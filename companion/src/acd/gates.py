"""Gate bookkeeping: setting gates, hashing the file each gate checked, staleness."""

from __future__ import annotations

from typing import Any

from .config import AcdError
from .store import Project, now_iso, sha256_text

GATES = ("spec_confirmed", "parts_verified", "erc_clean", "drc_clean", "unrouted_zero", "approved")
GATE_FILE = {
    "spec_confirmed": "spec",
    "parts_verified": "sch",
    "erc_clean": "sch",
    "drc_clean": "pcb",
    "unrouted_zero": "pcb",
}


def next_rev(rev: str) -> str:
    """A..Z, AA..AZ, BA.. (spreadsheet-column style)."""
    letters = list(rev)
    i = len(letters) - 1
    while i >= 0:
        if letters[i] == "Z":
            letters[i] = "A"
            i -= 1
        else:
            letters[i] = chr(ord(letters[i]) + 1)
            return "".join(letters)
    return "A" + "".join(letters)


def new_revision_obj(rev: str, parent: str | None) -> dict[str, Any]:
    return {"rev": rev, "parent": parent, "created": now_iso(), "status": "draft",
            "gates": {}, "notes": ""}


def current_hash(project: Project, gate: str) -> str | None:
    """Hash of the file a gate checks, as it is on disk now."""
    if gate == "approved":
        return combined_hash(project)
    which = GATE_FILE[gate]
    path = {"spec": project.spec, "sch": project.sch, "pcb": project.pcb}[which]
    return project.file_hash(path)


def combined_hash(project: Project) -> str:
    return sha256_text(f"sch:{project.file_hash(project.sch)}|pcb:{project.file_hash(project.pcb)}")


def set_gate(project: Project, gate: str, passed: bool, detail: str,
             hash_value: str | None = None, rev: str | None = None) -> dict[str, Any]:
    if gate not in GATES:
        raise AcdError(f"unknown gate {gate!r}")
    revs = project.revisions()
    rev = rev or project.current_rev()
    for r in revs:
        if r["rev"] == rev:
            value = {
                "passed": bool(passed),
                "at": now_iso(),
                "hash": hash_value if hash_value is not None else current_hash(project, gate),
                "detail": detail,
            }
            r.setdefault("gates", {})[gate] = value
            project.write_json("records/revisions.json", revs)
            return value
    raise AcdError(f"revision {rev!r} not found")


def gate_status(project: Project, rev: str | None = None) -> dict[str, Any]:
    """Every gate with `stale` recomputed against the files on disk now."""
    rev = rev or project.current_rev()
    revision = project.revision(rev)
    stored = revision.get("gates", {})
    out: dict[str, Any] = {}
    for gate in GATES:
        g = stored.get(gate)
        if g is None:
            out[gate] = {"passed": False, "stale": False, "effective": False,
                         "at": None, "detail": "not run"}
            continue
        now_hash = current_hash(project, gate)
        stale = now_hash != g.get("hash")
        out[gate] = {
            "passed": bool(g["passed"]),
            "stale": stale,
            "effective": bool(g["passed"]) and not stale,
            "at": g.get("at"),
            "detail": g.get("detail", "") + (" [stale: file changed since check]" if stale else ""),
        }
    return {
        "rev": rev,
        "status": revision["status"],
        "gates": out,
        "all_pre_approval_passed": all(out[g]["effective"] for g in GATES[:5]),
    }
