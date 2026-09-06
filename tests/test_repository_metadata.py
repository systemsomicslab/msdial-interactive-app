from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_metadata import (
    ClassProposalMismatch,
    apply_class_proposal,
    apply_classes_to_analysis_files,
    class_token,
    metadata_workspace,
    metadata_workspace_from_file,
    project_class_hierarchy,
    save_metadata_review,
)
from msdial_app.repository_reanalysis import (
    _mbpost_sample_metadata,
    _metabolights_sample_metadata,
    _workbench_sample_metadata,
)


class RepositoryMetadataTests(unittest.TestCase):
    def test_class_projection_preserves_original_values(self) -> None:
        workspace = metadata_workspace(
            {
                "repository": "test",
                "accession": "X1",
                "sample_metadata": [
                    {
                        "sample_id": "A",
                        "raw_file": "A.raw",
                        "values": {"Age": "Young adult", "Region": "North_West", "Sex": "F"},
                    },
                    {
                        "sample_id": "B",
                        "raw_file": "B.raw",
                        "values": {"Age": "Old", "Region": "", "Sex": "M"},
                    },
                ],
            }
        )
        projected = project_class_hierarchy(workspace, ["Age", "Region", "Sex"])
        self.assertEqual("Young-adult_North-West_F", projected["rows"][0]["class_id"])
        self.assertEqual("Old_NA_M", projected["rows"][1]["class_id"])
        self.assertEqual("North_West", projected["rows"][0]["values"]["Region"])

    def test_analysis_file_matching_applies_class_and_qc_type(self) -> None:
        workspace = project_class_hierarchy(
            metadata_workspace(
                {
                    "sample_metadata": [
                        {
                            "sample_id": "pooled_qc_1",
                            "raw_file": "FILES/run_01.raw",
                            "values": {"Group": "Pooled QC", "Injection order": "12"},
                        }
                    ]
                }
            ),
            ["Group"],
        )
        result = apply_classes_to_analysis_files(
            workspace,
            [{"file_path": r"D:\data\run_01.raw", "file_name": "run_01", "file_type": "Sample"}],
        )
        self.assertEqual(1, result["matched_count"])
        self.assertEqual("Pooled-QC", result["files"][0]["class_id"])
        self.assertEqual("QC", result["files"][0]["file_type"])
        self.assertEqual(12, result["files"][0]["analytical_order"])

    def test_review_bundle_contains_original_metadata_and_analysis_csv(self) -> None:
        workspace = project_class_hierarchy(
            metadata_workspace(
                {
                    "accession": "ST1",
                    "sample_metadata": [
                        {"sample_id": "S1", "raw_file": "S1.cdf", "values": {"Group": "Control"}}
                    ],
                }
            ),
            ["Group"],
        )
        with tempfile.TemporaryDirectory() as temporary:
            result = save_metadata_review(
                workspace,
                temporary,
                [{"file_path": str(Path(temporary) / "S1.cdf"), "file_name": "S1"}],
            )
            self.assertTrue(Path(result["metadata_json"]).is_file())
            self.assertIn("Group", Path(result["metadata_tsv"]).read_text(encoding="utf-8-sig"))
            self.assertIn("Control", Path(result["analysis_files_csv"]).read_text(encoding="utf-8-sig"))

    def test_loads_workspace_from_run_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "manifest.json"
            path.write_text(
                json.dumps(
                    {
                        "project": {
                            "repository": "metabolights",
                            "accession": "MTBLS1",
                            "sample_metadata": [
                                {"sample_id": "S1", "raw_file": "S1.mzML", "values": {"Sex": "F"}}
                            ],
                        }
                    }
                ),
                encoding="utf-8",
            )
            workspace = metadata_workspace_from_file(path)
            self.assertEqual("MTBLS1", workspace["accession"])
            self.assertEqual("Sex", workspace["fields"][0]["name"])

    def test_repository_specific_sample_parsers(self) -> None:
        workbench = _workbench_sample_metadata(
            [{"local_sample_id": "A1", "factors": "Genotype:KO | Sex:F", "additional_sample_data": "Age=12", "raw data": "run_A1"}]
        )
        self.assertEqual("KO", workbench[0]["values"]["Genotype"])
        self.assertEqual("run_A1", workbench[0]["raw_file"])
        assay = json.dumps(
            {
                "data": {
                    "rows": [
                        {
                            "Sample Name": "S1",
                            "Raw Spectral Data File": "FILES/S1.mzML",
                            "Factor Value[Genotype]": "WT",
                        }
                    ]
                }
            }
        )
        study_table = (
            "Source Name\tSample Name\tCharacteristics[Region]\tFactor Value[Genotype]\n"
            "source-1\tS1\tNorth\tWT\n"
        )
        metabolights = _metabolights_sample_metadata(
            assay, {"materials": {"samples": []}}, study_table
        )
        self.assertEqual("FILES/S1.mzML", metabolights[0]["raw_file"])
        self.assertEqual("North", metabolights[0]["values"]["Region"])
        self.assertEqual("WT", metabolights[0]["values"]["Genotype"])
        mbpost = _mbpost_sample_metadata(
            {"organism": "Human"},
            [{"name": "sample_1.wiff"}],
            {
                "sample_1.wiff": {
                    "presets": [
                        {
                            "category": "sample",
                            "presets": [{"label": "Genotype", "value": "KO"}],
                        }
                    ]
                }
            },
        )
        self.assertEqual("Human", mbpost[0]["values"]["organism"])
        self.assertEqual("KO", mbpost[0]["values"]["sample / Genotype"])

    def test_default_hierarchy_and_acquisition_are_applied(self) -> None:
        workspace = metadata_workspace(
            {
                "repository": "test",
                "acquisition_mode": "DIA",
                "sample_metadata": [
                    {"sample_id": "S1", "raw_file": "S1.raw", "values": {"Group": "Control"}},
                    {"sample_id": "S2", "raw_file": "S2.raw", "values": {"Group": "Case"}},
                    {"sample_id": "S3", "raw_file": "S3.raw", "values": {"Group": "Control"}},
                ],
            }
        )
        self.assertEqual(["Group"], workspace["hierarchy"])
        applied = apply_classes_to_analysis_files(
            workspace,
            [{"file_path": r"D:\data\S1.raw", "file_name": "S1", "acquisition_type": "DDA"}],
        )
        self.assertEqual("Control", applied["files"][0]["class_id"])
        self.assertEqual("SWATH", applied["files"][0]["acquisition_type"])

    def test_default_hierarchy_excludes_technical_namespaces(self) -> None:
        technical = metadata_workspace(
            {
                "sample_metadata": [
                    {
                        "sample_id": "S1",
                        "raw_file": "S1.wiff",
                        "values": {
                            "analyticalCondition / Collision energy": "14 eV",
                            "softwareSetting / Processing mode": "A",
                        },
                    },
                    {
                        "sample_id": "S2",
                        "raw_file": "S2.wiff",
                        "values": {
                            "analyticalCondition / Collision energy": "18 eV",
                            "softwareSetting / Processing mode": "B",
                        },
                    },
                    {
                        "sample_id": "S3",
                        "raw_file": "S3.wiff",
                        "values": {
                            "analyticalCondition / Collision energy": "14 eV",
                            "softwareSetting / Processing mode": "A",
                        },
                    },
                ]
            }
        )
        biological = metadata_workspace(
            {
                "sample_metadata": [
                    {"sample_id": "A1", "values": {"sample / Cell line": "A"}},
                    {"sample_id": "A2", "values": {"sample / Cell line": "A"}},
                    {"sample_id": "B1", "values": {"sample / Cell line": "B"}},
                ]
            }
        )

        self.assertEqual([], technical["hierarchy"])
        self.assertEqual(["sample / Cell line"], biological["hierarchy"])

    def test_class_token_is_split_safe(self) -> None:
        self.assertEqual("Control-North", class_token("Control_North"))
        self.assertEqual("対照群-東京", class_token("対照群_東京"))


class ApprovedClassProposalTests(unittest.TestCase):
    """The Catalog decides the grouping; Interactive applies it rather than re-deriving it."""

    def _workspace(self) -> dict:
        return metadata_workspace(
            {
                "repository": "test",
                "accession": "X1",
                "sample_metadata": [
                    {"sample_id": "A", "raw_file": "A.raw", "values": {"Strain": "Kyokai 6"}},
                    {"sample_id": "B", "raw_file": "B.raw", "values": {"Strain": "Kyokai 6"}},
                    {"sample_id": "C", "raw_file": "C.raw", "values": {"Strain": "EC1118"}},
                ],
            }
        )

    def _proposal(self, **overrides) -> dict:
        proposal = {
            "proposal_id": "p-1",
            "unit_id": "u-1",
            "purpose": "Compare fermentation strains",
            "selected_fields": ["Strain"],
            "rationale": "Balanced one-way design over the strain field.",
            "contrast_definition": {"kind": "one_way", "reference": "EC1118"},
            "model": "test-model",
            "prompt_hash": "abc123",
            "status": "proposed",
            "assignments": [
                {"sample_id": "A", "class_label": "Kyokai 6", "values": {"Strain": "Kyokai 6"}},
                {"sample_id": "B", "class_label": "Kyokai 6", "values": {"Strain": "Kyokai 6"}},
                {"sample_id": "C", "class_label": "EC1118", "values": {"Strain": "EC1118"}},
            ],
        }
        proposal.update(overrides)
        return proposal

    def test_the_approved_assignments_become_the_executed_classes(self) -> None:
        projected = apply_class_proposal(self._workspace(), self._proposal())

        self.assertEqual(
            {"A": "Kyokai-6", "B": "Kyokai-6", "C": "EC1118"},
            {row["sample_id"]: row["class_id"] for row in projected["rows"]},
        )
        self.assertEqual("catalog_class_proposal", projected["class_source"])
        self.assertEqual(["Strain"], projected["hierarchy"])

    def test_a_label_the_fields_cannot_reproduce_still_survives(self) -> None:
        # This is why the assignments are the source of truth and the fields are not: a proposal may
        # merge or rename labels for reasons that live in its rationale, and recombining
        # selected_fields reproduces only the mechanical case.
        proposal = self._proposal(
            assignments=[
                {"sample_id": "A", "class_label": "Sake strains", "values": {"Strain": "Kyokai 6"}},
                {"sample_id": "B", "class_label": "Sake strains", "values": {"Strain": "Kyokai 6"}},
                {"sample_id": "C", "class_label": "Wine strains", "values": {"Strain": "EC1118"}},
            ]
        )

        projected = apply_class_proposal(self._workspace(), proposal)

        self.assertEqual(
            {"Sake-strains", "Wine-strains"},
            {row["class_id"] for row in projected["rows"]},
        )

    def test_the_provenance_travels_with_the_projection(self) -> None:
        projected = apply_class_proposal(self._workspace(), self._proposal())

        provenance = projected["class_proposal_provenance"]
        self.assertEqual("p-1", provenance["proposal_id"])
        self.assertEqual("Compare fermentation strains", provenance["purpose"])
        self.assertEqual(
            {"kind": "one_way", "reference": "EC1118"}, provenance["contrast_definition"]
        )
        self.assertEqual("abc123", provenance["prompt_hash"])
        self.assertEqual(3, provenance["assignment_count"])

    def test_a_sample_the_proposal_does_not_assign_is_refused(self) -> None:
        proposal = self._proposal(assignments=self._proposal()["assignments"][:2])

        with self.assertRaises(ClassProposalMismatch) as caught:
            apply_class_proposal(self._workspace(), proposal)
        self.assertIn("unassigned", str(caught.exception))

    def test_an_assignment_for_a_sample_the_unit_lacks_is_refused(self) -> None:
        proposal = self._proposal(
            assignments=[
                *self._proposal()["assignments"],
                {"sample_id": "D", "class_label": "EC1118", "values": {}},
            ]
        )

        with self.assertRaises(ClassProposalMismatch) as caught:
            apply_class_proposal(self._workspace(), proposal)
        self.assertIn("does not contain", str(caught.exception))

    def test_a_sample_assigned_twice_is_refused(self) -> None:
        proposal = self._proposal(
            assignments=[
                *self._proposal()["assignments"],
                {"sample_id": "A", "class_label": "EC1118", "values": {}},
            ]
        )

        with self.assertRaises(ClassProposalMismatch) as caught:
            apply_class_proposal(self._workspace(), proposal)
        self.assertIn("more than once", str(caught.exception))

    def test_an_empty_class_label_is_refused_rather_than_defaulted(self) -> None:
        assignments = self._proposal()["assignments"]
        assignments[1] = {"sample_id": "B", "class_label": "  ", "values": {}}

        with self.assertRaises(ClassProposalMismatch) as caught:
            apply_class_proposal(self._workspace(), self._proposal(assignments=assignments))
        self.assertIn("empty Class label", str(caught.exception))

    def test_a_proposal_with_no_assignments_is_refused(self) -> None:
        with self.assertRaises(ClassProposalMismatch):
            apply_class_proposal(self._workspace(), self._proposal(assignments=[]))


if __name__ == "__main__":
    unittest.main()
