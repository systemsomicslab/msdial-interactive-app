"""CID/HCD only, and a Both unit split by polarity: the user's decisions of 2026-10-29 for the campaign.

Every reference spectrum the campaign annotates with is CID/HCD-derived, so a campaign lease keeps out an input whose
evidence names another fragmentation method: its file name (MB-POST MPST000010 names its OAD files
"..._OAD_NEG_ddMS_10eV_..." and its EAD files "..._EAD14eV_POS_..."), an mzML's activation terms, a converted mzXML's
activationMethod. Absence of evidence means run. A unit whose repository record says Both is admitted: each file's
polarity is its header's, the unit splits by polarity, and a file whose scans switch polarity is excluded while the
rest runs.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.fragmentation import (
    CID_HCD_ONLY_RULE,
    EVIDENCE_FILE_NAME,
    EVIDENCE_MZML_ACTIVATION,
    EVIDENCE_MZXML_ACTIVATION,
    EVIDENCE_SAMPLE_ROW,
    NOT_CID_HCD,
    RAW_HEADER_NOT_READ,
    classify_activation_term,
    classify_activation_text,
    exclude_non_cid_inputs,
    instrument_can,
    methods_in_file_name,
    methods_named,
    stated_methods,
)
from msdial_app import mcp_server
from msdial_app.mzml_encoding import scan_mzml_encoding
from msdial_app.raw_metadata_preflight import decide_disposition
from msdial_app.repository_reanalysis import (
    RepositoryFile,
    RepositoryProject,
    create_download_lease,
    read_manifest,
    split_unit_by_acquisition,
)

from test_download_lease_record import _Client, _project
from test_mzml_encoding import good_spectrum_arrays, mzml
from test_mzxml_conversion import dda_32
from test_raw_metadata_preflight import _APPROVAL, _header, _manifest


def _activation(name: str, accession: str) -> str:
    return (
        '<precursorList count="1"><precursor><selectedIonList count="1"><selectedIon>'
        '<cvParam cvRef="MS" accession="MS:1000744" name="selected ion m/z" value="250.5"/>'
        "</selectedIon></selectedIonList><activation>"
        f'<cvParam cvRef="MS" accession="{accession}" name="{name}" value=""/>'
        '<cvParam cvRef="MS" accession="MS:1000045" name="collision energy" value="14"/>'
        "</activation></precursor></precursorList>"
    )


def _spectra(activation: str = "") -> list[str]:
    """An MS1 then an MS2 spectrum whose precursor carries ``activation``, repeated."""
    arrays = "".join(good_spectrum_arrays())
    spectra = []
    for index in range(6):
        level = 1 + index % 2
        precursor = activation if level == 2 else ""
        spectra.append(
            f'<spectrum index="{index}" id="scan={index + 1}" defaultArrayLength="3">'
            f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{level}"/>{precursor}'
            f'<binaryDataArrayList count="2">{arrays}</binaryDataArrayList></spectrum>\n'
        )
    return spectra


def _resealed(data: bytes) -> bytes:
    """An edited mzXML with its embedded sha1 computed again, as test_mzxml_conversion writes it."""
    prefix = data[: data.index(b"  <sha1>")] + b"  <sha1>"
    return prefix + hashlib.sha1(prefix).hexdigest().encode("ascii") + b"</sha1>\n</mzXML>\n"


EAD = _activation("electron activated dissociation", "MS:1003294")
CID = _activation("collision-induced dissociation", "MS:1000133")
ETHCD = _activation("electron transfer/higher-energy collision dissociation", "MS:1002631")


class FileNamesNameAMethodByAWholeToken(unittest.TestCase):
    def test_mpst000010_names(self) -> None:
        self.assertEqual(["OAD"], methods_in_file_name("20240111_EquiSPLASH_2ugmL_OAD_NEG_ddMS_10eV_3_009.lcd"))
        self.assertEqual(["EAD"], methods_in_file_name("20240510_Marmoset_4_HC_5fold_EAD14eV_POS_1.wiff2"))
        self.assertEqual(["OAD"], methods_in_file_name("raw/20240614_UltimateSPLASH_OAD_x50_NEG_30eV_ddMS_4_032.lcd"))
        self.assertEqual([], methods_in_file_name("20240130_ISD_NEG_1_006.lcd"))
        self.assertEqual(["ETD"], methods_in_file_name("C:\\data\\run-etd-01.d\\"))
        self.assertEqual(["EThcD", "UVPD"], methods_named("lipid_EThcD_then_UVPD_EThcD.raw"))

    def test_a_token_inside_a_word_or_a_checksum_names_nothing(self) -> None:
        for name in ("load_curve.raw", "head_01.mzML", "Ready.wiff2", "CID_30eV.raw", "HCD_stepped.raw",
                     "95c44b45-8ead-43e8-afd1-de952d13d4ae_1.mcf", "a18ead97-4889-44a7-a27b-4be0beb87a1d_1.mcf",
                     "ecd4f2a19b0c4d1e9f8a7b6c5d4e3f21", "EADx.raw", "OADS.raw"):
            with self.subTest(name=name):
                self.assertEqual([], methods_named(name))

    def test_an_identifier_names_nothing(self) -> None:
        # A hyphenated GUID, and an all-hexadecimal run that holds a digit, are identifiers (review L1).
        for name in ("raw/x.d/95c44b45-ead1-43e8-afd1-de952d13d4ae_1.mcf", "ecd2_scan.raw", "S_ead1.raw",
                     "a-ECD0-b.raw"):
            with self.subTest(name=name):
                self.assertEqual([], methods_in_file_name(name))
        self.assertEqual(["EAD"], methods_in_file_name("S_EAD_1.raw"))
        self.assertEqual(["EDD"], methods_in_file_name("x_EDD_y.raw"))

    def test_a_row_states_a_method_only_in_a_field_that_states_one(self) -> None:
        self.assertEqual(["OAD"], stated_methods({"analyticalCondition / Fragmentation method": "OAD",
                                                  "Extract Name": "X_EAD_1"}))
        self.assertEqual(["EAD"], stated_methods({"Dissociation": "electron-activated dissociation"}))
        self.assertEqual([], stated_methods({"Parameter Value[Instrument] collision cell": "Orbitrap Velos ETD"}))

    def test_activation_terms_and_texts_are_classified(self) -> None:
        self.assertEqual("other_method", classify_activation_term("electron activated dissociation"))
        self.assertEqual("other_method", classify_activation_term("electron transfer/higher-energy collision dissociation"))
        self.assertEqual("other_method", classify_activation_term("ultraviolet photodissociation"))
        self.assertEqual("collisional", classify_activation_term("beam-type collision-induced dissociation"))
        self.assertEqual("collisional", classify_activation_term("pulsed q dissociation"))
        self.assertEqual("unclassified", classify_activation_term("sustained off-resonance irradiation"))
        self.assertEqual("other_method", classify_activation_text("EThcD"))
        self.assertEqual("collisional", classify_activation_text("HCD"))
        self.assertEqual("unclassified", classify_activation_text("unrecorded"))


class ANameCountsOnlyOnAnInstrumentThatCouldFragmentSo(unittest.TestCase):
    """User decision, 2026-10-29: a file name's token is credible only where the unit's instrument could perform the
    method, or none is recorded; an instrument's model name is never evidence of a method itself."""

    def test_capable_families_vendors_and_unrecorded_instruments(self) -> None:
        cases = [
            ("EAD", "Nexera/ZenoTof 7600", True), ("EAD", "SCIEX", True), ("EAD", "", True), ("EAD", "Unknown", True),
            ("EAD", "Thermo Scientific Exactive", False), ("EAD", "TripleTOF 6600", False),
            ("OAD", "LCMS-9030　Shimadzu", True), ("OAD", "Shimadzu, LCMS-9050", True), ("OAD", "Shimadzu", True),
            ("OAD", "LCMS-8060 (Shimadzu Co.)", False),
            ("ETD", "Thermo Scientific LTQ Orbitrap Velos ETD", True), ("ETD", "Orbitrap Fusion Lumos", True),
            ("ETD", "Thermo Fisher Scientific", True), ("ETD", "Q Exactive HF (Thermo Fisher Scientific Inc.)", False),
            ("ECD", "Bruker solariX 7T", True), ("ECD", "Agilent 6545XT AdvanceBio LC/Q-TOF with ExD", True),
            ("ECD", "Agilent 6545 Q-TOF", False), ("UVPD", "Orbitrap Eclipse", True), ("UVPD", "Q Exactive", False),
            ("IRMPD", "Thermo LTQ", True), ("EID", "Waters Xevo G2-XS", False),
        ]
        for method, instrument, capable in cases:
            with self.subTest(method=method, instrument=instrument):
                self.assertIs(capable, instrument_can(method, instrument))

    def test_a_name_an_instrument_could_not_produce_keeps_nothing_out_and_is_recorded(self) -> None:
        names = ["AA_EAD_A_1.raw", "AA_EAD_A_2.raw"]
        kept, excluded, record = exclude_non_cid_inputs(names, instrument="Thermo Scientific Exactive")

        self.assertEqual((names, []), (kept, excluded))
        self.assertEqual("Thermo Scientific Exactive", record["instrument"])
        self.assertEqual({"count": 2, "examples": [{"name": "AA_EAD_A_1.raw", "methods": ["EAD"]},
                                                    {"name": "AA_EAD_A_2.raw", "methods": ["EAD"]}]},
                         record["names_not_credible_on_instrument"])
        kept, excluded, _record = exclude_non_cid_inputs(names, instrument="ZenoTOF 7600")
        self.assertEqual(([], names), (kept, [item["path"] for item in excluded]))

    def test_an_activation_term_is_not_held_to_the_instrument(self) -> None:
        scans = {"s.mzML": {"activation": [{"accession": "MS:1003294", "name": "electron activated dissociation",
                                            "count": 1}]}}
        _kept, excluded, _record = exclude_non_cid_inputs(["s.mzML"], scans, instrument="Q Exactive")

        self.assertEqual([EVIDENCE_MZML_ACTIVATION], [item["evidence"] for item in excluded])


class TheScanRecordsActivationTerms(unittest.TestCase):
    def test_terms_but_energies_are_counted(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "a.mzML"
            path.write_bytes(mzml(_spectra(EAD)))
            scan = scan_mzml_encoding(path)

        self.assertEqual("scanned", scan["status"])
        self.assertEqual([], scan["problems"])
        self.assertEqual([("MS:1003294", "electron activated dissociation")],
                         [(item["accession"], item["name"]) for item in scan["activation"]])
        self.assertGreater(scan["activation"][0]["count"], 0)


class TheRuleKeepsOutWhatEvidenceNames(unittest.TestCase):
    def test_name_header_and_conversion_evidence(self) -> None:
        scans = {"s9.mzML": {"activation": [{"accession": "MS:1003294", "name": "electron activated dissociation",
                                              "count": 3}]},
                 "cid.mzML": {"activation": [{"accession": "MS:1000133", "name": "collision-induced dissociation",
                                              "count": 3}]}}
        conversions = {"converted.mzML": {"EThcD": 4, "unrecorded": 1}}
        inputs = ["S1_OAD_NEG.lcd", "s9.mzML", "cid.mzML", "converted.mzML", "S2.wiff2", "S3.raw"]

        kept, excluded, record = exclude_non_cid_inputs(inputs, scans, conversions)

        self.assertEqual(["cid.mzML", "S2.wiff2", "S3.raw"], kept)
        self.assertEqual(
            [("S1_OAD_NEG.lcd", EVIDENCE_FILE_NAME, ["OAD"]), ("s9.mzML", EVIDENCE_MZML_ACTIVATION,
                                                              ["electron activated dissociation"]),
             ("converted.mzML", EVIDENCE_MZXML_ACTIVATION, ["EThcD"])],
            [(item["path"], item["evidence"], item["methods"]) for item in excluded],
        )
        for item in excluded:
            self.assertEqual((NOT_CID_HCD, CID_HCD_ONLY_RULE), (item["reason"], item["rule"]))
            self.assertIn("CID/HCD-derived", item["detail"])
        self.assertEqual(3, record["excluded"])
        self.assertEqual({".mzml": 3}, record["raw_header"]["read"])
        # Never inferred: a vendor file's method was not read, and the wiff2 reader's CID is no evidence.
        self.assertEqual({".lcd": 1, ".raw": 1, ".wiff2": 1}, record["raw_header"][RAW_HEADER_NOT_READ])
        self.assertIn("Wiff2Reader", record["raw_header"]["not_read_detail"])

    def test_absence_of_evidence_runs(self) -> None:
        kept, excluded, record = exclude_non_cid_inputs(["a.wiff2", "b.d"])

        self.assertEqual((["a.wiff2", "b.d"], []), (kept, excluded))
        self.assertEqual(0, record["excluded"])


class TheCampaignLeaseKeepsThemOut(unittest.TestCase):
    def _lease(self, root: Path, payloads: dict[str, bytes], **options) -> dict:
        files = [RepositoryFile(f"FILES/{name}", len(data), f"https://example.org/{name}") for name, data in payloads.items()]
        project = _project(files, list(payloads))
        client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
        lease = create_download_lease(project, root, 10_000_000, client=client, **options)
        return read_manifest(lease["manifest_path"])

    def test_a_named_and_an_activated_file_are_excluded_and_the_rest_runs(self) -> None:
        payloads = {"S1_CID.mzML": mzml(_spectra(CID)), "S2_EAD14eV.mzML": mzml(_spectra(CID)),
                    "S3.mzML": mzml(_spectra(ETHCD))}
        with tempfile.TemporaryDirectory() as temporary:
            written = self._lease(Path(temporary), payloads, campaign_authorization=dict(_APPROVAL))

        self.assertEqual(["S1_CID.mzML"], [Path(item).name for item in written["input_candidates"]])
        excluded = {Path(item["path"]).name: item for item in written["excluded_input_candidates"]}
        self.assertEqual({"S2_EAD14eV.mzML", "S3.mzML"}, set(excluded))
        self.assertEqual((NOT_CID_HCD, EVIDENCE_FILE_NAME), (excluded["S2_EAD14eV.mzML"]["reason"],
                                                              excluded["S2_EAD14eV.mzML"]["evidence"]))
        self.assertEqual(EVIDENCE_MZML_ACTIVATION, excluded["S3.mzML"]["evidence"])
        self.assertEqual({"S2_EAD14eV.mzML", "S3.mzML"},
                         {Path(row["path"]).name for row in written["input_lineage"]["excluded"]})
        self.assertEqual(0, written["ignored_input_candidate_count"])
        check = written["fragmentation_check"]
        self.assertEqual((CID_HCD_ONLY_RULE, 3, 2), (check["rule"], check["inputs_checked"], check["excluded"]))
        attribute = next(stage for stage in written["lease_stages"] if stage["stage"] == "attribute")
        self.assertEqual(2, attribute["not_cid_hcd_excluded"])

    def test_a_converted_mzxml_whose_activation_method_is_another_is_excluded(self) -> None:
        payloads = {"S1.mzXML": _resealed(dda_32().replace(b'activationMethod="CID"', b'activationMethod="EThcD"'))}
        files = [RepositoryFile(f"FILES/{name}", len(data), f"https://example.org/{name}", role="requires_conversion")
                 for name, data in payloads.items()]
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS-FRAG", analysis_unit_id="unit-f", eligible=True,
            selection_status="eligible", separation="LC-MS", acquisition_mode="DDA", ion_mode="Positive",
            untargeted=True, files=files, total_download_bytes=sum(item.size_bytes for item in files), sample_count=1,
            sample_metadata=[{"sample_id": "S1", "raw_file": "FILES/S1.mzXML", "values": {}}],
        )
        with tempfile.TemporaryDirectory() as temporary:
            client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
            lease = create_download_lease(project, Path(temporary), 10**9, client=client,
                                          campaign_authorization=dict(_APPROVAL))
            written = read_manifest(lease["manifest_path"])

        self.assertEqual([], written["input_candidates"])
        (item,) = written["excluded_input_candidates"]
        self.assertEqual((NOT_CID_HCD, ["EThcD"]), (item["reason"], item["methods"]))
        self.assertEqual(EVIDENCE_MZXML_ACTIVATION, item["evidence"])

    def test_the_handoffs_instrument_decides_whether_a_name_is_credible(self) -> None:
        payloads = {"AA_EAD_A_1.mzML": mzml(_spectra(CID))}
        files = [RepositoryFile(f"FILES/{name}", len(data), f"https://example.org/{name}") for name, data in payloads.items()]
        project = _project(files, list(payloads))
        project.repository_metadata = {"catalog_handoff": {"technical_settings": {"instrument": "Thermo Scientific Exactive"}}}
        with tempfile.TemporaryDirectory() as temporary:
            client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
            lease = create_download_lease(project, Path(temporary), 10_000_000, client=client,
                                          campaign_authorization=dict(_APPROVAL))
            written = read_manifest(lease["manifest_path"])

        self.assertEqual(["AA_EAD_A_1.mzML"], [Path(item).name for item in written["input_candidates"]])
        self.assertEqual(1, written["fragmentation_check"]["names_not_credible_on_instrument"]["count"])

    def test_a_sample_row_that_states_a_method_keeps_its_files_out(self) -> None:
        # Review M2: the plan maps a row that states the method to its files, and the lease keeps the same files out.
        payloads = {"S1.mzML": mzml(_spectra(CID)), "S2.mzML": mzml(_spectra(CID))}
        files = [RepositoryFile(f"FILES/{name}", len(data), f"https://example.org/{name}") for name, data in payloads.items()]
        project = _project(files, list(payloads))
        project.sample_metadata[1]["values"] = {"analyticalCondition / Fragmentation method": "EAD"}
        with tempfile.TemporaryDirectory() as temporary:
            client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
            lease = create_download_lease(project, Path(temporary), 10_000_000, client=client,
                                          campaign_authorization=dict(_APPROVAL))
            written = read_manifest(lease["manifest_path"])

        self.assertEqual(["S1.mzML"], [Path(item).name for item in written["input_candidates"]])
        (item,) = written["excluded_input_candidates"]
        self.assertEqual(("S2.mzML", EVIDENCE_SAMPLE_ROW, ["EAD"]), (Path(item["path"]).name, item["evidence"], item["methods"]))

    def test_outside_a_campaign_nothing_is_kept_out(self) -> None:
        payloads = {"S1_EAD.mzML": mzml(_spectra(EAD))}
        with tempfile.TemporaryDirectory() as temporary:
            written = self._lease(Path(temporary), payloads)

        self.assertEqual(["S1_EAD.mzML"], [Path(item).name for item in written["input_candidates"]])
        self.assertNotIn("excluded_input_candidates", written)
        self.assertNotIn("fragmentation_check", written)


class TheDispositionAccountsForThem(unittest.TestCase):
    KEPT_OUT = {"path": "S2_OAD.lcd", "reason": NOT_CID_HCD, "evidence": EVIDENCE_FILE_NAME, "methods": ["OAD"]}

    def test_the_rest_of_the_unit_runs(self) -> None:
        manifest = _manifest([_header("S1.lcd", "DDA")], declared={"acquisition_mode": "DDA"})
        manifest["excluded_input_candidates"] = [dict(self.KEPT_OUT)]

        disposition = decide_disposition(manifest)

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual([("S2_OAD.lcd", NOT_CID_HCD)],
                         [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])

    def test_a_unit_left_with_no_input_is_excluded_or_skipped(self) -> None:
        manifest = _manifest([])
        manifest["input_candidates"] = []
        manifest["excluded_input_candidates"] = [dict(self.KEPT_OUT)]
        alone = decide_disposition(manifest)
        manifest["excluded_input_candidates"].append({"path": "S3.mzXML", "reason": "conversion_failed"})
        beside = decide_disposition(manifest)

        self.assertEqual(("exclude", ["no_inputs", NOT_CID_HCD]), (alone["disposition"], alone["reasons"]))
        self.assertIn("CID/HCD-derived", " ".join(alone["detail"]))
        self.assertEqual(("skip", ["no_inputs", NOT_CID_HCD, "conversion_failed"]),
                         (beside["disposition"], beside["reasons"]))


class ABothUnitSplitsByPolarity(unittest.TestCase):
    """A unit whose repository record says Both: each file's polarity is its header's."""

    def test_separate_files_split_and_a_switching_file_is_excluded(self) -> None:
        disposition = decide_disposition(_manifest(
            [_header("pos.wiff2", "DDA", polarity="Positive"), _header("neg.wiff2", "DDA", polarity="Negative"),
             _header("both.raw", "DDA", polarity="PolaritySwitching")],
            declared={"acquisition_mode": "DDA", "ion_mode": "Both"},
        ))

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(["polarity"], disposition["split_key"]["by"])
        self.assertEqual(["neg", "pos"], [group["part_key"] for group in disposition["split_key"]["groups"]])
        self.assertEqual([("both.raw", "polarity_switching")],
                         [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])
        self.assertIn("unresolved mixed polarity", " ".join(disposition["detail"]))

    def test_a_file_whose_header_gives_no_polarity_is_excluded_and_the_rest_runs(self) -> None:
        disposition = decide_disposition(_manifest(
            [_header("pos.raw", "DDA", polarity="Positive"), _header("unknown.raw", "DDA", polarity="Unknown")],
            declared={"acquisition_mode": "DDA", "ion_mode": "Both"},
        ))

        self.assertEqual(("run", "Positive"), (disposition["disposition"], disposition["ion_mode"]))
        self.assertEqual([("unknown.raw", "polarity_unresolved")],
                         [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])

    def test_a_unit_of_switching_files_only_is_excluded(self) -> None:
        disposition = decide_disposition(_manifest(
            [_header("a.raw", "DDA", polarity="PolaritySwitching")], declared={"ion_mode": "Both"}))

        self.assertEqual(("exclude", ["polarity_switching_out_of_scope"]),
                         (disposition["disposition"], disposition["reasons"]))


class TheSplitToolCarriesEachPartsPolarity(unittest.TestCase):
    """Review M1: msdial_split_repository_unit projects each part of the real split, its polarity and split key among
    them, so a campaign records a part of a Both unit in its own polarity."""

    def test_a_real_polarity_split_reaches_the_tool_result(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "mb_post" / "MPST1" / "uboth"
            data = root / "raw" / "data"
            data.mkdir(parents=True)
            (root / "provenance").mkdir()
            files = []
            for name in ("S1_pos.wiff2", "S1_neg.wiff2", "S2_pos.wiff2", "S2_neg.wiff2"):
                (data / name).write_bytes(b"x")
                files.append(str(data / name))
            per_file = [{"file": item, "acquisition_mode": "DDA", "ms_levels": [1, 2], "outcome": "ok",
                         "polarity": "Positive" if "_pos" in Path(item).name else "Negative"} for item in files]
            manifest = {
                "schema": "msdial-public-reanalysis-run.v1", "status": "preflight_review_required", "workspace": str(root),
                "raw_directory": str(root / "raw"), "input_directory": str(data), "input_candidates": files,
                "project": {"analysis_unit_id": "uboth", "repository": "mb_post", "accession": "MPST1", "ion_mode": "Both",
                            "acquisition_mode": "DDA", "separation": "LC-MS", "untargeted": True,
                            "sample_metadata": [{"sample_id": Path(item).stem, "raw_file": Path(item).name}
                                                for item in files]},
                "raw_metadata_preflight": {"summary": {"coverage": {"complete": True}, "acquisition_mode": "DDA",
                                                       "per_file": per_file}},
            }
            path = root / "provenance" / "run-manifest.json"
            path.write_text(json.dumps(manifest), encoding="utf-8")
            split = split_unit_by_acquisition(path, confirmed=True)
            with patch.object(mcp_server, "_request_json", return_value=split):
                result = mcp_server.msdial_split_repository_unit(manifest_path=str(path), confirmed=True)

        self.assertEqual([], split["blockers"])
        self.assertEqual(
            {"uboth-dda-pos": "Positive", "uboth-dda-neg": "Negative"},
            {part["analysis_unit_id"]: part["polarity"] for part in result["parts"]},
        )
        self.assertEqual({"Positive", "Negative"}, {part["split_key"]["polarity"] for part in result["parts"]})


if __name__ == "__main__":
    unittest.main()
