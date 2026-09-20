"""An interrupted repository download must not start again from zero.

The library downloader in this application has always resumed from its .part file. The repository
downloader, which handles objects an order of magnitude larger, did not: it opened the .part with
mode "wb" and sent no Range header, so every attempt began at byte 0, and it unlinked the .part on
any exception, so the bytes already moved were thrown away.

That asymmetry is not theoretical. The job history holds three "The local backend stopped before
this job completed" entries from 2026-09-06, and the backend stopped again on 2026-09-20 during a
378 MB library transfer. Repository archives are larger than that by an order of magnitude: one
unit of the 2026-09-20 trial arrives as a single 1.80 GB zip, and the largest archive measured in
the catalog is 38 GB. Each had to complete in one unbroken connection or begin again from nothing.

The tests serve a real HTTP server on localhost rather than a mock, because what is being tested is
the conversation with a server: whether a Range is sent, whether a 206 is distinguished from a 200,
and what happens when the server refuses to honour either.
"""

from __future__ import annotations

import hashlib
import http.server
import tempfile
import threading
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import RepositoryHttpClient


PAYLOAD = bytes(range(256)) * 400          # 102,400 bytes, and every byte position distinguishable
SHA256 = hashlib.sha256(PAYLOAD).hexdigest()
MD5 = hashlib.md5(PAYLOAD).hexdigest()


class _Handler(http.server.BaseHTTPRequestHandler):
    """Serves PAYLOAD, honouring Range or refusing it according to the server's mode."""

    def log_message(self, *args) -> None:  # noqa: D102 - silence the test output
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        mode = self.server.mode
        requested = self.headers.get("Range")
        self.server.ranges_seen.append(requested)

        if self.path == "/truncate":
            # Sends only part of the body and hangs up, which is what an interruption looks like
            # from the client's side.
            self.send_response(200)
            self.send_header("Content-Length", str(len(PAYLOAD)))
            self.end_headers()
            self.wfile.write(PAYLOAD[: len(PAYLOAD) // 4])
            return

        if requested and mode == "ranges":
            start = int(requested.split("=", 1)[1].split("-", 1)[0])
            if start >= len(PAYLOAD):
                self.send_error(416)
                return
            body = PAYLOAD[start:]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}")
            self.end_headers()
            self.wfile.write(body)
            return

        # mode == "no-ranges": the Range is ignored and the whole object comes back as a 200.
        self.send_response(200)
        self.send_header("Content-Length", str(len(PAYLOAD)))
        self.end_headers()
        self.wfile.write(PAYLOAD)


class _Server:
    def __init__(self, mode: str) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.mode = mode
        self.httpd.ranges_seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.httpd.server_port}/object.zip"

    @property
    def ranges_seen(self) -> list:
        return self.httpd.ranges_seen

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


class ResumeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.destination = Path(self.directory.name) / "object.zip"
        self.partial = self.destination.with_name("object.zip.part")
        self.client = RepositoryHttpClient(timeout=10)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_whole_download_still_works_and_is_not_a_resume(self) -> None:
        server = _Server("ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 10_000_000)

        self.assertEqual(len(PAYLOAD), result["size_bytes"])
        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(0, result["resumed_from_bytes"])
        self.assertEqual([None], server.ranges_seen, "no Range is sent when there is nothing to resume")
        self.assertEqual(PAYLOAD, self.destination.read_bytes())

    def test_a_partial_file_is_offered_back_to_the_server(self) -> None:
        """THE FIX. Half the object is on disk; only the other half should cross the network."""
        self.partial.write_bytes(PAYLOAD[:50_000])
        server = _Server("ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 10_000_000)

        self.assertEqual(["bytes=50000-"], server.ranges_seen)
        self.assertEqual(50_000, result["resumed_from_bytes"])
        self.assertEqual(len(PAYLOAD), result["size_bytes"], "the size describes the whole object")
        self.assertEqual(PAYLOAD, self.destination.read_bytes())

    def test_the_checksums_describe_the_whole_object_not_just_the_resumed_part(self) -> None:
        """Otherwise a resumed file would fail every integrity check that follows it."""
        self.partial.write_bytes(PAYLOAD[:70_000])
        server = _Server("ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 10_000_000)

        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(MD5, result["md5"])

    def test_a_server_that_ignores_the_range_restarts_cleanly(self) -> None:
        """A 200 answering a Range means the whole object is coming; appending it would corrupt.

        Not every repository honours Range. What must never happen is a file that is one partial
        object followed by one whole object, which no checksum in the manifest would match.
        """
        self.partial.write_bytes(PAYLOAD[:50_000])
        server = _Server("no-ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 10_000_000)

        self.assertEqual(["bytes=50000-"], server.ranges_seen, "it still asked")
        self.assertEqual(0, result["resumed_from_bytes"], "and then started over when refused")
        self.assertEqual(len(PAYLOAD), result["size_bytes"])
        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(PAYLOAD, self.destination.read_bytes())

    def test_a_partial_longer_than_the_object_is_discarded_and_refetched(self) -> None:
        """A stale .part from a previous, larger version of the object produces a 416."""
        self.partial.write_bytes(PAYLOAD + b"stale tail")
        server = _Server("ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 10_000_000)

        self.assertEqual(SHA256, result["sha256"])
        self.assertEqual(PAYLOAD, self.destination.read_bytes())

    def test_an_interrupted_transfer_keeps_what_it_moved(self) -> None:
        """THE OTHER HALF OF THE FIX. The old code unlinked the .part on any exception.

        A truncated response raises, and what matters is what survives on disk: the bytes already
        written, so the next attempt resumes instead of starting again.
        """
        server = _Server("ranges")
        self.addCleanup(server.close)
        truncating = server.url.replace("/object.zip", "/truncate")

        with self.assertRaises(ValueError) as raised:
            self.client.download(truncating, self.destination, 10_000_000)

        self.assertIn("of 102400 declared bytes", str(raised.exception))
        self.assertTrue(self.partial.is_file(), "the partial file must survive the failure")
        self.assertEqual(len(PAYLOAD) // 4, self.partial.stat().st_size)
        self.assertFalse(self.destination.exists(), "and nothing is presented as complete")

    def test_the_safety_limit_counts_the_resumed_bytes_too(self) -> None:
        """Otherwise a resumed download could pass a limit the whole object exceeds."""
        self.partial.write_bytes(PAYLOAD[:90_000])
        server = _Server("ranges")
        self.addCleanup(server.close)

        with self.assertRaises(ValueError) as raised:
            self.client.download(server.url, self.destination, 95_000)

        self.assertIn("limit is 95000", str(raised.exception))
        self.assertFalse(self.destination.exists())

    def test_a_partial_already_over_the_limit_is_discarded_rather_than_hashed(self) -> None:
        """A leftover bigger than the limit can never become a valid result, whatever it holds.

        It is dropped before the request rather than hashed and then rejected, and the object
        itself is then fetched whole - it fits the limit even though the stale partial did not.
        """
        self.partial.write_bytes(b"x" * 200_000)
        server = _Server("ranges")
        self.addCleanup(server.close)

        result = self.client.download(server.url, self.destination, 150_000)

        self.assertEqual([None], server.ranges_seen, "no Range offered from an unusable partial")
        self.assertEqual(0, result["resumed_from_bytes"])
        self.assertEqual(SHA256, result["sha256"])

    def test_progress_starts_from_what_is_already_on_disk(self) -> None:
        """A resumed download that reports 0% would read as no progress at all."""
        self.partial.write_bytes(PAYLOAD[:60_000])
        server = _Server("ranges")
        self.addCleanup(server.close)
        seen = []

        self.client.download(server.url, self.destination, 10_000_000,
                             progress_callback=lambda done, total: seen.append((done, total)))

        self.assertEqual(60_000, seen[0][0])
        self.assertEqual(len(PAYLOAD), seen[0][1], "and the total is the whole object")
        self.assertEqual(len(PAYLOAD), seen[-1][0])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
