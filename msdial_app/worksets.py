from __future__ import annotations

import datetime as dt
import json
import re
from pathlib import Path
from typing import Any

from .user_settings import user_data_directory


WORKSET_SCHEMA = "msdial-interactive.workset.v1"


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
        "workflow_overrides": dict(workflow_overrides or {}),
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


def _portable_answers(answers: dict[str, Any]) -> dict[str, Any]:
    excluded = {"input_path", "output_root", "confirmed"}
    return {key: value for key, value in answers.items() if key not in excluded}


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "workset-" + dt.datetime.now().strftime("%Y%m%d%H%M%S")
