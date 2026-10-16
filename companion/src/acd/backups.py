"""Zip backups: backups/YYYYMMDD-HHMM_<rev>_<stage>.zip, pruned per stage."""

from __future__ import annotations

import hashlib
import re
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .config import AcdError
from .store import Project, make_zip, project_files, restore_zip

# Stages Claude (or Andrew) backs up by hand, then the stages the gate tools use on their own.
# Gate tools never write to the hand stages, so re-running a gate cannot prune them away.
STAGES = ("spec", "library", "schematic", "placement", "routed", "review",
          "approved", "pre-restore", "manual",
          "pre-parts", "pre-erc", "pre-drc")

# Files that define the design. Gate records, reports and .kicad_prl (KiCad UI state) are not
# design: a gate re-run that changes only those does not need a new backup.
_DESIGN_SUFFIXES = (".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru")
_SPEC_ARC = "spec/circuit-spec.yaml"
_NAME_RE = re.compile(r"^(\d{8}-\d{4})_([A-Z]+)_([a-z-]+)\.zip$")


def _stamp_name(project: Project, rev: str, stage: str) -> str:
    """Timestamp name in the documented format.

    Two backups of one stage inside the same minute would collide, and pruning
    would free old names for reuse; so the stamp is always later than every
    existing backup of that stage (bumped by whole minutes when needed).
    """
    ts = datetime.now().replace(second=0, microsecond=0)
    stamps = [b["stamp"] for b in list_backups(project) if b["stage"] == stage]
    if stamps:
        latest = datetime.strptime(max(stamps), "%Y%m%d-%H%M")
        if latest >= ts:
            ts = latest + timedelta(minutes=1)
    return f"{ts:%Y%m%d-%H%M}_{rev}_{stage}.zip"


def _parse(name: str) -> dict[str, str] | None:
    m = _NAME_RE.match(name)
    if not m:
        return None
    return {"name": name, "stamp": m.group(1), "rev": m.group(2), "stage": m.group(3)}


def list_backups(project: Project) -> list[dict[str, Any]]:
    if not project.backups.is_dir():
        return []
    items = []
    for p in project.backups.iterdir():
        info = _parse(p.name)
        if info and p.is_file():
            info["size_bytes"] = p.stat().st_size
            items.append(info)
    items.sort(key=lambda i: (i["stamp"], i["name"]), reverse=True)
    return items


def prune(project: Project, stage: str) -> list[str]:
    keep = project.settings.backup_keep
    same = [b for b in list_backups(project) if b["stage"] == stage]
    removed = []
    for b in same[keep:]:
        (project.backups / b["name"]).unlink()
        removed.append(b["name"])
    return removed


def create_backup(project: Project, stage: str, *, do_prune: bool = True) -> dict[str, Any]:
    if stage not in STAGES:
        raise AcdError(f"unknown stage {stage!r}; use one of: {', '.join(STAGES)}")
    project.backups.mkdir(exist_ok=True)
    name = _stamp_name(project, project.current_rev(), stage)
    count = make_zip(project.root, project.backups / name)
    removed = prune(project, stage) if do_prune else []
    return {"backup": name, "files": count, "pruned": removed}


def _is_design(arc: str) -> bool:
    return arc == _SPEC_ARC or arc.endswith(_DESIGN_SUFFIXES)


def _digest(items: list[tuple[str, bytes]]) -> str:
    h = hashlib.sha256()
    for arc, data in sorted(items):
        h.update(arc.encode("utf-8") + b"\0" + hashlib.sha256(data).digest())
    return h.hexdigest()


def design_digest(project: Project) -> str:
    """Hash of the design files in the working folder."""
    items = []
    for p in project_files(project.root):
        arc = p.relative_to(project.root).as_posix()
        if _is_design(arc):
            items.append((arc, p.read_bytes()))
    return _digest(items)


def design_digest_of_backup(path: Path) -> str:
    """Hash of the design files inside a backup zip (same rule as design_digest)."""
    with zipfile.ZipFile(path) as zf:
        return _digest([(n, zf.read(n)) for n in zf.namelist() if _is_design(n)])


def create_gate_backup(project: Project, stage: str) -> dict[str, Any]:
    """Backup taken by a gate tool before it writes its record.

    Skipped when the design files match the newest backup of the same stage: a re-run on an
    unchanged design would only add a copy and push older backups of that stage out of the
    keep window. The result names the backup that holds the state going in either way.
    """
    latest = next((b for b in list_backups(project) if b["stage"] == stage), None)
    if latest is not None:
        try:
            same = design_digest_of_backup(project.backups / latest["name"]) == design_digest(project)
        except (OSError, zipfile.BadZipFile):
            same = False
        if same:
            return {"backup": latest["name"], "files": 0, "pruned": [], "skipped": True}
    out = create_backup(project, stage)
    out["skipped"] = False
    return out


def restore_backup(project: Project, backup_name: str) -> dict[str, Any]:
    if backup_name != _safe_name(backup_name) or not _parse(backup_name):
        raise AcdError(f"invalid backup name {backup_name!r}")
    target = project.backups / backup_name
    if not target.is_file():
        raise AcdError(f"backup {backup_name!r} not found")
    pre = create_backup(project, "pre-restore", do_prune=False)
    restored = restore_zip(project.root, target)
    pruned = prune(project, "pre-restore")
    return {"restored": backup_name, "files_restored": restored,
            "pre_restore_backup": pre["backup"], "pruned": pruned}


def _safe_name(name: str) -> str:
    return name.replace("/", "").replace("\\", "")
