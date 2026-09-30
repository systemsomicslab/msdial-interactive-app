"""Identity of the raw-metadata extractor that preflight runs.

The extractor (msrawdataworkbench RawMetadataConsoleApp) decides a unit's acquisition mode,
polarity and separation, and with them whether the unit runs at all. Until now it was known
only by path, size and modification time, and the binary in use was built from a working
checkout with uncommitted changes, against a moving MsdialWorkbench checkout. This module
gives it the same kind of identity the Console has (console_management.build_local_console
and workflow.inspect_console_path):

- plan_extractor_build previews a build from local clones at pinned commits. It runs no
  build, writes nothing and never adds a worktree to the checkouts it clones from.
- record_build writes a provenance record beside the built binary, once.
- inspect_raw_metadata_extractor says whether the binary on disk is the one the record
  describes: verified, absent, stale_mismatch, unreadable or dirty_source.

The record is local. A shared artifact names the extractor by its commits and checksums,
never by its path.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Mapping

from .workflow import console_git_state


EXTRACTOR_BUILD_PROVENANCE = "raw-metadata-extractor-build-provenance.json"
EXTRACTOR_BUILD_SCHEMA = "msdial-raw-metadata-extractor-build.v1"
# msrawdataworkbench origin/master as of its last fetch (Merge PR #39), and the production
# Console's MsdialWorkbench commit, so that the extractor and MS-DIAL share one Common.
PINNED_MSRAWDATAWORKBENCH_COMMIT = "b34c857a5328e8f08c1918b3d890e7dae50b7d6d"
PINNED_MSDIALWORKBENCH_COMMIT = "c471463a576626650e0886e26bd064cca53a7ae3"
RAW_TREE = "msrawdataworkbench"
COMMON_TREE = "MsdialWorkbench"
EXTRACTOR_PROJECT = Path("RawMetadataConsoleApp/RawMetadataConsoleApp.csproj")
# RawMetadataConsoleApp.csproj and RawDataHandlerStandard.csproj both reference
# ..\..\MsdialWorkbench\src\Common\CommonStandard, so the Common tree has to be a sibling
# of the extractor tree named exactly MsdialWorkbench.
COMMON_PROJECT = Path("src/Common/CommonStandard/CommonStandard.csproj")
EXTRACTOR_CONFIGURATION = "Release"
EXTRACTOR_FRAMEWORK = "net48"
EXTRACTOR_BINARY = "RawMetadataConsoleApp.exe"
# The assemblies compiled from each tree. Their ProductVersion carries the commit they were
# built from (the SDK appends +<SourceRevisionId>), which is how a record that names the
# wrong tree is caught. Newtonsoft.Json and the vendor libraries carry revisions of their
# own, so only these are compared.
TREE_ASSEMBLIES = {
    "RawMetadataConsoleApp.exe": RAW_TREE,
    "RawDataHandler.dll": RAW_TREE,
    "Common.dll": COMMON_TREE,
    "NCDK.dll": COMMON_TREE,
}
REQUIRED_ASSEMBLIES = ("RawMetadataConsoleApp.exe", "RawDataHandler.dll", "Common.dll")
# RawDataHandlerStandard imports Eazfuscator only when this is set. An obfuscated build is
# not the code its commit names.
REMOVED_BUILD_ENVIRONMENT = ("EAZFUSCATOR_NET_HOME",)
PROVENANCE_STATUSES = ("verified", "absent", "stale_mismatch", "unreadable", "dirty_source")
# The clones name their local source "source", so an origin/master in them can only be a
# ref someone fetched from upstream, never the source checkout's own stale master.
CLONE_SOURCE_REMOTE = "source"
_INVENTORY_EXCLUDED = {EXTRACTOR_BUILD_PROVENANCE, EXTRACTOR_BUILD_PROVENANCE + ".tmp"}
_COMMIT_PATTERN = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _now() -> str:
    return dt.datetime.now().astimezone().isoformat()


def _git(root: Path, *arguments: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *arguments],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized(path: Path) -> str:
    return os.path.normcase(os.path.normpath(str(path)))


def _is_within(path: Path, root: Path) -> bool:
    child, parent = _normalized(path), _normalized(root)
    try:
        return os.path.commonpath([child, parent]) == parent
    except ValueError:
        return False


def _relative_posix(path: Path, root: Path) -> str:
    return Path(os.path.relpath(str(path), str(root))).as_posix()


def _without_credentials(url: str) -> str:
    """A remote URL with any user name or token removed, since the plan is shown and kept."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme and "@" in parts.netloc:
        host = parts.netloc.rsplit("@", 1)[1]
        return urllib.parse.urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))
    return url


def assembly_product_version(path: str | Path) -> str:
    """The ProductVersion string of a PE file's version resource, or "" when it has none.

    This is the value Windows shows as the product version, which for an SDK-built .NET
    assembly is its InformationalVersion ("1.0.0+<commit>"). It is read from the bytes,
    without loading or running the file.
    """
    try:
        image = Path(path).read_bytes()
    except OSError:
        return ""
    root_key = "VS_VERSION_INFO".encode("utf-16-le")
    string_key = "ProductVersion".encode("utf-16-le") + b"\0\0"
    position = image.find(root_key)
    while position != -1:
        start = position - 6
        if start >= 0 and position % 2 == 0:
            (length,) = struct.unpack_from("<H", image, start)
            block = image[start:start + length]
            index = block.find(string_key)
            # A String structure starts six bytes before its key, on a 32-bit boundary of
            # the resource; its value follows the key, padded to the next boundary.
            while index != -1:
                entry = index - 6
                if entry >= 0 and entry % 4 == 0:
                    entry_length, _value_length, value_type = struct.unpack_from("<HHH", block, entry)
                    if value_type == 1 and entry_length > 6 + len(string_key):
                        value_start = (index + len(string_key) + 3) & ~3
                        raw = block[value_start:entry + entry_length]
                        text = raw[: len(raw) - len(raw) % 2].decode("utf-16-le", errors="replace")
                        return text.split("\0", 1)[0].strip()
                index = block.find(string_key, index + 2)
        position = image.find(root_key, position + 2)
    return ""


def _embedded_revision(product_version: str) -> str:
    match = re.search(r"\+([0-9a-fA-F]{7,40})(?![0-9a-fA-F])", product_version or "")
    return match.group(1).casefold() if match else ""


def _product_versions(
    output_directory: Path, heads: Mapping[str, str]
) -> dict[str, dict[str, Any]]:
    versions: dict[str, dict[str, Any]] = {}
    for name, tree in TREE_ASSEMBLIES.items():
        path = output_directory / name
        if not path.is_file():
            continue
        product_version = assembly_product_version(path)
        revision = _embedded_revision(product_version)
        head = str(heads.get(tree) or "").casefold()
        versions[name] = {
            "product_version": product_version,
            "tree": tree,
            "embedded_revision": revision,
            # None: the assembly names no revision, so it can neither confirm nor contradict
            # the record.
            "matches_tree_head": bool(head and head.startswith(revision)) if revision else None,
        }
    return versions


def extractor_inventory(output_directory: str | Path) -> list[dict[str, Any]]:
    """Every file of a build output folder except the build record, by relative path.

    The extractor's behaviour depends on the vendor libraries beside it as much as on its
    own code: which of them is present decides which formats it can read. So the identity
    is the whole folder, not the exe alone.
    """
    root = Path(output_directory)
    entries = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = _relative_posix(path, root)
        if relative in _INVENTORY_EXCLUDED:
            continue
        entries.append(
            {"path": relative, "sha256": _sha256_file(path), "size": path.stat().st_size}
        )
    return sorted(entries, key=lambda entry: entry["path"])


def inventory_sha256(entries: list[dict[str, Any]]) -> str:
    """sha256 over the sorted "relative/path<TAB>sha256" lines of an inventory."""
    lines = "".join(
        f"{entry['path']}\t{entry['sha256']}\n"
        for entry in sorted(entries, key=lambda entry: str(entry["path"]))
    )
    return hashlib.sha256(lines.encode("utf-8")).hexdigest()


def extractor_build_root(
    parent_directory: str | Path,
    raw_commit: str = PINNED_MSRAWDATAWORKBENCH_COMMIT,
    common_commit: str = PINNED_MSDIALWORKBENCH_COMMIT,
) -> Path:
    """The folder a pinned build lives in, named by both commits."""
    return Path(parent_directory) / f"RawMetadataExtractor-{raw_commit[:9]}-{common_commit[:9]}"


def extractor_output_directory(raw_tree_root: str | Path) -> Path:
    return (
        Path(raw_tree_root)
        / EXTRACTOR_PROJECT.parent
        / "bin"
        / EXTRACTOR_CONFIGURATION
        / EXTRACTOR_FRAMEWORK
    )


def extractor_build_command(raw_tree_root: str | Path, dotnet: str = "dotnet") -> list[str]:
    """The dotnet build of the extractor project on its own.

    Built without its solution, $(SolutionDir) is empty and RawDataHandlerStandard copies no
    vendor libraries from $(SolutionDir)RawDataAssemblies, so it is passed explicitly. The
    project concatenates it with no separator, hence the trailing one. The list is what runs;
    subprocess quotes it for CreateProcess, doubling a trailing backslash before a closing
    quote, which a command retyped into PowerShell 5.1 does not.
    """
    root = Path(raw_tree_root)
    return [
        dotnet,
        "build",
        str(root / EXTRACTOR_PROJECT),
        "--configuration",
        EXTRACTOR_CONFIGURATION,
        "--nologo",
        f"-p:SolutionDir={root}{os.sep}",
        "-p:ContinuousIntegrationBuild=true",
    ]


def extractor_build_environment(base: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment the build runs in: the given one without EAZFUSCATOR_NET_HOME."""
    source = dict(os.environ if base is None else base)
    removed = {name.casefold() for name in REMOVED_BUILD_ENVIRONMENT}
    return {key: value for key, value in source.items() if key.casefold() not in removed}


def _command_text(command: list[str]) -> str:
    return subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command)


def _step(purpose: str, command: list[str], expect: str = "") -> dict[str, Any]:
    step: dict[str, Any] = {
        "purpose": purpose,
        "command": command,
        "command_text": _command_text(command),
    }
    if expect:
        step["expect"] = expect
    return step


def _default_source_parent() -> Path:
    # The folder that holds the Interactive checkout, where _raw_metadata_extractor_candidates
    # already looks for msrawdataworkbench.
    return Path(__file__).resolve().parent.parent.parent


def _default_common_source(parent: Path, commit: str) -> Path:
    # The production Console's tree is already at the Common pin; the moving checkout shares
    # its objects and is the fallback.
    pinned = parent / f"{COMMON_TREE}-console-{commit[:9]}"
    return pinned if pinned.is_dir() else parent / COMMON_TREE


def _inspect_source(
    tree: str, source: Path, commit: str, project: Path
) -> tuple[dict[str, Any], list[str]]:
    blockers: list[str] = []
    state: dict[str, Any] = {"tree": tree, "source": str(source), "requested_commit": commit}
    top = _git(source, "rev-parse", "--show-toplevel")
    if not top or _normalized(Path(top).resolve()) != _normalized(source):
        blockers.append(f"{tree}: {source} is not the top of a git working tree.")
        return state, blockers
    resolved = _git(source, "rev-parse", "--verify", "--quiet", f"{commit}^{{commit}}")
    state["commit"] = resolved
    if not resolved:
        blockers.append(f"{tree}: commit {commit} is not in {source}.")
        return state, blockers
    if not resolved.casefold().startswith(commit.casefold()):
        blockers.append(f"{tree}: {commit} resolves to {resolved}, which it does not abbreviate.")
    if not _git(source, "cat-file", "-t", f"{resolved}:{project.as_posix()}"):
        blockers.append(f"{tree}: {project.as_posix()} does not exist at {resolved[:12]}.")
    state["upstream_url"] = _without_credentials(
        _git(source, "config", "--get", "remote.origin.url")
    )
    origin_master = _git(source, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/master")
    state["origin_master_head"] = origin_master
    state["commit_is_origin_master"] = bool(origin_master) and origin_master == resolved
    return state, blockers


def plan_extractor_build(
    raw_commit: str = PINNED_MSRAWDATAWORKBENCH_COMMIT,
    common_commit: str = PINNED_MSDIALWORKBENCH_COMMIT,
    parent_directory: str | Path | None = None,
    raw_source: str | Path | None = None,
    common_source: str | Path | None = None,
) -> dict[str, Any]:
    """Preview a pinned extractor build. Nothing is cloned, built or written.

    The two trees are cloned from the local checkouts into one folder named by both
    commits, side by side as the project references expect, and checked out detached. A
    clone copies the source's whole object store, so a commit that only a remote-tracking
    ref reaches (b34c857a5 is origin/master, not an ancestor of any local branch) is there.
    A worktree would do the same but writes .git\\worktrees into the checkouts, which are
    the user's; a clone only reads them.
    """
    for label, value in (("raw_commit", raw_commit), ("common_commit", common_commit)):
        if not _COMMIT_PATTERN.match(str(value or "")):
            raise ValueError(f"{label} must be a 7-40 character hexadecimal commit: {value!r}")
    parent = (
        Path(parent_directory).expanduser() if parent_directory else _default_source_parent()
    ).resolve()
    raw_source_path = (Path(raw_source).expanduser() if raw_source else parent / RAW_TREE).resolve()
    common_source_path = (
        Path(common_source).expanduser()
        if common_source
        else _default_common_source(parent, common_commit)
    ).resolve()
    build_root = extractor_build_root(parent, raw_commit, common_commit)
    raw_destination = build_root / RAW_TREE
    common_destination = build_root / COMMON_TREE

    blockers: list[str] = []
    warnings: list[str] = []
    raw_state, found = _inspect_source(RAW_TREE, raw_source_path, raw_commit, EXTRACTOR_PROJECT)
    blockers.extend(found)
    common_state, found = _inspect_source(
        COMMON_TREE, common_source_path, common_commit, COMMON_PROJECT
    )
    blockers.extend(found)
    for source in (raw_source_path, common_source_path):
        if _is_within(build_root, source):
            blockers.append(
                f"The build folder {build_root} lies inside the source checkout {source}."
            )
    if build_root.exists() and (not build_root.is_dir() or any(build_root.iterdir())):
        blockers.append(
            f"{build_root} already exists. A pinned build is made once in a new folder and "
            "never rebuilt in place."
        )
    dotnet = shutil.which("dotnet")
    if not dotnet:
        blockers.append("dotnet was not found on PATH. The project needs the .NET 10 SDK.")
    if raw_state.get("commit") and not raw_state.get("commit_is_origin_master"):
        warnings.append(
            f"{raw_state['commit'][:12]} is not {RAW_TREE} origin/master as the source last "
            f"fetched it ({str(raw_state.get('origin_master_head') or 'none')[:12]})."
        )

    raw_full = str(raw_state.get("commit") or raw_commit)
    common_full = str(common_state.get("commit") or common_commit)
    output_directory = extractor_output_directory(raw_destination)
    binary = output_directory / EXTRACTOR_BINARY
    command = extractor_build_command(raw_destination, dotnet or "dotnet")
    steps = []
    for state, source, destination, full in (
        (raw_state, raw_source_path, raw_destination, raw_full),
        (common_state, common_source_path, common_destination, common_full),
    ):
        steps.append(
            _step(
                f"Clone {state['tree']} from the local checkout, copying its objects rather "
                "than hardlinking them",
                [
                    "git",
                    "clone",
                    "--no-hardlinks",
                    "--no-checkout",
                    "--origin",
                    CLONE_SOURCE_REMOTE,
                    str(source),
                    str(destination),
                ],
            )
        )
        if state.get("upstream_url"):
            # SourceLink reads the origin remote. Adding it fetches nothing, so the clone
            # holds no origin/* refs until someone fetches from upstream.
            steps.append(
                _step(
                    f"Name the upstream repository of {state['tree']} for SourceLink (no fetch)",
                    ["git", "-C", str(destination), "remote", "add", "origin", state["upstream_url"]],
                )
            )
        steps.append(
            _step(
                f"Check out {state['tree']} at the pinned commit, detached",
                ["git", "-C", str(destination), "checkout", "--detach", full],
            )
        )
    for destination in (raw_destination, common_destination):
        steps.append(
            _step(
                "Confirm the clone is clean before the build",
                ["git", "-C", str(destination), "status", "--porcelain", "--untracked-files=all"],
                expect="no output",
            )
        )
    steps.append(_step("Record the .NET SDK", [dotnet or "dotnet", "--version"]))
    build_step = _step(
        "Build the extractor (the environment omits " + ", ".join(REMOVED_BUILD_ENVIRONMENT) + ")",
        command,
        expect="exit 0",
    )
    build_step["cwd"] = str(raw_destination)
    build_step["environment_removed"] = list(REMOVED_BUILD_ENVIRONMENT)
    steps.append(build_step)
    steps.append(_step("Start the built extractor", [str(binary), "--help"], expect="exit 0"))
    for destination in (raw_destination, common_destination):
        steps.append(
            _step(
                "Confirm the build changed no tracked file",
                ["git", "-C", str(destination), "status", "--porcelain", "--untracked-files=all"],
                expect="no output",
            )
        )

    return {
        "preview_only": True,
        "executed": False,
        "build_root": str(build_root),
        "trees": {
            RAW_TREE: {
                **raw_state,
                "destination": str(raw_destination),
                "project": EXTRACTOR_PROJECT.as_posix(),
            },
            COMMON_TREE: {
                **common_state,
                "destination": str(common_destination),
                "project": COMMON_PROJECT.as_posix(),
            },
        },
        "configuration": EXTRACTOR_CONFIGURATION,
        "framework": EXTRACTOR_FRAMEWORK,
        "output_directory": str(output_directory),
        "binary_path": str(binary),
        "command": command,
        "command_text": _command_text(command),
        "cwd": str(raw_destination),
        "environment_removed": list(REMOVED_BUILD_ENVIRONMENT),
        "environment_note": (
            "Run the build with extractor_build_environment(), which drops "
            + ", ".join(REMOVED_BUILD_ENVIRONMENT)
            + " so no obfuscation is applied."
        ),
        "eazfuscator_net_home_set_here": any(
            key.casefold() == "eazfuscator_net_home" for key in os.environ
        ),
        "steps": steps,
        "record_build": {
            "function": "msdial_app.raw_metadata_extractor.record_build",
            "binary_path": str(binary),
            "raw_source_root": str(raw_destination),
            "common_source_root": str(common_destination),
        },
        "network_steps_not_included": [
            _step(
                f"Confirm upstream {RAW_TREE} origin/master has not moved from the pin",
                ["git", "-C", str(raw_source_path), "fetch", "origin", "--prune"],
            )
        ],
        "restore_note": (
            "dotnet build restores packages from the local NuGet cache and the feeds in the "
            "user's NuGet.Config. A package missing from the cache is a download."
        ),
        "blockers": blockers,
        "warnings": warnings,
    }


def _dotnet_sdk_version(cwd: Path) -> str:
    try:
        result = subprocess.run(
            ["dotnet", "--version"],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=60,
            encoding="utf-8",
            errors="replace",
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def _restore_projects(assets: Mapping[str, Any], project_directory: Path) -> list[Path]:
    """Every project the NuGet restore graph resolved, as absolute paths."""
    paths: list[Path] = []
    restore = (assets.get("project") or {}).get("restore") or {}
    if restore.get("projectPath"):
        paths.append(Path(str(restore["projectPath"])))
    for framework in (restore.get("frameworks") or {}).values():
        references = (framework or {}).get("projectReferences") or {}
        for key, reference in references.items():
            paths.append(Path(str((reference or {}).get("projectPath") or key)))
    for library in (assets.get("libraries") or {}).values():
        if isinstance(library, dict) and library.get("type") == "project":
            relative = library.get("msbuildProject") or library.get("path")
            if relative:
                paths.append(project_directory / str(relative))
    unique: dict[str, Path] = {}
    for path in paths:
        normalized = Path(os.path.normpath(str(path))).resolve()
        unique.setdefault(_normalized(normalized), normalized)
    return list(unique.values())


def _source_record(tree: str, root: Path, pinned: str) -> dict[str, Any]:
    git = console_git_state(root)
    if not git.get("available"):
        raise ValueError(f"{tree} source {root} is not a git working tree, so it cannot be recorded.")
    head = str(git.get("head") or "")
    return {
        "root": str(root),
        "head": head,
        "short_head": head[:12],
        "branch": str(git.get("branch") or ""),
        "dirty": bool(git.get("dirty")),
        "changed_files": int(git.get("changed_files") or 0),
        "working_tree_state_sha256": str(git.get("working_tree_state_sha256") or ""),
        "working_tree_diff_sha256": str(git.get("working_tree_diff_sha256") or ""),
        "origin_master_head": str(git.get("origin_master_head") or ""),
        "cloned_from": _git(root, "config", "--get", f"remote.{CLONE_SOURCE_REMOTE}.url"),
        "pinned_commit": pinned,
        "matches_pin": head.casefold() == pinned.casefold(),
    }


def _write_record(path: Path, record: Mapping[str, Any]) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def record_build(
    binary_path: str | Path,
    raw_source_root: str | Path,
    common_source_root: str | Path,
    plan: Mapping[str, Any] | None = None,
    sdk_version: str | None = None,
    replace: bool = False,
) -> dict[str, Any]:
    """Write the build record beside a built extractor and return its inspection.

    Refuses rather than writes a record the build contradicts: a binary outside the
    extractor tree or other than its Release/net48 exe (or the plan's binary), a Common
    tree that is not the sibling the project references, a restore graph that resolved a
    project from anywhere else, or an assembly whose embedded revision is not its tree's
    head. A record is written once; replace=True overwrites it.
    """
    binary = Path(binary_path).expanduser().resolve()
    raw_root = Path(raw_source_root).expanduser().resolve()
    common_root = Path(common_source_root).expanduser().resolve()
    if not binary.is_file():
        raise FileNotFoundError(f"The built extractor was not found: {binary}")
    if not _is_within(binary, raw_root):
        raise ValueError(f"{binary} is not inside the {RAW_TREE} tree {raw_root}.")
    # The record states the configuration and framework, so the binary has to be the one
    # that build writes. A Debug build, or the net8.0-windows folder Interactive also
    # searches, would otherwise be recorded as Release/net48.
    expected = extractor_output_directory(raw_root) / EXTRACTOR_BINARY
    if _normalized(binary) != _normalized(expected):
        raise ValueError(
            f"{binary} is not {expected}, the {EXTRACTOR_CONFIGURATION}/{EXTRACTOR_FRAMEWORK} "
            "extractor the record describes."
        )
    planned_binary = str((plan or {}).get("binary_path") or "")
    if planned_binary and _normalized(binary) != _normalized(
        Path(planned_binary).expanduser().resolve()
    ):
        raise ValueError(f"{binary} is not the planned binary {planned_binary}.")
    if _normalized(common_root) != _normalized(raw_root.parent / COMMON_TREE):
        raise ValueError(
            f"The project references ..\\..\\{COMMON_TREE}, so the Common tree it compiled is "
            f"{raw_root.parent / COMMON_TREE}, not {common_root}."
        )
    output_directory = binary.parent
    record_path = output_directory / EXTRACTOR_BUILD_PROVENANCE
    if record_path.exists() and not replace:
        raise ValueError(
            f"A build record already exists at {record_path}. A pinned build is recorded once; "
            "pass replace=True only for a deliberate re-record."
        )
    missing = [name for name in REQUIRED_ASSEMBLIES if not (output_directory / name).is_file()]
    if missing:
        raise ValueError(f"The build output lacks {', '.join(missing)}: {output_directory}")

    planned = {
        tree: str((((plan or {}).get("trees") or {}).get(tree) or {}).get("commit") or "")
        for tree in (RAW_TREE, COMMON_TREE)
    }
    sources = {
        RAW_TREE: _source_record(
            RAW_TREE, raw_root, planned[RAW_TREE] or PINNED_MSRAWDATAWORKBENCH_COMMIT
        ),
        COMMON_TREE: _source_record(
            COMMON_TREE, common_root, planned[COMMON_TREE] or PINNED_MSDIALWORKBENCH_COMMIT
        ),
    }
    for tree, source in sources.items():
        if planned[tree] and not source["matches_pin"]:
            raise ValueError(
                f"{tree} is at {source['head'][:12]}, not the planned {planned[tree][:12]}."
            )
    heads = {tree: source["head"] for tree, source in sources.items()}
    product_versions = _product_versions(output_directory, heads)
    contradicted = [
        f"{name} names {entry['embedded_revision'][:12]}, but {entry['tree']} is at "
        f"{heads[entry['tree']][:12]}"
        for name, entry in product_versions.items()
        if entry["matches_tree_head"] is False
    ]
    if contradicted:
        raise ValueError(
            "The binaries were not built from these trees: " + "; ".join(contradicted) + "."
        )
    warnings = [
        f"{name} embeds no source revision in its ProductVersion "
        f"({entry['product_version'] or 'none'})."
        for name, entry in product_versions.items()
        if entry["matches_tree_head"] is None
    ]

    project_directory = raw_root / EXTRACTOR_PROJECT.parent
    assets_path = project_directory / "obj" / "project.assets.json"
    if not assets_path.is_file():
        raise ValueError(
            f"The restore record {assets_path} is missing, so the package graph is unknown."
        )
    try:
        assets = json.loads(assets_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"The restore record {assets_path} could not be read: {error}") from error
    if not isinstance(assets, dict):
        raise ValueError(f"The restore record {assets_path} is not a JSON object.")
    restored = _restore_projects(assets, project_directory)
    restored_root = str(((assets.get("project") or {}).get("restore") or {}).get("projectPath") or "")
    if not restored_root or _normalized(Path(restored_root).resolve()) != _normalized(
        raw_root / EXTRACTOR_PROJECT
    ):
        raise ValueError(
            f"The restore record describes {restored_root or 'no project'}, "
            f"not {raw_root / EXTRACTOR_PROJECT}."
        )
    # The build this replaces resolved CommonStandard from the moving MsdialWorkbench
    # checkout while its record-less binary sat in another tree. Nothing outside the two
    # recorded trees may have been compiled in.
    outside = [
        str(path)
        for path in restored
        if not (_is_within(path, raw_root) or _is_within(path, common_root))
    ]
    if outside:
        raise ValueError(
            "The restore graph resolved projects outside the recorded trees: " + ", ".join(outside)
        )
    restore_projects = sorted(
        (
            {"tree": RAW_TREE, "project": _relative_posix(path, raw_root)}
            if _is_within(path, raw_root)
            else {"tree": COMMON_TREE, "project": _relative_posix(path, common_root)}
            for path in restored
        ),
        key=lambda entry: (entry["tree"], entry["project"]),
    )

    inventory = extractor_inventory(output_directory)
    binary_entry = next(
        entry for entry in inventory if entry["path"] == _relative_posix(binary, output_directory)
    )
    stat = binary.stat()
    if plan:
        command = [str(part) for part in plan.get("command") or []]
        command_source = "plan"
        environment_removed = [str(name) for name in plan.get("environment_removed") or []]
    else:
        command = extractor_build_command(raw_root)
        command_source = "reconstructed"
        environment_removed = []
    obfuscation = (
        "not applied"
        if all(name in environment_removed for name in REMOVED_BUILD_ENVIRONMENT)
        else "not recorded"
    )
    record = {
        "schema": EXTRACTOR_BUILD_SCHEMA,
        "recorded_at": _now(),
        "built_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
        .astimezone()
        .isoformat(),
        "binary_path": str(binary),
        "binary_name": binary.name,
        "binary_sha256": binary_entry["sha256"],
        "binary_size": stat.st_size,
        "output_directory": str(output_directory),
        "inventory_sha256": inventory_sha256(inventory),
        "file_count": len(inventory),
        "inventory": inventory,
        "product_versions": product_versions,
        # Only the checksum: the file also lists the NuGet feeds and the package folder in
        # the user's profile.
        "project_assets_sha256": _sha256_file(assets_path),
        "restore_projects": restore_projects,
        "sources": sources,
        "sources_clean": not any(source["dirty"] for source in sources.values()),
        "sdk_version": _dotnet_sdk_version(raw_root) if sdk_version is None else str(sdk_version),
        "command": command,
        "command_source": command_source,
        "configuration": EXTRACTOR_CONFIGURATION,
        "framework": EXTRACTOR_FRAMEWORK,
        "environment_removed": environment_removed,
        "obfuscation": obfuscation,
        "warnings": warnings,
        "verification": None,
    }
    _write_record(record_path, record)
    return inspect_raw_metadata_extractor(binary)


def _load_record(path: Path) -> dict[str, Any] | None:
    """The record if it has the fields inspection needs, else None."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict) or loaded.get("schema") != EXTRACTOR_BUILD_SCHEMA:
        return None
    inventory = loaded.get("inventory")
    sources = loaded.get("sources")
    if not (
        isinstance(loaded.get("binary_sha256"), str)
        and isinstance(loaded.get("inventory_sha256"), str)
        and isinstance(inventory, list)
        and all(
            isinstance(entry, dict) and "path" in entry and "sha256" in entry
            for entry in inventory
        )
        and isinstance(loaded.get("product_versions", {}), dict)
        and isinstance(sources, dict)
        and all(
            isinstance(sources.get(tree), dict)
            and isinstance(sources[tree].get("head"), str)
            and sources[tree]["head"]
            for tree in (RAW_TREE, COMMON_TREE)
        )
    ):
        return None
    return loaded


def _inventory_differences(
    recorded: list[dict[str, Any]], current: list[dict[str, Any]]
) -> dict[str, list[str]]:
    before = {str(entry["path"]): str(entry["sha256"]) for entry in recorded}
    after = {str(entry["path"]): str(entry["sha256"]) for entry in current}
    return {
        "changed": sorted(path for path in before.keys() & after.keys() if before[path] != after[path]),
        "added": sorted(after.keys() - before.keys()),
        "removed": sorted(before.keys() - after.keys()),
    }


_STATUS_DETAIL = {
    "verified": "",
    "absent": (
        "No build record accompanies this extractor, so what it reads is identified by the "
        "checksums of its exe and its folder alone and not by a source revision."
    ),
    "stale_mismatch": (
        "A build record sits beside this extractor and describes different files, so the "
        "recorded commits name code that would not run. Rebuild into a new folder."
    ),
    "unreadable": "The build record beside this extractor could not be read.",
    "dirty_source": (
        "The record matches these files, but a source tree had uncommitted changes when it "
        "was recorded, so the commits do not fully describe the code."
    ),
}


def inspect_raw_metadata_extractor(path: str | Path) -> dict[str, Any]:
    """Whether the extractor at path is the build its record describes.

    provenance_status is one of PROVENANCE_STATUSES. Every file of the output folder is
    re-hashed, so a vendor library swapped beside an unchanged exe is stale_mismatch.
    Whenever the binary exists the result carries inventory_sha256 and file_count, with or
    without a readable record.
    """
    binary = Path(path).expanduser().resolve()
    if binary.is_dir():
        binary = binary / EXTRACTOR_BINARY
    record_path = binary.parent / EXTRACTOR_BUILD_PROVENANCE
    result: dict[str, Any] = {
        "path": str(binary),
        "exists": binary.is_file(),
        "provenance_path": str(record_path),
    }
    if not binary.is_file():
        result.update(
            provenance_status="absent",
            provenance_verified=False,
            detail=f"The extractor was not found: {binary}",
        )
        return result
    stat = binary.stat()
    binary_sha256 = _sha256_file(binary)
    # Hashed before the record is read: without a readable record, the folder's hash is all
    # that names the vendor libraries the extractor ran with.
    inventory = extractor_inventory(binary.parent)
    current_inventory_sha256 = inventory_sha256(inventory)
    result.update(
        {
            "binary_sha256": binary_sha256,
            "size_bytes": stat.st_size,
            "modified_at": dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.timezone.utc)
            .astimezone()
            .isoformat(),
            "product_version": assembly_product_version(binary),
            "inventory_sha256": current_inventory_sha256,
            "file_count": len(inventory),
        }
    )

    def finish(status: str, **extra: Any) -> dict[str, Any]:
        result.update(extra)
        result["provenance_status"] = status
        result["provenance_verified"] = status == "verified"
        result["detail"] = _STATUS_DETAIL[status]
        return result

    if not record_path.is_file():
        return finish("absent")
    record = _load_record(record_path)
    if record is None:
        return finish("unreadable")

    sources = record["sources"]
    heads = {tree: str(sources[tree].get("head") or "") for tree in (RAW_TREE, COMMON_TREE)}
    result.update(
        {
            "msrawdataworkbench_commit": heads[RAW_TREE],
            "msdialworkbench_commit": heads[COMMON_TREE],
            "pinned": heads[RAW_TREE] == PINNED_MSRAWDATAWORKBENCH_COMMIT
            and heads[COMMON_TREE] == PINNED_MSDIALWORKBENCH_COMMIT,
            "sdk_version": str(record.get("sdk_version") or ""),
            "recorded_at": str(record.get("recorded_at") or ""),
        }
    )

    differences = _inventory_differences(record["inventory"], inventory)
    product_versions = _product_versions(binary.parent, heads)
    result["product_versions"] = product_versions
    recorded_versions = record.get("product_versions") or {}
    version_changes = sorted(
        name
        for name in set(product_versions) | set(recorded_versions)
        if str((product_versions.get(name) or {}).get("product_version") or "")
        != str((recorded_versions.get(name) or {}).get("product_version") or "")
    )
    contradicted = sorted(
        name for name, entry in product_versions.items() if entry["matches_tree_head"] is False
    )
    # The inventory can match a record that was edited to name other commits; the
    # assemblies' own revisions cannot.
    if (
        record["binary_sha256"] != binary_sha256
        or record["inventory_sha256"] != current_inventory_sha256
        or record.get("binary_name", binary.name) != binary.name
        or any(differences.values())
        or version_changes
        or contradicted
    ):
        return finish(
            "stale_mismatch",
            provenance_mismatch={
                "record_path": str(record_path),
                "recorded_binary_sha256": record["binary_sha256"],
                "actual_binary_sha256": binary_sha256,
                "recorded_inventory_sha256": record["inventory_sha256"],
                "actual_inventory_sha256": current_inventory_sha256,
                **differences,
                "product_version_changed": version_changes,
                "revision_contradicts_record": contradicted,
            },
        )
    dirty = [tree for tree in (RAW_TREE, COMMON_TREE) if sources[tree].get("dirty")]
    if dirty:
        return finish("dirty_source", dirty_sources=dirty)
    return finish("verified")


def record_verification(binary_path: str | Path, verification: Mapping[str, Any]) -> dict[str, Any]:
    """Add the post-build verification results to a record that still matches its build.

    The verification block is evidence about the build, not part of its identity, so
    inspection ignores it.
    """
    inspection = inspect_raw_metadata_extractor(binary_path)
    if inspection["provenance_status"] not in {"verified", "dirty_source"}:
        raise ValueError(
            f"The build record is {inspection['provenance_status']}; verify only a build it describes."
        )
    record_path = Path(inspection["provenance_path"])
    record = _load_record(record_path)
    if record is None:
        raise ValueError(f"The build record {record_path} could not be read.")
    record["verification"] = {"recorded_at": _now(), **dict(verification)}
    _write_record(record_path, record)
    return inspect_raw_metadata_extractor(binary_path)
