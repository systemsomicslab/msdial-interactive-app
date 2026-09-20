"""The number in the method file must be a number the Console can read.

MS-DIAL's Console parses several method-file keys with int.TryParse and, until 2026-09-20,
reported the key as consumed whether or not the parse had succeeded. A value it could not read was
therefore discarded in silence and the parameter kept its built-in default.

Interactive writes those values from Python floats, which print their decimal point even when they
name a whole number, so every method file in the reanalysis workspace carried
"Minimum peak height: 500.0" -- and every one of them was rejected. MinimumAmplitude stayed at the
built-in 1000 while the retained method file, the publication report and the Methods paragraph all
said 500. The contract's zero-threshold diagnostic exists to choose that number; nothing it chose
ever reached the Console.

The Console reader has been fixed to accept either spelling. This is the other half, and it is the
half that takes effect first: the Console the pipeline resolves is built from a branch that takes
changes from master later, so a method file written today is read by yesterday's parser.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.workflow import _method_value, _write_method

TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"


class AWholeNumberIsWrittenAsAWholeNumber(unittest.TestCase):
    def test_a_threshold_reaches_the_file_without_a_decimal_point(self) -> None:
        """THE REGRESSION. int.TryParse("500.0") is false and nothing said so."""
        self.assertEqual("500", _method_value(500.0))

    def test_the_diagnostic_threshold_of_zero_survives(self) -> None:
        self.assertEqual("0", _method_value(0.0))
        self.assertEqual("0", _method_value(0))

    def test_a_value_that_is_not_whole_keeps_every_digit(self) -> None:
        """Tolerances are genuinely fractional and must not be rounded to look tidy."""
        self.assertEqual("0.01", _method_value(0.01))
        self.assertEqual("0.025", _method_value(0.025))
        self.assertEqual("0.5", _method_value(0.5))

    def test_a_boolean_is_still_True_or_False_and_not_1_or_0(self) -> None:
        """bool is an int in Python, so this is the one that breaks quietly if it is missed."""
        self.assertEqual("True", _method_value(True))
        self.assertEqual("False", _method_value(False))

    def test_text_is_left_exactly_as_it_is(self) -> None:
        self.assertEqual("LinearWeightedMovingAverage", _method_value("LinearWeightedMovingAverage"))
        self.assertEqual("", _method_value(""))
        self.assertEqual(r"C:\libraries\lab.msp", _method_value(r"C:\libraries\lab.msp"))

    def test_an_infinity_or_a_nan_is_not_silently_turned_into_an_integer(self) -> None:
        self.assertEqual("inf", _method_value(float("inf")))
        self.assertEqual("nan", _method_value(float("nan")))


class TheWrittenMethodFileIsReadableByTheConsole(unittest.TestCase):
    """End to end through the writer, against the shipped lipidomics template."""

    def _method(self, **state) -> dict[str, str]:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "method.txt"
            _write_method(
                path,
                {
                    "template_path": str(TEMPLATE),
                    "project_type": "lcms",
                    "ion_mode": "Negative",
                    "target_omics": "Lipidomics",
                    **state,
                },
            )
            written = {}
            for line in path.read_text(encoding="utf-8").splitlines():
                if ":" in line and not line.lstrip().startswith("#"):
                    key, _, value = line.partition(":")
                    written[key.strip().casefold()] = value.strip()
            return written

    def test_the_accepted_threshold_appears_as_an_integer(self) -> None:
        written = self._method(minimum_peak_height=500.0)

        self.assertEqual("500", written["minimum peak height"])

    def test_every_key_the_console_parses_as_an_integer_is_written_as_one(self) -> None:
        """The keys whose C# arm is int.TryParse. A decimal point on any of them is discarded.

        Read from ConfigParser.cs on 2026-09-20: smoothing level, average peak width, minimum peak
        width, minimum peak height, max charge number, number of threads, alignment reference file
        id, non/fully labeled reference id, isotope tracking dictionary id, and the two CorrDec
        counts.
        """
        written = self._method(
            minimum_peak_height=300.0,
            smoothing_level=3.0,
            average_peak_width=20.0,
            minimum_peak_width=5.0,
            number_of_threads=8.0,
            max_charge_number=2.0,
        )
        integer_keys = (
            "minimum peak height", "smoothing level", "average peak width",
            "minimum peak width", "number of threads", "maximum charge number",
        )
        for key in integer_keys:
            if key in written and written[key]:
                self.assertNotIn(".", written[key], f"{key} was written with a decimal point")

    def test_a_tolerance_keeps_its_fraction(self) -> None:
        written = self._method(ms1_tolerance=0.01, mass_slice_width=0.1)

        self.assertEqual("0.01", written["ms1 tolerance for centroid"])
        self.assertEqual("0.1", written["mass slice width"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
