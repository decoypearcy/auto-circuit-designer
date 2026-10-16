import json
import sys
from pathlib import Path

import pytest

from acd import tools
from acd.config import DEFAULT_PROFILE

HERE = Path(__file__).resolve().parent
FAKE = HERE / "fake_kicad_cli.py"

SCH_TEMPLATE = """(kicad_sch (version 20250114) (generator "test")
  (lib_symbols (symbol "acd:LM1117-3.3" (property "Reference" "U")))
{symbols}
)
"""


def sym(ref, lib_id="acd:LM1117-3.3", value="LM1117", footprint="Package_TO_SOT_SMD:SOT-223",
        mfr="TI", mpn="LM1117MPX-3.3", extra=""):
    props = f'(property "Reference" "{ref}") (property "Value" "{value}") (property "Footprint" "{footprint}")'
    if mfr is not None:
        props += f' (property "Manufacturer" "{mfr}")'
    if mpn is not None:
        props += f' (property "MPN" "{mpn}")'
    return f'(symbol (lib_id "{lib_id}") {props} {extra})'


def write_sch(path: Path, *symbols: str) -> None:
    path.write_text(SCH_TEMPLATE.format(symbols="\n".join(symbols)), encoding="utf-8")


@pytest.fixture
def env(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    library = tmp_path / "library"
    projects.mkdir()
    library.mkdir()
    monkeypatch.setenv("ACD_PROJECTS_ROOT", str(projects))
    monkeypatch.setenv("ACD_LIBRARY_ROOT", str(library))
    monkeypatch.setenv("ACD_KICAD_CLI", str(FAKE))
    monkeypatch.setenv("ACD_PROFILE", str(DEFAULT_PROFILE))
    monkeypatch.setenv("ACD_BACKUP_KEEP", "5")
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "kicad-calls.log"))
    for k in ("FAKE_ERC_ERRORS", "FAKE_ERC_WARNINGS", "FAKE_DRC_ERRORS", "FAKE_PARITY_ERRORS",
              "FAKE_UNCONNECTED", "FAKE_FAIL"):
        monkeypatch.delenv(k, raising=False)
    return {"projects": projects, "library": library, "log": tmp_path / "kicad-calls.log",
            "tmp": tmp_path}


@pytest.fixture
def proj(env):
    """An initialised project with the KiCad files the base MCP would create."""
    tools.init_project("demo")
    root = env["projects"] / "demo"
    (root / "demo.kicad_pro").write_text(json.dumps(
        {"meta": {"filename": "demo.kicad_pro", "version": 1},
         "board": {"design_settings": {"rules": {"min_clearance": 0.0, "keep_me": 1}}},
         "net_settings": {"classes": [{"name": "Default", "clearance": 0.2, "wire_width": 6}],
                          "meta": {"version": 3}}}, indent=2), encoding="utf-8")
    write_sch(root / "demo.kicad_sch",
              sym("U1"), sym("R1", "Device:R", "10k", "Resistor_SMD:R_0805_2012Metric", "Yageo", "RC0805FR-0710KL"),
              sym("TP1", "Connector:TestPoint", "TP", "TestPoint:TP", mfr=None, mpn=None, extra="(in_bom no)"))
    (root / "demo.kicad_pcb").write_text("(kicad_pcb (version 1) ; routed v1)\n", encoding="utf-8")
    (root / "spec" / "circuit-spec.yaml").write_text("name: demo\ndescription: test board\n", encoding="utf-8")
    return root


def register_and_verify(lib_id="acd:LM1117-3.3"):
    tools.register_part(lib_id, "LM1117MPX-3.3", "TI", "http://ds", "Package_TO_SOT_SMD:SOT-223", "")
    tools.verify_part(lib_id, "Andrew")


def pass_all_gates(name="demo"):
    register_and_verify()
    tools.confirm_spec(name)
    assert tools.check_parts(name)["passed"]
    assert tools.run_erc(name)["passed"]
    assert tools.run_drc(name)["gates"] == {"drc_clean": True, "unrouted_zero": True}


@pytest.fixture
def approved(proj):
    pass_all_gates()
    tools.approve("demo", "Andrew", "ok")
    return proj


def calls(env):
    if not env["log"].exists():
        return []
    return [json.loads(l) for l in env["log"].read_text().splitlines() if l]
