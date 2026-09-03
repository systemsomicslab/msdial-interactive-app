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
    console_git_state,
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
    provenance = {
        "schema_version": 1,
        "built_at": dt.datetime.now().astimezone().isoformat(),
        "binary_path": str(output),
        "binary_sha256": unrecorded["binary_sha256"],
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
