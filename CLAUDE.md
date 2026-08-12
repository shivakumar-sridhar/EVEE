# Project: voice-to-print

Natural language → parametric CAD → sliced G-code → Ender-3 V3 SE print, via MCP tools.

**The MCP server is the product; the model is bring-your-own.** Any MCP client drives it
— Claude Code, OpenCode, Cline, Zed. See `BUILD_PLAN.md` § System design.

## Stack
build123d (CAD) · PrusaSlicer CLI (slicing) · OctoPrint REST (printer) · MCP stdio server

## This file is not a control

**Only Claude Code loads `CLAUDE.md`.** An OpenCode or Cline user gets none of it. So a
safety rule that lives only here does not exist for most users of this project.

Every constraint below must be **enforced in Python** (`server.py` or the module it
wraps) and **communicated in the MCP tool description**, which ships with the server and
every client reads. Treat this file as developer context, never as the mechanism.

If you find yourself relying on a rule here to keep something safe, that is a bug in
`server.py`.

## Non-negotiable safety rules
1. `start_print()` requires explicit `bed_confirmed_clear=True`. Never default it. No agent can clear a bed. It must never be inferred from a voice transcript — a human supplies it through a non-voice channel.
2. Never chain design → slice → print without human approval between each step. The server exposes no tool that combines them.
3. Never use OctoPrint's `print=true` upload flag. Upload and start are separate.
4. Never suggest starting a print when the user is asleep or away from the building.

## House defaults (config/defaults.toml is the source of truth)
- Wall thickness: 2.0mm
- Press-fit clearance: 0.25mm (0.2 snug / 0.3 easy)
- Edge fillet: 1.0mm
- Dimensions in the user's request are OUTER unless stated otherwise

## CAD conventions
- Use build123d, not FreeCAD's API or CadQuery
- Prefer filling a template in `src/vtp/templates/` over writing new geometry
- If no template fits, say so and propose adding one — do not improvise geometry
- Always report resolved inner dimensions alongside outer

## Slicing
- Only use `config/ender3_v3se.ini`. It is hand-tuned and physically verified. Do not generate slicer configs.

## Testing
- Never test by starting a real print. Use `slice_part` output metadata for verification.
- `pytest tests/` for unit tests; geometry tests assert on volume and bounding box, not exact meshes.

---

## Current state (Phase 1 done; Phase 5 next)

`BUILD_PLAN.md` is the spec. Work through its phases in order, **one phase per session**,
and within a phase **one feature at a time on the user's call** — do not run ahead.

Done:
- Phase 1 — `templates/box.py` (`box_with_lid`) and `cad.py`. Geometry + previews + tests.
  Physically verified: a BNO085 sensor case printed and fitted correctly.
- `box_with_lid` grew a `ports` parameter (`PortSpec`) — rectangular wall openings for
  cables and connectors, subtracted after filleting, validated so they never breach the
  top rim.

- **Phase 0 — complete.** OctoPrint 1.11.8 verified at the URL in `.env`, Ender on
  `/dev/ttyUSB0` @ 115200. PrusaSlicer 2.9.4 installed; `config/ender3_v3se.ini` is
  hand-tuned, exported, and **physically verified** — it sliced the case body (55 layers,
  4.25 g, 30m44s) and the print completed.
- **Phase 5, design half — `src/vtp/server.py`.** MCP stdio server exposing
  `list_templates` and `design_part`. Registered in `.mcp.json`. Verified end to end
  against a real MCP client over a subprocess.

Not started:
- Phase 2 slicing — **unblocked now** that the profile is verified. `slicer.py` parses
  PrusaSlicer's *footer* (`; estimated printing time`, `; filament used [g]`), confirmed
  present in real output.
- Phase 3 printer client — unblocked. The full guard sequence has been run by hand
  against the live printer and works; encode exactly it. `BNO_Case.gcode` is on the Pi
  as a known-good artifact.
- Phase 5 remainder — `slice_part`, `get_printer_status`, `start_print` tools.
  **Do not add them as stubs**; an unimplemented print tool in the schema is an invitation.
- Phase 6 notify, Phase 7 voice.
- Phase 4 is superseded — see below.

### MCP server notes (`server.py`)

- **`mcp` 2.0 renamed things.** `FastMCP` is gone; it is `MCPServer` from `mcp.server`.
  Fields are snake_case (`input_schema`, `is_error`), not camelCase.
- **Nothing may write to stdout** — stdio transport is JSON-RPC on that stream.
  build123d's builder logging goes to stderr, which is why it is safe; keep it that way.
- Called in-process a failing tool raises `ToolError`; over a transport the kernel turns
  the same failure into `CallToolResult(is_error=True)`. Tests handle both.
- Validation errors are reformatted by `_describe_error` to strip Pydantic's
  `(root)` / `Value error,` noise. Those messages are the client model's only
  self-correction signal — keep them naming the offending value.

### The machine

**Creality Ender-3 V3 SE**, not a classic Ender 3 — confirmed from the
`TARGET_MACHINE.NAME` header of the verified print. CR Touch auto-levelling, different
mainboard, higher stock accelerations, non-interchangeable start G-code. Bed 220×220.

The first verified print came out of **Cura 5.13.0**, which proves the machine and the
geometry but gives no automatable profile. PrusaSlicer was chosen for automation because
its CLI is stable and its config is one flat `.ini`. When porting the profile, `M420 S1`
(use saved mesh levelling) is load-bearing — a stock Ender-3 profile lacks it, and
without it the CR Touch mesh is ignored and the first layer fails.

### The architecture change (supersedes BUILD_PLAN Phase 4)

The pipeline no longer contains its own extraction model. The connecting CLI's model
picks a template and fills its params from the JSON schema; the server validates.

- **No `extract.py`, no Ollama.** It returns only if a standalone voice loop ships with
  no agent CLI in it (Phase 7).
- **`design_part(template, params)` — never a free-text `description`.** A free-text
  parameter would put extraction back inside the server.
- **Server-side validation is the only guarantee.** The old plan leaned on constrained
  decoding to stop a model inventing a field; we do not control an arbitrary client's
  decoder. `extra="forbid"` and the cross-field `_validate()` are the real gate now.
- **Validation error messages are an API.** They are the client model's only feedback for
  self-correction, so they must name the offending value.
- **Voice is a frontend that drives a CLI**, not a parallel pipeline. Gate 1 needs a
  screen — a 3D shape cannot be reviewed by ear.

### Phase 1 decisions that override the plan text
- **Capping lid**, not rebated flush. The lid is a full-outer-dimension plate; its lip drops
  into the cavity, inset from the lid edge by `wall + clearance`. BUILD_PLAN.md's "lip inset
  by `wall/2 + clearance`" described a rebated design inconsistent with its own test assertion.
- **The cavity is a boolean subtraction, not a shell/`offset()`.** With `fillet=1.0` and
  `wall=2.0` a negative shell offset implies a −1.0mm inner radius, which OCC only survives
  via `Kind.INTERSECTION`. Subtraction is robust and gives exact inner dimensions.
- **`lid_style="sliding"` raises `NotImplementedError`.** It stays in the signature and the
  Pydantic model so the schema is stable, but the geometry is deferred until the
  press-fit box has been physically verified.
- `lip_height` was added to the signature — the plan's signature had no way to express
  engagement depth.

### Conventions worth keeping
- Every template exposes a Pydantic params model with `extra="forbid"`, including nested
  models like `PortSpec`. `model_json_schema()` is what the client model fills, and
  server-side rejection of unknown fields is the guarantee — not the client's decoder.
- Read-back sentences are templated in Python from *validated* params
  (`resolved_spec_sentence`), never written by a model.
- Adding a feature to a template is a routine change with a fixed shape: extend the
  params model → add cross-field checks to `_validate()` with a message naming the bad
  value → subtract/add geometry in the build function → assert on volume and bounding
  box in `tests/`. `ports` is the worked example. New *dimensions* are free; new
  *features* cost a change like this, and that is the intended trade.
- Previews are matplotlib-only (`Agg`). No pyrender/pyglet — offscreen GL on headless Linux
  is not worth the debugging time, and these images only need to answer "is the shape right".
