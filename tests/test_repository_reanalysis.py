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
    discard_download_lease,
    evaluate_eligibility,
    finalize_download_lease,
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


if __name__ == "__main__":
    unittest.main()
