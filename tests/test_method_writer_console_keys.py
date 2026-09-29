"""A template line the MS-DIAL Console reads as one of Interactive's settings is Interactive's to write.

The writer matched only an exact 'key:' prefix and inserted any key it did not find at the top of the
file. A template line under a Console alias ('Console alignment light mode'), with '=' or with a space
before the colon, was left in place below it. Since MsdialWorkbench #817 every Console reader takes the
last line that sets a value, so that template line decided the run, whatever Interactive had validated.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.workflow import CONSOLE_KEY_ALIASES, _write_method, console_method_key


def _written(template: str, **state) -> list[str]:
    with tempfile.TemporaryDirectory() as temporary:
        template_path = Path(temporary) / "template.txt"
        template_path.write_text(template, encoding="utf-8")
        method = Path(temporary) / "method.txt"
        _write_method(method, {"project_type": "lcms", "template_path": str(template_path),
                               "target_omics": "Metabolomics", **state})
        return method.read_text(encoding="utf-8").splitlines()


def _settings(lines: list[str], key: str) -> list[str]:
    """The values of every line the Console reads as the setting `key`."""
    group = next((g for g in CONSOLE_KEY_ALIASES if key in g), frozenset({key}))
    values = []
    for line in lines:
        found = console_method_key(line)
        if found in group:
            values.append(line[len(line.split(":", 1)[0]) + 1:].strip() if ":" in line else line.split("=", 1)[1].strip())
    return values


class ConsoleMethodKeyTests(unittest.TestCase):
    def test_the_key_is_read_as_the_console_reads_it(self) -> None:
        self.assertEqual("minimum peak height", console_method_key("Minimum peak height = 1000"))
        self.assertEqual("alignment light mode", console_method_key("Alignment light mode : True"))
        self.assertEqual("lbm file path", console_method_key("LBM file path: C:\\lib\\a=b.lbm2"))
        self.assertEqual("x", console_method_key("X=a: b"))
        for line in ("", "#", "# Annotation parameter: x", "   # note", "no separator"):
            with self.subTest(line=line):
                self.assertIsNone(console_method_key(line))


class WriterTests(unittest.TestCase):
    def test_an_alias_spelling_is_written_with_interactives_value(self) -> None:
        lines = _written(
            "Console alignment light mode: True\nLBM annotation priority: 1\n",
            alignment_light_mode=False, lbm_priority=3,
        )

        self.assertEqual(["False"], _settings(lines, "alignment light mode"))
        self.assertEqual(["3"], _settings(lines, "lbm annotator priority"))

    def test_another_separator_is_written_with_interactives_value(self) -> None:
        lines = _written(
            "Minimum peak height = 1000\nAlignment light mode : True\nLBM annotator priority = 1\n",
            minimum_peak_height=9000, alignment_light_mode=False, lbm_priority=3,
        )

        self.assertEqual(["9000"], _settings(lines, "minimum peak height"))
        self.assertEqual(["False"], _settings(lines, "alignment light mode"))
        self.assertEqual(["3"], _settings(lines, "lbm annotator priority"))

    def test_every_line_of_a_repeated_setting_carries_interactives_value(self) -> None:
        lines = _written("Minimum peak height: 100\nMinimum peak height: 200\n", minimum_peak_height=9000)

        self.assertEqual(["9000", "9000"], _settings(lines, "minimum peak height"))

    def test_a_template_in_the_canonical_spelling_is_written_as_before(self) -> None:
        lines = _written("Minimum peak height: 100\nAlignment light mode: True\n",
                         minimum_peak_height=9000, alignment_light_mode=False)

        self.assertIn("Minimum peak height: 9000", lines)
        self.assertIn("Alignment light mode: False", lines)
        self.assertEqual(1, sum(1 for line in lines if console_method_key(line) == "minimum peak height"))

    def test_comments_are_passed_through(self) -> None:
        lines = _written("# Minimum peak height: 100\nMinimum peak height: 200\n", minimum_peak_height=9000)

        self.assertIn("# Minimum peak height: 100", lines)
        self.assertEqual(["9000"], _settings(lines, "minimum peak height"))


if __name__ == "__main__":
    unittest.main()
