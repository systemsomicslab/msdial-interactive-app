from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from msdial_app.repository_reanalysis import (
    ADAPTERS,
    EligibilityPolicy,
    cleanup_download_lease,
    create_download_lease,
    discard_download_lease,
    discover_candidates,
    evaluate_eligibility,
    finalize_download_lease,
    project_from_dict,
    run_raw_metadata_preflight,
)
from msdial_app.repository_metadata import (
    metadata_workspace,
    metadata_workspace_from_file,
    project_class_hierarchy,
    save_metadata_review,
)


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Preflight public metabolomics projects for MS-DIAL reanalysis.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect", help="Inspect one public accession without downloading raw data.")
    inspect_parser.add_argument("repository", choices=sorted(ADAPTERS))
    inspect_parser.add_argument("accession")
    _policy_arguments(inspect_parser)

    select_parser = subparsers.add_parser("select", help="Select lightweight eligible projects reproducibly.")
    select_parser.add_argument("repository", choices=sorted(ADAPTERS))
    select_parser.add_argument("--count", type=int, default=10)
    select_parser.add_argument("--seed", type=int, default=20260824)
    select_parser.add_argument("--inspection-limit", type=int, default=200)
    select_parser.add_argument("--workers", type=int, default=4)
    select_parser.add_argument("--output", type=Path)
    _policy_arguments(select_parser)

    download_parser = subparsers.add_parser("download", help="Create a bounded raw-data download lease.")
    download_parser.add_argument("selection", type=Path)
    download_parser.add_argument("accession")
    download_parser.add_argument("--workspace-root", type=Path, default=_default_workspace())
    download_parser.add_argument("--max-download-gb", type=float, default=5.0)
    download_parser.add_argument("--allow-preflight", action="store_true")

    finalize_parser = subparsers.add_parser("finalize", help="Validate retained mzTab-M output and unlock safe cleanup.")
    finalize_parser.add_argument("manifest", type=Path)

    preflight_parser = subparsers.add_parser("preflight", help="Inspect representative raw headers before execution.")
    preflight_parser.add_argument("manifest", type=Path)
    preflight_parser.add_argument("--extractor", type=Path, required=True)
    preflight_parser.add_argument("--max-inputs", type=int, default=3)
    preflight_parser.add_argument("--confirm-untargeted", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="Delete raw data after validated outputs are retained.")
    cleanup_parser.add_argument("manifest", type=Path)
    cleanup_parser.add_argument("--confirmed", action="store_true")

    discard_parser = subparsers.add_parser("discard", help="Delete a rejected preflight download while retaining provenance.")
    discard_parser.add_argument("manifest", type=Path)
    discard_parser.add_argument("--confirmed", action="store_true")

    metadata_parser = subparsers.add_parser(
        "metadata", help="Inspect, project, and save repository sample metadata."
    )
    metadata_subparsers = metadata_parser.add_subparsers(dest="metadata_command", required=True)
    metadata_inspect = metadata_subparsers.add_parser(
        "inspect", help="Extract normalized sample metadata without downloading raw data."
    )
    metadata_inspect.add_argument("repository", choices=sorted(ADAPTERS))
    metadata_inspect.add_argument("accession")
    metadata_inspect.add_argument("--output", type=Path, required=True)
    metadata_project = metadata_subparsers.add_parser(
        "project", help="Project an ordered metadata hierarchy into MS-DIAL Class."
    )
    metadata_project.add_argument("metadata", type=Path)
    metadata_project.add_argument("--field", action="append", default=[], required=True)
    metadata_project.add_argument("--destination", type=Path, required=True)
    metadata_project.add_argument("--analysis-csv", type=Path)
    metadata_project.add_argument("--missing-value", default="NA")
    metadata_project.add_argument("--separator", default="_")

    args = parser.parse_args()
    display_result = None
    if args.command == "inspect":
        adapter = ADAPTERS[args.repository]()
        result = evaluate_eligibility(adapter.inspect(args.accession), _policy(args)).as_dict()
    elif args.command == "select":
        result = discover_candidates(
            args.repository,
            args.count,
            args.seed,
            _policy(args),
            args.inspection_limit,
            args.workers,
        )
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            display_result = {
                "output": str(args.output.resolve()),
                "repository": args.repository,
                "selected_count": len(result.get("selected", [])),
                "raw_metadata_required_count": len(result.get("raw_metadata_required", [])),
                "inspected_count": len(result.get("inspected", [])),
                "selected_accessions": [item["accession"] for item in result.get("selected", [])],
                "raw_metadata_required_accessions": [
                    item["accession"] for item in result.get("raw_metadata_required", [])[:20]
                ],
            }
    elif args.command == "download":
        selection = json.loads(args.selection.read_text(encoding="utf-8"))
        candidates = list(selection.get("selected", []))
        if args.allow_preflight:
            candidates.extend(selection.get("raw_metadata_required", []))
        item = next(
            (entry for entry in candidates if entry.get("accession") == args.accession),
            None,
        )
        if item is None:
            raise ValueError("Accession is not in the eligible selection or approved preflight set.")
        result = create_download_lease(
            project_from_dict(item),
            args.workspace_root,
            int(args.max_download_gb * 1024**3),
            allow_preflight=args.allow_preflight,
        )
    elif args.command == "finalize":
        result = finalize_download_lease(args.manifest)
    elif args.command == "preflight":
        result = run_raw_metadata_preflight(
            args.manifest,
            args.extractor,
            max_inputs=args.max_inputs,
            confirm_untargeted=args.confirm_untargeted,
        )
    elif args.command == "discard":
        result = discard_download_lease(args.manifest, args.confirmed)
    elif args.command == "metadata" and args.metadata_command == "inspect":
        adapter = ADAPTERS[args.repository]()
        inspector = getattr(adapter, "inspect_metadata", adapter.inspect)
        workspace = metadata_workspace(inspector(args.accession).as_dict())
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(workspace, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        result = {
            "metadata_file": str(args.output.resolve()),
            "sample_count": len(workspace.get("rows", [])),
            "field_count": len(workspace.get("fields", [])),
        }
    elif args.command == "metadata" and args.metadata_command == "project":
        workspace = project_class_hierarchy(
            metadata_workspace_from_file(args.metadata),
            args.field,
            args.separator,
            args.missing_value,
        )
        analysis_files = _read_analysis_rows(args.analysis_csv) if args.analysis_csv else []
        result = save_metadata_review(workspace, args.destination, analysis_files)
    else:
        result = cleanup_download_lease(args.manifest, args.confirmed)
    print(json.dumps(display_result or result, indent=2, ensure_ascii=False))
    return 0


def _policy_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--max-download-gb", type=float, default=5.0)
    parser.add_argument("--max-samples", type=int, default=40)
    parser.add_argument("--allow-unknown-size", action="store_true")
    parser.add_argument("--allow-ambiguous-untargeted", action="store_true")


def _policy(args: argparse.Namespace) -> EligibilityPolicy:
    return EligibilityPolicy(
        max_download_bytes=int(args.max_download_gb * 1024**3),
        max_samples=args.max_samples,
        require_known_size=not args.allow_unknown_size,
        require_untargeted=not args.allow_ambiguous_untargeted,
    )


def _default_workspace() -> Path:
    if os.name == "nt" and Path("D:/").exists():
        return Path("D:/MSDIAL_Public_Reanalysis")
    return Path.home() / "MSDIAL_Public_Reanalysis"


def _read_analysis_rows(path: Path) -> list[dict[str, str]]:
    with path.expanduser().resolve().open(encoding="utf-8-sig", newline="") as handle:
        return [dict(row) for row in csv.DictReader(handle)]


if __name__ == "__main__":
    raise SystemExit(main())
