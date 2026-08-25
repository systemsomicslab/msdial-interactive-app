from __future__ import annotations

import unittest
from unittest.mock import patch

from msdial_app.repository_qa import (
    propose_repository_qa_targets,
    repository_internal_standard_evidence,
)


class RepositoryQaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = {
            "repository": "mb_post",
            "accession": "MPST000007",
            "ion_mode": "Negative",
            "rows": [
                {
                    "sample_id": "S1",
                    "values": {
                        "preparation / Internal standard": "EquiSPLASH (Avanti Polar Lipids)"
                    },
                },
                {
                    "sample_id": "S2",
                    "values": {
                        "preparation / Internal standard": "EquiSPLASH (Avanti Polar Lipids)"
                    },
                },
            ],
        }

    def test_extracts_deduplicated_repository_evidence(self) -> None:
        evidence = repository_internal_standard_evidence(self.workspace)
        self.assertEqual(1, len(evidence))
        self.assertEqual(2, evidence[0]["sample_count"])

    @patch("msdial_app.repository_qa.resolve_llm_config", return_value={"provider": "test"})
    @patch(
        "msdial_app.repository_qa.chat_completion",
        return_value='{"candidates":[{"name":"EquiSPLASH component","adduct":"[M-H]-","mz":500.25,"rt":null,"confidence":"medium"}]}',
    )
    def test_llm_draft_keeps_unsupported_rt_blank(self, _chat, _resolve) -> None:
        result = propose_repository_qa_targets(
            self.workspace,
            {"ion_mode": "Negative", "target_omics": "Lipidomics"},
            {"provider": "test"},
        )
        self.assertIsNone(result["candidates"][0]["rt"])
        self.assertIn("500.25,,0.01", result["lines"][0])
        self.assertTrue(result["review_required"])


if __name__ == "__main__":
    unittest.main()
