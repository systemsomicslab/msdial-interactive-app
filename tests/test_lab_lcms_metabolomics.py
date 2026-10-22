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
    CLASSES,
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
        self.assertIn("S1_Pos", kept[0], "one polarity keeps its sample names as exported")
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
                         (group["first_block"], group["kept"]["alignment_id"], group["stereo_blocks"]))

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
