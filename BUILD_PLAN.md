# Voice-to-Print Pipeline — Build Plan

Agentic workflow: natural language part description → parametric CAD → approval → slice → approval → print on an Ender 3 via a Raspberry Pi.

This document is the spec. Work through phases in order. **Do not skip ahead** — each phase produces a verified artifact the next phase depends on.

---

## Locked decisions

Do not re-litigate these during implementation.

| Layer | Choice | Why |
|---|---|---|
| CAD | `build123d` | Python-native B-rep, clean API, headless-friendly |
| Slicer | PrusaSlicer CLI (or OrcaSlicer CLI) | Scriptable, stable config `.ini` format |
| Printer host | OctoPrint on the Pi | No firmware flashing needed, clean REST API over USB serial |
| Interface | MCP server (`stdio`) | Same tools work from a voice loop and from Claude Code |
| Language | Python 3.11+ | build123d and the MCP SDK both target it |
| Param extraction | LLM → JSON → **vetted template** | Not freeform geometry codegen |
| Extraction model | Qwen3 8B via Ollama, local | Task is small; constrained decoding matters more than size |

**Where code runs:** CAD + slicing on the laptop (PrusaSlicer on ARM is a pain). The Pi only runs OctoPrint. Laptop talks to the Pi over HTTP on the LAN.

---

## Repo layout

```
voice-to-print/
├── CLAUDE.md                   # agent context (see appendix)
├── pyproject.toml
├── config/
│   ├── ender3_profile.ini      # dialed-in slicer profile — hand-tuned, not generated
│   └── defaults.toml           # wall thickness, clearances, house rules
├── src/vtp/
│   ├── templates/
│   │   ├── __init__.py         # TEMPLATE_REGISTRY
│   │   └── box.py              # first template
│   ├── cad.py                  # template dispatch → STL + preview PNGs
│   ├── slicer.py               # STL → G-code + metadata
│   ├── printer.py              # OctoPrint REST client
│   ├── extract.py              # NL → template params (LLM)
│   └── server.py               # MCP server exposing the tools
├── output/                     # generated STL / PNG / gcode, gitignored
└── tests/
```

---

## Phase 0 — Hardware prep (human only, blocking)

**No code until every box is checked.** An agent that can start prints on command makes it very easy to start one carelessly.

- [ ] Identify Ender 3 mainboard revision and firmware version
- [ ] Confirm `THERMAL_RUNAWAY_PROTECTION` is enabled. Some older Creality boards shipped with it disabled in stock firmware — if you're on an old v1.1.4 with 2018-era Marlin, reflash to current Marlin before proceeding.
- [ ] Inspect hotend heater cartridge and thermistor wiring for wear or crimp damage (classic Ender 3 failure point)
- [ ] Working smoke detector in the room
- [ ] Printer on a non-combustible surface, not pushed against a wall
- [ ] OctoPrint installed on the Pi, connected over USB, can jog axes and read temps from the web UI
- [ ] Generate an OctoPrint API key (Settings → Application Keys). Store as `OCTOPRINT_API_KEY` in `.env` — never commit it.
- [ ] PrusaSlicer installed on the laptop; slice one STL by hand and confirm the result prints correctly. **That saved `.ini` becomes `config/ender3_profile.ini`.**

---

## Phase 1 — Parametric template, verified by print

Goal: one hand-written, physically verified template. This is the ground truth everything else is debugged against.

**Build `src/vtp/templates/box.py`:**

```python
def box_with_lid(
    outer_l: float, outer_w: float, outer_h: float,
    wall: float = 2.0,
    lid_style: Literal["press_fit", "sliding"] = "press_fit",
    clearance: float = 0.25,
    fillet: float = 1.0,
) -> tuple[Part, Part]:  # (body, lid)
```

Requirements:
- Explicit about **outer** dimensions; compute and report inner usable volume
- Lid lip inset by `wall/2 + clearance`
- Filleted vertical edges (prints better, no sharp corners)
- Body and lid as separate solids, exported as separate STLs
- Reject invalid geometry: `wall * 2 >= min(outer_l, outer_w)` → raise with a clear message

**Also build `src/vtp/cad.py`:**
- `render_preview(stl_path) -> list[Path]` — two PNGs (isometric + top-down)
- Use `trimesh` for loading. Headless rendering is fiddly; if `pyrender`/`pyglet` offscreen gives you trouble, fall back to matplotlib `mplot3d` plotting the mesh triangles. Crude is fine — this is for "does the shape look right," not beauty.

**Acceptance:** Print the body and lid at 50×40×20mm. The lid fits — snug, not loose, not forced. If the fit is wrong, adjust the default `clearance` in `config/defaults.toml` and reprint. **Do not proceed until a physical part is correct.**

---

## Phase 2 — Slice pipeline (no LLM)

`src/vtp/slicer.py`:

```python
def slice_stl(stl_path: Path, profile: Path, output: Path) -> SliceResult
```

- Shell out: `prusa-slicer --export-gcode --load {profile} --output {output} {stl}`
- Parse the G-code footer comments for: estimated print time, filament used (mm and grams), layer count
- Return a `SliceResult` dataclass with those fields + the G-code path
- Non-zero exit or missing output → raise with stderr included

**Acceptance:** Slicing the Phase 1 STL produces G-code byte-comparable in quality to what you got slicing by hand in the GUI. Metadata parses correctly.

---

## Phase 3 — Printer control

`src/vtp/printer.py` — thin OctoPrint REST client. Auth via `X-Api-Key` header.

| Method | Endpoint |
|---|---|
| `get_status()` | `GET /api/printer` — temps, state flags |
| `get_job()` | `GET /api/job` — progress, time left |
| `upload_gcode(path)` | `POST /api/files/local` (multipart, `select=true`, `print=false`) |
| `start_print(filename)` | `POST /api/job` `{"command": "start"}` |
| `cancel_print()` | `POST /api/job` `{"command": "cancel"}` |

**Hard constraints — encode these in the client, not just in prompts:**

1. `start_print()` requires an explicit `bed_confirmed_clear: bool` argument. If `False`, raise. No default value.
2. `start_print()` refuses if `get_status()` reports state is not `Operational` (already printing, error, disconnected).
3. Upload and start are **separate calls**. Never use OctoPrint's `print=true` upload flag. `select=true` marks the file active without printing it — that two-step separation is the point, don't collapse it.

**Upload detail:** multipart, form field named `file`.

**Keep the interface narrow.** Five methods, and map OctoPrint's response shapes into your own `PrinterStatus` dataclass at the client boundary. No OctoPrint-specific JSON should leak into `server.py`. If you later migrate to Klipper/Moonraker, only this file changes.

**Polling:** OctoPrint has no push channel, so Phase 6 needs a loop. Centralize it in one place with a configurable interval — 5s during a print, 30s idle. `/api/printer` is cheap but not free on a Pi also handling serial. Keeping it in one function means it can become a Moonraker websocket subscription later without restructuring.

**Acceptance:** Upload the Phase 2 G-code, start it manually via the client, watch progress poll correctly, cancel it mid-print.

---

## Phase 4 — LLM parameter extraction

`src/vtp/extract.py`. This is the only LLM-touching module.

The model's job is **natural language → JSON params for a known template**, nothing else. It never writes geometry code, never plans the workflow, never decides whether to print.

### Model: local, 8B class

This task is small enough to run locally. Default to **Qwen3 8B via Ollama**. Test **Qwen3 4B** — it may well be sufficient and is fast enough to vanish into the latency budget. Llama 3.1 8B is the fallback if Qwen misbehaves.

- ~6GB at Q4. Fine on 16GB unified memory or an 8GB+ VRAM GPU. CPU-only works but expect 3–5s, which gets annoying once voice is in front of it.
- Set `keep_alive` so the model stays resident. Cold-loading per request is what makes local feel slow, not inference.
- Context is ~1,500 tokens (template registry + house defaults). Each call is independent — **no conversation history**. Nothing accumulates.

**Make the backend a config value.** One `LLM_BACKEND` env var, one `extract()` signature, Ollama and a frontier API behind the same interface. Develop against local; flipping to an API for this one call costs approximately nothing at 1,500 tokens per part.

### Constrained decoding is mandatory

This matters more than model size. Use Ollama's structured output (`format` = JSON schema) or llama.cpp GBNF grammars to force schema-valid output at the token level.

With it, an 8B model *cannot* emit malformed JSON or invent a field — the worst failure becomes "slightly wrong number," which Gate 1 catches. Without it, you'll spend a week fighting trailing commas and markdown fences.

### Output contract

Three legal branches, all schema-enforced:

1. `{"template": "box_with_lid", "params": {...}}` — normal path, validated against that template's Pydantic model
2. `{"needs_clarification": true, "question": "..."}` — request is underspecified. **This branch must exist.** Without a legal way to punt, a small model will guess and confabulate.
3. `{"needs_template": true, "reason": "..."}` — no template fits. Stop. No freeform codegen fallback in v1.

### Build the read-back in Python, not the model

The resolved-spec sentence is the highest-value output in the pipeline:

> "Outer 50×40×20mm, 2mm walls, press-fit lid at 0.25mm clearance, 1mm edge fillet. Usable interior 46×36×18mm."

Template it from the **validated params** rather than asking the model to write prose. 8B models write this flatly, and generating it in code is both better and free. It also guarantees the sentence reflects what will actually be built, not what the model intended.

### Eval set — build this before tuning anything

`tests/eval_extraction.json`: 20 real phrasings with expected params. `pytest` asserts the match.

Twenty minutes of work. It turns "which model is better" from a vibe into a number, and it catches regressions when you add templates and the registry prompt grows. Include deliberately underspecified requests that should return branch 2.

---

## Phase 5 — MCP server + approval gates

`src/vtp/server.py`. Tools exposed:

- `design_part(description: str)` → resolved spec, STL paths, preview PNG paths
- `slice_part(stl_path: str)` → time estimate, grams, layer count, gcode path
- `start_print(gcode_path: str, bed_confirmed_clear: bool)` → job id
- `get_printer_status()` → state, temps, progress

**Two mandatory human gates. Neither is skippable.**

**Gate 1 — after `design_part`:** show preview PNGs + resolved spec + bounding box. Catches geometry errors.

**Gate 2 — after `slice_part`:** show time / grams / layers. Catches "this is a 6-hour print" and "this needs supports." Cheapest gate, saves the most wasted time.

The server never chains `design → slice → print` in one call. Three separate tool invocations, human input between each.

---

## Phase 6 — Async notification

The thing that makes this feel like an assistant rather than a script.

- Background poller on `get_job()`
- On completion or failure: push notification (ntfy.sh is the least-effort option — one HTTP POST, no account)
- Include job name, duration, and OctoPrint webcam snapshot if available

---

## Phase 7 — Voice (last, optional)

Only after 0–6 work reliably by text. Push-to-talk, not always-on wake word, for v1.

- `faster-whisper` (large-v3-turbo) for STT
- Kokoro or Piper for TTS — **not Coqui XTTS**, which is abandoned and non-commercially licensed
- Point it at the same MCP server. No pipeline changes should be needed.

---

## Explicit non-goals for v1

- Freeform CAD codegen for arbitrary shapes
- Multi-agent orchestration frameworks
- Print failure detection (add Obico later — purpose-built, plugs into OctoPrint)
- Auto-starting prints without confirmation
- Multi-part assemblies or mating constraints

---

## Growth path

Every part you design twice becomes a template. After ~10 parts you'll have covered most of what you actually print. Realistic near-term additions given SO-100 work:

- `bracket_l(...)` — L-bracket with slotted holes
- `servo_mount(servo="STS3215", angle=0)` — Feetech bolt pattern
- `camera_mount(cam="...", tilt_deg=15)`
- `standoff_plate(hole_pattern=[...])`

Honest expectation: for a simple box this pipeline is *slower* than opening a parametric file and changing two numbers. It starts paying off at the tenth part, when "design me a wrist cam mount at 15 degrees" resolves in one sentence.

---

## Appendix — `CLAUDE.md` for the repo root

See `CLAUDE.md` in this repo — it is maintained there, and carries a "Current state" section
recording which phases are done and which Phase 1 decisions overrode the text above.

---

## Suggested first prompt to Claude Code

> Read BUILD_PLAN.md. We're starting Phase 1 only. Scaffold the repo per the layout, then implement `src/vtp/templates/box.py` and `src/vtp/cad.py`. Write pytest tests asserting bounding box and that lid outer dims minus clearance match body inner dims. Do not implement slicing, printer control, or the MCP server yet. Stop when tests pass and I'll print the result.

Keep it to one phase per session. The temptation is to let it build the whole thing at once, and then you're debugging a voice pipeline and a slicer config simultaneously with no known-good baseline.
