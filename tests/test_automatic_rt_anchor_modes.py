"""MsdialWorkbench#845 in Interactive: named anchors, the anchor preview and its approval.

The Console's automatic alignment RT correction takes its anchors from the detected peaks (Automatic),
from an anchor library's True rows (UserOnly), or from both (Hybrid, whose False rows keep automatic
candidates out); "Automatic RT correction preview only: True" stops it once the anchors are chosen and
the audit written. What is held here:

- the anchor library is read and checked as strictly as AutomaticRtCorrectionAnchorLibrary.Parse and
  Check read it, and written back in the one format they read;
- the four settings have their own state and answer keys, are written to the method file only when set,
  only for a Console that has #845, and never as the legacy user-defined RT correction's keys; a
  template's preview line is never copied into a production method;
- the preview runs in a directory of its own, writes "preview only: True", loads no annotation library,
  and keeps the record the approval needs; its audit is never evidence of a correction;
- the approval writes the library the author decided on 2026-11-07 (D1-D3), refuses UserOnly when the
  True rows are short (D2), records who approved what, and is found again beside the library;
- the review reads #845's columns and the exclusions file, the Methods text words each mode, and the
  MCP tools, the agent answers and the job kinds carry the new path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server, server
from msdial_app.agent_workflow import SUPPORTED_ANSWER_KEYS, build_guided_plan
from msdial_app.automatic_rt_anchors import (
    LIBRARY_HEADER,
    AnchorLibraryError,
    anchor_library_issues,
    anchor_library_text,
    approval_for_library,
    approve_anchors,
    canonical_anchor_mode,
    canonical_peak_selection_mode,
    parse_anchor_library,
    read_anchor_library,
    write_anchor_library,
    write_preview_record,
)
from msdial_app.automatic_rt_evidence import automatic_rt_correction_proof
from msdial_app.automatic_rt_review import read_automatic_rt_review
from msdial_app.materials_methods import _automatic_rt_anchor_sentence
from msdial_app.supplementary_excel import GUIDED_SECTIONS
from msdial_app.workflow import (
    AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY,
    AUTOMATIC_RT_ANCHOR_MODES_CAPABILITY,
    _write_method,
    console_capabilities,
    console_method_key,
    load_parameter_template,
    prepare_automatic_rt_preview,
    prepare_run,
    validate_workflow,
)
from msdial_app.worksets import DATASET_SCOPED_ANSWERS

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"
MODE = "automatic_rt_correction_anchor_mode"
LIBRARY = "automatic_rt_correction_anchor_library_path"
SELECTION = "automatic_rt_correction_anchor_peak_selection_mode"
WEIGHT = "automatic_rt_correction_anchor_peak_selection_rt_weight"
MARKERS_810 = "execute automatic rt correction for alignment".encode("utf-16-le")
MARKERS_845 = MARKERS_810 + "automatic rt correction anchor mode".encode("utf-16-le") + (
    "automatic rt correction preview only".encode("utf-16-le")
)


def _library(*rows: str) -> str:
    return LIBRARY_HEADER + "\n" + "".join(f"{row}\n" for row in rows)


def _method_lines(path: str | Path) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key = console_method_key(line)
        if key is not None:
            values.setdefault(key, []).append(line.split(":", 1)[1].strip() if ":" in line else "")
    return values


class TheLibraryIsReadAsStrictlyAsTheConsoleReadsIt(unittest.TestCase):
    def test_a_library_reads_into_its_rows(self) -> None:
        rows = parse_anchor_library(
            _library("IS-1\t2.5\t0.2\t200.1\t0.01\t1000\tTrue", "", "Bad\t3.0\t0.1\t300\t0.01\t0\tf").splitlines()
        )
        self.assertEqual(2, len(rows))
        self.assertEqual(
            {"name": "IS-1", "rt": 2.5, "rt_tolerance": 0.2, "mz": 200.1, "mz_tolerance": 0.01,
             "minimum_height": 1000.0, "include": True},
            {key: rows[0][key] for key in ("name", "rt", "rt_tolerance", "mz", "mz_tolerance", "minimum_height", "include")},
        )
        self.assertFalse(rows[1]["include"])
        self.assertEqual(4, rows[1]["line"])

    def test_what_the_console_refuses_is_refused(self) -> None:
        cases = {
            "a row-like first line": "IS-1\t2.5\t0.2\t200\t0.01\t0\tTrue\n",
            "six columns": _library("IS-1\t2.5\t0.2\t200\t0.01\t0"),
            "a decimal comma": _library("IS-1\t2,5\t0.2\t200\t0.01\t0\tTrue"),
            "a thousands separator": _library("IS-1\t2.5\t0.2\t200\t0.01\t1,000\tTrue"),
            "a T/F spelling the Console does not read": _library("IS-1\t2.5\t0.2\t200\t0.01\t0\tyes"),
            "a non-finite number": _library("IS-1\tNaN\t0.2\t200\t0.01\t0\tTrue"),
            "nothing": "\n\n",
        }
        for case, text in cases.items():
            with self.subTest(case=case), self.assertRaises(AnchorLibraryError):
                parse_anchor_library(text.splitlines())

    def test_the_t_f_column_takes_the_four_spellings_in_any_case(self) -> None:
        for flag, expected in (("TRUE", True), ("t", True), ("False", False), ("F", False)):
            with self.subTest(flag=flag):
                rows = parse_anchor_library(_library(f"x\t1\t0.1\t100\t0.01\t0\t{flag}").splitlines())
                self.assertIs(expected, rows[0]["include"])

    def test_a_file_reads_as_file_read_all_lines_reads_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "anchors.txt"
            path.write_bytes(
                b"\xef\xbb\xbf" + _library("a\t1\t0.1\t100\t0.01\t0\tTrue", "b\t2\t0.1\t200\t0.01\t0\tFalse")
                .replace("\n", "\r").encode("ascii")
            )
            self.assertEqual(["a", "b"], [row["name"] for row in read_anchor_library(path)])
            with self.assertRaises(FileNotFoundError):
                read_anchor_library(Path(temporary) / "missing.txt")
            with self.assertRaises(AnchorLibraryError):
                read_anchor_library("")

    def test_the_check_is_the_consoles(self) -> None:
        rows = parse_anchor_library(
            _library("a\t1\t0.1\t100\t0.01\t0\tTrue", "b\t2\t0.1\t200\t0.01\t0\tTrue", "x\t3\t0\t0\t0\t-1\tFalse").splitlines()
        )
        self.assertEqual([], anchor_library_issues(rows, "Automatic", 3))
        short = anchor_library_issues(rows, "UserOnly", 3)
        self.assertTrue(short and "2 True row(s)" in short[0] and "Hybrid" in short[0], short)
        # UserOnly reads no False row, so the broken exclusion does not matter to it.
        self.assertEqual([], anchor_library_issues(rows, "UserOnly", 2))
        # Hybrid reads every row.
        self.assertIn("m/z must be positive", anchor_library_issues(rows, "Hybrid", 2)[0])
        self.assertIn("no rows", anchor_library_issues([], "Hybrid", 2)[0])
        negative = parse_anchor_library(_library("a\t1\t0.1\t100\t0.01\t-1\tTrue").splitlines())
        self.assertIn("minimum height", anchor_library_issues(negative, "UserOnly", 1)[0])
        # A tolerance the Console's single-precision field reads as 0 is not positive there.
        tiny = parse_anchor_library(_library("a\t1\t1e-50\t100\t0.01\t0\tTrue").splitlines())
        self.assertIn("RT tolerance", anchor_library_issues(tiny, "UserOnly", 1)[0])

    def test_what_is_written_reads_back_and_is_ascii(self) -> None:
        rows = [
            {"name": "IS-1", "rt": 2.5, "rt_tolerance": 0.2, "mz": 200.123456789, "mz_tolerance": 0.01,
             "minimum_height": 0, "include": True},
            {"name": "auto2", "rt": 1.339533333333, "rt_tolerance": 0.5, "mz": 254.12, "mz_tolerance": 0.015,
             "minimum_height": 0.0, "include": False},
        ]
        with tempfile.TemporaryDirectory() as temporary:
            path = write_anchor_library(Path(temporary) / "a.txt", rows)
            data = path.read_bytes()
            self.assertTrue(data.decode("ascii").startswith(LIBRARY_HEADER + "\n"))
            reread = read_anchor_library(path)
            self.assertEqual([200.123456789, 254.12], [row["mz"] for row in reread])
            self.assertEqual([True, False], [row["include"] for row in reread])
            self.assertIn("\tFalse\n", data.decode("ascii"))
        for bad in ({"name": ""}, {"name": "caf\u00e9"}, {"name": "a\tb"}, {"rt": float("nan")}):
            with self.subTest(bad=bad), self.assertRaises(AnchorLibraryError):
                anchor_library_text([{**rows[0], **bad}])

    def test_the_modes_are_read_as_the_console_names_them(self) -> None:
        self.assertEqual("UserOnly", canonical_anchor_mode("user_only"))
        self.assertEqual("UserOnly", canonical_anchor_mode("User Only"))
        self.assertEqual("Hybrid", canonical_anchor_mode("HYBRID"))
        self.assertIsNone(canonical_anchor_mode("1"))
        self.assertEqual("ClosestToReferenceRt", canonical_peak_selection_mode("closesttoreferencert"))
        self.assertIsNone(canonical_peak_selection_mode("Closest"))


class TheSettingsAreWrittenOnlyWhenSet(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.library = self.root / "anchors.txt"
        self.library.write_text(
            _library("a\t1\t0.1\t100\t0.01\t0\tTrue", "b\t2\t0.1\t200\t0.01\t0\tTrue", "c\t3\t0.1\t300\t0.01\t0\tTrue"),
            encoding="ascii",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _written(self, state: dict, template_text: str = "Ion mode: Negative\n") -> dict[str, list[str]]:
        template = self.root / "template.txt"
        template.write_text(template_text, encoding="ascii")
        method = self.root / "method.txt"
        _write_method(
            method,
            {"project_type": "lcms", "target_omics": "Metabolomics", "template_path": str(template), **state},
        )
        return _method_lines(method)

    def test_unset_nothing_is_written(self) -> None:
        method = self._written({"execute_automatic_rt_correction": True})
        for key in method:
            self.assertFalse(key.startswith("automatic rt correction anchor"), key)
            self.assertNotEqual("automatic rt correction preview only", key)

    def test_set_each_is_written_as_the_console_reads_it(self) -> None:
        method = self._written(
            {
                "execute_automatic_rt_correction": True,
                MODE: "user_only",
                LIBRARY: str(self.library),
                SELECTION: "weighted",
                WEIGHT: "0.25",
            }
        )
        self.assertEqual(["UserOnly"], method["automatic rt correction anchor mode"])
        self.assertEqual([str(self.library.resolve())], method["automatic rt correction anchor library file path"])
        self.assertEqual(["Weighted"], method["automatic rt correction anchor peak selection mode"])
        self.assertEqual(["0.25"], method["automatic rt correction anchor peak selection rt weight"])
        self.assertNotIn("automatic rt correction preview only", method)

    def test_the_library_is_written_only_for_a_mode_that_reads_it_and_the_weight_only_for_weighted(self) -> None:
        method = self._written(
            {"execute_automatic_rt_correction": True, MODE: "Automatic", LIBRARY: str(self.library),
             SELECTION: "HighestIntensity", WEIGHT: 0.25}
        )
        self.assertEqual(["Automatic"], method["automatic rt correction anchor mode"])
        self.assertNotIn("automatic rt correction anchor library file path", method)
        self.assertNotIn("automatic rt correction anchor peak selection rt weight", method)

    def test_nothing_is_written_when_the_correction_is_off(self) -> None:
        method = self._written({"execute_automatic_rt_correction": False, MODE: "UserOnly", LIBRARY: str(self.library)})
        self.assertFalse([key for key in method if key.startswith("automatic rt correction")])

    def test_they_are_not_the_legacy_keys(self) -> None:
        method = self._written(
            {"execute_automatic_rt_correction": True, MODE: "UserOnly", LIBRARY: str(self.library), SELECTION: "Weighted",
             WEIGHT: 0.9, "rt_correction_peak_selection_mode": "ClosestToReferenceRt",
             "rt_correction_peak_selection_rt_weight": 0.1}
        )
        self.assertEqual(["ClosestToReferenceRt"], method["rt correction peak selection mode"])
        self.assertEqual(["0.1"], method["rt correction peak selection rt weight"])
        self.assertEqual([""], method["compounds library file path for rt correction"])
        self.assertEqual(["Weighted"], method["automatic rt correction anchor peak selection mode"])

    def test_a_template_line_is_never_copied_past_the_state(self) -> None:
        template = (
            "Ion mode: Negative\nAutomatic RT correction preview only: True\n"
            "Automatic RT correction anchor mode: Hybrid\n"
        )
        method = self._written({"execute_automatic_rt_correction": True}, template)
        self.assertNotIn("automatic rt correction preview only", method)
        self.assertNotIn("automatic rt correction anchor mode", method)
        method = self._written({"execute_automatic_rt_correction": True, MODE: "UserOnly", LIBRARY: str(self.library)}, template)
        self.assertEqual(["UserOnly"], method["automatic rt correction anchor mode"])
        self.assertNotIn("automatic rt correction preview only", method)

    def test_a_template_line_is_read_into_the_state_except_the_preview_switch(self) -> None:
        template = self.root / "template.txt"
        template.write_text(
            "Ion mode: Negative\nAutomatic RT correction anchor mode: user only\n"
            "Automatic RT correction anchor library file path: anchors.txt\n"
            "Automatic RT correction anchor peak selection mode: weighted\n"
            "Automatic RT correction anchor peak selection RT weight: 0.3\n"
            "Automatic RT correction preview only: True\n",
            encoding="ascii",
        )
        workflow = load_parameter_template(template)["workflow"]
        self.assertEqual("UserOnly", workflow[MODE])
        self.assertEqual(str(self.library.resolve()), workflow[LIBRARY])
        self.assertEqual("Weighted", workflow[SELECTION])
        self.assertEqual("0.3", workflow[WEIGHT])
        self.assertNotIn("automatic_rt_correction_preview_only", workflow)
        self.assertNotIn(MODE, load_parameter_template(TEMPLATE)["workflow"])


class TheSettingsAreValidated(unittest.TestCase):
    BASE = {
        "project_type": "lcms",
        "execute_automatic_rt_correction": True,
        "files": [],
        "console_path": "",
        "template_path": "",
        "output_root": "",
        "target_omics": "Metabolomics",
    }

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _issues(self, level: str = "error", **state) -> list[str]:
        return [
            item["message"]
            for item in validate_workflow({**self.BASE, **state})
            if item["level"] == level and ("anchor" in item["message"].casefold() or "#845" in item["message"])
        ]

    def _library(self, *rows: str) -> str:
        path = self.root / f"lib{len(list(self.root.iterdir()))}.txt"
        path.write_text(_library(*rows), encoding="ascii")
        return str(path)

    def test_unknown_values_are_refused(self) -> None:
        self.assertTrue(self._issues(**{MODE: "Manual"}))
        self.assertTrue(self._issues(**{SELECTION: "Nearest"}))
        for weight in (-0.1, 1.5, "abc", True, float("nan")):
            with self.subTest(weight=weight):
                self.assertTrue(self._issues(**{WEIGHT: weight}))
        self.assertEqual([], self._issues(**{MODE: "Automatic", SELECTION: "Weighted", WEIGHT: 1}))

    def test_userOnly_and_hybrid_need_a_library_the_console_accepts(self) -> None:
        self.assertIn("needs an anchor library", self._issues(**{MODE: "UserOnly"})[0])
        self.assertIn("not found", self._issues(**{MODE: "Hybrid", LIBRARY: str(self.root / "none.txt")})[0])
        comma = self._library("a\t1,5\t0.1\t100\t0.01\t0\tTrue")
        self.assertIn("decimal point", self._issues(**{MODE: "Hybrid", LIBRARY: comma})[0])
        two = self._library("a\t1\t0.1\t100\t0.01\t0\tTrue", "b\t2\t0.1\t200\t0.01\t0\tTrue")
        short = self._issues(**{MODE: "UserOnly", LIBRARY: two})
        self.assertTrue(short and "UserOnly needs at least the minimum anchor count, 3" in short[0], short)
        self.assertEqual([], self._issues(**{MODE: "UserOnly", LIBRARY: two, "automatic_rt_correction_minimum_anchors": 2}))
        self.assertEqual([], self._issues(**{MODE: "Hybrid", LIBRARY: two}))
        warned = self._issues("warning", **{MODE: "Automatic", LIBRARY: two})
        self.assertTrue(warned and "reads no library" in warned[0])

    def test_a_console_without_845_is_refused_only_when_a_setting_is_used(self) -> None:
        console = self.root / "MSDIALCUI.exe"
        console.write_bytes(MARKERS_810)
        self.assertNotIn(AUTOMATIC_RT_ANCHOR_MODES_CAPABILITY, console_capabilities(str(console))["capabilities"])
        refused = self._issues(console_path=str(console), **{MODE: "Automatic"})
        self.assertTrue(refused and "MsdialWorkbench#845" in refused[0] and "anchor mode" in refused[0], refused)
        self.assertEqual([], self._issues(console_path=str(console)))
        console.write_bytes(MARKERS_845)
        found = console_capabilities(str(console))["capabilities"]
        self.assertIn(AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY, found)
        self.assertIn(AUTOMATIC_RT_ANCHOR_MODES_CAPABILITY, found)
        self.assertEqual([], self._issues(console_path=str(console), **{MODE: "Automatic"}))

    def test_the_capability_is_read_through_console_capabilities(self) -> None:
        console = self.root / "MSDIALCUI.exe"
        console.write_bytes(b"x")
        with patch(
            "msdial_app.workflow.console_capabilities",
            return_value={"capabilities": [AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY]},
        ):
            self.assertTrue(self._issues(console_path=str(console), **{SELECTION: "Weighted"}))
        with patch(
            "msdial_app.workflow.console_capabilities",
            return_value={"capabilities": [AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY, AUTOMATIC_RT_ANCHOR_MODES_CAPABILITY]},
        ):
            self.assertEqual([], self._issues(console_path=str(console), **{SELECTION: "Weighted"}))


# A preview's audit as Console 5.5.261105 writes it: a Hybrid preview with two user anchors (one missing
# in the reference file) and two automatic anchors, file 1 the reference.
SUMMARY_COLUMNS = (
    "File ID\tFile name\tFile type\tAnalytical order\tCandidate count\tMatched anchors\tUsed anchors\tModel source\t"
    "Reference score\tMedian absolute offset (min)\tMaximum absolute offset (min)\tNote\tEstimated scan interval (min)\t"
    "First used anchor RT (min)\tLast used anchor RT (min)\tPeaks before first used anchor\tPeaks after last used anchor\t"
    "Anchor mode\tUser anchors detected\tExcluded automatic candidates\tRun\tAnchor library\tAnchor library SHA-256"
)
ANCHOR_COLUMNS = (
    "File ID\tFile name\tAnchor ID\tm/z\tReference RT (min)\tOriginal RT (min)\tOffset (min)\tQuality score\t"
    "Non-Blank sample coverage\tUsed\tStatus\tOutlier test\tLocal support count\tExpected offset (min)\tOutlier scale (min)\t"
    "MS1 cycle at anchor (min)\tAnchor source\tAnchor name\tLibrary RT (min)\tOutlier flagged\tPeak height\tObserved m/z\tDetection"
)


def _anchor_row(file_id, anchor_id, mz, reference, original, used, status, source, name="", library="", flagged="False",
                height="1000", observed="", detection="", quality="0.8"):
    observed = observed or (str(mz) if original != "" else "")
    return "\t".join(
        str(item) for item in (
            file_id, f"F{file_id}", anchor_id, mz, reference, original, "", "" if source == "User" else quality, 1,
            used, status, "", "", "", "", "0.02", source, name, library, flagged, height if original != "" else "",
            observed, detection,
        )
    )


def _write_preview(root: Path, library_text: str | None, mode: str = "Hybrid", run: str = "Preview") -> dict:
    root.mkdir(parents=True, exist_ok=True)
    library_path = ""
    library_sha = ""
    if library_text is not None:
        library = root.parent / "preview-anchors.txt"
        library.write_text(library_text, encoding="ascii")
        library_path = str(library)
        library_sha = hashlib.sha256(library.read_bytes()).hexdigest()
    (root / "method.txt").write_text(
        "Execute automatic RT correction for alignment: True\nAutomatic RT correction preview only: True\n", encoding="utf-8"
    )
    keys = root / "method.keys.json"
    keys.write_text(
        json.dumps(
            {
                "method_file_sha256": hashlib.sha256((root / "method.txt").read_bytes()).hexdigest(),
                "applied": ["Execute automatic RT correction for alignment", "Automatic RT correction preview only"],
                "unrecognised": [], "unusable": [], "blank": [],
            }
        ),
        encoding="utf-8",
    )
    later = keys.stat().st_mtime + 1
    name = Path(library_path).name if library_path else ""
    summary = SUMMARY_COLUMNS + "\n" + "".join(
        f"{file_id}\tF{file_id}\tSample\t{file_id + 1}\t9\t3\t3\t{source}\t0.9\t0.01\t0.02\t\t0.02\t1\t4\t1\t1\t"
        f"{mode}\t1\t2\t{run}\t{name}\t{library_sha}\n"
        for file_id, source in ((0, "DetectedAnchors"), (1, "Reference"))
    )
    anchors = ANCHOR_COLUMNS + "\n" + "\n".join(
        [
            _anchor_row(1, 1, 150.05, 1.2, 1.2, "True", "Reference", "User", "IS-a", "1.25", detection="The only peak in the window."),
            _anchor_row(0, 1, 150.05, 1.2, 1.22, "True", "Used", "User", "IS-a", "1.25", flagged="True"),
            _anchor_row(1, 2, 222.1, 2.5, 2.5, "True", "Reference", "Automatic", height="5000", observed="222.1001"),
            _anchor_row(0, 2, 222.1, 2.5, 2.52, "True", "Used", "Automatic"),
            _anchor_row(1, 3, 333.2, 3.5, 3.5, "True", "Reference", "Automatic"),
            _anchor_row(0, 3, 333.2, 3.5, 3.56, "False", "LocalOutlier", "Automatic"),
            _anchor_row(1, 4, 400.0, "", "", "False", "MissingInReference", "User", "IS-b", "4.0", detection="No signal at the anchor's m/z."),
            _anchor_row(0, 4, 400.0, "", "", "False", "Missing", "User", "IS-b", "4.0"),
        ]
    ) + "\n"
    files = {"automatic_alignment_rt_correction_summary.tsv": summary, "automatic_alignment_rt_correction_anchors.tsv": anchors}
    if mode == "Hybrid":
        files["automatic_alignment_rt_correction_exclusions.tsv"] = (
            "Anchor name\tm/z\tRT (min)\tm/z tolerance\tRT tolerance (min)\tReference file ID\tReference file name\t"
            "Excluded automatic candidates\nold-reject\t500\t5\t0.01\t0.1\t1\tF1\t0\n"
        )
    for file_name, text in files.items():
        (root / file_name).write_text(text, encoding="utf-8")
        os.utime(root / file_name, (later, later))
    record = {
        "job_id": "job-preview",
        "output_root": str(root.parent / "output"),
        "anchor_mode": mode,
        "anchor_library_path": library_path,
        "anchor_library_sha256": library_sha,
        "match_rt_tolerance": 0.4,
        "alignment_ms1_tolerance": 0.012,
        "minimum_anchors": 3,
    }
    write_preview_record(root, record)
    return record


HYBRID_LIBRARY = _library(
    "IS-a\t1.25\t0.3\t150.05\t0.01\t500\tTrue",
    "old-reject\t5\t0.1\t500\t0.01\t0\tFalse",
    "IS-b\t4.0\t0.2\t400.0\t0.01\t0\tTrue",
)


class ThePreviewAuditIsReviewedNotProved(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "preview"
        _write_preview(self.root, HYBRID_LIBRARY)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_preview_is_never_a_performed_correction(self) -> None:
        proof = automatic_rt_correction_proof(self.root)
        self.assertFalse(proof["performed"])
        self.assertEqual("preview_only", proof["reason"])
        self.assertEqual("Preview", proof["run_kind"])
        self.assertEqual("Hybrid", proof["anchor_mode"])
        self.assertEqual("preview-anchors.txt", proof["anchor_library"])
        self.assertEqual(1, proof["exclusion_count"])
        self.assertEqual(1, proof["corrected_file_count"])

    def test_a_run_audit_with_the_same_rows_is(self) -> None:
        _write_preview(self.root, HYBRID_LIBRARY, run="Run")
        proof = automatic_rt_correction_proof(self.root)
        self.assertTrue(proof["performed"], proof)
        self.assertEqual("Run", proof["run_kind"])
        self.assertEqual(1, proof["selected_user_anchor_count"])
        self.assertEqual(1, proof["selected_automatic_anchor_count"])

    def test_the_review_shows_every_anchor_once_with_the_845_columns(self) -> None:
        review = read_automatic_rt_review(self.root)
        anchor_review = review["anchor_review"]
        self.assertEqual("Preview", anchor_review["run"])
        self.assertEqual(["User-1", "Automatic-2", "Automatic-3", "User-4"], [item["key"] for item in anchor_review["anchors"]])
        user = anchor_review["anchors"][0]
        self.assertEqual(("User", "IS-a", 1.25, 1.2, 150.05), (user["source"], user["name"], user["library_rt"], user["reference_rt"], user["mz"]))
        self.assertTrue(user["outlier_flagged"])
        self.assertEqual("The only peak in the window.", user["detection"])
        self.assertIsNone(user["quality_score"])
        automatic = anchor_review["anchors"][1]
        self.assertEqual((222.1001, 5000.0), (automatic["observed_mz"], automatic["peak_height"]))
        missing = anchor_review["anchors"][3]
        self.assertIsNone(missing["reference_rt"])
        self.assertEqual("MissingInReference", missing["reference_status"])
        self.assertEqual("unmatched", next(item for item in review["anchors"] if item["status"] == "MissingInReference")["category"])
        self.assertEqual([{"anchor_name": "old-reject", "mz": 500.0, "rt": 5.0, "mz_tolerance": 0.01, "rt_tolerance": 0.1,
                           "reference_file_id": "1", "reference_file_name": "F1", "excluded_candidates": 0}], review["exclusions"])
        self.assertEqual("preview_only", review["method_audit"]["status"])
        self.assertTrue(any("preview's" in note for note in review["notes"]))
        self.assertTrue(any("old-reject" in note for note in review["notes"]))
        self.assertTrue(any("IS-a" in warning and "outlier test" in warning for warning in review["warnings"]))
        self.assertFalse(any("do not prove" in warning for warning in review["warnings"]))


class TheApprovalWritesWhatTheAuthorDecided(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.base = Path(self.directory.name)
        self.preview = self.base / "preview"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_d1_d3_hybrid_preview(self) -> None:
        record = _write_preview(self.preview, HYBRID_LIBRARY)
        result = approve_anchors(
            self.preview, ["User-1", "Automatic-2"], ["Automatic-3", "User-4"], "A. Person", write=True,
        )
        rows = read_anchor_library(result["library_path"])
        by_name = {row["name"]: row for row in rows}
        # D3: accepted user anchors keep their row; a rejected one stays as a False row; an earlier False row stays.
        self.assertEqual((1.25, 0.3, 150.05, 0.01, 500.0, True), tuple(by_name["IS-a"][k] for k in ("rt", "rt_tolerance", "mz", "mz_tolerance", "minimum_height", "include")))
        self.assertFalse(by_name["IS-b"]["include"])
        self.assertEqual(0.2, by_name["IS-b"]["rt_tolerance"])
        self.assertFalse(by_name["old-reject"]["include"])
        # D1: an accepted automatic anchor at the reference file's RT and m/z, with the match RT tolerance, the
        # MS1 tolerance for alignment and a minimum height of 0. A rejected one is a False row at the same values.
        automatic = [row for row in rows if row["name"].startswith("auto")]
        self.assertEqual(
            [(2.5, 0.4, 222.1, 0.012, 0.0, True), (3.5, 0.4, 333.2, 0.012, 0.0, False)],
            [tuple(row[k] for k in ("rt", "rt_tolerance", "mz", "mz_tolerance", "minimum_height", "include")) for row in automatic],
        )
        self.assertEqual("auto2_mz222.1000_rt2.500", automatic[0]["name"])
        # D2: two True rows are fewer than the minimum of three, so UserOnly is refused and nothing is chosen.
        self.assertEqual(2, result["true_rows"])
        self.assertFalse(result["user_only_allowed"])
        self.assertTrue(result["hybrid_allowed"])
        self.assertEqual("", result["production_anchor_mode"])
        self.assertIn("Choose Hybrid explicitly", result["message"])
        approval = json.loads(Path(result["approval_record"]).read_text(encoding="utf-8"))
        self.assertEqual("A. Person", approval["approved_by"])
        self.assertEqual("job-preview", approval["preview_job_id"])
        self.assertEqual(hashlib.sha256(Path(result["library_path"]).read_bytes()).hexdigest(), approval["library_sha256"])
        self.assertEqual(
            hashlib.sha256((self.preview / "automatic_alignment_rt_correction_anchors.tsv").read_bytes()).hexdigest(),
            approval["preview_audit_sha256"]["automatic_alignment_rt_correction_anchors.tsv"],
        )
        self.assertIn("automatic_alignment_rt_correction_exclusions.tsv", approval["preview_audit_sha256"])
        self.assertEqual(record["anchor_library_sha256"], approval["preview_anchor_library"]["sha256"])
        self.assertEqual(Path(record["output_root"]).resolve(), Path(result["library_path"]).parent)
        self.assertTrue(Path(result["library_path"]).name.startswith("automatic_rt_anchors_approved_"))
        # The production run would not run UserOnly with it.
        self.assertTrue(anchor_library_issues(rows, "UserOnly", 3))
        self.assertEqual([], anchor_library_issues(rows, "Hybrid", 3))

    def test_d2_user_only_when_the_true_rows_suffice(self) -> None:
        _write_preview(self.preview, None, mode="Automatic")
        # The automatic-only audit rows of the fixture keep their user rows; strip them for an Automatic preview.
        anchors = self.preview / "automatic_alignment_rt_correction_anchors.tsv"
        lines = [line for line in anchors.read_text(encoding="utf-8").splitlines() if "\tUser\t" not in line]
        stamp = anchors.stat().st_mtime
        anchors.write_text("\n".join(lines) + "\n", encoding="utf-8")
        os.utime(anchors, (stamp, stamp))
        result = approve_anchors(self.preview, ["Automatic-2", "Automatic-3"], [], write=False)
        self.assertFalse(result["written"])
        self.assertEqual(2, result["true_rows"])
        self.assertFalse(result["user_only_allowed"])
        record = json.loads((self.preview / "automatic-rt-preview.json").read_text(encoding="utf-8"))
        record["minimum_anchors"] = 2
        (self.preview / "automatic-rt-preview.json").write_text(json.dumps(record), encoding="utf-8")
        result = approve_anchors(self.preview, ["Automatic-2", "Automatic-3"], [], "A. Person", write=True)
        self.assertEqual("UserOnly", result["production_anchor_mode"])
        self.assertTrue(result["user_only_allowed"])
        found = approval_for_library(result["library_path"])
        self.assertTrue(found["matches_library"])
        # Edited after its approval, the library is no longer the approved one.
        with open(result["library_path"], "a", encoding="ascii") as handle:
            handle.write("extra\t1\t0.1\t100\t0.01\t0\tTrue\n")
        self.assertFalse(approval_for_library(result["library_path"])["matches_library"])

    def test_every_anchor_is_decided_once_by_a_named_person(self) -> None:
        _write_preview(self.preview, HYBRID_LIBRARY)
        everything = (["User-1", "Automatic-2"], ["Automatic-3", "User-4"])
        for accepted, rejected, expected in (
            (["User-1"], ["Automatic-3", "User-4"], "neither accepted nor rejected: Automatic-2"),
            (["User-1", "Automatic-2", "User-4"], ["Automatic-3", "User-4"], "accepted and also rejected: User-4"),
            (["User-1", "Automatic-2", "Automatic-9"], ["Automatic-3", "User-4"], "not anchors of this preview: Automatic-9"),
        ):
            with self.subTest(expected=expected), self.assertRaises(ValueError) as caught:
                approve_anchors(self.preview, accepted, rejected)
            self.assertIn(expected, str(caught.exception))
        with self.assertRaises(ValueError) as caught:
            approve_anchors(self.preview, *everything, "  ", write=True)
        self.assertIn("confirmed_by", str(caught.exception))
        self.assertFalse((self.base / "output").exists())

    def test_a_library_changed_since_the_preview_or_a_run_audit_is_refused(self) -> None:
        _write_preview(self.preview, HYBRID_LIBRARY)
        (self.base / "preview-anchors.txt").write_text(HYBRID_LIBRARY + "x\t1\t0.1\t100\t0.01\t0\tFalse\n", encoding="ascii")
        with self.assertRaises(ValueError) as caught:
            approve_anchors(self.preview, ["User-1", "Automatic-2"], ["Automatic-3", "User-4"])
        self.assertIn("changed since the preview", str(caught.exception))
        _write_preview(self.preview, HYBRID_LIBRARY, run="Run")
        with self.assertRaises(ValueError) as caught:
            approve_anchors(self.preview, ["User-1", "Automatic-2"], ["Automatic-3", "User-4"])
        self.assertIn("not a preview's", str(caught.exception))


class ThePreviewRunsApart(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        for name in ("qc_01.mzML", "sample_01.mzML"):
            (self.data / name).write_text("x", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(MARKERS_845)
        self.library = self.root / "anchors.txt"
        self.library.write_text(
            _library("a\t1\t0.1\t100\t0.01\t0\tTrue", "b\t2\t0.1\t200\t0.01\t0\tTrue", "c\t3\t0.1\t300\t0.01\t0\tTrue"),
            encoding="ascii",
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _answers(self, **extra) -> dict:
        return {
            "project_type": "lcms",
            "ion_mode": "Positive",
            "target_omics": "Metabolomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "execute_automatic_rt_correction": True,
            "library_strategy": "none",
            "class_assignment_confirmed": True,
            "generate_materials_methods": False,
            "run_qa": False,
            "console_path": str(self.console),
            "template_path": str(TEMPLATE),
            "output_root": str(self.root / "output"),
            **extra,
        }

    def test_the_answers_reach_the_production_method(self) -> None:
        plan = build_guided_plan(str(self.data), self._answers(**{MODE: "UserOnly", LIBRARY: str(self.library), SELECTION: "Weighted", WEIGHT: 0.4}))
        self.assertTrue(plan["ready_to_prepare"], plan.get("blockers"))
        prepared = prepare_run(copy.deepcopy(plan["workflow"]))
        method = _method_lines(prepared["method_file"])
        self.assertEqual(["UserOnly"], method["automatic rt correction anchor mode"])
        self.assertEqual(["0.4"], method["automatic rt correction anchor peak selection rt weight"])
        self.assertNotIn("automatic rt correction preview only", method)
        record = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))["automatic_rt_correction_anchor_library"]
        self.assertEqual(("UserOnly", str(self.library.resolve())), (record["anchor_mode"], record["path"]))
        self.assertIsNone(record["approval"])
        import zipfile

        with zipfile.ZipFile(prepared["bundle"]) as bundle:
            self.assertTrue(any(name.endswith("anchors.txt") for name in bundle.namelist()), bundle.namelist())

    def test_only_the_preview_preparation_writes_the_preview_switch(self) -> None:
        workflow = build_guided_plan(str(self.data), self._answers())["workflow"]
        # As workflow_overrides or a saved state could carry it into a production run.
        workflow["automatic_rt_correction_preview_only"] = True
        method = _method_lines(prepare_run(copy.deepcopy(workflow))["method_file"])
        self.assertNotIn("automatic rt correction preview only", method)
        self.assertEqual(["True"], method["execute automatic rt correction for alignment"])

    def test_userOnly_without_a_library_is_asked_for(self) -> None:
        plan = build_guided_plan(str(self.data), self._answers(**{MODE: "UserOnly"}))
        self.assertIn(LIBRARY, [item["id"] for item in plan["remaining_questions"]])

    def test_the_preview_runs_in_its_own_directory_without_libraries(self) -> None:
        workflow = build_guided_plan(str(self.data), self._answers())["workflow"]
        before = copy.deepcopy(workflow)
        preview_root = self.root / "output" / ".msdial-diagnostics" / "job-1"
        prepared = prepare_automatic_rt_preview(workflow, preview_root, "job-1")
        self.assertEqual(before, workflow)
        self.assertEqual(str(preview_root.resolve()), prepared["run_directory"])
        method = _method_lines(prepared["method_file"])
        self.assertEqual(["True"], method["automatic rt correction preview only"])
        self.assertEqual(["True"], method["execute automatic rt correction for alignment"])
        self.assertEqual([""], method["msp file path"])
        self.assertNotIn("-p", prepared["command"])
        self.assertFalse((self.root / "output" / "method.txt").exists())
        self.assertEqual(
            [str(preview_root.resolve() / "automatic_alignment_rt_correction_summary.tsv"),
             str(preview_root.resolve() / "automatic_alignment_rt_correction_anchors.tsv")],
            json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))["expected_analysis_exports"],
        )
        record = json.loads((preview_root / "automatic-rt-preview.json").read_text(encoding="utf-8"))
        self.assertEqual(("job-1", "Automatic", 0.5, 3), (record["job_id"], record["anchor_mode"], record["match_rt_tolerance"], record["minimum_anchors"]))
        self.assertEqual(str((self.root / "output").resolve()), record["output_root"])
        with self.assertRaises(ValueError):
            prepare_automatic_rt_preview({**workflow, "execute_automatic_rt_correction": False}, preview_root)


class TheMethodsTextWordsEachMode(unittest.TestCase):
    EVIDENCE = {"anchor_library": "approved.txt", "anchor_library_sha256": "abc"}

    def test_each_mode(self) -> None:
        self.assertIn("learned distributed anchor features", _automatic_rt_anchor_sentence({}, {}))
        user_only = _automatic_rt_anchor_sentence({**self.EVIDENCE, "anchor_mode": "UserOnly"}, {})
        self.assertIn("alone", user_only)
        self.assertIn("approved.txt (SHA-256 abc)", user_only)
        self.assertNotIn("preview", user_only)
        hybrid = _automatic_rt_anchor_sentence({**self.EVIDENCE, "anchor_mode": "Hybrid"}, {})
        self.assertIn("filled the retention-time bins", hybrid)
        self.assertIn("False rows", hybrid)

    def test_an_approval_is_said_only_for_the_library_that_ran(self) -> None:
        approval = {"library_sha256": "ABC", "rows": {"automatic_accepted": 5, "automatic_rejected": 1}}
        text = _automatic_rt_anchor_sentence(
            {**self.EVIDENCE, "anchor_mode": "UserOnly"}, {"automatic_rt_correction_anchor_approval": approval}
        )
        self.assertIn("reviewed in a preview run", text)
        self.assertIn("5 anchor(s) were accepted and 1 rejected", text)
        other = _automatic_rt_anchor_sentence(
            {**self.EVIDENCE, "anchor_mode": "UserOnly"},
            {"automatic_rt_correction_anchor_approval": {**approval, "library_sha256": "def"}},
        )
        self.assertNotIn("preview", other)


class TheAgentPathCarriesIt(unittest.TestCase):
    def test_answer_keys_and_reports(self) -> None:
        for key in (MODE, LIBRARY, SELECTION, WEIGHT):
            self.assertIn(key, SUPPORTED_ANSWER_KEYS)
        self.assertNotIn("automatic_rt_correction_preview_only", SUPPORTED_ANSWER_KEYS)
        self.assertIn(LIBRARY, DATASET_SCOPED_ANSWERS)
        listed = {key for section, fields in GUIDED_SECTIONS for key, _, _ in fields}
        for key in (MODE, LIBRARY, SELECTION, WEIGHT, "automatic_rt_correction_anchor_approval"):
            self.assertIn(key, listed)

    def test_the_preview_is_a_console_job(self) -> None:
        for kinds in (server.CANCELLABLE_JOB_KINDS, server.CONSOLE_JOB_KINDS, server.UNIT_JOB_KINDS):
            self.assertIn("automatic_rt_preview", kinds)

    def test_the_mcp_tools_call_their_endpoints(self) -> None:
        calls = []

        def fake(method, path, **kwargs):
            calls.append((method, path, kwargs.get("body")))
            return {"anchor_review": {"anchors": []}, "run_directory": "d"}

        with patch.object(mcp_server, "_request_json", side_effect=fake):
            mcp_server.msdial_start_automatic_rt_anchor_preview("data", {"a": 1})
            mcp_server.msdial_automatic_rt_anchor_review(job_id="j")
            mcp_server.msdial_approve_automatic_rt_anchors(["Automatic-1"], ["User-1"], job_id="j")
        self.assertEqual("/api/agent/automatic-rt/preview", calls[0][1])
        self.assertIs(False, calls[0][2]["confirmed"])
        self.assertEqual("/api/automatic-rt/review", calls[1][1])
        self.assertEqual(("/api/automatic-rt/approve", False, ["Automatic-1"], ["User-1"]),
                         (calls[2][1], calls[2][2]["confirmed"], calls[2][2]["accepted"], calls[2][2]["rejected"]))

    def test_the_approval_request_writes_only_when_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            preview = Path(temporary) / "preview"
            _write_preview(preview, HYBRID_LIBRARY)
            body = {"preview_directory": str(preview), "accepted": ["User-1", "Automatic-2"],
                    "rejected": ["Automatic-3", "User-4"], "confirmed_by": "A. Person"}
            shown = server._approve_automatic_rt_anchors_request(body)
            self.assertTrue(shown["confirmation_required"])
            self.assertFalse(shown["written"])
            self.assertFalse((Path(temporary) / "output").exists())
            written = server._approve_automatic_rt_anchors_request({**body, "confirmed": True})
            self.assertTrue(Path(written["library_path"]).is_file())
            with patch.dict(server.JOBS, {"j": {"kind": "run", "status": "completed"}}):
                with self.assertRaises(ValueError):
                    server._approve_automatic_rt_anchors_request({**body, "job_id": "j"})


if __name__ == "__main__":
    unittest.main()
