"""Fake kicad-cli for tests. Run through the interpreter via ACD_KICAD_CLI=<this file>.

Behaviour knobs (environment variables):
  FAKE_LOG            file that gets one JSON line (argv) per call
  FAKE_ERC_ERRORS     number of ERC error violations (default 0)
  FAKE_ERC_WARNINGS   number of ERC warnings (default 0)
  FAKE_DRC_ERRORS     number of DRC error violations (default 0)
  FAKE_DRC_SILK       number of DRC silkscreen warnings (default 0)
  FAKE_PARITY_ERRORS  number of schematic-parity errors (default 0)
  FAKE_UNCONNECTED    number of unconnected items (default 0)
  FAKE_FAIL           if set, every call exits with code 3
  FAKE_GBRJOB         if set, the gerbers export also writes a .gbrjob file

Gerber, drill and job files carry the current time (with microseconds) in the same
header lines real KiCad writes, so two exports differ unless the dates are normalised.
"""

import csv
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path


def opt(argv, *names):
    for n in names:
        if n in argv:
            return argv[argv.index(n) + 1]
    return None


def stamp():
    return datetime.now().astimezone().isoformat()


def violations(n, sev, kind):
    return [{"type": kind, "severity": sev, "description": f"fake {kind} {i}",
             "items": [{"description": f"Pad 1 [NET{i}] of U{i}", "pos": {"x": 0, "y": 0}}]}
            for i in range(n)]


def parse_sch(path):
    parts = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        m = re.search(r'\(symbol \(lib_id "([^"]+)"\)(.*)', line)
        if not m:
            continue
        props = dict(re.findall(r'\(property "(\w+)" "([^"]*)"\)', m.group(2)))
        if "(in_bom no)" in m.group(2):
            continue
        parts.append(props)
    return parts


def bom(argv):
    sch, out = argv[-1], opt(argv, "-o", "--output")
    fields = opt(argv, "--fields").split(",")
    labels = opt(argv, "--labels").split(",")
    group_by = opt(argv, "--group-by")
    parts = parse_sch(sch)
    rows = []
    if group_by:
        keys = group_by.split(",")
        groups = {}
        for p in parts:
            groups.setdefault(tuple(p.get(k, "") for k in keys), []).append(p)
        for g in groups.values():
            first = dict(g[0])
            first["Reference"] = ",".join(sorted(p["Reference"] for p in g))
            first["${QUANTITY}"] = str(len(g))
            rows.append(first)
    else:
        rows = [dict(p, **{"${QUANTITY}": "1"}) for p in parts]
    rows.sort(key=lambda r: r.get("Reference", ""))
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, quoting=csv.QUOTE_ALL)
        w.writerow(labels)
        for r in rows:
            w.writerow([r.get(f, "") for f in fields])


def main(argv):
    log = os.environ.get("FAKE_LOG")
    if log:
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(argv) + "\n")
    if os.environ.get("FAKE_FAIL"):
        print("fake failure", file=sys.stderr)
        return 3
    if argv[:1] == ["--version"]:
        print("10.0.1")
        return 0
    cmd = argv[:2] if argv[:2] in (["sch", "erc"], ["pcb", "drc"]) else argv[:3]
    n = lambda k: int(os.environ.get(k, "0"))  # noqa: E731
    if cmd == ["sch", "erc"]:
        v = violations(n("FAKE_ERC_ERRORS"), "error", "pin_not_connected") + \
            violations(n("FAKE_ERC_WARNINGS"), "warning", "lib_symbol_issues")
        Path(opt(argv, "-o", "--output")).write_text(json.dumps(
            {"$schema": "x", "sheets": [{"path": "/", "violations": v}]}), encoding="utf-8")
    elif cmd == ["pcb", "drc"]:
        Path(opt(argv, "-o", "--output")).write_text(json.dumps({
            "violations": violations(n("FAKE_DRC_ERRORS"), "error", "clearance")
            + violations(n("FAKE_DRC_SILK"), "warning", "silk_edge_clearance"),
            "unconnected_items": violations(n("FAKE_UNCONNECTED"), "error", "unconnected_items"),
            "schematic_parity": violations(n("FAKE_PARITY_ERRORS"), "error", "footprint_mismatch"),
        }), encoding="utf-8")
    elif cmd == ["sch", "export", "bom"]:
        bom(argv)
    elif cmd == ["pcb", "export", "gerbers"]:
        out = Path(opt(argv, "-o", "--output"))
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(argv[-1]).stem
        for layer in opt(argv, "-l", "--layers").split(","):
            ext = ".gbr" if "--no-protel-ext" in argv else ".gtl"
            ts = stamp()
            (out / f"{stem}-{layer.replace('.', '_')}{ext}").write_text(
                f"G04 fake gerber*\nG04 #@! TF.CreationDate,{ts}*\n"
                f"%TF.CreationDate,{ts}*%\nM02*\n", encoding="utf-8")
        if os.environ.get("FAKE_GBRJOB"):
            (out / f"{stem}-job.gbrjob").write_text(json.dumps(
                {"Header": {"GenerationSoftware": {"Vendor": "KiCad"}, "CreationDate": stamp()}},
                indent=2), encoding="utf-8")
    elif cmd == ["pcb", "export", "drill"]:
        out = Path(opt(argv, "-o", "--output"))
        out.mkdir(parents=True, exist_ok=True)
        stem = Path(argv[-1]).stem
        names = ["PTH", "NPTH"] if "--excellon-separate-th" in argv else ["all"]
        for nm in names:
            (out / f"{stem}-{nm}.drl").write_text(
                f"M48\n; DRILL file KiCad 10.0.6 date {stamp()}\n"
                f"; #@! TF.CreationDate,{stamp()}\nMETRIC\nM30\n", encoding="utf-8")
    elif cmd == ["pcb", "export", "pos"]:
        Path(opt(argv, "-o", "--output")).write_text("Ref,Val,PosX,PosY\nR1,10k,1,2\n", encoding="utf-8")
    else:
        print(f"fake kicad-cli: unsupported {argv}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
