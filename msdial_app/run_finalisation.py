"""What a finished Console run leaves behind, put where it belongs before anything is validated or deleted.

Called once from the production job, after the Console returns and before the mzTab-M is validated and the
unit's retained artifacts are inventoried, so that what is validated, inventoried and later shared is what the
run keeps. It never raises: a step that fails is recorded, and the run's own verdict is left to validation and
the gate.

THREE THINGS, decided by the user on 2026-09-30 for the unattended repository campaign.

1. The mzTab-M names no location from this machine. The Console writes ``database[n]-uri`` as
   ``file://<library path>`` and ``ms_run[n]-location`` as ``file://<raw file path>`` (MztabFormatExport.cs).
   A library without a public DOI or https source gets ``null``; a public one its DOI; each raw file its path
   relative to the unit's workspace (``raw/...``). The ``-version`` file name, the prefix and the Console's
   ``custom[n]`` record and compound counts are kept, and a nulled library gains a ``custom`` line naming it
   by file name and sha256. The original values are kept only in a local-only provenance file. A laboratory
   run gets the private-library part alone: its public libraries and its raw locations are left as they are.

2. MS-DIAL's per-file and alignment containers move out of the raw tree. The Console writes them beside the
   files it reads, not into its output folder (AnalysisFilesParser.cs, AlignmentResultParser.cs,
   CommonProcess.SetProjectProperty), so deleting the raw data deleted them. Per input, named by the analysis
   CSV's file_name: ``<file_name>_<ts>.dcl``, ``.pai2`` (MessagePackHandler appends the version to ``.pai``),
   ``.rtc``, ``.sfs``, ``_tags.xml`` and ``<file_name>_<ts>_<CE x 100>.dcl`` for an AIF run's per-energy
   deconvolution; in the first input's directory, ``AlignResult-<ts>.arf2``, ``.dcl``, ``.EIC.aef``,
   ``_PeakProperties.arf``, ``_DriftSopts.arf`` and ``_tags.xml``. ``<ts>`` is ``yyyyMdHm`` with no zero padding,
   so it cannot be parsed back into a time, and every earlier attempt - a failed run, a peak-count
   diagnostic - left a set of its own. The finalised run's set is the one whose timestamp its mzTab-M carries
   (``AlignResult-<ts>.mzTab``), else, per input, the newest; it moves to ``output/msdial-intermediates/``
   with its path under the raw directory kept, and the job that moved it is named in the record. The other
   sets of this unit's inputs are recorded as superseded and left to be deleted with the raw tree.

3. ``<project>_Loaded.msp2.dbs``, MS-DIAL's serialised copy of every library the run loaded, is deleted from
   the output once the run is over, its name, size and sha256 recorded. With the private VS21 pair it is a
   copy of the private library in every unit, and it can never be shared.

1 applies to every run. 2 and 3 apply only to a unit a campaign approval has been recorded for - in its own
manifest's campaign_authorizations, or in those of the unit it was split from - because they were decided for
the campaign, whose raw data are deleted. Any other run, a trial repository unit included whatever its
retention policy, leaves its containers beside its inputs and its library copy in its output, as it always
has, and its project can still be reopened in place.

Every repository run also records what a raw-data reader wrote into its container inputs, such as the
analysis.sqlite Bruker's baf2sql writes into a BAF .d that arrived without one (``reader_created_files`` in the
unit manifest, msdial_app.reader_created). That is no MS-DIAL container: it is neither moved nor retained, and
it goes with the raw tree.

HOLDS. On Windows a file another process holds open without FILE_SHARE_DELETE - a viewer, the search indexer,
antivirus scanning what the Console has just written - can be neither replaced nor deleted, and a container the
Console could write can have a path too long for an ordinary move. Every file step is retried on
PermissionError within one time budget, and the moves use extended-length paths. A step that still fails does
not end as a log line. It becomes a hold in the unit manifest's ``finalisation_holds``:

- one that blocks ``sharing`` (an mzTab-M that still names a location, a library copy still in the output) is
  refused by the publication report, and must be refused by anything else built to leave this machine;
- one that blocks ``raw_deletion`` (containers still beside the inputs) is refused by the raw cleanup and the
  discard, a split parent's included when one of its parts holds it.

Each of those steps first retries what is held (resolve_finalisation_holds), so a lock let go in the meantime
clears itself. A laboratory run has no manifest: its hold is in the job record and the log.

THE PROJECT CANNOT BE REOPENED AS IT STANDS once 2 and 3 have run. The ``.mdproject`` names the output
directory and the ``.mddata``; the ``.mddata`` names every input by its absolute raw path and every container
by its absolute path beside it, and the project loader reads the libraries back from
``<project>_Loaded.msp2.dbs``. After this, and after the raw data are released, MS-DIAL stops at the missing
library copy. Copying ``msdial-intermediates/`` back under the raw directory, with the raw files
re-downloaded, restores every path the ``.mddata`` names; the library copy is not restorable without running
again.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import time
import urllib.parse
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .diagnostic_paths import INTERMEDIATES_DIRECTORY, extended_path, path_is_file
from .sharing import PATH_POLICY, SharingContext, local_path_of


SCHEMA = "msdial-console-run-finalisation.v1"
REDACTION_RECORD = "mztab-redaction.local.json"
LOADED_LIBRARY_COPY = "*_Loaded.msp2.dbs"
HOLDS = "finalisation_holds"
HOLD_RESOLUTIONS = "finalisation_hold_resolutions"
BLOCKS_SHARING = "sharing"
BLOCKS_RAW_DELETION = "raw_deletion"

# The pauses between attempts at a file step another process holds, drawn from one budget of waiting per call:
# a unit whose every container is held waits the budget once, not once per file.
RETRY_DELAYS_SECONDS = (0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 8.0, 15.0)
RETRY_BUDGET_SECONDS = 60.0

_TIMESTAMP = r"(?P<ts>20\d{6,10})"
PER_FILE_SUFFIX = re.compile(
    _TIMESTAMP + r"(?P<suffix>(?:_\d+)?\.dcl|\.pai2?|\.rtc|\.sfs|_tags\.xml)", re.IGNORECASE
)
ALIGNMENT_FILE = re.compile(
    r"AlignResult-" + _TIMESTAMP
    + r"(?P<suffix>(?:_\d+)?\.dcl|\.arf2?|\.EIC\.aef|_PeakProperties\.arf2?|_DriftSopts\.arf2?|_tags\.xml)",
    re.IGNORECASE,
)
ALIGNMENT_EXPORT = re.compile(r"AlignResult-" + _TIMESTAMP + r"\.mztab", re.IGNORECASE)

_BOM = b"\xef\xbb\xbf"
_DATABASE_URI = re.compile(r"database\[(\d+)\]-uri")
_DATABASE_VERSION = re.compile(r"database\[(\d+)\]-version")
_DATABASE_LINE = re.compile(r"database\[\d+\]")
_RUN_LOCATION = re.compile(r"ms_run\[\d+\]-location")
_CUSTOM = re.compile(r"custom\[(\d+)\]")
_PREPARATION_KEYS = ("run_directory", "settings_file", "manifest", "repository_run_manifest")

PROJECT_REOPEN = {
    "reopenable_in_place": False,
    "references": (
        "The .mdproject names the output directory and the .mddata. The .mddata names every input by its "
        "absolute raw path, the per-file .dcl/.pai2 and the alignment .arf2/.dcl/.EIC.aef by their absolute "
        "paths beside the inputs, and the project loader reads the libraries back from "
        "<project>_Loaded.msp2 and <project>_Loaded.msp2.dbs."
    ),
    "restore": (
        "Copy msdial-intermediates/<path> back under the raw directory, with the raw files at the paths the "
        "analysis CSV lists, to restore every container path the .mddata names. The loaded-library copy was "
        "deleted after the run, so MS-DIAL's loader still stops at <project>_Loaded.msp2.dbs; reopening needs "
        "the run repeated."
    ),
}


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(extended_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _size(path: str | Path) -> int:
    return os.stat(extended_path(path)).st_size


def _read_json(path: Path) -> dict[str, Any]:
    from .repository_reanalysis import read_manifest

    try:
        return read_manifest(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


class _Patience:
    """One budget of waiting for a call's file steps, spent only on PermissionError.

    That is what Windows raises (ERROR_ACCESS_DENIED, ERROR_SHARING_VIOLATION) for replacing or deleting a file
    another process has open without FILE_SHARE_DELETE, and the holder usually lets go within seconds. Any other
    error is not a lock and is not waited for. Only the pauses count against the budget, so hashing a
    multi-gigabyte library copy first does not use up the patience its deletion needs.
    """

    def __init__(self, budget: float | None = None) -> None:
        self.budget = RETRY_BUDGET_SECONDS if budget is None else budget
        self.waited = 0.0

    def run(self, step: Callable[[], Any]) -> Any:
        for delay in RETRY_DELAYS_SECONDS:
            try:
                return step()
            except PermissionError:
                if self.waited + delay > self.budget:
                    raise
            self.waited += delay
            time.sleep(delay)
        return step()


# ---- 1. the mzTab-M ---------------------------------------------------------------------------------


def _database_uri(value: str, version: str, context: SharingContext) -> tuple[str, Any]:
    local = local_path_of(value)
    if local is None:
        return value, None  # null, a DOI or a URL: nothing of this machine's
    library = context.library_for(local, version)
    if library is None or library.private:
        # A library no record describes is private until something says otherwise.
        return "null", library
    if context.full:
        return library.public_uri or "null", library
    return value, library


def _run_location(value: str, context: SharingContext) -> str:
    local = local_path_of(value)
    if local is None:
        return "null" if context.residual(value) else value
    relative = context.relative(local)
    if not relative or relative == ".":
        return "null"
    return urllib.parse.quote(relative, safe="/")


def _param_field(text: str) -> str:
    """One field of an mzTab-M Param, ``[label, accession, name, value]``.

    The specification requires a name or value that holds a comma to be quoted, and a strict reader
    (jmzTab-M) splits an unquoted one into a fifth field. A tab or line break would end the line, and a double
    quote (which no Windows file name holds) would end the quoting, so they are replaced.
    """
    clean = re.sub(r"[\t\r\n]+", " ", str(text)).replace('"', "'")
    return f'"{clean}"' if "," in clean else clean


def _identity_line(index: int, database: str, library: Any) -> str:
    identity = library.identity
    parts = [library.name, "sha256:" + (identity.get("sha256") or "not recorded")]
    if isinstance(identity.get("bytes"), int):
        parts.append(f"{identity['bytes']} bytes")
    # '; ' between the parts, as the Console writes its own custom[n] lines.
    parts.append("private; not distributed")
    return (
        f"MTD\tcustom[{index}]\t[,, MS-DIAL library file database[{database}], {_param_field('; '.join(parts))}]"
    )


def _mtd_fields(text: str) -> list[str] | None:
    parts = text.split("\t", 2)
    return parts if len(parts) == 3 and parts[0] == "MTD" else None


def redact_mztab(
    path: str | Path, context: SharingContext, patience: _Patience | None = None
) -> dict[str, Any]:
    """Rewrite one Console mzTab-M's metadata locations in place, atomically. Only MTD lines change.

    Returns what changed, with the original values under ``changes`` for a local-only record; nothing else
    returned carries a location. The metadata section is read into memory; the tables after it are copied
    as they are. The final replace waits, within ``patience``, for another process to let the file go; if it
    does not, the file is left exactly as it was and the error is raised.
    """
    source = Path(path)
    result: dict[str, Any] = {"file": source.name, "changed": False, "database_uri": 0, "ms_run_location": 0,
                              "other": 0, "identity_lines": 0, "changes": []}
    with source.open("rb") as handle:
        original: list[bytes] = []
        head: list[tuple[bytes, str, bytes]] = []  # (byte-order mark, text, line ending)
        first = b""
        while True:
            raw = handle.readline()
            if not raw:
                break
            lead = _BOM if not original and raw.startswith(_BOM) else b""
            body = raw[len(lead):]
            text = body.rstrip(b"\r\n")
            if text and not (text.startswith(b"MTD\t") or text.startswith(b"COM")):
                first = raw
                break
            original.append(raw)
            head.append((lead, text.decode("utf-8", "surrogateescape"), body[len(text):]))

        versions = {
            match.group(1): fields[2].strip()
            for fields in (_mtd_fields(text) for _lead, text, _end in head) if fields
            for match in [_DATABASE_VERSION.fullmatch(fields[1])] if match
        }
        nulled: list[tuple[str, Any]] = []
        for index, (lead, text, end) in enumerate(head):
            fields = _mtd_fields(text)
            if not fields:
                continue
            key, value = fields[1], fields[2]
            match = _DATABASE_URI.fullmatch(key)
            if match:
                new, library = _database_uri(value, versions.get(match.group(1), ""), context)
                kind = "database_uri"
                if new == "null" and value.strip().casefold() != "null" and library is not None:
                    nulled.append((match.group(1), library))
            elif context.full and _RUN_LOCATION.fullmatch(key):
                new, kind = _run_location(value, context), "ms_run_location"
            elif context.residual(value):
                new, kind = context.text(value), "other"
            else:
                continue
            if new != value:
                head[index] = (lead, f"MTD\t{key}\t{new}", end)
                result[kind] += 1
                result["changes"].append({"key": key, "original": value, "redacted": new})
        if not result["changes"]:
            return result
        if nulled:
            fields_by_line = [_mtd_fields(text) for _lead, text, _end in head]
            customs = [(position, int(_CUSTOM.fullmatch(fields[1]).group(1)))
                       for position, fields in enumerate(fields_by_line) if fields and _CUSTOM.fullmatch(fields[1])]
            databases = [position for position, fields in enumerate(fields_by_line)
                         if fields and _DATABASE_LINE.match(fields[1])]
            metadata = [position for position, fields in enumerate(fields_by_line) if fields]
            anchor = customs[-1][0] if customs else databases[-1] if databases else metadata[-1] if metadata else -1
            end = (head[anchor][2] if anchor >= 0 else b"") or b"\n"
            number = max((value for _position, value in customs), default=0)
            added = []
            for database, library in nulled:
                number += 1
                added.append((b"", _identity_line(number, database, library), end))
            head[anchor + 1:anchor + 1] = added
            result["identity_lines"] = len(added)

        before = hashlib.sha256()
        after = hashlib.sha256()
        for raw in original:
            before.update(raw)
        # Named with nothing a scan for mzTab-M files matches, so an interrupted rewrite is never read as one.
        temporary = source.parent / f".redact-{uuid.uuid4().hex}.tmp"
        try:
            with temporary.open("wb") as target:
                for lead, text, end in head:
                    data = lead + text.encode("utf-8", "surrogateescape") + end
                    target.write(data)
                    after.update(data)
                block = first
                while block:
                    before.update(block)
                    after.update(block)
                    target.write(block)
                    block = handle.read(4 * 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        (patience or _Patience()).run(lambda: os.replace(temporary, source))
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    result.update(changed=True, sha256_before=before.hexdigest(), sha256_after=after.hexdigest())
    return result


# ---- 2. the containers MS-DIAL left beside its inputs ------------------------------------------------


def _console_inputs(csv_path: Path) -> list[tuple[str, str]]:
    """(file_path as the Console read it, file_name) for each row of the analysis CSV.

    The Console names its containers after the file_name column, and after the file's stem only when the
    column is absent (AnalysisFilesParser.ReadCsvContents).
    """
    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    if not rows:
        return []
    header = [cell.strip().casefold() for cell in rows[0]]
    if "file_path" not in header:
        return []
    path_index = header.index("file_path")
    name_index = header.index("file_name") if "file_name" in header else -1
    inputs = []
    for row in rows[1:]:
        if len(row) <= path_index or not row[path_index].strip() or row[path_index].lstrip().startswith("#"):
            continue
        path = row[path_index].strip()
        if 0 <= name_index < len(row):
            name = row[name_index]
        else:
            name = PureWindowsPath(path.rstrip("\\/")).stem
        inputs.append((path, name))
    return inputs


def _directory_of(path_text: str) -> Path:
    """Path.GetDirectoryName(Path.GetFullPath(p)): a folder named with a trailing separator is its own."""
    if path_text.endswith(("\\", "/")):
        return Path(path_text.rstrip("\\/")).resolve()
    return Path(path_text).resolve().parent


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _move(source: Path, target: Path, patience: _Patience) -> str:
    """Move one file, at any path length; on another volume, copy it, compare the bytes, then remove the source."""
    origin, destination = extended_path(source), extended_path(target)
    Path(extended_path(target.parent)).mkdir(parents=True, exist_ok=True)
    try:
        patience.run(lambda: os.replace(origin, destination))
        return "rename"
    except PermissionError:
        # Still held after the budget. A copy could not remove the source either, so none is made.
        raise
    except OSError:
        shutil.copy2(origin, destination)
        if _sha256(origin) != _sha256(destination):
            os.unlink(destination)
            raise OSError(f"The copy of {source.name} does not match its source; the source was kept.")
        patience.run(lambda: os.unlink(origin))
        return "copy"


def relocate_intermediates(
    job_id: str,
    csv_path: Path,
    raw_directory: Path,
    output_directory: Path,
    mztab_names: list[str],
    *,
    pinned: dict[str, Any] | None = None,
    patience: _Patience | None = None,
) -> dict[str, Any]:
    """Move the finalised run's MS-DIAL containers from beside its inputs into the unit's output.

    ``pinned`` is an earlier call's choice for the same run - its alignment timestamp and each input's
    timestamp - and is how a retry moves what that call could not, and nothing else: an input whose set has
    already left the raw tree is not given an earlier attempt's set in its place. Every path that could not be
    moved is listed in ``pending``, with the reason in ``errors``.
    """
    patience = patience or _Patience()
    raw_root = raw_directory.resolve()
    destination = output_directory.resolve() / INTERMEDIATES_DIRECTORY
    record: dict[str, Any] = {
        "schema": "msdial-intermediates-relocation.v1",
        "job_id": job_id,
        "relocated_at": _now(),
        "raw_directory": str(raw_root),
        "destination": str(destination),
        "restore_to": "raw_directory",
        "alignment_timestamp": "",
        "moved": [],
        "superseded": [],
        "selection": [],
        "left_in_place": [],
        "no_intermediates_found": [],
        "pending": [],
        "errors": [],
        "project_reopen": dict(PROJECT_REOPEN),
    }
    inputs = _console_inputs(csv_path) if csv_path.is_file() else []
    if not inputs:
        record["errors"].append("The analysis CSV lists no input, so no container could be attributed.")
        return record

    pinned_inputs = {str(key): str(value) for key, value in ((pinned or {}).get("selection") or {}).items()}
    exported = {match.group("ts") for name in mztab_names for match in [ALIGNMENT_EXPORT.fullmatch(name)] if match}
    alignment_ts = max(exported, key=lambda item: (len(item), item)) if exported else ""
    if pinned_inputs:
        alignment_ts = str((pinned or {}).get("alignment_timestamp") or "")
    names_by_directory: dict[Path, list[str]] = {}
    for path_text, name in inputs:
        names_by_directory.setdefault(_directory_of(path_text), []).append(name)
    alignment_directory = _directory_of(inputs[0][0])

    groups: dict[str, dict[str, list[Path]]] = {name: {} for _path, name in inputs}
    alignment_sets: dict[str, list[Path]] = {}
    for directory, names in names_by_directory.items():
        try:
            entries = [entry for entry in os.scandir(directory) if entry.is_file()]
        except OSError as error:
            record["errors"].append(f"{directory.name}: {error}")
            continue
        longest_first = sorted(set(names), key=len, reverse=True)
        for entry in entries:
            if directory == alignment_directory:
                match = ALIGNMENT_FILE.fullmatch(entry.name)
                if match:
                    alignment_sets.setdefault(match.group("ts"), []).append(Path(entry.path))
                    continue
            lowered = entry.name.casefold()
            for name in longest_first:
                if lowered.startswith(name.casefold() + "_"):
                    match = PER_FILE_SUFFIX.fullmatch(entry.name[len(name) + 1:])
                    if match:
                        groups[name].setdefault(match.group("ts"), []).append(Path(entry.path))
                        break

    def newest(paths: list[Path]) -> float:
        return max((path.stat().st_mtime for path in paths if path.exists()), default=0.0)

    chosen: list[tuple[str, str, Path, str]] = []  # (kind, input, file, timestamp)
    superseded: list[tuple[str, str, Path, str]] = []
    chosen_timestamps: list[str] = []
    for _path, name in inputs:
        sets = groups.get(name) or {}
        if name in pinned_inputs:
            timestamp, rule = pinned_inputs[name], "pinned"
        elif not sets:
            if name not in {item["input"] for item in record["selection"]}:
                record["no_intermediates_found"].append(name)
            continue
        elif alignment_ts and alignment_ts in sets:
            timestamp, rule = alignment_ts, "alignment_timestamp"
        else:
            timestamp, rule = max(sets, key=lambda item: newest(sets[item])), "newest"
        record["selection"].append({"input": name, "timestamp": timestamp, "rule": rule})
        chosen_timestamps.append(timestamp)
        for ts, paths in sets.items():
            target = chosen if ts == timestamp else superseded
            target.extend(("per_file", name, path, ts) for path in paths)
        groups[name] = {}  # an input listed twice is attributed once
    if not alignment_ts and chosen_timestamps and not pinned_inputs:
        common = max(set(chosen_timestamps), key=chosen_timestamps.count)
        alignment_ts = common if common in alignment_sets else ""
    if alignment_ts in alignment_sets:
        chosen.extend(("alignment", "", path, alignment_ts) for path in alignment_sets[alignment_ts])
    record["alignment_timestamp"] = alignment_ts
    # An alignment set is this unit's earlier attempt only when one of its inputs has a set of the same
    # timestamp. Another run reading the same raw tree - the other part of a split unit - writes its own.
    earlier = {ts for _kind, _name, _path, ts in superseded}
    for ts, paths in alignment_sets.items():
        if ts != alignment_ts and ts in earlier:
            superseded.extend(("alignment", "", path, ts) for path in paths)

    for kind, name, path, timestamp in chosen:
        resolved = path.resolve()
        if not _is_under(resolved, raw_root):
            record["left_in_place"].append({"name": path.name, "kind": kind, "input": name,
                                            "reason": "not under the unit's raw directory"})
            continue
        relative = resolved.relative_to(raw_root)
        target = destination / relative
        entry: dict[str, Any] = {"relative_path": relative.as_posix(), "path": str(target), "kind": kind,
                                 "input": name, "timestamp": timestamp}
        try:
            size = _size(resolved)
            if path_is_file(target):
                earlier_copy = {"size_bytes": _size(target), "sha256": _sha256(target)}
                if earlier_copy["size_bytes"] == size and earlier_copy["sha256"] == _sha256(resolved):
                    patience.run(lambda: os.unlink(extended_path(resolved)))
                    method = "already_moved"
                else:
                    # An earlier run of this unit, within the same minute, moved a container of this name. This
                    # run has overwritten that run's outputs of the same names, and its container follows them.
                    method = _move(resolved, target, patience)
                    entry["replaced"] = earlier_copy
            else:
                method = _move(resolved, target, patience)
        except OSError as error:
            record["errors"].append(f"{relative.as_posix()}: {error}")
            record["pending"].append(relative.as_posix())
            continue
        record["moved"].append({**entry, "size_bytes": size, "method": method})
    for kind, name, path, timestamp in superseded:
        try:
            size = _size(path)
        except OSError:
            continue
        try:
            relative_text = path.resolve().relative_to(raw_root).as_posix()
        except ValueError:
            relative_text = path.name
        record["superseded"].append({"relative_path": relative_text, "size_bytes": size, "kind": kind,
                                     "input": name, "timestamp": timestamp})
    record["moved_bytes"] = sum(item["size_bytes"] for item in record["moved"])
    record["superseded_bytes"] = sum(item["size_bytes"] for item in record["superseded"])
    return record


def _pinned(relocation: dict[str, Any]) -> dict[str, Any]:
    return {
        "alignment_timestamp": relocation.get("alignment_timestamp") or "",
        "selection": {item["input"]: item["timestamp"] for item in relocation.get("selection") or []},
    }


# ---- 3. the loaded-library copy ------------------------------------------------------------------------


def delete_loaded_library_copies(
    output_directory: Path, patience: _Patience | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Delete MS-DIAL's serialised copies of the loaded libraries, recording each by name, size and sha256."""
    patience = patience or _Patience()
    deleted: list[dict[str, Any]] = []
    errors: list[str] = []
    if not output_directory.is_dir():
        return deleted, errors
    for path in sorted(output_directory.glob(LOADED_LIBRARY_COPY)):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
            digest = _sha256(path)
            patience.run(path.unlink)
        except OSError as error:
            errors.append(f"{path.name}: {error}")
            continue
        deleted.append({"name": path.name, "size_bytes": size, "sha256": digest, "deleted_at": _now()})
    return deleted, errors


def _library_copies(directories: list[Path]) -> list[str]:
    return [str(path) for directory in directories if directory.is_dir()
            for path in sorted(directory.glob(LOADED_LIBRARY_COPY)) if path.is_file()]


# ---- holds -----------------------------------------------------------------------------------------------


def _hold(blocks: list[str], step: str, job_id: str, reason: str, **detail: Any) -> dict[str, Any]:
    return {"id": uuid.uuid4().hex, "blocks": list(blocks), "step": step, "job_id": job_id, "reason": reason,
            "recorded_at": _now(), **detail}


class FinalisationHeld(ValueError):
    """A step refused because a run's finalisation left something undone that the step would make unsafe.

    A ValueError, so every structured-error path reports it; the message starts with a fixed token and what
    is blocked, so an unattended caller can tell a held unit from any other refusal without parsing prose.
    """

    def __init__(self, blocks: str, message: str, holds: list[dict[str, Any]]) -> None:
        self.blocks = blocks
        self.holds = list(holds)
        super().__init__(f"finalisation_held [{blocks}]: {message}: {describe_holds(self.holds)}.")


def _hold_summaries(holds: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: item[key] for key in ("id", "blocks", "step", "reason")} for item in holds]


def standing_holds(manifest: dict[str, Any], blocks: str | None = None) -> list[dict[str, Any]]:
    """The holds a unit manifest records, or only those that block ``blocks``."""
    holds = [item for item in manifest.get(HOLDS) or [] if isinstance(item, dict)]
    return [item for item in holds if blocks is None or blocks in (item.get("blocks") or [])]


def _split_parts(manifest: dict[str, Any]) -> list[Path]:
    return [Path(str(item["manifest_path"])) for item in manifest.get("split_into") or []
            if isinstance(item, dict) and str(item.get("manifest_path") or "").strip()]


def raw_deletion_holds(manifest_path: str | Path, manifest: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """What forbids deleting this unit's raw directory: its own holds, and those of every part split from it.

    A part reads its parent's raw tree and moves its containers out of it, so a part that could not is a hold
    on the parent's deletion. A part manifest that exists and cannot be read is one too.
    """
    from .repository_reanalysis import read_manifest

    path = Path(str(manifest_path)).expanduser().resolve()
    manifest = manifest if manifest is not None else _read_json(path)
    found = [{**item, "manifest_path": str(path)} for item in standing_holds(manifest, BLOCKS_RAW_DELETION)]
    for part in _split_parts(manifest):
        if not part.is_file():
            continue
        part = part.resolve()
        try:
            recorded = read_manifest(part)
        except (OSError, ValueError) as error:
            found.append({**_hold([BLOCKS_RAW_DELETION], "part_manifest", "",
                                  f"The manifest of a part split from this unit could not be read: {error}"),
                          "manifest_path": str(part)})
            continue
        found.extend({**item, "manifest_path": str(part)}
                     for item in standing_holds(recorded, BLOCKS_RAW_DELETION))
    return found


def describe_holds(holds: list[dict[str, Any]]) -> str:
    """One sentence for a refusal, naming each held step and its run."""
    return "; ".join(
        f"{item.get('step')} of run {item.get('job_id') or 'unrecorded'} ({item.get('reason')})" for item in holds
    )


def _retry_hold(
    hold: dict[str, Any], manifest_path: Path, patience: _Patience
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """(the hold as it stands after one retry, or None; a resolution note, or None)."""
    step = hold.get("step")
    now = _now()
    note = {"hold": hold.get("id"), "step": step, "job_id": hold.get("job_id"), "resolved_at": now}
    if step == "mztab_redaction":
        context, _run_directory, _manifest_text = _sharing_context(hold.get("preparation") or {})
        left, errors, done, changed = [], [], [], []
        for file in hold.get("files") or []:
            path = Path(str(file))
            if not path.is_file():
                done.append({"file": path.name, "outcome": "gone"})
                continue
            try:
                result = redact_mztab(path, context, patience)
            except OSError as error:
                left.append(str(file))
                errors.append(f"{path.name}: {error}")
                continue
            if result["changed"]:
                changed.append(result)
            done.append({"file": path.name, "outcome": "redacted" if result["changed"] else "nothing_to_redact"})
        if changed:
            try:
                _record_redactions(manifest_path, str(hold.get("job_id") or ""), changed)
            except (OSError, ValueError, TypeError) as error:
                note["local_record_error"] = f"{REDACTION_RECORD}: {error}"
        if left:
            return {**hold, "files": left, "errors": errors, "last_retried_at": now}, None
        return None, {**note, "files": done}
    if step == "loaded_library_copy":
        left, errors, deleted = [], [], []
        for file in hold.get("files") or []:
            path = Path(str(file))
            if not path.is_file():
                continue
            try:
                size, digest = path.stat().st_size, _sha256(path)
                patience.run(path.unlink)
            except OSError as error:
                left.append(str(file))
                errors.append(f"{path.name}: {error}")
                continue
            deleted.append({"name": path.name, "size_bytes": size, "sha256": digest, "deleted_at": _now()})
        if left:
            return {**hold, "files": left, "errors": errors, "last_retried_at": now}, None
        return None, {**note, "deleted": deleted}
    if step == "msdial_intermediates":
        raw = Path(str(hold.get("raw_directory") or ""))
        if not str(hold.get("raw_directory") or "").strip() or not raw.is_dir():
            return None, {**note, "outcome": "the raw directory is gone; nothing is left to move"}
        relocation = relocate_intermediates(
            str(hold.get("job_id") or ""), Path(str(hold.get("csv_path") or "")), raw,
            Path(str(hold.get("output_directory") or "")), list(hold.get("mztab_names") or []),
            pinned=hold.get("pinned") or {}, patience=patience,
        )
        if relocation["errors"]:
            return {**hold, "errors": relocation["errors"], "pending": relocation["pending"],
                    "last_retried_at": now}, None
        return None, {**note, "moved": relocation["moved"], "moved_bytes": relocation["moved_bytes"]}
    # Recorded when finalisation itself failed: nothing says what is left to do, so a person looks.
    return hold, None


def _resolve_in(manifest_path: Path, patience: _Patience, log: Callable[[str], None]) -> list[dict[str, Any]]:
    from .repository_reanalysis import update_manifest

    holds = standing_holds(_read_json(manifest_path))
    if not holds:
        return []
    outcomes: dict[str, tuple[dict[str, Any] | None, dict[str, Any] | None]] = {}
    for hold in holds:
        try:
            outcomes[str(hold.get("id"))] = _retry_hold(hold, manifest_path, patience)
        except Exception as error:  # a retry must not make a refusal into a crash
            outcomes[str(hold.get("id"))] = (
                {**hold, "errors": [f"{type(error).__name__}: {error}"], "last_retried_at": _now()}, None
            )
    for updated, note in outcomes.values():
        if note:
            log(f"Finalisation hold on {note['step']} of run {note['job_id']} resolved on retry.")
        elif updated is not None and updated.get("errors"):
            log(f"WARNING: finalisation hold on {updated['step']} of run {updated['job_id']} still stands: "
                + "; ".join(updated["errors"][:3]))

    def change(current: dict[str, Any]) -> None:
        kept = []
        for item in current.get(HOLDS) or []:
            key = str(item.get("id")) if isinstance(item, dict) else ""
            if key not in outcomes:
                kept.append(item)
                continue
            updated, note = outcomes[key]
            if updated is not None:
                kept.append(updated)
            if note:
                current[HOLD_RESOLUTIONS] = [*(current.get(HOLD_RESOLUTIONS) or []), note]
        current[HOLDS] = kept

    try:
        update_manifest(manifest_path, change)
    except (OSError, ValueError) as error:
        log(f"WARNING: the finalisation holds of {manifest_path.name} could not be updated: {error}")
        return [{**item, "manifest_path": str(manifest_path)} for item in holds]
    return [{**updated, "manifest_path": str(manifest_path)} for updated, _note in outcomes.values()
            if updated is not None]


def resolve_finalisation_holds(
    manifest_path: str | Path, log: Callable[[str], None] | None = None
) -> list[dict[str, Any]]:
    """Retry every step a run's finalisation left held on this unit and on the parts split from it.

    Returns the holds that still stand, each naming the manifest it is recorded in. Never raises: a unit whose
    manifest cannot be read has nothing this can retry, and its caller's own checks refuse it.
    """
    path = Path(str(manifest_path or "")).expanduser()
    if not str(manifest_path or "").strip() or not path.is_file():
        return []
    path = path.resolve()
    patience = _Patience()
    say = log or (lambda _line: None)
    standing = _resolve_in(path, patience, say)
    for part in _split_parts(_read_json(path)):
        if part.is_file():
            standing.extend(_resolve_in(part.resolve(), patience, say))
    return standing


# ---- the one call ------------------------------------------------------------------------------------


def _sharing_context(preparation: dict[str, Any]) -> tuple[SharingContext, Path, str]:
    run_directory = Path(str(preparation.get("run_directory") or "")).expanduser()
    state = _read_json(Path(str(preparation.get("settings_file") or run_directory / "workflow-settings.json")))
    recorded = _read_json(Path(str(preparation.get("manifest") or run_directory / "run-manifest.json")))
    # The job's own preparation says whether this is a repository unit's run; the settings follow it.
    manifest_text = str(preparation.get("repository_run_manifest") or "").strip()
    state = {**{key: value for key, value in state.items() if key != "repository_run_manifest"},
             **({"repository_run_manifest": manifest_text} if manifest_text else {})}
    context = SharingContext.for_state(state, run_directory=run_directory, recorded=recorded.get("libraries"))
    return context, run_directory, manifest_text


def _record_redactions(manifest_path: Path, job_id: str, changed: list[dict[str, Any]]) -> Path:
    """Append what was rewritten, the original values included, to the unit's local-only record."""
    from .repository_reanalysis import _write_json

    local = manifest_path.parent / REDACTION_RECORD
    previous = _read_json(local)
    entries = [entry for entry in previous.get("files") or [] if isinstance(entry, dict)]
    entries.extend({"job_id": job_id, "recorded_at": _now(), **item} for item in changed)
    _write_json(local, {
        "schema": "msdial-mztab-redaction.v1",
        # The locations the shared mzTab-M no longer carries. It stays on this machine: no bundle, report or
        # project archive includes it.
        "sharing": "local_only",
        "files": entries,
    })
    return local


def campaign_approval_recorded(manifest: dict[str, Any]) -> bool:
    """Whether a campaign approval has been recorded for this unit, or for the unit it was split from."""
    if any(isinstance(item, dict) for item in manifest.get("campaign_authorizations") or []):
        return True
    parent = str((manifest.get("split_from") or {}).get("manifest_path") or "").strip()
    if not parent:
        return False
    return any(isinstance(item, dict) for item in _read_json(Path(parent)).get("campaign_authorizations") or [])


def finalise_console_run(
    job_id: str,
    preparation: dict[str, Any],
    artifacts: dict[str, Any],
    exit_code: int | None,
    export_verification: dict[str, Any] | None,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Everything above, for one production job, once the Console has returned. Never raises.

    Every run gets the mzTab-M redaction. A unit a campaign approval covers also gets the other two: the
    containers move only for a run that produced every expected export, since a failed run's set is left to
    be deleted with the raw tree, and the loaded-library copy is deleted whatever the outcome. ``artifacts``
    loses the paths of what was deleted, so no later record names a file that is gone. What could not be done
    is recorded as a hold in the unit manifest, which the steps it would make unsafe refuse.
    """
    record: dict[str, Any] = {
        "schema": SCHEMA, "job_id": job_id, "finalised_at": _now(), "errors": [], "holds": [],
    }
    holds: list[dict[str, Any]] = []
    manifest_path: Path | None = None
    campaign = False
    patience = _Patience()
    manifest: dict[str, Any] = {}
    reader_created: dict[str, Any] | None = None
    try:
        # First, so that whatever fails below is held in the unit's own manifest.
        manifest_text = str(preparation.get("repository_run_manifest") or "").strip()
        if manifest_text:
            manifest_path = Path(manifest_text).expanduser().resolve()
            manifest = _read_json(manifest_path)
            campaign = campaign_approval_recorded(manifest)
            record["campaign_approval_recorded"] = campaign
        context, run_directory, _manifest_text = _sharing_context(preparation)
        record["scope"] = "repository_unit" if manifest_text else "laboratory"
        if context.full:
            record["shared_path_policy"] = PATH_POLICY
        record["libraries"] = context.identities()
        complete = exit_code == 0 and not (export_verification or {}).get("missing")

        redactions: list[dict[str, Any]] = []
        unredacted: list[str] = []
        for path in artifacts.get("mztab") or []:
            try:
                redactions.append(redact_mztab(path, context, patience))
            except OSError as error:
                unredacted.append(str(path))
                record["errors"].append(f"mzTab-M {Path(path).name}: {error}")
                log(f"WARNING: the mzTab-M {Path(path).name} could not be redacted, and it is held: nothing "
                    f"may share it until a retry redacts it. {error}")
        record["mztab_redaction"] = [{key: value for key, value in item.items() if key != "changes"}
                                     for item in redactions]
        for item in redactions:
            if item["changed"]:
                log(
                    f"Shared-path redaction of {item['file']}: {item['database_uri']} library URI(s), "
                    f"{item['ms_run_location']} ms_run location(s) and {item['other']} other value(s) "
                    f"rewritten; {item['identity_lines']} library identity line(s) added."
                )
        if unredacted:
            holds.append(_hold(
                [BLOCKS_SHARING], "mztab_redaction", job_id,
                "the mzTab-M could not be rewritten; it may still name a library or raw location of this machine",
                files=unredacted, preparation={key: str(preparation.get(key) or "") for key in _PREPARATION_KEYS},
            ))

        if manifest_path is None:
            record["holds"] = _hold_summaries(holds)
            return record
        output = Path(str(manifest.get("output_directory") or run_directory)).expanduser()
        changed = [item for item in redactions if item["changed"]]
        if changed:
            try:
                record["local_only"] = [str(_record_redactions(manifest_path, job_id, changed))]
            except (OSError, ValueError, TypeError) as error:
                record["errors"].append(f"{REDACTION_RECORD}: {error}")

        # What a reader wrote into the unit's container inputs during the run (Bruker's baf2sql writes
        # analysis.sqlite into a BAF .d that arrived without one): no MS-DIAL container, so it is neither
        # moved nor retained, only recorded in the unit manifest, and it goes with the raw tree.
        from .repository_reanalysis import reader_created_block

        try:
            reader_created = reader_created_block(manifest, "run")
        except (OSError, ValueError) as error:
            # A record, not a step anything waits on: the containers still move and the copy is still deleted.
            record["errors"].append(f"reader_created_files: {error}")
        if reader_created:
            record["reader_created_files"] = sum(len(item["files"]) for item in reader_created["containers"])
            log(f"Reader-created files inside the inputs: {record['reader_created_files']} file(s) in "
                f"{len(reader_created['containers'])} container(s), recorded as reader_created_files.")

        if not campaign:
            # Decided for the campaign, whose raw data are deleted: a trial or manual run keeps the project
            # MS-DIAL can reopen, with its containers beside the kept raw data and its library copy.
            record["left_as_before"] = {
                "steps": ["msdial_intermediates", "loaded_library_copy"],
                "reason": "No campaign approval is recorded for this unit, so its MS-DIAL containers stay beside "
                          "its inputs and the loaded-library copy stays in its output, as before.",
            }
        elif complete:
            raw = str(manifest.get("raw_directory") or "").strip()
            if raw:
                csv_path = Path(str(preparation.get("input_csv") or run_directory / "analysis_files.csv"))
                mztab_names = [Path(str(path)).name for path in artifacts.get("mztab") or []]
                relocation = relocate_intermediates(job_id, csv_path, Path(raw), output, mztab_names,
                                                    patience=patience)
                record["msdial_intermediates"] = relocation
                log(
                    f"MS-DIAL intermediates: moved {len(relocation['moved'])} file(s) "
                    f"({relocation.get('moved_bytes', 0) / 1e6:.1f} MB) into {INTERMEDIATES_DIRECTORY} in the "
                    f"output; {len(relocation['superseded'])} file(s) of earlier attempts are recorded as "
                    "superseded and stay in the raw directory."
                )
                for error in relocation["errors"]:
                    log("WARNING: MS-DIAL intermediates: " + error)
                if relocation["errors"]:
                    holds.append(_hold(
                        [BLOCKS_RAW_DELETION], "msdial_intermediates", job_id,
                        f"{len(relocation['pending']) or 'some'} container(s) could not be moved out of the raw "
                        "directory, and deleting it would delete them",
                        csv_path=str(csv_path), raw_directory=raw, output_directory=str(output),
                        mztab_names=mztab_names, pinned=_pinned(relocation), pending=relocation["pending"],
                        errors=relocation["errors"],
                    ))
            else:
                record["errors"].append("The unit manifest names no raw_directory; no container was moved.")

        if campaign:
            directories = [run_directory, *([output] if output.resolve() != run_directory.resolve() else [])]
            deleted: list[dict[str, Any]] = []
            for directory in directories:
                more, errors = delete_loaded_library_copies(directory, patience)
                deleted.extend(more)
                record["errors"].extend(errors)
            record["loaded_library_copies_deleted"] = deleted
            for item in deleted:
                log(f"Deleted MS-DIAL's loaded-library copy {item['name']} ({item['size_bytes']} bytes, "
                    f"sha256 {item['sha256']}).")
            left = _library_copies(directories)
            if left:
                log("WARNING: MS-DIAL's loaded-library copy could not be deleted, and it is held: "
                    + ", ".join(Path(path).name for path in left))
                holds.append(_hold(
                    [BLOCKS_SHARING], "loaded_library_copy", job_id,
                    "MS-DIAL's copy of every loaded library is still in the output", files=left,
                ))
            gone = {item["name"].casefold() for item in deleted}
            if gone:
                for kind, paths in list(artifacts.items()):
                    if kind != "records" and isinstance(paths, list):
                        artifacts[kind] = [path for path in paths if Path(str(path)).name.casefold() not in gone]
                if isinstance(artifacts.get("records"), list):
                    artifacts["records"] = [
                        item for item in artifacts["records"]
                        if Path(str((item or {}).get("path") or "")).name.casefold() not in gone
                    ]
    except Exception as error:  # a defect here must not cost the run its validation
        record["errors"].append(f"{type(error).__name__}: {error}")
        log(f"WARNING: finalising the Console run's outputs failed: {type(error).__name__}: {error}")
        # Nothing says what was left undone, so a person looks before this unit's outputs are shared or, for a
        # unit the campaign covers, its raw data deleted.
        holds.append(_hold(
            [BLOCKS_SHARING, *([BLOCKS_RAW_DELETION] if campaign else [])], "finalisation", job_id,
            f"finalisation stopped: {type(error).__name__}: {error}",
        ))

    record["holds"] = _hold_summaries(holds)
    if manifest_path is None:
        return record
    from .repository_reanalysis import update_manifest

    def change(current: dict[str, Any]) -> None:
        current["console_run_finalisation"] = record
        if reader_created:
            current["reader_created_files"] = reader_created
        earlier = standing_holds(current)
        if "msdial_intermediates" in record:
            # This run's set is the unit's set now. One an earlier run could not move is superseded with it.
            dropped = [item for item in earlier if item.get("step") == "msdial_intermediates"]
            if dropped:
                record["superseded_holds"] = [item.get("id") for item in dropped]
            earlier = [item for item in earlier if item.get("step") != "msdial_intermediates"]
        current[HOLDS] = [*earlier, *holds]

    try:
        update_manifest(manifest_path, change)
    except (OSError, ValueError) as error:
        record["errors"].append(f"unit manifest: {error}")
        if holds:
            log(f"WARNING: the finalisation holds could not be recorded in the unit manifest: {error}")
    return record
