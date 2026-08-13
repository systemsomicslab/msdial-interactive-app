from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SKILL = ROOT / "skills" / "msdial-guided-analysis"


def main() -> None:
    parser = argparse.ArgumentParser(description="Package the MS-DIAL Agent Skill for upload.")
    parser.add_argument("--out-dir", default=str(ROOT / "dist"))
    args = parser.parse_args()
    output = Path(args.out_dir).expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    target = output / "msdial-guided-analysis.skill.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(SKILL.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                archive.write(path, Path(SKILL.name) / path.relative_to(SKILL))
    print(target)


if __name__ == "__main__":
    main()
