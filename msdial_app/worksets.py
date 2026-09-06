from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .user_settings import user_data_directory


WORKSET_SCHEMA = "msdial-interactive.workset.v1"

# Answers that belong to one dataset, or to one person's agreement about one dataset.
# Reusing them is not merely unhelpful: a confirmation carried forward is a
# confirmation nobody gave, and a threshold measured on other files is a threshold
# nobody measured here. Each entry says why, because the reasons are reported to the
# person rather than applied in silence.
DATASET_SCOPED_ANSWERS: dict[str, str] = {
    "input_path": "names this dataset",
    "output_root": "names where this run wrote",
    "export_folder_path": "names where this run wrote",
    "project_store": "names where this run wrote",
    "confirmed": "approves this run and nothing after it",
    "class_assignment_confirmed": (
        "is one person's agreement about these file names; the next dataset has its own"
    ),
    "dilution_factor": (
        "belongs to how this batch was prepared, and a wrong value scales every concentration"
    ),
    "repository_metadata_path": "names this accession's metadata",
    "rt_correction_anchor_path": "names this batch's retention-time anchors",
    "rt_correction_selection_path": "names this batch's retention-time anchors",
}


_BUILTIN_WORKSETS: tuple[dict[str, Any], ...] = (
    {
        "id": "gcms-metabolomics",
        "name": "GC-MS metabolomics",
        "description": "GC-MS metabolomics with the bundled GC-MS parameter template.",
        "builtin": True,
        "answers": {
            "project_type": "gcms",
            "target_omics": "Metabolomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "ask",
            "run_qa": False,
            "generate_materials_methods": True,
        },
    },
    {
        "id": "lcms-positive-metabolomics",
        "name": "LC-MS positive metabolomics",
        "description": "Positive-mode LC-MS metabolomics.",
        "builtin": True,
        "answers": {
            "project_type": "lcms",
            "ion_mode": "Positive",
            "target_omics": "Metabolomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "ask",
            "run_qa": True,
            "generate_materials_methods": True,
        },
    },
    {
        "id": "lcms-negative-metabolomics",
        "name": "LC-MS negative metabolomics",
        "description": "Negative-mode LC-MS metabolomics.",
        "builtin": True,
        "answers": {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Metabolomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "ask",
            "run_qa": True,
            "generate_materials_methods": True,
        },
    },
    {
        "id": "lcms-positive-lipidomics",
        "name": "LC-MS positive lipidomics",
        "description": "Positive-mode LC-MS lipidomics.",
        "builtin": True,
        "answers": {
            "project_type": "lcms",
            "ion_mode": "Positive",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "ask",
            "run_qa": True,
            "generate_materials_methods": True,
        },
    },
    {
        "id": "lcms-negative-lipidomics",
        "name": "LC-MS negative lipidomics",
        "description": "Negative-mode LC-MS lipidomics.",
        "builtin": True,
        "answers": {
            "project_type": "lcms",
            "ion_mode": "Negative",
            "target_omics": "Lipidomics",
            "parameter_strategy": "default",
            "execute_rt_correction": False,
            "library_strategy": "ask",
            "run_qa": True,
            "generate_materials_methods": True,
        },
    },
)


def workset_directory() -> Path:
    return user_data_directory() / "worksets"


def list_worksets() -> list[dict[str, Any]]:
    result = [dict(item) for item in _BUILTIN_WORKSETS]
    directory = workset_directory()
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                item = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(item, dict) and item.get("schema") == WORKSET_SCHEMA:
                item["builtin"] = False
                item["file"] = str(path)
                result.append(item)
    return result


def get_workset(workset_id: str) -> dict[str, Any] | None:
    normalized = str(workset_id).strip().casefold()
    if not normalized:
        return None
    return next(
        (item for item in list_worksets() if str(item.get("id", "")).casefold() == normalized),
        None,
    )


def save_workset(
    name: str,
    answers: dict[str, Any],
    *,
    description: str = "",
    workflow_overrides: dict[str, Any] | None = None,
) -> dict[str, Any]:
    label = str(name).strip()
    if not label:
        raise ValueError("Set a workset name.")
    identifier = _slug(label)
    if any(item["id"] == identifier for item in _BUILTIN_WORKSETS):
        identifier += "-custom"
    item = {
        "schema": WORKSET_SCHEMA,
        "id": identifier,
        "name": label,
        "description": str(description).strip(),
        "builtin": False,
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "answers": _portable_answers(answers),
        # Overrides travel in their own field. They also arrive inside answers, from a
        # plan that carried them there, and taking only the argument would drop them.
        "workflow_overrides": dict(
            workflow_overrides or answers.get("workflow_overrides") or {}
        ),
    }
    directory = workset_directory()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{identifier}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(item, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    item["file"] = str(path)
    return item


def describe_workset_candidate(
    answers: dict[str, Any],
    *,
    source: dict[str, Any] | None = None,
    suggested_name: str = "",
) -> dict[str, Any]:
    """What a workset made from these answers would carry, and what it would drop.

    The point of a workset is that the second dataset only has to confirm what changed.
    That is only safe if the things which cannot be inherited are visibly not
    inherited, so both halves are returned: the method settings that carry, and the
    dataset-scoped answers that are deliberately left behind with the reason.
    """
    reusable = _portable_answers(answers)
    dropped = {
        key: DATASET_SCOPED_ANSWERS[key]
        for key in sorted(answers)
        if key in DATASET_SCOPED_ANSWERS
    }
    # A minimum peak height is a method parameter a laboratory fixes for an instrument,
    # so it carries. When it came from a diagnostic it was *measured* on these files,
    # and saving a measurement as a setting is only safe if it says so.
    caveats: list[str] = []
    if answers.get("parameter_strategy") in {"target_peak_count", "auto_peak_range"} and (
        "minimum_peak_height" in reusable
    ):
        caveats.append(
            f"minimum_peak_height {reusable['minimum_peak_height']} was measured by a "
            "diagnostic run on this dataset, not chosen. Reusing it on data from another "
            "instrument or preparation carries a threshold nobody measured there."
        )

    previous = dict((source or {}).get("answers", {}))
    changed = {
        key: {"was": previous.get(key), "now": value}
        for key, value in sorted(reusable.items())
        if key not in previous or previous[key] != value
    }
    return {
        "suggested_name": str(suggested_name).strip(),
        "reusable_answers": reusable,
        "workflow_overrides": dict(answers.get("workflow_overrides") or {}),
        "not_reusable": dropped,
        "caveats": caveats,
        "source_workset": (source or {}).get("id", ""),
        # A workset already in use and unchanged does not need saving again; one built
        # from nothing, or changed since, does.
        "changed_from_source": changed,
        "worth_saving": bool(reusable) and (source is None or bool(changed)),
    }


def _portable_answers(answers: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in answers.items()
        if key not in DATASET_SCOPED_ANSWERS and key != "workflow_overrides"
    }


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "workset-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
