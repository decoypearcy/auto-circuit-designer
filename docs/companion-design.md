# Companion MCP "acd" - design

Status: approved architecture (tracker D-19). This file is the build spec for T-12.
Base MCP (mixelpixx KiCAD-MCP-Server) does schematic, placement, routing and editing.
`acd` does only what the base MCP does not: folder layout, rule profile, gates,
revisions, approvals, backups, release export, lock check, parts registry, review summary.

## Runtime
- Python 3.11+, managed with uv. Package `acd` under `src/acd/`, entry point `acd-mcp`
  (stdio MCP server using the official `mcp` Python SDK, FastMCP).
- Dependencies: `mcp`, `pyyaml`. Standard library for everything else (zipfile, hashlib, json, subprocess).
- Target OS: Windows 10. Use `pathlib` everywhere. No shell=True.
- All KiCad work goes through `kicad-cli` (KiCad 10.0.x) via subprocess. No pcbnew import.

## Configuration (environment variables)
| Var | Meaning | Default |
|---|---|---|
| ACD_PROJECTS_ROOT | folder holding all PCB projects | required |
| ACD_LIBRARY_ROOT | shared component library folder | required |
| ACD_KICAD_CLI | path to kicad-cli | `C:\Program Files\KiCad\10.0\bin\kicad-cli.exe` |
| ACD_PROFILE | rule profile file | repo `profiles/pcbway-standard-2layer.yaml` |
| ACD_BACKUP_KEEP | backups kept per stage | 5 |

## Project folder layout
```
<ACD_PROJECTS_ROOT>/<name>/
  <name>.kicad_pro / .kicad_sch / .kicad_pcb   (created by the base MCP)
  spec/circuit-spec.yaml
  records/project.json      {name, created, kicad_version, profile, current_rev}
  records/revisions.json    [revision objects, see below]
  records/approvals.json    [ {rev, approver, date, note, sch_sha256, pcb_sha256} ]
  reports/erc.json, reports/drc.json   (latest kicad-cli JSON reports)
  backups/YYYYMMDD-HHMM_<rev>_<stage>.zip
  releases/rev-<X>/ gerbers/ drill/ bom.csv [pos.csv] project.zip
                    rev-<X>_pcbway.zip (gerbers + drill only) manifest.json (sha256 of every file)
```
Backups and project.zip exclude `backups/`, `releases/`, KiCad lock files (`*.lck`, `~*`),
and `*-backups/` folders made by KiCad itself.

## Library layout
```
<ACD_LIBRARY_ROOT>/
  symbols/acd.kicad_sym
  footprints/acd.pretty/
  3dmodels/
  registry.json   { "<lib_id>": {mpn, manufacturer, datasheet, footprint, verified, verified_by, verified_date, notes} }
```
`lib_id` is the KiCad symbol id, e.g. `acd:LM1117-3.3`.
Stock symbols accepted without registry verification: `Device:R`, `Device:C`, `Device:L`
(pin map is trivial), and any part with `exclude_from_bom` (mounting holes, test points).
Every BOM part still needs `Manufacturer` and `MPN` fields except excluded parts.

## Revision object
```json
{"rev": "A", "parent": null, "created": "2026-10-16T12:30:00+11:00",
 "status": "draft|approved|superseded",
 "gates": {"spec_confirmed": {...}, "parts_verified": {...}, "erc_clean": {...},
           "drc_clean": {...}, "unrouted_zero": {...}, "approved": {...}},
 "notes": ""}
```
Each gate value: `{"passed": bool, "at": iso8601, "hash": "<sha256 of the file it checked>", "detail": "..."}`.
Revision ids are letters A, B, C ... Z, AA.

## Gates (in order)
1. `spec_confirmed` - hash of `spec/circuit-spec.yaml`.
2. `parts_verified` - every BOM part passes the registry rule above. Hash of `.kicad_sch`.
3. `erc_clean` - zero ERC errors. Hash of `.kicad_sch`.
4. `drc_clean` - zero DRC errors including schematic parity. Hash of `.kicad_pcb`.
5. `unrouted_zero` - zero unconnected items in the DRC report. Hash of `.kicad_pcb`.
6. `approved` - set only by `approve`.
A gate is stale (treated as failed) when the current file hash differs from the stored hash.
`gate_status` recomputes staleness every call.

## Tools
All tools take `project` (folder name under ACD_PROJECTS_ROOT) unless noted, and return JSON-serialisable dicts.
Every tool that writes inside a project first runs the lock check and refuses with a clear message
if KiCad lock files are present (Andrew has the project open).

| Tool | Behaviour |
|---|---|
| `init_project(name)` | Create folder layout and records (rev A, draft). Refuse if folder exists. Returns the path for the base MCP to create KiCad files in. |
| `apply_profile(project)` | Write the profile into `.kicad_pro` (JSON: board design_settings rules, default net class widths/clearance/via) and write `<name>.kicad_dru` for rules .kicad_pro cannot hold (copper to edge, silk sizes, annular ring). Backup first. |
| `lock_check(project)` | List lock files. `{locked: bool, files: [...]}` |
| `confirm_spec(project)` | Set gate 1. Call only after Andrew confirms the spec in chat. |
| `register_part(lib_id, mpn, manufacturer, datasheet, footprint, notes)` | Add or update a registry entry with `verified: false`. |
| `verify_part(lib_id, verified_by)` | Mark verified. Call only after Andrew signs off. |
| `check_parts(project)` | Export BOM via `kicad-cli sch export bom`, apply the registry rule, set gate 2, list failures. |
| `run_erc(project)` | `kicad-cli sch erc --format json`, save report, set gate 3, return counts and the first 20 errors. |
| `run_drc(project)` | `kicad-cli pcb drc --format json --schematic-parity`, save report, set gates 4 and 5, return counts, unconnected count and first 20 errors. |
| `review_summary(project)` | Gate table plus ERC/DRC counts, unconnected nets list, open warnings count. Read-only. |
| `gate_status(project)` | Gates with staleness. Read-only. |
| `backup(project, stage)` | Zip to `backups/`, prune to ACD_BACKUP_KEEP per stage. Stages: spec, library, schematic, placement, routed, review, approved, pre-restore, manual; gate tools also use pre-parts, pre-erc and pre-drc (their own stages, so gate re-runs never prune hand backups; skipped when the design files match the newest backup of that stage). |
| `list_backups(project)` | Newest first. |
| `restore_backup(project, backup_name)` | Backup current state as `pre-restore`, then replace working files (never backups/ or releases/) with the zip contents. Resets gates to stale naturally via hashes. |
| `approve(project, approver, note)` | Requires gates 1 to 5 passed and not stale. Records approval with file hashes, sets status approved, makes an `approved` backup. |
| `export_release(project)` | Requires current rev approved and hashes unchanged since approval. Runs kicad-cli: gerbers (profile layers, no X2, no Protel extensions), drill (Excellon, mm, separate PTH/NPTH, absolute origin), BOM CSV (Reference, Value, Footprint, Manufacturer, MPN, Qty, grouped by value+footprint), pos only if profile centroid true. Writes `project.zip`, `rev-X_pcbway.zip`, `manifest.json`. Refuses if the release folder already exists. |
| `verify_release(project, rev)` | Recompute manifest hashes; report mismatches (SC-12). |
| `new_revision(project, from_rev)` | Requires from_rev approved. Mark it superseded, create next rev letter as draft with parent = from_rev, working files restored from the release `project.zip` if they differ. Approved release folders are never modified (SC-13). |

## kicad-cli
Use the KiCad 10 CLI reference (https://docs.kicad.org/10.0/en/cli/cli.html) for exact flags.
Wrap calls in one module (`kicad.py`) with a timeout (120 s) and return code checks, so flags live in one place.

## Tests
pytest, no KiCad needed: a fake `kicad-cli` (Python script set via ACD_KICAD_CLI) that writes canned
ERC/DRC JSON, BOM CSV and dummy gerber/drill files. Cover: layout creation, lock refusal, gate staleness
after a file edit, approval refusal with failing or stale gates, export refusal without approval,
backup prune count, restore with pre-restore backup, new revision immutability of the old release,
manifest verification.
