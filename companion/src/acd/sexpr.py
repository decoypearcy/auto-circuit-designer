"""Tiny KiCad s-expression reader, only enough to map references to lib_ids."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Union

Node = Union[str, list]


def parse(text: str) -> list[Any]:
    """Parse text into nested lists of strings. Quoted strings lose their quotes."""
    tokens: list[str | tuple[str]] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c in " \t\r\n":
            i += 1
        elif c in "()":
            tokens.append(c)
            i += 1
        elif c == '"':
            j = i + 1
            buf = []
            while j < n and text[j] != '"':
                if text[j] == "\\" and j + 1 < n:
                    j += 1
                buf.append(text[j])
                j += 1
            tokens.append((("".join(buf)),))  # tuple marks a quoted string
            i = j + 1
        else:
            j = i
            while j < n and text[j] not in ' \t\r\n()"':
                j += 1
            tokens.append(text[i:j])
            i = j
    root: list[Any] = []
    stack: list[list[Any]] = [root]
    for t in tokens:
        if t == "(":
            new: list[Any] = []
            stack[-1].append(new)
            stack.append(new)
        elif t == ")":
            if len(stack) > 1:
                stack.pop()
        elif isinstance(t, tuple):
            stack[-1].append(t[0])
        else:
            stack[-1].append(t)
    return root


def _children(node: list, head: str) -> list[list]:
    return [c for c in node if isinstance(c, list) and c and c[0] == head]


def _props(node: list) -> dict[str, str]:
    out = {}
    for p in _children(node, "property"):
        if len(p) >= 3 and isinstance(p[1], str) and isinstance(p[2], str):
            out[p[1]] = p[2]
    return out


def symbol_lib_ids(sch_path: Path, _seen: set[Path] | None = None) -> dict[str, str]:
    """Map reference -> lib_id for placed symbols, following hierarchical sheets."""
    seen = _seen if _seen is not None else set()
    sch_path = sch_path.resolve()
    if sch_path in seen or not sch_path.is_file():
        return {}
    seen.add(sch_path)
    tree = parse(sch_path.read_text(encoding="utf-8"))
    top = tree[0] if tree and isinstance(tree[0], list) else []
    result: dict[str, str] = {}
    for sym in _children(top, "symbol"):
        lib = _children(sym, "lib_id")
        if not lib or len(lib[0]) < 2:
            continue
        lib_id = lib[0][1]
        refs = []
        ref = _props(sym).get("Reference")
        if ref:
            refs.append(ref)
        for inst in _children(sym, "instances"):
            for proj in _children(inst, "project"):
                for path in _children(proj, "path"):
                    for r in _children(path, "reference"):
                        if len(r) >= 2:
                            refs.append(r[1])
        for r in refs:
            result.setdefault(r, lib_id)
    for sheet in _children(top, "sheet"):
        sub = _props(sheet).get("Sheetfile") or _props(sheet).get("Sheet file")
        if sub:
            result.update(symbol_lib_ids(sch_path.parent / sub, seen))
    return result
