"""A download lease opens every archive kind, verifies a study archive as the object it is, and attributes.

What these tests pin down is what stopped the campaign before a byte was analysed:

- 661 of the 839 declared units are Metabolomics Workbench units whose only listed file is a study
  archive with a published MD5. _verify_project_allowlist_checksums searched raw\\data for the archive's
  own name, found nothing (it had been extracted) and raised "resolved to 0 extracted files" after the
  whole download, so none of them could be leased; and extracted_files, matched by listed names only,
  was [] for every one;
- a MetaboLights unit of per-sample X.raw.zip files extracted to X.raw, which matched no sample name, and
  two such zips whose members sit at their root wrote _FUNC001.DAT over each other;
- .7z, .rar, bare .gz and .lzma objects were never opened;
- MB-POST's project tar of per-sample zips, each with a published MD5, was opened one level deep, and
  the zips were never inputs.

The network is a local HTTP server (the Workbench case, through the real RepositoryHttpClient) or an
injected client that serves fixed bytes. Nothing leaves the machine. Tests that need 7-Zip are skipped
where it is not installed; the RAR5 fixture is written from RARLAB's published format description with
stored entries, as in test_archives.
"""

from __future__ import annotations

import gzip
import hashlib
import http.server
import io
import json
import lzma
import os
import struct
import subprocess
import tarfile
import tempfile
import threading
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server, repository_reanalysis
from msdial_app.archives import ArchiveError, ExtractionLimits
from msdial_app.materials_methods import _input_integrity_sentence
from msdial_app.repository_reanalysis import (
    LEASE_STAGES,
    RepositoryFile,
    RepositoryHttpClient,
    RepositoryProject,
    _sample_file_names,
    create_download_lease,
    input_integrity_statement,
    project_from_dict,
    read_manifest,
)


SEVENZIP = Path(r"C:\Program Files\7-Zip\7z.exe")
HAVE_SEVENZIP = os.name == "nt" and SEVENZIP.is_file()
needs_sevenzip = unittest.skipUnless(HAVE_SEVENZIP, r"7-Zip is not installed at C:\Program Files\7-Zip")
# The default reserve is 20 GB of free space; these trees are a few kilobytes.
LIMITS = ExtractionLimits(reserve_bytes=0)


def _zip(entries: list[tuple[str, bytes]]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in entries:
            handle.writestr(name, data)
    return buffer.getvalue()


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


def _rar5_stored(entries: list[tuple[str, bytes]]) -> bytes:
    """A RAR5 archive of stored files (see test_archives._rar5_stored for the layout)."""
    out = b"Rar!\x1a\x07\x01\x00" + _rar5_header(1, 0, _vint(0))
    for name, data in entries:
        encoded = name.encode("utf-8")
        body = (
            _vint(0x0004) + _vint(len(data)) + _vint(0x20)
            + struct.pack("<I", zlib.crc32(data) & 0xFFFFFFFF) + _vint(0) + _vint(0)
        )
        out += _rar5_header(2, 0x0002, body + _vint(len(encoded)) + encoded, len(data)) + data
    return out + _rar5_header(5, 0, _vint(0))


class _Client:
    """Stands in for the network: fixed bytes per URL, written where the lease asks."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads
        self.requested: list[tuple[str, Path]] = []

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        self.requested.append((url, Path(destination)))
        data = self.payloads[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "resumed_from_bytes": 0,
        }


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args) -> None:  # noqa: D102 - quiet
        pass

    def do_GET(self) -> None:  # noqa: N802 - http.server's interface
        data = self.server.objects.get(self.path)
        if data is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


class _Server:
    """A repository on 127.0.0.1, so the lease downloads through the real RepositoryHttpClient."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.objects = objects
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self) -> "_Server":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"


def _workbench_handoff(archive_url: str, size: int, checksum: str, samples: list[str]) -> dict:
    """A Catalog handoff shaped as a Workbench unit: one study archive, samples naming their files."""
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabolomics_workbench",
        "accession": "ST000001",
        "analysis_unit_id": "an000001-neg",
        "technical_settings": {
            "separation": "LC-MS",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "target_omics": "Metabolomics",
            "untargeted": True,
        },
        "files": [
            {
                "path": "ST000001_Rawdata.zip",
                "role": "raw_archive",
                "size_bytes": size,
                "download_url": archive_url,
                "checksum": checksum,
            }
        ],
        "sample_metadata": [
            {"sample_id": sample, "raw_file": sample, "attributes": {}} for sample in samples
        ],
        "class_proposal": {"proposal_id": "class-1", "assignments": []},
        "blocking_reasons": [],
        "download_scope": {"file_count": 1, "bundle_bytes": size},
        "sample_count": len(samples),
    }


def _unit(
    files: list[RepositoryFile], samples: dict[str, str], repository: str = "metabolights"
) -> RepositoryProject:
    return RepositoryProject(
        repository=repository,
        accession="MTBLS-ARCHIVE",
        analysis_unit_id="unit-a",
        eligible=True,
        selection_status="eligible",
        files=files,
        total_download_bytes=sum(item.size_bytes for item in files) or 1,
        sample_metadata=[{"sample_id": sample_id, "raw_file": raw} for sample_id, raw in samples.items()],
    )


def _manifest_path(root: Path, repository: str = "metabolights", accession: str = "MTBLS-ARCHIVE",
                   unit: str = "unit-a") -> Path:
    return root / repository / accession / unit / "provenance" / "run-manifest.json"


def _rows(lease: dict) -> dict[str, dict]:
    return {Path(row["path"]).name: row for row in lease["input_lineage"]["rows"]}


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", LIMITS)
        limits.start()
        self.addCleanup(limits.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()


class AWorkbenchStudyArchiveIsVerifiedAsTheObjectItIs(_Workspace):
    SAMPLES = ["S1_neg", "S2_neg"]
    MEMBERS = {
        "ST000001/NEG/S1_neg.mzML": b"<mzML>negative one</mzML>",
        "ST000001/NEG/S2_neg.mzML": b"<mzML>negative two</mzML>",
        "ST000001/POS/S1_pos.mzML": b"<mzML>positive one</mzML>",
        "ST000001/README.txt": b"study notes",
    }

    def _lease(self, checksum: str | None = None) -> tuple[dict, bytes]:
        data = _zip(list(self.MEMBERS.items()))
        md5 = hashlib.md5(data).hexdigest() if checksum is None else checksum
        with _Server({"/studydata/ST000001_Rawdata.zip": data}) as server:
            url = server.url("/studydata/ST000001_Rawdata.zip")
            handoff = _workbench_handoff(url, len(data), md5, self.SAMPLES)
            project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)
            lease = create_download_lease(
                project_from_dict(project), self.root, 10_000_000, client=RepositoryHttpClient(timeout=10)
            )
        return lease, data

    def test_a_workbench_catalog_unit_leases_with_its_archive_verified_at_download(self) -> None:
        """THE REGRESSION: it raised 'Allow-listed file ... resolved to 0 extracted files'."""
        lease, data = self._lease()
        md5 = hashlib.md5(data).hexdigest()

        self.assertEqual("prepared", lease["status"])
        validation = lease["allowlist_checksum_validation"]
        self.assertEqual(
            {"required": True, "verified": 0, "skipped": 0, "archives_verified_at_download": 1},
            {key: validation[key]
             for key in ("required", "verified", "skipped", "archives_verified_at_download")},
        )
        self.assertEqual("as_downloaded", validation["archives"][0]["compared"])
        self.assertEqual(md5, validation["archives"][0]["declared"])
        self.assertEqual(
            ["S1_neg.mzML", "S2_neg.mzML"], sorted(Path(item).name for item in lease["extracted_files"])
        )
        self.assertEqual(2, lease["ignored_extracted_file_count"], "the positive unit's file and the README")
        self.assertEqual(
            ["S1_neg.mzML", "S2_neg.mzML"], sorted(Path(item).name for item in lease["input_candidates"])
        )
        download = lease["downloads"][0]
        self.assertTrue(download["declared_checksum_verified"])
        self.assertEqual("md5", download["declared_checksum_algorithm"])
        self.assertEqual("zip", download["archive"]["format"])

    def test_each_member_carries_the_verified_archive_md5_as_its_basis(self) -> None:
        lease, data = self._lease()
        extraction = lease["archive_extractions"][0]
        tsv = Path(extraction["members_tsv"]["path"])

        self.assertEqual(Path(lease["workspace"]) / "provenance", tsv.parent)
        self.assertEqual(hashlib.sha256(tsv.read_bytes()).hexdigest(), extraction["members_tsv"]["sha256"])
        self.assertEqual(lease["input_directory"], extraction["destination"])
        self.assertEqual("", extraction["placement"])
        self.assertEqual(hashlib.sha256(data).hexdigest(), extraction["archive_sha256"])
        row = _rows(lease)["S1_neg.mzML"]
        self.assertEqual("extracted_member", row["kind"])
        basis = row["basis"]
        self.assertEqual("archive_declared_checksum", basis["kind"])
        self.assertEqual(
            ("md5", hashlib.md5(data).hexdigest(), True),
            (basis["algorithm"], basis["value"], basis["verified"]),
        )
        self.assertEqual("extracted from an archive whose published MD5 matched", basis["statement"])
        self.assertEqual("ST000001/NEG/S1_neg.mzML", basis["listing"][0]["member"])
        self.assertEqual(str(tsv), basis["listing"][0]["members_tsv"])
        listed = {line.split("\t")[0] for line in tsv.read_text(encoding="utf-8").splitlines()[1:]}
        self.assertIn(basis["listing"][0]["member"], listed)

    def test_the_lease_records_its_stages_in_order(self) -> None:
        lease, _ = self._lease()
        stages = lease["lease_stages"]

        self.assertEqual(list(LEASE_STAGES), [entry["stage"] for entry in stages])
        status = {entry["stage"]: entry["status"] for entry in stages}
        self.assertEqual("not_used", status.pop("materialise"))
        self.assertEqual("not_used", status.pop("convert"))
        self.assertEqual({"completed"}, set(status.values()))
        by_stage = {entry["stage"]: entry for entry in stages}
        self.assertEqual(1, by_stage["verify_declared_checksums"]["md5_verified"])
        self.assertEqual(1, by_stage["extract"]["archives"])
        self.assertEqual(4, by_stage["extract"]["files"])
        self.assertEqual(1, by_stage["attribute"]["archives_verified_at_download"])

    def test_an_md5_mismatch_still_raises_before_anything_is_extracted(self) -> None:
        with self.assertRaisesRegex(ValueError, "MD5 checksum mismatch"):
            self._lease(checksum="0" * 32)
        manifest = read_manifest(
            _manifest_path(self.root, "metabolomics_workbench", "ST000001", "an000001-neg")
        )

        self.assertEqual("download_failed", manifest["status"])
        self.assertEqual("verify_declared_checksums", manifest["download_failure"]["stage"])
        self.assertEqual(
            {"fetch": "interrupted", "verify_declared_checksums": "failed"},
            {entry["stage"]: entry["status"] for entry in manifest["lease_stages"]},
        )
        self.assertEqual([], list(Path(manifest["input_directory"]).iterdir()))
        self.assertEqual([], manifest["archive_extractions"])

    def test_the_methods_text_says_how_the_inputs_are_vouched_for(self) -> None:
        """Never 'checksum-verified': the files themselves were compared with nothing (the gate's SUM-2)."""
        lease, _ = self._lease()

        statement = input_integrity_statement(lease)
        self.assertEqual(
            "Of the 2 analysis inputs, 2 were extracted from an archive whose published MD5 matched.",
            statement,
        )
        self.assertNotIn("checksum-verified", statement)
        self.assertEqual(
            " " + statement, _input_integrity_sentence({"repository_run_manifest": lease["manifest_path"]})
        )
        self.assertEqual("", _input_integrity_sentence({}))


class AStudyArchiveWithAnotherPublishedChecksum(_Workspace):
    def _project(self, checksum: str) -> tuple[RepositoryProject, dict[str, bytes]]:
        data = _zip([("study/S1.mzML", b"<mzML/>")])
        project = _unit(
            [RepositoryFile("study.zip", len(data), "https://example.org/study.zip", role="raw_archive",
                            checksum=checksum(data) if callable(checksum) else checksum)],
            {"S1": "S1.mzML"},
            repository="metabolomics_workbench",
        )
        return project, {"https://example.org/study.zip": data}

    def test_a_sha256_is_compared_with_the_download(self) -> None:
        project, payloads = self._project(lambda data: hashlib.sha256(data).hexdigest())
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))

        archive = lease["allowlist_checksum_validation"]["archives"][0]
        self.assertEqual(("sha256", "after_download"), (archive["declared_algorithm"], archive["compared"]))
        self.assertEqual("extracted from an archive whose published SHA-256 matched",
                         _rows(lease)["S1.mzML"]["basis"]["statement"])

    def test_a_sha256_that_does_not_match_raises(self) -> None:
        project, payloads = self._project("ab" * 32)
        with self.assertRaisesRegex(ValueError, "SHA256 checksum mismatch for study.zip"):
            create_download_lease(project, self.root, 10_000, client=_Client(payloads))


class PerSampleContainerArchivesAreAttributed(_Workspace):
    def _waters(self, sample: str) -> bytes:
        # Stored without their folder, as a per-sample zip of a Waters .raw often is.
        return _zip(
            [("_FUNC001.DAT", f"{sample} spectra".encode()), ("_HEADER.TXT", f"{sample} header".encode())]
        )

    def test_a_metabolights_raw_zip_unit_is_attributed(self) -> None:
        payloads = {
            f"https://example.org/FILES/{sample}.raw.zip": self._waters(sample) for sample in ("S1", "S2")
        }
        project = _unit(
            [RepositoryFile(f"FILES/{sample}.raw.zip", 10, f"https://example.org/FILES/{sample}.raw.zip")
             for sample in ("S1", "S2")],
            {"sample-1": "S1.raw.zip", "sample-2": "S2.raw.zip"},
        )
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))

        self.assertEqual(["S1.raw", "S2.raw"], sorted(Path(item).name for item in lease["input_candidates"]))
        rows = _rows(lease)
        self.assertEqual("archived_container", rows["S1.raw"]["kind"])
        self.assertEqual("sample-1", rows["S1.raw"]["sample_id"])
        self.assertEqual(["FILES/S1.raw.zip"], rows["S1.raw"]["declared_names"])
        self.assertEqual("archive_download_hash", rows["S1.raw"]["basis"]["kind"])
        self.assertEqual("S1.raw", rows["S1.raw"]["basis"]["listing"][0]["container"])
        self.assertEqual(4, len(lease["extracted_files"]))
        records = {record["archive_name"]: record for record in lease["archive_extractions"]}
        self.assertEqual("container_stem", records["S1.raw.zip"]["destination_rule"])
        self.assertEqual("S1.raw", records["S1.raw.zip"]["container_path"])
        self.assertEqual(
            "Of the 2 analysis inputs, 2 were extracted from an archive for which no checksum was published, "
            "identified only by its SHA-256.",
            input_integrity_statement(lease),
        )

    def test_unrooted_per_sample_zips_do_not_collide(self) -> None:
        """THE REGRESSION: both zips wrote _FUNC001.DAT into the data root, and the second won."""
        payloads = {
            f"https://example.org/FILES/{sample}.raw.zip": self._waters(sample) for sample in ("S1", "S2")
        }
        project = _unit(
            [RepositoryFile(f"FILES/{sample}.raw.zip", 10, f"https://example.org/FILES/{sample}.raw.zip")
             for sample in ("S1", "S2")],
            {"sample-1": "S1.raw.zip", "sample-2": "S2.raw.zip"},
        )
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))
        data = Path(lease["input_directory"])

        self.assertEqual(b"S1 spectra", (data / "S1.raw" / "_FUNC001.DAT").read_bytes())
        self.assertEqual(b"S2 spectra", (data / "S2.raw" / "_FUNC001.DAT").read_bytes())
        self.assertFalse((data / "_FUNC001.DAT").exists())

    def test_a_container_expands_in_the_directory_it_was_listed_in(self) -> None:
        """Two archives of one name in two directories download apart and expand apart."""
        payloads = {
            "https://example.org/FILES/pos/S1.raw.zip": self._waters("pos"),
            "https://example.org/FILES/neg/S1.raw.zip": self._waters("neg"),
        }
        project = _unit(
            [RepositoryFile("FILES/pos/S1.raw.zip", 10, "https://example.org/FILES/pos/S1.raw.zip"),
             RepositoryFile("FILES/neg/S1.raw.zip", 10, "https://example.org/FILES/neg/S1.raw.zip")],
            {"sample-1": "S1.raw.zip"},
        )
        client = _Client(payloads)
        lease = create_download_lease(project, self.root, 10_000, client=client)
        data = Path(lease["input_directory"])

        self.assertEqual(2, len({destination for _url, destination in client.requested}))
        self.assertEqual(b"pos spectra", (data / "pos" / "S1.raw" / "_FUNC001.DAT").read_bytes())
        self.assertEqual(b"neg spectra", (data / "neg" / "S1.raw" / "_FUNC001.DAT").read_bytes())
        self.assertEqual(
            ["neg/S1.raw", "pos/S1.raw"],
            sorted(Path(item).relative_to(data).as_posix() for item in lease["input_candidates"]),
        )

    def test_a_member_too_long_where_it_lands_is_refused_before_it_moves(self) -> None:
        """archives.py checks lengths under its staging directory; the listed directory can be deeper."""
        # Resolved, as the lease resolves its workspace root (a temporary directory may be an 8.3 name).
        raw = self.root.resolve() / "metabolights" / "MTBLS-ARCHIVE" / "unit-a" / "raw"
        member = "S1.raw/_FUNC001.DAT"
        # Exactly long enough under raw\x1.partial, fifteen characters too long under raw\data\RAW_FILES\...
        limit = len(str(raw / "x1.partial")) + 1 + len(member)
        url = "https://example.org/FILES/RAW_FILES/pos/deeper/S1.raw.zip"
        project = _unit(
            [RepositoryFile("FILES/RAW_FILES/pos/deeper/S1.raw.zip", 10, url)], {"a": "S1.raw.zip"}
        )
        limits = ExtractionLimits(reserve_bytes=0, max_path_length=limit, max_directory_length=limit)
        with patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", limits):
            with self.assertRaises(ArchiveError) as caught:
                create_download_lease(project, self.root, 10_000, client=_Client({url: self._waters("S1")}))
        manifest = read_manifest(_manifest_path(self.root))

        self.assertEqual("unsafe_listing", caught.exception.reason)
        self.assertIn("before it moved under", caught.exception.message,
                      "extracted, then refused where it lands")
        self.assertEqual({"path_too_long"}, {item["reason"] for item in caught.exception.rejected})
        self.assertEqual("extract", manifest["download_failure"]["stage"])
        self.assertEqual([], [path for path in (raw / "data").rglob("*") if path.is_file()])
        self.assertFalse((raw / "x1").exists())

    def test_two_archives_with_the_same_bytes_keep_a_listing_each(self) -> None:
        """The listing is named by the archive's sha256; a second copy must not write over the first's."""
        blank = self._waters("blank")
        payloads = {"https://example.org/FILES/pos/Blank.raw.zip": blank,
                    "https://example.org/FILES/neg/Blank_neg.raw.zip": blank}
        project = _unit(
            [RepositoryFile("FILES/pos/Blank.raw.zip", 10, "https://example.org/FILES/pos/Blank.raw.zip"),
             RepositoryFile("FILES/neg/Blank_neg.raw.zip", 10,
                            "https://example.org/FILES/neg/Blank_neg.raw.zip")],
            {"blank-pos": "Blank.raw.zip", "blank-neg": "Blank_neg.raw.zip"},
        )
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))

        listings = [Path(record["members_tsv"]["path"]) for record in lease["archive_extractions"]]
        self.assertEqual(2, len(set(listings)))
        for record, listing in zip(lease["archive_extractions"], listings):
            self.assertEqual(
                hashlib.sha256(listing.read_bytes()).hexdigest(), record["members_tsv"]["sha256"]
            )
            self.assertIn(record["archive_name"], listing.read_text(encoding="utf-8"))

    def test_the_alias_is_one_rule_for_samples(self) -> None:
        exact, _stems = _sample_file_names(
            _unit([], {"a": "FILES/X.d.zip", "b": "Y.raw.rar", "c": "z.mzXML.lzma"})
        )
        self.assertTrue({"x.d.zip", "x.d", "y.raw.rar", "y.raw", "z.mzxml.lzma", "z.mzxml"} <= exact)

    def test_a_file_inside_a_vendor_folder_is_never_opened_as_an_archive(self) -> None:
        inner = _zip([("method.xml", b"<method/>")])
        payloads = {
            "https://example.org/raw/S1.d/AcqData/MSScan.bin": b"scan",
            "https://example.org/raw/S1.d/AcqData/methods.zip": inner,
        }
        project = _unit(
            [RepositoryFile("raw/S1.d/AcqData/MSScan.bin", 4,
                            "https://example.org/raw/S1.d/AcqData/MSScan.bin"),
             RepositoryFile("raw/S1.d/AcqData/methods.zip", len(inner),
                            "https://example.org/raw/S1.d/AcqData/methods.zip")],
            {"sample-1": "S1.d"},
            repository="metabobank",
        )
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))
        data = Path(lease["input_directory"])

        self.assertEqual(inner, (data / "raw" / "S1.d" / "AcqData" / "methods.zip").read_bytes())
        self.assertEqual([], lease["archive_extractions"])
        self.assertEqual(["S1.d"], [Path(item).name for item in lease["input_candidates"]])


class PerSampleZipsInsideAProjectTar(_Workspace):
    """MB-POST ships one project tar holding per-sample zips, and publishes an MD5 for each zip."""

    def _lease(self, corrupt: str = "") -> dict:
        zips = {
            sample: _zip([("_FUNC001.DAT", f"{sample} spectra".encode()), ("_HEADER.TXT", b"header")])
            for sample in ("S1", "S2")
        }
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as handle:
            for sample, data in zips.items():
                info = tarfile.TarInfo(f"MB-POST_files_MPST-NESTED.0/FILES/{sample}.raw.zip")
                info.size = len(data)
                handle.addfile(info, io.BytesIO(data))
        url = "https://example.org/MPST-NESTED/files.tar"
        files = [
            RepositoryFile(
                f"FILES/{sample}.raw.zip", len(data), url,
                checksum=("0" * 32 if sample == corrupt else hashlib.md5(data).hexdigest()),
            )
            for sample, data in zips.items()
        ]
        project = _unit(files, {"sample-1": "S1.raw.zip", "sample-2": "S2.raw.zip"}, repository="mb_post")
        project.accession = "MPST-NESTED"
        self.zips = zips
        return create_download_lease(project, self.root, 10_000_000, client=_Client({url: buffer.getvalue()}))

    def test_each_zip_is_verified_before_it_expands_and_vouches_for_its_container(self) -> None:
        lease = self._lease()

        validation = lease["allowlist_checksum_validation"]
        self.assertEqual(
            (2, 0, 0),
            (validation["verified"], validation["skipped"], validation["archives_verified_at_download"]),
        )
        self.assertEqual({"before_expansion"}, {item["compared"] for item in validation["archives"]})
        self.assertEqual(["S1.raw", "S2.raw"], sorted(Path(item).name for item in lease["input_candidates"]))
        row = _rows(lease)["S1.raw"]
        self.assertEqual("archived_container", row["kind"])
        self.assertEqual("archive_declared_checksum", row["basis"]["kind"])
        self.assertEqual(hashlib.md5(self.zips["S1"]).hexdigest(), row["basis"]["value"])
        self.assertTrue(row["basis"]["archives"][0]["archive_path"].endswith("FILES/S1.raw.zip"))
        nested = lease["archive_extractions"][0]["nested"]
        self.assertTrue(all(item["declared_checksum_verified"] for item in nested))

    def test_a_zip_whose_md5_does_not_match_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "MD5 checksum mismatch for FILES/S2.raw.zip"):
            self._lease(corrupt="S2")


class EveryArchiveKindIsExtracted(_Workspace):
    def _lease_one(self, name: str, data: bytes, samples: dict[str, str], role: str = "raw") -> dict:
        url = f"https://example.org/FILES/{name}"
        project = _unit([RepositoryFile(f"FILES/{name}", len(data), url, role=role)], samples)
        return create_download_lease(project, self.root, 10_000_000, client=_Client({url: data}))

    def _assert_extracted(self, lease: dict, kind: str, expected: dict[str, bytes]) -> None:
        data = Path(lease["input_directory"])
        self.assertEqual("prepared", lease["status"])
        self.assertEqual(kind, lease["archive_extractions"][0]["format"])
        for relative, content in expected.items():
            self.assertEqual(content, (data / relative).read_bytes())
        self.assertEqual(sorted(Path(item).name for item in expected),
                         sorted(Path(item).name for item in lease["input_candidates"]))

    @needs_sevenzip
    def test_a_7z_study_archive_is_extracted(self) -> None:
        source = self.root / "source"
        (source / "study").mkdir(parents=True)
        (source / "study" / "S1.mzML").write_bytes(b"<mzML>one</mzML>")
        (source / "study" / "S2.mzML").write_bytes(b"<mzML>two</mzML>")
        archive = self.root / "study.7z"
        subprocess.run([str(SEVENZIP), "a", "-t7z", str(archive), "study"], cwd=source,
                       stdin=subprocess.DEVNULL, capture_output=True, check=True)
        lease = self._lease_one("study.7z", archive.read_bytes(), {"a": "S1.mzML", "b": "S2.mzML"},
                                role="raw_archive")
        self._assert_extracted(lease, "7z", {"study/S1.mzML": b"<mzML>one</mzML>",
                                             "study/S2.mzML": b"<mzML>two</mzML>"})
        self.assertEqual("7-Zip", lease["archive_extractions"][0]["reader"])

    @needs_sevenzip
    def test_a_rar_study_archive_is_extracted(self) -> None:
        rar = _rar5_stored([("study/S1.mzML", b"<mzML>rar one</mzML>")])
        lease = self._lease_one("study.rar", rar, {"a": "S1.mzML"}, role="raw_archive")
        self._assert_extracted(lease, "rar", {"study/S1.mzML": b"<mzML>rar one</mzML>"})

    def test_a_bare_gz_is_extracted_where_it_was_listed(self) -> None:
        """A bare .gz used to be routed as an archive and never opened."""
        lease = self._lease_one("S1.mzML.gz", gzip.compress(b"<mzML>gz</mzML>"), {"a": "S1.mzML"})
        self._assert_extracted(lease, "gz", {"S1.mzML": b"<mzML>gz</mzML>"})
        self.assertEqual("stream_file", lease["archive_extractions"][0]["destination_rule"])

    def test_an_lzma_alone_file_is_extracted(self) -> None:
        payload = b"<mzML>lzma</mzML>" * 100
        packed = lzma.compress(payload, format=lzma.FORMAT_ALONE)
        lease = self._lease_one("S1.mzML.lzma", packed, {"a": "S1.mzML"})
        self._assert_extracted(lease, "lzma", {"S1.mzML": payload})
        record = lease["archive_extractions"][0]
        self.assertEqual(
            ("lzma_alone", "none", False), (record["signature"], record["integrity"], record["crc_verified"])
        )
        self.assertIn("no_member_integrity", {warning["kind"] for warning in lease["archive_warnings"]})

    def test_an_mtbls688_mzxml_lzma_is_extracted_and_waits_for_conversion(self) -> None:
        """MTBLS688 publishes only x.mzXML.lzma. It comes out as x.mzXML, which no lease converts yet."""
        payload = b"<mzXML/>" * 50
        with self.assertRaisesRegex(ValueError, "did not contain an MS-DIAL input"):
            packed = lzma.compress(payload, format=lzma.FORMAT_ALONE)
            self._lease_one("x.mzXML.lzma", packed, {"a": "x.mzXML.lzma"})
        manifest = read_manifest(_manifest_path(self.root))

        self.assertEqual("attribute", manifest["download_failure"]["stage"])
        self.assertEqual("x.mzXML", manifest["archive_extractions"][0]["container_path"])
        self.assertEqual(payload, (Path(manifest["input_directory"]) / "x.mzXML").read_bytes())

    def test_an_html_page_saved_as_a_zip_is_a_failed_download(self) -> None:
        page = b"<!DOCTYPE html><html><body>502 Bad Gateway</body></html>"
        with self.assertRaises(ArchiveError) as caught:
            self._lease_one("S1.raw.zip", page, {"a": "S1.raw.zip"})
        manifest = read_manifest(_manifest_path(self.root))

        self.assertEqual("not_an_archive", caught.exception.reason)
        self.assertEqual("download_failed", manifest["status"])
        self.assertEqual("verify_declared_checksums", manifest["download_failure"]["stage"])
        self.assertEqual("not_an_archive", manifest["download_failure"]["archive_failure"]["reason"])


class NothingIsOverwritten(_Workspace):
    def _study(
        self, entries: list[tuple[str, bytes]], name: str = "study.zip"
    ) -> tuple[RepositoryFile, dict[str, bytes]]:
        data = _zip(entries)
        url = f"https://example.org/{name}"
        return RepositoryFile(name, len(data), url, role="raw_archive"), {url: data}

    def test_a_retried_lease_finds_its_files_already_in_place(self) -> None:
        item, payloads = self._study([("study/S1.mzML", b"one"), ("study/S2.mzML", b"two")])
        project = _unit([item], {"a": "S1.mzML", "b": "S2.mzML"})
        create_download_lease(project, self.root, 10_000, client=_Client(payloads))
        again = create_download_lease(project, self.root, 10_000, client=_Client(payloads))

        self.assertEqual("prepared", again["status"])
        self.assertEqual(2, again["archive_extractions"][0]["merge"]["already_present_files"])
        self.assertEqual(2, len(again["input_candidates"]))

    def test_a_changed_file_in_the_way_refuses_the_archive_and_is_kept(self) -> None:
        item, payloads = self._study([("study/S1.mzML", b"one")])
        project = _unit([item], {"a": "S1.mzML"})
        first = create_download_lease(project, self.root, 10_000, client=_Client(payloads))
        target = Path(first["input_directory"]) / "study" / "S1.mzML"
        target.write_bytes(b"edited in place")

        with self.assertRaises(ArchiveError) as caught:
            create_download_lease(project, self.root, 10_000, client=_Client(payloads))
        manifest = read_manifest(first["manifest_path"])

        self.assertEqual("extraction_collision", caught.exception.reason)
        self.assertEqual(b"edited in place", target.read_bytes())
        self.assertEqual("extract", manifest["download_failure"]["stage"])
        self.assertFalse((Path(first["raw_directory"]) / "x1").exists(), "the staging tree is removed")

    def test_two_archives_that_disagree_about_one_path_collide(self) -> None:
        first, first_payload = self._study([("study/S1.mzML", b"from one")], "one.zip")
        second, second_payload = self._study([("study/S1.mzML", b"from two")], "two.zip")
        project = _unit([first, second], {"a": "S1.mzML"})

        with self.assertRaises(ArchiveError) as caught:
            create_download_lease(
                project, self.root, 10_000, client=_Client({**first_payload, **second_payload})
            )

        self.assertEqual("extraction_collision", caught.exception.reason)
        manifest = read_manifest(_manifest_path(self.root))
        self.assertEqual(b"from one", (Path(manifest["input_directory"]) / "study" / "S1.mzML").read_bytes())
        self.assertEqual(1, len(manifest["archive_extractions"]), "the first archive's record survives")


class AUnitOfFilesRecordsWhatItAlwaysDid(_Workspace):
    """An MTBLS2207-shaped unit: mzML files listed one by one, no checksums, nothing to extract."""

    # What the lease wrote before it had stages, for such a unit (8019539).
    LEGACY_KEYS = {
        "schema", "created_at", "status", "project", "workspace", "raw_directory", "input_directory",
        "output_directory", "downloads", "extracted_files", "ignored_extracted_file_count",
        "allowlist_checksum_validation", "input_candidates", "ignored_input_candidate_count",
        "input_lineage", "analysis_input_path", "execution_allowed", "cleanup_allowed",
        "raw_retention_policy", "download_started_at", "download_completed_at", "lease_owner",
        "repository_metadata_file", "sample_metadata_file", "manifest_path",
    }

    def test_the_manifest_is_the_old_one_plus_its_stages(self) -> None:
        payloads = {f"https://example.org/FILES/{name}": name.encode() for name in ("a.mzML", "b.mzML")}
        project = _unit(
            [RepositoryFile(f"FILES/{name}", 6, f"https://example.org/FILES/{name}", role="converted")
             for name in ("a.mzML", "b.mzML")],
            {"a": "a.mzML", "b": "b.mzML"},
        )
        lease = create_download_lease(project, self.root, 10_000, client=_Client(payloads))

        self.assertEqual({"lease_stages", "archive_extractions"}, set(lease) - self.LEGACY_KEYS)
        self.assertEqual(set(), self.LEGACY_KEYS - set(lease))
        self.assertEqual([], lease["archive_extractions"])
        self.assertEqual(
            {"required": False, "verified": 0, "skipped": 2}, lease["allowlist_checksum_validation"]
        )
        self.assertEqual([], lease["extracted_files"])
        for download in lease["downloads"]:
            self.assertEqual(
                {"path", "size_bytes", "sha256", "md5", "resumed_from_bytes", "source_url",
                 "declared_checksum"},
                set(download),
            )
        for row in lease["input_lineage"]["rows"]:
            self.assertEqual("file", row["kind"])
            self.assertNotIn("basis", row)
        self.assertEqual("", input_integrity_statement(lease))
        self.assertEqual(
            json.loads(json.dumps(lease["lease_stages"])),
            read_manifest(lease["manifest_path"])["lease_stages"],
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
