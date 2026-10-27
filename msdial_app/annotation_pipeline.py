from __future__ import annotations

from copy import deepcopy
from typing import Any


TIERED_LCMS_PROFILE_ID = "lipid-rule-msp-high-low-v1"
# The same cascade with a fourth tier below the acquired MSP, an in-silico MSP library: GLACIER, by the author's
# decision of 2026-10-25. A profile of its own, so a record that names v1 still means three tiers.
TIERED_LCMS_IN_SILICO_PROFILE_ID = "lipid-rule-msp-high-low-insilico-v1"
IN_SILICO_ANNOTATOR_ID = "msp_in_silico"
# The tier's label, and with it the GLACIER citation and scope a Materials and Methods states, only for a file
# its name says is GLACIER (the campaign's are named so); any other in-silico library is labelled generically,
# so a run that searches another one is never cited as GLACIER (in_silico_evidence_tier).
IN_SILICO_EVIDENCE_TIER = "In-silico MSP (GLACIER)"
IN_SILICO_GENERIC_EVIDENCE_TIER = "In-silico MSP"
# The four tiers' priorities, highest first. None is 0: a priority 0 has been read as a missing one
# (int(row.get("priority") or index) and the like), and the order is what the Console ranks by after
# IsReferenceMatched, so an accepted in-silico match names a feature only where no LBM2 or acquired MSP match
# is accepted.
IN_SILICO_PROFILE_PRIORITIES = {"lbm": 4, "msp_high_quality": 3, "msp_low_quality": 2, IN_SILICO_ANNOTATOR_ID: 1}
# What a Materials and Methods says of the in-silico tier the profile searches. GLACIER is a non-public
# build its authors shared with the laboratory, so it is cited by its project and identified by file name and
# sha256, never distributed. Its records are [M+H]+ or [M-H]- only, three collision energies per compound,
# and carry no retention time.
GLACIER_CITATION = (
    "GLACIER (Coley group, ms-pred, https://github.com/coleygroup/ms-pred), a non-public build trained on "
    "NIST20 provided by the authors"
)
GLACIER_SCOPE = "[M+H]+ and [M-H]- precursors only, at collision energies 10, 20 and 40"
# What may have been done to an in-silico library before a run, by the id libraries.in_silico_msp_preprocessing
# names it with, and the clause a Materials and Methods states it in. The author's decision of 2026-10-25
# (Q6): GLACIER's negative-mode records carry one peak at the precursor m/z + 1.008, the neutral molecule's
# mass, which no fragment of [M-H]- can have; it is an artefact and is removed before use.
IN_SILICO_PREPROCESSING_STEPS = {
    "precursor_plus_1008_peak_removed": (
        "the peak at the precursor m/z + 1.008 that the negative-mode records carry, an artefact, was removed "
        "from every record before use"
    ),
}


def tiered_lcms_annotation(
    lbm_path: str,
    msp_path: str,
    lbm_annotator: dict[str, Any] | None = None,
    in_silico_path: str | None = None,
) -> dict[str, Any]:
    """Build the auditable LBM -> strict MSP -> broad MSP priority cascade.

    With ``in_silico_path`` the cascade has a fourth tier, an in-silico MSP library searched once below the
    other three (TIERED_LCMS_IN_SILICO_PROFILE_ID): its row says library_kind predicted, so the Console
    reports its matches as in-silico evidence and never as reference spectra, and suggestions false, so a
    precursor-only or failed comparison against it names nothing. The acquired rows then say library_kind
    acquired. Without one the profile is v1 as it always was.
    """
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
    if str(in_silico_path or "").strip():
        return _with_in_silico_tier(lbm, high, low, str(in_silico_path).strip())
    return {
        "profile_id": TIERED_LCMS_PROFILE_ID,
        "selection_semantics": "representative result priority cascade",
        "lbm_annotator": lbm,
        "msp_annotators": [high, low],
    }


def _with_in_silico_tier(
    lbm: dict[str, Any], high: dict[str, Any], low: dict[str, Any], in_silico_path: str
) -> dict[str, Any]:
    """The v1 tiers renumbered above an in-silico MSP tier, whose thresholds are defined once, in
    lab_lcms_metabolomics.THRESHOLDS["in_silico"] (the author's values of 2026-10-25)."""
    from .lab_lcms_metabolomics import THRESHOLDS

    lbm = {**lbm, "priority": IN_SILICO_PROFILE_PRIORITIES["lbm"]}
    acquired = [
        {**row, "priority": IN_SILICO_PROFILE_PRIORITIES[row["annotator_id"]], "library_kind": "acquired"}
        for row in (high, low)
    ]
    in_silico = {
        "msp_file_path": in_silico_path,
        # Explicit, as on every MSP row: in a Lipidomics project a row without it is scored as Lipidomics, and
        # MS-DIAL then labels its matches RuleBased before it asks whether the library is predicted.
        "target_omics": "Metabolomics",
        "library_kind": "predicted",
        "rt_tolerance": 0.5,
        "ms1_tolerance": 0.01,
        # GLACIER has no retention time.
        "use_rt_scoring": False,
        "use_rt_filtering": False,
        **THRESHOLDS["in_silico"],
        "annotator_id": IN_SILICO_ANNOTATOR_ID,
        "priority": IN_SILICO_PROFILE_PRIORITIES[IN_SILICO_ANNOTATOR_ID],
        "evidence_tier": in_silico_evidence_tier(in_silico_path),
        # No "no MS2:" or "low score:" name from a bare precursor mass against a library of hundreds of thousands
        # of predicted structures (the author's decision Q2).
        "suggestions": False,
    }
    return {
        "profile_id": TIERED_LCMS_IN_SILICO_PROFILE_ID,
        "selection_semantics": "representative result priority cascade",
        "lbm_annotator": lbm,
        "msp_annotators": [*acquired, in_silico],
    }


def in_silico_evidence_tier(path: str) -> str:
    """The in-silico tier's label: GLACIER's where the file's name says GLACIER, else the generic one.

    Nothing in an MSP says which model predicted it, so the name is what a person and the records go by; the
    plan never takes a file for GLACIER on any other ground.
    """
    name = str(path or "").replace("\\", "/").rsplit("/", 1)[-1]
    return IN_SILICO_EVIDENCE_TIER if "glacier" in name.casefold() else IN_SILICO_GENERIC_EVIDENCE_TIER


def apply_tiered_lcms_annotation(
    state: dict[str, Any],
    lbm_path: str,
    msp_path: str,
    in_silico_path: str | None = None,
) -> dict[str, Any]:
    profile = tiered_lcms_annotation(
        lbm_path,
        msp_path,
        state.get("lbm_annotator"),
        in_silico_path,
    )
    state["annotation_pipeline_profile"] = profile["profile_id"]
    state["lbm_annotator"] = profile["lbm_annotator"]
    state["lbm_path"] = profile["lbm_annotator"]["lbm_file_path"]
    state["msp_annotators"] = profile["msp_annotators"]
    return profile
