---
name: auto-circuit-designer
description: Use when Andrew describes a circuit or asks to design, revise, review, approve or export a PCB. Runs the Auto Circuit Designer workflow in KiCad 10 through the kicad and acd MCP servers on his PC - spec, verified library parts, schematic, outline and M3 mounting holes, placement, routing, review gates, approval, PCBWay export and local backups.
---

# Auto Circuit Designer

Turn Andrew's circuit description into an approved, PCBWay-ready KiCad 10 project.
Two MCP servers do the work:
- **kicad** (mixelpixx KiCAD-MCP-Server): schematic, libraries, board, placement, routing, Freerouting, previews.
- **acd** (this repo's companion): project folders, rule profile, gates, revisions, approvals, backups, release export.

When both servers have a tool with the same name (`run_erc`, `run_drc`), always use the **acd** one:
it records the gate. Use the kicad one only for `get_drc_violations` detail.

## Hard rules
1. Never call `acd confirm_spec`, `acd verify_part` or `acd approve` unless Andrew has said so in chat for that exact item. Quote what he approved in the `note`.
2. Never export except through `acd export_release`, and only after `acd approve`.
3. Before writing to a project, run `acd lock_check`. If locked, stop and ask Andrew to close the project in KiCad. Never write while he has it open.
4. Datasheet and part downloads need Andrew's approval first. Web lookups are fine.
5. Back up before every change, never after. The acd tools `confirm_spec`, `check_parts`, `run_erc`, `run_drc` and `approve` back up the project first, automatically. Before any other change in a stage (schematic edits, placement moves, routing edits, review edits), run `acd backup(stage=<stage>)` first, so the backup holds the state before the change. Restoring that backup undoes the stage.
6. Never hide a problem. Every ERC/DRC error, unrouted net, missing datasheet and unverified part goes in the review summary.
7. All dimensions in mm. Never use the long dash character in anything you write.
8. Release files are read-only history. Never edit anything in `releases/`. A new change is a new revision (step 10).

## kicad server quirks (found in T-15, base MCP v2.7.0)
- `sync_schematic_to_board`, `autoroute` and `refill_zones` write the board file outside the session. Call kicad `open_project` straight after each one, before any other board call. Otherwise the next edit is refused ("Auto-save refused") or works on a stale board.
- `create_netclass` does not assign its nets. Always follow it with `assign_net_to_class` for each net. Check that the `autoroute` result shows `netClasses.assignments` above 0, and that `acd run_drc` lists the nets under the class in `netclass_check`.
- KiCad DRC does not check net class track widths. `acd run_drc` does (`netclass_check.narrow_tracks`). The DRC gate fails on any narrow track; there is no neck-down exception. Fix a narrow track by widening it or rerouting. If a fine-pitch neck-down is really needed, ask Andrew first: the gate has no exception mechanism yet, so it will fail until the companion is changed. A net class with no nets fails the DRC gate.
- `add_mounting_hole` uses a footprint that is not in KiCad 10 (`MountingHole_3.2mm`) and adds no schematic symbol. Do not use it; holes are schematic symbols (see Mounting holes).
- The server writes `in_bom yes` on every placed symbol, even where the library says otherwise. Run `acd fix_bom_flags` after adding mounting holes or other non-BOM symbols.
- `sync_schematic_to_board` does not copy custom fields (MPN, Manufacturer, Description, Datasheet) to footprints, so DRC shows `footprint_symbol_field_mismatch`, one per part. The BOM comes from the schematic, so this is a known, accepted warning. List it in the review; do not try to fix it.
- Sync leaves a no-connect pin's pad with no net, where KiCad expects `unconnected-(<ref>-<pin>)`. DRC shows one `net_conflict` warning per no-connect pin. Known, accepted. Any other `net_conflict` is a real problem (usually local labels).
- Sync also adds an empty board net named after the PWR_FLAG symbol. It has no pads and is harmless.
- `check_courtyard_overlaps` reports mounting holes as off the board because of their value text. Ignore boundary flags on H references.

## House conventions (from profiles/pcbway-standard-2layer.yaml)
- 2 layers, 1.6 mm FR4, 1 oz. Board under 100 x 100 mm unless the spec says otherwise.
- Tracks 0.25 mm default (0.15 mm minimum). Clearance 0.2 mm (0.15 mm minimum). Vias 0.6 mm pad, 0.3 mm drill. Copper to edge 0.3 mm.
- Power tracks: the net class width must meet the profile's `tracks.power_width_table` for the spec's `current_a`: up to 1 A, 0.5 mm; up to 2 A, 1.0 mm; up to 3 A, 1.5 mm. Above 3 A, ask Andrew. The DRC gate fails if a class is narrower than its table width, and it lists every narrow track.
- Parts: 0805 passives by default, 0603 only where space requires it. Prefer KiCad `_HandSolder` footprint variants. No QFN, DFN, BGA or 0402 without Andrew's approval. Through-hole only where required (connectors, high-power parts).
- Every BOM part has a Datasheet field on its schematic symbol, including stock passives (`Device:R`, `Device:C`, `Device:L`). For a passive, use the manufacturer's series datasheet URL. `acd check_parts` fails on an empty Datasheet.
- Silkscreen: text 1.0 mm high, 0.15 mm line. Reference designators readable and not on pads.
- Boards are bare (Andrew hand solders). No centroid file unless he asks for assembly.

## Mounting holes (always)
- Default: four M3 holes, non-plated, one per corner, centres 4.0 mm in from each edge.
- Holes are schematic symbols so the board matches the schematic: `Mechanical:MountingHole`, references H1 to H4, footprint `MountingHole:MountingHole_3.2mm_M3`. Add them in step 3, then run `acd fix_bom_flags` so they stay out of the BOM.
- Keep a 7.0 mm diameter area around each hole clear of parts, tracks and pours.
- Andrew advises when a different size is needed. If the description mentions an enclosure, standoffs or a mounting pattern but gives no size, ask before placing.
- The spec can override count, size, plating and positions. Grounded (plated) holes only if Andrew asks: then use `MountingHole_3.2mm_M3_Pad_Via` and tie to GND.
- Position the holes right after the outline and sync in step 5, before any parts.
- Hide each hole's reference text right after positioning it: kicad `move_footprint_text(reference="H1", field="reference", visible=false)`, and the same for H2 to H4. At a 4 mm inset the text gets clipped by the board edge (silk warning).
- Copper pours must stay out of the keep-outs: give `add_copper_pour` a polygon outline that cuts a square of (inset + 3.5) mm out of each corner (7.5 mm at the default inset), 0.5 mm in from the board edge.

## Circuit spec format
Write `spec/circuit-spec.yaml` in the project folder:
```yaml
name: example-board
description: 5 V in, 3.3 V LDO, status LED, 4-pin header
board:
  max_size_mm: [50, 40]
  mounting_holes: {size: M3, count: 4, plated: false}   # or explicit positions
  connector_edges: {J1: left, J2: right}
  keepouts: []
supplies: [{net: VIN, volts: 5.0}, {net: +3V3, volts: 3.3}]
net_classes:
  - {name: Power, nets: [VIN, +3V3, GND], current_a: 0.5}
parts:
  - {ref: U1, function: 3.3 V LDO, mpn: AMS1117-3.3, package: SOT-223}
  - {ref: R1, function: LED resistor, value: 1k, package: "0805"}
connections:
  - {net: VIN, pins: [J1.1, U1.3, C1.1]}
  - {net: GND, pins: [J1.2, U1.1, C1.2, C2.2, D1.1]}
notes: []
```

## Workflow

### 1. Describe and confirm
- Read Andrew's description. Ask for anything missing that changes the design: supply voltages, currents, connector types, board size limit, mounting.
- `acd init_project(name)`, then kicad `create_project` in the returned folder, then `acd apply_profile`.
- Write the spec. Show Andrew a short summary: parts list, nets, board size, mounting holes, assumptions.
- When he confirms: `acd confirm_spec`. It backs up the spec stage first.

### 2. Library
- For each part, look for a symbol and footprint: kicad `search_symbols`, `search_footprints`, `get_footprint_info`. Prefer KiCad's official libraries.
- `Device:R`, `Device:C`, `Device:L` with standard footprints need no verification, but they still need a Datasheet field (see House conventions).
- The parts registry is keyed by symbol (lib_id), so a generic symbol such as `Device:LED` or `Connector_Generic:Conn_01x04` holds one MPN. If a board needs a different part on the same generic symbol, ask Andrew before re-registering it (it changes the entry for every board).
- For anything else not already verified in the library registry:
  - Find it with kicad `search_jlcpcb_parts` or `get_datasheet_url`. Ask before downloading.
  - If it is missing from the libraries, import it with easyeda2kicad by LCSC number into ACD_LIBRARY_ROOT (ask first).
  - Check the pin map and footprint pads against the datasheet: pin numbers and names, pad size and pitch, pin 1 location, package outline.
  - `acd register_part(...)`, then show Andrew the comparison (datasheet pinout vs symbol, land pattern vs footprint).
  - Only after his sign-off: `acd verify_part(lib_id, verified_by="Andrew")`.
- Set `Manufacturer`, `MPN` and `Datasheet` fields on every BOM part.
- Before the first schematic change in this stage, `acd backup(stage=library)`.

### 3. Schematic
- Before the first edit, `acd backup(stage=schematic)`.
- `kicad create_project` already made the schematic file. Place and wire parts in one call with kicad `batch_add_and_connect(..., labelType="global_label")`, or `batch_add_components` then `batch_connect(labelType="global_label")`.
- Always use global labels for nets. KiCad names a local-label net `/VIN`, but the kicad server names the board net `VIN`. That gives `net_conflict` DRC warnings, and after an Update PCB from Schematic (F8) the nets get renamed and lose their net class.
- `add_no_connect` on unused pins. Add `power:PWR_FLAG` on every supply that only comes in through a connector (usually the input rail and GND). The server also warns about rails driven by a regulator output; those need no flag.
- Add the mounting hole symbols (see Mounting holes), then `acd fix_bom_flags`.
- Set `Manufacturer`, `MPN` and `Datasheet` on every BOM part with kicad `batch_edit_schematic_components` (`{"U1": {"properties": {"Manufacturer": "...", "MPN": "...", "Datasheet": "..."}}}`).
- Check the nets with kicad `list_schematic_nets` against the spec's connections before running ERC.
- Layout for readability: power input left, outputs right, one functional block per area, net labels instead of long wires, decoupling caps next to their IC.
- `acd check_parts`, then `acd run_erc`. Both back up the schematic first. Fix every error. Explain any warning you leave.
- Show Andrew `get_schematic_view`.

### 4. Rules and net classes
- `acd apply_profile` already set the defaults. Add the spec's net classes with kicad `create_netclass`, widths per the power table above (at least the width the profile table gives for the spec's `current_a`).
- Then `assign_net_to_class` for every net in the class, one call per net (`create_netclass` does not assign them). The warning "in-memory net class assignment failed" is expected; the project file is what counts.

### 5. Outline, mounting holes, placement
- kicad `open_project`, then `add_board_outline` (`rounded_rectangle` with `cornerRadius` 1 unless the spec says otherwise). Put the top-left corner on a round number (for example 100, 100) so positions are easy to read.
- kicad `sync_schematic_to_board`, then `open_project`. All footprints, holes included, arrive at 0, 0.
- Before the first placement move, `acd backup(stage=placement)`.
- Move H1 to H4 to their positions and hide their reference text (Mounting holes).
- Place in this order, with kicad `place_component`, `move_component`, `rotate_component`:
  1. Connectors on their spec edges, mating face flush with or just past the outline.
  2. Main ICs, near the middle of their block.
  3. Decoupling caps within 3 mm of their IC power pin, on the same side.
  4. Supporting passives close to the pins they serve, in signal-flow order.
  5. LEDs and switches where Andrew can see or reach them.
- Keep parts 0.5 mm from the outline and out of the hole keep-outs. Align and space parts evenly; same-value passives in the same orientation.
- Check a proposed placement first with kicad `check_courtyard_overlaps(positions=..., margin=0.25)`, then place everything in one `batch_move_components` call.
- Show Andrew `get_board_2d_view`.

### 6. Routing
- Ask Andrew: auto route, manual (he routes in KiCad), or auto then he tidies. Default to auto.
- Before autoroute or any routing edit, `acd backup(stage=routed)`.
- Auto: kicad `check_freerouting`, then `autoroute(maxPasses=20, timeout=40)`, then `open_project`. Check `netClasses.assignments` in the result is above 0; if it is 0, the net classes are not assigned (step 4), so fix that, `acd restore_backup` the placement backup and route again.
- GND pours: kicad `add_copper_pour` on F.Cu and B.Cu with net GND, clearance 0.3 and the corner-cut polygon (Mounting holes).
- Then this exact order: `refill_zones`, `open_project`, `add_gnd_stitching_vias(strategies=["in_zones"], spacing=7, edgeMargin=1.5)`, `save_board`, `refill_zones`, `open_project`. Stitching needs filled zones, and DRC needs fills that include the vias.
- Run `refill_zones` (then `open_project`) after any later change to tracks, pours or placement, and always right before `acd run_drc`.
- Manual: tell Andrew the project is ready; wait until he says he has closed it.
- `acd run_drc` (it backs up the routed stage first). Fix errors (kicad `get_drc_violations`, `modify_trace`, `route_pad_to_pad`, `add_via`). Re-run until clean or until only items needing Andrew remain.

### 7. Review loop
- `acd review_summary` and send Andrew this:
```
Review - <project> rev <X>
ERC: <n> errors, <n> warnings
DRC: <n> errors, <n> warnings
Silk warnings: <n> (<type: item> per line, from silk_warnings)
Other warnings: <type: count> (from erc/drc warning_types; say which are the known accepted ones)
Net classes: <class: width, nets> (from netclass); narrow tracks: <net: min width, segments> (any narrow track fails the DRC gate)
Parts missing a datasheet: <list or none>
Unrouted nets: <n> (<list>)
Unverified parts: <list or none>
Board: <w> x <h> mm, <n> holes M3, <n> parts
Open items: <list>
Preview: <board view>
```
- Silk warnings do not block the DRC gate, so always list them. Fix them (move or hide the text) unless Andrew accepts them.
- Andrew may edit in KiCad. While he does, do not write to the project. When he says he is done, `acd lock_check`, then re-run `acd check_parts`, `acd run_erc`, `acd run_drc` and resend the summary. Offer to re-run the autorouter if he moved parts.
- Schematic changes: before editing, `acd backup(stage=review)`. Then edit the schematic, `sync_schematic_to_board` and `open_project` (placement is kept), then re-check.

### 8. Approval
- Approval needs all gates passed and not stale (`acd gate_status`): spec confirmed, parts verified, ERC clean, DRC clean, zero unrouted.
- Ask Andrew to approve, naming the revision. On his yes: `acd approve(approver="Andrew", note="<his words>")`. It backs up the approved stage before it writes the approval.

### 9. Export
- `acd export_release`. Report the release folder and the `rev-X_pcbway.zip` to upload to PCBWay.
- The release is designed to be reproducible: every date in it is the approval date, and the zips have fixed timestamps, so a re-export of the same approved revision should give identical files. This was tested only with the fake KiCad CLI; check it on the first real release after the update. If `acd export_release` reports that the release folder exists, leave it alone and start a new revision if a change is needed.
- `acd verify_release` to confirm the files match the manifest.
- Check that the bundle opens in a Gerber reader (KiCad's gerbview, or gerbonara on Andrew's PC) and that the Edge_Cuts size matches the spec.
- Remind Andrew to check the zip in PCBWay's online Gerber viewer before ordering.

### 10. Improve a design later
- `acd list_backups` / read `records/revisions.json` to find the approved revision.
- `acd new_revision(from_rev=<X>)`. It backs up the current state first, marks the old revision superseded, and restores the approved files. Then loop from step 1, 2 or 3 depending on the change. The old release stays untouched.

### Restore
- `acd list_backups`, show Andrew the choices, and on his pick `acd restore_backup`. It saves a pre-restore backup first.

## When something fails
- kicad server tool errors: retry once, then report the exact error and stop. Do not work around the server by editing KiCad files directly.
- kicad `search_symbols` or `search_footprints` finds nothing, even for `Device:R`: KiCad has no global library tables yet (it has never been opened since install). Ask Andrew to open and close KiCad once, then restart Claude Desktop so the kicad server reloads them.
- `check_freerouting` fails: tell Andrew Java or the Freerouting jar is missing and point him to setup/SETUP.md. Offer manual routing meanwhile.
- A gate keeps failing: list the exact items, propose fixes, and ask before making changes the spec did not cover.
