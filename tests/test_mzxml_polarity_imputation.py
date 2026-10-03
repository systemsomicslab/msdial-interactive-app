"""A campaign's conversion gives an mzXML's polarity-less scans the polarity their unit declares, and only that.

MS-DIAL skips a spectrum whose polarity is not the method's ion mode, and RawDataHandler reads polarity only as
a spectrum cvParam, so an mzXML whose scans record none was converted to an mzML that runs to nothing, and the
gate's CONV-1 failed it. The user decided on 2026-10-02 that, in a campaign, the lease's convert stage imputes
the polarity from the unit's declared ion mode, and only where the unit's Catalog handoff declares exactly one
polarity (technical_settings.ion_mode Positive or Negative), the field CONV-1 holds an imputation to; it is
recorded as an inference with its count. Both, Unknown or nothing imputes nothing, and CONV-1 fails the unit.
project.ion_mode is never read: the raw-header preflight rewrites it from headers that carry what was imputed,
and a split part's is its part's polarity. The converter still refuses to impute for a file some of whose scans
record the other polarity and some none, and the user decided on 2026-10-03 that such a file is excluded, with
reason polarity_contradicts_declaration, and the rest of the unit runs: it is no conversion, and the gate's CONV-1
and INP-1 pass. A file every scan of which records the other polarity is converted as it records it, and the
preflight splits the unit by polarity. Outside a campaign nothing converts, as before.

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
    NO_CONVERTED_INPUT_REASON,
    POLARITY_CONTRADICTS_DECLARATION,
    RepositoryProject,
    conversion_polarity_declaration,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
)

from test_mzxml_conversion import dda_32, polarity_32, read_mzml
from test_mzxml_lease_conversion import GATE, OFF, _declared, _project, _Scratch, _stage, _truncated
from test_raw_metadata_preflight import _Extractor, _PinnedExtractor

# The cvParams of positive and negative scan polarity.
POSITIVE, NEGATIVE = "MS:1000130", "MS:1000129"
# The scans of a synthetic DDA file (dda_32), none of which records a polarity once _without_polarity is done.
DDA_SCANS = 4


def _without_polarity(data: bytes) -> bytes:
    """The synthetic mzXML with no scan recording a polarity, its embedded sha1 computed again over the span."""
    prefix = data.split(b"  <sha1>", 1)[0].replace(b' polarity="+"', b"") + b"  <sha1>"
    return prefix + hashlib.sha1(prefix).hexdigest().encode("ascii") + b"</sha1>\n</mzXML>\n"


# A unit of two synthetic DDA files recording no polarity, and a third recording + on one of its three scans and
# none on the others: a declared Negative cannot stand for those two.
REFUSING = {
    "S01.mzXML": _without_polarity(dda_32()),
    "S02.mzXML": _without_polarity(dda_32()),
    "S03.mzXML": polarity_32([None, "+", None]),
}
# The header verdicts of the two files that convert, as a Negative unit's preflight reads them.
NEGATIVE_DDA = {name: {"method": "DDA", "polarity": "Negative"} for name in ("S01.mzML", "S02.mzML")}


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


class ADeclaredPolarityIsImputed(_Gate):
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

    def test_a_file_whose_scans_contradict_the_declaration_is_excluded_and_the_rest_run(self) -> None:
        """The converter's own guard: a declared Negative cannot stand for scans beside one that records +. As the user
        decided on 2026-10-03, that file is excluded with its reason, unconverted, and the rest of the unit runs."""
        manifest = self.lease(REFUSING, _handoff_project(REFUSING, "Negative", ion_mode="Negative"))
        block = manifest["input_conversions"]
        source = str((Path(manifest["input_directory"]) / "S03.mzXML").resolve())

        # No conversion: the two files that converted are the records, and the refusal is kept beside them.
        self.assertEqual([("S01.mzXML", "converted"), ("S02.mzXML", "converted")],
                         [(record["source"]["name"], record["status"]) for record in block["records"]])
        (contradiction,) = block["polarity_contradictions"]
        self.assertEqual((source, POLARITY_CONTRADICTS_DECLARATION, "negative"),
                         (contradiction["mzxml"], contradiction["reason"], contradiction["declared_polarity"]))
        refusal = contradiction["record"]
        self.assertEqual(("failed", "polarity_imputation", _declared("Negative", "negative")),
                         (refusal["status"], refusal["refused_inference"], refusal["options"]))
        self.assertIn("polarity imputation refused: 1 scans record positive polarity", refusal["error"])
        self.assertFalse(Path(refusal["output"]["path"]).exists(), "nothing is written for it")
        self.assertEqual((3, 2, 0, 1, 2 * DDA_SCANS), tuple(block["counts"][key] for key in (
            "sources", "converted", "failed", POLARITY_CONTRADICTS_DECLARATION, "imputed_polarity_spectra")))
        self.assertEqual(1, _stage(manifest, "convert")[POLARITY_CONTRADICTS_DECLARATION])

        # Kept out of the candidates with its reason, and listed wherever a file the lease excluded is.
        self.assertEqual(["S01.mzML", "S02.mzML"], [Path(item).name for item in manifest["input_candidates"]])
        (excluded,) = manifest["excluded_input_candidates"]
        self.assertEqual((source, POLARITY_CONTRADICTS_DECLARATION, [refusal["error"]]),
                         (excluded["path"], excluded["reason"], excluded["problems"]))
        (row,) = manifest["input_lineage"]["excluded"]
        self.assertEqual((source, "S03", POLARITY_CONTRADICTS_DECLARATION),
                         (row["path"], row["sample_id"], row["exclusion"]["reason"]))
        self.assertEqual(["S01.mzML", "S02.mzML"],
                         [Path(row["path"]).name for row in manifest["input_lineage"]["rows"]])
        built = build_repository_analysis_rows(manifest)
        self.assertEqual([], built["failures"])
        self.assertEqual(["S01", "S02"], [item["sample_id"] for item in built["rows"]])
        self.assertEqual([(source, POLARITY_CONTRADICTS_DECLARATION, "S03")],
                         [(item["path"], item["reason"], item["sample_id"]) for item in built["excluded_inputs"]])
        self.assertTrue(manifest["execution_allowed"])
        self.assertEqual((2, 0, 1), tuple(manifest["project"]["conversion_plan"]["outcome"][key] for key in (
            "analysis_inputs", "failed", POLARITY_CONTRADICTS_DECLARATION)))

        # The campaign disposition lists it among its excluded inputs, and runs the rest.
        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        result = self.preflight(path, NEGATIVE_DDA)
        disposition = result["campaign_disposition"]
        self.assertEqual(("run", True), (disposition["disposition"], disposition["applied"]))
        self.assertEqual([(source, POLARITY_CONTRADICTS_DECLARATION)],
                         [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])
        self.assertEqual(("preflight_passed", True), (result["status"], result["execution_allowed"]))

    def test_a_file_whose_every_scan_records_the_other_polarity_converts_as_recorded_and_the_unit_splits(self) -> None:
        """Nothing is asked of such a file: no scan of it records none, so nothing is imputed and nothing contradicts.
        It converts with the polarity it records, and the polarity rule of the preflight splits it from the others."""
        payloads = {**self.PAYLOADS, "S03.mzXML": polarity_32(["+", "+", "+"])}

        manifest = self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative"))
        block = manifest["input_conversions"]
        opposite = block["records"][2]

        self.assertEqual(["converted"] * 3, [record["status"] for record in block["records"]])
        self.assertEqual([_declared("Negative", "negative")] * 3, [record["options"] for record in block["records"]])
        self.assertEqual(([], {"positive": 3}), (opposite["inferences"], opposite["counts"]["polarity"]))
        self.assertNotIn("refused_inference", opposite)
        _, spectra = read_mzml(Path(opposite["output"]["path"]))
        self.assertEqual([[POSITIVE]] * 3, [[term for term in (POSITIVE, NEGATIVE) if term in spectrum["cv"]]
                                            for spectrum in spectra])
        self.assertEqual(([], 0), (block["polarity_contradictions"], block["counts"][POLARITY_CONTRADICTS_DECLARATION]))
        self.assertEqual(2 * DDA_SCANS, block["counts"]["imputed_polarity_spectra"])
        self.assertNotIn("excluded_input_candidates", manifest)
        self.assertEqual(["S01.mzML", "S02.mzML", "S03.mzML"],
                         [Path(item).name for item in manifest["input_candidates"]])

        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        disposition = self.preflight(
            path, {**NEGATIVE_DDA, "S03.mzML": {"method": "DDA", "polarity": "Positive"}}
        )["campaign_disposition"]

        self.assertEqual("split", disposition["disposition"], disposition["detail"])
        self.assertEqual([], disposition["excluded_inputs"])

    def test_a_re_lease_excludes_the_file_again_and_reuses_the_rest(self) -> None:
        """The same bytes are refused again: nothing of them was converted to be reused, and the rest is reused."""
        first = self.lease(REFUSING, _handoff_project(REFUSING, "Negative", ion_mode="Negative"))
        again = self.lease(REFUSING, _handoff_project(REFUSING, "Negative", ion_mode="Negative"))
        records = again["input_conversions"]["records"]

        self.assertEqual([True, True], [record["reused_previous_record"] for record in records])
        self.assertEqual([record["output"]["sha256"] for record in first["input_conversions"]["records"]],
                         [record["output"]["sha256"] for record in records])
        self.assertEqual([POLARITY_CONTRADICTS_DECLARATION],
                         [item["reason"] for item in again["excluded_input_candidates"]])
        self.assertEqual((2, 1), (_stage(again, "convert")["reused"],
                                  _stage(again, "convert")[POLARITY_CONTRADICTS_DECLARATION]))

    def test_a_conversion_another_declaration_left_is_removed_once_the_file_contradicts(self) -> None:
        """Declared Both, nothing is asked of the file, so nothing contradicts: it converts with its polarity-less scans
        unrecorded, and CONV-1 fails them. Once the Catalog corrects the unit to Negative the file contradicts it, and
        the mzML the earlier lease wrote is removed, so that no input is discovered for it."""
        both = self.lease(REFUSING, _handoff_project(REFUSING, "Both", ion_mode="Negative"))
        written = both["input_conversions"]["records"][2]
        self.assertEqual(({"positive": 1, "unrecorded": 2}, []),
                         (written["counts"]["polarity"], both["input_conversions"]["polarity_contradictions"]))
        self.assertNotIn("excluded_input_candidates", both)

        corrected = self.lease(REFUSING, _handoff_project(REFUSING, "Negative", ion_mode="Negative"))

        (contradiction,) = corrected["input_conversions"]["polarity_contradictions"]
        self.assertTrue(contradiction["record"]["output"].get("removed_after_failure"))
        self.assertFalse(Path(written["output"]["path"]).exists())
        self.assertEqual(["S01.mzML", "S02.mzML"], [Path(item).name for item in corrected["input_candidates"]])
        self.assertEqual([False, False],
                         [record["reused_previous_record"] for record in corrected["input_conversions"]["records"]])

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


class AUnitLeftWithNoInputIsSkipped(_Gate):
    def test_a_unit_whose_every_mzxml_contradicts_the_declaration_is_recorded_and_its_preflight_skips_it(self) -> None:
        cases = {
            "every file contradicts": (
                {"S01.mzXML": polarity_32([None, "+", None])}, [POLARITY_CONTRADICTS_DECLARATION]
            ),
            "and the other fails": (
                {"S01.mzXML": polarity_32([None, "+", None]), "S02.mzXML": _truncated()},
                [CONVERSION_FAILED, POLARITY_CONTRADICTS_DECLARATION],
            ),
        }
        for label, (payloads, reasons) in cases.items():
            with self.subTest(label), tempfile.TemporaryDirectory() as temporary:
                self.root = Path(temporary).resolve()
                self._extractor = None

                manifest = self.lease(payloads, _handoff_project(payloads, "Negative", ion_mode="Negative"))

                self.assertEqual(("prepared", [], False),
                                 (manifest["status"], manifest["input_candidates"], manifest["execution_allowed"]))
                project = manifest["project"]
                self.assertEqual(("excluded", False), (project["selection_status"], project["eligible"]))
                (reason,) = [
                    item for item in project["exclusion_reasons"] if item.startswith(NO_CONVERTED_INPUT_REASON)
                ]
                self.assertEqual([True] * len(reasons), [f"({item})" in reason for item in reasons], reason)

                path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
                result = self.preflight(path, {})
                disposition = result["campaign_disposition"]
                self.assertEqual(("skip", ["no_inputs", *reasons], True),
                                 (disposition["disposition"], disposition["reasons"], disposition["applied"]))
                self.assertEqual(("skipped_by_preflight", False), (result["status"], result["execution_allowed"]))


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

    def test_conv1_and_inp1_pass_on_a_unit_whose_contradicting_file_was_excluded(self) -> None:
        """The file was excluded with its reason, as the user decided on 2026-10-03, and is no conversion: CONV-1 has
        no conversion of it to hold, and INP-1 accounts for the declared input through the campaign disposition that
        lists it. Neither stops the rest of the unit."""
        manifest = self.lease(REFUSING, _handoff_project(REFUSING, "Negative", ion_mode="Negative"))
        path = Path(manifest["workspace"]) / "provenance" / "run-manifest.json"
        self.preflight(path, NEGATIVE_DDA)

        checks = self.checks(read_manifest(path))

        conv, inp = checks["CONV-1"], checks["INP-1"]
        self.assertEqual("pass", conv["status"], conv["detail"])
        self.assertEqual((2, 2, 0, 2 * DDA_SCANS), tuple(conv["evidence"][key] for key in (
            "records", "converted_inputs", "failed", "imputed_polarity_spectra")))
        self.assertEqual("pass", inp["status"], inp["detail"])
        self.assertEqual(["S03.mzXML"], inp["evidence"]["excluded"])
        for check_id in ("ELIG-1", "SUM-1", "CNT-1", "ACQ-1"):
            self.assertEqual("pass", checks[check_id]["status"], f"{check_id}: {checks[check_id]['detail']}")

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
