from __future__ import annotations

import hashlib
import html
import copy
import csv
import json
import os
import random
import re
import secrets
import shutil
import socket
import stat
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Iterator
from . import archives
from .archives import ArchiveError, ExtractionLimits
from .diagnostic_paths import (
    INTERMEDIATES_DIRECTORY,
    extended_path,
    intermediate_files,
    is_diagnostic_artifact,
    is_intermediate_artifact,
    path_is_file,
)
from .download_store import unlink_tree
from .process_liveness import process_created_at, process_is_alive

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    import fcntl


USER_AGENT = "MS-DIAL-Interactive/0.3 public-reanalysis"
RAW_SUFFIXES = {
    ".abf", ".cdf", ".d", ".lcd", ".mzml", ".qgd", ".raw", ".wiff", ".wiff2",
}
CONVERSION_REQUIRED_SUFFIXES = (".mzxml", ".mzdata", ".mzdata.xml")
# What an archive is, and how it is opened, is decided in one place: archives.py. The lease routes an
# object by archives.archive_kind_from_name before its bytes exist, and the bytes confirm the kind
# (archives.detect_archive) before anything is extracted. The suffix set that used to live here named
# .gz but no .7z, .rar, .bz2, .xz or .lzma, and _is_archive disagreed with it about bare .gz.
#
# The file roles under which a repository lists an archive of a whole study (Metabolomics Workbench).
# A per-sample archive (X.raw.zip) is listed under its sample's own role, usually raw.
ARCHIVE_ROLES = frozenset({"raw_archive", "shared_raw_archive"})
# The guards every lease extraction runs under: free space less a reserve, the expansion ratio, the
# member count and the nesting depth (archives.ExtractionLimits). They replace the old limit of five
# times the download limit, which the campaign, with no per-unit size limit, could not have set.
LEASE_EXTRACTION_LIMITS = ExtractionLimits()
TEXT_RESULT_SUFFIXES = {
    ".mdalign", ".mdmsp", ".mdpeak", ".mdscan", ".mztab", ".mztabm",
}
PROJECT_RESULT_SUFFIXES = {".arf", ".arf2", ".dcl", ".mdproject"}


@dataclass
class RepositoryFile:
    name: str
    size_bytes: int = 0
    url: str = ""
    role: str = "raw"
    checksum: str = ""


def requires_msdial_conversion(name: str) -> bool:
    """Return whether a repository file must be converted to mzML before MS-DIAL can read it."""
    normalized = str(name or "").replace("\\", "/").casefold()
    return any(normalized.endswith(suffix) for suffix in CONVERSION_REQUIRED_SUFFIXES)


@dataclass
class RepositoryProject:
    repository: str
    accession: str
    analysis_unit_id: str = ""
    source_subrecord_id: str = ""
    title: str = ""
    description: str = ""
    public_url: str = ""
    metadata_url: str = ""
    assay_name: str = ""
    license: str = ""
    separation: str = "Unknown"
    acquisition_mode: str = "Unknown"
    ion_mode: str = "Unknown"
    untargeted: bool | None = None
    sample_count: int | None = None
    files: list[RepositoryFile] = field(default_factory=list)
    publications: list[dict[str, str]] = field(default_factory=list)
    publication_status: str = "none_recorded"
    metadata_sources: list[str] = field(default_factory=list)
    sample_metadata: list[dict[str, Any]] = field(default_factory=list)
    repository_metadata: dict[str, Any] = field(default_factory=dict)
    total_download_bytes: int = 0
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection_status: str = "unreviewed"
    review_reasons: list[str] = field(default_factory=list)
    eligible: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)
    download_scope: dict[str, Any] = field(default_factory=dict)
    class_proposal: dict[str, Any] | None = None
    blocking_reasons: list[str] = field(default_factory=list)
    pending_decisions: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.publications and self.publication_status == "none_recorded":
            self.publication_status = "recorded"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EligibilityPolicy:
    max_download_bytes: int = 5 * 1024**3
    max_samples: int = 40
    require_known_size: bool = True
    require_untargeted: bool = True
    allowed_separations: tuple[str, ...] = ("LC-MS",)
    allowed_acquisition_modes: tuple[str, ...] = ("DDA", "DIA", "AIF", "SWATH")


class RepositoryHttpClient:
    def __init__(self, timeout: int = 60) -> None:
        self.timeout = timeout

    def get_bytes(self, url: str) -> bytes:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return response.read()

    def get_text(self, url: str) -> str:
        return self.get_bytes(url).decode("utf-8", errors="replace")

    def get_json(self, url: str) -> Any:
        return json.loads(self.get_text(url))

    def content_length(self, url: str) -> int:
        request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return int(response.headers.get("Content-Length") or 0)
        except (urllib.error.HTTPError, urllib.error.URLError, ValueError):
            return 0

    def download(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """Fetch one repository object, resuming a partial transfer where the server allows it.

        WHY THIS RESUMES. This used to open the .part file with mode "wb" and send no Range
        header, so every attempt started at byte 0, and it unlinked the .part on any exception,
        so an interrupted transfer lost everything already moved. The library downloader in this
        same application has always resumed from its .part; the repository downloader, which
        handles the far larger objects, did not.

        That asymmetry is not theoretical. The backend stopped three times during 2026-09-06 runs
        and once more on 2026-09-20 during a 378 MB library transfer, each time leaving
        "The local backend stopped before this job completed" in the job record. Repository
        archives are bigger than that by an order of magnitude - one unit of the 2026-09-20 trial
        arrives as a single 1.80 GB zip, and the largest archive measured in the catalog is 38 GB -
        and each had to complete in one unbroken connection or start again from nothing.

        HOW IT RESUMES. A .part left by an earlier attempt is offered back to the server as
        `Range: bytes=<size>-`. A 206 means the server honoured it: the existing bytes are hashed
        first, then the response is appended. Anything else - a 200 because the server ignores
        ranges, a 416 because the .part is already as long as the resource, a changed
        ETag/Last-Modified - restarts from zero, because a resumed file that mixes two versions of
        an object is worse than a slow one. The checksums are computed over the whole file either
        way, so a wrong guess about resumability shows up as a checksum that does not match rather
        than as silent corruption.

        HOW A CHANGED OBJECT IS TOLD. The validators of the response that started a .part (its
        strong ETag, else its Last-Modified, and its Content-Length) are kept beside it in
        <name>.part.json, and a resume sends them back as If-Range. A server whose object has
        changed since then answers with the whole new object (200), not the tail of it, so the
        restart above is decided by the server rather than discovered later as a mismatch; for a
        repository that publishes no checksum (MetaboLights) nothing later would discover it. This
        used to be said here and not done: no validator was ever sent or stored. A .part with no
        validators beside it, left by an earlier version, resumes as it always did. The validators
        are returned (etag, last_modified), for the download record and the download store.

        WHAT IT NO LONGER DOES. It does not delete the .part on failure. That deletion is what made
        every retry start from zero, and keeping the bytes is the entire point. A .part is only
        removed when it is proven unusable, or when it is renamed into place on success.
        """
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        validators_path = destination.with_name(destination.name + ".part.json")
        resume_from = partial.stat().st_size if partial.is_file() else 0
        if resume_from > maximum_bytes:
            # A leftover larger than the limit cannot become a valid result, and hashing it would
            # only waste the time before saying so.
            partial.unlink(missing_ok=True)
            resume_from = 0
        validators = _read_part_validators(validators_path, url) if resume_from else {}

        headers = {"User-Agent": USER_AGENT}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
            if_range = _if_range_value(validators)
            if if_range:
                headers["If-Range"] = if_range
        request = urllib.request.Request(url, headers=headers)

        try:
            response = urllib.request.urlopen(request, timeout=max(self.timeout, 300))
        except urllib.error.HTTPError as error:
            # 416 means the range is past the end of the resource: the .part is stale, or the
            # object shrank. Either way the only safe answer is to fetch it whole.
            if error.code == 416 and resume_from:
                partial.unlink(missing_ok=True)
                validators_path.unlink(missing_ok=True)
                return self.download(url, destination, maximum_bytes, progress_callback)
            raise

        with response:
            appending = response.status == 206 and resume_from > 0
            if not appending:
                # The server ignored the range, or there was nothing to resume, or the object changed
                # since the .part began. Start clean rather than append a whole object onto a partial
                # one, and keep this response's validators for the next resume.
                resume_from = 0
                validators = _response_validators(response, url)
                _write_part_validators(validators_path, validators)
            declared = int(response.headers.get("Content-Length") or 0)
            total_declared = declared + resume_from if declared else 0
            if total_declared and total_declared > maximum_bytes:
                raise ValueError(
                    f"Remote object is {total_declared} bytes; limit is {maximum_bytes} bytes."
                )

            digest = hashlib.sha256()
            md5 = hashlib.md5()
            downloaded = 0
            if appending:
                # Seed both hashes with the bytes already on disk, so the checksums describe the
                # whole object and not only what this attempt fetched.
                with partial.open("rb") as existing:
                    for chunk in iter(lambda: existing.read(1024 * 1024), b""):
                        digest.update(chunk)
                        md5.update(chunk)
                        downloaded += len(chunk)
                if downloaded != resume_from:
                    # The file changed under us between the stat and the read.
                    raise ValueError(
                        f"Partial file {partial.name} is {downloaded} bytes, expected {resume_from}."
                    )
                if progress_callback:
                    progress_callback(downloaded, total_declared)

            with partial.open("ab" if appending else "wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > maximum_bytes:
                        raise ValueError(
                            f"Download exceeded the {maximum_bytes}-byte safety limit."
                        )
                    output.write(chunk)
                    digest.update(chunk)
                    md5.update(chunk)
                    if progress_callback:
                        progress_callback(downloaded, total_declared)

        # A SHORT READ IS NOT A COMPLETE DOWNLOAD, and urllib does not say so: a server that
        # declares a Content-Length and then hangs up early simply stops yielding chunks, and the
        # loop above ends exactly as it would on a clean finish. The old code renamed that
        # truncated file into place and reported success with a checksum computed over the part
        # that arrived, so every later stage agreed with it. Found by the resume test, which
        # serves a deliberately truncated response.
        if total_declared and downloaded != total_declared:
            raise ValueError(
                f"Download ended at {downloaded} of {total_declared} declared bytes. "
                f"The partial file is kept at {partial.name} and the next attempt will resume."
            )

        partial.replace(destination)
        validators_path.unlink(missing_ok=True)
        result = {
            "path": str(destination),
            "size_bytes": downloaded,
            "sha256": digest.hexdigest(),
            "md5": md5.hexdigest(),
            "resumed_from_bytes": resume_from,
        }
        # Only what the server said, so a record of a server that sends none reads as it always did.
        for key in ("etag", "last_modified"):
            if validators.get(key):
                result[key] = validators[key]
        return result


def _response_validators(response: Any, url: str) -> dict[str, Any]:
    return {
        "url": url,
        "etag": str(response.headers.get("ETag") or "").strip(),
        "last_modified": str(response.headers.get("Last-Modified") or "").strip(),
        "content_length": int(response.headers.get("Content-Length") or 0),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }


def _write_part_validators(path: Path, validators: dict[str, Any]) -> None:
    """Keep a transfer's validators beside its .part; a transfer that sent none leaves no file."""
    if not (validators.get("etag") or validators.get("last_modified")):
        path.unlink(missing_ok=True)
        return
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(validators, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _read_part_validators(path: Path, url: str) -> dict[str, Any]:
    """The validators of the response that started a .part, when they are for this URL."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) and value.get("url") == url else {}


def _if_range_value(validators: dict[str, Any]) -> str:
    """A strong ETag, else a Last-Modified date: If-Range takes no weak validator (RFC 9110 13.1.5)."""
    etag = str(validators.get("etag") or "")
    if etag and not etag.startswith("W/"):
        return etag
    return str(validators.get("last_modified") or "")


class MetabolomicsWorkbenchAdapter:
    name = "metabolomics_workbench"
    base = "https://www.metabolomicsworkbench.org"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        text = self.client.get_text(f"{self.base}/rest/study/study_id/ST/available/json")
        return sorted(set(re.findall(r"(?m)^study_id\t(ST\d+)$", text)))

    def inspect(self, accession: str) -> RepositoryProject:
        summary_url = f"{self.base}/rest/study/study_id/{accession}/summary/json"
        analysis_url = f"{self.base}/rest/study/study_id/{accession}/analysis/json"
        summary = _parse_tab_blocks(self.client.get_text(summary_url))
        analysis = _parse_tab_blocks(self.client.get_text(analysis_url))
        summary_row = summary[0] if summary else {}
        public_url = summary_row.get(
            "study_url",
            f"{self.base}/data/DRCCMetadata.php?Mode=Study&StudyID={accession}",
        )
        detail_html = self.client.get_text(public_url)
        detail_text = _html_text(detail_html)
        combined = " ".join(
            value for row in [*summary, *analysis] for value in row.values()
        ) + " " + detail_text
        separation = _infer_separation(combined)
        acquisition = _infer_acquisition(combined)
        untargeted = _infer_untargeted(combined)
        download_page = (
            f"{self.base}/data/DRCCStudySummary.php?Mode=SetupRawDataDownload&StudyID={accession}"
        )
        download_html = self.client.get_text(download_page)
        files = _parse_workbench_downloads(download_html, self.base)
        total = sum(item.size_bytes for item in files)
        project = RepositoryProject(
            repository=self.name,
            accession=accession,
            title=summary_row.get("study_title", ""),
            description=summary_row.get("study_summary", ""),
            public_url=public_url,
            metadata_url=summary_url,
            license=summary_row.get("license", ""),
            separation=separation,
            acquisition_mode=acquisition,
            ion_mode=_infer_ion_mode(combined),
            untargeted=untargeted,
            sample_count=_parse_int(summary_row.get("number_of_samples", "")),
            files=files,
            publications=_extract_publications(summary_row, detail_text),
            metadata_sources=[summary_url, analysis_url, public_url],
            total_download_bytes=total,
            evidence=[
                f"analysis_type={summary_row.get('analysis_type', '')}",
                f"analysis records={len(analysis)}",
            ],
        )
        if not files:
            project.warnings.append("No public raw-data archive was found on the study download page.")
        return project

    def inspect_metadata(self, accession: str) -> RepositoryProject:
        project = self.inspect(accession)
        summary_url = f"{self.base}/rest/study/study_id/{accession}/summary/json"
        analysis_url = f"{self.base}/rest/study/study_id/{accession}/analysis/json"
        factors_url = f"{self.base}/rest/study/study_id/{accession}/factors/json"
        summary = _parse_tab_blocks(self.client.get_text(summary_url))
        analysis = _parse_tab_blocks(self.client.get_text(analysis_url))
        try:
            factor_rows = _parse_workbench_records(self.client.get_text(factors_url))
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            factor_rows = []
        project.metadata_sources = list(
            dict.fromkeys([*project.metadata_sources, factors_url])
        )
        project.sample_metadata = _workbench_sample_metadata(factor_rows)
        project.repository_metadata = {
            "summary": summary,
            "analysis": analysis,
            "factors": factor_rows,
        }
        return project


class MetaboLightsAdapter:
    name = "metabolights"
    api = "https://www.ebi.ac.uk/metabolights/ws"
    public = "https://ftp.ebi.ac.uk/pub/databases/metabolights/studies/public"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        records = self.client.get_json(f"{self.api}/studies/technology")
        result = []
        for record in records:
            technology = str(record.get("technology") or "")
            if _infer_separation(technology) in {"GC-MS", "LC-MS"}:
                result.append(str(record["accession"]))
        return sorted(set(result))

    def inspect(self, accession: str) -> RepositoryProject:
        study_url = f"{self.api}/studies/{accession}"
        payload = self.client.get_json(study_url)
        mtbls = payload.get("mtblsStudy", {})
        investigation = payload.get("isaInvestigation", {})
        studies = investigation.get("studies") or []
        study = studies[0] if studies else {}
        study_text = json.dumps(investigation, ensure_ascii=False)
        assay_listing = self.client.get_json(f"{self.api}/studies/{accession}/assays")
        assay_names = [
            item.get("filename", "")
            for item in assay_listing.get("data", {}).get("assays", [])
        ]
        file_index = _parse_apache_index(
            self.client.get_text(f"{self.public}/{accession}/FILES/")
        )
        assay_groups = []
        for assay_name in assay_names:
            if not assay_name:
                continue
            text = self.client.get_text(
                f"{self.api}/studies/{accession}/{urllib.parse.quote(assay_name)}"
            )
            raw_names = _raw_names_from_assay(text)
            files = _metabolights_files(accession, raw_names, file_index, self.public)
            separation = _infer_separation(assay_name)
            if separation == "Unknown":
                separation = _infer_separation(text)
            assay_groups.append(
                {
                    "name": assay_name,
                    "text": text,
                    "separation": separation,
                    "acquisition": _infer_acquisition(text),
                    "files": files,
                    "total": 0 if any(item.size_bytes <= 0 for item in files) else sum(item.size_bytes for item in files),
                }
            )
        selected_group = max(assay_groups, key=_metabolights_group_rank, default={})
        files = selected_group.get("files", [])
        combined = study_text + " " + selected_group.get("text", "")
        unknown_sizes = [item.name for item in files if item.size_bytes <= 0]
        total = selected_group.get("total", 0)
        sample_count = len(study.get("materials", {}).get("samples", [])) or len(files) or None
        project = RepositoryProject(
            repository=self.name,
            accession=accession,
            title=str(study.get("title") or investigation.get("title") or ""),
            description=str(study.get("description") or ""),
            public_url=f"https://www.ebi.ac.uk/metabolights/{accession}",
            metadata_url=study_url,
            assay_name=selected_group.get("name", ""),
            license=str(mtbls.get("datasetLicense") or _comment_value(study, "License")),
            separation=selected_group.get("separation", "Unknown"),
            acquisition_mode=selected_group.get("acquisition", "Unknown"),
            ion_mode=_infer_ion_mode(combined),
            untargeted=_infer_untargeted(combined),
            sample_count=sample_count,
            files=files,
            publications=_extract_publications(study.get("publications", [])),
            metadata_sources=[study_url, f"{self.api}/studies/{accession}/assays"],
            total_download_bytes=total,
            evidence=[
                f"available assay files={len(assay_names)}",
                f"selected assay={selected_group.get('name', '')}",
                f"selected spectral references={len(files)}",
            ],
        )
        if len({group["separation"] for group in assay_groups if group["separation"] != "Unknown"}) > 1:
            project.warnings.append("The study contains multiple analytical technologies; one assay was selected for this run.")
        if not files:
            project.warnings.append("ISA assay metadata did not expose raw spectral data filenames.")
        elif unknown_sizes:
            project.warnings.append(
                f"Size was unavailable for {len(unknown_sizes)} raw references; nested FILES paths may require review."
            )
        return project

    def inspect_metadata(self, accession: str) -> RepositoryProject:
        project = self.inspect(accession)
        study_url = f"{self.api}/studies/{accession}"
        payload = self.client.get_json(study_url)
        investigation = payload.get("isaInvestigation", {})
        studies = investigation.get("studies") or []
        study = studies[0] if studies else {}
        assay_text = ""
        if project.assay_name:
            assay_text = self.client.get_text(
                f"{self.api}/studies/{accession}/{urllib.parse.quote(project.assay_name)}"
            )
        study_table_url = f"{self.public}/{accession}/s_{accession}.txt"
        try:
            study_table_text = self.client.get_text(study_table_url)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            study_table_text = ""
        project.metadata_sources = list(
            dict.fromkeys([*project.metadata_sources, study_table_url])
        )
        project.sample_metadata = _metabolights_sample_metadata(
            assay_text, study, study_table_text
        )
        project.repository_metadata = {
            "study_api": payload,
            "selected_assay_name": project.assay_name,
            "selected_assay_rows": _assay_rows(assay_text),
            "study_table": study_table_text,
        }
        return project


class MbPostAdapter:
    name = "mb_post"
    base = "https://repository.massbank.jp"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        payload = self.client.get_json(f"{self.base}/api/projects?limit=1000&offset=0")
        return sorted(str(item["mbpostId"]) for item in payload.get("list", []))

    def inspect(self, accession: str) -> RepositoryProject:
        project_data = self.client.get_json(f"{self.base}/api/projects/{accession}")
        location = str(project_data.get("location") or f"{accession}.0")
        listing_url = f"{self.base}/api/projects/{location}/files?limit=10000&offset=0"
        listing = self.client.get_json(listing_url)
        raw_items = [item for item in listing.get("list", []) if item.get("type") == "raw"]
        files = [
            RepositoryFile(
                name=str(item.get("name") or ""),
                size_bytes=int(item.get("size") or 0),
                url=f"{self.base}/api/download/{location}",
                role="sidecar" if _is_sidecar_name(str(item.get("name") or "")) else "raw",
                checksum=str(item.get("checksum") or ""),
            )
            for item in raw_items
        ]
        primary_items = [item for item in raw_items if not _is_sidecar_name(str(item.get("name") or ""))]
        profile_text = ""
        if raw_items:
            detail = self.client.get_json(
                f"{self.base}/api/projects/{location}/files/{raw_items[0]['id']}"
            )
            profile_text = json.dumps(detail, ensure_ascii=False)
        combined = " ".join(
            [
                str(project_data.get("title") or ""),
                str(project_data.get("keywords") or ""),
                str(project_data.get("description") or ""),
                profile_text,
            ]
        )
        archive_size = int(listing.get("meta", {}).get("size") or 0)
        return RepositoryProject(
            repository=self.name,
            accession=accession,
            title=str(project_data.get("title") or ""),
            description=str(project_data.get("description") or ""),
            public_url=f"{self.base}/#/projects/{accession}",
            metadata_url=f"{self.base}/api/projects/{accession}",
            license="CC0 1.0",
            separation=_infer_separation(combined),
            acquisition_mode=_infer_acquisition(combined),
            ion_mode=_infer_ion_mode(combined),
            untargeted=_infer_untargeted(combined),
            sample_count=len(primary_items) or None,
            files=files,
            publications=_extract_publications(
                project_data.get("publications")
                or project_data.get("publication")
                or {
                    "title": "",
                    "pubmedId": project_data.get("pubmedId", ""),
                    "doi": project_data.get("doi", ""),
                }
            ),
            metadata_sources=[
                f"{self.base}/api/projects/{accession}",
                listing_url,
            ],
            sample_metadata=_mbpost_sample_metadata(project_data, primary_items),
            repository_metadata={
                "project": project_data,
                "file_listing": listing,
                "first_raw_file_detail": json.loads(profile_text) if profile_text else {},
            },
            total_download_bytes=archive_size,
            evidence=[
                f"primary raw files={len(primary_items)}",
                f"sidecar/companion files={len(raw_items) - len(primary_items)}",
                "MB-POST analytical-condition preset",
            ],
        )

    def inspect_metadata(self, accession: str) -> RepositoryProject:
        project = self.inspect(accession)
        location = next(
            (
                urllib.parse.urlparse(item.url).path.rsplit("/", 1)[-1]
                for item in project.files
                if item.url
            ),
            f"{accession}.0",
        )
        listing_url = f"{self.base}/api/projects/{location}/files?limit=10000&offset=0"
        listing = self.client.get_json(listing_url)
        raw_items = [item for item in listing.get("list", []) if item.get("type") == "raw"]

        def read_detail(item: dict[str, Any]) -> tuple[str, dict[str, Any], str]:
            try:
                detail = self.client.get_json(
                    f"{self.base}/api/projects/{location}/files/{item['id']}"
                )
                return str(item.get("name") or ""), detail, ""
            except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as error:
                return str(item.get("name") or ""), {}, str(error)

        details: dict[str, dict[str, Any]] = {}
        with ThreadPoolExecutor(max_workers=min(6, max(1, len(raw_items)))) as executor:
            for name, detail, error in executor.map(read_detail, raw_items):
                details[name.casefold()] = detail
                if error:
                    project.warnings.append(
                        f"Sample metadata detail was unavailable for {name}: {error}"
                    )
        project.sample_metadata = _mbpost_sample_metadata(
            {"keywords": ""}, raw_items, details
        )
        project.repository_metadata["raw_file_details"] = details
        project.metadata_sources.extend(
            f"{self.base}/api/projects/{location}/files/{item['id']}" for item in raw_items
        )
        return project


class MetaboBankAdapter:
    name = "metabobank"
    search_api = "https://ddbj.nig.ac.jp/search/api"

    def __init__(self, client: RepositoryHttpClient | None = None) -> None:
        self.client = client or RepositoryHttpClient()

    def list_accessions(self) -> list[str]:
        result = []
        page = 1
        while True:
            payload = self.client.get_json(
                f"{self.search_api}/entries/metabobank/?page={page}&perPage=100"
            )
            result.extend(
                str(item.get("identifier") or "")
                for item in payload.get("items", [])
                if item.get("identifier")
            )
            if not payload.get("pagination", {}).get("hasNext"):
                break
            page += 1
        return sorted(set(result))

    def inspect(self, accession: str) -> RepositoryProject:
        accession = accession.upper()
        metadata_url = f"{self.search_api}/entries/metabobank/{accession}"
        entry = self.client.get_json(metadata_url)
        data_root = _metabobank_data_root(entry)
        if not data_root:
            raise ValueError("MetaboBank did not publish a DATA distribution URL.")
        filelist_url = urllib.parse.urljoin(data_root, f"{accession}.filelist.txt")
        sdrf_url = urllib.parse.urljoin(data_root, f"{accession}.sdrf.txt")
        idf_url = urllib.parse.urljoin(data_root, f"{accession}.idf.txt")
        filelist_text = self.client.get_text(filelist_url)
        sdrf_text = self.client.get_text(sdrf_url)
        file_rows = _parse_metabobank_filelist(filelist_text)
        sdrf_rows = _parse_metabobank_sdrf(sdrf_text)
        raw_references = _metabobank_raw_references(sdrf_rows)
        files, fallback_to_abf = _metabobank_raw_files(
            file_rows, raw_references, data_root
        )
        combined = " ".join(
            [
                str(entry.get("title") or ""),
                str(entry.get("description") or ""),
                " ".join(_string_list(entry.get("studyType"))),
                " ".join(_string_list(entry.get("experimentType"))),
                " ".join(_string_list(entry.get("submissionType"))),
                sdrf_text,
            ]
        )
        project = RepositoryProject(
            repository=self.name,
            accession=accession,
            title=str(entry.get("title") or ""),
            description=str(entry.get("description") or ""),
            public_url=str(entry.get("url") or f"https://ddbj.nig.ac.jp/search/entry/metabobank/{accession}"),
            metadata_url=metadata_url,
            license=str(entry.get("license") or ""),
            separation=_infer_separation(combined),
            acquisition_mode=_infer_acquisition(combined),
            ion_mode=_infer_ion_mode(combined),
            untargeted=_infer_untargeted(combined),
            sample_count=len(sdrf_rows) or None,
            files=files,
            publications=_metabobank_publications(entry.get("publication")),
            metadata_sources=[metadata_url, idf_url, sdrf_url, filelist_url],
            sample_metadata=_metabobank_sample_metadata(sdrf_rows),
            repository_metadata={
                "search_entry": entry,
                "data_root": data_root,
                "sdrf_rows": sdrf_rows,
                "filelist": file_rows,
            },
            total_download_bytes=sum(item.size_bytes for item in files),
            evidence=[
                f"MAGE-TAB SDRF rows={len(sdrf_rows)}",
                f"original raw references={len(raw_references)}",
                f"download objects={len(files)}",
            ],
        )
        if fallback_to_abf:
            project.warnings.append(
                "Original vendor raw references were unavailable; MetaboBank ABF files were selected as a fallback."
            )
        if not files:
            project.warnings.append(
                "MAGE-TAB raw-data references did not match downloadable file-list entries."
            )
        return project

    def inspect_metadata(self, accession: str) -> RepositoryProject:
        return self.inspect(accession)


ADAPTERS = {
    "metabolomics_workbench": MetabolomicsWorkbenchAdapter,
    "metabolights": MetaboLightsAdapter,
    "mb_post": MbPostAdapter,
    "metabobank": MetaboBankAdapter,
}


def evaluate_eligibility(project: RepositoryProject, policy: EligibilityPolicy) -> RepositoryProject:
    reasons = []
    review_reasons = []
    if project.separation == "Unknown":
        review_reasons.append("Confirm LC-MS separation from repository context or raw scan metadata.")
    elif project.separation not in set(policy.allowed_separations):
        reasons.append("Repository reanalysis currently accepts LC-MS data only.")
    if project.separation == "LC-MS":
        if project.acquisition_mode == "Unknown":
            review_reasons.append("Inspect raw scan metadata to distinguish DDA from DIA/AIF/SWATH.")
        elif project.acquisition_mode == "Mixed":
            review_reasons.append(
                "Raw headers show more than one acquisition mode across this unit's files. MS-DIAL "
                "runs one acquisition mode per analysis; split the unit by per-file acquisition mode."
            )
        elif project.acquisition_mode not in set(policy.allowed_acquisition_modes):
            reasons.append("Repository reanalysis currently accepts untargeted DDA or DIA/AIF/SWATH LC-MS/MS acquisition only.")
        if project.ion_mode == "Unknown":
            review_reasons.append("Confirm LC-MS ion mode from raw scan metadata.")
        elif project.ion_mode == "Both":
            review_reasons.append(
                "Distinguish polarity-switching data from separate positive/negative files before analysis."
            )
    if policy.require_untargeted:
        if project.untargeted is False:
            reasons.append("Repository metadata identifies the study as targeted.")
        elif project.untargeted is None:
            review_reasons.append("Confirm untargeted status from repository context or raw scan metadata.")
    if not project.files:
        reasons.append("No downloadable raw data were identified.")
    conversion_required = {
        item.name
        for item in project.files
        if item.role == "requires_conversion"
        or (
            item.role in ANALYSIS_INPUT_ROLES
            and requires_msdial_conversion(item.name)
        )
    }
    conversion_required.update(
        str((sample or {}).get("raw_file") or "").strip()
        for sample in project.sample_metadata or []
        if requires_msdial_conversion(str((sample or {}).get("raw_file") or ""))
    )
    conversion_required.discard("")
    if conversion_required:
        ordered_conversion_inputs = sorted(conversion_required, key=str.casefold)
        preview = ", ".join(ordered_conversion_inputs[:3])
        if len(conversion_required) > 3:
            preview += f", and {len(conversion_required) - 3} more"
        reasons.append(
            "MS-DIAL has no mzXML/mzData reader. Convert the declared analysis input(s) to mzML "
            f"with ProteoWizard msconvert before reanalysis: {preview}."
        )
    if policy.require_known_size and project.total_download_bytes <= 0:
        reasons.append("Download size is unknown.")
    if project.total_download_bytes > policy.max_download_bytes:
        reasons.append(
            f"Download size {project.total_download_bytes} exceeds {policy.max_download_bytes} bytes."
        )
    if project.sample_count and project.sample_count > policy.max_samples:
        reasons.append(f"Sample/raw-file count {project.sample_count} exceeds {policy.max_samples}.")
    project.exclusion_reasons = reasons
    project.review_reasons = review_reasons if not reasons else []
    project.eligible = not reasons and not review_reasons
    if reasons:
        project.selection_status = "excluded"
    elif review_reasons:
        project.selection_status = "raw_metadata_required"
    else:
        project.selection_status = "eligible"
    return project


def discover_candidates(
    repository: str,
    count: int,
    seed: int,
    policy: EligibilityPolicy,
    inspection_limit: int = 200,
    workers: int = 4,
    client: RepositoryHttpClient | None = None,
) -> dict[str, Any]:
    if repository not in ADAPTERS:
        raise ValueError(f"Unknown repository: {repository}")
    adapter = ADAPTERS[repository](client)
    accessions = adapter.list_accessions()
    random.Random(seed).shuffle(accessions)
    inspected = []
    eligible = []
    preflight = []
    def inspect_one(accession: str) -> RepositoryProject:
        try:
            return evaluate_eligibility(adapter.inspect(accession), policy)
        except Exception as error:
            project = RepositoryProject(repository=repository, accession=accession)
            project.selection_status = "excluded"
            project.exclusion_reasons = [f"Inspection failed: {type(error).__name__}: {error}"]
            return project

    maximum = min(inspection_limit, len(accessions))
    batch_size = max(1, min(8, workers * 2))
    for offset in range(0, maximum, batch_size):
        batch = accessions[offset:min(offset + batch_size, maximum)]
        with ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
            projects = list(executor.map(inspect_one, batch))
        for project in projects:
            inspected.append(project)
            if project.eligible:
                eligible.append(project)
            elif project.selection_status == "raw_metadata_required":
                preflight.append(project)
        if len(eligible) >= count:
            eligible = eligible[:count]
            break
    return {
        "schema": "msdial-public-reanalysis-selection.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "repository": repository,
        "seed": seed,
        "policy": asdict(policy),
        "requested_count": count,
        "selected": [item.as_dict() for item in eligible],
        "raw_metadata_required": [item.as_dict() for item in preflight],
        "inspected": [item.as_dict() for item in inspected],
    }


def resolve_required_download_bytes(
    declared_bundle_bytes: Any, unit_file_bytes: Any
) -> dict[str, Any]:
    """Bytes that must actually be transferred for one analysis unit.

    ``download_scope.bundle_bytes`` is copied from the catalog handoff and is
    unverified until a download job has read the bundle's own Content-Length. A
    bundle may legitimately be far larger than the unit's files, because several
    units often share one archive, but it can never be smaller than the files it
    has to supply. When the declared figure falls below the total recomputed from
    the file manifest, the recomputed total is the safety-limit quantity and the
    declared figure is reported as contradicted rather than used. A stale handoff
    left behind by a re-bundled accession fails this way, not silently.
    """
    unit_bytes = max(int(unit_file_bytes or 0), 0)
    declared = max(int(declared_bundle_bytes or 0), 0)
    return {
        "required_download_bytes": max(declared, unit_bytes),
        "declared_bundle_bytes": declared,
        "unit_file_bytes": unit_bytes,
        "bundle_bytes_verified": False,
        "bundle_bytes_contradicted": bool(declared and unit_bytes and declared < unit_bytes),
    }


# The only two retention policies that mean anything. Named once because the HTTP download
# endpoint validated against an inline set while every other reader compared against a bare literal,
# so a third reader could -- and did -- accept a string no writer would ever produce.
RAW_RETENTION_POLICIES = ("keep", "delete_after_validated_output")
RAW_RETENTION_DEFAULT = "keep"


def normalize_raw_retention_policy(value: Any) -> tuple[str, bool]:
    """The policy to act on, and whether the caller asked for something unrecognised.

    Returns the default rather than the input when the input means nothing, because the only
    alternative to "keep" is an irreversible deletion and an unreadable request must never resolve
    towards it. The second value is what lets a caller SAY SO: the old code compared against a bare
    literal, so "delete", "Delete" and a trailing space were all silently keep-equivalent and the log
    reported "downloaded repository raw data were kept" -- true, and no help at all to someone who
    believed they had asked for deletion. At full-repository scale that request vanishes without a
    word.
    """
    text = str(value or "").strip()
    if not text:
        return RAW_RETENTION_DEFAULT, False
    return (text, False) if text in RAW_RETENTION_POLICIES else (RAW_RETENTION_DEFAULT, True)


def create_download_lease(
    project: RepositoryProject,
    workspace_root: Path,
    maximum_bytes: int,
    client: RepositoryHttpClient | None = None,
    allow_preflight: bool = False,
    progress_callback: Any = None,
    raw_retention_policy: str = "keep",
    campaign_authorization: dict[str, Any] | None = None,
    job_id: str = "",
) -> dict[str, Any]:
    """Download one unit's objects into its workspace and write the unit's run manifest.

    ``campaign_authorization`` is the crossing record a validated campaign approval produced for this
    download (msdial_app.campaign_authorization); it is written into the manifest from the first write
    on. None, the default, writes nothing about a campaign.

    ``job_id`` is the backend job the lease runs in. It is recorded with this process as the lease's
    owner, so a lease left "downloading" by a process that has since died can be told from a live one.

    A manifest already in the workspace is never lost: unless it is itself an unfinished or discarded
    lease, it is copied byte for byte beside itself before the first write, and the copy is named in
    previous_manifest and superseded_manifests. A split parent is not re-leased at all, and neither is a
    workspace another live lease is still downloading into.

    THE LEASE RUNS IN STAGES, EACH RECORDED (lease_stages, in LEASE_STAGES order):

    - fetch: every object the unit lists, once per URL;
    - verify_declared_checksums: each object's published MD5 compared with its bytes as it arrives,
      before the next is fetched, and each archive's format confirmed by its leading bytes;
    - extract: each archive expanded by archives.extract_archive into a staging tree beside the data
      root, then moved into it without overwriting anything (archive_extractions, and the member listing
      in provenance\\archive-members-<sha12>.tsv);
    - materialise and convert: recorded as not_used. The accession download store and the mzXML
      conversion are wired in here, after extraction and before input discovery, and nothing else in the
      order changes when they are;
    - discover: the MS-DIAL inputs under the data root;
    - attribute: the unit's own inputs, extracted files and declared checksums (allowlist_checksum_
      validation), and one input_lineage row per input;
    - record: the manifest.

    The stages are written into the manifest as they finish, so a lease that stops says where
    (download_failure.stage), and a unit that has nothing to extract records the same stages with
    nothing done in extract.
    """
    downloadable = project.eligible or (
        allow_preflight and project.selection_status == "raw_metadata_required"
    )
    if not downloadable:
        raise ValueError("Only an eligible or explicitly approved preflight project can receive a download lease.")
    size = resolve_required_download_bytes(
        project.download_scope.get("bundle_bytes"), project.total_download_bytes
    )
    required_download_bytes = size["required_download_bytes"]
    if required_download_bytes > maximum_bytes:
        raise ValueError(
            f"Required repository bundle is {required_download_bytes} bytes; "
            f"the download lease limit is {maximum_bytes} bytes."
        )
    client = client or RepositoryHttpClient()
    adapter_type = ADAPTERS.get(project.repository)
    if not project.analysis_unit_id and adapter_type and hasattr(adapter_type, "inspect_metadata"):
        try:
            detailed = adapter_type(client).inspect_metadata(project.accession)
            project.publications = detailed.publications
            project.metadata_sources = detailed.metadata_sources
            project.sample_metadata = detailed.sample_metadata
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
            project.warnings.append(f"Detailed repository sample metadata was unavailable: {error}")
    root = workspace_root.resolve() / project.repository / project.accession
    if project.analysis_unit_id:
        root = root / project.analysis_unit_id
    raw_root = root / "raw"
    download_root = raw_root / "downloads"
    data_root = raw_root / "data"
    provenance = root / "provenance"
    output = root / "output"
    for directory in (download_root, data_root, provenance, output):
        directory.mkdir(parents=True, exist_ok=True)
    manifest_path = provenance / "run-manifest.json"
    # WRITTEN BEFORE THE FIRST BYTE, AND AGAIN IF THE LEASE FAILS.
    #
    # The manifest used to be written once, after every object had arrived and been extracted. A lease
    # that failed on the way - a checksum mismatch, an allow-list that attributed nothing, a backend that
    # stopped - left bytes under raw\ and no record of whose they were or why they were there, and
    # discard_download_lease, which reads the manifest to find the raw directory it may remove, could not
    # act on them. At campaign scale that is disk filling with orphans nobody can release.
    #
    # So the unit says "downloading" before it downloads, and "download_failed", with the reason, when it
    # does not finish. Nothing here is eligible for a run or a cleanup until the full record replaces it.
    #
    # It also says who is downloading. A process killed mid-transfer - a reboot, a backend stopped - writes
    # nothing, and its lease stays "downloading" for good. The owner (job, process id, the process's
    # creation time, host) and a heartbeat refreshed as bytes arrive are what let discard_download_lease
    # tell that lease from one still running (lease_owner_state).
    started_at = datetime.now(timezone.utc).isoformat()
    owner = _new_lease_owner(job_id)
    lease_record: dict[str, Any] = {
        "schema": "msdial-public-reanalysis-run.v1",
        "created_at": started_at,
        "status": "downloading",
        "project": project.as_dict(),
        "workspace": str(root),
        "raw_directory": str(raw_root),
        "input_directory": str(data_root),
        "output_directory": str(output),
        "downloads": [],
        "execution_allowed": False,
        "cleanup_allowed": False,
        "raw_retention_policy": raw_retention_policy,
        "download_started_at": started_at,
        "lease_owner": owner,
        "download_progress_at": started_at,
    }
    if campaign_authorization:
        lease_record["campaign_authorizations"] = [dict(campaign_authorization)]
    # Held before the first write, so this process never reads its own new lease as abandoned.
    _hold_lease(owner["lease_id"])
    try:
        # Under the manifest's lock, so nothing is written between the look at what is there and the
        # write that replaces it.
        with manifest_lock(manifest_path):
            previous, superseded = _supersede_previous_manifest(manifest_path)
            if previous:
                # A lease into a workspace that already holds a manifest replaces it, as it always has.
                # What it replaced is said, so a retry is visibly a retry and not a first attempt, and a
                # record worth keeping was copied aside first rather than overwritten.
                lease_record["previous_manifest"] = previous
            if superseded:
                lease_record["superseded_manifests"] = superseded
            _write_json(manifest_path, lease_record)
    except BaseException:
        _release_lease(owner["lease_id"])
        raise

    last_beat = time.monotonic()

    def beat(received: int) -> None:
        nonlocal last_beat
        now = time.monotonic()
        if now - last_beat < LEASE_HEARTBEAT_SECONDS:
            return
        last_beat = now
        stamp = datetime.now(timezone.utc).isoformat()
        lease_record["download_progress_at"] = stamp
        lease_record["download_received_bytes"] = received
        _beat_lease(manifest_path, owner["lease_id"], stamp, received)

    stages = _LeaseStages(manifest_path, lease_record, owner["lease_id"])
    downloads: list[dict[str, Any]] = []
    # The same list, so every stage write says which objects have arrived.
    lease_record["downloads"] = downloads
    archive_extractions: list[dict[str, Any]] = []
    lease_record["archive_extractions"] = archive_extractions
    try:
        unique_urls: dict[str, RepositoryFile] = {}
        for item in project.files:
            unique_urls.setdefault(item.url, item)
        downloaded_bytes = 0
        total_objects = len(unique_urls)
        # Where each archive expands, by its download path: the data root for a bundle, the directory
        # it was listed in for a per-sample container or a single compressed file.
        placements: dict[str, str] = {}
        taken: set[str] = set()
        verification = {
            "objects": 0, "md5_verified": 0, "not_declared": 0, "not_compared": 0, "archives_confirmed": 0,
        }
        stages.start("fetch")
        stages.start("verify_declared_checksums")
        for index, (url, item) in enumerate(unique_urls.items(), start=1):
            filename, archive_kind, placement = _route_object(project, url, item, index)
            if archive_kind:
                destination = _archive_download_path(download_root, filename, index, taken)
            else:
                destination = data_root / _safe_relative_name(item.name)
            def item_progress(received: int, declared: int) -> None:
                beat(downloaded_bytes + received)
                if progress_callback:
                    known_total = required_download_bytes or (
                        downloaded_bytes + declared if declared else 0
                    )
                    progress_callback(
                        index,
                        total_objects,
                        item.name,
                        downloaded_bytes + received,
                        known_total,
                    )

            stages.at("fetch")
            result = client.download(
                url,
                destination,
                maximum_bytes - downloaded_bytes,
                progress_callback=item_progress,
            )
            downloaded_bytes += result["size_bytes"]
            result["source_url"] = url
            result["declared_checksum"] = item.checksum
            stages.at("verify_declared_checksums")
            outcome = _verify_object_checksum(result, item, project.repository, filename)
            verification["objects"] += 1
            verification[outcome] += 1
            if archive_kind:
                # The name routed it; the bytes must agree before anything is extracted. An error page
                # saved under the archive's name fails here, as a failed download, not later as "no
                # inputs".
                detection = archives.detect_archive(Path(result["path"]), filename)
                result["archive"] = detection.record()
                placements[_file_key(result["path"])] = placement
                verification["archives_confirmed"] += 1
            downloads.append(result)
            beat(downloaded_bytes)
            if progress_callback:
                progress_callback(
                    index,
                    total_objects,
                    item.name,
                    downloaded_bytes,
                    required_download_bytes or downloaded_bytes,
                )
        stages.finish(
            "fetch",
            objects_declared=total_objects,
            objects_completed=len(downloads),
            bytes=downloaded_bytes,
            archives=len(placements),
        )
        stages.finish("verify_declared_checksums", **verification)

        stages.start("extract")
        # Which archive each extracted file came from, for the input lineage below. The extraction loop
        # used to pool every member into one list and the association was gone.
        extracted: list[str] = []
        extracted_from: dict[str, dict[str, Any]] = {}
        extracted_members: dict[str, dict[str, Any]] = {}
        for item in downloads:
            if not item.get("archive"):
                continue
            record, members = _extract_into_data_root(
                Path(item["path"]),
                item,
                placements[_file_key(item["path"])],
                data_root,
                raw_root,
                provenance,
                len(archive_extractions) + 1,
                earlier=archive_extractions,
            )
            archive_extractions.append(record)
            for member_path, member in members:
                key = _file_key(member_path)
                extracted.append(member_path)
                # The first archive to write a file keeps it; a later one that carried the same bytes
                # found it already there (merge.already_present_files).
                extracted_from.setdefault(key, item)
                extracted_members.setdefault(key, {**member, "extraction": len(archive_extractions) - 1})
        stages.finish(
            "extract",
            archives=len(archive_extractions),
            files=len(extracted),
            files_already_present=sum(
                int((record.get("merge") or {}).get("already_present_files") or 0)
                for record in archive_extractions
            ),
        )
        stages.not_used(
            "materialise",
            "Every object was fetched into this unit's own raw tree; no accession download store is in use.",
        )
        stages.not_used("convert", "No input conversion ran in this lease.")

        stages.start("discover")
        all_inputs = _find_msdial_inputs(data_root)
        stages.finish("discover", input_candidates=len(all_inputs))

        stages.start("attribute")
        selected_extracted = _filter_project_allowlist_paths(extracted, data_root, project)
        verified_checksums: dict[str, dict[str, Any]] = {}
        checksum_validation = _verify_project_allowlist_checksums(
            data_root, project, verified_checksums, downloads, archive_extractions
        )
        inputs = _filter_inputs_by_project_allowlist(all_inputs, data_root, project)
        analysis_input = _common_input_path(inputs, data_root)
        input_lineage = build_input_lineage(
            inputs,
            downloads,
            extracted_from,
            data_root,
            download_root,
            project,
            verified_checksums,
            extracted_members=extracted_members,
            archive_extractions=archive_extractions,
        )
        stages.finish(
            "attribute",
            input_candidates=len(inputs),
            ignored_input_candidates=len(all_inputs) - len(inputs),
            extracted_files=len(selected_extracted),
            ignored_extracted_files=len(extracted) - len(selected_extracted),
            declared_files_verified=checksum_validation.get("verified", 0),
            archives_verified_at_download=checksum_validation.get("archives_verified_at_download", 0),
        )

        stages.start("record")
        manifest = {
            "schema": "msdial-public-reanalysis-run.v1",
            "created_at": started_at,
            "status": "prepared",
            "project": project.as_dict(),
            "workspace": str(root),
            "raw_directory": str(raw_root),
            "input_directory": str(data_root),
            "output_directory": str(output),
            "downloads": downloads,
            "extracted_files": selected_extracted,
            "ignored_extracted_file_count": len(extracted) - len(selected_extracted),
            "allowlist_checksum_validation": checksum_validation,
            "input_candidates": inputs,
            "ignored_input_candidate_count": len(all_inputs) - len(inputs),
            "input_lineage": input_lineage,
            "archive_extractions": archive_extractions,
            "analysis_input_path": analysis_input,
            "execution_allowed": project.eligible,
            "cleanup_allowed": False,
            # WRITTEN HERE BECAUSE THIS IS WHERE IT HAS TO SURVIVE.
            #
            # The retention policy is chosen once, at download, and decides whether this unit's raw data
            # may ever be deleted. It used to be held only in the in-memory job registry, which is
            # persisted truncated to the hundred most recently updated jobs -- so at campaign scale the
            # policy was evicted by later work while the data it governed was still on disk, and
            # cleanup_download_lease's preview reported `manifest.get("raw_retention_policy")`, which
            # nothing had ever written, as None. A person asked to confirm an irreversible deletion was
            # shown a blank where the intent should be.
            #
            # The manifest is the unit's own durable record and outlives every registry.
            "raw_retention_policy": raw_retention_policy,
            "download_started_at": started_at,
            "download_completed_at": datetime.now(timezone.utc).isoformat(),
        }
        warnings = _archive_warnings(archive_extractions)
        if warnings:
            manifest["archive_warnings"] = warnings
        for key in ("previous_manifest", "superseded_manifests", "campaign_authorizations", "lease_owner"):
            if key in lease_record:
                manifest[key] = lease_record[key]
        repository_metadata_path = provenance / "repository-metadata.json"
        sample_metadata_path = provenance / "sample-metadata-extracted.json"
        from .repository_metadata import metadata_workspace

        _write_json(repository_metadata_path, project.as_dict())
        _write_json(sample_metadata_path, metadata_workspace(project.as_dict()))
        manifest["repository_metadata_file"] = str(repository_metadata_path)
        manifest["sample_metadata_file"] = str(sample_metadata_path)
        stages.finish("record", persist=False)
        manifest["lease_stages"] = [dict(entry) for entry in stages.records]
        _write_json(manifest_path, manifest)
    except BaseException as error:
        _record_download_failure(manifest_path, lease_record, downloads, error, stage=stages.fail(error))
        raise
    finally:
        # After the last write, success or failure, so the lease is never "not held" while its manifest
        # still says downloading.
        _release_lease(owner["lease_id"])
    return {**manifest, "manifest_path": str(manifest_path)}


# A manifest in either state records a lease that has not delivered its inputs. Nothing that needs
# input_candidates may start from one. discard_download_lease may act on a failed one, and on one still
# "downloading" whose owner is provably gone.
LEASE_INCOMPLETE_STATUSES = frozenset({"downloading", "download_failed"})
# A manifest in any other state is a record worth keeping, and a new lease copies it aside before
# replacing it. These hold nothing a new lease does not supersede, and are summarised only.
LEASE_REPLACEABLE_STATUSES = LEASE_INCOMPLETE_STATUSES | {"discarded"}
# How often a running lease refreshes download_progress_at in its manifest while bytes arrive.
LEASE_HEARTBEAT_SECONDS = 30.0

# The leases this process is running now, by lease_id. A lease that names this process and is not here
# is not running, whatever the process table says: the process is alive because it is this one.
_ACTIVE_LEASES: set[str] = set()
_ACTIVE_LEASES_GUARD = threading.Lock()


def _hold_lease(lease_id: str) -> None:
    with _ACTIVE_LEASES_GUARD:
        _ACTIVE_LEASES.add(lease_id)


def _release_lease(lease_id: str) -> None:
    with _ACTIVE_LEASES_GUARD:
        _ACTIVE_LEASES.discard(lease_id)


def _new_lease_owner(job_id: str) -> dict[str, Any]:
    """Who holds a lease: enough to find the process again, and to tell it from a later one."""
    return {
        "lease_id": secrets.token_hex(16),
        "job_id": str(job_id or ""),
        "pid": os.getpid(),
        # Windows reuses process ids; the creation time is what says the id still names this process.
        "process_created_at": process_created_at(),
        "host": socket.gethostname(),
    }


def lease_owner_state(manifest: dict[str, Any]) -> dict[str, Any]:
    """Whether the process holding a lease recorded as downloading is still there.

    ``state`` is one of:

    - alive: this process is running the lease, or the recorded process is running and has the recorded
      creation time;
    - gone: provably not running - the recorded process has exited, its id now names a process started
      later, or the lease names this process and this process is not running it;
    - unknown: anything else - no owner recorded, another host, or a process whose identity cannot be
      read. It must be treated as possibly alive.

    Read through OpenProcess or psutil (process_liveness), never os.kill: on Windows signal 0 is
    CTRL_C_EVENT, not a probe.
    """
    owner = manifest.get("lease_owner")
    evidence = {"lease_owner": owner, "last_heartbeat_at": manifest.get("download_progress_at")}
    if not isinstance(owner, dict):
        return {"state": "unknown", "reason": "The lease records no owner process.", **evidence}
    try:
        pid = int(owner.get("pid"))
    except (TypeError, ValueError):
        return {"state": "unknown", "reason": "The lease records no owner process id.", **evidence}
    host = str(owner.get("host") or "")
    if host and host.casefold() != socket.gethostname().casefold():
        return {
            "state": "unknown",
            "reason": f"The lease was taken on host {host}, whose processes cannot be read from here.",
            **evidence,
        }
    if pid == os.getpid():
        with _ACTIVE_LEASES_GUARD:
            running = str(owner.get("lease_id") or "") in _ACTIVE_LEASES
        if running:
            return {"state": "alive", "reason": "This process is running the lease.", **evidence}
        return {
            "state": "gone",
            "reason": (
                f"The lease names process {pid}, which is this process and is not running it: the lease "
                "ended without recording how, or was taken by an earlier process with the same id."
            ),
            **evidence,
        }
    recorded = owner.get("process_created_at")
    alive = process_is_alive(pid, recorded)
    if alive is False:
        reused = process_created_at(pid) is not None
        return {
            "state": "gone",
            "reason": (
                f"Process {pid}, which took the lease, has exited"
                + ("; its id now names a process started later." if reused else ".")
            ),
            **evidence,
        }
    if alive and recorded is not None and process_created_at(pid) is not None:
        return {"state": "alive", "reason": f"Process {pid}, which took the lease, is running.", **evidence}
    return {
        "state": "unknown",
        "reason": f"Process {pid} exists, but whether it is the one that took the lease cannot be read.",
        **evidence,
    }


def _beat_lease(manifest_path: Path, lease_id: str, stamp: str, received: int) -> None:
    """Refresh a running lease's heartbeat. Never raises, and never waits long: it must not stall bytes.

    Skipped when the manifest no longer records this lease as downloading.
    """
    try:
        with manifest_lock(manifest_path, timeout=5):
            current = read_manifest(manifest_path)
            if current.get("status") != "downloading" or (current.get("lease_owner") or {}).get(
                "lease_id"
            ) != lease_id:
                return
            current["download_progress_at"] = stamp
            current["download_received_bytes"] = received
            _write_json(manifest_path, current)
    except (OSError, ValueError, TypeError):
        pass


def _superseded_copy_path(manifest_path: Path) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = manifest_path.with_name(f"{manifest_path.stem}.superseded-{stamp}{manifest_path.suffix}")
    counter = 1
    while candidate.exists():
        candidate = manifest_path.with_name(
            f"{manifest_path.stem}.superseded-{stamp}-{counter}{manifest_path.suffix}"
        )
        counter += 1
    return candidate


def _supersede_previous_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Make room for a new lease's first write without losing what the workspace already records.

    Called under the manifest's writer lock. Returns the summary recorded as previous_manifest and the
    list recorded as superseded_manifests; both are empty when there is no manifest.

    WHY A COPY. The lease writes "downloading" before its first byte, so a retry that fails, or is killed,
    used to leave only that record where a finalised unit's had been: its status, its retained artifacts
    and their inventory, its finalised run, its failures and its campaign crossings were gone, and a
    summary of five fields was all that said they had existed. A manifest that is anything but an
    unfinished or discarded lease is therefore copied byte for byte to run-manifest.superseded-<UTC>.json
    first. The copies accumulate in superseded_manifests, carried from each record to the next, so a chain
    of failed retries never drops the pointer to the record they replaced. The copy sits in provenance,
    where finalisation keeps every file and raw cleanup removes none.

    Refused, before anything is written: a split parent, whose parts read its raw files in place and name
    its manifest, which must stay split_by_acquisition; and a workspace another live lease is still
    downloading into. Raises when the manifest is there but cannot be read, rather than replace it unseen.
    """
    try:
        data = _read_manifest_bytes(manifest_path)
    except FileNotFoundError:
        return {}, []
    summary: dict[str, Any] = {"sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
    try:
        previous = json.loads(data.decode("utf-8-sig"))
    except ValueError:
        previous = None
    if not isinstance(previous, dict):
        summary["readable"] = False
        previous = {}
    for key in ("status", "created_at", "finalized_at", "raw_cleaned_at", "discarded_at", "download_failed_at"):
        if previous.get(key):
            summary[key] = previous[key]
    superseded = [dict(item) for item in previous.get("superseded_manifests") or [] if isinstance(item, dict)]
    status = str(previous.get("status") or "")
    if status == SPLIT_PARENT_STATUS:
        parts = [
            str(item.get("analysis_unit_id") or item.get("manifest_path") or "")
            for item in previous.get("split_into") or []
            if isinstance(item, dict)
        ]
        raise ValueError(
            f"This unit was split by acquisition mode into {len(parts)} part(s) ({', '.join(parts)}), which "
            "read its raw files in place and name this manifest as their parent. A new lease would replace "
            "that record; the parent is not re-leased."
        )
    if status == "downloading":
        state = lease_owner_state(previous)
        summary["lease_owner"] = previous.get("lease_owner")
        summary["lease_owner_state"] = state["state"]
        if state["state"] == "alive":
            job = str((previous.get("lease_owner") or {}).get("job_id") or "") or "unrecorded"
            raise ValueError(
                f"Another lease (job {job}) is still downloading into this workspace: {state['reason']}"
            )
    if status not in LEASE_REPLACEABLE_STATUSES:
        copy = _superseded_copy_path(manifest_path)
        # Its own name, so no lock of its own; written whole or not at all, like every record here.
        _replace_atomically(copy, data)
        entry = {
            "path": str(copy),
            "sha256": summary["sha256"],
            "size_bytes": len(data),
            "status": status or None,
            "superseded_at": datetime.now(timezone.utc).isoformat(),
        }
        summary["superseded_copy"] = {"path": entry["path"], "sha256": entry["sha256"]}
        superseded.append(entry)
    return summary, superseded


def _record_download_failure(
    manifest_path: Path,
    lease_record: dict[str, Any],
    downloads: list[dict[str, Any]],
    error: BaseException,
    stage: str = "",
) -> None:
    """Say, in the unit's own manifest, that its lease did not finish and why. Never raises.

    The caller is already on its error path, and an exception here would replace the one that explains
    what went wrong. ``stage`` is the lease stage that failed; an archive that was refused also leaves
    its reason code and the members it refused (archive_failure).
    """
    record = dict(lease_record)
    record["status"] = "download_failed"
    record["execution_allowed"] = False
    record["cleanup_allowed"] = False
    # The objects that did arrive, so the bytes on disk are accounted for and a retry can be compared.
    record["downloads"] = list(downloads)
    record["download_failed_at"] = datetime.now(timezone.utc).isoformat()
    record["download_failure"] = {
        "reason": str(error) or type(error).__name__,
        "error_type": type(error).__name__,
        "objects_completed": len(downloads),
        "objects_declared": len(
            {str(item.get("url") or "") for item in (record.get("project") or {}).get("files") or []}
        ),
    }
    if stage:
        record["download_failure"]["stage"] = stage
    if isinstance(error, ArchiveError):
        failure = error.record()
        failure["rejected_members"] = failure["rejected_members"][:50]
        record["download_failure"]["archive_failure"] = failure
    try:
        _write_json(manifest_path, record)
    except (OSError, ValueError, TypeError):
        pass


# The stages of a download lease, in the order they run. materialise (the accession download store) and
# convert (mzXML to mzML) are recorded as not_used until they are wired in; they sit where they will run.
LEASE_STAGES = (
    "fetch", "verify_declared_checksums", "extract", "materialise", "convert", "discover", "attribute",
    "record",
)


class _LeaseStages:
    """The lease's stages as its manifest records them, written as each one finishes.

    Each entry is {stage, status, started_at, finished_at, ...counts}. status is running, completed,
    failed, interrupted or not_used; a not_used entry says why instead of when. fetch and
    verify_declared_checksums run object by object, side by side, so the lease says which of them it
    is in (at); when it fails, that one is failed and the other interrupted.

    The writes are the lease's own keys only (lease_stages, downloads, archive_extractions),
    read-modified-written under the manifest's lock like the heartbeat, and never raise: a stage that
    cannot be recorded must not stop the bytes, and the final write or the failure record says the rest.
    """

    def __init__(self, manifest_path: Path, lease_record: dict[str, Any], lease_id: str) -> None:
        self.manifest_path = manifest_path
        self.lease_record = lease_record
        self.lease_id = lease_id
        self.records: list[dict[str, Any]] = lease_record.setdefault("lease_stages", [])
        self.current = ""

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _entry(self, name: str) -> dict[str, Any]:
        for entry in reversed(self.records):
            if entry["stage"] == name:
                return entry
        raise KeyError(name)

    def start(self, name: str) -> None:
        self.records.append({"stage": name, "status": "running", "started_at": self._now()})
        self.current = name

    def at(self, name: str) -> None:
        """Say which of two stages running side by side the lease is in now."""
        self.current = name

    def finish(self, name: str, *, persist: bool = True, **summary: Any) -> None:
        entry = self._entry(name)
        entry.update(summary)
        entry["status"] = "completed"
        entry["finished_at"] = self._now()
        if persist:
            self.persist()

    def not_used(self, name: str, reason: str) -> None:
        self.records.append({"stage": name, "status": "not_used", "reason": reason})
        self.persist()

    def fail(self, error: BaseException) -> str:
        """Mark the stage the lease was in as failed, and any other still running as interrupted.

        Returns the failed stage: the one the lease was in, or the last recorded when none was running
        (the final write itself).
        """
        running = [entry for entry in self.records if entry["status"] == "running"]
        names = [entry["stage"] for entry in running]
        failed = self.current if self.current in names else (names[-1] if names else "")
        for entry in running:
            entry["status"] = "failed" if entry["stage"] == failed else "interrupted"
            entry["finished_at"] = self._now()
            if entry["stage"] == failed:
                entry["error"] = str(error) or type(error).__name__
        if not failed and self.records:
            failed = self.records[-1]["stage"]
        return failed

    def persist(self) -> None:
        try:
            with manifest_lock(self.manifest_path, timeout=5):
                current = read_manifest(self.manifest_path)
                if current.get("status") != "downloading" or (current.get("lease_owner") or {}).get(
                    "lease_id"
                ) != self.lease_id:
                    return
                for key in ("lease_stages", "downloads", "archive_extractions"):
                    current[key] = self.lease_record.get(key)
                _write_json(self.manifest_path, current)
        except (OSError, ValueError, TypeError):
            pass


def _route_object(
    project: RepositoryProject, url: str, item: RepositoryFile, index: int
) -> tuple[str, str, str]:
    """(file name, archive kind by name or '', placement) for one repository object.

    The name is the URL's basename, or <accession>.tar for MB-POST's one project tar; a URL with no
    basename takes the listed name. When the URL names no archive but the unit's own listing names
    this object as one (a download link without a file name), the listed name is used, so the object
    is opened as what the repository says it is.

    A file inside a vendor folder (raw/x.d/..., raw/x.raw/...) is never an archive to open, whatever
    its name: unpacking it would take it out of the folder its reader expects it in.

    placement is where, under the data root, the archive expands. A per-sample container (X.raw.zip)
    or a single compressed file (x.mzML.gz) expands in the directory it was listed in, as the
    container it stands for (archives.container_alias); any other archive is a bundle whose members
    keep their own relative paths at the data root, as they always have.
    """
    listed = str(item.name or "").replace("\\", "/")
    listed_name = PurePosixPath(listed).name
    filename = (
        Path(urllib.parse.urlparse(url).path).name or listed_name or f"{project.accession}_{index}.zip"
    )
    if project.repository == "mb_post":
        filename = f"{project.accession}.tar"
    if listed and _inside_vendor_folder(listed):
        return filename, "", ""
    kind = archives.archive_kind_from_name(filename)
    if not kind and project.repository != "mb_post" and archives.archive_kind_from_name(listed_name):
        filename, kind = listed_name, archives.archive_kind_from_name(listed_name)
    if not kind:
        return filename, "", ""
    placement = ""
    names_this_object = bool(listed_name) and listed_name.casefold() == filename.casefold()
    if names_this_object and (archives.container_alias(filename) or kind in archives.STREAM_KINDS):
        placement = _safe_relative_name(listed).parent.as_posix()
        placement = "" if placement == "." else placement
    return filename, kind, placement


def _inside_vendor_folder(name: str) -> bool:
    parts = [part for part in str(name).replace("\\", "/").split("/") if part]
    return any(part.casefold().endswith(archives.FOLDER_CONTAINER_SUFFIXES) for part in parts[:-1])


def _archive_download_path(download_root: Path, filename: str, index: int, taken: set[str]) -> Path:
    """Where an archive is downloaded to: raw\\downloads\\<name>, never over another of this lease's.

    Two per-sample archives listed in different directories can share a name (pos/S1.raw.zip and
    neg/S1.raw.zip); the second goes to downloads\\<object number>\\<name>. The order of a unit's
    objects is its listing's, so a retry puts each object where the first attempt did.
    """
    destination = download_root / filename
    if str(destination).casefold() in taken:
        destination = download_root / str(index) / filename
    taken.add(str(destination).casefold())
    return destination


def _verify_object_checksum(
    result: dict[str, Any], item: RepositoryFile, repository: str, filename: str
) -> str:
    """Compare one downloaded object with its published MD5; raises on a mismatch.

    Returns md5_verified, not_declared or not_compared. Only an MD5 is compared here, because the
    download computes MD5 and SHA-256 as the bytes arrive and a 32-digit value is what the repositories
    publish for objects. MB-POST's declared values belong to the files inside its project tar, so they
    are compared after extraction, as is any SHA-1 or SHA-256 (_verify_project_allowlist_checksums).
    """
    declared = str(item.checksum or "").strip()
    if not declared:
        return "not_declared"
    if repository != "mb_post" and re.fullmatch(r"[0-9a-fA-F]{32}", declared):
        if result["md5"].casefold() != declared.casefold():
            raise ValueError(f"MD5 checksum mismatch for {filename}.")
        result["declared_checksum_verified"] = True
        result["declared_checksum_algorithm"] = "md5"
        return "md5_verified"
    return "not_compared"


def build_input_lineage(
    inputs: list[str],
    downloads: list[dict[str, Any]],
    extracted_from: dict[str, dict[str, Any]],
    data_root: Path,
    download_root: Path,
    project: RepositoryProject,
    verified_checksums: dict[str, dict[str, Any]] | None = None,
    *,
    extracted_members: dict[str, dict[str, Any]] | None = None,
    archive_extractions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One row per analysis input: what it is, where its bytes came from, and what vouches for them.

    WHY ONE TABLE. Every later reader of an input asks the same question in a different place - the
    gate's checksum coverage, the analysis-CSV builder, the conversion step, the archive checks - and each
    was about to answer it from its own reading of downloads, extracted_files and the allow-list. One
    record, written where the facts are known, is what they read instead.

    Kinds, decided by shape and by how the bytes arrived:

    - file: a file downloaded as its own repository object;
    - vendor_folder: a .d/.raw directory assembled from objects downloaded one by one;
    - archived_container: a .d/.raw directory that came out of an archive;
    - extracted_member: a file that came out of an archive;
    - converted: written by a conversion step, which records its own rows (none exist yet).

    An input this lease neither downloaded nor extracted - a file already in a reused workspace - keeps
    its shape's kind and says so in its source, rather than borrowing another object's checksums.

    A member of an archive is not hashed here; the archive's checksums, and whether the published one was
    compared, are carried as its source. ``sample_id`` is filled when exactly one of the unit's samples
    names the input; ``file_name`` is the analysis CSV's, and belongs to whatever writes that CSV.

    ``verified_checksums`` is what _verify_project_allowlist_checksums compared, by _file_key: a file or
    an extracted member whose own declared md5, sha1 or sha256 matched records it as declared,
    declared_algorithm and declared_verified. Without it a checked input read as unchecked.

    AN INPUT THAT CAME OUT OF AN ARCHIVE CARRIES ITS BASIS (_archive_basis): the archive whose published
    checksum matched, or the member's own, and the row of the archive's member listing that accounts for
    it (``extracted_members`` and ``archive_extractions``, from the lease's extract stage). A listed
    container archive (X.raw.zip) names its container (X.raw) for declared_names and sample_id.
    """
    verified_checksums = verified_checksums or {}
    extracted_members = extracted_members or {}
    archive_extractions = archive_extractions or []
    data_key = _file_key(str(data_root))
    direct: dict[str, dict[str, Any]] = {}
    for item in downloads:
        path = Path(str(item.get("path") or ""))
        if str(item.get("path") or "") and path.parent != download_root and not item.get("archive"):
            direct[_file_key(str(path))] = item

    declared: dict[str, str] = {}
    for item in project.files:
        if item.name:
            try:
                name = _safe_relative_name(item.name).as_posix().casefold()
            except ValueError:
                continue
            declared.setdefault(name, item.name)
            alias = _container_alias_path(name)
            if alias:
                declared.setdefault(alias, item.name)
    sample_names: dict[str, set[str]] = {}
    for sample in project.sample_metadata or []:
        raw = PurePosixPath(str((sample or {}).get("raw_file") or "").replace("\\", "/")).name.casefold()
        sample_id = str((sample or {}).get("sample_id") or "").strip()
        if raw and sample_id:
            sample_names.setdefault(raw, set()).add(sample_id)
            alias = archives.container_alias(raw).casefold()
            if alias:
                sample_names.setdefault(alias, set()).add(sample_id)

    folders = {_file_key(item): item for item in inputs if Path(item).is_dir()}
    # Each downloaded or extracted file is attributed to the input folder that encloses it by walking up
    # its own parents, so the cost grows with the number of files, not files times folders.
    folder_members: dict[str, list[tuple[str, dict[str, Any], str]]] = {key: [] for key in folders}
    if folders:
        for origin, entries in (("download", direct), ("archive", extracted_from)):
            for member_key, entry in entries.items():
                for parent in Path(member_key).parents:
                    parent_key = str(parent).casefold()
                    if parent_key in folder_members:
                        folder_members[parent_key].append((member_key, entry, origin))
                        break
                    if parent_key == data_key or len(parent_key) < len(data_key):
                        break

    rows = []
    for text in inputs:
        path = Path(text)
        key = _file_key(text)
        try:
            relative = path.resolve().relative_to(data_root.resolve()).as_posix()
        except ValueError:
            relative = ""
        candidates = {relative.casefold()} if relative else set()
        if relative.casefold().startswith("files/"):
            candidates.add(relative.casefold()[6:])
        parts = relative.casefold().split("/") if relative else []
        if len(parts) > 1:
            candidates.add("/".join(parts[1:]))
        base = path.name.casefold()
        matched_samples = sample_names.get(base) or sample_names.get(PurePosixPath(base).stem) or set()
        row: dict[str, Any] = {
            "path": str(path),
            "kind": "",
            "declared_names": sorted({declared[item] for item in candidates if item in declared}),
            "sample_id": next(iter(matched_samples)) if len(matched_samples) == 1 else "",
            "file_name": "",
            "source": {},
            "checksums": {},
        }
        if key in folder_members:
            members = folder_members[key]
            sources = {
                str(entry.get("path") or ""): entry for _, entry, origin in members if origin == "archive"
            }
            row["kind"] = "archived_container" if sources else "vendor_folder"
            downloaded = sorted(
                (
                    (Path(member).relative_to(Path(key)).as_posix(), entry)
                    for member, entry, origin in members
                    if origin == "download"
                ),
                key=lambda pair: pair[0],
            )
            if sources:
                row["source"] = {"archives": [_archive_source(entry) for entry in sources.values()]}
                # The archives its files came out of: the downloads, or the per-sample archives inside
                # a project tar that the files were expanded from.
                vouching: dict[str, dict[str, Any]] = {}
                for member, entry, origin in members:
                    if origin == "archive":
                        named = _vouching_archive(extracted_members.get(member), archive_extractions, entry)
                        where = named.get("archive_path") or named.get("download_path") or ""
                        vouching.setdefault(where, named)
                row["basis"] = _archive_basis(
                    list(vouching.values()),
                    listing=[
                        _container_listing(record, path)
                        for record in archive_extractions
                        if str(record.get("download_path") or "") in sources
                    ],
                )
            elif downloaded:
                row["source"] = {"objects": len(downloaded)}
            else:
                row["source"] = {"origin": "not_downloaded_by_this_lease"}
            if downloaded:
                # A digest over the member objects' own sha256, so a folder assembled from many downloads
                # has one checksum that changes when any member does.
                lines = "".join(
                    f"{relative_name}\t{entry.get('size_bytes', 0)}\t{entry.get('sha256', '')}\n"
                    for relative_name, entry in downloaded
                )
                row["checksums"] = {
                    "member_objects": len(downloaded),
                    "members_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
                }
        elif key in direct:
            entry = direct[key]
            row["kind"] = "file"
            row["source"] = {"url": entry.get("source_url", ""), "download_path": entry.get("path", "")}
            row["checksums"] = {
                "sha256": entry.get("sha256", ""),
                "md5": entry.get("md5", ""),
                "declared": entry.get("declared_checksum", ""),
                # The download loop compares only a 32-digit declared value, and only against the md5.
                "declared_algorithm": "md5" if entry.get("declared_checksum_verified") else "",
                "declared_verified": True if entry.get("declared_checksum_verified") else None,
            }
            if key in verified_checksums:
                row["checksums"].update(_declared_verification(verified_checksums[key]))
        elif key in extracted_from:
            row["kind"] = "extracted_member"
            try:
                member = path.resolve().relative_to(data_root.resolve()).as_posix()
            except ValueError:
                member = path.name
            row["source"] = {"archive": _archive_source(extracted_from[key]), "member": member}
            if key in verified_checksums:
                # The member's own declared checksum, compared against its extracted bytes. The archive's
                # checksum, verified or not, stays with the archive in the source.
                row["checksums"] = _declared_verification(verified_checksums[key])
            listed = extracted_members.get(key)
            row["basis"] = _archive_basis(
                [_vouching_archive(listed, archive_extractions, extracted_from[key])],
                member_checksums=row["checksums"],
                listing=[_member_listing(archive_extractions, listed)] if listed else [],
            )
        else:
            row["kind"] = "vendor_folder" if path.is_dir() else "file"
            row["source"] = {"origin": "not_downloaded_by_this_lease"}
        rows.append(row)
    return {"schema": "msdial-input-lineage.v1", "rows": rows}


def _declared_verification(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "declared": str(result.get("declared") or ""),
        "declared_algorithm": str(result.get("declared_algorithm") or ""),
        "declared_verified": True if result.get("verified") else None,
    }


def _archive_source(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "url": entry.get("source_url", ""),
        "download_path": entry.get("path", ""),
        "sha256": entry.get("sha256", ""),
        "md5": entry.get("md5", ""),
        "declared_checksum": entry.get("declared_checksum", ""),
        "declared_checksum_verified": True if entry.get("declared_checksum_verified") else None,
    }


# How a publication may describe an input by its basis. The gate's SUM-2 reads these phrasings: an
# input that came out of an archive was never compared with a checksum of its own, so it is "extracted
# from an archive whose published MD5 matched" and never "checksum-verified".
ARCHIVE_BASIS_STATEMENTS = {
    "archive_declared_checksum": "extracted from an archive whose published {algorithm} matched",
    "member_declared_checksum": "extracted from an archive, and its own published {algorithm} matched",
    "archive_download_hash": (
        "extracted from an archive for which no checksum was published, identified only by its SHA-256"
    ),
}


def _download_archive(entry: dict[str, Any]) -> dict[str, Any]:
    """A downloaded archive as a basis names it."""
    return {
        "download_path": str(entry.get("path") or ""),
        "url": str(entry.get("source_url") or ""),
        "sha256": str(entry.get("sha256") or ""),
        "md5": str(entry.get("md5") or ""),
        "declared": str(entry.get("declared_checksum") or ""),
        "declared_algorithm": str(entry.get("declared_checksum_algorithm") or ""),
        "declared_verified": True if entry.get("declared_checksum_verified") else None,
    }


def _expanded_archive(record: dict[str, Any], nested: dict[str, Any]) -> dict[str, Any]:
    """An archive that was inside a downloaded one, as a basis names it: where it was, and its digests."""
    parts = (str(record.get("placement") or ""), str(nested.get("archive_path") or ""))
    return {
        "archive_path": "/".join(part for part in parts if part),
        "inside": str(record.get("download_path") or ""),
        "sha256": str(nested.get("archive_sha256") or ""),
        "md5": str(nested.get("archive_md5") or ""),
        "declared": str(nested.get("declared_checksum") or ""),
        "declared_algorithm": str(nested.get("declared_checksum_algorithm") or ""),
        "declared_verified": True if nested.get("declared_checksum_verified") else None,
    }


def _vouching_archive(
    listed: dict[str, Any] | None, records: list[dict[str, Any]], download: dict[str, Any]
) -> dict[str, Any]:
    """The innermost archive a file came out of: a nested one the listing names, else the download."""
    if listed:
        index = int(listed.get("extraction", -1))
        record = records[index] if 0 <= index < len(records) else {}
        label = str(listed.get("archive") or "")
        for nested in _nested_records(record):
            if str(nested.get("archive_path") or "") == label:
                return _expanded_archive(record, nested)
    return _download_archive(download)


def _archive_basis(
    archives_named: list[dict[str, Any]],
    *,
    member_checksums: dict[str, Any] | None = None,
    listing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What vouches for an input that came out of an archive, strongest first.

    - member_declared_checksum: the file's own published checksum matched its extracted bytes (MB-POST
      publishes one per file inside its project tar);
    - archive_declared_checksum: the published checksum of the archive it came out of matched that
      archive, as downloaded or, for an archive inside another, before it expanded; and the input is in
      the listing of what came out of it. value is that verified checksum;
    - archive_download_hash: the archive published none; the SHA-256 computed at download (or, for an
      archive inside another, before it expanded) identifies it, and nothing more.

    archives_named are the archives it came out of (_download_archive, _expanded_archive): one for a
    file, one or more for a container. listing names the members-TSV row (or the container) that
    accounts for the input, so a reader can trace the input to the listing and the listing to the
    archive without re-reading the raw tree.
    """
    basis: dict[str, Any] = {"archives": list(archives_named), "listing": list(listing or [])}
    if member_checksums and member_checksums.get("declared_verified"):
        algorithm = str(member_checksums.get("declared_algorithm") or "")
        basis.update(kind="member_declared_checksum", algorithm=algorithm,
                     value=str(member_checksums.get("declared") or ""), verified=True)
    elif archives_named and all(item["declared_verified"] for item in archives_named):
        algorithm = archives_named[0]["declared_algorithm"] or "md5"
        value = archives_named[0].get(algorithm) or archives_named[0]["declared"]
        basis.update(kind="archive_declared_checksum", algorithm=algorithm, value=value, verified=True)
    else:
        basis.update(kind="archive_download_hash", algorithm="sha256",
                     value=archives_named[0]["sha256"] if archives_named else "", verified=None)
    algorithm_name = {"md5": "MD5", "sha1": "SHA-1", "sha256": "SHA-256"}.get(
        basis["algorithm"], basis["algorithm"].upper()
    )
    basis["statement"] = ARCHIVE_BASIS_STATEMENTS[basis["kind"]].format(algorithm=algorithm_name)
    return basis


def _listing_reference(record: dict[str, Any]) -> dict[str, Any]:
    tsv = record.get("members_tsv") or {}
    return {
        "archive_name": str(record.get("archive_name") or ""),
        "archive_sha256": str(record.get("archive_sha256") or ""),
        "members_tsv": str(tsv.get("path") or ""),
        "members_tsv_sha256": str(tsv.get("sha256") or ""),
    }


def _member_listing(records: list[dict[str, Any]], listed: dict[str, Any]) -> dict[str, Any]:
    """The listing row for one extracted file: its path in the listing and the archive it came out of."""
    index = int(listed.get("extraction", -1))
    record = records[index] if 0 <= index < len(records) else {}
    return {
        **_listing_reference(record),
        "member": str(listed.get("member") or ""),
        # The archive the listing names for the row: the outer one, or the nested one it expanded.
        "listed_archive": str(listed.get("archive") or ""),
    }


def _container_listing(record: dict[str, Any], container: Path) -> dict[str, Any]:
    """The listing that accounts for a container: the listing, and the container's path in it."""
    try:
        member = container.relative_to(Path(str(record.get("destination") or ""))).as_posix()
    except ValueError:
        member = ""
    return {**_listing_reference(record), "container": member}


def input_integrity_statement(manifest: dict[str, Any] | None) -> str:
    """One sentence for the methods text on what vouches for inputs that came out of archives.

    Empty for a unit none of whose inputs did, and for a manifest that has no input_lineage, so every
    publication written before this, and every per-file unit, reads as it did.
    """
    rows = [
        row for row in ((manifest or {}).get("input_lineage") or {}).get("rows") or []
        if isinstance(row, dict)
    ]
    counted: dict[str, int] = {}
    for row in rows:
        basis = row.get("basis") if isinstance(row.get("basis"), dict) else None
        if basis and basis.get("statement"):
            counted[str(basis["statement"])] = counted.get(str(basis["statement"]), 0) + 1
    if not counted:
        return ""
    clauses = [
        f"{count} {'was' if count == 1 else 'were'} {statement}"
        for statement, count in sorted(counted.items(), key=lambda item: (-item[1], item[0]))
    ]
    joined = clauses[0] if len(clauses) == 1 else ", ".join(clauses[:-1]) + " and " + clauses[-1]
    return f"Of the {len(rows)} analysis input{'s' if len(rows) != 1 else ''}, {joined}."


def load_unit_manifest(manifest_path: str | Path) -> dict[str, Any]:
    """A unit's run manifest, read directly, for the steps that used to need its download job.

    WHY. Preflight, split, preparation, the diagnostic estimate, QA and publication each found their unit
    through a completed job in the backend's registry, which is persisted truncated to its hundred newest
    jobs and forgets running ones on restart. At campaign scale a unit's download job is evicted long
    before its later steps run, and the unit became unreachable although its manifest sat on disk. The
    manifest is the durable record, so it is accepted in place of the job.

    What the job check guaranteed is kept: a lease that has not delivered its inputs - still downloading,
    or failed - is refused here exactly as an incomplete download job was.
    """
    path = Path(str(manifest_path or "")).expanduser()
    if not str(manifest_path or "").strip() or not path.is_file():
        raise FileNotFoundError(f"Repository run manifest was not found: {path}")
    path = path.resolve()
    manifest = read_manifest(path)
    if not isinstance(manifest.get("project"), dict):
        raise ValueError(f"{path} is not a repository run manifest: it records no project.")
    status = str(manifest.get("status") or "")
    if status in LEASE_INCOMPLETE_STATUSES:
        reason = (manifest.get("download_failure") or {}).get("reason") or "unrecorded"
        raise ValueError(
            f"The repository download for this unit is {status}, so it has no analysis inputs yet. "
            + (
                f"Recorded reason: {reason}."
                if status == "download_failed"
                else "Wait for the lease to finish."
            )
        )
    manifest["manifest_path"] = str(path)
    return manifest


def record_campaign_authorization(manifest_path: str | Path, crossing: dict[str, Any]) -> dict[str, Any]:
    """Append one boundary crossing made under a campaign approval to the unit's manifest.

    Raises when it cannot write, unlike the failure recorders: the crossing is written before the step it
    authorizes, and a step whose authority cannot be recorded does not run.
    """
    entry = dict(crossing)

    def change(manifest: dict[str, Any]) -> None:
        manifest["campaign_authorizations"] = [*(manifest.get("campaign_authorizations") or []), entry]

    return update_manifest(Path(manifest_path), change)


def record_run_failure(
    manifest_path: Path,
    reason: str,
    exit_code: int | None = None,
    log_tail: list[str] | None = None,
) -> dict[str, Any]:
    """Write a failed MS-DIAL run into the analysis unit's own manifest.

    WHAT THIS ENDS. A failed run recorded its status and its diagnosed error in the in-memory JOBS
    registry and nowhere else. The registry is persisted truncated to the hundred most recently
    updated jobs, so at the scale this programme is for -- each accession consuming a download job,
    a tuning job and one or more run jobs -- the record of a failure was evicted by later work, and
    the unit's own workspace looked exactly like a unit nobody had tried.

    The project contract requires "a failure record when unsuccessful" for every attempted unit. It
    existed only as prose instructing an agent to write one, which is the defect shape this
    programme is built against: something judged that is never connected to what actually ran.

    CLEANUP STAYS FORBIDDEN. cleanup_allowed is set false explicitly rather than left alone, because
    the raw data is what a retry needs and a failed run is exactly when someone is tempted to
    reclaim the disk. The campaign's retention policy is now "delete after a successful run"; this
    is the clause that keeps "successful" in it.

    Never raises. A failure while recording a failure would lose both, so any problem writing the
    manifest is returned rather than thrown -- the caller is already on its error path.
    """
    record = {
        "reason": reason,
        "exit_code": exit_code,
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
        # The last lines rather than the whole log: enough to tell a missing library from a crash,
        # small enough that a manifest stays readable. The full log lives with the job while it
        # survives.
        "log_tail": [str(line) for line in (log_tail or [])][-40:],
    }
    def change(manifest: dict[str, Any]) -> None:
        manifest["status"] = "run_failed"
        manifest["cleanup_allowed"] = False
        failures = list(manifest.get("run_failures") or [])
        failures.append(record)
        # Appended, not replaced: a unit retried three times and failed three times is a different
        # thing from a unit tried once, and the difference is what says whether to keep trying.
        manifest["run_failures"] = failures

    try:
        manifest_path = Path(manifest_path).resolve()
        manifest = update_manifest(manifest_path, change)
        return {**manifest, "manifest_path": str(manifest_path)}
    except (OSError, ValueError) as error:
        return {"status": "run_failed", "manifest_error": str(error), "run_failure": record}


def record_run_start(
    manifest_path: str | Path,
    job_id: str,
    kind: str = "run",
    *,
    output_directory: str | Path = "",
    console: dict[str, Any] | None = None,
    command: list[str] | None = None,
    timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
) -> dict[str, Any]:
    """Open one run attempt in the unit's manifest, before its Console starts. Never raises.

    WHY BEFORE. Every other record of a run is written after the Console returns: the failure record,
    the finalised run. A backend that stops while the Console runs - a reboot, a crash, an MCP session
    that ends - writes neither, and the unit then looked exactly like a unit nobody had started, while a
    Console might still be writing into it. An attempt opened here and closed by record_run_end says a
    run started, which job started it, from which backend process and with which Console; one left open
    says it never came back. The Console's process id is added by record_run_process once it exists,
    which is what lets a later caller tell an orphaned Console that is still running from one that died.

    Appended to run_attempts, never replaced: a unit retried twice is a different thing from a unit tried
    once. ``attempt`` numbers the attempts of one kind (run, tuning) in the order they were made.

    Returns the entry with recorded=True, or with recorded=False and the error when the manifest could not
    be written; the run goes ahead either way, since a record that cannot be written is not a reason to
    lose the run. The Console is identified by its version and checksums, not by its location.
    """
    identity = console or {}
    entry: dict[str, Any] = {
        "attempt_id": secrets.token_hex(8),
        "attempt": None,
        "job_id": str(job_id or ""),
        "kind": str(kind or "run"),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "ended_at": None,
        "exit_code": None,
        "reason": None,
        "console": {
            "version": str(identity.get("version") or ""),
            "binary_sha256": str(identity.get("binary_sha256") or ""),
            "assembly_sha256": str(identity.get("assembly_sha256") or ""),
            "provenance_status": str(identity.get("status") or identity.get("provenance_status") or ""),
        },
        "command_sha256": (
            hashlib.sha256(json.dumps([str(part) for part in command]).encode("utf-8")).hexdigest()
            if command
            else ""
        ),
        "output_directory": str(output_directory or ""),
        "timeout_seconds": timeout_seconds,
        "idle_timeout_seconds": idle_timeout_seconds,
        # The process that starts the Console, identified the way a download lease's owner is.
        "backend": {
            "pid": os.getpid(),
            "process_created_at": process_created_at(),
            "host": socket.gethostname(),
        },
        "console_pid": None,
        "console_process_created_at": None,
    }

    def change(manifest: dict[str, Any]) -> None:
        attempts = list(manifest.get("run_attempts") or [])
        entry["attempt"] = 1 + sum(
            1 for item in attempts if isinstance(item, dict) and item.get("kind") == entry["kind"]
        )
        manifest["run_attempts"] = [*attempts, entry]

    try:
        update_manifest(Path(manifest_path), change)
        return {**entry, "recorded": True}
    except Exception as error:  # noqa: BLE001 - a record that fails must not stop the run it records
        return {**entry, "recorded": False, "error": f"{type(error).__name__}: {error}"}


def record_run_process(
    manifest_path: str | Path, attempt_id: str, pid: int, process_created_at: float | None = None
) -> dict[str, Any]:
    """Add the Console's process id to an open run attempt, once the process exists. Never raises."""

    def change(manifest: dict[str, Any]) -> None:
        for item in manifest.get("run_attempts") or []:
            if isinstance(item, dict) and item.get("attempt_id") == attempt_id:
                item["console_pid"] = int(pid)
                item["console_process_created_at"] = process_created_at
                return
        raise LookupError(f"run attempt {attempt_id} is not in the manifest")

    try:
        update_manifest(Path(manifest_path), change)
        return {"recorded": True}
    except Exception as error:  # noqa: BLE001
        return {"recorded": False, "error": f"{type(error).__name__}: {error}"}


def record_run_end(
    manifest_path: str | Path,
    attempt: dict[str, Any] | None,
    exit_code: int | None,
    reason: str,
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Close the run attempt record_run_start opened, with how the Console ended. Never raises.

    ``attempt`` is what record_run_start returned. ``reason`` is exited (the Console ended on its own,
    and ``exit_code`` says how), timeout, idle_timeout, cancelled, sciex_scan_sidecar, start_failed (it
    never started) or error; ``detail`` holds the rest, such as how a stop was made. When the opening
    record is missing - it could not be written, or a new lease replaced the manifest meanwhile - the
    attempt is appended closed and marked start_unrecorded, so the end of a run is not lost with its start.
    """
    opened = dict(attempt or {})
    ended = {
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "exit_code": exit_code,
        "reason": str(reason or ""),
        **({"detail": dict(detail)} if detail else {}),
    }

    def change(manifest: dict[str, Any]) -> None:
        attempts = list(manifest.get("run_attempts") or [])
        for item in attempts:
            if (
                isinstance(item, dict)
                and opened.get("attempt_id")
                and item.get("attempt_id") == opened.get("attempt_id")
            ):
                item.update(ended)
                return
        closed = {key: value for key, value in opened.items() if key not in {"recorded", "error"}}
        closed.update(ended)
        closed["start_unrecorded"] = True
        closed["attempt"] = 1 + sum(
            1 for item in attempts if isinstance(item, dict) and item.get("kind") == closed.get("kind")
        )
        manifest["run_attempts"] = [*attempts, closed]

    try:
        update_manifest(Path(manifest_path), change)
        return {"recorded": True, **ended}
    except Exception as error:  # noqa: BLE001
        return {"recorded": False, "error": f"{type(error).__name__}: {error}", **ended}


def live_run_attempt(
    manifest_path: str | Path, ignore_backend_pid: int | None = None
) -> dict[str, Any] | None:
    """The unit's run attempt whose Console may still be running, or None.

    Read from the manifest, so it answers for Consoles no registry knows any more: one orphaned by a
    backend that stopped, or one started by another backend process. An attempt counts when its Console
    is alive (read through process_liveness, never by signalling it), when it is still open and its
    Console's liveness cannot be read, or when it is still open, names no Console yet, and the backend
    that opened it is another process that is still alive. ``ignore_backend_pid`` is the caller's own
    process id: its registry answers for the attempts it opened itself.
    """
    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    for item in reversed(list(manifest.get("run_attempts") or [])[-20:]):
        if not isinstance(item, dict):
            continue
        still_open = not item.get("ended_at")
        pid = item.get("console_pid")
        if pid:
            created = item.get("console_process_created_at")
            if not still_open and created is None:
                # Closed, and without a creation time a later process given the same id looks the same.
                continue
            alive = process_is_alive(pid, created)
            if alive or (alive is None and still_open):
                return item
            continue
        if not still_open:
            continue
        backend = item.get("backend") or {}
        if ignore_backend_pid is not None and backend.get("pid") == ignore_backend_pid:
            continue
        if process_is_alive(backend.get("pid"), backend.get("process_created_at")):
            return item
    return None


def record_peak_height_diagnostic(
    manifest_path: Path,
    estimate: dict[str, Any],
    representative: dict[str, Any] | None = None,
    job_id: str = "",
    diagnostic_directory: str = "",
) -> dict[str, Any]:
    """Write a peak-count diagnostic into the analysis unit's own manifest.

    WHAT THIS ENDS. The project contract requires the zero-threshold diagnostic before every
    production repository run, and requires "the method, representative sample, diagnostic count,
    threshold step, and accepted threshold" to be retained in provenance. All five were computed and
    none of them reached the workspace: the estimate went into the HTTP response and into the
    in-memory JOBS registry, which is persisted truncated to the hundred most recently updated jobs,
    so at campaign scale the measurement behind every threshold was evicted while the run it
    justified was still on disk.

    What survived was the number alone, carried by hand into the production answers. An audit
    reading the retained artifacts could see that a run used 500 and could not see whether 500 had
    ever been measured on this unit, on a different unit, or at all. That is the defect shape this
    programme is built against, in the one place the contract names explicitly.

    Appended rather than replaced. Re-running the diagnostic with a different step or a different
    representative is a normal thing to do, and which thresholds were considered is part of why the
    accepted one was accepted.

    Never raises. The diagnostic's own result is already in the caller's hands, and losing the
    record must not also lose the estimate.
    """
    record = {
        "recorded_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "job_id": str(job_id or ""),
        "diagnostic_run_directory": str(diagnostic_directory or ""),
        # The representative sample: which file the threshold was measured on, and why that one.
        # A threshold measured on a blank is a different fact from one measured on the QC nearest
        # the analytical-order midpoint, and only the record can tell them apart afterwards.
        "representative": dict(representative or {}),
        # Every field the estimator produced, unedited. Selecting fields here is how a later change
        # to the estimator silently stops being recorded.
        "estimate": dict(estimate or {}),
        "minimum_peak_height": estimate.get("minimum_peak_height"),
        "diagnostic_peak_count": estimate.get("diagnostic_peak_count"),
        "estimated_peak_count": estimate.get("estimated_peak_count"),
        "threshold_step": estimate.get("threshold_step"),
        "method": estimate.get("method", ""),
    }

    def change(manifest: dict[str, Any]) -> None:
        manifest["peak_height_diagnostics"] = [*(manifest.get("peak_height_diagnostics") or []), record]

    try:
        manifest_path = Path(manifest_path).resolve()
        update_manifest(manifest_path, change)
        return {"recorded": True, "manifest_path": str(manifest_path), "diagnostic": record}
    except (OSError, ValueError) as error:
        return {"recorded": False, "manifest_error": str(error), "diagnostic": record}


def _retained_result_paths(
    output: Path, provenance: Path, manifest_path: Path
) -> tuple[list[Path], list[Path]]:
    """The output files and the provenance files a validated run keeps.

    MS-DIAL's per-file and alignment containers, moved out of the raw tree after the run
    (run_finalisation.relocate_intermediates), are kept whatever their names: the raw data they sat beside
    are deleted, and these are what a restored project would read. They are listed by a walk that sees a
    path longer than MAX_PATH, which rglob silently leaves out.
    """
    results = []
    for path in output.rglob("*") if output.is_dir() else []:
        if is_diagnostic_artifact(path) or is_intermediate_artifact(path, output):
            continue
        if path.is_file() and (
            path.suffix.casefold() in TEXT_RESULT_SUFFIXES
            or path.suffix.casefold() in {".csv", ".tsv", ".txt", ".json", ".xlsx"}
            or any(
                token in path.name.casefold()
                for token in ("quality", "qa", "publication", "method", "parameter", "analysis_files")
            )
        ):
            results.append(path.resolve())
    if output.is_dir():
        results.extend(intermediate_files(output.resolve()))
    records = [
        path.resolve()
        for path in provenance.rglob("*")
        # The writer lock and any unfinished temporary file of a durable write are not records.
        if path.is_file() and path.resolve() != manifest_path and not is_manifest_scratch_file(path)
    ]
    return results, records


def finalize_download_lease(manifest_path: Path, run: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate a finished run's mzTab-M and record what it retains.

    ``run`` is the production job that produced the output - its id, run directory and the artifacts it
    created or updated - recorded as ``finalized_run``. It is what lets QA and publication be generated
    from the manifest after the job registry has forgotten the job: without it, the only thing that says
    which QA matrix belongs to this run is a registry truncated to its hundred newest jobs.
    """
    from .mztab_validation import validate_mztab_outputs

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    output = Path(manifest["output_directory"]).resolve()
    validation = validate_mztab_outputs(output)
    summary = validation.get("summary", {})
    mztab_files = [Path(item["file"]).resolve() for item in validation.get("files", [])]
    validated = bool(mztab_files) and not summary.get("failed", 0)
    results, records = _retained_result_paths(output, manifest_path.parent, manifest_path)
    project_archive = _archive_project_results(output)
    retained = [*mztab_files, *results, *([project_archive] if project_archive else []), *records]
    retained_artifacts = list(dict.fromkeys(str(path) for path in retained))
    inventory = [_artifact_inventory(Path(path)) for path in retained_artifacts]
    finalized_at = datetime.now(timezone.utc).isoformat()

    # Everything slow happened above, unlocked. The update itself re-reads the manifest under its writer
    # lock, so a record another writer added meanwhile is kept rather than overwritten.
    def change(current: dict[str, Any]) -> None:
        current["status"] = "mztab_validated" if validated else "validation_failed"
        current["cleanup_allowed"] = validated
        current["mztab_validation"] = validation
        current["retained_artifacts"] = retained_artifacts
        current["retained_artifact_inventory"] = inventory
        current["project_archive"] = str(project_archive) if project_archive else ""
        current["finalized_at"] = finalized_at
        if run:
            artifacts = run.get("artifacts") or {}
            current["finalized_run"] = {
                "job_id": str(run.get("job_id") or ""),
                "run_directory": str(run.get("run_directory") or ""),
                "artifacts": {
                    kind: [str(path) for path in artifacts.get(kind) or []] for kind in ("mztab", "qa")
                },
                "recorded_at": finalized_at,
            }

    manifest = update_manifest(manifest_path, change)
    return {**manifest, "manifest_path": str(manifest_path)}


def refresh_retained_artifacts(manifest_path: Path) -> dict[str, Any]:
    """Add what was written after finalisation to the retained inventory. Never raises.

    The inventory a raw-data deletion is judged against was computed when the run finished, before any
    publication artifact existed, so the report, its bundle and the supplementary tables were outside
    it: a deletion could be confirmed against an inventory that did not list them. This adds every file
    the finalisation rule would now keep, re-hashes every retained path that still exists, and keeps a
    listed path that has gone missing listed, so the cleanup plan still reports it missing.

    Only a manifest that has been finalised is refreshed; anything else is reported and left alone.
    """
    manifest_path = Path(manifest_path).resolve()
    try:
        manifest = read_manifest(manifest_path)
        if not manifest.get("finalized_at"):
            return {"refreshed": False, "reason": "not_finalized", "manifest_path": str(manifest_path)}
        output = Path(str(manifest.get("output_directory") or "")).resolve()
        results, records = _retained_result_paths(output, manifest_path.parent, manifest_path)
        found = [str(path) for path in (*results, *records)]
        refreshed_at = datetime.now(timezone.utc).isoformat()
        added: list[str] = []

        def change(current: dict[str, Any]) -> None:
            listed = [str(item) for item in current.get("retained_artifacts") or []]
            combined = list(dict.fromkeys([*listed, *found]))
            previous = {
                str(item.get("path")): item
                for item in current.get("retained_artifact_inventory") or []
                if isinstance(item, dict)
            }
            current["retained_artifacts"] = combined
            current["retained_artifact_inventory"] = [
                _artifact_inventory(Path(path))
                if path_is_file(path)
                else previous.get(path, {"path": path})
                for path in combined
            ]
            current["retained_artifacts_refreshed_at"] = refreshed_at
            added[:] = [path for path in combined if path not in listed]

        update_manifest(manifest_path, change)
        return {
            "refreshed": True,
            "manifest_path": str(manifest_path),
            "added": added,
            "refreshed_at": refreshed_at,
        }
    except (OSError, ValueError) as error:
        return {
            "refreshed": False,
            "reason": "manifest_error",
            "detail": str(error),
            "manifest_path": str(manifest_path),
        }


# The extractor selects this when it recognises the format but has no reader for it.
# It is deliberately distinct from any other non-zero exit: "this vendor format can
# never be checked here" and "the check did not work this time" call for different
# decisions, and one failure channel cannot carry both.
RAW_METADATA_UNSUPPORTED_FORMAT_EXIT_CODE = 82


def run_raw_metadata_preflight(
    manifest_path: Path,
    extractor_path: Path,
    max_inputs: int | None = None,
    confirm_untargeted: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    extractor_path = extractor_path.resolve()
    if not extractor_path.is_file():
        raise FileNotFoundError(f"Raw metadata extractor was not found: {extractor_path}")
    manifest = read_manifest(manifest_path)
    previously_allowed = bool(
        manifest.get("execution_allowed") or manifest.get("project", {}).get("eligible")
    )
    candidates = [Path(value) for value in manifest.get("input_candidates", [])]
    available = [path for path in candidates if path.exists()]
    # EVERY FILE BY DEFAULT. MS-DIAL reads the acquisition type per analysis file, so a verdict
    # read from the first three files and applied to the rest is a guess about the files nobody
    # looked at. A caller may still cap the inspection; the cap is then recorded as partial
    # coverage and the unit stays under review.
    inputs = available if not max_inputs or max_inputs <= 0 else available[0:max_inputs]
    if not inputs:
        raise ValueError("No extracted MS-DIAL input candidate is available for metadata preflight.")
    output = manifest_path.parent / "raw-metadata-preflight.json"
    command = [str(extractor_path)]
    for path in inputs:
        command.extend(["--input", str(path)])
    command.extend(["--output", str(output), "--max-spectrum-headers", "200"])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    extractor_stat = extractor_path.stat()
    manifest["raw_metadata_preflight"] = {
        "command": command,
        # Which binary produced this verdict, so the verdict can be tied to a build rather
        # than to whichever executable happened to be first on the search order.
        "extractor": {
            "path": str(extractor_path),
            "size_bytes": extractor_stat.st_size,
            "modified_at": datetime.fromtimestamp(
                extractor_stat.st_mtime, tz=timezone.utc
            ).isoformat(),
        },
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output": str(output),
    }
    if completed.returncode == RAW_METADATA_UNSUPPORTED_FORMAT_EXIT_CODE:
        # Fails closed exactly as an unavailable preflight does: a unit that was not
        # already eligible stays ineligible. What changes is that the agent can now tell
        # that waiting or retrying will never help.
        manifest["status"] = "preflight_unsupported_format"
        manifest["execution_allowed"] = previously_allowed
        manifest["raw_metadata_preflight"]["unsupported_formats"] = sorted(
            {path.suffix.lower() for path in inputs if path.suffix}
        )
        manifest["raw_metadata_preflight"]["detail"] = (completed.stderr or "").strip().splitlines()[:1]
        manifest["raw_metadata_preflight"]["advisory"] = (
            "This raw data format has no metadata reader, so a raw-header check cannot "
            "resolve the unit's technical settings on any retry. Resolve them from "
            "repository metadata or the publication, or exclude the unit."
        )
        _write_json(manifest_path, manifest)
        return {**manifest, "manifest_path": str(manifest_path)}
    if completed.returncode != 0 or not output.is_file():
        manifest["status"] = "preflight_unavailable"
        manifest["execution_allowed"] = previously_allowed
        manifest["raw_metadata_preflight"]["advisory"] = (
            "Raw header inspection was unavailable. Repository metadata remains authoritative "
            "only when the project was already eligible before this optional check."
        )
        _write_json(manifest_path, manifest)
        return {**manifest, "manifest_path": str(manifest_path)}
    records = json.loads(output.read_text(encoding="utf-8-sig"))
    if isinstance(records, dict):
        records = [records]
    summary = _summarize_raw_metadata(records)
    project = project_from_dict(manifest["project"])
    if summary["separation"] != "Unknown":
        project.separation = summary["separation"]
    # "Mixed" replaces whatever the repository metadata said, because the headers have shown that
    # label to be wrong for some of the files. "Unknown" still leaves it alone: no header spoke.
    if summary["acquisition_mode"] != "Unknown":
        project.acquisition_mode = summary["acquisition_mode"]
    if summary["ion_mode"] != "Unknown":
        project.ion_mode = summary["ion_mode"]
    if confirm_untargeted:
        project.untargeted = True
        project.evidence.append("Untargeted status confirmed during raw metadata preflight.")
    project.evidence.extend(summary["evidence"])
    coverage = {
        "input_candidates": len(candidates),
        "available": len(available),
        "inspected": len(inputs),
        "complete": len(inputs) == len(candidates),
    }
    summary["coverage"] = coverage
    evaluated = evaluate_eligibility(
        project,
        EligibilityPolicy(
            max_download_bytes=max(project.total_download_bytes, 1),
            max_samples=max(project.sample_count or 0, 1),
            require_known_size=False,
            require_untargeted=True,
        ),
    )
    if not coverage["complete"] and not evaluated.exclusion_reasons:
        evaluated.review_reasons.append(
            f"Raw headers were read from {coverage['inspected']} of {coverage['input_candidates']} "
            "input files, and MS-DIAL applies an acquisition type to each file; inspect every file "
            "before the unit's acquisition mode is taken as established."
        )
        evaluated.eligible = False
        evaluated.selection_status = "raw_metadata_required"
    manifest["project"] = evaluated.as_dict()
    manifest["raw_metadata_preflight"]["summary"] = summary
    manifest["execution_allowed"] = evaluated.eligible
    if summary["acquisition_mode"] == "Mixed":
        manifest["status"] = "preflight_mixed_acquisition"
        groups: dict[str, list[str]] = {}
        for item in summary["per_file"]:
            groups.setdefault(item["acquisition_mode"] or "Unknown", []).append(item["file"])
        manifest["raw_metadata_preflight"]["acquisition_groups"] = {
            mode: sorted(files) for mode, files in sorted(groups.items())
        }
        manifest["raw_metadata_preflight"]["advisory"] = (
            "The raw headers disagree about acquisition mode ("
            + ", ".join(f"{mode} {len(files)}" for mode, files in sorted(groups.items()))
            + "). MS-DIAL runs one acquisition mode per analysis, so this unit cannot run as one. "
            "Split it into one unit per acquisition group, each with its own workspace, manifest and "
            "Class assignments, and preflight each part."
        )
    elif evaluated.eligible:
        manifest["status"] = "preflight_passed"
    else:
        manifest["status"] = "preflight_review_required"
    _write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


# The status a parent unit carries once it has been split. It is not in CLEANUP_READY_STATUSES and it
# never sets execution_allowed, so neither MS-DIAL nor a raw deletion can run against the parent.
SPLIT_PARENT_STATUS = "split_by_acquisition"
SPLIT_PART_STATUS = "split_from_parent"


def _split_part_id(parent_unit_id: str, mode: str) -> str:
    return f"{parent_unit_id}-{mode.casefold()}"


def plan_acquisition_split(manifest_path: Path) -> dict[str, Any]:
    """Describe how a Mixed unit would be split by per-file acquisition mode. Changes nothing.

    WHY A SPLIT AND NOT A RELABEL. MS-DIAL deconvolutes each file by the acquisition type written
    against it, and a DDA file deconvoluted as SWATH, or a DIA file read as DDA, gives a result that
    completes, validates and is wrong. A unit whose headers disagree therefore cannot run as one,
    and the only evidence for how to divide it is what each file's own header said. The parts are
    made from that evidence and nothing else: a file whose header gave no usable mode blocks the
    split rather than being guessed into a part.

    The parts share the parent's raw data and do not copy it. Their input files are disjoint, and
    MS-DIAL's per-file intermediates carry a run timestamp, so parts run one after another do not
    collide. The raw data stay owned by the parent: a part's cleanup is refused, because its raw
    directory is not its own.
    """
    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    project = manifest.get("project") or {}
    preflight = manifest.get("raw_metadata_preflight") or {}
    summary = preflight.get("summary") or {}
    coverage = summary.get("coverage") or {}
    parent_id = str(project.get("analysis_unit_id") or "").strip()
    workspace = Path(str(manifest.get("workspace") or manifest_path.parent.parent))
    blockers: list[str] = []

    if manifest.get("status") == SPLIT_PARENT_STATUS:
        already = manifest.get("split_into") or []
        return {
            "manifest_path": str(manifest_path),
            "analysis_unit_id": parent_id,
            "already_split": True,
            "parts": already,
            "blockers": [],
        }
    if not parent_id:
        blockers.append("The manifest names no analysis_unit_id to derive part identifiers from.")
    if summary.get("acquisition_mode") != "Mixed":
        blockers.append(
            "Only a unit whose raw headers disagree about acquisition mode is split here; this one "
            f"is {summary.get('acquisition_mode') or project.get('acquisition_mode') or 'unknown'!r}."
        )
    if not coverage.get("complete"):
        blockers.append(
            "The raw-header preflight did not read every input file, so the files it did not read "
            "cannot be assigned to a part. Re-run it over every file."
        )

    per_file = {_file_key(str(item.get("file") or "")): item for item in summary.get("per_file") or []}
    candidates = [str(item) for item in manifest.get("input_candidates") or [] if str(item).strip()]
    groups: dict[str, list[str]] = {}
    unassigned: list[str] = []
    for candidate in candidates:
        verdict = per_file.get(_file_key(candidate))
        mode = str((verdict or {}).get("acquisition_mode") or "").strip()
        if mode in HEADER_ACQUISITION_TO_MSDIAL:
            groups.setdefault(mode, []).append(candidate)
        else:
            unassigned.append(f"{Path(candidate).name} ({mode or 'no header verdict'})")
    if unassigned:
        blockers.append(
            f"{len(unassigned)} input files have no DDA, DIA or AIF header verdict and cannot be put "
            f"into a part: {', '.join(unassigned[:5])}. Decide whether to exclude them."
        )

    samples = list(project.get("sample_metadata") or [])
    assignments = list((project.get("class_proposal") or {}).get("assignments") or [])
    parts = []
    claimed_samples: set[int] = set()
    for mode, files in sorted(groups.items()):
        names = {Path(item).name.casefold() for item in files}
        stems = {PurePosixPath(name).stem for name in names}
        part_samples = []
        for index, sample in enumerate(samples):
            raw = PurePosixPath(str((sample or {}).get("raw_file") or "").replace("\\", "/")).name.casefold()
            if raw and (raw in names or (not PurePosixPath(raw).suffix and raw in stems)):
                part_samples.append(sample)
                claimed_samples.add(index)
        sample_ids = {str(item.get("sample_id") or "") for item in part_samples}
        part_assignments = [item for item in assignments if str(item.get("sample_id") or "") in sample_ids]
        levels: dict[str, int] = {}
        for item in part_assignments:
            label = str(item.get("class_label") or "")
            levels[label] = levels.get(label, 0) + 1
        higher_levels = sorted(
            {
                int(level)
                for item in files
                for level in ((per_file.get(_file_key(item)) or {}).get("ms_levels") or [])
                if str(level).isdigit() and int(level) > 2
            }
        )
        part_id = _split_part_id(parent_id, mode)
        parts.append(
            {
                "analysis_unit_id": part_id,
                "acquisition_mode": mode,
                "workspace": str(workspace.parent / part_id),
                "input_candidates": sorted(files),
                "file_count": len(files),
                "sample_ids": sorted(sample_ids),
                "class_levels": levels,
                "higher_ms_levels": higher_levels,
            }
        )
    unclaimed = [
        str((sample or {}).get("sample_id") or index)
        for index, sample in enumerate(samples)
        if index not in claimed_samples
    ]
    return {
        "manifest_path": str(manifest_path),
        "analysis_unit_id": parent_id,
        "already_split": False,
        "parts": parts,
        "unclaimed_samples": unclaimed,
        "blockers": blockers,
    }


def _part_class_proposal(
    parent_proposal: dict[str, Any], sample_ids: set[str], parent_unit_id: str, mode: str
) -> dict[str, Any]:
    """The parent's accepted Class proposal, restricted to one part's samples, saying so.

    The rationale is the Catalog's own text about the whole unit and is left as written. What a run's
    provenance has to carry besides it is that this run holds only some of those samples: MTBLS2207's
    DDA part was about to record "Class ... across 11 samples" for a run of six. The warnings travel
    into the run's class_proposal_provenance, so that is where it is said.
    """
    if not parent_proposal:
        return {}
    proposal = copy.deepcopy(parent_proposal)
    every = list(proposal.get("assignments") or [])
    kept = [item for item in every if str(item.get("sample_id") or "") in sample_ids]
    levels = sorted({str(item.get("class_label") or "") for item in kept})
    proposal["assignments"] = kept
    proposal["split_from"] = {
        "parent_analysis_unit_id": parent_unit_id,
        "split_by": "raw_header_acquisition_mode",
        "acquisition_mode": mode,
        "assignments_kept": len(kept),
        "assignments_in_parent": len(every),
        "note": "The parent's accepted proposal, restricted to this part's samples. No sample was regrouped.",
    }
    warnings = list(proposal.get("warnings") or [])
    warnings.append(
        f"This run holds {len(kept)} of the {len(every)} samples the proposal assigned: analysis unit "
        f"{parent_unit_id} was split by raw-header acquisition mode and this is its {mode} part. The "
        "rationale describes the whole unit."
    )
    if len(levels) < 2:
        warnings.append(
            f"After the split this part holds {len(levels)} Class level(s) "
            f"({', '.join(levels) or 'none'}), so it carries no contrast."
        )
    proposal["warnings"] = warnings
    return proposal


def split_unit_by_acquisition(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    """Split a Mixed unit into one part per acquisition mode, each with its own manifest.

    With confirmed false this is plan_acquisition_split. With confirmed true it writes, for each part,
    a workspace holding provenance and output directories and a run manifest that:

    - admits only that part's input files, so the execution gate refuses the others;
    - carries the part's acquisition mode as read from its files' headers, and those headers' own
      verdicts, so the gate can check each file against its header;
    - keeps only the samples, and the accepted Class assignments, that belong to its files. The
      Class decision is the parent's, filtered; it is not a new grouping;
    - starts with execution_allowed false. Each part is preflighted on its own before it can run.

    The parent is marked split and names its parts. Nothing is copied or deleted.
    """
    plan = plan_acquisition_split(manifest_path)
    if plan["already_split"] or not confirmed or plan["blockers"]:
        return {**plan, "written": False}

    manifest_path = Path(plan["manifest_path"])
    parent = read_manifest(manifest_path)
    parent_project = parent.get("project") or {}
    parent_conversion_reasons = [
        reason
        for reason in evaluate_eligibility(
            project_from_dict(parent_project),
            EligibilityPolicy(
                max_download_bytes=max(int(parent_project.get("total_download_bytes") or 0), 1),
                max_samples=max(int(parent_project.get("sample_count") or 0), 1),
                require_known_size=False,
                require_untargeted=False,
            ),
        ).exclusion_reasons
        if "no mzXML/mzData reader" in reason
    ]
    per_file = {
        _file_key(str(item.get("file") or "")): item
        for item in (parent.get("raw_metadata_preflight") or {}).get("summary", {}).get("per_file") or []
    }
    from .repository_metadata import metadata_workspace

    now = datetime.now(timezone.utc).isoformat()
    written = []
    for part in plan["parts"]:
        root = Path(part["workspace"])
        provenance = root / "provenance"
        output = root / "output"
        provenance.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        names = {Path(item).name.casefold() for item in part["input_candidates"]}
        sample_ids = set(part["sample_ids"])

        project = copy.deepcopy(parent_project)
        project["analysis_unit_id"] = part["analysis_unit_id"]
        project["acquisition_mode"] = part["acquisition_mode"]
        project["sample_metadata"] = [
            sample for sample in parent_project.get("sample_metadata") or []
            if str((sample or {}).get("sample_id") or "") in sample_ids
        ]
        project["sample_count"] = len(project["sample_metadata"]) or part["file_count"]
        matched_files = [
            item for item in parent_project.get("files") or []
            if PurePosixPath(str(item.get("name") or "").replace("\\", "/")).name.casefold() in names
        ]
        # An archive unit's file list names the archive, not the files inside it; the part then
        # shares the parent's download description rather than being given an empty one.
        project["files"] = matched_files or list(parent_project.get("files") or [])
        project["total_download_bytes"] = sum(int(item.get("size_bytes") or 0) for item in project["files"])
        proposal = _part_class_proposal(
            parent_project.get("class_proposal") or {},
            sample_ids,
            plan["analysis_unit_id"],
            part["acquisition_mode"],
        )
        if proposal:
            project["class_proposal"] = proposal
        project["evidence"] = list(project.get("evidence") or []) + [
            f"Split from analysis unit {plan['analysis_unit_id']} by raw-header acquisition mode: "
            f"{part['file_count']} file(s) whose headers read {part['acquisition_mode']}."
        ]
        warnings = list(project.get("warnings") or [])
        if part["higher_ms_levels"]:
            warnings.append(
                f"Some files in this part record MS level(s) {part['higher_ms_levels']} as well as "
                "MS1 and MS2. MS-DIAL reads MS1 and MS2 only; the higher levels are not analysed."
            )
        project["warnings"] = warnings
        # The parent's verdict was about the parent - "more than one acquisition mode" is exactly
        # what a part is not - so a part is judged afresh. It is then held back regardless, because
        # its acquisition mode has so far been read only as one group of the parent's files.
        evaluated = evaluate_eligibility(
            project_from_dict(project),
            EligibilityPolicy(
                max_download_bytes=max(int(project["total_download_bytes"] or 0), 1),
                max_samples=max(int(project["sample_count"] or 0), 1),
                require_known_size=False,
                require_untargeted=True,
            ),
        )
        project["exclusion_reasons"] = list(
            dict.fromkeys([*evaluated.exclusion_reasons, *parent_conversion_reasons])
        )
        project["review_reasons"] = list(evaluated.review_reasons) + (
            []
            if project["exclusion_reasons"]
            else ["Preflight this part on its own before it can run."]
        )
        project["eligible"] = False
        project["selection_status"] = (
            "excluded" if project["exclusion_reasons"] else "raw_metadata_required"
        )

        part_manifest = {
            "schema": parent.get("schema", "msdial-public-reanalysis-run.v1"),
            "created_at": now,
            "status": SPLIT_PART_STATUS,
            "project": project,
            "split_from": {
                "manifest_path": str(manifest_path),
                "analysis_unit_id": plan["analysis_unit_id"],
                "split_by": "raw_header_acquisition_mode",
                "acquisition_mode": part["acquisition_mode"],
            },
            "workspace": str(root),
            # Shared with the parent and owned by it. A part's cleanup plan refuses this directory
            # because it is not <workspace>\raw, which is the intended outcome.
            "raw_directory": parent.get("raw_directory"),
            "input_directory": parent.get("input_directory"),
            "output_directory": str(output),
            "raw_owned_by": str(manifest_path),
            "input_candidates": part["input_candidates"],
            "analysis_input_path": parent.get("analysis_input_path"),
            "execution_allowed": False,
            "cleanup_allowed": False,
            "raw_retention_policy": parent.get("raw_retention_policy"),
            "header_verdicts_from_parent": [
                per_file[_file_key(item)] for item in part["input_candidates"] if _file_key(item) in per_file
            ],
        }
        lineage = parent.get("input_lineage")
        if isinstance(lineage, dict):
            # The part reads the parent's files, so their lineage is the parent's, row for row. Without
            # it every part would look like a manifest written before lineage existed.
            part_keys = {_file_key(item) for item in part["input_candidates"]}
            part_manifest["input_lineage"] = {
                **{key: value for key, value in lineage.items() if key != "rows"},
                "rows": [
                    row for row in lineage.get("rows") or []
                    if isinstance(row, dict) and _file_key(str(row.get("path") or "")) in part_keys
                ],
                "inherited_from": str(manifest_path),
            }
        repository_metadata_path = provenance / "repository-metadata.json"
        sample_metadata_path = provenance / "sample-metadata-extracted.json"
        part_manifest_path = provenance / "run-manifest.json"
        _write_json(repository_metadata_path, project)
        _write_json(sample_metadata_path, metadata_workspace(project))
        part_manifest["repository_metadata_file"] = str(repository_metadata_path)
        part_manifest["sample_metadata_file"] = str(sample_metadata_path)
        _write_json(part_manifest_path, part_manifest)
        written.append({**part, "manifest_path": str(part_manifest_path)})

    def change(current: dict[str, Any]) -> None:
        current["status"] = SPLIT_PARENT_STATUS
        current["execution_allowed"] = False
        current["split_at"] = now
        current["split_into"] = written

    update_manifest(manifest_path, change)
    return {**plan, "parts": written, "written": True}


# The manifest states from which a confirmed deletion may proceed. cleanup_pending_confirmation is the
# state a run leaves behind when its retention policy asked for deletion: the technical preconditions are
# met and the decision is now waiting for a person.
CLEANUP_READY_STATUSES = {"mztab_validated", "completed", "cleanup_pending_confirmation"}


def evaluate_repository_execution_gate(state: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a workflow may start MS-DIAL Console against a repository analysis unit.

    execution_allowed is the gate that holds a downloaded unit back until its technical conditions are
    settled. A unit whose repository metadata already establishes untargeted LC-MS/MS with a known
    acquisition mode and polarity gets it at download time; a unit whose metadata is ambiguous may still
    be downloaded for inspection, but with execution_allowed false, and only a raw-header preflight that
    re-evaluates eligibility can turn it true.

    The field was written at three points and read for a decision nowhere, so the hold never held. This
    is that missing read. It applies only to a workflow carrying a repository_run_manifest: an ordinary
    local analysis has no manifest, no eligibility verdict, and nothing to gate.

    Everything before the Console stays open. Metadata review, applying a Class proposal, the raw-header
    preflight itself and any dry-run preview are how a unit becomes eligible in the first place, so
    refusing them would make the gate impossible to pass.
    """
    manifest_text = str(state.get("repository_run_manifest") or "").strip()
    if not manifest_text:
        return {"gated": False, "allowed": True, "blockers": []}

    blockers: list[str] = []
    manifest_path = Path(manifest_text).expanduser()
    if not manifest_path.is_file():
        return {
            "gated": True,
            "allowed": False,
            "manifest_path": str(manifest_path),
            "blockers": [
                "The workflow names a repository run manifest that does not exist: "
                f"{manifest_path}. A repository unit may not run without the manifest that records "
                "its eligibility."
            ],
        }
    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError) as error:
        return {
            "gated": True,
            "allowed": False,
            "manifest_path": str(manifest_path),
            "blockers": [f"The repository run manifest could not be read: {error}"],
        }

    project = manifest.get("project") or {}
    if manifest.get("execution_allowed") is not True:
        blockers.append(
            "execution_allowed is not true for this analysis unit "
            f"(status {manifest.get('status', 'unknown')!r}, selection "
            f"{project.get('selection_status', 'unknown')!r}). Resolve the unit's technical conditions "
            "with a raw-header preflight before running MS-DIAL."
        )

    # The manifest and the workflow must be describing the same unit. A workflow that has drifted to
    # another directory or another file set is no longer covered by this manifest's verdict, whatever
    # that verdict says.
    declared_output = str(manifest.get("output_directory") or "").strip()
    requested_output = str(state.get("output_root") or "").strip()
    if declared_output and requested_output:
        if Path(declared_output).resolve() != Path(requested_output).resolve():
            blockers.append(
                f"The workflow writes to {requested_output}, but this unit's manifest owns "
                f"{declared_output}."
            )

    admitted = {
        Path(str(item)).resolve()
        for item in (manifest.get("input_candidates") or [])
        if str(item).strip()
    }
    if admitted:
        requested = [
            Path(str(item.get("file_path") or "")).resolve()
            for item in (state.get("files") or [])
            if str(item.get("file_path") or "").strip()
        ]
        outside = [str(path) for path in requested if path not in admitted]
        if outside:
            blockers.append(
                f"{len(outside)} input files are not among the files this unit's manifest admitted; "
                f"the first is {outside[0]}."
            )

    # A run in the wrong polarity produces a complete, validated, entirely void result, and no later
    # stage flags it. The manifest records what the repository declared for this unit.
    declared_mode = str(project.get("ion_mode") or "").strip().casefold()
    requested_mode = str(state.get("ion_mode") or "").strip().casefold()
    if declared_mode and requested_mode and declared_mode not in {"both", "unknown"}:
        if declared_mode != requested_mode:
            blockers.append(
                f"The workflow is set to {state.get('ion_mode')} ion mode, but this unit is "
                f"{project.get('ion_mode')}."
            )

    # MS-DIAL deconvolutes each file by the acquisition type written against it, and a file with no
    # type written against it is DDA. A unit whose headers disagree cannot run as one, whatever else
    # its manifest says; and where a header has spoken for a file, the type the workflow is about to
    # use for that file has to agree with it.
    if str(project.get("acquisition_mode") or "").strip() == "Mixed":
        blockers.append(
            "The raw headers show more than one acquisition mode across this unit's files. Split the "
            "unit by per-file acquisition mode before running MS-DIAL."
        )
    header_modes = _header_acquisition_by_file(manifest)
    if header_modes:
        disagreeing = []
        for item in state.get("files") or []:
            path_text = str(item.get("file_path") or "").strip()
            if not path_text:
                continue
            header = header_modes.get(_file_key(path_text))
            requested = str(item.get("acquisition_type") or "DDA").strip() or "DDA"
            if header and requested not in HEADER_ACQUISITION_TO_MSDIAL.get(header, {header}):
                disagreeing.append(f"{Path(path_text).name} (header {header}, run as {requested})")
        if disagreeing:
            blockers.append(
                f"{len(disagreeing)} input files would run with an acquisition type their raw header "
                f"contradicts; the first is {disagreeing[0]}."
            )

    return {
        "gated": True,
        "allowed": not blockers,
        "manifest_path": str(manifest_path),
        "analysis_unit_id": project.get("analysis_unit_id"),
        "execution_allowed": manifest.get("execution_allowed"),
        "manifest_status": manifest.get("status"),
        "blockers": blockers,
    }


# Which MS-DIAL AcquisitionType values a raw-header verdict admits. The header reader's "DIA" does not
# say whether the windows were sequential (SWATH) or all-ion (AIF), so it admits either; what it never
# admits is DDA, and a DDA header never admits a DIA deconvolution.
HEADER_ACQUISITION_TO_MSDIAL: dict[str, set[str]] = {
    "DDA": {"DDA"},
    "DIA": {"SWATH", "AIF"},
    "AIF": {"AIF"},
}


def _file_key(path_text: str) -> str:
    return str(Path(path_text).resolve()).casefold()


def _header_acquisition_by_file(manifest: dict[str, Any]) -> dict[str, str]:
    """The acquisition mode each inspected file's own header reported, keyed by resolved path."""
    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    result = {}
    for item in summary.get("per_file") or []:
        path_text = str(item.get("file") or "").strip()
        mode = str(item.get("acquisition_mode") or "").strip()
        if path_text and mode in HEADER_ACQUISITION_TO_MSDIAL:
            result[_file_key(path_text)] = mode
    return result


def _tree_size(root: Path) -> tuple[int, int]:
    """Return (file count, total bytes) under root, or (0, 0) when it is gone."""
    if not root.is_dir():
        return 0, 0
    files = [path for path in root.rglob("*") if path.is_file()]
    return len(files), sum(path.stat().st_size for path in files)


def plan_download_cleanup(manifest_path: Path) -> dict[str, Any]:
    """Describe exactly what a raw-data deletion would remove and what would survive it.

    Deleting downloaded raw data is the only irreversible operation in this pipeline, and the campaign
    rules require the person approving it to have seen three things first: the artifacts that will be
    retained, the paths that will be removed, and how much will be freed. This produces those three so a
    caller can present them; it changes nothing.

    A run whose finalisation could not move MS-DIAL's containers out of the raw tree holds the deletion
    (run_finalisation.raw_deletion_holds): the containers would go with it.
    """
    from .run_finalisation import describe_holds, raw_deletion_holds

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    raw_root = Path(manifest.get("raw_directory", "")).resolve()
    workspace = Path(manifest.get("workspace", "")).resolve()
    retained = [Path(value) for value in manifest.get("retained_artifacts", [])]
    missing = [str(path) for path in retained if not os.path.exists(extended_path(path))]
    file_count, total_bytes = _tree_size(raw_root)
    within_workspace = raw_root.parent == workspace and raw_root.name == "raw"
    blockers: list[str] = []
    if manifest.get("status") not in CLEANUP_READY_STATUSES:
        blockers.append(
            f"Manifest status is {manifest.get('status', 'unknown')!r}; deletion requires a validated run."
        )
    if not manifest.get("cleanup_allowed"):
        blockers.append("cleanup_allowed is not true; the run did not produce a validated mzTab-M output.")
    if not retained:
        blockers.append("No retained artifacts are recorded, so nothing would survive the deletion.")
    if missing:
        blockers.append(f"{len(missing)} recorded retained artifacts are missing from disk.")
    if not within_workspace:
        blockers.append("The raw directory is not the expected 'raw' folder inside the project workspace.")
    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        blockers.append(
            "MS-DIAL containers are still in the raw directory, and its deletion would delete them: "
            + describe_holds(held) + "."
        )
    return {
        **({"finalisation_holds": held} if held else {}),
        "manifest_path": str(manifest_path),
        "status": manifest.get("status"),
        "retention_policy": manifest.get("raw_retention_policy"),
        "cleanup_allowed": bool(manifest.get("cleanup_allowed")),
        "deletion_target": str(raw_root),
        "deletion_file_count": file_count,
        "deletion_bytes": total_bytes,
        "retained_artifact_count": len(retained),
        "retained_artifact_inventory": manifest.get("retained_artifact_inventory", []),
        "missing_retained_artifacts": missing,
        "blockers": blockers,
        "ready_for_confirmation": not blockers and file_count > 0,
    }


def request_download_cleanup(manifest_path: Path) -> dict[str, Any]:
    """Record that the run's retention policy asked for deletion, and stop there.

    The retention policy chosen at download time records a wish. Whether the technical preconditions are
    met is a second, separate thing, recorded as cleanup_allowed. Whether to actually delete, having seen
    what goes and what stays, is a third, and it belongs to a person. Running a job is not an occasion to
    make that third decision on their behalf, so this marks the manifest and returns the plan.
    """
    manifest_path = manifest_path.resolve()
    with manifest_lock(manifest_path):
        manifest = read_manifest(manifest_path)
        if manifest.get("cleanup_allowed") and manifest.get("status") in {"mztab_validated", "completed"}:
            manifest["status"] = "cleanup_pending_confirmation"
            manifest["cleanup_requested_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(manifest_path, manifest)
    plan = plan_download_cleanup(manifest_path)
    plan["deleted"] = False
    plan["confirmation_required"] = True
    return plan


def cleanup_download_lease(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    from .run_finalisation import (
        BLOCKS_RAW_DELETION,
        FinalisationHeld,
        raw_deletion_holds,
        resolve_finalisation_holds,
    )

    manifest_path = manifest_path.resolve()
    if raw_deletion_holds(manifest_path):
        # The move the run's finalisation could not make is retried before the preview is drawn, so that
        # the preview describes what the deletion would really remove; what it moves joins the retained
        # inventory. Nothing here deletes.
        resolve_finalisation_holds(manifest_path)
        refresh_retained_artifacts(manifest_path)
    manifest = read_manifest(manifest_path)
    if not confirmed:
        # The preview carries the retained artifacts, the target and the size, because a confirmation
        # given without them is not an informed one. It used to return only the flag.
        plan = plan_download_cleanup(manifest_path)
        plan["deleted"] = False
        plan["confirmation_required"] = True
        return plan
    if manifest.get("status") not in CLEANUP_READY_STATUSES or not manifest.get("cleanup_allowed"):
        raise ValueError("Raw cleanup requires a completed/validated manifest with cleanup_allowed=true.")
    retained = [Path(value) for value in manifest.get("retained_artifacts", [])]
    if not retained or any(not os.path.exists(extended_path(path)) for path in retained):
        raise ValueError("Retained mzTab-M/provenance artifacts are missing; raw cleanup was refused.")
    raw_root = Path(manifest["raw_directory"]).resolve()
    workspace = Path(manifest["workspace"]).resolve()
    if raw_root.parent != workspace or raw_root.name != "raw":
        raise ValueError("Raw directory is outside the expected project workspace.")
    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        raise FinalisationHeld(
            BLOCKS_RAW_DELETION,
            "MS-DIAL containers are still in the raw directory, and deleting it would delete them; raw cleanup "
            "was refused", held,
        )
    shutil.rmtree(raw_root)
    cleaned_at = datetime.now(timezone.utc).isoformat()

    def change(current: dict[str, Any]) -> None:
        current["status"] = "raw_cleaned"
        current["raw_cleaned_at"] = cleaned_at

    update_manifest(manifest_path, change)
    return {"deleted": True, "raw_directory": str(raw_root), "manifest_path": str(manifest_path)}


def discard_download_lease(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    from .mztab_validation import find_mztab_files

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    downloading = manifest.get("status") == "downloading"
    owner_state = lease_owner_state(manifest) if downloading else None
    if not confirmed:
        preview = {"deleted": False, "confirmation_required": True, "manifest_path": str(manifest_path)}
        if owner_state is not None:
            preview["lease_owner_state"] = owner_state
        return preview
    if manifest.get("status") in {"mztab_validated", "completed", "raw_cleaned"}:
        raise ValueError("Validated/completed runs must use the normal cleanup command.")
    stale: dict[str, Any] | None = None
    if downloading:
        # Written before the first byte of a lease. A lease that is still running writes into the tree
        # this would delete. One whose process is provably gone - killed with it by a reboot or a
        # backend stop, so it never wrote download_failed - never will, and its bytes are released here
        # rather than kept until someone downloads the unit again. Possibly alive is not gone.
        if owner_state["state"] != "gone":
            raise ValueError(
                "This unit's lease is recorded as still downloading, and its owner is not provably gone "
                f"({owner_state['reason']}); retry or finish the lease before discarding its raw data."
            )
        stale = {
            "lease_owner": manifest.get("lease_owner"),
            "evidence": owner_state["reason"],
            "last_heartbeat_at": manifest.get("download_progress_at"),
        }
    output = Path(manifest.get("output_directory", ""))
    if find_mztab_files(output):
        raise ValueError("mzTab-M output exists; finalize the run before deleting raw data.")
    raw_root = Path(manifest["raw_directory"]).resolve()
    workspace = Path(manifest["workspace"]).resolve()
    if raw_root.parent != workspace or raw_root.name != "raw":
        raise ValueError("Raw directory is outside the expected project workspace.")
    from .run_finalisation import BLOCKS_RAW_DELETION, FinalisationHeld, raw_deletion_holds

    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        raise FinalisationHeld(
            BLOCKS_RAW_DELETION,
            "MS-DIAL containers a finished run could not move are still in the raw directory, and the cleanup "
            "command retries the move; discard was refused", held,
        )
    if raw_root.exists():
        shutil.rmtree(raw_root)
    discarded_at = datetime.now(timezone.utc).isoformat()

    def change(current: dict[str, Any]) -> None:
        if stale is not None and (current.get("lease_owner") or {}).get("lease_id") != (
            (stale["lease_owner"] or {}).get("lease_id")
        ):
            # Another lease took the workspace over after the check. Its record is not this discard's.
            raise ValueError(
                "A new lease took this workspace over while its stale lease was being discarded; the new "
                "lease's record was left as it is."
            )
        current["status"] = "discarded"
        current["discarded_at"] = discarded_at
        current["discard_reason"] = (
            "Preflight/download was rejected before a retained mzTab-M result was produced."
            if stale is None
            else "The lease's process stopped before it recorded its inputs or its failure."
        )
        if stale is not None:
            current["stale_lease_discarded"] = {**stale, "discarded_at": discarded_at}

    update_manifest(manifest_path, change)
    result = {"deleted": True, "raw_directory": str(raw_root), "manifest_path": str(manifest_path)}
    if stale is not None:
        result["stale_lease_discarded"] = True
    return result


def project_from_dict(value: dict[str, Any]) -> RepositoryProject:
    data = dict(value)
    files = []
    for item in data.get("files", []):
        payload = {
            "name": str(item.get("name") or item.get("path") or ""),
            "size_bytes": int(item.get("size_bytes") or 0),
            "url": str(item.get("url") or item.get("download_url") or ""),
            "role": str(item.get("role") or "raw"),
            "checksum": str(item.get("checksum") or ""),
        }
        if (
            payload["role"] in ANALYSIS_INPUT_ROLES
            and (item.get("requires_conversion") or requires_msdial_conversion(payload["name"]))
        ):
            payload["role"] = "requires_conversion"
        files.append(RepositoryFile(**payload))
    data["files"] = files
    return RepositoryProject(**data)


# The roles MS-DIAL can open as an analysis input. "converted" means mzML here. mzXML and mzData
# require conversion to mzML and are assigned requires_conversion instead. Archives are not inputs
# themselves - their extracted contents are attributed by _sample_file_names below. Sidecars,
# auxiliaries and alternate encodings are never independent analysis inputs.
ANALYSIS_INPUT_ROLES = frozenset({"raw", "converted"})


def _project_allowlist(
    project: RepositoryProject, *, analysis_only: bool = False
) -> list[str]:
    """The unit's listed file names, relative and casefolded, as the data root holds them.

    A listed per-sample container archive also stands for the container it unpacks to
    (archives.container_alias): FILES/X.raw.zip admits X.raw, in the directory it was listed in,
    which is where the lease expands it. With analysis_only, only what MS-DIAL opens: files of an
    analysis role, and the container of a listed archive of one, or of an archive listed for this unit
    alone (raw_archive). A container named by an archive every unit of a study lists
    (shared_raw_archive) is admitted by this unit's sample names, never by the archive.
    """
    names: list[str] = []
    for item in project.files:
        if not item.name:
            continue
        name = _safe_relative_name(item.name).as_posix().casefold()
        analysis = item.role in ANALYSIS_INPUT_ROLES and not requires_msdial_conversion(item.name)
        if not analysis_only or analysis:
            names.append(name)
        alias = _container_alias_path(name)
        if alias and (
            not analysis_only
            or ((analysis or item.role == "raw_archive") and not requires_msdial_conversion(alias))
        ):
            names.append(alias)
    return names


def _container_alias_path(name: str) -> str:
    """'raw/x.raw.zip' -> 'raw/x.raw': a listed archive's path as the container it stands for, or ''."""
    listed = PurePosixPath(name)
    alias = archives.container_alias(listed.name)
    if not alias:
        return ""
    return alias if str(listed.parent) in ("", ".") else f"{listed.parent.as_posix()}/{alias}"


def _sample_file_names(project: RepositoryProject) -> tuple[set[str], set[str]]:
    """Every file name this unit's own samples claim, with and without an extension.

    WHY THIS EXISTS. Most repository units do not enumerate their raw files at all: Metabolomics
    Workbench publishes one archive per study, so the unit's file list is the archive and nothing
    else. Measured on 2026-09-21, 747 of the 831 campaign-eligible units holding files were in that
    state, and the analysis allow-list built from the file list alone came out empty for every one
    of them - after the download had already transferred the data.

    The unit's samples do name their files. Matching against those names is what attributes an
    extracted archive to one unit, and it is stricter than the archive name it replaces: an archive
    shared between a positive and a negative unit used to admit all of both, and a sample list
    belonging to one unit admits only that unit's files.

    Both the full name and the stem are kept, because a repository may record "sample_01" for a
    file that arrives as "sample_01.mzML" or as a "sample_01.d" directory. A name that is a packed
    container (X.raw.zip) also claims the container (X.raw).
    """
    exact: set[str] = set()
    stems: set[str] = set()
    for sample in project.sample_metadata or []:
        raw = str((sample or {}).get("raw_file") or "").strip()
        if not raw:
            continue
        base = PurePosixPath(raw.replace("\\", "/")).name.casefold()
        if not base:
            continue
        exact.add(base)
        # A sample recorded as its packed container (MetaboLights: X.raw.zip, X.d.zip) is the
        # container once unpacked, and that is the name the lease finds on disk.
        alias = archives.container_alias(base)
        if alias:
            exact.add(alias.casefold())
        if not PurePosixPath(base).suffix:
            # ONLY when the repository recorded no extension. Matching on the stem of a name that
            # HAS one would pull in a second encoding of the same sample: a .wiff2 beside a .wiff
            # shares its stem, and analysing both analyses that sample twice.
            stems.add(base)
    return exact, stems


def _matches_sample_file_names(
    path: Path, names: tuple[set[str], set[str]] | set[str]
) -> bool:
    exact, stems = names if isinstance(names, tuple) else (names, set())
    if not exact and not stems:
        return False
    base = path.name.casefold()
    if base in exact:
        return True
    return bool(stems) and PurePosixPath(base).stem in stems

def _filter_inputs_by_project_allowlist(
    inputs: list[str], data_root: Path, project: RepositoryProject
) -> list[str]:
    if not project.analysis_unit_id:
        return inputs
    allowed = _project_allowlist(project, analysis_only=True)
    sample_names = _sample_file_names(project)
    if not allowed and not any(sample_names):
        raise ValueError(
            f"Analysis unit {project.analysis_unit_id} names no analysis input: it declares no "
            f"file of role {sorted(ANALYSIS_INPUT_ROLES)} and none of its samples names a raw "
            "file, so there is nothing to attribute the downloaded data to."
        )

    # Either source is sufficient on its own, and both are scoped to THIS unit: the declared
    # analysis-input files, and the file names this unit's samples claim. An archive shared with
    # another unit used to admit all of both units' contents through the archive's own name; the
    # sample names admit only this unit's.
    selected = [
        item
        for item in inputs
        if not requires_msdial_conversion(Path(item).name)
        and (
            _path_matches_allowlist(Path(item), data_root, allowed)
            or _matches_sample_file_names(Path(item), sample_names)
        )
    ]
    if not selected:
        raise ValueError(
            f"Downloaded content did not contain an MS-DIAL input listed for analysis unit "
            f"{project.analysis_unit_id}. Refusing to fall back to accession-level inputs."
        )
    return selected



def _allowlist_forms(relative: str) -> set[str]:
    """The names a listed file may carry for a path relative to the data root (casefolded).

    The path itself, less a leading FILES/, and less its first component (the folder an archive
    unpacks into, such as MB-POST_files_MPST000007.0), again less a FILES/ under it. Nothing deeper.
    """
    candidates = {relative}
    if relative.startswith("files/"):
        candidates.add(relative[6:])
    parts = relative.split("/")
    if len(parts) > 1:
        without_archive_root = "/".join(parts[1:])
        candidates.add(without_archive_root)
        if without_archive_root.startswith("files/"):
            candidates.add(without_archive_root[6:])
    return candidates


def _relative_to_data_root(path: Path, data_root: Path) -> str | None:
    """path relative to data_root, '/'-separated and casefolded; None when it lies outside.

    Paths the lease builds are already under the resolved data root, and are compared as written;
    anything else is resolved first, as the allow-list always was.
    """
    try:
        return path.relative_to(data_root).as_posix().casefold()
    except ValueError:
        pass
    try:
        return path.resolve().relative_to(data_root.resolve()).as_posix().casefold()
    except ValueError:
        return None


def _path_matches_allowlist(
    path: Path,
    data_root: Path,
    allowed: Iterable[str],
    *,
    allow_directory_descendants: bool = False,
) -> bool:
    """Whether path is a listed file, or lies inside a listed .d/.raw folder when descendants count.

    allowed is looked up as a set, so matching every file of a unit is linear in its files and not
    files times listed names; a MetaboBank folder unit lists 6,844 members.
    """
    relative = _relative_to_data_root(path, data_root)
    if relative is None:
        return False
    expected = allowed if isinstance(allowed, (set, frozenset)) else set(allowed)
    for candidate in _allowlist_forms(relative):
        if candidate in expected:
            return True
        if allow_directory_descendants:
            parts = candidate.split("/")
            for index in range(1, len(parts)):
                prefix = "/".join(parts[:index])
                if prefix in expected and PurePosixPath(prefix).suffix in {".d", ".raw"}:
                    return True
    return False


def _is_sample_member(path: Path, data_root: Path, names: tuple[set[str], set[str]]) -> bool:
    """Whether an extracted file belongs to one of this unit's samples.

    It is a sample's file; or the .wiff.scan that travels with one; or it lies inside a .d/.raw
    folder a sample names. This is what lists a Workbench unit's members out of a study archive,
    whose own name matches none of them.
    """
    if not any(names):
        return False
    if _matches_sample_file_names(path, names):
        return True
    if _is_sidecar_name(path.name) and _matches_sample_file_names(path.with_name(path.name[:-5]), names):
        return True
    relative = _relative_to_data_root(path, data_root)
    if relative is None:
        return False
    parts = relative.split("/")
    return any(
        PurePosixPath(part).suffix in {".d", ".raw"} and _matches_sample_file_names(Path(part), names)
        for part in parts[:-1]
    )


def _filter_project_allowlist_paths(
    paths: list[str], data_root: Path, project: RepositoryProject
) -> list[str]:
    """The extracted files that are this unit's: listed, inside a listed folder, or its samples'.

    Matching the listed names alone gave [] for every Workbench unit, whose only listed file is the
    study archive: extracted_files said nothing came out for the unit although its inputs had.
    """
    if not project.analysis_unit_id:
        return paths
    allowed = set(_project_allowlist(project))
    sample_names = _sample_file_names(project)
    return [
        item
        for item in paths
        if _path_matches_allowlist(Path(item), data_root, allowed, allow_directory_descendants=True)
        or _is_sample_member(Path(item), data_root, sample_names)
    ]


def _listed_checksum(item: RepositoryFile) -> tuple[str, str]:
    """(algorithm, value) of an item's declared checksum; ('', '') when none. Raises when unusable."""
    checksum = item.checksum.strip().casefold()
    if not checksum:
        return "", ""
    algorithm = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(checksum))
    if algorithm is None or not re.fullmatch(r"[0-9a-f]+", checksum):
        raise ValueError(f"Unsupported checksum for allow-listed file {item.name}.")
    return algorithm, checksum


def _names_archive_object(item: RepositoryFile, download: dict[str, Any]) -> bool:
    """Whether a listed item is the archive object a download fetched, rather than a file inside it.

    An archive role says so (Metabolomics Workbench), and so does an item listed under the downloaded
    archive's own name (MetaboLights X.raw.zip). MB-POST lists the files inside its project tar under
    the tar's URL, and those are files to find in the extracted tree.
    """
    if not download.get("archive"):
        return False
    if item.role in ARCHIVE_ROLES:
        return True
    listed = PurePosixPath(str(item.name or "").replace("\\", "/")).name.casefold()
    return bool(listed) and listed == Path(str(download.get("path") or "")).name.casefold()


def _verify_project_allowlist_checksums(
    data_root: Path,
    project: RepositoryProject,
    per_file: dict[str, dict[str, Any]] | None = None,
    downloads: list[dict[str, Any]] | None = None,
    archive_extractions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Compare every declared md5, sha1 or sha256 with the file it names. Raises on any mismatch.

    Returns the counts the manifest records. ``per_file``, when given, is filled with what was verified
    for each file, keyed by _file_key: the input lineage records it on that file's row, where the gate
    reads it. Only the counts used to leave here, so an input whose declared checksum had been checked
    looked unverified to everything that read the lineage.

    AN ARCHIVE IS VERIFIED AS THE OBJECT IT IS. A listed item that is an archive the lease downloaded
    (``downloads``; see _names_archive_object) is compared with that download's bytes and counted as
    archives_verified_at_download. It used to be searched for under raw\\data by its own name, where
    no archive ever is once it has been expanded, so every one of the 661 declared Workbench units
    raised "resolved to 0 extracted files" after its whole download. What came out of such an archive
    is vouched for by the archive and the listing of what came out (archive_extractions), not by a
    checksum of its own; verified and skipped keep counting the files that were listed one by one. An
    archive verified here also marks its download entry, which is what the input lineage reads.

    A listed file that was itself an archive inside a downloaded one (MB-POST lists an MD5 for each
    per-sample zip in its project tar) was expanded in place and is gone from the data root. It is
    compared with the digests archives.py took of it before it expanded (``archive_extractions``),
    counted as verified like any listed file, and marked in its nested extraction record.

    Each listed name is looked up in one index of the data root's relative paths, so the check is
    linear in the files, not files times listed names.
    """
    if not project.analysis_unit_id:
        return {"required": False, "verified": 0, "skipped": 0}
    archive_downloads = {
        str(item.get("source_url") or ""): item for item in downloads or [] if item.get("archive")
    }
    index: dict[str, list[Path]] = {}
    for path in data_root.rglob("*"):
        if not path.is_file():
            continue
        relative = _relative_to_data_root(path, data_root)
        if relative is None:
            continue
        for form in _allowlist_forms(relative):
            index.setdefault(form, []).append(path)
    expanded: dict[str, list[tuple[dict[str, Any], dict[str, Any], str]]] = {}
    for record in archive_extractions or []:
        placement = str(record.get("placement") or "")
        for nested in _nested_records(record):
            relative = "/".join(part for part in (placement, str(nested.get("archive_path") or "")) if part)
            for form in _allowlist_forms(relative.casefold()):
                expanded.setdefault(form, []).append((record, nested, relative))
    verified = 0
    skipped = 0
    archives_verified: list[dict[str, Any]] = []
    expanded_verified: list[dict[str, Any]] = []
    for item in project.files:
        algorithm, checksum = _listed_checksum(item)
        if not checksum:
            skipped += 1
            continue
        download = archive_downloads.get(item.url)
        if download is not None and _names_archive_object(item, download):
            archives_verified.append(_verify_archive_object(item, download, algorithm, checksum))
            continue
        expected = _safe_relative_name(item.name).as_posix().casefold()
        matches = index.get(expected, [])
        if not matches and len(expanded.get(expected, [])) == 1:
            record, nested, relative = expanded[expected][0]
            expanded_verified.append(
                _verify_expanded_archive(item, record, nested, relative, algorithm, checksum)
            )
            verified += 1
            continue
        if len(matches) != 1:
            raise ValueError(
                f"Allow-listed file {item.name} resolved to {len(matches)} extracted files."
            )
        digest = hashlib.new(algorithm)
        with matches[0].open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if digest.hexdigest().casefold() != checksum:
            raise ValueError(f"{algorithm.upper()} checksum mismatch for {item.name}.")
        verified += 1
        if per_file is not None:
            per_file[_file_key(str(matches[0]))] = {
                "declared": checksum,
                "declared_algorithm": algorithm,
                "declared_name": item.name,
                "verified": True,
            }
    result: dict[str, Any] = {
        "required": verified + len(archives_verified) > 0,
        "verified": verified,
        "skipped": skipped,
    }
    if archive_downloads:
        # Only where an archive was downloaded, so a unit of files listed one by one records what it
        # always did.
        result["archives_verified_at_download"] = len(archives_verified)
        result["archives"] = archives_verified + expanded_verified
    return result


def _nested_records(record: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every archive expanded inside an extraction record, depth first."""
    for nested in record.get("nested") or []:
        if isinstance(nested, dict):
            yield nested
            yield from _nested_records(nested)


def _verify_expanded_archive(
    item: RepositoryFile,
    record: dict[str, Any],
    nested: dict[str, Any],
    relative: str,
    algorithm: str,
    checksum: str,
) -> dict[str, Any]:
    """Compare a listed file that was an archive inside another with its digests from before it expanded."""
    actual = str(nested.get(f"archive_{algorithm}") or "")
    if not actual:
        raise ValueError(
            f"Allow-listed file {item.name} was expanded inside {record.get('archive_name')} and its "
            f"{algorithm.upper()} was not recorded before it expanded, so it cannot be compared."
        )
    if actual.casefold() != checksum:
        raise ValueError(f"{algorithm.upper()} checksum mismatch for {item.name}.")
    nested["declared_name"] = item.name
    nested["declared_checksum"] = checksum
    nested["declared_checksum_algorithm"] = algorithm
    nested["declared_checksum_verified"] = True
    return {
        "name": item.name,
        "role": item.role,
        "url": item.url,
        "archive_path": relative,
        "inside": str(record.get("archive_name") or ""),
        "declared": checksum,
        "declared_algorithm": algorithm,
        "compared": "before_expansion",
        "verified": True,
    }


def _verify_archive_object(
    item: RepositoryFile, download: dict[str, Any], algorithm: str, checksum: str
) -> dict[str, Any]:
    """Compare a listed archive's declared checksum with the downloaded archive. Raises on a mismatch.

    An MD5 was compared as the bytes arrived (_verify_object_checksum), and that comparison is what
    is counted; a SHA-256 is compared with the one the download computed; a SHA-1 is computed now,
    from the archive, which is kept in raw\\downloads beside its extracted tree.
    """
    at_download = (
        algorithm == "md5"
        and download.get("declared_checksum_verified")
        and str(download.get("declared_checksum") or "").strip().casefold() == checksum
    )
    if at_download:
        actual = str(download.get("md5") or "")
    elif algorithm in ("md5", "sha256"):
        actual = str(download.get(algorithm) or "")
    else:
        digest = hashlib.new(algorithm)
        with Path(str(download["path"])).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        actual = digest.hexdigest()
    if actual.casefold() != checksum:
        raise ValueError(f"{algorithm.upper()} checksum mismatch for {item.name}.")
    download["declared_checksum_verified"] = True
    download["declared_checksum_algorithm"] = algorithm
    return {
        "name": item.name,
        "role": item.role,
        "url": item.url,
        "download_path": str(download.get("path") or ""),
        "declared": checksum,
        "declared_algorithm": algorithm,
        "compared": "as_downloaded" if at_download else "after_download",
        "verified": True,
    }


def _safe_relative_name(value: str) -> Path:
    normalized = value.replace("\\", "/").lstrip("/")
    if normalized.casefold().startswith("files/"):
        normalized = normalized[6:]
    parts = [part for part in Path(normalized).parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise ValueError(f"Unsafe repository file path: {value}")
    return Path(*parts)


def _archive_project_results(output: Path) -> Path | None:
    if not output.is_dir():
        return None
    # The containers moved out of the raw tree are retained file by file; zipping them again here would
    # keep every one of them twice.
    project_files = [
        path for path in output.rglob("*")
        if path.is_file()
        and path.suffix.casefold() in PROJECT_RESULT_SUFFIXES
        and not is_diagnostic_artifact(path)
        and not is_intermediate_artifact(path, output)
    ]
    if not project_files:
        return None
    archive = output / "msdial-project-artifacts.zip"
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as handle:
        for path in sorted(project_files):
            handle.write(path, path.relative_to(output).as_posix())
    with zipfile.ZipFile(archive) as handle:
        if handle.testzip() is not None:
            archive.unlink(missing_ok=True)
            raise ValueError("The MS-DIAL project artifact ZIP failed its integrity check.")
    return archive.resolve()


def _artifact_inventory(path: Path) -> dict[str, Any]:
    digest = hashlib.sha256()
    # A moved container's path can be longer than MAX_PATH.
    with open(extended_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    entry = {
        "path": str(path.resolve()),
        "size_bytes": os.stat(extended_path(path)).st_size,
        "sha256": digest.hexdigest(),
    }
    # The two kinds a reader must not mistake for ordinary results: a record that holds this machine's
    # locations and never leaves it, and an MS-DIAL container moved out of the raw tree.
    if path.name.casefold().endswith(".local.json"):
        entry["sharing"] = "local_only"
    elif INTERMEDIATES_DIRECTORY in path.parts[:-1]:
        entry["role"] = "msdial_intermediate"
    return entry


def _extract_into_data_root(
    archive: Path,
    download: dict[str, Any],
    placement: str,
    data_root: Path,
    raw_root: Path,
    provenance: Path,
    number: int,
    limits: ExtractionLimits | None = None,
    earlier: Iterable[dict[str, Any]] = (),
) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
    """Expand one downloaded archive into the unit's data root. Returns (record, members).

    archives.extract_archive builds the tree in a staging directory of its own, raw\\x<number>, which
    exists only once the tree matches the archive's listing, and writes that listing, for the whole
    lineage of nested archives, to provenance\\archive-members-<sha12>.tsv with its sha256. The tree is
    then moved under the data root, at placement (see _route_object), entry by entry. When an earlier
    archive of this lease had the same bytes under another name (``earlier``), its listing is not
    written over: this one goes to provenance\\archive-members\\<number>\\.

    NOTHING ALREADY THERE IS OVERWRITTEN. The old extraction wrote every archive into the data root
    with open("wb"), so two per-sample zips whose members sit at their root (_FUNC001.DAT) wrote over
    each other and the unit analysed whichever came last. archives.py now gives such an archive a
    folder of its own; here a file already in place with the same bytes is left as it is and counted
    as already present, which is what a retried lease finds and what two archives carrying one
    identical file give, and anything else refuses the whole archive (extraction_collision) before
    a single entry moves.

    record is extract_archive's, with destination set to where the members now are (the staging
    directory it verified them in is staging_destination), the placement, the archive's download and
    what the move did. members is one (data-root path, {member, archive}) per file that came out: its
    path in the listing, and the archive, outer or nested, that the listing says it came from.
    """
    limits = LEASE_EXTRACTION_LIMITS if limits is None else limits
    staging = raw_root / f"x{number}"
    if os.path.lexists(staging):
        # Verified and not yet moved when an earlier lease stopped; nothing else ever refers to it.
        removal = unlink_tree(staging)
        if not removal["complete"]:
            raise ArchiveError(
                "staging_not_removed",
                f"{staging}, left by an earlier lease, could not be removed: {removal['kept'][:3]}",
            )
    sha256 = str(download.get("sha256") or "")
    listing_directory = provenance
    if sha256 and any(str(record.get("archive_sha256") or "") == sha256 for record in earlier):
        listing_directory = provenance / "archive-members" / str(number)
    record = archives.extract_archive(
        archive,
        staging,
        archive_sha256=sha256,
        listing_directory=listing_directory,
        limits=limits,
    )
    target = data_root.joinpath(*placement.split("/")) if placement else data_root
    try:
        rows = [
            row for row in _read_members_listing(Path(record["members_tsv"]["path"]))
            if row.get("disposition") == "extracted"
        ]
        _refuse_long_final_paths(rows, target, limits, archive.name)
        merge = _merge_extracted_tree(staging, target, archive.name)
    finally:
        # What is left is directories the move emptied and files that were already in place.
        unlink_tree(staging)
    members: list[tuple[str, dict[str, Any]]] = [
        (str(target.joinpath(*row["path"].split("/"))), {"member": row["path"], "archive": row["archive"]})
        for row in rows
        if row.get("type") == "file"
    ]
    container_root = str(record.get("container_root") or "")
    return (
        {
            **record,
            "staging_destination": record["destination"],
            "destination": str(target),
            "placement": placement,
            # The container an archived container produced, relative to the data root.
            "container_path": "/".join(part for part in (placement, container_root) if part),
            "source_url": str(download.get("source_url") or ""),
            "download_path": str(download.get("path") or ""),
            "merge": merge,
            "extracted_file_count": len(members),
        },
        members,
    )


def _refuse_long_final_paths(
    rows: list[dict[str, str]], target: Path, limits: ExtractionLimits, label: str
) -> None:
    """Refuse an archive whose members would be too long for the Console where they finally land.

    archives.py holds every path to the limits under the staging directory it extracts into. The
    members then move under the data root, at their placement, which can be longer: a per-sample
    container listed in FILES/RAW_FILES/pos/ lands nine characters deeper than raw\\x<n> put it.
    LongPathsEnabled is 0 on the campaign host and the .NET Framework Console opens MAX_PATH paths.
    """
    base = len(str(target))
    too_long = []
    for row in rows:
        path = str(row.get("path") or "")
        folder = path if row.get("type") == "dir" else path.rpartition("/")[0]
        if base + 1 + len(path) > limits.max_path_length or (
            folder and base + 1 + len(folder) > limits.max_directory_length
        ):
            too_long.append({"name": path, "reason": "path_too_long"})
    if too_long:
        raise ArchiveError(
            "unsafe_listing",
            f"{label} was refused before it moved under {target}: {len(too_long)} member path(s) would be "
            f"longer than {limits.max_path_length} characters there, for example {too_long[0]['name']!r}.",
            rejected=too_long[:50],
        )


def _same_bytes(left: Path, right: Path) -> bool:
    if left.stat().st_size != right.stat().st_size:
        return False
    with left.open("rb") as first, right.open("rb") as second:
        while True:
            chunk = first.read(1024 * 1024)
            if chunk != second.read(1024 * 1024):
                return False
            if not chunk:
                return True


def _merge_extracted_tree(source: Path, target: Path, label: str) -> dict[str, Any]:
    """Move a verified tree under target, planned in full first, so a collision moves nothing."""
    moves: list[tuple[Path, Path]] = []
    already_present: list[str] = []
    collisions: list[dict[str, str]] = []
    pending = [(source, target, "")]
    while pending:
        here, there, relative = pending.pop()
        with os.scandir(here) as entries:
            children = sorted(entries, key=lambda entry: entry.name)
        for entry in children:
            destination = there / entry.name
            path = f"{relative}/{entry.name}" if relative else entry.name
            if not os.path.lexists(destination):
                moves.append((Path(entry.path), destination))
                continue
            existing = os.lstat(destination)
            linked = stat.S_ISLNK(existing.st_mode) or bool(
                getattr(existing, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            )
            if entry.is_dir(follow_symlinks=False):
                if stat.S_ISDIR(existing.st_mode) and not linked:
                    pending.append((Path(entry.path), destination, path))
                    continue
            elif stat.S_ISREG(existing.st_mode) and not linked and _same_bytes(Path(entry.path), destination):
                already_present.append(path)
                continue
            collisions.append({"path": path, "existing": str(destination)})
    if collisions:
        raise ArchiveError(
            "extraction_collision",
            f"{label} would write {len(collisions)} path(s) under {target} that already hold something "
            f"else, for example {collisions[0]['path']!r}. Nothing was moved or overwritten.",
            detail={"collisions": collisions[:20], "collision_count": len(collisions)},
        )
    target.mkdir(parents=True, exist_ok=True)
    for moving, destination in moves:
        os.replace(moving, destination)
    return {
        "moved_entries": len(moves),
        "already_present_files": len(already_present),
        "already_present": already_present[:50],
    }


def _read_members_listing(path: Path) -> list[dict[str, str]]:
    """The rows of an archive-members TSV that archives.extract_archive wrote.

    Split on newlines only: a member name may hold U+2028 or U+0085, which str.splitlines would read
    as line ends, while archives.py refuses control characters, tabs and newlines in names.
    """
    lines = path.read_text(encoding="utf-8").split("\n")
    header = lines[0].split("\t")
    return [dict(zip(header, line.split("\t"))) for line in lines[1:] if line]


def _archive_warnings(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    """What the extraction records say a reader of the unit should know, outer and nested archives alike.

    - container_name_mismatch: X.raw.zip holds Y.raw. It is extracted as packed; renaming it would
      decide which sample it is, so it is attributed only where a sample or a listed file names Y.raw.
    - nested_skipped: an archive inside an archive that was left packed, and why.
    - dropped_metadata: operating-system metadata (__MACOSX/, .DS_Store, ._ files, Thumbs.db) that
      was validated and not written.
    - no_member_integrity: a format with no per-member check (tar, LZMA-alone), whose members rest
      on the archive's own hash, or on their own published checksums where the repository lists
      them (MB-POST).
    """
    warnings: list[dict[str, str]] = []

    def visit(record: dict[str, Any]) -> None:
        name = str(record.get("archive_path") if record.get("depth", 1) != 1 else record.get("archive_name"))
        if record.get("container_name_mismatch"):
            warnings.append({
                "archive": name,
                "kind": "container_name_mismatch",
                "message": (
                    f"{name} stands for {archives.container_alias(str(record.get('archive_name') or ''))} but "
                    f"holds {record.get('container_root')}; it was extracted as packed and is attributed only "
                    "where a sample or a listed file names what it holds."
                ),
            })
        for skipped in record.get("nested_skipped") or []:
            warnings.append({
                "archive": name,
                "kind": "nested_skipped",
                "message": f"{skipped.get('path')} inside {name} was left packed ({skipped.get('reason')}).",
            })
        dropped = record.get("dropped_metadata") or {}
        if dropped.get("members"):
            warnings.append({
                "archive": name,
                "kind": "dropped_metadata",
                "message": (
                    f"{dropped['members']} operating-system metadata member(s) of {name} were not written: "
                    + ", ".join(str(entry) for entry in (dropped.get("entries") or [])[:5])
                ),
            })
        if record.get("crc_verified") is False:
            warnings.append({
                "archive": name,
                "kind": "no_member_integrity",
                "message": (
                    f"{name} is {record.get('format')}, which carries no check of its members; what came "
                    "out of it is vouched for by the archive's hash, or by its own published checksum "
                    "where one was compared."
                ),
            })
        for nested in record.get("nested") or []:
            visit(nested)

    for record in records:
        visit(record)
    return warnings


def _find_msdial_inputs(root: Path) -> list[str]:
    paths = sorted(root.rglob("*"))
    vendor_roots = {
        path.resolve()
        for path in paths
        if path.is_dir() and path.suffix.casefold() in {".d", ".raw"}
    }
    result = [str(path) for path in sorted(vendor_roots)]
    for path in paths:
        lower = path.name.casefold()
        if not path.is_file() or _is_sidecar_name(path.name):
            continue
        resolved = path.resolve()
        if any(parent in vendor_roots for parent in resolved.parents):
            continue
        if path.suffix.casefold() in RAW_SUFFIXES:
            result.append(str(resolved))
    return result


def _common_input_path(inputs: list[str], fallback: Path) -> str:
    if not inputs:
        return str(fallback.resolve())
    paths = [Path(value).resolve() for value in inputs]
    parents = {path if path.is_dir() else path.parent for path in paths}
    if len(parents) == 1:
        return str(next(iter(parents)))
    return str(fallback.resolve())


def _summarize_raw_metadata(records: list[dict[str, Any]]) -> dict[str, Any]:
    separations = {_metadata_value(item, "acquisition", "separation") for item in records}
    methods = {_metadata_value(item, "acquisition", "method") for item in records}
    polarities = {_metadata_value(item, "acquisition", "polarity") for item in records}
    separations.discard("")
    methods.discard("")
    polarities.discard("")
    separation_map = {
        "LiquidChromatography": "LC-MS",
        "GasChromatography": "GC-MS",
    }
    separation_values = {separation_map.get(value, "Unknown") for value in separations}
    separation_values.discard("Unknown")
    acquisition_values = {
        value for value in methods if value in {"FullScan", "DDA", "DIA", "AIF", "SIM", "MRM", "SRM"}
    }
    if len(polarities) > 1 or "PolaritySwitching" in polarities or "MixedFunctions" in polarities:
        ion_mode = "Both"
    else:
        ion_mode = next(iter(polarities), "Unknown")
    # MIXED IS NOT UNKNOWN. When the headers disagree, this used to answer "Unknown" - the same
    # word it uses when no header said anything - and the caller treats "Unknown" as "keep what
    # the repository metadata said". For MetaboLights MTBLS2207 the repository metadata said DIA
    # with no evidence behind it; eleven headers said six DDA and five DIA; and the stale DIA
    # survived the preflight. Confirming untargeted status afterwards would have made the unit
    # eligible as DIA, and agent_workflow stamps the unit's single mode onto every file, so the
    # six DDA files would have been deconvoluted as SWATH.
    #
    # "Mixed" says what was observed: the files disagree, so no single mode describes the unit,
    # and the per-file verdicts below are what a split has to be made from.
    if len(acquisition_values) > 1:
        acquisition_mode = "Mixed"
    else:
        acquisition_mode = next(iter(acquisition_values), "Unknown")
    per_file = []
    for item in records:
        source = item.get("source") or {}
        acquisition = item.get("acquisition") or {}
        method = acquisition.get("method")
        ms_levels = acquisition.get("msLevels")
        if isinstance(ms_levels, dict):
            ms_levels = ms_levels.get("value")
        per_file.append(
            {
                "file": str(source.get("filePath") or source.get("fileName") or ""),
                "acquisition_mode": _metadata_value(item, "acquisition", "method"),
                "confidence": method.get("confidence") if isinstance(method, dict) else None,
                "evidence": method.get("evidence") if isinstance(method, dict) else None,
                "polarity": _metadata_value(item, "acquisition", "polarity"),
                "ms_levels": ms_levels,
                # The extractor reads it from the file itself (mzML run@startTimeStamp, vendor
                # headers); it is the injection order the analysis CSV should carry.
                "acquisition_start_time": _metadata_value(item, "run", "acquisitionStartTime"),
                "acquisition_start_time_evidence": str(
                    ((item.get("run") or {}).get("acquisitionStartTime") or {}).get("evidence") or ""
                )
                if isinstance((item.get("run") or {}).get("acquisitionStartTime"), dict)
                else "",
            }
        )
    return {
        "files_inspected": len(records),
        "separation": next(iter(separation_values), "Unknown") if len(separation_values) <= 1 else "Unknown",
        "acquisition_mode": acquisition_mode,
        "per_file": per_file,
        "ion_mode": ion_mode if ion_mode in {"Positive", "Negative", "Both"} else "Unknown",
        "observed_separations": sorted(separations),
        "observed_acquisition_methods": sorted(methods),
        "observed_polarities": sorted(polarities),
        "evidence": [f"Raw metadata preflight inspected {len(records)} representative file(s)."],
    }


ACQUISITION_ORDER_SOURCE = "raw_header_acquisition_start_time"
# Where a unit's analytical order came from when the headers did not give it, in the words
# sample_grouping.propose_injection_order already uses for the last two.
DECLARED_ORDER_SOURCE = "repository_sample_table"
EMBEDDED_ORDER_SOURCE = "embedded"
LISTING_ORDER_SOURCE = "listing"
_FRACTION = re.compile(r"(T\d{2}:\d{2}:\d{2})\.(\d+)")


def _parse_acquisition_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    # The extractor writes .NET fractions of 1 to 7 digits; Python before 3.11 reads only 3 or 6.
    text = _FRACTION.sub(lambda match: match.group(1) + "." + (match.group(2) + "000000")[:6], text, count=1)
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _preflight_start_times(preflight: dict[str, Any]) -> tuple[dict[str, tuple[str, str]], set[str]]:
    times: dict[str, tuple[str, str]] = {}
    inspected: set[str] = set()
    for item in (preflight.get("summary") or {}).get("per_file") or []:
        if not isinstance(item, dict) or not item.get("file"):
            continue
        inspected.add(_file_key(str(item["file"])))
        value = str(item.get("acquisition_start_time") or "").strip()
        if value:
            times[_file_key(str(item["file"]))] = (
                value, str(item.get("acquisition_start_time_evidence") or "")
            )
    output = Path(str(preflight.get("output") or ""))
    if output.is_file():
        try:
            records = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            records = []
        if isinstance(records, dict):
            records = [records]
        for record in records if isinstance(records, list) else []:
            if not isinstance(record, dict):
                continue
            source = record.get("source") or {}
            path = str(source.get("filePath") or "").strip() if isinstance(source, dict) else ""
            if path:
                inspected.add(_file_key(path))
            value = _metadata_value(record, "run", "acquisitionStartTime").strip()
            if path and value:
                field = (record.get("run") or {}).get("acquisitionStartTime") or {}
                evidence = str(field.get("evidence") or "") if isinstance(field, dict) else ""
                times.setdefault(_file_key(path), (value, evidence))
    return times, inspected


def _acquisition_start_times(
    manifest: dict[str, Any],
) -> tuple[dict[str, tuple[str, str]], set[str], bool]:
    """The acquisition start time, and the extractor's evidence for it, of each inspected file.

    Read from the unit's own preflight, then from the extractor output it names (a summary
    written before the time was carried forward has none), then from the parent a split part
    was made from. The flag says whether any preflight was found at all, so "no header
    recorded a time" is not said of headers nobody read.
    """
    sources = [manifest.get("raw_metadata_preflight") or {}]
    split_from = manifest.get("split_from")
    parent_path = str(split_from.get("manifest_path") or "") if isinstance(split_from, dict) else ""
    if parent_path and Path(parent_path).is_file():
        try:
            parent = read_manifest(parent_path)
        except (OSError, ValueError):
            parent = {}
        if isinstance(parent, dict):
            sources.append(parent.get("raw_metadata_preflight") or {})
    times: dict[str, tuple[str, str]] = {}
    inspected: set[str] = set()
    read = False
    for preflight in (source for source in sources if isinstance(source, dict)):
        if (preflight.get("summary") or {}).get("per_file") or Path(str(preflight.get("output") or "")).is_file():
            read = True
        found, seen = _preflight_start_times(preflight)
        for key, value in found.items():
            times.setdefault(key, value)
        inspected |= seen
    return times, inspected, read


def acquisition_start_order(
    manifest: dict[str, Any],
    file_paths: list[str],
    class_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Rank a unit's input files by the acquisition start time in their own headers.

    The analytical order was the file listing, or a number read out of the names, even when
    every raw header recorded when it was acquired. For MTBLS2207 the listing put a file
    acquired in December 2019 last and one acquired in September 2020 second, and the only
    QA criterion the run could evaluate was a run-order drift computed against that listing.

    The header order is used only when it orders something: every file needs a readable time,
    and files the headers cannot tell apart must not decide the order by listing position
    across Classes. So nothing is reordered, and the record says why, when a header was never
    read, when a file has no time or an unreadable one, when every file shares one time, or
    when files of different Classes share a time. Equal times within one Class keep listing
    order and are named.

    The extractor writes every time with an offset. Where the header itself carried none it
    supplied one, which cannot be told apart here, so each file's evidence is kept with its
    time rather than presenting the offset as a header fact.
    """
    times, inspected, read = _acquisition_start_times(manifest)
    if not read:
        return {
            "derived_from": None,
            "headers_read": False,
            "reason": (
                "The raw headers of this unit have not been read (no raw-metadata preflight), "
                "so the order was not taken from them."
            ),
            "missing": [],
            "unreadable": [],
        }
    entries = []
    missing = []
    unreadable = []
    not_inspected = []
    for index, path in enumerate(file_paths):
        raw, evidence = times.get(_file_key(path), ("", ""))
        parsed = _parse_acquisition_time(raw)
        if not raw and _file_key(path) not in inspected:
            not_inspected.append(Path(path).name)
        elif not raw:
            missing.append(Path(path).name)
        elif parsed is None:
            unreadable.append(Path(path).name)
        else:
            entries.append((parsed, index, path, raw, evidence))
    if missing or unreadable or not_inspected or not entries:
        parts = []
        if not_inspected:
            parts.append("the raw header was not read for " + ", ".join(not_inspected))
        if missing:
            parts.append("no acquisition start time was recorded for " + ", ".join(missing))
        if unreadable:
            parts.append("the recorded time could not be read for " + ", ".join(unreadable))
        text = "; ".join(parts or ["no input file was given"])
        return {
            "derived_from": None,
            "headers_read": True,
            # Only the first letter: the rest holds file names, whose case matters.
            "reason": text[:1].upper() + text[1:] + "; the order was not taken from the headers.",
            "missing": missing,
            "unreadable": unreadable,
            "not_inspected": not_inspected,
        }
    if len({entry[0].tzinfo is None for entry in entries}) > 1:
        return {
            "derived_from": None,
            "headers_read": True,
            "reason": "Some acquisition start times carry a timezone and some do not, so they cannot be ordered.",
            "missing": [],
            "unreadable": [],
        }
    groups: dict[datetime, list[int]] = {}
    for entry in entries:
        groups.setdefault(entry[0], []).append(entry[1])
    if len(entries) > 1 and len(groups) == 1:
        return {
            "derived_from": None,
            "headers_read": True,
            "reason": "Every raw header records the same start time, so the headers carry no order.",
            "missing": [],
            "unreadable": [],
        }
    classes = list(class_ids or [])
    mixed = sorted(
        Path(file_paths[index]).name
        for indexes in groups.values()
        if len(indexes) > 1 and classes and len({classes[i] for i in indexes if i < len(classes)}) > 1
        for index in indexes
    )
    if mixed:
        return {
            "derived_from": None,
            "headers_read": True,
            "reason": (
                "Files of different Classes share a start time (" + ", ".join(mixed)
                + "), so the headers cannot order them and the listing would decide."
            ),
            "missing": [],
            "unreadable": [],
        }
    entries.sort(key=lambda entry: (entry[0], entry[1]))
    ranks = {entry[2]: rank for rank, entry in enumerate(entries, start=1)}
    tied = sorted(
        Path(file_paths[index]).name
        for indexes in groups.values()
        if len(indexes) > 1
        for index in indexes
    )
    return {
        "derived_from": ACQUISITION_ORDER_SOURCE,
        "headers_read": True,
        "reason": (
            "Ranked by the acquisition start time each file's raw header records"
            + (f"; equal times within one Class keep listing order ({', '.join(tied)})" if tied else "")
            + "."
        ),
        "orders": {_file_key(path): rank for path, rank in ranks.items()},
        "files": [
            {
                "file": Path(entry[2]).name,
                "acquisition_start_time": entry[3],
                "evidence": entry[4],
                "analytical_order": ranks[entry[2]],
            }
            for entry in entries
        ],
        "agrees_with_listing": [ranks[path] for path in file_paths] == list(range(1, len(file_paths) + 1)),
        "tied": tied,
    }


def with_order_source(
    record: dict[str, Any],
    files: list[dict[str, Any]],
    declared_order_files: Iterable[str],
    recognized: list[dict[str, Any]],
) -> dict[str, Any]:
    """The header decision, with what the analysis CSV's order was taken from in the end.

    The record said only whether the raw headers gave the order. When they did not, the order was
    the repository's declared one, a number read out of the file names, or the file listing, and
    nothing said which: a drift criterion computed against the listing read like one computed
    against a measured sequence. The source is recorded, and the files with the order they carry,
    so that a later reader can tell whether the CSV still has it. A source that cannot be told is
    None, never a guess.
    """
    result = dict(record)
    if result.get("derived_from") == ACQUISITION_ORDER_SOURCE:
        result["order_source"] = ACQUISITION_ORDER_SOURCE
        return result
    paths = [str(item.get("file_path", "")) for item in files]
    declared = {_file_key(path) for path in declared_order_files if str(path).strip()}
    declared_here = sum(1 for path in paths if _file_key(path) in declared)
    source: str | None
    if paths and declared_here == len(paths):
        source = DECLARED_ORDER_SOURCE
    else:
        from .sample_grouping import propose_injection_order

        # The order the files were recognised with: re-derived from the same names, and trusted only
        # where it gives every recognised file the order it has.
        names = [Path(str(item.get("file_path", ""))).stem for item in recognized]
        proposal = propose_injection_order(names) if names else {}
        consistent = bool(names) and all(
            str((proposal.get("orders") or {}).get(name)) == str(item.get("analytical_order"))
            for name, item in zip(names, recognized)
        )
        chosen = proposal.get("chosen") if consistent else None
        source = chosen if chosen in {EMBEDDED_ORDER_SOURCE, LISTING_ORDER_SOURCE} else None
    result["order_source"] = source
    if 0 < declared_here < len(paths):
        result["declared_files"] = declared_here
    result["files"] = [
        {"file": Path(str(item.get("file_path", ""))).name, "analytical_order": item.get("analytical_order")}
        for item in files
    ]
    return result


def recorded_order_match(
    manifest: dict[str, Any], record: dict[str, Any], files: list[dict[str, Any]]
) -> tuple[bool, bool]:
    """(same_unit, matches): whether the files are this unit's inputs, and whether they carry exactly
    the analytical order the record gives them.

    A record names files by name only, so another unit's manifest (a workset carries the path) could
    match on names and ranks alone; the files must also be that unit's inputs.
    """
    recorded = {
        Path(str(item.get("file", ""))).stem.casefold(): item.get("analytical_order")
        for item in record.get("files") or []
        if isinstance(item, dict)
    }
    candidates = {
        str(Path(str(path)).resolve()).casefold()
        for path in manifest.get("input_candidates") or []
        if str(path).strip()
    }
    same_unit = bool(candidates) and all(
        str(Path(str(item.get("file_path", ""))).resolve()).casefold() in candidates
        for item in files
    )
    in_csv: dict[str, Any] = {}
    duplicated = False
    for item in files:
        name = str(item.get("file_name", "")).casefold()
        duplicated = duplicated or name in in_csv
        in_csv[name] = item.get("analytical_order")
    matches = (
        same_unit
        and not duplicated
        and len(recorded) == len(files)
        and all(
            name in recorded and str(recorded[name]) == str(order)
            for name, order in in_csv.items()
        )
    )
    return same_unit, matches


def carries_recorded_order(record: dict[str, Any], files: list[dict[str, Any]]) -> bool:
    """Whether the files keep the relative order the record gives them.

    A file dropped since (a raw file missing at plan time drops its row), or the remaining ranks
    renumbered, leaves the order the record describes; a reordered one does not. Every file must be
    recorded, once, with a rank that reads as a whole number and that no other recorded file has.
    """
    recorded: dict[str, int] = {}
    for item in record.get("files") or []:
        if not isinstance(item, dict):
            return False
        stem = Path(str(item.get("file", ""))).stem.casefold()
        rank = _whole_number(item.get("analytical_order"))
        if not stem or stem in recorded or rank is None or rank in recorded.values():
            return False
        recorded[stem] = rank
    carried: list[tuple[int, int]] = []
    seen: set[str] = set()
    for item in files:
        name = str(item.get("file_name", "")).casefold()
        rank = _whole_number(item.get("analytical_order"))
        if not name or name in seen or name not in recorded or rank is None:
            return False
        seen.add(name)
        carried.append((recorded[name], rank))
    carried.sort()
    return bool(carried) and all(later[1] > earlier[1] for earlier, later in zip(carried, carried[1:]))


def _whole_number(value: Any) -> int | None:
    text = str(value if value is not None else "").strip()
    return int(text) if text.isdecimal() else None


def recorded_order_source(manifest_path: Any, files: list[dict[str, Any]]) -> str | None:
    """Where the analysis CSV's analytical order came from, as the unit manifest records it.

    None unless the files are among the unit's inputs (by name) and keep the relative order the
    record gives them: a CSV reordered since carries an order nobody recorded. A header record from
    before order_source existed reads as the header source; any other record from before it reads
    as unknown. The test is looser than adopted_order_proposal's exact match, because its answer is
    only ever used to withhold a criterion, never to call an order measured.
    """
    if not str(manifest_path or "").strip():
        return None
    try:
        manifest = read_manifest(str(manifest_path))
    except (OSError, ValueError):
        return None
    record = manifest.get("analytical_order") if isinstance(manifest, dict) else None
    if not isinstance(record, dict):
        return None
    source = record.get("order_source") or (
        ACQUISITION_ORDER_SOURCE if record.get("derived_from") == ACQUISITION_ORDER_SOURCE else None
    )
    if not source:
        return None
    inputs = {
        Path(str(path).replace("\\", "/")).name.casefold()
        for path in manifest.get("input_candidates") or []
        if str(path).strip()
    }
    files = list(files or [])
    same_unit = bool(inputs) and all(
        Path(str(item.get("file_path", "")).replace("\\", "/")).name.casefold() in inputs for item in files
    )
    return str(source) if same_unit and carries_recorded_order(record, files) else None


def record_analytical_order(manifest_path: str | Path, record: dict[str, Any]) -> None:
    """Keep how the analysis CSV's analytical order was decided in the unit's own manifest."""
    kept = {key: value for key, value in record.items() if key != "orders"} | {
        "recorded_at": datetime.now(timezone.utc).isoformat()
    }

    def change(manifest: dict[str, Any]) -> None:
        manifest["analytical_order"] = kept

    update_manifest(Path(manifest_path), change)


def _metadata_value(record: dict[str, Any], section: str, field_name: str) -> str:
    value = record.get(section, {}).get(field_name, {})
    if isinstance(value, dict):
        value = value.get("value")
    return str(value or "")


def _parse_tab_blocks(text: str) -> list[dict[str, str]]:
    blocks = []
    current: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                blocks.append(current)
                current = {}
            continue
        key, separator, value = line.partition("\t")
        if separator:
            current[key.strip().lower()] = value.strip()
    if current:
        blocks.append(current)
    return blocks


def _parse_workbench_records(text: str) -> list[dict[str, Any]]:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return _parse_tab_blocks(text)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if all(not isinstance(value, dict) for value in payload.values()):
            return [payload]
        return [item for item in payload.values() if isinstance(item, dict)]
    return []


def _parse_workbench_downloads(text: str, base: str) -> list[RepositoryFile]:
    pattern = re.compile(
        r'<a\s+href=["\'](?P<href>[^"\']+)["\'][^>]*>(?P<name>[^<]+)</a>\s*'
        r'<b>\((?P<size>[^)]+)\)</b>\s*\(Checksum:(?P<checksum>[a-fA-F0-9]+)\)',
        re.IGNORECASE,
    )
    return [
        RepositoryFile(
            name=html.unescape(match.group("name")),
            size_bytes=_parse_size(match.group("size")),
            url=urllib.parse.urljoin(base, match.group("href")),
            checksum=match.group("checksum").lower(),
        )
        for match in pattern.finditer(text)
    ]


def _raw_names_from_assay(text: str) -> set[str]:
    if text.lstrip().startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = {}
        result = set()
        for row in payload.get("data", {}).get("rows", []):
            value = str(row.get("Raw Spectral Data File") or row.get("Derived Spectral Data File") or "").strip()
            if value:
                result.add(value)
        return result
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return set()
    headers = lines[0].split("\t")
    raw_indices = [index for index, value in enumerate(headers) if "raw spectral data file" in value.casefold()]
    derived_indices = [index for index, value in enumerate(headers) if "derived spectral data file" in value.casefold()]
    result = set()
    for line in lines[1:]:
        values = line.split("\t")
        raw = next((values[index].strip() for index in raw_indices if index < len(values) and values[index].strip()), "")
        derived = next((values[index].strip() for index in derived_indices if index < len(values) and values[index].strip()), "")
        if raw or derived:
            result.add(raw or derived)
    return result


def _workbench_sample_metadata(rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        sample_id = str(
            row.get("local_sample_id")
            or row.get("sample_id")
            or row.get("sample")
            or f"sample_{index}"
        ).strip()
        values = {}
        values.update(_parse_name_value_pairs(str(row.get("factors") or "")))
        values.update(
            _parse_name_value_pairs(
                str(row.get("additional_sample_data") or row.get("additional sample data") or "")
            )
        )
        for key, value in row.items():
            if key not in {"study_id", "local_sample_id", "sample_id", "sample", "factors"}:
                values.setdefault(_display_field_name(key), str(value or "").strip())
        result.append(
            {
                "sample_id": sample_id,
                "source_name": str(row.get("subject_id") or row.get("subject") or sample_id),
                "raw_file": str(
                    row.get("raw_data")
                    or row.get("raw data")
                    or row.get("raw_file")
                    or row.get("filename")
                    or sample_id
                ),
                "values": values,
            }
        )
    return result


def _metabolights_sample_metadata(
    text: str, study: dict[str, Any], study_table_text: str = ""
) -> list[dict[str, Any]]:
    assay_rows = _assay_rows(text)
    material_values = _metabolights_material_values(study)
    for key, values in _metabolights_study_table_values(study_table_text).items():
        material_values.setdefault(key, {}).update(values)
    result = []
    for index, row in enumerate(assay_rows, start=1):
        raw_file = _first_matching_value(
            row,
            ("raw spectral data file", "derived spectral data file", "raw data file"),
        )
        sample_id = _first_matching_value(
            row,
            ("sample name", "source name", "sample identifier"),
        ) or Path(raw_file.replace("\\", "/")).stem or f"sample_{index}"
        source_name = _first_matching_value(row, ("source name",)) or sample_id
        values = dict(material_values.get(sample_id.casefold(), {}))
        values.update(material_values.get(source_name.casefold(), {}))
        for key, value in row.items():
            name = _display_field_name(key)
            if name.casefold() in {
                "raw spectral data file",
                "derived spectral data file",
                "raw data file",
                "sample name",
                "source name",
            }:
                continue
            text_value = _metadata_scalar(value)
            if text_value:
                values[name] = text_value
        result.append(
            {
                "sample_id": sample_id,
                "source_name": source_name,
                "raw_file": raw_file,
                "values": values,
            }
        )
    return result


def _metabolights_study_table_values(text: str) -> dict[str, dict[str, str]]:
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return {}
    result: dict[str, dict[str, str]] = {}
    for row in csv.DictReader(lines, delimiter="\t"):
        sample_name = _first_matching_value(row, ("sample name",))
        if not sample_name:
            continue
        values = result.setdefault(sample_name.casefold(), {})
        for key, value in row.items():
            text_value = _metadata_scalar(value)
            match = re.match(r"(?i)(?:characteristics|factor value)\[(.+)]$", str(key).strip())
            if match and text_value:
                values[_display_field_name(match.group(1))] = text_value
    return result


def _mbpost_sample_metadata(
    project: dict[str, Any],
    raw_items: Iterable[dict[str, Any]],
    details: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    shared = {}
    for key in ("organism", "species", "tissue", "sampleType", "keywords"):
        value = _metadata_scalar(project.get(key))
        if value:
            shared[_display_field_name(key)] = value
    described_samples = project.get("samples") or project.get("sampleList") or []
    by_name = {}
    if isinstance(described_samples, list):
        for sample in described_samples:
            if not isinstance(sample, dict):
                continue
            name = _metadata_scalar(
                sample.get("name") or sample.get("sampleName") or sample.get("id")
            )
            if name:
                by_name[name.casefold()] = {
                    _display_field_name(key): _metadata_scalar(value)
                    for key, value in sample.items()
                    if key not in {"name", "sampleName", "id"} and _metadata_scalar(value)
                }
    result = []
    for index, item in enumerate(raw_items, start=1):
        raw_file = str(item.get("name") or "").strip()
        sample_id = Path(raw_file.replace("\\", "/")).stem or f"sample_{index}"
        values = dict(shared)
        values.update(by_name.get(sample_id.casefold(), {}))
        detail = (details or {}).get(raw_file.casefold(), {})
        for preset_group in detail.get("presets", []) or []:
            category = _display_field_name(preset_group.get("category") or "metadata")
            for preset in preset_group.get("presets", []) or []:
                value = _metadata_scalar(preset.get("value"))
                if not value:
                    continue
                label = _display_field_name(preset.get("label") or preset.get("key"))
                values[f"{category} / {label}"] = value
        result.append(
            {
                "sample_id": sample_id,
                "source_name": sample_id,
                "raw_file": raw_file,
                "values": values,
            }
        )
    return result


def _assay_rows(text: str) -> list[dict[str, Any]]:
    stripped = text.lstrip()
    if stripped.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return []
        rows = payload.get("data", {}).get("rows", [])
        return [row for row in rows if isinstance(row, dict)]
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []
    return [dict(row) for row in csv.DictReader(lines, delimiter="\t")]


def _metabolights_material_values(study: dict[str, Any]) -> dict[str, dict[str, str]]:
    result: dict[str, dict[str, str]] = {}
    materials = study.get("materials", {})
    for group_name in ("sources", "samples"):
        for item in materials.get(group_name, []) or []:
            if not isinstance(item, dict):
                continue
            name = _metadata_scalar(item.get("name"))
            if not name:
                continue
            values = result.setdefault(name.casefold(), {})
            for collection in ("characteristics", "factorValues"):
                for entry in item.get(collection, []) or []:
                    if not isinstance(entry, dict):
                        continue
                    field_name = _annotation_text(
                        entry.get("category") or entry.get("factorName") or entry.get("name")
                    )
                    field_value = _annotation_text(
                        entry.get("value") or entry.get("annotationValue")
                    )
                    if field_name and field_value:
                        values[field_name] = field_value
    return result


def _parse_name_value_pairs(value: str) -> dict[str, str]:
    result = {}
    for part in re.split(r"\s*\|\s*|\s*;\s*", value.strip()):
        key, separator, item = part.partition(":")
        if not separator:
            key, separator, item = part.partition("=")
        if separator and key.strip():
            result[_display_field_name(key)] = item.strip()
    return result


def _extract_publications(*values: Any) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for value in values:
        candidates = value if isinstance(value, list) else [value]
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            lowered = {str(key).casefold(): item for key, item in candidate.items()}
            doi = _metadata_scalar(
                lowered.get("doi") or lowered.get("publication doi") or lowered.get("pubdoi")
            )
            pubmed = _metadata_scalar(
                lowered.get("pubmedid") or lowered.get("pubmed id") or lowered.get("pmid")
            )
            if pubmed.casefold().startswith("10.") and "/" in pubmed:
                doi = doi or pubmed
                pubmed = ""
            title = _metadata_scalar(
                lowered.get("title") or lowered.get("publication title") or lowered.get("citation")
            )
            if doi or pubmed or title:
                record = {"title": title, "doi": doi, "pubmed_id": pubmed}
                if not any(
                    item.get("doi", "").casefold() == doi.casefold()
                    and item.get("pubmed_id", "").casefold() == pubmed.casefold()
                    for item in records
                ):
                    records.append(record)
    joined = json.dumps(values, ensure_ascii=False) if values else ""
    for doi in re.findall(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", joined, re.IGNORECASE):
        cleaned = doi.rstrip(".,;)]}")
        if not any(item.get("doi", "").casefold() == cleaned.casefold() for item in records):
            records.append({"title": "", "doi": cleaned, "pubmed_id": ""})
    return records


def _first_matching_value(row: dict[str, Any], names: Iterable[str]) -> str:
    normalized = {str(key).strip().casefold(): value for key, value in row.items()}
    return next(
        (_metadata_scalar(normalized.get(name)) for name in names if _metadata_scalar(normalized.get(name))),
        "",
    )


def _annotation_text(value: Any) -> str:
    if isinstance(value, dict):
        return _metadata_scalar(
            value.get("annotationValue") or value.get("value") or value.get("name")
        )
    return _metadata_scalar(value)


def _metadata_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return _annotation_text(value)
    if isinstance(value, list):
        return "; ".join(filter(None, (_metadata_scalar(item) for item in value)))
    return str(value).strip()


def _display_field_name(value: Any) -> str:
    text = re.sub(r"[_-]+", " ", str(value or "")).strip()
    return re.sub(r"\s+", " ", text)


def _metabolights_files(
    accession: str,
    raw_names: Iterable[str],
    file_index: dict[str, int],
    public_root: str,
) -> list[RepositoryFile]:
    files = []
    for raw_name in sorted(raw_names):
        normalized = raw_name.replace("\\", "/").lstrip("/")
        if not normalized.upper().startswith("FILES/"):
            normalized = "FILES/" + normalized
        url = f"{public_root}/{accession}/" + urllib.parse.quote(normalized, safe="/")
        relative = normalized[6:]
        size = file_index.get(relative, file_index.get(Path(relative).name, 0))
        lower_name = relative.casefold()
        if lower_name.endswith(".mzml"):
            role = "converted"
        elif requires_msdial_conversion(lower_name):
            role = "requires_conversion"
        else:
            role = "raw"
        files.append(RepositoryFile(raw_name, size, url, role=role))
    return files


def _metabolights_group_rank(group: dict[str, Any]) -> tuple[int, int, int, int]:
    separation = group.get("separation")
    acquisition = group.get("acquisition")
    supported = separation == "LC-MS" and acquisition in {"DDA", "DIA", "AIF", "SWATH"}
    known_size = group.get("total", 0) > 0
    has_files = bool(group.get("files"))
    # Prefer a directly actionable assay, then one with downloadable files and a known bounded size.
    return (int(supported), int(has_files), int(known_size), -int(group.get("total", 0)))


def _parse_apache_index(text: str) -> dict[str, int]:
    result = {}
    for row in re.findall(r"(?is)<tr>.*?</tr>", text):
        href_match = re.search(r'(?i)<a\s+href="([^"]+)"', row)
        sizes = re.findall(r'(?i)<td\s+align="right">\s*([0-9.]+\s*[KMGT]?)\s*</td>', row)
        if not href_match or not sizes:
            continue
        href = urllib.parse.unquote(html.unescape(href_match.group(1)))
        if href.endswith("/") or href.startswith("?") or "Parent Directory" in row:
            continue
        result[href] = _parse_size(sizes[-1])
        result.setdefault(Path(href).name, result[href])
    return result


def _string_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    return [str(value)] if str(value or "").strip() else []


def _metabobank_data_root(entry: dict[str, Any]) -> str:
    for item in entry.get("distribution", []) or []:
        if str(item.get("encodingFormat") or "").upper() == "DATA":
            return str(item.get("contentUrl") or "").rstrip("/") + "/"
    return ""


def _parse_metabobank_filelist(text: str) -> list[dict[str, Any]]:
    rows = []
    reader = csv.DictReader(text.splitlines(), delimiter="\t")
    for row in reader:
        name = str(row.get("Name") or "").replace("\\", "/").lstrip("/")
        if not name:
            continue
        rows.append(
            {
                "type": str(row.get("Type") or ""),
                "name": name,
                "time": str(row.get("Time") or ""),
                "size": _parse_int(row.get("Size")) or 0,
                "md5": str(row.get("MD5") or "").strip(),
            }
        )
    return rows


def _parse_metabobank_sdrf(text: str) -> list[dict[str, str]]:
    reader = csv.reader(text.splitlines(), delimiter="\t")
    try:
        headers = next(reader)
    except StopIteration:
        return []
    counts: dict[str, int] = {}
    unique_headers = []
    for header in headers:
        base = header.strip() or "Unnamed"
        counts[base] = counts.get(base, 0) + 1
        unique_headers.append(base if counts[base] == 1 else f"{base} [{counts[base]}]")
    return [
        {header: values[index].strip() if index < len(values) else "" for index, header in enumerate(unique_headers)}
        for values in reader
        if any(value.strip() for value in values)
    ]


def _metabobank_raw_references(rows: list[dict[str, str]]) -> list[str]:
    references = []
    for row in rows:
        for key, value in row.items():
            if key.startswith("Raw Data File") and value.strip():
                normalized = value.replace("\\", "/").lstrip("/")
                if normalized not in references:
                    references.append(normalized)
    original = [
        value for value in references
        if not value.casefold().endswith(".abf") and "/abf/" not in value.casefold()
    ]
    return original or references


def _metabobank_raw_files(
    file_rows: list[dict[str, Any]], references: list[str], data_root: str
) -> tuple[list[RepositoryFile], bool]:
    selected: dict[str, dict[str, Any]] = {}
    fallback_to_abf = bool(references) and all(
        value.casefold().endswith(".abf") or "/abf/" in value.casefold()
        for value in references
    )
    for reference in references:
        prefix = reference.rstrip("/")
        lower = prefix.casefold()
        for row in file_rows:
            name = str(row.get("name") or "")
            name_lower = name.casefold()
            matches_folder = name_lower.startswith(lower + "/")
            matches_file = name_lower == lower
            matches_sidecar = lower.endswith((".wiff", ".wiff2")) and name_lower.startswith(lower + ".")
            if matches_folder or matches_file or matches_sidecar:
                selected.setdefault(name_lower, row)
    files = []
    for row in selected.values():
        name = str(row["name"])
        files.append(
            RepositoryFile(
                name=name,
                size_bytes=int(row.get("size") or 0),
                url=urllib.parse.urljoin(data_root, urllib.parse.quote(name, safe="/")),
                role="sidecar" if _is_sidecar_name(name) else "raw",
                checksum=str(row.get("md5") or ""),
            )
        )
    return sorted(files, key=lambda item: item.name.casefold()), fallback_to_abf


def _metabobank_sample_metadata(rows: list[dict[str, str]]) -> list[dict[str, Any]]:
    result = []
    for index, row in enumerate(rows, start=1):
        raw_values = [
            value for key, value in row.items()
            if key.startswith("Raw Data File") and value
        ]
        original = next(
            (
                value for value in raw_values
                if not value.casefold().endswith(".abf") and "/abf/" not in value.casefold()
            ),
            raw_values[0] if raw_values else "",
        )
        sample_id = str(row.get("Sample Name") or row.get("Assay Name") or f"sample_{index}")
        values = {
            key: value for key, value in row.items()
            if value and key not in {"Source Name", "Sample Name"} and not key.startswith("Raw Data File")
        }
        result.append(
            {
                "sample_id": sample_id,
                "source_name": str(row.get("Source Name") or sample_id),
                "raw_file": original,
                "values": values,
            }
        )
    return result


def _metabobank_publications(value: Any) -> list[dict[str, str]]:
    result = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        identifier = str(item.get("id") or "")
        db_type = str(item.get("dbType") or "").casefold()
        result.append(
            {
                "title": str(item.get("title") or ""),
                "doi": identifier if db_type == "doi" else "",
                "pubmed_id": identifier if db_type in {"pubmed", "pmid"} else "",
                "url": str(item.get("url") or ""),
            }
        )
    return result


def _html_text(value: str) -> str:
    without_scripts = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", value)
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def _infer_separation(text: str) -> str:
    value = _normalized_metadata_text(text)
    if re.search(r"\b(gc[- /]?ms|gc[- ]?tof|gas chromat)", value):
        return "GC-MS"
    if re.search(r"\b(u?plc|hplc|lc)[- /]?(q?tof|orbitrap|ms)|liquid chromat", value):
        return "LC-MS"
    return "Unknown"


def _infer_acquisition(text: str) -> str:
    value = _normalized_metadata_text(text)
    if re.search(r"\b(mrm|multiple reaction monitoring)\b", value):
        return "MRM"
    if re.search(r"\b(srm|selected reaction monitoring)\b", value):
        return "SRM"
    if re.search(r"\b(sim|selected ion monitoring)\b", value):
        return "SIM"
    if re.search(r"\binstrument\s*mode\W{0,30}(?:full\s*)?scan\b|\bfull scan\b", value):
        return "FullScan"
    if re.search(r"\b(aif|all[- ]?ions?|all ion fragmentation|msall|mse)\b", value):
        return "AIF"
    if re.search(r"\b(dia|swath|data[- ]independent)\b", value):
        return "DIA"
    if re.search(r"\b(dda|auto\s*ms/?ms|data[- ]dependent)\b", value):
        return "DDA"
    return "Unknown"


def _infer_ion_mode(text: str) -> str:
    value = _normalized_metadata_text(text)
    positive = bool(
        re.search(
            r"\bpos\b|\besi\s*\+|\bpositive\s+(?:ion|mode|polarity)|\b(?:ion mode|polarity|scan polarity)\W{0,20}positive\b",
            value,
        )
    )
    negative = bool(
        re.search(
            r"\bneg\b|\besi\s*-|\bnegative\s+(?:ion|mode|polarity)|\b(?:ion mode|polarity|scan polarity)\W{0,20}negative\b",
            value,
        )
    )
    if positive and negative:
        return "Both"
    if positive:
        return "Positive"
    if negative:
        return "Negative"
    return "Unknown"


def _infer_untargeted(text: str) -> bool | None:
    value = _normalized_metadata_text(text)
    if re.search(r"\b(untargeted|non[- ]?targeted|nontargeted|global metabol|global lipid)", value):
        return True
    if re.search(r"\b(targeted|quantitative panel|mrm|srm|sim|selected ion monitoring)\b", value):
        return False
    return None


def _normalized_metadata_text(text: str) -> str:
    return re.sub(r"[_]+", " ", text.casefold())


def _comment_value(study: dict[str, Any], name: str) -> str:
    for item in study.get("comments", []):
        if str(item.get("name", "")).casefold() == name.casefold():
            return str(item.get("value") or "")
    return ""


def _parse_size(value: str) -> int:
    match = re.match(r"\s*([0-9.]+)\s*([kmgt]?)(?:i?b)?\s*$", value, re.IGNORECASE)
    if not match:
        return 0
    scale = {"": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}
    return int(float(match.group(1)) * scale[match.group(2).casefold()])


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _is_sidecar_name(name: str) -> bool:
    value = name.casefold()
    return value.endswith(".wiff.scan") or value.endswith(".wiff2.scan")


# DURABLE MANIFEST WRITES.
#
# The unit manifest is the only record of a unit that outlives every registry, and it used to be written
# with a plain write_text: open for writing, which truncates, then write. A process killed between the two
# - a reboot, a backend restart, a full disk - left an empty or half-written file, and at campaign scale,
# with weeks of crash-and-resume, the record a resumed run needs was exactly the one most likely to have
# been caught mid-write. Two writers were also possible: the campaign runner calls these functions in its
# own process while the backend's run job finalises the same manifest, and each would have read, changed
# and written the whole file over the other's change.
#
# Now every write goes to a temporary file in the same directory, is flushed to disk, and replaces the
# manifest in one rename, so a reader sees the old record or the new one and never a fragment. Writers
# take a lock beside the file first. The lock is an operating-system lock on a file that is never deleted,
# so a writer that dies releases it with its handle and there is no stale lock to recover or process to
# probe for liveness. Readers take no lock: the rename is what keeps them from a fragment, and a retry is
# what keeps them from the rename itself (read_manifest).
MANIFEST_LOCK_SUFFIX = ".lock"
MANIFEST_TEMPORARY_SUFFIX = ".tmp"
MANIFEST_LOCK_TIMEOUT_SECONDS = 120.0
# How often, and how patiently, a rename onto the manifest and a read of it are retried while the other
# holds the file. One schedule for both: about 25 s in all, far longer than either holds it.
_MANIFEST_BUSY_ATTEMPTS = 50


def _manifest_busy_delay(attempt: int) -> float:
    return 0.02 * (attempt + 1)


class ManifestBusyError(TimeoutError):
    """A manifest stayed locked, or unreadable, for longer than a writer or a reader waits for it.

    Nothing was changed. A TimeoutError, as the lock's timeout always was, so existing handlers still
    catch it; its own type is what lets the MCP layer report it as busy rather than as a crash.
    """


_MANIFEST_THREAD_LOCKS: dict[str, threading.RLock] = {}
_MANIFEST_THREAD_LOCKS_GUARD = threading.Lock()
_MANIFEST_LOCK_DEPTH: dict[str, int] = {}


def _manifest_lock_key(path: Path) -> str:
    return os.path.normcase(str(Path(path).resolve()))


def _lock_file_handle(lock_path: Path, deadline: float) -> Any:
    handle = open(lock_path, "a+b")
    while True:
        try:
            if msvcrt is not None:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return handle
        except OSError:
            if time.monotonic() >= deadline:
                handle.close()
                raise ManifestBusyError(
                    f"Another writer held {lock_path.name} for longer than "
                    f"{MANIFEST_LOCK_TIMEOUT_SECONDS:.0f} s; the manifest was not changed."
                )
            time.sleep(0.02)


def _unlock_file_handle(handle: Any) -> None:
    try:
        if msvcrt is not None:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


@contextmanager
def manifest_lock(path: str | Path, timeout: float = MANIFEST_LOCK_TIMEOUT_SECONDS) -> Iterator[Path]:
    """Hold the writer lock of one JSON record, across threads and processes.

    Re-entrant within a thread, so a read-modify-write that holds it may call _write_json, which takes it
    again. The lock file sits beside the record as <name>.lock and is never removed: removing it would let
    a second writer lock a new file while a first still held the old one.
    """
    target = Path(path).resolve()
    key = _manifest_lock_key(target)
    with _MANIFEST_THREAD_LOCKS_GUARD:
        thread_lock = _MANIFEST_THREAD_LOCKS.setdefault(key, threading.RLock())
    deadline = time.monotonic() + max(float(timeout), 0.0)
    if not thread_lock.acquire(timeout=max(float(timeout), 0.0)):
        raise ManifestBusyError(
            f"Another thread held the lock on {target.name}; the manifest was not changed."
        )
    try:
        depth = _MANIFEST_LOCK_DEPTH.get(key, 0)
        if depth:
            _MANIFEST_LOCK_DEPTH[key] = depth + 1
            try:
                yield target
            finally:
                _MANIFEST_LOCK_DEPTH[key] -= 1
            return
        handle = _lock_file_handle(target.with_name(target.name + MANIFEST_LOCK_SUFFIX), deadline)
        _MANIFEST_LOCK_DEPTH[key] = 1
        try:
            yield target
        finally:
            _MANIFEST_LOCK_DEPTH.pop(key, None)
            _unlock_file_handle(handle)
    finally:
        thread_lock.release()


def is_manifest_scratch_file(path: str | Path) -> bool:
    """A lock or an unfinished temporary file of a durable write, which is never an artifact."""
    name = Path(path).name
    return name.endswith(MANIFEST_LOCK_SUFFIX) or (
        name.startswith(".") and name.endswith(MANIFEST_TEMPORARY_SUFFIX)
    )


def _replace_atomically(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=MANIFEST_TEMPORARY_SUFFIX, dir=str(path.parent)
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # On Windows a rename onto a file that another process has open for reading is refused for as
        # long as that reader holds it. Readers take no lock and hold the file for milliseconds, so the
        # replacement is retried briefly rather than failing the write.
        for attempt in range(_MANIFEST_BUSY_ATTEMPTS):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == _MANIFEST_BUSY_ATTEMPTS - 1:
                    raise
                time.sleep(_manifest_busy_delay(attempt))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if hasattr(os, "O_DIRECTORY"):
        # POSIX only: make the rename itself durable. Windows cannot open a directory this way, and
        # NTFS journals the rename.
        try:
            directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(directory)
        except OSError:
            pass
        finally:
            os.close(directory)


def _write_json(path: Path, value: Any) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Serialised and encoded before anything on disk is touched, so a value that cannot be written
    # leaves the previous record exactly as it was.
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    with manifest_lock(path):
        _replace_atomically(path, data)


def _read_manifest_bytes(path: str | Path) -> bytes:
    """A manifest's bytes, waiting out the instant a writer renames a new record onto it.

    On Windows a file that os.replace is replacing cannot be opened for that instant: the open fails with
    PermissionError rather than returning a partial file. With the campaign runner and the backend both
    touching one manifest, that surfaced as sporadic unstructured tool failures, and as an execution gate
    refusing a unit whose record "could not be read". The open is retried on the rename's own schedule. A
    missing file is reported at once; only a busy one is waited for.
    """
    target = Path(path)
    for attempt in range(_MANIFEST_BUSY_ATTEMPTS):
        try:
            return target.read_bytes()
        except PermissionError as error:
            if attempt == _MANIFEST_BUSY_ATTEMPTS - 1:
                waited = sum(_manifest_busy_delay(index) for index in range(attempt))
                raise ManifestBusyError(
                    f"{target.name} stayed busy or unreadable for {waited:.0f} s: {error}"
                ) from error
            time.sleep(_manifest_busy_delay(attempt))
    raise AssertionError("unreachable")  # pragma: no cover


def read_manifest(path: str | Path) -> dict[str, Any]:
    """One JSON record written by _write_json - a unit manifest, a diagnostic record - read safely.

    Every reader of a unit manifest goes through here, so none of them mistakes a rename for an error.
    """
    manifest = json.loads(_read_manifest_bytes(path).decode("utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError(f"{Path(path).name} does not hold one JSON object.")
    return manifest


def update_manifest(path: str | Path, change: Any) -> dict[str, Any]:
    """Read, change and write one manifest under its writer lock, so a concurrent writer is not lost.

    ``change`` receives the manifest as it is on disk now and edits it in place; its return value is
    ignored. The written manifest is returned.
    """
    target = Path(path).resolve()
    if not target.is_file():
        # Before the lock, whose file would otherwise be the first thing created in a directory that
        # holds no manifest.
        raise FileNotFoundError(f"Manifest was not found: {target}")
    with manifest_lock(target):
        manifest = read_manifest(target)
        change(manifest)
        _write_json(target, manifest)
    return manifest
