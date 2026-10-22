"""The laboratory LC-MS metabolomics agent: libraries, contract, plan, standards, normalization, merge.

A synthetic batch (lab_metabolomics_fixture) stands in for the raw folders, the libraries and a run's
exports. Nothing here runs MS-DIAL.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lab_metabolomics_fixture import (
    ALIGNMENT,
    GLUTAMINE,
    MetabolomicsBatch,
    STEMS,
    capabilities_patches,
    write_run,
)

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import lab_lcms_metabolomics as core
    from msdial_app import lab_lcms_metabolomics_agent as agent
    from msdial_app import mcp_server, server

from msdial_app.agent_workflow import _msp_row, build_guided_plan
from msdial_app.annotation_pipeline import tiered_lcms_annotation
from msdial_app.sharing import LOCAL_ONLY_FILE
from msdial_app.workflow import prepare_run


def tsv_rows(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.read_text(encoding="ascii").splitlines(), delimiter="\t"))


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


class AnnotationLibraries(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.files = {}
        for name in ("in_house", "predicted_rt", "no_rt", "insilico", "public"):
            path = self.root / f"{name}.msp"
            path.write_text("NAME: x\n", encoding="ascii")
            self.files[name] = str(path)
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"lbm")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_the_thresholds_are_the_guided_row_and_the_campaigns_two_tiers(self) -> None:
        row = _msp_row("x.msp", 1)
        self.assertEqual({key: row[key] for key in core.THRESHOLDS["standard"]}, core.THRESHOLDS["standard"])
        high, low = tiered_lcms_annotation("", "")["msp_annotators"]
        self.assertEqual({key: high[key] for key in core.THRESHOLDS["strict"]}, core.THRESHOLDS["strict"])
        self.assertEqual({key: low[key] for key in core.THRESHOLDS["mild"]}, core.THRESHOLDS["mild"])

    def test_the_laboratory_tiers_keep_their_own_retention_times_and_rank_above_the_standards(self) -> None:
        read = core.read_annotation({"preset": "lab_rt_tiers", "libraries": {
            "in_house": self.files["in_house"], "predicted_rt": self.files["predicted_rt"], "no_rt": self.files["no_rt"],
        }}, ["Positive"])
        self.assertEqual([], read["blockers"] + read["missing"])
        self.assertEqual(
            [("lib1_in_house", 4, "use", 0.5), ("lib2_predicted_rt", 3, "use", 2.0), ("lib3_no_rt", 2, "none", None)],
            [(entry["annotator_id"], entry["priority"], entry["retention_time"], entry["rt_tolerance"])
             for entry in read["entries"]],
        )
        self.assertGreater(min(entry["priority"] for entry in read["entries"]), core.INTERNAL_STANDARD_PRIORITY)

    def test_the_campaign_searches_one_msp_twice_and_the_in_silico_one_only_when_given(self) -> None:
        read = core.read_annotation({"preset": "campaign", "libraries": {"lbm": str(self.lbm), "msp": self.files["public"]}},
                                    ["Positive", "Negative"])
        self.assertEqual([], read["blockers"] + read["missing"])
        self.assertEqual(["lbm", "msp", "msp"], [entry["kind"] for entry in read["entries"]])
        self.assertEqual(["", "strict", "mild"], [entry.get("thresholds", "") for entry in read["entries"]])
        read = core.read_annotation({"preset": "campaign", "libraries": {
            "lbm": str(self.lbm), "msp": self.files["public"], "in_silico": self.files["insilico"]}}, ["Positive"])
        self.assertEqual(4, len(read["entries"]))

    def test_a_library_per_polarity_and_what_is_missing_or_wrong(self) -> None:
        read = core.read_annotation({"preset": "single_msp", "libraries": {"msp": {"Positive": self.files["public"]}}},
                                    ["Positive", "Negative"])
        self.assertEqual(["msp (negative)"], read["missing"])
        read = core.read_annotation({"preset": "lab_rt_tiers", "libraries": {"in_house": self.files["in_house"]}},
                                    ["Positive"])
        self.assertEqual(["predicted_rt (positive)", "no_rt (positive)"], read["missing"])
        read = core.read_annotation({"libraries": [{"kind": "msp", "path": self.files["public"], "retention_time": "use"}]},
                                    ["Positive"])
        self.assertTrue(any("needs rt_tolerance" in item for item in read["blockers"]))
        read = core.read_annotation({"libraries": [{"kind": "msp", "path": str(self.lbm), "retention_time": "none"}]},
                                    ["Positive"])
        self.assertTrue(any("is not an .msp library" in item for item in read["blockers"]))
        read = core.read_annotation({"preset": "everything"}, ["Positive"])
        self.assertTrue(any("is none of" in item for item in read["blockers"]))
        read = core.read_annotation({"libraries": [
            {"kind": "lbm", "path": str(self.lbm), "retention_time": "none"},
            {"kind": "lbm", "path": str(self.lbm), "retention_time": "none"},
        ]}, ["Positive"])
        self.assertTrue(any("one LBM2 per run" in item for item in read["blockers"]))
        read = core.read_annotation({"libraries": [{"kind": "msp", "path": self.files["public"], "retention_time": "none",
                                                    "label": "日本語"}]}, ["Positive"])
        self.assertTrue(any("printable ASCII" in item for item in read["blockers"]))

    def test_the_rows_reach_the_state_fixed_and_the_lbm2_with_its_priority(self) -> None:
        read = core.read_annotation({"preset": "campaign", "libraries": {"lbm": str(self.lbm), "msp": self.files["public"]}},
                                    ["Positive"])
        state: dict = {"msp_annotators": [], "text_annotators": []}
        core.apply_annotators(state, core.annotator_settings(read["entries"], "Positive")["annotators"])
        self.assertEqual(str(self.lbm.resolve()), state["lbm_path"])
        self.assertEqual(4, state["lbm_annotator"]["priority"])
        self.assertEqual([("lib2_msp_strict", 3, 0.05), ("lib3_msp_mild", 2, 0.25)],
                         [(row["annotator_id"], row["priority"], row["ms2_tolerance"]) for row in state["msp_annotators"]])
        self.assertTrue(all(row["rt_fixed"] and not row["use_rt_scoring"] for row in state["msp_annotators"]))
        self.assertTrue(all(item["license"] == "institutional/private" for item in state["library_provenance"]))


class PreferredAdducts(unittest.TestCase):
    def test_the_salt_chooses_the_negative_adduct_and_equivalent_names_count(self) -> None:
        self.assertEqual(["[M+H]+", "[M-H]-", "[M+HCOO]-"], core.preferred_adducts("HCOONH4"))
        self.assertEqual(["[M+H]+", "[M-H]-", "[M+CH3COO]-"], core.preferred_adducts("CH3COONH4"))
        self.assertEqual(["[M+H]+", "[M-H]-"], core.preferred_adducts("NH4HCO3"))
        formate = core.adduct_identities(core.preferred_adducts("HCOONH4"))
        self.assertTrue(core.is_preferred("[M+FA-H]-", formate))
        self.assertFalse(core.is_preferred("[M+Hac-H]-", formate))
        self.assertFalse(core.is_preferred("[M+Na]+", formate))

    def test_the_inchikey_first_block(self) -> None:
        self.assertEqual("ZDXPYRJPNDTMRX", core.inchikey_first_block(GLUTAMINE))
        self.assertEqual("ZDXPYRJPNDTMRX", core.inchikey_first_block("ZDXPYRJPNDTMRX"))
        for value in ("", "null", "InChIKey=ZDXP", "zdxpyrjpndtmrx-vkhmyheasa-n"):
            self.assertEqual("", core.inchikey_first_block(value))


class Batched(unittest.TestCase):
    polarities: tuple[str, ...] = ("Positive",)

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.batch = MetabolomicsBatch(Path(self.directory.name), self.polarities)
        self.patches = capabilities_patches()
        self.patches.__enter__()

    def tearDown(self) -> None:
        self.patches.__exit__(None, None, None)
        self.directory.cleanup()


class CheckAndContract(Batched):
    def test_the_folders_alone_list_what_is_there_and_ask_everything(self) -> None:
        check = agent.check_inputs({"pos_path": str(self.batch.folders["Positive"])})
        asked = {item["id"] for item in check["questions"]}
        self.assertEqual({"analysis_purpose", "annotation", "solvent_type", "is_text_pos", "normalizing_standard_pos",
                          "analysis_root", "raw_location", "console_path"}, asked)
        self.assertEqual(["Positive"], check["polarities"])
        self.assertEqual(4, check["folders"]["Positive"]["file_count"])
        self.assertFalse(check["ready_for_contract"])

    def test_formic_acid_is_formate_and_the_normalizing_standard_must_be_listed(self) -> None:
        check = agent.check_inputs(self.batch.request())
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual("HCOONH4", check["salt"]["salt"])
        self.assertEqual(["[M+H]+", "[M-H]-", "[M+HCOO]-"], check["salt"]["preferred_adducts"])
        self.assertEqual("Methionine-d3", check["normalizing_standards"]["Positive"]["name"])
        check = agent.check_inputs(self.batch.request(normalizing_standard_pos="Tyrosine-d4"))
        self.assertTrue(any("is not in the positive internal-standard library" in item for item in check["blockers"]))

    def test_the_contract_binds_what_was_checked_and_fixes_the_sample_table(self) -> None:
        check = agent.check_inputs(self.batch.request())
        refused = agent.write_contract({**self.batch.request(solvent_type="ammonium acetate"),
                                        "check_digest": check["check_digest"], "sample_table_confirmed": True})
        self.assertIn("changed after it was checked", refused["error"])
        contract_path = self.batch.contract()
        contract = agent.load_contract(contract_path)
        self.assertEqual(["Positive"], contract["polarities"])
        self.assertEqual("local_only", contract["sharing"])
        self.assertEqual({"S1": "Case", "S2": "Control", "QC1": "QC", "Blank": "Blank"},
                         {row["stem"]: row["class_id"] for row in contract["sample_table"]["rows"]})
        self.assertEqual(["lib1_in_house", "lib2_predicted_rt", "lib3_no_rt"],
                         [entry["annotator_id"] for entry in contract["annotation"]["libraries"]])
        self.assertTrue((self.batch.analysis / "inputs" / "is_pos.txt").is_file())
        self.assertFalse((self.batch.analysis / "inputs" / "is_neg.txt").exists())
        # A library that changes afterwards stops every plan.
        self.batch.libraries["in_house"].write_text("NAME: changed\n", encoding="ascii")
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.load_contract(contract_path)

    def test_the_contract_and_its_record_are_local_only(self) -> None:
        for name in ("lab-lcms-metabolomics-contract.json", "lab-lcms-metabolomics-contract.20261022120000.json",
                     "lab-lcms-metabolomics-record.jsonl"):
            self.assertIsNotNone(LOCAL_ONLY_FILE.search(name), name)


class OnePolarity(Batched):
    def test_a_positive_batch_is_merged_on_its_own(self) -> None:
        contract = self.batch.contract()
        job = write_run(self.batch, contract, "Positive")
        resolved = agent.resolve_standards_for_contract(contract, {"Positive": job})
        plan = agent.plan_normalize(contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertTrue(agent.finish_normalize(plan)["verified"])
        report = agent.merge(contract, write=True)
        self.assertEqual(["Positive"], report["polarities"])
        self.assertEqual({"within_polarity": 1, "across_polarities": 0}, report["counts"]["duplicates_removed"])
        kept = list(csv.DictReader(io.StringIO(Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8"))))
        self.assertEqual(["10", "14", "15"], [row["Alignment ID"] for row in kept])
        self.assertIn("S1_Pos", kept[0], "one polarity keeps its sample names as exported")
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.resolve_standards_for_contract(contract, {"Negative": None})


class PlanFromTheContract(Batched):
    def setUp(self) -> None:
        super().setUp()
        self.contract = self.batch.contract()

    def plan(self, **extra) -> dict:
        answers = {agent.CONTRACT_KEY: self.contract, "ion_mode": "Positive", "minimum_peak_height": 500, **extra}
        return build_guided_plan(str(self.batch.folders["Positive"]), {k: v for k, v in answers.items() if v is not None})

    def test_each_library_is_searched_with_its_own_retention_time_and_the_standards_at_priority_1(self) -> None:
        plan = self.plan()
        self.assertTrue(plan["ready_to_prepare"], (plan["blockers"], plan["remaining_questions"]))
        self.assertEqual("lab_lcms_metabolomics", plan["profile"])
        self.assertEqual([], plan["lab_lcms_metabolomics"]["deviations"])
        prepared = prepare_run(plan["workflow"])
        run = Path(prepared["run_directory"])
        msp = tsv_rows(run / "msp_annotator_settings.tsv")
        self.assertEqual(
            [("lib1_in_house", "4", "True", "True", "0.5"), ("lib2_predicted_rt", "3", "True", "True", "2.0"),
             ("lib3_no_rt", "2", "False", "False")],
            [(row["annotator_id"], row["priority"], row["use_retention_information_for_scoring"],
              row["use_retention_information_for_filtering"], *([row["rt_tolerance"]] if row["annotator_id"] != "lib3_no_rt" else []))
             for row in msp],
        )
        self.assertEqual({"Metabolomics"}, {row["target_omics"] for row in msp})
        text = tsv_rows(run / "text_annotator_settings.tsv")
        self.assertEqual([("internal_standards_pos", "1", "True")],
                         [(row["annotator_id"], row["priority"], row["use_retention_information_for_filtering"]) for row in text])
        lines = method_lines(prepared["method_file"])
        self.assertEqual("Metabolomics", lines["Target omics"])
        self.assertEqual("HCOONH4", lines["Solvent type"])
        self.assertEqual("", lines["Lbm file path"])
        self.assertEqual("True", lines["Export annotation candidates"])

    def test_what_the_contract_fixes_cannot_be_answered_otherwise(self) -> None:
        plan = self.plan(libraries={"annotators": []})
        self.assertTrue(any("libraries is fixed by the contract" in item for item in plan["blockers"]))
        plan = self.plan(ion_mode="Negative")
        self.assertTrue(any("Name the ion mode" in item for item in plan["blockers"]))
        plan = self.plan(workflow_overrides={"msp_annotators": []})
        self.assertTrue(any("workflow_overrides.msp_annotators would replace" in item for item in plan["blockers"]))
        plan = self.plan(use_retention_time_for_annotation=True, retention_time_tolerance=1.0)
        self.assertTrue(any("use_retention_time_for_annotation is fixed" in item for item in plan["blockers"]))


class StandardsNormalizeAndMerge(Batched):
    polarities = ("Positive", "Negative")

    def setUp(self) -> None:
        super().setUp()
        self.contract = self.batch.contract()
        self.jobs = {polarity: write_run(self.batch, self.contract, polarity) for polarity in self.polarities}

    def normalize(self, polarity: str, **decisions) -> dict:
        resolved = agent.resolve_standards_for_contract(self.contract, {polarity: self.jobs[polarity]})
        digest = resolved["polarities"][polarity]["resolution_digest"]
        plan = agent.plan_normalize(self.contract, self.jobs[polarity], digest, **decisions)
        return agent.finish_normalize(plan)

    def test_the_normalizing_standard_is_found_by_its_text_row(self) -> None:
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": self.jobs["Positive"]})
        summary = resolved["polarities"]["Positive"]["summary"]
        self.assertEqual(("B", "1", False), (summary["normalizing_standard"]["tier"],
                                             summary["normalizing_standard"]["alignment_id"],
                                             summary["normalizing_standard"]["needs_confirmation"]))
        # Met-d3 is in no Blank, which a Blank may lack.
        self.assertEqual(["Blank_Pos"], summary["normalizing_standard"]["tolerated_zero_height_files"])
        cuda = next(item for item in summary["standards"] if item["name"] == "CUDA")
        self.assertIsNone(cuda["cv_percent_qc"], "one QC injection has no CV")
        self.assertIsNotNone(cuda["cv_percent_sample_qc"])

    def test_normalization_divides_by_the_standard_and_scales_by_its_sample_and_qc_mean(self) -> None:
        record = self.normalize("Positive")
        self.assertTrue(record["verified"], record["verification"])
        self.assertEqual(1500.0, record["standard_mean"])  # (1000 + 2000 + 1500) / 3; the Blank is not counted
        self.assertEqual(["Blank_Pos"], record["raw_height_kept"])
        export = core._rows_with_cells(record["outputs"]["normalized"])
        start = export["first_sample_column"]
        row = next(item for item in export["rows"] if item[0] == "10")
        self.assertEqual([4000 / 1000 * 1500, 8000 / 2000 * 1500, 6000 / 1500 * 1500, 10.0],
                         [float(cell) for cell in row[start:start + 4]])
        self.assertTrue(all(cell == "" for cell in row[start + 4:]), "the class statistics are left empty")
        self.assertTrue((self.batch.analysis / "quant" / "NormalizedHeight_Pos.txt").is_file())

    def test_a_sample_without_the_standard_refuses_and_a_listed_spot_may_be_taken(self) -> None:
        spots = [dict(item) for item in __import__("lab_metabolomics_fixture").POSITIVE_SPOTS]
        spots[0]["cells"] = [1000, 0, 1500, 0]
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        digest = resolved["polarities"]["Positive"]["resolution_digest"]
        self.assertEqual("invalid", resolved["polarities"]["Positive"]["summary"]["normalizing_standard"]["status"])
        with self.assertRaises(agent.LabMetabolomicsError) as raised:
            agent.plan_normalize(self.contract, job, digest)
        self.assertIn("is invalid", str(raised.exception))
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.plan_normalize(self.contract, job, "another digest")

    def test_a_dilution_factor_multiplies_the_injection(self) -> None:
        contract = agent.load_contract(self.contract)
        rows = contract["sample_table"]["rows"]
        for row in rows:
            if row["stem"] == "S1":
                row["factor"] = 2.0
        Path(self.contract).write_text(json.dumps({k: v for k, v in contract.items() if not k.startswith("_")}),
                                       encoding="utf-8")
        job = write_run(self.batch, self.contract, "Positive")
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertEqual({"S1_Pos": 2.0}, plan["dilution_factors"])
        self.assertEqual(4000 / 1000 * 1500 * 2, plan["_normalized"]["values"]["10"][0])

    def test_the_written_matrix_is_checked_cell_by_cell(self) -> None:
        record = self.normalize("Positive")
        path = Path(record["outputs"]["normalized"])
        normalized = core.normalize_heights(record["height_export"], "1", {})
        self.assertTrue(core.verify_normalized(path, normalized)["passed"])
        path.write_bytes(core.normalized_text(normalized).replace("\t1500\t", "\t1501\t", 1).encode("utf-8"))
        self.assertFalse(core.verify_normalized(path, normalized)["passed"])

    def test_the_merge_keeps_one_row_per_compound_and_says_why_each_other_went(self) -> None:
        self.normalize("Positive")
        self.normalize("Negative")
        preview = agent.merge(self.contract)
        self.assertFalse(Path(preview["outputs"]["metabolome"]["path"]).exists(), "a preview writes nothing")
        report = agent.merge(self.contract, write=True)
        kept = list(csv.DictReader(io.StringIO(Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8"))))
        by_id = {(row["Polarity"], row["Alignment ID"]): row for row in kept}
        # Glutamine: negative [M-H]- is the larger of the preferred adducts; positive [M+Na]+ is not preferred.
        self.assertIn(("Negative", "110"), by_id)
        self.assertNotIn(("Positive", "10"), by_id)
        self.assertNotIn(("Positive", "11"), by_id)
        # Tryptophan: both adducts are preferred ([M+FA-H]- is formate); positive is the larger.
        self.assertIn(("Positive", "15"), by_id)
        self.assertNotIn(("Negative", "111"), by_id)
        self.assertIn(("Positive", "14"), by_id, "a row without an InChIKey is kept as its own")
        self.assertEqual("", by_id[("Positive", "14")]["InChIKey first block"])
        self.assertIn(("Negative", "112"), by_id)
        self.assertEqual("in-house RT and MS/MS", by_id[("Negative", "110")]["Library"])
        self.assertEqual(["S1", "S2", "QC1", "Blank"], [name for name in kept[0] if name in {"S1", "S2", "QC1", "Blank"}])
        removed = {(row["Polarity"], row["Alignment ID"]): row["Reason"] for row in csv.DictReader(
            io.StringIO(Path(report["outputs"]["removed"]["path"]).read_text(encoding="utf-8")), delimiter="\t")}
        self.assertEqual("internal standard", removed[("Positive", "1")])
        self.assertEqual("internal standard", removed[("Negative", "101")])
        self.assertEqual("unannotated", removed[("Positive", "12")])
        self.assertEqual("not reference matched", removed[("Positive", "13")])
        self.assertEqual("marked removable (Comment starts with x)", removed[("Negative", "113")])
        self.assertEqual("duplicate InChIKey first block", removed[("Positive", "10")])
        # Counted against the row kept: both positive glutamine rows lost to the negative one.
        self.assertEqual({"within_polarity": 0, "across_polarities": 3}, report["counts"]["duplicates_removed"])
        self.assertTrue(Path(report["merge_report"]).is_file())

    def test_the_merge_options(self) -> None:
        self.normalize("Positive")
        self.normalize("Negative")
        report = agent.merge(self.contract, reference_matched_only=False, preferred_adducts_only=True,
                             output_folder="metabolome_all_matches", write=True)
        kept = list(csv.DictReader(io.StringIO(Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8"))))
        names = {(row["Polarity"], row["Alignment ID"]) for row in kept}
        self.assertIn(("Positive", "13"), names, "a suggestion is kept when reference_matched_only is off")
        self.assertTrue(all(row["Preferred adduct"] == "TRUE" for row in kept))
        self.assertIn("metabolome_all_matches", report["outputs"]["metabolome"]["path"])
        for name in ("pos", "qa_figures", "../x"):
            with self.assertRaises(agent.LabMetabolomicsError):
                agent.merge(self.contract, output_folder=name)
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.merge(self.contract, reference_matched_only="false")

    def test_a_polarity_not_normalized_refuses_the_merge(self) -> None:
        self.normalize("Positive")
        with self.assertRaises(agent.LabMetabolomicsError) as raised:
            agent.merge(self.contract)
        self.assertIn("negative polarity has not been normalized", str(raised.exception))

    def test_the_endpoint_previews_then_writes_and_draws_the_standards(self) -> None:
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": self.jobs["Positive"]})
        digest = resolved["polarities"]["Positive"]["resolution_digest"]
        with patch.dict(server.JOBS, {"run-pos": self.jobs["Positive"]}):
            body = {"contract_path": self.contract, "job_id": "run-pos", "resolution_digest": digest}
            preview = server._laboratory_metabolomics_normalize(body)
            self.assertFalse(preview["written"])
            self.assertNotIn("_normalized", preview["plan"])
            self.assertFalse((self.batch.analysis / "quant" / "NormalizedHeight_Pos.txt").exists())
            written = server._laboratory_metabolomics_normalize({**body, "confirmed": True})
        self.assertTrue(written["record"]["verified"])
        self.assertIn("qa_figures", written)


class TheTools(unittest.TestCase):
    def capture(self, function, **arguments) -> dict:
        captured = {}

        def request(method, path, **options):
            captured.update(path=path, body=options.get("body"))
            return {"ok": True}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            asyncio.run(mcp_server.mcp.call_tool(function.__name__, arguments))
        return captured

    def refused(self, function, **arguments) -> None:
        with patch.object(mcp_server, "_request_json", side_effect=AssertionError("the backend was called")):
            with self.assertRaises(Exception) as raised:
                asyncio.run(mcp_server.mcp.call_tool(function.__name__, arguments))
        self.assertIn("validation error", str(raised.exception))

    def test_every_gate_is_a_json_true(self) -> None:
        for function, arguments, path in (
            (mcp_server.msdial_lcms_metabolomics_normalize, {"contract_path": "c", "job_id": "j", "resolution_digest": "d"},
             "/api/lcms-metabolomics/normalize"),
            (mcp_server.msdial_lcms_metabolomics_merge, {"contract_path": "c"}, "/api/lcms-metabolomics/merge"),
            (mcp_server.msdial_lcms_metabolomics_write_contract,
             {"annotation": {"preset": "single_msp"}, "solvent_type": "s", "analysis_root": "r", "analysis_purpose": "x",
              "console_path": "c", "raw_location": "copy", "check_digest": "d", "pos_path": "p"},
             "/api/lcms-metabolomics/write-contract"),
        ):
            with self.subTest(tool=function.__name__):
                self.assertEqual(path, self.capture(function, **arguments)["path"])
                self.assertIs(False, self.capture(function, **arguments)["body"]["confirmed"])
                self.assertIs(True, self.capture(function, **arguments, confirmed=True)["body"]["confirmed"])
                for value in ("yes", "true", 1):
                    self.refused(function, **arguments, confirmed=value)

    def test_the_merge_options_are_left_out_unless_given(self) -> None:
        body = self.capture(mcp_server.msdial_lcms_metabolomics_merge, contract_path="c")["body"]
        self.assertNotIn("reference_matched_only", body)
        self.assertNotIn("preferred_adducts_only", body)
        body = self.capture(mcp_server.msdial_lcms_metabolomics_merge, contract_path="c", reference_matched_only=False,
                            preferred_adducts_only=True)["body"]
        self.assertIs(False, body["reference_matched_only"])
        self.assertIs(True, body["preferred_adducts_only"])
        self.refused(mcp_server.msdial_lcms_metabolomics_merge, contract_path="c", reference_matched_only="false")


if __name__ == "__main__":
    unittest.main()
