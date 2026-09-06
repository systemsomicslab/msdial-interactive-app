from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

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
    cleanup_download_lease,
    create_download_lease,
    discard_download_lease,
    evaluate_eligibility,
    evaluate_repository_execution_gate,
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
