"""What a unit may analyse is decided by what its own samples name, not by an archive's name.

Two defects met here, and both surfaced on the first real download of the 2026-09 trial.

The analysis allow-list once accepted only files of role "raw". mzML is role "converted" and
MS-DIAL reads it natively. Every MetaboLights mzML unit was therefore refused, and the refusal came
AFTER the download had transferred the data: 587 MB of MTBLS2207 landed on disk and then
create_download_lease raised "empty file allow-list". mzXML is deliberately different: MS-DIAL has
no reader for it, so it must be converted to mzML before a unit can be analysed.

The deeper one: most units do not enumerate their raw files at all. Metabolomics Workbench
publishes one archive per study, so the unit's file list is the archive and nothing else. Measured
on 2026-09-21, 747 of the 831 campaign-eligible units holding files were in that state. Building
the allow-list from the file list alone came out empty for all of them.

The unit's samples do name their files, and matching those names is STRICTER than the archive name
it replaces. An archive shared between a positive and a negative unit admitted all of both through
its own name; a unit's sample list admits only that unit's files.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.repository_reanalysis import (
    ANALYSIS_INPUT_ROLES,
    EligibilityPolicy,
    RepositoryFile,
    RepositoryProject,
    _find_msdial_inputs,
    _filter_inputs_by_project_allowlist,
    _matches_sample_file_names,
    _sample_file_names,
    evaluate_eligibility,
    project_from_dict,
)


def _project(files: list[RepositoryFile], samples: list[str]) -> RepositoryProject:
    return RepositoryProject(
        repository="metabolomics_workbench",
        accession="ST000000",
        analysis_unit_id="unit-under-test",
        files=files,
        sample_metadata=[{"sample_id": name, "raw_file": name} for name in samples],
    )


class AcceptedRolesTests(unittest.TestCase):
    def test_converted_is_an_analysis_input(self) -> None:
        """mzML is a converted open format that MS-DIAL reads natively."""
        self.assertIn("converted", ANALYSIS_INPUT_ROLES)
        self.assertIn("raw", ANALYSIS_INPUT_ROLES)

    def test_mzxml_is_normalized_to_requires_conversion(self) -> None:
        project = project_from_dict(
            {
                "repository": "metabolights",
                "accession": "MTBLS1",
                "files": [
                    {
                        "name": "FILES/sample.mzXML",
                        "size_bytes": 10,
                        "url": "https://x",
                        "role": "converted",
                    }
                ],
            }
        )

        self.assertEqual("requires_conversion", project.files[0].role)

    def test_mzxml_is_rejected_before_download(self) -> None:
        project = _project(
            [RepositoryFile("study.zip", 10, "https://x", role="raw_archive")],
            ["sample.mzXML"],
        )

        evaluated = evaluate_eligibility(
            project,
            EligibilityPolicy(
                max_download_bytes=100,
                max_samples=10,
                require_known_size=False,
                require_untargeted=False,
            ),
        )

        self.assertFalse(evaluated.eligible)
        self.assertTrue(any("no mzXML/mzData reader" in item for item in evaluated.exclusion_reasons))

    def test_st003038_parallel_encodings_are_refused_without_a_reviewed_substitution(self) -> None:
        """Keep the repository's real shape until an explicit substitution rule exists."""
        project = _project(
            [
                RepositoryFile(
                    "ST003038_rawdata_mzML.zip", 10, "https://x", role="shared_raw_archive"
                ),
                RepositoryFile(
                    "ST003038_rawdata_mzXML.zip", 10, "https://x", role="shared_raw_archive"
                ),
            ],
            [f"211210_SVC_Pozzi__Lipidomics_NEG_S{i:02d}.mzXML" for i in range(1, 11)],
        )

        evaluated = evaluate_eligibility(
            project,
            EligibilityPolicy(
                max_download_bytes=100,
                max_samples=20,
                require_known_size=False,
                require_untargeted=False,
            ),
        )

        self.assertFalse(evaluated.eligible)
        self.assertTrue(
            any("no mzXML/mzData reader" in item for item in evaluated.exclusion_reasons)
        )

    def test_an_alternate_encoding_is_not_an_analysis_input(self) -> None:
        """A .wiff2 beside a .wiff is the same sample twice, and an existing test pinned this."""
        self.assertNotIn("raw_alternate", ANALYSIS_INPUT_ROLES)

    def test_an_alternate_encoding_does_not_slip_in_through_the_stem(self) -> None:
        """The narrower half of the same rule: stems only from names recorded without extensions."""
        names = _sample_file_names(_project([], ["sample.wiff"]))

        self.assertTrue(_matches_sample_file_names(Path("/d/sample.wiff"), names))
        self.assertFalse(_matches_sample_file_names(Path("/d/sample.wiff2"), names))

    def test_an_archive_is_not_an_analysis_input(self) -> None:
        """Its extracted contents are, and they are attributed by sample name."""
        self.assertNotIn("raw_archive", ANALYSIS_INPUT_ROLES)
        self.assertNotIn("shared_raw_archive", ANALYSIS_INPUT_ROLES)

    def test_a_sidecar_is_not_an_analysis_input(self) -> None:
        """A .wiff.scan is read through its .wiff, never opened on its own."""
        self.assertNotIn("sidecar", ANALYSIS_INPUT_ROLES)
        self.assertNotIn("auxiliary", ANALYSIS_INPUT_ROLES)


class SampleFileNameTests(unittest.TestCase):
    def test_both_the_name_and_the_stem_are_kept(self) -> None:
        """A repository may record "sample_01" for a file that arrives as "sample_01.mzML"."""
        project = _project([], ["NEG_Ig04GST_LV10.raw", "120721ElimC12HA1b"])

        exact, stems = _sample_file_names(project)

        self.assertIn("neg_ig04gst_lv10.raw", exact)
        self.assertNotIn("neg_ig04gst_lv10", stems,
                         "a recorded extension means the match must be exact")
        self.assertIn("120721elimc12ha1b", stems,
                      "and a recorded name without one may match any extension")

    def test_a_directory_style_input_matches_its_recorded_name(self) -> None:
        """Agilent .d and MassLynx .raw arrive as directories, not files."""
        names = _sample_file_names(_project([], ["SAMPLE_01.d"]))

        self.assertTrue(_matches_sample_file_names(Path("/data/SAMPLE_01.d"), names))

    def test_an_extension_the_repository_did_not_record_still_matches(self) -> None:
        names = _sample_file_names(_project([], ["120721ElimC12HA1b"]))

        self.assertTrue(_matches_sample_file_names(Path("/d/120721ElimC12HA1b.mzML"), names))

    def test_a_file_belonging_to_another_unit_does_not_match(self) -> None:
        """THE POINT OF THE WHOLE CHANGE. This is what the archive name could not express."""
        names = _sample_file_names(_project([], ["NEG_01.raw", "NEG_02.raw"]))

        self.assertFalse(_matches_sample_file_names(Path("/d/POS_01.raw"), names))
        self.assertFalse(_matches_sample_file_names(Path("/d/NEG_03.raw"), names))

    def test_a_sample_naming_no_file_contributes_nothing(self) -> None:
        self.assertEqual((set(), set()), _sample_file_names(_project([], ["", "   "])))


class FilterTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.data_root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _inputs(self, *names: str) -> list[str]:
        paths = []
        for name in names:
            path = self.data_root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("", encoding="ascii")
            paths.append(str(path))
        return paths

    def test_a_unit_of_converted_files_is_no_longer_refused(self) -> None:
        """THE MTBLS2207 CASE, which failed after downloading 587 MB."""
        project = _project(
            [RepositoryFile("FILES/M3T-Std_neg_DIA_20mz.mzML", 10, "https://x", role="converted")],
            ["M3T-Std_neg_DIA_20mz.mzML"],
        )
        inputs = self._inputs("M3T-Std_neg_DIA_20mz.mzML")

        selected = _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

        self.assertEqual(inputs, selected)

    def test_an_archive_only_unit_is_attributed_by_its_samples(self) -> None:
        """THE ST003399 CASE. The unit declares one zip and 61 samples that name their files."""
        project = _project(
            [RepositoryFile("ST003399_rawdata.zip", 10, "https://x", role="raw_archive")],
            ["120721ElimC12HA1b", "120721ElimC12HA2b"],
        )
        inputs = self._inputs("120721ElimC12HA1b.raw", "120721ElimC12HA2b.raw")

        selected = _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

        self.assertEqual(2, len(selected))

    def test_a_shared_archive_admits_only_this_units_files(self) -> None:
        """A shared archive is filtered by samples, not admitted wholesale.

        One zip holds both polarities. Through the archive's own name every file matched, so a
        unit labelled Negative would have analysed the positive files too. Through the unit's
        sample names, only its own ten arrive.

        The sample names are ST003038's with the extension rewritten to mzML, so this is a
        synthetic variant, not that study. ST003038 as published is mzXML and is refused before
        download; its real shape is held by
        test_st003038_parallel_encodings_are_refused_without_a_reviewed_substitution.
        """
        project = _project(
            [RepositoryFile("study_rawdata_mzML.zip", 10, "https://x", role="shared_raw_archive")],
            [f"211210_SVC_Pozzi__Lipidomics_NEG_S{i:02d}.mzML" for i in range(1, 11)],
        )
        inputs = self._inputs(
            *[f"211210_SVC_Pozzi__Lipidomics_NEG_S{i:02d}.mzML" for i in range(1, 11)],
            *[f"211210_SVC_Pozzi__Lipidomics_POS_S{i:02d}.mzML" for i in range(1, 11)],
        )

        selected = _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

        self.assertEqual(10, len(selected))
        self.assertTrue(all("NEG" in Path(item).name for item in selected))

    def test_mzxml_never_enters_the_msdial_input_set(self) -> None:
        project = _project([], ["sample.mzXML"])
        inputs = self._inputs("sample.mzXML")

        with self.assertRaises(ValueError):
            _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

    def test_input_discovery_ignores_mzxml_and_mzdata(self) -> None:
        self._inputs("sample.mzML")
        self._inputs("legacy.mzXML", "older.mzData.xml")

        self.assertEqual(
            ["sample.mzML"],
            [Path(item).name for item in _find_msdial_inputs(self.data_root)],
        )

    def test_a_unit_that_names_nothing_at_all_is_still_refused(self) -> None:
        """The guard stays: with no declared input and no sample naming a file, nothing is safe."""
        project = _project(
            [RepositoryFile("study.zip", 10, "https://x", role="raw_archive")], ["", ""]
        )
        inputs = self._inputs("whatever.mzML")

        with self.assertRaises(ValueError) as raised:
            _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

        self.assertIn("names no analysis input", str(raised.exception))

    def test_downloaded_content_that_matches_nothing_is_still_refused(self) -> None:
        """Refusing to fall back to accession-level inputs is the rule this must not relax."""
        project = _project(
            [RepositoryFile("study.zip", 10, "https://x", role="raw_archive")], ["expected_01.raw"]
        )
        inputs = self._inputs("something_else.raw")

        with self.assertRaises(ValueError) as raised:
            _filter_inputs_by_project_allowlist(inputs, self.data_root, project)

        self.assertIn("Refusing to fall back", str(raised.exception))

    def test_a_local_analysis_without_a_unit_is_untouched(self) -> None:
        project = RepositoryProject(repository="local", accession="none")
        inputs = self._inputs("a.mzML", "b.mzML")

        self.assertEqual(inputs, _filter_inputs_by_project_allowlist(inputs, self.data_root, project))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
