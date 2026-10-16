"""MCP tool implementations. Plain functions returning JSON-serialisable dicts.

Errors are raised as AcdError; the MCP layer reports them as tool errors.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import yaml

from . import backups as bk
from . import gates as gt
from . import boardcheck, kipro, registry, reports
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
        raise AcdError(f"rev {rev['rev']} is {rev['status']}; start a new revision (new_revision) first")
    return rev


@contextmanager
def _gate_run(proj: Project) -> Iterator[tuple[bool, Path, dict[str, Any]]]:
    """Setup for a gate check: (record, reports dir, extra result keys).

    On a draft revision the check records its gates and writes reports/. On any other
    revision (approved) it still runs, but writes into a temporary folder and records nothing,
    so the project folder does not change.
    """
    rev = proj.revision(proj.current_rev())
    if rev["status"] == "draft":
        proj.reports.mkdir(exist_ok=True)
        yield True, proj.reports, {"recorded": True}
        return
    note = f"rev {rev['rev']} is {rev['status']}: results not recorded; start a new revision to change it"
    with tempfile.TemporaryDirectory(prefix="acd-check-") as tmp:
        yield False, Path(tmp), {"recorded": False, "note": note}


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
    _require_draft(proj)
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
    _require_draft(proj)
    proj.require_file(proj.spec, "spec/circuit-spec.yaml")
    try:
        data = yaml.safe_load(proj.spec.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise AcdError(f"spec/circuit-spec.yaml is not valid YAML: {exc}") from exc
    if not data:
        raise AcdError("spec/circuit-spec.yaml is empty; write the spec before confirming it")
    backup = bk.create_gate_backup(proj, "spec")  # state going in, before the gate record changes
    gate = gt.set_gate(proj, "spec_confirmed", True, "spec confirmed by user")
    return {"gate": "spec_confirmed", **gate, "backup": backup["backup"]}


def register_part(lib_id: str, mpn: str, manufacturer: str, datasheet: str,
                  footprint: str, notes: str = "") -> dict[str, Any]:
    return registry.register_part(Settings.from_env(), lib_id, mpn, manufacturer,
                                  datasheet, footprint, notes)


def verify_part(lib_id: str, verified_by: str, mpn: str = "") -> dict[str, Any]:
    return registry.verify_part(Settings.from_env(), lib_id, verified_by, mpn)


def _no_datasheet(value: Any) -> bool:
    return str(value or "").strip().lower() in ("", "~", "-", "none")


def check_parts(project: str) -> dict[str, Any]:
    from .sexpr import symbol_lib_ids
    settings, proj, cli = _ctx(project)
    proj.require_file(proj.sch, proj.sch.name)
    sch_hash = proj.file_hash(proj.sch)
    with _gate_run(proj) as (record, out_dir, extra):
        backup = bk.create_gate_backup(proj, "pre-parts") if record else None  # before the record (and bom-check.csv) change
        rows = export_bom_rows(cli, proj.sch, out_dir / "bom-check.csv")
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
            mpn = (row.get("MPN") or "").strip()
            entry = registry.find_variant(reg, lib_id, mpn)
            if lib_id not in reg:
                problems.append(f"{lib_id} is not in the parts registry")
            elif entry is None:
                problems.append(f"{lib_id} has no registry entry for MPN {mpn}")
            else:
                if not entry.get("verified"):
                    problems.append(f"{lib_id} ({mpn}) is registered but not verified")
                if _no_datasheet(entry.get("datasheet")):
                    problems.append(f"{lib_id} has no datasheet in the parts registry")
                reg_fp = (entry.get("footprint") or "").strip()
                sch_fp = (row.get("Footprint") or "").strip()
                if reg_fp and reg_fp != sch_fp:
                    problems.append(f"footprint {sch_fp} in schematic, registry has {reg_fp}")
        for field in ("Manufacturer", "MPN"):
            if not (row.get(field) or "").strip():
                problems.append(f"missing {field} field")
        if _no_datasheet(row.get("Datasheet")):  # SC-3: every BOM part, stock passives included
            problems.append("missing Datasheet field (set it on the schematic symbol; "
                            "for a passive use the manufacturer's series datasheet URL)")
        if problems:
            failures.append({"reference": ref, "lib_id": lib_id or "", "problems": "; ".join(problems)})
    passed = not failures
    detail = (f"{len(rows)} BOM parts verified" if passed
              else f"{len(failures)} of {len(rows)} BOM parts failed")
    if record:
        gt.set_gate(proj, "parts_verified", passed, detail, hash_value=sch_hash)
    return {"gate": "parts_verified", "passed": passed, "parts_checked": len(rows),
            "failures": failures, "backup": backup["backup"] if backup else None, **extra}


# -- ERC / DRC --------------------------------------------------------------
def run_erc(project: str) -> dict[str, Any]:
    _, proj, cli = _ctx(project)
    proj.require_file(proj.sch, proj.sch.name)
    sch_hash = proj.file_hash(proj.sch)
    with _gate_run(proj) as (record, out_dir, extra):
        backup = bk.create_gate_backup(proj, "pre-erc") if record else None  # before cli.erc, so the state going in is saved
        out = out_dir / "erc.json"
        out.unlink(missing_ok=True)
        cli.erc(proj.sch, out)
        summary = reports.summarize_erc(reports.load_report(out))
    passed = summary["errors"] == 0
    if record:
        gt.set_gate(proj, "erc_clean", passed, f"{summary['errors']} errors, {summary['warnings']} warnings",
                    hash_value=sch_hash)
    return {"gate": "erc_clean", "passed": passed, **summary,
            "backup": backup["backup"] if backup else None, **extra}


def run_drc(project: str) -> dict[str, Any]:
    settings, proj, cli = _ctx(project)
    proj.require_file(proj.pcb, proj.pcb.name)
    pcb_hash = proj.file_hash(proj.pcb)
    with _gate_run(proj) as (record, out_dir, extra):
        backup = bk.create_gate_backup(proj, "pre-drc") if record else None  # before cli.drc and the gate records
        out = out_dir / "drc.json"
        out.unlink(missing_ok=True)
        cli.drc(proj.pcb, out)
        summary = reports.summarize_drc(reports.load_report(out))
        nc = _netclass_check(proj)
        spec = _load_spec(proj)
        failing, accepted, problems = _split_narrow(spec, nc["narrow_tracks"])
        nc["accepted_narrow"] = accepted
        (out_dir / "netclass.json").write_text(json.dumps(nc, indent=2), encoding="utf-8")
    summary["netclass_check"] = nc
    empty = len(nc["classes_without_nets"])
    reasons: list[str] = list(problems)
    if failing:
        total = sum(r["segments"] for r in failing.values())
        which = ", ".join(f"{net} {r['min_width']:g} mm vs class {r['class_width']:g} mm"
                          for net, r in failing.items())
        reasons.append(f"{total} narrow tracks ({which})")
    reasons += _class_current_failures(settings, proj, spec)
    summary["gate_failures"] = reasons
    drc_ok = summary["errors"] == 0 and empty == 0 and not reasons
    routed = summary["unconnected"] == 0
    detail = (f"{summary['errors']} errors (incl. {summary['parity_errors']} parity), "
              f"{empty} net classes without nets, {summary['warnings']} warnings")
    if reasons:
        detail += "; " + "; ".join(reasons)
    if accepted:
        detail += "; accepted narrow: " + ", ".join(
            f"{net} {r['min_width']:g} mm ({r['reason']})" for net, r in accepted.items())
    if record:
        gt.set_gate(proj, "drc_clean", drc_ok, detail, hash_value=pcb_hash)
        gt.set_gate(proj, "unrouted_zero", routed, f"{summary['unconnected']} unconnected items",
                    hash_value=pcb_hash)
    return {"gates": {"drc_clean": drc_ok, "unrouted_zero": routed}, **summary,
            "backup": backup["backup"] if backup else None, **extra}


def _load_spec(proj: Project) -> dict[str, Any]:
    if not proj.spec.is_file():
        return {}
    try:
        data = yaml.safe_load(proj.spec.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise AcdError(f"spec/circuit-spec.yaml is not valid YAML: {exc}") from exc
    return data if isinstance(data, dict) else {}


def _split_narrow(spec: dict[str, Any], narrow: dict[str, dict[str, Any]]
                  ) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Split narrow tracks into failing and accepted ones.

    A narrow net is accepted only if the spec lists it under accepted_narrow with a reason and
    a min_width_mm the board does not go below. Editing the spec makes spec_confirmed stale,
    so every accepted entry needs Andrew to re-confirm the spec.
    """
    entries: dict[str, dict[str, Any]] = {}
    problems: list[str] = []
    for e in spec.get("accepted_narrow") or []:
        if not isinstance(e, dict) or not str(e.get("net") or "").strip():
            problems.append("accepted_narrow entry without a net")
            continue
        entries[str(e["net"]).strip()] = e
    failing: dict[str, Any] = {}
    accepted: dict[str, Any] = {}
    for net, row in narrow.items():
        e = entries.get(net)
        if e is None:
            failing[net] = row
            continue
        reason = str(e.get("reason") or "").strip()
        try:
            floor = float(e.get("min_width_mm"))
        except (TypeError, ValueError):
            problems.append(f"accepted_narrow {net}: min_width_mm must be a number")
            failing[net] = row
            continue
        if not reason:
            problems.append(f"accepted_narrow {net}: a reason is required")
            failing[net] = row
        elif row["min_width"] + boardcheck.TOL < floor:
            failing[net] = row  # narrower than what was accepted
        else:
            accepted[net] = {**row, "accepted_min_mm": floor, "reason": reason}
    return failing, accepted, problems


def _class_current_failures(settings: Settings, proj: Project,
                            spec: dict[str, Any] | None = None) -> list[str]:
    """Spec net classes (name, nets, current_a) whose .kicad_pro width is below the
    width the profile table requires for that current (SC-6)."""
    if not proj.pro.is_file():
        return []
    data = _load_spec(proj) if spec is None else spec
    classes = [c for c in (data.get("net_classes") or [])
               if isinstance(c, dict) and c.get("current_a") not in (None, "")]
    if not classes:
        return []
    table = get(load_profile(settings.profile_path), "tracks.power_width_table")
    if not table:
        raise AcdError("rule profile has no tracks.power_width_table (needed for net class current_a)")
    widths = boardcheck.load_netclasses(proj.pro)[0]
    return boardcheck.class_width_failures(classes, widths, table)


def _netclass_check(proj: Project) -> dict[str, Any]:
    """KiCad DRC does not check net class widths; record them for the review (T-15)."""
    if not proj.pro.is_file():
        nc: dict[str, Any] = {"classes": {}, "classes_without_nets": [], "narrow_tracks": {}}
    else:
        nc = boardcheck.netclass_check(proj.pro, proj.pcb)
    return nc


def fix_bom_flags(project: str) -> dict[str, Any]:
    """Set in_bom no on placed symbols whose library symbol is not a BOM part
    (mounting holes, fiducials). The kicad server writes in_bom yes on every symbol."""
    _, proj, _ = _ctx(project)
    _require_draft(proj)
    proj.require_file(proj.sch, proj.sch.name)
    text = proj.sch.read_text(encoding="utf-8")
    new, changed = boardcheck.fix_bom_flags_text(text)
    backup = None
    if changed:
        backup = bk.create_backup(proj, "manual")["backup"]
        proj.sch.write_text(new, encoding="utf-8", newline="")
    return {"changed": changed, "backup": backup}


def gate_status(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return gt.gate_status(proj)


def review_summary(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    status = gt.gate_status(proj)
    erc = drc = None
    nets: list[str] = []
    silk: list[dict[str, Any]] = []
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
        silk = reports.silk_warnings(rep)
    nc_path = proj.reports / "netclass.json"
    netclass = None
    if nc_path.is_file():
        netclass = json.loads(nc_path.read_text(encoding="utf-8"))
    return {
        "project": proj.name, "rev": status["rev"], "status": status["status"],
        "gates": status["gates"], "erc": erc, "drc": drc,
        "unconnected_nets": nets, "open_warnings": warnings, "silk_warnings": silk,
        "netclass": netclass,
        "note": "reports are from the last run_erc/run_drc; a stale gate means the file changed since",
    }


# -- backups ----------------------------------------------------------------
def backup(project: str, stage: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    return bk.create_backup(proj, stage)


def list_backups(project: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return {"backups": bk.list_backups(proj)}


def restore_backup(project: str, backup_name: str, dry_run: bool = False) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=not dry_run)
    return bk.restore_backup(proj, backup_name, dry_run=dry_run)


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
    backup_info = bk.create_backup(proj, "approved")  # state going in, before any approval record
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
    result = _export_release(proj, cli, profile, rev["rev"], sch_h, pcb_h, appr["date"])
    return {"rev": rev["rev"], **result}


def verify_release(project: str, rev: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project, writes=False)
    return _verify_release(proj, rev)


def new_revision(project: str, from_rev: str) -> dict[str, Any]:
    _, proj, _ = _ctx(project)
    src = proj.revision(from_rev)
    if src["status"] != "approved":
        raise AcdError(f"revision {from_rev} is {src['status']}; only an approved revision can be revised")
    if src.get("superseded_by"):
        raise AcdError(f"revision {from_rev} is already superseded by rev {src['superseded_by']}; "
                       "only an approved revision that is not superseded can be revised")
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
            r["superseded_by"] = new  # the parent's status stays "approved"
    revs.append(gt.new_revision_obj(new, from_rev))
    proj.write_json("records/revisions.json", revs)
    info = proj.project_info()
    info["current_rev"] = new
    proj.write_json("records/project.json", info)
    return {"rev": new, "parent": from_rev, "status": "draft",
            "working_files_restored_from_release": restored, "pre_restore_backup": pre}
