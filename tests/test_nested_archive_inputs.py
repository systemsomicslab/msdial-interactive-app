"""A declared analysis input that is an archive inside the download is the sample its contents are.

MB-POST MPST000213 (JEOL AccuTOF LC-Express, positive; unit 334ab3e2f0e7600f4290) failed its lease three times in
the MB-POST preflight campaign of 2026-10-14: "declares its analysis inputs, and 3 of its 3 declared analysis
inputs are not in the download: Rubroshorea_leprosula_from_Borneo_ID_01.zip, ..._02.zip, ..._03.zip". Its one
download object is the project tar, and the Catalog declares, for each sample row, the per-sample zip inside it
(kind file). The extract stage expands a zip inside a downloaded archive in place, under the outer archive's
safety rules and budget (archives._expand_nested), so the declared zip was gone once its contents were on disk,
and only a path equal to the zip's was looked for.

What is pinned here, with synthetic tars of synthetic zips (nothing leaves the machine):

- the contents of a declared zip in the tar are that sample's input, wherever its destination rule put them (its
  own folder kept, a folder made for it, a vendor folder inside it), and the CSV gives them that sample's row;
- the manifest records the nesting (declared_archive_inputs; the nested extraction record names the declared input);
- the inner zip is held to the outer archive's rules: an unsafe member name, the member budget, its published MD5;
- one sample's encodings side by side inside one zip, in one folder and of one stem, are one sample's (the one
  encoding rule), whatever their rank: S1.cdf beside S1.mzML, and S1.d beside S1.wiff (a tie, the first by path);
  so are one name's copies in several folders of the zip, which the user decided on 2026-10-15 are copies (0.5.40;
  until then an undecided exception that refused the zip): one is used by the rule and the others are named in
  declared_archive_inputs (copies_unused, copy_rule) and in a warning; two runs of two names, both polarities'
  folders, and folders one of which may state a polarity by a mark the rule does not read (Posi/ and Nega/, ESI+/
  and ESI-/: review of 0.5.40) are still one declared input found more than once, and refused; an archived
  container still counts every
  input it matched; a zip holding nothing MS-DIAL opens (MPST000213's JEOL msAxel project) says what it held (its
  disposition, an exclusion, is pinned in test_declared_inputs_disposition);
- a place is matched by the full path under the data root only, and recorded as it is written on disk;
- an mzXML inside a declared zip is converted in a campaign, as a declared mzXML is;
- where no record says where a declared per-sample zip went (one the unit downloads itself), its sample decides;
- a store record made before 0.5.39, shaped as MPST000213's is, says where each zip went.
"""

from __future__ import annotations

import hashlib
import io
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server, repository_reanalysis
from msdial_app.archives import ArchiveError, ExtractionLimits
from msdial_app.repository_analysis_rows import build_repository_analysis_rows
from msdial_app.repository_reanalysis import (
    RepositoryFile,
    RepositoryProject,
    _file_key,
    _filter_inputs_by_project_allowlist,
    create_download_lease,
    declared_analysis_inputs,
    declared_archive_containers,
    declared_archive_expansions,
    match_declared_inputs,
    project_from_dict,
    read_manifest,
)

from test_folder_inputs import _set_preflight
from test_mzml_encoding import dda_spectra, mzml
from test_mzxml_conversion import dda_32
from test_raw_metadata_preflight import _APPROVAL

ACCESSION = "MPST-NEST"
TAR_URL = f"https://repository.massbank.jp/api/download/{ACCESSION}.0"
TAR_ROOT = f"MB-POST_files_{ACCESSION}.0"
# The default reserve is 20 GB of free space; these trees are a few kilobytes.
LIMITS = ExtractionLimits(reserve_bytes=0)
MZML = mzml(dda_spectra(2))


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as handle:
        for name, data in entries.items():
            handle.writestr(zipfile.ZipInfo(name, date_time=(2026, 8, 23, 7, 3, 8)), data)
    return buffer.getvalue()


def _tar(members: dict[str, bytes]) -> bytes:
    """The project tar MB-POST serves for an accession: every file under MB-POST_files_<accession>.0/."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as handle:
        for name, data in members.items():
            info = tarfile.TarInfo(f"{TAR_ROOT}/{name}")
            info.size = len(data)
            handle.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def _handoff(
    zips: dict[str, bytes], tar: bytes, *, checksums: dict[str, str] | None = None, ion_mode: str = "Positive"
) -> dict:
    """A Catalog handoff shaped as MPST000213's: each sample row declares the per-sample zip in the project tar."""
    checksums = checksums or {}
    stems = [name[: -len(".zip")] for name in zips]
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "mb_post",
        "accession": ACCESSION,
        "analysis_unit_id": "nested-archive-unit",
        "title": ACCESSION,
        "technical_settings": {
            "separation": "LC-MS",
            "ion_mode": ion_mode,
            "acquisition_mode": "DDA",
            "untargeted": True,
            "target_omics": "Metabolomics",
        },
        "files": [
            {
                "path": name,
                "role": "raw",
                "size_bytes": len(data),
                "checksum": checksums.get(name, hashlib.md5(data).hexdigest()),
                "download_url": TAR_URL,
                "sample_id": stem,
            }
            for (name, data), stem in zip(zips.items(), stems)
        ],
        "download_scope": {
            "kind": "accession_bundle_with_file_allowlist",
            "file_count": len(zips),
            "analysis_file_count": len(zips),
            "bundle_bytes": len(tar),
        },
        "sample_count": len(zips),
        "analytical_sample_count": len(zips),
        "analysis_input_model": "one-input-per-sample.v1",
        "analysis_inputs_declared": True,
        "analysis_input_count": len(zips),
        "analysis_inputs": [
            {
                "path": name, "kind": "file", "suffix": "", "format": "", "member_count": 1,
                "size_bytes": len(data), "sample_id": stem, "requires_conversion": False, "conversion_target": "",
            }
            for (name, data), stem in zip(zips.items(), stems)
        ],
        "analysis_input_issues": [],
        "sample_metadata": [
            {"sample_id": stem, "raw_file": name, "attributes": {}} for name, stem in zip(zips, stems)
        ],
        "class_proposal": None,
        "blocking_reasons": [],
    }


class _Client:
    """Stands in for the network: fixed bytes per URL."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
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


def _rows(manifest: dict) -> dict[str, dict]:
    return {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}


class _Workspace(unittest.TestCase):
    limits = LIMITS

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", self.limits)
        limits.start()
        self.addCleanup(limits.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def project(self, zips: dict[str, bytes], extras: dict[str, bytes] | None = None, **options):
        tar = _tar({**zips, **(extras or {})})
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(_handoff(zips, tar, **options))
        typed = project_from_dict(project)
        typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
        return typed, tar

    def lease(self, zips: dict[str, bytes], extras: dict[str, bytes] | None = None, *, lease_options=None, **options):
        typed, tar = self.project(zips, extras, **options)
        lease = create_download_lease(
            typed, self.root, 10**9, client=_Client({TAR_URL: tar}), **(lease_options or {})
        )
        return read_manifest(lease["manifest_path"])

    def fresh_root(self, name: str) -> None:
        """A workspace of its own for one subtest: a lease keeps what an earlier lease of its unit left in the tree,
        so a second synthetic tar in one workspace would be read beside the first one's files."""
        self.root = Path(self._temporary.name).resolve() / name
        self.root.mkdir()

    def relative(self, manifest: dict, paths: list[str]) -> list[str]:
        data = Path(manifest["input_directory"])
        return sorted(Path(item).relative_to(data).as_posix() for item in paths)


class TheContentsOfADeclaredZipInTheProjectTarAreItsSample(_Workspace):
    """THE REGRESSION: '3 of its 3 declared analysis inputs are not in the download' after the whole download."""

    ZIPS = {
        # Each zip holds exactly its own folder, as MPST000213's do: nested_rooted.
        "S1.zip": _zip({"S1/S1.mzML": MZML}),
        # A zip whose members sit at its root expands into a folder of its name: nested_directory.
        "S2.zip": _zip({"S2.mzML": MZML}),
        # A vendor folder inside the zip is the input, as one folder.
        "S3.zip": _zip({"S3.d/AcqData/MSScan.bin": b"scan", "S3.d/AcqData/MSPeak.bin": b"peak"}),
    }
    EXTRAS = {
        "Sample_Metadata.xlsx": b"not a sample",
        "Extracted_Raw_Sample_Spectra.zip": _zip({"Extracted_Raw_Sample_Spectra/DIP_1.txt": b"peak list"}),
    }

    def test_each_declared_zip_gives_its_samples_input(self) -> None:
        manifest = self.lease(self.ZIPS, self.EXTRAS)

        self.assertEqual("prepared", manifest["status"])
        self.assertEqual(
            [f"{TAR_ROOT}/S1/S1.mzML", f"{TAR_ROOT}/S2/S2.mzML", f"{TAR_ROOT}/S3/S3.d"],
            self.relative(manifest, manifest["input_candidates"]),
        )
        rows = _rows(manifest)
        self.assertEqual(
            {"S1.mzML": "S1", "S2.mzML": "S2", "S3.d": "S3"},
            {name: row["sample_id"] for name, row in rows.items()},
        )

    def test_each_zip_was_verified_against_its_published_md5_before_it_expanded(self) -> None:
        manifest = self.lease(self.ZIPS, self.EXTRAS)

        validation = manifest["allowlist_checksum_validation"]
        self.assertEqual((3, 0), (validation["verified"], validation["skipped"]))
        self.assertEqual({"before_expansion"}, {item["compared"] for item in validation["archives"]})
        basis = _rows(manifest)["S1.mzML"]["basis"]
        self.assertEqual(("archive_declared_checksum", hashlib.md5(self.ZIPS["S1.zip"]).hexdigest()),
                         (basis["kind"], basis["value"]))

    def test_the_manifest_records_where_each_declared_zip_expanded_and_what_it_gave(self) -> None:
        manifest = self.lease(self.ZIPS, self.EXTRAS)

        record = {item["declared_path"]: item for item in manifest["declared_archive_inputs"]}
        self.assertEqual(["S1.zip", "S2.zip", "S3.zip"], sorted(record))
        s1 = record["S1.zip"]
        self.assertEqual(
            {
                "sample_id": "S1",
                "archive_path": f"{TAR_ROOT}/S1.zip",
                "inside": f"{ACCESSION}.tar",
                "depth": 2,
                "format": "zip",
                "destination_rule": "nested_rooted",
                "expanded_to": [f"{TAR_ROOT}/S1"],
                "file_count": 1,
                "archive_sha256": hashlib.sha256(self.ZIPS["S1.zip"]).hexdigest(),
                "archive_md5": hashlib.md5(self.ZIPS["S1.zip"]).hexdigest(),
                "declared_checksum_verified": True,
                "inputs": [f"{TAR_ROOT}/S1/S1.mzML"],
            },
            {key: s1[key] for key in (
                "sample_id", "archive_path", "inside", "depth", "format", "destination_rule", "expanded_to",
                "file_count", "archive_sha256", "archive_md5", "declared_checksum_verified", "inputs",
            )},
        )
        self.assertEqual(("nested_directory", [f"{TAR_ROOT}/S2"]),
                         (record["S2.zip"]["destination_rule"], record["S2.zip"]["expanded_to"]))
        self.assertEqual([f"{TAR_ROOT}/S3/S3.d"], record["S3.zip"]["inputs"])
        nested = {item["archive_name"]: item for item in manifest["archive_extractions"][0]["nested"]}
        self.assertEqual("S1.zip", nested["S1.zip"]["declared_input"])
        self.assertNotIn("declared_input", nested["Extracted_Raw_Sample_Spectra.zip"])
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual(3, attribute["declared_archive_inputs"])

    def test_the_analysis_csv_gives_each_input_its_declared_samples_row(self) -> None:
        manifest = self.lease(self.ZIPS, self.EXTRAS)
        manifest_path = Path(manifest["input_directory"]).parents[1] / "provenance" / "run-manifest.json"
        _set_preflight(manifest_path, {"*": "DDA"})
        built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(
            [("S1", "S1"), ("S2", "S2"), ("S3", "S3")],
            sorted((row["file_name"], row["sample_id"]) for row in built["rows"]),
        )
        self.assertEqual([], built["samples_without_input"])

    def test_a_folder_of_another_case_than_its_zip_is_recorded_as_it_is_written(self) -> None:
        """Second review of 0.5.39: a zip holding exactly its own folder in another case is nested_rooted
        (destination_rule compares them without case), and expanded_to named the zip's case, S1, where the disk has
        s1: a path a case-sensitive file system does not have."""
        manifest = self.lease({"S1.zip": _zip({"s1/S1.mzML": MZML})})

        (record,) = manifest["declared_archive_inputs"]
        self.assertEqual(
            ("nested_rooted", [f"{TAR_ROOT}/s1"], [f"{TAR_ROOT}/s1/S1.mzML"]),
            (record["destination_rule"], record["expanded_to"], record["inputs"]),
        )
        self.assertEqual(["s1"], sorted(path.name for path in (Path(manifest["input_directory"]) / TAR_ROOT).iterdir()))

    def test_a_unit_of_files_listed_one_by_one_records_no_declared_archive(self) -> None:
        """Only where one was, so every other lease records what it always did."""
        payloads = {"https://example.org/FILES/S1.mzML": MZML}
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS-PLAIN", analysis_unit_id="plain", eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("FILES/S1.mzML", len(MZML), "https://example.org/FILES/S1.mzML")],
            total_download_bytes=len(MZML), sample_metadata=[{"sample_id": "S1", "raw_file": "FILES/S1.mzML"}],
            analysis_inputs=[{"path": "FILES/S1.mzML", "kind": "file", "sample_id": "S1"}],
            analysis_inputs_declared=True,
        )
        lease = create_download_lease(project, self.root, 10**9, client=_Client(payloads))

        self.assertNotIn("declared_archive_inputs", read_manifest(lease["manifest_path"]))


class TheInnerZipIsHeldToTheOuterArchivesRules(_Workspace):
    def test_an_unsafe_member_name_in_the_inner_zip_refuses_the_extraction(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            self.lease({"S1.zip": _zip({"../escaped.mzML": MZML})})

        self.assertEqual("unsafe_listing", caught.exception.reason)
        self.assertEqual(["../escaped.mzML"], [item["name"] for item in caught.exception.rejected])
        self.assertFalse(any(self.root.rglob("escaped.mzML")))

    def test_a_published_md5_the_inner_zip_does_not_match_raises(self) -> None:
        with self.assertRaisesRegex(ValueError, "MD5 checksum mismatch for S2.zip"):
            self.lease(
                {"S1.zip": _zip({"S1/S1.mzML": MZML}), "S2.zip": _zip({"S2/S2.mzML": MZML})},
                checksums={"S2.zip": "0" * 32},
            )


class TheInnerZipCountsAgainstTheOneMemberBudget(_Workspace):
    # The tar holds one member; the zip inside it three more.
    limits = ExtractionLimits(reserve_bytes=0, max_members=3)

    def test_members_of_the_inner_zip_count_against_the_outer_archives_budget(self) -> None:
        with self.assertRaises(ArchiveError) as caught:
            self.lease({"S1.zip": _zip({"S1/a.mzML": MZML, "S1/b.txt": b"b", "S1/c.txt": b"c"})})

        self.assertEqual("member_count_exceeded", caught.exception.reason)


class WhatOneZipHoldsIsOneSample(_Workspace):
    def test_two_encodings_of_its_sample_are_one_input_by_the_one_encoding_rule(self) -> None:
        manifest = self.lease({"S1.zip": _zip({"S1/S1.mzML": MZML, "S1/S1.cdf": b"CDF\x01netcdf"})})

        self.assertEqual([f"{TAR_ROOT}/S1/S1.cdf"], self.relative(manifest, manifest["input_candidates"]))
        choice = manifest["encoding_choices"][0]
        self.assertTrue(choice["used"].endswith("S1.cdf"))
        self.assertEqual(["S1.mzML"], [Path(item["path"]).name for item in choice["unused"]])
        self.assertEqual([f"{TAR_ROOT}/S1/S1.cdf"], manifest["declared_archive_inputs"][0]["inputs"])

    def test_two_runs_in_one_declared_zip_are_refused_as_one_input_found_twice(self) -> None:
        """Two stems are two runs, and still refused after the user's decision of 2026-10-15 that a per-sample
        archive's folders are copies: the rule groups a declared unit's files by row and stem, uses one of each, and
        leaves neither unused."""
        for index, files in enumerate((
            {"S1/run_1.mzML": MZML, "S1/run_2.mzML": MZML},
            # In folders of their own as well: the folders being copies does not make two stems one.
            {"S1/A/run_1.mzML": MZML, "S1/B/run_2.mzML": MZML},
        )):
            self.fresh_root(f"case-{index}")
            with self.subTest(files=sorted(files)), self.assertRaisesRegex(
                ValueError, r"found more than once: S1\.zip \(2 inputs in what the archive held\)"
            ):
                self.lease({"S1.zip": _zip(files)})

    def test_one_file_name_in_two_run_folders_is_one_acquisitions_copies_by_the_decision(self) -> None:
        """THE USER'S DECISION OF 2026-10-15 ("1つだけで良いと思っています"): the folders inside one per-sample archive
        are copies, and one is used. Until 0.5.40 run_a/data.mzML beside run_b/data.mzML was refused as found twice,
        because such folders might have been the sample's runs (the review of 0.5.39, which this test pinned until
        the decision); now the rule uses run_a/data.mzML, the first by path (clause 2), and records run_b's as a copy
        left unused, with a warning."""
        runs = {"S1/run_a/data.mzML": mzml(dda_spectra(2)), "S1/run_b/data.mzML": mzml(dda_spectra(3))}
        manifest = self.lease({"S1.zip": _zip(runs)})

        self.assert_one_copy_used(manifest, "S1/run_a/data.mzML", ["S1/run_b/data.mzML"], "tie_lexicographic")

    def assert_one_copy_used(self, manifest: dict, used: str, copies: list[str], reason: str) -> None:
        """One input, the copy the rule used; the others left unused with their reason, named in
        declared_archive_inputs (copies_unused, copy_rule), in the manifest's and the attribute stage's warnings, and
        in an archive warning that names the archive, the copy used and the copies left."""
        self.assertEqual("prepared", manifest["status"])
        self.assertEqual([f"{TAR_ROOT}/{used}"], self.relative(manifest, manifest["input_candidates"]))
        (choice,) = manifest["encoding_choices"]
        self.assertEqual(f"{TAR_ROOT}/{used}", choice["used"])
        self.assertEqual(
            [(f"{TAR_ROOT}/{item}", reason) for item in copies],
            [(item["path"], item["reason"]) for item in choice["unused"]],
        )
        (record,) = manifest["declared_archive_inputs"]
        self.assertEqual([f"{TAR_ROOT}/{used}"], record["inputs"])
        self.assertEqual([f"{TAR_ROOT}/{item}" for item in copies], record["copies_unused"])
        self.assertEqual("per_sample_archive_folders_are_copies_2026_10_15", record["copy_rule"])
        self.assertIn("declared_archive_copies_unused", manifest["warnings"])
        (warning,) = [item for item in manifest["archive_warnings"] if item["kind"] == "declared_archive_copies_unused"]
        self.assertEqual(f"{TAR_ROOT}/S1.zip", warning["archive"])
        self.assertIn(
            f"S1.zip (inside {ACCESSION}.tar) held its sample's file in more than one of its folders", warning["message"]
        )
        self.assertIn(
            f"Used: {TAR_ROOT}/{used}; left unused: " + ", ".join(f"{TAR_ROOT}/{item}" for item in copies) + ".",
            warning["message"],
        )
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual(1, attribute["declared_archive_copies_unused"])
        self.assertIn("declared_archive_copies_unused", attribute["warnings"])

    def test_both_polarities_folders_in_one_declared_zip_are_refused_not_chosen_by_path(self) -> None:
        """POS/S1.mzML and NEG/S1.mzML are two acquisitions, never two encodings of one; in this positive unit the
        lease ran NEG/S1.mzML, first by path, before the review of 0.5.39. The user's decision of 2026-10-15 that a
        per-sample archive's folders are copies does not reach them: the rule itself keeps two polarities apart."""
        for index, files in enumerate((
            {"S1/NEG/S1.mzML": MZML, "S1/POS/S1.mzML": MZML},
            # Another encoding as well as another folder: still two acquisitions.
            {"S1/NEG/S1.mzML": MZML, "S1/POS/S1.cdf": b"CDF\x01netcdf"},
        )):
            self.fresh_root(f"case-{index}")
            with self.subTest(files=sorted(files)), self.assertRaisesRegex(
                ValueError, r"found more than once: S1\.zip \(2 inputs in what the archive held\)"
            ):
                self.lease({"S1.zip": _zip(files)})

    def test_two_vendor_formats_beside_each_other_are_a_tie_the_rule_gives_the_first_by_path(self) -> None:
        """Second review of 0.5.39: the exemption also asked for another rank, so S1/S1.d beside S1/S1.wiff - clause
        2's 'two vendor formats', two names in one folder that differ only by their suffix, never two runs - was
        refused as found twice."""
        cases = (
            ({"S1/S1.d/AcqData/MSScan.bin": b"scan", "S1/S1.wiff": b"wiff"}, "S1/S1.d", "S1/S1.wiff"),
            # Two re-encodings of one rank (RE_ENCODING_RANK).
            ({"S1/S1.cdf": b"CDF\x01netcdf", "S1/S1.abf": b"ABF"}, "S1/S1.abf", "S1/S1.cdf"),
        )
        for index, (files, used, unused) in enumerate(cases):
            self.fresh_root(f"case-{index}")
            with self.subTest(files=sorted(files)):
                manifest = self.lease({"S1.zip": _zip(files)})

                self.assertEqual([f"{TAR_ROOT}/{used}"], self.relative(manifest, manifest["input_candidates"]))
                (choice,) = manifest["encoding_choices"]
                self.assertEqual(f"{TAR_ROOT}/{used}", choice["used"])
                self.assertEqual(
                    [(f"{TAR_ROOT}/{unused}", "tie_lexicographic")],
                    [(item["path"], item["reason"]) for item in choice["unused"]],
                )
                self.assertEqual([f"{TAR_ROOT}/{used}"], manifest["declared_archive_inputs"][0]["inputs"])

    def test_one_name_in_two_folders_of_a_declared_zip_is_copies_and_one_is_used(self) -> None:
        """THE USER'S DECISION OF 2026-10-15 (encoding_rule, FOLDERS INSIDE ONE DECLARED PER-SAMPLE ARCHIVE ARE
        COPIES). Until 0.5.40 each pair was refused as one declared input found more than once, an exception to the
        rule the user had not decided, and this test pinned that stop. Now each pair is one acquisition's copies or
        encodings, as the rule's words say ('copies in other folders included', clause 2; POS/S1.mzML beside S1.mzML
        in a positive unit is one sample's, _one_sample_polarities): the rule uses one, by rank and then the first by
        path, and the others are recorded and warned of."""
        cases = (
            ({"S1/cdf/S1.cdf": b"CDF\x01netcdf", "S1/mzML/S1.mzML": MZML}, "S1/cdf/S1.cdf", ["S1/mzML/S1.mzML"],
             "lower_in_encoding_order"),
            ({"S1/A/S1.mzML": MZML, "S1/B/S1.mzML": MZML}, "S1/A/S1.mzML", ["S1/B/S1.mzML"], "tie_lexicographic"),
            ({"S1/RAW/S1.d/AcqData/MSScan.bin": b"scan", "S1/mzML/S1.mzML": MZML}, "S1/RAW/S1.d",
             ["S1/mzML/S1.mzML"], "lower_in_encoding_order"),
            ({"S1/POS/S1.mzML": MZML, "S1/S1.mzML": MZML}, "S1/POS/S1.mzML", ["S1/S1.mzML"], "tie_lexicographic"),
            # Three copies: one used, both others named.
            ({"S1/A/S1.mzML": MZML, "S1/B/S1.mzML": MZML, "S1/C/S1.mzML": MZML}, "S1/A/S1.mzML",
             ["S1/B/S1.mzML", "S1/C/S1.mzML"], "tie_lexicographic"),
            # A copy a desktop made: its '-' stands between spaces, a separator and no polarity's sign
            # (_unread_polarity_mark), so the review of 0.5.40 leaves it a copy.
            ({"S1/S1 - Copy/S1.mzML": MZML, "S1/S1.mzML": MZML}, "S1/S1 - Copy/S1.mzML", ["S1/S1.mzML"],
             "tie_lexicographic"),
        )
        for index, (files, used, copies, reason) in enumerate(cases):
            self.fresh_root(f"case-{index}")
            with self.subTest(files=sorted(files)):
                self.assert_one_copy_used(self.lease({"S1.zip": _zip(files)}), used, copies, reason)

    def test_a_copy_beside_one_of_the_other_polarity_is_still_refused(self) -> None:
        """S1/S1.mzML states no polarity, and is a copy of S1/POS/S1.mzML only where the unit is positive
        (_one_sample_polarities); in a negative unit the two stay apart, and the zip is refused as found twice."""
        with self.assertRaisesRegex(
            ValueError, r"found more than once: S1\.zip \(2 inputs in what the archive held\)"
        ):
            self.lease({"S1.zip": _zip({"S1/POS/S1.mzML": MZML, "S1/S1.mzML": MZML})}, ion_mode="Negative")

    def test_folders_that_may_state_a_polarity_by_a_mark_the_rule_does_not_read_are_refused(self) -> None:
        """REVIEW OF 0.5.40 (2026-10-15). The rule reads a polarity only from a pos, positive, neg or negative token
        of its own, so Posi/ and Nega/ stated none, and the decision of 2026-10-15 took the two files as copies: a
        positive unit used S1/Nega/S1.mzML, the first by path, and recorded S1/Posi/S1.mzML as the copy left unused;
        a negative unit used ESI+/S1.mzML. 0.5.39 refused every such pair. Such a mark is still not read as a
        polarity, but a folder bearing one where the two paths' folders differ keeps the two from being copies, so the
        zip is refused as one declared input found more than once, in a unit of either polarity, and beside an
        unmarked folder too (_folders_bear_an_unread_polarity_mark)."""
        pairs = (
            ("Posi", "Nega"),
            ("ESI+", "ESI-"),
            ("ESIpos", "ESIneg"),
            ("P", "N"),
            ("(+)", "(-)"),
            ("ポジ", "ネガ"),
            # The positive one's mark beside a folder of none, in a positive unit as well.
            ("Posi", ""),
        )
        index = 0
        for first, second in pairs:
            for ion_mode in ("Positive", "Negative"):
                index += 1
                self.fresh_root(f"case-{index}")
                files = {
                    "/".join(part for part in ("S1", first, "S1.mzML") if part): MZML,
                    "/".join(part for part in ("S1", second, "S1.mzML") if part): MZML,
                }
                with self.subTest(files=sorted(files), ion_mode=ion_mode), self.assertRaisesRegex(
                    ValueError, r"found more than once: S1\.zip \(2 inputs in what the archive held\)"
                ):
                    self.lease({"S1.zip": _zip(files)}, ion_mode=ion_mode)

    def test_which_folder_names_bear_a_polarity_mark_the_rule_does_not_read(self) -> None:
        """_unread_polarity_mark, folder by folder: a mark is what may state a polarity and is not a token the rule
        reads (those decide through _one_sample_polarities); a separator, a run's or a copy's name is none."""
        mark = repository_reanalysis._unread_polarity_mark
        for name in (
            "Posi", "Nega", "ESI+", "ESI-", "ESI- mode", "-ESI", "ESIpos", "ESIneg", "ionmode_positiv", "P", "n",
            "(+)", "( - )", "pos(1)", "ポジ", "陰イオン",
        ):
            with self.subTest(name=name):
                self.assertTrue(mark(name))
        for name in (
            "A", "b", "RAW", "mzML", "run_a", "run-a", "S1 - Copy", "2026-10-15", "Data", "POS", "neg", "Positive",
            "QC_NEG", "esi_pos",
        ):
            with self.subTest(name=name):
                self.assertFalse(mark(name))

    def test_the_copies_left_unused_reach_the_analysis_csv_record_and_the_disposition(self) -> None:
        """A run's records say it too: the analysis-CSV record (warnings, declared_archive_copies) and the preflight's
        disposition (its warning, and a detail line naming the archive, the copy used and the copy left)."""
        from msdial_app.raw_metadata_preflight import decide_disposition

        manifest = self.lease({"S1.zip": _zip({"S1/A/S1.mzML": MZML, "S1/B/S1.mzML": MZML})})
        manifest_path = Path(manifest["input_directory"]).parents[1] / "provenance" / "run-manifest.json"
        _set_preflight(manifest_path, {"*": "DDA"})
        current = read_manifest(manifest_path)
        built = build_repository_analysis_rows(current)
        disposition = decide_disposition(current)

        self.assertEqual([], built["failures"])
        self.assertIn("declared_archive_copies_unused", built["warnings"])
        self.assertEqual(
            [{
                "declared_path": "S1.zip",
                "used": [f"{TAR_ROOT}/S1/A/S1.mzML"],
                "copies_unused": [f"{TAR_ROOT}/S1/B/S1.mzML"],
                "copy_rule": "per_sample_archive_folders_are_copies_2026_10_15",
            }],
            built["declared_archive_copies"],
        )
        self.assertIn("declared_archive_copies_unused", disposition["warnings"])
        self.assertTrue(
            any(
                "S1.zip" in line and f"{TAR_ROOT}/S1/A/S1.mzML is used" in line and f"{TAR_ROOT}/S1/B/S1.mzML" in line
                for line in disposition["detail"]
            ),
            disposition["detail"],
        )

    def test_a_unit_whose_zips_hold_one_copy_each_records_no_copies(self) -> None:
        """Only where a copy was left, so every other lease records what it always did."""
        manifest = self.lease({"S1.zip": _zip({"S1/S1.mzML": MZML, "S1/S1.cdf": b"CDF\x01netcdf"})})

        self.assertNotIn("copies_unused", manifest["declared_archive_inputs"][0])
        self.assertNotIn("warnings", manifest)
        self.assertFalse(
            any(item["kind"] == "declared_archive_copies_unused" for item in manifest.get("archive_warnings") or [])
        )

    def test_a_zip_holding_nothing_ms_dial_opens_says_what_it_held(self) -> None:
        """MPST000213: each zip held a JEOL msAxel project, which no RawDataHandler reader opens."""
        msaxel = {
            "Data/Data test/Adit_20260420_1/RD/MsData.RPS": b"rps",
            "Data/Data test/Adit_20260420_1/RD/MsData.RPC": b"rpc",
            "Data/Data test/Adit_20260420_1/MS/0/Profile.dfa": b"dfa",
            "Data/Data test/Adit_20260420_1/MS/1/Profile.dfa": b"dfa",
            "Settings/AccuTofLcMsMethod.xml": b"<xml/>",
        }
        zips = {
            f"Borneo_ID_0{index}.zip": _zip({f"Borneo_ID_0{index}/{name}": data for name, data in msaxel.items()})
            for index in (1, 2, 3)
        }
        with self.assertRaises(ValueError) as caught:
            self.lease(zips)

        message = str(caught.exception)
        self.assertIn("3 of its 3 declared analysis inputs are not in the download", message)
        self.assertIn(f"3 of them were archives inside {ACCESSION}.tar that the extract stage expanded", message)
        self.assertIn(f"Borneo_ID_01.zip: 5 file(s) under {TAR_ROOT}/Borneo_ID_01, by suffix .dfa 2,", message)
        self.assertIn(".rps 1", message)

    def test_what_a_zip_held_is_named_by_its_folder_as_written(self) -> None:
        """The folder is found, and named, in its own case (second review of 0.5.39): on a case-sensitive file
        system the zip's case found no folder, and the message lost its count by suffix."""
        with self.assertRaises(ValueError) as caught:
            self.lease({"Borneo_ID_01.zip": _zip({"borneo_id_01/Data/RD/MsData.RPS": b"rps"})})

        self.assertIn(
            f"(Borneo_ID_01.zip: 1 file(s) under {TAR_ROOT}/borneo_id_01, by suffix .rps 1)", str(caught.exception)
        )


class AnMzxmlInADeclaredZipIsConvertedInACampaign(_Workspace):
    def test_the_converted_mzml_is_the_samples_input(self) -> None:
        manifest = self.lease(
            {"S1.zip": _zip({"S1/S1.mzXML": dda_32()})},
            lease_options={"campaign_authorization": dict(_APPROVAL)},
        )

        self.assertEqual(["S1.mzML"], [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual("converted", manifest["input_conversions"]["records"][0]["status"])
        self.assertEqual([f"{TAR_ROOT}/S1/S1.mzXML"], manifest["declared_archive_inputs"][0]["inputs"])
        self.assertEqual("S1", _rows(manifest)["S1.mzML"]["sample_id"])


class APerSampleZipTheUnitDownloadsIsMatchedThroughItsSample(_Workspace):
    """A declared FILES/S1.zip the unit downloads itself expands at the data root with its own paths; no record says
    which of them were its, so the sample the archive's name gives them decides, as for an archived container."""

    def test_its_contents_are_its_samples_input(self) -> None:
        payloads = {
            f"https://example.org/FILES/{stem}.zip": _zip({f"{stem}.d/AcqData/MSScan.bin": stem.encode()})
            for stem in ("S1", "S2")
        }
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS-ZIPS", analysis_unit_id="zips", eligible=True,
            selection_status="eligible",
            files=[
                RepositoryFile(f"FILES/{stem}.zip", 10, f"https://example.org/FILES/{stem}.zip") for stem in ("S1", "S2")
            ],
            total_download_bytes=20,
            sample_metadata=[{"sample_id": stem, "raw_file": f"FILES/{stem}.zip"} for stem in ("S1", "S2")],
            analysis_inputs=[{"path": f"FILES/{stem}.zip", "kind": "file", "sample_id": stem} for stem in ("S1", "S2")],
            analysis_inputs_declared=True,
        )
        manifest = read_manifest(
            create_download_lease(project, self.root, 10**9, client=_Client(payloads))["manifest_path"]
        )

        self.assertEqual(
            {"S1.d": "S1", "S2.d": "S2"}, {name: row["sample_id"] for name, row in _rows(manifest).items()}
        )
        self.assertNotIn("declared_archive_inputs", manifest)


class AnArchivedContainerStillCountsEveryInputItMatched(unittest.TestCase):
    """Review of 0.5.39: the exemption for unused encodings applied to every declared input, so an archived container
    matched through its sample in two folders (A/S1.d, B/S1.d), which was refused as found twice, was no longer."""

    def test_two_containers_of_one_name_matched_through_their_sample_are_refused(self) -> None:
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS-AC", analysis_unit_id="ac", eligible=True,
            selection_status="eligible", files=[RepositoryFile("raw/S1.d.zip", 10, "https://example.org/S1.d.zip")],
            total_download_bytes=10, sample_metadata=[{"sample_id": "S1", "raw_file": "raw/S1.d"}],
            analysis_inputs=[
                {"path": "raw/S1.d", "kind": "archived_container", "archive": "raw/S1.d.zip", "sample_id": "S1"}
            ],
            analysis_inputs_declared=True,
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            first, second = str(root / "A" / "S1.d"), str(root / "B" / "S1.d")
            with self.assertRaisesRegex(ValueError, r"found more than once: raw/S1\.d \(2 inputs\)"):
                _filter_inputs_by_project_allowlist(
                    [first, second], root, project,
                    archive_samples={_file_key(first): "S1", _file_key(second): "S1"},
                    unused={_file_key(second)},
                )


# The nested record of Rubroshorea_leprosula_from_Borneo_ID_01.zip as the download store wrote it for MPST000213 on
# 2026-10-14 (Interactive 0.5.37), less its timings and tool: no field this version added.
MPST000213_ZIP = "Rubroshorea_leprosula_from_Borneo_ID_01.zip"
MPST000213_RECORD = {
    "archive_name": "MPST000213.tar",
    "placement": "",
    "destination_rule": "archive_root",
    "nested": [
        {
            "format": "zip", "member_count": 1292, "file_count": 1028, "depth": 2,
            "archive_name": MPST000213_ZIP,
            "archive_path": f"MB-POST_files_MPST000213.0/{MPST000213_ZIP}",
            "destination_relative": "MB-POST_files_MPST000213.0",
            "archive_bytes": 1855732429,
            "archive_md5": "00cee5d47593eb25e4d77d58b1d15fb4",
            "destination_rule": "nested_rooted", "container_stem": "", "container_root": "",
            "nested": [], "nested_skipped": [],
        },
        {
            "format": "zip", "file_count": 615, "depth": 2,
            "archive_name": "Extracted_Raw_Sample_ Spectra_Peak_615.zip",
            "archive_path": "MB-POST_files_MPST000213.0/Extracted_Raw_Sample_ Spectra_Peak_615.zip",
            "destination_relative": "MB-POST_files_MPST000213.0",
            "destination_rule": "nested_rooted", "container_stem": "", "container_root": "",
            "nested": [], "nested_skipped": [],
        },
    ],
}


class AStoreRecordMadeBeforeThisVersionSaysWhereEachZipWent(unittest.TestCase):
    def _project(self) -> RepositoryProject:
        return RepositoryProject(
            repository="mb_post", accession="MPST000213", analysis_unit_id="334ab3e2f0e7600f4290", eligible=True,
            selection_status="eligible",
            files=[
                RepositoryFile(
                    MPST000213_ZIP, 1855732429, "https://repository.massbank.jp/api/download/MPST000213.0",
                    checksum="00cee5d47593eb25e4d77d58b1d15fb4",
                )
            ],
            total_download_bytes=1855732429,
            sample_metadata=[{"sample_id": "Rubroshorea_leprosula_from_Borneo_ID_01", "raw_file": MPST000213_ZIP}],
            analysis_inputs=[
                {"path": MPST000213_ZIP, "kind": "file", "sample_id": "Rubroshorea_leprosula_from_Borneo_ID_01"}
            ],
            analysis_inputs_declared=True,
        )

    def test_the_place_is_read_from_the_destination_rule(self) -> None:
        project = self._project()
        declared = declared_analysis_inputs(project)

        key = MPST000213_ZIP.casefold()
        place = "mb-post_files_mpst000213.0/rubroshorea_leprosula_from_borneo_id_01"
        self.assertEqual({place + "/": key}, declared_archive_containers(project, declared, [MPST000213_RECORD]))
        (expansion,) = declared_archive_expansions(declared, [MPST000213_RECORD])
        self.assertEqual(
            (
                f"MB-POST_files_MPST000213.0/{MPST000213_ZIP}",
                ["MB-POST_files_MPST000213.0/Rubroshorea_leprosula_from_Borneo_ID_01"],
            ),
            (expansion["archive_path"], expansion["places"]),
        )

    def test_a_file_under_the_place_matches_and_one_beside_it_does_not(self) -> None:
        project = self._project()
        declared = declared_analysis_inputs(project)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            inside = root / "MB-POST_files_MPST000213.0" / "Rubroshorea_leprosula_from_Borneo_ID_01" / "Data" / "S.mzML"
            beside = root / "MB-POST_files_MPST000213.0" / "Rubroshorea_leprosula_from_Borneo_ID_010" / "S.mzML"
            found = match_declared_inputs(
                [str(inside), str(beside)], root, declared,
                containers=declared_archive_containers(project, declared, [MPST000213_RECORD]),
            )
            with self.assertRaisesRegex(ValueError, r"1 of them was an archive inside MPST000213\.tar"):
                _filter_inputs_by_project_allowlist(
                    [str(beside)], root, project, archive_extractions=[MPST000213_RECORD]
                )

        self.assertEqual({MPST000213_ZIP.casefold(): [str(inside)]}, found)

    def test_a_place_is_matched_by_the_full_path_under_the_data_root_only(self) -> None:
        """Review of 0.5.39: the place S1/ of a zip expanded at the root of a root-less archive was also tested
        against each path less its first folder (_allowlist_forms), so QC/S1/x.mzML was S1.zip's input."""
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS-PLACE", analysis_unit_id="place", eligible=True,
            selection_status="eligible", files=[RepositoryFile("S1.zip", 10, "https://example.org/bundle.tar")],
            total_download_bytes=10, sample_metadata=[{"sample_id": "S1", "raw_file": "S1.zip"}],
            analysis_inputs=[{"path": "S1.zip", "kind": "file", "sample_id": "S1"}], analysis_inputs_declared=True,
        )
        declared = declared_analysis_inputs(project)

        def record(rule: str, destination: str, container: str = "") -> dict:
            return {
                "archive_name": "bundle.tar", "placement": "", "destination_rule": "archive_root",
                "nested": [{"archive_name": "S1.zip", "archive_path": "S1.zip", "depth": 2, "destination_rule": rule,
                            "destination_relative": destination, "container_root": container}],
            }

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for extraction, expected in (
                # The zip's contents in a folder made for it, at the root.
                (record("nested_directory", "S1"), {"S1/y.mzML": True, "QC/S1/x.mzML": False}),
                # A vendor folder the zip held is the place itself, and the input.
                (record("container_stem", "A/S1.d", "A/S1.d"), {"A/S1.d": True, "Q/A/S1.d": False, "A/S1.d0": False}),
            ):
                containers = declared_archive_containers(project, declared, [extraction])
                for relative, matches in expected.items():
                    with self.subTest(rule=extraction["nested"][0]["destination_rule"], input=relative):
                        path = str(root.joinpath(*relative.split("/")))
                        found = match_declared_inputs([path], root, declared, containers=containers)
                        self.assertEqual([path] if matches else [], found["s1.zip"])
        rules = {
            ("nested_directory", "A/S1", ""): "A/S1",
            ("container_stem", "A/S1.d", "A/S1.d"): "A/S1.d",
            ("container_rooted_other_name", "A", "A/B.d"): "A/B.d",
            ("container_single_file", "A", "A/S1.raw"): "A/S1.raw",
            ("stream_file", "A", ""): "A/S1",
        }
        for (rule, destination, container), expected in rules.items():
            nested = {"archive_name": "S1.zip", "archive_path": "A/S1.zip", "destination_rule": rule,
                      "destination_relative": destination, "container_root": container}
            with self.subTest(rule=rule):
                self.assertEqual([expected], repository_reanalysis._expanded_places(nested))
        self.assertEqual([], repository_reanalysis._expanded_places(
            {"archive_name": "S1.zip", "archive_path": "A/S1.zip", "destination_rule": "container_files"}
        ))


class APlaceIsNamedAsItIsWrittenOnDisk(unittest.TestCase):
    """_as_written: each part of a place in its own case on disk, read part by part (second review of 0.5.39)."""

    def test_each_part_takes_the_case_it_has_on_disk(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            (root / "MB" / "s1" / "Data").mkdir(parents=True)

            self.assertEqual("MB/s1", repository_reanalysis._as_written(root, "mb/S1"))
            self.assertEqual("MB/s1/Data", repository_reanalysis._as_written(root, "MB/S1/data"))
            # From a part that is not there, the rest is as given.
            self.assertEqual("MB/s1/Gone/X", repository_reanalysis._as_written(root, "mb/s1/Gone/X"))
            self.assertEqual("Elsewhere/S1", repository_reanalysis._as_written(root / "missing", "Elsewhere/S1"))

    def test_a_case_sensitive_folder_is_read_by_its_listing(self) -> None:
        """What a case-sensitive file system lists, given as the folders' listings: a part found as given stays, one
        found once without case takes that case, and one found twice so is left as given."""
        root = Path("root")
        listings = {root: ["MB"], root / "MB": ["s1", "S2", "s2"]}

        self.assertEqual("MB/s1", repository_reanalysis._as_written(root, "MB/S1", dict(listings)))
        self.assertEqual("MB/S2", repository_reanalysis._as_written(root, "MB/S2", dict(listings)))
        self.assertEqual("MB/s2", repository_reanalysis._as_written(root, "MB/s2", dict(listings)))
        self.assertEqual("MB/S3", repository_reanalysis._as_written(root, "mb/S3", dict(listings)))
        self.assertEqual("mb/S2", repository_reanalysis._as_written(Path("other"), "mb/S2", {Path("other"): ["MB", "mB"]}))


class ASplitPartCarriesOnlyItsOwnArchivesCopies(unittest.TestCase):
    """_part_declared_archive_inputs: a part takes the parent's declared_archive_inputs entries whose input is one of
    its own (an mzML the convert stage wrote by the mzXML it stands for), so its copies warning is its own."""

    def test_the_part_takes_the_entries_of_its_own_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            data_root = Path(temporary).resolve() / "raw" / "data"
            converted = Path(temporary).resolve() / "raw" / "converted" / "S2.mzML"
            parent = {
                "input_directory": str(data_root),
                "declared_archive_inputs": [
                    {"declared_path": "S1.zip", "inputs": [f"{TAR_ROOT}/S1/A/S1.mzML"],
                     "copies_unused": [f"{TAR_ROOT}/S1/B/S1.mzML"], "copy_rule": "x"},
                    {"declared_path": "S2.zip", "inputs": [f"{TAR_ROOT}/S2/S2.mzXML"]},
                ],
            }
            take = repository_reanalysis._part_declared_archive_inputs

            first = take(parent, [str(data_root / TAR_ROOT / "S1" / "A" / "S1.mzML")], {})
            second = take(
                parent, [str(converted)], {_file_key(str(converted)): str(data_root / TAR_ROOT / "S2" / "S2.mzXML")}
            )

            self.assertEqual(["S1.zip"], [item["declared_path"] for item in first])
            self.assertEqual(["S2.zip"], [item["declared_path"] for item in second])
            self.assertEqual([], take({**parent, "input_directory": ""}, [str(converted)], {}))


if __name__ == "__main__":
    unittest.main()
