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
    TIERED_LCMS_IN_SILICO_PROFILE_ID,
    TIERED_LCMS_PROFILE_ID,
    tiered_lcms_annotation,
)
from msdial_app.lab_lcms_metabolomics import PRESETS, THRESHOLDS
from msdial_app.library_copy_store import library_names
from msdial_app.lipid_rt_check import console_summary, read_setting
from msdial_app.materials_methods import generate_publication_report
from msdial_app.run_finalisation import lipid_rt_check_outcome
from msdial_app.sharing import library_records
from msdial_app.workflow import (
    LIBRARY_KIND_CONSOLE_MARKERS,
    LIPID_RT_CHECK_CONSOLE_MARKERS,
    MSP_SUGGESTIONS_CONSOLE_MARKERS,
    _write_msp_annotator_settings,
    _write_reproduction_files,
    console_capabilities,
    load_parameter_template,
    parse_mdpeak,
    prepare_run,
)

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "resources" / "msdial_console_param4lipidomics.txt"
ALL_MARKERS = (*LIBRARY_KIND_CONSOLE_MARKERS, *MSP_SUGGESTIONS_CONSOLE_MARKERS, *LIPID_RT_CHECK_CONSOLE_MARKERS)


def method_lines(path: str | Path) -> dict[str, str]:
    lines: dict[str, str] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if ":" in line and not line.startswith("#"):
            key, _, value = line.partition(":")
            lines[key.strip()] = value.strip()
    return lines


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
        profile = tiered_lcms_annotation("lipids.lbm2", "private.msp", {"evidence_tier": "x"}, "insilico_neg.msp")

        self.assertEqual(TIERED_LCMS_IN_SILICO_PROFILE_ID, profile["profile_id"])
        self.assertEqual(4, profile["lbm_annotator"]["priority"])
        rows = profile["msp_annotators"]
        self.assertEqual(
            [("msp_high_quality", 3, "acquired", "private.msp"), ("msp_low_quality", 2, "acquired", "private.msp"),
             ("msp_in_silico", 1, "predicted", "insilico_neg.msp")],
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
        self.in_silico = self.libraries / "Synthetic-InSilico_neg_allCE.msp"
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
        other = self.libraries / "Synthetic-InSilico_pos_allCE.msp"
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
        self.assertIn("No review list was written, so the check did not run.", text)
        self.assertNotIn(str(self.libraries), text)

        review = Path(prepared["run_directory"]) / "AlignResult-2026_lipidrt_review.tsv"
        review.write_text("ID\tName\n", encoding="utf-8")
        method_time = Path(prepared["method_file"]).stat().st_mtime
        os.utime(review, (method_time + 5, method_time + 5))
        text = generate_publication_report(
            settings, None, prepared["run_directory"], app_version=__version__, console_version="test",
        )["methods_text"]
        self.assertIn(f"{review.name} (SHA-256 {hashlib.sha256(review.read_bytes()).hexdigest()}; 0 rows)", text)
        self.assertNotIn("did not run", text)

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

        location = "E:\\insilico libs\\GLACIER\\Synthetic-InSilico_pos_allCE.msp"
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
                "path": location, "name": "Synthetic-InSilico_pos_allCE.msp", "version": "synthetic-1",
                "source": "https://github.com/coleygroup/ms-pred", "doi": "", "license": "institutional/private",
                "library_kind": "predicted",
            })
            recorded = json.loads((output / "run-manifest.json").read_text(encoding="utf-8"))
            recorded["libraries"].append({
                "name": "Synthetic-InSilico_pos_allCE.msp", "filename": "Synthetic-InSilico_pos_allCE.msp",
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
            self.assertIn(f"Synthetic-InSilico_pos_allCE.msp (SHA-256 {'ef' * 32})", report["methods_text"])
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


if __name__ == "__main__":
    unittest.main()
