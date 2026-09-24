from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from msdial_app.repository_reanalysis import (
    EligibilityPolicy,
    RepositoryFile,
    RepositoryProject,
    _infer_acquisition,
    _infer_ion_mode,
    _parse_apache_index,
    _parse_tab_blocks,
    _parse_workbench_downloads,
    _parse_metabobank_filelist,
    _parse_metabobank_sdrf,
    _metabobank_raw_files,
    _metabobank_raw_references,
    _raw_names_from_assay,
    _summarize_raw_metadata,
    _extract_archive,
    _common_input_path,
    run_raw_metadata_preflight,
    cleanup_download_lease,
    create_download_lease,
    discard_download_lease,
    evaluate_eligibility,
    evaluate_repository_execution_gate,
    plan_download_cleanup,
    split_unit_by_acquisition,
    finalize_download_lease,
    request_download_cleanup,
)


class RepositoryReanalysisTests(unittest.TestCase):
    def test_parse_workbench_tab_blocks(self) -> None:
        rows = _parse_tab_blocks("study_id\tST1\nanalysis_type\tLC-MS\n\nstudy_id\tST1\nion_mode\tNEGATIVE\n")
        self.assertEqual(
            rows,
            [
                {"study_id": "ST1", "analysis_type": "LC-MS"},
                {"study_id": "ST1", "ion_mode": "NEGATIVE"},
            ],
        )

    def test_parse_workbench_download_archive(self) -> None:
        files = _parse_workbench_downloads(
            '<li><a href="/studydownload/ST1.zip" download>ST1.zip</a> '
            '<b>(1.6G)</b>(Checksum:284ac9d191b5e43a4e47bfa1c88a31e4)</li>',
            "https://example.org",
        )
        self.assertEqual(files[0].size_bytes, int(1.6 * 1024**3))
        self.assertEqual(files[0].url, "https://example.org/studydownload/ST1.zip")

    def test_infer_acquisition(self) -> None:
        cases = [
            ("DDA-high res. LC-MS", "DDA"),
            ("SWATH data-independent acquisition", "DIA"),
            ("All-Ions fragmentation", "AIF"),
            ("MRM targeted panel", "MRM"),
            ("selected ion monitoring", "SIM"),
            ('"instrumentMode": "Scan"', "FullScan"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(_infer_acquisition(text), expected)

    def test_inference_accepts_repository_filename_separators(self) -> None:
        from msdial_app.repository_reanalysis import _infer_separation

        self.assertEqual("LC-MS", _infer_separation("a_MTBLS555_LC-MS_metabolite_profiling.txt"))
        self.assertEqual("GC-MS", _infer_separation("a_MTBLS527_GC-MS_mass_spectrometry.txt"))

    def test_ion_mode_inference_requires_measurement_context(self) -> None:
        self.assertEqual("Unknown", _infer_ion_mode("a positive correlation was observed"))
        self.assertEqual("Positive", _infer_ion_mode("ion_mode: POSITIVE"))
        self.assertEqual("Negative", _infer_ion_mode("negative ion mode ESI-"))

    def test_parse_apache_index(self) -> None:
        listing = (
            '<tr><td><a href="sample.mzML">sample.mzML</a></td>'
            '<td align="right">2026-01-01</td><td align="right"> 36M</td></tr>'
        )
        self.assertEqual(_parse_apache_index(listing)["sample.mzML"], 36 * 1024**2)

    def test_metabobank_prefers_original_vendor_folder_over_abf(self) -> None:
        sdrf = (
            "Sample Name\tRaw Data File\tRaw Data File\tFactor Value[Group]\n"
            "S1\traw/rawdata/S1.raw/\traw/abf/S1.abf\tControl\n"
        )
        filelist = (
            "Type\tName\tTime\tSize\tMD5\n"
            "raw\traw/rawdata/S1.raw/_HEADER.TXT\t2026-01-01T00:00:00Z\t12\t0123456789abcdef0123456789abcdef\n"
            "raw\traw/rawdata/S1.raw/_FUNC001.DAT\t2026-01-01T00:00:00Z\t34\tfedcba9876543210fedcba9876543210\n"
            "raw\traw/abf/S1.abf\t2026-01-01T00:00:00Z\t56\t11111111111111111111111111111111\n"
        )
        rows = _parse_metabobank_sdrf(sdrf)
        references = _metabobank_raw_references(rows)
        files, fallback = _metabobank_raw_files(
            _parse_metabobank_filelist(filelist), references, "https://example.org/MTBKS1/"
        )
        self.assertEqual(["raw/rawdata/S1.raw/"], references)
        self.assertFalse(fallback)
        self.assertEqual(2, len(files))
        self.assertTrue(all("raw/rawdata/S1.raw/" in item.name for item in files))

    def test_metabolights_assay_json_falls_back_to_derived_spectra(self) -> None:
        payload = {
            "data": {
                "rows": [
                    {
                        "Raw Spectral Data File": "",
                        "Derived Spectral Data File": "FILES/sample.mzML",
                    }
                ]
            }
        }
        self.assertEqual({"FILES/sample.mzML"}, _raw_names_from_assay(json.dumps(payload)))

    def test_eligibility_requires_lightweight_untargeted_scan_lcms(self) -> None:
        project = RepositoryProject(
            repository="test",
            accession="X1",
            separation="LC-MS",
            acquisition_mode="DDA",
            ion_mode="Positive",
            untargeted=True,
            sample_count=4,
            files=[RepositoryFile("a.mzML", 100, "https://example.org/a.mzML")],
            total_download_bytes=100,
        )
        result = evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
        self.assertTrue(result.eligible)
        self.assertEqual("eligible", result.selection_status)

    def test_unknown_lcms_acquisition_requires_raw_metadata(self) -> None:
        project = RepositoryProject(
            repository="test",
            accession="X2",
            separation="LC-MS",
            acquisition_mode="Unknown",
            untargeted=True,
            sample_count=2,
            files=[RepositoryFile("a.raw", 100, "https://example.org/a.raw")],
            total_download_bytes=100,
        )
        result = evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
        self.assertFalse(result.eligible)
        self.assertEqual("raw_metadata_required", result.selection_status)
        self.assertFalse(result.exclusion_reasons)

    def test_mixed_lcms_ion_mode_requires_split_or_raw_metadata(self) -> None:
        project = RepositoryProject(
            repository="test",
            accession="X2b",
            separation="LC-MS",
            acquisition_mode="DDA",
            ion_mode="Both",
            untargeted=True,
            sample_count=2,
            files=[RepositoryFile("positive.raw", 50, "https://example.org/data")],
            total_download_bytes=100,
        )
        result = evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
        self.assertFalse(result.eligible)
        self.assertEqual("raw_metadata_required", result.selection_status)
        self.assertIn("polarity-switching", " ".join(result.review_reasons))

    def test_explicit_targeted_project_is_excluded(self) -> None:
        project = RepositoryProject(
            repository="test",
            accession="X3",
            separation="LC-MS",
            acquisition_mode="DDA",
            untargeted=False,
            files=[RepositoryFile("a.raw", 100, "https://example.org/a.raw")],
            total_download_bytes=100,
        )
        result = evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
        self.assertEqual("excluded", result.selection_status)
        self.assertTrue(result.exclusion_reasons)

    def test_repository_campaign_accepts_dda_and_dia_but_excludes_gcms(self) -> None:
        lcms_projects = [
            RepositoryProject(
                repository="test",
                accession=mode,
                separation="LC-MS",
                acquisition_mode=mode,
                ion_mode="Negative",
                untargeted=True,
                files=[RepositoryFile("a.raw", 100, "https://example.org/a.raw")],
                total_download_bytes=100,
            )
            for mode in ("DDA", "DIA", "AIF", "SWATH")
        ]
        gcms = RepositoryProject(
            repository="test",
            accession="GC",
            separation="GC-MS",
            acquisition_mode="Scan",
            ion_mode="Positive",
            untargeted=True,
            files=[RepositoryFile("a.cdf", 100, "https://example.org/a.cdf")],
            total_download_bytes=100,
        )

        lcms_results = [
            evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
            for project in lcms_projects
        ]
        gc_result = evaluate_eligibility(gcms, EligibilityPolicy(max_download_bytes=1000))

        self.assertTrue(all(result.selection_status == "eligible" for result in lcms_results))
        self.assertEqual("excluded", gc_result.selection_status)
        self.assertIn("LC-MS data only", " ".join(gc_result.exclusion_reasons))

    def test_gc_sim_is_excluded(self) -> None:
        project = RepositoryProject(
            repository="test",
            accession="X3b",
            separation="GC-MS",
            acquisition_mode="SIM",
            untargeted=False,
            files=[RepositoryFile("a.qgd", 100, "https://example.org/a.qgd")],
            total_download_bytes=100,
        )
        result = evaluate_eligibility(project, EligibilityPolicy(max_download_bytes=1000))
        self.assertEqual("excluded", result.selection_status)

    def test_cleanup_refuses_before_validated_outputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "X1"
            raw = root / "raw"
            provenance = root / "provenance"
            raw.mkdir(parents=True)
            provenance.mkdir()
            manifest = provenance / "run-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "downloaded",
                        "cleanup_allowed": False,
                        "workspace": str(root),
                        "raw_directory": str(raw),
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaises(ValueError):
                cleanup_download_lease(manifest, confirmed=True)

    def test_archive_extraction_rejects_parent_traversal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "bad.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.txt", "unsafe")
            with self.assertRaises(ValueError):
                _extract_archive(archive, root / "data", 1024)

    def test_discard_removes_only_rejected_raw_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "X5"
            raw = root / "raw"
            output = root / "output"
            provenance = root / "provenance"
            for directory in (raw, output, provenance):
                directory.mkdir(parents=True)
            (raw / "bad.raw").write_text("x", encoding="ascii")
            manifest = provenance / "run-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "preflight_failed",
                        "workspace": str(root),
                        "raw_directory": str(raw),
                        "output_directory": str(output),
                    }
                ),
                encoding="utf-8",
            )
            result = discard_download_lease(manifest, confirmed=True)
            self.assertTrue(result["deleted"])
            self.assertFalse(raw.exists())
            self.assertTrue(manifest.exists())

    def test_finalize_unlocks_cleanup_after_mztab_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "X4"
            raw = root / "raw"
            output = root / "output"
            provenance = root / "provenance"
            for directory in (raw, output, provenance):
                directory.mkdir(parents=True)
            mztab = output / "result.mzTab"
            mztab.write_text(
                "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n",
                encoding="ascii",
            )
            mdpeak = output / "sample.mdpeak"
            mdpeak.write_text("Peak ID\n", encoding="ascii")
            arf = output / "result.arf2"
            arf.write_bytes(b"project")
            manifest = provenance / "run-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "prepared",
                        "workspace": str(root),
                        "raw_directory": str(raw),
                        "output_directory": str(output),
                        "cleanup_allowed": False,
                    }
                ),
                encoding="utf-8",
            )
            result = finalize_download_lease(manifest)
            self.assertEqual("mztab_validated", result["status"])
            self.assertTrue(result["cleanup_allowed"])
            self.assertIn(str(mztab.resolve()), result["retained_artifacts"])
            self.assertIn(str(mdpeak.resolve()), result["retained_artifacts"])
            archive = Path(result["project_archive"])
            self.assertTrue(archive.is_file())
            with zipfile.ZipFile(archive) as handle:
                self.assertIn("result.arf2", handle.namelist())
            self.assertTrue(result["retained_artifact_inventory"])

    def _validated_unit(self, root: Path) -> tuple[Path, Path]:
        """A unit whose run finished and validated, standing at the retention decision."""
        raw = root / "raw"
        output = root / "output"
        provenance = root / "provenance"
        for directory in (raw, output, provenance):
            directory.mkdir(parents=True)
        (raw / "sample.lcd").write_bytes(b"x" * 2048)
        (output / "result.mzTab").write_text(
            "MTD\tmzTab-version\t2.0.0-M\nSMH\tSML_ID\nSML\t1\n", encoding="ascii"
        )
        (output / "sample.mdpeak").write_text("Peak ID\n", encoding="ascii")
        manifest = provenance / "run-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "prepared",
                    "workspace": str(root),
                    "raw_directory": str(raw),
                    "output_directory": str(output),
                    "raw_retention_policy": "delete_after_validated_output",
                    "cleanup_allowed": False,
                }
            ),
            encoding="utf-8",
        )
        finalize_download_lease(manifest)
        return manifest, raw

    def test_requesting_cleanup_deletes_nothing_and_states_what_it_would_remove(self) -> None:
        # The retention policy chosen at download time records a wish. It is not an approval to delete,
        # and a background job holds none of what an informed approval needs.
        with tempfile.TemporaryDirectory() as temporary:
            manifest, raw = self._validated_unit(Path(temporary) / "repo" / "X5")

            pending = request_download_cleanup(manifest)

            self.assertFalse(pending["deleted"])
            self.assertTrue(pending["confirmation_required"])
            self.assertTrue(raw.is_dir(), "the raw tree must survive a mere request")
            self.assertEqual(
                "cleanup_pending_confirmation",
                json.loads(manifest.read_text(encoding="utf-8"))["status"],
            )
            # The three things a person needs in front of them before answering.
            self.assertEqual(str(raw.resolve()), pending["deletion_target"])
            self.assertEqual(1, pending["deletion_file_count"])
            self.assertEqual(2048, pending["deletion_bytes"])
            self.assertGreater(pending["retained_artifact_count"], 0)
            self.assertTrue(pending["ready_for_confirmation"])
            self.assertEqual([], pending["blockers"])

    def test_an_unconfirmed_cleanup_carries_the_same_inventory(self) -> None:
        # The preview used to return only a flag, so a caller had nothing to show. A confirmation given
        # without the target, the size and the retained artifacts is not an informed one.
        with tempfile.TemporaryDirectory() as temporary:
            manifest, raw = self._validated_unit(Path(temporary) / "repo" / "X6")

            preview = cleanup_download_lease(manifest, confirmed=False)

            self.assertFalse(preview["deleted"])
            self.assertTrue(raw.is_dir())
            self.assertEqual(str(raw.resolve()), preview["deletion_target"])
            self.assertEqual(2048, preview["deletion_bytes"])
            self.assertTrue(preview["retained_artifact_inventory"])

    def test_a_confirmed_cleanup_still_proceeds_after_a_request(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, raw = self._validated_unit(Path(temporary) / "repo" / "X7")
            request_download_cleanup(manifest)

            result = cleanup_download_lease(manifest, confirmed=True)

            self.assertTrue(result["deleted"])
            self.assertFalse(raw.exists())
            self.assertEqual(
                "raw_cleaned", json.loads(manifest.read_text(encoding="utf-8"))["status"]
            )

    def test_a_request_on_an_unvalidated_run_reports_blockers_and_stays_put(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "repo" / "X8"
            raw = root / "raw"
            provenance = root / "provenance"
            raw.mkdir(parents=True)
            provenance.mkdir()
            (raw / "sample.lcd").write_bytes(b"x")
            manifest = provenance / "run-manifest.json"
            manifest.write_text(
                json.dumps(
                    {
                        "status": "prepared",
                        "workspace": str(root),
                        "raw_directory": str(raw),
                        "output_directory": str(root / "output"),
                        "cleanup_allowed": False,
                    }
                ),
                encoding="utf-8",
            )

            pending = request_download_cleanup(manifest)

            self.assertFalse(pending["ready_for_confirmation"])
            self.assertTrue(pending["blockers"])
            self.assertEqual(
                "prepared",
                json.loads(manifest.read_text(encoding="utf-8"))["status"],
                "an unvalidated run must not be advanced to pending confirmation",
            )
            self.assertTrue(raw.is_dir())

    def _gate_workspace(self, root: Path, **manifest_overrides: object) -> tuple[Path, dict]:
        """A repository unit's manifest plus a workflow state that matches it."""
        output = root / "output"
        data = root / "raw" / "data"
        provenance = root / "provenance"
        for directory in (output, data, provenance):
            directory.mkdir(parents=True)
        sample = data / "sample.lcd"
        sample.write_bytes(b"x")
        manifest = provenance / "run-manifest.json"
        payload = {
            "status": "preflight_passed",
            "workspace": str(root),
            "output_directory": str(output),
            "input_candidates": [str(sample)],
            "execution_allowed": True,
            "project": {"analysis_unit_id": "unit-1", "ion_mode": "Negative"},
        }
        payload.update(manifest_overrides)
        manifest.write_text(json.dumps(payload), encoding="utf-8")
        state = {
            "repository_run_manifest": str(manifest),
            "output_root": str(output),
            "ion_mode": "Negative",
            "files": [{"file_path": str(sample)}],
        }
        return manifest, state

    def test_a_local_analysis_is_not_gated(self) -> None:
        # An ordinary local run carries no manifest, no eligibility verdict and nothing to gate.
        gate = evaluate_repository_execution_gate({"output_root": "C:/tmp", "files": []})
        self.assertFalse(gate["gated"])
        self.assertTrue(gate["allowed"])

    def test_an_eligible_unit_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(Path(temporary) / "repo" / "G1")
            gate = evaluate_repository_execution_gate(state)
            self.assertTrue(gate["gated"])
            self.assertTrue(gate["allowed"], gate["blockers"])
            self.assertEqual("unit-1", gate["analysis_unit_id"])

    def test_an_unresolved_unit_is_refused(self) -> None:
        # The state a unit sits in when its acquisition mode could not be established from repository
        # metadata and no raw-header preflight has settled it.
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(
                Path(temporary) / "repo" / "G2",
                execution_allowed=False,
                status="preflight_review_required",
            )
            gate = evaluate_repository_execution_gate(state)
            self.assertFalse(gate["allowed"])
            self.assertTrue(any("execution_allowed" in item for item in gate["blockers"]))

    def test_a_missing_manifest_is_refused_rather_than_ignored(self) -> None:
        gate = evaluate_repository_execution_gate(
            {"repository_run_manifest": "D:/nowhere/run-manifest.json", "files": []}
        )
        self.assertFalse(gate["allowed"])
        self.assertTrue(any("does not exist" in item for item in gate["blockers"]))

    def test_a_workflow_writing_outside_the_unit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(Path(temporary) / "repo" / "G3")
            state["output_root"] = str(Path(temporary) / "somewhere-else")
            gate = evaluate_repository_execution_gate(state)
            self.assertFalse(gate["allowed"])
            self.assertTrue(any("owns" in item for item in gate["blockers"]))

    def test_an_input_the_manifest_did_not_admit_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(Path(temporary) / "repo" / "G4")
            intruder = Path(temporary) / "elsewhere.lcd"
            intruder.write_bytes(b"x")
            state["files"].append({"file_path": str(intruder)})
            gate = evaluate_repository_execution_gate(state)
            self.assertFalse(gate["allowed"])
            self.assertTrue(
                any("not among the files" in item for item in gate["blockers"]), gate["blockers"]
            )

    def test_the_wrong_polarity_is_refused(self) -> None:
        # A run in the wrong polarity produces a complete, validated, entirely void result, and no other
        # stage flags it.
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(Path(temporary) / "repo" / "G5")
            state["ion_mode"] = "Positive"
            gate = evaluate_repository_execution_gate(state)
            self.assertFalse(gate["allowed"])
            self.assertTrue(any("ion mode" in item for item in gate["blockers"]))

    def test_an_undeclared_polarity_does_not_manufacture_a_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            _, state = self._gate_workspace(
                Path(temporary) / "repo" / "G6",
                project={"analysis_unit_id": "unit-1", "ion_mode": "Unknown"},
            )
            state["ion_mode"] = "Positive"
            gate = evaluate_repository_execution_gate(state)
            self.assertTrue(gate["allowed"], gate["blockers"])

    def test_raw_metadata_summary_maps_normalized_contract(self) -> None:
        records = [
            {
                "acquisition": {
                    "separation": {"value": "LiquidChromatography"},
                    "method": {"value": "DIA"},
                    "polarity": {"value": "Negative"},
                }
            }
        ]
        summary = _summarize_raw_metadata(records)
        self.assertEqual("LC-MS", summary["separation"])
        self.assertEqual("DIA", summary["acquisition_mode"])
        self.assertEqual("Negative", summary["ion_mode"])

    def test_common_input_path_uses_nested_archive_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            nested = root / "archive"
            nested.mkdir()
            first = nested / "a.lcd"
            second = nested / "b.lcd"
            first.write_text("", encoding="ascii")
            second.write_text("", encoding="ascii")
            self.assertEqual(str(nested.resolve()), _common_input_path([str(first), str(second)], root))

    def test_download_lease_reports_byte_progress_for_one_large_object(self) -> None:
        class FakeClient:
            def download(self, _url, destination, _maximum_bytes, progress_callback=None):
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"12345678")
                if progress_callback:
                    progress_callback(4, 8)
                    progress_callback(8, 8)
                return {
                    "path": str(destination),
                    "size_bytes": 8,
                    "sha256": "",
                    "md5": "",
                }

        project = RepositoryProject(
            repository="test",
            accession="X6",
            eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("sample.mzML", 8, "https://example.org/sample.mzML")],
            total_download_bytes=8,
        )
        updates = []
        with tempfile.TemporaryDirectory() as temporary:
            result = create_download_lease(
                project,
                Path(temporary),
                100,
                client=FakeClient(),
                progress_callback=lambda *args: updates.append(args),
            )
            self.assertEqual(8, updates[-1][3])
            self.assertEqual(8, updates[-1][4])
            self.assertEqual(1, len(result["input_candidates"]))

    def test_download_lease_uses_bundle_size_for_safety_limit(self) -> None:
        project = RepositoryProject(
            repository="mb_post",
            accession="MPST-BUNDLE",
            eligible=True,
            selection_status="eligible",
            files=[RepositoryFile("sample.mzML", 8, "https://example.org/bundle.tar")],
            total_download_bytes=8,
            download_scope={"bundle_bytes": 200},
        )
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "Required repository bundle"):
                create_download_lease(project, Path(temporary), 100)


if __name__ == "__main__":
    unittest.main()


class RawMetadataPreflightFormatTests(unittest.TestCase):
    """CLAUDE-C03: a format that can never be checked is not the same as a check that failed."""

    def _workspace(self, root: Path, *, execution_allowed: bool) -> tuple[Path, Path]:
        data = root / "raw" / "data"
        provenance = root / "provenance"
        for directory in (data, provenance):
            directory.mkdir(parents=True)
        sample = data / "0555_1_neg.lcd"
        sample.write_bytes(b"x")
        manifest = provenance / "run-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "downloaded",
                    "workspace": str(root),
                    "input_candidates": [str(sample)],
                    "execution_allowed": execution_allowed,
                    "project": {"analysis_unit_id": "unit-1", "eligible": execution_allowed},
                }
            ),
            encoding="utf-8",
        )
        extractor = root / "RawMetadataConsoleApp.exe"
        extractor.write_bytes(b"stub")
        return manifest, extractor

    @staticmethod
    def _completed(returncode: int, stderr: str = ""):
        from subprocess import CompletedProcess

        return CompletedProcess(args=["stub"], returncode=returncode, stdout="", stderr=stderr)

    def test_an_unsupported_format_is_reported_as_its_own_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor = self._workspace(Path(temporary) / "u1", execution_allowed=False)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run",
                return_value=self._completed(
                    82, "unsupported format: .lcd (Shimadzu) has no raw metadata reader: x"
                ),
            ):
                result = run_raw_metadata_preflight(manifest, extractor)

        self.assertEqual("preflight_unsupported_format", result["status"])
        self.assertEqual([".lcd"], result["raw_metadata_preflight"]["unsupported_formats"])
        self.assertIn("no metadata reader", result["raw_metadata_preflight"]["advisory"])
        self.assertEqual(1, len(result["raw_metadata_preflight"]["detail"]))

    def test_an_unsupported_format_cannot_promote_an_ineligible_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor = self._workspace(Path(temporary) / "u2", execution_allowed=False)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run",
                return_value=self._completed(82),
            ):
                result = run_raw_metadata_preflight(manifest, extractor)

        self.assertFalse(result["execution_allowed"])

    def test_an_unsupported_format_does_not_revoke_an_already_eligible_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor = self._workspace(Path(temporary) / "u3", execution_allowed=True)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run",
                return_value=self._completed(82),
            ):
                result = run_raw_metadata_preflight(manifest, extractor)

        self.assertTrue(result["execution_allowed"])
        self.assertEqual("preflight_unsupported_format", result["status"])

    def test_any_other_failure_is_still_an_unavailable_preflight(self) -> None:
        # A crash, a missing dependency or a timeout may work on a retry; an absent reader
        # never will, and only the second is worth telling an agent to stop trying.
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor = self._workspace(Path(temporary) / "u4", execution_allowed=False)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run",
                return_value=self._completed(1, "System.NullReferenceException"),
            ):
                result = run_raw_metadata_preflight(manifest, extractor)

        self.assertEqual("preflight_unavailable", result["status"])
        self.assertNotIn("unsupported_formats", result["raw_metadata_preflight"])
        self.assertFalse(result["execution_allowed"])

    def test_the_manifest_records_which_extractor_produced_the_verdict(self) -> None:
        # A verdict that does not say which binary produced it cannot be tied to a build,
        # and a stale executable earlier on the search order looks identical to the fix.
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor = self._workspace(Path(temporary) / "u5", execution_allowed=False)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run",
                return_value=self._completed(82),
            ):
                result = run_raw_metadata_preflight(manifest, extractor)
            expected_path = str(extractor.resolve())
            expected_size = extractor.stat().st_size

        recorded = result["raw_metadata_preflight"]["extractor"]
        self.assertEqual(expected_path, recorded["path"])
        self.assertEqual(expected_size, recorded["size_bytes"])
        self.assertTrue(recorded["modified_at"])


class _MixedUnitFixture:
    """A repository unit on disk plus a stand-in raw-header extractor, for the tests below."""

    MODES = {"a_DDA_1.mzML": "DDA", "b_DIA_1.mzML": "DIA", "c_DDA_2.mzML": "DDA", "d_DIA_2.mzML": "DIA"}

    def _workspace(self, root: Path, modes: dict[str, str]) -> tuple[Path, Path, list[Path]]:
        data = root / "raw" / "data"
        provenance = root / "provenance"
        output = root / "output"
        for directory in (data, provenance, output):
            directory.mkdir(parents=True)
        files = []
        for name in modes:
            path = data / name
            path.write_bytes(b"x")
            files.append(path)
        manifest = provenance / "run-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "downloaded",
                    "workspace": str(root),
                    "raw_directory": str(root / "raw"),
                    "input_directory": str(data),
                    "output_directory": str(output),
                    "input_candidates": [str(path) for path in files],
                    "execution_allowed": False,
                    "project": {
                        "repository": "metabolights",
                        "accession": "MTBLS-MIXED",
                        "analysis_unit_id": "unit-mixed",
                        "separation": "LC-MS",
                        "acquisition_mode": "DIA",
                        "ion_mode": "Negative",
                        "files": [
                            {"name": f"FILES/{path.name}", "size_bytes": 1, "url": "", "role": "converted"}
                            for path in files
                        ],
                        "total_download_bytes": len(files),
                        "sample_count": len(files),
                        "sample_metadata": [
                            {"sample_id": path.stem, "raw_file": f"FILES/{path.name}", "values": {}}
                            for path in files
                        ],
                        "class_proposal": {
                            "proposal_id": "p1",
                            "status": "accepted",
                            "selected_fields": ["origin"],
                            "assignments": [
                                {"sample_id": path.stem, "class_label": "Bio" if index % 3 == 0 else "Chem"}
                                for index, path in enumerate(files)
                            ],
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        extractor = root / "RawMetadataConsoleApp.exe"
        extractor.write_bytes(b"stub")
        return manifest, extractor, files

    def _extractor(self, modes: dict[str, str], levels: dict[str, list[int]] | None = None):
        """A stand-in for the extractor: writes one header record per --input it is given."""
        from subprocess import CompletedProcess

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
                        "msLevels": (levels or {}).get(Path(path).name, [1, 2]),
                    },
                }
                for path in inputs
            ]
            output.write_text(json.dumps(records), encoding="utf-8")
            return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        return run

    def _gate_state(self, manifest: Path, files: list[Path], types: dict[str, str]) -> dict:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        entries = []
        for path in files:
            entry = {"file_path": str(path)}
            if path.name in types:
                entry["acquisition_type"] = types[path.name]
            entries.append(entry)
        return {
            "repository_run_manifest": str(manifest),
            "output_root": payload["output_directory"],
            "ion_mode": "Negative",
            "files": entries,
        }


class MixedAcquisitionPreflightTests(_MixedUnitFixture, unittest.TestCase):
    """A unit whose headers disagree about acquisition mode is Mixed, not Unknown.

    MetaboLights MTBLS2207: repository metadata said DIA with no evidence behind it, eleven headers
    said six DDA and five DIA, and the preflight answered "Unknown", which the caller reads as "keep
    what the repository said". Confirming untargeted status would then have made the unit eligible as
    DIA, and the six DDA files would have been deconvoluted as SWATH.
    """

    def test_disagreeing_headers_are_mixed_and_replace_the_repository_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "u1", self.MODES)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
            ):
                result = run_raw_metadata_preflight(manifest, extractor)

        self.assertEqual("Mixed", result["raw_metadata_preflight"]["summary"]["acquisition_mode"])
        self.assertEqual("Mixed", result["project"]["acquisition_mode"])
        self.assertEqual("preflight_mixed_acquisition", result["status"])
        groups = result["raw_metadata_preflight"]["acquisition_groups"]
        self.assertEqual({"DDA": 2, "DIA": 2}, {mode: len(files) for mode, files in groups.items()})
        self.assertIn("Split", result["raw_metadata_preflight"]["advisory"])
        self.assertEqual(4, len(result["raw_metadata_preflight"]["summary"]["per_file"]))

    def test_confirming_untargeted_cannot_make_a_mixed_unit_eligible(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "u2", self.MODES)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
            ):
                result = run_raw_metadata_preflight(manifest, extractor, confirm_untargeted=True)

        self.assertFalse(result["execution_allowed"])
        self.assertFalse(result["project"]["eligible"])
        self.assertTrue(any("split" in reason for reason in result["project"]["review_reasons"]))

    def test_every_candidate_is_inspected_by_default(self) -> None:
        # A unit whose first three files are all DDA used to be called DDA from a sample of three.
        modes = {f"s{index:02d}_DDA.mzML": "DDA" for index in range(3)}
        modes["s99_DIA.mzML"] = "DIA"
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "u3", modes)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(modes)
            ):
                result = run_raw_metadata_preflight(manifest, extractor, confirm_untargeted=True)

        summary = result["raw_metadata_preflight"]["summary"]
        self.assertEqual(4, summary["files_inspected"])
        self.assertTrue(summary["coverage"]["complete"])
        self.assertEqual("Mixed", summary["acquisition_mode"])

    def test_a_capped_inspection_leaves_the_unit_under_review(self) -> None:
        modes = {f"s{index:02d}_DDA.mzML": "DDA" for index in range(4)}
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "u4", modes)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(modes)
            ):
                result = run_raw_metadata_preflight(
                    manifest, extractor, max_inputs=3, confirm_untargeted=True
                )

        self.assertEqual("DDA", result["project"]["acquisition_mode"])
        self.assertFalse(result["raw_metadata_preflight"]["summary"]["coverage"]["complete"])
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(any("3 of 4" in reason for reason in result["project"]["review_reasons"]))

    def test_a_uniform_unit_inspected_in_full_still_passes(self) -> None:
        modes = {f"s{index:02d}_DDA.mzML": "DDA" for index in range(4)}
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "u5", modes)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(modes)
            ):
                result = run_raw_metadata_preflight(manifest, extractor, confirm_untargeted=True)

        self.assertEqual("preflight_passed", result["status"])
        self.assertTrue(result["execution_allowed"])
        self.assertEqual("DDA", result["project"]["acquisition_mode"])

    def test_the_gate_refuses_a_mixed_unit_even_if_execution_was_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, files = self._workspace(Path(temporary) / "g1", self.MODES)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
            ):
                run_raw_metadata_preflight(manifest, extractor, confirm_untargeted=True)
            # The flag is set by hand; the headers still disagree.
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["execution_allowed"] = True
            manifest.write_text(json.dumps(payload), encoding="utf-8")
            gate = evaluate_repository_execution_gate(
                self._gate_state(manifest, files, {name: "SWATH" for name in self.MODES})
            )

        self.assertFalse(gate["allowed"])
        self.assertTrue(any("more than one acquisition mode" in item for item in gate["blockers"]))
        self.assertTrue(
            any("header DDA, run as SWATH" in item for item in gate["blockers"]), gate["blockers"]
        )

    def test_the_gate_refuses_a_file_run_against_its_own_header(self) -> None:
        # A unit split and preflighted as DIA, run with no acquisition type written against its
        # files: MS-DIAL would read each of them as DDA.
        modes = {f"s{index:02d}_DIA.mzML": "DIA" for index in range(3)}
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, files = self._workspace(Path(temporary) / "g2", modes)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(modes)
            ):
                run_raw_metadata_preflight(manifest, extractor, confirm_untargeted=True)
            unstamped = evaluate_repository_execution_gate(self._gate_state(manifest, files, {}))
            stamped = evaluate_repository_execution_gate(
                self._gate_state(manifest, files, {name: "SWATH" for name in modes})
            )

        self.assertFalse(unstamped["allowed"])
        self.assertTrue(any("header DIA, run as DDA" in item for item in unstamped["blockers"]))
        self.assertTrue(stamped["allowed"], stamped["blockers"])


class AcquisitionSplitTests(_MixedUnitFixture, unittest.TestCase):
    """A Mixed unit is split into one part per header-read acquisition mode."""

    def _mixed(self, root: Path, levels: dict[str, list[int]] | None = None):
        manifest, extractor, files = self._workspace(root, self.MODES)
        with patch(
            "msdial_app.repository_reanalysis.subprocess.run",
            side_effect=self._extractor(self.MODES, levels),
        ):
            run_raw_metadata_preflight(manifest, extractor)
        return manifest, extractor, files

    def test_split_preview_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self._mixed(Path(temporary) / "unit")
            before = manifest.read_text(encoding="utf-8")
            plan = split_unit_by_acquisition(manifest, confirmed=False)
            after = manifest.read_text(encoding="utf-8")
            siblings = sorted(path.name for path in (Path(temporary)).iterdir())

        self.assertFalse(plan["written"])
        self.assertEqual([], plan["blockers"])
        self.assertEqual(["DDA", "DIA"], [part["acquisition_mode"] for part in plan["parts"]])
        self.assertEqual(before, after)
        self.assertEqual(["unit"], siblings)

    def test_split_writes_one_manifest_per_mode_admitting_only_its_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self._mixed(Path(temporary) / "unit")
            result = split_unit_by_acquisition(manifest, confirmed=True)
            parts = {
                part["acquisition_mode"]: json.loads(Path(part["manifest_path"]).read_text(encoding="utf-8"))
                for part in result["parts"]
            }
            parent = json.loads(manifest.read_text(encoding="utf-8"))
            dda_root = Path(result["parts"][0]["workspace"])
            has_own_raw = (dda_root / "raw").exists()

        self.assertTrue(result["written"])
        self.assertEqual({"a_DDA_1.mzML", "c_DDA_2.mzML"}, {Path(p).name for p in parts["DDA"]["input_candidates"]})
        self.assertEqual({"b_DIA_1.mzML", "d_DIA_2.mzML"}, {Path(p).name for p in parts["DIA"]["input_candidates"]})
        self.assertEqual("DDA", parts["DDA"]["project"]["acquisition_mode"])
        self.assertEqual("unit-mixed-dda", parts["DDA"]["project"]["analysis_unit_id"])
        self.assertFalse(parts["DDA"]["execution_allowed"])
        self.assertEqual(
            ["a_DDA_1", "c_DDA_2"],
            [item["sample_id"] for item in parts["DDA"]["project"]["class_proposal"]["assignments"]],
        )
        self.assertEqual(2, len(parts["DDA"]["project"]["sample_metadata"]))
        self.assertEqual("split_by_acquisition", parent["status"])
        self.assertFalse(parent["execution_allowed"])
        self.assertEqual(2, len(parent["split_into"]))
        self.assertFalse(has_own_raw, "a part must read the parent's raw data, not copy it")

    def test_split_parts_do_not_inherit_the_parents_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self._mixed(Path(temporary) / "unit")
            result = split_unit_by_acquisition(manifest, confirmed=True)
            part = json.loads(Path(result["parts"][0]["manifest_path"]).read_text(encoding="utf-8"))

        reasons = part["project"]["review_reasons"]
        self.assertFalse(any("more than one acquisition mode" in item for item in reasons), reasons)
        self.assertTrue(any("on its own" in item for item in reasons), reasons)
        self.assertFalse(part["project"]["eligible"])

    def test_split_is_refused_for_a_unit_that_is_not_mixed(self) -> None:
        modes = {f"s{index:02d}_DDA.mzML": "DDA" for index in range(3)}
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "unit", modes)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(modes)
            ):
                run_raw_metadata_preflight(manifest, extractor)
            result = split_unit_by_acquisition(manifest, confirmed=True)

        self.assertFalse(result["written"])
        self.assertTrue(any("Only a unit" in item for item in result["blockers"]))

    def test_split_is_refused_when_some_files_were_never_read(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, _ = self._workspace(Path(temporary) / "unit", self.MODES)
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
            ):
                run_raw_metadata_preflight(manifest, extractor, max_inputs=2)
            result = split_unit_by_acquisition(manifest, confirmed=True)

        self.assertFalse(result["written"])
        self.assertTrue(any("did not read every input file" in item for item in result["blockers"]))

    def test_split_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self._mixed(Path(temporary) / "unit")
            first = split_unit_by_acquisition(manifest, confirmed=True)
            second = split_unit_by_acquisition(manifest, confirmed=True)

        self.assertTrue(second["already_split"])
        self.assertFalse(second["written"])
        self.assertEqual(
            [part["manifest_path"] for part in first["parts"]],
            [part["manifest_path"] for part in second["parts"]],
        )

    def test_split_flags_ms_levels_beyond_ms2(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, _, _ = self._mixed(Path(temporary) / "unit", levels={"c_DDA_2.mzML": [1, 2, 3]})
            result = split_unit_by_acquisition(manifest, confirmed=True)
            dda = json.loads(Path(result["parts"][0]["manifest_path"]).read_text(encoding="utf-8"))
            dia = json.loads(Path(result["parts"][1]["manifest_path"]).read_text(encoding="utf-8"))

        self.assertEqual([3], result["parts"][0]["higher_ms_levels"])
        self.assertTrue(any("MS level(s) [3]" in item for item in dda["project"]["warnings"]))
        self.assertFalse(any("MS level" in item for item in dia["project"]["warnings"]))

    def test_a_part_preflights_and_runs_only_as_its_own_mode(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, files = self._mixed(Path(temporary) / "unit")
            result = split_unit_by_acquisition(manifest, confirmed=True)
            dia_part = Path(result["parts"][1]["manifest_path"])
            with patch(
                "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
            ):
                preflight = run_raw_metadata_preflight(dia_part, extractor, confirm_untargeted=True)
            dia_files = [path for path in files if "DIA" in path.name]
            dda_file = next(path for path in files if "DDA" in path.name)
            cleared = evaluate_repository_execution_gate(
                self._gate_state(dia_part, dia_files, {path.name: "SWATH" for path in dia_files})
            )
            intruding = evaluate_repository_execution_gate(
                self._gate_state(
                    dia_part, dia_files + [dda_file], {path.name: "SWATH" for path in dia_files + [dda_file]}
                )
            )
            cleanup = plan_download_cleanup(dia_part)

        self.assertEqual("preflight_passed", preflight["status"])
        self.assertEqual("DIA", preflight["project"]["acquisition_mode"])
        self.assertTrue(cleared["allowed"], cleared["blockers"])
        self.assertFalse(intruding["allowed"])
        self.assertTrue(any("not among the files" in item for item in intruding["blockers"]))
        self.assertTrue(
            any("not the expected 'raw' folder" in item for item in cleanup["blockers"]),
            "a part must never delete the raw data it shares with its parent",
        )


class SplitPartJobTests(_MixedUnitFixture, unittest.TestCase):
    """A part is reachable by the tools that take a download job id, and stays reachable."""

    def _split_through_server(self, root: Path, jobs: dict) -> tuple[dict, str]:
        from msdial_app import server

        manifest, extractor, _ = self._workspace(root, self.MODES)
        with patch(
            "msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)
        ):
            run_raw_metadata_preflight(manifest, extractor)
        jobs["parent"] = {
            "id": "parent",
            "kind": "repository_download",
            "status": "completed",
            "repository": "metabolights",
            "accession": "MTBLS-MIXED",
            "result": {"manifest_path": str(manifest)},
        }
        with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None):
            result = server._split_repository_download({"download_job_id": "parent", "confirmed": True})
        return result, str(manifest)

    def test_each_part_gets_a_completed_job_naming_its_manifest_and_parent(self) -> None:
        jobs: dict = {}
        with tempfile.TemporaryDirectory() as temporary:
            result, _ = self._split_through_server(Path(temporary) / "unit", jobs)
            recorded = [
                json.loads(Path(part["manifest_path"]).read_text(encoding="utf-8"))["job_id"]
                for part in result["parts"]
            ]

        part_jobs = [jobs[part["job_id"]] for part in result["parts"]]
        self.assertEqual({"repository_split_part"}, {job["kind"] for job in part_jobs})
        self.assertEqual({"completed"}, {job["status"] for job in part_jobs})
        self.assertEqual({"parent"}, {job["result"]["split_from_job_id"] for job in part_jobs})
        self.assertEqual([2, 2], [len(job["result"]["recognized"]["files"]) for job in part_jobs])
        self.assertEqual([part["job_id"] for part in result["parts"]], recorded)

    def test_an_evicted_part_job_is_re_registered_under_the_same_id(self) -> None:
        from msdial_app import server

        jobs: dict = {}
        with tempfile.TemporaryDirectory() as temporary:
            first, _ = self._split_through_server(Path(temporary) / "unit", jobs)
            evicted = first["parts"][0]["job_id"]
            del jobs[evicted]
            with patch.object(server, "JOBS", jobs), patch.object(server, "_persist_jobs_locked", lambda: None):
                again = server._split_repository_download({"download_job_id": "parent", "confirmed": True})

        self.assertTrue(again["already_split"])
        self.assertEqual(evicted, again["parts"][0]["job_id"])
        self.assertIn(evicted, jobs)

    def test_only_a_download_job_can_be_split(self) -> None:
        from msdial_app import server

        jobs = {"x": {"id": "x", "kind": "repository_split_part", "status": "completed", "result": {}}}
        with patch.object(server, "JOBS", jobs):
            with self.assertRaisesRegex(ValueError, "not a repository download job"):
                server._split_repository_download({"download_job_id": "x", "confirmed": True})
