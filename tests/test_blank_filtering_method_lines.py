"""Every method file Interactive writes names one blank filtering comparison, and its lines agree.

The shipped templates wrote 'Blank filtering: SampleMaxOverBlankAve', 'Sample max / blank average: 5' and
'Sample average / blank average: 5'. A Console before MsdialWorkbench #823 read only the first two; since
#823 the Console refuses, before any processing, a method file whose blank filtering lines disagree, and
both ratio keys at once is such a file. The templates now carry the two lines both Consoles read alike,
and a template whose lines disagree is refused by Interactive, naming the template, rather than copied
into a method file the Console refuses.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import RESOURCES
from msdial_app.workflow import (
    _write_method,
    blank_filtering_conflict,
    console_method_key,
    load_parameter_template,
    prepare_rt_correction_run,
)

SHIPPED_TEMPLATES = {
    "lcms": RESOURCES / "msdial_console_param4lipidomics.txt",
    "gcms": RESOURCES / "gcms_console_param_kovats.txt",
}
BLANK_FILTERING_KEYS = {
    "blank filtering",
    "fold change for blank filtering",
    "sample max / blank average",
    "sample average / blank average",
}
LEGACY_LINES = (
    "Blank filtering: SampleMaxOverBlankAve\n"
    "Sample max / blank average: 5\n"
    "Sample average / blank average: 5\n"
)


def _blank_filtering_lines(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if console_method_key(line) in BLANK_FILTERING_KEYS]


def _write(directory: Path, template: Path, project_type: str) -> list[str]:
    method = directory / f"{project_type}_method.txt"
    _write_method(method, {"project_type": project_type, "template_path": str(template),
                           "target_omics": "Metabolomics"})
    return method.read_text(encoding="utf-8").splitlines()


class ShippedTemplateTests(unittest.TestCase):
    def test_each_shipped_template_names_one_comparison_both_consoles_read_alike(self) -> None:
        for project_type, template in SHIPPED_TEMPLATES.items():
            with self.subTest(project_type=project_type):
                lines = template.read_text(encoding="utf-8-sig").splitlines()
                self.assertIsNone(blank_filtering_conflict(lines))
                self.assertEqual(
                    ["Blank filtering: SampleMaxOverBlankAve", "Sample max / blank average: 5"],
                    _blank_filtering_lines(lines),
                )

    def test_no_shipped_template_is_refused_on_loading(self) -> None:
        for project_type, template in SHIPPED_TEMPLATES.items():
            with self.subTest(project_type=project_type):
                load_parameter_template(template)


class GeneratedMethodTests(unittest.TestCase):
    def test_a_generated_lcms_and_gcms_method_has_exactly_one_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            for project_type, template in SHIPPED_TEMPLATES.items():
                with self.subTest(project_type=project_type):
                    lines = _write(Path(temporary), template, project_type)
                    self.assertIsNone(blank_filtering_conflict(lines))
                    self.assertEqual(
                        ["Blank filtering: SampleMaxOverBlankAve", "Sample max / blank average: 5"],
                        _blank_filtering_lines(lines),
                    )


class RefusalTests(unittest.TestCase):
    def _template(self, directory: Path, blank_lines: str) -> Path:
        template = directory / "template.txt"
        template.write_text("Minimum peak height: 1000\n# Filtering\n" + blank_lines, encoding="utf-8")
        return template

    def test_the_former_shipped_lines_are_refused_before_a_method_file_is_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            template = self._template(directory, LEGACY_LINES)
            for project_type in ("lcms", "gcms"):
                with self.subTest(project_type=project_type):
                    with self.assertRaises(ValueError) as caught:
                        _write(directory, template, project_type)
                    message = str(caught.exception)
                    self.assertIn(str(template), message)
                    self.assertIn("'Sample max / blank average: 5'", message)
                    self.assertIn("'Sample average / blank average: 5'", message)
                    self.assertFalse((directory / f"{project_type}_method.txt").exists())

    def test_a_template_whose_lines_disagree_is_refused_on_loading(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            template = self._template(Path(temporary), LEGACY_LINES)
            with self.assertRaisesRegex(ValueError, "blank filtering"):
                load_parameter_template(template)

    def test_the_rt_correction_preview_refuses_the_template_it_would_copy(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            template = self._template(directory, LEGACY_LINES)
            console = directory / "MsdialConsoleApp.exe"
            anchor = directory / "anchors.txt"
            raw = directory / "sample.mzML"
            for path in (console, anchor, raw):
                path.write_text("", encoding="utf-8")
            output = directory / "preview"
            with self.assertRaisesRegex(ValueError, "Sample average / blank average"):
                prepare_rt_correction_run({
                    "project_type": "lcms",
                    "files": [{"file_path": str(raw)}],
                    "console_path": str(console),
                    "rt_correction_anchor_path": str(anchor),
                    "output_root": str(output),
                    "template_path": str(template),
                })
            self.assertFalse(any(output.iterdir()))


class ConflictRuleTests(unittest.TestCase):
    """The rule of the Console's BlankFilteringConflict (MsdialWorkbench #823)."""

    def test_disagreeing_lines(self) -> None:
        cases = {
            "both ratio keys, equal values": LEGACY_LINES,
            "both ratio keys alone": "Sample max / blank average: 5\nSample average / blank average: 3\n",
            "mode against shorthand": "Blank filtering: SampleAveOverBlankAve\nSample max / blank average: 5\n",
            "numeric mode against shorthand": "Blank filtering: 0\nSample average / blank average: 5\n",
            "fold change against shorthand": "Fold change for blank filtering: 3\nSample max / blank average: 5\n",
            "another separator and case": "BLANK FILTERING = sampleaveoverblankave\nsample max / blank average = 5\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(blank_filtering_conflict(text.splitlines()))

    def test_agreeing_lines(self) -> None:
        cases = {
            "no blank filtering lines": "Minimum peak height: 1000\n",
            "the shipped pair": "Blank filtering: SampleMaxOverBlankAve\nSample max / blank average: 5\n",
            "the canonical pair": "Blank filtering: SampleAveOverBlankAve\nFold change for blank filtering: 3\n",
            "agreeing shorthand": (
                "Blank filtering: sampleaveoverblankave\nFold change for blank filtering: 5.0\n"
                "Sample average / blank average: 5\n"
            ),
            "one key written twice": "Sample max / blank average: 3\nSample max / blank average: 5\n",
            "a blank ratio line": "Sample max / blank average: 5\nSample average / blank average:\n",
            "an unreadable ratio line": "Sample max / blank average: 5\nSample average / blank average: five\n",
            "an unreadable mode": "Blank filtering: 2\nSample max / blank average: 5\n",
            "a non-ASCII digit mode": "Blank filtering: ¹\nSample max / blank average: 5\n",
            "a fold change equal as a single-precision float": (
                "Fold change for blank filtering: 5.00000001\nSample max / blank average: 5\n"
            ),
            "a commented line": "Sample max / blank average: 5\n# Sample average / blank average: 5\n",
        }
        for name, text in cases.items():
            with self.subTest(name):
                self.assertIsNone(blank_filtering_conflict(text.splitlines()))


if __name__ == "__main__":
    unittest.main()
