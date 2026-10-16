"""Rule profile loading (YAML)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .config import AcdError


def load_profile(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AcdError(f"rule profile not found: {path} (set ACD_PROFILE)")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AcdError(f"rule profile {path} is not valid YAML: {exc}") from exc
    if not isinstance(data, dict) or "profile" not in data:
        raise AcdError(f"rule profile {path} has no 'profile' name")
    return data


def get(profile: dict[str, Any], dotted: str, default: Any = None) -> Any:
    cur: Any = profile
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def require(profile: dict[str, Any], dotted: str) -> Any:
    val = get(profile, dotted)
    if val is None:
        raise AcdError(f"rule profile is missing required key '{dotted}'")
    return val
