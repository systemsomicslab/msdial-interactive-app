"""The hardened raw-metadata preflight and the one mapping from its verdicts to a campaign disposition.

Every extractor here is a stand-in: a Python function given to subprocess.run's place, or a small Python
script run as a real process where a real time limit has to expire. The real RawMetadataConsoleApp never
runs in a test.
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from subprocess import CompletedProcess
from unittest.mock import patch

from msdial_app import raw_metadata_preflight as preflight
from msdial_app.raw_metadata_preflight import (
    CHUNK_INPUTS,
    COMMAND_LINE_LIMIT,
    decide_disposition,
    file_key,
    header_console_acquisition_type,
    input_timeout_seconds,
    run_extractor,
)
from msdial_app.repository_reanalysis import (
    _acquisition_start_times,
    _summarize_raw_metadata,
    classify_preflight,
    evaluate_repository_execution_gate,
    read_manifest,
    run_raw_metadata_preflight,
    split_unit_by_acquisition,
    update_manifest,
)


def _header(
    path: str,
    method: str = "DDA",
    *,
    confidence: float = 0.8,
    source: str = "SpectrumStatistics",
    polarity: str = "Negative",
    separation: str = "LiquidChromatography",
    levels: list[int] | None = None,
    mobility: bool = False,
    targets: list[float] | None = None,
    energies: list[float] | None = None,
) -> dict:
    """One extractor record, shaped as msdial.raw-metadata.v1 writes it."""
    levels = [1, 2] if levels is None else levels
    if targets is None:
        targets = [100.0 + index * 25 for index in range(20)] if method == "DIA" else [150.1, 233.7]
    return {
        "schemaVersion": "msdial.raw-metadata.v1",
        "source": {"filePath": path, "fileName": Path(path).stem, "readerName": "SyntheticReader"},
        "acquisition": {
            "polarity": {"value": polarity, "source": "SpectrumHeader", "confidence": 1.0},
            "method": {"value": method, "source": source, "confidence": confidence, "evidence": "synthetic"},
            "separation": {"value": separation, "source": "Derived", "confidence": 0.8},
            "hasIonMobility": {"value": mobility, "source": "SpectrumHeader", "confidence": 0.8},
            "hasMs1": {"value": 1 in levels, "source": "SpectrumHeader", "confidence": 1.0},
            "hasMs2": {"value": 2 in levels, "source": "SpectrumHeader", "confidence": 1.0},
            "collisionEnergies": [30.0] if energies is None else energies,
            "isolationWindowTargets": targets,
            "msLevels": levels,
        },
        "run": {"acquisitionStartTime": {"value": "", "source": "Unknown", "confidence": 0.0, "evidence": ""}},
    }


class _Extractor:
    """A stand-in for RawMetadataConsoleApp, installed in subprocess.run's place.

    ``verdicts`` maps an input's file name to keyword arguments for _header, or to "fail" (exit 1),
    "unsupported" (exit 82), "hang" (TimeoutExpired) or "oserror". Like the real extractor it stops at the
    first input it cannot read and writes no output then.
    """

    def __init__(self, verdicts: dict, stderr_lines: int = 3, during=None) -> None:
        self.verdicts = verdicts
        self.stderr_lines = stderr_lines
        self.during = during
        self.commands: list[list[str]] = []
        self.timeouts: list[float] = []

    def __call__(self, command, **kwargs):
        self.commands.append(list(command))
        self.timeouts.append(kwargs.get("timeout"))
        if self.during is not None:
            self.during(command)
        inputs = [command[index + 1] for index, token in enumerate(command) if token == "--input"]
        output = Path(command[command.index("--output") + 1])
        stderr = "\n".join(f"synthetic stderr line {index}" for index in range(self.stderr_lines))
        for path in inputs:
            verdict = self.verdicts.get(Path(path).name, {})
            if verdict == "hang":
                raise subprocess.TimeoutExpired(command, kwargs.get("timeout"), stderr="still reading\n")
            if verdict == "oserror":
                raise OSError(206, "The filename or extension is too long")
            if verdict == "fail":
                return CompletedProcess(command, 1, stdout="", stderr=stderr + "\nSystem.IO.InvalidDataException")
            if verdict == "unsupported":
                return CompletedProcess(command, 82, stdout="", stderr="unsupported format: no raw metadata reader")
        records = [_header(path, **self.verdicts.get(Path(path).name, {})) for path in inputs]
        output.write_text(json.dumps(records[0] if len(records) == 1 else records), encoding="utf-8")
        return CompletedProcess(command, 0, stdout=str(output) + "\n" + "x" * 5000, stderr=stderr)

    def inputs_read(self) -> list[list[str]]:
        return [
            [Path(command[index + 1]).name for index, token in enumerate(command) if token == "--input"]
            for command in self.commands
        ]


def _unit(
    root: Path,
    names: list[str],
    *,
    acquisition: str = "Unknown",
    ion_mode: str = "Negative",
    separation: str = "LC-MS",
    untargeted: bool | None = True,
    folders: tuple[str, ...] = (),
    extra: dict | None = None,
) -> tuple[Path, Path, list[Path]]:
    data = root / "raw" / "data"
    for directory in (data, root / "provenance", root / "output"):
        directory.mkdir(parents=True, exist_ok=True)
    files = []
    for name in names:
        path = data / name
        if name in folders:
            path.mkdir()
            (path / "analysis.baf").write_bytes(b"baf")
        else:
            path.write_bytes(b"x")
        files.append(path)
    manifest = root / "provenance" / "run-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "msdial-public-reanalysis-run.v1",
                "status": "downloaded",
                "workspace": str(root),
                "raw_directory": str(root / "raw"),
                "input_directory": str(data),
                "output_directory": str(root / "output"),
                "input_candidates": [str(path) for path in files],
                "execution_allowed": False,
                "raw_retention_policy": "keep",
                "project": {
                    "repository": "metabolights",
                    "accession": "MTBLS-SYNTHETIC",
                    "analysis_unit_id": "unit-synthetic",
                    "separation": separation,
                    "acquisition_mode": acquisition,
                    "ion_mode": ion_mode,
                    "untargeted": untargeted,
                    "files": [
                        {"name": f"FILES/{path.name}", "size_bytes": 1, "url": "", "role": "raw"} for path in files
                    ],
                    "total_download_bytes": len(files),
                    "sample_count": len(files),
                    "sample_metadata": [
                        {"sample_id": path.stem, "raw_file": f"FILES/{path.name}", "values": {}} for path in files
                    ],
                },
                **(extra or {}),
            }
        ),
        encoding="utf-8",
    )
    extractor = root / "RawMetadataConsoleApp.exe"
    extractor.write_bytes(b"stub")
    return manifest, extractor, files


class _Scratch(unittest.TestCase):
    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.root = Path(self._directory.name).resolve()

    def tearDown(self) -> None:
        self._directory.cleanup()

    def preflight(self, manifest: Path, extractor: Path, fake: _Extractor, **options) -> dict:
        with patch("msdial_app.repository_reanalysis.subprocess.run", side_effect=fake):
            return run_raw_metadata_preflight(manifest, extractor, **options)


class ChunkingTests(_Scratch):
    def test_six_hundred_inputs_run_in_chunks_that_fit_one_command_line(self) -> None:
        # Long names, as repository file names are: all 600 on one command line, as before, would have been
        # far past what CreateProcess accepts, and subprocess raised OSError before anything was recorded.
        names = [f"{index:03d}_{'LongRepositorySampleName_' * 6}neg.mzML" for index in range(600)]
        manifest, extractor, files = _unit(self.root / "unit", names, acquisition="DDA")
        fake = _Extractor({})
        one_command = preflight.extractor_command(extractor, files, self.root / "unit" / "out.json")
        self.assertGreater(preflight.command_line_length(one_command), COMMAND_LINE_LIMIT)

        result = self.preflight(manifest, extractor, fake)

        self.assertEqual(600 // CHUNK_INPUTS, len(fake.commands))
        self.assertTrue(all(len(read) <= CHUNK_INPUTS for read in fake.inputs_read()))
        self.assertTrue(all(preflight.command_line_length(command) < COMMAND_LINE_LIMIT for command in fake.commands))
        self.assertEqual(sorted(names), sorted(name for read in fake.inputs_read() for name in read))
        merged = json.loads((manifest.parent / "raw-metadata-preflight.json").read_text(encoding="utf-8"))
        self.assertEqual(600, len(merged))
        block = result["raw_metadata_preflight"]
        self.assertEqual(600, len(block["summary"]["per_file"]))
        self.assertEqual(0, block["exit_code"])
        self.assertEqual({"input_candidates": 600, "inspected": 600, "read": 600}, {
            key: block["summary"]["coverage"][key] for key in ("input_candidates", "inspected", "read")
        })
        self.assertEqual("preflight_passed", result["status"])
        self.assertEqual("run", result["campaign_disposition"]["disposition"])
        self.assertFalse((manifest.parent / "raw-metadata-preflight-chunks").exists())

    def test_the_command_line_limit_shrinks_a_chunk_and_an_input_too_long_alone_is_never_started(self) -> None:
        names = [f"s{index:02d}_{'x' * 40}.mzML" for index in range(10)]
        manifest, extractor, files = _unit(self.root / "unit", names)
        fake = _Extractor({})
        # The longest chunk output name run_extractor plans with.
        output = self.root / "work" / "chunk-9999-input-9999.json"
        limit = preflight.command_line_length(preflight.extractor_command(extractor, files[:3], output)) + 1

        with patch("msdial_app.raw_metadata_preflight.subprocess.run", side_effect=fake):
            result = run_extractor(extractor, files, self.root / "work", command_line_limit=limit)
            tiny = run_extractor(extractor, files[:1], self.root / "work", command_line_limit=10)

        self.assertTrue(all(preflight.command_line_length(command) < limit for command in fake.commands))
        self.assertEqual([3, 3, 3, 1], [len(read) for read in fake.inputs_read()])
        self.assertEqual(10, result["counts"]["ok"])
        self.assertEqual("os_error", tiny["outcomes"][file_key(files[0])]["outcome"])
        self.assertIn("not started", tiny["outcomes"][file_key(files[0])]["error"])
        self.assertEqual(4, len(fake.commands), "the input too long alone was never started")


class IsolationTests(_Scratch):
    def test_a_file_that_fails_and_one_without_a_reader_cost_only_their_own_verdicts(self) -> None:
        names = ["a.mzML", "b_bad.mzML", "c.mzML", "d_unsupported.lcd", "e.mzML"]
        manifest, extractor, files = _unit(self.root / "unit", names, acquisition="DDA")
        fake = _Extractor({"b_bad.mzML": "fail", "d_unsupported.lcd": "unsupported"})

        result = self.preflight(manifest, extractor, fake)

        # One chunk, then each of its five inputs alone.
        self.assertEqual([names] + [[name] for name in names], fake.inputs_read())
        entries = {Path(item["file"]).name: item for item in result["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual(
            {"a.mzML": "ok", "b_bad.mzML": "failed", "c.mzML": "ok", "d_unsupported.lcd": "unsupported_format", "e.mzML": "ok"},
            {name: entry["outcome"] for name, entry in entries.items()},
        )
        self.assertEqual(1, entries["b_bad.mzML"]["exit_code"])
        self.assertEqual(82, entries["d_unsupported.lcd"]["exit_code"])
        self.assertEqual("DDA", entries["a.mzML"]["acquisition_mode"])
        block = result["raw_metadata_preflight"]
        self.assertEqual(1, block["exit_code"], "the first failure in input order")
        coverage = block["summary"]["coverage"]
        self.assertEqual((5, 3, 1, 1), (coverage["inspected"], coverage["read"], coverage["failed"], coverage["unsupported_format"]))
        self.assertEqual(3, len(json.loads(Path(block["output"]).read_text(encoding="utf-8"))))
        # Outside a campaign the unit is held for review; the disposition says what a campaign would do.
        self.assertFalse(result["execution_allowed"])
        self.assertTrue(any("could not be read" in reason for reason in result["project"]["review_reasons"]))
        disposition = result["campaign_disposition"]
        self.assertEqual("run", disposition["disposition"])
        self.assertFalse(disposition["applied"])
        self.assertEqual(
            {("b_bad.mzML", "raw_header_unreadable"), ("d_unsupported.lcd", "raw_header_unsupported_format")},
            {(Path(item["path"]).name, item["reason"]) for item in disposition["excluded_inputs"]},
        )

    def test_outside_a_campaign_an_eligible_unit_with_an_unreadable_file_stays_eligible_as_before(self) -> None:
        # One unreadable file used to stop the single extractor process: preflight_unavailable, or
        # preflight_unsupported_format when that file had no reader, and an eligible unit stayed eligible.
        # Nothing outside a campaign can exclude the file, so holding the unit back would strand it.
        for bad, status in (("fail", "preflight_unavailable"), ("unsupported", "preflight_unsupported_format")):
            with self.subTest(bad=bad):
                manifest, extractor, _ = _unit(self.root / bad, ["a.mzML", "bad.lcd", "c.mzML"], acquisition="DDA")
                update_manifest(manifest, lambda current: current.update(execution_allowed=True))
                project_before = read_manifest(manifest)["project"]

                result = self.preflight(manifest, extractor, _Extractor({"bad.lcd": bad}))

                self.assertEqual((status, True), (result["status"], result["execution_allowed"]))
                self.assertEqual(project_before, result["project"])
                self.assertIn("already eligible", result["raw_metadata_preflight"]["advisory"])
                self.assertEqual(3, len(result["raw_metadata_preflight"]["summary"]["per_file"]))
                self.assertFalse(result["campaign_disposition"]["applied"])

    def test_every_input_without_a_reader_is_still_its_own_state(self) -> None:
        manifest, extractor, _ = _unit(self.root / "unit", ["a.lcd", "b.lcd"], acquisition="DDA")
        fake = _Extractor({"a.lcd": "unsupported", "b.lcd": "unsupported"})

        result = self.preflight(manifest, extractor, fake)

        self.assertEqual("preflight_unsupported_format", result["status"])
        self.assertEqual([".lcd"], result["raw_metadata_preflight"]["unsupported_formats"])
        self.assertEqual(82, result["raw_metadata_preflight"]["exit_code"])
        disposition = result["campaign_disposition"]
        # Declared DDA with no header readable: it runs on the declaration.
        self.assertEqual("run", disposition["disposition"])
        self.assertIn("acquisition_declared_only", disposition["warnings"])


class RecordedFailureTests(_Scratch):
    def test_a_time_limit_and_an_operating_system_error_are_recorded_never_raised(self) -> None:
        names = ["a.mzML", "b_slow.mzML", "c_oserror.mzML"]
        manifest, extractor, _ = _unit(self.root / "unit", names, acquisition="DDA")
        fake = _Extractor({"b_slow.mzML": "hang", "c_oserror.mzML": "oserror"})

        result = self.preflight(manifest, extractor, fake)
        recorded = read_manifest(manifest)

        entries = {Path(item["file"]).name: item for item in recorded["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual("ok", entries["a.mzML"]["outcome"])
        self.assertEqual("timed_out", entries["b_slow.mzML"]["outcome"])
        self.assertEqual(["still reading"], entries["b_slow.mzML"]["stderr_tail"])
        self.assertEqual("os_error", entries["c_oserror.mzML"]["outcome"])
        self.assertIn("too long", entries["c_oserror.mzML"]["error"])
        self.assertEqual(-1, recorded["raw_metadata_preflight"]["exit_code"])
        self.assertEqual(result["status"], recorded["status"])

    def test_only_the_whole_preflights_os_error_on_every_chunk_still_writes_the_manifest(self) -> None:
        manifest, extractor, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"])
        fake = _Extractor({"a.mzML": "oserror", "b.mzML": "oserror"})

        result = self.preflight(manifest, extractor, fake)

        self.assertEqual("preflight_unavailable", read_manifest(manifest)["status"])
        self.assertEqual({"os_error": 2}, {key: value for key, value in result["raw_metadata_preflight"]["outcomes"].items() if value})
        self.assertEqual("skip", result["campaign_disposition"]["disposition"])
        self.assertEqual(["raw_header_unreadable"], result["campaign_disposition"]["reasons"])

    @unittest.skipUnless(os.name == "nt" or sys.platform.startswith("linux"), "needs a real child process")
    def test_a_real_process_past_its_limit_is_stopped_and_recorded(self) -> None:
        script = self.root / "stand_in.py"
        script.write_text(
            "import sys, time\n"
            "sys.stderr.write('reading the vendor file\\n'); sys.stderr.flush()\n"
            "time.sleep(30)\n",
            encoding="utf-8",
        )
        target = self.root / "slow.mzML"
        target.write_bytes(b"x")
        extractor = self.root / "RawMetadataConsoleApp.exe"
        extractor.write_bytes(b"stub")

        def runner(command, **kwargs):
            return subprocess.run([sys.executable, str(script), *command[1:]], **kwargs)

        started = time.monotonic()
        with patch.dict(preflight.TIMEOUT_POLICY, {"metadata_reader": {"base_seconds": 1.0, "seconds_per_gb": 0.0}}):
            result = run_extractor(extractor, [target], self.root / "work", runner=runner)

        self.assertLess(time.monotonic() - started, 20)
        outcome = result["outcomes"][file_key(target)]
        self.assertEqual("timed_out", outcome["outcome"])
        self.assertEqual(1.0, outcome["timeout_seconds"])
        self.assertEqual(-1, result["exit_code"])

    def test_only_a_tail_of_stderr_is_kept_and_stdout_not_at_all(self) -> None:
        manifest, extractor, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"])
        fake = _Extractor({"b.mzML": "fail"}, stderr_lines=200)

        result = self.preflight(manifest, extractor, fake)

        block = result["raw_metadata_preflight"]
        self.assertNotIn("stdout", block)
        self.assertNotIn("stderr", block)
        self.assertLessEqual(len(block["stderr_tail"]), preflight.STDERR_TAIL_LINES)
        self.assertEqual("System.IO.InvalidDataException", block["stderr_tail"][-1])
        entry = next(item for item in block["summary"]["per_file"] if item["file"].endswith("b.mzML"))
        self.assertLessEqual(len(entry["stderr_tail"]), preflight.INPUT_STDERR_TAIL_LINES)
        self.assertLess(len(json.dumps(block)), 20000)


class ProgressTests(_Scratch):
    def test_each_process_deadline_is_in_the_manifest_while_it_runs_and_gone_after(self) -> None:
        names = [f"s{index:02d}.mzML" for index in range(25)]
        manifest, extractor, _ = _unit(self.root / "unit", names)
        seen: list[dict] = []

        def watch(_command) -> None:
            seen.append(dict(read_manifest(manifest).get("raw_metadata_preflight_progress") or {}))

        self.preflight(manifest, extractor, _Extractor({}, during=watch))

        self.assertEqual(["0000", "0001"], [note["attempt"] for note in seen])
        self.assertEqual([20, 5], [note["inputs"] for note in seen])
        self.assertEqual([6000.0, 1500.0], [note["timeout_seconds"] for note in seen])
        self.assertEqual([0, 20], [note["settled"] for note in seen])
        self.assertEqual(os.getpid(), seen[0]["owner"]["pid"])
        self.assertNotIn("raw_metadata_preflight_progress", read_manifest(manifest))

    def test_a_watcher_tells_a_long_read_from_a_dead_or_overdue_one(self) -> None:
        from datetime import datetime, timedelta, timezone

        from msdial_app.process_liveness import process_created_at
        from msdial_app.repository_reanalysis import preflight_progress_state

        started = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
        note = {
            "updated_at": started.isoformat(),
            "timeout_seconds": 3600.0,
            "attempt": "0003",
            "owner": {"pid": os.getpid(), "process_created_at": process_created_at()},
        }
        running = preflight_progress_state({"raw_metadata_preflight_progress": note}, now=started + timedelta(minutes=50))
        overdue = preflight_progress_state({"raw_metadata_preflight_progress": note}, now=started + timedelta(minutes=70))
        # The same pid with another creation time is another process: the one that ran the preflight is gone.
        reused = {**note, "owner": {"pid": os.getpid(), "process_created_at": (process_created_at() or 0) - 3600}}
        gone = preflight_progress_state({"raw_metadata_preflight_progress": reused}, now=started)

        self.assertEqual("running", running["state"])
        self.assertEqual("overdue", overdue["state"])
        self.assertEqual("gone", gone["state"])
        self.assertEqual({"state": "none"}, preflight_progress_state({}))


class TimeoutPolicyTests(unittest.TestCase):
    def test_metadata_readers_have_a_flat_limit_and_waters_and_ion_mobility_scale_with_size(self) -> None:
        gigabyte = 1024**3
        self.assertEqual(300.0, input_timeout_seconds("a.mzML", "mzml", 10 * gigabyte))
        self.assertEqual(300.0, input_timeout_seconds("a.raw", "thermo_raw", 2 * gigabyte))
        self.assertEqual(300.0 + 1800.0 * 2, input_timeout_seconds("a.raw", "waters_raw", 2 * gigabyte))
        self.assertEqual(300.0 + 600.0 * 2, input_timeout_seconds("a.d", "bruker_tdf", 2 * gigabyte))
        self.assertEqual(300.0 + 600.0, input_timeout_seconds("a.d", "agilent_d_im", gigabyte))
        self.assertEqual(preflight.INPUT_TIMEOUT_CEILING_SECONDS, input_timeout_seconds("a.raw", "waters_raw", 10**6 * gigabyte))

    def test_a_chunk_is_given_the_sum_of_its_inputs_limits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            waters = root / "w.raw"
            waters.mkdir()
            (waters / "_FUNC001.DAT").write_bytes(b"\0" * 1024)
            mzml = root / "a.mzML"
            mzml.write_bytes(b"x")
            extractor = root / "RawMetadataConsoleApp.exe"
            extractor.write_bytes(b"stub")
            fake = _Extractor({})
            with patch("msdial_app.raw_metadata_preflight.subprocess.run", side_effect=fake):
                result = run_extractor(extractor, [waters, mzml], root / "work")

        expected = input_timeout_seconds(waters, "waters_raw", 1024) + 300.0
        self.assertAlmostEqual(expected, fake.timeouts[0], places=1)
        self.assertEqual("waters_raw", result["outcomes"][file_key(waters)]["format"])

    def test_vendor_folders_are_told_apart_by_what_they_hold(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            layouts = {
                "tims.d": ["analysis.tdf"],
                "baf.d": ["analysis.baf"],
                "tsf.d": ["analysis.tsf"],
                "agilent.d": ["AcqData/Contents.xml"],
                "agilent_im.d": ["AcqData/IMSFrame.bin"],
                "waters.raw": ["_FUNC001.DAT"],
                "waters_im.raw": ["_FUNC001.DAT", "_func001.cdt"],
            }
            for folder, members in layouts.items():
                for member in members:
                    path = root / folder / member
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"x")
            found = {folder: preflight.input_format(root / folder) for folder in layouts}

        self.assertEqual(
            {
                "tims.d": "bruker_tdf", "baf.d": "bruker_baf", "tsf.d": "bruker_tsf", "agilent.d": "agilent_d",
                "agilent_im.d": "agilent_d_im", "waters.raw": "waters_raw", "waters_im.raw": "waters_raw_im",
            },
            found,
        )


class ExtractorRecordTests(_Scratch):
    def test_the_manifest_names_the_extractor_by_checksum_inventory_and_provenance(self) -> None:
        manifest, extractor, _ = _unit(self.root / "unit", ["a.mzML"])

        result = self.preflight(manifest, extractor, _Extractor({}), extractor_source="setting")

        recorded = result["raw_metadata_preflight"]["extractor"]
        import hashlib

        self.assertEqual(hashlib.sha256(b"stub").hexdigest(), recorded["sha256"])
        self.assertEqual(64, len(recorded["inventory_sha256"]))
        self.assertEqual("absent", recorded["provenance_status"])
        self.assertFalse(recorded["pinned"])
        self.assertEqual("", recorded["msrawdataworkbench_commit"])
        self.assertEqual("setting", recorded["selected_from"])
        self.assertEqual(str(extractor.resolve()), recorded["path"])
        disposition_extractor = result["campaign_disposition"]["extractor"]
        self.assertEqual(
            {"sha256": recorded["sha256"], "inventory_sha256": recorded["inventory_sha256"], "provenance_status": "absent", "pinned": False},
            disposition_extractor,
        )
        entry = result["raw_metadata_preflight"]["summary"]["per_file"][0]
        self.assertEqual(recorded["sha256"], entry["extractor_sha256"])

    def test_a_reader_that_writes_into_a_vendor_folder_is_recorded(self) -> None:
        manifest, extractor, files = _unit(self.root / "unit", ["P_1.d"], folders=("P_1.d",), acquisition="DDA")

        def writes_cache(command) -> None:
            # baf2sql leaves an analysis.sqlite in a .d that had none.
            (files[0] / "analysis.sqlite").write_bytes(b"cache")

        result = self.preflight(manifest, extractor, _Extractor({}, during=writes_cache))

        entry = result["raw_metadata_preflight"]["summary"]["per_file"][0]
        self.assertEqual([str(files[0] / "analysis.sqlite")], entry["reader_created_files"])
        self.assertEqual("bruker_baf", entry["format"])

    def test_what_a_reader_wrote_stays_recorded_when_the_read_is_reused_or_made_again(self) -> None:
        manifest, extractor, files = _unit(self.root / "unit", ["P_1.d"], folders=("P_1.d",), acquisition="DDA")
        created = [str(files[0] / "analysis.sqlite")]

        def writes_cache(_command) -> None:
            cache = files[0] / "analysis.sqlite"
            if not cache.exists():
                cache.write_bytes(b"cache")

        self.preflight(manifest, extractor, _Extractor({}, during=writes_cache))
        reused = self.preflight(manifest, extractor, _Extractor({}, during=writes_cache))
        extractor.write_bytes(b"a rebuilt extractor, which reads the folder the first one wrote into")
        again = self.preflight(manifest, extractor, _Extractor({}, during=writes_cache))

        reused_entry = reused["raw_metadata_preflight"]["summary"]["per_file"][0]
        again_entry = again["raw_metadata_preflight"]["summary"]["per_file"][0]
        self.assertEqual(("reused", created), (reused_entry["outcome"], reused_entry["reader_created_files"]))
        self.assertEqual(("ok", created), (again_entry["outcome"], again_entry["reader_created_files"]))

    def test_a_split_part_knows_what_the_reader_wrote_into_its_parents_input(self) -> None:
        names = ["a_DDA.d", "b_DIA.d"]
        verdicts = {name: {"method": "DIA" if "DIA" in name else "DDA"} for name in names}
        manifest, extractor, _ = _unit(self.root / "unit", names, folders=tuple(names))

        def writes_cache(command) -> None:
            for index, token in enumerate(command):
                if token == "--input" and not (Path(command[index + 1]) / "analysis.sqlite").exists():
                    (Path(command[index + 1]) / "analysis.sqlite").write_bytes(b"cache")

        self.preflight(manifest, extractor, _Extractor(verdicts, during=writes_cache))
        split = split_unit_by_acquisition(manifest, confirmed=True)
        part = Path(split["parts"][0]["manifest_path"])
        result = self.preflight(part, extractor, _Extractor(verdicts, during=writes_cache))

        entry = result["raw_metadata_preflight"]["summary"]["per_file"][0]
        self.assertEqual("reused", entry["outcome"])
        self.assertEqual([str(Path(entry["file"]) / "analysis.sqlite")], entry["reader_created_files"])


class ReuseTests(_Scratch):
    def test_an_unchanged_input_read_by_the_same_extractor_is_not_read_again(self) -> None:
        manifest, extractor, files = _unit(self.root / "unit", ["a.mzML", "b.mzML", "c.mzML"], acquisition="DDA")
        self.preflight(manifest, extractor, _Extractor({}))
        stat = files[1].stat()
        os.utime(files[1], ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
        second = _Extractor({})

        result = self.preflight(manifest, extractor, second)

        self.assertEqual([["b.mzML"]], second.inputs_read())
        outcomes = {Path(item["file"]).name: item["outcome"] for item in result["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual({"a.mzML": "reused", "b.mzML": "ok", "c.mzML": "reused"}, outcomes)
        self.assertEqual(3, len(json.loads((manifest.parent / "raw-metadata-preflight.json").read_text(encoding="utf-8"))))

    def test_another_extractor_reads_everything_again(self) -> None:
        manifest, extractor, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"], acquisition="DDA")
        self.preflight(manifest, extractor, _Extractor({}))
        extractor.write_bytes(b"another build")
        second = _Extractor({})

        self.preflight(manifest, extractor, second)

        self.assertEqual([["a.mzML", "b.mzML"]], second.inputs_read())

    def test_a_split_part_reuses_its_parents_reads(self) -> None:
        names = ["a_DDA.mzML", "b_DIA.mzML", "c_DDA.mzML", "d_DIA.mzML"]
        verdicts = {name: {"method": "DIA" if "DIA" in name else "DDA"} for name in names}
        manifest, extractor, _ = _unit(self.root / "unit", names)
        self.preflight(manifest, extractor, _Extractor(verdicts))
        split = split_unit_by_acquisition(manifest, confirmed=True)
        part = Path(split["parts"][0]["manifest_path"])
        again = _Extractor(verdicts)

        result = self.preflight(part, extractor, again)

        self.assertEqual([], again.commands)
        self.assertEqual({"reused"}, {item["outcome"] for item in result["raw_metadata_preflight"]["summary"]["per_file"]})
        self.assertEqual(str(manifest.resolve()), result["raw_metadata_preflight"]["summary"]["per_file"][0]["reused_from"])
        self.assertEqual("preflight_passed", result["status"])


class SummaryTests(unittest.TestCase):
    def test_prm_is_recognised_as_targeted_not_unknown(self) -> None:
        summary = _summarize_raw_metadata([_header("a.mzML", "PRM", confidence=0.9)])

        self.assertEqual("PRM", summary["acquisition_mode"])
        self.assertEqual(["PRM"], summary["out_of_scope_methods"])
        self.assertIsNone(summary["per_file"][0]["console_acquisition_type"])

    def test_every_targeted_method_is_out_of_scope(self) -> None:
        for method in ("PRM", "SRM", "MRM", "SIM"):
            with self.subTest(method=method):
                manifest = _manifest([_header(f"x_{method}.mzML", method, confidence=0.9)])
                disposition = decide_disposition(manifest)
                self.assertEqual("exclude", disposition["disposition"])
                self.assertEqual([f"acquisition_out_of_scope:{method}"], disposition["reasons"])

    def test_the_per_file_record_carries_the_shared_fields(self) -> None:
        summary = _summarize_raw_metadata(
            [_header("a.mzML", "DIA", source="VendorHeader", confidence=0.95, mobility=False, polarity="Positive")]
        )
        entry = summary["per_file"][0]

        self.assertEqual(
            {
                "console_acquisition_type": "SWATH",
                "has_ms1": True,
                "has_ms2": True,
                "has_ion_mobility": False,
                "polarity": "Positive",
                "confidence": 0.95,
                "method_source": "VendorHeader",
            },
            {key: entry[key] for key in (
                "console_acquisition_type", "has_ms1", "has_ms2", "has_ion_mobility", "polarity", "confidence",
                "method_source",
            )},
        )
        self.assertEqual(["VendorHeader"], summary["evidence_kinds"])

    def test_dia_maps_to_swath_or_aif_by_isolation_and_never_by_default(self) -> None:
        self.assertEqual(("SWATH", "header_isolation_windows"), header_console_acquisition_type("DIA", [100.0, 125.0, 150.0]))
        self.assertEqual(("AIF", "header_no_isolation"), header_console_acquisition_type("DIA", []))
        self.assertEqual((None, "dia_single_isolation_target"), header_console_acquisition_type("DIA", [675.0]))
        self.assertEqual((None, "dia_isolation_unrecorded"), header_console_acquisition_type("DIA", None))
        self.assertEqual(("AIF", "header"), header_console_acquisition_type("AIF", [675.0]))
        self.assertEqual(("DDA", "header"), header_console_acquisition_type("DDA", None))
        self.assertEqual((None, ""), header_console_acquisition_type("FullScan", []))

    def test_an_unread_input_is_not_counted_as_inspected_for_the_analytical_order(self) -> None:
        manifest = {
            "raw_metadata_preflight": {
                "summary": {"per_file": [{"file": "a.mzML", "outcome": "failed", "acquisition_start_time": ""}]},
                "output": "",
            }
        }
        _times, inspected, read = _acquisition_start_times(manifest)

        self.assertEqual(set(), inspected)
        self.assertFalse(read)


def _manifest(
    records: list[dict],
    *,
    declared: dict | None = None,
    failures: dict[str, str] | None = None,
    formats: dict[str, str] | None = None,
) -> dict:
    """A preflighted manifest from extractor records and failed inputs (name -> outcome)."""
    summary = _summarize_raw_metadata(records)
    entries = []
    for entry in summary["per_file"]:
        entry.update({"outcome": "ok", "format": (formats or {}).get(Path(entry["file"]).name, "mzml")})
        entries.append(entry)
    for name, outcome in (failures or {}).items():
        # Shaped as _per_input_entries writes an input whose header could not be read.
        entries.append(
            {
                "file": name, "outcome": outcome, "acquisition_mode": "", "polarity": "",
                "format": (formats or {}).get(name, "mzml"), "has_ion_mobility": None,
                "header_console_acquisition_type": None, "console_acquisition_type": None,
            }
        )
    summary["per_file"] = entries
    summary["coverage"] = {"input_candidates": len(entries), "inspected": len(entries), "complete": True}
    declared = {"acquisition_mode": "Unknown", "ion_mode": "Unknown", "separation": "LC-MS", "untargeted": True, **(declared or {})}
    return {
        "input_candidates": [entry["file"] for entry in entries],
        "project": {"analysis_unit_id": "u"},
        "raw_metadata_preflight": {"summary": summary, "declared": declared, "extractor": {"sha256": "ab" * 32}},
    }


class DispositionMatrixTests(unittest.TestCase):
    """Every rule the user decided on 2026-09-30, one case each."""

    def decide(self, records, **options):
        return decide_disposition(_manifest(records, **options))

    def test_undeclared_acquisition_takes_the_headers_verdict(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA", confidence=0.6), _header("b.mzML", "DDA", confidence=0.6)])

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual("DDA", disposition["console_acquisition_type"])
        self.assertEqual("Negative", disposition["ion_mode"])
        self.assertEqual(
            {"a.mzML": "DDA", "b.mzML": "DDA"},
            {Path(item["path"]).name: item["console_acquisition_type"] for item in disposition["assignments"].values()},
        )

    def test_undeclared_and_unknown_in_the_headers_is_skipped(self) -> None:
        # Waters DDA read by the full-spectrum fallback: Unknown at 0.30.
        disposition = self.decide([_header("w.raw", "Unknown", confidence=0.3)])

        self.assertEqual(("skip", ["acquisition_unresolved"]), (disposition["disposition"], disposition["reasons"]))

    def test_a_declaration_the_headers_agree_with_runs(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA")], declared={"acquisition_mode": "DDA"})

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual([], disposition["warnings"])

    def test_a_declaration_with_no_readable_header_runs_on_the_declaration(self) -> None:
        for outcome in ("unsupported_format", "failed"):
            with self.subTest(outcome=outcome):
                disposition = self.decide(
                    [],
                    declared={"acquisition_mode": "DDA", "ion_mode": "Negative"},
                    failures={"a.wiff2": outcome, "b.wiff2": outcome},
                )
                self.assertEqual("run", disposition["disposition"])
                self.assertEqual("DDA", disposition["console_acquisition_type"])
                self.assertIn("acquisition_declared_only", disposition["warnings"])
                self.assertEqual({"declaration"}, {item["basis"] for item in disposition["assignments"].values()})

    def test_an_unknown_header_is_excluded_whatever_the_unit_declares(self) -> None:
        # Rule 2 of 2026-10-06: the declaration is not taken for a file whose header was read and says Unknown.
        for declared in ({"acquisition_mode": "DDA"}, {"acquisition_mode": "DIA"}, {}):
            with self.subTest(declared=declared):
                alone = self.decide([_header("w.raw", "Unknown", confidence=0.3)], declared=declared)
                beside = self.decide(
                    [_header("a.mzML", "DDA"), _header("blank.mzML", "Unknown", confidence=0.3)], declared=declared
                )

                self.assertEqual(("skip", ["acquisition_unresolved"]), (alone["disposition"], alone["reasons"]))
                self.assertNotIn("acquisition_declared_only", alone["warnings"])
                self.assertEqual("run", beside["disposition"])
                self.assertEqual({file_key("a.mzML")}, set(beside["assignments"]))
                self.assertEqual(
                    [("blank.mzML", "acquisition_unresolved")],
                    [(item["path"], item["reason"]) for item in beside["excluded_inputs"]],
                )

    def test_a_confident_header_overrides_the_declaration(self) -> None:
        disposition = self.decide([_header("a.mzML", "DIA", confidence=0.82)], declared={"acquisition_mode": "DDA"})

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual("SWATH", disposition["console_acquisition_type"])
        self.assertIn("acquisition_header_overrides_declaration", disposition["warnings"])
        self.assertEqual("DIA", disposition["declared_vs_header"][0]["decided"])

    def test_a_header_with_ms2_decides_over_the_declaration_at_any_confidence(self) -> None:
        # Rule 1 of 2026-10-06. The extractor's confidence is a constant per branch: 0.75 for every DDA read
        # without an isolation width (MTBLS1572's DDA files under the Catalog's keyword "DIA"), 0.6 here for DIA.
        cases = [
            ("DIA", "DDA", 0.75, {}, "DDA"),
            ("DDA", "DIA", 0.6, {}, "SWATH"),
            ("DDA", "DIA", 0.5, {"targets": []}, "AIF"),
            ("SWATH", "AIF", 0.3, {"targets": []}, "AIF"),
        ]
        for declared, header, confidence, options, console in cases:
            with self.subTest(declared=declared, header=header):
                disposition = self.decide(
                    [_header("a.mzML", header, confidence=confidence, **options),
                     _header("b.mzML", header, confidence=confidence, **options)],
                    declared={"acquisition_mode": declared},
                )

                self.assertEqual(("run", console), (disposition["disposition"], disposition["console_acquisition_type"]))
                self.assertTrue(all(item["basis"].startswith("header") for item in disposition["assignments"].values()))
                self.assertIn("acquisition_header_overrides_declaration", disposition["warnings"])
                self.assertNotIn("acquisition_header_disagrees_low_confidence", disposition["warnings"])
                self.assertEqual(2, len(disposition["declared_vs_header"]))
                entry = disposition["declared_vs_header"][0]
                self.assertEqual(
                    (declared, header, confidence, header, "header", "unattributed"),
                    (entry["declared"], entry["header"], entry["confidence"], entry["decided"], entry["basis"],
                     entry["declaration_source"]),
                )

    def test_a_targeted_header_is_out_of_scope_whatever_the_unit_declares(self) -> None:
        disposition = self.decide([_header("a.mzML", "PRM", confidence=0.65)], declared={"acquisition_mode": "DDA"})

        self.assertEqual(("exclude", ["acquisition_out_of_scope:PRM"]), (disposition["disposition"], disposition["reasons"]))
        self.assertNotIn("acquisition_header_overrides_declaration", disposition["warnings"])
        entry = disposition["declared_vs_header"][0]
        self.assertEqual(
            ("PRM", "excluded", "excluded", "acquisition_out_of_scope:PRM"),
            (entry["header"], entry["decided"], entry["basis"], entry["excluded_reason"]),
        )

    def test_a_header_that_overrode_the_declaration_and_was_then_excluded_is_not_said_to_run(self) -> None:
        # A DIA header with one recurring target settles neither SWATH nor AIF, and a declared DDA gives no
        # fallback: excluded as dia_scheme_unresolved. A DDA header with no MS1 is product-ion-only data.
        beside = self.decide(
            [_header("a.mzML", "DIA", targets=[500.0], confidence=0.82), _header("b.mzML", "DDA")],
            declared={"acquisition_mode": "DDA"},
        )
        alone = self.decide([_header("p.mzML", "DDA", levels=[2], confidence=0.75)], declared={"acquisition_mode": "DIA"})

        self.assertEqual(("run", "DDA"), (beside["disposition"], beside["console_acquisition_type"]))
        self.assertEqual(("exclude", ["acquisition_out_of_scope:product_ion_only"]), (alone["disposition"], alone["reasons"]))
        for disposition, reason in ((beside, "dia_scheme_unresolved"), (alone, "acquisition_out_of_scope:product_ion_only")):
            with self.subTest(reason=reason):
                self.assertNotIn("acquisition_header_overrides_declaration", disposition["warnings"])
                self.assertNotIn("run as their raw headers give", " ".join(disposition["detail"]))
                self.assertIn(f"are excluded ({reason} 1)", " ".join(disposition["detail"]))
                (entry,) = disposition["declared_vs_header"]
                self.assertEqual(
                    ("excluded", "excluded", reason), (entry["decided"], entry["basis"], entry["excluded_reason"])
                )

    def test_an_override_is_said_to_run_only_for_the_files_that_run(self) -> None:
        disposition = self.decide(
            [_header("a.mzML", "DDA", confidence=0.75), _header("b.mzML", "DDA", levels=[2], confidence=0.75)],
            declared={"acquisition_mode": "DIA"},
        )

        self.assertEqual(("run", "DDA"), (disposition["disposition"], disposition["console_acquisition_type"]))
        self.assertIn("acquisition_header_overrides_declaration", disposition["warnings"])
        detail = " ".join(disposition["detail"])
        self.assertIn("1 input(s) run as their raw headers give, over the declared DIA", detail)
        self.assertIn("1 input(s) whose raw header contradicts the declared DIA (unattributed) are excluded", detail)
        self.assertEqual(
            [("a.mzML", "DDA", "header"), ("b.mzML", "excluded", "excluded")],
            [(item["file"], item["decided"], item["basis"]) for item in disposition["declared_vs_header"]],
        )

    def test_unresolved_ms2_headers_beside_ms1_only_files_skip_the_unit_as_unresolved(self) -> None:
        # An MTBLS1842-like unit whose MS2 files all read Unknown: the MS1-only files are out of scope only for
        # want of a runnable MS2 file beside them, which a header that settles the others may give.
        records = [
            _header("a.mzML", "Unknown", confidence=0.3),
            _header("full.mzML", "FullScan", levels=[1], confidence=0.95),
        ]
        for declared in ({"acquisition_mode": "DIA"}, {"acquisition_mode": "DDA"}, {}):
            with self.subTest(declared=declared):
                disposition = self.decide(records, declared=declared)

                self.assertEqual(("skip", ["acquisition_unresolved"]), (disposition["disposition"], disposition["reasons"]))
                self.assertEqual(
                    [("a.mzML", "acquisition_unresolved"), ("full.mzML", "acquisition_out_of_scope:FullScan")],
                    [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]],
                )
                self.assertIn("skipped, not excluded", " ".join(disposition["detail"]))

    def test_an_unresolved_header_beside_a_targeted_one_still_excludes_the_unit(self) -> None:
        disposition = self.decide(
            [_header("a.mzML", "Unknown", confidence=0.3), _header("s.mzML", "SRM", confidence=0.9)],
            declared={"acquisition_mode": "DIA"},
        )

        self.assertEqual(("exclude", ["acquisition_out_of_scope:SRM"]), (disposition["disposition"], disposition["reasons"]))

    def test_a_split_part_keeps_ms1_only_files_out_of_dda_where_its_parent_was_declared_dia(self) -> None:
        # A part split before 0.5.29 may carry MS1-only inputs its DDA part folded in; its own declaration is the
        # DDA its split wrote, so the guard reads its parent's.
        records = [_header("a.mzML", "DDA", confidence=0.75), _header("full.mzML", "FullScan", levels=[1], confidence=0.95)]
        recorded = _manifest(records, declared={"acquisition_mode": "DDA"})
        recorded["split_from"] = {"acquisition_mode": "DDA", "parent_declared_acquisition_mode": "DIA"}
        legacy = _manifest(records, declared={"acquisition_mode": "DDA"})
        legacy["split_from"] = {"acquisition_mode": "DDA"}

        for disposition in (decide_disposition(recorded), decide_disposition(legacy, parent_declared="DIA")):
            with self.subTest(split_from=disposition.get("split_parent_declared_acquisition_mode")):
                self.assertEqual(("run", "DDA"), (disposition["disposition"], disposition["console_acquisition_type"]))
                self.assertEqual(
                    [("full.mzML", "ms1_only_in_declared_dia_unit")],
                    [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]],
                )
                self.assertEqual("DIA", disposition["split_parent_declared_acquisition_mode"])
                self.assertIn("split from a unit declared DIA", " ".join(disposition["detail"]))
        folded = decide_disposition(legacy, parent_declared="")
        self.assertEqual(2, len(folded["assignments"]))
        self.assertNotIn("split_parent_declared_acquisition_mode", folded)

    def test_the_declarations_source_is_recorded_beside_the_header_that_overrode_it(self) -> None:
        handoff = {"catalog_handoff": {"technical_settings": {"acquisition_mode": "DIA"}}}
        catalog = _manifest([_header("a.mzML", "DDA", confidence=0.75)], declared={"acquisition_mode": "DIA"})
        catalog["project"]["repository_metadata"] = handoff
        part = _manifest([_header("a.mzML", "DIA", confidence=0.75)], declared={"acquisition_mode": "DDA"})
        part["project"]["repository_metadata"] = handoff
        part["split_from"] = {"analysis_unit_id": "u", "acquisition_mode": "DDA"}
        elsewhere = _manifest([_header("a.mzML", "DDA", confidence=0.75)], declared={"acquisition_mode": "AIF"})
        elsewhere["project"]["repository_metadata"] = handoff
        undeclared = _manifest([_header("a.mzML", "DDA", confidence=0.75)])

        for manifest, source in (
            (catalog, "catalog_keyword_inference"), (part, "split_part"), (elsewhere, "unattributed"), (undeclared, ""),
        ):
            with self.subTest(source=source):
                disposition = decide_disposition(manifest)

                self.assertEqual("run", disposition["disposition"])
                self.assertEqual(source, disposition["declared_acquisition_source"])
                self.assertEqual(
                    [source] if source else [], [item["declaration_source"] for item in disposition["declared_vs_header"]]
                )
        self.assertIn("(catalog_keyword_inference)", " ".join(decide_disposition(catalog)["detail"]))

    def test_a_campaign_runner_can_tell_the_header_first_rule_is_in_force(self) -> None:
        from msdial_app.agent_bridge import summarize_jobs

        self.assertIn("campaign_header_first_acquisition", summarize_jobs({})["capabilities"])

    def test_ion_mobility_is_excluded_from_the_header_or_the_folder(self) -> None:
        by_header = self.decide([_header("im.d", "AIF", mobility=True, confidence=1.0)])
        by_folder = self.decide([], failures={"tims.d": "failed"}, formats={"tims.d": "bruker_tdf"})
        mixed = self.decide(
            [_header("baf.d", "DDA"), _header("im.d", "DDA", mobility=True)], formats={"baf.d": "bruker_baf"}
        )

        self.assertEqual(("exclude", ["ion_mobility_out_of_scope"]), (by_header["disposition"], by_header["reasons"]))
        self.assertEqual(("exclude", ["ion_mobility_out_of_scope"]), (by_folder["disposition"], by_folder["reasons"]))
        self.assertEqual("run", mixed["disposition"])
        self.assertEqual([("im.d", "ion_mobility_out_of_scope")], [(item["path"], item["reason"]) for item in mixed["excluded_inputs"]])

    def test_a_unit_with_no_ms2_at_all_is_excluded(self) -> None:
        disposition = self.decide([_header("a.mzML", "FullScan", levels=[1], confidence=0.95)])

        self.assertEqual(("exclude", ["acquisition_out_of_scope:FullScan"]), (disposition["disposition"], disposition["reasons"]))

    def test_ms1_only_files_beside_dda_files_are_folded_into_the_dda_run(self) -> None:
        disposition = self.decide(
            [_header("a.mzML", "DDA"), _header("pool.mzML", "FullScan", levels=[1], confidence=0.95)],
            declared={"acquisition_mode": "DDA"},
        )

        self.assertEqual("run", disposition["disposition"])
        self.assertIn("ms1_only_files_folded", disposition["warnings"])
        folded = disposition["assignments"][file_key("pool.mzML")]
        self.assertEqual(("DDA", "folded_ms1_only"), (folded["console_acquisition_type"], folded["basis"]))
        self.assertNotIn("acquisition_header_overrides_declaration", disposition["warnings"])

    def test_ms1_only_files_are_not_folded_into_dda_where_the_unit_is_declared_dia_or_aif(self) -> None:
        # Rule 3 of 2026-10-06: MTBLS1572's bbCID files carry no MS2 scans, and MTBKS217's z_014nn goes with them.
        records = [
            _header("a.mzML", "DDA", confidence=0.75),
            _header("bbcid.mzML", "FullScan", levels=[1], confidence=0.95),
            _header("full.mzML", "FullScan", levels=[1], confidence=0.95),
        ]
        for declared in ("DIA", "AIF", "SWATH"):
            with self.subTest(declared=declared):
                disposition = self.decide(records, declared={"acquisition_mode": declared})

                self.assertEqual(("run", "DDA"), (disposition["disposition"], disposition["console_acquisition_type"]))
                self.assertEqual({file_key("a.mzML")}, set(disposition["assignments"]))
                self.assertEqual(
                    [("bbcid.mzML", "ms1_only_in_declared_dia_unit"), ("full.mzML", "ms1_only_in_declared_dia_unit")],
                    [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]],
                )
                self.assertNotIn("ms1_only_files_folded", disposition["warnings"])
                self.assertIn("ms1_only_in_declared_dia_unit", " ".join(disposition["detail"]))
        for declared in ("DDA", "Unknown"):
            with self.subTest(declared=declared):
                disposition = self.decide(records, declared={"acquisition_mode": declared})

                self.assertEqual(3, len(disposition["assignments"]))
                self.assertEqual([], disposition["excluded_inputs"])
                self.assertIn("ms1_only_files_folded", disposition["warnings"])

    def test_ms1_only_files_beside_dia_or_alone_keep_their_reasons_in_a_declared_dia_unit(self) -> None:
        full = _header("full.mzML", "FullScan", levels=[1], confidence=0.95)
        beside = self.decide([_header("b.mzML", "DIA", confidence=0.82), full], declared={"acquisition_mode": "DIA"})
        alone = self.decide([full], declared={"acquisition_mode": "DIA"})

        self.assertEqual(("run", "SWATH"), (beside["disposition"], beside["console_acquisition_type"]))
        self.assertEqual([("full.mzML", "ms1_only_beside_dia")], [(item["path"], item["reason"]) for item in beside["excluded_inputs"]])
        self.assertEqual(("exclude", ["acquisition_out_of_scope:FullScan"]), (alone["disposition"], alone["reasons"]))

    def test_a_declared_dia_unit_of_dda_and_swath_headers_splits_without_its_ms1_only_files(self) -> None:
        # Rule 5: mixed modes still go to the split; the MS1-only file is in neither part.
        disposition = self.decide(
            [
                _header("a.mzML", "DDA", confidence=0.75),
                _header("b.mzML", "DIA", confidence=0.82),
                _header("full.mzML", "FullScan", levels=[1], confidence=0.95),
            ],
            declared={"acquisition_mode": "DIA"},
        )

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(
            [("DDA", ["a.mzML"]), ("SWATH", ["b.mzML"])],
            [(group["console_acquisition_type"], group["inputs"]) for group in disposition["split_key"]["groups"]],
        )
        self.assertEqual([("full.mzML", "ms1_only_in_declared_dia_unit")], [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]])

    def test_an_ms1_only_file_whose_header_gives_aif_is_never_folded_into_dda(self) -> None:
        # No MS2 recorded, yet the header gives AIF: folding it into DDA would contradict its header, which the
        # execution gate refuses. None of the per-file records on disk is one; the rule keeps the two consistent.
        disposition = self.decide(
            [_header("a.mzML", "DDA"), _header("mse.raw", "AIF", levels=[1], targets=[], confidence=0.95)]
        )

        self.assertEqual(("run", "DDA"), (disposition["disposition"], disposition["console_acquisition_type"]))
        self.assertEqual(
            [("mse.raw", "ms1_only_header_contradicts_dda")],
            [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]],
        )
        self.assertNotIn("ms1_only_files_folded", disposition["warnings"])

    def test_dda_beside_dia_splits_by_acquisition(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA"), _header("b.mzML", "DIA", confidence=0.82)])

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(["acquisition"], disposition["split_key"]["by"])
        self.assertEqual(
            [("DDA", 1), ("SWATH", 1)],
            [(group["console_acquisition_type"], group["file_count"]) for group in disposition["split_key"]["groups"]],
        )

    def test_swath_beside_aif_splits_by_acquisition(self) -> None:
        disposition = self.decide(
            [_header("a.mzML", "DIA", confidence=0.82), _header("b.d", "DIA", confidence=0.95, targets=[])]
        )

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(["AIF", "SWATH"], [group["console_acquisition_type"] for group in disposition["split_key"]["groups"]])

    def test_both_polarities_split_by_polarity(self) -> None:
        disposition = self.decide(
            [_header("pos.mzML", "DDA", polarity="Positive"), _header("neg.mzML", "DDA", polarity="Negative")]
        )

        self.assertEqual("split", disposition["disposition"])
        self.assertEqual(["polarity"], disposition["split_key"]["by"])
        self.assertEqual(["neg", "pos"], [group["part_key"] for group in disposition["split_key"]["groups"]])

    def test_acquisition_and_polarity_can_both_split(self) -> None:
        disposition = self.decide(
            [_header("a.mzML", "DDA", polarity="Positive"), _header("b.mzML", "DIA", polarity="Negative", confidence=0.9)]
        )

        self.assertEqual(["acquisition", "polarity"], disposition["split_key"]["by"])
        self.assertEqual(["dda-pos", "swath-neg"], [group["part_key"] for group in disposition["split_key"]["groups"]])

    def test_unreadable_files_beside_readable_ones_are_excluded_and_the_rest_run(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA")], failures={"b.mzML": "failed", "c.mzML": "timed_out"})

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual(
            [("b.mzML", "raw_header_unreadable"), ("c.mzML", "raw_header_unreadable")],
            [(item["path"], item["reason"]) for item in disposition["excluded_inputs"]],
        )

    def test_every_file_unreadable_and_nothing_declared_is_skipped(self) -> None:
        disposition = self.decide([], failures={"a.mzML": "failed", "b.mzML": "unsupported_format"})

        self.assertEqual(("skip", ["raw_header_unreadable"]), (disposition["disposition"], disposition["reasons"]))

    def test_an_unknown_untargeted_status_is_inferred_from_headers_with_ms1_and_ms2(self) -> None:
        inferred = self.decide([_header("a.mzML", "DDA")], declared={"untargeted": None})
        not_inferable = self.decide(
            [],
            declared={"untargeted": None, "acquisition_mode": "DDA", "ion_mode": "Negative"},
            failures={"a.mzML": "failed"},
        )

        self.assertEqual("run", inferred["disposition"])
        self.assertIn("untargeted_inferred_from_headers", inferred["warnings"])
        self.assertEqual(("skip", ["untargeted_unresolved"]), (not_inferable["disposition"], not_inferable["reasons"]))

    def test_a_targeted_study_is_excluded(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA")], declared={"untargeted": False})

        self.assertEqual(("exclude", ["targeted_out_of_scope"]), (disposition["disposition"], disposition["reasons"]))

    def test_a_header_that_cannot_tell_the_separation_never_excludes_a_repository_lc_unit(self) -> None:
        # Waters, Agilent DIA and SCIEX headers report Unknown separation.
        repository_lc = self.decide([_header("a.raw", "AIF", separation="Unknown", confidence=0.8)])
        from_headers = self.decide([_header("a.raw", "DDA")], declared={"separation": "Unknown"})
        neither = self.decide([_header("a.raw", "DDA", separation="Unknown")], declared={"separation": "Unknown"})
        gas = self.decide([_header("a.raw", "DDA")], declared={"separation": "GC-MS"})

        self.assertEqual("run", repository_lc["disposition"])
        self.assertEqual("run", from_headers["disposition"])
        self.assertIn("separation_inferred_from_headers", from_headers["warnings"])
        self.assertEqual(("skip", ["separation_unresolved"]), (neither["disposition"], neither["reasons"]))
        self.assertEqual(("exclude", ["separation_out_of_scope:GC-MS"]), (gas["disposition"], gas["reasons"]))

    def test_an_aif_file_with_no_collision_energy_target_is_only_a_warning(self) -> None:
        disposition = self.decide([_header("a.d", "AIF", confidence=1.0, targets=[], energies=[])])

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual("AIF", disposition["console_acquisition_type"])
        self.assertIn("aif_collision_energy_targets_empty", disposition["warnings"])

    def test_a_dia_file_whose_windows_are_unrecorded_takes_the_declared_type_or_is_skipped(self) -> None:
        record = _header("a.mzML", "DIA", confidence=0.9)
        record["acquisition"].pop("isolationWindowTargets")
        declared = decide_disposition(_manifest([record], declared={"acquisition_mode": "DIA"}))
        undeclared = decide_disposition(_manifest([record]))

        self.assertEqual(("run", "SWATH"), (declared["disposition"], declared["console_acquisition_type"]))
        self.assertIn("declared_dia_read_as_swath", declared["warnings"])
        self.assertEqual(("skip", ["dia_scheme_unresolved"]), (undeclared["disposition"], undeclared["reasons"]))

    def test_polarity_switching_files_are_excluded(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA", polarity="PolaritySwitching")])

        self.assertEqual(("exclude", ["polarity_switching_out_of_scope"]), (disposition["disposition"], disposition["reasons"]))

    def test_an_mzxml_input_is_excluded_as_needing_conversion(self) -> None:
        disposition = self.decide([], failures={"a.mzXML": "unsupported_format"})

        self.assertEqual(("exclude", ["conversion_required"]), (disposition["disposition"], disposition["reasons"]))

    def test_no_preflight_and_a_capped_preflight_are_skipped(self) -> None:
        capped = _manifest([_header("a.mzML")])
        capped["raw_metadata_preflight"]["summary"]["coverage"]["capped"] = True

        self.assertEqual(["raw_metadata_preflight_missing"], decide_disposition({"input_candidates": ["a"]})["reasons"])
        self.assertEqual(["raw_metadata_incomplete"], decide_disposition(capped)["reasons"])

    def test_an_input_gone_or_never_read_skips_the_unit_rather_than_shrinking_it(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            converted_later = Path(temporary) / "converted_later.mzML"
            converted_later.write_bytes(b"x")
            manifest = _manifest([_header("a.mzML", "DDA")], declared={"acquisition_mode": "DDA"})
            manifest["input_candidates"] += [str(converted_later), str(Path(temporary) / "never_downloaded.mzML")]
            disposition = decide_disposition(manifest)

        self.assertEqual("skip", disposition["disposition"])
        self.assertEqual(["inputs_missing", "raw_metadata_incomplete"], disposition["reasons"])
        self.assertEqual([], disposition["excluded_inputs"])

    def test_a_declared_targeted_acquisition_yields_to_a_read_header_and_decides_an_unreadable_unit(self) -> None:
        # Header first (2026-10-06): a targeted declaration is a declaration like any other. Untargeted status is
        # still never inferred over one (test_untargeted_is_never_inferred_over_a_declared_targeted_acquisition).
        for method in ("MRM", "SRM", "PRM", "SIM"):
            with self.subTest(method=method):
                weak = self.decide([_header("a.mzML", "DDA", confidence=0.5)], declared={"acquisition_mode": method})
                unreadable = self.decide([], declared={"acquisition_mode": method}, failures={"a.wiff2": "failed"})
                confident = self.decide([_header("a.mzML", "DDA", confidence=0.9)], declared={"acquisition_mode": method})

                self.assertEqual(("run", "DDA"), (weak["disposition"], weak["console_acquisition_type"]))
                self.assertIn("acquisition_header_overrides_declaration", weak["warnings"])
                self.assertEqual(
                    ("exclude", [f"acquisition_out_of_scope:{method}"]), (unreadable["disposition"], unreadable["reasons"])
                )
                self.assertIn("acquisition_declared_only", unreadable["warnings"])
                self.assertEqual(("run", "DDA"), (confident["disposition"], confident["console_acquisition_type"]))
                self.assertIn("acquisition_header_overrides_declaration", confident["warnings"])

    def test_srm_and_mrm_are_one_declaration(self) -> None:
        disposition = self.decide([_header("a.mzML", "MRM", confidence=0.9)], declared={"acquisition_mode": "SRM"})

        self.assertEqual(("exclude", ["acquisition_out_of_scope:MRM"]), (disposition["disposition"], disposition["reasons"]))
        self.assertNotIn("acquisition_header_overrides_declaration", disposition["warnings"])

    def test_a_declared_full_scan_is_a_declaration(self) -> None:
        weak = self.decide([_header("a.mzML", "DDA", confidence=0.6)], declared={"acquisition_mode": "FullScan"})
        unknown = self.decide([_header("w.raw", "Unknown", confidence=0.3)], declared={"acquisition_mode": "FullScan"})
        confident = self.decide([_header("a.mzML", "DDA", confidence=0.9)], declared={"acquisition_mode": "FullScan"})

        unreadable = self.decide([], declared={"acquisition_mode": "FullScan"}, failures={"a.wiff2": "failed"})

        self.assertEqual(("run", "DDA"), (weak["disposition"], weak["console_acquisition_type"]))
        self.assertEqual(("skip", ["acquisition_unresolved"]), (unknown["disposition"], unknown["reasons"]))
        self.assertEqual("run", confident["disposition"])
        self.assertEqual(("exclude", ["acquisition_out_of_scope:FullScan"]), (unreadable["disposition"], unreadable["reasons"]))

    def test_untargeted_is_never_inferred_over_a_declared_targeted_acquisition(self) -> None:
        targeted = self.decide(
            [_header("a.mzML", "DDA", confidence=0.9)], declared={"acquisition_mode": "MRM", "untargeted": None}
        )
        full_scan = self.decide(
            [_header("a.mzML", "DDA", confidence=0.9)], declared={"acquisition_mode": "FullScan", "untargeted": None}
        )

        self.assertEqual(("skip", ["untargeted_unresolved"]), (targeted["disposition"], targeted["reasons"]))
        self.assertIn("declares MRM acquisition", " ".join(targeted["detail"]))
        # Full scan names no study design; the headers may still show an untargeted acquisition.
        self.assertEqual("run", full_scan["disposition"])
        self.assertIn("untargeted_inferred_from_headers", full_scan["warnings"])


    def test_what_the_lease_kept_out_is_excluded_in_every_disposition_with_its_reason(self) -> None:
        """An mzXML whose conversion failed and an mzML RawDataHandler cannot decode are no input candidates, but the
        Catalog declared them, and the gate's INP-1 accounts for a declared input that is no candidate only through
        the binding disposition's excluded_inputs. They decide nothing about the rest of the unit."""
        kept_out = [
            {"path": "S03.mzXML", "reason": "conversion_failed", "problems": ["ParseError: unclosed token"]},
            {"path": "n.mzML", "reason": "unsupported_mzml_encoding", "problems": ["MS:1002312"]},
        ]
        cases = {
            "run": _manifest([_header("a.mzML", "DDA")], declared={"acquisition_mode": "DDA"}),
            "split": _manifest([_header("a.mzML", "DDA", polarity="Positive"), _header("b.mzML", "DDA")]),
            "skip": _manifest([_header("w.raw", "Unknown", confidence=0.3)]),
            "exclude": _manifest([_header("a.mzML", "DDA", polarity="PolaritySwitching")]),
        }
        for kind, manifest in cases.items():
            with self.subTest(disposition=kind):
                manifest["excluded_input_candidates"] = kept_out
                disposition = decide_disposition(manifest)
                alone = decide_disposition({**manifest, "excluded_input_candidates": []})

                self.assertEqual(kind, disposition["disposition"])
                self.assertEqual(
                    [("S03.mzXML", "conversion_failed"), ("n.mzML", "unsupported_mzml_encoding")],
                    [(item["path"], item["reason"]) for item in disposition["excluded_inputs"][:2]],
                )
                self.assertEqual(alone["excluded_inputs"], disposition["excluded_inputs"][2:])
                for key in ("disposition", "reasons", "warnings", "split_key"):
                    self.assertEqual(alone[key], disposition[key], key)

    def test_the_record_has_the_shared_contract_shape(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA")])
        disposition.pop("assignments")

        for key in ("schema", "disposition", "reasons", "warnings", "excluded_inputs", "split_key", "decided_at", "extractor"):
            self.assertIn(key, disposition)
        self.assertEqual("msdial-campaign-disposition.v1", disposition["schema"])
        self.assertEqual({"sha256", "inventory_sha256", "provenance_status", "pinned"}, set(disposition["extractor"]))


def _legacy_entry(path: str, method: str, levels: list[int] | None = None, confidence: float = 0.8) -> dict:
    """A per-file record as Interactive 0.5.16 and earlier summarised an input."""
    return {
        "file": path, "acquisition_mode": method, "confidence": confidence, "evidence": "synthetic",
        "polarity": "Negative", "ms_levels": [1, 2] if levels is None else levels,
        "acquisition_start_time": "", "acquisition_start_time_evidence": "",
    }


def _legacy_manifest(entries: list[dict], declared: dict | None = None) -> dict:
    return {
        "input_candidates": [entry["file"] for entry in entries],
        "project": {"analysis_unit_id": "u", "separation": "LC-MS", "untargeted": True, "ion_mode": "Negative",
                    **(declared or {})},
        "raw_metadata_preflight": {
            "exit_code": 0,
            "summary": {"per_file": entries, "coverage": {"complete": True}, "observed_separations": []},
        },
    }


class LegacySummaryTests(_Scratch):
    """Per-file records written before this preflight recorded formats, MS-level flags and isolation."""

    def test_an_ion_mobility_folder_is_told_from_the_disk(self) -> None:
        folder = self.root / "IM-AI_mltstd_01.d"
        (folder / "AcqData").mkdir(parents=True)
        (folder / "AcqData" / "IMSFrame.bin").write_bytes(b"x")

        disposition = decide_disposition(_legacy_manifest([_legacy_entry(str(folder), "AIF")], {"acquisition_mode": "AIF"}))

        self.assertEqual(("exclude", ["ion_mobility_out_of_scope"]), (disposition["disposition"], disposition["reasons"]))
        self.assertIn("raw_metadata_preflight_legacy", disposition["warnings"])

    def test_ms1_and_ms2_are_read_from_ms_levels(self) -> None:
        both = decide_disposition(
            _legacy_manifest([_legacy_entry("a.mzML", "DDA")], {"acquisition_mode": "DDA", "untargeted": None})
        )
        ms1_only = decide_disposition(_legacy_manifest([_legacy_entry("a.mzML", "DDA", levels=[1])]))

        self.assertEqual("run", both["disposition"])
        self.assertIn("untargeted_inferred_from_headers", both["warnings"])
        self.assertEqual(("exclude", ["acquisition_out_of_scope:FullScan"]), (ms1_only["disposition"], ms1_only["reasons"]))

    def test_a_legacy_aif_verdict_is_aif_without_a_declaration(self) -> None:
        disposition = decide_disposition(_legacy_manifest([_legacy_entry("a.d", "AIF", confidence=1.0)]))

        self.assertEqual(("run", "AIF"), (disposition["disposition"], disposition["console_acquisition_type"]))

    def test_classify_decides_a_legacy_dia_summary_from_the_extractor_records(self) -> None:
        manifest, _stub, files = _unit(
            self.root / "unit", ["a_DIA.mzML", "b_DIA.mzML"], acquisition="DIA",
            extra={"campaign_authorizations": [dict(_APPROVAL)]},
        )
        output = manifest.parent / "raw-metadata-preflight.json"
        records = [_header(str(path), "DIA", confidence=0.82) for path in files]
        output.write_text(json.dumps(records), encoding="utf-8")

        def legacy(current: dict) -> None:
            current["status"] = "preflight_passed"
            current["raw_metadata_preflight"] = {
                "exit_code": 0, "output": str(output),
                "summary": {
                    "per_file": [_legacy_entry(str(path), "DIA", confidence=0.82) for path in files],
                    "coverage": {"input_candidates": 2, "inspected": 2, "complete": True},
                    "observed_separations": ["LiquidChromatography"],
                },
            }

        update_manifest(manifest, legacy)

        disposition = classify_preflight(manifest)
        recorded = read_manifest(manifest)

        self.assertEqual(("run", "SWATH"), (disposition["disposition"], disposition["console_acquisition_type"]))
        # The windows the extractor recorded decided SWATH, not the declaration.
        self.assertNotIn("declared_dia_read_as_swath", disposition["warnings"])
        self.assertIn("raw_metadata_preflight_legacy", disposition["warnings"])
        entries = recorded["raw_metadata_preflight"]["summary"]["per_file"]
        self.assertEqual(["SWATH", "SWATH"], [entry["console_acquisition_type"] for entry in entries])
        # Decided from the records, recorded as it was.
        self.assertNotIn("header_console_acquisition_type", entries[0])


class _PinnedExtractor:
    """A synthetic build recorded by record_build, so it inspects as verified and pinned."""

    @staticmethod
    def make(root: Path) -> Path:
        import test_raw_metadata_extractor as fixtures
        from msdial_app import raw_metadata_extractor

        raw, common, binary = fixtures._write_build(root)
        with patch.object(raw_metadata_extractor, "console_git_state", side_effect=fixtures._git_state()), patch.object(
            raw_metadata_extractor, "_git", return_value=""
        ):
            inspection = raw_metadata_extractor.record_build(binary, raw, common, sdk_version="10.0.401")
        assert inspection["provenance_status"] == "verified" and inspection["pinned"], inspection
        return binary


_APPROVAL = {"approval_id": "approval-1", "manifest_digest": "sha256:" + "0" * 64, "boundary": 1, "unit_id": "unit-synthetic"}


class CampaignPreflightTests(_Scratch):
    def campaign_unit(self, names, **options):
        return _unit(self.root / "unit", names, extra={"campaign_authorizations": [dict(_APPROVAL)]}, **options)

    def test_a_campaign_refuses_an_extractor_that_is_not_verified_and_pinned_before_reading(self) -> None:
        from msdial_app.raw_metadata_extractor import RawMetadataExtractorRefused

        manifest, stub, _ = self.campaign_unit(["a.mzML"])
        before = manifest.read_bytes()
        fake = _Extractor({})

        with self.assertRaises(RawMetadataExtractorRefused) as refusal:
            self.preflight(manifest, stub, fake)

        self.assertIn("extractor_absent", refusal.exception.codes)
        self.assertIn("extractor_not_pinned", refusal.exception.codes)
        self.assertTrue(str(refusal.exception).startswith("raw_metadata_extractor_refused ["))
        self.assertEqual([], fake.commands)
        self.assertEqual(before, manifest.read_bytes())

    def test_a_campaign_unit_runs_as_its_disposition_decides(self) -> None:
        manifest, _stub, files = self.campaign_unit(["a.mzML", "pool.mzML", "bad.mzML"], untargeted=None)
        extractor = _PinnedExtractor.make(self.root / "build")
        fake = _Extractor({"pool.mzML": {"method": "FullScan", "levels": [1], "confidence": 0.95}, "bad.mzML": "fail"})

        result = self.preflight(manifest, extractor, fake)

        disposition = result["campaign_disposition"]
        self.assertEqual("run", disposition["disposition"])
        self.assertTrue(disposition["applied"])
        self.assertEqual("approval-1", disposition["campaign"]["approval_id"])
        self.assertTrue(result["execution_allowed"])
        self.assertEqual("preflight_passed", result["status"])
        self.assertEqual(("DDA", "Negative", True), tuple(result["project"][key] for key in ("acquisition_mode", "ion_mode", "untargeted")))
        self.assertTrue(any("inferred from the raw headers" in line for line in result["project"]["evidence"]))
        entries = {Path(item["file"]).name: item for item in result["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual(("DDA", "folded_ms1_only"), (entries["pool.mzML"]["console_acquisition_type"], entries["pool.mzML"]["console_acquisition_basis"]))
        self.assertTrue(result["raw_metadata_preflight"]["extractor"]["pinned"])

        state = {
            "repository_run_manifest": str(manifest),
            "output_root": str(self.root / "unit" / "output"),
            "ion_mode": "Negative",
            "files": [{"file_path": str(path), "acquisition_type": "DDA"} for path in files[:2]],
        }
        self.assertTrue(evaluate_repository_execution_gate(state)["allowed"], evaluate_repository_execution_gate(state)["blockers"])
        state["files"].append({"file_path": str(files[2]), "acquisition_type": "DDA"})
        refused = evaluate_repository_execution_gate(state)
        self.assertFalse(refused["allowed"])
        self.assertTrue(any("excluded by this unit's campaign disposition" in item for item in refused["blockers"]))

    def test_a_campaign_unit_the_disposition_skips_or_excludes_is_held_back(self) -> None:
        manifest, _stub, _ = self.campaign_unit(["a.mzML"])
        extractor = _PinnedExtractor.make(self.root / "build")

        result = self.preflight(manifest, extractor, _Extractor({"a.mzML": {"method": "MRM", "confidence": 1.0}}))

        self.assertEqual("excluded_by_preflight", result["status"])
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(["acquisition_out_of_scope:MRM"], result["campaign_disposition"]["reasons"])

    def test_a_weak_header_runs_over_the_declaration_and_an_unknown_one_is_left_out(self) -> None:
        # MTBLS1572 as 2026-10-06 decided it: DDA headers at the extractor's constant 0.75 run as DDA in a unit the
        # Catalog's keyword match declares DIA, and the blank whose header is Unknown is not run on the declaration.
        manifest, _stub, files = self.campaign_unit(["a.mzML", "b.mzML", "blank.mzML"], acquisition="DIA")
        extractor = _PinnedExtractor.make(self.root / "build")
        verdicts = {
            "a.mzML": {"method": "DDA", "confidence": 0.75},
            "b.mzML": {"method": "DDA", "confidence": 0.75},
            "blank.mzML": {"method": "Unknown", "confidence": 0.3},
        }

        result = self.preflight(manifest, extractor, _Extractor(verdicts))

        disposition = result["campaign_disposition"]
        self.assertTrue(result["execution_allowed"])
        self.assertEqual("DDA", result["project"]["acquisition_mode"])
        self.assertEqual([("blank.mzML", "acquisition_unresolved")], [
            (Path(item["path"]).name, item["reason"]) for item in disposition["excluded_inputs"]
        ])
        self.assertEqual(["DIA", "DIA"], [item["declared"] for item in disposition["declared_vs_header"]])
        entries = {Path(item["file"]).name: item for item in result["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual(("DDA", "header"), (entries["a.mzML"]["console_acquisition_type"], entries["a.mzML"]["console_acquisition_basis"]))
        self.assertEqual((None, ""), (entries["blank.mzML"]["console_acquisition_type"], entries["blank.mzML"]["console_acquisition_basis"]))
        state = {
            "repository_run_manifest": str(manifest),
            "output_root": str(self.root / "unit" / "output"),
            "ion_mode": "Negative",
            "files": [{"file_path": str(path), "acquisition_type": "DDA"} for path in files[:2]],
        }
        self.assertTrue(evaluate_repository_execution_gate(state)["allowed"])
        as_swath = evaluate_repository_execution_gate(
            {**state, "files": [{**item, "acquisition_type": "SWATH"} for item in state["files"]]}
        )
        self.assertFalse(as_swath["allowed"])
        self.assertTrue(any("raw header contradicts" in item for item in as_swath["blockers"]), as_swath["blockers"])
        with_blank = evaluate_repository_execution_gate(
            {**state, "files": [*state["files"], {"file_path": str(files[2]), "acquisition_type": "SWATH"}]}
        )
        self.assertFalse(with_blank["allowed"])
        self.assertTrue(any("blank.mzML (acquisition_unresolved)" in item for item in with_blank["blockers"]))

    def test_a_header_that_guesses_another_separation_does_not_reach_a_run_of_a_repository_lc_unit(self) -> None:
        manifest, _stub, _ = self.campaign_unit(["a.mzML"], acquisition="DDA")
        extractor = _PinnedExtractor.make(self.root / "build")

        result = self.preflight(manifest, extractor, _Extractor({"a.mzML": {"separation": "GasChromatography"}}))

        self.assertEqual("run", result["campaign_disposition"]["disposition"])
        self.assertEqual("LC-MS", result["project"]["separation"])
        self.assertTrue(result["execution_allowed"])

    def test_an_approval_passed_in_must_name_the_unit(self) -> None:
        from msdial_app.campaign_authorization import CampaignAuthorizationError

        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML"])
        record = self.root / "approval.json"
        record.write_text(
            json.dumps(
                {
                    "schema": "msdial-campaign-authorization.v1", "approval_id": "a2", "campaign_id": "c",
                    "manifest_digest": "sha256:" + "1" * 64, "approved_by": "synthetic", "approved_at": "2026-09-30T00:00:00Z",
                    "covers": [1, 3, 4, 5, "split"], "units": ["another-unit"], "raw_retention_policy": "keep",
                }
            ),
            encoding="utf-8",
        )

        with self.assertRaises(CampaignAuthorizationError) as refusal:
            self.preflight(manifest, stub, _Extractor({}), campaign_authorization_path=record)

        self.assertIn("unit_not_covered", refusal.exception.codes)

    def test_outside_a_campaign_the_disposition_is_advice_and_nothing_else_changes(self) -> None:
        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML", "pool.mzML"], acquisition="DDA")

        result = self.preflight(manifest, stub, _Extractor({"pool.mzML": {"method": "FullScan", "levels": [1], "confidence": 0.95}}))

        self.assertEqual("run", result["campaign_disposition"]["disposition"])
        self.assertFalse(result["campaign_disposition"]["applied"])
        # As before: the headers disagree, so the unit is Mixed and held for a split.
        self.assertEqual("preflight_mixed_acquisition", result["status"])
        self.assertEqual("Mixed", result["project"]["acquisition_mode"])
        self.assertFalse(result["execution_allowed"])

    def test_classify_preflight_applies_a_disposition_once_the_unit_is_a_campaign_unit(self) -> None:
        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"], acquisition="DDA")
        self.preflight(manifest, stub, _Extractor({"b.mzML": "fail"}))
        self.assertFalse(read_manifest(manifest)["campaign_disposition"]["applied"])
        update_manifest(manifest, lambda current: current.update(campaign_authorizations=[dict(_APPROVAL)]))

        disposition = classify_preflight(manifest)
        recorded = read_manifest(manifest)

        self.assertEqual("run", disposition["disposition"])
        self.assertTrue(recorded["campaign_disposition"]["applied"])
        self.assertTrue(recorded["execution_allowed"])
        self.assertEqual([("b.mzML", "raw_header_unreadable")], [
            (Path(item["path"]).name, item["reason"]) for item in recorded["campaign_disposition"]["excluded_inputs"]
        ])

    def test_a_declared_mrm_unit_with_weak_dda_headers_is_held_back_not_run_as_dda(self) -> None:
        # The DDA headers decide the acquisition (2026-10-06), but untargeted status is never inferred over a
        # declared targeted acquisition, so a unit that does not declare it untargeted does not run.
        manifest, _stub, _ = self.campaign_unit(["a.wiff", "b.wiff"], acquisition="MRM", untargeted=None)
        extractor = _PinnedExtractor.make(self.root / "build")
        weak = {name: {"method": "DDA", "confidence": 0.55} for name in ("a.wiff", "b.wiff")}

        result = self.preflight(manifest, extractor, _Extractor(weak))

        self.assertEqual("skipped_by_preflight", result["status"])
        self.assertFalse(result["execution_allowed"])
        self.assertEqual(["untargeted_unresolved"], result["campaign_disposition"]["reasons"])
        self.assertIn("declares MRM acquisition", " ".join(result["campaign_disposition"]["detail"]))
        self.assertIsNot(True, result["project"]["untargeted"])

    def test_an_input_the_disposition_excludes_runs_as_no_type(self) -> None:
        manifest, _stub, _ = self.campaign_unit(["a.mzML", "im.mzML"])
        extractor = _PinnedExtractor.make(self.root / "build")
        mobility = {"im.mzML": {"method": "AIF", "mobility": True, "targets": [], "confidence": 1.0}}

        result = self.preflight(manifest, extractor, _Extractor(mobility))

        entries = {Path(item["file"]).name: item for item in result["raw_metadata_preflight"]["summary"]["per_file"]}
        self.assertEqual(("DDA", None), (entries["a.mzML"]["console_acquisition_type"], entries["im.mzML"]["console_acquisition_type"]))
        self.assertEqual("AIF", entries["im.mzML"]["header_console_acquisition_type"])


def _touch(paths: list[Path]) -> None:
    """Move each input's modification time on, so the next preflight reads it again instead of reusing it."""
    for path in paths:
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))


class DispositionHoldTests(_Scratch):
    """A split parent, a finished unit, a running one and an unpreflighted one keep the state they are in."""

    def split_campaign_parent(self) -> Path:
        # b's header is DIA with one isolation target: Mixed as the summary reads it, and a DIA file the
        # disposition cannot type, so a classification applied to the parent would make it runnable as DDA.
        names = ["a_DDA.mzML", "b_DIA.mzML"]
        verdicts = {"a_DDA.mzML": {"method": "DDA"}, "b_DIA.mzML": {"method": "DIA", "targets": [675.0]}}
        manifest, extractor, _ = _unit(self.root / "unit", names)
        self.preflight(manifest, extractor, _Extractor(verdicts))
        self.assertTrue(split_unit_by_acquisition(manifest, confirmed=True)["written"])
        update_manifest(manifest, lambda current: current.update(campaign_authorizations=[dict(_APPROVAL)]))
        return manifest

    def test_classify_never_makes_a_split_parent_runnable(self) -> None:
        manifest = self.split_campaign_parent()
        before = manifest.read_bytes()

        disposition = classify_preflight(manifest)

        self.assertEqual(before, manifest.read_bytes())
        self.assertFalse(disposition["applied"])
        self.assertEqual("split_parent", disposition["held"]["reason"])
        self.assertEqual(("split_by_acquisition", False), tuple(read_manifest(manifest)[key] for key in ("status", "execution_allowed")))

    def test_classify_never_skips_a_split_parent_whose_raw_data_its_parts_read(self) -> None:
        manifest = self.split_campaign_parent()

        def capped(current: dict) -> None:
            current["raw_metadata_preflight"]["summary"]["coverage"]["capped"] = True

        update_manifest(manifest, capped)
        before = read_manifest(manifest).get("campaign_disposition")

        disposition = classify_preflight(manifest)
        recorded = read_manifest(manifest)

        self.assertEqual("skip", disposition["disposition"])
        self.assertEqual("split_by_acquisition", recorded["status"])
        self.assertEqual(before, recorded.get("campaign_disposition"), "a skip reached the split parent's record")

    def test_classify_leaves_a_finished_unit_where_its_run_left_it(self) -> None:
        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML"], acquisition="DDA")
        self.preflight(manifest, stub, _Extractor({}))
        update_manifest(
            manifest, lambda current: current.update(status="mztab_validated", campaign_authorizations=[dict(_APPROVAL)])
        )
        before = manifest.read_bytes()

        disposition = classify_preflight(manifest)

        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(("past_preflight", "mztab_validated"), (disposition["held"]["reason"], disposition["held"]["status"]))

    def test_classify_does_not_skip_a_unit_that_was_never_preflighted(self) -> None:
        manifest, _stub, _ = _unit(self.root / "unit", ["a.mzML"], extra={"campaign_authorizations": [dict(_APPROVAL)]})
        before = manifest.read_bytes()

        disposition = classify_preflight(manifest)

        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(["raw_metadata_preflight_missing"], disposition["reasons"])
        self.assertEqual("raw_metadata_preflight_missing", disposition["held"]["reason"])
        self.assertEqual("downloaded", read_manifest(manifest)["status"])

    def test_classify_holds_a_unit_whose_run_is_open(self) -> None:
        from msdial_app.repository_reanalysis import record_run_start

        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML"], acquisition="DDA")
        self.preflight(manifest, stub, _Extractor({}))
        update_manifest(manifest, lambda current: current.update(campaign_authorizations=[dict(_APPROVAL)]))
        self.assertTrue(record_run_start(manifest, "job-1")["recorded"])
        before = manifest.read_bytes()

        disposition = classify_preflight(manifest)

        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(("run_in_progress", "job-1"), (disposition["held"]["reason"], disposition["held"]["job_id"]))

    def test_a_campaign_preflight_reads_nothing_of_a_split_or_finished_unit(self) -> None:
        parent = self.split_campaign_parent()
        finished, _stub, _ = _unit(
            self.root / "finished", ["a.mzML"],
            extra={"campaign_authorizations": [dict(_APPROVAL)], "status": "cleanup_pending_confirmation"},
        )
        for manifest, reason in ((parent, "split_parent"), (finished, "past_preflight")):
            with self.subTest(reason=reason):
                before = manifest.read_bytes()
                fake = _Extractor({})

                result = self.preflight(manifest, _PinnedExtractor.make(self.root / f"build-{reason}"), fake)

                self.assertEqual([], fake.commands)
                self.assertEqual(reason, result["preflight_held"]["reason"])
                self.assertEqual(before, manifest.read_bytes())

    def test_a_campaign_unit_split_while_its_headers_are_read_keeps_the_disposition_it_carries(self) -> None:
        names = ["a_DDA.mzML", "b_DIA.mzML"]
        verdicts = {name: {"method": "DIA" if "DIA" in name else "DDA"} for name in names}
        manifest, _stub, files = _unit(self.root / "unit", names, extra={"campaign_authorizations": [dict(_APPROVAL)]})
        extractor = _PinnedExtractor.make(self.root / "build")
        first = self.preflight(manifest, extractor, _Extractor(verdicts))
        self.assertEqual(("split", True), (first["campaign_disposition"]["disposition"], first["campaign_disposition"]["applied"]))
        _touch(files)
        splits: list[dict] = []

        def split_meanwhile(_command) -> None:
            if not splits:
                splits.append(split_unit_by_acquisition(manifest, confirmed=True))

        # A capped read would decide skip; the split parent must not take that decision.
        self.preflight(manifest, extractor, _Extractor(verdicts, during=split_meanwhile), max_inputs=1)
        recorded = read_manifest(manifest)

        self.assertTrue(splits and splits[0]["written"])
        self.assertEqual(("split_by_acquisition", False), (recorded["status"], recorded["execution_allowed"]))
        self.assertEqual(first["campaign_disposition"], recorded["campaign_disposition"])

    def test_a_run_that_starts_while_the_headers_are_read_keeps_its_record(self) -> None:
        manifest, _stub, files = _unit(
            self.root / "unit", ["a.mzML"], acquisition="DDA", extra={"campaign_authorizations": [dict(_APPROVAL)]}
        )
        extractor = _PinnedExtractor.make(self.root / "build")
        first = self.preflight(manifest, extractor, _Extractor({}))
        output = manifest.parent / "raw-metadata-preflight.json"
        output_before = output.read_bytes()
        _touch(files)

        def finished_meanwhile(_command) -> None:
            update_manifest(manifest, lambda current: current.update(status="mztab_validated"))

        result = self.preflight(manifest, extractor, _Extractor({}, during=finished_meanwhile))
        recorded = read_manifest(manifest)

        self.assertEqual("past_preflight", result["preflight_held"]["reason"])
        self.assertEqual("mztab_validated", recorded["status"])
        self.assertEqual(first["raw_metadata_preflight"], recorded["raw_metadata_preflight"])
        self.assertEqual(first["campaign_disposition"], recorded["campaign_disposition"])
        self.assertEqual(output_before, output.read_bytes())
        self.assertNotIn("raw_metadata_preflight_progress", recorded)


class DecidedTypeGateTests(_Scratch):
    """Under an applied disposition a file runs as the type it decided, and as no other."""

    def gate(self, manifest: Path, files: list[Path], kind: str) -> dict:
        return evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": str(manifest.parent.parent / "output"),
                "ion_mode": "Negative",
                "files": [{"file_path": str(path), "acquisition_type": kind} for path in files],
            }
        )

    def campaign(self, names: list[str], verdicts: dict, **options) -> tuple[Path, list[Path], dict]:
        manifest, _stub, files = _unit(
            self.root / "unit", names, extra={"campaign_authorizations": [dict(_APPROVAL)]}, **options
        )
        result = self.preflight(manifest, _PinnedExtractor.make(self.root / "build"), _Extractor(verdicts))
        return manifest, files, result

    def test_a_unit_run_on_its_declaration_runs_only_as_declared(self) -> None:
        # SCIEX wiff2: the extractor fails on every file, and the declaration decides.
        manifest, files, result = self.campaign(["a.wiff2", "b.wiff2"], {"a.wiff2": "fail", "b.wiff2": "fail"}, acquisition="DIA")
        self.assertEqual("SWATH", result["campaign_disposition"]["console_acquisition_type"])

        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        for kind in ("DDA", "AIF", ""):
            with self.subTest(kind=kind):
                refused = self.gate(manifest, files, kind)
                self.assertFalse(refused["allowed"])
                self.assertTrue(any("campaign disposition decided" in item for item in refused["blockers"]))

    def test_a_header_dia_file_decided_swath_does_not_run_as_aif(self) -> None:
        manifest, files, result = self.campaign(["a.mzML"], {"a.mzML": {"method": "DIA", "confidence": 0.82}}, acquisition="DIA")
        self.assertEqual("SWATH", result["campaign_disposition"]["console_acquisition_type"])

        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        self.assertFalse(self.gate(manifest, files, "AIF")["allowed"])

    def test_a_file_a_weak_header_decided_runs_only_as_its_header_gives(self) -> None:
        manifest, files, result = self.campaign(
            ["a.mzML", "b.mzML"], {name: {"method": "DIA", "confidence": 0.6} for name in ("a.mzML", "b.mzML")},
            acquisition="DDA",
        )
        self.assertEqual("SWATH", result["campaign_disposition"]["console_acquisition_type"])

        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        for kind in ("DDA", "AIF", ""):
            with self.subTest(kind=kind):
                refused = self.gate(manifest, files, kind)
                self.assertFalse(refused["allowed"])
                self.assertTrue(any("raw header contradicts" in item for item in refused["blockers"]), refused["blockers"])

    def test_a_row_that_contradicts_the_console_type_its_header_gives_is_refused_whatever_was_decided(self) -> None:
        # Rule 6 of 2026-10-06. A disposition recorded under the earlier rule kept a declared DIA over DDA headers
        # below 0.8 and decided SWATH (MTBLS1572); the gate no longer lets such a row through.
        manifest, files, _result = self.campaign(
            ["a.mzML", "b.mzML"], {name: {"method": "DDA", "confidence": 0.75} for name in ("a.mzML", "b.mzML")},
            acquisition="DIA",
        )

        def as_recorded_under_the_earlier_rule(current: dict) -> None:
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                entry.update(console_acquisition_type="SWATH", console_acquisition_basis="declaration")

        update_manifest(manifest, as_recorded_under_the_earlier_rule)
        refused = self.gate(manifest, files, "SWATH")

        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("raw header contradicts" in item and "(header gives DDA, run as SWATH)" in item for item in refused["blockers"]),
            refused["blockers"],
        )
        self.assertFalse(any("campaign disposition decided" in item for item in refused["blockers"]), refused["blockers"])

    def test_a_split_reads_the_header_first_decision_and_leaves_the_ms1_only_file_out(self) -> None:
        # Rule 5: the split machinery splits what the disposition decided, and an input it excluded is in no part.
        verdicts = {
            "a.mzML": {"method": "DDA", "confidence": 0.75},
            "b.mzML": {"method": "DIA", "confidence": 0.82},
            "full.mzML": {"method": "FullScan", "levels": [1], "confidence": 0.95},
        }
        manifest, _files, result = self.campaign(list(verdicts), verdicts, acquisition="DIA")
        self.assertEqual("split", result["campaign_disposition"]["disposition"])

        plan = split_unit_by_acquisition(manifest, confirmed=False)

        self.assertEqual([], plan["blockers"])
        self.assertEqual(
            [("DDA", ["a.mzML"]), ("DIA", ["b.mzML"])],
            [(part["acquisition_mode"], [Path(item).name for item in part["input_candidates"]]) for part in plan["parts"]],
        )
        self.assertEqual(
            [("full.mzML", "ms1_only_in_declared_dia_unit")],
            [(Path(item["path"]).name, item["reason"]) for item in plan["excluded_inputs"]],
        )

    def test_outside_a_campaign_a_file_is_held_to_its_header_as_before(self) -> None:
        manifest, stub, files = _unit(self.root / "unit", ["a.mzML"], acquisition="DIA")
        self.preflight(manifest, stub, _Extractor({"a.mzML": {"method": "DIA", "confidence": 0.82, "targets": [500.0]}}))
        update_manifest(manifest, lambda current: current.update(execution_allowed=True))

        # One recurring target settles neither SWATH nor AIF, so the header admits both.
        self.assertTrue(self.gate(manifest, files, "AIF")["allowed"])
        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        self.assertFalse(self.gate(manifest, files, "DDA")["allowed"])
        self.assertFalse(self.gate(manifest, files, "")["allowed"], "a blank type is DDA to the Console")

    def test_outside_a_campaign_a_header_that_gives_a_console_type_binds_the_row(self) -> None:
        # Recurring isolation windows make the header's DIA SWATH; AIF would ignore the windows.
        manifest, stub, files = _unit(self.root / "unit", ["a.mzML"], acquisition="DIA")
        self.preflight(manifest, stub, _Extractor({"a.mzML": {"method": "DIA", "confidence": 0.82}}))
        update_manifest(manifest, lambda current: current.update(execution_allowed=True))

        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        refused = self.gate(manifest, files, "AIF")
        self.assertFalse(refused["allowed"])
        self.assertTrue(any("(header gives SWATH, run as AIF)" in item for item in refused["blockers"]), refused["blockers"])


class LegacyDispositionGateTests(_Scratch):
    """An applied disposition decided before 0.5.29 is decided again at the gate, and refused where rule B2 differs."""

    def gate(self, manifest: Path, files: list[Path], kinds: list[str] | str) -> dict:
        kinds = [kinds] * len(files) if isinstance(kinds, str) else kinds
        return evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": str(manifest.parent.parent / "output"),
                "ion_mode": "Negative",
                "files": [{"file_path": str(path), "acquisition_type": kind} for path, kind in zip(files, kinds)],
            }
        )

    def campaign(self, verdicts: dict, **options) -> tuple[Path, dict[str, Path]]:
        manifest, _stub, files = _unit(
            self.root / "unit", list(verdicts), extra={"campaign_authorizations": [dict(_APPROVAL)]}, **options
        )
        self.preflight(manifest, _PinnedExtractor.make(self.root / "build"), _Extractor(verdicts))
        return manifest, {path.name: path for path in files}

    @staticmethod
    def as_decided_before_0529(manifest: Path, decided: dict[str, tuple[str, str]]) -> None:
        """Rewrite the applied disposition as 0.5.24-0.5.28 recorded it: these files ran as (type, basis)."""

        def rewrite(current: dict) -> None:
            disposition = current["campaign_disposition"]
            disposition.pop("declared_acquisition_source")
            disposition["excluded_inputs"] = [
                item for item in disposition["excluded_inputs"] if Path(item["path"]).name not in decided
            ]
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                if Path(entry["file"]).name in decided:
                    entry["console_acquisition_type"], entry["console_acquisition_basis"] = decided[Path(entry["file"]).name]

        update_manifest(manifest, rewrite)

    def test_a_legacy_fold_of_an_ms1_only_file_into_a_declared_dia_unit_is_refused_until_decided_again(self) -> None:
        # MTBKS217: declared DIA, z_014nn has no MS2 and was folded into the DDA run.
        verdicts = {
            "a.mzML": {"method": "DDA", "confidence": 0.75},
            "z.mzML": {"method": "FullScan", "levels": [1], "confidence": 0.95},
        }
        manifest, files = self.campaign(verdicts, acquisition="DIA")
        self.as_decided_before_0529(manifest, {"z.mzML": ("DDA", "folded_ms1_only")})

        refused = self.gate(manifest, [files["a.mzML"], files["z.mzML"]], "DDA")
        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("before Interactive 0.5.29" in item and "z.mzML (now ms1_only_in_declared_dia_unit)" in item
                and "msdial_prepare_repository_reanalysis" in item for item in refused["blockers"]),
            refused["blockers"],
        )
        self.assertTrue(self.gate(manifest, [files["a.mzML"]], "DDA")["allowed"])

        decided = classify_preflight(manifest)
        self.assertIn("declared_acquisition_source", decided)
        again = self.gate(manifest, [files["a.mzML"], files["z.mzML"]], "DDA")
        self.assertFalse(again["allowed"])
        self.assertFalse(any("before Interactive 0.5.29" in item for item in again["blockers"]), again["blockers"])
        self.assertTrue(any("excluded by this unit's campaign disposition" in item for item in again["blockers"]))

    def test_a_legacy_unknown_header_taken_at_the_declaration_is_refused(self) -> None:
        # MTBLS1572's blank: an Unknown header, run as SWATH by the declared DIA.
        verdicts = {
            "a.mzML": {"method": "DIA", "confidence": 0.82},
            "blank.mzML": {"method": "Unknown", "confidence": 0.3},
        }
        manifest, files = self.campaign(verdicts, acquisition="DIA")
        self.as_decided_before_0529(manifest, {"blank.mzML": ("SWATH", "declaration")})

        refused = self.gate(manifest, [files["blank.mzML"]], "SWATH")
        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("blank.mzML (now acquisition_unresolved)" in item for item in refused["blockers"]), refused["blockers"]
        )
        self.assertTrue(self.gate(manifest, [files["a.mzML"]], "SWATH")["allowed"])

    def test_a_legacy_disposition_whose_unit_would_no_longer_run_is_refused(self) -> None:
        verdicts = {"blank.mzML": {"method": "Unknown", "confidence": 0.3}, "b.mzML": {"method": "Unknown", "confidence": 0.3}}
        manifest, files = self.campaign(verdicts, acquisition="DIA")
        self.assertEqual("skip", read_manifest(manifest)["campaign_disposition"]["disposition"])

        def ran_by_declaration(current: dict) -> None:
            disposition = current["campaign_disposition"]
            disposition.pop("declared_acquisition_source")
            disposition.update(disposition="run", reasons=[], excluded_inputs=[], console_acquisition_type="SWATH")
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                entry.update(console_acquisition_type="SWATH", console_acquisition_basis="declaration")
            current.update(execution_allowed=True, status="preflight_passed")

        update_manifest(manifest, ran_by_declaration)
        refused = self.gate(manifest, list(files.values()), "SWATH")

        self.assertFalse(refused["allowed"])
        self.assertTrue(
            any("the unit would skip (acquisition_unresolved), not run" in item for item in refused["blockers"]),
            refused["blockers"],
        )

    def test_a_dia_header_with_no_recorded_target_does_not_refuse_a_swath_row_its_disposition_decided(self) -> None:
        # The extractor records isolation targets only from MS2 headers that carry a precursor m/z, so none
        # recorded is no evidence of all-ion acquisition; the decided type binds the row, not that reading.
        verdicts = {"a.mzML": {"method": "DIA", "confidence": 0.82, "targets": []}}
        manifest, files = self.campaign(verdicts, acquisition="DIA")
        self.assertEqual("AIF", read_manifest(manifest)["campaign_disposition"]["console_acquisition_type"])

        refused = self.gate(manifest, [files["a.mzML"]], "SWATH")
        self.assertFalse(refused["allowed"])
        self.assertFalse(any("raw header contradicts" in item for item in refused["blockers"]), refused["blockers"])
        self.assertTrue(any("campaign disposition decided" in item for item in refused["blockers"]))

        self.as_decided_before_0529(manifest, {"a.mzML": ("SWATH", "declaration")})
        self.assertTrue(self.gate(manifest, [files["a.mzML"]], "SWATH")["allowed"], "the legacy SWATH runs")
        self.assertFalse(self.gate(manifest, [files["a.mzML"]], "DDA")["allowed"])

    def test_outside_a_campaign_a_dia_header_with_no_recorded_target_admits_swath_or_aif(self) -> None:
        manifest, stub, files = _unit(self.root / "unit", ["a.mzML"], acquisition="DIA")
        self.preflight(manifest, stub, _Extractor({"a.mzML": {"method": "DIA", "confidence": 0.82, "targets": []}}))
        update_manifest(manifest, lambda current: current.update(execution_allowed=True))

        self.assertTrue(self.gate(manifest, files, "SWATH")["allowed"])
        self.assertTrue(self.gate(manifest, files, "AIF")["allowed"])
        self.assertFalse(self.gate(manifest, files, "DDA")["allowed"])

    def test_a_split_records_its_parents_declaration_and_a_part_split_before_is_held_to_it(self) -> None:
        verdicts = {"a.mzML": {"method": "DDA", "confidence": 0.75}, "b.mzML": {"method": "DIA", "confidence": 0.82}}
        manifest, extractor, _files = _unit(self.root / "unit", list(verdicts), acquisition="DIA")
        self.preflight(manifest, extractor, _Extractor(verdicts))
        split = split_unit_by_acquisition(manifest, confirmed=True)
        part = next(Path(item["manifest_path"]) for item in split["parts"] if item["acquisition_mode"] == "DDA")
        self.assertEqual("DIA", read_manifest(part)["split_from"]["parent_declared_acquisition_mode"])

        # As a part split under the earlier rule: no recorded parent declaration, and an MS1-only input folded in.
        full = self.root / "unit" / "raw" / "data" / "full.mzML"
        full.write_bytes(b"x")

        def split_before_0529(current: dict) -> None:
            current["split_from"].pop("parent_declared_acquisition_mode")
            current["input_candidates"].append(str(full))

        update_manifest(part, split_before_0529)
        verdicts["full.mzML"] = {"method": "FullScan", "levels": [1], "confidence": 0.95}
        result = self.preflight(part, extractor, _Extractor(verdicts))
        disposition = result["campaign_disposition"]

        self.assertEqual("DIA", disposition["split_parent_declared_acquisition_mode"])
        self.assertEqual(
            [("full.mzML", "ms1_only_in_declared_dia_unit")],
            [(Path(item["path"]).name, item["reason"]) for item in disposition["excluded_inputs"]],
        )


class LegacyDispositionPrepareTests(_Scratch):
    """Preparing a unit decides a pre-0.5.29 disposition again; a unit past its run only for a new production run."""

    gate = LegacyDispositionGateTests.gate
    campaign = LegacyDispositionGateTests.campaign
    as_decided_before_0529 = staticmethod(LegacyDispositionGateTests.as_decided_before_0529)
    MTBKS217 = {
        "a.mzML": {"method": "DDA", "confidence": 0.75},
        "z.mzML": {"method": "FullScan", "levels": [1], "confidence": 0.95},
    }

    def finished_legacy_unit(self, verdicts: dict, decided: dict[str, tuple[str, str]], status: str = "mztab_validated"):
        manifest, files = self.campaign(verdicts, acquisition="DIA")
        self.as_decided_before_0529(manifest, decided)

        def finished(current: dict) -> None:
            # As MTBKS217 and MTBLS1572 are recorded: their runs finished and validated under the old disposition,
            # with a lineage row per input as every lease since 0.5.9 writes it.
            current.update(status=status, execution_allowed=True)
            current["input_lineage"] = {
                "schema": "msdial-input-lineage.v1",
                "rows": [
                    {"path": str(path), "kind": "file", "sample_id": path.stem, "file_name": "", "source": {}, "checksums": {}}
                    for path in files.values()
                ],
            }

        update_manifest(manifest, finished)
        return manifest, files

    def with_finished_run_outputs(self, manifest: Path, files: dict[str, Path]) -> dict:
        """The finished run's files and records, as finalize_download_lease leaves them: the CSV it read, the
        per-file containers, a validated mzTab-M, and the inventory of their checksums."""
        import hashlib

        output = Path(read_manifest(manifest)["output_directory"])
        output.mkdir(parents=True, exist_ok=True)
        csv_path = output / "analysis_files.csv"
        csv_path.write_text(
            "file_path,file_name,acquisition_type\n" + "".join(f"{path},{path.stem},DDA\n" for path in files.values()),
            encoding="utf-8",
        )
        for path in files.values():
            (output / f"{path.stem}.mdpeak").write_bytes(b"peaks")
        (output / "result.mzTab.txt").write_text("MTD\tmzTab-version\t2.0.0-M\n", encoding="utf-8")
        retained = [csv_path, *sorted(output.glob("*.mdpeak")), output / "result.mzTab.txt"]
        inventory = [
            {"path": str(path), "size_bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in retained
        ]

        def finalised(current: dict) -> None:
            current.update(
                cleanup_allowed=True,
                finalized_at="2026-10-01T00:00:00+00:00",
                finalized_run={"job_id": "run-1", "run_directory": str(output), "artifacts": {"mztab": [], "qa": []}},
                mztab_validation={"summary": {"failed": 0, "passed": 1}},
                retained_artifacts=[str(path) for path in retained],
                retained_artifact_inventory=inventory,
                analysis_csv={"status": "written", "path": str(csv_path), "rows": len(files)},
            )
            for row in (current.get("input_lineage") or {}).get("rows") or []:
                row.update(file_name=Path(row["path"]).stem, console_path=row["path"], acquisition_type="DDA")

        update_manifest(manifest, finalised)
        return {"output": output, "csv": csv_path, "inventory": inventory, "bytes": {p: p.read_bytes() for p in retained}}

    def prepare(self, manifest: Path, confirmed: bool, **options) -> dict:
        with patch.dict(os.environ, {"LOCALAPPDATA": str(self.root / "config"), "APPDATA": str(self.root / "config")}):
            from msdial_app import mcp_server

            with patch.object(mcp_server, "_request_json", side_effect=AssertionError("no backend")):
                return mcp_server.msdial_prepare_repository_reanalysis(
                    hierarchy=[], confirmed=confirmed, manifest_path=str(manifest), **options
                )

    @staticmethod
    def csv_rows(prepared: dict) -> list[dict]:
        import csv

        with open(prepared["input_path"], encoding="utf-8-sig", newline="") as handle:
            return list(csv.DictReader(handle))

    @staticmethod
    def redecide(manifest: Path) -> dict:
        from msdial_app.repository_reanalysis import redecide_legacy_disposition

        record, _view = redecide_legacy_disposition({**read_manifest(manifest), "manifest_path": str(manifest)}, write=True)
        return record

    def gate_at(self, manifest: Path, files: list[Path], kinds: list[str] | str, output_root: Path) -> dict:
        kinds = [kinds] * len(files) if isinstance(kinds, str) else kinds
        return evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": str(output_root),
                "ion_mode": "Negative",
                "files": [{"file_path": str(path), "acquisition_type": kind} for path, kind in zip(files, kinds)],
            }
        )

    def open_run_attempt(self, manifest: Path) -> None:
        from msdial_app.repository_reanalysis import process_created_at

        def opened(current: dict) -> None:
            # A rerun whose Console is still running: record_run_start appends this and leaves the status alone.
            current["run_attempts"] = [
                *(current.get("run_attempts") or []),
                {
                    "attempt_id": "rerun", "kind": "run", "job_id": "rerun", "started_at": "2026-10-07T00:00:00+00:00",
                    "ended_at": None, "console_pid": None,
                    "backend": {"pid": os.getpid(), "process_created_at": process_created_at()},
                },
            ]

        update_manifest(manifest, opened)

    def test_a_finished_unit_is_refused_without_new_run_and_nothing_of_its_run_is_written(self) -> None:
        # Review r5-62, medium: preparing MTBKS217 rewrote output\analysis_files.csv, the CSV its run read, while
        # the unit stayed mztab_validated and cleanup-allowed.
        for status in ("mztab_validated", "completed", "cleanup_pending_confirmation", "validation_failed"):
            with self.subTest(status=status):
                self.root = Path(tempfile.mkdtemp(dir=self._directory.name)).resolve()
                manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")}, status)
                run = self.with_finished_run_outputs(manifest, files)
                before = manifest.read_bytes()

                for confirmed in (False, True):
                    refused = self.prepare(manifest, confirmed=confirmed)
                    self.assertEqual(
                        (False, False, "run_finished"), (refused["ok"], refused["prepared"], refused["reason"]), refused
                    )
                    self.assertIn("new_run=true", refused["next_step"])
                    self.assertNotIn("preview", refused)
                self.assertEqual(before, manifest.read_bytes(), "the manifest is not changed")
                self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]}, "nor any file of the run")
                self.assertEqual([run["output"]], [p for p in manifest.parent.parent.iterdir() if p.name.startswith("output")])
                # Nothing decides it again in place either.
                self.assertIsNotNone(self.redecide(manifest)["held"])
                self.assertEqual(before, manifest.read_bytes())

    def test_a_new_run_for_a_finished_legacy_fold_keeps_the_finished_run_and_writes_its_csv_elsewhere(self) -> None:
        # MTBKS217: mztab_validated, declared DIA, z_014nn (MS1 only) folded into the DDA run before 0.5.29.
        from msdial_app.repository_reanalysis import plan_download_cleanup, refresh_retained_artifacts

        manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")})
        run = self.with_finished_run_outputs(manifest, files)
        finished = read_manifest(manifest)
        both = [files["a.mzML"], files["z.mzML"]]
        refused = self.gate(manifest, both, "DDA")
        self.assertTrue(
            any("before Interactive 0.5.29" in item and "new_run=true" in item for item in refused["blockers"]),
            refused["blockers"],
        )
        self.assertEqual("past_preflight", classify_preflight(manifest)["held"]["reason"])
        before = manifest.read_bytes()
        new_output = manifest.parent.parent / "output-run-2"

        preview = self.prepare(manifest, confirmed=False, new_run=True)
        self.assertFalse(preview["prepared"])
        new_run = preview["preview"]["new_run"]
        self.assertEqual(
            (True, False, str(new_output), "mztab_validated", str(run["output"])),
            (new_run["started"], new_run["written"], new_run["output_directory"], new_run["previous_status"],
             new_run["previous_output_directory"]),
        )
        self.assertEqual(str(new_output), preview["preview"]["output_root"])
        redecision = preview["preview"]["legacy_disposition_redecision"]
        self.assertEqual(
            (True, False, "run", "DDA", "mztab_validated", "preflight_passed"),
            (redecision["redecided"], redecision["written"], redecision["disposition"],
             redecision["console_acquisition_type"], redecision["status"], redecision["status_after"]),
        )
        self.assertEqual([{"file": "z.mzML", "reason": "ms1_only_in_declared_dia_unit"}], redecision["excluded_inputs"])
        self.assertEqual(before, manifest.read_bytes(), "a preview writes nothing")
        self.assertFalse(new_output.exists(), "nor creates the new run's folder")

        prepared = self.prepare(manifest, confirmed=True, new_run=True)
        self.assertTrue(prepared["prepared"], prepared)
        self.assertTrue(prepared["preview"]["new_run"]["written"])
        self.assertTrue(prepared["preview"]["legacy_disposition_redecision"]["written"])
        self.assertEqual(new_output / "analysis_files.csv", Path(prepared["input_path"]))
        self.assertEqual([("a", "DDA")], [(row["file_name"], row["acquisition_type"]) for row in self.csv_rows(prepared)])
        # The finished run's files are as it left them.
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})

        recorded = read_manifest(manifest)
        self.assertEqual(
            ("preflight_passed", False, True, str(new_output)),
            (recorded["status"], recorded["cleanup_allowed"], recorded["execution_allowed"], recorded["output_directory"]),
        )
        for key in ("finalized_at", "finalized_run", "mztab_validation", "retained_artifacts", "retained_artifact_inventory"):
            self.assertNotIn(key, recorded, f"{key} was the finished run's")
        [superseded] = recorded["superseded_runs"]
        for key in ("status", "cleanup_allowed", "execution_allowed", "output_directory", "finalized_at", "finalized_run",
                    "mztab_validation", "retained_artifacts", "retained_artifact_inventory", "campaign_disposition", "project"):
            self.assertEqual(finished[key], superseded[key], key)
        self.assertEqual(finished["analysis_csv"], superseded["analysis_csv"])
        self.assertEqual(
            [(row["path"], row["file_name"], row["acquisition_type"]) for row in finished["input_lineage"]["rows"]],
            [(row["path"], row["file_name"], row["acquisition_type"]) for row in superseded["input_lineage_written"]],
        )
        self.assertIn(
            {"file": str(files["z.mzML"]), "console_acquisition_type": "DDA", "console_acquisition_basis": "folded_ms1_only"},
            superseded["preflight_per_file"],
        )
        disposition = recorded["campaign_disposition"]
        self.assertIn("declared_acquisition_source", disposition)
        self.assertEqual("msdial_prepare_repository_reanalysis", disposition["redecided"]["by"])
        self.assertNotIn("declared_acquisition_source", disposition["supersedes"])

        # The new run's records describe the new run only, and the raw data are not released on the old run's word.
        self.assertFalse(refresh_retained_artifacts(manifest)["refreshed"])
        self.assertTrue(plan_download_cleanup(manifest)["blockers"])
        rows = self.csv_rows(prepared)
        paths, kinds = [Path(row["file_path"]) for row in rows], [row["acquisition_type"] for row in rows]
        allowed = self.gate_at(manifest, paths, kinds, new_output)
        self.assertTrue(allowed["allowed"], allowed["blockers"])
        into_old = self.gate_at(manifest, paths, kinds, run["output"])
        self.assertTrue(any("this unit's manifest owns" in item for item in into_old["blockers"]), into_old["blockers"])
        still = self.gate_at(manifest, both, "DDA", new_output)
        self.assertFalse(any("before Interactive 0.5.29" in item for item in still["blockers"]), still["blockers"])
        self.assertTrue(any("excluded by this unit's campaign disposition" in item for item in still["blockers"]))

        # Prepared again before it runs, the unit is not past a run: the CSV is rewritten in the new folder only.
        again = self.prepare(manifest, confirmed=True)
        self.assertTrue(again["prepared"], again)
        self.assertEqual(new_output / "analysis_files.csv", Path(again["input_path"]))
        self.assertNotIn("legacy_disposition_redecision", again["preview"])
        self.assertEqual(superseded, read_manifest(manifest)["superseded_runs"][0])

        # Once that run has finished too, another new run takes the next folder and keeps both records.
        update_manifest(manifest, lambda current: current.update(status="mztab_validated", cleanup_allowed=True,
                                                                 finalized_at="2026-10-08T00:00:00+00:00"))
        third = self.prepare(manifest, confirmed=True, new_run=True)
        self.assertTrue(third["prepared"], third)
        self.assertEqual(manifest.parent.parent / "output-run-3" / "analysis_files.csv", Path(third["input_path"]))
        self.assertNotIn("legacy_disposition_redecision", third["preview"])
        recorded = read_manifest(manifest)
        self.assertEqual(superseded, recorded["superseded_runs"][0])
        self.assertEqual(
            [str(run["output"]), str(new_output)], [item["output_directory"] for item in recorded["superseded_runs"]]
        )
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})

    def test_a_finished_unit_with_an_open_run_attempt_is_not_decided_again_nor_given_a_new_run(self) -> None:
        # Review r5-62, low: disposition_hold says past_preflight before it looks for a live attempt, and the
        # finished unit was decided again (and its CSV rewritten) while its rerun's Console was running.
        from msdial_app.repository_reanalysis import _live_run_attempt_in

        manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")})
        run = self.with_finished_run_outputs(manifest, files)
        self.open_run_attempt(manifest)
        self.assertIsNotNone(_live_run_attempt_in(read_manifest(manifest)))
        before = manifest.read_bytes()

        record = self.redecide(manifest)
        self.assertEqual((False, False), (record["redecided"], record["written"]))
        self.assertIsNotNone(record["held"])
        self.assertEqual("run_finished", self.prepare(manifest, confirmed=True)["reason"])
        for confirmed in (False, True):
            refused = self.prepare(manifest, confirmed=confirmed, new_run=True)
            self.assertEqual(
                (False, False, "run_in_progress", "rerun"),
                (refused["ok"], refused["prepared"], refused["reason"], refused["new_run"]["job_id"]),
            )
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        self.assertFalse((manifest.parent.parent / "output-run-2").exists())

    def test_a_new_run_the_unit_would_no_longer_get_is_not_prepared(self) -> None:
        # MTBLS1572's shape: every header Unknown, run as SWATH by the declaration; decided again it skips.
        verdicts = {"blank.mzML": {"method": "Unknown", "confidence": 0.3}, "b.mzML": {"method": "Unknown", "confidence": 0.3}}
        manifest, files = self.campaign(verdicts, acquisition="DIA")

        def ran_by_declaration(current: dict) -> None:
            disposition = current["campaign_disposition"]
            disposition.pop("declared_acquisition_source")
            disposition.update(disposition="run", reasons=[], excluded_inputs=[], console_acquisition_type="SWATH")
            for entry in current["raw_metadata_preflight"]["summary"]["per_file"]:
                entry.update(console_acquisition_type="SWATH", console_acquisition_basis="declaration")
            current.update(execution_allowed=True, status="cleanup_pending_confirmation", cleanup_allowed=True)

        update_manifest(manifest, ran_by_declaration)
        before = manifest.read_bytes()

        self.assertIsNotNone(self.redecide(manifest)["held"])
        refused = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, "would_not_run"), (refused["prepared"], refused["reason"]))
        redecision = refused["new_run"]["legacy_disposition_redecision"]
        self.assertEqual(("skip", ["acquisition_unresolved"], False), (redecision["disposition"], redecision["reasons"], redecision["written"]))
        self.assertEqual(before, manifest.read_bytes(), "the finished run stays as it was recorded")
        self.assertFalse((manifest.parent.parent / "output-run-2").exists())
        gated = self.gate(manifest, list(files.values()), "SWATH")
        self.assertTrue(
            any("before Interactive 0.5.29" in item and "new_run=true" in item for item in gated["blockers"]), gated["blockers"]
        )

    def test_a_legacy_unit_whose_raw_data_were_released_is_never_prepared(self) -> None:
        for status in ("raw_cleaned", "discarded"):
            with self.subTest(status=status):
                self.root = Path(tempfile.mkdtemp(dir=self._directory.name)).resolve()
                manifest, _files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")}, status)
                before = manifest.read_bytes()

                record = self.redecide(manifest)
                self.assertEqual((False, False, "past_preflight"), (record["redecided"], record["written"], record["held"]["reason"]))
                for new_run in (False, True):
                    refused = self.prepare(manifest, confirmed=True, new_run=new_run)
                    self.assertEqual((False, "raw_released"), (refused["prepared"], refused["reason"]))
                self.assertEqual(before, manifest.read_bytes())
                self.assertFalse((manifest.parent.parent / "output-run-2").exists())

    def test_a_legacy_unit_that_cannot_be_decided_again_is_left_as_it_is_and_still_refused(self) -> None:
        manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")}, status="preflight_passed")
        before = manifest.read_bytes()

        with patch("msdial_app.repository_reanalysis._decide_recorded_preflight", side_effect=KeyError("per_file")):
            record = self.redecide(manifest)

        self.assertEqual((False, False), (record["redecided"], record["written"]))
        self.assertIn("KeyError", record["error"])
        self.assertEqual(before, manifest.read_bytes())
        refused = self.gate(manifest, [files["a.mzML"], files["z.mzML"]], "DDA")
        self.assertTrue(any("before Interactive 0.5.29" in item for item in refused["blockers"]), refused["blockers"])

    def test_a_new_run_whose_decision_fails_is_not_prepared_and_the_finished_run_stands(self) -> None:
        manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")})
        run = self.with_finished_run_outputs(manifest, files)
        before = manifest.read_bytes()

        with patch("msdial_app.repository_reanalysis._decide_recorded_preflight", side_effect=KeyError("per_file")):
            refused = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, "redecision_failed"), (refused["prepared"], refused["reason"]))
        self.assertIn("KeyError", refused["detail"])
        self.assertEqual(before, manifest.read_bytes())
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        self.assertFalse((manifest.parent.parent / "output-run-2").exists())

    def test_a_new_run_for_a_finished_unit_decided_under_rule_b2_moves_only_the_run(self) -> None:
        manifest, files = self.campaign(self.MTBKS217, acquisition="DIA")
        update_manifest(manifest, lambda current: current.update(
            status="mztab_validated", execution_allowed=True,
            input_lineage={"schema": "msdial-input-lineage.v1", "rows": [
                {"path": str(path), "kind": "file", "sample_id": path.stem, "file_name": "", "source": {}, "checksums": {}}
                for path in files.values()
            ]},
        ))
        run = self.with_finished_run_outputs(manifest, files)
        disposition = read_manifest(manifest)["campaign_disposition"]

        prepared = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertTrue(prepared["prepared"], prepared)
        self.assertNotIn("legacy_disposition_redecision", prepared["preview"])
        recorded = read_manifest(manifest)
        self.assertEqual(disposition, recorded["campaign_disposition"], "a current disposition is not decided again")
        self.assertEqual(("preflight_passed", False), (recorded["status"], recorded["cleanup_allowed"]))
        self.assertEqual(run["inventory"], recorded["superseded_runs"][0]["retained_artifact_inventory"])
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})

    def test_an_agent_can_tell_a_prepare_keeps_a_finished_run(self) -> None:
        from msdial_app.agent_bridge import summarize_jobs

        self.assertIn("repository_prepare_new_production_run", summarize_jobs({})["capabilities"])

    def test_new_run_for_a_unit_not_past_a_run_prepares_it_as_any_other(self) -> None:
        manifest, _files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")}, status="preflight_passed")

        prepared = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertTrue(prepared["prepared"], prepared)
        self.assertEqual({"started": False, "reason": "no_finished_run", "written": False}, prepared["preview"]["new_run"])
        self.assertEqual(manifest.parent.parent / "output" / "analysis_files.csv", Path(prepared["input_path"]))
        self.assertNotIn("superseded_runs", read_manifest(manifest))

    def test_a_legacy_unit_not_yet_run_is_decided_again_as_classify_preflight_would(self) -> None:
        manifest, _files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")}, status="preflight_passed")

        prepared = self.prepare(manifest, confirmed=True)

        redecision = prepared["preview"]["legacy_disposition_redecision"]
        self.assertEqual((True, True, "preflight_passed"), (redecision["redecided"], redecision["written"], redecision["status_after"]))
        recorded = read_manifest(manifest)
        self.assertEqual("preflight_passed", recorded["status"])
        self.assertNotIn("superseded_runs", recorded)
        self.assertEqual(
            [("z.mzML", "ms1_only_in_declared_dia_unit")],
            [(Path(item["path"]).name, item["reason"]) for item in recorded["campaign_disposition"]["excluded_inputs"]],
        )
        rows = self.csv_rows(prepared)
        self.assertTrue(self.gate(manifest, [Path(row["file_path"]) for row in rows], "DDA")["allowed"])


    # Review r6-62, medium: a confirmed new_run prepare superseded the finished run before its CSV was known to be
    # writable. Each later step that fails must leave the manifest and every file byte for byte as they were.

    @staticmethod
    def tree(root: Path) -> dict:
        """Every folder and every file's bytes under root: what a failed new run must leave as it found."""
        found: dict = {}
        for path in sorted(root.rglob("*")):
            key = str(path.relative_to(root))
            found[key] = None if path.is_dir() else path.read_bytes()
        return found

    def assert_untouched(self, manifest: Path, before: dict, run: dict) -> None:
        workspace = manifest.parent.parent
        self.assertEqual(before, self.tree(workspace), "the manifest and every file are as they were")
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        self.assertEqual([], [p.name for p in workspace.iterdir() if p.name.startswith((".output", "output-run"))])

    def finished_mtbks217(self) -> tuple[Path, dict, dict, dict]:
        manifest, files = self.finished_legacy_unit(self.MTBKS217, {"z.mzML": ("DDA", "folded_ms1_only")})
        run = self.with_finished_run_outputs(manifest, files)
        return manifest, files, run, self.tree(manifest.parent.parent)

    def assert_finished_run_still_judged(self, manifest: Path) -> None:
        """Cleanup and discard judge the unit by its finished run, as before the prepare that failed."""
        from msdial_app.repository_reanalysis import discard_download_lease, plan_download_cleanup, plan_download_discard

        cleanup = plan_download_cleanup(manifest)
        self.assertEqual(([], True), (cleanup["blockers"], cleanup["ready_for_confirmation"]))
        self.assertIn("Validated/completed runs must use the normal cleanup command.", plan_download_discard(manifest)["blockers"])
        with self.assertRaises(ValueError):
            discard_download_lease(manifest, confirmed=True)

    def test_a_new_run_whose_rows_cannot_be_built_changes_nothing(self) -> None:
        manifest, _files, run, before = self.finished_mtbks217()

        with patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows", side_effect=RuntimeError("rows")):
            with self.assertRaisesRegex(RuntimeError, "rows"):
                self.prepare(manifest, confirmed=True, new_run=True)

        self.assert_untouched(manifest, before, run)
        self.assert_finished_run_still_judged(manifest)

    def test_a_new_run_whose_rows_disagree_changes_nothing_and_records_no_failure(self) -> None:
        from msdial_app import repository_analysis_rows

        manifest, _files, run, before = self.finished_mtbks217()
        real = repository_analysis_rows.build_repository_analysis_rows

        def disagreeing(manifest_view, projected):
            built = real(manifest_view, projected)
            built["failures"] = [*built["failures"], {"code": "lineage_disagrees", "message": "The lineage disagrees.", "inputs": []}]
            return built

        with patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows", side_effect=disagreeing):
            failed = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, False, "analysis_csv_failed", ["lineage_disagrees"]),
                         (failed["ok"], failed["prepared"], failed["reason"], failed["codes"]))
        self.assertFalse(failed["analysis_csv"]["written_to_manifest"])
        self.assertFalse(failed["preview"]["new_run"]["written"])
        self.assert_untouched(manifest, before, run)
        self.assert_finished_run_still_judged(manifest)

    def test_a_new_run_whose_alias_fails_takes_back_the_aliases_it_made(self) -> None:
        from msdial_app import repository_analysis_rows

        real = repository_analysis_rows.create_console_aliases
        for outcome in ("returns_a_failure", "raises"):
            with self.subTest(outcome=outcome):
                self.root = Path(tempfile.mkdtemp(dir=self._directory.name)).resolve()
                manifest, files, run, before = self.finished_mtbks217()
                aliases = manifest.parent.parent / "raw" / "console-aliases"
                made_here: list = []

                def one_made_then_failed(built, made=None):
                    # A real alias, in a folder this call creates, before the one that fails.
                    alias = {"path": str(aliases / "alias-a.mzML"), "target": str(files["a.mzML"]), "kind": "hardlink"}
                    self.assertEqual([], real({"rows": [{"console_alias": alias}]}, made=made))
                    made_here.extend(made)
                    self.assertTrue((aliases / "alias-a.mzML").is_file())
                    if outcome == "raises":
                        raise OSError("the volume refused the link")
                    return [{"code": "console_alias_failed", "message": "No ASCII-safe alias could be made for z.mzML.",
                             "inputs": ["z.mzML"]}]

                with patch("msdial_app.repository_analysis_rows.create_console_aliases", side_effect=one_made_then_failed):
                    if outcome == "raises":
                        # The tool returns an OSError as os_error rather than raising past the MCP boundary.
                        failed = self.prepare(manifest, confirmed=True, new_run=True)
                        self.assertEqual((False, "os_error"), (failed["ok"], failed["reason"]))
                        self.assertIn("refused the link", failed["detail"])
                    else:
                        failed = self.prepare(manifest, confirmed=True, new_run=True)
                        self.assertEqual(("analysis_csv_failed", ["console_alias_failed"]), (failed["reason"], failed["codes"]))

                self.assertEqual(["directory", "hardlink"], [kind for kind, _link, _target in made_here])
                self.assertFalse(aliases.exists(), "the alias and the folder made for it are taken back")
                self.assertEqual(b"x", files["a.mzML"].read_bytes(), "the input itself stays")
                self.assert_untouched(manifest, before, run)

    def test_a_new_run_whose_csv_cannot_be_written_removes_its_staging(self) -> None:
        from msdial_app import repository_analysis_rows

        manifest, _files, run, before = self.finished_mtbks217()
        real = repository_analysis_rows.write_analysis_csv
        staged: list = []

        def written_then_failed(built, path):
            staged.append(real(built, path))
            raise OSError("the disk filled")

        with patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=written_then_failed):
            failed = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, "os_error", "the disk filled"), (failed["ok"], failed["reason"], failed["detail"]))

        [csv_path] = staged
        self.assertTrue(csv_path.parent.name.startswith(".output-run-2.") and csv_path.parent.name.endswith(".staging"))
        self.assertEqual(manifest.parent.parent, csv_path.parent.parent)
        self.assertFalse(csv_path.parent.exists())
        self.assert_untouched(manifest, before, run)
        self.assert_finished_run_still_judged(manifest)

    def test_a_new_run_is_not_committed_over_a_manifest_another_writer_changed(self) -> None:
        from msdial_app import repository_analysis_rows

        manifest, _files, run, _before = self.finished_mtbks217()
        real = repository_analysis_rows.write_analysis_csv

        def written_while_another_writer_writes(built, path):
            written = real(built, path)
            update_manifest(manifest, lambda current: current.update(written_meanwhile="kept"))
            return written

        with patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=written_while_another_writer_writes):
            refused = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, False, "new_run_conflict"), (refused["ok"], refused["prepared"], refused["reason"]))
        self.assertFalse(refused["new_run"]["written"])
        recorded = read_manifest(manifest)
        self.assertEqual(("kept", "mztab_validated", True), (recorded["written_meanwhile"], recorded["status"], recorded["cleanup_allowed"]))
        self.assertNotIn("superseded_runs", recorded)
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        workspace = manifest.parent.parent
        self.assertEqual([], [p.name for p in workspace.iterdir() if p.name.startswith((".output", "output-run"))])

    def finished_unit_without_lineage(self) -> tuple[Path, dict, dict, dict]:
        manifest, files = self.campaign(self.MTBKS217, acquisition="DIA")
        update_manifest(manifest, lambda current: current.update(status="mztab_validated", execution_allowed=True))
        run = self.with_finished_run_outputs(manifest, files)
        self.assertNotIn("input_lineage", read_manifest(manifest))
        return manifest, files, run, self.tree(manifest.parent.parent)

    def test_a_new_run_of_a_unit_without_lineage_whose_files_do_not_map_changes_nothing(self) -> None:
        from msdial_app import repository_metadata

        manifest, _files, run, before = self.finished_unit_without_lineage()
        real = repository_metadata.apply_classes_to_analysis_files
        for field in ("unmatched", "ambiguous"):
            with self.subTest(field=field):

                def not_mapped(projected, recognized):
                    return {**real(projected, recognized), field: ["FILES/a.mzML"]}

                with patch("msdial_app.repository_metadata.apply_classes_to_analysis_files", side_effect=not_mapped):
                    preview = self.prepare(manifest, confirmed=False, new_run=True)
                    self.assertTrue(preview["preview"]["new_run"]["started"])
                    with self.assertRaisesRegex(RuntimeError, "did not map uniquely"):
                        self.prepare(manifest, confirmed=True, new_run=True)

                self.assert_untouched(manifest, before, run)
        self.assert_finished_run_still_judged(manifest)

    def test_a_new_run_of_a_unit_without_lineage_is_committed_with_its_csv(self) -> None:
        manifest, _files, run, _before = self.finished_unit_without_lineage()
        finished = read_manifest(manifest)

        prepared = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertTrue(prepared["prepared"], prepared)
        new_output = manifest.parent.parent / "output-run-2"
        self.assertEqual(new_output / "analysis_files.csv", Path(prepared["input_path"]))
        self.assertTrue(Path(prepared["input_path"]).is_file())
        self.assertTrue(Path(prepared["files"]["metadata_json"]).is_file())
        self.assertEqual(new_output, Path(prepared["files"]["metadata_json"]).parent)
        self.assertTrue(prepared["preview"]["new_run"]["written"])
        recorded = read_manifest(manifest)
        self.assertEqual((str(new_output), "preflight_passed", False), (recorded["output_directory"], recorded["status"], recorded["cleanup_allowed"]))
        self.assertEqual(finished["analytical_order"] if "analytical_order" in finished else None,
                         recorded["superseded_runs"][0].get("analytical_order"))
        self.assertIn("recorded_at", recorded["analytical_order"], "the new CSV's order is written with the run")
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        self.assertEqual([], [p.name for p in manifest.parent.parent.iterdir() if p.name.startswith(".output")])

    def test_a_confirmed_new_run_writes_the_manifest_once(self) -> None:
        from msdial_app import repository_reanalysis

        manifest, _files, run, _before = self.finished_mtbks217()
        real = repository_reanalysis._write_json
        writes: list = []

        def counted(path, value):
            if Path(path).resolve() == manifest.resolve():
                writes.append(dict(value))
            return real(path, value)

        with patch("msdial_app.repository_reanalysis._write_json", side_effect=counted):
            prepared = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertTrue(prepared["prepared"], prepared)
        [written] = writes
        self.assertEqual(("preflight_passed", "written"), (written["status"], written["analysis_csv"]["status"]))
        self.assertEqual(str(manifest.parent.parent / "output-run-2" / "analysis_files.csv"), written["analysis_csv"]["path"])
        self.assertTrue(written["superseded_runs"])

    def test_raw_data_of_a_unit_whose_new_run_has_not_validated_are_neither_cleaned_nor_discarded(self) -> None:
        # Cleanup and discard judge the unit by its current run. While a new run prepared after a validated run
        # has not validated (prepared, or failed), the raw data are kept for it, and each refusal says why.
        from msdial_app.repository_reanalysis import (
            cleanup_download_lease,
            discard_download_lease,
            plan_download_cleanup,
            plan_download_discard,
            superseded_validated_run,
        )

        manifest, files, run, _before = self.finished_mtbks217()
        self.assertIsNone(superseded_validated_run(read_manifest(manifest)))
        self.assertTrue(self.prepare(manifest, confirmed=True, new_run=True)["prepared"])
        for status in ("preflight_passed", "run_failed"):
            with self.subTest(status=status):
                update_manifest(manifest, lambda current: current.update(status=status))
                found = superseded_validated_run(read_manifest(manifest))
                self.assertEqual((0, "mztab_validated", str(run["output"])),
                                 (found["superseded_run"], found["status"], found["output_directory"]))
                discard = plan_download_discard(manifest)
                self.assertTrue(any("A superseded run of this unit validated" in item for item in discard["blockers"]), discard)
                with self.assertRaisesRegex(ValueError, "superseded run of this unit validated"):
                    discard_download_lease(manifest, confirmed=True)
                cleanup = plan_download_cleanup(manifest)
                self.assertFalse(cleanup["ready_for_confirmation"])
                self.assertTrue(any("A superseded run of this unit validated" in item for item in cleanup["blockers"]), cleanup)
                with self.assertRaises(ValueError):
                    cleanup_download_lease(manifest, confirmed=True)
                self.assertEqual(b"x", files["a.mzML"].read_bytes(), "nothing was deleted")
                self.assertEqual(status, read_manifest(manifest)["status"])

        # Once the new run validates, cleanup judges it as any other, and the superseded run no longer holds it.
        update_manifest(manifest, lambda current: current.update(status="mztab_validated", cleanup_allowed=True))
        self.assertIsNone(superseded_validated_run(read_manifest(manifest)))
        self.assertFalse(any("superseded run" in item for item in plan_download_cleanup(manifest)["blockers"]))

    def aliased_rows(self, alias: Path):
        """The real rows, with a.mzML read through ``alias`` as an input that needs an ASCII-safe alias is."""
        from msdial_app import repository_analysis_rows

        real = repository_analysis_rows.build_repository_analysis_rows

        def build(manifest_view, projected=None):
            built = real(manifest_view, projected)
            for row in built["rows"]:
                if Path(row["input_path"]).name == "a.mzML":
                    row.update(file_name=alias.stem, file_path=str(alias))
                    row["console_alias"] = {"path": str(alias), "kind": "hardlink", "target": row["input_path"],
                                            "reasons": ["non_ascii"]}
            return built

        return patch("msdial_app.repository_analysis_rows.build_repository_analysis_rows", side_effect=build)

    def test_a_conflicting_new_run_keeps_the_alias_the_committed_run_reused(self) -> None:
        # Review r7-62's probe, with the alias named by the rows as a real one is: A makes the alias, B reuses it
        # and commits output-run-2, A's commit is refused. A's abandon must not remove what B's CSV names.
        from msdial_app import repository_analysis_rows

        manifest, files, _run, _before = self.finished_mtbks217()
        alias = manifest.parent.parent / "raw" / "console-aliases" / "alias-a.mzML"
        real_alias = repository_analysis_rows.create_console_aliases
        real_csv = repository_analysis_rows.write_analysis_csv
        made_by: dict = {}
        inner: dict = {}

        def aliases(built, made=None):
            failures = real_alias(built, made=made)
            made_by.setdefault("A" if not inner else "B", list(made or []))
            return failures

        def csv_then_b(built, path):
            written = real_csv(built, path)
            if not inner:
                inner["B"] = None
                inner["B"] = self.prepare(manifest, confirmed=True, new_run=True)
            return written

        with self.aliased_rows(alias), \
                patch("msdial_app.repository_analysis_rows.create_console_aliases", side_effect=aliases), \
                patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=csv_then_b):
            a = self.prepare(manifest, confirmed=True, new_run=True)

        b = inner["B"]
        self.assertEqual((True, str(manifest.parent.parent / "output-run-2" / "analysis_files.csv")),
                         (b["prepared"], b["input_path"]))
        self.assertEqual((False, "new_run_conflict"), (a["ok"], a["reason"]))
        self.assertEqual(["directory", "hardlink"], [kind for kind, _link, _target in made_by["A"]], "A made the alias")
        self.assertEqual([], made_by["B"], "B reused it")
        recorded = read_manifest(manifest)
        self.assertEqual((str(manifest.parent.parent / "output-run-2"), "preflight_passed"),
                         (recorded["output_directory"], recorded["status"]))
        self.assertIn(str(alias), [row["file_path"] for row in self.csv_rows(b)])
        self.assertTrue(alias.is_file(), "the alias the committed run's CSV names stays")
        self.assertTrue(all(Path(row["file_path"]).exists() for row in self.csv_rows(b)))
        self.assertTrue(os.path.samefile(alias, files["a.mzML"]))
        self.assertEqual([], [p.name for p in manifest.parent.parent.iterdir() if p.name.startswith(".output")])

    def test_review_r7_62_probe_as_written(self) -> None:
        # The reviewer's probe unchanged: both prepares make or reuse an alias that no row names. B commits; A is
        # refused. A's abandon removes that alias, as no committed CSV names it, and every input path B's
        # committed CSV names still exists.
        from msdial_app import repository_analysis_rows

        manifest, files, _run, _before = self.finished_mtbks217()
        aliases = manifest.parent.parent / "raw" / "console-aliases"
        alias = {"path": str(aliases / "alias-a.mzML"), "target": str(files["a.mzML"]), "kind": "hardlink"}
        real_alias = repository_analysis_rows.create_console_aliases
        real_csv = repository_analysis_rows.write_analysis_csv
        log: list = []

        def aliases_fn(built, made=None):
            fails = real_alias({"rows": [{"console_alias": dict(alias)}]}, made=made)
            log.append(("alias", list(made or [])))
            return fails

        state = {"inner": False}

        def csv_fn(built, path):
            written = real_csv(built, path)
            if not state["inner"]:
                state["inner"] = True
                b = self.prepare(manifest, confirmed=True, new_run=True)
                log.append(("B", b.get("prepared"), b.get("reason"), b.get("input_path")))
            return written

        with patch("msdial_app.repository_analysis_rows.create_console_aliases", side_effect=aliases_fn), \
                patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=csv_fn):
            a = self.prepare(manifest, confirmed=True, new_run=True)

        [b] = [entry for entry in log if entry[0] == "B"]
        self.assertEqual((True, None), b[1:3])
        self.assertEqual((False, "new_run_conflict"), (a["ok"], a["reason"]))
        rec = read_manifest(manifest)
        self.assertEqual((str(manifest.parent.parent / "output-run-2"), "preflight_passed"), (rec["output_directory"], rec["status"]))
        self.assertFalse((aliases / "alias-a.mzML").exists(), "no committed CSV names it")
        self.assertTrue(all(Path(row["file_path"]).exists() for row in self.csv_rows({"input_path": b[3]})))

    def test_an_abandoned_new_run_still_removes_the_alias_no_committed_csv_names(self) -> None:
        # The same alias, made by a prepare whose CSV then failed and that no other prepare reused: it goes.
        from msdial_app import repository_analysis_rows

        manifest, files, run, before = self.finished_mtbks217()
        alias = manifest.parent.parent / "raw" / "console-aliases" / "alias-a.mzML"
        real_csv = repository_analysis_rows.write_analysis_csv

        def failed(built, path):
            real_csv(built, path)
            raise OSError("the disk filled")

        with self.aliased_rows(alias), patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=failed):
            refused = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual("os_error", refused["reason"])
        self.assertFalse(alias.parent.exists(), "the alias and the folder made for it are taken back")
        self.assertEqual(b"x", files["a.mzML"].read_bytes())
        self.assert_untouched(manifest, before, run)

    def test_a_new_run_whose_reused_alias_was_taken_back_meanwhile_is_not_committed(self) -> None:
        # The other order: the prepare that made the alias abandons it before the one that reused it commits.
        # The commit finds the alias gone and refuses, rather than commit a CSV that names a missing input.
        from msdial_app import repository_analysis_rows

        manifest, files, run, _before = self.finished_mtbks217()
        alias = manifest.parent.parent / "raw" / "console-aliases" / "alias-a.mzML"
        alias.parent.mkdir(parents=True)
        os.link(files["a.mzML"], alias)
        real_csv = repository_analysis_rows.write_analysis_csv

        def taken_back_meanwhile(built, path):
            written = real_csv(built, path)
            alias.unlink()
            return written

        with self.aliased_rows(alias), \
                patch("msdial_app.repository_analysis_rows.write_analysis_csv", side_effect=taken_back_meanwhile):
            refused = self.prepare(manifest, confirmed=True, new_run=True)

        self.assertEqual((False, False, "new_run_conflict"), (refused["ok"], refused["prepared"], refused["reason"]))
        self.assertIn("no longer exist", refused["detail"])
        recorded = read_manifest(manifest)
        self.assertEqual(("mztab_validated", True), (recorded["status"], recorded["cleanup_allowed"]))
        self.assertNotIn("superseded_runs", recorded)
        self.assertEqual(run["bytes"], {path: path.read_bytes() for path in run["bytes"]})
        workspace = manifest.parent.parent
        self.assertEqual([], [p.name for p in workspace.iterdir() if p.name.startswith((".output", "output-run"))])

    def test_the_aliases_a_committed_manifest_names_include_sidecars_and_superseded_runs(self) -> None:
        from msdial_app.repository_reanalysis import _alias_is_named, _aliases_named_by_manifest

        folder = self.root / "raw" / "console-aliases"
        folder.mkdir(parents=True)
        old_csv = self.root / "output" / "analysis_files.csv"
        old_csv.parent.mkdir()
        old_csv.write_text(f"file_path,file_name\n{folder / 'alias-old.mzML'},alias-old\n", encoding="utf-8-sig")
        named = _aliases_named_by_manifest({
            "input_lineage": {"rows": [
                {"path": "x.wiff2", "console_path": str(folder / "alias-x.wiff2"),
                 "console_alias": {"path": str(folder / "alias-x.wiff2"), "sidecars": ["alias-x.wiff.scan"]}},
            ]},
            "superseded_runs": [
                {"analysis_csv": {"path": str(old_csv)}},
                {"input_lineage_written": [{"path": "d.d", "console_path": str(folder / "alias-d.d")}]},
            ],
        })
        for name, expected in (
            ("alias-x.wiff2", True), ("alias-x.wiff.scan", True), ("alias-x.timeseries.data", True),
            ("alias-old.mzML", True), ("alias-d.d", True), ("alias-xy.wiff2", False), ("alias-y.mzML", False),
        ):
            with self.subTest(name=name):
                self.assertEqual(expected, _alias_is_named(folder / name, named))
        self.assertFalse(_alias_is_named(folder, named), "the alias folder itself is no alias")

class UntargetedWordingTests(_Scratch):
    def test_confirm_untargeted_is_recorded_as_an_inference_not_a_confirmation(self) -> None:
        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML"], untargeted=None, acquisition="DDA")

        result = self.preflight(manifest, stub, _Extractor({}), confirm_untargeted=True)

        evidence = " ".join(result["project"]["evidence"])
        self.assertNotIn("Untargeted status confirmed", evidence)
        self.assertIn("an inference", evidence)
        self.assertTrue(result["project"]["untargeted"])


class LostUpdateTests(_Scratch):
    def test_what_another_writer_records_while_the_headers_are_read_survives(self) -> None:
        manifest, stub, _ = _unit(self.root / "unit", ["a.mzML", "b.mzML"])

        def concurrent_writer(_command) -> None:
            # A lease heartbeat, a recorded approval, a run attempt: anything written while the extractor runs.
            update_manifest(manifest, lambda current: current.update(written_meanwhile="kept"))

        self.preflight(manifest, stub, _Extractor({}, during=concurrent_writer))

        self.assertEqual("kept", read_manifest(manifest).get("written_meanwhile"))

    def test_a_unit_split_while_its_headers_are_read_again_stays_split(self) -> None:
        names = ["a_DDA.mzML", "b_DIA.mzML"]
        verdicts = {name: {"method": "DIA" if "DIA" in name else "DDA"} for name in names}
        manifest, stub, _ = _unit(self.root / "unit", names)
        self.preflight(manifest, stub, _Extractor(verdicts))
        stub.write_bytes(b"a rebuilt extractor, so the second preflight reads every input again")
        splits: list[dict] = []

        def split_meanwhile(_command) -> None:
            if not splits:
                splits.append(split_unit_by_acquisition(manifest, confirmed=True))

        self.preflight(manifest, stub, _Extractor(verdicts, during=split_meanwhile))
        recorded = read_manifest(manifest)

        self.assertTrue(splits[0]["written"])
        self.assertEqual("split_by_acquisition", recorded["status"])
        self.assertFalse(recorded["execution_allowed"])
        self.assertEqual(2, len(recorded["split_into"]))
        self.assertIn("raw_metadata_preflight", recorded)

    def test_two_splits_of_one_unit_at_once_make_its_parts_once(self) -> None:
        names = ["a_DDA.mzML", "b_DIA.mzML", "c_DDA.mzML", "d_DIA.mzML"]
        verdicts = {name: {"method": "DIA" if "DIA" in name else "DDA"} for name in names}
        manifest, stub, _ = _unit(self.root / "unit", names)
        self.preflight(manifest, stub, _Extractor(verdicts))
        writing = threading.Event()
        second_done = threading.Event()
        from msdial_app import repository_metadata

        real_workspace = repository_metadata.metadata_workspace

        def slow_workspace(project):
            if threading.current_thread().name == "first" and not writing.is_set():
                writing.set()
                # Give the second split every chance to run to its end while the first is part-way through.
                second_done.wait(timeout=1.5)
            return real_workspace(project)

        results: dict[str, dict] = {}

        def split(name: str) -> None:
            results[name] = split_unit_by_acquisition(manifest, confirmed=True)
            if name == "second":
                second_done.set()

        with patch.object(repository_metadata, "metadata_workspace", side_effect=slow_workspace):
            first = threading.Thread(target=split, args=("first",), name="first")
            first.start()
            writing.wait(timeout=10)
            second = threading.Thread(target=split, args=("second",), name="second")
            second.start()
            first.join(timeout=30)
            second.join(timeout=30)

        self.assertEqual([True, False], [results["first"]["written"], results["second"]["written"]])
        self.assertTrue(results["second"]["already_split"])
        self.assertEqual(2, len(read_manifest(manifest)["split_into"]))


if __name__ == "__main__":
    unittest.main()
