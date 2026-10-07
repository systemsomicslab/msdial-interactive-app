"""MsdialWorkbench#826 in Interactive: the local anchor outlier test of automatic alignment RT correction.

#826 judges each anchor against the other compounds matched in the file within a local support window
(status LocalOutlier; co-eluting reference candidates count as one compound, the anchor's own left
out), falls back to the run-wide test where fewer than three are found (MadOutlier), floors the scale
at the MS1 cycle around the anchor, and appends audit columns. What is held here:

- the window is written to the method file only when the state sets it, so a Console without #826
  is never handed a key it does not know, and it is validated like the other automatic RT settings;
- the evidence and the audit viewer read both audits: #826's, with LocalOutlier counted as a
  rejection beside MadOutlier and its appended columns read by name, and one written before #826,
  which reads as it always did;
- the Methods text describes the local test and the MS1-cycle floor only for a #826 audit;
- Interactive's own defaults are unchanged, and a campaign profile's answers reach the production
  method but never the zero-threshold diagnostic;
- a public-repository run that corrects RT is refused with a Console that predates #826 even when
  the window is left unset, while a guided analysis may still use an #810 Console.
"""

from __future__ import annotations

import copy
import csv
import hashlib
import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server
from msdial_app.agent_workflow import build_guided_plan
from msdial_app.automatic_rt_evidence import (
    LOCAL_SUPPORT_ANCHOR_COLUMNS,
    OUTLIER_TEST_LOCAL,
    OUTLIER_TEST_OFF,
    OUTLIER_TEST_RUN_WIDE_FLOORED,
    OUTLIER_TEST_RUN_WIDE_MAD,
    automatic_rt_correction_proof,
)
from msdial_app.automatic_rt_review import read_automatic_rt_review
from msdial_app.materials_methods import generate_publication_report
from msdial_app.workflow import (
    AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY,
    AUTOMATIC_RT_CORRECTION_DEFAULTS,
    AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY,
    _write_method,
    console_capabilities,
    console_method_key,
    expand_paths,
    load_parameter_template,
    prepare_run,
    prepare_tuning_run,
    validate_workflow,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"
WINDOW_KEY = "automatic_rt_correction_local_support_rt_window"
WINDOW_LINE = "Automatic RT correction local support RT window"

# The audit TSVs as a Console before #826 (#810) writes them.
SUMMARY_810 = (
    "File ID\tFile name\tFile type\tAnalytical order\tCandidate count\tMatched anchors\tUsed anchors\t"
    "Model source\tReference score\tMedian absolute offset (min)\tMaximum absolute offset (min)\tNote\n"
    "0\tQC-1\tQC\t1\t8\t4\t4\tReference\t0.9\t0\t0\t\n"
    "1\tSample-1\tSample\t2\t8\t4\t2\tDetectedAnchors\t0.8\t0.02\t0.03\t\n"
)
ANCHORS_810 = (
    "File ID\tFile name\tAnchor ID\tm/z\tReference RT (min)\tOriginal RT (min)\tOffset (min)\t"
    "Quality score\tNon-Blank sample coverage\tUsed\tStatus\n"
    "0\tQC-1\t1\t100\t2\t2\t0\t0.8\t1\tTrue\tReference\n"
    "0\tQC-1\t2\t200\t4\t4\t0\t0.8\t1\tTrue\tReference\n"
    "1\tSample-1\t1\t100\t2\t2.03\t-0.03\t0.7\t1\tTrue\tUsed\n"
    "1\tSample-1\t2\t200\t4\t4.01\t-0.01\t0.8\t1\tTrue\tUsed\n"
    "1\tSample-1\t3\t300\t6\t6.4\t-0.4\t0.6\t1\tFalse\tMadOutlier\n"
)
# The same run as a Console with #826 writes it: the columns appended after the old ones.
SUMMARY_826 = (
    SUMMARY_810.splitlines()[0]
    + "\tEstimated scan interval (min)\tFirst used anchor RT (min)\tLast used anchor RT (min)"
    "\tPeaks before first used anchor\tPeaks after last used anchor\n"
    "0\tQC-1\tQC\t1\t8\t4\t4\tReference\t0.9\t0\t0\t\t0.0212\t2\t8\t11\t40\n"
    "1\tSample-1\tSample\t2\t8\t4\t2\tDetectedAnchors\t0.8\t0.02\t0.03\t\t0.0213\t2.03\t4.01\t12\t300\n"
)
ANCHORS_826 = (
    ANCHORS_810.splitlines()[0]
    + "\tOutlier test\tLocal support count\tExpected offset (min)\tOutlier scale (min)"
    "\tMS1 cycle at anchor (min)\n"
    "0\tQC-1\t1\t100\t2\t2\t0\t0.8\t1\tTrue\tReference\t\t\t\t\t0.0212\n"
    "0\tQC-1\t2\t200\t4\t4\t0\t0.8\t1\tTrue\tReference\t\t\t\t\t0.0212\n"
    "1\tSample-1\t1\t100\t2\t2.03\t-0.03\t0.7\t1\tTrue\tUsed\tLocal\t5\t-0.025\t0.0213\t0.0213\n"
    "1\tSample-1\t2\t200\t4\t4.01\t-0.01\t0.8\t1\tTrue\tUsed\tLocal\t4\t-0.012\t0.0213\t0.0213\n"
    "1\tSample-1\t3\t300\t6\t6.4\t-0.4\t0.6\t1\tFalse\tLocalOutlier\tLocal\t6\t-0.02\t0.0213\t0.0213\n"
    "1\tSample-1\t4\t400\t8\t8.3\t-0.3\t0.6\t1\tFalse\tMadOutlier\tGlobal\t1\t-0.02\t0.03\t0.0214\n"
)


def _write_run(
    root: Path,
    summary: str,
    anchors: str,
    method_lines: tuple[str, ...] = (),
    **record_changes,
) -> None:
    """The three Console records as a run leaves them: method file, its key record, then the audits."""
    method = root / "method.txt"
    method.write_text(
        "Execute automatic RT correction for alignment: True\n" + "".join(f"{line}\n" for line in method_lines),
        encoding="utf-8",
    )
    applied = ["Execute automatic RT correction for alignment"] + [line.split(":", 1)[0] for line in method_lines]
    record = {
        "method_file_sha256": hashlib.sha256(method.read_bytes()).hexdigest(),
        "applied": applied,
        "unrecognised": [],
        "unusable": [],
        "blank": [],
    }
    record.update(record_changes)
    keys = root / "method.keys.json"
    keys.write_text(json.dumps(record), encoding="utf-8")
    later = keys.stat().st_mtime + 1
    for name, text in (
        ("automatic_alignment_rt_correction_summary.tsv", summary),
        ("automatic_alignment_rt_correction_anchors.tsv", anchors),
    ):
        (root / name).write_text(text, encoding="utf-8")
        os.utime(root / name, (later, later))


def _workflow() -> dict:
    return {
        "project_type": "lcms",
        "ion_mode": "Negative",
        "target_omics": "Metabolomics",
        "files": [],
        "execute_automatic_rt_correction": True,
    }


def _method_lines(path: str | Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key = console_method_key(line)
        if key is not None:
            values.setdefault(key, []).append(line.split(":", 1)[1].strip() if ":" in line else "")
    return values


class TheWindowIsWrittenOnlyWhenSet(unittest.TestCase):
    def _written(self, state: dict, template_text: str = "Ion mode: Negative\n") -> str:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "template.txt"
            template.write_text(template_text, encoding="ascii")
            method = Path(temporary) / "method.txt"
            _write_method(
                method,
                {"project_type": "lcms", "target_omics": "Metabolomics", "template_path": str(template), **state},
            )
            return method.read_text(encoding="utf-8")

    def test_unset_it_is_not_written(self) -> None:
        for unset in ({}, {WINDOW_KEY: None}, {WINDOW_KEY: ""}, {WINDOW_KEY: "  "}):
            with self.subTest(unset=unset):
                written = self._written({"execute_automatic_rt_correction": True, **unset})
                self.assertIn("Execute automatic RT correction for alignment: True", written)
                self.assertNotIn("local support", written.casefold())

    def test_set_it_is_written_as_the_console_reads_it(self) -> None:
        for value, line in ((1.5, "1.5"), (0, "0"), ("2.5", "2.5"), (2, "2")):
            with self.subTest(value=value):
                written = self._written({"execute_automatic_rt_correction": True, WINDOW_KEY: value})
                self.assertIn(f"{WINDOW_LINE}: {line}\n", written)
                self.assertEqual(1, written.casefold().count("local support rt window"))

    def test_it_is_not_written_when_the_correction_is_off(self) -> None:
        written = self._written({"execute_automatic_rt_correction": False, WINDOW_KEY: 1.5})
        self.assertNotIn("automatic rt correction", written.casefold())

    def test_a_template_line_is_not_copied_past_the_state(self) -> None:
        template = f"Ion mode: Negative\n{WINDOW_LINE}: 2.5\n"
        self.assertNotIn(
            "local support", self._written({"execute_automatic_rt_correction": True}, template).casefold()
        )
        written = self._written({"execute_automatic_rt_correction": True, WINDOW_KEY: 0.75}, template)
        self.assertIn(f"{WINDOW_LINE}: 0.75\n", written)
        self.assertNotIn(f"{WINDOW_LINE}: 2.5", written)

    def test_a_template_line_is_read_into_the_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "template.txt"
            template.write_text(f"Ion mode: Negative\n{WINDOW_LINE}: 2.5\n", encoding="ascii")
            self.assertEqual(2.5, load_parameter_template(template)["workflow"][WINDOW_KEY])
            template.write_text("Ion mode: Negative\n", encoding="ascii")
            self.assertNotIn(WINDOW_KEY, load_parameter_template(template)["workflow"])

    def test_interactive_defaults_are_unchanged(self) -> None:
        self.assertEqual(6, AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_maximum_anchors"])
        self.assertNotIn(WINDOW_KEY, AUTOMATIC_RT_CORRECTION_DEFAULTS)
        self.assertNotIn(WINDOW_KEY, load_parameter_template(TEMPLATE)["workflow"])
        self.assertFalse(load_parameter_template(TEMPLATE)["workflow"]["execute_automatic_rt_correction"])


class TheWindowIsValidated(unittest.TestCase):
    BASE = {
        "project_type": "lcms",
        "execute_automatic_rt_correction": True,
        "files": [],
        "console_path": "",
        "template_path": "",
        "output_root": "",
        "target_omics": "Metabolomics",
    }

    def _errors(self, **state) -> list[str]:
        return [
            item["message"]
            for item in validate_workflow({**self.BASE, **state})
            if item["level"] == "error" and "local support" in item["message"]
        ]

    def test_values_the_console_would_refuse_are_refused(self) -> None:
        for value in (-0.1, "-1", "abc", "1_000", True, float("nan"), "nan", float("inf"), 1e39):
            with self.subTest(value=value):
                self.assertTrue(self._errors(**{WINDOW_KEY: value}))

    def test_zero_and_positive_windows_and_unset_pass(self) -> None:
        for state in ({WINDOW_KEY: 0}, {WINDOW_KEY: 1.5}, {WINDOW_KEY: " 2.5 "}, {WINDOW_KEY: None}, {}):
            with self.subTest(state=state):
                self.assertEqual([], self._errors(**state))

    def test_a_console_without_826_is_refused_only_when_the_window_is_set(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            console = Path(temporary) / "MSDIALCUI.exe"
            console.write_bytes("execute automatic rt correction for alignment".encode("utf-16-le"))
            self.assertNotIn(AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY, console_capabilities(str(console))["capabilities"])
            self.assertTrue(self._errors(console_path=str(console), **{WINDOW_KEY: 1.5}))
            self.assertEqual([], self._errors(console_path=str(console)))

            console.write_bytes(
                "execute automatic rt correction for alignment".encode("utf-16-le")
                + "automatic rt correction local support rt window".encode("utf-16-le")
            )
            found = console_capabilities(str(console))["capabilities"]
            self.assertIn(AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY, found)
            self.assertIn(AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY, found)
            self.assertEqual([], self._errors(console_path=str(console), **{WINDOW_KEY: 1.5}))


class TheEvidenceReadsBothAudits(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_pre_826_audit_reads_as_before(self) -> None:
        _write_run(self.root, SUMMARY_810, ANCHORS_810)
        proof = automatic_rt_correction_proof(self.root)
        self.assertTrue(proof["performed"], proof)
        self.assertEqual(OUTLIER_TEST_RUN_WIDE_MAD, proof["outlier_test"])
        self.assertFalse(proof["local_support_columns"])
        self.assertIsNone(proof["local_support_rt_window"])
        self.assertEqual({"MadOutlier": 1}, proof["outlier_status_counts"])

        review = read_automatic_rt_review(self.root)
        self.assertEqual("verified", review["method_audit"]["status"])
        self.assertFalse(review["local_support_columns"])
        self.assertEqual({"MadOutlier": 1}, review["rejected_status_counts"])
        self.assertIsNone(review["files"][1]["estimated_scan_interval"])
        self.assertEqual("", review["anchors"][2]["outlier_test"])
        self.assertIsNone(review["anchors"][2]["ms1_cycle_time"])
        window = next(item for item in review["outlier_settings"] if "window" in item["label"])
        self.assertIn("predates", window["value"])

    def test_an_826_audit_counts_local_outliers_as_rejections_and_reads_its_columns(self) -> None:
        _write_run(self.root, SUMMARY_826, ANCHORS_826)
        proof = automatic_rt_correction_proof(self.root)
        self.assertTrue(proof["performed"], proof)
        self.assertEqual(OUTLIER_TEST_LOCAL, proof["outlier_test"])
        self.assertTrue(proof["local_support_columns"])
        # No line in method.txt: the Console ran with its default.
        self.assertEqual(1.5, proof["local_support_rt_window"])
        self.assertEqual("console_default", proof["local_support_rt_window_source"])
        self.assertEqual({"LocalOutlier": 1, "MadOutlier": 1}, proof["outlier_status_counts"])
        self.assertEqual({"Local": 3, "Global": 1}, proof["outlier_tests"])

        review = read_automatic_rt_review(self.root)
        self.assertEqual("verified", review["method_audit"]["status"])
        self.assertEqual({"LocalOutlier": 1, "MadOutlier": 1}, review["rejected_status_counts"])
        self.assertEqual({"LocalOutlier": 1, "MadOutlier": 1}, review["outlier_status_counts"])
        local = next(item for item in review["anchors"] if item["status"] == "LocalOutlier")
        self.assertEqual("rejected", local["category"])
        self.assertEqual("Local", local["outlier_test"])
        self.assertEqual(6, local["local_support_count"])
        self.assertAlmostEqual(-0.02, local["expected_offset"])
        self.assertAlmostEqual(0.0213, local["outlier_scale"])
        self.assertAlmostEqual(0.0213, local["ms1_cycle_time"])
        # The reference file's anchors carry the cycle but were not judged.
        reference = review["anchors"][0]
        self.assertEqual("", reference["outlier_test"])
        self.assertIsNone(reference["local_support_count"])
        self.assertAlmostEqual(0.0212, reference["ms1_cycle_time"])
        sample = review["files"][1]
        self.assertAlmostEqual(0.0213, sample["estimated_scan_interval"])
        self.assertEqual(2.03, sample["first_used_anchor_rt"])
        self.assertEqual(300, sample["peaks_after_last_used_anchor"])
        self.assertTrue(any("LocalOutlier (1)" in item for item in review["warnings"]), review["warnings"])
        window = next(item for item in review["outlier_settings"] if "window" in item["label"])
        self.assertEqual("1.5 (Console default; method.txt has no line)", window["value"])
        described = next(item for item in review["outlier_settings"] if item["label"] == "Outlier test")
        self.assertIn("other compounds matched in the file", described["value"])
        self.assertIn("count as one compound", described["value"])
        self.assertNotIn("reference candidates within", described["value"])

    def test_the_window_and_threshold_in_the_method_file_decide_the_test(self) -> None:
        cases = (
            ((f"{WINDOW_LINE}: 2.5",), OUTLIER_TEST_LOCAL, 2.5),
            ((f"{WINDOW_LINE}: 0",), OUTLIER_TEST_RUN_WIDE_FLOORED, 0.0),
            ((f"{WINDOW_LINE}: 1.5", "Automatic RT correction outlier MAD threshold: 0"), OUTLIER_TEST_OFF, 1.5),
        )
        for lines, test, window in cases:
            with self.subTest(lines=lines):
                _write_run(self.root, SUMMARY_826, ANCHORS_826, lines)
                proof = automatic_rt_correction_proof(self.root)
                self.assertTrue(proof["performed"], proof)
                self.assertEqual(test, proof["outlier_test"])
                self.assertEqual(window, proof["local_support_rt_window"])
                self.assertEqual("method_file", proof["local_support_rt_window_source"])

    def test_a_window_a_pre_826_console_did_not_know_is_not_proof(self) -> None:
        _write_run(
            self.root,
            SUMMARY_810,
            ANCHORS_810,
            (f"{WINDOW_LINE}: 1.5",),
            applied=["Execute automatic RT correction for alignment"],
            unrecognised=[WINDOW_LINE],
        )
        proof = automatic_rt_correction_proof(self.root)
        self.assertFalse(proof["performed"])
        self.assertEqual("method_key_not_recognised_by_console", proof["reason"])
        self.assertEqual(["automatic rt correction local support rt window"], proof["discarded_keys"])
        self.assertEqual("method_key_not_recognised_by_console", read_automatic_rt_review(self.root)["method_audit"]["status"])

    def test_a_window_the_console_could_not_use_is_not_proof(self) -> None:
        _write_run(
            self.root,
            SUMMARY_826,
            ANCHORS_826,
            (f"{WINDOW_LINE}: -1",),
            applied=["Execute automatic RT correction for alignment"],
            unusable=[f"{WINDOW_LINE}: -1"],
        )
        proof = automatic_rt_correction_proof(self.root)
        self.assertEqual("method_key_value_discarded_by_console", proof["reason"])

    def test_the_appended_columns_are_read_by_name_whatever_their_order(self) -> None:
        # Readers address columns by name; a reordered or partly filled #826 audit still parses.
        rows = list(csv.reader(ANCHORS_826.splitlines(), delimiter="\t"))
        order = list(reversed(range(len(rows[0]))))
        shuffled = "".join("\t".join(row[index] for index in order) + "\n" for row in rows)
        _write_run(self.root, SUMMARY_826, shuffled)
        review = read_automatic_rt_review(self.root)
        self.assertEqual({"LocalOutlier": 1, "MadOutlier": 1}, review["rejected_status_counts"])
        self.assertTrue(set(LOCAL_SUPPORT_ANCHOR_COLUMNS) <= set(rows[0]))


class TheMethodsTextFollowsTheAudit(unittest.TestCase):
    def _report(self, summary: str, anchors: str, lines: tuple[str, ...] = ()) -> dict:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _write_run(root, summary, anchors, lines)
            result = generate_publication_report(
                _workflow(), None, root, app_version="0.5.32", console_version="5.5"
            )
            with Path(result["supplementary_table"]).open(encoding="utf-8-sig", newline="") as handle:
                result["rows"] = list(csv.DictReader(handle, delimiter="\t"))
            with zipfile.ZipFile(result["supplementary_workbook"]) as workbook:
                result["guided"] = workbook.read("xl/worksheets/sheet2.xml").decode("utf-8")
        return result

    def test_an_826_run_says_neighbours_within_the_window_and_the_ms1_cycle_floor(self) -> None:
        result = self._report(SUMMARY_826, ANCHORS_826)
        text = result["methods_text"]
        self.assertIn("learned distributed anchor features", text)
        # #826's head counts compounds, not reference candidates: co-eluting candidates are one
        # neighbour and the anchor's own compound is no support for it.
        self.assertIn("other compounds matched in the file within 1.5 min of it", text)
        # The merged head groups peak tops within two MS1 scan steps (Ms1CycleProfile.NearestScanIndex),
        # not within 1.5 median cycles; where a file's scans are unknown only identical RTs group.
        self.assertIn("within two MS1 scans of each other in both the reference and the judged file", text)
        self.assertIn("only at identical retention times where a file's scans were unknown", text)
        self.assertNotIn("MS1 cycles of each other", text)
        self.assertIn("counted as one compound", text)
        self.assertIn("the anchor's own compound was not counted as support for it", text)
        self.assertIn("fewer than three were found", text)
        self.assertNotIn("reference candidates within", text)
        self.assertIn("no less than the MS1 cycle time around the anchor", text)
        self.assertIn("by more than 3.5 times a robust scale", text)
        self.assertIn("1 anchor match(es) were rejected by the local test and 1 by the file-wide test", text)
        evidence = {
            row["Parameter"]: row["Value"]
            for row in result["rows"]
            if row["Section"] == "Automatic alignment RT correction evidence"
        }
        self.assertEqual(OUTLIER_TEST_LOCAL, evidence["outlier_test"])
        self.assertEqual("1.5", evidence["local_support_rt_window"])
        self.assertEqual("console_default", evidence["local_support_rt_window_source"])
        self.assertIn("Local outlier test RT window", result["guided"])

    def test_a_zero_window_says_the_file_wide_test_with_the_floor(self) -> None:
        text = self._report(SUMMARY_826, ANCHORS_826, (f"{WINDOW_LINE}: 0",))["methods_text"]
        self.assertIn("from the median offset of the file's anchors by more than 3.5 times", text)
        self.assertIn("MS1 cycle time", text)
        self.assertNotIn("within 0 min", text)
        self.assertNotIn("local test", text)

    def test_a_pre_826_run_claims_neither(self) -> None:
        result = self._report(SUMMARY_810, ANCHORS_810)
        text = result["methods_text"]
        self.assertIn("learned distributed anchor features", text)
        self.assertNotIn("MS1 cycle", text)
        self.assertNotIn("reference candidates within", text)
        self.assertNotIn("compound", text)
        self.assertNotIn("local test", text)
        self.assertIn("not applicable: Console predates MsdialWorkbench#826", result["guided"])

    def test_no_outlier_test_is_described_when_the_threshold_is_zero(self) -> None:
        text = self._report(
            SUMMARY_826, ANCHORS_826, ("Automatic RT correction outlier MAD threshold: 0",)
        )["methods_text"]
        self.assertIn("learned distributed anchor features", text)
        self.assertNotIn("An anchor was rejected", text)


class ACampaignProfileTurnsItOn(unittest.TestCase):
    """The campaign runner merges the prepare's answer seed with its profile's answers and sends the
    same answers to the diagnostic and to the production run (scripts/campaign/machine.py answers())."""

    PROFILE = {
        "execute_rt_correction": False,
        "execute_automatic_rt_correction": True,
        "automatic_rt_correction_maximum_anchors": 12,
        "library_strategy": "none",
        "class_assignment_confirmed": True,
        "generate_materials_methods": False,
        "run_qa": False,
    }

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name in ("qc_01.mzML", "sample_01.mzML"):
            (self.data / name).write_text("x", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(
            "execute automatic rt correction for alignment".encode("utf-16-le")
            + "automatic rt correction local support rt window".encode("utf-16-le")
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _answers(self, profile: dict) -> dict:
        workspace = {
            "separation": "Liquid chromatography",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "target_omics": "Metabolomics",
            "rows": [],
        }
        manifest = {"manifest_path": str(self.root / "provenance" / "run-manifest.json")}
        seed = mcp_server._repository_answer_seed(workspace, manifest, str(self.root / "output"), "keep")
        answers = {**seed, **copy.deepcopy(profile)}
        answers["workflow_overrides"] = dict(seed["workflow_overrides"])
        answers["console_path"] = str(self.console)
        answers["template_path"] = str(TEMPLATE)
        answers["minimum_peak_height"] = 1000
        return answers

    def _plan(self, profile: dict) -> dict:
        plan = build_guided_plan(str(self.data), self._answers(profile))
        self.assertIsNotNone(plan["workflow"], plan["remaining_questions"])
        return plan

    def test_the_profile_answers_reach_the_production_method(self) -> None:
        plan = self._plan(self.PROFILE)
        workflow = plan["workflow"]
        self.assertTrue(workflow["execute_automatic_rt_correction"])
        self.assertEqual(12, workflow["automatic_rt_correction_maximum_anchors"])
        self.assertNotIn(WINDOW_KEY, workflow)
        self.assertFalse(
            [item for item in validate_workflow(workflow) if "automatic rt" in item["message"].casefold()
             and item["level"] == "error"]
        )

        method = _method_lines(prepare_run(copy.deepcopy(workflow))["method_file"])
        self.assertEqual(["True"], method["execute automatic rt correction for alignment"])
        self.assertEqual(["12"], method["automatic rt correction maximum anchors"])
        # Unset by the profile: left to the Console's default, not written.
        self.assertNotIn("automatic rt correction local support rt window", method)

    def test_a_profile_window_reaches_the_production_method(self) -> None:
        plan = self._plan({**self.PROFILE, WINDOW_KEY: 1.5})
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        method = _method_lines(prepare_run(copy.deepcopy(plan["workflow"]))["method_file"])
        self.assertEqual(["1.5"], method["automatic rt correction local support rt window"])
        self.assertEqual(["12"], method["automatic rt correction maximum anchors"])

    def test_the_diagnostic_never_runs_it(self) -> None:
        workflow = self._plan({**self.PROFILE, WINDOW_KEY: 1.5})["workflow"]
        diagnostic = prepare_tuning_run(
            workflow, workflow["files"][0]["file_path"], self.root / "diagnostics" / "job-1"
        )
        written = Path(diagnostic["method_file"]).read_text(encoding="utf-8").casefold()
        self.assertNotIn("automatic rt correction", written)
        # The production state is left as it was.
        self.assertTrue(workflow["execute_automatic_rt_correction"])
        self.assertEqual(1.5, workflow[WINDOW_KEY])

    def test_without_the_profile_answers_interactive_keeps_its_defaults(self) -> None:
        profile = {
            key: value
            for key, value in self.PROFILE.items()
            if not key.startswith(("execute_automatic", "automatic_rt"))
        }
        workflow = self._plan(profile)["workflow"]
        self.assertFalse(workflow["execute_automatic_rt_correction"])
        self.assertEqual(6, workflow["automatic_rt_correction_maximum_anchors"])
        self.assertNotIn(WINDOW_KEY, workflow)
        method = Path(prepare_run(copy.deepcopy(workflow))["method_file"]).read_text(encoding="utf-8")
        self.assertNotIn("automatic rt correction", method.casefold())

    def _use_an_810_console(self) -> None:
        self.console.write_bytes("execute automatic rt correction for alignment".encode("utf-16-le"))
        found = console_capabilities(str(self.console))["capabilities"]
        self.assertIn(AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY, found)
        self.assertNotIn(AUTOMATIC_RT_LOCAL_SUPPORT_CAPABILITY, found)

    @staticmethod
    def _826_errors(workflow: dict) -> list[str]:
        return [
            item["message"]
            for item in validate_workflow(workflow)
            if item["level"] == "error" and "#826" in item["message"]
        ]

    def test_a_repository_run_with_an_810_console_is_refused_with_the_window_unset(self) -> None:
        # The campaign leaves the window to the Console's default; nothing else names #826, so a
        # Console with #810 alone would run the old run-wide test and still finish as corrected.
        self._use_an_810_console()
        plan = build_guided_plan(str(self.data), self._answers(self.PROFILE))
        workflow = plan["workflow"]
        self.assertTrue(workflow["repository_run_manifest"])
        self.assertNotIn(WINDOW_KEY, workflow)
        errors = self._826_errors(workflow)
        self.assertEqual(1, len(errors), errors)
        self.assertIn("public-repository reanalysis", errors[0])
        self.assertFalse(plan["ready_to_prepare"])
        with self.assertRaises(Exception):
            prepare_run(copy.deepcopy(workflow))

    def test_a_repository_run_with_an_810_console_and_the_window_set_is_refused_once(self) -> None:
        self._use_an_810_console()
        workflow = build_guided_plan(str(self.data), self._answers({**self.PROFILE, WINDOW_KEY: 1.5}))["workflow"]
        errors = self._826_errors(workflow)
        self.assertEqual(1, len(errors), errors)
        self.assertIn("local support RT window is set", errors[0])

    def test_the_diagnostic_of_a_repository_run_with_an_810_console_is_not_refused(self) -> None:
        # The diagnostic turns the correction off, so it does not need #826.
        self._use_an_810_console()
        workflow = build_guided_plan(str(self.data), self._answers(self.PROFILE))["workflow"]
        diagnostic = prepare_tuning_run(
            workflow, workflow["files"][0]["file_path"], self.root / "diagnostics" / "job-1"
        )
        written = Path(diagnostic["method_file"]).read_text(encoding="utf-8").casefold()
        self.assertNotIn("automatic rt correction", written)

    def test_a_repository_run_without_the_correction_does_not_need_826(self) -> None:
        self._use_an_810_console()
        profile = {**self.PROFILE, "execute_automatic_rt_correction": False}
        workflow = build_guided_plan(str(self.data), self._answers(profile))["workflow"]
        self.assertEqual([], self._826_errors(workflow))

    def test_a_guided_run_with_an_810_console_and_the_window_unset_is_not_refused(self) -> None:
        self._use_an_810_console()
        answers = self._answers(self.PROFILE)
        answers["workflow_overrides"] = {
            key: value
            for key, value in answers["workflow_overrides"].items()
            if not key.startswith("repository_")
        }
        workflow = build_guided_plan(str(self.data), answers)["workflow"]
        self.assertFalse(str(workflow.get("repository_run_manifest") or "").strip())
        self.assertTrue(workflow["execute_automatic_rt_correction"])
        self.assertEqual([], self._826_errors(workflow))

    def test_a_blank_answer_leaves_the_window_unset(self) -> None:
        workflow = self._plan({**self.PROFILE, WINDOW_KEY: None})["workflow"]
        self.assertNotIn(WINDOW_KEY, workflow)

    def test_the_window_is_a_supported_answer(self) -> None:
        plan = self._plan({**self.PROFILE, WINDOW_KEY: 0})
        self.assertNotIn(WINDOW_KEY, plan.get("unknown_answer_keys") or [])
        self.assertEqual(0, plan["workflow"][WINDOW_KEY])


if __name__ == "__main__":
    unittest.main()
