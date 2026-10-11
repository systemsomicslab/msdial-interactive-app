"""Reopen a finalised campaign unit in the MS-DIAL GUI for viewing, and take the reopening back.

WHY. Finalisation leaves a campaign unit's project as nothing MS-DIAL can open: its containers move into
``output/msdial-intermediates`` while the ``.mddata`` names each one beside the inputs, and its loaded-library
copy goes into the library-copy store while the loader reads ``<project>_Loaded.msp2.dbs`` beside the project
(run_finalisation). The user's decision of 2026-10-11 keeps it that way and reopens a unit through a restore
step instead; a parallel MS-DIAL GUI change opens a project whose raw files are absent.

WHAT A RESTORE DOES (restore_project_for_viewing; one mode):

1. copies each moved container from ``output/msdial-intermediates/<relative_path>`` back to the path under the
   raw directory its relative_path names, which is where the ``.mddata`` looks, creating the directories it
   needs - whether the raw files are there or released. Copied, never moved or linked: MS-DIAL rewrites a
   container in place when it switches files, and the retained ones must not change;
2. links the run's store object into the output under the run's own ``.dbs`` name: a hard link on the same
   volume, else a verified copy. MS-DIAL replaces that file on a save (delete, then move) and never writes into
   it, so a save does not reach the store;
3. backs up the ``.mdproject``, ``.mddata`` and ``_Loaded.msp2`` into ``<workspace root>/_viewing/r-<restore id>/``,
   whose ``restore.local.json`` lists every path the restore made - written before each step, so that a
   restore that stopped half way is taken back as completely as one that finished;
4. records ``viewing_restores[]`` in the unit manifest and a hold ``{step: viewing_restore, blocks: [sharing,
   raw_deletion]}``: no publication, raw cleanup, discard or split-parent release while it stands, and no
   retry lifts it (run_finalisation._retry_hold returns a step it does not know as it stands).

It refuses a unit with no run finalisation, no store object for the run, a restore already standing, any
other finalisation hold (a retry would undo the restore), a run or a download under way, a raw deletion that
has begun and not finished, and any target path that already exists. A preview (``confirmed=False``) writes
nothing and names every path relative to the workspace root.

undo_project_restore removes exactly what the record lists, puts back a project file whose sha256 changed from
its backup, reports - never deletes - any other file that appeared in the output or in the raw-side
directories the restore copied into, and lifts the hold, recording the outcome in ``viewing_restores``. A file
MS-DIAL wrote over the link on a save is a new library copy: it goes through the library-copy store, which
removes it only where it holds the same content. A file another program still holds open is left, and the
restore stays held until a later call.

Nothing here launches MS-DIAL.
"""

from __future__ import annotations

import datetime as dt
import errno
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from . import __version__
from .diagnostic_paths import INTERMEDIATES_DIRECTORY, extended_path, path_is_file
from .download_store import _read_json, _write_json_atomic
from .library_copy_store import (
    LOADED_LIBRARY_COPY_SUFFIX,
    RECORD_SCHEMA,
    RESTORABLE_OUTCOMES,
    VIEWING_DIRECTORY_NAME,
    LibraryCopyStore,
    LibraryCopyStoreError,
    describe_error,
    read_copy_identity,
    unit_ref,
    workspace_root_for,
)
from .run_finalisation import (
    BLOCKS_RAW_DELETION,
    BLOCKS_SHARING,
    HOLDS,
    LEGACY_LIBRARY_COPY_RECORD,
    LIBRARY_COPY_RECORD,
    LIBRARY_COPY_RECORDS,
    VIEWING_RESTORE,
    _console_inputs,
    _hold,
    describe_holds,
    standing_holds,
)


RESTORE_SCHEMA = "msdial-viewing-restore.v1"
VIEWING_RESTORES = "viewing_restores"
RECORD_FILE = "restore.local.json"
REFUSED = "viewing_restore_refused"
# MS-DIAL's GUI is a .NET Framework program without a long-path manifest, and LongPathsEnabled is 0 on the
# campaign host: it opens no file whose path is this long or longer.
GUI_MAX_PATH = 260
_SHOWN_PATHS = 50
_CROSS_DEVICE_WINERRORS = {17}


class RestoreRefused(ValueError):
    """A restore or its undo was refused before anything was written. The message starts with a fixed token."""

    def __init__(self, message: str) -> None:
        super().__init__(f"{REFUSED}: {message}")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    from .library_copy_store import _sha256 as digest

    return digest(path)


def active_viewing_restores(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    """The viewing_restore holds standing on a unit."""
    return [item for item in standing_holds(manifest) if item.get("step") == VIEWING_RESTORE]


def _relative(path: Path, root: Path) -> str:
    try:
        return Path(path).resolve().relative_to(root.resolve()).as_posix()
    except (OSError, ValueError):
        return f"<outside the workspace root>/{Path(path).name}"


def _library_copy_of(manifest: dict[str, Any], job_id: str) -> tuple[dict[str, Any] | None, str]:
    """The record naming the store object of a run's library copy, or None and why not."""
    finalisation = manifest.get("console_run_finalisation") or {}
    found: list[dict[str, Any]] = []
    if str(finalisation.get("job_id") or "") == job_id:
        block = finalisation.get(LIBRARY_COPY_RECORD) if isinstance(finalisation.get(LIBRARY_COPY_RECORD), dict) else {}
        found.extend(item for item in (block or {}).get("copies") or [] if isinstance(item, dict))
    found.extend(
        item for item in manifest.get(LIBRARY_COPY_RECORDS) or []
        if isinstance(item, dict) and str(item.get("job_id") or "") == job_id
    )
    if not found:
        if finalisation.get(LEGACY_LIBRARY_COPY_RECORD):
            return None, (
                "the run's copy was deleted before the library-copy store existed (Interactive before 0.5.37), "
                "and no object is attached to it; attach one with python -m msdial_app.library_copy_store attach"
            )
        return None, "the run records no library copy in the store"
    usable = [item for item in found if item.get("object_id")
              and (item.get("outcome") in RESTORABLE_OUTCOMES or item.get("outcome") == "attached")]
    if not usable:
        outcomes = sorted({str(item.get("outcome")) for item in found})
        return None, f"the run's library copy was not kept in the store as one it can be reopened from ({', '.join(outcomes)})"
    objects = {str(item["object_id"]) for item in usable}
    if len(objects) != 1:
        return None, f"the run names {len(objects)} store objects, not one"
    names = {str(item.get("name") or "") for item in usable}
    if len(names) != 1 or not next(iter(names)).endswith(LOADED_LIBRARY_COPY_SUFFIX):
        return None, "the run's library copy is not named <project>_Loaded.msp2.dbs"
    return usable[-1], ""


def _live_job(manifest_path: Path) -> str:
    """A run or diagnostic of this unit this process has queued or running, read only if its backend is loaded."""
    server = sys.modules.get("msdial_app.server")
    if server is None:
        return ""
    try:
        with server.JOBS_LOCK:
            return server._live_console_job_locked(server._unit_key(str(manifest_path)))
    except Exception:  # noqa: BLE001 - a registry that cannot be read refuses nothing by itself
        return ""


def _refuse_live(manifest: dict[str, Any], manifest_path: Path) -> None:
    from .repository_reanalysis import DELETION_RESUMABLE_STATES, _live_run_attempt_in, _owner_manifest_path, read_manifest

    live = _live_run_attempt_in(manifest)
    if live is not None:
        raise RestoreRefused(
            f"a run of this unit is live (job {live.get('job_id') or 'unrecorded'}, {live.get('kind') or 'run'}); "
            "restore once it has ended."
        )
    job = _live_job(manifest_path)
    if job:
        raise RestoreRefused(f"a run of this unit is live (job {job} is queued or running); restore once it has ended.")
    if str(manifest.get("status") or "") == "downloading":
        raise RestoreRefused("this unit's raw data are being downloaded; restore once the download has ended.")
    if str((manifest.get("raw_deletion") or {}).get("state") or "") in DELETION_RESUMABLE_STATES:
        raise RestoreRefused(
            "a deletion of this unit's raw data has begun and not finished; a restore would write into what it "
            "deletes. Finish or settle the deletion first."
        )
    owner = _owner_manifest_path(manifest, manifest_path)
    if owner is not None and owner.is_file():
        try:
            release = read_manifest(owner).get("raw_release") or {}
        except (OSError, ValueError):
            release = {}
        if str(release.get("state") or "") in DELETION_RESUMABLE_STATES:
            raise RestoreRefused(
                "the release of the raw tree this part reads has begun and not finished; a restore would write "
                "into what it deletes."
            )


def _plan(manifest_path: Path, job_id: str) -> dict[str, Any]:
    """Everything a restore would do, checked; raises RestoreRefused. Writes nothing."""
    from .repository_reanalysis import _is_split_parent, read_manifest

    manifest = read_manifest(manifest_path)
    if _is_split_parent(manifest):
        parts = [Path(str(item.get("manifest_path") or "")).parent.parent.name
                 for item in manifest.get("split_into") or [] if isinstance(item, dict)]
        raise RestoreRefused(
            f"this is a split parent, which has no run of its own; restore one of its parts: {', '.join(parts)}."
        )
    finalisation = manifest.get("console_run_finalisation") if isinstance(
        manifest.get("console_run_finalisation"), dict) else {}
    current = str((finalisation or {}).get("job_id") or "")
    if not current:
        raise RestoreRefused("no run finalisation is recorded for this unit, so there is no finalised project to restore.")
    if job_id and job_id != current:
        raise RestoreRefused(
            f"run {job_id} is not this unit's finalised run; only the current run, {current}, can be restored."
        )
    if not finalisation.get("campaign_approval_recorded"):
        raise RestoreRefused(
            f"no campaign approval is recorded for run {current}, so its finalisation left its project as MS-DIAL "
            "wrote it; open its .mdproject in the output directly."
        )
    relocation = finalisation.get("msdial_intermediates") if isinstance(
        finalisation.get("msdial_intermediates"), dict) else {}
    moved = [item for item in (relocation or {}).get("moved") or [] if isinstance(item, dict)]
    if not moved or (relocation or {}).get("pending") or (relocation or {}).get("errors"):
        raise RestoreRefused(
            f"run {current} moved no MS-DIAL containers (it did not complete, or some are still pending), so there "
            "is nothing to restore."
        )
    restores = active_viewing_restores(manifest)
    if restores:
        raise RestoreRefused(
            f"restore {restores[0].get('restore_id')} is already active on this unit; undo it first with "
            "msdial_undo_project_restore."
        )
    holds = standing_holds(manifest)
    if holds:
        raise RestoreRefused(
            f"finalisation holds stand on this unit ({describe_holds(holds)}); a retry of any of them would undo "
            "a restore. Resolve them first."
        )
    _refuse_live(manifest, manifest_path)
    root, why = workspace_root_for(manifest, manifest_path)
    if root is None:
        raise RestoreRefused(f"no library-copy store could be located for this unit: {why}.")
    store = LibraryCopyStore(root)
    copy, why = _library_copy_of(manifest, current)
    if copy is None:
        raise RestoreRefused(
            f"the library-copy store holds no copy verified to be this run's ({why}). A copy of other libraries "
            "or settings would open with wrong annotations and no error."
        )
    object_id = str(copy["object_id"])
    entry = store.entry(object_id)
    problem = store.problem(object_id, entry)
    if problem:
        raise RestoreRefused(f"library-copy object {object_id} does not match its record ({problem}); it was not linked.")
    if not (entry or {}).get("recognised"):
        raise RestoreRefused(f"library-copy object {object_id} is not laid out as MS-DIAL's library copy.")
    output = Path(str(manifest.get("output_directory") or "")).expanduser()
    if not str(manifest.get("output_directory") or "").strip() or not output.is_dir():
        raise RestoreRefused("the unit's output directory is not on disk.")
    name = str(copy["name"])
    project = name[: -len(LOADED_LIBRARY_COPY_SUFFIX)]
    link = output / name
    if os.path.lexists(extended_path(link)):
        raise RestoreRefused(
            f"the output already holds {name}. If MS-DIAL wrote it, it is a copy of the loaded libraries: a retry "
            "of its finalisation stores it."
        )
    project_files = {suffix: output / f"{project}{suffix}" for suffix in (".mdproject", ".mddata", "_Loaded.msp2")}
    for suffix in (".mdproject", ".mddata"):
        if not project_files[suffix].is_file():
            raise RestoreRefused(f"the output holds no {project}{suffix}, so there is no project to reopen.")
    raw_root = Path(str((relocation or {}).get("raw_directory") or ""))
    if not str((relocation or {}).get("raw_directory") or "").strip():
        raise RestoreRefused("the run's relocation record names no raw directory, so the containers' paths are unknown.")
    inventory: dict[str, str] = {}
    for item in manifest.get("retained_artifact_inventory") or []:
        if isinstance(item, dict) and item.get("sha256"):
            inventory[os.path.normcase(str(item.get("path") or ""))] = str(item["sha256"])
    source_root = output / INTERMEDIATES_DIRECTORY
    containers, missing, existing = [], [], []
    for item in moved:
        relative = str(item.get("relative_path") or "")
        source = source_root / relative
        target = raw_root / relative
        if not relative or not path_is_file(source):
            missing.append(relative)
            continue
        if os.path.lexists(extended_path(target)):
            existing.append(target)
        expected = inventory.get(os.path.normcase(str(source.resolve()))) or inventory.get(
            os.path.normcase(str(item.get("path") or "")))
        containers.append({"relative_path": relative, "source": source, "target": target,
                           "size_bytes": os.stat(extended_path(source)).st_size, "sha256": expected})
    if missing:
        raise RestoreRefused(
            f"{len(missing)} retained MS-DIAL container(s) are not in msdial-intermediates; the first is {missing[0]}."
        )
    if existing:
        raise RestoreRefused(
            f"{len(existing)} target path(s) already exist; the first is {_relative(existing[0], root)}. A restore "
            "never writes over a file."
        )
    directories: list[Path] = []
    for container in containers:
        parent = container["target"].parent
        chain = []
        while not os.path.isdir(extended_path(parent)) and parent != parent.parent:
            chain.append(parent)
            parent = parent.parent
        for directory in reversed(chain):
            if directory not in directories:
                directories.append(directory)
    csv_path = output / "analysis_files.csv"
    inputs = [path for path, _name in _console_inputs(csv_path)] if csv_path.is_file() else []
    present = sum(1 for path in inputs if os.path.exists(path))
    warnings = []
    long_paths = [item["target"] for item in containers if len(str(item["target"])) >= GUI_MAX_PATH]
    if long_paths:
        warnings.append(
            f"{len(long_paths)} target path(s) are {GUI_MAX_PATH} characters or longer, which MS-DIAL's GUI cannot "
            f"open while LongPathsEnabled is 0; the first is {_relative(long_paths[0], root)}."
        )
    return {
        "manifest": manifest,
        "root": root,
        "store": store,
        "job_id": current,
        "copy": copy,
        "entry": entry,
        "object_id": object_id,
        "output": output,
        "project": project,
        "project_files": project_files,
        "link": link,
        "raw_root": raw_root,
        "containers": containers,
        "directories": directories,
        "raw_files": {"present": present, "total": len(inputs)},
        "warnings": warnings,
    }


def _describe_plan(plan: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    root = plan["root"]
    raw = plan["raw_files"]
    raw_present = bool(raw["total"]) and raw["present"] == raw["total"]
    targets = [_relative(item["target"], root) for item in plan["containers"]]
    return {
        "manifest": _relative(manifest_path, root),
        "job_id": plan["job_id"],
        "project": _relative(plan["project_files"][".mdproject"], root),
        "raw_directory": _relative(plan["raw_root"], root),
        "raw_files_present": raw_present,
        "raw_files": raw,
        "containers": len(plan["containers"]),
        "container_bytes": sum(int(item["size_bytes"]) for item in plan["containers"]),
        "targets": targets[:_SHOWN_PATHS],
        "targets_not_shown": max(0, len(targets) - _SHOWN_PATHS),
        "directories_to_create": [_relative(item, root) for item in plan["directories"]],
        "library_copy": {
            "object_id": plan["object_id"],
            "object": _relative(plan["store"].object_path(plan["object_id"]), root),
            "link": _relative(plan["link"], root),
            "size_bytes": plan["entry"].get("size_bytes"),
            "verified_by": plan["copy"].get("verified_by"),
            "outcome": plan["copy"].get("outcome"),
        },
        "creates_loaded_msp2": not plan["project_files"]["_Loaded.msp2"].is_file(),
        "hold": {"step": VIEWING_RESTORE, "blocks": [BLOCKS_SHARING, BLOCKS_RAW_DELETION]},
        "warnings": list(plan["warnings"]),
    }


def plan_project_restore(manifest_path: str | Path, job_id: str = "") -> dict[str, Any]:
    """What restore_project_for_viewing would do, with every path relative to the workspace root. Writes nothing."""
    path = Path(manifest_path).expanduser().resolve()
    plan = _plan(path, str(job_id or "").strip())
    return _describe_plan(plan, path)


def _write_record(path: Path, record: dict[str, Any]) -> None:
    _write_json_atomic(path, record)


def _set_entry_state(manifest_path: Path, restore_id: str, **fields: Any) -> None:
    from .repository_reanalysis import update_manifest

    def change(current: dict[str, Any]) -> None:
        for item in current.get(VIEWING_RESTORES) or []:
            if isinstance(item, dict) and item.get("restore_id") == restore_id:
                item.update(fields)

    update_manifest(manifest_path, change)


def restore_project_for_viewing(manifest_path: str | Path, job_id: str = "", confirmed: bool = False) -> dict[str, Any]:
    """Make a finalised campaign unit's project openable in the MS-DIAL GUI, for viewing. See the module docstring.

    A preview unless ``confirmed``. Refusals raise RestoreRefused (a ValueError) before anything is written. A
    failure part way is recorded (state ``failed``) and returned; the hold stands, and the undo takes back what
    the record lists.
    """
    from .repository_reanalysis import _live_run_attempt_in, update_manifest

    path = Path(manifest_path).expanduser().resolve()
    plan = _plan(path, str(job_id or "").strip())
    described = _describe_plan(plan, path)
    raw_note = (
        "" if described["raw_files_present"] else
        " The raw files are not on disk: open the project with an MS-DIAL GUI that opens a project without them."
    )
    if not confirmed:
        return {
            **described,
            "restored": False,
            "effects": "While this restore stands, this unit's outputs are held from sharing and its raw data from "
                       "deletion, the campaign included.",
            "message": "Nothing was restored. Show the library-copy object and how it was verified, the files to "
                       "copy, whether the raw files are present and the hold to the user, and call again with "
                       "confirmed=true only after an explicit answer." + raw_note,
        }
    root: Path = plan["root"]
    store: LibraryCopyStore = plan["store"]
    object_id = plan["object_id"]
    # Re-read in full now: a damaged object loads as empty libraries in MS-DIAL, with no error.
    problem = store.problem(object_id, plan["entry"], full=True)
    if problem:
        raise RestoreRefused(f"library-copy object {object_id} does not match its record ({problem}); it was not linked.")
    restore_id = uuid.uuid4().hex[:16]
    directory = root / VIEWING_DIRECTORY_NAME / f"r-{restore_id}"
    record_path = directory / RECORD_FILE
    record_name = _relative(record_path, root)
    output: Path = plan["output"]
    output_snapshot = sorted(entry.name for entry in os.scandir(output) if entry.is_file())
    raw_snapshot: dict[str, list[str]] = {}
    for container in plan["containers"]:
        parent = container["target"].parent
        key = _relative(parent, root)
        if key not in raw_snapshot and os.path.isdir(extended_path(parent)):
            raw_snapshot[key] = sorted(entry.name for entry in os.scandir(extended_path(parent)) if entry.is_file())
    record: dict[str, Any] = {
        "schema": RESTORE_SCHEMA,
        "restore_id": restore_id,
        "state": "restoring",
        "sharing": "local_only",
        "manifest": _relative(path, root),
        "job_id": plan["job_id"],
        "object_id": object_id,
        "project": plan["project"],
        "started_at": _now(),
        "by": f"Interactive {__version__}",
        "plan": {
            "containers": [{"relative_path": item["relative_path"], "target": _relative(item["target"], root),
                            "size_bytes": item["size_bytes"], "sha256": item["sha256"]}
                           for item in plan["containers"]],
            "link": _relative(plan["link"], root),
        },
        "output_snapshot": output_snapshot,
        "raw_snapshot": raw_snapshot,
        "created_files": [],
        "created_directories": [],
        "created_project_files": [],
        "backups": [],
        "link": None,
    }
    directory.mkdir(parents=True)
    _write_record(record_path, record)
    hold = _hold(
        [BLOCKS_SHARING, BLOCKS_RAW_DELETION], VIEWING_RESTORE, plan["job_id"],
        "restored for viewing in MS-DIAL: copies of its containers beside the raw data and a link to its "
        "library copy are in the unit until msdial_undo_project_restore takes them back",
        restore_id=restore_id, record=record_name,
    )
    entry = {
        "restore_id": restore_id, "state": "restoring", "job_id": plan["job_id"], "record": record_name,
        "object_id": object_id, "containers": len(plan["containers"]),
        "bytes": described["container_bytes"], "raw_files_present": described["raw_files_present"],
        "started_at": record["started_at"],
    }

    def change(current: dict[str, Any]) -> None:
        # Again under the writer lock: nothing may have started, or been held, since the plan was read.
        if active_viewing_restores(current):
            raise RestoreRefused("another restore became active on this unit meanwhile; undo it first.")
        if standing_holds(current):
            raise RestoreRefused(f"finalisation holds stand on this unit ({describe_holds(standing_holds(current))}).")
        live = _live_run_attempt_in(current)
        if live is not None:
            raise RestoreRefused(f"a run of this unit is live (job {live.get('job_id') or 'unrecorded'}).")
        current[HOLDS] = [*standing_holds(current), hold]
        current[VIEWING_RESTORES] = [*(current.get(VIEWING_RESTORES) or []), entry]

    try:
        update_manifest(path, change)
    except BaseException:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    done = 0
    try:
        backup = directory / "backup"
        backup.mkdir()
        for suffix, source in plan["project_files"].items():
            if source.is_file():
                shutil.copy2(source, backup / source.name)
                details = source.stat()
                record["backups"].append({"name": source.name, "size_bytes": details.st_size,
                                          "mtime_ns": details.st_mtime_ns, "sha256": _sha256(source)})
            elif suffix == "_Loaded.msp2":
                # The loader needs it beside the project; the restore made it, and its undo removes it.
                record["created_project_files"].append(source.name)
                _write_record(record_path, record)
                try:
                    with open(source, "xb"):
                        pass
                except FileExistsError:
                    record["created_project_files"].pop()  # not this restore's: never removed by its undo
                    raise
        _write_record(record_path, record)
        done = 1
        for directory_path in plan["directories"]:
            # Journalled before it is made, so that a crash leaves nothing the record does not name.
            record["created_directories"].append(_relative(directory_path, root))
            _write_record(record_path, record)
            try:
                os.mkdir(extended_path(directory_path))
            except FileExistsError:
                record["created_directories"].pop()
        for container in plan["containers"]:
            created = {"path": _relative(container["target"], root), "relative_path": container["relative_path"],
                       "size_bytes": container["size_bytes"], "sha256": None}
            record["created_files"].append(created)
            _write_record(record_path, record)
            try:
                with open(extended_path(container["source"]), "rb") as reader, \
                        open(extended_path(container["target"]), "xb") as writer:
                    shutil.copyfileobj(reader, writer, 4 * 1024 * 1024)
            except FileExistsError:
                record["created_files"].pop()  # another file took the path meanwhile: never this restore's
                raise
            shutil.copystat(extended_path(container["source"]), extended_path(container["target"]))
            digest = _sha256(container["target"])
            created["sha256"] = digest
            if container["sha256"] and digest != container["sha256"]:
                raise LibraryCopyStoreError(
                    f"the copy of {container['relative_path']} does not match the checksum retained for it"
                )
        done = 2
        link = plan["link"]
        stored = store.object_path(object_id)
        record["link"] = {"path": _relative(link, root), "object_id": object_id, "method": None}
        _write_record(record_path, record)
        try:
            os.link(extended_path(stored), extended_path(link))
            method = "link"
        except FileExistsError:
            record["link"] = None  # not this restore's link: its undo must not take it
            raise
        except OSError as error:
            # Another volume (ERROR_NOT_SAME_DEVICE, EXDEV), or a file at its link limit: a copy, verified.
            if getattr(error, "winerror", None) not in _CROSS_DEVICE_WINERRORS and error.errno != errno.EXDEV:
                record["link"]["link_refused"] = describe_error(error, root)
            shutil.copyfile(extended_path(stored), extended_path(link))
            if _sha256(link) != plan["entry"].get("sha256"):
                raise LibraryCopyStoreError(f"the copy of object {object_id} does not match it")
            method = "copy"
        details = os.stat(extended_path(link))
        record["link"].update(method=method, st_dev=details.st_dev, st_ino=details.st_ino,
                              nlink=details.st_nlink, size_bytes=details.st_size,
                              sha256=plan["entry"].get("sha256"))
        done = 3
        record["state"] = "restored"
        record["restored_at"] = _now()
    except Exception as error:  # noqa: BLE001 - recorded, and the undo takes back what the record lists
        record["state"] = "failed"
        record["error"] = describe_error(error, root)
    _write_record(record_path, record)
    try:
        _set_entry_state(path, restore_id, state=record["state"],
                         **({"restored_at": record["restored_at"]} if record["state"] == "restored" else
                            {"error": record.get("error")}),
                         library_method=(record.get("link") or {}).get("method"))
    except (OSError, ValueError) as error:
        record.setdefault("manifest_error", describe_error(error, root))
    result = {**described, "restore_id": restore_id, "record": record_name, "state": record["state"],
              "restored": record["state"] == "restored"}
    if record["state"] != "restored":
        result["message"] = (
            f"The restore stopped after {done} of 3 step(s): {record.get('error')}. Everything it made is listed in "
            "its record, and the hold stands; msdial_undo_project_restore takes it back."
        )
        return result
    method = "linked" if record["link"]["method"] == "link" else "copied"
    result["library_method"] = record["link"]["method"]
    result["message"] = (
        f"Restored {plan['project']} for viewing: {len(plan['containers'])} container(s) copied to the paths its "
        f".mddata names ({described['container_bytes']} bytes), and the loaded-library copy {method} from object "
        f"{object_id}. Sharing and raw deletion of this unit are held until msdial_undo_project_restore takes it "
        f"back. Open {plan['project']}.mdproject in the output (not the .mddata), and do not save or reprocess: "
        "MS-DIAL rewrites containers when it switches files, and a save writes the whole library copy through its "
        "TEMP directory." + raw_note
    )
    return result


# ---- undo ----------------------------------------------------------------------------------------------


def _standing_restore(manifest: dict[str, Any], restore_id: str) -> dict[str, Any]:
    restores = active_viewing_restores(manifest)
    if restore_id:
        restores = [item for item in restores if item.get("restore_id") == restore_id]
    if not restores:
        raise RestoreRefused(
            ("no viewing restore stands on this unit" if not restore_id else
             f"restore {restore_id} does not stand on this unit") + "; there is nothing to take back."
        )
    if len(restores) > 1:
        raise RestoreRefused(
            "more than one viewing restore stands on this unit; name one: "
            + ", ".join(str(item.get("restore_id")) for item in restores) + "."
        )
    return restores[0]


def _undo_plan(manifest: dict[str, Any], manifest_path: Path, restore_id: str) -> dict[str, Any]:
    hold = _standing_restore(manifest, restore_id)
    root, why = workspace_root_for(manifest, manifest_path)
    if root is None:
        raise RestoreRefused(f"the workspace root of this unit cannot be located ({why}), so the restore's record cannot be read.")
    name = str(hold.get("record") or "")
    record_path = (root / name).resolve()
    viewing = (root / VIEWING_DIRECTORY_NAME).resolve()
    if not name or viewing not in record_path.parents:
        raise RestoreRefused("the restore's hold names no record under _viewing; a person looks.")
    record = _read_json(record_path)
    if not isinstance(record, dict) or record.get("restore_id") != hold.get("restore_id"):
        raise RestoreRefused(f"the record of restore {hold.get('restore_id')} is missing or unreadable; a person looks.")
    if record.get("state") == "undone":
        raise RestoreRefused(f"restore {hold.get('restore_id')} is recorded as taken back already; a person looks.")
    store = LibraryCopyStore(root)
    output = Path(str(manifest.get("output_directory") or ""))
    plan: dict[str, Any] = {"hold": hold, "root": root, "record_path": record_path, "record": record,
                            "store": store, "output": output, "directory": record_path.parent}
    # The link, or what MS-DIAL wrote in its place.
    link = record.get("link") or {}
    plan["link"] = None
    if link.get("path"):
        target = root / link["path"]
        if os.path.lexists(extended_path(target)):
            details = os.stat(extended_path(target))
            if link.get("method") == "link":
                same = (details.st_dev, details.st_ino) == (link.get("st_dev"), link.get("st_ino"))
            elif link.get("method") == "copy":
                same = details.st_size == link.get("size_bytes") and details.st_ino == link.get("st_ino")
            else:
                same = False  # stopped between its record and the link: whatever is there was not checked
            plan["link"] = {"path": target, "action": "remove" if same else "store", "relative": link["path"]}
    files = []
    for item in record.get("created_files") or []:
        target = root / str(item.get("path") or "")
        if not item.get("path") or not os.path.lexists(extended_path(target)):
            continue
        files.append({"path": target, "relative": item["path"], "sha256": item.get("sha256")})
    plan["files"] = files
    plan["directories"] = [root / item for item in reversed(record.get("created_directories") or [])]
    backups = []
    for item in record.get("backups") or []:
        current = output / str(item["name"])
        try:
            changed = not current.is_file() or _sha256(current) != item.get("sha256")
        except OSError:
            changed = True
        if changed:
            backups.append({"name": item["name"], "current": current, "backup": record_path.parent / "backup" / item["name"],
                            "sha256": item.get("sha256")})
    plan["backups"] = backups
    plan["created_project_files"] = [output / name for name in record.get("created_project_files") or []
                                     if (output / name).is_file()]
    # What appeared that the restore did not make: reported, never deleted.
    ours = {os.path.normcase(str(item["path"])) for item in files}
    ours.update(os.path.normcase(str(output / name)) for name in record.get("created_project_files") or [])
    if plan["link"] is not None:
        ours.add(os.path.normcase(str(plan["link"]["path"])))
    elif link.get("path"):
        ours.add(os.path.normcase(str(root / link["path"])))
    known_output = set(record.get("output_snapshot") or [])
    new_files = []
    if output.is_dir():
        for entry in os.scandir(output):
            if entry.is_file() and entry.name not in known_output and os.path.normcase(entry.path) not in ours:
                new_files.append(_relative(Path(entry.path), root))
    snapshot = record.get("raw_snapshot") or {}
    restored_into = sorted({str(Path(str(item.get("path"))).parent.as_posix())
                            for item in record.get("created_files") or [] if item.get("path")})
    for relative in restored_into:
        folder = root / relative
        if not os.path.isdir(extended_path(folder)):
            continue
        before = set(snapshot.get(relative) or [])
        for entry in os.scandir(extended_path(folder)):
            path = folder / entry.name
            if entry.is_file() and entry.name not in before and os.path.normcase(str(path)) not in ours:
                new_files.append(_relative(path, root))
    plan["new_files"] = sorted(new_files)
    plan["changed_by_gui"] = []
    return plan


def _describe_undo(plan: dict[str, Any]) -> dict[str, Any]:
    root = plan["root"]
    link = plan["link"]
    return {
        "restore_id": plan["record"].get("restore_id"),
        "state": plan["record"].get("state"),
        "files_to_remove": len(plan["files"]),
        "directories_to_remove_if_empty": [_relative(item, root) for item in plan["directories"]],
        "library_copy": None if link is None else {
            "path": link["relative"],
            "action": link["action"],
            "why": None if link["action"] == "remove" else
            "the file is no longer the link the restore made: MS-DIAL's save wrote a new library copy, which goes "
            "through the library-copy store and is removed only where the store holds the same content",
        },
        "project_files_to_put_back": [item["name"] for item in plan["backups"]],
        "created_project_files_to_remove": [item.name for item in plan["created_project_files"]],
        "new_files": plan["new_files"],
    }


def plan_project_restore_undo(manifest_path: str | Path, restore_id: str = "") -> dict[str, Any]:
    from .repository_reanalysis import read_manifest

    path = Path(manifest_path).expanduser().resolve()
    return _describe_undo(_undo_plan(read_manifest(path), path, str(restore_id or "").strip()))


def _remove(path: Path, busy: list[str], root: Path) -> bool:
    try:
        os.unlink(extended_path(path))
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        busy.append(_relative(path, root))
        return False


def undo_project_restore(manifest_path: str | Path, restore_id: str = "", confirmed: bool = False) -> dict[str, Any]:
    """Take a viewing restore back. See the module docstring. A preview unless ``confirmed``."""
    from .repository_reanalysis import read_manifest, update_manifest

    path = Path(manifest_path).expanduser().resolve()
    manifest = read_manifest(path)
    plan = _undo_plan(manifest, path, str(restore_id or "").strip())
    described = _describe_undo(plan)
    if not confirmed:
        return {**described, "undone": False, "message": (
            "Nothing was removed. Show what would be removed, what MS-DIAL changed or created and the files that "
            "would be put back to the user, and call again with confirmed=true only after an explicit answer."
        )}
    root: Path = plan["root"]
    record: dict[str, Any] = plan["record"]
    record_path: Path = plan["record_path"]
    store: LibraryCopyStore = plan["store"]
    restore = str(record["restore_id"])
    busy: list[str] = []
    problems: list[str] = []
    removed = 0
    changed = []
    record["state"] = "undoing"
    _write_record(record_path, record)
    # 1. the library copy, or what MS-DIAL wrote in its place
    gui_copy = None
    link = plan["link"]
    if link is not None:
        if link["action"] == "remove":
            if _remove(link["path"], busy, root):
                removed += 1
        else:
            try:
                identity = read_copy_identity(link["path"])
                stored = store.put(link["path"], identity, unit=unit_ref(manifest, path, root),
                                   job_id=str(record.get("job_id") or ""), how="undo_gui_copy")
                gui_copy = {"schema": RECORD_SCHEMA, "action": "undo_gui_copy", "job_id": record.get("job_id"),
                            "restore_id": restore, **stored, "recorded_at": _now(),
                            "by": f"Interactive {__version__}"}
                removed += 1
            except (OSError, ValueError) as error:
                problems.append(f"{link['relative']}: {describe_error(error, root)}")
    # 2. the copies of the containers
    for item in plan["files"]:
        try:
            if item["sha256"] and _sha256(item["path"]) != item["sha256"]:
                changed.append(item["relative"])
        except OSError:
            pass
        if _remove(item["path"], busy, root):
            removed += 1
    # 3. the project files the restore backed up, as they were
    restored_files = 0
    for item in plan["backups"]:
        backup: Path = item["backup"]
        try:
            if _sha256(backup) != item["sha256"]:
                problems.append(f"the backup of {item['name']} no longer matches its checksum; it was not put back")
                continue
            if item["current"].is_file():
                saved = plan["directory"] / "gui-saved"
                saved.mkdir(exist_ok=True)
                os.replace(item["current"], saved / item["name"])
            shutil.copy2(backup, item["current"])
            restored_files += 1
        except PermissionError:
            busy.append(_relative(item["current"], root))
        except OSError as error:
            problems.append(f"{item['name']}: {describe_error(error, root)}")
    for created in plan["created_project_files"]:
        if _remove(created, busy, root):
            removed += 1
    # 4. the directories it made, deepest first, only when empty
    directories = 0
    kept_directories = []
    for folder in plan["directories"]:
        try:
            os.rmdir(extended_path(folder))
            directories += 1
        except FileNotFoundError:
            continue
        except OSError:
            kept_directories.append(_relative(folder, root))
    complete = not busy and not problems
    record.update(
        state="undone" if complete else "partial",
        undo={"at": _now(), "removed_files": removed, "removed_directories": directories,
              "kept_directories": kept_directories, "restored_project_files": restored_files,
              "changed_by_gui": changed, "new_files": plan["new_files"], "busy": busy, "problems": problems,
              "gui_copy": None if gui_copy is None else {"object_id": gui_copy.get("object_id"),
                                                         "outcome": gui_copy.get("outcome")}},
    )
    if complete:
        record["undone_at"] = record["undo"]["at"]
    _write_record(record_path, record)

    def change(current: dict[str, Any]) -> None:
        if complete:
            current[HOLDS] = [item for item in standing_holds(current) if item.get("id") != plan["hold"].get("id")]
        for item in current.get(VIEWING_RESTORES) or []:
            if isinstance(item, dict) and item.get("restore_id") == restore:
                item.update(state=record["state"], **({"undone_at": record["undone_at"]} if complete else {}),
                            undo={key: record["undo"][key] for key in
                                  ("removed_files", "removed_directories", "kept_directories",
                                   "restored_project_files", "changed_by_gui", "new_files", "busy", "problems")})
        if gui_copy is not None:
            current[LIBRARY_COPY_RECORDS] = [*(current.get(LIBRARY_COPY_RECORDS) or []), gui_copy]

    update_manifest(path, change)
    result = {**described, "undone": complete, "state": record["state"], "removed_files": removed,
              "removed_directories": directories, "kept_directories": kept_directories,
              "restored_project_files": restored_files, "changed_by_gui": changed, "busy": busy,
              "problems": problems}
    notes = []
    if changed:
        notes.append(f"{len(changed)} restored container(s) were rewritten by MS-DIAL while it was open (the first is "
                     f"{changed[0]}); they are removed, and the retained copies in msdial-intermediates are unchanged.")
    if gui_copy is not None:
        notes.append(f"MS-DIAL's save had replaced the link with a library copy of its own; it went to the "
                     f"library-copy store ({gui_copy.get('outcome')} as object {gui_copy.get('object_id')}).")
    if plan["new_files"]:
        notes.append(f"{len(plan['new_files'])} file(s) appeared that the restore did not make (the first is "
                     f"{plan['new_files'][0]}); they were left where they are for a person to look at.")
    if kept_directories:
        notes.append(f"{len(kept_directories)} director(y/ies) the restore made are not empty and were kept (the "
                     f"first is {kept_directories[0]}).")
    if complete:
        result["message"] = (
            f"Restore {restore} is taken back: {removed} file(s) and {directories} director(y/ies) removed, "
            f"{restored_files} project file(s) put back from their backups; the hold on sharing and raw deletion "
            "is lifted." + (" " + " ".join(notes) if notes else "")
        )
    else:
        reasons = []
        if busy:
            reasons.append(f"{len(busy)} file(s) are still open in another program ({busy[0]})")
        reasons.extend(problems)
        result["message"] = (
            f"Restore {restore} is partly taken back and still held: " + "; ".join(reasons)
            + ". Close MS-DIAL and call again." + (" " + " ".join(notes) if notes else "")
        )
    return result
