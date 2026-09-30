from __future__ import annotations

import csv
import copy
import math
import re
import struct
import datetime as dt
import hashlib
import json
import math
import os
import platform
import queue
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Callable, Iterable

from . import __version__
from .sample_grouping import file_type_for, propose_grouping, propose_injection_order
from .diagnostic_paths import REPRODUCTION_DIRECTORY_NAME
from .user_settings import load_user_settings, user_data_directory


SUPPORTED_SUFFIXES = {
    ".abf",
    ".cdf",
    ".ibf",
    ".lcd",
    ".mzml",
    ".qgd",
    ".raw",
    ".wiff",
    ".wiff2",
}
CONVERSION_REQUIRED_SUFFIXES = (".mzxml", ".mzdata", ".mzdata.xml")
VC2013_DOWNLOAD_URL = (
    "https://support.microsoft.com/en-us/topic/"
    "update-for-visual-c-2013-and-visual-c-redistributable-package-"
    "5b2ac5ab-4139-8acc-08e2-9578ec9b2cf1"
)
SMOOTHING_METHODS = [
    "SimpleMovingAverage",
    "LinearWeightedMovingAverage",
    "SavitzkyGolayFilter",
    "BinomialFilter",
    "LowessFilter",
    "LoessFilter",
    "TimeBasedLinearWeightedMovingAverage",
]
LCMS_QA_CAPABILITY = "lcms_alignment_qa_matrix"
RT_CORRECTION_REVIEW_CAPABILITY = "rt_correction_review"
AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY = "automatic_alignment_rt_correction"
CONSOLE_BUILD_PROVENANCE = "msdial-console-build-provenance.json"
# THE CONSOLE IS ITS WHOLE OUTPUT FOLDER, NOT MSDIALCUI.exe ALONE.
#
# The build record named MSDIALCUI.exe's sha256 and the git head and nothing else. The mzML base64 fix
# lives in RawDataHandler.dll, a package the Console only references, so a folder whose RawDataHandler.dll
# had been swapped for another build still inspected as verified while the code that read every mzML had
# changed. The record now carries an inventory, the sha256 of every file in the folder, and a record that
# has one is checked file by file (inspect_console_path).
#
# Not part of it: the build record itself, and what a run or a person leaves beside the Console - logs,
# temporary files, crash dumps.
CONSOLE_INVENTORY_EXCLUDED_NAMES = frozenset(
    name.casefold() for name in (CONSOLE_BUILD_PROVENANCE, CONSOLE_BUILD_PROVENANCE + ".tmp")
)
CONSOLE_INVENTORY_EXCLUDED_SUFFIXES = (".log", ".tmp", ".dmp")
CONSOLE_INVENTORY_EXCLUDED_DIRECTORIES = frozenset({"log", "logs"})
# A Console output folder holds a few hundred files and a few hundred megabytes. One placed among other
# data is not hashed through: past either bound the inventory is too_large and names nothing.
CONSOLE_INVENTORY_MAX_FILES = 5000
CONSOLE_INVENTORY_MAX_BYTES = 4 * 1024 * 1024 * 1024
# The assemblies whose ProductVersion is recorded beside their sha256: MS-DIAL's own, built from the
# MsdialWorkbench tree, and RawDataHandler, the reader, which comes from its own package.
CONSOLE_KEY_ASSEMBLIES = (
    "MSDIALCUI.exe",
    "MSDIALCUI.dll",
    "MsdialCore.dll",
    "MsdialLcMsApi.dll",
    "MsdialLcImMsApi.dll",
    "MsdialGcMsApi.dll",
    "MsdialDimsCore.dll",
    "MsdialImmsCore.dll",
    "MsdialIntegrate.dll",
    "Common.dll",
    "RawDataHandler.dll",
)
# A build record written before inventories were names the binary only. It still inspects as verified,
# with this warning.
CONSOLE_INVENTORY_NOT_RECORDED = "inventory_not_recorded"
CONSOLE_INVENTORY_TOO_LARGE = "inventory_too_large"
AUTOMATIC_RT_CORRECTION_SUMMARY = "automatic_alignment_rt_correction_summary.tsv"
AUTOMATIC_RT_CORRECTION_ANCHORS = "automatic_alignment_rt_correction_anchors.tsv"
# The copy of the parameter template an RT-correction preview hands the Console.
RT_CORRECTION_METHOD_FILE = "rt_correction_method.txt"
AUTOMATIC_RT_CORRECTION_METHOD_KEYS = {
    "execute automatic rt correction for alignment",
    "automatic rt correction reference file id",
    "automatic rt correction rt bin width",
    "automatic rt correction match rt tolerance",
    "automatic rt correction minimum anchors",
    "automatic rt correction maximum anchors",
    "automatic rt correction minimum sample coverage",
    "automatic rt correction intensity quantile",
    "automatic rt correction maximum peak width quantile",
    "automatic rt correction minimum signal to noise",
    "automatic rt correction minimum gaussian similarity",
    "automatic rt correction minimum ideal slope",
    "automatic rt correction outlier mad threshold",
    "automatic rt correction reference centrality weight",
    "automatic rt correction interpolate blanks by analytical order",
}
# One set of defaults for the template reader, the validator and the method writer. The
# validator used to default an absent tolerance to 0 and refuse it, while the writer would
# have written 0.5 for the same state.
AUTOMATIC_RT_CORRECTION_DEFAULTS: dict[str, Any] = {
    "automatic_rt_correction_reference_file_id": -1,
    "automatic_rt_correction_rt_bin_width": 0.5,
    "automatic_rt_correction_match_rt_tolerance": 0.5,
    "automatic_rt_correction_minimum_anchors": 3,
    "automatic_rt_correction_maximum_anchors": 6,
    "automatic_rt_correction_minimum_sample_coverage": 0.5,
    "automatic_rt_correction_intensity_quantile": 0.75,
    "automatic_rt_correction_maximum_peak_width_quantile": 0.5,
    "automatic_rt_correction_minimum_signal_to_noise": 3,
    "automatic_rt_correction_minimum_gaussian_similarity": 0,
    "automatic_rt_correction_minimum_ideal_slope": 0,
    "automatic_rt_correction_outlier_mad_threshold": 3.5,
    "automatic_rt_correction_reference_centrality_weight": 0.35,
    "automatic_rt_correction_interpolate_blanks_by_analytical_order": True,
}
# The Console reads these as whole numbers and keeps its default for anything else, so a
# fractional value must be refused here rather than truncated: truncating it validated one
# number while the method file carried another.
AUTOMATIC_RT_CORRECTION_INTEGER_KEYS = frozenset(
    {
        "automatic_rt_correction_reference_file_id",
        "automatic_rt_correction_minimum_anchors",
        "automatic_rt_correction_maximum_anchors",
    }
)
# Strings only the Console assembly carries when it implements the feature: the lowercase
# method key its ConfigParser reads, and the audit line LcmsProcess writes. The title-case
# field label lives in MsdialCore.dll, so a Console that merely ships beside a newer core, or
# a byte search that happens to hit the label, would have claimed a feature that never runs.
# The assembly is MSDIALCUI.exe for net48 and MSDIALCUI.dll for net8, whose .exe is only a
# launcher; console_capabilities reads the file console_assembly_path names.
AUTOMATIC_RT_CORRECTION_CONSOLE_MARKERS = (
    "execute automatic rt correction for alignment",
    "Automatic alignment RT correction audit:",
)


def console_assembly_path(console_path: str | Path) -> Path:
    """The file that holds the Console's code, which is not always the file that is started.

    A net8 MSDIALCUI.exe, or MSDIALCUI without an extension on Linux and macOS, is an
    apphost launcher and the code is in the MSDIALCUI.dll beside it. Two launchers built
    from different commits differ only in their version string, so neither a capability
    probe nor a checksum of the launcher says what ran. Only a launcher has a
    runtimeconfig.json: a net48 MSDIALCUI.exe is the assembly itself, and a stale net8 dll
    left beside it by an unpacked archive is not what runs. A net48 archive unpacked over
    a net8 folder leaves the runtimeconfig.json behind as well, so the file's own header
    decides: a managed PE is the assembly itself.
    """
    path = Path(console_path)
    assembly = path.with_suffix(".dll")
    if not (
        path.suffix.casefold() in {".exe", ""}
        and assembly.is_file()
        and path.with_suffix(".runtimeconfig.json").is_file()
    ):
        return path
    try:
        image = path.read_bytes()
    except OSError:
        return path
    return path if _is_managed_pe(image) else assembly


def _git_output(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=15,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


# Below this, hashing costs less than the round trip to the cache file.
IDENTITY_CACHE_MIN_BYTES = 64 * 1024 * 1024


def file_identity(path: str | Path) -> dict[str, Any]:
    """Identify one file by content, not by where it happens to sit.

    A manifest that records only a library path says nothing about which library was
    used: the path can be reused, moved, or point at a rebuilt file. A checksum is the
    only field that survives all of that, so it is recorded even when no catalogue
    knows the file -- which is the usual case for a laboratory's own LBM2.

    Hashing a 700 MB library takes seconds, and a run prepares more than once, so the
    digest is cached against the file's size and modification time.
    """
    try:
        resolved = Path(path).expanduser().resolve()
        stat = resolved.stat()
    except (OSError, ValueError):
        return {"sha256": "", "size": 0, "modified_at": "", "identity_error": "unreadable"}

    # A size-and-timestamp cache cannot tell two same-length rewrites apart when both
    # land inside one clock tick, and a recorded checksum of content that is not there
    # is worse than no checksum. So the cache is used only where hashing is genuinely
    # expensive -- a multi-hundred-megabyte library, which takes seconds to write and
    # cannot be rewritten inside a tick -- and everything smaller is simply hashed.
    key = f"{resolved}|{stat.st_size}|{stat.st_mtime_ns}"
    cacheable = stat.st_size >= IDENTITY_CACHE_MIN_BYTES
    cache_path = user_data_directory() / "file-identity-cache.json"
    cache: dict[str, str] = {}
    if cacheable:
        try:
            if cache_path.is_file():
                loaded = json.loads(cache_path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    cache = {str(k): str(v) for k, v in loaded.items()}
        except (OSError, json.JSONDecodeError):
            cache = {}

    digest = cache.get(key, "")
    if not digest:
        hasher = hashlib.sha256()
        try:
            with resolved.open("rb") as handle:
                for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                    hasher.update(chunk)
        except OSError:
            return {
                "sha256": "",
                "size": stat.st_size,
                "modified_at": "",
                "identity_error": "unreadable",
            }
        digest = hasher.hexdigest()
    if cacheable and cache.get(key) != digest:
        cache[key] = digest
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            # Keep the cache from growing without bound; the entries are cheap to remake.
            trimmed = dict(list(cache.items())[-256:])
            cache_path.write_text(json.dumps(trimmed, indent=2), encoding="utf-8")
        except OSError:
            pass

    return {
        "sha256": digest,
        "size": stat.st_size,
        "modified_at": dt.datetime.fromtimestamp(
            stat.st_mtime, tz=dt.timezone.utc
        ).astimezone().isoformat(),
    }


def _console_provenance_warning(console: dict[str, Any]) -> str:
    """What to tell the analyst about a binary whose identity is not established."""
    status = console.get("provenance_status", "absent")
    if status == "verified":
        notes = []
        dirty = (console.get("git") or {}).get("dirty")
        if dirty:
            notes.append(
                "The recorded build matches this binary, but it was built from a working "
                "tree with uncommitted changes, so the git revision does not fully "
                "describe the code that ran."
            )
        if CONSOLE_INVENTORY_NOT_RECORDED in (console.get("provenance_warnings") or []):
            notes.append(
                "The build record names this binary but not the files beside it, so a "
                "changed RawDataHandler.dll or other dependency would go unnoticed; "
                "console_management.record_console_inventory adds the inventory."
            )
        return " ".join(notes)
    if status == "stale_mismatch":
        return (
            "A build record sits beside this binary and describes a different one, so the "
            "recorded version names code that did not run. Offer a rebuild through "
            "msdial_build_console_from_local_source before relying on the provenance."
        )
    if status == "unreadable":
        return "The build record beside this binary could not be read."
    return (
        "No build record accompanies this binary, so the run is identified by its "
        "checksum alone and not by a source revision."
    )


def find_console_source_root(console_path: str | Path) -> Path | None:
    path = Path(console_path).expanduser().resolve()
    for parent in path.parents:
        project = parent / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "MsdialCoreTestApp.csproj"
        if project.is_file() and (parent / ".git").exists():
            return parent
    return None


def console_git_state(source_root: str | Path) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    head = _git_output(root, "rev-parse", "HEAD")
    if not head:
        return {"source_root": str(root), "available": False}
    status = _git_output(root, "status", "--porcelain", "--untracked-files=normal")
    diff = _git_output(root, "diff", "--binary", "HEAD")
    working_tree_state = status + "\n" + diff
    origin_master = _git_output(root, "rev-parse", "--verify", "refs/remotes/origin/master")
    ahead = behind = None
    if origin_master:
        counts = _git_output(root, "rev-list", "--left-right", "--count", "HEAD...origin/master")
        try:
            ahead_text, behind_text = counts.split()
            ahead, behind = int(ahead_text), int(behind_text)
        except (ValueError, TypeError):
            pass
    return {
        "source_root": str(root),
        "available": True,
        "branch": _git_output(root, "branch", "--show-current"),
        "head": head,
        "short_head": head[:12],
        "head_time": _git_output(root, "show", "-s", "--format=%cI", "HEAD"),
        "origin_master_head": origin_master,
        "origin_master_short_head": origin_master[:12],
        "ahead_of_origin_master": ahead,
        "behind_origin_master": behind,
        "remote_note": "origin/master is a local tracking reference; use Fetch and compare source to refresh it.",
        "dirty": bool(status),
        "changed_files": len(status.splitlines()) if status else 0,
        "working_tree_diff_sha256": hashlib.sha256(diff.encode("utf-8")).hexdigest()
        if diff
        else "",
        "working_tree_state_sha256": hashlib.sha256(
            working_tree_state.encode("utf-8")
        ).hexdigest()
        if working_tree_state.strip()
        else "",
    }


def _sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _console_inventory_excluded(relative: Path) -> bool:
    name = relative.name.casefold()
    if name in CONSOLE_INVENTORY_EXCLUDED_NAMES or name.endswith(CONSOLE_INVENTORY_EXCLUDED_SUFFIXES):
        return True
    return any(part.casefold() in CONSOLE_INVENTORY_EXCLUDED_DIRECTORIES for part in relative.parts[:-1])


def console_inventory(output_directory: str | Path) -> dict[str, Any]:
    """The Console's output folder, file by file: what a build record's inventory holds.

    Returns {inventory_status, inventory, inventory_sha256, inventory_file_count, key_assemblies}.
    inventory lists {path, sha256, size} by path relative to the folder, in POSIX form, and
    inventory_sha256 is taken over its "path<TAB>sha256" lines, as the raw-metadata extractor's is.
    key_assemblies gives, for each of CONSOLE_KEY_ASSEMBLIES at the top of the folder, its ProductVersion
    and sha256. inventory_status is complete, or too_large past CONSOLE_INVENTORY_MAX_FILES or
    CONSOLE_INVENTORY_MAX_BYTES, when nothing is hashed and the inventory is empty. A file that cannot be
    read is listed with an empty sha256, which no record matches.
    """
    # Imported here: raw_metadata_extractor imports this module.
    from .raw_metadata_extractor import assembly_product_version, inventory_sha256

    root = Path(output_directory)
    listed: list[tuple[str, Path, int]] = []
    total = 0
    too_large = False
    for current, directories, names in os.walk(root):
        directories.sort()
        for name in sorted(names):
            path = Path(current) / name
            relative = path.relative_to(root)
            if _console_inventory_excluded(relative) or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            listed.append((relative.as_posix(), path, size))
            total += size
            if len(listed) > CONSOLE_INVENTORY_MAX_FILES or total > CONSOLE_INVENTORY_MAX_BYTES:
                too_large = True
                break
        if too_large:
            break
    entries: list[dict[str, Any]] = []
    if not too_large:
        for relative, path, size in listed:
            try:
                digest = _sha256_of(path)
            except OSError:
                digest = ""
            entries.append({"path": relative, "sha256": digest, "size": size})
        entries.sort(key=lambda entry: entry["path"])
    by_path = {entry["path"].casefold(): entry for entry in entries}
    key_assemblies: dict[str, dict[str, str]] = {}
    for name in CONSOLE_KEY_ASSEMBLIES:
        path = root / name
        if path.is_file():
            key_assemblies[name] = {
                "product_version": assembly_product_version(path),
                "sha256": str((by_path.get(name.casefold()) or {}).get("sha256") or ""),
            }
    return {
        "inventory_status": "too_large" if too_large else "complete",
        "inventory": entries,
        "inventory_sha256": "" if too_large else inventory_sha256(entries),
        "inventory_file_count": len(entries),
        "key_assemblies": key_assemblies,
    }


def _console_inventory_mismatch(
    recorded: dict[str, Any], current: dict[str, Any]
) -> dict[str, Any] | None:
    """How the folder differs from the inventory a build record carries: {} when it does not, None
    when the record's inventory is not a list of {path, sha256} it can be held to."""
    entries = recorded.get("inventory")
    if not isinstance(entries, list) or not all(
        isinstance(entry, dict) and "path" in entry and "sha256" in entry for entry in entries
    ):
        return None
    before = {str(entry["path"]): str(entry["sha256"]) for entry in entries}
    # A folder too large to inventory has no list to compare, and every recorded file would read as
    # removed; it is named by its status instead.
    complete = current["inventory_status"] == "complete"
    after = {str(entry["path"]): str(entry["sha256"]) for entry in current["inventory"]} if complete else before
    recorded_versions = recorded.get("key_assemblies")
    recorded_versions = recorded_versions if isinstance(recorded_versions, dict) else {}
    versions = current["key_assemblies"]

    def version(table: dict[str, Any], name: str) -> str:
        entry = table.get(name)
        return str(entry.get("product_version") or "") if isinstance(entry, dict) else ""

    difference: dict[str, Any] = {
        "changed": sorted(name for name in before.keys() & after.keys() if before[name] != after[name]),
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
        "product_version_changed": {
            name: {"recorded": version(recorded_versions, name), "actual": version(versions, name)}
            for name in sorted(set(recorded_versions) | set(versions))
            if version(recorded_versions, name) != version(versions, name)
        },
    }
    if not complete:
        difference["inventory_status"] = current["inventory_status"]
    elif not any(difference.values()) and str(recorded.get("inventory_sha256") or "") == current["inventory_sha256"]:
        return {}
    return {
        "recorded_inventory_sha256": str(recorded.get("inventory_sha256") or ""),
        "actual_inventory_sha256": current["inventory_sha256"],
        **difference,
    }


def inspect_console_path(console_path: str | Path, source: str = "") -> dict[str, Any]:
    path = Path(console_path).expanduser().resolve()
    if not path.is_file():
        return {"path": str(path), "exists": False, "source": source or "custom"}
    binary_sha256 = _sha256_of(path)
    # The launcher's checksum is kept because it names the file that was started, but a
    # net8 launcher is the same bytes apart from its version string whatever code sits
    # beside it, so the assembly's checksum is the one that identifies what ran. For a
    # net48 exe the two are the same file.
    assembly = console_assembly_path(path).resolve()
    assembly_sha256 = binary_sha256 if assembly == path else _sha256_of(assembly)
    stat = path.stat()
    source_root = find_console_source_root(path)
    folder_text = str(path.parent).casefold()
    if source_root:
        source_kind = "local_source_build"
    elif "msdial.console" in folder_text:
        source_kind = "official_distribution"
    else:
        source_kind = "custom"
    provenance_path = path.parent / CONSOLE_BUILD_PROVENANCE
    provenance: dict[str, Any] = {}
    recorded: dict[str, Any] = {}
    # "No record exists" and "a record exists and describes a different binary"
    # are opposite situations that a boolean reports identically. CLAUDE.md
    # requires software versions in every retained artifact, so the caller has to
    # be able to tell them apart -- and a rebuild outside the build tool leaves a
    # sidecar that still names the previous binary.
    # The build tool records the assembly it built (MSDIALCUI.dll for net8), so the
    # record is checked against the assembly. Checked against a net8 launcher, a genuine
    # build read as stale, and a record of the launcher would still match after the dll
    # beside it was rebuilt from other code.
    #
    # The folder is inventoried whether or not a record sits there, so a run manifest names the files the
    # Console ran with even where nothing vouches for them. A record written before inventories were
    # names the binary only: it still verifies, with the warning inventory_not_recorded, so the builds
    # recorded that way (c471463a5, f56d4478a) keep working until
    # console_management.record_console_inventory adds one.
    inventory = console_inventory(path.parent)
    warnings: list[str] = []
    inventory_mismatch: dict[str, Any] | None = {}
    provenance_status = "absent"
    if provenance_path.is_file():
        try:
            loaded = json.loads(provenance_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            provenance_status = "unreadable"
        else:
            if isinstance(loaded, dict):
                recorded = loaded
                if "inventory" in loaded:
                    inventory_mismatch = _console_inventory_mismatch(loaded, inventory)
                else:
                    warnings.append(
                        CONSOLE_INVENTORY_TOO_LARGE
                        if loaded.get("inventory_status") == "too_large"
                        else CONSOLE_INVENTORY_NOT_RECORDED
                    )
                if inventory_mismatch is None:
                    # An inventory that is not a list of {path, sha256} says nothing it can be held to.
                    provenance_status = "unreadable"
                elif loaded.get("binary_sha256") == assembly_sha256 and not inventory_mismatch:
                    # The list stays in the record beside the binary; its digest and the key assemblies
                    # are what travels into a run manifest.
                    provenance = {key: value for key, value in loaded.items() if key != "inventory"}
                    provenance_status = "verified"
                else:
                    provenance_status = "stale_mismatch"
            else:
                provenance_status = "unreadable"
    if inventory["inventory_status"] == "too_large" and CONSOLE_INVENTORY_TOO_LARGE not in warnings:
        warnings.append(CONSOLE_INVENTORY_TOO_LARGE)
    result = {
        "path": str(path),
        "exists": True,
        "source": source or source_kind.replace("_", " "),
        "source_kind": source_kind,
        "version": console_version(str(path)),
        "binary_sha256": binary_sha256,
        "binary_size": stat.st_size,
        "binary_modified_at": dt.datetime.fromtimestamp(
            stat.st_mtime, tz=dt.timezone.utc
        ).astimezone().isoformat(),
        "assembly_path": str(assembly),
        "assembly_sha256": assembly_sha256,
        "provenance_verified": provenance_status == "verified",
        "provenance_status": provenance_status,
        "provenance": provenance,
        "provenance_warnings": warnings,
        "inventory_status": inventory["inventory_status"],
        "inventory_sha256": inventory["inventory_sha256"],
        "inventory_file_count": inventory["inventory_file_count"],
        "key_assemblies": inventory["key_assemblies"],
        **console_capabilities(str(path)),
    }
    if provenance_status in {"stale_mismatch", "unreadable"}:
        if provenance_status == "unreadable":
            detail = "The build-provenance record beside this binary could not be read."
        elif recorded.get("binary_sha256") == assembly_sha256 and inventory_mismatch:
            named = [
                *inventory_mismatch["changed"],
                *(f"{name} (added)" for name in inventory_mismatch["added"]),
                *(f"{name} (removed)" for name in inventory_mismatch["removed"]),
            ]
            if named:
                which = ": " + ", ".join(named[:10]) + (f" and {len(named) - 10} more" if len(named) > 10 else "")
            elif inventory_mismatch.get("inventory_status"):
                which = " (the folder is now too large to inventory)"
            else:
                which = " (the recorded inventory digest does not match its own list)"
            detail = (
                "The build-provenance record names this binary, but the files beside it are not the "
                f"ones it recorded{which}. Rebuild through msdial_build_console_from_local_source "
                "before relying on recorded software versions."
            )
        else:
            detail = (
                "A build-provenance record sits beside this binary but does not describe it. "
                "Rebuild through msdial_build_console_from_local_source, or remove the record, "
                "before relying on recorded software versions."
            )
        result["provenance_mismatch"] = {
            "record_path": str(provenance_path),
            "recorded_binary_sha256": str(recorded.get("binary_sha256") or ""),
            # What the record was compared with: the assembly, not a net8 launcher.
            "actual_binary_path": str(assembly),
            "actual_binary_sha256": assembly_sha256,
            "recorded_git_head": str(recorded.get("git_head") or ""),
            "recorded_built_at": str(recorded.get("built_at") or ""),
            # Which files changed, where the record carries an inventory.
            **(inventory_mismatch or {}),
            "detail": detail,
        }
    if source_root:
        result["git"] = console_git_state(source_root)
        if provenance:
            result["matches_recorded_git_head"] = (
                provenance.get("git_head") == result["git"].get("head")
                and provenance.get("working_tree_state_sha256", "")
                == result["git"].get("working_tree_state_sha256", "")
            )
    return result


def discover_console_paths(search_roots: Iterable[str | Path] | None = None) -> dict[str, Any]:
    saved = str(load_user_settings().get("console_path", "")).strip()
    environment = str(os.environ.get("MSDIAL_CONSOLE_PATH", "")).strip()
    path_match = shutil.which("MSDIALCUI.exe") or shutil.which("MSDIALCUI")
    candidates: list[tuple[str, Path | None]] = [
        ("saved setting", Path(saved) if saved else None),
        ("MSDIAL_CONSOLE_PATH", Path(environment) if environment else None),
        ("PATH", Path(path_match) if path_match else None),
    ]
    bases = [Path.cwd(), Path(sys.executable).resolve().parent, Path(__file__).resolve().parent.parent]
    for base in list(bases):
        bases.extend(list(base.parents)[:2])
    for base in dict.fromkeys(bases):
        candidates.extend(
            [
                ("near application", base / "MSDIALCUI.exe"),
                ("near application", base / "MSDIALCUI.dll"),
                ("source build", base / "MsdialWorkbench" / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "bin" / "Release" / "net48" / "MSDIALCUI.exe"),
                ("source build", base / "MsdialWorkbench" / "tests" / "MSDIAL5" / "MsdialCoreTestApp" / "bin" / "Release" / "net8" / "MSDIALCUI.dll"),
            ]
        )
        if base.is_dir():
            candidates.extend(
                ("console distribution", path)
                for pattern in ("MSDIAL.console*/MSDIALCUI.exe", "MSDIAL.console*/MSDIALCUI.dll")
                for path in base.glob(pattern)
            )
    for raw_root in search_roots or []:
        root = Path(raw_root).expanduser()
        if root.is_file():
            candidates.append(("requested path", root))
        elif root.is_dir():
            candidates.extend(
                ("requested search root", path)
                for name in ("MSDIALCUI.exe", "MSDIALCUI.dll")
                for path in root.rglob(name)
            )
    found: list[dict[str, Any]] = []
    seen: set[str] = set()
    for source, candidate in candidates:
        if not candidate or not candidate.is_file():
            continue
        resolved = str(candidate.resolve())
        if resolved.casefold() in seen:
            continue
        seen.add(resolved.casefold())
        found.append(inspect_console_path(resolved, source))
    return {
        "configured_path": saved,
        "environment_path": environment,
        "candidates": found,
        "selected_path": found[0]["path"] if found else "",
        "expected_names": ["MSDIALCUI.exe", "MSDIALCUI.dll"],
    }


def is_supported(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_SUFFIXES or (
        path.is_dir() and path.name.lower().endswith((".d", ".raw"))
    )


def requires_mzml_conversion(path: Path) -> bool:
    name = path.name.casefold()
    return any(name.endswith(suffix) for suffix in CONVERSION_REQUIRED_SUFFIXES)


def detect_raw_format(path: str | Path) -> dict[str, Any]:
    """What a file's format implies, before anyone has decided anything.

    The peak-height and mass-slice values here are what this vendor and instrument
    family usually want. They are named as suggestions because the run applies one
    value chosen elsewhere: reporting a per-file 100 beside an applied 300 states a
    threshold that governs nothing.
    """
    target = Path(path)
    suffix = target.suffix.lower()
    if target.is_file() and suffix in {".wiff", ".wiff2"}:
        return {
            "vendor": "SCIEX",
            "format": "SCIEX WIFF" if suffix == ".wiff" else "SCIEX WIFF2",
            "instrument_family": "QTOF",
            "suggested_minimum_peak_height": 100,
            "suggested_mass_slice_width": 0.1,
            "sidecar_available": (
                suffix != ".wiff" or Path(str(target) + ".scan").is_file()
            ),
        }
    if target.is_dir() and suffix == ".raw":
        return {
            "vendor": "Waters",
            "format": "Waters .raw folder",
            "instrument_family": "QTOF",
            "suggested_minimum_peak_height": 100,
            "suggested_mass_slice_width": 0.1,
        }
    if target.is_file() and suffix == ".raw":
        return {
            "vendor": "Thermo",
            "format": "Thermo .raw file",
            "instrument_family": "Fourier-transform MS",
            "suggested_minimum_peak_height": 10000,
            "suggested_mass_slice_width": 0.05,
        }
    if target.is_file() and suffix in {".lcd", ".qgd"}:
        return {
            "vendor": "Shimadzu",
            "format": "Shimadzu LCD" if suffix == ".lcd" else "Shimadzu QGD",
            "instrument_family": "QTOF" if suffix == ".lcd" else "GC-MS",
            "suggested_minimum_peak_height": 100,
            "suggested_mass_slice_width": 0.1,
        }
    if target.is_dir() and suffix == ".d":
        if (target / "AcqData").is_dir():
            vendor, label = "Agilent", "Agilent .d folder"
        elif any((target / name).is_file() for name in ("analysis.tdf", "analysis.tsf")):
            vendor, label = "Bruker", "Bruker TDF/TSF .d folder"
        elif (target / "analysis.baf").is_file():
            vendor, label = "Bruker", "Bruker BAF .d folder"
        else:
            vendor, label = "Unknown", "Unrecognized .d folder"
        return {
            "vendor": vendor,
            "format": label,
            "instrument_family": "QTOF",
            "suggested_minimum_peak_height": 100,
            "suggested_mass_slice_width": 0.1,
        }
    return {
        "vendor": "Open format" if suffix in {".mzml", ".cdf"} else "Other",
        "format": suffix.lstrip(".").upper() or "Unknown",
        "instrument_family": "QTOF",
        "suggested_minimum_peak_height": 100,
        "suggested_mass_slice_width": 0.1,
    }


def format_based_peak_parameters(files: Iterable[dict[str, Any]]) -> dict[str, Any]:
    rows = list(files)
    if not rows:
        return {"minimum_peak_height": 100, "mass_slice_width": 0.1}
    high_resolution = any(
        item.get("instrument_family") in {"Fourier-transform MS", "FT-ICR"}
        or item.get("vendor") == "Thermo"
        for item in rows
    )
    return {
        "minimum_peak_height": 10000 if high_resolution else 100,
        "mass_slice_width": 0.05 if high_resolution else 0.1,
    }


def expand_paths(paths: Iterable[str]) -> list[dict[str, Any]]:
    return expand_paths_report(paths)["files"]


def read_analysis_csv(path: str | Path) -> dict[str, Any]:
    csv_path = Path(path).expanduser().resolve()
    if not csv_path.is_file():
        raise FileNotFoundError(f"Analysis metadata CSV was not found: {csv_path}")

    files: list[dict[str, Any]] = []
    rejected: list[str] = []
    warnings: list[str] = []
    with csv_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("Analysis metadata CSV has no header row.")
        header_map = {str(name).strip().casefold(): name for name in reader.fieldnames}
        if "file_path" not in header_map:
            raise ValueError("Analysis metadata CSV requires a file_path column.")

        for line_number, source in enumerate(reader, start=2):
            row = {
                key: source.get(original, "")
                for key, original in header_map.items()
            }
            raw_path = str(row.get("file_path", "")).strip()
            if not raw_path:
                rejected.append(f"line {line_number}: empty file_path")
                continue
            analysis_path = Path(raw_path).expanduser()
            if not analysis_path.is_absolute():
                analysis_path = csv_path.parent / analysis_path
            analysis_path = analysis_path.resolve()
            if not analysis_path.exists():
                rejected.append(f"{analysis_path} (not found; line {line_number})")
                continue
            if not is_supported(analysis_path):
                reason = (
                    "MS-DIAL has no mzXML/mzData reader; convert to mzML"
                    if requires_mzml_conversion(analysis_path)
                    else "unsupported"
                )
                rejected.append(f"{analysis_path} ({reason}; line {line_number})")
                continue

            format_info = detect_raw_format(analysis_path)
            files.append(
                {
                    "file_path": str(analysis_path),
                    "file_name": str(row.get("file_name", "")).strip() or analysis_path.stem,
                    "file_type": str(row.get("file_type", "")).strip() or "Sample",
                    "class_id": str(row.get("class_id", "")).strip() or "Sample",
                    "acquisition_type": str(row.get("acquisition_type", "")).strip() or "DDA",
                    "batch_order": _csv_number(row.get("batch_order"), 1, int),
                    "analytical_order": _csv_number(
                        row.get("analytical_order"), len(files) + 1, int
                    ),
                    "factor": _csv_number(row.get("factor"), 1, float),
                    **format_info,
                }
            )

    if not files:
        detail = f" Rejected rows: {'; '.join(rejected[:5])}" if rejected else ""
        raise ValueError(f"Analysis metadata CSV contained no usable analysis files.{detail}")
    duplicate_paths = len(files) - len({item["file_path"].casefold() for item in files})
    if duplicate_paths:
        warnings.append(f"The CSV contains {duplicate_paths} duplicate file path(s).")
    return {
        "files": files,
        "warnings": warnings,
        "rejected": rejected,
        "source_csv": str(csv_path),
    }


def _csv_number(value: Any, default: int | float, converter: Any) -> int | float:
    text = str(value or "").strip()
    if not text:
        return default
    try:
        return converter(float(text)) if converter is int else converter(text)
    except (TypeError, ValueError):
        return default


def expand_paths_report(paths: Iterable[str]) -> dict[str, Any]:
    expanded: list[Path] = []
    rejected: list[str] = []
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        if path.is_file() and is_supported(path):
            expanded.append(path)
        elif path.is_dir() and is_supported(path):
            expanded.append(path)
        elif path.is_dir():
            children = list(path.iterdir())
            expanded.extend(child for child in children if is_supported(child))
            rejected.extend(
                f"{child.resolve()} (MS-DIAL has no mzXML/mzData reader; convert to mzML)"
                for child in children
                if child.is_file() and requires_mzml_conversion(child)
            )
        elif path.is_file() and requires_mzml_conversion(path):
            rejected.append(
                f"{path} (MS-DIAL has no mzXML/mzData reader; convert to mzML)"
            )
        elif path.exists():
            rejected.append(str(path))
        else:
            rejected.append(f"{path} (not found)")
    unique = sorted(set(expanded), key=lambda item: str(item).lower())
    # The grouping is read from how the names vary across the whole set, so it has to be
    # decided once for all of them rather than file by file.
    grouping = propose_grouping([path.stem for path in unique])
    injection = propose_injection_order([path.stem for path in unique])
    result = []
    for index, path in enumerate(unique):
        format_info = detect_raw_format(path)
        name = path.stem
        result.append(
            {
                "file_path": str(path),
                "file_name": name,
                "file_type": file_type_for(name),
                "class_id": grouping["assignments"].get(name, "Sample"),
                "acquisition_type": "DDA",
                "batch_order": 1,
                "analytical_order": injection["orders"].get(name, index + 1),
                "factor": 1,
                **format_info,
            }
        )
    warnings = _sciex_pair_warnings(unique)
    return {"files": result, "warnings": warnings, "rejected": rejected}


def _sciex_pair_warnings(paths: Iterable[Path]) -> list[str]:
    samples: dict[tuple[str, str], set[str]] = {}
    for path in paths:
        suffix = path.suffix.lower()
        if suffix not in {".wiff", ".wiff2"}:
            continue
        key = (str(path.parent).lower(), path.stem.lower())
        samples.setdefault(key, set()).add(suffix)
    return [
        (
            f"Both .wiff and .wiff2 were found for sample '{sample}'. "
            "Choose exactly one SCIEX primary data file."
        )
        for (_, sample), suffixes in samples.items()
        if suffixes == {".wiff", ".wiff2"}
    ]


def read_lipid_queries(path: str | Path) -> list[dict[str, Any]]:
    query_path = Path(path)
    if not query_path.exists():
        return []
    rows = []
    with query_path.open(encoding="utf-8-sig", errors="replace") as handle:
        for raw in handle:
            if not raw.strip() or raw.startswith("#"):
                continue
            columns = raw.rstrip("\r\n").split("\t")
            if len(columns) < 4 or columns[0].lower() == "class":
                continue
            rows.append(
                {
                    "lipid_class": columns[0].strip(),
                    "adduct": columns[1].strip(),
                    "ion_mode": columns[2].strip(),
                    "selected": columns[3].strip().lower() == "true",
                }
            )
    return rows


def read_adducts(path: str | Path, ion_mode: str) -> list[dict[str, Any]]:
    resource = Path(path)
    rows: list[dict[str, Any]] = []
    with resource.open(encoding="utf-8-sig", errors="replace") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        for row in reader:
            adduct = str(row.get("Adduct", "")).strip()
            if not adduct:
                continue
            rows.append(
                {
                    "adduct": adduct,
                    "charge": int(row.get("Charge", 0)),
                    "accurate_mass": float(row.get("Accurate mass", 0)),
                    "ion_mode": ion_mode,
                    "selected": True,
                }
            )
    return rows


def read_rt_correction_anchors(path: str | Path) -> list[dict[str, Any]]:
    anchor_path = Path(path).expanduser().resolve()
    if not anchor_path.is_file():
        raise FileNotFoundError(f"RT correction anchor library was not found: {anchor_path}")
    rows: list[dict[str, Any]] = []
    with anchor_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.reader(handle, delimiter="\t")
        next(reader, None)
        for line_number, columns in enumerate(reader, start=2):
            if not columns or not any(str(value).strip() for value in columns):
                continue
            if len(columns) < 7:
                raise ValueError(
                    f"RT correction anchor line {line_number} requires 7 tab-delimited columns."
                )
            try:
                rows.append(
                    {
                        "name": columns[0].strip(),
                        "rt": float(columns[1]),
                        "rt_tolerance": float(columns[2]),
                        "mz": float(columns[3]),
                        "mz_tolerance": float(columns[4]),
                        "minimum_height": float(columns[5]),
                        "include": columns[6].strip().casefold() == "true",
                    }
                )
            except ValueError as error:
                raise ValueError(
                    f"RT correction anchor line {line_number} contains a non-numeric value."
                ) from error
    if not rows:
        raise ValueError("RT correction anchor library contains no entries.")
    return rows


def save_rt_correction_anchors(
    state: dict[str, Any], rows: list[dict[str, Any]]
) -> str:
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    source_value = str(
        state.get("rt_correction_anchor_source_path")
        or state.get("rt_correction_anchor_path")
        or "rt_correction_anchors"
    ).strip()
    source_stem = Path(source_value).stem or "rt_correction_anchors"
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    path = output_root / f"{source_stem}_{timestamp}.txt"
    suffix = 2
    while path.exists():
        path = output_root / f"{source_stem}_{timestamp}_{suffix}.txt"
        suffix += 1
    with path.open("w", encoding="ascii", errors="replace", newline="") as handle:
        writer = csv.writer(handle, delimiter="\t", lineterminator="\n")
        writer.writerow(
            ["Name", "RT(min)", "RT tol.(min)", "m/z", "m/z tol.", "Minimum height", "T/F"]
        )
        for index, row in enumerate(rows, start=1):
            name = str(row.get("name", "")).strip()
            if not name:
                raise ValueError(f"RT correction anchor row {index} requires a name.")
            rt = float(row.get("rt", 0))
            rt_tolerance = float(row.get("rt_tolerance", 0))
            mz = float(row.get("mz", 0))
            mz_tolerance = float(row.get("mz_tolerance", 0))
            minimum_height = float(row.get("minimum_height", 0))
            if rt < 0 or rt_tolerance <= 0 or mz <= 0 or mz_tolerance <= 0 or minimum_height < 0:
                raise ValueError(
                    f"RT correction anchor row {index} has an invalid RT, tolerance, m/z, or height."
                )
            writer.writerow(
                [
                    name,
                    format(rt, ".15g"),
                    format(rt_tolerance, ".15g"),
                    format(mz, ".15g"),
                    format(mz_tolerance, ".15g"),
                    format(minimum_height, ".15g"),
                    "TRUE" if bool(row.get("include", False)) else "FALSE",
                ]
            )
    return str(path)


def parse_method(path: str | Path) -> dict[str, str]:
    values: dict[str, str] = {}
    method_path = Path(path)
    if not method_path.exists():
        return values
    for line in method_path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        if line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    return values


def load_parameter_template(
    path: str | Path,
    lipid_queries_path: str | Path | None = None,
) -> dict[str, Any]:
    template_path = Path(path).expanduser().resolve()
    if not template_path.is_file():
        raise FileNotFoundError(f"Parameter template not found: {template_path}")
    values = parse_method(template_path)

    def value(*keys: str, default: str = "") -> str:
        return next((values[key] for key in keys if key in values), default)

    def number(*keys: str, default: float = 0.0) -> float:
        try:
            return float(value(*keys, default=str(default)))
        except ValueError:
            return default

    def boolean(*keys: str, default: bool = False) -> bool:
        text = value(*keys, default=str(default)).strip().casefold()
        return text in {"true", "1", "yes", "on"}

    def library_path(*keys: str) -> str:
        text = value(*keys).strip()
        if not text:
            return ""
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = template_path.parent / candidate
        return str(candidate.resolve())

    def annotator_rows(setting_key: str, path_key: str, defaults: dict[str, Any]) -> list[dict[str, Any]]:
        settings_file = library_path(setting_key)
        if not settings_file:
            return []
        settings_path = Path(settings_file)
        if not settings_path.is_file():
            raise FileNotFoundError(f"Annotator settings file not found: {settings_path}")
        rows: list[dict[str, Any]] = []
        with settings_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
            for index, source in enumerate(csv.DictReader(handle, delimiter="\t"), start=1):
                library_text = str(source.get(path_key, "")).strip()
                library = Path(library_text).expanduser()
                if library_text and not library.is_absolute():
                    library = settings_path.parent / library
                row = dict(defaults)
                row.update(
                    {
                        "annotator_id": str(source.get("annotator_id", "")).strip()
                        or f"{defaults['annotator_id'].rsplit('_', 1)[0]}_{index}",
                        path_key: str(library.resolve()) if library_text else "",
                        "priority": int(float(source.get("priority") or index)),
                        "target_omics": str(source.get("target_omics", "")).strip()
                        or row.get("target_omics", ""),
                        "evidence_tier": str(source.get("evidence_tier", "")).strip()
                        or row.get("evidence_tier", ""),
                    }
                )
                for key in (
                    "rt_tolerance",
                    "ms1_tolerance",
                    "ms2_tolerance",
                    "weighted_dot_product_cutoff",
                    "simple_dot_product_cutoff",
                    "reverse_dot_product_cutoff",
                    "matched_peaks_percentage_cutoff",
                    "minimum_spectrum_match",
                    "total_score_cutoff",
                ):
                    if source.get(key, "").strip():
                        row[key] = float(source[key])
                if "minimum_spectrum_match" in row:
                    row["minimum_spectrum_match"] = int(row["minimum_spectrum_match"])
                row["use_rt_scoring"] = str(
                    source.get("use_retention_information_for_scoring", row.get("use_rt_scoring", False))
                ).casefold() in {"true", "1", "yes", "on"}
                row["use_rt_filtering"] = str(
                    source.get("use_retention_information_for_filtering", row.get("use_rt_filtering", False))
                ).casefold() in {"true", "1", "yes", "on"}
                rows.append(row)
        return rows

    machine = value("machine category", "ionization").casefold()
    project_type = "gcms" if "gc" in machine or value("ionization").casefold() == "ei" else "lcms"
    target_omics = value("target omics", default="Metabolomics")
    workflow_values: dict[str, Any] = {
        "project_type": project_type,
        "ion_mode": value("ion mode", default="Positive"),
        "target_omics": target_omics,
        "ms1_data_type": value("ms1 data type", default="Centroid"),
        "ms2_data_type": value("ms2 data type", default="Centroid"),
        "number_of_threads": int(number("number of threads", default=4)),
        "smoothing_method": value("smoothing method", default="LinearWeightedMovingAverage"),
        "minimum_peak_height": number("minimum peak height", default=300),
        "mass_slice_width": number("mass slice width", default=0.1),
        "minimum_peak_width": int(number("minimum peak width", default=5)),
        "retention_time_begin": number("retention time begin", default=0),
        "retention_time_end": number("retention time end", default=100),
        "ms1_tolerance": number("ms1 tolerance for centroid", default=0.01),
        "ms2_tolerance": number("ms2 tolerance for centroid", default=0.025),
        "alignment_rt_tolerance": number("retention time tolerance for alignment", default=0.1),
        "alignment_ms1_tolerance": number("ms1 tolerance for alignment", default=0.015),
        "alignment_light_mode": boolean("alignment light mode"),
        "execute_automatic_rt_correction": boolean(
            "execute automatic rt correction for alignment"
        ),
        # automatic_rt_correction_value, not int(): int() truncated 2.9 to 2, which then passed
        # validation, and raised on nan and inf.
        "automatic_rt_correction_reference_file_id": automatic_rt_correction_value(
            "automatic_rt_correction_reference_file_id",
            number(
                "automatic rt correction reference file id",
                default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_reference_file_id"],
            ),
        ),
        "automatic_rt_correction_rt_bin_width": number(
            "automatic rt correction rt bin width",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_rt_bin_width"],
        ),
        "automatic_rt_correction_match_rt_tolerance": number(
            "automatic rt correction match rt tolerance",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_match_rt_tolerance"],
        ),
        "automatic_rt_correction_minimum_anchors": automatic_rt_correction_value(
            "automatic_rt_correction_minimum_anchors",
            number(
                "automatic rt correction minimum anchors",
                default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_minimum_anchors"],
            ),
        ),
        "automatic_rt_correction_maximum_anchors": automatic_rt_correction_value(
            "automatic_rt_correction_maximum_anchors",
            number(
                "automatic rt correction maximum anchors",
                default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_maximum_anchors"],
            ),
        ),
        "automatic_rt_correction_minimum_sample_coverage": number(
            "automatic rt correction minimum sample coverage",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_minimum_sample_coverage"],
        ),
        "automatic_rt_correction_intensity_quantile": number(
            "automatic rt correction intensity quantile",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_intensity_quantile"],
        ),
        "automatic_rt_correction_maximum_peak_width_quantile": number(
            "automatic rt correction maximum peak width quantile",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_maximum_peak_width_quantile"],
        ),
        "automatic_rt_correction_minimum_signal_to_noise": number(
            "automatic rt correction minimum signal to noise",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_minimum_signal_to_noise"],
        ),
        "automatic_rt_correction_minimum_gaussian_similarity": number(
            "automatic rt correction minimum gaussian similarity",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_minimum_gaussian_similarity"],
        ),
        "automatic_rt_correction_minimum_ideal_slope": number(
            "automatic rt correction minimum ideal slope",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_minimum_ideal_slope"],
        ),
        "automatic_rt_correction_outlier_mad_threshold": number(
            "automatic rt correction outlier mad threshold",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_outlier_mad_threshold"],
        ),
        "automatic_rt_correction_reference_centrality_weight": number(
            "automatic rt correction reference centrality weight",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS["automatic_rt_correction_reference_centrality_weight"],
        ),
        "automatic_rt_correction_interpolate_blanks_by_analytical_order": boolean(
            "automatic rt correction interpolate blanks by analytical order",
            default=AUTOMATIC_RT_CORRECTION_DEFAULTS[
                "automatic_rt_correction_interpolate_blanks_by_analytical_order"
            ],
        ),
        "export_folder_path": library_path("export folder path"),
        "height_matrix_export": boolean("height matrix export"),
        "solvent": value("solvent type", default="CH3COONH4"),
        "gcms_accuracy_type": value("accuracy type", default="IsNominal"),
        "gcms_ri_compound_type": value("ri compound type", "ri compound", default="Alkanes"),
        "gcms_retention_type": value("retention type", default="RT"),
        "gcms_alignment_index_type": value("alignment index type", default="RT"),
        "gcms_ri_alignment_tolerance": number("retention index alignment tolerance", default=10),
        "gcms_ri_dictionary_path": library_path("ri index file pathes"),
    }

    msp_path = library_path("msp file path")
    msp = {
        "annotator_id": "msp_annotator_1",
        "msp_file_path": msp_path,
        "priority": 1,
        "rt_tolerance": number("rt tolerance for msp-based annotation", default=0.5),
        "use_rt_scoring": boolean("use retention information for msp-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for msp-based annotation filtering"),
        "weighted_dot_product_cutoff": number("weighted dot product cutoff for msp-based annotation", default=0.6),
        "simple_dot_product_cutoff": number("simple dot product cutoff for msp-based annotation", default=0.6),
        "reverse_dot_product_cutoff": number("reverse dot product cutoff for msp-based annotation", default=0.8),
        "matched_peaks_percentage_cutoff": number("matched peaks percentage cutoff for msp-based annotation", default=0.1),
        "minimum_spectrum_match": int(number("minimum spectrum match for msp-based annotation", default=3)),
    }
    text_path = library_path("text db file path")
    text = {
        "annotator_id": "text_annotator_1",
        "text_db_file_path": text_path,
        "priority": 1,
        "rt_tolerance": number("rt tolerance for text-based annotation", default=0.5),
        "ms1_tolerance": number("accurate ms1 tolerance for text-based annotation", default=0.01),
        "total_score_cutoff": number("total score cutoff for text-based annotation", default=0.8),
        "use_rt_scoring": boolean("use retention information for text-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for text-based annotation filtering"),
    }
    lbm = {
        "lbm_file_path": library_path("lbm file path"),
        "priority": int(number("lbm annotator priority", "lbm annotation priority", default=1)),
        "rt_tolerance": number("rt tolerance for lbm-based annotation", default=100),
        "ms1_tolerance": number("ms1 tolerance for lbm-based annotation", default=0.01),
        "ms2_tolerance": number("ms2 tolerance for lbm-based annotation", default=0.025),
        "weighted_dot_product_cutoff": number("weighted dot product cutoff for lbm-based annotation", default=0.15),
        "simple_dot_product_cutoff": number("simple dot product cutoff for lbm-based annotation", default=0.15),
        "reverse_dot_product_cutoff": number("reverse dot product cutoff for lbm-based annotation", default=0.3),
        "matched_peaks_percentage_cutoff": number("matched peaks percentage cutoff for lbm-based annotation", default=0),
        "minimum_spectrum_match": int(number("minimum spectrum match for lbm-based annotation", default=1)),
        "use_rt_scoring": boolean("use retention information for lbm-based annotation scoring"),
        "use_rt_filtering": boolean("use retention information for lbm-based annotation filtering"),
    }
    msp_rows = annotator_rows(
        "msp annotator settings file path", "msp_file_path", msp
    )
    text_rows = annotator_rows(
        "text annotator settings file path", "text_db_file_path", text
    )

    searched_lipids = [
        item.strip()
        for item in value("searched lipid class").split(";")
        if item.strip()
    ]
    searched_adducts = [
        item.strip()
        for item in value("searched adduct ions", "adduct list").split(",")
        if item.strip()
    ]
    lipid_queries = read_lipid_queries(lipid_queries_path) if lipid_queries_path else []
    selected_set = set(searched_lipids)
    if target_omics.casefold() == "lipidomics" and not selected_set:
        selected_set = {
            f"{item['lipid_class']} {item['adduct']}" for item in lipid_queries
        }
    for item in lipid_queries:
        item["selected"] = f"{item['lipid_class']} {item['adduct']}" in selected_set

    return {
        "path": str(template_path),
        "workflow": workflow_values,
        "msp_annotators": msp_rows or [msp],
        "text_annotators": text_rows or [text],
        "lbm_annotator": lbm,
        "selected_lipids": searched_lipids,
        "selected_adducts": searched_adducts,
        "lipid_queries": lipid_queries,
    }


def validate_workflow(state: dict[str, Any]) -> list[dict[str, str]]:
    issues: list[dict[str, str]] = []
    files = state.get("files", [])
    project_type = str(state.get("project_type", "lcms")).lower()
    if state.get("run_qa") and project_type == "lcms":
        if not state.get("height_matrix_export"):
            issues.append(
                {
                    "level": "error",
                    "message": "LC-MS QA requires Height matrix export to be enabled.",
                }
            )
        if not str(state.get("export_folder_path", "")).strip():
            issues.append(
                {
                    "level": "warning",
                    "message": (
                        "LC-MS QA Export folder path is blank; Output root will be "
                        "written into the generated method before execution."
                    ),
                }
            )
        console_path = str(state.get("console_path", "")).strip()
        if console_path and LCMS_QA_CAPABILITY not in console_capabilities(console_path)["capabilities"]:
            issues.append(
                {
                    "level": "error",
                    "message": (
                        "The selected MS-DIAL Console does not support LC-MS QA matrix export. "
                        "Choose a Console build containing the LC-MS QA matrix exporter, "
                        "or disable QA matrix export for this run."
                    ),
                }
            )
    if project_type not in {"lcms", "gcms"}:
        issues.append(
            {
                "level": "error",
                "message": (
                    f"{project_type.upper()} parameter UI is scaffolded, but this version "
                    "does not execute this project type yet. A mode-specific parameter "
                    "template and backend are required before it is runnable."
                ),
            }
        )
    if state.get("alignment_light_mode") and project_type != "lcms":
        issues.append(
            {
                "level": "error",
                "message": "Alignment light mode is currently available for LC-MS Console runs only.",
            }
        )
    if state.get("execute_rt_correction"):
        if project_type != "lcms":
            issues.append(
                {
                    "level": "error",
                    "message": "Retention-time correction is currently available only for LC-MS.",
                }
            )
        anchor_path = str(state.get("rt_correction_anchor_path", "")).strip()
        if not anchor_path:
            issues.append(
                {
                    "level": "error",
                    "message": "Set the RT correction anchor library path.",
                }
            )
        elif not Path(anchor_path).exists():
            issues.append(
                {
                    "level": "error",
                    "message": f"RT correction anchor library not found: {anchor_path}",
                }
            )
        selection_path = str(state.get("rt_correction_selection_path", "")).strip()
        if selection_path and not Path(selection_path).exists():
            issues.append(
                {
                    "level": "error",
                    "message": f"RT correction peak selection file not found: {selection_path}",
                }
            )
        selection_mode = str(
            state.get("rt_correction_peak_selection_mode", "HighestIntensity")
        )
        if selection_mode not in {
            "HighestIntensity",
            "ClosestToReferenceRt",
            "Weighted",
        }:
            issues.append(
                {
                    "level": "error",
                    "message": f"Unknown RT correction peak selection mode: {selection_mode}",
                }
            )
        rt_weight = float(state.get("rt_correction_peak_selection_rt_weight", 0.5))
        if not 0 <= rt_weight <= 1:
            issues.append(
                {
                    "level": "error",
                    "message": "RT correction peak selection RT weight must be between 0 and 1.",
                }
            )
    if state.get("execute_automatic_rt_correction"):
        if project_type != "lcms":
            issues.append(
                {
                    "level": "error",
                    "message": "Automatic alignment RT correction is currently available only for LC-MS.",
                }
            )
        if state.get("execute_rt_correction"):
            issues.append(
                {
                    "level": "error",
                    "message": (
                        "User-defined RT correction and automatic alignment RT correction "
                        "cannot be enabled together because that would correct the RT axis twice."
                    ),
                }
            )
        if not state.get("together_with_alignment", True):
            issues.append(
                {
                    "level": "error",
                    "message": "Automatic alignment RT correction requires Together with alignment to be enabled.",
                }
            )
        issues.extend(_automatic_rt_correction_value_issues(state))
        console_path = Path(str(state.get("console_path", "")).strip()).expanduser()
        if console_path.is_file() and (
            AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY
            not in console_capabilities(str(console_path))["capabilities"]
        ):
            issues.append(
                {
                    "level": "error",
                    "message": (
                        "Automatic alignment RT correction requires an MS-DIAL Console build "
                        "that implements this feature. Select a compatible source build or "
                        "disable automatic alignment RT correction."
                    ),
                }
            )
        order_proposal = state.get("sample_table_proposal") or {}
        order_source = str(
            (order_proposal.get("analytical_order") or {}).get("derived_from", "")
        ).casefold()
        if (
            state.get("repository_run_manifest")
            and state.get(
                "automatic_rt_correction_interpolate_blanks_by_analytical_order", True
            )
            # "embedded" is a number read out of the file names, with every Blank and QC
            # moved to the end: an inference as much as the listing is, and for Blank
            # interpolation the worst one, since it places every Blank after every sample.
            and order_source in {"", "listing", "embedded"}
            # With no Blank there is nothing to interpolate, and a warning that cannot
            # matter teaches the reader to skip the ones that do.
            and any(is_blank_file_type(item.get("file_type")) for item in state.get("files", []))
        ):
            issues.append(
                {
                    "level": "warning",
                    "message": (
                        "Blank RT-correction models would be interpolated using analytical "
                        "order inferred from the repository file names or listing, not read "
                        "from the instrument. Confirm the injection order or disable Blank "
                        "interpolation before production analysis."
                    ),
                }
            )
    if state.get("alignment_light_mode") and not state.get("together_with_alignment", True):
        issues.append(
            {
                "level": "error",
                "message": "Alignment light mode requires Together with alignment to be enabled.",
            }
        )
    required_paths = [
        ("console_path", "MS-DIAL Console executable"),
        ("template_path", "parameter template"),
        ("output_root", "output root"),
    ]
    for key, label in required_paths:
        value = str(state.get(key, "")).strip()
        if not value:
            issues.append({"level": "error", "message": f"Missing {label}."})
        elif key != "output_root" and not Path(value).exists():
            issues.append({"level": "error", "message": f"Not found: {value}"})
    if not files:
        issues.append({"level": "error", "message": "No analysis files were added."})
    names = set()
    acquisition_types = set()
    sciex_samples: dict[tuple[str, str], set[str]] = {}
    for item in files:
        path = Path(item.get("file_path", ""))
        name = str(item.get("file_name", ""))
        if not path.exists():
            issues.append({"level": "error", "message": f"Input not found: {path}"})
        if (
            path.is_file()
            and path.suffix.lower() == ".wiff"
            and not Path(str(path) + ".scan").is_file()
        ):
            issues.append(
                {
                    "level": "error",
                    "message": (
                        f"WIFF.SCAN is not accessible next to {path}. "
                        "Use Add original files, Add original folder, or Add path so the "
                        "original WIFF directory remains directly accessible."
                    ),
                }
            )
        if path.is_dir() and path.suffix.lower() == ".d" and item.get("vendor") == "Unknown":
            issues.append(
                {
                    "level": "error",
                    "message": f"Unrecognized .d folder (no AcqData/analysis.tdf/analysis.tsf/analysis.baf): {path}",
                }
            )
        if "," in str(path) or "," in name or "," in str(item.get("class_id", "")):
            issues.append(
                {
                    "level": "error",
                    "message": f"Console CSV cannot quote commas: {name}",
                }
            )
        if name.lower() in names:
            issues.append({"level": "error", "message": f"Duplicate file_name: {name}"})
        names.add(name.lower())
        acquisition_types.add(item.get("acquisition_type", "DDA"))
        if path.suffix.lower() in {".wiff", ".wiff2"}:
            key = (str(path.parent).lower(), path.stem.lower())
            sciex_samples.setdefault(key, set()).add(path.suffix.lower())
    for (_, sample), suffixes in sciex_samples.items():
        if suffixes == {".wiff", ".wiff2"}:
            issues.append(
                {
                    "level": "error",
                    "message": (
                        f"Both .wiff and .wiff2 are selected for sample '{sample}'. "
                        "Keep exactly one SCIEX primary data file."
                    ),
                }
            )
    if len(acquisition_types) > 1:
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Multiple acquisition types require the per-file acquisition_type fix "
                    "(fix/console-per-file-acquisition-type) or a release containing it."
                ),
            }
        )
    if any(item.get("vendor") == "Agilent" for item in files):
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Agilent .d reading on Windows may require Microsoft Visual C++ "
                    f"2013 Redistributable Package x64: {VC2013_DOWNLOAD_URL}"
                ),
            }
        )
        if platform.system() != "Windows":
            issues.append(
                {
                    "level": "warning",
                    "message": (
                        "Agilent .d uses a vendor reader whose OS support depends on the "
                        "selected MS-DIAL Console package. Convert to mzML when the reader "
                        "is unavailable on this OS."
                    ),
                }
            )
        console_value = str(state.get("console_path", "")).strip()
        console_path = Path(console_value) if console_value else None
        if console_path and console_path.exists():
            console_directory = console_path.parent
            root_reader = console_directory / "BaseDataAccess.dll"
            packaged_reader = console_directory / "lib" / "Agilent" / "BaseDataAccess.dll"
            if not root_reader.exists() and not packaged_reader.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": (
                            "Agilent reader dependency BaseDataAccess.dll was not found "
                            f"beside the Console or under lib/Agilent: {console_directory}"
                        ),
                    }
                )
            elif not root_reader.exists() and packaged_reader.exists():
                issues.append(
                    {
                        "level": "warning",
                        "message": (
                            "BaseDataAccess.dll exists only under lib/Agilent. If the run "
                            "reports a BaseDataAccess load error, use an official packaged "
                            "Console build or deploy the Agilent reader so the runtime can "
                            "resolve it."
                        ),
                    }
                )
    has_folder_type_input = any(Path(str(item.get("file_path", ""))).is_dir() for item in files)
    if has_folder_type_input and not _console_supports_folder_type_csv(
        str(state.get("console_path", ""))
    ):
        issues.append(
            {
                "level": "error",
                "message": (
                    "The selected MS-DIAL Console does not support folder-type raw-data "
                    "paths in analysis_files.csv. Use the patched source build: "
                    f"{_patched_console_path_hint()}"
                ),
            }
        )
    elif has_folder_type_input:
        issues.append(
            {
                "level": "warning",
                "message": (
                    "Folder-type raw data (.d/.raw) will be passed through analysis_files.csv. "
                    "This requires the patched MS-DIAL Console source build so per-file "
                    "metadata can be honored."
                ),
            }
        )
    for key, label in (
        ("msp_path", "MSP library"),
        ("lbm_path", "LBM library"),
        ("text_db_path", "Text DB"),
    ):
        value = str(state.get(key, "")).strip()
        if value and not Path(value).exists():
            issues.append({"level": "error", "message": f"{label} not found: {value}"})
    for index, annotator in enumerate(state.get("msp_annotators", []), start=1):
        value = str(annotator.get("msp_file_path", "")).strip()
        if value and not Path(value).exists():
            label = str(annotator.get("annotator_id", "")).strip() or f"MSP annotator row {index}"
            issues.append({"level": "error", "message": f"{label} MSP file not found: {value}"})
    for index, annotator in enumerate(state.get("text_annotators", []), start=1):
        value = str(annotator.get("text_db_file_path", "")).strip()
        if value and not Path(value).exists():
            label = str(annotator.get("annotator_id", "")).strip() or f"Text annotator row {index}"
            issues.append({"level": "error", "message": f"{label} Text library file not found: {value}"})
    selected = state.get("selected_lipids", [])
    if project_type != "gcms" and state.get("target_omics") == "Lipidomics" and not selected:
        issues.append({"level": "error", "message": "No lipid annotation query is selected."})
    if (
        project_type != "gcms"
        and "selected_adducts" in state
        and not state.get("selected_adducts")
    ):
        issues.append({"level": "error", "message": "No adduct ion is selected."})
    if state.get("smoothing_method", "LinearWeightedMovingAverage") not in SMOOTHING_METHODS:
        issues.append(
            {
                "level": "error",
                "message": f"Unsupported smoothing method: {state.get('smoothing_method')}",
            }
        )
    if project_type == "gcms":
        uses_ri = (
            str(state.get("gcms_retention_type", "RT")).upper() == "RI"
            or str(state.get("gcms_alignment_index_type", "RT")).upper() == "RI"
        )
        if uses_ri:
            source = str(state.get("gcms_ri_source", "single"))
            if source == "dictionary":
                dictionary = str(state.get("gcms_ri_dictionary_path", "")).strip()
                if not dictionary:
                    issues.append({"level": "error", "message": "Set the GC-MS RI dictionary path."})
                elif not Path(dictionary).exists():
                    issues.append({"level": "error", "message": f"RI dictionary not found: {dictionary}"})
            elif source == "perFile":
                mapping = {
                    str(item.get("file_path", "")): str(item.get("ri_path", "")).strip()
                    for item in state.get("gcms_ri_file_map", [])
                }
                for file in files:
                    raw_path = str(file.get("file_path", ""))
                    ri_path = mapping.get(raw_path, "")
                    if not ri_path:
                        issues.append(
                            {
                                "level": "error",
                                "message": f"Missing RI carbon-RT file for {file.get('file_name', raw_path)}.",
                            }
                        )
                    elif not Path(ri_path).exists():
                        issues.append({"level": "error", "message": f"RI carbon-RT file not found: {ri_path}"})
            else:
                standard = str(state.get("gcms_ri_standard_path", "")).strip()
                if not standard:
                    issues.append({"level": "error", "message": "Set the alkane/FAME carbon-RT file for RI calculation."})
                elif not Path(standard).exists():
                    issues.append({"level": "error", "message": f"RI carbon-RT file not found: {standard}"})
    return issues


def _stage_input(source: Path, destination_folder: Path) -> Path:
    """Copy one input, and whatever travels with it, into a working folder.

    MS-DIAL writes its per-file intermediates beside the file it read, so an analysis
    run against data in place leaves .dcl, .pai2 and tag files in the original folder.
    On a shared or archival location that is not acceptable, and no output setting
    prevents it -- reading from a copy is the only remedy.

    Sidecars travel with their file: a .wiff is unreadable without its .wiff.scan, and
    copying one without the other produces an input that fails deep inside the vendor
    reader rather than here.
    """
    if source.is_dir():
        target = destination_folder / source.name
        if not target.exists():
            shutil.copytree(source, target)
        return target
    for candidate in sorted(source.parent.glob(source.name + "*")):
        if candidate.is_file():
            target = destination_folder / candidate.name
            if not target.exists() or target.stat().st_size != candidate.stat().st_size:
                shutil.copy2(candidate, target)
    staged = destination_folder / source.name
    if not staged.exists():
        shutil.copy2(source, staged)
    return staged


def prepare_run(
    state: dict[str, Any],
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    issues = validate_workflow(state)
    errors = [issue["message"] for issue in issues if issue["level"] == "error"]
    if errors:
        raise ValueError("\n".join(errors))
    project_type = str(state.get("project_type", "lcms")).lower()
    run_directory = Path(state["output_root"]).expanduser().resolve()
    run_directory.mkdir(parents=True, exist_ok=True)
    effective_files: list[Path] = []
    files = state["files"]
    stage_inputs = bool(state.get("stage_inputs", False))
    staging_folder = run_directory / "input" if stage_inputs else None
    if staging_folder is not None:
        staging_folder.mkdir(parents=True, exist_ok=True)
    for index, item in enumerate(files):
        source = Path(item["file_path"]).resolve()
        if staging_folder is None:
            if progress:
                progress(f"Using original input {index + 1}/{len(files)}: {source}")
            effective_files.append(source)
            continue
        if progress:
            progress(f"Staging input {index + 1}/{len(files)}: {source}")
        effective_files.append(_stage_input(source, staging_folder))

    csv_path = run_directory / "analysis_files.csv"
    _write_analysis_csv(csv_path, files, effective_files)
    method_state = dict(state)
    if method_state.get("height_matrix_export") and not str(
        method_state.get("export_folder_path", "")
    ).strip():
        method_state["export_folder_path"] = str(run_directory)
    method_state["msdial_console_version"] = console_version(state["console_path"]) or "not recorded"
    method_state["msdial_interactive_version"] = __version__
    repository_metadata_files: dict[str, str] = {}
    if isinstance(state.get("repository_metadata"), dict):
        from .repository_metadata import save_metadata_review

        repository_metadata_files = save_metadata_review(
            state["repository_metadata"], run_directory
        )
        method_state["repository_metadata_files"] = repository_metadata_files
    ri_dictionary = _prepare_gcms_ri_dictionary(
        run_directory,
        method_state,
        files,
        effective_files,
    )
    method_path = run_directory / "method.txt"
    _write_method(method_path, method_state)
    command = build_console_command(
        state["console_path"],
        csv_path,
        run_directory,
        method_path,
        project_type,
        bool(state.get("project_store", True)),
    )
    project_file_requested = bool(state.get("project_store", True))
    analysis_extension = ".mdscan" if project_type == "gcms" else ".mdpeak"
    expected_analysis_exports = [
        str(run_directory / f"{item['file_name']}{analysis_extension}")
        for item in files
    ]
    expected_automatic_rt_correction_exports: list[str] = []
    if project_type == "lcms" and method_state.get("execute_automatic_rt_correction"):
        expected_automatic_rt_correction_exports = [
            str(run_directory / AUTOMATIC_RT_CORRECTION_SUMMARY),
            str(run_directory / AUTOMATIC_RT_CORRECTION_ANCHORS),
        ]
        expected_analysis_exports.extend(expected_automatic_rt_correction_exports)
    manifest_path = run_directory / "run-manifest.json"
    # A version string and a path cannot identify a binary: the string is whatever the
    # assembly claims, the path can be rebuilt under. inspect_console_path already
    # computes the checksum, the build record and the git state of the working tree it
    # came from; the manifest simply never carried any of it, so a run could not be
    # traced back to the code that produced it.
    console = inspect_console_path(state["console_path"])
    manifest = {
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "platform": platform.platform(),
        "analysis_type": project_type,
        "msdial_console_version": method_state["msdial_console_version"],
        "msdial_interactive_version": method_state["msdial_interactive_version"],
        "console": {
            "path": console.get("path", ""),
            "source_kind": console.get("source_kind", ""),
            "binary_sha256": console.get("binary_sha256", ""),
            "binary_size": console.get("binary_size", 0),
            "binary_modified_at": console.get("binary_modified_at", ""),
            # The code that ran: MSDIALCUI.dll beside a net8 launcher, else the binary.
            "assembly_path": console.get("assembly_path", ""),
            "assembly_sha256": console.get("assembly_sha256", ""),
            "provenance_status": console.get("provenance_status", "absent"),
            "provenance": console.get("provenance", {}),
            "provenance_mismatch": console.get("provenance_mismatch", {}),
            "provenance_warnings": console.get("provenance_warnings", []),
            # Every file the Console ran with, by one digest (console_inventory), and the
            # versions of the assemblies that decide what it did.
            "inventory_sha256": console.get("inventory_sha256", ""),
            "inventory_file_count": console.get("inventory_file_count", 0),
            "key_assemblies": console.get("key_assemblies", {}),
            "git": console.get("git", {}),
        },
        # One field a reader sees without digging: whether the software this run used
        # can be identified at all.
        "software_provenance_status": console.get("provenance_status", "absent"),
        "libraries": _manifest_libraries(method_state),
        "project_file_requested": project_file_requested,
        "stage_inputs": stage_inputs,
        "input_csv": str(csv_path),
        "console_input": str(csv_path),
        "temporary_input_folder": str(staging_folder) if staging_folder is not None else "",
        "method_file": str(method_path),
        "output_folder": str(run_directory),
        "source_files": [item["file_path"] for item in files],
        "effective_files": [str(path) for path in effective_files],
        "ri_dictionary_file": str(ri_dictionary) if ri_dictionary is not None else "",
        "msp_annotator_settings_file": str(method_state.get("msp_annotator_settings_file_path", "")),
        "text_annotator_settings_file": str(method_state.get("text_annotator_settings_file_path", "")),
        "rt_correction_anchor_file": str(method_state.get("rt_correction_anchor_path", "")),
        "rt_correction_selection_file": str(method_state.get("rt_correction_selection_path", "")),
        "repository_metadata_files": repository_metadata_files,
        "command": command,
        "expected_analysis_exports": expected_analysis_exports,
        "expected_automatic_rt_correction_exports": expected_automatic_rt_correction_exports,
        "export_folder_path": str(method_state.get("export_folder_path", "")),
        "qa_matrix_expected": bool(
            project_type == "lcms" and method_state.get("height_matrix_export")
        ),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    reproduction = _write_reproduction_files(
        run_directory,
        method_state,
        command,
    )
    from .repository_reanalysis import normalize_raw_retention_policy

    retention_policy, retention_unrecognized = normalize_raw_retention_policy(
        state.get("repository_raw_retention_policy")
    )
    retention_warnings = (
        [
            {
                "level": "warning",
                "message": (
                    "Repository raw-data retention policy "
                    f"{str(state.get('repository_raw_retention_policy'))!r} was not recognised; "
                    f"{retention_policy!r} applies instead. Downloaded raw data will be kept."
                ),
            }
        ]
        if retention_unrecognized
        else []
    )
    return {
        "run_directory": str(run_directory),
        "analysis_type": project_type,
        # Written into the manifest above; repeated here because a status nobody reads
        # is the same as a status nobody recorded.
        "software_provenance": {
            "status": console.get("provenance_status", "absent"),
            "binary_sha256": console.get("binary_sha256", ""),
            "assembly_path": console.get("assembly_path", ""),
            "assembly_sha256": console.get("assembly_sha256", ""),
            "inventory_sha256": console.get("inventory_sha256", ""),
            "version": method_state["msdial_console_version"],
            "warning": _console_provenance_warning(console),
        },
        "expected_analysis_exports": expected_analysis_exports,
        "expected_automatic_rt_correction_exports": expected_automatic_rt_correction_exports,
        "export_folder_path": str(method_state.get("export_folder_path", "")),
        "qa_matrix_expected": bool(
            project_type == "lcms" and method_state.get("height_matrix_export")
        ),
        "diagnostic_result_file": expected_analysis_exports[0] if len(files) == 1 else "",
        "input_csv": str(csv_path),
        "console_input": str(csv_path),
        "temporary_input_folder": str(staging_folder) if staging_folder is not None else "",
        # A staged copy was asked for, so it is the analyst's copy to keep: deleting it
        # after the run would throw away the very thing that lets the next run avoid
        # touching the original data again.
        "preserve_temporary_input_folder": stage_inputs,
        "project_file_requested": project_file_requested,
        "method_file": str(method_path),
        "manifest": str(manifest_path),
        "command": command,
        # CARRIED THROUGH RATHER THAN RE-COPIED BY EACH CALLER.
        #
        # These two decide, in _run_job, whether the unit manifest gets its mzTab validation, its
        # retained-artifact inventory and its retention verdict at all: the whole block is behind
        # `if manifest_text:`. prepare_run returns an explicit dict, so they used to be dropped here
        # and the GUI path copied them back onto the result by hand afterwards. The agent path --
        # every MCP-driven repository run, which is the only path the reanalysis agents use -- did
        # not, so an agent-driven run produced no validation record, no artifact inventory, and raw
        # data that could never be cleaned up, while the same unit run from the GUI produced all
        # three. Two code paths that were supposed to be one.
        #
        # Returning them from here means a caller cannot forget: there is no second place to
        # remember. The state already carries them, put there by the MCP tool's workflow_overrides
        # or by the GUI's own state.
        "repository_run_manifest": str(state.get("repository_run_manifest") or ""),
        # NORMALISED, and an unreadable request is reported rather than quietly becoming "keep".
        # The download endpoint validates this, so the governed chain cannot produce a bad value --
        # but a caller that hand-writes answers["workflow_overrides"] reaches here unchecked, and the
        # comparison downstream is a bare string equality with no casefolding and no strip. "delete",
        # "Delete" and a trailing space were all keep-equivalent, and the run logged that the data
        # were kept without ever saying the policy had not been understood.
        "repository_raw_retention_policy": retention_policy,
        **reproduction,
        "warnings": [issue for issue in issues if issue["level"] == "warning"] + retention_warnings,
    }


def _manifest_libraries(state: dict[str, Any]) -> list[dict[str, Any]]:
    """The run manifest's library records.

    A repository unit's run is built to be redistributed, and its manifest travels in the workflow bundle,
    so it names each library the run loads the way every shared artifact does: file name, sha256, size and
    distribution, and no location. The location stays in workflow-settings.json, which is this machine's. A
    laboratory run's manifest keeps the provenance entries with their paths, as it always has.
    """
    if str(state.get("repository_run_manifest") or "").strip():
        from .sharing import library_records

        return [library.identity for library in library_records(state)]
    return [
        {
            **{key: entry.get(key, "") for key in ("path", "version", "source", "doi", "license")},
            **file_identity(entry.get("path", "")),
        }
        for entry in state.get("library_provenance", [])
        if isinstance(entry, dict) and str(entry.get("path", "")).strip()
    ]


def prepare_tuning_run(
    state: dict[str, Any],
    file_path: str,
    output_root: str | Path,
) -> dict[str, Any]:
    tuning = copy.deepcopy(state)
    selected = next(
        (item for item in tuning.get("files", []) if item.get("file_path") == file_path),
        None,
    )
    if selected is None:
        raise ValueError("Select one analysis file for parameter tuning.")
    tuning["files"] = [selected]
    tuning["output_root"] = str(output_root)
    tuning["stage_inputs"] = False
    tuning["project_store"] = False
    tuning["together_with_alignment"] = False
    tuning["execute_rt_correction"] = False
    # Both are alignment features. The diagnostic runs one file without alignment, and with
    # either still on it was refused as "requires Together with alignment", so a production
    # state that had enabled automatic RT correction could not be tuned at all.
    tuning["execute_automatic_rt_correction"] = False
    tuning["alignment_light_mode"] = False
    if str(tuning.get("project_type", "lcms")).lower() == "gcms":
        tuning["minimum_peak_height"] = state.get("minimum_peak_height", 1000)
    else:
        tuning["minimum_peak_height"] = 0
    tuning["msp_weighted_dot_product"] = 0
    tuning["msp_simple_dot_product"] = 0
    tuning["msp_reverse_dot_product"] = 0
    tuning["msp_matched_peaks_percentage"] = 0
    tuning["msp_minimum_spectrum_match"] = 0
    for annotator in tuning.get("msp_annotators", []):
        annotator["weighted_dot_product_cutoff"] = 0
        annotator["simple_dot_product_cutoff"] = 0
        annotator["reverse_dot_product_cutoff"] = 0
        annotator["matched_peaks_percentage_cutoff"] = 0
        annotator["minimum_spectrum_match"] = 0
    prepared = prepare_run(tuning)
    if prepared.get("temporary_input_folder"):
        prepared["diagnostic_input_folder"] = prepared["temporary_input_folder"]
        prepared.setdefault("warnings", []).append(
            {
                "level": "warning",
                "message": (
                    "Folder-type input uses a temporary directory link so older "
                    "MS-DIAL Console builds can read .d/.raw data without CSV folder-path support."
                ),
            }
        )
    return prepared


def _folder_type_inputs(paths: list[Path]) -> list[Path]:
    return [
        path
        for path in paths
        if path.is_dir() and path.name.lower().endswith((".d", ".raw"))
    ]


def _patched_console_path_hint() -> str:
    return str(
        Path(__file__).resolve().parent.parent.parent
        / "MsdialWorkbench"
        / "tests"
        / "MSDIAL5"
        / "MsdialCoreTestApp"
        / "bin"
        / "Release"
        / "net48"
        / "MSDIALCUI.exe"
    )


def _console_supports_folder_type_csv(console_path: str) -> bool:
    if os.environ.get("MSDIAL_ASSUME_FOLDER_TYPE_CSV_SUPPORTED") == "1":
        return True
    path_text = str(console_path or "")
    lowered = path_text.replace("/", "\\").lower()
    return (
        "\\msdialworkbench\\tests\\msdial5\\msdialcoretestapp\\bin\\" in lowered
        and path_text.lower().endswith(("msdialcui.exe", "msdialcui.dll"))
    )


def _prepare_temporary_console_input_folder(sources: list[Path], purpose: str) -> Path:
    staging_base = Path(__file__).resolve().parent.parent / "work" / "console_inputs"
    staging_base.mkdir(parents=True, exist_ok=True)
    staging_root = staging_base / (
        f".msdial_interactive_input_{purpose}_"
        + dt.datetime.now().strftime("%Y%m%d%H%M%S%f")
    )
    staging_root.mkdir(parents=True, exist_ok=False)
    seen: set[str] = set()
    for source in sources:
        if source.name.lower() in seen:
            raise ValueError(f"Duplicate folder-type raw-data name cannot be staged: {source.name}")
        seen.add(source.name.lower())
        _link_directory(source, staging_root / source.name)
    return staging_root


def _link_directory(source: Path, link: Path) -> None:
    try:
        os.symlink(source, link, target_is_directory=True)
    except OSError as symlink_error:
        if os.name != "nt":
            raise RuntimeError(
                f"Could not create a directory symlink for {source}: {symlink_error}"
            ) from symlink_error
        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(source)],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                "Could not create a directory junction for folder-type raw data: "
                + (completed.stderr or completed.stdout or str(symlink_error)).strip()
            ) from symlink_error


# MS-DIAL marks an annotation score that it never computed, and the notation changed. A Console
# that includes the shared AnnotationScoreFormat writes "null" into every score column of such a
# row; earlier Consoles wrote 0.000 for the three dot products and -1 for the matched-peak
# columns. Both forms mean the same thing: a precursor-only suggestion had no product-ion spectrum
# to compare, and a text-database annotation has no reference spectrum to compare against. Neither
# is a similarity of zero, so neither is counted as a spectral comparison here.
def _annotation_score_row(
    weighted: float | None,
    simple: float | None,
    reverse: float | None,
    matched_percentage: float | None,
    matched_count: float | None,
) -> dict[str, float] | None:
    values = (weighted, simple, reverse, matched_percentage, matched_count)
    if any(value is None or value < 0 for value in values):
        return None
    return {
        "weighted": weighted,
        "simple": simple,
        "reverse": reverse,
        "matched_percentage": matched_percentage,
        "matched_count": matched_count,
    }


# A reference candidate is any row MS-DIAL gave a name, including the "no MS2: ", "w/o MS2: " and
# "low score: " prefixes. "Unknown" and an empty name mean there was no candidate. Counting names
# rather than present score cells keeps this number the same whichever notation the Console used.
def _names_a_reference_candidate(value: str | None) -> bool:
    name = str(value or "").strip()
    return bool(name) and name.lower() != "unknown"


def parse_mdpeak(path: str | Path) -> dict[str, Any]:
    mdpeak = Path(path)
    heights: list[float] = []
    scores: list[dict[str, float]] = []
    candidate_count = 0
    with mdpeak.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {"Height", "Simple dot product", "Weighted dot product", "Reverse dot product"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Unsupported mdpeak header: {mdpeak}")
        for row in reader:
            height = _nullable_float(row.get("Height"))
            if height is not None:
                heights.append(height)
            if _names_a_reference_candidate(row.get("Name")):
                candidate_count += 1
            score = _annotation_score_row(
                _nullable_float(row.get("Weighted dot product")),
                _nullable_float(row.get("Simple dot product")),
                _nullable_float(row.get("Reverse dot product")),
                _nullable_float(row.get("Matched peaks percentage")),
                _nullable_float(row.get("Matched peaks count")),
            )
            if score is not None:
                scores.append(score)
    heights.sort()
    return {
        "mdpeak": str(mdpeak),
        "source_file": str(mdpeak),
        "peak_count": len(heights),
        "heights": heights,
        # Name is always present in a Console .mdpeak. Fall back to the scored rows so a reduced
        # table supplied by a caller still reports a sane candidate count.
        "msp_candidate_count": max(candidate_count, len(scores)),
        "msp_scored_count": len(scores),
        "msp_scores": scores,
    }


def parse_mdscan(path: str | Path) -> dict[str, Any]:
    mdscan = Path(path)
    heights: list[float] = []
    scores: list[dict[str, float]] = []
    candidate_count = 0
    with mdscan.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="\t")
        required = {
            "Integrated height",
            "Simple dot product",
            "Weighted dot product",
            "Reverse dot product",
            "Fragment presence %",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError(f"Unsupported mdscan header: {mdscan}")
        for row in reader:
            height = _nullable_float(row.get("Integrated height"))
            if height is not None:
                heights.append(height)
            if _names_a_reference_candidate(row.get("Name")):
                candidate_count += 1
            matched_count = _nullable_float(row.get("Matched peaks count"))
            if matched_count is None:
                matched_count = _count_spectrum_peaks(row.get("Spectrum"))
            score = _annotation_score_row(
                _nullable_float(row.get("Weighted dot product")),
                _nullable_float(row.get("Simple dot product")),
                _nullable_float(row.get("Reverse dot product")),
                _nullable_float(row.get("Fragment presence %")),
                matched_count,
            )
            if score is not None:
                scores.append(score)
    heights.sort()
    return {
        "mdscan": str(mdscan),
        "source_file": str(mdscan),
        "peak_count": len(heights),
        "heights": heights,
        # GcmsAnalysisMetadataAccessor writes the string -1 into these columns when there is no
        # match result, so an Unknown .mdscan row is excluded above and counted by name here.
        "msp_candidate_count": max(candidate_count, len(scores)),
        "msp_scored_count": len(scores),
        "msp_scores": scores,
    }


def find_mdpeak(run_directory: str | Path) -> Path:
    files = sorted(Path(run_directory).glob("*.mdpeak"))
    if not files:
        raise FileNotFoundError(f"No mdpeak was generated in {run_directory}")
    return files[0]


def find_mdscan(run_directory: str | Path) -> Path:
    files = sorted(Path(run_directory).glob("*.mdscan"))
    if not files:
        raise FileNotFoundError(f"No mdscan was generated in {run_directory}")
    return files[0]


def _count_spectrum_peaks(value: str | None) -> float:
    if not value:
        return 0
    return float(len([item for item in value.split() if ":" in item]))


def build_console_command(
    console_path: str,
    csv_path: str | Path,
    output_path: str | Path,
    method_path: str | Path,
    analysis_type: str = "lcms",
    project_store: bool = True,
) -> list[str]:
    executable = Path(console_path).expanduser().resolve()
    prefix = ["dotnet", str(executable)] if executable.suffix.lower() == ".dll" else [str(executable)]
    command = prefix + [
        analysis_type,
        "-i",
        str(csv_path),
        "-o",
        str(output_path),
        "-m",
        str(method_path),
    ]
    if project_store:
        command.append("-p")
    return command


def prepare_rt_correction_run(state: dict[str, Any]) -> dict[str, Any]:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        raise ValueError("Retention-time correction preview is currently available only for LC-MS.")
    files = state.get("files", [])
    if not files:
        raise ValueError("Add at least one LC-MS analysis file.")
    console_path = str(state.get("console_path", "")).strip()
    if not console_path:
        raise ValueError("Set the MS-DIAL Console path.")
    executable = Path(console_path).expanduser().resolve()
    if not executable.is_file():
        raise ValueError(f"MS-DIAL Console not found: {executable}")
    anchor_path = Path(str(state.get("rt_correction_anchor_path", ""))).expanduser().resolve()
    if not anchor_path.is_file():
        raise ValueError(f"RT correction anchor library not found: {anchor_path}")
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    template_path = Path(str(state.get("template_path", ""))).expanduser().resolve()
    if not template_path.is_file():
        raise ValueError(f"Parameter template not found: {template_path}")

    acquisition_types = {
        str(item.get("acquisition_type", "DDA") or "DDA")
        for item in files
    }
    if len(acquisition_types) != 1:
        raise ValueError("RT correction preview currently requires one common acquisition type.")
    acquisition_type = next(iter(acquisition_types))
    prefix = ["dotnet", str(executable)] if executable.suffix.lower() == ".dll" else [str(executable)]
    eic_path = output_root / "rt_correction_eics.csv"
    features = console_capabilities(str(executable))["capabilities"]
    rt_command = (
        ["rtcorrection"]
        if RT_CORRECTION_REVIEW_CAPABILITY in features
        else ["eic", "rtcorrection"]
    )
    # The Console writes <method>.keys.json beside the method file it reads, so reading the
    # template wrote into whatever folder held it: for the default template, the resources
    # folder of this checkout. It reads a byte copy in the preview's own directory instead,
    # under a name of its own, so the record it leaves there cannot overwrite the
    # method.keys.json of a run in the same directory. rtcorrection resolves no path against
    # the method file's folder, so the copy reads exactly as the template did.
    method_path = output_root / RT_CORRECTION_METHOD_FILE
    command = prefix + rt_command
    for item in files:
        command.extend(["-i", str(Path(str(item["file_path"])).expanduser().resolve())])
    command.extend(
        [
            "--library",
            str(anchor_path),
            "-o",
            str(eic_path),
            "-m",
            str(method_path),
            "--ionmode",
            str(state.get("ion_mode", "Negative")),
            "--acquisitiontype",
            acquisition_type,
        ]
    )
    selection_input = str(state.get("rt_correction_selection_path", "")).strip()
    if selection_input:
        selection_path = Path(selection_input).expanduser().resolve()
        if not selection_path.is_file():
            raise ValueError(f"RT correction peak selection file not found: {selection_path}")
        command.extend(["--selection", str(selection_path)])
        result_selection = output_root / "rt_correction_peak_selections_applied.tsv"
    else:
        result_selection = output_root / "rt_correction_peak_selections.tsv"
    try:
        shutil.copyfile(template_path, method_path)
    except shutil.SameFileError:
        pass
    return {
        "run_directory": str(output_root),
        "analysis_type": "lcms",
        "kind": "rt_correction",
        "command": command,
        "template_file": str(template_path),
        "method_file": str(method_path),
        "eic_file": str(eic_path),
        "selection_file": str(result_selection),
    }


def parse_rt_correction_result(preparation: dict[str, Any]) -> dict[str, Any]:
    selection_path = Path(preparation["selection_file"])
    eic_path = Path(preparation["eic_file"])
    if not selection_path.is_file():
        raise FileNotFoundError(f"RT correction peak selection file was not generated: {selection_path}")
    if not eic_path.is_file():
        raise FileNotFoundError(f"RT correction EIC file was not generated: {eic_path}")
    run_started_ns = int(preparation.get("run_started_ns", 0))
    if run_started_ns and (
        selection_path.stat().st_mtime_ns < run_started_ns
        or eic_path.stat().st_mtime_ns < run_started_ns
    ):
        raise RuntimeError(
            "The selected MS-DIAL Console did not generate fresh RT correction outputs. "
            "Use a CUI build containing the current rtcorrection implementation: "
            f"{preparation['command'][0]}"
        )

    rows: list[dict[str, Any]] = []
    with selection_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle, delimiter="\t"):
            rows.append(
                {
                    "file_path": row.get("File path", ""),
                    "file_name": row.get("File name", ""),
                    "standard_id": int(row.get("Standard ID", "0")),
                    "standard_name": row.get("Standard name", ""),
                    "reference_rt": float(row.get("Reference RT (min)", "0")),
                    "detected_rt": float(row.get("Detected RT (min)", "0")),
                    "selected_rt": float(row.get("Selected RT (min)", "0")),
                    "use": str(row.get("Use", "False")).lower() == "true",
                    "peak_height": float(row.get("Peak height", "0")),
                }
            )

    series_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    with eic_path.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        reader = csv.DictReader(handle)
        required_columns = {"CorrectedRT", "SmoothedIntensity", "RTTolerance"}
        missing_columns = required_columns.difference(reader.fieldnames or [])
        if missing_columns:
            raise RuntimeError(
                "The selected MS-DIAL Console returned the legacy RT correction EIC format "
                f"(missing: {', '.join(sorted(missing_columns))}). "
                "This output cannot display corrected chromatograms. Rebuild or select the "
                f"current CUI: {preparation['command'][0]}"
            )
        for row in reader:
            key = (row.get("FilePath", ""), int(row.get("StandardId", "0")))
            series = series_by_key.setdefault(
                key,
                {
                    "file_path": row.get("FilePath", ""),
                    "file_name": row.get("FileName", ""),
                    "standard_id": key[1],
                    "standard_name": row.get("StandardName", ""),
                    "target_mz": float(row.get("TargetMz", "0")),
                    "reference_rt": float(row.get("ReferenceRT", "0")),
                    "rt_tolerance": float(row.get("RTTolerance", "0")),
                    "rt": [],
                    "corrected_rt": [],
                    "intensity": [],
                    "smoothed_intensity": [],
                },
            )
            series["rt"].append(float(row.get("RT", "0")))
            series["corrected_rt"].append(float(row.get("CorrectedRT", "0")))
            series["intensity"].append(float(row.get("Intensity", "0")))
            series["smoothed_intensity"].append(float(row.get("SmoothedIntensity", "0")))
    return {
        "selection_file": str(selection_path),
        "eic_file": str(eic_path),
        "rows": rows,
        "series": list(series_by_key.values()),
    }


def save_rt_correction_selections(state: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    output_root = Path(str(state.get("output_root", ""))).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    allowed_files = {
        str(Path(str(item["file_path"])).expanduser().resolve()).casefold()
        for item in state.get("files", [])
    }
    path = output_root / "rt_correction_peak_selections_edited.tsv"
    fields = [
        "File path",
        "File name",
        "Standard ID",
        "Standard name",
        "Reference RT (min)",
        "Detected RT (min)",
        "Selected RT (min)",
        "Use",
        "Peak height",
    ]
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for row in rows:
            file_path = str(Path(str(row.get("file_path", ""))).expanduser().resolve())
            if file_path.casefold() not in allowed_files:
                raise ValueError(f"Unexpected analysis file in RT correction selections: {file_path}")
            selected_rt = float(row.get("selected_rt", 0))
            use = bool(row.get("use", False))
            writer.writerow(
                {
                    "File path": file_path,
                    "File name": row.get("file_name", ""),
                    "Standard ID": int(row.get("standard_id", 0)),
                    "Standard name": row.get("standard_name", ""),
                    "Reference RT (min)": float(row.get("reference_rt", 0)),
                    "Detected RT (min)": float(row.get("detected_rt", 0)),
                    "Selected RT (min)": selected_rt if use else 0,
                    "Use": str(use),
                    "Peak height": float(row.get("peak_height", 0)),
                }
            )
    return str(path)


def _write_reproduction_files(
    run_directory: Path,
    state: dict[str, Any],
    command: list[str],
) -> dict[str, str]:
    settings_path = run_directory / "workflow-settings.json"
    settings = {
        key: value
        for key, value in state.items()
        if key not in {"files", "repository_metadata"}
    }
    settings["files"] = [
        {
            key: item.get(key)
            for key in (
                "file_path",
                "file_name",
                "file_type",
                "class_id",
                "acquisition_type",
                "batch_order",
                "analytical_order",
                "factor",
            )
        }
        for item in state.get("files", [])
    ]
    settings_path.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    command_path = run_directory / "command.txt"
    command_path.write_text(
        subprocess.list2cmdline(command) + "\n",
        encoding="utf-8",
    )
    default_console = str(state["console_path"])
    analysis_type = str(state.get("project_type", "lcms")).lower()
    powershell_path = run_directory / "run-msdial.ps1"
    store_project = "-p" in command
    # Windows PowerShell 5.1 reads a BOM-less script in the ANSI code page, which garbles a
    # non-ASCII default Console path; PowerShell 7 accepts the BOM too.
    powershell_path.write_text(
        _powershell_script(default_console, analysis_type, store_project),
        encoding="utf-8-sig",
    )
    shell_path = run_directory / "run-msdial.sh"
    shell_path.write_text(
        _shell_script(default_console, analysis_type, store_project),
        encoding="utf-8",
        newline="\n",
    )
    readme_path = run_directory / "REPRODUCE.txt"
    readme_path.write_text(_LOCAL_REPRODUCE_TEXT, encoding="utf-8")
    bundle_path = run_directory / "msdial-workflow-bundle.zip"
    members = [
        run_directory / "analysis_files.csv",
        run_directory / "method.txt",
        run_directory / "run-manifest.json",
        settings_path,
        command_path,
        powershell_path,
        shell_path,
        readme_path,
    ]
    ri_dictionary = state.get("ri_dictionary_file_path")
    if ri_dictionary and Path(ri_dictionary).is_file():
        members.append(Path(ri_dictionary))
    msp_annotator_settings = state.get("msp_annotator_settings_file_path")
    if msp_annotator_settings and Path(msp_annotator_settings).is_file():
        members.append(Path(msp_annotator_settings))
    text_annotator_settings = state.get("text_annotator_settings_file_path")
    if text_annotator_settings and Path(text_annotator_settings).is_file():
        members.append(Path(text_annotator_settings))
    if state.get("execute_rt_correction"):
        for key in ("rt_correction_anchor_path", "rt_correction_selection_path"):
            value = str(state.get(key, "")).strip()
            if value and Path(value).is_file():
                members.append(Path(value))
    for value in (state.get("repository_metadata_files") or {}).values():
        path = Path(str(value))
        if path.is_file():
            members.append(path)
    withheld_members = _write_workflow_bundle(bundle_path, members, readme_path, run_directory, state)
    return {
        "settings_file": str(settings_path),
        "command_file": str(command_path),
        "powershell_script": str(powershell_path),
        "shell_script": str(shell_path),
        "reproduce_readme": str(readme_path),
        "bundle": str(bundle_path),
        **({"bundle_withheld_members": withheld_members} if withheld_members else {}),
    }


_LOCAL_REPRODUCE_TEXT = (
    "MS-DIAL reproducible Console workflow\n\n"
    "Files:\n"
    "- analysis_files.csv: absolute input paths (the staged copies under input\\ when\n"
    "  inputs were staged) and sample metadata\n"
    "- method.txt: final parameter file, including Tune parameters values\n"
    "- msp_annotator_settings.tsv: optional per-MSP LC-MS annotation settings\n"
    "- text_annotator_settings.tsv: optional per-Text-library LC-MS annotation settings\n"
    "- RT correction anchor/selection files: included when RT correction is enabled\n"
    "- workflow-settings.json: UI settings used to generate the workflow\n"
    "- *_repository_metadata_reviewed.json / *_sample_metadata_reviewed.tsv: reviewed repository metadata and Class hierarchy\n"
    "- command.txt: exact command generated on the original machine\n"
    "- run-msdial.ps1 / run-msdial.sh: portable launch scripts\n\n"
    "Edit parameters:\n"
    "  vim method.txt\n\n"
    "Windows PowerShell:\n"
    "  .\\run-msdial.ps1\n"
    "  .\\run-msdial.ps1 'C:\\path\\to\\MSDIALCUI.exe'\n\n"
    "Bash:\n"
    "  bash run-msdial.sh\n"
    "  bash run-msdial.sh /path/to/MSDIALCUI.dll\n\n"
    "The scripts copy method.txt to method.reproduce.txt beside it and analysis_files.csv into\n"
    "reproduced-results, and run the Console from this directory on the copies. The Console's\n"
    "key record is then written as method.reproduce.keys.json beside method.txt instead of\n"
    "overwriting method.keys.json, and every -o output goes to reproduced-results.\n"
    "MS-DIAL still writes its per-file .dcl/.pai2/_tags.xml and AlignResult-* intermediates\n"
    "beside the input files listed in analysis_files.csv.\n\n"
    "The CSV contains absolute input paths (staged copies under input\\ when inputs were\n"
    "staged). Update them if the data move.\n"
    "Paths in method.txt are absolute too, including the MSP/Text annotator settings files,\n"
    "which name this run directory, and the RT correction files; update them after moving\n"
    "the bundle. The scripts run the Console from the directory of method.txt, so a relative\n"
    "path is read against it.\n"
)

_PRIVATE_LIBRARY_NOTE = (
    "\nPrivate libraries are named by file name only in this bundle's method.txt and annotator\n"
    "settings, and they are not distributed with it. Place each one beside method.txt, or edit\n"
    "those files to point at it, before running.\n"
)

_SHARED_REPRODUCE_TEXT = (
    "MS-DIAL reproducible Console workflow\n\n"
    "This bundle follows the shared-path policy {policy}: it carries no path from the machine\n"
    "that ran it. SHARED-PATHS.json states the policy, names every library by file name and SHA-256,\n"
    "and says what the placeholders mean.\n\n"
    "Files:\n"
    "- analysis_files.csv: input paths relative to this directory, as raw/<path in the analysis\n"
    "  unit's raw data directory>, and sample metadata\n"
    "- method.txt: final parameter file, including Tune parameters values; libraries and the\n"
    "  annotator settings files are named by file name\n"
    "- msp_annotator_settings.tsv: optional per-MSP LC-MS annotation settings\n"
    "- text_annotator_settings.tsv: optional per-Text-library LC-MS annotation settings\n"
    "- RT correction anchor/selection files: included when RT correction is enabled\n"
    "- workflow-settings.json and run-manifest.json: the settings and the run record, with paths\n"
    "  made relative to this directory\n"
    "- *_repository_metadata_reviewed.json / *_sample_metadata_reviewed.tsv: reviewed repository metadata and Class hierarchy\n"
    "- command.txt: the command generated on the original machine, its paths made relative\n"
    "- run-msdial.ps1 / run-msdial.sh: portable launch scripts\n"
    "- SHARED-PATHS.json: the path policy and each library's identity\n\n"
    "Before running:\n"
    "- Place the raw data under raw\\ beside method.txt, at the paths analysis_files.csv lists.\n"
    "- Place each library method.txt and the annotator settings name beside method.txt. A library\n"
    "  SHARED-PATHS.json marks private is not distributed; its SHA-256 identifies the file.\n"
    "- A value shown as <local path withheld: NAME> named a file outside the analysis unit's\n"
    "  workspace; supply that file and edit the value.\n\n"
    "Edit parameters:\n"
    "  vim method.txt\n\n"
    "Windows PowerShell:\n"
    "  .\\run-msdial.ps1\n"
    "  .\\run-msdial.ps1 <path to MSDIALCUI.exe>\n\n"
    "Bash:\n"
    "  bash run-msdial.sh\n"
    "  bash run-msdial.sh <path to MSDIALCUI.dll>\n\n"
    "Without an argument the scripts run MSDIALCUI.exe from this directory or from PATH.\n"
    "The scripts copy method.txt to method.reproduce.txt beside it and analysis_files.csv into\n"
    "reproduced-results, and run the Console from this directory on the copies. The Console's\n"
    "key record is then written as method.reproduce.keys.json beside method.txt instead of\n"
    "overwriting method.keys.json, and every -o output goes to reproduced-results.\n"
    "MS-DIAL still writes its per-file .dcl/.pai2/_tags.xml and AlignResult-* intermediates\n"
    "beside the input files listed in analysis_files.csv.\n"
)


def _write_workflow_bundle(
    bundle_path: Path,
    members: list[Path],
    readme_path: Path,
    run_directory: Path,
    state: dict[str, Any],
) -> list[str]:
    """Zip the reproduction files as a bundle that may leave this machine. Returns what was left out.

    The files on disk are the ones the Console reads, so they keep their absolute paths. The bundle carries
    renderings of them. A repository unit's bundle names nothing of this machine - raw inputs as raw/...,
    the run directory as the bundle itself, libraries and the Console by file name - and declares the
    shared-path policy by carrying SHARED-PATHS.json. A laboratory run's bundle changes only where a private
    library would have been located. No library file, no copy of one and no local-only record is ever a
    member, and a member that still matches what may not be shared after rendering is left out rather than
    shipped.
    """
    from .sharing import PATH_POLICY, SHARED_PATHS_MEMBER, SharingContext, bundle_member_allowed, render_member

    try:
        recorded = json.loads((run_directory / "run-manifest.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        recorded = {}
    context = SharingContext.for_state(
        state,
        run_directory=run_directory,
        recorded=(recorded or {}).get("libraries") if isinstance(recorded, dict) else None,
        bundle=True,
    )
    private = any(library.private for library in context.libraries)
    withheld: list[str] = []
    with zipfile.ZipFile(bundle_path, "w", zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            if not bundle_member_allowed(member.name, shared=True):
                withheld.append(f"{member.name}: a library file or a local-only record")
                continue
            if not context.full and not private:
                archive.write(member, member.name)  # nothing in a laboratory run's bundle to rewrite
                continue
            if member == readme_path:
                text = (
                    _SHARED_REPRODUCE_TEXT.format(policy=PATH_POLICY) if context.full
                    else _LOCAL_REPRODUCE_TEXT + _PRIVATE_LIBRARY_NOTE
                )
                data = text.encode("utf-8")
            else:
                data = render_member(member, context)
            findings = context.scan(member.name, data)
            if findings:
                withheld.append(f"{member.name}: {'; '.join(findings[:3])}")
                continue
            info = zipfile.ZipInfo.from_file(member, member.name)
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
        if context.full:
            archive.writestr(
                SHARED_PATHS_MEMBER,
                json.dumps(
                    {**context.describe(), "libraries": context.identities(), "withheld_members": withheld},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
            )
    context.assert_shareable(bundle_path)
    return withheld


def _powershell_script(
    default_console: str, analysis_type: str, store_project: bool = True
) -> str:
    quoted = default_console.replace("'", "''")
    project = ", '-p'" if store_project else ""
    return (
        "param([string]$Console = '" + quoted + "')\n"
        "$Here = Split-Path -Parent $MyInvocation.MyCommand.Path\n"
        f"$Output = Join-Path $Here '{REPRODUCTION_DIRECTORY_NAME}'\n"
        "New-Item -ItemType Directory -Force -Path $Output | Out-Null\n"
        # The Console writes <method>.keys.json beside the method file it reads, so a
        # reproduction that read method.txt overwrote the run's own key record, the evidence
        # its automatic RT-correction claim rests on. It reads a byte copy under another name
        # in the same directory, so relative paths resolve as before and the encoding is kept.
        # The CSV copy moves the Console's project folder (the CSV's directory) out of the run.
        "$Method = Join-Path $Here 'method.reproduce.txt'\n"
        "$Inputs = Join-Path $Output 'analysis_files.csv'\n"
        "try {\n"
        "  Copy-Item -LiteralPath (Join-Path $Here 'method.txt') -Destination $Method -Force -ErrorAction Stop\n"
        "  Copy-Item -LiteralPath (Join-Path $Here 'analysis_files.csv') -Destination $Inputs -Force -ErrorAction Stop\n"
        "} catch {\n"
        "  Write-Error \"Could not prepare the reproduction inputs: $_\" -ErrorAction Continue\n"
        "  exit 1\n"
        "}\n"
        f"$Arguments = @('{analysis_type}', '-i', $Inputs, '-o', $Output, '-m', $Method{project})\n"
        # The LC-MS Console reads library and RT-correction paths against its working
        # directory, and GC-MS against the method file's; running from the bundle makes a
        # relative path mean the same in both.
        # A Console path given relative to the caller's directory must survive the move.
        # A relative path is the caller's, whether or not it exists; a bare name is a file here
        # or a command on PATH.
        "if (-not [System.IO.Path]::IsPathRooted($Console)) {\n"
        "  if ($Console -match '[\\\\/]') { $Console = Join-Path (Get-Location).ProviderPath $Console }\n"
        "  elseif (Test-Path -LiteralPath $Console -PathType Leaf) { $Console = (Resolve-Path -LiteralPath $Console).ProviderPath }\n"
        "}\n"
        # finally, so the caller's session is back where it was even when the start fails
        # under $ErrorActionPreference = 'Stop'.
        "Push-Location -LiteralPath $Here\n"
        "try {\n"
        "  try {\n"
        "    if ($Console.ToLowerInvariant().EndsWith('.dll')) {\n"
        "      & dotnet $Console @Arguments\n"
        "    } else {\n"
        "      & $Console @Arguments\n"
        "    }\n"
        "  } catch {\n"
        "    Write-Error \"Could not start the MS-DIAL Console: $_\" -ErrorAction Continue\n"
        "    exit 1\n"
        "  }\n"
        "} finally {\n"
        "  Pop-Location\n"
        "}\n"
        # A command that never started leaves $LASTEXITCODE unset, and `exit $null` is 0.
        "if ($null -eq $LASTEXITCODE) { exit 1 }\n"
        "exit $LASTEXITCODE\n"
    )


def _shell_script(default_console: str, analysis_type: str, store_project: bool = True) -> str:
    project = " -p" if store_project else ""
    arguments = f'{analysis_type} -i "$INPUTS" -o "$OUTPUT" -m "$METHOD"{project}'
    return (
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f"CONSOLE=${{1:-{shlex.quote(default_console)}}}\n"
        'HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"\n'
        f'OUTPUT="$HERE/{REPRODUCTION_DIRECTORY_NAME}"\n'
        'mkdir -p "$OUTPUT"\n'
        # See _powershell_script for why both inputs are copies.
        'METHOD="$HERE/method.reproduce.txt"\n'
        'INPUTS="$OUTPUT/analysis_files.csv"\n'
        # -f: a read-only source makes a read-only copy, which a plain cp cannot overwrite.
        'cp -f "$HERE/method.txt" "$METHOD"\n'
        'cp -f "$HERE/analysis_files.csv" "$INPUTS"\n'
        # See _powershell_script: run from the bundle so relative paths mean one thing, after
        # anchoring the Console path to the caller. An absolute path, Windows or POSIX, is left
        # as given; a relative path is the caller's, whether or not it exists; a bare name is a
        # file here or a command on PATH. Under MSYS cygpath gives the Windows form a native
        # dotnet needs.
        'absolute() { if command -v cygpath >/dev/null 2>&1; then cygpath -am "$1"; '
        'else printf \'%s/%s\\n\' "$PWD" "$1"; fi; }\n'
        'case "$CONSOLE" in\n'
        '  /*|[A-Za-z]:[\\\\/]*|\\\\\\\\*) ;;\n'
        '  */*|*\\\\*) CONSOLE="$(absolute "$CONSOLE")" ;;\n'
        '  *) if [ -f "$CONSOLE" ]; then CONSOLE="$(absolute "$CONSOLE")"; fi ;;\n'
        "esac\n"
        'cd "$HERE"\n'
        # A case pattern rather than ${CONSOLE,,}, which needs bash 4 (macOS ships 3.2).
        'case "$CONSOLE" in\n'
        f'  *.[dD][lL][lL]) dotnet "$CONSOLE" {arguments} ;;\n'
        f'  *) "$CONSOLE" {arguments} ;;\n'
        "esac\n"
    )


# What run_console returns when it stopped the Console itself rather than the Console exiting. They are
# negative, which the exit code of a process that ended on its own never is on Windows.
CONSOLE_EXIT_SCIEX_SIDECAR = -2
CONSOLE_EXIT_TIMEOUT = -3
CONSOLE_EXIT_CANCELLED = -4
# Every line the watch writes into a job's log starts with this, so a reader - the failure diagnosis
# among them - can tell the watch's account of a stop from anything the Console printed.
CONSOLE_WATCHDOG_PREFIX = "Console watchdog: "
_WATCH_POLL_SECONDS = 0.25
# After the Console has exited, how long its pipe is waited on for more output. Everything the pipe
# delivered within that time is passed on, however long a slow line handler takes over it. A process the
# Console started can inherit the pipe and hold it open, and write to it, for as long as it lives; the
# job does not wait on that.
_WATCH_DRAIN_SECONDS = 10.0
# The longest time limit accepted: ten years. A longer one is a mistake rather than a limit, and from
# about 2.6e11 s its deadline is past the last date Python can represent.
_WATCH_MAX_SECONDS = 10 * 365.25 * 24 * 3600
_WATCH_KILL_WAIT_SECONDS = 30.0
_ACTIVITY_SCAN_MAX_SECONDS = 10.0
_ACTIVITY_ROOT_LIMIT = 64


def console_watch_seconds(value: Any, name: str = "time limit") -> float | None:
    """A Console time limit in seconds, or None for none. None, '' and 0 all mean no limit."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a number of seconds, not {value!r}.")
    try:
        seconds = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} must be a number of seconds, not {value!r}.") from error
    if math.isnan(seconds) or seconds < 0:
        raise ValueError(f"{name} must be a positive number of seconds, or 0 for no limit.")
    if seconds == 0 or math.isinf(seconds):
        return None
    if seconds > _WATCH_MAX_SECONDS:
        raise ValueError(
            f"{name} must be at most ten years ({_WATCH_MAX_SECONDS:.0f} s), or 0 for no limit."
        )
    return seconds


def run_console(
    preparation: dict[str, Any],
    on_line: Callable[[str], None],
    *,
    timeout_seconds: float | None = None,
    idle_timeout_seconds: float | None = None,
    cancel_event: threading.Event | None = None,
    on_start: Callable[[int], None] | None = None,
    outcome: dict[str, Any] | None = None,
) -> int:
    """Run the prepared Console command, passing each line of its output to ``on_line``.

    Returns the Console's own exit code, or one of the codes above when this function stopped it:
    CONSOLE_EXIT_SCIEX_SIDECAR after repeated SCIEX scan-sidecar read failures; CONSOLE_EXIT_TIMEOUT when
    it ran past ``timeout_seconds``, or when neither its output nor its log grew for
    ``idle_timeout_seconds``; CONSOLE_EXIT_CANCELLED once ``cancel_event`` is set.

    WHY A WATCH. The Console had no time limit and could not be stopped: its output was read until the
    pipe closed and the process was then waited for without a timeout. One hung Console held its job,
    and at campaign scale every unit queued behind it, for ever; a caller that gave up polling could only
    leave it running. With any of the three given, the output is read in a helper thread and this thread
    watches the clock, the cancel flag, and the Console's activity: its output lines, and the files in
    its output and export folders and beside its inputs, where MS-DIAL writes its intermediates. A stop
    kills the whole process tree - taskkill /T /F on Windows, the process group elsewhere - so a dotnet
    launcher and anything the Console started go with it, and the watch then gives the Console's
    remaining output a bounded time to arrive.

    Nothing is watched unless asked: with none of the three, the Console runs exactly as it always has.

    ``on_start`` receives the Console's process id as soon as the process exists. If it raises, the
    Console is stopped and the exception propagates, so no Console runs that its caller could not
    register. The same holds for anything else that fails once the process exists - ``on_line``, the
    watch itself: the Console is stopped before the failure reaches the caller, who could otherwise
    neither see nor stop it. ``outcome``, when given, is filled with what happened: the reason (exited,
    timeout, idle_timeout, cancelled, sciex_scan_sidecar), the process id, the start and end times, and
    how a stop was made.
    """
    timeout = console_watch_seconds(timeout_seconds, "timeout_seconds")
    idle = console_watch_seconds(idle_timeout_seconds, "idle_timeout_seconds")
    watched = timeout is not None or idle is not None or cancel_event is not None
    report = outcome if outcome is not None else {}
    report.update(
        {
            "reason": None,
            "pid": None,
            "exit_code": None,
            "timeout_seconds": timeout,
            "idle_timeout_seconds": idle,
        }
    )
    if cancel_event is not None and cancel_event.is_set():
        # Cancelled while the job was queued or being prepared: nothing starts, so nothing is left over.
        on_line(
            CONSOLE_WATCHDOG_PREFIX
            + "the job was cancelled before the MS-DIAL Console started (exit code -4)."
        )
        report.update(
            {"reason": "cancelled", "exit_code": CONSOLE_EXIT_CANCELLED, "ended_at": _utc_now()}
        )
        return CONSOLE_EXIT_CANCELLED
    process = subprocess.Popen(
        preparation["command"],
        cwd=str(Path(preparation["command"][0]).parent)
        if Path(preparation["command"][0]).is_absolute()
        else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
        # Its own process group, so a stop can end everything it started (POSIX; Windows uses taskkill).
        **({"start_new_session": True} if watched and os.name != "nt" else {}),
    )
    started = time.monotonic()
    report["pid"] = getattr(process, "pid", None)
    try:
        report["started_at"] = _utc_now()
        if timeout is not None:
            report["deadline_at"] = (
                dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=timeout)
            ).isoformat()
        if on_start is not None:
            on_start(process.pid)
        assert process.stdout is not None
        if watched:
            code = _watch_console(
                process, on_line, preparation, timeout, idle, cancel_event, report, started
            )
        else:
            code = _read_console(process, on_line, report)
    except BaseException:
        # The caller learns of the failure but not of a Console still running, which it could neither
        # cancel nor keep a second one off the unit for. A stop the watch already made keeps its record.
        if process.poll() is None:
            report.setdefault("stop", _stop_console_tree(process, group=watched))
        raise
    report["exit_code"] = code
    report["ended_at"] = _utc_now()
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    return code


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _is_sciex_scan_failure(message: str) -> bool:
    lower = message.lower()
    return "required 'scan' file missing" in lower or "required 'scan' file is missing" in lower


def _read_console(process: subprocess.Popen, on_line: Callable[[str], None], report: dict[str, Any]) -> int:
    """The Console's output read to the end, unwatched: how every run went before the watch."""
    scan_file_errors = 0
    for line in process.stdout:
        message = line.rstrip()
        on_line(message)
        if _is_sciex_scan_failure(message):
            scan_file_errors += 1
            if scan_file_errors >= 3:
                on_line(
                    "Stopping diagnostic after repeated SCIEX scan-sidecar read failures."
                )
                process.terminate()
                process.wait(timeout=10)
                report["reason"] = "sciex_scan_sidecar"
                return CONSOLE_EXIT_SCIEX_SIDECAR
    report["reason"] = "exited"
    return process.wait()


def _watch_console(
    process: subprocess.Popen,
    on_line: Callable[[str], None],
    preparation: dict[str, Any],
    timeout: float | None,
    idle: float | None,
    cancel_event: threading.Event | None,
    report: dict[str, Any],
    started: float,
) -> int:
    # Each line with the time the pipe delivered it; None once the pipe is closed. Unbounded, so the
    # Console never waits on a slow line handler - and none of its lines is lost to one either.
    lines: queue.Queue[tuple[float, str | None]] = queue.Queue()

    def read() -> None:
        try:
            for line in process.stdout:
                lines.put((time.monotonic(), line))
        except (OSError, ValueError):
            pass
        finally:
            lines.put((time.monotonic(), None))

    # The pipe is read here, never in the watching thread, so nothing the Console does with its output
    # can keep the watch from acting. on_line is still called only from the caller's thread.
    threading.Thread(target=read, name=f"msdial-console-output-{process.pid}", daemon=True).start()
    roots = _console_activity_roots(preparation) if idle is not None else []
    fingerprint = _activity_fingerprint(roots)
    scan_every = min(max(idle / 4, _WATCH_POLL_SECONDS), _ACTIVITY_SCAN_MAX_SECONDS) if idle else 0.0
    next_scan = started + scan_every
    last_activity = started
    stop: tuple[str, int] | None = None
    stopped_at = 0.0
    exited_at: float | None = None
    scan_file_errors = 0
    at_end = False
    while True:
        line = ""
        if not at_end:
            try:
                arrived, item = lines.get(timeout=_WATCH_POLL_SECONDS)
            except queue.Empty:
                arrived, item = 0.0, ""
            if item is None:
                at_end = True
            elif exited_at is not None and arrived - exited_at >= _WATCH_DRAIN_SECONDS:
                # Written after the drain time, by something that outlived the Console and holds its pipe.
                report["output_left_open"] = True
                break
            else:
                line = item
        else:
            try:
                process.wait(timeout=_WATCH_POLL_SECONDS)
            except subprocess.TimeoutExpired:
                pass
        now = time.monotonic()
        if line:
            last_activity = now
            message = line.rstrip()
            on_line(message)
            if stop is None and _is_sciex_scan_failure(message):
                scan_file_errors += 1
                if scan_file_errors >= 3:
                    on_line("Stopping diagnostic after repeated SCIEX scan-sidecar read failures.")
                    stop, stopped_at = ("sciex_scan_sidecar", CONSOLE_EXIT_SCIEX_SIDECAR), now
                    report["stop"] = _stop_console_tree(process, group=True)
        if process.poll() is not None:
            # Exited, on its own or stopped. Everything already read from the pipe is passed on, however
            # long that takes; the pipe itself is waited on for a bounded time.
            if at_end:
                break
            if exited_at is None:
                exited_at = now
            elif now - exited_at >= _WATCH_DRAIN_SECONDS and lines.empty():
                report["output_left_open"] = True
                break
            continue
        if stop is not None:
            # Stopped, and it outlived even the fallback kill. Waiting longer changes nothing.
            if now - stopped_at >= _WATCH_DRAIN_SECONDS:
                report["still_running"] = True
                break
            continue
        if idle is not None and now >= next_scan:
            next_scan = now + scan_every
            current = _activity_fingerprint(roots)
            if current != fingerprint:
                fingerprint, last_activity = current, now
        message = ""
        if cancel_event is not None and cancel_event.is_set():
            stop = ("cancelled", CONSOLE_EXIT_CANCELLED)
            message = "the job was cancelled; stopped the MS-DIAL Console (exit code -4)."
        elif timeout is not None and now - started >= timeout:
            stop = ("timeout", CONSOLE_EXIT_TIMEOUT)
            message = (
                f"the MS-DIAL Console ran longer than its {timeout:g} s time limit; "
                "stopped it (exit code -3)."
            )
        elif idle is not None and now - last_activity >= idle:
            stop = ("idle_timeout", CONSOLE_EXIT_TIMEOUT)
            message = (
                f"neither the MS-DIAL Console's output nor its log grew for {idle:g} s; "
                "stopped it (exit code -3)."
            )
        if stop is not None:
            stopped_at = now
            report["idle_seconds"] = round(now - last_activity, 3)
            on_line(CONSOLE_WATCHDOG_PREFIX + message)
            report["stop"] = _stop_console_tree(process, group=True)
    if at_end:
        # The reader is done with the pipe. One a survivor still holds open is left to the reader.
        process.stdout.close()
    code = process.poll()
    if stop is not None:
        report["reason"] = stop[0]
        report["process_exit_code"] = code
        return stop[1]
    report["reason"] = "exited"
    return code


def _stop_console_tree(process: subprocess.Popen, group: bool) -> dict[str, Any]:
    """End the Console and everything it started, and wait for it. Signals nothing else.

    ``group`` says the Console was started in its own process group (POSIX), which is then what is
    killed; on Windows taskkill follows the process tree from the Console's own id.
    """
    record: dict[str, Any] = {"pid": process.pid}
    if process.poll() is None:
        if os.name == "nt":
            record["method"] = "taskkill /T /F"
            system_taskkill = Path(os.environ.get("SystemRoot") or r"C:\Windows") / "System32" / "taskkill.exe"
            try:
                completed = subprocess.run(
                    [str(system_taskkill) if system_taskkill.is_file() else "taskkill",
                     "/T", "/F", "/PID", str(process.pid)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_WATCH_KILL_WAIT_SECONDS,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                record["returncode"] = completed.returncode
            except (OSError, subprocess.SubprocessError) as error:
                record["error"] = str(error)
        elif group:
            record["method"] = "killpg SIGKILL"
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError as error:
                record["error"] = str(error)
        else:
            record["method"] = "kill"
            process.kill()
    try:
        process.wait(timeout=_WATCH_KILL_WAIT_SECONDS)
    except subprocess.TimeoutExpired:
        # The tree kill did not end it: end this one process directly, the last thing that can be done.
        record["fallback"] = "kill"
        process.kill()
        try:
            process.wait(timeout=_WATCH_KILL_WAIT_SECONDS)
        except subprocess.TimeoutExpired:
            record["still_running"] = True
    return record


def _console_activity_roots(preparation: dict[str, Any]) -> list[Path]:
    """Where a working Console leaves traces: its output and export folders, and beside its inputs.

    MS-DIAL writes its per-file intermediates (.dcl, .pai2 and the like) beside the files it reads, so
    the directories that hold the inputs are watched too; the inputs are read from the analysis CSV the
    run was prepared with.
    """
    candidates: list[Any] = [preparation.get("run_directory"), preparation.get("export_folder_path")]
    csv_text = str(preparation.get("input_csv") or "").strip()
    if csv_text:
        try:
            with open(csv_text, encoding="utf-8-sig", newline="") as handle:
                for row in csv.DictReader(handle):
                    path_text = str(row.get("file_path") or "").strip()
                    if path_text:
                        candidates.append(str(Path(path_text).parent))
        except (OSError, csv.Error, UnicodeDecodeError):
            pass
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "").strip()
        if not text:
            continue
        key = os.path.normcase(os.path.abspath(text))
        if key in seen:
            continue
        seen.add(key)
        roots.append(Path(text))
        if len(roots) >= _ACTIVITY_ROOT_LIMIT:
            break
    return roots


def _activity_fingerprint(roots: list[Path]) -> tuple[int, int, int]:
    """Entry count, total file size and newest modification time directly under each root."""
    count = size = newest = 0
    for root in roots:
        try:
            with os.scandir(root) as entries:
                for entry in entries:
                    try:
                        # A fresh stat, not the one cached from the directory listing: on Windows the
                        # listing's size of a file still being written lags behind the file.
                        stat = os.stat(entry.path, follow_symlinks=False)
                    except OSError:
                        try:
                            stat = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                    count += 1
                    newest = max(newest, stat.st_mtime_ns)
                    if entry.is_file(follow_symlinks=False):
                        size += stat.st_size
        except OSError:
            continue
    return count, size, newest


def _write_analysis_csv(
    path: Path,
    rows: list[dict[str, Any]],
    effective_paths: list[Path],
) -> None:
    headers = [
        "file_path",
        "file_name",
        "file_type",
        "class_id",
        "acquisition_type",
        "batch_order",
        "analytical_order",
        "factor",
    ]
    with path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers, lineterminator="\n")
        writer.writeheader()
        for row, effective in zip(rows, effective_paths, strict=True):
            data = {key: row.get(key, "") for key in headers}
            data["file_path"] = str(effective)
            writer.writerow(data)


def _prepare_gcms_ri_dictionary(
    run_directory: Path,
    state: dict[str, Any],
    rows: list[dict[str, Any]],
    effective_paths: list[Path],
) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "gcms":
        return None
    uses_ri = (
        str(state.get("gcms_retention_type", "RT")).upper() == "RI"
        or str(state.get("gcms_alignment_index_type", "RT")).upper() == "RI"
    )
    if not uses_ri:
        state["ri_dictionary_file_path"] = ""
        return None
    source = str(state.get("gcms_ri_source", "single"))
    if source == "dictionary":
        dictionary = Path(str(state.get("gcms_ri_dictionary_path", ""))).expanduser().resolve()
        state["ri_dictionary_file_path"] = str(dictionary)
        return None
    dictionary = run_directory / "ri_dictionary_paths.txt"
    with dictionary.open("w", encoding="ascii", newline="") as handle:
        if source == "perFile":
            mapping = {
                str(item.get("file_path", "")): str(item.get("ri_path", "")).strip()
                for item in state.get("gcms_ri_file_map", [])
            }
            for original, effective in zip(rows, effective_paths, strict=True):
                standard = Path(mapping[str(original["file_path"])]).expanduser().resolve()
                handle.write(f"{effective}\t{standard}\n")
        else:
            standard = Path(str(state.get("gcms_ri_standard_path", ""))).expanduser().resolve()
            for path in effective_paths:
                handle.write(f"{path}\t{standard}\n")
    state["ri_dictionary_file_path"] = str(dictionary)
    return dictionary


def _method_value(value: Any) -> str:
    """Render one method-file value the way MS-DIAL's own writer would.

    A Python float prints its decimal point even when it names a whole number, so a minimum peak
    height of 500 reached the method file as "500.0". MS-DIAL's reader parsed that key with
    int.TryParse and reported the key as consumed whether or not the parse had succeeded, so the
    value was discarded, MinimumAmplitude kept its built-in 1000, and the retained method file
    recorded 500. Every threshold the contract's zero-threshold diagnostic produced was thrown
    away that way, and no artifact in the workspace could contradict the number.

    The reader has been fixed to accept either spelling. This does not wait for that fix: the
    Console the pipeline resolves is built from a branch that takes changes from master later, so
    a method file written today is read by yesterday's parser.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return str(int(value))
    return str(value)


# Keys the MS-DIAL Console reads as one setting: the case labels that share an arm in its
# ConfigParser (MsdialWorkbench f0583493a). A template line under any of them sets what Interactive's
# own line sets, so the writer treats it as that line rather than leaving it to compete. Since
# MsdialWorkbench #817 every reader takes the last line that sets a value, so a stray template line
# after Interactive's would win.
CONSOLE_KEY_ALIASES: tuple[frozenset[str], ...] = (
    frozenset({"alignment light mode", "alignment light", "console alignment light mode"}),
    frozenset({"annotation candidates", "export annotation candidates"}),
    frozenset({"detailed alignment provenance", "export detailed alignment provenance"}),
    frozenset({"execute annotation process only for alignment file",
               "execute annotation process only for alignment file for msp-based annotation"}),
    frozenset({"lbm annotator priority", "lbm annotation priority"}),
    frozenset({"matched peaks percentage cutoff", "matched peaks percentage cutoff for msp-based annotation"}),
    frozenset({"minimum spectrum match", "minimum spectrum match for msp-based annotation"}),
    frozenset({"msp annotator settings file path", "msp annotation settings file path",
               "msp search settings file path"}),
    frozenset({"retention index alignment tolerance", "retention index tolerance for alignment"}),
    frozenset({"retention index tolerance for identification", "ri tolerance for identification",
               "ri tolerance for msp-based annotation"}),
    frozenset({"reverse dot product cutoff", "square root of reverse dot product cutoff for msp-based annotation"}),
    frozenset({"ri compound", "ri compound type"}),
    frozenset({"ri dictionary file path", "ri dictionary file paths", "ri index file pathes", "ri index file paths"}),
    frozenset({"simple dot product cutoff", "square root of simple dot product cutoff for msp-based annotation"}),
    frozenset({"weighted dot product cutoff", "square root of weighted dot product cutoff for msp-based annotation"}),
    frozenset({"text annotator settings file path", "text library annotator settings file path",
               "text db annotator settings file path", "text annotation settings file path"}),
)
_CONSOLE_KEY_GROUP = {key: group for group in CONSOLE_KEY_ALIASES for key in group}


def console_method_key(line: str) -> str | None:
    """The key the MS-DIAL Console reads from a method-file line, case-folded, or None.

    As its readFieldValues does: nothing for a blank or '#' line or one without a separator, and
    otherwise the text before the first ':' or '=', trimmed.
    """
    if len(line) < 2 or line.lstrip().startswith("#"):
        return None
    separators = [index for index in (line.find(":"), line.find("=")) if index >= 0]
    if not separators:
        return None
    return line[: min(separators)].strip().casefold()


def _write_method(path: Path, state: dict[str, Any]) -> None:
    template_path = Path(state["template_path"])
    lines = template_path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
    project_type = str(state.get("project_type", "lcms")).lower()
    msp_annotator_settings_path = _write_msp_annotator_settings(path.parent, state)
    text_annotator_settings_path = _write_text_annotator_settings(path.parent, state)
    first_msp_path = next(
        (
            str(row.get("msp_file_path", "")).strip()
            for row in state.get("msp_annotators", [])
            if str(row.get("msp_file_path", "")).strip()
        ),
        "",
    )
    replacements = {
        "msp file path": "" if msp_annotator_settings_path else (state.get("msp_path", "") or first_msp_path),
        "lbm file path": state.get("lbm_path", ""),
        "lbm annotator priority": int(state.get("lbm_annotator", {}).get("priority", state.get("lbm_priority", 1))),
        "text db file path": "" if text_annotator_settings_path else state.get("text_db_path", ""),
        "searched adduct ions": ",".join(state.get("selected_adducts", [])),
        "ion mode": state.get("ion_mode", "Negative"),
        "target omics": state.get("target_omics", "Lipidomics"),
        "ms1 data type": state.get("ms1_data_type", "Centroid"),
        "ms2 data type": state.get("ms2_data_type", "Centroid"),
        "number of threads": state.get("number_of_threads", 4),
        "smoothing method": state.get("smoothing_method", "LinearWeightedMovingAverage"),
        "minimum peak height": state.get("minimum_peak_height", 1000),
        "mass slice width": state.get("mass_slice_width", 0.1),
        "minimum peak width": state.get("minimum_peak_width", 5),
        "retention time begin": state.get("retention_time_begin", 0),
        "retention time end": state.get("retention_time_end", 100),
        "ms1 tolerance for centroid": state.get("ms1_tolerance", 0.01),
        "ms2 tolerance for centroid": state.get("ms2_tolerance", 0.025),
        "retention time tolerance for alignment": state.get(
            "alignment_rt_tolerance", 0.1
        ),
        "ms1 tolerance for alignment": state.get("alignment_ms1_tolerance", 0.015),
        "weighted dot product cutoff for msp-based annotation": state.get(
            "msp_weighted_dot_product", 0.6
        ),
        "simple dot product cutoff for msp-based annotation": state.get(
            "msp_simple_dot_product", 0.6
        ),
        "reverse dot product cutoff for msp-based annotation": state.get(
            "msp_reverse_dot_product", 0.8
        ),
        "matched peaks percentage cutoff for msp-based annotation": state.get(
            "msp_matched_peaks_percentage", 0.1
        ),
        "minimum spectrum match for msp-based annotation": state.get(
            "msp_minimum_spectrum_match", 3
        ),
        "rt tolerance for lbm-based annotation": state.get("lbm_rt_tolerance", 100),
        "ms1 tolerance for lbm-based annotation": state.get("lbm_ms1_tolerance", 0.01),
        "ms2 tolerance for lbm-based annotation": state.get("lbm_ms2_tolerance", 0.025),
        "weighted dot product cutoff for lbm-based annotation": state.get("lbm_weighted_dot_product", 0.15),
        "simple dot product cutoff for lbm-based annotation": state.get("lbm_simple_dot_product", 0.15),
        "reverse dot product cutoff for lbm-based annotation": state.get("lbm_reverse_dot_product", 0.3),
        "matched peaks percentage cutoff for lbm-based annotation": state.get("lbm_matched_peaks_percentage", 0),
        "minimum spectrum match for lbm-based annotation": state.get("lbm_minimum_spectrum_match", 1),
        "use retention information for lbm-based annotation scoring": state.get("lbm_use_rt_scoring", False),
        "use retention information for lbm-based annotation filtering": state.get("lbm_use_rt_filtering", False),
        "together with alignment": state.get("together_with_alignment", True),
        "export as mztabm format": "True",
        "export folder path": state.get("export_folder_path", ""),
        "height matrix export": bool(state.get("height_matrix_export", False)),
        "compounds library file path for rt correction": state.get("rt_correction_anchor_path", ""),
        "rt correction peak selection file path": state.get("rt_correction_selection_path", ""),
        "execute rt correction": bool(state.get("execute_rt_correction", False)),
        "rt correction with smoothing for rt diff": bool(state.get("rt_correction_smooth_rt_diff", False)),
        "user setting intercept": state.get("rt_correction_intercept", 0),
        "rt diff calc method": state.get("rt_correction_diff_method", "SampleMinusSampleAverage"),
        "interpolation method": "Linear",
        "extrapolation method (begin)": state.get("rt_correction_extrapolation_begin", "UserSetting"),
        "extrapolation method (end)": state.get("rt_correction_extrapolation_end", "LastPoint"),
        "rt correction peak selection mode": state.get(
            "rt_correction_peak_selection_mode", "HighestIntensity"
        ),
        "rt correction peak selection rt weight": state.get(
            "rt_correction_peak_selection_rt_weight", 0.5
        ),
    }
    # Keyed by the method-file label, in the order the Console documents them. Every value
    # falls back to the shared default, so what is written is what was validated.
    automatic_rt_replacements: dict[str, Any] = {
        "execute automatic rt correction for alignment": True,
    }
    # The parsed value, not the string given: " 5", "5\n" and "3." are validated as numbers, and
    # written as given a line break split the method line, while bool("false") wrote True.
    for state_key, default in AUTOMATIC_RT_CORRECTION_DEFAULTS.items():
        label = state_key.replace("automatic_rt_correction_", "automatic rt correction ").replace("_", " ")
        raw = state.get(state_key, default)
        try:
            automatic_rt_replacements[label] = automatic_rt_correction_value(state_key, raw)
        except (TypeError, ValueError, OverflowError):
            automatic_rt_replacements[label] = raw
    automatic_rt_enabled = bool(
        project_type == "lcms" and state.get("execute_automatic_rt_correction", False)
    )
    if automatic_rt_enabled:
        replacements.update(automatic_rt_replacements)
    if project_type == "lcms":
        replacements["alignment light mode"] = bool(state.get("alignment_light_mode", False))
        if state.get("annotation_pipeline_profile"):
            replacements["annotation pipeline profile"] = state["annotation_pipeline_profile"]
    if msp_annotator_settings_path is not None:
        replacements["msp annotator settings file path"] = str(msp_annotator_settings_path)
    if text_annotator_settings_path is not None:
        replacements["text annotator settings file path"] = str(text_annotator_settings_path)
    if project_type == "gcms":
        replacements.update(
            {
                "ionization": "EI",
                "machine category": "GCMS",
                "accuracy type": state.get("gcms_accuracy_type", "IsNominal"),
                "ri index file pathes": state.get("ri_dictionary_file_path", ""),
                "ri compound": state.get("gcms_ri_compound_type", "Alkanes"),
                "ri compound type": state.get("gcms_ri_compound_type", "Alkanes"),
                "retention type": state.get("gcms_retention_type", "RT"),
                "alignment index type": state.get("gcms_alignment_index_type", "RT"),
                "retention index alignment tolerance": state.get(
                    "gcms_ri_alignment_tolerance", 10
                ),
                "weighted dot product cutoff": state.get(
                    "msp_weighted_dot_product", 0.5
                ),
                "simple dot product cutoff": state.get(
                    "msp_simple_dot_product", 0.5
                ),
                "reverse dot product cutoff": state.get(
                    "msp_reverse_dot_product", 0.5
                ),
                "matched peaks percentage cutoff": state.get(
                    "msp_matched_peaks_percentage", 0.5
                ),
                "minimum spectrum match": state.get(
                    "msp_minimum_spectrum_match", 3
                ),
            }
        )
    selected = state.get("selected_lipids", [])
    searched = ";".join(
        f"{item['lipid_class']} {item['adduct']}"
        for item in selected
        if item.get("ion_mode") == state.get("ion_mode")
    )
    output: list[str] = []
    found: set[str] = set()
    annotation_inserted = False
    output_aliases = (
        {
            "retention index alignment tolerance": "retention index tolerance for alignment",
            "weighted dot product cutoff": "square root of weighted dot product cutoff for msp-based annotation",
            "simple dot product cutoff": "square root of simple dot product cutoff for msp-based annotation",
            "reverse dot product cutoff": "square root of reverse dot product cutoff for msp-based annotation",
            "matched peaks percentage cutoff": "matched peaks percentage cutoff for msp-based annotation",
            "minimum spectrum match": "minimum spectrum match for msp-based annotation",
        }
        if project_type == "gcms"
        else {}
    )
    for line in lines:
        stripped = line.lstrip()
        line_key = console_method_key(line)
        if line_key in AUTOMATIC_RT_CORRECTION_METHOD_KEYS and not automatic_rt_enabled:
            continue
        if line_key in ("solvent type", "searched lipid class"):
            continue
        if line_key == "adduct list":
            output.append(
                f"Searched adduct ions: {replacements['searched adduct ions']}"
            )
            found.add("searched adduct ions")
            continue
        # The key as the Console reads it, under any spelling it reads as the same setting; every such
        # line is Interactive's to write, so no template line is left to compete with it.
        same_setting = _CONSOLE_KEY_GROUP.get(line_key, frozenset({line_key})) if line_key else frozenset()
        matched = line_key if line_key in replacements else next(
            (key for key in replacements if key in same_setting),
            None,
        )
        if matched:
            output_key = output_aliases.get(matched, matched)
            output.append(f"{_title_for_key(output_key)}: {_method_value(replacements[matched])}")
            found.add(matched)
            found.add(output_key)
            continue
        output.append(line)
        if project_type != "gcms" and stripped.lower() == "# annotation parameter":
            output.append(f"Searched lipid class: {searched}")
            output.append(f"Solvent type: {state.get('solvent', 'CH3COONH4')}")
            annotation_inserted = True
    gcms_no_auto_insert = {
        "searched adduct ions",
        "lbm file path",
        "text db file path",
        "export as mztabm format",
        "weighted dot product cutoff",
        "simple dot product cutoff",
        "reverse dot product cutoff",
        "matched peaks percentage cutoff",
        "minimum spectrum match",
        "rt tolerance for lbm-based annotation",
        "ms1 tolerance for lbm-based annotation",
        "ms2 tolerance for lbm-based annotation",
        "weighted dot product cutoff for lbm-based annotation",
        "simple dot product cutoff for lbm-based annotation",
        "reverse dot product cutoff for lbm-based annotation",
        "matched peaks percentage cutoff for lbm-based annotation",
        "minimum spectrum match for lbm-based annotation",
        "use retention information for lbm-based annotation scoring",
        "use retention information for lbm-based annotation filtering",
        "compounds library file path for rt correction",
        "rt correction peak selection file path",
        "execute rt correction",
        "rt correction with smoothing for rt diff",
        "user setting intercept",
        "rt diff calc method",
        "interpolation method",
        "extrapolation method (begin)",
        "extrapolation method (end)",
    }
    for key, value in replacements.items():
        if key not in found:
            if project_type == "gcms" and key in gcms_no_auto_insert:
                continue
            output.insert(0, f"{_title_for_key(key)}: {_method_value(value)}")
    if project_type == "gcms":
        if "ri compound" not in found:
            output.append(f"RI compound: {state.get('gcms_ri_compound_type', 'Alkanes')}")
    elif not annotation_inserted:
        output.extend(
            [
                "",
                "# Annotation parameter",
                f"Searched lipid class: {searched}",
                f"Solvent type: {state.get('solvent', 'CH3COONH4')}",
            ]
        )
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def _write_msp_annotator_settings(run_directory: Path, state: dict[str, Any]) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        state["msp_annotator_settings_file_path"] = ""
        return None
    rows = [
        row
        for row in state.get("msp_annotators", [])
        if str(row.get("msp_file_path", "")).strip()
    ]
    if not rows:
        state["msp_annotator_settings_file_path"] = ""
        return None

    settings_path = run_directory / "msp_annotator_settings.tsv"
    header = [
        "annotator_id",
        "msp_file_path",
        "priority",
        "rt_tolerance",
        "ms1_tolerance",
        "ms2_tolerance",
        "weighted_dot_product_cutoff",
        "simple_dot_product_cutoff",
        "reverse_dot_product_cutoff",
        "matched_peaks_percentage_cutoff",
        "minimum_spectrum_match",
        "use_retention_information_for_scoring",
        "use_retention_information_for_filtering",
        "target_omics",
        "evidence_tier",
    ]
    with settings_path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "annotator_id": str(row.get("annotator_id", "")).strip() or f"msp_annotator_{index}",
                    "msp_file_path": str(Path(str(row["msp_file_path"])).expanduser().resolve()),
                    "priority": int(row.get("priority") or index),
                    "target_omics": str(row.get("target_omics", "")).strip(),
                    "evidence_tier": str(row.get("evidence_tier", "")).strip(),
                    "rt_tolerance": row.get("rt_tolerance", state.get("msp_rt_tolerance", 100)),
                    "ms1_tolerance": row.get("ms1_tolerance", state.get("ms1_tolerance", 0.01)),
                    "ms2_tolerance": row.get("ms2_tolerance", state.get("ms2_tolerance", 0.025)),
                    "weighted_dot_product_cutoff": row.get("weighted_dot_product_cutoff", state.get("msp_weighted_dot_product", 0.6)),
                    "simple_dot_product_cutoff": row.get("simple_dot_product_cutoff", state.get("msp_simple_dot_product", 0.6)),
                    "reverse_dot_product_cutoff": row.get("reverse_dot_product_cutoff", state.get("msp_reverse_dot_product", 0.8)),
                    "matched_peaks_percentage_cutoff": row.get("matched_peaks_percentage_cutoff", state.get("msp_matched_peaks_percentage", 0.1)),
                    "minimum_spectrum_match": row.get("minimum_spectrum_match", state.get("msp_minimum_spectrum_match", 3)),
                    "use_retention_information_for_scoring": str(bool(row.get("use_rt_scoring", False))),
                    "use_retention_information_for_filtering": str(bool(row.get("use_rt_filtering", False))),
                }
            )
    state["msp_annotator_settings_file_path"] = str(settings_path)
    return settings_path


def _write_text_annotator_settings(run_directory: Path, state: dict[str, Any]) -> Path | None:
    if str(state.get("project_type", "lcms")).lower() != "lcms":
        state["text_annotator_settings_file_path"] = ""
        return None
    rows = [
        row
        for row in state.get("text_annotators", [])
        if str(row.get("text_db_file_path", "")).strip()
    ]
    if not rows:
        state["text_annotator_settings_file_path"] = ""
        return None

    settings_path = run_directory / "text_annotator_settings.tsv"
    header = [
        "annotator_id",
        "text_db_file_path",
        "priority",
        "rt_tolerance",
        "ms1_tolerance",
        "total_score_cutoff",
        "use_retention_information_for_scoring",
        "use_retention_information_for_filtering",
    ]
    with settings_path.open("w", encoding="ascii", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header, delimiter="\t", lineterminator="\n")
        writer.writeheader()
        for index, row in enumerate(rows, start=1):
            writer.writerow(
                {
                    "annotator_id": str(row.get("annotator_id", "")).strip() or f"text_annotator_{index}",
                    "text_db_file_path": str(Path(str(row["text_db_file_path"])).expanduser().resolve()),
                    "priority": int(row.get("priority") or index),
                    "rt_tolerance": row.get("rt_tolerance", state.get("text_rt_tolerance", 0.5)),
                    "ms1_tolerance": row.get("ms1_tolerance", state.get("ms1_tolerance", 0.01)),
                    "total_score_cutoff": row.get("total_score_cutoff", 0.8),
                    "use_retention_information_for_scoring": str(bool(row.get("use_rt_scoring", False))),
                    "use_retention_information_for_filtering": str(bool(row.get("use_rt_filtering", False))),
                }
            )
    state["text_annotator_settings_file_path"] = str(settings_path)
    return settings_path


def _title_for_key(key: str) -> str:
    names = {
        "msp file path": "Msp file path",
        "msp annotator settings file path": "MSP annotator settings file path",
        "lbm file path": "Lbm file path",
        "text db file path": "Text DB file path",
        "text annotator settings file path": "Text annotator settings file path",
        "searched adduct ions": "Searched adduct ions",
        "ion mode": "Ion mode",
        "target omics": "Target omics",
        "ms1 data type": "MS1 data type",
        "ms2 data type": "MS2 data type",
        "number of threads": "Number of threads",
        "smoothing method": "Smoothing method",
        "minimum peak height": "Minimum peak height",
        "mass slice width": "Mass slice width",
        "minimum peak width": "Minimum peak width",
        "retention time begin": "Retention time begin",
        "retention time end": "Retention time end",
        "ms1 tolerance for centroid": "MS1 tolerance for centroid",
        "ms2 tolerance for centroid": "MS2 tolerance for centroid",
        "retention time tolerance for alignment": "Retention time tolerance for alignment",
        "ms1 tolerance for alignment": "MS1 tolerance for alignment",
        "weighted dot product cutoff for msp-based annotation": "Weighted dot product cutoff for MSP-based annotation",
        "simple dot product cutoff for msp-based annotation": "Simple dot product cutoff for MSP-based annotation",
        "reverse dot product cutoff for msp-based annotation": "Reverse dot product cutoff for MSP-based annotation",
        "matched peaks percentage cutoff for msp-based annotation": "Matched peaks percentage cutoff for MSP-based annotation",
        "minimum spectrum match for msp-based annotation": "Minimum spectrum match for MSP-based annotation",
        "annotation pipeline profile": "Annotation pipeline profile",
        "lbm annotator priority": "LBM annotator priority",
        "rt tolerance for lbm-based annotation": "RT tolerance for LBM-based annotation",
        "ms1 tolerance for lbm-based annotation": "MS1 tolerance for LBM-based annotation",
        "ms2 tolerance for lbm-based annotation": "MS2 tolerance for LBM-based annotation",
        "weighted dot product cutoff for lbm-based annotation": "Weighted dot product cutoff for LBM-based annotation",
        "simple dot product cutoff for lbm-based annotation": "Simple dot product cutoff for LBM-based annotation",
        "reverse dot product cutoff for lbm-based annotation": "Reverse dot product cutoff for LBM-based annotation",
        "matched peaks percentage cutoff for lbm-based annotation": "Matched peaks percentage cutoff for LBM-based annotation",
        "minimum spectrum match for lbm-based annotation": "Minimum spectrum match for LBM-based annotation",
        "use retention information for lbm-based annotation scoring": "Use retention information for LBM-based annotation scoring",
        "use retention information for lbm-based annotation filtering": "Use retention information for LBM-based annotation filtering",
        "together with alignment": "Together with alignment",
        "alignment light mode": "Alignment light mode",
        "export as mztabm format": "Export as mztabM format",
        "export folder path": "Export folder path",
        "height matrix export": "Height matrix export",
        "compounds library file path for rt correction": "Compounds library file path for RT correction",
        "rt correction peak selection file path": "RT correction peak selection file path",
        "execute rt correction": "Execute RT correction",
        "rt correction with smoothing for rt diff": "RT correction with smoothing for RT diff",
        "user setting intercept": "User setting intercept",
        "rt diff calc method": "RT diff calc method",
        "interpolation method": "Interpolation method",
        "extrapolation method (begin)": "Extrapolation method (begin)",
        "extrapolation method (end)": "Extrapolation method (end)",
        "rt correction peak selection mode": "RT correction peak selection mode",
        "rt correction peak selection rt weight": "RT correction peak selection RT weight",
        "execute automatic rt correction for alignment": "Execute automatic RT correction for alignment",
        "automatic rt correction reference file id": "Automatic RT correction reference file ID",
        "automatic rt correction rt bin width": "Automatic RT correction RT bin width",
        "automatic rt correction match rt tolerance": "Automatic RT correction match RT tolerance",
        "automatic rt correction minimum anchors": "Automatic RT correction minimum anchors",
        "automatic rt correction maximum anchors": "Automatic RT correction maximum anchors",
        "automatic rt correction minimum sample coverage": "Automatic RT correction minimum sample coverage",
        "automatic rt correction intensity quantile": "Automatic RT correction intensity quantile",
        "automatic rt correction maximum peak width quantile": "Automatic RT correction maximum peak width quantile",
        "automatic rt correction minimum signal to noise": "Automatic RT correction minimum signal to noise",
        "automatic rt correction minimum gaussian similarity": "Automatic RT correction minimum Gaussian similarity",
        "automatic rt correction minimum ideal slope": "Automatic RT correction minimum ideal slope",
        "automatic rt correction outlier mad threshold": "Automatic RT correction outlier MAD threshold",
        "automatic rt correction reference centrality weight": "Automatic RT correction reference centrality weight",
        "automatic rt correction interpolate blanks by analytical order": "Automatic RT correction interpolate blanks by analytical order",
        "ionization": "Ionization",
        "machine category": "Machine category",
        "accuracy type": "Accuracy type",
        "ri index file pathes": "RI index file pathes",
        "ri compound": "RI compound",
        "ri compound type": "RI compound type",
        "retention type": "Retention type",
        "alignment index type": "Alignment index type",
        "retention index alignment tolerance": "Retention index alignment tolerance",
        "retention index tolerance for alignment": "Retention index tolerance for alignment",
        "weighted dot product cutoff": "Weighted dot product cutoff",
        "simple dot product cutoff": "Simple dot product cutoff",
        "reverse dot product cutoff": "Reverse dot product cutoff",
        "matched peaks percentage cutoff": "Matched peaks percentage cutoff",
        "minimum spectrum match": "Minimum spectrum match",
        "square root of weighted dot product cutoff for msp-based annotation": "Square root of weighted dot product cutoff for MSP-based annotation",
        "square root of simple dot product cutoff for msp-based annotation": "Square root of simple dot product cutoff for MSP-based annotation",
        "square root of reverse dot product cutoff for msp-based annotation": "Square root of reverse dot product cutoff for MSP-based annotation",
        "matched peaks percentage cutoff for msp-based annotation": "Matched peaks percentage cutoff for MSP-based annotation",
        "minimum spectrum match for msp-based annotation": "Minimum spectrum match for MSP-based annotation",
    }
    return names[key]


def _nullable_float(value: Any) -> float | None:
    text = str(value or "").strip()
    if not text or text.lower() == "null":
        return None
    try:
        return float(text)
    except ValueError:
        return None


def console_version(console_path: str) -> str:
    path = Path(console_path)
    if not path.exists():
        return ""
    command = ["dotnet", str(path)] if path.suffix.lower() == ".dll" else [str(path)]
    try:
        result = subprocess.run(
            command + ["--version"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    text = (result.stdout + result.stderr).strip()
    match = re.search(r"(?:^|\s)(\d+\.\d+(?:\.\d+)+)(?:\s|$)", text)
    if match:
        return match.group(1)
    try:
        fallback = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    match = re.search(
        r"(?:Version|Application)\s+([0-9.]+)",
        fallback.stdout + fallback.stderr,
        re.I,
    )
    return match.group(1) if match else ""


AUTOMATIC_RT_CORRECTION_LABELS = {
    "automatic_rt_correction_reference_file_id": "reference file ID",
    "automatic_rt_correction_rt_bin_width": "RT bin width",
    "automatic_rt_correction_match_rt_tolerance": "match RT tolerance",
    "automatic_rt_correction_minimum_anchors": "minimum anchors",
    "automatic_rt_correction_maximum_anchors": "maximum anchors",
    "automatic_rt_correction_minimum_sample_coverage": "minimum sample coverage",
    "automatic_rt_correction_intensity_quantile": "intensity quantile",
    "automatic_rt_correction_maximum_peak_width_quantile": "maximum peak-width quantile",
    "automatic_rt_correction_minimum_signal_to_noise": "minimum signal-to-noise",
    "automatic_rt_correction_minimum_gaussian_similarity": "minimum Gaussian similarity",
    "automatic_rt_correction_minimum_ideal_slope": "minimum ideal slope",
    "automatic_rt_correction_outlier_mad_threshold": "outlier MAD threshold",
    "automatic_rt_correction_reference_centrality_weight": "reference centrality weight",
}
_INT32_MIN, _INT32_MAX = -(2**31), 2**31 - 1
# What int/double.TryParse with the invariant culture accepts. Python's float() also takes
# "1_000", full-width digits, "nan" and "infinity", which the Console refuses and replaces
# with its default. Surrounding ASCII whitespace is allowed because the method writer writes
# the parsed number, not the string it was given, so a CR or LF never reaches the file.
_INVARIANT_NUMBER = re.compile(r"\s*[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\s*", re.ASCII)


def _as_float32(value: float) -> float:
    """The value the Console stores for a setting it keeps as a C# float."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _is_managed_pe(binary: bytes) -> bool:
    """True for a .NET assembly: a PE image whose CLI header directory is present.

    A net48 MSDIALCUI.exe is one; a net8 apphost launcher is a native PE (or ELF/Mach-O) with
    no CLI header, and its code is in the MSDIALCUI.dll beside it.
    """
    if binary[:2] != b"MZ" or len(binary) < 0x40:
        return False
    offset = struct.unpack_from("<I", binary, 0x3C)[0]
    if binary[offset:offset + 4] != b"PE\0\0":
        return False
    optional = offset + 24
    if len(binary) < optional + 2:
        return False
    magic = struct.unpack_from("<H", binary, optional)[0]
    directories = optional + (96 if magic == 0x10B else 112)
    cli = directories + 14 * 8
    if len(binary) < cli + 8:
        return False
    rva, size = struct.unpack_from("<II", binary, cli)
    return rva != 0 and size != 0


_CONSOLE_FILE_TYPES = ("Sample", "Standard", "QC", "Blank")


def console_file_type(value: Any) -> str:
    """The analysis-file type as the Console reads it: Sample, Standard, QC or Blank.

    The Console parses it with a case-insensitive enum parse, which also accepts the enum's
    number (Sample 0, Standard 1, QC 2, Blank 3), and falls back to Sample.
    """
    text = str(value if value is not None else "").strip()
    for name in _CONSOLE_FILE_TYPES:
        if text.casefold() == name.casefold():
            return name
    # Enum.TryParse takes an optional sign and ASCII digits; Python's int() also takes "0_3".
    digits = text[1:] if text[:1] in {"+", "-"} else text
    if digits and digits.isascii() and digits.isdigit() and 0 <= int(text) < len(_CONSOLE_FILE_TYPES):
        return _CONSOLE_FILE_TYPES[int(text)]
    return "Sample"


def is_blank_file_type(value: Any) -> bool:
    """True for a file type the Console reads as Blank.

    A literal comparison with "blank" missed a CSV that said 3, and the Console interpolated
    that file as a Blank all the same.
    """
    return console_file_type(value) == "Blank"


def _automatic_rt_correction_value_issues(state: dict[str, Any]) -> list[dict[str, str]]:
    """Refuse every automatic RT-correction value the Console would not use as written.

    The Console reads these as invariant-culture numbers, the counts and the reference ID as
    32-bit whole numbers, and keeps its default for anything it cannot read. A value it
    discards is therefore one the method file, Table S1 and the run disagree about, so it is
    refused here: not a number, not finite, fractional or out of range for a whole-number
    setting, or outside the range the setting means.
    """
    issues: list[dict[str, str]] = []

    def error(message: str) -> None:
        issues.append({"level": "error", "message": f"Automatic RT correction {message}"})

    values: dict[str, float] = {}
    for key, label in AUTOMATIC_RT_CORRECTION_LABELS.items():
        raw = state.get(key, AUTOMATIC_RT_CORRECTION_DEFAULTS[key])
        # float(True) is 1.0, but the method file then says True, which the Console refuses.
        if isinstance(raw, bool) or (
            isinstance(raw, str) and not _INVARIANT_NUMBER.fullmatch(raw)
        ):
            error(f"{label} must be a number, not {raw!r}.")
            continue
        try:
            number = float(raw)
        except (TypeError, ValueError, OverflowError):
            error(f"{label} must be a number, not {raw!r}.")
            continue
        if not math.isfinite(number):
            error(f"{label} must be a finite number, not {raw!r}.")
            continue
        if key in AUTOMATIC_RT_CORRECTION_INTEGER_KEYS:
            if not number.is_integer():
                error(f"{label} must be a whole number.")
                continue
            if not _INT32_MIN <= number <= _INT32_MAX:
                error(f"{label} must be a whole number the Console can read (32-bit).")
                continue
        values[key] = number

    reference = values.get("automatic_rt_correction_reference_file_id")
    files = state.get("files") or []
    if reference is not None:
        if reference < -1:
            error("reference file ID must be -1 (automatic) or a file ID.")
        elif reference >= 0 and files:
            # Console file IDs are the 0-based rows of the analysis-file list, and a missing or
            # Blank reference fails only after every file has been peak-picked.
            index = int(reference)
            if index >= len(files):
                error(
                    f"reference file ID {index} is not a file: IDs are the rows of the file "
                    f"list, 0 to {len(files) - 1}."
                )
            elif is_blank_file_type(files[index].get("file_type")):
                error(f"reference file ID {index} is a Blank, which the Console refuses as the reference.")
    minimum = values.get("automatic_rt_correction_minimum_anchors")
    maximum = values.get("automatic_rt_correction_maximum_anchors")
    if minimum is not None and minimum < 2:
        error("minimum anchors must be at least 2.")
    if minimum is not None and maximum is not None and maximum < minimum:
        error("maximum anchors must be at least the minimum anchors.")
    # Compared as the Console stores them, single precision: 1e-46 is above 0 as a double
    # and 0 as a float, and the Console refuses 0 after peak picking.
    for key in (
        "automatic_rt_correction_rt_bin_width",
        "automatic_rt_correction_match_rt_tolerance",
    ):
        if key in values and not _as_float32(values[key]) > 0:
            error(f"{AUTOMATIC_RT_CORRECTION_LABELS[key]} must be greater than 0.")
    # The correction matches anchors within the alignment MS1 tolerance and refuses a
    # tolerance that is not above 0, again only after every file has been peak-picked.
    try:
        ms1_tolerance = float(state.get("alignment_ms1_tolerance", 0.015))
    except (TypeError, ValueError, OverflowError):
        ms1_tolerance = math.nan
    if isinstance(state.get("alignment_ms1_tolerance"), bool) or not (
        math.isfinite(ms1_tolerance) and _as_float32(ms1_tolerance) > 0
    ):
        error("requires an alignment MS1 tolerance greater than 0.")
    # The Console skips MAD outlier rejection at 0, so 0 is a setting, not an error.
    if (
        "automatic_rt_correction_outlier_mad_threshold" in values
        and not values["automatic_rt_correction_outlier_mad_threshold"] >= 0
    ):
        error("outlier MAD threshold must be 0 (no outlier rejection) or greater.")
    if (
        "automatic_rt_correction_minimum_signal_to_noise" in values
        and not values["automatic_rt_correction_minimum_signal_to_noise"] >= 0
    ):
        error("minimum signal-to-noise must be 0 or greater.")
    # Gaussian similarity and ideal slope are scores in [0, 1]; a floor above 1 leaves no
    # candidate at all, and the Console finds that out only after peak picking.
    for key in (
        "automatic_rt_correction_minimum_sample_coverage",
        "automatic_rt_correction_intensity_quantile",
        "automatic_rt_correction_maximum_peak_width_quantile",
        "automatic_rt_correction_reference_centrality_weight",
        "automatic_rt_correction_minimum_gaussian_similarity",
        "automatic_rt_correction_minimum_ideal_slope",
    ):
        if key in values and not 0 <= values[key] <= 1:
            error(f"{AUTOMATIC_RT_CORRECTION_LABELS[key]} must be between 0 and 1.")
    return issues


def automatic_rt_correction_value(key: str, value: Any) -> Any:
    """Coerce one automatic RT-correction setting without hiding a value the Console refuses.

    A whole number stays an int for the integer keys, and a fractional one stays a float so
    validate_workflow can refuse it, rather than int() quietly writing a different number.
    """
    if key == "automatic_rt_correction_interpolate_blanks_by_analytical_order":
        if isinstance(value, str):
            return value.strip().casefold() in {"1", "true", "yes", "on"}
        return bool(value)
    number = float(value)
    if key in AUTOMATIC_RT_CORRECTION_INTEGER_KEYS and number.is_integer():
        return int(number)
    return number


def console_capabilities(console_path: str) -> dict[str, Any]:
    path = Path(console_path)
    if not path.is_file():
        return {"capability_probe": "missing", "capabilities": []}
    command = ["dotnet", str(path)] if path.suffix.lower() == ".dll" else [str(path)]
    capabilities: set[str] = set()
    probes: list[str] = []

    # Command availability is already represented by System.CommandLine help;
    # do not require MS-DIAL Console to maintain a separate partial inventory.
    try:
        result = subprocess.run(
            command + ["rtcorrection", "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        result = None
    if result is not None and result.returncode == 0:
        help_text = result.stdout + result.stderr
        if "--library" in help_text and "--selection" in help_text:
            capabilities.add(RT_CORRECTION_REVIEW_CAPABILITY)
            probes.append("rtcorrection help")

    # QA export is not a standalone command, so recognize the exact exporter
    # message embedded in compatible builds and verify the artifact after a run.
    # The strings are in the assembly, which inspect_console_path also hashes, so the
    # file a feature is claimed for is the file the run manifest identifies.
    try:
        binary = console_assembly_path(path).read_bytes()
    except OSError:
        binary = b""
    marker = "LC-MS quality-assurance matrix:"
    if marker.encode("utf-8") in binary or marker.encode("utf-16-le") in binary:
        capabilities.add(LCMS_QA_CAPABILITY)
        probes.append("QA exporter marker")
    if any(
        marker.encode("utf-8") in binary or marker.encode("utf-16-le") in binary
        for marker in AUTOMATIC_RT_CORRECTION_CONSOLE_MARKERS
    ):
        capabilities.add(AUTOMATIC_ALIGNMENT_RT_CORRECTION_CAPABILITY)
        probes.append("automatic RT correction marker")
    return {
        "capability_probe": " + ".join(probes) if probes else "unsupported",
        "capabilities": sorted(capabilities),
    }
