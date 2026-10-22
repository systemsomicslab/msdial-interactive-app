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
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from lab_batch_fixture import EVERY_CAPABILITY
from lab_metabolomics_fixture import (
    ALIGNMENT,
    CLASSES,
    GLUTAMINE,
    FakeExtractor,
    MetabolomicsBatch,
    STEMS,
    capabilities_patches,
    header_record,
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
from msdial_app.workflow import MULTI_ENERGY_AIF_CAPABILITY, prepare_run


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
        self.assertEqual({"acquired"}, {row["library_kind"] for row in state["msp_annotators"]})

    def test_an_unknown_kind_is_a_blocker_and_still_shown(self) -> None:
        for kind in ("MSP2", ""):
            with self.subTest(kind=kind):
                read = core.read_annotation({"libraries": [{"kind": kind, "path": self.files["public"],
                                                            "retention_time": "none"}]}, ["Positive"])
                self.assertTrue(any("is neither msp nor lbm" in item for item in read["blockers"]))
                self.assertEqual(2, core.annotation_summary(read["entries"])[0]["priority"])

    def test_an_optional_library_may_serve_one_polarity(self) -> None:
        read = core.read_annotation({"preset": "campaign", "libraries": {
            "lbm": str(self.lbm), "msp": self.files["public"], "in_silico": {"Positive": self.files["insilico"]}}},
            ["Positive", "Negative"])
        self.assertEqual([], read["blockers"] + read["missing"])
        positive = core.annotator_settings(read["entries"], "Positive")["annotators"]
        negative = core.annotator_settings(read["entries"], "Negative")["annotators"]
        self.assertEqual(4, len(positive))
        self.assertEqual(3, len(negative))
        self.assertEqual("predicted", positive[-1]["library_kind"], "an in-silico MSP is searched as predicted")

    def test_one_file_is_either_acquired_or_predicted(self) -> None:
        read = core.read_annotation({"libraries": [
            {"kind": "msp", "path": self.files["public"], "retention_time": "none", "thresholds": "strict"},
            {"kind": "msp", "path": self.files["public"], "retention_time": "none", "thresholds": "mild",
             "library_kind": "predicted"},
        ]}, ["Positive"])
        self.assertTrue(any("both acquired and predicted" in item for item in read["blockers"]))
        read = core.read_annotation({"libraries": [{"kind": "msp", "path": self.files["public"], "retention_time": "none",
                                                    "library_kind": "simulated"}]}, ["Positive"])
        self.assertTrue(any("neither acquired nor predicted" in item for item in read["blockers"]))

    def test_the_lbm2_is_named_as_the_console_names_it(self) -> None:
        read = core.read_annotation({"preset": "campaign", "libraries": {"lbm": str(self.lbm), "msp": self.files["public"]}},
                                    ["Positive"])
        self.assertEqual({"Positive": "LbmDB: lab"}, core.export_annotator_ids(read["entries"][0]))
        self.assertEqual({"Positive": "lib2_msp_strict"}, core.export_annotator_ids(read["entries"][1]))
        self.assertEqual("LbmDB", core.console_lbm_annotator_id(""))

    def test_the_retention_time_census_reads_as_ms_dial_reads(self) -> None:
        path = self.root / "census.msp"
        path.write_text("NAME: a\nRETENTIONTIME: 1.5\nNum Peaks: 0\n\nNAME: b\nRETENTIONTIME: \nNum Peaks: 0\n\n"
                        "Name: c\nRT: 0\nNum Peaks: 0\n\nNAME: d\nrt: 2\nNum Peaks: 0\n", encoding="ascii")
        self.assertEqual({"records": 4, "with_retention_time": 2}, core.retention_time_census(path))
        # Without a peak count a record does not end: MS-DIAL reads the next NAME line as a field of it.
        path.write_text("NAME: a\nRETENTIONTIME: 1.5\n\nNAME: b\nRETENTIONTIME: 0\n", encoding="ascii")
        self.assertEqual({"records": 1, "with_retention_time": 0}, core.retention_time_census(path))
        # A space before the colon is another field name; the last parseable RT wins; the peak count ends a
        # record; a byte-order mark does not hide the first record; a comment line is skipped.
        path = self.root / "census2.msp"
        path.write_bytes(
            "﻿NAME: a\nRT : 5.2\nNum Peaks: 1\n100\t1\nRETENTIONTIME: 3\n"
            "NAME: b\nRETENTIONTIME: 3\n# RT: 9\nRETENTIONTIME: 0\nNum Peaks: 0\n"
            "NAME: c\nRETENTIONTIME: 0\nretention_time: 4\nNum Peaks: 0\n".encode("utf-8"))
        self.assertEqual({"records": 3, "with_retention_time": 1}, core.retention_time_census(path))

    def test_a_setting_the_annotation_does_not_read_is_refused(self) -> None:
        read = core.read_annotation({"preset": "lab_rt_tiers", "libraries": {"in_house": self.files["in_house"],
                                     "predicted_rt": self.files["predicted_rt"], "no_rt": self.files["no_rt"]},
                                     "rt_tolerance": {"in_house": 0.2}}, ["Positive"])
        self.assertTrue(any("annotation.rt_tolerance is not read" in item for item in read["blockers"]))
        read = core.read_annotation({"libraries": [{"kind": "msp", "path": self.files["public"], "retention_time": "none",
                                                    "ms2_tolerance": 0.01}]}, ["Positive"])
        self.assertTrue(any("ms2_tolerance is not read" in item for item in read["blockers"]))
        read = core.read_annotation({"preset": "lab_rt_tiers", "libraries": {
            "in_house": self.files["in_house"], "no_rt": self.files["no_rt"],
            "predicted_rt": {"path": self.files["predicted_rt"], "library_kind": "predicted"}}}, ["Positive"])
        self.assertEqual([], read["blockers"] + read["missing"])
        self.assertEqual(["", "predicted", ""], [entry["library_kind"] for entry in read["entries"]])

    def test_a_library_path_outside_ascii_is_refused(self) -> None:
        folder = self.root / "共有"
        folder.mkdir()
        path = folder / "x.msp"
        path.write_text("NAME: x\n", encoding="ascii")
        read = core.read_annotation({"preset": "single_msp", "libraries": {"msp": str(path)}}, ["Positive"])
        self.assertTrue(any("is not an ASCII path" in item for item in read["blockers"]))


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
                          "analysis_root", "raw_location", "console_path",
                          "duplicate_rule", "inchikey_match"}, asked)
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

    def test_a_library_searched_with_rt_must_give_rts(self) -> None:
        self.batch.libraries["in_house"].write_text("NAME: a\nRETENTIONTIME: \nNum Peaks: 0\n", encoding="ascii")
        check = agent.check_inputs(self.batch.request())
        self.assertTrue(any("none of the 1 records" in item for item in check["blockers"]), check["blockers"])
        self.batch.libraries["in_house"].write_text(
            "NAME: a\nRETENTIONTIME: 1.0\nNum Peaks: 0\n\nNAME: b\nNum Peaks: 0\n", encoding="ascii")
        check = agent.check_inputs(self.batch.request())
        self.assertTrue(check["ready_for_contract"], check["blockers"])
        self.assertTrue(any("1 of the 2 records" in item for item in check["warnings"]), check["warnings"])
        self.assertEqual({"records": 2, "with_retention_time": 1},
                         check["annotation"]["retention_time_census"]["in-house RT and MS/MS (positive)"])

    def test_folder_raw_data_need_a_console_that_reads_them(self) -> None:
        folder = self.batch.root / "agilent" / "pos"
        for stem in ("S1_POS", "QC1_POS"):
            (folder / f"{stem}.d").mkdir(parents=True)
            (folder / f"{stem}.d" / "AcqData").mkdir()
        with patch.object(agent, "console_supports_folder_type_csv", return_value=False):
            check = agent.check_inputs(self.batch.request(pos_path=str(folder)))
        self.assertTrue(any("The raw data are folders" in item for item in check["blockers"]), check["blockers"])

    def test_raw_files_the_console_cannot_name_are_refused(self) -> None:
        for folder_name in ("データ_pos", "a,b_pos"):
            with self.subTest(folder=folder_name):
                folder = self.batch.root / "data" / folder_name
                folder.mkdir()
                (folder / "S1_Pos.mzML").write_text("", encoding="ascii")
                check = agent.check_inputs(self.batch.request(pos_path=str(folder)))
                self.assertTrue(any("The Console cannot read the positive raw folder" in item for item in check["blockers"]),
                                check["blockers"])

    def test_a_raw_file_the_console_cannot_name_may_be_left_out(self) -> None:
        (self.batch.folders["Positive"] / "テスト_Pos.mzML").write_text("", encoding="ascii")
        check = agent.check_inputs(self.batch.request())
        self.assertTrue(check["ready_for_contract"], check["blockers"])
        self.assertTrue(any("by their names" in item for item in check["warnings"]))
        rows = [{"stem": stem, "class_id": CLASSES[stem], **({} if kind == "Sample" else {"file_type": kind})}
                for stem, kind in STEMS]
        request = {**self.batch.request(), "check_digest": check["check_digest"], "sample_table_confirmed": True,
                   "confirmed_by": "the analyst"}
        refused = agent.write_contract({**request, "sample_table": rows + [{"stem": "テスト"}]})
        self.assertIn("cannot reach the Console", refused.get("error", ""))
        written = agent.write_contract({**request, "sample_table": rows + [{"stem": "テスト", "included": False}],
                                        "confirmed": True})
        self.assertTrue(written["written"], written)

    def test_values_the_console_reads_as_something_else_are_refused(self) -> None:
        check = agent.check_inputs(self.batch.request())
        rows = [{"stem": stem, "class_id": CLASSES[stem], **({} if kind == "Sample" else {"file_type": kind})}
                for stem, kind in STEMS]
        request = {**self.batch.request(), "check_digest": check["check_digest"], "sample_table_confirmed": True}
        for change, word in (({"acquisition_type": "DIA"}, "is none of DDA, SWATH or AIF"),
                             ({"analytical_order": 20261022001}, "beyond what the Console reads")):
            with self.subTest(change=change):
                table = [dict(row, **change) if row["stem"] == "S1" else row for row in rows]
                refused = agent.write_contract({**request, "sample_table": table})
                self.assertIn(word, refused.get("error", ""))
        self.assertTrue(agent.row_included({"included": " true"}))
        self.assertFalse(agent.row_included({"included": " false"}))
        self.assertTrue(agent.row_included({}))

    def test_a_class_the_console_cannot_read_is_refused(self) -> None:
        check = agent.check_inputs(self.batch.request())
        for class_id in ("症例", 'Case "A"'):
            with self.subTest(class_id=class_id):
                refused = agent.write_contract({
                    **self.batch.request(), "check_digest": check["check_digest"], "sample_table_confirmed": True,
                    "sample_table": [{"stem": stem, "class_id": class_id if stem == "S1" else CLASSES[stem],
                                      **({} if kind == "Sample" else {"file_type": kind})} for stem, kind in STEMS],
                })
                self.assertIn("class_id", refused.get("error", ""))

    def test_each_library_file_is_listed_by_label_and_polarity(self) -> None:
        check = agent.check_inputs(self.batch.request())
        self.assertEqual(["in-house RT and MS/MS", "predicted RT", "MS/MS without RT"],
                         [item["label"] for item in check["annotation"]["files"]])
        self.assertTrue(all(item["sha256"] and item["polarity"] == "Positive" for item in check["annotation"]["files"]))

    def test_an_analysis_root_outside_ascii_is_refused(self) -> None:
        check = agent.check_inputs(self.batch.request(analysis_root=str(self.batch.root / "解析")))
        self.assertTrue(any("is not an ASCII path" in item for item in check["blockers"]))

    def test_a_root_holding_the_other_laboratory_analysis_is_refused(self) -> None:
        inputs = self.batch.analysis / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "lab-lipidomics-contract.json").write_text("{}", encoding="utf-8")
        check = agent.check_inputs(self.batch.request())
        self.assertTrue(any("holds a laboratory lipidomics analysis" in item for item in check["blockers"]))
        (inputs / "lab-lipidomics-contract.json").unlink()
        contract = self.batch.contract()
        self.assertTrue(Path(contract).is_file())
        from lab_batch_fixture import Batch, capabilities_patches as lipidomics_patches
        from msdial_app import lab_lipidomics_agent

        lipidomics = Batch(self.batch.root / "lipidomics")
        with lipidomics_patches():
            check = lab_lipidomics_agent.check_inputs(lipidomics.request(analysis_root=str(self.batch.analysis)))
        self.assertTrue(any("holds a laboratory LC-MS metabolomics analysis" in item for item in check["blockers"]))

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
        self.assertIn("S1", kept[0], "the columns take the confirmed sample names")
        # A folder read asks for both polarities; the one the contract lacks is passed over, a named job is refused.
        resolved = agent.resolve_standards_for_contract(contract, {"Positive": job, "Negative": None})
        self.assertEqual(["Positive"], list(resolved["polarities"]))
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.resolve_standards_for_contract(contract, {"Negative": job})
        for name in ("qa_figures.", "metabolome.", "pos", "a b"):
            with self.subTest(output_folder=name), self.assertRaises(agent.LabMetabolomicsError):
                agent.merge(contract, output_folder=name)


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
        self.assertEqual({"acquired"}, {row["library_kind"] for row in msp})
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

    def test_the_contract_is_never_carried_to_another_dataset(self) -> None:
        plan = self.plan()
        self.assertIn(agent.CONTRACT_KEY, plan["workset_suggestion"]["not_reusable"])
        self.assertNotIn(agent.CONTRACT_KEY, plan["workset_suggestion"].get("reusable_answers", {}))
        # Reached through a workset, the key is not one this plan loaded.
        workset = {"id": "w", "kind": "", "answers": {agent.CONTRACT_KEY: self.contract}}
        with patch("msdial_app.agent_workflow.get_workset", return_value=workset):
            other = build_guided_plan(str(self.batch.folders["Positive"]), {
                "project_type": "lcms", "ion_mode": "Positive", "target_omics": "Metabolomics",
                "library_strategy": "none", "class_assignment_confirmed": True, "run_qa": False,
                "generate_materials_methods": False,
            }, workset_id="w")
        self.assertEqual("", other["profile"])
        self.assertNotIn(agent.CONTRACT_KEY, other["answers"])
        self.assertNotIn(agent.CONTRACT_KEY, other["workflow"] or {})

    def test_a_settings_file_without_a_library_kind_is_written_as_before(self) -> None:
        plan = build_guided_plan(str(self.batch.folders["Positive"]), {
            "project_type": "lcms", "ion_mode": "Positive", "target_omics": "Metabolomics",
            "parameter_strategy": "default", "execute_rt_correction": False,
            "library_strategy": "existing", "libraries": {"msp_paths": [str(self.batch.libraries["no_rt"])]},
            "use_retention_time_for_annotation": False, "class_assignment_confirmed": True, "run_qa": False,
            "generate_materials_methods": False, "output_root": str(self.batch.root / "guided"),
            "console_path": str(self.batch.console),
        })
        self.assertIsNotNone(plan["workflow"], plan["remaining_questions"])
        prepared = prepare_run(plan["workflow"])
        header = (Path(prepared["run_directory"]) / "msp_annotator_settings.tsv").read_text(encoding="ascii").splitlines()[0]
        self.assertNotIn("library_kind", header.split("\t"))


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

    def test_the_written_matrix_is_recomputed_on_its_own(self) -> None:
        record = self.normalize("Positive")
        path = Path(record["outputs"]["normalized"])
        height = record["outputs"]["height_copy"]
        self.assertTrue(core.verify_normalized(path, height, "1", {})["passed"])
        # A matrix printed from wrong values reads back as itself; recomputing from the heights catches it.
        normalized = core.normalize_heights(height, "1", {})
        normalized["values"]["10"][0] *= 2
        path.write_bytes(core.normalized_text(normalized).encode("utf-8"))
        checked = core.verify_normalized(path, height, "1", {})
        self.assertFalse(checked["passed"])
        self.assertEqual([("10", "S1_Pos")], [(item["alignment_id"], item["sample"]) for item in checked["failed"]])
        self.assertFalse(core.verify_normalized(path, height, "1", {}, expected_height_sha256="0" * 64)["passed"])

    def test_a_spot_taken_instead_is_the_standard_and_the_one_passed_over_stays_a_feature(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        spots = [dict(item) for item in POSITIVE_SPOTS]
        # Met-d3 has no text-row annotation; the most intense spot in its window is an MSP-matched metabolite.
        spots[0] = dict(spots[0], name="Unknown", method="", cells=[1000, 2000, 1500, 0])
        spots.append(dict(alignment_id=3, mz=153.0770, rt=0.95, name="Isobaric metabolite Y", method="lib3_no_rt",
                          inchikey="YYYYYYYYYYYYYY-YYYYYYYYYY-N", cells=[50000, 50000, 50000, 0]))
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        summary = resolved["polarities"]["Positive"]["summary"]["normalizing_standard"]
        self.assertEqual(("D", "3", True), (summary["tier"], summary["alignment_id"], summary["needs_confirmation"]))
        digest = resolved["polarities"]["Positive"]["resolution_digest"]
        with self.assertRaises(agent.LabMetabolomicsError):
            agent.plan_normalize(self.contract, job, digest)
        plan = agent.plan_normalize(self.contract, job, digest, accept_spot={"Methionine-d3": 1})
        self.assertEqual(("1", "taken_by_person", "D"), (plan["normalizing_standard"]["alignment_id"],
                                                         plan["normalizing_standard"]["status"],
                                                         plan["normalizing_standard"]["tier"]))
        self.assertIn("1", plan["internal_standard_ids"])
        self.assertNotIn("3", plan["internal_standard_ids"], "the spot the person passed over is no standard")
        self.assertIn({"StandardName": "Methionine-d3", "PeakID": 1}, plan["figure_rows"])
        record = agent.finish_normalize(plan)
        self.assertTrue(record["verified"], record["verification"])
        self.normalize("Negative")
        self.jobs["Positive"] = job
        report = agent.merge(self.contract, write=True)
        kept = {row["Alignment ID"] for row in csv.DictReader(io.StringIO(
            Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8"))) if row["Polarity"] == "Positive"}
        self.assertIn("3", kept)

    def test_a_standard_found_only_by_search_is_named_and_kept(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] != 2]
        # CUDA is absent; an MSP-matched metabolite sits at its m/z and RT.
        spots.append(dict(alignment_id=20, mz=341.2800, rt=2.90, name="Endogenous Z", method="lib1_in_house",
                          inchikey="ZZZZZZZZZZZZZZ-ZZZZZZZZZZ-N", cells=[700, 800, 750, 0]))
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertEqual(["1"], plan["internal_standard_ids"])
        self.assertEqual([("CUDA", "20")], [(item["standard"], item["alignment_id"])
                                            for item in plan["unconfirmed_standard_spots"]])
        # Figure 04 draws the response where the standard was looked for, whatever the merge decides.
        self.assertIn({"StandardName": "CUDA", "PeakID": 20}, plan["figure_rows"])
        self.assertTrue(any("keeps it as its own row" in item for item in plan["warnings"]), plan["warnings"])
        agent.finish_normalize(plan)
        self.normalize("Negative")
        report = agent.merge(self.contract, write=True)
        self.assertEqual([{"standard": "CUDA", "alignment_id": "20", "kept": True}],
                         [{key: item[key] for key in ("standard", "alignment_id", "kept")}
                          for item in report["unconfirmed_standard_spots"]["Positive"]])

    def test_a_standard_a_library_named_is_a_standard_even_when_invalid(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        spots = [dict(item) for item in POSITIVE_SPOTS]
        # CUDA annotated by the in-house library by its own name, missing from one Sample: tier A, invalid.
        spots[1] = dict(spots[1], method="lib1_in_house", cells=[5000, 0, 5100, 4900])
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        cuda = next(item for item in resolved["polarities"]["Positive"]["summary"]["standards"] if item["name"] == "CUDA")
        self.assertEqual(("A", "invalid"), (cuda["tier"], cuda["status"]))
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertEqual(["1", "2"], plan["internal_standard_ids"])
        self.assertEqual([], plan["unconfirmed_standard_spots"])

    def test_an_unconfirmed_standard_spot_never_removes_the_endogenous_compound(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS, TRYPTOPHAN

        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] != 2]
        # CUDA's window holds a labelled tryptophan under another name, spiked high: tier D, nobody confirms it.
        spots.append(dict(alignment_id=21, mz=341.2799, rt=2.88, name="L-Tryptophan-d5", method="lib1_in_house",
                          inchikey="QIVBCDIJIAJPQS-DUAAAAAAAA-N", cells=[90000, 90000, 90000, 90000]))
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        agent.finish_normalize(plan)
        self.normalize("Negative")
        report = agent.merge(self.contract, write=True)
        kept = {(row["Polarity"], row["Alignment ID"]) for row in csv.DictReader(io.StringIO(
            Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8")))}
        self.assertIn(("Positive", "21"), kept)
        self.assertIn(("Positive", "15"), kept, "the endogenous tryptophan stays")
        spot = report["unconfirmed_standard_spots"]["Positive"][0]
        self.assertEqual(("21", True), (spot["alignment_id"], spot["kept"]))
        self.assertEqual([("Positive", "15")], [(item["polarity"], item["alignment_id"])
                                                for item in spot["shares_first_block_with"]])
        self.assertEqual(1, report["counts"]["kept_unconfirmed_standard_spots"])
        self.assertTrue(TRYPTOPHAN.startswith("QIVBCDIJIAJPQS"))

    def test_the_warnings_say_what_the_merge_does_with_each_standards_spot(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": self.jobs["Positive"]})
        plan = agent.plan_normalize(self.contract, self.jobs["Positive"], resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertTrue(any("Spot 2 ('CUDA') was annotated as CUDA (tier B" in item and "drops it as an internal standard" in item
                            for item in plan["warnings"]), plan["warnings"])
        # Two library spots named as CUDA: both are dropped, and both are named.
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] != 2]
        spots += [dict(alignment_id=9, mz=341.2799, rt=2.88, name="CUDA", method="lib1_in_house", cells=[5000] * 4),
                  dict(alignment_id=19, mz=341.2799, rt=3.10, name="CUDA", method="lib1_in_house", cells=[50] * 4)]
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertEqual(["1", "9", "19"], plan["internal_standard_ids"])
        for spot_id in ("9", "19"):
            self.assertTrue(any(f"Spot {spot_id} ('CUDA')" in item and "drops it as an internal standard" in item
                                for item in plan["warnings"]), (spot_id, plan["warnings"]))
        # A second Met-d3 spot the text row annotated below the cutoff (tier C, not chosen) is named too.
        spots = [dict(item) for item in POSITIVE_SPOTS]
        spots.append(dict(alignment_id=5, mz=153.0772, rt=1.00, name="Methionine-d3", method="internal_standards_pos",
                          cells=[100, 100, 100, 0], matched=False))
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertTrue(any("Spot 5 ('Methionine-d3')" in item and "drops it as an internal standard" in item
                            for item in plan["warnings"]), plan["warnings"])
        # CUDA absent; the only spot at its m/z and RT is unannotated.
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] != 2]
        spots.append(dict(alignment_id=30, mz=341.2799, rt=2.88, name="Unknown", cells=[100, 100, 100, 0]))
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertTrue(any("Spot 30" in item and "drops it as unannotated" in item for item in plan["warnings"]),
                        plan["warnings"])
        # A standard not found: no class is quantified here, so nothing says so.
        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] != 2]
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        warnings = resolved["polarities"]["Positive"]["warnings"]
        self.assertTrue(any(item.startswith("CUDA was not found at its adduct's m/z within 0.5 min") for item in warnings), warnings)
        self.assertFalse(any("NOT QUANTIFIED" in item for item in warnings))

    def test_a_gap_filled_standard_in_a_sample_needs_the_persons_word(self) -> None:
        job = write_run(self.batch, self.contract, "Positive", gap_filled={1: ["S2"]})
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        summary = resolved["polarities"]["Positive"]["summary"]["normalizing_standard"]
        self.assertEqual(["S2_Pos", "Blank_Pos"], summary["gap_filled_files"])
        self.assertEqual("Methionine-d3", summary["ms_dial_name"])
        self.assertIsNotNone(summary["delta_rt"])
        digest = resolved["polarities"]["Positive"]["resolution_digest"]
        with self.assertRaises(agent.LabMetabolomicsError) as raised:
            agent.plan_normalize(self.contract, job, digest)
        self.assertIn("S2_Pos (gap-filled height 2000)", str(raised.exception))
        plan = agent.plan_normalize(self.contract, job, digest, accept_gap_filled=True)
        self.assertEqual(["S2_Pos"], plan["accepted_gap_filled"])
        self.assertTrue(agent.finish_normalize(plan)["verified"])

    def test_a_blank_with_a_noise_standard_keeps_its_raw_heights(self) -> None:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        spots = [dict(item) for item in POSITIVE_SPOTS]
        # Gap filling put 50 counts where Met-d3 would be in the Blank: under 10% of the lowest Sample (1000).
        spots[0] = dict(spots[0], cells=[1000, 2000, 1500, 50])
        job = write_run(self.batch, self.contract, "Positive", spots=spots, peak_ids=False)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        self.assertTrue(any("mdpeakid.tsv is not beside" in item for item in resolved["polarities"]["Positive"]["warnings"]))
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertIn("below 10%", plan["raw_height_reasons"]["Blank_Pos"])
        self.assertIsNone(plan["multipliers"]["Blank_Pos"])
        self.assertEqual(10.0, plan["_normalized"]["values"]["10"][3])
        self.assertTrue(agent.finish_normalize(plan)["verified"])

    def test_a_plan_cannot_change_a_dilution_factor(self) -> None:
        plan = build_guided_plan(str(self.batch.folders["Positive"]), {
            agent.CONTRACT_KEY: self.contract, "ion_mode": "Positive", "minimum_peak_height": 500, "dilution_factor": 2})
        self.assertTrue(any("dilution_factor is fixed by the contract" in item for item in plan["blockers"]))
        plan = build_guided_plan(str(self.batch.folders["Positive"]), {
            agent.CONTRACT_KEY: self.contract, "ion_mode": "Positive", "minimum_peak_height": 500})
        self.assertNotIn("dilution_factor", [item["id"] for item in plan["remaining_questions"]])

    def test_possible_isomers_are_reported(self) -> None:
        from lab_metabolomics_fixture import NEGATIVE_SPOTS

        spots = [dict(item) for item in NEGATIVE_SPOTS]
        # Mannose 6-phosphate and glucose 6-phosphate share a first block and lie 0.5+ min apart.
        spots.append(dict(alignment_id=120, mz=259.0224, rt=8.10, name="Glucose 6-phosphate", adduct="[M-H]-",
                          method="lib1_in_house", inchikey="NBSCHQHZLSJFNQ-GASJEMHNSA-N", cells=[1000, 1000, 1000, 0]))
        spots.append(dict(alignment_id=121, mz=259.0224, rt=8.70, name="Mannose 6-phosphate", adduct="[M-H]-",
                          method="lib1_in_house", inchikey="NBSCHQHZLSJFNQ-QTVWNMPRSA-N", cells=[3000, 3000, 3000, 0]))
        self.jobs["Negative"] = write_run(self.batch, self.contract, "Negative", spots=spots)
        self.normalize("Positive")
        self.normalize("Negative")
        report = agent.merge(self.contract)
        self.assertEqual(1, report["counts"]["possible_isomer_groups"])
        group = report["possible_isomers"][0]
        self.assertEqual(("NBSCHQHZLSJFNQ", "121", ["GASJEMHNSA", "QTVWNMPRSA"]),
                         (group["compound_key"], group["kept"]["alignment_id"], group["stereo_blocks"]))

    def test_a_merge_folder_that_is_a_file_or_a_device_is_refused(self) -> None:
        self.normalize("Positive")
        self.normalize("Negative")
        for name in ("Height_Pos.txt", "nul", "COM1.txt"):
            with self.subTest(output_folder=name), self.assertRaises(agent.LabMetabolomicsError):
                agent.merge(self.contract, output_folder=name)

    def test_a_project_saved_long_after_the_run_is_said(self) -> None:
        job = self.jobs["Positive"]
        project = Path(job["artifacts"]["other"][2])
        height = Path(job["artifacts"]["other"][0])
        os.utime(project, (height.stat().st_mtime + 2 * 3600, height.stat().st_mtime + 2 * 3600))
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        plan = agent.plan_normalize(self.contract, job, resolved["polarities"]["Positive"]["resolution_digest"])
        self.assertTrue(any("do not reach it" in item for item in plan["warnings"]), plan["warnings"])

    def test_a_library_that_left_no_candidate_is_named(self) -> None:
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": self.jobs["Positive"]})
        polarity = resolved["polarities"]["Positive"]
        self.assertEqual({"in-house RT and MS/MS": 3, "predicted RT": 1, "MS/MS without RT": 1},
                         polarity["summary"]["candidates_by_library"])
        spots = [dict(item) for item in __import__("lab_metabolomics_fixture").POSITIVE_SPOTS
                 if item.get("method") != "lib2_predicted_rt"]
        job = write_run(self.batch, self.contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(self.contract, {"Positive": job})
        self.assertEqual(0, resolved["polarities"]["Positive"]["summary"]["candidates_by_library"]["predicted RT"])
        self.assertTrue(any("The library 'predicted RT'" in item for item in resolved["polarities"]["Positive"]["warnings"]))

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
        self.assertEqual("duplicate InChIKey first block", removed[("Positive", "10")])
        self.assertEqual({}, report["unconfirmed_standard_spots"])
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


class DuplicateRules(Batched):
    """Which row of one compound the merge keeps, and what one compound is: the person's answers."""

    polarities = ("Positive",)

    def merge_with(self, spots: list[dict], **options) -> dict[str, list[str]]:
        contract = getattr(self, "contract_path", None) or self.batch.contract()
        self.contract_path = contract
        job = write_run(self.batch, contract, "Positive", spots=spots)
        resolved = agent.resolve_standards_for_contract(contract, {"Positive": job})
        agent.finish_normalize(agent.plan_normalize(contract, job, resolved["polarities"]["Positive"]["resolution_digest"]))
        report = agent.merge(contract, write=True, **options)
        kept = [row["Alignment ID"] for row in csv.DictReader(io.StringIO(
            Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8")))]
        return {"kept": kept, "report": report}

    def spots(self) -> list[dict]:
        from lab_metabolomics_fixture import POSITIVE_SPOTS

        spots = [dict(item) for item in POSITIVE_SPOTS if item["alignment_id"] in (1, 2)]
        # Glucose 6-phosphate by the in-house library with RT and MS/MS, weak; an MS/MS-only hit of the same
        # compound elsewhere, strong; and mannose 6-phosphate, its stereoisomer, with RT and MS/MS.
        spots += [
            dict(alignment_id=40, mz=261.0370, rt=8.10, name="Glucose 6-phosphate", method="lib1_in_house",
                 inchikey="NBSCHQHZLSJFNQ-GASJEMHNSA-N", cells=[1000, 1000, 1000, 0], rt_matched=True),
            dict(alignment_id=41, mz=261.0370, rt=7.40, name="Glucose 6-phosphate", method="lib3_no_rt",
                 inchikey="NBSCHQHZLSJFNQ-GASJEMHNSA-N", cells=[9000, 9000, 9000, 0]),
            dict(alignment_id=42, mz=261.0370, rt=8.70, name="Mannose 6-phosphate", method="lib1_in_house",
                 inchikey="NBSCHQHZLSJFNQ-QTVWNMPRSA-N", cells=[500, 500, 500, 0], rt_matched=True),
        ]
        return spots

    def test_evidence_keeps_the_rt_and_ms2_match_over_a_stronger_ms2_only_one(self) -> None:
        result = self.merge_with(self.spots(), duplicate_rule="evidence")
        self.assertEqual(["40"], [item for item in result["kept"] if item in {"40", "41", "42"}])
        self.assertEqual(["duplicate_rule is evidence; the contract says intensity"],
                         result["report"]["deviations_from_contract"])

    def test_intensity_keeps_the_stronger_row(self) -> None:
        result = self.merge_with(self.spots())
        self.assertEqual(["41"], [item for item in result["kept"] if item in {"40", "41", "42"}])
        self.assertEqual([], result["report"]["deviations_from_contract"])

    def test_keep_all_and_two_blocks(self) -> None:
        result = self.merge_with(self.spots(), duplicate_rule="keep_all")
        self.assertEqual(["40", "41", "42"], [item for item in result["kept"] if item in {"40", "41", "42"}])
        self.assertEqual(2, result["report"]["counts"]["duplicates_kept"])
        result = self.merge_with(self.spots(), duplicate_rule="evidence", inchikey_match="first_two_blocks")
        # Mannose 6-phosphate is its own compound by its second block.
        self.assertEqual(["40", "42"], [item for item in result["kept"] if item in {"40", "41", "42"}])

    def test_the_answers_are_asked_and_fixed_in_the_contract(self) -> None:
        check = agent.check_inputs(self.batch.request(duplicate_rule=None, inchikey_match=None))
        self.assertEqual({"duplicate_rule", "inchikey_match"}, {item["id"] for item in check["questions"]})
        check = agent.check_inputs(self.batch.request(duplicate_rule="loudest"))
        self.assertTrue(any("duplicate_rule 'loudest'" in item for item in check["blockers"]))
        contract = agent.load_contract(self.batch.contract(duplicate_rule="evidence", inchikey_match="first_two_blocks"))
        self.assertEqual(("evidence", "first_two_blocks"), (contract["merge"]["defaults"]["duplicate_rule"],
                                                            contract["merge"]["defaults"]["inchikey_match"]))


class PairingAndStandardsText(unittest.TestCase):
    def test_files_named_apart_pair_by_the_words_that_set_them_apart(self) -> None:
        from msdial_app.lab_lcms_metabolomics import pair_by_shared_tokens

        positive = [{"file_name": f"20230125_SWATH_{name}_C_CE20_d1.raw", "file_type": "Sample", "class_id": name[0]}
                    for name in ("A1", "A2", "B1", "B2")]
        negative = [{"file_name": f"20230208_SWATH_{name}_A_CE20_{suffix}.raw", "file_type": "Sample", "class_id": name[0]}
                    for name, suffix in (("A1", "d2"), ("A2", "d2r2"), ("B1", "d2"), ("B2", "d2"))]
        paired = pair_by_shared_tokens(positive, negative)
        self.assertEqual([], paired["problems"])
        self.assertEqual({"A1": "20230208_SWATH_A1_A_CE20_d2.raw", "A2": "20230208_SWATH_A2_A_CE20_d2r2.raw"},
                         {row["stem"]: row["negative_file"] for row in paired["rows"] if row["stem"] in {"A1", "A2"}})
        paired = pair_by_shared_tokens(positive, negative[:3])
        self.assertTrue(any("no negative counterpart" in item for item in paired["problems"]))

    def test_only_letter_and_number_replicates_give_a_class(self) -> None:
        four = {"A1": ["A1"], "A2": ["A2"], "B1": ["B1"], "B2": ["B2"]}
        self.assertEqual({"A1": "A", "A2": "A", "B1": "B", "B2": "B"}, core.replicate_classes(four))
        # A blank, a QC or a standard keeps its own class and makes no group.
        self.assertEqual({"A1": "A", "A2": "A", "B1": "B", "B2": "B"},
                         core.replicate_classes({**four, "Blank": ["Blank"], "QC1": ["QC1"], "Std2": ["Std2"]},
                                                {"Blank": "Blank"}))
        numbers = {f"S{index}": [f"S{index}"] for index in range(1, 17)}
        for shared in (
            {"S1": ["S1"], "S2": ["S2"]},  # one group: sample numbers
            {"A1": ["A1"], "B1": ["B1"]},  # no group holds a replicate
            {"A1": ["A1"], "A2": ["A2"], "B1": ["B1"]},  # a group of one pair
            {"A1": ["A1"], "A2": ["A2"], "Blank": ["Blank"]},
            {"A1": ["A1", "B2"], "A2": ["A2"], "B1": ["B1"]},
            {**numbers, "QC1": ["QC1"], "QC2": ["QC2"], "QC3": ["QC3"]},  # sample numbers beside QCs
            {**numbers, "Std1": ["Std1"], "Std2": ["Std2"]},
            {**{f"Mouse{index}": [f"Mouse{index}"] for index in range(1, 13)}, "Blank1": ["Blank1"]},
        ):
            with self.subTest(shared=sorted(shared)[:3]):
                self.assertIsNone(core.replicate_classes(shared))

    def test_a_class_both_polarities_names_agree_on_is_never_replaced_by_letters(self) -> None:
        # Mouse numbers beside a numbered blank, and plate wells, both letter+number words the pairs share.
        wells = [f"{row}{column}" for row in "ABCD" for column in (1, 2, 3)]
        for samples, groups in (
            ([("Ctrl" if index <= 6 else "Treat", f"Mouse{index}") for index in range(1, 13)], {"Ctrl": 6, "Treat": 6}),
            ([("WT" if well[0] in "AB" else "KO", well) for well in wells], {"WT": 6, "KO": 6}),
        ):
            with self.subTest(groups=groups), tempfile.TemporaryDirectory() as directory, capabilities_patches():
                batch = MetabolomicsBatch(Path(directory), ("Positive", "Negative"), stems=[])
                for group, word in samples:
                    (batch.folders["Positive"] / f"20230101_Pos_{group}_{word}.mzML").write_text("", encoding="ascii")
                    (batch.folders["Negative"] / f"20230105_Neg_{group}_{word}.mzML").write_text("", encoding="ascii")
                (batch.folders["Positive"] / "20230101_Pos_Blank1.mzML").write_text("", encoding="ascii")
                (batch.folders["Negative"] / "20230105_Neg_Blank1.mzML").write_text("", encoding="ascii")
                check = agent.check_inputs(batch.request())
                table = check["paired_sample_table"]
                self.assertIn("words that set", table["pairing"])
                self.assertEqual({**groups, "Blank": 1},
                                 {name: [row["class_id"] for row in table["rows"]].count(name) for name in [*groups, "Blank"]})
                self.assertNotEqual("shared_sample_word_letters", table["class_proposal"].get("derived_from"))
                self.assertFalse(any("shared sample word's letters" in item for item in check["warnings"]), check["warnings"])
        # Plate wells beside a factor one polarity names in lower case, and beside one file name with a typo: every
        # pair keeps its names' class, and each pair whose two classes differ is said.
        wells = [f"{row}{column}" for row in "ABCD" for column in (1, 2, 3)]
        for label, negative_class in (
            ("case only", lambda well, group: group.lower()),
            ("one typo", lambda well, group: "WT" if well == "C1" else group),
        ):
            with self.subTest(label):
                positive = [{"file_name": f"20230101_Pos_{group}_{well}_{index:03d}", "file_type": "Sample",
                             "class_id": group}
                            for index, well in enumerate(wells, start=1) for group in ["WT" if well[0] in "AB" else "KO"]]
                negative = [{"file_name": f"20230105_Neg_{negative_class(well, group)}_{well}_{index:03d}",
                             "file_type": "Sample", "class_id": negative_class(well, group)}
                            for index, well in enumerate(wells, start=1) for group in ["WT" if well[0] in "AB" else "KO"]]
                paired = core.pair_by_shared_tokens(positive, negative)
                self.assertEqual([], paired["problems"])
                self.assertEqual({}, paired["class_proposal"])
                self.assertEqual({"WT": 6, "KO": 6}, {name: [row["class_id"] for row in paired["rows"]].count(name)
                                                      for name in ("WT", "KO")})
                said = [item for item in paired["warnings"] if "class_id is" in item]
                self.assertEqual(12 if label == "case only" else 1, len(said), said)
        # Where the letters decide, each pair whose two classes differ is still said, and recorded.
        positive = [{"file_name": f"20230125_SWATH_{name}_C_CE20_d1", "file_type": "Sample", "class_id": "Sample"}
                    for name in ("A1", "A2", "B1", "B2")]
        negative = [{"file_name": f"20230208_SWATH_{name}_A_CE20_{suffix}", "file_type": "Sample", "class_id": suffix}
                    for name, suffix in (("A1", "d2"), ("A2", "d2r2"), ("B1", "d2"), ("B2", "d2"))]
        paired = core.pair_by_shared_tokens(positive, negative)
        self.assertEqual(["A", "A", "B", "B"], sorted(row["class_id"] for row in paired["rows"]))
        self.assertEqual(4, len(paired["class_proposal"]["polarity_disagreements"]))
        self.assertTrue(any("A2: class_id is 'Sample' in positive and 'd2r2' in negative; the class is proposed from the "
                            "letters." in item for item in paired["warnings"]), paired["warnings"])
        # Paired by their sample numbers, with a factor both polarities name: the factor stands.
        positive = [{"file_name": f"20230125_S{index}_{group}_C_d1", "file_type": "Sample", "class_id": group}
                    for index, group in ((1, "Ctrl"), (2, "Ctrl"), (3, "Case"), (4, "Case"))]
        negative = [{"file_name": f"20230208_S{index}_{group}_A_d2", "file_type": "Sample", "class_id": group}
                    for index, group in ((1, "Ctrl"), (2, "Ctrl"), (3, "Case"), (4, "Case"))]
        paired = core.pair_by_shared_tokens(positive, negative)
        self.assertEqual({}, paired["class_proposal"])
        self.assertEqual(["Case", "Case", "Ctrl", "Ctrl"], sorted(row["class_id"] for row in paired["rows"]))

    def test_a_batch_whose_polarities_are_named_apart_is_paired_merged_and_kept_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory, capabilities_patches():
            batch = MetabolomicsBatch(Path(directory), ("Positive", "Negative"))
            # The negative files acquired another day, under other names.
            for stem, _ in STEMS:
                old = batch.folders["Negative"] / f"{stem}_Neg.mzML"
                old.rename(batch.folders["Negative"] / f"20230208_{stem}_A_d2.mzML")
            for stem, _ in STEMS:
                old = batch.folders["Positive"] / f"{stem}_Pos.mzML"
                old.rename(batch.folders["Positive"] / f"20230125_{stem}_C_d1.mzML")
            check = agent.check_inputs(batch.request())
            self.assertTrue(check["ready_for_contract"], check["blockers"])
            table = check["paired_sample_table"]
            self.assertIn("words that set", table["pairing"])
            self.assertEqual({"S1", "S2", "QC1", "Blank"}, {row["stem"] for row in table["rows"]})
            contract = batch.contract()
            from lab_metabolomics_fixture import NEGATIVE_SPOTS, POSITIVE_SPOTS, _samples
            import lab_metabolomics_fixture as fixture

            renamed = {"Positive": "20230125_{}_C_d1", "Negative": "20230208_{}_A_d2"}
            original = fixture._samples
            fixture._samples = lambda label, stems: [
                (renamed["Positive" if label == "Pos" else "Negative"].format(stem), fixture.CLASSES[stem], kind)
                for stem, kind in stems]
            try:
                jobs = {polarity: write_run(batch, contract, polarity) for polarity in ("Positive", "Negative")}
            finally:
                fixture._samples = original
            for polarity, job in jobs.items():
                resolved = agent.resolve_standards_for_contract(contract, {polarity: job})
                plan = agent.plan_normalize(contract, job, resolved["polarities"][polarity]["resolution_digest"])
                self.assertTrue(agent.finish_normalize(plan)["verified"])
            report = agent.merge(contract, write=True)
            self.assertEqual(["S1", "S2", "QC1", "Blank"], report["samples"])
            header = Path(report["outputs"]["metabolome"]["path"]).read_text(encoding="utf-8").splitlines()[0]
            self.assertTrue(header.endswith('"S1","S2","QC1","Blank"'), header)

    def test_ms_dial_four_column_standards_text_is_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory, capabilities_patches():
            batch = MetabolomicsBatch(Path(directory))
            text = batch.root / "data" / "Identification_cation.txt"
            text.write_text("Name\tPos-m/z\tRT\tAdduct\r\nMethionine-d3\t153.07716\t0.896\t[M+H]+\r\n"
                            "CUDA\t341.27987\t2.87\t[M+H]+\r\n", encoding="ascii")
            contract = agent.load_contract(batch.contract(is_text_pos=str(text)))
            derived = Path(contract["derived"]["internal_standard_libraries"]["Positive"]["path"])
            lines = derived.read_text(encoding="ascii").splitlines()
            self.assertEqual("Internal standard", lines[1].split("\t")[7], "a standard given without a class gets one")


# Eight recurring isolation windows, as the demo's SWATH files record them.
WINDOWS = [100.0 + 75.0 * index for index in range(8)]
# The demo's naming: the polarities acquired on other days, and two negative files re-injected (d2r2).
DEMO = [("A1", "d2"), ("A2", "d2"), ("A3", "d2r2"), ("B1", "d2"), ("B2", "d2"), ("B3", "d2")]


def demo_batch(root: Path) -> MetabolomicsBatch:
    batch = MetabolomicsBatch(root, ("Positive", "Negative"))
    for folder in batch.folders.values():
        for path in folder.iterdir():
            path.unlink()
    for name, suffix in DEMO:
        (batch.folders["Positive"] / f"20230125_SWATH_{name}_C_CE20_d1.mzML").write_text("", encoding="ascii")
        (batch.folders["Negative"] / f"20230208_SWATH_{name}_A_CE20_{suffix}.mzML").write_text("", encoding="ascii")
    return batch


def types_by_stem(check: dict) -> dict:
    return {row["stem"]: row["acquisition_type"] for row in check["paired_sample_table"]["rows"]}


class RawHeaders(unittest.TestCase):
    """Each file's acquisition type and the analytical order come from its raw header, or are asked."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def check(self, batch: MetabolomicsBatch, extractor: FakeExtractor, **extra) -> dict:
        with capabilities_patches(extractor=extractor):
            return agent.check_inputs(batch.request(**extra))

    def write(self, batch: MetabolomicsBatch, check: dict, rows: list | None = None, **extra) -> dict:
        request = {**batch.request(**extra), "check_digest": check["check_digest"], "sample_table_confirmed": True,
                   "confirmed_by": "the analyst", "confirmed": True}
        if rows is not None:
            request["sample_table"] = rows
        return agent.write_contract(request)

    def test_dia_with_isolation_windows_is_swath_and_reaches_the_analysis_file(self) -> None:
        batch = demo_batch(self.root)
        extractor = FakeExtractor(lambda path: header_record(path, "DIA", WINDOWS))
        check = self.check(batch, extractor)
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual({"SWATH"}, set(types_by_stem(check).values()))
        self.assertEqual({"raw_header"}, {row["acquisition_type_basis"] for row in check["paired_sample_table"]["rows"]})
        for polarity in ("Positive", "Negative"):
            report = check["raw_headers"]["polarities"][polarity]
            self.assertEqual(("all", 6, {"DIA": 6}, [8], {"SWATH": 6}, {"raw_header": 6}),
                             (report["coverage"], report["files_read"], report["methods"], report["isolation_targets"],
                              report["acquisition_types"], report["proposed_by"]))
            self.assertEqual(["Q Exactive Plus Orbitrap"], report["instruments"])
            self.assertEqual({"SWATH"}, {item["acquisition_type"] for item in report["per_file"]})
        # The build the headers were read with, by the hash of its exe and of every reader beside it.
        self.assertEqual(("setting", "a" * 64, "b" * 64),
                         tuple(check["raw_headers"]["extractor"][key] for key in ("source", "inventory_sha256", "binary_sha256")))
        with capabilities_patches(extractor=extractor):
            written = self.write(batch, check)
            self.assertTrue(written["written"], written)
            contract = agent.load_contract(written["contract_path"])
            self.assertEqual({"SWATH"}, {row["acquisition_type"] for row in contract["sample_table"]["rows"]})
            self.assertEqual({"DIA": 6}, contract["raw_headers"]["polarities"]["Negative"]["methods"])
            self.assertEqual("a" * 64, contract["raw_headers"]["extractor"]["inventory_sha256"])
            plan = build_guided_plan(str(batch.folders["Positive"]), {
                agent.CONTRACT_KEY: written["contract_path"], "ion_mode": "Positive", "minimum_peak_height": 500})
            self.assertTrue(plan["ready_to_prepare"], (plan["blockers"], plan["remaining_questions"]))
            prepared = prepare_run(plan["workflow"])
        rows = list(csv.DictReader(io.StringIO(
            (Path(prepared["run_directory"]) / "analysis_files.csv").read_text(encoding="ascii"))))
        self.assertEqual(6, len(rows))
        self.assertEqual({"SWATH"}, {row["acquisition_type"] for row in rows})

    def test_the_re_injected_files_pair_and_the_replicates_letters_are_the_class(self) -> None:
        batch = demo_batch(self.root)
        check = self.check(batch, FakeExtractor(lambda path: header_record(path, "DIA", WINDOWS)))
        table = check["paired_sample_table"]
        self.assertIn("words that set", table["pairing"])
        self.assertEqual("20230208_SWATH_A3_A_CE20_d2r2",
                         next(row["negative_file"] for row in table["rows"] if row["stem"] == "A3"))
        # d2 against d2r2 varies only in the negative files, and the names give no class: the letters do.
        self.assertEqual({"A1": "A", "A2": "A", "A3": "A", "B1": "B", "B2": "B", "B3": "B"},
                         {row["stem"]: row["class_id"] for row in table["rows"]})
        self.assertEqual("shared_sample_word_letters", table["class_proposal"]["derived_from"])
        self.assertTrue(any("shared sample word's letters (A: 3, B: 3)" in item for item in check["warnings"]),
                        check["warnings"])
        self.assertFalse(any("class_id is" in item for item in check["warnings"]), check["warnings"])

    def test_an_aif_verdict_and_one_isolation_target_are_asked_and_dda_is_dda(self) -> None:
        batch = MetabolomicsBatch(self.root)
        for method, targets, expected, suggested in (("DIA", [], None, "AIF"), ("DDA", [], "DDA", None),
                                                     ("DIA", [500.0], None, None)):
            with self.subTest(method=method, targets=targets):
                check = self.check(batch, FakeExtractor(lambda path: header_record(path, method, targets, energies=[20.0])))
                self.assertEqual({expected}, set(types_by_stem(check).values()))
                questions = {item["id"]: item for item in check["questions"]}
                self.assertEqual(expected is None, "acquisition_type_pos" in questions)
                per_file = check["raw_headers"]["polarities"]["Positive"]["per_file"]
                self.assertEqual({method}, {item["method"] for item in per_file})
                self.assertEqual({(20.0,)}, {tuple(item["ms2_collision_energies"]) for item in per_file})
                if expected is None:
                    self.assertEqual(["DDA", "SWATH", "AIF"], questions["acquisition_type_pos"]["choices"])
                if suggested:
                    # The header's AIF is read out with the energy, and never filled in.
                    prompt = questions["acquisition_type_pos"]["prompt"]
                    self.assertIn("(MS2 collision energies 20 eV) give AIF (DIA without isolation)", prompt)
                    self.assertIn("The headers suggest AIF for 4 of them", prompt)
                elif expected is None:
                    self.assertIn("S1_Pos (DIA, 1 isolation target)", questions["acquisition_type_pos"]["prompt"])
        check = self.check(batch, FakeExtractor(lambda path: header_record(path, "DIA", [], energies=[20.0])),
                           acquisition_type_pos="swath")
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual({"SWATH"}, set(types_by_stem(check).values()))
        self.assertEqual({"answer"}, {row["acquisition_type_basis"] for row in check["paired_sample_table"]["rows"]})

    def test_aif_meets_the_campaigns_collision_energy_guards(self) -> None:
        batch = MetabolomicsBatch(self.root)
        with_825 = {**EVERY_CAPABILITY, "capabilities": sorted({*EVERY_CAPABILITY["capabilities"], MULTI_ENERGY_AIF_CAPABILITY})}
        # The QC acquired at 40 eV, the rest at 20 eV: one energy each, two over the polarity, as the campaign counts.
        apart = {"QC1_Pos": [40.0]}
        for energies, found, refused, warned in (
            ([20.0, 40.0], None, "record 2 MS2 collision energies among them (20, 40 eV: Blank_Pos, QC1_Pos, S1_Pos, "
                                 "S2_Pos)", ""),
            ([20.0, 40.0], with_825, "", ""),
            (apart, None, "record 2 MS2 collision energies among them (20 eV: Blank_Pos, S1_Pos, S2_Pos; 40 eV: QC1_Pos)",
             ""),
            (apart, with_825, "", "record different sets of MS2 collision energies (20 eV: Blank_Pos, S1_Pos, S2_Pos; "
                                  "40 eV: QC1_Pos)"),
            (None, with_825, "which stops on an AIF file", ""),
            (None, None, "", "the repository campaign holds such AIF data"),
            ([20.0], None, "", ""),
        ):
            with self.subTest(energies=energies, console_825=found is not None):
                extractor = FakeExtractor(lambda path: header_record(path, "DIA", [], energies=(
                    energies.get(Path(path).stem, [20.0]) if isinstance(energies, dict) else energies)))
                with capabilities_patches(found, extractor=extractor):
                    check = agent.check_inputs(batch.request(acquisition_type_pos="AIF"))
                self.assertEqual({"AIF"}, set(types_by_stem(check).values()))
                if refused:
                    self.assertTrue(any(refused in item for item in check["blockers"]), check["blockers"])
                else:
                    self.assertEqual([], check["blockers"])
                if warned:
                    self.assertTrue(any(warned in item for item in check["warnings"]), check["warnings"])
        # A type stated in sample_table meets the same guard, over the files stated AIF.
        extractor = FakeExtractor(lambda path: header_record(path, "DIA", [], energies=apart.get(Path(path).stem, [20.0])))
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request(acquisition_type_pos="SWATH"))
            self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
            for aif, refused in ((("S1", "QC1"), True), (("S1", "S2"), False)):
                with self.subTest(stated_aif=aif):
                    rows = [{"stem": stem, "class_id": CLASSES[stem], "acquisition_type": "AIF" if stem in aif else "SWATH",
                             **({} if kind == "Sample" else {"file_type": kind})} for stem, kind in STEMS]
                    result = self.write(batch, check, rows, acquisition_type_pos="SWATH")
                    if refused:
                        self.assertIn("(20 eV: S1_Pos; 40 eV: QC1_Pos)", result.get("error", ""))
                    else:
                        self.assertTrue(result.get("written"), result)

    def test_a_header_that_cannot_be_read_is_asked_kept_and_the_answer_fills_only_that_file(self) -> None:
        batch = MetabolomicsBatch(self.root)
        extractor = FakeExtractor(lambda path: None if Path(path).stem == "QC1_Pos" else header_record(path))
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request())
            question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
            self.assertIn("QC1_Pos could not be read", question["prompt"])
            self.assertIn("(1 not read)", question["prompt"])
            self.assertIn("The 3 file(s) read from their own headers keep their type (DDA 3)", question["prompt"])
            self.assertFalse(check["ready_for_contract"])
            self.assertEqual({"S1": "DDA", "S2": "DDA", "QC1": None, "Blank": "DDA"}, types_by_stem(check))
            self.assertEqual(["QC1_Pos"], check["raw_headers"]["polarities"]["Positive"]["unreadable"])
            check = agent.check_inputs(batch.request(acquisition_type_pos="swath"))
            # The failed read is kept like a good one: checking again does not wait for it again.
            self.assertEqual(1, len(extractor.calls))
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual({"S1": "DDA", "S2": "DDA", "QC1": "SWATH", "Blank": "DDA"}, types_by_stem(check))
        self.assertTrue(any("acquisition_type_pos is SWATH for the 1 positive file(s) no header of their own settled "
                            "(1 not read); the 3 file(s) read from their own headers keep their headers' type (DDA 3)"
                            in item for item in check["warnings"]), check["warnings"])
        self.assertTrue(any("The positive files are of more than one acquisition type (DDA 3, SWATH 1)" in item
                            for item in check["warnings"]), check["warnings"])
        check = self.check(batch, extractor, acquisition_type_pos="DIA")
        self.assertTrue(any("acquisition_type_pos 'DIA' is none of DDA, SWATH or AIF" in item for item in check["blockers"]))

    def test_without_an_extractor_the_type_is_asked_never_assumed(self) -> None:
        batch = MetabolomicsBatch(self.root)
        extractor = FakeExtractor(configured=False)
        check = self.check(batch, extractor)
        question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
        self.assertIn("No raw-metadata extractor is configured", question["prompt"])
        self.assertIsNone(check["raw_headers"]["extractor"])
        self.assertEqual({None}, set(types_by_stem(check).values()))
        self.assertEqual([], extractor.calls)
        check = self.check(batch, extractor, acquisition_type_pos="DDA")
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual({"answer": 4}, check["raw_headers"]["polarities"]["Positive"]["proposed_by"])

    def test_the_analytical_order_follows_the_acquisition_start_times(self) -> None:
        batch = MetabolomicsBatch(self.root)
        times = {"S1_Pos": "2023-01-25T09:00:00+00:00", "S2_Pos": "2023-01-25T09:30:00+00:00",
                 "QC1_Pos": "2023-01-25T10:00:00.1234567+00:00", "Blank_Pos": "2023-01-25T11:00:00+00:00"}
        extractor = FakeExtractor(lambda path: header_record(path, start=times.get(Path(path).stem)))
        check = self.check(batch, extractor)
        table = check["paired_sample_table"]
        self.assertEqual([("S1", 1), ("S2", 2), ("QC1", 3), ("Blank", 4)],
                         [(row["stem"], row["analytical_order"]) for row in table["rows"]])
        self.assertEqual("raw_header_acquisition_start_time", table["order_proposal"]["derived_from"])
        self.assertFalse(table["order_proposal"]["agrees_with_file_listing"])
        per_file = {item["file"]: item["acquisition_start_time"]
                    for item in check["raw_headers"]["polarities"]["Positive"]["per_file"]}
        self.assertEqual(times["QC1_Pos"], per_file["QC1_Pos"])
        # One file without a time: the order proposed from the names stands, and the reason is said.
        del times["Blank_Pos"]
        check = self.check(batch, extractor)
        table = check["paired_sample_table"]
        self.assertNotEqual("raw_header_acquisition_start_time", table["order_proposal"]["derived_from"])
        self.assertIn("no readable acquisition start time was recorded for Blank_Pos",
                      table["order_proposal"]["raw_headers"]["reason"])
        self.assertEqual(["Blank", "QC1", "S1", "S2"], [row["stem"] for row in table["rows"]])

    def test_the_order_is_the_headers_only_when_every_polaritys_files_record_a_time(self) -> None:
        batch = MetabolomicsBatch(self.root, ("Positive", "Negative"))
        ranks = {stem: rank for rank, (stem, _kind) in enumerate(reversed(STEMS))}
        # Every positive file records a time; no negative file does.
        extractor = FakeExtractor(lambda path: header_record(path, start=(
            f"2023-01-25T10:0{ranks[Path(path).stem.rsplit('_', 1)[0]]}:00+00:00" if Path(path).stem.endswith("_Pos") else None
        )))
        check = self.check(batch, extractor)
        table = check["paired_sample_table"]
        self.assertNotEqual("raw_header_acquisition_start_time", table["order_proposal"].get("derived_from"))
        self.assertIn("negative: no readable acquisition start time was recorded",
                      table["order_proposal"]["raw_headers"]["reason"])
        self.assertEqual("raw_header_acquisition_start_time",
                         check["raw_headers"]["polarities"]["Positive"]["order"]["derived_from"])

    def test_polarities_acquired_in_another_order_are_said(self) -> None:
        batch = demo_batch(self.root)
        samples = [name for name, _ in DEMO]

        def record(path: Path) -> dict:
            stem = Path(path).stem
            rank = samples.index(stem.split("_")[2])
            # The positive run in the listing's order, the negative one in reverse.
            minute = rank if "_C_" in stem else len(samples) - rank
            return header_record(path, "DIA", WINDOWS, start=f"2023-01-25T10:{minute:02d}:00+00:00")

        check = self.check(batch, FakeExtractor(record))
        table = check["paired_sample_table"]
        self.assertEqual([(name, rank) for rank, name in enumerate(samples, start=1)],
                         [(row["stem"], row["analytical_order"]) for row in table["rows"]])
        self.assertTrue(any("The negative files were acquired in another order" in item for item in check["warnings"]),
                        check["warnings"])

    def test_a_pair_whose_files_differ_carries_no_type_until_the_person_states_it(self) -> None:
        batch = MetabolomicsBatch(self.root, ("Positive", "Negative"))
        # S2's positive header says DDA beside its SWATH negative partner.
        extractor = FakeExtractor(lambda path: header_record(path, "DDA" if Path(path).stem == "S2_Pos" else "DIA", WINDOWS))
        answers = {"acquisition_type_mixed_pos": "as_read"}
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request(**answers))
            self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
            table = check["paired_sample_table"]
            self.assertEqual({"S2": {"Positive": "DDA", "Negative": "SWATH"}}, table["acquisition_type_conflicts"])
            self.assertEqual(["S2"], table["acquisition_type_required"])
            # Neither polarity's type is proposed for the pair: an echoed table carries none.
            self.assertEqual((None, "polarities_disagree"),
                             next((row["acquisition_type"], row["acquisition_type_basis"]) for row in table["rows"]
                                  if row["stem"] == "S2"))
            self.assertTrue(any("S2 (DDA positive, SWATH negative)" in item for item in check["warnings"]), check["warnings"])
            # The proposal sent back as it stands, one class corrected, as a correction table must be.
            echoed = [dict(row, class_id="Case2") if row["stem"] == "S2" else dict(row) for row in table["rows"]]
            refused = self.write(batch, check, echoed, **answers)
            self.assertIn("S2 (DDA positive, SWATH negative)", refused.get("error", ""))
            blank = [dict(row, acquisition_type=" ") if row["stem"] == "S2" else row for row in echoed]
            refused = self.write(batch, check, blank, **answers)
            self.assertIn("S2 (DDA positive, SWATH negative)", refused.get("error", ""))
            stated = [dict(row, acquisition_type="swath") if row["stem"] == "S2" else row for row in echoed]
            written = self.write(batch, check, stated, **answers)
            self.assertTrue(written["written"], written)
        contract = agent.load_contract(written["contract_path"])
        rows = {row["stem"]: row for row in contract["sample_table"]["rows"]}
        self.assertEqual({"SWATH"}, {row["acquisition_type"] for row in rows.values()})
        self.assertEqual("sample_table", rows["S2"]["acquisition_type_basis"])
        self.assertEqual({"acquisition_type_mixed_pos": "as_read"}, contract["raw_headers"]["answers"])

    def test_files_whose_own_headers_differ_are_asked_about(self) -> None:
        batch = MetabolomicsBatch(self.root)
        extractor = FakeExtractor(lambda path: header_record(path, "DDA" if Path(path).stem == "QC1_Pos" else "DIA", WINDOWS))
        check = self.check(batch, extractor)
        question = next(item for item in check["questions"] if item["id"] == "acquisition_type_mixed_pos")
        self.assertIn("(DDA 1: QC1_Pos; SWATH 3: ", question["prompt"])
        self.assertEqual(["as_read"], question["choices"])
        self.assertFalse(check["ready_for_contract"])
        check = self.check(batch, extractor, acquisition_type_mixed_pos="AS_READ")
        self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
        self.assertEqual({"S1": "SWATH", "S2": "SWATH", "QC1": "DDA", "Blank": "SWATH"}, types_by_stem(check))
        check = self.check(batch, extractor, acquisition_type_mixed_pos="yes")
        self.assertTrue(any("acquisition_type_mixed_pos 'yes' is not as_read" in item for item in check["blockers"]))

    def test_a_header_of_the_other_polarity_or_of_both_refuses_the_check(self) -> None:
        batch = MetabolomicsBatch(self.root)
        for polarity, said in (("Negative", "record the other polarity"), ("PolaritySwitching", "record both polarities")):
            with self.subTest(polarity=polarity):
                check = self.check(batch, FakeExtractor(
                    lambda path: header_record(path, polarity=polarity if Path(path).stem == "S2_Pos" else "Positive")))
                self.assertTrue(any(f"S2_Pos{' (PolaritySwitching)' if polarity == 'PolaritySwitching' else ''} in the "
                                    f"positive folder {said}" in item for item in check["blockers"]), check["blockers"])
                self.assertFalse(check["ready_for_contract"])

    def test_past_the_limit_the_files_between_the_first_and_the_last_are_asked(self) -> None:
        batch = MetabolomicsBatch(self.root)
        extractor = FakeExtractor(lambda path: header_record(path, "DIA", WINDOWS))
        with patch.object(agent, "HEADER_READ_ALL_LIMIT", 2), capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request())
            self.assertEqual([["Blank_Pos.mzML", "S2_Pos.mzML"]], extractor.calls)
            report = check["raw_headers"]["polarities"]["Positive"]
            self.assertEqual(("first_and_last", 2, {"first_and_last_files": 2, "raw_header": 2}),
                             (report["coverage"], report["files_read"], report["proposed_by"]))
            # The two ends' type is a suggestion only: the files between them are asked about.
            self.assertEqual({"Blank": "SWATH", "QC1": None, "S1": None, "S2": "SWATH"}, types_by_stem(check))
            question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
            self.assertIn("(2 inferred from the first and the last file, not read)", question["prompt"])
            self.assertIn("The headers suggest SWATH for 2 of them", question["prompt"])
            self.assertFalse(check["ready_for_contract"])
            self.assertTrue(any("Only the first and the last of the 4 positive raw files were read" in item
                                for item in check["warnings"]), check["warnings"])
            self.assertIn("only the first and the last", check["paired_sample_table"]["order_proposal"]["raw_headers"]["reason"])
            # An answer other than the ends' type is taken for the files between, and said with both counts.
            check = agent.check_inputs(batch.request(acquisition_type_pos="DDA"))
            self.assertEqual({"Blank": "SWATH", "QC1": "DDA", "S1": "DDA", "S2": "SWATH"}, types_by_stem(check))
            self.assertTrue(any("acquisition_type_pos is DDA for the 2 positive file(s) no header of their own settled "
                                "(2 inferred from the first and the last file, not read); the 2 file(s) read from their "
                                "own headers keep their headers' type (SWATH 2)" in item for item in check["warnings"]),
                            check["warnings"])
            check = agent.check_inputs(batch.request(acquisition_type_pos="SWATH"))
            self.assertEqual({"answer": 2, "raw_header": 2},
                             check["raw_headers"]["polarities"]["Positive"]["proposed_by"])
            written = self.write(batch, check, acquisition_type_pos="SWATH")
            self.assertTrue(written["written"], written)
        contract = agent.load_contract(written["contract_path"])
        self.assertEqual({"acquisition_type_pos": "SWATH"}, contract["raw_headers"]["answers"])
        self.assertEqual({"SWATH"}, {row["acquisition_type"] for row in contract["sample_table"]["rows"]})
        # Two that disagree suggest nothing for the files between them.
        extractor = FakeExtractor(lambda path: header_record(path, "DDA" if Path(path).stem == "Blank_Pos" else "DIA", WINDOWS))
        with patch.object(agent, "HEADER_READ_ALL_LIMIT", 2):
            check = self.check(batch, extractor)
        question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
        self.assertNotIn("suggest", question["prompt"])
        self.assertEqual({"Blank": "DDA", "QC1": None, "S1": None, "S2": "SWATH"}, types_by_stem(check))

    def test_a_check_again_reads_no_header_twice_and_the_digest_binds_what_was_read(self) -> None:
        batch = MetabolomicsBatch(self.root)
        extractor = FakeExtractor(lambda path: header_record(path, "DIA", WINDOWS))
        with capabilities_patches(extractor=extractor):
            first = agent.check_inputs(batch.request())
            second = agent.check_inputs(batch.request())
            self.assertEqual(1, len(extractor.calls))
            self.assertEqual(first["check_digest"], second["check_digest"])
            # Another extractor build reads every file again.
            extractor.inventory = "c" * 64
            agent.check_inputs(batch.request())
            self.assertEqual(2, len(extractor.calls))
            self.assertEqual(4, len(extractor.calls[-1]))
        # The same files, read otherwise: the table the person saw is not the one that would be written.
        with capabilities_patches(extractor=FakeExtractor()):
            refused = agent.write_contract({**batch.request(), "check_digest": first["check_digest"],
                                            "sample_table_confirmed": True})
        self.assertIn("changed after it was checked", refused["error"])
        # A file rewritten is read again; the others come from the cache.
        with capabilities_patches(extractor=extractor):
            agent.check_inputs(batch.request())
            (batch.folders["Positive"] / "S1_Pos.mzML").write_text("changed", encoding="ascii")
            agent.check_inputs(batch.request())
        self.assertEqual(["S1_Pos.mzML"], extractor.calls[-1])

    def test_the_reads_answer_in_time_and_a_file_left_unread_is_asked_and_read_next_time(self) -> None:
        # Inside the MCP request's 600 s, write_contract's own check included.
        self.assertLess(agent.HEADER_READ_BUDGET_SECONDS + 2 * 10, 600)
        self.assertLess(max(item["base_seconds"] for item in agent.HEADER_TIMEOUT_POLICY.values()),
                        agent.HEADER_READ_BUDGET_SECONDS)
        batch = MetabolomicsBatch(self.root)
        late = {"QC1_Pos"}
        extractor = FakeExtractor(lambda path: "late" if Path(path).stem in late else header_record(path))
        with capabilities_patches(extractor=extractor):
            before = time.monotonic()
            check = agent.check_inputs(batch.request())
            options = extractor.options[0]
            self.assertEqual(agent.HEADER_TIMEOUT_POLICY, options["timeout_policy"])
            # Chunks of 4 files at 30 s each take at most half the check's 240 s.
            self.assertEqual(4, options["chunk_inputs"])
            self.assertEqual(agent.HEADER_INPUT_TIMEOUT_CEILING_SECONDS, options["timeout_ceiling_seconds"])
            self.assertLessEqual(options["deadline"], time.monotonic() + agent.HEADER_READ_BUDGET_SECONDS)
            self.assertGreaterEqual(options["deadline"], before + agent.HEADER_READ_BUDGET_SECONDS - 1)
            report = check["raw_headers"]["polarities"]["Positive"]
            self.assertEqual(["QC1_Pos"], report["not_read_in_time"])
            self.assertIn({"file": "QC1_Pos", "read": "not_read_time_limit"}, report["per_file"])
            question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
            self.assertIn("QC1_Pos were not read within the check's 240 s; checking again reads them first, one at a "
                          "time", question["prompt"])
            # A read the time limit cut short is not kept: the next check reads that file, and only it, on its own.
            late.clear()
            check = agent.check_inputs(batch.request())
            self.assertEqual(["QC1_Pos.mzML"], extractor.calls[-1])
            self.assertEqual(1, extractor.options[-1]["chunk_inputs"])
            self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))

    def test_a_second_check_waits_for_the_read_in_flight(self) -> None:
        batch = MetabolomicsBatch(self.root)
        entered, release = threading.Event(), threading.Event()

        def during(path: Path, work: Path) -> None:
            entered.set()
            release.wait(10)

        extractor = FakeExtractor(lambda path: header_record(path, "DIA", WINDOWS), during=during)
        results: dict = {}
        with capabilities_patches(extractor=extractor):
            first = threading.Thread(target=lambda: results.__setitem__("first", agent.check_inputs(batch.request())))
            second = threading.Thread(target=lambda: results.__setitem__("second", agent.check_inputs(batch.request())))
            first.start()
            self.assertTrue(entered.wait(10))
            second.start()
            second.join(1.0)
            self.assertTrue(second.is_alive(), "the second check waits for the first one's read")
            release.set()
            first.join(10)
            second.join(10)
        self.assertEqual(1, len(extractor.calls))
        self.assertEqual(results["first"]["check_digest"], results["second"]["check_digest"])
        self.assertEqual({"SWATH"}, set(types_by_stem(results["second"]).values()))

    def test_a_bruker_baf_folder_is_never_given_to_the_reader(self) -> None:
        batch = MetabolomicsBatch(self.root)
        folder = batch.folders["Positive"] / "S3_Pos.d"
        folder.mkdir()
        (folder / "analysis.baf").write_bytes(b"baf")
        extractor = FakeExtractor()
        check = self.check(batch, extractor)
        self.assertTrue(extractor.calls)
        self.assertNotIn("S3_Pos.d", [name for call in extractor.calls for name in call])
        self.assertIn({"file": "S3_Pos", "read": "not_read_reader_writes_into_folder"},
                      check["raw_headers"]["polarities"]["Positive"]["per_file"])
        question = next(item for item in check["questions"] if item["id"] == "acquisition_type_pos")
        self.assertIn("S3_Pos were not read: the extractor's reader writes analysis.sqlite", question["prompt"])
        self.assertEqual(["analysis.baf"], sorted(item.name for item in folder.iterdir()))

    def test_the_reader_works_outside_the_raw_data_in_a_folder_removed_afterwards(self) -> None:
        batch = MetabolomicsBatch(self.root, ("Positive", "Negative"))
        raw = [folder.resolve() for folder in batch.folders.values()]

        def during(path: Path, work: Path) -> None:
            self.assertTrue(work.is_dir())
            for folder in raw:
                self.assertFalse(work.resolve().is_relative_to(folder), work)
            (work / "chunk-0000.json").write_text("[]", encoding="ascii")

        extractor = FakeExtractor(during=during)
        before = {folder: sorted(item.name for item in folder.iterdir()) for folder in raw}
        self.check(batch, extractor)
        self.assertEqual(1, len(extractor.work_directories))
        self.assertFalse(extractor.work_directories[0].exists())
        self.assertEqual(before, {folder: sorted(item.name for item in folder.iterdir()) for folder in raw})

    def test_what_a_reader_leaves_beside_the_raw_data_is_said_again_and_recorded(self) -> None:
        batch = MetabolomicsBatch(self.root)
        folder = batch.folders["Positive"]

        def during(path: Path, work: Path) -> None:
            if path.name == "S1_Pos.mzML":
                (folder / "S1_Pos.mzML.index").write_text("", encoding="ascii")

        extractor = FakeExtractor(during=during)
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request())
            said = [item for item in check["warnings"] if "Reading the headers left files in or beside the raw data" in item]
            self.assertTrue(said and "S1_Pos.mzML.index" in said[0], check["warnings"])
            # Checked again, the read comes from the cache, and the warning stays; so does the digest.
            again = agent.check_inputs(batch.request())
            self.assertEqual(1, len(extractor.calls))
            self.assertIn(said[0], again["warnings"])
            self.assertEqual(check["check_digest"], again["check_digest"])
            written = self.write(batch, check)
            self.assertTrue(written["written"], written)
        contract = agent.load_contract(written["contract_path"])
        self.assertEqual([str(folder.resolve() / "S1_Pos.mzML.index")],
                         contract["raw_headers"]["polarities"]["Positive"]["reader_created_files"])
        # A folder input: what run_extractor says its reader created is said, and kept the same way.
        batch = MetabolomicsBatch(self.root / "second")
        waters = batch.folders["Positive"] / "S3_Pos.raw"
        waters.mkdir()
        (waters / "_FUNC001.DAT").write_bytes(b"x")
        extractor = FakeExtractor(created=lambda path: [path / "_INDEX.idx"] if path.suffix == ".raw" else [])
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request())
            again = agent.check_inputs(batch.request())
        for result in (check, again):
            self.assertEqual([str(waters.resolve() / "_INDEX.idx")],
                             result["raw_headers"]["polarities"]["Positive"]["reader_created_files"])

    def test_a_file_that_hangs_costs_no_other_file_its_read_and_is_kept_as_failed(self) -> None:
        from msdial_app import raw_metadata_preflight as preflight

        stems = [(f"S{index:02d}", "Sample") for index in range(1, 21)]
        batch = MetabolomicsBatch(self.root, stems=stems)
        commands: list[list[str]] = []

        def extractor_process(command, **options):
            inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
            commands.append([Path(item).stem for item in inputs])
            if any(Path(item).stem == "S07_Pos" for item in inputs):
                # It hangs until the time limit it is given ends.
                time.sleep(options["timeout"])
                raise subprocess.TimeoutExpired(command, options["timeout"])
            output = Path(command[command.index("--output") + 1])
            output.write_text(json.dumps([header_record(item, "DIA", WINDOWS) for item in inputs]), encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        quick = {name: {"base_seconds": 0.2, "seconds_per_gb": 0.0} for name in agent.HEADER_TIMEOUT_POLICY}
        with capabilities_patches(extractor=FakeExtractor()), \
                patch.object(agent, "run_extractor", preflight.run_extractor), \
                patch("msdial_app.raw_metadata_preflight.subprocess.run", side_effect=extractor_process), \
                patch.object(agent, "HEADER_TIMEOUT_POLICY", quick), \
                patch.object(agent, "HEADER_READ_BUDGET_SECONDS", 2.0):
            check = agent.check_inputs(batch.request())
            # Chunks of 5 (half the time each at most): the hanging one ends on its own limit, its other files are
            # read one at a time, and the hanging file fails on its own.
            self.assertEqual(5, max(len(command) for command in commands))
            self.assertIn(["S07_Pos"], commands)
            report = check["raw_headers"]["polarities"]["Positive"]
            self.assertEqual(["S07_Pos"], report["unreadable"])
            self.assertEqual([], report["not_read_in_time"])
            self.assertEqual({"SWATH": 19, "unresolved": 1}, report["acquisition_types"])
            # Kept as a failed read: checking again does not wait for it again.
            count = len(commands)
            again = agent.check_inputs(batch.request())
            self.assertEqual(count, len(commands))
            self.assertEqual(check["check_digest"], again["check_digest"])

    def test_a_read_is_kept_for_the_input_as_it_became_only_when_its_reader_changed_it(self) -> None:
        batch = MetabolomicsBatch(self.root)
        source = batch.folders["Positive"] / "S1_Pos.mzML"

        def copying(path: Path, work: Path) -> None:
            # A copy still writing the file while it is read.
            if path.name == "S1_Pos.mzML" and not source.read_text(encoding="ascii"):
                source.write_text("the rest of the file", encoding="ascii")

        extractor = FakeExtractor(during=copying)
        with capabilities_patches(extractor=extractor):
            agent.check_inputs(batch.request())
            agent.check_inputs(batch.request())
        self.assertEqual(["S1_Pos.mzML"], extractor.calls[-1])
        # A folder its reader wrote into is read once.
        batch = MetabolomicsBatch(self.root / "second")
        waters = batch.folders["Positive"] / "S3_Pos.raw"
        waters.mkdir()
        (waters / "_FUNC001.DAT").write_bytes(b"x")

        def writing(path: Path, work: Path) -> None:
            if path.suffix == ".raw":
                (path / "_INDEX.idx").write_bytes(b"index")

        extractor = FakeExtractor(during=writing, created=lambda path: [path / "_INDEX.idx"] if path.suffix == ".raw" else [])
        with capabilities_patches(extractor=extractor):
            agent.check_inputs(batch.request())
            agent.check_inputs(batch.request())
        self.assertEqual(1, len(extractor.calls))

    def test_a_file_answered_unread_and_read_as_aif_before_the_contract_is_shown_again(self) -> None:
        batch = MetabolomicsBatch(self.root)
        late = {"QC1_Pos"}
        # No header records an energy or a time: only why QC1 was asked, and what its header now suggests, change.
        extractor = FakeExtractor(lambda path: "late" if Path(path).stem in late
                                  else header_record(path, "DIA", [] if Path(path).stem == "QC1_Pos" else WINDOWS))
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request(acquisition_type_pos="SWATH"))
            self.assertTrue(check["ready_for_contract"], (check["questions"], check["blockers"]))
            late.clear()
            refused = self.write(batch, check, acquisition_type_pos="SWATH")
        self.assertIn("changed after it was checked", refused.get("error", ""))

    def test_a_file_that_appears_meanwhile_is_said_but_not_put_down_to_the_reader(self) -> None:
        batch = MetabolomicsBatch(self.root)
        folder = batch.folders["Positive"]

        def meanwhile(path: Path, work: Path) -> None:
            if path.name == "S1_Pos.mzML":
                (folder / "sync.tmp").write_text("", encoding="ascii")
                (folder / "S2_Pos.dcl").write_text("", encoding="ascii")

        extractor = FakeExtractor(during=meanwhile)
        with capabilities_patches(extractor=extractor):
            check = agent.check_inputs(batch.request())
            again = agent.check_inputs(batch.request())
        self.assertEqual([], check["raw_headers"]["polarities"]["Positive"]["reader_created_files"])
        said = [item for item in check["warnings"] if "appeared in the positive raw folder" in item]
        self.assertTrue(said and "sync.tmp" in said[0] and "S2_Pos.dcl" in said[0], check["warnings"])
        self.assertFalse(any("appeared in the positive raw folder" in item for item in again["warnings"]))
        self.assertFalse(any("left files in or beside the raw data" in item for item in check["warnings"]))
        self.assertEqual(check["check_digest"], again["check_digest"])

    def test_a_contract_of_an_earlier_version_or_with_a_row_of_no_type_is_refused(self) -> None:
        with capabilities_patches():
            batch = MetabolomicsBatch(self.root)
            contract = batch.contract()
            path = Path(contract)
            original = path.read_text(encoding="utf-8")
            answers = {agent.CONTRACT_KEY: contract, "ion_mode": "Positive", "minimum_peak_height": 500}
            data = json.loads(original)
            data["schema"] = "msdial-interactive.lab-lcms-metabolomics-contract.v1"
            path.write_text(json.dumps(data), encoding="utf-8")
            plan = build_guided_plan(str(batch.folders["Positive"]), answers)
            self.assertFalse(plan["ready_to_prepare"])
            self.assertTrue(any("written by an earlier version of the laboratory metabolomics agent" in item
                                for item in plan["blockers"]), plan["blockers"])
            for value in ("", "MRM"):
                with self.subTest(acquisition_type=value):
                    data = json.loads(original)
                    data["sample_table"]["rows"][0]["acquisition_type"] = value
                    path.write_text(json.dumps(data), encoding="utf-8")
                    plan = build_guided_plan(str(batch.folders["Positive"]), answers)
                    self.assertFalse(plan["ready_to_prepare"])
                    self.assertTrue(any("gives no acquisition type the Console reads for "
                                        + data["sample_table"]["rows"][0]["stem"] in item for item in plan["blockers"]),
                                    plan["blockers"])

    def test_sample_metadata_is_compared_with_the_table_proposed(self) -> None:
        batch = MetabolomicsBatch(self.root)
        order = {"S1": 1, "S2": 2, "QC1": 3, "Blank": 4}
        extractor = FakeExtractor(lambda path: header_record(
            path, start=f"2023-01-25T10:0{order[Path(path).stem.rsplit('_', 1)[0]]}:00+00:00"))
        sheet = batch.root / "data" / "samples.csv"
        for given, disagreements in ((order, 0), ({"Blank": 1, "QC1": 2, "S1": 3, "S2": 4}, 4)):
            with self.subTest(given=given):
                sheet.write_text("File name,Analytical order\n" + "".join(
                    f"{stem}_Pos,{rank}\n" for stem, rank in given.items()), encoding="ascii")
                check = self.check(batch, extractor, sample_metadata_path=str(sheet))
                self.assertEqual(disagreements, len(check["sample_metadata"]["joined"]["Positive"]["disagreements"]))
                said = [item for item in check["warnings"] if "samples.csv gives analytical_order" in item]
                self.assertEqual(disagreements, len(said))
                self.assertTrue(all("the proposed table gives" in item for item in said), said)

    def test_a_contract_written_before_the_headers_were_read_is_refused(self) -> None:
        with capabilities_patches():
            batch = MetabolomicsBatch(self.root)
            contract = batch.contract()
            path = Path(contract)
            data = json.loads(path.read_text(encoding="utf-8"))
            del data["raw_headers"]
            path.write_text(json.dumps(data), encoding="utf-8")
            plan = build_guided_plan(str(batch.folders["Positive"]), {
                agent.CONTRACT_KEY: contract, "ion_mode": "Positive", "minimum_peak_height": 500})
        self.assertFalse(plan["ready_to_prepare"])
        self.assertTrue(any("written before each file's acquisition type was read from its raw header" in item
                            for item in plan["blockers"]), plan["blockers"])


class AlignmentTolerance(Batched):
    def plan(self, contract: str, **extra) -> dict:
        answers = {agent.CONTRACT_KEY: contract, "ion_mode": "Positive", "minimum_peak_height": 500, **extra}
        return build_guided_plan(str(self.batch.folders["Positive"]), answers)

    def test_the_persons_alignment_tolerance_reaches_the_method_file(self) -> None:
        check = agent.check_inputs(self.batch.request(alignment_rt_tolerance=0))
        self.assertTrue(any("alignment_rt_tolerance '0' is not a positive number" in item for item in check["blockers"]))
        check = agent.check_inputs(self.batch.request())
        self.assertIn("0.1 min", check["stated_defaults"]["alignment_rt_tolerance"])
        contracts = {}
        for index, (given, expected) in enumerate(((1.5, "1.5"), (None, "0.1"))):
            with self.subTest(given=given):
                contract = self.batch.contract(alignment_rt_tolerance=given,
                                               analysis_root=str(self.batch.root / f"analysis{index}"))
                contracts[given] = contract
                self.assertEqual(given, agent.load_contract(contract)["alignment_rt_tolerance"])
                plan = self.plan(contract)
                self.assertTrue(plan["ready_to_prepare"], (plan["blockers"], plan["remaining_questions"]))
                # Fixed for both polarities either way: the person's value, or the template's.
                self.assertIn("alignment_rt_tolerance", plan["lab_lcms_metabolomics"]["fixed_keys"])
                self.assertEqual([], plan["lab_lcms_metabolomics"]["deviations"])
                lines = method_lines(prepare_run(plan["workflow"])["method_file"])
                self.assertEqual(expected, lines["Retention time tolerance for alignment"])
        for given in (1.5, None):
            with self.subTest(changed=given):
                # A plan's answer cannot move one polarity off the contract's value, nor can a workset's.
                plan = self.plan(contracts[given], alignment_rt_tolerance=0.3)
                self.assertTrue(any("alignment_rt_tolerance is fixed by the contract" in item for item in plan["blockers"]),
                                plan["blockers"])
                workset = {"id": "w", "kind": "user", "answers": {"alignment_rt_tolerance": 0.3}}
                with patch("msdial_app.agent_workflow.get_workset", return_value=workset):
                    plan = build_guided_plan(str(self.batch.folders["Positive"]), {
                        agent.CONTRACT_KEY: contracts[given], "ion_mode": "Positive", "minimum_peak_height": 500}, "w")
                self.assertTrue(any("alignment_rt_tolerance is fixed by the contract" in item for item in plan["blockers"]),
                                plan["blockers"])
                self.assertTrue(self.plan(contracts[given], alignment_rt_tolerance=1.5 if given else 0.1)["ready_to_prepare"])
                plan = self.plan(contracts[given], workflow_overrides={"alignment_rt_tolerance": 0.3})
                self.assertTrue(any("workflow_overrides.alignment_rt_tolerance would replace" in item
                                    for item in plan["blockers"]))
        for value in (0, -1, "abc", True):
            with self.subTest(value=value):
                plan = self.plan(contracts[None], alignment_rt_tolerance=value)
                self.assertTrue(any("alignment_rt_tolerance is fixed by the contract" in item for item in plan["blockers"]),
                                plan["blockers"])


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

    def test_the_header_answers_and_the_alignment_tolerance_reach_the_backend(self) -> None:
        for function in (mcp_server.msdial_lcms_metabolomics_check_inputs, mcp_server.msdial_lcms_metabolomics_write_contract):
            with self.subTest(tool=function.__name__):
                arguments = {"pos_path": "p", "acquisition_type_pos": "SWATH", "acquisition_type_mixed_pos": "as_read",
                             "alignment_rt_tolerance": 1.5}
                if function is mcp_server.msdial_lcms_metabolomics_write_contract:
                    arguments.update(annotation={"preset": "single_msp"}, solvent_type="s", analysis_root="r",
                                     analysis_purpose="x", console_path="c", raw_location="copy", check_digest="d")
                body = self.capture(function, **arguments)["body"]
                self.assertEqual(("SWATH", "", "as_read", "", 1.5), (
                    body["acquisition_type_pos"], body["acquisition_type_neg"], body["acquisition_type_mixed_pos"],
                    body["acquisition_type_mixed_neg"], body["alignment_rt_tolerance"]))

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
