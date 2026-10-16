"""Zip backups: backups/YYYYMMDD-HHMM_<rev>_<stage>.zip, pruned per stage."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from .config import AcdError
from .store import Project, make_zip, restore_zip

STAGES = ("spec", "library", "schematic", "placement", "routed", "review",
          "approved", "pre-restore", "manual")
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
