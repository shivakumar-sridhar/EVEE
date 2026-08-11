# EVI — voice-to-print

Natural language → parametric CAD → sliced G-code → a physical print on an Ender 3.

Describe a part in a sentence, approve the geometry, approve the slice, and it prints.
The pipeline is exposed as MCP tools, so the same interface works from a voice loop or
from an agent like Claude Code.

**Status: Phase 1 of 7.** The CAD layer works and is tested. Slicing, printer control,
LLM extraction, and the MCP server are not built yet — see [Roadmap](#roadmap).
[`BUILD_PLAN.md`](BUILD_PLAN.md) is the full spec.

---

## Safety

This project is designed to eventually start prints on a real machine that heats a
hotend to 200°C+ unattended. That shapes the architecture, and it should shape how you
use it.

- **Do not skip Phase 0.** Confirm `THERMAL_RUNAWAY_PROTECTION` is enabled in your
  firmware, inspect the hotend wiring, and have a working smoke detector in the room
  before any code touches the printer. Some older Creality boards shipped with thermal
  runaway protection disabled.
- **`start_print()` will require an explicit `bed_confirmed_clear=True`,** with no
  default. No agent can look at your bed. That argument is a human asserting they did.
- **Two mandatory approval gates**, after design and after slicing. The pipeline never
  chains design → slice → print in one call.

These are enforced in code, not just in prompts.

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

## Roadmap

| Phase | | |
|---|---|---|
| 0 | Hardware prep — human checklist, blocking | ☐ |
| 1 | Parametric template + CAD dispatch | ☑ |
| 2 | PrusaSlicer CLI wrapper, G-code metadata | ☐ |
| 3 | OctoPrint REST client | ☐ |
| 4 | LLM parameter extraction (local, constrained) | ☐ |
| 5 | MCP server + the two approval gates | ☐ |
| 6 | Async completion notification | ☐ |
| 7 | Voice, push-to-talk | ☐ |

Phase 1 is code-complete but its acceptance criterion is physical: print the body and
lid and confirm the lid fits — snug, not loose, not forced.

Templates grow by use. Anything you design twice becomes one; after ~10 parts you have
covered most of what you actually print. Honest expectation: for a simple box this is
*slower* than editing two numbers in a parametric file. It starts paying off around the
tenth part, when "a wrist camera mount at 15 degrees" resolves in one sentence.

## Stack

[build123d](https://build123d.readthedocs.io) · PrusaSlicer CLI ·
[OctoPrint](https://octoprint.org) REST · [MCP](https://modelcontextprotocol.io) stdio server ·
Qwen3 8B via [Ollama](https://ollama.com) for extraction
