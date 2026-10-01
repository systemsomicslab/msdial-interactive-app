"""mzXML becomes the mzML that MS-DIAL's RawDataHandler reads, and nothing is lost on the way.

MS-DIAL has no mzXML reader, so a repository sample whose only readable encoding is mzXML could not
be analysed at all. msdial_app.mzxml_conversion writes such a file as mzML with the standard library
and proves the result by re-reading it with a second reader.

The fixtures are synthetic mzXML 2.1 and 3.2 documents written here byte by byte, so every value
the tests expect is known independently of the converter: the arrays are packed with struct, and
the embedded sha1 is computed over the bytes the fixture writes. What is checked is what the pinned
RawDataHandler 1.3.9699.469 and the extractor's MzmlMetadataReader need: spectrum-level polarity,
the isolation target (MS:1000827) beside the selected ion (MS:1000744), a width only where one was
recorded, collision energy under activation, 64-bit m/z, zlib, no self-closed containers, invariant
numbers, and bytes that do not depend on where the files live or when they were written.
"""

from __future__ import annotations

import ast
import base64
import copy
import errno
import hashlib
import re
import shutil
import struct
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path
from unittest.mock import patch

from msdial_app import mzxml_conversion
from msdial_app.mzxml_conversion import (
    ConversionOptions,
    convert_mzxml_to_mzml,
    converter_identity,
    validate_conversion,
)


MZML = "{http://psi.hupo.org/ms/mzml}"
NS_32 = "http://sashimi.sourceforge.net/schema_revision/mzXML_3.2"
NS_21 = "http://sashimi.sourceforge.net/schema_revision/mzXML_2.1"

MS1_MZ = [100.123456, 250.5, 999.999]
MS1_INTENSITY = [1000.5, 2.25e6, 3.3]
MS2_MZ = [60.0445, 120.5, 249.9]
MS2_INTENSITY = [10.0, 20.0, 30.0]


def _float32(values: list[float]) -> list[float]:
    """The values a 32-bit mzXML array actually holds."""
    return list(struct.unpack(f">{len(values)}f", struct.pack(f">{len(values)}f", *values)))


def _encoded(values: list[float], precision: int = 32, compressed: bool = False) -> str:
    raw = struct.pack(f">{len(values)}{'f' if precision == 32 else 'd'}", *values)
    if compressed:
        raw = zlib.compress(raw)
    return base64.b64encode(raw).decode("ascii")


def _interleaved(mz: list[float], intensity: list[float]) -> list[float]:
    return [value for pair in zip(mz, intensity) for value in pair]


def peaks_32(mz, intensity, precision=32, compression="none", content="m/z-int") -> str:
    values = _interleaved(mz, intensity) if content == "m/z-int" else list(mz)
    return (
        f'<peaks compressionType="{compression}" compressedLen="0" precision="{precision}"'
        f' byteOrder="network" contentType="{content}">'
        f"{_encoded(values, precision, compression == 'zlib')}</peaks>"
    )


def peaks_21(mz, intensity) -> str:
    return (
        '<peaks precision="32" byteOrder="network" pairOrder="m/z-int">'
        f"{_encoded(_interleaved(mz, intensity))}</peaks>"
    )


def scan(num: int, attributes: dict[str, object], body: str = "", nested: str = "") -> str:
    text = " ".join(f'{key}="{value}"' for key, value in {"num": num, **attributes}.items())
    return f"    <scan {text}>\n      {body}\n{nested}    </scan>\n"


HEADER_32 = """<?xml version="1.0" encoding="ISO-8859-1"?>
<mzXML xmlns="{namespace}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xsi:schemaLocation="{namespace} {namespace}/mzXML_idx_3.2.xsd">
  <msRun scanCount="{count}" startTime="PT0S" endTime="PT100S">
    <parentFile fileName="file://C:\\synthetic\\acquired.RAW" fileType="RAWData" fileSha1="0123456789abcdef0123456789abcdef01234567"/>
    <msInstrument msInstrumentID="1">
      <msManufacturer category="msManufacturer" value="Thermo Scientific"/>
      <msModel category="msModel" value="Q Exactive"/>
      <msIonisation category="msIonisation" value="electrospray ionization"/>
      <msMassAnalyzer category="msMassAnalyzer" value="orbitrap"/>
      <msDetector category="msDetector" value="inductive detector"/>
      <software type="acquisition" name="Xcalibur" version="2.8"/>
    </msInstrument>
    <dataProcessing centroided="{centroided}">
      <software type="conversion" name="ProteoWizard software" version="3.0.9987"/>
    </dataProcessing>
"""

HEADER_21 = """<?xml version="1.0" encoding="ISO-8859-1"?>
<mzXML xmlns="{namespace}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <msRun scanCount="{count}" startTime="PT0S" endTime="PT40S">
    <parentFile fileName="C:/synthetic/legacy.RAW" fileType="RAWData" fileSha1="89abcdef0123456789abcdef0123456789abcdef"/>
    <msInstrument>
      <msManufacturer category="msManufacturer" value="ThermoFinnigan"/>
      <msModel category="msModel" value="LTQ FT"/>
      <msIonisation category="msIonisation" value="ESI"/>
      <msMassAnalyzer category="msMassAnalyzer" value="FTMS"/>
      <msDetector category="msDetector" value="unknown"/>
      <software type="acquisition" name="Xcalibur" version="2.0.7"/>
    </msInstrument>
    <dataProcessing centroided="{centroided}">
      <software type="conversion" name="ReAdW" version="4.3.1"/>
    </dataProcessing>
"""


def document(
    scans: str,
    *,
    count: int,
    header: str = HEADER_32,
    namespace: str = NS_32,
    centroided: str = "1",
    embed_sha1: bool = True,
) -> bytes:
    text = header.format(namespace=namespace, count=count, centroided=centroided) + scans
    text += '  </msRun>\n  <index name="scan">\n    <offset id="1">0</offset>\n  </index>\n'
    text += "  <indexOffset>0</indexOffset>\n"
    data = text.encode("iso-8859-1")
    if not embed_sha1:
        return data + b"</mzXML>\n"
    # The schema's span: from the first byte up to and including the opening <sha1> tag.
    prefix = data + b"  <sha1>"
    return prefix + hashlib.sha1(prefix).hexdigest().encode("ascii") + b"</sha1>\n</mzXML>\n"


def dda_32() -> bytes:
    scans = (
        scan(
            1,
            {
                "msLevel": 1, "peaksCount": 3, "polarity": "+", "scanType": "Full",
                "filterLine": "FTMS + p ESI Full ms [100.00-1000.00] &amp; &lt;check&gt;",
                "retentionTime": "PT12.5S", "lowMz": "100.123456", "highMz": "999.999",
                "basePeakMz": "250.5", "basePeakIntensity": "2250000", "totIonCurrent": "2251003.8",
                "startMz": "100", "endMz": "1000", "centroided": "1", "msInstrumentID": "1",
            },
            peaks_32(MS1_MZ, MS1_INTENSITY, 32, "zlib"),
        )
        + scan(
            2,
            {
                "msLevel": 2, "peaksCount": 3, "polarity": "+", "retentionTime": "PT1M3.2S",
                "collisionEnergy": "35", "centroided": "1", "startMz": "50", "endMz": "260",
            },
            '<precursorMz precursorScanNum="1" precursorIntensity="2250000" precursorCharge="2"'
            ' activationMethod="CID" windowWideness="2.0">250.5</precursorMz>'
            + peaks_32(MS2_MZ, MS2_INTENSITY, 64, "none"),
        )
        + scan(
            3,
            {"msLevel": 1, "peaksCount": 0, "polarity": "+", "retentionTime": "PT70S", "centroided": "1"},
            '<peaks compressionType="none" compressedLen="0" precision="32" byteOrder="network"'
            ' contentType="m/z-int"></peaks>',
        )
        + scan(
            4,
            {"msLevel": 2, "peaksCount": 2, "polarity": "+", "retentionTime": "PT71.25S"},
            '<precursorMz precursorIntensity="0">300.25</precursorMz>'
            + peaks_32([80.5, 150.25], [5.0, 6.0]),
        )
    )
    return document(scans, count=4)


def nested_21() -> bytes:
    nested = scan(
        2,
        {"msLevel": 2, "peaksCount": 1, "polarity": "-", "retentionTime": "PT30.5S", "collisionEnergy": "25"},
        '<precursorMz precursorIntensity="5000" precursorCharge="1">301.1</precursorMz>'
        + peaks_21([150.5], [77.0]),
    ) + scan(
        3,
        {"msLevel": 2, "peaksCount": 0, "polarity": "-", "retentionTime": "PT31S"},
        '<precursorMz precursorIntensity="100">402.2</precursorMz>'
        # Some writers encode an empty scan as one (0, 0) pair.
        '<peaks precision="32" byteOrder="network" pairOrder="m/z-int">AAAAAAAAAAA=</peaks>',
    )
    scans = scan(
        1,
        {
            "msLevel": 1, "peaksCount": 2, "polarity": "-", "retentionTime": "PT30S",
            "filterLine": "FTMS - p ESI Full ms [150.00-2000.00] \u00e9",
        },
        peaks_21([301.1, 402.2], [5000.0, 100.0]),
        nested,
    ) + scan(4, {"msLevel": 1, "peaksCount": 1, "retentionTime": "PT32S"}, peaks_21([500.5], [1.0]))
    return document(scans, count=4, header=HEADER_21, namespace=NS_21, centroided="0")


def swath_32(ladder=(425.0, 450.0, 475.0), cycles=2) -> bytes:
    scans, num, time = "", 1, 10.0
    for _ in range(cycles):
        scans += scan(num, {"msLevel": 1, "peaksCount": 1, "polarity": "+", "retentionTime": f"PT{time}S"},
                      peaks_32([400.0], [9.0]))
        num, time = num + 1, time + 0.25
        for target in ladder:
            scans += scan(
                num,
                {"msLevel": 2, "peaksCount": 1, "polarity": "+", "retentionTime": f"PT{time}S", "collisionEnergy": "30"},
                f'<precursorMz precursorIntensity="0" activationMethod="CID">{target}</precursorMz>'
                + peaks_32([target - 100.0], [3.0]),
            )
            num, time = num + 1, time + 0.25
    return document(scans, count=num - 1)


def all_ion_32() -> bytes:
    scans = scan(
        1, {"msLevel": 1, "peaksCount": 1, "polarity": "+", "retentionTime": "PT5S", "startMz": "50", "endMz": "1500"},
        peaks_32([200.0], [4.0]),
    ) + scan(
        2,
        {
            "msLevel": 2, "peaksCount": 2, "polarity": "+", "retentionTime": "PT5.5S", "collisionEnergy": "30",
            "startMz": "50", "endMz": "1500",
        },
        peaks_32([80.0, 120.0], [1.0, 2.0]),
    )
    return document(scans, count=2)


def polarity_32(polarities: list[str]) -> bytes:
    scans = ""
    for number, polarity in enumerate(polarities, start=1):
        attributes = {"msLevel": 1, "peaksCount": 1, "retentionTime": f"PT{number}S"}
        if polarity is not None:
            attributes["polarity"] = polarity
        scans += scan(number, attributes, peaks_32([100.0 + number], [1.0]))
    return document(scans, count=len(polarities))


def _cv_of(element) -> dict[str, tuple[str, str | None]]:
    if element is None:
        return {}
    return {
        child.get("accession"): (child.get("value"), child.get("unitAccession"))
        for child in element.findall(f"{MZML}cvParam")
    }


def _decode_array(element) -> tuple[str, int, bytes, tuple[float, ...]]:
    cv = _cv_of(element)
    text = element.find(f"{MZML}binary").text or ""
    raw = base64.b64decode(text)
    if "MS:1000574" in cv and raw:
        raw = zlib.decompress(raw)
    bits = 64 if "MS:1000523" in cv else 32
    count = len(raw) // (bits // 8)
    values = struct.unpack(f"<{count}{'d' if bits == 64 else 'f'}", raw)
    kind = "mz" if "MS:1000514" in cv else "intensity"
    return kind, bits, raw, values


def read_mzml(path: Path) -> tuple[ET.Element, list[dict]]:
    root = ET.parse(path).getroot()
    spectra = []
    for spectrum in root.iter(f"{MZML}spectrum"):
        scan_element = spectrum.find(f"{MZML}scanList/{MZML}scan")
        precursors = []
        for precursor in spectrum.iter(f"{MZML}precursor"):
            precursors.append(
                {
                    "attributes": dict(precursor.attrib),
                    "isolation": _cv_of(precursor.find(f"{MZML}isolationWindow")),
                    "selected": _cv_of(precursor.find(f"{MZML}selectedIonList/{MZML}selectedIon")),
                    "activation": _cv_of(precursor.find(f"{MZML}activation")),
                    "activation_user": [
                        (user.get("name"), user.get("value"))
                        for user in precursor.find(f"{MZML}activation").findall(f"{MZML}userParam")
                    ],
                }
            )
        arrays = {}
        for array_element in spectrum.iter(f"{MZML}binaryDataArray"):
            kind, bits, raw, values = _decode_array(array_element)
            arrays[kind] = {"bits": bits, "raw": raw, "values": values}
        spectra.append(
            {
                "attributes": dict(spectrum.attrib),
                "cv": _cv_of(spectrum),
                "scan": _cv_of(scan_element),
                "window": _cv_of(spectrum.find(f"{MZML}scanList/{MZML}scan/{MZML}scanWindowList/{MZML}scanWindow")),
                "precursors": precursors,
                "arrays": arrays,
            }
        )
    return root, spectra


class _Workspace(unittest.TestCase):
    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name)

    def tearDown(self) -> None:
        self._temporary.cleanup()

    def write(self, name: str, data: bytes, folder: str = "data") -> Path:
        path = self.root / folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def convert(self, data: bytes, name: str = "sample.mzXML", options=None, **kwargs) -> tuple[dict, Path]:
        source = self.write(name, data)
        destination = self.root / "converted" / (Path(name).stem + ".mzML")
        record = convert_mzxml_to_mzml(source, destination, options, **kwargs)
        return record, destination

    def assertConverted(self, record: dict) -> None:
        self.assertEqual(record["status"], "converted", record.get("error") or record.get("validation"))
        self.assertEqual(record["validation"]["status"], "passed", record["validation"])

    def assertFailedCleanly(self, record: dict, destination: Path, *fragments: str) -> None:
        self.assertEqual(record["status"], "failed")
        for fragment in fragments:
            self.assertIn(fragment, record["error"])
        self.assertFalse(destination.exists())
        leftovers = [path.name for path in destination.parent.glob("*")] if destination.parent.exists() else []
        self.assertEqual(leftovers, [])


class Mzxml32MappingTests(_Workspace):
    """Every mzXML field lands on the CV term the pinned parsers read, and nowhere else."""

    def setUp(self) -> None:
        super().setUp()
        self.record, self.destination = self.convert(dda_32(), "dda.mzXML")
        self.root_element, self.spectra = read_mzml(self.destination)

    def test_the_conversion_is_recorded_as_converted_and_validated(self) -> None:
        self.assertConverted(self.record)
        self.assertEqual(self.record["validation"]["spectra_compared"], 4)
        self.assertEqual(self.record["output"]["sha256"], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.assertEqual(self.record["mzxml"]["version"], "3.2")
        self.assertEqual(self.record["source"]["embedded_sha1"]["status"], "verified")
        self.assertEqual(self.record["source"]["embedded_sha1"]["span"], "through_opening_tag")
        self.assertEqual(self.record["converter"], converter_identity())

    def test_the_source_is_hashed_as_repositories_publish_it(self) -> None:
        # The md5 beside the sha256 and sha1: a source whose declared MD5 was verified is then tied to the very
        # bytes this conversion read.
        source = self.record["source"]
        self.assertEqual(
            (hashlib.sha256(dda_32()).hexdigest(), hashlib.sha1(dda_32()).hexdigest(), hashlib.md5(dda_32()).hexdigest()),
            (source["sha256"], source["sha1"], source["md5"]),
        )
        self.assertEqual(len(dda_32()), source["bytes"])

    def test_spectrum_list_count_equals_the_spectra(self) -> None:
        spectrum_list = self.root_element.find(f"{MZML}run/{MZML}spectrumList")
        self.assertEqual(spectrum_list.get("count"), "4")
        self.assertEqual([item["attributes"]["id"] for item in self.spectra], ["scan=1", "scan=2", "scan=3", "scan=4"])
        self.assertEqual([item["attributes"]["index"] for item in self.spectra], ["0", "1", "2", "3"])

    def test_ms_level_polarity_and_representation_are_spectrum_cv_params(self) -> None:
        ms1, ms2 = self.spectra[0]["cv"], self.spectra[1]["cv"]
        self.assertEqual(ms1["MS:1000511"][0], "1")
        self.assertIn("MS:1000579", ms1)
        self.assertEqual(ms2["MS:1000511"][0], "2")
        self.assertIn("MS:1000580", ms2)
        for spectrum in self.spectra:
            self.assertIn("MS:1000130", spectrum["cv"])
            self.assertNotIn("MS:1000129", spectrum["cv"])
            self.assertIn("MS:1000127", spectrum["cv"])
        # Scan 4 records no centroided attribute; msRun/dataProcessing says centroided="1".
        self.assertEqual(
            self.record["counts"]["representation_by_ms_level"], {"1": {"centroid": 2}, "2": {"centroid": 2}}
        )

    def test_retention_time_is_the_full_duration_in_seconds(self) -> None:
        times = [spectrum["scan"]["MS:1000016"] for spectrum in self.spectra]
        self.assertEqual([unit for _, unit in times], ["UO:0000010"] * 4)
        self.assertEqual([value for value, _ in times], ["12.5", "63.2", "70.0", "71.25"])
        # RawDataHandler and MzmlMetadataReader both divide a UO:0000010 value by 60, so MS-DIAL holds
        # exactly the minutes it would have computed from the mzXML's own seconds.
        minutes = [float(value) / 60.0 for value, _ in times]
        self.assertEqual(minutes, [12.5 / 60.0, 63.2 / 60.0, 70.0 / 60.0, 71.25 / 60.0])

    def test_precursor_mz_is_both_the_isolation_target_and_the_selected_ion(self) -> None:
        precursor = self.spectra[1]["precursors"][0]
        self.assertEqual(precursor["attributes"].get("spectrumRef"), "scan=1")
        self.assertEqual(precursor["isolation"]["MS:1000827"][0], "250.5")
        self.assertEqual(precursor["selected"]["MS:1000744"][0], "250.5")
        self.assertEqual(precursor["selected"]["MS:1000041"][0], "2")
        self.assertEqual(precursor["selected"]["MS:1000042"][0], "2250000.0")

    def test_a_recorded_window_is_split_evenly_and_an_unrecorded_one_is_omitted(self) -> None:
        recorded = self.spectra[1]["precursors"][0]["isolation"]
        self.assertEqual(recorded["MS:1000828"][0], "1.0")
        self.assertEqual(recorded["MS:1000829"][0], "1.0")
        unrecorded = self.spectra[3]["precursors"][0]["isolation"]
        self.assertNotIn("MS:1000828", unrecorded)
        self.assertNotIn("MS:1000829", unrecorded)
        self.assertEqual(self.record["precursors"]["with_window"], 1)
        self.assertEqual(self.record["precursors"]["without_window"], 1)

    def test_collision_energy_and_activation_are_written_only_where_recorded(self) -> None:
        activation = self.spectra[1]["precursors"][0]["activation"]
        self.assertIn("MS:1000133", activation)
        self.assertEqual(activation["MS:1000045"], ("35.0", "UO:0000266"))
        # The spectrum-level copy is a deviation that stays off unless it is asked for.
        self.assertNotIn("MS:1000045", self.spectra[1]["cv"])
        bare = self.spectra[3]["precursors"][0]
        self.assertEqual(bare["activation"], {})
        self.assertEqual(bare["activation_user"], [("mzXML activationMethod", "not recorded in mzXML")])
        self.assertNotIn("MS:1000041", bare["selected"])
        self.assertEqual(self.record["collision_energies"]["msn"]["values"], [35.0])
        self.assertEqual(self.record["deviations"], [])
        self.assertEqual(self.record["inferences"], [])

    def test_scan_window_filter_string_and_descriptive_values(self) -> None:
        self.assertEqual(self.spectra[0]["window"]["MS:1000501"][0], "100.0")
        self.assertEqual(self.spectra[0]["window"]["MS:1000500"][0], "1000.0")
        self.assertEqual(self.spectra[0]["scan"]["MS:1000512"][0], "FTMS + p ESI Full ms [100.00-1000.00] & <check>")
        self.assertEqual(self.spectra[0]["cv"]["MS:1000528"][0], "100.123456")
        self.assertEqual(self.spectra[0]["cv"]["MS:1000505"], ("2250000.0", "MS:1000131"))

    def test_arrays_round_trip_bit_for_bit(self) -> None:
        ms1 = self.spectra[0]["arrays"]
        # 32-bit m/z is widened to 64-bit, which is exact.
        self.assertEqual(ms1["mz"]["bits"], 64)
        self.assertEqual(list(ms1["mz"]["values"]), _float32(MS1_MZ))
        # 32-bit intensity keeps its precision and its exact bits.
        self.assertEqual(ms1["intensity"]["bits"], 32)
        source_bits = struct.pack(">3f", *MS1_INTENSITY)
        self.assertEqual(ms1["intensity"]["raw"], struct.pack("<3f", *struct.unpack(">3f", source_bits)))
        ms2 = self.spectra[1]["arrays"]
        self.assertEqual(ms2["mz"]["bits"], 64)
        self.assertEqual(list(ms2["mz"]["values"]), MS2_MZ)
        self.assertEqual(ms2["intensity"]["bits"], 64)
        self.assertEqual(list(ms2["intensity"]["values"]), MS2_INTENSITY)

    def test_an_empty_scan_is_an_empty_spectrum(self) -> None:
        empty = self.spectra[2]
        self.assertEqual(empty["attributes"]["defaultArrayLength"], "0")
        self.assertEqual(empty["arrays"]["mz"]["values"], ())
        self.assertEqual(empty["arrays"]["intensity"]["values"], ())
        self.assertEqual(self.record["counts"]["empty_spectra"], 1)

    def test_every_spectrum_carries_the_terms_the_pinned_readers_need(self) -> None:
        for spectrum in self.spectra:
            self.assertIn("MS:1000511", spectrum["cv"])
            self.assertTrue({"MS:1000579", "MS:1000580"} & set(spectrum["cv"]))
            self.assertIn("MS:1000016", spectrum["scan"])
            self.assertEqual(set(spectrum["arrays"]), {"mz", "intensity"})
        self.assertEqual(
            [cv.get("accession") for cv in self.root_element.find(f"{MZML}fileDescription/{MZML}fileContent")],
            ["MS:1000579", "MS:1000580"],
        )

    def test_source_file_names_the_mzxml_with_its_sha1_and_no_workspace_path(self) -> None:
        source_file = self.root_element.find(f"{MZML}fileDescription/{MZML}sourceFileList/{MZML}sourceFile")
        self.assertEqual(source_file.get("name"), "dda.mzXML")
        self.assertEqual(source_file.get("location"), ".")
        cv = _cv_of(source_file)
        self.assertIn("MS:1000566", cv)
        self.assertIn("MS:1000776", cv)
        self.assertEqual(cv["MS:1000569"][0], hashlib.sha1((self.root / "data" / "dda.mzXML").read_bytes()).hexdigest())
        text = self.destination.read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn("startTimeStamp", text)

    def test_instrument_terms_and_no_model_as_a_cv_param(self) -> None:
        text = self.destination.read_text(encoding="utf-8")
        self.assertNotIn("MS:1000031", text)
        configuration = self.root_element.find(f"{MZML}instrumentConfigurationList/{MZML}instrumentConfiguration")
        self.assertEqual(_cv_of(configuration), {})
        user_params = [(user.get("name"), user.get("value")) for user in configuration.findall(f"{MZML}userParam")]
        self.assertIn(("instrument model", "Q Exactive"), user_params)
        self.assertIn("MS:1000073", _cv_of(configuration.find(f"{MZML}componentList/{MZML}source")))
        self.assertIn("MS:1000484", _cv_of(configuration.find(f"{MZML}componentList/{MZML}analyzer")))
        self.assertEqual(self.record["mzxml"]["instruments"][0]["analyzer_family"], "fourier_transform")
        software = [
            (item.get("id"), sorted(_cv_of(item)))
            for item in self.root_element.find(f"{MZML}softwareList")
        ]
        self.assertEqual(software[0], ("Xcalibur", ["MS:1000532"]))
        self.assertEqual(software[1][1], ["MS:1000615"])
        self.assertEqual(software[-1], ("MSDIAL_Interactive_mzXML_converter", ["MS:1000799"]))

    def test_structure_suits_the_pinned_raw_data_handler(self) -> None:
        text = self.destination.read_text(encoding="utf-8")
        containers = (
            "spectrum|scanList|scan|scanWindowList|scanWindow|precursorList|precursor|isolationWindow|"
            "selectedIonList|selectedIon|activation|binaryDataArrayList|binaryDataArray|fileContent"
        )
        self.assertIsNone(re.search(rf"<(?:{containers})\b[^>]*/>", text))
        self.assertNotIn("referenceableParamGroup", text)
        self.assertNotIn("indexedmzML", text)
        for array_element in self.root_element.iter(f"{MZML}binaryDataArray"):
            accessions = {cv.get("accession") for cv in array_element.findall(f"{MZML}cvParam")}
            self.assertLessEqual(accessions, mzxml_conversion.ALLOWED_BINARY_ACCESSIONS)
            self.assertIn("MS:1000574", accessions)
            binary = array_element.find(f"{MZML}binary").text or ""
            self.assertIsNone(re.search(r"\s", binary))
            self.assertEqual(array_element.get("encodedLength"), str(len(binary)))
        numeric = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?")
        for element in self.root_element.iter(f"{MZML}cvParam"):
            if element.get("accession") in mzxml_conversion._FLOAT_ACCESSIONS | mzxml_conversion._INTEGER_ACCESSIONS:
                self.assertRegex(element.get("value"), numeric)
                self.assertNotIn(",", element.get("value"))


class Mzxml21Tests(_Workspace):
    """mzXML 2.1: nested scans, pairOrder, no compressionType, and the msRun centroided fallback."""

    def setUp(self) -> None:
        super().setUp()
        self.record, self.destination = self.convert(nested_21(), "legacy.mzXML")
        self.root_element, self.spectra = read_mzml(self.destination)

    def test_nested_scans_are_flattened_in_document_order(self) -> None:
        self.assertConverted(self.record)
        self.assertEqual(self.record["mzxml"]["version"], "2.1")
        self.assertEqual([item["attributes"]["id"] for item in self.spectra], ["scan=1", "scan=2", "scan=3", "scan=4"])
        self.assertEqual([item["cv"]["MS:1000511"][0] for item in self.spectra], ["1", "2", "2", "1"])
        self.assertEqual(self.spectra[1]["precursors"][0]["isolation"]["MS:1000827"][0], "301.1")

    def test_profile_from_data_processing_and_negative_polarity(self) -> None:
        for spectrum in self.spectra:
            self.assertIn("MS:1000128", spectrum["cv"])
        for spectrum in self.spectra[:3]:
            self.assertIn("MS:1000129", spectrum["cv"])
        self.assertNotIn("MS:1000129", self.spectra[3]["cv"])
        self.assertNotIn("MS:1000130", self.spectra[3]["cv"])
        self.assertEqual(self.record["counts"]["polarity"], {"negative": 3, "unrecorded": 1})
        self.assertTrue(any("record no polarity" in warning for warning in self.record["warnings"]))

    def test_the_zero_pair_placeholder_is_an_empty_spectrum(self) -> None:
        self.assertEqual(self.spectra[2]["attributes"]["defaultArrayLength"], "0")
        self.assertEqual(self.record["counts"]["empty_scan_placeholders"], 1)

    def test_values_and_arrays_survive(self) -> None:
        self.assertEqual(list(self.spectra[0]["arrays"]["mz"]["values"]), _float32([301.1, 402.2]))
        self.assertEqual(list(self.spectra[0]["arrays"]["intensity"]["values"]), _float32([5000.0, 100.0]))
        self.assertEqual(self.spectra[1]["precursors"][0]["activation"]["MS:1000045"][0], "25.0")
        self.assertEqual(self.spectra[0]["scan"]["MS:1000512"][0], "FTMS - p ESI Full ms [150.00-2000.00] \u00e9")
        self.assertIn('encoding="utf-8"', self.destination.read_text(encoding="utf-8").splitlines()[0])

    def test_ambiguous_ftms_gets_a_family_but_no_analyzer_term(self) -> None:
        instrument = self.record["mzxml"]["instruments"][0]
        self.assertEqual(instrument["analyzer_terms"], [])
        self.assertEqual(instrument["analyzer_family"], "fourier_transform")
        analyzer = self.root_element.find(
            f"{MZML}instrumentConfigurationList/{MZML}instrumentConfiguration/{MZML}componentList/{MZML}analyzer"
        )
        self.assertEqual(_cv_of(analyzer), {})


class SeparateArraysTests(_Workspace):
    def test_separate_mz_and_intensity_peaks_keep_their_own_precision(self) -> None:
        body = peaks_32([101.5, 202.25], [], 64, "zlib", "m/z") + peaks_32([7.5, 8.5], [], 32, "none", "intensity")
        data = document(scan(1, {"msLevel": 1, "peaksCount": 2, "polarity": "+", "retentionTime": "PT1S"}, body), count=1)
        record, destination = self.convert(data)
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        self.assertEqual(spectra[0]["arrays"]["mz"]["values"], (101.5, 202.25))
        self.assertEqual(spectra[0]["arrays"]["intensity"]["bits"], 32)
        self.assertEqual(spectra[0]["arrays"]["intensity"]["values"], (7.5, 8.5))


class InferenceFlagTests(_Workspace):
    """Each supplied value is behind a flag that is off by default, and each use is recorded."""

    def test_defaults_are_all_off(self) -> None:
        options = ConversionOptions()
        self.assertIsNone(options.impute_polarity)
        self.assertFalse(options.infer_dia_windows)
        self.assertFalse(options.synthesize_all_ion_windows)
        self.assertFalse(options.spectrum_level_collision_energy)
        self.assertTrue(options.fail_on_sha1_mismatch)

    def test_spectrum_level_collision_energy_is_a_recorded_deviation(self) -> None:
        record, destination = self.convert(dda_32(), options={"spectrum_level_collision_energy": True})
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        self.assertEqual(spectra[1]["cv"]["MS:1000045"], ("35.0", "UO:0000266"))
        self.assertEqual(spectra[1]["precursors"][0]["activation"]["MS:1000045"][0], "35.0")
        self.assertNotIn("MS:1000045", spectra[0]["cv"])
        self.assertEqual([item["kind"] for item in record["deviations"]], ["spectrum_level_collision_energy"])
        self.assertIn("spectrum_level_collision_energy", destination.read_text(encoding="utf-8"))

    def test_unrecorded_polarity_stays_unrecorded_by_default(self) -> None:
        record, destination = self.convert(polarity_32(["any", None, "any"]))
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        for spectrum in spectra:
            self.assertFalse({"MS:1000129", "MS:1000130"} & set(spectrum["cv"]))
        self.assertEqual(record["counts"]["polarity"], {"unrecorded": 3})
        self.assertEqual(record["counts"]["polarity_recorded"], {"absent": 1, "any": 2})

    def test_polarity_imputation_from_the_declared_ion_mode(self) -> None:
        record, destination = self.convert(polarity_32(["any", "-", None]), options={"impute_polarity": "negative"})
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        for spectrum in spectra:
            self.assertIn("MS:1000129", spectrum["cv"])
        self.assertEqual(record["inferences"][0]["kind"], "polarity_imputation")
        self.assertEqual(record["inferences"][0]["spectra"], 2)

    def test_polarity_imputation_is_refused_when_the_file_records_the_other_polarity(self) -> None:
        record, destination = self.convert(polarity_32(["any", "+"]), options={"impute_polarity": "negative"})
        self.assertFailedCleanly(record, destination, "polarity imputation refused")

    def test_all_ion_scans_get_no_precursor_unless_synthesis_is_asked_for(self) -> None:
        record, destination = self.convert(all_ion_32())
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        self.assertEqual(spectra[1]["precursors"], [])
        self.assertEqual(record["precursors"]["msn_without_precursor"], 1)
        self.assertTrue(any("no precursor" in warning for warning in record["warnings"]))

    def test_all_ion_window_synthesis_spans_the_scan_window(self) -> None:
        record, destination = self.convert(all_ion_32(), options={"synthesize_all_ion_windows": True})
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        precursor = spectra[1]["precursors"][0]
        self.assertEqual(precursor["isolation"]["MS:1000827"][0], "775.0")
        self.assertEqual(precursor["isolation"]["MS:1000828"][0], "725.0")
        self.assertEqual(precursor["isolation"]["MS:1000829"][0], "725.0")
        self.assertEqual(precursor["selected"]["MS:1000744"][0], "775.0")
        self.assertEqual(precursor["activation"]["MS:1000045"][0], "30.0")
        self.assertEqual(record["inferences"][0]["kind"], "all_ion_window_synthesis")
        self.assertEqual(record["inferences"][0]["basis"], {"scan_window": 1})

    def test_dia_windows_are_omitted_by_default(self) -> None:
        record, destination = self.convert(swath_32())
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        for spectrum in spectra:
            for precursor in spectrum["precursors"]:
                self.assertNotIn("MS:1000828", precursor["isolation"])
        self.assertEqual(record["precursors"]["without_window"], 6)

    def test_dia_windows_are_inferred_from_a_uniform_repeated_ladder(self) -> None:
        record, destination = self.convert(swath_32(), options={"infer_dia_windows": True})
        self.assertConverted(record)
        _, spectra = read_mzml(destination)
        offsets = [
            (precursor["isolation"]["MS:1000828"][0], precursor["isolation"]["MS:1000829"][0])
            for spectrum in spectra
            for precursor in spectrum["precursors"]
        ]
        self.assertEqual(offsets, [("12.5", "12.5")] * 6)
        self.assertEqual(record["inferences"][0]["kind"], "dia_window_inference")
        self.assertEqual(record["inferences"][0]["precursors"], 6)
        self.assertEqual(record["precursors"]["inferred_window"], 6)

    def test_an_irregular_ladder_is_not_given_windows(self) -> None:
        record, destination = self.convert(swath_32(ladder=(425.0, 450.0, 490.0)), options={"infer_dia_windows": True})
        self.assertConverted(record)
        self.assertEqual(record["inferences"], [])
        self.assertTrue(any("not uniformly spaced" in warning for warning in record["warnings"]))

    def test_unknown_options_are_a_recorded_failure(self) -> None:
        record, destination = self.convert(dda_32(), options={"guess_everything": True})
        self.assertFailedCleanly(record, destination, "unknown conversion options")


class IntegrityTests(_Workspace):
    """A file the converter cannot prove is a failed record, never an exception, never an output."""

    def test_a_truncated_mzxml_fails(self) -> None:
        data = dda_32()
        record, destination = self.convert(data[: len(data) // 2])
        self.assertFailedCleanly(record, destination, "ParseError")
        self.assertNotIn("error_errno", record, "a file that will never convert names no system error")

    def test_a_full_disk_is_recorded_with_its_system_error(self) -> None:
        """So that a caller can tell a disk a retry may get past from a file that will never convert."""
        full = OSError(errno.ENOSPC, "No space left on device")
        with patch.object(mzxml_conversion, "_write_mzml", side_effect=full):
            record, destination = self.convert(dda_32())
        self.assertFailedCleanly(record, destination, "No space left on device")
        self.assertEqual(errno.ENOSPC, record["error_errno"])

    def test_a_sha1_mismatch_fails_by_default(self) -> None:
        data = dda_32().replace(b"Q Exactive", b"Q Exactivf")
        record, destination = self.convert(data)
        self.assertEqual(record["source"]["embedded_sha1"]["status"], "mismatch")
        self.assertFailedCleanly(record, destination, "embedded sha1")

    def test_a_sha1_mismatch_can_be_downgraded_to_a_warning(self) -> None:
        data = dda_32().replace(b"Q Exactive", b"Q Exactivf")
        record, _ = self.convert(data, options={"fail_on_sha1_mismatch": False})
        self.assertConverted(record)
        self.assertTrue(any("embedded sha1" in warning for warning in record["warnings"]))

    def test_a_file_without_an_embedded_sha1_is_recorded_as_such(self) -> None:
        text = dda_32()
        data = text[: text.index(b"  <sha1>")] + b"</mzXML>\n"
        record, _ = self.convert(data)
        self.assertConverted(record)
        self.assertEqual(record["source"]["embedded_sha1"]["status"], "absent")

    def test_a_sha1_that_stops_before_the_tag_is_accepted_and_named(self) -> None:
        text = dda_32()
        head = text[: text.index(b"  <sha1>")] + b"  "
        data = head + b"<sha1>" + hashlib.sha1(head).hexdigest().encode() + b"</sha1>\n</mzXML>\n"
        record, _ = self.convert(data)
        self.assertConverted(record)
        self.assertEqual(record["source"]["embedded_sha1"]["span"], "before_opening_tag")

    def test_a_peaks_count_mismatch_fails(self) -> None:
        data = dda_32().replace(b'num="1" msLevel="1" peaksCount="3"', b'num="1" msLevel="1" peaksCount="4"')
        record, destination = self.convert(data, options={"fail_on_sha1_mismatch": False})
        self.assertFailedCleanly(record, destination, "peaksCount is 4")

    def test_an_unknown_content_type_fails(self) -> None:
        body = peaks_32([101.5], [], 32, "none", "S/N")
        data = document(scan(1, {"msLevel": 1, "peaksCount": 1, "retentionTime": "PT1S"}, body), count=1)
        record, destination = self.convert(data)
        self.assertFailedCleanly(record, destination, "contentType 'S/N'")

    def test_a_scan_without_retention_time_fails(self) -> None:
        data = document(scan(1, {"msLevel": 1, "peaksCount": 1}, peaks_32([1.0], [1.0])), count=1)
        record, destination = self.convert(data)
        self.assertFailedCleanly(record, destination, "no retentionTime")

    def test_a_file_without_scans_fails(self) -> None:
        record, destination = self.convert(document("", count=0))
        self.assertFailedCleanly(record, destination, "no scans")

    def test_a_document_that_is_not_mzxml_fails(self) -> None:
        record, destination = self.convert(b'<?xml version="1.0"?>\n<mzML xmlns="http://psi.hupo.org/ms/mzml"></mzML>\n')
        self.assertFailedCleanly(record, destination, "not <mzXML>")

    def test_a_missing_source_fails(self) -> None:
        destination = self.root / "converted" / "absent.mzML"
        record = convert_mzxml_to_mzml(self.root / "absent.mzXML", destination)
        self.assertEqual(record["status"], "failed")
        self.assertIn("does not exist", record["error"])

    def test_a_destination_that_is_not_mzml_is_refused_and_never_removed(self) -> None:
        source = self.write("sample.mzXML", dda_32())
        record = convert_mzxml_to_mzml(source, source)
        self.assertEqual(record["status"], "failed")
        self.assertTrue(source.exists())
        other = self.write("other.txt", b"keep me")
        record = convert_mzxml_to_mzml(source, other)
        self.assertEqual(record["status"], "failed")
        self.assertEqual(other.read_bytes(), b"keep me")

    def test_a_failed_reconversion_removes_the_stale_output(self) -> None:
        record, destination = self.convert(dda_32())
        self.assertConverted(record)
        source = self.root / "data" / "sample.mzXML"
        source.write_bytes(dda_32()[:200])
        again = convert_mzxml_to_mzml(source, destination, previous=record)
        self.assertEqual(again["status"], "failed")
        self.assertFalse(destination.exists())
        self.assertTrue(again["output"]["removed_after_failure"])


class DestinationOwnershipTests(_Workspace):
    """Only this converter's own output is ever replaced or removed at the destination.

    The destination may hold the repository's own mzML of the same stem. Replacing or deleting it
    would delete raw data that nobody confirmed for deletion.
    """

    FOREIGN = b'<?xml version="1.0"?>\n<mzML xmlns="http://psi.hupo.org/ms/mzml">repository bytes</mzML>\n'

    def setUp(self) -> None:
        super().setUp()
        self.source = self.write("sample.mzXML", dda_32())
        self.destination = self.root / "converted" / "sample.mzML"

    def place(self, data: bytes) -> None:
        self.destination.parent.mkdir(parents=True, exist_ok=True)
        self.destination.write_bytes(data)

    def assertKept(self, record: dict, data: bytes, fragment: str = "not written by the converter") -> None:
        self.assertEqual(record["status"], "failed")
        self.assertIn(fragment, record["error"])
        self.assertEqual(self.destination.read_bytes(), data)
        self.assertNotIn("removed_after_failure", record["output"])
        self.assertEqual([path.name for path in self.destination.parent.iterdir()], [self.destination.name])

    def test_a_foreign_mzml_is_not_replaced_by_a_good_conversion(self) -> None:
        self.place(self.FOREIGN)
        record = convert_mzxml_to_mzml(self.source, self.destination)
        self.assertKept(record, self.FOREIGN)
        self.assertTrue(record["output"]["foreign_file_kept"])

    def test_a_foreign_mzml_survives_a_failed_conversion(self) -> None:
        self.place(self.FOREIGN)
        self.source.write_bytes(dda_32()[:300])
        self.assertKept(convert_mzxml_to_mzml(self.source, self.destination), self.FOREIGN)

    def test_a_foreign_file_that_arrives_during_the_conversion_is_kept(self) -> None:
        original = mzxml_conversion._validate
        for outcome, fragment in (("passed", "not written by the converter"), ("failed", "does not reproduce")):
            with self.subTest(outcome=outcome):

                def arrive(*arguments, outcome=outcome):
                    result = original(*arguments)
                    self.place(self.FOREIGN)
                    return result if outcome == "passed" else {**result, "status": "failed", "problems": ["forced"]}

                mzxml_conversion._validate = arrive
                try:
                    record = convert_mzxml_to_mzml(self.source, self.destination)
                finally:
                    mzxml_conversion._validate = original
                self.assertKept(record, self.FOREIGN, fragment)
                self.destination.unlink()

    def test_argument_errors_touch_nothing_at_the_destination(self) -> None:
        record = convert_mzxml_to_mzml(self.source, self.destination)
        self.assertConverted(record)
        own = self.destination.read_bytes()
        partial = self.destination.with_name(self.destination.name + ".partial")
        for data in (own, self.FOREIGN):
            self.place(data)
            partial.write_bytes(b"another call's partial file")
            for label, options, source in (
                ("unknown option", {"no_such_option": 1}, self.source),
                ("invalid option", {"impute_polarity": "both"}, self.source),
                ("missing source", None, self.root / "data" / "absent.mzXML"),
            ):
                with self.subTest(label=label, own=data == own):
                    again = convert_mzxml_to_mzml(source, self.destination, options, previous=record)
                    self.assertEqual(again["status"], "failed")
                    self.assertEqual(self.destination.read_bytes(), data)
                    self.assertEqual(partial.read_bytes(), b"another call's partial file")
                    self.assertNotIn("removed_after_failure", again["output"])

    def test_the_converters_own_output_is_recognised_without_a_record(self) -> None:
        convert_mzxml_to_mzml(self.source, self.destination)
        self.assertTrue(self.destination.read_bytes().startswith(mzxml_conversion._OUTPUT_PREFIX))
        replaced = convert_mzxml_to_mzml(self.source, self.destination, {"spectrum_level_collision_energy": True})
        self.assertConverted(replaced)
        self.assertEqual(replaced["output"]["sha256"], hashlib.sha256(self.destination.read_bytes()).hexdigest())
        self.source.write_bytes(dda_32()[:300])
        failed = convert_mzxml_to_mzml(self.source, self.destination)
        self.assertTrue(failed["output"]["removed_after_failure"])
        self.assertFalse(self.destination.exists())

    def test_an_output_another_tool_rewrote_is_foreign(self) -> None:
        # A tool that rewrites this converter's output adds its own processing, so the file begins
        # the same way but no longer carries the converter's processing record.
        convert_mzxml_to_mzml(self.source, self.destination)
        rewritten = self.destination.read_bytes().replace(
            b'<dataProcessingList count="1">', b'<dataProcessingList count="2">', 1
        )
        self.place(rewritten)
        self.assertKept(convert_mzxml_to_mzml(self.source, self.destination), rewritten)

    def test_the_recorded_output_is_recognised_by_its_bytes(self) -> None:
        # An output whose header does not carry the processing record as this writer spells it (an
        # earlier writer's) is still the converter's when it is exactly what a record says it wrote.
        record = convert_mzxml_to_mzml(self.source, self.destination)
        earlier = self.destination.read_bytes().replace(
            b'<dataProcessingList count="1">', b'<dataProcessingList  count="1">', 1
        )
        self.place(earlier)
        previous = copy.deepcopy(record)
        previous["output"].update(bytes=len(earlier), sha256=hashlib.sha256(earlier).hexdigest())
        # Other options, so that the record is not simply reused.
        options = {"spectrum_level_collision_energy": True}
        self.assertKept(convert_mzxml_to_mzml(self.source, self.destination, options), earlier)
        replaced = convert_mzxml_to_mzml(self.source, self.destination, options, previous=previous)
        self.assertConverted(replaced)
        self.place(earlier)
        self.source.write_bytes(dda_32()[:300])
        failed = convert_mzxml_to_mzml(self.source, self.destination, previous=previous)
        self.assertTrue(failed["output"]["removed_after_failure"])
        self.assertFalse(self.destination.exists())

    def test_a_directory_at_the_destination_is_refused_and_kept(self) -> None:
        self.destination.mkdir(parents=True)
        (self.destination / "keep").write_bytes(b"keep me")
        record = convert_mzxml_to_mzml(self.source, self.destination)
        self.assertEqual(record["status"], "failed")
        self.assertIn("not written by the converter", record["error"])
        self.assertEqual((self.destination / "keep").read_bytes(), b"keep me")


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, (list, tuple)):
        return [text for item in value for text in _strings(item)]
    return []


class DeterminismAndResumeTests(_Workspace):
    def test_the_same_mzxml_gives_the_same_bytes_wherever_it_lives(self) -> None:
        first = self.write("sample.mzXML", dda_32(), "one/deep/folder")
        second = self.write("sample.mzXML", dda_32(), "two")
        relative = "FILES/sample.mzXML"
        record_one = convert_mzxml_to_mzml(first, self.root / "out-one" / "sample.mzML", source_relative_path=relative)
        record_two = convert_mzxml_to_mzml(
            second, self.root / "elsewhere" / "x" / "sample.mzML", source_relative_path=relative
        )
        self.assertConverted(record_one)
        self.assertConverted(record_two)
        self.assertEqual(record_one["output"]["sha256"], record_two["output"]["sha256"])
        self.assertEqual(
            (self.root / "out-one" / "sample.mzML").read_bytes(),
            (self.root / "elsewhere" / "x" / "sample.mzML").read_bytes(),
        )
        text = (self.root / "out-one" / "sample.mzML").read_text(encoding="utf-8")
        self.assertIn('location="FILES"', text)

    def test_converting_twice_gives_the_same_sha256(self) -> None:
        record_one, destination = self.convert(swath_32(), options={"infer_dia_windows": True})
        first = destination.read_bytes()
        destination.unlink()
        record_two, _ = self.convert(swath_32(), options={"infer_dia_windows": True})
        self.assertEqual(first, destination.read_bytes())
        self.assertEqual(record_one["output"]["sha256"], record_two["output"]["sha256"])

    def test_a_completed_record_is_reused_and_a_deleted_output_is_rewritten(self) -> None:
        record, destination = self.convert(dda_32())
        self.assertConverted(record)
        written = destination.stat().st_mtime_ns
        source = self.root / "data" / "sample.mzXML"
        again = convert_mzxml_to_mzml(source, destination, previous=record)
        self.assertTrue(again["reused_previous_record"])
        self.assertEqual(again["output"]["sha256"], record["output"]["sha256"])
        self.assertEqual(destination.stat().st_mtime_ns, written)

        destination.unlink()
        rewritten = convert_mzxml_to_mzml(source, destination, previous=record)
        self.assertFalse(rewritten["reused_previous_record"])
        self.assertConverted(rewritten)
        self.assertEqual(rewritten["output"]["sha256"], record["output"]["sha256"])

    def test_changed_options_or_a_changed_output_are_not_reused(self) -> None:
        record, destination = self.convert(dda_32())
        source = self.root / "data" / "sample.mzXML"
        other = convert_mzxml_to_mzml(source, destination, {"spectrum_level_collision_energy": True}, previous=record)
        self.assertFalse(other["reused_previous_record"])
        self.assertConverted(other)
        destination.write_bytes(destination.read_bytes() + b"\n")
        options = {"spectrum_level_collision_energy": True}
        tampered = convert_mzxml_to_mzml(source, destination, options, previous=other)
        self.assertFalse(tampered["reused_previous_record"])
        self.assertEqual(tampered["output"]["sha256"], other["output"]["sha256"])
        # The relative path is written into the mzML, so a record for another path is not this output.
        moved = convert_mzxml_to_mzml(source, destination, options, source_relative_path="FILES/x.mzXML", previous=tampered)
        self.assertFalse(moved["reused_previous_record"])
        self.assertNotEqual(moved["output"]["sha256"], tampered["output"]["sha256"])

    def test_a_record_for_a_source_of_another_name_is_not_reused(self) -> None:
        # The source's name is written into the mzML, so the same bytes under another name are another output.
        relative = "FILES/sample.mzXML"
        record, destination = self.convert(dda_32(), source_relative_path=relative)
        renamed = self.write("renamed.mzXML", dda_32())
        copied = self.root / "converted" / "renamed.mzML"
        shutil.copyfile(destination, copied)
        again = convert_mzxml_to_mzml(renamed, copied, source_relative_path=relative, previous=record)
        self.assertFalse(again["reused_previous_record"])
        self.assertConverted(again)
        self.assertNotEqual(again["output"]["sha256"], record["output"]["sha256"])

    def test_a_record_reused_for_another_units_files_names_this_calls_files(self) -> None:
        relative = "FILES/sample.mzXML"
        sources = [self.write("sample.mzXML", dda_32(), f"{unit}/raw/data/FILES") for unit in ("unitA", "unitB")]
        outputs = [self.root / unit / "raw" / "converted" / "FILES" / "sample.mzML" for unit in ("unitA", "unitB")]
        record_a = convert_mzxml_to_mzml(sources[0], outputs[0], source_relative_path=relative)
        self.assertConverted(record_a)
        unchanged = copy.deepcopy(record_a)
        outputs[1].parent.mkdir(parents=True)
        shutil.copyfile(outputs[0], outputs[1])

        record_b = convert_mzxml_to_mzml(sources[1], outputs[1], source_relative_path=relative, previous=record_a)
        self.assertTrue(record_b["reused_previous_record"])
        self.assertEqual(record_b["source"]["path"], str(sources[1]))
        self.assertEqual(record_b["output"]["path"], str(outputs[1]))
        self.assertEqual(record_b["output"]["sha256"], record_a["output"]["sha256"])
        self.assertEqual(
            record_b["reused_from"],
            {
                "source_path": str(sources[0]),
                "output_path": str(outputs[0]),
                "started_at": record_a["started_at"],
                "completed_at": record_a["completed_at"],
            },
        )
        # Outside reused_from, nothing in unit B's record names unit A's files.
        described = _strings({key: value for key, value in record_b.items() if key != "reused_from"})
        self.assertFalse([text for text in described if str(self.root / "unitA") in text], described)
        self.assertEqual(record_a, unchanged)

        # A reuse of the reuse still names the conversion that wrote the bytes.
        record_c = convert_mzxml_to_mzml(sources[1], outputs[1], source_relative_path=relative, previous=record_b)
        self.assertTrue(record_c["reused_previous_record"])
        self.assertEqual(record_c["reused_from"], record_b["reused_from"])
        self.assertEqual(record_c["output"]["path"], str(outputs[1]))

    def test_a_moved_workspace_is_reused_under_its_new_paths(self) -> None:
        before, after = self.root / "before", self.root / "after"
        source = self.write("sample.mzXML", dda_32(), "before/data")
        record = convert_mzxml_to_mzml(source, before / "converted" / "sample.mzML")
        self.assertConverted(record)
        before.rename(after)
        moved = convert_mzxml_to_mzml(after / "data" / "sample.mzXML", after / "converted" / "sample.mzML", previous=record)
        self.assertTrue(moved["reused_previous_record"])
        self.assertEqual(moved["source"]["path"], str(after / "data" / "sample.mzXML"))
        self.assertEqual(moved["output"]["path"], str(after / "converted" / "sample.mzML"))
        self.assertEqual(moved["reused_from"]["output_path"], str(before / "converted" / "sample.mzML"))

    @unittest.skipUnless(sys.platform == "win32", "8.3 short names are a Windows spelling")
    def test_a_short_path_spelling_is_reused_under_this_calls_spelling(self) -> None:
        import ctypes

        folder = self.root / "a long folder name"
        source = self.write("sample.mzXML", dda_32(), "a long folder name/data")
        record = convert_mzxml_to_mzml(source, folder / "converted" / "sample.mzML")
        self.assertConverted(record)
        buffer = ctypes.create_unicode_buffer(32768)
        if not ctypes.windll.kernel32.GetShortPathNameW(str(folder), buffer, len(buffer)):
            self.skipTest("no short name could be read for the folder")
        short = Path(buffer.value)
        if short.name == folder.name:
            self.skipTest("this volume does not keep 8.3 short names")
        again = convert_mzxml_to_mzml(short / "data" / "sample.mzXML", short / "converted" / "sample.mzML", previous=record)
        self.assertTrue(again["reused_previous_record"])
        self.assertEqual(again["source"]["path"], str(short / "data" / "sample.mzXML"))
        self.assertEqual(again["output"]["path"], str(short / "converted" / "sample.mzML"))


class ChunkBoundaryTests(_Workspace):
    """Hashing and the independent reader read in chunks; tags and the sha1 span cross their edges.

    Production chunks are 1 MiB, so a fixture would never reach a boundary. Shrinking the chunk to
    64 bytes puts dozens of boundaries through every tag and through the embedded sha1.
    """

    def setUp(self) -> None:
        super().setUp()
        self._chunk = mzxml_conversion._CHUNK
        mzxml_conversion._CHUNK = 64

    def tearDown(self) -> None:
        mzxml_conversion._CHUNK = self._chunk
        super().tearDown()

    def test_small_chunks_change_nothing(self) -> None:
        record, destination = self.convert(dda_32())
        self.assertConverted(record)
        self.assertEqual(record["source"]["embedded_sha1"]["status"], "verified")
        self.assertEqual(record["source"]["sha256"], hashlib.sha256(dda_32()).hexdigest())
        mzxml_conversion._CHUNK = self._chunk
        source = self.write("sample.mzXML", dda_32(), "other")
        again = convert_mzxml_to_mzml(source, self.root / "converted-again" / "sample.mzML")
        self.assertEqual(record["source"]["sha256"], again["source"]["sha256"])
        self.assertEqual(record["source"]["embedded_sha1"], again["source"]["embedded_sha1"])
        self.assertEqual(record["output"]["sha256"], again["output"]["sha256"])

    def test_both_sha1_spans_are_found_at_every_alignment(self) -> None:
        text = dda_32()
        base = text[: text.index(b"  <sha1>")]
        for padding in range(64):
            head = base + b" " * padding
            for span, digest in (
                ("through_opening_tag", hashlib.sha1(head + b"<sha1>").hexdigest()),
                ("before_opening_tag", hashlib.sha1(head).hexdigest()),
            ):
                with self.subTest(padding=padding, span=span):
                    data = head + b"<sha1>" + digest.encode() + b"</sha1>\n</mzXML>\n"
                    embedded = mzxml_conversion._source_integrity(self.write(f"pad{padding}.mzXML", data))
                    self.assertEqual((embedded["embedded_sha1"]["status"], embedded["embedded_sha1"]["span"]),
                                     ("verified", span))

    def test_a_self_closed_container_is_recognised_across_boundaries(self) -> None:
        record, destination = self.convert(dda_32())
        text = destination.read_text(encoding="utf-8")
        edited = re.sub(r"<activation>.*?</activation>", "<activation/>", text, count=1, flags=re.S)
        for shift in range(64):
            with self.subTest(shift=shift):
                path = self.root / f"edited{shift}.mzML"
                # Leading spaces inside the root move every later byte across the chunk edges.
                path.write_text(edited.replace("<cvList", " " * shift + "<cvList", 1), encoding="utf-8")
                result = validate_conversion(self.root / "data" / "sample.mzXML", path)
                self.assertTrue(
                    any("self-closed <activation>" in problem for problem in result["problems"]), result["problems"]
                )


class ValidatorTests(_Workspace):
    """The validator is only worth running if it catches what it is there to catch."""

    def setUp(self) -> None:
        super().setUp()
        self.record, self.destination = self.convert(dda_32())
        self.source = self.root / "data" / "sample.mzXML"
        self.text = self.destination.read_text(encoding="utf-8")

    def assertProblem(self, result: dict, fragment: str) -> None:
        self.assertEqual(result["status"], "failed")
        self.assertTrue(any(fragment in problem for problem in result["problems"]), result["problems"])

    def validate_edited(self, edited: str) -> dict:
        path = self.root / "edited.mzML"
        path.write_text(edited, encoding="utf-8", newline="\n")
        return validate_conversion(self.source, path)

    def test_a_faithful_conversion_passes(self) -> None:
        result = validate_conversion(self.source, self.destination)
        self.assertEqual(result["status"], "passed", result["problems"])
        self.assertEqual(result["spectra_compared"], 4)
        self.assertEqual(result["spectrum_list_count"], 4)

    def test_a_self_closed_activation_is_caught(self) -> None:
        edited = re.sub(r"<activation>.*?</activation>", "<activation/>", self.text, count=1, flags=re.S)
        self.assertProblem(self.validate_edited(edited), "self-closed <activation>")

    def test_an_empty_container_pair_is_caught_as_empty(self) -> None:
        edited = re.sub(r"<activation>.*?</activation>", "<activation></activation>", self.text, count=1, flags=re.S)
        result = self.validate_edited(edited)
        self.assertProblem(result, "empty <activation>")
        self.assertFalse(any("self-closed" in problem for problem in result["problems"]), result["problems"])

    def test_a_changed_intensity_is_caught(self) -> None:
        root = ET.fromstring(self.text)
        binary = next(root.iter(f"{MZML}binaryDataArray"))  # the first m/z array
        raw = zlib.decompress(base64.b64decode(binary.find(f"{MZML}binary").text))
        values = list(struct.unpack(f"<{len(raw) // 8}d", raw))
        values[-1] += 1e-9
        replacement = base64.b64encode(zlib.compress(struct.pack(f"<{len(values)}d", *values))).decode()
        original = binary.find(f"{MZML}binary").text
        edited = self.text.replace(original, replacement, 1).replace(
            f'encodedLength="{len(original)}"', f'encodedLength="{len(replacement)}"', 1
        )
        self.assertProblem(self.validate_edited(edited), "m/z array differs")

    def test_a_dropped_last_peak_is_caught(self) -> None:
        # What RawDataHandler's decoder does to every array must not pass for a faithful conversion.
        root = ET.fromstring(self.text)
        binary = next(root.iter(f"{MZML}binaryDataArray"))
        raw = zlib.decompress(base64.b64decode(binary.find(f"{MZML}binary").text))
        replacement = base64.b64encode(zlib.compress(raw[:-8] + bytes(8))).decode()
        original = binary.find(f"{MZML}binary").text
        edited = self.text.replace(original, replacement, 1).replace(
            f'encodedLength="{len(original)}"', f'encodedLength="{len(replacement)}"', 1
        )
        self.assertEqual(self.validate_edited(edited)["status"], "failed")

    def test_a_wrong_polarity_retention_time_or_window_is_caught(self) -> None:
        for old, new, fragment in (
            ('accession="MS:1000130" name="positive scan"', 'accession="MS:1000129" name="negative scan"', "polarity"),
            ('value="63.2" unitCvRef="UO"', 'value="63.3" unitCvRef="UO"', "scan start time"),
            (
                'name="isolation window lower offset" value="1.0"',
                'name="isolation window lower offset" value="0.0"',
                "lower offset",
            ),
        ):
            with self.subTest(fragment=fragment):
                self.assertProblem(self.validate_edited(self.text.replace(old, new, 1)), fragment)

    def test_forbidden_structures_are_caught(self) -> None:
        group = (
            '<referenceableParamGroupList count="1"><referenceableParamGroup id="SpectrumParamsPositive">'
            '<cvParam cvRef="MS" accession="MS:1000130" name="positive scan" value=""/>'
            "</referenceableParamGroup></referenceableParamGroupList>\n  <fileDescription>"
        )
        integer = ('accession="MS:1000521" name="32-bit float"', 'accession="MS:1000519" name="32-bit integer"')
        for edited, fragment in (
            (self.text.replace("<fileDescription>", group, 1), "referenceableParamGroup"),
            (self.text.replace('<spectrumList count="4"', '<spectrumList count="5"', 1), "spectrumList count"),
            (self.text.replace(*integer, 1), "MS:1000519"),
            (self.text.replace('value="63.2"', 'value="63,2"', 1), "invariant-culture"),
        ):
            with self.subTest(fragment=fragment):
                self.assertProblem(self.validate_edited(edited), fragment)

    def test_whitespace_inside_binary_is_caught(self) -> None:
        match = re.search(r"<binary>([A-Za-z0-9+/=]{8})", self.text)
        edited = self.text.replace(match.group(0), f"<binary>{match.group(1)[:4]}\n{match.group(1)[4:]}", 1)
        self.assertProblem(self.validate_edited(edited), "whitespace inside <binary>")

    def test_a_truncated_mzml_fails_without_raising(self) -> None:
        result = self.validate_edited(self.text[: len(self.text) // 2])
        self.assertEqual(result["status"], "failed")


class StandardLibraryOnlyTests(unittest.TestCase):
    def test_the_module_imports_only_the_standard_library_and_its_package(self) -> None:
        tree = ast.parse(Path(mzxml_conversion.__file__).read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported |= {alias.name.split(".")[0] for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0:
                imported.add(node.module.split(".")[0])
        imported.discard("__future__")
        self.assertLessEqual(imported, set(sys.stdlib_module_names))


if __name__ == "__main__":
    unittest.main()
