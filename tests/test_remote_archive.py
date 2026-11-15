"""Listing a remote zip from its central directory alone, over HTTP Range requests.

Every archive here is synthetic and served from a local HTTP server on 127.0.0.1. The server counts each
request's range and the bytes it sent, so a test can check what the lister fetched as well as what it said:
the stage3b campaign ran every unit of a multi-unit accession on the whole study's files, and the triage that
replaces it needs each archive's member list (paths, sizes, CRC32) before anything is downloaded.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import struct
import tempfile
import threading
import time
import unittest
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server
from msdial_app.remote_archive import (
    DEFAULT_MAX_BYTES,
    RemoteArchiveError,
    identical_content_groups,
    list_remote_zip,
)


class _Archive:
    def __init__(self, body: bytes, *, ranges: bool = True, etag: str = '"v1"', last_modified: str = "",
                 delay: float = 0.0, content_type: str = "application/zip", honour_if_range: bool = True,
                 short_suffix: bool = False, trickle: float = 0.0, trickle_headers: float = 0.0) -> None:
        self.body = body
        self.ranges = ranges
        self.etag = etag
        self.last_modified = last_modified
        self.delay = delay
        self.content_type = content_type
        # False: a server that ignores If-Range and answers every Range request with a range.
        self.honour_if_range = honour_if_range
        # True: a suffix range answered with a range that stops one byte short of the end.
        self.short_suffix = short_suffix
        # Seconds between 64-byte pieces of the body, or between padding header lines: a trickling server.
        self.trickle = trickle
        self.trickle_headers = trickle_headers
        # Served instead of body from the second request on: the archive replaced while it was being listed.
        self.replacement: tuple[bytes, str] | None = None
        self.served = 0


class _Handler(BaseHTTPRequestHandler):
    archives: dict[str, _Archive] = {}
    log: list[dict] = []

    def log_message(self, *args) -> None:  # quiet
        pass

    def do_GET(self) -> None:
        if self.path.startswith("/redirect/"):
            self.send_response(302)
            self.send_header("Location", "/" + self.path[len("/redirect/"):])
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        archive = self.archives.get(self.path.lstrip("/"))
        entry = {"path": self.path, "range": self.headers.get("Range"), "if_range": self.headers.get("If-Range"),
                 "status": None, "sent": 0}
        self.log.append(entry)
        if archive is None:
            entry["status"] = 404
            self.send_error(404)
            return
        if archive.delay:
            time.sleep(archive.delay)
        body, etag = archive.body, archive.etag
        if archive.replacement is not None and archive.served >= 1:
            body, etag = archive.replacement
        archive.served += 1
        total = len(body)
        requested = self.headers.get("Range")
        if_range = self.headers.get("If-Range")
        partial = archive.ranges and requested and (
            if_range is None or if_range == etag or not archive.honour_if_range
        )
        if partial:
            match = re.match(r"bytes=(\d*)-(\d*)$", requested)
            first_text, last_text = match.group(1), match.group(2)
            if first_text == "":
                first, last = max(0, total - int(last_text)), total - 1
                if archive.short_suffix:
                    last -= 1
            else:
                first = int(first_text)
                last = min(total - 1, int(last_text)) if last_text else total - 1
            piece = body[first:last + 1]
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {first}-{last}/{total}")
        else:
            piece = body
            self.send_response(200)
        entry["status"] = 206 if partial else 200
        try:
            if etag:
                self.send_header("ETag", etag)
            if archive.last_modified:
                self.send_header("Last-Modified", archive.last_modified)
            if archive.ranges:
                self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Type", archive.content_type)
            self.send_header("Content-Length", str(len(piece)))
            if archive.trickle_headers:
                for index in range(90):
                    self.send_header(f"X-Padding-{index}", "x")
                    self.flush_headers()
                    time.sleep(archive.trickle_headers)
            self.end_headers()
            step = 64 if archive.trickle else 65536
            for start in range(0, len(piece), step):
                self.wfile.write(piece[start:start + step])
                if archive.trickle:
                    self.wfile.flush()
                    time.sleep(archive.trickle)
                entry["sent"] += len(piece[start:start + step])
        except (ConnectionError, OSError):
            pass


class _Server:
    def __enter__(self) -> "_Server":
        _Handler.archives = {}
        _Handler.log = []
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def add(self, name: str, archive: _Archive) -> str:
        _Handler.archives[name] = archive
        return f"http://127.0.0.1:{self.httpd.server_address[1]}/{name}"

    @property
    def log(self) -> list[dict]:
        return _Handler.log


def _zip(members: list[tuple[str, bytes]], *, comment: bytes = b"", method=zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=method) as archive:
        for name, data in members:
            if name.endswith("/"):
                archive.writestr(zipfile.ZipInfo(name, (2023, 3, 18, 11, 32, 4)), b"")
            else:
                archive.writestr(zipfile.ZipInfo(name, (2023, 3, 18, 11, 32, 4)), data, compress_type=method)
        archive.comment = comment
    return buffer.getvalue()


def _zip64(members: list[tuple[str, bytes]]) -> bytes:
    """A real ZIP64 archive: zipfile writes ZIP64 end records and extra fields once its limit is lowered."""
    buffer = io.BytesIO()
    with patch.object(zipfile, "ZIP64_LIMIT", 64), patch.object(zipfile, "ZIP_FILECOUNT_LIMIT", 2):
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as archive:
            for name, data in members:
                with archive.open(zipfile.ZipInfo(name, (2024, 1, 2, 3, 4, 6)), "w", force_zip64=True) as handle:
                    handle.write(data)
    return buffer.getvalue()


def _eocd_at(body: bytes) -> int:
    return body.rfind(b"PK")


def _patched(body: bytes, offset: int, fmt: str, *values: int) -> bytes:
    patch_bytes = struct.pack(fmt, *values)
    return body[:offset] + patch_bytes + body[offset + len(patch_bytes):]


def _unit_members() -> list[tuple[str, bytes]]:
    shared = b"identical run " * 400
    return [
        ("HILIC_NEGATIVE/", b""),
        ("HILIC_NEGATIVE/SA_01_HILICneg.mzML", b"<mzML>hilic one</mzML>" * 300),
        ("HILIC_NEGATIVE/SA_02_HILICneg.mzML", shared),
        ("RPLC_NEGATIVE/SA_02_RPLCneg.mzML", shared),
        ("RPLC_NEGATIVE/QC_01_RPLCneg.mzML", b"<mzML>qc</mzML>" * 500),
        ("RPLC_NEGATIVE/empty.txt", b""),
    ]


def _expected(body: bytes) -> list[dict]:
    with zipfile.ZipFile(io.BytesIO(body)) as archive:
        return [
            {"path": info.filename, "size": info.file_size, "compressed_size": info.compress_size,
             "crc32": f"{info.CRC:08X}", "method": info.compress_type, "local_header_offset": info.header_offset}
            for info in archive.infolist()
        ]


def _observed(listing: dict) -> list[dict]:
    keys = ("path", "size", "compressed_size", "crc32", "method", "local_header_offset")
    return [{key: member[key] for key in keys} for member in listing["members"]]


class RemoteZipListingTest(unittest.TestCase):
    def test_a_plain_zip_is_listed_from_its_tail_alone_with_provenance(self) -> None:
        body = _zip(_unit_members()) + b""
        with _Server() as server:
            url = server.add("ST000001_rawdata.zip", _Archive(body, last_modified="Thu, 27 Jul 2023 19:02:29 GMT"))
            listing = list_remote_zip(url)
            log = list(server.log)
        self.assertTrue(listing["ok"])
        self.assertEqual(_expected(body), _observed(listing))
        self.assertEqual((6, 5, 1), (listing["entry_count"], listing["file_count"], listing["directory_count"]))
        self.assertFalse(listing["zip64"])
        self.assertEqual(len(body), listing["size_bytes"])
        member = listing["members"][1]
        self.assertEqual(("deflate", False, "2023-03-18 11:32:04"),
                         (member["compression"], member["is_dir"], member["modified"]))
        self.assertTrue(listing["members"][0]["is_dir"])
        provenance = listing["provenance"]
        # A small archive's directory lies inside the first suffix range: one request, the whole file here.
        self.assertEqual(1, len(log))
        self.assertEqual("bytes=-8192", log[0]["range"])
        self.assertEqual(sum(entry["sent"] for entry in log), provenance["bytes_fetched"])
        self.assertEqual(('"v1"', "Thu, 27 Jul 2023 19:02:29 GMT"), (provenance["etag"], provenance["last_modified"]))
        self.assertEqual(url, provenance["url"])
        self.assertRegex(provenance["central_directory_sha256"], r"^[0-9a-f]{64}$")
        directory = body[provenance["central_directory_offset"]:
                         provenance["central_directory_offset"] + provenance["central_directory_size"]]
        import hashlib
        self.assertEqual(hashlib.sha256(directory).hexdigest(), provenance["central_directory_sha256"])
        self.assertIn("remote-archive-listing", provenance["user_agent"])
        self.assertEqual([], listing["warnings"])

    def test_a_large_archive_fetches_only_its_tail_and_central_directory(self) -> None:
        # Incompressible member data, so the archive is far larger than what the listing may read.
        import random
        rng = random.Random(7)
        members = [(f"data/run_{index:03d}.mzML", rng.randbytes(40_000)) for index in range(60)]
        body = _zip(members, method=zipfile.ZIP_STORED)
        with _Server() as server:
            url = server.add("big.zip", _Archive(body))
            listing = list_remote_zip(url)
            log = list(server.log)
        self.assertEqual(_expected(body), _observed(listing))
        self.assertGreater(len(body), 2_000_000)
        self.assertLess(listing["provenance"]["bytes_fetched"], 16_000)
        self.assertEqual(sum(entry["sent"] for entry in log), listing["provenance"]["bytes_fetched"])
        # Every request after the first is conditional on the first answer's ETag.
        self.assertTrue(all(entry["if_range"] == '"v1"' for entry in log[1:]))

    def test_zip64_end_records_and_extra_fields_are_read(self) -> None:
        members = [(f"RP_POS/sample_{index}.mzML", (b"zip64 member %d " % index) * 200) for index in range(4)]
        body = _zip64(members)
        self.assertIn(b"PK\x06\x06", body)
        self.assertIn(b"PK\x06\x07", body)
        with _Server() as server:
            listing = list_remote_zip(server.add("z64.zip", _Archive(body)))
        self.assertTrue(listing["zip64"])
        self.assertEqual(_expected(body), _observed(listing))
        self.assertTrue(any(member["zip64"] for member in listing["members"]))

    def test_a_long_comment_pushes_the_end_record_past_the_first_tail(self) -> None:
        body = _zip(_unit_members(), comment=b"c" * 30_000)
        with _Server() as server:
            listing = list_remote_zip(server.add("comment.zip", _Archive(body)))
            log = list(server.log)
        self.assertEqual(_expected(body), _observed(listing))
        self.assertEqual("c" * 30_000, listing["comment"])
        self.assertEqual(2, len(log))

    def test_bytes_before_the_archive_shift_its_offsets(self) -> None:
        body = b"STUB" * 3000 + _zip(_unit_members())
        with _Server() as server:
            listing = list_remote_zip(server.add("sfx.zip", _Archive(body)))
        self.assertEqual(12_000, listing["provenance"]["prefix_bytes"])
        self.assertEqual([member["path"] for member in _expected(body)],
                         [member["path"] for member in listing["members"]])
        self.assertTrue(any("precede" in warning for warning in listing["warnings"]))

    def test_utf8_names_and_a_redirect(self) -> None:
        body = _zip([("Lipids/Neg/sérum_01.mzML", b"x" * 100), ("脂質/QC.mzML", b"y" * 100)])
        with _Server() as server:
            server.add("utf8.zip", _Archive(body))
            base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
            listing = list_remote_zip(base + "/redirect/utf8.zip")
        self.assertEqual(["Lipids/Neg/sérum_01.mzML", "脂質/QC.mzML"], [m["path"] for m in listing["members"]])
        self.assertEqual({"utf8_flag"}, {m["name_encoding"] for m in listing["members"]})
        self.assertTrue(listing["provenance"]["final_url"].endswith("/utf8.zip"))
        self.assertNotIn("redirect", listing["provenance"]["final_url"])

    def test_a_declared_size_that_differs_is_a_warning_not_a_refusal(self) -> None:
        body = _zip(_unit_members())
        with _Server() as server:
            listing = list_remote_zip(server.add("a.zip", _Archive(body)), expected_size_bytes=len(body) - 100)
        self.assertTrue(listing["ok"])
        self.assertTrue(any("declared" in warning for warning in listing["warnings"]))

    def test_identical_content_is_grouped_by_crc_and_size(self) -> None:
        body = _zip(_unit_members())
        with _Server() as server:
            listing = list_remote_zip(server.add("a.zip", _Archive(body)))
        groups = identical_content_groups(listing["members"])
        self.assertEqual(1, len(groups))
        self.assertEqual(["HILIC_NEGATIVE/SA_02_HILICneg.mzML", "RPLC_NEGATIVE/SA_02_RPLCneg.mzML"],
                         groups[0]["paths"])

    def test_exactly_65535_entries_without_zip64_records(self) -> None:
        # CPython's zipfile writes ZIP64 end records only above 65,535 entries; at exactly 65,535 the plain end
        # record carries 0xFFFF as the real count and no locator precedes it.
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as archive:
            for index in range(65_535):
                archive.writestr(zipfile.ZipInfo(f"{index:04x}", (2023, 3, 18, 11, 32, 4)), b"")
        body = buffer.getvalue()
        self.assertNotIn(b"PK\x06\x07", body)
        self.assertEqual(0xFFFF, struct.unpack_from("<H", body, _eocd_at(body) + 10)[0])
        with _Server() as server:
            listing = list_remote_zip(server.add("many.zip", _Archive(body)), max_bytes=4 * 1024 * 1024)
        self.assertFalse(listing["zip64"])
        self.assertEqual(65_535, listing["entry_count"])
        self.assertEqual(_expected(body), _observed(listing))

    def test_a_zip64_end_record_with_extensible_data_is_found_at_its_recorded_offset(self) -> None:
        members = [(f"RP_POS/sample_{index}.mzML", (b"zip64 member %d " % index) * 200) for index in range(4)]
        original = _zip64(members)
        record = original.rfind(b"PK\x06\x06")
        self.assertEqual(44, struct.unpack_from("<Q", original, record + 4)[0])
        # 40 bytes of extensible data after the fixed record: the locator no longer sits 56 bytes after it.
        body = (original[:record] + b"PK\x06\x06" + struct.pack("<Q", 44 + 40) + original[record + 12:record + 56]
                + b"\xAB" * 40 + original[record + 56:])
        with _Server() as server:
            listing = list_remote_zip(server.add("z64x.zip", _Archive(body)))
        self.assertTrue(listing["zip64"])
        self.assertEqual(_expected(original), _observed(listing))
        self.assertEqual(0, listing["provenance"]["prefix_bytes"])

    def test_an_info_zip_unicode_path_extra_names_a_cp437_member_while_its_crc_matches(self) -> None:
        import zlib
        raw, unicode_name = b"Lipids/s_rum_01.mzML", "Lipids/s\u00e9rum_01.mzML".encode("utf-8")
        fresh = zipfile.ZipInfo(raw.decode("ascii"), (2023, 3, 18, 11, 32, 4))
        fresh.extra = struct.pack("<HHBI", 0x7075, 5 + len(unicode_name), 1, zlib.crc32(raw)) + unicode_name
        # A stale extra (its CRC is of another name, as after a rename by a tool that ignored it) is not used.
        stale = zipfile.ZipInfo("QC/renamed_01.mzML", (2023, 3, 18, 11, 32, 4))
        stale.extra = struct.pack("<HHBI", 0x7075, 5 + len(unicode_name), 1, zlib.crc32(b"other")) + unicode_name
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(fresh, b"x" * 100)
            archive.writestr(stale, b"y" * 100)
        with _Server() as server:
            listing = list_remote_zip(server.add("u.zip", _Archive(buffer.getvalue())))
        self.assertEqual(
            [("Lipids/s\u00e9rum_01.mzML", "unicode_path_extra"), ("QC/renamed_01.mzML", "cp437")],
            [(member["path"], member["name_encoding"]) for member in listing["members"]],
        )


class RemoteZipRefusalTest(unittest.TestCase):
    def _refused(self, url: str, **options) -> RemoteArchiveError:
        with self.assertRaises(RemoteArchiveError) as caught:
            list_remote_zip(url, **options)
        return caught.exception

    def test_a_server_without_range_support_is_refused_with_its_body_unread(self) -> None:
        body = b"\0" * (8 * 1024 * 1024) + _zip(_unit_members())
        with _Server() as server:
            url = server.add("norange.zip", _Archive(body, ranges=False))
            started = time.monotonic()
            error = self._refused(url)
            elapsed = time.monotonic() - started
            log = list(server.log)
        self.assertEqual("range_not_supported", error.reason)
        self.assertEqual(0, error.provenance["bytes_fetched"])
        self.assertEqual([200], [entry["status"] for entry in log])
        self.assertEqual(1, len(log))
        self.assertLess(elapsed, 5)
        self.assertIn("206", error.detail)
        result = error.as_result()
        self.assertEqual((False, "range_not_supported"), (result["ok"], result["reason"]))

    def test_a_directory_beyond_the_byte_cap_is_refused_before_it_is_requested(self) -> None:
        members = [(f"very/long/folder/name/for/a/study/sample_{index:05d}_HILIC_positive.mzML", b"")
                   for index in range(3000)]
        body = _zip(members)
        with _Server() as server:
            url = server.add("many.zip", _Archive(body))
            error = self._refused(url, max_bytes=64 * 1024)
            log = list(server.log)
            # With the cap raised the same archive lists.
            listing = list_remote_zip(url, max_bytes=1024 * 1024)
        self.assertEqual("byte_cap_exceeded", error.reason)
        self.assertEqual(1, len(log))
        self.assertLessEqual(error.provenance["bytes_fetched"], 64 * 1024)
        self.assertEqual(3000, listing["entry_count"])
        self.assertLessEqual(listing["provenance"]["bytes_fetched"], 1024 * 1024)

    def test_the_cap_holds_for_every_request(self) -> None:
        body = _zip(_unit_members(), comment=b"c" * 60_000)
        with _Server() as server:
            error = self._refused(server.add("c.zip", _Archive(body)), max_bytes=16 * 1024)
            log = list(server.log)
        self.assertEqual("byte_cap_exceeded", error.reason)
        self.assertLessEqual(sum(entry["sent"] for entry in log), 16 * 1024)

    def test_an_archive_replaced_between_requests_is_refused(self) -> None:
        import random
        members = [(f"study/folder/d_{index:04d}.bin", random.Random(index).randbytes(2_000)) for index in range(300)]
        body = _zip(members, method=zipfile.ZIP_STORED)
        with _Server() as server:
            archive = _Archive(body)
            archive.replacement = (_zip(members[:-1], method=zipfile.ZIP_STORED), '"v2"')
            error = self._refused(server.add("changing.zip", archive))
            log = list(server.log)
        self.assertEqual("changed_during_listing", error.reason)
        self.assertEqual([206, 200], [entry["status"] for entry in log])

    def test_a_same_size_replacement_with_a_new_etag_is_caught_when_if_range_is_ignored(self) -> None:
        import random
        members = [(f"study/folder/d_{index:04d}.bin", random.Random(index).randbytes(2_000)) for index in range(300)]
        body = _zip(members, method=zipfile.ZIP_STORED)
        replaced = body[:1000] + bytes([body[1000] ^ 0xFF]) + body[1001:]
        self.assertEqual(len(body), len(replaced))
        with _Server() as server:
            archive = _Archive(body, honour_if_range=False)
            archive.replacement = (replaced, '"v2"')
            error = self._refused(server.add("same-size.zip", archive))
            log = list(server.log)
        self.assertEqual("changed_during_listing", error.reason)
        # The server answered both requests with ranges; only the ETag tells the two versions apart.
        self.assertEqual([206, 206], [entry["status"] for entry in log])
        self.assertIn('"v2"', error.detail)

    def test_without_a_validator_a_changed_size_is_still_caught(self) -> None:
        import random
        members = [(f"study/folder/d_{index:04d}.bin", random.Random(index).randbytes(2_000)) for index in range(300)]
        body = _zip(members, method=zipfile.ZIP_STORED)
        with _Server() as server:
            archive = _Archive(body, etag="")
            archive.replacement = (_zip(members[:-1], method=zipfile.ZIP_STORED), "")
            error = self._refused(server.add("changing.zip", archive))
        self.assertEqual("changed_during_listing", error.reason)

    def test_without_a_validator_the_listing_warns(self) -> None:
        with _Server() as server:
            listing = list_remote_zip(server.add("a.zip", _Archive(_zip(_unit_members()), etag="")))
        self.assertTrue(any("ETag" in warning for warning in listing["warnings"]))

    def test_a_sign_in_page_after_a_redirect_is_an_access_refusal_not_missing_range_support(self) -> None:
        with _Server() as server:
            server.add("login", _Archive(b"<html><form>Sign in</form></html>" * 1000, ranges=False,
                                         content_type="text/html; charset=utf-8"))
            base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
            redirected = self._refused(base + "/redirect/login")
            direct = self._refused(base + "/login")
            # An archive that is served whole after a redirect is still a missing-range refusal, and says so.
            server.add("whole.zip", _Archive(_zip(_unit_members()), ranges=False))
            whole = self._refused(base + "/redirect/whole.zip")
        self.assertEqual("not_an_archive_response", redirected.reason)
        self.assertEqual(0, redirected.provenance["bytes_fetched"])
        self.assertTrue(redirected.provenance["final_url"].endswith("/login"))
        self.assertIn("text/html", redirected.detail)
        self.assertIn("redirect", redirected.detail)
        self.assertEqual("text/html; charset=utf-8", redirected.provenance["requests"][0]["content_type"])
        self.assertEqual("not_an_archive_response", direct.reason)
        self.assertEqual("range_not_supported", whole.reason)
        self.assertIn("after a redirect", whole.detail)

    def test_a_trickled_body_is_cut_off_at_the_overall_deadline(self) -> None:
        body = _zip(_unit_members(), method=zipfile.ZIP_STORED)
        self.assertGreater(len(body), 64 * 20)
        with _Server() as server:
            url = server.add("trickle.zip", _Archive(body, trickle=0.3))
            started = time.monotonic()
            error = self._refused(url, timeout_seconds=1, deadline_seconds=2)
            elapsed = time.monotonic() - started
        self.assertEqual("timeout", error.reason)
        self.assertIn("deadline", error.detail)
        self.assertLess(elapsed, 4)
        # What arrived before the cut is counted.
        self.assertGreater(error.provenance["bytes_fetched"], 0)
        self.assertLess(error.provenance["bytes_fetched"], len(body))

    def test_trickled_headers_are_cut_off_at_the_overall_deadline(self) -> None:
        with _Server() as server:
            url = server.add("slow-headers.zip", _Archive(_zip(_unit_members()), trickle_headers=0.3))
            started = time.monotonic()
            error = self._refused(url, timeout_seconds=1, deadline_seconds=2)
            elapsed = time.monotonic() - started
        self.assertEqual("timeout", error.reason)
        self.assertLess(elapsed, 4)
        self.assertEqual(0, error.provenance["bytes_fetched"])

    def test_a_suffix_range_that_stops_short_of_the_end_is_refused(self) -> None:
        with _Server() as server:
            error = self._refused(server.add("short.zip", _Archive(_zip(_unit_members()), short_suffix=True)))
        self.assertEqual("unexpected_range", error.reason)

    def test_an_end_record_whose_entry_count_the_directory_does_not_hold(self) -> None:
        body = _zip(_unit_members())
        eocd = _eocd_at(body)
        count = struct.unpack_from("<H", body, eocd + 10)[0]
        with _Server() as server:
            error = self._refused(server.add("count.zip", _Archive(_patched(body, eocd + 8, "<HH", count + 1,
                                                                           count + 1))))
        self.assertEqual("malformed_central_directory", error.reason)
        self.assertIn(f"{count + 1:,} entries", error.detail)

    def test_a_split_archive_is_refused_from_its_end_record_or_its_zip64_locator(self) -> None:
        body = _zip(_unit_members())
        split_eocd = _patched(body, _eocd_at(body) + 4, "<H", 1)
        zip64_body = _zip64([(f"RP_POS/sample_{index}.mzML", b"m" * 300) for index in range(3)])
        locator = zip64_body.rfind(b"PK\x06\x07")
        split_locator = _patched(zip64_body, locator + 16, "<I", 2)
        with _Server() as server:
            from_eocd = self._refused(server.add("split1.zip", _Archive(split_eocd)))
            from_locator = self._refused(server.add("split2.zip", _Archive(split_locator)))
        self.assertEqual(("multi_disk", "multi_disk"), (from_eocd.reason, from_locator.reason))

    def test_a_file_that_is_not_a_zip(self) -> None:
        with _Server() as server:
            error = self._refused(server.add("notzip.zip", _Archive(b"<html>not found</html>" * 4000)))
        self.assertEqual("not_a_zip", error.reason)

    def test_a_missing_file_is_an_http_error(self) -> None:
        with _Server() as server:
            base = f"http://127.0.0.1:{server.httpd.server_address[1]}"
            error = self._refused(base + "/missing.zip")
        self.assertEqual("http_error", error.reason)
        self.assertIn("404", error.detail)

    def test_a_server_that_does_not_answer_times_out(self) -> None:
        with _Server() as server:
            error = self._refused(server.add("slow.zip", _Archive(_zip(_unit_members()), delay=3)),
                                  timeout_seconds=0.5)
        self.assertEqual("timeout", error.reason)

    def test_urls_and_arguments_outside_the_contract(self) -> None:
        for url in ("file:///C:/data/a.zip", "ftp://ftp.example.org/a.zip", "a.zip", ""):
            self.assertEqual("unsupported_url", self._refused(url).reason, url)
        for cap in (0, 10, 64 * 1024 * 1024, "lots"):
            self.assertEqual("invalid_argument", self._refused("https://example.org/a.zip", max_bytes=cap).reason)
        for options in ({"timeout_seconds": "slow"}, {"deadline_seconds": 0}, {"deadline_seconds": None}):
            self.assertEqual("invalid_argument", self._refused("https://example.org/a.zip", **options).reason)
        # A declared size from repository metadata that is not a number is refused before any request.
        body = _zip(_unit_members())
        with _Server() as server:
            url = server.add("a.zip", _Archive(body))
            for declared in ("unknown", -5, "8.5 GB"):
                self.assertEqual("invalid_argument", self._refused(url, expected_size_bytes=declared).reason)
            log = list(server.log)
            listing = list_remote_zip(url, expected_size_bytes=" %d " % len(body))
        self.assertEqual([], log)
        self.assertEqual([], listing["warnings"])
        self.assertEqual(512 * 1024, DEFAULT_MAX_BYTES)


class RemoteArchiveToolTest(unittest.TestCase):
    def test_the_tool_is_served_and_answers_a_refusal_as_data(self) -> None:
        names = [tool.name for tool in asyncio.run(mcp_server.mcp.list_tools())]
        self.assertIn("msdial_list_remote_archive", names)
        with _Server() as server:
            url = server.add("norange.zip", _Archive(_zip(_unit_members()), ranges=False))
            result = mcp_server.msdial_list_remote_archive(url)
        self.assertEqual((False, "range_not_supported"), (result["ok"], result["reason"]))

    def test_member_limit_and_output_path(self) -> None:
        body = _zip(_unit_members())
        with tempfile.TemporaryDirectory() as folder, _Server() as server:
            url = server.add("a.zip", _Archive(body))
            destination = Path(folder) / "listing.json"
            result = mcp_server.msdial_list_remote_archive(url, member_limit=2, output_path=str(destination))
            saved = json.loads(destination.read_text(encoding="utf-8"))
            again = mcp_server.msdial_list_remote_archive(url, output_path=str(destination))
            relative = mcp_server.msdial_list_remote_archive(url, output_path="listing.json")
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(result["members"]))
        self.assertTrue(result["members_truncated"])
        self.assertEqual(6, result["entry_count"])
        self.assertEqual(6, len(saved["members"]))
        self.assertEqual(1, len(result["identical_content_groups"]))
        self.assertEqual(saved["identical_content_groups"], result["identical_content_groups"])
        self.assertFalse(again["ok"])
        self.assertIn("already exists", again["detail"])
        self.assertFalse(relative["ok"])


if __name__ == "__main__":
    unittest.main()
