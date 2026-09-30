"""One analysis input per vendor folder, and the analysis CSV built from the unit's own record.

The user decided on 2026-09-30 that a Waters .raw or an Agilent/Bruker .d folder is ONE data file, and
that a campaign unit's analysis CSV is generated without a person. MetaboBank MTBKS217 positive is the
shape exercised here: twelve folders named by twelve sample rows, whose files the repository lists one by
one. The Catalog (0.6.0) now hands over the twelve inputs and keeps the files as their members; what is
checked here is Interactive's side of that:

- the handoff mapper keeps the members as members and reads the declared inputs, and a handoff whose
  counts disagree with its own listing is a failure recorded for that unit, not an exception;
- a folder that did not arrive whole is no input (container completeness);
- a folder is found once, at its outermost root, and matched by its path, every declared one;
- the Console is trusted to read a folder by what its build record says it is, not by where it sits;
- the CSV has one row per input, each row's acquisition type the file's own, and a name the Console's
  parser cannot read back is given an ASCII-safe alias rather than written as it is.
"""

from __future__ import annotations

import codecs
import csv
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from collections import Counter
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

_CONFIG = tempfile.TemporaryDirectory()
with patch.dict(os.environ, {"LOCALAPPDATA": _CONFIG.name}):
    from msdial_app import mcp_server, workflow
    from msdial_app import repository_analysis_rows as rows_module
    from msdial_app.agent_workflow import build_guided_plan
    from msdial_app.repository_analysis_rows import (
        ALIAS_DIRECTORY,
        blocking_failures,
        build_repository_analysis_rows,
        create_console_aliases,
        order_rows,
        record_analysis_csv,
        write_analysis_csv,
    )
    from msdial_app.repository_reanalysis import (
        ANALYSIS_INPUT_ROLES,
        RepositoryFile,
        RepositoryProject,
        _filter_inputs_by_project_allowlist,
        _find_msdial_inputs,
        _tree_size,
        _write_json,
        create_download_lease,
        declared_analysis_inputs,
        declared_archive_containers,
        evaluate_repository_execution_gate,
        project_from_dict,
        read_manifest,
        run_raw_metadata_preflight,
        split_unit_by_acquisition,
        update_manifest,
        verify_container_completeness,
    )
    from msdial_app.run_finalisation import _directory_of

# MetaboBank MTBKS217 positive (5b635e6fc36ea3042e2e): the sample id, the folder it names, and what its
# sample row says about it. Sample x9, Blank x2, Standard x1.
MTBKS217 = [
    ("standard sample", "190827_025pp", "standard", ""),
    ("Blank_sample_1", "190827_026pp", "blank", ""),
    ("CSRSPlant_Arabi01", "190827_027pp", "sample", "Arabidopsis"),
    ("CSRSPlant_Arabi02", "190827_028pp", "sample", "Arabidopsis"),
    ("CSRSPlant_Arabi03", "190827_029pp", "sample", "Arabidopsis"),
    ("CSRSPlant_Ine01", "190827_030pp", "sample", "Rice"),
    ("CSRSPlant_Ine02", "190827_031pp", "sample", "Rice"),
    ("CSRSPlant_Ine03", "190827_032pp", "sample", "Rice"),
    ("CSRSPlant_Nasu01", "190827_033pp", "sample", "Eggplant"),
    ("CSRSPlant_Nasu02", "190827_034pp", "sample", "Eggplant"),
    ("CSRSPlant_Nasu03", "190827_035pp", "sample", "Eggplant"),
    ("Blank_sample_2", "z_011pp", "blank", ""),
]
# What a Waters folder holds; every _FUNC*.DAT used to be read as a .dat file to convert.
MEMBERS = ("_FUNC001.DAT", "_FUNC001.IDX", "_FUNC002.DAT", "_extern.inf")
BASE_URL = "https://example.org/MTBKS217/"


def _member_bytes(folder: str, member: str) -> bytes:
    return f"{folder}/{member}".encode("ascii")


def _payloads() -> dict[str, bytes]:
    return {
        f"{BASE_URL}raw/{folder}.raw/{member}": _member_bytes(folder, member)
        for _sample, folder, _kind, _plant in MTBKS217
        for member in MEMBERS
    }


def _handoff(**changes) -> dict:
    """A Catalog 0.6.0 handoff for MTBKS217 positive: 12 inputs, 48 members, 12 sample rows."""
    files = []
    inputs = []
    samples = []
    for sample, folder, kind, plant in MTBKS217:
        container = f"raw/{folder}.raw"
        listed = []
        for member in MEMBERS:
            data = _member_bytes(folder, member)
            listed.append(
                {
                    "path": f"{container}/{member}",
                    "download_url": f"{BASE_URL}{container}/{member}",
                    "size_bytes": len(data),
                    "checksum": hashlib.md5(data).hexdigest(),
                    "role": "vendor_folder_member",
                    "container": container,
                    "sample_id": sample,
                }
            )
        files.extend(listed)
        inputs.append(
            {
                "path": container,
                "kind": "vendor_folder",
                "suffix": ".raw",
                "format": "waters_raw",
                "member_count": len(listed),
                "size_bytes": sum(item["size_bytes"] for item in listed),
                "sample_id": sample,
            }
        )
        samples.append(
            {
                "sample_id": sample,
                "raw_file": container,
                "attributes": {"Sample type": kind, "Plant": plant or "none"},
            }
        )
    handoff = {
        "schema": "msdial-repository-reanalysis-handoff.v1",
        "repository": "metabobank",
        "accession": "MTBKS217",
        "analysis_unit_id": "5b635e6fc36ea3042e2e",
        "title": "MTBKS217",
        "technical_settings": {
            "separation": "LC-MS",
            "ion_mode": "Positive",
            "acquisition_mode": "DIA",
            "untargeted": True,
            "target_omics": "Metabolomics",
        },
        "files": files,
        "download_scope": {
            "kind": "unit_files",
            "file_count": len(files),
            "analysis_file_count": len(inputs),
            "bundle_bytes": sum(item["size_bytes"] for item in files),
        },
        "sample_count": len(samples),
        "analytical_sample_count": len(inputs),
        "analysis_input_model": "one-input-per-sample.v1",
        "analysis_inputs_declared": True,
        "analysis_input_count": len(inputs),
        "analysis_inputs": inputs,
        "analysis_input_issues": [],
        "split_hint": None,
        "sample_metadata": samples,
        "class_proposal": None,
        "blocking_reasons": ["class_proposal:missing"],
    }
    handoff.update(changes)
    return handoff


class _Client:
    """Stands in for the network: fixed bytes per URL."""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.payloads = payloads

    def download(self, url, destination, _maximum_bytes, progress_callback=None):
        data = self.payloads[url]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)
        return {
            "path": str(destination),
            "size_bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "md5": hashlib.md5(data).hexdigest(),
            "resumed_from_bytes": 0,
        }


def _leased_unit(root: Path, handoff: dict | None = None) -> Path:
    """Map the handoff and lease the unit into root, as the campaign would. Returns the manifest path."""
    project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff or _handoff())
    typed = project_from_dict(project)
    # The Class proposal is the Catalog's to ratify; it is not what is under test here.
    typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
    lease = create_download_lease(typed, root, 10_000_000, client=_Client(_payloads()))
    return Path(lease["manifest_path"])


def _set_preflight(manifest_path: Path, types: dict[str, str | None]) -> None:
    """Record a raw-metadata preflight: each input's console_acquisition_type by folder name (None: absent)."""

    def change(manifest: dict) -> None:
        per_file = []
        for path in manifest["input_candidates"]:
            record = {"file": path, "acquisition_mode": "DIA", "polarity": "Positive", "ms_levels": [1, 2]}
            value = types.get(Path(path).name, types.get("*"))
            if value is not None:
                record["console_acquisition_type"] = value
            per_file.append(record)
        manifest["raw_metadata_preflight"] = {"summary": {"per_file": per_file, "acquisition_mode": "DIA"}}

    update_manifest(manifest_path, change)


def _console_rows(csv_path: Path) -> list[list[str]]:
    """The CSV as the pinned Console's AnalysisFilesParser reads it: ASCII bytes, split on ',' only.

    The unit's own CSV carries a UTF-8 byte-order mark (repository_metadata writes it so); the copy the
    Console reads is written in ASCII by prepare_run. Either way every byte after the mark must be ASCII.
    """
    data = csv_path.read_bytes()
    if data.startswith(codecs.BOM_UTF8):
        data = data[len(codecs.BOM_UTF8):]
    return [line.split(",") for line in data.decode("ascii").splitlines() if line.strip()]


def _no_backend(*_args, **_kwargs):
    raise AssertionError("preparing a unit by its manifest must not need the backend")


class TheHandoffKeepsFoldersAsOneInputEach(unittest.TestCase):
    def test_a_folder_units_members_stay_members_and_its_inputs_are_read(self) -> None:
        project, workspace = mcp_server._project_from_analysis_unit_handoff(_handoff())

        self.assertNotEqual("excluded", project["selection_status"], project["exclusion_reasons"])
        self.assertEqual([], project["exclusion_reasons"])
        self.assertEqual(12, project["sample_count"])
        self.assertEqual(12, len(workspace["rows"]))
        self.assertEqual(12, len(project["analysis_inputs"]))
        self.assertTrue(project["analysis_inputs_declared"])
        self.assertEqual({"vendor_folder_member"}, {item["role"] for item in project["files"]})
        self.assertNotIn("vendor_folder_member", ANALYSIS_INPUT_ROLES)
        self.assertEqual("raw/190827_027pp.raw", project["files"][8]["container"])
        check = project["repository_metadata"]["analysis_input_check"]
        self.assertEqual({"status": "passed", "analysis_inputs": 12, "members": 48}, check)

    def test_the_typed_project_keeps_each_members_container_and_the_inputs(self) -> None:
        project, _ = mcp_server._project_from_analysis_unit_handoff(_handoff())
        typed = project_from_dict(project)
        again = project_from_dict(typed.as_dict())

        self.assertEqual("raw/190827_025pp.raw", again.files[0].container)
        self.assertEqual(12, len(again.analysis_inputs))
        self.assertEqual("vendor_folder", again.analysis_inputs[0]["kind"])

    def test_a_count_that_disagrees_with_the_listing_is_a_failure_record_for_the_unit(self) -> None:
        """Raised, it would stop a batch at this unit; recorded, the campaign goes on to the next."""
        project, _ = mcp_server._project_from_analysis_unit_handoff(_handoff(analysis_input_count=13))

        self.assertEqual("excluded", project["selection_status"])
        self.assertFalse(project["eligible"])
        self.assertIn(mcp_server.ANALYSIS_INPUT_COUNT_MISMATCH, project["blocking_reasons"])
        self.assertTrue(any("analysis_input_count is 13" in item for item in project["exclusion_reasons"]))
        check = project["repository_metadata"]["analysis_input_check"]
        self.assertEqual("failed", check["status"])
        self.assertEqual(mcp_server.ANALYSIS_INPUT_COUNT_MISMATCH, check["code"])

    def test_a_folder_whose_member_count_disagrees_is_a_failure_record(self) -> None:
        handoff = _handoff()
        handoff["analysis_inputs"][3]["member_count"] = 5
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual("excluded", project["selection_status"])
        self.assertTrue(
            any("declares 5 members but the file listing holds 4" in item for item in project["exclusion_reasons"])
        )

    def test_a_member_of_a_folder_no_input_names_is_a_failure_record(self) -> None:
        handoff = _handoff()
        handoff["files"][0]["container"] = "raw/elsewhere.raw"
        handoff["files"][0]["path"] = "raw/elsewhere.raw/_FUNC001.DAT"
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual("excluded", project["selection_status"])
        self.assertTrue(any("raw/elsewhere.raw" in item for item in project["exclusion_reasons"]))

    def test_a_blocking_catalog_issue_is_the_catalogs_to_report_not_a_count_mismatch(self) -> None:
        """MTBKS212 names seven folders from eight rows: the Catalog blocks it for that, and says so."""
        handoff = _handoff(
            sample_metadata=_handoff()["sample_metadata"][:11],
            sample_count=11,
            analysis_input_issues=[{"code": "container_shared_by_samples", "blocking": True, "message": "x"}],
            blocking_reasons=["analysis_input:container_shared_by_samples"],
        )
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertNotIn(mcp_server.ANALYSIS_INPUT_COUNT_MISMATCH, project["blocking_reasons"])
        self.assertIn("analysis_input:container_shared_by_samples", project["blocking_reasons"])
        self.assertEqual("container_shared_by_samples", project["analysis_input_issues"][0]["code"])

    def test_a_handoff_from_before_the_input_model_is_read_as_it_always_was(self) -> None:
        handoff = _handoff()
        for key in ("analysis_input_model", "analysis_inputs_declared", "analysis_input_count", "analysis_inputs"):
            handoff.pop(key)
        project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual([], project["analysis_inputs"])
        self.assertFalse(project["analysis_inputs_declared"])
        self.assertNotIn("analysis_input_check", project["repository_metadata"])

    def test_the_inputs_are_read_from_their_manifest_when_the_handoff_omits_them(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            handoff = _handoff()
            path = Path(temporary) / "unit-inputs.json"
            path.write_text(json.dumps(handoff["analysis_inputs"]), encoding="utf-8")
            handoff.update(analysis_inputs=[], analysis_inputs_omitted=True, analysis_input_manifest_path=str(path))
            project, _ = mcp_server._project_from_analysis_unit_handoff(handoff)

        self.assertEqual(12, len(project["analysis_inputs"]))
        self.assertNotEqual("excluded", project["selection_status"])


def _folder_project(members: dict[str, int], container: str = "raw/S1.raw") -> RepositoryProject:
    return RepositoryProject(
        repository="metabobank",
        accession="MTBKS-TEST",
        analysis_unit_id="unit-folder",
        files=[
            RepositoryFile(f"{container}/{name}", size, f"https://example.org/{name}", "vendor_folder_member", container=container)
            for name, size in members.items()
        ],
        sample_metadata=[{"sample_id": "S1", "raw_file": container}],
    )


class AFolderMustArriveWhole(unittest.TestCase):
    MEMBERS = {"_FUNC001.DAT": 4, "_FUNC001.IDX": 3, "_extern.inf": 2}

    def _folder(self, root: Path, skip: str = "", sizes: dict[str, int] | None = None) -> Path:
        folder = root / "raw" / "S1.raw"
        folder.mkdir(parents=True)
        for name, size in {**self.MEMBERS, **(sizes or {})}.items():
            if name != skip:
                (folder / name).write_bytes(b"x" * size)
        return folder

    def test_a_folder_holding_every_member_at_its_listed_size_is_complete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._folder(Path(temporary))
            record = verify_container_completeness(Path(temporary), _folder_project(self.MEMBERS))

        self.assertEqual(
            {"required": True, "containers": 1, "members": 3, "complete": True, "reader_created_files": [],
             "unlisted_file_count": 0, "unlisted_files": []},
            record,
        )

    def test_a_missing_member_stops_the_lease_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._folder(Path(temporary), skip="_FUNC001.IDX")
            with self.assertRaisesRegex(ValueError, r"raw/S1.raw: 1 member\(s\) missing: _FUNC001.IDX"):
                verify_container_completeness(Path(temporary), _folder_project(self.MEMBERS))

    def test_a_member_of_another_size_stops_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            self._folder(Path(temporary), sizes={"_FUNC001.DAT": 1})
            with self.assertRaisesRegex(ValueError, r"of another size: _FUNC001.DAT \(1 of 4 bytes\)"):
                verify_container_completeness(Path(temporary), _folder_project(self.MEMBERS))

    def test_a_partial_transfer_left_inside_the_folder_stops_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            folder = self._folder(Path(temporary))
            (folder / "_FUNC002.DAT.part").write_bytes(b"half")
            with self.assertRaisesRegex(ValueError, r"partial: _FUNC002.DAT.part"):
                verify_container_completeness(Path(temporary), _folder_project(self.MEMBERS))

    def test_a_folder_that_never_arrived_stops_the_lease(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, r"raw/S1.raw: the folder is not in the download"):
                verify_container_completeness(Path(temporary), _folder_project(self.MEMBERS))

    def test_what_a_vendor_reader_writes_into_the_folder_is_tolerated_and_named(self) -> None:
        """Bruker's baf2sql writes analysis.sqlite into a BAF .d that arrived without one."""
        members = {"analysis.baf": 5, "S1.d": 0}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "raw" / "S1.d"
            folder.mkdir(parents=True)
            (folder / "analysis.baf").write_bytes(b"x" * 5)
            (folder / "S1.d").write_bytes(b"")
            (folder / "analysis.sqlite").write_bytes(b"written by the reader")
            (folder / "notes.txt").write_bytes(b"not listed")
            record = verify_container_completeness(Path(temporary), _folder_project(members, "raw/S1.d"))

        self.assertTrue(record["complete"])
        self.assertEqual(["raw/S1.d/analysis.sqlite"], record["reader_created_files"])
        self.assertEqual(["raw/S1.d/notes.txt"], record["unlisted_files"])

    def test_a_listed_reader_file_the_reader_rewrote_is_not_a_size_mismatch(self) -> None:
        members = {"analysis.baf": 5, "analysis.sqlite": 10}
        with tempfile.TemporaryDirectory() as temporary:
            folder = Path(temporary) / "raw" / "S1.d"
            folder.mkdir(parents=True)
            (folder / "analysis.baf").write_bytes(b"x" * 5)
            (folder / "analysis.sqlite").write_bytes(b"rewritten, longer than listed")
            record = verify_container_completeness(Path(temporary), _folder_project(members, "raw/S1.d"))

        self.assertTrue(record["complete"])

    def test_a_unit_listing_no_folder_member_requires_nothing(self) -> None:
        project = RepositoryProject(
            repository="metabolights", accession="MTBLS1", analysis_unit_id="u",
            files=[RepositoryFile("FILES/a.mzML", 1, "https://example.org/a.mzML", "converted")],
        )
        with tempfile.TemporaryDirectory() as temporary:
            self.assertEqual(
                {"required": False, "containers": 0, "members": 0},
                verify_container_completeness(Path(temporary), project),
            )

    def test_the_lease_records_the_check_for_a_folder_unit(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest = read_manifest(_leased_unit(Path(temporary)))

        self.assertEqual(12, len(manifest["input_candidates"]))
        record = manifest["container_completeness"]
        self.assertEqual((True, 12, 48, True), (record["required"], record["containers"], record["members"], record["complete"]))
        discover = next(entry for entry in manifest["lease_stages"] if entry["stage"] == "discover")
        self.assertEqual((12, 12), (discover["input_candidates"], discover["vendor_folders_complete"]))
        self.assertEqual({"vendor_folder"}, {row["kind"] for row in manifest["input_lineage"]["rows"]})


class AFolderIsFoundOnceAndByItsPath(unittest.TestCase):
    def test_a_vendor_folder_nested_in_another_is_part_of_it(self) -> None:
        """An Agilent or Bruker .d may keep a directory whose name ends in .d as well."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "X.d" / "AcqData").mkdir(parents=True)
            (root / "X.d" / "5512.m").mkdir()
            (root / "X.d" / "Calibration.d").mkdir()
            (root / "X.d" / "Calibration.d" / "inner.mzML").write_bytes(b"m")
            (root / "Y.raw").mkdir()
            (root / "Y.raw" / "_FUNC001.DAT").write_bytes(b"d")
            found = _find_msdial_inputs(root)

        self.assertEqual(["X.d", "Y.raw"], [Path(item).name for item in found])

    def _declared(self, paths: list[str]) -> RepositoryProject:
        return RepositoryProject(
            repository="metabobank",
            accession="MTBKS-TEST",
            analysis_unit_id="unit-declared",
            files=[
                RepositoryFile(f"{path}/_FUNC001.DAT", 1, f"https://example.org/{path}", "vendor_folder_member", container=path)
                for path in paths
            ],
            sample_metadata=[{"sample_id": Path(path).stem, "raw_file": path} for path in paths],
            analysis_inputs=[
                {"path": path, "kind": "vendor_folder", "sample_id": Path(path).stem} for path in paths
            ],
            analysis_inputs_declared=True,
        )

    def test_declared_folders_are_matched_by_path_not_by_a_name_another_folder_shares(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("raw/a.raw", "raw/b.raw", "other/a.raw"):
                (root / relative).mkdir(parents=True)
            inputs = _find_msdial_inputs(root)
            selected = _filter_inputs_by_project_allowlist(inputs, root, self._declared(["raw/a.raw", "raw/b.raw"]))

        self.assertEqual(
            ["raw/a.raw", "raw/b.raw"],
            sorted(Path(item).relative_to(Path(temporary).resolve()).as_posix() for item in selected),
        )

    def test_a_declared_folder_missing_from_the_download_stops_the_lease_by_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "raw" / "a.raw").mkdir(parents=True)
            inputs = _find_msdial_inputs(root)
            with self.assertRaisesRegex(ValueError, r"1 of its 2 declared analysis inputs are not in the download: raw/b.raw"):
                _filter_inputs_by_project_allowlist(inputs, root, self._declared(["raw/a.raw", "raw/b.raw"]))

    def test_without_declared_inputs_the_names_decide_as_they_did(self) -> None:
        project = self._declared(["raw/a.raw"])
        project.analysis_inputs, project.analysis_inputs_declared = [], False
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for relative in ("raw/a.raw", "other/a.raw"):
                (root / relative).mkdir(parents=True)
            selected = _filter_inputs_by_project_allowlist(_find_msdial_inputs(root), root, project)

        self.assertEqual(2, len(selected))


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments], capture_output=True, text=True, check=True,
        env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
             "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"},
    )
    return completed.stdout.strip()


class TheConsoleIsTrustedByWhatItIs(unittest.TestCase):
    """A Console reads a folder named in analysis_files.csv from #739 (77a42a87c) on.

    The check used to be a substring of the Console's path, so the campaign's pinned Consoles, each in a
    checkout of its own, were refused. A synthetic MsdialWorkbench checkout stands in here: three commits,
    the middle one playing the part of #739.
    """

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary.name) / "MsdialWorkbench-console-c471463a5"
        project = self.root / "tests" / "MSDIAL5" / "MsdialCoreTestApp"
        project.mkdir(parents=True)
        (project / "MsdialCoreTestApp.csproj").write_text("<Project/>", encoding="utf-8")
        _git(self.root, "init", "-q")
        self.commits = []
        for label in ("before", "folder-csv", "after"):
            (self.root / "history.txt").write_text(label, encoding="utf-8")
            _git(self.root, "add", "-A")
            _git(self.root, "commit", "-q", "-m", label)
            self.commits.append(_git(self.root, "rev-parse", "HEAD"))
        self.console = project / "bin" / "Release" / "net48" / "MSDIALCUI.exe"
        self.console.parent.mkdir(parents=True)
        self.console.write_bytes(b"MZ console bytes")
        workflow._FOLDER_TYPE_CSV_VERDICTS.clear()
        patcher = patch.object(workflow, "FOLDER_TYPE_CSV_COMMIT", self.commits[1])
        patcher.start()
        self.addCleanup(patcher.stop)
        environment = patch.dict(os.environ, {}, clear=False)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED", None)

    def tearDown(self) -> None:
        workflow._FOLDER_TYPE_CSV_VERDICTS.clear()
        self._temporary.cleanup()

    def _record(self, head: str, sha256: str = "") -> None:
        (self.console.parent / workflow.CONSOLE_BUILD_PROVENANCE).write_text(
            json.dumps(
                {
                    "binary_sha256": sha256 or hashlib.sha256(self.console.read_bytes()).hexdigest(),
                    "git_head": head,
                    "source_root": str(self.root),
                }
            ),
            encoding="utf-8",
        )

    def test_the_pinned_console_layout_is_accepted_when_its_build_descends_from_the_fix(self) -> None:
        self._record(self.commits[2])

        self.assertTrue(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_the_fix_commit_itself_is_accepted(self) -> None:
        self._record(self.commits[1])

        self.assertTrue(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_a_console_older_than_the_fix_is_refused(self) -> None:
        self._record(self.commits[0])

        self.assertFalse(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_a_record_that_describes_another_binary_is_refused(self) -> None:
        self._record(self.commits[2], sha256="0" * 64)

        self.assertFalse(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_a_console_with_no_record_is_refused_wherever_it_sits(self) -> None:
        """The substring the old check looked for is exactly where this binary is."""
        legacy = Path(self._temporary.name) / "MsdialWorkbench" / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "bin" / "Release" / "net48"
        legacy.mkdir(parents=True)
        (legacy / "MSDIALCUI.exe").write_bytes(b"MZ")

        self.assertFalse(workflow._console_supports_folder_type_csv(str(legacy / "MSDIALCUI.exe")))
        self.assertFalse(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_a_commit_the_checkout_does_not_know_is_refused(self) -> None:
        self._record("1234567890abcdef1234567890abcdef12345678")

        self.assertFalse(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_the_environment_override_still_decides_for_a_console_built_elsewhere(self) -> None:
        with patch.dict(os.environ, {"MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED": "1"}):
            self.assertTrue(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_the_verdict_is_read_again_once_the_record_changes(self) -> None:
        self._record(self.commits[0])
        self.assertFalse(workflow._console_supports_folder_type_csv(str(self.console)))
        record = self.console.parent / workflow.CONSOLE_BUILD_PROVENANCE
        self._record(self.commits[2])
        stat = record.stat()
        os.utime(record, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))

        self.assertTrue(workflow._console_supports_folder_type_csv(str(self.console)))

    def test_the_workflow_refuses_a_folder_input_on_an_older_console(self) -> None:
        self._record(self.commits[0])
        folder = Path(self._temporary.name) / "S1.raw"
        folder.mkdir()
        issues = workflow.validate_workflow(
            {"files": [{"file_path": str(folder)}], "project_type": "lcms", "console_path": str(self.console)}
        )

        self.assertTrue(any("folder-type raw-data" in item["message"] and item["level"] == "error" for item in issues))


class TheAnalysisCsvIsBuiltFromTheLineage(unittest.TestCase):
    def test_an_mtbks217_shaped_unit_gives_twelve_ascii_rows_one_per_folder(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})
            manifest = read_manifest(manifest_path)
            built = build_repository_analysis_rows(manifest)
            order_rows(manifest, built)
            csv_path = write_analysis_csv(built, Path(temporary) / "analysis_files.csv")
            parsed = _console_rows(csv_path)

        self.assertEqual([], built["failures"])
        self.assertEqual(12, len(built["rows"]))
        self.assertEqual(
            {"input_candidates": 12, "input_lineage_rows": 12, "declared_analysis_inputs": 12, "sample_rows": 12, "rows": 12},
            built["counts"],
        )
        self.assertEqual(13, len(parsed), "a header and twelve rows")
        self.assertEqual({8}, {len(row) for row in parsed})
        self.assertEqual(
            ["190827_025pp", "190827_026pp", "190827_027pp"], [row["file_name"] for row in built["rows"][:3]]
        )
        self.assertTrue(built["rows"][2]["file_path"].endswith(r"raw\data\raw\190827_027pp.raw"))
        self.assertEqual(
            {"Sample": 9, "Blank": 2, "Standard": 1},
            {kind: sum(1 for row in built["rows"] if row["file_type"] == kind) for kind in ("Sample", "Blank", "Standard")},
        )
        self.assertEqual({"AIF"}, {row["acquisition_type"] for row in built["rows"]})
        self.assertEqual({"raw_header"}, {row["acquisition_type_source"] for row in built["rows"]})
        self.assertEqual("CSRSPlant_Arabi01", built["rows"][2]["sample_id"])
        self.assertEqual([], built["aliases"])

    def test_the_mcp_step_writes_the_csv_and_names_every_input_in_its_lineage(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                preview = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Plant"], manifest_path=str(manifest_path)
                )
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Plant"], confirmed=True, manifest_path=str(manifest_path)
                )
            with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
                written = list(csv.DictReader(handle))
            manifest = read_manifest(manifest_path)

        self.assertFalse(preview["prepared"])
        self.assertEqual("input_lineage", preview["preview"]["built_from"])
        self.assertEqual(12, preview["preview"]["matched_count"])
        self.assertEqual({"AIF": 12}, preview["preview"]["acquisition_types"])
        self.assertTrue(prepared["prepared"], prepared)
        self.assertEqual(12, len(written))
        self.assertEqual({"AIF"}, {row["acquisition_type"] for row in written})
        self.assertEqual(
            {"Arabidopsis", "Rice", "Eggplant", "none"}, {row["class_id"] for row in written}
        )
        self.assertEqual("written", manifest["analysis_csv"]["status"])
        self.assertEqual(12, manifest["analysis_csv"]["rows"])
        self.assertEqual({"190827_025pp"}, {row["file_name"] for row in manifest["input_lineage"]["rows"][:1]})
        self.assertTrue(all(row["console_path"] for row in manifest["input_lineage"]["rows"]))
        self.assertIn("analytical_order", manifest)

    def test_a_legacy_manifest_is_still_prepared_from_its_recognised_files(self) -> None:
        """No input_lineage: the name-matched path, unchanged (test_manifest_reentry covers it in full)."""
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))

            def drop(manifest: dict) -> None:
                manifest.pop("input_lineage")

            update_manifest(manifest_path, drop)
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                preview = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Plant"], manifest_path=str(manifest_path)
                )

        self.assertNotIn("built_from", preview["preview"])
        self.assertEqual(12, preview["preview"]["recognized_count"])


def _hand_made_unit(root: Path, names: list[str], mode: str = "DDA", per_file: dict[str, str] | None = None) -> Path:
    """A unit on disk with a lineage row per input and a sample row naming each. Folders end in .raw."""
    data = root / "raw" / "data"
    data.mkdir(parents=True)
    paths = []
    for name in names:
        path = data / name
        if name.endswith(".raw"):
            path.mkdir()
            (path / "_FUNC001.DAT").write_bytes(b"d" * 7)
        else:
            path.write_bytes(b"m" * 5)
        paths.append(str(path.resolve()))
    (root / "provenance").mkdir()
    (root / "output").mkdir()
    manifest = {
        "schema": "msdial-public-reanalysis-run.v1",
        "status": "prepared",
        "workspace": str(root),
        "raw_directory": str(root / "raw"),
        "input_directory": str(data),
        "output_directory": str(root / "output"),
        "input_candidates": paths,
        "execution_allowed": True,
        "raw_retention_policy": "keep",
        "input_lineage": {
            "schema": "msdial-input-lineage.v1",
            "rows": [
                {"path": path, "kind": "vendor_folder" if path.endswith(".raw") else "file",
                 "sample_id": Path(path).stem, "file_name": "", "source": {}, "checksums": {}}
                for path in paths
            ],
        },
        "project": {
            "repository": "metabolights",
            "accession": "MTBLS-ALIAS",
            "analysis_unit_id": "unit-alias",
            "separation": "LC-MS",
            "acquisition_mode": mode,
            "ion_mode": "Negative",
            "untargeted": True,
            "files": [{"name": f"FILES/{name}", "size_bytes": 1, "url": "", "role": "raw"} for name in names],
            "sample_metadata": [
                {"sample_id": Path(name).stem, "raw_file": f"FILES/{name}", "values": {"Group": "g"}} for name in names
            ],
        },
    }
    if per_file is not None:
        manifest["raw_metadata_preflight"] = {
            "summary": {
                "per_file": [
                    {"file": path, **({"console_acquisition_type": per_file[Path(path).name]} if Path(path).name in per_file else {})}
                    for path in paths
                ]
            }
        }
    manifest_path = root / "provenance" / "run-manifest.json"
    _write_json(manifest_path, manifest)
    return manifest_path


class ANameTheConsoleCannotReadGetsAnAlias(unittest.TestCase):
    """The pinned Console's parser reads ASCII and splits on ',' with no quoting (AnalysisFilesParser.cs)."""

    NAMES = ["S1,rep1.raw", "試料02.raw", "plain_03.raw", "ｻﾝﾌﾟﾙ04.mzML"]

    def _build(self, root: Path) -> tuple[Path, dict]:
        manifest_path = _hand_made_unit(root, self.NAMES)
        return manifest_path, build_repository_analysis_rows(read_manifest(manifest_path))

    def test_the_rows_name_ascii_aliases_inside_the_units_raw_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            _manifest_path, built = self._build(root)
            failures = create_console_aliases(built)
            csv_path = write_analysis_csv(built, root / "output" / "analysis_files.csv")
            # What prepare_run hands the Console: the rows read back, rewritten in ASCII in the run folder.
            files = workflow.read_analysis_csv(csv_path)["files"]
            run_csv = root / "output" / "run" / "analysis_files.csv"
            run_csv.parent.mkdir()
            workflow._write_analysis_csv(run_csv, files, [workflow.analysis_input_path(item["file_path"]) for item in files])
            parsed = _console_rows(run_csv)
            by_input = {Path(row["input_path"]).name: row for row in built["rows"]}
            comma, japanese, plain, katakana = (by_input[name] for name in self.NAMES)
            alias_dir = root / "raw" / ALIAS_DIRECTORY
            junction_target = Path(os.path.realpath(comma["file_path"]))
            linked_same = os.path.samefile(katakana["file_path"], katakana["input_path"])
            member_seen = (Path(japanese["file_path"]) / "_FUNC001.DAT").read_bytes()

        self.assertEqual([], failures)
        self.assertEqual({8}, {len(row) for row in parsed}, "no comma shifted a column")
        # By name: a hard link resolves to its own (long) path, and a junction is kept as written.
        self.assertEqual(
            sorted(Path(row["file_path"]).name for row in built["rows"]),
            sorted(Path(row[0]).name for row in parsed[1:]),
        )
        self.assertEqual(plain["input_path"], plain["file_path"], "a name the Console reads is not aliased")
        self.assertIsNone(plain["console_alias"])
        for row in (comma, japanese, katakana):
            self.assertTrue(workflow.console_safe_text(row["file_path"]), row["file_path"])
            self.assertTrue(workflow.console_safe_text(row["file_name"]))
            self.assertEqual(alias_dir, Path(row["file_path"]).parent)
        self.assertTrue(comma["file_name"].startswith("S1_rep1-"))
        self.assertEqual(".raw", Path(comma["file_path"]).suffix, "the reader is chosen by the suffix")
        self.assertEqual(("junction", "hardlink"), (comma["console_alias"]["kind"], katakana["console_alias"]["kind"]))
        self.assertEqual(Path(comma["input_path"]), junction_target)
        self.assertTrue(linked_same)
        self.assertEqual(b"d" * 7, member_seen)

    def test_the_alias_is_recorded_on_the_inputs_lineage_row(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path, built = self._build(root)
            create_console_aliases(built)
            record_analysis_csv(manifest_path, built, root / "output" / "analysis_files.csv")
            lineage = {Path(row["path"]).name: row for row in read_manifest(manifest_path)["input_lineage"]["rows"]}

        self.assertEqual("junction", lineage["S1,rep1.raw"]["console_alias"]["kind"])
        self.assertEqual(lineage["S1,rep1.raw"]["console_alias"]["path"], lineage["S1,rep1.raw"]["console_path"])
        self.assertIn("path_not_console_safe", lineage["S1,rep1.raw"]["console_alias"]["reasons"])
        self.assertNotIn("console_alias", lineage["plain_03.raw"])
        self.assertEqual("plain_03", lineage["plain_03.raw"]["file_name"])

    def test_an_alias_is_read_back_as_the_path_the_csv_names_not_its_target(self) -> None:
        """Resolving a junction names the unreadable path again; read_analysis_csv and prepare_run keep it."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            _manifest_path, built = self._build(root)
            create_console_aliases(built)
            csv_path = write_analysis_csv(built, root / "output" / "analysis_files.csv")
            read = {Path(item["file_path"]).name: item for item in workflow.read_analysis_csv(csv_path)["files"]}
            expected = {Path(row["file_path"]).name for row in built["rows"]}

        self.assertEqual(expected, set(read))

    def test_the_execution_gate_admits_the_input_an_alias_stands_for_once_it_is_recorded(self) -> None:
        """A hard link resolves to itself, not to the input it is; the lineage row says which input it is."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path, built = self._build(root)
            create_console_aliases(built)
            csv_path = write_analysis_csv(built, root / "output" / "analysis_files.csv")
            state = {
                "repository_run_manifest": str(manifest_path),
                "output_root": str(root / "output"),
                "ion_mode": "Negative",
                "files": workflow.read_analysis_csv(csv_path)["files"],
            }
            unrecorded = evaluate_repository_execution_gate(state)
            record_analysis_csv(manifest_path, built, csv_path)
            recorded = evaluate_repository_execution_gate(state)

        self.assertFalse(unrecorded["allowed"])
        self.assertTrue(any("not among the files" in item for item in unrecorded["blockers"]), unrecorded["blockers"])
        self.assertTrue(recorded["allowed"], recorded["blockers"])

    def test_the_raw_tree_counts_an_aliased_folder_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            _manifest_path, built = self._build(root)
            before = _tree_size(root / "raw")
            create_console_aliases(built)
            after = _tree_size(root / "raw")

        self.assertEqual(before, after)

    def test_a_console_container_beside_an_alias_is_looked_for_beside_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            _manifest_path, built = self._build(root)
            create_console_aliases(built)
            comma = next(row for row in built["rows"] if row["console_alias"] and row["console_alias"]["kind"] == "junction")
            directory = _directory_of(comma["file_path"])

        self.assertEqual((root / "raw" / ALIAS_DIRECTORY).resolve(), directory)

    def test_an_alias_path_holding_something_else_is_never_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            _manifest_path, built = self._build(root)
            katakana = next(row for row in built["rows"] if row["console_alias"] and row["console_alias"]["kind"] == "hardlink")
            Path(katakana["file_path"]).parent.mkdir(parents=True)
            Path(katakana["file_path"]).write_bytes(b"someone else's")
            failures = create_console_aliases(built)
            kept = Path(katakana["file_path"]).read_bytes()

        self.assertEqual(["console_alias_failed"], [item["code"] for item in failures])
        self.assertEqual(b"someone else's", kept)

    def test_a_raw_tree_the_console_cannot_read_is_a_recorded_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "ユニット"
            _manifest_path, built = self._build(root)

        self.assertIn("console_alias_unsafe_location", [item["code"] for item in built["failures"]])

    def test_a_class_label_the_console_cannot_read_is_folded_keeping_the_grouping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML", "b.mzML", "c.mzML"])
            manifest = read_manifest(manifest_path)
            workspace = {"rows": [
                {"sample_id": "a", "raw_file": "FILES/a.mzML", "class_id": "対照, 2h", "values": {}},
                {"sample_id": "b", "raw_file": "FILES/b.mzML", "class_id": "処理, 2h", "values": {}},
                {"sample_id": "c", "raw_file": "FILES/c.mzML", "class_id": "処理, 2h", "values": {}},
            ]}
            built = build_repository_analysis_rows(manifest, workspace)

        labels = [row["class_id"] for row in built["rows"]]
        self.assertTrue(all(workflow.console_safe_text(label) for label in labels), labels)
        self.assertNotEqual(labels[0], labels[1])
        self.assertEqual(labels[1], labels[2])
        self.assertEqual(2, len(built["class_id_aliases"]))


class EachRowCarriesItsOwnAcquisitionType(unittest.TestCase):
    """The pinned Console turns an acquisition_type it cannot parse into DDA without a word."""

    def test_each_files_console_acquisition_type_is_written_for_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(
                Path(temporary) / "unit", ["a.mzML", "b.mzML"], mode="DIA",
                per_file={"a.mzML": "SWATH", "b.mzML": "AIF"},
            )
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(["SWATH", "AIF"], [row["acquisition_type"] for row in built["rows"]])

    def test_a_legacy_preflight_takes_the_units_declared_swath_aif_or_dda(self) -> None:
        for mode in ("SWATH", "AIF", "DDA"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as temporary:
                manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML"], mode=mode, per_file={})
                built = build_repository_analysis_rows(read_manifest(manifest_path))

                self.assertEqual([], built["failures"])
                self.assertEqual([(mode, "unit_declaration")], [
                    (row["acquisition_type"], row["acquisition_type_source"]) for row in built["rows"]
                ])

    def test_a_bare_dia_is_refused_as_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML", "b.mzML"], mode="DIA", per_file={})
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(["acquisition_type_ambiguous"], [item["code"] for item in blocking_failures(built)])
        self.assertEqual(["a.mzML", "b.mzML"], built["failures"][0]["inputs"])

    def test_a_header_value_outside_the_three_is_not_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(
                Path(temporary) / "unit", ["a.mzML"], mode="DIA", per_file={"a.mzML": "DIA"}
            )
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(["acquisition_type_ambiguous"], [item["code"] for item in built["failures"]])

    def test_the_mcp_step_records_the_refusal_and_writes_no_csv(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path = _hand_made_unit(root, ["a.mzML", "b.mzML"], mode="DIA", per_file={})
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                result = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Group"], confirmed=True, manifest_path=str(manifest_path)
                )
            manifest = read_manifest(manifest_path)
            written = (root / "output" / "analysis_files.csv").exists()

        self.assertFalse(result["ok"])
        self.assertEqual("analysis_csv_failed", result["reason"])
        self.assertEqual(["acquisition_type_ambiguous"], result["codes"])
        self.assertEqual("failed", manifest["analysis_csv"]["status"])
        self.assertEqual("acquisition_type_ambiguous", manifest["analysis_csv"]["failures"][0]["code"])
        self.assertFalse(written)


class AUnitThatDisagreesWithItselfFailsWithARecord(unittest.TestCase):
    def test_an_input_without_a_lineage_row_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML", "b.mzML"])

            def drop(manifest: dict) -> None:
                manifest["input_lineage"]["rows"] = manifest["input_lineage"]["rows"][:1]

            update_manifest(manifest_path, drop)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(["input_without_lineage"], [item["code"] for item in built["failures"]])
        self.assertEqual(["b.mzML"], built["failures"][0]["inputs"])

    def test_a_declared_input_with_no_candidate_is_named(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})

            def drop(manifest: dict) -> None:
                gone = manifest["input_candidates"].pop()
                manifest["input_lineage"]["rows"] = [
                    row for row in manifest["input_lineage"]["rows"] if row["path"] != gone
                ]

            update_manifest(manifest_path, drop)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        codes = {item["code"]: item for item in built["failures"]}
        self.assertEqual({"analysis_input_not_found", "sample_without_input"}, set(codes))
        self.assertEqual(["raw/z_011pp.raw"], codes["analysis_input_not_found"]["inputs"])
        self.assertEqual(["Blank_sample_2"], codes["sample_without_input"]["inputs"])

    def test_an_input_no_sample_names_may_be_accepted_as_before_with_partial_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML", "b.mzML"])

            def rename(manifest: dict) -> None:
                manifest["project"]["sample_metadata"] = manifest["project"]["sample_metadata"][:1]
                for row in manifest["input_lineage"]["rows"]:
                    if row["path"].endswith("b.mzML"):
                        row["sample_id"] = ""

            update_manifest(manifest_path, rename)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(["input_without_sample"], [item["code"] for item in blocking_failures(built)])
        self.assertEqual([], blocking_failures(built, allow_partial_mapping=True))

    def test_two_inputs_with_one_stem_get_names_the_console_writes_once_each(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path = _hand_made_unit(root, ["a.mzML"])
            (root / "raw" / "data" / "a.wiff").write_bytes(b"w")

            def add(manifest: dict) -> None:
                path = str((root / "raw" / "data" / "a.wiff").resolve())
                manifest["input_candidates"].append(path)
                manifest["input_lineage"]["rows"].append({"path": path, "kind": "file", "sample_id": ""})

            update_manifest(manifest_path, add)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        names = [row["file_name"] for row in built["rows"]]
        self.assertEqual(2, len({name.casefold() for name in names}), names)
        self.assertTrue(all(row.get("file_name_reason") == "file_name_not_unique" for row in built["rows"]))


class ASplitPartTakesItsOwnFolders(unittest.TestCase):
    """A Mixed folder unit is split by header; each part is held to its own folders, members and inputs."""

    DDA = {"190827_027pp", "190827_028pp", "190827_029pp"}

    def test_a_part_lists_its_folders_members_and_builds_its_own_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            stub = Path(temporary) / "RawMetadataConsoleApp.exe"
            stub.write_bytes(b"stub")

            def extractor(command, **_kwargs):
                inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
                records = [
                    {
                        "source": {"filePath": path, "fileName": Path(path).stem},
                        "acquisition": {
                            "separation": {"value": "LiquidChromatography"},
                            "method": {"value": "DDA" if Path(path).stem in self.DDA else "DIA", "confidence": 0.9},
                            "polarity": {"value": "Positive"},
                            "msLevels": [1, 2],
                        },
                    }
                    for path in inputs
                ]
                Path(command[command.index("--output") + 1]).write_text(json.dumps(records), encoding="utf-8")
                return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

            with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=extractor):
                run_raw_metadata_preflight(manifest_path, stub, confirm_untargeted=True)
            result = split_unit_by_acquisition(manifest_path, confirmed=True)
            parts = {
                part["acquisition_mode"]: read_manifest(part["manifest_path"]) for part in result["parts"]
            }
            dda = parts["DDA"]
            built = build_repository_analysis_rows(dda)

        self.assertEqual(self.DDA, {Path(path).stem for path in dda["input_candidates"]})
        self.assertEqual(3 * len(MEMBERS), len(dda["project"]["files"]))
        self.assertEqual({f"raw/{stem}.raw" for stem in self.DDA}, {item["container"] for item in dda["project"]["files"]})
        self.assertEqual({f"raw/{stem}.raw" for stem in self.DDA}, {item["path"] for item in dda["project"]["analysis_inputs"]})
        self.assertEqual(9, len(parts["DIA"]["project"]["analysis_inputs"]))
        self.assertEqual([], built["failures"])
        self.assertEqual(sorted(self.DDA), [row["file_name"] for row in built["rows"]])
        self.assertEqual({"DDA"}, {row["acquisition_type"] for row in built["rows"]})


class TheHeaderOrderIsRecordedByTheNamesTheCsvCarries(unittest.TestCase):
    def test_a_renamed_input_is_recorded_under_its_csv_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path = _hand_made_unit(root, ["S1,rep1.mzML", "b.mzML"])

            def times(manifest: dict) -> None:
                manifest["raw_metadata_preflight"] = {"summary": {"per_file": [
                    {"file": path, "acquisition_start_time": stamp, "console_acquisition_type": "DDA"}
                    for path, stamp in zip(sorted(manifest["input_candidates"], key=str.lower),
                                           ("2020-01-02T00:00:00+00:00", "2020-01-01T00:00:00+00:00"))
                ]}}

            update_manifest(manifest_path, times)
            manifest = read_manifest(manifest_path)
            built = build_repository_analysis_rows(manifest)
            record = order_rows(manifest, built)

        self.assertEqual("raw_header_acquisition_start_time", record["derived_from"])
        by_name = {Path(item["file"]).stem: item["analytical_order"] for item in record["files"]}
        self.assertEqual({row["file_name"]: row["analytical_order"] for row in built["rows"]}, by_name)
        self.assertEqual([2, 1], [row["analytical_order"] for row in built["rows"]])


TEMPLATE = Path(__file__).resolve().parents[1] / "resources" / "msdial_console_param4lipidomics.txt"
# MTBKS217 is declared 'DIA'; its headers are made to say SWATH for these folders and AIF for the rest.
SWATH_FOLDERS = {"190827_025pp.raw", "190827_027pp.raw", "190827_030pp.raw"}


class TheRunKeepsEachRowsAcquisitionType(unittest.TestCase):
    """The guided plan writes an answered acquisition_type over every file (agent_workflow._workflow).

    The answer seed used to read the unit's one label, 'DIA' as SWATH, so a unit whose headers said AIF
    ran as SWATH, and a header's 'DIA' admits either, so the execution gate did not see it.
    """

    def _prepare(self, root: Path, types: dict[str, str]) -> tuple[Path, dict]:
        manifest_path = _leased_unit(root)
        _set_preflight(manifest_path, types)
        with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
            prepared = mcp_server.msdial_prepare_repository_reanalysis(
                hierarchy=["Plant"], confirmed=True, manifest_path=str(manifest_path)
            )
        self.assertTrue(prepared["prepared"], prepared)
        return manifest_path, prepared

    def _plan(self, root: Path, prepared: dict, **extra) -> dict:
        console = root / "MSDIALCUI.exe"
        console.write_bytes(b"not really a console binary")
        answers = {
            **prepared["preview"]["answer_seed"],
            "execute_rt_correction": False,
            "library_strategy": "none",
            "minimum_peak_height": 1000,
            "console_path": str(console),
            "template_path": str(TEMPLATE),
            **extra,
        }
        plan = build_guided_plan(prepared["input_path"], answers)
        self.assertIsNotNone(plan["workflow"], plan["remaining_questions"])
        return plan

    def test_rows_of_two_types_reach_the_console_csv_each_as_written(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED": "1"}
        ):
            root = Path(temporary)
            _manifest_path, prepared = self._prepare(root, {"*": "AIF", **{name: "SWATH" for name in SWATH_FOLDERS}})
            with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
                written = {row["file_name"]: row["acquisition_type"] for row in csv.DictReader(handle)}
            # No QA matrix: the stand-in Console has no exporter to be asked for one.
            plan = self._plan(root, prepared, run_qa=False)
            run = workflow.prepare_run(plan["workflow"])
            parsed = _console_rows(Path(run["console_input"]))
            header = parsed[0]
            ran = {row[header.index("file_name")]: row[header.index("acquisition_type")] for row in parsed[1:]}

        self.assertNotIn("acquisition_type", prepared["preview"]["answer_seed"])
        self.assertEqual({"AIF": 9, "SWATH": 3}, dict(Counter(written.values())))
        self.assertEqual(written, ran)

    def test_rows_that_share_one_type_seed_that_type_not_the_units_label(self) -> None:
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, {"MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED": "1"}
        ):
            root = Path(temporary)
            _manifest_path, prepared = self._prepare(root, {"*": "AIF"})
            plan = self._plan(root, prepared)

        self.assertEqual("AIF", prepared["preview"]["answer_seed"]["acquisition_type"])
        self.assertEqual({"AIF"}, {item["acquisition_type"] for item in plan["workflow"]["files"]})

    def test_the_gate_refuses_a_type_written_over_the_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manifest_path, prepared = self._prepare(root, {"*": "AIF"})
            files = workflow.read_analysis_csv(Path(prepared["input_path"]))["files"]
            state = {
                "repository_run_manifest": str(manifest_path),
                "output_root": prepared["output_root"],
                "ion_mode": "Positive",
                "files": files,
            }
            as_written = evaluate_repository_execution_gate(state)
            rewritten = evaluate_repository_execution_gate(
                {**state, "files": [{**item, "acquisition_type": "SWATH"} for item in files]}
            )
            lineage = read_manifest(manifest_path)["input_lineage"]["rows"]

        self.assertEqual({"AIF"}, {row["acquisition_type"] for row in lineage})
        self.assertTrue(as_written["allowed"], as_written["blockers"])
        self.assertFalse(rewritten["allowed"])
        self.assertEqual(1, len(rewritten["blockers"]), rewritten["blockers"])
        self.assertIn("12 input files would run with an acquisition type other than", rewritten["blockers"][0])
        self.assertIn("written AIF, run as SWATH", rewritten["blockers"][0])


def _exclude(manifest_path: Path, names: list[str], reason: str = "ion_mobility_out_of_scope", applied: bool = True) -> None:
    """Record a campaign disposition that runs the unit and excludes the inputs of these names."""

    def change(manifest: dict) -> None:
        manifest["campaign_disposition"] = {
            "schema": "msdial-campaign-disposition.v1",
            "disposition": "run",
            "applied": applied,
            "reasons": [],
            "warnings": [],
            "excluded_inputs": [
                {"path": path, "reason": reason} for path in manifest["input_candidates"] if Path(path).name in names
            ],
            "split_key": None,
        }

    update_manifest(manifest_path, change)


class AnInputTheDispositionExcludedIsNoRow(unittest.TestCase):
    """classify_preflight may run a unit and exclude some of its inputs, which stay input candidates."""

    def test_an_excluded_folder_gets_no_row_and_its_sample_is_not_counted_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})
            _exclude(manifest_path, ["190827_025pp.raw"])
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual(11, len(built["rows"]))
        self.assertNotIn("190827_025pp", [row["file_name"] for row in built["rows"]])
        self.assertEqual(
            [("190827_025pp.raw", "ion_mobility_out_of_scope", "standard sample")],
            [(Path(item["path"]).name, item["reason"], item["sample_id"]) for item in built["excluded_inputs"]],
        )
        self.assertEqual(list(range(1, 12)), sorted(row["listing_order"] for row in built["rows"]))

    def test_the_csv_record_names_the_excluded_input_and_the_lineage_keeps_no_csv_name_for_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})
            with patch.object(mcp_server, "_request_json", side_effect=_no_backend):
                mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Plant"], confirmed=True, manifest_path=str(manifest_path)
                )
                _exclude(manifest_path, ["190827_025pp.raw"])
                prepared = mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=["Plant"], confirmed=True, manifest_path=str(manifest_path)
                )
            with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
                written = list(csv.DictReader(handle))
            manifest = read_manifest(manifest_path)
            files = workflow.read_analysis_csv(Path(prepared["input_path"]))["files"]
            gate = evaluate_repository_execution_gate(
                {"repository_run_manifest": str(manifest_path), "output_root": prepared["output_root"],
                 "ion_mode": "Positive", "files": files}
            )

        self.assertEqual(11, len(written))
        self.assertEqual(
            ["ion_mobility_out_of_scope"], [item["reason"] for item in manifest["analysis_csv"]["excluded_inputs"]]
        )
        self.assertEqual(1, len(prepared["preview"]["excluded_inputs"]))
        excluded = next(row for row in manifest["input_lineage"]["rows"] if row["path"].endswith("190827_025pp.raw"))
        self.assertNotIn("console_path", excluded)
        self.assertNotIn("acquisition_type", excluded)
        self.assertEqual("", excluded["file_name"])
        self.assertTrue(gate["allowed"], gate["blockers"])

    def test_a_disposition_decided_but_not_applied_excludes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _leased_unit(Path(temporary))
            _set_preflight(manifest_path, {"*": "AIF"})
            _exclude(manifest_path, ["190827_025pp.raw"], applied=False)
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(12, len(built["rows"]))
        self.assertEqual([], built["excluded_inputs"])

    def test_an_excluded_input_whose_name_needs_an_alias_gets_none(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary) / "unit"
            manifest_path = _hand_made_unit(root, ["S1,rep1.raw", "b.mzML"])
            _exclude(manifest_path, ["S1,rep1.raw"], reason="raw_header_unreadable")
            built = build_repository_analysis_rows(read_manifest(manifest_path))
            failures = create_console_aliases(built)
            made = (root / "raw" / ALIAS_DIRECTORY).exists()

        self.assertEqual([], built["failures"] + failures)
        self.assertEqual(["b"], [row["file_name"] for row in built["rows"]])
        self.assertEqual([], built["samples_without_input"])
        self.assertEqual([], built["aliases"])
        self.assertFalse(made)

    def test_a_unit_whose_every_input_was_excluded_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            manifest_path = _hand_made_unit(Path(temporary) / "unit", ["a.mzML", "b.mzML"])
            _exclude(manifest_path, ["a.mzML", "b.mzML"], reason="acquisition_unresolved")
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual(["no_analysis_input"], [item["code"] for item in blocking_failures(built)])
        self.assertEqual(["a.mzML", "b.mzML"], built["failures"][0]["inputs"])


def _zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as handle:
        for name, data in entries.items():
            handle.writestr(zipfile.ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0)), data)
    return buffer.getvalue()


class ADeclaredArchivedContainerIsFoundWhereItsArchivePutIt(unittest.TestCase):
    """MetaboLights per-sample archives: A.d.zip holding B.d is extracted as B.d (container_rooted_other_name).

    The Catalog names the input after its archive, raw/A.d, since the listing shows nothing inside it.
    """

    BASE = "https://example.org/MTBLS-X/"

    def _handoff(self) -> tuple[dict, dict[str, bytes]]:
        archives = {
            "raw/A.d.zip": _zip({"B.d/analysis.baf": b"b" * 10, "B.d/B.d": b""}),
            "raw/C.d.zip": _zip({"C.d/analysis.baf": b"c" * 10}),
        }
        files, inputs, samples = [], [], []
        for index, (path, data) in enumerate(archives.items(), start=1):
            files.append({"path": path, "download_url": self.BASE + path, "size_bytes": len(data),
                          "checksum": hashlib.md5(data).hexdigest(), "role": "raw", "container": path[:-4]})
            inputs.append({"path": path[:-4], "kind": "archived_container", "archive": path, "suffix": ".d",
                           "member_count": 1, "size_bytes": len(data), "sample_id": f"S{index}"})
            samples.append({"sample_id": f"S{index}", "raw_file": path, "attributes": {"Group": "g"}})
        handoff = _handoff(
            files=files, analysis_inputs=inputs, sample_metadata=samples, sample_count=2,
            analytical_sample_count=2, analysis_input_count=2, repository="metabolights", accession="MTBLS-X",
            download_scope={"kind": "unit_files", "file_count": 2, "analysis_file_count": 2,
                            "bundle_bytes": sum(len(data) for data in archives.values())},
        )
        return handoff, {self.BASE + path: data for path, data in archives.items()}

    def _lease(self, root: Path) -> dict:
        handoff, payloads = self._handoff()
        project, _workspace = mcp_server._project_from_analysis_unit_handoff(handoff)
        typed = project_from_dict(project)
        typed.eligible, typed.selection_status, typed.blocking_reasons = True, "eligible", []
        return create_download_lease(typed, root, 10_000_000, client=_Client(payloads))

    def test_the_lease_admits_the_container_its_archive_produced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = self._lease(Path(temporary))
            manifest = read_manifest(lease["manifest_path"])

        self.assertEqual(["B.d", "C.d"], sorted(Path(item).name for item in lease["input_candidates"]))
        self.assertEqual(
            {"B.d": "S1", "C.d": "S2"},
            {Path(row["path"]).name: row["sample_id"] for row in manifest["input_lineage"]["rows"]},
        )

    def test_the_csv_gives_it_the_row_of_the_sample_it_was_declared_for(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lease = self._lease(Path(temporary))
            manifest_path = Path(lease["manifest_path"])
            _set_preflight(manifest_path, {"*": "DDA"})
            built = build_repository_analysis_rows(read_manifest(manifest_path))

        self.assertEqual([], built["failures"])
        self.assertEqual([("B", "S1"), ("C", "S2")], [(row["file_name"], row["sample_id"]) for row in built["rows"]])

    def test_the_extraction_record_says_where_the_container_went(self) -> None:
        handoff, _payloads = self._handoff()
        project = project_from_dict(mcp_server._project_from_analysis_unit_handoff(handoff)[0])
        records = [
            {"source_url": self.BASE + "raw/A.d.zip", "container_path": "raw/B.d"},
            {"source_url": self.BASE + "raw/C.d.zip", "container_path": "raw/C.d"},
            {"source_url": "https://example.org/another-unit.zip", "container_path": "raw/Z.d"},
        ]

        self.assertEqual(
            {"raw/b.d": "raw/a.d"},
            declared_archive_containers(project, declared_analysis_inputs(project), records),
        )

    def test_without_a_record_the_archive_its_sample_names_decides(self) -> None:
        handoff, _payloads = self._handoff()
        project = project_from_dict(mcp_server._project_from_analysis_unit_handoff(handoff)[0])
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for name in ("raw/B.d", "raw/C.d", "raw/D.d"):
                (root / name).mkdir(parents=True)
            inputs = _find_msdial_inputs(root)
            by_sample = _filter_inputs_by_project_allowlist(
                inputs, root, project, archive_samples={str(root / "raw" / "B.d").casefold(): "S1"}
            )
            with self.assertRaisesRegex(ValueError, "not in the download: raw/A.d"):
                _filter_inputs_by_project_allowlist(inputs, root, project)

        self.assertEqual(["B.d", "C.d"], sorted(Path(item).name for item in by_sample))


if __name__ == "__main__":
    unittest.main()
