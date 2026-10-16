"""Write the rule profile into .kicad_pro (JSON) and .kicad_dru (custom rules)."""

from __future__ import annotations

import json
from typing import Any

from .config import AcdError
from .profile import get, require
from .store import Project, write_json_atomic

# VERIFY-K10: key names below are from KiCad 8/9 project files; re-check
# against a project saved by KiCad 10 (board.design_settings.rules,
# board.design_settings.defaults, net_settings.classes).


def _mm(value: Any) -> float:
    return round(float(value), 4)


def build_rules(profile: dict[str, Any]) -> dict[str, float]:
    """board.design_settings.rules  # VERIFY-K10"""
    return {
        "min_clearance": _mm(require(profile, "clearance.min_mm")),
        "min_track_width": _mm(require(profile, "tracks.min_width_mm")),
        "min_copper_edge_clearance": _mm(require(profile, "clearance.copper_to_edge_mm")),
        "min_via_diameter": _mm(require(profile, "vias.pad_mm")),
        "min_through_hole_diameter": _mm(require(profile, "drill.min_mm")),
        "min_via_annular_width": _mm(require(profile, "annular_ring.min_mm")),
        "min_text_height": _mm(require(profile, "silkscreen.min_text_height_mm")),
        "min_text_thickness": _mm(require(profile, "silkscreen.min_line_mm")),
        "solder_mask_min_width": _mm(require(profile, "solder_mask.min_web_mm")),
    }


def build_defaults(profile: dict[str, Any]) -> dict[str, float]:
    """board.design_settings.defaults (silkscreen)  # VERIFY-K10"""
    height = _mm(require(profile, "silkscreen.default_text_height_mm"))
    line = _mm(require(profile, "silkscreen.default_line_mm"))
    return {
        "silk_line_width": line,
        "silk_text_size_h": height,
        "silk_text_size_v": height,
        "silk_text_thickness": line,
    }


def build_default_netclass(profile: dict[str, Any]) -> dict[str, Any]:
    """net_settings.classes[name == Default]  # VERIFY-K10"""
    return {
        "clearance": _mm(require(profile, "clearance.default_mm")),
        "track_width": _mm(require(profile, "tracks.default_width_mm")),
        "via_diameter": _mm(require(profile, "vias.pad_mm")),
        "via_drill": _mm(require(profile, "vias.drill_mm")),
    }


def apply_to_pro(pro: dict[str, Any], profile: dict[str, Any]) -> dict[str, Any]:
    ds = pro.setdefault("board", {}).setdefault("design_settings", {})
    ds.setdefault("rules", {}).update(build_rules(profile))
    ds.setdefault("defaults", {}).update(build_defaults(profile))
    net = pro.setdefault("net_settings", {})
    classes = net.setdefault("classes", [])
    default_cls = next((c for c in classes if c.get("name") == "Default"), None)
    if default_cls is None:
        default_cls = {"name": "Default"}
        classes.insert(0, default_cls)
    default_cls.update(build_default_netclass(profile))
    return pro


def build_dru(profile: dict[str, Any]) -> str:
    edge = _mm(require(profile, "clearance.copper_to_edge_mm"))
    ring = _mm(require(profile, "annular_ring.min_mm"))
    dmin = _mm(require(profile, "drill.min_mm"))
    dmax = _mm(require(profile, "drill.max_mm"))
    th = _mm(require(profile, "silkscreen.min_text_height_mm"))
    tt = _mm(require(profile, "silkscreen.min_line_mm"))
    name = get(profile, "profile", "profile")
    return f"""(version 1)
# Written by acd apply_profile from rule profile '{name}'. Do not edit by hand;
# change the profile and run apply_profile again.

(rule "acd_copper_to_edge"
  (condition "A.Type == 'Track' || A.Type == 'Via' || A.Type == 'Pad' || A.Type == 'Zone'")
  (constraint edge_clearance (min {edge}mm)))

(rule "acd_via_annular_ring"
  (condition "A.Type == 'Via'")
  (constraint annular_width (min {ring}mm)))

# VERIFY-K10: A.Pad_Type value 'Through-hole'
(rule "acd_pad_annular_ring"
  (condition "A.Type == 'Pad' && A.Pad_Type == 'Through-hole'")
  (constraint annular_width (min {ring}mm)))

# VERIFY-K10: hole_size constraint name
(rule "acd_drill_size"
  (condition "A.Type == 'Via' || A.Type == 'Pad'")
  (constraint hole_size (min {dmin}mm) (max {dmax}mm)))

# VERIFY-K10: text_height / text_thickness constraint names
(rule "acd_silk_text_height"
  (condition "(A.Type == 'Text' || A.Type == 'Text Box') && (A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')")
  (constraint text_height (min {th}mm)))

(rule "acd_silk_text_thickness"
  (condition "(A.Type == 'Text' || A.Type == 'Text Box') && (A.Layer == 'F.Silkscreen' || A.Layer == 'B.Silkscreen')")
  (constraint text_thickness (min {tt}mm)))
"""


def apply_profile_files(project: Project, profile: dict[str, Any]) -> dict[str, Any]:
    project.require_file(project.pro, f"{project.name}.kicad_pro")
    try:
        pro = json.loads(project.pro.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AcdError(f"{project.pro.name} is not valid JSON: {exc}") from exc
    apply_to_pro(pro, profile)
    write_json_atomic(project.pro, pro)
    project.dru.write_text(build_dru(profile), encoding="utf-8", newline="\n")
    return {
        "kicad_pro": project.pro.name,
        "kicad_dru": project.dru.name,
        "rules": build_rules(profile),
        "default_netclass": build_default_netclass(profile),
    }
