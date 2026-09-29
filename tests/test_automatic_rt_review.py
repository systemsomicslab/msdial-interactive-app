import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.automatic_rt_evidence import discarded_method_keys
from msdial_app.automatic_rt_review import _correct_rt, _smooth_eic, extract_anchor_eic, read_automatic_rt_review


SUMMARY = (
    "File ID\tFile name\tFile type\tAnalytical order\tCandidate count\tMatched anchors\tUsed anchors\tModel source\tReference score\tMedian absolute offset (min)\tMaximum absolute offset (min)\tNote\n"
    "0\tQC-1\tQC\t1\t5\t2\t2\tReference\t0.9\t0\t0\t\n"
    "1\tSample-1\tSample\t2\t5\t2\t2\tDetectedAnchors\t0.8\t0.02\t0.03\t\n"
)
ANCHORS = (
    "File ID\tFile name\tAnchor ID\tm/z\tReference RT (min)\tOriginal RT (min)\tOffset (min)\tQuality score\tNon-Blank sample coverage\tUsed\tStatus\n"
    "0\tQC-1\t1\t100\t2\t2\t0\t0.8\t1\tTrue\tReference\n"
    "0\tQC-1\t2\t200\t4\t4\t0\t0.8\t1\tTrue\tReference\n"
    "1\tSample-1\t1\t100\t2\t2.03\t-0.03\t0.7\t1\tTrue\tUsed\n"
    "1\tSample-1\t2\t200\t4\t4.01\t-0.01\t0.8\t1\tTrue\tUsed\n"
    "1\tSample-1\t3\t300\t4\t4.01\t-0.01\t0.5\t1\tFalse\tNonMonotonic\n"
)


class AutomaticRtReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "method.txt").write_text(
            "Execute automatic RT correction for alignment: True\nSmoothing method: LinearWeightedMovingAverage\n"
            "Smoothing level: 1\nMS1 tolerance for centroid: 0.01\n"
            "Automatic RT correction minimum Gaussian similarity: 0\n", encoding="utf-8")
        digest = hashlib.sha256((self.root / "method.txt").read_bytes()).hexdigest()
        record = {
            "method_file_sha256": digest,
            "applied": ["Execute automatic RT correction for alignment"],
            "unrecognised": ["GNPS export"],
            "unusable": [],
            "blank": ["Instrument"],
        }
        (self.root / "method.keys.json").write_text(json.dumps(record), encoding="utf-8")
        self.summary = self.root / "automatic_alignment_rt_correction_summary.tsv"
        self.anchors = self.root / "automatic_alignment_rt_correction_anchors.tsv"
        self.summary.write_text(SUMMARY, encoding="utf-8")
        self.anchors.write_text(ANCHORS, encoding="utf-8")

    def tearDown(self):
        self.temporary.cleanup()

    def _rewrite_record(self, method_text=None, **changes):
        """Change method.keys.json (and method.txt) as a different run would have written it, then the audits after it."""
        keys = self.root / "method.keys.json"
        record = json.loads(keys.read_text(encoding="utf-8"))
        if method_text is not None:
            (self.root / "method.txt").write_text(method_text, encoding="utf-8")
            record["method_file_sha256"] = hashlib.sha256((self.root / "method.txt").read_bytes()).hexdigest()
        record.update(changes)
        keys.write_text(json.dumps(record), encoding="utf-8")
        later = keys.stat().st_mtime + 1
        for path in (self.summary, self.anchors):
            os.utime(path, (later, later))

    def _prepare_eic_run(self):
        """Give the run a raw file, its input CSV, a manifest and a Console; return a fake EIC run."""
        raw = self.root / "Sample-1.mzML"
        raw.write_text("", encoding="ascii")
        console = self.root / "MSDIALCUI.exe"
        console.write_text("", encoding="ascii")
        (self.root / "analysis_files.csv").write_text(
            "file_path,file_name,acquisition_type\n"
            f"{self.root / 'QC-1.mzML'},QC-1,DDA\n"
            f"{raw},Sample-1,DDA\n",
            encoding="utf-8",
        )
        (self.root / "run-manifest.json").write_text(
            json.dumps({"console": {"path": str(console)}}), encoding="utf-8"
        )

        def fake_run(command, **_kwargs):
            self.assertEqual(2, command.count("-target"))
            self.assertEqual("--acquisitiontype", command[-2])
            output = Path(command[command.index("--output") + 1])
            output.write_text(
                "ScanId,RT,TargetMz,Tolerance,Intensity\n0,1.5,100,0.01,1000\n"
                "1,2.03,100,0.01,125\n2,2.1,100,0.01,80\n3,2.5,100,0.01,5000\n",
                encoding="ascii",
            )
            return type("Completed", (), {"returncode": 0, "stdout": "", "stderr": ""})()

        return fake_run

    def test_reads_matched_and_rejected_anchors_with_verified_method(self):
        result = read_automatic_rt_review(self.root)
        self.assertEqual("verified", result["method_audit"]["status"])
        self.assertEqual("QC-1", result["reference"]["name"])
        self.assertEqual(1, result["model_counts"]["DetectedAnchors"])
        self.assertEqual(1, result["rejected_status_counts"]["NonMonotonic"])
        self.assertAlmostEqual(-0.03, result["anchors"][2]["offset"])
        self.assertIn("GNPS export", result["method_audit"]["unrecognised"])
        self.assertEqual("LinearWeightedMovingAverage", result["smoothing_settings"]["method"])
        self.assertEqual("0", next(item["value"] for item in result["selection_settings"] if item["label"] == "Minimum Gaussian similarity"))

    def test_stale_audit_cannot_claim_verified_run(self):
        old = (self.root / "method.keys.json").stat().st_mtime - 60
        os.utime(self.anchors, (old, old))
        result = read_automatic_rt_review(self.root)
        self.assertEqual("audit_older_than_method_key_record", result["method_audit"]["status"])

    def test_replaced_method_cannot_claim_verified_run(self):
        (self.root / "method.txt").write_text("Different method\n", encoding="utf-8")
        result = read_automatic_rt_review(self.root)
        self.assertEqual("method_key_record_not_from_this_method_file", result["method_audit"]["status"])

    def test_rejects_incomplete_audit(self):
        self.summary.write_text("File ID\tFile name\n0\tQC-1\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "missing columns"):
            read_automatic_rt_review(self.root)

    def test_missing_and_ambiguous_anchors_remain_in_table_without_blocking_model(self):
        with self.anchors.open("a", encoding="utf-8") as handle:
            handle.write("1\tSample-1\t4\t400\t5\t\t\t0\t0.6\tFalse\tMissing\n")
            handle.write("1\tSample-1\t5\t500\t6\tNaN\t\t0\t0.6\tFalse\tAmbiguous\n")
        result = read_automatic_rt_review(self.root)
        missing = result["anchors"][-2:]
        self.assertEqual(["Missing", "Ambiguous"], [item["status"] for item in missing])
        self.assertTrue(all(item["original_rt"] is None for item in missing))
        self.assertTrue(all(item["offset"] is None and not item["rt_available"] for item in missing))
        self.assertTrue(result["files"][1]["model_reconstructable"])
        # Matching no single peak is not a rejection by the model, and it explains the missing RT.
        self.assertEqual({"Missing": 1, "Ambiguous": 1}, result["unmatched_status_counts"])
        self.assertEqual({"NonMonotonic": 1}, result["rejected_status_counts"])
        self.assertEqual(["unmatched", "unmatched"], [item["category"] for item in missing])
        self.assertTrue(any("2 anchor record(s) in non-Blank files matched no single peak" in item for item in result["notes"]))
        self.assertFalse(any("unavailable RT" in item for item in result["warnings"]))
        self.assertIn("Rejected anchors require review: NonMonotonic (1)", result["warnings"])
        self.assertAlmostEqual(2.0, _correct_rt(2.03, result["anchors"][2:]))
        json.dumps(result, allow_nan=False)

    def test_missing_rt_on_used_anchor_disables_model_without_hiding_other_files(self):
        self.anchors.write_text(ANCHORS.replace(
            "1\tSample-1\t2\t200\t4\t4.01", "1\tSample-1\t2\t200\t4\tinf"
        ), encoding="utf-8")
        result = read_automatic_rt_review(self.root)
        self.assertTrue(result["files"][0]["model_reconstructable"])
        self.assertFalse(result["files"][1]["model_reconstructable"])
        self.assertTrue(result["anchors"][3]["used"])
        self.assertIsNone(_correct_rt(2.03, result["anchors"][2:]))
        self.assertTrue(any("marked Used" in item for item in result["warnings"]))

    def test_file_with_only_missing_anchors_remains_reviewable(self):
        self.summary.write_text(SUMMARY.replace(
            "1\tSample-1\tSample\t2\t5\t2\t2\tDetectedAnchors",
            "1\tSample-1\tSample\t2\t5\t0\t0\tUncorrected",
        ), encoding="utf-8")
        self.anchors.write_text(
            "\n".join(ANCHORS.splitlines()[:3]) + "\n"
            "1\tSample-1\t1\t100\t2\t\t\t0\t0.6\tFalse\tMissing\n",
            encoding="utf-8",
        )
        result = read_automatic_rt_review(self.root)
        self.assertEqual(2, len(result["files"]))
        self.assertFalse(result["files"][1]["model_reconstructable"])
        self.assertEqual("Missing", result["anchors"][-1]["status"])

    def test_missing_anchor_eic_does_not_invent_an_apex_or_read_raw(self):
        with self.anchors.open("a", encoding="utf-8") as handle:
            handle.write("1\tSample-1\t4\t400\t5\t\t\t0\t0.6\tFalse\tMissing\n")
        with patch("msdial_app.automatic_rt_review.subprocess.run") as run:
            with self.assertRaisesRegex(ValueError, "no finite.*RT"):
                extract_anchor_eic(self.root, "1", "4", 0.01)
        run.assert_not_called()

    def test_eic_uses_two_target_flags_and_projects_rt_without_changing_intensity(self):
        fake_run = self._prepare_eic_run()
        with patch("msdial_app.automatic_rt_review.subprocess.run", side_effect=fake_run):
            result = extract_anchor_eic(self.root, "1", "1", 0.01)
        self.assertEqual(2, len(result["points"]))
        self.assertEqual(125, result["points"][0]["intensity"])
        self.assertAlmostEqual(2.0, result["points"][0]["corrected_rt"])
        self.assertTrue(result["smoothing"]["available"])
        self.assertAlmostEqual(332.5, result["points"][0]["smoothed_intensity"])
        self.assertAlmostEqual(1321.25, result["points"][1]["smoothed_intensity"])
        self.assertEqual(125, result["points"][0]["intensity"])
        with patch("msdial_app.automatic_rt_review.subprocess.run", side_effect=fake_run), patch(
            "msdial_app.automatic_rt_review._smooth_eic", side_effect=ValueError("Unsupported recorded method")
        ):
            raw_only = extract_anchor_eic(self.root, "1", "1", 0.01)
        self.assertFalse(raw_only["smoothing"]["available"])
        self.assertIn("Unsupported", raw_only["smoothing"]["note"])
        self.assertTrue(all(point["smoothed_intensity"] is None for point in raw_only["points"]))
        self.assertEqual([125, 80], [point["intensity"] for point in raw_only["points"]])

    def test_smoothing_matches_commonstandard_weights_and_edge_padding(self):
        points = [{"original_rt": i, "intensity": value} for i, value in enumerate([10, 20, 30])]
        self.assertEqual([12.5, 20, 27.5], _smooth_eic(points, "LinearWeightedMovingAverage", 1))
        self.assertEqual([40 / 3, 20, 80 / 3], _smooth_eic(points, "SimpleMovingAverage", 1))
        self.assertEqual([10, 20, 30], _smooth_eic(points, "LinearWeightedMovingAverage", 0))
        self.assertEqual([], _smooth_eic([], "LinearWeightedMovingAverage", 3))

    def test_time_based_smoothing_matches_uniform_interior_and_irregular_weights(self):
        points = [{"original_rt": i, "intensity": value} for i, value in enumerate([10, 50, 20, 70, 30])]
        a = _smooth_eic(points, "LinearWeightedMovingAverage", 1)
        b = _smooth_eic(points, "TimeBasedLinearWeightedMovingAverage", 1)
        self.assertEqual(a[1:-1], b[1:-1])
        irregular = [{"original_rt": time, "intensity": value} for time, value in zip([0, 1, 4], [0, 10, 20])]
        expected = [15 / 3.5, 7.5, 18]
        for actual, target in zip(_smooth_eic(irregular, "TimeBasedLinearWeightedMovingAverage", 1), expected):
            self.assertAlmostEqual(target, actual)
        self.assertEqual([10], _smooth_eic(points[:1], "TimeBasedLinearWeightedMovingAverage", 3))

    def test_smoothing_does_not_silently_replace_an_unsupported_method(self):
        with self.assertRaisesRegex(ValueError, "not implemented"):
            _smooth_eic([], "SavitzkyGolayFilter", 3)
        with self.assertRaisesRegex(ValueError, "between 0 and 100"):
            _smooth_eic([], "LinearWeightedMovingAverage", -1)
        with self.assertRaisesRegex(ValueError, "strictly increasing"):
            _smooth_eic([{"original_rt": 0, "intensity": 1}] * 2, "TimeBasedLinearWeightedMovingAverage", 3)

    def test_method_bom_and_path_colons_do_not_change_recorded_smoothing(self):
        from msdial_app.automatic_rt_review import _method_values
        path = self.root / "other-method.txt"
        path.write_text("# Smoothing level: 99\nSmoothing method: LinearWeightedMovingAverage\nSmoothing level: 3\nMsp file path: D:\\library.msp\n", encoding="utf-8-sig")
        self.assertEqual("3", _method_values(path)["smoothing level"])
        self.assertEqual("D:\\library.msp", _method_values(path)["msp file path"])

    def test_eic_does_not_read_raw_when_method_audit_is_stale(self):
        old = (self.root / "method.keys.json").stat().st_mtime - 60
        os.utime(self.anchors, (old, old))
        with self.assertRaisesRegex(ValueError, "provenance"):
            extract_anchor_eic(self.root, "1", "1", 0.01)

    # Blank anchors are unused by design, not rejected.

    def test_blank_anchors_are_reported_apart_from_rejections(self):
        # The Console gives a Blank's matched anchors a Blank status and leaves those that
        # matched no peak Missing or Ambiguous; none of them is ever used. The Blank's model
        # comes from its neighbours, or it keeps its original RT.
        for status, model in (("BlankInterpolateByOrder", "InterpolatedBlank"), ("BlankNotCorrected", "Uncorrected")):
            with self.subTest(status):
                self.summary.write_text(
                    SUMMARY + f"2\tBlank-1\tBlank\t3\t5\t2\t0\t{model}\t0\t0\t0\tBlank note\n",
                    encoding="utf-8")
                self.anchors.write_text(
                    "\n".join(ANCHORS.splitlines()[:5]) + "\n"
                    f"2\tBlank-1\t1\t100\t2\t2.05\t-0.05\t0.6\t1\tFalse\t{status}\n"
                    f"2\tBlank-1\t2\t200\t4\t4.02\t-0.02\t0.6\t1\tFalse\t{status}\n"
                    "2\tBlank-1\t3\t300\t4\t\t\t0\t1\tFalse\tMissing\n",
                    encoding="utf-8")

                result = read_automatic_rt_review(self.root)

                self.assertEqual({}, result["rejected_status_counts"])
                self.assertEqual({}, result["unmatched_status_counts"])
                self.assertEqual({status: 2, "Missing": 1}, result["blank_status_counts"])
                self.assertFalse(any("Rejected anchors" in item for item in result["warnings"]))
                self.assertFalse(any("unavailable RT" in item for item in result["warnings"]))
                self.assertTrue(any("3 anchor record(s) are in Blank files, not used by design" in item for item in result["notes"]))
                self.assertEqual(["blank"] * 3, [item["category"] for item in result["anchors"] if item["file_id"] == "2"])
                self.assertEqual("verified", result["method_audit"]["status"])

    def test_rejections_are_still_warned_beside_blank_anchors(self):
        self.summary.write_text(SUMMARY + "2\tBlank-1\tBlank\t3\t5\t1\t0\tInterpolatedBlank\t0\t0\t0\t\n", encoding="utf-8")
        with self.anchors.open("a", encoding="utf-8") as handle:
            handle.write("1\tSample-1\t4\t400\t5\t5.9\t-0.9\t0.4\t1\tFalse\tMadOutlier\n")
            handle.write("1\tSample-1\t5\t500\t6\t6.01\t-0.01\t0.4\t1\tFalse\tSomeFutureStatus\n")
            handle.write("2\tBlank-1\t1\t100\t2\t2.05\t-0.05\t0.6\t1\tFalse\tBlankInterpolateByOrder\n")
        result = read_automatic_rt_review(self.root)
        # A status this viewer does not know is a rejection until someone says otherwise.
        self.assertIn("Rejected anchors require review: MadOutlier (1), NonMonotonic (1), SomeFutureStatus (1)", result["warnings"])
        self.assertEqual({"BlankInterpolateByOrder": 1}, result["blank_status_counts"])

    def test_a_rejected_anchor_without_rt_is_still_warned(self):
        # Only Missing and Ambiguous explain an absent RT.
        with self.anchors.open("a", encoding="utf-8") as handle:
            handle.write("1\tSample-1\t4\t400\t5\t\t\t0.4\t1\tFalse\tMadOutlier\n")
        result = read_automatic_rt_review(self.root)
        self.assertTrue(any(item.startswith("1 anchor record(s) other than Missing or Ambiguous have unavailable RT")
                            for item in result["warnings"]))

    # A smoothing value the Console discarded stops the smoothed preview.

    def test_method_key_names_are_read_as_the_console_records_them(self):
        record = {
            "unusable": ["Smoothing level: 1.5", "Msp file path: D:\\library.msp"],
            "blank": [" Smoothing method "],
        }
        self.assertEqual({"smoothing level", "msp file path", "smoothing method"}, discarded_method_keys(record))

    def test_a_discarded_smoothing_setting_leaves_only_the_raw_eic(self):
        fake_run = self._prepare_eic_run()
        method = (self.root / "method.txt").read_text(encoding="utf-8")
        level_line, method_line = "Smoothing level: 1\n", "Smoothing method: LinearWeightedMovingAverage\n"
        cases = {
            # The Console refuses a fractional level; Python's int() would say something else.
            "unusable level": (method.replace(level_line, "Smoothing level: 1.5\n"),
                               {"unusable": ["Smoothing level: 1.5"]}, "smoothing level"),
            "unusable method, other case": (method.replace(method_line, "SMOOTHING METHOD: Savitzky\n"),
                                            {"unusable": ["SMOOTHING METHOD: Savitzky"]}, "smoothing method"),
            "blank method": (method.replace(method_line, "Smoothing method:\n"),
                             {"blank": ["Smoothing method"]}, "smoothing method"),
            # The Console kept 1; the preview would have read the last line.
            "discarded and applied": (method + "Smoothing level: x\n",
                                      {"applied": ["Execute automatic RT correction for alignment", "Smoothing level"],
                                       "unusable": ["Smoothing level: x"]}, "smoothing level"),
        }
        for name, (method_text, changes, key) in cases.items():
            with self.subTest(name):
                self._rewrite_record(method_text=method_text, **{
                    "unusable": [], "blank": [], "applied": ["Execute automatic RT correction for alignment"], **changes})
                with patch("msdial_app.automatic_rt_review.subprocess.run", side_effect=fake_run):
                    result = extract_anchor_eic(self.root, "1", "1", 0.01)
                self.assertFalse(result["smoothing"]["available"])
                self.assertEqual(f"The Console discarded the recorded {key}; no smoothed preview is inferred.",
                                 result["smoothing"]["note"])
                self.assertTrue(all(point["smoothed_intensity"] is None for point in result["points"]))
                self.assertEqual([125, 80], [point["intensity"] for point in result["points"]])

    def test_a_discarded_setting_outside_smoothing_keeps_the_preview(self):
        fake_run = self._prepare_eic_run()
        self._rewrite_record(unusable=["Minimum peak height: abc"], blank=["Instrument"])
        with patch("msdial_app.automatic_rt_review.subprocess.run", side_effect=fake_run):
            result = extract_anchor_eic(self.root, "1", "1", 0.01)
        self.assertTrue(result["smoothing"]["available"])

    # The viewer calls a run verified only when the publication report describes its correction.

    def test_the_viewer_does_not_verify_what_the_report_refuses(self):
        cases = {
            "method_key_not_applied": lambda: self._rewrite_record(applied=[]),
            "method_key_value_discarded_by_console": lambda: self._rewrite_record(
                unusable=["Automatic RT correction maximum anchors: 3000000000"]),
            "audit_does_not_show_correction": lambda: self.summary.write_text(SUMMARY.replace(
                "1\tSample-1\tSample\t2\t5\t2\t2\tDetectedAnchors",
                "1\tSample-1\tSample\t2\t5\t2\t0\tUncorrected",
            ), encoding="utf-8"),
        }
        for reason, change in cases.items():
            with self.subTest(reason):
                self.summary.write_text(SUMMARY, encoding="utf-8")
                self._rewrite_record(applied=["Execute automatic RT correction for alignment"], unusable=[])
                change()
                result = read_automatic_rt_review(self.root)
                self.assertEqual(reason, result["method_audit"]["status"])
                self.assertEqual(reason, result["method_audit"]["reason"])
                self.assertTrue(any(f"do not prove a correction in this run ({reason})" in item
                                    and "publication report does not describe" in item
                                    for item in result["warnings"]))
                # The reference file's model is always reconstructable, so only the verdict stops it.
                with patch("msdial_app.automatic_rt_review.subprocess.run") as run:
                    with self.assertRaisesRegex(ValueError, f"provenance.*{reason}"):
                        extract_anchor_eic(self.root, "0", "1", 0.01)
                run.assert_not_called()

    def test_the_discarded_setting_is_named(self):
        self._rewrite_record(blank=["Automatic RT correction minimum anchors"])
        result = read_automatic_rt_review(self.root)
        self.assertEqual(["automatic rt correction minimum anchors"], result["method_audit"]["discarded_keys"])
        self.assertTrue(any("discarded the value it was given for automatic rt correction minimum anchors" in item
                            for item in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
