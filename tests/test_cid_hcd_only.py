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
import tempfile
import unittest
from pathlib import Path

from msdial_app.fragmentation import (
    CID_HCD_ONLY_RULE,
    EVIDENCE_FILE_NAME,
    EVIDENCE_MZML_ACTIVATION,
    EVIDENCE_MZXML_ACTIVATION,
    NOT_CID_HCD,
    RAW_HEADER_NOT_READ,
    classify_activation_term,
    classify_activation_text,
    exclude_non_cid_inputs,
    methods_in_file_name,
    methods_named,
)
from msdial_app.mzml_encoding import scan_mzml_encoding
from msdial_app.raw_metadata_preflight import decide_disposition
from msdial_app.repository_reanalysis import (
    RepositoryFile,
    RepositoryProject,
    create_download_lease,
    read_manifest,
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


if __name__ == "__main__":
    unittest.main()
