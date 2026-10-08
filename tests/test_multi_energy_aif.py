"""Multi-energy AIF with a Console that has MsdialWorkbench#825 (Interactive 0.5.34).

The hold of 2026-10-07 was "until the patched Console". Where the configured Console's assembly carries #825's
multi-energy AIF processing, a unit whose AIF inputs record more than one MS2 collision energy runs as AIF; without
it the hold stands. Single-energy AIF runs as SWATH and AIF with an unrecorded energy is held, whatever the Console.

Inputs whose energy sets differ run as AIF with #825 too, on record (user decision, 2026-10-08, Interactive 0.5.36):
each file is processed with its own per-file representative collision energy. 0.5.34-0.5.35 held them.

Every Console here is a file holding (or not holding) the marker; no Console is started.
"""

import csv
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from subprocess import CompletedProcess

from test_raw_metadata_preflight import _APPROVAL, _Extractor, _PinnedExtractor, _Scratch, _header, _manifest, _unit

from msdial_app import workflow
from msdial_app.materials_methods import _methods_text, multi_energy_aif_evidence, supplementary_rows
from msdial_app.raw_metadata_preflight import (
    AIF_AS_SWATH_RULE,
    AIF_CE_BY_INPUT_KEY,
    AIF_CE_DIFFERS_HOLD,
    AIF_CE_SETS_DIFFER_RECORDED,
    AIF_CE_UNRECORDED_HOLD,
    AIF_MULTI_CE_BASIS,
    AIF_MULTI_CE_HOLD,
    AIF_MULTI_CE_RULE,
    aif_input_key,
    decide_disposition,
    multi_energy_aif_ready,
)
from msdial_app.repository_analysis_rows import build_repository_analysis_rows, write_analysis_csv
from msdial_app.repository_reanalysis import (
    classify_preflight,
    evaluate_repository_execution_gate,
    held_by_disposition,
    read_manifest,
    run_raw_metadata_preflight,
    update_manifest,
)
from msdial_app.workflow import (
    MULTI_ENERGY_AIF_CAPABILITY,
    MULTI_ENERGY_AIF_CONSOLE_MARKERS,
    configured_console_path,
    console_capabilities,
    multi_energy_aif_console,
)


# The one #825 message a local AIF patch build from before #825's representative-energy rule carries (01ac842c1).
PRE_825_MARKER = " nor a collision-energy file exists."


def _console(
    root: Path, *, with_825: bool, name: str = "MSDIALCUI.exe", markers: tuple[str, ...] | None = None
) -> Path:
    """A stand-in Console assembly: managed strings are UTF-16LE in the user-string heap, as in the real one.

    with_825 writes every #825 marker; markers, when given, writes those instead."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    body = b"MZ\x90\x00 stand-in assembly " + "LC-MS quality-assurance matrix:".encode("utf-16-le")
    for marker in markers if markers is not None else (MULTI_ENERGY_AIF_CONSOLE_MARKERS if with_825 else ()):
        body += b"\x00\x01" + marker.encode("utf-16-le")
    path.write_bytes(body)
    return path


def _no_configured_console():
    """No saved console_path setting and no MSDIAL_CONSOLE_PATH: the Console is only what a call names."""
    environment = {key: value for key, value in os.environ.items() if key != "MSDIAL_CONSOLE_PATH"}
    return (
        patch.object(workflow, "load_user_settings", return_value={}),
        patch.dict(os.environ, environment, clear=True),
    )


class _NoConfiguredConsole(_Scratch):
    def setUp(self) -> None:
        super().setUp()
        for patcher in _no_configured_console():
            patcher.start()
            self.addCleanup(patcher.stop)
        self.with_825 = _console(self.root / "console-825", with_825=True)
        self.without_825 = _console(self.root / "console-818", with_825=False)
        # A local AIF patch build: per-energy .dcl files, but an unannotated peak still read from the first energy.
        self.pre_825 = _console(self.root / "console-aif-patch", with_825=False, markers=(PRE_825_MARKER,))


class ConsoleProbeTests(_NoConfiguredConsole):
    def test_the_marker_in_the_assembly_is_the_capability(self) -> None:
        found = multi_energy_aif_console(self.with_825)

        self.assertEqual(
            (True, "multi_energy_aif_marker", MULTI_ENERGY_AIF_CAPABILITY, "argument", "MSDIALCUI.exe"),
            (found["available"], found["probe"], found["capability"], found["console_source"], found["console_assembly"]),
        )
        self.assertEqual(64, len(found["assembly_sha256"]))
        self.assertTrue(multi_energy_aif_ready(found))

    def test_a_console_without_the_marker_or_without_a_file_is_no_capability(self) -> None:
        for path, probe in (
            (self.without_825, "marker_absent"),
            (self.root / "nowhere" / "MSDIALCUI.exe", "console_missing"),
            (None, "no_console_configured"),
        ):
            with self.subTest(probe=probe):
                found = multi_energy_aif_console(path)
                self.assertEqual((False, probe), (found["available"], found["probe"]))
                self.assertFalse(multi_energy_aif_ready(found))
        self.assertFalse(multi_energy_aif_ready(None))
        self.assertFalse(multi_energy_aif_ready({"capability": "other", "available": True}))

    def test_every_marker_is_required(self) -> None:
        self.assertIn(PRE_825_MARKER, MULTI_ENERGY_AIF_CONSOLE_MARKERS)
        self.assertGreater(len(MULTI_ENERGY_AIF_CONSOLE_MARKERS), 1)
        for index, marker in enumerate(MULTI_ENERGY_AIF_CONSOLE_MARKERS):
            with self.subTest(only=marker):
                found = multi_energy_aif_console(_console(self.root / f"only-{index}", with_825=False, markers=(marker,)))
                self.assertEqual((False, "marker_incomplete"), (found["available"], found["probe"]))
                self.assertEqual(64, len(found["assembly_sha256"]))
                self.assertFalse(multi_energy_aif_ready(found))
        # Every marker in UTF-8 counts as well: the probe reads either encoding.
        utf8 = self.root / "utf8" / "MSDIALCUI.exe"
        utf8.parent.mkdir()
        utf8.write_bytes(b"MZ " + b" | ".join(marker.encode("utf-8") for marker in MULTI_ENERGY_AIF_CONSOLE_MARKERS))
        self.assertTrue(multi_energy_aif_console(utf8)["available"])

    def test_a_pre_825_aif_patch_build_is_no_capability(self) -> None:
        found = multi_energy_aif_console(self.pre_825)
        self.assertEqual((False, "marker_incomplete"), (found["available"], found["probe"]))
        with patch.object(workflow.subprocess, "run", side_effect=OSError("not started in a test")):
            self.assertNotIn(MULTI_ENERGY_AIF_CAPABILITY, console_capabilities(str(self.pre_825))["capabilities"])

    def test_a_net8_launcher_is_probed_through_its_assembly(self) -> None:
        folder = self.root / "net8"
        launcher = _console(folder, with_825=False)
        launcher.write_bytes(b"apphost launcher, not a managed image")
        (folder / "MSDIALCUI.runtimeconfig.json").write_text("{}", encoding="utf-8")
        _console(folder, with_825=True, name="MSDIALCUI.dll")

        found = multi_energy_aif_console(launcher)
        self.assertEqual((True, "MSDIALCUI.dll"), (found["available"], found["console_assembly"]))

    def test_console_capabilities_reports_it_beside_the_others(self) -> None:
        with patch.object(workflow.subprocess, "run", side_effect=OSError("not started in a test")):
            present = console_capabilities(str(self.with_825))["capabilities"]
            absent = console_capabilities(str(self.without_825))["capabilities"]

        self.assertIn(MULTI_ENERGY_AIF_CAPABILITY, present)
        self.assertNotIn(MULTI_ENERGY_AIF_CAPABILITY, absent)
        self.assertIn(workflow.LCMS_QA_CAPABILITY, absent)

    def test_the_configured_console_is_the_argument_then_the_setting_then_the_environment(self) -> None:
        self.assertEqual(("", ""), configured_console_path())
        with patch.dict(os.environ, {"MSDIAL_CONSOLE_PATH": str(self.without_825)}):
            self.assertEqual((str(self.without_825), "MSDIAL_CONSOLE_PATH"), configured_console_path())
            with patch.object(workflow, "load_user_settings", return_value={"console_path": str(self.with_825)}):
                self.assertEqual((str(self.with_825), "saved setting"), configured_console_path())
                self.assertEqual(("x.exe", "argument"), configured_console_path("x.exe"))
                self.assertTrue(multi_energy_aif_console()["available"])


class DispositionTests(_NoConfiguredConsole):
    MULTI = [_header("a.mzML", "AIF", targets=[], energies=[10.0, 20.0]),
             _header("b.mzML", "AIF", targets=[], energies=[10.0, 20.04])]

    def decide(self, records, console):
        return decide_disposition(_manifest(records), multi_energy_aif_console=console)

    def test_multi_energy_aif_runs_as_aif_with_825_and_is_held_without(self) -> None:
        ran = self.decide(self.MULTI, multi_energy_aif_console(self.with_825))

        self.assertEqual(("run", "AIF"), (ran["disposition"], ran["console_acquisition_type"]))
        self.assertEqual({"collision_energies": [10.0, 20.0], "rule": AIF_MULTI_CE_RULE}, ran["aif_multi_ce_run"])
        self.assertTrue(ran["multi_energy_aif_console"]["available"])
        self.assertEqual(
            {("AIF", AIF_MULTI_CE_BASIS)},
            {(item["console_acquisition_type"], item["basis"]) for item in ran["assignments"].values()},
        )
        self.assertNotIn("hold", ran)
        self.assertNotIn("aif_run_as_swath", ran)
        self.assertTrue(any(AIF_MULTI_CE_RULE in line for line in ran["detail"]), ran["detail"])

        for console in (
            multi_energy_aif_console(self.without_825),
            multi_energy_aif_console(self.pre_825),
            multi_energy_aif_console(None),
            None,
        ):
            with self.subTest(console=(console or {}).get("probe")):
                held = self.decide(self.MULTI, console)
                self.assertEqual(("skip", [AIF_MULTI_CE_HOLD], True), (held["disposition"], held["reasons"], held["hold"]))
                self.assertEqual({}, held["assignments"])
                self.assertNotIn("aif_multi_ce_run", held)
                self.assertEqual([10.0, 20.0], held["aif_collision_energies"])
                if console is None:
                    # As before 0.5.34: no probe, nothing recorded about one.
                    self.assertNotIn("multi_energy_aif_console", held)
                else:
                    self.assertEqual(console["probe"], held["multi_energy_aif_console"]["probe"])

    def test_single_energy_aif_runs_as_swath_whatever_the_console(self) -> None:
        records = [_header("a.mzML", "AIF", targets=[], energies=[35.0])]
        for console in (multi_energy_aif_console(self.with_825), multi_energy_aif_console(self.without_825), None):
            with self.subTest(console=(console or {}).get("probe")):
                disposition = self.decide(records, console)
                self.assertEqual(("run", "SWATH"), (disposition["disposition"], disposition["console_acquisition_type"]))
                self.assertEqual({"collision_energies": [35.0], "rule": AIF_AS_SWATH_RULE}, disposition["aif_run_as_swath"])
                self.assertNotIn("aif_multi_ce_run", disposition)

    def test_an_unrecorded_energy_is_held_with_825_too(self) -> None:
        for records in (
            [_header("a.mzML", "AIF", targets=[], energies=[])],
            # Two energies over the unit, and an input that records none: held for the unrecorded one only.
            [*self.MULTI, _header("c.mzML", "AIF", targets=[], energies=[])],
        ):
            with self.subTest(files=len(records)):
                held = self.decide(records, multi_energy_aif_console(self.with_825))
                self.assertEqual(
                    ("skip", [AIF_CE_UNRECORDED_HOLD], True), (held["disposition"], held["reasons"], held["hold"])
                )
                self.assertEqual({}, held["assignments"])
                self.assertTrue(any("stops on an AIF file" in line for line in held["detail"]), held["detail"])

    DIFFERING = {
        "one energy each, not the same": (
            [_header("a.mzML", "AIF", targets=[], energies=[10.0]),
             _header("b.mzML", "AIF", targets=[], energies=[40.0])],
            {"a.mzML": [10.0], "b.mzML": [40.0]},
        ),
        "different sets": (
            [_header("a.mzML", "AIF", targets=[], energies=[10.0, 20.0]),
             _header("b.mzML", "AIF", targets=[], energies=[10.0, 40.0])],
            {"a.mzML": [10.0, 20.0], "b.mzML": [10.0, 40.0]},
        ),
        # The edge the decision names: some inputs with one energy, others with several. Multi-energy AIF.
        "a multi-energy file beside a single-energy one": (
            [_header("a.mzML", "AIF", targets=[], energies=[10.0, 20.0]),
             _header("b.mzML", "AIF", targets=[], energies=[20.0]),
             _header("c.mzML", "AIF", targets=[], energies=[20.04])],
            {"a.mzML": [10.0, 20.0], "b.mzML": [20.0], "c.mzML": [20.0]},
        ),
    }

    def test_energy_sets_that_differ_between_inputs_run_as_aif_with_825_on_record(self) -> None:
        # Answer 6 of 2026-10-08, "run as is": #825 processes each file with its own representative energy.
        for name, (records, by_input) in self.DIFFERING.items():
            with self.subTest(case=name):
                listed = sorted({value for values in by_input.values() for value in values})
                ran = self.decide(records, multi_energy_aif_console(self.with_825))
                self.assertEqual(("run", "AIF"), (ran["disposition"], ran["console_acquisition_type"]))
                self.assertEqual([], ran["reasons"])
                self.assertNotIn("hold", ran)
                self.assertNotIn(AIF_CE_DIFFERS_HOLD, ran["reasons"])
                self.assertIn(AIF_CE_SETS_DIFFER_RECORDED, ran["warnings"])
                sets = {}
                for values in by_input.values():
                    sets[tuple(values)] = sets.get(tuple(values), 0) + 1
                self.assertEqual(
                    {
                        "collision_energies": listed,
                        "rule": AIF_MULTI_CE_RULE,
                        "energy_sets_differ": True,
                        "collision_energy_sets": [
                            {"collision_energies": list(values), "file_count": count}
                            for values, count in sorted(sets.items())
                        ],
                    },
                    ran["aif_multi_ce_run"],
                )
                self.assertEqual(by_input, ran["aif_collision_energies_by_input"])
                self.assertEqual(
                    {("AIF", AIF_MULTI_CE_BASIS)},
                    {(item["console_acquisition_type"], item["basis"]) for item in ran["assignments"].values()},
                )
                self.assertEqual(len(records), len(ran["assignments"]))
                self.assertTrue(ran["multi_energy_aif_console"]["available"])
                self.assertNotIn("aif_run_as_swath", ran)
                self.assertTrue(
                    any("own per-file representative collision energy" in line
                        and "can differ between files" in line and AIF_CE_SETS_DIFFER_RECORDED in line
                        for line in ran["detail"]),
                    ran["detail"],
                )

                # Without #825 the hold is exactly as before: awaiting the Console, nothing per input.
                for console in (multi_energy_aif_console(self.without_825), multi_energy_aif_console(self.pre_825), None):
                    before = self.decide(records, console)
                    self.assertEqual(("skip", [AIF_MULTI_CE_HOLD], True),
                                     (before["disposition"], before["reasons"], before["hold"]))
                    self.assertEqual({}, before["assignments"])
                    self.assertNotIn("aif_collision_energies_by_input", before)
                    self.assertNotIn(AIF_CE_SETS_DIFFER_RECORDED, before["warnings"])
                    self.assertEqual(listed, before["aif_collision_energies"])

    def test_one_shared_energy_runs_as_swath_and_one_different_energy_does_not(self) -> None:
        shared = [_header("a.mzML", "AIF", targets=[], energies=[20.0]),
                  _header("b.mzML", "AIF", targets=[], energies=[20.04])]
        swath = self.decide(shared, multi_energy_aif_console(self.with_825))
        self.assertEqual(("run", "SWATH"), (swath["disposition"], swath["console_acquisition_type"]))
        self.assertNotIn(AIF_CE_SETS_DIFFER_RECORDED, swath["warnings"])
        self.assertNotIn("aif_collision_energies_by_input", swath)

    def test_an_unrecorded_energy_holds_a_unit_whose_other_inputs_differ(self) -> None:
        # Answer 3 of 2026-10-08: an input with no recorded energy holds the unit, raw data kept.
        records = [_header("a.mzML", "AIF", targets=[], energies=[10.0]),
                   _header("b.mzML", "AIF", targets=[], energies=[40.0]),
                   _header("c.mzML", "AIF", targets=[], energies=[])]
        held = self.decide(records, multi_energy_aif_console(self.with_825))
        self.assertEqual(("skip", [AIF_CE_UNRECORDED_HOLD], True), (held["disposition"], held["reasons"], held["hold"]))
        self.assertEqual({}, held["assignments"])
        self.assertNotIn("aif_multi_ce_run", held)
        self.assertNotIn(AIF_CE_SETS_DIFFER_RECORDED, held["warnings"])

    def test_the_same_energies_in_every_input_run_as_aif_whatever_their_order(self) -> None:
        records = [_header("a.mzML", "AIF", targets=[], energies=[20.0, 10.0]),
                   _header("b.mzML", "AIF", targets=[], energies=[10.0, 20.0, 20.0]),
                   _header("c.mzML", "AIF", targets=[], energies=[10.04, 19.96])]
        ran = self.decide(records, multi_energy_aif_console(self.with_825))
        self.assertEqual(("run", "AIF"), (ran["disposition"], ran["console_acquisition_type"]))
        self.assertEqual({"collision_energies": [10.0, 20.0], "rule": AIF_MULTI_CE_RULE}, ran["aif_multi_ce_run"])
        self.assertNotIn("aif_collision_energies_by_input", ran)
        self.assertNotIn(AIF_CE_SETS_DIFFER_RECORDED, ran["warnings"])
        self.assertTrue(any("Each of the unit's 3 AIF input(s) records the same 2" in line for line in ran["detail"]),
                        ran["detail"])

    def test_dda_and_swath_units_carry_no_probe(self) -> None:
        console = multi_energy_aif_console(self.with_825)
        for method in ("DDA", "DIA"):
            disposition = self.decide([_header("a.mzML", method, energies=[10.0, 20.0])], console)
            self.assertNotIn("multi_energy_aif_console", disposition)
            self.assertNotIn("aif_multi_ce_run", disposition)


class CampaignTests(_NoConfiguredConsole):
    VERDICTS = {name: {"method": "AIF", "targets": [], "energies": [10.0, 20.0]} for name in ("a.mzML", "b.mzML")}

    def campaign_unit(self) -> tuple[Path, list[Path]]:
        manifest, _stub, files = _unit(
            self.root / "unit", list(self.VERDICTS), acquisition="AIF",
            extra={"campaign_authorizations": [dict(_APPROVAL)]},
        )
        return manifest, files

    def preflight_unit(self, manifest: Path, **options) -> dict:
        if not hasattr(self, "extractor"):
            self.extractor = _PinnedExtractor.make(self.root / "build")
        return self.preflight(manifest, self.extractor, _Extractor(self.VERDICTS), **options)

    def gate(self, manifest: Path, files: list[Path], kind: str, console: Path) -> dict:
        return evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": str(manifest.parent.parent / "output"),
                "ion_mode": "Negative",
                "console_path": str(console),
                "files": [{"file_path": str(path), "acquisition_type": kind} for path in files],
            }
        )

    def assert_runs_as_aif(self, manifest: Path) -> dict:
        recorded = read_manifest(manifest)
        self.assertEqual(("preflight_passed", True), (recorded["status"], recorded["execution_allowed"]))
        self.assertEqual("AIF", recorded["project"]["acquisition_mode"])
        disposition = recorded["campaign_disposition"]
        self.assertEqual(("run", True), (disposition["disposition"], disposition["applied"]))
        self.assertEqual(AIF_MULTI_CE_RULE, disposition["aif_multi_ce_run"]["rule"])
        self.assertFalse(held_by_disposition(recorded))
        self.assertEqual(
            {("AIF", AIF_MULTI_CE_BASIS, "AIF")},
            {(entry["console_acquisition_type"], entry["console_acquisition_basis"],
              entry["header_console_acquisition_type"])
             for entry in recorded["raw_metadata_preflight"]["summary"]["per_file"]},
        )
        self.assertTrue(any("MsdialWorkbench#825" in line for line in recorded["project"]["evidence"]))
        return recorded

    def test_a_held_unit_is_released_by_a_recheck_with_the_825_console(self) -> None:
        manifest, files = self.campaign_unit()
        first = self.preflight_unit(manifest, console_path=self.without_825)

        held = read_manifest(manifest)
        self.assertEqual("skipped_by_preflight", held["status"])
        self.assertEqual([AIF_MULTI_CE_HOLD], first["campaign_disposition"]["reasons"])
        self.assertEqual("marker_absent", held["campaign_disposition"]["multi_energy_aif_console"]["probe"])
        self.assertTrue(held_by_disposition(held))

        # The operator's recheck: the same preflight, now with the #825 Console configured.
        with patch.object(workflow, "load_user_settings", return_value={"console_path": str(self.with_825)}):
            self.preflight_unit(manifest)
        recorded = self.assert_runs_as_aif(manifest)
        probe = recorded["campaign_disposition"]["multi_energy_aif_console"]
        self.assertEqual(("saved setting", True), (probe["console_source"], probe["available"]))

        self.assertTrue(self.gate(manifest, files, "AIF", self.with_825)["allowed"])
        self.assertFalse(self.gate(manifest, files, "SWATH", self.with_825)["allowed"])
        for console in (self.without_825, self.pre_825):
            with self.subTest(console=console.parent.name):
                refused = self.gate(manifest, files, "AIF", console)
                self.assertFalse(refused["allowed"])
                self.assertTrue(
                    any("multi-energy AIF" in item and "MsdialWorkbench#825" in item for item in refused["blockers"]),
                    refused["blockers"],
                )

    def test_classify_releases_a_held_unit_from_its_recorded_headers(self) -> None:
        manifest, _files = self.campaign_unit()
        self.preflight_unit(manifest)
        self.assertTrue(held_by_disposition(read_manifest(manifest)))
        self.assertEqual("no_console_configured",
                         read_manifest(manifest)["campaign_disposition"]["multi_energy_aif_console"]["probe"])

        for console in (self.without_825, self.pre_825):
            with self.subTest(console=console.parent.name):
                again = classify_preflight(manifest, console_path=console)
                self.assertEqual(("skip", [AIF_MULTI_CE_HOLD]), (again["disposition"], again["reasons"]))
                self.assertTrue(held_by_disposition(read_manifest(manifest)))

        released = classify_preflight(manifest, console_path=self.with_825)
        self.assertEqual(("run", "AIF"), (released["disposition"], released["console_acquisition_type"]))
        self.assert_runs_as_aif(manifest)

    DIFFERING = {"a.mzML": {"method": "AIF", "targets": [], "energies": [10.0, 20.0]},
                 "b.mzML": {"method": "AIF", "targets": [], "energies": [40.0]}}

    def test_a_unit_whose_inputs_differ_runs_as_aif_on_record(self) -> None:
        self.VERDICTS = self.DIFFERING
        manifest, files = self.campaign_unit()
        first = self.preflight_unit(manifest, console_path=self.with_825)
        self.assertEqual(("run", []), (first["campaign_disposition"]["disposition"], first["campaign_disposition"]["reasons"]))
        recorded = self.assert_runs_as_aif(manifest)
        disposition = recorded["campaign_disposition"]
        self.assertIn(AIF_CE_SETS_DIFFER_RECORDED, disposition["warnings"])
        self.assertTrue(disposition["aif_multi_ce_run"]["energy_sets_differ"])
        self.assertEqual({"a.mzML": [10.0, 20.0], "b.mzML": [40.0]}, disposition["aif_collision_energies_by_input"])
        # Provenance: the project evidence says the sets differ and what that means.
        self.assertTrue(
            any("2 different sets of MS2 collision energies" in line and "per-file representative" in line
                for line in recorded["project"]["evidence"]),
            recorded["project"]["evidence"],
        )
        # The per-file records keep each input's own energies.
        self.assertEqual(
            {"a.mzML": [10.0, 20.0], "b.mzML": [40.0]},
            {Path(entry["file"]).name: entry["ms2_collision_energies"]
             for entry in recorded["raw_metadata_preflight"]["summary"]["per_file"]},
        )

        self.assertTrue(self.gate(manifest, files, "AIF", self.with_825)["allowed"])
        refused = self.gate(manifest, files, "AIF", self.without_825)
        self.assertFalse(refused["allowed"])
        self.assertTrue(any("MsdialWorkbench#825" in item for item in refused["blockers"]), refused["blockers"])

        # A per-file record that no longer carries its own recorded set is refused.
        def other_energy(current: dict) -> None:
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                if Path(entry["file"]).name == "b.mzML":
                    entry["ms2_collision_energies"] = [10.0]

        update_manifest(manifest, other_energy)
        refused = self.gate(manifest, files, "AIF", self.with_825)
        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("aif_collision_energies_by_input" in item and "1 input files record other energies" in item
                for item in refused["blockers"]),
            refused["blockers"],
        )

    def test_a_unit_held_for_differing_energies_by_0534_is_released_by_a_recheck(self) -> None:
        self.VERDICTS = self.DIFFERING
        manifest, files = self.campaign_unit()
        self.preflight_unit(manifest, console_path=self.with_825)

        def as_0534_held_it(current: dict) -> None:
            # The disposition 0.5.34-0.5.35 recorded for these inputs with a #825 Console.
            disposition = current["campaign_disposition"]
            for key in ("aif_multi_ce_run", "console_acquisition_type", "ion_mode"):
                disposition.pop(key, None)
            disposition.update(
                disposition="skip", hold=True, reasons=[AIF_CE_DIFFERS_HOLD], aif_collision_energies=[10.0, 20.0, 40.0],
                warnings=[item for item in disposition["warnings"] if item != AIF_CE_SETS_DIFFER_RECORDED],
                assignments={},
            )
            current["status"] = "skipped_by_preflight"
            current["execution_allowed"] = False

        update_manifest(manifest, as_0534_held_it)
        held = read_manifest(manifest)
        self.assertTrue(held_by_disposition(held))
        self.assertFalse(self.gate(manifest, files, "AIF", self.with_825)["allowed"])

        # The operator's recheck needs no OK, and decides it again: it runs.
        released = classify_preflight(manifest, console_path=self.with_825)
        self.assertEqual(("run", "AIF", []), (released["disposition"], released["console_acquisition_type"],
                                              released["reasons"]))
        recorded = self.assert_runs_as_aif(manifest)
        self.assertIn(AIF_CE_SETS_DIFFER_RECORDED, recorded["campaign_disposition"]["warnings"])
        self.assertTrue(self.gate(manifest, files, "AIF", self.with_825)["allowed"])

        # Without #825 the same unit is held as it was before 0.5.34.
        before = classify_preflight(manifest, console_path=self.without_825)
        self.assertEqual(("skip", [AIF_MULTI_CE_HOLD]), (before["disposition"], before["reasons"]))

    def test_the_preflight_tool_reports_the_differing_sets(self) -> None:
        from msdial_app import mcp_server

        self.VERDICTS = self.DIFFERING
        manifest, _files = self.campaign_unit()
        self.extractor = _PinnedExtractor.make(self.root / "build")

        def no_backend(*_args, **_kwargs):
            raise AssertionError("the preflight tool needs no backend")

        with patch.object(mcp_server, "_request_json", side_effect=no_backend), patch.object(
            mcp_server, "ROOT", self.root / "app"
        ), patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=_Extractor(self.VERDICTS)):
            result = mcp_server.msdial_repository_raw_metadata_preflight(
                extractor_path=str(self.extractor), manifest_path=str(manifest), console_path=str(self.with_825)
            )

        self.assertTrue(result["completed"], result)
        disposition = result["campaign_disposition"]
        self.assertEqual(("run", "AIF"), (disposition["disposition"], disposition["console_acquisition_type"]))
        self.assertIn(AIF_CE_SETS_DIFFER_RECORDED, disposition["warnings"])
        self.assertTrue(disposition["aif_multi_ce_run"]["energy_sets_differ"])
        self.assertEqual(
            [{"collision_energies": [10.0, 20.0], "file_count": 1}, {"collision_energies": [40.0], "file_count": 1}],
            disposition["aif_collision_energy_sets"],
        )

    def test_a_per_file_record_whose_energies_are_not_the_recorded_ones_is_refused(self) -> None:
        manifest, files = self.campaign_unit()
        self.preflight_unit(manifest, console_path=self.with_825)
        self.assertTrue(self.gate(manifest, files, "AIF", self.with_825)["allowed"])

        def one_energy(current: dict) -> None:
            current["raw_metadata_preflight"]["summary"]["per_file"][1]["ms2_collision_energies"] = [40.0]

        # As a disposition from before the per-file condition would have it: pooled energies, one file at one.
        update_manifest(manifest, one_energy)
        refused = self.gate(manifest, files, "AIF", self.with_825)
        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("never across files" in item and "1 input files record other energies" in item
                for item in refused["blockers"]),
            refused["blockers"],
        )

    def test_a_basis_claimed_without_the_record_is_refused(self) -> None:
        manifest, files = self.campaign_unit()
        self.preflight_unit(manifest, console_path=self.with_825)
        update_manifest(manifest, lambda current: current["campaign_disposition"].pop("aif_multi_ce_run"))

        refused = self.gate(manifest, files, "AIF", self.with_825)
        self.assertFalse(refused["allowed"])
        self.assertTrue(any("records no aif_multi_ce_run" in item for item in refused["blockers"]), refused["blockers"])

    def test_the_analysis_csv_says_aif_and_the_methods_state_the_energies(self) -> None:
        manifest, files = self.campaign_unit()
        self.preflight_unit(manifest, console_path=self.with_825)

        def with_lineage(current: dict) -> None:
            current["input_lineage"] = {
                "schema": "msdial-input-lineage.v1",
                "rows": [
                    {"path": str(path), "kind": "file", "sample_id": path.stem, "file_name": "", "source": {},
                     "checksums": {}}
                    for path in files
                ],
            }

        update_manifest(manifest, with_lineage)
        built = build_repository_analysis_rows(read_manifest(manifest))
        self.assertEqual([], built["failures"])
        self.assertEqual(["AIF", "AIF"], [row["acquisition_type"] for row in built["rows"]])
        self.assertEqual({"collision_energies": [10.0, 20.0], "rule": AIF_MULTI_CE_RULE}, built["aif_multi_ce_run"])
        csv_path = write_analysis_csv(built, self.root / "analysis_files.csv")
        with csv_path.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.reader(handle))
        # The Console's fifth column is the acquisition type; the energies come from the raw data, not the CSV.
        self.assertEqual(["AIF", "AIF"], [row[4] for row in rows[1:]])

        state = {
            "repository_run_manifest": str(manifest),
            "project_type": "lcms",
            "files": [{"file_path": str(path), "file_name": path.stem, "acquisition_type": "AIF"} for path in files],
        }
        evidence = multi_energy_aif_evidence(state)
        self.assertEqual(("10, 20", 2, AIF_MULTI_CE_RULE), (evidence["collision_energies_ev"], evidence["aif_files"],
                                                          evidence["rule"]))
        text = _methods_text({**state, "multi_energy_aif_evidence": evidence}, None, {"checks": []}, "0.5.34", "5.5")
        self.assertIn("collision energies of 10, 20 eV", text)
        self.assertIn("deconvoluted separately at each collision energy", text)
        self.assertIn("MS/MS reference-spectrum match", text)
        self.assertIn("most product ions", text)
        self.assertNotIn(str(self.root), text)
        rows = supplementary_rows({**state, "multi_energy_aif_evidence": evidence}, None, {"checks": []},
                                  app_version="0.5.34", console_version="5.5")
        self.assertIn(("Multi-energy AIF", "collision_energies_ev", "10, 20"),
                      [(row["Record"], row["Parameter"], row["Value"]) for row in rows])

        # A SWATH run of a single-energy unit, or any run without the record, says nothing of the kind.
        self.assertEqual({}, multi_energy_aif_evidence({**state, "files": [{**item, "acquisition_type": "SWATH"}
                                                                           for item in state["files"]]}))
        update_manifest(manifest, lambda current: current["campaign_disposition"].pop("aif_multi_ce_run"))
        self.assertEqual({}, multi_energy_aif_evidence(state))
        self.assertNotIn("collision energies of", _methods_text(state, None, {"checks": []}, "0.5.34", "5.5"))
        # The same energies in every input: no per-file sentence and no set rows.
        self.assertNotIn("energy_sets_differ", evidence)

    def test_the_methods_say_each_file_keeps_its_own_representative_energy_when_the_sets_differ(self) -> None:
        self.VERDICTS = self.DIFFERING
        manifest, files = self.campaign_unit()
        self.preflight_unit(manifest, console_path=self.with_825)
        state = {
            "repository_run_manifest": str(manifest),
            "project_type": "lcms",
            "files": [{"file_path": str(path), "file_name": path.stem, "acquisition_type": "AIF"} for path in files],
        }
        evidence = multi_energy_aif_evidence(state)
        self.assertEqual(
            ("10, 20, 40", True, "10, 20 eV in 1 file; 40 eV in 1 file"),
            (evidence["collision_energies_ev"], evidence["energy_sets_differ"], evidence["collision_energy_sets_ev"]),
        )
        text = _methods_text({**state, "multi_energy_aif_evidence": evidence}, None, {"checks": []}, "0.5.36", "5.5")
        self.assertIn("collision energies of 10, 20, 40 eV", text)
        self.assertIn(
            "The input files recorded different sets of collision energies (10, 20 eV in 1 file; 40 eV in 1 file), "
            "and each file was processed with its own per-file representative collision energy, so representative "
            "energies could differ between files.",
            text,
        )
        self.assertNotIn("a.mzML", text)
        self.assertNotIn(str(self.root), text)
        rows = [(row["Record"], row["Parameter"], row["Value"])
                for row in supplementary_rows({**state, "multi_energy_aif_evidence": evidence}, None, {"checks": []},
                                              app_version="0.5.36", console_version="5.5")]
        self.assertIn(("Multi-energy AIF", "energy_sets_differ", "TRUE"), rows)
        self.assertIn(("Multi-energy AIF", "collision_energy_sets_ev", "10, 20 eV in 1 file; 40 eV in 1 file"), rows)

        # The analysis-row record carries the sets as well.
        def with_lineage(current: dict) -> None:
            current["input_lineage"] = {
                "schema": "msdial-input-lineage.v1",
                "rows": [
                    {"path": str(path), "kind": "file", "sample_id": path.stem, "file_name": "", "source": {},
                     "checksums": {}}
                    for path in files
                ],
            }

        update_manifest(manifest, with_lineage)
        built = build_repository_analysis_rows(read_manifest(manifest))
        self.assertEqual([], built["failures"])
        self.assertEqual(["AIF", "AIF"], [row["acquisition_type"] for row in built["rows"]])
        self.assertTrue(built["aif_multi_ce_run"]["energy_sets_differ"])
        self.assertEqual(2, len(built["aif_multi_ce_run"]["collision_energy_sets"]))



# ---- inputs of one name in two folders (user decision, 2026-10-08, second round, answer 2) ------------------


class _ByFolderExtractor(_Extractor):
    """_Extractor whose verdicts are keyed by an input's folder and name ('POS/QC_01.mzML'), so two inputs of one
    basename in two folders can record different energies."""

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
        output = Path(command[command.index("--output") + 1])
        records = [_header(path, **self.verdicts.get("/".join(Path(path).parts[-2:]), {})) for path in inputs]
        output.write_text(json.dumps(records[0] if len(records) == 1 else records), encoding="utf-8")
        return CompletedProcess(command, 0, stdout=str(output), stderr="")


class InputsOfOneNameInTwoFoldersTests(_NoConfiguredConsole):
    """Each input's energy set is keyed by its path under the data root, never its basename: POS/QC_01.mzML and
    NEG/QC_01.mzML keep a set each (aif_collision_energies_by_input, AIF_CE_BY_INPUT_KEY)."""

    DATA = Path("C:/workspace/MTBLS-X/raw/data") if os.name == "nt" else Path("/workspace/MTBLS-X/raw/data")

    def decide(self, energies: dict[str, list[float]]) -> dict:
        manifest = _manifest([_header(str(self.DATA / name), "AIF", targets=[], energies=values)
                              for name, values in energies.items()])
        manifest["input_directory"] = str(self.DATA)
        return decide_disposition(manifest, multi_energy_aif_console=multi_energy_aif_console(self.with_825))

    def test_the_key_is_the_path_under_the_data_root(self) -> None:
        self.assertEqual("POS/QC_01.mzML", aif_input_key(self.DATA / "POS" / "QC_01.mzML", self.DATA))
        self.assertEqual("../converted/POS/QC_01.mzML",
                         aif_input_key(self.DATA.parent / "converted" / "POS" / "QC_01.mzML", self.DATA))
        # No data root recorded: the path as the record gives it.
        self.assertEqual("POS/QC_01.mzML", aif_input_key("POS\\QC_01.mzML", None))
        self.assertEqual("a.mzML", aif_input_key("a.mzML", self.DATA))

    def test_differing_sets_of_one_basename_run_as_aif_each_on_record(self) -> None:
        ran = self.decide({"POS/QC_01.mzML": [10.0, 20.0], "NEG/QC_01.mzML": [40.0]})

        self.assertEqual(("run", "AIF", []), (ran["disposition"], ran["console_acquisition_type"], ran["reasons"]))
        self.assertIn(AIF_CE_SETS_DIFFER_RECORDED, ran["warnings"])
        self.assertEqual({"NEG/QC_01.mzML": [40.0], "POS/QC_01.mzML": [10.0, 20.0]},
                         ran["aif_collision_energies_by_input"])
        self.assertEqual(AIF_CE_BY_INPUT_KEY, ran["aif_collision_energies_by_input_key"])
        self.assertEqual({"NEG/QC_01.mzML": "QC_01.mzML", "POS/QC_01.mzML": "QC_01.mzML"},
                         ran["aif_collision_energies_input_names"])
        self.assertEqual(
            [{"collision_energies": [10.0, 20.0], "file_count": 1}, {"collision_energies": [40.0], "file_count": 1}],
            ran["aif_multi_ce_run"]["collision_energy_sets"],
        )
        self.assertEqual(2, len(ran["assignments"]))

    def test_identical_sets_of_one_basename_run_as_aif_with_nothing_per_input(self) -> None:
        ran = self.decide({"POS/QC_01.mzML": [10.0, 20.0], "NEG/QC_01.mzML": [20.0, 10.0]})

        self.assertEqual(("run", "AIF"), (ran["disposition"], ran["console_acquisition_type"]))
        self.assertEqual({"collision_energies": [10.0, 20.0], "rule": AIF_MULTI_CE_RULE}, ran["aif_multi_ce_run"])
        self.assertNotIn("aif_collision_energies_by_input", ran)
        self.assertNotIn(AIF_CE_SETS_DIFFER_RECORDED, ran["warnings"])

    def test_one_of_them_unrecorded_holds_the_unit(self) -> None:
        held = self.decide({"POS/QC_01.mzML": [10.0, 20.0], "NEG/QC_01.mzML": []})

        self.assertEqual(("skip", [AIF_CE_UNRECORDED_HOLD], True), (held["disposition"], held["reasons"], held["hold"]))
        self.assertEqual({}, held["assignments"])
        self.assertNotIn("aif_collision_energies_by_input", held)

    def test_a_campaign_unit_runs_and_the_gate_holds_each_input_to_its_own_set(self) -> None:
        verdicts = {"POS/QC_01.mzML": {"method": "AIF", "targets": [], "energies": [10.0, 20.0]},
                    "NEG/QC_01.mzML": {"method": "AIF", "targets": [], "energies": [40.0]}}
        for folder in ("POS", "NEG"):
            (self.root / "unit" / "raw" / "data" / folder).mkdir(parents=True)
        manifest, _stub, files = _unit(
            self.root / "unit", list(verdicts), acquisition="AIF",
            extra={"campaign_authorizations": [dict(_APPROVAL)]},
        )
        extractor = _PinnedExtractor.make(self.root / "build")
        self.preflight(manifest, extractor, _ByFolderExtractor(verdicts), console_path=self.with_825)

        recorded = read_manifest(manifest)
        disposition = recorded["campaign_disposition"]
        self.assertEqual(("run", True, "AIF"), (disposition["disposition"], disposition["applied"],
                                                disposition["console_acquisition_type"]))
        self.assertEqual({"NEG/QC_01.mzML": [40.0], "POS/QC_01.mzML": [10.0, 20.0]},
                         disposition["aif_collision_energies_by_input"])

        def gate() -> dict:
            return evaluate_repository_execution_gate(
                {
                    "repository_run_manifest": str(manifest),
                    "output_root": str(manifest.parent.parent / "output"),
                    "ion_mode": "Negative",
                    "console_path": str(self.with_825),
                    "files": [{"file_path": str(path), "acquisition_type": "AIF"} for path in files],
                }
            )

        allowed = gate()
        self.assertTrue(allowed["allowed"], allowed["blockers"])

        # NEG/QC_01.mzML recording the set of POS/QC_01.mzML is refused: the basename alone does not answer.
        def neg_takes_pos_set(current: dict) -> None:
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                if Path(entry["file"]).parent.name == "NEG":
                    entry["ms2_collision_energies"] = [10.0, 20.0]

        update_manifest(manifest, neg_takes_pos_set)
        refused = gate()
        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("1 input files record other energies; the first is NEG/QC_01.mzML" in item for item in refused["blockers"]),
            refused["blockers"],
        )


if __name__ == "__main__":
    unittest.main()
