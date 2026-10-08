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
        PARTIAL_SAMPLE_COVERAGE_WARNING,
        blocking_failures,
        build_repository_analysis_rows,
    )
    from msdial_app.repository_reanalysis import (
        INFERRED_PAIRING_WARNING,
        LEADING_IDENTIFIER_TOKEN_PAIRING,
        PREFIXED_MEMBER_PAIRING,
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
from test_mzml_encoding import dda_spectra, mzml
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
        # Outside a campaign nothing converts, so an unpaired mzXML stays out (requires_conversion). Of one name in
        # two encodings the encoding order takes one (2026-10-08, second round, answer 3): y_S6.raw over y_S6.mzML,
        # and an admitted BioRec1 .raw over its unpaired .mzML. Two vendor encodings of one name tie, and both stay
        # out as before.
        members = [
            *ST001264_MEMBERS[:3], "x_S9_neg.raw", "x_S8.mzXML", "x_S7.raw", "y_S6.raw", "y_S6.mzML",
            f"{PREFIX}BioRec1.mzML", "z_S5.raw", "z_S5.d/analysis.baf",
        ]
        manifest = self.unit(UNIT_SCOPED, members=members)

        self.assertEqual(
            sorted([*ST001264_MEMBERS[:3], "x_S7.raw", "y_S6.raw"]),
            sorted(Path(item).name for item in manifest["input_candidates"]),
        )
        record = manifest["unattributed_members"]
        self.assertEqual(["x_S7.raw", "y_S6.raw"], record["members"])
        self.assertEqual(
            [(f"{PREFIX}BioRec1.mzML", "chosen_other_encoding"), ("x_S8.mzXML", "requires_conversion"),
             ("x_S9_neg.raw", "polarity_token_contradicts_ion_mode"), ("y_S6.mzML", "chosen_other_encoding"),
             ("z_S5.d", "two_encodings_of_one_name"), ("z_S5.raw", "two_encodings_of_one_name")],
            [(item["member_name"], item["reason"]) for item in record["left_out"]],
        )
        chosen = {item["member_name"]: (item.get("chosen"), item.get("chosen_by")) for item in record["left_out"]}
        self.assertEqual((f"{PREFIX}BioRec1.raw", "encoding_order"), chosen[f"{PREFIX}BioRec1.mzML"])
        self.assertEqual(("y_S6.raw", "encoding_order"), chosen["y_S6.mzML"])
        self.assertEqual((None, None), chosen["z_S5.raw"])
        self.assertEqual(6, record["left_out_count"])
        self.assertEqual(
            [item["member_name"] for item in record["left_out"]], [item["path"] for item in record["left_out"]]
        )

    def test_one_name_in_two_encoding_folders_is_one_sample_and_the_order_takes_one(self) -> None:
        # RAW/ and mzML/ name encodings, not places (_sample_locus): the .raw is taken, the mzML left out for it.
        # POS/ and NEG/ are places: two samples of one name, each taken.
        members = [*ST001264_MEMBERS[:3], "RAW/q_S4.raw", "mzML/q_S4.mzML", "A/r_S3.raw", "B/r_S3.raw"]
        record = self.unit(UNIT_SCOPED, members=members)["unattributed_members"]

        self.assertEqual(["A/r_S3.raw", "B/r_S3.raw", "RAW/q_S4.raw"], record["paths"])
        self.assertEqual(
            [("mzML/q_S4.mzML", "chosen_other_encoding", "RAW/q_S4.raw", "encoding_order")],
            [(item["path"], item["reason"], item["chosen"], item["chosen_by"]) for item in record["left_out"]],
        )

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
            [("mzXML/y_S6.mzXML", "chosen_other_encoding", "y_S6.raw"),
             ("x_S9_neg.mzXML", "polarity_token_contradicts_ion_mode", None)],
            [(item["path"], item["reason"], item.get("chosen")) for item in record["left_out"]],
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

    def test_a_vendor_twin_of_an_admitted_mzxml_is_analysed_for_its_sample_in_a_campaign(self) -> None:
        from test_mzxml_conversion import dda_32

        members = [*ST001264_MEMBERS[:3], "S1.mzXML", "S1.raw"]
        samples = [*ST001264_SAMPLES[:3], ("Sample1", "S1.mzXML")]
        manifest = self.unit(UNIT_SCOPED, members=members, samples=samples, contents={"S1.mzXML": dda_32()},
                             campaign=True)

        # The convert stage analyses the .raw instead of the sample's mzXML: the sample's input, not unattributed.
        self.assertIn("S1.raw", [Path(item).name for item in manifest["input_candidates"]])
        rows = {Path(row["path"]).name: row for row in manifest["input_lineage"]["rows"]}
        self.assertEqual("Sample1", rows["S1.raw"]["sample_id"])
        self.assertNotIn("name_pairing", rows["S1.raw"])
        record = manifest["unattributed_members"]
        self.assertEqual((0, []), (record["count"], record["members"]))
        self.assertEqual(
            [("S1.raw", "analysed_for_an_admitted_sample", "S1.mzXML")],
            [(item["path"], item["reason"], item["stands_for"]) for item in record["left_out"]],
        )

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

    def test_a_campaign_converts_an_unpaired_mzxml_over_its_undecodable_mzml_twin(self) -> None:
        # Review ia-r2 follow-up 1, medium: the encoding order took w_S2.mzML, which the lease then excluded, and
        # the sample had no input. A convertible mzXML outranks an unreadable twin (the user, 2026-09-30).
        from test_mzml_encoding import _numpress_mzml
        from test_mzxml_conversion import dda_32

        members = [*ST001264_MEMBERS[:3], "w_S2.mzXML", "w_S2.mzML"]
        manifest = self.unit(
            UNIT_SCOPED, members=members, contents={"w_S2.mzXML": dda_32(), "w_S2.mzML": _numpress_mzml()},
            campaign=True,
        )

        record = manifest["unattributed_members"]
        self.assertEqual((1, ["w_S2.mzXML"], ["w_S2.mzXML"]), (record["count"], record["members"], record["converted"]))
        self.assertEqual(
            [("w_S2.mzML", "chosen_other_encoding", "w_S2.mzXML", "undecodable_mzml_set_aside")],
            [(item["path"], item["reason"], item["chosen"], item["chosen_by"]) for item in record["left_out"]],
        )
        rows = [row for row in manifest["input_lineage"]["rows"] if Path(row["path"]).name == "w_S2.mzML"]
        self.assertEqual([("converted", "w_S2.mzXML")], [(row["kind"], row["name_pairing"]["member_name"]) for row in rows])
        self.assertNotIn("excluded", record)
        self.assert_the_gate_reads_the_record_as_the_lineage(manifest)

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

    def test_a_vendor_member_is_taken_where_the_units_own_mzml_of_its_sample_cannot_be_decoded(self) -> None:
        from test_mzml_encoding import _numpress_mzml

        members = [*ST001264_MEMBERS[:3], "S1.mzML", "S1.raw"]
        samples = [*ST001264_SAMPLES[:3], ("Sample1", "S1.mzML")]
        manifest = self.unit(UNIT_SCOPED, members=members, samples=samples, contents={"S1.mzML": _numpress_mzml()})

        record = manifest["unattributed_members"]
        self.assertEqual((1, ["S1.raw"]), (record["count"], record["members"]))
        self.assertEqual([{"path": "S1.raw", "instead_of": "S1.mzML"}], record["taken_instead_of_undecodable"])
        self.assertNotIn("left_out", record)
        self.assertIn("S1.raw", [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual(
            [("S1.mzML", "unsupported_mzml_encoding")],
            [(Path(item["path"]).name, item["reason"]) for item in manifest["excluded_input_candidates"]],
        )
        self.assert_the_gate_reads_the_record_as_the_lineage(manifest)

    def test_a_nested_archive_names_its_members_by_basename_as_the_lineage_does(self) -> None:
        # Review ia-0531, medium: the record listed 'ST001264_POSITIVE/<name>', the lineage '<name>', so the
        # gate found no lineage member in the record. Both now give the basename; paths keeps where each lies.
        nested = [f"ST001264_POSITIVE/{name}" for name in ST001264_MEMBERS]
        twins = ["ST001264_POSITIVE/a/QC_01.raw", "ST001264_POSITIVE/b/QC_01.raw"]
        other = "ST001264_POSITIVE/neg/x_S9_neg.raw"
        manifest = self.unit(UNIT_SCOPED, members=[*nested, *twins, other])
        record = manifest["unattributed_members"]

        self.assertEqual(5, record["count"])
        self.assertEqual(sorted([*YOUN, "QC_01.raw", "QC_01.raw"], key=lambda item: (item.casefold(), item)),
                         record["members"])
        self.assertEqual(
            sorted([f"ST001264_POSITIVE/{name}" for name in YOUN] + twins, key=lambda item: (item.casefold(), item)),
            record["paths"],
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
