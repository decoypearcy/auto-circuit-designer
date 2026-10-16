"""MCP tool implementations. Plain functions returning JSON-serialisable dicts.

Errors are raised as AcdError; the MCP layer reports them as tool errors.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import yaml

from . import backups as bk
from . import gates as gt
from . import kipro, registry, reports
from .config import AcdError, Settings, validate_name
from .kicad import KiCadCli
from .profile import get, load_profile
from .release import export_bom_rows, export_release as _export_release
from .release import verify_release as _verify_release
from .store import (Project, lock_check as _lock_check, now_iso, require_unlocked,
                    restore_zip, zip_differs)

SPEC_TEMPLATE = """\
# Circuit specification for {name}
# Fill this in with Claude, then call confirm_spec once you have confirmed it in chat.
"""


def _ctx(project: str, *, writes: bool = True) -> tuple[Settings, Project, KiCadCli]:
    settings = Settings.from_env()
    proj = Project(settings, project)
    if writes:
        require_unlocked(proj)
    return settings, proj, KiCadCli(settings.kicad_cli)


def _require_draft(proj: Project) -> dict[str, Any]:
    rev = proj.revision(proj.current_rev())
    if rev["status"] != "draft":
        raise AcdError(f"revision {rev['rev']} is {rev['status']}, not draft; use new_revision to continue work")
    return rev


# -- project --------------------------------------------------------------
def init_project(name: str) -> dict[str, Any]:
    settings = Settings.from_env()
    validate_name(name)
    proj = Project(settings, name, must_exist=False)
    if proj.root.exists():
        raise AcdError(f"project folder already exists: {proj.root}")
    profile = load_profile(settings.profile_path)
    for d in (proj.root / "spec", proj.records, proj.reports, proj.backups, proj.releases):
        d.mkdir(parents=True)
    try:
        kicad_version = KiCadCli(settings.kicad_cli).version()
    except AcdError:
        kicad_version = "unknown"
    proj.spec.write_text(SPEC_TEMPLATE.format(name=name), encoding="utf-8")
    proj.write_json("records/project.json", {
        "name": name, "created": now_iso(), "kicad_version": kicad_version,
        "profile": get(profile, "profile"), "current_rev": "A"})
    proj.write_json("records/revisions.json", [gt.new_revision_obj("A", None)])
    proj.write_json("records/approvals.json", [])
    return {"project": name, "path": str(proj.root), "rev": "A", "status": "draft",
            "kicad_files_expected": [proj.pro.name, proj.sch.name, proj.pcb.name]}


def apply_profile(project: str) -> dict[str, Any]:
    settings, proj, _ = _ctx(project)
    profile = load_profile(settings.profile_path)
    proj.require_file(proj.pro, proj.pro.name)
    backup = bk.create_backup(proj, "manual")
    applied = kipro.apply_profile_files(proj, profile)
    applied["backup"] = backup["backup"]
    applied["profile"] = get(profile, "profile")
    return applied


def lock_check(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return _lock_check(proj)


# -- spec and parts ---------------------------------------------------------
def confirm_spec(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    proj.require_file(proj.spec, "spec/circuit-spec.yaml")
    try:
        data = yaml.safe_load(proj.spec.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AcdError(f"spec/circuit-spec.yaml is not valid YAML: {exc}") from exc
    if not data:
        raise AcdError("spec/circuit-spec.yaml is empty; write the spec before confirming it")
    gate = gt.set_gate(proj, "spec_confirmed", True, "spec confirmed by user")
    return {"gate": "spec_confirmed", **gate}


def register_part(lib_id: str, mpn: str, manufacturer: str, datasheet: str,
                  footprint: str, notes: str = "") -> dict[str, Any]:
    return registry.register_part(Settings.from_env(), lib_id, mpn, manufacturer,
                                  datasheet, footprint, notes)


def verify_part(lib_id: str, verified_by: str) -> dict[str, Any]:
    return registry.verify_part(Settings.from_env(), lib_id, verified_by)


def check_parts(project: str) -> dict[str, Any]:
    from .sexpr import symbol_lib_ids
    settings, proj, cli = _ctx(project)
    proj.require_file(proj.sch, proj.sch.name)
    sch_hash = proj.file_hash(proj.sch)
    rows = export_bom_rows(cli, proj.sch, proj.reports / "bom-check.csv")
    lib_ids = symbol_lib_ids(proj.sch)
    reg = registry.load(settings)
    failures: list[dict[str, str]] = []
    for row in rows:
        ref = row.get("Reference", "")
        problems = []
        lib_id = lib_ids.get(ref)
        if lib_id is None:
            problems.append("symbol not found in schematic file (cannot determine lib_id)")
        elif lib_id not in registry.STOCK_ACCEPTED:
            entry = reg.get(lib_id)
            if entry is None:
                problems.append(f"{lib_id} is not in the parts registry")
            elif not entry.get("verified"):
                problems.append(f"{lib_id} is registered but not verified")
        for field in ("Manufacturer", "MPN"):
            if not (row.get(field) or "").strip():
                problems.append(f"missing {field} field")
        if problems:
            failures.append({"reference": ref, "lib_id": lib_id or "", "problems": "; ".join(problems)})
    passed = not failures
    detail = (f"{len(rows)} BOM parts verified" if passed
              else f"{len(failures)} of {len(rows)} BOM parts failed")
    gt.set_gate(proj, "parts_verified", passed, detail, hash_value=sch_hash)
    return {"gate": "parts_verified", "passed": passed, "parts_checked": len(rows),
            "failures": failures}


# -- ERC / DRC --------------------------------------------------------------
def run_erc(project: str) -> dict[str, Any]:
    _, proj, cli = _ctx(project)
    proj.require_file(proj.sch, proj.sch.name)
    sch_hash = proj.file_hash(proj.sch)
    out = proj.reports / "erc.json"
    proj.reports.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    cli.erc(proj.sch, out)
    summary = reports.summarize_erc(reports.load_report(out))
    passed = summary["errors"] == 0
    gt.set_gate(proj, "erc_clean", passed, f"{summary['errors']} errors, {summary['warnings']} warnings",
                hash_value=sch_hash)
    return {"gate": "erc_clean", "passed": passed, **summary}


def run_drc(project: str) -> dict[str, Any]:
    _, proj, cli = _ctx(project)
    proj.require_file(proj.pcb, proj.pcb.name)
    pcb_hash = proj.file_hash(proj.pcb)
    out = proj.reports / "drc.json"
    proj.reports.mkdir(exist_ok=True)
    out.unlink(missing_ok=True)
    cli.drc(proj.pcb, out)
    summary = reports.summarize_drc(reports.load_report(out))
    drc_ok = summary["errors"] == 0
    routed = summary["unconnected"] == 0
    gt.set_gate(proj, "drc_clean", drc_ok,
                f"{summary['errors']} errors (incl. {summary['parity_errors']} parity), "
                f"{summary['warnings']} warnings", hash_value=pcb_hash)
    gt.set_gate(proj, "unrouted_zero", routed, f"{summary['unconnected']} unconnected items",
                hash_value=pcb_hash)
    return {"gates": {"drc_clean": drc_ok, "unrouted_zero": routed}, **summary}


def gate_status(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return gt.gate_status(proj)


def review_summary(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    status = gt.gate_status(proj)
    erc = drc = None
    nets: list[str] = []
    warnings = 0
    erc_path, drc_path = proj.reports / "erc.json", proj.reports / "drc.json"
    if erc_path.is_file():
        erc = reports.summarize_erc(reports.load_report(erc_path), limit=0)
        erc.pop("first_errors", None)
        warnings += erc["warnings"]
    if drc_path.is_file():
        rep = reports.load_report(drc_path)
        drc = reports.summarize_drc(rep, limit=0)
        drc.pop("first_errors", None)
        drc.pop("first_unconnected", None)
        warnings += drc["warnings"]
        nets = reports.unconnected_nets(rep)
    return {
        "project": proj.name, "rev": status["rev"], "status": status["status"],
        "gates": status["gates"], "erc": erc, "drc": drc,
        "unconnected_nets": nets, "open_warnings": warnings,
        "note": "reports are from the last run_erc/run_drc; a stale gate means the file changed since",
    }


# -- backups ----------------------------------------------------------------
def backup(project: str, stage: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    return bk.create_backup(proj, stage)


def list_backups(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return {"backups": bk.list_backups(proj)}


def restore_backup(project: str, backup_name: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    return bk.restore_backup(proj, backup_name)


# -- approval and release -----------------------------------------------------
def approve(project: str, approver: str, note: str = "") -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    if not approver or not approver.strip():
        raise AcdError("approver is required")
    rev = _require_draft(proj)
    status = gt.gate_status(proj)
    bad = [g for g in gt.GATES[:5] if not status["gates"][g]["effective"]]
    if bad:
        detail = "; ".join(f"{g}: {status['gates'][g]['detail']}" for g in bad)
        raise AcdError(f"cannot approve rev {rev['rev']}: gates not passed or stale - {detail}")
    sch_h, pcb_h = proj.file_hash(proj.sch), proj.file_hash(proj.pcb)
    entry = {"rev": rev["rev"], "approver": approver.strip(), "date": now_iso(),
             "note": note, "sch_sha256": sch_h, "pcb_sha256": pcb_h}
    approvals = proj.read_json("records/approvals.json")
    approvals.append(entry)
    proj.write_json("records/approvals.json", approvals)
    gt.set_gate(proj, "approved", True, f"approved by {entry['approver']}", hash_value=gt.combined_hash(proj))
    revs = proj.revisions()
    for r in revs:
        if r["rev"] == rev["rev"]:
            r["status"] = "approved"
    proj.write_json("records/revisions.json", revs)
    backup_info = bk.create_backup(proj, "approved")
    return {"rev": rev["rev"], "approval": entry, "backup": backup_info["backup"]}


def _approved_rev(proj: Project) -> tuple[dict[str, Any], dict[str, Any]]:
    rev = proj.revision(proj.current_rev())
    if rev["status"] != "approved":
        raise AcdError(f"revision {rev['rev']} is {rev['status']}; it must be approved first")
    approvals = [a for a in proj.read_json("records/approvals.json") if a["rev"] == rev["rev"]]
    if not approvals:
        raise AcdError(f"no approval record for rev {rev['rev']}")
    return rev, approvals[-1]


def export_release(project: str) -> dict[str, Any]:
    settings, proj, cli = _ctx(project)
    rev, appr = _approved_rev(proj)
    proj.require_file(proj.sch, proj.sch.name)
    proj.require_file(proj.pcb, proj.pcb.name)
    sch_h, pcb_h = proj.file_hash(proj.sch), proj.file_hash(proj.pcb)
    if sch_h != appr["sch_sha256"] or pcb_h != appr["pcb_sha256"]:
        changed = [n for n, a, b in (("schematic", sch_h, appr["sch_sha256"]),
                                     ("pcb", pcb_h, appr["pcb_sha256"])) if a != b]
        raise AcdError(f"{' and '.join(changed)} changed since approval of rev {rev['rev']}; "
                       "restore the approved files or start a new revision")
    profile = load_profile(settings.profile_path)
    result = _export_release(proj, cli, profile, rev["rev"], sch_h, pcb_h)
    return {"rev": rev["rev"], **result}


def verify_release(project: str, rev: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return _verify_release(proj, rev)


def new_revision(project: str, from_rev: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    src = proj.revision(from_rev)
    if src["status"] != "approved":
        raise AcdError(f"revision {from_rev} is {src['status']}; only an approved revision can be revised")
    zpath = proj.release_dir(from_rev) / "project.zip"
    if not zpath.is_file():
        raise AcdError(f"rev {from_rev} has no release (releases/rev-{from_rev}/project.zip); run export_release first")
    revs = proj.revisions()
    new = gt.next_rev(revs[-1]["rev"])
    restored = False
    pre = None
    if zip_differs(proj.root, zpath):
        pre = bk.create_backup(proj, "pre-restore", do_prune=False)["backup"]
        restore_zip(proj.root, zpath)
        bk.prune(proj, "pre-restore")
        restored = True
    for r in revs:
        if r["rev"] == from_rev:
            r["status"] = "superseded"
    revs.append(gt.new_revision_obj(new, from_rev))
    proj.write_json("records/revisions.json", revs)
    info = proj.project_info()
    info["current_rev"] = new
    proj.write_json("records/project.json", info)
    return {"rev": new, "parent": from_rev, "status": "draft",
            "working_files_restored_from_release": restored, "pre_restore_backup": pre}
