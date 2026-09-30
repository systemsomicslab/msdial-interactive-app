"""What a finished Console run leaves behind, put where it belongs before anything is validated or deleted.

Called once from the production job, after the Console returns and before the mzTab-M is validated and the
unit's retained artifacts are inventoried, so that what is validated, inventoried and later shared is what the
run keeps. It never raises: a step that fails is recorded, and the run's own verdict is left to validation and
the gate.

THREE THINGS, decided by the user on 2026-09-30 for repository analysis units.

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
   (``AlignResult-<ts>.mzTab``), else, per input, the newest; it moves to ``output/<job>/msdial-intermediates/``
   with its path under the raw directory kept. The other sets of this unit's inputs are recorded as
   superseded and left to be deleted with the raw tree.

3. ``<project>_Loaded.msp2.dbs``, MS-DIAL's serialised copy of every library the run loaded, is deleted from
   the output once the run is over, its name, size and sha256 recorded. With the private VS21 pair it is a
   copy of the private library in every unit, and it can never be shared.

THE PROJECT CANNOT BE REOPENED AS IT STANDS. The ``.mdproject`` names the output directory and the ``.mddata``;
the ``.mddata`` names every input by its absolute raw path and every container by its absolute path beside
it, and the project loader reads the libraries back from ``<project>_Loaded.msp2.dbs``. After this, and after
the raw data are released, MS-DIAL stops at the missing library copy. Copying ``msdial-intermediates/`` back
under the raw directory, with the raw files re-downloaded, restores every path the ``.mddata`` names; the
library copy is not restorable without running again.
"""

from __future__ import annotations

import csv
import datetime as dt
import hashlib
import os
import re
import shutil
import urllib.parse
import uuid
from pathlib import Path, PureWindowsPath
from typing import Any, Callable

from .diagnostic_paths import INTERMEDIATES_DIRECTORY
from .sharing import PATH_POLICY, SharingContext, local_path_of


SCHEMA = "msdial-console-run-finalisation.v1"
REDACTION_RECORD = "mztab-redaction.local.json"
LOADED_LIBRARY_COPY = "*_Loaded.msp2.dbs"

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


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    from .repository_reanalysis import read_manifest

    try:
        return read_manifest(path) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


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


def _identity_line(index: int, database: str, library: Any) -> str:
    identity = library.identity
    parts = [library.name, "sha256:" + (identity.get("sha256") or "not recorded")]
    if isinstance(identity.get("bytes"), int):
        parts.append(f"{identity['bytes']} bytes")
    parts.append("private, not distributed")
    return f"MTD\tcustom[{index}]\t[,, MS-DIAL library file database[{database}], {'; '.join(parts)}]"


def _mtd_fields(text: str) -> list[str] | None:
    parts = text.split("\t", 2)
    return parts if len(parts) == 3 and parts[0] == "MTD" else None


def redact_mztab(path: str | Path, context: SharingContext) -> dict[str, Any]:
    """Rewrite one Console mzTab-M's metadata locations in place, atomically. Only MTD lines change.

    Returns what changed, with the original values under ``changes`` for a local-only record; nothing else
    returned carries a location. The metadata section is read into memory; the tables after it are copied
    as they are.
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
        os.replace(temporary, source)
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


def _move(source: Path, target: Path) -> str:
    """Move one file; on another volume, copy it, compare the bytes, then remove the source."""
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.replace(source, target)
        return "rename"
    except OSError:
        shutil.copy2(source, target)
        if _sha256(source) != _sha256(target):
            target.unlink(missing_ok=True)
            raise OSError(f"The copy of {source.name} does not match its source; the source was kept.")
        source.unlink()
        return "copy"


def relocate_intermediates(
    job_id: str,
    csv_path: Path,
    raw_directory: Path,
    output_directory: Path,
    mztab_names: list[str],
) -> dict[str, Any]:
    """Move the finalised run's MS-DIAL containers from beside its inputs into the unit's output."""
    raw_root = raw_directory.resolve()
    destination = output_directory.resolve() / job_id / INTERMEDIATES_DIRECTORY
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
        "errors": [],
        "project_reopen": dict(PROJECT_REOPEN),
    }
    inputs = _console_inputs(csv_path) if csv_path.is_file() else []
    if not inputs:
        record["errors"].append("The analysis CSV lists no input, so no container could be attributed.")
        return record

    exported = {match.group("ts") for name in mztab_names for match in [ALIGNMENT_EXPORT.fullmatch(name)] if match}
    alignment_ts = max(exported, key=lambda item: (len(item), item)) if exported else ""
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
        if not sets:
            if name not in {item["input"] for item in record["selection"]}:
                record["no_intermediates_found"].append(name)
            continue
        if alignment_ts and alignment_ts in sets:
            timestamp, rule = alignment_ts, "alignment_timestamp"
        else:
            timestamp, rule = max(sets, key=lambda item: newest(sets[item])), "newest"
        record["selection"].append({"input": name, "timestamp": timestamp, "rule": rule})
        chosen_timestamps.append(timestamp)
        for ts, paths in sets.items():
            target = chosen if ts == timestamp else superseded
            target.extend(("per_file", name, path, ts) for path in paths)
        groups[name] = {}  # an input listed twice is attributed once
    if not alignment_ts and chosen_timestamps:
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
        try:
            size = resolved.stat().st_size
            if target.exists():
                if target.stat().st_size == size and _sha256(target) == _sha256(resolved):
                    resolved.unlink()
                    method = "already_moved"
                else:
                    record["errors"].append(f"{relative.as_posix()}: a different file is already at the destination")
                    continue
            else:
                method = _move(resolved, target)
        except OSError as error:
            record["errors"].append(f"{relative.as_posix()}: {error}")
            continue
        record["moved"].append({"relative_path": relative.as_posix(), "path": str(target), "size_bytes": size,
                                "kind": kind, "input": name, "timestamp": timestamp, "method": method})
    for kind, name, path, timestamp in superseded:
        try:
            size = path.stat().st_size
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


# ---- 3. the loaded-library copy ------------------------------------------------------------------------


def delete_loaded_library_copies(output_directory: Path) -> tuple[list[dict[str, Any]], list[str]]:
    """Delete MS-DIAL's serialised copies of the loaded libraries, recording each by name, size and sha256."""
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
            path.unlink()
        except OSError as error:
            errors.append(f"{path.name}: {error}")
            continue
        deleted.append({"name": path.name, "size_bytes": size, "sha256": digest, "deleted_at": _now()})
    return deleted, errors


# ---- the one call ------------------------------------------------------------------------------------


def finalise_console_run(
    job_id: str,
    preparation: dict[str, Any],
    artifacts: dict[str, Any],
    exit_code: int | None,
    export_verification: dict[str, Any] | None,
    log: Callable[[str], None],
) -> dict[str, Any]:
    """Everything above, for one production job, once the Console has returned. Never raises.

    A laboratory run gets the mzTab-M's private-library redaction and nothing else. A repository unit gets all
    three: the containers move only for a run that produced every expected export, since a failed run's set
    is left to be deleted with the raw tree, and the loaded-library copy is deleted whatever the outcome.
    ``artifacts`` loses the paths of what was deleted, so no later record names a file that is gone.
    """
    record: dict[str, Any] = {"schema": SCHEMA, "job_id": job_id, "finalised_at": _now(), "errors": []}
    try:
        run_directory = Path(str(preparation.get("run_directory") or "")).expanduser()
        state = _read_json(Path(str(preparation.get("settings_file") or run_directory / "workflow-settings.json")))
        recorded = _read_json(Path(str(preparation.get("manifest") or run_directory / "run-manifest.json")))
        # The job's own preparation says whether this is a repository unit's run; the settings follow it.
        manifest_text = str(preparation.get("repository_run_manifest") or "").strip()
        state = {**{key: value for key, value in state.items() if key != "repository_run_manifest"},
                 **({"repository_run_manifest": manifest_text} if manifest_text else {})}
        context = SharingContext.for_state(state, run_directory=run_directory, recorded=recorded.get("libraries"))
        record["scope"] = "repository_unit" if manifest_text else "laboratory"
        if context.full:
            record["shared_path_policy"] = PATH_POLICY
        record["libraries"] = context.identities()
        complete = exit_code == 0 and not (export_verification or {}).get("missing")

        redactions: list[dict[str, Any]] = []
        for path in artifacts.get("mztab") or []:
            try:
                redactions.append(redact_mztab(path, context))
            except OSError as error:
                record["errors"].append(f"mzTab-M {Path(path).name}: {error}")
                log(f"WARNING: the mzTab-M {Path(path).name} could not be redacted: {error}")
        record["mztab_redaction"] = [{key: value for key, value in item.items() if key != "changes"}
                                     for item in redactions]
        for item in redactions:
            if item["changed"]:
                log(
                    f"Shared-path redaction of {item['file']}: {item['database_uri']} library URI(s), "
                    f"{item['ms_run_location']} ms_run location(s) and {item['other']} other value(s) "
                    f"rewritten; {item['identity_lines']} library identity line(s) added."
                )

        if not manifest_text:
            return record
        manifest_path = Path(manifest_text).expanduser().resolve()
        manifest = _read_json(manifest_path)
        output = Path(str(manifest.get("output_directory") or run_directory)).expanduser()
        changed = [item for item in redactions if item["changed"]]
        if changed:
            from .repository_reanalysis import _write_json

            local = manifest_path.parent / REDACTION_RECORD
            try:
                previous = _read_json(local)
                entries = [entry for entry in previous.get("files") or [] if isinstance(entry, dict)]
                entries.extend({"job_id": job_id, "recorded_at": _now(), **item} for item in changed)
                _write_json(local, {
                    "schema": "msdial-mztab-redaction.v1",
                    # The locations the shared mzTab-M no longer carries. It stays on this machine: no bundle,
                    # report or project archive includes it.
                    "sharing": "local_only",
                    "files": entries,
                })
                record["local_only"] = [str(local)]
            except (OSError, ValueError, TypeError) as error:
                record["errors"].append(f"{REDACTION_RECORD}: {error}")

        if complete:
            raw = str(manifest.get("raw_directory") or "").strip()
            if raw:
                csv_path = Path(str(preparation.get("input_csv") or run_directory / "analysis_files.csv"))
                relocation = relocate_intermediates(
                    job_id, csv_path, Path(raw), output,
                    [Path(str(path)).name for path in artifacts.get("mztab") or []],
                )
                record["msdial_intermediates"] = relocation
                log(
                    f"MS-DIAL intermediates: moved {len(relocation['moved'])} file(s) "
                    f"({relocation.get('moved_bytes', 0) / 1e6:.1f} MB) into {job_id}/{INTERMEDIATES_DIRECTORY} "
                    f"in the output; {len(relocation['superseded'])} file(s) of earlier attempts are recorded "
                    "as superseded and stay in the raw directory."
                )
                for error in relocation["errors"]:
                    log("WARNING: MS-DIAL intermediates: " + error)
            else:
                record["errors"].append("The unit manifest names no raw_directory; no container was moved.")

        deleted, errors = delete_loaded_library_copies(run_directory)
        if output.resolve() != run_directory.resolve():
            more, more_errors = delete_loaded_library_copies(output)
            deleted, errors = deleted + more, errors + more_errors
        record["loaded_library_copies_deleted"] = deleted
        record["errors"].extend(errors)
        for item in deleted:
            log(f"Deleted MS-DIAL's loaded-library copy {item['name']} ({item['size_bytes']} bytes, "
                f"sha256 {item['sha256']}).")
        gone = {item["name"].casefold() for item in deleted}
        if gone:
            for kind, paths in list(artifacts.items()):
                if kind != "records" and isinstance(paths, list):
                    artifacts[kind] = [path for path in paths if Path(str(path)).name.casefold() not in gone]
            if isinstance(artifacts.get("records"), list):
                artifacts["records"] = [item for item in artifacts["records"]
                                        if Path(str((item or {}).get("path") or "")).name.casefold() not in gone]

        from .repository_reanalysis import update_manifest

        def change(current: dict[str, Any]) -> None:
            current["console_run_finalisation"] = record

        try:
            update_manifest(manifest_path, change)
        except (OSError, ValueError) as error:
            record["errors"].append(f"unit manifest: {error}")
    except Exception as error:  # a defect here must not cost the run its validation
        record["errors"].append(f"{type(error).__name__}: {error}")
        log(f"WARNING: finalising the Console run's outputs failed: {type(error).__name__}: {error}")
    return record


