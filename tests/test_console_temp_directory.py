"""A repository unit's Console keeps its temporary files on the analysis volume, and they are settled after it.

MS-DIAL writes every stream it saves - the whole loaded-library copy among them, 0.86 GB with the private
positive-mode library - to Path.GetTempFileName() before moving it into place (DirectoryTreeStreamManager). With
no TMP or TEMP of its own, every campaign run wrote that private copy through the user profile's TEMP on C:.
Each Console Interactive starts for a repository unit now gets <workspace root>/_tmp/<job id>; what is left
there afterwards is listed, a library copy goes to the library-copy store, and the rest is removed. A
laboratory run keeps the TEMP its user has.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import server
    from msdial_app.library_copy_store import LibraryCopyStore
    from msdial_app.repository_reanalysis import _write_json, read_manifest
    from msdial_app.workflow import prepare_rt_correction_run, run_console

# A stand-in Console that writes what MS-DIAL leaves in its TEMP when it is stopped mid-save: a library copy
# laid out as MS-DIAL writes one, and a scratch file.
_LEAVES = r"""
import os, sys, tempfile, zipfile
folder = tempfile.gettempdir()
print("TEMP=" + os.environ.get("TEMP", ""))
print("TMP=" + os.environ.get("TMP", ""))
with zipfile.ZipFile(os.path.join(folder, "tmpA1B2.tmp"), "w") as archive:
    for name, data in (("MetabolomicsDB\\LbmDB\\DataBase", b"lbm" * 50), ("Storage", b"settings")):
        info = zipfile.ZipInfo("x", date_time=(2026, 10, 1, 0, 0, 0))
        info.filename = name
        archive.writestr(info, data)
with open(os.path.join(folder, "tmpC3D4.tmp"), "wb") as handle:
    handle.write(b"x" * 7)
sys.exit(0)
"""


class TheConsoleSeesItsTemp(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_tmp_and_temp_are_the_directory_given_and_it_is_made(self) -> None:
        temp = self.root / "_tmp" / "job1"
        lines: list[str] = []
        script = "import os, tempfile; print(os.environ['TEMP']); print(os.environ['TMP']); print(tempfile.gettempdir())"
        code = run_console({"command": [sys.executable, "-u", "-c", script]}, lines.append, temp_directory=temp)

        self.assertEqual(0, code)
        self.assertEqual([str(temp)] * 3, [os.path.normcase(os.path.abspath(line)) == os.path.normcase(str(temp))
                                           and str(temp) for line in lines])
        self.assertTrue(temp.is_dir())

    def test_without_one_the_console_has_the_temp_it_always_had(self) -> None:
        lines: list[str] = []
        run_console({"command": [sys.executable, "-u", "-c", "import os; print(os.environ.get('TEMP', ''))"]},
                    lines.append)
        self.assertEqual([os.environ.get("TEMP", "")], lines)


class TheJobsSettleIt(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.workspace = self.root / "metabolights" / "MTBLS-SYN" / "unit-syn"
        self.output = self.workspace / "output"
        self.output.mkdir(parents=True)
        self.manifest = self.workspace / "provenance" / "run-manifest.json"
        _write_json(self.manifest, {
            "status": "preflight_passed", "workspace": str(self.workspace), "raw_retention_policy": "keep",
            "project": {"repository": "metabolights", "accession": "MTBLS-SYN", "analysis_unit_id": "unit-syn"},
        })
        self.jobs: dict = {}
        for item in (patch.object(server, "JOBS", self.jobs), patch.object(server, "PROCESSES", {}),
                     patch.object(server, "_persist_jobs_locked", lambda: None)):
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def preparation(self, command: list[str], manifest: bool = True) -> dict:
        return {"command": command, "run_directory": str(self.output), "export_folder_path": str(self.output),
                "repository_run_manifest": str(self.manifest) if manifest else "",
                "expected_analysis_exports": [], "qa_matrix_expected": False}

    def test_a_repository_run_keeps_its_temp_under_the_workspace_root_and_leaves_nothing_there(self) -> None:
        preparation = self.preparation([sys.executable, "-u", "-c", _LEAVES])
        self.jobs["job1"] = {"id": "job1", "status": "running", "kind": "run", "logs": [], "preparation": preparation}
        lines: list[str] = []

        code = server._run_console_for_job("job1", "run", preparation, lines.append)

        temp = self.root / "_tmp" / "job1"
        self.assertEqual(0, code, lines)
        self.assertIn(f"TEMP={temp}", lines)
        self.assertIn(f"TMP={temp}", lines)
        self.assertFalse(temp.exists(), "settled and removed")
        settled = self.jobs["job1"]["console_outcome"]["console_temp"]
        self.assertEqual(("_tmp/job1", True), (settled["directory"], settled["redirected"]))
        self.assertEqual({"tmpA1B2.tmp", "tmpC3D4.tmp"}, {item["name"] for item in settled["leftovers"]})
        [copy] = settled["library_copies"]
        self.assertEqual("stored", copy["outcome"])
        store = LibraryCopyStore(self.root)
        self.assertIsNone(store.problem(copy["object_id"], full=True))
        self.assertEqual("console_temp", store.entry(copy["object_id"])["ingested"]["how"])
        [attempt] = read_manifest(self.manifest)["run_attempts"]
        self.assertEqual("_tmp/job1", attempt["detail"]["console_temp"]["directory"])
        self.assertTrue(any("were left in _tmp/job1 and removed" in line for line in lines), lines)
        recorded = json.dumps(attempt["detail"]["console_temp"]) + json.dumps(settled)
        self.assertNotIn(str(self.root), recorded)
        self.assertNotIn(str(self.root).replace("\\", "\\\\"), recorded)

    def test_a_laboratory_run_is_given_no_temp_of_ours(self) -> None:
        seen: list = []

        def console(_preparation, _log, **watch):
            seen.append(watch.get("temp_directory"))
            return 0

        preparation = self.preparation(["MSDIALCUI.exe"], manifest=False)
        self.jobs["local1"] = {"id": "local1", "status": "running", "kind": "run", "logs": [],
                               "preparation": preparation}
        with patch.object(server, "run_console", console):
            server._run_console_for_job("local1", "run", preparation, lambda _line: None)
        self.assertEqual([None], seen)
        self.assertFalse((self.root / "_tmp").exists())

    def test_the_retention_time_preview_of_a_unit_is_given_it_too(self) -> None:
        seen: list = []

        def console(_preparation, _log, **watch):
            seen.append(watch.get("temp_directory"))
            return 1

        raw = self.workspace / "raw" / "sample.mzML"
        raw.parent.mkdir(parents=True)
        raw.write_text("raw", encoding="ascii")
        anchor = self.root / "anchors.txt"
        anchor.write_text("Name\tRT\nSTD1\t7.5\n", encoding="ascii")
        console_file = self.root / "MSDIALCUI.dll"
        console_file.write_text("", encoding="ascii")
        state = {
            "project_type": "lcms",
            "files": [{"file_path": str(raw), "file_name": "sample", "acquisition_type": "DDA"}],
            "console_path": str(console_file),
            "template_path": str(server.RESOURCES / "msdial_console_param4lipidomics.txt"),
            "output_root": str(self.workspace / "rt-preview"),
            "ion_mode": "Negative",
            "rt_correction_anchor_path": str(anchor),
            "rt_correction_selection_path": "",
            "repository_run_manifest": str(self.manifest),
        }
        with patch("msdial_app.workflow.console_capabilities", return_value={"capabilities": []}):
            preparation = prepare_rt_correction_run(state)
        # The preparation carries the unit itself, as /api/rt-correction/run receives it.
        self.assertEqual(str(self.manifest), preparation["repository_run_manifest"])
        self.jobs["rt1"] = {"id": "rt1", "status": "queued", "kind": "rt_correction", "logs": [],
                            "preparation": preparation}
        with patch.object(server, "run_console", console):
            server._run_rt_correction_job("rt1", preparation)
        self.assertEqual([self.root / "_tmp" / "rt1"], seen)
        self.assertEqual("_tmp/rt1", self.jobs["rt1"]["console_temp"]["directory"])

    def test_a_unit_whose_root_cannot_be_located_runs_and_says_so_after_the_console(self) -> None:
        _write_json(self.manifest, {"status": "preflight_passed", "project": {"analysis_unit_id": "unit-a"}})
        seen: list = []

        def console(_preparation, log, **watch):
            seen.append(watch.get("temp_directory"))
            log("the Console's first line")
            return 0

        preparation = self.preparation(["MSDIALCUI.exe"])
        self.jobs["job2"] = {"id": "job2", "status": "running", "kind": "run", "logs": [], "preparation": preparation}
        lines: list[str] = []
        with patch.object(server, "run_console", console):
            server._run_console_for_job("job2", "run", preparation, lines.append)
        self.assertEqual([None], seen)
        self.assertEqual("the Console's first line", lines[0])
        self.assertIn("system TEMP directory", lines[-1])
        self.assertFalse(self.jobs["job2"]["console_outcome"]["console_temp"]["redirected"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
