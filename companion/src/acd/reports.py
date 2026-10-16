"""Read kicad-cli ERC/DRC JSON reports."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .config import AcdError

_NET_RE = re.compile(r"\[([^\]]+)\]")


def load_report(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise AcdError(f"kicad-cli did not produce {path.name}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AcdError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise AcdError(f"{path.name} has an unexpected structure")
    return data


def _brief(v: dict[str, Any]) -> dict[str, Any]:
    return {
        "type": v.get("type", ""),
        "severity": v.get("severity", ""),
        "description": v.get("description", ""),
        "items": [i.get("description", "") for i in v.get("items", []) if isinstance(i, dict)],
    }


def _count(violations: list[dict[str, Any]]) -> dict[str, int]:
    out = {"errors": 0, "warnings": 0, "excluded": 0}
    for v in violations:
        sev = v.get("severity")
        if sev == "error":
            out["errors"] += 1
        elif sev == "warning":
            out["warnings"] += 1
        elif sev == "exclusion":
            out["excluded"] += 1
    return out


def erc_violations(report: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sheet in report.get("sheets", []):
        out.extend(v for v in sheet.get("violations", []) if isinstance(v, dict))
    return out


def summarize_erc(report: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    viol = erc_violations(report)
    counts = _count(viol)
    counts["first_errors"] = [_brief(v) for v in viol if v.get("severity") == "error"][:limit]
    return counts


def drc_parts(report: dict[str, Any]) -> tuple[list, list, list]:
    def lst(key: str) -> list[dict[str, Any]]:
        return [v for v in report.get(key, []) if isinstance(v, dict)]
    return lst("violations"), lst("unconnected_items"), lst("schematic_parity")


def summarize_drc(report: dict[str, Any], limit: int = 20) -> dict[str, Any]:
    viol, unconnected, parity = drc_parts(report)
    # DRC errors include schematic parity errors; unconnected items are gate 5.
    both = viol + parity
    counts = _count(both)
    counts["unconnected"] = len(unconnected)
    counts["parity_errors"] = _count(parity)["errors"]
    counts["first_errors"] = [_brief(v) for v in both if v.get("severity") == "error"][:limit]
    counts["first_unconnected"] = [_brief(v) for v in unconnected][:limit]
    return counts


def unconnected_nets(report: dict[str, Any]) -> list[str]:
    """Net names mentioned in unconnected item descriptions (best effort)."""
    _, unconnected, _ = drc_parts(report)
    nets: set[str] = set()
    for u in unconnected:
        for item in u.get("items", []):
            nets.update(_NET_RE.findall(item.get("description", "")))
    return sorted(nets)
