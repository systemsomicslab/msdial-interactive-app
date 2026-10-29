"""A vendor companion the Console needs, and a polarity read from a file name: the user's decisions of 2026-10-29.

MB-POST MPST000063 deposited 20 SCIEX .wiff and no .wiff.scan. The extractor read the .wiff headers and split the
unit by polarity, and each part then failed at its diagnostic start ("WIFF.SCAN is not accessible next to ..."),
retried. Such an input is now excluded at the disposition with reason vendor_companion_missing and the companion's
name, and a unit left with none is excluded.

MB-POST MPST000048 is ZenoTOF .wiff2 (241202_Tomita_18_neg.wiff2, _pos.wiff2), declared Both and DDA. The pinned
extractor opens no .wiff2 (unsupported_format), so every file was polarity_unresolved and the unit was skipped. An
input whose header could not be read, in a unit declaring no single polarity, now takes its polarity from one
polarity token in its file name, on record (FILE_NAME_POLARITY_RULE); a readable header always wins.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.raw_metadata_preflight import (
    FILE_NAME_POLARITY_RULE,
    POLARITY_FROM_FILE_NAME_WARNING,
    VENDOR_COMPANION_MISSING,
    decide_disposition,
    file_name_polarity,
    missing_vendor_companion,
)
from msdial_app.repository_reanalysis import split_unit_by_acquisition

from test_raw_metadata_preflight import _header, _manifest

DECLARED_BOTH_DDA = {"acquisition_mode": "DDA", "ion_mode": "Both", "untargeted": True}


class _Folder(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def touch(self, *names: str) -> list[str]:
        paths = []
        for name in names:
            path = self.root / name
            path.write_bytes(b"x")
            paths.append(str(path))
        return paths


class AVendorCompanionTheConsoleNeeds(_Folder):
    NAMES = ("Reelin_WT01_Pos.wiff", "Reelin_WT01_Neg.wiff", "Reelin_WT02_Pos.wiff", "Reelin_WT02_Neg.wiff")

    def records(self, paths: list[str]) -> list[dict]:
        return [_header(path, "DDA", polarity="Positive" if "_Pos" in path else "Negative") for path in paths]

    def test_the_rule_names_only_what_the_console_needs(self) -> None:
        (wiff,) = self.touch("S1.wiff")
        self.assertEqual("S1.wiff.scan", missing_vendor_companion(wiff))
        self.touch("S1.wiff.scan")
        self.assertEqual("", missing_vendor_companion(wiff))
        (wiff2,) = self.touch("S2.wiff2")
        self.assertEqual("", missing_vendor_companion(wiff2), "only the .wiff's .wiff.scan is held to")

    def test_mpst000063_every_wiff_without_its_scan_excludes_the_unit(self) -> None:
        paths = self.touch(*self.NAMES)
        disposition = decide_disposition(_manifest(self.records(paths), declared=DECLARED_BOTH_DDA))

        self.assertEqual(("exclude", [VENDOR_COMPANION_MISSING]), (disposition["disposition"], disposition["reasons"]))
        self.assertEqual(
            sorted(Path(path).name + ".scan" for path in paths),
            sorted(item["companion"] for item in disposition["excluded_inputs"]),
        )
        self.assertEqual({VENDOR_COMPANION_MISSING}, {item["reason"] for item in disposition["excluded_inputs"]})
        self.assertEqual({}, disposition["assignments"])

    def test_the_rest_of_the_unit_runs_and_splits(self) -> None:
        paths = self.touch(*self.NAMES)
        self.touch(*(name + ".scan" for name in self.NAMES[1:]))
        disposition = decide_disposition(_manifest(self.records(paths), declared=DECLARED_BOTH_DDA))

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual([("Reelin_WT01_Pos.wiff", VENDOR_COMPANION_MISSING, "Reelin_WT01_Pos.wiff.scan")],
                         [(Path(item["path"]).name, item["reason"], item.get("companion"))
                          for item in disposition["excluded_inputs"]])
        self.assertEqual([1, 2], sorted(group["file_count"] for group in disposition["split_key"]["groups"]))


class APolarityReadFromAFileName(unittest.TestCase):
    NAMES = ("241202_Tomita_18_neg.wiff2", "241202_Tomita_18_pos.wiff2", "241202_Tomita_19_neg.wiff2",
             "241202_Tomita_19_pos.wiff2")

    def unreadable(self, names=NAMES, **declared) -> dict:
        return _manifest([], declared={**DECLARED_BOTH_DDA, **declared},
                         failures={name: "unsupported_format" for name in names},
                         formats={name: "sciex_wiff2" for name in names})

    def test_the_token_rule(self) -> None:
        cases = {
            "241202_Tomita_18_neg.wiff2": ("Negative", "neg"), "S_Pos.wiff": ("Positive", "Pos"),
            "S1_positive.mzML": ("Positive", "positive"), "S1_ESI+.wiff2": ("Positive", "ESI+"),
            "S1_ESI-_2.wiff2": ("Negative", "ESI-"), "S1(+).raw": ("Positive", "(+)"),
            "x_pos_neg.wiff2": None, "S1.wiff2": None, "position_1.raw": None, "Neg_Ctrl_1.wiff2": None,
            "QC_pos_1.wiff2": None, "pos/S1.wiff2": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(expected, file_name_polarity(name))

    def test_mpst000048_unreadable_wiff2_split_by_the_names_polarity_on_record(self) -> None:
        disposition = decide_disposition(self.unreadable())

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(["polarity"], disposition["split_key"]["by"])
        self.assertEqual(["neg", "pos"], [group["part_key"] for group in disposition["split_key"]["groups"]])
        self.assertIn("acquisition_declared_only", disposition["warnings"])
        self.assertIn(POLARITY_FROM_FILE_NAME_WARNING, disposition["warnings"])
        self.assertEqual(
            {name: (("Negative", "neg") if "_neg" in name else ("Positive", "pos")) for name in self.NAMES},
            {item["path"]: (item["polarity"], item["token"]) for item in disposition["polarity_inferences"]},
        )
        self.assertEqual({("file_name", FILE_NAME_POLARITY_RULE)},
                         {(item["polarity_source"], item["rule"]) for item in disposition["polarity_inferences"]})
        self.assertEqual({"file_name"}, {item["polarity_source"] for item in disposition["assignments"].values()})

    def test_a_name_with_no_single_token_stays_unresolved_and_the_rest_runs(self) -> None:
        names = ("A_neg.wiff2", "A_pos.wiff2", "A_blank.wiff2")
        disposition = decide_disposition(self.unreadable(names))

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual([("A_blank.wiff2", "polarity_unresolved")],
                         [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])

    def test_an_undeclared_acquisition_stays_skipped(self) -> None:
        disposition = decide_disposition(self.unreadable(acquisition_mode="Unknown"))

        self.assertEqual("skip", disposition["disposition"])
        self.assertNotIn("polarity_inferences", disposition)

    def test_an_untargeted_status_nobody_declared_still_skips(self) -> None:
        # MPST000048's Catalog record leaves untargeted unset; unreadable headers cannot show it either.
        disposition = decide_disposition(self.unreadable(untargeted=None))

        self.assertEqual(("skip", ["untargeted_unresolved"]), (disposition["disposition"], disposition["reasons"]))

    def test_a_single_declared_polarity_and_a_readable_header_win_over_the_name(self) -> None:
        declared = decide_disposition(self.unreadable(ion_mode="Positive"))
        self.assertEqual(("run", "Positive"), (declared["disposition"], declared["ion_mode"]))
        self.assertNotIn("polarity_inferences", declared)

        read = decide_disposition(_manifest([_header("S_neg.mzML", "DDA", polarity="Positive")], declared=DECLARED_BOTH_DDA))
        self.assertEqual(("run", "Positive"), (read["disposition"], read["ion_mode"]))
        self.assertNotIn("polarity_inferences", read)

    def test_a_part_carries_its_parents_inferences(self) -> None:
        manifest = self.unreadable(("241202_Tomita_18_neg.wiff2",), ion_mode="Negative")
        manifest["polarity_inferences"] = [{"path": "241202_Tomita_18_neg.wiff2", "polarity": "Negative",
                                            "polarity_source": "file_name", "token": "neg",
                                            "rule": FILE_NAME_POLARITY_RULE}]
        disposition = decide_disposition(manifest)

        self.assertEqual(("run", "Negative"), (disposition["disposition"], disposition["ion_mode"]))
        self.assertEqual(manifest["polarity_inferences"], disposition["polarity_inferences"])
        self.assertIn(POLARITY_FROM_FILE_NAME_WARNING, disposition["warnings"])


class TheSplitWritesTheInferencesIntoEachPart(_Folder):
    def test_each_part_records_the_inferences_its_polarity_rests_on(self) -> None:
        data = self.root / "mb_post" / "MPST000048" / "u48" / "raw" / "data"
        data.mkdir(parents=True)
        (self.root / "mb_post" / "MPST000048" / "u48" / "provenance").mkdir()
        files = []
        for name in APolarityReadFromAFileName.NAMES:
            (data / name).write_bytes(b"x")
            files.append(str(data / name))
        manifest = _manifest([], declared=DECLARED_BOTH_DDA, failures={path: "unsupported_format" for path in files},
                             formats={path: "sciex_wiff2" for path in files})
        disposition = decide_disposition(manifest)
        assignments = disposition.pop("assignments")
        disposition["applied"] = True
        for entry in manifest["raw_metadata_preflight"]["summary"]["per_file"]:
            entry["console_acquisition_type"] = assignments[next(key for key in assignments
                                                                 if assignments[key]["path"] == entry["file"])][
                "console_acquisition_type"]
        root = data.parent.parent
        manifest.update({
            "schema": "msdial-public-reanalysis-run.v1", "status": "preflight_review_required", "workspace": str(root),
            "raw_directory": str(root / "raw"), "input_directory": str(data), "campaign_disposition": disposition,
        })
        manifest["project"].update({"repository": "mb_post", "accession": "MPST000048", "ion_mode": "Both",
                                    "acquisition_mode": "DDA", "separation": "LC-MS", "untargeted": True,
                                    "analysis_unit_id": "u48",
                                    "sample_metadata": [{"sample_id": Path(path).stem, "raw_file": Path(path).name}
                                                        for path in files]})
        path = root / "provenance" / "run-manifest.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")

        result = split_unit_by_acquisition(path, confirmed=True)
        parts = {part["analysis_unit_id"]: json.loads(Path(part["manifest_path"]).read_text(encoding="utf-8"))
                 for part in result["parts"]}

        self.assertEqual([], result["blockers"])
        self.assertEqual({"u48-dda-neg", "u48-dda-pos"}, set(parts))
        for part_id, part in parts.items():
            polarity = "Negative" if part_id.endswith("neg") else "Positive"
            self.assertEqual(polarity, part["project"]["ion_mode"])
            self.assertEqual({polarity}, {item["polarity"] for item in part["polarity_inferences"]})
            self.assertEqual(2, len(part["polarity_inferences"]))


if __name__ == "__main__":
    unittest.main()
