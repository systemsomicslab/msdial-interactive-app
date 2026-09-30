from __future__ import annotations

import datetime as dt
import json
import os
import platform
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .user_settings import save_path_settings
from .workflow import (
    CONSOLE_BUILD_PROVENANCE,
    CONSOLE_INVENTORY_NOT_RECORDED,
    console_git_state,
    console_inventory,
    inspect_console_path,
)


GITHUB_RELEASES_API = (
    "https://api.github.com/repos/systemsomicslab/MsdialWorkbench/releases?per_page=30"
)
CONSOLE_PROJECT = Path("tests/MSDIAL5/MsdialCoreTestApp/MsdialCoreTestApp.csproj")
SUPPORTED_FRAMEWORKS = {"net472", "net48", "net8"}


def _release_summary(release: dict[str, Any]) -> dict[str, Any]:
    assets = []
    for asset in release.get("assets", []):
        name = str(asset.get("name", ""))
        if "msdial.console" not in name.casefold():
            continue
        assets.append(
            {
                "name": name,
                "url": str(asset.get("browser_download_url", "")),
                "size": int(asset.get("size") or 0),
                "download_count": int(asset.get("download_count") or 0),
            }
        )
    return {
        "tag": str(release.get("tag_name", "")),
        "name": str(release.get("name", "")),
        "prerelease": bool(release.get("prerelease")),
        "published_at": str(release.get("published_at", "")),
        "html_url": str(release.get("html_url", "")),
        "console_assets": assets,
    }


def fetch_official_console_releases(timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        GITHUB_RELEASES_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "MSDIAL-Interactive"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        releases = json.loads(response.read().decode("utf-8"))
    console_releases = [
        _release_summary(item)
        for item in releases
        if not item.get("draft")
        and any(
            "msdial.console" in str(asset.get("name", "")).casefold()
            for asset in item.get("assets", [])
        )
    ]
    stable = next((item for item in console_releases if not item["prerelease"]), None)
    preview = next((item for item in console_releases if item["prerelease"]), None)
    return {
        "checked_at": dt.datetime.now().astimezone().isoformat(),
        "stable": stable,
        "preview": preview,
        "releases": console_releases,
        "source": GITHUB_RELEASES_API,
    }


def prepare_local_console_build(
    source_root: str | Path,
    framework: str = "net48",
    configuration: str = "Release",
) -> dict[str, Any]:
    root = Path(source_root).expanduser().resolve()
    project = root / CONSOLE_PROJECT
    framework = str(framework).strip()
    configuration = str(configuration).strip() or "Release"
    if framework not in SUPPORTED_FRAMEWORKS:
        raise ValueError(f"Unsupported target framework: {framework}")
    if configuration not in {"Release", "Debug", "Release vendor unsupported", "Debug vendor unsupported"}:
        raise ValueError(f"Unsupported build configuration: {configuration}")
    if not project.is_file():
        raise ValueError(f"MS-DIAL Console project was not found: {project}")
    dotnet = shutil.which("dotnet")
    if not dotnet:
        raise ValueError("dotnet was not found on PATH. Install a compatible .NET SDK first.")
    output_name = "MSDIALCUI.dll" if framework == "net8" else "MSDIALCUI.exe"
    output = project.parent / "bin" / configuration / framework / output_name
    command = [
        dotnet,
        "build",
        str(project),
        "--configuration",
        configuration,
        "--framework",
        framework,
        "--nologo",
    ]
    return {
        "source_root": str(root),
        "project": str(project),
        "framework": framework,
        "configuration": configuration,
        "command": command,
        "command_text": subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command),
        "output_path": str(output),
        "git": console_git_state(root),
        "host_os": platform.system(),
    }


def fetch_and_compare_source(source_root: str | Path) -> dict[str, Any]:
    if not str(source_root).strip():
        raise ValueError("Set the local MS-DIAL source root before fetching Git status.")
    root = Path(source_root).expanduser().resolve()
    if not (root / ".git").exists() or not (root / CONSOLE_PROJECT).is_file():
        raise ValueError(f"MS-DIAL Git source tree was not found: {root}")
    result = subprocess.run(
        ["git", "-C", str(root), "fetch", "origin", "--prune"],
        capture_output=True,
        text=True,
        timeout=180,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout).strip() or "git fetch failed.")
    return {
        "checked_at": dt.datetime.now().astimezone().isoformat(),
        "git": console_git_state(root),
        "fetch_output": (result.stdout + result.stderr).strip(),
    }


def build_local_console(
    plan: dict[str, Any],
    log: Callable[[str], None],
    select_after_build: bool = True,
) -> dict[str, Any]:
    log("Building MS-DIAL Console from the selected local source tree.")
    log("Command: " + str(plan["command_text"]))
    # The build overwrites the binary before the new record is written. Remove the
    # old record first so an interrupted build leaves no provenance at all rather
    # than a record that appears to describe the binary now on disk.
    superseded = Path(str(plan["output_path"])).parent / CONSOLE_BUILD_PROVENANCE
    if superseded.is_file():
        try:
            superseded.unlink()
            log(f"Removed the superseded build-provenance record: {superseded}")
        except OSError as error:
            log(f"Could not remove the superseded build-provenance record: {error}")
    process = subprocess.Popen(
        list(plan["command"]),
        cwd=str(plan["source_root"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert process.stdout is not None
    for line in process.stdout:
        log(line.rstrip())
    exit_code = process.wait()
    if exit_code != 0:
        raise RuntimeError(f"dotnet build exited with code {exit_code}.")
    output = Path(str(plan["output_path"]))
    if not output.is_file():
        raise FileNotFoundError(f"Build completed but Console output was not found: {output}")
    git = console_git_state(plan["source_root"])
    unrecorded = inspect_console_path(output, "local source build")
    # The record names the assembly, which inspect_console_path checks it against. The
    # output is already the assembly (MSDIALCUI.dll for net8), so this is the same file;
    # recording it this way keeps the writer and the reader on one file by construction.
    provenance = {
        "schema_version": 1,
        "built_at": dt.datetime.now().astimezone().isoformat(),
        "binary_path": unrecorded["assembly_path"],
        "binary_sha256": unrecorded["assembly_sha256"],
        "binary_version": unrecorded.get("version", ""),
        "source_root": str(plan["source_root"]),
        "git_branch": git.get("branch", ""),
        "git_head": git.get("head", ""),
        "git_dirty": bool(git.get("dirty")),
        "changed_files": int(git.get("changed_files") or 0),
        "working_tree_diff_sha256": git.get("working_tree_diff_sha256", ""),
        "working_tree_state_sha256": git.get("working_tree_state_sha256", ""),
        "framework": plan["framework"],
        "configuration": plan["configuration"],
        "command": plan["command"],
        # Every file the build put beside the binary, so a dependency swapped after it (a
        # RawDataHandler.dll from another package) is not the build this record describes.
        **_inventory_fields(output.parent, "at_build"),
    }
    provenance_path = output.parent / CONSOLE_BUILD_PROVENANCE
    provenance_path.write_text(
        json.dumps(provenance, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    if select_after_build:
        save_path_settings(
            {
                "console_path": str(output),
                "console_source_kind": "local_source_build",
                "console_source_root": str(plan["source_root"]),
            }
        )
    result = inspect_console_path(output, "local source build")
    result.update(
        {
            "exit_code": exit_code,
            "selected": select_after_build,
            "provenance_path": str(provenance_path),
        }
    )
    return result


def _inventory_fields(output_directory: Path, when: str) -> dict[str, Any]:
    """The inventory a build record carries. A folder too large to inventory records only that."""
    inventory = console_inventory(output_directory)
    fields: dict[str, Any] = {
        "inventory_status": inventory["inventory_status"],
        "inventory_recorded": when,
        "inventory_recorded_at": dt.datetime.now().astimezone().isoformat(),
        "key_assemblies": inventory["key_assemblies"],
    }
    if inventory["inventory_status"] == "complete":
        fields.update(
            {
                "inventory_sha256": inventory["inventory_sha256"],
                "inventory_file_count": inventory["inventory_file_count"],
                "inventory": inventory["inventory"],
            }
        )
    return fields


def record_console_inventory(console_path: str | Path, confirmed: bool = False) -> dict[str, Any]:
    """Add the dependency inventory to an existing build record, without rebuilding.

    A record written before inventories were (the pinned c471463a5 and f56d4478a builds) names
    MSDIALCUI.exe and the git head only, and inspects as verified with the warning
    inventory_not_recorded. This adds the sha256 of every file now beside the binary, and the key
    assemblies' ProductVersions, and leaves every other field of the record as it was. The record
    says the inventory was taken after the build (inventory_recorded: after_build, and when), since
    it vouches for the folder as it is now, not as the build left it.

    Only a record that still names this binary takes an inventory: a stale or unreadable one
    describes something else, and one that already carries an inventory is left alone. With
    confirmed=False nothing is written and the result is the preview: the record, the digest and
    the files it would hold. With confirmed=True the record is replaced atomically and the result
    carries the inspection after the write.
    """
    inspected = inspect_console_path(console_path)
    if not inspected.get("exists"):
        raise ValueError(f"The MS-DIAL Console was not found: {inspected.get('path')}")
    record_path = Path(str(inspected["path"])).parent / CONSOLE_BUILD_PROVENANCE
    status = str(inspected.get("provenance_status") or "absent")
    plan: dict[str, Any] = {
        "record_path": str(record_path),
        "provenance_status": status,
        "confirmed": bool(confirmed),
        "written": False,
    }
    if status == "verified" and CONSOLE_INVENTORY_NOT_RECORDED not in (
        inspected.get("provenance_warnings") or []
    ):
        # It carries one already, or its build found the folder too large to inventory.
        return {
            **plan,
            "reason": "already_recorded"
            if "inventory_sha256" in (inspected.get("provenance") or {})
            else "inventory_too_large",
            "inventory_sha256": inspected.get("inventory_sha256", ""),
            "inspection": inspected,
        }
    if status != "verified":
        raise ValueError(
            f"The build record beside this Console is {status}, so it does not name this binary; "
            "rebuild through msdial_build_console_from_local_source rather than add an inventory to it."
        )
    record = json.loads(record_path.read_text(encoding="utf-8-sig"))
    fields = _inventory_fields(record_path.parent, "after_build")
    plan.update(
        {
            "inventory_status": fields["inventory_status"],
            "inventory_sha256": fields.get("inventory_sha256", ""),
            "inventory_file_count": fields.get("inventory_file_count", 0),
            "key_assemblies": fields["key_assemblies"],
            "files": [entry["path"] for entry in fields.get("inventory", [])],
        }
    )
    if not confirmed:
        return plan
    record.update(fields)
    # Beside the record, under a name the inventory leaves out, and moved over it in one step.
    temporary = record_path.with_name(record_path.name + ".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, record_path)
    return {**plan, "written": True, "inspection": inspect_console_path(console_path)}
