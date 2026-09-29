"""The RT-correction preview must not write into the checkout's resources folder.

The Console's ConfigParser.ReadForLcmsParameter, at the pinned 31dea2b39 and on master, writes
<method>.keys.json beside the method file it was given. The preview used to give it the
parameter template itself, which by default is resources/msdial_console_param4lipidomics.txt,
so every preview left a key record in the code checkout.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from msdial_app.server import RESOURCES
from msdial_app.workflow import (
    RT_CORRECTION_METHOD_FILE,
    RT_CORRECTION_REVIEW_CAPABILITY,
    prepare_rt_correction_run,
    run_console,
)

DEFAULT_TEMPLATE = RESOURCES / "msdial_console_param4lipidomics.txt"


def _snapshot(directory: Path) -> dict[str, str]:
    return {
        path.relative_to(directory).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.rglob("*"))
        if path.is_file()
    }


def _method_argument(command: list[str]) -> Path:
    return Path(command[command.index("-m") + 1])


class _Console:
    """Stands in for the Console process: writes the key record where ReadForLcmsParameter does."""

    def __init__(self, command: list[str], **_: object) -> None:
        method = _method_argument(command).resolve()
        # Path.Combine(GetDirectoryName(GetFullPath(m)), GetFileNameWithoutExtension(m) + ".keys.json")
        (method.parent / f"{method.stem}.keys.json").write_text(
            json.dumps({"schema": "msdial-method-file-keys.v1", "method_file": method.name}),
            encoding="utf-8",
        )
        self.stdout = iter(["RT correction EIC audit CSV: written\n"])

    def wait(self, timeout: float | None = None) -> int:
        return 0


class RtCorrectionPreviewMethodTests(unittest.TestCase):
    def _state(self, root: Path, template: Path, selection: Path | None) -> dict[str, object]:
        raw = root / "sample.mzML"
        raw.write_text("raw", encoding="ascii")
        anchor = root / "anchors.txt"
        anchor.write_text("Name\tRT\nSTD1\t7.5\n", encoding="ascii")
        console = root / "MSDIALCUI.dll"
        console.write_text("", encoding="ascii")
        return {
            "project_type": "lcms",
            "files": [{"file_path": str(raw), "file_name": "sample", "acquisition_type": "DDA"}],
            "console_path": str(console),
            "template_path": str(template),
            "output_root": str(root / "output"),
            "ion_mode": "Negative",
            "rt_correction_anchor_path": str(anchor),
            "rt_correction_selection_path": str(selection) if selection else "",
        }

    def test_method_argument_is_never_under_resources(self) -> None:
        self.assertTrue(DEFAULT_TEMPLATE.is_file(), DEFAULT_TEMPLATE)
        resources = RESOURCES.resolve()
        for capabilities in ([], [RT_CORRECTION_REVIEW_CAPABILITY]):
            for with_selection in (False, True):
                with self.subTest(capabilities=capabilities, selection=with_selection), \
                        tempfile.TemporaryDirectory() as temporary:
                    root = Path(temporary)
                    selection = root / "selection.tsv" if with_selection else None
                    if selection is not None:
                        selection.write_text("selection", encoding="ascii")
                    state = self._state(root, DEFAULT_TEMPLATE, selection)
                    with patch(
                        "msdial_app.workflow.console_capabilities",
                        return_value={"capabilities": capabilities},
                    ):
                        prepared = prepare_rt_correction_run(state)

                    method = _method_argument(prepared["command"]).resolve()
                    self.assertFalse(method.is_relative_to(resources), method)
                    self.assertEqual((root / "output").resolve(), method.parent)
                    self.assertEqual(RT_CORRECTION_METHOD_FILE, method.name)
                    self.assertEqual(DEFAULT_TEMPLATE.read_bytes(), method.read_bytes())
                    self.assertEqual(str(DEFAULT_TEMPLATE.resolve()), prepared["template_file"])
                    self.assertEqual(str(method), prepared["method_file"])

    def test_preview_leaves_resources_unchanged(self) -> None:
        before = _snapshot(RESOURCES)
        # Should the preview regress, the stand-in Console writes into resources/; take back
        # only what appeared during this test.
        self.addCleanup(
            lambda: [
                path.unlink()
                for path in RESOURCES.rglob("*")
                if path.is_file() and path.relative_to(RESOURCES).as_posix() not in before
            ]
        )
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = self._state(root, DEFAULT_TEMPLATE, None)
            output = root / "output"
            output.mkdir()
            # A run's own key record in the same directory, which the preview must not replace.
            run_keys = output / "method.keys.json"
            run_keys.write_text('{"method_file": "method.txt"}', encoding="utf-8")
            with patch(
                "msdial_app.workflow.console_capabilities",
                return_value={"capabilities": [RT_CORRECTION_REVIEW_CAPABILITY]},
            ):
                prepared = prepare_rt_correction_run(state)
            with patch("msdial_app.workflow.subprocess.Popen", _Console):
                self.assertEqual(0, run_console(prepared, lambda line: None))

            self.assertEqual(before, _snapshot(RESOURCES))
            self.assertTrue((output / "rt_correction_method.keys.json").is_file())
            self.assertEqual('{"method_file": "method.txt"}', run_keys.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
