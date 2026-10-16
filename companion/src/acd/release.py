"""Release export, manifest verification."""

from __future__ import annotations

import csv
import json
import os
import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import AcdError
from .kicad import KiCadCli
from .profile import get
from .store import Project, make_zip, sha256_file, write_fixed

BOM_FIELDS = ["Reference", "Value", "Footprint", "Manufacturer", "MPN", "${QUANTITY}"]
BOM_LABELS = ["Reference", "Value", "Footprint", "Manufacturer", "MPN", "Qty"]
BOM_GROUP = ["Value", "Footprint"]
PART_FIELDS = ["Reference", "Value", "Footprint", "Manufacturer", "MPN", "Datasheet"]


def export_bom_rows(cli: KiCadCli, sch: Path, out_csv: Path) -> list[dict[str, str]]:
    """Ungrouped BOM used by check_parts (one row per placed part)."""
    cli.bom(sch, out_csv, fields=PART_FIELDS, labels=PART_FIELDS)
    with out_csv.open(newline="", encoding="utf-8") as fh:
        return [dict(r) for r in csv.DictReader(fh)]


# -- reproducible output (SC-12) -------------------------------------------------
# Creation-date values in the files kicad-cli writes. Only the value is replaced.
#   Gerber X2:        %TF.CreationDate,<iso>*%      (--no-x2: G04 #@! TF.CreationDate,<iso>*)
#   Gerber (old):     G04 Created by KiCad (...) date <date>*
#   Excellon:         ; DRILL file {KiCad ...} date <date>   and   ; #@! TF.CreationDate,<iso>
#   Gerber job JSON:  "CreationDate": "<iso>"   and   "FileDate": "<iso>"
_DATE_PATTERNS = (
    re.compile(r"(TF\.CreationDate,)[^*\r\n]*"),
    re.compile(r"(G04 Created by KiCad[^\r\n]*? date )[^*\r\n]*"),
    re.compile(r"(; DRILL file \{[^}\r\n]*\} date )[^\r\n]*"),
    re.compile(r'("(?:CreationDate|FileDate)"\s*:\s*")[^"]*(")'),
)
_DATED_SUFFIXES = (".gbr", ".drl", ".gbrjob")


def normalise_dates(text: str, date_iso: str) -> str:
    """Replace every creation-date value in a Gerber, drill or job file with date_iso."""
    for pat in _DATE_PATTERNS:
        text = pat.sub(lambda m: m.group(1) + date_iso + (m.group(2) if m.lastindex and m.lastindex >= 2 else ""), text)
    return text


def normalise_file(path: Path, date_iso: str) -> bool:
    """Rewrite one file with normalised dates (byte-exact otherwise). True if changed."""
    raw = path.read_bytes()
    new = normalise_dates(raw.decode("latin-1"), date_iso).encode("latin-1")
    if new != raw:
        path.write_bytes(new)
    return new != raw


def zip_date_time(date_iso: str) -> tuple[int, ...]:
    """Zip entry timestamp taken from the approval date as written (no time zone shift)."""
    try:
        dt = datetime.fromisoformat(date_iso)
    except ValueError as exc:
        raise AcdError(f"approval date {date_iso!r} is not an ISO date") from exc
    return max(dt.year, 1980), dt.month, dt.day, dt.hour, dt.minute, dt.second - dt.second % 2


def manifest_files(release_dir: Path) -> dict[str, str]:
    out = {}
    for p in sorted(release_dir.rglob("*")):
        if p.is_file() and p.name != "manifest.json":
            out[p.relative_to(release_dir).as_posix()] = sha256_file(p)
    return out


def export_release(project: Project, cli: KiCadCli, profile: dict[str, Any],
                   rev: str, sch_hash: str, pcb_hash: str, date_iso: str) -> dict[str, Any]:
    """date_iso (the approval date) is the single date source for the whole release."""
    zdt = zip_date_time(date_iso)
    final = project.release_dir(rev)
    if final.exists():
        raise AcdError(f"release folder already exists: releases/rev-{rev} (releases are never overwritten)")
    project.releases.mkdir(exist_ok=True)
    tmp = project.releases / f".rev-{rev}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    (tmp / "gerbers").mkdir(parents=True)
    (tmp / "drill").mkdir()
    try:
        layers = get(profile, "outputs.layers") or []
        if not layers:
            raise AcdError("rule profile has no outputs.layers")
        cli.gerbers(project.pcb, tmp / "gerbers", layers=layers,
                    x2=bool(get(profile, "outputs.gerber_x2", False)), protel_ext=False)
        cli.drill(project.pcb, tmp / "drill",
                  units=get(profile, "outputs.drill_units", "mm"),
                  separate_th=bool(get(profile, "outputs.drill_split_pth_npth", True)))
        if not any((tmp / "gerbers").iterdir()):
            raise AcdError("kicad-cli produced no gerber files")
        if not any((tmp / "drill").iterdir()):
            raise AcdError("kicad-cli produced no drill files")
        for sub in ("gerbers", "drill"):
            for p in (tmp / sub).iterdir():
                if p.is_file() and p.suffix.lower() in _DATED_SUFFIXES:
                    normalise_file(p, date_iso)
        cli.bom(project.sch, tmp / "bom.csv", fields=BOM_FIELDS, labels=BOM_LABELS,
                group_by=BOM_GROUP, sort_field="Reference", exclude_dnp=True)
        if not (tmp / "bom.csv").is_file():
            raise AcdError("kicad-cli produced no BOM")
        has_pos = bool(get(profile, "outputs.centroid", False))
        if has_pos:
            cli.pos(project.pcb, tmp / "pos.csv")
        make_zip(project.root, tmp / "project.zip", date_time=zdt)
        fab = tmp / f"rev-{rev}_pcbway.zip"
        with zipfile.ZipFile(fab, "w", zipfile.ZIP_DEFLATED) as zf:
            for sub in ("gerbers", "drill"):
                for p in sorted((tmp / sub).iterdir()):
                    if p.is_file():
                        write_fixed(zf, p.name, p.read_bytes(), zdt)
        files = manifest_files(tmp)
        manifest = {
            "rev": rev,
            "created": date_iso,
            "profile": get(profile, "profile"),
            "sch_sha256": sch_hash,
            "pcb_sha256": pcb_hash,
            "files": files,
        }
        (tmp / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, final)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return {"release": f"releases/rev-{rev}", "files": sorted(files) + ["manifest.json"],
            "pos_exported": has_pos}


def verify_release(project: Project, rev: str) -> dict[str, Any]:
    rdir = project.release_dir(rev)
    mpath = rdir / "manifest.json"
    if not mpath.is_file():
        raise AcdError(f"no manifest for rev {rev}: releases/rev-{rev}/manifest.json")
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    expected: dict[str, str] = manifest["files"]
    actual = manifest_files(rdir)
    mismatched = sorted(f for f, h in expected.items() if f in actual and actual[f] != h)
    missing = sorted(f for f in expected if f not in actual)
    extra = sorted(f for f in actual if f not in expected)
    return {"rev": rev, "ok": not (mismatched or missing or extra),
            "checked": len(expected), "mismatched": mismatched,
            "missing": missing, "extra": extra}
