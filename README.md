# Auto Circuit Designer

Describe a circuit to Claude and get an approved, PCBWay-ready KiCad 10 project back.

## What is in this repo
| Folder | Contents |
|---|---|
| `skill/auto-circuit-designer/SKILL.md` | The workflow Claude follows: spec, library, schematic, outline and mounting holes, placement, routing, review, approval, export, revisions. |
| `companion/` | The `acd` MCP server (Python): project folders, rule profile, gates, revisions, approvals, backups, release export. |
| `profiles/` | Design rule profiles. `pcbway-standard-2layer.yaml` is the default. |
| `setup/` | Windows 10 setup script, pinned versions and the Claude Desktop config example. Start with `setup/SETUP.md`. |
| `docs/` | Design notes, including the companion MCP spec. |

## How it fits together
- **KiCad 10** is the engine. `kicad-cli` runs ERC, DRC and exports.
- **kicad MCP** ([mixelpixx/KiCAD-MCP-Server](https://github.com/mixelpixx/KiCAD-MCP-Server), MIT) edits schematics and boards and runs Freerouting.
- **acd MCP** (this repo) enforces the gates and handles backups and releases.
- PCB projects and the component library live in local folders outside git. Each project keeps its own `backups/` and `releases/`.

## Development
```
cd companion
uv sync
uv run pytest
```
Tests use a fake `kicad-cli`, so KiCad is not needed to run them.
