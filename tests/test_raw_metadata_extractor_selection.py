"""Which raw-metadata extractor a preflight runs, and what a campaign accepts.

The pins are data (PINNED_BUILDS); a candidate is labelled with where it came from; a campaign runs only
the first extractor named, and only when it inspects as verified and pinned. Every extractor here is a
synthetic file; nothing is run, and no setting of this machine is read or written.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_raw_metadata_extractor as fixtures
from msdial_app import mcp_server
from msdial_app import raw_metadata_extractor as extractor
from msdial_app.raw_metadata_extractor import (
    PINNED_BUILDS,
    RawMetadataExtractorRefused,
    campaign_refusal,
    check_raw_metadata_extractors,
    inspect_raw_metadata_extractor,
    pinned_build,
    raw_metadata_extractor_candidates,
    record_build,
    require_campaign_extractor,
    select_raw_metadata_extractor,
    set_raw_metadata_extractor_path,
)


def _recorded_build(root: Path, raw_head: str = fixtures.RAW_HEAD, common_head: str = fixtures.COMMON_HEAD) -> Path:
    """A synthetic build with a matching record, whose sources are at the given heads."""
    raw, common, binary = fixtures._write_build(root, raw_head=raw_head, common_head=common_head)

    def state(tree_root):
        value = fixtures._git_state()(tree_root)
        value["head"] = raw_head if Path(tree_root).name == "msrawdataworkbench" else common_head
        return value

    with patch.object(extractor, "console_git_state", side_effect=state), patch.object(extractor, "_git", return_value=""):
        record_build(binary, raw, common, sdk_version="10.0.401")
    return binary


def _checkout(parent: Path) -> tuple[Path, Path]:
    """The working checkout's own builds, net48 and net8.0-windows, with no build record."""
    net48, net8 = extractor.working_checkout_extractor_paths(parent)
    for path in (net48, net8):
        path.parent.mkdir(parents=True)
        path.write_bytes(b"working checkout build " + path.parent.name.encode())
    return net48, net8


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()
        # The settings file this module reads and writes lives here, never in this machine's profile.
        self._environment = patch.dict(
            os.environ,
            {"LOCALAPPDATA": str(self.root / "local"), "XDG_CONFIG_HOME": str(self.root / "config")},
        )
        self._environment.start()
        os.environ.pop(extractor.EXTRACTOR_ENVIRONMENT_VARIABLE, None)

    def tearDown(self) -> None:
        self._environment.stop()
        self._directory.cleanup()

    def stub(self, name: str) -> Path:
        path = self.root / name / extractor.EXTRACTOR_BINARY
        path.parent.mkdir(parents=True)
        path.write_bytes(name.encode())
        return path


class PinnedBuildTests(_Scratch):
    def test_the_pins_are_data_with_the_built_pair_first(self) -> None:
        self.assertEqual(
            [
                (fixtures.RAW_HEAD, fixtures.COMMON_HEAD, "built"),
                (fixtures.PREVIOUS_RAW_HEAD, fixtures.COMMON_HEAD, "built"),
                (fixtures.PLANNED_RAW_HEAD, fixtures.PLANNED_COMMON_HEAD, "planned"),
            ],
            [(entry["msrawdataworkbench"], entry["MsdialWorkbench"], entry["state"]) for entry in PINNED_BUILDS],
        )
        self.assertEqual("built", pinned_build(fixtures.RAW_HEAD.upper(), fixtures.COMMON_HEAD)["state"])
        self.assertIsNone(pinned_build(fixtures.RAW_HEAD, fixtures.PLANNED_COMMON_HEAD))

    def test_a_verified_build_of_the_built_pair_is_accepted_by_a_campaign(self) -> None:
        binary = _recorded_build(self.root / "pinned")

        inspection = require_campaign_extractor(binary)

        self.assertEqual(("verified", True, "built"), (inspection["provenance_status"], inspection["pinned"], inspection["pin_state"]))
        self.assertEqual(([], []), campaign_refusal(inspection))

    def test_a_build_of_the_planned_pair_is_recognised_but_not_pinned(self) -> None:
        binary = _recorded_build(self.root / "planned", fixtures.PLANNED_RAW_HEAD, fixtures.PLANNED_COMMON_HEAD)

        inspection = inspect_raw_metadata_extractor(binary)
        codes, reasons = campaign_refusal(inspection)

        self.assertEqual("verified", inspection["provenance_status"])
        self.assertEqual((False, "planned"), (inspection["pinned"], inspection["pin_state"]))
        self.assertEqual(["extractor_not_pinned"], codes)
        self.assertIn("planned pair that was never verified", reasons[0])

    def test_an_extractor_without_a_record_is_refused_by_a_campaign(self) -> None:
        binary = self.stub("record_less")

        with self.assertRaises(RawMetadataExtractorRefused) as refusal:
            require_campaign_extractor(binary)

        self.assertIsInstance(refusal.exception, ValueError)
        self.assertEqual(["extractor_absent", "extractor_not_pinned"], refusal.exception.codes)
        self.assertTrue(str(refusal.exception).startswith("raw_metadata_extractor_refused [extractor_absent, extractor_not_pinned]"))


class CandidateTests(_Scratch):
    def test_the_argument_the_setting_the_environment_and_the_working_checkout_in_that_order(self) -> None:
        argument, setting, environment = self.stub("argument"), self.stub("setting"), self.stub("environment")
        net48, net8 = _checkout(self.root / "checkout")

        candidates = raw_metadata_extractor_candidates(
            str(argument),
            settings={"raw_metadata_extractor_path": str(setting)},
            environment={"MSDIAL_RAW_METADATA_EXTRACTOR": str(environment)},
            checkout_parent=self.root / "checkout",
        )

        self.assertEqual(
            [
                ("argument", str(argument)),
                ("setting", str(setting)),
                ("environment", str(environment)),
                ("working_checkout_default", str(net48)),
                ("working_checkout_default", str(net8)),
            ],
            [(item["source"], item["path"]) for item in candidates],
        )

    def test_the_setting_outranks_the_environment_variable(self) -> None:
        setting, environment = self.stub("setting"), self.stub("environment")

        selected = select_raw_metadata_extractor(
            "",
            settings={"raw_metadata_extractor_path": str(setting)},
            environment={"MSDIAL_RAW_METADATA_EXTRACTOR": str(environment)},
            checkout_parent=self.root / "no-checkout",
        )

        self.assertEqual(("setting", str(setting)), (selected["source"], selected["path"]))

    def test_a_folder_stands_for_its_extractor_and_a_path_named_twice_is_listed_once(self) -> None:
        binary = self.stub("folder")

        candidates = raw_metadata_extractor_candidates(
            str(binary.parent),
            settings={"raw_metadata_extractor_path": str(binary)},
            environment={},
            checkout_parent=self.root / "no-checkout",
        )

        self.assertEqual(
            [("argument", str(binary))],
            [(item["source"], item["path"]) for item in candidates if item["exists"]],
        )
        # The working checkout's builds are listed as candidates even where none was built.
        self.assertEqual(["argument", "working_checkout_default", "working_checkout_default"], [item["source"] for item in candidates])

    def test_outside_a_campaign_a_missing_extractor_is_passed_over_as_before(self) -> None:
        net48, _net8 = _checkout(self.root / "checkout")

        selected = select_raw_metadata_extractor(
            str(self.root / "missing" / extractor.EXTRACTOR_BINARY),
            settings={},
            environment={},
            checkout_parent=self.root / "checkout",
        )

        self.assertEqual(("working_checkout_default", str(net48)), (selected["source"], selected["path"]))

    def test_a_campaign_refuses_the_working_checkout_default_a_missing_path_and_an_unverified_one(self) -> None:
        _checkout(self.root / "checkout")
        unverified = self.stub("unverified")
        cases = {
            "extractor_working_checkout_default": dict(configured="", settings={}),
            "extractor_missing": dict(configured=str(self.root / "missing.exe"), settings={}),
            "extractor_absent": dict(configured="", settings={"raw_metadata_extractor_path": str(unverified)}),
        }
        for code, options in cases.items():
            with self.subTest(code=code):
                with self.assertRaises(RawMetadataExtractorRefused) as refusal:
                    select_raw_metadata_extractor(
                        options["configured"],
                        campaign=True,
                        settings=options["settings"],
                        environment={},
                        checkout_parent=self.root / "checkout",
                    )
                self.assertEqual(code, refusal.exception.codes[0])

    def test_a_campaign_runs_a_verified_pinned_build_named_by_the_setting(self) -> None:
        _checkout(self.root / "checkout")
        binary = _recorded_build(self.root / "pinned")

        selected = select_raw_metadata_extractor(
            "",
            campaign=True,
            settings={"raw_metadata_extractor_path": str(binary)},
            environment={},
            checkout_parent=self.root / "checkout",
        )

        self.assertEqual(("setting", str(binary.resolve())), (selected["source"], selected["path"]))
        self.assertTrue(selected["inspection"]["pinned"])

    def test_the_check_reports_every_candidate_and_what_a_campaign_would_do(self) -> None:
        _checkout(self.root / "checkout")
        binary = _recorded_build(self.root / "pinned")

        report = check_raw_metadata_extractors(
            str(binary), settings={}, environment={}, checkout_parent=self.root / "checkout"
        )

        self.assertEqual(["argument", "working_checkout_default", "working_checkout_default"], [item["source"] for item in report["candidates"]])
        self.assertTrue(report["campaign_accepts"])
        self.assertEqual("verified", report["candidates"][0]["provenance_status"])
        self.assertEqual(["extractor_working_checkout_default", "extractor_absent", "extractor_not_pinned"], report["candidates"][1]["campaign_refusal_codes"])
        self.assertEqual(len(PINNED_BUILDS), len(report["pinned_builds"]))


class SettingTests(_Scratch):
    def settings_file(self) -> Path:
        from msdial_app.user_settings import settings_path

        return settings_path()

    def test_an_unverified_extractor_is_not_saved_unless_asked(self) -> None:
        binary = self.stub("unverified")

        with self.assertRaisesRegex(ValueError, "inspects as absent, not verified"):
            set_raw_metadata_extractor_path(binary)
        self.assertFalse(self.settings_file().exists())

        saved = set_raw_metadata_extractor_path(binary, allow_unverified=True)

        self.assertEqual(str(binary), saved["raw_metadata_extractor_path"])
        self.assertFalse(saved["campaign_accepts"])
        self.assertEqual(str(binary), json.loads(self.settings_file().read_text(encoding="utf-8"))["raw_metadata_extractor_path"])

    def test_a_verified_pinned_build_is_saved_and_a_campaign_would_accept_it(self) -> None:
        binary = _recorded_build(self.root / "pinned")

        saved = set_raw_metadata_extractor_path(binary.parent)

        self.assertEqual(str(binary.resolve()), saved["raw_metadata_extractor_path"])
        self.assertTrue(saved["campaign_accepts"])
        self.assertEqual(str(binary.resolve()), raw_metadata_extractor_candidates(environment={}, checkout_parent=self.root / "none")[0]["path"])

    def test_anything_but_the_extractor_is_refused(self) -> None:
        other = self.root / "MSDIALCUI.exe"
        other.write_bytes(b"console")

        with self.assertRaisesRegex(ValueError, "RawMetadataConsoleApp.exe"):
            set_raw_metadata_extractor_path(other, allow_unverified=True)


def _no_backend(*_args, **_kwargs):
    raise AssertionError("the extractor tools need no backend")


class McpToolTests(_Scratch):
    def test_the_check_and_set_tools_work_without_a_backend(self) -> None:
        binary = _recorded_build(self.root / "pinned")
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend), patch.object(
            mcp_server, "ROOT", self.root / "app"
        ):
            checked = mcp_server.msdial_check_raw_metadata_extractor(extractor_path=str(binary))
            saved = mcp_server.msdial_set_raw_metadata_extractor_path(extractor_path=str(binary))
            refused = mcp_server.msdial_set_raw_metadata_extractor_path(extractor_path=str(self.stub("unverified")))

        self.assertEqual("verified", checked["candidates"][0]["provenance_status"])
        self.assertTrue(checked["campaign_accepts"])
        self.assertTrue(saved["campaign_accepts"])
        self.assertEqual(("validation_error", False), (refused["reason"], refused["ok"]))

    def test_the_preflight_tool_reports_a_refused_extractor_under_a_campaign(self) -> None:
        from test_raw_metadata_preflight import _APPROVAL, _unit

        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML"], extra={"campaign_authorizations": [dict(_APPROVAL)]})
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend), patch.object(
            mcp_server, "ROOT", self.root / "app"
        ), patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=AssertionError("nothing is read")):
            result = mcp_server.msdial_repository_raw_metadata_preflight(
                extractor_path=str(stub), manifest_path=str(manifest)
            )

        self.assertFalse(result["ok"])
        self.assertEqual("raw_metadata_extractor_refused", result["reason"])
        self.assertEqual(["extractor_absent", "extractor_not_pinned"], result["codes"])

    def test_the_preflight_tool_runs_the_pinned_setting_and_returns_the_disposition(self) -> None:
        from test_raw_metadata_preflight import _APPROVAL, _Extractor, _unit

        manifest, _stub, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"], extra={"campaign_authorizations": [dict(_APPROVAL)]})
        binary = _recorded_build(self.root / "pinned")
        set_raw_metadata_extractor_path(binary)
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend), patch.object(
            mcp_server, "ROOT", self.root / "app"
        ), patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=_Extractor({"b.mzML": "fail"})):
            result = mcp_server.msdial_repository_raw_metadata_preflight(manifest_path=str(manifest))

        self.assertTrue(result["completed"], result)
        self.assertEqual("setting", result["extractor"]["selected_from"])
        self.assertTrue(result["extractor"]["pinned"])
        self.assertEqual(1, result["exit_code"])
        self.assertEqual(
            {"disposition": "run", "applied": True, "excluded_inputs": 1, "console_acquisition_type": "DDA"},
            {key: result["campaign_disposition"][key] for key in ("disposition", "applied", "excluded_inputs", "console_acquisition_type")},
        )
        self.assertTrue(result["execution_allowed"])


if __name__ == "__main__":
    unittest.main()
