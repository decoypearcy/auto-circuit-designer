"""Project folder access, JSON records, hashing, lock check and zipping."""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .config import AcdError, Settings, validate_name

# Folders at the project root that are never zipped or restored.
EXCLUDED_ROOT_DIRS = ("backups", "releases")
# Restore and new_revision leave these alone (history must survive a restore).
PROTECTED_ON_RESTORE = ("backups", "releases", "records")
LOCK_PATTERNS = ("*.lck", "~*")
KICAD_BACKUP_DIR_SUFFIX = "-backups"


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


# -- paths ----------------------------------------------------------------
class Project:
    def __init__(self, settings: Settings, name: str, must_exist: bool = True):
        self.settings = settings
        self.name = validate_name(name)
        self.root = settings.projects_root / self.name
        if must_exist and not self.root.is_dir():
            raise AcdError(f"project {name!r} not found under {settings.projects_root}")

    # KiCad files
    @property
    def pro(self) -> Path: return self.root / f"{self.name}.kicad_pro"
    @property
    def sch(self) -> Path: return self.root / f"{self.name}.kicad_sch"
    @property
    def pcb(self) -> Path: return self.root / f"{self.name}.kicad_pcb"
    @property
    def dru(self) -> Path: return self.root / f"{self.name}.kicad_dru"
    # acd files
    @property
    def spec(self) -> Path: return self.root / "spec" / "circuit-spec.yaml"
    @property
    def records(self) -> Path: return self.root / "records"
    @property
    def reports(self) -> Path: return self.root / "reports"
    @property
    def backups(self) -> Path: return self.root / "backups"
    @property
    def releases(self) -> Path: return self.root / "releases"

    def release_dir(self, rev: str) -> Path:
        return self.releases / f"rev-{rev}"

    def require_file(self, path: Path, what: str) -> Path:
        if not path.is_file():
            raise AcdError(f"{what} not found: {path.name} (create it with the base KiCad MCP first)")
        return path

    # records
    def read_json(self, rel: str) -> Any:
        p = self.root / rel
        if not p.is_file():
            raise AcdError(f"{rel} is missing - is {self.name!r} an acd project?")
        return json.loads(p.read_text(encoding="utf-8"))

    def write_json(self, rel: str, data: Any) -> None:
        write_json_atomic(self.root / rel, data)

    def project_info(self) -> dict[str, Any]:
        return self.read_json("records/project.json")

    def revisions(self) -> list[dict[str, Any]]:
        return self.read_json("records/revisions.json")

    def current_rev(self) -> str:
        return self.project_info()["current_rev"]

    def revision(self, rev: str) -> dict[str, Any]:
        for r in self.revisions():
            if r["rev"] == rev:
                return r
        raise AcdError(f"revision {rev!r} not found")

    # hashing
    def file_hash(self, path: Path) -> str | None:
        return sha256_file(path) if path.is_file() else None


def write_json_atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# -- lock check -----------------------------------------------------------
def _is_lock(name: str) -> bool:
    return any(fnmatch.fnmatch(name, pat) for pat in LOCK_PATTERNS)


def _walk(root: Path) -> Iterator[Path]:
    """Yield files under root, skipping backups/, releases/ and KiCad's own *-backups/."""
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        keep = []
        for d in sorted(dirnames):
            if here == root and d in EXCLUDED_ROOT_DIRS:
                continue
            if d.endswith(KICAD_BACKUP_DIR_SUFFIX):
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            yield here / f


def find_locks(root: Path) -> list[str]:
    return [p.relative_to(root).as_posix() for p in _walk(root) if _is_lock(p.name)]


def lock_check(project: Project) -> dict[str, Any]:
    files = find_locks(project.root)
    return {"locked": bool(files), "files": files}


def require_unlocked(project: Project) -> None:
    res = lock_check(project)
    if res["locked"]:
        raise AcdError(
            "KiCad appears to have this project open (lock files present: "
            + ", ".join(res["files"])
            + "). Close KiCad and try again."
        )


# -- zipping --------------------------------------------------------------
def project_files(root: Path) -> list[Path]:
    """Files that go into backups and project.zip."""
    return [p for p in _walk(root) if not _is_lock(p.name) and not p.name.endswith(".tmp")]


def make_zip(root: Path, dest: Path) -> int:
    tmp = dest.with_name(dest.name + ".tmp")
    n = 0
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in project_files(root):
            zf.write(p, p.relative_to(root).as_posix())
            n += 1
    os.replace(tmp, dest)
    return n


def safe_member_target(root: Path, member: str) -> Path | None:
    """Resolve a zip member under root; None if it would escape."""
    target = (root / member).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


def is_protected_member(member: str) -> bool:
    first = member.replace("\\", "/").split("/", 1)[0]
    return first in PROTECTED_ON_RESTORE


def restore_zip(root: Path, zip_path: Path, *, delete_extras: bool = True) -> int:
    """Replace working files with the zip contents (never backups/, releases/, records/)."""
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.namelist() if not m.endswith("/")]
        todo = [m for m in members if not is_protected_member(m)]
        for m in todo:
            if safe_member_target(root, m) is None:
                raise AcdError(f"refusing unsafe path in archive: {m}")
        if delete_extras:
            for p in project_files(root):
                rel = p.relative_to(root).as_posix()
                if not is_protected_member(rel):
                    p.unlink()
        for m in todo:
            target = safe_member_target(root, m)
            assert target is not None
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(m) as src, target.open("wb") as dst:
                dst.write(src.read())
    return len(todo)


def zip_differs(root: Path, zip_path: Path) -> bool:
    """True if working files (excluding protected folders) differ from the zip."""
    with zipfile.ZipFile(zip_path) as zf:
        expected = {
            m: hashlib.sha256(zf.read(m)).hexdigest()
            for m in zf.namelist()
            if not m.endswith("/") and not is_protected_member(m)
        }
    actual = {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in project_files(root)
        if not is_protected_member(p.relative_to(root).as_posix())
    }
    return expected != actual
