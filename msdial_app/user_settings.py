from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


APP_DIRECTORY_NAME = "MSDIALInteractive"
SETTINGS_FILENAME = "settings.json"
PATH_SETTING_KEYS = {"console_path", "template_path", "queries_path"}


def user_config_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        return base / APP_DIRECTORY_NAME
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_DIRECTORY_NAME
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "msdial-interactive"


def user_data_directory() -> Path:
    if os.name == "nt" or sys.platform == "darwin":
        return user_config_directory()
    base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return base / "msdial-interactive"


def settings_path() -> Path:
    return user_config_directory() / SETTINGS_FILENAME


def load_user_settings() -> dict[str, Any]:
    path = settings_path()
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def save_path_settings(values: dict[str, Any]) -> dict[str, str]:
    current = load_user_settings()
    for key in PATH_SETTING_KEYS:
        if key in values:
            current[key] = str(values.get(key, "")).strip()
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(current, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)
    return {key: str(current.get(key, "")) for key in sorted(PATH_SETTING_KEYS)}
