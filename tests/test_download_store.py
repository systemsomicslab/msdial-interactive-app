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
- a linked file written in place is detected, and the object is fetched or extracted again;
- an object is deleted only when no claim keeps it and a campaign authorization says delete, and
  it leaves a tombstone;
- a failed link falls back to a copy that is recorded as such;
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

from msdial_app import download_store
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
AUTHORIZATION = {
    "schema": "msdial-campaign-authorization.v1",
    "approval_id": "approval-synthetic-1",
    "raw_retention_policy": "delete_after_validated_output",
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
            self.assertEqual((False, None), download_store._windows_process_probe(child.pid))
            alive, created = download_store._windows_process_probe(os.getpid())
            self.assertIs(True, alive)
            self.assertAlmostEqual(process_created_at(), created, delta=1.0)

    @unittest.skipUnless(WINDOWS, "the native probe is the Windows path")
    def test_the_native_probe_agrees_with_psutil(self) -> None:
        try:
            import psutil
        except ImportError:
            self.skipTest("psutil is not installed")
        alive, created = download_store._windows_process_probe(os.getpid())
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
        self.assertFalse(self.store.gc(AUTHORIZATION)["partials_removed"], "a claimed partial is kept for the resume")


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

    def clean(self, unit_id: str, authorization=AUTHORIZATION) -> dict:
        self.assertTrue(unlink_tree(self.unit_data(unit_id).parent)["complete"])
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
        self.assertEqual(
            {("unit-a", "raw_cleaned"), ("unit-b", "raw_cleaned")},
            {(item["unit_id"], item["release_reason"]) for item in tombstone["release_record"]},
        )
        self.assertEqual(len(V1), tombstone["collected_bytes"])

    def test_retention_keep_never_deletes(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        cleaned = self.clean("unit-a", authorization=KEEP)
        self.assertFalse(cleaned["gc"]["authorized"])
        self.assertIn("keep", cleaned["gc"]["reason"])
        self.assertTrue(self.object_file(result).exists())
        self.assertFalse(self.store.gc(KEEP)["collected"])

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
        collected = self.store.gc(AUTHORIZATION)
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
        path = Path(self.directory.name) / "campaign-authorization.json"
        path.write_text(json.dumps(AUTHORIZATION), encoding="utf-8")
        for malformed in (
            {"schema": "something-else", "approval_id": "x", "raw_retention_policy": "delete_after_validated_output"},
            {**AUTHORIZATION, "approval_id": ""},
            {key: value for key, value in AUTHORIZATION.items() if key != "raw_retention_policy"},
        ):
            with self.subTest(malformed=malformed), self.assertRaises(StoreError):
                self.store.gc(malformed)
        self.assertTrue(unlink_tree(self.unit_data("unit-a").parent)["complete"])
        self.store.release_unit("unit-a", "raw_cleaned")
        self.assertEqual(1, len(self.store.gc(path)["collected"]))

    def test_an_object_whose_lock_is_held_is_left_for_a_later_pass(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        result = self.consume("unit-a", fetcher)
        self.assertTrue(unlink_tree(self.unit_data("unit-a").parent)["complete"])
        self.store.release_unit("unit-a", "raw_cleaned")
        handle = self.store.lock(f"u-{self.store.url_key(URL)}").acquire()
        try:
            collected = self.store.gc(AUTHORIZATION)
        finally:
            handle.release()
        self.assertEqual([result["object_id"]], collected["busy"])
        self.assertTrue(self.object_file(result).exists())

    def test_orphan_partials_are_removed_and_claimed_ones_kept(self) -> None:
        orphan = "https://repository.example.org/studies/ST000001/abandoned.zip"
        claimed = "https://repository.example.org/studies/ST000001/in-progress.zip"
        for url in (orphan, claimed):
            partial = self.store.partial_path(self.store.url_key(url)).with_name(f"{self.store.url_key(url)}.part")
            partial.parent.mkdir(parents=True, exist_ok=True)
            partial.write_bytes(b"half an object")
        self.store.claim(claimed, "unit-b")

        collected = self.store.gc(AUTHORIZATION)

        self.assertEqual([f"{self.store.url_key(orphan)}.part"], collected["partials_removed"])
        kept = self.store.partial_path(self.store.url_key(claimed))
        self.assertTrue(kept.with_name(f"{kept.name}.part").exists())

    def test_the_summary_names_who_keeps_each_object(self) -> None:
        fetcher = FakeFetcher({URL: V1})
        self.consume("unit-a", fetcher)
        self.store.claim(URL, "unit-b", source="batch_plan")
        summary = self.store.summary()
        self.assertEqual(["unit-a", "unit-b"], summary["objects"][0]["live_claims"])
        self.assertEqual({"materialized": 1, "pending": 1}, summary["claims"])


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
