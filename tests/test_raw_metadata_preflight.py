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
        entries.append(
            {
                "file": name, "outcome": outcome, "acquisition_mode": "", "polarity": "",
                "format": (formats or {}).get(name, "mzml"), "has_ion_mobility": None,
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

    def test_a_declaration_with_an_unknown_header_runs_on_the_declaration(self) -> None:
        disposition = self.decide([_header("w.raw", "Unknown", confidence=0.3)], declared={"acquisition_mode": "DDA"})

        self.assertEqual("run", disposition["disposition"])
        self.assertIn("acquisition_declared_only", disposition["warnings"])

    def test_a_confident_header_overrides_the_declaration(self) -> None:
        disposition = self.decide([_header("a.mzML", "DIA", confidence=0.82)], declared={"acquisition_mode": "DDA"})

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual("SWATH", disposition["console_acquisition_type"])
        self.assertIn("acquisition_header_overrides_declaration", disposition["warnings"])
        self.assertEqual("DIA", disposition["declared_vs_header"][0]["decided"])

    def test_an_unconfident_header_leaves_the_declaration_in_force(self) -> None:
        disposition = self.decide([_header("a.mzML", "PRM", confidence=0.65)], declared={"acquisition_mode": "DDA"})

        self.assertEqual("run", disposition["disposition"])
        self.assertEqual("DDA", disposition["console_acquisition_type"])
        self.assertIn("acquisition_header_disagrees_low_confidence", disposition["warnings"])
        self.assertEqual("DDA", disposition["declared_vs_header"][0]["decided"])

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

    def test_the_record_has_the_shared_contract_shape(self) -> None:
        disposition = self.decide([_header("a.mzML", "DDA")])
        disposition.pop("assignments")

        for key in ("schema", "disposition", "reasons", "warnings", "excluded_inputs", "split_key", "decided_at", "extractor"):
            self.assertIn(key, disposition)
        self.assertEqual("msdial-campaign-disposition.v1", disposition["schema"])
        self.assertEqual({"sha256", "inventory_sha256", "provenance_status", "pinned"}, set(disposition["extractor"]))


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

    def test_a_low_confidence_header_the_declaration_overrules_does_not_block_the_run(self) -> None:
        manifest, _stub, files = self.campaign_unit(["a.mzML", "b.mzML"], acquisition="DDA")
        extractor = _PinnedExtractor.make(self.root / "build")

        result = self.preflight(manifest, extractor, _Extractor({"b.mzML": {"method": "DIA", "confidence": 0.6}}))

        self.assertTrue(result["execution_allowed"])
        self.assertEqual("DDA", result["project"]["acquisition_mode"])
        gate = evaluate_repository_execution_gate(
            {
                "repository_run_manifest": str(manifest),
                "output_root": str(self.root / "unit" / "output"),
                "ion_mode": "Negative",
                "files": [{"file_path": str(path), "acquisition_type": "DDA"} for path in files],
            }
        )
        self.assertTrue(gate["allowed"], gate["blockers"])

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

    def test_a_file_the_declaration_decided_does_not_run_as_its_weak_header_says(self) -> None:
        manifest, files, result = self.campaign(["a.mzML", "b.mzML"], {"b.mzML": {"method": "DIA", "confidence": 0.6}}, acquisition="DDA")
        self.assertEqual("DDA", result["campaign_disposition"]["console_acquisition_type"])

        self.assertTrue(self.gate(manifest, files, "DDA")["allowed"])
        self.assertFalse(self.gate(manifest, files[1:], "SWATH")["allowed"])

    def test_outside_a_campaign_a_file_is_held_to_its_header_as_before(self) -> None:
        manifest, stub, files = _unit(self.root / "unit", ["a.mzML"], acquisition="DIA")
        self.preflight(manifest, stub, _Extractor({"a.mzML": {"method": "DIA", "confidence": 0.82}}))
        update_manifest(manifest, lambda current: current.update(execution_allowed=True))

        self.assertTrue(self.gate(manifest, files, "AIF")["allowed"])
        self.assertFalse(self.gate(manifest, files, "DDA")["allowed"])
        self.assertFalse(self.gate(manifest, files, "")["allowed"], "a blank type is DDA to the Console")


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
