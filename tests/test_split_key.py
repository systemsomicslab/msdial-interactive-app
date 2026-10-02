"""One split key: acquisition mode, ion-mobility regime and polarity, with every differing part in the part id.

The split was keyed on the acquisition mode alone, and the part id was <unit>-<mode>. So a Bruker unit whose
BAF and TDF folders were both DDA was not split at all (or, keyed by container format as well, gave the
same id twice), a timsTOF part would have run LC-IM-MS data as LC-MS, and a unit whose files differed only in
polarity was refused ("Only a unit whose raw headers disagree about acquisition mode is split here").

Now the key has three parts. A part whose inputs carry ion mobility is written excluded, since LC-IM-MS is
outside the campaign's scope; a part split by polarity carries that polarity as its ion mode. And a part's
file list is its own folders' members, matched by path: by name alone, pos/S1.raw and neg/S1.raw are one
folder, and each polarity part listed the members of both.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from msdial_app.repository_reanalysis import (
    ION_MOBILITY_EXCLUSION,
    plan_acquisition_split,
    plan_split_parent_cleanup,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
    update_manifest,
)

MEMBERS = ("_FUNC001.DAT", "_FUNC001.IDX", "_extern.inf")


def _extractor(verdicts: dict[str, dict]):
    """A stand-in for the raw-metadata extractor: one record per --input, by the input's path under data."""

    def run(command, **_kwargs):
        inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
        records = []
        for path in inputs:
            verdict = verdicts[Path(path).parent.name + "/" + Path(path).name]
            acquisition = {
                "separation": {"value": "LiquidChromatography"},
                "method": {"value": verdict["mode"], "confidence": 0.9},
                "polarity": {"value": verdict.get("polarity", "Negative")},
                "msLevels": [1, 2],
            }
            if verdict.get("mobility") is not None:
                acquisition["hasIonMobility"] = {"value": verdict["mobility"]}
            records.append({"source": {"filePath": path, "fileName": Path(path).stem}, "acquisition": acquisition})
        Path(command[command.index("--output") + 1]).write_text(json.dumps(records), encoding="utf-8")
        return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

    return run


class _Unit:
    """A unit on disk: its inputs under raw/data/<folder>/, its manifest, and a stub extractor binary."""

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def unit(self, inputs: dict[str, str], *, ion_mode: str = "Negative", declared_inputs: bool = False) -> Path:
        """``inputs``: {"<folder>/<name>": kind}, kind one of mzml, baf, tdf, waters. One sample per input."""
        workspace = self.root / "unit"
        data = workspace / "raw" / "data"
        (workspace / "provenance").mkdir(parents=True)
        (workspace / "output").mkdir(parents=True)
        candidates, files, samples, declared = [], [], [], []
        for index, (relative, kind) in enumerate(sorted(inputs.items())):
            path = data / relative
            listed = f"FILES/{relative}"
            sample = f"{Path(relative).parent.name}_{Path(relative).stem}"
            if kind == "mzml":
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"<mzML/>")
                files.append({"name": listed, "size_bytes": 7, "url": "", "role": "raw"})
            else:
                path.mkdir(parents=True)
                members = {"baf": ("analysis.baf",), "tdf": ("analysis.tdf", "analysis.tdf_bin"), "waters": MEMBERS}[kind]
                for member in members:
                    (path / member).write_bytes(f"{relative}/{member}".encode("ascii"))
                    files.append(
                        {
                            "name": f"{listed}/{member}",
                            "size_bytes": 1,
                            "url": "",
                            "role": "vendor_folder_member",
                            "container": listed,
                            "sample_id": sample,
                        }
                    )
            candidates.append(str(path))
            samples.append({"sample_id": sample, "raw_file": listed, "values": {}})
            declared.append(
                {"path": listed, "kind": "file" if kind == "mzml" else "vendor_folder", "sample_id": sample}
            )
        project = {
            "repository": "metabobank",
            "accession": "MTBKS-SPLIT",
            "analysis_unit_id": "unit-x",
            "separation": "LC-MS",
            "acquisition_mode": "DDA",
            "ion_mode": ion_mode,
            "untargeted": True,
            "files": files,
            "total_download_bytes": len(files),
            "sample_count": len(samples),
            "sample_metadata": samples,
            "class_proposal": {
                "proposal_id": "p1",
                "status": "accepted",
                "assignments": [
                    {"sample_id": item["sample_id"], "class_label": "A" if index % 2 else "B"}
                    for index, item in enumerate(samples)
                ],
            },
        }
        if declared_inputs:
            project.update(analysis_inputs=declared, analysis_inputs_declared=True)
        manifest = workspace / "provenance" / "run-manifest.json"
        manifest.write_text(
            json.dumps(
                {
                    "status": "downloaded",
                    "workspace": str(workspace),
                    "raw_directory": str(workspace / "raw"),
                    "input_directory": str(data),
                    "output_directory": str(workspace / "output"),
                    "input_candidates": candidates,
                    "execution_allowed": False,
                    "raw_retention_policy": "delete_after_validated_output",
                    "project": project,
                }
            ),
            encoding="utf-8",
        )
        self.extractor = self.root / "RawMetadataConsoleApp.exe"
        self.extractor.write_bytes(b"stub")
        return manifest

    def preflight(self, manifest: Path, verdicts: dict[str, dict]) -> dict:
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=_extractor(verdicts)):
            return run_raw_metadata_preflight(manifest, self.extractor)


class BrukerBafAndTdf(_Unit, unittest.TestCase):
    INPUTS = {"d/S1.d": "baf", "d/S2.d": "baf", "d/T1.d": "tdf", "d/T2.d": "tdf"}
    # Every folder is DDA, and the TDF headers say nothing about mobility: the container format decides.
    VERDICTS = {name: {"mode": "DDA"} for name in INPUTS}

    def test_baf_and_tdf_split_into_two_parts_and_the_tdf_part_is_excluded(self) -> None:
        manifest = self.unit(self.INPUTS)
        self.preflight(manifest, self.VERDICTS)

        plan = plan_acquisition_split(manifest)
        result = split_unit_by_acquisition(manifest, confirmed=True)
        parts = {part["analysis_unit_id"]: read_manifest(part["manifest_path"]) for part in result["parts"]}
        parent = read_manifest(manifest)

        self.assertEqual([], plan["blockers"])
        self.assertEqual(["ion_mobility"], plan["split_key"]["by"])
        self.assertEqual({"unit-x-dda", "unit-x-dda-im"}, set(parts))
        lc, im = parts["unit-x-dda"], parts["unit-x-dda-im"]
        self.assertEqual({"S1.d", "S2.d"}, {Path(item).name for item in lc["input_candidates"]})
        self.assertEqual({"T1.d", "T2.d"}, {Path(item).name for item in im["input_candidates"]})
        self.assertEqual("split_from_parent", lc["status"])
        self.assertNotIn("split_exclusion", lc)
        # The timsTOF part never runs, and says why, in the words of the one disposition mapping.
        self.assertEqual("excluded_by_preflight", im["status"])
        self.assertFalse(im["execution_allowed"])
        self.assertEqual(ION_MOBILITY_EXCLUSION, im["split_exclusion"]["reason"])
        self.assertEqual("excluded", im["project"]["selection_status"])
        self.assertTrue(any("LC-IM-MS" in reason for reason in im["project"]["exclusion_reasons"]))
        self.assertEqual("exclude", im["campaign_disposition"]["disposition"])
        self.assertIn(ION_MOBILITY_EXCLUSION, im["campaign_disposition"]["reasons"])
        self.assertFalse(im["campaign_disposition"]["applied"], "outside a campaign it is advice")
        # Each part lists its own folders' members, and the parent stays the raw owner of both.
        self.assertEqual({"FILES/d/S1.d", "FILES/d/S2.d"}, {item["container"] for item in lc["project"]["files"]})
        self.assertEqual({"FILES/d/T1.d", "FILES/d/T2.d"}, {item["container"] for item in im["project"]["files"]})
        self.assertEqual("split_by_acquisition", parent["status"])
        self.assertEqual(["ion_mobility"], parent["split_key"]["by"])

    def test_an_excluded_part_stays_excluded_when_it_is_preflighted_on_its_own(self) -> None:
        manifest = self.unit(self.INPUTS)
        self.preflight(manifest, self.VERDICTS)
        result = split_unit_by_acquisition(manifest, confirmed=True)
        im = next(Path(part["manifest_path"]) for part in result["parts"] if part["ion_mobility"])

        after = self.preflight(im, self.VERDICTS)

        self.assertEqual("excluded_by_preflight", after["status"])
        self.assertFalse(after["execution_allowed"])
        self.assertEqual("excluded", after["project"]["selection_status"])
        self.assertEqual("exclude", after["campaign_disposition"]["disposition"])
        self.assertTrue(after["raw_metadata_preflight"]["summary"]["per_file"], "the reads are still recorded")

    def test_the_excluded_part_has_ended_for_its_parents_raw_release(self) -> None:
        manifest = self.unit(self.INPUTS)
        self.preflight(manifest, self.VERDICTS)
        split_unit_by_acquisition(manifest, confirmed=True)

        plan = plan_split_parent_cleanup(manifest)

        states = {part["analysis_unit_id"]: part["state"] for part in plan["parts"]}
        self.assertEqual({"unit-x-dda": "pending", "unit-x-dda-im": "excluded"}, states)

    def test_a_header_that_records_ion_mobility_decides_for_any_container(self) -> None:
        inputs = {"d/a.mzML": "mzml", "d/b.mzML": "mzml"}
        manifest = self.unit(inputs)
        self.preflight(manifest, {"d/a.mzML": {"mode": "DIA"}, "d/b.mzML": {"mode": "DIA", "mobility": True}})

        plan = plan_acquisition_split(manifest)

        self.assertEqual([], plan["blockers"])
        self.assertEqual(["unit-x-dia", "unit-x-dia-im"], [part["analysis_unit_id"] for part in plan["parts"]])
        self.assertEqual(ION_MOBILITY_EXCLUSION, plan["parts"][1]["excluded"]["reason"])

    def test_one_regime_and_one_mode_is_still_not_split(self) -> None:
        manifest = self.unit({"d/S1.d": "baf", "d/S2.d": "baf"})
        self.preflight(manifest, {"d/S1.d": {"mode": "DDA"}, "d/S2.d": {"mode": "DDA"}})

        result = split_unit_by_acquisition(manifest, confirmed=True)

        self.assertFalse(result["written"])
        self.assertTrue(any("Only a unit" in item for item in result["blockers"]), result["blockers"])


class Polarity(_Unit, unittest.TestCase):
    # Two folders of the same names, one per polarity, as a positive and a negative run are often deposited.
    INPUTS = {"pos/S1.raw": "waters", "pos/S2.raw": "waters", "neg/S1.raw": "waters", "neg/S2.raw": "waters"}

    def verdicts(self, modes: dict[str, str] | None = None) -> dict[str, dict]:
        return {
            name: {"mode": (modes or {}).get(name, "DDA"), "polarity": "Positive" if name.startswith("pos/") else "Negative"}
            for name in self.INPUTS
        }

    def test_polarity_split_ids_name_the_polarity(self) -> None:
        manifest = self.unit(self.INPUTS, ion_mode="Unknown", declared_inputs=True)
        self.preflight(manifest, self.verdicts())

        result = split_unit_by_acquisition(manifest, confirmed=True)
        parts = {part["analysis_unit_id"]: read_manifest(part["manifest_path"]) for part in result["parts"]}

        self.assertTrue(result["written"], result["blockers"])
        self.assertEqual(["polarity"], result["split_key"]["by"])
        self.assertEqual({"unit-x-dda-neg", "unit-x-dda-pos"}, set(parts))
        self.assertEqual("Negative", parts["unit-x-dda-neg"]["project"]["ion_mode"])
        self.assertEqual("Positive", parts["unit-x-dda-pos"]["project"]["ion_mode"])
        self.assertEqual("raw_header_split_key", parts["unit-x-dda-pos"]["split_from"]["split_by"])
        self.assertEqual(
            {"acquisition": "DDA", "ion_mobility": False, "polarity": "Positive"},
            parts["unit-x-dda-pos"]["split_from"]["split_key"],
        )

    def test_a_parts_files_are_its_own_folders_members_and_its_samples_its_own(self) -> None:
        manifest = self.unit(self.INPUTS, ion_mode="Unknown", declared_inputs=True)
        self.preflight(manifest, self.verdicts())

        result = split_unit_by_acquisition(manifest, confirmed=True)
        positive = read_manifest(next(part["manifest_path"] for part in result["parts"] if part["polarity"] == "Positive"))

        project = positive["project"]
        self.assertEqual({"FILES/pos/S1.raw", "FILES/pos/S2.raw"}, {item["container"] for item in project["files"]})
        self.assertEqual(2 * len(MEMBERS), len(project["files"]))
        self.assertEqual({"pos_S1", "pos_S2"}, {item["sample_id"] for item in project["sample_metadata"]})
        self.assertEqual({"pos_S1", "pos_S2"}, {item["sample_id"] for item in project["analysis_inputs"]})
        self.assertEqual(
            {"pos_S1", "pos_S2"}, {item["sample_id"] for item in project["class_proposal"]["assignments"]}
        )
        self.assertEqual([], result["unclaimed_samples"])

    def test_mixed_acquisition_and_polarity_name_both(self) -> None:
        manifest = self.unit(self.INPUTS, ion_mode="Unknown", declared_inputs=True)
        self.preflight(manifest, self.verdicts({"pos/S2.raw": "DIA", "neg/S2.raw": "DIA"}))

        plan = plan_acquisition_split(manifest)

        self.assertEqual([], plan["blockers"])
        self.assertEqual(["acquisition", "polarity"], plan["split_key"]["by"])
        self.assertEqual(
            ["unit-x-dda-neg", "unit-x-dda-pos", "unit-x-dia-neg", "unit-x-dia-pos"],
            [part["analysis_unit_id"] for part in plan["parts"]],
        )

    def test_an_input_with_no_single_polarity_blocks_a_polarity_split(self) -> None:
        manifest = self.unit(self.INPUTS, ion_mode="Unknown", declared_inputs=True)
        verdicts = self.verdicts()
        verdicts["neg/S2.raw"]["polarity"] = "PolaritySwitching"
        self.preflight(manifest, verdicts)

        result = split_unit_by_acquisition(manifest, confirmed=True)

        self.assertFalse(result["written"])
        self.assertTrue(any("record no single polarity" in item for item in result["blockers"]), result["blockers"])

    def test_a_campaign_disposition_split_by_polarity_alone_is_made(self) -> None:
        manifest = self.unit(self.INPUTS, ion_mode="Unknown", declared_inputs=True)
        self.preflight(manifest, self.verdicts())

        def applied(current: dict) -> None:
            groups = {}
            for path in current["input_candidates"]:
                polarity = "Positive" if Path(path).parent.name == "pos" else "Negative"
                groups.setdefault(polarity, []).append(path)
            current["campaign_disposition"] = {
                "disposition": "split",
                "applied": True,
                "excluded_inputs": [],
                "split_key": {
                    "by": ["polarity"],
                    "groups": [
                        {"console_acquisition_type": "DDA", "polarity": polarity, "inputs": sorted(paths)}
                        for polarity, paths in sorted(groups.items())
                    ],
                },
            }

        update_manifest(manifest, applied)
        plan = plan_acquisition_split(manifest)

        self.assertEqual([], plan["blockers"])
        self.assertEqual(["unit-x-dda-neg", "unit-x-dda-pos"], [part["analysis_unit_id"] for part in plan["parts"]])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
