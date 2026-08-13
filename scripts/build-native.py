from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
OUTPUT = ROOT / "dist-native"
ARTIFACTS = ROOT / "release-artifacts"
NAME = "MS-DIAL-Interactive"


def main() -> None:
    shutil.rmtree(OUTPUT, ignore_errors=True)
    separator = os.pathsep
    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        NAME,
        "--distpath",
        str(OUTPUT),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build"),
        "--add-data",
        f"{ROOT / 'static'}{separator}static",
        "--add-data",
        f"{ROOT / 'resources'}{separator}resources",
        "--add-data",
        f"{ROOT / 'knowledge'}{separator}knowledge",
    ]
    if sys.platform == "darwin":
        command.append("--windowed")
    command.append(str(ROOT / "app.py"))
    subprocess.run(command, check=True, cwd=ROOT)

    package = OUTPUT / NAME
    if sys.platform == "darwin":
        package = OUTPUT / f"{NAME}.app"
    for document in ("LICENSE", "COPYING", "COPYING.LESSER", "README.md"):
        source = ROOT / document
        if source.is_file():
            destination = OUTPUT / document
            shutil.copy2(source, destination)
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    platform_name = {"win32": "windows", "darwin": "macos"}.get(
        sys.platform, "linux"
    )
    archive_format = "zip" if sys.platform == "win32" else "gztar"
    archive = shutil.make_archive(
        str(ARTIFACTS / f"msdial-interactive-{platform_name}"),
        archive_format,
        root_dir=OUTPUT,
    )
    print(package)
    print(archive)


if __name__ == "__main__":
    main()
