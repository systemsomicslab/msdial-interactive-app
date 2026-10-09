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

import copy
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
    from msdial_app.encoding_rule import RULE
    from msdial_app.repository_analysis_rows import (
        MAPPING_FAILURES,
        PARTIAL_SAMPLE_COVERAGE_WARNING,
        analysis_csv_change,
        blocking_failures,
        build_repository_analysis_rows,
    )
    from msdial_app.repository_reanalysis import (
        INFERRED_PAIRING_WARNING,
        LEADING_IDENTIFIER_TOKEN_PAIRING,
        PREFIXED_MEMBER_PAIRING,
        UNATTRIBUTED_MEMBER_PAIRING,
        RepositoryFile,
        RepositoryProject,
        _file_key,
        _member_name_pairings,
        _prefixed_member_pairing,
        create_download_lease,
        evaluate_repository_execution_gate,
        leading_identifier_key,
        name_polarities,
        names_state_polarity,
        plan_acquisition_split,
        project_from_dict,
        read_manifest,
        run_raw_metadata_preflight,
        split_unit_by_acquisition,
        update_manifest,
    )

from test_folder_inputs import _Client, _exclude, _hand_made_unit, _no_backend
from test_split_key import _Unit, _extractor
from test_mzml_encoding import _numpress_mzml, dda_spectra, mzml
from test_raw_metadata_preflight import _APPROVAL

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


def _lease(root: Path, handoff: dict, payloads: dict[str, bytes], *, campaign: bool = False) -> Path:
    project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff)
    typed = project_from_dict(project)
    typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
    return Path(
        create_download_lease(
            typed, root, 10_000_000, client=_Client(payloads),
            **({"campaign_authorization": dict(_APPROVAL)} if campaign else {}),
        )["manifest_path"]
    )


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


def _st001264(
    members: list[str], samples: list[tuple[str, str]], contents: dict[str, bytes] | None = None
) -> tuple[RepositoryProject, dict[str, bytes]]:
    """A Workbench unit whose one listed file is the study archive, and whose rows name the raw files. ``contents``
    gives a member bytes of its own (a real mzXML for the convert stage); every other holds a line of text."""
    data = _zip({name: (contents or {}).get(name) or f"thermo raw bytes of {name}".encode() for name in members})
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


# ---- ST001264: the members no row pairs with, taken as unattributed inputs (user decision, 2026-10-07) ---------


UNIT_SCOPED = {
    "kind": "unit_files",
    "bundle_urls": [{"url": "https://example.org/studydownload/ST001264_POSITIVE.zip", "shared_unit_count": 1}],
}
SHARED = {
    "kind": "accession_archive",
    "bundle_urls": [{"url": "https://example.org/studydownload/ST001264_POSITIVE.zip", "shared_unit_count": 2}],
}
YOUN = [f"{PREFIX}Youn_sa1.raw", f"{PREFIX}Youn_sa10.raw", f"{PREFIX}Youn_sa11.raw"]
ABSTENTION = {
    "status": "accepted",
    "assignments": [{"sample_id": sample, "class_label": "All", "values": {}} for sample, _raw in ST001264_SAMPLES],
    "contrast_definition": {"kind": "abstention", "class_label": "All", "reason": "no_declared_factor", "considered": []},
}


class TheMembersNoRowPairsWithAreUnattributedInputs(_Workspace):
    def unit(
        self, scope: dict, members: list[str] = ST001264_MEMBERS, samples=ST001264_SAMPLES, *,
        contents: dict[str, bytes] | None = None, campaign: bool = False,
    ) -> dict:
        project, payloads = _st001264(members, samples, contents)
        project.download_scope = dict(scope)
        if not campaign:
            return self.lease(project, payloads)
        # A campaign's lease, whose convert stage converts the unit's mzXML (2026-09-30).
        lease = create_download_lease(
            project, self.root, 10_000_000, client=_Client(payloads), campaign_authorization=dict(_APPROVAL)
        )
        return read_manifest(lease["manifest_path"])

    def test_a_unit_scoped_archive_takes_every_member_and_records_the_unattributed_ones(self) -> None:
        from msdial_app.raw_metadata_preflight import decide_disposition

        manifest = self.unit(UNIT_SCOPED)

        self.assertEqual(sorted(ST001264_MEMBERS), sorted(Path(item).name for item in manifest["input_candidates"]))
        self.assertEqual(
            {"rule": "unit_scoped_archive_2026_10_07", "applied": True, "count": 3, "members": YOUN, "paths": YOUN,
             "scope": "unit_files"},
            manifest["unattributed_members"],
        )
        self.assertEqual([INFERRED_PAIRING_WARNING, "unattributed_members_included"], manifest["warnings"])
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        youn = rows[f"{PREFIX}Youn_sa1.raw"]
        self.assertEqual({"paired_by": "unattributed_member", "member_name": f"{PREFIX}Youn_sa1.raw"}, youn["name_pairing"])
        self.assertEqual(f"{PREFIX}Youn_sa1", youn["sample_id"])
        self.assertIn("sample_row", youn)
        self.assertIsNone(youn["sample_row"])
        # The paired members are as before.
        self.assertEqual(PREFIXED_MEMBER_PAIRING, rows[f"{PREFIX}BioRec1.raw"]["name_pairing"]["paired_by"])
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual((3, 3), (attribute["prefixed_member_pairings"], attribute["unattributed_members"]))
        self.assertIn("unattributed_members_included", attribute["warnings"])
        self.assertEqual(sorted(ST001264_MEMBERS), sorted(Path(item).name for item in manifest["extracted_files"]))
        # Every disposition of the unit carries it, whatever it decides.
        self.assertIn("unattributed_members_included", decide_disposition(manifest)["warnings"])

    def test_the_csv_gives_an_unattributed_input_no_sample_row_and_the_abstentions_class(self) -> None:
        manifest = self.unit(UNIT_SCOPED)
        built = build_repository_analysis_rows(manifest)

        self.assertEqual([], built["failures"])
        rows = {Path(row["input_path"]).name: row for row in built["rows"]}
        self.assertEqual(6, len(rows))
        youn = rows[f"{PREFIX}Youn_sa1.raw"]
        self.assertEqual(
            ("Unattributed", "Sample", f"{PREFIX}Youn_sa1", None, "unattributed_member"),
            (youn["class_id"], youn["file_type"], youn["sample_id"], youn["sample_row_index"], youn["raw_file_paired_by"]),
        )
        self.assertEqual("Biorec1", rows[f"{PREFIX}BioRec1.raw"]["sample_id"])
        self.assertIn("unattributed_members_included", built["warnings"])
        self.assertIn(PARTIAL_SAMPLE_COVERAGE_WARNING, built["warnings"])
        self.assertEqual(["Sample1", "Sample2"], built["samples_without_input"])
        self.assertEqual(3, len(built["inferred_name_pairings"]))
        self.assertEqual(sorted(YOUN), sorted(item["member_name"] for item in built["unattributed_inputs"]))

        # Under an abstention, the abstention's one Class.
        manifest["project"]["class_proposal"] = ABSTENTION
        self.assertEqual({"All"}, {row["class_id"] for row in build_repository_analysis_rows(manifest)["rows"]})

        # The reviewed sample table gets a row per unattributed member, after the unit's own rows.
        review = mcp_server._with_raw_file_paired_by(
            {"rows": [{"sample_id": sample, "raw_file": raw} for sample, raw in ST001264_SAMPLES]}, built
        )
        self.assertEqual(
            [("Biorec1", "prefixed_member_name"), ("Biorec2", "prefixed_member_name"), ("Biorec3", "prefixed_member_name"),
             ("Sample1", ""), ("Sample2", ""),
             *((Path(name).stem, "unattributed_member") for name in YOUN)],
            [(row["sample_id"], row["raw_file_paired_by"]) for row in review["rows"]],
        )
        self.assertEqual({"Unattributed"}, {row["class_id"] for row in review["rows"][5:]})

    def test_a_shared_archive_takes_none_and_says_why(self) -> None:
        manifest = self.unit(SHARED)

        self.assertEqual(3, len(manifest["input_candidates"]))
        record = manifest["unattributed_members"]
        self.assertEqual(
            (False, "shared_archive", 0, []), (record["applied"], record["reason"], record["count"], record["members"])
        )
        self.assertEqual(YOUN, [item["member_name"] for item in record["left_out"]])
        self.assertEqual({"shared_archive"}, {item["reason"] for item in record["left_out"]})
        self.assertEqual([INFERRED_PAIRING_WARNING], manifest["warnings"])

    def test_a_scope_that_says_nothing_takes_none(self) -> None:
        manifest = self.unit({})

        self.assertEqual(3, len(manifest["input_candidates"]))
        self.assertEqual("download_scope_not_unit_scoped", manifest["unattributed_members"]["reason"])

    def test_a_unit_every_member_of_which_pairs_records_nothing(self) -> None:
        manifest = self.unit(UNIT_SCOPED, members=ST001264_MEMBERS[:3], samples=ST001264_SAMPLES[:3])

        self.assertEqual(3, len(manifest["input_candidates"]))
        self.assertNotIn("unattributed_members", manifest)
        self.assertEqual([INFERRED_PAIRING_WARNING], manifest["warnings"])

    def test_an_other_polarity_member_and_an_mzxml_are_left_out_on_record(self) -> None:
        # Outside a campaign nothing converts, so an unpaired mzXML stays out (requires_conversion), and a member whose
        # path names the other polarity stays out. Which file of a sample runs is the one encoding rule's
        # (2026-10-09): y_S6.raw over y_S6.mzML, the row's BioRec1 .raw over its unpaired .mzML, and of two vendor
        # encodings of one name the first by path (z_S5.d before z_S5.raw). Each unused file is on record in
        # encoding_choices, not in left_out.
        members = [
            *ST001264_MEMBERS[:3], "x_S9_neg.raw", "x_S8.mzXML", "x_S7.raw", "y_S6.raw", "y_S6.mzML",
            f"{PREFIX}BioRec1.mzML", "z_S5.raw", "z_S5.d/analysis.baf",
        ]
        manifest = self.unit(UNIT_SCOPED, members=members)

        self.assertEqual(
            sorted([*ST001264_MEMBERS[:3], "x_S7.raw", "y_S6.raw", "z_S5.d"]),
            sorted(Path(item).name for item in manifest["input_candidates"]),
        )
        record = manifest["unattributed_members"]
        self.assertEqual(["x_S7.raw", "y_S6.raw", "z_S5.d"], record["members"])
        self.assertEqual(
            [("x_S8.mzXML", "requires_conversion"), ("x_S9_neg.raw", "polarity_token_contradicts_ion_mode")],
            [(item["member_name"], item["reason"]) for item in record["left_out"]],
        )
        self.assertEqual(2, record["left_out_count"])
        self.assertEqual(
            [
                {"rule": RULE, "used": f"{PREFIX}BioRec1.raw",
                 "unused": [{"path": f"{PREFIX}BioRec1.mzML", "reason": "lower_in_encoding_order"}]},
                {"rule": RULE, "used": "y_S6.raw", "unused": [{"path": "y_S6.mzML", "reason": "lower_in_encoding_order"}]},
                {"rule": RULE, "used": "z_S5.d", "unused": [{"path": "z_S5.raw", "reason": "tie_lexicographic"}]},
            ],
            manifest["encoding_choices"],
        )
        assert_every_member_on_record(self, manifest, [*members[:10], "z_S5.raw", "z_S5.d"])

    def test_a_campaign_converts_an_unpaired_mzxml_and_takes_it_unattributed_on_record(self) -> None:
        from test_mzxml_conversion import dda_32

        members = [*ST001264_MEMBERS[:3], "x_S8.mzXML", "x_S9_neg.mzXML", "y_S6.raw", "mzXML/y_S6.mzXML"]
        contents = {name: dda_32() for name in members if name.endswith(".mzXML")}
        manifest = self.unit(UNIT_SCOPED, members=members, contents=contents, campaign=True)

        record = manifest["unattributed_members"]
        self.assertEqual((2, ["x_S8.mzXML", "y_S6.raw"], ["x_S8.mzXML", "y_S6.raw"]),
                         (record["count"], record["members"], record["paths"]))
        self.assertEqual(["x_S8.mzXML"], record["converted"])
        self.assertEqual(
            [("x_S9_neg.mzXML", "polarity_token_contradicts_ion_mode")],
            [(item["path"], item["reason"]) for item in record["left_out"]],
        )
        # Of y_S6's two encodings the one encoding rule uses the .raw, and the mzXML is not converted.
        self.assertEqual(
            [{"rule": RULE, "used": "y_S6.raw", "unused": [{"path": "mzXML/y_S6.mzXML", "reason": "lower_in_encoding_order"}]}],
            manifest["encoding_choices"],
        )
        # The mzML the convert stage wrote is the input; its lineage row carries the conversion and names the mzXML.
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        converted = rows["x_S8.mzML"]
        self.assertEqual("converted", converted["kind"])
        self.assertEqual({"paired_by": "unattributed_member", "member_name": "x_S8.mzXML"}, converted["name_pairing"])
        self.assertEqual(("x_S8", None), (converted["sample_id"], converted["sample_row"]))
        self.assertIn("conversion", converted["source"])
        self.assertEqual(
            ["x_S8.mzXML"],
            [item["source"]["relative_path"] for item in manifest["input_conversions"]["records"]
             if item.get("status") == "converted"],
        )
        self.assertEqual(
            sorted([*ST001264_MEMBERS[:3], "x_S8.mzML", "y_S6.raw"]),
            sorted(Path(item).name for item in manifest["input_candidates"]),
        )
        self.assertIn("unattributed_members_included", manifest["warnings"])
        # The CSV gives it no sample row, as any unattributed input.
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        row = next(item for item in built["rows"] if Path(item["input_path"]).name == "x_S8.mzML")
        self.assertEqual(("Unattributed", "unattributed_member"), (row["class_id"], row["raw_file_paired_by"]))

    def test_a_campaign_converts_no_unpaired_mzxml_of_a_shared_archive(self) -> None:
        from test_mzxml_conversion import dda_32

        members = [*ST001264_MEMBERS[:3], "x_S8.mzXML"]
        manifest = self.unit(SHARED, members=members, contents={"x_S8.mzXML": dda_32()}, campaign=True)

        record = manifest["unattributed_members"]
        self.assertEqual((False, 0), (record["applied"], record["count"]))
        self.assertEqual([("x_S8.mzXML", "shared_archive")], [(item["path"], item["reason"]) for item in record["left_out"]])
        self.assertNotIn("input_conversions", manifest)
        self.assertEqual(sorted(ST001264_MEMBERS[:3]), sorted(Path(item).name for item in manifest["input_candidates"]))

    def assert_the_gate_reads_the_record_as_the_lineage(self, manifest: dict) -> None:
        """What the gate's PAIR-1 compares (review ia-r2 follow-up 1): count is the number of unattributed lineage
        rows, inputs and lease-excluded alike, and members lists each one's member_name."""
        lineage = manifest["input_lineage"]
        rows = {
            row["path"].casefold(): row["name_pairing"]["member_name"]
            for part in ("rows", "excluded")
            for row in lineage.get(part) or []
            if (row.get("name_pairing") or {}).get("paired_by") == "unattributed_member"
        }
        record = manifest["unattributed_members"]
        self.assertEqual(len(rows), record["count"])
        self.assertEqual(sorted(rows.values()), sorted(record["members"]))
        if rows:
            self.assertIn("unattributed_members_included", manifest["warnings"])

    def test_outside_a_campaign_an_undecodable_unpaired_mzml_is_listed_as_excluded(self) -> None:
        # Nothing converts, so the mzXML stays out; the mzML is admitted, and the lease excludes it. It is on record
        # in count, members and excluded, as the gate counts the lineage's excluded rows too.
        from test_mzml_encoding import _numpress_mzml

        members = [*ST001264_MEMBERS[:3], "w_S2.mzXML", "w_S2.mzML"]
        manifest = self.unit(UNIT_SCOPED, members=members, contents={"w_S2.mzML": _numpress_mzml()})

        record = manifest["unattributed_members"]
        self.assertEqual((1, ["w_S2.mzML"]), (record["count"], record["members"]))
        self.assertEqual(
            [{"member_name": "w_S2.mzML", "path": "w_S2.mzML", "reason": "unsupported_mzml_encoding"}], record["excluded"]
        )
        self.assertEqual([("w_S2.mzXML", "requires_conversion")],
                         [(item["path"], item["reason"]) for item in record["left_out"]])
        self.assertNotIn("w_S2.mzML", [Path(item).name for item in manifest["input_candidates"]])
        self.assert_the_gate_reads_the_record_as_the_lineage(manifest)

    def test_an_unpaired_member_whose_conversion_fails_is_counted_and_listed_as_excluded(self) -> None:
        # Review ia-r2 follow-up 1, medium: count 0 and no member, while the lineage's excluded rows held it, so
        # the gate's PAIR-1 FAILed (record only).
        manifest = self.unit(
            UNIT_SCOPED, members=[*ST001264_MEMBERS[:3], "f_S2.mzXML"], contents={"f_S2.mzXML": b"<not an mzXML"},
            campaign=True,
        )

        record = manifest["unattributed_members"]
        self.assertEqual((1, ["f_S2.mzXML"], ["f_S2.mzXML"]), (record["count"], record["members"], record["paths"]))
        self.assertEqual(
            [{"member_name": "f_S2.mzXML", "path": "f_S2.mzXML", "reason": "conversion_failed"}], record["excluded"]
        )
        self.assertNotIn("converted", record)
        self.assertEqual(sorted(ST001264_MEMBERS[:3]), sorted(Path(item).name for item in manifest["input_candidates"]))
        self.assert_the_gate_reads_the_record_as_the_lineage(manifest)
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertIn("unattributed_members_included", attribute["warnings"])

    def test_a_declared_unit_records_the_members_no_declaration_names(self) -> None:
        # R2-3 keeps them out "on record". At 4722776 a declared unit carried no unattributed_members at all, and the
        # archive's member listing was their only trace.
        members = ["S1.raw", "S2.raw", "S3.mzML", "x_S9_neg.raw"]
        project, payloads = _st001264(members, [("Sample1", "S1.raw")])
        project.download_scope = dict(UNIT_SCOPED)
        project.analysis_inputs = [{"path": "S1.raw", "kind": "file", "sample_id": "Sample1"}]
        manifest = self.lease(project, payloads)

        self.assertEqual(["S1.raw"], [Path(item).name for item in manifest["input_candidates"]])
        record = manifest["unattributed_members"]
        self.assertEqual(
            {"rule": "unit_scoped_archive_2026_10_07", "applied": False, "count": 0, "members": [], "paths": [],
             "reason": "catalog_declared_inputs", "left_out_count": 3,
             "left_out": [{"member_name": name, "path": name, "reason": "not_named_by_the_catalog_declaration"}
                          for name in ["S2.raw", "S3.mzML", "x_S9_neg.raw"]]},
            record,
        )
        self.assertNotIn("unattributed_members_included", manifest.get("warnings") or [])
        assert_every_member_on_record(self, manifest, members)

    def test_a_declared_unit_whose_every_member_is_declared_records_nothing(self) -> None:
        project, payloads = _st001264(["S1.raw"], [("Sample1", "S1.raw")])
        project.download_scope = dict(UNIT_SCOPED)
        project.analysis_inputs = [{"path": "S1.raw", "kind": "file", "sample_id": "Sample1"}]
        self.assertNotIn("unattributed_members", self.lease(project, payloads))

    def test_a_nested_archive_names_its_members_by_basename_as_the_lineage_does(self) -> None:
        # Review ia-0531, medium: the record listed 'ST001264_POSITIVE/<name>', the lineage '<name>', so the
        # gate found no lineage member in the record. Both now give the basename; paths keeps where each lies.
        nested = [f"ST001264_POSITIVE/{name}" for name in ST001264_MEMBERS]
        twins = ["ST001264_POSITIVE/a/QC_01.raw", "ST001264_POSITIVE/b/QC_01.raw"]
        other = "ST001264_POSITIVE/neg/x_S9_neg.raw"
        manifest = self.unit(UNIT_SCOPED, members=[*nested, *twins, other])
        record = manifest["unattributed_members"]

        # The two copies of QC_01.raw are one sample's (the one encoding rule, 2026-10-09): the first path runs.
        self.assertEqual(4, record["count"])
        self.assertEqual(sorted([*YOUN, "QC_01.raw"], key=lambda item: (item.casefold(), item)), record["members"])
        self.assertEqual(
            sorted([f"ST001264_POSITIVE/{name}" for name in YOUN] + twins[:1], key=lambda item: (item.casefold(), item)),
            record["paths"],
        )
        self.assertEqual(
            [{"rule": RULE, "used": twins[0], "unused": [{"path": twins[1], "reason": "tie_lexicographic"}]}],
            manifest["encoding_choices"],
        )
        lineage = sorted(
            row["name_pairing"]["member_name"] for row in manifest["input_lineage"]["rows"]
            if (row.get("name_pairing") or {}).get("paired_by") == "unattributed_member"
        )
        self.assertEqual(sorted(record["members"]), lineage)
        self.assertEqual(
            [("x_S9_neg.raw", other, "polarity_token_contradicts_ion_mode")],
            [(item["member_name"], item["path"], item["reason"]) for item in record["left_out"]],
        )

    def test_a_shared_nested_archive_lists_its_left_out_members_by_basename_and_path(self) -> None:
        nested = [f"ST001264_POSITIVE/{name}" for name in ST001264_MEMBERS]
        record = self.unit(SHARED, members=nested)["unattributed_members"]

        self.assertEqual((0, [], []), (record["count"], record["members"], record["paths"]))
        self.assertEqual(YOUN, [item["member_name"] for item in record["left_out"]])
        self.assertEqual([f"ST001264_POSITIVE/{name}" for name in YOUN], [item["path"] for item in record["left_out"]])

    def test_a_unit_no_row_of_which_pairs_runs_on_its_unattributed_members_alone(self) -> None:
        manifest = self.unit(UNIT_SCOPED, members=YOUN, samples=[("Sample1", "Sample1")])

        self.assertEqual(sorted(YOUN), sorted(Path(item).name for item in manifest["input_candidates"]))
        self.assertEqual(["unattributed_members_included"], manifest["warnings"])

    def test_unit_scope_is_read_from_the_catalogs_download_scope(self) -> None:
        from msdial_app.repository_reanalysis import unit_scoped_download

        def scoped(scope: dict) -> tuple[bool, str]:
            project, _payloads = _st001264(ST001264_MEMBERS[:1], ST001264_SAMPLES[:1])
            project.download_scope = scope
            return unit_scoped_download(project)

        self.assertEqual((True, "unit_files"), scoped({"kind": "unit_files"}))
        self.assertEqual(
            (True, "bundle_urls_unit_scoped"),
            scoped({"kind": "accession_archive", "bundle_urls": [{"url": "a", "shared_unit_count": 1}]}),
        )
        self.assertEqual(
            (False, "shared_archive"),
            scoped({"kind": "unit_files", "bundle_urls": [{"url": "a", "shared_unit_count": 1},
                                                          {"url": "b", "shared_unit_count": 3}]}),
        )
        self.assertEqual((False, "shared_archive"), scoped({"kind": "unit_files", "bundle_shared_unit_count": 2}))
        self.assertEqual((False, "download_scope_not_unit_scoped"), scoped({"kind": "accession_archive"}))


# ---- The user's one encoding rule of 2026-10-09 ("A: この一つのルールで統一"), on real leases ----------------------
#
# When one sample's data arrive in several encodings (copies in other folders included): of the readable ones the
# lease uses exactly one, vendor -> mzML -> mzXML (clause 1); a tie goes to the first path, without case (2); one
# that cannot be read or decoded, or whose conversion fails, gives way to the next (3); the file used is the sample's
# own input whatever encoding its row names (4); and every file not used is on record with its reason (5). The
# function itself is held to clauses 1, 2, 3 and 5 in test_encoding_rule.


def assert_every_member_on_record(test: unittest.TestCase, manifest: dict, members: list[str]) -> None:
    """Every member is on record with what happened to it, consistent with what runs: an input (itself, or the mzXML
    an input was converted from), a candidate the lease excluded, a member unattributed_members left out with a
    reason, or a file the rule left unused in encoding_choices. A file the rule used runs; a file it left unused, or
    a member left out, does not; a lineage row's encoding_choice is its sample's record; and no sample row has two
    inputs."""
    root = manifest["input_directory"]

    def member(row: dict) -> str:
        conversion = (row.get("source") or {}).get("conversion") or {}
        return os.path.relpath(conversion.get("source_path") or row["path"], root).replace("\\", "/").casefold()

    running = {member(row): row for row in manifest["input_lineage"]["rows"]}
    excluded = {member(row): row for row in manifest["input_lineage"].get("excluded") or []}
    left_out = {item["path"].casefold(): item for item in (manifest.get("unattributed_members") or {}).get("left_out") or []}
    unused: dict[str, dict] = {}
    records = manifest.get("encoding_choices") or []
    for choice in records:
        test.assertEqual(RULE, choice["rule"])
        if choice["used"] is not None:
            test.assertIn(choice["used"].casefold(), running, f"{choice['used']} is used, and it does not run")
        for item in choice["unused"]:
            test.assertTrue(item["reason"], item)
            unused[item["path"].casefold()] = item
    for name in members:
        test.assertTrue({name.casefold()} & {*running, *excluded, *left_out, *unused}, f"{name} is on no record")
    for name in [*unused, *left_out]:
        test.assertNotIn(name, running, f"{name} is left unused or out, and it runs")
    for row in manifest["input_lineage"]["rows"]:
        choice = row.get("encoding_choice")
        if choice and choice["unused"]:
            test.assertIn({key: value for key, value in choice.items() if key != "stands_for"}, records)
    built = build_repository_analysis_rows(manifest)
    test.assertNotIn("sample_row_with_two_inputs", [item["code"] for item in built["failures"]])


def _with_classes(manifest: dict, samples: list[tuple[str, str]], treated: str) -> dict:
    """The manifest under a two-group Class proposal: ``treated`` is Treated, every other sample Control."""
    manifest["project"]["class_proposal"] = {
        "status": "accepted",
        "assignments": [
            {"sample_id": sample, "class_label": "Treated" if sample == treated else "Control", "values": {}}
            for sample, _raw in samples
        ],
        "contrast_definition": {"kind": "two_group", "class_label": ""},
    }
    return manifest


class TheOneEncodingRule(_Workspace):
    TRIO = ST001264_MEMBERS[:3]
    TRIO_ROWS = ST001264_SAMPLES[:3]

    def unit(
        self, members: list[str], samples=None, *, scope: dict = UNIT_SCOPED, contents: dict[str, bytes] | None = None,
        campaign: bool = False, header_reader=None, ion_mode: str | None = None,
    ) -> dict:
        project, payloads = _st001264([*self.TRIO, *members], [*self.TRIO_ROWS, *(samples or [])], contents)
        project.download_scope = dict(scope)
        if ion_mode is not None:
            project.ion_mode = ion_mode
        lease = create_download_lease(
            project, self.root, 10_000_000, client=_Client(payloads),
            **({"campaign_authorization": dict(_APPROVAL)} if campaign else {}),
            **({"header_reader": header_reader} if header_reader is not None else {}),
        )
        return read_manifest(lease["manifest_path"])

    @staticmethod
    def headers(unreadable: dict[str, str], calls: list[list[str]]):
        """A lease header reader: '' for every file but ``unreadable``'s (by file name), recording what it was asked."""

        def read(paths: list[Path], work_directory: Path) -> dict[str, str]:
            calls.append(sorted(Path(item).name for item in paths))
            return {_file_key(str(item)): unreadable.get(Path(item).name, "") for item in paths}

        return read

    @staticmethod
    def names(manifest: dict) -> list[str]:
        return sorted(
            os.path.relpath(item, manifest["input_directory"]).replace("\\", "/") for item in manifest["input_candidates"]
        )

    @staticmethod
    def rows(manifest: dict) -> dict[str, dict]:
        return {
            os.path.relpath(
                ((row.get("source") or {}).get("conversion") or {}).get("source_path") or row["path"],
                manifest["input_directory"],
            ).replace("\\", "/"): row
            for row in manifest["input_lineage"]["rows"]
        }

    def choice(self, manifest: dict, used: str | None) -> list[tuple[str, str]]:
        """The (path, reason) of each file the rule left unused for the sample it used ``used`` for."""
        found = [item for item in manifest.get("encoding_choices") or [] if item["used"] == used]
        self.assertEqual(1, len(found), manifest.get("encoding_choices"))
        return [(item["path"], item["reason"]) for item in found[0]["unused"]]

    def csv_row(self, manifest: dict, member: str) -> dict:
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        path = self.rows(manifest)[member]["path"]
        return next(item for item in built["rows"] if item["input_path"] == path)

    # ---- clause 1: vendor, then mzML, then mzXML ---------------------------------------------------------------

    def test_clause_1_a_vendor_file_is_used_before_an_mzml_and_an_mzxml(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S1.raw", "S1.mzML", "S1.mzXML"], [("Sample1", "S1.raw")],
            contents={"S1.mzML": mzml(dda_spectra(6)), "S1.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual(sorted([*self.TRIO, "S1.raw"]), self.names(manifest))
        self.assertEqual([("S1.mzML", "lower_in_encoding_order"), ("S1.mzXML", "lower_in_encoding_order")],
                         self.choice(manifest, "S1.raw"))
        # The mzXML is not converted: the rule never reached it.
        self.assertEqual([], manifest["input_conversions"]["records"])
        self.assertEqual(1, manifest["input_conversions"]["counts"]["not_converted_readable_encoding"])
        self.assertNotIn("excluded_input_candidates", manifest)
        assert_every_member_on_record(self, manifest, ["S1.raw", "S1.mzML", "S1.mzXML"])

    def test_clause_1_an_mzml_is_used_before_an_mzxml_in_a_campaign(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S2.mzML", "S2.mzXML"], [("Sample2", "S2.mzXML")],
            contents={"S2.mzML": mzml(dda_spectra(6)), "S2.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual(sorted([*self.TRIO, "S2.mzML"]), self.names(manifest))
        self.assertEqual([("S2.mzXML", "lower_in_encoding_order")], self.choice(manifest, "S2.mzML"))
        self.assertEqual([], manifest["input_conversions"]["records"])
        # The row names the mzXML; the mzML used is that sample's input (clause 4), through the mzXML.
        row = self.rows(manifest)["S2.mzML"]
        self.assertEqual("Sample2", row["sample_id"])
        self.assertEqual("S2.mzXML", Path(row["encoding_choice"]["stands_for"]).name)
        self.assertEqual("Sample2", self.csv_row(manifest, "S2.mzML")["sample_id"])
        assert_every_member_on_record(self, manifest, ["S2.mzML", "S2.mzXML"])

    def test_clause_1_outside_a_campaign_an_mzxml_is_no_input(self) -> None:
        # Nothing converts it: a readable encoding of its sample is used, and alone it is no input at all.
        manifest = self.unit(
            ["S2.mzML", "S2.mzXML", "S3.mzXML"], [("Sample2", "S2.mzXML"), ("Sample3", "S3.mzXML")],
            contents={"S2.mzML": mzml(dda_spectra(6))},
        )

        self.assertEqual(sorted([*self.TRIO, "S2.mzML"]), self.names(manifest))
        self.assertEqual([("S2.mzXML", "lower_in_encoding_order")], self.choice(manifest, "S2.mzML"))
        self.assertNotIn("input_conversions", manifest)
        self.assertEqual("Sample2", self.csv_row(manifest, "S2.mzML")["sample_id"])
        self.assertIn("Sample3", build_repository_analysis_rows(manifest)["samples_without_input"])

    # ---- clause 2: a tie goes to the first path, without case ---------------------------------------------------

    def test_clause_2_copies_in_two_folders_tie_and_the_first_path_is_used(self) -> None:
        # One name a row admits in two folders, and an unpaired name in two folders: one input each, the first by
        # path (not the one nearest the data root: 'raw/s4.raw' sorts before 's4.raw').
        manifest = self.unit(
            ["b/S1.raw", "A/S1.raw", "S4.raw", "RAW/S4.raw"], [("Sample1", "S1.raw")],
        )

        self.assertEqual(sorted([*self.TRIO, "A/S1.raw", "RAW/S4.raw"]), self.names(manifest))
        self.assertEqual([("b/S1.raw", "tie_lexicographic")], self.choice(manifest, "A/S1.raw"))
        self.assertEqual([("S4.raw", "tie_lexicographic")], self.choice(manifest, "RAW/S4.raw"))
        self.assertEqual("Sample1", self.csv_row(manifest, "A/S1.raw")["sample_id"])
        record = manifest["unattributed_members"]
        self.assertEqual((1, ["RAW/S4.raw"]), (record["count"], record["paths"]))
        assert_every_member_on_record(self, manifest, ["b/S1.raw", "A/S1.raw", "S4.raw", "RAW/S4.raw"])

    def test_clause_2_two_vendor_formats_tie(self) -> None:
        manifest = self.unit(["z_S5.raw", "z_S5.d/analysis.baf"])

        self.assertEqual(sorted([*self.TRIO, "z_S5.d"]), self.names(manifest))
        self.assertEqual([("z_S5.raw", "tie_lexicographic")], self.choice(manifest, "z_S5.d"))

    # ---- clause 3: one that cannot be read gives way to the next ------------------------------------------------

    def test_clause_3_an_undecodable_mzml_gives_way_to_the_next_in_order(self) -> None:
        manifest = self.unit(
            ["A/S1.mzML", "S1.mzML"], [("Sample1", "S1.mzML")],
            contents={"A/S1.mzML": _numpress_mzml(), "S1.mzML": mzml(dda_spectra(6))},
        )

        self.assertEqual(sorted([*self.TRIO, "S1.mzML"]), self.names(manifest))
        self.assertEqual([("A/S1.mzML", "undecodable")], self.choice(manifest, "S1.mzML"))
        # Unused, not excluded: the sample ran on its next encoding.
        self.assertNotIn("excluded_input_candidates", manifest)
        assert_every_member_on_record(self, manifest, ["A/S1.mzML", "S1.mzML"])

    def test_clause_3_an_undecodable_mzml_gives_way_to_its_mzxml_which_is_converted(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["w_S2.mzXML", "w_S2.mzML"], contents={"w_S2.mzXML": dda_32(), "w_S2.mzML": _numpress_mzml()}, campaign=True,
        )

        self.assertIn("w_S2.mzML", [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual([("w_S2.mzML", "undecodable")], self.choice(manifest, "w_S2.mzXML"))
        record = manifest["unattributed_members"]
        self.assertEqual((1, ["w_S2.mzXML"], ["w_S2.mzXML"]), (record["count"], record["members"], record["converted"]))
        row = self.rows(manifest)["w_S2.mzXML"]
        self.assertEqual(("converted", "w_S2.mzXML"), (row["kind"], row["name_pairing"]["member_name"]))
        self.assertNotIn("excluded_input_candidates", manifest)
        assert_every_member_on_record(self, manifest, ["w_S2.mzXML", "w_S2.mzML"])

    def test_clause_3_a_failed_conversion_gives_way_to_the_next_mzxml(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["a/S4.mzXML", "b/S4.mzXML"], [("Sample4", "S4.mzXML")],
            contents={"a/S4.mzXML": b"<not an mzXML", "b/S4.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual([("a/S4.mzXML", "conversion_failed")], self.choice(manifest, "b/S4.mzXML"))
        records = manifest["input_conversions"]["records"]
        self.assertEqual([("a/S4.mzXML", "failed"), ("b/S4.mzXML", "converted")],
                         [(item["source"]["relative_path"], item["status"]) for item in records])
        # The failed one is unused, not excluded: its sample runs on the next.
        self.assertNotIn("excluded_input_candidates", manifest)
        self.assertEqual("Sample4", self.csv_row(manifest, "b/S4.mzXML")["sample_id"])
        assert_every_member_on_record(self, manifest, ["a/S4.mzXML", "b/S4.mzXML"])

    def test_clause_3_where_no_encoding_can_be_read_none_is_used_and_each_is_excluded_on_record(self) -> None:
        manifest = self.unit(
            ["S5.mzML", "S5.mzXML"], [("Sample5", "S5.mzML")],
            contents={"S5.mzML": _numpress_mzml(), "S5.mzXML": b"<not an mzXML"}, campaign=True,
        )

        self.assertEqual(sorted(self.TRIO), self.names(manifest))
        self.assertEqual([("S5.mzML", "undecodable"), ("S5.mzXML", "conversion_failed")], self.choice(manifest, None))
        self.assertEqual(
            [("S5.mzXML", "conversion_failed"), ("S5.mzML", "unsupported_mzml_encoding")],
            [(Path(item["path"]).name, item["reason"]) for item in manifest["excluded_input_candidates"]],
        )
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertNotIn("Sample5", built["samples_without_input"])
        assert_every_member_on_record(self, manifest, ["S5.mzML", "S5.mzXML"])

    # ---- clause 4: the file used is the sample's own input ------------------------------------------------------

    def test_clause_4_a_vendor_file_runs_as_the_sample_whose_row_names_an_mzml(self) -> None:
        # Decodable or not, the row's mzML gives way to the vendor file of its sample (clause 1), which is paired to
        # the row and in its Class, never an unattributed input.
        samples = [*self.TRIO_ROWS, ("Sample1", "S1.mzML")]
        for undecodable in (True, False):
            with self.subTest(undecodable=undecodable), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit(
                    ["S1.mzML", "S1.raw"], [("Sample1", "S1.mzML")],
                    contents={"S1.mzML": _numpress_mzml() if undecodable else mzml(dda_spectra(6))},
                )

                self.assertEqual(sorted([*self.TRIO, "S1.raw"]), self.names(manifest))
                self.assertEqual([("S1.mzML", "lower_in_encoding_order")], self.choice(manifest, "S1.raw"))
                record = manifest["unattributed_members"]
                self.assertEqual((0, [], []), (record["count"], record["members"], record["paths"]))
                self.assertNotIn("unattributed_members_included", manifest.get("warnings") or [])
                row = self.rows(manifest)["S1.raw"]
                self.assertEqual("Sample1", row["sample_id"])
                self.assertNotIn("name_pairing", row)
                self.assertEqual(
                    str((Path(manifest["input_directory"]) / "S1.mzML").resolve()).casefold(),
                    str(Path(row["encoding_choice"]["stands_for"]).resolve()).casefold(),
                )
                self.assertNotIn("excluded_input_candidates", manifest)
                csv_row = self.csv_row(_with_classes(manifest, samples, "Sample1"), "S1.raw")
                self.assertEqual(
                    ("Sample1", "S1.mzML", 3, "Treated", "exact"),
                    (csv_row["sample_id"], csv_row["sample_raw_file"], csv_row["sample_row_index"], csv_row["class_id"],
                     csv_row["raw_file_paired_by"]),
                )
                assert_every_member_on_record(self, manifest, ["S1.mzML", "S1.raw"])

    def test_clause_4_a_converted_mzxml_runs_as_the_sample_whose_row_names_an_undecodable_mzml(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S6.mzML", "S6.mzXML"], [("Sample6", "S6.mzML")],
            contents={"S6.mzML": _numpress_mzml(), "S6.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual([("S6.mzML", "undecodable")], self.choice(manifest, "S6.mzXML"))
        row = self.rows(manifest)["S6.mzXML"]
        self.assertEqual(("converted", "Sample6"), (row["kind"], row["sample_id"]))
        self.assertEqual("S6.mzML", Path(row["encoding_choice"]["stands_for"]).name)
        self.assertNotIn("name_pairing", row)
        self.assertEqual(0, manifest["unattributed_members"]["count"])
        self.assertEqual(("Sample6", 3), (self.csv_row(manifest, "S6.mzXML")["sample_id"],
                                          self.csv_row(manifest, "S6.mzXML")["sample_row_index"]))
        assert_every_member_on_record(self, manifest, ["S6.mzML", "S6.mzXML"])

    def test_clause_4_a_twin_of_a_prefixed_member_runs_as_that_members_sample(self) -> None:
        manifest = self.unit([f"{PREFIX}S7.mzML", f"{PREFIX}S7.raw"], [("Sample7", "S7.mzML")])

        self.assertEqual([(f"{PREFIX}S7.mzML", "lower_in_encoding_order")], self.choice(manifest, f"{PREFIX}S7.raw"))
        row = self.rows(manifest)[f"{PREFIX}S7.raw"]
        # The prefix pairs the mzML only (its name ends in _S7.mzML); the .raw is its sample's by its stem. The record
        # names the pairing as it was made, by the mzML, and says the rule used the .raw for its sample (review of
        # PR #69, follow-up 3): the .raw is not recorded as paired by a prefix.
        self.assertEqual(
            ("Sample7", {"declared_raw_file": "S7.mzML", "member_name": f"{PREFIX}S7.mzML",
                         "paired_by": PREFIXED_MEMBER_PAIRING}),
            (row["sample_id"], row["name_pairing"]),
        )
        self.assertEqual(
            [(f"{PREFIX}S7.mzML", f"{PREFIX}S7.raw")],
            [(item["member_name"], item["encoding_used"]) for item in manifest["input_name_pairings"]["paired"]
             if item["declared_raw_file"] == "S7.mzML"],
        )
        self.assertEqual(("Sample7", 3), (self.csv_row(manifest, f"{PREFIX}S7.raw")["sample_id"],
                                          self.csv_row(manifest, f"{PREFIX}S7.raw")["sample_row_index"]))
        assert_every_member_on_record(self, manifest, [f"{PREFIX}S7.mzML", f"{PREFIX}S7.raw"])

    def test_clause_4_a_sample_paired_by_its_leading_identifier_runs_on_its_vendor_file(self) -> None:
        # Both encodings share the row's leading identifier; one sample's files are one candidate, so neither is
        # refused, and the rule takes the .raw for the row that names the mzML.
        mzml_name, raw_name = "VV_13_HEpG2_C1_exp344_pos.mzML", "VV_13_HEpG2_C1_exp344_pos.raw"
        manifest = self.unit([mzml_name, raw_name], [("VV_13_HEpG2_C1", "VV_13_HEpG2_C1_pos.mzML")],
                             contents={mzml_name: _numpress_mzml()})

        self.assertEqual([(mzml_name, "lower_in_encoding_order")], self.choice(manifest, raw_name))
        row = self.rows(manifest)[raw_name]
        self.assertEqual(("VV_13_HEpG2_C1", "VV_13_HEpG2_C1_pos.mzML", LEADING_IDENTIFIER_TOKEN_PAIRING),
                         (row["sample_id"], row["name_pairing"]["declared_raw_file"], row["name_pairing"]["paired_by"]))
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertNotIn("refused_name_pairings", attribute)
        self.assertEqual("VV_13_HEpG2_C1", self.csv_row(manifest, raw_name)["sample_id"])
        assert_every_member_on_record(self, manifest, [mzml_name, raw_name])

    def test_clause_4_copies_of_a_prefixed_name_in_two_folders_are_one_sample(self) -> None:
        manifest = self.unit([f"b/{PREFIX}S8.raw", f"a/{PREFIX}S8.raw"], [("Sample8", "S8.raw")])

        self.assertEqual([(f"b/{PREFIX}S8.raw", "tie_lexicographic")], self.choice(manifest, f"a/{PREFIX}S8.raw"))
        self.assertEqual("Sample8", self.csv_row(manifest, f"a/{PREFIX}S8.raw")["sample_id"])
        self.assertEqual([], manifest["input_name_pairings"]["refused"])

    # ---- clause 5: every file not used is on record -------------------------------------------------------------

    def test_clause_5_the_record_names_the_file_used_and_every_other_with_its_reason(self) -> None:
        from test_mzxml_conversion import dda_32

        members = ["S9.mzXML", "b/S9.raw", "a/S9.raw", "S9.mzML", "c/S9.mzXML"]
        manifest = self.unit(
            members, [("Sample9", "S9.mzXML")], contents={"S9.mzXML": dda_32(), "c/S9.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual(
            [{"rule": RULE, "used": "a/S9.raw",
              "unused": [{"path": "b/S9.raw", "reason": "tie_lexicographic"},
                         {"path": "S9.mzML", "reason": "lower_in_encoding_order"},
                         {"path": "c/S9.mzXML", "reason": "lower_in_encoding_order"},
                         {"path": "S9.mzXML", "reason": "lower_in_encoding_order"}]}],
            manifest["encoding_choices"],
        )
        row = self.rows(manifest)["a/S9.raw"]
        self.assertEqual({**manifest["encoding_choices"][0], "stands_for": row["encoding_choice"]["stands_for"]},
                         row["encoding_choice"])
        self.assertEqual("S9.mzXML", Path(row["encoding_choice"]["stands_for"]).name)
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual((1, 4), (attribute["encoding_choices"], attribute["unused_encodings"]))
        assert_every_member_on_record(self, manifest, members)

    def test_a_lease_with_one_file_per_sample_records_no_choice(self) -> None:
        manifest = self.unit(["S1.raw"], [("Sample1", "S1.raw")])

        self.assertNotIn("encoding_choices", manifest)
        self.assertNotIn("encoding_choice", self.rows(manifest)["S1.raw"])

    # ---- the cases around the rule ----------------------------------------------------------------------------

    def test_a_shared_archives_encoding_of_a_named_sample_is_that_samples_candidate(self) -> None:
        # Review of PR #69, follow-up 1 (probe K2): a shared archive's S1.raw is no unpaired member of it - the CSV
        # pairs it with the row naming S1.mzML by its stem - so the rule reaches it "across folders and archives":
        # decodable or not, the row's mzML gives way to its sample's vendor file (clauses 1, 3 and 4). A member of
        # no admitted sample's stem is still an unpaired member of the shared archive, left out on record.
        samples = [*self.TRIO_ROWS, ("Sample1", "S1.mzML")]
        for undecodable in (True, False):
            with self.subTest(undecodable=undecodable), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit(
                    ["S1.mzML", "S1.raw", "S9.raw"], [("Sample1", "S1.mzML")], scope=SHARED,
                    contents={"S1.mzML": _numpress_mzml() if undecodable else mzml(dda_spectra(6))},
                )

                self.assertEqual(sorted([*self.TRIO, "S1.raw"]), self.names(manifest))
                self.assertEqual([("S1.mzML", "lower_in_encoding_order")], self.choice(manifest, "S1.raw"))
                self.assertEqual([("S9.raw", "shared_archive")],
                                 [(item["path"], item["reason"]) for item in manifest["unattributed_members"]["left_out"]])
                self.assertNotIn("excluded_input_candidates", manifest)
                row = self.rows(manifest)["S1.raw"]
                self.assertEqual("Sample1", row["sample_id"])
                self.assertNotIn("name_pairing", row)
                self.assertEqual("S1.mzML", Path(row["encoding_choice"]["stands_for"]).name)
                csv_row = self.csv_row(_with_classes(manifest, samples, "Sample1"), "S1.raw")
                self.assertEqual(("Sample1", "S1.mzML", "Treated"),
                                 (csv_row["sample_id"], csv_row["sample_raw_file"], csv_row["class_id"]))
                assert_every_member_on_record(self, manifest, ["S1.mzML", "S1.raw", "S9.raw"])

    def test_a_shared_archives_mzml_is_used_before_the_mzxml_its_row_names(self) -> None:
        # Probe L: rows name S1.mzXML, and the shared archive holds a readable S1.mzML of it. Clause 1 ranks the mzML
        # first, and no mzXML is converted.
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S1.mzML", "S1.mzXML"], [("Sample1", "S1.mzXML")], scope=SHARED,
            contents={"S1.mzML": mzml(dda_spectra(6)), "S1.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual(sorted([*self.TRIO, "S1.mzML"]), self.names(manifest))
        self.assertEqual([("S1.mzXML", "lower_in_encoding_order")], self.choice(manifest, "S1.mzML"))
        self.assertEqual([], manifest["input_conversions"]["records"])
        self.assertNotIn("unattributed_members", manifest)
        self.assertEqual("Sample1", self.csv_row(manifest, "S1.mzML")["sample_id"])
        assert_every_member_on_record(self, manifest, ["S1.mzML", "S1.mzXML"])

    def test_a_shared_archives_mzxml_of_a_named_sample_is_converted_where_nothing_above_it_reads(self) -> None:
        # The row's mzML cannot be decoded and the shared archive's mzXML of it is the next in order (clause 3): the
        # convert stage starts for it, though the unit has no mzXML of its own.
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S1.mzML", "S1.mzXML"], [("Sample1", "S1.mzML")], scope=SHARED,
            contents={"S1.mzML": _numpress_mzml(), "S1.mzXML": dda_32()}, campaign=True,
        )

        self.assertEqual([("S1.mzML", "undecodable")], self.choice(manifest, "S1.mzXML"))
        self.assertEqual(["converted"], [item["status"] for item in manifest["input_conversions"]["records"]])
        row = self.rows(manifest)["S1.mzXML"]
        self.assertEqual(("converted", "Sample1"), (row["kind"], row["sample_id"]))
        self.assertEqual("Sample1", self.csv_row(manifest, "S1.mzXML")["sample_id"])
        assert_every_member_on_record(self, manifest, ["S1.mzML", "S1.mzXML"])

    def test_a_shared_archives_mzxml_beside_a_readable_named_file_starts_no_convert_stage(self) -> None:
        from test_mzxml_conversion import dda_32

        manifest = self.unit(
            ["S1.raw", "S1.mzXML"], [("Sample1", "S1.raw")], scope=SHARED, contents={"S1.mzXML": dda_32()},
            campaign=True,
        )

        self.assertEqual([("S1.mzXML", "lower_in_encoding_order")], self.choice(manifest, "S1.raw"))
        convert = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "convert")
        self.assertEqual("not_used", convert["status"])
        self.assertNotIn("input_conversions", manifest)

    def test_a_shared_archives_encoding_of_a_stem_two_rows_share_is_left_out_on_record(self) -> None:
        manifest = self.unit(
            ["S1.raw", "S1.mzML", "x/S1.d/analysis.baf"], [("SampleA", "S1.raw"), ("SampleB", "S1.mzML")],
            scope=SHARED, contents={"S1.mzML": mzml(dda_spectra(6))},
        )

        self.assertEqual(sorted([*self.TRIO, "S1.mzML", "S1.raw"]), self.names(manifest))
        self.assertEqual([("x/S1.d", "stem_of_several_sample_rows")],
                         [(item["path"], item["reason"]) for item in manifest["unattributed_members"]["left_out"]])
        self.assertEqual([], build_repository_analysis_rows(manifest)["failures"])

    # ---- a re-encoding MS-DIAL opens is no vendor format -----------------------------------------------------------

    def test_a_re_encoding_never_takes_a_sample_from_its_vendor_file(self) -> None:
        # Review of PR #69, follow-up 1: [S1.raw, S1.cdf] ran S1.cdf and recorded S1.raw as a same-rank tie.
        for vendor, re_encoding in (("S1.raw", "S1.cdf"), ("S1.raw", "S1.abf"), ("S1.d/analysis.baf", "S1.cdf")):
            with self.subTest(vendor=vendor, re_encoding=re_encoding), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                used = vendor.split("/")[0]
                manifest = self.unit([vendor, re_encoding], [("Sample1", used)])

                self.assertEqual(sorted([*self.TRIO, used]), self.names(manifest))
                self.assertEqual([(re_encoding, "lower_in_encoding_order")], self.choice(manifest, used))
                self.assertEqual("Sample1", self.csv_row(manifest, used)["sample_id"])

    # ---- one polarity folder beside an untokened copy is one sample ------------------------------------------------

    def test_a_copy_in_the_units_own_polarity_folder_is_one_sample_with_an_untokened_copy(self) -> None:
        # Review of PR #69, follow-up 1 (probes I and I2): POS/S1.raw and S1.raw in a positive unit are one sample's
        # copies; before, both ran (the CSV refused the row) or the vendor file ran unattributed beside POS/S1.mzML.
        manifest = self.unit(["POS/S1.raw", "S1.raw"], [("Sample1", "S1.raw")], ion_mode="Positive")
        self.assertEqual(sorted([*self.TRIO, "POS/S1.raw"]), self.names(manifest))
        self.assertEqual([("S1.raw", "tie_lexicographic")], self.choice(manifest, "POS/S1.raw"))
        self.assertEqual("Sample1", self.csv_row(manifest, "POS/S1.raw")["sample_id"])

        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary).resolve()
            manifest = self.unit(["POS/S1.mzML", "S1.raw"], [("Sample1", "S1.mzML")], ion_mode="Positive",
                                 contents={"POS/S1.mzML": mzml(dda_spectra(6))})
            self.assertEqual(sorted([*self.TRIO, "S1.raw"]), self.names(manifest))
            self.assertEqual([("POS/S1.mzML", "lower_in_encoding_order")], self.choice(manifest, "S1.raw"))
            self.assertEqual(0, manifest["unattributed_members"]["count"])
            self.assertEqual("Sample1", self.csv_row(manifest, "S1.raw")["sample_id"])
            assert_every_member_on_record(self, manifest, ["POS/S1.mzML", "S1.raw"])

    def test_a_copy_in_the_other_polarity_folder_stays_apart(self) -> None:
        # NEG/S1.raw in a positive unit is no copy of S1.raw: the rule never merges them, and what the lease does with
        # a file the row names exactly is what it always did (both stay inputs, for the preflight's polarity split);
        # an unpaired NEG/ member is left out by its token. With no unit polarity, POS/ and NEG/ copies beside an
        # untokened one are three files the rule never merges.
        manifest = self.unit(["NEG/S1.raw", "S1.raw", "NEG/S3.raw", "S3.raw"], [("Sample1", "S1.raw")],
                             ion_mode="Positive")
        self.assertEqual(sorted([*self.TRIO, "NEG/S1.raw", "S1.raw", "S3.raw"]), self.names(manifest))
        self.assertNotIn("encoding_choices", manifest)
        self.assertEqual([("NEG/S3.raw", "polarity_token_contradicts_ion_mode")],
                         [(item["path"], item["reason"]) for item in manifest["unattributed_members"]["left_out"]])

        with tempfile.TemporaryDirectory() as temporary:
            self.root = Path(temporary).resolve()
            manifest = self.unit(["NEG/S2.raw", "POS/S2.raw", "S2.raw"], ion_mode="")
            self.assertEqual(sorted([*self.TRIO, "NEG/S2.raw", "POS/S2.raw", "S2.raw"]), self.names(manifest))
            self.assertNotIn("encoding_choices", manifest)

    # ---- the lease reads a vendor header where another encoding could stand in (clause 3) -------------------------

    def test_a_vendor_file_whose_header_cannot_be_read_gives_way_to_the_next_encoding(self) -> None:
        # Review of PR #69, follow-up 1: a vendor file was taken as readable, and a preflight that could not read it
        # excluded it while encoding_choices still named it used and the sample's mzML lay unused.
        for reason in ("raw_header_unreadable", "raw_header_unsupported_format"):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                calls: list[list[str]] = []
                manifest = self.unit(
                    ["S1.lcd", "S1.mzML", "S2.raw"], [("Sample1", "S1.lcd"), ("Sample2", "S2.raw")],
                    contents={"S1.mzML": mzml(dda_spectra(6))}, header_reader=self.headers({"S1.lcd": reason}, calls),
                )

                # Only a vendor file the rule reached for a sample with another candidate is read.
                self.assertEqual([["S1.lcd"]], calls)
                self.assertEqual(sorted([*self.TRIO, "S1.mzML", "S2.raw"]), self.names(manifest))
                self.assertEqual([("S1.lcd", reason)], self.choice(manifest, "S1.mzML"))
                attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
                self.assertEqual((1, 1), (attribute["vendor_headers_read"], attribute["vendor_headers_unreadable"]))
                row = self.rows(manifest)["S1.mzML"]
                self.assertEqual(("Sample1", "S1.lcd"), (row["sample_id"], Path(row["encoding_choice"]["stands_for"]).name))
                self.assertEqual("Sample1", self.csv_row(manifest, "S1.mzML")["sample_id"])
                self.assertNotIn("excluded_input_candidates", manifest)
                assert_every_member_on_record(self, manifest, ["S1.lcd", "S1.mzML", "S2.raw"])

    def test_a_vendor_header_that_reads_keeps_the_vendor_file(self) -> None:
        calls: list[list[str]] = []
        manifest = self.unit(["S1.raw", "S1.mzML"], [("Sample1", "S1.raw")], contents={"S1.mzML": mzml(dda_spectra(6))},
                             header_reader=self.headers({}, calls))

        self.assertEqual([["S1.raw"]], calls)
        self.assertEqual([("S1.mzML", "lower_in_encoding_order")], self.choice(manifest, "S1.raw"))
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual((1, 0), (attribute["vendor_headers_read"], attribute["vendor_headers_unreadable"]))

    def test_where_only_a_header_stood_in_the_way_the_vendor_file_is_still_used(self) -> None:
        # Nothing else of the sample can be read: the lease's header read is no exclusion, and the preflight decides
        # the file, as it does a sample's only file (a unit none of whose headers read is taken at its declaration).
        calls: list[list[str]] = []
        manifest = self.unit(
            ["S1.lcd", "S1.mzML"], [("Sample1", "S1.lcd")], contents={"S1.mzML": _numpress_mzml()},
            header_reader=self.headers({"S1.lcd": "raw_header_unreadable"}, calls),
        )

        self.assertEqual(sorted([*self.TRIO, "S1.lcd"]), self.names(manifest))
        # Review of PR #69, follow-up 2 (probe P4): the mzML after it was set aside because it cannot be decoded, and
        # the record says so (clause 5), not that it was lower in the order.
        self.assertEqual([("S1.mzML", "undecodable")], self.choice(manifest, "S1.lcd"))
        self.assertNotIn("excluded_input_candidates", manifest)
        assert_every_member_on_record(self, manifest, ["S1.lcd", "S1.mzML"])

    def test_where_only_a_header_stood_in_the_way_the_preflight_names_no_fallback_not_taken(self) -> None:
        # Probe P4 downstream: the preflight cannot read S1.lcd's header either. Its next encoding is undecodable, so
        # no fallback was missed, and encoding_fallback_not_taken (which tells the operator to lease again with an
        # extractor that was configured) is not raised. Had the record said lower_in_encoding_order, it would be.
        from msdial_app.raw_metadata_preflight import decide_disposition
        from test_raw_metadata_preflight import _header, _manifest

        manifest = self.unit(
            ["S1.lcd", "S1.mzML"], [("Sample1", "S1.lcd")], contents={"S1.mzML": _numpress_mzml()},
            header_reader=self.headers({"S1.lcd": "raw_header_unreadable"}, []),
        )
        lcd = self.rows(manifest)["S1.lcd"]["path"]
        readable = self.rows(manifest)[self.TRIO[0]]["path"]

        def disposition(lineage: dict) -> dict:
            preflight = _manifest([_header(readable, "DDA", polarity="Positive")], failures={lcd: "failed"})
            preflight["input_lineage"] = lineage
            return decide_disposition(preflight)

        decided = disposition(manifest["input_lineage"])
        self.assertIn("raw_header_unreadable", [item["reason"] for item in decided["excluded_inputs"]])
        self.assertNotIn("encoding_fallback_not_taken", decided["warnings"])
        misstated = {
            **manifest["input_lineage"],
            "rows": [
                {**row, "encoding_choice": {**row["encoding_choice"], "unused": [
                    {**item, "reason": "lower_in_encoding_order"} for item in row["encoding_choice"]["unused"]]}}
                if row.get("encoding_choice") else row
                for row in manifest["input_lineage"]["rows"]
            ],
        }
        self.assertIn("encoding_fallback_not_taken", disposition(misstated)["warnings"])

    def test_where_only_a_header_stood_in_the_way_an_mzxml_keeps_why_it_could_not_be_used(self) -> None:
        # The same for an mzXML: one whose conversion failed in a campaign, and one outside a campaign, which needs a
        # conversion nothing runs. Neither is lower in the order: neither could have been used.
        for campaign, reason in ((True, "conversion_failed"), (False, "requires_conversion")):
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit(
                    ["S1.lcd", "S1.mzXML"], [("Sample1", "S1.lcd")], contents={"S1.mzXML": b"<not an mzXML"},
                    campaign=campaign, header_reader=self.headers({"S1.lcd": "raw_header_unreadable"}, []),
                )

                self.assertEqual(sorted([*self.TRIO, "S1.lcd"]), self.names(manifest))
                self.assertEqual([("S1.mzXML", reason)], self.choice(manifest, "S1.lcd"))
                self.assertNotIn("excluded_input_candidates", manifest)
                assert_every_member_on_record(self, manifest, ["S1.lcd", "S1.mzXML"])

    # ---- one row's files whatever their stems (THE SAME SAMPLE) ------------------------------------------------

    def test_a_file_of_the_rows_stem_and_a_prefixed_member_of_the_row_are_one_sample(self) -> None:
        # Review of PR #69, follow-up 2 (probes P3c, P3c'): the prefix pairs 021518_387057_CSHp_S7.mzML with the row
        # naming S7.mzML, and S7.raw is of the row's own stem. Both are Sample7's, unit-scoped archive or shared: the
        # vendor file is its one input (clauses 1 and 4), and the prefixed mzML is unused, on record (clause 5).
        # Before, the prefixed mzML ran as Sample7 and S7.raw ran again as an unattributed sample, or was left out
        # as a shared archive's member.
        samples = [*self.TRIO_ROWS, ("Sample7", "S7.mzML")]
        for scope in (UNIT_SCOPED, SHARED):
            with self.subTest(scope=scope["kind"]), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit([f"{PREFIX}S7.mzML", "S7.raw"], [("Sample7", "S7.mzML")], scope=scope,
                                     contents={f"{PREFIX}S7.mzML": mzml(dda_spectra(6))})

                self.assertEqual(sorted([*self.TRIO, "S7.raw"]), self.names(manifest))
                self.assertEqual([(f"{PREFIX}S7.mzML", "lower_in_encoding_order")], self.choice(manifest, "S7.raw"))
                record = manifest.get("unattributed_members") or {}
                self.assertEqual([], record.get("members") or [])
                self.assertEqual([], record.get("left_out") or [])
                row = self.rows(manifest)["S7.raw"]
                self.assertEqual("Sample7", row["sample_id"])
                self.assertEqual(f"{PREFIX}S7.mzML", Path(row["encoding_choice"]["stands_for"]).name)
                csv_row = self.csv_row(_with_classes(manifest, samples, "Sample7"), "S7.raw")
                self.assertEqual(("Sample7", "S7.mzML", "Treated"),
                                 (csv_row["sample_id"], csv_row["sample_raw_file"], csv_row["class_id"]))
                assert_every_member_on_record(self, manifest, [f"{PREFIX}S7.mzML", "S7.raw"])

    def test_a_prefixed_vendor_file_of_the_row_and_an_mzml_of_its_stem_are_one_sample(self) -> None:
        # Probes P3 and N4: the row names S7.raw (P3) or S7 with no extension (N4); the prefixed .raw is the row's by
        # its prefix, and S7.mzML by the row's stem. The vendor file runs as Sample7, and the mzML is unused; it no
        # longer runs again as an unattributed sample. For N4 the mzML carries the row's name exactly, which before
        # kept the prefixed .raw from being paired at all: an exact name keeps a prefix from pairing only a file of
        # its own encoding rank, and a file of another rank is the row's sample's other encoding.
        for raw_file in ("S7.raw", "S7"):
            with self.subTest(raw_file=raw_file), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                samples = [*self.TRIO_ROWS, ("Sample7", raw_file)]
                manifest = self.unit([f"{PREFIX}S7.raw", "S7.mzML"], [("Sample7", raw_file)],
                                     contents={"S7.mzML": mzml(dda_spectra(6))})

                self.assertEqual(sorted([*self.TRIO, f"{PREFIX}S7.raw"]), self.names(manifest))
                self.assertEqual([("S7.mzML", "lower_in_encoding_order")], self.choice(manifest, f"{PREFIX}S7.raw"))
                self.assertEqual([], (manifest.get("unattributed_members") or {}).get("members") or [])
                row = self.rows(manifest)[f"{PREFIX}S7.raw"]
                self.assertEqual(
                    ("Sample7", PREFIXED_MEMBER_PAIRING, raw_file),
                    (row["sample_id"], row["name_pairing"]["paired_by"], row["name_pairing"]["declared_raw_file"]),
                )
                csv_row = self.csv_row(_with_classes(manifest, samples, "Sample7"), f"{PREFIX}S7.raw")
                self.assertEqual(("Sample7", raw_file, "Treated"),
                                 (csv_row["sample_id"], csv_row["sample_raw_file"], csv_row["class_id"]))
                assert_every_member_on_record(self, manifest, [f"{PREFIX}S7.raw", "S7.mzML"])

    def test_an_exact_name_still_keeps_a_prefix_from_pairing_a_file_of_its_own_encoding(self) -> None:
        # What the exact-first rule protected is kept: a prefixed member of the encoding rank a member carries the
        # row's name in is a different file (the prefixed .raw beside S7.raw, the row naming S7 or S7.raw), never
        # paired. One of another rank is.
        for raw_file in ("S7.raw", "S7"):
            with self.subTest(raw_file=raw_file):
                self.assertEqual({}, self.pairing([f"{PREFIX}S7.raw", "S7.raw"], [("Sample7", raw_file)]))
        self.assertEqual({}, self.pairing([f"{PREFIX}S7.mzML", "S7.mzML"], [("Sample7", "S7")]))
        self.assertEqual({f"{PREFIX}S7.raw": "S7"}, self.pairing([f"{PREFIX}S7.raw", "S7.mzML"], [("Sample7", "S7")]))

    # ---- a leading identifier two stems carry pairs nothing (review of PR #69, follow-up 3) ---------------------
    #
    # The one encoding rule does not say whether a file that shares only a row's leading identifier is that row's
    # sample, so the token rule keeps its own words (2026-10-06): a key is paired only where it matches uniquely on
    # both sides, the row's own files counting. Where the row's own file is there (by its exact name, or of its stem
    # in another encoding) and a file of another stem carries the key, nothing is paired by it: the row's own file
    # is its input, and the other file is an unattributed input of a unit-scoped archive, or left out on record from
    # a shared one, as it was before the rule.

    V13 = "VV_13_HEpG2_C1_pos"

    def assert_unpaired_beside_the_rows_own_file(
        self, manifest: dict, own: str, other: str, scope: dict, *, own_paired: bool = False
    ) -> None:
        """``own`` runs as V13, by its exact name, or (``own_paired``) by the key, as the row's own-stem file alone
        is; ``other`` is no encoding of V13 and is paired with nothing: an unattributed input, or left out."""
        self.assertIn(own, self.names(manifest))
        self.assertEqual("V13", self.rows(manifest)[own]["sample_id"])
        self.assertNotIn(other, [item["used"] for item in manifest.get("encoding_choices") or []])
        self.assertNotIn(other.casefold(), [item["path"].casefold() for choice in manifest.get("encoding_choices") or []
                                            for item in choice["unused"]])
        listed = [(item["member_name"], item["paired_by"])
                  for item in (manifest.get("input_name_pairings") or {}).get("paired") or []
                  if item["declared_raw_file"].startswith("VV_13")]
        if own_paired:
            self.assertEqual({"declared_raw_file": f"{self.V13}.raw", "member_name": own,
                              "paired_by": LEADING_IDENTIFIER_TOKEN_PAIRING, "key": "vv_13"},
                             self.rows(manifest)[own]["name_pairing"])
            self.assertEqual([(own, LEADING_IDENTIFIER_TOKEN_PAIRING)], listed)
        else:
            self.assertNotIn("name_pairing", self.rows(manifest)[own])
            self.assertEqual([], listed)
        record = manifest["unattributed_members"]
        if scope is UNIT_SCOPED:
            self.assertIn(other, self.names(manifest))
            self.assertIn(other, record["paths"])
            self.assertEqual(UNATTRIBUTED_MEMBER_PAIRING, self.rows(manifest)[other]["name_pairing"]["paired_by"])
        else:
            self.assertNotIn(other, self.names(manifest))
            self.assertIn(other, [item["path"] for item in record["left_out"]])
        assert_every_member_on_record(self, manifest, [own, other])

    def test_a_token_another_stem_carries_beside_the_rows_exact_file_pairs_nothing(self) -> None:
        # Probe V13: the row names VV_13_HEpG2_C1_pos.raw, which is there; VV_13_HEpG2_C1_rep2_pos carries the key
        # too, in an mzML or a .raw. It is another injection, not an encoding of V13: it runs unattributed (or is left
        # out of a shared archive), whatever its encoding, and the row's .raw runs as V13.
        own = f"{self.V13}.raw"
        for other in ("VV_13_HEpG2_C1_rep2_pos.mzML", "VV_13_HEpG2_C1_rep2_pos.raw"):
            for scope in (UNIT_SCOPED, SHARED):
                with self.subTest(other=other, scope=scope["kind"]), tempfile.TemporaryDirectory() as temporary:
                    self.root = Path(temporary).resolve()
                    manifest = self.unit([own, other], [("V13", own)], scope=scope,
                                         contents={other: mzml(dda_spectra(6))})

                    self.assert_unpaired_beside_the_rows_own_file(manifest, own, other, scope)
                    self.assertNotIn(other, [item["member_name"] for item in
                                             (manifest.get("input_name_pairings") or {}).get("refused") or []])

    def test_a_token_another_stem_carries_never_sets_aside_the_mzml_the_row_names(self) -> None:
        # Probe L1: the row names VV_13_HEpG2_C1_pos.mzML, which is there, beside VV_13_HEpG2_C1_exp344_pos.raw. The
        # mzML the row names runs as V13; the .raw is not paired by the key and does not displace it.
        own, other = f"{self.V13}.mzML", "VV_13_HEpG2_C1_exp344_pos.raw"
        for scope in (UNIT_SCOPED, SHARED):
            with self.subTest(scope=scope["kind"]), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit([own, other], [("V13", own)], scope=scope, contents={own: mzml(dda_spectra(6))})

                self.assert_unpaired_beside_the_rows_own_file(manifest, own, other, scope)

    def test_a_token_another_stem_carries_never_sets_aside_the_rows_own_stem(self) -> None:
        # Probe L3: the row names VV_13_HEpG2_C1_pos.raw, which is not there; VV_13_HEpG2_C1_pos.mzML is of its stem
        # (V13's by clause 4), and VV_13_HEpG2_C1_exp344_pos.raw shares only the key. Two stems carry it, so it is not
        # unique: the exp344 .raw is refused the pairing, on record. The row's own-stem mzML is paired by the key as
        # it is when it is alone (and as before the rule), and runs as V13.
        own, other = f"{self.V13}.mzML", "VV_13_HEpG2_C1_exp344_pos.raw"
        for scope in (UNIT_SCOPED, SHARED):
            with self.subTest(scope=scope["kind"]), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit([own, other], [("V13", f"{self.V13}.raw")], scope=scope,
                                     contents={own: mzml(dda_spectra(6))})

                self.assert_unpaired_beside_the_rows_own_file(manifest, own, other, scope, own_paired=True)
                self.assertEqual(
                    [(other, f"{self.V13}.raw", LEADING_IDENTIFIER_TOKEN_PAIRING, "leading_identifier_not_unique")],
                    [(item["member_name"], item["declared_raw_file"], item["rule"], item["reason"])
                     for item in manifest["input_name_pairings"]["refused"] if item["member_name"] == other],
                )

    def test_a_token_only_one_stem_carries_still_pairs_one_samples_files(self) -> None:
        # What the token rule pairs is kept: where the row's name is not there and one sample (of one stem) carries
        # the key, its files are paired, and the rule runs its vendor file.
        name = "VV_13_HEpG2_C1_exp344_pos"
        manifest = self.unit([f"{name}.raw", f"{name}.mzML"], [("V13", f"{self.V13}.raw")],
                             contents={f"{name}.mzML": mzml(dda_spectra(6))})

        self.assertEqual([(f"{name}.mzML", "lower_in_encoding_order")], self.choice(manifest, f"{name}.raw"))
        row = self.rows(manifest)[f"{name}.raw"]
        self.assertEqual(("V13", LEADING_IDENTIFIER_TOKEN_PAIRING, f"{name}.raw"),
                         (row["sample_id"], row["name_pairing"]["paired_by"], row["name_pairing"]["member_name"]))

    # ---- a file of the row's stem used for a sample a prefix paired is not itself paired (follow-up 3) ----------

    def assert_paired_through(self, manifest: dict, used: str, member: str, declared: str, encoding_used: str) -> None:
        """The input ``used`` is its row's sample's, which a prefix paired through ``member`` (unused): its lineage row
        names that pairing as it was made (member_name ``member``), and the manifest lists it once, by ``member``,
        saying which file the rule used for its sample."""
        row = self.rows(manifest)[used]
        self.assertEqual("Sample7", row["sample_id"])
        self.assertEqual(member, Path(row["encoding_choice"]["stands_for"]).name)
        self.assertEqual(
            {"declared_raw_file": declared, "member_name": member, "paired_by": PREFIXED_MEMBER_PAIRING},
            row["name_pairing"],
        )
        self.assertEqual(
            [{"member_name": member, "declared_raw_file": declared, "paired_by": PREFIXED_MEMBER_PAIRING,
              "encoding_used": encoding_used}],
            [item for item in manifest["input_name_pairings"]["paired"] if item["declared_raw_file"] == declared],
        )
        self.assertIn(INFERRED_PAIRING_WARNING, manifest["warnings"])
        # The analysis-CSV record (review ia-final): the input that runs is listed with the pairing as it was made,
        # by ``member``, and with the file the rule used for its sample, never as an input a prefix paired itself.
        built = build_repository_analysis_rows(manifest)
        recorded = copy.deepcopy(manifest)
        analysis_csv_change(built, "analysis.csv")(recorded)
        csv_row = self.csv_row(manifest, used)
        self.assertEqual(
            [{"input": Path(csv_row["input_path"]).name, "sample_id": "Sample7", "sample_raw_file": declared,
              "declared_raw_file": declared, "member_name": member, "paired_by": PREFIXED_MEMBER_PAIRING,
              "encoding_used": encoding_used}],
            [item for item in recorded["analysis_csv"]["inferred_name_pairings"]
             if item["declared_raw_file"] == declared],
        )

    def test_a_file_of_the_rows_stem_is_not_recorded_as_paired_by_a_prefix(self) -> None:
        # Probe P3c: S7.raw runs as Sample7, the row naming S7.mzML. It carries no prefix; the member a prefix paired
        # is 021518_387057_CSHp_S7.mzML, which the rule left unused. The record says that pairing, not one of S7.raw.
        for scope in (UNIT_SCOPED, SHARED):
            with self.subTest(scope=scope["kind"]), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.unit([f"{PREFIX}S7.mzML", "S7.raw"], [("Sample7", "S7.mzML")], scope=scope,
                                     contents={f"{PREFIX}S7.mzML": mzml(dda_spectra(6))})

                self.assert_paired_through(manifest, "S7.raw", f"{PREFIX}S7.mzML", "S7.mzML", "S7.raw")
                attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
                # The three BioRec members and Sample7's: S7.raw is counted once, through the member paired.
                self.assertEqual(4, attribute["prefixed_member_pairings"])
                self.assertEqual(manifest["input_name_pairings"]["paired"], attribute["inferred_name_pairings"])

    def test_copies_of_the_rows_stem_in_folders_are_not_recorded_as_paired_by_a_prefix(self) -> None:
        manifest = self.unit([f"{PREFIX}S7.mzML", "A/S7.raw", "RAW/S7.raw"], [("Sample7", "S7.mzML")],
                             contents={f"{PREFIX}S7.mzML": mzml(dda_spectra(6))})

        self.assertEqual([("RAW/S7.raw", "tie_lexicographic"), (f"{PREFIX}S7.mzML", "lower_in_encoding_order")],
                         self.choice(manifest, "A/S7.raw"))
        self.assert_paired_through(manifest, "A/S7.raw", f"{PREFIX}S7.mzML", "S7.mzML", "A/S7.raw")

    def test_a_converted_file_of_the_rows_stem_keeps_its_sample_and_the_prefix_pairing(self) -> None:
        # Probe U1 (a campaign): the prefixed mzML cannot be decoded, so the rule converts S7.mzXML, of the row's
        # stem, and runs it as Sample7. Its lineage row keeps Sample7 and names the prefix pairing as it was made.
        from test_mzxml_conversion import dda_32

        manifest = self.unit([f"{PREFIX}S7.mzML", "S7.mzXML"], [("Sample7", "S7.mzML")], campaign=True,
                             contents={f"{PREFIX}S7.mzML": _numpress_mzml(), "S7.mzXML": dda_32()})

        self.assertEqual([(f"{PREFIX}S7.mzML", "undecodable")], self.choice(manifest, "S7.mzXML"))
        self.assertEqual("converted", self.rows(manifest)["S7.mzXML"]["kind"])
        self.assert_paired_through(manifest, "S7.mzXML", f"{PREFIX}S7.mzML", "S7.mzML", "S7.mzXML")
        self.assertEqual("Sample7", self.csv_row(manifest, "S7.mzXML")["sample_id"])
        assert_every_member_on_record(self, manifest, [f"{PREFIX}S7.mzML", "S7.mzXML"])

    def test_a_prefixed_name_in_the_units_polarity_folder_and_untokened_is_one_candidate(self) -> None:
        # The pairing reads one sample as the rule does: POS/<prefix>S8.raw and <prefix>S8.raw in a positive unit are
        # one candidate for the row's S8.raw, so neither is refused as not one to one, and the rule runs one.
        manifest = self.unit([f"POS/{PREFIX}S8.raw", f"{PREFIX}S8.raw"], [("Sample8", "S8.raw")], ion_mode="Positive")

        self.assertEqual([(f"POS/{PREFIX}S8.raw", "tie_lexicographic")], self.choice(manifest, f"{PREFIX}S8.raw"))
        self.assertEqual([], manifest["input_name_pairings"]["refused"])
        self.assertEqual("Sample8", self.csv_row(manifest, f"{PREFIX}S8.raw")["sample_id"])

    # ---- a declaration of one sample's file in two folders ---------------------------------------------------------

    def test_a_declared_unit_runs_one_of_two_declared_copies_of_a_sample(self) -> None:
        # Review of PR #69, follow-up 1 (probe A): the Catalog leaves equally preferred copies both declared, and both
        # ran, so the CSV refused the row (sample_row_with_two_inputs). They are one row's, of one stem: the rule runs
        # the first by path, and the other declared input is accounted for by the record, not missing.
        project, payloads = _st001264(["A/S1.raw", "B/S1.raw", "S2.raw"], [("Sample1", "S1.raw"), ("Sample2", "S2.raw")])
        project.download_scope = dict(UNIT_SCOPED)
        project.analysis_inputs = [
            {"path": "A/S1.raw", "kind": "file", "sample_id": "Sample1"},
            {"path": "B/S1.raw", "kind": "file", "sample_id": "Sample1"},
            {"path": "S2.raw", "kind": "file", "sample_id": "Sample2"},
        ]
        manifest = self.lease(project, payloads)

        self.assertEqual(["A/S1.raw", "S2.raw"], self.names(manifest))
        self.assertEqual([("B/S1.raw", "tie_lexicographic")], self.choice(manifest, "A/S1.raw"))
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual({"A/S1.raw": "Sample1", "S2.raw": "Sample2"},
                         {os.path.relpath(row["input_path"], manifest["input_directory"]).replace("\\", "/"): row["sample_id"]
                          for row in built["rows"]})
        self.assertNotIn("unattributed_members", manifest)
        assert_every_member_on_record(self, manifest, ["A/S1.raw", "B/S1.raw", "S2.raw"])

    def test_declared_copies_of_one_name_for_two_rows_are_two_samples(self) -> None:
        # MTBKS64's shape: S01 has two rows naming raw/batch1/QC.RAW and raw/batch2/QC.RAW; each declared input is its
        # own row's, so the rule chooses between nothing.
        project, payloads = _st001264(["batch1/QC.raw", "batch2/QC.raw"], [("S01", "batch1/QC.raw"), ("S01", "batch2/QC.raw")])
        project.download_scope = dict(UNIT_SCOPED)
        project.analysis_inputs = [
            {"path": "batch1/QC.raw", "kind": "file", "sample_id": "S01"},
            {"path": "batch2/QC.raw", "kind": "file", "sample_id": "S01"},
        ]
        manifest = self.lease(project, payloads)

        self.assertEqual(["batch1/QC.raw", "batch2/QC.raw"], self.names(manifest))
        self.assertNotIn("encoding_choices", manifest)

    def test_the_lease_header_reader_gives_the_preflights_reasons(self) -> None:
        from msdial_app import raw_metadata_preflight
        from msdial_app.repository_reanalysis import lease_header_reader

        files = {name: self.root / name for name in ("a.raw", "b.lcd", "c.wiff", "d.raw")}
        outcomes = {"a.raw": "ok", "b.lcd": "unsupported_format", "c.wiff": "failed", "d.raw": "reused"}
        asked: list[list[str]] = []

        def run_extractor(extractor, inputs, work_directory, **options):
            asked.append([Path(item).name for item in inputs])
            return {"outcomes": {_file_key(str(item)): {"outcome": outcomes[Path(item).name]} for item in inputs}}

        with patch.object(repository_reanalysis, "raw_metadata_extractor_identity", return_value={"binary_sha256": "ab"}), \
                patch.object(raw_metadata_preflight, "run_extractor", side_effect=run_extractor):
            read = lease_header_reader(self.root / "extractor.exe")(list(files.values()), self.root / "reads")

        self.assertEqual([["a.raw", "b.lcd", "c.wiff", "d.raw"]], asked)
        self.assertEqual(
            {"a.raw": "", "b.lcd": "raw_header_unsupported_format", "c.wiff": "raw_header_unreadable", "d.raw": ""},
            {name: read[_file_key(str(path))] for name, path in files.items()},
        )

    def test_the_server_gives_a_lease_the_extractor_a_preflight_would_run_or_none(self) -> None:
        from msdial_app import raw_metadata_extractor, server

        logs: list[str] = []
        select = "select_raw_metadata_extractor"
        with patch.object(raw_metadata_extractor, select, return_value={"path": ""}):
            self.assertIsNone(server._lease_header_reader(False, logs.append))
        refused = raw_metadata_extractor.RawMetadataExtractorRefused(["extractor_absent"], ["none named"], {})
        with patch.object(raw_metadata_extractor, select, side_effect=refused):
            self.assertIsNone(server._lease_header_reader(True, logs.append))
        with patch.object(raw_metadata_extractor, select, return_value={"path": "x.exe"}) as chosen,                 patch.object(repository_reanalysis, "lease_header_reader", return_value="a reader") as made:
            self.assertEqual("a reader", server._lease_header_reader(True, logs.append))
        self.assertTrue(chosen.call_args.kwargs["campaign"])
        self.assertEqual(Path("x.exe"), made.call_args.args[0])
        self.assertEqual(2, len(logs))

    def test_an_incomplete_vendor_folder_gives_way_to_the_next_encoding(self) -> None:
        from msdial_app.repository_reanalysis import _choose_encodings, _EncodingGroup

        data = self.root / "data"
        (data / "S1.d").mkdir(parents=True)
        (data / "S1.mzML").write_bytes(mzml(dda_spectra(6)))
        folder, mzml_path = str((data / "S1.d").resolve()), str((data / "S1.mzML").resolve())
        groups = [_EncodingGroup({folder: "S1.d", mzml_path: "S1.mzML"}, [folder])]

        choices, reads = _choose_encodings(groups, None, incomplete={_file_key(folder)})
        self.assertEqual((mzml_path, [(folder, "incomplete_container")]), (choices[0].used, list(choices[0].unused)))
        self.assertEqual({}, reads)
        choices, _reads = _choose_encodings(groups, None)
        self.assertEqual(folder, choices[0].used)

    def test_two_polarities_are_never_one_samples_encodings(self) -> None:
        # POS/S1.raw and NEG/S1.raw are two acquisitions, not two encodings of one; a unit of both polarities keeps
        # both (its split divides them), and a positive unit leaves the negative one out by its token, as before.
        def lease(ion_mode: str) -> dict:
            project, payloads = _st001264([*self.TRIO, "POS/S1.raw", "NEG/S1.raw"], list(self.TRIO_ROWS))
            project.download_scope, project.ion_mode = dict(UNIT_SCOPED), ion_mode
            return read_manifest(create_download_lease(project, self.root, 10_000_000, client=_Client(payloads))["manifest_path"])

        both = lease("")
        self.assertEqual(sorted([*self.TRIO, "NEG/S1.raw", "POS/S1.raw"]), self.names(both))
        self.assertNotIn("encoding_choices", both)

    def test_a_stem_two_rows_pair_with_keeps_each_rows_file_and_leaves_an_unpaired_one_out(self) -> None:
        # Rows naming S1.raw and S1.mzML are two samples: each keeps its own file. An unpaired member of that stem is
        # no one sample's encoding, and is left out on record (stem_of_several_sample_rows).
        manifest = self.unit(
            ["S1.raw", "S1.mzML", "x/S1.d/analysis.baf"], [("SampleA", "S1.raw"), ("SampleB", "S1.mzML")],
            contents={"S1.mzML": mzml(dda_spectra(6))},
        )

        self.assertEqual(sorted([*self.TRIO, "S1.mzML", "S1.raw"]), self.names(manifest))
        self.assertEqual([("x/S1.d", "stem_of_several_sample_rows")],
                         [(item["path"], item["reason"]) for item in manifest["unattributed_members"]["left_out"]])
        self.assertEqual(0, manifest["unattributed_members"]["count"])
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual({"S1.raw": "SampleA", "S1.mzML": "SampleB"},
                         {Path(row["input_path"]).name: row["sample_id"] for row in built["rows"] if row["sample_id"].startswith("Sample")})

    def test_a_row_without_an_extension_admits_every_encoding_and_the_rule_takes_one(self) -> None:
        # A row naming S7 admits S7.raw and S7.mzML by its stem; before, both were its inputs and the analysis CSV
        # refused the row (sample_row_with_two_inputs).
        manifest = self.unit(["S7.mzML", "S7.raw"], [("Sample7", "S7")], contents={"S7.mzML": mzml(dda_spectra(6))})

        self.assertEqual(sorted([*self.TRIO, "S7.raw"]), self.names(manifest))
        self.assertEqual([("S7.mzML", "lower_in_encoding_order")], self.choice(manifest, "S7.raw"))
        self.assertEqual("Sample7", self.csv_row(manifest, "S7.raw")["sample_id"])

    def test_a_declared_unit_runs_its_declared_file_and_keeps_an_undeclared_twin_out(self) -> None:
        # The declaration names the sample's file, and a member it does not name stays out on record (2026-10-08,
        # second round, answer 3): the rule has nothing to choose there. The undecodable declared mzML is excluded.
        project, payloads = _st001264(
            ["S1.mzML", "S1.raw", "S2.raw"], [("Sample1", "S1.mzML"), ("Sample2", "S2.raw")], {"S1.mzML": _numpress_mzml()}
        )
        project.download_scope = dict(UNIT_SCOPED)
        project.analysis_inputs = [
            {"path": "S1.mzML", "kind": "file", "sample_id": "Sample1"},
            {"path": "S2.raw", "kind": "file", "sample_id": "Sample2"},
        ]
        manifest = self.lease(project, payloads)

        self.assertEqual(["S2.raw"], [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual([("S1.mzML", "unsupported_mzml_encoding")],
                         [(Path(item["path"]).name, item["reason"]) for item in manifest["excluded_input_candidates"]])
        self.assertEqual(
            [{"member_name": "S1.raw", "path": "S1.raw", "reason": "not_named_by_the_catalog_declaration"}],
            manifest["unattributed_members"]["left_out"],
        )
        self.assertNotIn("encoding_choices", manifest)
        assert_every_member_on_record(self, manifest, ["S1.mzML", "S1.raw", "S2.raw"])


# ---- the handoff check and the CSV builder pair a declared input with a row alike --------------------------


def _mtbks64_in_folders() -> tuple[dict, dict[str, bytes]]:
    """MTBKS64 with S01's two rows naming one file name in two folders, as MTBLS3317's rows name
    'FILES/Method 1/X.mzML' beside 'FILES/X.mzML'."""
    handoff, _payloads = _mtbks64()
    moved = {"raw/MDLC1_17050.RAW": "raw/batch1/QC.RAW", "raw/MDLC1_17051.RAW": "raw/batch2/QC.RAW"}
    payloads = {}
    for item in handoff["files"]:
        item["path"] = moved.get(item["path"], item["path"])
        item["download_url"] = MTBKS64_BASE + item["path"]
        data = f"thermo raw bytes of {item['path']}".encode()
        payloads[item["download_url"]] = data
        item["size_bytes"], item["checksum"] = len(data), hashlib.md5(data).hexdigest()
    for item in handoff["analysis_inputs"]:
        item["path"] = moved.get(item["path"], item["path"])
    for item in handoff["sample_metadata"]:
        item["raw_file"] = moved.get(item["raw_file"], item["raw_file"])
    handoff["download_scope"]["bundle_bytes"] = sum(item["size_bytes"] for item in handoff["files"])
    return handoff, payloads


class TheHandoffAndTheCsvPairRowsAlike(unittest.TestCase):
    def test_rows_that_share_a_file_name_in_two_folders_are_paired_by_path_before_and_after_download(self) -> None:
        handoff, payloads = _mtbks64_in_folders()
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff)
        self.assertEqual("passed", project["repository_metadata"]["analysis_input_check"]["status"])

        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _lease(Path(temporary), handoff, payloads)
            _set_types(manifest_path, "DDA")
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(
            [("S01", "raw/batch1/QC.RAW", 0), ("S01", "raw/batch2/QC.RAW", 1)],
            sorted(
                (row["sample_id"], row["sample_raw_file"], row["sample_row_index"])
                for row in built["rows"]
                if row["sample_id"] == "S01"
            ),
        )
        for row in built["rows"]:
            self.assertEqual(Path(row["input_path"]).parent.name, Path(row["sample_raw_file"]).parent.name)

    def test_an_input_neither_path_nor_name_pairs_is_refused_before_the_download(self) -> None:
        handoff, _payloads = _mtbks64_in_folders()
        handoff["analysis_inputs"][1]["path"] = "raw/batch3/QC2.RAW"
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual("failed", project["repository_metadata"]["analysis_input_check"]["status"])


# ---- a split divides a sample's replicate rows by the part their inputs go to (MTBKS220) ------------------------


class ASplitDividesReplicateRowsByTheirInputs(_Unit, unittest.TestCase):
    """MetaboBank MTBKS220 gives each sample a timsOFF BAF row and a timsON TDF row under one sample id."""

    INPUTS = {"d/S1.d": "baf", "d/S2.d": "baf", "d/T1.d": "tdf", "d/T2.d": "tdf"}
    SAMPLE_OF = {"S1": "X1", "T1": "X1", "S2": "X2", "T2": "X2"}

    def replicated_unit(self, declared_inputs: bool) -> Path:
        manifest_path = self.unit(self.INPUTS, ion_mode="Positive", declared_inputs=declared_inputs)

        def change(manifest: dict) -> None:
            project = manifest["project"]
            for row in project["sample_metadata"]:
                row["sample_id"] = self.SAMPLE_OF[Path(row["raw_file"]).stem]
            for entry in project.get("analysis_inputs") or []:
                entry["sample_id"] = self.SAMPLE_OF[Path(entry["path"]).stem]
            for item in project["files"]:
                if item.get("sample_id"):
                    item["sample_id"] = self.SAMPLE_OF[Path(item["container"]).stem]
            project["class_proposal"]["assignments"] = [
                {"sample_id": "X1", "class_label": "A"}, {"sample_id": "X2", "class_label": "B"}
            ]

        update_manifest(manifest_path, change)
        self.preflight(manifest_path, {name: {"mode": "DDA", "polarity": "Positive"} for name in self.INPUTS})
        return manifest_path

    def check(self, declared_inputs: bool) -> None:
        manifest_path = self.replicated_unit(declared_inputs)

        plan = plan_acquisition_split(manifest_path)
        result = split_unit_by_acquisition(manifest_path, confirmed=True)
        parts = {part["analysis_unit_id"]: read_manifest(part["manifest_path"]) for part in result["parts"]}

        self.assertEqual([], plan["blockers"])
        self.assertEqual({"unit-x-dda": [0, 1], "unit-x-dda-im": [2, 3]},
                         {part["analysis_unit_id"]: part["sample_row_indexes"] for part in plan["parts"]})
        lc = parts["unit-x-dda"]
        self.assertEqual(
            ["FILES/d/S1.d", "FILES/d/S2.d"], [row["raw_file"] for row in lc["project"]["sample_metadata"]]
        )
        assignments = lc["project"]["class_proposal"]["assignments"]
        self.assertEqual(["X1", "X2"], sorted(item["sample_id"] for item in assignments))
        if declared_inputs:
            self.assertEqual(["FILES/d/S1.d", "FILES/d/S2.d"],
                             sorted(entry["path"] for entry in lc["project"]["analysis_inputs"]))
            self.assertEqual(["FILES/d/T1.d", "FILES/d/T2.d"],
                             sorted(entry["path"] for entry in parts["unit-x-dda-im"]["project"]["analysis_inputs"]))
        built = build_repository_analysis_rows(lc)
        # The fixture is a hand-made manifest with no lease lineage; nothing else may fail, and before the rows
        # were divided this part failed with analysis_input_not_found and sample_without_input.
        self.assertEqual(["input_without_lineage"], [item["code"] for item in built["failures"]])
        self.assertEqual([("X1", "FILES/d/S1.d"), ("X2", "FILES/d/S2.d")],
                         [(row["sample_id"], row["sample_raw_file"]) for row in built["rows"]])

    def test_each_part_holds_only_the_rows_and_declared_inputs_of_its_own_inputs(self) -> None:
        self.check(declared_inputs=True)

    def test_an_undeclared_unit_divides_its_rows_alike(self) -> None:
        self.check(declared_inputs=False)


# ---- polarity tokens: a member of the other polarity is never paired ------------------------------------------


class APairingNeverCrossesAPolarityToken(_Workspace):
    def pairings(self, members: list[str], samples: list[tuple[str, str]], **project_changes) -> dict:
        data_root = self.root / "raw" / "data"
        project, _ = _st001264(members, samples)
        for key, value in project_changes.items():
            setattr(project, key, value)
        extracted = {_file_key(str(data_root / name)): {"path": name} for name in members}
        return _member_name_pairings(project, extracted, data_root)

    def test_a_prefixed_member_of_the_other_polarity_is_refused_and_recorded(self) -> None:
        result = self.pairings(["NEG_S1.raw", "NEG_S2.raw", "POS_S2.raw"], [("1", "S1.raw"), ("2", "S2.raw")])

        self.assertEqual({}, result["paired"])
        refused = {(item["member_name"].casefold(), item["reason"]) for item in result["refused"]}
        self.assertIn(("neg_s1.raw", "polarity_token_contradicts_ion_mode"), refused)
        self.assertIn(("neg_s2.raw", "not_one_to_one"), refused)

    def test_a_polarity_folder_counts_as_a_token_of_the_members_path(self) -> None:
        result = self.pairings(["NEG/x_S1.raw"], [("1", "S1.raw")])

        self.assertEqual({}, result["paired"])
        self.assertEqual(["polarity_token_contradicts_ion_mode"], [item["reason"] for item in result["refused"]])

    def test_only_a_whole_token_states_a_polarity(self) -> None:
        """'position' and 'negx' are no polarity tokens; 'Pos' in any case is one, and agrees here."""
        result = self.pairings(
            ["position_S1.raw", "negx_S2.raw", "run_Pos_S3.raw"], [("1", "S1.raw"), ("2", "S2.raw"), ("3", "S3.raw")]
        )

        self.assertEqual(
            {"S1.raw", "S2.raw", "S3.raw"}, {item["declared_raw_file"] for item in result["paired"].values()}
        )

    def test_a_token_pairing_across_the_declared_names_polarity_is_refused(self) -> None:
        result = self.pairings(["VV_1_b_neg.raw"], [("1", "VV_1_a_pos.raw")], ion_mode="Unknown")

        self.assertEqual({}, result["paired"])
        self.assertEqual(
            [("leading_identifier_token", "polarity_token_contradicts_declared_name")],
            [(item["rule"], item["reason"]) for item in result["refused"]],
        )

    def test_a_polarity_token_beside_a_control_blank_or_qc_token_states_no_polarity(self) -> None:
        """Neg_Ctrl_1.raw is a negative control, not a negative-mode file (review of PR #58, 2026-10-06)."""
        for name in (
            "Neg_Ctrl_1.raw", "021518_Neg_Ctrl_1.raw", "pos_ctrl.raw", "Positive_control_3.raw", "neg_control.raw",
            "neg_blank.raw", "S1-control-neg.raw", "QC_pos_01.raw", "Pos QC 2.raw",
        ):
            self.assertEqual(set(), name_polarities(name, polarity_named=False), name)
        self.assertFalse(names_state_polarity(["Neg_Ctrl_1.raw", "S_1.raw", "pos_ctrl.raw"]))

    def test_a_polarity_token_not_beside_one_still_states_its_polarity(self) -> None:
        for polarity_named in (False, True):
            self.assertEqual({"Negative"}, name_polarities("S1_neg.raw", polarity_named=polarity_named))
            self.assertEqual({"Negative"}, name_polarities("neg_S1_ctrl.raw", polarity_named=polarity_named))
            # A control sample's file states the polarity it ran in by a token of its own, and that one only,
            self.assertEqual({"Negative"}, name_polarities("Pos_Ctrl_1_neg.raw", polarity_named=polarity_named))
            self.assertEqual({"Positive"}, name_polarities("Neg_Ctrl_1_pos.raw", polarity_named=polarity_named))
            # and a polarity folder states its polarity whatever the file in it is called.
            self.assertEqual(
                {"Negative"}, name_polarities("NEG/021518_Pos_Ctrl_1.raw", polarity_named=polarity_named)
            )

    def test_in_a_listing_named_by_polarity_a_token_beside_qc_blank_or_control_is_the_files_polarity(self) -> None:
        """Every Catalog unit whose rows name a file by such a token against the unit's ion mode names its other
        files by polarity: ST002251, ST002510, ST003858 (names as the Catalog gives them)."""
        listing = ["20200715_003_QC-pos.mzML", "20200715_004_QC-neg.mzML", "20200715_005_S1-pos.mzML"]

        self.assertTrue(names_state_polarity(listing))
        self.assertEqual({"Negative"}, name_polarities("20200715_004_QC-neg.mzML", polarity_named=True))
        self.assertEqual({"Negative"}, name_polarities("GL_NEG_Ctrl_B3_1.raw", polarity_named=True))
        self.assertEqual({"Positive"}, name_polarities("2024-11-25_Wills-15-Blank_POS_001.mzML", polarity_named=True))

    def test_a_control_sample_named_by_its_polarity_is_paired_in_either_polarity_unit(self) -> None:
        positive = self.pairings(
            ["021518_Neg_Ctrl_1.raw", "021518_S_1.raw"], [("c", "Neg_Ctrl_1.raw"), ("s", "S_1.raw")]
        )
        negative = self.pairings(["run_pos_ctrl.raw"], [("c", "pos_ctrl.raw")], ion_mode="Negative")
        by_token = self.pairings(["C_7_exp2_neg_control.raw"], [("c", "C_7_neg_control.raw")])

        self.assertEqual(
            {"Neg_Ctrl_1.raw": PREFIXED_MEMBER_PAIRING, "S_1.raw": PREFIXED_MEMBER_PAIRING},
            {item["declared_raw_file"]: item["paired_by"] for item in positive["paired"].values()},
        )
        self.assertEqual([], positive["refused"])
        self.assertEqual(["pos_ctrl.raw"], [item["declared_raw_file"] for item in negative["paired"].values()])
        self.assertEqual([], negative["refused"])
        self.assertEqual(
            [("C_7_neg_control.raw", LEADING_IDENTIFIER_TOKEN_PAIRING)],
            [(item["declared_raw_file"], item["paired_by"]) for item in by_token["paired"].values()],
        )

    def test_a_control_sample_whose_name_gives_its_own_polarity_pairs_in_a_listing_named_by_polarity(self) -> None:
        result = self.pairings(
            ["VV_1_a_exp_pos.raw", "Neg_Ctrl_1_exp_pos.raw"], [("v", "VV_1_a_pos.raw"), ("c", "Neg_Ctrl_1_pos.raw")]
        )

        self.assertEqual(
            {"VV_1_a_pos.raw", "Neg_Ctrl_1_pos.raw"}, {item["declared_raw_file"] for item in result["paired"].values()}
        )
        self.assertEqual([], result["refused"])

    def test_a_control_sample_of_the_other_polarity_is_still_refused(self) -> None:
        in_folder = self.pairings(["NEG/021518_Neg_Ctrl_1.raw"], [("c", "Neg_Ctrl_1.raw")])
        by_name = self.pairings(["Pos_Ctrl_1_neg.raw"], [("c", "Pos_Ctrl_1_pos.raw")])
        against_declared = self.pairings(
            ["Neg_Ctrl_1_run2_neg.raw"], [("c", "Neg_Ctrl_1_pos.raw")], ion_mode="Unknown"
        )

        self.assertEqual({}, in_folder["paired"])
        self.assertEqual(["polarity_token_contradicts_ion_mode"], [item["reason"] for item in in_folder["refused"]])
        self.assertEqual({}, by_name["paired"])
        self.assertEqual(["polarity_token_contradicts_ion_mode"], [item["reason"] for item in by_name["refused"]])
        self.assertEqual({}, against_declared["paired"])
        self.assertEqual(
            ["polarity_token_contradicts_declared_name"], [item["reason"] for item in against_declared["refused"]]
        )

    def test_a_qc_or_blank_of_the_other_polarity_in_a_listing_named_by_polarity_is_still_refused(self) -> None:
        """Catalog-shaped: ST002251's positive unit lists 20200715_004_QC-neg.mzML beside its _pos files, and
        ST003858's negative unit lists Blank_POS_001.mzML beside its _NEG files."""
        positive_rows = ["20200715_003_QC-pos.mzML", "20200715_004_QC-neg.mzML", "20200715_005_S1-pos.mzML"]
        positive = self.pairings(
            [f"run_{name}" for name in positive_rows], [(str(index), name) for index, name in enumerate(positive_rows)]
        )
        negative_rows = ["2024-11-25_Wills-15-Blank_POS_001.mzML", "2024-11-25_Wills-15-S1_NEG_001.mzML"]
        negative = self.pairings(
            [f"run_{name}" for name in negative_rows],
            [(str(index), name) for index, name in enumerate(negative_rows)],
            ion_mode="Negative",
        )

        self.assertEqual(
            {"20200715_003_QC-pos.mzML", "20200715_005_S1-pos.mzML"},
            {item["declared_raw_file"] for item in positive["paired"].values()},
        )
        self.assertEqual(
            [("20200715_004_QC-neg.mzML", "polarity_token_contradicts_ion_mode")],
            [(item["declared_raw_file"], item["reason"]) for item in positive["refused"]],
        )
        self.assertEqual(
            ["2024-11-25_Wills-15-S1_NEG_001.mzML"], [item["declared_raw_file"] for item in negative["paired"].values()]
        )
        self.assertEqual(
            [("2024-11-25_Wills-15-Blank_POS_001.mzML", "polarity_token_contradicts_ion_mode")],
            [(item["declared_raw_file"], item["reason"]) for item in negative["refused"]],
        )

    def test_a_polarity_folder_named_with_qc_blank_or_control_states_its_polarity(self) -> None:
        """Only a file name's pos/neg token can name a sample: a QC_NEG/ or Blank_POS/ folder states its polarity,
        as NEG/ does, in any listing (review of PR #58, 2026-10-07)."""
        for polarity_named in (False, True):
            for path, polarity in (
                ("QC_NEG/2020_QC_1.raw", "Negative"), ("Blank_POS/2020_B_1.raw", "Positive"),
                ("neg_ctrl/run_S1.raw", "Negative"), (r"Data\Pos-Control\run_S1.raw", "Positive"),
                ("QC_NEG/2020_QC_1.raw/", "Negative"),
            ):
                self.assertEqual({polarity}, name_polarities(path, polarity_named=polarity_named), path)
            # The file name's own token beside QC is still a sample's name where the listing states no polarity.
            self.assertEqual(set(), name_polarities("Samples/Neg_Ctrl_1.raw", polarity_named=False))
            self.assertEqual({"Negative"}, name_polarities("QC_NEG/Pos_Ctrl_1.raw", polarity_named=polarity_named))
        self.assertTrue(names_state_polarity(["QC_NEG/2020_QC_1.raw", "Samples/2020_S1.raw"]))
        self.assertFalse(names_state_polarity(["Samples/Neg_Ctrl_1.raw", "Samples/2020_S1.raw"]))

    def test_a_member_in_a_qc_or_blank_folder_of_the_other_polarity_is_refused(self) -> None:
        """The reviewer's case: a positive unit's QC_1.raw is not paired with QC_NEG/2020_QC_1.raw, nor a negative
        unit's B_1.raw with Blank_POS/2020_B_1.raw; a385a28 refused both, and so does this branch."""
        positive = self.pairings(
            ["QC_NEG/2020_QC_1.raw", "Samples/2020_S1.raw"], [("q", "QC_1.raw"), ("s", "S1.raw")]
        )
        negative = self.pairings(
            ["Blank_POS/2020_B_1.raw", "Samples/2020_S1.raw"], [("b", "B_1.raw"), ("s", "S1.raw")],
            ion_mode="Negative",
        )

        for result, refused in ((positive, "QC_1.raw"), (negative, "B_1.raw")):
            self.assertEqual(["S1.raw"], [item["declared_raw_file"] for item in result["paired"].values()])
            self.assertEqual(
                [(refused, "polarity_token_contradicts_ion_mode")],
                [(item["declared_raw_file"], item["reason"]) for item in result["refused"]],
            )

    def test_a_member_in_a_qc_folder_of_the_units_own_polarity_is_paired(self) -> None:
        result = self.pairings(["QC_POS/2020_QC_1.raw", "Samples/2020_S1.raw"], [("q", "QC_1.raw"), ("s", "S1.raw")])

        self.assertEqual({"QC_1.raw", "S1.raw"}, {item["declared_raw_file"] for item in result["paired"].values()})
        self.assertEqual([], result["refused"])


# ---- leading identifier tokens (the user's decision of 2026-10-06) ---------------------------------------------

ST001359_SAMPLES = [
    ("VV_13_HEpG2_C1", "VV_13_HEpG2_C1_pos.raw"), ("VV_14_HEpG2_C2", "VV_14_HEepG2_C2_pos.raw"),
    ("VV_15_HEpG2_C3", "VV_15_HEpG2_C3_pos.raw"), ("VV_16_HEpG2_SDC1", "VV_16_HEpG2_SDC1_pos.raw"),
    ("VV_17_HEpG2_SDC2", "VV_17_HEpG2_SDC2_pos.raw"), ("VV_18_HEpG2_SDC3", "VV_18_HEpG2_SDC3_pos.raw"),
]
ST001359_MEMBERS = [
    "VV_13_HEpG2_C1_exp344_pos.raw", "VV_14_HEpG2_C2_exp344_pos.raw", "VV_15_HEpG2_C3_exp344_pos.raw",
    "VV_16_HEpG2_SDC1_exp344_pos.raw", "VV_17_HEpG2_SDC2_exp344_pos.raw", "VV_18_HEpG2_SDC3_exp344_pos.raw",
]


class ADeclaredNameIsPairedByItsLeadingIdentifier(_Workspace):
    def pairings(self, members: list[str], samples: list[tuple[str, str]], **project_changes) -> dict:
        return APairingNeverCrossesAPolarityToken.pairings(self, members, samples, **project_changes)

    def test_the_key_is_the_stem_up_to_its_first_token_with_a_digit(self) -> None:
        self.assertEqual("vv_13", leading_identifier_key("VV_13_HEpG2_C1_pos.raw"))
        self.assertEqual("vv_14", leading_identifier_key("VV_14_HEepG2_C2_pos.raw"))
        self.assertEqual("biorec1", leading_identifier_key("BioRec1.raw"))
        self.assertEqual("sample1", leading_identifier_key("Sample1"))
        self.assertEqual("qc_pool_2", leading_identifier_key("QC pool-2.mzML"))
        # A run date is no identifier: a key of digits only, or a name without a digit, gives none.
        self.assertEqual("", leading_identifier_key("021518_387057_CSHp_BioRec1.raw"))
        self.assertEqual("", leading_identifier_key("blank.raw"))

    def test_st001359s_six_declared_names_pair_with_their_members_the_misspelt_one_included(self) -> None:
        result = self.pairings(ST001359_MEMBERS, ST001359_SAMPLES)

        paired = {Path(key).name.casefold(): item for key, item in result["paired"].items()}
        self.assertEqual(6, len(paired))
        self.assertEqual(
            {"declared_raw_file": "VV_14_HEepG2_C2_pos.raw", "paired_by": LEADING_IDENTIFIER_TOKEN_PAIRING,
             "key": "vv_14"},
            paired["vv_14_hepg2_c2_exp344_pos.raw"],
        )
        self.assertEqual([], result["refused"])

    def test_st001264s_rows_named_sample1_are_never_paired_with_its_youn_sa_members(self) -> None:
        result = self.pairings(ST001264_MEMBERS, ST001264_SAMPLES)

        self.assertEqual(
            {"BioRec1.raw", "BioRec2.raw", "BioRec3.raw"},
            {item["declared_raw_file"] for item in result["paired"].values()},
        )
        self.assertEqual({PREFIXED_MEMBER_PAIRING}, {item["paired_by"] for item in result["paired"].values()})

    def test_a_key_two_declared_names_share_pairs_neither_and_says_so(self) -> None:
        result = self.pairings(["VV_1_x_pos.raw"], [("a", "VV_1_a_pos.raw"), ("b", "VV_1_b_pos.raw")])

        self.assertEqual({}, result["paired"])
        self.assertEqual({"leading_identifier_not_unique"}, {item["reason"] for item in result["refused"]})

    def test_a_key_two_members_share_pairs_neither(self) -> None:
        result = self.pairings(["VV_1_x_pos.raw", "VV_1_y_pos.raw"], [("a", "VV_1_a_pos.raw")])

        self.assertEqual({}, result["paired"])
        self.assertEqual(2, len(result["refused"]))

    def test_exact_and_prefixed_pairings_come_first(self) -> None:
        # S_1.raw is carried exactly; R_2.raw behind a prefix; only T_3 is left for its token.
        result = self.pairings(
            ["S_1.raw", "x_R_2.raw", "T_3_extra.raw"], [("s", "S_1.raw"), ("r", "R_2.raw"), ("t", "T_3_pos.raw")]
        )

        self.assertEqual(
            {"R_2.raw": PREFIXED_MEMBER_PAIRING, "T_3_pos.raw": LEADING_IDENTIFIER_TOKEN_PAIRING},
            {item["declared_raw_file"]: item["paired_by"] for item in result["paired"].values()},
        )

    def test_one_samples_files_are_one_candidate_and_each_is_paired(self) -> None:
        # A sample's encodings and copies (one stem, one stated polarity, whatever folders) are one candidate for a
        # declared name: each is paired with it, and which runs is the one encoding rule's (2026-10-09), never the
        # pairing's. Before, a twin made the token and prefix rules refuse both.
        data_key = _file_key(str(self.root / "raw" / "data"))
        result = self.pairings(
            ["VV_13_C1_exp344_pos.mzML", "VV_13_C1_exp344_pos.raw", "x_S7.mzML", "x_S7.raw", "x_S7.d/analysis.baf",
             "x_S8.mzML", "mzML/x_S8.mzML", "x_S8.mzXML"],
            [("c1", "VV_13_C1_pos.mzML"), ("s7", "S7"), ("s8", "S8")],
        )

        self.assertEqual(
            {"vv_13_c1_exp344_pos.mzml": ("VV_13_C1_pos.mzML", LEADING_IDENTIFIER_TOKEN_PAIRING),
             "vv_13_c1_exp344_pos.raw": ("VV_13_C1_pos.mzML", LEADING_IDENTIFIER_TOKEN_PAIRING),
             "x_s7.mzml": ("S7", PREFIXED_MEMBER_PAIRING), "x_s7.raw": ("S7", PREFIXED_MEMBER_PAIRING),
             "x_s7.d": ("S7", PREFIXED_MEMBER_PAIRING),
             "x_s8.mzml": ("S8", PREFIXED_MEMBER_PAIRING), "mzml/x_s8.mzml": ("S8", PREFIXED_MEMBER_PAIRING),
             "x_s8.mzxml": ("S8", PREFIXED_MEMBER_PAIRING)},
            {os.path.relpath(key, data_key).replace("\\", "/"): (item["declared_raw_file"], item["paired_by"])
             for key, item in result["paired"].items()},
        )
        self.assertEqual([], result["refused"])

    def test_one_samples_files_across_the_polarity_are_each_refused(self) -> None:
        result = self.pairings(["x_S7_neg.mzML", "x_S7_neg.raw"], [("s7", "S7_neg")])

        self.assertEqual({}, result["paired"])
        self.assertEqual(
            [("x_s7_neg.mzml", "polarity_token_contradicts_ion_mode"), ("x_s7_neg.raw", "polarity_token_contradicts_ion_mode")],
            sorted((Path(item["member_name"]).name.casefold(), item["reason"]) for item in result["refused"]),
        )

    def test_two_samples_one_name_claims_are_still_refused(self) -> None:
        # Two stems are two samples: a declared name they both claim pairs neither, as before.
        result = self.pairings(["a_S7.raw", "b_x_S7.raw"], [("s7", "S7.raw")])
        self.assertEqual({}, result["paired"])
        self.assertEqual({"not_one_to_one"}, {item["reason"] for item in result["refused"]})

        result = self.pairings(["VV_1_x_pos.raw", "VV_1_y_pos.raw"], [("a", "VV_1_a_pos.mzML")])
        self.assertEqual({}, result["paired"])
        self.assertEqual({"leading_identifier_not_unique"}, {item["reason"] for item in result["refused"]})

    def test_a_token_is_never_paired_beside_the_rows_exact_file_in_any_encoding(self) -> None:
        # Review of PR #69, follow-up 3: the exact-first rule keeps a token from pairing anything with a declared
        # name a member carries exactly, whatever the member's encoding: a file of another stem that shares the key is
        # another injection as far as the record can tell. No pairing, and no refusal: the token rule never ran.
        for members, raw in (
            (["VV_13_C1_pos.raw", "VV_13_C1_rep2_pos.mzML"], "VV_13_C1_pos.raw"),
            (["VV_13_C1_pos.raw", "VV_13_C1_rep2_pos.raw"], "VV_13_C1_pos.raw"),
            (["VV_13_C1_pos.mzML", "VV_13_C1_exp344_pos.raw"], "VV_13_C1_pos.mzML"),
        ):
            with self.subTest(members=members):
                self.assertEqual({"paired": {}, "refused": []}, self.pairings(members, [("c1", raw)]))

    def test_beside_another_stem_the_rows_own_stem_is_paired_and_the_other_refused(self) -> None:
        # The key vv_13 is carried by two stems, so it does not match uniquely: VV_13_C1_exp344_pos.raw is refused.
        # VV_13_C1_pos.mzML, of the declared name's own stem, is the row's (clause 4) and paired by the key as it is
        # alone. The rank of either file does not change that.
        data_key = _file_key(str(self.root / "raw" / "data"))
        for own, other in (("VV_13_C1_pos.mzML", "VV_13_C1_exp344_pos.raw"),
                           ("VV_13_C1_pos.mzML", "VV_13_C1_exp344_pos.mzML")):
            with self.subTest(other=other):
                result = self.pairings([own, other], [("c1", "VV_13_C1_pos.raw")])

                self.assertEqual(
                    {own.casefold(): ("VV_13_C1_pos.raw", LEADING_IDENTIFIER_TOKEN_PAIRING)},
                    {os.path.relpath(key, data_key).replace("\\", "/"): (item["declared_raw_file"], item["paired_by"])
                     for key, item in result["paired"].items()},
                )
                self.assertEqual([(other.casefold(), "leading_identifier_not_unique")],
                                 [(item["member_name"].casefold(), item["reason"]) for item in result["refused"]])
        # Alone, as always.
        result = self.pairings(["VV_13_C1_pos.mzML"], [("c1", "VV_13_C1_pos.raw")])
        self.assertEqual(["VV_13_C1_pos.raw"], [item["declared_raw_file"] for item in result["paired"].values()])

    def test_a_unit_whose_catalog_declared_its_inputs_is_never_paired_by_token(self) -> None:
        declared = [{"path": "VV_13_HEpG2_C1_pos.raw", "kind": "file", "sample_id": "VV_13_HEpG2_C1"}]
        result = self.pairings(ST001359_MEMBERS, ST001359_SAMPLES, analysis_inputs=declared)

        self.assertEqual({"paired": {}, "refused": []}, result)


def _st001359_handoff() -> tuple[dict, dict[str, bytes]]:
    data = _zip({name: f"thermo raw bytes of {name}".encode() for name in ST001359_MEMBERS})
    url = "https://example.org/studydownload/ST001359_rawdata.zip"
    files = [{
        "path": "ST001359_rawdata.zip", "download_url": url, "size_bytes": len(data),
        "checksum": hashlib.md5(data).hexdigest(), "role": "raw_archive", "sample_id": "", "sample_id_resolved": False,
    }]
    samples = [{"sample_id": sample, "raw_file": raw, "attributes": {}} for sample, raw in ST001359_SAMPLES]
    proposal = {
        "proposal_id": "p-st001359", "unit_id": "3c19ade8159ca01ad428", "status": "accepted",
        "selected_fields": [], "rationale": "abstention",
        "contrast_definition": {"kind": "abstention", "class_label": "All", "reason": "no_usable_declared_factor"},
        "assignments": [{"sample_id": sample, "class_label": "All", "values": {}} for sample, _raw in ST001359_SAMPLES],
    }
    handoff = _handoff("ST001359", "3c19ade8159ca01ad428", files, [], samples, proposal)
    handoff["repository"] = "metabolomics_workbench"
    return handoff, {url: data}


class AnInferredPairingIsAlwaysLeftOnRecord(_Workspace):
    def test_the_lease_the_disposition_the_csv_and_the_reviewed_table_say_how_each_file_was_paired(self) -> None:
        from msdial_app.raw_metadata_preflight import decide_disposition

        handoff, payloads = _st001359_handoff()
        manifest_path = _lease(self.root, handoff, payloads)
        _set_types(manifest_path, "DDA")
        manifest = read_manifest(manifest_path)

        # The lineage row of each input names the declared raw file, the rule and the key.
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        self.assertEqual(
            {"declared_raw_file": "VV_14_HEepG2_C2_pos.raw", "member_name": "VV_14_HEpG2_C2_exp344_pos.raw",
             "paired_by": LEADING_IDENTIFIER_TOKEN_PAIRING, "key": "vv_14"},
            rows["VV_14_HEpG2_C2_exp344_pos.raw"]["name_pairing"],
        )
        self.assertEqual("VV_14_HEpG2_C2", rows["VV_14_HEpG2_C2_exp344_pos.raw"]["sample_id"])
        # The attribute stage counts and lists every inferred pairing, and the unit carries the warning.
        attribute = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "attribute")
        self.assertEqual(6, attribute["leading_identifier_token_pairings"])
        self.assertEqual(6, len(attribute["inferred_name_pairings"]))
        self.assertEqual([INFERRED_PAIRING_WARNING], attribute["warnings"])
        self.assertEqual([INFERRED_PAIRING_WARNING], manifest["warnings"])
        self.assertEqual(6, len(manifest["input_name_pairings"]["paired"]))
        self.assertIn(INFERRED_PAIRING_WARNING, decide_disposition(manifest)["warnings"])

        with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
            prepared = mcp_server.msdial_prepare_repository_reanalysis(confirmed=True, manifest_path=str(manifest_path))
        with open(prepared["files"]["metadata_tsv"], encoding="utf-8-sig", newline="") as handle:
            reviewed = list(csv.DictReader(handle, delimiter="\t"))
        recorded = read_manifest(manifest_path)["analysis_csv"]

        self.assertTrue(prepared["prepared"], prepared)
        self.assertEqual([LEADING_IDENTIFIER_TOKEN_PAIRING] * 6, [row["raw_file_paired_by"] for row in reviewed])
        self.assertEqual([INFERRED_PAIRING_WARNING], recorded["warnings"])
        self.assertEqual(6, len(recorded["inferred_name_pairings"]))
        self.assertEqual([INFERRED_PAIRING_WARNING], prepared["preview"]["warnings"])

    def test_a_new_production_run_keeps_the_pairing_column_in_its_reviewed_table(self) -> None:
        # 0.5.25's raw_file_paired_by column and 0.5.29's new_run prepare: the new run's reviewed sample table
        # must say how each file was paired, as the first run's did.
        handoff, payloads = _st001359_handoff()
        manifest_path = _lease(self.root, handoff, payloads)
        _set_types(manifest_path, "DDA")
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
            first = mcp_server.msdial_prepare_repository_reanalysis(confirmed=True, manifest_path=str(manifest_path))
        self.assertTrue(first["prepared"], first)

        def finish(manifest: dict) -> None:
            manifest["status"] = "mztab_validated"
            manifest["finalized_at"] = "2026-10-07T00:00:00+00:00"

        update_manifest(manifest_path, finish)
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
            second = mcp_server.msdial_prepare_repository_reanalysis(
                confirmed=True, manifest_path=str(manifest_path), new_run=True
            )
        self.assertTrue(second["prepared"], second)
        self.assertNotEqual(first["output_root"], second["output_root"])
        with open(second["files"]["metadata_tsv"], encoding="utf-8-sig", newline="") as handle:
            reviewed = list(csv.DictReader(handle, delimiter="	"))
        self.assertEqual([LEADING_IDENTIFIER_TOKEN_PAIRING] * 6, [row["raw_file_paired_by"] for row in reviewed])

    def test_a_unit_whose_rows_are_not_all_delivered_says_so_beside_its_csv(self) -> None:
        """ST001264 runs its 3 BioRec rows; the rows named Sample1.. have no input, and that is recorded."""
        project, payloads = _st001264(ST001264_MEMBERS, ST001264_SAMPLES)
        manifest = self.lease(project, payloads)
        built = build_repository_analysis_rows(manifest)

        self.assertEqual([], built["failures"])
        self.assertEqual({"sample_rows": 5, "with_input": 3, "without_input": 2}, built["sample_row_coverage"])
        self.assertEqual([PARTIAL_SAMPLE_COVERAGE_WARNING, INFERRED_PAIRING_WARNING], built["warnings"])
        self.assertEqual(
            [PREFIXED_MEMBER_PAIRING] * 3, [row["raw_file_paired_by"] for row in built["rows"]]
        )

    def test_a_unit_paired_exactly_records_what_it_always_did(self) -> None:
        handoff, payloads = _mtbks64()
        manifest_path = _lease(self.root, handoff, payloads)
        manifest = read_manifest(manifest_path)
        built = build_repository_analysis_rows(manifest)

        self.assertNotIn("warnings", manifest)
        self.assertNotIn("input_name_pairings", manifest)
        self.assertEqual([], built["warnings"])
        self.assertEqual({"exact"}, {row["raw_file_paired_by"] for row in built["rows"]})


# ---- a split part carries the inferred-pairing record of its own inputs and rows ------------------------------


def _paired_archive_handoff(members: dict[str, bytes], rows: list[tuple[str, str]]) -> tuple[dict, dict[str, bytes]]:
    """A Workbench unit whose study archive holds ``members`` and whose rows declare (sample_id, raw_file)."""
    data = _zip(members)
    url = "https://example.org/studydownload/ST000001_rawdata.zip"
    files = [{
        "path": "ST000001_rawdata.zip", "download_url": url, "size_bytes": len(data),
        "checksum": hashlib.md5(data).hexdigest(), "role": "raw_archive", "sample_id": "", "sample_id_resolved": False,
    }]
    samples = [{"sample_id": sample, "raw_file": raw, "attributes": {}} for sample, raw in rows]
    proposal = {
        "proposal_id": "p-split", "unit_id": "u-split", "status": "accepted", "selected_fields": [],
        "rationale": "abstention",
        "contrast_definition": {"kind": "abstention", "class_label": "All", "reason": "no_usable_declared_factor"},
        "assignments": [
            {"sample_id": sample, "class_label": "All", "values": {}}
            for sample in dict.fromkeys(sample for sample, _raw in rows)
        ],
    }
    handoff = _handoff("ST000001", "u-split", files, [], samples, proposal)
    handoff["repository"] = "metabolomics_workbench"
    return handoff, {url: data}


class ASplitPartCarriesItsOwnPairingRecord(_Workspace):
    """The lease's manifest-level record (warnings, input_name_pairings) went to no part (review of PR #58)."""

    def split(
        self, members: dict[str, bytes], rows: list[tuple[str, str]], modes: dict[str, str], *, campaign: bool = False
    ) -> tuple:
        handoff, payloads = _paired_archive_handoff(members, rows)
        manifest_path = _lease(self.root, handoff, payloads, campaign=campaign)
        inputs = read_manifest(manifest_path)["input_candidates"]
        if campaign:
            # A campaign's preflight runs a verified, pinned extractor only, and resolves a DIA scheme from the
            # header's isolation windows (_header gives a DIA record twenty).
            from test_raw_metadata_preflight import _Extractor, _PinnedExtractor

            extractor = _PinnedExtractor.make(self.root / "build")
            fake = _Extractor(
                {Path(path).name: {"method": modes.get(Path(path).name, "DDA"), "polarity": "Positive"} for path in inputs}
            )
        else:
            extractor = self.root / "RawMetadataConsoleApp.exe"
            extractor.write_bytes(b"stub")
            fake = _extractor({
                f"{Path(path).parent.name}/{Path(path).name}": {"mode": modes.get(Path(path).name, "DDA"), "polarity": "Positive"}
                for path in inputs
            })
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=fake):
            run_raw_metadata_preflight(manifest_path, extractor)
        result = split_unit_by_acquisition(manifest_path, confirmed=True)
        self.assertTrue(result["written"], result["blockers"])
        parts = {part["analysis_unit_id"]: read_manifest(part["manifest_path"]) for part in result["parts"]}
        return read_manifest(manifest_path), parts

    def test_each_part_carries_the_pairings_and_refusals_of_its_own_inputs_and_rows(self) -> None:
        from msdial_app.raw_metadata_preflight import decide_disposition

        members = ["A_1_exp_pos.raw", "B_2_exp_neg.raw", "run_C_3_pos.raw", "run_D_4_pos.raw"]
        parent, parts = self.split(
            {name: f"thermo raw bytes of {name}".encode() for name in members},
            # Sample X has two rows; B_2's only member is of the other polarity and is refused.
            [("X", "A_1_pos.raw"), ("X", "B_2_pos.raw"), ("Y", "C_3_pos.raw"), ("Z", "D_4_pos.raw")],
            {"run_D_4_pos.raw": "DIA"},
        )
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]

        self.assertEqual(3, len(parent["input_name_pairings"]["paired"]))
        self.assertEqual(
            {"A_1_exp_pos.raw": LEADING_IDENTIFIER_TOKEN_PAIRING, "run_C_3_pos.raw": PREFIXED_MEMBER_PAIRING},
            {item["member_name"]: item["paired_by"] for item in dda["input_name_pairings"]["paired"]},
        )
        # The refusal goes with the part that holds the row it was refused for.
        self.assertEqual(
            [("B_2_exp_neg.raw", "B_2_pos.raw", "polarity_token_contradicts_ion_mode")],
            [
                (item["member_name"], item["declared_raw_file"], item["reason"])
                for item in dda["input_name_pairings"]["refused"]
            ],
        )
        self.assertEqual(
            {"paired": [{"member_name": "run_D_4_pos.raw", "declared_raw_file": "D_4_pos.raw",
                         "paired_by": PREFIXED_MEMBER_PAIRING}], "refused": []},
            dia["input_name_pairings"],
        )
        for part in (dda, dia):
            self.assertEqual([INFERRED_PAIRING_WARNING], part["warnings"])
            self.assertIn(INFERRED_PAIRING_WARNING, decide_disposition(part)["warnings"])

    def test_a_part_carries_only_its_own_unattributed_members(self) -> None:
        from msdial_app.raw_metadata_preflight import decide_disposition

        # The handoff's download is the unit's own (unit_files): U_8.raw and U_9.raw pair with no row.
        members = ["A_1_pos.raw", "D_4_pos.raw", "U_8.raw", "U_9.raw"]
        parent, parts = self.split(
            {name: f"thermo raw bytes of {name}".encode() for name in members},
            [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw")],
            {"D_4_pos.raw": "DIA", "U_9.raw": "DIA"},
        )
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]

        self.assertEqual(["U_8.raw", "U_9.raw"], parent["unattributed_members"]["members"])
        for part, member in ((dda, "U_8.raw"), (dia, "U_9.raw")):
            self.assertEqual(
                {"rule": "unit_scoped_archive_2026_10_07", "applied": True, "scope": "unit_files", "count": 1,
                 "members": [member], "paths": [member]},
                part["unattributed_members"],
            )
            self.assertEqual(["unattributed_members_included"], part["warnings"])
            self.assertIn("unattributed_members_included", decide_disposition(part)["warnings"])

    def test_a_part_holds_the_sample_the_encoding_rule_used_a_file_for(self) -> None:
        # The one encoding rule (2026-10-09): S5.raw is used for the row that names S5.mzML (clause 4), so the part
        # that holds S5.raw holds that row, and carries the sample's choice.
        from test_mzml_encoding import _numpress_mzml

        members = {name: f"thermo raw bytes of {name}".encode() for name in ["A_1_pos.raw", "D_4_pos.raw", "S5.raw"]}
        members["S5.mzML"] = _numpress_mzml()
        parent, parts = self.split(
            members, [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw"), ("S", "S5.mzML")], {"D_4_pos.raw": "DIA", "S5.raw": "DIA"},
        )
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]

        record = {"rule": RULE, "used": "S5.raw", "unused": [{"path": "S5.mzML", "reason": "lower_in_encoding_order"}]}
        self.assertEqual([record], parent["encoding_choices"])
        self.assertEqual([record], dia["encoding_choices"])
        self.assertNotIn("encoding_choices", dda)
        self.assertEqual(["Z", "S"], [row["sample_id"] for row in dia["project"]["sample_metadata"]])
        self.assertEqual(["X"], [row["sample_id"] for row in dda["project"]["sample_metadata"]])
        row = next(item for item in dia["input_lineage"]["rows"] if Path(item["path"]).name == "S5.raw")
        self.assertEqual(("S", "S5.mzML"), (row["sample_id"], Path(row["encoding_choice"]["stands_for"]).name))
        self.assertNotIn("unattributed_members", dia)
        built = build_repository_analysis_rows(dia)
        # The stub header gives a DIA record no console type; that is all the builder finds wrong.
        self.assertEqual(["acquisition_type_ambiguous"], [item["code"] for item in built["failures"]])
        self.assertEqual(
            [("D_4_pos.raw", "Z"), ("S5.raw", "S")],
            sorted((Path(item["input_path"]).name, item["sample_id"]) for item in built["rows"]),
        )

    def test_a_part_lists_a_prefix_pairing_by_the_member_paired_not_by_the_file_of_the_rows_stem(self) -> None:
        # Review of PR #69, follow-up 3 (probe P3c through a split): S7.raw runs as S, the row naming S7.mzML, whose
        # 021518_387057_CSHp_S7.mzML a prefix paired. The part holding S7.raw lists that pairing as it was made, by the
        # prefixed mzML, with the file the rule used; S7.raw is never listed as paired by a prefix.
        members = {name: f"thermo raw bytes of {name}".encode() for name in ["A_1_pos.raw", "D_4_pos.raw", "S7.raw"]}
        members[f"{PREFIX}S7.mzML"] = mzml(dda_spectra(6))
        parent, parts = self.split(
            members, [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw"), ("S", "S7.mzML")], {"D_4_pos.raw": "DIA"},
        )
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]

        pairing = {"member_name": f"{PREFIX}S7.mzML", "declared_raw_file": "S7.mzML",
                   "paired_by": PREFIXED_MEMBER_PAIRING, "encoding_used": "S7.raw"}
        self.assertEqual([pairing], parent["input_name_pairings"]["paired"])
        self.assertEqual([pairing], dda["input_name_pairings"]["paired"])
        self.assertEqual([], (dia.get("input_name_pairings") or {}).get("paired") or [])
        row = next(item for item in dda["input_lineage"]["rows"] if Path(item["path"]).name == "S7.raw")
        self.assertEqual(("S", f"{PREFIX}S7.mzML", PREFIXED_MEMBER_PAIRING),
                         (row["sample_id"], row["name_pairing"]["member_name"], row["name_pairing"]["paired_by"]))
        self.assertEqual(["X", "S"], [item["sample_id"] for item in dda["project"]["sample_metadata"]])

    def test_a_part_names_a_converted_member_by_its_mzxml_as_the_parent_does(self) -> None:
        # Review ia-r2 follow-up 1, medium: the part listed 'u_9.mzml' (the converted mzML's lower-cased name),
        # its lineage row 'U_9.mzXML', and gave no converted list.
        from test_mzxml_conversion import dda_32

        members = {name: f"thermo raw bytes of {name}".encode() for name in ["A_1_pos.raw", "D_4_pos.raw", "U_8.raw"]}
        members["U_9.mzXML"] = dda_32()
        parent, parts = self.split(
            members, [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw")], {"D_4_pos.raw": "DIA", "U_8.raw": "DIA"},
            campaign=True,
        )

        self.assertEqual((["U_8.raw", "U_9.mzXML"], ["U_9.mzXML"]),
                         (parent["unattributed_members"]["members"], parent["unattributed_members"]["converted"]))
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]
        self.assertEqual(
            {"rule": "unit_scoped_archive_2026_10_07", "applied": True, "scope": "unit_files", "count": 1,
             "members": ["U_9.mzXML"], "paths": ["U_9.mzXML"], "converted": ["U_9.mzXML"]},
            dda["unattributed_members"],
        )
        self.assertEqual((["U_8.raw"], False), (dia["unattributed_members"]["members"], "converted" in dia["unattributed_members"]))
        for part in (dda, dia):
            lineage = [
                row["name_pairing"]["member_name"] for row in part["input_lineage"]["rows"]
                if (row.get("name_pairing") or {}).get("paired_by") == "unattributed_member"
            ]
            self.assertEqual(part["unattributed_members"]["members"], lineage)

    def test_a_part_of_a_nested_archive_names_its_members_by_basename_and_keeps_their_paths(self) -> None:
        # Review ia-0531, medium: the record listed paths under the data root, the lineage basenames.
        members = ["RUN/A_1_pos.raw", "RUN/D_4_pos.raw", "RUN/U_8.raw", "RUN/more/U_9.raw"]
        parent, parts = self.split(
            {name: f"thermo raw bytes of {name}".encode() for name in members},
            [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw")],
            {"D_4_pos.raw": "DIA", "U_9.raw": "DIA"},
        )

        self.assertEqual(["U_8.raw", "U_9.raw"], parent["unattributed_members"]["members"])
        self.assertEqual(["RUN/more/U_9.raw", "RUN/U_8.raw"], parent["unattributed_members"]["paths"])
        for part, member, path in (
            (parts["u-split-dda"], "U_8.raw", "RUN/U_8.raw"), (parts["u-split-dia"], "U_9.raw", "RUN/more/U_9.raw")
        ):
            record = part["unattributed_members"]
            self.assertEqual(([member], [path], 1), (record["members"], record["paths"], record["count"]))
            lineage = [
                row["name_pairing"]["member_name"] for row in part["input_lineage"]["rows"]
                if (row.get("name_pairing") or {}).get("paired_by") == "unattributed_member"
            ]
            self.assertEqual(record["members"], lineage)

    def test_a_part_whose_inputs_were_named_exactly_carries_no_pairing_record(self) -> None:
        from msdial_app.raw_metadata_preflight import decide_disposition

        members = ["A_1_exp_pos.raw", "D_4_pos.raw"]
        _parent, parts = self.split(
            {name: f"thermo raw bytes of {name}".encode() for name in members},
            [("X", "A_1_pos.raw"), ("Z", "D_4_pos.raw")],
            {"D_4_pos.raw": "DIA"},
        )
        dda, dia = parts["u-split-dda"], parts["u-split-dia"]

        self.assertEqual([INFERRED_PAIRING_WARNING], dda["warnings"])
        self.assertEqual(1, len(dda["input_name_pairings"]["paired"]))
        self.assertNotIn("warnings", dia)
        self.assertNotIn("input_name_pairings", dia)
        self.assertNotIn(INFERRED_PAIRING_WARNING, decide_disposition(dia)["warnings"])

    def test_an_excluded_ion_mobility_part_records_its_pairing_and_its_disposition_says_so(self) -> None:
        _parent, parts = self.split(
            {
                "A_1_exp_pos.d/analysis.baf": b"baf of A_1",
                "run_C_3_pos.d/analysis.tdf": b"tdf of C_3",
                "run_C_3_pos.d/analysis.tdf_bin": b"tdf_bin of C_3",
            },
            [("X", "A_1_pos.d"), ("Y", "C_3_pos.d")],
            {},
        )
        lc, im = parts["u-split-dda"], parts["u-split-dda-im"]

        self.assertEqual(["A_1_exp_pos.d"], [item["member_name"] for item in lc["input_name_pairings"]["paired"]])
        self.assertEqual(["run_C_3_pos.d"], [item["member_name"] for item in im["input_name_pairings"]["paired"]])
        self.assertEqual("excluded_by_preflight", im["status"])
        self.assertEqual([INFERRED_PAIRING_WARNING], im["warnings"])
        self.assertIn(INFERRED_PAIRING_WARNING, im["campaign_disposition"]["warnings"])


if __name__ == "__main__":
    unittest.main()
