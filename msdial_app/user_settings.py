from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


APP_DIRECTORY_NAME = "MSDIALInteractive"
SETTINGS_FILENAME = "settings.json"
PATH_SETTING_KEYS = {
    "console_path",
    "console_source_kind",
    "console_source_root",
    "template_path",
    "queries_path",
    # Where the downloaded spectral libraries live. They used to be pinned under LOCALAPPDATA,
    # which on a Windows workstation is the system drive, and the public MS/MS libraries alone are
    # 1.2 GB before any laboratory library is added. A site whose data drive is not C: had no way
    # to say so.
    "library_directory",
    # The RawMetadataConsoleApp preflight runs. Without it the extractor was whichever build sat in
    # the working checkout beside this one, which is a moving tree with no build record.
    "raw_metadata_extractor_path",
}


# WHETHER A LEASE OUTSIDE A CAMPAIGN USES THE ACCESSION DOWNLOAD STORE (msdial_app.download_store).
#
# A campaign's lease always does: the user decided on 2026-09-30 that a shared object is downloaded once.
# Any other lease downloads into its unit's own raw tree, as every lease did before the store, unless this
# setting is "always". "campaign", the default, is that behaviour; anything else reads as it.
STORE_MODE_SETTING = "store_mode"
STORE_MODES = ("campaign", "always")
STORE_MODE_DEFAULT = "campaign"


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


def download_store_mode(settings: dict[str, Any] | None = None) -> str:
    """The saved store_mode, or the default when none is saved or the saved value means nothing."""
    value = str((load_user_settings() if settings is None else settings).get(STORE_MODE_SETTING) or "")
    value = value.strip().casefold()
    return value if value in STORE_MODES else STORE_MODE_DEFAULT


def save_download_store_mode(value: Any) -> str:
    """Save store_mode; refuses a value that is not one of STORE_MODES rather than guess at it."""
    mode = str(value or "").strip().casefold()
    if mode not in STORE_MODES:
        raise ValueError(f"store_mode must be one of {list(STORE_MODES)}, not {value!r}.")
    current = load_user_settings()
    current[STORE_MODE_SETTING] = mode
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)
    return mode
