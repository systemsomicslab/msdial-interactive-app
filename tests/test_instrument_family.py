"""The instrument family behind the peak-count diagnostic's threshold step.

Before 0.5.28 the family came from the file format alone and every mzML was QTOF. The pilot's ST004304 is a
Thermo Q Exactive published as mzML: its diagnostic recorded instrument_family QTOF and threshold_step 100
where Fourier-transform data take 1,000 (MTBLS2207, an Orbitrap ID-X published as mzML, likewise). Now an
mzML's header is read for its instrument, Orbitrap-class instruments and FT-ICRs are Fourier-transform, and
where the file's format leaves the family at a default, a repository's declared instrument may name it.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import representative_instrument_family, select_peak_tuning_representative
from msdial_app.repository_reanalysis import declared_instrument
from msdial_app.workflow import detect_raw_format, instrument_family_from_text


def _mzml(path: Path, instrument: str, before: str = "") -> Path:
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?>\n<indexedmzML><mzML>\n'
        '<fileDescription><sourceFileList count="1"><sourceFile id="RAW1" name="QE_orbitrap_run.raw" location="file:///"/>'
        "</sourceFileList></fileDescription>\n"
        + before
        + '<instrumentConfigurationList count="1"><instrumentConfiguration id="IC1">\n'
        + instrument
        + "</instrumentConfiguration></instrumentConfigurationList>\n"
        '<run id="r1" defaultInstrumentConfigurationRef="IC1"><spectrumList count="0"/></run></mzML></indexedmzML>\n',
        encoding="utf-8",
    )
    return path


def _cv(name: str) -> str:
    return f'<cvParam cvRef="MS" accession="MS:0000000" name="{name}" value=""/>\n'


def _user(name: str, value: str) -> str:
    return f'<userParam name="{name}" value="{value}"/>\n'


ORBITRAP_ANALYZER = '<componentList count="1"><analyzer order="2">' + _cv("orbitrap") + "</analyzer></componentList>\n"
FTICR_ANALYZER = (
    '<componentList count="1"><analyzer order="2">'
    + _cv("fourier transform ion cyclotron resonance mass spectrometer")
    + "</analyzer></componentList>\n"
)
TOF_ANALYZER = '<componentList count="1"><analyzer order="2">' + _cv("time-of-flight") + "</analyzer></componentList>\n"


class TheNamesThatAreFourierTransform(unittest.TestCase):
    FOURIER = [
        # PSI-MS CV model names (every Orbitrap and FT-ICR term under the Thermo and Bruker branches is checked
        # against psi-ms.obo when this was written; these are a sample).
        "Q Exactive", "Q Exactive HF", "Q Exactive HF-X", "Q Exactive Plus", "Exactive", "Exactive Plus",
        "Orbitrap Exploris 480", "Orbitrap Fusion", "Orbitrap Fusion Lumos", "Orbitrap Eclipse", "Orbitrap Ascend",
        "Orbitrap Astral", "LTQ Orbitrap", "LTQ Orbitrap XL", "LTQ Orbitrap Velos", "Orbitrap Velos Pro",
        "Orbitrap Elite", "Orbitrap ID-X", "Orbitrap IQ-X", "orbitrap",
        # Repository free text, as the pilot's catalog rows carry it.
        "Thermo Fusion Tribrid Orbitrap", "Thermo Q Exactive HF hybrid Orbitrap", "Thermo Q Exactive Orbitrap",
        "LC, Nexera X2 (Shimadzu Co.); MS, Q Exactive HF (Thermo Fisher Scientific Inc.)",
        "Thermo Scientific Orbitrap ID-X Tribrid", "Thermo Exploris 240", "Thermo IQ-X tribrid",
    ]
    FT_ICR = ["solariX", "solariX XR", "apex ultra", "APEX-Qe", "Bruker APEX-Qe 9.4T", "scimaX", "LTQ FT",
              "LTQ FT Ultra", "fourier transform ion cyclotron resonance mass spectrometer"]
    NOT_FOURIER = [
        "LTQ", "LTQ Velos", "Velos Plus", "Velos Pro", "LTQ XL", "TSQ Altis", "TSQ Quantiva", "ISQ", "Stellar",
        "EVOQ Elite", "maXis", "impact II", "timsTOF Pro", "Xevo G2-XS QTof", "Synapt G2-Si", "TripleTOF 5600",
        "QTRAP 6500", "6545 Q-TOF LC/MS", "LCMS-9030", "time-of-flight",
        "Bruker maXis UHR-ToF", "Bruker impact II UHR-TOF", "Waters Xevo G2 QTof", "AB SCIEX TripleTOF 5600+",
        "Waters Acquity UPLC", "Nexera X2 (Shimadzu)", "Agilent 1100 HPLC (Agilent Technologies)",
        "Bruker Elute UHPLC system",
        # HPLC columns a repository's free text can carry beside the instrument.
        "Agilent Zorbax Eclipse Plus C18", "Phenomenex Synergi Fusion-RP",
    ]

    def test_orbitrap_class_instruments_are_fourier_transform(self) -> None:
        for name in self.FOURIER:
            with self.subTest(name=name):
                self.assertEqual(("Fourier-transform MS", name), instrument_family_from_text([name]))

    def test_ft_icr_instruments_are_ft_icr(self) -> None:
        for name in self.FT_ICR:
            with self.subTest(name=name):
                self.assertEqual(("FT-ICR", name), instrument_family_from_text([name]))

    def test_the_rest_are_not(self) -> None:
        for name in self.NOT_FOURIER:
            with self.subTest(name=name):
                self.assertIsNone(instrument_family_from_text([name]))

    def test_an_orbitrap_model_outranks_the_ft_icr_term_proteowizard_gives_a_model_it_does_not_know(self) -> None:
        """MTBLS2207: MS:1000079 beside a userParam naming the Orbitrap ID-X."""
        self.assertEqual(
            ("Fourier-transform MS", "Orbitrap ID-X"),
            instrument_family_from_text(
                ["Thermo Electron instrument model", "Orbitrap ID-X", "fourier transform ion cyclotron resonance mass spectrometer"]
            ),
        )


class AnMzmlIsReadForItsInstrument(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_q_exactive_mzml_is_fourier_transform(self) -> None:
        """ST004304's QC-D2-B.mzML: MS:1001911 "Q Exactive" in a referenceable group, analyzer orbitrap."""
        path = _mzml(
            self.root / "QC-D2-B.mzML",
            '<referenceableParamGroupRef ref="CommonInstrumentParams"/>\n' + ORBITRAP_ANALYZER,
            before='<referenceableParamGroupList count="1"><referenceableParamGroup id="CommonInstrumentParams">\n'
            + _cv("Q Exactive") + _cv("instrument serial number")
            + "</referenceableParamGroup></referenceableParamGroupList>\n",
        )

        found = detect_raw_format(path)

        self.assertEqual("Fourier-transform MS", found["instrument_family"])
        self.assertEqual("mzml_instrument_configuration", found["instrument_family_source"])
        self.assertEqual("Q Exactive", found["instrument_evidence"])
        self.assertEqual((10000, 0.05), (found["suggested_minimum_peak_height"], found["suggested_mass_slice_width"]))

    def test_an_orbitrap_id_x_mzml_with_the_ft_icr_analyzer_term_is_fourier_transform(self) -> None:
        path = _mzml(
            self.root / "M3T.mzML",
            FTICR_ANALYZER,
            before='<referenceableParamGroupList count="1"><referenceableParamGroup id="CommonInstrumentParams">\n'
            + _cv("Thermo Electron instrument model") + _user("instrument model", "Orbitrap ID-X")
            + "</referenceableParamGroup></referenceableParamGroupList>\n",
        )

        found = detect_raw_format(path)

        self.assertEqual(("Fourier-transform MS", "Orbitrap ID-X"), (found["instrument_family"], found["instrument_evidence"]))

    def test_a_converted_maxis_mzml_is_qtof_on_its_header(self) -> None:
        """MTBLS1572, through Interactive's own mzXML conversion: userParam instrument model, TOF analyzer."""
        path = _mzml(self.root / "HILIC.mzML", _user("instrument model", "maXis impact") + TOF_ANALYZER)

        found = detect_raw_format(path)

        self.assertEqual(("QTOF", "mzml_instrument_configuration"), (found["instrument_family"], found["instrument_family_source"]))

    def test_an_mzml_naming_no_instrument_is_qtof_by_default_only(self) -> None:
        path = _mzml(self.root / "plain.mzML", _user("instrument model", "not recorded in mzXML"))

        found = detect_raw_format(path)

        self.assertEqual(("QTOF", "format_default"), (found["instrument_family"], found["instrument_family_source"]))

    def test_a_file_name_saying_orbitrap_outside_the_instrument_lists_is_not_evidence(self) -> None:
        path = _mzml(self.root / "orbitrap_named.mzML", "")

        self.assertEqual("QTOF", detect_raw_format(path)["instrument_family"])

    def test_vendor_formats_keep_their_family(self) -> None:
        thermo = self.root / "a.raw"
        thermo.write_bytes(b"x")
        waters = self.root / "b.raw"
        waters.mkdir()
        sciex = self.root / "c.wiff"
        sciex.write_bytes(b"x")

        self.assertEqual(("Fourier-transform MS", "vendor_format"),
                         tuple(detect_raw_format(thermo)[key] for key in ("instrument_family", "instrument_family_source")))
        self.assertEqual(("QTOF", "vendor_format"),
                         tuple(detect_raw_format(waters)[key] for key in ("instrument_family", "instrument_family_source")))
        self.assertEqual(("QTOF", "vendor_format"),
                         tuple(detect_raw_format(sciex)[key] for key in ("instrument_family", "instrument_family_source")))


class TheRepresentativesFamily(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_a_row_made_before_the_header_was_read_is_read_again(self) -> None:
        """A workflow state from before 0.5.28 carries QTOF for a Q Exactive mzML: the step is still 1,000."""
        path = _mzml(self.root / "QC-D2-B.mzML", _cv("Q Exactive") + ORBITRAP_ANALYZER)
        files = [{"file_path": str(path), "file_name": "QC-D2-B", "file_type": "QC", "analytical_order": 1,
                  "instrument_family": "QTOF"}]

        profile = select_peak_tuning_representative(files)

        self.assertEqual(("Fourier-transform MS", 1000), (profile["instrument_family"], profile["threshold_step"]))
        self.assertEqual("mzml_instrument_configuration", profile["instrument_family_source"])

    def test_a_declared_orbitrap_names_the_family_of_an_mzml_that_names_none(self) -> None:
        path = _mzml(self.root / "plain.mzML", "")
        files = [{"file_path": str(path), "file_name": "plain", "file_type": "Sample", "analytical_order": 1}]

        profile = select_peak_tuning_representative(files, declared_instrument="Thermo Q Exactive Orbitrap")

        self.assertEqual(("Fourier-transform MS", 1000), (profile["instrument_family"], profile["threshold_step"]))
        self.assertEqual("repository_declared_instrument", profile["instrument_family_source"])
        self.assertEqual("Thermo Q Exactive Orbitrap", profile["declared_instrument"])

    def test_a_declaration_does_not_overrule_the_file(self) -> None:
        tof = _mzml(self.root / "tof.mzML", _user("instrument model", "maXis impact") + TOF_ANALYZER)
        waters = self.root / "w.raw"
        waters.mkdir()
        for path in (tof, waters):
            with self.subTest(path=path.name):
                found = representative_instrument_family({"file_path": str(path)}, "Thermo Q Exactive Orbitrap")
                self.assertEqual("QTOF", found["instrument_family"])

    def test_a_declaration_naming_no_fourier_instrument_changes_nothing(self) -> None:
        path = _mzml(self.root / "plain.mzML", "")

        found = representative_instrument_family({"file_path": str(path)}, "AB SCIEX TripleTOF 5600+")

        self.assertEqual(("QTOF", "format_default"), (found["instrument_family"], found["instrument_family_source"]))

    def test_a_row_whose_file_is_not_on_disk_keeps_what_it_says(self) -> None:
        found = representative_instrument_family(
            {"file_path": "D:/absent/qc.raw", "instrument_family": "Fourier-transform MS"}
        )

        self.assertEqual(("Fourier-transform MS", "file_row"), (found["instrument_family"], found["instrument_family_source"]))

    def test_the_declared_instrument_is_read_from_the_catalog_handoff(self) -> None:
        manifest = self.root / "run-manifest.json"
        manifest.write_text(json.dumps({"project": {"repository_metadata": {"catalog_handoff": {
            "technical_settings": {"instrument": "Thermo Q Exactive Orbitrap"}}}}}), encoding="utf-8")

        self.assertEqual("Thermo Q Exactive Orbitrap", declared_instrument(manifest))
        self.assertEqual("", declared_instrument(self.root / "absent.json"))
        self.assertEqual("", declared_instrument(""))


if __name__ == "__main__":
    unittest.main()
