from __future__ import annotations

import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .user_settings import user_data_directory


LIBRARY_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "id": "metabolomics-positive",
        "label": "Metabolomics MS/MS library (positive)",
        "scope": "LC-MS / LC-IM-MS / DI-MS / IM-MS, positive ion mode",
        "kind": "msp",
        "ion_mode": "Positive",
        "record_id": 21901200,
        "filename": "MSMS-Public_all-pos-VS20.msp",
        "size": 396_946_829,
        "md5": "53ab4d94cd98b6aecbac0eb73cb9f2e7",
    },
    {
        "id": "metabolomics-negative",
        "label": "Metabolomics MS/MS library (negative)",
        "scope": "LC-MS / LC-IM-MS / DI-MS / IM-MS, negative ion mode",
        "kind": "msp",
        "ion_mode": "Negative",
        "record_id": 21904103,
        "filename": "MSMS-Public_all-neg-VS20.msp",
        "size": 50_818_071,
        "md5": "2cc3f3843d7c139e807611553b966202",
    },
    {
        "id": "lipidomics",
        "label": "Lipidomics LBM library",
        "scope": "LC-MS lipidomics",
        "kind": "lbm",
        "record_id": 21904324,
        "filename": "Msp20250303164224_NCDK-TUAT-LC25_converted_dev.lbm2",
        "size": 785_186_493,
        "md5": "c54c577ec40d2e8fd6d4365daf4a8157",
    },
    {
        "id": "gcms-kovats",
        "label": "GC-MS EI library (Kovats RI)",
        "scope": "GC-MS with alkane/Kovats RI",
        "kind": "gcms_msp",
        "ri_compound_type": "Alkanes",
        "record_id": 21910638,
        "filename": "GCMS DB-Public-KovatsRI-VS3.msp",
        "size": 23_075_883,
        "md5": "34c20f2d8fbfb21c53b44e7ebab6b1ad",
    },
    {
        "id": "gcms-fiehn",
        "label": "GC-MS EI library (Fiehn RI)",
        "scope": "GC-MS with FAME/Fiehn RI",
        "kind": "gcms_msp",
        "ri_compound_type": "Fames",
        "record_id": 21910646,
        "filename": "GCMS DB-Public-FiehnRI-VS3.msp",
        "size": 23_073_288,
        "md5": "dc03f35bd11ca2c666fe9aff0e2d4a3c",
    },
)


def library_directory() -> Path:
    return user_data_directory() / "libraries"


def _entry(catalog_id: str) -> dict[str, Any]:
    try:
        return next(item for item in LIBRARY_CATALOG if item["id"] == catalog_id)
    except StopIteration as error:
        raise ValueError(f"Unknown library catalog entry: {catalog_id}") from error


def library_path(item: dict[str, Any]) -> Path:
    return library_directory() / str(item["record_id"]) / str(item["filename"])


def catalog_status() -> list[dict[str, Any]]:
    result = []
    for source in LIBRARY_CATALOG:
        item = dict(source)
        path = library_path(item)
        downloaded = _has_verified_metadata(item, path)
        item.update(
            {
                "record_url": f"https://zenodo.org/records/{item['record_id']}",
                "local_path": str(path) if path.is_file() else "",
                "downloaded": downloaded,
                "size_mb": round(item["size"] / 1_000_000, 1),
                "license": "CC BY 4.0",
            }
        )
        result.append(item)
    return result


def _has_verified_metadata(item: dict[str, Any], path: Path) -> bool:
    metadata_path = path.parent / "zenodo-record.json"
    if not path.is_file() or path.stat().st_size != item["size"] or not metadata_path.is_file():
        return False
    try:
        metadata = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    return (
        str(metadata.get("record_id")) == str(item["record_id"])
        and metadata.get("filename") == item["filename"]
        and str(metadata.get("md5", "")).casefold() == item["md5"].casefold()
    )


def download_library(
    catalog_id: str,
    progress: Callable[[int, int], None] | None = None,
) -> dict[str, Any]:
    item = _entry(catalog_id)
    target = library_path(item)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == item["size"]:
        if _md5(target) == item["md5"]:
            _write_metadata(item, target)
            return _download_result(item, target, reused=True)

    encoded_name = urllib.parse.quote(str(item["filename"]))
    url = f"https://zenodo.org/records/{item['record_id']}/files/{encoded_name}?download=1"
    temporary = target.with_suffix(target.suffix + ".part")
    digest = hashlib.md5()
    received = 0
    request = urllib.request.Request(url, headers={"User-Agent": "MS-DIAL-Interactive/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as handle:
            total = int(response.headers.get("Content-Length") or item["size"])
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                handle.write(block)
                digest.update(block)
                received += len(block)
                if progress:
                    progress(received, total)
        if received != item["size"]:
            raise RuntimeError(
                f"Downloaded size mismatch for {item['filename']}: {received} != {item['size']} bytes."
            )
        if digest.hexdigest().lower() != item["md5"]:
            raise RuntimeError(f"MD5 verification failed for {item['filename']}.")
        os.replace(temporary, target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise

    _write_metadata(item, target)
    return _download_result(item, target, reused=False)


def _write_metadata(item: dict[str, Any], target: Path) -> None:
    metadata = target.parent / "zenodo-record.json"
    metadata.write_text(
        json.dumps(
            {
                "record_id": item["record_id"],
                "record_url": f"https://zenodo.org/records/{item['record_id']}",
                "filename": item["filename"],
                "size": item["size"],
                "md5": item["md5"],
                "license": "CC BY 4.0",
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _md5(path: Path) -> str:
    digest = hashlib.md5()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().lower()


def _download_result(item: dict[str, Any], path: Path, reused: bool) -> dict[str, Any]:
    return {
        "catalog_id": item["id"],
        "record_id": item["record_id"],
        "kind": item["kind"],
        "ion_mode": item.get("ion_mode", ""),
        "ri_compound_type": item.get("ri_compound_type", ""),
        "local_path": str(path),
        "record_url": f"https://zenodo.org/records/{item['record_id']}",
        "filename": item["filename"],
        "md5": item["md5"],
        "license": "CC BY 4.0",
        "reused": reused,
    }
