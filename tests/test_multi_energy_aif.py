"""Multi-energy AIF with a Console that has MsdialWorkbench#825 (Interactive 0.5.34).

The hold of 2026-10-07 was "until the patched Console". Where the configured Console's assembly carries #825's
multi-energy AIF processing, a unit whose AIF inputs record more than one MS2 collision energy runs as AIF; without
it the hold stands. Single-energy AIF runs as SWATH and AIF with an unrecorded energy is held, whatever the Console.

Every Console here is a file holding (or not holding) the marker; no Console is started.
"""

import csv
import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from test_raw_metadata_preflight import _APPROVAL, _Extractor, _PinnedExtractor, _Scratch, _header, _manifest, _unit

from msdial_app import workflow
from msdial_app.materials_methods import _methods_text, multi_energy_aif_evidence, supplementary_rows
from msdial_app.raw_metadata_preflight import (
    AIF_AS_SWATH_RULE,
    AIF_CE_UNRECORDED_HOLD,
    AIF_MULTI_CE_BASIS,
    AIF_MULTI_CE_HOLD,
    AIF_MULTI_CE_RULE,
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


def _console(root: Path, *, with_825: bool, name: str = "MSDIALCUI.exe") -> Path:
    """A stand-in Console assembly: managed strings are UTF-16LE in the user-string heap, as in the real one."""
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    body = b"MZ\x90\x00 stand-in assembly " + "LC-MS quality-assurance matrix:".encode("utf-16-le")
    if with_825:
        body += MULTI_ENERGY_AIF_CONSOLE_MARKERS[0].encode("utf-16-le")
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

        for console in (multi_energy_aif_console(self.without_825), multi_energy_aif_console(None), None):
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
        refused = self.gate(manifest, files, "AIF", self.without_825)
        self.assertFalse(refused["allowed"])
        self.assertTrue(any("multi-energy AIF" in item and "MsdialWorkbench#825" in item for item in refused["blockers"]),
                        refused["blockers"])

    def test_classify_releases_a_held_unit_from_its_recorded_headers(self) -> None:
        manifest, _files = self.campaign_unit()
        self.preflight_unit(manifest)
        self.assertTrue(held_by_disposition(read_manifest(manifest)))
        self.assertEqual("no_console_configured",
                         read_manifest(manifest)["campaign_disposition"]["multi_energy_aif_console"]["probe"])

        again = classify_preflight(manifest, console_path=self.without_825)
        self.assertEqual(("skip", [AIF_MULTI_CE_HOLD]), (again["disposition"], again["reasons"]))

        released = classify_preflight(manifest, console_path=self.with_825)
        self.assertEqual(("run", "AIF"), (released["disposition"], released["console_acquisition_type"]))
        self.assert_runs_as_aif(manifest)

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


if __name__ == "__main__":
    unittest.main()
