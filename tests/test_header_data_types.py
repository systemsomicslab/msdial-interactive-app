"""A repository unit's MS1 and MS2 data type come from its inputs' raw headers where they all agree.

The extractor records acquisition.spectrumRepresentation per file: Centroid or Profile when the scan headers
it sampled agree, Mixed when they do not, null when none said. Interactive used to ignore it, so every unit
ran with the template's Centroid; a Thermo unit whose headers all read Profile (Metabolomics Workbench
ST001337 in the 2026-10-03 pilot) was run as Centroid. Every record here is synthetic.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_raw_metadata_preflight as fx

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.raw_metadata_preflight import header_data_types, spectrum_representation_fields
from msdial_app.repository_reanalysis import (
    evaluate_repository_execution_gate,
    record_run_start,
    run_data_types,
    run_raw_metadata_preflight,
)
from msdial_app.workflow import prepare_run

TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"


def _entry(name: str, representation: str = "", *, levels: list[int] | None = None, by_level: dict | None = None) -> dict:
    """One per-file preflight record, as a 0.5.27 summary writes it."""
    levels = [1, 2] if levels is None else levels
    return {
        "file": name,
        "acquisition_mode": "",
        "has_ms1": 1 in levels,
        "has_ms2": 2 in levels,
        "ms_levels": levels,
        "spectrum_representation": representation,
        "spectrum_representation_source": "SpectrumHeader" if representation else "",
        "spectrum_representation_by_level": dict(by_level or {}),
    }


def _waters_record(path: str, survey_continuum: bool, product_continuum: bool) -> dict:
    """A MassLynx record: one survey and one product function, and a lock-mass function with no MS level."""
    whole = {True: "Profile", False: "Centroid"}
    values = {whole[survey_continuum], whole[product_continuum]}
    record = fx._header(path, "DIA", levels=[1, 2])
    record["acquisition"]["spectrumRepresentation"] = {
        "value": next(iter(values)) if len(values) == 1 else "Mixed",
        "source": "SpectrumHeader",
        "confidence": 1.0,
        "evidence": "Sampled scan headers",
    }
    record["experiments"] = [
        {"id": "1", "name": "TOFM", "vendorFields": {"role": "survey", "ms_level": "1", "continuum": str(survey_continuum).lower()}},
        {"id": "2", "name": "TOFM", "vendorFields": {"role": "product", "ms_level": "2", "continuum": str(product_continuum).lower()}},
        {"id": "3", "name": "TOFM", "vendorFields": {"role": "reference", "continuum": "true"}},
    ]
    return record


class HeaderDataTypeDecisionTests(unittest.TestCase):
    def test_inputs_that_all_record_profile_run_as_profile(self) -> None:
        decision = header_data_types([_entry("a.raw", "Profile"), _entry("b.raw", "Profile")])

        self.assertEqual(("Profile", "raw_header"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))
        self.assertEqual(("Profile", "raw_header"), (decision["ms2_data_type"], decision["ms2_data_type_basis"]))
        self.assertEqual({"Profile": 2}, decision["levels"]["ms1"]["recorded"])
        self.assertEqual([], decision["warnings"])
        self.assertFalse(decision["disagreement"])

    def test_inputs_that_disagree_keep_the_default_and_say_so(self) -> None:
        entries = [_entry("a.raw", "Profile"), _entry("b.raw", "Centroid"), _entry("c.raw", "Centroid")]

        decision = header_data_types(entries)

        level = decision["levels"]["ms1"]
        self.assertEqual(("Centroid", "default", "inputs_disagree"), (level["data_type"], level["basis"], level["reason"]))
        self.assertIsNone(level["header_data_type"])
        self.assertEqual({"Centroid": 2, "Profile": 1}, level["recorded"])
        self.assertEqual({"Centroid": ["b.raw", "c.raw"], "Profile": ["a.raw"]}, level["example_files"])
        self.assertTrue(decision["disagreement"])
        self.assertTrue(any(line.startswith("MS1 data type: the raw headers of the inputs disagree (Centroid 2, Profile 1 of 3)") for line in decision["warnings"]))

    def test_the_default_kept_is_the_one_given(self) -> None:
        decision = header_data_types([_entry("a.raw", "Profile"), _entry("b.raw", "Centroid")], {"ms1": "Profile", "ms2": "Centroid"})

        self.assertEqual(("Profile", "default"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))

    def test_one_input_whose_header_is_silent_keeps_the_default(self) -> None:
        decision = header_data_types([_entry("a.raw", "Profile"), {"file": "unread.raw"}])

        self.assertEqual(("Centroid", "default", "unrecorded"), tuple(decision["levels"]["ms1"][key] for key in ("data_type", "basis", "reason")))
        self.assertEqual(["unread.raw"], decision["levels"]["ms1"]["example_files"]["unrecorded"])
        self.assertTrue(any("do not record whether the spectra are centroid or profile" in line for line in decision["warnings"]))

    def test_a_file_that_mixes_both_without_levels_is_unresolved(self) -> None:
        decision = header_data_types([_entry("a.raw", "Mixed"), _entry("b.raw", "Centroid")])

        self.assertEqual(("default", "unresolved"), (decision["levels"]["ms2"]["basis"], decision["levels"]["ms2"]["reason"]))
        self.assertFalse(decision["disagreement"])

    def test_an_input_without_a_level_does_not_vote_for_it(self) -> None:
        decision = header_data_types([_entry("pool.raw", "Profile", levels=[1]), _entry("a.raw", "Profile")])

        self.assertEqual({"Profile": 1}, decision["levels"]["ms2"]["recorded"])
        self.assertEqual(1, decision["levels"]["ms2"]["not_applicable"])
        self.assertEqual("raw_header", decision["ms2_data_type_basis"])

    def test_an_ms1_only_unit_keeps_the_ms2_default_without_a_warning(self) -> None:
        decision = header_data_types([_entry("a.raw", "Profile", levels=[1])])

        self.assertEqual(("Profile", "raw_header"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))
        self.assertEqual(("default", "no_input_at_level"), (decision["levels"]["ms2"]["basis"], decision["levels"]["ms2"]["reason"]))
        self.assertEqual([], decision["warnings"])

    def test_no_inputs_decide_nothing(self) -> None:
        decision = header_data_types([])

        self.assertEqual((0, "default", "default"), (decision["inputs"], decision["ms1_data_type_basis"], decision["ms2_data_type_basis"]))

    def test_names_only_never_paths(self) -> None:
        decision = header_data_types([_entry(r"D:\data\unit\raw\a.raw", "Profile"), _entry(r"D:\data\unit\raw\b.raw", "Centroid")])

        self.assertNotIn("D:", json.dumps(decision))


class PerLevelRepresentationTests(unittest.TestCase):
    def test_a_waters_record_says_which_level_is_which(self) -> None:
        fields = spectrum_representation_fields(_waters_record("a.raw", survey_continuum=True, product_continuum=False))

        self.assertEqual("Mixed", fields["spectrum_representation"])
        # The lock-mass function has no MS level and is not one the file's verdict was read from.
        self.assertEqual({"1": "Profile", "2": "Centroid"}, fields["spectrum_representation_by_level"])

        decision = header_data_types([{"file": "a.raw", **fields}, {"file": "b.raw", **fields}])

        self.assertEqual(("Profile", "raw_header"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))
        self.assertEqual(("Centroid", "raw_header"), (decision["ms2_data_type"], decision["ms2_data_type_basis"]))

    def test_another_reader_gives_one_value_for_the_file(self) -> None:
        record = fx._header("a.mzML")
        record["acquisition"]["spectrumRepresentation"] = {"value": "Centroid", "source": "SpectrumHeader", "confidence": 1.0}

        fields = spectrum_representation_fields(record)

        self.assertEqual(("Centroid", "SpectrumHeader", {}), tuple(fields.values()))

    def test_a_record_without_the_field_records_nothing(self) -> None:
        self.assertEqual("", spectrum_representation_fields(fx._header("a.mzML"))["spectrum_representation"])

    def test_a_level_whose_functions_disagree_is_unresolved(self) -> None:
        record = _waters_record("a.raw", survey_continuum=False, product_continuum=False)
        record["experiments"].append({"id": "4", "vendorFields": {"ms_level": "2", "continuum": "true"}})
        record["acquisition"]["spectrumRepresentation"]["value"] = "Mixed"

        decision = header_data_types([{"file": "a.raw", **spectrum_representation_fields(record)}])

        self.assertEqual(("Centroid", "raw_header"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))
        self.assertEqual("unresolved", decision["levels"]["ms2"]["reason"])


class _RepresentingExtractor(fx._Extractor):
    """fx._Extractor, with each record's spectrumRepresentation set from ``representations`` by file name."""

    def __init__(self, representations: dict, verdicts: dict | None = None) -> None:
        super().__init__(verdicts or {})
        self.representations = representations

    def __call__(self, command, **kwargs):
        completed = super().__call__(command, **kwargs)
        output = Path(command[command.index("--output") + 1])
        if completed.returncode == 0 and output.is_file():
            loaded = json.loads(output.read_text(encoding="utf-8"))
            records = loaded if isinstance(loaded, list) else [loaded]
            for record in records:
                value = self.representations.get(Path(record["source"]["filePath"]).name)
                if value:
                    record["acquisition"]["spectrumRepresentation"] = {
                        "value": value, "source": "SpectrumHeader", "confidence": 1.0, "evidence": "Sampled scan headers"
                    }
            output.write_text(json.dumps(records[0] if len(records) == 1 else records), encoding="utf-8")
        return completed


class PreflightRecordsTheRepresentationTests(fx._Scratch):
    def preflight_with(self, representations: dict) -> dict:
        manifest, extractor, _files = fx._unit(self.root / "unit", sorted(representations), acquisition="DDA")
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=_RepresentingExtractor(representations)):
            return run_raw_metadata_preflight(manifest, extractor)

    def test_each_input_and_the_unit_record_it(self) -> None:
        result = self.preflight_with({"a.mzML": "Profile", "b.mzML": "Profile"})

        summary = result["raw_metadata_preflight"]["summary"]
        self.assertEqual(["Profile", "Profile"], [item["spectrum_representation"] for item in summary["per_file"]])
        self.assertEqual(("inspected_inputs", "Profile", "raw_header"), tuple(summary["data_types"][key] for key in ("scope", "ms1_data_type", "ms1_data_type_basis")))

    def test_a_unit_whose_inputs_disagree_shows_it_in_the_preflight(self) -> None:
        result = self.preflight_with({"a.mzML": "Profile", "b.mzML": "Centroid"})

        data_types = result["raw_metadata_preflight"]["summary"]["data_types"]
        self.assertTrue(data_types["disagreement"])
        self.assertEqual({"Centroid": ["b.mzML"], "Profile": ["a.mzML"]}, data_types["levels"]["ms1"]["example_files"])
        self.assertTrue(data_types["warnings"])


def _write_unit(root: Path, entries: list[dict], *, extra: dict | None = None, preflight_extra: dict | None = None) -> Path:
    """A unit manifest whose preflight recorded the given per-file entries."""
    manifest = root / "provenance" / "run-manifest.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "schema": "msdial-public-reanalysis-run.v1",
                "status": "preflight_passed",
                "workspace": str(root),
                "output_directory": str(root / "out"),
                "execution_allowed": True,
                "input_candidates": [item["file"] for item in entries],
                "project": {"analysis_unit_id": "unit-synthetic", "ion_mode": "Negative", "acquisition_mode": "DDA"},
                "raw_metadata_preflight": {"summary": {"per_file": entries}, **(preflight_extra or {})},
                **(extra or {}),
            }
        ),
        encoding="utf-8",
    )
    return manifest


class _UnitCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.data = self.root / "raw"
        self.data.mkdir()
        self.files = []
        for name in ("a.mzML", "b.mzML"):
            (self.data / name).write_text("", encoding="ascii")
            self.files.append(self.data / name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def state(self, manifest: Path, **values) -> dict:
        return {
            "repository_run_manifest": str(manifest),
            "output_root": str(self.root / "out"),
            "ion_mode": "Negative",
            "files": [{"file_path": str(path), "acquisition_type": "DDA"} for path in self.files],
            **values,
        }

    def unit(self, *representations: str, **options) -> Path:
        return _write_unit(
            self.root,
            [_entry(str(path), value) for path, value in zip(self.files, representations)],
            **options,
        )


class RunDecisionTests(_UnitCase):
    def test_an_input_the_campaign_excluded_does_not_vote(self) -> None:
        manifest = self.unit(
            "Profile",
            "Centroid",
            extra={"campaign_disposition": {"applied": True, "excluded_inputs": [{"path": str(self.files[1]), "reason": "raw_header_unreadable"}]}},
        )

        decision = run_data_types(self.state(manifest))

        self.assertEqual(("Profile", "raw_header", 1), (decision["ms1_data_type"], decision["ms1_data_type_basis"], decision["inputs"]))
        self.assertEqual("run_inputs", decision["scope"])

    def test_a_preflight_from_before_is_read_from_its_output(self) -> None:
        output = self.root / "provenance" / "raw-metadata-preflight.json"
        records = []
        for path in self.files:
            record = fx._header(str(path))
            record["acquisition"]["spectrumRepresentation"] = {"value": "Profile", "source": "SpectrumHeader", "confidence": 1.0}
            records.append(record)
        entries = [{"file": str(path), "acquisition_mode": "DDA", "header_console_acquisition_type": "DDA"} for path in self.files]
        manifest = _write_unit(self.root, entries, preflight_extra={"output": str(output)})
        output.write_text(json.dumps(records), encoding="utf-8")

        decision = run_data_types(self.state(manifest))

        self.assertEqual(("Profile", "raw_header"), (decision["ms1_data_type"], decision["ms1_data_type_basis"]))
        self.assertTrue(decision["read_from_preflight_output"])

    def test_outside_a_unit_there_is_nothing_to_decide(self) -> None:
        self.assertIsNone(run_data_types({"files": [{"file_path": str(self.files[0])}]}))


class GateTests(_UnitCase):
    def blockers(self, manifest: Path, **values) -> list[str]:
        return [
            item for item in evaluate_repository_execution_gate(self.state(manifest, **values))["blockers"]
            if "data type" in item
        ]

    def test_a_run_set_against_headers_that_agree_is_refused(self) -> None:
        manifest = self.unit("Profile", "Profile")

        refused = self.blockers(manifest, ms1_data_type="Centroid", ms2_data_type="Profile")

        self.assertEqual(
            ["The workflow sets the MS1 data type to Centroid, but the raw header of every input that runs records Profile spectra."],
            refused,
        )
        self.assertEqual(2, len(self.blockers(manifest)), "a state that sets neither runs as Centroid")

    def test_a_run_that_follows_the_headers_passes(self) -> None:
        manifest = self.unit("Profile", "Profile")

        gate = evaluate_repository_execution_gate(self.state(manifest, ms1_data_type="Profile", ms2_data_type="profile"))

        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_headers_that_disagree_or_are_silent_refuse_nothing(self) -> None:
        for representations in (("Profile", "Centroid"), ("Profile", ""), ("", "")):
            with self.subTest(representations=representations):
                manifest = self.unit(*representations)
                self.assertEqual([], self.blockers(manifest, ms1_data_type="Centroid", ms2_data_type="Centroid"))
                self.assertEqual([], self.blockers(manifest, ms1_data_type="Profile", ms2_data_type="Profile"))


class PlanTests(_UnitCase):
    """build_guided_plan, which the campaign's and the MCP tool's runs both go through."""

    def setUp(self) -> None:
        super().setUp()
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(b"not really a console binary")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")

    def plan(self, manifest: Path | None, **overrides) -> dict:
        answers = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "existing",
            "libraries": {"lbm_path": str(self.lbm)},
            "run_qa": False,
            "generate_materials_methods": False,
            "console_path": str(self.console),
            "template_path": str(TEMPLATE),
            "output_root": str(self.root / "out"),
            "class_assignment_confirmed": True,
            "workflow_overrides": {**({"repository_run_manifest": str(manifest)} if manifest else {}), **overrides},
        }
        plan = build_guided_plan(str(self.data), answers)
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        return plan["workflow"]

    def test_a_unit_whose_headers_agree_runs_as_they_say_and_records_why(self) -> None:
        workflow = self.plan(self.unit("Profile", "Profile"))

        self.assertEqual(("Profile", "Profile"), (workflow["ms1_data_type"], workflow["ms2_data_type"]))
        self.assertEqual(("raw_header", "raw_header"), tuple(workflow["data_type_provenance"][f"{key}_basis"] for key in ("ms1_data_type", "ms2_data_type")))

        prepared = prepare_run(workflow)

        method = Path(prepared["method_file"]).read_text(encoding="utf-8-sig")
        self.assertIn("MS1 data type: Profile", method)
        self.assertIn("MS2 data type: Profile", method)
        recorded = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))["data_types"]
        self.assertEqual(("Profile", "raw_header"), (recorded["ms1_data_type"], recorded["ms1_data_type_basis"]))
        self.assertEqual("run_inputs", recorded["decision"]["scope"])
        settings = json.loads((Path(prepared["run_directory"]) / "workflow-settings.json").read_text(encoding="utf-8"))
        self.assertEqual("raw_header", settings["data_type_provenance"]["ms1_data_type_basis"])

    def test_a_unit_whose_headers_disagree_keeps_the_default_with_a_warning(self) -> None:
        workflow = self.plan(self.unit("Profile", "Centroid"))

        self.assertEqual("Centroid", workflow["ms1_data_type"])
        provenance = workflow["data_type_provenance"]
        self.assertEqual(("default", "inputs_disagree"), (provenance["ms1_data_type_basis"], provenance["levels"]["ms1"]["reason"]))
        self.assertEqual({"Centroid": 1, "Profile": 1}, provenance["levels"]["ms1"]["recorded"])

        recorded = prepare_run(workflow)["data_types"]
        self.assertEqual("default", recorded["ms1_data_type_basis"])
        self.assertTrue(recorded["warnings"])

    def test_an_explicit_override_stands_and_the_gate_judges_it(self) -> None:
        manifest = self.unit("Profile", "Profile")

        workflow = self.plan(manifest, ms1_data_type="Centroid")

        self.assertEqual(("Centroid", "workflow_override"), (workflow["ms1_data_type"], workflow["data_type_provenance"]["ms1_data_type_basis"]))
        self.assertEqual(("Profile", "raw_header"), (workflow["ms2_data_type"], workflow["data_type_provenance"]["ms2_data_type_basis"]))
        gate = evaluate_repository_execution_gate(workflow)
        self.assertTrue(any("MS1 data type to Centroid" in item for item in gate["blockers"]), gate["blockers"])

    def test_a_laboratory_analysis_is_unchanged(self) -> None:
        workflow = self.plan(None)

        self.assertEqual(("Centroid", "Centroid"), (workflow["ms1_data_type"], workflow["ms2_data_type"]))
        self.assertNotIn("data_type_provenance", workflow)
        recorded = prepare_run(workflow)["data_types"]
        self.assertEqual({"ms1_data_type": "Centroid", "ms1_data_type_basis": "unrecorded", "ms2_data_type": "Centroid", "ms2_data_type_basis": "unrecorded"}, recorded)


class RunAttemptRecordTests(_UnitCase):
    def test_the_attempt_carries_the_data_types_without_the_decision(self) -> None:
        manifest = self.unit("Profile", "Profile")
        record = {"ms1_data_type": "Profile", "ms1_data_type_basis": "raw_header", "warnings": [], "decision": {"levels": {}}}

        opened = record_run_start(manifest, "job-1", data_types=record)

        self.assertTrue(opened["recorded"])
        stored = json.loads(manifest.read_text(encoding="utf-8"))["run_attempts"][-1]["data_types"]
        self.assertEqual({"ms1_data_type": "Profile", "ms1_data_type_basis": "raw_header", "warnings": []}, stored)


if __name__ == "__main__":
    unittest.main()
