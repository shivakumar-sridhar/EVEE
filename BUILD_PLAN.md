# Voice-to-Print Pipeline — Build Plan

Agentic workflow: natural language part description → parametric CAD → approval → slice → approval → print on an Ender-3 V3 SE via a Raspberry Pi.

**The MCP server is the product. The model is bring-your-own.** Everything above the
server — voice, agent CLI, model provider — is swappable by the user. See
[System design](#system-design).

This document is the spec. Work through phases in order. **Do not skip ahead** — each phase produces a verified artifact the next phase depends on.

---

## Locked decisions

Do not re-litigate these during implementation.

| Layer | Choice | Why |
|---|---|---|
| CAD | `build123d` | Python-native B-rep, clean API, headless-friendly |
| Slicer | PrusaSlicer CLI (or OrcaSlicer CLI) | Scriptable, stable config `.ini` format |
| Printer host | OctoPrint on the Pi | No firmware flashing needed, clean REST API over USB serial |
| Interface | MCP server (`stdio`) | **This is the product.** Any MCP client drives it — Claude Code, OpenCode, Cline, Zed |
| Language | Python 3.11+ | build123d and the MCP SDK both target it |
| Param extraction | LLM → JSON → **vetted template** | Not freeform geometry codegen |
| Extraction model | **whatever the user's CLI runs** | BYO. The server validates rather than trusting anyone's decoder |

**Where code runs:** CAD + slicing on the laptop (PrusaSlicer on ARM is a pain). The Pi only runs OctoPrint. Laptop talks to the Pi over HTTP on the LAN.

---

## System design

### Layers

```
                          HUMAN
          speaks ──── sees preview ──── confirms print
             │             ▲                  ▲
             ▼             │                  │
   ┌─────────────────────────────────────────────────────┐
   │ VOICE FRONTEND                   (Phase 7, optional) │
   │   faster-whisper STT  ──►  text                      │
   │   Piper / Kokoro TTS  ◄──  text                      │
   └─────────────────────────────────────────────────────┘
             │ text
             ▼
   ┌─────────────────────────────────────────────────────┐
   │ AGENT CLI — bring your own model                     │
   │   Claude Code │ OpenCode │ Cline │ Zed │ …           │
   │   reads tool schemas ─► picks a template             │
   │                      ─► fills params with numbers    │
   │   never writes geometry, never decides to print      │
   └─────────────────────────────────────────────────────┘
             │ MCP over stdio — JSON params only
             ▼
   ╔═════════════════════════════════════════════════════╗
   ║ vtp MCP SERVER                    src/vtp/server.py  ║
   ║   list_templates()                        Phase 5    ║
   ║   design_part(template, params)           Phase 5    ║
   ║   slice_part(stl_path)                    Phase 5    ║
   ║   get_printer_status()                    Phase 5    ║
   ║   start_print(gcode, bed_confirmed_clear) Phase 5    ║
   ╚═════════════════════════════════════════════════════╝
        │                  │                   │
        ▼                  ▼                   ▼
     cad.py             slicer.py           printer.py
     Phase 1            Phase 2             Phase 3
        │                  │                   │
     build123d          PrusaSlicer         OctoPrint REST
     templates/         ender3_v3se.ini     (Pi, over LAN)
        │                  │                   │
     STL + PNG          G-code              Ender-3 V3 SE
```

### Request lifecycle

```
 1  human: "case for my BNO085, ports on both ends"
 2  STT → text                                        [voice only]
 3  agent → list_templates()          → box_with_lid + description
 4  agent reads box_with_lid JSON schema
 5  agent computes numbers, calls design_part(template, params)
    ────────────────────────────────────────────────── server side
 6  Pydantic validate  (extra="forbid")   ─┐ bad input → error naming
 7  _validate() cross-field checks         │ the value → agent fixes
 8  build123d → body + lid solids          │ and retries
 9  export STL + render preview PNGs      ─┘
10  return spec sentence, bboxes, paths
    ──────────────────────────────────────────────────
11  ▶ GATE 1  human sees preview PNG + spec sentence.  SCREEN REQUIRED.
12  agent → slice_part(stl)  → PrusaSlicer + hand-tuned profile
13  ▶ GATE 2  human sees time / grams / layers
14  agent → get_printer_status()  → must be Operational
15  agent → upload_gcode()        → separate call, print=false
16  ▶ GATE 3  human supplies bed_confirmed_clear=True   NON-VOICE ONLY
17  agent → start_print()  → OctoPrint → Ender-3 V3 SE
18  background poller → ntfy push on done/fail          [Phase 6] DONE
```

### Trust boundaries

The property that makes bring-your-own-model safe: **the agent emits numbers, never
shape.** All geometry authority lives server-side.

| Boundary | What crosses | Enforced by | On failure |
|---|---|---|---|
| Human → Agent | ambiguous English | nothing, by design | Gate 1 read-back catches it |
| Agent → Server | JSON params | `extra="forbid"` + `_validate()` | tool error, agent self-corrects |
| Server → Machine | G-code, REST | `bed_confirmed_clear`, state check, split upload/start | print refused |

---

## Portability — the server cannot assume Claude

Once this is open sourced, the connecting client is arbitrary. That changes where
safety is allowed to live.

| Ships with the server — every client sees it | Claude Code only |
|---|---|
| MCP tool descriptions | `CLAUDE.md` |
| JSON schemas + field descriptions | |
| Python validation and its error messages | |
| `config/defaults.toml` house rules | |

**Rule:** if a constraint is safety-critical it goes in the left column. `CLAUDE.md` is
developer context, never a control. A rule that exists only there does not exist for an
OpenCode user.

Two consequences to build to:

- **Server-side validation is the only guarantee.** The original plan leaned on
  constrained decoding to stop a model inventing a field. We do not control the
  client's decoder, so `extra="forbid"` and the cross-field `_validate()` are the real
  gate, not a second line of defence.
- **Tool descriptions carry the house rules.** Dimensions are OUTER unless stated,
  defaults come from `config/defaults.toml`, templates are a fixed whitelist and there
  is no freeform geometry path. Every client reads these; none of them read `CLAUDE.md`.

---

## Repo layout

```
voice-to-print/
├── CLAUDE.md                   # agent context (see appendix)
├── pyproject.toml
├── config/
│   ├── ender3_v3se.ini         # dialed-in slicer profile — hand-tuned, not generated
│   └── defaults.toml           # wall thickness, clearances, house rules
├── src/vtp/
│   ├── templates/
│   │   ├── __init__.py         # TEMPLATE_REGISTRY
│   │   └── box.py              # first template
│   ├── cad.py                  # template dispatch → STL + preview PNGs
│   ├── slicer.py               # STL → G-code + metadata
│   ├── printer.py              # OctoPrint REST client
│   ├── extract.py              # OPTIONAL, Phase 7 only — NL → params without a CLI
│   └── server.py               # MCP server exposing the tools
├── output/                     # generated STL / PNG / gcode, gitignored
└── tests/
```

---

## Phase 0 — Hardware prep (human only, blocking)

**No code until every box is checked.** An agent that can start prints on command makes it very easy to start one carelessly.

**The machine is a Creality Ender-3 V3 SE** — confirmed from the `TARGET_MACHINE.NAME`
header of a known-good print. Not a classic Ender 3: different mainboard, CR Touch
auto-levelling, higher stock accelerations, and start G-code that is not interchangeable.

- [x] Identify mainboard revision and firmware version — Ender-3 V3 SE, stock Marlin
- [ ] Confirm `THERMAL_RUNAWAY_PROTECTION` is enabled. Stock V3 SE firmware ships with it on; verify rather than assume, and reflash to current Marlin if anything looks off.
- [ ] Inspect hotend heater cartridge and thermistor wiring for wear or crimp damage (a classic Creality failure point across the whole line)
- [ ] Working smoke detector in the room
- [ ] Printer on a non-combustible surface, not pushed against a wall
- [x] OctoPrint installed on the Pi, connected over USB, can jog axes and read temps from the web UI — OctoPrint 1.11.8, `/dev/ttyUSB0` @ 115200, state Operational
- [x] Generate an OctoPrint API key. Stored as `OCTOPRINT_API_KEY` in `.env`; handshake verified with `GET /api/version` → HTTP 200
- [ ] PrusaSlicer installed on the laptop; slice one STL by hand and confirm the result prints correctly. **That saved `.ini` becomes `config/ender3_v3se.ini`.**

**Slicer history:** the first verified print (`BNO_Case.gcode`) came out of Cura
5.13.0, which proves the machine and the geometry but does not give us an automatable
profile — Cura's settings inheritance lives in its GUI, not its engine. PrusaSlicer was
chosen for automation because its CLI is stable and its config is a single flat `.ini`,
matching how this repo already treats the profile. The Cura start G-code is the
reference to port from.

> **Corrected 2026-08-12.** This paragraph used to call `M420 S1` (use saved mesh
> levelling) "the load-bearing line a stock Ender-3 profile will not have". It is not in
> the verified profile and never was — `config/ender3_v3se.ini` probes fresh with `G29`
> on every print, which is what the physically verified print did. `M420 S1` is now
> injected into the *exported G-code* by `slicer.py`, and only when `calibrate_bed` has
> actually stored a mesh. See CLAUDE.md § Bed levelling.

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

## Phase 4 — Parameter extraction (mostly deleted; see Phase 5)

**Superseded.** This phase originally built `src/vtp/extract.py` around a local Qwen3 8B
via Ollama. With the MCP server as the product, the connecting CLI already has a model,
and that model does extraction by reading the tool's JSON schema. A second model inside
the server is a redundant hop.

So there is **no `extract.py` to build here.** What that module was responsible for is
now split:

| Was Phase 4's job | Now |
|---|---|
| NL → template choice + params | the client CLI's model, against `model_json_schema()` |
| Constrained decoding | the client's concern; the server does not trust it |
| Schema validation | `src/vtp/templates/` Pydantic models — server-side, mandatory |
| Read-back sentence | already built: `resolved_spec_sentence()` in the template |
| "needs clarification" branch | the CLI's own conversation; it can just ask |
| "no template fits" branch | `UnknownTemplateError` from the registry |

### What survives, and is now more important

The original safety argument was *"constrained decoding means the model cannot invent a
field."* That guarantee is gone — we do not control an arbitrary client's decoder. Its
replacement is server-side validation, which already exists and is tested:

- `extra="forbid"` on every params model, so an invented field is a hard error
- cross-field `_validate()`, so geometrically impossible params never reach build123d
- error messages that **name the offending value**, because they are the client model's
  only feedback signal for self-correction

Treat those three as load-bearing, not as defensive extras.

### Read-backs stay in Python

Unchanged and still the highest-value output in the pipeline:

> "Outer 50×40×20mm, 2mm walls, press-fit lid at 0.25mm clearance, 1mm edge fillet. Usable interior 46×36×18mm."

Templated from **validated params**, never written by a model. It reflects what will
actually be built rather than what any model intended — which matters more now that the
model could be anything.

### When `extract.py` comes back

Only for a **standalone voice loop with no agent CLI in it** (see Phase 7). If voice
drives Claude Code or another CLI, it is never needed. Do not build it speculatively.

### Eval set — still worth building

`tests/eval_extraction.json`: 20 real phrasings with expected params. Now it evaluates
**the client model**, not a module you own — run it through whichever CLI you are
targeting. It turns "does this work with OpenCode / a local model / Haiku" from a vibe
into a number, and it catches regressions when the registry grows and tool descriptions
get longer. Include deliberately underspecified requests where the right answer is
asking the user rather than guessing.

---

## Phase 5 — MCP server + approval gates

`src/vtp/server.py`. **This is the product** — everything above it is the user's choice
of client and model. Tools exposed:

- `list_templates()` → name → description, plus each template's JSON schema
- `design_part(template: str, params: dict)` → resolved spec, STL paths, preview PNG paths
- `slice_part(stl_path: str)` → time estimate, grams, layer count, gcode path
- `start_print(gcode_path: str, bed_confirmed_clear: bool)` → job id
- `get_printer_status()` → state, temps, progress

**`design_part` takes a template name and params, not a free-text description.** The
client's model picks the template and fills the schema — that is the whole design. A
`description: str` parameter would smuggle extraction back inside the server and
recreate the Phase 4 that was just deleted.

**Tool descriptions are the portable `CLAUDE.md`.** They ship with the server and every
client reads them, so the house rules live there: dimensions are OUTER unless stated,
defaults come from `config/defaults.toml`, templates are a fixed whitelist, there is no
freeform geometry path. See [Portability](#portability--the-server-cannot-assume-claude).

**Build it incrementally.** `list_templates` and `design_part` depend only on Phase 1
and can ship before a slicer or printer exists — that vertical slice is what proves the
bring-your-own-model thesis. Do not add `slice_part` or `start_print` as stubs; an
unimplemented print tool in the schema is an invitation.

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

**Voice is a frontend that drives an agent CLI, not a second pipeline.** It converts
speech to text, hands the text to the CLI, and speaks the reply. It does not talk to the
MCP server directly and it does not need its own model. Two integration options:

| Option | How | Trade-off |
|---|---|---|
| Headless CLI calls | shell out per utterance (`claude -p …`) | Simplest. Each turn is largely standalone |
| Persistent agent session | Claude Agent SDK, one long-lived session | Multi-turn works — *"make it 5mm taller"* resolves against the previous part. Preferred |

The persistent-session path is what makes it feel like an assistant rather than a
command line with a microphone. Verify the exact CLI/SDK surface when this phase
starts rather than trusting the flags written here.

Only if you want voice **without** any agent CLI does `extract.py` come back — a small
local model doing NL → params directly against the template schema. Do not build that
until it is actually needed.

### Two hard constraints on the voice layer

- **Gate 1 needs a screen.** The point of that gate is looking at the preview render,
  and a 3D shape cannot be reviewed by ear. `resolved_spec_sentence` confirms the
  *numbers* well and the *shape* not at all. Keep a display in the loop.
- **Gate 3 must not be reachable from a transcript.** `bed_confirmed_clear` is supplied
  by a human through a non-voice channel — typed token, phone tap, physical button.
  Never inferred from ASR output. *"Sure, go ahead"* is a cheap utterance, STT
  mishears, and the thing on the other end heats to 200°C. Voice may design and slice
  freely; something else starts the print.

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

## Working rhythm

One phase per session, and within a phase, one feature at a time on the human's call.
The temptation is to let an agent build the whole thing at once, and then you are
debugging a voice pipeline and a slicer config simultaneously with no known-good
baseline.

Phase 1 is done and physically verified. The next unblocked work is the Phase 5 vertical
slice — `list_templates` + `design_part` only, no slicer, no printer. That proves an
arbitrary MCP client can design a part end to end, which is the thesis this whole
re-architecture rests on.

Phases 2 and 3 stay blocked until Phase 0 is signed off: **no slicer installed on this
machine and no `config/ender3_v3se.ini`.** That file must come from a hand-tuned
PrusaSlicer export — never generated.
