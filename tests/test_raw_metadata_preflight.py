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
                and "classify_preflight" in item for item in refused["blockers"]),
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
