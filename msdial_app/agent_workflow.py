from __future__ import annotations

import copy
import math
from bisect import bisect_left
from pathlib import Path
from typing import Any

from .annotation_pipeline import apply_tiered_lcms_annotation
from .library_catalog import catalog_status
from .user_settings import load_user_settings
from .workflow import (
    expand_paths_report,
    discover_console_paths,
    load_parameter_template,
    read_adducts,
    read_analysis_csv,
    read_lipid_queries,
    validate_workflow,
)
from .worksets import get_workset


ROOT = Path(__file__).resolve().parent.parent
RESOURCES = ROOT / "resources"
SUPPORTED_PROJECT_TYPES = {"lcms", "gcms"}
SUPPORTED_ANSWER_KEYS = {
    "project_type", "ion_mode", "target_omics", "parameter_strategy",
    "target_peak_count", "target_peak_count_min", "target_peak_count_max",
    "minimum_peak_height", "smoothing_method", "acquisition_type",
    "execute_rt_correction", "rt_correction_anchor_path",
    "rt_correction_selection_path", "rt_correction_peak_selection_mode",
    "rt_correction_peak_selection_rt_weight", "library_strategy", "libraries",
    "library_provenance", "run_qa", "internal_standards",
    "generate_materials_methods", "alignment_light_mode", "output_root",
    "export_folder_path", "height_matrix_export", "console_path", "template_path",
    "queries_path", "project_store", "workflow_overrides", "repository_metadata_path",
    "gcms_retention_type",
    "gcms_alignment_index_type", "gcms_ri_compound_type", "gcms_ri_source",
    "gcms_ri_standard_path", "gcms_ri_dictionary_path",
}


def inspect_analysis_input(input_path: str) -> dict[str, Any]:
    value = str(input_path).strip()
    if not value:
        return {
            "input_path": "",
            "files": [],
            "warnings": [],
            "rejected": [],
            "formats": [],
            "vendors": [],
            "default_output_root": "",
        }
    path = Path(value).expanduser().resolve()
    if path.is_file() and path.suffix.casefold() == ".csv":
        report = read_analysis_csv(path)
        default_output = path.parent
    else:
        report = expand_paths_report([str(path)])
        if path.is_dir() and path.suffix.casefold() not in {".d", ".raw"}:
            default_output = path
        else:
            default_output = path.parent
    files = report.get("files", [])
    return {
        "input_path": str(path),
        "files": files,
        "file_count": len(files),
        "warnings": report.get("warnings", []),
        "rejected": report.get("rejected", []),
        "formats": sorted({str(item.get("format", "Unknown")) for item in files}),
        "vendors": sorted({str(item.get("vendor", "Unknown")) for item in files}),
        "default_output_root": str(default_output),
        "analysis_csv_source": str(path) if path.suffix.casefold() == ".csv" else "",
        # How the proposed grouping was arrived at, and what else it could have been.
        # The grouping is a scientific decision; the file names only suggest it, so the
        # reasoning travels with the proposal for a person to accept or replace.
        "class_proposal": _describe_class_proposal(files),
    }


def _describe_class_proposal(files: list[dict[str, Any]]) -> dict[str, Any]:
    from .sample_grouping import propose_grouping

    names = [str(item.get("file_name", "")) for item in files]
    if not names:
        return {"reason": "no files were recognised", "alternatives": [], "groups": {}}
    grouping = propose_grouping(names)
    groups: dict[str, list[str]] = {}
    for item in files:
        groups.setdefault(str(item.get("class_id", "Sample")), []).append(
            str(item.get("file_name", ""))
        )
    chosen = grouping.get("chosen")
    return {
        "reason": grouping.get("reason", ""),
        "groups": {label: len(members) for label, members in sorted(groups.items())},
        "alternatives": [
            {
                "label": " / ".join(candidate["values"]),
                "group_count": candidate["group_count"],
                "smallest_group": candidate["smallest_group"],
                "chosen": chosen is not None and candidate["position"] == chosen["position"],
            }
            for candidate in grouping.get("candidates", [])[:5]
        ],
        "confirmation_required": True,
    }


def build_guided_plan(
    input_path: str,
    answers: dict[str, Any] | None = None,
    workset_id: str = "",
) -> dict[str, Any]:
    supplied = dict(answers or {})
    workset = get_workset(workset_id)
    merged = dict((workset or {}).get("answers", {}))
    merged.update(supplied)
    unknown_answer_keys = sorted(set(merged) - SUPPORTED_ANSWER_KEYS)
    inspection = inspect_analysis_input(input_path)
    questions = _questions(merged)
    blockers: list[str] = []
    official_library: dict[str, Any] | None = None
    if not inspection["files"]:
        blockers.append("No supported analysis files were found at input_path.")
    project_type = str(merged.get("project_type", "")).casefold()
    if project_type and project_type not in SUPPORTED_PROJECT_TYPES:
        blockers.append(
            f"Project type '{project_type}' is not supported by guided execution yet; use LC-MS or GC-MS."
        )
    if merged.get("library_strategy") == "official" and _can_resolve_official(merged):
        catalog_id = _official_catalog_id(merged)
        item = next((entry for entry in catalog_status() if entry["id"] == catalog_id), None)
        official_library = item
        if item and not item.get("downloaded"):
            blockers.append(
                f"Official library '{catalog_id}' is not downloaded. Ask for confirmation, then download it."
            )
    if merged.get("library_strategy") == "tiered_lipid_msp":
        item = next(
            (entry for entry in catalog_status() if entry["id"] == "lipidomics"),
            None,
        )
        official_library = item
        if item and not item.get("downloaded"):
            blockers.append(
                "The official lipidomics LBM library is not downloaded. Ask for confirmation, then download it."
            )
    tuning_strategy = merged.get("parameter_strategy") in {
        "target_peak_count", "auto_peak_range"
    }
    if tuning_strategy and not _has_minimum_peak_height(merged):
        blockers.append(
            "Peak-count tuning requires a diagnostic run and an accepted minimum_peak_height."
        )

    workflow = _workflow(inspection, merged) if not questions else None
    validation: list[dict[str, str]] = []
    if workflow is not None:
        validation = validate_workflow(workflow)
        blockers.extend(
            item["message"] for item in validation if item.get("level") == "error"
        )
    return {
        "schema": "msdial-interactive.guided-plan.v1",
        "input": inspection,
        "workset": workset,
        "answers": merged,
        "next_question": questions[0] if questions else None,
        "remaining_questions": questions,
        "workflow": workflow,
        "validation": validation,
        "warnings": [
            *inspection.get("warnings", []),
            *[f"Unknown answers key was not applied: {key}" for key in unknown_answer_keys],
        ],
        "unknown_answer_keys": unknown_answer_keys,
        "blockers": list(dict.fromkeys(blockers)),
        "official_library": official_library,
        "ready_to_prepare": not questions and not blockers,
        "requires_diagnostic": tuning_strategy and not _has_minimum_peak_height(merged),
        "post_run_actions": {
            "quality_assurance": _as_bool(merged.get("run_qa")),
            "internal_standards": merged.get("internal_standards", []),
            "materials_and_methods": _as_bool(
                merged.get("generate_materials_methods")
            ),
        },
    }


def estimate_peak_height(heights: list[float], target_peak_count: int) -> dict[str, Any]:
    values = sorted(float(value) for value in heights if float(value) >= 0)
    target = max(1, int(target_peak_count))
    if not values:
        raise ValueError("The diagnostic result contains no peak heights.")
    index = max(0, len(values) - min(target, len(values)))
    threshold = values[index]
    detected = len(values) - index
    return {
        "minimum_peak_height": threshold,
        "target_peak_count": target,
        "estimated_peak_count": detected,
        "diagnostic_peak_count": len(values),
        "method": "height order statistic",
        "note": "Review this threshold in the Tune parameters view before production use.",
    }


def estimate_peak_height_range(
    heights: list[float],
    minimum_peak_count: int = 3000,
    maximum_peak_count: int = 6000,
    threshold_step: int = 100,
) -> dict[str, Any]:
    values = sorted(float(value) for value in heights if float(value) >= 0)
    minimum = max(0, int(minimum_peak_count))
    maximum = max(minimum, int(maximum_peak_count))
    step = max(1, int(threshold_step))
    if not values:
        raise ValueError("The diagnostic result contains no peak heights.")

    if len(values) <= maximum:
        return {
            "minimum_peak_height": 0,
            "target_peak_count_min": minimum,
            "target_peak_count_max": maximum,
            "estimated_peak_count": len(values),
            "diagnostic_peak_count": len(values),
            "threshold_step": step,
            "within_target_range": minimum <= len(values) <= maximum,
            "method": "quantized height-range search",
            "note": (
                "The zero-threshold diagnostic did not exceed the upper peak-count bound; "
                "Minimum peak height remains 0."
            ),
        }

    candidates = {0}
    for value in values:
        lower = max(0, math.floor(value / step) * step)
        candidates.add(lower)
        candidates.add(lower + step)
    midpoint = (minimum + maximum) / 2

    def candidate_score(threshold: int) -> tuple[float, float, int]:
        count = len(values) - bisect_left(values, threshold)
        distance = max(minimum - count, 0, count - maximum)
        return distance, abs(count - midpoint), threshold

    threshold = min(candidates, key=candidate_score)
    detected = len(values) - bisect_left(values, threshold)
    return {
        "minimum_peak_height": threshold,
        "target_peak_count_min": minimum,
        "target_peak_count_max": maximum,
        "estimated_peak_count": detected,
        "diagnostic_peak_count": len(values),
        "threshold_step": step,
        "within_target_range": minimum <= detected <= maximum,
        "method": "quantized height-range search",
        "note": (
            "The threshold is constrained to the instrument-family step. Review the "
            "diagnostic count when no stepped threshold can enter the requested range."
        ),
    }


def select_peak_tuning_representative(
    files: list[dict[str, Any]], requested_file: str = ""
) -> dict[str, Any]:
    if not files:
        raise ValueError("No analysis file is available for peak-count tuning.")
    requested = str(requested_file or "").strip().casefold()
    if requested:
        selected = next(
            (item for item in files if str(item.get("file_path") or "").casefold() == requested),
            None,
        )
        if selected is None:
            raise ValueError("The requested representative file is not part of this analysis unit.")
        reason = "user-selected"
    else:
        qc = [item for item in files if _is_qc_file(item)]
        candidates = qc or [item for item in files if not _is_blank_file(item)] or list(files)
        orders = [float(item.get("analytical_order") or 0) for item in files]
        midpoint = (min(orders) + max(orders)) / 2 if orders else 0
        selected = min(
            candidates,
            key=lambda item: (
                abs(float(item.get("analytical_order") or 0) - midpoint),
                str(item.get("file_path") or "").casefold(),
            ),
        )
        reason = "QC-nearest-run-midpoint" if qc else "non-blank-nearest-run-midpoint"
    instrument_family = str(selected.get("instrument_family") or "Unknown")
    family = instrument_family.casefold()
    threshold_step = 1000 if ("fourier" in family or "ft-icr" in family) else 100
    return {
        "file": selected,
        "file_path": str(selected.get("file_path") or ""),
        "file_name": str(selected.get("file_name") or ""),
        "selection_reason": reason,
        "instrument_family": instrument_family,
        "threshold_step": threshold_step,
        "target_peak_count_min": 3000,
        "target_peak_count_max": 6000,
    }


def _has_minimum_peak_height(answers: dict[str, Any]) -> bool:
    value = answers.get("minimum_peak_height")
    return value is not None and str(value).strip() != ""


def _is_qc_file(item: dict[str, Any]) -> bool:
    values = (
        item.get("file_type"), item.get("class_id"), item.get("file_name")
    )
    return any(str(value or "").strip().casefold() == "qc" for value in values[:2]) or any(
        token == "qc"
        for token in str(values[2] or "").replace("-", "_").casefold().split("_")
    )


def _is_blank_file(item: dict[str, Any]) -> bool:
    return any(
        str(item.get(key) or "").strip().casefold() == "blank"
        for key in ("file_type", "class_id")
    )


def _questions(answers: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def ask(identifier: str, prompt: str, choices: list[str] | None = None) -> None:
        result.append(
            {
                "id": identifier,
                "prompt": prompt,
                "choices": choices or [],
                "required": True,
                "presentation": "neutral",
            }
        )

    project_type = str(answers.get("project_type", "")).casefold()
    if not project_type:
        ask("project_type", "Is this LC-MS or GC-MS data?", ["lcms", "gcms"])
        return result
    if project_type not in SUPPORTED_PROJECT_TYPES:
        return result
    if project_type == "lcms":
        if not answers.get("ion_mode"):
            ask("ion_mode", "Which ion mode was used?", ["Positive", "Negative"])
        if not answers.get("target_omics"):
            ask("target_omics", "Is the target Metabolomics or Lipidomics?", ["Metabolomics", "Lipidomics"])
    if not answers.get("parameter_strategy"):
        ask(
            "parameter_strategy",
            "Use template defaults, automatically tune to 3,000-6,000 peaks, or choose an exact target?",
            ["default", "auto_peak_range", "target_peak_count"],
        )
    elif answers.get("parameter_strategy") == "target_peak_count" and not answers.get(
        "target_peak_count"
    ):
        ask("target_peak_count", "Approximately how many peaks should one representative sample contain?")
    if project_type == "lcms":
        if "execute_rt_correction" not in answers:
            ask("execute_rt_correction", "Apply retention-time correction?", ["false", "true"])
        elif _as_bool(answers.get("execute_rt_correction")):
            if not answers.get("rt_correction_anchor_path"):
                ask("rt_correction_anchor_path", "Provide the RT-correction anchor library path.")
            if not answers.get("rt_correction_peak_selection_mode"):
                ask(
                    "rt_correction_peak_selection_mode",
                    "How should an anchor peak be selected?",
                    ["HighestIntensity", "ClosestToReferenceRt", "Weighted"],
                )
    if project_type == "gcms" and not answers.get("gcms_retention_type"):
        ask("gcms_retention_type", "Use retention time or retention index?", ["RT", "RI"])
    if project_type == "gcms" and answers.get("gcms_retention_type") == "RI":
        if not answers.get("gcms_ri_compound_type"):
            ask("gcms_ri_compound_type", "Use Kovats alkane RI or Fiehn FAME RI?", ["Alkanes", "Fames"])
        if not answers.get("gcms_ri_standard_path") and not answers.get("gcms_ri_dictionary_path"):
            ask("gcms_ri_standard_path", "Provide the alkane/FAME carbon-number and RT file path.")
    if not answers.get("library_strategy") or answers.get("library_strategy") == "ask":
        ask(
            "library_strategy",
            "Which annotation libraries should be used?",
            ["official", "existing", "tiered_lipid_msp", "none"],
        )
    elif answers.get("library_strategy") == "existing" and not _has_existing_library(answers):
        ask(
            "libraries",
            "Provide msp_paths and optional text_paths/lbm_path for the existing libraries.",
        )
    elif answers.get("library_strategy") == "tiered_lipid_msp" and not _has_msp_library(answers):
        ask(
            "libraries",
            "Provide one msp_paths entry for the high- and low-quality MSP tiers. The official LBM library is used as tier 1.",
        )
    if project_type == "lcms" and "run_qa" not in answers:
        ask("run_qa", "Generate the LC-MS quality-assurance report after analysis?", ["true", "false"])
    if "generate_materials_methods" not in answers:
        ask(
            "generate_materials_methods",
            "Generate Materials and Methods text and supplementary tables?",
            ["true", "false"],
        )
    return result


def _workflow(inspection: dict[str, Any], answers: dict[str, Any]) -> dict[str, Any]:
    project_type = str(answers["project_type"]).casefold()
    settings = load_user_settings()
    queries_path = _existing_path(
        answers.get("queries_path") or settings.get("queries_path"),
        RESOURCES / "LbmQueries.txt",
    )
    if project_type == "gcms":
        template = _existing_path(
            answers.get("template_path"),
            RESOURCES / "gcms_console_param_kovats.txt",
        )
    else:
        template = _existing_path(
            answers.get("template_path") or settings.get("template_path"),
            RESOURCES / "msdial_console_param4lipidomics.txt",
        )
    loaded = load_parameter_template(template, queries_path)
    state = dict(loaded["workflow"])
    output_root = str(answers.get("output_root") or inspection["default_output_root"])
    run_qa = _as_bool(answers.get("run_qa"))
    state.update(
        {
            "files": copy.deepcopy(inspection["files"]),
            "project_type": project_type,
            "ion_mode": answers.get("ion_mode", "Positive"),
            "target_omics": answers.get("target_omics", "Metabolomics"),
            "console_path": str(
                answers.get("console_path")
                or settings.get("console_path")
                or _default_console_path()
            ),
            "template_path": str(template.resolve()),
            "output_root": output_root,
            "run_qa": run_qa,
            "generate_materials_methods": _as_bool(answers.get("generate_materials_methods")),
            "height_matrix_export": _as_bool(
                answers.get("height_matrix_export", run_qa)
            ),
            "export_folder_path": str(
                answers.get("export_folder_path") or (output_root if run_qa else "")
            ),
            "project_store": _as_bool(answers.get("project_store", True)),
            "together_with_alignment": True,
            "stage_inputs": False,
            "execute_rt_correction": _as_bool(answers.get("execute_rt_correction", False)),
            "rt_correction_anchor_path": str(answers.get("rt_correction_anchor_path", "")),
            "rt_correction_selection_path": str(answers.get("rt_correction_selection_path", "")),
            "rt_correction_peak_selection_mode": answers.get(
                "rt_correction_peak_selection_mode", "HighestIntensity"
            ),
            "rt_correction_peak_selection_rt_weight": float(
                answers.get("rt_correction_peak_selection_rt_weight", 0.5)
            ),
            "alignment_light_mode": _as_bool(answers.get("alignment_light_mode", False)),
            "library_provenance": copy.deepcopy(
                answers.get("library_provenance", [])
            ),
        }
    )
    if answers.get("smoothing_method"):
        state["smoothing_method"] = str(answers["smoothing_method"])
    if answers.get("minimum_peak_height") is not None:
        state["minimum_peak_height"] = float(answers["minimum_peak_height"])
    acquisition_type = answers.get("acquisition_type")
    if project_type == "gcms" and not acquisition_type:
        acquisition_type = "None"
    if acquisition_type:
        for item in state["files"]:
            item["acquisition_type"] = acquisition_type
    _apply_libraries(state, loaded, answers)
    if project_type == "lcms":
        ion_mode = str(state["ion_mode"])
        state["selected_adducts"] = [
            item["adduct"]
            for item in read_adducts(
                RESOURCES / f"AdductIonResource_{ion_mode}.txt", ion_mode
            )
            if item.get("selected")
        ]
        queries = read_lipid_queries(queries_path)
        state["selected_lipids"] = [
            item
            for item in queries
            if item.get("ion_mode") == ion_mode
            and (state["target_omics"] == "Lipidomics" or state.get("lbm_path"))
        ]
    else:
        state.update(
            {
                "gcms_retention_type": answers.get("gcms_retention_type", "RT"),
                "gcms_alignment_index_type": answers.get(
                    "gcms_alignment_index_type", answers.get("gcms_retention_type", "RT")
                ),
                "gcms_ri_compound_type": answers.get("gcms_ri_compound_type", "Alkanes"),
                "gcms_ri_source": answers.get("gcms_ri_source", "single"),
                "gcms_ri_standard_path": str(answers.get("gcms_ri_standard_path", "")),
                "gcms_ri_dictionary_path": str(answers.get("gcms_ri_dictionary_path", "")),
            }
        )
    state.update(dict(answers.get("workflow_overrides") or {}))
    repository_metadata_path = str(answers.get("repository_metadata_path") or "").strip()
    if repository_metadata_path:
        from .repository_metadata import metadata_workspace_from_file

        state["repository_metadata"] = metadata_workspace_from_file(repository_metadata_path)
        state["repository_metadata_source_path"] = str(
            Path(repository_metadata_path).expanduser().resolve()
        )
    return state


def _existing_path(configured: Any, fallback: Path) -> Path:
    value = str(configured or "").strip()
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    return fallback.resolve()


def _apply_libraries(
    state: dict[str, Any], loaded: dict[str, Any], answers: dict[str, Any]
) -> None:
    strategy = answers.get("library_strategy")
    state.update({"msp_path": "", "text_db_path": "", "lbm_path": ""})
    state["msp_annotators"] = []
    state["text_annotators"] = []
    if strategy == "none":
        return
    if strategy == "official":
        catalog_id = _official_catalog_id(answers)
        item = next(entry for entry in catalog_status() if entry["id"] == catalog_id)
        path = item.get("local_path", "")
        if item["kind"] == "lbm":
            state["lbm_path"] = path
        else:
            state["msp_annotators"] = [_msp_row(path, 1)]
        state["library_provenance"] = [
            {
                "path": path,
                "version": str(item["record_id"]),
                "source": item["record_url"],
                "doi": item["doi"],
                "license": item["license"],
            }
        ]
        return
    libraries = dict(answers.get("libraries") or {})
    msp_paths = _path_list(libraries.get("msp_paths"))
    text_paths = _path_list(libraries.get("text_paths"))
    if strategy == "tiered_lipid_msp":
        lbm_item = next(entry for entry in catalog_status() if entry["id"] == "lipidomics")
        lbm_path = str(lbm_item.get("local_path", ""))
        apply_tiered_lcms_annotation(state, lbm_path, msp_paths[0])
        state["library_provenance"] = [
            {
                "path": lbm_path,
                "version": str(lbm_item["record_id"]),
                "source": lbm_item["record_url"],
                "doi": lbm_item["doi"],
                "license": lbm_item["license"],
            },
            {
                "path": str(msp_paths[0]),
                "version": str(libraries.get("msp_version", "")),
                "source": str(libraries.get("msp_source", "")),
                "doi": str(libraries.get("msp_doi", "")),
                "license": str(libraries.get("msp_license", "institutional/private")),
            },
        ]
        return
    for index, path in enumerate(msp_paths, start=1):
        state["msp_annotators"].append(_msp_row(path, index))
    for index, path in enumerate(text_paths, start=1):
        state["text_annotators"].append(
            {
                "annotator_id": f"text_annotator_{index}",
                "text_db_file_path": str(path),
                "priority": index,
                "rt_tolerance": 0.5,
                "ms1_tolerance": 0.01,
                "total_score_cutoff": 0.8,
                "use_rt_scoring": False,
                "use_rt_filtering": False,
            }
        )
    state["lbm_path"] = str(libraries.get("lbm_path", ""))
    if not state["library_provenance"]:
        state["library_provenance"] = [
            {"path": str(path), "version": "", "source": "", "doi": "", "license": ""}
            for path in [*msp_paths, *text_paths, libraries.get("lbm_path", "")]
            if str(path).strip()
        ]


def _msp_row(path: str, priority: int) -> dict[str, Any]:
    return {
        "annotator_id": f"msp_annotator_{priority}",
        "msp_file_path": str(path),
        "priority": priority,
        "rt_tolerance": 0.5,
        "use_rt_scoring": False,
        "use_rt_filtering": False,
        "weighted_dot_product_cutoff": 0.6,
        "simple_dot_product_cutoff": 0.6,
        "reverse_dot_product_cutoff": 0.8,
        "matched_peaks_percentage_cutoff": 0.1,
        "minimum_spectrum_match": 3,
    }


def _official_catalog_id(answers: dict[str, Any]) -> str:
    if str(answers.get("project_type", "")).casefold() == "gcms":
        return (
            "gcms-fiehn"
            if answers.get("gcms_ri_compound_type") == "Fames"
            else "gcms-kovats"
        )
    if answers.get("target_omics") == "Lipidomics":
        return "lipidomics"
    return (
        "metabolomics-positive"
        if answers.get("ion_mode") == "Positive"
        else "metabolomics-negative"
    )


def _can_resolve_official(answers: dict[str, Any]) -> bool:
    project_type = str(answers.get("project_type", "")).casefold()
    if project_type == "gcms":
        return True
    return project_type == "lcms" and bool(
        answers.get("ion_mode") and answers.get("target_omics")
    )


def _has_existing_library(answers: dict[str, Any]) -> bool:
    libraries = answers.get("libraries") or {}
    return bool(
        libraries.get("msp_paths")
        or libraries.get("text_paths")
        or libraries.get("lbm_path")
    )


def _has_msp_library(answers: dict[str, Any]) -> bool:
    libraries = answers.get("libraries") or {}
    return bool(_path_list(libraries.get("msp_paths")))


def _path_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [str(item) for item in (value or []) if str(item).strip()]


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


def _default_console_path() -> str:
    return str(discover_console_paths().get("selected_path", ""))
