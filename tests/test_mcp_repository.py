from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app import mcp_server


class McpRepositoryToolsTests(unittest.TestCase):
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
        self.assertTrue(result["run_qa"])
        self.assertEqual(
            "delete_after_validated_output",
            result["workflow_overrides"]["repository_raw_retention_policy"],
        )

    def test_repository_download_requires_confirmation_before_start(self) -> None:
        inspection = {
            "project": {
                "repository": "mb_post",
                "accession": "MPST000007",
                "title": "Test project",
                "sample_count": 2,
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
                "mb_post", "MPST000007", "D:/repository", confirmed=False
            )

        self.assertFalse(result["started"])
        self.assertTrue(result["confirmation_required"])
        self.assertEqual(1, request.call_count)
        self.assertEqual("/api/repository/metadata/inspect", request.call_args.args[1])

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
