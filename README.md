# EVI — voice-to-print

Natural language → parametric CAD → sliced G-code → a physical print on an Ender-3 V3 SE.

Describe a part in a sentence, approve the geometry, approve the slice, and it prints.
The pipeline is exposed as MCP tools, so the same interface works from a voice loop or
from an agent like Claude Code.

**Status: the full path works — design, slice, print.** Five MCP tools, three human
gates, all enforced in Python. Notification and voice are the remaining phases — see
[Roadmap](#roadmap). [`BUILD_PLAN.md`](BUILD_PLAN.md) is the full spec.

---

## Safety

This project is designed to eventually start prints on a real machine that heats a
hotend to 200°C+ unattended. That shapes the architecture, and it should shape how you
use it.

- **Do not skip Phase 0.** Confirm `THERMAL_RUNAWAY_PROTECTION` is enabled in your
  firmware, inspect the hotend wiring, and have a working smoke detector in the room
  before any code touches the printer. Some older Creality boards shipped with thermal
  runaway protection disabled.
- **`start_print()` requires an explicit `bed_confirmed_clear=True`,** with no default
  anywhere in the chain. No agent can look at your bed. That argument is a human
  asserting they did, and the check is `is not True` — a truthy value will not pass.
- **Three mandatory approval gates**: after design, after slicing, and before the print
  starts. No tool spans design, slicing and printing, so a model cannot skip one of
  those by picking a shorter call. `cancel_print` exists but takes its own explicit
  `confirmed` flag, checked the same way — ending an eight-hour print is not something
  a model should be able to do by misreading "how's it going?".
- **`start_print` verifies which file it is starting.** OctoPrint's start command takes
  no filename — it runs whatever is currently selected, possibly something a human
  picked in the web UI hours ago. The client selects the file it was asked for and reads
  the selection back before starting.
- **`start_print` uploads and then starts.** Two separate OctoPrint requests with
  `print=false` on the upload — that flag is never used — but one tool call, so the bed
  confirmation is the only thing between the call and a moving machine. Nothing is
  uploaded unless the bed is confirmed and the printer is already idle.

These are enforced in code, not just in prompts. `src/vtp/printer.py` is where they
live; the MCP tool descriptions restate them so every client sees them, but the Python
refusal is the mechanism.

---

## What actually works today

A parametric box-with-lid template, exported to STL with preview renders:

```python
from vtp.cad import design

result = design("box_with_lid", dict(outer_l=50, outer_w=40, outer_h=20))
print(result.summary())
```

```
Outer 50x40x20mm, 2mm walls, press-fit lid at 0.25mm clearance with a 3mm lip,
1mm edge fillet. Usable interior 46x36x18mm.

  body   50 x 40 x 20 mm  ->  box_with_lid_body.stl
  lid    50 x 40 x 5 mm   ->  box_with_lid_lid.stl
```

Writes two STLs and four preview PNGs (isometric + top-down per part) into `output/`.
Both parts come out in print orientation, sitting on Z=0, needing no supports.

## Install

Requires Python 3.12+ and [uv](https://docs.astral.sh/uv/).

```bash
git clone git@github.com:shivakumar-sridhar/EVI.git
cd EVI
uv sync
uv run pytest
```

`build123d` pulls in OpenCascade via prebuilt wheels — no source build, but it is a
large download (~400MB).

Nothing here talks to a printer, a slicer, or a network yet, so `uv sync` and `pytest`
are safe to run on any machine.

## Layout

```
config/defaults.toml       house rules: wall thickness, clearances, fillets
src/vtp/
  config.py                reads defaults.toml
  cad.py                   template dispatch -> STL + preview PNGs
  templates/
    __init__.py            TEMPLATE_REGISTRY
    box.py                 box_with_lid
output/                    generated artifacts (gitignored)
tests/
```

**`config/defaults.toml` is the tuning knob.** If a printed lid comes out too tight,
raise `clearance.press_fit` and regenerate. No code change.

## Design decisions worth knowing

**The LLM never writes geometry.** Its entire job is natural language → JSON parameters
for a template that a human already vetted. Every template's Pydantic model sets
`extra="forbid"`, and that schema drives constrained decoding, so the model cannot emit
malformed JSON or invent a parameter. The worst failure mode is a slightly wrong number,
which the first approval gate catches.

**Read-back sentences are templated in Python, not written by the model.** That
guarantees the sentence describes what will actually be built rather than what the model
intended.

**Cavities are boolean subtractions, not shells.** At a 1.0mm outer fillet with 2.0mm
walls, a negative shell offset implies a −1.0mm inner corner radius, which OpenCascade
only survives via `Kind.INTERSECTION`. Subtracting an explicit inner solid is robust and
makes the inner dimensions exact — which matters, because the lid is dimensioned off them.

**Previews are matplotlib, deliberately crude.** `Poly3DCollection` has no z-buffer, so
back faces are culled manually and shaded by normal; without that a hollow box renders as
a solid block, hiding the exact feature the preview exists to check. Offscreen GL on
headless Linux is not worth the debugging time.

**If no template fits, the pipeline stops** and says so, rather than improvising
geometry. Freeform CAD codegen is an explicit non-goal.

## Connecting a client

The MCP server is the product. It speaks stdio, so **any MCP client can drive it** —
bring your own model.

```bash
uv sync
```

**Claude Code** — the repo ships a project-scoped `.mcp.json`; a session started in this
directory picks it up. Verify with `/mcp`.

**Any other client** (OpenCode, Cline, Zed, …) — register the equivalent:

| | |
|---|---|
| command | `.venv/bin/python` |
| args | `["-m", "vtp.server"]` |
| cwd | the repo root |
| transport | stdio |

Nothing Claude-specific is required. `python -m vtp.server` speaks MCP on its own.

Seven tools, with a human decision between each step:

| Tool | Does | Gate after |
|---|---|---|
| `list_templates()` | returns each template's JSON Schema | — |
| `design_part(template, params)` | builds STLs plus a combined plate, opens them on the bed in PrusaSlicer | **1** — is the shape right? |
| `slice_part(stl_path)` | slices with the verified profile; reports layers, grams, time | **2** — is the cost acceptable? |
| `get_printer_status()` | state, temperatures, current job, stored bed mesh | **3** — is the build plate clear? |
| `start_print(gcode_path, bed_confirmed_clear)` | uploads it to the printer and starts it | — |
| `cancel_print(confirmed)` | stops the running job and parks the head so the plate can be cleaned | — |
| `calibrate_bed(bed_confirmed_clear)` | probes the bed once and stores the mesh, so later prints skip the probe | — |

`design_part` writes one STL per part *and* a `_plate.stl` holding them side by side in
exactly the arrangement the viewer shows. Slicing the plate prints every part in one
job — one warm-up, one bed check — and slicing a single part is the reprint path.

There is no free-text `description` parameter — your client's model picks the template
and fills the schema, and the server validates it. `slice_part` takes no profile
parameter, and nothing combines design, slicing and printing.

## Roadmap

| Phase | | |
|---|---|---|
| 0 | Hardware prep — human checklist, blocking | ☑ |
| 1 | Parametric template + CAD dispatch | ☑ |
| 2 | PrusaSlicer CLI wrapper, G-code metadata | ☑ |
| 3 | OctoPrint REST client | ☑ |
| 4 | ~~LLM parameter extraction~~ — superseded, the client's model does this | — |
| 5 | MCP server + the three approval gates | ☑ |
| 6 | Async completion notification | ☐ |
| 7 | Voice, push-to-talk | ☐ |

Phases 0–2 are verified physically, not just in tests: a BNO085 sensor case was
designed here, sliced with `config/ender3_v3se.ini`, and printed successfully. The
Phase 3 client is verified against the live machine for every read and for upload, and
for each refusal path; the one thing not exercised end to end is a print actually
started by `start_print`, which is a human's call to make.

`start_print` originally split into `upload_gcode` and `start_print` so a human could
see the file reach the printer before committing. That was folded back into one call on
request; every Python guard survived, but the bed confirmation is now the last checkpoint.

Templates grow by use. Anything you design twice becomes one; after ~10 parts you have
covered most of what you actually print. Honest expectation: for a simple box this is
*slower* than editing two numbers in a parametric file. It starts paying off around the
tenth part, when "a wrist camera mount at 15 degrees" resolves in one sentence.

## Stack

[build123d](https://build123d.readthedocs.io) · PrusaSlicer CLI ·
[OctoPrint](https://octoprint.org) REST · [MCP](https://modelcontextprotocol.io) stdio server ·
your own model, through your own client
