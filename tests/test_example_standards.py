"""The example internal-standard texts shipped for the laboratory lipidomics tutorial.

examples/lab-lipidomics/EquiSPLASH_Pos.txt and EquiSPLASH_Neg.txt are the demo record's two texts
(Zenodo 10.5281/zenodo.10957708, metabolomics_hands-on.zip > data2.zip > data2/
20200121_MsdialTxtDB_{Pos,Neg}_EquiSPLASH.txt, CC BY 4.0) as the MS-DIAL laboratory edited them for
its demo run of 2026-10-15, whose contract records exactly these sha256 digests. The edits, as the
example README states them:

- in both, SM 18:1;2O/18:1(d9) and Cer 18:1;2O/15:0(d7) are written SM 18:1;O2/18:1(d9) and
  Cer 18:1;O2/15:0(d7), as sheet 1 of the example workbook names them;
- the negative text has no row for PA 15:0_18:1(d7), which sheet 1 does not prepare.

Undoing exactly those edits gives the record's files back, by the CRC-32 the record's archive index
lists for each. As published, the texts stop msdial_lipidomics_check_inputs with the example
workbook ("SM 18:1;O2/18:1(d9) is prepared in sheet 1 but neither internal-standard library lists
it"); these do not.
"""

from __future__ import annotations

import hashlib
import tempfile
import unittest
import zlib
from pathlib import Path

from msdial_app.lab_lipidomics import (
    NEGATIVE,
    POSITIVE,
    cross_check_standards,
    expected_resolution_matrix,
    read_internal_standard_text,
    read_quant_workbook,
    salt_evidence,
)

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples" / "lab-lipidomics"
TEXTS = {POSITIVE: EXAMPLES / "EquiSPLASH_Pos.txt", NEGATIVE: EXAMPLES / "EquiSPLASH_Neg.txt"}
WORKBOOK = EXAMPLES / "quant_utility_table.xlsx"
# The inputs/internal_standard_texts digests of the laboratory's demo contract.
SHA256 = {
    POSITIVE: "bd1dc34a32c73cfbf811544a1d976d19ecf3b847884a74df334c76db557a258d",
    NEGATIVE: "c07ad9d93a72d1bb8d07a743f5ef4405ec14cc42fa8c777b7686e0a5efab0763",
}
# data2/20200121_MsdialTxtDB_{Pos,Neg}_EquiSPLASH.txt in the record: (size, CRC-32) from its index.
PUBLISHED = {POSITIVE: (2829, 0x722B03F0), NEGATIVE: (2463, 0xAE46564F)}
PUBLISHED_PA_ROW = (
    b"PA 15:0_18:1(d7)\t666.50975\t8\t[M-H]-\tNKHIVFXDPYZIBK-ZYYJJESQNA-N\tC36H62D7O8P\t"
    b"[C@](COP(=O)(O)O)([H])(OC(CCCCCCC/C=C\\CCCCCC([2H])([2H])C([2H])([2H])C([2H])([2H])[2H])=O)"
    b"COC(CCCCCCCCCCCCCC)=O\tPA\t250"
)
AHEX = ["AHexBRS", "AHexCAS", "AHexCS", "AHexSIS", "AHexSTS"]


def as_published(polarity: str) -> bytes:
    """The shipped text with the README's edits undone."""
    data = TEXTS[polarity].read_bytes().replace(b";O2", b";2O")
    if polarity == NEGATIVE:
        lines = data.split(b"\r\n")
        # The record lists PA right after PI.
        lines.insert(next(index for index, line in enumerate(lines) if line.startswith(b"PI ")) + 1, PUBLISHED_PA_ROW)
        data = b"\r\n".join(lines)
    return data


class ExampleStandardsTests(unittest.TestCase):
    def test_they_are_the_texts_the_laboratorys_demo_contract_recorded(self) -> None:
        for polarity, path in TEXTS.items():
            data = path.read_bytes()
            self.assertEqual(SHA256[polarity], hashlib.sha256(data).hexdigest(), path.name)
            data.decode("ascii")  # MS-DIAL reads a standard's name as ASCII
            self.assertNotIn(b"\n", data.replace(b"\r\n", b""), "CRLF throughout, as published")

    def test_undoing_the_stated_edits_gives_the_published_files(self) -> None:
        for polarity in TEXTS:
            data = as_published(polarity)
            self.assertEqual(PUBLISHED[polarity], (len(data), zlib.crc32(data)), polarity)

    def test_the_input_check_finds_every_prepared_standard_with_the_example_workbook(self) -> None:
        workbook = read_quant_workbook(WORKBOOK, "H")
        reads = {polarity: read_internal_standard_text(path, polarity) for polarity, path in TEXTS.items()}
        for polarity, read in reads.items():
            self.assertEqual(([], []), (read["blockers"], read["warnings"]), polarity)
        records = {polarity: read["records"] for polarity, read in reads.items()}
        cross = cross_check_standards(workbook["preparation"], workbook["lookup"], records[POSITIVE], records[NEGATIVE])
        self.assertEqual([], cross["blockers"])
        self.assertEqual([], cross["warnings"])
        self.assertEqual(["FA 16:0(d3) is prepared but sheet 2 assigns it no class; it is searched and not used."], cross["notes"])
        # What the tutorial reads out at step 4.
        matrix = expected_resolution_matrix(workbook["lookup"], workbook["quant_info"], records[POSITIVE], records[NEGATIVE])
        self.assertEqual([], matrix["by_polarity"][POSITIVE]["not_quantified"])
        self.assertEqual(AHEX, matrix["by_polarity"][NEGATIVE]["not_quantified"])
        self.assertEqual("LPC 18:1(d7)", matrix["any_others_standard"])
        salt = salt_evidence("CH3COONH4", records[NEGATIVE], workbook["quant_info"])
        self.assertEqual(["CH3COONH4"], salt["salts_consistent_with_evidence"])
        self.assertIn("[M+CH3COO]-", salt["negative_standard_adducts"])
        self.assertEqual([], salt["blockers"])

    def test_as_published_the_texts_stop_the_check(self) -> None:
        # Why the edited texts are shipped: the record's texts spell SM and Cer otherwise than sheet 1.
        workbook = read_quant_workbook(WORKBOOK, "H")
        with tempfile.TemporaryDirectory() as folder:
            records = {}
            for polarity in TEXTS:
                path = Path(folder) / f"{polarity}.txt"
                path.write_bytes(as_published(polarity))
                records[polarity] = read_internal_standard_text(path, polarity)["records"]
        cross = cross_check_standards(workbook["preparation"], workbook["lookup"], records[POSITIVE], records[NEGATIVE])
        self.assertEqual(
            [
                "SM 18:1;O2/18:1(d9) is prepared in sheet 1 but neither internal-standard library lists it.",
                "Cer 18:1;O2/15:0(d7) is prepared in sheet 1 but neither internal-standard library lists it.",
            ],
            cross["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
