"""Replicate injections are inputs of their own, and an archive member may carry a declared name behind a prefix.

Three units the pilot campaign of 2026-10-03 stopped at, as small synthetic fixtures:

- MetaboLights MTBLS291 lists every injection as an assay row of its own: the five rows of sample Cel share its
  id and each names its own mzML (Factor Value[Replicate] 1..5). The Catalog declares no inputs for it (the
  files are converted mzML), the lease attributes each file to Cel by its name, and the analysis-CSV builder,
  keying rows by sample id, read the second replicate as a second input of the first one's row and wrote no
  CSV (sample_with_two_inputs, as the code was then named);
- MetaboBank MTBKS64 names S01 in two rows (Assay Names S01_M01 and S01_M02), one per .RAW, and S02 in two.
  The Catalog lists four inputs for the four rows and counts them alike, and Interactive's handoff check, which
  read the second input of S01 as a second input of one row, excluded the unit before its download
  (analysis_input:count_mismatch);
- Metabolomics Workbench ST001264 declares BioRec1.raw, and its study archive holds
  021518_387057_CSHp_BioRec1.raw. No name rule matched it, and the lease failed at its attribute stage after
  the whole download ("Refusing to fall back to accession-level inputs").

The unit of mapping is the sample row. What is genuinely ambiguous is still refused: one row two inputs name,
one input two rows name, an input of a sample whose rows do not say which it is, and a prefixed name that is
not one to one.
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, repository_reanalysis, workflow
    from msdial_app.archives import ExtractionLimits
    from msdial_app.repository_analysis_rows import (
        MAPPING_FAILURES,
        blocking_failures,
        build_repository_analysis_rows,
    )
    from msdial_app.repository_reanalysis import (
        PREFIXED_MEMBER_PAIRING,
        RepositoryFile,
        RepositoryProject,
        _file_key,
        _prefixed_member_pairing,
        create_download_lease,
        evaluate_repository_execution_gate,
        project_from_dict,
        read_manifest,
        update_manifest,
    )

from test_folder_inputs import _Client, _exclude, _hand_made_unit, _no_backend
from test_mzml_encoding import dda_spectra, mzml

# The default reserve is 20 GB of free space; these trees are a few kilobytes.
LIMITS = ExtractionLimits(reserve_bytes=0)


def _set_types(manifest_path: Path, value: str) -> None:
    """Record a raw-metadata preflight that gives every input the Console acquisition type ``value``."""

    def change(manifest: dict) -> None:
        manifest["raw_metadata_preflight"] = {
            "summary": {
                "acquisition_mode": value,
                "per_file": [
                    {"file": path, "acquisition_mode": value, "polarity": "Positive", "ms_levels": [1, 2],
                     "console_acquisition_type": value}
                    for path in manifest["input_candidates"]
                ],
            }
        }

    update_manifest(manifest_path, change)


def _handoff(accession: str, unit_id: str, files: list, inputs: list, samples: list, proposal: dict) -> dict:
    """A Catalog handoff (one-input-per-sample.v1) for a positive-mode DDA unit."""
    return {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabobank" if accession.startswith("MTBKS") else "metabolights",
        "accession": accession,
        "analysis_unit_id": unit_id,
        "title": accession,
        "technical_settings": {
            "separation": "LC-MS", "ion_mode": "Positive", "acquisition_mode": "DDA", "untargeted": True,
            "target_omics": "Metabolomics",
        },
        "files": files,
        "download_scope": {
            "kind": "unit_files", "file_count": len(files), "analysis_file_count": len(inputs),
            "bundle_bytes": sum(item["size_bytes"] for item in files),
        },
        "sample_count": len(samples),
        "analytical_sample_count": len(inputs),
        "analysis_input_model": "one-input-per-sample.v1",
        "analysis_inputs_declared": bool(inputs),
        "analysis_input_count": len(inputs),
        "analysis_inputs": inputs,
        "analysis_input_issues": [],
        "split_hint": None,
        "sample_metadata": samples,
        "class_proposal": proposal,
        "blocking_reasons": [],
    }


def _lease(root: Path, handoff: dict, payloads: dict[str, bytes]) -> Path:
    project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff)
    typed = project_from_dict(project)
    typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
    return Path(create_download_lease(typed, root, 10_000_000, client=_Client(payloads))["manifest_path"])


# ---- MTBKS64: four declared inputs for four rows, two per sample -------------------------------------------

MTBKS64_BASE = "https://example.org/MTBKS64/"
MTBKS64 = [
    ("S01", "raw/MDLC1_17050.RAW", "S01_M01", "1% sucrose"),
    ("S01", "raw/MDLC1_17051.RAW", "S01_M02", "1% sucrose"),
    ("S02", "raw/MDLC1_17053.RAW", "S02_M01", "no sucrose"),
    ("S02", "raw/MDLC1_17054.RAW", "S02_M02", "no sucrose"),
]


def _mtbks64() -> tuple[dict, dict[str, bytes]]:
    files, inputs, samples, payloads = [], [], [], {}
    for sample, raw, assay, treatment in MTBKS64:
        data = f"thermo raw bytes of {raw}".encode()
        payloads[MTBKS64_BASE + raw] = data
        files.append({
            "path": raw, "download_url": MTBKS64_BASE + raw, "size_bytes": len(data),
            "checksum": hashlib.md5(data).hexdigest(), "role": "raw", "sample_id": "", "sample_id_resolved": True,
        })
        inputs.append({
            "path": raw, "kind": "file", "suffix": ".raw", "format": "", "member_count": 1, "size_bytes": len(data),
            "sample_id": sample, "requires_conversion": False, "conversion_target": "",
        })
        samples.append({
            "sample_id": sample, "raw_file": raw,
            "attributes": {"Assay Name": assay, "Factor Value[treatment]": treatment},
        })
    proposal = {
        "proposal_id": "0afb5ded39be6bd0ac7c", "unit_id": "3e7a137913ca68547a57", "status": "accepted",
        "selected_fields": ["Factor Value[treatment]"], "rationale": "declared factor",
        "contrast_definition": {"kind": "declared_factor", "fields": ["Factor Value[treatment]"]},
        "assignments": [
            {"sample_id": "S01", "class_label": "1-sucrose", "values": {"Factor Value[treatment]": "1% sucrose"}},
            {"sample_id": "S02", "class_label": "no-sucrose", "values": {"Factor Value[treatment]": "no sucrose"}},
        ],
    }
    return _handoff("MTBKS64", "3e7a137913ca68547a57", files, inputs, samples, proposal), payloads


class TheHandoffPairsOneInputWithEachRow(unittest.TestCase):
    def test_two_rows_of_one_sample_each_with_its_own_input_are_consistent(self) -> None:
        handoff, _ = _mtbks64()
        project, workspace = mcp_server._project_from_analysis_unit_handoff(handoff)

        check = project["repository_metadata"]["analysis_input_check"]
        self.assertEqual({"status": "passed", "analysis_inputs": 4, "members": 0}, check)
        self.assertNotIn(mcp_server.ANALYSIS_INPUT_COUNT_MISMATCH, project["blocking_reasons"])
        self.assertNotEqual("excluded", project["selection_status"], project["exclusion_reasons"])
        self.assertEqual(4, project["sample_count"])
        self.assertEqual(["S01", "S01", "S02", "S02"], [row["sample_id"] for row in workspace["rows"]])

    def _problems(self, handoff: dict) -> list[str]:
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)
        self.assertEqual("excluded", project["selection_status"])
        self.assertIn(mcp_server.ANALYSIS_INPUT_COUNT_MISMATCH, project["blocking_reasons"])
        return project["repository_metadata"]["analysis_input_check"]["problems"]

    def test_two_inputs_on_one_row_of_a_replicated_sample_are_still_refused(self) -> None:
        """Both inputs of S01 carry the file name of its first row; its second row is named by neither."""
        handoff, _ = _mtbks64()
        handoff["analysis_inputs"][1]["path"] = "other/MDLC1_17050.RAW"
        problems = self._problems(handoff)

        self.assertTrue(any("two analysis inputs name the same sample row" in item for item in problems), problems)

    def test_an_input_none_of_its_samples_rows_names_is_refused(self) -> None:
        handoff, _ = _mtbks64()
        handoff["analysis_inputs"][1]["path"] = "raw/MDLC1_99999.RAW"
        problems = self._problems(handoff)

        self.assertTrue(any("raw/MDLC1_99999.RAW" in item for item in problems), problems)

    def test_more_inputs_than_a_sample_has_rows_is_refused(self) -> None:
        handoff, _ = _mtbks64()
        handoff["analysis_inputs"][2]["sample_id"] = "S01"
        problems = self._problems(handoff)

        self.assertTrue(any("3 analysis inputs name the 2 sample rows of sample 'S01'" in item for item in problems),
                        problems)

    def test_a_sample_of_one_row_named_by_two_inputs_is_refused_as_before(self) -> None:
        handoff, _ = _mtbks64()
        for item in handoff["sample_metadata"]:
            item["sample_id"] = item["attributes"]["Assay Name"]
        for item, sample in zip(handoff["analysis_inputs"], ("S01_M01", "S01_M01", "S02_M01", "S02_M02")):
            item["sample_id"] = sample
        handoff["class_proposal"] = None
        problems = self._problems(handoff)

        self.assertTrue(any("two analysis inputs name the same sample row" in item for item in problems), problems)


class EachReplicateIsARowOfItsOwn(unittest.TestCase):
    def test_an_mtbks64_shaped_unit_gives_four_rows_each_its_samples_class(self) -> None:
        handoff, payloads = _mtbks64()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(
            [
                ("MDLC1_17050", "S01", "raw/MDLC1_17050.RAW", 0, "1-sucrose"),
                ("MDLC1_17051", "S01", "raw/MDLC1_17051.RAW", 1, "1-sucrose"),
                ("MDLC1_17053", "S02", "raw/MDLC1_17053.RAW", 2, "no-sucrose"),
                ("MDLC1_17054", "S02", "raw/MDLC1_17054.RAW", 3, "no-sucrose"),
            ],
            [
                (row["file_name"], row["sample_id"], row["sample_raw_file"], row["sample_row_index"], row["class_id"])
                for row in built["rows"]
            ],
        )

    def test_the_mcp_step_writes_every_replicate_and_records_its_row(self) -> None:
        handoff, payloads = _mtbks64()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    confirmed=True, manifest_path=str(manifest_path)
                )
            with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
                written = list(csv.DictReader(handle))
            with open(prepared["files"]["metadata_tsv"], encoding="utf-8-sig", newline="") as handle:
                reviewed = list(csv.DictReader(handle, delimiter="\t"))
            manifest = read_manifest(manifest_path)
            files = workflow.read_analysis_csv(Path(prepared["input_path"]))["files"]
            gate = evaluate_repository_execution_gate(
                {"repository_run_manifest": str(manifest_path), "output_root": prepared["output_root"],
                 "ion_mode": "Positive", "files": files}
            )

        self.assertTrue(prepared["prepared"], prepared)
        self.assertEqual([], prepared["preview"]["ambiguous"])
        self.assertEqual(4, len(written))
        self.assertEqual(["1-sucrose", "1-sucrose", "no-sucrose", "no-sucrose"], [row["class_id"] for row in written])
        # The reviewed sample table keeps each row: its sample id and the raw file that tells it apart.
        self.assertEqual(
            [("S01", "raw/MDLC1_17050.RAW"), ("S01", "raw/MDLC1_17051.RAW"),
             ("S02", "raw/MDLC1_17053.RAW"), ("S02", "raw/MDLC1_17054.RAW")],
            [(row["sample_id"], row["raw_file"]) for row in reviewed],
        )
        # The lineage keeps the lease's sample id and names the row the CSV row was written from.
        self.assertEqual(
            [("S01", 0, "raw/MDLC1_17050.RAW"), ("S01", 1, "raw/MDLC1_17051.RAW"),
             ("S02", 2, "raw/MDLC1_17053.RAW"), ("S02", 3, "raw/MDLC1_17054.RAW")],
            sorted(
                (row["sample_row"]["sample_id"], row["sample_row"]["index"], row["sample_row"]["raw_file"])
                for row in manifest["input_lineage"]["rows"]
            ),
        )
        self.assertEqual(4, manifest["analysis_csv"]["rows"])
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_a_replicate_whose_input_is_missing_is_named_by_its_row(self) -> None:
        handoff, payloads = _mtbks64()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")

            def drop(manifest: dict) -> None:
                gone = next(item for item in manifest["input_candidates"] if item.endswith("MDLC1_17051.RAW"))
                manifest["input_candidates"].remove(gone)
                manifest["input_lineage"]["rows"] = [
                    row for row in manifest["input_lineage"]["rows"] if row["path"] != gone
                ]

            update_manifest(manifest_path, drop)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        codes = {item["code"]: item for item in built["failures"]}
        self.assertEqual({"analysis_input_not_found", "sample_without_input"}, set(codes))
        self.assertEqual(["S01 (raw/MDLC1_17051.RAW)"], codes["sample_without_input"]["inputs"])

    def test_an_excluded_replicate_spares_its_own_row_only(self) -> None:
        handoff, payloads = _mtbks64()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            _exclude(manifest_path, ["MDLC1_17051.RAW"], reason="acquisition_unresolved")
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(["MDLC1_17050", "MDLC1_17053", "MDLC1_17054"], [row["file_name"] for row in built["rows"]])
        self.assertEqual(
            [("MDLC1_17051.RAW", "S01", "raw/MDLC1_17051.RAW")],
            [(Path(item["path"]).name, item["sample_id"], item["sample_raw_file"]) for item in built["excluded_inputs"]],
        )


# ---- MTBLS291: no declared inputs, replicate rows named by their files ----------------------------------------

MTBLS291_BASE = "https://example.org/MTBLS291/"
MTBLS291 = [
    ("Cel", "Cel_AutoMS_2-A,2_01_18611.mzML", "1"),
    ("Cel", "Cel_AutoMS_2-A,2_02_18612.mzML", "2"),
    ("Cel", "Cel_AutoMS_2-A,2_03_18613.mzML", "3"),
    ("PC", "PC_AutoMS_2-B,2_01_18566.mzML", "1"),
    ("PC", "PC_AutoMS_2-B,2_02_18567.mzML", "2"),
]


def _mtbls291() -> tuple[dict, dict[str, bytes]]:
    files, samples, payloads = [], [], {}
    for index, (sample, name, replicate) in enumerate(MTBLS291):
        data = mzml(dda_spectra(4 + index))
        payloads[MTBLS291_BASE + name] = data
        files.append({
            "path": f"FILES/{name}", "download_url": MTBLS291_BASE + name, "size_bytes": len(data), "checksum": "",
            "role": "converted", "sample_id": "", "sample_id_resolved": False,
        })
        samples.append({
            "sample_id": sample, "raw_file": f"FILES/{name}",
            "attributes": {"Factor Value[Replicate]": replicate, "MS Assay Name": Path(name).stem},
        })
    proposal = {
        "proposal_id": "d645d2cfc5c7d672ed3d", "unit_id": "93be54410626162ccd8e", "status": "accepted",
        "selected_fields": [], "rationale": "abstention",
        "contrast_definition": {"kind": "abstention", "class_label": "All", "reason": "no_usable_declared_factor"},
        "assignments": [{"sample_id": sample, "class_label": "All", "values": {}} for sample in ("Cel", "PC")],
    }
    return _handoff("MTBLS291", "93be54410626162ccd8e", files, [], samples, proposal), payloads


class ReplicatesNamedByTheirFilesAreRowsOfTheirOwn(unittest.TestCase):
    def test_an_mtbls291_shaped_unit_gives_a_row_per_replicate(self) -> None:
        handoff, payloads = _mtbls291()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            manifest = read_manifest(manifest_path)
            built = build_repository_analysis_rows(manifest)

        self.assertEqual(
            ["Cel", "Cel", "Cel", "PC", "PC"], [row["sample_id"] for row in manifest["input_lineage"]["rows"]]
        )
        self.assertEqual([], built["failures"])
        self.assertEqual(
            [f"FILES/{name}" for _sample, name, _replicate in MTBLS291],
            [row["sample_raw_file"] for row in built["rows"]],
        )
        self.assertEqual({"All"}, {row["class_id"] for row in built["rows"]})
        # A comma is no character the Console's parser reads back: each replicate gets an alias of its own.
        self.assertEqual(5, len({row["file_name"].casefold() for row in built["rows"]}))
        self.assertEqual([], built["samples_without_input"])

    def test_a_replicate_the_disposition_excluded_takes_only_its_own_row_out(self) -> None:
        """The pilot's preflight excluded 35 of MTBLS291's 40 files as acquisition_unresolved and kept Cel's five."""
        handoff, payloads = _mtbls291()
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            _exclude(manifest_path, ["Cel_AutoMS_2-A,2_02_18612.mzML"], reason="acquisition_unresolved")
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(
            ["FILES/Cel_AutoMS_2-A,2_01_18611.mzML", "FILES/Cel_AutoMS_2-A,2_03_18613.mzML",
             "FILES/PC_AutoMS_2-B,2_01_18566.mzML", "FILES/PC_AutoMS_2-B,2_02_18567.mzML"],
            [row["sample_raw_file"] for row in built["rows"]],
        )
        self.assertEqual(
            [("Cel", "FILES/Cel_AutoMS_2-A,2_02_18612.mzML", "acquisition_unresolved")],
            [(item["sample_id"], item["sample_raw_file"], item["reason"]) for item in built["excluded_inputs"]],
        )
        self.assertEqual([], built["samples_without_input"])


# ---- what stays ambiguous -------------------------------------------------------------------------------------


class WhatIsAmbiguousIsStillRefused(unittest.TestCase):
    def _built(self, names: list[str], change) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path = _hand_made_unit(root, names)
            update_manifest(manifest_path, lambda manifest: change(manifest, root))
            return build_repository_analysis_rows(read_manifest(manifest_path))

    def test_one_row_two_inputs_name_is_refused(self) -> None:
        def change(manifest: dict, root: Path) -> None:
            # Both inputs are attributed to sample a, which has one row.
            manifest["project"]["sample_metadata"] = manifest["project"]["sample_metadata"][:1]
            for row in manifest["input_lineage"]["rows"]:
                row["sample_id"] = "a"

        built = self._built(["a.mzML", "a2.mzML"], change)

        self.assertEqual(["sample_row_with_two_inputs"], [item["code"] for item in built["failures"]])
        self.assertEqual(["a2.mzML and a.mzML (a)"], built["failures"][0]["inputs"])

    def test_an_input_two_rows_name_is_refused(self) -> None:
        def change(manifest: dict, root: Path) -> None:
            manifest["project"]["sample_metadata"].append({"sample_id": "other", "raw_file": "FILES/a.mzML"})
            for row in manifest["input_lineage"]["rows"]:
                row["sample_id"] = ""

        built = self._built(["a.mzML"], change)

        self.assertEqual(["input_with_two_sample_rows"], [item["code"] for item in built["failures"]])
        self.assertEqual(["a.mzML"], built["failures"][0]["inputs"])

    def test_two_rows_of_one_sample_naming_one_file_are_refused(self) -> None:
        def change(manifest: dict, root: Path) -> None:
            manifest["project"]["sample_metadata"].append({"sample_id": "a", "raw_file": "FILES/a.mzML"})

        built = self._built(["a.mzML"], change)

        self.assertEqual(["input_with_two_sample_rows"], [item["code"] for item in built["failures"]])

    def test_an_input_none_of_its_samples_rows_names_is_refused(self) -> None:
        def change(manifest: dict, root: Path) -> None:
            manifest["project"]["sample_metadata"] = [
                {"sample_id": "Cel", "raw_file": "FILES/r1.mzML"}, {"sample_id": "Cel", "raw_file": "FILES/r2.mzML"},
            ]
            for row in manifest["input_lineage"]["rows"]:
                row["sample_id"] = "Cel"

        built = self._built(["r1.mzML", "r3.mzML"], change)

        self.assertEqual(["sample_row_not_identified"], [item["code"] for item in built["failures"]])
        self.assertEqual(["r3.mzML"], built["failures"][0]["inputs"])
        self.assertEqual(["Cel"], built["samples_without_input"])
        self.assertEqual([{"sample_row_index": 1, "sample_id": "Cel", "raw_file": "FILES/r2.mzML"}],
                         built["sample_rows_without_input"])

    def test_the_mapping_failures_may_be_accepted_with_partial_mapping(self) -> None:
        self.assertEqual(
            {"input_without_sample", "sample_row_with_two_inputs", "input_with_two_sample_rows",
             "sample_row_not_identified"},
            set(MAPPING_FAILURES),
        )

        def change(manifest: dict, root: Path) -> None:
            manifest["project"]["sample_metadata"].append({"sample_id": "a", "raw_file": "FILES/a.mzML"})

        built = self._built(["a.mzML"], change)
        self.assertEqual([], blocking_failures(built, allow_partial_mapping=True))


# ---- ST001264: declared names the archive members carry behind a prefix ---------------------------------------


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, data in entries.items():
            handle.writestr(name, data)
    return buffer.getvalue()


PREFIX = "021518_387057_CSHp_"


def _st001264(members: list[str], samples: list[tuple[str, str]]) -> tuple[RepositoryProject, dict[str, bytes]]:
    """A Workbench unit whose one listed file is the study archive, and whose rows name the raw files."""
    data = _zip({name: f"thermo raw bytes of {name}".encode() for name in members})
    url = "https://example.org/studydownload/ST001264_POSITIVE.zip"
    project = RepositoryProject(
        repository="metabolomics_workbench", accession="ST001264", analysis_unit_id="341429fe120ee3ee9a77",
        eligible=True, selection_status="eligible", separation="LC-MS", acquisition_mode="DDA",
        ion_mode="Positive", untargeted=True, total_download_bytes=len(data),
        files=[RepositoryFile("ST001264_POSITIVE.zip", len(data), url, role="raw_archive",
                              checksum=hashlib.md5(data).hexdigest())],
        sample_metadata=[{"sample_id": sample, "raw_file": raw} for sample, raw in samples],
    )
    return project, {url: data}


ST001264_SAMPLES = [
    ("Biorec1", "BioRec1.raw"), ("Biorec2", "BioRec2.raw"), ("Biorec3", "BioRec3.raw"),
    ("Sample1", "Sample1"), ("Sample2", "Sample2"),
]
ST001264_MEMBERS = [
    f"{PREFIX}BioRec1.raw", f"{PREFIX}BioRec2.raw", f"{PREFIX}BioRec3.raw",
    f"{PREFIX}Youn_sa1.raw", f"{PREFIX}Youn_sa10.raw", f"{PREFIX}Youn_sa11.raw",
]


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name).resolve()
        limits = patch.object(repository_reanalysis, "LEASE_EXTRACTION_LIMITS", LIMITS)
        limits.start()
        self.addCleanup(limits.stop)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def lease(self, project: RepositoryProject, payloads: dict[str, bytes]) -> dict:
        lease = create_download_lease(project, self.root, 10_000_000, client=_Client(payloads))
        return read_manifest(lease["manifest_path"])

    def pairing(self, members: list[str], samples: list[tuple[str, str]], **project_changes) -> dict[str, str]:
        """_prefixed_member_pairing over these member names: {member name: declared raw file}."""
        data_root = self.root / "raw" / "data"
        project, _ = _st001264(members, samples)
        for key, value in project_changes.items():
            setattr(project, key, value)
        extracted = {_file_key(str(data_root / name)): {"path": name} for name in members}
        paired = _prefixed_member_pairing(project, extracted, data_root)
        for item in paired.values():
            self.assertEqual(PREFIXED_MEMBER_PAIRING, item["paired_by"])
        names = {_file_key(str(data_root / name)): name for name in members}
        return {names[key]: item["declared_raw_file"] for key, item in paired.items()}


class ADeclaredNameBehindAPrefixIsPairedOneToOne(_Workspace):
    def test_st001264s_members_are_paired_with_the_names_its_rows_declare(self) -> None:
        self.assertEqual(
            {f"{PREFIX}BioRec1.raw": "BioRec1.raw", f"{PREFIX}BioRec2.raw": "BioRec2.raw",
             f"{PREFIX}BioRec3.raw": "BioRec3.raw"},
            self.pairing(ST001264_MEMBERS, ST001264_SAMPLES),
        )

    def test_a_declared_name_is_never_paired_with_a_longer_one_that_ends_in_its_digits(self) -> None:
        """Youn_sa1.raw takes ..._Youn_sa1.raw, never ..._Youn_sa11.raw: a whole name follows the separator."""
        self.assertEqual(
            {f"{PREFIX}Youn_sa1.raw": "Youn_sa1.raw"},
            self.pairing(ST001264_MEMBERS, [("s1", "Youn_sa1.raw")]),
        )

    def test_a_name_a_row_records_without_an_extension_is_matched_against_the_members_stem(self) -> None:
        self.assertEqual({"run_QC_01.raw": "QC_01"}, self.pairing(["run_QC_01.raw", "run_QC_011.raw"], [("q", "QC_01")]))

    def test_an_exact_match_wins_and_its_name_is_not_paired_again(self) -> None:
        self.assertEqual({}, self.pairing(["S1.raw", "x_S1.raw"], [("s", "S1.raw")]))

    def test_a_declared_name_the_listing_names_itself_is_not_paired(self) -> None:
        listed = [RepositoryFile("S1.raw", 1, "https://example.org/S1.raw")]
        self.assertEqual({}, self.pairing(["x_S1.raw"], [("s", "S1.raw")], files=listed))

    def test_a_declared_name_two_members_carry_is_paired_with_neither(self) -> None:
        """A shared study archive with a folder per polarity holds a run of the same name in each."""
        self.assertEqual({}, self.pairing(["POS/x_S1.raw", "NEG/x_S1.raw"], [("s", "S1.raw")]))

    def test_a_member_that_ends_in_two_declared_names_is_paired_with_neither(self) -> None:
        self.assertEqual({}, self.pairing(["x_A_B.raw"], [("a", "A_B.raw"), ("b", "B.raw")]))

    def test_only_a_separator_may_precede_the_declared_name(self) -> None:
        self.assertEqual(
            {"run-S1.raw": "S1.raw"}, self.pairing(["run-S1.raw", "runS2.raw"], [("1", "S1.raw"), ("2", "S2.raw")])
        )

    def test_a_unit_whose_catalog_declared_its_inputs_is_matched_by_path_and_never_by_prefix(self) -> None:
        declared = [{"path": "S1.raw", "kind": "file", "sample_id": "s"}]
        self.assertEqual({}, self.pairing(["x_S1.raw"], [("s", "S1.raw")], analysis_inputs=declared))


class TheLeaseAttributesAPrefixedMember(_Workspace):
    def test_an_st001264_shaped_unit_leases_the_members_its_rows_name_behind_a_prefix(self) -> None:
        project, payloads = _st001264(ST001264_MEMBERS, ST001264_SAMPLES)
        manifest = self.lease(project, payloads)

        self.assertEqual(
            [f"{PREFIX}BioRec1.raw", f"{PREFIX}BioRec2.raw", f"{PREFIX}BioRec3.raw"],
            [Path(item).name for item in manifest["input_candidates"]],
        )
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        self.assertEqual("Biorec1", rows[f"{PREFIX}BioRec1.raw"]["sample_id"])
        self.assertEqual(
            {"declared_raw_file": "BioRec1.raw", "member_name": f"{PREFIX}BioRec1.raw",
             "paired_by": PREFIXED_MEMBER_PAIRING},
            rows[f"{PREFIX}BioRec1.raw"]["name_pairing"],
        )
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual(3, attribute["prefixed_member_pairings"])
        self.assertEqual(
            sorted(f"{PREFIX}BioRec{index}.raw" for index in (1, 2, 3)),
            sorted(Path(item).name for item in manifest["extracted_files"]),
        )

    def test_the_csv_finds_the_same_sample_rows_and_says_which_rows_have_no_input(self) -> None:
        project, payloads = _st001264(ST001264_MEMBERS, ST001264_SAMPLES)
        manifest = self.lease(project, payloads)
        built = build_repository_analysis_rows(manifest)

        self.assertEqual([], built["failures"])
        self.assertEqual(
            [("Biorec1", "BioRec1.raw"), ("Biorec2", "BioRec2.raw"), ("Biorec3", "BioRec3.raw")],
            [(row["sample_id"], row["sample_raw_file"]) for row in built["rows"]],
        )
        # The rows no member carries the name of, exactly or behind a prefix, are said, as before.
        self.assertEqual(["Sample1", "Sample2"], built["samples_without_input"])

    def test_replicate_rows_are_told_apart_by_the_names_their_members_carry(self) -> None:
        project, payloads = _st001264(["p_R_1.raw", "p_R_2.raw"], [("R", "R_1.raw"), ("R", "R_2.raw")])
        manifest = self.lease(project, payloads)
        built = build_repository_analysis_rows(manifest)

        self.assertEqual([], built["failures"])
        self.assertEqual([("R", "R_1.raw", 0), ("R", "R_2.raw", 1)],
                         [(row["sample_id"], row["sample_raw_file"], row["sample_row_index"]) for row in built["rows"]])

    def test_without_a_one_to_one_pairing_the_lease_still_refuses_to_fall_back(self) -> None:
        project, payloads = _st001264(["POS/x_S1.raw", "NEG/x_S1.raw"], [("s", "S1.raw")])

        with self.assertRaisesRegex(ValueError, "Refusing to fall back to accession-level inputs"):
            self.lease(project, payloads)


if __name__ == "__main__":
    unittest.main()
