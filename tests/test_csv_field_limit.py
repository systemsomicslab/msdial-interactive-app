"""An MS-DIAL text export whose MS/MS spectrum field is longer than Python's default csv field limit is read.

V5 (v5-mpst48-20261029): the negative part of MB-POST MPST000048 (ZenoTOF 7600 DDA) failed its peak-count
diagnostic twice with "field larger than field limit (131072)": 9 of the 19,623 rows of its .mdpeak carried an
MSMS spectrum of 136,199-270,713 characters. msdial_app raises the limit for the process when it is imported.
"""

from __future__ import annotations

import csv
import tempfile
import unittest
from pathlib import Path

import msdial_app
from msdial_app.workflow import parse_mdpeak

HEADER = ["Peak ID", "Name", "Height", "Simple dot product", "Weighted dot product", "Reverse dot product",
          "MSMS spectrum"]


class LargeSpectrumFieldTests(unittest.TestCase):
    def test_importing_the_package_raises_the_csv_field_limit(self) -> None:
        self.assertGreaterEqual(csv.field_size_limit(), 2**31 - 1)
        self.assertTrue(msdial_app.__version__)

    def test_an_mdpeak_with_a_spectrum_longer_than_the_default_limit_is_parsed(self) -> None:
        spectrum = " ".join(f"{100 + index * 0.0001:.4f}:{index % 997}" for index in range(24000))
        self.assertGreater(len(spectrum), 270_713)
        rows = [HEADER, ["0", "Unknown", "1200", "0", "0", "0", "100.0000:1"],
                ["1", "Unknown", "3400", "0", "0", "0", spectrum]]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "sample.mdpeak"
            path.write_text("\n".join("\t".join(row) for row in rows) + "\n", encoding="utf-8")
            summary = parse_mdpeak(path)
        self.assertEqual(summary["peak_count"], 2)


if __name__ == "__main__":
    unittest.main()
