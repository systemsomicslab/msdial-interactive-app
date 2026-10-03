"""The peak-count diagnostic measures peaks, so an LC-MS diagnostic loads no annotation library.

Only the .mdpeak rows and their Height column are read from a diagnostic (workflow.parse_mdpeak, then
estimate_peak_height_range). The LC-MS Console writes one row per peak spotted, after peak spotting,
isotope estimation and deconvolution, and annotation neither adds nor removes a peak nor changes its
height. A diagnostic that annotated as the production run does searched the tiered LBM -> strict MSP ->
broad MSP cascade at Minimum peak height 0, where every peak above the noise is a query, and a
diagnostic of one Waters AIF file ran for more than forty minutes for a number annotation cannot change.

What is held here: the diagnostic's method and settings name no library; the production method still
names every one; every setting that decides which peaks are spotted is the same in both; and the
diagnostic says, wherever it is recorded, that annotation was skipped and why.
"""

from __future__ import annotations

import copy
import csv
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import server

from msdial_app import workflow
from msdial_app.annotation_pipeline import TIERED_LCMS_PROFILE_ID, apply_tiered_lcms_annotation
from msdial_app.repository_reanalysis import _write_json, read_manifest, record_peak_height_diagnostic
from msdial_app.workflow import (
    ANNOTATION_LIBRARY_METHOD_KEYS,
    DIAGNOSTIC_ANNOTATION_PERFORMED,
    DIAGNOSTIC_ANNOTATION_SKIPPED,
    console_method_key,
    expand_paths,
    prepare_run,
    prepare_tuning_run,
)

TEMPLATE = Path(__file__).resolve().parent.parent / "resources" / "msdial_console_param4lipidomics.txt"

# Every method-file setting the LC-MS Console reads for peak spotting (PeakSpottingCore), isotope
# estimation (IsotopeEstimator), MS2 deconvolution (Ms2Dec) and the characterisation that follows
# annotation (PeakCharacterEstimator), plus the run's identity. Minimum peak height is the one that
# differs, by design.
PEAK_SPOTTING_KEYS = (
    "ms1 data type",
    "ms2 data type",
    "ion mode",
    "target omics",
    "ionization",
    "machine category",
    "smoothing method",
    "smoothing level",
    "minimum peak width",
    "average peak width",
    "mass slice width",
    "retention time begin",
    "retention time end",
    "ms1 mass range begin",
    "ms1 mass range end",
    "ms2 mass range begin",
    "ms2 mass range end",
    "ms1 tolerance for centroid",
    "ms2 tolerance for centroid",
    "accuracy type",
    "max charge number",
    "considering br and cl for isotopes",
    "exclude mass list",
    "max isotopes detected in ms1 spectrum",
    "sigma window value",
    "amplitude cut off",
    "keep isotope range",
    "exclude after precursor",
    "keep original precursor isotopes",
    "is do andromeda ms2 deconvolution",
    "andromeda delta",
    "andromeda max peaks",
    "target ce",
    "compounds library file path for target detection",
    "searched adduct ions",
    "number of threads",
)

# What a diagnostic may change, and nothing else: the threshold it measures at, the alignment it does
# not run, the libraries it does not load, and where its own exports go.
ALLOWED_DIFFERENCES = frozenset(
    {
        "minimum peak height",
        "together with alignment",
        "alignment light mode",
        "execute rt correction",
        "annotation pipeline profile",
        "export folder path",
        *ANNOTATION_LIBRARY_METHOD_KEYS,
    }
)


def _method(path: str | Path) -> dict[str, list[str]]:
    """Every method-file key as the Console reads it, with each value it is given."""
    values: dict[str, list[str]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        key = console_method_key(line)
        if key is None:
            continue
        separators = [index for index in (line.find(":"), line.find("=")) if index >= 0]
        values.setdefault(key, []).append(line[min(separators) + 1 :].strip())
    return values


class _Unit:
    """A two-file LC-MS state annotated the way a campaign unit is: LBM, two MSP tiers and a text DB."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        data = self.root / "data"
        data.mkdir()
        for name in ("qc_01.mzML", "qc_02.mzML"):
            (data / name).write_text("x", encoding="ascii")
        libraries = self.root / "libraries"
        libraries.mkdir()
        self.msp = libraries / "private-msp-stand-in.msp"
        self.msp.write_text("NAME: stand-in\n", encoding="ascii")
        self.lbm = libraries / "lipid-stand-in.lbm2"
        self.lbm.write_bytes(b"stand-in")
        self.text = libraries / "text-db-stand-in.txt"
        self.text.write_text("stand-in\n", encoding="ascii")
        self.library_names = (self.msp.name, self.lbm.name, self.text.name)
        console = self.root / "MSDIALCUI.exe"
        console.write_bytes(b"")
        self.state = {
            "files": expand_paths([str(data / "qc_01.mzML"), str(data / "qc_02.mzML")]),
            "project_type": "lcms",
            "console_path": str(console),
            "template_path": str(TEMPLATE),
            "output_root": str(self.root / "output"),
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "selected_adducts": ["[M-H]-", "[M+FA-H]-", "[M+Hac-H]-"],
            "selected_lipids": [],
            "together_with_alignment": True,
            "height_matrix_export": True,
            "smoothing_method": "TimeBasedLinearWeightedMovingAverage",
            "minimum_peak_height": 500,
            "mass_slice_width": 0.05,
            "minimum_peak_width": 4,
            "ms1_tolerance": 0.005,
            "ms2_tolerance": 0.02,
            "number_of_threads": 6,
            "text_annotators": [{"annotator_id": "text_annotator_1", "text_db_file_path": str(self.text)}],
            "library_provenance": [
                {"path": str(self.msp), "version": "VS21", "license": "institutional/private"},
                {"path": str(self.lbm), "version": "1", "license": "CC BY 4.0"},
            ],
        }
        apply_tiered_lcms_annotation(self.state, str(self.lbm), str(self.msp))
        self.original = copy.deepcopy(self.state)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def production(self) -> dict:
        return prepare_run(copy.deepcopy(self.state))

    def diagnostic(self, **options) -> dict:
        return prepare_tuning_run(
            self.state, self.state["files"][0]["file_path"], self.root / "diagnostics" / "job-1", **options
        )


class TheDiagnosticLoadsNoLibrary(_Unit, unittest.TestCase):
    def test_its_method_names_no_library_and_no_annotation_profile(self) -> None:
        method = _method(self.diagnostic()["method_file"])

        for key in ANNOTATION_LIBRARY_METHOD_KEYS:
            self.assertEqual([""], method.get(key), key)
        self.assertNotIn("annotation pipeline profile", method)

    def test_it_writes_no_annotator_settings_file(self) -> None:
        directory = Path(self.diagnostic()["run_directory"])

        self.assertFalse((directory / "msp_annotator_settings.tsv").exists())
        self.assertFalse((directory / "text_annotator_settings.tsv").exists())

    def test_no_library_is_named_in_anything_it_writes(self) -> None:
        # The diagnostic's settings, manifest, method and scripts: none of them may name a library the
        # run did not load. A private MSP named there would also travel in the workflow bundle.
        directory = Path(self.diagnostic()["run_directory"])

        for path in directory.iterdir():
            if path.suffix in {".txt", ".json", ".csv", ".tsv", ".ps1", ".sh"}:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                for name in self.library_names:
                    self.assertNotIn(name, text, f"{name} in {path.name}")

    def test_its_manifest_lists_no_library_and_says_annotation_was_skipped(self) -> None:
        preparation = self.diagnostic()
        manifest = json.loads(Path(preparation["manifest"]).read_text(encoding="utf-8"))

        self.assertEqual([], manifest["libraries"])
        self.assertEqual(DIAGNOSTIC_ANNOTATION_SKIPPED, manifest["diagnostic_annotation"]["status"])
        self.assertIn("Height", manifest["diagnostic_annotation"]["reason"])
        self.assertEqual(manifest["diagnostic_annotation"], preparation["diagnostic_annotation"])

    def test_its_record_names_what_the_production_run_annotates_with_by_role(self) -> None:
        annotation = self.diagnostic()["diagnostic_annotation"]

        self.assertEqual(TIERED_LCMS_PROFILE_ID, annotation["production_annotation_pipeline_profile"])
        self.assertEqual(["lbm", "msp", "text"], annotation["production_library_roles_not_loaded"])

    def test_its_settings_file_says_annotation_was_skipped(self) -> None:
        preparation = self.diagnostic()
        settings = json.loads(Path(preparation["settings_file"]).read_text(encoding="utf-8"))

        self.assertEqual(DIAGNOSTIC_ANNOTATION_SKIPPED, settings["diagnostic_annotation"]["status"])
        self.assertEqual([], settings["msp_annotators"])
        self.assertEqual("", settings["lbm_path"])
        self.assertEqual([], settings["library_provenance"])

    def test_a_template_library_line_is_written_blank_too(self) -> None:
        # An isotope text DB or an annotator settings file is a template line, not Interactive's
        # state, so it is blanked in the method rather than trusted to be empty.
        lines = [
            f"Isotope text DB file path: {self.text}"
            if console_method_key(line) == "isotope text db file path"
            else line
            for line in TEMPLATE.read_text(encoding="utf-8-sig").splitlines()
        ]
        lines.append(f"MSP annotation settings file path: {self.root / 'stale.tsv'}")
        template = self.root / "template.txt"
        template.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.assertIn(str(self.text), template.read_text(encoding="utf-8"))
        self.state["template_path"] = str(template)

        method = _method(self.diagnostic()["method_file"])

        self.assertEqual([""], method["isotope text db file path"])
        self.assertNotIn("msp annotation settings file path", method)
        self.assertEqual([""], method["msp annotator settings file path"])

    def test_the_production_state_is_left_as_it_was(self) -> None:
        self.diagnostic()

        self.assertEqual(self.original, self.state)


class TheProductionRunStillAnnotates(_Unit, unittest.TestCase):
    def test_its_method_names_every_library(self) -> None:
        preparation = self.production()
        method = _method(preparation["method_file"])

        self.assertEqual(1, len(method["lbm file path"]))
        self.assertTrue(Path(method["lbm file path"][0]).samefile(self.lbm))
        self.assertEqual([TIERED_LCMS_PROFILE_ID], method["annotation pipeline profile"])
        settings = Path(method["msp annotator settings file path"][0])
        with settings.open(encoding="ascii", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(["msp_high_quality", "msp_low_quality"], [row["annotator_id"] for row in rows])
        self.assertEqual({str(self.msp.resolve())}, {row["msp_file_path"] for row in rows})
        self.assertTrue(Path(method["text annotator settings file path"][0]).is_file())

    def test_its_manifest_lists_the_libraries_and_no_diagnostic_record(self) -> None:
        manifest = json.loads(Path(self.production()["manifest"]).read_text(encoding="utf-8"))

        self.assertTrue(manifest["libraries"])
        self.assertNotIn("diagnostic_annotation", manifest)

    def test_a_production_run_prepared_after_a_diagnostic_still_annotates(self) -> None:
        self.diagnostic()

        method = _method(self.production()["method_file"])

        self.assertEqual([TIERED_LCMS_PROFILE_ID], method["annotation pipeline profile"])
        self.assertNotEqual([""], method["lbm file path"])


class PeakSpottingIsTheProductionMethods(_Unit, unittest.TestCase):
    def test_every_peak_spotting_setting_is_the_same(self) -> None:
        production = _method(self.production()["method_file"])
        diagnostic = _method(self.diagnostic()["method_file"])

        for key in PEAK_SPOTTING_KEYS:
            self.assertIn(key, production, key)
            self.assertEqual(production[key], diagnostic.get(key), key)

    def test_nothing_else_differs(self) -> None:
        production = _method(self.production()["method_file"])
        diagnostic = _method(self.diagnostic()["method_file"])

        differing = {
            key for key in set(production) | set(diagnostic) if production.get(key) != diagnostic.get(key)
        }

        self.assertLessEqual(differing, ALLOWED_DIFFERENCES)
        self.assertEqual(["0"], diagnostic["minimum peak height"])
        self.assertEqual(["False"], diagnostic["together with alignment"])


class TheGuiDiagnosticStillAnnotates(_Unit, unittest.TestCase):
    """The GUI panel tunes the MSP score cutoffs from the same run, so it asks for annotation."""

    def test_it_keeps_the_libraries_with_every_candidate_kept(self) -> None:
        preparation = self.diagnostic(annotate=True)
        method = _method(preparation["method_file"])

        self.assertNotEqual([""], method["lbm file path"])
        settings = Path(method["msp annotator settings file path"][0])
        with settings.open(encoding="ascii", newline="") as handle:
            rows = list(csv.DictReader(handle, delimiter="\t"))
        self.assertEqual(2, len(rows))
        for row in rows:
            self.assertEqual(0.0, float(row["weighted_dot_product_cutoff"]))
            self.assertEqual(0.0, float(row["minimum_spectrum_match"]))
        self.assertEqual(DIAGNOSTIC_ANNOTATION_PERFORMED, preparation["diagnostic_annotation"]["status"])

    def test_a_gcms_diagnostic_keeps_its_annotation(self) -> None:
        # Not established for GC-MS: what a .mdscan row is does not rest on the LC-MS reading.
        captured: list[dict] = []
        self.state["project_type"] = "gcms"
        with patch.object(workflow, "prepare_run", side_effect=lambda state: captured.append(state) or {}):
            preparation = self.diagnostic()

        self.assertEqual(
            [str(self.msp), str(self.msp)], [row["msp_file_path"] for row in captured[0]["msp_annotators"]]
        )
        self.assertEqual(str(self.lbm), captured[0]["lbm_path"])
        self.assertEqual(DIAGNOSTIC_ANNOTATION_PERFORMED, preparation["diagnostic_annotation"]["status"])


class TheRecordCarriesTheAnnotationVerdict(unittest.TestCase):
    def test_the_unit_manifest_keeps_it_beside_the_count(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = Path(temporary) / "run-manifest.json"
            _write_json(manifest, {"schema": "msdial-public-reanalysis-run.v1"})
            annotation = {"status": DIAGNOSTIC_ANNOTATION_SKIPPED, "reason": "peaks only"}

            record_peak_height_diagnostic(
                manifest, {"diagnostic_peak_count": 9000}, {}, job_id="j1", annotation=annotation
            )
            record_peak_height_diagnostic(manifest, {"diagnostic_peak_count": 9000}, {}, job_id="j0")

            first, second = read_manifest(manifest)["peak_height_diagnostics"]
        self.assertEqual(annotation, first["annotation"])
        # A diagnostic from before the record existed says nothing, rather than something invented.
        self.assertNotIn("annotation", second)

    def test_the_job_record_keeps_it_for_an_estimate_made_from_disk(self) -> None:
        # _write_diagnostic_record is what _diagnostic_job_from_manifest rebuilds the preparation from,
        # and the rebuilt preparation is what the estimate records in the unit manifest.
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary) / "diagnostics" / "job-1"
            directory.mkdir(parents=True)
            annotation = {"status": DIAGNOSTIC_ANNOTATION_SKIPPED, "reason": "peaks only"}
            preparation = {"diagnostic_run_directory": str(directory), "diagnostic_annotation": annotation}

            server._write_diagnostic_record("job-1", preparation, "running")
            server._write_diagnostic_record("job-1", preparation, "completed", exit_code=0)

            record = read_manifest(directory / server.DIAGNOSTIC_JOB_RECORD)
        self.assertEqual(annotation, record["annotation"])
        self.assertEqual("completed", record["status"])


class EachEndpointAsksForWhatItReads(_Unit, unittest.TestCase):
    """The agent diagnostic reads the peaks only; the GUI panel reads the match scores as well."""

    def setUp(self) -> None:
        super().setUp()
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.calls: list[dict] = []

    def tearDown(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)
        super().tearDown()

    def _prepare(self, state, file_path, output_root, **options):
        # Stop before a job is registered or a Console started: only the request matters here.
        self.calls.append(options)
        raise ValueError("stopped before the Console")

    def _post(self, path: str, body: dict) -> dict:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.http.server_port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return payload

    def test_the_agent_and_campaign_diagnostic_skips_annotation(self) -> None:
        plan = {"workflow": self.state, "requires_diagnostic": True}
        with (
            patch.object(server, "build_guided_plan", return_value=plan),
            patch.object(server, "evaluate_repository_execution_gate", return_value={"allowed": True}),
            patch.object(server, "prepare_tuning_run", side_effect=self._prepare),
        ):
            response = self._post("/api/agent/tuning/run", {"input_path": "x", "answers": {}, "confirmed": True})

        self.assertIn("stopped before the Console", response.get("error", ""))
        self.assertEqual([{"annotate": False}], self.calls)

    def test_the_gui_diagnostic_annotates(self) -> None:
        with (
            patch.object(server, "evaluate_repository_execution_gate", return_value={"allowed": True}),
            patch.object(server, "prepare_tuning_run", side_effect=self._prepare),
        ):
            response = self._post("/api/tuning/run", {"workflow": self.state, "file_path": ""})

        self.assertIn("stopped before the Console", response.get("error", ""))
        self.assertEqual([{"annotate": True}], self.calls)

    def test_an_agent_can_tell_the_diagnostic_skips_annotation(self) -> None:
        from msdial_app.agent_bridge import summarize_jobs

        self.assertIn("peak_count_diagnostic_without_annotation", summarize_jobs({})["capabilities"])


if __name__ == "__main__":
    unittest.main()
