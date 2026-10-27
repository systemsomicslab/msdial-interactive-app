"""The in-silico MSP tier, the lipid RT check and the target omics pin (0.5.48).

The author's decisions of 2026-10-25: GLACIER, an in-silico MS/MS library, is a fourth annotation tier below
the LBM2 and the two acquired-MSP tiers; its matches are in-silico evidence, never reference spectra, and it
names nothing from a bare precursor mass. The lipid RT check is written out in full and recorded. Every
campaign unit is a Lipidomics project whatever its repository metadata suggested, and both are recorded.
Every library here is synthetic: a few bytes under a temporary folder, or a location nothing reads.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import __version__
from msdial_app.agent_bridge import create_datamining_handoff
from msdial_app.agent_workflow import build_guided_plan, target_omics_record
from msdial_app.annotation_pipeline import (
    GLACIER_CITATION,
    IN_SILICO_EVIDENCE_TIER,
    IN_SILICO_GENERIC_EVIDENCE_TIER,
    TIERED_LCMS_IN_SILICO_PROFILE_ID,
    TIERED_LCMS_PROFILE_ID,
    in_silico_evidence_tier,
    tiered_lcms_annotation,
)
from msdial_app.lab_lcms_metabolomics import (
    PRESETS,
    THRESHOLDS,
    annotation_summary,
    annotator_settings,
    apply_annotators,
    precursor_plus_1008_census,
    read_annotation,
)
from msdial_app.library_copy_store import library_names
from msdial_app.lipid_rt_check import (
    console_summary,
    method_record,
    read_setting,
    summary_outcome,
    template_setting,
    template_values,
)
from msdial_app.materials_methods import _annotation_sentence, _lipid_rt_check_sentence, generate_publication_report
from msdial_app.run_finalisation import finalise_console_run, lipid_rt_check_outcome
from msdial_app.sharing import library_records
from msdial_app.workflow import (
    DIAGNOSTIC_ANNOTATION_SKIPPED,
    LIBRARY_KIND_CONSOLE_MARKERS,
    LIPID_RT_CHECK_CONSOLE_MARKERS,
    MSP_SUGGESTIONS_CONSOLE_MARKERS,
    _annotator_capability_issues,
    _write_msp_annotator_settings,
    _write_reproduction_files,
    console_capabilities,
    load_parameter_template,
    parse_mdpeak,
    prepare_run,
    validate_workflow,
    with_template_lipid_rt_check,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"
ALL_MARKERS = (*LIBRARY_KIND_CONSOLE_MARKERS, *MSP_SUGGESTIONS_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS)
LIPID_RT_KEYS = (
    "Lipid RT check", "Lipid RT check operating point", "Lipid RT check chromatography",
    "Lipid RT check plausibility profile", "Lipid RT check class-level matches", "Lipid RT check minor duplicates",
    "Lipid RT check secondary-only features",
)
RT_SUMMARY = (
    "Lipid RT check (alignment): 12 annotated, 9 evaluable: 4 Confirmed, 5 NotEvaluable, 0 Inconsistent; "
    "Confirmed tags set 4 (already tagged 0, Misannotation 0, suppressed 0, earlier ones withdrawn 0); "
    "rules 1.0, parameter 1a2b3c4d."
)


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


def write_review(prepared: dict, name: str = "AlignResult-2026_lipidrt_review.tsv", rows: str = "") -> Path:
    """A review list as the Console writes one, dated after the run's method file."""
    review = Path(prepared["run_directory"]) / name
    review.write_text("ID\tName\n" + rows, encoding="utf-8")
    method_time = Path(prepared["method_file"]).stat().st_mtime
    os.utime(review, (method_time + 5, method_time + 5))
    return review


def fake_console(path: Path, markers=ALL_MARKERS) -> Path:
    """A file that carries the Console assembly's marker strings as .NET stores them, and runs nothing."""
    path.write_bytes(b"".join(marker.encode("utf-16-le") for marker in markers))
    return path


class ProfileTests(unittest.TestCase):
    def test_without_an_in_silico_library_the_v1_profile_is_unchanged(self) -> None:
        # The v1 profile as 0.5.47 built it, key for key and in order: a record that names v1 means this.
        common = {
            "msp_file_path": "private.msp", "target_omics": "Metabolomics", "rt_tolerance": 0.5,
            "ms1_tolerance": 0.01, "use_rt_scoring": False, "use_rt_filtering": False,
            "matched_peaks_percentage_cutoff": 0.0,
        }
        expected = {
            "profile_id": "lipid-rule-msp-high-low-v1",
            "selection_semantics": "representative result priority cascade",
            "lbm_annotator": {"lbm_file_path": "lipids.lbm2", "priority": 3},
            "msp_annotators": [
                {**common, "annotator_id": "msp_high_quality", "priority": 2, "ms2_tolerance": 0.05,
                 "weighted_dot_product_cutoff": 0.6, "simple_dot_product_cutoff": 0.6,
                 "reverse_dot_product_cutoff": 0.8, "minimum_spectrum_match": 3,
                 "evidence_tier": "MSP high quality"},
                {**common, "annotator_id": "msp_low_quality", "priority": 1, "ms2_tolerance": 0.25,
                 "weighted_dot_product_cutoff": 0.5, "simple_dot_product_cutoff": 0.5,
                 "reverse_dot_product_cutoff": 0.5, "minimum_spectrum_match": 1,
                 "evidence_tier": "MSP low quality candidate"},
            ],
        }
        for in_silico in ((), (None,), ("",), ("  ",)):
            with self.subTest(in_silico=in_silico):
                profile = tiered_lcms_annotation("lipids.lbm2", "private.msp", None, *in_silico)
                self.assertEqual(json.dumps(expected), json.dumps(profile))
        self.assertEqual(TIERED_LCMS_PROFILE_ID, expected["profile_id"])

    def test_four_tiers_with_their_priorities_kinds_and_omics(self) -> None:
        profile = tiered_lcms_annotation("lipids.lbm2", "private.msp", {"evidence_tier": "x"}, "GLACIER_neg.msp")

        self.assertEqual(TIERED_LCMS_IN_SILICO_PROFILE_ID, profile["profile_id"])
        self.assertEqual(4, profile["lbm_annotator"]["priority"])
        rows = profile["msp_annotators"]
        self.assertEqual(
            [("msp_high_quality", 3, "acquired", "private.msp"), ("msp_low_quality", 2, "acquired", "private.msp"),
             ("msp_in_silico", 1, "predicted", "GLACIER_neg.msp")],
            [(row["annotator_id"], row["priority"], row["library_kind"], row["msp_file_path"]) for row in rows],
        )
        self.assertEqual({"Metabolomics"}, {row["target_omics"] for row in rows})
        in_silico = rows[-1]
        # One definition of the in-silico thresholds (the author's Q3): 0.05 Da, 0.5/0.5/0.5, >= 3, 0 %.
        self.assertEqual(THRESHOLDS["in_silico"], {key: in_silico[key] for key in THRESHOLDS["in_silico"]})
        self.assertEqual(
            (0.01, 0.05, 0.5, 0.5, 0.5, 3, 0.0),
            (in_silico["ms1_tolerance"], in_silico["ms2_tolerance"], in_silico["weighted_dot_product_cutoff"],
             in_silico["simple_dot_product_cutoff"], in_silico["reverse_dot_product_cutoff"],
             in_silico["minimum_spectrum_match"], in_silico["matched_peaks_percentage_cutoff"]),
        )
        self.assertFalse(in_silico["use_rt_scoring"] or in_silico["use_rt_filtering"])
        self.assertIs(False, in_silico["suggestions"])
        self.assertEqual(IN_SILICO_EVIDENCE_TIER, in_silico["evidence_tier"])
        self.assertEqual("In-silico MSP (GLACIER)", IN_SILICO_EVIDENCE_TIER)
        # Only a file its name says is GLACIER is labelled, and so cited, as GLACIER; another in-silico library
        # is searched the same way under the generic label.
        other = tiered_lcms_annotation("lipids.lbm2", "private.msp", None, "D:\\libs\\glacier\\CFMID_neg.msp")
        self.assertEqual(IN_SILICO_GENERIC_EVIDENCE_TIER, other["msp_annotators"][-1]["evidence_tier"])
        self.assertEqual(in_silico_evidence_tier("/libs/Glacier_NEG_allCE.msp"), IN_SILICO_EVIDENCE_TIER)
        self.assertTrue(all("suggestions" not in row for row in rows[:2]))
        # The laboratory preset that mirrors the campaign's tiers searches its in-silico MSP the same way.
        self.assertEqual(["in_silico"], [entry["thresholds"] for entry in PRESETS["campaign"] if entry["id"] == "in_silico"])

    def test_the_annotator_table_writes_kind_and_suggestions_and_a_template_reads_them_back(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            msp, in_silico = root / "acquired.msp", root / "insilico_neg.msp"
            for path in (msp, in_silico):
                path.write_text("NAME: A\n", encoding="ascii")
            v1 = {"project_type": "lcms", "msp_annotators": tiered_lcms_annotation("l.lbm2", str(msp))["msp_annotators"]}
            v1_path = _write_msp_annotator_settings(root, v1)
            with v1_path.open(encoding="ascii", newline="") as handle:
                self.assertFalse({"library_kind", "suggestions"} & set(next(csv.reader(handle, delimiter="\t"))))

            state = {"project_type": "lcms", "msp_annotators": tiered_lcms_annotation(
                "l.lbm2", str(msp), None, str(in_silico))["msp_annotators"]}
            path = _write_msp_annotator_settings(root, state)
            with path.open(encoding="ascii", newline="") as handle:
                rows = list(csv.DictReader(handle, delimiter="\t"))
            self.assertEqual(
                [("3", "acquired", "True", "Metabolomics"), ("2", "acquired", "True", "Metabolomics"),
                 ("1", "predicted", "False", "Metabolomics")],
                [(row["priority"], row["library_kind"], row["suggestions"], row["target_omics"]) for row in rows],
            )
            template = root / "method.txt"
            template.write_text(f"MSP annotator settings file path: {path.name}\n", encoding="ascii")
            loaded = load_parameter_template(template)["msp_annotators"]
            self.assertEqual(["acquired", "acquired", "predicted"], [row["library_kind"] for row in loaded])
            self.assertEqual([True, True, False], [row["suggestions"] for row in loaded])


class CapabilityTests(unittest.TestCase):
    @patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console"))
    def test_the_console_markers_name_library_kind_suggestions_and_the_lipid_rt_check(self, _run) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            console = fake_console(Path(temporary) / "MSDIALCUI.exe")
            found = console_capabilities(str(console))["capabilities"]
            self.assertTrue({"library_kind", "msp_suggestions", "lipid_rt_check"} <= set(found))
            # The lipid RT check needs both of its markers: the key the parser reads and the review-list line.
            fake_console(console, (LIPID_RT_CHECK_CONSOLE_MARKERS[0],))
            self.assertNotIn("lipid_rt_check", console_capabilities(str(console))["capabilities"])
            fake_console(console, ())
            self.assertFalse({"library_kind", "msp_suggestions", "lipid_rt_check"}
                             & set(console_capabilities(str(console))["capabilities"]))


class InSilicoPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.data = self.root / "data"
        self.data.mkdir()
        (self.data / "sample.mzML").write_text("", encoding="ascii")
        self.libraries = self.root / "libraries"
        self.libraries.mkdir()
        self.console = fake_console(self.root / "MSDIALCUI.exe")
        self.msp = self.libraries / "synthetic-acquired-neg.msp"
        self.msp.write_text("NAME: A\n", encoding="ascii")
        self.in_silico = self.libraries / "Synthetic-GLACIER_neg_allCE.msp"
        self.in_silico.write_text("NAME: B\nPRECURSORMZ: 100\n", encoding="ascii")
        self.lbm = self.libraries / "synthetic-lab-lipids.lbm2"
        self.lbm.write_bytes(b"synthetic laboratory LBM2 library")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def answers(self, **libraries) -> dict:
        return {
            "project_type": "lcms", "ion_mode": "Negative", "target_omics": "Metabolomics",
            "target_omics_pin": "Lipidomics",
            "parameter_strategy": "default", "execute_rt_correction": False,
            "library_strategy": "tiered_lipid_msp",
            "libraries": {
                "lbm_path": str(self.lbm), "msp_paths": [str(self.msp)], "msp_version": "VS-test",
                "in_silico_msp_paths": [str(self.in_silico)], "in_silico_msp_version": "synthetic-1",
                "in_silico_msp_source": "https://github.com/coleygroup/ms-pred",
                "in_silico_msp_preprocessing": ["precursor_plus_1008_peak_removed"],
                **libraries,
            },
            "lipid_rt_check": {"mode": "auto", "operating_point": "HighPrecision", "chromatography": "Auto"},
            "run_qa": False, "generate_materials_methods": False, "class_assignment_confirmed": True,
            "console_path": str(self.console), "template_path": str(TEMPLATE),
            "output_root": str(self.root / "out"),
        }

    def plan(self, answers: dict) -> dict:
        with patch("msdial_app.agent_workflow.catalog_status", side_effect=AssertionError("catalog consulted")), \
                patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            return build_guided_plan(str(self.data), answers)

    def prepare(self, workflow: dict) -> dict:
        with patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            return prepare_run(workflow)

    def test_the_four_tiers_reach_the_annotator_table_and_every_record_by_kind(self) -> None:
        plan = self.plan(self.answers())
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        workflow = plan["workflow"]
        self.assertEqual(TIERED_LCMS_IN_SILICO_PROFILE_ID, workflow["annotation_pipeline_profile"])
        self.assertEqual(4, workflow["lbm_annotator"]["priority"])
        self.assertEqual([3, 2, 1], [row["priority"] for row in workflow["msp_annotators"]])
        provenance = workflow["library_provenance"]
        self.assertEqual(["acquired", "predicted"], [entry.get("library_kind") for entry in provenance[1:]])
        self.assertEqual(
            {"name": self.in_silico.name, "version": "synthetic-1", "license": "institutional/private",
             "library_kind": "predicted", "preprocessing": ["precursor_plus_1008_peak_removed"]},
            {key: provenance[2][key] for key in ("name", "version", "license", "library_kind", "preprocessing")},
        )

        prepared = self.prepare(workflow)
        with Path(prepared["method_file"]).parent.joinpath("msp_annotator_settings.tsv").open(
                encoding="ascii", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(["acquired", "acquired", "predicted"], [row["library_kind"] for row in rows])
        self.assertEqual(["True", "True", "False"], [row["suggestions"] for row in rows])
        self.assertTrue(Path(rows[-1]["msp_file_path"]).samefile(self.in_silico))
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(TIERED_LCMS_IN_SILICO_PROFILE_ID, manifest["annotation_pipeline_profile"])
        kinds = {Path(item["path"]).name: item.get("library_kind") for item in manifest["libraries"]}
        self.assertEqual({self.msp.name: "acquired", self.in_silico.name: "predicted", self.lbm.name: None}, kinds)
        identities = {item.name: item.identity for item in library_records(workflow)}
        self.assertEqual("predicted", identities[self.in_silico.name]["library_kind"])
        self.assertEqual("acquired", identities[self.msp.name]["library_kind"])
        self.assertNotIn("library_kind", identities[self.lbm.name])
        self.assertTrue(identities[self.in_silico.name]["private"])
        self.assertEqual(hashlib.sha256(self.in_silico.read_bytes()).hexdigest(),
                         identities[self.in_silico.name]["sha256"])
        stored = library_names(item.identity for item in library_records(workflow))
        self.assertEqual("predicted", next(item for item in stored if item["name"] == self.in_silico.name)["library_kind"])

    def test_a_console_without_library_kind_or_the_suggestions_switch_blocks_the_plan(self) -> None:
        for markers, blocked in (
            ((), ("does not read library_kind", "no per-annotator suggestions switch", "no lipid retention-time check")),
            ((*LIBRARY_KIND_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS), ("no per-annotator suggestions switch",)),
            ((*MSP_SUGGESTIONS_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS), ("does not read library_kind",)),
        ):
            with self.subTest(markers=markers):
                fake_console(self.console, markers)
                plan = self.plan(self.answers())
                self.assertFalse(plan["ready_to_prepare"])
                for message in blocked:
                    self.assertTrue(any(message in item for item in plan["blockers"]), (message, plan["blockers"]))
        # Without the in-silico tier and the lipid RT check, the same old Console plans as before.
        fake_console(self.console, ())
        answers = self.answers()
        del answers["libraries"]["in_silico_msp_paths"], answers["libraries"]["in_silico_msp_preprocessing"]
        del answers["lipid_rt_check"]
        plan = self.plan(answers)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        self.assertEqual(TIERED_LCMS_PROFILE_ID, plan["workflow"]["annotation_pipeline_profile"])

    def test_what_the_in_silico_answer_refuses(self) -> None:
        other = self.libraries / "Synthetic-GLACIER_pos_allCE.msp"
        other.write_text("NAME: C\n", encoding="ascii")
        text = self.libraries / "insilico_neg.txt"
        text.write_text("x", encoding="ascii")
        cases = (
            ({"in_silico_msp_paths": [str(self.msp)]}, "which libraries.msp_paths names as acquired"),
            ({"in_silico_msp_paths": [str(text)]}, "reads an .msp library; insilico_neg.txt is not one"),
            ({"in_silico_msp_paths": [str(other)]}, "is named for positive ion mode and this run is negative"),
            ({"in_silico_msp_paths": [str(self.in_silico), str(other)]}, "searches one in-silico MSP"),
            ({"in_silico_msp_preprocessing": ["deisotoped"]}, "in_silico_msp_preprocessing names deisotoped"),
        )
        for libraries, message in cases:
            with self.subTest(message=message):
                plan = self.plan(self.answers(**libraries))
                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(any(message in item for item in plan["blockers"]), plan["blockers"])
        answers = self.answers()
        answers["library_strategy"] = "existing"
        plan = self.plan(answers)
        self.assertTrue(any("searched only by library_strategy 'tiered_lipid_msp'" in item for item in plan["blockers"]))

    def test_the_target_omics_pin_is_applied_and_recorded_beside_the_seed(self) -> None:
        plan = self.plan(self.answers())
        workflow = plan["workflow"]
        self.assertEqual("Lipidomics", workflow["target_omics"])
        self.assertEqual(
            {"repository_seed": None, "answered": "Metabolomics", "pinned": "Lipidomics", "applied": "Lipidomics"},
            workflow["target_omics_record"],
        )
        prepared = self.prepare(workflow)
        self.assertEqual("Lipidomics", method_lines(prepared["method_file"])["Target omics"])
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertEqual(workflow["target_omics_record"], manifest["target_omics"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        self.assertEqual(workflow["target_omics_record"], settings["target_omics_record"])
        # The seed is the repository metadata's suggestion where the run reads one.
        self.assertEqual(
            {"repository_seed": "Metabolomics", "answered": "Metabolomics", "pinned": "Lipidomics",
             "applied": "Lipidomics"},
            target_omics_record({"target_omics": "Metabolomics", "target_omics_pin": "lipidomics"},
                                {"repository_metadata": {"target_omics": "Metabolomics"}, "target_omics": "Lipidomics"}),
        )
        plan = self.plan({**self.answers(), "target_omics_pin": "Proteomics"})
        self.assertTrue(any("target_omics_pin 'Proteomics'" in item for item in plan["blockers"]))

    def test_the_lipid_rt_check_is_written_in_full_and_recorded(self) -> None:
        plan = self.plan(self.answers())
        workflow = plan["workflow"]
        self.assertEqual(
            {"mode": "Auto", "operating_point": "HighPrecision", "chromatography": "Auto",
             "plausibility_profile": "None", "class_level_matches": "Corroborated", "minor_duplicates": "Allow",
             "secondary_only_features": "Evaluate"},
            workflow["lipid_rt_check"],
        )
        prepared = self.prepare(workflow)
        lines = method_lines(prepared["method_file"])
        self.assertEqual(
            ["Auto", "HighPrecision", "Auto", "None", "Corroborated", "Allow", "Evaluate"],
            [lines[key] for key in (
                "Lipid RT check", "Lipid RT check operating point", "Lipid RT check chromatography",
                "Lipid RT check plausibility profile", "Lipid RT check class-level matches",
                "Lipid RT check minor duplicates", "Lipid RT check secondary-only features",
            )],
        )
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        for record in (settings["lipid_rt_check_method"], manifest["lipid_rt_check"]):
            self.assertTrue(record["written_to_method_file"])
            self.assertTrue(record["console_capability"])
            self.assertEqual(workflow["lipid_rt_check"], record["setting"])

        # The run did not write a review list: the check did not run.
        line = ("Lipid RT check (alignment): 12 annotated, 9 evaluable: 4 Confirmed, 5 NotEvaluable, 0 Inconsistent; "
                "Confirmed tags set 4 (already tagged 0, Misannotation 0, suppressed 0, earlier ones withdrawn 0); "
                "rules 1.0, parameter 1a2b3c4d.")
        outcome = lipid_rt_check_outcome(prepared, ["[12:00] other", line])
        self.assertFalse(outcome["ran"])
        self.assertEqual([], outcome["review_files"])
        self.assertEqual(("1.0", "1a2b3c4d"), (outcome["rule_version"], outcome["parameter_hash"]))
        self.assertTrue(outcome["method"]["written_to_method_file"])
        # A review list written by this run (after its method file) is recorded by name, sha256 and rows.
        review = Path(prepared["run_directory"]) / "AlignResult-2026_lipidrt_review.tsv"
        review.write_text("ID\tName\tOutcome\n1\tPC 34:1\tInconsistent\n2\tPE 36:2\tNotEvaluable\n", encoding="utf-8")
        method_time = Path(prepared["method_file"]).stat().st_mtime
        os.utime(review, (method_time + 5, method_time + 5))
        outcome = lipid_rt_check_outcome(prepared, [line])
        self.assertTrue(outcome["ran"])
        self.assertEqual(
            [{"name": review.name, "sha256": hashlib.sha256(review.read_bytes()).hexdigest(), "rows": 2}],
            outcome["review_files"],
        )
        # One older than the method file is an earlier run's.
        os.utime(review, (method_time - 60, method_time - 60))
        self.assertFalse(lipid_rt_check_outcome(prepared, [line])["ran"])

    def test_the_methods_name_both_kinds_of_annotator_the_evidence_column_and_the_lipid_rt_check(self) -> None:
        plan = self.plan(self.answers())
        prepared = self.prepare(plan["workflow"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        digest = hashlib.sha256(self.in_silico.read_bytes()).hexdigest()

        report = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )
        text = report["methods_text"]
        self.assertIn("2 MSP annotators searching acquired spectra", text)
        self.assertIn("1 MSP annotator searching in-silico (predicted) spectra", text)
        self.assertIn("one LBM annotator", text)
        self.assertIn("evidence source InSilico", text)
        self.assertIn("opt_global_evidence_source", text)
        self.assertIn("\"Evidence source\" column", text)
        self.assertIn("carries no retention time", text)
        self.assertIn(GLACIER_CITATION, text)
        self.assertIn("https://github.com/coleygroup/ms-pred", text)
        self.assertIn("[M+H]+ and [M-H]- precursors only, at collision energies 10, 20 and 40", text)
        self.assertIn(f"For {self.in_silico.name}, the peak at the precursor m/z + 1.008", text)
        self.assertIn(f"{self.in_silico.name} (SHA-256 {digest})", text)
        self.assertIn("The MS-DIAL target omics was set to Lipidomics as a campaign setting", text)
        self.assertIn("Every MSP annotator was scored as Metabolomics", text)
        self.assertIn("lipid retention-time order check", text)
        self.assertIn("operating point HighPrecision", text)
        self.assertIn("No review list was written: the check did not run, or failed and withdrew", text)
        self.assertNotIn(str(self.libraries), text)
        # The in-silico library's record names its public project as the source, yet the build is private: it
        # was not downloaded from there, and it is identified by name and digest only.
        self.assertNotIn("Downloaded libraries were", text)

        review = Path(prepared["run_directory"]) / "AlignResult-2026_lipidrt_review.tsv"
        review.write_text("ID\tName\n", encoding="utf-8")
        method_time = Path(prepared["method_file"]).stat().st_mtime
        os.utime(review, (method_time + 5, method_time + 5))
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn(f"{review.name} (SHA-256 {hashlib.sha256(review.read_bytes()).hexdigest()}; 0 rows)", text)
        self.assertNotIn("did not run", text)
        # Without the Console's summary nothing says the reversed-phase gate was passed, so an empty list is not
        # read as an order that held.
        self.assertIn("an empty list means the check listed nothing for review", text)
        self.assertNotIn("found nothing to review", text)

    def test_a_run_that_neither_set_nor_ran_the_lipid_rt_check_claims_nothing_of_it(self) -> None:
        # A Lipidomics project with no lipid_rt_check answer: nothing here knows what the Console does by
        # default, and a Console from before MsdialWorkbench#832 has no check at all.
        answers = self.answers()
        del answers["lipid_rt_check"]
        prepared = self.prepare(self.plan(answers)["workflow"])
        self.assertFalse(set(LIPID_RT_KEYS) & set(method_lines(prepared["method_file"])))
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertIsNone(manifest["lipid_rt_check"]["setting"])
        self.assertFalse(manifest["lipid_rt_check"]["written_to_method_file"])
        self.assertNotIn("Lipidomics project", manifest["lipid_rt_check"]["reason"])
        self.assertNotIn("defaults apply", method_record(None, None)["reason"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))

        report = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )
        self.assertNotIn("lipid retention-time order check", report["methods_text"])
        self.assertNotIn("Lipid RT check", Path(report["supplementary_table"]).read_text(encoding="utf-8"))

        # Not set, yet a review list shows the Console ran it: at its own defaults, whatever they are.
        write_review(prepared)
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn("was not set by this run and ran at the MS-DIAL Console's own defaults", text)
        self.assertNotIn("which run it", text)

    def test_a_setting_asked_for_but_not_written_says_so(self) -> None:
        prepared = self.prepare(self.plan(self.answers())["workflow"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        settings["lipid_rt_check_method"] = method_record(settings["lipid_rt_check"], None)
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn(
            "was asked for, but no setting of it was written to the method file (no Console to probe, so no key "
            "was written). No review list was written: the check did not run, or failed and withdrew what it had "
            "written.",
            text,
        )
        self.assertNotIn("defaults", text[text.index("lipid retention-time order check"):])

    def test_a_templates_lipid_rt_check_lines_are_the_setting_it_writes_and_records(self) -> None:
        self.assertEqual(
            {"lipid_rt_check": read_setting({"mode": "False", "operating_point": "Balanced"})[0]},
            template_setting({"lipid rt check": "False", "lipid rt check operating point": "balanced"}),
        )
        self.assertEqual({}, template_setting({"minimum peak height": "1000"}))
        template = self.root / "console-template.txt"
        base = TEMPLATE.read_text(encoding="utf-8-sig")
        template.write_text(
            base + "\nLipid RT check: False\nLipid RT check operating point: balanced\n", encoding="utf-8"
        )
        answers = {**self.answers(), "template_path": str(template)}
        del answers["lipid_rt_check"]
        plan = self.plan(answers)
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])
        workflow = plan["workflow"]
        self.assertEqual(("False", "Balanced", "Auto"), (
            workflow["lipid_rt_check"]["mode"], workflow["lipid_rt_check"]["operating_point"],
            workflow["lipid_rt_check"]["chromatography"],
        ))
        prepared = self.prepare(workflow)
        text = Path(prepared["method_file"]).read_text(encoding="utf-8")
        for key in LIPID_RT_KEYS:
            self.assertEqual(1, sum(1 for line in text.splitlines() if line.split(":")[0].strip() == key), key)
        lines = method_lines(prepared["method_file"])
        self.assertEqual(("False", "Balanced"), (lines["Lipid RT check"], lines["Lipid RT check operating point"]))
        manifest = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))
        self.assertTrue(manifest["lipid_rt_check"]["written_to_method_file"])
        self.assertEqual(workflow["lipid_rt_check"], manifest["lipid_rt_check"]["setting"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn("mode False; operating point Balanced", text)

        # An answer replaces the template's lines.
        prepared = self.prepare(self.plan({**answers, "lipid_rt_check": {"mode": "auto"}})["workflow"])
        lines = method_lines(prepared["method_file"])
        self.assertEqual(("Auto", "HighPrecision"), (lines["Lipid RT check"], lines["Lipid RT check operating point"]))

        # A state that never read its template's lines, as the web page posts one (it has no control for the
        # check), reads them at the run: written from the setting and recorded, never dropped for the Console's
        # default, nor copied through.
        bare = dict(workflow)
        del bare["lipid_rt_check"]
        prepared = self.prepare(bare)
        lines = method_lines(prepared["method_file"])
        self.assertEqual(("False", "Balanced"), (lines["Lipid RT check"], lines["Lipid RT check operating point"]))
        recorded = json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))["lipid_rt_check"]
        self.assertEqual(workflow["lipid_rt_check"], recorded["setting"])
        self.assertTrue(recorded["written_to_method_file"])
        # A state whose setting is known to be none (read, and the template has none) writes no key.
        plain = {**dict(workflow), "template_path": str(TEMPLATE)}
        del plain["lipid_rt_check"]
        prepared = self.prepare(plain)
        self.assertFalse(set(LIPID_RT_KEYS) & set(method_lines(prepared["method_file"])))
        self.assertIsNone(json.loads(Path(prepared["manifest"]).read_text(encoding="utf-8"))["lipid_rt_check"]["setting"])

        # On a Console without the check the template's lines are refused, as the answer is.
        fake_console(self.console, (*LIBRARY_KIND_CONSOLE_MARKERS, *MSP_SUGGESTIONS_CONSOLE_MARKERS))
        plan = self.plan(answers)
        self.assertTrue(any("the parameter template's Lipid RT check lines" in item for item in plan["blockers"]),
                        plan["blockers"])
        # A template value the Console would refuse is refused, never left to run the default unrecorded.
        fake_console(self.console)
        template.write_text(base + "\nLipid RT check operating point: Fast\n", encoding="utf-8")
        plan = self.plan(answers)
        self.assertTrue(
            any("The parameter template's 'Lipid RT check operating point' is 'Fast'" in item
                for item in plan["blockers"]),
            plan["blockers"],
        )
        plan = self.plan({**answers, "lipid_rt_check": "Auto"})
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])

    def test_the_methods_say_when_an_in_silico_annotator_used_retention_time(self) -> None:
        prepared = self.prepare(self.plan(self.answers())["workflow"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        settings["msp_annotators"][-1]["use_rt_scoring"] = True
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn("every in-silico annotator was also searched with retention time", text)
        self.assertNotIn("carries no retention time", text)
        # The ranking holds whatever the priority: MS-DIAL compares Rank before Priority.
        self.assertIn("an in-silico name is reported only where neither is accepted", text)
        settings["msp_annotators"].append(
            {**settings["msp_annotators"][-1], "annotator_id": "second", "use_rt_scoring": "false"}
        )
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn("1 of the 2 in-silico annotators were also searched with retention time", text)
        # The annotator table hands the Console the same reading of that row: "false" is False there too.
        written = self.root / "table"
        written.mkdir()
        with _write_msp_annotator_settings(written, dict(settings)).open(encoding="ascii", newline="") as handle:
            table = {row["annotator_id"]: row for row in csv.DictReader(handle, delimiter="\t")}
        self.assertEqual(("True", "False"), (
            table["msp_in_silico"]["use_retention_information_for_scoring"],
            table["second"]["use_retention_information_for_scoring"],
        ))

    def test_the_peak_removal_is_checked_against_the_run_and_the_file(self) -> None:
        # Stated for a positive-mode run, whose file has no such artefact.
        positive = self.libraries / "Synthetic-GLACIER_pos_allCE.msp"
        positive.write_text("NAME: C\nPRECURSORMZ: 200\n", encoding="ascii")
        plan = self.plan({**self.answers(in_silico_msp_paths=[str(positive)]), "ion_mode": "Positive"})
        self.assertTrue(any("for a positive-mode run" in item for item in plan["blockers"]), plan["blockers"])
        plan = self.plan({**self.answers(in_silico_msp_paths=[str(positive)], in_silico_msp_preprocessing=[]),
                          "ion_mode": "Positive"})
        self.assertFalse(any("in_silico" in item for item in plan["blockers"]), plan["blockers"])

        # A file that still carries the peak at a record's precursor m/z + 1.008 has not had it removed.
        self.in_silico.write_text(
            "NAME: B\nPRECURSORMZ: 300.1\nNum Peaks: 2\n150.05\t10\n301.108\t5\n\n"
            "NAME: C\nPRECURSORMZ: 250\nNum Peaks: 3\n120 10; 251.007 3\n249.992 1\n\n"
            "NAME: D\nPRECURSORMZ: 400\nNum Peaks: 3\n401.0034 4\n398.992 2\n200 10\n",
            encoding="ascii",
        )
        self.assertEqual({"records": 3, "with_precursor_plus_1008_peak": 2}, precursor_plus_1008_census(self.in_silico))
        plan = self.plan(self.answers())
        self.assertTrue(
            any("2 of the 3 records in Synthetic-GLACIER_neg_allCE.msp still carry a peak" in item
                for item in plan["blockers"]),
            plan["blockers"],
        )
        # Without the step the same file is still refused: the peak must be removed (Q6), not merely left
        # unclaimed. A positive-mode run's file is not censused for it.
        plan = self.plan(self.answers(in_silico_msp_preprocessing=[]))
        self.assertEqual(
            ["2 of the 3 records in Synthetic-GLACIER_neg_allCE.msp carry a peak at the precursor m/z + 1.008, the "
             "negative-mode records' artefact, which is removed before use. Give the file with the peak removed, "
             "and name precursor_plus_1008_peak_removed in libraries.in_silico_msp_preprocessing."],
            [item for item in plan["blockers"] if "1.008" in item],
        )
        positive.write_bytes(self.in_silico.read_bytes())
        plan = self.plan({**self.answers(in_silico_msp_paths=[str(positive)], in_silico_msp_preprocessing=[]),
                          "ion_mode": "Positive"})
        self.assertFalse(any("1.008" in item for item in plan["blockers"]), plan["blockers"])
        # A file that cannot be read to census is a blocker, with the step or without it, never an exception.
        denied = PermissionError(13, "Permission denied")
        with patch("msdial_app.lab_lcms_metabolomics.precursor_plus_1008_census", side_effect=denied):
            declared = self.plan(self.answers())
            undeclared = self.plan(self.answers(in_silico_msp_preprocessing=[]))
        self.assertIn(
            "libraries.in_silico_msp_preprocessing names precursor_plus_1008_peak_removed, but "
            "Synthetic-GLACIER_neg_allCE.msp cannot be read (Permission denied) to check it for the peak at the "
            "precursor m/z + 1.008. Plan again once it can.",
            declared["blockers"],
        )
        self.assertIn(
            "This negative-mode run's Synthetic-GLACIER_neg_allCE.msp cannot be read (Permission denied) to check "
            "it for the peak at the precursor m/z + 1.008. Plan again once it can.",
            undeclared["blockers"],
        )
        # With the peak removed it plans. An isotope-like +1.003 peak and the H losses below the precursor are
        # not the artefact.
        self.in_silico.write_text(
            "NAME: B\nPRECURSORMZ: 300.1\nNum Peaks: 1\n150.05\t10\n\n"
            "NAME: D\nPRECURSORMZ: 400\nNum Peaks: 3\n401.0034 4\n398.992 2\n200 10\n",
            encoding="ascii",
        )
        self.assertEqual({"records": 2, "with_precursor_plus_1008_peak": 0}, precursor_plus_1008_census(self.in_silico))
        plan = self.plan(self.answers())
        self.assertTrue(plan["ready_to_prepare"], plan["blockers"])

    def test_what_else_the_in_silico_and_lipid_rt_answers_refuse(self) -> None:
        empty = self.libraries / "Empty-InSilico_neg.msp"
        empty.write_bytes(b"")
        without_paths = self.answers()
        del without_paths["libraries"]["in_silico_msp_paths"]
        for answers, message in (
            (self.answers(in_silico_msp_paths=[str(empty)], in_silico_msp_preprocessing=[]),
             "libraries.in_silico_msp_paths: Empty-InSilico_neg.msp is empty."),
            (without_paths, "libraries.in_silico_msp_preprocessing is given without libraries.in_silico_msp_paths."),
            ({**self.answers(), "project_type": "gcms"}, "a GC-MS run has no lipid RT check"),
        ):
            with self.subTest(message=message):
                plan = self.plan(answers)
                self.assertFalse(plan["ready_to_prepare"])
                self.assertTrue(any(message in item for item in plan["blockers"]), plan["blockers"])
        answers = self.answers()
        answers["library_strategy"] = "existing"
        plan = self.plan(answers)
        self.assertTrue(
            any("Use 'tiered_lipid_msp' for the GLACIER tier" in item and "without the GLACIER evidence tier" in item
                for item in plan["blockers"]),
            plan["blockers"],
        )

    def test_a_peak_count_diagnostic_is_not_refused_for_what_it_does_not_load(self) -> None:
        fake_console(self.console, ())
        state = {
            "project_type": "lcms", "console_path": str(self.console),
            "msp_annotators": [{"annotator_id": "msp_in_silico", "msp_file_path": str(self.in_silico),
                                "library_kind": "predicted", "suggestions": False, "target_omics": "Metabolomics"}],
            "lipid_rt_check": read_setting("Auto")[0],
        }
        with patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            self.assertEqual(3, len(_annotator_capability_issues(state, "lcms")))
            state["diagnostic_annotation"] = {"status": DIAGNOSTIC_ANNOTATION_SKIPPED}
            self.assertEqual([], _annotator_capability_issues(state, "lcms"))

    def test_the_supplementary_table_names_each_librarys_kind(self) -> None:
        prepared = self.prepare(self.plan(self.answers())["workflow"])
        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        report = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )
        with Path(report["supplementary_table"]).open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        identity = {
            row["Record"]: row["Value"] for row in rows
            if row["Section"] == "Library identity" and row["Parameter"] == "library_kind"
        }
        self.assertEqual({self.msp.name: "acquired", self.in_silico.name: "predicted"}, identity)

    def test_the_finalised_run_records_the_check_and_the_methods_read_its_rule_version(self) -> None:
        prepared = self.prepare(self.plan(self.answers())["workflow"])
        unit_manifest = self.root / "unit" / "provenance" / "run-manifest.json"
        unit_manifest.parent.mkdir(parents=True)
        unit_manifest.write_text(json.dumps({
            "schema": "msdial-public-reanalysis-run.v1", "output_directory": prepared["run_directory"],
        }), encoding="utf-8")
        review = write_review(prepared, rows="1\tPC 34:1\n")
        logged: list[str] = []

        record = finalise_console_run(
            "run1", {**prepared, "repository_run_manifest": str(unit_manifest)}, {}, 0, {}, logged.append,
            console_lines=["[12:00] other", RT_SUMMARY],
        )

        self.assertEqual(("1.0", "1a2b3c4d", True), (
            record["lipid_rt_check"]["rule_version"], record["lipid_rt_check"]["parameter_hash"],
            record["lipid_rt_check"]["ran"],
        ))
        written = json.loads(unit_manifest.read_text(encoding="utf-8"))["console_run_finalisation"]["lipid_rt_check"]
        self.assertEqual("1.0", written["rule_version"])
        self.assertEqual([{"name": review.name, "sha256": hashlib.sha256(review.read_bytes()).hexdigest(), "rows": 1}],
                         written["review_files"])
        self.assertTrue(any(line.startswith("Lipid RT check: review list") and "rules 1.0" in line for line in logged))

        settings = json.loads(Path(prepared["settings_file"]).read_text(encoding="utf-8"))
        settings["repository_run_manifest"] = str(unit_manifest)
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn("The lipid retention-time order check of MS-DIAL (rules 1.0), which tags", text)

    def test_the_laboratory_campaign_presets_in_silico_slot_names_nothing_from_a_precursor_alone(self) -> None:
        self.assertIs(False, next(entry for entry in PRESETS["campaign"] if entry["id"] == "in_silico")["suggestions"])
        read = read_annotation(
            {"preset": "campaign",
             "libraries": {"lbm": str(self.lbm), "msp": str(self.msp), "in_silico": str(self.in_silico)}},
            ["Negative"],
        )
        self.assertEqual([], read["blockers"])
        self.assertEqual([None, True, True, False], [item["suggestions"] for item in annotation_summary(read["entries"])])
        state: dict = {"project_type": "lcms"}
        apply_annotators(state, annotator_settings(read["entries"], "Negative")["annotators"])
        rows = state["msp_annotators"]
        self.assertEqual(["acquired", "acquired", "predicted"], [row["library_kind"] for row in rows])
        self.assertEqual([None, None, False], [row.get("suggestions") for row in rows])
        self.assertEqual(THRESHOLDS["in_silico"], {key: rows[-1][key] for key in THRESHOLDS["in_silico"]})
        path = _write_msp_annotator_settings(self.root, state)
        with path.open(encoding="ascii", newline="") as handle:
            self.assertEqual(["True", "True", "False"],
                             [row["suggestions"] for row in csv.DictReader(handle, delimiter="\t")])
        # On a Console without the per-annotator switch the run is refused, as the campaign's tier is.
        fake_console(self.console, (*LIBRARY_KIND_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS))
        state["console_path"] = str(self.console)
        with patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            issues = _annotator_capability_issues(state, "lcms")
        self.assertTrue(any("no per-annotator suggestions switch" in issue["message"] for issue in issues), issues)

    def test_the_data_mining_handoff_names_each_tier_by_library_name_and_sha256(self) -> None:
        plan = self.plan(self.answers())
        prepared = self.prepare(plan["workflow"])
        handoff = create_datamining_handoff(run_directory=prepared["run_directory"], write_file=False)
        annotation = handoff["annotation"]
        self.assertEqual(TIERED_LCMS_IN_SILICO_PROFILE_ID, annotation["pipeline_profile"])
        self.assertEqual(
            [("LbmDB: synthetic-lab-lipids", 4, "lbm", None), ("msp_high_quality", 3, "msp", "acquired"),
             ("msp_low_quality", 2, "msp", "acquired"), ("msp_in_silico", 1, "msp", "predicted")],
            [(tier["annotator_id"], tier["priority"], tier["role"], tier["library_kind"]) for tier in annotation["tiers"]],
        )
        self.assertEqual(
            (self.in_silico.name, hashlib.sha256(self.in_silico.read_bytes()).hexdigest(), IN_SILICO_EVIDENCE_TIER),
            (annotation["tiers"][-1]["library_name"], annotation["tiers"][-1]["sha256"],
             annotation["tiers"][-1]["evidence_tier"]),
        )
        self.assertEqual("Lipidomics", annotation["target_omics"]["applied"])
        self.assertIn("not an identification", annotation["note"])
        rendered = json.dumps(annotation)
        for location in (str(self.libraries), str(self.libraries).replace("\\", "\\\\"), self.libraries.as_posix()):
            self.assertNotIn(location, rendered)


class SharedArtifactTests(unittest.TestCase):
    """No bundle, report or handoff of a repository unit names where the in-silico library is kept."""

    def test_the_in_silico_library_location_reaches_no_shared_artifact(self) -> None:
        from test_shared_paths import SHARED_FILES, _texts, build_unit, publish

        location = "E:\\insilico libs\\GLACIER\\Synthetic-GLACIER_pos_allCE.msp"
        encodings = ("E:\\insilico libs", "E:\\\\insilico libs", "E:/insilico libs", "insilico%20libs", "E%3A%5Cinsilico")
        with tempfile.TemporaryDirectory() as temporary:
            unit = build_unit(Path(temporary))
            state, output = unit["state"], unit["output"]
            state["annotation_pipeline_profile"] = TIERED_LCMS_IN_SILICO_PROFILE_ID
            state["msp_annotators"].append({
                "annotator_id": "msp_in_silico", "msp_file_path": location, "priority": 1,
                "library_kind": "predicted", "suggestions": False, "target_omics": "Metabolomics",
                "evidence_tier": IN_SILICO_EVIDENCE_TIER,
            })
            state["library_provenance"].append({
                "path": location, "name": "Synthetic-GLACIER_pos_allCE.msp", "version": "synthetic-1",
                "source": "https://github.com/coleygroup/ms-pred", "doi": "", "license": "institutional/private",
                "library_kind": "predicted",
            })
            recorded = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
            recorded["libraries"].append({
                "name": "Synthetic-GLACIER_pos_allCE.msp", "filename": "Synthetic-GLACIER_pos_allCE.msp",
                "role": "msp", "library_kind": "predicted", "sha256": "ef" * 32, "bytes": 2100000000,
                "size_bytes": 2100000000, "private": True, "distribution": "private",
            })
            (output / "run-manifest.json").write_text(json.dumps(recorded, ensure_ascii=False, indent=2), encoding="utf-8")
            (output / "msp_annotator_settings.tsv").write_text(
                "annotator_id\tmsp_file_path\tpriority\tlibrary_kind\tsuggestions\n"
                f"msp_high_quality\t{state['msp_annotators'][0]['msp_file_path']}\t2\tacquired\tTrue\n"
                f"msp_in_silico\t{location}\t1\tpredicted\tFalse\n",
                encoding="ascii",
            )
            _write_reproduction_files(output, state, [str(output / "MSDIALCUI.exe")])
            report = publish(unit)
            handoff = create_datamining_handoff(run_directory=output, write_file=True)

            self.assertIn("1 MSP annotator searching in-silico (predicted) spectra", report["methods_text"])
            self.assertIn(f"Synthetic-GLACIER_pos_allCE.msp (SHA-256 {'ef' * 32})", report["methods_text"])
            self.assertEqual("predicted", handoff["annotation"]["tiers"][-1]["library_kind"])
            shared = [output / name for name in SHARED_FILES] + [Path(handoff["handoff_file"])]
            for path in shared:
                for where, text in _texts(path.name, path.read_bytes()):
                    for encoding in encodings:
                        self.assertNotIn(encoding, text, where)


class SummaryTests(unittest.TestCase):
    def test_representatives_are_counted_by_evidence_source_and_annotator(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "sample.mdpeak"
            header = ["Peak ID", "Name", "Height", "Simple dot product", "Weighted dot product", "Reverse dot product",
                      "Comment", "Evidence source"]
            rows = [
                ["0", "PC 34:1", "100", "0.9", "0.9", "0.9", "Annotation method: LbmDB: lab", "RuleBased"],
                ["1", "Caffeine", "90", "0.8", "0.8", "0.9", "Annotation method: msp_high_quality", "ReferenceSpectrum"],
                ["2", "Predicted A", "80", "0.6", "0.6", "0.6", "Annotation method: msp_in_silico", "InSilico"],
                ["3", "Predicted B", "70", "0.6", "0.6", "0.6", "Annotation method: msp_in_silico", "InSilico"],
                ["4", "Unknown", "60", "null", "null", "null", "", "null"],
            ]
            path.write_text("\n".join("\t".join(row) for row in [header, *rows]) + "\n", encoding="utf-8")
            summary = parse_mdpeak(path)

        self.assertEqual(4, summary["msp_candidate_count"])
        self.assertEqual({"InSilico": 2, "ReferenceSpectrum": 1, "RuleBased": 1}, summary["evidence_source_counts"])
        self.assertEqual({"LbmDB: lab": 1, "msp_high_quality": 1, "msp_in_silico": 2}, summary["annotator_counts"])
        self.assertEqual(2, summary["identification_count"])
        self.assertEqual(2, summary["in_silico_count"])

    def test_an_export_without_the_evidence_column_counts_nothing_as_identified(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "old.mdpeak"
            path.write_text(
                "Name\tHeight\tSimple dot product\tWeighted dot product\tReverse dot product\n"
                "Caffeine\t100\t0.9\t0.9\t0.9\n",
                encoding="utf-8",
            )
            summary = parse_mdpeak(path)
        self.assertIsNone(summary["evidence_source_counts"])
        self.assertIsNone(summary["identification_count"])


class LipidRtSettingTests(unittest.TestCase):
    def test_the_setting_is_complete_canonical_and_refuses_what_the_console_would(self) -> None:
        self.assertEqual((None, []), read_setting(None))
        setting, problems = read_setting("false")
        self.assertEqual([], problems)
        self.assertEqual("False", setting["mode"])
        self.assertEqual("HighPrecision", setting["operating_point"])
        self.assertEqual("True", read_setting({"mode": True})[0]["mode"])
        self.assertEqual("ReversedPhase", read_setting({"chromatography": "reversedphase"})[0]["chromatography"])
        for answer, message in (
            ({"operating_point": "2"}, "lipid_rt_check.operating_point '2' is none of"),
            ({"window": 0.1}, "lipid_rt_check.window is not a setting"),
            (["Auto"], "lipid_rt_check is an object"),
        ):
            with self.subTest(answer=answer):
                setting, problems = read_setting(answer)
                self.assertIsNone(setting)
                self.assertTrue(any(message in item for item in problems), problems)

    def test_the_console_summary_states_the_rule_version(self) -> None:
        self.assertEqual(
            {"summary": "not run.", "rule_version": "", "parameter_hash": ""},
            console_summary(["Lipid RT check (alignment): not run."]),
        )
        self.assertEqual("1.0", console_summary(["x", "Lipid RT check (alignment): 1 annotated, 0 evaluable: 0 "
                                                     "Confirmed; rules 1.0, parameter ab12."])["rule_version"])

    def test_a_blank_template_value_is_skipped_as_the_console_skips_it(self) -> None:
        # The Console reads a key's last line with a value (ConfigParser.ReadLast): a blank one leaves the key at
        # its default, or at an earlier line's value.
        self.assertEqual({}, template_setting({"lipid rt check operating point": ""}))
        self.assertEqual(
            {"lipid rt check": "False"},
            template_values(["Lipid RT check: False", "# Lipid RT check: True", "Lipid RT check:", "Other: x"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            template = Path(temporary) / "method.txt"
            template.write_text(
                TEMPLATE.read_text(encoding="utf-8-sig")
                + "\nLipid RT check: False\nLipid RT check operating point:\nLipid RT check:\n",
                encoding="utf-8",
            )
            workflow = load_parameter_template(template)["workflow"]
        self.assertNotIn("lipid_rt_check_template_problems", workflow)
        self.assertEqual(("False", "HighPrecision"), (
            workflow["lipid_rt_check"]["mode"], workflow["lipid_rt_check"]["operating_point"],
        ))

    def test_the_methods_read_the_consoles_summary_for_a_failed_check_and_an_unpassed_gate(self) -> None:
        counts = RT_SUMMARY.split(": ", 1)[1]
        self.assertEqual(
            ["", "failed", "not_run", "gate_not_passed", "evaluated"],
            [summary_outcome(text) for text in (
                "", "failed (InvalidOperationException: x); no verdicts or Confirmed tags were written by it, and "
                "its earlier ones were withdrawn (0).", "not run.",
                counts + " Reversed-phase gate not passed: 0.41 < 0.6.", counts,
            )],
        )
        listed = {"name": "A_lipidrt_review.tsv", "sha256": "ab" * 32, "rows": 0}
        evidence = {"setting": read_setting("Auto")[0], "written_to_method_file": True, "ran": True,
                    "review_files": [listed], "rule_version": "1.1"}
        gate = _lipid_rt_check_sentence({**evidence, "summary": counts + " Reversed-phase gate not passed: x."})
        self.assertIn("A_lipidrt_review.tsv", gate)
        self.assertIn("reversed-phase gate was not passed, so no lipid annotation was evaluated", gate)
        self.assertNotIn("found nothing to review", gate)
        evaluated = _lipid_rt_check_sentence({**evidence, "summary": counts})
        self.assertIn("an empty list means the check found nothing to review", evaluated)
        failed = _lipid_rt_check_sentence({
            **evidence, "ran": False, "review_files": [],
            "summary": "failed (IOException: disk); no verdicts or Confirmed tags were written by it, and its "
                       "earlier ones were withdrawn (3).",
        })
        self.assertIn("reported that the check failed in this run and withdrew every verdict and Confirmed tag", failed)
        self.assertNotIn("did not run", failed)


class ReviewedCapabilityTests(unittest.TestCase):
    """What a state is refused for whatever path built it: the web page's, a template's, or the guided plan's."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.console = fake_console(self.root / "MSDIALCUI.exe")
        self.in_silico = self.root / "Synthetic-GLACIER_neg_allCE.msp"
        self.in_silico.write_text("NAME: B\n", encoding="ascii")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def issues(self, state: dict) -> list[str]:
        with patch("msdial_app.workflow.subprocess.run", side_effect=OSError("not a Console")):
            return [issue["message"] for issue in _annotator_capability_issues(state, "lcms")]

    def test_a_template_value_the_console_would_refuse_is_refused_without_an_in_silico_row_or_a_console(self) -> None:
        problem = "The parameter template's 'Lipid RT check operating point' is 'Fast', none of Strict."
        state = {"project_type": "lcms", "lipid_rt_check_template_problems": [problem],
                 "msp_annotators": [{"annotator_id": "msp_annotator_1", "msp_file_path": "a.msp"}]}
        self.assertEqual([problem], self.issues(state))
        self.assertEqual([problem], self.issues({**state, "console_path": str(self.console)}))
        # A state the web page posts never read the template: the run reads it, and refuses the same value.
        template = self.root / "method.txt"
        template.write_text(
            TEMPLATE.read_text(encoding="utf-8-sig") + "\nLipid RT check operating point: Fast\n", encoding="utf-8"
        )
        posted = dict(load_parameter_template(template)["workflow"])
        posted.pop("lipid_rt_check_template_problems")
        posted.update(template_path=str(template), msp_annotators=[], console_path="")
        self.assertIn("lipid_rt_check_template_problems", with_template_lipid_rt_check(posted))
        messages = [issue["message"] for issue in validate_workflow(posted) if issue["level"] == "error"]
        self.assertTrue(any("'Lipid RT check operating point' is 'Fast'" in item for item in messages), messages)

    def test_a_predicted_row_scored_as_lipidomics_is_refused(self) -> None:
        # A table without a target_omics column, read back from a template: the row takes the project's.
        table = self.root / "msp_annotator_settings.tsv"
        table.write_text(
            "annotator_id\tmsp_file_path\tpriority\tlibrary_kind\n"
            f"ins\t{self.in_silico}\t1\tpredicted\n",
            encoding="ascii",
        )
        template = self.root / "method.txt"
        template.write_text(
            f"Target omics: Lipidomics\nMSP annotator settings file path: {table.name}\n", encoding="ascii"
        )
        rows = load_parameter_template(template)["msp_annotators"]
        self.assertEqual([("predicted", "")], [(row["library_kind"], row.get("target_omics", "")) for row in rows])
        state = {"project_type": "lcms", "target_omics": "Lipidomics", "msp_annotators": rows}
        refused = "MSP annotator(s) ins search predicted (in-silico) spectra but are scored as Lipidomics"
        self.assertTrue(any(refused in item for item in self.issues(state)), self.issues(state))
        rows[0]["target_omics"] = "Lipidomics"
        self.assertTrue(any(refused in item for item in self.issues({**state, "target_omics": "Metabolomics"})))
        rows[0]["target_omics"] = "Metabolomics"
        self.assertFalse(any(refused in item for item in self.issues(state)))

    def test_a_predicted_row_where_the_lipid_rt_check_runs_needs_its_in_silico_exclusion(self) -> None:
        # A Console with the check and library_kind, but not the change that left in-silico matches out of the
        # check (rules 1.1), which came with the per-annotator suggestions switch.
        fake_console(self.console, (*LIBRARY_KIND_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS))
        row = {"annotator_id": "msp_in_silico", "msp_file_path": str(self.in_silico), "library_kind": "predicted",
               "target_omics": "Metabolomics"}
        state = {"project_type": "lcms", "target_omics": "Lipidomics", "console_path": str(self.console),
                 "msp_annotators": [row]}
        refused = "where the lipid RT check runs, but the selected MS-DIAL Console's check predates"
        # Set or not: the Console runs it in a Lipidomics project by default.
        self.assertTrue(any(refused in item for item in self.issues(state)), self.issues(state))
        self.assertTrue(any(refused in item for item in self.issues({**state, "lipid_rt_check": read_setting("Auto")[0]})))
        self.assertEqual([], self.issues({**state, "lipid_rt_check": read_setting("False")[0]}))
        self.assertEqual([], self.issues({**state, "target_omics": "Metabolomics"}))
        fake_console(self.console)
        self.assertEqual([], self.issues(state))

    def test_the_methods_cite_neither_a_private_library_as_downloaded_nor_another_in_silico_library_as_glacier(
        self,
    ) -> None:
        other = tiered_lcms_annotation("lipids.lbm2", "public.msp", None, "CFMID_neg.msp")
        workflow = {
            "msp_annotators": other["msp_annotators"],
            "library_provenance": [
                {"path": "public.msp", "doi": "10.1000/example", "license": "CC BY 4.0"},
                {"path": "CFMID_neg.msp", "source": "https://github.com/coleygroup/ms-pred",
                 "license": "institutional/private", "library_kind": "predicted"},
            ],
        }
        text = _annotation_sentence(workflow, [{"name": "CFMID_neg.msp", "sha256": "cd" * 32, "private": True}])
        self.assertIn("Downloaded libraries were public.msp (10.1000/example).", text)
        self.assertIn(f"identified by file name and SHA-256 digest: CFMID_neg.msp (SHA-256 {'cd' * 32})", text)
        self.assertNotIn("ms-pred", text)
        self.assertNotIn("GLACIER", text)
        self.assertIn("evidence source InSilico", text)


if __name__ == "__main__":
    unittest.main()
