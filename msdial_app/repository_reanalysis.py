from __future__ import annotations

import hashlib
import html
import json
import random
import re
import shutil
import subprocess
import tarfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


USER_AGENT = "MS-DIAL-Interactive/0.3 public-reanalysis"
RAW_SUFFIXES = {
    ".abf", ".cdf", ".d", ".lcd", ".mzml", ".mzxml", ".qgd", ".raw", ".wiff", ".wiff2",
}
ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz"}


@dataclass
class RepositoryFile:
    name: str
    size_bytes: int
    url: str
    role: str = "raw"
    checksum: str = ""


@dataclass
class RepositoryProject:
    repository: str
    accession: str
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
    total_download_bytes: int = 0
    evidence: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    selection_status: str = "unreviewed"
    review_reasons: list[str] = field(default_factory=list)
    eligible: bool = False
    exclusion_reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EligibilityPolicy:
    max_download_bytes: int = 5 * 1024**3
    max_samples: int = 40
    require_known_size: bool = True
    require_untargeted: bool = True


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

    def download(self, url: str, destination: Path, maximum_bytes: int) -> dict[str, Any]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + ".part")
        digest = hashlib.sha256()
        md5 = hashlib.md5()
        downloaded = 0
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=max(self.timeout, 300)) as response, partial.open("wb") as output:
                declared = int(response.headers.get("Content-Length") or 0)
                if declared and declared > maximum_bytes:
                    raise ValueError(f"Remote object is {declared} bytes; limit is {maximum_bytes} bytes.")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    downloaded += len(chunk)
                    if downloaded > maximum_bytes:
                        raise ValueError(f"Download exceeded the {maximum_bytes}-byte safety limit.")
                    output.write(chunk)
                    digest.update(chunk)
                    md5.update(chunk)
            partial.replace(destination)
        except Exception:
            partial.unlink(missing_ok=True)
            raise
        return {
            "path": str(destination),
            "size_bytes": downloaded,
            "sha256": digest.hexdigest(),
            "md5": md5.hexdigest(),
        }


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
            total_download_bytes=total,
            evidence=[
                f"analysis_type={summary_row.get('analysis_type', '')}",
                f"analysis records={len(analysis)}",
            ],
        )
        if not files:
            project.warnings.append("No public raw-data archive was found on the study download page.")
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
            total_download_bytes=archive_size,
            evidence=[
                f"primary raw files={len(primary_items)}",
                f"sidecar/companion files={len(raw_items) - len(primary_items)}",
                "MB-POST analytical-condition preset",
            ],
        )


ADAPTERS = {
    "metabolomics_workbench": MetabolomicsWorkbenchAdapter,
    "metabolights": MetaboLightsAdapter,
    "mb_post": MbPostAdapter,
}


def evaluate_eligibility(project: RepositoryProject, policy: EligibilityPolicy) -> RepositoryProject:
    reasons = []
    review_reasons = []
    if project.separation not in {"GC-MS", "LC-MS"}:
        reasons.append("Separation is not confidently GC-MS or LC-MS.")
    if project.separation == "LC-MS":
        if project.acquisition_mode == "Unknown":
            review_reasons.append("Inspect raw scan metadata to distinguish DDA/DIA/AIF from unsupported acquisition modes.")
        elif project.acquisition_mode not in {"DDA", "DIA", "AIF"}:
            reasons.append("LC-MS acquisition is not scan-based DDA/DIA/AIF.")
        if project.ion_mode == "Unknown":
            review_reasons.append("Confirm LC-MS ion mode from raw scan metadata.")
        elif project.ion_mode == "Both":
            review_reasons.append(
                "Distinguish polarity-switching data from separate positive/negative files before analysis."
            )
    if project.separation == "GC-MS" and project.acquisition_mode in {"MRM", "SRM", "SIM"}:
        reasons.append("Targeted GC-MS SIM/MRM/SRM is outside this pilot.")
    if policy.require_untargeted:
        if project.untargeted is False:
            reasons.append("Repository metadata identifies the study as targeted.")
        elif project.untargeted is None:
            review_reasons.append("Confirm untargeted status from repository context or raw scan metadata.")
    if not project.files:
        reasons.append("No downloadable raw data were identified.")
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


def create_download_lease(
    project: RepositoryProject,
    workspace_root: Path,
    maximum_bytes: int,
    client: RepositoryHttpClient | None = None,
    allow_preflight: bool = False,
) -> dict[str, Any]:
    downloadable = project.eligible or (
        allow_preflight and project.selection_status == "raw_metadata_required"
    )
    if not downloadable:
        raise ValueError("Only an eligible or explicitly approved preflight project can receive a download lease.")
    if project.total_download_bytes > maximum_bytes:
        raise ValueError("Project exceeds the download lease size limit.")
    client = client or RepositoryHttpClient()
    root = workspace_root.resolve() / project.repository / project.accession
    raw_root = root / "raw"
    download_root = raw_root / "downloads"
    data_root = raw_root / "data"
    provenance = root / "provenance"
    output = root / "output"
    for directory in (download_root, data_root, provenance, output):
        directory.mkdir(parents=True, exist_ok=True)
    downloads = []
    unique_urls: dict[str, RepositoryFile] = {}
    for item in project.files:
        unique_urls.setdefault(item.url, item)
    downloaded_bytes = 0
    for index, (url, item) in enumerate(unique_urls.items(), start=1):
        filename = Path(urllib.parse.urlparse(url).path).name or f"{project.accession}_{index}.zip"
        if project.repository == "mb_post":
            filename = f"{project.accession}.tar"
        archive = Path(filename).suffix.casefold() in ARCHIVE_SUFFIXES
        destination = download_root / filename if archive else data_root / _safe_relative_name(item.name)
        result = client.download(url, destination, maximum_bytes - downloaded_bytes)
        downloaded_bytes += result["size_bytes"]
        result["source_url"] = url
        result["declared_checksum"] = item.checksum
        if item.checksum and project.repository != "mb_post" and re.fullmatch(r"[0-9a-fA-F]{32}", item.checksum):
            if result["md5"].casefold() != item.checksum.casefold():
                raise ValueError(f"MD5 checksum mismatch for {filename}.")
        downloads.append(result)
    extracted = []
    for item in downloads:
        archive_path = Path(item["path"])
        if archive_path.parent == download_root and _is_archive(archive_path):
            extracted.extend(_extract_archive(archive_path, data_root, maximum_bytes * 5))
    inputs = _find_msdial_inputs(data_root)
    analysis_input = _common_input_path(inputs, data_root)
    manifest = {
        "schema": "msdial-public-reanalysis-run.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": "prepared",
        "project": project.as_dict(),
        "workspace": str(root),
        "raw_directory": str(raw_root),
        "input_directory": str(data_root),
        "output_directory": str(output),
        "downloads": downloads,
        "extracted_files": extracted,
        "input_candidates": inputs,
        "analysis_input_path": analysis_input,
        "execution_allowed": project.eligible,
        "cleanup_allowed": False,
    }
    manifest_path = provenance / "run-manifest.json"
    _write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def finalize_download_lease(manifest_path: Path) -> dict[str, Any]:
    from .mztab_validation import validate_mztab_outputs

    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    output = Path(manifest["output_directory"]).resolve()
    validation = validate_mztab_outputs(output)
    summary = validation.get("summary", {})
    mztab_files = [Path(item["file"]).resolve() for item in validation.get("files", [])]
    if not mztab_files or summary.get("failed", 0):
        manifest["status"] = "validation_failed"
        manifest["cleanup_allowed"] = False
    else:
        manifest["status"] = "mztab_validated"
        manifest["cleanup_allowed"] = True
    retained = list(mztab_files)
    for path in output.rglob("*") if output.is_dir() else []:
        if path.is_file() and any(
            token in path.name.casefold()
            for token in ("quality", "qa", "publication", "method", "parameter", "analysis_files")
        ):
            retained.append(path.resolve())
    manifest["mztab_validation"] = validation
    manifest["retained_artifacts"] = list(dict.fromkeys(str(path) for path in retained))
    manifest["finalized_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def run_raw_metadata_preflight(
    manifest_path: Path,
    extractor_path: Path,
    max_inputs: int = 3,
    confirm_untargeted: bool = False,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    extractor_path = extractor_path.resolve()
    if not extractor_path.is_file():
        raise FileNotFoundError(f"Raw metadata extractor was not found: {extractor_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    previously_allowed = bool(
        manifest.get("execution_allowed") or manifest.get("project", {}).get("eligible")
    )
    candidates = [Path(value) for value in manifest.get("input_candidates", [])]
    inputs = [path for path in candidates if path.exists()][0:max(1, max_inputs)]
    if not inputs:
        raise ValueError("No extracted MS-DIAL input candidate is available for metadata preflight.")
    output = manifest_path.parent / "raw-metadata-preflight.json"
    command = [str(extractor_path)]
    for path in inputs:
        command.extend(["--input", str(path)])
    command.extend(["--output", str(output), "--max-spectrum-headers", "200"])
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    manifest["raw_metadata_preflight"] = {
        "command": command,
        "exit_code": completed.returncode,
        "stdout": completed.stdout,
        "stderr": completed.stderr,
        "output": str(output),
    }
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
    if summary["acquisition_mode"] != "Unknown":
        project.acquisition_mode = summary["acquisition_mode"]
    if summary["ion_mode"] != "Unknown":
        project.ion_mode = summary["ion_mode"]
    if confirm_untargeted:
        project.untargeted = True
        project.evidence.append("Untargeted status confirmed during raw metadata preflight.")
    project.evidence.extend(summary["evidence"])
    evaluated = evaluate_eligibility(
        project,
        EligibilityPolicy(
            max_download_bytes=max(project.total_download_bytes, 1),
            max_samples=max(project.sample_count or 0, 1),
            require_known_size=False,
            require_untargeted=True,
        ),
    )
    manifest["project"] = evaluated.as_dict()
    manifest["raw_metadata_preflight"]["summary"] = summary
    manifest["execution_allowed"] = evaluated.eligible
    manifest["status"] = "preflight_passed" if evaluated.eligible else "preflight_review_required"
    _write_json(manifest_path, manifest)
    return {**manifest, "manifest_path": str(manifest_path)}


def cleanup_download_lease(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not confirmed:
        return {"deleted": False, "confirmation_required": True, "manifest_path": str(manifest_path)}
    if manifest.get("status") not in {"mztab_validated", "completed"} or not manifest.get("cleanup_allowed"):
        raise ValueError("Raw cleanup requires a completed/validated manifest with cleanup_allowed=true.")
    retained = [Path(value) for value in manifest.get("retained_artifacts", [])]
    if not retained or any(not path.exists() for path in retained):
        raise ValueError("Retained mzTab-M/provenance artifacts are missing; raw cleanup was refused.")
    raw_root = Path(manifest["raw_directory"]).resolve()
    workspace = Path(manifest["workspace"]).resolve()
    if raw_root.parent != workspace or raw_root.name != "raw":
        raise ValueError("Raw directory is outside the expected project workspace.")
    shutil.rmtree(raw_root)
    manifest["status"] = "raw_cleaned"
    manifest["raw_cleaned_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(manifest_path, manifest)
    return {"deleted": True, "raw_directory": str(raw_root), "manifest_path": str(manifest_path)}


def discard_download_lease(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    from .mztab_validation import find_mztab_files

    manifest_path = manifest_path.resolve()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not confirmed:
        return {"deleted": False, "confirmation_required": True, "manifest_path": str(manifest_path)}
    if manifest.get("status") in {"mztab_validated", "completed", "raw_cleaned"}:
        raise ValueError("Validated/completed runs must use the normal cleanup command.")
    output = Path(manifest.get("output_directory", ""))
    if find_mztab_files(output):
        raise ValueError("mzTab-M output exists; finalize the run before deleting raw data.")
    raw_root = Path(manifest["raw_directory"]).resolve()
    workspace = Path(manifest["workspace"]).resolve()
    if raw_root.parent != workspace or raw_root.name != "raw":
        raise ValueError("Raw directory is outside the expected project workspace.")
    if raw_root.exists():
        shutil.rmtree(raw_root)
    manifest["status"] = "discarded"
    manifest["discarded_at"] = datetime.now(timezone.utc).isoformat()
    manifest["discard_reason"] = "Preflight/download was rejected before a retained mzTab-M result was produced."
    _write_json(manifest_path, manifest)
    return {"deleted": True, "raw_directory": str(raw_root), "manifest_path": str(manifest_path)}


def project_from_dict(value: dict[str, Any]) -> RepositoryProject:
    data = dict(value)
    data["files"] = [RepositoryFile(**item) for item in data.get("files", [])]
    return RepositoryProject(**data)


def _safe_relative_name(value: str) -> Path:
    normalized = value.replace("\\", "/").lstrip("/")
    if normalized.casefold().startswith("files/"):
        normalized = normalized[6:]
    parts = [part for part in Path(normalized).parts if part not in {"", "."}]
    if not parts or ".." in parts:
        raise ValueError(f"Unsafe repository file path: {value}")
    return Path(*parts)


def _is_archive(path: Path) -> bool:
    lower = path.name.casefold()
    return lower.endswith((".zip", ".tar", ".tar.gz", ".tgz"))


def _extract_archive(archive: Path, destination: Path, maximum_bytes: int) -> list[str]:
    destination = destination.resolve()
    extracted = []
    total = 0
    if tarfile.is_tarfile(archive):
        with tarfile.open(archive) as handle:
            for member in handle.getmembers():
                if member.issym() or member.islnk():
                    raise ValueError(f"Archive links are not accepted: {member.name}")
                target = (destination / member.name).resolve()
                if target != destination and destination not in target.parents:
                    raise ValueError(f"Archive member escapes the workspace: {member.name}")
                total += max(0, member.size)
                if total > maximum_bytes:
                    raise ValueError("Expanded archive exceeds the extraction safety limit.")
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    source = handle.extractfile(member)
                    if source is not None:
                        with source, target.open("wb") as output:
                            shutil.copyfileobj(source, output)
                        extracted.append(str(target))
            return extracted
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as handle:
            for member in handle.infolist():
                target = (destination / member.filename).resolve()
                if target != destination and destination not in target.parents:
                    raise ValueError(f"Archive member escapes the workspace: {member.filename}")
                total += member.file_size
                if total > maximum_bytes:
                    raise ValueError("Expanded archive exceeds the extraction safety limit.")
                if member.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with handle.open(member) as source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)
                extracted.append(str(target))
        return extracted
    return []


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
        if path.suffix.casefold() in RAW_SUFFIXES or lower.endswith(".mzdata.xml"):
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
    return {
        "files_inspected": len(records),
        "separation": next(iter(separation_values), "Unknown") if len(separation_values) <= 1 else "Unknown",
        "acquisition_mode": next(iter(acquisition_values), "Unknown") if len(acquisition_values) <= 1 else "Unknown",
        "ion_mode": ion_mode if ion_mode in {"Positive", "Negative", "Both"} else "Unknown",
        "observed_separations": sorted(separations),
        "observed_acquisition_methods": sorted(methods),
        "observed_polarities": sorted(polarities),
        "evidence": [f"Raw metadata preflight inspected {len(records)} representative file(s)."],
    }


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
        role = "converted" if lower_name.endswith((".mzml", ".mzxml", ".mzdata.xml")) else "raw"
        files.append(RepositoryFile(raw_name, size, url, role=role))
    return files


def _metabolights_group_rank(group: dict[str, Any]) -> tuple[int, int, int, int]:
    separation = group.get("separation")
    acquisition = group.get("acquisition")
    supported = separation == "GC-MS" or (separation == "LC-MS" and acquisition in {"DDA", "DIA", "AIF"})
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


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
