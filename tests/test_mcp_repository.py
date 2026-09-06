from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server


class McpRepositoryToolsTests(unittest.TestCase):
    @staticmethod
    def _unit_handoff(unit_id: str = "unit-neg") -> dict:
        return {
            "schema": "msdial-repository-reanalysis-handoff.v1",
            "repository": "mb_post",
            "accession": "MPST-MIXED",
            "analysis_unit_id": unit_id,
            "source_subrecord_id": "lcms-neg-dda",
            "technical_settings": {
                "separation": "LC-MS",
                "ion_mode": "Negative",
                "acquisition_mode": "DDA",
                "target_omics": "Lipidomics",
                "untargeted": True,
            },
            "repository_url": "https://example.org/MPST-MIXED",
            "files": [
                {
                    "path": "FILES/sample_neg.raw",
                    "role": "raw",
                    "size_bytes": 1024,
                    "checksum": "abc",
                    "download_url": "https://example.org/MPST-MIXED.tar",
                }
            ],
            "sample_metadata": [
                {
                    "sample_id": "sample_neg",
                    "raw_file": "sample_neg.raw",
                    "attributes": {"Condition": "control"},
                }
            ],
            "class_proposal": {"proposal_id": "class-1", "assignments": []},
            "blocking_reasons": [],
            "download_scope": {
                "kind": "accession_bundle_with_file_allowlist",
                "allowlist_required": True,
                "file_count": 1,
                "analysis_file_count": 1,
                "bundle_bytes": 2048,
            },
            "sample_count": 1,
            "analytical_sample_count": 1,
        }

    def test_analysis_unit_handoff_scopes_plan_without_repository_inspection(self) -> None:
        handoff = self._unit_handoff()
        with patch.object(mcp_server, "_request_json") as request:
            result = mcp_server.msdial_repository_reanalysis_plan(
                "mb_post",
                "MPST-MIXED",
                "D:/repository",
                analysis_unit_handoff=handoff,
                analysis_purpose="Compare biological groups and produce mzTab-M.",
            )

        request.assert_not_called()
        self.assertEqual("unit-neg", result["project"]["analysis_unit_id"])
        self.assertEqual(1, result["project"]["file_count"])
        self.assertEqual("Negative", result["project"]["ion_mode"])
        self.assertEqual(2048, result["download"]["required_download_bytes"])
        self.assertIn("DDA", result["execution_scope"]["acquisition_modes"])
        self.assertIn("DIA", result["execution_scope"]["acquisition_modes"])
        self.assertEqual("LC-MS/MS", result["execution_scope"]["project_type"])
        self.assertTrue(result["analysis_intent"]["confirmed"])

    def test_handoff_uses_common_attributes_and_rejects_truncation(self) -> None:
        handoff = self._unit_handoff()
        handoff["unit_attributes"] = {"Organism": "Homo sapiens"}
        project, workspace = mcp_server._project_from_analysis_unit_handoff(handoff)
        self.assertEqual("Homo sapiens", project["sample_metadata"][0]["values"]["Organism"])
        self.assertEqual(1, len(workspace["rows"]))

        handoff["sample_count"] = 2
        with self.assertRaisesRegex(ValueError, "declares 2 sample rows"):
            mcp_server._project_from_analysis_unit_handoff(handoff)

    def test_handoff_eligibility_does_not_treat_integer_zero_as_unknown(self) -> None:
        handoff = self._unit_handoff()
        handoff["technical_settings"]["untargeted"] = 0
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)
        self.assertFalse(project["eligible"])
        self.assertEqual("excluded", project["selection_status"])
        self.assertTrue(any("targeted" in item for item in project["exclusion_reasons"]))

    def test_handoff_path_and_workspace_root_validation(self) -> None:
        handoff = self._unit_handoff()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "handoff.json"
            path.write_text(json.dumps(handoff), encoding="utf-8")
            result = mcp_server.msdial_repository_reanalysis_plan(
                "mb_post",
                "MPST-MIXED",
                str(Path(directory) / "workspace"),
                analysis_unit_handoff_path=str(path),
            )
        self.assertEqual("unit-neg", result["project"]["analysis_unit_id"])
        invalid = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post", "MPST-MIXED", "", analysis_unit_handoff=handoff
        )
        self.assertFalse(invalid["ok"])
        self.assertEqual("validation_error", invalid["reason"])
        self.assertIn("workspace root", invalid["detail"])

    def test_configured_repository_workspace_boundary_is_enforced(self) -> None:
        handoff = self._unit_handoff()
        with tempfile.TemporaryDirectory() as directory:
            boundary = Path(directory) / "analysis"
            outside = Path(directory) / "outside"
            with patch.dict(
                os.environ,
                {"MSDIAL_REPOSITORY_WORKSPACE_ROOT": str(boundary)},
            ):
                accepted = mcp_server.msdial_repository_reanalysis_plan(
                    "mb_post",
                    "MPST-MIXED",
                    str(boundary / "project"),
                    analysis_unit_handoff=handoff,
                )
                rejected = mcp_server.msdial_repository_reanalysis_plan(
                    "mb_post",
                    "MPST-MIXED",
                    str(outside),
                    analysis_unit_handoff=handoff,
                )

        self.assertEqual("unit-neg", accepted["project"]["analysis_unit_id"])
        self.assertFalse(rejected["ok"])
        self.assertIn("configured boundary", rejected["detail"])

    def test_batch_plan_keeps_analysis_units_independent(self) -> None:
        first = self._unit_handoff("unit-neg")
        second = self._unit_handoff("unit-pos")
        second["technical_settings"] = {
            **second["technical_settings"],
            "ion_mode": "Positive",
        }
        result = mcp_server.msdial_repository_batch_plan(
            [first, second],
            "D:/repository",
            analysis_purpose="Compare positive and negative analysis-unit outcomes separately.",
        )

        self.assertEqual(2, result["run_count"])
        self.assertEqual(2, result["ready_count"])
        self.assertNotEqual(result["runs"][0]["workspace"], result["runs"][1]["workspace"])
        self.assertEqual("Negative", result["runs"][0]["project"]["ion_mode"])
        self.assertEqual("Positive", result["runs"][1]["project"]["ion_mode"])

    def test_batch_plan_propagates_missing_class_decision(self) -> None:
        handoff = self._unit_handoff()
        handoff["class_proposal"] = None
        handoff["blocking_reasons"] = ["class_proposal:missing"]

        result = mcp_server.msdial_repository_batch_plan(
            [handoff], "D:/repository", analysis_purpose="Compare biological Classes."
        )

        self.assertEqual(0, result["ready_count"])
        self.assertFalse(result["runs"][0]["ready"])
        self.assertIn("class_proposal:missing", result["runs"][0]["blocking_reasons"])
        self.assertEqual(["class_proposal"], result["runs"][0]["pending_decisions"])

        preview = mcp_server.msdial_download_repository_raw(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            analysis_unit_handoff=handoff,
            confirmed=False,
            analysis_purpose="Compare biological Classes.",
        )
        self.assertTrue(preview["blocked"])
        self.assertIn("class_proposal:missing", preview["preview"]["blocking_reasons"])

    def test_batch_plan_returns_structured_handoff_validation_error(self) -> None:
        handoff = self._unit_handoff()
        handoff["sample_count"] = 2

        result = mcp_server.msdial_repository_batch_plan([handoff], "D:/repository")

        self.assertFalse(result["ok"])
        self.assertEqual("validation_error", result["reason"])
        self.assertIn("declares 2 sample rows", result["detail"])

    def test_analysis_purpose_is_a_download_readiness_decision(self) -> None:
        handoff = self._unit_handoff()

        plan = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post", "MPST-MIXED", "D:/repository", analysis_unit_handoff=handoff
        )
        batch = mcp_server.msdial_repository_batch_plan([handoff], "D:/repository")
        preview = mcp_server.msdial_download_repository_raw(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            analysis_unit_handoff=handoff,
            confirmed=False,
        )

        self.assertIn("analysis_purpose:missing", plan["download"]["blocking_reasons"])
        self.assertEqual(["analysis_purpose"], plan["analysis_intent"]["pending_decisions"])
        self.assertIn("analysis_purpose", batch["runs"][0]["pending_decisions"])
        self.assertTrue(preview["blocked"])

    def test_batch_plan_reports_bundle_size_as_a_blocking_reason(self) -> None:
        handoff = self._unit_handoff()
        handoff["download_scope"]["bundle_bytes"] = 3 * 1024**3

        result = mcp_server.msdial_repository_batch_plan(
            [handoff],
            "D:/repository",
            maximum_gb_per_unit=1,
            analysis_purpose="Compare biological Classes.",
        )

        self.assertFalse(result["runs"][0]["ready"])
        self.assertFalse(result["runs"][0]["within_size_limit"])
        self.assertIn("size_limit:exceeded", result["runs"][0]["blocking_reasons"])

    def test_handoff_publication_status_is_preserved(self) -> None:
        handoff = self._unit_handoff()
        handoff["publication_status"] = "none_recorded"
        result = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post", "MPST-MIXED", "D:/repository", analysis_unit_handoff=handoff
        )
        self.assertEqual("none_recorded", result["project"]["publication_status"])

    def test_analysis_unit_allowlist_rejects_sibling_inputs(self) -> None:
        from msdial_app.repository_reanalysis import (
            RepositoryFile,
            RepositoryProject,
            _filter_inputs_by_project_allowlist,
            _path_matches_allowlist,
            _verify_project_allowlist_checksums,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            selected = root / "FILES" / "sample_neg.raw"
            sibling = root / "FILES" / "sample_pos.raw"
            selected.parent.mkdir()
            selected.write_bytes(b"selected")
            sibling.write_bytes(b"sibling")
            project = RepositoryProject(
                repository="mb_post",
                accession="MPST-MIXED",
                analysis_unit_id="unit-neg",
                files=[
                    RepositoryFile(
                        name="FILES/sample_neg.raw",
                        size_bytes=8,
                        url="https://example.org/bundle.tar",
                        checksum=hashlib.md5(b"selected").hexdigest(),
                    )
                ],
            )
            result = _filter_inputs_by_project_allowlist(
                [str(selected), str(sibling)], root, project
            )

            self.assertEqual([str(selected)], result)
            validation = _verify_project_allowlist_checksums(root, project)
            self.assertEqual(1, validation["verified"])

            spoof = root / "deep" / "unexpected" / "sample_neg.raw"
            spoof.parent.mkdir(parents=True)
            spoof.write_bytes(b"selected")
            self.assertFalse(
                _path_matches_allowlist(spoof, root, ["sample_neg.raw"])
            )

    def test_analysis_allowlist_excludes_sidecars_and_alternate_raw_files(self) -> None:
        from msdial_app.repository_reanalysis import (
            RepositoryFile,
            RepositoryProject,
            _filter_inputs_by_project_allowlist,
        )

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            primary = root / "sample.wiff"
            sidecar = root / "sample.wiff.scan"
            alternate = root / "sample.wiff2"
            for path in (primary, sidecar, alternate):
                path.write_bytes(b"x")
            project = RepositoryProject(
                repository="mb_post",
                accession="MPST-MIXED",
                analysis_unit_id="unit-neg",
                files=[
                    RepositoryFile("sample.wiff", 1, "https://x", role="raw"),
                    RepositoryFile("sample.wiff.scan", 1, "https://x", role="sidecar"),
                    RepositoryFile("sample.wiff2", 1, "https://x", role="raw_alternate"),
                ],
            )
            selected = _filter_inputs_by_project_allowlist(
                [str(primary), str(sidecar), str(alternate)], root, project
            )
            self.assertEqual([str(primary)], selected)

    def test_accession_scope_reports_mixed_technical_metadata(self) -> None:
        inspection = {
            "project": {
                "repository": "mb_post",
                "accession": "MPST-MIXED",
                "files": [{"name": "bundle.tar", "size_bytes": 100, "url": "https://x"}],
                "total_download_bytes": 100,
                "sample_count": 2,
                "separation": "LC-MS",
                "acquisition_mode": "DDA",
                "ion_mode": "Negative",
                "untargeted": True,
            },
            "workspace": {
                "separation": "LC-MS",
                "acquisition_mode": "DDA",
                "ion_mode": "Negative",
                "rows": [
                    {"values": {"Polarity": "Negative", "Method type": "LC-MS"}},
                    {"values": {"Polarity": "Positive", "Method type": "Flow injection"}},
                ],
            },
        }
        with patch.object(mcp_server, "_request_json", return_value=inspection):
            result = mcp_server.msdial_repository_reanalysis_plan(
                "mb_post", "MPST-MIXED", "D:/repository"
            )

        self.assertFalse(result["project"]["eligible"])
        self.assertIsNone(result["project"]["ion_mode"])
        self.assertIsNone(result["project"]["separation"])
        self.assertGreaterEqual(len(result["project"]["review_reasons"]), 2)

    def test_repository_answer_seed_preserves_inferred_metadata_and_provenance(self) -> None:
        workspace = {
            "separation": "Liquid chromatography",
            "ion_mode": "Negative",
            "acquisition_mode": "DDA",
            "target_omics": "Lipidomics",
            "rows": [],
        }
        manifest = {"manifest_path": "D:/workspace/provenance/run-manifest.json"}

        result = mcp_server._repository_answer_seed(
            workspace,
            manifest,
            "D:/workspace/output",
            "delete_after_validated_output",
        )

        self.assertEqual("lcms", result["project_type"])
        self.assertEqual("Negative", result["ion_mode"])
        self.assertEqual("DDA", result["acquisition_type"])
        self.assertEqual("Lipidomics", result["target_omics"])
        self.assertEqual("auto_peak_range", result["parameter_strategy"])
        self.assertEqual(3000, result["target_peak_count_min"])
        self.assertEqual(6000, result["target_peak_count_max"])
        self.assertEqual(
            "TimeBasedLinearWeightedMovingAverage", result["smoothing_method"]
        )
        self.assertTrue(result["run_qa"])
        self.assertEqual(
            "delete_after_validated_output",
            result["workflow_overrides"]["repository_raw_retention_policy"],
        )

    def test_repository_answer_seed_maps_dia_family_to_console_acquisition_types(self) -> None:
        manifest = {"manifest_path": "D:/workspace/provenance/run-manifest.json"}
        expected = {
            "DDA": "DDA",
            "DIA": "SWATH",
            "SWATH": "SWATH",
            "AIF": "AIF",
        }

        for repository_mode, console_mode in expected.items():
            with self.subTest(repository_mode=repository_mode):
                result = mcp_server._repository_answer_seed(
                    {
                        "separation": "LC-MS",
                        "ion_mode": "Negative",
                        "acquisition_mode": repository_mode,
                        "rows": [],
                    },
                    manifest,
                    "D:/workspace/output",
                    "keep",
                )
                self.assertEqual(console_mode, result["acquisition_type"])

    def test_repository_download_requires_confirmation_before_start(self) -> None:
        inspection = {
            "project": {
                "repository": "mb_post",
                "accession": "MPST000007",
                "title": "Test project",
                "sample_count": 2,
                "separation": "LC-MS",
                "acquisition_mode": "DDA",
                "ion_mode": "Negative",
                "untargeted": True,
                "files": [{"name": "raw.tar"}],
                "total_download_bytes": 1024,
                "eligible": True,
                "selection_status": "eligible",
            },
            "workspace": {
                "rows": [{}, {}],
                "separation": "Liquid chromatography",
                "acquisition_mode": "DDA",
                "ion_mode": "Negative",
                "target_omics": "Lipidomics",
            },
        }
        with patch.object(mcp_server, "_request_json", return_value=inspection) as request:
            result = mcp_server.msdial_download_repository_raw(
                "mb_post",
                "MPST000007",
                "D:/repository",
                confirmed=False,
                analysis_purpose="Compare repository sample groups.",
            )

        self.assertFalse(result["started"])
        self.assertTrue(result["confirmation_required"])
        self.assertEqual(1, request.call_count)
        self.assertEqual("/api/repository/metadata/inspect", request.call_args.args[1])

    def test_repository_download_preview_blocks_bundle_over_limit(self) -> None:
        handoff = self._unit_handoff()
        handoff["download_scope"]["bundle_bytes"] = 3 * 1024**3
        result = mcp_server.msdial_download_repository_raw(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            maximum_gb=1,
            analysis_unit_handoff=handoff,
            confirmed=False,
            analysis_purpose="Evaluate annotation coverage.",
        )
        self.assertTrue(result["blocked"])
        self.assertFalse(result["confirmation_required"])
        self.assertFalse(result["preview"]["within_size_limit"])
        self.assertIn("size_limit:exceeded", result["preview"]["blocking_reasons"])

    def test_repository_download_rejects_ineligible_unit_after_class_confirmation(self) -> None:
        handoff = self._unit_handoff()
        handoff["technical_settings"]["acquisition_mode"] = "SRM"
        handoff["technical_settings"]["untargeted"] = False
        handoff["class_proposal"] = {"proposal_id": "confirmed-class", "assignments": []}
        handoff["blocking_reasons"] = []

        with patch.object(mcp_server, "_request_json") as request:
            result = mcp_server.msdial_download_repository_raw(
                "mb_post",
                "MPST-MIXED",
                "D:/repository",
                analysis_unit_handoff=handoff,
                confirmed=True,
                analysis_purpose="Compare the declared sample groups.",
            )

        request.assert_not_called()
        self.assertTrue(result["blocked"])
        self.assertFalse(result["started"])
        self.assertIn(
            "Repository metadata identifies the study as targeted.",
            result["preview"]["blocking_reasons"],
        )

    def test_confirmed_download_persists_analysis_purpose_in_repository_metadata(self) -> None:
        handoff = self._unit_handoff()
        purpose = "Compare treatment Classes and assess annotation coverage."

        with patch.object(
            mcp_server, "_request_json", return_value={"job_id": "download-job"}
        ) as request:
            result = mcp_server.msdial_download_repository_raw(
                "mb_post",
                "MPST-MIXED",
                "D:/repository",
                analysis_unit_handoff=handoff,
                confirmed=True,
                analysis_purpose=purpose,
            )

        self.assertTrue(result["started"])
        body = request.call_args.kwargs["body"]
        self.assertEqual(
            purpose,
            body["project"]["repository_metadata"]["analysis_purpose"],
        )

    def test_prepare_repository_reanalysis_previews_then_writes_analysis_csv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            output.mkdir()
            manifest_path = root / "run-manifest.json"
            manifest_path.write_text(
                json.dumps(
                    {
                        "project": {
                            "repository": "mb_post",
                            "accession": "MPST000007",
                            "title": "Test project",
                            "separation": "Liquid chromatography",
                            "acquisition_mode": "DDA",
                            "ion_mode": "Negative",
                            "sample_metadata": [
                                {
                                    "sample_id": "sample_a",
                                    "raw_file": "sample_a.lcd",
                                    "values": {"Cell line": "A"},
                                },
                                {
                                    "sample_id": "sample_b",
                                    "raw_file": "sample_b.lcd",
                                    "values": {"Cell line": "B"},
                                },
                            ],
                        },
                        "analysis_input_path": str(root / "raw"),
                        "output_directory": str(output),
                    }
                ),
                encoding="utf-8",
            )
            job = {
                "id": "download-job",
                "kind": "repository_download",
                "status": "completed",
                "raw_retention_policy": "keep",
                "result": {
                    "manifest_path": str(manifest_path),
                    "recognized": {
                        "files": [
                            {
                                "file_path": str(root / "raw" / "sample_a.lcd"),
                                "file_name": "sample_a",
                                "file_type": "Sample",
                                "class_id": "Sample",
                                "acquisition_type": "DDA",
                                "batch_order": 1,
                                "analytical_order": 1,
                                "factor": 1,
                            },
                            {
                                "file_path": str(root / "raw" / "sample_b.lcd"),
                                "file_name": "sample_b",
                                "file_type": "Sample",
                                "class_id": "Sample",
                                "acquisition_type": "DDA",
                                "batch_order": 1,
                                "analytical_order": 2,
                                "factor": 1,
                            },
                        ]
                    },
                },
            }

            with patch.object(mcp_server, "_request_json", return_value=job):
                preview = mcp_server.msdial_prepare_repository_reanalysis(
                    "download-job", hierarchy=["Cell line"], confirmed=False
                )
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    "download-job", hierarchy=["Cell line"], confirmed=True
                )

            self.assertFalse(preview["prepared"])
            self.assertEqual(2, preview["preview"]["matched_count"])
            self.assertTrue(prepared["prepared"])
            self.assertTrue(Path(prepared["input_path"]).is_file())
            self.assertTrue(
                Path(prepared["preview"]["answer_seed"]["repository_metadata_path"]).is_file()
            )
            self.assertEqual(
                str(manifest_path.resolve()),
                prepared["preview"]["answer_seed"]["workflow_overrides"][
                    "repository_run_manifest"
                ],
            )


if __name__ == "__main__":
    unittest.main()


class DownloadSizeGuardTests(unittest.TestCase):
    """CLAUDE-C02/C04: the number a human is asked to approve must be checkable."""

    def _handoff(self) -> dict:
        return McpRepositoryToolsTests._unit_handoff()

    def test_a_handoff_cannot_declare_a_bundle_smaller_than_its_own_files(self) -> None:
        handoff = self._handoff()
        handoff["download_scope"]["bundle_bytes"] = 1
        with self.assertRaisesRegex(
            ValueError, r"declares a 1-byte bundle but its file manifest totals 1024 bytes"
        ):
            mcp_server._project_from_analysis_unit_handoff(handoff)

    def test_a_forged_bundle_figure_no_longer_defeats_the_size_limit(self) -> None:
        handoff = self._handoff()
        handoff["download_scope"]["bundle_bytes"] = 1
        result = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            analysis_unit_handoff=handoff,
            analysis_purpose="Compare biological groups and produce mzTab-M.",
            maximum_gb=0.001,
        )
        self.assertFalse(result["ok"])
        self.assertEqual("validation_error", result["reason"])
        self.assertIn("1-byte bundle", result["detail"])

    def test_a_bundle_figure_below_the_manifest_is_never_within_the_size_limit(self) -> None:
        # Defence in depth: a project that reached the resolver without passing the
        # handoff guard still may not report a figure smaller than its own files.
        size = mcp_server._required_download_size(
            {"download_scope": {"bundle_bytes": 1}, "total_download_bytes": 103_478_458}
        )
        self.assertEqual(103_478_458, size["required_download_bytes"])
        self.assertTrue(size["bundle_bytes_contradicted"])
        self.assertEqual(1, size["declared_bundle_bytes"])

    def test_a_shared_bundle_larger_than_the_unit_is_not_a_contradiction(self) -> None:
        size = mcp_server._required_download_size(
            {"download_scope": {"bundle_bytes": 20_121_195_706}, "total_download_bytes": 103_478_458}
        )
        self.assertEqual(20_121_195_706, size["required_download_bytes"])
        self.assertFalse(size["bundle_bytes_contradicted"])

    def test_an_unknown_bundle_figure_falls_back_to_the_manifest_total(self) -> None:
        size = mcp_server._required_download_size(
            {"download_scope": {}, "total_download_bytes": 1024}
        )
        self.assertEqual(1024, size["required_download_bytes"])
        self.assertFalse(size["bundle_bytes_contradicted"])

    def test_the_declared_bundle_figure_is_never_reported_as_verified(self) -> None:
        handoff = self._handoff()
        result = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            analysis_unit_handoff=handoff,
            analysis_purpose="Compare biological groups and produce mzTab-M.",
        )
        self.assertFalse(result["download"]["bundle_bytes_verified"])
        self.assertFalse(result["download"]["bundle_bytes_contradicted"])
        self.assertEqual(2048, result["download"]["declared_bundle_bytes"])

    def test_maximum_gb_is_decimal_gb_not_gibibytes(self) -> None:
        handoff = self._handoff()
        handoff["files"][0]["size_bytes"] = 103_478_458
        handoff["download_scope"]["bundle_bytes"] = 20_121_195_706
        result = mcp_server.msdial_repository_reanalysis_plan(
            "mb_post",
            "MPST-MIXED",
            "D:/repository",
            analysis_unit_handoff=handoff,
            analysis_purpose="Compare biological groups and produce mzTab-M.",
            maximum_gb=19,
        )
        # 19 GiB would admit 20,401,094,656 bytes and pass this bundle.
        self.assertFalse(result["download"]["within_size_limit"])
        self.assertIn("size_limit:exceeded", result["download"]["blocking_reasons"])
