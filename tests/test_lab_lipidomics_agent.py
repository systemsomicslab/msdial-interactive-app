"""The laboratory lipidomics agent's steps: check, contract, resolve, normalize, merge.

A synthetic batch (lab_batch_fixture) stands in for the raw folders, the libraries and the workbook;
a production run's outputs and normalize's are written by the fixture, and the Console is a small
Python script that copies them and prints normalize's report. Nothing here runs MS-DIAL.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from lab_batch_fixture import (
    ALIGNMENT,
    STEMS_WITH_QC,
    UNIT,
    Batch,
    capabilities_patches,
    guided_answers,
    normalize_outputs,
    write_run,
)
from lab_export_fixture import LBM
from xlsx_fixture import LOOKUP, NEGATIVE_LIBRARY, POSITIVE_LIBRARY, QUANT_INFO, lookup_sheet, quant_sheet, quant_workbook

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import lab_lipidomics_agent, mcp_server, server
    from msdial_app.lab_lipidomics_agent import (
        LabContractError,
        check_inputs,
        finish_normalize,
        load_contract,
        merge_lipidome,
        plan_normalize,
        public_check,
        public_normalize_plan,
        resolve_standards_for_contract,
        write_contract,
        write_normalize_inputs,
    )
    from msdial_app.sharing import bundle_member_allowed
    from msdial_app.worksets import LAB_LIPIDOMICS_WORKSET_KIND, get_workset, save_lab_lipidomics_workset


# The fixture's sheet 3 as the merge's reconciliation needs it: BMP and EtherPE from negative [M-H]- only.
RECONCILED_QUANT_INFO = [
    (klass, adduct, mode, False if (klass, adduct) == ("EtherPE", "[M+H]+") else selected)
    for klass, adduct, mode, selected in QUANT_INFO
] + [("PG", "[M-H]-", "Negative", True), ("BMP", "[M-H]-", "Negative", True), ("BMP", "[M+NH4]+", "Positive", False)]


class _Batch(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.batch = Batch(Path(self.directory.name))
        self.patches = capabilities_patches()
        self.patches.__enter__()

    def tearDown(self) -> None:
        self.patches.__exit__(None, None, None)
        self.directory.cleanup()


class CheckingTheInputs(_Batch):
    def test_with_only_the_folders_it_lists_what_is_there_and_asks_the_rest(self) -> None:
        (self.batch.workbook.parent / "~$quant_utility_table.xlsx").write_bytes(b"lock")
        (self.batch.pos / "S1_Pos.dcl").write_bytes(b"intermediate")
        result = public_check(check_inputs({"pos_path": str(self.batch.pos), "neg_path": str(self.batch.neg)}))
        self.assertEqual(
            ["analysis_purpose", "lbm_path", "is_text_pos/is_text_neg", "quant_table_path", "amount_column",
             "solvent_type", "retention_time_tolerance", "analysis_root", "raw_location", "console_path"],
            [item["id"] for item in result["questions"]],
        )
        self.assertEqual(3, result["folders"]["Positive"]["file_count"])
        self.assertEqual(1, result["folders"]["Positive"]["existing_intermediates"])
        found = result["candidate_inputs"]
        self.assertEqual(
            {self.batch.is_pos.name: (4, 0), self.batch.is_neg.name: (0, 5)},
            {Path(item["path"]).name: (item["positive"], item["negative"]) for item in found["internal_standard_texts"]},
        )
        self.assertEqual(["~$quant_utility_table.xlsx"], [Path(item["path"]).name for item in found["excel_lock_files"]])
        self.assertFalse(result["ready_for_contract"])
        self.assertNotIn("_parsed", result)

    def test_with_every_answer_it_is_ready_and_says_what_cannot_be_quantified(self) -> None:
        result = public_check(check_inputs(self.batch.request()))
        self.assertTrue(result["ready_for_contract"], (result["questions"], result["blockers"]))
        self.assertEqual({"salt": "CH3COONH4", "answer": "酢酸アンモニウム", "matched": "酢酸アンモニウム", "problem": ""},
                         result["salt"])
        self.assertIn("CH3COONH4", result["salt_evidence"]["salts_consistent_with_evidence"])
        negative = result["expected_resolution"]["by_polarity"]["Negative"]
        self.assertEqual(["AHexCS"], negative["not_quantified"])
        self.assertEqual(["CE 18:1(d7)"], negative["unresolved_expected"])
        self.assertEqual(["FA 18:0(d3)"], result["expected_resolution"]["by_polarity"]["Positive"]["unresolved_expected"])
        self.assertEqual("pmol_per_microL_plasma", result["quant_table"]["unit"])
        self.assertEqual(10.0, result["quant_table"]["sample_parameter"]["value"])
        self.assertEqual(
            [("S1", "S1_Pos", "S1_Neg"), ("S2", "S2_Pos", "S2_Neg"), ("Blank", "Blank_Pos", "Blank_Neg")],
            sorted(((row["stem"], row["positive_file"], row["negative_file"]) for row in result["paired_sample_table"]["rows"]),
                   key=lambda item: ["S1", "S2", "Blank"].index(item[0])),
        )
        self.assertTrue(result["paired_sample_table"]["matched"])
        self.assertEqual(64, len(result["check_digest"]))

    def test_a_sheet_three_that_contradicts_the_merges_reconciliation_is_asked_about_and_not_changed(self) -> None:
        # The fixture's sheet 3 takes EtherPE from both polarities and selects no BMP: both are the person's to
        # change before the contract, or to keep, explicitly. After the contract the workbook is bound.
        before = self.batch.workbook.read_bytes()
        result = public_check(check_inputs(self.batch.request(merge_selection_accepted=None)))
        selection = result["merge_selection_warnings"]
        self.assertEqual(2, len(selection))
        self.assertIn("does not select BMP [M-H]-", selection[0])
        self.assertIn("in neither BMP nor PG", selection[0])
        self.assertIn("selects EtherPE [M+H]+ in positive mode", selection[1])
        self.assertTrue(set(selection) <= set(result["warnings"]))
        self.assertEqual([], result["blockers"])
        self.assertEqual(["merge_selection_accepted"], [item["id"] for item in result["questions"]])
        question = result["questions"][0]["prompt"]
        self.assertIn("new contract and new runs of both polarities", question)
        self.assertTrue(all(item in question for item in selection))
        self.assertFalse(result["ready_for_contract"])
        self.assertFalse(result["merge_selection_accepted"])
        self.assertEqual(0.1, result["stated_defaults"]["merge_method"]["polarity_reconciliation"]["rt_tolerance_min"])
        # Kept as it is: the question is answered, and the answer is bound by the digest.
        kept = public_check(check_inputs(self.batch.request()))
        self.assertTrue(kept["ready_for_contract"], kept["questions"])
        self.assertTrue(kept["merge_selection_accepted"])
        self.assertNotEqual(result["check_digest"], kept["check_digest"])
        self.assertEqual(before, self.batch.workbook.read_bytes())

    def test_a_sheet_three_changed_as_the_check_says_asks_nothing(self) -> None:
        # The advice followed: BMP and EtherPE positive FALSE, their [M-H]- TRUE, saved, checked again.
        quant_workbook(self.batch.workbook, quant=quant_sheet(RECONCILED_QUANT_INFO))
        result = public_check(check_inputs(self.batch.request(merge_selection_accepted=None)))
        self.assertEqual([], result["merge_selection_warnings"])
        self.assertTrue(result["ready_for_contract"], result["questions"])
        self.assertFalse(result["merge_selection_accepted"])
        # Accepting a selection that contradicts nothing is no answer, and changes nothing.
        self.assertEqual(result["check_digest"], public_check(check_inputs(self.batch.request()))["check_digest"])

    def test_the_folders_must_be_two_and_each_its_own_polarity(self) -> None:
        same = public_check(check_inputs(self.batch.request(neg_path=str(self.batch.pos))))
        self.assertTrue(any("same folder" in item for item in same["blockers"]))
        self.assertTrue(any("file names say positive" in item for item in same["blockers"]))

    def test_a_salt_that_would_drop_a_selected_class_blocks(self) -> None:
        result = public_check(check_inputs(self.batch.request(solvent_type="ammonium formate")))
        self.assertTrue(any("removes the negative-mode LBM records sheet 3 selects" in item for item in result["blockers"]))
        self.assertTrue(any("only the internal-standard text library can" in item for item in result["warnings"]))

    def test_an_analysis_root_inside_a_raw_folder_or_a_checkout_blocks(self) -> None:
        inside = public_check(check_inputs(self.batch.request(analysis_root=str(self.batch.pos / "analysis"))))
        self.assertTrue(any("lies inside the positive raw-data folder" in item for item in inside["blockers"]))

    def test_a_root_whose_pos_and_neg_are_the_raw_folders_blocks(self) -> None:
        # data\pos and data\neg are the raw folders; data as the root would make them the runs' output folders.
        result = public_check(check_inputs(self.batch.request(analysis_root=str(self.batch.pos.parent))))
        self.assertFalse(result["ready_for_contract"])
        for polarity in ("positive", "negative"):
            self.assertTrue(any(f"output folder" in item and f"the {polarity} raw-data folder itself" in item
                                for item in result["blockers"]), result["blockers"])
        # A root that only holds the raw folders, under other names, is fine.
        holder = self.batch.root / "batch"
        (holder / "raw").mkdir(parents=True)
        shutil.copytree(self.batch.pos, holder / "raw" / "pos")
        shutil.copytree(self.batch.neg, holder / "raw" / "neg")
        fine = public_check(check_inputs(self.batch.request(
            pos_path=str(holder / "raw" / "pos"), neg_path=str(holder / "raw" / "neg"), analysis_root=str(holder))))
        self.assertTrue(fine["ready_for_contract"], fine["blockers"])

    def test_where_ms_dial_reads_the_raw_data_is_asked_and_must_be_an_answer(self) -> None:
        asked = public_check(check_inputs(self.batch.request(raw_location="")))
        question = next(item for item in asked["questions"] if item["id"] == "raw_location")
        self.assertIn("writes its per-file .dcl, .pai2", question["prompt"])
        self.assertEqual(["in_place", "copy"], question["choices"])
        self.assertFalse(asked["ready_for_contract"])
        wrong = public_check(check_inputs(self.batch.request(raw_location="somewhere")))
        self.assertTrue(any("raw_location 'somewhere'" in item for item in wrong["blockers"]))
        self.assertEqual("in_place", public_check(check_inputs(self.batch.request(raw_location="In_Place")))[
            "raw_location"]["answer"])

    def test_the_digest_binds_the_raw_folders_by_where_they_are(self) -> None:
        # A copy of the batch: the same file names and sizes in other folders.
        copy = self.batch.root / "copy"
        shutil.copytree(self.batch.pos, copy / "pos")
        shutil.copytree(self.batch.neg, copy / "neg")
        original = check_inputs(self.batch.request())["check_digest"]
        copied = check_inputs(self.batch.request(pos_path=str(copy / "pos"), neg_path=str(copy / "neg")))["check_digest"]
        self.assertNotEqual(original, copied)

    def test_a_wiff_without_its_scan_file_blocks(self) -> None:
        # The demo batch is SCIEX: a copy that left a .wiff.scan behind would fail only once the Console starts.
        folders = {}
        for label in ("Pos", "Neg"):
            folder = self.batch.root / "wiff" / label.lower()
            folder.mkdir(parents=True)
            for stem in ("S1", "S2"):
                (folder / f"{stem}_{label}.wiff").write_bytes(b"wiff")
                (folder / f"{stem}_{label}.wiff.scan").write_bytes(b"scan")
            folders[label] = folder
        request = {"pos_path": str(folders["Pos"]), "neg_path": str(folders["Neg"])}
        self.assertFalse(any(".wiff.scan" in item for item in check_inputs(request)["blockers"]))
        (folders["Pos"] / "S2_Pos.wiff.scan").unlink()
        blockers = check_inputs(request)["blockers"]
        self.assertIn("S2_Pos.wiff has no S2_Pos.wiff.scan beside it; MS-DIAL cannot read it.", blockers)
        self.assertEqual(1, sum(".wiff.scan" in item for item in blockers))

    def test_a_console_without_what_the_profile_needs_blocks(self) -> None:
        with patch.object(lab_lipidomics_agent, "console_capabilities",
                          return_value={"capabilities": ["automatic_alignment_rt_correction_local_support"]}):
            result = public_check(check_inputs(self.batch.request()))
        self.assertTrue(any("--peak-ids-only" in item and "annotation candidate" in item for item in result["blockers"]))


class WritingTheContract(_Batch):
    def checked(self, **extra) -> dict:
        return check_inputs(self.batch.request(**extra))

    def write(self, **extra) -> dict:
        check = self.checked()
        body = {**self.batch.request(), "check_digest": check["check_digest"], "sample_table_confirmed": True,
                "confirmed": True}
        body.update(extra)
        return write_contract(body)

    def test_a_preview_writes_nothing(self) -> None:
        result = self.write(confirmed=False)
        self.assertFalse(result["written"])
        self.assertTrue(result["confirmation_required"])
        self.assertFalse((self.batch.analysis / "inputs").exists())
        self.assertIn((self.batch.analysis / "inputs" / "lab-lipidomics-contract.json").resolve(),
                      [Path(item).resolve() for item in result["would_write"]])

    def test_the_sample_table_must_be_the_persons_and_the_digest_the_one_they_saw(self) -> None:
        self.assertIn("sample table is the person's", self.write(sample_table_confirmed=False)["error"])
        self.assertIn("Pass the check_digest", self.write(check_digest="")["error"])
        check = self.checked()
        self.batch.is_neg.write_bytes(self.batch.is_neg.read_bytes().replace(b"9.5\t[M+CH3COO]", b"9.6\t[M+CH3COO]"))
        refused = write_contract({**self.batch.request(), "check_digest": check["check_digest"],
                                  "sample_table_confirmed": True, "confirmed": True})
        self.assertFalse(refused["written"])
        self.assertIn("changed after it was checked", refused["error"])

    def test_the_preview_names_the_raw_folders_and_another_folder_pair_needs_its_own_check(self) -> None:
        preview = self.write(confirmed=False)
        self.assertEqual({"Positive": str(self.batch.pos.resolve()), "Negative": str(self.batch.neg.resolve())},
                         preview["raw_folders"])
        self.assertEqual("copy", preview["raw_location"]["answer"])
        copy = self.batch.root / "copy"
        shutil.copytree(self.batch.pos, copy / "pos")
        shutil.copytree(self.batch.neg, copy / "neg")
        refused = self.write(pos_path=str(copy / "pos"), neg_path=str(copy / "neg"))
        self.assertFalse(refused["written"])
        self.assertIn("changed after it was checked", refused["error"])

    def test_one_library_holding_both_polarities_runs_through_to_the_resolution(self) -> None:
        both = self.batch.root / "data" / "is_both.txt"
        both.write_bytes((POSITIVE_LIBRARY + "".join(NEGATIVE_LIBRARY.splitlines(keepends=True)[1:])).encode("ascii"))
        check = public_check(self.checked(is_text_pos=str(both), is_text_neg=str(both)))
        self.assertTrue(check["ready_for_contract"], check["blockers"])
        self.assertTrue(check["internal_standard_libraries"]["Positive"]["shared_by_polarities"])
        contract = self.batch.contract(is_text_pos=str(both), is_text_neg=str(both))
        derived = load_contract(contract)["derived"]["internal_standard_libraries"]
        self.assertEqual((4, 5), (derived["Positive"]["records"], derived["Negative"]["records"]))
        runs = {polarity: write_run(self.batch, contract, polarity) for polarity in ("Positive", "Negative")}
        resolved = resolve_standards_for_contract(contract, runs)["polarities"]
        self.assertEqual(([], []), (resolved["Positive"]["problems"], resolved["Negative"]["problems"]))
        negative = {item["name"]: item["tier"] for item in resolved["Negative"]["summary"]["standards"]}
        self.assertEqual("B", negative["PC 15:0_18:1(d7)"])

    def test_corrections_must_name_every_sample(self) -> None:
        result = self.write(sample_table=[{"stem": "S1", "class_id": "SPF"}])
        self.assertIn("no row for", result["error"])
        result = self.write(sample_table=[{"stem": "S9"}])
        self.assertIn("S9", result["error"])

    def test_the_contract_and_its_derived_files_are_written(self) -> None:
        path = Path(self.batch.contract())
        contract = load_contract(path)
        self.assertEqual("msdial-interactive.lab-lipidomics-contract.v1", contract["schema"])
        self.assertEqual("local_only", contract["sharing"])
        self.assertEqual("CH3COONH4", contract["salt"]["salt"])
        self.assertEqual((1.5, 0.5), (contract["retention_time_tolerance"], contract["internal_standard_rt_tolerance"]))
        self.assertEqual("H", contract["amount_column"])
        self.assertEqual(10, len(contract["lookup"]))
        rows = {row["stem"]: row for row in contract["sample_table"]["rows"]}
        self.assertEqual(("SPF", "GF", "Blank"), (rows["S1"]["class_id"], rows["S2"]["class_id"], rows["Blank"]["class_id"]))
        self.assertTrue(contract["sample_table"]["corrected"])
        inputs = path.parent
        canonical = (inputs / "is_pos.txt").read_text(encoding="ascii").splitlines()
        self.assertEqual("Name\tMZ\tRT\tAdduct\tInChIKey\tFormula\tSMILES\tOntology\tCCS", canonical[0])
        self.assertEqual("StandardName\tTargetClass\tConcentration", (inputs / "standards_template.tsv").read_text().splitlines()[0])
        self.assertIn("LipidA\t[M-H]-\tNegative\tFALSE", (inputs / "quant_rules.tsv").read_text())
        events = [json.loads(line) for line in (inputs / "lab-lipidomics-record.jsonl").read_text().splitlines()]
        self.assertEqual(["contract_written"], [event["event"] for event in events])
        self.assertEqual("copy", events[0]["raw_location"])
        self.assertEqual("copy", contract["raw_location"]["answer"])
        self.assertIn("beside the raw files it reads", contract["raw_location"]["writes_beside_raw"])

    def test_an_existing_contract_is_replaced_only_when_asked_and_kept(self) -> None:
        self.batch.contract()
        again = self.write()
        self.assertIn("already exists", again["error"])
        replaced = self.write(replace_existing=True)
        self.assertTrue(replaced["written"])
        self.assertEqual(1, len(list((self.batch.analysis / "inputs").glob("lab-lipidomics-contract.2*.json"))))

    def test_a_contract_already_there_is_shown_before_anything_is_confirmed(self) -> None:
        path = self.batch.contract()
        lab_lipidomics_agent.record_event(path, {"event": "production_run_started", "polarity": "Negative",
                                                 "job_id": "run-neg"})
        refused = self.write(confirmed=False)
        self.assertFalse(refused["written"])
        self.assertNotIn("confirmation_required", refused)
        existing = refused["existing_contract"]
        self.assertEqual((str(Path(path)), load_contract(path)["_sha256"]), (existing["path"], existing["sha256"]))
        self.assertEqual([("production_run_started", "run-neg")],
                         [(step["event"], step["job_id"]) for step in existing["recorded_steps"]])
        self.assertIn("can then no longer be resolved", existing["replacing_it"])
        preview = self.write(confirmed=False, replace_existing=True)
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(existing["sha256"], preview["existing_contract"]["sha256"])
        self.assertEqual(load_contract(path)["_sha256"], existing["sha256"], "a preview writes nothing")
        self.assertTrue(self.write(replace_existing=True)["written"])
        # The new contract's record starts afresh: nothing was done under it yet.
        again = self.write(confirmed=False)
        self.assertEqual([], again["existing_contract"]["recorded_steps"])

    def test_the_contract_fixes_the_method_files_and_the_console_build(self) -> None:
        contract = load_contract(self.batch.contract())
        resources = Path(lab_lipidomics_agent.__file__).resolve().parent.parent / "resources"
        for key, name in (("parameter_template", "msdial_console_param4lipidomics.txt"), ("lbm_queries", "LbmQueries.txt")):
            self.assertEqual((str((resources / name).resolve()), lab_lipidomics_agent.file_sha256(resources / name)),
                             (contract["inputs"][key]["path"], contract["inputs"][key]["sha256"]))
        self.assertEqual(lab_lipidomics_agent.file_sha256(self.batch.console), contract["console"]["assembly_sha256"])
        self.assertTrue(contract["console"]["inventory_sha256"])

    def test_a_changed_input_makes_the_contract_unusable(self) -> None:
        path = self.batch.contract()
        self.batch.workbook.write_bytes(self.batch.workbook.read_bytes() + b"\0")
        with self.assertRaises(LabContractError) as raised:
            load_contract(path)
        self.assertIn("quant table", str(raised.exception))

    def test_an_edited_internal_standard_library_in_the_inputs_makes_the_contract_unusable(self) -> None:
        # The library MS-DIAL's Text-DB annotator reads and the resolver takes its windows from.
        path = self.batch.contract()
        library = self.batch.analysis / "inputs" / "is_neg.txt"
        library.write_bytes(library.read_bytes().replace(b"\t9.5\t", b"\t9.9\t"))
        with self.assertRaises(LabContractError) as raised:
            load_contract(path)
        self.assertIn("Negative canonical internal-standard library is_neg.txt has changed", str(raised.exception))


class FromTheRunToTheLipidome(_Batch):
    def setUp(self) -> None:
        super().setUp()
        self.contract = self.batch.contract()
        self.runs = {
            "Positive": write_run(self.batch, self.contract, "Positive"),
            "Negative": write_run(self.batch, self.contract, "Negative"),
        }

    def resolved(self) -> dict:
        return resolve_standards_for_contract(self.contract, dict(self.runs))

    def plan(self, polarity: str, **decisions) -> dict:
        digest = self.resolved()["polarities"][polarity]["resolution_digest"]
        return plan_normalize(self.contract, self.runs[polarity], digest, **decisions)

    def normalize(self, polarity: str, **decisions) -> dict:
        plan = self.plan(polarity, **decisions)
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), polarity)
        return finish_normalize(plan, 0, report.splitlines())

    def test_the_standards_are_resolved_by_tier(self) -> None:
        result = self.resolved()["polarities"]
        positive = {item["name"]: item for item in result["Positive"]["summary"]["standards"]}
        self.assertEqual(("A", "B", "D", "A"), tuple(positive[name]["tier"] for name in (
            "PC 15:0_18:1(d7)", "LPC 18:1(d7)", "Cer 18:1;O2/15:0(d7)", "CE 18:1(d7)")))
        self.assertEqual("unresolved_expected", positive["FA 18:0(d3)"]["status"])
        self.assertEqual(["Cer 18:1;O2/15:0(d7)"], result["Positive"]["summary"]["needs_confirmation"])
        self.assertEqual(["Cer-NS", "FA", "HexCer-NS"], result["Positive"]["summary"]["classes_not_quantified_if_unchanged"])
        negative = {item["name"]: item for item in result["Negative"]["summary"]["standards"]}
        self.assertEqual("B", negative["FA 18:0(d3)"]["tier"])
        self.assertEqual(["Blank_Neg"], negative["FA 18:0(d3)"]["tolerated_zero_height_files"])
        self.assertEqual(["AHexCS", "CE"], result["Negative"]["summary"]["classes_not_quantified_if_unchanged"])

    def test_a_run_not_made_from_the_contract_is_refused(self) -> None:
        (self.batch.analysis / "pos" / "method.txt").write_text("Solvent type: HCOONH4\n", encoding="utf-8")
        failed = dict(self.runs["Negative"], status="failed",
                      logs=["Automatic alignment RT correction failed: too few anchors"])
        result = resolve_standards_for_contract(self.contract, {"Positive": self.runs["Positive"], "Negative": failed})
        self.assertTrue(any("Solvent type: CH3COONH4" in item for item in result["polarities"]["Positive"]["problems"]))
        negative = " ".join(result["polarities"]["Negative"]["problems"])
        self.assertIn("is failed", negative)
        self.assertIn("never retry it unasked", negative)

    def test_a_run_planned_from_a_replaced_contract_is_refused(self) -> None:
        from msdial_app.agent_workflow import build_guided_plan

        # The backend writes the contract's sha256 beside the run it plans.
        plan = build_guided_plan(str(self.batch.neg), {"lab_lipidomics_contract_path": self.contract,
                                                       "ion_mode": "Negative", "minimum_peak_height": 500})
        written = server._write_guided_answers({"run_directory": self.runs["Negative"]["preparation"]["run_directory"]},
                                               plan)
        self.assertEqual(load_contract(self.contract)["_sha256"],
                         json.loads(Path(written).read_text(encoding="utf-8"))["lab_lipidomics"]["contract_sha256"])
        self.assertEqual([], self.resolved()["polarities"]["Negative"]["problems"])
        check = check_inputs(self.batch.request(internal_standard_rt_tolerance=0.3))
        replaced = write_contract({**self.batch.request(internal_standard_rt_tolerance=0.3),
                                   "check_digest": check["check_digest"], "sample_table_confirmed": True,
                                   "replace_existing": True, "confirmed": True})
        self.assertTrue(replaced["written"], replaced)
        problems = " ".join(self.resolved()["polarities"]["Negative"]["problems"])
        self.assertIn("another version of this contract", problems)

    def test_a_run_whose_files_or_annotations_are_missing_is_refused(self) -> None:
        output = Path(self.runs["Negative"]["preparation"]["run_directory"])
        cases = {
            "not planned from this contract": lambda: (output / "guided-answers.json").write_text(json.dumps(
                {"answers": {"lab_lipidomics_contract_path": str(self.batch.root / "other" / "contract.json")}}),
                encoding="utf-8"),
            ".mdcandidate.tsv is missing": lambda: (output / f"{ALIGNMENT}.mdcandidate.tsv").unlink(),
            "_Loaded.msp2.dbs is not beside the project": lambda: next(output.glob("*_Loaded.msp2.dbs")).unlink(),
            "No spot in the run is annotated by the LBM library": lambda: (output / f"{ALIGNMENT}_Height.txt").write_text(
                (output / f"{ALIGNMENT}_Height.txt").read_text(encoding="ascii").replace("LbmDB: ", "Other: "),
                encoding="ascii", newline=""),
        }
        for message, damage in cases.items():
            with self.subTest(message=message):
                self.runs["Negative"] = write_run(self.batch, self.contract, "Negative")
                damage()
                problems = " ".join(self.resolved()["polarities"]["Negative"]["problems"])
                self.assertIn(message, problems)

    def test_normalize_needs_the_digest_shown_and_a_decision_for_every_searched_standard(self) -> None:
        with self.assertRaises(LabContractError) as raised:
            plan_normalize(self.contract, self.runs["Positive"], "0" * 64)
        self.assertIn("not the one the person confirmed", str(raised.exception))
        with self.assertRaises(LabContractError) as raised:
            self.plan("Positive")
        self.assertIn("Cer 18:1;O2/15:0(d7)", str(raised.exception))
        with self.assertRaises(LabContractError):
            self.plan("Positive", accept_searched=["PC 15:0_18:1(d7)"])

    def test_the_command_says_why_each_flag_is_there(self) -> None:
        plan = self.plan("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        command = plan["command"]
        self.assertEqual(["normalize", "-i"], command[1:3])
        self.assertIn("--peak-ids-only", command)
        self.assertIn("--allow-unresolved-standards", command)  # FA 18:0(d3) is not in the positive library
        self.assertIn("--allow-mismatched-peak-ids", command)  # the searched Cer d7 spot is unnamed
        self.assertNotIn("-d", command)
        self.assertEqual(UNIT, command[command.index("-u") + 1])
        rows = {row["TargetClass"]: row for row in plan["rows"]}
        self.assertEqual((3, True), (rows["Cer-NS"]["PeakID"], rows["Cer-NS"]["AssignName"]))
        self.assertEqual((-1, False), (rows["FA"]["PeakID"], rows["FA"]["AssignName"]))
        declined = self.plan("Positive", accept_unresolved=["Cer 18:1;O2/15:0(d7)"])
        self.assertEqual(-1, {row["TargetClass"]: row for row in declined["rows"]}["Cer-NS"]["PeakID"])
        self.assertNotIn("--allow-mismatched-peak-ids", declined["command"])

    def rewrite_contract(self, change) -> None:
        contract = json.loads(Path(self.contract).read_text(encoding="utf-8"))
        change(contract)
        Path(self.contract).write_text(json.dumps(contract), encoding="utf-8")
        for polarity, run in self.runs.items():
            # The runs are planned again from the rewritten contract.
            (Path(run["preparation"]["run_directory"]) / "guided-answers.json").write_text(
                guided_answers(self.contract, polarity), encoding="utf-8")

    def test_dilution_factors_other_than_one_are_applied_by_multiplying(self) -> None:
        def sample(stem, **change):
            def apply(contract):
                next(row for row in contract["sample_table"]["rows"] if row["stem"] == stem).update(change)
            return apply

        # A sample left out is not normalized, so its factor asks for no -d.
        self.rewrite_contract(sample("S2", factor=3.0, included=False))
        self.assertNotIn("-d", self.plan("Negative")["command"])
        self.rewrite_contract(sample("S1", factor=2.0))
        plan = self.plan("Negative")
        self.assertIn("-d", plan["command"])
        self.assertIn("multiplies", plan["flags"]["-d"])
        self.assertEqual({"S1_Neg": 2.0, "Blank_Neg": 1.0}, plan["dilution_factors"])
        write_normalize_inputs(plan)
        # What the Console writes with -d: S1's values, the standards' own included, multiplied by 2.
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative", factors={"S1_Neg": 2.0})
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertTrue(record["verified"], (record["problems"], record["verification"]["failed_rows"]))
        # The same outputs, had the factor not been applied, are refused cell by cell.
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative")
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertFalse(record["verified"])
        self.assertIn("101", record["verification"]["failed_ids"])

    def test_the_person_may_take_another_listed_spot_for_a_standard(self) -> None:
        # A second spot at the positive Cer d7's m/z and RT, less intense than the one the resolver takes.
        extra = [dict(alignment_id=5, mz=531.5480, rt=9.20, name="Unknown", cells=[4000, 3500, 1000])]
        self.runs["Positive"] = write_run(self.batch, self.contract, "Positive", extra_spots=extra)
        summary = {item["name"]: item for item in self.resolved()["polarities"]["Positive"]["summary"]["standards"]}
        cer = summary["Cer 18:1;O2/15:0(d7)"]
        self.assertEqual(("D", "3", ["5"]), (cer["tier"], cer["alignment_id"], cer["valid_alternatives"]))
        name = "Cer 18:1;O2/15:0(d7)"
        plan = self.plan("Positive", accept_spot={name: 5})
        rows = {row["TargetClass"]: row for row in plan["rows"]}
        self.assertEqual((5, True, "chosen_by_person"), (rows["Cer-NS"]["PeakID"], rows["Cer-NS"]["AssignName"],
                                                       rows["Cer-NS"]["status"]))
        self.assertEqual({name: 5}, plan["accepted_spots"])
        self.assertIn((name, 5), [(item["standard"], item["alignment_id"]) for item in plan["predicted_mismatches"]])
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Positive", extra_spots=extra)
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertTrue(record["verified"], (record["problems"], record["verification"]["failed_rows"]))
        self.assertEqual({name: 5}, record["accepted_spots"])
        self.assertIn("5", record["internal_standard_ids"])
        refusals = (
            ({"accept_spot": {name: 10}}, "not a candidate"),
            ({"accept_spot": {name: 5}, "accept_searched": [name]}, "more than one decision"),
            ({"accept_spot": {"FA 16:0(d3)": 5}}, "sheet 2 does not use"),
        )
        for decisions, message in refusals:
            with self.subTest(message=message), self.assertRaisesRegex(LabContractError, message):
                self.plan("Positive", **decisions)

    def test_a_console_rebuilt_since_the_contract_is_said_and_recorded(self) -> None:
        plan = self.plan("Negative")
        self.assertEqual((False, []), (plan["console"]["changed_since_contract"], plan["warnings"]))
        self.batch.console.write_bytes(b"not a console, rebuilt")
        plan = self.plan("Negative")
        self.assertTrue(plan["console"]["changed_since_contract"])
        self.assertIn("not the build the contract recorded", plan["warnings"][0])
        self.assertNotEqual(plan["console"]["contract"]["assembly_sha256"], plan["console"]["now"]["assembly_sha256"])
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative")
        record = finish_normalize(plan, 0, report.splitlines())
        stored = json.loads(Path(record["record"]).read_text(encoding="utf-8"))
        self.assertEqual(plan["console"]["now"]["assembly_sha256"], stored["console"]["now"]["assembly_sha256"])
        self.assertTrue(stored["console"]["changed_since_contract"])

    def test_a_console_without_peak_ids_only_is_refused(self) -> None:
        with patch.object(lab_lipidomics_agent, "console_capabilities", return_value={"capabilities": []}):
            with self.assertRaises(LabContractError) as raised:
                self.plan("Negative")
        self.assertIn("--peak-ids-only", str(raised.exception))

    def test_normalize_is_checked_and_each_cell_recomputed(self) -> None:
        record = self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.assertTrue(record["verified"], (record["problems"], record["verification"]))
        quant = self.batch.analysis / "quant"
        self.assertTrue((quant / "NormalizedHeight_Pos.txt").is_file())
        self.assertTrue((quant / "Height_Pos.txt").is_file())
        self.assertTrue((quant / "standards_pos.tsv").is_file())
        self.assertTrue((quant / "is_resolution_pos.tsv").is_file())
        self.assertEqual(["1", "2", "3", "4"], record["internal_standard_ids"])

    def test_a_cell_that_does_not_follow_fails_its_row_and_a_report_that_differs_fails_the_step(self) -> None:
        plan = self.plan("Negative")
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative", wrong_cell=110)
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertTrue(record["completed"])
        self.assertFalse(record["verified"])
        self.assertEqual(["110"], record["verification"]["failed_ids"])
        tampered = [line.replace("alignment ID 101", "alignment ID 110") for line in report.splitlines()]
        record = finish_normalize(plan, 0, tampered)
        self.assertFalse(record["completed"])
        self.assertTrue(any("normalize used 110" in item for item in record["problems"]))

    def test_mismatches_normalize_reports_must_be_the_predicted_ones(self) -> None:
        # Normalize finds the negative PC d7 spot stored under its sum-level name: a mismatch nobody predicted.
        plan = self.plan("Negative")
        write_normalize_inputs(plan)
        self.assertEqual([], plan["predicted_mismatches"])
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative", stored_names={101: "PC 33:1(d7)"})
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertFalse(record["completed"])
        self.assertTrue(any("other mismatched standards than were predicted" in item for item in record["problems"]))
        # The searched positive Cer d7 spot is predicted to be reported, and normalize says nothing of it.
        plan = self.plan("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        write_normalize_inputs(plan)
        self.assertEqual(["Cer 18:1;O2/15:0(d7)"], [item["standard"] for item in plan["predicted_mismatches"]])
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Positive",
                                         stored_names={3: "Cer 18:1;O2/15:0(d7)"})
        record = finish_normalize(plan, 0, report.splitlines())
        self.assertFalse(record["completed"])

    def test_the_merge_previews_then_writes_the_lipidome(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative")
        # The merge's own folder: R reads every subfolder of the folder it runs in, and quant holds pos and neg.
        quant = self.batch.analysis / "quant" / "lipidome"
        preview = merge_lipidome(self.contract)
        self.assertFalse(quant.exists())
        self.assertFalse(preview["written"])
        written = merge_lipidome(self.contract, write=True)
        self.assertEqual(preview["output_sha256"], written["output_sha256"])
        self.assertEqual((3, 3), (written["kept_from_positive"], written["kept_from_negative"]))
        self.assertEqual({"AHexCS": 1}, written["not_quantified"]["by_class"])
        text = (quant / "lipid_metabolome.csv").read_bytes()
        self.assertIn(b"\r\n", text)
        self.assertNotIn(b"(d7)", text)
        self.assertTrue((quant / "lipid_metabolome_not_quantified.csv").is_file())
        for name in ("lipid_metabolome4stat.csv", "lipid_metabolome4classsummary.csv", "lipid_metabolome4summaryplot.csv"):
            self.assertTrue((quant / name).is_file(), name)
        # The samples and QCs, never the blank: S1 and S2 here.
        self.assertEqual(["SPF_1", "GF_2"], [item["name"] for item in written["samples"]["taken"]])
        self.assertTrue((quant / "Summary_Dotplots").is_dir())
        self.assertNotIn("manifest", written["outputs"]["dotplots"])
        report = json.loads((quant / "merge-report.json").read_text(encoding="utf-8"))
        self.assertEqual(written["outputs"]["dotplots"]["files"], len(report["outputs"]["dotplots"]["manifest"]))
        # What the merge step adds is in the file too, the build that normalized each polarity included.
        self.assertEqual(UNIT, report["unit"])
        self.assertEqual({"Positive", "Negative"}, set(report["verification"]))
        self.assertEqual(lab_lipidomics_agent.file_sha256(self.batch.console),
                         report["normalize_console"]["Negative"]["assembly_sha256"])
        self.assertNotIn("path", report["normalize_console"]["Negative"])
        events = [json.loads(line)["event"]
                  for line in (self.batch.analysis / "inputs" / "lab-lipidomics-record.jsonl").read_text().splitlines()]
        self.assertEqual(["contract_written", "normalized", "normalized", "merged"], events)

    def test_the_merge_refuses_a_polarity_not_normalized_or_changed_since(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        with self.assertRaises(LabContractError) as raised:
            merge_lipidome(self.contract)
        self.assertIn("negative polarity has not been normalized", str(raised.exception))
        self.normalize("Negative")
        normalized = self.batch.analysis / "quant" / "NormalizedHeight_Neg.txt"
        normalized.write_bytes(normalized.read_bytes() + b"\r\n")
        with self.assertRaises(LabContractError):
            merge_lipidome(self.contract)
        with self.assertRaises(LabContractError):
            merge_lipidome(self.contract, output_name="..\\elsewhere.csv")

    def test_the_merge_never_writes_over_what_it_or_normalize_wrote(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative")
        quant = self.batch.analysis / "quant"
        kept = {path.name: path.read_bytes() for path in quant.iterdir() if path.is_file()}
        for name in ("lipid_metabolome_not_quantified.csv", "LIPID_METABOLOME_NOT_QUANTIFIED.CSV",
                     "NormalizedHeight_Pos.txt", "height_neg.txt", "standards_pos.tsv", "is_resolution_neg.tsv",
                     "lipid_metabolome4stat.csv", "Lipid_Metabolome4ClassSummary.csv"):
            with self.subTest(name=name), self.assertRaisesRegex(LabContractError, "choose another name"):
                merge_lipidome(self.contract, output_name=name, write=True)
        self.assertEqual(kept, {path.name: path.read_bytes() for path in quant.iterdir() if path.is_file()})
        first = merge_lipidome(self.contract, output_name="lipidome_2026.csv", write=True)
        self.assertTrue(first["written"])
        # The three tables keep metaboservice's names, which metabolomics_service.R reads, whatever the lipidome is called.
        folder = quant / "lipidome"
        self.assertEqual(
            ["lipid_metabolome4classsummary.csv", "lipid_metabolome4stat.csv", "lipid_metabolome4summaryplot.csv",
             "lipid_metabolome_not_quantified.csv", "lipidome_2026.csv", "merge-report.json"],
            sorted(path.name for path in folder.iterdir() if path.is_file()),
        )
        stat = (folder / "lipid_metabolome4stat.csv").read_bytes()
        # Merged again under the default name: the tables are rewritten, and the earlier lipidome left beside it is said.
        second = merge_lipidome(self.contract, write=True)
        self.assertTrue(any("lipidome_2026.csv, the lipidome the earlier merge here wrote, stays beside this merge's "
                            "lipid_metabolome.csv" in item for item in second["warnings"]))
        self.assertEqual(stat, (folder / "lipid_metabolome4stat.csv").read_bytes())
        merged = [json.loads(line) for line in (self.batch.analysis / "inputs" / "lab-lipidomics-record.jsonl")
                  .read_text(encoding="utf-8").splitlines() if json.loads(line)["event"] == "merged"]
        self.assertEqual(
            [{"lipid_metabolome4stat.csv", "lipid_metabolome4classsummary.csv", "lipid_metabolome4summaryplot.csv"}] * 2,
            [set(item["tables"]) for item in merged],
        )

    def test_the_merge_refuses_a_normalize_not_completed_as_planned(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        plan = self.plan("Negative")
        write_normalize_inputs(plan)
        _, _, report = normalize_outputs(public_normalize_plan(plan), "Negative")
        # The matrices are there and match their sha256; normalize's own account did not match the table.
        finish_normalize(plan, 0, [line.replace("alignment ID 101", "alignment ID 110") for line in report.splitlines()])
        with self.assertRaises(LabContractError) as raised:
            merge_lipidome(self.contract)
        self.assertIn("did not complete as planned", str(raised.exception))
        self.assertFalse((self.batch.analysis / "quant" / "lipidome" / "lipid_metabolome.csv").exists())

    def test_the_merge_reconciles_the_polarities_as_the_contract_states_and_records_it(self) -> None:
        # A plasmenyl PE that positive mode names PE P-34:1 and negative mode names PE O-34:2.
        extra = {
            "Positive": [dict(alignment_id=20, mz=702.5436, rt=10.00, name="PE P-34:1", adduct="[M+H]+",
                              ontology="EtherPE", method=LBM, cells=[5000, 4000, 0])],
            "Negative": [dict(alignment_id=120, mz=700.5291, rt=10.04, name="PE O-34:2", adduct="[M-H]-",
                              ontology="EtherPE", method=LBM, cells=[6000, 5000, 0])],
        }
        self.runs = {polarity: write_run(self.batch, self.contract, polarity, extra_spots=extra[polarity])
                     for polarity in ("Positive", "Negative")}
        for polarity, accepted in (("Positive", ["Cer 18:1;O2/15:0(d7)"]), ("Negative", [])):
            plan = self.plan(polarity, accept_searched=accepted)
            write_normalize_inputs(plan)
            _, _, report = normalize_outputs(public_normalize_plan(plan), polarity, extra_spots=extra[polarity])
            self.assertTrue(finish_normalize(plan, 0, report.splitlines())["completed"])
        contract = load_contract(self.contract)
        self.assertEqual((True, 0.1), (contract["merge"]["polarity_reconciliation"]["enabled"],
                                       contract["merge"]["polarity_reconciliation"]["rt_tolerance_min"]))
        self.assertEqual(["Sample", "QC"], contract["merge"]["sample_file_types"])

        preview = merge_lipidome(self.contract)
        reconciliation = preview["polarity_reconciliation"]
        self.assertEqual(1, reconciliation["counts"]["EtherPE"]["matched"])
        self.assertEqual(("20", "120", "PE P-34:1"), (
            reconciliation["matched"][0]["positive"]["alignment_id"], reconciliation["matched"][0]["negative"]["alignment_id"],
            reconciliation["matched"][0]["renamed_to"]["name"]))
        self.assertEqual([], reconciliation["deviations_from_contract"])
        # The fixture's sheet 3 still takes EtherPE from positive mode too, which the person kept at the check and
        # the contract records. The merge says what the lipidome counts, not how to change sheet 3: an edit now
        # would make the contract, and everything run under it, unusable.
        self.assertEqual(2, len(contract["merge"]["selection_warnings_accepted"]))
        selection = [item for item in preview["warnings"] if "positive mode, so this lipidome counts" in item
                     or "does not select BMP [M-H]-" in item]
        self.assertEqual(2, len(selection), preview["warnings"])
        self.assertTrue(selection[1].startswith("Sheet 3 selects EtherPE [M+H]+ in positive mode, so this lipidome counts "
                                                "EtherPE from both polarities"), selection)
        for item in selection:
            self.assertTrue(item.endswith("The contract accepted this sheet 3 as it is (merge_selection_accepted). Sheet 3 "
                                          "is part of the contract: changing it now needs a new contract and new runs of "
                                          "both polarities."), item)
            self.assertNotIn("FALSE", item)
            self.assertNotIn("TRUE", item)
        self.assertEqual("EtherPE", reconciliation["normalized_as"]["EtherPE"]["normalized_as"])

        off = merge_lipidome(self.contract, write=True, reconcile_polarities=False)
        self.assertFalse(off["polarity_reconciliation"]["enabled"])
        self.assertEqual(["the positive/negative reconciliation is off; the contract says on"],
                         off["polarity_reconciliation"]["deviations_from_contract"])
        folder = self.batch.analysis / "quant" / "lipidome"
        self.assertIn(b",PE O-34:2,", (folder / "lipid_metabolome.csv").read_bytes())
        # Merged again as the contract says: the earlier merge's dotplot files are replaced, not left for R.
        on = merge_lipidome(self.contract, write=True)
        self.assertEqual(1, on["outputs"]["dotplots"]["replaced_files_of_earlier_merge"])
        self.assertFalse((folder / "subclass_EtherPE" / "PE O-34_2_120.csv").exists())
        self.assertTrue((folder / "subclass_EtherPE" / "PE P-34_1_120.csv").is_file())
        merged = [json.loads(line) for line in (self.batch.analysis / "inputs" / "lab-lipidomics-record.jsonl")
                  .read_text(encoding="utf-8").splitlines() if json.loads(line)["event"] == "merged"]
        self.assertEqual([False, True], [item["polarity_reconciliation"]["enabled"] for item in merged])
        self.assertEqual(1, merged[-1]["polarity_reconciliation"]["counts"]["EtherPE"]["matched"])
        self.assertEqual({"kept": 1, "not_quantified": 0, "not_selected": 0}, merged[-1]["polarity_reconciliation"]["renamed_rows"])
        self.assertEqual(["Sample", "QC"], merged[-1]["sample_file_types"])
        self.assertIn("lipid_metabolome4stat.csv", merged[-1]["tables"])
        for tolerance in (0, -0.1, float("nan"), True):
            with self.subTest(tolerance=tolerance), self.assertRaises(LabContractError):
                merge_lipidome(self.contract, reconciliation_rt_tolerance=tolerance)
        wider = merge_lipidome(self.contract, reconciliation_rt_tolerance=0.2)
        self.assertEqual(["the reconciliation RT tolerance is 0.2 min; the contract says 0.1"],
                         wider["polarity_reconciliation"]["deviations_from_contract"])

    def test_a_sheet_three_changed_after_the_contract_makes_the_merge_refuse(self) -> None:
        # Why the merge does not advise editing sheet 3: the contract holds the workbook.
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative")
        quant_workbook(self.batch.workbook, quant=quant_sheet(RECONCILED_QUANT_INFO))
        with self.assertRaises(LabContractError) as raised:
            merge_lipidome(self.contract)
        self.assertIn("quant table quant_utility_table.xlsx has changed", str(raised.exception))

    def test_the_merge_refuses_a_contract_or_a_candidate_table_changed_since_normalize(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative")
        candidates = self.batch.analysis / "neg" / f"{ALIGNMENT}.mdcandidate.tsv"
        kept = candidates.read_bytes()
        candidates.write_bytes(kept + b"\n")
        with self.assertRaises(LabContractError) as raised:
            merge_lipidome(self.contract)
        self.assertIn("candidate table has changed since normalize", str(raised.exception))
        candidates.write_bytes(kept)
        merge_lipidome(self.contract)
        check = check_inputs(self.batch.request())
        write_contract({**self.batch.request(), "check_digest": check["check_digest"], "sample_table_confirmed": True,
                        "replace_existing": True, "confirmed": True})
        with self.assertRaises(LabContractError) as raised:
            merge_lipidome(self.contract)
        self.assertIn("made under another contract", str(raised.exception))


class TheMergeAsTheLaboratoryRunsIt(_Batch):
    """A batch with a QC file, and a sheet 2 that names other standards for BMP and PG."""

    def prepare(self, *, stems=None, workbook=None, extra=None) -> str:
        root = Path(self.directory.name) / "lab"
        self.batch = Batch(root, stems=stems) if stems else Batch(root)
        if workbook:
            quant_workbook(self.batch.workbook, **workbook)
        contract = self.batch.contract()
        extra = extra or {}
        for polarity, accepted in (("Positive", ["Cer 18:1;O2/15:0(d7)"]), ("Negative", [])):
            run = write_run(self.batch, contract, polarity, extra_spots=extra.get(polarity))
            resolved = resolve_standards_for_contract(contract, {polarity: run})
            plan = plan_normalize(contract, run, resolved["polarities"][polarity]["resolution_digest"],
                                  accept_searched=accepted)
            write_normalize_inputs(plan)
            _, _, report = normalize_outputs(public_normalize_plan(plan), polarity, extra_spots=extra.get(polarity),
                                             stems=self.batch.stems)
            record = finish_normalize(plan, 0, report.splitlines())
            self.assertTrue(record["completed"], record.get("problems"))
        return contract

    def test_the_statistics_take_the_qc_with_the_samples_and_never_the_blank(self) -> None:
        # The laboratory's decision (metaboservice 2025): Sample and QC columns, a Blank never.
        contract = self.prepare(stems=STEMS_WITH_QC)
        written = merge_lipidome(contract, write=True)
        self.assertEqual(["SPF_1", "GF_2", "QC_3"], [item["name"] for item in written["samples"]["taken"]])
        self.assertEqual(["Blank_Pos"], [item["file"] for item in written["samples"]["not_taken"]])
        folder = self.batch.analysis / "quant" / "lipidome"

        def rows(path: Path) -> list[list[str]]:
            return [line.split(",") for line in path.read_bytes().decode("ascii").split("\r\n")[1:-1]]

        self.assertEqual([["SPF_1", "SPF"], ["GF_2", "GF"], ["QC_3", "QC"]],
                         [row[:2] for row in rows(folder / "lipid_metabolome4stat.csv")])
        self.assertEqual(["SPF_1", "GF_2", "QC_3"], [row[0] for row in rows(folder / "lipid_metabolome4classsummary.csv")])
        self.assertEqual(["GF", "QC", "SPF"], [row[1] for row in rows(folder / "subclass_LPC" / "LPC 16_0_10.csv")])

    def test_a_sheet_three_changed_before_the_contract_leaves_nothing_to_say_at_the_merge(self) -> None:
        extra = {
            "Positive": [dict(alignment_id=20, mz=702.5436, rt=10.00, name="PE P-34:1", adduct="[M+H]+",
                              ontology="EtherPE", method=LBM, cells=[5000, 4000, 0])],
            "Negative": [dict(alignment_id=120, mz=700.5291, rt=10.04, name="PE O-34:2", adduct="[M-H]-",
                              ontology="EtherPE", method=LBM, cells=[6000, 5000, 0])],
        }
        contract = self.prepare(workbook={"quant": quant_sheet(RECONCILED_QUANT_INFO)}, extra=extra)
        self.assertEqual([], load_contract(contract)["merge"]["selection_warnings_accepted"])
        preview = merge_lipidome(contract)
        self.assertEqual(1, preview["polarity_reconciliation"]["counts"]["EtherPE"]["matched"])
        self.assertEqual({"kept": 1, "not_quantified": 0, "not_selected": 0}, preview["polarity_reconciliation"]["renamed_rows"])
        self.assertFalse(any("Sheet 3" in item for item in preview["warnings"]), preview["warnings"])
        # The renamed row is the only EtherPE: the positive PE P- is not selected.
        self.assertEqual(0, sum(1 for item in preview["possible_double_counts"] if item["ontology"] == "EtherPE"))

    def bmp_batch(self, bmp_standard: str | None) -> str:
        workbook = {
            "lookup": lookup_sheet(LOOKUP + [("PC 15:0_18:1(d7)", "PG")]
                                   + ([(bmp_standard, "BMP")] if bmp_standard is not None else [])),
            "quant": quant_sheet(QUANT_INFO + [("PG", "[M-H]-", "Negative", True), ("BMP", "[M-H]-", "Negative", True),
                                               ("BMP", "[M+NH4]+", "Positive", False)]),
        }
        extra = {
            "Positive": [dict(alignment_id=21, mz=792.5538, rt=8.30, name="BMP 36:2", adduct="[M+NH4]+", ontology="BMP",
                              method=LBM, cells=[4000, 3000, 0])],
            "Negative": [dict(alignment_id=121, mz=773.5338, rt=8.33, name="PG 36:2", adduct="[M-H]-", ontology="PG",
                              method=LBM, cells=[7000, 6000, 0])],
        }
        return self.prepare(workbook=workbook, extra=extra)

    def test_a_pg_renamed_bmp_against_another_standard_than_sheet_two_names_for_bmp_is_said(self) -> None:
        preview = merge_lipidome(self.bmp_batch("LPC 18:1(d7)"))
        reconciliation = preview["polarity_reconciliation"]
        self.assertEqual(1, reconciliation["counts"]["BMP"]["matched"])
        self.assertEqual(("21", "121", "BMP 36:2"), (
            reconciliation["matched"][0]["positive"]["alignment_id"], reconciliation["matched"][0]["negative"]["alignment_id"],
            reconciliation["matched"][0]["renamed_to"]["name"]))
        bmp = reconciliation["normalized_as"]["BMP"]
        self.assertEqual((True, "PC 15:0_18:1(d7)", "LPC 18:1(d7)"), (bmp["differs"], bmp["pg_standard"], bmp["bmp_standard"]))
        self.assertIn(
            "The negative rows renamed BMP carry the concentration normalize computed for them as PG, against "
            "PC 15:0_18:1(d7); sheet 2 names LPC 18:1(d7) for BMP.", preview["warnings"])
        self.assertEqual({"kept": 1, "not_quantified": 0, "not_selected": 0}, reconciliation["renamed_rows"])

    def test_a_bmp_sheet_two_does_not_name_falls_to_any_others_and_that_is_said(self) -> None:
        preview = merge_lipidome(self.bmp_batch(None))
        bmp = preview["polarity_reconciliation"]["normalized_as"]["BMP"]
        self.assertEqual(("LPC 18:1(d7)", "Any others", "PC 15:0_18:1(d7)", "PG", True),
                         (bmp["bmp_standard"], bmp["bmp_standard_from"], bmp["pg_standard"], bmp["pg_standard_from"],
                          bmp["differs"]))
        self.assertIn(
            "The negative rows renamed BMP carry the concentration normalize computed for them as PG, against "
            "PC 15:0_18:1(d7); sheet 2 names no standard for BMP, so BMP falls to Any others (LPC 18:1(d7)).",
            preview["warnings"])

    def test_a_bmp_normalized_against_the_standard_sheet_two_names_for_it_is_not_said(self) -> None:
        preview = merge_lipidome(self.bmp_batch("PC 15:0_18:1(d7)"))
        self.assertEqual(1, preview["polarity_reconciliation"]["counts"]["BMP"]["matched"])
        self.assertFalse(preview["polarity_reconciliation"]["normalized_as"]["BMP"]["differs"])
        self.assertFalse(any("renamed BMP carry" in item for item in preview["warnings"]))


class WorksetsAndSharing(_Batch):
    def test_a_laboratory_workset_carries_the_inputs_and_not_the_batch(self) -> None:
        contract = self.batch.contract()
        settings = lab_lipidomics_agent.lab_workset_settings(contract)
        with patch("msdial_app.worksets.workset_directory", return_value=self.batch.root / "worksets"):
            saved = save_lab_lipidomics_workset("Plasma lipidomics, conventional LC", settings)
            self.assertEqual(LAB_LIPIDOMICS_WORKSET_KIND, saved["kind"])
            self.assertEqual({}, saved["answers"])
            self.assertIn("sample_table", saved["not_carried"])
            self.assertNotIn("sample_table", saved["lab_lipidomics"]["answers"])
            self.assertEqual("CH3COONH4", saved["lab_lipidomics"]["answers"]["solvent_type"])
            self.assertEqual(saved["id"], get_workset(saved["id"])["id"])
            # It fills the next batch's check, which says when a carried file has changed.
            self.batch.is_pos.write_bytes(self.batch.is_pos.read_bytes() + b"\r\n")
            result = public_check(check_inputs({
                "pos_path": str(self.batch.pos), "neg_path": str(self.batch.neg), "workset_id": saved["id"],
            }))
            # The answer about a sheet 3 that contradicts the reconciliation is the batch's, never carried.
            self.assertEqual(["analysis_purpose", "merge_selection_accepted", "analysis_root", "raw_location"],
                             [item["id"] for item in result["questions"]])
            self.assertTrue(any("has changed since workset" in item for item in result["warnings"]))
            from msdial_app.agent_workflow import build_guided_plan

            plan = build_guided_plan(str(self.batch.pos), {"ion_mode": "Positive"}, saved["id"])
            self.assertTrue(any("laboratory lipidomics workset" in item for item in plan["blockers"]))

    def test_the_workset_carries_the_internal_standard_rt_tolerance_through_the_tool(self) -> None:
        contract = self.batch.contract(internal_standard_rt_tolerance=0.3)
        settings = lab_lipidomics_agent.lab_workset_settings(contract)
        captured = {}

        def request(method, path, **options):
            captured.update(options.get("body") or {})
            return {}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            mcp_server.msdial_lipidomics_check_inputs(pos_path=str(self.batch.pos), neg_path=str(self.batch.neg),
                                                      workset_id="w")
        self.assertIsNone(captured["internal_standard_rt_tolerance"])
        with patch("msdial_app.worksets.workset_directory", return_value=self.batch.root / "worksets"):
            saved = save_lab_lipidomics_workset("Plasma lipidomics", settings)
            result = public_check(check_inputs({**captured, "workset_id": saved["id"]}))
        self.assertEqual(0.3, result["tolerances"]["internal_standard_rt"])
        self.assertEqual(0.5, public_check(check_inputs({**captured, "workset_id": ""}))["tolerances"]["internal_standard_rt"])

    def test_a_check_filled_from_a_workset_is_written_with_the_same_workset(self) -> None:
        contract = self.batch.contract(internal_standard_rt_tolerance=0.3, number_of_threads=4)
        settings = lab_lipidomics_agent.lab_workset_settings(contract)
        batch = {"pos_path": str(self.batch.pos), "neg_path": str(self.batch.neg), "raw_location": "copy",
                 "analysis_purpose": "the next batch", "analysis_root": str(self.batch.root / "next"),
                 "merge_selection_accepted": True}
        with patch("msdial_app.worksets.workset_directory", return_value=self.batch.root / "worksets"):
            saved = save_lab_lipidomics_workset("Plasma lipidomics", settings)
            check = public_check(check_inputs({**batch, "workset_id": saved["id"]}))
            self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
            filled = check["filled_from_workset"]
            self.assertEqual(saved["id"], filled["workset_id"])
            self.assertEqual((0.3, 4), (filled["answers"]["internal_standard_rt_tolerance"],
                                        filled["answers"]["number_of_threads"]))
            confirmation = {"check_digest": check["check_digest"], "sample_table_confirmed": True, "confirmed": True}
            # Every value the check showed but the thread count, which only the workset gave.
            shown = {**batch, **{key: value for key, value in filled["answers"].items() if key != "number_of_threads"}}
            refused = write_contract({**shown, **confirmation})
            self.assertIn("pass the same workset_id", refused["error"])
            written = write_contract({**batch, **confirmation, "workset_id": saved["id"]})
            self.assertTrue(written["written"], written)
            self.assertEqual(4, load_contract(written["contract_path"])["number_of_threads"])
        captured = {}
        with patch.object(mcp_server, "_request_json", side_effect=lambda method, path, **options: captured.update(
                options.get("body") or {})):
            mcp_server.msdial_lipidomics_write_contract(
                pos_path="p", neg_path="n", lbm_path="l", solvent_type="s", retention_time_tolerance=1.5,
                is_text_pos="a", is_text_neg="b", quant_table_path="q", amount_column="H", analysis_root="r",
                analysis_purpose="x", console_path="c", raw_location="copy", check_digest="d", workset_id="w",
            )
        self.assertEqual("w", captured["workset_id"])

    def test_the_contract_and_its_record_never_leave_this_machine(self) -> None:
        for name in ("lab-lipidomics-contract.json", "lab-lipidomics-contract.20261014120000.json",
                     "lab-lipidomics-record.jsonl", "normalize_pos.local.json"):
            self.assertFalse(bundle_member_allowed(name, shared=True), name)
        self.assertTrue(bundle_member_allowed("lipid_metabolome.csv", shared=True))

    def test_a_diagnostic_of_a_contract_run_is_recorded_beside_the_contract(self) -> None:
        contract = self.batch.contract()
        recorded = server._record_peak_height_diagnostic(
            {"lab_lipidomics_contract_path": contract, "lab_lipidomics_polarity": "Positive",
             "diagnostic_run_directory": "d"},
            {"minimum_peak_height": 300, "estimated_peak_count": 4100},
            {"file_name": "S1_Pos", "selection_reason": "sample-nearest-run-midpoint", "threshold_step": 100},
            "job-1",
        )
        self.assertTrue(recorded["recorded"])
        last = json.loads(Path(recorded["lab_lipidomics_record"]).read_text().splitlines()[-1])
        self.assertEqual(("peak_height_diagnostic", 300), (last["event"], last["estimate"]["minimum_peak_height"]))


class TheToolsAndTheirGates(unittest.TestCase):
    """The tools as a client calls them: through the SDK, whose argument validation runs before the tool."""

    def capture(self, function, **arguments) -> dict:
        captured = {}

        def request(method, path, **options):
            captured.update(path=path, body=options.get("body"))
            return {"ok": True}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            asyncio.run(mcp_server.mcp.call_tool(function.__name__, arguments))
        return captured

    def refused(self, function, **arguments) -> str:
        with patch.object(mcp_server, "_request_json", side_effect=AssertionError("the backend was called")):
            with self.assertRaises(Exception) as raised:
                asyncio.run(mcp_server.mcp.call_tool(function.__name__, arguments))
        self.assertIn("validation error", str(raised.exception))
        return str(raised.exception)

    def test_only_a_literal_true_confirms(self) -> None:
        for function, arguments, path in (
            (mcp_server.msdial_lipidomics_normalize, {"contract_path": "c", "job_id": "j", "resolution_digest": "d"},
             "/api/lipidomics/normalize"),
            (mcp_server.msdial_lipidomics_merge, {"contract_path": "c"}, "/api/lipidomics/merge"),
        ):
            with self.subTest(tool=function.__name__):
                self.assertEqual(path, self.capture(function, **arguments)["path"])
                self.assertIs(False, self.capture(function, **arguments)["body"]["confirmed"])
                self.assertIs(True, self.capture(function, **arguments, confirmed=True)["body"]["confirmed"])
                # The SDK would read these as true; a gate takes only a JSON true.
                for value in ("yes", "true", 1):
                    with self.subTest(value=value):
                        self.assertIn("confirmed", self.refused(function, **arguments, confirmed=value))

    def test_the_contract_tools_flags_are_json_booleans(self) -> None:
        arguments = dict(pos_path="p", neg_path="n", lbm_path="l", solvent_type="s", retention_time_tolerance=1.5,
                         is_text_pos="a", is_text_neg="b", quant_table_path="q", amount_column="H", analysis_root="r",
                         analysis_purpose="x", console_path="c", raw_location="copy", check_digest="d")
        for flag in ("confirmed", "sample_table_confirmed", "replace_existing", "merge_selection_accepted"):
            with self.subTest(flag=flag):
                self.assertIs(True, self.capture(mcp_server.msdial_lipidomics_write_contract, **arguments, **{flag: True})
                              ["body"][flag])
                self.refused(mcp_server.msdial_lipidomics_write_contract, **arguments, **{flag: "yes"})
        body = self.capture(mcp_server.msdial_lipidomics_check_inputs, pos_path="p", neg_path="n",
                            merge_selection_accepted=True)["body"]
        self.assertIs(True, body["merge_selection_accepted"])
        self.refused(mcp_server.msdial_lipidomics_check_inputs, pos_path="p", neg_path="n", merge_selection_accepted="true")
        self.refused(mcp_server.msdial_lipidomics_resolve_standards, contract_path="c", read_output_folders="true")

    def test_the_merge_tool_passes_the_reconciliation_options_and_leaves_them_out_when_not_given(self) -> None:
        body = self.capture(mcp_server.msdial_lipidomics_merge, contract_path="c", reconcile_polarities=False,
                            reconciliation_rt_tolerance=0.2)["body"]
        self.assertIs(False, body["reconcile_polarities"])
        self.assertEqual(0.2, body["reconciliation_rt_tolerance"])
        self.assertIs(True, self.capture(mcp_server.msdial_lipidomics_merge, contract_path="c",
                                         reconcile_polarities=True)["body"]["reconcile_polarities"])
        body = self.capture(mcp_server.msdial_lipidomics_merge, contract_path="c")["body"]
        self.assertNotIn("reconcile_polarities", body)
        self.assertNotIn("reconciliation_rt_tolerance", body)
        # As the endpoint refuses them: text, or a number for a flag, is no answer.
        for value in ("false", "true", "yes", 0, 1):
            with self.subTest(value=value):
                self.assertIn("reconcile_polarities", self.refused(mcp_server.msdial_lipidomics_merge, contract_path="c",
                                                                   reconcile_polarities=value))
        self.refused(mcp_server.msdial_lipidomics_merge, contract_path="c", reconciliation_rt_tolerance="0.2")
        self.assertEqual(1.0, self.capture(mcp_server.msdial_lipidomics_merge, contract_path="c",
                                           reconciliation_rt_tolerance=1)["body"]["reconciliation_rt_tolerance"])

    def test_the_check_tool_says_what_merge_selection_warnings_are(self) -> None:
        self.assertIn("merge_selection_warnings", mcp_server.msdial_lipidomics_check_inputs.__doc__)

    def test_the_contract_tool_defaults_to_a_preview(self) -> None:
        body = self.capture(
            mcp_server.msdial_lipidomics_write_contract, pos_path="p", neg_path="n", lbm_path="l",
            solvent_type="s", retention_time_tolerance=1.5, is_text_pos="a", is_text_neg="b",
            quant_table_path="q", amount_column="H", analysis_root="r", analysis_purpose="x",
            console_path="c", raw_location="copy", check_digest="d",
        )["body"]
        self.assertIs(False, body["confirmed"])
        self.assertIs(False, body["sample_table_confirmed"])


class TheNormalizeJob(_Batch):
    """The backend runs normalize as a job and records what it found, as a Console run is."""

    def setUp(self) -> None:
        super().setUp()
        self.contract = self.batch.contract()
        self.run = write_run(self.batch, self.contract, "Negative")
        self.jobs: dict = {self.run["id"]: self.run}
        for item in (patch.object(server, "JOBS", self.jobs), patch.object(server, "PROCESSES", {}),
                     patch.object(server, "_persist_jobs_locked", lambda: None)):
            item.start()
            self.addCleanup(item.stop)
        self.backend = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.backend.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.backend.shutdown()
        self.backend.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.backend.server_port}{path}", data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload

    def test_a_confirmed_normalize_runs_the_console_and_records_the_verification(self) -> None:
        status, resolved = self.post("/api/lipidomics/resolve", {"contract_path": self.contract, "neg_job_id": self.run["id"]})
        self.assertEqual(200, status, resolved)
        digest = resolved["polarities"]["Negative"]["resolution_digest"]
        body = {"contract_path": self.contract, "job_id": self.run["id"], "resolution_digest": digest}
        status, preview = self.post("/api/lipidomics/normalize", body)
        self.assertEqual(200, status, preview)
        self.assertFalse(preview["started"])
        self.assertFalse((self.batch.analysis / "quant" / "standards_neg.tsv").exists())

        # The Console stand-in copies what normalize would write and prints its report.
        _, _, report = normalize_outputs(preview["plan"], "Negative")
        out = Path(preview["plan"]["paths"]["output_folder"])
        staged = self.batch.root / "staged"
        staged.mkdir()
        for name in (f"{ALIGNMENT}_Height.txt", f"{ALIGNMENT}_NormalizedHeight.txt"):
            (out / name).replace(staged / name)
        script = self.batch.root / "console.py"
        script.write_text(
            "import shutil, sys\n"
            f"staged = {str(staged)!r}\n"
            "out = sys.argv[sys.argv.index('-o') + 1]\n"
            f"for name in {[f'{ALIGNMENT}_Height.txt', f'{ALIGNMENT}_NormalizedHeight.txt']!r}:\n"
            "    shutil.copyfile(staged + '/' + name, out + '/' + name)\n"
            f"print({report!r})\n",
            encoding="utf-8",
        )
        real = server.lab_plan_normalize

        def stand_in(*arguments, **options):
            plan = real(*arguments, **options)
            plan["command"] = [sys.executable, str(script)] + plan["command"][1:]
            return plan

        with patch.object(server, "lab_plan_normalize", side_effect=stand_in):
            status, started = self.post("/api/lipidomics/normalize", {**body, "confirmed": True})
        self.assertEqual(200, status, started)
        self.assertTrue(started["started"])
        job_id = started["job_id"]
        deadline = time.monotonic() + 60
        while self.jobs[job_id]["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(0.05)
        job = self.jobs[job_id]
        self.assertEqual("completed", job["status"], (job.get("error"), job["logs"][-5:]))
        self.assertTrue(job["result"]["verified"])
        record = json.loads(Path(job["result"]["record"]).read_text(encoding="utf-8"))
        self.assertEqual("Negative", record["polarity"])
        self.assertTrue((self.batch.analysis / "quant" / "NormalizedHeight_Neg.txt").is_file())

    def test_the_merge_endpoint_writes_only_when_confirmed(self) -> None:
        runs = {"Positive": write_run(self.batch, self.contract, "Positive"), "Negative": self.run}
        for polarity, accepted in (("Positive", ["Cer 18:1;O2/15:0(d7)"]), ("Negative", [])):
            resolved = resolve_standards_for_contract(self.contract, {polarity: runs[polarity]})
            plan = plan_normalize(self.contract, runs[polarity], resolved["polarities"][polarity]["resolution_digest"],
                                  accept_searched=accepted)
            write_normalize_inputs(plan)
            _, _, report = normalize_outputs(public_normalize_plan(plan), polarity)
            self.assertTrue(finish_normalize(plan, 0, report.splitlines())["completed"])
        output = self.batch.analysis / "quant" / "lipidome" / "lipid_metabolome.csv"
        for body in ({"contract_path": self.contract}, {"contract_path": self.contract, "confirmed": "true"}):
            status, preview = self.post("/api/lipidomics/merge", body)
            self.assertEqual(200, status, preview)
            self.assertFalse(preview["written"])
            self.assertTrue(preview["confirmation_required"])
            self.assertFalse(output.exists())
        status, written = self.post("/api/lipidomics/merge", {"contract_path": self.contract, "confirmed": True})
        self.assertEqual(200, status, written)
        self.assertTrue(written["written"])
        self.assertTrue(output.is_file())

    def test_the_merge_endpoint_takes_the_reconciliation_off_only_for_a_literal_false(self) -> None:
        runs = {"Positive": write_run(self.batch, self.contract, "Positive"), "Negative": self.run}
        for polarity, accepted in (("Positive", ["Cer 18:1;O2/15:0(d7)"]), ("Negative", [])):
            resolved = resolve_standards_for_contract(self.contract, {polarity: runs[polarity]})
            plan = plan_normalize(self.contract, runs[polarity], resolved["polarities"][polarity]["resolution_digest"],
                                  accept_searched=accepted)
            write_normalize_inputs(plan)
            _, _, report = normalize_outputs(public_normalize_plan(plan), polarity)
            self.assertTrue(finish_normalize(plan, 0, report.splitlines())["completed"])
        status, off = self.post("/api/lipidomics/merge", {"contract_path": self.contract, "reconcile_polarities": False})
        self.assertEqual(200, status, off)
        reconciliation = off["report"]["polarity_reconciliation"]
        self.assertFalse(reconciliation["enabled"])
        self.assertEqual(["the positive/negative reconciliation is off; the contract says on"],
                         reconciliation["deviations_from_contract"])
        status, wider = self.post("/api/lipidomics/merge", {"contract_path": self.contract, "reconciliation_rt_tolerance": 0.2})
        self.assertEqual(200, status, wider)
        self.assertEqual((True, 0.2), (wider["report"]["polarity_reconciliation"]["enabled"],
                                       wider["report"]["polarity_reconciliation"]["rt_tolerance_min"]))
        status, on = self.post("/api/lipidomics/merge", {"contract_path": self.contract})
        self.assertEqual((200, True, []), (status, on["report"]["polarity_reconciliation"]["enabled"],
                                           on["report"]["polarity_reconciliation"]["deviations_from_contract"]))
        # Text is no answer: "false" neither turns it off nor leaves it on.
        for value in ("false", "true", 0, 1):
            with self.subTest(value=value):
                status, refused = self.post("/api/lipidomics/merge", {"contract_path": self.contract, "reconcile_polarities": value})
                self.assertEqual(400, status, refused)
                self.assertIn("reconcile_polarities is true or false", refused["error"])
        status, refused = self.post("/api/lipidomics/merge", {"contract_path": self.contract, "reconciliation_rt_tolerance": "0.2"})
        self.assertEqual(400, status, refused)
        self.assertFalse((self.batch.analysis / "quant" / "lipidome" / "lipid_metabolome.csv").exists())

    def test_a_diagnostic_from_the_contract_starts_only_on_the_contracts_raw_folder(self) -> None:
        # The same file names in another folder: the person's original data, say, rather than the copy.
        other = self.batch.root / "original" / "neg"
        shutil.copytree(self.batch.neg, other)
        started: list[str] = []

        def prepare(workflow, representative, directory, **options):
            started.append(str(representative))
            raise ValueError("stopped before the Console")

        answers = {"lab_lipidomics_contract_path": self.contract, "ion_mode": "Negative"}
        with patch.object(server, "prepare_tuning_run", side_effect=prepare):
            for confirmed in (False, True):
                status, refused = self.post("/api/agent/tuning/run",
                                            {"input_path": str(other), "answers": answers, "confirmed": confirmed})
                self.assertEqual(400, status, refused)
                self.assertIn("The diagnostic is refused", refused["error"])
                self.assertIn("input_path must be the contract's negative raw-data folder", refused["error"])
            self.assertEqual([], started)
            status, stopped = self.post("/api/agent/tuning/run",
                                        {"input_path": str(self.batch.neg), "answers": answers, "confirmed": True})
        self.assertIn("stopped before the Console", stopped["error"])
        self.assertEqual([self.batch.neg.resolve()], [Path(item).parent.resolve() for item in started])

    def test_the_endpoint_passes_the_persons_spot_on(self) -> None:
        _, resolved = self.post("/api/lipidomics/resolve", {"contract_path": self.contract, "neg_job_id": self.run["id"]})
        status, refused = self.post("/api/lipidomics/normalize", {
            "contract_path": self.contract, "job_id": self.run["id"], "accept_spot": {"LPC 18:1(d7)": 110},
            "resolution_digest": resolved["polarities"]["Negative"]["resolution_digest"],
        })
        self.assertEqual(400, status)
        self.assertIn("Spot 110 is not a candidate for LPC 18:1(d7)", refused["error"])
        captured = {}
        with patch.object(mcp_server, "_request_json", side_effect=lambda method, path, **options: captured.update(
                options.get("body") or {})):
            mcp_server.msdial_lipidomics_normalize(contract_path="c", job_id="j", resolution_digest="d",
                                                   accept_spot={"LPC 18:1(d7)": 102})
        self.assertEqual({"LPC 18:1(d7)": 102}, captured["accept_spot"])

    def test_a_second_normalize_of_one_polarity_waits_for_the_first(self) -> None:
        _, resolved = self.post("/api/lipidomics/resolve", {"contract_path": self.contract, "neg_job_id": self.run["id"]})
        self.jobs["live"] = {
            "id": "live", "kind": "normalize", "status": "running", "logs": [],
            "preparation": {"lab_lipidomics_contract_path": str(Path(self.contract).resolve()),
                            "lab_lipidomics_polarity": "Negative"},
        }
        status, refused = self.post("/api/lipidomics/normalize", {
            "contract_path": self.contract, "job_id": self.run["id"], "confirmed": True,
            "resolution_digest": resolved["polarities"]["Negative"]["resolution_digest"],
        })
        self.assertEqual(400, status)
        self.assertIn("Normalize job live", refused["error"])
        self.assertFalse((self.batch.analysis / "quant" / "standards_neg.tsv").exists())


if __name__ == "__main__":
    unittest.main()
