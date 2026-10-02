"""The accession-scoped download store: one fetch per object, one hardlink per unit file.

Every analysis unit used to download its own copy of every object it lists. In the declared pool
489 units touch a URL another unit also lists, and fetching each URL once instead of once per unit
saves 6.15 TB, about 71 days at the observed transfer rate; ST001408.zip alone is 928 GB used by
three units. The store fetches an object once per accession and gives each unit a tree of
hardlinks, because MS-DIAL writes its intermediates beside the files it reads and a unit must
therefore never read the store directly.

What these tests hold the store to:
- two consumers of one URL cause one transfer, and a concurrent consumer waits rather than fetch;
- a lock left by a dead process is recovered, judged without ever calling os.kill (on Windows
  os.kill terminates the process it is pointed at);
- a declared MD5 or a remote size that no longer matches the cache causes a fresh fetch, and the
  unit fails only if the fresh bytes do not match its declaration either;
- fetched bytes meet their declared MD5 before an extractor sees them, a declaration is taken as
  wrong only once two full fetches agree on other bytes, and a HEAD size a refetch showed to
  mislead does not send every later consumer to fetch again;
- a linked file written in place is detected, and the object is fetched or extracted again;
- an object is deleted only when no claim keeps it and the campaign approval, read from its file,
  is not revoked, says delete, and covers boundary 5 for every unit that released it; it leaves a
  tombstone, and a claim written while GC decides waits for the decision;
- a failed link falls back to a copy that is recorded as such, and a directory past 247 characters
  is refused before anything is written;
- removing a unit's tree never touches the store's file, even when it is read-only, because NTFS
  keeps attributes on the file record that every link shares.

All data are synthetic and live in temporary directories.
"""

from __future__ import annotations

import errno
import hashlib
import http.server
import io
import json
import os
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from msdial_app import download_store, process_liveness
from msdial_app.campaign_authorization import CampaignAuthorization
from msdial_app.download_store import (
    ClientFetcher,
    DeclaredChecksumMismatch,
    DownloadStore,
    MaterializationCollision,
    StoreError,
    StoreLockTimeout,
    inspect_lock,
    materialize_unit_tree,
    process_created_at,
    process_is_alive,
    unlink_tree,
)
from msdial_app.repository_reanalysis import RepositoryHttpClient


URL = "https://repository.example.org/studies/ST000001/sample1.mzML"
ARCHIVE_URL = "https://repository.example.org/studies/ST000001/ST000001.zip"
REPOSITORY = "metabolomics_workbench"
ACCESSION = "ST000001"
# The record campaign_authorization.py defines, as the campaign runner writes it. Synthetic throughout.
AUTHORIZATION = {
    "schema": "msdial-campaign-authorization.v1",
    "approval_id": "approval-synthetic-1",
    "campaign_id": "campaign-synthetic",
    "manifest_digest": "sha256:" + "0" * 64,
    "approved_by": "synthetic person",
    "approved_at": "2026-09-30T00:00:00+00:00",
    "statement": "synthetic approval for tests",
    "covers": [1, 3, 4, 5, "split"],
    "units": ["unit-a", "unit-b", "unit-c", "unit-x"],
    "raw_retention_policy": "delete_after_validated_output",
    "libraries": [{"name": "Synthetic.msp", "sha256": "ab" * 32}],
    "revoked_at": None,
}
KEEP = {**AUTHORIZATION, "raw_retention_policy": "keep"}
WINDOWS = os.name == "nt"


def _payload(tag: str, size: int = 4096) -> bytes:
    return (tag.encode("ascii") * (size // len(tag) + 1))[:size]


def _md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


V1 = _payload("version-one|")
V2 = _payload("version-two|", 5000)
V3 = _payload("version-three|")


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in members.items():
            handle.writestr(name, data)
    return buffer.getvalue()


ARCHIVE = _zip_bytes({"run1.mzML": _payload("run-one|"), "sub/run2.mzML": _payload("run-two|")})


class FakeFetcher:
    """Writes the configured bytes the way RepositoryHttpClient does: .part, then rename."""

    def __init__(self, payloads: dict[str, bytes], gate: threading.Event | None = None) -> None:
        self.payloads = dict(payloads)
        self.gate = gate
        self.calls: list[str] = []
        self.started = threading.Event()
        self._lock = threading.Lock()

    def fetch(self, url: str, destination: Path, progress_callback=None) -> dict:
        with self._lock:
            self.calls.append(url)
        self.started.set()
        if self.gate is not None and not self.gate.wait(30):
            raise TimeoutError("test gate was never opened")
        data = self.payloads[url]
        partial = destination.with_name(destination.name + ".part")
        partial.write_bytes(data)
        os.replace(partial, destination)
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": _sha256(data),
            "md5": _md5(data),
            "resumed_from_bytes": 0,
        }


class HeadFetcher(FakeFetcher):
    def __init__(self, payloads: dict[str, bytes], head_sizes: dict[str, int | None]) -> None:
        super().__init__(payloads)
        self.head_sizes = head_sizes
        self.heads: list[str] = []

    def head(self, url: str):
        self.heads.append(url)
        return self.head_sizes.get(url)


class ZipExtractor:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, archive: Path, destination: Path) -> dict:
        self.calls += 1
        with zipfile.ZipFile(archive) as handle:
            handle.extractall(destination)
            return {
                "tool": "zipfile",
                "members": [
                    {"path": item.filename, "crc": f"{item.CRC:08x}"}
                    for item in handle.infolist()
                    if not item.is_dir()
                ],
            }


def _dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class StoreTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._cleanup)
        self.workspace = Path(self.directory.name) / "analysis"
        self.store = DownloadStore(
            self.workspace, REPOSITORY, ACCESSION, lock_poll_seconds=0.02, lock_heartbeat_seconds=0.05
        )

    def _cleanup(self) -> None:
        # Read-only files left by a test would stop the temporary directory's own cleanup.
        for path in Path(self.directory.name).rglob("*"):
            try:
                os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass
        self.directory.cleanup()

    def unit_data(self, unit_id: str) -> Path:
        return self.workspace / REPOSITORY / ACCESSION / unit_id / "raw" / "data"

    def authorization(self, record: dict | None = None, *, prefix: bytes = b"", **changes) -> Path:
        """Write a campaign-authorization record to its own file and return the path GC is given."""
        body = {**(AUTHORIZATION if record is None else record), **changes}
        descriptor, name = tempfile.mkstemp(suffix=".json", prefix="campaign-authorization-", dir=self.directory.name)
        os.close(descriptor)
        path = Path(name)
        path.write_bytes(prefix + json.dumps(body, indent=2).encode("utf-8"))
        return path

    def fetch(self, unit_id: str, fetcher, url: str = URL, name: str = "sample1.mzML", **options) -> dict:
        return self.store.fetch_or_reuse(url, name, unit_id=unit_id, fetcher=fetcher, **options)

    def materialize(
        self, unit_id: str, url: str = URL, target: str = "sample1.mzML", source: str = "object", **options
    ) -> dict:
        return self.store.materialize(
            unit_id, self.unit_data(unit_id), [{"url": url, "source": source, "target": target}], **options
        )

    def object_file(self, result: dict) -> Path:
        return Path(result["object_path"])


class OneFetchTests(StoreTestCase):
    def test_two_consumers_share_one_fetch_and_one_file_record(self) -> None:
        fetcher = FakeFetcher({URL: V1})

        first = self.fetch("unit-a", fetcher, declared_md5=_md5(V1))
        second = self.fetch("unit-b", fetcher, declared_md5=_md5(V1))

        self.assertEqual([URL], fetcher.calls, "the second consumer must not transfer the object again")
        self.assertEqual(("fetched", "reused"), (first["action"], second["action"]))
        self.assertEqual("fetched_by_this_unit", first["sha256_origin"])
        self.assertEqual("inherited_from_cache", second["sha256_origin"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual(0, second["transferred_bytes"])
        self.assertEqual(len(V1), first["transferred_bytes"])
        self.assertEqual(_sha256(V1), first["sha256"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(first["md5"], second["md5"])
        self.assertEqual("unit-a", second["fetched_by"]["unit_id"])
        self.assertTrue(second["declared_checksum_verified"])
        self.assertEqual("md5", second["declared_checksum_algorithm"])

        self.materialize("unit-a")
        self.materialize("unit-b")
        store_file = self.object_file(first)
        links = [self.unit_data(unit) / "sample1.mzML" for unit in ("unit-a", "unit-b")]
        inodes = {os.stat(path).st_ino for path in [store_file, *links]}
        self.assertEqual(1, len(inodes), "both units link the store's file record")
        self.assertGreaterEqual(os.stat(store_file).st_nlink, 3)
        self.assertEqual(V1, links[1].read_bytes())
        self.assertTrue(self.store.verify_members(first["object_id"])["intact"], "linking must not look like a write")
        self.assertEqual("materialized", self.store.read_claim(URL, "unit-b")["state"])

    def test_the_object_keeps_its_original_name(self) -> None:
        """The gate judges an archive by its suffix, so the store must not rename the bytes."""
        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})
        result = self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="FILES/ST000001.zip")
        self.assertEqual("ST000001.zip", self.object_file(result).name)
        self.assertEqual("obj", self.object_file(result).parent.name)
        self.assertEqual(_sha256(ARCHIVE)[:16], result["object_id"])


class _CountingHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - silence the test output
        pass

    def do_HEAD(self) -> None:  # noqa: N802 - http.server's interface
        self.server.heads += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(V1)))
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        self.server.gets += 1
        self.send_response(200)
        self.send_header("Content-Length", str(len(V1)))
        self.end_headers()
        self.wfile.write(V1)


class HttpTests(StoreTestCase):
    def test_two_units_sharing_a_url_make_one_http_get(self) -> None:
        """Through the real client, against a real server: the second unit sends only a HEAD."""
        server = http.server.HTTPServer(("127.0.0.1", 0), _CountingHandler)
        server.gets = 0
        server.heads = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        url = f"http://127.0.0.1:{server.server_port}/ST000001/sample1.mzML"
        fetcher = ClientFetcher(RepositoryHttpClient(timeout=10), maximum_bytes=10_000_000)

        first = self.fetch("unit-a", fetcher, url=url, declared_md5=_md5(V1))
        second = self.fetch("unit-b", fetcher, url=url, declared_md5=_md5(V1))

        self.assertEqual(1, server.gets)
        self.assertEqual(1, server.heads, "the reuse check asks the server for the size once")
        self.assertEqual("match", second["reuse_check"]["remote_size"])
        self.assertEqual(first["sha256"], second["sha256"])
        self.assertEqual(V1, self.object_file(second).read_bytes())
        self.assertFalse(any(self.store.root.joinpath("partial").glob("*.part")), "no partial is left behind")


class WaiterTests(StoreTestCase):
    def test_a_concurrent_consumer_waits_for_the_fetch_instead_of_fetching_again(self) -> None:
        gate = threading.Event()
        fetcher = FakeFetcher({URL: V1}, gate=gate)
        results: dict[str, dict] = {}
        errors: list[BaseException] = []

        def run(unit_id: str, **options) -> None:
            try:
                results[unit_id] = self.fetch(unit_id, fetcher, lock_timeout=30, **options)
            except BaseException as error:  # surfaced below; a thread cannot fail the test itself
                errors.append(error)

        holder = threading.Thread(target=run, args=("unit-a",))
        holder.start()
        self.assertTrue(fetcher.started.wait(10), "the first consumer started its fetch")

        waits: list[dict] = []
        waiting = threading.Event()

        def on_wait(info: dict) -> None:
            waits.append(info)
            waiting.set()

        waiter = threading.Thread(target=run, args=("unit-b",), kwargs={"on_wait": on_wait})
        waiter.start()
        self.assertTrue(waiting.wait(10), "the second consumer reported that it is waiting")
        self.assertEqual(1, len(fetcher.calls), "it waits; it does not start a second transfer")
        gate.set()
        holder.join(30)
        waiter.join(30)

        self.assertEqual([], errors)
        self.assertEqual([URL], fetcher.calls)
        self.assertEqual("waiting_for_shared_download", waits[0]["status"])
        self.assertEqual(URL, waits[0]["url"])
        self.assertEqual(os.getpid(), waits[0]["pid"])
        self.assertNotIn("token", waits[0])
        self.assertNotIn("host", waits[0], "a status that may be recorded does not name the machine")
        self.assertTrue(waits[0]["holder_on_this_host"])
        self.assertNotIn("host", results["unit-b"]["lock_holder"])
        self.assertEqual("heartbeat_recent", waits[0]["reason"])
        self.assertFalse(waits[0]["stale"])
        self.assertTrue(results["unit-b"]["cache_hit"])
        self.assertTrue(results["unit-b"]["waited_for_lock"])
        self.assertEqual(results["unit-a"]["object_id"], results["unit-b"]["object_id"])


class LockTests(StoreTestCase):
    def lock_path(self, url: str = URL) -> Path:
        return self.store.root / "locks" / f"u-{self.store.url_key(url)}.lock"

    def plant_lock(
        self, *, pid: int, age_seconds: float, host: str | None = None, created_at=None, content=None
    ) -> Path:
        path = self.lock_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        if content is None:
            content = json.dumps(
                {
                    "schema": "msdial-download-store-lock.v1",
                    "token": "planted",
                    "pid": pid,
                    "process_created_at": created_at,
                    "host": host or socket.gethostname(),
                    "job_id": "job-from-a-backend-that-died",
                }
            )
        path.write_text(content, encoding="utf-8")
        moment = time.time() - age_seconds
        os.utime(path, (moment, moment))
        return path

    def test_a_stale_lock_of_a_dead_process_is_recovered_without_signalling_anyone(self) -> None:
        self.plant_lock(pid=_dead_pid(), age_seconds=3600)
        fetcher = FakeFetcher({URL: V1})

        with mock.patch("os.kill", side_effect=AssertionError("os.kill must never be used for liveness")):
            result = self.fetch("unit-a", fetcher, lock_timeout=10)

        self.assertEqual([URL], fetcher.calls)
        self.assertEqual(1, len(result["recovered_stale_locks"]))
        self.assertEqual("holder_dead_and_heartbeat_lapsed", result["recovered_stale_locks"][0]["reason"])
        self.assertFalse(self.lock_path().exists(), "the lock is released after the fetch")

    def test_a_dead_holder_with_a_recent_heartbeat_is_waited_for(self) -> None:
        path = self.plant_lock(pid=_dead_pid(), age_seconds=0)
        with self.assertRaises(StoreLockTimeout):
            self.store.lock(path.stem).acquire(timeout=0.3)
        self.assertTrue(path.exists())

    def test_a_live_holder_with_a_lapsed_heartbeat_is_waited_for(self) -> None:
        path = self.plant_lock(pid=os.getpid(), age_seconds=3600, created_at=process_created_at())
        with self.assertRaises(StoreLockTimeout):
            self.store.lock(path.stem).acquire(timeout=0.3)
        self.assertEqual("holder_alive", inspect_lock(path)["reason"])

    def test_a_reused_pid_is_recognised_by_its_creation_time(self) -> None:
        """Windows reuses PIDs: a live process that started later is not the lock's holder."""
        created = process_created_at()
        self.assertIsNotNone(created)
        path = self.plant_lock(pid=os.getpid(), age_seconds=3600, created_at=created - 5000)
        handle = self.store.lock(path.stem).acquire(timeout=5)
        try:
            self.assertEqual("holder_dead_and_heartbeat_lapsed", handle.recovered[0]["reason"])
        finally:
            handle.release()

    def test_an_unreadable_lock_is_recovered_only_after_its_heartbeat_lapses(self) -> None:
        """A crash between creating the lock and writing it leaves an empty file."""
        path = self.plant_lock(pid=0, age_seconds=0, content="")
        with self.assertRaises(StoreLockTimeout):
            self.store.lock(path.stem).acquire(timeout=0.3)
        self.plant_lock(pid=0, age_seconds=3600, content="")
        handle = self.store.lock(path.stem).acquire(timeout=5)
        handle.release()
        self.assertEqual("unreadable_and_heartbeat_lapsed", handle.recovered[0]["reason"])

    def test_a_holder_on_another_host_is_never_presumed_dead(self) -> None:
        path = self.plant_lock(pid=_dead_pid(), age_seconds=3600, host="another-host.invalid")
        with self.assertRaises(StoreLockTimeout):
            self.store.lock(path.stem).acquire(timeout=0.3)
        self.assertEqual("holder_on_another_host", inspect_lock(path)["reason"])

    def test_the_heartbeat_keeps_a_held_lock_fresh(self) -> None:
        handle = self.store.lock("long-transfer").acquire()
        try:
            moment = time.time() - 3600
            os.utime(handle.path, (moment, moment))
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and inspect_lock(handle.path)["heartbeat_age_seconds"] > 60:
                time.sleep(0.02)
            self.assertEqual("heartbeat_recent", inspect_lock(handle.path)["reason"])
        finally:
            handle.release()
        self.assertFalse(handle.path.exists())

    def test_release_never_deletes_a_lock_this_handle_no_longer_owns(self) -> None:
        handle = self.store.lock("taken-over").acquire()
        record = json.loads(handle.path.read_text(encoding="utf-8"))
        record["token"] = "somebody-else"
        handle.path.write_text(json.dumps(record), encoding="utf-8")
        handle.release()
        self.assertTrue(handle.path.exists())
        self.assertTrue(handle.lost)


class LivenessTests(unittest.TestCase):
    def test_liveness_is_read_and_never_signalled(self) -> None:
        dead = _dead_pid()
        with mock.patch("os.kill", side_effect=AssertionError("os.kill must never be used for liveness")):
            self.assertIs(True, process_is_alive(os.getpid()))
            self.assertIs(True, process_is_alive(os.getpid(), process_created_at()))
            self.assertIs(False, process_is_alive(os.getpid(), process_created_at() - 5000))
            self.assertIs(False, process_is_alive(dead))
            self.assertIsNone(process_is_alive(0))
            self.assertIsNone(process_is_alive("not-a-pid"))

    def test_an_exited_child_whose_handle_is_still_open_is_dead(self) -> None:
        """OpenProcess succeeds for such a process; only the wait says it has exited."""
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait()
        self.assertIs(False, process_is_alive(child.pid))
        if WINDOWS:
            self.assertEqual((False, None), process_liveness._windows_process_probe(child.pid))
            alive, created = process_liveness._windows_process_probe(os.getpid())
            self.assertIs(True, alive)
            self.assertAlmostEqual(process_created_at(), created, delta=1.0)

    @unittest.skipUnless(WINDOWS, "the native probe is the Windows path")
    def test_the_native_probe_agrees_with_psutil(self) -> None:
        try:
            import psutil
        except ImportError:
            self.skipTest("psutil is not installed")
        alive, created = process_liveness._windows_process_probe(os.getpid())
        self.assertTrue(alive)
        self.assertAlmostEqual(psutil.Process().create_time(), created, delta=1.0)


class DriftTests(StoreTestCase):
    def test_a_declared_md5_that_differs_from_the_cache_fetches_again(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        first = self.fetch("unit-a", fetcher, declared_md5=_md5(V1))
        self.materialize("unit-a")
        fetcher.payloads[URL] = V2  # upstream replaced the file

        second = self.fetch("unit-b", fetcher, declared_md5=_md5(V2))

        self.assertEqual([URL, URL], fetcher.calls)
        self.assertEqual("refetched", second["action"])
        self.assertEqual("declared_md5_differs", second["reuse_reason"])
        self.assertEqual("differs_from_cache", second["reuse_check"]["declared_md5"])
        self.assertNotEqual(first["object_id"], second["object_id"], "changed bytes are a new object")
        self.assertEqual(V2, self.object_file(second).read_bytes())
        self.assertEqual(V1, self.object_file(first).read_bytes(), "the earlier consumer's bytes are kept")
        self.assertEqual(first["object_id"], self.store.read_claim(URL, "unit-a")["object_id"])
        index = self.store.lookup(URL)
        self.assertEqual(second["object_id"], index["object_id"])
        self.assertEqual(first["object_id"], index["history"][0]["object_id"])

    def test_a_unit_fails_only_when_the_fresh_bytes_do_not_match_its_declaration_either(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.fetch("unit-a", fetcher, declared_md5=_md5(V1))

        with self.assertRaisesRegex(DeclaredChecksumMismatch, r"^MD5 checksum mismatch for sample1\.mzML\."):
            self.fetch("unit-c", fetcher, declared_md5=_md5(V3))

        self.assertEqual([URL, URL], fetcher.calls, "the declaration was checked against fresh bytes")
        claim = self.store.read_claim(URL, "unit-c")
        self.assertEqual("pending", claim["state"], "a failed unit keeps its claim")
        self.assertIn("MD5 checksum mismatch", claim["last_error"]["message"])

        with self.assertRaises(DeclaredChecksumMismatch):
            self.fetch("unit-c", fetcher, declared_md5=_md5(V3))
        self.assertEqual(2, len(fetcher.calls), "a refuted declaration is not downloaded a third time")

    def test_a_refuted_declaration_is_fetched_again_once_the_remote_size_changes(self) -> None:
        fetcher = HeadFetcher({URL: V1}, {URL: len(V1)})
        self.fetch("unit-a", fetcher)
        with self.assertRaises(DeclaredChecksumMismatch):
            self.fetch("unit-c", fetcher, declared_md5=_md5(V2))
        self.assertEqual(2, len(fetcher.calls))
        fetcher.payloads[URL] = V2  # upstream corrected the file
        fetcher.head_sizes[URL] = len(V2)

        corrected = self.fetch("unit-c", fetcher, declared_md5=_md5(V2))

        self.assertEqual(3, len(fetcher.calls))
        self.assertEqual("remote_size_changed", corrected["reuse_reason"])
        self.assertTrue(corrected["declared_checksum_verified"])

    def test_a_first_fetch_that_does_not_match_its_declaration_fails(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        with self.assertRaisesRegex(DeclaredChecksumMismatch, "MD5 checksum mismatch"):
            self.fetch("unit-a", fetcher, declared_md5=_md5(V2))
        self.assertEqual(1, len(fetcher.calls))

    def test_a_changed_remote_size_fetches_again(self) -> None:
        fetcher = HeadFetcher({URL: V1}, {URL: len(V2)})
        first = self.fetch("unit-a", fetcher)
        fetcher.payloads[URL] = V2

        second = self.fetch("unit-b", fetcher)

        self.assertEqual("refetched", second["action"])
        self.assertEqual("changed", second["reuse_check"]["remote_size"])
        self.assertNotEqual(first["object_id"], second["object_id"])
        self.assertEqual(2, len(fetcher.calls))
        self.assertNotIn("head_size_mismatch", self.store.lookup(URL), "the HEAD was right: the bytes changed")

    def test_an_equal_or_unanswered_remote_size_reuses(self) -> None:
        for answer, verdict in ((len(V1), "match"), (0, "not_answered"), (None, "not_answered")):
            with self.subTest(answer=answer):
                store = DownloadStore(self.workspace / str(answer), REPOSITORY, ACCESSION, lock_poll_seconds=0.02)
                fetcher = HeadFetcher({URL: V1}, {URL: answer})
                store.fetch_or_reuse(URL, "sample1.mzML", unit_id="unit-a", fetcher=fetcher)
                second = store.fetch_or_reuse(URL, "sample1.mzML", unit_id="unit-b", fetcher=fetcher)
                self.assertEqual("reused", second["action"])
                self.assertEqual(verdict, second["reuse_check"]["remote_size"])
                self.assertEqual(1, len(fetcher.calls))

    def test_a_head_that_fails_does_not_fail_the_lease(self) -> None:
        class BrokenHead(FakeFetcher):
            def head(self, url):
                raise OSError("HEAD not allowed")

        fetcher = BrokenHead({URL: V1})
        self.fetch("unit-a", fetcher)
        second = self.fetch("unit-b", fetcher)
        self.assertEqual("reused", second["action"])
        self.assertEqual("not_answered", second["reuse_check"]["remote_size"])


class TaintTests(StoreTestCase):
    def test_an_in_place_write_to_a_linked_file_is_caught_and_the_object_fetched_again(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        first = self.fetch("unit-a", fetcher)
        self.materialize("unit-a")
        linked = self.unit_data("unit-a") / "sample1.mzML"
        time.sleep(0.05)  # past the filesystem's timestamp granularity
        with linked.open("r+b") as handle:  # what a vendor reader writing in place would do
            handle.write(b"X" * 16)

        check = self.store.verify_members(first["object_id"])
        self.assertFalse(check["intact"])
        self.assertFalse(check["object_intact"])
        with self.assertRaisesRegex(StoreError, "modified in place"):
            self.materialize("unit-a")  # the stat check runs before every materialization

        second = self.fetch("unit-b", fetcher)

        self.assertEqual("refetched", second["action"])
        self.assertEqual("object_tainted", second["reuse_reason"])
        self.assertEqual(2, len(fetcher.calls))
        self.assertEqual(first["object_id"], second["object_id"], "the same bytes are the same object")
        self.assertEqual(V1, self.object_file(second).read_bytes(), "the store holds the published bytes again")
        self.assertNotEqual(os.stat(linked).st_ino, os.stat(self.object_file(second)).st_ino)
        self.assertTrue(self.store.verify_members(second["object_id"])["intact"])
        self.assertTrue(self.store.entry(first["object_id"])["taint_history"])
        self.materialize("unit-b")
        self.assertEqual(V1, (self.unit_data("unit-b") / "sample1.mzML").read_bytes())

    def test_a_modified_extraction_member_is_extracted_again_from_the_kept_archive(self) -> None:
        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})
        extractor = ZipExtractor()
        first = self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=extractor)
        self.materialize("unit-a", url=ARCHIVE_URL, source="tree", target="")
        linked = self.unit_data("unit-a") / "run1.mzML"
        time.sleep(0.05)
        with linked.open("ab") as handle:
            handle.write(b"appended by a reader")

        second = self.fetch("unit-b", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=extractor)

        self.assertEqual("re-extracted", second["action"])
        self.assertEqual(1, len(fetcher.calls), "the kept archive is enough; nothing is downloaded")
        self.assertEqual(2, extractor.calls)
        tree_file = Path(second["tree_path"]) / "run1.mzML"
        self.assertEqual(_payload("run-one|"), tree_file.read_bytes())
        self.assertTrue(linked.read_bytes().endswith(b"appended by a reader"), "unit A keeps what it has")
        self.assertTrue(self.store.verify_members(first["object_id"])["intact"])

    def test_an_archive_is_extracted_once_for_every_consumer(self) -> None:
        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})
        extractor = ZipExtractor()
        first = self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=extractor)
        self.fetch("unit-b", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=extractor)
        self.assertEqual(1, extractor.calls)
        entry = self.store.entry(first["object_id"])
        self.assertEqual(2, entry["tree"]["files"])
        self.assertEqual("zipfile", entry["extraction"]["tool"])
        rows = self.store.members_path(first["object_id"]).read_text(encoding="utf-8").splitlines()
        self.assertEqual("path\tsize\tcrc\tmtime_ns", rows[0])
        self.assertEqual(
            {"obj/ST000001.zip", "t/run1.mzML", "t/sub/run2.mzML"}, {row.split("\t")[0] for row in rows[1:]}
        )
        crc = {row.split("\t")[0]: row.split("\t")[2] for row in rows[1:]}
        self.assertRegex(crc["t/sub/run2.mzML"], r"^[0-9a-f]{8}$")

    def test_a_failed_extraction_leaves_no_tree_and_is_recorded(self) -> None:
        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})

        def broken(archive: Path, destination: Path) -> dict:
            (destination / "half.mzML").write_bytes(b"partial")
            raise ValueError("unsupported compression method")

        with self.assertRaisesRegex(ValueError, "unsupported compression"):
            self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=broken)
        object_id = _sha256(ARCHIVE)[:16]
        directory = self.store.object_directory(object_id)
        self.assertFalse((directory / "t").exists())
        self.assertFalse((directory / "t.partial").exists())
        self.assertIn("unsupported compression", self.store.entry(object_id)["extraction_failures"][0]["error"])

        retried = self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=ZipExtractor())
        self.assertEqual(1, len(fetcher.calls), "the retry extracts the kept bytes; it does not download again")
        self.assertEqual("reused", retried["action"])
        self.assertTrue((Path(retried["tree_path"]) / "sub" / "run2.mzML").is_file())

    def test_a_failed_fetch_keeps_its_partial_bytes_and_its_claim(self) -> None:
        class Interrupted(FakeFetcher):
            def fetch(self, url, destination, progress_callback=None):
                destination.with_name(destination.name + ".part").write_bytes(V1[:100])
                raise ConnectionResetError("the server hung up")

        with self.assertRaises(ConnectionResetError):
            self.fetch("unit-a", Interrupted({URL: V1}))
        claim = self.store.read_claim(URL, "unit-a")
        self.assertEqual("pending", claim["state"])
        self.assertIn("hung up", claim["last_error"]["message"])
        partial = self.store.partial_path(self.store.url_key(URL))
        self.assertEqual(100, partial.with_name(partial.name + ".part").stat().st_size)
        self.assertFalse(
            self.store.gc(self.authorization())["partials_removed"], "a claimed partial is kept for the resume"
        )


class MaterializeTests(StoreTestCase):
    def test_a_failed_link_falls_back_to_a_recorded_copy(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.fetch("unit-a", fetcher)

        def refuse(source, destination):
            raise OSError(errno.EXDEV, "Invalid cross-device link")

        record = self.materialize("unit-a", link=refuse)

        self.assertEqual("copy", record["materialization"])
        self.assertEqual(1, record["copied_files"])
        self.assertEqual(len(V1), record["bytes_copied"])
        self.assertEqual(0, record["bytes_linked_from_store"])
        self.assertEqual(errno.EXDEV, record["copy_fallbacks"][0]["errno"])
        unit_file = self.unit_data("unit-a") / "sample1.mzML"
        self.assertEqual(V1, unit_file.read_bytes())
        self.assertNotEqual(os.stat(unit_file).st_ino, os.stat(self.object_file(result)).st_ino)
        self.assertEqual(1, os.stat(self.object_file(result)).st_nlink)
        self.assertEqual("copy", self.store.read_claim(URL, "unit-a")["materialization"]["materialization"])

    def test_targets_that_collide_are_refused_before_anything_is_written(self) -> None:
        other = "https://repository.example.org/studies/ST000001/other/SAMPLE1.mzML"
        fetcher = FakeFetcher({URL: V1, other: V2})
        self.fetch("unit-a", fetcher)
        self.fetch("unit-a", fetcher, url=other, name="SAMPLE1.mzML")

        with self.assertRaises(MaterializationCollision) as caught:
            self.store.materialize(
                "unit-a",
                self.unit_data("unit-a"),
                [
                    {"url": URL, "source": "object", "target": "sample1.mzML"},
                    {"url": other, "source": "object", "target": "SAMPLE1.mzML"},
                ],
            )

        self.assertEqual("same_path", caught.exception.collisions[0]["kind"])
        self.assertFalse(self.unit_data("unit-a").exists(), "nothing was written")
        self.assertIn("refused", self.store.read_claim(URL, "unit-a")["last_error"]["message"])

    def test_root_less_per_sample_archives_do_not_overwrite_each_other(self) -> None:
        """Two zips whose members sit at the root under one name used to overwrite silently."""
        first_url = "https://repository.example.org/studies/ST000001/A.raw.zip"
        second_url = "https://repository.example.org/studies/ST000001/B.raw.zip"
        fetcher = FakeFetcher(
            {first_url: _zip_bytes({"_FUNC001.DAT": b"a"}), second_url: _zip_bytes({"_FUNC001.DAT": b"b"})}
        )
        extractor = ZipExtractor()
        self.fetch("unit-a", fetcher, url=first_url, name="A.raw.zip", extract=extractor)
        self.fetch("unit-a", fetcher, url=second_url, name="B.raw.zip", extract=extractor)
        with self.assertRaises(MaterializationCollision):
            self.store.materialize(
                "unit-a",
                self.unit_data("unit-a"),
                [
                    {"url": first_url, "source": "tree", "target": ""},
                    {"url": second_url, "source": "tree", "target": ""},
                ],
            )
        record = self.store.materialize(
            "unit-a",
            self.unit_data("unit-a"),
            [
                {"url": first_url, "source": "tree", "target": "A.raw"},
                {"url": second_url, "source": "tree", "target": "B.raw"},
            ],
        )
        self.assertEqual(2, record["linked_files"])
        self.assertEqual(b"b", (self.unit_data("unit-a") / "B.raw" / "_FUNC001.DAT").read_bytes())

    def test_an_existing_different_file_is_never_overwritten(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.fetch("unit-a", fetcher)
        existing = self.unit_data("unit-a") / "sample1.mzML"
        existing.parent.mkdir(parents=True)
        existing.write_bytes(b"the unit's own file")

        with self.assertRaises(MaterializationCollision) as caught:
            self.materialize("unit-a")

        self.assertEqual("exists_different", caught.exception.collisions[0]["kind"])
        self.assertEqual(b"the unit's own file", existing.read_bytes())

    def test_a_file_and_a_directory_cannot_share_a_path(self) -> None:
        source_root = Path(self.directory.name) / "sources"
        (source_root / "tree" / "x").mkdir(parents=True)
        (source_root / "tree" / "x" / "y.mzML").write_bytes(b"y")
        (source_root / "x").write_bytes(b"x")
        with self.assertRaises(MaterializationCollision) as caught:
            materialize_unit_tree(self.unit_data("unit-a"), [(source_root / "tree", ""), (source_root / "x", "x")])
        self.assertEqual("file_and_directory", caught.exception.collisions[0]["kind"])

    def test_materializing_again_changes_nothing(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.fetch("unit-a", fetcher)
        self.materialize("unit-a")
        again = self.materialize("unit-a")
        self.assertEqual((0, 1), (again["linked_files"], again["already_present_files"]))
        self.assertEqual("hardlink", again["materialization"])

    def test_a_path_past_the_length_limit_is_refused(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.fetch("unit-a", fetcher)
        limit = len(str(self.unit_data("unit-a").resolve())) + 5
        with self.assertRaises(MaterializationCollision) as caught:
            self.materialize("unit-a", max_path_length=limit)
        self.assertEqual("path_too_long", caught.exception.collisions[0]["kind"])

    def test_what_ms_dial_writes_beside_a_link_stays_in_the_unit_tree(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.fetch("unit-a", fetcher)
        self.materialize("unit-a")
        for name in ("sample1_2026930120.dcl", "sample1_2026930120.pai2", "sample1_tags.xml"):
            (self.unit_data("unit-a") / name).write_bytes(b"MS-DIAL intermediate")

        store_names = {path.name for path in self.store.root.rglob("*")}
        self.assertFalse(store_names & {"sample1_2026930120.dcl", "sample1_2026930120.pai2", "sample1_tags.xml"})
        self.assertTrue(self.store.verify_members(result["object_id"])["intact"])

    def test_a_unit_tree_inside_the_store_is_refused(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.fetch("unit-a", fetcher)
        with self.assertRaisesRegex(StoreError, "inside the download store"):
            self.store.materialize("unit-a", self.store.root / "units" / "a", [{"url": URL, "target": "x.mzML"}])

    def test_materializing_needs_a_fetched_claim(self) -> None:
        self.store.claim(URL, "unit-a", source="batch_plan")
        with self.assertRaisesRegex(StoreError, "fetch_or_reuse first"):
            self.materialize("unit-a")


class UnlinkTreeTests(StoreTestCase):
    def linked_unit(self) -> tuple[dict, Path]:
        fetcher = FakeFetcher({URL: V1})
        result = self.fetch("unit-a", fetcher)
        self.materialize("unit-a")
        return result, self.unit_data("unit-a").parent

    def test_removing_a_multiply_linked_file_leaves_the_store_object_intact(self) -> None:
        result, raw = self.linked_unit()
        (raw / "data" / "sample1_2026930120.dcl").write_bytes(b"unit-only")

        removal = unlink_tree(raw)

        self.assertTrue(removal["complete"])
        self.assertEqual(1, removal["removed_links"])
        self.assertEqual(1, removal["removed_files"])
        self.assertEqual(len(b"unit-only"), removal["removed_bytes"], "only the unit's own bytes are freed")
        self.assertFalse(raw.exists())
        store_file = self.object_file(result)
        self.assertEqual(V1, store_file.read_bytes())
        self.assertEqual(1, os.stat(store_file).st_nlink)
        self.assertTrue(self.store.verify_members(result["object_id"])["intact"])

    @unittest.skipUnless(WINDOWS, "the read-only attribute is shared by NTFS hardlinks")
    def test_a_read_only_shared_file_is_unlinked_without_clearing_its_attribute(self) -> None:
        result, raw = self.linked_unit()
        store_file = self.object_file(result)
        os.chmod(store_file, stat.S_IREAD)
        link = raw / "data" / "sample1.mzML"
        self.assertTrue(os.stat(link).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY, "one record, one attribute")

        removal = unlink_tree(raw)

        self.assertTrue(removal["complete"], removal["kept"])
        self.assertFalse(link.exists())
        self.assertTrue(
            os.stat(store_file).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY,
            "clearing the attribute to delete the link would have cleared it on the store object",
        )
        self.assertEqual(V1, store_file.read_bytes())
        self.assertTrue(self.store.verify_members(result["object_id"])["intact"])

    def test_a_read_only_file_with_one_link_is_the_trees_own_and_is_removed(self) -> None:
        tree = Path(self.directory.name) / "own"
        tree.mkdir()
        own = tree / "method.txt"
        own.write_bytes(b"own")
        os.chmod(own, stat.S_IREAD)
        removal = unlink_tree(tree)
        self.assertTrue(removal["complete"], removal["kept"])
        self.assertEqual(1, removal["removed_files"])

    @unittest.skipUnless(WINDOWS, "Windows refuses to delete a file held open without delete sharing")
    def test_a_busy_file_is_kept_and_reported(self) -> None:
        result, raw = self.linked_unit()
        link = raw / "data" / "sample1.mzML"
        with link.open("rb"):
            removal = unlink_tree(raw)
        self.assertFalse(removal["complete"])
        self.assertEqual("busy", removal["kept"][0]["reason"])
        self.assertEqual(1, removal["kept_count"], "its directories are not reported again")
        self.assertTrue(link.exists())
        self.assertTrue(unlink_tree(raw)["complete"])
        self.assertEqual(V1, self.object_file(result).read_bytes())

    @unittest.skipUnless(WINDOWS, "directory junctions are a Windows reparse point")
    def test_a_junction_is_removed_and_never_followed(self) -> None:
        import _winapi

        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})
        result = self.fetch("unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", extract=ZipExtractor())
        raw = self.unit_data("unit-a").parent
        raw.mkdir(parents=True)
        _winapi.CreateJunction(str(Path(result["tree_path"])), str(raw / "junction"))

        removal = unlink_tree(raw)

        self.assertTrue(removal["complete"], removal["kept"])
        self.assertEqual(1, removal["removed_reparse_points"])
        self.assertEqual(_payload("run-one|"), (Path(result["tree_path"]) / "run1.mzML").read_bytes())

    def test_a_filesystem_root_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            unlink_tree(Path(self.directory.name).anchor)


class ClaimTests(StoreTestCase):
    def test_a_claim_is_idempotent(self) -> None:
        first = self.store.claim(URL, "unit-a", source="batch_plan")
        second = self.store.claim(URL, "unit-a")
        self.assertEqual(first["claimed_at"], second["claimed_at"])
        self.assertEqual("batch_plan", second["claimed_by"]["source"])
        self.assertEqual(1, len(list((self.store.root / "claims").rglob("*.json"))))

    def test_a_released_claim_can_be_reopened_and_keeps_its_history(self) -> None:
        self.store.claim(URL, "unit-a")
        self.store.release(URL, "unit-a", "excluded")
        reopened = self.store.claim(URL, "unit-a")
        self.assertEqual("pending", reopened["state"])
        self.assertEqual("excluded", reopened["history"][0]["release_reason"])

    def test_an_unknown_release_reason_is_refused(self) -> None:
        self.store.claim(URL, "unit-a")
        with self.assertRaisesRegex(StoreError, "Unknown release reason"):
            self.store.release(URL, "unit-a", "finished")

    def test_unit_ids_that_differ_only_in_case_get_separate_claims(self) -> None:
        """NTFS folds case, so a readable file name is used only for lower-case ids."""
        self.store.claim(URL, "unit-a")
        self.store.claim(URL, "UNIT-A")
        claims = [self.store.read_claim(URL, "unit-a"), self.store.read_claim(URL, "UNIT-A")]
        self.assertEqual({"unit-a", "UNIT-A"}, {claim["unit_id"] for claim in claims})
        self.assertEqual(2, len(list((self.store.root / "claims").rglob("*.json"))))


class GarbageCollectionTests(StoreTestCase):
    def consume(self, unit_id: str, fetcher) -> dict:
        result = self.fetch(unit_id, fetcher)
        self.materialize(unit_id)
        return result

    def clean(self, unit_id: str, authorization="the campaign's") -> dict:
        self.assertTrue(unlink_tree(self.unit_data(unit_id).parent)["complete"])
        if authorization == "the campaign's":
            authorization = self.authorization()
        return self.store.release_unit(unit_id, "raw_cleaned", authorization=authorization)

    def test_a_pending_claim_keeps_the_object_after_another_unit_is_cleaned(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        self.store.claim(URL, "unit-b", source="batch_plan")  # approved, not started

        cleaned = self.clean("unit-a")

        self.assertEqual([], cleaned["gc"]["collected"])
        self.assertEqual(["unit-b"], cleaned["gc"]["kept"][0]["live_claims"])
        self.assertTrue(self.object_file(result).exists())

    def test_the_last_release_collects_the_object_and_leaves_a_tombstone(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        self.store.claim(URL, "unit-b", source="batch_plan")
        self.clean("unit-a")
        self.consume("unit-b", fetcher)

        cleaned = self.clean("unit-b")

        self.assertEqual(1, len(fetcher.calls))
        self.assertEqual("collected", cleaned["gc"]["collected"][0]["state"])
        directory = self.store.object_directory(result["object_id"])
        self.assertFalse((directory / "obj").exists())
        self.assertTrue((directory / "members.tsv").exists(), "the member listing stays as evidence")
        tombstone = self.store.entry(result["object_id"])
        self.assertEqual("collected", tombstone["state"])
        self.assertEqual("approval-synthetic-1", tombstone["collected_under"]["approval_id"])
        self.assertRegex(tombstone["collected_under"]["authorization_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(5, tombstone["collected_under"]["boundary"])
        self.assertEqual(
            [{"unit_id": "unit-a", "covered_as": "listed"}, {"unit_id": "unit-b", "covered_as": "listed"}],
            tombstone["collected_under"]["units"],
        )
        self.assertEqual(
            {("unit-a", "raw_cleaned"), ("unit-b", "raw_cleaned")},
            {(item["unit_id"], item["release_reason"]) for item in tombstone["release_record"]},
        )
        self.assertEqual(len(V1), tombstone["collected_bytes"])

    def test_retention_keep_never_deletes(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        cleaned = self.clean("unit-a", authorization=self.authorization(KEEP))
        self.assertFalse(cleaned["gc"]["authorized"])
        self.assertIn("keep", cleaned["gc"]["reason"])
        self.assertEqual(["retention_keep"], cleaned["gc"]["refusal_codes"])
        self.assertTrue(self.object_file(result).exists())
        self.assertFalse(self.store.gc(self.authorization(KEEP))["collected"])

    def test_nothing_is_deleted_without_a_campaign_authorization(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        cleaned = self.clean("unit-a", authorization=None)
        self.assertIsNone(cleaned["gc"])
        self.assertFalse(self.store.gc(None)["authorized"])
        self.assertTrue(self.object_file(result).exists())

    def test_a_failed_units_live_claim_keeps_its_object(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)  # the run then failed; its claim stays live
        collected = self.store.gc(self.authorization())
        self.assertEqual([], collected["collected"])
        self.assertEqual(["unit-a"], collected["kept"][0]["live_claims"])
        self.assertTrue(self.object_file(result).exists())

    def test_a_collected_object_is_fetched_again_for_a_new_consumer(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        self.clean("unit-a")

        again = self.fetch("unit-c", fetcher)

        self.assertEqual(2, len(fetcher.calls))
        self.assertEqual("refetched", again["action"])
        self.assertEqual(result["object_id"], again["object_id"])
        entry = self.store.entry(again["object_id"])
        self.assertEqual("ready", entry["state"])
        self.assertEqual("collected", entry["collections"][0]["state"])
        self.assertEqual("unit-c", entry["fetched_by"]["unit_id"])

    def test_an_authorization_is_read_from_its_file_and_a_malformed_one_is_refused(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.consume("unit-a", fetcher)
        path = self.authorization()
        malformed = (
            {**AUTHORIZATION, "schema": "something-else"},
            {**AUTHORIZATION, "approval_id": ""},
            {key: value for key, value in AUTHORIZATION.items() if key != "raw_retention_policy"},
            {**AUTHORIZATION, "covers": [1, 3, 4, 5, 6]},
        )
        for record in malformed:
            with self.subTest(record=record), self.assertRaisesRegex(StoreError, "^campaign_authorization_refused"):
                self.store.gc(self.authorization(record))
        self.assertTrue(unlink_tree(self.unit_data("unit-a").parent)["complete"])
        self.store.release_unit("unit-a", "raw_cleaned")
        self.assertEqual(1, len(self.store.gc(path)["collected"]))

    def test_an_object_whose_lock_is_held_is_left_for_a_later_pass(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        self.assertTrue(unlink_tree(self.unit_data("unit-a").parent)["complete"])
        self.store.release_unit("unit-a", "raw_cleaned")
        for name in (f"u-{self.store.url_key(URL)}", f"c-{self.store.url_key(URL)}"):
            with self.subTest(lock=name):
                handle = self.store.lock(name).acquire()
                try:
                    collected = self.store.gc(self.authorization())
                finally:
                    handle.release()
                self.assertEqual([result["object_id"]], collected["busy"])
                self.assertTrue(self.object_file(result).exists())

    def test_orphan_partials_are_removed_and_claimed_ones_kept(self) -> None:
        orphan = "https://repository.example.org/studies/ST000001/abandoned.zip"
        claimed = "https://repository.example.org/studies/ST000001/in-progress.zip"
        unclaimed = "https://repository.example.org/studies/ST000001/nobody-claimed.zip"
        for url in (orphan, claimed, unclaimed):
            partial = self.store.partial_path(self.store.url_key(url)).with_name(f"{self.store.url_key(url)}.part")
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"half an object")
        self.store.claim(orphan, "unit-x")
        self.store.release(orphan, "unit-x", "excluded")
        self.store.claim(claimed, "unit-b")

        collected = self.store.gc(self.authorization())

        self.assertEqual([f"{self.store.url_key(orphan)}.part"], collected["partials_removed"])
        kept = self.store.partial_path(self.store.url_key(claimed))
        self.assertTrue(kept.with_name(f"{kept.name}.part").exists())
        self.assertEqual(
            [{"url_key": self.store.url_key(unclaimed), "units": {"": ["no_releasing_unit"]}}],
            collected["partials_kept"],
            "a transfer no unit ever claimed has no unit an approval could name",
        )

    def test_the_summary_names_who_keeps_each_object(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.consume("unit-a", fetcher)
        self.store.claim(URL, "unit-b", source="batch_plan")
        summary = self.store.summary()
        self.assertEqual(["unit-a", "unit-b"], summary["objects"][0]["live_claims"])
        self.assertEqual({"materialized": 1, "pending": 1}, summary["claims"])


class AuthorizationScopeTests(StoreTestCase):
    """GC deletes raw bytes only as far as the campaign approval covers boundary 5 for their units."""

    def released_object(self, *units: str) -> dict:
        fetcher = FakeFetcher({URL: V1})
        result: dict = {}
        for unit in units or ("unit-a",):
            result = self.fetch(unit, fetcher)
            self.store.release_unit(unit, "raw_cleaned")
        return result

    def assert_kept(self, result: dict) -> None:
        self.assertTrue(self.object_file(result).exists())
        self.assertEqual("ready", self.store.entry(result["object_id"])["state"])

    def test_a_revoked_approval_deletes_nothing(self) -> None:
        result = self.released_object()
        collected = self.store.gc(self.authorization(revoked_at="2026-09-30T01:00:00+00:00"))
        self.assertFalse(collected["authorized"])
        self.assertEqual(["revoked"], collected["refusal_codes"])
        self.assertIn("revoked", collected["reason"])
        self.assertEqual([], collected["collected"])
        self.assert_kept(result)

    def test_an_approval_that_does_not_cover_boundary_5_deletes_nothing(self) -> None:
        result = self.released_object()
        collected = self.store.gc(self.authorization(covers=[1, 3, 4, "split"]))
        self.assertFalse(collected["authorized"])
        self.assertEqual(["boundary_not_covered"], collected["refusal_codes"])
        self.assert_kept(result)

    def test_an_approval_for_other_units_does_not_delete_this_units_object(self) -> None:
        result = self.released_object()
        collected = self.store.gc(self.authorization(units=["some-other-unit"]))
        self.assertEqual([], collected["collected"])
        self.assertEqual(
            [{"object_id": result["object_id"], "units": {"unit-a": ["unit_not_covered"]}}], collected["refused"]
        )
        self.assert_kept(result)

    def test_every_unit_whose_release_freed_the_object_must_be_covered(self) -> None:
        result = self.released_object("unit-a", "unit-outside-the-approval")
        collected = self.store.gc(self.authorization())
        self.assertEqual({"unit-outside-the-approval": ["unit_not_covered"]}, collected["refused"][0]["units"])
        self.assert_kept(result)

    def test_an_object_no_unit_claimed_is_kept(self) -> None:
        result = self.released_object()
        for path in (self.store.root / "claims").rglob("*.json"):
            path.unlink()
        collected = self.store.gc(self.authorization())
        self.assertEqual({"": ["no_releasing_unit"]}, collected["refused"][0]["units"])
        self.assert_kept(result)

    def test_a_bom_prefixed_record_is_read_as_the_campaign_authorization_reads_it(self) -> None:
        self.released_object()
        collected = self.store.gc(self.authorization(prefix=b"\xef\xbb\xbf"))
        self.assertEqual(1, len(collected["collected"]))

    def test_a_record_built_in_memory_is_not_an_authorization(self) -> None:
        result = self.released_object()
        for value in (dict(AUTHORIZATION), json.dumps(AUTHORIZATION).encode("utf-8")):
            with self.subTest(kind=type(value).__name__), self.assertRaisesRegex(StoreError, "path to its record"):
                self.store.gc(value)
        self.assert_kept(result)

    def test_a_loaded_record_is_accepted_and_the_tombstone_holds_its_files_digest(self) -> None:
        result = self.released_object()
        path = self.authorization()
        collected = self.store.gc(CampaignAuthorization.load(path))
        self.assertEqual(1, len(collected["collected"]))
        under = self.store.entry(result["object_id"])["collected_under"]
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), under["authorization_sha256"])
        self.assertEqual(("campaign-synthetic", "sha256:" + "0" * 64), (under["campaign_id"], under["manifest_digest"]))
        self.assertNotIn(path.name, json.dumps(self.store.entry(result["object_id"])), "named by digest, not location")


class DeclaredChecksumBeforeExtractionTests(StoreTestCase):
    """As the per-unit lease did, fetched bytes meet their declared MD5 before any extractor sees them."""

    def test_a_damaged_archive_fails_on_its_md5_and_never_reaches_the_extractor(self) -> None:
        damaged = b"PK\x03\x04" + b"\x00" * 5000
        fetcher = FakeFetcher({ARCHIVE_URL: damaged})
        seen: list[Path] = []

        def extractor(archive: Path, destination: Path) -> dict:
            seen.append(archive)
            with zipfile.ZipFile(archive) as handle:
                handle.extractall(destination)
            return {}

        declared = _md5(ARCHIVE)
        for attempt in range(4):
            with self.subTest(attempt=attempt), self.assertRaisesRegex(
                DeclaredChecksumMismatch, r"^MD5 checksum mismatch for ST000001\.zip\."
            ):
                self.fetch(
                    "unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", declared_md5=declared, extract=extractor
                )

        self.assertEqual([], seen, "unverified bytes were never handed to the extractor")
        self.assertEqual(2, len(fetcher.calls), "a second fetch confirms the bytes; later retries download nothing")
        memo = self.store.lookup(ARCHIVE_URL)["refuted_declared_md5"][declared]
        self.assertEqual((2, True), (memo["fetches"], memo["refuted"]))
        object_id = _sha256(damaged)[:16]
        self.assertFalse((self.store.object_directory(object_id) / "t").exists())
        self.assertNotIn("extraction_failures", self.store.entry(object_id))
        self.assertIn("MD5 checksum mismatch", self.store.read_claim(ARCHIVE_URL, "unit-a")["last_error"]["message"])

    def test_an_error_page_saved_under_an_archive_name_fails_on_its_md5(self) -> None:
        fetcher = FakeFetcher({ARCHIVE_URL: b"<html>503 Service Unavailable</html>"})
        extractor = ZipExtractor()
        with self.assertRaises(DeclaredChecksumMismatch):
            self.fetch(
                "unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", declared_md5=_md5(ARCHIVE), extract=extractor
            )
        self.assertEqual(0, extractor.calls)

    def test_a_matching_declaration_is_extracted(self) -> None:
        fetcher = FakeFetcher({ARCHIVE_URL: ARCHIVE})
        extractor = ZipExtractor()
        result = self.fetch(
            "unit-a", fetcher, url=ARCHIVE_URL, name="ST000001.zip", declared_md5=_md5(ARCHIVE), extract=extractor
        )
        self.assertTrue(result["declared_checksum_verified"])
        self.assertEqual(1, extractor.calls)
        self.assertTrue((Path(result["tree_path"]) / "sub" / "run2.mzML").is_file())


V1_DAMAGED = b"X" + V1[1:]  # one byte off, at the published size: what a resume across two versions leaves


class TransientCorruptionTests(StoreTestCase):
    """One bad transfer must not refute a correct declaration; two agreeing fetches do."""

    def test_one_damaged_transfer_does_not_fail_the_unit_for_good(self) -> None:
        for with_head in (False, True):
            with self.subTest(with_head=with_head):
                store = DownloadStore(self.workspace / str(with_head), REPOSITORY, ACCESSION, lock_poll_seconds=0.02)
                fetcher = (
                    HeadFetcher({URL: V1_DAMAGED}, {URL: len(V1)}) if with_head else FakeFetcher({URL: V1_DAMAGED})
                )
                with self.assertRaisesRegex(DeclaredChecksumMismatch, "fetches it once more"):
                    store.fetch_or_reuse(URL, "sample1.mzML", unit_id="unit-a", fetcher=fetcher, declared_md5=_md5(V1))
                memo = store.lookup(URL)["refuted_declared_md5"][_md5(V1)]
                self.assertEqual((1, False), (memo["fetches"], memo["refuted"]))
                fetcher.payloads[URL] = V1

                retried = store.fetch_or_reuse(
                    URL, "sample1.mzML", unit_id="unit-a", fetcher=fetcher, declared_md5=_md5(V1)
                )

                self.assertTrue(retried["declared_checksum_verified"])
                self.assertEqual("declared_md5_differs", retried["reuse_reason"])
                self.assertEqual(2, len(fetcher.calls))
                self.assertNotIn("refuted_declared_md5", store.lookup(URL), "a satisfied declaration leaves no memo")

    def test_a_consumer_without_a_declaration_does_not_reuse_disputed_bytes(self) -> None:
        fetcher = FakeFetcher({URL: V1_DAMAGED})
        with self.assertRaises(DeclaredChecksumMismatch):
            self.fetch("unit-a", fetcher, declared_md5=_md5(V1))
        fetcher.payloads[URL] = V1

        second = self.fetch("unit-b", fetcher)

        self.assertEqual(("refetched", "declared_md5_disputed"), (second["action"], second["reuse_reason"]))
        self.assertEqual(V1, self.object_file(second).read_bytes())
        again = self.fetch("unit-a", fetcher, declared_md5=_md5(V1))
        self.assertEqual("reused", again["action"])
        self.assertEqual(2, len(fetcher.calls))

    def test_a_dispute_a_second_fetch_confirms_refutes_the_declaration(self) -> None:
        fetcher = FakeFetcher({URL: V1})  # the declaration is the wrong one
        with self.assertRaises(DeclaredChecksumMismatch):
            self.fetch("unit-a", fetcher, declared_md5=_md5(V2))

        self.assertEqual("declared_md5_disputed", self.fetch("unit-b", fetcher)["reuse_reason"])
        self.assertEqual("reused", self.fetch("unit-c", fetcher)["action"])
        with self.assertRaisesRegex(DeclaredChecksumMismatch, "not downloaded again"):
            self.fetch("unit-a", fetcher, declared_md5=_md5(V2))
        self.assertEqual(2, len(fetcher.calls))

    def test_force_refetch_overrides_a_refuted_declaration(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        for _ in range(3):
            with self.assertRaises(DeclaredChecksumMismatch):
                self.fetch("unit-a", fetcher, declared_md5=_md5(V2))
        self.assertEqual(2, len(fetcher.calls))
        fetcher.payloads[URL] = V2

        forced = self.fetch("unit-a", fetcher, declared_md5=_md5(V2), force_refetch=True)

        self.assertEqual(3, len(fetcher.calls))
        self.assertEqual(("force_refetch", "refetch_forced"), (forced["reuse_reason"], forced["reuse_check"]["cache"]))
        self.assertTrue(forced["declared_checksum_verified"])


class MisleadingHeadTests(StoreTestCase):
    """HEAD support is unverified for Workbench, MetaboLights and MetaboBank; a wrong one must not multiply GETs."""

    def test_a_head_size_a_refetch_showed_to_mislead_does_not_refetch_every_consumer(self) -> None:
        fetcher = HeadFetcher({URL: V1}, {URL: len(V1) + 123})

        results = [self.fetch(unit, fetcher) for unit in ("unit-a", "unit-b", "unit-c", "unit-d")]

        self.assertEqual(2, len(fetcher.calls), "one fetch, and one refetch that shows the bytes unchanged")
        self.assertEqual(["fetched", "refetched", "reused", "reused"], [result["action"] for result in results])
        self.assertEqual("differs_as_before", results[3]["reuse_check"]["remote_size"])
        recorded = self.store.lookup(URL)["head_size_mismatch"]
        self.assertEqual(
            (results[0]["object_id"], len(V1) + 123, len(V1)),
            (recorded["object_id"], recorded["head_content_length"], recorded["object_size_bytes"]),
        )

    def test_a_new_head_size_still_fetches_again(self) -> None:
        fetcher = HeadFetcher({URL: V1}, {URL: len(V1) + 123})
        self.fetch("unit-a", fetcher)
        self.fetch("unit-b", fetcher)
        fetcher.payloads[URL] = V2
        fetcher.head_sizes[URL] = len(V2)

        changed = self.fetch("unit-c", fetcher)

        self.assertEqual("remote_size_changed", changed["reuse_reason"])
        self.assertEqual(V2, self.object_file(changed).read_bytes())
        self.assertEqual(3, len(fetcher.calls))
        self.assertNotIn("head_size_mismatch", self.store.lookup(URL), "it named bytes the URL no longer serves")


class DirectoryLengthTests(StoreTestCase):
    """CreateDirectoryW and .NET Framework refuse directories of 248 characters or more."""

    def source_file(self) -> Path:
        source = Path(self.directory.name) / "s"
        source.mkdir(exist_ok=True)
        (source / "f").write_bytes(b"x")
        return source / "f"

    def test_a_long_directory_with_a_short_file_name_is_refused_before_anything_is_written(self) -> None:
        source = self.source_file()
        root = Path(self.directory.name) / "u"
        base = len(str(root.resolve())) + 1
        for directory_length in (248, 252, 257):
            with self.subTest(directory_length=directory_length):
                name = "d" * (directory_length - base)
                self.assertLessEqual(directory_length + 2, 259, "the file itself is within MAX_PATH")
                with self.assertRaises(MaterializationCollision) as caught:
                    materialize_unit_tree(root, [(source, f"{name}/f")], max_path_length=259, max_directory_length=247)
                self.assertEqual(
                    [{"path": name, "kind": "path_too_long", "length": directory_length, "directory": True}],
                    caught.exception.collisions,
                )
                self.assertFalse(root.exists(), "nothing was written")

    def test_a_root_past_the_directory_limit_is_refused(self) -> None:
        source = self.source_file()
        parent = Path(self.directory.name).resolve()
        root = parent / ("r" * (250 - len(str(parent)) - 1))
        with self.assertRaises(MaterializationCollision) as caught:
            materialize_unit_tree(root, [(source, "f")], max_path_length=259, max_directory_length=247)
        collision = caught.exception.collisions[0]
        self.assertEqual(("", "path_too_long", 250), (collision["path"], collision["kind"], collision["length"]))
        self.assertFalse(root.exists())

    @unittest.skipUnless(WINDOWS, "MAX_PATH is a Windows limit")
    def test_the_windows_defaults_refuse_what_createdirectory_would(self) -> None:
        source = self.source_file()
        root = Path(self.directory.name) / "u"
        name = "d" * (252 - len(str(root.resolve())) - 1)
        with self.assertRaises(MaterializationCollision):
            materialize_unit_tree(root, [(source, f"{name}/f")])

    def test_the_store_passes_the_limit_through_and_records_the_refusal(self) -> None:
        self.fetch("unit-a", FakeFetcher({URL: V1}))
        limit = len(str(self.unit_data("unit-a").resolve())) + 2
        with self.assertRaises(MaterializationCollision):
            self.materialize("unit-a", target="sub/sample1.mzML", max_directory_length=limit)
        self.assertIn("path_too_long", self.store.read_claim(URL, "unit-a")["last_error"]["message"])
        self.assertFalse(self.unit_data("unit-a").exists())


class ClaimLockTests(StoreTestCase):
    """GC decides under the URL's claim lock; a claim never waits for a transfer."""

    def test_a_claim_written_while_gc_decides_is_never_deleted_from_under(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.fetch("unit-a", fetcher)
        self.store.release_unit("unit-a", "raw_cleaned")
        original = DownloadStore.live_claims
        race: dict = {}

        def racing(store: DownloadStore, object_id: str) -> list:
            live = original(store, object_id)
            # A batch pre-claim from the runner process, arriving just after GC read the claims.
            thread = threading.Thread(
                target=lambda: race.setdefault("claim", self.store.claim(URL, "unit-b", source="batch_plan"))
            )
            thread.start()
            thread.join(0.5)
            race["landed_while_gc_decided"] = not thread.is_alive()
            race["thread"] = thread
            return live

        with mock.patch.object(DownloadStore, "live_claims", racing):
            self.store.gc(self.authorization())
        race["thread"].join(10)

        self.assertFalse(race["landed_while_gc_decided"], "the claim waited for GC's claim lock")
        tombstone = self.store.entry(result["object_id"])
        self.assertEqual("collected", tombstone["state"])
        self.assertNotIn("unit-b", {row["unit_id"] for row in tombstone["release_record"]})
        self.assertGreaterEqual(race["claim"]["claimed_at"], tombstone["collected_at"])
        self.assertEqual("refetched", self.fetch("unit-b", fetcher)["action"])

    def test_a_pre_claim_does_not_wait_for_a_transfer_in_progress(self) -> None:
        transfer = self.store.lock(f"u-{self.store.url_key(URL)}").acquire()
        claimer = threading.Thread(target=self.store.claim, args=(URL, "unit-b"), kwargs={"source": "batch_plan"})
        try:
            claimer.start()
            claimer.join(5)
            self.assertFalse(claimer.is_alive(), "a pre-claim waited for the transfer lock")
        finally:
            transfer.release()
            claimer.join(10)
        self.assertEqual("pending", self.store.read_claim(URL, "unit-b")["state"])


class LeaseMaterializationTests(StoreTestCase):
    """What the lease asks of materialization beyond links: the per-unit merge's rules, and a protected copy."""

    def sources(self, **files: bytes) -> dict[str, Path]:
        root = Path(self.directory.name) / "sources"
        paths = {}
        for name, data in files.items():
            path = root / name / "README.txt"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            paths[name] = path.parent
        return paths

    def test_a_second_source_with_the_same_bytes_is_not_placed_again(self) -> None:
        sources = self.sources(first=b"same notes", second=b"same notes")
        placed: dict = {}

        record = materialize_unit_tree(
            self.unit_data("unit-a"), [(sources["first"], ""), (sources["second"], "")],
            same_content=lambda left, right: left.read_bytes() == right.read_bytes(), placed=placed,
        )

        self.assertEqual((1, 1), (record["linked_files"], record["duplicate_source_files"]))
        self.assertTrue(os.path.samefile(self.unit_data("unit-a") / "README.txt", sources["first"] / "README.txt"))
        self.assertEqual([{"source": str(sources["second"] / "README.txt"), "pair": 1}], placed["README.txt"]["duplicates"])
        with self.assertRaises(MaterializationCollision):
            materialize_unit_tree(self.unit_data("unit-b"), [(sources["first"], ""), (sources["second"], "")])

    def test_a_file_already_there_with_its_sources_bytes_is_kept_not_refused(self) -> None:
        sources = self.sources(first=b"notes")
        own = self.unit_data("unit-a") / "README.txt"
        own.parent.mkdir(parents=True)
        own.write_bytes(b"notes")
        placed: dict = {}

        record = materialize_unit_tree(
            self.unit_data("unit-a"), [(sources["first"], "")],
            same_content=lambda left, right: left.read_bytes() == right.read_bytes(), placed=placed,
        )

        self.assertEqual((1, len(b"notes"), "kept_existing"), (record["kept_existing_files"],
                                                               record["bytes_kept_existing"], record["materialization"]))
        self.assertEqual("kept_existing", placed["README.txt"]["how"])
        self.assertEqual(1, os.stat(own).st_nlink, "the unit's own file is left as it is")

    def test_copy_instead_keeps_a_file_a_reader_rewrites_off_the_stores_record(self) -> None:
        sources = self.sources(first=b"sqlite")

        record = materialize_unit_tree(
            self.unit_data("unit-a"), [(sources["first"], "S1.d")], copy_instead=lambda source: True,
        )

        copied = self.unit_data("unit-a") / "S1.d" / "README.txt"
        self.assertFalse(os.path.samefile(copied, sources["first"] / "README.txt"))
        self.assertEqual((1, ["S1.d/README.txt"], "copy"), (record["protected_copies"],
                                                             record["protected_copy_paths"], record["materialization"]))
        self.assertEqual(0, record["copy_fallback_count"], "a protected copy is no failed link")


class EnsureExtractedTests(StoreTestCase):
    def test_a_fetched_archive_is_extracted_once_and_its_tree_reused(self) -> None:
        extractor = ZipExtractor()
        result = self.fetch("unit-a", FakeFetcher({ARCHIVE_URL: ARCHIVE}), url=ARCHIVE_URL, name="ST000001.zip")

        first = self.store.ensure_extracted(result["object_id"], extractor, unit_id="unit-a")
        second = self.store.ensure_extracted(result["object_id"], extractor, unit_id="unit-b")

        self.assertEqual(("extracted", "reused", 1), (first["extraction_action"], second["extraction_action"], extractor.calls))
        self.assertEqual("unit-a", second["tree"]["extracted_by"]["unit_id"])

    def test_a_tree_a_reader_modified_is_extracted_again(self) -> None:
        extractor = ZipExtractor()
        result = self.fetch("unit-a", FakeFetcher({ARCHIVE_URL: ARCHIVE}), url=ARCHIVE_URL, name="ST000001.zip")
        self.store.ensure_extracted(result["object_id"], extractor, unit_id="unit-a")
        member = self.store.object_directory(result["object_id"]) / "t" / "run1.mzML"
        member.write_bytes(b"rewritten in place")

        again = self.store.ensure_extracted(result["object_id"], extractor, unit_id="unit-b")

        self.assertEqual(("re-extracted", 2), (again["extraction_action"], extractor.calls))
        self.assertEqual(_payload("run-one|"), member.read_bytes())
        self.assertTrue(self.store.verify_members(result["object_id"])["intact"])

    def test_an_object_that_is_not_ready_is_refused(self) -> None:
        with self.assertRaises(StoreError):
            self.store.ensure_extracted("0" * 16, ZipExtractor(), unit_id="unit-a")

    def test_all_claims_lists_every_unit_and_url(self) -> None:
        self.store.claim(URL, "unit-a")
        self.store.claim(ARCHIVE_URL, "unit-b", source="batch_plan")

        claims = self.store.all_claims()
        self.assertEqual({("unit-a", URL), ("unit-b", ARCHIVE_URL)}, {(item["unit_id"], item["url"]) for item in claims})


class StorePathTests(unittest.TestCase):
    def test_the_store_is_scoped_to_one_accession_beside_its_units(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = DownloadStore(directory, REPOSITORY, ACCESSION)
            self.assertEqual(Path(directory).resolve() / REPOSITORY / ACCESSION / "_dl", store.root)

    def test_unsafe_path_components_are_refused(self) -> None:
        unsafe = (("..", ACCESSION), (REPOSITORY, "a/b"), (REPOSITORY, "CON"), (REPOSITORY, "ST1."), ("", ACCESSION))
        for repository, accession in unsafe:
            with self.subTest(repository=repository, accession=accession), self.assertRaises(StoreError):
                DownloadStore(tempfile.gettempdir(), repository, accession)


if __name__ == "__main__":
    unittest.main()
