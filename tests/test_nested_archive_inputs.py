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
- one sample's encodings inside one zip are one sample's (the one encoding rule), two runs are two inputs, and a
  zip holding nothing MS-DIAL opens (MPST000213's JEOL msAxel project) says what it held;
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


def _handoff(zips: dict[str, bytes], tar: bytes, *, checksums: dict[str, str] | None = None) -> dict:
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
            "ion_mode": "Positive",
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
        with self.assertRaisesRegex(
            ValueError, r"found more than once: S1\.zip \(2 inputs in what the archive held\)"
        ):
            self.lease({"S1.zip": _zip({"S1/run_1.mzML": MZML, "S1/run_2.mzML": MZML})})

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
        self.assertEqual(
            {place: key, place + "/": key}, declared_archive_containers(project, declared, [MPST000213_RECORD])
        )
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

    def test_each_destination_rule_names_its_place(self) -> None:
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


if __name__ == "__main__":
    unittest.main()
