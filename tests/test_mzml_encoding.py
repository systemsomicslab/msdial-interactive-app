"""An mzML whose binary arrays RawDataHandler cannot decode is found before MS-DIAL reads it.

RawDataHandler decodes a binaryDataArray only as a 32- or 64-bit float, zlib-compressed or not; anything
else reaches a default branch that logs to Debug and reads the bytes as uncompressed 32-bit floats, and
the spectrum comes out as garbage or empty without an error. msdial_app.mzml_encoding scans the
binaryDataArray cvParams of the first few spectra and chromatograms and names what it cannot decode.

The fixtures are mzML written here as text, so every accession a test expects is one the test put there.
The arrays hold real base64 of real doubles; the scan reads only the cvParams, so their bytes do not have
to match the encoding a test declares.
"""

from __future__ import annotations

import base64
import json
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from msdial_app import mzml_encoding
from msdial_app.mzml_encoding import (
    UNSUPPORTED_MZML_ENCODING,
    mzml_encoding_problems,
    scan_mzml_encoding,
)
from msdial_app.mzxml_conversion import ALLOWED_BINARY_ACCESSIONS, convert_mzxml_to_mzml
from msdial_app.repository_reanalysis import (
    RepositoryFile,
    create_download_lease,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
)

from test_download_lease_record import _Client, _project
from test_mzxml_conversion import all_ion_32, dda_32, nested_21, swath_32
from test_repository_reanalysis import _MixedUnitFixture


NAMES = {
    "MS:1000514": "m/z array", "MS:1000515": "intensity array", "MS:1000595": "time array",
    "MS:1002816": "mean ion mobility array", "MS:1000519": "32-bit integer", "MS:1000521": "32-bit float",
    "MS:1000522": "64-bit integer", "MS:1000523": "64-bit float", "MS:1000574": "zlib compression",
    "MS:1000576": "no compression", "MS:1002312": "MS-Numpress linear prediction compression",
    "MS:1002313": "MS-Numpress positive integer compression",
    "MS:1002314": "MS-Numpress short logged float compression",
    "MS:1002746": "MS-Numpress linear prediction compression followed by zlib compression",
    "MS:1002747": "MS-Numpress positive integer compression followed by zlib compression",
    "MS:1002748": "MS-Numpress short logged float compression followed by zlib compression",
    "MS:1003780": "zstd compression", "MS:1003089": "truncation, delta prediction and zlib compression",
}


def cv(accession: str, name: str | None = None) -> str:
    return f'<cvParam cvRef="MS" accession="{accession}" name="{name or NAMES.get(accession, "")}" value=""/>'


def array(kind: str, *, data_type: str | None = "MS:1000523", compression: str | None = "MS:1000574",
          extra: str = "", group: str = "") -> str:
    encoded = base64.b64encode(zlib.compress(struct.pack("<3d", 100.0, 200.0, 300.0))).decode("ascii")
    terms = "".join(cv(accession) for accession in (data_type, compression, kind) if accession)
    reference = f'<referenceableParamGroupRef ref="{group}"/>' if group else ""
    return (f'<binaryDataArray encodedLength="{len(encoded)}">{reference}{terms}{extra}'
            f"<binary>{encoded}</binary></binaryDataArray>")


def spectrum(index: int, arrays: list[str], ms_level: int = 1, length: int = 3) -> str:
    return (f'<spectrum index="{index}" id="scan={index + 1}" defaultArrayLength="{length}">'
            f'<cvParam cvRef="MS" accession="MS:1000511" name="ms level" value="{ms_level}"/>'
            f'<binaryDataArrayList count="{len(arrays)}">{"".join(arrays)}</binaryDataArrayList></spectrum>\n')


def chromatogram(index: int, arrays: list[str], identifier: str = "TIC") -> str:
    return (f'<chromatogram index="{index}" id="{identifier}" defaultArrayLength="3">'
            f'<binaryDataArrayList count="{len(arrays)}">{"".join(arrays)}</binaryDataArrayList></chromatogram>\n')


def good_spectrum_arrays() -> list[str]:
    return [array("MS:1000514"), array("MS:1000515", data_type="MS:1000521")]


def good_chromatogram_arrays() -> list[str]:
    return [array("MS:1000595"), array("MS:1000515", data_type="MS:1000521")]


def dda_spectra(count: int, arrays=None) -> list[str]:
    """MS1 then MS2, alternating, so the scan sees an MSn spectrum early."""
    return [spectrum(index, arrays or good_spectrum_arrays(), ms_level=1 + index % 2) for index in range(count)]


def mzml(spectra: list[str], chromatograms: list[str] = (), *, indexed: bool = False, groups: str = "") -> bytes:
    head = ('<?xml version="1.0" encoding="utf-8"?>\n'
            + ('<indexedmzML xmlns="http://psi.hupo.org/ms/mzml">\n' if indexed else "")
            + '<mzML xmlns="http://psi.hupo.org/ms/mzml" version="1.1.0">\n'
            + (f"<referenceableParamGroupList count=\"1\">{groups}</referenceableParamGroupList>\n" if groups else "")
            + '<run id="r1">\n'
            + f'<spectrumList count="{len(spectra)}">\n')
    body = head.encode("utf-8")
    body += "".join(spectra).encode("utf-8") + b"</spectrumList>\n"
    offsets = []
    if chromatograms:
        body += f'<chromatogramList count="{len(chromatograms)}">\n'.encode("utf-8")
        for item in chromatograms:
            offsets.append(len(body))
            body += item.encode("utf-8")
        body += b"</chromatogramList>\n"
    body += b"</run>\n</mzML>\n"
    if not indexed:
        return body
    index_offset = len(body)
    listed = "".join(f'<offset idRef="c{number}">{offset}</offset>\n' for number, offset in enumerate(offsets))
    body += (f'<indexList count="2">\n<index name="spectrum">\n<offset idRef="scan=1">0</offset>\n</index>\n'
             f'<index name="chromatogram">\n{listed}</index>\n</indexList>\n'
             f"<indexListOffset>{index_offset}</indexListOffset>\n</indexedmzML>\n").encode("utf-8")
    return body


class _Files(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name)

    def tearDown(self) -> None:
        self._directory.cleanup()

    def write(self, data: bytes, name: str = "sample.mzML") -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def problems(self, data: bytes) -> list[dict]:
        return mzml_encoding_problems(self.write(data))


class DecodableFiles(_Files):
    def test_float_arrays_with_zlib_or_no_compression_raise_nothing(self) -> None:
        arrays = [array("MS:1000514", compression="MS:1000576"), array("MS:1000515", data_type="MS:1000521")]
        path = self.write(mzml(dda_spectra(4, arrays), [chromatogram(0, good_chromatogram_arrays())]))

        scan = scan_mzml_encoding(path)

        self.assertEqual("scanned", scan["status"])
        self.assertEqual([], scan["problems"])
        self.assertEqual(4, scan["spectra_scanned"])
        self.assertEqual(1, scan["chromatograms_scanned"])
        self.assertIn(
            {"where": "spectrum", "array": "MS:1000514", "binary_data_type": "MS:1000523",
             "compression": "MS:1000576"},
            scan["encodings"],
        )

    def test_an_array_raw_data_handler_drops_is_not_judged(self) -> None:
        """A numpress ion-mobility array is parsed and dropped; the spectrum's m/z and intensity are fine."""
        arrays = [*good_spectrum_arrays(), array("MS:1002816", compression="MS:1002312")]

        self.assertEqual([], self.problems(mzml(dda_spectra(6, arrays))))

    def test_an_empty_spectrum_needs_no_arrays(self) -> None:
        empty = spectrum(0, [], length=0)

        self.assertEqual([], self.problems(mzml([empty, *dda_spectra(6)[1:]])))


class UndecodableEncodings(_Files):
    def test_every_numpress_compression_is_named_with_its_accession(self) -> None:
        for accession in ("MS:1002312", "MS:1002313", "MS:1002314", "MS:1002746", "MS:1002747", "MS:1002748"):
            with self.subTest(accession=accession):
                arrays = [array("MS:1000514", compression=accession), array("MS:1000515")]

                problems = self.problems(mzml(dda_spectra(6, arrays)))

                self.assertEqual(1, len(problems), problems)
                self.assertEqual(UNSUPPORTED_MZML_ENCODING, problems[0]["reason"])
                self.assertEqual(accession, problems[0]["accession"])
                self.assertEqual("compression", problems[0]["term"])
                self.assertEqual("MS:1000514", problems[0]["array"])
                self.assertEqual("scan=1", problems[0]["first_id"])
                self.assertEqual(mzml_encoding.SCAN_SPECTRA, problems[0]["count"], "counted over the spectra scanned")

    def test_numpress_beside_zlib_is_still_numpress(self) -> None:
        """Older writers put numpress and zlib in two cvParams; RawDataHandler would inflate and stop there."""
        arrays = [array("MS:1000514", extra=cv("MS:1002312")), array("MS:1000515")]

        self.assertEqual(["MS:1002312"], [item["accession"] for item in self.problems(mzml(dda_spectra(6, arrays)))])

    def test_integer_arrays_are_named(self) -> None:
        for accession in ("MS:1000519", "MS:1000522"):
            with self.subTest(accession=accession):
                arrays = [array("MS:1000514"), array("MS:1000515", data_type=accession)]

                problems = self.problems(mzml(dda_spectra(6, arrays)))

                self.assertEqual([(accession, "binary_data_type", "MS:1000515")],
                                 [(item["accession"], item["term"], item["array"]) for item in problems])

    def test_other_compressions_and_one_the_cv_does_not_list_yet(self) -> None:
        for accession, name in (("MS:1003780", None), ("MS:1003089", None), ("MS:1009999", "future compression")):
            with self.subTest(accession=accession):
                arrays = [array("MS:1000514", compression=None, extra=cv(accession, name)), array("MS:1000515")]

                problems = self.problems(mzml(dda_spectra(6, arrays)))

                self.assertEqual([accession], [item["accession"] for item in problems])

    def test_array_terms_from_a_param_group_are_not_read(self) -> None:
        groups = ('<referenceableParamGroup id="mzArray">' + cv("MS:1000523") + cv("MS:1000574") + cv("MS:1000514")
                  + "</referenceableParamGroup>")
        arrays = [array(None, data_type=None, compression=None, group="mzArray"), array("MS:1000515")]

        problems = self.problems(mzml(dda_spectra(6, arrays), groups=groups))

        self.assertEqual(1, len(problems), problems)
        self.assertEqual(("param_group", "mzArray", ""), (problems[0]["term"], problems[0]["param_group"],
                                                         problems[0]["accession"]))

    def test_a_type_and_compression_only_in_a_group_are_not_read(self) -> None:
        arrays = [array("MS:1000514", data_type=None, compression=None, group="encoding"), array("MS:1000515")]

        problems = self.problems(mzml(dda_spectra(6, arrays)))

        self.assertEqual({("param_group", "binary data type"), ("param_group", "binary data compression type")},
                         {(item["term"], item["name"]) for item in problems})

    def test_an_absent_compression_term_is_named(self) -> None:
        arrays = [array("MS:1000514", compression=None), array("MS:1000515")]

        problems = self.problems(mzml(dda_spectra(6, arrays)))

        self.assertEqual([("missing_term", "binary data compression type", "")],
                         [(item["term"], item["name"], item["accession"]) for item in problems])

    def test_a_spectrum_with_points_and_no_mz_array(self) -> None:
        problems = self.problems(mzml(dda_spectra(6, [array("MS:1000515")])))

        self.assertEqual([("missing_term", "m/z array", "MS:1000514")],
                         [(item["term"], item["name"], item["array"]) for item in problems])


# Enough spectra that the file is several times the scan's first read.
LONG = 10_000


class ScanReach(_Files):
    def test_the_scan_stops_after_the_first_spectra_and_reads_little(self) -> None:
        path = self.write(mzml(dda_spectra(LONG)))

        scan = scan_mzml_encoding(path)

        self.assertEqual(mzml_encoding.SCAN_SPECTRA, scan["spectra_scanned"])
        self.assertLess(scan["bytes_read"], path.stat().st_size / 2)

    def test_it_reads_on_until_an_msn_spectrum_is_among_them(self) -> None:
        spectra = [spectrum(index, good_spectrum_arrays(), ms_level=1) for index in range(8)]
        spectra.append(spectrum(8, [array("MS:1000514", compression="MS:1002312"), array("MS:1000515")], 2))

        problems = self.problems(mzml(spectra))

        self.assertEqual(["scan=9"], [item["first_id"] for item in problems])

    def test_a_chromatogram_is_reached_through_the_index(self) -> None:
        bad = [array("MS:1000595", compression="MS:1002746"), array("MS:1000515")]
        path = self.write(mzml(dda_spectra(LONG), [chromatogram(0, bad), chromatogram(1, bad, "BPC")], indexed=True))

        scan = scan_mzml_encoding(path)

        self.assertEqual("index", scan["chromatograms_reached"])
        self.assertEqual(2, scan["chromatograms_scanned"])
        self.assertEqual([("chromatogram", "MS:1002746", "TIC", 2)],
                         [(item["where"], item["accession"], item["first_id"], item["count"])
                          for item in scan["problems"]])
        self.assertLess(scan["bytes_read"], path.stat().st_size / 2)

    def test_without_an_index_a_long_spectrum_list_leaves_the_chromatograms_unread(self) -> None:
        bad = [array("MS:1000595", compression="MS:1002746"), array("MS:1000515")]
        path = self.write(mzml(dda_spectra(LONG), [chromatogram(0, bad)]))

        scan = scan_mzml_encoding(path)

        self.assertEqual((0, None, []), (scan["chromatograms_scanned"], scan["chromatograms_reached"],
                                         scan["problems"]))

    def test_a_short_spectrum_list_reaches_the_chromatograms_in_sequence(self) -> None:
        bad = [array("MS:1000595", data_type="MS:1000522"), array("MS:1000515")]

        scan = scan_mzml_encoding(self.write(mzml(dda_spectra(2), [chromatogram(0, bad)])))

        self.assertEqual("sequence", scan["chromatograms_reached"])
        self.assertEqual(["MS:1000522"], [item["accession"] for item in scan["problems"]])

    def test_an_index_offset_that_is_not_a_chromatogram_is_not_followed(self) -> None:
        data = mzml(dda_spectra(LONG), [chromatogram(0, good_chromatogram_arrays())], indexed=True)
        # Point the index one byte past the chromatogram's start.
        start = data.index(b'<offset idRef="c0">') + len(b'<offset idRef="c0">')
        end = data.index(b"<", start)
        data = data[:start] + str(int(data[start:end]) + 1).encode() + data[end:]

        scan = scan_mzml_encoding(self.write(data))

        self.assertEqual("scanned", scan["status"])
        self.assertEqual(0, scan["chromatograms_scanned"])


class UnreadableFiles(_Files):
    def test_a_file_that_is_not_xml_is_unreadable_not_undecodable(self) -> None:
        path = self.write(b"\x00\x01 not xml at all")

        scan = scan_mzml_encoding(path)

        self.assertEqual("unreadable", scan["status"])
        self.assertIn("not well-formed", scan["error"])
        self.assertEqual([], mzml_encoding_problems(path))

    def test_a_missing_file_does_not_raise(self) -> None:
        scan = scan_mzml_encoding(self.root / "absent.mzML")

        self.assertEqual("unreadable", scan["status"])
        self.assertIn("FileNotFoundError", scan["error"])

    def test_another_xml_document_is_not_an_mzml(self) -> None:
        scan = scan_mzml_encoding(self.write(b"<mzXML><msRun/></mzXML>"))

        self.assertEqual("unreadable", scan["status"])
        self.assertIn("<mzXML>", scan["error"])

    def test_a_file_cut_short_after_its_first_spectra_keeps_what_was_read(self) -> None:
        data = mzml(dda_spectra(3, [array("MS:1000514", compression="MS:1002312"), array("MS:1000515")]))

        problems = self.problems(data[: data.index(b'<spectrum index="2"') + 20])

        self.assertEqual(["MS:1002312"], [item["accession"] for item in problems])


class ConverterOutput(unittest.TestCase):
    """mzxml_conversion writes nothing this scan would refuse."""

    def test_the_converter_lint_allows_only_decodable_terms(self) -> None:
        allowed = mzml_encoding.DECODABLE_BINARY_DATA_TYPES | mzml_encoding.DECODABLE_COMPRESSIONS | {
            "MS:1000514", "MS:1000515"}
        self.assertEqual(set(), set(ALLOWED_BINARY_ACCESSIONS) - allowed)

    def test_converted_files_have_no_encoding_problems(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            for name, data in (("dda", dda_32()), ("nested", nested_21()), ("swath", swath_32()),
                               ("allion", all_ion_32())):
                with self.subTest(fixture=name):
                    source = Path(directory) / f"{name}.mzXML"
                    source.write_bytes(data)
                    destination = Path(directory) / "converted" / f"{name}.mzML"
                    record = convert_mzxml_to_mzml(source, destination)
                    self.assertEqual("converted", record["status"], record.get("error"))

                    scan = scan_mzml_encoding(destination)

                    self.assertEqual("scanned", scan["status"])
                    self.assertEqual([], scan["problems"])
                    self.assertTrue(scan["encodings"])
                    self.assertEqual({"MS:1000574"}, {item["compression"] for item in scan["encodings"]})


def _numpress_mzml() -> bytes:
    return mzml(dda_spectra(6, [array("MS:1000514", compression="MS:1002746"), array("MS:1000515")]))


class TheLeaseExcludesUndecodableInputs(unittest.TestCase):
    """Wired where the lease discovers its inputs: an undecodable mzML never becomes an input candidate."""

    def _lease(self, root: Path, payloads: dict[str, bytes]) -> dict:
        files = [RepositoryFile(f"FILES/{name}", len(data), f"https://example.org/{name}")
                 for name, data in payloads.items()]
        project = _project(files, list(payloads))
        client = _Client({f"https://example.org/{name}": data for name, data in payloads.items()})
        return create_download_lease(project, root, 10_000_000, client=client)

    def test_the_file_is_excluded_with_its_reason_and_recorded_in_lineage_and_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = self._lease(Path(temporary), {"good.mzML": mzml(dda_spectra(6)), "packed.mzML": _numpress_mzml()})
            written = read_manifest(lease["manifest_path"])

        self.assertEqual(["good.mzML"], [Path(item).name for item in written["input_candidates"]])
        excluded = written["excluded_input_candidates"]
        self.assertEqual(["packed.mzML"], [Path(item["path"]).name for item in excluded])
        self.assertEqual(UNSUPPORTED_MZML_ENCODING, excluded[0]["reason"])
        self.assertEqual(["MS:1002746"], [item["accession"] for item in excluded[0]["problems"]])
        self.assertEqual(0, written["ignored_input_candidate_count"], "an exclusion is not an unattributed file")

        lineage = written["input_lineage"]
        self.assertEqual(["good.mzML"], [Path(row["path"]).name for row in lineage["rows"]])
        self.assertEqual(["packed.mzML"], [Path(row["path"]).name for row in lineage["excluded"]])
        row = lineage["excluded"][0]
        self.assertEqual("file", row["kind"])
        self.assertEqual("https://example.org/packed.mzML", row["source"]["url"])
        self.assertEqual(UNSUPPORTED_MZML_ENCODING, row["exclusion"]["reason"])
        self.assertEqual("MS:1002746", row["exclusion"]["problems"][0]["accession"])

        attribute = next(stage for stage in written["lease_stages"] if stage["stage"] == "attribute")
        self.assertEqual((1, 1, 2), (attribute["input_candidates"], attribute["excluded_input_candidates"],
                                     attribute["mzml_encodings_scanned"]))

    def test_a_unit_with_nothing_to_exclude_records_what_it_always_did(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = self._lease(Path(temporary), {"good.mzML": mzml(dda_spectra(6)), "text.mzML": b"mzml bytes"})

        self.assertEqual({"good.mzML", "text.mzML"}, {Path(item).name for item in lease["input_candidates"]},
                         "a file the scan cannot read is left to the Console")
        self.assertNotIn("excluded_input_candidates", lease)
        self.assertNotIn("excluded", lease["input_lineage"])

    def test_a_unit_whose_every_input_is_excluded_is_still_recorded(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = self._lease(Path(temporary), {"packed.mzML": _numpress_mzml()})

        self.assertEqual("prepared", lease["status"])
        self.assertEqual([], lease["input_candidates"])
        self.assertEqual([], lease["input_lineage"]["rows"])
        self.assertEqual(1, len(lease["excluded_input_candidates"]))


class SplitPartsDoNotCarryTheParentsExclusions(_MixedUnitFixture, unittest.TestCase):
    def test_the_excluded_rows_stay_with_the_parent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest, extractor, files = self._workspace(Path(temporary) / "unit", self.MODES)
            with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=self._extractor(self.MODES)):
                run_raw_metadata_preflight(manifest, extractor)
            parent = json.loads(manifest.read_text(encoding="utf-8"))
            excluded = {"path": str(files[0].parent / "packed.mzML"), "kind": "file",
                        "exclusion": {"reason": UNSUPPORTED_MZML_ENCODING, "problems": []}}
            parent["input_lineage"] = {
                "schema": "msdial-input-lineage.v1",
                "rows": [{"path": str(path), "kind": "file"} for path in files],
                "excluded": [excluded],
            }
            manifest.write_text(json.dumps(parent), encoding="utf-8")

            result = split_unit_by_acquisition(manifest, confirmed=True)
            parts = [json.loads(Path(item["manifest_path"]).read_text(encoding="utf-8")) for item in result["parts"]]
            parent = json.loads(manifest.read_text(encoding="utf-8"))

        self.assertEqual(2, len(parts))
        for part in parts:
            self.assertEqual(2, len(part["input_lineage"]["rows"]))
            self.assertNotIn("excluded", part["input_lineage"])
        self.assertEqual([excluded], parent["input_lineage"]["excluded"])


if __name__ == "__main__":
    unittest.main()
