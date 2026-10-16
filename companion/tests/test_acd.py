import json
import os
import zipfile
from pathlib import Path

import pytest

from acd import backups as bk
from acd import gates as gt
from acd import kipro, tools
from acd.config import DEFAULT_PROFILE, AcdError
from acd.profile import load_profile
from acd.sexpr import symbol_lib_ids
from acd.store import Project, sha256_file

from conftest import calls, pass_all_gates, register_and_verify, sym, write_sch


# --------------------------------------------------------------- layout
def test_init_project_creates_layout_and_records(env):
    res = tools.init_project("widget")
    root = env["projects"] / "widget"
    assert res["path"] == str(root) and res["rev"] == "A"
    for d in ("spec", "records", "reports", "backups", "releases"):
        assert (root / d).is_dir()
    assert (root / "spec" / "circuit-spec.yaml").is_file()
    info = json.loads((root / "records" / "project.json").read_text())
    assert info["name"] == "widget" and info["current_rev"] == "A"
    assert info["profile"] == "pcbway-standard-2layer" and info["kicad_version"] == "10.0.1"
    revs = json.loads((root / "records" / "revisions.json").read_text())
    assert len(revs) == 1 and revs[0]["rev"] == "A" and revs[0]["status"] == "draft"
    assert revs[0]["parent"] is None
    assert json.loads((root / "records" / "approvals.json").read_text()) == []


def test_init_project_refuses_existing_and_bad_names(env):
    tools.init_project("widget")
    with pytest.raises(AcdError, match="already exists"):
        tools.init_project("widget")
    for bad in ("../evil", "a/b", "", ".hidden", "a b"):
        with pytest.raises(AcdError, match="invalid project name"):
            tools.init_project(bad)


def test_missing_env_is_a_clear_error(monkeypatch):
    monkeypatch.delenv("ACD_PROJECTS_ROOT", raising=False)
    with pytest.raises(AcdError, match="ACD_PROJECTS_ROOT"):
        tools.init_project("x")


# ------------------------------------------------------------ lock check
def test_lock_check_and_write_refusals(proj):
    assert tools.lock_check("demo") == {"locked": False, "files": []}
    (proj / "~demo.kicad_pcb.lck").write_text("{}")
    res = tools.lock_check("demo")
    assert res["locked"] and res["files"] == ["~demo.kicad_pcb.lck"]
    for call in (lambda: tools.apply_profile("demo"), lambda: tools.confirm_spec("demo"),
                 lambda: tools.check_parts("demo"), lambda: tools.run_erc("demo"),
                 lambda: tools.run_drc("demo"), lambda: tools.backup("demo", "manual"),
                 lambda: tools.approve("demo", "A", ""), lambda: tools.export_release("demo"),
                 lambda: tools.new_revision("demo", "A"),
                 lambda: tools.restore_backup("demo", "20260101-0000_A_manual.zip")):
        with pytest.raises(AcdError, match="has this project open|open"):
            call()
    # read-only tools still work
    assert tools.gate_status("demo")["rev"] == "A"
    assert tools.list_backups("demo") == {"backups": []}
    assert tools.review_summary("demo")["rev"] == "A"


def test_locks_in_backups_and_kicad_backup_dirs_are_ignored(proj):
    (proj / "demo-backups").mkdir()
    (proj / "demo-backups" / "~x.lck").write_text("")
    (proj / "backups" / "~y.lck").write_text("")
    assert tools.lock_check("demo")["locked"] is False


# ---------------------------------------------------------- apply_profile
def test_apply_profile_writes_pro_and_dru(proj):
    res = tools.apply_profile("demo")
    pro = json.loads((proj / "demo.kicad_pro").read_text())
    rules = pro["board"]["design_settings"]["rules"]
    assert rules["min_copper_edge_clearance"] == 0.3
    assert rules["min_track_width"] == 0.15 and rules["min_via_annular_width"] == 0.15
    assert rules["min_clearance"] == 0.15 and rules["keep_me"] == 1  # existing keys kept
    assert pro["board"]["design_settings"]["defaults"]["silk_text_size_h"] == 1.0
    default = pro["net_settings"]["classes"][0]
    assert default["track_width"] == 0.25 and default["clearance"] == 0.2
    assert default["via_diameter"] == 0.6 and default["via_drill"] == 0.3
    assert default["wire_width"] == 6 and pro["net_settings"]["meta"] == {"version": 3}
    dru = (proj / "demo.kicad_dru").read_text()
    assert dru.startswith("(version 1)")
    assert "edge_clearance (min 0.3mm)" in dru and "annular_width (min 0.15mm)" in dru
    assert "text_height (min 0.8mm)" in dru
    assert dru.count("(") == dru.count(")")
    # backup taken first, and it holds the pre-profile .kicad_pro
    with zipfile.ZipFile(proj / "backups" / res["backup"]) as zf:
        assert json.loads(zf.read("demo.kicad_pro"))["board"]["design_settings"]["rules"]["min_clearance"] == 0.0


def test_apply_profile_creates_default_netclass_when_missing(proj):
    (proj / "demo.kicad_pro").write_text("{}")
    tools.apply_profile("demo")
    pro = json.loads((proj / "demo.kicad_pro").read_text())
    assert pro["net_settings"]["classes"][0]["name"] == "Default"


def test_apply_profile_needs_kicad_pro(env):
    tools.init_project("bare")
    with pytest.raises(AcdError, match="kicad_pro"):
        tools.apply_profile("bare")


# ------------------------------------------------- spec and parts registry
def test_confirm_spec_requires_content(env):
    tools.init_project("blank")
    with pytest.raises(AcdError, match="empty"):
        tools.confirm_spec("blank")


def test_register_verify_and_check_parts(proj, env):
    res = tools.check_parts("demo")
    assert not res["passed"]
    assert [f["reference"] for f in res["failures"]] == ["U1"]  # R1 stock, TP1 excluded
    assert "not in the parts registry" in res["failures"][0]["problems"]

    tools.register_part("acd:LM1117-3.3", "LM1117MPX-3.3", "TI", "http://ds", "SOT-223", "n")
    reg = json.loads((env["library"] / "registry.json").read_text())
    assert reg["acd:LM1117-3.3"]["verified"] is False
    assert (env["library"] / "footprints" / "acd.pretty").is_dir()
    res = tools.check_parts("demo")
    assert not res["passed"] and "not verified" in res["failures"][0]["problems"]

    tools.verify_part("acd:LM1117-3.3", "Andrew")
    reg = json.loads((env["library"] / "registry.json").read_text())
    assert reg["acd:LM1117-3.3"]["verified"] and reg["acd:LM1117-3.3"]["verified_by"] == "Andrew"
    assert tools.check_parts("demo")["passed"]
    assert gt.gate_status(Project(tools.Settings.from_env(), "demo"))["gates"]["parts_verified"]["effective"]

    # re-registering resets verification
    tools.register_part("acd:LM1117-3.3", "LM1117MPX-3.3", "TI", "http://ds", "SOT-223", "changed")
    assert not tools.check_parts("demo")["passed"]


def test_check_parts_requires_manufacturer_and_mpn(proj):
    register_and_verify()
    write_sch(proj / "demo.kicad_sch", sym("U1", mfr=None, mpn=""),
              sym("R1", "Device:R", "10k", "R_0805", "Yageo", "RC"))
    res = tools.check_parts("demo")
    assert not res["passed"]
    assert "missing Manufacturer" in res["failures"][0]["problems"]
    assert "missing MPN" in res["failures"][0]["problems"]


def test_verify_part_unknown_or_anonymous(env):
    with pytest.raises(AcdError, match="not in the registry"):
        tools.verify_part("acd:nope", "Andrew")
    tools.register_part("acd:x", "m", "mf", "", "fp")
    with pytest.raises(AcdError, match="verified_by"):
        tools.verify_part("acd:x", " ")


def test_sexpr_lib_ids_follow_sub_sheets(tmp_path):
    (tmp_path / "sub.kicad_sch").write_text(
        '(kicad_sch (symbol (lib_id "acd:SUB") (property "Reference" "U9")))')
    (tmp_path / "top.kicad_sch").write_text(
        '(kicad_sch (lib_symbols (symbol "acd:X")) (symbol (lib_id "Device:R") (property "Reference" "R1"))'
        ' (sheet (property "Sheetfile" "sub.kicad_sch")))')
    assert symbol_lib_ids(tmp_path / "top.kicad_sch") == {"R1": "Device:R", "U9": "acd:SUB"}


# ------------------------------------------------------------- ERC / DRC
def test_erc_gate_and_error_cap(proj, monkeypatch):
    monkeypatch.setenv("FAKE_ERC_ERRORS", "25")
    monkeypatch.setenv("FAKE_ERC_WARNINGS", "2")
    res = tools.run_erc("demo")
    assert not res["passed"] and res["errors"] == 25 and res["warnings"] == 2
    assert len(res["first_errors"]) == 20
    assert (proj / "reports" / "erc.json").is_file()
    monkeypatch.setenv("FAKE_ERC_ERRORS", "0")
    assert tools.run_erc("demo")["passed"]


def test_drc_sets_gates_4_and_5_with_parity_and_unconnected(proj, monkeypatch):
    monkeypatch.setenv("FAKE_DRC_ERRORS", "1")
    monkeypatch.setenv("FAKE_PARITY_ERRORS", "2")
    monkeypatch.setenv("FAKE_UNCONNECTED", "3")
    res = tools.run_drc("demo")
    assert res["errors"] == 3 and res["parity_errors"] == 2 and res["unconnected"] == 3
    assert res["gates"] == {"drc_clean": False, "unrouted_zero": False}
    flags = [c for c in calls({"log": Path(os.environ["FAKE_LOG"])}) if c[:2] == ["pcb", "drc"]][-1]
    assert "--schematic-parity" in flags and "json" in flags
    monkeypatch.setenv("FAKE_DRC_ERRORS", "0")
    monkeypatch.setenv("FAKE_PARITY_ERRORS", "0")
    res = tools.run_drc("demo")
    assert res["gates"] == {"drc_clean": True, "unrouted_zero": False}
    summary = tools.review_summary("demo")
    assert summary["unconnected_nets"] == ["NET0", "NET1", "NET2"]
    assert summary["drc"]["unconnected"] == 3


def test_kicad_failure_is_reported(proj, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(AcdError, match="exited with code 3"):
        tools.run_erc("demo")


def test_missing_kicad_cli(proj, monkeypatch):
    monkeypatch.setenv("ACD_KICAD_CLI", str(proj / "nope.exe"))
    with pytest.raises(AcdError, match="not found"):
        tools.run_erc("demo")


# -------------------------------------------------------- gate staleness
def test_gate_goes_stale_after_file_edit(proj):
    tools.run_erc("demo")
    tools.run_drc("demo")
    st = tools.gate_status("demo")["gates"]
    assert st["erc_clean"]["effective"] and st["drc_clean"]["effective"]
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; edited)\n")
    st = tools.gate_status("demo")["gates"]
    assert st["drc_clean"]["passed"] and st["drc_clean"]["stale"] and not st["drc_clean"]["effective"]
    assert st["unrouted_zero"]["stale"]
    assert st["erc_clean"]["effective"]  # schematic untouched
    (proj / "demo.kicad_sch").write_text((proj / "demo.kicad_sch").read_text() + "\n")
    assert not tools.gate_status("demo")["gates"]["erc_clean"]["effective"]
    assert gt.next_rev("A") == "B" and gt.next_rev("Z") == "AA" and gt.next_rev("AZ") == "BA"


# --------------------------------------------------------------- approval
def test_approve_refuses_failing_and_stale_gates(proj, monkeypatch):
    with pytest.raises(AcdError, match="spec_confirmed"):
        tools.approve("demo", "Andrew", "")
    pass_all_gates()
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; moved a part)\n")
    with pytest.raises(AcdError, match="stale"):
        tools.approve("demo", "Andrew", "")
    monkeypatch.setenv("FAKE_UNCONNECTED", "1")
    tools.run_drc("demo")
    with pytest.raises(AcdError, match="unrouted_zero"):
        tools.approve("demo", "Andrew", "")
    assert json.loads((proj / "records" / "approvals.json").read_text()) == []


def test_approve_records_hashes_status_and_backup(approved):
    proj = approved
    appr = json.loads((proj / "records" / "approvals.json").read_text())
    assert len(appr) == 1 and appr[0]["rev"] == "A" and appr[0]["approver"] == "Andrew"
    assert appr[0]["sch_sha256"] == sha256_file(proj / "demo.kicad_sch")
    assert appr[0]["pcb_sha256"] == sha256_file(proj / "demo.kicad_pcb")
    revs = json.loads((proj / "records" / "revisions.json").read_text())
    assert revs[0]["status"] == "approved" and revs[0]["gates"]["approved"]["passed"]
    assert any(b["stage"] == "approved" for b in tools.list_backups("demo")["backups"])
    assert tools.gate_status("demo")["gates"]["approved"]["effective"]
    with pytest.raises(AcdError, match="not draft"):
        tools.approve("demo", "Andrew", "again")
    with pytest.raises(AcdError, match="approver"):
        tools.approve("demo", " ", "")


# ---------------------------------------------------------------- release
def test_export_refused_without_approval(proj):
    pass_all_gates()
    with pytest.raises(AcdError, match="must be approved"):
        tools.export_release("demo")
    assert not (proj / "releases" / "rev-A").exists()


def test_export_refused_when_files_changed_after_approval(approved):
    (approved / "demo.kicad_pcb").write_text("(kicad_pcb ; sneaky edit)\n")
    with pytest.raises(AcdError, match="changed since approval"):
        tools.export_release("demo")


def test_export_release_contents_and_flags(approved, env):
    res = tools.export_release("demo")
    rel = approved / "releases" / "rev-A"
    assert res["rev"] == "A" and res["pos_exported"] is False
    gerbers = sorted(p.name for p in (rel / "gerbers").iterdir())
    assert len(gerbers) == 9 and all(g.endswith(".gbr") for g in gerbers)
    assert any("F_Silkscreen" in g for g in gerbers) and any("Edge_Cuts" in g for g in gerbers)
    assert sorted(p.name for p in (rel / "drill").iterdir()) == ["demo-NPTH.drl", "demo-PTH.drl"]
    assert not (rel / "pos.csv").exists()
    bom = (rel / "bom.csv").read_text().splitlines()
    assert bom[0] == '"Reference","Value","Footprint","Manufacturer","MPN","Qty"'
    assert len(bom) == 3 and "TP1" not in "".join(bom)
    assert (rel / "project.zip").is_file() and (rel / "rev-A_pcbway.zip").is_file()
    with zipfile.ZipFile(rel / "rev-A_pcbway.zip") as zf:
        names = zf.namelist()
        assert len(names) == 11 and not any("/" in n for n in names)
        assert not any(n.endswith(".csv") for n in names)
    with zipfile.ZipFile(rel / "project.zip") as zf:
        assert "demo.kicad_pcb" in zf.namelist()
        assert not any(n.startswith(("backups/", "releases/")) for n in zf.namelist())
    manifest = json.loads((rel / "manifest.json").read_text())
    assert "manifest.json" not in manifest["files"] and "bom.csv" in manifest["files"]
    assert manifest["files"]["bom.csv"] == sha256_file(rel / "bom.csv")
    assert tools.verify_release("demo", "A")["ok"]
    # kicad-cli flags
    log = calls(env)
    gerb = next(c for c in log if c[:3] == ["pcb", "export", "gerbers"])
    assert "--no-x2" in gerb and "--no-protel-ext" in gerb and "F.Silkscreen" in gerb[gerb.index("-l") + 1]
    drill = next(c for c in log if c[:3] == ["pcb", "export", "drill"])
    assert drill[drill.index("--format") + 1] == "excellon"
    assert drill[drill.index("--drill-origin") + 1] == "absolute"
    assert drill[drill.index("--excellon-units") + 1] == "mm" and "--excellon-separate-th" in drill
    bomcall = next(c for c in log if c[:3] == ["sch", "export", "bom"] and "--group-by" in c)
    assert bomcall[bomcall.index("--group-by") + 1] == "Value,Footprint"
    # a second export is refused and leaves the release untouched
    before = (rel / "manifest.json").read_text()
    with pytest.raises(AcdError, match="already exists"):
        tools.export_release("demo")
    assert (rel / "manifest.json").read_text() == before


def test_export_release_with_centroid_profile(approved, env, tmp_path, monkeypatch):
    import yaml
    prof = yaml.safe_load(tools.Settings.from_env().profile_path.read_text(encoding="utf-8"))
    prof["outputs"]["centroid"] = True
    prof["outputs"]["gerber_x2"] = True
    p = tmp_path / "prof.yaml"
    p.write_text(yaml.safe_dump(prof))
    monkeypatch.setenv("ACD_PROFILE", str(p))
    res = tools.export_release("demo")
    assert res["pos_exported"] and (approved / "releases" / "rev-A" / "pos.csv").is_file()
    gerb = next(c for c in calls(env) if c[:3] == ["pcb", "export", "gerbers"])
    assert "--no-x2" not in gerb


def test_failed_export_leaves_no_partial_release(approved, monkeypatch):
    monkeypatch.setenv("FAKE_FAIL", "1")
    with pytest.raises(AcdError):
        tools.export_release("demo")
    assert list((approved / "releases").iterdir()) == []


def test_verify_release_detects_tampering(approved):
    tools.export_release("demo")
    rel = approved / "releases" / "rev-A"
    assert tools.verify_release("demo", "A")["ok"]
    g = next((rel / "gerbers").iterdir())
    g.write_text("tampered")
    (rel / "drill" / "demo-PTH.drl").unlink()
    (rel / "notes.txt").write_text("extra")
    res = tools.verify_release("demo", "A")
    assert not res["ok"]
    assert f"gerbers/{g.name}" in res["mismatched"]
    assert "drill/demo-PTH.drl" in res["missing"] and "notes.txt" in res["extra"]
    with pytest.raises(AcdError, match="no manifest"):
        tools.verify_release("demo", "Z")


# ---------------------------------------------------------------- backups
def test_backup_prune_keeps_n_per_stage(proj, monkeypatch):
    monkeypatch.setenv("ACD_BACKUP_KEEP", "3")
    names = [tools.backup("demo", "manual")["backup"] for _ in range(5)]
    assert len(set(names)) == 5  # same-minute collisions get distinct names
    other = tools.backup("demo", "routed")["backup"]
    listed = tools.list_backups("demo")["backups"]
    manual = [b["name"] for b in listed if b["stage"] == "manual"]
    assert len(manual) == 3 and manual == sorted(names)[-3:][::-1]  # newest first, oldest pruned
    assert other in [b["name"] for b in listed]
    assert len(list((proj / "backups").iterdir())) == 4
    with pytest.raises(AcdError, match="unknown stage"):
        tools.backup("demo", "whenever")


def test_backup_excludes_locks_backups_releases_and_kicad_backup_dirs(proj):
    (proj / "~demo.kicad_sch.lck").write_text("")
    (proj / "demo-backups").mkdir()
    (proj / "demo-backups" / "old.zip").write_text("x")
    (proj / "releases" / "rev-Q").mkdir()
    (proj / "releases" / "rev-Q" / "f.txt").write_text("x")
    # lock refuses; remove it, run, then check contents
    with pytest.raises(AcdError):
        tools.backup("demo", "manual")
    (proj / "~demo.kicad_sch.lck").unlink()
    name = tools.backup("demo", "manual")["backup"]
    tools.backup("demo", "schematic")
    with zipfile.ZipFile(proj / "backups" / name) as zf:
        names = zf.namelist()
    assert "demo.kicad_sch" in names and "records/project.json" in names
    assert not any(n.startswith(("backups/", "releases/", "demo-backups/")) for n in names)


def test_restore_makes_pre_restore_backup_and_keeps_records(proj):
    original = (proj / "demo.kicad_pcb").read_text()
    name = tools.backup("demo", "placement")["backup"]
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; later work)\n")
    (proj / "extra.txt").write_text("created after backup")
    tools.confirm_spec("demo")
    res = tools.restore_backup("demo", name)
    assert (proj / "demo.kicad_pcb").read_text() == original
    assert not (proj / "extra.txt").exists()
    pre = res["pre_restore_backup"]
    assert pre.endswith("_A_pre-restore.zip")
    with zipfile.ZipFile(proj / "backups" / pre) as zf:
        assert zf.read("demo.kicad_pcb") == b"(kicad_pcb ; later work)\n"
        assert zf.read("extra.txt") == b"created after backup"
    # records survive the restore, and the gate (spec hash unchanged) is still set
    revs = json.loads((proj / "records" / "revisions.json").read_text())
    assert "spec_confirmed" in revs[0]["gates"]
    assert (proj / "backups" / name).is_file()
    with pytest.raises(AcdError, match="invalid backup name"):
        tools.restore_backup("demo", "../records/project.json")
    with pytest.raises(AcdError, match="not found"):
        tools.restore_backup("demo", "20200101-0000_A_manual.zip")


def test_restore_turns_gates_stale(proj):
    name = tools.backup("demo", "manual")["backup"]
    tools.run_drc("demo")
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; v2)\n")
    tools.run_drc("demo")
    assert tools.gate_status("demo")["gates"]["drc_clean"]["effective"]
    tools.restore_backup("demo", name)
    assert tools.gate_status("demo")["gates"]["drc_clean"]["stale"]


def test_restore_rejects_zip_slip(proj):
    evil = proj / "backups" / "20260101-0000_A_manual.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(AcdError, match="unsafe"):
        tools.restore_backup("demo", evil.name)
    assert not (proj.parent / "escaped.txt").exists()


# ---------------------------------------------------------- new revision
def test_new_revision_requires_approved(proj):
    with pytest.raises(AcdError, match="only an approved revision"):
        tools.new_revision("demo", "A")


def test_new_revision_keeps_old_release_immutable(approved):
    proj = approved
    tools.export_release("demo")
    rel = proj / "releases" / "rev-A"
    snapshot = {p.relative_to(rel).as_posix(): sha256_file(p) for p in rel.rglob("*") if p.is_file()}
    approved_pcb = (proj / "demo.kicad_pcb").read_text()
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; work towards rev B)\n")

    res = tools.new_revision("demo", "A")
    assert res["rev"] == "B" and res["parent"] == "A" and res["working_files_restored_from_release"]
    assert (proj / "demo.kicad_pcb").read_text() == approved_pcb  # restored from release project.zip
    assert res["pre_restore_backup"].endswith("_A_pre-restore.zip")

    revs = json.loads((proj / "records" / "revisions.json").read_text())
    assert [r["rev"] for r in revs] == ["A", "B"]
    assert revs[0]["status"] == "superseded" and revs[1]["status"] == "draft" and revs[1]["parent"] == "A"
    assert revs[1]["gates"] == {}
    assert json.loads((proj / "records" / "project.json").read_text())["current_rev"] == "B"
    assert tools.gate_status("demo")["rev"] == "B"
    assert len(json.loads((proj / "records" / "approvals.json").read_text())) == 1

    # rev B work does not touch rev A's release
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; rev B routing)\n")
    pass_gates_b()
    tools.approve("demo", "Andrew", "rev B")
    tools.export_release("demo")
    after = {p.relative_to(rel).as_posix(): sha256_file(p) for p in rel.rglob("*") if p.is_file()}
    assert after == snapshot
    assert tools.verify_release("demo", "A")["ok"] and tools.verify_release("demo", "B")["ok"]
    with pytest.raises(AcdError, match="only an approved revision"):
        tools.new_revision("demo", "A")  # superseded


def pass_gates_b():
    tools.confirm_spec("demo")
    assert tools.check_parts("demo")["passed"]
    assert tools.run_erc("demo")["passed"]
    tools.run_drc("demo")


def test_new_revision_without_changes_does_not_restore(approved):
    tools.export_release("demo")
    res = tools.new_revision("demo", "A")
    assert res["working_files_restored_from_release"] is False and res["pre_restore_backup"] is None


# ----------------------------------------------------------- misc / wiring
def test_kicad_pro_builders_use_profile_values():
    profile = load_profile(DEFAULT_PROFILE)
    r = kipro.build_rules(profile)
    assert r["solder_mask_min_width"] == 0.1 and r["min_through_hole_diameter"] == 0.3
    assert kipro.build_default_netclass(profile)["via_drill"] == 0.3


def test_server_registers_all_tools(env):
    import asyncio
    from acd.server import TOOL_NAMES, build_server
    server = build_server()
    listed = asyncio.run(server.list_tools())
    listed = listed.tools if hasattr(listed, "tools") else listed
    assert sorted(t.name for t in listed) == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 18
