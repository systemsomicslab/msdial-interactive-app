"""Reading a unit's raw headers with the extractor, and the one decision a campaign takes from them.

TWO HALVES.

run_extractor runs RawMetadataConsoleApp over a unit's inputs. It used to put every input on one command
line, with no time limit, and keep the whole stdout in the manifest. So one unreadable file failed the unit
(the extractor stops at the first input it cannot read, exit 1, or has no reader for, exit 82); a unit of
more than about 250 files could not be started at all, because Windows refuses a command line over 32,767
characters and subprocess raised OSError past every handler; and a vendor SDK that hung held the campaign
indefinitely. Now:

- inputs go in chunks of at most CHUNK_INPUTS, and no command line reaches COMMAND_LINE_LIMIT;
- each chunk has a time limit, the sum of its inputs' limits (input_timeout_seconds). These are the only
  limits on a preflight. The campaign runner watches for a stall and does not time a preflight out itself,
  because a unit of 165 Waters folders legitimately reads for hours;
- a chunk that fails or times out is run again one input at a time, so every input gets its own outcome;
- OSError and TimeoutExpired become outcomes and are never raised;
- only a tail of stderr is kept;
- an input read before by the same extractor binary, with the same path, size and modification time, is
  not read again.

decide_disposition turns the recorded verdicts into what a campaign does with the unit: run, skip, exclude
or split. It is the only place that mapping lives. The campaign runner reads the record it produces
(campaign_disposition) and never decides eligibility itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping


CHUNK_INPUTS = 20
# CreateProcess's lpCommandLine holds at most 32,767 characters including its terminating null.
COMMAND_LINE_LIMIT = 32_767
MAX_SPECTRUM_HEADERS = "200"
UNSUPPORTED_FORMAT_EXIT_CODE = 82
STDERR_TAIL_LINES = 20
STDERR_TAIL_CHARACTERS = 4_000
# A failed input keeps fewer lines than a chunk: several hundred of them can land in one manifest.
INPUT_STDERR_TAIL_LINES = 5

OUTCOME_OK = "ok"
OUTCOME_REUSED = "reused"
OUTCOME_UNSUPPORTED = "unsupported_format"
OUTCOME_FAILED = "failed"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_OS_ERROR = "os_error"
READ_OUTCOMES = frozenset({OUTCOME_OK, OUTCOME_REUSED})
OUTCOMES = (OUTCOME_OK, OUTCOME_REUSED, OUTCOME_UNSUPPORTED, OUTCOME_FAILED, OUTCOME_TIMED_OUT, OUTCOME_OS_ERROR)

# Wall time of one extractor process per input, measured on 2026-09-30 with the pinned build
# (msrawdataworkbench 592b6dbce, MsdialWorkbench f0583493a; --max-spectrum-headers 200, process start
# included, warm file cache):
#   mzML 0.2-0.4 s (22-78 MB)         Thermo .raw 0.5 s (0.37-0.61 GB)        SCIEX .wiff 0.3 s
#   Bruker BAF .d 0.3-0.5 s, and 0.9 s when it writes its analysis.sqlite    Agilent .d 0.5 s
#   Agilent ion-mobility .d 4.0 s (0.65 GB, about 6 s/GB)
#   Waters .raw 1.8-3.7 s (0.09-0.59 GB). Waters has no metadata-only reader, so the full-spectrum
#   fallback runs and the time follows the spectrum count rather than the bytes: up to about 42 s/GB.
# The limits sit far above those, for a cold cache, a network share and vendor SDK start-up, and grow with
# the bytes only where the measured time did: about 40 times the worst Waters rate and 100 times the
# ion-mobility rate. Bruker TDF was not measured; it is held to the ion-mobility rate.
TIMEOUT_POLICY: dict[str, dict[str, float]] = {
    "metadata_reader": {"base_seconds": 300.0, "seconds_per_gb": 0.0},
    "full_spectrum": {"base_seconds": 300.0, "seconds_per_gb": 1800.0},
    "ion_mobility": {"base_seconds": 300.0, "seconds_per_gb": 600.0},
}
INPUT_TIMEOUT_CEILING_SECONDS = 12 * 3600.0
_FORMAT_TIMEOUT_CLASS = {
    "waters_raw": "full_spectrum",
    "abf": "full_spectrum",
    "netcdf": "full_spectrum",
    "ibf": "full_spectrum",
    "waters_raw_im": "ion_mobility",
    "agilent_d_im": "ion_mobility",
    "bruker_tdf": "ion_mobility",
}
# The folder formats whose data carry an ion-mobility dimension, read from the folder itself. The Catalog
# reads bruker_tdf from member names before a download; the folder on disk is what decides here.
ION_MOBILITY_FORMATS = frozenset({"bruker_tdf", "agilent_d_im", "waters_raw_im"})
_FILE_FORMATS = {
    ".mzml": "mzml",
    ".raw": "thermo_raw",
    ".wiff": "sciex_wiff",
    ".wiff2": "sciex_wiff2",
    ".lcd": "shimadzu_lcd",
    ".abf": "abf",
    ".cdf": "netcdf",
    ".ibf": "ibf",
    ".mzxml": "mzxml",
    ".mzdata": "mzdata",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def file_key(path: str | Path) -> str:
    try:
        return str(Path(path).resolve()).casefold()
    except (OSError, ValueError):
        return str(path).casefold()


# ------------------------------------------------------------------------------------------------------
# Inputs
# ------------------------------------------------------------------------------------------------------


def _children(path: Path) -> list[Path]:
    try:
        return list(path.iterdir())
    except OSError:
        return []


def input_format(path: str | Path) -> str:
    """What kind of input a path is, from the path and, for a vendor folder, its contents."""
    target = Path(path)
    suffix = target.suffix.casefold()
    if target.is_dir():
        if suffix == ".raw":
            # Waters writes drift-time data as _FUNCnnn.CDT beside the scans of a mobility function.
            mobility = any(
                child.name.casefold().startswith("_func") and child.name.casefold().endswith(".cdt")
                for child in _children(target)
            )
            return "waters_raw_im" if mobility else "waters_raw"
        if suffix == ".d":
            if (target / "analysis.tdf").is_file():
                return "bruker_tdf"
            if (target / "analysis.tsf").is_file():
                return "bruker_tsf"
            if (target / "analysis.baf").is_file():
                return "bruker_baf"
            acquisition = target / "AcqData"
            if acquisition.is_dir():
                return "agilent_d_im" if (acquisition / "IMSFrame.bin").is_file() else "agilent_d"
            return "unrecognised_d"
        return "folder"
    name = target.name.casefold()
    if name.endswith(".mzdata.xml"):
        return "mzdata"
    return _FILE_FORMATS.get(suffix, suffix.lstrip(".") or "file")


def _tree_files(root: Path) -> Iterable[Path]:
    for directory, _folders, files in os.walk(root):
        for name in files:
            yield Path(directory) / name


def input_signature(path: str | Path) -> dict[str, int] | None:
    """Size and modification time of an input; for a vendor folder, of every file in it. None if gone."""
    target = Path(path)
    try:
        if target.is_dir():
            size = count = 0
            latest = target.stat().st_mtime_ns
            for item in _tree_files(target):
                stat = item.stat()
                size += stat.st_size
                count += 1
                latest = max(latest, stat.st_mtime_ns)
            return {"size_bytes": size, "mtime_ns": latest, "file_count": count}
        stat = target.stat()
        return {"size_bytes": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    except OSError:
        return None


def _folder_listing(path: Path) -> set[str] | None:
    if not path.is_dir():
        return None
    try:
        return {item.relative_to(path).as_posix() for item in _tree_files(path)}
    except (OSError, ValueError):
        return None


def input_timeout_seconds(path: str | Path, fmt: str | None = None, size_bytes: int | None = None) -> float:
    """The time one extractor read of this input may take, from its format and, where it matters, size."""
    kind = fmt or input_format(path)
    policy = TIMEOUT_POLICY[_FORMAT_TIMEOUT_CLASS.get(kind, "metadata_reader")]
    if size_bytes is None:
        signature = input_signature(path) or {}
        size_bytes = int(signature.get("size_bytes") or 0)
    seconds = policy["base_seconds"] + policy["seconds_per_gb"] * max(int(size_bytes), 0) / 1024**3
    return round(min(seconds, INPUT_TIMEOUT_CEILING_SECONDS), 1)


# ------------------------------------------------------------------------------------------------------
# Running the extractor
# ------------------------------------------------------------------------------------------------------


def extractor_command(extractor: Path, inputs: Iterable[Path], output: Path) -> list[str]:
    command = [str(extractor)]
    for path in inputs:
        command.extend(["--input", str(path)])
    command.extend(["--output", str(output), "--max-spectrum-headers", MAX_SPECTRUM_HEADERS])
    return command


def command_line_length(command: list[str]) -> int:
    """The length of the command line CreateProcess receives for this argument list."""
    return len(subprocess.list2cmdline(command))


def plan_chunks(
    extractor: Path,
    inputs: list[Path],
    output: Path,
    chunk_inputs: int = CHUNK_INPUTS,
    command_line_limit: int = COMMAND_LINE_LIMIT,
) -> tuple[list[list[Path]], list[Path]]:
    """Inputs in order, grouped so no group exceeds chunk_inputs or the command-line limit.

    The second list holds inputs whose command line would be too long even alone; they are recorded as
    failures and never started. ``output`` stands in for the longest chunk output path.
    """
    chunks: list[list[Path]] = []
    too_long: list[Path] = []
    current: list[Path] = []
    for path in inputs:
        # The terminating null is part of the limit.
        if command_line_length(extractor_command(extractor, [path], output)) >= command_line_limit:
            too_long.append(path)
            continue
        trial = [*current, path]
        if len(trial) > chunk_inputs or (
            command_line_length(extractor_command(extractor, trial, output)) >= command_line_limit
        ):
            chunks.append(current)
            trial = [path]
        current = trial
    if current:
        chunks.append(current)
    return chunks, too_long


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def stderr_tail(text: Any, lines: int = STDERR_TAIL_LINES) -> list[str]:
    kept = [line.rstrip() for line in _text(text).splitlines() if line.strip()][-lines:]
    total = 0
    tail: list[str] = []
    for line in reversed(kept):
        total += len(line)
        if total > STDERR_TAIL_CHARACTERS:
            break
        tail.append(line)
    return list(reversed(tail))


def _read_records(output: Path) -> list[dict[str, Any]] | None:
    try:
        data = json.loads(output.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if isinstance(data, dict):
        data = [data]
    if not isinstance(data, list):
        return None
    return [item for item in data if isinstance(item, dict)]


def _assign_records(records: list[dict[str, Any]], chunk: list[Path]) -> dict[str, dict[str, Any]]:
    """Each record to the input it describes, by the path the extractor wrote into it."""
    wanted = {file_key(path) for path in chunk}
    assigned: dict[str, dict[str, Any]] = {}
    strays: list[dict[str, Any]] = []
    for record in records:
        source = record.get("source") if isinstance(record.get("source"), dict) else {}
        text = str(source.get("filePath") or "").strip()
        key = file_key(text) if text else ""
        if key in wanted and key not in assigned:
            assigned[key] = record
        else:
            strays.append(record)
    remaining = [path for path in chunk if file_key(path) not in assigned]
    # The extractor writes one record per input, in order. Only when every record is accounted for that
    # way is a record that names its input differently (a short path, another casing) given to it.
    if strays and len(strays) == len(remaining) and len(records) == len(chunk):
        for path, record in zip(remaining, strays):
            source = record.get("source") if isinstance(record.get("source"), dict) else {}
            # Named after the input it was given for, so every later lookup by path finds it; the name the
            # extractor wrote is kept beside it.
            assigned[file_key(path)] = {
                **record,
                "source": {**source, "filePath": str(path), "reportedFilePath": source.get("filePath")},
            }
    return assigned


def run_extractor(
    extractor: Path,
    inputs: list[Path],
    work_directory: Path,
    *,
    extractor_sha256: str = "",
    previous: Mapping[str, Mapping[str, Any]] | None = None,
    created_before: Mapping[str, Iterable[str]] | None = None,
    chunk_inputs: int = CHUNK_INPUTS,
    command_line_limit: int = COMMAND_LINE_LIMIT,
    runner: Callable[..., Any] | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Read every input's header. Never raises for anything the extractor or the operating system does.

    ``previous`` maps an input's file_key to an earlier read of it ({"record", "entry", "source"}); it is
    used only when that entry names the same extractor sha256 and the same size and modification time.
    ``created_before`` maps an input's file_key to the files an earlier read recorded the reader creating
    in it; they stay in reader_created_files, because a folder read again is compared only with the state
    the earlier read left it in.
    ``progress`` is told, before each extractor process starts, which attempt it is and its time limit;
    it is how a watcher tells a long read from a stalled one, and nothing it raises stops the reads.
    Returns the records read (input order), one outcome per input keyed by file_key, and the chunk log.
    """
    run = runner or subprocess.run
    work_directory.mkdir(parents=True, exist_ok=True)
    for stale in work_directory.glob("chunk-*.json"):
        # Left by a preflight that did not finish; never read as this one's.
        stale.unlink(missing_ok=True)
    outcomes: dict[str, dict[str, Any]] = {}
    records: dict[str, dict[str, Any]] = {}
    chunk_log: list[dict[str, Any]] = []
    formats = {file_key(path): input_format(path) for path in inputs}
    signatures = {file_key(path): input_signature(path) for path in inputs}

    def created_earlier(key: str, entry: Mapping[str, Any]) -> list[str]:
        # What a reader wrote into an input is a fact about the input from then on: the read that finds the
        # folder as the first one left it sees no difference, and a reused read makes no listing at all.
        found = [*(entry.get("reader_created_files") or []), *((created_before or {}).get(key) or [])]
        return list(dict.fromkeys(str(item) for item in found if str(item).strip()))

    to_read: list[Path] = []
    for path in inputs:
        key = file_key(path)
        earlier = (previous or {}).get(key) or {}
        entry = earlier.get("entry") or {}
        if (
            earlier.get("record")
            and extractor_sha256
            and entry.get("extractor_sha256") == extractor_sha256
            and entry.get("outcome") in READ_OUTCOMES
            and signatures[key] is not None
            and entry.get("input_signature") == signatures[key]
        ):
            records[key] = dict(earlier["record"])
            outcomes[key] = {
                "outcome": OUTCOME_REUSED,
                "exit_code": 0,
                "reused_from": str(earlier.get("source") or ""),
                "input_signature": signatures[key],
                "extractor_sha256": extractor_sha256,
                "format": formats[key],
                "reader_created_files": created_earlier(key, entry),
            }
        else:
            to_read.append(path)

    # Listed once, before the first read: a reader that writes into a folder (Bruker's baf2sql leaves an
    # analysis.sqlite in a .d that had none) is caught even if its chunk failed and the input was read
    # again on its own.
    listings = {file_key(path): _folder_listing(path) for path in to_read}
    sizes = {key: int((signatures[key] or {}).get("size_bytes") or 0) for key in signatures}
    timeouts = {
        file_key(path): input_timeout_seconds(path, formats[file_key(path)], sizes[file_key(path)])
        for path in to_read
    }
    longest_output = work_directory / "chunk-9999-input-9999.json"
    chunks, too_long = plan_chunks(extractor, to_read, longest_output, chunk_inputs, command_line_limit)
    for path in too_long:
        outcomes[file_key(path)] = {
            "outcome": OUTCOME_OS_ERROR,
            "exit_code": None,
            "error": (
                f"The command line for this input alone would exceed {command_line_limit} characters, "
                "so it was not started."
            ),
        }

    def attempt(label: str, group: list[Path], output: Path) -> dict[str, Any]:
        output.unlink(missing_ok=True)
        timeout = round(sum(timeouts[file_key(path)] for path in group), 1)
        command = extractor_command(extractor, group, output)
        if progress is not None:
            try:
                progress(
                    {
                        "attempt": label,
                        "inputs": len(group),
                        "timeout_seconds": timeout,
                        "settled": len(outcomes),
                        "total": len(inputs),
                        "started_at": _now(),
                    }
                )
            except Exception:  # noqa: BLE001 - a progress note must never cost a verdict
                pass
        started = time.monotonic()
        entry: dict[str, Any] = {"chunk": label, "inputs": len(group), "timeout_seconds": timeout}
        try:
            completed = run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as error:
            entry.update(result=OUTCOME_TIMED_OUT, exit_code=None, stderr_tail=stderr_tail(error.stderr))
        except (OSError, ValueError) as error:
            # ValueError: an argument subprocess cannot pass at all, such as an embedded null.
            entry.update(result=OUTCOME_OS_ERROR, exit_code=None, error=f"{type(error).__name__}: {error}")
        else:
            code = completed.returncode
            entry.update(
                result="exited",
                exit_code=code,
                stderr_tail=stderr_tail(completed.stderr),
                stdout_characters=len(_text(completed.stdout)),
            )
        entry["elapsed_seconds"] = round(time.monotonic() - started, 3)
        found = _read_records(output) if entry["result"] == "exited" and entry["exit_code"] == 0 else None
        entry["records"] = len(found or [])
        output.unlink(missing_ok=True)
        chunk_log.append(entry)
        return {"entry": entry, "records": _assign_records(found, group) if found else {}}

    def settle(path: Path, result: dict[str, Any], own: bool) -> None:
        """The outcome of an input from the attempt that last read it (own: it was read alone)."""
        key = file_key(path)
        entry = result["entry"]
        record = result["records"].get(key)
        if record is not None:
            records[key] = record
            outcomes[key] = {"outcome": OUTCOME_OK, "exit_code": 0}
            return
        if entry["result"] == OUTCOME_TIMED_OUT:
            outcome = OUTCOME_TIMED_OUT
        elif entry["result"] == OUTCOME_OS_ERROR:
            outcome = OUTCOME_OS_ERROR
        elif entry.get("exit_code") == UNSUPPORTED_FORMAT_EXIT_CODE:
            outcome = OUTCOME_UNSUPPORTED
        else:
            # Exit 1 (a reader failed), 2 (a missing path), or 0 with no record for this input.
            outcome = OUTCOME_FAILED
        detail: dict[str, Any] = {"outcome": outcome, "exit_code": entry.get("exit_code")}
        if own:
            detail["elapsed_seconds"] = entry["elapsed_seconds"]
            if entry.get("stderr_tail"):
                detail["stderr_tail"] = entry["stderr_tail"][-INPUT_STDERR_TAIL_LINES:]
            if entry.get("error"):
                detail["error"] = entry["error"]
        outcomes[key] = detail

    for index, chunk in enumerate(chunks):
        label = f"{index:04d}"
        result = attempt(label, chunk, work_directory / f"chunk-{label}.json")
        missing = [path for path in chunk if file_key(path) not in result["records"]]
        if not missing or len(chunk) == 1:
            for path in chunk:
                settle(path, result, own=len(chunk) == 1)
            continue
        for path in chunk:
            if file_key(path) in result["records"]:
                settle(path, result, own=False)
        # One input at a time, each with its own limit, so one bad file, or one that hangs, is the only
        # one without a verdict.
        for position, path in enumerate(missing):
            alone = f"{label}-input-{position:04d}"
            settle(path, attempt(alone, [path], work_directory / f"chunk-{alone}.json"), own=True)

    for path in to_read:
        key = file_key(path)
        outcome = outcomes[key]
        outcome["format"] = formats[key]
        outcome["timeout_seconds"] = timeouts[key]
        outcome["extractor_sha256"] = extractor_sha256
        # After the read: this is the state a later read of the same bytes will be compared with.
        outcome["input_signature"] = input_signature(path)
        before = listings.get(key)
        after = _folder_listing(path) if before is not None else None
        created = [str(path / relative) for relative in sorted(after - before)] if before is not None and after else []
        earlier = ((previous or {}).get(key) or {}).get("entry") or {}
        outcome["reader_created_files"] = list(dict.fromkeys([*created_earlier(key, earlier), *created]))
    try:
        work_directory.rmdir()
    except OSError:
        pass

    ordered = [records[file_key(path)] for path in inputs if file_key(path) in records]
    first_failure = next(
        (outcomes[file_key(path)] for path in inputs if outcomes[file_key(path)]["outcome"] not in READ_OUTCOMES),
        None,
    )
    exit_code = 0
    if first_failure is not None:
        code = first_failure.get("exit_code")
        # A time limit or a start that never happened has no exit code of its own; -1 keeps "not 0".
        exit_code = int(code) if isinstance(code, int) and code != 0 else -1
    return {
        "records": ordered,
        "outcomes": outcomes,
        "chunks": chunk_log,
        "command_template": extractor_command(extractor, [Path("<input>")], work_directory / "chunk-<n>.json"),
        "exit_code": exit_code,
        "counts": {
            name: sum(1 for item in outcomes.values() if item["outcome"] == name) for name in OUTCOMES
        },
    }


# ------------------------------------------------------------------------------------------------------
# What a header verdict means to the Console
# ------------------------------------------------------------------------------------------------------

CONSOLE_ACQUISITION_TYPES = ("DDA", "SWATH", "AIF")
OUT_OF_SCOPE_METHODS = ("PRM", "SRM", "MRM", "SIM")


def header_console_acquisition_type(method: str, isolation_targets: Any) -> tuple[str | None, str]:
    """(Console AcquisitionType, basis) for one file's header verdict, or (None, why not).

    The pinned Console's AcquisitionType is DDA, SWATH, AIF or None, and a value it cannot parse becomes
    DDA without a word (AnalysisFilesParser). The extractor says DIA for windowed and for all-ion data
    alike, so a DIA verdict is resolved by the MS2 isolation it recorded - SWATH when there are isolation
    windows, recorded by the vendor or inferred from recurring windowed MS2, and AIF when MS2 has no
    isolation at all - and never by a default.
    """
    value = str(method or "").strip()
    if value in {"DDA", "AIF", "SWATH"}:
        return value, "header"
    if value != "DIA":
        return None, ""
    if not isinstance(isolation_targets, list):
        return None, "dia_isolation_unrecorded"
    distinct = set()
    for item in isolation_targets:
        try:
            distinct.add(round(float(item), 4))
        except (TypeError, ValueError):
            continue
    if len(distinct) >= 2:
        return "SWATH", "header_isolation_windows"
    if not distinct:
        return "AIF", "header_no_isolation"
    # One recurring target is either one wide window or a single targeted precursor.
    return None, "dia_single_isolation_target"


def _declared_console_type(mode: str) -> tuple[str | None, str]:
    value = str(mode or "").strip().upper()
    if value in {"DDA", "SWATH", "AIF"}:
        return value, ""
    if value == "DIA":
        # The Catalog folds SWATH into DIA and keeps AIF apart, so its DIA is windowed DIA.
        return "SWATH", "declared_dia_read_as_swath"
    return None, ""


def _declared_agrees(declared: str, header: str, header_console: str | None) -> bool:
    declared = str(declared or "").strip().upper()
    if declared == "DDA":
        return header == "DDA"
    if declared == "DIA":
        return header in {"DIA", "AIF", "SWATH"}
    if declared == "SWATH":
        return header == "SWATH" or (header == "DIA" and header_console in {"SWATH", None})
    if declared == "AIF":
        return header == "AIF" or (header == "DIA" and header_console in {"AIF", None})
    return False


# ------------------------------------------------------------------------------------------------------
# The disposition
# ------------------------------------------------------------------------------------------------------

DISPOSITION_SCHEMA = "msdial-campaign-disposition.v1"
DISPOSITIONS = ("run", "skip", "exclude", "split")
# A header verdict this confident replaces a repository declaration it contradicts.
HEADER_OVERRIDE_CONFIDENCE = 0.8
_DECLARED_MODES = {"DDA", "DIA", "AIF", "SWATH"}
_POLARITIES = {"Positive", "Negative"}
_SWITCHING = {"PolaritySwitching", "MixedFunctions"}
_SEPARATION_NAMES = {
    "LiquidChromatography": "LC-MS",
    "GasChromatography": "GC-MS",
    "DirectInfusion": "DI-MS",
    "CapillaryElectrophoresis": "CE-MS",
}
# File-level reasons that mean "out of scope" when every input has one; the others mean "unresolved".
_OUT_OF_SCOPE_FILE_REASONS = (
    "ion_mobility_out_of_scope",
    "acquisition_out_of_scope:",
    "polarity_switching",
    "conversion_required",
    "ms1_only_beside_dia",
)
_UNIT_REASON_FOR_FILE_REASON = {
    "polarity_switching": "polarity_switching_out_of_scope",
    "raw_header_unsupported_format": "raw_header_unreadable",
    "input_missing": "inputs_missing",
    "raw_header_not_inspected": "raw_metadata_incomplete",
}


def declared_technical(project: Mapping[str, Any] | None) -> dict[str, Any]:
    """The four technical facts a unit's repository record declares, as the disposition reads them."""
    project = project or {}
    untargeted = project.get("untargeted")
    return {
        "acquisition_mode": str(project.get("acquisition_mode") or "Unknown"),
        "ion_mode": str(project.get("ion_mode") or "Unknown"),
        "separation": str(project.get("separation") or "Unknown"),
        "untargeted": untargeted if isinstance(untargeted, bool) else None,
    }


def _is_out_of_scope(reason: str) -> bool:
    return any(
        reason == item or (item.endswith(":") and reason.startswith(item)) for item in _OUT_OF_SCOPE_FILE_REASONS
    )


def decide_disposition(
    manifest: Mapping[str, Any],
    declared: Mapping[str, Any] | None = None,
    extractor: Mapping[str, Any] | None = None,
    decided_at: str | None = None,
) -> dict[str, Any]:
    """What a campaign does with a unit, from its recorded preflight. Never raises; changes nothing.

    Returns the campaign_disposition record (msdial-campaign-disposition.v1), plus ``assignments``: for
    each input that would run, its Console acquisition type, polarity and the basis of the type. The
    caller strips ``assignments`` before persisting and applies them to the per-file records.
    """
    def mapping(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    preflight = mapping(manifest.get("raw_metadata_preflight"))
    summary = mapping(preflight.get("summary"))
    coverage = mapping(summary.get("coverage"))
    declared = dict(declared or preflight.get("declared") or declared_technical(manifest.get("project")))
    identity = dict(extractor or preflight.get("extractor") or {})
    reasons: list[str] = []
    warnings: list[str] = []
    excluded: list[dict[str, str]] = []
    detail: list[str] = []
    disagreements: list[dict[str, Any]] = []
    assignments: dict[str, dict[str, Any]] = {}

    def warn(code: str) -> None:
        if code and code not in warnings:
            warnings.append(code)

    def result(disposition: str, split_key: dict[str, Any] | None = None, **extra: Any) -> dict[str, Any]:
        return {
            "schema": DISPOSITION_SCHEMA,
            "disposition": disposition,
            "reasons": list(dict.fromkeys(reasons)),
            "warnings": warnings,
            "excluded_inputs": excluded,
            "split_key": split_key,
            "decided_at": decided_at or _now(),
            "extractor": {
                "sha256": str(identity.get("sha256") or identity.get("binary_sha256") or ""),
                "inventory_sha256": str(identity.get("inventory_sha256") or ""),
                "provenance_status": str(identity.get("provenance_status") or ""),
                "pinned": bool(identity.get("pinned")),
            },
            "declared": declared,
            "declared_vs_header": disagreements[:50],
            "detail": detail,
            **extra,
            "assignments": assignments,
        }

    if not preflight or not summary:
        reasons.append("raw_metadata_preflight_missing")
        detail.append("No raw-header preflight is recorded, so nothing is known from the data.")
        return result("skip")
    candidates = [str(item) for item in manifest.get("input_candidates") or [] if str(item).strip()]
    if not candidates:
        reasons.append("no_inputs")
        detail.append("The unit has no input files.")
        return result("skip")
    if coverage.get("capped"):
        reasons.append("raw_metadata_incomplete")
        detail.append("The preflight was capped and did not attempt every input; run it over all of them.")
        return result("skip")

    # Scope that the repository record decides for the whole unit.
    separation = str(declared.get("separation") or "Unknown")
    observed = {
        _SEPARATION_NAMES.get(str(item), "Unknown") for item in summary.get("observed_separations") or []
    }
    observed.discard("Unknown")
    if separation not in {"LC-MS", "Unknown", ""}:
        reasons.append(f"separation_out_of_scope:{separation}")
    elif separation in {"Unknown", ""}:
        # The header reports Unknown for Waters, Agilent DIA and SCIEX, so it can only add evidence where the
        # repository said nothing; it never takes LC away from a unit whose repository says LC-MS.
        if observed == {"LC-MS"}:
            warn("separation_inferred_from_headers")
        elif observed - {"LC-MS"}:
            reasons.extend(f"separation_out_of_scope:{item}" for item in sorted(observed - {"LC-MS"}))
    if declared.get("untargeted") is False:
        reasons.append("targeted_out_of_scope")
    if reasons:
        detail.append("The unit lies outside this campaign's untargeted LC-MS/MS scope.")
        return result("exclude")
    if separation in {"Unknown", ""} and not observed:
        reasons.append("separation_unresolved")
        detail.append("Neither the repository record nor any header says how the samples were separated.")
        return result("skip")

    per_file = {
        file_key(str(item.get("file") or "")): item
        for item in summary.get("per_file") or []
        if isinstance(item, dict) and str(item.get("file") or "").strip()
    }
    declared_mode = str(declared.get("acquisition_mode") or "").strip()
    declared_known = declared_mode.upper() in _DECLARED_MODES
    declared_polarity = str(declared.get("ion_mode") or "").strip()

    def exclude(path: str, reason: str) -> None:
        excluded.append({"path": path, "reason": reason})

    readable: list[tuple[str, dict[str, Any]]] = []
    unreadable: list[tuple[str, dict[str, Any]]] = []
    for path in candidates:
        entry = per_file.get(file_key(path)) or {}
        fmt = str(entry.get("format") or "")
        if path.casefold().endswith((".mzxml", ".mzdata", ".mzdata.xml")):
            exclude(path, "conversion_required")
        elif entry.get("has_ion_mobility") is True or fmt in ION_MOBILITY_FORMATS:
            exclude(path, "ion_mobility_out_of_scope")
        elif not entry:
            exclude(path, "input_missing" if not Path(path).exists() else "raw_header_not_inspected")
        elif entry.get("outcome", OUTCOME_OK) in READ_OUTCOMES:
            readable.append((path, entry))
        else:
            unreadable.append((path, entry))

    header_based: set[str] = set()
    decided: list[tuple[str, dict[str, Any], str, str | None, str]] = []
    if readable:
        for path, entry in unreadable:
            exclude(
                path,
                "raw_header_unsupported_format"
                if entry.get("outcome") == OUTCOME_UNSUPPORTED
                else "raw_header_unreadable",
            )
    elif unreadable:
        if not declared_known:
            for path, entry in unreadable:
                exclude(path, "raw_header_unreadable")
        else:
            # Nothing could be read, but the repository says what the data are: they run on its word.
            warn("acquisition_declared_only")
            detail.append(
                "No input's header could be read, so the unit runs on its repository declaration "
                f"({declared_mode})."
            )
            for path, entry in unreadable:
                decided.append((path, entry, declared_mode, None, "declaration"))

    for path, entry in readable:
        header = str(entry.get("acquisition_mode") or "").strip()
        confidence = entry.get("confidence")
        confidence = float(confidence) if isinstance(confidence, (int, float)) else 0.0
        header_console = entry.get("header_console_acquisition_type")
        if header == "FullScan" or entry.get("has_ms2") is False:
            # MS1 only: whatever the declaration says, there is no MS2 to deconvolute.
            decided.append((path, entry, "FullScan", None, "header"))
            header_based.add(path)
            continue
        if not declared_known:
            decided.append((path, entry, header or "Unknown", header_console, "header"))
            if header not in {"", "Unknown"}:
                header_based.add(path)
            continue
        if header in {"", "Unknown"}:
            warn("acquisition_declared_only")
            decided.append((path, entry, declared_mode, None, "declaration"))
        elif _declared_agrees(declared_mode, header, header_console):
            decided.append((path, entry, header, header_console, "header"))
            header_based.add(path)
        elif confidence >= HEADER_OVERRIDE_CONFIDENCE:
            warn("acquisition_header_overrides_declaration")
            disagreements.append(
                {"file": path, "declared": declared_mode, "header": header, "confidence": confidence, "decided": header}
            )
            decided.append((path, entry, header, header_console, "header"))
            header_based.add(path)
        else:
            warn("acquisition_header_disagrees_low_confidence")
            disagreements.append(
                {"file": path, "declared": declared_mode, "header": header, "confidence": confidence,
                 "decided": declared_mode}
            )
            decided.append((path, entry, declared_mode, None, "declaration"))

    included: list[tuple[str, dict[str, Any], str, str]] = []
    ms1_only: list[tuple[str, dict[str, Any]]] = []
    for path, entry, mode, header_console, basis in decided:
        if mode in {"", "Unknown"}:
            exclude(path, "acquisition_unresolved")
            continue
        if mode in OUT_OF_SCOPE_METHODS:
            exclude(path, f"acquisition_out_of_scope:{mode}")
            continue
        if mode == "FullScan":
            ms1_only.append((path, entry))
            continue
        if entry.get("has_ms1") is False:
            # Product-ion-only data have no MS1 survey to find features in.
            exclude(path, "acquisition_out_of_scope:product_ion_only")
            continue
        if basis == "declaration":
            console, why = _declared_console_type(mode)
            warn(why)
            console_basis = "declaration"
        elif mode == "DDA":
            console, console_basis = "DDA", "header"
        else:
            console = header_console if header_console in CONSOLE_ACQUISITION_TYPES else None
            console_basis = str(entry.get("header_console_acquisition_basis") or "header")
            if console is None:
                fallback, why = _declared_console_type(declared_mode) if declared_known else (None, "")
                if fallback in {"SWATH", "AIF"}:
                    console, console_basis = fallback, "declaration"
                    warn(why)
        if console is None:
            exclude(path, "dia_scheme_unresolved")
            continue
        included.append((path, entry, console, console_basis))

    if ms1_only:
        if any(console == "DDA" for _path, _entry, console, _basis in included):
            # MS1-only survey files beside DDA files are aligned with them; DDA processing of a file with no
            # MS2 finds its MS1 features and nothing else.
            warn("ms1_only_files_folded")
            for path, entry in ms1_only:
                included.append((path, entry, "DDA", "folded_ms1_only"))
        elif included:
            for path, _entry in ms1_only:
                exclude(path, "ms1_only_beside_dia")
        else:
            for path, _entry in ms1_only:
                exclude(path, "acquisition_out_of_scope:FullScan")

    groups: dict[tuple[str, str], list[str]] = {}
    polarity_warned = False
    for path, entry, console, basis in included:
        header_polarity = str(entry.get("polarity") or "").strip()
        if header_polarity in _SWITCHING:
            exclude(path, "polarity_switching")
            continue
        if header_polarity in _POLARITIES:
            polarity = header_polarity
            if declared_polarity in _POLARITIES and declared_polarity != polarity and not polarity_warned:
                warn("polarity_header_overrides_declaration")
                polarity_warned = True
        elif declared_polarity in _POLARITIES:
            polarity = declared_polarity
        else:
            exclude(path, "polarity_unresolved")
            continue
        if console == "AIF" and entry.get("collision_energy_count") == 0:
            # The pinned Console skips every AIF deconvolution target with CE <= 0, and with none at all the
            # file gets no MS2Dec result and no log line. Recorded, not refused.
            warn("aif_collision_energy_targets_empty")
        groups.setdefault((console, polarity), []).append(path)
        assignments[file_key(path)] = {
            "path": path, "console_acquisition_type": console, "polarity": polarity, "basis": basis,
        }

    if not groups:
        file_reasons = [item["reason"] for item in excluded]
        out_of_scope = [reason for reason in file_reasons if _is_out_of_scope(reason)]
        chosen = out_of_scope or file_reasons or ["acquisition_unresolved"]
        reasons.extend(_UNIT_REASON_FOR_FILE_REASON.get(reason, reason) for reason in chosen)
        detail.append("No input remains that this campaign can run.")
        return result("exclude" if out_of_scope else "skip")

    if declared.get("untargeted") is None:
        runnable = [path for path, _entry, _console, basis in included if basis != "folded_ms1_only"]
        inferred = all(
            path in header_based
            and per_file.get(file_key(path), {}).get("has_ms1") is True
            and per_file.get(file_key(path), {}).get("has_ms2") is True
            for path in runnable
        )
        if not inferred:
            reasons.append("untargeted_unresolved")
            detail.append(
                "The repository does not say whether the study is untargeted, and the headers do not show "
                "DDA, DIA or AIF acquisition with MS1 and MS2 for every input, so it cannot be inferred."
            )
            return result("skip")
        # An inference from how the data were acquired, recorded as one; never as a confirmation.
        warn("untargeted_inferred_from_headers")

    if len(groups) > 1:
        acquisitions = {console for console, _polarity in groups}
        polarities = {polarity for _console, polarity in groups}
        by = [name for name, values in (("acquisition", acquisitions), ("polarity", polarities)) if len(values) > 1]
        split_key = {
            "by": by,
            "groups": [
                {
                    "console_acquisition_type": console,
                    "polarity": polarity,
                    "part_key": "-".join(
                        [*([console.casefold()] if "acquisition" in by else []),
                         *([polarity.casefold()[:3]] if "polarity" in by else [])]
                    ),
                    "file_count": len(paths),
                    "inputs": sorted(paths),
                }
                for (console, polarity), paths in sorted(groups.items())
            ],
        }
        detail.append(
            "MS-DIAL runs one acquisition type and one ion mode per analysis; the unit splits by "
            + " and ".join(by) + "."
        )
        return result("split", split_key)
    ((console, polarity),) = groups
    return result("run", console_acquisition_type=console, ion_mode=polarity)
