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
   whose ``restore.local.json`` lists every path the restore made, the output directory among them - written
   before each step, so that a restore that stopped half way is taken back as completely as one that finished;
4. records ``viewing_restores[]`` in the unit manifest and a hold ``{step: viewing_restore, blocks: [sharing,
   raw_deletion]}``: no publication, raw cleanup, discard or split-parent release while it stands, no retry
   lifts it (run_finalisation._retry_hold returns a step it does not know as it stands), and no Console job or
   new production run of the unit starts.

It refuses a unit with no run finalisation, no store object for the run, a restore already standing, any
other finalisation hold (a retry would undo the restore), a run, a retention-time preview or a download under
way, a raw deletion that has begun and not finished, an output already holding a library copy, any target path
that already exists, and any path it would write outside the workspace root it records paths relative to. From
before its hold is written until its last copy is made it holds the raw-deletion locks of the unit and, for a
split part, of the unit whose raw tree it reads, taken without waiting: a cleanup, a discard or a split parent's
release under way refuses it, and none starts meanwhile; and as it records the hold it reads the deletion state
and the live jobs again under the manifest's writer lock. The containers it restores are those the run's
finalisation moved and those a retry of its held move moved later. A preview (``confirmed=False``) writes
nothing and names every path relative to the workspace root.

undo_project_restore removes exactly what the record lists, puts back a project file whose sha256 changed from
its backup (keeping the changed one under the restore's directory, never over an earlier one), reports - never
deletes - any other file that appeared in the output or in the raw-side directories the restore copied into,
and lifts the hold, recording the outcome in ``viewing_restores``. A file MS-DIAL wrote over the link on a
save, and a library copy a "Save as" wrote beside it, is a new library copy: it goes through the library-copy
store, which removes it only where it holds the same content, and is recorded as the GUI's (undo_gui_copy),
never as the run's; its record is kept whole in the restore's record as it is stored, and named in the unit
manifest by whichever call first writes it. A file the restore made itself - its link, or its copy of the object
however far that got - is removed outright, and only as the file the undo read: renamed aside and compared first,
so a save MS-DIAL makes on close in between is stored as the GUI's instead. A file another program still holds
open, a directory the restore made that now holds files it did not make, a library copy saved while the undo
ran, and a path it could not name are left, and the restore stays held until a later call; so does a manifest
that could not be written, and the next call only lifts the hold. Two split parts restored into one released raw
tree share its directories: the other part's copies are never reported as new, and a directory this restore made
that holds only them keeps it held until that restore is undone.

Nothing here launches MS-DIAL.
"""

from __future__ import annotations

import datetime as dt
import errno
import os
import shutil
import sys
import uuid
import zlib
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import __version__
from .diagnostic_paths import INTERMEDIATES_DIRECTORY, extended_path, path_is_file, plain_path
from .download_store import StoreError, _read_json, _write_json_atomic
from .library_copy_store import (
    LIBRARY_COPY_RECORD,
    LIBRARY_COPY_RECORDS,
    LOADED_LIBRARY_COPY_SUFFIX,
    PENDING,
    RECORD_SCHEMA,
    RESTORABLE_OUTCOMES,
    VIEWING_DIRECTORY_NAME,
    LibraryCopyPending,
    LibraryCopyStore,
    LibraryCopyStoreError,
    describe_error,
    file_fingerprint,
    is_library_copy_file,
    is_run_copy,
    legacy_deleted_copies,
    private_name,
    read_copy_identity,
    rename_back,
    unit_ref,
    workspace_root_for,
)
from .run_finalisation import (
    BLOCKS_RAW_DELETION,
    BLOCKS_SHARING,
    HOLD_RESOLUTIONS,
    HOLDS,
    VIEWING_RESTORE,
    _console_inputs,
    _hold,
    _split_parts,
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
# An undo that took back everything the restore made and could not then write the unit manifest: the next call
# only lifts the hold. "undone" is written once the manifest no longer holds the unit.
LIFTING_HOLD = "lifting_hold"


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


def _inside(path: Path, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _library_copy_of(manifest: dict[str, Any], job_id: str) -> tuple[dict[str, Any] | None, str]:
    """The record naming the store object of a run's library copy, or None and why not.

    Only the run's own copy counts: its finalisation's, a retry's, a repair's or an attach's. A copy MS-DIAL's
    GUI wrote during an earlier restore, or one a Console left in its temporary directory, is recorded under the
    run's job too, and is never the copy the run reopens from.
    """
    finalisation = manifest.get("console_run_finalisation") or {}
    found: list[dict[str, Any]] = []
    if str(finalisation.get("job_id") or "") == job_id:
        block = finalisation.get(LIBRARY_COPY_RECORD) if isinstance(finalisation.get(LIBRARY_COPY_RECORD), dict) else {}
        found.extend(item for item in (block or {}).get("copies") or [] if isinstance(item, dict))
    found.extend(
        item for item in manifest.get(LIBRARY_COPY_RECORDS) or []
        if isinstance(item, dict) and str(item.get("job_id") or "") == job_id
    )
    found = [item for item in found if is_run_copy(item)]
    if not found:
        # Its finalisation's deleted copies, and those a retry of its held deletion deleted (before 0.5.37).
        if legacy_deleted_copies(manifest, job_id):
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


def _live_job(manifest_path: Path, timeout: float = -1) -> str:
    """A Console job of this unit this process has queued or running - a run, a diagnostic or a retention-time
    preview, which reads the unit's raw files too - or is still preparing, holding the unit's slot before it is
    registered (server._single_console_per_unit). Read only if its backend is loaded.

    ``timeout`` bounds the wait for the registry's lock, for a caller that holds the unit manifest's writer lock:
    a failing job records its failure in that manifest while it holds the registry's. A registry that stays busy
    refuses the restore rather than leave it unknown whether a run is starting.
    """
    server = sys.modules.get("msdial_app.server")
    if server is None:
        return ""
    try:
        key = server._unit_key(str(manifest_path))
        if not server.JOBS_LOCK.acquire(timeout=timeout):
            raise RestoreRefused("this backend's job registry stayed busy, so whether a run of this unit is starting "
                                 "is not known; call again.")
        try:
            return str(server.CONSOLE_SLOTS.get(key) or "") or server._live_console_job_locked(
                key, server.UNIT_JOB_KINDS)
        finally:
            server.JOBS_LOCK.release()
    except RestoreRefused:
        raise
    except Exception:  # noqa: BLE001 - a registry that cannot be read refuses nothing by itself
        return ""


def _moved_containers(
    manifest: dict[str, Any], relocation: dict[str, Any], job_id: str
) -> tuple[list[dict[str, Any]], list[str]]:
    """The containers a run's finalisation moved and those a retry of its held move moved later; and what is left.

    A container finalisation could not move is pending under a msdial_intermediates hold, and the retry that
    moves it records it in that hold's resolution (finalisation_hold_resolutions), not in the run's record. Left
    is every pending container no retry moved, or the run's own errors where no retry ever settled them.
    """
    moved: dict[str, dict[str, Any]] = {}
    notes = [note for note in manifest.get(HOLD_RESOLUTIONS) or []
             if isinstance(note, dict) and note.get("step") == "msdial_intermediates"
             and str(note.get("job_id") or "") == job_id]
    for item in [*(relocation.get("moved") or []), *(item for note in notes for item in note.get("moved") or [])]:
        if isinstance(item, dict) and str(item.get("relative_path") or "").strip():
            moved[os.path.normcase(str(item["relative_path"]))] = item
    retried = bool(notes)
    left = [str(item) for item in relocation.get("pending") or [] if os.path.normcase(str(item)) not in moved]
    if not left and relocation.get("errors") and not retried:
        left = [str(item) for item in relocation["errors"]][:1]
    return list(moved.values()), left


def _deletion_under_way(manifest: dict[str, Any], manifest_path: Path) -> str:
    """Why a deletion of the raw tree this unit reads has begun and not finished, or "": its own raw_deletion,
    and for a split part its owner's raw_release."""
    from .repository_reanalysis import DELETION_RESUMABLE_STATES, _owner_manifest_path, read_manifest

    deletion = manifest.get("raw_deletion") if isinstance(manifest.get("raw_deletion"), dict) else {}
    if str((deletion or {}).get("state") or "") in DELETION_RESUMABLE_STATES:
        return ("a deletion of this unit's raw data has begun and not finished; a restore would write into what it "
                "deletes. Finish or settle the deletion first.")
    owner = _owner_manifest_path(manifest, manifest_path)
    if owner is not None and owner.is_file():
        try:
            release = read_manifest(owner).get("raw_release") or {}
        except (OSError, ValueError):
            release = {}
        if str((release if isinstance(release, dict) else {}).get("state") or "") in DELETION_RESUMABLE_STATES:
            return ("the release of the raw tree this part reads has begun and not finished; a restore would write "
                    "into what it deletes.")
    return ""


@contextmanager
def _deletion_locks(manifest: dict[str, Any], manifest_path: Path) -> Iterator[None]:
    """The raw-deletion locks of this unit and, for a split part, of the unit whose raw tree it reads: taken
    without waiting, and held while a restore writes its hold and its copies.

    A cleanup, a discard and a split parent's release each take theirs before they read the holds, and keep it
    until the tree is gone: while one holds it a restore is refused, and while a restore holds it none starts;
    one that starts afterwards finds the restore's hold.
    """
    from .repository_reanalysis import ManifestBusyError, _owner_manifest_path, _raw_deletion_lock

    paths = [manifest_path]
    owner = _owner_manifest_path(manifest, manifest_path)
    if owner is not None and owner.is_file():
        paths.append(owner.resolve())
    with ExitStack() as stack:
        for item in paths:
            try:
                stack.enter_context(_raw_deletion_lock(item))
            except ManifestBusyError as error:
                raise RestoreRefused(
                    "a deletion of this unit's raw data, or of the raw tree it reads, is under way and holds its "
                    "lock; restore once it has finished."
                ) from error
        yield


def _refuse_live(manifest: dict[str, Any], manifest_path: Path) -> None:
    from .repository_reanalysis import _live_run_attempt_in

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
    deletion = _deletion_under_way(manifest, manifest_path)
    if deletion:
        raise RestoreRefused(deletion)


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
    moved, unmoved = _moved_containers(manifest, relocation or {}, current)
    if not moved:
        raise RestoreRefused(
            f"run {current} moved no MS-DIAL containers (it did not complete), so there is nothing to restore."
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
    if unmoved:
        raise RestoreRefused(
            f"{len(unmoved)} of run {current}'s MS-DIAL containers were never moved out of the raw directory by its "
            f"finalisation or a retry (the first is {unmoved[0]}), so the project cannot be restored whole."
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
    try:
        entry = store.entry(object_id)
    except (OSError, StoreError) as error:
        raise RestoreRefused(
            f"the record of library-copy object {object_id} cannot be read ({describe_error(error, root)})."
        ) from error
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
            f"the output already holds {name}. If MS-DIAL wrote it, it is a copy of the loaded libraries outside "
            "the library-copy store, and a person decides what it is before anything is linked over it."
        )
    loose = sorted(item.name for item in output.glob("*" + LOADED_LIBRARY_COPY_SUFFIX) if item.is_file())
    if loose:
        # Its undo stores every library copy that appears in the output; one already there would be neither
        # the restore's nor new, and nothing would take it away.
        raise RestoreRefused(
            f"the output already holds {loose[0]}, a copy of the loaded libraries outside the library-copy store; "
            "a person decides what it is before the project is restored beside it."
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
    outside = [path for path in [output, link, *(item["target"] for item in containers), *directories]
               if not _inside(path, root)]
    if outside:
        # The record names every path relative to the root, so its undo could find none of these.
        raise RestoreRefused(
            f"{len(outside)} path(s) it would write lie outside the workspace root of its library-copy store (the "
            f"first is named {outside[0].name}); a restore records every path relative to that root, and its undo "
            "could not take them back."
        )
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


def _planned(manifest_path: Path, job_id: str) -> dict[str, Any]:
    """_plan, with a file it could not read refused by name: an OSError's own text holds absolute paths."""
    try:
        return _plan(manifest_path, job_id)
    except RestoreRefused:
        raise
    except (OSError, StoreError) as error:
        raise RestoreRefused(f"a file the restore reads could not be read ({describe_error(error)}).") from error


def plan_project_restore(manifest_path: str | Path, job_id: str = "") -> dict[str, Any]:
    """What restore_project_for_viewing would do, with every path relative to the workspace root. Writes nothing."""
    path = Path(manifest_path).expanduser().resolve()
    plan = _planned(path, str(job_id or "").strip())
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


def _copy_object(
    stored: Path, link: Path, record: dict[str, Any], record_path: Path, entry: dict[str, Any], object_id: str
) -> None:
    """A verified copy of a store object at the link's place, journalled by its file record before its first byte.

    The undo knows the file by that record (copy_pending, and its file id once it exists) and removes it as the
    restore's own, however far the copy got: never taking it for a library copy MS-DIAL saved. One that does not
    match the object is removed here.
    """
    record["link"]["method"] = "copy_pending"
    _write_record(record_path, record)
    with open(extended_path(stored), "rb") as reader:
        try:
            writer = open(extended_path(link), "xb")
        except FileExistsError:
            record["link"] = None  # not this restore's file: its undo must not take it
            raise
        with writer:
            details = os.fstat(writer.fileno())
            record["link"].update(st_dev=details.st_dev, st_ino=details.st_ino)
            _write_record(record_path, record)
            shutil.copyfileobj(reader, writer, 4 * 1024 * 1024)
    if _sha256(link) != entry.get("sha256"):
        try:
            os.unlink(extended_path(link))
            record["link"] = None
        except OSError:
            pass  # still recorded as copy_pending: the undo removes it by its file record
        raise LibraryCopyStoreError(f"the copy of object {object_id} does not match it")


def restore_project_for_viewing(manifest_path: str | Path, job_id: str = "", confirmed: bool = False) -> dict[str, Any]:
    """Make a finalised campaign unit's project openable in the MS-DIAL GUI, for viewing. See the module docstring.

    A preview unless ``confirmed``. Refusals raise RestoreRefused (a ValueError) before anything is written. A
    failure part way is recorded (state ``failed``) and returned; the hold stands, and the undo takes back what
    the record lists.
    """
    path = Path(manifest_path).expanduser().resolve()
    plan = _planned(path, str(job_id or "").strip())
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
    # From before its hold is written until its copies are made: a deletion of the raw tree it writes into that
    # holds these locks is under way, and one that takes them later finds the hold.
    with _deletion_locks(plan["manifest"], path):
        return _restore(plan, path, described, raw_note)


def _restore(plan: dict[str, Any], path: Path, described: dict[str, Any], raw_note: str) -> dict[str, Any]:
    """restore_project_for_viewing once confirmed, under the deletion locks."""
    from .repository_reanalysis import _live_run_attempt_in, update_manifest

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
        # The output the restore wrote into, which its undo reads, whatever the manifest names by then.
        "output": _relative(output, root),
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
        deletion = _deletion_under_way(current, path)
        if deletion:
            raise RestoreRefused(deletion)
        # A Console job of the unit queued, running or still preparing since the plan was read; one registered
        # from now on reads this hold before its Console starts (server._run_console_for_job).
        job = _live_job(path, timeout=5.0)
        if job:
            raise RestoreRefused(f"a run of this unit is live (job {job} is queued or running).")
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
            _copy_object(stored, link, record, record_path, plan["entry"], object_id)
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


def _recorded(root: Path, text: Any, problems: list[str]) -> Path | None:
    """A path the restore's record names relative to the workspace root; None, and a problem, where it could not."""
    text = str(text or "")
    if not text:
        return None
    if text.startswith("<outside"):
        # Not a path that is gone: one the record could not name, so a person looks for it.
        problems.append(f"the restore recorded {text}, which is not named relative to the workspace root; a person "
                        "looks for it and removes it")
        return None
    return root / text


def _is_gui_library_copy(path: Path) -> bool:
    return path.name.endswith(LOADED_LIBRARY_COPY_SUFFIX) or is_library_copy_file(path)


def _other_restores(
    manifest: dict[str, Any], manifest_path: Path, root: Path, restore_id: str
) -> list[dict[str, Any]]:
    """The records of the other viewing restores standing on the raw tree this unit reads.

    A split part restores into its owner's raw tree, which the owner's other parts read too: two parts restored
    while the tree is released share the directories the first one made, and the copies of each lie beside the
    other's. Read from the viewing_restore holds of the owner's other parts, as the gate reads them.
    """
    from .repository_reanalysis import _owner_manifest_path, read_manifest

    owner = _owner_manifest_path(manifest, manifest_path)
    if owner is None or not owner.is_file():
        return []
    try:
        parts = _split_parts(read_manifest(owner))
    except (OSError, ValueError):
        return []
    found = []
    for part in parts:
        try:
            if os.path.normcase(str(part.resolve())) == os.path.normcase(str(manifest_path.resolve())):
                continue
            recorded = read_manifest(part)
        except (OSError, ValueError):
            continue
        for hold in active_viewing_restores(recorded):
            if not hold.get("record") or hold.get("restore_id") == restore_id:
                continue
            try:
                record = _read_json(root / str(hold["record"]))
            except (OSError, StoreError):
                record = None
            if isinstance(record, dict):
                found.append({**record, "manifest": record.get("manifest") or _relative(part, root)})
    return found


def _holds_only(folder: Path, theirs: set[str]) -> bool:
    """Whether ``folder`` holds files, every one of them made by another standing restore (``theirs``)."""
    found = False
    for directory, _folders, names in os.walk(extended_path(folder)):
        for name in names:
            if os.path.normcase(plain_path(os.path.join(directory, name))) not in theirs:
                return False
            found = True
    return found


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
    store = LibraryCopyStore(root)
    problems: list[str] = []
    # The output the restore wrote into, as its record names it; a record written before it named one, the
    # manifest's (a new run is refused while a restore stands, so the two are one).
    output = (_recorded(root, record.get("output"), problems) if record.get("output")
              else Path(str(manifest.get("output_directory") or "")))
    plan: dict[str, Any] = {"hold": hold, "root": root, "record_path": record_path, "record": record,
                            "store": store, "output": output, "directory": record_path.parent, "problems": problems,
                            "lift_only": record.get("state") in {LIFTING_HOLD, "undone"}, "others": [],
                            "theirs": set()}
    if plan["lift_only"]:
        # Everything it made was taken back by an earlier call, which could not then write the unit manifest:
        # only the hold is left to lift, and the GUI's library copies that call stored to name.
        plan.update(link=None, files=[], directories=[], backups=[], created_project_files=[], library_copies=[],
                    new_files=list((record.get("undo") or {}).get("new_files") or []))
        return plan
    # Another part's restore into the same raw tree: its copies are not new files of this one, and a directory
    # this restore made that holds only them waits for that restore's undo.
    others = _other_restores(manifest, manifest_path, root, str(hold.get("restore_id") or ""))
    plan["others"] = [{"restore_id": item.get("restore_id"), "manifest": item.get("manifest")} for item in others]
    plan["theirs"] = {
        os.path.normcase(str(root / item["path"])) for other in others for item in other.get("created_files") or []
        if isinstance(item, dict) and item.get("path") and not str(item["path"]).startswith("<outside")
    }
    # The link, or what MS-DIAL wrote in its place.
    link = record.get("link") or {}
    plan["link"] = None
    target = _recorded(root, link.get("path"), problems)
    if target is not None and os.path.lexists(extended_path(target)):
        details = os.stat(extended_path(target))
        ids = (details.st_dev, details.st_ino)
        recorded_ids = (link.get("st_dev"), link.get("st_ino"))
        method = link.get("method")
        if method == "link":
            same = ids == recorded_ids
        elif method == "copy":
            same = ids == recorded_ids and details.st_size == link.get("size_bytes")
        elif method == "copy_pending":
            # The restore's own copy of the object, however far it got: by its file record once it had one,
            # and before that whatever is there was made by the restore, which never said it had restored.
            same = link.get("st_ino") is None or ids == recorded_ids
        else:
            # Stopped between its record and os.link: what is there is the restore's link only if it is the
            # object's own file record.
            try:
                stored = os.stat(extended_path(store.object_path(str(record.get("object_id") or ""))))
                same = ids == (stored.st_dev, stored.st_ino)
            except OSError:
                same = False
        # The file read here, by file record, size and time: the one the undo removes as the restore's, and no
        # other (_remove_if_same).
        plan["link"] = {"path": target, "action": "remove" if same else "store", "relative": link["path"],
                        "fingerprint": (details.st_dev, details.st_ino, details.st_size, details.st_mtime_ns)}
    files = []
    for item in record.get("created_files") or []:
        target = _recorded(root, item.get("path"), problems)
        if target is None or not os.path.lexists(extended_path(target)):
            continue
        files.append({"path": target, "relative": item["path"], "sha256": item.get("sha256")})
    plan["files"] = files
    plan["directories"] = [path for path in (_recorded(root, item, problems)
                                             for item in reversed(record.get("created_directories") or []))
                           if path is not None]
    backups = []
    for item in record.get("backups") or [] if output is not None else []:
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
                                     if output is not None and (output / name).is_file()]
    # What appeared that the restore did not make: reported, never deleted - but for a library copy, which goes
    # through the store, as the one MS-DIAL wrote over the link does ("Save as" writes <name>_Loaded.msp2.dbs).
    ours = {os.path.normcase(str(item["path"])) for item in files}
    if output is not None:
        ours.update(os.path.normcase(str(output / name)) for name in record.get("created_project_files") or [])
    if plan["link"] is not None:
        ours.add(os.path.normcase(str(plan["link"]["path"])))
    elif link.get("path") and not str(link["path"]).startswith("<outside"):
        ours.add(os.path.normcase(str(root / link["path"])))
    known_output = set(record.get("output_snapshot") or [])
    new_files = []
    library_copies = []
    if output is not None and output.is_dir():
        for entry in os.scandir(output):
            if entry.is_file() and entry.name not in known_output and os.path.normcase(entry.path) not in ours:
                if _is_gui_library_copy(Path(entry.path)):
                    library_copies.append(Path(entry.path))
                else:
                    new_files.append(_relative(Path(entry.path), root))
    snapshot = record.get("raw_snapshot") or {}
    restored_into = sorted({str(Path(str(item.get("path"))).parent.as_posix())
                            for item in record.get("created_files") or []
                            if item.get("path") and not str(item["path"]).startswith("<outside")})
    for relative in restored_into:
        folder = root / relative
        if not os.path.isdir(extended_path(folder)):
            continue
        before = set(snapshot.get(relative) or [])
        for entry in os.scandir(extended_path(folder)):
            path = folder / entry.name
            key = os.path.normcase(str(path))
            if entry.is_file() and entry.name not in before and key not in ours and key not in plan["theirs"]:
                new_files.append(_relative(path, root))
    plan["new_files"] = sorted(new_files)
    plan["library_copies"] = sorted(library_copies)
    plan["changed_by_gui"] = []
    return plan


def _describe_undo(plan: dict[str, Any]) -> dict[str, Any]:
    root = plan["root"]
    link = plan["link"]
    return {
        "restore_id": plan["record"].get("restore_id"),
        "state": plan["record"].get("state"),
        "lift_only": plan["lift_only"],
        "files_to_remove": len(plan["files"]),
        "directories_to_remove_if_empty": [_relative(item, root) for item in plan["directories"]],
        "library_copy": None if link is None else {
            "path": link["relative"],
            "action": link["action"],
            "why": None if link["action"] == "remove" else
            "the file is no longer the link the restore made: MS-DIAL's save wrote a new library copy, which goes "
            "through the library-copy store and is removed only where the store holds the same content",
        },
        "library_copies_to_store": [_relative(item, root) for item in plan["library_copies"]],
        "project_files_to_put_back": [item["name"] for item in plan["backups"]],
        "created_project_files_to_remove": [item.name for item in plan["created_project_files"]],
        "new_files": plan["new_files"],
        "other_restores": list(plan["others"]),
        "problems": list(plan["problems"]),
    }


def _planned_undo(manifest: dict[str, Any], manifest_path: Path, restore_id: str) -> dict[str, Any]:
    """_undo_plan, with a file it could not read refused by name: an OSError's own text holds absolute paths."""
    try:
        return _undo_plan(manifest, manifest_path, restore_id)
    except RestoreRefused:
        raise
    except (OSError, StoreError) as error:
        raise RestoreRefused(f"a file the undo reads could not be read ({describe_error(error)}).") from error


def plan_project_restore_undo(manifest_path: str | Path, restore_id: str = "") -> dict[str, Any]:
    from .repository_reanalysis import read_manifest

    path = Path(manifest_path).expanduser().resolve()
    return _describe_undo(_planned_undo(read_manifest(path), path, str(restore_id or "").strip()))


def _remove(path: Path, busy: list[str], root: Path) -> bool:
    try:
        os.unlink(extended_path(path))
        return True
    except FileNotFoundError:
        return True
    except PermissionError:
        busy.append(_relative(path, root))
        return False


def _remove_if_same(
    path: Path, fingerprint: tuple[int, int, int, int], busy: list[str], problems: list[str], root: Path
) -> str:
    """Remove the file the undo's plan read at ``path``, and no other: 'removed', 'gone', 'changed', 'busy', 'kept'.

    It is renamed to a private name beside it and compared there with what the plan read (file record, size and
    time) before it is removed. MS-DIAL saves the library copy by deleting it and moving a new file into place,
    and holds nothing open between saves, so a save on close can land between the plan and the removal: what it
    wrote is put back, never over a newer file, and goes through the store as the GUI's ('changed').
    """
    private = path.with_name(private_name(path.name, "undo"))
    try:
        os.rename(extended_path(path), extended_path(private))
    except FileNotFoundError:
        return "gone"  # a save that deleted it; the file it moves into place is found by the closing scan
    except PermissionError:
        busy.append(_relative(path, root))
        return "busy"
    try:
        same = file_fingerprint(private) == tuple(fingerprint)
    except OSError:
        same = False
    if not same:
        try:
            rename_back(private, path)
        except OSError as error:
            problems.append(f"{_relative(private, root)}: MS-DIAL saved over the link while the undo ran, and another "
                            f"file has taken its name since ({describe_error(error, root)}); call again, and both go "
                            "through the library-copy store")
            return "kept"
        return "changed"
    try:
        os.unlink(extended_path(private))
    except PermissionError:
        try:
            rename_back(private, path)
        except OSError:
            pass  # under its private name, which keeps the suffix the closing scan reads
        busy.append(_relative(path, root))
        return "busy"
    return "removed"


def _gui_copy(
    store: LibraryCopyStore, path: Path, *, manifest: dict[str, Any], manifest_path: Path, root: Path,
    record: dict[str, Any], problems: list[str],
) -> dict[str, Any] | None:
    """A library copy MS-DIAL's GUI wrote, put through the store as the GUI's - never as the run's own copy.

    Removed only where the store holds the same content, or stored; one the store could not take stays, and is a
    problem that keeps the restore held. One that left for the store and whose install stopped is recorded as
    pending, which the store's repair completes.
    """
    restore = str(record.get("restore_id") or "")
    try:
        stored = store.put(path, read_copy_identity(path), unit=unit_ref(manifest, manifest_path, root),
                           job_id=str(record.get("job_id") or ""), how="undo_gui_copy")
    except LibraryCopyPending as error:
        stored = {**error.record, "outcome": PENDING, "store_error": describe_error(error, root),
                  "incoming": error.incoming}
    except (OSError, ValueError, zlib.error) as error:
        problems.append(f"{_relative(path, root)}: {describe_error(error, root)}")
        return None
    return {"schema": RECORD_SCHEMA, "action": "undo_gui_copy", "job_id": record.get("job_id"),
            "restore_id": restore, **stored, "recorded_at": _now(), "by": f"Interactive {__version__}"}


def _keep_aside(directory: Path) -> Path:
    """A directory of its own, for the project files one undo call keeps: a later call never writes over them."""
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    for index in range(100):
        candidate = directory / "gui-saved" / (stamp if not index else f"{stamp}-{index}")
        try:
            candidate.mkdir(parents=True)
            return candidate
        except FileExistsError:
            continue
    raise FileExistsError(errno.EEXIST, "no free directory for the files MS-DIAL saved", "gui-saved")


def _copy_summary(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"name": item.get("name"), "object_id": item.get("object_id"), "outcome": item.get("outcome")}
            for item in items]


def undo_project_restore(manifest_path: str | Path, restore_id: str = "", confirmed: bool = False) -> dict[str, Any]:
    """Take a viewing restore back. See the module docstring. A preview unless ``confirmed``."""
    from .repository_reanalysis import read_manifest, update_manifest

    path = Path(manifest_path).expanduser().resolve()
    manifest = read_manifest(path)
    plan = _planned_undo(manifest, path, str(restore_id or "").strip())
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
    problems: list[str] = list(plan["problems"])
    removed = 0
    changed = []
    # Every library copy MS-DIAL's GUI wrote that an undo call of this restore put through the store, kept whole
    # in its record as each is stored and named in the unit manifest by whichever call first writes it: a call
    # whose manifest write fails loses none of them, the one that only lifts the hold included.
    gui_records: list[dict[str, Any]] = [item for item in record.get("gui_copy_records") or [] if isinstance(item, dict)]
    gui_copies: list[dict[str, Any]] = []
    unstored: set[str] = set()
    saved_aside: list[str] = []
    directories = 0
    kept_directories: list[str] = []
    shared_directories: list[str] = []
    restored_files = 0

    def keep_gui_copy(path_: Path, item: dict[str, Any] | None) -> None:
        nonlocal removed
        if item is None:
            unstored.add(path_.name)
            return
        gui_copies.append(item)
        gui_records.append(item)
        record["gui_copy_records"] = list(gui_records)
        removed += 1
        try:
            _write_record(record_path, record)
        except OSError as error:
            problems.append(f"{_relative(record_path, root)}: {describe_error(error, root)}")

    if not plan["lift_only"]:
        record["state"] = "undoing"
        _write_record(record_path, record)
        # 1. the library copy, or what MS-DIAL wrote in its place; and any library copy a "Save as" wrote
        link = plan["link"]
        if link is not None:
            if link["action"] == "remove":
                outcome = _remove_if_same(link["path"], link["fingerprint"], busy, problems, root)
                if outcome == "removed":
                    removed += 1
                elif outcome == "changed":
                    # Saved over after the plan read it: a new library copy, which goes through the store.
                    keep_gui_copy(link["path"], _gui_copy(store, link["path"], manifest=manifest, manifest_path=path,
                                                          root=root, record=record, problems=problems))
                elif outcome in {"busy", "kept"}:
                    unstored.add(link["path"].name)
            else:
                keep_gui_copy(link["path"], _gui_copy(store, link["path"], manifest=manifest, manifest_path=path,
                                                      root=root, record=record, problems=problems))
        for copy in plan["library_copies"]:
            keep_gui_copy(copy, _gui_copy(store, copy, manifest=manifest, manifest_path=path, root=root,
                                          record=record, problems=problems))
        # 2. the copies of the containers
        for item in plan["files"]:
            try:
                if item["sha256"] and _sha256(item["path"]) != item["sha256"]:
                    changed.append(item["relative"])
            except OSError:
                pass
            if _remove(item["path"], busy, root):
                removed += 1
        # 3. the project files the restore backed up, as they were; what MS-DIAL saved is kept, never written over
        aside: Path | None = None
        for item in plan["backups"]:
            backup: Path = item["backup"]
            try:
                if _sha256(backup) != item["sha256"]:
                    problems.append(f"the backup of {item['name']} no longer matches its checksum; it was not put back")
                    continue
                if item["current"].is_file():
                    aside = aside or _keep_aside(plan["directory"])
                    os.replace(item["current"], aside / item["name"])
                    saved_aside.append(_relative(aside / item["name"], root))
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
        for folder in plan["directories"]:
            try:
                os.rmdir(extended_path(folder))
                directories += 1
            except FileNotFoundError:
                continue
            except OSError:
                if plan["theirs"] and _holds_only(folder, plan["theirs"]):
                    shared_directories.append(_relative(folder, root))
                else:
                    kept_directories.append(_relative(folder, root))
        # 5. a library copy MS-DIAL saved while this ran - after the plan read the output - is one more for the
        # store: the restore stays held, and the next call stores it.
        output = plan["output"]
        if output is not None and output.is_dir():
            late = sorted(entry.name for entry in os.scandir(output)
                          if entry.is_file() and entry.name.endswith(LOADED_LIBRARY_COPY_SUFFIX)
                          and entry.name not in unstored)
            if late:
                problems.append(f"{len(late)} library cop(ies) MS-DIAL saved while the undo ran are in the output (the "
                                f"first is {late[0]}); call again, and they go through the library-copy store")
        # A directory the restore made that holds files it did not make is the restore's still: under a released
        # raw tree it makes the tree read as present against its recorded deletion. A person clears it first -
        # unless what it holds is another standing restore's, whose own undo comes first.
        complete = not busy and not problems and not kept_directories and not shared_directories
        record.update(
            state=LIFTING_HOLD if complete else "partial",
            undo={"at": _now(), "removed_files": removed, "removed_directories": directories,
                  "kept_directories": kept_directories, "shared_directories": shared_directories,
                  "waiting_for": [item["restore_id"] for item in plan["others"]] if shared_directories else [],
                  "restored_project_files": restored_files, "changed_by_gui": changed, "new_files": plan["new_files"],
                  "gui_saved": saved_aside, "busy": busy, "problems": problems,
                  "gui_copies": _copy_summary(gui_records)},
        )
        _write_record(record_path, record)
    else:
        complete = True
        undo = record.get("undo") or {}
        removed, directories = int(undo.get("removed_files") or 0), int(undo.get("removed_directories") or 0)
        restored_files = int(undo.get("restored_project_files") or 0)
        changed, saved_aside = list(undo.get("changed_by_gui") or []), list(undo.get("gui_saved") or [])
    undo = {key: (record.get("undo") or {}).get(key) for key in
            ("removed_files", "removed_directories", "kept_directories", "shared_directories", "waiting_for",
             "restored_project_files", "changed_by_gui", "new_files", "gui_saved", "busy", "problems", "gui_copies")}
    undone_at = _now()

    def change(current: dict[str, Any]) -> None:
        if complete:
            current[HOLDS] = [item for item in standing_holds(current) if item.get("id") != plan["hold"].get("id")]
        for item in current.get(VIEWING_RESTORES) or []:
            if isinstance(item, dict) and item.get("restore_id") == restore:
                item.update(state="undone" if complete else "partial",
                            **({"undone_at": undone_at} if complete else {}), undo=undo)
        recorded = [item for item in current.get(LIBRARY_COPY_RECORDS) or [] if isinstance(item, dict)]
        named = {(str(item.get("restore_id") or ""), item.get("object_id"), item.get("sha256"))
                 for item in recorded if item.get("action") == "undo_gui_copy"}
        missing = [item for item in gui_records
                   if (restore, item.get("object_id"), item.get("sha256")) not in named]
        if missing:
            current[LIBRARY_COPY_RECORDS] = [*(current.get(LIBRARY_COPY_RECORDS) or []), *missing]

    manifest_error = None
    try:
        update_manifest(path, change)
    except (OSError, ValueError) as error:
        # The record says lifting_hold (or partial): the next call finds the hold standing and lifts it, and
        # names the GUI's copies this one stored.
        manifest_error = describe_error(error, root)
    if complete and manifest_error is None:
        record.update(state="undone", undone_at=undone_at)
        try:
            _write_record(record_path, record)
        except OSError:
            pass  # the hold is lifted; the record's lifting_hold says no more than that the last write was missed
    undone = complete and manifest_error is None
    result = {**described, "undone": undone, "state": record["state"], "removed_files": removed,
              "removed_directories": directories, "kept_directories": kept_directories,
              "shared_directories": shared_directories, "restored_project_files": restored_files,
              "changed_by_gui": changed, "gui_saved": saved_aside, "gui_library_copies": _copy_summary(gui_records),
              "busy": busy, "problems": problems}
    notes = []
    if changed:
        notes.append(f"{len(changed)} restored container(s) were rewritten by MS-DIAL while it was open (the first is "
                     f"{changed[0]}); they are removed, and the retained copies in msdial-intermediates are unchanged.")
    if gui_records:
        notes.append(f"{len(gui_records)} library cop(ies) MS-DIAL's GUI wrote went to the library-copy store as its "
                     "own, not the run's (" + ", ".join(f"{item.get('outcome')} as object {item.get('object_id')}"
                                                        for item in gui_records[:3]) + ").")
    if saved_aside:
        notes.append(f"{len(saved_aside)} project file(s) MS-DIAL saved are kept under {saved_aside[0].rsplit('/', 1)[0]}.")
    if plan["new_files"]:
        notes.append(f"{len(plan['new_files'])} file(s) appeared that the restore did not make (the first is "
                     f"{plan['new_files'][0]}); they were left where they are for a person to look at.")
    if undone:
        result["message"] = (
            f"Restore {restore} is taken back: {removed} file(s) and {directories} director(y/ies) removed, "
            f"{restored_files} project file(s) put back from their backups; the hold on sharing and raw deletion "
            "is lifted." + (" " + " ".join(notes) if notes else "")
        )
    elif complete:
        result["message"] = (
            f"Everything restore {restore} made is taken back, but the unit manifest could not be written "
            f"({manifest_error}): the hold on sharing and raw deletion still stands. Call again to lift it."
            + (" " + " ".join(notes) if notes else "")
        )
        result["manifest_error"] = manifest_error
    else:
        reasons = []
        if busy:
            reasons.append(f"{len(busy)} file(s) are still open in another program ({busy[0]})")
        if shared_directories:
            others = ", ".join(f"restore {item['restore_id']} of {item['manifest']}" for item in plan["others"][:3])
            reasons.append(f"{len(shared_directories)} director(y/ies) it made (the first is {shared_directories[0]}) "
                           f"hold only the copies of another restore standing on the same raw tree ({others}); undo "
                           "that restore first - its files are not this restore's, and nobody removes them by hand")
        if kept_directories:
            reasons.append(f"{len(kept_directories)} director(y/ies) the restore made hold files it did not make (the "
                           f"first is {kept_directories[0]}); a person looks at them and moves or removes them")
        reasons.extend(problems)
        result["message"] = (
            f"Restore {restore} is partly taken back and still held: " + "; ".join(reasons)
            + ". Close MS-DIAL, settle what is named, and call again." + (" " + " ".join(notes) if notes else "")
        )
        if manifest_error:
            result["manifest_error"] = manifest_error
    return result
