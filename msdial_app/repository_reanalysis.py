from __future__ import annotations

import hashlib
import html
import http.client
import copy
import csv
import errno
import json
import os
import random
import re
import secrets
import socket
import ssl
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
from . import archives, encoding_preference
from .archives import ArchiveError, ExtractionLimits
from .diagnostic_paths import (
    INTERMEDIATES_DIRECTORY,
    extended_path,
    intermediate_files,
    is_diagnostic_artifact,
    is_intermediate_artifact,
    path_is_file,
    plain_path,
)
from .download_store import (
    LIVE_CLAIM_STATES,
    STORE_DIRECTORY,
    DeclaredChecksumMismatch,
    DownloadStore,
    MaterializationCollision,
    StoreError,
    StoreLockTimeout,
    unlink_tree,
)
from .mzml_encoding import UNSUPPORTED_MZML_ENCODING, scan_mzml_encoding
from .mzxml_conversion import (
    CONVERTER_NAME,
    POLARITY_IMPUTATION,
    ConversionOptions,
    convert_mzxml_to_mzml,
    converter_identity,
)
from .process_liveness import process_created_at, process_is_alive
from .reader_created import container_members, reader_created_files, reader_created_names

try:
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None
    import fcntl


USER_AGENT = "MS-DIAL-Interactive/0.3 public-reanalysis"
RAW_SUFFIXES = {
    ".abf", ".cdf", ".d", ".lcd", ".mzml", ".qgd", ".raw", ".wiff", ".wiff2",
}
# MS-DIAL HAS NO READER FOR EITHER, AND ONLY ONE OF THEM IS CONVERTED, AND ONLY IN A CAMPAIGN. Outside a
# campaign a unit that needs either is requires_conversion and is excluded before download, as it always
# was: it needs a reviewed ProteoWizard-to-mzML conversion with new provenance. The user decided on
# 2026-09-30 that a campaign's mzXML-only data are converted to mzML and run: the lease's convert stage,
# given a campaign authorization, writes each of the unit's mzXML as mzML under raw\converted
# (msdial_app.mzxml_conversion, every inference off but the polarity a unit's declaration gives, below).
# Nothing converts mzData, so a unit that needs it is excluded in a campaign too. A packed mzXML
# (x.mzXML.lzma, x.mzXML.gz) is the mzXML it unpacks to, and is converted once the extract stage has
# unpacked it.
CONVERTIBLE_SUFFIXES = (".mzxml",)
UNSUPPORTED_ENCODING_SUFFIXES = (".mzdata", ".mzdata.xml")
CONVERSION_REQUIRED_SUFFIXES = CONVERTIBLE_SUFFIXES + UNSUPPORTED_ENCODING_SUFFIXES
# Where the convert stage writes, beside raw\data and raw\downloads: released with the raw tree, never
# mixed with a repository's own mzML, and outside the trees the discovery walk and the checksum index read.
CONVERTED_DIRECTORY = "converted"
# The reason a file whose conversion failed is kept out of the input candidates, and the provenance record
# every conversion of a lease is written to (also the manifest's input_conversions).
CONVERSION_FAILED = "conversion_failed"
# The reason an mzXML is kept out of them where its unit declares one polarity and some of its scans record the
# other beside scans that record none: the declaration cannot be imputed to those, and the user decided on
# 2026-10-03 that such a file is excluded and the rest of the unit runs (A FILE WHOSE SCANS CONTRADICT THE
# DECLARATION, at the convert stage).
POLARITY_CONTRADICTS_DECLARATION = "polarity_contradicts_declaration"
# The system errors that fail a conversion through no fault of its file - the disk or the quota is full, or
# another process (a virus scanner, an indexer) still held the file after the converter waited for it - and
# stop the lease instead, so that the unit is retried rather than run without that sample.
FULL_DISK_ERRNOS = frozenset(code for code in (errno.ENOSPC, getattr(errno, "EDQUOT", None)) if code is not None)
HELD_FILE_ERRNOS = frozenset({errno.EACCES, errno.EPERM})
LEASE_STOPPING_ERRNOS = FULL_DISK_ERRNOS | HELD_FILE_ERRNOS
INPUT_CONVERSIONS_SCHEMA = "msdial-input-conversions.v1"
INPUT_CONVERSIONS_NAME = "input-conversions.json"
CONVERSION_PLAN_SCHEMA = "msdial-mzxml-conversion-plan.v1"
# THE ONE INFERENCE A CAMPAIGN'S CONVERSION MAKES. MS-DIAL skips a spectrum whose polarity is not the method's
# ion mode, and RawDataHandler reads polarity only as a spectrum cvParam, so an mzXML whose scans record none
# runs to nothing. The user decided on 2026-10-02 that the convert stage then imputes the unit's declared ion
# mode, and only where its Catalog handoff's technical settings declare exactly one polarity, Positive or
# Negative (DECLARED_POLARITIES). That field is read because nothing rewrites it, and the gate's CONV-1 holds
# an imputation to it (_declared_ion_mode); project.ion_mode is never read, since the raw-header preflight
# rewrites it from headers that carry what was imputed, and a split part's is its part's polarity. Both,
# Unknown or no declaration imputes nothing, and CONV-1 fails the spectra left without a polarity. A file some
# of whose scans record the other polarity and some none is excluded (POLARITY_CONTRADICTS_DECLARATION).
DECLARED_ION_MODE_FIELD = "project.repository_metadata.catalog_handoff.technical_settings.ion_mode"
DECLARED_POLARITIES = {"positive": "positive", "negative": "negative"}
# The exclusion reasons evaluate_eligibility gives for these, which a split part inherits from its parent:
# outside a campaign, for an mzXML or mzData; in one, for what nothing converts, and for a unit whose lease
# converted and was left with no input.
CONVERSION_REQUIRED_REASON = "MS-DIAL has no mzXML/mzData reader"
UNCONVERTIBLE_INPUT_REASON = "MS-DIAL cannot read these analysis inputs, and nothing converts them to mzML"
NO_CONVERTED_INPUT_REASON = "No analysis input survived its conversion from mzXML to mzML"
# What an archive is, and how it is opened, is decided in one place: archives.py. The lease routes an
# object by archives.archive_kind_from_name before its bytes exist, and the bytes confirm the kind
# (archives.detect_archive) before anything is extracted. The suffix set that used to live here named
# .gz but no .7z, .rar, .bz2, .xz or .lzma, and _is_archive disagreed with it about bare .gz.
#
# The file roles under which a repository lists an archive of a whole study (Metabolomics Workbench).
# A per-sample archive (X.raw.zip) is listed under its sample's own role, usually raw.
ARCHIVE_ROLES = frozenset({"raw_archive", "shared_raw_archive"})
# The vendor folders an input can be, as _find_msdial_inputs finds them (casefolded suffixes).
FOLDER_INPUT_SUFFIXES = frozenset({".d", ".raw"})
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
    # The analysis input this file belongs to, as the Catalog lists it: the vendor folder a member lies in
    # (raw/x.raw for raw/x.raw/_FUNC001.DAT), or the container a per-sample archive unpacks to. "" for a
    # file that is an input of its own.
    container: str = ""


def requires_msdial_conversion(name: str) -> bool:
    """Return whether a repository file must be converted to mzML before MS-DIAL can read it."""
    normalized = str(name or "").replace("\\", "/").casefold()
    return any(normalized.endswith(suffix) for suffix in CONVERSION_REQUIRED_SUFFIXES)


def is_convertible_input(name: str) -> bool:
    """Whether a repository file is an mzXML, packed or not, which the lease converts to mzML."""
    return encoding_preference.is_convertible(str(name or "").replace("\\", "/"))


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
    # ONE ANALYSIS INPUT PER VENDOR CONTAINER (Catalog 0.6.0, analysis_input_model one-input-per-sample.v1).
    # What MS-DIAL opens, one entry per sample row: a file, a vendor folder whose members `files` lists one
    # by one, or the container a per-sample archive unpacks to. Empty when the Catalog declared none - a unit
    # whose data sit inside a study archive finds its inputs after the download, by its sample names, as
    # every unit did before.
    analysis_inputs: list[dict[str, Any]] = field(default_factory=list)
    analysis_inputs_declared: bool = False
    analysis_input_issues: list[dict[str, Any]] = field(default_factory=list)
    split_hint: dict[str, Any] | None = None
    # What evaluate_eligibility found to convert from mzXML (CONVERSION_PLAN_SCHEMA), and, once a lease
    # ran its convert stage, what came of it ("outcome"). Empty for a unit with nothing to convert, and then
    # not written at all, so such a unit's project reads as it always did.
    conversion_plan: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.publications and self.publication_status == "none_recorded":
            self.publication_status = "recorded"

    def as_dict(self) -> dict[str, Any]:
        value = asdict(self)
        if not value["conversion_plan"]:
            del value["conversion_plan"]
        return value


@dataclass
class EligibilityPolicy:
    max_download_bytes: int = 5 * 1024**3
    max_samples: int = 40
    require_known_size: bool = True
    require_untargeted: bool = True
    allowed_separations: tuple[str, ...] = ("LC-MS",)
    allowed_acquisition_modes: tuple[str, ...] = ("DDA", "DIA", "AIF", "SWATH")
    # Set only for a campaign unit: one a campaign approval covers, or one whose manifest records one
    # (_converts_mzxml). Its mzXML then plans a conversion that the lease makes. Off, an mzXML or mzData
    # excludes the unit before download, as it always did: outside a campaign it needs a reviewed
    # ProteoWizard-to-mzML conversion with new provenance.
    convert_mzxml: bool = False


# HOW LONG A DOWNLOAD MAY GO WITHOUT A BYTE, AND HOW OFTEN IT IS TRIED AGAIN.
#
# A repository object was fetched with a 300 s socket timeout and one attempt. A read that stalled held
# the lease for five minutes and then failed it as a network error, with the .part kept but nothing to
# resume it until the whole unit was retried; and a cancel asked for during the stall was heard only
# when a byte next arrived, which it never did, so the lease recorded a timeout rather than the cancel.
#
# Now every read carries an idle timeout (no byte for this long raises), a stalled read or a lost
# connection is retried from the .part after a backoff, and each attempt is recorded. The caller's
# progress callback is also called at a stall and through each backoff, so a caller that stops a download
# by raising from it (a cancelled job) is heard within the idle timeout.
#
# A stall is not the only way to go quiet. Each read asked for a whole MiB, and urllib's read(amt) waits
# until it has one, so a transfer trickling in at 1 KB/s reached the callback once in seventeen minutes
# while every socket read came well inside the idle timeout; and a resume hashed its whole .part, tens of
# seconds for a large one, before the callback first heard from it. Both now report at least this often.
DOWNLOAD_IDLE_TIMEOUT_SECONDS = 120.0
DOWNLOAD_RETRIES = 3
DOWNLOAD_RETRY_BACKOFF_SECONDS = (10.0, 30.0, 90.0)
DOWNLOAD_PROGRESS_INTERVAL_SECONDS = 1.0
_DOWNLOAD_CHUNK_BYTES = 1024 * 1024


class RetryableDownloadError(Exception):
    """A transfer that stalled or lost its connection. Its .part is kept, and a later attempt resumes it
    under the same If-Range rules. ``reason`` names what happened; ``download_attempts``, set on the error
    a download finally raises, lists every attempt it made."""

    reason = "interrupted"


class DownloadStalled(RetryableDownloadError, TimeoutError):
    reason = "stalled"


class DownloadConnectionLost(RetryableDownloadError, ConnectionError):
    reason = "connection_lost"


class DownloadIncomplete(RetryableDownloadError, ValueError):
    """A response that ended before its declared length: a server that hung up early."""

    reason = "incomplete"


# What a lost connection looks like from urllib: a reset or an abort mid-transfer, a server gone before
# its status line (http.client.RemoteDisconnected is a ConnectionResetError), a chunked body cut short,
# and a TLS stream closed without its close_notify.
_CONNECTION_LOST = (
    ConnectionResetError, ConnectionAbortedError, BrokenPipeError, http.client.IncompleteRead, ssl.SSLEOFError,
)


def download_interruption(error: BaseException, idle_timeout: float) -> RetryableDownloadError | None:
    """The retryable error an exception from a transfer amounts to, or None when retrying would not help.

    An HTTP status, a refused or unresolvable connection, a limit, a changed file: none of those is
    answered by trying again, and each is raised as it was.
    """
    if isinstance(error, RetryableDownloadError):
        return error
    cause: BaseException = error
    if isinstance(error, urllib.error.URLError) and not isinstance(error, urllib.error.HTTPError):
        if isinstance(error.reason, BaseException):
            cause = error.reason
    if isinstance(cause, TimeoutError):
        return DownloadStalled(
            f"No bytes arrived from the server for {idle_timeout:g} s. The partial file is kept and a later "
            "attempt resumes it."
        )
    if isinstance(cause, _CONNECTION_LOST):
        return DownloadConnectionLost(
            f"The connection was lost during the transfer ({type(cause).__name__}: {cause}). The partial "
            "file is kept and a later attempt resumes it."
        )
    return None


class RepositoryHttpClient:
    def __init__(
        self,
        timeout: int = 60,
        *,
        idle_timeout: float = DOWNLOAD_IDLE_TIMEOUT_SECONDS,
        retries: int = DOWNLOAD_RETRIES,
        retry_backoff_seconds: tuple[float, ...] = DOWNLOAD_RETRY_BACKOFF_SECONDS,
    ) -> None:
        """``timeout`` is the socket timeout of a metadata request. A download's is ``idle_timeout``: every
        socket read carries it, so it is how long a transfer may go without a byte. ``retries`` is how many
        times a stalled or lost transfer is tried again, after ``retry_backoff_seconds`` (the last value
        repeats); 0 makes one attempt."""
        self.timeout = timeout
        self.idle_timeout = float(idle_timeout)
        self.retries = max(0, int(retries))
        self.retry_backoff_seconds = tuple(float(value) for value in retry_backoff_seconds) or (0.0,)

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
        """Fetch one repository object, retrying a stalled or lost transfer from its .part.

        Each attempt is one _download_once, with every socket read under the idle timeout. An attempt
        that stalls, loses its connection or ends short (download_interruption) is tried again after a
        backoff, up to ``retries`` times, and resumes from the .part it left. Anything else is raised at
        once. The result carries ``attempts``: one {attempt, started_at, ended_at, outcome,
        part_bytes_before, part_bytes_after} per attempt, outcome completed, stalled, connection_lost,
        incomplete, stopped (the progress callback raised) or failed, with the error of one that did not
        complete. The error a download finally raises carries the same list as ``download_attempts``, and
        a retryable one keeps its partial file and its validators for a later resume.

        ``progress_callback(received, declared)`` is called as bytes arrive: after each MiB, and after the
        first read to return a second or more since the last call, however few bytes it brought. It is
        also called about once a second while a resume hashes the .part it already holds, when a read
        stalls, and about once a second through each backoff, with the last values it was given. A caller
        that stops a download by raising from it (a cancelled job) is therefore heard within the idle
        timeout and a second, however slowly the bytes come, and the exception it raised is what the
        download raises: a stop, not a network error.
        """
        partial = destination.with_name(destination.name + ".part")
        attempts: list[dict[str, Any]] = []
        last = [0, 0]
        stopped: list[BaseException] = []

        def report(received: int, declared: int) -> None:
            last[:] = [received, declared]
            if progress_callback is None:
                return
            try:
                progress_callback(received, declared)
            except BaseException as error:
                stopped.append(error)
                raise

        def part_bytes() -> int:
            try:
                return partial.stat().st_size if partial.is_file() else 0
            except OSError:
                return 0

        def carry(error: BaseException) -> BaseException:
            try:
                error.download_attempts = attempts  # type: ignore[attr-defined]
            except (AttributeError, TypeError):
                pass
            return error

        for number in range(1, self.retries + 2):
            entry: dict[str, Any] = {
                "attempt": number,
                "started_at": datetime.now(timezone.utc).isoformat(),
                "part_bytes_before": part_bytes(),
            }
            attempts.append(entry)
            try:
                result = self._download_once(url, destination, maximum_bytes, report)
            except BaseException as error:
                entry.update(ended_at=datetime.now(timezone.utc).isoformat(), part_bytes_after=part_bytes())
                problem = None if stopped else download_interruption(error, self.idle_timeout)
                if problem is None:
                    entry.update(
                        outcome="stopped" if stopped else "failed",
                        error=str(error) or type(error).__name__,
                        error_type=type(error).__name__,
                    )
                    raise carry(error)
                entry.update(outcome=problem.reason, error=str(problem), error_type=type(error).__name__)
                final = number > self.retries
                if not final:
                    entry["retry_after_seconds"] = self.retry_backoff_seconds[
                        min(number - 1, len(self.retry_backoff_seconds) - 1)
                    ]
                try:
                    # The caller is heard at the stall, not only when a next byte arrives, and through
                    # the backoff.
                    report(*last)
                    deadline = time.monotonic() + (0.0 if final else entry["retry_after_seconds"])
                    while (remaining := deadline - time.monotonic()) > 0:
                        time.sleep(min(1.0, remaining))
                        report(*last)
                except BaseException as stop:
                    entry.update(outcome="stopped", stopped_after=problem.reason,
                                 error=str(stop) or type(stop).__name__, error_type=type(stop).__name__)
                    raise carry(stop) from error
                if final:
                    if problem is error:
                        raise carry(error)
                    raise carry(problem) from error
                continue
            entry.update(ended_at=datetime.now(timezone.utc).isoformat(), outcome="completed",
                         part_bytes_after=0)
            result["attempts"] = attempts
            return result
        raise AssertionError("unreachable")  # pragma: no cover

    def _download_once(
        self,
        url: str,
        destination: Path,
        maximum_bytes: int,
        progress_callback: Any = None,
    ) -> dict[str, Any]:
        """One attempt at one repository object, resuming a partial transfer where the server allows it.

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
        `Range: bytes=<size>-`. Its bytes are hashed before the request is sent, and a 206 means the
        server honoured it: the response is appended to the file and to those hashes. Anything else -
        a 200 because the server ignores ranges, a 416 because the .part is already as long as the
        resource, a changed ETag/Last-Modified - discards the hashes and restarts from zero, because
        a resumed file that mixes two versions of an object is worse than a slow one. The checksums
        are computed over the whole file either way, so a wrong guess about resumability shows up as
        a checksum that does not match rather than as silent corruption.

        The hashing used to come after the connection opened. The server then waited on a client
        that read nothing, for over a minute on a .part of tens of GB, and a server that drops such a
        client (nginx's send_timeout is 60 s by default) dropped every resume of exactly the large
        objects the retries are for: each retry hashed the .part again and was dropped again.

        HOW A CHANGED OBJECT IS TOLD. The validators of the response that started a .part (its
        strong ETag, else its Last-Modified, and its Content-Length) are kept beside it in
        <name>.part.json, and a resume sends them back as If-Range. A server whose object has
        changed since then answers with the whole new object (200), not the tail of it, so the
        restart above is decided by the server rather than discovered later as a mismatch; for a
        repository that publishes no checksum (MetaboLights) nothing later would discover it. This
        used to be said here and not done: no validator was ever sent or stored. A .part with no
        validators beside it, left by an earlier version, resumes as it always did. The validators
        are returned (etag, last_modified), for the download record and the download store. A .part
        and its validators always describe the same object: a refused restart removes the old head
        before the new validators exist, and a 206 whose own validator differs from the stored one
        (a server that ignores If-Range) is not appended.

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
        # Before the request, not after: no server waits while a large .part is read back.
        seeded = (
            _hash_part(partial, resume_from, int(validators.get("content_length") or 0), progress_callback)
            if resume_from
            else None
        )

        headers = {"User-Agent": USER_AGENT}
        if resume_from:
            headers["Range"] = f"bytes={resume_from}-"
            if_range = _if_range_value(validators)
            if if_range:
                headers["If-Range"] = if_range
        request = urllib.request.Request(url, headers=headers)

        try:
            # The socket timeout bounds the connect and every read that follows, so no read waits
            # longer than the idle timeout for a byte.
            response = urllib.request.urlopen(request, timeout=self.idle_timeout)
        except urllib.error.HTTPError as error:
            # 416 means the range is past the end of the resource: the .part is stale, or the
            # object shrank. Either way the only safe answer is to fetch it whole.
            if error.code == 416 and resume_from:
                partial.unlink(missing_ok=True)
                validators_path.unlink(missing_ok=True)
                return self._download_once(url, destination, maximum_bytes, progress_callback)
            raise

        if response.status == 206 and resume_from and _validators_changed(validators, response):
            # A server that honours Range but ignores If-Range sends the new object's tail anyway.
            # Its 206 still names the object the tail came from, and that is not the one the .part
            # began on, so start again rather than join the two.
            response.close()
            partial.unlink(missing_ok=True)
            validators_path.unlink(missing_ok=True)
            return self._download_once(url, destination, maximum_bytes, progress_callback)

        with response:
            appending = response.status == 206 and resume_from > 0
            if not appending:
                # The server ignored the range, or there was nothing to resume, or the object changed
                # since the .part began. Start clean rather than append a whole object onto a partial
                # one, and keep this response's validators for the next resume.
                #
                # The old head goes first, before anything below can refuse. Writing the new
                # validators beside it and then refusing (an object now over the limit, a malformed
                # Content-Length, a .part a scanner holds open) left the old head under the new
                # object's validators, so the next If-Range matched and the server sent the new tail
                # onto the old head: the mixing If-Range is there to prevent. The new validators are
                # written only once the .part has been opened afresh for this object.
                partial.unlink(missing_ok=True)
                validators_path.unlink(missing_ok=True)
                resume_from = 0
                validators = _response_validators(response, url)
            declared = int(response.headers.get("Content-Length") or 0)
            total_declared = declared + resume_from if declared else 0
            if total_declared and total_declared > maximum_bytes:
                raise ValueError(
                    f"Remote object is {total_declared} bytes; limit is {maximum_bytes} bytes."
                )

            if appending:
                # Both hashes already hold the bytes on disk, so the checksums describe the whole
                # object and not only what this attempt fetched.
                digest, md5 = seeded
                downloaded = resume_from
                if progress_callback:
                    progress_callback(downloaded, total_declared)
            else:
                digest = hashlib.sha256()
                md5 = hashlib.md5()
                downloaded = 0

            # read1 returns what the socket has, up to a MiB. read(amt) waited for the whole MiB, so a
            # slow transfer reached the callback, and through it a cancel and the lease heartbeat, once
            # a MiB however long that took; and the bytes of an unfinished MiB were lost at a stall.
            read = getattr(response, "read1", None) or response.read
            with partial.open("ab" if appending else "wb") as output:
                if not appending:
                    _write_part_validators(validators_path, validators)
                reported, reported_at = downloaded, time.monotonic()
                while True:
                    chunk = read(_DOWNLOAD_CHUNK_BYTES)
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
                    if progress_callback and (
                        downloaded - reported >= _DOWNLOAD_CHUNK_BYTES
                        or time.monotonic() - reported_at >= DOWNLOAD_PROGRESS_INTERVAL_SECONDS
                    ):
                        progress_callback(downloaded, total_declared)
                        reported, reported_at = downloaded, time.monotonic()
                if progress_callback and downloaded != reported:
                    progress_callback(downloaded, total_declared)

        # A SHORT READ IS NOT A COMPLETE DOWNLOAD, and urllib does not say so: a server that
        # declares a Content-Length and then hangs up early simply stops yielding chunks, and the
        # loop above ends exactly as it would on a clean finish. The old code renamed that
        # truncated file into place and reported success with a checksum computed over the part
        # that arrived, so every later stage agreed with it. Found by the resume test, which
        # serves a deliberately truncated response.
        if total_declared and downloaded != total_declared:
            raise DownloadIncomplete(
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


def _hash_part(partial: Path, expected: int, declared: int, progress_callback: Any) -> tuple[Any, Any]:
    """The sha256 and md5 of the .part a resume would append to, which must be the size the stat found.

    The callback hears (expected, declared) about once a second while the hash runs, so a cancel is
    heard during a .part of tens of GB and not only after it; ``declared`` is the whole object's length
    as the response that began the .part gave it, or 0.
    """
    digest = hashlib.sha256()
    md5 = hashlib.md5()
    hashed = 0
    reported_at = time.monotonic()
    with partial.open("rb") as existing:
        for chunk in iter(lambda: existing.read(_DOWNLOAD_CHUNK_BYTES), b""):
            digest.update(chunk)
            md5.update(chunk)
            hashed += len(chunk)
            if progress_callback and time.monotonic() - reported_at >= DOWNLOAD_PROGRESS_INTERVAL_SECONDS:
                progress_callback(expected, declared)
                reported_at = time.monotonic()
    if hashed != expected:
        # The file changed under us between the stat and the read.
        raise ValueError(f"Partial file {partial.name} is {hashed} bytes, expected {expected}.")
    return digest, md5


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


def _validators_changed(validators: dict[str, Any], response: Any) -> bool:
    """Whether a 206 names a different object from the one its .part began on.

    Only a validator present on both sides is compared, so a .part with none (an earlier version's)
    or a server that sends none resumes as before. ETags are compared weakly (RFC 9110 8.8.3.2): a
    W/ prefix alone is not a change, but a different opaque tag is.
    """
    stored_etag = str(validators.get("etag") or "").strip()
    served_etag = str(response.headers.get("ETag") or "").strip()
    if stored_etag and served_etag:
        return stored_etag.removeprefix("W/") != served_etag.removeprefix("W/")
    stored_date = str(validators.get("last_modified") or "").strip()
    served_date = str(response.headers.get("Last-Modified") or "").strip()
    return bool(stored_date and served_date and stored_date != served_date)


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


def conversion_polarity_declaration(project: RepositoryProject | dict[str, Any]) -> dict[str, Any]:
    """The ion mode a campaign's convert stage reads for a unit, and the polarity it imputes from it.

    Read from the Catalog handoff the project carries (DECLARED_ION_MODE_FIELD), never from project.ion_mode.
    A split part carries its parent's handoff, copied with the rest of its project, so a part reads the
    declaration of the unit whose lease converted its files, its raw owner's, as CONV-1 does.

    Returns the field read; the value declared there, as the handoff gives it (None where nothing is); the
    analysis unit whose handoff it is (declared_by); the polarity imputed, None unless exactly one polarity,
    Positive or Negative, is declared; and, where none is imputed, why.
    """
    metadata = project.repository_metadata if isinstance(project, RepositoryProject) else (project or {}).get(
        "repository_metadata"
    )
    handoff = metadata.get("catalog_handoff") if isinstance(metadata, dict) else None
    settings = handoff.get("technical_settings") if isinstance(handoff, dict) else None
    declared = str(settings.get("ion_mode") or "").strip() if isinstance(settings, dict) else ""
    declaration: dict[str, Any] = {
        "field": DECLARED_ION_MODE_FIELD,
        "declared": declared or None,
        "declared_by": (str(handoff.get("analysis_unit_id") or "").strip() or None) if isinstance(handoff, dict) else None,
        "impute_polarity": DECLARED_POLARITIES.get(declared.casefold()),
    }
    if declaration["impute_polarity"] is None:
        declaration["reason"] = (
            "The unit carries no Catalog handoff, so no ion mode is declared for it." if not isinstance(handoff, dict)
            else "The unit's Catalog handoff declares no ion mode." if not declared
            else f"The unit's Catalog handoff declares {declared}, not one polarity."
        )
    return declaration


def campaign_conversion_options(declaration: dict[str, Any]) -> ConversionOptions:
    """The converter's options under a declaration (conversion_polarity_declaration): every inference off but
    the polarity it gives, recorded with the declaration and the field it was read from."""
    declared = declaration.get("declared")
    return ConversionOptions(
        impute_polarity=declaration.get("impute_polarity"),
        declared_ion_mode=declared,
        declared_ion_mode_field=declaration["field"] if declared else None,
    )


def _conversion_plan(
    names: list[str], outcome: dict[str, Any] | None = None, options: ConversionOptions | None = None
) -> dict[str, Any]:
    """What a unit will convert from mzXML, as evaluate_eligibility records it before any byte is fetched.

    ``names`` are the listed files and the sample rows' raw files that are mzXML; for a unit that lists only
    a study archive, the samples' names are all there is until the archive is extracted, and the convert
    stage decides there which files are converted (an mzXML a readable encoding of the same sample came
    out beside is not). ``outcome`` is what an earlier lease's convert stage recorded, kept. ``options`` are
    those the convert stage will use (campaign_conversion_options), every inference off where none is given.
    """
    plan: dict[str, Any] = {
        "schema": CONVERSION_PLAN_SCHEMA,
        "target": "mzML",
        "converter": CONVERTER_NAME,
        # Every inference flag off, as the user decided on 2026-09-30, but the polarity a unit's declaration
        # gives, as the user decided on 2026-10-02: nothing else the mzXML does not record is supplied.
        "options": asdict(options or ConversionOptions()),
        "stage": "the download lease's convert stage, after extraction and before input discovery",
        "named_inputs": len(names),
        "names": names[:50],
    }
    if isinstance(outcome, dict) and outcome:
        plan["outcome"] = outcome
    return plan


def _no_converted_input_reason(outcome: Any) -> str:
    """The exclusion reason of a unit whose lease converted mzXML and was left with no input, else ''.

    Its mzXML failed their conversion, or contradicted the polarity the unit declares and were excluded
    (POLARITY_CONTRADICTS_DECLARATION), or both.
    """
    if not isinstance(outcome, dict) or outcome.get("analysis_inputs") != 0:
        return ""
    failed = outcome.get("failed") or 0
    contradicting = outcome.get(POLARITY_CONTRADICTS_DECLARATION) or 0
    lost = [
        *([f"{failed} of the unit's mzXML file(s) failed their conversion ({CONVERSION_FAILED})"] if failed else []),
        *(
            [
                f"{contradicting} of its mzXML file(s) record the polarity opposite to its declared ion mode in some "
                f"scans and none in others ({POLARITY_CONTRADICTS_DECLARATION})"
            ]
            if contradicting
            else []
        ),
    ]
    if not lost:
        return ""
    return f"{NO_CONVERTED_INPUT_REASON}: {'; '.join(lost)}, and no other input of the unit remains."


def _conversion_required_reasons(project: RepositoryProject) -> list[str]:
    """Outside a campaign: a unit that names an mzXML or mzData is excluded before any byte is fetched.

    MS-DIAL has no reader for either. Such a unit is requires_conversion and needs a reviewed
    ProteoWizard-to-mzML conversion with new provenance before it is reanalysed, as it always did; only a
    campaign's lease converts (_campaign_conversion_reasons). A conversion plan is not kept: nothing
    here converts. What a campaign lease recorded converting (an outcome) is.
    """
    if not (project.conversion_plan or {}).get("outcome"):
        project.conversion_plan = {}
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
    if not conversion_required:
        return []
    ordered_conversion_inputs = sorted(conversion_required, key=str.casefold)
    preview = ", ".join(ordered_conversion_inputs[:3])
    if len(conversion_required) > 3:
        preview += f", and {len(conversion_required) - 3} more"
    return [
        f"{CONVERSION_REQUIRED_REASON}. Convert the declared analysis input(s) to mzML "
        f"with ProteoWizard msconvert before reanalysis: {preview}."
    ]


def _campaign_conversion_reasons(project: RepositoryProject) -> list[str]:
    """In a campaign: an mzXML plans its conversion, and what nothing converts excludes the unit.

    The user decided on 2026-09-30 that a campaign's mzXML-only data are converted to mzML and run. The
    lease's convert stage does it (CONVERTIBLE_SUFFIXES), so here an mzXML, packed or not, only plans a
    conversion (project.conversion_plan); what nothing converts - mzData, and whatever else the Catalog
    marks as needing a conversion it has no route for - still excludes the unit. So does a lease's record
    that no input survived its conversions.
    """
    reasons: list[str] = []
    convertible: set[str] = set()
    unconvertible: set[str] = set()
    for item in project.files:
        if item.role == "requires_conversion" or (
            item.role in ANALYSIS_INPUT_ROLES and requires_msdial_conversion(item.name)
        ):
            (convertible if is_convertible_input(item.name) else unconvertible).add(item.name)
    for sample in project.sample_metadata or []:
        raw_file = str((sample or {}).get("raw_file") or "").strip()
        if is_convertible_input(raw_file):
            convertible.add(raw_file)
        elif requires_msdial_conversion(raw_file):
            unconvertible.add(raw_file)
    if unconvertible:
        ordered = sorted(unconvertible, key=str.casefold)
        preview = ", ".join(ordered[:3])
        if len(ordered) > 3:
            preview += f", and {len(ordered) - 3} more"
        reasons.append(f"{UNCONVERTIBLE_INPUT_REASON} (mzData or another format; only mzXML is converted): {preview}.")
    outcome = (project.conversion_plan or {}).get("outcome")
    if convertible:
        project.conversion_plan = _conversion_plan(
            sorted(convertible, key=str.casefold),
            outcome,
            campaign_conversion_options(conversion_polarity_declaration(project)),
        )
    elif not outcome:
        project.conversion_plan = {}
    survivor = _no_converted_input_reason(outcome)
    if survivor:
        reasons.append(survivor)
    return reasons


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
    # WHAT MS-DIAL CANNOT READ. Only a campaign unit's mzXML is converted, and only a unit's own: an
    # accession-level project has no Catalog decision of which files are its inputs.
    if policy.convert_mzxml and project.analysis_unit_id:
        reasons.extend(_campaign_conversion_reasons(project))
    else:
        reasons.extend(_conversion_required_reasons(project))
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


def unit_download_objects(project: dict[str, Any]) -> list[dict[str, Any]]:
    """The objects one unit's lease fetches, one per URL, in the order of its files.

    As the Catalog's handoff lists them (download_scope.objects, Catalog 0.6.1: name, kind, bytes,
    size_known and every consumer unit), or, for a handoff that lists none, as the unit's own files make
    them: each listed path counted once at its largest size, and an object holding a path of no listed size
    of unknown size (bytes None), never 0.
    """
    files = [item for item in project.get("files") or [] if isinstance(item, dict) and str(item.get("url") or "")]
    declared = {
        str(item.get("url")): item
        for item in (project.get("download_scope") or {}).get("objects") or []
        if isinstance(item, dict) and str(item.get("url") or "")
    }
    repository = str(project.get("repository") or "")
    objects: list[dict[str, Any]] = []
    for url in dict.fromkeys(str(item["url"]) for item in files):
        if url in declared:
            item = declared[url]
            known = int(item.get("known_bytes") or item.get("bytes") or 0)
            size_known = bool(item.get("size_known", item.get("bytes") is not None))
            objects.append(
                {
                    "url": url,
                    "name": str(item.get("name") or _url_basename(url)),
                    "kind": str(item.get("kind") or ""),
                    "bytes": known if size_known else None,
                    "known_bytes": known,
                    "size_known": size_known,
                    "catalog_consumer_unit_ids": [str(unit) for unit in item.get("consumer_unit_ids") or []],
                    "declared_by": "catalog_download_objects",
                }
            )
            continue
        listed = [item for item in files if str(item["url"]) == url]
        sizes: dict[str, int] = {}
        for item in listed:
            name = str(item.get("name") or "")
            sizes[name] = max(sizes.get(name, 0), int(item.get("size_bytes") or 0))
        unknown = any(size <= 0 for size in sizes.values())
        known = sum(size for size in sizes.values() if size > 0)
        roles = {str(item.get("role") or "raw") for item in listed}
        if repository == "mb_post" or len(sizes) > 1:
            kind = "bundle"
        elif roles & ARCHIVE_ROLES:
            kind = "archive"
        else:
            kind = "file"
        name = f"{project.get('accession')}.tar" if repository == "mb_post" else (
            _url_basename(url) or PurePosixPath(next(iter(sizes), "").replace("\\", "/")).name
        )
        objects.append(
            {
                "url": url,
                "name": name,
                "kind": kind,
                "bytes": None if unknown else known,
                "known_bytes": known,
                "size_known": not unknown,
                "catalog_consumer_unit_ids": [],
                "declared_by": "unit_files",
            }
        )
    return objects


def plan_batch_downloads(units: list[dict[str, Any]], workspace_root: str | Path | None = None) -> dict[str, Any]:
    """What fetching a batch of units through the download store amounts to. Changes nothing.

    ``units`` are {analysis_unit_id, repository, accession, objects (unit_download_objects)} in the batch's
    order. Returns the distinct objects, each with the batch units that consume it; per unit its objects and
    bytes; the per-unit total (what fetching per unit would move) against the distinct total (what the store
    moves, each object once per accession); the sharing groups - units joined, directly or through others,
    by an object more than one of them needs - and a run order that keeps each group together, so that no
    object waits on disk longer than its group takes. Bytes count only objects of known size, the
    lower-bound figures add what is stated of the others, and an object of unknown size is counted, never
    priced at 0. With ``workspace_root``, an object already ready in its accession's store transfers nothing.
    """
    by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    unit_keys: dict[str, list[tuple[str, str, str]]] = {}
    order: list[str] = []
    for unit in units:
        unit_id = str(unit.get("analysis_unit_id") or "")
        order.append(unit_id)
        keys = unit_keys.setdefault(unit_id, [])
        for item in unit.get("objects") or []:
            key = (str(unit.get("repository") or ""), str(unit.get("accession") or ""), str(item["url"]))
            entry = by_key.setdefault(
                key, {**item, "repository": key[0], "accession": key[1], "consumers": []}
            )
            if unit_id not in entry["consumers"]:
                entry["consumers"].append(unit_id)
            if key not in keys:
                keys.append(key)
    for entry in by_key.values():
        entry["in_store"] = False
        if workspace_root is None:
            continue
        try:
            store = DownloadStore(workspace_root, entry["repository"], entry["accession"])
            index = store.lookup(entry["url"]) if store.root.is_dir() else None
            stored = store.entry(index["object_id"]) if index and index.get("object_id") else None
        except (StoreError, OSError):
            continue
        if stored and stored.get("state") == "ready":
            entry.update(in_store=True, store_object_id=stored.get("object_id"))

    # Sharing groups: connected components of the batch's units, joined by the objects they share.
    parent = {unit: unit for unit in order}

    def root(unit: str) -> str:
        while parent[unit] != unit:
            parent[unit] = parent[parent[unit]]
            unit = parent[unit]
        return unit

    for entry in by_key.values():
        consumers = entry["consumers"]
        for other in consumers[1:]:
            left, right = root(consumers[0]), root(other)
            if left != right:
                first, second = sorted((left, right), key=order.index)
                parent[second] = first
    groups: dict[str, list[str]] = {}
    for unit in order:
        groups.setdefault(root(unit), []).append(unit)

    def totals(entries: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "distinct_bytes": sum(item["known_bytes"] for item in entries if item["size_known"]),
            "distinct_bytes_lower_bound": sum(item["known_bytes"] for item in entries),
            "unknown_size_objects": sum(1 for item in entries if not item["size_known"]),
        }

    group_records = []
    group_of: dict[str, str] = {}
    for index, members in enumerate(groups.values(), start=1):
        group_id = f"group-{index}"
        keys = list(dict.fromkeys(key for unit in members for key in unit_keys[unit]))
        group_records.append(
            {
                "group_id": group_id,
                "unit_ids": members,
                "object_count": len(keys),
                "shared_object_count": sum(1 for key in keys if len(by_key[key]["consumers"]) > 1),
                **totals([by_key[key] for key in keys]),
            }
        )
        group_of.update((unit, group_id) for unit in members)
    run_order = [unit for record in group_records for unit in record["unit_ids"]]
    per_unit = []
    for unit in order:
        entries = [by_key[key] for key in unit_keys[unit]]
        unit_totals = totals(entries)
        size_known = bool(entries) and not unit_totals["unknown_size_objects"]
        per_unit.append(
            {
                "analysis_unit_id": unit,
                "object_count": len(entries),
                "bytes": unit_totals["distinct_bytes"] if size_known else None,
                "known_bytes": unit_totals["distinct_bytes_lower_bound"],
                "size_known": size_known,
                "shared_object_count": sum(1 for item in entries if len(item["consumers"]) > 1),
                "sharing_group": group_of[unit],
                "run_position": run_order.index(unit) + 1,
            }
        )
    objects = list(by_key.values())
    distinct = totals(objects)
    to_transfer = [item for item in objects if not item["in_store"]]
    return {
        "schema": "msdial-batch-download-plan.v1",
        "object_count": len(objects),
        "shared_objects": sum(1 for item in objects if len(item["consumers"]) > 1),
        "per_unit_known_bytes": sum(item["known_bytes"] for item in per_unit),
        **distinct,
        "objects_in_store": len(objects) - len(to_transfer),
        "distinct_bytes_to_transfer": sum(item["known_bytes"] for item in to_transfer if item["size_known"]),
        "units_of_unknown_size": sum(1 for item in per_unit if not item["size_known"]),
        "group_count": len(group_records),
        "groups": group_records,
        "run_order": run_order,
        "units": per_unit,
        "objects": objects,
    }


def pre_claim_downloads(workspace_root: str | Path, units: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record a pending store claim of each unit on each object it will fetch, before any of them is leased.

    A pending claim keeps an object through the release of another unit that shares it, so a unit approved
    in a batch but not run yet still counts when the store decides what it may delete. Idempotent: a live
    claim is kept as it is. ``units`` are as plan_batch_downloads takes them.
    """
    claimed = []
    for unit in units:
        unit_id = str(unit.get("analysis_unit_id") or "")
        store = DownloadStore(workspace_root, unit.get("repository"), unit.get("accession"))
        states = [
            store.claim(str(item["url"]), unit_id, source="batch_plan").get("state")
            for item in unit.get("objects") or []
        ]
        claimed.append(
            {
                "analysis_unit_id": unit_id,
                "store": str(store.root),
                "claims": len(states),
                "live": sum(1 for state in states if state in {"pending", "materialized"}),
            }
        )
    return claimed


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
    store_mode: str | None = None,
    shared_download_callback: Any = None,
) -> dict[str, Any]:
    """Download one unit's objects into its workspace and write the unit's run manifest.

    ``campaign_authorization`` is the crossing record a validated campaign approval produced for this
    download (msdial_app.campaign_authorization); it is written into the manifest from the first write
    on. None, the default, writes nothing about a campaign.

    ``job_id`` is the backend job the lease runs in. It is recorded with this process as the lease's
    owner, so a lease left "downloading" by a process that has since died can be told from a live one.

    THE ACCESSION DOWNLOAD STORE (msdial_app.download_store) is used by a campaign's lease, and by any
    other only where ``store_mode`` - by default the saved setting (user_settings.download_store_mode) -
    is "always". Its lease fetches each object into <workspace_root>\\<repository>\\<accession>\\_dl once
    for every unit of the accession, under a claim of this unit's, extracts each archive there once, and
    gives this unit a raw tree of hardlinks to it (see _StoreLease). Without it, the default, every
    object is fetched into this unit's own raw tree as it always was. ``shared_download_callback(event,
    detail)`` hears waiting_for_shared_download while another lease holds an object this one needs, and
    shared_download_ready once it has it.

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
    - materialise: with the download store, this unit's raw tree of links to the store's objects and
      extraction trees (raw_storage); without it, not_used. Links that turn out to be none of the unit's
      inputs, members or sidecars are pruned once the attribute stage has said which those are;
    - convert: each of the unit's mzXML written as mzML under raw\\converted, every inference off but the
      polarity the unit's Catalog handoff declares, given to scans that record none where it declares
      exactly one (conversion_polarity_declaration), and recorded (input_conversions,
      provenance\\input-conversions.json, the declaration and its field among them); a file some of whose
      scans record the other polarity and some none is not converted (polarity_contradictions); where
      extraction shows a readable encoding of the same sample beside an mzXML, that one is analysed instead.
      Only under a ``campaign_authorization``: not used outside a campaign, where an mzXML is no input,
      nor where the unit has no mzXML. See the notes above _find_mzxml_files;
    - discover: every vendor folder listed member by member checked whole (container_completeness),
      then the MS-DIAL inputs under the data root, outermost folders only, and the mzML converted;
    - attribute: the unit's own inputs - by path, every one, when the Catalog declared them
      (analysis_inputs); a converted mzML through the mzXML it was read from - extracted files and
      declared checksums (allowlist_checksum_validation), and one input_lineage row per input. An mzML
      whose binary arrays RawDataHandler cannot decode is not an input: it is listed in
      excluded_input_candidates and in input_lineage's excluded rows, with reason
      unsupported_mzml_encoding and the accessions found (_exclude_undecodable_inputs). Nor is an mzXML
      whose conversion failed (conversion_failed), or whose scans contradict the polarity the unit declares
      (polarity_contradicts_declaration); a unit left with no input is recorded, excluded, and not raised;
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
    reserved = [
        f"{label} {value!r}"
        for label, value in (
            ("repository", project.repository), ("accession", project.accession), ("unit", project.analysis_unit_id)
        )
        if is_reserved_workspace_name(value)
    ]
    if reserved:
        raise ValueError(
            f"The {', '.join(reserved)} is a name the workspace keeps for itself "
            f"({', '.join(sorted(RESERVED_WORKSPACE_NAMES))}); no unit is leased under it."
        )
    client = client or RepositoryHttpClient()
    store, store_use = _lease_store(project, workspace_root, campaign_authorization, store_mode)
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
    if store is not None:
        # From the first write on, so a lease that stops part-way says whose store holds its claims.
        lease_record["download_cache"] = _download_cache_record(store, project, store_use, [])
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

        def waiting_beat(name: str) -> None:
            # While this lease waits for another's extraction of an object both need: its heartbeat, and its
            # progress callback, which is where a cancelled job is heard.
            beat(downloaded_bytes)
            if progress_callback:
                progress_callback(
                    total_objects, total_objects, name, downloaded_bytes, required_download_bytes or downloaded_bytes
                )

        store_lease = None if store is None else _StoreLease(
            store,
            project,
            store_use,
            lease_record,
            client=client,
            data_root=data_root,
            provenance=provenance,
            stages=stages,
            job_id=job_id,
            heartbeat=waiting_beat,
            shared_download_callback=shared_download_callback,
        )
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
            if store_lease is None:
                result = client.download(
                    url,
                    destination,
                    maximum_bytes - downloaded_bytes,
                    progress_callback=item_progress,
                )
            else:
                # Into the store, once for every unit of the accession; a per-file object's path is where
                # this unit's link to it will be, an archive's the store's own (see _StoreLease.fetch).
                result = store_lease.fetch(
                    url,
                    item,
                    filename,
                    None if archive_kind else _safe_relative_name(item.name).as_posix(),
                    maximum_bytes - downloaded_bytes,
                    item_progress,
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
            if store_lease is None:
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
            else:
                # Once in the store, for every unit; this unit's members are linked in at materialise.
                record, members = store_lease.extract(
                    item,
                    placements[_file_key(item["path"])],
                    len(archive_extractions) + 1,
                    archive_extractions,
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
            **(store_lease.extraction_counts() if store_lease is not None else {}),
        )
        if store_lease is None:
            stages.not_used(
                "materialise",
                "Every object was fetched into this unit's own raw tree; no accession download store is in use."
                if store_use.get("reason") is None
                else store_use["reason"],
            )
        else:
            stages.start("materialise")
            stages.finish("materialise", **store_lease.materialise())

        # The unit's mzXML, written as mzML under raw\converted (see the notes above _find_mzxml_files). Only a
        # campaign's lease converts: without a campaign authorization, and in a unit with no mzXML of its own,
        # the stage is not used, as in every lease before, and an mzXML is no input.
        archive_samples = _archive_sample_attribution(
            project, archive_extractions, extracted_members, data_root
        )
        mzxml_found = _find_mzxml_files(data_root) if campaign_authorization else []
        conversion_sources = _select_conversion_sources(
            mzxml_found, data_root, project, archive_samples, archive_extractions
        ) if mzxml_found else []
        conversion: dict[str, Any] | None = None
        stands_for: dict[str, str] = {}
        if conversion_sources:
            stages.start("convert")
            readable = _find_msdial_inputs(data_root)
            choices, stands_for = _encoding_choices(
                conversion_sources,
                data_root,
                readable,
                _extracted_keys(extracted_members, data_root),
                _readable_inputs_admitted(readable, data_root, project, archive_samples),
            )
            chosen_over = {_file_key(item["mzxml"]) for item in choices}

            def between(name: str) -> None:
                beat(downloaded_bytes)
                if progress_callback:
                    # Where a cancelled job is heard during the conversions: the callback raises.
                    progress_callback(
                        total_objects, total_objects, f"converting {name}", downloaded_bytes,
                        required_download_bytes or downloaded_bytes,
                    )

            # The polarity a scan recording none is given: the unit's Catalog handoff's, where it declares
            # exactly one, and never project.ion_mode, which a preflight rewrites from what was imputed.
            declaration = conversion_polarity_declaration(project)
            conversion = _run_lease_conversions(
                [item for item in conversion_sources if _file_key(item) not in chosen_over],
                data_root,
                raw_root,
                provenance,
                choices,
                declaration=declaration,
                between=between,
            )
            stands_for = {**stands_for, **conversion["outputs"]}
            stages.finish(
                "convert",
                mzxml_found=len(mzxml_found),
                impute_polarity=declaration["impute_polarity"],
                **conversion["block"]["counts"],
            )
        else:
            stages.not_used(
                "convert",
                "No mzXML found here is one of this unit's inputs." if mzxml_found
                else "No input conversion ran in this lease.",
            )

        stages.start("discover")
        # Before anything is discovered: a folder that did not arrive whole is no input at all.
        container_completeness = verify_container_completeness(data_root, project)
        all_inputs = _find_msdial_inputs(data_root)
        if conversion is not None:
            # The mzML the convert stage wrote, beside the data root rather than under it.
            all_inputs += [
                str(Path(item["output"]["path"]).resolve())
                for item in conversion["block"]["records"]
                if item.get("status") == "converted"
            ]
        stages.finish(
            "discover",
            input_candidates=len(all_inputs),
            **(
                {"vendor_folders_complete": container_completeness["containers"]}
                if container_completeness["required"]
                else {}
            ),
        )

        stages.start("attribute")
        selected_extracted = _filter_project_allowlist_paths(
            extracted, data_root, project, archive_samples=archive_samples
        )
        verified_checksums: dict[str, dict[str, Any]] = {}
        checksum_validation = _verify_project_allowlist_checksums(
            data_root, project, verified_checksums, downloads, archive_extractions
        )
        inputs = _filter_inputs_by_project_allowlist(
            all_inputs,
            data_root,
            project,
            archive_samples=archive_samples,
            archive_extractions=archive_extractions,
            stands_for=stands_for,
            set_aside=[item["path"] for item in (conversion or {}).get("excluded") or []],
        )
        inputs, excluded_inputs, mzml_scanned = _exclude_undecodable_inputs(inputs)
        ignored_inputs = len(all_inputs) - len(inputs) - len(excluded_inputs)
        if conversion is not None:
            # An mzXML whose conversion failed, or whose scans contradict the declared polarity, is no
            # candidate, as an undecodable mzML is none.
            excluded_inputs = [*conversion["excluded"], *excluded_inputs]
        analysis_input = _common_input_path(inputs, data_root)
        kept = {_file_key(item) for item in inputs}
        conversions, conversion_rows = _conversion_lineage(
            conversion,
            kept,
            downloads,
            extracted_from,
            data_root,
            download_root,
            project,
            verified_checksums,
            extracted_members=extracted_members,
            archive_extractions=archive_extractions,
        )
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
            excluded_inputs=excluded_inputs,
            conversions=conversions,
            stands_for={key: value for key, value in stands_for.items() if key in kept},
        )
        if conversion_rows:
            input_lineage["conversion_sources"] = conversion_rows
        pruned: dict[str, Any] = {}
        if store_lease is not None:
            # Now that the unit's own files are known: the links to anything else the store's objects
            # carried (the other polarity's samples in a shared study archive) are removed from this
            # unit's tree. The store keeps their bytes for the units that need them.
            pruned = store_lease.prune(
                [
                    *inputs,
                    *(str(item["path"]) for item in excluded_inputs),
                    *conversion_sources,
                    *selected_extracted,
                    *(Path(key) for key in verified_checksums),
                    *(str(item["path"]) for item in downloads if not item.get("archive")),
                ]
            )
        stages.finish(
            "attribute",
            input_candidates=len(inputs),
            ignored_input_candidates=ignored_inputs,
            mzml_encodings_scanned=mzml_scanned,
            excluded_input_candidates=len(excluded_inputs),
            extracted_files=len(selected_extracted),
            ignored_extracted_files=len(extracted) - len(selected_extracted),
            declared_files_verified=checksum_validation.get("verified", 0),
            archives_verified_at_download=checksum_validation.get("archives_verified_at_download", 0),
            **({"pruned_links": pruned.get("removed_files", 0)} if store_lease is not None else {}),
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
            "ignored_input_candidate_count": ignored_inputs,
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
        if excluded_inputs:
            # Only where one was, so a unit with nothing excluded records what it always did.
            manifest["excluded_input_candidates"] = excluded_inputs
        if conversion is not None:
            # Only where the convert stage ran. The gate's SUM-1 follows each converted input to its record
            # here, and its CONV-1 holds the mzML to it; provenance\input-conversions.json is the same block.
            manifest["input_conversions"] = conversion["block"]
            _record_conversion_outcome(project, conversion, inputs)
            manifest["project"] = project.as_dict()
            manifest["execution_allowed"] = project.eligible
        if container_completeness["required"]:
            manifest["container_completeness"] = container_completeness
        warnings = _archive_warnings(archive_extractions)
        if warnings:
            manifest["archive_warnings"] = warnings
        if store_lease is not None:
            # Only where the store was used, so every other lease records what it always did.
            manifest["download_cache"] = store_lease.cache_record()
            manifest["raw_storage"] = store_lease.raw_storage
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
    attempts = getattr(error, "download_attempts", None)
    if isinstance(attempts, list):
        # RepositoryHttpClient.download: each attempt at the object that failed, and how it ended.
        record["download_failure"]["attempts"] = attempts
        if isinstance(error, RetryableDownloadError):
            record["download_failure"]["retryable"] = True
    if isinstance(error, ArchiveError):
        failure = error.record()
        failure["rejected_members"] = failure["rejected_members"][:50]
        record["download_failure"]["archive_failure"] = failure
    if isinstance(error, MaterializationCollision):
        # The download store's: the paths two of its objects, or an object and a file already there, wanted.
        record["download_failure"]["materialization_collisions"] = error.collisions[:20]
    try:
        _write_json(manifest_path, record)
    except (OSError, ValueError, TypeError):
        pass


# The stages of a download lease, in the order they run. materialise (the accession download store) is
# not_used in a lease that does not use the store; convert (mzXML to mzML) is not_used in a unit with no
# mzXML of its own.
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

    The writes are the lease's own keys only (lease_stages, downloads, archive_extractions, and
    download_cache where the download store is used),
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
                if "download_cache" in self.lease_record:
                    current["download_cache"] = self.lease_record["download_cache"]
                _write_json(self.manifest_path, current)
        except (OSError, ValueError, TypeError):
            pass


# ---- the accession download store in the lease ------------------------------------------------------------
#
# WHY. A Workbench study archive lists every sample of a study, and the store's design measured 489 declared
# units touching a URL another unit lists: fetched per unit, 14.37 TB move where fetching each URL once moves
# 8.22 TB, and ST001408.zip alone is 928 GB used by three units. The user decided on 2026-09-30 that a shared
# object is downloaded once. download_store.py (0.5.13) holds the bytes; until now no lease used it.
#
# WHAT THE STORE OWNS AND WHAT THE UNIT OWNS. The store owns the downloaded objects and their extraction
# trees, under <workspace_root>\<repository>\<accession>\_dl, kept alive by one claim per consuming unit.
# Each unit still owns its own raw tree, <workspace>\raw, so every deletion that requires raw to be that
# folder (cleanup_download_lease, discard_download_lease, cleanup_split_parent) works on it as before: the
# tree holds hardlinks to the store's files, which MS-DIAL reads, and MS-DIAL's intermediates written beside
# them stay in the unit's tree and never reach the store. A unit's release of its tree releases its claims,
# and the store deletes an object only when no live claim holds it, under the campaign approval that covers
# boundary 5 for every unit that released it (DownloadStore.gc). Split parts never claim: the parent's claims
# stand for its parts until the parent's own release.
#
# RESERVED NAMES. _dl is the store's directory beside an accession's units, and _campaigns the campaign
# runner's beside the repositories; neither is ever a unit, an accession or a repository.
RESERVED_WORKSPACE_NAMES = frozenset({STORE_DIRECTORY, "_campaigns"})
DOWNLOAD_CACHE_SCHEMA = "msdial-download-cache.v1"
RAW_STORAGE_SCHEMA = "msdial-raw-storage.v1"
STORE_RELEASE_SCHEMA = "msdial-download-store-release.v1"
# How long one wait for another lease's lock lasts before the lease beats its heartbeat and hears a cancel.
STORE_WAIT_POLL_SECONDS = 15.0


def is_reserved_workspace_name(name: Any) -> bool:
    return str(name or "").strip().casefold() in {item.casefold() for item in RESERVED_WORKSPACE_NAMES}


def _lease_store(
    project: RepositoryProject,
    workspace_root: Path,
    campaign_authorization: dict[str, Any] | None,
    store_mode: str | None,
) -> tuple[DownloadStore | None, dict[str, Any]]:
    """The download store this lease uses, or None, and why.

    A campaign's lease uses it; any other only where store_mode (the argument, else the saved setting) is
    "always". A unit without an analysis unit id has no claim to hold, and is leased as before.
    """
    from .user_settings import STORE_MODES, download_store_mode

    mode = str(store_mode or "").strip().casefold()
    mode = mode if mode in STORE_MODES else download_store_mode()
    use: dict[str, Any] = {"store_mode": mode, "reason": None}
    if campaign_authorization:
        use["activated_by"] = "campaign_authorization"
    elif mode == "always":
        use["activated_by"] = "store_mode"
    else:
        return None, use
    if not project.analysis_unit_id:
        use["reason"] = (
            "The accession download store was asked for, but the unit has no analysis unit id to claim its "
            "objects under; every object was fetched into this unit's own raw tree."
        )
        return None, use
    try:
        return DownloadStore(workspace_root, project.repository, project.accession), use
    except StoreError as error:
        use["reason"] = (
            f"The accession download store was asked for, but cannot be placed: {error} Every object was "
            "fetched into this unit's own raw tree."
        )
        return None, use


def _download_cache_record(
    store: DownloadStore, project: RepositoryProject, use: dict[str, Any], objects: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema": DOWNLOAD_CACHE_SCHEMA,
        "store": str(store.root),
        "workspace_root": str(store.root.parent.parent.parent),
        "repository": store.repository,
        "accession": store.accession,
        "unit_id": project.analysis_unit_id,
        "activated_by": use.get("activated_by"),
        "store_mode": use.get("store_mode"),
        "objects": objects,
    }


class _LeaseFetcher:
    """The store's fetcher over the lease's client, so a store fetch keeps the client's idle timeouts and
    retries (RepositoryHttpClient.download) and the attempts it records."""

    def __init__(self, client: Any, maximum_bytes: int) -> None:
        self.client = client
        self.maximum_bytes = maximum_bytes
        self.results: list[dict[str, Any]] = []

    def fetch(self, url: str, destination: Path, progress_callback: Any = None) -> dict[str, Any]:
        result = self.client.download(url, destination, self.maximum_bytes, progress_callback=progress_callback)
        self.results.append(result)
        return result

    def head(self, url: str) -> int:
        probe = getattr(self.client, "content_length", None)
        return probe(url) if callable(probe) else 0


class _StoreLease:
    """The fetch, extract and materialise stages of one unit's lease through the accession download store.

    fetch: each object through DownloadStore.fetch_or_reuse, which claims it for this unit, transfers it
    only when no verified copy is in the store (one GET for every unit of the accession, with the client's
    idle timeouts and retries), and compares its declared MD5 before anything else sees it. A per-file
    object's downloads[] path is where this unit's link to it will be, so the lineage and the gate find it
    as they always did; an archive's is the store's object, which keeps its name and its suffix. Each entry
    also records cache_object_path, sha256_origin (fetched_by_this_unit or inherited_from_cache) and
    declared_checksum_verified.

    extract: each archive once, in the store (DownloadStore.ensure_extracted), with archives.extract_archive
    and the lease's limits; a unit that finds the tree already there reuses it. The extraction record is the
    store's, with this unit's placement, and its member listing is copied into this unit's provenance,
    where it survives raw deletion, as the per-unit extraction wrote it.

    materialise: every object and tree linked into the unit's raw\\data at the placement the per-unit lease
    used (_route_object), planned whole first, never overwriting: a second source with the same bytes at one
    path is not placed again, and a file already there with its source's bytes is kept, as the per-unit
    merge allowed; anything else refuses the lease (MaterializationCollision). A failed link is a copy, and
    raw_storage says which (hardlink, copy or mixed); so is a file a reader rewrites in place
    (_rewritten_by_its_reader), which would otherwise change the store's record through the link.

    prune: once the attribute stage has said what is the unit's, the links this materialisation made to
    anything else are removed - never a file the unit holds itself.

    While another lease holds an object's lock, this one waits in polls of STORE_WAIT_POLL_SECONDS, telling
    shared_download_callback once (waiting_for_shared_download), beating its heartbeat and calling its
    progress callback between polls, so a cancel is heard during the wait.
    """

    def __init__(
        self,
        store: DownloadStore,
        project: RepositoryProject,
        use: dict[str, Any],
        lease_record: dict[str, Any],
        *,
        client: Any,
        data_root: Path,
        provenance: Path,
        stages: _LeaseStages,
        job_id: str,
        heartbeat: Any,
        shared_download_callback: Any = None,
    ) -> None:
        self.store = store
        self.project = project
        self.unit_id = project.analysis_unit_id
        self.client = client
        self.data_root = data_root
        self.provenance = provenance
        self.stages = stages
        self.job_id = job_id
        # Called between polls of a wait outside the fetch: the lease's heartbeat, and its progress callback,
        # which is where a cancelled job is heard.
        self.heartbeat = heartbeat
        self.shared_download_callback = shared_download_callback
        self.objects: list[dict[str, Any]] = []
        self.placements: list[dict[str, Any]] = []
        self.tree_records: list[tuple[int, dict[str, Any]]] = []
        self.placed: dict[str, dict[str, Any]] = {}
        self.raw_storage: dict[str, Any] = {}
        self.record = _download_cache_record(store, project, use, self.objects)
        # The record a stage write persists while the lease runs (_LeaseStages.persist).
        lease_record["download_cache"] = self.record

    def _wait(self, call: Any, between: Any, label: str) -> Any:
        told: list[dict[str, Any]] = []

        def on_wait(holder: dict[str, Any]) -> None:
            if told:
                return
            detail = {**holder, "unit_id": self.unit_id, "object": label}
            told.append(detail)
            if self.shared_download_callback is not None:
                self.shared_download_callback("waiting_for_shared_download", detail)

        while True:
            try:
                result = call(STORE_WAIT_POLL_SECONDS, on_wait)
                break
            except StoreLockTimeout:
                # Another lease is still fetching or extracting it. Its bytes are this lease's too, so it
                # waits; between polls it beats and hears a cancel through the progress callback.
                between()
        if told and self.shared_download_callback is not None:
            self.shared_download_callback("shared_download_ready", {"unit_id": self.unit_id, "object": label})
        return result

    def fetch(
        self,
        url: str,
        item: RepositoryFile,
        filename: str,
        link_target: str | None,
        maximum_bytes: int,
        progress: Any,
    ) -> dict[str, Any]:
        """One object, as the download record the per-unit fetch would have returned, plus the store's own."""
        declared = str(item.checksum or "").strip()
        if self.project.repository == "mb_post" or not re.fullmatch(r"[0-9a-fA-F]{32}", declared):
            # As _verify_object_checksum: only an object's own MD5 is compared as it arrives.
            declared = ""
        fetcher = _LeaseFetcher(self.client, maximum_bytes)
        try:
            fetched = self._wait(
                lambda timeout, on_wait: self.store.fetch_or_reuse(
                    url,
                    filename,
                    unit_id=self.unit_id,
                    fetcher=fetcher,
                    declared_md5=declared,
                    job_id=self.job_id,
                    progress_callback=progress,
                    on_wait=on_wait,
                    lock_timeout=timeout,
                ),
                lambda: progress(0, 0),
                item.name,
            )
        except DeclaredChecksumMismatch:
            self.stages.at("verify_declared_checksums")
            raise
        result: dict[str, Any] = {
            "path": fetched["object_path"] if link_target is None else str(self.data_root / link_target),
            "size_bytes": int(fetched["size_bytes"]),
            "sha256": fetched["sha256"],
            "md5": fetched["md5"],
            "resumed_from_bytes": int(fetched.get("resumed_from_bytes") or 0),
        }
        if fetcher.results:
            for key in ("etag", "last_modified", "attempts"):
                if fetcher.results[-1].get(key):
                    result[key] = fetcher.results[-1][key]
        result.update(
            cache_object_path=fetched["object_path"],
            cache_object_id=fetched["object_id"],
            sha256_origin=fetched["sha256_origin"],
            declared_checksum_verified=fetched["declared_checksum_verified"],
        )
        self.objects.append(
            {
                key: fetched.get(key)
                for key in (
                    "url", "url_key", "object_id", "name", "size_bytes", "cache_hit", "action", "reuse_reason",
                    "sha256_origin", "fetched_by", "transferred_bytes", "reuse_check", "waited_for_lock",
                    "lock_holder", "recovered_stale_locks", "claim_path", "claim_state",
                )
            }
        )
        if link_target is not None:
            self.placements.append({"url": url, "source": "object", "target": link_target})
        return result

    def extract(
        self,
        download: dict[str, Any],
        placement: str,
        number: int,
        earlier: list[dict[str, Any]],
    ) -> tuple[dict[str, Any], list[tuple[str, dict[str, Any]]]]:
        """One archive's extraction, made once in the store, as this unit's record and members.

        The same (record, members) _extract_into_data_root returns, with the members where materialise will
        link them: under the data root at ``placement``.
        """
        sha256 = str(download.get("sha256") or "")
        object_id = str(download["cache_object_id"])

        def extract(obj: Path, staging: Path) -> dict[str, Any]:
            # extract_archive builds its own staging beside a destination that must not exist yet, and holds
            # every member path to MAX_PATH under that staging: so it extracts to the shortest name the
            # object's directory can give, x, whose staging is x.partial, and the tree is then moved to where
            # the store expects it. The member listing is left beside the tree, in the object's directory.
            short = staging.parent / "x"
            if os.path.lexists(short):
                unlink_tree(short)  # verified and not moved when an earlier extraction stopped
            staging.rmdir()
            record = archives.extract_archive(
                obj,
                short,
                archive_sha256=sha256,
                listing_directory=staging.parent,
                limits=LEASE_EXTRACTION_LIMITS,
            )
            os.replace(short, staging)
            return record

        name = Path(str(download.get("cache_object_path") or download.get("path") or "")).name
        entry = self._wait(
            lambda timeout, on_wait: self.store.ensure_extracted(
                object_id, extract, unit_id=self.unit_id, job_id=self.job_id, lock_timeout=timeout, on_wait=on_wait
            ),
            lambda: self.heartbeat(name),
            name,
        )
        extraction = dict(entry.get("extraction") or {})
        listing = dict(extraction.get("members_tsv") or {})
        if not listing.get("path"):
            raise StoreError(f"The store's extraction of {name} recorded no member listing.")
        listing_directory = self.provenance
        if sha256 and any(str(record.get("archive_sha256") or "") == sha256 for record in earlier):
            listing_directory = self.provenance / "archive-members" / str(number)
        copy_path = listing_directory / Path(str(listing["path"])).name
        _copy_store_listing(Path(str(listing["path"])), copy_path, str(listing.get("sha256") or ""))
        rows = [row for row in _read_members_listing(copy_path) if row.get("disposition") == "extracted"]
        target = self.data_root.joinpath(*placement.split("/")) if placement else self.data_root
        _refuse_long_final_paths(rows, target, LEASE_EXTRACTION_LIMITS, name)
        members: list[tuple[str, dict[str, Any]]] = [
            (str(target.joinpath(*row["path"].split("/"))), {"member": row["path"], "archive": row["archive"]})
            for row in rows
            if row.get("type") == "file"
        ]
        tree = self.store.object_directory(object_id) / "t"
        container_root = str(extraction.get("container_root") or "")
        record = {
            **extraction,
            "members_tsv": {**listing, "path": str(copy_path)},
            "staging_destination": str(tree),
            "destination": str(target),
            "placement": placement,
            "container_path": "/".join(part for part in (placement, container_root) if part),
            "source_url": str(download.get("source_url") or ""),
            "download_path": str(download.get("path") or ""),
            "extracted_file_count": len(members),
            "store_extraction": {
                "object_id": object_id,
                "tree": str(tree),
                "action": entry.get("extraction_action"),
                "extracted_by": (entry.get("tree") or {}).get("extracted_by"),
                "extracted_at": (entry.get("tree") or {}).get("extracted_at"),
            },
        }
        self.tree_records.append((len(self.placements), record))
        self.placements.append({"url": str(download.get("source_url") or ""), "source": "tree", "target": placement})
        return record, members

    def extraction_counts(self) -> dict[str, Any]:
        actions = [str((record.get("store_extraction") or {}).get("action") or "") for _index, record in self.tree_records]
        return {
            "extracted_in_store": sum(1 for action in actions if action in {"extracted", "re-extracted"}),
            "store_extractions_reused": actions.count("reused"),
        }

    def materialise(self) -> dict[str, Any]:
        """Link every object and tree into the unit's tree; returns the stage's summary."""
        placed: dict[str, dict[str, Any]] = {}
        record = self.store.materialize(
            self.unit_id,
            self.data_root,
            self.placements,
            same_content=_same_bytes,
            placed=placed,
            copy_instead=_rewritten_by_its_reader,
        )
        self.placed = placed
        for index, extraction in self.tree_records:
            mine = [(relative, info) for relative, info in placed.items() if info["pair"] == index]
            already = [relative for relative, info in mine if info["how"] in {"present", "kept_existing"}]
            # Members a placement before this one already put at the same path, with the same bytes.
            already += [
                relative for relative, info in placed.items()
                for duplicate in info.get("duplicates") or [] if duplicate["pair"] == index
            ]
            extraction["merge"] = {
                "materialized_from_store": True,
                "linked_files": sum(1 for _relative, info in mine if info["how"] == "linked"),
                "copied_files": sum(1 for _relative, info in mine if info["how"] == "copied"),
                "moved_entries": 0,
                "already_present_files": len(already),
                "already_present": sorted(already)[:50],
            }
        self.raw_storage = {
            "schema": RAW_STORAGE_SCHEMA,
            "materialization": record["materialization"],
            "store": str(self.store.root),
            "data_root": record["data_root"],
            "files": record["files"],
            "directories": record["directories"],
            "linked_files": record["linked_files"],
            "copied_files": record["copied_files"],
            "already_present_files": record["already_present_files"],
            "duplicate_source_files": record.get("duplicate_source_files", 0),
            "kept_existing_files": record.get("kept_existing_files", 0),
            "logical_bytes": record["logical_bytes"],
            "bytes_linked_from_store": record["bytes_linked_from_store"],
            # What this unit holds that the store does not: copies made where a link failed, and files an
            # earlier lease left in the tree with the same bytes.
            "bytes_held_by_unit": int(record["bytes_copied"]) + int(record.get("bytes_kept_existing") or 0),
            "copy_fallback_count": record["copy_fallback_count"],
            "copy_fallbacks": record["copy_fallbacks"],
            "protected_copies": record.get("protected_copies", 0),
            "protected_copy_paths": record.get("protected_copy_paths", []),
        }
        return {
            "objects": len(self.placements),
            "materialization": record["materialization"],
            "files": record["files"],
            "linked_files": record["linked_files"],
            "copied_files": record["copied_files"],
            "already_present_files": record["already_present_files"] + record.get("duplicate_source_files", 0)
            + record.get("kept_existing_files", 0),
            "bytes_linked_from_store": record["bytes_linked_from_store"],
        }

    def prune(self, keep: Iterable[Any]) -> dict[str, Any]:
        """Remove the links this materialisation made to files that are none of the unit's.

        ``keep`` is every path the lease found to be the unit's: its inputs (a folder keeps all it holds),
        the inputs it excluded, the mzXML it converted or chose against, its members of the archives, the
        files whose declared checksums it verified, and its own per-file objects. What the SCIEX reader opens
        beside a kept .wiff or .wiff2 stays with it (travels_with_sciex_file: x.wiff2's x.wiff.scan and
        x.timeseries.data, x.wiff.<n>.scan), as the per-unit lease left it, so that the Console and the
        analysis CSV's alias find them. A name is removed only if it is the store's file (or this lease's copy
        of one); a file the unit holds itself is never removed here, because deleting raw data is not a
        lease's to do.
        """
        files: set[str] = set()
        folders: set[str] = set()
        for value in keep:
            text = str(value or "").strip()
            if not text:
                continue
            key = _file_key(text)
            (folders if Path(text).is_dir() else files).add(key)
        # The kept SCIEX files by their directory, whose companions travel with them.
        sciex: dict[str, list[str]] = {}
        for key in files:
            if Path(key).suffix in SCIEX_SUFFIXES:
                sciex.setdefault(str(Path(key).parent), []).append(Path(key).name)
        removed = removed_bytes = directories = 0
        kept_failures: list[dict[str, Any]] = []
        emptied: set[Path] = set()
        for relative, info in sorted(self.placed.items()):
            if info["how"] not in {"linked", "copied", "present"}:
                continue
            path = self.data_root.joinpath(*relative.split("/"))
            key = _file_key(str(path))
            if key in files or any(str(parent) in folders for parent in Path(key).parents):
                continue
            if any(travels_with_sciex_file(path.name, primary) for primary in sciex.get(str(Path(key).parent), ())):
                continue
            try:
                ours = info["how"] == "copied" or os.path.samefile(path, info["source"])
                size = path.stat().st_size
            except OSError:
                continue
            if not ours:
                continue
            removal = unlink_tree(path)
            if removal["complete"]:
                removed += 1
                removed_bytes += size
                emptied.add(path.parent)
            else:
                kept_failures.extend(removal["kept"])
        root = self.data_root.resolve()
        for directory in sorted(emptied, key=lambda item: len(item.parts), reverse=True):
            current = directory
            while current != root and root in current.parents:
                try:
                    current.rmdir()
                except OSError:
                    break
                directories += 1
                current = current.parent
        result = {
            "removed_files": removed,
            "removed_logical_bytes": removed_bytes,
            "removed_directories": directories,
            "kept_count": len(kept_failures),
            "kept": kept_failures[:_REPORTED_DELETION_ITEMS],
        }
        self.raw_storage["pruned"] = result
        self.raw_storage["files_after_prune"] = int(self.raw_storage.get("files") or 0) - removed
        return result

    def cache_record(self) -> dict[str, Any]:
        record = dict(self.record)
        record["objects_fetched"] = sum(1 for item in self.objects if not item.get("cache_hit"))
        record["cache_hits"] = sum(1 for item in self.objects if item.get("cache_hit"))
        record["transferred_bytes"] = sum(int(item.get("transferred_bytes") or 0) for item in self.objects)
        record["waited_for_shared_download"] = sum(1 for item in self.objects if item.get("waited_for_lock"))
        return record


def _rewritten_by_its_reader(source: Path) -> bool:
    """Whether a store file is one a reader may rewrite in place, and so is copied into a unit, not linked.

    Bruker's baf2sql writes analysis.sqlite into the BAF .d it opens (msdial_app.reader_created). A .d that
    arrived with one would have it rewritten through the unit's link, which is the store's file record and
    every other unit's: the store would find it tainted only afterwards. A copy keeps the store's own.
    """
    return source.name.casefold() in reader_created_names(source.parent)


def _copy_store_listing(source: Path, destination: Path, sha256: str) -> None:
    """Copy the store's member listing into the unit's provenance, refusing one that is not the recorded one."""
    data = source.read_bytes()
    if sha256 and hashlib.sha256(data).hexdigest() != sha256:
        raise StoreError(
            f"The store's member listing {source.name} is not the one its extraction recorded; the object has to "
            "be extracted again before a unit can use it."
        )
    if destination.is_file() and destination.read_bytes() == data:
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, destination)


def _route_object(
    project: RepositoryProject, url: str, item: RepositoryFile, index: int
) -> tuple[str, str, str]:
    """(file name, archive kind by name or '', placement) for one repository object.

    The name is the URL's basename, percent-decoded, or <accession>.tar for MB-POST's one project
    tar; a URL with no basename takes the listed name. When the decoded basename is the listed name
    but for case, the listed spelling is kept. When the URL names no archive but the unit's own
    listing names this object as one (a download link without a file name), the listed name is used,
    so the object is opened as what the repository says it is.

    WHY DECODED. MetaboLights lists '130612_EG_NaHCO3_1 mM-1.raw.zip' and serves it from
    '.../130612_EG_NaHCO3_1%20mM-1.raw.zip' (1,575 per-sample archives in 67 units of the
    2026-09-22 Catalog snapshot). Taken undecoded, the name did not match the listing, so the object
    was placed as a bundle, and an unrooted X.raw.zip unpacked to a folder named '..._1%20mM-1.raw'
    that no sample names; a rooted one drew a container_name_mismatch warning for every archive.

    A file inside a vendor folder (raw/x.d/..., raw/x.raw/...) is never an archive to open, whatever
    its name: unpacking it would take it out of the folder its reader expects it in.

    placement is where, under the data root, the archive expands. A per-sample container (X.raw.zip)
    or a single compressed file (x.mzML.gz) expands in the directory it was listed in, as the
    container it stands for (archives.container_alias); any other archive is a bundle whose members
    keep their own relative paths at the data root, as they always have.
    """
    listed = str(item.name or "").replace("\\", "/")
    listed_name = PurePosixPath(listed).name
    filename = _url_basename(url) or listed_name or f"{project.accession}_{index}.zip"
    if listed_name and listed_name.casefold() == filename.casefold():
        filename = listed_name
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


def _url_basename(url: str) -> str:
    """A URL's last path segment, percent-decoded when what it decodes to is one safe file name.

    Decoding comes after the segment is taken, so an encoded separator (%2F, %5C) would put a
    directory into what is used as a file name; such a segment, or one that decodes to a name
    Windows cannot hold, is kept as it was served.
    """
    served = Path(urllib.parse.urlparse(url).path).name
    decoded = urllib.parse.unquote(served)
    if (
        decoded in {"", ".", ".."}
        or any(character in decoded for character in '/\\<>:"|?*')
        or any(ord(character) < 32 for character in decoded)
        or decoded != decoded.rstrip(" .")
    ):
        return served
    return decoded


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
    excluded_inputs: list[dict[str, Any]] | None = None,
    conversions: dict[str, dict[str, Any]] | None = None,
    stands_for: dict[str, str] | None = None,
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
    - converted: an mzML the convert stage wrote from an mzXML (``conversions``, by the mzML's _file_key:
      {record, index, source_row}). Its checksums are the mzML's own sha256, and source.conversion names the
      mzXML it was read from - its path, sha256, md5 and sha1 - with the converter and the validation, and
      carries that mzXML's own row (source_row), built here as any input's would be, so whatever vouches for
      the repository's bytes vouches for this input through it. The mzXML is no input, and has no row of
      its own among the rows; it is listed under ``conversion_sources`` instead, with what it became.

    An input this lease neither downloaded nor extracted - a file already in a reused workspace - keeps
    its shape's kind and says so in its source, rather than borrowing another object's checksums.

    A member of an archive is not hashed here; the archive's checksums, and whether the published one was
    compared, are carried as its source. ``sample_id`` is filled when exactly one of the unit's samples
    names the input, or, when none does, names the archive it came out of (_archive_sample_attribution);
    ``file_name`` is the analysis CSV's, and belongs to whatever writes that CSV.

    ``verified_checksums`` is what _verify_project_allowlist_checksums compared, by _file_key: a file or
    an extracted member whose own declared md5, sha1 or sha256 matched records it as declared,
    declared_algorithm and declared_verified. Without it a checked input read as unchecked.

    AN INPUT THAT CAME OUT OF AN ARCHIVE CARRIES ITS BASIS (_archive_basis): the archive whose published
    checksum matched, or the member's own, and the row of the archive's member listing that accounts for
    it (``extracted_members`` and ``archive_extractions``, from the lease's extract stage). A listed
    container archive (X.raw.zip) names its container (X.raw) for declared_names and sample_id.

    A FILE A READER WRITES INTO A CONTAINER (reader_created: Bruker's baf2sql writes analysis.sqlite into
    a BAF .d that arrived without one) is not one of its members. A container a reader rule applies to
    carries reader_created_files, the files a reader has already written there that it did not arrive
    with (none on a first lease; a retry after a preflight finds them), with their size and sha256, and
    reader_named_members, the files of those names it did arrive with, which stay its own. They are what
    reader_created_block tells later readers' files from.

    A FILE THE LEASE EXCLUDED is no input, so it has no row: rows stay one per analysis input, which is
    what the gate resolves inputs against. ``excluded_inputs`` ({path, reason, problems}, from
    _exclude_undecodable_inputs, or an mzXML whose conversion failed or whose scans contradict the
    polarity the unit declares) are described the same way under
    ``excluded``, each with its ``exclusion``, so where the bytes of a file that was not analysed came from
    is still recorded.

    AN INPUT THAT STANDS FOR AN MZXML - one converted from it, or a readable encoding of its sample the lease
    chose over it (``stands_for``, by _file_key; the latter's row carries encoding_choice) - is given the
    sample and the declared names of that mzXML wherever its own name gives none.
    """
    verified_checksums = verified_checksums or {}
    excluded_inputs = excluded_inputs or []
    conversions = conversions or {}
    stands_for = stands_for or {}
    excluded_keys = {_file_key(str(item["path"])): item for item in excluded_inputs}
    inputs = [*inputs, *(str(item["path"]) for item in excluded_inputs)]
    extracted_members = extracted_members or {}
    archive_extractions = archive_extractions or []
    # An input that came out of an archive one sample names (X.zip) is that sample's, although its own
    # name (X.d) is no sample's.
    archive_samples = _archive_sample_attribution(project, archive_extractions, extracted_members, data_root)
    # Each extraction's nested archives by label, built once (_nesting_index), so tracing a file to
    # the archives that held it does not walk the whole lineage once per file.
    nesting: dict[int, dict[str, list[dict[str, Any]]]] = {}
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

    def naming(path: Path, key: str) -> tuple[set[str], set[str]]:
        """(the declared-name forms of a path relative to the data root, the samples its name gives)."""
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
        matched = sample_names.get(base) or sample_names.get(PurePosixPath(base).stem) or set()
        if not matched and key in archive_samples:
            matched = {archive_samples[key]}
        return candidates, matched

    rows = []
    for text in inputs:
        path = Path(text)
        key = _file_key(text)
        candidates, matched_samples = naming(path, key)
        stand = stands_for.get(key, "")
        if stand:
            stand_candidates, stand_samples = naming(Path(stand), _file_key(stand))
            if not {item for item in candidates if item in declared}:
                candidates = stand_candidates
            if not matched_samples:
                matched_samples = stand_samples
        row: dict[str, Any] = {
            "path": str(path),
            "kind": "",
            "declared_names": sorted({declared[item] for item in candidates if item in declared}),
            "sample_id": next(iter(matched_samples)) if len(matched_samples) == 1 else "",
            "file_name": "",
            "source": {},
            "checksums": {},
        }
        if key in conversions:
            conversion = conversions[key]
            record = conversion["record"]
            row["kind"] = "converted"
            row["source"] = {
                "conversion": _conversion_reference(record, conversion.get("source_row"), conversion["index"])
            }
            row["checksums"] = {"sha256": str((record.get("output") or {}).get("sha256") or "")}
        elif key in folder_members:
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
                # The archives its files came out of, innermost first: the per-sample archive inside a
                # project tar or a study archive that the files were expanded from, then each archive
                # that held it, ending at the download.
                vouching: dict[str, list[dict[str, Any]]] = {}
                enclosing: dict[tuple[str, str], dict[str, Any]] = {}
                for member, entry, origin in members:
                    if origin == "archive":
                        listed = extracted_members.get(member)
                        chain = _vouching_chain(listed, archive_extractions, entry, nesting)
                        where = chain[0].get("archive_path") or chain[0].get("download_path") or ""
                        if where not in vouching:
                            vouching[where] = chain
                            for level in _enclosing_listing(listed, archive_extractions, nesting):
                                enclosing.setdefault((level["members_tsv"], level["member"]), level)
                row["basis"] = _archive_basis(
                    list(vouching.values()),
                    listing=[
                        _container_listing(record, path)
                        for record in archive_extractions
                        if str(record.get("download_path") or "") in sources
                    ] + list(enclosing.values()),
                )
            elif downloaded:
                row["source"] = {"objects": len(downloaded)}
            else:
                row["source"] = {"origin": "not_downloaded_by_this_lease"}
            reader_names = reader_created_names(path)
            if reader_names:
                arrived = [Path(member).relative_to(Path(key)).as_posix() for member, _, _ in members]
                row["reader_created_files"] = reader_created_files(path, arrived)
                own = sorted(relative for relative in arrived if relative in reader_names)
                if own:
                    row["reader_named_members"] = own
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
                [_vouching_chain(listed, archive_extractions, extracted_from[key], nesting)],
                member_checksums=row["checksums"],
                listing=(
                    [_member_listing(archive_extractions, listed),
                     *_enclosing_listing(listed, archive_extractions, nesting)]
                    if listed else []
                ),
            )
        else:
            row["kind"] = "vendor_folder" if path.is_dir() else "file"
            row["source"] = {"origin": "not_downloaded_by_this_lease"}
        if stand and key not in conversions:
            # Chosen over an mzXML of the same sample that came out of an archive beside it (_encoding_choices).
            row["encoding_choice"] = {"stands_for": stand, "rule": "encoding_preference.v1"}
        rows.append(row)
    table: dict[str, Any] = {
        "schema": "msdial-input-lineage.v1",
        "rows": [row for row in rows if _file_key(row["path"]) not in excluded_keys],
    }
    if excluded_keys:
        table["excluded"] = [
            {
                **row,
                "exclusion": {
                    "reason": excluded_keys[_file_key(row["path"])]["reason"],
                    "problems": excluded_keys[_file_key(row["path"])].get("problems") or [],
                },
            }
            for row in rows
            if _file_key(row["path"]) in excluded_keys
        ]
    return table


READER_CREATED_SCHEMA = "msdial-reader-created-files.v1"


def reader_created_block(manifest: dict[str, Any], stage: str) -> dict[str, Any] | None:
    """What readers have written into the unit's container inputs by now, for its reader_created_files.

    One entry per container input holding such a file: {path, files}, each file {path, size, sha256,
    reader} (msdial_app.reader_created). A file the container arrived with is told by its lineage row's
    reader_named_members; a container whose row was written before rows carried them (0.5.16 and earlier)
    reports every file a rule names, with members_known false. None when no container holds one, so a unit
    without any records nothing. ``stage`` says when it was taken: preflight or run.
    """
    lineage = manifest.get("input_lineage")
    rows = {
        _file_key(str(row["path"])): row
        for row in ((lineage or {}).get("rows") or [] if isinstance(lineage, dict) else [])
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    }
    containers = []
    for text in manifest.get("input_candidates") or []:
        path = Path(str(text))
        if not path.is_dir() or not reader_created_names(path):
            continue
        row = rows.get(_file_key(str(text))) or {}
        known = "reader_created_files" in row
        files = reader_created_files(path, (row.get("reader_named_members") or []) if known else None)
        if files:
            containers.append({"path": str(path), "files": files, **({} if known else {"members_known": False})})
    if not containers:
        return None
    return {
        "schema": READER_CREATED_SCHEMA,
        "stage": stage,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "containers": containers,
    }


def record_reader_created_files(manifest_path: str | Path, stage: str) -> dict[str, Any] | None:
    """Write reader_created_block into the unit manifest, after a preflight or a run. Never raises.

    Returns the block written, or None when there was nothing to record or the manifest could not be
    written. What is recorded replaces the earlier record: the files are still there, and a run may have
    rewritten one a preflight wrote. The files are hashed before the manifest's lock is taken, not under it.
    """
    try:
        block = reader_created_block(read_manifest(manifest_path), stage)
        if block:
            update_manifest(manifest_path, lambda current: current.__setitem__("reader_created_files", block))
    except (OSError, ValueError, TypeError):
        return None
    return block


def _exclude_undecodable_inputs(inputs: list[str]) -> tuple[list[str], list[dict[str, Any]], int]:
    """(inputs RawDataHandler can decode, the excluded ones, the number of mzML scanned).

    RawDataHandler decodes an mzML array only as a 32- or 64-bit float, zlib-compressed or not, and reads
    anything else - Numpress, integer arrays, a type or compression given only through a param group - as
    uncompressed floats, so the Console ran such a file and wrote garbage or empty spectra without an error.
    Each mzML input is scanned over its first few spectra and chromatograms (mzml_encoding); one with a
    problem is excluded with reason unsupported_mzml_encoding and the accessions found, and the rest of the
    unit goes on without it. A file the scan could not read is kept: the Console, which reads all of it, is
    the judge of that. Other formats are not scanned.
    """
    kept: list[str] = []
    excluded: list[dict[str, Any]] = []
    scanned = 0
    for text in inputs:
        path = Path(text)
        if path.suffix.casefold() != ".mzml" or not path.is_file():
            kept.append(text)
            continue
        scanned += 1
        scan = scan_mzml_encoding(path)
        if not scan["problems"]:
            kept.append(text)
            continue
        excluded.append({
            "path": text,
            "reason": UNSUPPORTED_MZML_ENCODING,
            "problems": scan["problems"],
            "scan": {
                key: scan.get(key)
                for key in ("schema", "spectra_scanned", "chromatograms_scanned", "chromatograms_reached")
            },
        })
    return kept, excluded, scanned


# ---- The convert stage: mzXML written as the mzML MS-DIAL reads ------------------------------------------
#
# ONLY IN A CAMPAIGN. The stage runs where the lease was given a campaign authorization, for a unit with an
# analysis_unit_id, and evaluate_eligibility plans it only for such a unit (EligibilityPolicy.convert_mzxml).
# Outside a campaign an mzXML or mzData still excludes its unit before download: it needs a reviewed
# ProteoWizard-to-mzML conversion with new provenance, and the stage is recorded as not used.
#
# WHERE IT RUNS, AND WHY THERE. After extract, because a packed mzXML (x.mzXML.lzma) and an mzXML inside a
# study archive exist only once their archives are open; before discover, because the input candidates are
# found there, and they are the mzML written here. The mzXML stays where it arrived, under raw\data, as the
# repository's file; its mzML goes to raw\converted, at the same relative path, so it is never mixed with a
# repository's own mzML, the checksum index of raw\data never sees it, and MS-DIAL's per-file intermediates
# land beside it inside the raw tree, released with it.
#
# WHAT IS CONVERTED. The unit's own mzXML only - listed for analysis, declared by the Catalog, or named by
# its samples - and of those only the ones the Catalog's encoding rule keeps: where extraction shows a
# vendor container or an mzML of the same sample beside an mzXML (an mzML archive beside an mzXML archive),
# the readable one is analysed in its place and the mzXML is not converted (encoding_preference). The rule
# is the Catalog's, applied here only to what an archive showed: a listing the Catalog saw it already
# decided. It is given the files of one sample's place, never another folder's file of the same name
# (_sample_locus), and wherever they are the readable files the unit admits by itself, which are its inputs
# in any case (_readable_inputs_admitted).
#
# WHAT IS INFERRED. Every inference the converter offers stays off but one: a scan whose mzXML records no
# polarity is given the unit's declared ion mode, where its Catalog handoff declares exactly one polarity
# (conversion_polarity_declaration, DECLARED_ION_MODE_FIELD). The converter records each imputation as an
# inference with its count; the declaration and its field go into the options of every record and of
# input_conversions, and into the block's polarity_declaration. A unit declaring Both or Unknown, or nothing,
# imputes nothing.
#
# A FILE WHOSE SCANS CONTRADICT THE DECLARATION. The converter refuses to impute where a scan of the file
# records the other polarity (refused_inference POLARITY_IMPUTATION): the declaration cannot stand for the
# scans that record none. The user decided on 2026-10-03 that such a file is excluded with a reason, and the
# rest of the unit runs: it is kept out of the candidates with reason polarity_contradicts_declaration, and
# listed, as a file whose conversion failed is, in excluded_input_candidates and input_lineage's excluded
# rows, and through them in the campaign disposition's excluded_inputs and the analysis-CSV record, where
# the gate's INP-1 accounts for it. It is not a conversion that failed: nothing was written for it, and the
# converter's record of the refusal is kept in the block's polarity_contradictions, with the mzXML and the
# declaration it contradicts, not among its records, which are the conversions CONV-1 holds to their mzML.
# A later lease asks again, and the same bytes are refused again. A file every scan of which records the
# other polarity asks for no imputation and is no such file: it converts with the polarity it records, and
# the raw-header preflight's polarity rule splits the unit by polarity, as it would for a vendor file.
#
# A FILE WHOSE CONVERSION FAILS is no input: it is kept out of the candidates with reason conversion_failed,
# like an mzML RawDataHandler cannot decode, and the rest of the unit runs. A unit left with no input is
# recorded as such, never raised: its project is excluded with NO_CONVERTED_INPUT_REASON, its preflight
# skips it, and the campaign goes on. A full disk is not the file's fault, nor a file another process still
# held after the converter waited for it: the lease stops there, keeping the records written so far, so that
# a retry converts what is left (LEASE_STOPPING_ERRNOS).
#
# RESUMABLE. Each conversion's record is written to provenance\input-conversions.json as it completes, and a
# lease that finds one for the same output passes it to the converter, which reuses the output when the
# mzXML, the options and the converter are what the record says and the mzML still has its recorded sha256.
# The options carry the declaration, so a record made under another one - before the Catalog corrected a
# unit's ion mode, say - is converted again rather than reused.


def _find_mzxml_files(root: Path) -> list[str]:
    """Every mzXML under the data root, outside vendor folders, resolved, in path order."""
    found: list[str] = []
    for directory, folders, files in os.walk(root):
        folders[:] = sorted(name for name in folders if Path(name).suffix.casefold() not in FOLDER_INPUT_SUFFIXES)
        for name in sorted(files):
            if name.casefold().endswith(CONVERTIBLE_SUFFIXES):
                found.append(str((Path(directory) / name).resolve()))
    return sorted(found, key=str.casefold)


def _select_conversion_sources(
    found: list[str],
    data_root: Path,
    project: RepositoryProject,
    archive_samples: dict[str, str],
    archive_extractions: list[dict[str, Any]],
) -> list[str]:
    """The mzXML that are this unit's to analyse, as the attribute stage will admit what is written from them.

    The Catalog's declared inputs, matched by path, where it declared them. Otherwise a file the listing
    names is converted only when the listing gives it for analysis (requires_conversion; an mzXML the
    Catalog demoted to raw_alternate beside a vendor file is not), and a file only an archive held when one
    of the unit's samples names it, or it came out of the archive one sample names. A project with no
    analysis unit has no Catalog decision of which files are its inputs, and converts none: every mzXML of
    an accession would be converted beside the vendor files of the same samples.
    """
    if not project.analysis_unit_id:
        return []
    declared = declared_analysis_inputs(project)
    if declared:
        convertible = {
            key: entry
            for key, entry in declared.items()
            if is_convertible_input(key) or str(entry.get("conversion_target") or "").casefold() == "mzml"
        }
        if not convertible:
            return []
        matched = match_declared_inputs(
            found,
            data_root,
            convertible,
            containers=declared_archive_containers(project, convertible, archive_extractions),
            samples=archive_samples,
        )
        chosen = {item for items in matched.values() for item in items}
        return [item for item in found if item in chosen]
    listed: set[str] = set()
    for item in project.files:
        try:
            name = _safe_relative_name(item.name).as_posix().casefold()
        except ValueError:
            continue
        listed.add(name)
        alias = _container_alias_path(name)
        if alias:
            listed.add(alias)
    sources = set(_project_allowlist(project, analysis_only=True, convertible=True))
    sample_names = _sample_file_names(project)
    selected = []
    for item in found:
        relative = _relative_to_data_root(Path(item), data_root)
        forms = _allowlist_forms(relative) if relative is not None else set()
        if forms & listed:
            if forms & sources:
                selected.append(item)
        elif _matches_sample_file_names(Path(item), sample_names) or _file_key(item) in archive_samples:
            selected.append(item)
    return selected


def _encoding_choices(
    sources: list[str],
    data_root: Path,
    readable: list[str],
    extracted: set[str],
    admitted: set[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Where the unit holds a readable encoding of an mzXML's sample: (the choices, {winner key: mzXML}).

    The Catalog's rule (encoding_preference.prefer_encodings) decides between each mzXML and the readable
    files of its sample's name found on disk - vendor containers, folders and mzML. It is applied only where
    the mzXML or the encoding that wins came out of an archive (``extracted``, by _file_key, members and the
    folders that hold them): what the repository listed file by file, the Catalog already decided. A twin the
    unit admits by itself is the exception (below).

    An mzML whose arrays RawDataHandler cannot decode (mzml_encoding) is no readable twin: the lease would
    exclude it, and a convertible mzXML outranks an unreadable twin, as the user decided on 2026-09-30.

    WHICH FILES ARE ONE SAMPLE'S. The Catalog pairs files by name within one unit's listing. A data root an
    accession archive expanded into holds other units' files too - a study archive with a folder per
    polarity or per column, a QC_01 in each - and pairing by name across all of it would let the positive
    unit's POS/QC_01.mzML displace the negative unit's NEG/QC_01.mzXML. So the rule is given the files
    whose folders agree once the words that name an encoding are set aside (_sample_locus): mzML/x.mzML
    and mzXML/x.mzXML are one sample's, POS/x.mzML and NEG/x.mzXML are not. It is also given, whatever
    their folders, the readable files the unit admits by itself (``admitted``, by _file_key;
    _readable_inputs_admitted): a Thermo_RAW/x.raw that a sample named x admits is that sample's input
    whatever else is converted, so converting mzXML/x.mzXML as well would give the sample two inputs and
    the unit no analysis CSV. Such a twin is paired whether or not an archive held it, for the same reason.
    """
    admitted = admitted or set()
    by_stem: dict[str, list[str]] = {}
    for item in readable:
        by_stem.setdefault(encoding_preference.stem(Path(item).name), []).append(item)
    choices: list[dict[str, Any]] = []
    winners: dict[str, str] = {}
    for source in sources:
        locus = _sample_locus(source, data_root)
        twins = [
            item
            for item in by_stem.get(encoding_preference.stem(Path(source).name)) or []
            if (_file_key(item) in admitted or _sample_locus(item, data_root) == locus)
            and not (
                Path(item).suffix.casefold() == ".mzml" and Path(item).is_file()
                and scan_mzml_encoding(Path(item))["problems"]
            )
        ]
        if not twins:
            continue
        names = {item: (_relative_to_data_root(Path(item), data_root) or Path(item).name) for item in [source, *twins]}
        roles = encoding_preference.prefer_encodings(list(names.values()))
        if roles.get(names[source]) != encoding_preference.ALTERNATE:
            continue
        chosen = [item for item in twins if roles.get(names[item]) == encoding_preference.RAW]
        by_unit = any(_file_key(item) in admitted for item in chosen)
        if not chosen or not (
            by_unit or _file_key(source) in extracted or any(_file_key(item) in extracted for item in chosen)
        ):
            continue
        for item in chosen:
            winners.setdefault(_file_key(item), source)
        choices.append({
            "mzxml": source,
            "analysed_instead": chosen,
            "rule": "encoding_preference.v1",
            "paired_by": "admitted_by_unit" if by_unit else "same_folder",
            "reason": (
                "The unit admits a readable encoding of the same sample by itself - its listing, its sample "
                "names, or the archive a sample names - and the Catalog's encoding rule analyses that one; the "
                "mzXML is not converted."
                if by_unit
                else "A readable encoding of the same sample came out of an archive beside it, and the Catalog's "
                "encoding rule analyses that one; the mzXML is not converted."
            ),
        })
    return choices, winners


# The words of a folder's name that say which encoding it holds rather than whose samples: ST003038's
# mzML/ beside its mzXML/, an archive's raw/ beside its mzXML/, NEG_mzML/ beside NEG_mzXML/. A one-letter
# word (the .d of Agilent and Bruker) names too much else to be set aside.
_ENCODING_FOLDER_WORDS = frozenset(
    suffix[1:]
    for suffix in (
        *encoding_preference.VENDOR_SUFFIXES, *encoding_preference.OPEN_SUFFIXES, ".mzxml", ".mzdata",
    )
    if len(suffix) > 2
)


def _sample_locus(path: str, data_root: Path) -> tuple[tuple[str, ...], ...]:
    """The folders a file lies in below the data root, each as its words less those naming an encoding.

    Two encodings of a name are one sample's only where these agree (_encoding_choices). A folder whose
    every word names an encoding (mzML, RAW) is no place of its own, so x.mzML at the root and mzXML/x.mzXML
    agree, and NEG_mzML and NEG_mzXML are both NEG.
    """
    relative = _relative_to_data_root(Path(path), data_root)
    folders = PurePosixPath(relative).parts[:-1] if relative else ()
    locus = []
    for folder in folders:
        words = tuple(
            word for word in re.split(r"[\W_]+", folder.casefold()) if word and word not in _ENCODING_FOLDER_WORDS
        )
        if words:
            locus.append(words)
    return tuple(locus)


def _extracted_keys(extracted_members: dict[str, dict[str, Any]], data_root: Path) -> set[str]:
    """Every extracted file, and every folder under the data root holding one, by _file_key."""
    keys = set(extracted_members)
    stop = _file_key(str(data_root))
    for key in list(extracted_members):
        for parent in Path(key).parents:
            parent_key = str(parent)
            if parent_key == stop or len(parent_key) <= len(stop) or parent_key in keys:
                break
            keys.add(parent_key)
    return keys


def _previous_conversion_records(provenance: Path) -> dict[str, dict[str, Any]]:
    """An earlier lease's completed conversion records of this unit, by their output's _file_key."""
    path = provenance / INPUT_CONVERSIONS_NAME
    try:
        block = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}
    records = block.get("records") if isinstance(block, dict) and block.get("schema") == INPUT_CONVERSIONS_SCHEMA else None
    previous: dict[str, dict[str, Any]] = {}
    for record in records if isinstance(records, list) else []:
        output = record.get("output") if isinstance(record, dict) else None
        if isinstance(output, dict) and record.get("status") == "converted" and str(output.get("path") or ""):
            previous[_file_key(str(output["path"]))] = record
    return previous


def _converted_destination(source: str, data_root: Path, raw_root: Path) -> tuple[Path, str]:
    """(where an mzXML's mzML is written, the mzXML's path relative to the data root)."""
    try:
        relative = Path(source).resolve().relative_to(data_root.resolve())
    except ValueError:
        relative = Path(Path(source).name)
    return raw_root / CONVERTED_DIRECTORY / relative.with_suffix(".mzML"), relative.as_posix()


def _run_lease_conversions(
    sources: list[str],
    data_root: Path,
    raw_root: Path,
    provenance: Path,
    choices: list[dict[str, Any]],
    *,
    declaration: dict[str, Any],
    between: Any = None,
) -> dict[str, Any]:
    """Convert each mzXML, recording as it goes; raises only for a full disk or an unwritable record.

    ``declaration`` is the unit's (conversion_polarity_declaration): every conversion runs under the options
    it gives (campaign_conversion_options), and the block records it as polarity_declaration. A file that
    refuses the imputation they ask for is not converted (A FILE WHOSE SCANS CONTRADICT THE DECLARATION,
    above): the converter's record of the refusal goes into the block's polarity_contradictions, not its
    records.

    Returns the input_conversions block (INPUT_CONVERSIONS_SCHEMA, with the converter's records), the mzML
    written by _file_key with the mzXML each was written from, and the exclusion entry of each mzXML whose
    conversion failed (conversion_failed) or that contradicts the declaration
    (polarity_contradicts_declaration), in source order. ``between(name)`` is called before each file, where
    the lease beats its heartbeat and hears a cancel.

    A conversion the disk filled under (FULL_DISK_ERRNOS), or one stopped by a file another process held
    after the converter waited for it (HELD_FILE_ERRNOS), says nothing about its mzXML, so it excludes no
    sample: the block is written as stopped, with every record so far, and OSError is raised, which fails
    the lease at its convert stage. A retry reuses what was converted and converts the rest.
    """
    previous = _previous_conversion_records(provenance)
    options = campaign_conversion_options(declaration)
    block: dict[str, Any] = {
        "schema": INPUT_CONVERSIONS_SCHEMA,
        "status": "running",
        "converter": converter_identity(),
        "options": asdict(options),
        "polarity_declaration": dict(declaration),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "records": [],
        "encoding_choices": choices,
        "polarity_contradictions": [],
    }
    record_path = provenance / INPUT_CONVERSIONS_NAME
    outputs: dict[str, str] = {}
    excluded: list[dict[str, Any]] = []
    for source in sources:
        if between is not None:
            between(Path(source).name)
        destination, relative = _converted_destination(source, data_root, raw_root)
        record = convert_mzxml_to_mzml(
            source,
            destination,
            options,
            source_relative_path=relative,
            previous=previous.get(_file_key(str(destination))),
        )
        if record.get("refused_inference") == POLARITY_IMPUTATION:
            # A scan of the file records the other polarity, so the declaration cannot stand for those that
            # record none: the file is excluded, and the rest of the unit runs (the user, 2026-10-03).
            block["polarity_contradictions"].append({
                "mzxml": source,
                "reason": POLARITY_CONTRADICTS_DECLARATION,
                "declared_polarity": options.impute_polarity,
                "record": record,
            })
            excluded.append({
                "path": source,
                "reason": POLARITY_CONTRADICTS_DECLARATION,
                "problems": [str(record.get("error") or "the polarity imputation was refused")],
                "conversion": {
                    "output": str(destination),
                    "polarity_contradiction": len(block["polarity_contradictions"]) - 1,
                },
            })
            _write_json(record_path, block)
            continue
        block["records"].append(record)
        if record.get("status") == "converted":
            outputs[_file_key(str(destination))] = source
        elif record.get("error_errno") in LEASE_STOPPING_ERRNOS:
            block["status"] = "stopped"
            block["stopped_at"] = datetime.now(timezone.utc).isoformat()
            _write_json(record_path, block)
            what = (
                f"The disk filled while {Path(source).name} was converted to mzML"
                if record["error_errno"] in FULL_DISK_ERRNOS
                else f"Another process held a file of {Path(source).name}'s conversion to mzML"
            )
            raise OSError(
                record["error_errno"],
                f"{what} ({record.get('error')}). The lease stops here rather than exclude the file, so that a "
                "retry converts it and the rest.",
            )
        else:
            excluded.append({
                "path": source,
                "reason": CONVERSION_FAILED,
                "problems": [str(record.get("error") or "the conversion did not complete")],
                "conversion": {"output": str(destination), "record": len(block["records"]) - 1},
            })
        # As each completes, so a lease that stops keeps what was done.
        _write_json(record_path, block)
    records = block["records"]
    block["counts"] = {
        "sources": len(sources),
        "converted": sum(1 for item in records if item.get("status") == "converted"),
        "reused": sum(1 for item in records if item.get("reused_previous_record")),
        "failed": sum(1 for item in excluded if item["reason"] == CONVERSION_FAILED),
        # The files excluded because their scans contradict the declared polarity, none of them converted.
        POLARITY_CONTRADICTS_DECLARATION: len(block["polarity_contradictions"]),
        "not_converted_readable_encoding": len(choices),
        # The spectra given the declared polarity, which each record's polarity_imputation inference counts.
        "imputed_polarity_spectra": sum(
            int(item.get("spectra") or 0)
            for record in records
            if record.get("status") == "converted"
            for item in record.get("inferences") or []
            if isinstance(item, dict) and item.get("kind") == POLARITY_IMPUTATION
        ),
    }
    block["status"] = "completed"
    block["completed_at"] = datetime.now(timezone.utc).isoformat()
    block["record_path"] = str(record_path)
    _write_json(record_path, block)
    return {"block": block, "outputs": outputs, "excluded": excluded}


def lineage_stands_for(manifest: dict[str, Any]) -> dict[str, str]:
    """{_file_key of an input: the mzXML it stands for}, read from the manifest's input_lineage rows.

    A converted row stands for the mzXML its conversion read (source.conversion.source_path); a readable
    encoding the lease chose over an mzXML of the same sample (encoding_choice.stands_for) for that mzXML.
    The allow-list, the split plan and the analysis-CSV builder attribute such an input through it.
    """
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    result: dict[str, str] = {}
    for row in lineage.get("rows") or []:
        if not isinstance(row, dict) or not str(row.get("path") or "").strip():
            continue
        source = row.get("source") if isinstance(row.get("source"), dict) else {}
        conversion = source.get("conversion") if isinstance(source.get("conversion"), dict) else {}
        choice = row.get("encoding_choice") if isinstance(row.get("encoding_choice"), dict) else {}
        stand = str(conversion.get("source_path") or choice.get("stands_for") or "").strip()
        if stand:
            result[_file_key(str(row["path"]))] = stand
    return result


def _conversion_reference(record: dict[str, Any], source_row: dict[str, Any] | None, index: int) -> dict[str, Any]:
    """What a converted input's lineage row says about the conversion that wrote it.

    The fields the gate's SUM-1 follows to the mzXML (source_path, source_sha256, source_row) and those its
    CONV-1 holds the mzML to (output_sha256, the converter, the validation), read from the record itself.
    """
    source = record.get("source") or {}
    output = record.get("output") or {}
    converter = record.get("converter") or {}
    validation = record.get("validation") or {}
    reference: dict[str, Any] = {
        "source_path": str(source.get("path") or ""),
        "source_relative_path": str(source.get("relative_path") or ""),
        "source_sha256": str(source.get("sha256") or ""),
        "source_md5": str(source.get("md5") or ""),
        "source_sha1": str(source.get("sha1") or ""),
        "source_bytes": source.get("bytes"),
        "output_sha256": str(output.get("sha256") or ""),
        "output_bytes": output.get("bytes"),
        "converter": {key: converter.get(key) for key in ("name", "version", "module_sha256")},
        "validation": {key: validation.get(key) for key in ("schema", "status", "spectra_compared", "problem_count")},
        "record": index,
    }
    if source_row is not None:
        reference["source_row"] = source_row
    return reference


def _conversion_lineage(
    conversion: dict[str, Any] | None,
    kept: set[str],
    downloads: list[dict[str, Any]],
    extracted_from: dict[str, dict[str, Any]],
    data_root: Path,
    download_root: Path,
    project: RepositoryProject,
    verified_checksums: dict[str, dict[str, Any]],
    *,
    extracted_members: dict[str, dict[str, Any]],
    archive_extractions: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """(build_input_lineage's conversions, by the mzML's _file_key; the rows of the mzXML they were read from).

    Each mzXML's row is built as an input's row would be - a file downloaded as itself, or a member of the
    archive it came out of, with its declared checksum where one was verified - so that a converted input
    carries it (source.conversion.source_row) and is vouched for by what vouches for the repository's file.
    Only conversions whose mzML is an input (``kept``) are given to the rows; every completed one is listed,
    saying what it became.
    """
    if conversion is None:
        return {}, []
    converted = [
        (index, record) for index, record in enumerate(conversion["block"]["records"])
        if record.get("status") == "converted"
    ]
    if not converted:
        return {}, []
    table = build_input_lineage(
        [str(record["source"]["path"]) for _index, record in converted],
        downloads,
        extracted_from,
        data_root,
        download_root,
        project,
        verified_checksums,
        extracted_members=extracted_members,
        archive_extractions=archive_extractions,
    )
    source_rows = {_file_key(str(row["path"])): row for row in table["rows"]}
    conversions: dict[str, dict[str, Any]] = {}
    listed: list[dict[str, Any]] = []
    for index, record in converted:
        output_key = _file_key(str(record["output"]["path"]))
        row = source_rows.get(_file_key(str(record["source"]["path"])))
        if row is None:
            continue
        listed.append({**row, "converted_to": str(record["output"]["path"]), "is_input": output_key in kept})
        if output_key in kept:
            conversions[output_key] = {"record": record, "index": index, "source_row": row}
    return conversions, listed


def _record_conversion_outcome(project: RepositoryProject, conversion: dict[str, Any], inputs: list[str]) -> None:
    """Write what the convert stage did into the project's conversion plan, and exclude a unit it left empty.

    evaluate_eligibility reads the outcome back (_no_converted_input_reason), so a unit every later step
    judges again - a preflight, a campaign disposition, a split - stays what the lease found it to be:
    eligible where an input survived the conversion, excluded where none did.
    """
    block = conversion["block"]
    counts = block["counts"]
    outcome = {
        "converted": counts["converted"],
        "reused": counts["reused"],
        "failed": counts["failed"],
        "not_converted_readable_encoding": counts["not_converted_readable_encoding"],
        "analysis_inputs": len(inputs),
    }
    if counts[POLARITY_CONTRADICTS_DECLARATION]:
        # Only where one was, so the outcome of a unit with none records what it always did.
        outcome[POLARITY_CONTRADICTS_DECLARATION] = counts[POLARITY_CONTRADICTS_DECLARATION]
    plan = dict(project.conversion_plan or {}) or _conversion_plan(
        sorted(
            {
                *(Path(str(record["source"]["path"])).name for record in block["records"]),
                *(Path(str(item["mzxml"])).name for item in block["polarity_contradictions"]),
            },
            key=str.casefold,
        )
    )
    # The options the stage converted with, which a plan made before the declaration was read may not give.
    plan["options"] = dict(block["options"])
    plan["outcome"] = outcome
    project.conversion_plan = plan
    reason = _no_converted_input_reason(outcome)
    if reason:
        project.exclusion_reasons = list(dict.fromkeys([*project.exclusion_reasons, reason]))
        project.review_reasons = []
        project.eligible = False
        project.selection_status = "excluded"


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


def _nesting_index(record: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Each archive expanded inside an extraction, by label, with the nested records that lead to it.

    The label is the archive's path in the members listing (its archive_path), which is also what the
    listing's archive column names for the files that came out of it. The path runs from the archive
    the download held down to the one labelled.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    pending = [(nested, []) for nested in record.get("nested") or [] if isinstance(nested, dict)]
    while pending:
        nested, above = pending.pop()
        path = [*above, nested]
        index.setdefault(str(nested.get("archive_path") or ""), path)
        pending.extend((child, path) for child in nested.get("nested") or [] if isinstance(child, dict))
    return index


def _nesting_path(
    listed: dict[str, Any] | None,
    records: list[dict[str, Any]],
    nesting: dict[int, dict[str, list[dict[str, Any]]]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """(extraction record, nested records from the outermost to the one a file came out of)."""
    if not listed:
        return {}, []
    index = int(listed.get("extraction", -1))
    record = records[index] if 0 <= index < len(records) else {}
    if index not in nesting:
        nesting[index] = _nesting_index(record)
    return record, nesting[index].get(str(listed.get("archive") or ""), [])


def _vouching_chain(
    listed: dict[str, Any] | None,
    records: list[dict[str, Any]],
    download: dict[str, Any],
    nesting: dict[int, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """The archives a file came out of, innermost first: nested ones the listing names, then the download.

    A per-sample zip inside a Workbench study archive has no published checksum of its own; the study
    archive that held it has one. Only the innermost archive used to be named, so such a file read as
    "extracted from an archive for which no checksum was published", in the methods text too, although
    the archive it came out of, through the zip, had matched its published MD5. The whole chain is
    named, and _archive_basis takes the nearest archive in it that was verified.
    """
    record, path = _nesting_path(listed, records, nesting)
    return [_expanded_archive(record, nested) for nested in reversed(path)] + [_download_archive(download)]


def _enclosing_listing(
    listed: dict[str, Any] | None,
    records: list[dict[str, Any]],
    nesting: dict[int, dict[str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """The listing rows of the archives that held the one a file came out of, innermost first.

    Each nested archive is itself a row of the members listing (disposition expanded_archive), listed
    as a member of the archive that held it. With the file's own row, these trace it level by level to
    the download.
    """
    record, path = _nesting_path(listed, records, nesting)
    rows = []
    for depth in range(len(path) - 1, -1, -1):
        holder = path[depth - 1].get("archive_path") if depth else record.get("archive_name")
        rows.append({
            **_listing_reference(record),
            "member": str(path[depth].get("archive_path") or ""),
            "listed_archive": str(holder or ""),
        })
    return rows


def _archive_basis(
    chains: list[list[dict[str, Any]]],
    *,
    member_checksums: dict[str, Any] | None = None,
    listing: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """What vouches for an input that came out of an archive, strongest first.

    - member_declared_checksum: the file's own published checksum matched its extracted bytes (MB-POST
      publishes one per file inside its project tar);
    - archive_declared_checksum: the published checksum of an archive it came out of matched that
      archive, as downloaded or, for an archive inside another, before it expanded; and the input is in
      the listing of what came out of it. The archive is the nearest verified one in the chain: the
      innermost when it published a checksum, else the one that held it, out to the download. value is
      that verified checksum;
    - archive_download_hash: no archive in the chain published one; the SHA-256 of the innermost,
      computed at download (or, for an archive inside another, before it expanded), identifies it, and
      nothing more.

    chains are the archives each input came out of (_vouching_chain), innermost first: one chain for a
    file, one or more for a container, whose basis is a verified one only when every chain has a
    verified archive. archives is every archive of every chain, innermost first. listing names the
    members-TSV rows (or the container) that account for the input, at every level, so a reader can
    trace the input to the listing and the listing to the archive without re-reading the raw tree.
    """
    named: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for chain in chains:
        for archive in chain:
            key = (str(archive.get("inside") or ""), str(archive.get("archive_path") or ""),
                   str(archive.get("download_path") or ""))
            if key not in seen:
                seen.add(key)
                named.append(archive)
    vouchers = [next((item for item in chain if item["declared_verified"]), None) for chain in chains]
    basis: dict[str, Any] = {"archives": named, "listing": list(listing or [])}
    if member_checksums and member_checksums.get("declared_verified"):
        algorithm = str(member_checksums.get("declared_algorithm") or "")
        basis.update(kind="member_declared_checksum", algorithm=algorithm,
                     value=str(member_checksums.get("declared") or ""), verified=True)
    elif vouchers and all(vouchers):
        voucher = vouchers[0]
        algorithm = voucher["declared_algorithm"] or "md5"
        value = voucher.get(algorithm) or voucher["declared"]
        basis.update(kind="archive_declared_checksum", algorithm=algorithm, value=value, verified=True)
    else:
        innermost = chains[0][0] if chains and chains[0] else {}
        basis.update(kind="archive_download_hash", algorithm="sha256",
                     value=str(innermost.get("sha256") or ""), verified=None)
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
    *,
    status: str = "run_failed",
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

    A Console that exits 0 and leaves the unit without a validated mzTab-M has failed too, and is
    recorded here like one that exits non-zero, so the run failures count every run that did not
    validate. ``status`` is validation_failed for such a run that finalize_download_lease has
    already finalised as validation_failed, which it keeps; anything else is run_failed.

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
        manifest["status"] = status
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
        return {"status": status, "manifest_error": str(error), "run_failure": record}


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
            # Every file beside the binary, by one digest (workflow.console_inventory).
            "inventory_sha256": str(identity.get("inventory_sha256") or ""),
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
    return _live_run_attempt_in(manifest, ignore_backend_pid)


def _live_run_attempt_in(manifest: dict[str, Any], ignore_backend_pid: int | None = None) -> dict[str, Any] | None:
    """live_run_attempt for a manifest already read, such as one held under its writer lock."""
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


PREFLIGHT_OUTPUT_NAME = "raw-metadata-preflight.json"
# Where the chunk outputs are written while a preflight runs; emptied and removed when it ends.
PREFLIGHT_CHUNK_DIRECTORY = "raw-metadata-preflight-chunks"
SKIPPED_BY_PREFLIGHT_STATUS = "skipped_by_preflight"
EXCLUDED_BY_PREFLIGHT_STATUS = "excluded_by_preflight"
_EXTRACTOR_IDENTITIES: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {}
_EXTRACTOR_IDENTITIES_GUARD = threading.Lock()


def raw_metadata_extractor_identity(extractor_path: Path) -> dict[str, Any]:
    """inspect_raw_metadata_extractor, re-hashed only when a file of the extractor's folder has changed.

    A campaign preflights thousands of units with one build, and inspecting it hashes every file of its
    folder. The folder's listing - every file's path, size and modification time - is what decides whether
    the hashes are still the ones computed.
    """
    from .raw_metadata_extractor import inspect_raw_metadata_extractor

    binary = Path(extractor_path).resolve()
    folder = binary.parent
    try:
        listing = tuple(
            sorted(
                (str(item.relative_to(folder)), stat.st_size, stat.st_mtime_ns)
                for item in folder.rglob("*")
                if item.is_file()
                for stat in (item.stat(),)
            )
        )
    except OSError:
        listing = ()
    key = os.path.normcase(str(binary))
    with _EXTRACTOR_IDENTITIES_GUARD:
        cached = _EXTRACTOR_IDENTITIES.get(key)
    if cached and listing and cached[0] == listing:
        return copy.deepcopy(cached[1])
    inspection = inspect_raw_metadata_extractor(binary)
    with _EXTRACTOR_IDENTITIES_GUARD:
        _EXTRACTOR_IDENTITIES[key] = (listing, copy.deepcopy(inspection))
    return inspection


def _extractor_record(extractor_path: Path, identity: dict[str, Any], source: str) -> dict[str, Any]:
    stat = extractor_path.stat()
    return {
        "path": str(extractor_path),
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat(),
        # Which build produced the verdicts, by checksum and by the commits its record names, so a verdict
        # is tied to code rather than to whichever executable came first on the search order.
        "sha256": str(identity.get("binary_sha256") or ""),
        "inventory_sha256": str(identity.get("inventory_sha256") or ""),
        "file_count": identity.get("file_count"),
        "provenance_status": str(identity.get("provenance_status") or ""),
        "provenance_path": str(identity.get("provenance_path") or ""),
        "msrawdataworkbench_commit": str(identity.get("msrawdataworkbench_commit") or ""),
        "msdialworkbench_commit": str(identity.get("msdialworkbench_commit") or ""),
        "product_version": str(identity.get("product_version") or ""),
        "pinned": bool(identity.get("pinned")),
        "pin_state": str(identity.get("pin_state") or ""),
        "selected_from": str(source or "argument"),
    }


def _campaign_crossings(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The campaign approvals recorded for this unit, or for the unit it was split from."""
    own = [item for item in manifest.get("campaign_authorizations") or [] if isinstance(item, dict)]
    if own:
        return own
    parent = str((manifest.get("split_from") or {}).get("manifest_path") or "").strip()
    if not parent or not Path(parent).is_file():
        return []
    try:
        record = read_manifest(parent)
    except (OSError, ValueError):
        return []
    return [item for item in record.get("campaign_authorizations") or [] if isinstance(item, dict)]


def _converts_mzxml(manifest: dict[str, Any]) -> bool:
    """Whether a unit's mzXML is converted when it is judged again: only a campaign unit's is.

    A campaign approval recorded for the unit, or for the unit it was split from, made its download a
    campaign's, and only a campaign's lease converts (EligibilityPolicy.convert_mzxml).
    """
    return bool(_campaign_crossings(manifest))


def preflight_campaign(
    manifest: dict[str, Any], campaign_authorization_path: str | Path | None = None
) -> dict[str, Any] | None:
    """The campaign a unit's preflight runs under, or None outside one.

    An approval passed in must name the unit - a preflight crosses no confirmation boundary itself, but it
    decides whether the unit reaches the runs the approval covers (boundary 4) - and one that does not is
    refused, as at every other entry point. Without one, an approval already recorded for the unit or its
    split parent means the unit is a campaign unit: the download that made it was approved as one.
    """
    from .campaign_authorization import CampaignAuthorizationError, load_campaign_authorization, unit_identity

    authorization = load_campaign_authorization(campaign_authorization_path)
    if authorization is not None:
        unit, parent = unit_identity(manifest)
        verdict = authorization.check(unit, 4, parent_unit_id=parent)
        if not verdict["valid"]:
            raise CampaignAuthorizationError(verdict["codes"], verdict["reasons"])
        return {
            "approval_id": authorization.approval_id,
            "manifest_digest": authorization.manifest_digest,
            "basis": "authorization_passed",
        }
    crossings = _campaign_crossings(manifest)
    if crossings:
        return {
            "approval_id": str(crossings[0].get("approval_id") or ""),
            "manifest_digest": str(crossings[0].get("manifest_digest") or ""),
            "basis": "authorization_recorded",
        }
    return None


def disposition_hold(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Why no disposition may change this unit's state now, or None when one may. Changes nothing.

    split_parent: the unit was split. A parent owns its parts' raw data and never runs, so a disposition
    that made it runnable, or skipped it - and a campaign deletes a skipped unit's raw data - would reach
    the parts. excluded_at_split: a part of ion-mobility inputs, excluded when its parent was split
    (split_exclusion). past_preflight: its run finished (mztab_validated, cleanup_pending_confirmation,
    raw_cleaned and the like) or its raw data were discarded; applied again, a disposition would move a
    finished unit out of the cleanup-ready states and make it look as if it were waiting to run.
    run_in_progress: a Console of its own may still be running.

    One step goes past a past_preflight hold: redecide_legacy_disposition, as a finished unit is prepared again,
    decides a pre-0.5.29 disposition again and keeps the unit's status.
    """
    status = str(manifest.get("status") or "")
    if status == SPLIT_PARENT_STATUS or manifest.get("split_into"):
        return {
            "reason": "split_parent",
            "status": status,
            "detail": "The unit was split; its parts are preflighted and run, and it never is.",
        }
    if manifest.get("split_exclusion"):
        # A part of ion-mobility inputs ended at its split: a disposition decided from its own headers would
        # say the same, and one that made it runnable would run LC-IM-MS data as LC-MS.
        return {
            "reason": "excluded_at_split",
            "status": status,
            "detail": "The part was excluded when its parent was split: "
            + str((manifest.get("split_exclusion") or {}).get("reason") or "excluded")
            + ".",
        }
    if status in PAST_PREFLIGHT_STATUSES:
        return {
            "reason": "past_preflight",
            "status": status,
            "detail": f"The unit is past its preflight ({status}); what recorded that describes it now.",
        }
    attempt = _live_run_attempt_in(manifest)
    if attempt is not None:
        return {
            "reason": "run_in_progress",
            "status": status,
            "job_id": str(attempt.get("job_id") or ""),
            "detail": "A run attempt of this unit is open and its process may still be running.",
        }
    return None


def _declared_technical(manifest: dict[str, Any]) -> dict[str, Any]:
    """What the repository record declared, before any preflight wrote header values into the project.

    A preflight replaces the project's acquisition mode, polarity and separation with what the headers
    said, so a second preflight reading the project would take the first one's verdict for the
    declaration. The copy of the project written when the unit was made (repository-metadata.json; for a
    split part, the part as the split wrote it) comes first, then an earlier preflight's record.
    """
    from .raw_metadata_preflight import declared_technical

    path = str(manifest.get("repository_metadata_file") or "").strip()
    if path and Path(path).is_file():
        try:
            return declared_technical(read_manifest(path))
        except (OSError, ValueError):
            pass
    earlier = (manifest.get("raw_metadata_preflight") or {}).get("declared")
    if isinstance(earlier, dict):
        return dict(earlier)
    return declared_technical(manifest.get("project"))


def _split_parent_declared(manifest: dict[str, Any], depth: int = 0) -> str | None:
    """The acquisition mode a split part's parent declared; None for a unit that is no split part.

    A split from 0.5.29 records it (split_from.parent_declared_acquisition_mode). For a part split before that,
    it is read from the parent manifest as _declared_technical gives it, and through the parent's own parent
    where the parent is itself a part; "" where the parent cannot be read.
    """
    from .raw_metadata_preflight import SPLIT_PARENT_DECLARED_FIELD

    split = manifest.get("split_from")
    if not isinstance(split, dict):
        return None
    recorded = str(split.get(SPLIT_PARENT_DECLARED_FIELD) or "").strip()
    if recorded:
        return recorded
    parent_path = str(split.get("manifest_path") or "").strip()
    if not parent_path or depth > 8:
        return ""
    try:
        parent = read_manifest(parent_path) if Path(parent_path).is_file() else None
    except (OSError, ValueError):
        parent = None
    if not isinstance(parent, dict):
        return ""
    grandparent = _split_parent_declared(parent, depth + 1)
    if grandparent is not None:
        return grandparent
    mode = str(_declared_technical(parent).get("acquisition_mode") or "").strip()
    return "" if mode.casefold() == "unknown" else mode


def _previous_reads(manifest: dict[str, Any], manifest_path: Path) -> dict[str, dict[str, Any]]:
    """Earlier reads that may stand for this one: the unit's own last preflight, then its split parent's.

    run_extractor uses one only for the same extractor sha256 and the same size and modification time.
    A split part reads its parent's files, so its own preflight need not read a Waters folder again.
    """
    from .raw_metadata_preflight import READ_OUTCOMES, file_key

    sources: list[tuple[Path, dict[str, Any]]] = [(manifest_path, manifest)]
    parent = str((manifest.get("split_from") or {}).get("manifest_path") or "").strip()
    if parent and Path(parent).is_file():
        try:
            sources.append((Path(parent), read_manifest(parent)))
        except (OSError, ValueError):
            pass
    reads: dict[str, dict[str, Any]] = {}
    for source, record in sources:
        preflight = record.get("raw_metadata_preflight") or {}
        entries = [
            item for item in (preflight.get("summary") or {}).get("per_file") or []
            if isinstance(item, dict)
            and item.get("outcome") in READ_OUTCOMES
            and item.get("extractor_sha256")
            and item.get("input_signature")
        ]
        output = Path(str(preflight.get("output") or ""))
        if not entries or not output.is_file():
            continue
        try:
            raw = json.loads(output.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        raw_records = {}
        for item in [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []:
            source_record = (item.get("source") or {}) if isinstance(item, dict) else {}
            path_text = str(source_record.get("filePath") or "") if isinstance(source_record, dict) else ""
            if path_text:
                raw_records.setdefault(file_key(path_text), item)
        for entry in entries:
            key = file_key(str(entry.get("file") or ""))
            if key in raw_records and key not in reads:
                reads[key] = {"record": raw_records[key], "entry": entry, "source": str(source)}
    return reads


def _reader_created_before(manifest: dict[str, Any]) -> dict[str, list[str]]:
    """What earlier reads of these inputs - the unit's own, then its split parent's - wrote into them.

    Taken from every earlier per-file record, whatever its outcome: a reader that wrote its cache into a
    Bruker .d and then failed has still changed the folder.
    """
    from .raw_metadata_preflight import file_key

    records = [manifest]
    parent = str((manifest.get("split_from") or {}).get("manifest_path") or "").strip()
    if parent and Path(parent).is_file():
        try:
            records.append(read_manifest(parent))
        except (OSError, ValueError):
            pass
    created: dict[str, list[str]] = {}
    for record in records:
        for entry in ((record.get("raw_metadata_preflight") or {}).get("summary") or {}).get("per_file") or []:
            if not isinstance(entry, dict) or not str(entry.get("file") or "").strip():
                continue
            files = [str(item) for item in entry.get("reader_created_files") or [] if str(item).strip()]
            if files:
                key = file_key(str(entry["file"]))
                created[key] = list(dict.fromkeys([*created.get(key, []), *files]))
    return created


def _conversion_left_no_input(manifest: dict[str, Any]) -> bool:
    """Whether the lease converted this unit's mzXML and no input survived (the project records why)."""
    plan = (manifest.get("project") or {}).get("conversion_plan")
    return bool(_no_converted_input_reason((plan or {}).get("outcome") if isinstance(plan, dict) else None))


def run_raw_metadata_preflight(
    manifest_path: Path,
    extractor_path: Path,
    max_inputs: int | None = None,
    confirm_untargeted: bool = False,
    *,
    campaign_authorization_path: str | Path | None = None,
    require_pinned_extractor: bool = False,
    extractor_source: str = "",
) -> dict[str, Any]:
    """Read every input's raw header, record what was read, and decide the unit's campaign disposition.

    The extractor runs in bounded chunks (raw_metadata_preflight.run_extractor), outside the manifest's
    lock, for as long as the reads take. What it found is then written through update_manifest: the change
    is applied to the manifest as it is on disk when the reads end, so nothing another writer recorded in
    the meantime - an approval, a lease heartbeat, a split - is overwritten by the copy read at the start.

    Under a campaign (an approval passed, or one recorded for the unit) the extractor must inspect as
    verified and pinned, and campaign_disposition is applied to the unit: it decides execution_allowed and
    the status. Outside one the disposition is recorded as advice and nothing else differs from before.

    A campaign unit that disposition_hold holds - split, finished, or with a run of its own open - is not
    read at all: the manifest is returned as it is, with preflight_held saying why. Its recorded
    disposition stands, since a campaign acts on whatever disposition the unit carries.
    """
    from .raw_metadata_extractor import RawMetadataExtractorRefused, campaign_refusal
    from .raw_metadata_preflight import run_extractor

    manifest_path = manifest_path.resolve()
    extractor_path = extractor_path.resolve()
    if not extractor_path.is_file():
        raise FileNotFoundError(f"Raw metadata extractor was not found: {extractor_path}")
    snapshot = read_manifest(manifest_path)
    campaign = preflight_campaign(snapshot, campaign_authorization_path)
    if campaign is not None:
        held = disposition_hold(snapshot)
        if held is not None:
            return {**snapshot, "manifest_path": str(manifest_path), "preflight_held": held}
    identity = raw_metadata_extractor_identity(extractor_path)
    if campaign is not None or require_pinned_extractor:
        # A campaign decides from these verdicts whether thousands of units run, and then deletes their raw
        # data, so they have to come from a build whose source is known. Refused before anything is read.
        codes, reasons = campaign_refusal(identity)
        if codes:
            raise RawMetadataExtractorRefused(codes, reasons, identity)
    candidates = [Path(value) for value in snapshot.get("input_candidates", [])]
    available = [path for path in candidates if path.exists()]
    # EVERY FILE BY DEFAULT. MS-DIAL reads the acquisition type per analysis file, so a verdict
    # read from the first three files and applied to the rest is a guess about the files nobody
    # looked at. A caller may still cap the inspection; the cap is then recorded as partial
    # coverage and the unit stays under review.
    capped = bool(max_inputs and max_inputs > 0 and max_inputs < len(available))
    inputs = available if not max_inputs or max_inputs <= 0 else available[0:max_inputs]
    # A unit whose every mzXML failed its conversion has no input to read, and that is its answer: the
    # preflight is recorded with nothing read, and the disposition skips the unit, rather than failing it.
    converted_to_nothing = not candidates and _conversion_left_no_input(snapshot)
    if not inputs and not converted_to_nothing:
        raise ValueError("No extracted MS-DIAL input candidate is available for metadata preflight.")
    output = manifest_path.parent / PREFLIGHT_OUTPUT_NAME
    declared = _declared_technical(snapshot)
    started_at = datetime.now(timezone.utc).isoformat()
    owner = {"pid": os.getpid(), "process_created_at": process_created_at()}

    def progress(note: dict[str, Any]) -> None:
        # The extractor's own deadline for the process about to start, and who is running it, where the
        # campaign runner reads them (preflight_progress_state): a preflight is timed out only here, and the
        # runner, which only watches for a stall, needs to know how long this one may legitimately take.
        def change(current: dict[str, Any]) -> None:
            current["raw_metadata_preflight_progress"] = {
                "started_at": started_at,
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "owner": owner,
                **note,
            }

        update_manifest(manifest_path, change)

    if converted_to_nothing:
        execution = {"outcomes": {}, "records": [], "counts": {}, "chunks": [], "command_template": [], "exit_code": None}
    else:
        execution = run_extractor(
            extractor_path,
            inputs,
            manifest_path.parent / PREFLIGHT_CHUNK_DIRECTORY,
            extractor_sha256=str(identity.get("binary_sha256") or ""),
            previous=_previous_reads(snapshot, manifest_path),
            created_before=_reader_created_before(snapshot),
            progress=progress,
        )
    extractor = _extractor_record(extractor_path, identity, extractor_source)
    held: dict[str, Any] = {}

    def change(current: dict[str, Any]) -> None:
        hold = disposition_hold(current)
        if campaign is not None and hold is not None and hold["reason"] != "split_parent":
            # The unit's run started or ended while its headers were read. What the run recorded stands, and
            # so do the extractor output and the preflight it was made from; this read is dropped.
            current.pop("raw_metadata_preflight_progress", None)
            held.update(hold)
            return
        # Every record read, in input order, where _preflight_start_times and a later reuse find them.
        # Written even when empty, so an earlier preflight's records never pass for this one's.
        _write_json(output, execution["records"])
        _record_preflight(
            current,
            execution=execution,
            extractor=extractor,
            inputs=inputs,
            output=output,
            declared=declared,
            confirm_untargeted=confirm_untargeted,
            capped=capped,
            campaign=campaign,
            started_at=started_at,
        )

    written = update_manifest(manifest_path, change)
    return {**written, "manifest_path": str(manifest_path), **({"preflight_held": held} if held else {})}


# Past an attempt's own time limit, the time a stopped extractor and its bookkeeping may take before a
# preflight that has not moved on is called overdue.
PREFLIGHT_PROGRESS_MARGIN_SECONDS = 120.0


def preflight_progress_state(manifest: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    """Whether a preflight recorded as running is still reading, for a watcher that only detects stalls.

    none: no preflight is recorded as running. running: its process is alive, or cannot be read, and the
    current extractor attempt is within its own limit plus PREFLIGHT_PROGRESS_MARGIN_SECONDS. overdue:
    alive but past that, which the extractor's own limit should have made impossible. gone: the process
    that ran it is not alive, so the record will never be completed. Changes nothing, never raises.
    """
    note = manifest.get("raw_metadata_preflight_progress")
    if not isinstance(note, dict):
        return {"state": "none"}
    owner = note.get("owner") if isinstance(note.get("owner"), dict) else {}
    alive = process_is_alive(owner.get("pid"), owner.get("process_created_at"))
    if alive is False:
        return {"state": "gone", "owner": owner, "attempt": note.get("attempt")}
    try:
        started = datetime.fromisoformat(str(note.get("updated_at") or note.get("started_at")))
        deadline = started.timestamp() + float(note.get("timeout_seconds") or 0.0) + PREFLIGHT_PROGRESS_MARGIN_SECONDS
    except (TypeError, ValueError):
        return {"state": "running", "owner": owner, "attempt": note.get("attempt"), "deadline_at": None}
    current = (now or datetime.now(timezone.utc)).timestamp()
    return {
        "state": "overdue" if current > deadline else "running",
        "owner": owner,
        "attempt": note.get("attempt"),
        "deadline_at": datetime.fromtimestamp(deadline, tz=timezone.utc).isoformat(),
    }


def _per_input_entries(
    per_file: list[dict[str, Any]], outcomes: dict[str, dict[str, Any]], inputs: list[Path]
) -> list[dict[str, Any]]:
    """One record per inspected input: its header verdict, if it had one, and how its read went."""
    from .raw_metadata_preflight import file_key

    by_key = {file_key(str(item.get("file") or "")): item for item in per_file if str(item.get("file") or "")}
    entries = []
    seen = set()
    for path in inputs:
        key = file_key(path)
        seen.add(key)
        entry = by_key.get(key)
        if entry is None:
            entry = {
                "file": str(path),
                "acquisition_mode": "",
                "confidence": None,
                "evidence": None,
                "method_source": "",
                "polarity": "",
                "ms_levels": None,
                "has_ms1": None,
                "has_ms2": None,
                "has_ion_mobility": None,
                "separation": "",
                "isolation_window_count": None,
                "collision_energy_count": None,
                "header_console_acquisition_type": None,
                "header_console_acquisition_basis": "",
                "console_acquisition_type": None,
                "console_acquisition_basis": "",
                "acquisition_start_time": "",
                "acquisition_start_time_evidence": "",
            }
        entry.update({name: value for name, value in (outcomes.get(key) or {}).items()})
        entries.append(entry)
    entries.extend(item for key, item in by_key.items() if key not in seen)
    return entries


def _record_preflight(
    current: dict[str, Any],
    *,
    execution: dict[str, Any],
    extractor: dict[str, Any],
    inputs: list[Path],
    output: Path,
    declared: dict[str, Any],
    confirm_untargeted: bool,
    capped: bool,
    campaign: dict[str, Any] | None,
    started_at: str,
) -> None:
    """Write one preflight's results into the manifest as it is now. The change update_manifest applies."""
    from .raw_metadata_preflight import (
        OUTCOME_UNSUPPORTED,
        READ_OUTCOMES,
        decide_disposition,
        file_key,
    )

    # A split unit - split before this preflight, or while its headers were being read - stays split: a
    # parent is the raw owner of its parts and never runs, whatever its own headers say. The reads are
    # recorded, for its parts to reuse; the verdicts change nothing, and the disposition it carries stands.
    hold = disposition_hold(current)
    split_parent = hold is not None and hold["reason"] == "split_parent"
    # A part excluded at its split is read like any unit, and stays excluded: outside a campaign nothing
    # else here knows that ion mobility is out of scope.
    excluded_part = hold is not None and hold["reason"] == "excluded_at_split"
    status_before = current.get("status")
    project_before = copy.deepcopy(current.get("project") or {})
    previously_allowed = bool(
        current.get("execution_allowed") or (current.get("project") or {}).get("eligible")
    )
    candidates = [str(value) for value in current.get("input_candidates") or [] if str(value).strip()]
    outcomes = execution["outcomes"]
    records = execution["records"]
    counts = execution["counts"]
    read = sum(1 for item in outcomes.values() if item.get("outcome") in READ_OUTCOMES)
    failing = next((entry for entry in execution["chunks"] if entry.get("exit_code") != 0), None)
    block: dict[str, Any] = {
        # The command as each chunk ran it, with the inputs and the chunk output elided: several hundred
        # full paths per unit used to be stored here, and the whole stdout beside them.
        "command_template": execution["command_template"],
        "chunks": [
            {key: value for key, value in entry.items() if key != "stderr_tail"} for entry in execution["chunks"]
        ],
        "extractor": extractor,
        "exit_code": execution["exit_code"],
        "stderr_tail": list((failing or {}).get("stderr_tail") or []),
        "output": str(output),
        "outcomes": counts,
        "declared": declared,
        "started_at": started_at,
        "completed_at": datetime.now(timezone.utc).isoformat(),
    }
    current["raw_metadata_preflight"] = block
    current.pop("raw_metadata_preflight_progress", None)
    coverage = {
        "input_candidates": len(candidates),
        "available": sum(1 for value in candidates if Path(value).exists()),
        "inspected": len(inputs),
        "complete": len(inputs) == len(candidates),
        "capped": capped,
        "read": read,
        **{name: counts.get(name, 0) for name in ("reused", "unsupported_format", "failed", "timed_out", "os_error")},
    }
    summary = _summarize_raw_metadata(records)
    summary["per_file"] = _per_input_entries(summary["per_file"], outcomes, inputs)
    summary["coverage"] = coverage
    block["summary"] = summary

    if not records:
        current["execution_allowed"] = previously_allowed
        if not inputs and _conversion_left_no_input(current):
            # Nothing to read: the lease converted the unit's mzXML and none of them survived. Its status stays
            # what the lease left; the disposition below skips it.
            block["advisory"] = (
                "No input of this unit survived its conversion from mzXML to mzML, so there was no header to "
                "read. The unit has nothing to run, and its campaign disposition skips it."
            )
        elif outcomes and all(item.get("outcome") == OUTCOME_UNSUPPORTED for item in outcomes.values()):
            # Fails closed exactly as an unavailable preflight does: a unit that was not
            # already eligible stays ineligible. What changes is that the agent can now tell
            # that waiting or retrying will never help.
            current["status"] = "preflight_unsupported_format"
            block["unsupported_formats"] = sorted({path.suffix.lower() for path in inputs if path.suffix})
            block["detail"] = block["stderr_tail"][:1]
            block["advisory"] = (
                "This raw data format has no metadata reader, so a raw-header check cannot "
                "resolve the unit's technical settings on any retry. Resolve them from "
                "repository metadata or the publication, or exclude the unit."
            )
        else:
            current["status"] = "preflight_unavailable"
            block["advisory"] = (
                "Raw header inspection was unavailable. Repository metadata remains authoritative "
                "only when the project was already eligible before this optional check."
            )
    else:
        project = project_from_dict(current["project"])
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
            # What the caller asserted, on the strength of what the headers show. The headers show how the
            # data were acquired, not why, so this is an inference and is recorded as one.
            project.evidence.append(
                "Untargeted status was accepted at the raw-metadata preflight on the caller's instruction "
                "(confirm_untargeted): an inference from the acquisition the headers show, not a repository "
                "declaration."
            )
        project.evidence.extend(summary["evidence"])
        evaluated = evaluate_eligibility(
            project,
            EligibilityPolicy(
                max_download_bytes=max(project.total_download_bytes, 1),
                max_samples=max(project.sample_count or 0, 1),
                require_known_size=False,
                require_untargeted=True,
                convert_mzxml=_converts_mzxml(current),
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
        unread = len(inputs) - read
        if unread and not evaluated.exclusion_reasons:
            # One unreadable file used to fail the whole preflight, so nothing was learnt from the rest.
            # Now the rest are read, and the files that could not be are named rather than assumed.
            failures = ", ".join(
                f"{name} {counts[name]}"
                for name in ("unsupported_format", "failed", "timed_out", "os_error")
                if counts.get(name)
            )
            evaluated.review_reasons.append(
                f"The headers of {unread} of {len(inputs)} inspected input files could not be read "
                f"({failures}), so their acquisition mode is not established. A campaign disposition "
                "excludes such files; outside a campaign, resolve them before the unit runs."
            )
            evaluated.eligible = False
            evaluated.selection_status = "raw_metadata_required"
        unread_outcomes = [
            (path, (outcomes.get(file_key(path)) or {}).get("outcome"))
            for path in inputs
            if (outcomes.get(file_key(path)) or {}).get("outcome") not in READ_OUTCOMES
        ]
        if (
            campaign is None
            and previously_allowed
            and unread_outcomes
            and not evaluated.exclusion_reasons
            and summary["acquisition_mode"] != "Mixed"
        ):
            # AS BEFORE, OUTSIDE A CAMPAIGN. One unreadable input used to stop the one extractor process, and
            # the unit ended as an unavailable (or, when that input had no reader, unsupported-format)
            # preflight that left an eligible unit eligible. Only a campaign disposition can exclude that
            # input, so outside one the unit keeps what its repository metadata established, and the verdicts
            # that were read are recorded beside it; the execution gate still holds each file read to its
            # own header.
            current["execution_allowed"] = previously_allowed
            if unread_outcomes[0][1] == OUTCOME_UNSUPPORTED:
                current["status"] = "preflight_unsupported_format"
                block["unsupported_formats"] = sorted(
                    {
                        path.suffix.lower()
                        for path, outcome in unread_outcomes
                        if outcome == OUTCOME_UNSUPPORTED and path.suffix
                    }
                )
            else:
                current["status"] = "preflight_unavailable"
            block["advisory"] = (
                f"The headers of {unread} of {len(inputs)} inspected input files could not be read. Repository "
                "metadata remains authoritative because the project was already eligible before this optional "
                "check; the headers that were read are recorded in summary.per_file."
            )
        else:
            current["project"] = evaluated.as_dict()
            current["execution_allowed"] = evaluated.eligible
            if summary["acquisition_mode"] == "Mixed":
                current["status"] = "preflight_mixed_acquisition"
                groups: dict[str, list[str]] = {}
                for item in summary["per_file"]:
                    if item.get("outcome", "ok") in READ_OUTCOMES:
                        groups.setdefault(item["acquisition_mode"] or "Unknown", []).append(item["file"])
                block["acquisition_groups"] = {mode: sorted(files) for mode, files in sorted(groups.items())}
                block["advisory"] = (
                    "The raw headers disagree about acquisition mode ("
                    + ", ".join(f"{mode} {len(files)}" for mode, files in sorted(groups.items()))
                    + "). MS-DIAL runs one acquisition mode per analysis, so this unit cannot run as one. "
                    "Split it into one unit per acquisition group, each with its own workspace, manifest and "
                    "Class assignments, and preflight each part."
                )
            elif evaluated.eligible:
                current["status"] = "preflight_passed"
            else:
                current["status"] = "preflight_review_required"

    if split_parent or excluded_part:
        # Its status as it was, and the disposition it carries as it was: a campaign acts on whatever
        # disposition a unit carries, and one decided from a split parent's own headers - skip, say - would
        # reach the raw data its parts read. A part excluded at its split keeps its exclusion the same way.
        current["status"] = status_before
        current["execution_allowed"] = False
        if excluded_part:
            current["project"] = project_before
        return
    disposition = decide_disposition(
        current, declared=declared, extractor=extractor, parent_declared=_split_parent_declared(current)
    )
    assignments = disposition.pop("assignments")
    disposition["applied"] = campaign is not None
    if campaign is not None:
        disposition["campaign"] = dict(campaign)
        _apply_disposition(current, disposition, assignments)
    current["campaign_disposition"] = disposition


def _apply_disposition(
    current: dict[str, Any], disposition: dict[str, Any], assignments: dict[str, dict[str, Any]]
) -> None:
    """Make a campaign unit what its disposition says: runnable as decided, split, or held back."""
    from .raw_metadata_preflight import file_key

    kind = disposition["disposition"]
    summary = (current.get("raw_metadata_preflight") or {}).get("summary") or {}
    for entry in summary.get("per_file") or []:
        assigned = assignments.get(file_key(str(entry.get("file") or ""))) if kind in {"run", "split"} else None
        # Under an applied disposition this is the type the file runs as, and an input that does not run -
        # excluded, or in a unit skipped or excluded - has none; what its header alone meant stays in
        # header_console_acquisition_type.
        entry["console_acquisition_type"] = assigned["console_acquisition_type"] if assigned else None
        entry["console_acquisition_basis"] = assigned["basis"] if assigned else ""
    if kind == "run":
        project = project_from_dict(current["project"])
        project.acquisition_mode = str(disposition["console_acquisition_type"])
        project.ion_mode = str(disposition["ion_mode"])
        # A run disposition is an LC-MS one: the repository said LC-MS, or said nothing and the headers did.
        # A header that guessed otherwise must not reach the answer seed, which picks the project type
        # from this field.
        project.separation = "LC-MS"
        warnings = set(disposition.get("warnings") or [])
        lines = []
        if "untargeted_inferred_from_headers" in warnings and project.untargeted is None:
            project.untargeted = True
            lines.append(
                "Untargeted status was inferred from the raw headers, which show DDA, DIA or AIF acquisition "
                "with MS1 and MS2 in every input that runs: an inference, not a repository declaration."
            )
        lines.append(
            f"Campaign disposition: run as {project.acquisition_mode} {project.ion_mode}"
            + (
                f", with {len(disposition['excluded_inputs'])} input(s) excluded"
                if disposition.get("excluded_inputs")
                else ""
            )
            + "."
        )
        project.evidence.extend(line for line in lines if line not in project.evidence)
        evaluated = evaluate_eligibility(
            project,
            EligibilityPolicy(
                max_download_bytes=max(project.total_download_bytes, 1),
                max_samples=max(project.sample_count or 0, 1),
                require_known_size=False,
                require_untargeted=True,
                convert_mzxml=_converts_mzxml(current),
            ),
        )
        current["project"] = evaluated.as_dict()
        current["execution_allowed"] = True
        current["status"] = "preflight_passed"
    elif kind == "split":
        # The split itself is the next step, and its parts are preflighted on their own.
        current["execution_allowed"] = False
    else:
        current["execution_allowed"] = False
        current["status"] = SKIPPED_BY_PREFLIGHT_STATUS if kind == "skip" else EXCLUDED_BY_PREFLIGHT_STATUS


def _rebuilt_legacy_per_file(current: dict[str, Any]) -> list[dict[str, Any]] | None:
    """A summary written before the per-file fields, read again from the extractor's own records.

    Interactive 0.5.16 and earlier summarised each input as acquisition_mode, polarity and ms_levels,
    with no format, MS-level flags or isolation, so a DIA verdict could not be told SWATH from AIF. Such a
    summary exists only for a preflight whose one extractor process read every input, and that process's
    records are in the preflight's output. Returns per-file records in the current shape, for deciding
    only, or None when the summary is current or the output does not hold a record for every input.
    """
    from .raw_metadata_preflight import OUTCOME_OK, file_key, input_format

    preflight = current.get("raw_metadata_preflight") or {}
    entries = [item for item in (preflight.get("summary") or {}).get("per_file") or [] if isinstance(item, dict)]
    if not entries or any("header_console_acquisition_type" in item for item in entries):
        return None
    try:
        raw = json.loads(Path(str(preflight.get("output") or "")).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    records: dict[str, dict[str, Any]] = {}
    for item in [raw] if isinstance(raw, dict) else raw if isinstance(raw, list) else []:
        source = (item.get("source") or {}) if isinstance(item, dict) else {}
        path_text = str(source.get("filePath") or "") if isinstance(source, dict) else ""
        if path_text:
            records.setdefault(file_key(path_text), item)
    keys = [file_key(str(item.get("file") or "")) for item in entries]
    if not all(key in records for key in keys):
        return None
    rebuilt = _summarize_raw_metadata([records[key] for key in keys])["per_file"]
    for entry, legacy in zip(rebuilt, entries):
        entry.update(file=str(legacy.get("file") or ""), outcome=OUTCOME_OK, format=input_format(str(legacy["file"])))
    return rebuilt


def classify_preflight(
    manifest_path: str | Path, campaign_authorization_path: str | Path | None = None
) -> dict[str, Any]:
    """Decide, record and, for a campaign unit, apply the disposition of an already preflighted unit.

    run_raw_metadata_preflight does this as its last step; this is the same step alone, for a unit whose
    preflight is recorded and whose disposition is wanted again - after the unit became a campaign unit,
    or for a manifest written before dispositions existed. The one mapping from verdicts to what a campaign
    does is raw_metadata_preflight.decide_disposition; this is its only writer. Never raises for anything
    in the manifest; only an unreadable manifest or a refused approval raise.

    Nothing is written for a unit disposition_hold holds (split, finished, or running) or one with no
    preflight recorded: the decision is returned with applied false and ``held`` saying why, and the
    manifest, with any disposition it carries, is left as it is. A summary written before the per-file
    fields is decided from the extractor records its preflight left (_rebuilt_legacy_per_file), and
    recorded as it was.
    """
    target = Path(manifest_path).resolve()
    campaign = preflight_campaign(read_manifest(target), campaign_authorization_path)
    with manifest_lock(target):
        current = read_manifest(target)
        preflight = current.get("raw_metadata_preflight") or {}
        disposition = _decide_recorded_preflight(current)
        assignments = disposition.pop("assignments")
        held = disposition_hold(current)
        if held is None and not (preflight.get("summary") or {}):
            held = {
                "reason": "raw_metadata_preflight_missing",
                "status": str(current.get("status") or ""),
                "detail": "No raw-header preflight is recorded; preflight the unit first.",
            }
        if held is not None:
            return {**disposition, "applied": False, "held": held}
        disposition["applied"] = campaign is not None
        if campaign is not None:
            disposition["campaign"] = dict(campaign)
            _apply_disposition(current, disposition, assignments)
        current["campaign_disposition"] = disposition
        _write_json(target, current)
    return disposition


def _decide_recorded_preflight(current: dict[str, Any]) -> dict[str, Any]:
    """decide_disposition over a unit's recorded preflight, as classify_preflight decides it. Changes nothing.

    The declaration is the one the preflight recorded, else _declared_technical; a split part is also held to
    its parent's declaration (_split_parent_declared); a summary written before the per-file fields is decided
    from the extractor records its preflight left (_rebuilt_legacy_per_file). The result keeps ``assignments``.
    """
    from .raw_metadata_preflight import decide_disposition

    preflight = current.get("raw_metadata_preflight") or {}
    declared = preflight.get("declared")
    if not isinstance(declared, dict):
        declared = _declared_technical(current)
    view = current
    rebuilt = _rebuilt_legacy_per_file(current)
    if rebuilt is not None:
        view = copy.deepcopy(current)
        view["raw_metadata_preflight"]["summary"]["per_file"] = rebuilt
    disposition = decide_disposition(view, declared=declared, parent_declared=_split_parent_declared(current))
    if rebuilt is not None and "raw_metadata_preflight_legacy" not in disposition["warnings"]:
        disposition["warnings"].append("raw_metadata_preflight_legacy")
        disposition["detail"].append(
            "The per-file records predate recorded formats, MS-level flags and isolation; the unit was "
            "decided from the extractor records its preflight left."
        )
    return disposition


def is_legacy_disposition(manifest: dict[str, Any]) -> bool:
    """Whether the unit carries an applied disposition decided before 0.5.29 (no declared_acquisition_source)."""
    applied = _applied_disposition(manifest)
    return bool(applied) and "declared_acquisition_source" not in applied


def redecide_legacy_disposition(
    manifest: dict[str, Any], *, write: bool
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Decide an applied pre-0.5.29 disposition again under the header-first rule, as the unit is prepared.

    WHY HERE. The execution gate refuses the rows such a disposition runs and the header-first rule (user
    decision, 2026-10-06) would not (_legacy_disposition_refusals). classify_preflight and a new preflight are
    held for a unit whose run has finished (disposition_hold: past_preflight), so for MTBKS217 and MTBLS1572,
    both mztab_validated, neither could clear it. Preparing the unit again (msdial_prepare_repository_reanalysis)
    is the step that precedes any run, and the analysis CSV it writes is what the gate checks, so the unit is
    decided again there and the CSV is built from that decision.

    Returns (record, manifest). record is None, and manifest the one given, for a unit with no applied
    disposition or one decided from 0.5.29 on. Otherwise the unit is decided from its recorded preflight
    (_decide_recorded_preflight) and the decision applied as classify_preflight applies it, under the campaign
    the old disposition recorded; with ``write`` the manifest on disk (``manifest_path``) is changed under its
    lock, otherwise only the copy returned. The old disposition, with each input's type and basis under it,
    is kept in the new one's ``supersedes``.

    A finished unit (mztab_validated, completed, cleanup_pending_confirmation) keeps its status: its run
    happened, and the outputs on disk are that run's. Its execution_allowed follows the new decision, false
    where the unit would no longer run. A unit disposition_hold holds for any other reason - split, excluded at
    its split, a run attempt open, raw data released - is not decided again (record ``held``); the gate refuses
    it on its own grounds, and the legacy refusal stands.
    """
    if not is_legacy_disposition(manifest):
        return None, manifest

    def decide(current: dict[str, Any]) -> dict[str, Any]:
        previous = copy.deepcopy(_applied_disposition(current))
        status = str(current.get("status") or "")
        record: dict[str, Any] = {
            "previous_disposition": str(previous.get("disposition") or ""),
            "previous_console_acquisition_type": previous.get("console_acquisition_type"),
            "status": status,
            "redecided": False,
            "held": None,
        }
        if not is_legacy_disposition(current):
            # Decided again by another writer since the caller read the unit.
            return {**record, "already_current": True}
        held = disposition_hold(current)
        finished = held is not None and held["reason"] == "past_preflight" and status in CLEANUP_READY_STATUSES
        if held is not None and not finished:
            return {**record, "held": held}
        disposition = _decide_recorded_preflight(current)
        assignments = disposition.pop("assignments")
        disposition["applied"] = True
        campaign = previous.get("campaign") or preflight_campaign(current)
        if campaign:
            disposition["campaign"] = dict(campaign)
        summary = (current.get("raw_metadata_preflight") or {}).get("summary") or {}
        disposition["supersedes"] = {
            **previous,
            "per_file": [
                {
                    "file": str(entry.get("file") or ""),
                    "console_acquisition_type": entry.get("console_acquisition_type"),
                    "console_acquisition_basis": entry.get("console_acquisition_basis") or "",
                }
                for entry in summary.get("per_file") or []
                if isinstance(entry, dict)
            ],
        }
        disposition["redecided"] = {
            "reason": "legacy_disposition",
            "by": "msdial_prepare_repository_reanalysis",
            "decided_at": datetime.now(timezone.utc).isoformat(),
            "status_kept": finished,
        }
        _apply_disposition(current, disposition, assignments)
        current["campaign_disposition"] = disposition
        if finished:
            current["status"] = status
        return {
            **record,
            "redecided": True,
            "status_kept": finished,
            "disposition": disposition["disposition"],
            "reasons": list(disposition.get("reasons") or []),
            "console_acquisition_type": disposition.get("console_acquisition_type"),
            "excluded_inputs": [
                {"file": Path(str(item.get("path") or "")).name, "reason": str(item.get("reason") or "")}
                for item in disposition.get("excluded_inputs") or []
                if isinstance(item, dict)
            ],
            "execution_allowed": current.get("execution_allowed") is True,
        }

    def attempt(current: dict[str, Any]) -> dict[str, Any]:
        # A unit that cannot be decided again is prepared as it is, and the gate refuses its legacy rows.
        try:
            return decide(current)
        except Exception as error:
            return {"redecided": False, "held": None, "error": f"{type(error).__name__}: {error}"}

    if not write:
        view = copy.deepcopy(manifest)
        record = attempt(view)
        return {**record, "written": False}, view if record["redecided"] else manifest
    if not str(manifest.get("manifest_path") or "").strip():
        raise ValueError("A legacy disposition is decided again on disk only for a manifest read from its path.")
    target = Path(str(manifest["manifest_path"])).resolve()
    with manifest_lock(target):
        current = read_manifest(target)
        record = attempt(current)
        if record["redecided"]:
            _write_json(target, current)
    # The unit as it is on disk now, unless a decision that failed left the copy read half changed.
    return {**record, "written": record["redecided"]}, (
        manifest if "error" in record else {**current, "manifest_path": str(target)}
    )


# The status a parent unit carries once it has been split. It is not in CLEANUP_READY_STATUSES and it
# never sets execution_allowed, so neither MS-DIAL nor a raw deletion can run against the parent.
SPLIT_PARENT_STATUS = "split_by_acquisition"
SPLIT_PART_STATUS = "split_from_parent"


DECIDED_TYPE_PART_MODE = {"DDA": "DDA", "SWATH": "DIA", "AIF": "AIF"}

# ONE SPLIT KEY. A part is one acquisition mode, one ion-mobility regime and one polarity: MS-DIAL runs one
# acquisition type and one ion mode per analysis, and the campaign runs LC-MS only. The key was the
# acquisition mode alone, so a BAF and a TDF folder of one DDA unit landed in one part (or, keyed by
# container as well, under one id twice), and a unit whose files differed only in polarity could not be
# split at all. A part whose files carry ion mobility is written excluded: LC-IM-MS is outside the campaign's
# scope, and Interactive runs no ion-mobility project. The part id names every key part in which the parts
# differ, after the acquisition mode it always named, so a DDA/DIA split keeps <unit>-dda and <unit>-dia.
SPLIT_KEY_SCHEMA = "msdial-split-key.v1"
SPLIT_KEY_PARTS = ("acquisition", "ion_mobility", "polarity")
ION_MOBILITY_PART_TOKEN = "im"
POLARITY_PART_TOKENS = {"Positive": "pos", "Negative": "neg"}
ION_MOBILITY_EXCLUSION = "ion_mobility_out_of_scope"
ION_MOBILITY_EXCLUSION_REASON = (
    "LC-IM-MS is outside this campaign's LC-MS/MS scope: these inputs carry an ion-mobility dimension, by their "
    "raw header (has_ion_mobility) or their container format, and Interactive runs no ion-mobility project."
)


def _split_part_id(parent_unit_id: str, mode: str, ion_mobility: bool = False, polarity: str = "") -> str:
    tokens = [mode.casefold()]
    if ion_mobility:
        tokens.append(ION_MOBILITY_PART_TOKEN)
    if polarity:
        tokens.append(POLARITY_PART_TOKENS[polarity])
    return f"{parent_unit_id}-{'-'.join(tokens)}"


def _input_ion_mobility(verdict: dict[str, Any] | None, path: str) -> bool:
    """Whether an input carries ion mobility: its header says so, or its container format is a mobility one.

    Either is enough, as for the campaign disposition (decide_disposition), which excludes such an input by
    the same reading: a TDF folder whose header read no mobility is still a timsTOF acquisition.
    """
    from .raw_metadata_preflight import ION_MOBILITY_FORMATS, input_format

    entry = verdict or {}
    if entry.get("has_ion_mobility") is True:
        return True
    return (str(entry.get("format") or "") or input_format(path)) in ION_MOBILITY_FORMATS


def _disposition_polarities(disposition: dict[str, Any]) -> dict[str, str]:
    """The polarity an applied split disposition gave each input, by _file_key."""
    result: dict[str, str] = {}
    for group in ((disposition.get("split_key") or {}).get("groups") or []) if disposition else []:
        polarity = str((group or {}).get("polarity") or "") if isinstance(group, dict) else ""
        if polarity in POLARITY_PART_TOKENS:
            for item in group.get("inputs") or []:
                result[_file_key(str(item))] = polarity
    return result


def _input_samples(manifest: dict[str, Any], candidates: list[str]) -> dict[str, str]:
    """The sample each input is, by _file_key: the Catalog's declared input it is, else its lineage row's.

    Asked as the analysis-CSV builder asks it (match_declared_inputs), so a part holds the samples its CSV
    will. A name alone cannot say it where two folders share one, as pos/S1.raw and neg/S1.raw do.
    """
    lineage = manifest.get("input_lineage") if isinstance(manifest.get("input_lineage"), dict) else {}
    lineage_samples = {
        _file_key(str(row.get("path") or "")): str(row.get("sample_id") or "").strip()
        for row in lineage.get("rows") or []
        if isinstance(row, dict) and str(row.get("path") or "").strip()
    }
    result = {key: value for key, value in lineage_samples.items() if value}
    project_record = manifest.get("project") or {}
    data_root = str(manifest.get("input_directory") or "").strip()
    if not project_record.get("analysis_inputs") or not data_root:
        return result
    try:
        project = project_from_dict(project_record)
    except (TypeError, ValueError):
        return result
    declared = declared_analysis_inputs(project)
    if not declared:
        return result
    matched = match_declared_inputs(
        candidates,
        Path(data_root),
        declared,
        containers=declared_archive_containers(project, declared, manifest.get("archive_extractions")),
        samples=lineage_samples,
        stands_for=lineage_stands_for(manifest),
    )
    for form, found in matched.items():
        sample = str(declared[form].get("sample_id") or "").strip()
        for item in found if sample else []:
            result[_file_key(item)] = sample
    return result


def _sample_parts_by_path(
    samples: list[dict[str, Any]],
    groups: dict[tuple[str, bool, str], list[str]],
    sample_of: dict[str, str],
    stands_for: dict[str, str],
    data_root: Path | None,
) -> dict[int, set[tuple[str, bool, str]]]:
    """The parts (group keys) of each sample no input names (sample_of), by the sample's index.

    A sample's raw_file is matched against the paths of the parts' unnamed inputs under the data root, in the
    forms a listed file may carry for them (_allowlist_forms), as _files_of_parts matches the file list: the
    full path before any shorter form. Only a raw_file no input's path accounts for is matched by its name
    (or its stem, when it records no extension), as every sample used to be. An input is then the sample's
    that matched it best; where two samples match it equally well, neither has it. By name alone pos/S1.raw and
    neg/S1.raw are one file, and each polarity part held the samples of both.
    """
    forms: dict[str, list[tuple[int, tuple[str, bool, str], str]]] = {}
    bare_forms: dict[str, list[tuple[int, tuple[str, bool, str], str]]] = {}
    names: dict[str, list[tuple[tuple[str, bool, str], str]]] = {}
    stems: dict[str, list[tuple[tuple[str, bool, str], str]]] = {}
    for key, files in groups.items():
        for item in files:
            item_key = _file_key(item)
            if sample_of.get(item_key):
                continue
            for source in (item, stands_for.get(item_key, "")):
                if not source:
                    continue
                name = Path(source).name.casefold()
                names.setdefault(name, []).append((key, item_key))
                stems.setdefault(PurePosixPath(name).stem, []).append((key, item_key))
                relative = _relative_to_data_root(Path(source), data_root) if data_root is not None else None
                if relative is None:
                    continue
                exact = {relative, relative[6:] if relative.startswith("files/") else relative}
                for form in _allowlist_forms(relative):
                    entry = (2 if form in exact else 1, key, item_key)
                    forms.setdefault(form, []).append(entry)
                    bare = PurePosixPath(form)
                    bare_forms.setdefault(str(bare.with_name(bare.stem)) if bare.suffix else form, []).append(entry)
    # input -> {sample index: how well it matched}: 3 and 2 by path, the full path or a shorter form; 1 by name.
    claims: dict[str, dict[int, int]] = {}
    for index, sample in enumerate(samples):
        text = str((sample or {}).get("raw_file") or "").strip()
        raw = PurePosixPath(text.replace("\\", "/")).name.casefold()
        if not raw:
            continue
        no_extension = not PurePosixPath(raw).suffix
        listed = _listed_key(text)
        alias = _container_alias_path(listed) if listed else ""
        table = bare_forms if no_extension else forms
        found = [*(table.get(listed) or []), *(forms.get(alias) or [] if alias else [])] if listed else []
        if found:
            best = max(rank for rank, _key, _item in found)
            matched = [(rank + 1, item) for rank, _key, item in found if rank == best]
        else:
            alias_name = archives.container_alias(raw).casefold()
            by_name = [*(names.get(raw) or []), *(names.get(alias_name) or [] if alias_name else [])]
            if no_extension:
                by_name.extend(stems.get(raw) or [])
            matched = [(1, item) for _key, item in by_name]
        for level, item in matched:
            claims.setdefault(item, {})[index] = max(level, claims.get(item, {}).get(index, 0))
    part_of = {_file_key(item): key for key, files in groups.items() for item in files}
    result: dict[int, set[tuple[str, bool, str]]] = {}
    for item, by_sample in claims.items():
        best = max(by_sample.values())
        owners = [index for index, level in by_sample.items() if level == best]
        if len(owners) == 1:
            result.setdefault(owners[0], set()).add(part_of[item])
    return result


def plan_acquisition_split(manifest_path: Path) -> dict[str, Any]:
    """Describe how a unit would be split by its split key. Changes nothing.

    WHY A SPLIT AND NOT A RELABEL. MS-DIAL deconvolutes each file by the acquisition type written
    against it, and a DDA file deconvoluted as SWATH, or a DIA file read as DDA, gives a result that
    completes, validates and is wrong. A unit whose headers disagree therefore cannot run as one,
    and the only evidence for how to divide it is what each file's own header said. The parts are
    made from that evidence and nothing else: a file whose header gave no usable mode blocks the
    split rather than being guessed into a part.

    THE KEY is the acquisition mode, the ion-mobility regime and the polarity (SPLIT_KEY_PARTS). A unit
    is split when its headers disagree about acquisition mode, when an applied campaign disposition
    splits it, or when its inputs differ in any part of the key. Polarity is the header's, or the one an
    applied disposition gave the input; it is part of the key only where the inputs differ in it, and
    then an input with no single polarity blocks the split. A part of ion-mobility inputs is planned
    excluded (ION_MOBILITY_EXCLUSION), so its inputs are accounted for and none of them runs.

    The parts share the parent's raw data and do not copy it. Their input files are disjoint, and
    MS-DIAL's per-file intermediates carry a run timestamp, so parts run one after another do not
    collide. The raw data stay owned by the parent: a part's cleanup is refused, because its raw
    directory is not its own, and the parent's tree is released only by plan_split_parent_cleanup's
    rule, once every part has ended.
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
    disposition = _applied_disposition(manifest)
    # A campaign disposition splits SWATH from AIF too, and both of those read "DIA" in the header; and it
    # splits by polarity where the inputs' headers differ in it.
    split_by_disposition = disposition.get("disposition") == "split" and bool(
        set((disposition.get("split_key") or {}).get("by") or []) & {"acquisition", "polarity"}
    )
    if not coverage.get("complete"):
        blockers.append(
            "The raw-header preflight did not read every input file, so the files it did not read "
            "cannot be assigned to a part. Re-run it over every file."
        )

    per_file = {_file_key(str(item.get("file") or "")): item for item in summary.get("per_file") or []}
    candidates = [str(item) for item in manifest.get("input_candidates") or [] if str(item).strip()]
    decided = _decided_acquisition_by_file(manifest)
    excluded = _campaign_excluded_inputs(manifest)
    decided_polarity = _disposition_polarities(disposition)
    keyed: list[tuple[str, str, bool, str]] = []
    unassigned: list[str] = []
    left_out: list[dict[str, str]] = []
    for candidate in candidates:
        if _file_key(candidate) in excluded:
            # The disposition excluded it; it belongs to no part, and it does not block the others.
            left_out.append({"path": candidate, "reason": excluded[_file_key(candidate)]})
            continue
        verdict = per_file.get(_file_key(candidate))
        mode = str((verdict or {}).get("acquisition_mode") or "").strip()
        console = decided.get(_file_key(candidate))
        if console in DECIDED_TYPE_PART_MODE:
            # The type an applied campaign disposition decided, named as the header names it (a SWATH file
            # is in the DIA part), so a DDA/DIA split keeps the part identifiers it always had.
            mode = DECIDED_TYPE_PART_MODE[console]
        if mode not in HEADER_ACQUISITION_TO_MSDIAL:
            unassigned.append(f"{Path(candidate).name} ({mode or 'no header verdict'})")
            continue
        polarity = decided_polarity.get(_file_key(candidate)) or str((verdict or {}).get("polarity") or "").strip()
        keyed.append(
            (
                candidate,
                mode,
                _input_ion_mobility(verdict, candidate),
                polarity if polarity in POLARITY_PART_TOKENS else "",
            )
        )
    if unassigned:
        blockers.append(
            f"{len(unassigned)} input files have no DDA, DIA or AIF header verdict and cannot be put "
            f"into a part: {', '.join(unassigned[:5])}. Decide whether to exclude them."
        )
    by_polarity = len({polarity for *_, polarity in keyed if polarity}) > 1
    if by_polarity:
        unpolarised = [Path(candidate).name for candidate, *_, polarity in keyed if not polarity]
        if unpolarised:
            blockers.append(
                f"The inputs differ in polarity, and {len(unpolarised)} of them record no single polarity, so "
                f"they cannot be put into a part: {', '.join(unpolarised[:5])}. Decide whether to exclude them."
            )
    groups: dict[tuple[str, bool, str], list[str]] = {}
    for candidate, mode, mobility, polarity in keyed:
        if by_polarity and not polarity:
            continue
        groups.setdefault((mode, mobility, polarity if by_polarity else ""), []).append(candidate)
    varying = [
        name
        for name, index in (("acquisition", 0), ("ion_mobility", 1), ("polarity", 2))
        if len({key[index] for key in groups}) > 1
    ]
    if summary.get("acquisition_mode") != "Mixed" and not split_by_disposition and not varying:
        described = summary.get("acquisition_mode") or project.get("acquisition_mode") or "unknown"
        blockers.append(
            "Only a unit whose raw headers disagree about acquisition mode, ion-mobility regime or polarity is "
            f"split here; this one is {described!r} in one regime and one polarity."
        )

    samples = list(project.get("sample_metadata") or [])
    assignments = list((project.get("class_proposal") or {}).get("assignments") or [])
    # An mzML converted from an mzXML, or chosen over one, is that mzXML's sample's: the sample names the
    # repository's file, not what the lease wrote from it.
    stands_for = lineage_stands_for(manifest)
    sample_of = _input_samples(manifest, candidates)
    named_anywhere = set(sample_of.values())
    data_root_text = str(manifest.get("input_directory") or "").strip()
    # The rest are matched by their raw_file's path, and only where no path accounts for it by name.
    by_path = _sample_parts_by_path(
        samples, groups, sample_of, stands_for, Path(data_root_text) if data_root_text else None
    )
    parts = []
    claimed_samples: set[int] = set()
    for group_key, files in sorted(groups.items()):
        mode, mobility, polarity = group_key
        # A sample the declared inputs or the lineage attribute is matched by that; only the rest by path.
        named = {sample_of[_file_key(item)] for item in files if sample_of.get(_file_key(item))}
        part_samples = []
        for index, sample in enumerate(samples):
            sample_id = str((sample or {}).get("sample_id") or "").strip()
            if sample_id and sample_id in named:
                matched = True
            elif sample_id and sample_id in named_anywhere:
                matched = False
            else:
                matched = group_key in by_path.get(index, set())
            if matched:
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
        part_id = _split_part_id(parent_id, mode, ion_mobility=mobility, polarity=polarity)
        part = {
            "analysis_unit_id": part_id,
            "acquisition_mode": mode,
            "ion_mobility": mobility,
            "polarity": polarity,
            "split_key": {"acquisition": mode, "ion_mobility": mobility, "polarity": polarity},
            "workspace": str(workspace.parent / part_id),
            "input_candidates": sorted(files),
            "file_count": len(files),
            "sample_ids": sorted(sample_ids),
            "class_levels": levels,
            "higher_ms_levels": higher_levels,
        }
        if mobility:
            part["excluded"] = {"reason": ION_MOBILITY_EXCLUSION, "detail": ION_MOBILITY_EXCLUSION_REASON}
        parts.append(part)
    unclaimed = [
        str((sample or {}).get("sample_id") or index)
        for index, sample in enumerate(samples)
        if index not in claimed_samples
    ]
    return {
        "manifest_path": str(manifest_path),
        "analysis_unit_id": parent_id,
        "already_split": False,
        "split_key": {"schema": SPLIT_KEY_SCHEMA, "parts": list(SPLIT_KEY_PARTS), "by": varying},
        "parts": parts,
        "unclaimed_samples": unclaimed,
        "excluded_inputs": left_out,
        "blockers": blockers,
    }


def _part_label(part: dict[str, Any]) -> str:
    """'DDA', or 'DDA ion-mobility Negative': the part's key as words, for evidence and warnings."""
    return " ".join(
        [
            str(part.get("acquisition_mode") or ""),
            *(["ion-mobility"] if part.get("ion_mobility") else []),
            *([str(part["polarity"])] if part.get("polarity") else []),
        ]
    )


def _part_class_proposal(
    parent_proposal: dict[str, Any], sample_ids: set[str], parent_unit_id: str, mode: str, label: str = ""
) -> dict[str, Any]:
    """The parent's accepted Class proposal, restricted to one part's samples, saying so.

    The rationale is the Catalog's own text about the whole unit and is left as written. What a run's
    provenance has to carry besides it is that this run holds only some of those samples: MTBLS2207's
    DDA part was about to record "Class ... across 11 samples" for a run of six. The warnings travel
    into the run's class_proposal_provenance, so that is where it is said.
    """
    if not parent_proposal:
        return {}
    label = label or mode
    proposal = copy.deepcopy(parent_proposal)
    every = list(proposal.get("assignments") or [])
    kept = [item for item in every if str(item.get("sample_id") or "") in sample_ids]
    levels = sorted({str(item.get("class_label") or "") for item in kept})
    proposal["assignments"] = kept
    proposal["split_from"] = {
        "parent_analysis_unit_id": parent_unit_id,
        "split_by": "raw_header_acquisition_mode" if label == mode else "raw_header_split_key",
        "acquisition_mode": mode,
        "assignments_kept": len(kept),
        "assignments_in_parent": len(every),
        "note": "The parent's accepted proposal, restricted to this part's samples. No sample was regrouped.",
    }
    warnings = list(proposal.get("warnings") or [])
    warnings.append(
        f"This run holds {len(kept)} of the {len(every)} samples the proposal assigned: analysis unit "
        f"{parent_unit_id} was split by raw-header "
        + ("acquisition mode" if label == mode else "acquisition mode, ion-mobility regime and polarity")
        + f" and this is its {label} part. The rationale describes the whole unit."
    )
    if len(levels) < 2:
        warnings.append(
            f"After the split this part holds {len(levels)} Class level(s) "
            f"({', '.join(levels) or 'none'}), so it carries no contrast."
        )
    proposal["warnings"] = warnings
    return proposal


def _listed_key(name: str) -> str:
    """A listed file's path as the lease places it (_safe_relative_name), casefolded; '' when unsafe."""
    try:
        return _safe_relative_name(str(name or "")).as_posix().casefold()
    except ValueError:
        return ""


def _files_of_parts(
    listed: list[dict[str, Any]],
    parts: list[dict[str, Any]],
    data_root: Path | None,
    stands_for: dict[str, str],
) -> dict[str, list[dict[str, Any]]]:
    """Each part's entries of its parent's file list: its inputs' own, and its folders' members.

    A member belongs to the part whose input its container is, matched by the container's path under the
    data root (as the lease's allow-list matches it, _allowlist_forms), the full path before any shorter
    form; only an entry no input's path accounts for is matched by name, as every entry used to be. By name
    alone, pos/S1.raw and neg/S1.raw are one folder, and each polarity part listed both folders' members.
    A converted input is listed as the mzXML it was converted from.
    """
    forms: dict[str, dict[str, int]] = {}
    names: dict[str, set[str]] = {}
    for part in parts:
        part_id = part["analysis_unit_id"]
        for item in part["input_candidates"]:
            for source in (item, stands_for.get(_file_key(item), "")):
                if not source:
                    continue
                names.setdefault(Path(source).name.casefold(), set()).add(part_id)
                relative = _relative_to_data_root(Path(source), data_root) if data_root is not None else None
                if relative is None:
                    continue
                exact = {relative, relative[6:] if relative.startswith("files/") else relative}
                for form in _allowlist_forms(relative):
                    rank = 2 if form in exact else 1
                    owners = forms.setdefault(form, {})
                    owners[part_id] = max(owners.get(part_id, 0), rank)
    result: dict[str, list[dict[str, Any]]] = {part["analysis_unit_id"]: [] for part in parts}
    for entry in listed:
        if not isinstance(entry, dict):
            continue
        member = str(entry.get("role") or "") == VENDOR_FOLDER_MEMBER_ROLE
        name = str((entry.get("container") if member else entry.get("name") or entry.get("path")) or "")
        key = _listed_key(name)
        owners = (forms.get(key) or forms.get(_container_alias_path(key)) or {}) if key else {}
        if owners:
            best = max(owners.values())
            chosen = {part_id for part_id, rank in owners.items() if rank == best}
        else:
            chosen = names.get(PurePosixPath(name.replace("\\", "/")).name.casefold(), set())
        for part_id in chosen:
            result[part_id].append(entry)
    return result


def _excluded_part_disposition(
    parent: dict[str, Any], part_manifest: dict[str, Any], decided_at: str
) -> dict[str, Any]:
    """The campaign disposition of a part planned excluded, decided by the one mapping (decide_disposition).

    Decided from the parent's header verdicts for the part's inputs, which is all a part has before its own
    preflight; it excludes every ion-mobility input, so the part is excluded whole. It is applied when the
    parent is a campaign unit, so a campaign acts on it as on any excluded unit's.
    """
    from .raw_metadata_preflight import decide_disposition

    preflight = parent.get("raw_metadata_preflight") or {}
    summary = preflight.get("summary") or {}
    view = {
        "raw_metadata_preflight": {
            "summary": {
                **{key: value for key, value in summary.items() if key not in ("per_file", "coverage")},
                "per_file": list(part_manifest.get("header_verdicts_from_parent") or []),
                "coverage": {**(summary.get("coverage") or {}), "complete": True, "capped": False},
            },
            "declared": preflight.get("declared"),
            "extractor": preflight.get("extractor"),
        },
        "input_candidates": list(part_manifest.get("input_candidates") or []),
        "project": part_manifest.get("project") or {},
    }
    declared = preflight.get("declared") if isinstance(preflight.get("declared"), dict) else None
    disposition = decide_disposition(view, declared=declared, extractor=preflight.get("extractor"), decided_at=decided_at)
    disposition.pop("assignments", None)
    if disposition.get("disposition") not in {"exclude", "skip"}:
        # Not reachable while decide_disposition excludes ion mobility; a part of mobility data never runs.
        disposition.update(
            disposition="exclude",
            reasons=[ION_MOBILITY_EXCLUSION],
            excluded_inputs=[
                {"path": item, "reason": ION_MOBILITY_EXCLUSION} for item in part_manifest.get("input_candidates") or []
            ],
            split_key=None,
        )
    disposition["decided_from"] = "split_parent_preflight"
    campaign = preflight_campaign(parent)
    disposition["applied"] = campaign is not None
    if campaign is not None:
        disposition["campaign"] = dict(campaign)
    return disposition


def split_unit_by_acquisition(manifest_path: Path, confirmed: bool = False) -> dict[str, Any]:
    """Split a unit into one part per value of its split key, each with its own manifest.

    With confirmed false this is plan_acquisition_split. With confirmed true it writes, for each part,
    a workspace holding provenance and output directories and a run manifest that:

    - admits only that part's input files, so the execution gate refuses the others;
    - carries the part's acquisition mode as read from its files' headers, and those headers' own
      verdicts, so the gate can check each file against its header; and, for a part split by polarity,
      that polarity as its ion mode;
    - keeps only the samples, and the accepted Class assignments, that belong to its files. The
      Class decision is the parent's, filtered; it is not a new grouping;
    - lists only its own entries of the parent's file list, its folders' members among them;
    - starts with execution_allowed false. Each part is preflighted on its own before it can run.

    A part of ion-mobility inputs is written excluded (status excluded_by_preflight, split_exclusion, and
    the campaign disposition the one mapping gives it), so it never runs and it counts as ended for the
    release of its parent's raw data.

    The parent is marked split and names its parts. Nothing is copied or deleted.

    A confirmed split holds the parent manifest's writer lock from its plan to its last write, so the plan
    is made from the manifest the parent is marked on: a preflight finishing meanwhile, or a second split
    of the same unit, waits and then sees the split instead of racing it.
    """
    if not confirmed:
        return {**plan_acquisition_split(manifest_path), "written": False}
    with manifest_lock(Path(manifest_path)):
        return _split_unit_locked(Path(manifest_path))


def _split_unit_locked(manifest_path: Path) -> dict[str, Any]:
    plan = plan_acquisition_split(manifest_path)
    if plan["already_split"] or plan["blockers"]:
        return {**plan, "written": False}

    manifest_path = Path(plan["manifest_path"])
    parent = read_manifest(manifest_path)
    parent_project = parent.get("project") or {}
    # A campaign parent's mzXML were converted, and so are its parts'; any other unit's still require it.
    converts = _converts_mzxml(parent)
    parent_conversion_reasons = [
        reason
        for reason in evaluate_eligibility(
            project_from_dict(parent_project),
            EligibilityPolicy(
                max_download_bytes=max(int(parent_project.get("total_download_bytes") or 0), 1),
                max_samples=max(int(parent_project.get("sample_count") or 0), 1),
                require_known_size=False,
                require_untargeted=False,
                convert_mzxml=converts,
            ),
        ).exclusion_reasons
        if reason.startswith((CONVERSION_REQUIRED_REASON, UNCONVERTIBLE_INPUT_REASON))
    ]
    stands_for = lineage_stands_for(parent)
    per_file = {
        _file_key(str(item.get("file") or "")): item
        for item in (parent.get("raw_metadata_preflight") or {}).get("summary", {}).get("per_file") or []
    }
    from .raw_metadata_preflight import SPLIT_PARENT_DECLARED_FIELD

    parent_declared_mode = _split_parent_declared(parent)
    if parent_declared_mode is None:
        parent_declared_mode = str(
            ((parent.get("raw_metadata_preflight") or {}).get("declared") or _declared_technical(parent)).get(
                "acquisition_mode"
            )
            or ""
        ).strip()
        if parent_declared_mode.casefold() == "unknown":
            parent_declared_mode = ""
    data_root = str(parent.get("input_directory") or "").strip()
    files_of = _files_of_parts(
        list(parent_project.get("files") or []),
        plan["parts"],
        Path(data_root) if data_root else None,
        stands_for,
    )
    from .repository_metadata import metadata_workspace

    now = datetime.now(timezone.utc).isoformat()
    written = []
    for part in plan["parts"]:
        root = Path(part["workspace"])
        provenance = root / "provenance"
        output = root / "output"
        provenance.mkdir(parents=True, exist_ok=True)
        output.mkdir(parents=True, exist_ok=True)
        sample_ids = set(part["sample_ids"])
        label = _part_label(part)

        project = copy.deepcopy(parent_project)
        project["analysis_unit_id"] = part["analysis_unit_id"]
        project["acquisition_mode"] = part["acquisition_mode"]
        if part.get("polarity"):
            project["ion_mode"] = part["polarity"]
        project["sample_metadata"] = [
            sample for sample in parent_project.get("sample_metadata") or []
            if str((sample or {}).get("sample_id") or "") in sample_ids
        ]
        project["sample_count"] = len(project["sample_metadata"]) or part["file_count"]
        if project.get("analysis_inputs"):
            # The Catalog's inputs of this part's samples only, so a part's analysis CSV is held to its own
            # inputs and not to the parent's.
            project["analysis_inputs"] = [
                entry for entry in parent_project.get("analysis_inputs") or []
                if isinstance(entry, dict) and str(entry.get("sample_id") or "") in sample_ids
            ]
        # A folder's files are listed one by one, as its members (Catalog 0.6.0): they go with the part
        # whose input their folder is, so a part's download description is its own folders'. An archive
        # unit's file list names the archive, not the files inside it; the part then shares the parent's
        # download description rather than being given an empty one.
        project["files"] = files_of[part["analysis_unit_id"]] or list(parent_project.get("files") or [])
        project["total_download_bytes"] = sum(int(item.get("size_bytes") or 0) for item in project["files"])
        proposal = _part_class_proposal(
            parent_project.get("class_proposal") or {},
            sample_ids,
            plan["analysis_unit_id"],
            part["acquisition_mode"],
            label,
        )
        if proposal:
            project["class_proposal"] = proposal
        project["evidence"] = list(project.get("evidence") or []) + [
            (
                f"Split from analysis unit {plan['analysis_unit_id']} by raw-header acquisition mode: "
                f"{part['file_count']} file(s) whose headers read {part['acquisition_mode']}."
            )
            if label == part["acquisition_mode"]
            else (
                f"Split from analysis unit {plan['analysis_unit_id']} by raw-header acquisition mode, ion-mobility "
                f"regime and polarity: {part['file_count']} file(s) read as {label}."
            )
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
                convert_mzxml=converts,
            ),
        )
        excluded_part = part.get("excluded")
        project["exclusion_reasons"] = list(
            dict.fromkeys(
                [
                    *evaluated.exclusion_reasons,
                    *parent_conversion_reasons,
                    *([excluded_part["detail"]] if excluded_part else []),
                ]
            )
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
                "split_by": "raw_header_acquisition_mode" if label == part["acquisition_mode"] else "raw_header_split_key",
                "acquisition_mode": part["acquisition_mode"],
                # What the parent's repository record declared (decide_disposition holds a part to it where it
                # keeps MS1-only inputs out of a DDA run); "" where it declared nothing.
                SPLIT_PARENT_DECLARED_FIELD: parent_declared_mode,
                **({"split_key": part["split_key"]} if label != part["acquisition_mode"] else {}),
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
        if excluded_part:
            # Ended at the split: it never runs, and its parent's raw data are released once the others end.
            disposition = _excluded_part_disposition(parent, part_manifest, now)
            part_manifest["status"] = (
                SKIPPED_BY_PREFLIGHT_STATUS if disposition["disposition"] == "skip" else EXCLUDED_BY_PREFLIGHT_STATUS
            )
            part_manifest["split_exclusion"] = {**excluded_part, "decided_at": now}
            part_manifest["campaign_disposition"] = disposition
        lineage = parent.get("input_lineage")
        if isinstance(lineage, dict):
            # The part reads the parent's files, so their lineage is the parent's, row for row. Without
            # it every part would look like a manifest written before lineage existed. The files the lease
            # excluded are in no part, and stay recorded in the parent's table only, as do the mzXML it
            # converted: each converted row carries its own mzXML's row (source.conversion.source_row).
            part_keys = {_file_key(item) for item in part["input_candidates"]}
            part_manifest["input_lineage"] = {
                **{
                    key: value for key, value in lineage.items()
                    if key not in ("rows", "excluded", "conversion_sources")
                },
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
        current["split_key"] = plan["split_key"]
        if plan.get("excluded_inputs"):
            # The parent's inputs are its parts' plus these, each exactly once.
            current["split_excluded_inputs"] = plan["excluded_inputs"]

    update_manifest(manifest_path, change)
    return {**plan, "parts": written, "written": True}


# The manifest states from which a confirmed deletion may proceed. cleanup_pending_confirmation is the
# state a run leaves behind when its retention policy asked for deletion: the technical preconditions are
# met and the decision is now waiting for a person.
CLEANUP_READY_STATUSES = {"mztab_validated", "completed", "cleanup_pending_confirmation"}
# The states past every preflight: a run finished, or the raw data are gone (raw_cleaned after a run,
# discarded without one). What recorded them, not a disposition, describes the unit from then on.
PAST_PREFLIGHT_STATUSES = CLEANUP_READY_STATUSES | {"raw_cleaned", "discarded"}


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
    # A unit whose raw data were deleted never runs again: its inputs are gone, and a run against what is left
    # - a re-created folder, a file that one deletion pass kept - would record a result for data that are not
    # the unit's. Its execution_allowed is left as it was, because the gate reads it. A part's raw tree is its
    # parent's, released by the parent's raw_release.
    status = str(manifest.get("status") or "")
    if status in RAW_RELEASED_STATUSES:
        blockers.append(
            f"This unit's raw data were deleted (status {status!r}); a unit whose raw data were released is never "
            "run again. Download it into a new lease to analyse it again."
        )
    elif str((manifest.get("raw_deletion") or {}).get("state") or "") in DELETION_RESUMABLE_STATES:
        blockers.append("A deletion of this unit's raw data has begun and not finished; the unit cannot run.")
    owner_path = _owner_manifest_path(manifest, manifest_path.resolve())
    if owner_path is not None:
        try:
            owner_release = (read_manifest(owner_path).get("raw_release") or {}) if owner_path.is_file() else {}
        except (OSError, ValueError):
            owner_release = {}
        if str(owner_release.get("state") or "") in {*DELETION_RESUMABLE_STATES, "deleted"}:
            blockers.append(
                "This part's raw data are its parent's, and the parent's raw tree has been released "
                f"(raw_release {owner_release.get('state')!r}); the part cannot run."
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
    aliases = console_alias_inputs(manifest)
    if admitted:
        requested = [
            Path(_unit_input_of(str(item.get("file_path") or ""), aliases)).resolve()
            for item in (state.get("files") or [])
            if str(item.get("file_path") or "").strip()
        ]
        outside = [str(path) for path in requested if path not in admitted]
        if outside:
            blockers.append(
                f"{len(outside)} input files are not among the files this unit's manifest admitted; "
                f"the first is {outside[0]}."
            )

    # An analysis CSV built from the input lineage wrote each input's acquisition type - the one an applied
    # campaign disposition decided, else its own header's - and recorded it on the input's lineage row
    # (repository_analysis_rows). A workflow that has since set another type for an input - one answer
    # written over every file - would deconvolute it as something its header did not say, and a header's
    # 'DIA' admits SWATH and AIF alike, so the check below cannot tell. The type the CSV wrote is the one
    # the input runs as.
    written = _written_acquisition_by_input(manifest)
    if written:
        rewritten = []
        for item in state.get("files") or []:
            path_text = str(item.get("file_path") or "").strip()
            expected = written.get(_file_key(_unit_input_of(path_text, aliases))) if path_text else None
            requested = str(item.get("acquisition_type") or "DDA").strip() or "DDA"
            if expected and requested != expected:
                rewritten.append(f"{Path(path_text).name} (written {expected}, run as {requested})")
        if rewritten:
            blockers.append(
                f"{len(rewritten)} input files would run with an acquisition type other than the one this "
                f"unit's analysis CSV wrote for them; the first is {rewritten[0]}."
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
    # The Console type a file's header alone gives (header_console_acquisition_type: DDA, SWATH or AIF) binds
    # every row of that file, whatever a disposition decided (user decision, 2026-10-06): a header that gives
    # one decides the file over any declaration, so a row that contradicts it is a disposition, or a workflow,
    # gone wrong, and the run would complete, validate and be wrong.
    header_types = _header_console_type_by_file(manifest)
    # The type an applied campaign disposition decided a file runs as - from its header, from the
    # repository's declaration where no header could be read or a DIA header left SWATH and AIF open, or DDA
    # for an MS1-only file folded into a DDA run - is the one type that file may run as. A file with no
    # decision is held to what its header alone admits, as before dispositions existed. All are recorded
    # against the input, so a row that reads it through a Console alias is looked up as that input.
    decided_types = _decided_acquisition_by_file(manifest)
    # An applied disposition decided before the header-first rule (Interactive 0.5.29, which records
    # declared_acquisition_source on every disposition) may run what that rule excludes: an MS1-only file
    # folded into the DDA run of a unit declared DIA, or a file whose header gives no acquisition mode, taken
    # at the declaration. It is decided again here from the same records, as classify_preflight would, and
    # changes nothing: a row that decision would not run refuses the run until the unit is decided again.
    legacy_refusals = _legacy_disposition_refusals(manifest, state.get("files") or [], aliases)
    blockers.extend(legacy_refusals)
    if header_modes or header_types or decided_types:
        disagreeing = []
        undecided_as = []
        for item in state.get("files") or []:
            path_text = str(item.get("file_path") or "").strip()
            if not path_text:
                continue
            input_key = _file_key(_unit_input_of(path_text, aliases))
            given = str(item.get("acquisition_type") or "").strip()
            requested = given or "DDA"
            header_type = header_types.get(input_key)
            if header_type and requested != header_type:
                disagreeing.append(f"{Path(path_text).name} (header gives {header_type}, run as {given or 'no type'})")
            decided = decided_types.get(input_key)
            if decided:
                if given != decided:
                    # A blank type is not the decided one even where the decision is DDA: the Console reads
                    # a blank, or any value it cannot parse, as DDA without a word.
                    undecided_as.append(f"{Path(path_text).name} (decided {decided}, run as {given or 'no type'})")
                continue
            header = header_modes.get(input_key)
            if not header_type and header and requested not in HEADER_ACQUISITION_TO_MSDIAL.get(header, {header}):
                disagreeing.append(f"{Path(path_text).name} (header {header}, run as {requested})")
        if disagreeing:
            blockers.append(
                f"{len(disagreeing)} input files would run with an acquisition type their raw header "
                f"contradicts; the first is {disagreeing[0]}."
            )
        if undecided_as:
            blockers.append(
                f"{len(undecided_as)} input files would run with an acquisition type other than the one this "
                f"unit's campaign disposition decided; the first is {undecided_as[0]}."
            )

    # An input a campaign disposition excluded - unreadable, ion mobility, out of scope - is not part of
    # the run that disposition allowed, whatever the input list still names, and whatever alias names it.
    excluded = _campaign_excluded_inputs(manifest)
    if excluded:
        named = []
        for item in state.get("files") or []:
            path_text = str(item.get("file_path") or "").strip()
            reason = excluded.get(_file_key(_unit_input_of(path_text, aliases))) if path_text else None
            if reason is not None:
                named.append(f"{Path(path_text).name} ({reason})")
        if named:
            blockers.append(
                f"{len(named)} input files were excluded by this unit's campaign disposition; the first is "
                f"{named[0]}."
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


# The extractor's RawAcquisitionMethod values (msrawdataworkbench RawDataMetadata.cs) other than Unknown.
# The targeted ones are outside the campaign's untargeted DDA/DIA scope.
HEADER_ACQUISITION_METHODS = frozenset({"FullScan", "DDA", "DIA", "AIF", "SIM", "SRM", "MRM", "PRM"})
OUT_OF_SCOPE_HEADER_METHODS = ("PRM", "SRM", "MRM", "SIM")


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


def _header_console_type_by_file(manifest: dict[str, Any]) -> dict[str, str]:
    """The Console type (DDA, SWATH or AIF) each inspected file's own header gives, keyed by resolved path.

    header_console_acquisition_type, which no disposition rewrites; a record written before it existed gives
    DDA or AIF as its acquisition_mode says, and nothing for DIA, whose scheme it did not record.
    """
    from .raw_metadata_preflight import entry_header_console

    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    result = {}
    for item in summary.get("per_file") or []:
        path_text = str(item.get("file") or "").strip() if isinstance(item, dict) else ""
        console = entry_header_console(item) if path_text else None
        if console == "AIF" and str(item.get("acquisition_mode") or "").strip() == "DIA":
            # A DIA header with no recorded isolation target (header_no_isolation): the extractor records
            # targets only from MS2 headers that carry a precursor m/z, so a vendor read without one gives none
            # for windowed DIA too. It binds nothing; the header's DIA admits SWATH or AIF, and a disposition's
            # decided type still binds.
            continue
        if console in {"DDA", "SWATH", "AIF"}:
            result[_file_key(path_text)] = console
    return result


# The bases of an assignment that a header did not settle (raw_metadata_preflight.decide_disposition): the
# declaration's SWATH or AIF, and AIF for a DIA header that recorded no isolation target, which is the extractor's
# reading of MS2 headers with no precursor m/z and not evidence of all-ion acquisition. A legacy row of another
# type for such a file is no contradiction of its header.
_UNSETTLED_ASSIGNMENT_BASES = {"declaration", "header_no_isolation"}


def _legacy_disposition_refusals(
    manifest: dict[str, Any], files: list[dict[str, Any]], aliases: dict[str, str]
) -> list[str]:
    """Blockers for the rows an applied pre-0.5.29 disposition runs and the header-first rule would not.

    Empty for a unit with no applied disposition, or one decided from 0.5.29 on (it records
    declared_acquisition_source). Otherwise the unit is decided again from its recorded preflight
    (_decide_recorded_preflight), in memory: the run is refused where that decision does not run the unit as
    one, excludes a row's file, or gives it another type on its header's word.
    """
    from .raw_metadata_preflight import file_key

    applied = _applied_disposition(manifest)
    if not applied or "declared_acquisition_source" in applied:
        return []
    again_hint = (
        "Prepare the unit again (msdial_prepare_repository_reanalysis with confirmed=true, or under its campaign "
        "approval): that decides it again from its recorded preflight, a finished unit included, and writes the "
        "analysis CSV from the new decision."
    )
    try:
        again = _decide_recorded_preflight(manifest)
    except Exception as error:  # A gate that cannot decide refuses rather than runs.
        return [
            "This unit's campaign disposition was decided before Interactive 0.5.29 took each file's acquisition "
            f"from its raw header first, and could not be decided again here ({type(error).__name__}). "
            + again_hint
        ]
    kind = str(again.get("disposition") or "")
    if kind != "run":
        return [
            "This unit's campaign disposition was decided before Interactive 0.5.29 took each file's acquisition "
            f"from its raw header first (user decision, 2026-10-06); decided again from the same records, the "
            f"unit would {kind} ({', '.join(again.get('reasons') or []) or 'no reason recorded'}), not run. "
            + again_hint
        ]
    assigned = again.get("assignments") or {}
    excluded_now = {
        file_key(str(item.get("path") or "")): str(item.get("reason") or "")
        for item in again.get("excluded_inputs") or []
        if isinstance(item, dict)
    }
    refused = []
    for item in files:
        path_text = str(item.get("file_path") or "").strip()
        if not path_text:
            continue
        key = file_key(_unit_input_of(path_text, aliases))
        given = str(item.get("acquisition_type") or "").strip() or "no type"
        assignment = assigned.get(key)
        if assignment is None:
            refused.append(f"{Path(path_text).name} (now {excluded_now.get(key) or 'not decided'})")
        elif (
            assignment.get("console_acquisition_type") != given
            and str(assignment.get("basis") or "") not in _UNSETTLED_ASSIGNMENT_BASES
        ):
            refused.append(
                f"{Path(path_text).name} (now {assignment.get('console_acquisition_type')} on the basis "
                f"{assignment.get('basis')}, run as {given})"
            )
    if not refused:
        return []
    return [
        f"{len(refused)} input files would run as a campaign disposition decided before Interactive 0.5.29 took "
        "each file's acquisition from its raw header first (user decision, 2026-10-06), and decided again from "
        f"the same records they would not; the first is {refused[0]}. " + again_hint
    ]


def _applied_disposition(manifest: dict[str, Any]) -> dict[str, Any]:
    disposition = manifest.get("campaign_disposition")
    return disposition if isinstance(disposition, dict) and disposition.get("applied") is True else {}


def _decided_acquisition_by_file(manifest: dict[str, Any]) -> dict[str, str]:
    """The Console acquisition type an applied campaign disposition decided for each file."""
    if not _applied_disposition(manifest):
        return {}
    summary = (manifest.get("raw_metadata_preflight") or {}).get("summary") or {}
    result = {}
    for item in summary.get("per_file") or []:
        path_text = str(item.get("file") or "").strip()
        decided = str(item.get("console_acquisition_type") or "").strip()
        if path_text and decided:
            result[_file_key(path_text)] = decided
    return result


def _campaign_excluded_inputs(manifest: dict[str, Any]) -> dict[str, str]:
    """The inputs an applied campaign disposition excluded, keyed by resolved path, with the reason."""
    return {
        _file_key(str(item.get("path"))): str(item.get("reason") or "")
        for item in _applied_disposition(manifest).get("excluded_inputs") or []
        if isinstance(item, dict) and str(item.get("path") or "").strip()
    }


def console_alias_inputs(manifest: dict[str, Any]) -> dict[str, str]:
    """The input each Console alias of the unit stands for, keyed by the alias's _file_key.

    The analysis-CSV builder (repository_analysis_rows) reads an input whose own path the Console cannot
    read through an ASCII-safe alias in raw\\console-aliases, and records it on the input's lineage row. A
    directory junction resolves to its input already; a hard link resolves to itself, so every check that
    asks whether a CSV row is one of the unit's inputs asks it of the input the alias stands for. An alias
    counts only while it still is that input (os.path.samefile). Empty for a unit with none.
    """
    result: dict[str, str] = {}
    for row in ((manifest or {}).get("input_lineage") or {}).get("rows") or []:
        alias = row.get("console_alias") if isinstance(row, dict) else None
        if not isinstance(alias, dict):
            continue
        link, target = str(alias.get("path") or ""), str(row.get("path") or "")
        try:
            same = bool(link and target) and os.path.samefile(link, target)
        except OSError:
            same = False
        if same:
            result[_file_key(link)] = target
    return result


def _unit_input_of(path_text: str, aliases: dict[str, str]) -> str:
    """The unit's input a CSV row names: itself, or the input its Console alias stands for."""
    return aliases.get(_file_key(path_text), path_text) if aliases else path_text


def _written_acquisition_by_input(manifest: dict[str, Any]) -> dict[str, str]:
    """The acquisition type the unit's lineage-built analysis CSV wrote for each input, by _file_key.

    record_analysis_csv puts it on the input's lineage row. Empty for a unit whose CSV was matched by
    name, or not yet written.
    """
    if ((manifest or {}).get("analysis_csv") or {}).get("status") != "written":
        return {}
    result: dict[str, str] = {}
    for row in ((manifest or {}).get("input_lineage") or {}).get("rows") or []:
        if not isinstance(row, dict):
            continue
        path_text, value = str(row.get("path") or "").strip(), str(row.get("acquisition_type") or "").strip()
        if path_text and value:
            result[_file_key(path_text)] = value
    return result


def _tree_size(root: Path) -> tuple[int, int]:
    """Return (file count, total bytes) under root, or (0, 0) when it is gone."""
    count, size, _elsewhere = _tree_census(root)
    return count, size


def _tree_census(root: Path) -> tuple[int, int, int]:
    """(file count, total bytes, bytes held elsewhere too) under root; (0, 0, 0) when it is gone.

    The third is the bytes of the hard-linked files with a name outside the tree, which deleting the tree
    does not free: a store lease's links to the download store's files (raw_storage).
    """
    if not root.is_dir():
        return 0, 0, 0
    count = size = 0
    # (st_dev, st_ino) of each multiply-linked file met: its link count, size and names met in the tree.
    linked: dict[tuple[int, int], list[int]] = {}
    for directory, directories, names in os.walk(root):
        # A directory junction is an alias the analysis CSV reads a folder through (console-aliases), not a
        # second copy of it, and Path.rglob and os.walk both descend into one. Its bytes are counted once,
        # where the folder is; so are a hard-linked file's, whichever of its names is met first.
        directories[:] = [
            name for name in directories
            if not (os.path.islink(os.path.join(directory, name)) or _is_junction(os.path.join(directory, name)))
        ]
        for name in names:
            path = Path(directory) / name
            if not path.is_file():
                continue
            status = path.stat()
            if status.st_nlink > 1:
                identity = (status.st_dev, status.st_ino)
                if identity in linked:
                    linked[identity][2] += 1
                    continue
                linked[identity] = [status.st_nlink, status.st_size, 1]
            count += 1
            size += status.st_size
    elsewhere = sum(file_size for links, file_size, met in linked.values() if met < links)
    return count, size, elsewhere


def _is_junction(path: str | Path) -> bool:
    isjunction = getattr(os.path, "isjunction", None)
    return bool(isjunction and isjunction(path))


# ---- deleting raw data ----------------------------------------------------------------------------------
#
# Three deletions, one way of making them. cleanup_download_lease deletes a validated unit's raw tree,
# discard_download_lease one that produced no validated output, and cleanup_split_parent a split parent's
# tree once every part has ended. Each takes either a person's confirmed=true or a campaign approval that
# covers boundary 5 for the unit and states delete_after_validated_output (campaign_authorization), and each:
#
# - refuses while a retained artifact lies under the tree it would delete;
# - holds a deletion lock in the unit's provenance (raw-deletion.lock), so two deletions never interleave;
# - writes its intent into the manifest before the first file goes (raw_deletion, or raw_release for a
#   split parent, state deleting), so a deletion a crash or a held file stopped is a record, not a guess;
# - removes the tree with download_store.unlink_tree: a multiply-linked file loses this name only, and its
#   attributes, which every other name of it shares, are never touched;
# - records what went (files, links, bytes) and what stayed (the retained artifacts, the failure artifacts,
#   anything unlink_tree had to keep), and resumes from a deleting or partial record when called again.
RAW_DELETION_SCHEMA = "msdial-raw-deletion.v1"
SPLIT_PARENT_RELEASE_SCHEMA = "msdial-split-parent-raw-release.v1"
FAILURE_ARTIFACTS_SCHEMA = "msdial-failure-artifacts.v1"
FAILURE_ARTIFACTS_DIRECTORY = "failure-artifacts"
FAILURE_VALIDATION_RECORD = "output-validation.json"
FAILURE_RUN_RECORD = "run-failure-record.json"
RAW_DELETION_LOCK = "raw-deletion"
# removed: a split parent's tree is gone and its parts are still being told; resumed like the others.
DELETION_RESUMABLE_STATES = ("deleting", "partial", "removed")
RAW_RELEASED_STATUSES = frozenset({"raw_cleaned", "discarded"})
# "A failed unit is retried twice, then its raw data are deleted" (the user's rule of 2026-09-30): a split
# part with this many recorded run failures has ended, and no longer holds its parent's raw data.
CAMPAIGN_RUN_ATTEMPTS = 3
_REPORTED_DELETION_ITEMS = 20


def plan_download_cleanup(manifest_path: Path) -> dict[str, Any]:
    """Describe exactly what a raw-data deletion would remove and what would survive it.

    Deleting downloaded raw data is the only irreversible operation in this pipeline, and the campaign
    rules require the person approving it to have seen three things first: the artifacts that will be
    retained, the paths that will be removed, and how much will be freed. This produces those three so a
    caller can present them; it changes nothing.

    A run whose finalisation could not move MS-DIAL's containers out of the raw tree holds the deletion
    (run_finalisation.raw_deletion_holds): the containers would go with it. So does a retained artifact
    that lies under the raw tree, whatever put it there.
    """
    from .run_finalisation import describe_holds, raw_deletion_holds

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    raw_root = Path(manifest.get("raw_directory", "")).resolve()
    workspace = Path(manifest.get("workspace", "")).resolve()
    retained = [Path(value) for value in manifest.get("retained_artifacts", [])]
    missing = [str(path) for path in retained if not os.path.exists(extended_path(path))]
    file_count, total_bytes, linked_bytes = _tree_census(raw_root)
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
    under = _paths_under([str(path) for path in retained], raw_root)
    if under:
        blockers.append(
            f"{len(under)} retained artifact(s) lie under the raw directory, so its deletion would delete them; "
            f"the first is {under[0]}."
        )
    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        blockers.append(
            "MS-DIAL containers are still in the raw directory, and its deletion would delete them: "
            + describe_holds(held) + "."
        )
    store = _store_release_preview(manifest, linked_bytes)
    return {
        **({"finalisation_holds": held} if held else {}),
        **({"download_store": store} if store else {}),
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
        **({"raw_deletion": manifest["raw_deletion"]} if isinstance(manifest.get("raw_deletion"), dict) else {}),
        "blockers": blockers,
        "ready_for_confirmation": not blockers and file_count > 0,
    }


def request_download_cleanup(manifest_path: Path) -> dict[str, Any]:
    """Record that the run's retention policy asked for deletion, and stop there.

    The retention policy chosen at download time records a wish. Whether the technical preconditions are
    met is a second, separate thing, recorded as cleanup_allowed. Whether to actually delete, having seen
    what goes and what stays, is a third, and it belongs to a person - or, in a campaign, to the runner
    acting under the recorded approval, which calls the cleanup itself. Running a job is not an occasion to
    make that third decision on anyone's behalf, so this marks the manifest and returns the plan.
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


def _paths_under(paths: Iterable[str], root: Path) -> list[str]:
    """The paths that are root itself or lie under it, compared as resolved, casefolded paths."""
    base = _file_key(str(root))
    return [
        str(path) for path in paths
        if str(path).strip() and (_file_key(str(path)) == base or _file_key(str(path)).startswith(base + os.sep))
    ]


def _is_split_parent(manifest: dict[str, Any]) -> bool:
    return manifest.get("status") == SPLIT_PARENT_STATUS or bool(manifest.get("split_into"))


def _owner_manifest_path(manifest: dict[str, Any], manifest_path: Path) -> Path | None:
    """The manifest of the unit whose raw tree this one reads, when that is another unit's: a split part's parent."""
    owner = str(manifest.get("raw_owned_by") or "").strip()
    if not owner:
        return None
    path = Path(owner).expanduser()
    return None if _file_key(str(path)) == _file_key(str(manifest_path)) else path


def _deletion_crossing(
    campaign_authorization_path: str | Path | None, manifest: dict[str, Any], entry_point: str
) -> dict[str, Any] | None:
    """The boundary-5 crossing a campaign approval gives this unit's deletion; None when none was passed.

    It must cover boundary 5 for the unit, or for the unit it was split from, and the unit's own manifest
    must record the approval's delete_after_validated_output: an approval that keeps raw data, or a unit
    that chose to keep its own, is a refusal (CampaignAuthorizationError), never a fallback to confirmed.
    """
    from .campaign_authorization import authorize, unit_identity

    if campaign_authorization_path is None or not str(campaign_authorization_path).strip():
        return None
    unit, parent = unit_identity(manifest)
    return authorize(
        campaign_authorization_path,
        unit,
        5,
        entry_point=entry_point,
        parent_unit_id=parent,
        raw_retention_policy=str(manifest.get("raw_retention_policy") or "keep"),
    )


@contextmanager
def _raw_deletion_lock(manifest_path: Path) -> Iterator[None]:
    """Hold the unit's deletion lock for one deletion; another one already under way is a refusal, not a wait.

    Its own lock file in the unit's provenance, not the manifest's writer lock: a deletion of tens of GB takes
    longer than any manifest writer waits, and the manifest is written several times while it runs.
    """
    lock = manifest_lock(Path(manifest_path).parent / RAW_DELETION_LOCK, timeout=0)
    try:
        lock.__enter__()
    except ManifestBusyError as error:
        raise ManifestBusyError(
            f"Another deletion of this unit's raw data holds {RAW_DELETION_LOCK}{MANIFEST_LOCK_SUFFIX}; nothing "
            "was deleted. Call again once it has finished: a deletion it left unfinished is resumed."
        ) from error
    try:
        yield
    finally:
        lock.__exit__(None, None, None)


def _remove_raw_tree(raw_root: Path) -> dict[str, Any]:
    """unlink_tree on the raw tree, by its extended-length path so a container deeper than MAX_PATH goes too."""
    target = Path(extended_path(raw_root, always=True))
    if not os.path.lexists(target):
        return {
            "root": str(raw_root), "removed_files": 0, "removed_links": 0, "removed_directories": 0,
            "removed_reparse_points": 0, "removed_bytes": 0, "kept": [], "kept_count": 0, "complete": True,
            "absent": True,
        }
    result = unlink_tree(target)
    result["root"] = plain_path(str(result.get("root") or target))
    result["kept"] = [{**item, "path": plain_path(str(item.get("path") or ""))} for item in result.get("kept") or []]
    return result


def _deletion_pass(removal: dict[str, Any], at: str) -> dict[str, Any]:
    return {
        "at": at,
        **{
            key: removal.get(key, 0)
            for key in ("removed_files", "removed_links", "removed_directories", "removed_reparse_points", "removed_bytes")
        },
        "kept_count": removal.get("kept_count", 0),
        "kept": list(removal.get("kept") or [])[:_REPORTED_DELETION_ITEMS],
        "complete": bool(removal.get("complete")),
        **({"tree_absent": True} if removal.get("absent") else {}),
    }


def _deletion_refusal(removal: dict[str, Any]) -> str:
    reasons = sorted({str(item.get("reason") or "not_removed") for item in removal.get("kept") or []})
    return (
        f"{removal.get('kept_count', 0)} entries of the raw tree could not be removed "
        f"({', '.join(reasons) or 'not removed'}); the deletion was recorded as partial, and it resumes where it "
        "stopped when called again."
    )


def _delete_raw_tree(
    manifest_path: Path,
    raw_root: Path,
    *,
    kind: str,
    authorized_by: dict[str, Any],
    kept: dict[str, Any],
    finish: Any,
    record_key: str = "raw_deletion",
    extra: dict[str, Any] | None = None,
    final_state: str = "deleted",
    guard: Any = None,
) -> dict[str, Any]:
    """Write the intent, remove the tree, and record what went and what stayed. Called under the deletion lock.

    ``finish(current)`` makes the unit's own change once the tree is gone (raw_cleaned, discarded), in the same
    write that records the tree as ``final_state``. ``guard(current)``, when given, raises in the intent's own
    write if the manifest is no longer the one this deletion was judged on, so nothing is deleted. A record left
    deleting, partial or removed by an earlier call of the same kind on the same tree is resumed, keeping when
    it was planned and how much it found.
    """
    planned_count, planned_bytes = _tree_size(raw_root)
    started = datetime.now(timezone.utc).isoformat()
    record: dict[str, Any] = {}

    def intent(current: dict[str, Any]) -> None:
        if guard is not None:
            guard(current)
        previous = current.get(record_key)
        resumed = (
            isinstance(previous, dict)
            and previous.get("state") in DELETION_RESUMABLE_STATES
            and previous.get("kind") == kind
            and _file_key(str(previous.get("target") or "")) == _file_key(str(raw_root))
        )
        if resumed:
            entry = dict(previous)
            entry["resumed_at"] = [*(previous.get("resumed_at") or []), started]
        else:
            entry = {
                "schema": SPLIT_PARENT_RELEASE_SCHEMA if record_key == "raw_release" else RAW_DELETION_SCHEMA,
                "kind": kind,
                "target": str(raw_root),
                "planned_at": started,
                "file_count": planned_count,
                "bytes": planned_bytes,
                "authorized_by": authorized_by,
                "kept": kept,
                "passes": [],
                **(extra or {}),
            }
        entry["state"] = "deleting"
        current[record_key] = entry
        record.clear()
        record.update(entry)

    update_manifest(manifest_path, intent)
    removal = _remove_raw_tree(raw_root)
    ended = datetime.now(timezone.utc).isoformat()
    step = _deletion_pass(removal, ended)
    if not removal["complete"]:
        remaining_count, remaining_bytes = _tree_size(raw_root)

        def partial(current: dict[str, Any]) -> None:
            entry = current.get(record_key) if isinstance(current.get(record_key), dict) else dict(record)
            entry["state"] = "partial"
            entry["passes"] = [*(entry.get("passes") or []), step]
            entry["remaining_file_count"] = remaining_count
            entry["remaining_bytes"] = remaining_bytes
            current[record_key] = entry
            record.clear()
            record.update(entry)

        update_manifest(manifest_path, partial)
        return {
            "deleted": False,
            "partial": True,
            "raw_directory": str(raw_root),
            "manifest_path": str(manifest_path),
            record_key: record,
            "blockers": [_deletion_refusal(removal)],
        }

    def done(current: dict[str, Any]) -> None:
        entry = current.get(record_key) if isinstance(current.get(record_key), dict) else dict(record)
        entry["state"] = final_state
        entry["removed_at" if final_state != "deleted" else "deleted_at"] = ended
        entry["passes"] = [*(entry.get("passes") or []), step]
        entry.pop("remaining_file_count", None)
        entry.pop("remaining_bytes", None)
        current[record_key] = entry
        finish(current)
        record.clear()
        record.update(entry)

    update_manifest(manifest_path, done)
    return {"deleted": True, "raw_directory": str(raw_root), "manifest_path": str(manifest_path), record_key: record}


def _resumable_deletion(manifest: dict[str, Any], kind: str, raw_root: Path, record_key: str = "raw_deletion") -> bool:
    previous = manifest.get(record_key)
    return (
        isinstance(previous, dict)
        and previous.get("state") in DELETION_RESUMABLE_STATES
        and previous.get("kind") == kind
        and _file_key(str(previous.get("target") or "")) == _file_key(str(raw_root))
    )


def _retained_kept(manifest: dict[str, Any]) -> dict[str, Any]:
    """What a cleanup leaves: the retained artifacts, by count and by one digest over their inventory."""
    inventory = [item for item in manifest.get("retained_artifact_inventory") or [] if isinstance(item, dict)]
    lines = "".join(
        f"{item.get('path', '')}\t{item.get('size_bytes', 0)}\t{item.get('sha256', '')}\n" for item in inventory
    )
    return {
        "retained_artifact_count": len(manifest.get("retained_artifacts") or []),
        "retained_artifact_inventory_sha256": hashlib.sha256(lines.encode("utf-8")).hexdigest(),
    }


def cleanup_download_lease(
    manifest_path: Path,
    confirmed: bool = False,
    *,
    campaign_authorization_path: str | Path | None = None,
    entry_point: str = "cleanup_download_lease",
) -> dict[str, Any]:
    """Delete a validated unit's raw tree, on a person's confirmation or under a campaign approval.

    With neither, this is the preview: plan_download_cleanup, deleting nothing. ``campaign_authorization_path``
    stands in for confirmed=true only when it covers boundary 5 for the unit (or the unit it was split from)
    and the approval and the unit both state delete_after_validated_output; it is refused otherwise. Under it
    every guard of the preview still applies, and nothing is recorded or deleted until the preview is ready;
    the crossing is then written into the manifest before the first file goes.

    A split parent is released by cleanup_split_parent, which this calls for one. A part's own raw directory
    is its parent's, so its cleanup is refused as before; its preview carries its parent's plan.

    A unit that holds download-store claims and was cleaned already, asked again under either, deletes
    nothing and records no crossing: its claims are released again (already_cleaned), which finishes a
    release that failed or stopped after the deletion and, under an approval, lets the store collect what
    the first release left (release_store_claims). Any other cleaned unit is refused as it always was.
    """
    from .run_finalisation import (
        BLOCKS_RAW_DELETION,
        FinalisationHeld,
        raw_deletion_holds,
        resolve_finalisation_holds,
    )

    manifest_path = manifest_path.resolve()
    if _is_split_parent(read_manifest(manifest_path)):
        return cleanup_split_parent(
            manifest_path,
            confirmed=confirmed,
            campaign_authorization_path=campaign_authorization_path,
            entry_point=entry_point,
        )
    if raw_deletion_holds(manifest_path):
        # The move the run's finalisation could not make is retried before the preview is drawn, so that
        # the preview describes what the deletion would really remove; what it moves joins the retained
        # inventory. Nothing here deletes.
        resolve_finalisation_holds(manifest_path)
        refresh_retained_artifacts(manifest_path)
    manifest = read_manifest(manifest_path)
    crossing = _deletion_crossing(campaign_authorization_path, manifest, entry_point)
    if not confirmed and crossing is None:
        # The preview carries the retained artifacts, the target and the size, because a confirmation
        # given without them is not an informed one. It used to return only the flag.
        plan = plan_download_cleanup(manifest_path)
        plan["deleted"] = False
        plan["confirmation_required"] = True
        owner = _owner_manifest_path(manifest, manifest_path)
        if owner is not None and owner.is_file():
            # A part's raw tree is its parent's, released with the parent's once every part has ended.
            try:
                plan["split_parent_plan"] = plan_split_parent_cleanup(owner)
            except (OSError, ValueError) as error:
                plan["split_parent_plan"] = {"error": str(error)}
        return plan
    raw_root = Path(str(manifest.get("raw_directory") or "")).resolve()
    with _raw_deletion_lock(manifest_path):
        manifest = read_manifest(manifest_path)
        finished = _cleanup_finished(manifest, raw_root)
        if finished is not None and _holds_store_claims(manifest):
            # Asked again once it was made, a cleanup deletes nothing; the store release it left unmade - one
            # that failed, or a stop between the deletion and the release - is made now, as a repeated discard
            # or split-parent release makes it. A unit with no store claims is refused as it always was.
            made = _cleanup_made(manifest_path, raw_root, finished, crossing)
            if crossing is not None:
                made["campaign_authorization"] = crossing
            return made
        resuming = _resumable_deletion(manifest, "cleanup", raw_root)
        if crossing is not None:
            plan = plan_download_cleanup(manifest_path)
            if plan["blockers"] or not (plan["deletion_file_count"] > 0 or resuming):
                owner = _owner_manifest_path(manifest, manifest_path)
                if owner is not None and owner.is_file():
                    try:
                        plan["split_parent_plan"] = plan_split_parent_cleanup(owner)
                    except (OSError, ValueError) as error:
                        plan["split_parent_plan"] = {"error": str(error)}
                return {
                    **plan,
                    "deleted": False,
                    "confirmation_required": False,
                    "campaign_authorization": crossing,
                    "message": "Nothing was deleted: the approval covers this unit, but the deletion is not ready.",
                }
        else:
            if manifest.get("status") not in CLEANUP_READY_STATUSES or not manifest.get("cleanup_allowed"):
                raise ValueError("Raw cleanup requires a completed/validated manifest with cleanup_allowed=true.")
            retained = [Path(value) for value in manifest.get("retained_artifacts", [])]
            if not retained or any(not os.path.exists(extended_path(path)) for path in retained):
                raise ValueError("Retained mzTab-M/provenance artifacts are missing; raw cleanup was refused.")
            workspace = Path(manifest["workspace"]).resolve()
            if raw_root.parent != workspace or raw_root.name != "raw":
                raise ValueError("Raw directory is outside the expected project workspace.")
            under = _paths_under([str(path) for path in retained], raw_root)
            if under:
                raise ValueError(
                    f"{len(under)} retained artifact(s) lie under the raw directory, and deleting it would delete "
                    f"them (the first is {under[0]}); raw cleanup was refused."
                )
            held = raw_deletion_holds(manifest_path, manifest)
            if held:
                raise FinalisationHeld(
                    BLOCKS_RAW_DELETION,
                    "MS-DIAL containers are still in the raw directory, and deleting it would delete them; raw "
                    "cleanup was refused", held,
                )
        if crossing is not None:
            record_campaign_authorization(manifest_path, crossing)
        cleaned_at = datetime.now(timezone.utc).isoformat()

        def change(current: dict[str, Any]) -> None:
            current["status"] = "raw_cleaned"
            current["raw_cleaned_at"] = cleaned_at

        result = _delete_raw_tree(
            manifest_path,
            raw_root,
            kind="cleanup",
            authorized_by=_authorized_by(crossing),
            kept=_retained_kept(manifest),
            finish=change,
        )
        if result.get("deleted"):
            # The tree's links are gone, so its claims go too, and the store collects what no unit holds.
            released = release_store_claims(manifest_path, "raw_cleaned", crossing)
            if released is not None:
                result["download_store"] = released
    if crossing is not None:
        result["campaign_authorization"] = crossing
    return result


def _cleanup_finished(manifest: dict[str, Any], raw_root: Path) -> dict[str, Any] | None:
    """The deletion record of this unit's cleanup when the cleanup was made and has finished, else None.

    Finished: the unit is raw_cleaned, its raw tree holds no file, and its raw_deletion is a cleanup of this
    tree, deleted; a unit cleaned before deletions were recorded carries none ({}).
    """
    if manifest.get("status") != "raw_cleaned" or _tree_size(raw_root)[0]:
        return None
    record = manifest.get("raw_deletion")
    if not isinstance(record, dict):
        return {}
    same = record.get("kind") == "cleanup" and _file_key(str(record.get("target") or "")) == _file_key(str(raw_root))
    return record if same and record.get("state") == "deleted" else None


def _holds_store_claims(manifest: dict[str, Any]) -> bool:
    """Whether the unit has claims, in any state, in a download store; a store that cannot be read counts,
    so that the release records why."""
    store = _unit_store(manifest)
    unit = str((manifest.get("project") or {}).get("analysis_unit_id") or "")
    if store is None or not unit:
        return False
    try:
        return bool(store.claims_for_unit(unit))
    except (StoreError, OSError):
        return True


def _cleanup_made(
    manifest_path: Path, raw_root: Path, record: dict[str, Any], crossing: dict[str, Any] | None
) -> dict[str, Any]:
    """A cleanup asked for again once it has finished: nothing is deleted, and no crossing recorded; the unit's
    store claims are released (release_store_claims, which releases nothing already released and keeps the
    first release's record unless this one changes something)."""
    result: dict[str, Any] = {
        "deleted": True,
        "already_cleaned": True,
        "raw_directory": str(raw_root),
        "manifest_path": str(manifest_path),
        **({"raw_deletion": record} if record else {}),
    }
    released = release_store_claims(manifest_path, "raw_cleaned", crossing)
    if released is not None:
        result["download_store"] = released
    return result


def _unit_store(manifest: dict[str, Any]) -> DownloadStore | None:
    """The accession download store that may hold claims of this unit's; None when there is none.

    The one its lease recorded (download_cache), else - a lease from before the store, or one that stopped
    before it recorded one - the store beside the unit's workspace, where a batch pre-claim may have put a
    claim of this unit's.
    """
    project = manifest.get("project") or {}
    cache = manifest.get("download_cache")
    try:
        if isinstance(cache, dict) and str(cache.get("workspace_root") or "").strip():
            return DownloadStore(cache["workspace_root"], cache.get("repository"), cache.get("accession"))
        unit = str(project.get("analysis_unit_id") or "")
        workspace = Path(str(manifest.get("workspace") or ""))
        accession_root = workspace.parent
        if (
            not unit
            or workspace.name != unit
            or accession_root.name != str(project.get("accession") or "")
            or accession_root.parent.name != str(project.get("repository") or "")
            or not (accession_root / STORE_DIRECTORY).is_dir()
        ):
            return None
        return DownloadStore(accession_root.parent.parent, project.get("repository"), project.get("accession"))
    except StoreError:
        return None


def release_store_claims(
    manifest_path: Path, reason: str, crossing: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Release every download-store claim this unit holds, now that its raw tree is gone. Never raises.

    Called by cleanup_download_lease, discard_download_lease and cleanup_split_parent once the tree they
    delete is gone, under the deletion they made: with the campaign approval that covered it (``crossing``,
    boundary 5), the store's GC then deletes each released object no live claim still holds
    (DownloadStore.gc: a pending pre-claim of a unit that has not run yet keeps it, as does a materialized
    claim of a unit still reading it); with a person's confirmation alone the claims are released and the
    store deletes nothing, because its objects are deleted only under a campaign approval.

    A release asked for again - a deletion repeated, or one that stopped, or failed, between its deletion and
    its release - releases nothing already released, and asks the GC again under the repeat's approval. The
    manifest's download_store_release is the record of the release that last changed something: released a
    claim, collected an object or a partial transfer, or followed one that failed. A repeat that changes
    nothing leaves that record as it is and is noted in its ``repeats``, so a person's confirmation repeated
    after a campaign's collection never replaces the evidence of that collection; a record a later release
    replaces is kept in its ``earlier``, without its claim list. Returns what this call did, with
    recorded_as release or repeat. None when no store holds claims of this unit's.
    """
    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError):
        return None
    store = _unit_store(manifest)
    unit = str((manifest.get("project") or {}).get("analysis_unit_id") or "")
    if store is None or not unit:
        return None
    at = datetime.now(timezone.utc).isoformat()
    authorization = str((crossing or {}).get("authorization_path") or "").strip() or None
    record: dict[str, Any] = {"schema": STORE_RELEASE_SCHEMA, "released_at": at, "reason": reason, "store": str(store.root)}
    changed = False
    try:
        claims = store.claims_for_unit(unit)
        if not claims:
            return None
        live = [claim for claim in claims if claim.get("state") in LIVE_CLAIM_STATES]
        released = store.release_unit(unit, reason, authorization=authorization)
    except (StoreError, OSError) as error:
        record["error"] = f"{type(error).__name__}: {error}"
    else:
        record["claims"] = [
            {key: claim.get(key) for key in ("url", "object_id", "state", "release_reason", "released_at")}
            for claim in released["released"]
        ]
        gc = released.get("gc")
        if gc is None:
            record["gc"] = {
                "authorized": False,
                "reason": (
                    "No campaign approval covered this deletion, and the store deletes nothing without one; "
                    "its objects stay for a collection under one."
                ),
            }
        else:
            record["gc"] = {
                key: gc.get(key)
                for key in ("authorized", "approval_id", "raw_retention_policy", "reason", "refusal_codes")
                if key in gc
            }
            record["gc"].update(
                collected=[
                    {key: item.get(key) for key in ("object_id", "state", "removed_bytes")}
                    for item in gc.get("collected") or []
                ],
                kept=list(gc.get("kept") or []),
                refused=list(gc.get("refused") or []),
                busy=list(gc.get("busy") or []),
                partials_removed=len(gc.get("partials_removed") or []),
            )
        changed = bool(live or record["gc"].get("collected") or record["gc"].get("partials_removed"))
    recorded_as: list[str] = []

    def change(current: dict[str, Any]) -> None:
        existing = current.get("download_store_release")
        existing = existing if isinstance(existing, dict) and existing.get("schema") == STORE_RELEASE_SCHEMA else None
        if existing is not None and not existing.get("error") and not changed:
            current["download_store_release"] = {
                **existing,
                "repeats": [*(existing.get("repeats") or []), _store_release_repeat(record)][-_STORE_RELEASE_HISTORY:],
            }
            recorded_as[:] = ["repeat"]
            return
        if existing is not None:
            replaced = {key: value for key, value in existing.items() if key not in {"claims", "earlier", "repeats"}}
            replaced.update(claim_count=len(existing.get("claims") or []), repeat_count=len(existing.get("repeats") or []))
            record["earlier"] = [*(existing.get("earlier") or []), replaced][-_STORE_RELEASE_HISTORY:]
        current["download_store_release"] = record
        recorded_as[:] = ["release"]

    try:
        update_manifest(manifest_path, change)
    except (OSError, ValueError):
        pass
    return {**record, "recorded_as": recorded_as[0] if recorded_as else "not_recorded"}


# How many repeats of a store release, and earlier records a release replaced, its record keeps.
_STORE_RELEASE_HISTORY = 10


def _store_release_repeat(record: dict[str, Any]) -> dict[str, Any]:
    """The note a release that changed nothing leaves in the standing record's repeats."""
    note: dict[str, Any] = {"at": record["released_at"], "reason": record["reason"]}
    if record.get("error"):
        note["error"] = record["error"]
        return note
    gc = record.get("gc") or {}
    note["gc"] = {
        "authorized": bool(gc.get("authorized")),
        **({"approval_id": gc["approval_id"]} if gc.get("approval_id") else {}),
        **({"refusal_codes": gc["refusal_codes"]} if gc.get("refusal_codes") else {}),
        **{key: len(gc.get(key) or []) for key in ("kept", "refused", "busy") if key in gc},
    }
    return note


def _store_release_preview(manifest: dict[str, Any], linked_bytes: int) -> dict[str, Any] | None:
    """What releasing this unit's store claims would free, and what the store keeps, for which units.

    For a deletion's preview. Of the bytes the deletion counts in the unit's tree (deletion_bytes), those of its
    links to the store's files are freed by no deletion of the tree (tree_bytes_kept_by_store): they are the
    store's, and go only when the store collects the object they belong to. That happens under a campaign
    approval that covers boundary 5 for every unit that released the object, once no other live claim holds
    it (bytes_collectable_after_release: an archive's extraction tree with it); a person's confirmation
    releases the claims and deletes no store object (store_bytes_freed_by_a_confirmation, 0). ``linked_bytes``
    is the third of _tree_census for the unit's raw tree, which the preview has walked. Changes nothing.
    None without claims.
    """
    store = _unit_store(manifest)
    unit = str((manifest.get("project") or {}).get("analysis_unit_id") or "")
    if store is None or not unit:
        return None
    try:
        claims = store.claims_for_unit(unit)
        if not claims:
            return None
        objects = []
        for claim in claims:
            object_id = claim.get("object_id") or (store.lookup(str(claim.get("url") or "")) or {}).get("object_id")
            entry = store.entry(object_id) if object_id else None
            others = sorted(
                {item["unit_id"] for item in store.live_claims(object_id) if item.get("unit_id") != unit}
            ) if object_id else []
            objects.append(
                {
                    "url": claim.get("url"),
                    "object_id": object_id,
                    "claim_state": claim.get("state"),
                    "object_state": (entry or {}).get("state"),
                    "size_bytes": (entry or {}).get("size_bytes"),
                    "tree_bytes": int(((entry or {}).get("tree") or {}).get("bytes") or 0),
                    "kept_for_units": others,
                }
            )
    except (StoreError, OSError) as error:
        return {"store": str(store.root), "error": f"{type(error).__name__}: {error}"}
    # One object per id: two claims of a unit (two URLs of the same bytes) name one object.
    distinct = {str(item["object_id"]): item for item in objects if item["object_id"]}
    collectable = sum(
        int(item.get("size_bytes") or 0) + item["tree_bytes"]
        for item in distinct.values()
        if not item["kept_for_units"] and item.get("object_state") == "ready"
    )
    return {
        "store": str(store.root),
        "claims": len(claims),
        "objects": objects,
        "tree_bytes_kept_by_store": linked_bytes,
        "store_bytes_freed_by_a_confirmation": 0,
        "bytes_collectable_after_release": collectable,
        "collection": (
            f"{linked_bytes} of the bytes in this unit's raw tree are links to the download store's files, which "
            "deleting the tree does not free. A store object is deleted only under a campaign approval that "
            "covers boundary 5 for every unit that released it, and only once no live claim holds it: under "
            f"such an approval {collectable} bytes would be collected with this release. A person's "
            "confirmation releases the claims and deletes no store object, so it frees none of them; a cleanup "
            "or discard of this unit repeated later under an approval that covers it collects what it left."
        ),
    }


def unit_workspaces(accession_root: Path) -> list[Path]:
    """The unit workspaces under one accession's directory: those with a run manifest, never _dl or _campaigns."""
    try:
        children = sorted(Path(accession_root).iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    return [
        child for child in children
        if child.is_dir()
        and not is_reserved_workspace_name(child.name)
        and (child / "provenance" / "run-manifest.json").is_file()
    ]


def download_store_status(
    workspace_root: str | Path, repository: str = "", accession: str = "", unit_id: str = ""
) -> dict[str, Any]:
    """A read-only picture of the accession download stores under a workspace root. Changes nothing.

    Per store: its objects (state, size, the units whose live claims keep each), its claims by state, its
    partial transfers and its lock holders (DownloadStore.summary), and, read from each unit's own manifest,
    the claims of units whose raw data are already released (raw_cleaned, discarded) yet still live, and
    the objects no live claim holds, which a collection under a campaign approval deletes only when it covers
    boundary 5 for every unit that released them: one released under a person's confirmation alone stays.
    ``repository`` and ``accession`` narrow it to one store, ``unit_id`` to the claims of one unit. Only the
    _dl directory of each accession is read; _dl and _campaigns are never taken for units.
    """
    from .user_settings import download_store_mode

    root = Path(workspace_root).expanduser().resolve()
    stores: list[dict[str, Any]] = []
    repositories = [root / repository] if repository else (
        sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []
    )
    for repository_root in repositories:
        if is_reserved_workspace_name(repository_root.name) or not repository_root.is_dir():
            continue
        accessions = [repository_root / accession] if accession else sorted(
            path for path in repository_root.iterdir() if path.is_dir()
        )
        for accession_root in accessions:
            if is_reserved_workspace_name(accession_root.name) or not (accession_root / STORE_DIRECTORY).is_dir():
                continue
            try:
                store = DownloadStore(root, repository_root.name, accession_root.name)
                summary = store.summary()
                claims = store.all_claims()
            except (StoreError, OSError) as error:
                stores.append({"store": str(accession_root / STORE_DIRECTORY), "error": f"{type(error).__name__}: {error}"})
                continue
            statuses: dict[str, str] = {}
            for workspace in unit_workspaces(accession_root):
                try:
                    recorded = read_manifest(workspace / "provenance" / "run-manifest.json")
                except (OSError, ValueError):
                    continue
                unit = str((recorded.get("project") or {}).get("analysis_unit_id") or workspace.name)
                statuses[unit] = str(recorded.get("status") or "")
            if unit_id:
                claims = [claim for claim in claims if claim.get("unit_id") == unit_id]
            by_unit: dict[str, list[dict[str, Any]]] = {}
            for claim in claims:
                by_unit.setdefault(str(claim.get("unit_id") or ""), []).append(
                    {key: claim.get(key) for key in ("url", "object_id", "state", "release_reason", "claimed_at", "released_at")}
                )
            stale = sorted(
                unit for unit, items in by_unit.items()
                if statuses.get(unit) in RAW_RELEASED_STATUSES and any(item["state"] in {"pending", "materialized"} for item in items)
            )
            stores.append(
                {
                    **summary,
                    "repository": repository_root.name,
                    "accession": accession_root.name,
                    "units": {
                        unit: {"status": statuses.get(unit) or "no_manifest", "claims": items}
                        for unit, items in sorted(by_unit.items())
                    },
                    "live_claims_of_released_units": stale,
                    "unclaimed_objects": [
                        item["object_id"] for item in summary["objects"]
                        if item.get("state") == "ready" and not item.get("live_claims")
                    ],
                }
            )
    return {
        "schema": "msdial-download-store-status.v1",
        "workspace_root": str(root),
        "store_mode": download_store_mode(),
        "store_count": len(stores),
        "stores": stores,
    }


def _store_release_reason(status: str) -> str:
    """The reason a discarded unit's claims are released with, by the status it was discarded from."""
    if status in {SKIPPED_BY_PREFLIGHT_STATUS, EXCLUDED_BY_PREFLIGHT_STATUS}:
        return "excluded"
    if status in {"run_failed", "validation_failed"}:
        return "failed_terminal"
    return "discarded"


def _authorized_by(crossing: dict[str, Any] | None) -> dict[str, Any]:
    if crossing is None:
        return {"kind": "confirmed"}
    return {
        "kind": "campaign_authorization",
        **{
            key: crossing.get(key)
            for key in ("approval_id", "campaign_id", "manifest_digest", "authorization_sha256", "covered_as", "boundary")
        },
    }


def _write_failure_record(path: Path, value: Any, minimal: Any, context: Any) -> dict[str, Any]:
    """Write one failure artifact, atomically and with no lock file beside it, and describe it.

    It lies under output, which is shared, so it is written as a shared artifact: ``value`` has been through
    ``context`` (sharing.SharingContext), and its bytes are scanned with the gate's patterns before they are
    written. Should anything still match, ``minimal`` - identifiers, codes and times, no free text - is written
    instead; should that match too, SharingError is raised and nothing is written.
    """
    from .sharing import SharingError

    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    reduced = bool(context.scan(path.name, data))
    if reduced:
        data = (json.dumps(minimal, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
        findings = context.scan(path.name, data)
        if findings:
            raise SharingError(path.name, findings)
    _replace_atomically(path, data)
    return {
        "path": str(path),
        "size_bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        **({"free_text_withheld": True} if reduced else {}),
    }


# A run attempt's fields a shared failure record carries: no output directory, backend host or process, and
# no stop detail, all of which are this machine's.
_SHARED_ATTEMPT_FIELDS = (
    "attempt_id", "attempt", "job_id", "kind", "started_at", "ended_at", "exit_code", "reason", "command_sha256",
    "timeout_seconds", "idle_timeout_seconds", "start_unrecorded",
)
_SHARED_CONSOLE_FIELDS = ("version", "binary_sha256", "assembly_sha256", "inventory_sha256", "provenance_status")


def _shared_run_failure(item: dict[str, Any], context: Any, *, free_text: bool = True) -> dict[str, Any]:
    """One run failure as a shared record carries it: its reason redacted, and no log lines.

    The log tail stays in the provenance manifest only. It is the Console's own output, and the Console prints a
    library's full location when it cannot open it ("MSP file was not found: <location>").
    """
    tail = item.get("log_tail") if isinstance(item.get("log_tail"), list) else []
    return {
        **({"reason": context.text(str(item.get("reason") or ""))} if free_text else {}),
        "exit_code": item.get("exit_code"),
        "recorded_at": item.get("recorded_at"),
        "log_tail_line_count": len(tail),
    }


def _shared_run_attempt(item: dict[str, Any], context: Any) -> dict[str, Any]:
    console = item.get("console") if isinstance(item.get("console"), dict) else {}
    shared = {key: item[key] for key in _SHARED_ATTEMPT_FIELDS if key in item}
    shared["console"] = {key: console[key] for key in _SHARED_CONSOLE_FIELDS if key in console}
    return context.view(shared)


def _validation_minimal(validation: dict[str, Any]) -> dict[str, Any]:
    return {
        "summary": validation.get("summary") or {},
        "files": [
            {
                "file_name": str(item.get("file_name") or ""),
                "status": item.get("status"),
                "error_count": len(item.get("errors") or []),
                "warning_count": len(item.get("warnings") or []),
            }
            for item in validation.get("files") or []
            if isinstance(item, dict)
        ],
    }


def _failure_artifacts(manifest_path: Path, manifest: dict[str, Any], output: Path, at: str) -> dict[str, Any] | None:
    """Keep what a failed run left as failure artifacts under its output: its mzTab-M, validated now, and its
    failure record. None when there is neither an mzTab-M nor a recorded failure.

    The mzTab-M stays where the run wrote it and is never deleted: it is evidence of how the run failed. Its
    validation and the unit's failure record are written beside the outputs, in failure-artifacts/, because
    the manifest that also holds them is no file anyone shares. Neither name holds "mztab", which is how
    find_mztab_files recognises an mzTab-M by name.

    Output is what is shared, so both are written as the unit's other shared artifacts are
    (sharing.SharingContext for its manifest; shared_path_policy declared): the raw directory becomes raw/,
    the workspace the root of a relative path, and any other location is withheld. The failure record carries
    each failure's reason, exit code and time and each attempt's identifiers, never a log line, a backend's
    host or an output directory: the full record stays in the provenance manifest.
    """
    from .mztab_validation import validate_mztab_files
    from .sharing import PATH_POLICY, SharingContext

    mztab = [Path(item) for item in _mztab_outputs(output)]
    failures = [item for item in manifest.get("run_failures") or [] if isinstance(item, dict)]
    if not mztab and not failures:
        return None
    context = SharingContext.for_state({"repository_run_manifest": str(manifest_path)}, run_directory=output)
    declared = {"shared_path_policy": PATH_POLICY, "shared_paths": context.describe()}
    directory = output / FAILURE_ARTIFACTS_DIRECTORY
    files: list[dict[str, Any]] = []
    summary: dict[str, Any] = {}
    for path in mztab:
        files.append({**_artifact_inventory(path), "kind": "mztab"})
    if mztab:
        validation = validate_mztab_files(mztab, output)
        summary = dict(validation.get("summary") or {})
        head = {"schema": "msdial-failed-run-output-validation.v1", "validated_at": at, **declared}
        files.append(
            {
                **_write_failure_record(
                    directory / FAILURE_VALIDATION_RECORD,
                    {**head, **context.view(validation)},
                    {**head, **_validation_minimal(validation)},
                    context,
                ),
                "kind": "mztab_validation",
            }
        )
    attempts = [item for item in manifest.get("run_attempts") or [] if isinstance(item, dict)][-10:]
    head = {
        "schema": "msdial-run-failure-record.v1",
        "recorded_at": at,
        **declared,
        "analysis_unit_id": str((manifest.get("project") or {}).get("analysis_unit_id") or ""),
        "status": manifest.get("status"),
        "full_record": "run_failures and run_attempts in the unit's provenance manifest, which is not shared",
    }
    files.append(
        {
            **_write_failure_record(
                directory / FAILURE_RUN_RECORD,
                {
                    **head,
                    "run_failures": [_shared_run_failure(item, context) for item in failures],
                    "run_attempts": [_shared_run_attempt(item, context) for item in attempts],
                },
                {
                    **head,
                    "run_failures": [_shared_run_failure(item, context, free_text=False) for item in failures],
                    "run_attempts": [
                        {key: item.get(key) for key in ("attempt_id", "attempt", "started_at", "ended_at", "exit_code")}
                        for item in attempts
                    ],
                },
                context,
            ),
            "kind": "run_failure_record",
        }
    )
    return {
        "schema": FAILURE_ARTIFACTS_SCHEMA,
        "recorded_at": at,
        "directory": str(directory),
        "mztab_validation_status": summary.get("status") if summary else None,
        "files": files,
    }


def _mztab_outputs(output: Path) -> list[str]:
    from .mztab_validation import find_mztab_files

    return [str(path) for path in find_mztab_files(output)] if str(output).strip() and output.is_dir() else []


def plan_download_discard(manifest_path: Path, *, authorized: bool = False) -> dict[str, Any]:
    """Describe what discarding a unit's raw data would remove and keep, and what refuses it. Changes nothing.

    ``authorized`` is whether a campaign approval covers the discard. Under one a failed unit whose output holds
    an mzTab-M - unvalidated, or invalid - may be discarded, its mzTab-M kept as a failure artifact; with
    confirmed=true alone such a unit is refused, as it always was.
    """
    from .run_finalisation import describe_holds, raw_deletion_holds

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    status = str(manifest.get("status") or "")
    raw_root = Path(str(manifest.get("raw_directory") or "")).resolve()
    workspace = Path(str(manifest.get("workspace") or "")).resolve()
    output = Path(str(manifest.get("output_directory") or ""))
    mztab = _mztab_outputs(output)
    file_count, total_bytes, linked_bytes = _tree_census(raw_root)
    blockers: list[str] = []
    if status in {"mztab_validated", "completed", "cleanup_pending_confirmation", "raw_cleaned"}:
        blockers.append("Validated/completed runs must use the normal cleanup command.")
    if status == "downloading":
        owner_state = lease_owner_state(manifest)
        if owner_state["state"] != "gone":
            blockers.append(
                "This unit's lease is recorded as still downloading, and its owner is not provably gone "
                f"({owner_state['reason']})."
            )
    attempt = _live_run_attempt_in(manifest)
    if attempt is not None:
        blockers.append(
            f"Run attempt {attempt.get('attempt_id') or '?'} of job {attempt.get('job_id') or 'unrecorded'} may still "
            "have its MS-DIAL Console reading the raw tree."
        )
    if mztab and not authorized:
        blockers.append("mzTab-M output exists; finalize the run before deleting raw data.")
    if raw_root.parent != workspace or raw_root.name != "raw":
        blockers.append("Raw directory is outside the expected project workspace.")
    kept = [str(item) for item in manifest.get("retained_artifacts") or []] + [
        str(item.get("path") or "") for item in ((manifest.get("failure_artifacts") or {}).get("files") or [])
        if isinstance(item, dict)
    ] + mztab
    under = _paths_under(kept, raw_root)
    if under:
        blockers.append(
            f"{len(under)} retained or failure artifact(s) lie under the raw directory, so its deletion would "
            f"delete them; the first is {under[0]}."
        )
    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        blockers.append(
            "finalisation_held [raw_deletion]: MS-DIAL containers a finished run could not move are still in the raw "
            "directory: " + describe_holds(held) + "."
        )
    store = _store_release_preview(manifest, linked_bytes)
    return {
        **({"finalisation_holds": held} if held else {}),
        **({"download_store": store} if store else {}),
        "manifest_path": str(manifest_path),
        "status": status,
        "retention_policy": manifest.get("raw_retention_policy"),
        "deletion_target": str(raw_root),
        "deletion_file_count": file_count,
        "deletion_bytes": total_bytes,
        "mztab_files": mztab,
        # Kept under output whatever the deletion does; under an approval written as failure artifacts first.
        "failure_artifacts_kept": bool(authorized and (mztab or manifest.get("run_failures"))),
        **({"raw_deletion": manifest["raw_deletion"]} if isinstance(manifest.get("raw_deletion"), dict) else {}),
        "blockers": blockers,
    }


def _discard_finished(manifest: dict[str, Any], raw_root: Path, *, authorized: bool) -> dict[str, Any] | None:
    """The deletion record of this unit's discard when the discard was made and has finished, else None.

    Finished: the unit is discarded, and its raw_deletion is of kind discard, for this tree, and deleted. Under
    an approval, a unit discarded before deletions were recorded, which carries no record, has finished too
    ({}). A deleting or partial record has not: that deletion is resumed. Without an approval such a legacy
    unit is discarded again, as it always was.
    """
    if manifest.get("status") != "discarded":
        return None
    record = manifest.get("raw_deletion")
    if isinstance(record, dict):
        same = record.get("kind") == "discard" and _file_key(str(record.get("target") or "")) == _file_key(str(raw_root))
        return record if same and record.get("state") == "deleted" else None
    return {} if authorized else None


def _discard_made(
    manifest_path: Path,
    manifest: dict[str, Any],
    raw_root: Path,
    record: dict[str, Any],
    crossing: dict[str, Any] | None,
) -> dict[str, Any]:
    """A discard asked for again once it has finished: the record it made, with nothing deleted or rewritten.

    A caller that repeats a discard - a runner that stopped after the discard and before it recorded the result -
    must not replace the first deletion's accounting with an empty one, rewrite the failure record as the
    discarded unit's, or record another crossing. A raw tree that holds files again, which no deletion of this
    unit made, is a refusal: the unit's own record says its tree is gone.
    """
    remaining, remaining_bytes = _tree_size(raw_root)
    if remaining:
        when = f"at {record['deleted_at']}" if record.get("deleted_at") else "earlier"
        message = (
            f"This unit's raw data were discarded ({when}), yet its raw directory holds {remaining} file(s) "
            f"({remaining_bytes} bytes) again, which no deletion of this unit made; nothing was deleted or recorded."
        )
        if crossing is None:
            raise ValueError(message)
        return {
            "deleted": False,
            "confirmation_required": False,
            "manifest_path": str(manifest_path),
            "status": manifest.get("status"),
            "deletion_target": str(raw_root),
            "deletion_file_count": remaining,
            "deletion_bytes": remaining_bytes,
            "campaign_authorization": crossing,
            "blockers": [message],
            "message": "Nothing was deleted: the approval covers this unit, but its discard was already made.",
        }
    kept = manifest.get("failure_artifacts")
    return {
        "deleted": True,
        "already_discarded": True,
        "raw_directory": str(raw_root),
        "manifest_path": str(manifest_path),
        **({"raw_deletion": record} if record else {}),
        **({"failure_artifacts": kept} if isinstance(kept, dict) else {}),
    }


def discard_download_lease(
    manifest_path: Path,
    confirmed: bool = False,
    *,
    campaign_authorization_path: str | Path | None = None,
    entry_point: str = "discard_download_lease",
) -> dict[str, Any]:
    """Delete the raw data of a unit that produced no validated output.

    With confirmed=false and no approval this is a preview that deletes nothing. ``campaign_authorization_path``
    stands in for confirmed=true when it covers boundary 5 for the unit, or the unit it was split from, and the
    approval and the unit both state delete_after_validated_output. Under it, and only under it, a failed unit
    whose output holds an mzTab-M - unvalidated, or invalid - is discarded too: that mzTab-M, its validation
    and the unit's failure record are kept as failure artifacts under output (failure_artifacts), and none of
    them is deleted. A refusal under an approval is returned as blockers, with nothing recorded or deleted;
    with confirmed=true it is raised, as it always was.

    A discard that has finished, asked for again, returns its record (already_discarded) and writes nothing;
    one that stopped part-way is resumed, keeping the failure artifacts it wrote first (_discard_finished).

    A split parent is released by cleanup_split_parent, which this calls for one. A part's raw data are its
    parent's: under an approval its discard records that the part has ended, deleting nothing, and its parent's
    tree goes with the parent's release.
    """
    from .mztab_validation import find_mztab_files

    manifest_path = manifest_path.resolve()
    manifest = read_manifest(manifest_path)
    if _is_split_parent(manifest):
        return cleanup_split_parent(
            manifest_path,
            confirmed=confirmed,
            campaign_authorization_path=campaign_authorization_path,
            entry_point=entry_point,
        )
    crossing = _deletion_crossing(campaign_authorization_path, manifest, entry_point)
    owner = _owner_manifest_path(manifest, manifest_path)
    if owner is not None and crossing is not None:
        return _discard_split_part(manifest_path, owner, crossing)
    downloading = manifest.get("status") == "downloading"
    owner_state = lease_owner_state(manifest) if downloading else None
    if not confirmed and crossing is None:
        preview = {
            "deleted": False,
            "confirmation_required": True,
            "manifest_path": str(manifest_path),
            "plan": plan_download_discard(manifest_path),
        }
        if owner_state is not None:
            preview["lease_owner_state"] = owner_state
        return preview
    from .run_finalisation import BLOCKS_RAW_DELETION, FinalisationHeld, raw_deletion_holds

    raw_root = Path(str(manifest.get("raw_directory") or "")).resolve()
    with _raw_deletion_lock(manifest_path):
        manifest = read_manifest(manifest_path)
        finished = _discard_finished(manifest, raw_root, authorized=crossing is not None)
        if finished is not None:
            made = _discard_made(manifest_path, manifest, raw_root, finished, crossing)
            if made.get("deleted"):
                # A discard stopped between its deletion and its release finishes the release; a repeated
                # one releases nothing more.
                previous = manifest.get("download_store_release") or {}
                released = release_store_claims(
                    manifest_path, str(previous.get("reason") or "discarded"), crossing
                )
                if released is not None:
                    made["download_store"] = released
            return made
        released_from = str(manifest.get("status") or "")
        downloading = manifest.get("status") == "downloading"
        owner_state = lease_owner_state(manifest) if downloading else None
        if crossing is not None:
            plan = plan_download_discard(manifest_path, authorized=True)
            if plan["blockers"]:
                return {
                    **plan,
                    "deleted": False,
                    "confirmation_required": False,
                    "campaign_authorization": crossing,
                    "message": "Nothing was deleted: the approval covers this unit, but the discard is refused.",
                }
        elif manifest.get("status") in {"mztab_validated", "completed", "raw_cleaned"}:
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
        if crossing is None:
            if find_mztab_files(output):
                raise ValueError("mzTab-M output exists; finalize the run before deleting raw data.")
            workspace = Path(manifest["workspace"]).resolve()
            if raw_root.parent != workspace or raw_root.name != "raw":
                raise ValueError("Raw directory is outside the expected project workspace.")
            held = raw_deletion_holds(manifest_path, manifest)
            if held:
                raise FinalisationHeld(
                    BLOCKS_RAW_DELETION,
                    "MS-DIAL containers a finished run could not move are still in the raw directory, and the "
                    "cleanup command retries the move; discard was refused", held,
                )
            attempt = _live_run_attempt_in(manifest)
            if attempt is not None:
                raise ValueError(
                    f"Run attempt {attempt.get('attempt_id') or '?'} of this unit may still have its MS-DIAL Console "
                    "reading the raw tree; discard was refused."
                )
            under = _paths_under([str(item) for item in manifest.get("retained_artifacts") or []], raw_root)
            if under:
                raise ValueError(
                    f"{len(under)} retained artifact(s) lie under the raw directory, and deleting it would delete "
                    f"them (the first is {under[0]}); discard was refused."
                )
        discarded_at = datetime.now(timezone.utc).isoformat()
        # A deletion an earlier call began and did not finish keeps the failure artifacts it wrote first: they
        # were written from the failed unit, and are not written again from one half deleted.
        kept_before = (
            manifest.get("failure_artifacts")
            if _resumable_deletion(manifest, "discard", raw_root) and isinstance(manifest.get("failure_artifacts"), dict)
            else None
        )
        failure_artifacts = (
            (kept_before or _failure_artifacts(manifest_path, manifest, output, discarded_at))
            if crossing is not None
            else None
        )
        if crossing is not None:
            record_campaign_authorization(manifest_path, crossing)
        if failure_artifacts is not None and kept_before is None:
            # Written before the first file goes, so a deletion that stops still names what it keeps.
            def keep(current: dict[str, Any]) -> None:
                current["failure_artifacts"] = failure_artifacts

            update_manifest(manifest_path, keep)
        mztab_kept = [item["path"] for item in (failure_artifacts or {}).get("files") or [] if item["kind"] == "mztab"]

        def same_lease(current: dict[str, Any]) -> None:
            if stale is not None and (current.get("lease_owner") or {}).get("lease_id") != (
                (stale["lease_owner"] or {}).get("lease_id")
            ):
                # Another lease took the workspace over after the check. Its record is not this discard's.
                raise ValueError(
                    "A new lease took this workspace over while its stale lease was being discarded; the new "
                    "lease's record was left as it is."
                )

        def change(current: dict[str, Any]) -> None:
            same_lease(current)
            current["status"] = "discarded"
            current["discarded_at"] = discarded_at
            if stale is not None:
                current["discard_reason"] = "The lease's process stopped before it recorded its inputs or its failure."
                current["stale_lease_discarded"] = {**stale, "discarded_at": discarded_at}
            elif mztab_kept:
                current["discard_reason"] = (
                    "The run produced no validated mzTab-M output. Its raw data were deleted under the campaign "
                    "approval; its mzTab-M, that mzTab-M's validation and its failure record are kept as failure "
                    "artifacts."
                )
            else:
                current["discard_reason"] = (
                    "Preflight/download was rejected before a retained mzTab-M result was produced."
                )

        kept: dict[str, Any] = {
            "retained_artifact_count": len(manifest.get("retained_artifacts") or []),
            "failure_artifact_count": len((failure_artifacts or {}).get("files") or []),
            "mztab_kept": mztab_kept,
        }
        result = _delete_raw_tree(
            manifest_path,
            raw_root,
            kind="discard",
            authorized_by=_authorized_by(crossing),
            kept=kept,
            finish=change,
            # Checked again before the first file goes, not only after the last.
            guard=same_lease,
        )
        if result.get("deleted"):
            released = release_store_claims(manifest_path, _store_release_reason(released_from), crossing)
            if released is not None:
                result["download_store"] = released
    if stale is not None and result.get("deleted"):
        result["stale_lease_discarded"] = True
    if failure_artifacts is not None:
        result["failure_artifacts"] = failure_artifacts
    if crossing is not None:
        result["campaign_authorization"] = crossing
    return result


def _discard_split_part(manifest_path: Path, parent_path: Path, crossing: dict[str, Any]) -> dict[str, Any]:
    """Record under an approval that a split part has ended without validated output. Deletes nothing.

    The part's raw tree is its parent's, read by every other part, and goes only with the parent's release
    (cleanup_split_parent). What a part's discard can do is say that this part no longer needs it - a run
    failed after its retries, a refusal before production - which is what lets the parent's release count it
    as ended. Its output is kept, an mzTab-M and its failure record among the failure artifacts.
    """
    with _raw_deletion_lock(manifest_path):
        manifest = read_manifest(manifest_path)
        status = str(manifest.get("status") or "")
        blockers: list[str] = []
        if status in {"mztab_validated", "completed", "cleanup_pending_confirmation", "raw_cleaned"}:
            blockers.append("Validated/completed runs must use the normal cleanup command.")
        attempt = _live_run_attempt_in(manifest)
        if attempt is not None:
            blockers.append(
                f"Run attempt {attempt.get('attempt_id') or '?'} of job {attempt.get('job_id') or 'unrecorded'} may "
                "still have its MS-DIAL Console reading the raw tree."
            )
        if blockers:
            return {
                "deleted": False,
                "confirmation_required": False,
                "manifest_path": str(manifest_path),
                "status": status,
                "campaign_authorization": crossing,
                "blockers": blockers,
                "message": "Nothing was recorded: the approval covers this part, but its discard is refused.",
            }
        if status == "discarded":
            ended = {"already_discarded": True}
        else:
            at = datetime.now(timezone.utc).isoformat()
            output = Path(str(manifest.get("output_directory") or ""))
            failure_artifacts = _failure_artifacts(manifest_path, manifest, output, at)
            record_campaign_authorization(manifest_path, crossing)

            def change(current: dict[str, Any]) -> None:
                current["status"] = "discarded"
                current["discarded_at"] = at
                current["discard_reason"] = (
                    "The part ended without validated output. Its raw data are its parent's, and are released "
                    "with the parent's once every part has ended."
                )
                current["raw_release_deferred_to"] = str(parent_path)
                if failure_artifacts is not None:
                    current["failure_artifacts"] = failure_artifacts

            update_manifest(manifest_path, change)
            ended = {**({"failure_artifacts": failure_artifacts} if failure_artifacts else {})}
    try:
        parent_plan = plan_split_parent_cleanup(parent_path)
    except (OSError, ValueError) as error:
        parent_plan = {"error": str(error)}
    return {
        "deleted": False,
        "part_ended": True,
        "manifest_path": str(manifest_path),
        "raw_release_deferred_to": str(parent_path),
        "split_parent_plan": parent_plan,
        "campaign_authorization": crossing,
        **ended,
    }


# ---- releasing a split parent's raw tree -----------------------------------------------------------------


def _part_end(part: dict[str, Any], parent_raw: Path) -> dict[str, Any]:
    """How one split part stands for its parent's raw release: its state, whether it has ended, what blocks.

    Ended means one of: validated (a cleanup-ready status, cleanup_allowed, no mzTab-M that failed, every
    retained artifact present and none under the parent's raw tree); released (raw_cleaned, by an earlier pass
    of this release); failed after its retries (CAMPAIGN_RUN_ATTEMPTS recorded run failures, which the post-run
    hook records for a Console that exits non-zero and for one that exits 0 without a validated mzTab-M alike);
    skipped or excluded (by its campaign disposition, or at the split); or discarded (its own discard under an
    approval). Anything else - not yet preflighted, prepared, running, failed with retries left - has not.
    """
    status = str(part.get("status") or "")
    blockers: list[str] = []
    failures = len([item for item in part.get("run_failures") or [] if isinstance(item, dict)])
    retained = [str(item) for item in part.get("retained_artifacts") or []]
    if status in CLEANUP_READY_STATUSES or status == "raw_cleaned":
        state = "released" if status == "raw_cleaned" else "validated"
        if state == "validated" and not part.get("cleanup_allowed"):
            blockers.append("cleanup_allowed is not true")
        failed = ((part.get("mztab_validation") or {}).get("summary") or {}).get("failed")
        if failed:
            blockers.append(f"{failed} of its mzTab-M file(s) failed validation")
        if not retained:
            blockers.append("no retained artifacts are recorded")
        missing = [item for item in retained if not os.path.exists(extended_path(item))]
        if missing:
            blockers.append(f"{len(missing)} of its retained artifacts are missing")
    elif status == SKIPPED_BY_PREFLIGHT_STATUS:
        state = "skipped"
    elif status == EXCLUDED_BY_PREFLIGHT_STATUS:
        state = "excluded"
    elif status == "discarded":
        state = "discarded"
    elif status in {"run_failed", "validation_failed"} and failures >= CAMPAIGN_RUN_ATTEMPTS:
        state = "failed"
    else:
        state = "pending"
        blockers.append(
            f"it is {status or 'unrecorded'!r}"
            + (
                f", with {failures} of {CAMPAIGN_RUN_ATTEMPTS} runs failed"
                if status in {"run_failed", "validation_failed"}
                else ""
            )
            + ", and has not ended"
        )
    kept = retained + [
        str(item.get("path") or "") for item in ((part.get("failure_artifacts") or {}).get("files") or [])
        if isinstance(item, dict)
    ]
    under = _paths_under(kept, parent_raw)
    if under:
        blockers.append(f"{len(under)} of its retained or failure artifacts lie under the parent's raw tree")
    attempt = _live_run_attempt_in(part)
    if attempt is not None:
        blockers.append(f"run attempt {attempt.get('attempt_id') or '?'} may still have its MS-DIAL Console running")
    mztab = [
        {"path": str(item.get("path") or ""), "sha256": str(item.get("sha256") or "")}
        for item in part.get("retained_artifact_inventory") or []
        if isinstance(item, dict) and str(item.get("path") or "").casefold().endswith(".mztab")
    ]
    return {
        "state": state,
        "ended": state != "pending",
        "blockers": blockers,
        "status": status,
        "run_failures": failures,
        "retained_artifact_count": len(retained),
        "mztab_files": mztab,
    }


def plan_split_parent_cleanup(manifest_path: Path) -> dict[str, Any]:
    """Describe the release of a split parent's raw tree, and what still refuses it. Changes nothing.

    WHAT A SPLIT PARENT IS. The unit that downloaded the data and owns <workspace>\\raw, which every part split
    from it reads in place. It never runs, and its tree can go only once no part will read it again. Released
    when every condition holds:

    1. The parent is split_by_acquisition, names its parts, and its raw directory is <workspace>\\raw.
    2. Its raw_retention_policy is delete_after_validated_output: a unit kept under keep (MTBLS2207) is never
       released.
    3. Every part's manifest is readable and names this parent, its manifest and its raw directory, as SPL-1
       reads them.
    4. The parts' inputs, with the inputs a campaign disposition excluded, are the parent's inputs, each once.
    5. Every part has ended (_part_end): validated, released, failed after its retries, skipped, excluded or
       discarded; none has a Console that may still be running, and none keeps an artifact under the tree.
    6. No finalisation hold stands on the parent or a part (run_finalisation.raw_deletion_holds).

    The release is ``released`` when some part's outputs validated, else ``discarded``. The authorization - a
    person's confirmed=true or a campaign approval covering boundary 5 for the parent - is cleanup_split_parent's
    to check; this reports what the deletion would remove and each part's state.
    """
    from .run_finalisation import describe_holds, raw_deletion_holds

    manifest_path = Path(manifest_path).resolve()
    manifest = read_manifest(manifest_path)
    project = manifest.get("project") or {}
    raw_root = Path(str(manifest.get("raw_directory") or "")).resolve()
    workspace = Path(str(manifest.get("workspace") or "")).resolve()
    blockers: list[str] = []
    if manifest.get("status") != SPLIT_PARENT_STATUS:
        blockers.append(
            f"The unit is {manifest.get('status') or 'unrecorded'!r}, not {SPLIT_PARENT_STATUS!r}; only a split "
            "parent's raw tree is released here."
        )
    listed = [item for item in manifest.get("split_into") or [] if isinstance(item, dict)]
    if not listed:
        blockers.append("The parent names no parts.")
    if not str(manifest.get("raw_directory") or "").strip() or raw_root.parent != workspace or raw_root.name != "raw":
        blockers.append("The raw directory is not the expected 'raw' folder inside the parent's workspace.")
    policy = str(manifest.get("raw_retention_policy") or "keep")
    if policy != "delete_after_validated_output":
        blockers.append(f"The parent's raw retention policy is {policy!r}; its raw data are kept.")

    parts: list[dict[str, Any]] = []
    claimed: list[str] = []
    for item in listed:
        part_path = Path(str(item.get("manifest_path") or "")).expanduser()
        unit_id = str(item.get("analysis_unit_id") or "")
        try:
            part = read_manifest(part_path)
        except (OSError, ValueError) as error:
            blockers.append(f"The manifest of part {unit_id or part_path} could not be read: {error}.")
            parts.append({"analysis_unit_id": unit_id, "manifest_path": str(part_path), "state": "unreadable", "ended": False})
            continue
        problems = []
        if _file_key(str((part.get("split_from") or {}).get("manifest_path") or "")) != _file_key(str(manifest_path)):
            problems.append("its split_from names another parent")
        if _file_key(str(part.get("raw_owned_by") or "")) != _file_key(str(manifest_path)):
            problems.append("its raw_owned_by names another unit")
        if _file_key(str(part.get("raw_directory") or "")) != _file_key(str(raw_root)):
            problems.append("its raw_directory is not the parent's")
        end = _part_end(part, raw_root)
        for problem in problems + end["blockers"]:
            blockers.append(f"Part {unit_id or part_path}: {problem}.")
        claimed.extend(str(path) for path in part.get("input_candidates") or [] if str(path).strip())
        parts.append({"analysis_unit_id": unit_id, "manifest_path": str(part_path), **end})

    excluded = {
        *(_file_key(str(item.get("path"))) for item in manifest.get("split_excluded_inputs") or []
          if isinstance(item, dict) and str(item.get("path") or "").strip()),
        *_campaign_excluded_inputs(manifest),
    }
    candidates = sorted(_file_key(str(item)) for item in manifest.get("input_candidates") or [] if str(item).strip())
    accounted = sorted([_file_key(item) for item in claimed] + [key for key in excluded if key in set(candidates)])
    if listed and accounted != candidates:
        blockers.append(
            f"The parts hold {len(claimed)} inputs and the excluded ones {len(accounted) - len(claimed)}, against the "
            f"parent's {len(candidates)} input candidates: they are not the parent's inputs, each once."
        )
    held = raw_deletion_holds(manifest_path, manifest)
    if held:
        blockers.append(
            "MS-DIAL containers are still in the raw directory, and its deletion would delete them: "
            + describe_holds(held) + "."
        )
    file_count, total_bytes, linked_bytes = _tree_census(raw_root)
    release = manifest.get("raw_release") if isinstance(manifest.get("raw_release"), dict) else {}
    already = release.get("state") == "deleted" and not raw_root.exists()
    # The parent's claims stand for every part (parts never claim), and are released with its tree.
    store = _store_release_preview(manifest, linked_bytes)
    return {
        **({"download_store": store} if store else {}),
        "manifest_path": str(manifest_path),
        "analysis_unit_id": str(project.get("analysis_unit_id") or ""),
        "status": manifest.get("status"),
        "retention_policy": policy,
        "deletion_target": str(raw_root),
        "deletion_file_count": file_count,
        "deletion_bytes": total_bytes,
        "kind": "released" if any(part.get("state") in {"validated", "released"} for part in parts) else "discarded",
        "parts": parts,
        **({"raw_release": release} if release else {}),
        "already_released": already,
        "resumable": release.get("state") in DELETION_RESUMABLE_STATES,
        "blockers": blockers,
        "ready": not blockers and not already,
    }


def record_split_parent_pending(part_manifest_path: Path) -> dict[str, Any] | None:
    """Record on a part's parent the release plan as it stands now; delete nothing. None for an unsplit unit.

    What the backend's post-run hook does once a part's run is finalised. The release itself is the caller's
    to make - in a campaign, the runner's, under its approval - so a run job never deletes raw data, and two
    processes never race to release one tree.
    """
    part_manifest_path = Path(part_manifest_path).resolve()
    part = read_manifest(part_manifest_path)
    parent_text = str((part.get("split_from") or {}).get("manifest_path") or "").strip()
    if not parent_text or not Path(parent_text).is_file():
        return None
    parent_path = Path(parent_text).resolve()
    plan = plan_split_parent_cleanup(parent_path)
    pending = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "requested_by": str((part.get("project") or {}).get("analysis_unit_id") or ""),
        "ready": plan["ready"],
        "kind": plan["kind"],
        "parts": [
            {"analysis_unit_id": item.get("analysis_unit_id"), "state": item.get("state")} for item in plan["parts"]
        ],
        "blockers": plan["blockers"][:_REPORTED_DELETION_ITEMS],
        "deleted": False,
    }

    def change(current: dict[str, Any]) -> None:
        current["raw_release_pending"] = pending

    update_manifest(parent_path, change)
    return {**plan, "pending_recorded": True}


def cleanup_split_parent(
    manifest_path: Path,
    confirmed: bool = False,
    *,
    campaign_authorization_path: str | Path | None = None,
    entry_point: str = "cleanup_split_parent",
) -> dict[str, Any]:
    """Release a split parent's raw tree once every part has ended (plan_split_parent_cleanup).

    With neither confirmed=true nor ``campaign_authorization_path`` this is the plan, deleting nothing. An
    approval must cover boundary 5 for the parent and state delete_after_validated_output, as the parent does.
    Under the deletion lock the plan is drawn again, and any blocker stops it with nothing recorded. Then:

    1. the crossing, if any, is recorded on the parent, and raw_release (msdial-split-parent-raw-release.v1) is
       written with state deleting: the kind, the target, its files and bytes, and each part as it ended;
    2. the tree is removed (download_store.unlink_tree), and raw_release becomes state removed. A tree that
       could not be removed whole is state partial with what remains, and a later call resumes it;
    3. each validated part becomes raw_cleaned with raw_released_by naming the parent; every other part keeps
       its status and gains raw_released_at and raw_released_by;
    4. raw_release becomes state deleted. The parent stays split_by_acquisition, as SPL-1 requires.

    Idempotent: a release already recorded deleted returns it again, deleting nothing.
    """
    from .run_finalisation import raw_deletion_holds, resolve_finalisation_holds

    manifest_path = Path(manifest_path).resolve()
    manifest = read_manifest(manifest_path)
    if not _is_split_parent(manifest):
        raise ValueError(
            "This unit was not split; its raw data are deleted by cleanup_download_lease or discard_download_lease."
        )
    if raw_deletion_holds(manifest_path):
        # A part's containers that could not be moved out of the tree are moved now, if they can be; what moves
        # joins that part's retained inventory.
        resolve_finalisation_holds(manifest_path)
        for item in manifest.get("split_into") or []:
            if isinstance(item, dict) and Path(str(item.get("manifest_path") or "")).is_file():
                refresh_retained_artifacts(Path(str(item["manifest_path"])))
    crossing = _deletion_crossing(campaign_authorization_path, manifest, entry_point)
    plan = plan_split_parent_cleanup(manifest_path)
    if plan["already_released"] and not confirmed and crossing is None:
        return {**plan, "deleted": True, "already_released": True}
    if not confirmed and crossing is None:
        return {**plan, "deleted": False, "confirmation_required": True}
    raw_root = Path(plan["deletion_target"])
    with _raw_deletion_lock(manifest_path):
        plan = plan_split_parent_cleanup(manifest_path)
        if plan["already_released"]:
            return _split_parent_released_again(manifest_path, plan, crossing)
        if plan["blockers"]:
            return {
                **plan,
                "deleted": False,
                "confirmation_required": False,
                **({"campaign_authorization": crossing} if crossing else {}),
                "message": "Nothing was deleted: the split parent's raw tree is not ready to be released.",
            }
        if crossing is not None:
            record_campaign_authorization(manifest_path, crossing)
        parts = [
            {
                "analysis_unit_id": part.get("analysis_unit_id"),
                "manifest_path": part.get("manifest_path"),
                "state": part.get("state"),
                "status_at_release": part.get("status"),
                "mztab_files": part.get("mztab_files") or [],
                "retained_artifact_count": part.get("retained_artifact_count", 0),
            }
            for part in plan["parts"]
        ]
        removal = _delete_raw_tree(
            manifest_path,
            raw_root,
            kind=plan["kind"],
            authorized_by=_authorized_by(crossing),
            kept={"parts": len(parts), "retained_artifact_count": sum(item["retained_artifact_count"] for item in parts)},
            finish=lambda current: None,
            record_key="raw_release",
            extra={"parts": parts},
            final_state="removed",
        )
        if not removal["deleted"]:
            return {**plan, **removal, **({"campaign_authorization": crossing} if crossing else {})}
        released_at = str(removal["raw_release"].get("removed_at") or datetime.now(timezone.utc).isoformat())
        for part in plan["parts"]:

            def change(current: dict[str, Any], state: str = str(part.get("state") or "")) -> None:
                if state == "validated" and current.get("status") in CLEANUP_READY_STATUSES:
                    current["status"] = "raw_cleaned"
                    current["raw_cleaned_at"] = released_at
                elif current.get("status") != "raw_cleaned":
                    current.setdefault("raw_released_at", released_at)
                current.setdefault("raw_released_by", str(manifest_path))

            update_manifest(Path(str(part["manifest_path"])), change)

        def finish(current: dict[str, Any]) -> None:
            # Only now, with every part told: a release stopped between the two is resumed as removed.
            release = dict(current.get("raw_release") or {})
            release["state"] = "deleted"
            release["deleted_at"] = datetime.now(timezone.utc).isoformat()
            current["raw_release"] = release
            current["raw_cleaned_at"] = released_at
            current.pop("raw_release_pending", None)

        update_manifest(manifest_path, finish)
        # The parent's claims stood for every part; with the tree gone they go, and the store collects what
        # no other unit holds.
        released = release_store_claims(
            manifest_path, "raw_cleaned" if removal["raw_release"].get("kind") == "released" else "discarded", crossing
        )
    result = {
        **plan_split_parent_cleanup(manifest_path),
        "deleted": True,
        "kind": removal["raw_release"].get("kind"),
        "raw_directory": str(raw_root),
        "raw_release": read_manifest(manifest_path).get("raw_release"),
    }
    if released is not None:
        result["download_store"] = released
    if crossing is not None:
        result["campaign_authorization"] = crossing
    return result


def _split_parent_released_again(
    manifest_path: Path, plan: dict[str, Any], crossing: dict[str, Any] | None
) -> dict[str, Any]:
    """A release asked for again: nothing is deleted, and a claim release a stop left unmade is made now."""
    result = {**plan, "deleted": True, "already_released": True}
    recorded = read_manifest(manifest_path)
    reason = "raw_cleaned" if (recorded.get("raw_release") or {}).get("kind") == "released" else "discarded"
    released = release_store_claims(manifest_path, reason, crossing)
    if released is not None:
        result["download_store"] = released
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
            "container": str(item.get("container") or ""),
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
    project: RepositoryProject, *, analysis_only: bool = False, convertible: bool = False
) -> list[str]:
    """The unit's listed file names, relative and casefolded, as the data root holds them.

    A listed per-sample container archive also stands for the container it unpacks to
    (archives.container_alias): FILES/X.raw.zip admits X.raw, in the directory it was listed in,
    which is where the lease expands it. With analysis_only, only what MS-DIAL opens: files of an
    analysis role, and the container of a listed archive of one, or of an archive listed for this unit
    alone (raw_archive). A container named by an archive every unit of a study lists
    (shared_raw_archive) is admitted by this unit's sample names, never by the archive.

    With convertible as well, the mzXML the unit lists for analysis (requires_conversion, packed or not)
    counts as what MS-DIAL opens: the convert stage writes it as the mzML that is opened.
    """
    names: list[str] = []
    for item in project.files:
        if not item.name:
            continue
        name = _safe_relative_name(item.name).as_posix().casefold()
        analysis = item.role in ANALYSIS_INPUT_ROLES and not requires_msdial_conversion(item.name)
        source = convertible and item.role in {*ANALYSIS_INPUT_ROLES, "requires_conversion"} and is_convertible_input(
            item.name
        )
        if not analysis_only or analysis or source:
            names.append(name)
        alias = _container_alias_path(name)
        if alias and (
            not analysis_only
            or ((analysis or item.role == "raw_archive") and not requires_msdial_conversion(alias))
            or (source and is_convertible_input(alias))
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


def _admitted_by_unit(
    path: Path,
    data_root: Path,
    listed: Iterable[str],
    sample_names: tuple[set[str], set[str]],
    archive_samples: dict[str, str],
) -> bool:
    """Whether an undeclared unit admits a file by itself: listed, named by its samples, or out of an archive
    one of its samples names (archive_samples, by _file_key)."""
    return (
        _path_matches_allowlist(path, data_root, listed)
        or _matches_sample_file_names(path, sample_names)
        or (bool(archive_samples) and _file_key(str(path)) in archive_samples)
    )


def _readable_inputs_admitted(
    readable: list[str], data_root: Path, project: RepositoryProject, archive_samples: dict[str, str]
) -> set[str]:
    """The readable inputs an undeclared unit admits by itself, by _file_key, as its attribute stage will.

    The convert stage pairs an mzXML with these whatever their folders (_encoding_choices): such a file is
    the unit's input in any case, so converting its sample's mzXML as well would give the sample two
    inputs. Empty for a declared unit, whose inputs are the Catalog's, one per sample, by path.
    """
    if not project.analysis_unit_id or declared_analysis_inputs(project):
        return set()
    listed = set(_project_allowlist(project, analysis_only=True))
    sample_names = _sample_file_names(project)
    return {
        _file_key(item)
        for item in readable
        if not requires_msdial_conversion(Path(item).name)
        and _admitted_by_unit(Path(item), data_root, listed, sample_names, archive_samples)
    }


def _filter_inputs_by_project_allowlist(
    inputs: list[str],
    data_root: Path,
    project: RepositoryProject,
    *,
    archive_samples: dict[str, str] | None = None,
    archive_extractions: list[dict[str, Any]] | None = None,
    stands_for: dict[str, str] | None = None,
    set_aside: list[str] | None = None,
) -> list[str]:
    """The inputs that are this unit's: listed, named by its samples, or out of an archive one names.

    archive_samples is _archive_sample_attribution's: the files, and outermost .d/.raw folders, that
    came out of an archive exactly one of this unit's samples names (X.zip). Without it, only names
    are matched, as they always were. archive_extractions is the lease's extraction records, which say
    where a declared archived container really is (declared_archive_containers).

    stands_for maps an input, by _file_key, to the mzXML it stands for (the convert stage's
    stands_for): an mzML converted from it, or a readable encoding of its sample chosen over it. Such an
    input is this unit's when it, or the mzXML, is. set_aside is the mzXML of this unit the convert stage
    excluded, whose conversion failed or whose scans contradict the declared polarity: a declared input
    they are is not missing, and a unit left with nothing else selects nothing rather than raising, so the
    lease can record why.
    """
    if not project.analysis_unit_id:
        return inputs
    stands_for = stands_for or {}
    declared = declared_analysis_inputs(project)
    if declared:
        return _select_declared_inputs(
            inputs,
            data_root,
            project,
            declared,
            archive_samples=archive_samples,
            archive_extractions=archive_extractions,
            stands_for=stands_for,
            set_aside=set_aside,
        )
    archive_samples = archive_samples or {}
    allowed = _project_allowlist(project, analysis_only=True)
    sources = set(_project_allowlist(project, analysis_only=True, convertible=True)) if stands_for else set()
    sample_names = _sample_file_names(project)
    if not allowed and not sources and not any(sample_names):
        raise ValueError(
            f"Analysis unit {project.analysis_unit_id} names no analysis input: it declares no "
            f"file of role {sorted(ANALYSIS_INPUT_ROLES)} and none of its samples names a raw "
            "file, so there is nothing to attribute the downloaded data to."
        )

    def admitted(path: Path, listed: Iterable[str]) -> bool:
        return _admitted_by_unit(path, data_root, listed, sample_names, archive_samples)

    # Either source is sufficient on its own, and both are scoped to THIS unit: the declared
    # analysis-input files, and the file names this unit's samples claim. An archive shared with
    # another unit used to admit all of both units' contents through the archive's own name; the
    # sample names admit only this unit's.
    allowed_set = set(allowed)
    selected = [
        item
        for item in inputs
        if not requires_msdial_conversion(Path(item).name)
        and (
            admitted(Path(item), allowed_set)
            or (_file_key(item) in stands_for and admitted(Path(stands_for[_file_key(item)]), sources))
        )
    ]
    if not selected and not set_aside:
        raise ValueError(
            f"Downloaded content did not contain an MS-DIAL input listed for analysis unit "
            f"{project.analysis_unit_id}. Refusing to fall back to accession-level inputs."
        )
    return selected


# The kinds of Catalog analysis input MS-DIAL opens. A declared directory (a Bruker NMR experiment folder)
# is a sample but never an input, and the Catalog blocks a unit that holds one.
DECLARED_INPUT_KINDS = frozenset({"file", "vendor_folder", "archived_container"})


def declared_analysis_inputs(project: RepositoryProject) -> dict[str, dict[str, Any]]:
    """The Catalog's declared analysis inputs that MS-DIAL opens, by path relative to the data root.

    Keyed as the lease places them: '/'-separated, casefolded, less a leading FILES/ (_safe_relative_name).
    Empty when the Catalog declared none. An input it says must be converted first is kept only when it is
    an mzXML (conversion_target mzML): the mzML the convert stage writes from it stands for it, and is
    matched to it through the mzXML it was converted from (match_declared_inputs, stands_for). One nothing
    converts (mzData) is left out: no reader opens it, and its unit is excluded.
    """
    result: dict[str, dict[str, Any]] = {}
    for entry in project.analysis_inputs or []:
        if not isinstance(entry, dict):
            continue
        if entry.get("requires_conversion") and not (
            str(entry.get("conversion_target") or "").casefold() == "mzml"
            or is_convertible_input(str(entry.get("path") or ""))
        ):
            continue
        if str(entry.get("kind") or "") not in DECLARED_INPUT_KINDS:
            continue
        try:
            key = _safe_relative_name(str(entry.get("path") or "")).as_posix().casefold()
        except ValueError:
            continue
        result[key] = entry
    return result


def declared_archive_containers(
    project: RepositoryProject,
    declared: dict[str, dict[str, Any]],
    archive_extractions: list[dict[str, Any]] | None,
) -> dict[str, str]:
    """Where each declared archived container really is, relative to the data root: {that path: its key}.

    The Catalog names an archived container after its archive - raw/A.d for raw/A.d.zip - because the
    listing shows nothing inside it. archives.destination_rule extracts a container as it was packed, so
    A.d.zip holding B.d is B.d (container_rooted_other_name): renaming it would decide which sample it is.
    The extraction record of the container's own archive, found by the archive's download URL, says where
    it went (container_path). Only paths that differ from the declared one are returned, keyed as
    declared_analysis_inputs keys them.
    """
    archives_of: dict[str, str] = {}
    for key, entry in declared.items():
        if str(entry.get("kind") or "") != "archived_container" or not str(entry.get("archive") or "").strip():
            continue
        try:
            archives_of[_safe_relative_name(str(entry["archive"])).as_posix().casefold()] = key
        except ValueError:
            continue
    if not archives_of or not archive_extractions:
        return {}
    by_url: dict[str, str] = {}
    for item in project.files:
        try:
            name = _safe_relative_name(item.name).as_posix().casefold()
        except ValueError:
            continue
        if name in archives_of and item.url:
            by_url.setdefault(item.url, archives_of[name])
    result: dict[str, str] = {}
    for record in archive_extractions:
        if not isinstance(record, dict):
            continue
        key = by_url.get(str(record.get("source_url") or ""))
        container = str(record.get("container_path") or "").replace("\\", "/").strip("/") if key else ""
        if not container:
            continue
        try:
            located = _safe_relative_name(container).as_posix().casefold()
        except ValueError:
            continue
        if located != key:
            result.setdefault(located, key)
    return result


def match_declared_inputs(
    inputs: list[str],
    data_root: Path,
    declared: dict[str, dict[str, Any]],
    *,
    containers: dict[str, str] | None = None,
    samples: dict[str, str] | None = None,
    stands_for: dict[str, str] | None = None,
) -> dict[str, list[str]]:
    """Which inputs are which declared analysis input: {declared key: [inputs]}, every key present.

    The lease's allow-list and the analysis-CSV builder both ask this, and must answer it alike. An input
    is matched by its path relative to the data root, the most specific form first (_allowlist_forms),
    so it is one declared input's whichever others share a tail; else at the path its archive's
    extraction record gives a declared archived container (``containers``, declared_archive_containers).
    An input still unmatched is an archived container's when ``samples`` (by _file_key) gives it the one
    sample that container is declared for and no input was matched to it: the attribution through the
    archive that sample names (_archive_sample_attribution, or a lineage row's sample_id), which is how
    such a container was admitted before the Catalog declared its inputs.

    An input ``stands_for`` names (by _file_key) is matched as the mzXML it stands for wherever it is not
    matched as itself: the mzML converted from raw\\data\\X.mzXML lies in raw\\converted, outside the data
    root, and is the declared FILES/X.mzXML's input.
    """
    containers = containers or {}
    samples = samples or {}
    stands_for = stands_for or {}
    found: dict[str, list[str]] = {key: [] for key in declared}
    unmatched: list[str] = []

    def match(path_text: str) -> str | None:
        relative = _relative_to_data_root(Path(path_text), data_root)
        if relative is None:
            return None
        forms = sorted(_allowlist_forms(relative), key=len, reverse=True)
        key = next((form for form in forms if form in found), None)
        if key is None:
            key = next((containers[form] for form in forms if form in containers), None)
        return key

    for item in inputs:
        source = stands_for.get(_file_key(item))
        key = match(item)
        if key is None and source:
            key = match(source)
        if key is None:
            if _relative_to_data_root(Path(item), data_root) is not None or source:
                unmatched.append(item)
        else:
            found[key].append(item)
    waiting: dict[str, str] = {}
    for key, entry in declared.items():
        sample = str(entry.get("sample_id") or "").strip()
        if str(entry.get("kind") or "") == "archived_container" and sample and not found[key]:
            waiting[sample] = key
    for item in unmatched if waiting and samples else []:
        source = stands_for.get(_file_key(item))
        sample = samples.get(_file_key(item)) or (samples.get(_file_key(source)) if source else "")
        key = waiting.get(str(sample or "").strip())
        if key is not None:
            found[key].append(item)
    return found


def _select_declared_inputs(
    inputs: list[str],
    data_root: Path,
    project: RepositoryProject,
    declared: dict[str, dict[str, Any]],
    *,
    archive_samples: dict[str, str] | None = None,
    archive_extractions: list[dict[str, Any]] | None = None,
    stands_for: dict[str, str] | None = None,
    set_aside: list[str] | None = None,
) -> list[str]:
    """The inputs that are this unit's declared analysis inputs, matched by path, every one of them.

    An input converted from a declared mzXML is matched through it (``stands_for``); a declared mzXML the
    convert stage excluded (``set_aside``: its conversion failed, or its scans contradict the declared
    polarity) is accounted for by that exclusion, recorded with it, not missing.

    WHY BY PATH, AND WHY EVERY ONE. Before the Catalog declared its inputs, an input was admitted when a
    listed name or a sample's file name matched its basename. A Waters folder matched neither - its
    listing names the files inside it - so a MetaboBank folder unit raised "did not contain an MS-DIAL
    input" after its whole download; a Bruker folder passed only because its 0-byte marker file X.d/X.d
    happens to carry the folder's name. The declared path is what the Catalog attributed to a sample row,
    so it is what is matched, and a declared input that is not on disk stops the lease by name rather than
    leaving a unit that silently lost a sample. A basename match is not a fallback here: it is how an input
    another unit shares a name with would get in. An archived container is matched where its archive put
    it, and through the archive its sample names (match_declared_inputs).
    """
    containers = declared_archive_containers(project, declared, archive_extractions)
    found = match_declared_inputs(
        inputs,
        data_root,
        declared,
        containers=containers,
        samples=archive_samples,
        stands_for=stands_for,
    )
    failed = (
        {key for key, matched in match_declared_inputs(
            list(set_aside), data_root, declared, containers=containers, samples=archive_samples
        ).items() if matched}
        if set_aside
        else set()
    )
    missing = [
        str(declared[key].get("path") or key) for key, matched in found.items() if not matched and key not in failed
    ]
    doubled = [
        f"{declared[key].get('path') or key} ({len(matched)} inputs)"
        for key, matched in found.items()
        if len(matched) > 1
    ]
    if missing or doubled:
        problems = []
        if missing:
            problems.append(
                f"{len(missing)} of its {len(declared)} declared analysis inputs are not in the download: "
                + ", ".join(sorted(missing, key=str.casefold)[:5])
                + (f", and {len(missing) - 5} more" if len(missing) > 5 else "")
            )
        if doubled:
            problems.append("declared inputs found more than once: " + ", ".join(doubled[:5]))
        raise ValueError(
            f"Analysis unit {project.analysis_unit_id} declares its analysis inputs, and "
            + "; ".join(problems)
            + ". Refusing to run it without them."
        )
    selected = {item for matched in found.values() for item in matched}
    return [item for item in inputs if item in selected]



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
    paths: list[str],
    data_root: Path,
    project: RepositoryProject,
    *,
    archive_samples: dict[str, str] | None = None,
) -> list[str]:
    """The extracted files that are this unit's: listed, inside a listed folder, or its samples'.

    Matching the listed names alone gave [] for every Workbench unit, whose only listed file is the
    study archive: extracted_files said nothing came out for the unit although its inputs had. A file
    that came out of an archive one of its samples names (archive_samples) is that sample's.
    """
    if not project.analysis_unit_id:
        return paths
    allowed = set(_project_allowlist(project))
    sample_names = _sample_file_names(project)
    archive_samples = archive_samples or {}
    return [
        item
        for item in paths
        if _path_matches_allowlist(Path(item), data_root, allowed, allow_directory_descendants=True)
        or _is_sample_member(Path(item), data_root, sample_names)
        or (bool(archive_samples) and _file_key(item) in archive_samples)
    ]


def _archive_sample_attribution(
    project: RepositoryProject,
    archive_extractions: list[dict[str, Any]],
    extracted_members: dict[str, dict[str, Any]],
    data_root: Path,
) -> dict[str, str]:
    """The sample each extracted file belongs to through the archive it came out of, by _file_key.

    WHY. A sample may name a plain per-sample archive, X.zip, with no container suffix. In the
    2026-09-22 Catalog snapshot 4,540 Workbench sample names in 76 units name one inside the study
    archive (the MoTrPAC ST0026xx studies, and ST000322, ST000329 and ST000354 of the declared pool),
    and 8,874 MetaboLights sample names in 159 units name one the unit downloads itself. It expands
    to X/X.d, or to X.d at the data root, and no rule that matches names - exact, extensionless stem,
    container alias - matches X.d to X.zip, so every such unit failed at the attribute stage. The
    members listing says which archive each file came out of, so that is what is matched instead: an
    archive exactly one of this unit's samples names, by its file name, gives that sample every file
    that came out of it, through any archive nested inside it.

    What is not given: an archive more than one sample names (a batch zip, or one archive every sample
    names), since which of its files is whose is not known; and a downloaded archive the unit lists as
    a study archive (raw_archive, shared_raw_archive), whose contents are other samples' and other
    units' too, whatever a sample calls it. The result holds each file, and the outermost .d/.raw
    folder holding it, under the sample's id; a folder two samples' archives both wrote into is left
    out.
    """
    named: dict[str, set[str]] = {}
    for sample in project.sample_metadata or []:
        raw = PurePosixPath(str((sample or {}).get("raw_file") or "").replace("\\", "/")).name.casefold()
        sample_id = str((sample or {}).get("sample_id") or "").strip()
        if raw and sample_id and archives.is_archive_name(raw):
            named.setdefault(raw, set()).add(sample_id)
    if not named or not extracted_members:
        return {}

    def owner(label: str) -> str:
        samples = named.get(PurePosixPath(label.replace("\\", "/")).name.casefold()) or set()
        return next(iter(samples)) if len(samples) == 1 else ""

    study_archives = {item.url for item in project.files if item.role in ARCHIVE_ROLES}
    by_label: dict[tuple[int, str], str] = {}
    for index, record in enumerate(archive_extractions):
        outer_label = str(record.get("archive_name") or "")
        outer = "" if str(record.get("source_url") or "") in study_archives else owner(outer_label)
        if outer:
            by_label[(index, outer_label)] = outer
        pending = [(nested, outer) for nested in record.get("nested") or [] if isinstance(nested, dict)]
        while pending:
            nested, inherited = pending.pop()
            label = str(nested.get("archive_path") or "")
            sample = owner(label) or inherited
            if sample:
                by_label[(index, label)] = sample
            pending.extend(
                (child, sample) for child in nested.get("nested") or [] if isinstance(child, dict)
            )
    if not by_label:
        return {}

    data_key = _file_key(str(data_root))
    attributed: dict[str, str] = {}
    folders: dict[str, set[str]] = {}
    for key, listed in extracted_members.items():
        sample = by_label.get((int(listed.get("extraction", -1)), str(listed.get("archive") or "")))
        if not sample:
            continue
        attributed[key] = sample
        outermost = ""
        for parent in Path(key).parents:
            parent_key = str(parent)
            if parent_key == data_key or len(parent_key) <= len(data_key):
                break
            if parent.suffix in FOLDER_INPUT_SUFFIXES:
                outermost = parent_key
        if outermost:
            folders.setdefault(outermost, set()).add(sample)
    for folder, samples in folders.items():
        if len(samples) == 1:
            attributed[folder] = next(iter(samples))
    return attributed


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


# The role the Catalog gives a file inside a vendor folder it lists member by member (Catalog 0.6.0). A
# member is downloaded, checksummed and covered, and is never an input or a sample: its folder is.
VENDOR_FOLDER_MEMBER_ROLE = "vendor_folder_member"
# What a partial transfer leaves beside its file (RepositoryHttpClient.download).
PARTIAL_TRANSFER_SUFFIXES = (".part", ".part.json")


def _vendor_container_of(name: str) -> str:
    """The outermost .d/.raw folder a listed file lies in, as the Catalog's container_of reads it; ''."""
    parts = [part for part in str(name or "").replace("\\", "/").split("/") if part]
    for index, part in enumerate(parts[:-1]):
        lowered = part.casefold()
        if lowered.endswith(archives.FOLDER_CONTAINER_SUFFIXES) and lowered not in archives.FOLDER_CONTAINER_SUFFIXES:
            return "/".join(parts[: index + 1])
    return ""


def verify_container_completeness(data_root: Path, project: RepositoryProject) -> dict[str, Any]:
    """Whether every vendor folder the unit lists member by member arrived whole. Raises when one did not.

    WHY. A folder is one data file, so a folder short of one member is a damaged data file, and its reader
    either fails deep inside a vendor SDK or reads what is there: a Waters .raw missing one _FUNCnnn.DAT has
    silently lost an acquisition function. The download compares each member's published MD5 as it lands,
    but a member published without one, a folder whose members were never all fetched, or a workspace a
    lease reused is not caught by that. So each declared folder must be a directory holding every member
    it lists, each of its listed size, and no partial transfer (.part, .part.json).

    A file inside the folder that the listing does not name is recorded, not refused: one a vendor reader
    writes into the folder it reads (msdial_app.reader_created: Bruker's baf2sql writes analysis.sqlite, and
    its journal while it writes, into a BAF .d) as reader_created_files, and anything else as unlisted, for
    a reader of the record. A listed member of a name such a reader writes may have been rewritten by it, so
    its size is not held to the listing. Only files of role vendor_folder_member are checked, so a unit
    whose listing names no folder member (every handoff before Catalog 0.6.0, an archive unit) records that
    nothing was required.
    """
    members: dict[str, list[RepositoryFile]] = {}
    spelled: dict[str, str] = {}
    for item in project.files:
        if item.role != VENDOR_FOLDER_MEMBER_ROLE:
            continue
        container = str(item.container or "").strip() or _vendor_container_of(item.name)
        try:
            key = _safe_relative_name(container).as_posix().casefold() if container else ""
        except ValueError:
            key = ""
        if not key:
            raise ValueError(f"Folder member {item.name} names no vendor folder it lies in.")
        members.setdefault(key, []).append(item)
        spelled.setdefault(key, container)
    if not members:
        return {"required": False, "containers": 0, "members": 0}

    # Where each folder is: an archive may have put the listed tree under a folder of its own (MB-POST's
    # project tar), so folders are found by the same relative forms the allow-list reads, a folder at
    # exactly the listed path before one that only ends in it.
    on_disk: dict[str, Path] = {}
    for directory, directories, _names in os.walk(data_root):
        for name in list(directories):
            if not name.casefold().endswith(archives.FOLDER_CONTAINER_SUFFIXES):
                continue
            path = Path(directory) / name
            relative = _relative_to_data_root(path, data_root)
            if relative is None:
                continue
            if relative in members:
                on_disk[relative] = path
                continue
            for form in _allowlist_forms(relative):
                if form in members:
                    on_disk.setdefault(form, path)

    problems: list[str] = []
    reader_created: list[str] = []
    unlisted: list[str] = []
    for key, listed in sorted(members.items()):
        container = spelled[key]
        folder = on_disk.get(key)
        if folder is None:
            problems.append(f"{container}: the folder is not in the download")
            continue
        offset = len(_safe_relative_name(container).as_posix()) + 1
        expected: dict[str, tuple[str, RepositoryFile]] = {}
        for item in listed:
            relative = _safe_relative_name(item.name).as_posix()[offset:]
            expected[relative.casefold()] = (relative, item)
        # The names a reader may write into this folder: none unless its rule applies (a BAF .d).
        reader_names = reader_created_names(folder)
        missing: list[str] = []
        resized: list[str] = []
        for relative, item in expected.values():
            path = folder / relative
            if not path.is_file():
                missing.append(relative)
            elif item.size_bytes and path.stat().st_size != item.size_bytes and relative.casefold() not in reader_names:
                resized.append(f"{relative} ({path.stat().st_size} of {item.size_bytes} bytes)")
        partial: list[str] = []
        not_listed: list[str] = []
        for directory, _directories, names in os.walk(folder):
            for name in names:
                relative = (Path(directory) / name).relative_to(folder).as_posix()
                if name.casefold().endswith(PARTIAL_TRANSFER_SUFFIXES):
                    partial.append(relative)
                elif relative.casefold() not in expected:
                    not_listed.append(relative)
        own, created = container_members(folder, not_listed, [relative for relative, _item in expected.values()])
        unlisted.extend(f"{container}/{relative}" for relative in own)
        reader_created.extend(f"{container}/{relative}" for relative in created)
        for label, found in (("missing", missing), ("of another size", resized), ("partial", partial)):
            if found:
                problems.append(
                    f"{container}: {len(found)} member(s) {label}: " + ", ".join(sorted(found)[:3])
                )
    record: dict[str, Any] = {
        "required": True,
        "containers": len(members),
        "members": sum(len(items) for items in members.values()),
        "complete": not problems,
        "reader_created_files": sorted(reader_created),
        "unlisted_file_count": len(unlisted),
        "unlisted_files": sorted(unlisted)[:20],
    }
    if problems:
        raise ValueError(
            f"{len(problems)} of the unit's {len(members)} vendor folders did not arrive whole: "
            + "; ".join(problems[:5])
            + (f"; and {len(problems) - 5} more" if len(problems) > 5 else "")
            + "."
        )
    return record


def _find_msdial_inputs(root: Path) -> list[str]:
    paths = sorted(root.rglob("*"))
    vendor_roots = {
        path.resolve()
        for path in paths
        if path.is_dir() and path.suffix.casefold() in {".d", ".raw"}
    }
    # OUTERMOST ROOTS ONLY. A folder is one data file, whatever it holds: an Agilent or Bruker .d may keep
    # a directory whose name also ends in .d (a method, a calibration), and it is part of its folder, read
    # by that folder's reader, never an input of its own.
    outermost = {
        path for path in vendor_roots if not any(parent in vendor_roots for parent in path.parents)
    }
    result = [str(path) for path in sorted(outermost)]
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
    # PRM is one of the extractor's RawAcquisitionMethod values (RawDataMetadata.cs) and was the one this
    # list left out, so a PRM unit summarised as "Unknown" - the word for "no header spoke" - rather than
    # as the targeted acquisition it is.
    acquisition_values = {value for value in methods if value in HEADER_ACQUISITION_METHODS}
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
    from .raw_metadata_preflight import header_console_acquisition_type

    per_file = []
    for item in records:
        source = item.get("source") or {}
        acquisition = item.get("acquisition") or {}
        method = acquisition.get("method")
        ms_levels = acquisition.get("msLevels")
        if isinstance(ms_levels, dict):
            ms_levels = ms_levels.get("value")
        levels = {int(level) for level in ms_levels if str(level).isdigit()} if isinstance(ms_levels, list) else set()
        targets = acquisition.get("isolationWindowTargets")
        energies = acquisition.get("collisionEnergies")
        header_mode = _metadata_value(item, "acquisition", "method")
        console, console_basis = header_console_acquisition_type(header_mode, targets)
        per_file.append(
            {
                "file": str(source.get("filePath") or source.get("fileName") or ""),
                "acquisition_mode": header_mode,
                "confidence": method.get("confidence") if isinstance(method, dict) else None,
                "evidence": method.get("evidence") if isinstance(method, dict) else None,
                # VendorHeader or SpectrumStatistics: a mode read from a vendor's method record and one
                # inferred from how the spectra recur are different kinds of evidence, and an artifact
                # must not call the second "header-confirmed".
                "method_source": str(method.get("source") or "") if isinstance(method, dict) else "",
                "polarity": _metadata_value(item, "acquisition", "polarity"),
                "ms_levels": ms_levels,
                "has_ms1": _metadata_flag(item, "hasMs1", 1 in levels if levels else None),
                "has_ms2": _metadata_flag(item, "hasMs2", 2 in levels if levels else None),
                "has_ion_mobility": _metadata_flag(item, "hasIonMobility", None),
                "separation": _metadata_value(item, "acquisition", "separation"),
                "isolation_window_count": len(targets) if isinstance(targets, list) else None,
                "collision_energy_count": len(energies) if isinstance(energies, list) else None,
                # What the header alone means to the Console (DDA, SWATH, AIF or None). The disposition of
                # a campaign unit may replace console_acquisition_type with the type the file runs as.
                "header_console_acquisition_type": console,
                "header_console_acquisition_basis": console_basis,
                "console_acquisition_type": console,
                "console_acquisition_basis": console_basis if console else "",
                "reader": str(source.get("readerName") or ""),
                "extractor_warnings": sorted(
                    {
                        str(warning.get("code") or "")
                        for warning in item.get("warnings") or []
                        if isinstance(warning, dict) and warning.get("code")
                    }
                ),
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
        "out_of_scope_methods": sorted(methods & set(OUT_OF_SCOPE_HEADER_METHODS)),
        "evidence_kinds": sorted({entry["method_source"] for entry in per_file if entry["method_source"]}),
        "evidence": [f"Raw metadata preflight inspected {len(records)} representative file(s)."],
    }


def _metadata_flag(record: dict[str, Any], field_name: str, fallback: bool | None) -> bool | None:
    """A boolean the extractor records under acquisition, or the fallback when it records none."""
    value = (record.get("acquisition") or {}).get(field_name)
    if isinstance(value, dict):
        value = value.get("value")
    return value if isinstance(value, bool) else fallback


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
        # A per-file record now exists for every input a preflight attempted; one whose header could not be
        # read was not inspected, and "no header recorded a time" must not be said of it.
        if item.get("outcome", "ok") not in {"ok", "reused"}:
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
        entries = (preflight.get("summary") or {}).get("per_file") or []
        # The output file is written by every preflight now, empty when nothing could be read, so it
        # counts as a read only for a summary from before per-file records.
        if any(isinstance(item, dict) and item.get("outcome", "ok") in {"ok", "reused"} for item in entries) or (
            not entries and Path(str(preflight.get("output") or "")).is_file()
        ):
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
    aliases = console_alias_inputs(manifest)
    same_unit = bool(candidates) and all(
        str(Path(_unit_input_of(str(item.get("file_path", "")), aliases)).resolve()).casefold() in candidates
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
    aliases = console_alias_inputs(manifest)
    same_unit = bool(inputs) and all(
        Path(_unit_input_of(str(item.get("file_path", "")), aliases).replace("\\", "/")).name.casefold() in inputs
        for item in files
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


# What the SCIEX readers open beside a primary file, named by its stem: Analyst writes x.wiff with
# x.wiff.scan (and x.wiff.<n>.scan for a multi-part one), and SCIEX OS writes x.wiff2 with x.wiff.scan and
# x.timeseries.data (MsdialWorkbenchDemo massql_demofiles), so a .wiff2's scan data is not named after the
# .wiff2. The analysis CSV's aliases (repository_analysis_rows) and the store lease's prune both read it here,
# so that what an alias carries and what a unit's tree keeps are the same files.
SCIEX_SUFFIXES = (".wiff", ".wiff2")
SCIEX_COMPANION_SUFFIXES = (".wiff.scan", ".wiff2.scan", ".timeseries.data")


def travels_with_sciex_file(name: str, primary: str) -> bool:
    """Whether a file named ``name``, beside the SCIEX file named ``primary``, is one its reader opens with it.

    Its stem with a companion suffix (SCIEX_COMPANION_SUFFIXES), or its whole name with a part and .scan
    (x.wiff.1.scan). Never the primary itself, and nothing travels with a file that is no .wiff or .wiff2.
    """
    primary_name = str(primary).casefold()
    suffix = PurePosixPath(primary_name).suffix
    if suffix not in SCIEX_SUFFIXES:
        return False
    lowered = str(name).casefold()
    if lowered == primary_name:
        return False
    stem = primary_name[: -len(suffix)]
    return any(lowered == stem + item for item in SCIEX_COMPANION_SUFFIXES) or (
        lowered.startswith(primary_name + ".") and lowered.endswith(".scan")
    )


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
