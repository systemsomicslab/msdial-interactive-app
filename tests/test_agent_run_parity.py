"""The agent path and the GUI path must produce the same record for the same unit.

The laboratory's stated architecture is that MS-DIAL Interactive offers both a GUI and an MCP
surface, a person uses the GUI, an agent uses MCP, and *both drive the same functions*. They did
not. `prepare_run` returns an explicit dict, and `repository_run_manifest` was not in it; the GUI
endpoint copied the key back onto the result by hand afterwards and `/api/agent/run` did not.

`_run_job` gates the entire repository-retention block on `if manifest_text:` -- the mzTab validation
record, the retained-artifact inventory and the retention verdict all live inside it. So an
agent-driven repository run wrote none of them and its raw data could never be cleaned up, while the
same unit run from the GUI wrote all three. Nothing failed; the record was simply thinner on one path
than the other, which is the hardest kind of difference to notice.

The fix is that prepare_run carries the keys through itself, so there is no second place to remember.
These tests hold that: the key must survive prepare_run, and the two paths must agree.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.workflow import prepare_run

TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"


class AgentRunCarriesTheRepositoryManifest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / "sample.mzML").write_text("", encoding="ascii")
        self.console = self.root / "MSDIALCUI.exe"
        self.console.write_bytes(b"not really a console binary")
        self.lbm = self.root / "lab.lbm2"
        self.lbm.write_bytes(b"laboratory library")
        self.manifest = self.root / "run-manifest.json"
        self.manifest.write_text(
            json.dumps({"workspace": str(self.root), "status": "prepared"}), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _answers(self, **extra) -> dict:
        answers = {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "existing",
            "libraries": {"lbm_path": str(self.lbm)},
            "run_qa": False,
            "generate_materials_methods": False,
            "console_path": str(self.console),
            "template_path": str(TEMPLATE),
            "output_root": str(self.root / "out"),
            "class_assignment_confirmed": True,
        }
        answers.update(extra)
        return answers

    def _prepare(self, **extra):
        plan = build_guided_plan(str(self.root), self._answers(**extra))
        self.assertIsNotNone(plan["workflow"], plan["blockers"])
        return plan, prepare_run(plan["workflow"])

    def test_the_manifest_an_agent_supplies_survives_prepare_run(self) -> None:
        """The MCP tool passes the manifest as a workflow override; it has to come out the far end.

        msdial_prepare_repository_reanalysis puts repository_run_manifest into
        answers["workflow_overrides"], build_guided_plan folds those into the workflow state, and
        _run_job reads the key off the *preparation*. That last hop is where it used to be lost.
        """
        overrides = {
            "repository_run_manifest": str(self.manifest),
            "repository_raw_retention_policy": "delete",
        }
        plan, prepared = self._prepare(workflow_overrides=overrides)

        self.assertEqual(str(self.manifest), plan["workflow"]["repository_run_manifest"])
        self.assertEqual(
            str(self.manifest),
            prepared["repository_run_manifest"],
            "an agent-driven run would write no validation record and never release its raw data",
        )
        self.assertEqual("delete", prepared["repository_raw_retention_policy"])

    def test_both_paths_prepare_the_same_repository_keys(self) -> None:
        """What the GUI used to add by hand is now what prepare_run returns for either caller."""
        overrides = {
            "repository_run_manifest": str(self.manifest),
            "repository_raw_retention_policy": "delete",
        }
        _, from_agent = self._prepare(workflow_overrides=overrides)

        # The GUI never calls build_guided_plan: /api/run holds its own workflow state and the two
        # keys sit on it directly. Setting them on a plain state is what that path actually looks
        # like. (Supplying them as ordinary answers would NOT reach the workflow -- only
        # workflow_overrides is folded in -- which is why the agent path needs the override block.)
        gui_state = dict(build_guided_plan(str(self.root), self._answers())["workflow"])
        gui_state.update(overrides)
        from_gui = prepare_run(gui_state)

        for key in ("repository_run_manifest", "repository_raw_retention_policy"):
            self.assertEqual(from_gui[key], from_agent[key], key)

    def test_a_local_analysis_still_carries_no_manifest_and_keeps_its_raw_data(self) -> None:
        """An ordinary laboratory analysis has no repository unit, and must not acquire one.

        The empty string is what tells the execution gate there is nothing to gate and tells
        _run_job there is no unit manifest to write into. "keep" is the default the contract sets,
        and defaulting to anything else here would put a deletion policy on data nobody downloaded.
        """
        _, prepared = self._prepare()

        self.assertEqual("", prepared["repository_run_manifest"])
        self.assertEqual("keep", prepared["repository_raw_retention_policy"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
