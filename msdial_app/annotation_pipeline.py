from __future__ import annotations

from copy import deepcopy
from typing import Any


TIERED_LCMS_PROFILE_ID = "lipid-rule-msp-high-low-v1"


def tiered_lcms_annotation(
    lbm_path: str,
    msp_path: str,
    lbm_annotator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the auditable LBM -> strict MSP -> broad MSP priority cascade."""
    lbm = deepcopy(lbm_annotator or {})
    lbm.update(
        {
            "lbm_file_path": str(lbm_path).strip(),
            "priority": 3,
        }
    )
    common = {
        "msp_file_path": str(msp_path).strip(),
        "target_omics": "Metabolomics",
        "rt_tolerance": 0.5,
        "ms1_tolerance": 0.01,
        "use_rt_scoring": False,
        "use_rt_filtering": False,
        "matched_peaks_percentage_cutoff": 0.0,
    }
    high = {
        **common,
        "annotator_id": "msp_high_quality",
        "priority": 2,
        "ms2_tolerance": 0.05,
        "weighted_dot_product_cutoff": 0.6,
        "simple_dot_product_cutoff": 0.6,
        "reverse_dot_product_cutoff": 0.8,
        "minimum_spectrum_match": 3,
        "evidence_tier": "MSP high quality",
    }
    low = {
        **common,
        "annotator_id": "msp_low_quality",
        "priority": 1,
        "ms2_tolerance": 0.25,
        "weighted_dot_product_cutoff": 0.5,
        "simple_dot_product_cutoff": 0.5,
        "reverse_dot_product_cutoff": 0.5,
        "minimum_spectrum_match": 1,
        "evidence_tier": "MSP low quality candidate",
    }
    return {
        "profile_id": TIERED_LCMS_PROFILE_ID,
        "selection_semantics": "representative result priority cascade",
        "lbm_annotator": lbm,
        "msp_annotators": [high, low],
    }


def apply_tiered_lcms_annotation(
    state: dict[str, Any],
    lbm_path: str,
    msp_path: str,
) -> dict[str, Any]:
    profile = tiered_lcms_annotation(
        lbm_path,
        msp_path,
        state.get("lbm_annotator"),
    )
    state["annotation_pipeline_profile"] = profile["profile_id"]
    state["lbm_annotator"] = profile["lbm_annotator"]
    state["lbm_path"] = profile["lbm_annotator"]["lbm_file_path"]
    state["msp_annotators"] = profile["msp_annotators"]
    return profile
