"""Add the dependency inventory to an MS-DIAL Console's existing build record, without rebuilding.

A build record written before inventories were names MSDIALCUI.exe and the git head only, so a
RawDataHandler.dll swapped beside it goes unnoticed. This records the sha256 of every file beside
the binary into that record (console_management.record_console_inventory).

    python scripts/record-console-inventory.py <path to MSDIALCUI.exe>              # preview
    python scripts/record-console-inventory.py <path to MSDIALCUI.exe> --confirmed  # write
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msdial_app.console_management import record_console_inventory


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("console", type=Path, help="MSDIALCUI.exe, or MSDIALCUI.dll for net8")
    parser.add_argument("--confirmed", action="store_true", help="write the record; without it, preview only")
    arguments = parser.parse_args()
    try:
        result = record_console_inventory(arguments.console, confirmed=arguments.confirmed)
    except ValueError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    inspection = result.pop("inspection", None)
    files = result.pop("files", [])
    if files:
        result["file_count"] = len(files)
    if inspection:
        result["provenance_status_after"] = inspection.get("provenance_status")
        result["provenance_warnings_after"] = inspection.get("provenance_warnings")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
