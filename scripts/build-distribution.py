from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DEFAULT_NAME = "msdial-interactive-app-local"
INCLUDE_PATHS = [
    "app.py",
    "pyproject.toml",
    "README.md",
    "docs",
    "knowledge",
    "msdial_app",
    "resources",
    "scripts",
    "static",
    "tests",
]
EXCLUDE_DIRS = {
    ".git",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "runs",
    "work",
}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def should_include(path: Path) -> bool:
    relative_parts = path.relative_to(ROOT).parts
    if any(part in EXCLUDE_DIRS for part in relative_parts):
        return False
    if path.suffix in EXCLUDE_SUFFIXES:
        return False
    return True


def iter_files() -> list[Path]:
    files: list[Path] = []
    for item in INCLUDE_PATHS:
        path = ROOT / item
        if path.is_file() and should_include(path):
            files.append(path)
        elif path.is_dir():
            files.extend(child for child in path.rglob("*") if child.is_file() and should_include(child))
    return sorted(files)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a local-user distribution ZIP.")
    parser.add_argument("--name", default=DEFAULT_NAME)
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    zip_path = out_dir / f"{args.name}.zip"
    root_folder = args.name

    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for file_path in iter_files():
            relative = file_path.relative_to(ROOT)
            archive_name = f"{root_folder}/{relative.as_posix()}"
            archive.write(file_path, archive_name)
            if file_path.suffix in {".sh", ".command"}:
                info = archive.getinfo(archive_name)
                info.external_attr = 0o755 << 16

    print(zip_path)


if __name__ == "__main__":
    main()
