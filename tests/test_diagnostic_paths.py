"""The peak-count diagnostic must not be able to touch, or be mistaken for, a production result.

The failure these guard against was observed end to end: a diagnostic run reduced a reviewed
thirty-row analysis_files.csv to its single representative, and the next planning call produced a
fully self-consistent single-file plan reporting ready_to_prepare with no blockers, while the
reviewed metadata, the Class proposal and every downstream report still described thirty samples in
ten classes.

Two separations are tested, because one cannot cover both layouts. A repository unit's diagnostics
sit outside the production output entirely. A local analysis has no workspace to put a sibling in, so
its diagnostics sit inside the output under a dotted name, and every production artifact discovery
filters that name out by path.
"""

from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_bridge import _select_primary_mztab_file
from msdial_app.diagnostic_paths import (
    LOCAL_DIAGNOSTIC_DIRECTORY,
    REPOSITORY_DIAGNOSTIC_DIRECTORY,
    diagnostic_run_directory,
    is_diagnostic_artifact,
)
from msdial_app.mztab_validation import find_mztab_files
from msdial_app.workflow import prepare_tuning_run


MZTAB = "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _row_count(path: Path) -> int:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return len(list(csv.DictReader(handle)))


class DiagnosticDirectoryTests(unittest.TestCase):
    def test_a_repository_unit_puts_diagnostics_outside_its_production_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "unit"
            output = workspace / "output"
            output.mkdir(parents=True)

            directory = diagnostic_run_directory(output, "job-1", workspace=workspace)

            self.assertEqual(
                (workspace / REPOSITORY_DIAGNOSTIC_DIRECTORY / "job-1").resolve(), directory
            )
            self.assertNotIn(output.resolve(), directory.resolve().parents)

    def test_a_local_analysis_stays_inside_the_output_the_user_chose(self):
        # Quietly creating a sibling next to the user's output directory would write somewhere they
        # did not point at.
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "my-results"
            output.mkdir()

            directory = diagnostic_run_directory(output, "job-1")

            self.assertEqual(output.resolve() / LOCAL_DIAGNOSTIC_DIRECTORY / "job-1", directory)

    def test_a_workspace_that_does_not_own_the_output_is_refused(self):
        # Otherwise a workflow could steer a diagnostic anywhere by pairing an unrelated workspace
        # with an output_root of its choosing.
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "unit"
            elsewhere = Path(temporary) / "elsewhere" / "output"
            elsewhere.mkdir(parents=True)
            workspace.mkdir()

            with self.assertRaises(ValueError):
                diagnostic_run_directory(elsewhere, "job-1", workspace=workspace)

    def test_two_diagnostics_never_share_a_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "output"
            output.mkdir()

            first = diagnostic_run_directory(output, "job-1")
            second = diagnostic_run_directory(output, "job-2")

            self.assertNotEqual(first, second)

    def test_a_diagnostic_directory_needs_a_job_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(ValueError):
                diagnostic_run_directory(Path(temporary), "  ")


class DiagnosticClassificationTests(unittest.TestCase):
    def test_both_layouts_are_recognised(self):
        self.assertTrue(is_diagnostic_artifact(Path("C:/u/unit/diagnostics/job-1/x.mzTab")))
        self.assertTrue(is_diagnostic_artifact(Path("C:/u/out/.msdial-diagnostics/job-1/x.mzTab")))

    def test_a_production_artifact_is_not_classified_as_diagnostic(self):
        self.assertFalse(is_diagnostic_artifact(Path("C:/u/unit/output/AlignResult-1.mzTab")))


class ProductionDiscoveryTests(unittest.TestCase):
    def _output_with_diagnostic(self, root: Path) -> tuple[Path, Path, Path]:
        output = root / "output"
        output.mkdir(parents=True)
        production = output / "AlignResult-1.mzTab"
        production.write_text(MZTAB, encoding="ascii")
        diagnostic_directory = diagnostic_run_directory(output, "job-1")
        diagnostic_directory.mkdir(parents=True)
        diagnostic = diagnostic_directory / "AlignResult-diagnostic.mzTab"
        diagnostic.write_text(MZTAB, encoding="ascii")
        # Make the diagnostic the newest file, which is what the primary-file choice keys on.
        production_time = production.stat().st_mtime
        import os

        os.utime(diagnostic, (production_time + 60, production_time + 60))
        return output, production, diagnostic

    def test_a_diagnostic_mztab_is_not_discovered_as_a_run_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            output, production, diagnostic = self._output_with_diagnostic(Path(temporary))

            found = find_mztab_files(output)

            self.assertIn(production.resolve(), [path.resolve() for path in found])
            self.assertNotIn(diagnostic.resolve(), [path.resolve() for path in found])

    def test_a_newer_diagnostic_mztab_does_not_become_the_primary_file(self):
        # The primary-file choice takes the newest match, so an unfiltered scan would hand a
        # single-sample diagnostic to validation, publication and the data-mining handoff.
        with tempfile.TemporaryDirectory() as temporary:
            _, production, diagnostic = self._output_with_diagnostic(Path(temporary))

            primary = _select_primary_mztab_file(
                [{"path": str(production)}, {"path": str(diagnostic)}]
            )

            self.assertEqual(str(production), primary)


class ProductionInputProtectionTests(unittest.TestCase):
    def _workflow(self, root: Path) -> dict:
        output = root / "output"
        data = root / "data"
        output.mkdir(parents=True)
        data.mkdir()
        console = root / "MSDIALCUI.exe"
        console.write_text("", encoding="ascii")
        template = root / "method.txt"
        template.write_text("Ion mode: Negative\n", encoding="ascii")
        files = []
        for index in range(3):
            path = data / f"sample_{index + 1}.mzML"
            path.write_text("x", encoding="ascii")
            files.append(
                {
                    "file_path": str(path),
                    "file_name": path.stem,
                    "file_type": "Sample",
                    "class_id": f"Class{index + 1}",
                    "acquisition_type": "DDA",
                    "batch_order": 1,
                    "analytical_order": index + 1,
                }
            )
        return {
            "files": files,
            "output_root": str(output),
            "project_type": "LCMS",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "selected_adducts": ["[M-H]-"],
            "console_path": str(console),
            "template_path": str(template),
        }

    def test_a_diagnostic_leaves_the_production_analysis_csv_untouched(self):
        # This is the regression itself: the shared preparer rewrites analysis_files.csv, method.txt
        # and run-manifest.json in whatever directory it is given.
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = self._workflow(root)
            output = Path(workflow["output_root"])
            production_csv = output / "analysis_files.csv"
            with production_csv.open("w", encoding="ascii", newline="") as handle:
                writer = csv.DictWriter(
                    handle,
                    fieldnames=["file_path", "file_name", "file_type", "class_id"],
                    lineterminator="\n",
                )
                writer.writeheader()
                for item in workflow["files"]:
                    writer.writerow({key: item[key] for key in writer.fieldnames})
            before_digest, before_rows = _sha256(production_csv), _row_count(production_csv)
            self.assertEqual(3, before_rows)

            directory = diagnostic_run_directory(output, "job-1")
            preparation = prepare_tuning_run(workflow, workflow["files"][1]["file_path"], directory)

            self.assertEqual(before_digest, _sha256(production_csv))
            self.assertEqual(before_rows, _row_count(production_csv))
            self.assertNotEqual(
                production_csv.resolve(), Path(preparation["input_csv"]).resolve()
            )
            self.assertTrue(is_diagnostic_artifact(preparation["input_csv"]))
            self.assertEqual(1, _row_count(Path(preparation["input_csv"])))

    def test_a_second_diagnostic_does_not_reuse_the_first_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = self._workflow(root)
            output = Path(workflow["output_root"])

            first = prepare_tuning_run(
                workflow,
                workflow["files"][0]["file_path"],
                diagnostic_run_directory(output, "job-1"),
            )
            second = prepare_tuning_run(
                workflow,
                workflow["files"][1]["file_path"],
                diagnostic_run_directory(output, "job-2"),
            )

            self.assertNotEqual(
                Path(first["input_csv"]).parent, Path(second["input_csv"]).parent
            )


if __name__ == "__main__":
    unittest.main()
