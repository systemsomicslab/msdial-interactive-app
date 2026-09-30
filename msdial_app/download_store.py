"""Accession-scoped download store: fetch each repository object once, link it into every unit.

WHY THIS EXISTS. Every analysis unit used to download its own copy of every object it lists, into
its own raw tree (repository_reanalysis.create_download_lease de-duplicates URLs only inside one
unit). In the declared pool 489 units touch a URL that another unit also lists, in 214 sharing
groups, so fetching per unit moves 14.37 TB where fetching each URL once moves 8.22 TB: 6.15 TB,
about 71 days at the observed ~1 MB/s. ST001408.zip alone is 928 GB used by three units. No sharing
group crosses an accession, so the store is scoped to one accession and lives beside its units:

    <workspace_root>/<repository>/<accession>/_dl/
        index/<sha256(url)[:24]>.json      URL -> current object id, validators, history
        partial/<url key>[.part|.json]      the resumable transfer and who started it
        o/<sha256[:16]>/obj/<name>          the bytes, under their original name
        o/<id>/t/                           the verified extraction tree, when there is one
        o/<id>/entry.json                   the object record (a tombstone after collection)
        o/<id>/members.tsv                  path, size, crc, mtime_ns of every file units link to
        claims/<url key>/<unit>.json        one file per consumer, so consumers never share a write
        locks/<name>.lock                   O_EXCL locks with a heartbeat

Objects are identified by the sha256 of their bytes and found by URL. A URL whose bytes change
upstream gets a new object, and the provenance of earlier consumers keeps naming the bytes they used.

WHY UNITS GET LINKS AND NOT THE STORE. MS-DIAL writes its .dcl, .pai2 and _tags.xml beside the files
it reads, so a unit must never read the store directly. Each unit gets real directories of its own
with one NTFS hardlink per file (same volume, no admin rights, no extra data bytes), falling back to
a copy that is recorded as such. What MS-DIAL writes beside a link stays in the unit's tree.

WHY DELETION IS CAREFUL. A hardlink is a second name for one file record, and NTFS keeps the
attributes on the record, not on the name. Clearing a read-only attribute in order to delete a
unit's link, as shutil.rmtree's usual onerror handler does, clears it on the store object and on
every other unit's link too. unlink_tree therefore never changes the attributes of a file with
st_nlink > 1: it removes the name with the Windows disposition flag that ignores the attribute, or
leaves the file and says so.

WHY LIVENESS IS NOT os.kill. On Windows os.kill(pid, 0) is not a probe: signal 0 is CTRL_C_EVENT,
and os.kill with any value other than the two console events terminates the process. The backend
is embedded in the MCP process and restarts leave stale locks behind, so a lock is judged stale by
its heartbeat and its holder's liveness, read through OpenProcess (or psutil), never by signalling.
process_is_alive is public so that the campaign runner's lock uses the same helper.

WHAT THIS MODULE DOES NOT DO. It does not read archives (archives.py does, through the extract
callback), decide which URLs a unit needs, prune a unit's tree to its selected inputs, or decide
retention. Deletion of store objects happens only in gc, and only under a campaign authorization
(campaign_authorization.py, read exactly as every other entry point reads it) that is not revoked,
states delete_after_validated_output, and covers boundary 5 for every unit whose release freed the
object.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import stat
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterable, Iterator, Mapping

from .campaign_authorization import CampaignAuthorization, CampaignAuthorizationError, load_campaign_authorization


STORE_DIRECTORY = "_dl"
INDEX_SCHEMA = "msdial-download-store-index.v1"
OBJECT_SCHEMA = "msdial-download-store-object.v1"
CLAIM_SCHEMA = "msdial-download-store-claim.v1"
LOCK_SCHEMA = "msdial-download-store-lock.v1"
# The contract's confirmation boundary for deleting raw data, which GC is.
GC_BOUNDARY = 5
# Full fetches that must return the same bytes before a declared MD5 they disagree with is taken as
# wrong. One is not enough: a damaged transfer would then fail the unit for good.
REFUTING_FETCHES = 2

LIVE_CLAIM_STATES = frozenset({"pending", "materialized"})
RELEASE_REASONS = frozenset({"raw_cleaned", "discarded", "excluded", "failed_terminal", "superseded"})

LOCK_STALE_SECONDS = 600.0
LOCK_HEARTBEAT_SECONDS = 30.0
LOCK_POLL_SECONDS = 2.0
# Breaking a stale lock takes milliseconds, so a break marker this old was left by a crashed breaker.
BREAK_MARKER_STALE_SECONDS = 60.0
# LongPathsEnabled is 0 on the campaign host and the .NET Framework Console reads MAX_PATH paths.
MAX_WINDOWS_PATH = 259
# CreateDirectoryW keeps room for an 8.3 name below MAX_PATH (248 with the terminator), and .NET
# Framework refuses a directory name of 248 characters or more, so a directory is held to 247.
MAX_WINDOWS_DIRECTORY_PATH = 247
MEMBERS_HEADER = ("path", "size", "crc", "mtime_ns")
_REPORTED_ITEMS = 50
# What a collection writes into an entry, and what installing the object again clears.
_COLLECTION_FIELDS = (
    "collected_at", "collected_under", "collected_bytes", "release_record", "collection_kept",
)

_HEX_MD5 = re.compile(r"[0-9a-fA-F]{32}")
_SAFE_UNIT_FILE = re.compile(r"[a-z0-9][a-z0-9._-]{0,99}")
_RESERVED_NAMES = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{index}" for index in range(1, 10)}
    | {f"lpt{index}" for index in range(1, 10)}
)
_UNSAFE_NAME_CHARACTERS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class StoreError(ValueError):
    """A store operation was refused; nothing the store holds was changed by the refusal."""


class StoreLockTimeout(StoreError):
    """The lock was held by a live holder for longer than the caller was willing to wait."""


class StoreLockLost(StoreError):
    """Another process removed or replaced a lock this process believed it held."""


class DeclaredChecksumMismatch(StoreError):
    """The bytes a unit would use do not match the checksum the unit declared for them."""


class MaterializationCollision(StoreError):
    """Two sources, or a source and an existing file, want the same path in a unit's tree."""

    def __init__(self, collisions: list[dict[str, Any]]) -> None:
        self.collisions = collisions
        shown = "; ".join(f"{item['kind']}: {item['path']}" for item in collisions[:5])
        more = f" (and {len(collisions) - 5} more)" if len(collisions) > 5 else ""
        super().__init__(f"Unit tree materialization refused, nothing was written: {shown}{more}")


# --------------------------------------------------------------------------------------------------
# Process liveness
# --------------------------------------------------------------------------------------------------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SYNCHRONIZE = 0x00100000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_WAIT_OBJECT_0 = 0x0
_WAIT_TIMEOUT = 0x102
_STILL_ACTIVE = 259
_FILETIME_UNIX_EPOCH_SECONDS = 11_644_473_600


def process_is_alive(pid: Any, created_at: float | None = None) -> bool | None:
    """Return True, False, or None when liveness cannot be read. Never signals the process.

    `created_at` is the holder's creation time as recorded by process_created_at. Windows reuses
    PIDs, so a live process with a different creation time is a different process, and the holder
    is dead. None means "cannot tell", and a caller must treat it as possibly alive.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    alive, created = _probe_process(pid)
    if alive and created_at is not None and created is not None:
        try:
            if abs(float(created) - float(created_at)) > 1.0:
                return False
        except (TypeError, ValueError):
            return alive
    return alive


def process_created_at(pid: int | None = None) -> float | None:
    """Creation time of a process (default: this one) in Unix seconds, or None if unreadable."""
    alive, created = _probe_process(os.getpid() if pid is None else int(pid))
    return created if alive else None


def _probe_process(pid: int) -> tuple[bool | None, float | None]:
    if os.name == "nt":
        alive, created = _windows_process_probe(pid)
        if alive is not None:
            return alive, created
        return _psutil_process_probe(pid)
    alive, created = _psutil_process_probe(pid)
    if alive is not None:
        return alive, created
    return _proc_process_probe(pid)


def _windows_process_probe(pid: int) -> tuple[bool | None, float | None]:
    try:
        import ctypes
        from ctypes import wintypes
    except ImportError:  # pragma: no cover - ctypes is part of every CPython on Windows
        return None, None
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetProcessTimes.restype = wintypes.BOOL
    kernel32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(wintypes.FILETIME),) * 4
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    # A waited-for child keeps its process object alive while any handle to it is open, so
    # OpenProcess alone succeeds for a process that has already exited. The wait (or, without
    # SYNCHRONIZE, the exit code) is what says whether it is still running.
    synchronize = True
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION | _SYNCHRONIZE, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == _ERROR_INVALID_PARAMETER:
            return False, None
        if error != _ERROR_ACCESS_DENIED:
            return None, None
        synchronize = False
        handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            # Access denied for even the least right: the process exists but is not ours to read.
            return (True if ctypes.get_last_error() == _ERROR_ACCESS_DENIED else None), None
    try:
        if synchronize:
            state = kernel32.WaitForSingleObject(handle, 0)
            if state == _WAIT_OBJECT_0:
                return False, None
            if state != _WAIT_TIMEOUT:
                return None, None
        else:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None, None
            if code.value != _STILL_ACTIVE:
                return False, None
        times = [wintypes.FILETIME() for _ in range(4)]
        created = None
        if kernel32.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            created = ticks / 10_000_000 - _FILETIME_UNIX_EPOCH_SECONDS
        return True, created
    finally:
        kernel32.CloseHandle(handle)


def _psutil_process_probe(pid: int) -> tuple[bool | None, float | None]:
    try:
        import psutil
    except ImportError:
        return None, None
    try:
        process = psutil.Process(pid)
        if process.status() == psutil.STATUS_ZOMBIE:
            return False, None
        return True, process.create_time()
    except psutil.NoSuchProcess:  # includes ZombieProcess
        return False, None
    except psutil.AccessDenied:
        return True, None
    except psutil.Error:
        return None, None


def _proc_process_probe(pid: int) -> tuple[bool | None, float | None]:
    if not Path("/proc/self").exists():
        return None, None
    return Path(f"/proc/{pid}").exists(), None


# --------------------------------------------------------------------------------------------------
# Locks
# --------------------------------------------------------------------------------------------------


class StoreLock:
    """An O_CREAT|O_EXCL lock file holding pid, host, job and a token, kept fresh by a heartbeat.

    The lock is stale only when its heartbeat (the file's mtime) is older than `stale_seconds` AND
    its holder is known to be dead, or when its content is unreadable and the heartbeat has lapsed.
    A holder on another host, or one whose liveness cannot be read, is never presumed dead: a
    waiter waits rather than fetch the same object twice.
    """

    def __init__(
        self,
        path: Path,
        *,
        job_id: str = "",
        stale_seconds: float = LOCK_STALE_SECONDS,
        heartbeat_seconds: float = LOCK_HEARTBEAT_SECONDS,
        poll_seconds: float = LOCK_POLL_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.job_id = str(job_id or "")
        self.stale_seconds = float(stale_seconds)
        self.heartbeat_seconds = float(heartbeat_seconds)
        self.poll_seconds = float(poll_seconds)
        self.token = ""
        self.recovered: list[dict[str, Any]] = []
        self.waited = False
        self.first_holder: dict[str, Any] | None = None
        self._held = False
        self._lost = False
        self._stop = threading.Event()
        self._heartbeat: threading.Thread | None = None

    @property
    def lost(self) -> bool:
        return self._lost

    def acquire(
        self,
        timeout: float | None = None,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
    ) -> "StoreLock":
        if self._held:
            raise StoreError(f"Lock {self.path.name} is already held by this handle.")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        vanished = 0
        while True:
            token = secrets.token_hex(16)
            try:
                descriptor = os.open(
                    self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0)
                )
            except (FileExistsError, PermissionError) as error:
                # PermissionError: on Windows a lock file whose deletion is pending cannot be
                # created again until the last handle closes. Both mean "held for now".
                holder = inspect_lock(self.path, self.stale_seconds)
                if holder is None:
                    # Released between the two calls. A PermissionError that keeps coming with no
                    # lock file present is a directory that cannot be written, not contention.
                    vanished += 1
                    if vanished > 500:
                        raise StoreError(f"Cannot create lock {self.path.name}: {error}") from error
                    time.sleep(0.01)
                    continue
                vanished = 0
                if holder["stale"] and self._break_stale(holder):
                    self.recovered.append(public_lock_holder(holder))
                    continue
                if not self.waited:
                    self.waited = True
                    self.first_holder = public_lock_holder(holder)
                    if on_wait is not None:
                        on_wait(self.first_holder)
                if deadline is not None and time.monotonic() >= deadline:
                    raise StoreLockTimeout(
                        f"Lock {self.path.name} is held by pid {holder.get('pid')} "
                        f"({holder.get('reason')}); gave up after {timeout} s."
                    )
                pause = self.poll_seconds
                if deadline is not None:
                    pause = max(0.0, min(pause, deadline - time.monotonic()))
                time.sleep(pause)
                continue
            record = {
                "schema": LOCK_SCHEMA,
                "lock": self.path.name,
                "token": token,
                "pid": os.getpid(),
                "process_created_at": process_created_at(),
                "host": socket.gethostname(),
                "job_id": self.job_id,
                "acquired_at": _now(),
            }
            try:
                os.write(descriptor, (json.dumps(record) + "\n").encode("utf-8"))
            finally:
                os.close(descriptor)
            self.token = token
            self._held = True
            self._lost = False
            self._stop.clear()
            self._heartbeat = threading.Thread(
                target=self._beat, name=f"store-lock-{self.path.name}", daemon=True
            )
            self._heartbeat.start()
            return self

    def ensure_held(self) -> None:
        """Raise if the lock was broken or replaced while this process held it."""
        if not self._held:
            raise StoreLockLost(f"Lock {self.path.name} is not held.")
        if self._lost or self._ownership() != "ours":
            self._lost = True
            raise StoreLockLost(f"Lock {self.path.name} was taken over by another holder.")

    def release(self) -> None:
        if not self._held:
            return
        self._stop.set()
        if self._heartbeat is not None:
            self._heartbeat.join()
        self._held = False
        ownership = self._ownership(attempts=40)
        if ownership == "missing":
            self._lost = True
            return
        if ownership != "ours":
            # Replaced by another holder (deleting it would release somebody else's lock), or
            # unreadable even after retries, in which case it is left to be judged stale.
            self._lost = True
            return
        for attempt in range(40):
            try:
                self.path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                # A waiter is reading the file at this instant; Windows refuses the delete.
                if attempt == 39:
                    raise
                time.sleep(0.05)

    def __enter__(self) -> "StoreLock":
        if not self._held:
            self.acquire()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.release()

    def _ownership(self, attempts: int = 5) -> str:
        """"ours", "theirs", "missing", or "unreadable" (a reader or writer had it at that instant)."""
        for attempt in range(attempts):
            try:
                text = self.path.read_text(encoding="utf-8")
            except FileNotFoundError:
                return "missing"
            except OSError:
                time.sleep(0.05)
                continue
            try:
                record = json.loads(text)
            except ValueError:
                return "theirs"  # ours was written whole before this handle was returned
            return "ours" if isinstance(record, dict) and record.get("token") == self.token else "theirs"
        return "unreadable"

    def _beat(self) -> None:
        while not self._stop.wait(self.heartbeat_seconds):
            ownership = self._ownership()
            if ownership in {"missing", "theirs"}:
                self._lost = True
                return
            if ownership == "unreadable":
                continue  # a transient sharing conflict is not a lost lock; beat again next time
            try:
                os.utime(self.path)
            except FileNotFoundError:
                self._lost = True
                return
            except OSError:
                continue

    def _break_stale(self, judged: dict[str, Any]) -> bool:
        """Remove a stale lock, but only the one that was judged: never a fresh one created since.

        All breakers go through one O_EXCL break marker, and the holder is judged again under it.
        Without the marker, two waiters that both judged the same stale lock could interleave so
        that the second deletes the fresh lock the first had just created.
        """
        marker = self.path.with_name(self.path.name + ".break")
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            with contextlib.suppress(OSError):
                if time.time() - marker.stat().st_mtime > BREAK_MARKER_STALE_SECONDS:
                    marker.unlink()
            return False
        try:
            os.close(descriptor)
            again = inspect_lock(self.path, self.stale_seconds)
            if again is None:
                return True
            if not again["stale"] or again.get("token") != judged.get("token"):
                return False
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
            except PermissionError:
                return False
            return True
        finally:
            with contextlib.suppress(OSError):
                marker.unlink()


def inspect_lock(path: Path, stale_seconds: float = LOCK_STALE_SECONDS) -> dict[str, Any] | None:
    """Describe a lock file and judge whether it is stale. None when there is no lock file."""
    path = Path(path)
    try:
        mtime = path.stat().st_mtime
    except FileNotFoundError:
        return None
    except OSError:
        mtime = time.time()
    record = _read_json_quiet(path)
    age = max(0.0, time.time() - mtime)
    info: dict[str, Any] = {
        "lock": path.name,
        "heartbeat_age_seconds": round(age, 3),
        "readable": record is not None,
    }
    if record:
        for field_name in ("token", "pid", "process_created_at", "host", "job_id", "acquired_at"):
            info[field_name] = record.get(field_name)
    if age < stale_seconds:
        info.update(stale=False, reason="heartbeat_recent")
    elif record is None:
        info.update(stale=True, reason="unreadable_and_heartbeat_lapsed")
    elif record.get("host") and record.get("host") != socket.gethostname():
        info.update(stale=False, reason="holder_on_another_host")
    else:
        alive = process_is_alive(record.get("pid"), record.get("process_created_at"))
        info["holder_alive"] = alive
        if alive is False:
            info.update(stale=True, reason="holder_dead_and_heartbeat_lapsed")
        elif alive is None:
            info.update(stale=False, reason="holder_liveness_unknown")
        else:
            info.update(stale=False, reason="holder_alive")
    return info


def public_lock_holder(info: Mapping[str, Any]) -> dict[str, Any]:
    """A lock description fit for job status and manifests: no token, and no host name.

    The host name is needed on disk to judge a holder's pid, but it identifies the machine and has
    no place in a record that may leave it.
    """
    view = {key: value for key, value in info.items() if key not in {"token", "host"}}
    if "host" in info:
        view["holder_on_this_host"] = not info.get("host") or info.get("host") == socket.gethostname()
    return view


# --------------------------------------------------------------------------------------------------
# Fetchers
# --------------------------------------------------------------------------------------------------


@dataclass
class ClientFetcher:
    """The store's fetcher interface over RepositoryHttpClient (download and content_length).

    A fetcher is any object with fetch(url, destination, progress_callback) that leaves the whole
    object at `destination` and returns at least size_bytes (sha256 and md5 are computed by the
    store when absent), and optionally head(url) returning a Content-Length (0 or None when the
    server does not answer) or a mapping with content_length, etag and last_modified.
    """

    client: Any
    maximum_bytes: int = 1 << 62

    def fetch(self, url: str, destination: Path, progress_callback: Any = None) -> dict[str, Any]:
        return self.client.download(
            url, destination, self.maximum_bytes, progress_callback=progress_callback
        )

    def head(self, url: str) -> int:
        return self.client.content_length(url)


# --------------------------------------------------------------------------------------------------
# The store
# --------------------------------------------------------------------------------------------------


class DownloadStore:
    def __init__(
        self,
        workspace_root: str | Path,
        repository: str,
        accession: str,
        *,
        lock_stale_seconds: float = LOCK_STALE_SECONDS,
        lock_heartbeat_seconds: float = LOCK_HEARTBEAT_SECONDS,
        lock_poll_seconds: float = LOCK_POLL_SECONDS,
    ) -> None:
        self.repository = _path_component(repository, "repository")
        self.accession = _path_component(accession, "accession")
        self.root = Path(workspace_root).resolve() / self.repository / self.accession / STORE_DIRECTORY
        self.lock_stale_seconds = lock_stale_seconds
        self.lock_heartbeat_seconds = lock_heartbeat_seconds
        self.lock_poll_seconds = lock_poll_seconds

    # ---- names -----------------------------------------------------------------------------------

    @staticmethod
    def url_key(url: str) -> str:
        return hashlib.sha256(str(url).encode("utf-8")).hexdigest()[:24]

    @staticmethod
    def object_id_for(sha256: str) -> str:
        value = str(sha256 or "").casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise StoreError(f"Not a sha256 digest: {sha256!r}")
        return value[:16]

    def index_path(self, url_key: str) -> Path:
        return self.root / "index" / f"{url_key}.json"

    def partial_path(self, url_key: str) -> Path:
        """The fetch destination; a resumable fetcher keeps its bytes at this path plus '.part'."""
        return self.root / "partial" / url_key

    def object_directory(self, object_id: str) -> Path:
        return self.root / "o" / object_id

    def entry_path(self, object_id: str) -> Path:
        return self.object_directory(object_id) / "entry.json"

    def members_path(self, object_id: str) -> Path:
        return self.object_directory(object_id) / "members.tsv"

    def claim_path(self, url_key: str, unit_id: str) -> Path:
        return self.root / "claims" / url_key / _unit_file_name(unit_id)

    def lock(self, name: str, *, job_id: str = "") -> StoreLock:
        return StoreLock(
            self.root / "locks" / f"{name}.lock",
            job_id=job_id,
            stale_seconds=self.lock_stale_seconds,
            heartbeat_seconds=self.lock_heartbeat_seconds,
            poll_seconds=self.lock_poll_seconds,
        )

    @contextlib.contextmanager
    def _locked(
        self,
        name: str,
        *,
        job_id: str = "",
        timeout: float | None = None,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
    ) -> Iterator[StoreLock]:
        handle = self.lock(name, job_id=job_id).acquire(timeout=timeout, on_wait=on_wait)
        try:
            yield handle
        finally:
            handle.release()

    def _claim_lock(self, key: str, *, job_id: str = "") -> contextlib.AbstractContextManager[StoreLock]:
        """The lock every write of a URL's claims takes, and that gc holds while it decides and collects.

        Not u-<key>: that one is held for a whole transfer, days for the largest archives, and a
        batch pre-claim must not wait for it. This one is held only for one small write, or for one
        collection. A claim written while gc works on the URL therefore lands either before gc reads
        the claims, and keeps the object, or after the object is collected, and fetches it again. It
        is always taken last, and gc never waits for a lock, so it cannot deadlock.
        """
        return self._locked(f"c-{key}", job_id=job_id)

    # ---- records ---------------------------------------------------------------------------------

    def lookup(self, url: str) -> dict[str, Any] | None:
        return _read_json(self.index_path(self.url_key(url)))

    def entry(self, object_id: str) -> dict[str, Any] | None:
        return _read_json(self.entry_path(object_id))

    def claim(self, url: str, unit_id: str, *, job_id: str = "", source: str = "lease") -> dict[str, Any]:
        """Record that `unit_id` needs `url`. Idempotent: a live claim is returned unchanged.

        A pre-claim made at batch approval, before any fetch, keeps the object alive for units
        that have not started yet; the lease makes the claim itself when no pre-claim exists. A
        released claim is reopened, with its earlier life kept in `history`.
        """
        unit_id = _unit_id(unit_id)
        key = self.url_key(url)
        path = self.claim_path(key, unit_id)
        with self._claim_lock(key, job_id=job_id):
            existing = _read_json(path)
            if existing and existing.get("state") in LIVE_CLAIM_STATES:
                return existing
            history = list((existing or {}).get("history") or [])
            if existing:
                history.append(
                    {
                        "state": existing.get("state"),
                        "object_id": existing.get("object_id"),
                        "release_reason": existing.get("release_reason"),
                        "released_at": existing.get("released_at"),
                    }
                )
            record = {
                "schema": CLAIM_SCHEMA,
                "url": url,
                "url_key": key,
                "unit_id": unit_id,
                "state": "pending",
                "object_id": None,
                "claimed_at": _now(),
                "claimed_by": {"source": str(source or "lease"), "job_id": str(job_id or "")},
                "history": history,
            }
            _write_json_atomic(path, record)
        return record

    def read_claim(self, url: str, unit_id: str) -> dict[str, Any] | None:
        return _read_json(self.claim_path(self.url_key(url), _unit_id(unit_id)))

    def claims_for_unit(self, unit_id: str) -> list[dict[str, Any]]:
        unit_id = _unit_id(unit_id)
        file_name = _unit_file_name(unit_id)
        claims = []
        for directory in _sorted_children(self.root / "claims"):
            if not directory.is_dir():
                continue
            record = _read_json(directory / file_name)
            if record and record.get("unit_id") == unit_id:
                claims.append(record)
        return claims

    def mark_materialized(
        self,
        url: str,
        unit_id: str,
        *,
        object_id: str,
        record: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        key = self.url_key(url)
        path = self.claim_path(key, _unit_id(unit_id))
        with self._claim_lock(key):
            claim = _read_json(path)
            if not claim or claim.get("state") not in LIVE_CLAIM_STATES:
                raise StoreError(f"Unit {unit_id} has no live claim on {url}; claim it before materializing.")
            claim.update(
                state="materialized",
                object_id=object_id,
                materialized_at=_now(),
                materialization=dict(record or {}),
            )
            claim.pop("last_error", None)
            _write_json_atomic(path, claim)
        return claim

    def release(
        self,
        url: str,
        unit_id: str,
        reason: str,
        *,
        authorization: Any = None,
    ) -> dict[str, Any]:
        """Release one claim. With an authorization, collect what the release left unclaimed."""
        if reason not in RELEASE_REASONS:
            raise StoreError(f"Unknown release reason {reason!r}; expected one of {sorted(RELEASE_REASONS)}.")
        key = self.url_key(url)
        released = self._release_claim(key, _unit_id(unit_id), reason)
        result: dict[str, Any] = {"released": [released] if released else [], "gc": None}
        if authorization is not None:
            objects = self._objects_of_claims([released] if released else [], [key])
            result["gc"] = self.gc(authorization, object_ids=objects)
        return result

    def release_unit(self, unit_id: str, reason: str, *, authorization: Any = None) -> dict[str, Any]:
        """Release every claim a unit holds (its raw tree is gone, or it will never use it)."""
        if reason not in RELEASE_REASONS:
            raise StoreError(f"Unknown release reason {reason!r}; expected one of {sorted(RELEASE_REASONS)}.")
        unit_id = _unit_id(unit_id)
        released = []
        keys = []
        for claim in self.claims_for_unit(unit_id):
            keys.append(claim["url_key"])
            record = self._release_claim(claim["url_key"], unit_id, reason)
            if record:
                released.append(record)
        result: dict[str, Any] = {"released": released, "gc": None}
        if authorization is not None:
            result["gc"] = self.gc(authorization, object_ids=self._objects_of_claims(released, keys))
        return result

    def _release_claim(self, key: str, unit_id: str, reason: str) -> dict[str, Any] | None:
        path = self.claim_path(key, unit_id)
        with self._claim_lock(key):
            claim = _read_json(path)
            if not claim:
                return None
            if claim.get("state") == "released":
                return claim
            claim.update(state="released", release_reason=reason, released_at=_now())
            _write_json_atomic(path, claim)
        return claim

    def _objects_of_claims(self, claims: Iterable[dict[str, Any]], keys: Iterable[str]) -> list[str]:
        objects = {claim.get("object_id") for claim in claims if claim.get("object_id")}
        for key in keys:
            index = _read_json(self.index_path(key))
            if index and index.get("object_id"):
                objects.add(index["object_id"])
        return sorted(objects)

    def live_claims(self, object_id: str) -> list[dict[str, Any]]:
        """Claims that keep an object: live, and naming it (or, while pending, its URL's current object)."""
        entry = self.entry(object_id) or {}
        keys = sorted({item.get("url_key") for item in entry.get("urls") or [] if item.get("url_key")})
        live = []
        for key in keys:
            index = _read_json(self.index_path(key)) or {}
            for path in _claim_files(self.root / "claims" / key):
                claim = _read_json(path)
                if not claim or claim.get("state") not in LIVE_CLAIM_STATES:
                    continue
                named = claim.get("object_id")
                if named == object_id or (not named and index.get("object_id") == object_id):
                    live.append(claim)
        return live

    # ---- fetch or reuse --------------------------------------------------------------------------

    def fetch_or_reuse(
        self,
        url: str,
        name: str,
        *,
        unit_id: str,
        fetcher: Any,
        declared_md5: str = "",
        job_id: str = "",
        extract: Callable[[Path, Path], Mapping[str, Any] | None] | None = None,
        progress_callback: Any = None,
        on_wait: Callable[[dict[str, Any]], None] | None = None,
        lock_timeout: float | None = None,
        force_refetch: bool = False,
    ) -> dict[str, Any]:
        """Give `unit_id` verified bytes for `url`, transferring them only when no usable copy exists.

        Reuse requires, in order: the unit's declared MD5 (when it is a hex MD5) equals the cached
        one; a HEAD Content-Length, when the server answers one, equals the cached size; and every
        file listed in members.tsv still has its recorded size and mtime_ns. A declared MD5 that
        differs sends a fresh fetch, and the unit fails with "MD5 checksum mismatch" only if the
        fresh bytes do not match it either. A changed size, or a modified object file, is fetched
        again; a modified extraction member is re-extracted from the kept archive when `extract`
        is given. A waiter on another consumer's fetch is told once through `on_wait`, with the
        status waiting_for_shared_download, and then reuses what that consumer fetched.

        Fetched bytes are compared with the declaration before `extract` sees them, as the per-unit
        lease did, so a damaged archive fails on its checksum and never reaches 7-Zip. A declaration
        is taken as wrong, and no longer fetched for, only once REFUTING_FETCHES full fetches have
        returned the same other bytes; until then a retry fetches again, because one bad transfer
        must not fail a unit for good. A consumer without a declaration does not reuse bytes that
        another unit's declaration disputes and that only one fetch has produced. A HEAD size that
        a refetch showed to disagree with unchanged bytes is remembered and does not force another
        refetch while the server keeps answering it. `force_refetch` fetches whatever the cache and
        its memos say, for a retry policy that has reason to distrust them.

        The caller decides which checksum applies: MB-POST publishes per-member MD5s, not one for
        the tar, so its lease passes no declared_md5, exactly as the per-unit download did.
        """
        if not url:
            raise StoreError("A store object needs a URL.")
        unit_id = _unit_id(unit_id)
        key = self.url_key(url)
        object_name = _safe_object_name(name)
        declared = declared_md5.strip().casefold() if _HEX_MD5.fullmatch(str(declared_md5 or "").strip()) else ""
        self.claim(url, unit_id, job_id=job_id)
        reuse_check: dict[str, Any] = {
            "declared_md5": "not_declared" if not declared else "not_checked",
            "remote_size": "not_checked",
            "members": "not_checked",
        }
        action = ""
        fetched: dict[str, Any] | None = None
        reason = ""

        def waiting(holder: dict[str, Any]) -> None:
            if on_wait is not None:
                on_wait({"status": "waiting_for_shared_download", "url": url, "url_key": key, **holder})

        with self._locked(f"u-{key}", job_id=job_id, timeout=lock_timeout, on_wait=waiting) as url_lock:
            index = _read_json(self.index_path(key)) or {}
            entry: dict[str, Any] | None = None
            prior_id = index.get("object_id")
            verified: bool | None = None
            if not prior_id:
                reuse_check["cache"] = "miss"
            elif force_refetch:
                reuse_check["cache"] = "refetch_forced"
                reason = "force_refetch"
            else:
                with self._locked(f"o-{prior_id}", job_id=job_id, timeout=lock_timeout):
                    entry = self._ready_entry(prior_id)
                    if entry is None:
                        reuse_check["cache"] = "object_not_ready"
                    else:
                        reuse_check["cache"] = "hit"
                        reason = self._reuse_verdict(entry, index, declared, fetcher, url, reuse_check)
                        if reason == "declared_md5_refuted":
                            self._fail_claim(key, unit_id, f"MD5 checksum mismatch for {object_name}.")
                            raise DeclaredChecksumMismatch(
                                f"MD5 checksum mismatch for {object_name}. {REFUTING_FETCHES} full fetches "
                                "already returned the same other bytes; it was not downloaded again "
                                "(force_refetch fetches it anyway)."
                            )
                        if reason == "tree_tainted" and extract is not None:
                            entry = self._extract_locked(entry, extract, unit_id, job_id, reason)
                            action = "re-extracted"
                        elif reason in {"", "tree_tainted"}:
                            # A consumer without `extract` takes the object, not the tree, so a
                            # modified tree member does not stop it; the taint stays on record.
                            if extract is not None and not entry.get("tree"):
                                entry = self._extract_locked(entry, extract, unit_id, job_id, reason)
                            action = "reused"
                        else:
                            entry = None
            if not action:
                try:
                    fetched = self._fetch(url, key, fetcher, unit_id, job_id, progress_callback)
                except Exception as error:
                    # The partial bytes stay for the next attempt, and so does the claim.
                    self._fail_claim(key, unit_id, f"{type(error).__name__}: {error}")
                    raise
                url_lock.ensure_held()
                entry = self._install(
                    key, url, object_name, fetched, unit_id=unit_id, job_id=job_id,
                    replace_object=reason == "object_tainted",
                )
                # Pointed before anything else, so that an extraction failure is retried from the
                # kept bytes rather than by downloading them again, and so that the count of fetches
                # a declaration disagrees with is on record before the unit fails.
                pointed = self._point_index(
                    key, url, object_name, entry, fetched, reason or "fetched",
                    prior_id=prior_id,
                    declared=declared,
                    head_size=(reuse_check.get("remote") or {}).get("content_length"),
                )
                action = "refetched" if prior_id else "fetched"
                if declared:
                    verified = entry["md5"] == declared
                    reuse_check["fetched_declared_md5"] = "match" if verified else "mismatch"
                if verified is False:
                    # Before extraction: unverified bytes never reach an extractor, and a damaged
                    # archive fails on its checksum rather than on whatever the extractor raises.
                    memo = (pointed.get("refuted_declared_md5") or {}).get(declared) or {}
                    self._fail_claim(key, unit_id, f"MD5 checksum mismatch for {object_name}.")
                    if memo.get("refuted"):
                        detail = (
                            f"{memo.get('fetches')} full fetches returned these same bytes, so later "
                            "attempts fail without downloading unless the remote size changes or "
                            "force_refetch is passed."
                        )
                    else:
                        detail = "The next attempt fetches it once more before the declaration is taken as wrong."
                    raise DeclaredChecksumMismatch(f"MD5 checksum mismatch for {object_name}. {detail}")
                if extract is not None:
                    with self._locked(f"o-{entry['object_id']}", job_id=job_id, timeout=lock_timeout):
                        if not entry.get("tree") or not self.verify_members(entry["object_id"])["tree_intact"]:
                            entry = self._extract_locked(entry, extract, unit_id, job_id, reason)
            assert entry is not None
            if fetched is None and declared:
                # _reuse_verdict reuses only a matching object; this states it in the result.
                verified = entry["md5"] == declared
                if not verified:
                    self._fail_claim(key, unit_id, f"MD5 checksum mismatch for {object_name}.")
                    raise DeclaredChecksumMismatch(f"MD5 checksum mismatch for {object_name}.")
            claim = self._record_fetch(key, unit_id, entry, action, job_id)

        object_path = self.object_directory(entry["object_id"]) / "obj" / entry["name"]
        tree = entry.get("tree")
        transferred = 0
        resumed = 0
        if fetched is not None:
            resumed = int(fetched.get("resumed_from_bytes") or 0)
            transferred = max(0, int(fetched["size_bytes"]) - resumed)
        return {
            "url": url,
            "url_key": key,
            "object_id": entry["object_id"],
            "object_directory": str(self.object_directory(entry["object_id"])),
            "object_path": str(object_path),
            "tree_path": str(self.object_directory(entry["object_id"]) / "t") if tree else None,
            "name": entry["name"],
            "size_bytes": entry["size_bytes"],
            "sha256": entry["sha256"],
            "md5": entry["md5"],
            "cache_hit": fetched is None,
            "action": action,
            "reuse_reason": reason or None,
            "sha256_origin": "fetched_by_this_unit" if fetched is not None else "inherited_from_cache",
            "fetched_by": entry.get("fetched_by"),
            "transferred_bytes": transferred,
            "resumed_from_bytes": resumed,
            "declared_checksum": str(declared_md5 or ""),
            "declared_checksum_algorithm": "md5" if declared else None,
            "declared_checksum_verified": verified,
            "reuse_check": reuse_check,
            "waited_for_lock": url_lock.waited,
            "lock_holder": url_lock.first_holder,
            "recovered_stale_locks": url_lock.recovered,
            "claim_path": str(self.claim_path(key, unit_id)),
            "claim_state": claim.get("state"),
        }

    def _ready_entry(self, object_id: str) -> dict[str, Any] | None:
        entry = self.entry(object_id)
        if not entry or entry.get("state") != "ready":
            return None
        if not (self.object_directory(object_id) / "obj" / str(entry.get("name") or "")).is_file():
            return None
        return entry

    def _reuse_verdict(
        self,
        entry: dict[str, Any],
        index: dict[str, Any],
        declared: str,
        fetcher: Any,
        url: str,
        reuse_check: dict[str, Any],
    ) -> str:
        """Return "" when the cached object may be reused, else the reason it may not.

        A declaration that REFUTING_FETCHES full fetches already contradicted with these same bytes
        fails the unit without another transfer, unless the server now reports a different size.
        One contradicting fetch is not enough, and a consumer with no declaration of its own does
        not reuse bytes so disputed either: it fetches them again, which settles the dispute.
        """
        memos = index.get("refuted_declared_md5") or {}
        refuted = False
        if declared:
            if entry["md5"] == declared:
                reuse_check["declared_md5"] = "match"
            else:
                memo = memos.get(declared) or {}
                if memo.get("object_id") != entry["object_id"] or not memo.get("refuted"):
                    reuse_check["declared_md5"] = "differs_from_cache"
                    return "declared_md5_differs"
                reuse_check["declared_md5"] = "refuted_by_earlier_fetches"
                refuted = True
        else:
            disputes = [
                memo for memo in memos.values()
                if memo.get("object_id") == entry["object_id"] and not memo.get("refuted")
            ]
            if disputes:
                reuse_check["disputed_by_declarations"] = len(disputes)
                return "declared_md5_disputed"
        head = getattr(fetcher, "head", None)
        if callable(head):
            answer = _head_answer(head, url)
            reuse_check["remote"] = answer
            size = answer.get("content_length")
            misleading = index.get("head_size_mismatch") or {}
            if not size:
                reuse_check["remote_size"] = "not_answered"
            elif int(size) == int(entry["size_bytes"]):
                reuse_check["remote_size"] = "match"
            elif (
                misleading.get("object_id") == entry["object_id"]
                and int(misleading.get("head_content_length") or 0) == int(size)
            ):
                # A refetch already showed that this server answers this size for these bytes.
                reuse_check["remote_size"] = "differs_as_before"
            else:
                reuse_check["remote_size"] = "changed"
                return "remote_size_changed"
        if refuted:
            return "declared_md5_refuted"
        check = self.verify_members(entry["object_id"])
        reuse_check["members"] = "intact" if check["intact"] else "tainted"
        if check["intact"]:
            return ""
        reuse_check["member_mismatches"] = check["mismatches"]
        self._note_taint(entry, check)
        return "object_tainted" if not check["object_intact"] else "tree_tainted"

    def _fetch(
        self,
        url: str,
        key: str,
        fetcher: Any,
        unit_id: str,
        job_id: str,
        progress_callback: Any,
    ) -> dict[str, Any]:
        destination = self.partial_path(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        record_path = destination.with_name(f"{key}.json")
        record = _read_json(record_path) or {"url": url, "url_key": key, "attempts": []}
        record["attempts"] = list(record.get("attempts") or []) + [
            {"unit_id": unit_id, "job_id": job_id, "started_at": _now()}
        ]
        _write_json_atomic(record_path, record)
        result = dict(fetcher.fetch(url, destination, progress_callback) or {})
        if not destination.is_file():
            raise StoreError(f"The fetcher returned without leaving the object at {destination.name}.")
        size = destination.stat().st_size
        if "size_bytes" in result and int(result["size_bytes"]) != size:
            raise StoreError(
                f"The fetcher reported {result['size_bytes']} bytes but {size} bytes are on disk."
            )
        result["size_bytes"] = size
        if not result.get("sha256") or not result.get("md5"):
            result["sha256"], result["md5"] = _hash_file(destination)
        result["sha256"] = str(result["sha256"]).casefold()
        result["md5"] = str(result["md5"]).casefold()
        return result

    def _install(
        self,
        key: str,
        url: str,
        name: str,
        fetched: dict[str, Any],
        *,
        unit_id: str,
        job_id: str,
        replace_object: bool,
    ) -> dict[str, Any]:
        object_id = self.object_id_for(fetched["sha256"])
        partial = self.partial_path(key)
        directory = self.object_directory(object_id)
        with self._locked(f"o-{object_id}", job_id=job_id):
            existing = self.entry(object_id)
            if existing and existing.get("sha256") != fetched["sha256"]:
                raise StoreError(f"Object id {object_id} is already held by different bytes.")
            reusable = (
                existing is not None
                and existing.get("state") == "ready"
                and not replace_object
                and self.verify_members(object_id)["object_intact"]
            )
            if reusable:
                # The same bytes are already here (a refetch that proved nothing changed, or a
                # second URL serving identical content). Keep the file units already link to.
                partial.unlink(missing_ok=True)
                entry = existing
            else:
                obj = directory / "obj"
                if obj.exists():
                    # A modified or half-installed file: drop the store's name for it. Units that
                    # link to it keep theirs, and the taint is on record in the entry.
                    removal = unlink_tree(obj)
                    if not removal["complete"]:
                        raise StoreError(f"Could not replace the object files of {object_id}: {removal['kept'][:3]}")
                obj.mkdir(parents=True, exist_ok=True)
                target = obj / str((existing or {}).get("name") or name)
                _replace(partial, target)
                now = _now()
                fetcher_record = {"unit_id": unit_id, "job_id": job_id, "at": now}
                entry = dict(existing or {})
                if existing and existing.get("state") == "ready":
                    # A tainted object replaced by fresh bytes: the first fetch stays on record.
                    entry["refetched_by"] = fetcher_record
                else:
                    if existing:
                        # A tombstone installed again: its collection history is kept.
                        entry.setdefault("collections", []).append(
                            {
                                key_name: existing.get(key_name)
                                for key_name in ("state", "collected_at", "collected_under", "release_record")
                            }
                        )
                        for stale_field in _COLLECTION_FIELDS + ("tree", "extraction", "refetched_by"):
                            entry.pop(stale_field, None)
                    entry["fetched_by"] = fetcher_record
                entry.update(
                    schema=OBJECT_SCHEMA,
                    object_id=object_id,
                    state="ready",
                    name=target.name,
                    size_bytes=int(fetched["size_bytes"]),
                    sha256=fetched["sha256"],
                    md5=fetched["md5"],
                    installed_at=now,
                )
                tree_rows = [row for row in _read_members(self.members_path(object_id)) if row["path"].startswith("t/")]
                if not entry.get("tree") or not (directory / "t").is_dir():
                    tree_rows = []
                    entry.pop("tree", None)
                self._write_members(object_id, [_member_row(directory, target)] + tree_rows)
            urls = [item for item in entry.get("urls") or [] if item.get("url_key") != key]
            urls.append({"url": url, "url_key": key, "name": name})
            entry["urls"] = urls
            _write_json_atomic(self.entry_path(object_id), entry)
        partial.with_name(f"{key}.json").unlink(missing_ok=True)
        return entry

    def _extract_locked(
        self,
        entry: dict[str, Any],
        extract: Callable[[Path, Path], Mapping[str, Any] | None],
        unit_id: str,
        job_id: str,
        reason: str,
    ) -> dict[str, Any]:
        """Extract the object into t/ once, through a staging directory. The object lock is held."""
        object_id = entry["object_id"]
        directory = self.object_directory(object_id)
        tree = directory / "t"
        staging = directory / "t.partial"
        if staging.exists():
            # An interrupted earlier extraction; nothing ever linked to it.
            unlink_tree(staging)
        staging.mkdir(parents=True)
        obj = directory / "obj" / entry["name"]
        try:
            record = dict(extract(obj, staging) or {})
            _refuse_reparse_points(staging)
        except Exception as error:
            entry.setdefault("extraction_failures", []).append(
                {"at": _now(), "unit_id": unit_id, "job_id": job_id, "error": f"{type(error).__name__}: {error}"}
            )
            _write_json_atomic(self.entry_path(object_id), entry)
            unlink_tree(staging)
            raise
        if tree.exists():
            # Re-extraction after a taint: only the store's names go; linked units keep theirs.
            removal = unlink_tree(tree)
            if not removal["complete"]:
                unlink_tree(staging)
                raise StoreError(f"Could not replace the extraction tree of {object_id}: {removal['kept'][:3]}")
        _replace(staging, tree)
        crc_by_path = {
            str(item.get("path")).replace("\\", "/"): item.get("crc")
            for item in record.pop("members", None) or []
            if isinstance(item, Mapping) and item.get("path")
        }
        rows = [_member_row(directory, obj)]
        files = 0
        size = 0
        for path in _walk_files(tree):
            row = _member_row(directory, path)
            row["crc"] = crc_by_path.get(PurePosixPath(*path.relative_to(tree).parts).as_posix(), "") or ""
            rows.append(row)
            files += 1
            size += int(row["size"])
        self._write_members(object_id, rows)
        now = _now()
        entry["tree"] = {
            "path": "t",
            "files": files,
            "bytes": size,
            "extracted_at": now,
            "extracted_by": {"unit_id": unit_id, "job_id": job_id},
        }
        if reason == "tree_tainted":
            entry["tree"]["re_extracted_after_taint"] = True
        entry["extraction"] = record
        _write_json_atomic(self.entry_path(object_id), entry)
        return entry

    def _point_index(
        self,
        key: str,
        url: str,
        name: str,
        entry: dict[str, Any],
        fetched: Mapping[str, Any],
        reason: str,
        *,
        prior_id: str | None = None,
        declared: str = "",
        head_size: int | None = None,
    ) -> dict[str, Any]:
        """Point the URL at the object one full fetch produced, and update what that fetch proves.

        `prior_id` is the object the URL pointed at before the fetch. When the fetch reproduced it,
        two full fetches agree on these bytes: that counts against any declaration they contradict,
        and, for a refetch that a HEAD size sent, shows that the HEAD size is not the body's.
        """
        path = self.index_path(key)
        index = _read_json(path) or {"schema": INDEX_SCHEMA, "url": url, "url_key": key, "history": []}
        now = _now()
        previous = index.get("object_id")
        if previous and previous != entry["object_id"]:
            index.setdefault("history", []).append(
                {"object_id": previous, "replaced_at": now, "reason": reason}
            )
        index.update(
            object_id=entry["object_id"],
            name=name,
            updated_at=now,
            validators={
                "etag": fetched.get("etag"),
                "last_modified": fetched.get("last_modified"),
                "content_length": fetched.get("content_length") or fetched.get("size_bytes"),
            },
        )
        _count_contradicting_fetches(index, entry, prior_id, declared, now)
        reproduced = prior_id == entry["object_id"]
        if reason == "remote_size_changed" and reproduced and head_size and int(head_size) != int(entry["size_bytes"]):
            # Without this, a server whose HEAD never answers the body's size (a download script,
            # a redirect page) would make every later consumer move the whole object again.
            index["head_size_mismatch"] = {
                "object_id": entry["object_id"],
                "head_content_length": int(head_size),
                "object_size_bytes": int(entry["size_bytes"]),
                "recorded_at": now,
            }
        elif (index.get("head_size_mismatch") or {}).get("object_id") not in {None, entry["object_id"]}:
            index.pop("head_size_mismatch", None)
        _write_json_atomic(path, index)
        return index

    def _fail_claim(self, key: str, unit_id: str, message: str) -> None:
        path = self.claim_path(key, unit_id)
        with self._claim_lock(key):
            claim = _read_json(path)
            if claim:
                claim["last_error"] = {"at": _now(), "message": message}
                _write_json_atomic(path, claim)

    def _record_fetch(self, key: str, unit_id: str, entry: dict[str, Any], action: str, job_id: str) -> dict[str, Any]:
        path = self.claim_path(key, unit_id)
        with self._claim_lock(key, job_id=job_id):
            claim = _read_json(path) or {}
            if claim.get("state") == "materialized" and claim.get("object_id") != entry["object_id"]:
                # The unit's tree links an older object; it has to be materialized again.
                claim["state"] = "pending"
            claim["object_id"] = entry["object_id"]
            claim["last_fetch"] = {"action": action, "at": _now(), "job_id": job_id}
            claim.pop("last_error", None)
            _write_json_atomic(path, claim)
        return claim

    def _note_taint(self, entry: dict[str, Any], check: Mapping[str, Any]) -> None:
        entry.setdefault("taint_history", []).append(
            {
                "detected_at": _now(),
                "mismatch_count": check["mismatch_count"],
                "mismatches": check["mismatches"][:10],
                "object_intact": check["object_intact"],
            }
        )
        _write_json_atomic(self.entry_path(entry["object_id"]), entry)

    # ---- member stats ----------------------------------------------------------------------------

    def verify_members(self, object_id: str) -> dict[str, Any]:
        """Compare every file in members.tsv with its recorded size and mtime_ns.

        A hardlink is the same file record as the store's own name, so a vendor reader or the
        Console writing into a linked file in place changes the store object and every other unit's
        view of it. This check is how that is found afterwards; it runs before each reuse and each
        materialization, and should run after each Console run on linked inputs.
        """
        directory = self.object_directory(object_id)
        rows = _read_members(self.members_path(object_id))
        mismatches = []
        object_intact = True
        for row in rows:
            path = directory / Path(*PurePosixPath(row["path"]).parts)
            expected = {"size": int(row["size"]), "mtime_ns": int(row["mtime_ns"])}
            try:
                current = path.stat()
            except FileNotFoundError:
                mismatches.append({"path": row["path"], "kind": "missing", "expected": expected})
                object_intact = object_intact and not row["path"].startswith("obj/")
                continue
            actual = {"size": current.st_size, "mtime_ns": current.st_mtime_ns}
            if actual != expected:
                mismatches.append({"path": row["path"], "kind": "changed", "expected": expected, "actual": actual})
                object_intact = object_intact and not row["path"].startswith("obj/")
        return {
            "object_id": object_id,
            "checked": len(rows),
            "mismatch_count": len(mismatches),
            "mismatches": mismatches[:_REPORTED_ITEMS],
            "intact": bool(rows) and not mismatches,
            "object_intact": bool(rows) and object_intact,
            "tree_intact": not any(item["path"].startswith("t/") for item in mismatches),
        }

    def _write_members(self, object_id: str, rows: list[dict[str, Any]]) -> None:
        lines = ["\t".join(MEMBERS_HEADER)]
        for row in rows:
            lines.append(
                "\t".join(
                    [
                        _escape_tsv(row["path"]),
                        str(int(row["size"])),
                        _escape_tsv(str(row.get("crc") or "")),
                        str(int(row["mtime_ns"])),
                    ]
                )
            )
        _write_text_atomic(self.members_path(object_id), "\n".join(lines) + "\n")

    # ---- materialization -------------------------------------------------------------------------

    def materialize(
        self,
        unit_id: str,
        data_root: str | Path,
        placements: Iterable[Mapping[str, Any]],
        *,
        link: Callable[[Path, Path], None] = os.link,
        copy: Callable[[Path, Path], Any] = shutil.copy2,
        max_path_length: int | None = MAX_WINDOWS_PATH if os.name == "nt" else None,
        max_directory_length: int | None = MAX_WINDOWS_DIRECTORY_PATH if os.name == "nt" else None,
    ) -> dict[str, Any]:
        """Link claimed objects into a unit's own tree and mark its claims materialized.

        Each placement is {"url": ..., "source": "object" | "tree", "target": relative path}. An
        "object" placement puts the object file at `target`, a per-file object's declared name. A
        "tree" placement puts the extraction tree's files under `target` ("" for the data root), so
        a bundle archive keeps its internal relative paths, as the per-unit extraction did.
        """
        unit_id = _unit_id(unit_id)
        data_root = Path(data_root).resolve()
        if data_root == self.root or self.root in data_root.parents:
            raise StoreError("A unit tree must never lie inside the download store.")
        pairs: list[tuple[Path, str]] = []
        objects: list[tuple[str, str]] = []
        for placement in placements:
            url = str(placement.get("url") or "")
            claim = self.read_claim(url, unit_id)
            if not claim or claim.get("state") not in LIVE_CLAIM_STATES or not claim.get("object_id"):
                raise StoreError(f"Unit {unit_id} has no fetched claim on {url}; call fetch_or_reuse first.")
            object_id = claim["object_id"]
            entry = self._ready_entry(object_id)
            if entry is None:
                raise StoreError(f"Object {object_id} for {url} is not ready.")
            check = self.verify_members(object_id)
            if not check["intact"]:
                # Recorded by the fetch_or_reuse that repairs it, which holds the object's lock.
                raise StoreError(
                    f"Object {object_id} for {url} was modified in place ({check['mismatch_count']} "
                    "member(s)); fetch_or_reuse it again before materializing."
                )
            source_kind = str(placement.get("source") or "object")
            if source_kind == "tree":
                if not entry.get("tree"):
                    raise StoreError(f"Object {object_id} for {url} has no extraction tree.")
                source = self.object_directory(object_id) / "t"
            elif source_kind == "object":
                source = self.object_directory(object_id) / "obj" / entry["name"]
            else:
                raise StoreError(f"Unknown placement source {source_kind!r}.")
            pairs.append((source, str(placement.get("target") or "")))
            objects.append((url, object_id))
        try:
            record = materialize_unit_tree(
                data_root,
                pairs,
                link=link,
                copy=copy,
                max_path_length=max_path_length,
                max_directory_length=max_directory_length,
            )
        except MaterializationCollision as error:
            for url, _object_id in objects:
                self._fail_claim(self.url_key(url), unit_id, str(error))
            raise
        summary = {key_name: value for key_name, value in record.items() if key_name != "copy_fallbacks"}
        for url, object_id in objects:
            self.mark_materialized(url, unit_id, object_id=object_id, record=summary)
        record["objects"] = [{"url": url, "object_id": object_id} for url, object_id in objects]
        return record

    # ---- garbage collection ----------------------------------------------------------------------

    def gc(self, authorization: Any, *, object_ids: Iterable[str] | None = None, job_id: str = "") -> dict[str, Any]:
        """Delete objects no live claim keeps, only where a campaign authorization covers deleting them.

        `authorization` is a path to the campaign-authorization record, or the record that
        CampaignAuthorization.load returned. Without one nothing is deleted. Nothing is deleted
        either when the approval is revoked, does not cover boundary 5, or keeps raw data. An
        object, or an abandoned partial transfer, is deleted only when the approval covers
        boundary 5 for every unit whose released claim left it unclaimed; one no unit ever claimed
        is kept and reported, because no approval can name the unit it belongs to.

        A collected object keeps entry.json (as a tombstone naming the approval, the sha256 of the
        record's file, the units it covered and the releases that freed the object) and
        members.tsv; its obj, t and partial files go. The decision and the collection are made
        under the URL's claim lock, so a claim written meanwhile is never deleted from under. An
        object whose locks are held is left for a later pass rather than waited for.
        """
        authority = _gc_authority(authorization)
        result: dict[str, Any] = {
            "store": str(self.root),
            "authorized": False,
            "approval_id": authority.approval_id if authority else None,
            "raw_retention_policy": authority.raw_retention_policy if authority else None,
            "collected": [],
            "kept": [],
            "refused": [],
            "busy": [],
            "partials_removed": [],
            "partials_kept": [],
        }
        if authority is None:
            result["reason"] = "no campaign authorization; the store deletes nothing without one"
            return result
        # What the record refuses for every unit alike: asked for no unit, so drop that one code.
        verdict = authority.check("", GC_BOUNDARY)
        refusals = [(code, text) for code, text in zip(verdict["codes"], verdict["reasons"]) if code != "unit_unnamed"]
        if refusals:
            result["refusal_codes"] = [code for code, _text in refusals]
            result["reason"] = "Nothing is deleted. " + " ".join(text for _code, text in refusals)
            return result
        result["authorized"] = True
        candidates = sorted(set(object_ids)) if object_ids is not None else [
            path.name for path in _sorted_children(self.root / "o") if path.is_dir()
        ]
        for object_id in candidates:
            entry = self.entry(object_id)
            if not entry or entry.get("state") == "collected":
                continue
            keys = sorted({item.get("url_key") for item in entry.get("urls") or [] if item.get("url_key")})
            names = [f"u-{key}" for key in keys] + [f"o-{object_id}"] + [f"c-{key}" for key in keys]
            with self._try_locks(names, job_id) as held:
                if not held:
                    result["busy"].append(object_id)
                    continue
                # Read again under the locks: a consumer may have changed it since.
                entry = self.entry(object_id)
                if not entry or entry.get("state") == "collected":
                    continue
                live = self.live_claims(object_id)
                if live:
                    result["kept"].append(
                        {"object_id": object_id, "live_claims": sorted({claim["unit_id"] for claim in live})}
                    )
                    continue
                release_record = self._release_record(keys, object_id)
                covered, refused = _deletion_scope(authority, release_record)
                if refused:
                    result["refused"].append({"object_id": object_id, "units": refused})
                    continue
                result["collected"].append(
                    self._collect(object_id, entry, keys, authority, release_record, covered)
                )
        result["partials_removed"], result["partials_kept"] = self._collect_partials(authority, job_id)
        return result

    def _release_record(self, keys: Iterable[str], object_id: str | None) -> list[dict[str, Any]]:
        """The claims on these URLs that named the object, or no object yet; None means any object."""
        record = []
        for key in keys:
            for path in _claim_files(self.root / "claims" / key):
                claim = _read_json(path)
                if claim and (object_id is None or claim.get("object_id") in {None, object_id}):
                    record.append(
                        {
                            key_name: claim.get(key_name)
                            for key_name in ("unit_id", "url", "state", "release_reason", "released_at")
                        }
                    )
        return record

    def _collect(
        self,
        object_id: str,
        entry: dict[str, Any],
        keys: list[str],
        authority: CampaignAuthorization,
        release_record: list[dict[str, Any]],
        covered: list[dict[str, Any]],
    ) -> dict[str, Any]:
        directory = self.object_directory(object_id)
        kept = []
        removed_bytes = 0
        for part in ("obj", "t", "t.partial"):
            target = directory / part
            if target.exists():
                removal = unlink_tree(target)
                removed_bytes += removal["removed_bytes"]
                kept.extend(removal["kept"])
        for key in keys:
            # A partial belongs to the URL, whose claims may also name other objects.
            _covered, refused = _deletion_scope(authority, self._release_record([key], None))
            if not self._live_partial_claims(key) and not refused:
                destination = self.partial_path(key)
                for path in (destination, destination.with_name(f"{key}.part"), destination.with_name(f"{key}.json")):
                    with contextlib.suppress(FileNotFoundError):
                        removed_bytes += path.stat().st_size
                        path.unlink()
        entry.update(
            state="collected" if not kept else "collection_incomplete",
            collected_at=_now(),
            collected_under=_collection_authority(authority, covered),
            collected_bytes=removed_bytes,
            release_record=release_record,
        )
        if kept:
            entry["collection_kept"] = kept[:_REPORTED_ITEMS]
        else:
            entry.pop("collection_kept", None)
        _write_json_atomic(self.entry_path(object_id), entry)
        return {
            "object_id": object_id,
            "state": entry["state"],
            "removed_bytes": removed_bytes,
            "kept": kept[:_REPORTED_ITEMS],
        }

    def _live_partial_claims(self, key: str) -> bool:
        for path in _claim_files(self.root / "claims" / key):
            claim = _read_json(path)
            if claim and claim.get("state") in LIVE_CLAIM_STATES:
                return True
        return False

    def _collect_partials(
        self, authority: CampaignAuthorization, job_id: str
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Remove abandoned partial transfers, under the same per-unit scope as objects."""
        removed: list[str] = []
        kept: list[dict[str, Any]] = []
        keys = sorted(
            {
                path.name.split(".", 1)[0]
                for path in _sorted_children(self.root / "partial")
                if not path.name.startswith(".")
            }
        )
        for key in keys:
            if self._live_partial_claims(key):
                continue
            with self._try_locks([f"u-{key}", f"c-{key}"], job_id) as held:
                if not held or self._live_partial_claims(key):
                    continue
                _covered, refused = _deletion_scope(authority, self._release_record([key], None))
                if refused:
                    kept.append({"url_key": key, "units": refused})
                    continue
                for path in _sorted_children(self.root / "partial"):
                    if path.name.split(".", 1)[0] == key and path.is_file():
                        with contextlib.suppress(FileNotFoundError):
                            path.unlink()
                            removed.append(path.name)
        return removed, kept

    @contextlib.contextmanager
    def _try_locks(self, names: list[str], job_id: str) -> Iterator[bool]:
        """Take every lock without waiting, or none. GC never waits, so it can never deadlock."""
        held: list[StoreLock] = []
        acquired = True
        for name in names:
            try:
                held.append(self.lock(name, job_id=job_id).acquire(timeout=0))
            except StoreLockTimeout:
                acquired = False
                break
        try:
            if not acquired:
                for handle in reversed(held):
                    handle.release()
                held = []
            yield acquired
        finally:
            for handle in reversed(held):
                handle.release()

    # ---- status ----------------------------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        """A read-only picture of the store: objects, who keeps them, partials and lock holders."""
        objects = []
        for directory in _sorted_children(self.root / "o"):
            entry = _read_json(directory / "entry.json")
            if not entry:
                objects.append({"object_id": directory.name, "state": "incomplete"})
                continue
            live = self.live_claims(directory.name) if entry.get("state") != "collected" else []
            objects.append(
                {
                    "object_id": directory.name,
                    "state": entry.get("state"),
                    "name": entry.get("name"),
                    "size_bytes": entry.get("size_bytes"),
                    "urls": [item.get("url") for item in entry.get("urls") or []],
                    "tree_files": (entry.get("tree") or {}).get("files"),
                    "live_claims": sorted({claim["unit_id"] for claim in live}),
                    "taint_events": len(entry.get("taint_history") or []),
                }
            )
        claims: dict[str, int] = {}
        for directory in _sorted_children(self.root / "claims"):
            for path in _claim_files(directory):
                claim = _read_json(path) or {}
                state = str(claim.get("state") or "unreadable")
                claims[state] = claims.get(state, 0) + 1
        locks = [
            public_lock_holder(info)
            for path in _sorted_children(self.root / "locks")
            if path.suffix == ".lock"
            for info in [inspect_lock(path, self.lock_stale_seconds)]
            if info
        ]
        partials = [
            {"name": path.name, "size_bytes": path.stat().st_size}
            for path in _sorted_children(self.root / "partial")
            if path.is_file() and not path.name.endswith(".json") and not path.name.startswith(".")
        ]
        return {"store": str(self.root), "objects": objects, "claims": claims, "partials": partials, "locks": locks}


# --------------------------------------------------------------------------------------------------
# Unit trees
# --------------------------------------------------------------------------------------------------


def materialize_unit_tree(
    data_root: str | Path,
    pairs: Iterable[tuple[str | Path, str]],
    *,
    link: Callable[[Path, Path], None] = os.link,
    copy: Callable[[Path, Path], Any] = shutil.copy2,
    max_path_length: int | None = MAX_WINDOWS_PATH if os.name == "nt" else None,
    max_directory_length: int | None = MAX_WINDOWS_DIRECTORY_PATH if os.name == "nt" else None,
) -> dict[str, Any]:
    """Build real directories under `data_root` with one hardlink per source file.

    `pairs` are (source, target): a source file lands at `target`; a source directory's files land
    under `target` with their relative paths. Every destination is planned before anything is
    written, and the whole placement is refused when two sources want one path (compared
    case-insensitively, as NTFS does), when a file and a directory want one path, when an existing
    different file is in the way, when a file's path would pass `max_path_length`, or when a
    directory's path, the root's included, would pass `max_directory_length`. Nothing is ever
    overwritten. A file already linked to the same source is left as it is, so a retry is a no-op.
    A link that fails (another volume, the 1023-link NTFS limit, a filesystem without links) falls
    back to a copy, and the record says so.
    """
    if os.path.lexists(data_root) and _is_link_like(os.lstat(data_root)):
        raise StoreError(f"A unit tree root must be a real directory, not a link: {data_root}")
    root = Path(data_root).resolve()
    files: dict[str, tuple[Path, PurePosixPath]] = {}
    directories: dict[str, PurePosixPath] = {}
    collisions: list[dict[str, Any]] = []

    def claim_directory(relative: PurePosixPath, source: Path) -> None:
        for depth in range(1, len(relative.parts) + 1):
            prefix = PurePosixPath(*relative.parts[:depth])
            folded = prefix.as_posix().casefold()
            if folded in files:
                sources = [str(files[folded][0]), str(source)]
                collisions.append({"path": prefix.as_posix(), "kind": "file_and_directory", "sources": sources})
                return
            directories.setdefault(folded, prefix)

    def claim_file(relative: PurePosixPath, source: Path) -> None:
        folded = relative.as_posix().casefold()
        if folded in files:
            collisions.append(
                {"path": relative.as_posix(), "kind": "same_path", "sources": [str(files[folded][0]), str(source)]}
            )
            return
        if folded in directories:
            collisions.append({"path": relative.as_posix(), "kind": "file_and_directory", "sources": [str(source)]})
            return
        if relative.parent.parts:
            claim_directory(relative.parent, source)
        files[folded] = (source, relative)

    for source_value, target_value in pairs:
        source = Path(source_value)
        target = _relative_target(target_value)
        details = os.lstat(source)
        if _is_link_like(details):
            raise StoreError(f"Store sources must not be links or reparse points: {source}")
        if stat.S_ISDIR(details.st_mode):
            if target.parts:
                claim_directory(target, source)
            for directory, file_path in _walk_tree(source):
                relative = target.joinpath(*(directory or file_path).relative_to(source).parts)
                if directory is not None:
                    claim_directory(relative, source)
                else:
                    claim_file(relative, file_path)
        else:
            if not target.parts:
                raise StoreError(f"A file placement needs a target name: {source}")
            claim_file(target, source)

    for folded, (source, relative) in files.items():
        destination = root.joinpath(*relative.parts)
        if max_path_length is not None and len(str(destination)) > max_path_length:
            collisions.append({"path": relative.as_posix(), "kind": "path_too_long", "length": len(str(destination))})
            continue
        if os.path.lexists(destination):
            if destination.is_dir() or _is_link_like(os.lstat(destination)):
                collisions.append({"path": relative.as_posix(), "kind": "exists_as_other_type"})
            elif not os.path.samefile(source, destination):
                collisions.append({"path": relative.as_posix(), "kind": "exists_different"})
        parent = destination.parent
        while parent != root and root in parent.parents:
            if os.path.lexists(parent) and not parent.is_dir():
                collisions.append({"path": relative.as_posix(), "kind": "parent_is_a_file"})
                break
            parent = parent.parent
    if max_directory_length is not None and len(str(root)) > max_directory_length:
        collisions.append({"path": "", "kind": "path_too_long", "length": len(str(root)), "directory": True})
    for folded, relative in directories.items():
        destination = root.joinpath(*relative.parts)
        if max_directory_length is not None and len(str(destination)) > max_directory_length:
            # Every parent of every file is here, so a short file name under a long directory is
            # refused now, rather than by CreateDirectoryW after shallower directories exist.
            collisions.append(
                {
                    "path": relative.as_posix(),
                    "kind": "path_too_long",
                    "length": len(str(destination)),
                    "directory": True,
                }
            )
            continue
        if os.path.lexists(destination) and (not destination.is_dir() or _is_link_like(os.lstat(destination))):
            collisions.append({"path": relative.as_posix(), "kind": "exists_as_other_type"})
    if collisions:
        raise MaterializationCollision(collisions)

    root.mkdir(parents=True, exist_ok=True)
    for relative in sorted(directories.values(), key=lambda value: len(value.parts)):
        root.joinpath(*relative.parts).mkdir(exist_ok=True)
    linked = copied = present = 0
    logical = linked_bytes = copied_bytes = 0
    fallbacks: list[dict[str, Any]] = []
    for source, relative in sorted(files.values(), key=lambda item: item[1].as_posix()):
        destination = root.joinpath(*relative.parts)
        size = source.stat().st_size
        logical += size
        if os.path.lexists(destination):
            present += 1
            linked_bytes += size
            continue
        try:
            link(source, destination)
        except FileExistsError:
            raise
        except OSError as error:
            copy(source, destination)
            copied += 1
            copied_bytes += size
            fallbacks.append(
                {
                    "path": relative.as_posix(),
                    "error": f"{type(error).__name__}: {error}",
                    "errno": getattr(error, "errno", None),
                    "winerror": getattr(error, "winerror", None),
                }
            )
            continue
        linked += 1
        linked_bytes += size
    if copied and (linked or present):
        method = "mixed"
    elif copied:
        method = "copy"
    elif linked or present:
        method = "hardlink"
    else:
        method = "none"
    return {
        "data_root": str(root),
        "materialization": method,
        "files": len(files),
        "directories": len(directories),
        "linked_files": linked,
        "copied_files": copied,
        "already_present_files": present,
        "logical_bytes": logical,
        "bytes_linked_from_store": linked_bytes,
        "bytes_copied": copied_bytes,
        "copy_fallback_count": len(fallbacks),
        "copy_fallbacks": fallbacks[:_REPORTED_ITEMS],
    }


def unlink_tree(root: str | Path) -> dict[str, Any]:
    """Remove a tree of links, never changing the attributes of a file other names still share.

    Reparse points (symlinks, junctions) are removed themselves and never followed. A file with
    st_nlink > 1 is a name of a record that the store or another unit also names: its read-only
    attribute belongs to all of them, so it is never cleared; the name is removed with the
    disposition flag that ignores the attribute, or, where that is unavailable, kept and reported.
    A file with one link is the tree's own, and its read-only attribute may be cleared. A file held
    open without delete sharing is kept and reported, and the caller must not record the tree as
    removed while `complete` is false.
    """
    root = Path(root)
    result: dict[str, Any] = {
        "root": str(root),
        "removed_files": 0,
        "removed_links": 0,
        "removed_directories": 0,
        "removed_reparse_points": 0,
        "removed_bytes": 0,
        "kept": [],
        "kept_count": 0,
        "complete": True,
    }
    if root.resolve().parent == root.resolve():
        raise StoreError(f"Refusing to remove a filesystem root: {root}")
    if os.path.lexists(root):
        _unlink_entry(root, result)
    result["kept_count"] = len(result["kept"])
    result["kept"] = result["kept"][:_REPORTED_ITEMS]
    result["complete"] = result["kept_count"] == 0 and not os.path.lexists(root)
    return result


def _unlink_entry(path: Path, result: dict[str, Any]) -> None:
    try:
        details = os.lstat(path)
    except FileNotFoundError:
        return
    if _is_link_like(details):
        try:
            attributes = getattr(details, "st_file_attributes", 0)
            if stat.S_ISDIR(details.st_mode) or attributes & stat.FILE_ATTRIBUTE_DIRECTORY:
                os.rmdir(path)
            else:
                os.unlink(path)
            result["removed_reparse_points"] += 1
        except OSError as error:
            result["kept"].append({"path": str(path), "reason": "reparse_point_not_removed", "error": str(error)})
        return
    if stat.S_ISDIR(details.st_mode):
        try:
            with os.scandir(path) as entries:
                children = [Path(entry.path) for entry in entries]
        except OSError as error:
            result["kept"].append({"path": str(path), "reason": "directory_unreadable", "error": str(error)})
            return
        kept_before = len(result["kept"])
        for child in children:
            _unlink_entry(child, result)
        if len(result["kept"]) > kept_before:
            # Not empty: what was kept is already reported, and its directory stays with it.
            return
        try:
            os.rmdir(path)
        except PermissionError:
            # A directory is never a hardlink, so its read-only attribute is this tree's own.
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
                os.rmdir(path)
            except OSError as error:
                result["kept"].append({"path": str(path), "reason": "directory_not_removed", "error": str(error)})
                return
        except OSError as error:
            result["kept"].append({"path": str(path), "reason": "directory_not_removed", "error": str(error)})
            return
        result["removed_directories"] += 1
        return
    shared = details.st_nlink > 1
    try:
        os.unlink(path)
    except PermissionError as error:
        if not _is_read_only(details):
            result["kept"].append({"path": str(path), "reason": "busy", "error": str(error)})
            return
        if shared:
            try:
                removed = _unlink_ignoring_read_only(path)
            except OSError as inner:
                result["kept"].append({"path": str(path), "reason": "busy", "error": str(inner)})
                return
            if not removed:
                result["kept"].append({"path": str(path), "reason": "read_only_and_multiply_linked"})
                return
        else:
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
                os.unlink(path)
            except OSError as inner:
                result["kept"].append({"path": str(path), "reason": "busy", "error": str(inner)})
                return
    except FileNotFoundError:
        return
    except OSError as error:
        result["kept"].append({"path": str(path), "reason": "not_removed", "error": str(error)})
        return
    result["removed_links" if shared else "removed_files"] += 1
    if not shared:
        result["removed_bytes"] += details.st_size


_DELETE = 0x00010000
_FILE_SHARE_ALL = 0x1 | 0x2 | 0x4
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000
_FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
_FILE_DISPOSITION_INFO_EX = 21
_FILE_DISPOSITION_FLAG_DELETE = 0x1
_FILE_DISPOSITION_FLAG_POSIX_SEMANTICS = 0x2
_FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE = 0x10
_UNSUPPORTED_WINERRORS = {1, 50, 87, 124}


def _unlink_ignoring_read_only(path: Path) -> bool:
    """Remove one name of a read-only file without clearing the attribute (Windows 10 1809+).

    Returns False where the disposition flag is not supported, and raises OSError when the file
    cannot be opened for deletion (for example, held open without delete sharing).
    """
    if os.name != "nt":
        return False
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CreateFileW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    kernel32.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.SetFileInformationByHandle.argtypes = (wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    handle = kernel32.CreateFileW(
        str(path), _DELETE, _FILE_SHARE_ALL, None, _OPEN_EXISTING,
        _FILE_FLAG_BACKUP_SEMANTICS | _FILE_FLAG_OPEN_REPARSE_POINT, None,
    )
    if handle is None or handle == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        flags = wintypes.DWORD(
            _FILE_DISPOSITION_FLAG_DELETE
            | _FILE_DISPOSITION_FLAG_POSIX_SEMANTICS
            | _FILE_DISPOSITION_FLAG_IGNORE_READONLY_ATTRIBUTE
        )
        size = ctypes.sizeof(flags)
        if kernel32.SetFileInformationByHandle(handle, _FILE_DISPOSITION_INFO_EX, ctypes.byref(flags), size):
            return True
        error = ctypes.get_last_error()
        if error in _UNSUPPORTED_WINERRORS:
            return False
        raise ctypes.WinError(error)
    finally:
        kernel32.CloseHandle(handle)


# --------------------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _path_component(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if (
        not text
        or text in {".", ".."}
        or _UNSAFE_NAME_CHARACTERS.search(text)
        or text[-1] in ". "
        or text.split(".", 1)[0].casefold() in _RESERVED_NAMES
    ):
        raise StoreError(f"Unsafe {label} for a store path: {value!r}")
    return text


def _unit_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise StoreError("A claim needs a unit id.")
    return text


def _unit_file_name(unit_id: str) -> str:
    """A claim's file name. Readable for Catalog-style ids, hashed for anything else.

    Readable names are lower case only, because NTFS folds case: two ids differing only in case
    would otherwise share one claim file. The hashed form starts with '_', which a readable name
    cannot, so the two forms never collide.
    """
    readable = _SAFE_UNIT_FILE.fullmatch(unit_id) and not unit_id.endswith(".")
    if readable and unit_id.split(".", 1)[0] not in _RESERVED_NAMES:
        return f"{unit_id}.json"
    return f"_u_{hashlib.sha256(unit_id.encode('utf-8')).hexdigest()[:24]}.json"


def _safe_object_name(value: Any) -> str:
    """One path component for obj/<name>, keeping the original name wherever it is safe.

    The name is kept because later stages judge an object by its suffix (the gate's archive test
    reads it), so it is sanitized character by character rather than replaced.
    """
    text = str(value or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    text = _UNSAFE_NAME_CHARACTERS.sub("_", text).rstrip(". ")
    if not text:
        return "object"
    if text.split(".", 1)[0].casefold() in _RESERVED_NAMES:
        text = f"_{text}"
    return text[:180]


def _relative_target(value: Any) -> PurePosixPath:
    text = str(value or "").replace("\\", "/").lstrip("/")
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if any(part == ".." or ":" in part for part in parts):
        raise StoreError(f"Unsafe unit tree target: {value!r}")
    return PurePosixPath(*parts)


def _is_link_like(details: os.stat_result) -> bool:
    if stat.S_ISLNK(details.st_mode):
        return True
    return bool(getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _is_read_only(details: os.stat_result) -> bool:
    attributes = getattr(details, "st_file_attributes", None)
    if attributes is not None:
        return bool(attributes & stat.FILE_ATTRIBUTE_READONLY)
    return not details.st_mode & stat.S_IWUSR


def _walk_tree(root: Path) -> Iterator[tuple[Path | None, Path | None]]:
    """Yield (directory, None) and (None, file) below `root`, refusing links and reparse points."""
    with os.scandir(root) as entries:
        children = sorted(entries, key=lambda entry: entry.name)
    for entry in children:
        path = Path(entry.path)
        details = entry.stat(follow_symlinks=False)
        if _is_link_like(details):
            raise StoreError(f"Store trees must not contain links or reparse points: {path}")
        if stat.S_ISDIR(details.st_mode):
            yield path, None
            yield from _walk_tree(path)
        elif stat.S_ISREG(details.st_mode):
            yield None, path
        else:
            raise StoreError(f"Store trees hold only files and directories: {path}")


def _walk_files(root: Path) -> Iterator[Path]:
    for directory, file_path in _walk_tree(root):
        if file_path is not None:
            yield file_path


def _refuse_reparse_points(root: Path) -> None:
    for _ in _walk_tree(root):
        pass


def _member_row(directory: Path, path: Path) -> dict[str, Any]:
    details = path.stat()
    return {
        "path": PurePosixPath(*path.relative_to(directory).parts).as_posix(),
        "size": details.st_size,
        "crc": "",
        "mtime_ns": details.st_mtime_ns,
    }


def _escape_tsv(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\t", "\\t").replace("\n", "\\n").replace("\r", "\\r")


def _unescape_tsv(value: str) -> str:
    replacements = {"\\\\": "\\", "\\t": "\t", "\\n": "\n", "\\r": "\r"}
    return re.sub(r"\\[\\tnr]", lambda match: replacements[match.group(0)], value)


def _read_members(path: Path) -> list[dict[str, Any]]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    rows = []
    for line in text.splitlines()[1:]:
        if not line:
            continue
        member, size, crc, mtime_ns = line.split("\t")
        rows.append(
            {"path": _unescape_tsv(member), "size": int(size), "crc": _unescape_tsv(crc), "mtime_ns": int(mtime_ns)}
        )
    return rows


def _head_answer(head: Callable[[str], Any], url: str) -> dict[str, Any]:
    try:
        answer = head(url)
    except Exception as error:  # a HEAD is advisory; its failure must never fail the lease
        return {"content_length": None, "error": f"{type(error).__name__}: {error}"}
    if isinstance(answer, Mapping):
        return {
            "content_length": int(answer.get("content_length") or 0) or None,
            "etag": answer.get("etag"),
            "last_modified": answer.get("last_modified"),
        }
    try:
        return {"content_length": int(answer or 0) or None}
    except (TypeError, ValueError):
        return {"content_length": None}


def _count_contradicting_fetches(
    index: dict[str, Any],
    entry: Mapping[str, Any],
    prior_id: str | None,
    declared: str,
    now: str,
) -> None:
    """Update, in `index`, how many full fetches returned bytes each declared MD5 disagrees with.

    Each memo under refuted_declared_md5 names the object those fetches returned and counts them;
    it is `refuted` once REFUTING_FETCHES agree. Without the memo, every retry of a unit whose
    declaration is wrong would move the whole object again, and one of these objects is 928 GB;
    with a count of one, a single damaged transfer would fail the unit for good. A fetch that
    satisfies a declaration removes its memo, and one that returns other bytes starts it again.
    The first mismatching fetch counts twice when it reproduced the object the URL already
    pointed at, because that object was itself a full fetch of this URL.
    """
    object_id = entry["object_id"]
    first_count = 2 if prior_id == object_id else 1
    memos: dict[str, dict[str, Any]] = {}
    for digest, memo in (index.get("refuted_declared_md5") or {}).items():
        if digest == entry["md5"]:
            continue
        if memo.get("object_id") == object_id:
            memos[digest] = dict(memo, fetches=int(memo.get("fetches") or 1) + 1)
        else:
            memos[digest] = {
                "object_id": object_id, "object_md5": entry["md5"], "fetches": first_count, "first_mismatch_at": now,
            }
    if declared and declared != entry["md5"] and declared not in memos:
        memos[declared] = {
            "object_id": object_id, "object_md5": entry["md5"], "fetches": first_count, "first_mismatch_at": now,
        }
    for memo in memos.values():
        memo["refuted"] = int(memo["fetches"]) >= REFUTING_FETCHES
        if memo["refuted"]:
            memo.setdefault("refuted_at", now)
        else:
            memo.pop("refuted_at", None)
    if memos:
        index["refuted_declared_md5"] = memos
    else:
        index.pop("refuted_declared_md5", None)


def _hash_file(path: Path) -> tuple[str, str]:
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            sha256.update(chunk)
            md5.update(chunk)
    return sha256.hexdigest(), md5.hexdigest()


def _gc_authority(authorization: Any) -> CampaignAuthorization | None:
    """The campaign approval GC deletes under, or None when none was passed.

    Accepted: a path to the msdial-campaign-authorization.v1 record, read by
    CampaignAuthorization.load exactly as every other entry point reads it, or the record load
    returned. Not a mapping: the sha256 a tombstone records must be that of the file a person
    approved, not of a re-serialization, and a dict built in memory approves nothing. A record load
    refuses is refused here as well, loudly, because a deletion authority that silently failed to
    parse would look like "keep" forever.
    """
    if isinstance(authorization, CampaignAuthorization):
        return authorization
    if authorization is None or isinstance(authorization, (str, os.PathLike)):
        try:
            return load_campaign_authorization(authorization)
        except CampaignAuthorizationError as error:
            raise StoreError(str(error)) from error
    raise StoreError(
        "A campaign authorization is a path to its record, or the record CampaignAuthorization.load "
        f"returned; not a {type(authorization).__name__}."
    )


def _deletion_scope(
    authority: CampaignAuthorization, release_record: Iterable[Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """(units the approval covers boundary 5 for, {unit: refusal codes}) for the releasing units.

    Every unit whose claim named the bytes must be covered. With none, nothing is: no approval can
    name the unit an unclaimed object belongs to, so it is kept for a person to look at.
    """
    units = sorted({str(row.get("unit_id") or "") for row in release_record} - {""})
    if not units:
        return [], {"": ["no_releasing_unit"]}
    covered: list[dict[str, Any]] = []
    refused: dict[str, list[str]] = {}
    for unit in units:
        verdict = authority.check(unit, GC_BOUNDARY)
        if verdict["valid"]:
            covered.append({"unit_id": unit, "covered_as": verdict["covered_as"]})
        else:
            refused[unit] = list(verdict["codes"])
    return covered, refused


def _collection_authority(authority: CampaignAuthorization, covered: list[dict[str, Any]]) -> dict[str, Any]:
    """What a tombstone records of the approval: identity and digest, never the record's location."""
    return {
        "schema": authority.record.get("schema"),
        "approval_id": authority.approval_id,
        "campaign_id": authority.campaign_id,
        "manifest_digest": authority.manifest_digest,
        "authorization_sha256": authority.sha256,
        "raw_retention_policy": authority.raw_retention_policy,
        "boundary": GC_BOUNDARY,
        "units": covered,
    }


def _sorted_children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []


def _claim_files(directory: Path) -> list[Path]:
    """Claim records in one URL's claim directory; an atomic write's temporary file is not one."""
    return [path for path in _sorted_children(directory) if path.suffix == ".json" and not path.name.startswith(".")]


def _replace(source: Path, target: Path, attempts: int = 40, delay: float = 0.05) -> None:
    """os.replace, retried briefly: Windows refuses it while a reader holds the target open."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(temporary, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        _replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, value: Any) -> None:
    _write_text_atomic(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> dict[str, Any] | None:
    for attempt in range(40):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except PermissionError:
            # The record is being replaced at this instant.
            if attempt == 39:
                raise
            time.sleep(0.05)
        except json.JSONDecodeError as error:
            raise StoreError(f"Store record {path.name} is unreadable: {error}") from error
    return None


def _read_json_quiet(path: Path) -> dict[str, Any] | None:
    """A lock file's record, or None: a lock is briefly empty between its creation and its write."""
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None
