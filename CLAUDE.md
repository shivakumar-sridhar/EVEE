# Project: voice-to-print

Natural language → parametric CAD → sliced G-code → Ender 3 print, via MCP tools.

## Stack
build123d (CAD) · PrusaSlicer CLI (slicing) · OctoPrint REST (printer) · MCP stdio server

## Non-negotiable safety rules
1. `start_print()` requires explicit `bed_confirmed_clear=True`. Never default it. No agent can clear a bed.
2. Never chain design → slice → print without human approval between each step.
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
- Only use `config/ender3_profile.ini`. It is hand-tuned and physically verified. Do not generate slicer configs.

## Testing
- Never test by starting a real print. Use `slice_part` output metadata for verification.
- `pytest tests/` for unit tests; geometry tests assert on volume and bounding box, not exact meshes.

---

## Current state (Phase 1)

`BUILD_PLAN.md` is the spec. Work through its phases in order, **one phase per session**.

Done:
- Phase 1 — `templates/box.py` (`box_with_lid`) and `cad.py`. Geometry + previews + tests.

Not started:
- Phase 0 — hardware checklist is the user's, and is **blocking** for Phase 3.
  Still outstanding: no slicer installed on this machine, and `config/ender3_profile.ini`
  does not exist. It must come from a hand-tuned PrusaSlicer export, never generated.
- Phase 2 slicing, Phase 3 printer, Phase 4 extraction, Phase 5 MCP server, Phase 6 notify, Phase 7 voice.

### Phase 1 decisions that override the plan text
- **Capping lid**, not rebated flush. The lid is a full-outer-dimension plate; its lip drops
  into the cavity, inset from the lid edge by `wall + clearance`. BUILD_PLAN.md's "lip inset
  by `wall/2 + clearance`" described a rebated design inconsistent with its own test assertion.
- **The cavity is a boolean subtraction, not a shell/`offset()`.** With `fillet=1.0` and
  `wall=2.0` a negative shell offset implies a −1.0mm inner radius, which OCC only survives
  via `Kind.INTERSECTION`. Subtraction is robust and gives exact inner dimensions.
- **`lid_style="sliding"` raises `NotImplementedError`.** It stays in the signature and the
  Pydantic model so Phase 4's schema is stable, but the geometry is deferred until the
  press-fit box has been physically verified.
- `lip_height` was added to the signature — the plan's signature had no way to express
  engagement depth.

### Conventions worth keeping
- Every template exposes a Pydantic params model with `extra="forbid"`. Phase 4 feeds
  `model_json_schema()` to Ollama's constrained decoding, so the model literally cannot
  invent a field.
- Read-back sentences are templated in Python from *validated* params
  (`resolved_spec_sentence`), never written by the LLM.
- Previews are matplotlib-only (`Agg`). No pyrender/pyglet — offscreen GL on headless Linux
  is not worth the debugging time, and these images only need to answer "is the shape right".
