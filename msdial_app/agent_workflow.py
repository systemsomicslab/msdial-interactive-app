from __future__ import annotations

import copy
import json
import math
from bisect import bisect_left
from pathlib import Path
from typing import Any

from .annotation_pipeline import apply_tiered_lcms_annotation
from .library_catalog import catalog_status
from .user_settings import load_user_settings
from .workflow import (
    AUTOMATIC_RT_CORRECTION_DEFAULTS,
    AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW,
    FAMILY_FROM_FORMAT_DEFAULT,
    FAMILY_FROM_VENDOR_FORMAT,
    console_file_type,
    detect_raw_format,
    instrument_family_from_text,
    automatic_rt_correction_value,
    expand_paths_report,
    discover_console_paths,
    load_parameter_template,
    read_adducts,
    read_analysis_csv,
    read_lipid_queries,
    validate_workflow,
)
from .worksets import describe_workset_candidate, get_workset


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
    "execute_automatic_rt_correction", "automatic_rt_correction_reference_file_id",
    "automatic_rt_correction_rt_bin_width", "automatic_rt_correction_match_rt_tolerance",
    "automatic_rt_correction_minimum_anchors", "automatic_rt_correction_maximum_anchors",
    "automatic_rt_correction_minimum_sample_coverage",
    "automatic_rt_correction_intensity_quantile",
    "automatic_rt_correction_maximum_peak_width_quantile",
    "automatic_rt_correction_minimum_signal_to_noise",
    "automatic_rt_correction_minimum_gaussian_similarity",
    "automatic_rt_correction_minimum_ideal_slope",
    "automatic_rt_correction_outlier_mad_threshold",
    "automatic_rt_correction_reference_centrality_weight",
    "automatic_rt_correction_interpolate_blanks_by_analytical_order",
    # MsdialWorkbench#826; written to the method file only when given (Console default 1.5 min).
    "automatic_rt_correction_local_support_rt_window",
    "library_provenance", "run_qa", "internal_standards",
    "use_retention_time_for_annotation", "retention_time_tolerance", "number_of_threads",
    "stage_inputs", "dilution_factor", "class_assignment_confirmed",
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
        "sample_table_proposal": _describe_sample_table(files),
    }


def _describe_sample_table(files: list[dict[str, Any]]) -> dict[str, Any]:
    """How the injection order was arrived at, and what the dilution factor is set to.

    Both are silently consequential: the order is what every drift plot downstream is
    drawn against, and the factor scales every concentration. Neither said where it came
    from, so a value that happened to be right was indistinguishable from one that had
    never been considered.
    """
    from .sample_grouping import propose_injection_order

    names = [str(item.get("file_name", "")) for item in files]
    order = propose_injection_order(names) if names else {"reason": "no files", "alternatives": []}
    factors = sorted({float(item.get("factor", 1) or 1) for item in files})
    return {
        "analytical_order": {
            "derived_from": order.get("chosen", "listing"),
            "reason": order.get("reason", ""),
            "agrees_with_file_listing": order.get("agrees_with_listing"),
            "alternatives": [item.get("label", "") for item in order.get("alternatives", [])],
        },
        "dilution_factor": {
            "values": factors,
            "assumed": factors == [1.0],
            "note": (
                "Every file is set to 1, which is the default rather than a value read "
                "from anywhere. A wrong factor scales every concentration."
                if factors == [1.0]
                else "Dilution factors differ between files; confirm they are right."
            ),
        },
        "confirmation_required": True,
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


def _suggested_workset_name(answers: dict[str, Any], inspection: dict[str, Any]) -> str:
    """A name a laboratory would recognise, built from what makes this method distinct."""
    parts = [
        str(answers.get("project_type", "")).upper().replace("LCMS", "LC-MS").replace("GCMS", "GC-MS"),
        str(answers.get("ion_mode", "")),
        str(answers.get("target_omics", "")),
    ]
    library = (answers.get("libraries") or {})
    if isinstance(library, dict) and library.get("lbm_path"):
        parts.append(Path(str(library["lbm_path"])).stem[:24])
    elif inspection.get("vendors"):
        parts.append(str(inspection["vendors"][0]))
    return " ".join(part for part in parts if part).strip()


def build_guided_plan(
    input_path: str,
    answers: dict[str, Any] | None = None,
    workset_id: str = "",
) -> dict[str, Any]:
    supplied = dict(answers or {})
    workset = get_workset(workset_id)
    merged = dict((workset or {}).get("answers", {}))
    # A workset stores answers and workflow_overrides side by side, and only the answers
    # were ever read back: overrides saved into a workset were accepted, written to disk,
    # and then silently ignored by every plan built from it.
    overrides = {
        **dict((workset or {}).get("workflow_overrides", {}) or {}),
        **dict(merged.get("workflow_overrides") or {}),
        **dict(supplied.get("workflow_overrides") or {}),
    }
    merged.update(supplied)
    if overrides:
        merged["workflow_overrides"] = overrides
    unknown_answer_keys = sorted(set(merged) - SUPPORTED_ANSWER_KEYS)
    inspection = inspect_analysis_input(input_path)
    manifest_override = str((merged.get("workflow_overrides") or {}).get("repository_run_manifest") or "")
    if manifest_override:
        inspection["sample_table_proposal"] = adopted_order_proposal(
            manifest_override, inspection.get("files", []), inspection.get("sample_table_proposal")
        )
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

    # An unanswered advisory question leaves a conservative default in place, so the
    # workflow can still be built and shown; only a required one makes it unknowable.
    pending_required = [item for item in questions if item.get("required", True)]
    workflow = _workflow(inspection, merged) if not pending_required else None
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
        # The first question still to be answered, required or not, so an agent works
        # through them all; readiness below turns only on the required ones.
        "next_question": questions[0] if questions else None,
        "remaining_questions": questions,
        "advisory_questions": [item for item in questions if not item.get("required", True)],
        "workflow": workflow,
        # The second dataset should only have to confirm what changed, which requires
        # that the first one's settings be saved. Nothing here saves them; it says what
        # a workset would hold and what it would deliberately not carry, so the offer
        # can be made with the trade-off visible.
        "workset_suggestion": describe_workset_candidate(
            merged,
            source=workset,
            suggested_name=_suggested_workset_name(merged, inspection),
        ),
        "validation": validation,
        "warnings": [
            *inspection.get("warnings", []),
            *[f"Unknown answers key was not applied: {key}" for key in unknown_answer_keys],
        ],
        "unknown_answer_keys": unknown_answer_keys,
        "blockers": list(dict.fromkeys(blockers)),
        "official_library": official_library,
        "ready_to_prepare": not pending_required and not blockers,
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


# The finest steps of the user's rule of 2026-10-06. Absolute floors: the search never goes finer.
FINE_THRESHOLD_STEP_QTOF = 10
FINE_THRESHOLD_STEP_FT = 100
# Within the target range, the highest threshold whose estimated count is still at least the lower bound:
# the lower end of 3,000-6,000 (the user's decision of 2026-10-06), for MS/MS of higher quality, since gap
# filling recovers the peaks a threshold leaves out of a file.
SELECTION_RULE = "highest_threshold_keeping_at_least_minimum"


def is_fourier_transform_family(instrument_family: str) -> bool:
    """True for the labels Interactive gives Orbitrap and FT-ICR data ("Fourier-transform MS", "FT-ICR")."""
    family = str(instrument_family or "").casefold()
    return "fourier" in family or "ft-icr" in family or "fticr" in family


# The instrument-family steps of the project contract: the coarse step the range search always starts from.
FAMILY_THRESHOLD_STEP_QTOF = 100
FAMILY_THRESHOLD_STEP_FT = 1000


def family_threshold_step(instrument_family: str) -> int:
    """The coarse step: 1,000 for Fourier-transform data, 100 for every other family (QTOF-type, GC-MS, Unknown)."""
    return FAMILY_THRESHOLD_STEP_FT if is_fourier_transform_family(instrument_family) else FAMILY_THRESHOLD_STEP_QTOF


def fine_threshold_step(coarse_step: int, instrument_family: str = "") -> int:
    """The finest step the range search uses: 10 for QTOF-type data, 100 for Fourier-transform data.

    The user's decision of 2026-10-06. An absolute floor, not a tenth of whatever step a caller passes: a
    caller echoing a fallback's step of 10 back must not get a search in steps of 1, which on the Waters
    MSe demo lands at a threshold of 2, where the median S/N is 2.6. The family decides the floor; without
    one, a step of 1,000 or more is the Fourier-transform family's step. A coarse step at or below the
    floor is its own fine step, so it has no fallback.
    """
    step = max(1, int(coarse_step))
    return min(step, _step_floor(step, instrument_family))


def _step_floor(step: int, instrument_family: str) -> int:
    if is_fourier_transform_family(instrument_family) or int(step) >= 1000:
        return FINE_THRESHOLD_STEP_FT
    return FINE_THRESHOLD_STEP_QTOF


def _count_at_or_above(values: list[float], threshold: float) -> int:
    return len(values) - bisect_left(values, threshold)


def _stepped_threshold(
    values: list[float], step: int, minimum: int, maximum: int
) -> tuple[int, int]:
    """The highest multiple of step whose count is still at least minimum, when its count is in range.

    Counts fall as the threshold rises, so that multiple keeps the fewest peaks of all those keeping at
    least minimum: when its count is above maximum, no multiple of step lands in range, and the result is
    whichever of it and the next multiple is nearer the range (the one keeping more peaks on a tie).
    values are sorted and number more than maximum. Returns (threshold, count at or above it).
    """
    anchor = values[len(values) - minimum]  # the minimum-th largest height
    threshold = int(math.floor(anchor / step)) * step
    while threshold > 0 and _count_at_or_above(values, threshold) < minimum:
        threshold -= step  # a quotient floating point rounded up
    count = _count_at_or_above(values, threshold)
    if count <= maximum:
        return threshold, count
    above = threshold + step
    above_count = _count_at_or_above(values, above)
    if minimum - above_count < count - maximum:
        return above, above_count
    return threshold, count


def estimate_peak_height_range(
    heights: list[float],
    minimum_peak_count: int = 3000,
    maximum_peak_count: int = 6000,
    threshold_step: int = 100,
    instrument_family: str = "",
) -> dict[str, Any]:
    """A Minimum peak height on the instrument-family step that keeps minimum-maximum peaks.

    The user's rules of 2026-10-06:
    - 0 when the zero-threshold count is at most the upper bound;
    - otherwise, of the multiples of the family step (100 for QTOF-type, 1,000 for FT), the HIGHEST whose
      estimated count is still at least the lower bound - the lower end of the range;
    - only when no multiple of the family step lands in range, the same choice in the fine step (10 for
      QTOF-type, 100 for FT: fine_threshold_step), and never finer;
    - when even the fine step misses, the candidate nearest the range, marked out of range with a
      warning.

    The family step is ALWAYS the coarse step. instrument_family decides it (family_threshold_step), and
    threshold_step is only a request: one below the fine step is raised to it, and any other that is not
    the family step (one between the fine step and the family step, a fallback's step echoed back, or a
    step of 15) is recorded as requested_threshold_step, with a warning, and never replaces the family
    step. Until 0.5.28's review, a requested step of 100 on FT data was searched as the coarse step, so
    MTBLS2207's DDA unit got 19,700 where the rule gives 19,000, recorded as coarse step 100 with no
    fallback. Only without a family does the request name one: 1,000 or more is the FT family's step,
    anything else the QTOF-type family's. A threshold_step of 0 is no request.

    threshold_step in the result is the step actually used, coarse_threshold_step the family step,
    step_fallback and fallback_reason ("no_coarse_step_in_range" or None) say whether and why the fine
    step was used. coarse_minimum_peak_height and coarse_estimated_peak_count keep what the family step
    alone chose, so a fallback can be read against it. requested_step_disposition says what became of a
    request: None (none made), "family_step", "fine_step" (the fine step, used only on a fallback),
    "raised_to_fine_step" (below it) or "recorded_only".
    """
    values = sorted(float(value) for value in heights if float(value) >= 0)
    minimum = max(1, int(minimum_peak_count))
    maximum = max(minimum, int(maximum_peak_count))
    requested_value = int(threshold_step or 0)
    requested_step = requested_value if requested_value > 0 else None
    if str(instrument_family or "").strip():
        step = family_threshold_step(instrument_family)
    else:
        step = FAMILY_THRESHOLD_STEP_FT if (requested_step or 0) >= 1000 else FAMILY_THRESHOLD_STEP_QTOF
    fine_step = fine_threshold_step(step, instrument_family)
    if requested_step is None:
        disposition = None
    elif requested_step == step:
        disposition = "family_step"
    elif requested_step == fine_step:
        disposition = "fine_step"
    elif requested_step < fine_step:
        disposition = "raised_to_fine_step"
    else:
        disposition = "recorded_only"
    request_warnings: list[str] = []
    if requested_step is not None and requested_step != step:
        request_warnings.append(
            f"A threshold_step of {requested_step} was requested. The instrument-family step "
            f"({step}{', ' + str(instrument_family) if str(instrument_family or '').strip() else ''}) is always "
            f"the coarse step and {fine_step} the finest step, used only when no multiple of {step} lands in "
            "range"
            + (f"; a request below {fine_step} is raised to it" if requested_step < fine_step else "")
            + ". The request is recorded as requested_threshold_step and was not searched."
        )
    if not values:
        raise ValueError("The diagnostic result contains no peak heights.")
    common = {
        "target_peak_count_min": minimum,
        "target_peak_count_max": maximum,
        "diagnostic_peak_count": len(values),
        "requested_threshold_step": requested_step,
        "requested_step_disposition": disposition,
        "coarse_threshold_step": step,
        "fine_threshold_step": fine_step,
        "instrument_family": str(instrument_family or ""),
        "selection_rule": SELECTION_RULE,
        "method": "quantized height-range search",
    }

    if len(values) <= maximum:
        return {
            "minimum_peak_height": 0,
            "estimated_peak_count": len(values),
            "threshold_step": step,
            **common,
            "step_fallback": False,
            "fallback_reason": None,
            "coarse_minimum_peak_height": 0,
            "coarse_estimated_peak_count": len(values),
            "within_target_range": minimum <= len(values) <= maximum,
            "warnings": list(request_warnings),
            "note": (
                "The zero-threshold diagnostic did not exceed the upper peak-count bound; "
                "the threshold is kept at zero."
            ),
        }

    coarse_threshold, coarse_detected = _stepped_threshold(values, step, minimum, maximum)
    threshold, detected, used_step = coarse_threshold, coarse_detected, step
    fallback = not (minimum <= coarse_detected <= maximum) and fine_step < step
    if fallback:
        threshold, detected = _stepped_threshold(values, fine_step, minimum, maximum)
        used_step = fine_step
    within = minimum <= detected <= maximum
    warnings: list[str] = list(request_warnings)
    if not within:
        warnings.append(
            f"No Minimum peak height in steps of {used_step} gives {minimum}-{maximum} peaks: the "
            f"nearest, {threshold}, keeps {detected} of the {len(values)} found at zero threshold. "
            + (
                f"The family step {step} was tried first and a step of {fine_step} is the finest the "
                "search uses. "
                if fallback
                else ""
            )
            + "The threshold is out of the target range; review it before a production run."
        )
    if fallback:
        note = (
            f"No multiple of the instrument-family step {step} gave {minimum}-{maximum} peaks "
            f"(nearest {coarse_threshold}, {coarse_detected} peaks), so the search fell back to "
            f"steps of {fine_step}, the finest it uses."
        )
    else:
        note = (
            "The threshold is the highest multiple of the instrument-family step that still keeps "
            f"at least {minimum} peaks. Review the diagnostic count when no stepped threshold can "
            "enter the requested range."
        )
    return {
        "minimum_peak_height": threshold,
        "estimated_peak_count": detected,
        "threshold_step": used_step,
        **common,
        "step_fallback": fallback,
        "fallback_reason": "no_coarse_step_in_range" if fallback else None,
        "coarse_minimum_peak_height": coarse_threshold,
        "coarse_estimated_peak_count": coarse_detected,
        "within_target_range": within,
        "warnings": warnings,
        "note": note,
    }


def representative_instrument_family(
    selected: dict[str, Any], declared_instrument: str = ""
) -> dict[str, Any]:
    """The instrument family the diagnostic's threshold step is chosen by, and what it rests on.

    The file itself first: its format is read again when it is on disk, so a row made before an mzML's
    header was read (every mzML was QTOF before 0.5.28) cannot keep a Q Exactive on steps of 100. Only when
    the file's family is a format default - an mzML whose header names no instrument, a Bruker or
    unrecognised .d - does a repository's declared instrument (the Catalog handoff's technical_settings.
    instrument) decide, and only to name a Fourier-transform family. A vendor format or an mzML header
    that names a TOF is evidence about the file, and a declaration does not overrule it.
    """
    row = dict(selected or {})
    path_text = str(row.get("file_path") or "").strip()
    if path_text and Path(path_text).exists():
        row.update(detect_raw_format(path_text))
    family = str(row.get("instrument_family") or "Unknown")
    source = str(row.get("instrument_family_source") or "file_row")
    result: dict[str, Any] = {"instrument_family": family, "instrument_family_source": source}
    if row.get("instrument_evidence"):
        result["instrument_evidence"] = str(row["instrument_evidence"])
    declared = str(declared_instrument or "").strip()
    if declared:
        result["declared_instrument"] = declared
        named = instrument_family_from_text([declared])
        if named is not None and source == FAMILY_FROM_FORMAT_DEFAULT and not is_fourier_transform_family(family):
            result.update(
                instrument_family=named[0],
                instrument_family_source="repository_declared_instrument",
                instrument_evidence=named[1],
            )
    return result


# The suffixes whose instrument family a vendor format fixed before 0.5.28 (Thermo .raw FT; Waters .raw, SCIEX
# and Shimadzu QTOF or GC-MS). Every other family then was a format default: an mzML was QTOF unread, and a .d
# was QTOF whatever its vendor.
_LEGACY_VENDOR_SUFFIXES = {".raw", ".wiff", ".wiff2", ".lcd", ".qgd"}


def _legacy_family_source(profile: dict[str, Any]) -> str:
    """What a profile recorded before 0.5.28, which kept no instrument_family_source, rested on."""
    if is_fourier_transform_family(str(profile.get("instrument_family") or "")):
        return FAMILY_FROM_VENDOR_FORMAT
    suffix = Path(str(profile.get("file_path") or "")).suffix.lower()
    return FAMILY_FROM_VENDOR_FORMAT if suffix in _LEGACY_VENDOR_SUFFIXES else FAMILY_FROM_FORMAT_DEFAULT


def current_peak_tuning_profile(
    profile: dict[str, Any], declared_instrument: str = ""
) -> dict[str, Any]:
    """A stored diagnostic's representative, its instrument family worked out again by this version.

    The estimate reads the family and the family step from the diagnostic's peak_tuning_profile, which the
    version that STARTED the diagnostic wrote. Before 0.5.28 every mzML was QTOF, so MTBLS2207's Orbitrap
    ID-X diagnostic, re-estimated from its manifest, kept QTOF and steps of 100 (19,700 where the rule gives
    19,000 in steps of 1,000), and the new peak_height_diagnostics entry said QTOF. The family is read again
    the way a new diagnostic reads it (representative_instrument_family): the representative file's own
    format and mzML header when it is on disk, else the stored family, which for a pre-0.5.28 profile with
    no recorded source is a format default unless a vendor format fixed it (_legacy_family_source), so a
    repository's declared instrument can still name an FT family once the raw data are deleted.

    threshold_step becomes the re-derived family's step, and instrument_family_source says what the family
    now rests on. When the family or the step changes, instrument_family_rederived is true and
    stored_peak_tuning_profile keeps what was stored, so the record shows both. The stored
    diagnostic-job.json is not rewritten: it is the record of what the diagnostic was started with.
    """
    stored = dict(profile or {})
    row: dict[str, Any] = {
        "file_path": stored.get("file_path") or "",
        "instrument_family": stored.get("instrument_family") or "Unknown",
        "instrument_family_source": stored.get("instrument_family_source") or _legacy_family_source(stored),
    }
    if stored.get("instrument_evidence"):
        row["instrument_evidence"] = stored["instrument_evidence"]
    family = representative_instrument_family(row, declared_instrument)
    current = {
        key: value for key, value in stored.items()
        if key not in {"instrument_evidence", "declared_instrument", "stored_peak_tuning_profile",
                       "instrument_family_rederived"}
    }
    current.update(family)
    current["threshold_step"] = family_threshold_step(family["instrument_family"])
    changed = (str(stored.get("instrument_family") or ""), int(stored.get("threshold_step") or 0)) != (
        current["instrument_family"], current["threshold_step"]
    )
    current["instrument_family_rederived"] = changed
    if changed:
        current["stored_peak_tuning_profile"] = {
            "instrument_family": stored.get("instrument_family"),
            "instrument_family_source": stored.get("instrument_family_source"),
            "threshold_step": stored.get("threshold_step"),
        }
    return current


def select_peak_tuning_representative(
    files: list[dict[str, Any]], requested_file: str = "", declared_instrument: str = ""
) -> dict[str, Any]:
    """The diagnostic's representative file, its instrument family and the family's threshold step.

    declared_instrument is a repository unit's declared instrument text, read only when the file's own
    format leaves the family at a default (representative_instrument_family).
    """
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
        # Without a QC, the nearest Sample: a Standard is a chemical mix, not the matrix the
        # threshold is for. Other non-Blank files only when there is no Sample.
        samples = [
            item for item in files
            if console_file_type(item.get("file_type") or "Sample") == "Sample"
            and not _is_blank_file(item)
        ]
        candidates = qc or samples or [item for item in files if not _is_blank_file(item)] or list(files)
        orders = [float(item.get("analytical_order") or 0) for item in files]
        midpoint = (min(orders) + max(orders)) / 2 if orders else 0
        selected = min(
            candidates,
            key=lambda item: (
                abs(float(item.get("analytical_order") or 0) - midpoint),
                str(item.get("file_path") or "").casefold(),
            ),
        )
        reason = (
            "QC-nearest-run-midpoint" if qc
            else "sample-nearest-run-midpoint" if samples
            else "non-blank-nearest-run-midpoint"
        )
    family = representative_instrument_family(selected, declared_instrument)
    threshold_step = family_threshold_step(family["instrument_family"])
    return {
        "file": selected,
        "file_path": str(selected.get("file_path") or ""),
        "file_name": str(selected.get("file_name") or ""),
        "selection_reason": reason,
        **family,
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
    return console_file_type(values[0]) == "QC" or str(values[1] or "").strip().casefold() == "qc" or any(
        token == "qc"
        for token in str(values[2] or "").replace("-", "_").casefold().split("_")
    )


def _is_blank_file(item: dict[str, Any]) -> bool:
    return console_file_type(item.get("file_type")) == "Blank" or (
        str(item.get("class_id") or "").strip().casefold() == "blank"
    )


def _questions(answers: dict[str, Any]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []

    def ask(
        identifier: str,
        prompt: str,
        choices: list[str] | None = None,
        required: bool = True,
    ) -> None:
        # A question is required when proceeding without it would mean guessing. Where a
        # conservative default exists and is the honest one, the question is still put --
        # it is the analyst's to answer -- but it does not hold the plan hostage.
        result.append(
            {
                "id": identifier,
                "prompt": prompt,
                "choices": choices or [],
                "required": required,
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
        elif "execute_automatic_rt_correction" not in answers:
            ask(
                "execute_automatic_rt_correction",
                (
                    "Apply automatic RT correction only during alignment, using anchors "
                    "learned from the detected features?"
                ),
                ["false", "true"],
                required=False,
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
    # Once a library is settled, how its retention times should be used is the next
    # decision, and it belongs to the analyst: a library built for this chromatography
    # is normally scored and filtered on retention time, one built elsewhere must not be,
    # and no property of the file says which this is.
    strategy = str(answers.get("library_strategy") or "")
    library_chosen = strategy in {"official", "existing", "tiered_lipid_msp"} and (
        strategy != "existing" or _has_existing_library(answers)
    )
    if library_chosen and "use_retention_time_for_annotation" not in answers:
        ask(
            "use_retention_time_for_annotation",
            "Does this library carry retention times for this chromatography, so that "
            "annotation should be scored and filtered on them?",
            ["true", "false"],
            required=False,
        )
    if (
        library_chosen
        and _as_bool(answers.get("use_retention_time_for_annotation"))
        and answers.get("retention_time_tolerance") is None
    ):
        ask(
            "retention_time_tolerance",
            "Within how many minutes of the library retention time should a match be accepted?",
            required=False,
        )
    if "dilution_factor" not in answers:
        ask(
            "dilution_factor",
            "What dilution factor applies to these samples? It scales every concentration, "
            "and 1 is a default rather than a value read from the data.",
            required=False,
        )
    if "stage_inputs" not in answers:
        ask(
            "stage_inputs",
            "MS-DIAL writes its per-file intermediates beside the files it reads, so "
            "running against the data where it sits will add .dcl, .pai2 and tag files "
            "to that folder. Copy the raw data into the output folder first?",
            ["true", "false"],
            required=False,
        )
    if "number_of_threads" not in answers:
        ask(
            "number_of_threads",
            "How many threads should MS-DIAL use on this machine?",
            required=False,
        )
    # The one question worth blocking on. Every downstream comparison is drawn along
    # this axis, correcting it afterwards means re-running, and answering it costs a
    # word. A repository reanalysis has its own gate and no analyst to ask, so it is
    # raised only where the classes came from reading file names.
    if (
        not answers.get("repository_metadata_path")
        and "class_assignment_confirmed" not in answers
    ):
        ask(
            "class_assignment_confirmed",
            "The Class assignment was read from the file names. Confirm it is the "
            "comparison this experiment is about, or give the assignment to use.",
            ["true", "false"],
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
            "sample_table_proposal": copy.deepcopy(
                inspection.get("sample_table_proposal", {})
            ),
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
            "execute_automatic_rt_correction": _as_bool(
                answers.get("execute_automatic_rt_correction", False)
            ),
            # One table of defaults for every entry point; a fractional anchor count is kept
            # as given so validation refuses it instead of int() truncating it.
            **{
                key: automatic_rt_correction_value(
                    key, answers.get(key, state.get(key, default))
                )
                for key, default in AUTOMATIC_RT_CORRECTION_DEFAULTS.items()
            },
            "alignment_light_mode": _as_bool(answers.get("alignment_light_mode", False)),
            "library_provenance": copy.deepcopy(
                answers.get("library_provenance", [])
            ),
        }
    )
    # Unlike the keys above it has no default here: unset, it is not written, and a Console with
    # MsdialWorkbench#826 uses its own 1.5 min while one without it is never handed a key it does
    # not know. A template line for it reached the state through load_parameter_template.
    if AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW in answers:
        window = answers[AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW]
        if window is None or (isinstance(window, str) and not window.strip()):
            state.pop(AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW, None)
        else:
            state[AUTOMATIC_RT_CORRECTION_LOCAL_SUPPORT_RT_WINDOW] = window
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
    _apply_retention_time_use(state, answers)
    if answers.get("number_of_threads") is not None:
        state["number_of_threads"] = max(1, int(answers["number_of_threads"]))
    if answers.get("stage_inputs") is not None:
        state["stage_inputs"] = _as_bool(answers.get("stage_inputs"))
    if answers.get("dilution_factor") is not None:
        factor = float(answers["dilution_factor"])
        if factor > 0:
            for item in state["files"]:
                item["factor"] = factor
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
    overrides = dict(answers.get("workflow_overrides") or {})
    state.update(overrides)
    if str(state.get("repository_run_manifest") or "").strip():
        # A repository unit's MS1 and MS2 data type are what its inputs deliver to MS-DIAL where they all
        # agree (the stored representation for a reader that passes it on, Centroid for one that centroids),
        # not the template's; the decision and its basis are kept as data_type_provenance.
        from .repository_reanalysis import DATA_TYPE_KEYS, apply_delivered_data_types

        apply_delivered_data_types(state, explicit=[key for key in DATA_TYPE_KEYS if key in overrides])
    _adopt_recorded_analytical_order(state)
    repository_metadata_path = str(answers.get("repository_metadata_path") or "").strip()
    if repository_metadata_path:
        from .repository_metadata import metadata_workspace_from_file

        state["repository_metadata"] = metadata_workspace_from_file(repository_metadata_path)
        state["repository_metadata_source_path"] = str(
            Path(repository_metadata_path).expanduser().resolve()
        )
    return state


def adopted_order_proposal(
    manifest_path: str, files: list[dict[str, Any]], proposal: dict[str, Any] | None
) -> dict[str, Any]:
    """The sample-table proposal, saying where the analytical order came from.

    The proposal is read from the file names, so for an analysis CSV whose order was ranked
    from the raw headers it said "listing". The manifest's record is adopted only while the
    CSV still carries exactly the recorded order: a CSV re-saved without it, or a record from
    another unit reached through a workset, would otherwise label a guessed order as measured
    and silence the Blank-interpolation warning. A record that does not match is attached as a
    note, and the name-derived source stands.
    """
    adopted = dict(proposal or {})
    if not str(manifest_path or "").strip():
        return adopted
    from .repository_reanalysis import read_manifest, recorded_order_match

    try:
        manifest = read_manifest(manifest_path)
    except (OSError, ValueError):
        return adopted
    record = manifest.get("analytical_order")
    if not isinstance(record, dict):
        return adopted
    if not record.get("derived_from"):
        adopted["recorded_header_order"] = {
            "derived_from": None,
            "reason": str(record.get("reason") or ""),
        }
        return adopted
    same_unit, matches = recorded_order_match(manifest, record, files)
    if not matches:
        adopted["recorded_header_order"] = {
            "derived_from": record["derived_from"],
            "matches_analysis_csv": False,
            "reason": (
                "The analysis CSV does not carry the order the unit manifest records."
                if same_unit
                else "The unit manifest describes other input files than these."
            ),
        }
        # An inherited adoption (the inspection's, before workflow overrides replaced the
        # files) must not survive a mismatch: fall back to the order read from the names.
        if (adopted.get("analytical_order") or {}).get("derived_from") == record["derived_from"]:
            adopted["analytical_order"] = _describe_sample_table(files)["analytical_order"]
        return adopted
    adopted["analytical_order"] = {
        "derived_from": record["derived_from"],
        "reason": str(record.get("reason") or ""),
        "agrees_with_file_listing": record.get("agrees_with_listing"),
        "matches_analysis_csv": True,
        "alternatives": ["file listing order"],
    }
    return adopted


def _adopt_recorded_analytical_order(state: dict[str, Any]) -> None:
    manifest_path = str(state.get("repository_run_manifest") or "").strip()
    if manifest_path:
        state["sample_table_proposal"] = adopted_order_proposal(
            manifest_path, state.get("files", []), state.get("sample_table_proposal")
        )


def _existing_path(configured: Any, fallback: Path) -> Path:
    value = str(configured or "").strip()
    if value:
        candidate = Path(value).expanduser()
        if candidate.is_file():
            return candidate.resolve()
    return fallback.resolve()


def _apply_retention_time_use(state: dict[str, Any], answers: dict[str, Any]) -> None:
    """Apply the analyst's decision about retention time to every library in use.

    Whether retention time helps or hurts annotation is a property of the library and
    the chromatography it was built for, not of the file format, so it is asked once
    and then applies to whichever library kinds this run actually uses.
    """
    if answers.get("use_retention_time_for_annotation") is None:
        return
    use_rt = _as_bool(answers.get("use_retention_time_for_annotation"))
    for kind in ("lbm", "msp", "text"):
        state[f"{kind}_use_rt_scoring"] = use_rt
        state[f"{kind}_use_rt_filtering"] = use_rt
    tolerance = answers.get("retention_time_tolerance")
    if not use_rt or tolerance is None:
        return
    value = float(tolerance)
    if value <= 0:
        return
    for kind in ("lbm", "msp", "text"):
        state[f"{kind}_rt_tolerance"] = value


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
