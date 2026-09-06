from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msdial_app.agent_workflow import build_guided_plan
from msdial_app.workflow import prepare_run


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare an LC-MS LBM -> strict MSP -> broad MSP demo workflow."
    )
    parser.add_argument("--input", required=True, help="Raw-data folder, file, or analysis CSV")
    parser.add_argument("--output", required=True, help="Directory for reproducible workflow files")
    parser.add_argument("--console", required=True, help="Patched MSDIALCUI.exe or MSDIALCUI.dll")
    parser.add_argument("--msp", required=True, help="One MSP reused by the strict and broad tiers")
    parser.add_argument("--ion-mode", choices=("Positive", "Negative"), required=True)
    parser.add_argument("--msp-version", default="", help="Library version recorded in provenance")
    parser.add_argument(
        "--target-omics",
        choices=("Metabolomics", "Lipidomics"),
        default="Metabolomics",
    )
    args = parser.parse_args()

    plan = build_guided_plan(
        args.input,
        {
            "project_type": "lcms",
            "ion_mode": args.ion_mode,
            "target_omics": args.target_omics,
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "tiered_lipid_msp",
            "libraries": {
                "msp_paths": [str(Path(args.msp).expanduser().resolve())],
                "msp_version": args.msp_version,
                "msp_source": "institutional library",
                "msp_license": "institutional/private",
            },
            "run_qa": False,
            "generate_materials_methods": True,
            "console_path": str(Path(args.console).expanduser().resolve()),
            "output_root": str(Path(args.output).expanduser().resolve()),
        },
    )
    if not plan["ready_to_prepare"]:
        raise RuntimeError(
            json.dumps(
                {
                    "questions": plan["remaining_questions"],
                    "blockers": plan["blockers"],
                },
                indent=2,
            )
        )
    preparation = prepare_run(plan["workflow"])
    print(json.dumps(preparation, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
