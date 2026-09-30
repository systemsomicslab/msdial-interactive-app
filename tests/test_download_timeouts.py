"""A repository download that stalls is timed out, retried from its .part, and stoppable.

A repository object was fetched with a 300 s socket timeout and a single attempt. A read that stalled
held the lease for five minutes and then failed it as a network error; and a cancel asked for during
the stall was heard only when a byte next arrived, which it never did, so the lease said "timed out"
where the job had been cancelled. Now every read carries an idle timeout, a stalled or lost transfer is
retried from its .part after a backoff (the If-Range rules of test_download_resume apply to each
resume), each attempt is recorded, and the progress callback, through which a job cancels, is called at
the stall and through the backoff.

A real HTTP server on localhost serves the objects, since what is tested is the conversation: a body
that stops arriving, a connection that closes early, and the resume that follows.
"""

from __future__ import annotations

import hashlib
import http.client
import http.server
import json
import ssl
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from msdial_app import repository_reanalysis
from msdial_app.repository_reanalysis import (
    DOWNLOAD_IDLE_TIMEOUT_SECONDS,
    DOWNLOAD_PROGRESS_INTERVAL_SECONDS,
    DownloadConnectionLost,
    DownloadIncomplete,
    DownloadStalled,
    RepositoryFile,
    RepositoryHttpClient,
    RetryableDownloadError,
    create_download_lease,
    download_interruption,
    read_manifest,
)

from test_download_lease_record import _project


# Larger than the client's 1 MiB read, so a stall after the first half (1.5 MiB) falls inside a MiB: the
# client once lost the bytes of that unfinished MiB and now keeps every byte that arrived.
PAYLOAD = bytes(range(256)) * (12 * 1024)          # 3 MiB, every byte position distinguishable
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
HALF = len(PAYLOAD) // 2
CHUNK = 1024 * 1024
IDLE = 0.5
ETAG = '"v1"'


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD under ETAG, doing to each request what the server's plan says next.

    stall: the headers and half the body, then nothing until released or stall_seconds pass, then a
    close. truncate: half the body and a close. serve: the object, or its tail as a 206 for a Range
    under a matching If-Range. The plan's last entry repeats.
    """

    def log_message(self, *args) -> None:  # noqa: D102 - quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        server = self.server
        with server.guard:
            action = server.plan.pop(0) if len(server.plan) > 1 else server.plan[0]
            server.requests.append({"Range": self.headers.get("Range"), "If-Range": self.headers.get("If-Range"),
                                    "action": action})
        if self.path == "/missing":
            self.send_error(404)
            return
        requested = self.headers.get("Range")
        start = 0
        if action == "serve" and requested and self.headers.get("If-Range") in (None, ETAG):
            start = int(requested.split("=", 1)[1].split("-", 1)[0])
        body = PAYLOAD[start:]
        self.send_response(206 if start else 200)
        self.send_header("ETag", ETAG)
        self.send_header("Content-Length", str(len(body)))
        if start:
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
        self.end_headers()
        if action == "serve":
            self.wfile.write(body)
            return
        self.wfile.write(body[:HALF])
        self.wfile.flush()
        if action == "stall":
            server.release.wait(server.stall_seconds)


TRICKLE_BYTES = 40          # one every 0.1 s: four seconds of body, and no read anywhere near IDLE


class _Trickle(http.server.BaseHTTPRequestHandler):
    """Declares the first TRICKLE_BYTES of PAYLOAD and sends one byte every 0.1 s: never a stall, never a MiB."""

    def log_message(self, *args) -> None:  # noqa: D102 - quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        with self.server.guard:
            self.server.requests.append({"Range": self.headers.get("Range"), "action": "trickle"})
        self.send_response(200)
        self.send_header("Content-Length", str(TRICKLE_BYTES))
        self.end_headers()
        for index in range(TRICKLE_BYTES):
            try:
                self.wfile.write(PAYLOAD[index:index + 1])
            except OSError:
                return      # the client has stopped listening
            if self.server.release.wait(0.1):
                return


class _Server:
    def __init__(self, plan: list[str], stall_seconds: float = 4.0, handler: type = _Handler) -> None:
        # Threaded, so a retry is answered while the stalled request is still held open.
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.httpd.daemon_threads = True
        self.httpd.plan = list(plan)
        self.httpd.requests = []
        self.httpd.guard = threading.Lock()
        self.httpd.release = threading.Event()
        self.httpd.stall_seconds = stall_seconds
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str = "/object.zip") -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}{path}"

    @property
    def requests(self) -> list[dict]:
        return self.httpd.requests

    def close(self) -> None:
        self.httpd.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


class Cancelled(Exception):
    def __init__(self) -> None:
        super().__init__("cancelled")


class _Downloads(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.addCleanup(self._directory.cleanup)
        self.root = Path(self._directory.name)
        self.destination = self.root / "object.zip"
        self.partial = self.destination.with_name("object.zip.part")

    def server(self, plan: list[str], stall_seconds: float = 4.0, handler: type = _Handler) -> _Server:
        server = _Server(plan, stall_seconds, handler)
        self.addCleanup(server.close)
        return server

    @staticmethod
    def client(retries: int = 0, backoff: tuple[float, ...] = (0.01,)) -> RepositoryHttpClient:
        return RepositoryHttpClient(timeout=10, idle_timeout=IDLE, retries=retries, retry_backoff_seconds=backoff)


class AStalledReadTimesOut(_Downloads):
    def test_it_raises_a_retryable_stall_within_the_idle_timeout_and_keeps_the_part(self) -> None:
        server = self.server(["stall"])
        started = time.monotonic()

        with self.assertRaises(DownloadStalled) as raised:
            self.client().download(server.url(), self.destination, 10_000_000)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, IDLE + 2.5, "not the old five minutes, nor the server's own close")
        self.assertIsInstance(raised.exception, TimeoutError, "still a TimeoutError to any older handler")
        self.assertEqual(HALF, self.partial.stat().st_size, "every byte that arrived is kept, not whole MiBs")
        self.assertTrue(self.destination.with_name("object.zip.part.json").is_file(), "and their validators")
        [attempt] = raised.exception.download_attempts
        self.assertEqual(("stalled", 0, HALF), (attempt["outcome"], attempt["part_bytes_before"],
                                                 attempt["part_bytes_after"]))
        self.assertNotIn("retry_after_seconds", attempt)

    def test_the_default_idle_timeout_is_two_minutes_and_bounds_every_request(self) -> None:
        self.assertEqual(120.0, DOWNLOAD_IDLE_TIMEOUT_SECONDS)
        client = RepositoryHttpClient()
        seen: list[object] = []

        def urlopen(request, timeout=None):
            seen.append(timeout)
            raise urllib.error.URLError("synthetic: no server")

        with patch.object(repository_reanalysis.urllib.request, "urlopen", side_effect=urlopen):
            for call in (lambda: client.get_bytes("http://synthetic.test/a"),
                         lambda: client.download("http://synthetic.test/a", self.destination, 100)):
                with self.assertRaises(urllib.error.URLError):
                    call()
            self.assertEqual(0, client.content_length("http://synthetic.test/a"))

        self.assertEqual([60, 120.0, 60], seen, "metadata under timeout, a download under the idle timeout")


class ARetryResumesFromThePart(_Downloads):
    def test_a_stalled_transfer_is_resumed_under_its_validator(self) -> None:
        server = self.server(["stall", "serve"])

        result = self.client(retries=1).download(server.url(), self.destination, 10_000_000)

        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(PAYLOAD, self.destination.read_bytes())
        self.assertEqual(HALF, result["resumed_from_bytes"])
        self.assertEqual({"Range": f"bytes={HALF}-", "If-Range": ETAG, "action": "serve"}, server.requests[-1])
        self.assertEqual(["stalled", "completed"], [item["outcome"] for item in result["attempts"]])
        self.assertEqual(0.01, result["attempts"][0]["retry_after_seconds"])
        self.assertEqual(HALF, result["attempts"][1]["part_bytes_before"])

    def test_a_connection_closed_early_is_retried(self) -> None:
        server = self.server(["truncate", "serve"])

        result = self.client(retries=1).download(server.url(), self.destination, 10_000_000)

        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(["incomplete", "completed"], [item["outcome"] for item in result["attempts"]])

    def test_the_retries_are_bounded_and_each_is_recorded(self) -> None:
        server = self.server(["stall"])

        with self.assertRaises(DownloadStalled) as raised:
            self.client(retries=2, backoff=(0.01, 0.02)).download(server.url(), self.destination, 10_000_000)

        attempts = raised.exception.download_attempts
        self.assertEqual(3, len(server.requests))
        self.assertEqual(["stalled"] * 3, [item["outcome"] for item in attempts])
        self.assertEqual([0.01, 0.02], [item.get("retry_after_seconds") for item in attempts[:2]])
        self.assertNotIn("retry_after_seconds", attempts[2])
        self.assertTrue(self.partial.is_file(), "the part is kept for the unit's own retry")

    def test_an_error_retrying_would_not_answer_is_raised_at_once(self) -> None:
        server = self.server(["serve"])

        with self.assertRaises(urllib.error.HTTPError) as raised:
            self.client(retries=3).download(server.url("/missing"), self.destination, 10_000_000)
        raised.exception.close()
        with self.assertRaises(ValueError) as limited:
            self.client(retries=3).download(server.url(), self.destination, 1000)

        self.assertEqual(["failed"], [item["outcome"] for item in raised.exception.download_attempts])
        self.assertEqual(1, len(limited.exception.download_attempts))
        self.assertNotIsInstance(limited.exception, RetryableDownloadError)
        self.assertEqual(2, len(server.requests))


class ACancelMeetsAStall(_Downloads):
    def test_it_is_heard_at_the_stall_and_raised_as_the_cancel(self) -> None:
        """THE REGRESSION. The callback was called only as bytes arrived, so the stall hid the cancel."""
        server = self.server(["stall"])
        cancel = threading.Event()

        def progress(received: int, _declared: int) -> None:
            if cancel.is_set():
                raise Cancelled()
            if received >= CHUNK:
                cancel.set()   # asked for while the rest of the body is not arriving

        started = time.monotonic()
        with self.assertRaises(Cancelled) as raised:
            self.client(retries=3, backoff=(60.0,)).download(server.url(), self.destination, 10_000_000,
                                                             progress_callback=progress)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, IDLE + 2.5, "within the idle timeout, and the backoff never began")
        [attempt] = raised.exception.download_attempts
        self.assertEqual(("stopped", "stalled", "cancelled"),
                         (attempt["outcome"], attempt["stopped_after"], attempt["error"]))
        self.assertEqual(HALF, self.partial.stat().st_size)

    def test_it_is_heard_through_the_backoff(self) -> None:
        server = self.server(["stall"])
        stalled = threading.Event()
        cancel = threading.Event()
        calls: list[int] = []

        def progress(received: int, _declared: int) -> None:
            calls.append(received)
            if cancel.is_set():
                raise Cancelled()
            if len(calls) > 1 and calls[-1] == calls[-2]:
                stalled.set()   # a report with no new bytes: the stall has been noticed

        threading.Thread(target=lambda: stalled.wait(10) and (time.sleep(0.3), cancel.set()), daemon=True).start()
        started = time.monotonic()
        with self.assertRaises(Cancelled) as raised:
            self.client(retries=3, backoff=(30.0,)).download(server.url(), self.destination, 10_000_000,
                                                             progress_callback=progress)

        self.assertLess(time.monotonic() - started, IDLE + 4.0, "not after the thirty-second backoff")
        self.assertEqual(30.0, raised.exception.download_attempts[0]["retry_after_seconds"])
        self.assertEqual("stopped", raised.exception.download_attempts[0]["outcome"])


class ASlowTransferIsStillHeard(_Downloads):
    """Bytes slower than a MiB per idle timeout never stall a read, so only the reads can carry a report.

    Each read asked for a whole MiB and urllib's read(amt) waited for all of it, so at 1 KB/s the
    callback, and through it a cancel and the lease heartbeat, was heard once in seventeen minutes, while
    every socket read came well inside the idle timeout and none timed out.
    """

    def test_a_cancel_is_heard_within_a_second_however_slowly_the_bytes_come(self) -> None:
        """THE REGRESSION. The first report, and so the cancel, came only when the body ended."""
        server = self.server(["trickle"], handler=_Trickle)
        asked = threading.Event()
        timer = threading.Timer(0.3, asked.set)
        timer.start()
        self.addCleanup(timer.cancel)

        def progress(_received: int, _declared: int) -> None:
            if asked.is_set():
                raise Cancelled()

        started = time.monotonic()
        with self.assertRaises(Cancelled) as raised:
            self.client(retries=3, backoff=(60.0,)).download(server.url(), self.destination, 10_000_000,
                                                             progress_callback=progress)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.3 + DOWNLOAD_PROGRESS_INTERVAL_SECONDS + 0.7, "not when the body ends, 4 s on")
        [attempt] = raised.exception.download_attempts
        self.assertEqual(("stopped", "cancelled"), (attempt["outcome"], attempt["error"]))
        self.assertTrue(0 < self.partial.stat().st_size < TRICKLE_BYTES, "what arrived is kept for the resume")

    def test_it_reports_at_least_once_a_second_and_ends_on_the_last_byte(self) -> None:
        server = self.server(["trickle"], handler=_Trickle)
        seen: list[tuple[float, int, int]] = []
        started = time.monotonic()

        self.client().download(server.url(), self.destination, 10_000_000,
                               progress_callback=lambda received, declared: seen.append(
                                   (time.monotonic(), received, declared)))

        self.assertEqual(PAYLOAD[:TRICKLE_BYTES], self.destination.read_bytes())
        self.assertEqual((TRICKLE_BYTES, TRICKLE_BYTES), seen[-1][1:], "the last byte is reported")
        moments = [started] + [moment for moment, _, _ in seen]
        self.assertLess(max(later - earlier for earlier, later in zip(moments, moments[1:])),
                        DOWNLOAD_PROGRESS_INTERVAL_SECONDS + 0.5)
        self.assertGreaterEqual(len(seen), 3, "four seconds of body reported through, not once at its end")


class WhatIsRetryable(unittest.TestCase):
    def test_timeouts_and_lost_connections_are(self) -> None:
        cases = {
            TimeoutError("timed out"): DownloadStalled,
            urllib.error.URLError(TimeoutError("timed out")): DownloadStalled,
            ConnectionResetError(10054, "reset"): DownloadConnectionLost,
            urllib.error.URLError(ConnectionResetError(10054, "reset")): DownloadConnectionLost,
            ConnectionAbortedError(10053, "aborted"): DownloadConnectionLost,
            http.client.RemoteDisconnected("closed"): DownloadConnectionLost,
            http.client.IncompleteRead(b"x", 10): DownloadConnectionLost,
            ssl.SSLEOFError(8, "EOF"): DownloadConnectionLost,
        }
        for error, expected in cases.items():
            with self.subTest(error=repr(error)):
                self.assertIsInstance(download_interruption(error, 120.0), expected)
        incomplete = DownloadIncomplete("short")
        self.assertIs(incomplete, download_interruption(incomplete, 120.0))

    def test_what_trying_again_would_not_answer_is_not(self) -> None:
        for error in (
            urllib.error.HTTPError("http://synthetic.test/a", 503, "Unavailable", {}, None),
            urllib.error.URLError("[Errno 11001] getaddrinfo failed"),
            urllib.error.URLError(ConnectionRefusedError(10061, "refused")),
            ValueError("Download exceeded the 1000-byte safety limit."),
            OSError(28, "No space left on device"),
        ):
            with self.subTest(error=repr(error)):
                self.assertIsNone(download_interruption(error, 120.0))
            if isinstance(error, urllib.error.HTTPError):
                error.close()


class TheLeaseRecordsTheAttempts(_Downloads):
    def lease(self, server: _Server, client: RepositoryHttpClient, progress=None) -> Path:
        files = [RepositoryFile("FILES/a.mzML", len(PAYLOAD), server.url("/a.mzML"))]
        project = _project(files, ["a.mzML"])
        self.manifest = self.root / "ws" / "metabolights" / "MTBLS-LEASE" / "unit-a" / "provenance" / "run-manifest.json"
        return create_download_lease(project, self.root / "ws", 10_000_000, client=client, progress_callback=progress)

    def test_a_retried_object_carries_its_attempts(self) -> None:
        server = self.server(["stall", "serve"])

        lease = self.lease(server, self.client(retries=1))

        [download] = read_manifest(lease["manifest_path"])["downloads"]
        self.assertEqual(SHA256, download["sha256"])
        self.assertEqual(["stalled", "completed"], [item["outcome"] for item in download["attempts"]])

    def test_a_cancel_during_a_stall_is_recorded_as_cancelled_not_as_a_timeout(self) -> None:
        server = self.server(["stall"])
        cancel = threading.Event()

        def progress(_index, _total, _name, received, _declared) -> None:
            if cancel.is_set():
                raise Cancelled()
            if received >= CHUNK:
                cancel.set()

        with self.assertRaises(Cancelled):
            self.lease(server, self.client(retries=3, backoff=(60.0,)), progress)
        failure = read_manifest(self.manifest)["download_failure"]

        self.assertEqual(("cancelled", "Cancelled", "fetch"), (failure["reason"], failure["error_type"], failure["stage"]))
        self.assertEqual(["stopped"], [item["outcome"] for item in failure["attempts"]])
        self.assertNotIn("retryable", failure)
        part = self.manifest.parents[1] / "raw" / "data" / "a.mzML.part"
        self.assertEqual(HALF, part.stat().st_size, "kept for the resume")

    def test_a_stall_past_every_retry_is_recorded_as_retryable(self) -> None:
        server = self.server(["stall"])

        with self.assertRaises(DownloadStalled):
            self.lease(server, self.client(retries=1))
        failure = read_manifest(self.manifest)["download_failure"]

        self.assertEqual(("DownloadStalled", True), (failure["error_type"], failure["retryable"]))
        self.assertEqual(["stalled", "stalled"], [item["outcome"] for item in failure["attempts"]])


if __name__ == "__main__":
    unittest.main()
