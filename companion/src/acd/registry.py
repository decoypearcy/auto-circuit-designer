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


def _norm(mpn: Any) -> str:
    return str(mpn or "").strip().lower()


def load(settings: Settings) -> dict[str, dict[str, Any]]:
    """{lib_id: {"variants": [entry, ...]}}. An old-format entry (top-level "mpn") is read as
    a single variant; the next save() writes the new format."""
    p = registry_path(settings)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AcdError(f"{p} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AcdError(f"{p} must contain a JSON object")
    out: dict[str, dict[str, Any]] = {}
    for lib_id, value in data.items():
        if isinstance(value, dict) and isinstance(value.get("variants"), list):
            out[lib_id] = {"variants": value["variants"]}
        elif isinstance(value, dict) and "mpn" in value:
            out[lib_id] = {"variants": [value]}
        else:
            raise AcdError(f"{p}: entry {lib_id!r} has neither 'variants' nor 'mpn'")
    return out


def save(settings: Settings, data: dict[str, dict[str, Any]]) -> None:
    write_json_atomic(registry_path(settings), data)


def find_variant(reg: dict[str, dict[str, Any]], lib_id: str, mpn: str) -> dict[str, Any] | None:
    """The variant of lib_id whose MPN matches (stripped, case-insensitive), or None."""
    for entry in reg.get(lib_id, {}).get("variants", []):
        if _norm(entry.get("mpn")) == _norm(mpn):
            return entry
    return None


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
    variants = data.setdefault(lib_id, {"variants": []})["variants"]
    for i, old in enumerate(variants):
        if _norm(old.get("mpn")) == _norm(mpn):
            variants[i] = entry  # new or changed variant: verification starts again
            updated = True
            break
    else:
        variants.append(entry)
        updated = False
    save(settings, data)
    return {"lib_id": lib_id, "entry": entry, "action": "updated" if updated else "added",
            "updated_existing": updated, "variant_count": len(variants)}


def verify_part(settings: Settings, lib_id: str, verified_by: str, mpn: str = "") -> dict[str, Any]:
    if not verified_by or not verified_by.strip():
        raise AcdError("verified_by is required")
    data = load(settings)
    if lib_id not in data:
        raise AcdError(f"{lib_id!r} is not in the registry; register_part first")
    variants = data[lib_id]["variants"]
    if _norm(mpn):
        entry = find_variant(data, lib_id, mpn)
        if entry is None:
            raise AcdError(f"{lib_id!r} has no variant with MPN {mpn.strip()!r}; register_part first")
    elif len(variants) == 1:
        entry = variants[0]
    else:
        listed = ", ".join(str(v.get("mpn", "")) for v in variants) or "none"
        raise AcdError(f"{lib_id!r} has {len(variants)} variants; pass mpn to say which to verify ({listed})")
    entry.update({"verified": True, "verified_by": verified_by.strip(),
                  "verified_date": date.today().isoformat()})
    save(settings, data)
    return {"lib_id": lib_id, "entry": entry}
