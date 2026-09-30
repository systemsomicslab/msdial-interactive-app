"""Every step after a download can reach its unit through the unit's manifest, not only its job.

Preflight, split, preparation, the diagnostic estimate, QA and publication each found their unit through a
completed job in the backend's registry. That registry is persisted truncated to its hundred newest jobs
and forgets running ones when the backend restarts, so at campaign scale a unit's download job is evicted
long before its later steps run, and the unit became unreachable although its manifest sat on disk. The
diagnostic was worse: its heights lived only in the registry, so an evicted diagnostic meant running the
Console again.

Each of these is exercised here with an empty registry, and with no backend at all where the step runs in
the MCP process.
"""

from __future__ import annotations

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
from subprocess import CompletedProcess
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, server
    from msdial_app.repository_reanalysis import (
        _write_json,
        finalize_download_lease,
        read_manifest,
    )


def _no_backend(*_args, **_kwargs):
    raise AssertionError("the manifest route must not need the backend or its job registry")


class _Unit:
    """A downloaded repository unit on disk: four mzML inputs, a manifest, an output directory."""

    MODES = {"a_DDA_1.mzML": "DDA", "b_DIA_1.mzML": "DIA", "c_DDA_2.mzML": "DDA", "d_DIA_2.mzML": "DIA"}

    def make(self, root: Path, **extra) -> Path:
        data = root / "raw" / "data"
        for directory in (data, root / "provenance", root / "output"):
            directory.mkdir(parents=True, exist_ok=True)
        files = []
        for name in self.MODES:
            (data / name).write_bytes(b"x")
            files.append(data / name)
        manifest = root / "provenance" / "run-manifest.json"
        _write_json(
            manifest,
            {
                "schema": "msdial-public-reanalysis-run.v1",
                "status": "prepared",
                "workspace": str(root),
                "raw_directory": str(root / "raw"),
                "input_directory": str(data),
                "output_directory": str(root / "output"),
                "input_candidates": [str(path) for path in files],
                "execution_allowed": False,
                "raw_retention_policy": "keep",
                "project": {
                    "repository": "metabolights",
                    "accession": "MTBLS-REENTRY",
                    "analysis_unit_id": "unit-reentry",
                    "separation": "LC-MS",
                    "acquisition_mode": "DIA",
                    "ion_mode": "Negative",
                    "untargeted": True,
                    "files": [
                        {"name": f"FILES/{path.name}", "size_bytes": 1, "url": "", "role": "converted"}
                        for path in files
                    ],
                    "total_download_bytes": len(files),
                    "sample_count": len(files),
                    "sample_metadata": [
                        {"sample_id": path.stem, "raw_file": f"FILES/{path.name}", "values": {"Group": path.stem[0]}}
                        for path in files
                    ],
                },
                **extra,
            },
        )
        return manifest

    def extractor(self, root: Path) -> tuple[Path, object]:
        stub = root / "RawMetadataConsoleApp.exe"
        stub.write_bytes(b"stub")
        modes = self.MODES

        def run(command, **_kwargs):
            inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
            output = Path(command[command.index("--output") + 1])
            records = [
                {
                    "source": {"filePath": path, "fileName": Path(path).stem},
                    "acquisition": {
                        "separation": {"value": "LiquidChromatography"},
                        "method": {"value": modes[Path(path).name], "confidence": 0.8},
                        "polarity": {"value": "Negative"},
                        "msLevels": [1, 2],
                    },
                }
                for path in inputs
            ]
            output.write_text(json.dumps(records), encoding="utf-8")
            return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        return stub, run


class TheMcpStepsReachAUnitByItsManifest(_Unit, unittest.TestCase):
    def test_preflight_runs_with_no_backend_and_no_job(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest = self.make(root)
            stub, run = self.extractor(root)
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend), patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=run
            ):
                result = mcp_server.msdial_repository_raw_metadata_preflight(
                    extractor_path=str(stub), manifest_path=str(manifest)
                )
            recorded = read_manifest(manifest)

        self.assertTrue(result["completed"], result)
        self.assertEqual("preflight_mixed_acquisition", result["status"])
        self.assertEqual({"DDA": 2, "DIA": 2}, result["acquisition_groups"])
        self.assertEqual("preflight_mixed_acquisition", recorded["status"])

    def test_preparation_rebuilds_the_recognised_files_from_the_manifest(self) -> None:
        """The download job's result carried the recognised files; the manifest's input_candidates are
        what it recognised them from, so the same list is rebuilt in place."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest = self.make(root)
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                preview = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Group"], manifest_path=str(manifest)
                )
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Group"], confirmed=True, manifest_path=str(manifest)
                )
            csv_exists = Path(prepared["input_path"]).is_file()
            order_recorded = "analytical_order" in read_manifest(manifest)

        self.assertFalse(preview["prepared"])
        self.assertEqual(4, preview["preview"]["recognized_count"])
        self.assertEqual(4, preview["preview"]["matched_count"])
        self.assertEqual("keep", preview["preview"]["answer_seed"]["workflow_overrides"]["repository_raw_retention_policy"])
        self.assertTrue(prepared["prepared"], prepared)
        self.assertTrue(csv_exists)
        self.assertTrue(order_recorded)

    def test_qa_evidence_reads_the_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.make(Path(temporary) / "unit")
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                result = mcp_server.msdial_repository_qa_evidence(manifest_path=str(manifest))

        self.assertEqual("MTBLS-REENTRY", result["accession"])

    def test_an_unfinished_lease_is_refused_by_manifest_as_its_job_was(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = self.make(Path(temporary) / "unit", status="download_failed",
                                 download_failure={"reason": "MD5 checksum mismatch for x.zip."})
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                result = mcp_server.msdial_prepare_repository_reanalysis(manifest_path=str(manifest))

        self.assertFalse(result["ok"])
        self.assertIn("download_failed", result["detail"])
        self.assertIn("MD5 checksum mismatch", result["detail"])

    def test_naming_neither_identifier_is_a_validation_error(self) -> None:
        result = mcp_server.msdial_repository_raw_metadata_preflight()

        self.assertFalse(result["ok"])
        self.assertIn("download_job_id or manifest_path", result["detail"])


class TheSplitReachesItsParentByManifest(_Unit, unittest.TestCase):
    def test_a_parent_whose_download_job_is_gone_still_splits(self) -> None:
        from msdial_app.repository_reanalysis import run_raw_metadata_preflight

        jobs: dict = {}
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest = self.make(root)
            stub, run = self.extractor(root)
            with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=run):
                run_raw_metadata_preflight(manifest, stub)
            with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None):
                result = server._split_repository_download({"manifest_path": str(manifest), "confirmed": True})
            parent = read_manifest(manifest)
            part_ids = [read_manifest(part["manifest_path"])["job_id"] for part in result["parts"]]

        self.assertTrue(result["written"])
        self.assertEqual("split_by_acquisition", parent["status"])
        self.assertEqual(2, len(result["parts"]))
        self.assertEqual(part_ids, [part["job_id"] for part in result["parts"]])
        for part in result["parts"]:
            job = jobs[part["job_id"]]
            self.assertEqual("repository_split_part", job["kind"])
            self.assertIn("Split from the unit manifest", job["logs"][0])


class _Backend:
    def start(self) -> int:
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        return self.http.server_port

    def stop(self) -> None:
        self.http.shutdown()
        self.http.server_close()
        self.thread.join(timeout=5)

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.http.server_port}{path}",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read().decode("utf-8"))
            error.close()
            return error.code, payload


class TheDiagnosticEstimateOutlivesTheRegistry(_Unit, _Backend, unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.manifest = self.make(self.root)
        self.job_id = "diag0001"
        self.diagnostic = self.root / "diagnostics" / self.job_id
        self.diagnostic.mkdir(parents=True)
        self.result_file = self.diagnostic / "a_DDA_1.mdpeak"
        rows = "".join(f"{height}\t0\t0\t0\n" for height in range(100, 100 * 9001, 100))
        self.result_file.write_text(
            "Height\tSimple dot product\tWeighted dot product\tReverse dot product\n" + rows,
            encoding="utf-8",
        )
        self.preparation = {
            "diagnostic_run_directory": str(self.diagnostic),
            "repository_run_manifest": str(self.manifest),
            "analysis_type": "lcms",
            "diagnostic_result_file": str(self.result_file),
            "peak_tuning_profile": {"file_name": "a_DDA_1", "threshold_step": 100, "reason": "QC nearest"},
        }
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.directory.cleanup()

    def _estimate(self) -> tuple[int, dict]:
        with patch.object(server, "JOBS", {}):
            return self.post(
                "/api/agent/tuning/estimate",
                {
                    "job_id": self.job_id,
                    "manifest_path": str(self.manifest),
                    "target_peak_count_min": 3000,
                    "target_peak_count_max": 6000,
                },
            )

    def test_a_completed_diagnostic_is_estimated_from_its_own_directory(self) -> None:
        """THE REGRESSION. The registry forgot the job, and the answer was to run the Console again."""
        server._write_diagnostic_record(self.job_id, self.preparation, "running")
        server._write_diagnostic_record(self.job_id, self.preparation, "completed", exit_code=0)

        status, response = self._estimate()

        self.assertEqual(200, status, response)
        self.assertTrue(response["ready"])
        self.assertEqual(9000, response["estimate"]["diagnostic_peak_count"])
        self.assertEqual("a_DDA_1", response["representative"]["file_name"])
        recorded = read_manifest(self.manifest)["peak_height_diagnostics"][-1]
        self.assertEqual(self.job_id, recorded["job_id"])
        self.assertTrue(Path(recorded["diagnostic_run_directory"]).samefile(self.diagnostic))

    def test_a_diagnostic_that_never_finished_is_not_ready(self) -> None:
        server._write_diagnostic_record(self.job_id, self.preparation, "running")

        status, response = self._estimate()

        self.assertEqual(200, status, response)
        self.assertFalse(response["ready"])
        self.assertEqual("running", response["status"])

    def test_a_record_from_another_unit_is_refused(self) -> None:
        other = self.make(Path(self.directory.name) / "other")
        server._write_diagnostic_record(
            self.job_id, {**self.preparation, "repository_run_manifest": str(other)}, "completed"
        )

        status, response = self._estimate()

        self.assertEqual(400, status)
        self.assertIn("does not describe diagnostic", response["error"])

    def test_a_diagnostic_from_before_the_record_existed_says_so(self) -> None:
        status, response = self._estimate()

        self.assertEqual(400, status)
        self.assertIn("left no diagnostic-job.json", response["error"])


class QaAndPublicationReachTheFinalisedRun(_Unit, _Backend, unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.manifest = self.make(self.root)
        self.output = self.root / "output"
        (self.output / "result.mzTab").write_text(
            "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n", encoding="ascii"
        )
        (self.output / "workflow-settings.json").write_text(
            json.dumps({"project_type": "lcms", "files": [], "output_root": str(self.output)}),
            encoding="utf-8",
        )
        self.qa = self.output / "AlignResult.qa.tsv"
        with self.qa.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["ID", "File", "Class", "File type", "Injection order", "Batch ID",
                             "Height", "RT", "MZ", "SN", "MSMS", "Reference matched"])
            for spot in range(4):
                for index, name in enumerate(("qc1", "s1", "qc2")):
                    writer.writerow([spot, name, "QC" if "qc" in name else "Case",
                                     "QC" if "qc" in name else "Sample", index + 1, 1,
                                     (spot + 1) * (index + 1) * 100, 5.0 + spot, 100.0 + spot, 10,
                                     "TRUE", "FALSE"])
        self.start()

    def tearDown(self) -> None:
        self.stop()
        self.directory.cleanup()

    def _finalize(self, qa: list[str]) -> None:
        finalize_download_lease(
            self.manifest,
            run={
                "job_id": "run0001",
                "run_directory": str(self.output),
                "artifacts": {"mztab": [str(self.output / "result.mzTab")], "qa": qa},
            },
        )

    def test_qa_is_generated_from_the_matrix_the_finalised_run_produced(self) -> None:
        self._finalize([str(self.qa)])

        with patch.object(server, "JOBS", {}):
            status, response = self.post("/api/qa/report", {"manifest_path": str(self.manifest)})

        self.assertEqual(200, status, response)
        self.assertEqual("run0001", response["job_id"])
        self.assertEqual(str(self.qa), response["qa_file"])
        self.assertEqual(str(self.manifest.resolve()), response["recovered_from"])

    def test_a_run_that_made_no_matrix_is_still_refused(self) -> None:
        """The job-scoped rule holds by manifest too: never QA from a matrix the run did not make."""
        self._finalize([])

        with patch.object(server, "JOBS", {}):
            status, response = self.post("/api/qa/report", {"manifest_path": str(self.manifest)})

        self.assertEqual(400, status)
        self.assertIn("Job run0001 did not create or update an LC-MS QA matrix", response["error"])

    def test_publication_by_manifest_refreshes_the_retained_inventory(self) -> None:
        """The inventory a deletion is judged against was taken before the report existed."""
        self._finalize([str(self.qa)])
        before = set(read_manifest(self.manifest)["retained_artifacts"])

        with patch.object(server, "JOBS", {}):
            status, response = self.post(
                "/api/publication/report",
                {"manifest_path": str(self.manifest), "run_qa": False, "use_saved_run": True},
            )
        manifest = read_manifest(self.manifest)

        self.assertEqual(200, status, response)
        refresh = response["retained_artifacts_refresh"]
        self.assertTrue(refresh["refreshed"], refresh)
        added = {Path(path).name for path in refresh["added"]}
        self.assertIn("MS_DIAL_Materials_and_Methods.txt", added)
        self.assertTrue(before < set(manifest["retained_artifacts"]))
        listed = {item["path"] for item in manifest["retained_artifact_inventory"]}
        self.assertEqual(set(manifest["retained_artifacts"]), listed)
        self.assertEqual("mztab_validated", manifest["status"], "a refresh changes no verdict")

    def test_a_manifest_from_before_the_record_is_refused_rather_than_guessed(self) -> None:
        finalize_download_lease(self.manifest)

        with patch.object(server, "JOBS", {}):
            status, response = self.post("/api/qa/report", {"manifest_path": str(self.manifest)})

        self.assertEqual(400, status)
        self.assertIn("records no finalised production run", response["error"])

    def test_a_registered_job_and_a_manifest_must_name_the_same_unit(self) -> None:
        self._finalize([str(self.qa)])
        other = self.make(Path(self.directory.name) / "other")
        jobs = {
            "run0001": {
                "id": "run0001",
                "kind": "run",
                "status": "completed",
                "preparation": {"run_directory": str(self.output), "repository_run_manifest": str(other)},
                "artifacts": {"qa": [str(self.qa)]},
            }
        }

        with patch.object(server, "JOBS", jobs):
            status, response = self.post(
                "/api/qa/report", {"job_id": "run0001", "manifest_path": str(self.manifest)}
            )

        self.assertEqual(400, status)
        self.assertIn("belongs to", response["error"])

    def test_finalisation_records_the_run_that_produced_the_output(self) -> None:
        self._finalize([str(self.qa)])

        run = read_manifest(self.manifest)["finalized_run"]

        self.assertEqual("run0001", run["job_id"])
        self.assertEqual([str(self.qa)], run["artifacts"]["qa"])
        self.assertEqual(str(self.output), run["run_directory"])


class TheJobsWriteWhatTheManifestRouteReads(_Unit, unittest.TestCase):
    """The records above are only useful if the jobs that run write them."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name) / "unit"
        self.manifest = self.make(self.root)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _diagnostic(self, exit_code: int) -> tuple[list[str], dict]:
        directory = self.root / "diagnostics" / "job1"
        directory.mkdir(parents=True)
        result_file = directory / "a_DDA_1.mdpeak"
        preparation = {
            "run_directory": str(directory),
            "diagnostic_run_directory": str(directory),
            "repository_run_manifest": str(self.manifest),
            "analysis_type": "lcms",
            "diagnostic_result_file": str(result_file),
            "peak_tuning_profile": {"file_name": "a_DDA_1", "threshold_step": 100},
        }
        seen: list[str] = []

        def console(_preparation, _log, **_watch):
            seen.append(read_manifest(directory / server.DIAGNOSTIC_JOB_RECORD)["status"])
            result_file.write_text(
                "Height\tSimple dot product\tWeighted dot product\tReverse dot product\n100\t0\t0\t0\n",
                encoding="utf-8",
            )
            return exit_code

        jobs = {"job1": {"id": "job1", "status": "queued", "kind": "tuning", "logs": [], "preparation": preparation}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", console):
            server._run_tuning_job("job1", preparation)
        return seen, read_manifest(directory / server.DIAGNOSTIC_JOB_RECORD)

    def test_a_diagnostic_records_itself_before_and_after_the_console(self) -> None:
        seen, record = self._diagnostic(0)

        self.assertEqual(["running"], seen, "the record exists while the Console runs")
        self.assertEqual("completed", record["status"])
        self.assertEqual(0, record["exit_code"])
        self.assertTrue(record["diagnostic_result_file"].endswith("a_DDA_1.mdpeak"))
        self.assertEqual("a_DDA_1", record["peak_tuning_profile"]["file_name"])
        self.assertIn("ended_at", record)

    def test_a_failed_diagnostic_is_recorded_as_failed(self) -> None:
        _, record = self._diagnostic(3)

        self.assertEqual("failed", record["status"])
        self.assertEqual(3, record["exit_code"])
        self.assertIn("exited with code 3", record["error"])

    def test_a_production_run_names_itself_in_the_manifest_it_finalises(self) -> None:
        output = self.root / "output"
        preparation = {
            "command": ["MSDIALCUI.exe"],
            "run_directory": str(output),
            "export_folder_path": str(output),
            "repository_run_manifest": str(self.manifest),
            "repository_raw_retention_policy": "keep",
            "expected_analysis_exports": [],
            "qa_matrix_expected": False,
        }

        def console(_preparation, _log, **_watch):
            (output / "result.mzTab").write_text(
                "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n", encoding="ascii"
            )
            (output / "AlignResult.qa.tsv").write_text("ID\n", encoding="utf-8")
            return 0

        jobs = {"run1": {"id": "run1", "status": "queued", "kind": "run", "logs": [], "preparation": preparation,
                         "artifact_baseline": {}}}
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None), \
                patch.object(server, "run_console", console):
            server._run_job("run1", preparation)
        manifest = read_manifest(self.manifest)

        self.assertEqual("completed", jobs["run1"]["status"], jobs["run1"].get("error"))
        self.assertEqual("mztab_validated", manifest["status"])
        self.assertEqual("run1", manifest["finalized_run"]["job_id"])
        self.assertEqual(["AlignResult.qa.tsv"], [Path(path).name for path in manifest["finalized_run"]["artifacts"]["qa"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
