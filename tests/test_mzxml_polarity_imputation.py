"""A campaign's conversion gives an mzXML's polarity-less scans the polarity their unit declares, and only that.

MS-DIAL skips a spectrum whose polarity is not the method's ion mode, and RawDataHandler reads polarity only as
a spectrum cvParam, so an mzXML whose scans record none was converted to an mzML that runs to nothing, and the
gate's CONV-1 failed it. The user decided on 2026-10-02 that, in a campaign, the lease's convert stage imputes
the polarity from the unit's declared ion mode, and only where the unit's Catalog handoff declares exactly one
polarity (technical_settings.ion_mode Positive or Negative), the field CONV-1 holds an imputation to; it is
recorded as an inference with its count. Both, Unknown or nothing imputes nothing, and CONV-1 fails the unit.
project.ion_mode is never read: the raw-header preflight rewrites it from headers that carry what was imputed,
and a split part's is its part's polarity. The converter still refuses a file any of whose scans records the
other polarity. Outside a campaign nothing converts, as before.

The fixtures are the converter's synthetic mzXML (test_mzxml_conversion) with their polarity attributes taken
out, served by test_download_lease_record._Client. The gate tests run the reanalysis gate's own script, where it
is on this machine, on a workspace these leases wrote.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_mzxml_lease_conversion as lease_tests
from msdial_app import mcp_server
from msdial_app.repository_analysis_rows import build_repository_analysis_rows, write_analysis_csv
from msdial_app.repository_reanalysis import (
    CONVERSION_FAILED,
    DECLARED_ION_MODE_FIELD,
    RepositoryProject,
    conversion_polarity_declaration,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
)

from test_mzxml_conversion import dda_32, polarity_32, read_mzml
from test_mzxml_lease_conversion import GATE, OFF, _declared, _project, _Scratch, _stage
from test_raw_metadata_preflight import _Extractor, _PinnedExtractor

# The cvParams of positive and negative scan polarity.
POSITIVE, NEGATIVE = "MS:1000130", "MS:1000129"
# The scans of a synthetic DDA file (dda_32), none of which records a polarity once _without_polarity is done.
DDA_SCANS = 4


def _without_polarity(data: bytes) -> bytes:
    """The synthetic mzXML with no scan recording a polarity, its embedded sha1 computed again over the span."""
    prefix = data.split(b"  <sha1>", 1)[0].replace(b' polarity="+"', b"") + b"  <sha1>"
    return prefix + hashlib.sha1(prefix).hexdigest().encode("ascii") + b"</sha1>\n</mzXML>\n"


def _handoff_project(
    payloads: dict[str, bytes], mode: str | None, *, ion_mode: str = "Unknown", unit: str = "mtbls417-neg", **options
) -> RepositoryProject:
    """An MTBLS417-shaped unit whose Catalog handoff's technical settings declare ``mode`` (None: no ion_mode),
    with its project.ion_mode ``ion_mode``, as a preflight may have rewritten it."""
    project = _project(payloads, declared=True, unit=unit, **options)
    settings = {"separation": "LC-MS", "acquisition_mode": project.acquisition_mode, "untargeted": True}
    if mode is not None:
        settings["ion_mode"] = mode
    project.ion_mode = ion_mode
    project.repository_metadata = {
        "catalog_handoff": {
            "schema": "msdial-repository-reanalysis-handoff.v1",
            "repository": project.repository,
            "accession": project.accession,
            "analysis_unit_id": unit,
            "technical_settings": settings,
        }
    }
    return project


def _declaration(mode: str | None, polarity: str | None, unit: str = "mtbls417-neg", reason: str = "") -> dict:
    declaration = {"field": DECLARED_ION_MODE_FIELD, "declared": mode, "declared_by": unit, "impute_polarity": polarity}
    if reason:
        declaration["reason"] = reason
    return declaration


class _Gate(_Scratch):
    def checks(self, manifest: dict) -> dict[str, dict]:
        """The before-production gate on a unit, with the analysis CSV its manifest gives."""
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        write_analysis_csv(built, Path(manifest["output_directory"]) / "analysis_files.csv")
        completed = subprocess.run(
            [sys.executable, str(GATE), manifest["workspace"], "--stage", "before-production", "--json"],
            capture_output=True, text=True, encoding="utf-8", check=False,
        )
        report = json.loads(completed.stdout)
        return {check["check_id"]: check for check in report["checks"]}

    def preflight(self, path: Path, verdicts: dict[str, dict]) -> dict:
        """The unit's raw-header preflight, with these header verdicts by file name, by a pinned stand-in."""
        if getattr(self, "_extractor", None) is None:
            self._extractor = _PinnedExtractor.make(self.root / "build")
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=_Extractor(verdicts)):
            return run_raw_metadata_preflight(path, self._extractor)


# ---- the lease's convert stage ------------------------------------------------------------------------------


class ADeclaredPolarityIsImputed(_Scratch):
    PAYLOADS = {"S01.mzXML": _without_polarity(dda_32()), "S02.mzXML": _without_polarity(dda_32())}

    def test_a_negative_units_polarity_less_scans_are_written_negative_and_recorded(self) -> None:
        manifest = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Negative", ion_mode="Negative"))
        block = manifest["input_conversions"]
        options = _declared("Negative", "negative")

        self.assertEqual(["converted", "converted"], [record["status"] for record in block["records"]])
        self.assertEqual([options, options], [record["options"] for record in block["records"]])
        self.assertEqual(options, block["options"])
        self.assertEqual(_declaration("Negative", "negative"), block["polarity_declaration"])
        for record in block["records"]:
            (inference,) = record["inferences"]
            self.assertEqual(
                ("polarity_imputation", "negative", DDA_SCANS, "Negative", DECLARED_ION_MODE_FIELD),
                (inference["kind"], inference["value"], inference["spectra"], inference["declared_ion_mode"],
                 inference["declared_in"]),
            )
            self.assertEqual({"negative": DDA_SCANS}, record["counts"]["polarity"])
            self.assertEqual({"absent": DDA_SCANS}, record["counts"]["polarity_recorded"])
            _, spectra = read_mzml(Path(record["output"]["path"]))
            self.assertEqual([True] * DDA_SCANS, [NEGATIVE in spectrum["cv"] and POSITIVE not in spectrum["cv"]
                                                  for spectrum in spectra])
        self.assertEqual(2 * DDA_SCANS, block["counts"]["imputed_polarity_spectra"])
        convert = _stage(manifest, "convert")
        self.assertEqual(("negative", 2 * DDA_SCANS), (convert["impute_polarity"], convert["imputed_polarity_spectra"]))
        self.assertEqual(options, manifest["project"]["conversion_plan"]["options"])
        self.assertTrue(manifest["execution_allowed"])

    def test_the_declaration_is_the_handoffs_never_project_ion_mode(self) -> None:
        """project.ion_mode is what a preflight rewrote from headers that carry the imputation, or a part's
        polarity: it is not what the unit declares."""
        for mode, ion_mode, expected in (("Negative", "Positive", "negative"), ("Both", "Negative", None),
                                         ("Positive", "Negative", "positive"), (None, "Negative", None)):
            with self.subTest(declared=mode, ion_mode=ion_mode), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, mode, ion_mode=ion_mode))
                records = manifest["input_conversions"]["records"]

                self.assertEqual([expected] * 2, [record["options"]["impute_polarity"] for record in records])
                self.assertEqual([mode] * 2, [record["options"]["declared_ion_mode"] for record in records])
                self.assertEqual([{expected: DDA_SCANS} if expected else {"unrecorded": DDA_SCANS}] * 2,
                                 [record["counts"]["polarity"] for record in records])

    def test_a_unit_declaring_no_single_polarity_imputes_nothing(self) -> None:
        cases = {
            "Both": ("Both", "The unit's Catalog handoff declares Both, not one polarity."),
            "Unknown": ("Unknown", "The unit's Catalog handoff declares Unknown, not one polarity."),
            "no ion mode": (None, "The unit's Catalog handoff declares no ion mode."),
        }
        for label, (mode, reason) in cases.items():
            with self.subTest(declared=label), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                manifest = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, mode, ion_mode="Negative"))
                block = manifest["input_conversions"]

                self.assertEqual(_declaration(mode, None, reason=reason), block["polarity_declaration"])
                expected = _declared(mode, None) if mode else OFF
                self.assertEqual([expected, expected], [record["options"] for record in block["records"]])
                self.assertEqual([[], []], [record["inferences"] for record in block["records"]])
                self.assertEqual([{"unrecorded": DDA_SCANS}] * 2, [record["counts"]["polarity"] for record in block["records"]])
                self.assertEqual(0, block["counts"]["imputed_polarity_spectra"])
                self.assertIsNone(_stage(manifest, "convert")["impute_polarity"])

    def test_a_unit_with_no_handoff_converts_as_before(self) -> None:
        manifest = self.lease(self.PAYLOADS, _project(self.PAYLOADS))
        block = manifest["input_conversions"]

        self.assertEqual([OFF, OFF], [record["options"] for record in block["records"]])
        self.assertEqual(
            _declaration(None, None, unit=None, reason="The unit carries no Catalog handoff, so no ion mode is declared for it."),
            block["polarity_declaration"],
        )

    def test_a_scan_recording_the_other_polarity_refuses_its_file_and_the_rest_run(self) -> None:
        """The converter's own guard: a declared Negative cannot stand for scans beside one that records +."""
        payloads = {**self.PAYLOADS, "S03.mzXML": polarity_32([None, "+", None])}

        manifest = self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative"))
        records = manifest["input_conversions"]["records"]
        source = str((Path(manifest["input_directory"]) / "S03.mzXML").resolve())

        self.assertEqual(["converted", "converted", "failed"], [record["status"] for record in records])
        self.assertIn("polarity imputation refused: 1 scans record positive polarity", records[2]["error"])
        self.assertFalse(Path(records[2]["output"]["path"]).exists())
        self.assertEqual([(source, CONVERSION_FAILED)],
                         [(item["path"], item["reason"]) for item in manifest["excluded_input_candidates"]])
        self.assertEqual(["S01.mzML", "S02.mzML"], [Path(item).name for item in manifest["input_candidates"]])
        self.assertEqual(2 * DDA_SCANS, manifest["input_conversions"]["counts"]["imputed_polarity_spectra"])
        self.assertTrue(manifest["execution_allowed"])

    def test_a_re_lease_never_reuses_a_conversion_made_under_another_declaration(self) -> None:
        """The converter reuses a record only when its options are this call's, and the declaration is among them:
        once the Catalog corrects a unit from Both to Negative, its mzXML are converted again, with the imputation."""
        first = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Both", ion_mode="Negative"))
        corrected = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Negative", ion_mode="Negative"))
        again = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Negative", ion_mode="Positive"))

        self.assertEqual([None, None], [item["options"]["impute_polarity"] for item in first["input_conversions"]["records"]])
        records = corrected["input_conversions"]["records"]
        self.assertEqual([False, False], [item["reused_previous_record"] for item in records])
        self.assertEqual((0, 2 * DDA_SCANS), (_stage(corrected, "convert")["reused"],
                                              corrected["input_conversions"]["counts"]["imputed_polarity_spectra"]))
        self.assertNotEqual(
            [item["output"]["sha256"] for item in first["input_conversions"]["records"]],
            [item["output"]["sha256"] for item in records],
        )
        # The same declaration again, whatever project.ion_mode says now: the conversions are reused.
        self.assertEqual([True, True], [item["reused_previous_record"] for item in again["input_conversions"]["records"]])
        self.assertEqual([item["output"]["sha256"] for item in records],
                         [item["output"]["sha256"] for item in again["input_conversions"]["records"]])


class TheConversionPlanNamesTheDeclaration(unittest.TestCase):
    """evaluate_eligibility plans the conversion before a byte is fetched, with the options the lease will use."""

    def test_a_handoffs_declaration_decides_the_planned_imputation(self) -> None:
        for mode, polarity in (("Negative", "negative"), ("Positive", "positive"), ("Both", None), ("Unknown", None)):
            with self.subTest(declared=mode):
                handoff = lease_tests.AnMzxmlUnitIsEligibleWithAConversionPlan._handoff(["FILES/a.mzXML"])
                handoff["technical_settings"]["ion_mode"] = mode

                project, _ = mcp_server._project_from_analysis_unit_handoff(handoff, convert_mzxml=True)

                self.assertEqual(_declared(mode, polarity), project["conversion_plan"]["options"])
                self.assertEqual(
                    (mode, "mtbls417-pos-dda", polarity),
                    tuple(conversion_polarity_declaration(project)[key] for key in ("declared", "declared_by", "impute_polarity")),
                )

    def test_outside_a_campaign_nothing_is_planned_and_nothing_converts(self) -> None:
        handoff = lease_tests.AnMzxmlUnitIsEligibleWithAConversionPlan._handoff(["FILES/a.mzXML"])
        handoff["technical_settings"]["ion_mode"] = "Negative"

        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual((False, "excluded"), (project["eligible"], project["selection_status"]))
        self.assertNotIn("conversion_plan", project)


class TheAgentStatusSaysSo(unittest.TestCase):
    def test_a_runner_can_check_for_the_imputation_before_it_relies_on_it(self) -> None:
        from msdial_app.agent_bridge import summarize_jobs

        self.assertIn("campaign_mzxml_polarity_from_declared_ion_mode", summarize_jobs({})["capabilities"])


class OutsideACampaignNothingChanges(_Scratch):
    def test_a_lease_without_a_campaign_authorization_imputes_and_converts_nothing(self) -> None:
        payloads = ADeclaredPolarityIsImputed.PAYLOADS

        # The unit declares its mzXML as its inputs, and outside a campaign no mzXML is one.
        with self.assertRaisesRegex(ValueError, "2 of its 2 declared analysis inputs are not in the download"):
            self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative"), campaign_authorization=None)

        manifest = read_manifest(self.root / "metabolights" / "MTBLS417" / "mtbls417-neg" / "provenance" / "run-manifest.json")
        self.assertEqual(("not_used", "No input conversion ran in this lease."),
                         (_stage(manifest, "convert")["status"], _stage(manifest, "convert")["reason"]))
        self.assertNotIn("input_conversions", manifest)
        self.assertFalse((Path(manifest["raw_directory"]) / "converted").exists())


# ---- what the gate reads ---------------------------------------------------------------------------------------


@unittest.skipUnless(GATE.is_file(), "the reanalysis gate (verify-run-invariants.py) is not on this machine")
class TheGateHoldsAnImputationToTheDeclaration(_Gate):
    """CONV-1 reads the raw owner's Catalog handoff (_declared_ion_mode) and fails a spectrum left without a
    polarity: run here, by the gate's own script, on a workspace these leases wrote."""

    PAYLOADS = ADeclaredPolarityIsImputed.PAYLOADS

    def test_conv1_passes_on_a_negative_unit_whose_polarity_was_imputed(self) -> None:
        manifest = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Negative", ion_mode="Negative"))

        checks = self.checks(manifest)

        conv = checks["CONV-1"]
        self.assertEqual("pass", conv["status"], conv["detail"])
        self.assertEqual((2, 2 * DDA_SCANS, "Negative"), (conv["evidence"]["converted_inputs"],
                                                         conv["evidence"]["imputed_polarity_spectra"],
                                                         conv["evidence"]["declared_ion_mode"]))
        self.assertEqual({"polarity_imputation": 2}, conv["evidence"]["inferences"])
        self.assertIn("by imputing negative polarity, the unit's declared ion mode from the Catalog handoff's",
                      conv["detail"])
        self.assertEqual("pass", checks["SUM-1"]["status"], checks["SUM-1"]["detail"])

    def test_conv1_fails_a_both_unit_that_imputes_nothing(self) -> None:
        manifest = self.lease(self.PAYLOADS, _handoff_project(self.PAYLOADS, "Both", ion_mode="Negative"))

        conv = self.checks(manifest)["CONV-1"]

        self.assertEqual("fail", conv["status"], conv["detail"])
        self.assertEqual((0, "Both"), (conv["evidence"]["imputed_polarity_spectra"], conv["evidence"]["declared_ion_mode"]))
        self.assertTrue(all(f"{DDA_SCANS} of its spectra carry no polarity and none was imputed" in problem
                            for problem in conv["evidence"]["problems"]), conv["evidence"]["problems"])

    def test_conv1_warns_of_the_file_whose_other_polarity_refused_the_imputation(self) -> None:
        payloads = {**self.PAYLOADS, "S03.mzXML": polarity_32([None, "+", None])}

        conv = self.checks(self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative")))["CONV-1"]

        self.assertEqual("warn", conv["status"], conv["detail"])
        self.assertIn("polarity imputation refused", conv["detail"])
        self.assertEqual((1, 2 * DDA_SCANS), (conv["evidence"]["failed"], conv["evidence"]["imputed_polarity_spectra"]))

    def test_each_part_of_a_split_unit_takes_its_parents_declaration(self) -> None:
        """A unit the Catalog declares Negative whose headers show two polarities: the DDA files that record none
        were imputed negative, the others record positive, and it splits by polarity. The positive part's
        project.ion_mode is now Positive; its conversions, and the declaration they stand for, are its parent's."""
        payloads = {
            "a_N_1.mzXML": _without_polarity(dda_32()), "a_N_2.mzXML": _without_polarity(dda_32()),
            "b_P_1.mzXML": dda_32(), "b_P_2.mzXML": dda_32(),
        }
        manifest = self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative", unit="mtbls9-x"))
        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        verdicts = {Path(name).with_suffix(".mzML").name: {"method": "DDA",
                                                            "polarity": "Negative" if "_N_" in name else "Positive"}
                    for name in payloads}
        self.assertEqual("split", self.preflight(path, verdicts)["campaign_disposition"]["disposition"])

        result = split_unit_by_acquisition(path, confirmed=True)

        self.assertTrue(result["written"], result["blockers"])
        parts = {item["polarity"]: item for item in result["parts"]}
        self.assertEqual({"Negative", "Positive"}, set(parts))
        for polarity, part in parts.items():
            with self.subTest(part=polarity):
                self.preflight(Path(part["manifest_path"]), verdicts)
                written = read_manifest(part["manifest_path"])
                project = written["project"]

                self.assertEqual(polarity, project["ion_mode"])
                self.assertEqual(_declaration("Negative", "negative", unit="mtbls9-x"),
                                 conversion_polarity_declaration(project))
                self.assertEqual(_declared("Negative", "negative"), project["conversion_plan"]["options"])
                conv = self.checks(written)["CONV-1"]
                self.assertEqual("pass", conv["status"], conv["detail"])
                self.assertEqual("Negative", conv["evidence"]["declared_ion_mode"])
                self.assertEqual(2 * DDA_SCANS if polarity == "Negative" else 0, conv["evidence"]["imputed_polarity_spectra"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
