"""Shared parts registry in ACD_LIBRARY_ROOT/registry.json."""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from .config import AcdError, Settings
from .store import write_json_atomic

STOCK_ACCEPTED = ("Device:R", "Device:C", "Device:L")


def registry_path(settings: Settings):
    return settings.library_root / "registry.json"


def load(settings: Settings) -> dict[str, dict[str, Any]]:
    p = registry_path(settings)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AcdError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AcdError(f"{p} must contain a JSON object")
    return data


def save(settings: Settings, data: dict[str, dict[str, Any]]) -> None:
    write_json_atomic(registry_path(settings), data)


def ensure_library_dirs(settings: Settings) -> None:
    root = settings.library_root
    (root / "symbols").mkdir(parents=True, exist_ok=True)
    (root / "footprints" / "acd.pretty").mkdir(parents=True, exist_ok=True)
    (root / "3dmodels").mkdir(parents=True, exist_ok=True)


def register_part(settings: Settings, lib_id: str, mpn: str, manufacturer: str,
                  datasheet: str, footprint: str, notes: str = "") -> dict[str, Any]:
    if not lib_id or ":" not in lib_id:
        raise AcdError("lib_id must look like 'acd:LM1117-3.3'")
    ensure_library_dirs(settings)
    data = load(settings)
    entry = {
        "mpn": mpn, "manufacturer": manufacturer, "datasheet": datasheet,
        "footprint": footprint, "verified": False, "verified_by": "",
        "verified_date": "", "notes": notes,
    }
    updated = lib_id in data
    data[lib_id] = entry
    save(settings, data)
    return {"lib_id": lib_id, "entry": entry, "updated_existing": updated}


def verify_part(settings: Settings, lib_id: str, verified_by: str) -> dict[str, Any]:
    if not verified_by or not verified_by.strip():
        raise AcdError("verified_by is required")
    data = load(settings)
    if lib_id not in data:
        raise AcdError(f"{lib_id!r} is not in the registry; register_part first")
    entry = data[lib_id]
    entry.update({"verified": True, "verified_by": verified_by.strip(),
                  "verified_date": date.today().isoformat()})
    save(settings, data)
    return {"lib_id": lib_id, "entry": entry}
