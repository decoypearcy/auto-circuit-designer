"""Checks and fixes that KiCad DRC and the kicad server do not cover (found in T-15).

netclass_check: KiCad DRC does not enforce a net class track width, and the kicad
server's create_netclass can leave a class with no nets. Both pass DRC silently.

fix_bom_flags: the kicad server writes (in_bom yes) on every placed symbol, even when
the library symbol says (in_bom no) (for example Mechanical:MountingHole), so mounting
holes land in the BOM.
"""

from __future__ import annotations

import fnmatch
import json
import re
from pathlib import Path
from typing import Any

from .config import AcdError
from .sexpr import parse

TOL = 1e-6


def _children(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _first(node: list, head: str) -> list | None:
    found = _children(node, head)
    return found[0] if found else None


# -- net classes -------------------------------------------------------------
def load_netclasses(pro_path: Path) -> tuple[dict[str, float], dict[str, list[str]], list[dict]]:
    try:
        pro = json.loads(pro_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcdError(f"cannot read {pro_path.name}: {exc}") from exc
    ns = pro.get("net_settings") or {}
    widths = {c.get("name", ""): float(c.get("track_width") or 0)
              for c in ns.get("classes") or [] if isinstance(c, dict)}
    assignments = ns.get("netclass_assignments") or {}
    patterns = [p for p in ns.get("netclass_patterns") or [] if isinstance(p, dict)]
    return widths, assignments, patterns


def class_of(net: str, assignments: dict[str, list[str]], patterns: list[dict]) -> str:
    """Net class for a net name: explicit assignment, then the first matching pattern."""
    bare = net.lstrip("/")
    names = [net, bare, "/" + bare]  # KiCad local-label nets carry a sheet prefix
    for n in names:
        cls = assignments.get(n)
        if cls:
            return cls[0] if isinstance(cls, list) else str(cls)
    for p in patterns:
        pat = str(p.get("pattern", ""))
        if any(fnmatch.fnmatchcase(n, pat) for n in names):
            return str(p.get("netclass", "Default"))
    return "Default"


def board_tracks(pcb_path: Path) -> list[dict[str, Any]]:
    """Track segments and arcs: net name, width, layer. Handles net by name or by code."""
    try:
        tree = parse(pcb_path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AcdError(f"cannot read {pcb_path.name}: {exc}") from exc
    top = tree[0] if tree and isinstance(tree[0], list) else []
    codes = {n[1]: n[2] for n in _children(top, "net") if len(n) >= 3}
    out = []
    for kind in ("segment", "arc"):
        for seg in _children(top, kind):
            w, net, layer = _first(seg, "width"), _first(seg, "net"), _first(seg, "layer")
            if not w or not net or len(w) < 2 or len(net) < 2:
                continue
            name = net[1]
            if len(net) == 2 and name in codes:  # (net 3) form, older files
                name = codes[name]
            out.append({"net": name, "width": float(w[1]),
                        "layer": layer[1] if layer and len(layer) > 1 else ""})
    return out


def netclass_check(pro_path: Path, pcb_path: Path) -> dict[str, Any]:
    widths, assignments, patterns = load_netclasses(pro_path)
    tracks = board_tracks(pcb_path)
    nets = sorted({t["net"] for t in tracks if t["net"]})
    used: dict[str, list[str]] = {}
    narrow: dict[str, dict[str, Any]] = {}
    for net in nets:
        cls = class_of(net, assignments, patterns)
        used.setdefault(cls, []).append(net)
    for t in tracks:
        cls = class_of(t["net"], assignments, patterns)
        want = widths.get(cls, 0)
        if want and t["width"] + TOL < want:
            row = narrow.setdefault(t["net"], {"class": cls, "class_width": want,
                                               "min_width": t["width"], "segments": 0})
            row["segments"] += 1
            row["min_width"] = min(row["min_width"], t["width"])
    # A non-Default class that no routed net uses means the assignment never happened.
    empty = sorted(c for c in widths if c != "Default" and c not in used)
    return {"classes": {c: sorted(used.get(c, [])) for c in widths},
            "classes_without_nets": empty, "narrow_tracks": narrow}


def required_width(table: list[dict[str, Any]], current_a: float) -> float:
    """Minimum track width for a current, from the profile tracks.power_width_table."""
    rows = sorted((float(r["up_to_a"]), float(r["width_mm"])) for r in table)
    for limit, width in rows:
        if current_a <= limit + TOL:
            return width
    raise AcdError(f"current above {rows[-1][0]:g} A: ask Andrew for a width")


def class_width_failures(spec_classes: Any, widths: dict[str, float],
                         table: list[dict[str, Any]]) -> list[str]:
    """Spec net classes whose .kicad_pro class width is below the width their current needs."""
    out: list[str] = []
    for c in spec_classes or []:
        if not isinstance(c, dict) or c.get("current_a") in (None, ""):
            continue
        name = str(c.get("name", ""))
        try:
            amps = float(c["current_a"])
        except (TypeError, ValueError):
            out.append(f"class {name}: current_a {c['current_a']!r} is not a number")
            continue
        try:
            need = required_width(table, amps)
        except AcdError as exc:
            out.append(f"class {name} {amps:g} A: {exc}")
            continue
        have = widths.get(name)
        if have is None:
            out.append(f"class {name} ({amps:g} A) is not in the .kicad_pro net classes")
        elif have + TOL < need:
            out.append(f"class {name} {have:g} mm vs {need:g} mm needed for {amps:g} A")
    return out


# -- BOM flags ---------------------------------------------------------------
_INSTANCE_RE = re.compile(r'\(symbol\s*\(lib_id\s+"([^"]+)"\)')


def _block_end(text: str, start: int) -> int:
    """Index just past the s-expression that opens at text[start] == '('."""
    depth, i, n, in_str = 0, start, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            if c == "\\":
                i += 1
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AcdError("unbalanced parentheses in schematic")


def lib_not_in_bom(text: str) -> set[str]:
    """lib_ids whose embedded library symbol says (in_bom no)."""
    tree = parse(text)
    top = tree[0] if tree and isinstance(tree[0], list) else []
    out = set()
    libs = _first(top, "lib_symbols") or []
    for s in _children(libs, "symbol"):
        flag = _first(s, "in_bom")
        if len(s) > 1 and flag and len(flag) > 1 and flag[1] == "no":
            out.add(s[1])
    return out


def fix_bom_flags_text(text: str) -> tuple[str, list[str]]:
    """Set (in_bom no) on placed symbols whose library symbol is not in the BOM."""
    skip = lib_not_in_bom(text)
    if not skip:
        return text, []
    changed: list[str] = []
    parts: list[str] = []
    pos = 0
    for m in _INSTANCE_RE.finditer(text):
        if m.start() < pos or m.group(1) not in skip:
            continue
        end = _block_end(text, m.start())
        block = text[m.start():end]
        new = block.replace("(in_bom yes)", "(in_bom no)", 1)
        if new != block:
            ref = re.search(r'\(property\s+"Reference"\s+"([^"]*)"', block)
            changed.append(ref.group(1) if ref else m.group(1))
            parts.append(text[pos:m.start()])
            parts.append(new)
            pos = end
    parts.append(text[pos:])
    return "".join(parts), changed
