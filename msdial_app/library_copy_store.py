"""One stored copy of MS-DIAL's loaded-library copy per content, kept beside the analysis units.

WHAT IT KEEPS. MS-DIAL serialises every library a run loaded into ``<project>_Loaded.msp2.dbs`` beside the
project, and its loader reads the libraries back from there and from nowhere else. With the private VS21 pair
that file is a copy of the private library - 0.28 GB in negative mode, 0.86 GB in positive mode - in every
unit. Finalisation of a campaign unit deleted it, so a finalised project could not be opened again without
running it again. The user's decision of 2026-10-11: keep one copy per library set and annotation settings in a
local-only store beside the units, keep the containers in ``output/msdial-intermediates`` as before, and reopen
a unit through a restore step (project_restore):

    <workspace_root>/_library_copies/
        STORE.json                           the store's own record; local only, like everything below
        o/<object_id>/library-copy.dbs       the bytes, writable, keeping the .dbs suffix every sharing rule refuses
        o/<object_id>/entry.json             the object record
        o/<object_id>/claims/<claim>.json    one per unit run that stored it, equalled it or was attached to it
        incoming/<uuid>/                     an ingest under way: intent.json first, then the copy
        quarantine/<uuid>/                   a copy with an object's content key and other content
        locks/<object_id>.lock               download_store.StoreLock

It lies outside every unit's workspace and is named like nothing a unit, an accession or a repository is
(repository_reanalysis.RESERVED_WORKSPACE_NAMES), so no raw cleanup, campaign plan or download-store check
walks into it.

IDENTITY, read in one pass over the file (read_copy_identity). MS-DIAL writes the .dbs as a new zip on every
save, so two copies of the same libraries differ in the DOS time and date of each entry and nowhere else.

- ``content_key``: the sha256 of the sorted (name, CRC-32, size) of every entry, names with ``/`` for ``\\``,
  under a version prefix. It finds the object a copy may equal. ``object_id`` is its first 32 characters.
- ``normalized_sha256``: the sha256 of the file with every DOS time and date field set to zero, in the local
  headers and in the central directory. Equal means the same bytes but for the timestamps.
- ``sha256``: the file as written, as finalisation recorded it before 0.5.37.

A copy is recognised when it holds exactly one ``Storage`` entry and every other entry is a
MetabolomicsDB, ProteomicsDB or EadLipidomicsDB database (DataBaseStorage.cs). A file that is not - not a zip,
or laid out otherwise - is keyed by its sha256 and is only ever found equal to the same bytes.

WHAT IS NEVER DONE. A copy is removed only once the store holds one proven equal: the same normalised bytes,
re-read from the object under its lock rather than taken from entry.json, or entries that decompress to the
same content. A copy that cannot be stored stays where it is, and its caller holds it from sharing. Nothing
here writes an absolute path into a record, a log line or an error text: the store and every file in it are
named relative to the workspace root, and an OSError is described by its codes and the file's relative name
(describe_error), never by Python's own text, which carries the absolute path.

The module also runs as ``python -m msdial_app.library_copy_store`` for three migration steps, each a preview
unless --confirmed is given: ``seed`` copies a verified .dbs into the store (never moving or linking it),
``attach`` records the object an already-finalised unit's deleted copy is equivalent to, and ``status``
(with ``--repair``) reports the store and completes an ingest a crash interrupted.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import sys
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Callable, Iterable

from . import __version__
from .diagnostic_paths import extended_path, plain_path
from .download_store import (
    LOCK_STALE_SECONDS,
    StoreError,
    StoreLock,
    StoreLockTimeout,
    _read_json,
    _replace,
    _unit_file_name,
    _write_json_atomic,
    unlink_tree,
)


STORE_DIRECTORY_NAME = "_library_copies"
# Beside the store under the workspace root: a repository unit's Console temporary files, one directory per
# job, and what project_restore keeps for a restore it made.
CONSOLE_TEMP_DIRECTORY_NAME = "_tmp"
VIEWING_DIRECTORY_NAME = "_viewing"
OBJECT_FILE = "library-copy.dbs"
LOADED_LIBRARY_COPY_SUFFIX = "_Loaded.msp2.dbs"
STORE_SCHEMA = "msdial-library-copy-store.v1"
OBJECT_SCHEMA = "msdial-library-copy-object.v1"
CLAIM_SCHEMA = "msdial-library-copy-claim.v1"
INTENT_SCHEMA = "msdial-library-copy-intent.v1"
QUARANTINE_SCHEMA = "msdial-library-copy-quarantine.v1"
COPY_SCHEMA = "msdial-loaded-library-copy.v1"
RECORD_SCHEMA = "msdial-library-copy-record.v1"
SETTINGS_SCHEMA = "msdial-annotation-settings.v1"
CONTENT_KEY_VERSION = 1
CONTENT_KEY_PREFIX = "msdial-dbs-content-key.v1\n"
LOCK_TIMEOUT_SECONDS = 600.0

STORED = "stored"
DEDUPLICATED = "deduplicated"
DEDUPLICATED_BY_CONTENT = "deduplicated_by_content"
QUARANTINED = "quarantined"
# Every outcome but a quarantined copy's leaves the run reopenable from the store.
RESTORABLE_OUTCOMES = frozenset({STORED, DEDUPLICATED, DEDUPLICATED_BY_CONTENT})

DBS_DATABASE = re.compile(r"^(?:MetabolomicsDB|ProteomicsDB|EadLipidomicsDB)/[^/]+(?:/DataBase)?$")
_LOCAL_HEADER = b"PK\x03\x04"
_CENTRAL_RECORD = b"PK\x01\x02"
_CENTRAL_RECORD_SIZE = 46
_CHUNK = 4 * 1024 * 1024
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")
_ROOTED_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\|/)")
# The method-file keys that decide what MS-DIAL serialises into the .dbs: the libraries, the annotators and
# their settings. Every key naming an annotation is one; these are the others.
_ANNOTATION_KEYS = frozenset({
    "msp file path", "lbm file path", "text db file path", "isotope text db file path", "ion mode",
    "target omics", "searched adduct ions", "searched lipid class", "solvent type",
})
_ANNOTATOR_TABLES = ("msp_annotator_settings.tsv", "text_annotator_settings.tsv")
_ANNOTATOR_PATH_COLUMNS = ("msp_file_path", "text_db_file_path")


class LibraryCopyStoreError(OSError):
    """The store could not take a copy, or refused to. An OSError, so every caller that holds a copy it could
    not move or delete holds this one too. Its text names no absolute path."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _call(step: Callable[[], Any]) -> Any:
    return step()


def _relative_name(path: Any, root: Path | None) -> str:
    text = plain_path(os.fsdecode(path)) if isinstance(path, (str, bytes, os.PathLike)) else str(path)
    candidate = Path(text)
    if root is not None:
        try:
            return candidate.resolve().relative_to(Path(root).resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return candidate.name or text


def describe_error(error: BaseException, root: Path | None = None) -> str:
    """An error as a record, a log line or a hold may carry it: its type, its Windows or POSIX code and its
    text, and the files it names relative to ``root`` (else by name). Python's own text for an OSError holds
    the absolute path, which is why it is never used."""
    if isinstance(error, OSError) and not isinstance(error, LibraryCopyStoreError) and (
        error.strerror or error.errno
    ):
        parts = [type(error).__name__]
        winerror = getattr(error, "winerror", None)
        if winerror:
            parts.append(f"[WinError {winerror}]")
        elif error.errno:
            parts.append(f"[Errno {error.errno}]")
        if error.strerror:
            parts.append(str(error.strerror).strip())
        names = [_relative_name(item, root) for item in (error.filename, error.filename2) if item]
        return " ".join(parts) + (": " + " -> ".join(names) if names else "")
    text = str(error)
    if root is not None:
        for form in {str(root), str(Path(root).resolve())}:
            text = re.sub(re.escape(form), "<workspace root>", text, flags=re.IGNORECASE)
    return f"{type(error).__name__}: {text}" if text else type(error).__name__


# ---- identity --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class CopyIdentity:
    """What one library copy is, read in one pass. See the module docstring."""

    size_bytes: int
    sha256: str
    normalized_sha256: str
    content_key: str
    content_key_kind: str
    recognised: bool
    entries: tuple[dict[str, Any], ...] = ()
    dos_times: tuple[dict[str, Any], ...] = ()
    storage_sha256: str | None = None
    reason: str = ""

    @property
    def object_id(self) -> str:
        return self.content_key[:32]

    def record(self, name: str) -> dict[str, Any]:
        """The copy as a unit manifest records it: no path, and no entry content."""
        record: dict[str, Any] = {
            "name": name,
            "size_bytes": self.size_bytes,
            "sha256": self.sha256,
            "normalized_sha256": self.normalized_sha256,
            "content_key": self.content_key,
            "content_key_kind": self.content_key_kind,
            "content_key_version": CONTENT_KEY_VERSION,
            "recognised": self.recognised,
            "dos_times": [dict(item) for item in self.dos_times],
            "storage_sha256": self.storage_sha256,
            "object_id": self.object_id,
        }
        if not self.recognised:
            record["not_recognised_because"] = self.reason
        return record


def dbs_structure(names: Iterable[str]) -> bool:
    """Whether zip entry names are laid out as MS-DIAL's serialised library copy (DataBaseStorage.cs).

    Exactly one ``Storage`` entry, and every other entry one database: ``<kind>DB/<id>/DataBase``, or
    ``<kind>DB/<id>`` as older builds wrote it. MS-DIAL names entries with Path.Combine, so on disk they hold
    backslashes; they are compared with ``/``.
    """
    normalised = [str(name).replace("\\", "/") for name in names]
    others = [name for name in normalised if name != "Storage"]
    return (
        len(normalised) - len(others) == 1
        and bool(others)
        and all(DBS_DATABASE.fullmatch(name) for name in others)
    )


def _decoded_name(raw: bytes, flags: int) -> str:
    return raw.decode("utf-8" if flags & 0x800 else "cp437")


def _zip_layout(path: Path) -> tuple[dict[str, Any] | None, str]:
    """The entries of a recognised copy and where its timestamps lie, or None and why it is not one."""
    try:
        with zipfile.ZipFile(extended_path(path)) as archive:
            infos = archive.infolist()
            start = int(archive.start_dir)
            names = [info.filename.replace("\\", "/") for info in infos]
            if not dbs_structure(names):
                return None, "its entries are not one Storage and MS-DIAL databases"
            if any(info.flag_bits & 0x1 for info in infos):
                return None, "an entry is encrypted"
            storage = next(info for info, name in zip(infos, names) if name == "Storage")
            digest = hashlib.sha256()
            try:
                with archive.open(storage) as handle:
                    for chunk in iter(lambda: handle.read(_CHUNK), b""):
                        digest.update(chunk)
                storage_sha256: str | None = digest.hexdigest()
            except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
                storage_sha256 = None
    except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error, KeyError):
        return None, "it is not a readable zip archive"
    patches: list[tuple[int, int]] = []
    dos_times: list[dict[str, Any]] = []
    with open(extended_path(path), "rb") as handle:
        handle.seek(start)
        for info, name in zip(infos, names):
            offset = handle.tell()
            fixed = handle.read(_CENTRAL_RECORD_SIZE)
            if len(fixed) < _CENTRAL_RECORD_SIZE or fixed[:4] != _CENTRAL_RECORD:
                return None, "its central directory does not read as zipfile lists it"
            flags, = struct.unpack_from("<H", fixed, 8)
            time, date = struct.unpack_from("<HH", fixed, 12)
            name_length, extra_length, comment_length = struct.unpack_from("<HHH", fixed, 28)
            raw_name = handle.read(name_length)
            handle.seek(extra_length + comment_length, os.SEEK_CUR)
            try:
                decoded = _decoded_name(raw_name, flags).replace("\\", "/")
            except UnicodeDecodeError:
                return None, "an entry name cannot be decoded"
            if decoded != name:
                return None, "its central directory does not read as zipfile lists it"
            patches.append((offset + 12, 4))
            dos_times.append({"entry": name, "time": time, "date": date})
        for info in infos:
            handle.seek(info.header_offset)
            if handle.read(4) != _LOCAL_HEADER:
                return None, "a local header is not where the central directory puts it"
            patches.append((info.header_offset + 10, 4))
    entries = [
        {"name": name, "method": info.compress_type, "crc32": info.CRC, "compressed_size": info.compress_size,
         "size": info.file_size}
        for info, name in zip(infos, names)
    ]
    return {"entries": entries, "dos_times": dos_times, "patches": patches,
            "storage_sha256": storage_sha256}, ""


def _hash_file(path: Path, patches: list[tuple[int, int]]) -> tuple[str, str, int]:
    """(sha256, the sha256 with every patched range read as zeros, size), in one pass."""
    digest = hashlib.sha256()
    normalized = hashlib.sha256()
    ranges = sorted((start, start + length) for start, length in patches)
    position = 0
    with open(extended_path(path), "rb") as handle:
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
            end = position + len(chunk)
            hits = [(start, stop) for start, stop in ranges if start < end and stop > position]
            if hits:
                patched = bytearray(chunk)
                for start, stop in hits:
                    first, last = max(start, position) - position, min(stop, end) - position
                    patched[first:last] = bytes(last - first)
                normalized.update(patched)
            else:
                normalized.update(chunk)
            position = end
    return digest.hexdigest(), normalized.hexdigest(), position


def content_key_of(entries: Iterable[dict[str, Any]]) -> str:
    lines = sorted((str(item["name"]), int(item["crc32"]), int(item["size"])) for item in entries)
    text = CONTENT_KEY_PREFIX + "".join(f"{name}\t{crc:08x}\t{size}\n" for name, crc, size in lines)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_copy_identity(path: str | Path) -> CopyIdentity:
    """The identity of one library copy, read in one streaming pass (and a read of its central directory).

    Raises OSError when the file cannot be read; a file that is not a recognised copy is not an error.
    """
    source = Path(path)
    layout, reason = _zip_layout(source)
    sha256, normalized, size = _hash_file(source, (layout or {}).get("patches") or [])
    if layout is None:
        return CopyIdentity(size, sha256, sha256, sha256, "file_sha256", False, reason=reason)
    return CopyIdentity(
        size_bytes=size,
        sha256=sha256,
        normalized_sha256=normalized,
        content_key=content_key_of(layout["entries"]),
        content_key_kind="zip_entries",
        recognised=True,
        entries=tuple(layout["entries"]),
        dos_times=tuple(layout["dos_times"]),
        storage_sha256=layout["storage_sha256"],
    )


def entry_table(path: str | Path) -> list[dict[str, Any]] | None:
    """A zip's entries as entry.json records them, from its central directory alone; None if it is no zip."""
    try:
        with zipfile.ZipFile(extended_path(path)) as archive:
            return [
                {"name": info.filename.replace("\\", "/"), "method": info.compress_type, "crc32": info.CRC,
                 "compressed_size": info.compress_size, "size": info.file_size}
                for info in archive.infolist()
            ]
    except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error):
        return None


def is_library_copy_file(path: str | Path) -> bool:
    """Whether a file, whatever its name, is laid out as MS-DIAL's library copy."""
    try:
        with zipfile.ZipFile(extended_path(path)) as archive:
            return dbs_structure(archive.namelist())
    except (OSError, zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error):
        return False


def _testzip(path: Path) -> str | None:
    """None when every entry decompresses to its CRC, else what failed."""
    try:
        with zipfile.ZipFile(extended_path(path)) as archive:
            bad = archive.testzip()
    except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error) as error:
        return f"{type(error).__name__}: {error}"
    return None if bad is None else f"entry {bad.replace(chr(92), '/')} does not match its CRC"


def _compare_inflated(copy: Path, stored: Path) -> str:
    """'equal', 'different', 'copy_damaged' or 'object_damaged': every entry decompressed, side by side.

    zipfile checks each entry's CRC as it reaches the end, so a damaged side is told apart from a difference.
    """
    try:
        left_archive = zipfile.ZipFile(extended_path(copy))
    except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error):
        return "copy_damaged"
    try:
        try:
            right_archive = zipfile.ZipFile(extended_path(stored))
        except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError, struct.error):
            return "object_damaged"
        with left_archive, right_archive:
            left = {info.filename.replace("\\", "/"): info for info in left_archive.infolist()}
            right = {info.filename.replace("\\", "/"): info for info in right_archive.infolist()}
            if sorted(left) != sorted(right):
                return "different"
            for name in sorted(left):
                try:
                    left_handle = left_archive.open(left[name])
                except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
                    return "copy_damaged"
                try:
                    right_handle = right_archive.open(right[name])
                except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
                    left_handle.close()
                    return "object_damaged"
                with left_handle, right_handle:
                    while True:
                        try:
                            a = left_handle.read(_CHUNK)
                        except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
                            return "copy_damaged"
                        try:
                            b = right_handle.read(len(a) if a else _CHUNK)
                        except (zipfile.BadZipFile, EOFError, ValueError, NotImplementedError):
                            return "object_damaged"
                        if a != b:
                            return "different"
                        if not a:
                            break
    finally:
        left_archive.close()
    return "equal"


def _sha256(path: Path) -> str:
    return _hash_file(path, [])[0]


# ---- the libraries and settings a copy was written under -------------------------------------------------


def library_names(libraries: Iterable[Any]) -> list[dict[str, Any]]:
    """A run's library records reduced to what entry.json may carry: name, sha256, private. Never a path."""
    found = []
    for item in libraries or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("filename") or "").strip()
        if name:
            found.append({"name": name, "sha256": str(item.get("sha256") or "").strip().casefold(),
                          "private": bool(item.get("private"))})
    return found


def _library_set(libraries: Iterable[Any]) -> set[tuple[str, str]]:
    return {(item["name"].casefold(), item["sha256"]) for item in library_names(libraries)}


def _path_free(value: str) -> str:
    text = value.strip()
    return PureWindowsPath(text).name if _ROOTED_PATH.match(text) else text


def _annotator_rows(path: Path) -> list[dict[str, str]] | None:
    if not path.is_file():
        return None
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        rows = []
        for row in csv.DictReader(handle, delimiter="\t"):
            rows.append({
                str(key).strip(): (_path_free(str(value or "")) if str(key).strip() in _ANNOTATOR_PATH_COLUMNS
                                   else str(value or "").strip())
                for key, value in row.items() if key is not None
            })
        return rows


def annotation_settings(run_directory: str | Path, method_file: str | Path | None = None) -> dict[str, Any] | None:
    """The annotation settings a run's library copy was written under, with every path reduced to its name.

    The method file's annotation lines (each key's last line, as the Console keeps the last) and the per-MSP
    and per-text-database annotator tables beside it. A library at another location gives the same settings:
    MS-DIAL reads the libraries back from the copy, not from their paths. None when the run directory holds
    no method file.
    """
    directory = Path(run_directory)
    method = Path(method_file) if method_file else directory / "method.txt"
    try:
        text = method.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    lines: dict[str, str] = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, value = stripped.split(":", 1)
        key = " ".join(key.split()).casefold()
        if "annotat" in key or key in _ANNOTATION_KEYS:
            lines[key] = _path_free(value)
    tables = {}
    for name in _ANNOTATOR_TABLES:
        try:
            rows = _annotator_rows(directory / name)
        except OSError:
            rows = None
        if rows is not None:
            tables[name] = rows
    settings = {"method": dict(sorted(lines.items())), "annotators": tables}
    digest = hashlib.sha256(json.dumps(settings, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()
    return {"schema": SETTINGS_SCHEMA, **settings, "sha256": digest}


def settings_differences(left: dict[str, Any] | None, right: dict[str, Any] | None) -> list[str]:
    """What differs between two annotation settings records, by method key or annotator table."""
    if not left or not right:
        return ["no annotation settings are recorded" + (" for the unit" if not left else " for the object")]
    found = []
    a, b = left.get("method") or {}, right.get("method") or {}
    found.extend(f"method line {key!r}" for key in sorted(set(a) | set(b)) if a.get(key) != b.get(key))
    a, b = left.get("annotators") or {}, right.get("annotators") or {}
    found.extend(f"annotator table {name}" for name in sorted(set(a) | set(b)) if a.get(name) != b.get(name))
    return found


# ---- where the store is -------------------------------------------------------------------------------------


def workspace_root_for(manifest: dict[str, Any], manifest_path: str | Path | None = None) -> tuple[Path | None, str]:
    """The workspace root a unit lies under, and how it was found; or None and why not. Never raises.

    The lease's download_cache.workspace_root, when it names a directory; else the unit's workspace read as
    <workspace root>/<repository>/<accession>/<unit> (a split part included) or
    <workspace root>/<repository>/<accession> (a unit with no analysis unit id).
    """
    cache = manifest.get("download_cache") if isinstance(manifest.get("download_cache"), dict) else {}
    text = str((cache or {}).get("workspace_root") or "").strip()
    if text:
        path = Path(text).expanduser()
        if path.is_absolute() and path.is_dir():
            return path, "download_cache.workspace_root"
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    repository = str((project or {}).get("repository") or "").strip().casefold()
    accession = str((project or {}).get("accession") or "").strip().casefold()
    workspace = str(manifest.get("workspace") or "").strip()
    if not workspace and manifest_path:
        located = Path(str(manifest_path))
        if located.parent.name.casefold() == "provenance":
            workspace = str(located.parent.parent)
    if workspace and repository and accession:
        unit = Path(workspace).expanduser()
        if unit.is_absolute():
            parents = unit.parents
            if len(parents) >= 3 and parents[0].name.casefold() == accession and parents[1].name.casefold() == repository:
                return parents[2], "workspace"
            if len(parents) >= 2 and unit.name.casefold() == accession and parents[0].name.casefold() == repository:
                return parents[1], "workspace"
    return None, (
        "its manifest records no download_cache.workspace_root, and its workspace is not "
        "<workspace root>/<repository>/<accession>[/<unit>]"
    )


def library_copy_store_for(
    manifest: dict[str, Any], manifest_path: str | Path | None = None
) -> tuple["LibraryCopyStore | None", str]:
    """The library-copy store of a unit's workspace root, or None and why there is none. Never raises."""
    root, how = workspace_root_for(manifest, manifest_path)
    return (LibraryCopyStore(root), how) if root is not None else (None, how)


def unit_ref(manifest: dict[str, Any], manifest_path: str | Path | None, root: Path | None) -> dict[str, Any]:
    """A unit as the store names it: repository, accession, unit id and its manifest relative to the root."""
    project = manifest.get("project") if isinstance(manifest.get("project"), dict) else {}
    manifest_text = ""
    if manifest_path:
        manifest_text = _relative_name(manifest_path, root) if root is not None else Path(str(manifest_path)).name
    return {
        "repository": str((project or {}).get("repository") or ""),
        "accession": str((project or {}).get("accession") or ""),
        "unit_id": str((project or {}).get("analysis_unit_id") or ""),
        "manifest": manifest_text,
    }


def unit_label(unit: dict[str, Any] | None) -> str | None:
    if not unit:
        return None
    return "/".join(str(unit.get(key) or "-") for key in ("repository", "accession", "unit_id"))


def _component(value: Any) -> str:
    text = _SAFE_COMPONENT.sub("_", str(value or "").strip()).strip("._")
    return (text or "_")[:64]


def claim_name(unit: dict[str, Any] | None, job_id: str, action: str) -> str:
    unit = unit or {}
    unit_part = _unit_file_name(str(unit.get("unit_id") or "_"))[: -len(".json")] if unit.get("unit_id") else "_"
    return "~".join([_component(unit.get("repository")), _component(unit.get("accession")), unit_part,
                     _component(job_id), _component(action)]) + ".json"


def _object_record(
    identity: CopyIdentity, *, how: str, unit: dict[str, Any] | None, job_id: str, method: str,
    libraries: list[dict[str, Any]], settings: dict[str, Any] | None, testzip: str,
    repairs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """An object's entry.json: its identity, how it came in, and its libraries and settings. No path."""
    return {
        "schema": OBJECT_SCHEMA,
        "object_id": identity.object_id,
        "content_key": identity.content_key,
        "content_key_kind": identity.content_key_kind,
        "content_key_version": CONTENT_KEY_VERSION,
        "normalized_sha256": identity.normalized_sha256,
        "sha256": identity.sha256,
        "size_bytes": identity.size_bytes,
        "recognised": identity.recognised,
        "entries": [dict(item) for item in identity.entries],
        "storage_sha256": identity.storage_sha256,
        "testzip": {"result": testzip, "checked_at": _now()},
        "ingested": {
            "at": _now(), "how": how, "unit": unit_label(unit), "job_id": job_id or None, "method": method,
            "interactive_version": __version__,
        },
        "libraries": libraries,
        "annotation_settings": settings,
        "sharing": "local_only",
        **({"repairs": repairs} if repairs else {}),
    }


# ---- the store ---------------------------------------------------------------------------------------------


class LibraryCopyStore:
    """One workspace root's store of library copies. Every name it returns is relative to that root."""

    def __init__(self, workspace_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root)
        self.root = self.workspace_root / STORE_DIRECTORY_NAME

    # -- names

    def relative(self, path: str | Path) -> str:
        return _relative_name(path, self.workspace_root)

    def object_directory(self, object_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", str(object_id or "")):
            raise LibraryCopyStoreError(f"{object_id!r} is not a library-copy object id.")
        return self.root / "o" / object_id

    def object_path(self, object_id: str) -> Path:
        return self.object_directory(object_id) / OBJECT_FILE

    def entry_path(self, object_id: str) -> Path:
        return self.object_directory(object_id) / "entry.json"

    def claims_directory(self, object_id: str) -> Path:
        return self.object_directory(object_id) / "claims"

    def lock(self, object_id: str, job_id: str = "") -> StoreLock:
        return StoreLock(self.root / "locks" / f"{object_id}.lock", job_id=job_id)

    def entry(self, object_id: str) -> dict[str, Any] | None:
        return _read_json(self.entry_path(object_id))

    def object_ids(self) -> list[str]:
        """Every object with a record or a file; an object directory an interrupted install left empty is none."""
        directory = self.root / "o"
        if not directory.is_dir():
            return []
        return sorted(
            item.name for item in directory.iterdir()
            if re.fullmatch(r"[0-9a-f]{32}", item.name)
            and ((item / "entry.json").is_file() or (item / OBJECT_FILE).is_file())
        )

    def _ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        record = self.root / "STORE.json"
        if not record.is_file():
            _write_json_atomic(record, {
                "schema": STORE_SCHEMA,
                # MS-DIAL's copies of the loaded libraries, the private ones among them: nothing here is shared.
                "sharing": "local_only",
                "created_at": _now(),
                "created_by": f"Interactive {__version__}",
            })

    def _locked(self, object_id: str, job_id: str, timeout: float | None = None) -> StoreLock:
        self._ensure()
        lock = self.lock(object_id, job_id)
        try:
            return lock.acquire(timeout=LOCK_TIMEOUT_SECONDS if timeout is None else timeout)
        except (StoreError, OSError) as error:
            # A lock that could not be taken is a copy that could not be stored, which its caller holds.
            raise LibraryCopyStoreError(
                f"the store's lock on object {object_id} was not taken: {describe_error(error, self.workspace_root)}"
            ) from error

    @staticmethod
    def _release(lock: StoreLock) -> None:
        try:
            lock.release()
        except (StoreError, OSError):
            pass  # left to be judged stale; what it guarded is already done

    # -- verification

    def problem(self, object_id: str, entry: dict[str, Any] | None = None, *, full: bool = False) -> str | None:
        """What is wrong with an object, or None: its file, its size, its entries, and with ``full`` its bytes."""
        entry = entry if entry is not None else self.entry(object_id)
        if entry is None:
            return "its record is absent"
        path = self.object_path(object_id)
        try:
            size = os.stat(extended_path(path)).st_size
        except FileNotFoundError:
            return "its file is missing"
        except OSError as error:
            return f"its file cannot be read ({describe_error(error, self.workspace_root)})"
        if size != entry.get("size_bytes"):
            return f"its file holds {size} bytes and its record {entry.get('size_bytes')}"
        if entry.get("recognised"):
            if entry_table(path) != entry.get("entries"):
                return "its entries differ from its record"
        if not full:
            return None
        try:
            identity = read_copy_identity(path)
        except OSError as error:
            return f"its file cannot be read ({describe_error(error, self.workspace_root)})"
        if identity.normalized_sha256 != entry.get("normalized_sha256"):
            return "its bytes differ from its record"
        return None

    # -- claims

    def claim(
        self,
        object_id: str,
        *,
        unit: dict[str, Any] | None,
        job_id: str,
        action: str,
        name: str,
        copy: dict[str, Any] | None,
        outcome: str,
    ) -> str:
        file = claim_name(unit, job_id, action)
        _write_json_atomic(self.claims_directory(object_id) / file, {
            "schema": CLAIM_SCHEMA,
            "object_id": object_id,
            "unit": unit_label(unit),
            "manifest": (unit or {}).get("manifest") or None,
            "job_id": job_id,
            "action": action,
            "name": name,
            "sha256": (copy or {}).get("sha256"),
            "dos_times": (copy or {}).get("dos_times") or [],
            "outcome": outcome,
            "recorded_at": _now(),
            "by": f"Interactive {__version__}",
        })
        return file

    def claims(self, object_id: str) -> list[dict[str, Any]]:
        directory = self.claims_directory(object_id)
        found = []
        for path in sorted(directory.glob("*.json")) if directory.is_dir() else []:
            if path.name.startswith("."):
                continue
            try:
                record = _read_json(path)
            except StoreError:
                record = None
            if isinstance(record, dict):
                found.append(record)
        return found

    # -- putting a copy in

    def put(
        self,
        source: str | Path,
        identity: CopyIdentity,
        *,
        unit: dict[str, Any] | None = None,
        job_id: str = "",
        libraries: Iterable[Any] = (),
        settings: dict[str, Any] | None = None,
        how: str = "finalisation",
        patience: Any = None,
        keep_source: bool = False,
    ) -> dict[str, Any]:
        """Store one copy, or prove the store holds it already and remove it. Returns the copy's record.

        Under the object's lock: a content key the store does not hold is staged through incoming/ (its intent
        recorded first) and installed; one it holds is compared with the object, re-read now, and the copy is
        removed only when equal - else, if it decompresses to other content, it is moved to quarantine. A
        damaged object is replaced by this copy and kept beside it. ``keep_source`` copies and leaves the
        source (a seed). Raises LibraryCopyStoreError (an OSError) when the copy could not be stored; the copy
        is then where it was.
        """
        lock = self._locked(identity.object_id, job_id)
        try:
            return self._put_locked(Path(source), identity, unit=unit, job_id=job_id, libraries=libraries,
                                    settings=settings, how=how, patience=patience, keep_source=keep_source)
        finally:
            self._release(lock)

    def _put_locked(
        self, source: Path, identity: CopyIdentity, *, unit: dict[str, Any] | None, job_id: str,
        libraries: Iterable[Any], settings: dict[str, Any] | None, how: str, patience: Any, keep_source: bool,
    ) -> dict[str, Any]:
        run = patience.run if patience is not None else _call
        object_id = identity.object_id
        record = identity.record(source.name)
        entry = self.entry(object_id)
        if entry is None:
            return self._install(source, identity, record, unit=unit, job_id=job_id, libraries=libraries,
                                 settings=settings, how=how, run=run, keep_source=keep_source)
        problem = self.problem(object_id, entry)
        if problem is None:
            if identity.content_key_kind != entry.get("content_key_kind"):
                raise LibraryCopyStoreError(
                    f"object {object_id} is keyed by {entry.get('content_key_kind')} and this copy by "
                    f"{identity.content_key_kind}; it was not stored"
                )
            if identity.normalized_sha256 == entry.get("normalized_sha256"):
                # entry.json says the object is this copy; the object's own bytes, re-read now, must say so too.
                problem = self.problem(object_id, entry, full=True)
                if problem is None:
                    verified = "normalized_sha256" if identity.recognised else "sha256"
                    return self._settle_equal(source, identity, record, entry, DEDUPLICATED, verified,
                                              unit=unit, job_id=job_id, libraries=libraries, settings=settings,
                                              how=how, run=run, keep_source=keep_source)
            else:
                verdict = _compare_inflated(source, self.object_path(object_id))
                if verdict == "equal":
                    return self._settle_equal(source, identity, record, entry, DEDUPLICATED_BY_CONTENT,
                                              "decompressed_content", unit=unit, job_id=job_id,
                                              libraries=libraries, settings=settings, how=how, run=run,
                                              keep_source=keep_source)
                if verdict == "copy_damaged":
                    raise LibraryCopyStoreError(f"{source.name} does not decompress to its own CRCs; it was not stored")
                if verdict == "different":
                    return self._quarantine(source, identity, record, object_id, unit=unit, job_id=job_id,
                                            how=how, run=run, keep_source=keep_source)
                problem = "its entries do not decompress to their CRCs"
        return self._install(source, identity, record, unit=unit, job_id=job_id, libraries=libraries,
                             settings=settings, how=how, run=run, keep_source=keep_source, damaged=problem,
                             previous=entry)

    def _settle_equal(
        self, source: Path, identity: CopyIdentity, record: dict[str, Any], entry: dict[str, Any], outcome: str,
        verified: str, *, unit: dict[str, Any] | None, job_id: str, libraries: Iterable[Any],
        settings: dict[str, Any] | None, how: str, run: Callable[[Callable[[], Any]], Any], keep_source: bool,
    ) -> dict[str, Any]:
        object_id = identity.object_id
        names = library_names(libraries)
        if (names and not entry.get("libraries")) or (settings and not entry.get("annotation_settings")):
            # A seeded object learns its libraries and settings from the first run proven to equal it.
            updated = dict(entry)
            if names and not entry.get("libraries"):
                updated["libraries"] = names
            if settings and not entry.get("annotation_settings"):
                updated["annotation_settings"] = settings
            _write_json_atomic(self.entry_path(object_id), updated)
        claim = None
        if unit is not None:
            claim = self.claim(object_id, unit=unit, job_id=job_id, action=how, name=source.name, copy=record,
                               outcome=outcome)
        if not keep_source:
            run(lambda: os.unlink(extended_path(source)))
        return {**record, "outcome": outcome, "verified_by": verified, "claim": claim,
                "removed_at": None if keep_source else _now(),
                "annotation_settings_sha256": (settings or {}).get("sha256")}

    def _stage(self, source: Path, staged: Path, identity: CopyIdentity, run: Callable[[Callable[[], Any]], Any],
               copy: bool) -> str:
        """Put the copy at ``staged``: renamed on one volume, else copied and compared. Returns how."""
        if not copy:
            try:
                run(lambda: os.replace(extended_path(source), extended_path(staged)))
                return "rename"
            except PermissionError:
                raise  # still held after the patience: a copy could not remove the source either
            except OSError:
                pass  # another volume
        try:
            shutil.copyfile(extended_path(source), extended_path(staged))
            if _sha256(staged) != identity.sha256:
                raise LibraryCopyStoreError(f"the copy of {source.name} does not match it; it was not stored")
        except BaseException:
            try:
                os.unlink(extended_path(staged))
            except OSError:
                pass
            raise
        return "copy"

    def _install(
        self, source: Path, identity: CopyIdentity, record: dict[str, Any], *, unit: dict[str, Any] | None,
        job_id: str, libraries: Iterable[Any], settings: dict[str, Any] | None, how: str,
        run: Callable[[Callable[[], Any]], Any], keep_source: bool, damaged: str | None = None,
        previous: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        object_id = identity.object_id
        tested = _testzip(source) if identity.recognised else None
        if tested is not None:
            raise LibraryCopyStoreError(f"{source.name} is not a readable library copy ({tested}); it was not stored")
        names = library_names(libraries) or library_names((previous or {}).get("libraries") or [])
        incoming = self.root / "incoming" / uuid.uuid4().hex
        incoming.mkdir(parents=True)
        # Before the copy moves, so that a crash leaves a record of whose copy is in incoming/ and repair can
        # complete the ingest rather than set it aside.
        _write_json_atomic(incoming / "intent.json", {
            "schema": INTENT_SCHEMA,
            "object_id": object_id,
            "copy": record,
            "unit": unit,
            "job_id": job_id,
            "how": how,
            "libraries": names,
            "annotation_settings": settings,
            "keep_source": keep_source,
            "recorded_at": _now(),
        })
        staged = incoming / OBJECT_FILE
        try:
            # A name other files share (a hard link) is copied, so no store object shares a file record with a
            # file outside the store.
            linked = os.stat(extended_path(source)).st_nlink > 1
            method = self._stage(source, staged, identity, run, copy=keep_source or linked)
        except BaseException:
            unlink_tree(incoming)
            raise
        directory = self.object_directory(object_id)
        target = self.object_path(object_id)
        kept = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(extended_path(target)):
                kept = directory / f"damaged-{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')}.dbs"
                _replace(Path(extended_path(target)), Path(extended_path(kept)))
            _replace(Path(extended_path(staged)), Path(extended_path(target)))
        except BaseException:
            # Nothing is installed: the damaged object goes back where it was, and a renamed copy back to its
            # own place, so that its caller holds it there. Only if that fails too is it left for repair.
            if kept is not None and not os.path.lexists(extended_path(target)):
                try:
                    _replace(Path(extended_path(kept)), Path(extended_path(target)))
                except OSError:
                    pass
            try:
                if method == "rename":
                    _replace(Path(extended_path(staged)), Path(extended_path(source)))
                unlink_tree(incoming)
            except OSError:
                pass
            raise
        repairs = list((previous or {}).get("repairs") or [])
        if damaged:
            repairs.append({"at": _now(), "problem": damaged, "kept_as": kept.name if kept else None,
                            "job_id": job_id})
        entry = _object_record(
            identity, how=how, unit=unit, job_id=job_id, method=method, libraries=names,
            settings=settings or (previous or {}).get("annotation_settings"),
            testzip="passed" if identity.recognised else "not_a_zip", repairs=repairs,
        )
        _write_json_atomic(self.entry_path(object_id), entry)
        claim = None
        if unit is not None:
            claim = self.claim(object_id, unit=unit, job_id=job_id, action=how, name=source.name, copy=record,
                               outcome=STORED)
        unlink_tree(incoming)
        removed = None
        if method == "copy" and not keep_source:
            # The store holds it now; only then does the source name go.
            run(lambda: os.unlink(extended_path(source)))
            removed = _now()
        elif method == "rename":
            removed = _now()
        result = {**record, "outcome": STORED, "verified_by": "testzip" if identity.recognised else "sha256",
                  "claim": claim, "removed_at": removed, "stored_by": method,
                  "annotation_settings_sha256": (settings or {}).get("sha256")}
        if damaged:
            result.update(repaired_object=True, repaired_problem=damaged)
        return result

    def _quarantine(
        self, source: Path, identity: CopyIdentity, record: dict[str, Any], object_id: str, *,
        unit: dict[str, Any] | None, job_id: str, how: str, run: Callable[[Callable[[], Any]], Any],
        keep_source: bool,
    ) -> dict[str, Any]:
        if keep_source:
            raise LibraryCopyStoreError(
                f"{source.name} has the content key of object {object_id} and other content; nothing was stored"
            )
        directory = self.root / "quarantine" / uuid.uuid4().hex
        directory.mkdir(parents=True)
        reason = {
            "schema": QUARANTINE_SCHEMA,
            "object_id": object_id,
            "reason": "the copy has the object's content key, and its entries decompress to other content",
            "copy": record,
            "unit": unit_label(unit),
            "job_id": job_id,
            "how": how,
            "recorded_at": _now(),
        }
        _write_json_atomic(directory / "reason.json", reason)
        try:
            self._stage(source, directory / OBJECT_FILE, identity, run,
                        copy=os.stat(extended_path(source)).st_nlink > 1)
        except BaseException:
            unlink_tree(directory)
            raise
        if os.path.lexists(extended_path(source)):
            run(lambda: os.unlink(extended_path(source)))
        return {**record, "outcome": QUARANTINED, "verified_by": "decompressed_content", "claim": None,
                "removed_at": _now(), "quarantine": self.relative(directory)}

    # -- seeding

    def seed(self, source: str | Path, *, confirmed: bool = False) -> dict[str, Any]:
        """Copy a verified .dbs into the store, never moving or linking it. A preview unless ``confirmed``.

        The libraries and annotation settings recorded with the object are read from the run directory that
        holds the source (its run-manifest.json, workflow-settings.json, method.txt and annotator tables);
        without them the object records none, and no finalised unit can be attached to it.
        """
        path = Path(source).expanduser()
        if not path.is_file():
            raise LibraryCopyStoreError(f"{path.name} is not a file.")
        identity = read_copy_identity(path)
        result: dict[str, Any] = {
            "source": self.relative(path),
            "size_bytes": identity.size_bytes,
            "object_id": identity.object_id,
            "content_key": identity.content_key,
            "recognised": identity.recognised,
            "action": "copy",
        }
        if not identity.recognised:
            raise LibraryCopyStoreError(
                f"{path.name} is not a readable MS-DIAL library copy ({identity.reason}); nothing was stored."
            )
        entry = self.entry(identity.object_id) if self.root.is_dir() else None
        result["store_holds_key"] = entry is not None
        libraries, settings = _run_records(path.parent)
        result["libraries"] = library_names(libraries)
        result["annotation_settings_sha256"] = (settings or {}).get("sha256")
        if entry is not None and entry.get("normalized_sha256") == identity.normalized_sha256:
            result["message"] = (
                f"The store already holds this content as object {identity.object_id}; nothing was copied."
            )
            result["seeded"] = False
            return result
        if not confirmed:
            result["seeded"] = False
            result["message"] = (
                "Nothing was stored. Show the source, its size, its content key and whether the store already "
                "holds it to the user, and run again with --confirmed only after an explicit answer."
            )
            return result
        stored = self.put(path, identity, unit=None, job_id="", libraries=libraries, settings=settings,
                          how="seed", keep_source=True)
        result.update(seeded=True, outcome=stored["outcome"], verified_by=stored["verified_by"])
        result["message"] = (
            f"Stored {path.name} ({identity.size_bytes} bytes) as library-copy object {identity.object_id}; "
            "the source was copied, not moved."
            if stored["outcome"] == STORED else
            f"The store already holds this content as object {identity.object_id} ({stored['outcome']}); "
            "nothing was copied."
        )
        return result

    # -- status and repair

    def orphans(self) -> list[dict[str, Any]]:
        """Every ingest a crash left in incoming/, with what repair would do with it."""
        directory = self.root / "incoming"
        found = []
        for item in sorted(directory.iterdir()) if directory.is_dir() else []:
            if not item.is_dir():
                continue
            try:
                intent = _read_json(item / "intent.json")
            except StoreError:
                intent = None
            staged = (item / OBJECT_FILE).is_file()
            object_id = str((intent or {}).get("object_id") or "")
            if intent is None:
                action = "quarantine"
            elif staged:
                action = "complete"
            elif object_id and self.object_path(object_id).is_file() and self.entry(object_id) is None:
                action = "record"
            else:
                action = "remove"
            found.append({"incoming": self.relative(item), "object_id": object_id or None,
                          "unit": unit_label((intent or {}).get("unit")), "job_id": (intent or {}).get("job_id"),
                          "staged_copy": staged, "action": action})
        return found

    def repair(self, *, confirmed: bool = False) -> dict[str, Any]:
        """Complete every ingest a crash interrupted; set aside only what no intent describes."""
        orphans = self.orphans()
        result: dict[str, Any] = {"orphans": orphans, "repaired": [], "errors": []}
        if not confirmed:
            complete = sum(1 for item in orphans if item["action"] in {"complete", "record", "remove"})
            result["message"] = (
                f"Nothing was changed. {complete} unfinished ingest(s) would be completed and "
                f"{len(orphans) - complete} quarantined; call again with repair and confirmed after an "
                "explicit answer."
            )
            return result
        for item in orphans:
            directory = self.workspace_root / item["incoming"]
            try:
                result["repaired"].append({**item, **self._repair_one(directory, item)})
            except (OSError, StoreError) as error:
                result["errors"].append(f"{item['incoming']}: {describe_error(error, self.workspace_root)}")
        return result

    def _repair_one(self, directory: Path, item: dict[str, Any]) -> dict[str, Any]:
        intent = _read_json(directory / "intent.json") if (directory / "intent.json").is_file() else None
        if intent is None or not item.get("object_id"):
            # put writes the intent the instant after it makes the directory: one without an intent that is
            # not older than a lock's staleness may be an ingest under way.
            age = dt.datetime.now().timestamp() - directory.stat().st_mtime
            if age < LOCK_STALE_SECONDS:
                return {"outcome": "in_progress"}
            target = self.root / "quarantine" / directory.name
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace(directory, target)
            _write_json_atomic(target / "reason.json", {
                "schema": QUARANTINE_SCHEMA, "object_id": None,
                "reason": "an ingest with no recorded intent was found in incoming/", "recorded_at": _now(),
            })
            return {"outcome": QUARANTINED, "quarantine": self.relative(target)}
        object_id = str(intent["object_id"])
        unit = intent.get("unit")
        job_id = str(intent.get("job_id") or "")
        how = str(intent.get("how") or "repair")
        staged = directory / OBJECT_FILE
        lock = self.lock(object_id, job_id)
        try:
            lock.acquire(timeout=0)
        except StoreLockTimeout:
            return {"outcome": "in_progress"}  # its ingest holds the lock: not an orphan
        try:
            if staged.is_file():
                # The interrupted ingest is completed as a new one: the staged copy is the store's own now, so
                # it moves, and it is removed only where the object proves equal.
                outcome = self._put_locked(staged, read_copy_identity(staged), unit=unit, job_id=job_id,
                                           libraries=intent.get("libraries") or [],
                                           settings=intent.get("annotation_settings"), how=how, patience=None,
                                           keep_source=False)
                done = {"outcome": outcome["outcome"], "object_id": outcome["object_id"]}
            else:
                target = self.object_path(object_id)
                if target.is_file() and self.entry(object_id) is None:
                    # Crashed after the copy was installed and before its record was written.
                    identity = read_copy_identity(target)
                    if identity.object_id != object_id or identity.sha256 != (intent.get("copy") or {}).get("sha256"):
                        raise LibraryCopyStoreError(
                            f"object {object_id}'s file is not the copy its intent records; a person looks"
                        )
                    tested = _testzip(target) if identity.recognised else None
                    _write_json_atomic(self.entry_path(object_id), _object_record(
                        identity, how=how, unit=unit, job_id=job_id, method="repair",
                        libraries=library_names(intent.get("libraries") or []),
                        settings=intent.get("annotation_settings"),
                        testzip="not_a_zip" if not identity.recognised else "passed" if tested is None else "failed",
                    ))
                    copy = identity.record(str((intent.get("copy") or {}).get("name") or OBJECT_FILE))
                    if unit is not None:
                        self.claim(object_id, unit=unit, job_id=job_id, action=how, name=copy["name"], copy=copy,
                                   outcome=STORED)
                    done = {"outcome": STORED, "object_id": object_id}
                else:
                    done = {"outcome": "nothing_left", "object_id": object_id}
            unlink_tree(directory)
        finally:
            self._release(lock)
        _record_repair(self, intent, done)
        return done

    def summary(self, *, verify: bool = False) -> dict[str, Any]:
        """Every object, its claims and its health; unfinished ingests; quarantined copies. Read-only."""
        objects = []
        for object_id in self.object_ids():
            try:
                entry = self.entry(object_id)
            except StoreError as error:
                objects.append({"object_id": object_id, "problem": describe_error(error, self.workspace_root)})
                continue
            claims = self.claims(object_id)
            objects.append({
                "object_id": object_id,
                "path": self.relative(self.object_path(object_id)),
                "size_bytes": (entry or {}).get("size_bytes"),
                "recognised": (entry or {}).get("recognised"),
                "content_key_kind": (entry or {}).get("content_key_kind"),
                "testzip": ((entry or {}).get("testzip") or {}).get("result"),
                "ingested": (entry or {}).get("ingested"),
                "libraries": (entry or {}).get("libraries") or [],
                "annotation_settings_sha256": ((entry or {}).get("annotation_settings") or {}).get("sha256"),
                "claims": len(claims),
                "units": sorted({str(item.get("unit")) for item in claims if item.get("unit")}),
                "problem": self.problem(object_id, entry, full=verify),
                "verified": "full" if verify else "size_and_entries",
            })
        quarantine = self.root / "quarantine"
        quarantined = []
        for item in sorted(quarantine.iterdir()) if quarantine.is_dir() else []:
            try:
                reason = _read_json(item / "reason.json") or {}
            except StoreError:
                reason = {}
            quarantined.append({"path": self.relative(item), "object_id": reason.get("object_id"),
                                "reason": reason.get("reason"), "unit": reason.get("unit")})
        return {
            "store": self.relative(self.root),
            "exists": self.root.is_dir(),
            "objects": objects,
            "object_bytes": sum(int(item.get("size_bytes") or 0) for item in objects),
            "unfinished_ingests": self.orphans(),
            "quarantined": quarantined,
        }


def _record_repair(store: LibraryCopyStore, intent: dict[str, Any], done: dict[str, Any]) -> None:
    """Name a repaired ingest in its unit's manifest, when the intent names one that does not record it yet."""
    from .repository_reanalysis import update_manifest

    unit = intent.get("unit") or {}
    relative = str(unit.get("manifest") or "")
    job_id = str(intent.get("job_id") or "")
    if not relative or not job_id or done.get("outcome") not in RESTORABLE_OUTCOMES:
        return
    path = store.workspace_root / relative
    if not path.is_file():
        return
    copy = dict(intent.get("copy") or {})

    def change(current: dict[str, Any]) -> None:
        recorded = [item for item in current.get("library_copy_records") or [] if isinstance(item, dict)]
        if any(item.get("job_id") == job_id and item.get("object_id") == done["object_id"] for item in recorded):
            return
        current["library_copy_records"] = [*recorded, {
            "schema": RECORD_SCHEMA, "action": "repair", "job_id": job_id, **copy,
            "object_id": done["object_id"], "outcome": done["outcome"], "verified_by": "repair",
            "recorded_at": _now(), "by": f"Interactive {__version__}",
        }]

    try:
        update_manifest(path, change)
    except (OSError, ValueError):
        pass


def _run_records(directory: Path) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """The library identities and annotation settings of the run whose directory holds a copy."""
    from .run_finalisation import _sharing_context

    libraries: list[dict[str, Any]] = []
    if (directory / "workflow-settings.json").is_file() or (directory / "run-manifest.json").is_file():
        try:
            context, _run, _manifest = _sharing_context({"run_directory": str(directory)})
            libraries = context.identities()
        except (OSError, ValueError):
            libraries = []
    return libraries, annotation_settings(directory)


# ---- a finalised unit whose copy was deleted before the store existed ------------------------------------


def _finalised_copy(manifest: dict[str, Any]) -> tuple[str, dict[str, Any] | None]:
    finalisation = manifest.get("console_run_finalisation") if isinstance(
        manifest.get("console_run_finalisation"), dict) else {}
    job_id = str((finalisation or {}).get("job_id") or "")
    deleted = [item for item in (finalisation or {}).get("loaded_library_copies_deleted") or [] if isinstance(item, dict)]
    return job_id, (deleted[0] if len(deleted) == 1 else None)


def attach(manifest_path: str | Path, object_id: str, *, confirmed: bool = False) -> dict[str, Any]:
    """Record which stored object stands for the copy an already-finalised unit deleted. A preview unless confirmed.

    Before 0.5.37 finalisation deleted the copy and recorded only its sha256, which no stored copy can equal
    (the zip timestamps differ). The object stands for it when the unit's recorded libraries (name and sha256)
    are the object's and the unit's annotation settings, paths reduced to names, are those recorded with the
    object: MS-DIAL reads the libraries back from the copy alone. The unit's console_run_finalisation is not
    rewritten; a library_copy_records item names the object, and a claim in the store names the unit.
    """
    from .repository_reanalysis import read_manifest, update_manifest

    path = Path(manifest_path).expanduser().resolve()
    manifest = read_manifest(path)
    store, why = library_copy_store_for(manifest, path)
    if store is None:
        raise LibraryCopyStoreError(f"no library-copy store could be located for this unit: {why}.")
    job_id, deleted = _finalised_copy(manifest)
    finalisation = manifest.get("console_run_finalisation") or {}
    if not job_id or not finalisation.get("campaign_approval_recorded"):
        raise LibraryCopyStoreError(
            "this unit records no campaign run whose finalisation deleted its library copy; nothing to attach."
        )
    recorded = [item for item in manifest.get("library_copy_records") or []
                if isinstance(item, dict) and item.get("job_id") == job_id]
    if (finalisation.get("loaded_library_copy") or {}).get("copies") or recorded:
        raise LibraryCopyStoreError(f"run {job_id} already names the stored object of its library copy.")
    if deleted is None:
        raise LibraryCopyStoreError(f"run {job_id} records no single deleted library copy; nothing to attach.")
    entry = store.entry(object_id)
    if entry is None:
        raise LibraryCopyStoreError(f"the store holds no object {object_id}.")
    refused: list[str] = []
    unit_libraries = finalisation.get("libraries") or []
    if not _library_set(unit_libraries):
        refused.append("the unit records no libraries")
    elif _library_set(unit_libraries) != _library_set(entry.get("libraries") or []):
        refused.append("the unit's libraries (name and sha256) are not the object's")
    run_directory = Path(str((manifest.get("finalized_run") or {}).get("run_directory")
                             or manifest.get("output_directory") or ""))
    settings = annotation_settings(run_directory) if str(run_directory) not in {"", "."} else None
    differences = settings_differences(settings, entry.get("annotation_settings"))
    if differences:
        refused.append("the annotation settings differ: " + "; ".join(differences[:5]))
    problem = store.problem(object_id, entry, full=confirmed)
    if problem:
        refused.append(f"object {object_id} does not match its record ({problem})")
    result: dict[str, Any] = {
        "manifest": store.relative(path),
        "job_id": job_id,
        "deleted_copy": {key: deleted.get(key) for key in ("name", "size_bytes", "sha256")},
        "object_id": object_id,
        "object_size_bytes": entry.get("size_bytes"),
        "libraries": library_names(unit_libraries),
        "annotation_settings_sha256": (settings or {}).get("sha256"),
        "refused": refused,
        "attached": False,
    }
    if refused:
        result["message"] = "Refused: " + "; ".join(refused) + "."
        return result
    if not confirmed:
        result["message"] = (
            "Nothing was recorded. Show the unit, the run, its deleted copy and the object it would be attached "
            "to to the user, and run again with --confirmed only after an explicit answer."
        )
        return result
    unit = unit_ref(manifest, path, store.workspace_root)
    item = {
        "schema": RECORD_SCHEMA,
        "action": "attach",
        "job_id": job_id,
        "name": deleted.get("name"),
        "size_bytes": deleted.get("size_bytes"),
        "sha256": deleted.get("sha256"),
        "object_id": object_id,
        "content_key": entry.get("content_key"),
        "recognised": bool(entry.get("recognised")),
        "outcome": "attached",
        "verified_by": "library_and_settings_identity",
        "annotation_settings_sha256": (settings or {}).get("sha256"),
        "recorded_at": _now(),
        "by": f"Interactive {__version__}",
    }

    def change(current: dict[str, Any]) -> None:
        current["library_copy_records"] = [*(current.get("library_copy_records") or []), item]

    update_manifest(path, change)
    store.claim(object_id, unit=unit, job_id=job_id, action="attach", name=str(deleted.get("name") or ""),
                copy=deleted, outcome="attached")
    result.update(attached=True, record=item, message=(
        f"Attached run {job_id} to library-copy object {object_id}, verified by its libraries and annotation "
        "settings; recorded under library_copy_records."
    ))
    return result


def store_status(workspace_root: str | Path, *, verify: bool = False, repair: bool = False,
                 confirmed: bool = False) -> dict[str, Any]:
    store = LibraryCopyStore(Path(workspace_root).expanduser().resolve())
    result = store.summary(verify=verify)
    if repair:
        result["repair"] = store.repair(confirmed=confirmed)
    return result


# ---- the Console's temporary directory --------------------------------------------------------------------


def console_temp_directory(manifest_path: str | Path, job_id: str) -> tuple[Path | None, str]:
    """Where a repository unit's Console keeps its temporary files: <workspace root>/_tmp/<job id>.

    MS-DIAL writes every stream it saves - the whole library copy among them - to Path.GetTempFileName() before
    moving it into place (DirectoryTreeStreamManager), which is the user profile's TEMP on C: unless TMP and
    TEMP say otherwise. Returns None and why when the unit's workspace root cannot be located.
    """
    from .repository_reanalysis import read_manifest

    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError) as error:
        return None, f"its manifest could not be read ({describe_error(error)})"
    root, how = workspace_root_for(manifest, manifest_path)
    if root is None:
        return None, how
    return root / CONSOLE_TEMP_DIRECTORY_NAME / _component(job_id), how


def settle_console_temp(
    directory: str | Path,
    *,
    manifest_path: str | Path,
    job_id: str,
    libraries: Callable[[], Iterable[Any]] | Iterable[Any] = (),
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """What a Console left in its temporary directory, put where it belongs. Never raises.

    Every file is listed (relative name and size). One laid out as MS-DIAL's library copy - written there by a
    save the Console did not finish - goes into the library-copy store; one that cannot is kept, and the caller
    holds it from sharing (``held``, absolute paths, for the hold only). Everything else is removed, and the
    directory with it once nothing is held.
    """
    from .repository_reanalysis import read_manifest

    directory = Path(directory)
    label = f"{CONSOLE_TEMP_DIRECTORY_NAME}/{directory.name}"
    result: dict[str, Any] = {"directory": label, "leftovers": [], "library_copies": [], "held": [],
                              "removed_files": 0, "removed_bytes": 0, "errors": []}
    if not directory.is_dir():
        return result
    files = []
    for folder, _directories, names in os.walk(extended_path(directory, always=True)):
        for name in names:
            path = Path(plain_path(os.path.join(folder, name)))
            try:
                size = os.stat(extended_path(path)).st_size
            except OSError:
                size = None
            files.append(path)
            result["leftovers"].append({"name": path.relative_to(directory).as_posix(), "size_bytes": size})
    copies = [path for path in files if is_library_copy_file(path)]
    if copies:
        try:
            manifest = read_manifest(manifest_path)
        except (OSError, ValueError):
            manifest = {}
        store, why = library_copy_store_for(manifest, manifest_path)
        root = store.workspace_root if store is not None else None
        names = list(libraries() if callable(libraries) else libraries)
        for path in copies:
            if store is None:
                result["held"].append(str(path))
                result["errors"].append(f"{path.name}: no library-copy store could be located ({why})")
                continue
            try:
                identity = read_copy_identity(path)
                result["library_copies"].append(store.put(
                    path, identity, unit=unit_ref(manifest, manifest_path, root), job_id=job_id, libraries=names,
                    settings=settings, how="console_temp",
                ))
            except (OSError, StoreError) as error:
                result["held"].append(str(path))
                result["errors"].append(f"{path.name}: {describe_error(error, root)}")
    held = {str(path) for path in result["held"]}
    rest = [path for path in files if str(path) not in held and os.path.lexists(extended_path(path))]
    for path in rest:
        try:
            size = os.stat(extended_path(path)).st_size
            os.unlink(extended_path(path))
        except OSError as error:
            result["errors"].append(f"{path.relative_to(directory).as_posix()}: {describe_error(error, directory)}")
            continue
        result["removed_files"] += 1
        result["removed_bytes"] += size
    if not held:
        removal = unlink_tree(directory)
        if not removal["complete"]:
            result["errors"].extend(
                f"{_relative_name(item.get('path'), directory)}: {item.get('reason')}" for item in removal["kept"]
            )
    result["leftover_count"] = len(result["leftovers"])
    result["leftover_bytes"] = sum(int(item["size_bytes"] or 0) for item in result["leftovers"])
    return result


# ---- the command line ---------------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m msdial_app.library_copy_store",
        description="Migration steps for the library-copy store. Each is a preview unless --confirmed is given.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    seed = commands.add_parser("seed", help="copy a verified .dbs into the store; the source is never moved")
    seed.add_argument("source")
    seed.add_argument("--workspace-root", required=True)
    seed.add_argument("--confirmed", action="store_true")
    attached = commands.add_parser("attach", help="name the object a finalised unit's deleted copy stands for")
    attached.add_argument("manifest_path")
    attached.add_argument("--object", required=True, dest="object_id")
    attached.add_argument("--confirmed", action="store_true")
    status = commands.add_parser("status", help="report the store; --repair completes interrupted ingests")
    status.add_argument("--workspace-root", required=True)
    status.add_argument("--verify", action="store_true")
    status.add_argument("--repair", action="store_true")
    status.add_argument("--confirmed", action="store_true")
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "seed":
            root = Path(arguments.workspace_root).expanduser().resolve()
            result = LibraryCopyStore(root).seed(arguments.source, confirmed=arguments.confirmed)
        elif arguments.command == "attach":
            result = attach(arguments.manifest_path, arguments.object_id, confirmed=arguments.confirmed)
        else:
            result = store_status(arguments.workspace_root, verify=arguments.verify, repair=arguments.repair,
                                  confirmed=arguments.confirmed)
    except (OSError, ValueError) as error:
        print(json.dumps({"ok": False, "error": describe_error(error)}, indent=2))
        return 1
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
