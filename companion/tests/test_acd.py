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

    tools.register_part("acd:LM1117-3.3", "LM1117MPX-3.3", "TI", "http://ds", "Package_TO_SOT_SMD:SOT-223", "n")
    reg = json.loads((env["library"] / "registry.json").read_text())
    assert reg["acd:LM1117-3.3"]["variants"][0]["verified"] is False
    assert (env["library"] / "footprints" / "acd.pretty").is_dir()
    res = tools.check_parts("demo")
    assert not res["passed"] and "not verified" in res["failures"][0]["problems"]

    tools.verify_part("acd:LM1117-3.3", "Andrew")
    reg = json.loads((env["library"] / "registry.json").read_text())
    variant = reg["acd:LM1117-3.3"]["variants"][0]
    assert variant["verified"] and variant["verified_by"] == "Andrew"
    assert tools.check_parts("demo")["passed"]
    assert gt.gate_status(Project(tools.Settings.from_env(), "demo"))["gates"]["parts_verified"]["effective"]

    # re-registering resets verification
    tools.register_part("acd:LM1117-3.3", "LM1117MPX-3.3", "TI", "http://ds", "Package_TO_SOT_SMD:SOT-223", "changed")
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
    assert summary["silk_warnings"] == []


def test_silk_warnings_listed_but_do_not_block_drc(proj, monkeypatch):
    monkeypatch.setenv("FAKE_DRC_SILK", "2")
    res = tools.run_drc("demo")
    assert res["gates"]["drc_clean"] and res["warnings"] == 2
    assert res["warning_types"] == {"silk_edge_clearance": 2}
    summary = tools.review_summary("demo")
    assert [w["type"] for w in summary["silk_warnings"]] == ["silk_edge_clearance"] * 2
    assert summary["drc"]["warning_types"] == {"silk_edge_clearance": 2}


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
    with pytest.raises(AcdError, match="start a new revision"):
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
    assert revs[0]["status"] == "approved" and revs[0]["superseded_by"] == "B"  # parent record not rewritten
    assert revs[1]["status"] == "draft" and revs[1]["parent"] == "A" and "superseded_by" not in revs[1]
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
    with pytest.raises(AcdError, match="already superseded by rev B"):
        tools.new_revision("demo", "A")


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
    assert "solder_mask_min_width" not in r and r["min_through_hole_diameter"] == 0.3
    assert kipro.build_default_netclass(profile)["via_drill"] == 0.3


def test_server_registers_all_tools(env):
    import asyncio
    from acd.server import TOOL_NAMES, build_server
    server = build_server()
    listed = asyncio.run(server.list_tools())
    listed = listed.tools if hasattr(listed, "tools") else listed
    assert sorted(t.name for t in listed) == sorted(TOOL_NAMES)
    assert len(TOOL_NAMES) == 19


# ------------------------------------------------- T-15 board checks
PCB_TRACKS = """(kicad_pcb (version 20250114)
  (net 0 "")
  (net 1 "VIN")
  (segment (start 0 0) (end 1 0) (width 0.5) (layer "F.Cu") (net "VIN") (uuid "a"))
  (segment (start 1 0) (end 2 0) (width 0.375) (layer "F.Cu") (net "VIN") (uuid "b"))
  (segment (start 0 1) (end 1 1) (width 0.25) (layer "F.Cu") (net 1) (uuid "c"))
  (segment (start 0 2) (end 1 2) (width 0.25) (layer "B.Cu") (net "LED_A") (uuid "d"))
)
"""


PCB_WIDE = """(kicad_pcb (version 20250114)
  (net 0 "")
  (net 1 "VIN")
  (segment (start 0 0) (end 1 0) (width 1.0) (layer "F.Cu") (net "VIN") (uuid "a"))
)
"""


def _spec_classes(proj, current_a):
    (proj / "spec" / "circuit-spec.yaml").write_text(
        "name: demo\nnet_classes:\n  - name: Power\n    nets: [VIN]\n"
        f"    current_a: {current_a}\n", encoding="utf-8")


def _power_board(proj, class_width, track_width):
    pro = json.loads((proj / "demo.kicad_pro").read_text())
    pro["net_settings"]["classes"] = [{"name": "Default", "track_width": 0.25},
                                      {"name": "Power", "track_width": class_width}]
    pro["net_settings"]["netclass_assignments"] = {"VIN": ["Power"]}
    (proj / "demo.kicad_pro").write_text(json.dumps(pro))
    (proj / "demo.kicad_pcb").write_text(PCB_WIDE.replace("(width 1.0)", f"(width {track_width})"))


def test_net_class_width_must_meet_spec_current(proj):
    _spec_classes(proj, 2.0)  # 2 A needs 1.0 mm
    _power_board(proj, class_width=0.5, track_width=0.5)
    res = tools.run_drc("demo")
    assert res["gates"]["drc_clean"] is False and res["netclass_check"]["narrow_tracks"] == {}
    detail = tools.gate_status("demo")["gates"]["drc_clean"]["detail"]
    assert "class Power 0.5 mm vs 1 mm needed for 2 A" in detail
    _power_board(proj, class_width=1.0, track_width=1.0)
    assert tools.run_drc("demo")["gates"]["drc_clean"] is True


def test_net_class_current_above_table_asks_andrew(proj):
    _spec_classes(proj, 3.5)
    _power_board(proj, class_width=2.0, track_width=2.0)
    assert tools.run_drc("demo")["gates"]["drc_clean"] is False
    assert "current above 3 A: ask Andrew for a width" in tools.gate_status("demo")["gates"]["drc_clean"]["detail"]
    _spec_classes(proj, 3.0)  # exactly 3 A -> 1.5 mm, class 2.0 passes
    assert tools.run_drc("demo")["gates"]["drc_clean"] is True


def test_required_width_table_from_profile():
    from acd.boardcheck import required_width
    table = load_profile(DEFAULT_PROFILE)["tracks"]["power_width_table"]
    assert [required_width(table, a) for a in (0.2, 1.0, 1.5, 2.0, 3.0)] == [0.5, 0.5, 1.0, 1.0, 1.5]


def _pro_with_power(proj, assignments=None, patterns=None):
    pro = json.loads((proj / "demo.kicad_pro").read_text())
    ns = pro.setdefault("net_settings", {})
    ns["classes"] = [{"name": "Default", "track_width": 0.25}, {"name": "Power", "track_width": 0.5}]
    ns["netclass_assignments"] = assignments
    ns["netclass_patterns"] = patterns or []
    (proj / "demo.kicad_pro").write_text(json.dumps(pro))


def test_netclass_check_flags_narrow_tracks_and_resolves_net_codes(proj):
    _pro_with_power(proj, assignments={"VIN": ["Power"]})
    (proj / "demo.kicad_pcb").write_text(PCB_TRACKS)
    res = tools.run_drc("demo")
    nc = res["netclass_check"]
    assert nc["classes"]["Power"] == ["VIN"] and nc["classes_without_nets"] == []
    assert nc["narrow_tracks"]["VIN"] == {"class": "Power", "class_width": 0.5,
                                          "min_width": 0.25, "segments": 2}
    assert "LED_A" not in nc["narrow_tracks"]
    assert res["gates"]["drc_clean"] is False  # any narrow track fails the gate (no neck-down exemption)
    detail = tools.gate_status("demo")["gates"]["drc_clean"]["detail"]
    assert "2 narrow tracks (VIN 0.25 mm vs class 0.5 mm)" in detail
    assert tools.review_summary("demo")["netclass"]["narrow_tracks"]["VIN"]["segments"] == 2


def test_netclass_with_no_nets_fails_drc_gate(proj):
    _pro_with_power(proj, assignments=None)
    (proj / "demo.kicad_pcb").write_text(PCB_TRACKS)
    res = tools.run_drc("demo")
    assert res["netclass_check"]["classes_without_nets"] == ["Power"]
    assert not res["gates"]["drc_clean"]
    _pro_with_power(proj, patterns=[{"netclass": "Power", "pattern": "/VIN"}])
    (proj / "demo.kicad_pcb").write_text(PCB_WIDE)
    assert tools.run_drc("demo")["gates"]["drc_clean"]


def test_fix_bom_flags_only_touches_symbols_marked_not_in_bom(proj):
    sch = proj / "demo.kicad_sch"
    sch.write_text("""(kicad_sch (version 20250114) (generator "test")
  (lib_symbols
    (symbol "Mechanical:MountingHole" (pin_names (offset 1.016)) (exclude_from_sim no) (in_bom no) (on_board yes))
    (symbol "Device:R" (exclude_from_sim no) (in_bom yes) (on_board yes)))
  (symbol (lib_id "Mechanical:MountingHole") (at 1 1 0) (unit 1)
    (body_style 1) (exclude_from_sim no) (in_bom yes) (on_board yes) (dnp no)
    (property "Reference" "H1") (property "Value" "MountingHole"))
  (symbol
    (lib_id "Device:R") (at 2 2 0) (in_bom yes) (on_board yes)
    (property "Reference" "R1") (property "Value" "1k (x)"))
)
""")
    res = tools.fix_bom_flags("demo")
    assert res["changed"] == ["H1"] and res["backup"]
    text = sch.read_text()
    assert text.count("(in_bom no)") == 2 and text.count("(in_bom yes)") == 2
    assert tools.fix_bom_flags("demo") == {"changed": [], "backup": None}


def test_mcp_backups_folder_is_not_backed_up(proj):
    (proj / ".mcp-backups").mkdir()
    (proj / ".mcp-backups" / "demo.kicad_pcb.1").write_text("x")
    name = tools.backup("demo", "manual")["backup"]
    with zipfile.ZipFile(proj / "backups" / name) as zf:
        assert not any(n.startswith(".mcp-backups") for n in zf.namelist())


# ------------------------------------------------- datasheet (SC-3)
def test_check_parts_requires_datasheet_on_every_part(proj):
    register_and_verify()
    for blank in ("", "  ", "~", "-", "None"):
        write_sch(proj / "demo.kicad_sch", sym("U1"),
                  sym("R1", "Device:R", "10k", "R_0805", "Yageo", "RC", datasheet=blank))
        res = tools.check_parts("demo")
        assert not res["passed"], blank
        assert [f["reference"] for f in res["failures"]] == ["R1"]  # stock-accepted part still needs one
        assert "missing Datasheet field" in res["failures"][0]["problems"]
    write_sch(proj / "demo.kicad_sch", sym("U1"),
              sym("R1", "Device:R", "10k", "R_0805", "Yageo", "RC", datasheet=None))
    assert not tools.check_parts("demo")["passed"]
    write_sch(proj / "demo.kicad_sch", sym("U1"),
              sym("R1", "Device:R", "10k", "R_0805", "Yageo", "RC", datasheet="https://example.com/rc.pdf"))
    assert tools.check_parts("demo")["passed"]


def test_check_parts_requires_registry_datasheet(proj):
    tools.register_part("acd:LM1117-3.3", "LM1117MPX-3.3", "TI", "", "SOT-223", "")
    tools.verify_part("acd:LM1117-3.3", "Andrew")
    res = tools.check_parts("demo")
    assert not res["passed"] and "no datasheet in the parts registry" in res["failures"][0]["problems"]


# ------------------------------------------------- backup before change
def _stage_backups(proj, stage):
    return sorted(p for p in (proj / "backups").iterdir() if p.name.endswith(f"_{stage}.zip"))


def _backup_gates(zpath):
    with zipfile.ZipFile(zpath) as zf:
        return json.loads(zf.read("records/revisions.json"))[0]["gates"]


def test_gate_tools_back_up_before_the_gate_record_changes(proj):
    register_and_verify()
    steps = [("spec", lambda: tools.confirm_spec("demo"), ["spec_confirmed"]),
             ("pre-parts", lambda: tools.check_parts("demo"), ["parts_verified"]),
             ("pre-erc", lambda: tools.run_erc("demo"), ["erc_clean"]),
             ("pre-drc", lambda: tools.run_drc("demo"), ["drc_clean", "unrouted_zero"])]
    for stage, call, new_gates in steps:
        before = _stage_backups(proj, stage)
        res = call()
        after = _stage_backups(proj, stage)
        assert len(after) == len(before) + 1 and res["backup"] in {p.name for p in after}
        taken = _backup_gates(proj / "backups" / res["backup"])
        assert all(g not in taken for g in new_gates), (stage, taken)
        assert all(g in tools.gate_status("demo")["gates"] for g in new_gates)
    # the backup of check_parts predates its own gate but holds earlier ones
    assert "spec_confirmed" in _backup_gates(proj / "backups" / sorted(
        p.name for p in _stage_backups(proj, "pre-parts"))[0])


def test_approve_backs_up_before_writing_approval(proj):
    pass_all_gates()
    res = tools.approve("demo", "Andrew", "ok")
    z = proj / "backups" / res["backup"]
    assert res["backup"].endswith("_approved.zip") and z.is_file()
    with zipfile.ZipFile(z) as zf:
        assert json.loads(zf.read("records/approvals.json")) == []
        revs = json.loads(zf.read("records/revisions.json"))
    assert revs[0]["status"] == "draft" and "approved" not in revs[0]["gates"]
    assert json.loads((proj / "records" / "approvals.json").read_text())[0]["approver"] == "Andrew"


def test_gate_rerun_on_unchanged_design_reuses_the_backup(proj):
    register_and_verify()
    names = {tools.run_erc("demo")["backup"] for _ in range(4)}
    assert len(names) == 1 and len(_stage_backups(proj, "pre-erc")) == 1
    sch = proj / "demo.kicad_sch"
    sch.write_text(sch.read_text() + "\n")  # a design change -> a new backup
    assert tools.run_erc("demo")["backup"] not in names
    assert len(_stage_backups(proj, "pre-erc")) == 2


def test_gate_backups_never_prune_hand_stage_backups(proj, monkeypatch):
    monkeypatch.setenv("ACD_BACKUP_KEEP", "2")
    register_and_verify()
    hand = {tools.backup("demo", s)["backup"] for s in ("schematic", "routed")}
    sch, pcb = proj / "demo.kicad_sch", proj / "demo.kicad_pcb"
    for i in range(4):  # four review rounds, each with a design change
        sch.write_text(sch.read_text() + "\n")
        pcb.write_text(pcb.read_text() + "\n")
        tools.check_parts("demo")
        tools.run_erc("demo")
        tools.run_drc("demo")
    assert len(_stage_backups(proj, "pre-erc")) == 2  # gate stages are pruned to the keep count
    assert len(_stage_backups(proj, "pre-drc")) == 2
    left = {p.name for p in (proj / "backups").iterdir()}
    assert hand <= left  # the backups taken by hand before each stage are all still there


def _spec_accepted(proj, text):
    (proj / "spec" / "circuit-spec.yaml").write_text("name: demo\n" + text, encoding="utf-8")


def test_accepted_narrow_passes_only_with_reason_and_floor(proj):
    _pro_with_power(proj, assignments={"VIN": ["Power"]})
    (proj / "demo.kicad_pcb").write_text(PCB_TRACKS)  # VIN at 0.25 mm in a 0.5 mm class
    _spec_accepted(proj, "accepted_narrow:\n  - {net: VIN, min_width_mm: 0.25, "
                         "reason: neck-down into U1 pins}\n")
    res = tools.run_drc("demo")
    assert res["gates"]["drc_clean"] is True
    assert res["netclass_check"]["accepted_narrow"]["VIN"]["reason"] == "neck-down into U1 pins"
    assert "accepted narrow: VIN 0.25 mm (neck-down into U1 pins)" in \
        tools.gate_status("demo")["gates"]["drc_clean"]["detail"]
    assert tools.review_summary("demo")["netclass"]["accepted_narrow"]["VIN"]["segments"] == 2
    # narrower than the accepted floor -> fails
    _spec_accepted(proj, "accepted_narrow:\n  - {net: VIN, min_width_mm: 0.3, reason: x}\n")
    assert tools.run_drc("demo")["gates"]["drc_clean"] is False
    # no reason -> fails, with the reason named
    _spec_accepted(proj, "accepted_narrow:\n  - {net: VIN, min_width_mm: 0.25}\n")
    res = tools.run_drc("demo")
    assert res["gates"]["drc_clean"] is False
    assert "accepted_narrow VIN: a reason is required" in res["gate_failures"]


def test_adding_accepted_narrow_makes_spec_confirmation_stale(proj):
    tools.confirm_spec("demo")
    assert tools.gate_status("demo")["gates"]["spec_confirmed"]["effective"]
    _spec_accepted(proj, "accepted_narrow:\n  - {net: VIN, min_width_mm: 0.25, reason: r}\n")
    assert not tools.gate_status("demo")["gates"]["spec_confirmed"]["effective"]  # Andrew re-confirms


def test_failed_gate_precondition_makes_no_backup(env):
    tools.init_project("blank")
    with pytest.raises(AcdError):
        tools.confirm_spec("blank")  # empty spec: nothing changes, so nothing is backed up
    assert list((env["projects"] / "blank" / "backups").iterdir()) == []


# ------------------------------------------------- reproducible export (SC-12)
def test_normalise_dates_only_changes_date_values():
    from acd.release import normalise_dates
    d = "2026-10-16T09:30:00+11:00"
    a = ("G04 #@! TF.CreationDate,2025-01-01T01:02:03+00:00*\n%TF.CreationDate,2025-01-01T01:02:03+00:00*%\n"
         "G04 Created by KiCad (PCBNEW 7) date 2025-01-01 01:02:03*\nX1Y1D02*\n")
    out = normalise_dates(a, d)
    assert out == (f"G04 #@! TF.CreationDate,{d}*\n%TF.CreationDate,{d}*%\n"
                   f"G04 Created by KiCad (PCBNEW 7) date {d}*\nX1Y1D02*\n")
    # real KiCad 10.0.6 Excellon header (seen in a real export): no braces around the version
    drl = "M48\n; DRILL file KiCad 10.0.6 date 2026-10-16T20:41:02+11:00\n; #@! TF.CreationDate,2025-01-01T01:02:03+00:00\nMETRIC\n"
    assert normalise_dates(drl, d) == (f"M48\n; DRILL file KiCad 10.0.6 date {d}\n"
                                       f"; #@! TF.CreationDate,{d}\nMETRIC\n")
    # the older braced form still normalises too
    drl_old = "M48\n; DRILL file {KiCad 10.0.1} date Thu 01 Jan 2025\nMETRIC\n"
    assert normalise_dates(drl_old, d) == f"M48\n; DRILL file {{KiCad 10.0.1}} date {d}\nMETRIC\n"
    job = '{"Header": {"CreationDate": "2025-01-01T01:02:03+00:00", "Other": "keep"}, "FileDate": "x"}'
    assert normalise_dates(job, d) == (f'{{"Header": {{"CreationDate": "{d}", "Other": "keep"}}, "FileDate": "{d}"}}')
    assert normalise_dates(out, d) == out  # idempotent


def test_reexport_of_same_approved_rev_is_byte_identical(approved, env, monkeypatch):
    monkeypatch.setenv("FAKE_GBRJOB", "1")
    rel = approved / "releases"
    tools.export_release("demo")
    (rel / "rev-A").rename(rel / "first")
    tools.export_release("demo")
    (rel / "rev-A").rename(rel / "second")
    first = {p.relative_to(rel / "first").as_posix(): sha256_file(p) for p in (rel / "first").rglob("*") if p.is_file()}
    second = {p.relative_to(rel / "second").as_posix(): sha256_file(p) for p in (rel / "second").rglob("*") if p.is_file()}
    assert first == second and any(n.endswith(".gbrjob") for n in first) and "manifest.json" in first
    appr_date = json.loads((approved / "records" / "approvals.json").read_text())[0]["date"]
    gerber = next((rel / "second" / "gerbers").glob("*.gbr")).read_text()
    assert f"TF.CreationDate,{appr_date}*" in gerber
    with zipfile.ZipFile(rel / "second" / "project.zip") as zf:
        stamps = {i.date_time for i in zf.infolist()}
        assert len(stamps) == 1 and stamps.pop()[0] == int(appr_date[:4])
        assert zf.namelist() == sorted(zf.namelist())
    shutil_target = rel / "rev-A"
    (rel / "second").rename(shutil_target)
    assert tools.verify_release("demo", "A")["ok"]


# ------------------------------------------------- fix A: errors reach the caller
def test_wrapped_tool_returns_acd_error_as_a_result(env):
    from acd.server import wrap_tool
    res = wrap_tool(tools.gate_status)("no-such-project")
    assert res["ok"] is False and "no-such-project" in res["error"] and "where" not in res


def test_wrapped_tool_reports_other_exceptions_with_location(env):
    from acd.server import wrap_tool

    def boom(project: str) -> dict:
        return {}["missing"]
    res = wrap_tool(boom)("x")
    assert res["ok"] is False and res["error"].startswith("KeyError: ")
    assert res["where"].startswith("test_acd.py:")


def test_wrapped_tool_passes_results_through_and_keeps_signature(proj):
    import inspect
    from acd.server import wrap_tool
    wrapped = wrap_tool(tools.gate_status)
    assert wrapped("demo") == tools.gate_status("demo")
    for name in ("gate_status", "verify_part", "restore_backup", "new_revision"):
        assert inspect.signature(wrap_tool(getattr(tools, name))) == inspect.signature(getattr(tools, name))


def test_server_tool_failure_is_a_normal_result(env):
    import asyncio
    from acd.server import build_server
    res = asyncio.run(build_server().call_tool("gate_status", {"project": "no-such-project"}))
    assert "no-such-project" in str(res) and "Error executing tool" not in str(res)


# ------------------------------------------------- fix C: registry variants per lib_id
def _led_board(proj, *rows):
    write_sch(proj / "demo.kicad_sch", *rows)


def test_old_format_registry_still_loads_and_passes(proj, env):
    (env["library"] / "registry.json").write_text(json.dumps({"acd:LM1117-3.3": {
        "mpn": "LM1117MPX-3.3", "manufacturer": "TI", "datasheet": "http://ds",
        "footprint": "Package_TO_SOT_SMD:SOT-223", "verified": True, "verified_by": "Andrew",
        "verified_date": "2026-01-01", "notes": ""}}))
    assert tools.check_parts("demo")["passed"]
    tools.register_part("acd:LM1117-3.3", "OTHER", "TI", "http://ds", "fp")  # save() writes the new format
    reg = json.loads((env["library"] / "registry.json").read_text())
    assert [v["mpn"] for v in reg["acd:LM1117-3.3"]["variants"]] == ["LM1117MPX-3.3", "OTHER"]
    assert reg["acd:LM1117-3.3"]["variants"][0]["verified"] is True  # untouched


def test_two_mpns_on_one_lib_id_are_separate_variants(env):
    r1 = tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://a", "LED_SMD:LED_0603_1608Metric")
    r2 = tools.register_part("Device:LED", " led-grn ", "Kingbright", "http://b", "LED_SMD:LED_0805_2012Metric")
    assert (r1["action"], r2["action"], r2["variant_count"]) == ("added", "added", 2)
    r3 = tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://a2", "LED_SMD:LED_0603_1608Metric")
    assert r3["action"] == "updated" and r3["variant_count"] == 2
    tools.verify_part("Device:LED", "Andrew", "led-red")
    reg = json.loads((env["library"] / "registry.json").read_text())["Device:LED"]["variants"]
    assert [v["verified"] for v in reg] == [True, False] and reg[0]["datasheet"] == "http://a2"
    tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://a2", "LED_SMD:LED_0603_1608Metric")
    reg = json.loads((env["library"] / "registry.json").read_text())["Device:LED"]["variants"]
    assert [v["verified"] for v in reg] == [False, False]  # re-registering resets verification


def test_verify_part_with_several_variants_needs_an_mpn(env):
    tools.register_part("Device:LED", "A1", "m", "ds", "fp")
    tools.register_part("Device:LED", "B2", "m", "ds", "fp")
    with pytest.raises(AcdError, match="A1, B2"):
        tools.verify_part("Device:LED", "Andrew")
    with pytest.raises(AcdError, match="no variant with MPN"):
        tools.verify_part("Device:LED", "Andrew", "C3")
    assert tools.verify_part("Device:LED", "Andrew", "B2")["entry"]["verified"]


def test_check_parts_matches_the_bom_mpn_to_a_variant(proj):
    led = lambda mpn, fp="LED_SMD:LED_0603_1608Metric": sym("D1", "Device:LED", "LED", fp, "Kingbright", mpn)
    tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://ds", "LED_SMD:LED_0603_1608Metric")
    _led_board(proj, led("LED-RED"))
    res = tools.check_parts("demo")  # registered but not verified
    assert not res["passed"] and "registered but not verified" in res["failures"][0]["problems"]
    tools.verify_part("Device:LED", "Andrew")
    assert tools.check_parts("demo")["passed"]
    _led_board(proj, led("LED-GRN"))  # a different LED is not covered by the verified one
    res = tools.check_parts("demo")
    assert not res["passed"]
    assert "Device:LED has no registry entry for MPN LED-GRN" in res["failures"][0]["problems"]
    _led_board(proj, led(" led-red "))  # MPN compare ignores case and padding
    assert tools.check_parts("demo")["passed"]


def test_check_parts_fails_on_footprint_mismatch(proj):
    tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://ds", "LED_SMD:LED_0603_1608Metric")
    tools.verify_part("Device:LED", "Andrew")
    _led_board(proj, sym("D1", "Device:LED", "LED", "LED_SMD:LED_0805_2012Metric", "Kingbright", "LED-RED"))
    res = tools.check_parts("demo")
    assert not res["passed"]
    assert ("footprint LED_SMD:LED_0805_2012Metric in schematic, registry has "
            "LED_SMD:LED_0603_1608Metric") in res["failures"][0]["problems"]
    tools.register_part("Device:LED", "LED-RED", "Kingbright", "http://ds", "")  # no footprint set: no check
    tools.verify_part("Device:LED", "Andrew")
    assert tools.check_parts("demo")["passed"]


# ------------------------------------------------- fix D: approved revisions are not changed
def _tree_hashes(root):
    return {p.relative_to(root).as_posix(): sha256_file(p) for p in sorted(root.rglob("*"))
            if p.is_file() and p.relative_to(root).parts[0] not in ("backups", "releases")}


def test_gate_tools_on_an_approved_revision_change_nothing(approved, env):
    before = _tree_hashes(approved)
    backups_before = sorted(p.name for p in (approved / "backups").iterdir())
    for call in (lambda: tools.check_parts("demo"), lambda: tools.run_erc("demo"),
                 lambda: tools.run_drc("demo")):
        res = call()
        assert res["recorded"] is False and res["backup"] is None
        assert res["note"] == "rev A is approved: results not recorded; start a new revision to change it"
    assert _tree_hashes(approved) == before
    assert sorted(p.name for p in (approved / "backups").iterdir()) == backups_before
    assert tools.gate_status("demo")["gates"]["approved"]["effective"]


def test_approved_revision_check_still_reports_failures(approved, monkeypatch):
    monkeypatch.setenv("FAKE_ERC_ERRORS", "2")
    res = tools.run_erc("demo")
    assert res["passed"] is False and res["errors"] == 2 and res["recorded"] is False
    assert tools.gate_status("demo")["gates"]["erc_clean"]["passed"] is True


def test_gate_tools_on_a_draft_revision_say_recorded(proj):
    register_and_verify()
    assert tools.check_parts("demo")["recorded"] is True
    assert tools.run_erc("demo")["recorded"] is True
    assert tools.run_drc("demo")["recorded"] is True
    assert (proj / "reports" / "erc.json").is_file() and (proj / "reports" / "netclass.json").is_file()


def test_changing_tools_refuse_an_approved_revision(approved):
    for call in (lambda: tools.confirm_spec("demo"), lambda: tools.apply_profile("demo"),
                 lambda: tools.fix_bom_flags("demo")):
        with pytest.raises(AcdError, match=r"rev A is approved; start a new revision \(new_revision\) first"):
            call()


# ------------------------------------------------- fix G: restore_backup dry run, removals, renames
def test_restore_dry_run_changes_nothing_and_lists_removals(proj):
    name = tools.backup("demo", "placement")["backup"]
    (proj / "demo.kicad_pcb").write_text("(kicad_pcb ; later work)\n")
    (proj / "extra.txt").write_text("created after backup")
    before = _tree_hashes(proj)
    backups_before = sorted(p.name for p in (proj / "backups").iterdir())
    res = tools.restore_backup("demo", name, dry_run=True)
    assert res["would_remove"] == ["extra.txt"] and res["would_rename"] == {}
    assert "demo.kicad_pcb" in res["would_restore"] and "records/project.json" not in res["would_restore"]
    assert _tree_hashes(proj) == before
    assert sorted(p.name for p in (proj / "backups").iterdir()) == backups_before


def test_restore_reports_removed_files_and_keeps_them_in_the_pre_restore_backup(proj):
    name = tools.backup("demo", "placement")["backup"]
    (proj / "extra.txt").write_text("created after backup")
    res = tools.restore_backup("demo", name)
    assert res["removed"] == ["extra.txt"] and res["renamed"] == {}
    assert not (proj / "extra.txt").exists()
    with zipfile.ZipFile(proj / "backups" / res["pre_restore_backup"]) as zf:
        assert zf.read("extra.txt") == b"created after backup"


def test_restore_renames_kicad_files_to_the_project_name(env):
    tools.init_project("new-name")
    root = env["projects"] / "new-name"
    (root / "new-name.kicad_pcb").write_text("(kicad_pcb ; current)\n")
    z = root / "backups" / "20260101-0000_A_manual.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("old-name.kicad_pcb", "(kicad_pcb ; old)\n")
        zf.writestr("old-name.kicad_sch", "(kicad_sch)\n")
        zf.writestr("notes/old-name.kicad_note", "not renamed\n")
    dry = tools.restore_backup("new-name", z.name, dry_run=True)
    assert dry["would_rename"] == {"old-name.kicad_pcb": "new-name.kicad_pcb",
                                   "old-name.kicad_sch": "new-name.kicad_sch"}
    assert dry["would_remove"] == ["spec/circuit-spec.yaml"]
    res = tools.restore_backup("new-name", z.name)
    assert res["renamed"] == dry["would_rename"]
    assert (root / "new-name.kicad_pcb").read_text() == "(kicad_pcb ; old)\n"
    assert (root / "new-name.kicad_sch").is_file()
    assert (root / "notes" / "old-name.kicad_note").is_file()
    assert not (root / "old-name.kicad_pcb").exists()
    assert "new-name.kicad_pcb" not in res["removed"]


def test_restore_does_not_rename_when_stems_differ(env):
    tools.init_project("new-name")
    root = env["projects"] / "new-name"
    z = root / "backups" / "20260101-0000_A_manual.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("a.kicad_pcb", "x")
        zf.writestr("b.kicad_sch", "y")
    assert tools.restore_backup("new-name", z.name, dry_run=True)["would_rename"] == {}


def test_restore_dry_run_still_rejects_unsafe_archives(proj):
    evil = proj / "backups" / "20260101-0000_A_manual.zip"
    with zipfile.ZipFile(evil, "w") as zf:
        zf.writestr("../escaped.txt", "x")
    with pytest.raises(AcdError, match="unsafe"):
        tools.restore_backup("demo", evil.name, dry_run=True)
