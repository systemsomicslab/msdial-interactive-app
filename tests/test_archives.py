"""Archive detection, the 7-Zip adapter, and extraction that refuses before it writes.

The campaign's raw data come as zip, 7z, rar, tar.gz and bare compressed files. What these tests
pin down is what the old lease code got wrong or never did: a .7z, .rar or bare .gz was never
opened; an HTML error page saved as .zip became "no inputs"; two root-less per-sample zips wrote
_FUNC001.DAT over each other; and 7-Zip, which the new code needs for 7z and rar, silently rewrites
'../evil.txt' to 'evil.txt' and exits 0, so every unsafe name has to be refused from the listing,
before a byte is written.

Every fixture is built here. Zip, tar and the compressed streams come from the standard library.
The 7z, the Deflate64 zip and the encrypted archives are made by the installed 7-Zip, and those
tests are skipped when it is absent. 7-Zip cannot create RAR, so the RAR5 fixture is written by
_rar5_stored below from RARLAB's published RAR 5.0 archive format description, using stored
(uncompressed) entries only; no RAR or UnRAR code is involved. RAR listings that cannot be made
that way (links, encryption) are fed to the adapter as mocked 7-Zip output.
"""

from __future__ import annotations

import bz2
import csv
import gzip
import hashlib
import io
import lzma
import os
import stat
import struct
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest
import warnings
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from msdial_app import archives
from msdial_app.archives import (
    ArchiveError,
    ArchiveToolError,
    ExtractionLimits,
    SevenZipTool,
    archive_kind,
    archive_kind_from_name,
    archive_stem,
    container_alias,
    detect_archive,
    extract_archive,
    find_sevenzip,
    inspect_sevenzip,
    list_archive,
    parse_sevenzip_info,
    sevenzip_candidates,
)


SEVENZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
HAVE_SEVENZIP = os.name == "nt" and SEVENZIP.is_file()
needs_sevenzip = unittest.skipUnless(HAVE_SEVENZIP, r"7-Zip is not installed at C:\Program Files\7-Zip")


def _zip_bytes(entries: list[tuple[str, bytes]], compression: int = zipfile.ZIP_DEFLATED) -> bytes:
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # duplicate names are written on purpose
        with zipfile.ZipFile(buffer, "w", compression=compression) as handle:
            for name, data in entries:
                # writestr(name, ...) stamps the current time, so the same entries built twice
                # differed whenever a 2-second boundary fell between the builds. The date is
                # fixed, as _tar_bytes fixes mtime; the rest is what writestr gives a name.
                info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = compression
                info.external_attr = (0o40775 << 16 | 0x10) if name.endswith("/") else 0o600 << 16
                handle.writestr(info, data)
    return buffer.getvalue()


def _write_zip(path: Path, entries: list[tuple[str, bytes]], **options) -> Path:
    path.write_bytes(_zip_bytes(entries, **options))
    return path


def _tar_bytes(entries: list[tuple[str, bytes | None]], mode: str = "w") -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode=mode) as handle:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.mtime = 1_700_000_000
            if data is None:
                info.type = tarfile.DIRTYPE
                handle.addfile(info)
            else:
                info.size = len(data)
                handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _patch_zip_headers(path: Path, *, flag: int | None = None, method: int | None = None) -> None:
    """Set the flag or method field of every local and central header of a small zip."""
    data = bytearray(path.read_bytes())
    for signature, flag_offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        start = 0
        while (position := data.find(signature, start)) >= 0:
            if flag is not None:
                data[position + flag_offset: position + flag_offset + 2] = struct.pack("<H", flag)
            if method is not None:
                data[position + flag_offset + 2: position + flag_offset + 4] = struct.pack("<H", method)
            start = position + 4
    path.write_bytes(bytes(data))


def _vint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte = value & 0x7F
        value >>= 7
        if value:
            out.append(byte | 0x80)
        else:
            out.append(byte)
            return bytes(out)


def _rar5_header(kind: int, flags: int, body: bytes, data_size: int | None = None) -> bytes:
    fields = _vint(kind) + _vint(flags)
    if flags & 0x0002:
        fields += _vint(data_size or 0)
    fields += body
    size = _vint(len(fields))
    return struct.pack("<I", zlib.crc32(size + fields) & 0xFFFFFFFF) + size + fields


def _rar5_stored(
    path: Path, entries: list[tuple[str, bytes | None]], comment: bytes | None = None
) -> Path:
    """A RAR5 archive of stored entries, per RARLAB's 'RAR 5.0 archive format' description.

    Layout: the 8-byte signature, a main archive header (type 1), one file header (type 2) per
    entry followed by its data area, and an end-of-archive header (type 5). Each header is its
    CRC32, its size and type as vints, its flags, and its fields. A file header's fields are file
    flags (0x1 directory, 0x4 CRC32 present), unpacked size, attributes, data CRC32, compression
    information (0: version 0, method 0 'store'), host OS (0: Windows), and the UTF-8 name. An
    archive comment is a service header (type 3) laid out like a file header and named 'CMT'.
    """
    out = b"Rar!\x1a\x07\x01\x00" + _rar5_header(1, 0, _vint(0))
    if comment is not None:
        body = (
            _vint(0x0004) + _vint(len(comment)) + _vint(0)
            + struct.pack("<I", zlib.crc32(comment) & 0xFFFFFFFF) + _vint(0) + _vint(0)
        )
        out += _rar5_header(3, 0x0002, body + _vint(3) + b"CMT", len(comment)) + comment
    for name, data in entries:
        encoded = name.encode("utf-8")
        if data is None:
            body = _vint(0x0001) + _vint(0) + _vint(0x10) + _vint(0) + _vint(0)
            out += _rar5_header(2, 0, body + _vint(len(encoded)) + encoded)
        else:
            body = (
                _vint(0x0004) + _vint(len(data)) + _vint(0x20)
                + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + _vint(0) + _vint(0)
            )
            out += _rar5_header(2, 0x0002, body + _vint(len(encoded)) + encoded, len(data)) + data
    out += _rar5_header(5, 0, _vint(0))
    path.write_bytes(out)
    return path


def _truncated_zip(path: Path) -> Path:
    """A zip cut to half its length, which is what an interrupted download leaves."""
    full = _zip_bytes([(f"S{index}.mzML", os.urandom(20_000)) for index in range(5)])
    path.write_bytes(full[: len(full) // 2])
    return path


def _directory_room(destination: Path, limit: int) -> int:
    """How long a top-level directory may be for '<destination>.partial\\<directory>' to be limit."""
    return limit - (len(str(destination.absolute())) + len(".partial")) - 1


def _files_under(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _sevenzip(*arguments: str, cwd: Path, data: bytes | None = None) -> None:
    subprocess.run(
        [str(SEVENZIP), *arguments],
        cwd=cwd,
        input=data,
        stdin=None if data is not None else subprocess.DEVNULL,
        capture_output=True,
        check=True,
    )


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        archives._remove_tree(self.root)
        self._temporary.cleanup()

    def assertNothingWritten(self, destination: Path, *present: str) -> None:
        self.assertFalse(destination.exists())
        self.assertFalse(destination.with_name(destination.name + ".partial").exists())
        self.assertEqual(sorted(present), sorted(path.name for path in self.root.iterdir()))


class ArchiveDetectionTests(_Workspace):
    def test_names_are_told_apart(self) -> None:
        cases = {
            "a.tar.gz": "tar.gz", "a.tgz": "tar.gz", "a.gz": "gz", "a.tar.bz2": "tar.bz2",
            "a.tbz2": "tar.bz2", "a.tar.xz": "tar.xz", "a.txz": "tar.xz", "a.bz2": "bz2",
            "a.xz": "xz", "a.7z": "7z", "a.rar": "rar", "a.zip": "zip", "a.tar": "tar",
            "X.RAW.ZIP": "zip", "X.raw": "", "run.mzML": "", ".zip": "",
            "x.mzXML.lzma": "lzma", "a.lzma": "lzma",
        }
        for name, kind in cases.items():
            with self.subTest(name=name):
                self.assertEqual(kind, archive_kind_from_name(name))

    def test_the_bytes_confirm_or_correct_the_name(self) -> None:
        tar = _tar_bytes([("bundle/run.mzML", b"<mzML/>")])
        fixtures = {
            "plain.gz": (gzip.compress(b"not a tar"), "gz"),
            "bundle.gz": (gzip.compress(tar), "tar.gz"),       # a bare .gz holding a tar
            "bundle.tgz": (gzip.compress(tar), "tar.gz"),
            "claims.tar.gz": (gzip.compress(b"not a tar"), "gz"),
            "b.bz2": (bz2.compress(b"x"), "bz2"),
            "b.tar.bz2": (bz2.compress(tar), "tar.bz2"),
            "c.xz": (lzma.compress(b"x"), "xz"),
            "c.tar.xz": (lzma.compress(tar), "tar.xz"),
            "d.tar": (tar, "tar"),
            "e.zip": (_zip_bytes([("a", b"a")]), "zip"),
            "f.7z": (b"7z\xbc\xaf\x27\x1c" + bytes(26), "7z"),
            "g.rar": (b"Rar!\x1a\x07\x01\x00" + bytes(24), "rar"),
            "h.rar": (b"Rar!\x1a\x07\x00" + bytes(24), "rar"),
            "i.zip": (b"7z\xbc\xaf\x27\x1c" + bytes(26), "7z"),  # a 7z named .zip
            "j.lzma": (lzma.compress(b"x" * 100, format=lzma.FORMAT_ALONE), "lzma"),
            "k.lzma": (lzma.compress(b"x"), "xz"),  # an xz named .lzma
        }
        for name, (data, kind) in fixtures.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                self.assertEqual(kind, archive_kind(path))
        self.assertTrue(detect_archive(self.root / "i.zip").name_mismatch)
        self.assertEqual("rar5", detect_archive(self.root / "g.rar").signature)
        self.assertEqual("rar4", detect_archive(self.root / "h.rar").signature)
        self.assertEqual("", archive_kind(self.root / "d.tar", name="d.mzML"))

    def test_html_saved_as_zip_is_a_failure_not_an_archive(self) -> None:
        page = self.root / "ST001234.zip"
        page.write_bytes(b"<!DOCTYPE html>\n<html><body>503 Service Unavailable</body></html>")
        with self.assertRaises(ArchiveError) as caught:
            detect_archive(page)
        self.assertEqual("not_an_archive", caught.exception.reason)
        self.assertIn("HTML", caught.exception.message)

        destination = self.root / "out"
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(page, destination)
        self.assertEqual("not_an_archive", caught.exception.reason)
        self.assertNothingWritten(destination, "ST001234.zip")

    def test_an_lzma_alone_stream_is_confirmed_by_its_decoded_header(self) -> None:
        """LZMA-alone has no magic bytes, so the name claims it and the header must decode (MTBLS688)."""
        good = lzma.compress(b"<mzXML/>" * 100, format=lzma.FORMAT_ALONE)
        self.assertEqual(b"\x5d", good[:1])
        broken = {
            "page.lzma": b"<!DOCTYPE html><html><body>404</body></html>" * 4,
            "properties.lzma": b"\xff" + good[1:],               # lc/lp/pb out of range
            "literals.lzma": bytes([4 * 9 + 8]) + good[1:],      # lc + lp above 4
            "dictionary.lzma": good[:1] + (0x12345).to_bytes(4, "little") + good[5:],
            "size.lzma": good[:5] + (1 << 40).to_bytes(8, "little") + good[13:],
            "coder.lzma": good[:13] + b"\x01" + good[14:],        # the range coder starts with zero
            "short.lzma": good[:10],
        }
        for name, data in broken.items():
            with self.subTest(name=name):
                path = self.root / name
                path.write_bytes(data)
                with self.assertRaises(ArchiveError) as caught:
                    detect_archive(path)
                self.assertEqual("not_an_archive", caught.exception.reason)
        path = self.root / "x.mzXML.lzma"
        path.write_bytes(good)
        detection = detect_archive(path)
        self.assertEqual(("lzma", "lzma_alone"), (detection.kind, detection.signature))

    def test_an_lzma_alone_stream_extracts_and_says_it_checks_nothing(self) -> None:
        payload = b"<mzXML/>" * 1000
        archive = self.root / "x.mzXML.lzma"
        archive.write_bytes(lzma.compress(payload, format=lzma.FORMAT_ALONE))
        record = extract_archive(archive, self.root / "out", listing_directory=self.root / "p")
        self.assertEqual({"x.mzXML": payload}, _files_under(self.root / "out"))
        self.assertEqual(("lzma", "lzma", "stream_file"),
                         (record["format"], record["reader"], record["destination_rule"]))
        self.assertEqual(("none", False), (record["integrity"], record["crc_verified"]))
        self.assertEqual("x.mzXML", record["container_root"])

        truncated = self.root / "cut.mzXML.lzma"
        truncated.write_bytes(archive.read_bytes()[:40])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(truncated, self.root / "cut")
        self.assertEqual("corrupt_archive", caught.exception.reason)
        self.assertFalse((self.root / "cut").exists())

    def test_the_container_alias_is_one_rule(self) -> None:
        self.assertEqual("x.mzXML", container_alias("x.mzXML.lzma"))
        self.assertEqual("X.raw", container_alias("X.raw.zip"))
        self.assertEqual("S1.d", container_alias("S1.d.rar"))
        self.assertEqual("run.mzML", container_alias("run.mzML.gz"))
        self.assertEqual("", container_alias("ST000001.zip"))
        self.assertEqual("", container_alias("X.raw"))
        self.assertEqual("X.raw", archive_stem("X.raw.zip"))
        self.assertEqual("study", archive_stem("study.tar.gz"))


class StandardLibraryExtractionTests(_Workspace):
    MEMBERS = [("S1.raw/_FUNC001.DAT", b"spectra" * 100), ("run.mzML", b"<mzML/>" * 50)]

    def _fixture(self, kind: str) -> Path:
        tar = _tar_bytes([("S1.raw", None)] + self.MEMBERS)
        payload = self.MEMBERS[1][1]
        builders = {
            "zip": ("bundle.zip", lambda: _zip_bytes([("S1.raw/", b"")] + self.MEMBERS)),
            "tar": ("bundle.tar", lambda: tar),
            "tar.gz": ("bundle.tar.gz", lambda: gzip.compress(tar)),
            "tar.bz2": ("bundle.tar.bz2", lambda: bz2.compress(tar)),
            "tar.xz": ("bundle.tar.xz", lambda: lzma.compress(tar)),
            "gz": ("run.mzML.gz", lambda: gzip.compress(payload)),
            "bz2": ("run.mzML.bz2", lambda: bz2.compress(payload)),
            "xz": ("run.mzML.xz", lambda: lzma.compress(payload)),
        }
        name, build = builders[kind]
        path = self.root / name
        path.write_bytes(build())
        return path

    def test_every_standard_format_extracts_with_a_full_record_and_listing(self) -> None:
        expected_crc = {
            "zip": True, "tar": False, "tar.gz": True, "tar.bz2": True, "tar.xz": True,
            "gz": True, "bz2": True, "xz": True,
        }
        for kind, crc_verified in expected_crc.items():
            with self.subTest(kind=kind):
                archive = self._fixture(kind)
                destination = self.root / f"out-{kind}"
                provenance = self.root / "provenance"
                record = extract_archive(archive, destination, listing_directory=provenance)

                if kind in ("gz", "bz2", "xz"):
                    self.assertEqual({"run.mzML": self.MEMBERS[1][1]}, _files_under(destination))
                    self.assertEqual("stream_file", record["destination_rule"])
                else:
                    self.assertEqual(dict(self.MEMBERS), _files_under(destination))
                    self.assertEqual("archive_root", record["destination_rule"])
                self.assertFalse(destination.with_name(destination.name + ".partial").exists())

                self.assertEqual(archives.EXTRACTION_SCHEMA, record["schema"])
                self.assertEqual(kind, record["format"])
                self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(), record["archive_sha256"])
                self.assertEqual("computed", record["archive_sha256_source"])
                self.assertEqual(archive.stat().st_size, record["compressed_bytes"])
                self.assertTrue(record["tool"]["name"].startswith("python-"))
                self.assertTrue(record["tool"]["version"])
                self.assertNotIn("executable", record["tool"])  # no interpreter path in a record
                self.assertTrue(record["started_at"] and record["finished_at"])
                self.assertEqual(record["file_count"] + record["directory_count"], record["member_count"])
                self.assertEqual(sum(len(data) for data in _files_under(destination).values()),
                                 record["expanded_bytes"])
                self.assertEqual([], record["rejected_members"])
                self.assertEqual([], record["nested"])
                self.assertIs(crc_verified, record["crc_verified"])
                self.assertIn("limits", record)

                listing = Path(record["members_tsv"]["path"])
                self.assertEqual(f"archive-members-{record['archive_sha256'][:12]}.tsv", listing.name)
                self.assertEqual(hashlib.sha256(listing.read_bytes()).hexdigest(),
                                 record["members_tsv"]["sha256"])
                with listing.open(encoding="utf-8", newline="") as handle:
                    rows = list(csv.DictReader(handle, delimiter="\t"))
                self.assertEqual(record["member_count"], len(rows))
                self.assertEqual(record["members_tsv"]["rows"], len(rows))
                files = {row["path"]: int(row["size"]) for row in rows if row["type"] == "file"}
                self.assertEqual({path: len(data) for path, data in _files_under(destination).items()},
                                 files)

    def test_a_tar_made_from_dot_keeps_its_root_entry_out_of_the_way(self) -> None:
        archive = self.root / "dot.tar.gz"
        archive.write_bytes(gzip.compress(_tar_bytes(
            [("./", None), ("./S1.raw", None), ("./S1.raw/_FUNC001.DAT", b"f")]
        )))
        record = extract_archive(archive, self.root / "out", listing_directory=self.root / "p")
        self.assertEqual({"S1.raw/_FUNC001.DAT": b"f"}, _files_under(self.root / "out"))
        self.assertEqual(2, record["member_count"])
        with open(record["members_tsv"]["path"], encoding="utf-8", newline="") as handle:
            self.assertEqual(["S1.raw", "S1.raw/_FUNC001.DAT"],
                             [row["path"] for row in csv.DictReader(handle, delimiter="\t")])

    def test_a_caller_supplied_hash_is_not_recomputed(self) -> None:
        archive = self._fixture("zip")
        record = extract_archive(archive, self.root / "out", archive_sha256="ab" * 32)
        self.assertEqual("ab" * 32, record["archive_sha256"])
        self.assertEqual("caller", record["archive_sha256_source"])

    def test_a_bad_crc_fails_and_leaves_no_tree(self) -> None:
        archive = _write_zip(self.root / "bad.zip", [("a.txt", b"abcdefgh" * 64)],
                             compression=zipfile.ZIP_STORED)
        data = bytearray(archive.read_bytes())
        data[data.find(b"abcdefgh") + 3] ^= 0xFF
        archive.write_bytes(bytes(data))
        destination = self.root / "out"
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, destination)
        self.assertEqual("corrupt_archive", caught.exception.reason)
        self.assertNothingWritten(destination, "bad.zip")

    def test_a_truncated_zip_is_corrupt_even_without_7zip(self) -> None:
        # zipfile refuses it, which sends it to 7-Zip; with 7-Zip absent the unit used to fail as
        # sevenzip_not_found, a reason that says nothing about the download that was cut short.
        archive = _truncated_zip(self.root / "partial.zip")
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out",
                            sevenzip_setting=str(self.root / "no-7zip" / "7z.exe"))
        self.assertEqual("corrupt_archive", caught.exception.reason)
        self.assertIn("BadZipFile", caught.exception.detail["stdlib_error"])
        self.assertEqual("sevenzip_not_found", caught.exception.detail["sevenzip_error"]["reason"])
        self.assertNothingWritten(self.root / "out", "partial.zip")

    def test_an_unexpected_error_still_leaves_no_tree(self) -> None:
        archive = self._fixture("zip")
        destination = self.root / "out"
        with patch.object(archives, "_write_members_tsv", side_effect=RuntimeError("no room")):
            with self.assertRaises(ArchiveError) as caught:
                extract_archive(archive, destination, listing_directory=self.root / "provenance")
        self.assertEqual("extraction_failed", caught.exception.reason)
        self.assertIn("RuntimeError", caught.exception.message)
        self.assertNothingWritten(destination, "bundle.zip")

    def test_the_destination_must_not_exist(self) -> None:
        archive = self._fixture("zip")
        (self.root / "out").mkdir()
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out")
        self.assertEqual("destination_exists", caught.exception.reason)

    def test_an_interrupted_extraction_is_cleared_on_retry(self) -> None:
        archive = self._fixture("zip")
        stale = self.root / "out.partial"
        (stale / "half").mkdir(parents=True)
        (stale / "half" / "written.bin").write_bytes(b"x" * 10)
        os.chmod(stale / "half" / "written.bin", stat.S_IREAD)
        record = extract_archive(archive, self.root / "out")
        self.assertTrue(record["removed_stale_staging"])
        self.assertFalse(stale.exists())
        self.assertEqual(dict(self.MEMBERS), _files_under(self.root / "out"))


class ListingValidationTests(_Workspace):
    UNSAFE = {
        "../x": "parent_traversal",
        "a/../../x": "parent_traversal",
        "C:/x": "drive_path",
        "\\\\srv\\share\\x": "unc_path",
        "/abs": "absolute_path",
        "a:b": "drive_path",            # drive-relative on Windows: b in drive A's current directory
        "run.raw:stream": "colon_in_name",
        "CON": "reserved_name",
        "nul.txt": "reserved_name",
        "COM1.dat": "reserved_name",
        "lpt9": "reserved_name",
        "x.": "trailing_dot_or_space",
        "dir /x": "trailing_dot_or_space",
        "q?.txt": "invalid_character",
    }

    def test_unsafe_names_are_refused_before_anything_is_written(self) -> None:
        for name, reason in self.UNSAFE.items():
            with self.subTest(name=name):
                archive = _write_zip(self.root / "evil.zip", [("safe.txt", b"ok"), (name, b"bad")])
                destination = self.root / "out"
                with self.assertRaises(ArchiveError) as caught:
                    extract_archive(archive, destination)
                self.assertEqual("unsafe_listing", caught.exception.reason)
                self.assertEqual([{"name": name.replace("\\", "/"), "reason": reason}],
                                 caught.exception.rejected)
                self.assertNothingWritten(destination, "evil.zip")

    def test_duplicates_up_to_case_and_file_directory_conflicts_are_refused(self) -> None:
        cases = {
            "case_duplicate": [("Ab.txt", b"1"), ("ab.TXT", b"2")],
            "path_conflict": [("a", b"file"), ("a/b", b"under a file")],
            "duplicate_name": [("same.txt", b"1"), ("same.txt", b"2")],
        }
        for reason, entries in cases.items():
            with self.subTest(reason=reason):
                archive = _write_zip(self.root / "dup.zip", entries)
                with self.assertRaises(ArchiveError) as caught:
                    extract_archive(archive, self.root / "out")
                self.assertEqual([reason], [item["reason"] for item in caught.exception.rejected])
                self.assertNothingWritten(self.root / "out", "dup.zip")
        # A directory spelled two ways would merge into one on NTFS.
        archive = _write_zip(self.root / "dirs.zip", [("d/x", b"1"), ("D/y", b"2")])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out")
        self.assertEqual("case_duplicate", caught.exception.rejected[0]["reason"])

    def test_links_and_special_files_are_refused(self) -> None:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as handle:
            for name, kind in (("sym", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE),
                               ("fifo", tarfile.FIFOTYPE)):
                info = tarfile.TarInfo(name)
                info.type = kind
                info.linkname = "../outside"
                handle.addfile(info)
        (self.root / "links.tar").write_bytes(buffer.getvalue())
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(self.root / "links.tar", self.root / "out")
        self.assertEqual(
            {"sym": "symbolic_link", "hard": "hard_link", "fifo": "special_file"},
            {item["name"]: item["reason"] for item in caught.exception.rejected},
        )

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            link = zipfile.ZipInfo("link")
            link.create_system = 3
            link.external_attr = (stat.S_IFLNK | 0o777) << 16
            handle.writestr(link, "../outside")
            reparse = zipfile.ZipInfo("junction")
            reparse.external_attr = 0x20 | stat.FILE_ATTRIBUTE_REPARSE_POINT
            handle.writestr(reparse, "x")
        (self.root / "links.zip").write_bytes(buffer.getvalue())
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(self.root / "links.zip", self.root / "out")
        self.assertEqual(
            {"link": "symbolic_link", "junction": "reparse_point"},
            {item["name"]: item["reason"] for item in caught.exception.rejected},
        )
        self.assertNothingWritten(self.root / "out", "links.tar", "links.zip")

    def test_the_path_length_limit_counts_the_staging_directory(self) -> None:
        parent = self.root / "deep"
        destination = parent / "d"
        staging_length = len(str(destination.absolute())) + len(".partial")
        allowed = 259 - staging_length - 1
        if allowed < 2:
            self.skipTest("the temporary directory is already too deep for this test")
        parent.mkdir()
        fits = _write_zip(self.root / "fits.zip", [("f" * allowed, b"x")])
        record = extract_archive(fits, destination)
        self.assertEqual(1, record["file_count"])
        archives._remove_tree(destination)
        too_long = _write_zip(self.root / "long.zip", [("f" * (allowed + 1), b"x")])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(too_long, destination)
        self.assertEqual("path_too_long", caught.exception.rejected[0]["reason"])
        self.assertFalse(destination.exists())

    def test_a_directory_longer_than_windows_can_create_is_refused(self) -> None:
        # Without long-path support CreateDirectoryW stops at 247 characters, 12 short of the file
        # limit. A member whose file path fitted but whose folder did not failed mid-write with
        # WinError 206 through zipfile, and succeeded through 7-Zip, which writes \\?\ paths.
        parent = self.root / "deep"
        destination = parent / "d"
        room = _directory_room(destination, 247)
        if room < 2:
            self.skipTest("the temporary directory is already too deep for this test")
        parent.mkdir()
        fits = _write_zip(self.root / "fits.zip", [("D" * room + "/f.txt", b"x")])
        self.assertEqual(1, extract_archive(fits, destination)["file_count"])
        archives._remove_tree(destination)
        cases = {
            "file": [("D" * (room + 1) + "/f.txt", b"x")],
            "directory": [("D" * (room + 1) + "/", b"")],
        }
        for label, entries in cases.items():
            with self.subTest(member=label):
                archive = _write_zip(self.root / "long.zip", entries)
                with self.assertRaises(ArchiveError) as caught:
                    extract_archive(archive, destination)
                self.assertEqual("unsafe_listing", caught.exception.reason)
                self.assertEqual(["path_too_long"],
                                 [item["reason"] for item in caught.exception.rejected])
                self.assertFalse(destination.exists())
                self.assertEqual([], os.listdir(parent))

    def test_a_tar_name_that_is_not_utf8_is_refused_before_anything_is_written(self) -> None:
        # A Japanese instrument PC writes cp932 names. tarfile hands them over with lone
        # surrogates, which NTFS stores under a name nothing can match and which the listing TSV
        # cannot encode; that left a complete '<destination>.partial' copy behind.
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT,
                          encoding="cp932") as handle:
            info = tarfile.TarInfo("\u30b5\u30f3\u30d7\u30eb.mzML")
            info.size = 7
            handle.addfile(info, io.BytesIO(b"<mzML/>"))
        archive = self.root / "sjis.tar"
        archive.write_bytes(buffer.getvalue())
        destination = self.root / "out"
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, destination, listing_directory=self.root / "provenance")
        self.assertEqual("unsafe_listing", caught.exception.reason)
        self.assertEqual(["undecodable_name"], [item["reason"] for item in caught.exception.rejected])
        # The refusal itself must be writable into a UTF-8 manifest.
        caught.exception.message.encode("utf-8")
        caught.exception.rejected[0]["name"].encode("utf-8")
        self.assertNothingWritten(destination, "sjis.tar")

    def test_an_encrypted_zip_is_refused_from_its_listing_without_7zip(self) -> None:
        archive = _write_zip(self.root / "locked.zip", [("a.txt", b"secret")],
                             compression=zipfile.ZIP_STORED)
        _patch_zip_headers(archive, flag=0x0001)
        started = time.monotonic()
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", sevenzip_setting=str(self.root / "none.exe"))
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual("encrypted_archive", caught.exception.reason)
        self.assertNothingWritten(self.root / "out", "locked.zip")


class GuardTests(_Workspace):
    def test_member_count_limit(self) -> None:
        archive = _write_zip(self.root / "many.zip", [(f"{index}.txt", b"x") for index in range(3)])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", limits=ExtractionLimits(max_members=2))
        self.assertEqual("member_count_exceeded", caught.exception.reason)
        self.assertNothingWritten(self.root / "out", "many.zip")

    def test_expansion_ratio_limit(self) -> None:
        archive = _write_zip(self.root / "bomb.zip", [("zeros.bin", bytes(200_000))])
        limits = ExtractionLimits(max_ratio=10, ratio_floor_bytes=100_000)
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", limits=limits)
        self.assertEqual("expansion_ratio_exceeded", caught.exception.reason)
        self.assertNothingWritten(self.root / "out", "bomb.zip")
        # Below the size floor the same ratio is accepted.
        record = extract_archive(archive, self.root / "out",
                                 limits=ExtractionLimits(max_ratio=10, ratio_floor_bytes=10 ** 9))
        self.assertEqual(200_000, record["expanded_bytes"])

    def test_the_disk_reserve_is_checked_from_the_exact_listing(self) -> None:
        archive = _write_zip(self.root / "big.zip", [("a.bin", bytes(5_000))])
        seen: list[Path] = []

        def free_bytes(path: Path) -> int:
            seen.append(path)
            return 1_000_000

        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", free_bytes=free_bytes,
                            limits=ExtractionLimits(reserve_bytes=996_000))
        self.assertEqual("insufficient_disk_space", caught.exception.reason)
        self.assertEqual(5_000, caught.exception.detail["declared_bytes"])
        self.assertTrue(all(path.is_dir() for path in seen))
        self.assertNothingWritten(self.root / "out", "big.zip")

    def test_a_compressed_stream_is_guarded_as_it_expands(self) -> None:
        archive = self.root / "zeros.bin.gz"
        archive.write_bytes(gzip.compress(bytes(3 * 1024 * 1024)))
        with patch.object(archives, "_STREAM_CHECK_BYTES", 1024 * 1024):
            with self.assertRaises(ArchiveError) as caught:
                extract_archive(archive, self.root / "out",
                                limits=ExtractionLimits(max_ratio=10, ratio_floor_bytes=100_000))
            self.assertEqual("expansion_ratio_exceeded", caught.exception.reason)
            # A stream declares no size, so admission passes; the space runs out as it expands.
            answers = iter([10 ** 12])
            with self.assertRaises(ArchiveError) as caught:
                extract_archive(archive, self.root / "out",
                                free_bytes=lambda path: next(answers, 10),
                                limits=ExtractionLimits(reserve_bytes=1000))
            self.assertEqual("insufficient_disk_space", caught.exception.reason)
        self.assertNothingWritten(self.root / "out", "zeros.bin.gz")


class NestedAndContainerTests(_Workspace):
    def test_root_less_per_sample_containers_get_their_own_folder(self) -> None:
        for sample in ("A", "B"):
            _write_zip(self.root / f"{sample}.raw.zip",
                       [("_FUNC001.DAT", f"{sample}-func".encode()), ("_HEADER.TXT", b"h")])
            record = extract_archive(self.root / f"{sample}.raw.zip", self.root / f"out-{sample}")
            self.assertEqual("container_stem", record["destination_rule"])
            self.assertEqual(f"{sample}.raw", record["container_stem"])
            self.assertEqual(f"{sample}.raw", record["container_root"])
            self.assertFalse(record["container_name_mismatch"])
            self.assertEqual(
                {f"{sample}.raw/_FUNC001.DAT": f"{sample}-func".encode(),
                 f"{sample}.raw/_HEADER.TXT": b"h"},
                _files_under(self.root / f"out-{sample}"),
            )

    def test_a_container_that_already_holds_its_folder_is_not_nested_twice(self) -> None:
        _write_zip(self.root / "X.raw.zip", [("X.raw/_FUNC001.DAT", b"f")])
        record = extract_archive(self.root / "X.raw.zip", self.root / "rooted")
        self.assertEqual("container_rooted", record["destination_rule"])
        self.assertEqual({"X.raw/_FUNC001.DAT": b"f"}, _files_under(self.root / "rooted"))
        self.assertEqual(("X.raw", False), (record["container_root"], record["container_name_mismatch"]))

        _write_zip(self.root / "T.raw.zip", [("T.raw", b"thermo")])
        record = extract_archive(self.root / "T.raw.zip", self.root / "thermo")
        self.assertEqual("container_single_file", record["destination_rule"])
        self.assertEqual({"T.raw": b"thermo"}, _files_under(self.root / "thermo"))
        self.assertEqual(("T.raw", False), (record["container_root"], record["container_name_mismatch"]))

        _write_zip(self.root / "ST000001.zip", [("S1.raw/_FUNC001.DAT", b"f")])
        record = extract_archive(self.root / "ST000001.zip", self.root / "bundle")
        self.assertEqual(("", False), (record["container_root"], record["container_name_mismatch"]))

    def test_a_container_packed_under_another_name_records_what_it_produced(self) -> None:
        # A.raw.zip holding B.raw is B.raw on disk. The alias still says A.raw, so the record has
        # to name the folder that exists rather than leave attribution to guess.
        _write_zip(self.root / "A.raw.zip", [("B.raw/_FUNC001.DAT", b"f")])
        record = extract_archive(self.root / "A.raw.zip", self.root / "out")
        self.assertEqual("container_rooted_other_name", record["destination_rule"])
        self.assertEqual({"B.raw/_FUNC001.DAT": b"f"}, _files_under(self.root / "out"))
        self.assertEqual("B.raw", record["container_root"])
        self.assertTrue(record["container_name_mismatch"])
        self.assertEqual("A.raw", container_alias("A.raw.zip"))

    def test_operating_system_metadata_is_dropped_and_does_not_defeat_the_rooted_rule(self) -> None:
        # Finder adds __MACOSX/ (whose S1.d/ would look like a second Bruker folder) and .DS_Store;
        # tar on macOS writes '._name' AppleDouble files; Explorer leaves Thumbs.db. Any of them
        # made a second top-level entry, and S1.d.zip was unpacked as S1.d/S1.d/.
        finder = _write_zip(self.root / "S1.d.zip", [
            ("S1.d/analysis.tdf", b"t"), ("S1.d/analysis.tdf_bin", b"b"),
            ("S1.d/.DS_Store", b"finder"), ("S1.d/method.m/.DS_Store", b"finder"),
            ("__MACOSX/", b""), ("__MACOSX/S1.d/._analysis.tdf", b"apple"), ("Thumbs.db", b"x"),
        ])
        record = extract_archive(finder, self.root / "finder", listing_directory=self.root / "p")
        self.assertEqual("container_rooted", record["destination_rule"])
        self.assertEqual({"S1.d/analysis.tdf": b"t", "S1.d/analysis.tdf_bin": b"b"},
                         _files_under(self.root / "finder"))
        # The folder that held only a .DS_Store was a folder in the archive, and stays one.
        self.assertTrue((self.root / "finder" / "S1.d" / "method.m").is_dir())
        self.assertFalse((self.root / "finder" / "__MACOSX").exists())
        self.assertEqual(("S1.d", False), (record["container_root"], record["container_name_mismatch"]))
        self.assertEqual(5, record["dropped_metadata"]["members"])
        self.assertEqual(2, record["file_count"])
        with open(record["members_tsv"]["path"], encoding="utf-8", newline="") as handle:
            rows = {row["path"]: row["disposition"] for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual("dropped_metadata", rows["__MACOSX/S1.d/._analysis.tdf"])
        self.assertEqual("dropped_metadata", rows["S1.d/.DS_Store"])
        self.assertEqual("extracted", rows["S1.d/analysis.tdf"])

        mac_tar = self.root / "S2.d.tar"
        mac_tar.write_bytes(_tar_bytes([("._S2.d", b"apple"), ("S2.d", None),
                                        ("S2.d/analysis.tdf", b"t"), ("S2.d/._analysis.tdf", b"a")]))
        record = extract_archive(mac_tar, self.root / "tar")
        self.assertEqual("container_rooted", record["destination_rule"])
        self.assertEqual({"S2.d/analysis.tdf": b"t"}, _files_under(self.root / "tar"))

        # Root-less, the metadata goes and the container still gets its own folder.
        _write_zip(self.root / "S3.raw.zip", [("_FUNC001.DAT", b"f"), (".DS_Store", b"finder"),
                                              ("__MACOSX/._FUNC001.DAT", b"apple")])
        record = extract_archive(self.root / "S3.raw.zip", self.root / "rootless")
        self.assertEqual("container_stem", record["destination_rule"])
        self.assertEqual({"S3.raw/_FUNC001.DAT": b"f"}, _files_under(self.root / "rootless"))

    def test_a_study_archive_of_per_sample_archives_expands_without_collisions(self) -> None:
        inner_tar = gzip.compress(_tar_bytes([("batch", None), ("batch/run1.mzML", b"<run1/>")]))
        sample_a = _zip_bytes([("_FUNC001.DAT", b"A-func"), ("_HEADER.TXT", b"A")])
        study = _write_zip(self.root / "ST000001.zip", [
            ("raw/A.raw.zip", sample_a),
            ("raw/B.raw.zip", _zip_bytes([("_FUNC001.DAT", b"B-func"), ("_HEADER.TXT", b"B")])),
            ("raw/sample.mzML.gz", gzip.compress(b"<sample/>")),
            ("raw/batch.tar.gz", inner_tar),
            ("raw/notes.gz", b"plain text that only looks like a gzip by name"),
        ])
        provenance = self.root / "provenance"
        record = extract_archive(study, self.root / "out", listing_directory=provenance)

        self.assertEqual(
            {
                "raw/A.raw/_FUNC001.DAT": b"A-func", "raw/A.raw/_HEADER.TXT": b"A",
                "raw/B.raw/_FUNC001.DAT": b"B-func", "raw/B.raw/_HEADER.TXT": b"B",
                "raw/sample.mzML": b"<sample/>",
                "raw/batch/run1.mzML": b"<run1/>",
                "raw/notes.gz": b"plain text that only looks like a gzip by name",
            },
            _files_under(self.root / "out"),
        )
        nested = {item["archive_path"]: item for item in record["nested"]}
        self.assertEqual({"raw/A.raw.zip", "raw/B.raw.zip", "raw/sample.mzML.gz", "raw/batch.tar.gz"},
                         set(nested))
        self.assertEqual("container_stem", nested["raw/A.raw.zip"]["destination_rule"])
        self.assertEqual("nested_rooted", nested["raw/batch.tar.gz"]["destination_rule"])
        self.assertEqual("stream_file", nested["raw/sample.mzML.gz"]["destination_rule"])
        self.assertTrue(all(item["depth"] == 2 for item in nested.values()))
        # The digests are of the bytes that were packed, not of a second build of the same entries.
        self.assertEqual(hashlib.sha256(sample_a).hexdigest(), nested["raw/A.raw.zip"]["archive_sha256"])
        # A nested archive is gone once expanded, so every digest a repository may publish is kept.
        self.assertEqual(hashlib.md5(sample_a).hexdigest(), nested["raw/A.raw.zip"]["archive_md5"])
        self.assertEqual(40, len(nested["raw/A.raw.zip"]["archive_sha1"]))
        self.assertEqual([{"path": "raw/notes.gz", "reason": "not_an_archive"}], record["nested_skipped"])
        self.assertEqual(5, record["lineage"]["archives"])

        with open(record["members_tsv"]["path"], encoding="utf-8", newline="") as handle:
            rows = {row["path"]: row for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual("expanded_archive", rows["raw/A.raw.zip"]["disposition"])
        self.assertEqual("raw/A.raw.zip", rows["raw/A.raw/_FUNC001.DAT"]["archive"])
        self.assertEqual("2", rows["raw/A.raw/_FUNC001.DAT"]["depth"])
        self.assertEqual("ST000001.zip", rows["raw/notes.gz"]["archive"])

    def test_an_empty_nested_archive_expands_to_an_empty_folder(self) -> None:
        # The folder the module made for it was reported as an unexpected directory, and the whole
        # study failed verification.
        _write_zip(self.root / "study.zip", [("raw/a.mzML", b"a"), ("raw/empty.zip", _zip_bytes([]))])
        record = extract_archive(self.root / "study.zip", self.root / "out")
        self.assertEqual({"raw/a.mzML": b"a"}, _files_under(self.root / "out"))
        self.assertTrue((self.root / "out" / "raw" / "empty").is_dir())
        self.assertEqual(["raw/empty.zip"], [item["archive_path"] for item in record["nested"]])
        self.assertEqual(0, record["nested"][0]["member_count"])

        # A nested archive alone in its folder: that folder is still expected once it is unpacked.
        _write_zip(self.root / "lone.zip", [("raw/X.wiff.zip", _zip_bytes([]))])
        record = extract_archive(self.root / "lone.zip", self.root / "lone")
        self.assertEqual("container_files", record["nested"][0]["destination_rule"])
        self.assertEqual([], os.listdir(self.root / "lone" / "raw"))

    def test_a_nested_archive_whose_expansion_exists_beside_it_is_left_packed(self) -> None:
        # A study that ships run.mzML and run.mzML.gz used to fail as a whole.
        packed = gzip.compress(b"<a/>")
        s1 = _zip_bytes([("x.mzML", b"x")])
        archive = _write_zip(self.root / "both.zip", [
            ("run.mzML", b"<a/>"), ("run.mzML.gz", packed),
            ("S1.zip", s1), ("S1/kept.txt", b"k"),
            ("other.zip", _zip_bytes([("y.mzML", b"y")])),
        ])
        record = extract_archive(archive, self.root / "out", listing_directory=self.root / "p")
        self.assertEqual(
            {"run.mzML": b"<a/>", "run.mzML.gz": packed, "S1.zip": s1,
             "S1/kept.txt": b"k", "other/y.mzML": b"y"},
            _files_under(self.root / "out"),
        )
        self.assertEqual(
            [{"path": "run.mzML.gz", "reason": "destination_exists", "existing": "run.mzML"},
             {"path": "S1.zip", "reason": "destination_exists", "existing": "S1"}],
            sorted(record["nested_skipped"], key=lambda item: item["path"], reverse=True),
        )
        self.assertEqual(["other.zip"], [item["archive_path"] for item in record["nested"]])
        with open(record["members_tsv"]["path"], encoding="utf-8", newline="") as handle:
            rows = {row["path"]: row["disposition"] for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual("extracted", rows["run.mzML.gz"])
        self.assertEqual("expanded_archive", rows["other.zip"])

    def test_nesting_is_limited_to_three_levels(self) -> None:
        level3 = _zip_bytes([("deep.txt", b"deep")])
        level2 = _zip_bytes([("l3.zip", level3)])
        _write_zip(self.root / "l1.zip", [("l2.zip", level2)])
        record = extract_archive(self.root / "l1.zip", self.root / "three")
        self.assertEqual({"l2/l3/deep.txt": b"deep"}, _files_under(self.root / "three"))
        self.assertEqual(3, record["nested"][0]["nested"][0]["depth"])

        _write_zip(self.root / "l0.zip", [("l1.zip", (self.root / "l1.zip").read_bytes())])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(self.root / "l0.zip", self.root / "four")
        self.assertEqual("nesting_depth_exceeded", caught.exception.reason)
        self.assertFalse((self.root / "four").exists())
        self.assertFalse((self.root / "four.partial").exists())

    def test_an_archive_inside_a_vendor_folder_is_left_as_the_vendor_wrote_it(self) -> None:
        inner = _zip_bytes([("method.xml", b"<m/>")])
        _write_zip(self.root / "S1.d.zip", [("S1.d/AcqData/method.zip", inner)])
        record = extract_archive(self.root / "S1.d.zip", self.root / "out")
        self.assertEqual({"S1.d/AcqData/method.zip": inner}, _files_under(self.root / "out"))
        self.assertEqual([{"path": "S1.d/AcqData/method.zip", "reason": "inside_vendor_container"}],
                         record["nested_skipped"])


class ToolRunTests(unittest.TestCase):
    def test_the_watchdog_kills_the_tool(self) -> None:
        started = time.monotonic()
        run = archives._run_tool(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=120, watch=lambda: "insufficient_disk_space", interval=0.05,
        )
        self.assertEqual("insufficient_disk_space", run.killed_reason)
        self.assertLess(time.monotonic() - started, 10)

    def test_a_watchdog_that_raises_kills_the_tool(self) -> None:
        # A free-space probe that cannot answer is a breach, not a pass.
        def watch() -> str:
            raise OSError("the volume went away")

        started = time.monotonic()
        run = archives._run_tool(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            timeout=120, watch=watch, interval=0.05,
        )
        self.assertEqual("watchdog_failed", run.killed_reason)
        self.assertIn("the volume went away", run.killed_detail)
        self.assertLess(time.monotonic() - started, 10)

    def test_nothing_leaves_the_supervision_loop_with_the_tool_still_running(self) -> None:
        started: list[subprocess.Popen] = []
        popen = subprocess.Popen

        def spy(*arguments, **options):
            started.append(popen(*arguments, **options))
            return started[-1]

        self.addCleanup(lambda: [process.kill() for process in started if process.poll() is None])

        def watch() -> str:
            raise KeyboardInterrupt

        with patch.object(archives.subprocess, "Popen", spy):
            with self.assertRaises(KeyboardInterrupt):
                archives._run_tool([sys.executable, "-c", "import time; time.sleep(60)"],
                                   timeout=120, watch=watch, interval=0.05)
        self.assertEqual(1, len(started))
        self.assertIsNotNone(started[0].poll())

    def test_the_timeout_kills_the_tool(self) -> None:
        run = archives._run_tool(
            [sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.3, interval=0.05,
        )
        self.assertEqual("tool_timeout", run.killed_reason)

    def test_stdin_is_closed_so_nothing_can_wait_for_a_password(self) -> None:
        run = archives._run_tool(
            [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
            timeout=30, interval=0.05,
        )
        self.assertEqual(0, run.exit_code)
        self.assertEqual("0", run.stdout_tail.strip())


class SevenZipDiscoveryTests(_Workspace):
    INFO = (
        "\r\n7-Zip 25.01 (x64) : Copyright (c) 1999-2025 Igor Pavlov : 2025-08-03\r\n\r\n\r\n"
        "Libs:\r\n 0 : 25.01 : {library}\r\n\r\nFormats:\r\n"
        " 0 C...F..........c.a.m+.. w...0  7z       7z            7 z BC AF ' 1C\r\n"
        " 0  ...F..................  Rar      rar r00       R a r ! 1A 07 00\r\n"
        " 0  ...F..................  Rar5     rar r00       R a r ! 1A 07 01 00\r\n"
        " 0 C...FMG........c.a.m+.. wud.0  zip      zip z01 zipx jar  P K 03 04\r\n"
        "\r\nCodecs:\r\n 0  ED     40109 Deflate64\r\n"
    )

    def _fake(self, directory: Path, version: str = "25.01") -> tuple[Path, object]:
        directory.mkdir(parents=True, exist_ok=True)
        executable = directory / "7z.exe"
        executable.write_bytes(b"stand-in for 7z.exe " + version.encode())
        library = directory / "7z.dll"
        library.write_bytes(b"stand-in for 7z.dll")
        text = self.INFO.replace("25.01", version).format(library=library)

        def run(argv, **options):
            self.assertEqual(subprocess.DEVNULL, options.get("stdin"))
            return subprocess.CompletedProcess(argv, 0, text.encode("utf-8"), b"")

        return executable, run

    def test_the_info_output_is_parsed(self) -> None:
        info = parse_sevenzip_info(self.INFO.format(library=r"C:\Tools\7-Zip\7z.dll"))
        self.assertEqual("25.01", info["version"])
        self.assertEqual(r"C:\Tools\7-Zip\7z.dll", info["library"])
        self.assertEqual(("7z", "Rar", "Rar5", "zip"), info["formats"])
        self.assertEqual("16.02", parse_sevenzip_info("7-Zip [64] 16.02 : Copyright")["version"])
        self.assertEqual("25.01", parse_sevenzip_info("7-Zip (z) 25.01 (x64) : Copyright")["version"])

    def test_the_search_order(self) -> None:
        environ = {
            "MSDIAL_SEVENZIP": r"E:\env\7z.exe", "ProgramW6432": r"P:\64",
            "ProgramFiles": r"P:\pf", "ProgramFiles(x86)": r"P:\86",
        }
        candidates = sevenzip_candidates(
            r"S:\set\7z.exe", environ=environ, registry=lambda: [r"R:\reg"],
            which=lambda name: r"W:\bin\7z.exe" if name == "7z" else None,
        )
        self.assertEqual(
            ["setting", "environment", "registry", "program_files", "program_files",
             "program_files", "path"],
            [source for source, _ in candidates],
        )

    def test_program_files_is_found_with_path_empty_and_hashes_are_recorded(self) -> None:
        executable, run = self._fake(self.root / "Program Files" / "7-Zip")
        tool = find_sevenzip(
            environ={"ProgramW6432": str(self.root / "Program Files"), "PATH": ""},
            registry=lambda: [], which=lambda name: None, run=run,
        )
        self.assertEqual("program_files", tool.source)
        self.assertEqual(str(executable), tool.executable)
        self.assertEqual("25.01", tool.version)
        self.assertEqual(hashlib.sha256(executable.read_bytes()).hexdigest(), tool.executable_sha256)
        self.assertEqual(hashlib.sha256((executable.parent / "7z.dll").read_bytes()).hexdigest(),
                         tool.library_sha256)
        self.assertTrue(tool.supports("rar5"))

    def test_an_old_7zip_is_refused_and_a_newer_candidate_used(self) -> None:
        old, old_run = self._fake(self.root / "old", "24.09")
        with self.assertRaises(ArchiveToolError) as caught:
            inspect_sevenzip(old, run=old_run)
        self.assertEqual("sevenzip_version_too_old", caught.exception.reason)

        new, new_run = self._fake(self.root / "Program Files" / "7-Zip")

        def run(argv, **options):
            return (old_run if Path(argv[0]) == old else new_run)(argv, **options)

        tool = find_sevenzip(
            environ={"ProgramW6432": str(self.root / "Program Files")},
            registry=lambda: [str(self.root / "old")], which=lambda name: None, run=run,
        )
        self.assertEqual(str(new), tool.executable)

    def test_a_configured_path_is_authoritative(self) -> None:
        executable, run = self._fake(self.root / "Program Files" / "7-Zip")
        with self.assertRaises(ArchiveToolError) as caught:
            find_sevenzip(
                str(self.root / "missing" / "7z.exe"),
                environ={"ProgramW6432": str(self.root / "Program Files")},
                registry=lambda: [], which=lambda name: None, run=run,
            )
        self.assertEqual("sevenzip_not_found", caught.exception.reason)
        tool = find_sevenzip(environ={"MSDIAL_SEVENZIP": str(executable)},
                             registry=lambda: [], which=lambda name: None, run=run)
        self.assertEqual("environment", tool.source)

    def test_nothing_found_is_a_clear_failure(self) -> None:
        with self.assertRaises(ArchiveToolError) as caught:
            find_sevenzip(environ={}, registry=lambda: [], which=lambda name: None)
        self.assertEqual("sevenzip_not_found", caught.exception.reason)

    def test_a_zip_method_python_cannot_read_needs_7zip(self) -> None:
        archive = _write_zip(self.root / "explorer.zip", [("a.txt", b"x" * 100)],
                             compression=zipfile.ZIP_STORED)
        _patch_zip_headers(archive, method=9)  # Deflate64, as Windows Explorer writes large zips
        with self.assertRaises(ArchiveToolError) as caught:
            extract_archive(archive, self.root / "out",
                            sevenzip_setting=str(self.root / "no-7zip" / "7z.exe"))
        self.assertEqual("sevenzip_not_found", caught.exception.reason)
        self.assertNothingWritten(self.root / "out", "explorer.zip")

    @needs_sevenzip
    def test_the_installed_7zip_is_found_through_the_registry_and_program_files(self) -> None:
        if not archives._registry_sevenzip_directories():
            self.skipTest("7-Zip is installed but not registered")
        tool = find_sevenzip(environ={"PATH": ""}, which=lambda name: None)
        self.assertEqual("registry", tool.source)
        self.assertGreaterEqual(archives._version_tuple(tool.version), archives.MINIMUM_SEVENZIP_VERSION)
        tool = find_sevenzip(environ={"ProgramW6432": str(SEVENZIP.parent.parent), "PATH": ""},
                             registry=lambda: [], which=lambda name: None)
        self.assertEqual("program_files", tool.source)
        self.assertEqual(str(SEVENZIP), tool.executable)
        self.assertEqual(hashlib.sha256(SEVENZIP.read_bytes()).hexdigest(), tool.executable_sha256)
        self.assertEqual(hashlib.sha256((SEVENZIP.parent / "7z.dll").read_bytes()).hexdigest(),
                         tool.library_sha256)
        self.assertTrue(tool.supports("rar5") and tool.supports("rar4") and tool.supports("7z"))


RAR5_LISTING = """
7-Zip 25.01 (x64) : Copyright (c) 1999-2025 Igor Pavlov : 2025-08-03

Scanning the drive for archives:
1 file, 400 bytes (1 KiB)

Listing archive: samples.rar

--
Path = samples.rar
Type = Rar5
Physical Size = 400

----------
Path = S1.raw
Folder = +
Size = 0
Packed Size = 0
Attributes = D
Alternate Stream = -
Encrypted = -
CRC =
Symbolic Link =
Hard Link =

Path = S1.raw\\_FUNC001.DAT
Folder = -
Size = 14
Packed Size = 14
Attributes = A
Alternate Stream = -
Encrypted = -
CRC = 6729A6A7
Symbolic Link =
Hard Link =
{extra}
"""

RAR5_MEMBER = """
Path = {path}
Folder = -
Size = 5
Packed Size = 5
Attributes = A
Alternate Stream = {stream}
Encrypted = {encrypted}
CRC = 3610A686
Symbolic Link = {symlink}
Hard Link = {hardlink}
"""


class MockedSevenZipListingTests(_Workspace):
    """RAR listings with links or encryption, which no fixture here can produce, as 7-Zip prints them."""

    TOOL = SevenZipTool(
        executable="7z-under-test", version="25.01", source="test", executable_sha256="0" * 64,
    )

    def _rar(self, signature: bytes = b"Rar!\x1a\x07\x01\x00") -> Path:
        path = self.root / "samples.rar"
        path.write_bytes(signature + bytes(32))
        return path

    def _fake_run(self, text: str, exit_code: int = 0, stderr: str = ""):
        calls: list[list[str]] = []

        def run(argv, *, timeout, on_line=None, watch=None, interval=5.0):
            calls.append(list(argv))
            if on_line is not None:
                for line in text.split("\n"):
                    on_line(line)
            return archives._ToolRun(exit_code, text[-1000:], stderr, "", 0.01)

        return run, calls

    def _member(self, path="x.txt", stream="-", encrypted="-", symlink="", hardlink="") -> str:
        return RAR5_MEMBER.format(path=path, stream=stream, encrypted=encrypted,
                                  symlink=symlink, hardlink=hardlink)

    def test_a_plain_rar_listing_is_parsed_with_the_rar5_switch_and_the_sentinel(self) -> None:
        run, calls = self._fake_run(RAR5_LISTING.format(extra=""))
        with patch.object(archives, "_run_tool", run):
            listing = list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual("Rar5", listing.reported_type)
        self.assertEqual([("S1.raw", True, 0), ("S1.raw\\_FUNC001.DAT", False, 14)],
                         [(member.name, member.is_dir, member.size) for member in listing.members])
        self.assertEqual([], archives.validate_listing(listing.members, [self.root]))
        argv = calls[0]
        self.assertEqual(["7z-under-test", "l", "-slt"], argv[:3])
        self.assertIn("-trar5", argv)
        self.assertIn(f"-p{archives.SENTINEL_PASSWORD}", argv)

        run, calls = self._fake_run(RAR5_LISTING.format(extra=""))
        with patch.object(archives, "_run_tool", run):
            list_archive(self._rar(b"Rar!\x1a\x07\x00"), sevenzip=self.TOOL)
        self.assertIn("-trar", calls[0])

    def test_links_streams_and_encryption_in_a_rar_are_refused_without_extracting(self) -> None:
        cases = {
            "symbolic_link": self._member(symlink="..\\..\\Windows"),
            "hard_link": self._member(hardlink="S1.raw\\_FUNC001.DAT"),
            "alternate_stream": self._member(stream="+"),
            "encrypted": self._member(encrypted="+"),
        }
        for reason, member in cases.items():
            with self.subTest(reason=reason):
                run, calls = self._fake_run(RAR5_LISTING.format(extra=member))
                with patch.object(archives, "_run_tool", run):
                    with self.assertRaises(ArchiveError) as caught:
                        extract_archive(self._rar(), self.root / "out", sevenzip=self.TOOL)
                self.assertEqual("encrypted_archive" if reason == "encrypted" else "unsafe_listing",
                                 caught.exception.reason)
                self.assertEqual([{"name": "x.txt", "reason": reason}], caught.exception.rejected)
                self.assertEqual(["l"], [argv[1] for argv in calls])  # listed, never extracted
                self.assertFalse((self.root / "out").exists())

    def test_a_header_encrypted_rar_fails_as_encrypted(self) -> None:
        run, _calls = self._fake_run(
            "\n--\nPath = samples.rar\n", exit_code=2,
            stderr="ERROR: samples.rar : Cannot open encrypted archive. Wrong password?\n",
        )
        with patch.object(archives, "_run_tool", run):
            with self.assertRaises(ArchiveError) as caught:
                extract_archive(self._rar(), self.root / "out", sevenzip=self.TOOL)
        self.assertEqual("encrypted_archive", caught.exception.reason)
        self.assertIn("Wrong password", caught.exception.message)

    def test_listing_warnings_and_unreadable_listings_fail(self) -> None:
        warned = RAR5_LISTING.format(extra="").replace(
            "Type = Rar5\n", "Type = Rar5\nWARNINGS:\nThere are data after the end of archive\n"
        )
        run, _calls = self._fake_run(warned)
        with patch.object(archives, "_run_tool", run):
            with self.assertRaises(ArchiveError) as caught:
                list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual("sevenzip_warning", caught.exception.reason)

        garbled = RAR5_LISTING.format(extra="").replace("Size = 14\n", "Size = 14\nnot a property\n")
        run, _calls = self._fake_run(garbled)
        with patch.object(archives, "_run_tool", run):
            with self.assertRaises(ArchiveError) as caught:
                list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual("unparsable_listing", caught.exception.reason)

    def test_a_multi_line_archive_comment_is_a_value_not_a_warning(self) -> None:
        # 7-Zip prints a value with line breaks as 'Comment = ', '{', its lines, '}'. Each line was
        # read as a warning, so every RAR with a multi-line comment was refused.
        comment = "Comment = \n{\nline one\n\n----------\nPath = not a member\n}\n"
        commented = RAR5_LISTING.format(extra="").replace("Physical Size = 400\n",
                                                          "Physical Size = 400\n" + comment)
        run, _calls = self._fake_run(commented)
        with patch.object(archives, "_run_tool", run):
            listing = list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual(["S1.raw", "S1.raw\\_FUNC001.DAT"], [member.name for member in listing.members])

        unterminated = RAR5_LISTING.format(extra="").replace(
            "Physical Size = 400\n", "Physical Size = 400\nComment = \n{\nline one\n"
        ).replace("----------\n", "")
        run, _calls = self._fake_run(unterminated)
        with patch.object(archives, "_run_tool", run):
            with self.assertRaises(ArchiveError) as caught:
                list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual("unparsable_listing", caught.exception.reason)

    def test_property_lines_never_decide_why_7zip_failed(self) -> None:
        # Every zip and rar block carries 'Encrypted = -'. Searching the whole output for
        # 'encrypted' named every truncated download an encrypted archive, a permanent reason.
        truncated = RAR5_LISTING.format(extra="").replace(
            "Type = Rar5\n", "Type = Rar5\nERRORS:\nUnexpected end of archive\n"
        ) + "\n\nErrors: 1\n"
        run, _calls = self._fake_run(truncated, exit_code=2)
        with patch.object(archives, "_run_tool", run):
            with self.assertRaises(ArchiveError) as caught:
                list_archive(self._rar(), sevenzip=self.TOOL)
        self.assertEqual("corrupt_archive", caught.exception.reason)
        self.assertIn("Unexpected end of archive", caught.exception.message)

        extraction_stdout = (
            "\n7-Zip 25.01 (x64)\n\nExtracting archive: samples.rar\n--\nPath = samples.rar\n"
            "Type = Rar5\nEncrypted = -\nComment = \n{\nWrong password? Not a diagnosis.\n}\n\n"
            "Sub items Errors: 1\n\nArchives with Errors: 1\n"
        )
        cases = {
            "ERROR: CRC Failed : S1.mzML": "corrupt_archive",
            "ERROR: Data Error : S2.mzML": "corrupt_archive",
            "ERRORS:\nUnexpected end of archive\n\nERROR: Data Error : S2.mzML": "corrupt_archive",
            "ERROR: Data Error in encrypted file. Wrong password? : a.txt": "encrypted_archive",
            "ERROR: CRC Failed in encrypted file. Wrong password? : a.txt": "encrypted_archive",
            "ERROR: Wrong password : a.txt": "encrypted_archive",
            "ERROR: x.7z\nCannot open encrypted archive. Wrong password?\n\nERRORS:\nHeaders Error":
                "encrypted_archive",
            "ERRORS:\nHeaders Error": "corrupt_archive",
            "ERROR: x.7z\nOpen ERROR: Cannot open the file as [7z] archive\n\nERRORS:\n"
            "Unexpected end of archive": "corrupt_archive",
            "ERROR: x.7z\nOpen ERROR: Cannot open the file as [7z] archive": "not_an_archive",
            "": "sevenzip_failed",
        }
        for stderr, reason in cases.items():
            with self.subTest(stderr=stderr):
                error = archives._sevenzip_error(
                    archives._ToolRun(2, extraction_stdout, stderr, "", 0.01), "samples.rar",
                    "extraction",
                )
                self.assertEqual(reason, error.reason)
                self.assertNotIn("Encrypted = -", error.message)
        warned = archives._sevenzip_error(archives._ToolRun(1, extraction_stdout, "", "", 0.01),
                                          "samples.rar", "extraction")
        self.assertEqual("sevenzip_warning", warned.reason)


@needs_sevenzip
class SevenZipExtractionTests(_Workspace):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tool = find_sevenzip(str(SEVENZIP))

    def _source_tree(self) -> Path:
        source = self.root / "src"
        (source / "S1.raw").mkdir(parents=True)
        (source / "empty").mkdir()
        (source / "S1.raw" / "_FUNC001.DAT").write_bytes(b"spectra" * 1000)
        (source / "S1.raw" / "_HEADER.TXT").write_bytes(b"header")
        (source / "run.mzML").write_bytes(b"<mzML/>" * 300)
        return source

    def test_a_7z_made_by_7zip_extracts_with_the_tool_recorded(self) -> None:
        source = self._source_tree()
        archive = self.root / "samples.7z"
        _sevenzip("a", "-t7z", str(archive), "S1.raw", "run.mzML", "empty", cwd=source)
        record = extract_archive(archive, self.root / "out", sevenzip=self.tool,
                                 listing_directory=self.root / "provenance")
        self.assertEqual(_files_under(source), _files_under(self.root / "out"))
        self.assertTrue((self.root / "out" / "empty").is_dir())
        self.assertEqual("7z", record["format"])
        self.assertEqual("7-Zip", record["reader"])
        self.assertEqual(self.tool.version, record["tool"]["version"])
        self.assertEqual(hashlib.sha256(SEVENZIP.read_bytes()).hexdigest(),
                         record["tool"]["executable_sha256"])
        self.assertIn("-t7z", record["argv"])
        self.assertIn(f"-p{archives.SENTINEL_PASSWORD}", record["argv"])
        self.assertEqual("l", record["listing_argv"][1])
        self.assertEqual(0, record["exit_code"])
        self.assertTrue(record["crc_verified"])
        self.assertEqual("7z", record["reported_type"])

    def test_a_rar5_fixture_extracts_through_7zip(self) -> None:
        archive = _rar5_stored(self.root / "samples.rar", [
            ("S1.raw", None),
            ("S1.raw/_FUNC001.DAT", b"spectrum-bytes"),
            ("readme.txt", b"hello rar5\n"),
        ])
        record = extract_archive(archive, self.root / "out", sevenzip=self.tool)
        self.assertEqual({"S1.raw/_FUNC001.DAT": b"spectrum-bytes", "readme.txt": b"hello rar5\n"},
                         _files_under(self.root / "out"))
        self.assertEqual("rar", record["format"])
        self.assertEqual("rar5", record["signature"])
        self.assertIn("-trar5", record["argv"])
        self.assertEqual("Rar5", record["reported_type"])
        self.assertTrue(record["crc_verified"])

    def test_a_deflate64_zip_falls_back_to_7zip(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"hello world " * 3000)
        archive = self.root / "explorer.zip"
        _sevenzip("a", "-tzip", "-mm=Deflate64", str(archive), "a.txt", cwd=source)
        self.assertEqual(9, zipfile.ZipFile(archive).infolist()[0].compress_type)
        record = extract_archive(archive, self.root / "out", sevenzip=self.tool)
        self.assertEqual(_files_under(source), _files_under(self.root / "out"))
        self.assertEqual("7-Zip", record["reader"])
        self.assertEqual("zip_method_unsupported_by_stdlib:Deflate64", record["fallback_reason"])
        self.assertIn("-tzip", record["argv"])

    def test_encrypted_archives_fail_fast(self) -> None:
        source = self.root / "src"
        source.mkdir()
        (source / "a.txt").write_bytes(b"secret")
        made = {
            "headers.7z": ("-t7z", "-mhe=on"),
            "members.7z": ("-t7z",),
            "zipcrypto.zip": ("-tzip",),
        }
        for name, switches in made.items():
            _sevenzip("a", *switches, "-pcorrect horse", str(self.root / name), "a.txt", cwd=source)
        for name in made:
            with self.subTest(name=name):
                started = time.monotonic()
                with self.assertRaises(ArchiveError) as caught:
                    extract_archive(self.root / name, self.root / "out", sevenzip=self.tool)
                self.assertLess(time.monotonic() - started, 5)
                self.assertEqual("encrypted_archive", caught.exception.reason)
                self.assertFalse((self.root / "out").exists())
                self.assertFalse((self.root / "out.partial").exists())

    def test_damaged_archives_are_corrupt_not_encrypted(self) -> None:
        # A truncated download is the common failure, and a retry can mend it; encrypted_archive
        # is permanent. 7-Zip's 'Encrypted = -' property lines made every one of these encrypted.
        _truncated_zip(self.root / "partial.zip")
        whole = _rar5_stored(self.root / "whole.rar",
                             [(f"f{index}.bin", os.urandom(5000)) for index in range(4)]).read_bytes()
        (self.root / "partial.rar").write_bytes(whole[: len(whole) - 7000])
        flipped = _rar5_stored(self.root / "flipped.rar", [("S1.mzML", b"spectrum-bytes" * 200)])
        data = bytearray(flipped.read_bytes())
        data[data.find(b"spectrum-bytes") + 5] ^= 0xFF
        flipped.write_bytes(bytes(data))
        cases = {
            "partial.zip": ("auto", "Unexpected end of archive"),
            "partial.rar": ("auto", "Unexpected end of archive"),
            "flipped.rar": ("auto", "CRC Failed"),
        }
        _truncated_zip(self.root / "partial-7zip-reader.zip")
        cases["partial-7zip-reader.zip"] = ("sevenzip", "Unexpected end of archive")
        for name, (reader, evidence) in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(ArchiveError) as caught:
                    extract_archive(self.root / name, self.root / "out", sevenzip=self.tool,
                                    reader=reader)
                self.assertEqual("corrupt_archive", caught.exception.reason)
                self.assertIn(evidence, caught.exception.message)
                self.assertFalse((self.root / "out").exists())
                self.assertFalse((self.root / "out.partial").exists())

    def test_archives_with_multi_line_comments_extract(self) -> None:
        rar = _rar5_stored(self.root / "commented.rar", [("a.txt", b"hello")],
                           comment=b"line one\r\n\r\nline three")
        record = extract_archive(rar, self.root / "rar", sevenzip=self.tool)
        self.assertEqual({"a.txt": b"hello"}, _files_under(self.root / "rar"))
        self.assertEqual("Rar5", record["reported_type"])

        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as handle:
            handle.writestr("a.txt", b"hello")
            handle.comment = b"line one\r\nline two\r\n"
        (self.root / "commented.zip").write_bytes(buffer.getvalue())
        record = extract_archive(self.root / "commented.zip", self.root / "zip", sevenzip=self.tool,
                                 reader="sevenzip")
        self.assertEqual({"a.txt": b"hello"}, _files_under(self.root / "zip"))
        self.assertEqual("7-Zip", record["reader"])

    def test_a_directory_too_long_for_windows_is_refused_by_the_7zip_reader_too(self) -> None:
        # 7-Zip writes \\?\ paths and would create the folder; Python and the Console could not.
        parent = self.root / "deep"
        destination = parent / "d"
        room = _directory_room(destination, 247)
        if room < 2:
            self.skipTest("the temporary directory is already too deep for this test")
        parent.mkdir()
        archive = _write_zip(self.root / "long.zip", [("D" * (room + 1) + "/f.txt", b"x")])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, destination, sevenzip=self.tool, reader="sevenzip")
        self.assertEqual(["path_too_long"], [item["reason"] for item in caught.exception.rejected])
        self.assertEqual([], os.listdir(parent))

    def test_unsafe_names_are_refused_before_7zip_can_rewrite_them(self) -> None:
        # The probe: 7z x turned '../evil.txt' into 'evil.txt' and 'C:/abs.txt' into
        # 'C_/abs.txt' and exited 0. Through the 7-Zip reader these must be refused as well.
        archive = _write_zip(self.root / "evil.zip", [
            ("ok.txt", b"ok"), ("../evil.txt", b"x"), ("C:/abs.txt", b"x"), ("CON", b"x"),
            ("Ab.txt", b"1"), ("ab.TXT", b"2"),
        ])
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", sevenzip=self.tool, reader="sevenzip")
        self.assertEqual(
            {"../evil.txt": "parent_traversal", "C:/abs.txt": "drive_path", "CON": "reserved_name",
             "ab.TXT": "case_duplicate"},
            {item["name"].replace("\\", "/"): item["reason"] for item in caught.exception.rejected},
        )
        self.assertNothingWritten(self.root / "out", "evil.zip")

    def test_the_watchdog_stops_7zip_when_the_disk_reserve_is_breached(self) -> None:
        archive = self.root / "zeros.7z"
        _sevenzip("a", "-t7z", "-mx=1", "-sizeros.bin", str(archive), cwd=self.root,
                  data=bytes(50_000_000))
        calls = {"n": 0}

        def free_bytes(path: Path) -> int:
            # Plenty when the listing is admitted, none once 7-Zip is running.
            calls["n"] += 1
            return 10 ** 15 if calls["n"] == 1 else 0

        started = time.monotonic()
        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", sevenzip=self.tool, free_bytes=free_bytes,
                            limits=ExtractionLimits(reserve_bytes=1, watchdog_interval_seconds=0.05))
        self.assertEqual("insufficient_disk_space", caught.exception.reason)
        self.assertLess(time.monotonic() - started, 10)
        self.assertNothingWritten(self.root / "out", "zeros.7z")

    def test_a_failing_watchdog_stops_7zip_before_the_tree_is_removed(self) -> None:
        # The probe raised out of the supervision loop, 7-Zip kept writing, and the cleanup raced it:
        # the staging directory outlived the call.
        archive = self.root / "zeros.7z"
        _sevenzip("a", "-t7z", "-mx=1", "-sizeros.bin", str(archive), cwd=self.root,
                  data=bytes(50_000_000))
        calls = {"n": 0}

        def free_bytes(path: Path) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                return 10 ** 15
            raise OSError("the volume went away")

        with self.assertRaises(ArchiveError) as caught:
            extract_archive(archive, self.root / "out", sevenzip=self.tool, free_bytes=free_bytes,
                            limits=ExtractionLimits(reserve_bytes=1, watchdog_interval_seconds=0.05))
        self.assertEqual("watchdog_failed", caught.exception.reason)
        self.assertIn("the volume went away", caught.exception.message)
        self.assertNothingWritten(self.root / "out", "zeros.7z")


if __name__ == "__main__":
    unittest.main()
