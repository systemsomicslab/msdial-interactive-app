"""The laboratory lipidomics agent's steps: check, contract, resolve, normalize, merge.

A synthetic batch (lab_batch_fixture) stands in for the raw folders, the libraries and the workbook;
a production run's outputs and normalize's are written by the fixture, and the Console is a small
Python script that copies them and prints normalize's report. Nothing here runs MS-DIAL.
"""

from __future__ import annotations

import json
import os
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

from lab_batch_fixture import ALIGNMENT, UNIT, Batch, capabilities_patches, normalize_outputs, write_run

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
             "solvent_type", "retention_time_tolerance", "analysis_root", "console_path"],
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
        events = [json.loads(line)["event"] for line in (inputs / "lab-lipidomics-record.jsonl").read_text().splitlines()]
        self.assertEqual(["contract_written"], events)

    def test_an_existing_contract_is_replaced_only_when_asked_and_kept(self) -> None:
        self.batch.contract()
        again = self.write()
        self.assertIn("already exists", again["error"])
        replaced = self.write(replace_existing=True)
        self.assertTrue(replaced["written"])
        self.assertEqual(1, len(list((self.batch.analysis / "inputs").glob("lab-lipidomics-contract.2*.json"))))

    def test_a_changed_input_makes_the_contract_unusable(self) -> None:
        path = self.batch.contract()
        self.batch.workbook.write_bytes(self.batch.workbook.read_bytes() + b"\0")
        with self.assertRaises(LabContractError) as raised:
            load_contract(path)
        self.assertIn("quant table", str(raised.exception))


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
        self.assertEqual(["Blank_Neg"], negative["FA 18:0(d3)"]["blank_zero_height_files"])
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

    def test_dilution_factors_other_than_one_are_applied_by_multiplying(self) -> None:
        contract = json.loads(Path(self.contract).read_text(encoding="utf-8"))
        contract["sample_table"]["rows"][0]["factor"] = 2.0
        Path(self.contract).write_text(json.dumps(contract), encoding="utf-8")
        for run in self.runs.values():
            (Path(run["preparation"]["run_directory"]) / "guided-answers.json").write_text(
                json.dumps({"answers": {"lab_lipidomics_contract_path": self.contract}}), encoding="utf-8")
        plan = self.plan("Negative")
        self.assertIn("-d", plan["command"])
        self.assertIn("multiplies", plan["flags"]["-d"])

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

    def test_the_merge_previews_then_writes_the_lipidome(self) -> None:
        self.normalize("Positive", accept_searched=["Cer 18:1;O2/15:0(d7)"])
        self.normalize("Negative")
        quant = self.batch.analysis / "quant"
        preview = merge_lipidome(self.contract)
        self.assertFalse((quant / "lipid_metabolome.csv").exists())
        self.assertFalse(preview["written"])
        written = merge_lipidome(self.contract, write=True)
        self.assertEqual(preview["output_sha256"], written["output_sha256"])
        self.assertEqual((3, 3), (written["kept_from_positive"], written["kept_from_negative"]))
        self.assertEqual({"AHexCS": 1}, written["not_quantified"]["by_class"])
        text = (quant / "lipid_metabolome.csv").read_bytes()
        self.assertIn(b"\r\n", text)
        self.assertNotIn(b"(d7)", text)
        self.assertTrue((quant / "lipid_metabolome_not_quantified.csv").is_file())
        self.assertTrue((quant / "merge-report.json").is_file())
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
            self.assertEqual(["analysis_purpose", "analysis_root"], [item["id"] for item in result["questions"]])
            self.assertTrue(any("has changed since workset" in item for item in result["warnings"]))
            from msdial_app.agent_workflow import build_guided_plan

            plan = build_guided_plan(str(self.batch.pos), {"ion_mode": "Positive"}, saved["id"])
            self.assertTrue(any("laboratory lipidomics workset" in item for item in plan["blockers"]))

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
    def capture(self, function, **arguments) -> dict:
        captured = {}

        def request(method, path, **options):
            captured.update(path=path, body=options.get("body"))
            return {"ok": True}

        with patch.object(mcp_server, "_request_json", side_effect=request):
            function(**arguments)
        return captured

    def test_only_a_literal_true_confirms(self) -> None:
        for function, arguments, path in (
            (mcp_server.msdial_lipidomics_normalize, {"contract_path": "c", "job_id": "j", "resolution_digest": "d"},
             "/api/lipidomics/normalize"),
            (mcp_server.msdial_lipidomics_merge, {"contract_path": "c"}, "/api/lipidomics/merge"),
        ):
            with self.subTest(tool=function.__name__):
                self.assertEqual(path, self.capture(function, **arguments)["path"])
                self.assertIs(False, self.capture(function, **arguments)["body"]["confirmed"])
                self.assertIs(False, self.capture(function, **arguments, confirmed="yes")["body"]["confirmed"])
                self.assertIs(True, self.capture(function, **arguments, confirmed=True)["body"]["confirmed"])

    def test_the_contract_tool_defaults_to_a_preview(self) -> None:
        body = self.capture(
            mcp_server.msdial_lipidomics_write_contract, pos_path="p", neg_path="n", lbm_path="l",
            solvent_type="s", retention_time_tolerance=1.5, is_text_pos="a", is_text_neg="b",
            quant_table_path="q", amount_column="H", analysis_root="r", analysis_purpose="x",
            console_path="c", check_digest="d",
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
