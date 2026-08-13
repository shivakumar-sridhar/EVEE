# EVEE — Everyday Virtual Engineering Engine

Natural language → parametric CAD → sliced G-code → a physical print on an Ender-3 V3 SE.

Describe a part in a sentence, approve the geometry, approve the slice, and it prints.
The pipeline is exposed as MCP tools, so any MCP client drives it — Claude Code,
OpenCode, Cline, Zed. Speech, if you want it, comes from the client (`/voice` in Claude
Code); nothing here listens.

**Status: the whole pipeline works — describe, design, slice, print, and get told when
it is done.** Seven MCP tools, three human gates, all enforced in Python.

**This is my workflow for my printer, published so you can take it apart.** It is not a
product aiming to fit every setup. The machine is a specific Ender-3 V3 SE, the slicer
profile is hand-tuned for it, and the numbers in `config/` came from printing things and
looking at them. Clone it, point it at your machine, and re-verify — especially the
slicer profile, which is the one file that will be wrong for you.

[`BUILD_PLAN.md`](BUILD_PLAN.md) is the original spec, kept as history;
[`CLAUDE.md`](CLAUDE.md) is what is actually true now.

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

These are enforced in code, not just in prompts. `src/evee/printer.py` is where they
live; the MCP tool descriptions restate them so every client sees them, but the Python
refusal is the mechanism.

---

## What actually works today

A parametric box-with-lid template, exported to STL with preview renders:

```python
from evee.cad import design

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
git clone git@github.com:shivakumar-sridhar/EVEE.git
cd EVEE
uv sync
uv run pytest
git config core.hooksPath .githooks    # refuse to commit credentials
```

That last line is worth the two seconds. `.githooks/pre-commit` blocks a commit that
stages a credential — by filename (`.env*`, `*.bak`, `*.pem`, …) and by content, meaning
a key pasted into a file whose name looks perfectly innocent is caught too. Git does not
track `.git/hooks`, so `core.hooksPath` is how a hook survives a clone; without that line
the hook is inert.

It exists because this repo pushed a live OctoPrint key once, in a `.env.bak` that a
`git add -A` swept into a commit about something else entirely. `.gitignore` named the
literal file `.env` and nothing else, so the backup walked straight past it. Untracking
cannot reach a pushed commit — the key had to be revoked. A rule would not have stopped
that; a hook does.

`build123d` pulls in OpenCascade via prebuilt wheels — no source build, but it is a
large download (~400MB).

`uv sync` and `pytest` are safe to run on any machine: `tests/conftest.py` repoints the
printer credentials at `printer.invalid` suite-wide, so no test can reach a real machine
even by accident. Slicing and printing need PrusaSlicer and an OctoPrint host; neither is
required to run the suite.

## Layout

```
config/
  defaults.toml            house rules: wall thickness, clearances, fillets
  ender3_v3se.ini          hand-tuned slicer profile — the one file that is mine, not yours
src/evee/
  config.py                reads defaults.toml and .env
  cad.py                   template dispatch -> STLs, plate, preview PNGs
  slicer.py                STL -> G-code + metadata
  viewer.py                opens the review windows for gates 1 and 2
  printer.py               OctoPrint REST client and every print refusal
  calibration.py           stored bed mesh state
  notify.py                standalone poller -> ntfy push (separate process)
  server.py                the MCP server
  templates/box.py         box_with_lid
reference/                 known-good artefacts kept as evidence
output/                    generated STL / PNG / G-code (gitignored)
packaging/                 systemd user unit for the notifier
tests/
```

**`config/defaults.toml` is the tuning knob.** If a printed lid comes out too tight,
raise `clearance.press_fit` and regenerate. No code change.

## Design decisions worth knowing

**The LLM never writes geometry.** Its entire job is natural language → JSON parameters
for a template that a human already vetted. The worst failure mode is a slightly wrong
number, which the first approval gate catches.

**Server-side validation is the only guarantee, not the client's decoder.** An earlier
version of this plan leaned on constrained decoding to stop a model inventing a field.
That was wrong: the client is arbitrary and we do not control its decoder. Every
template's Pydantic model sets `extra="forbid"` and runs cross-field checks, and *that*
is what rejects a bad parameter — for every client, including one whose decoder does
nothing. The error messages name the offending value, because they are the model's only
signal for correcting itself.

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
| args | `["-m", "evee.server"]` |
| cwd | the repo root |
| transport | stdio |

Nothing Claude-specific is required. `python -m evee.server` speaks MCP on its own.

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

## Speech

There is nothing to install. Use your client's own speech input — `/voice` in Claude
Code — and talk to it exactly as you would type.

This repo did ship a push-to-talk frontend once (faster-whisper, Piper, a persistent
Agent SDK session). It worked and it was removed: the client already does it, and
reproducing it here bought nothing.

**Crucially, no safety property left with it.** A spoken session cannot start a print
because `bed_confirmed_clear` is checked with `is not True` in `printer.py`, is
keyword-only, and no tool composes design into print — not because some frontend of ours
filtered the tool list. Those checks hold for any client, including one listening to a
room. *"Sure, go ahead"* is a cheap utterance and speech recognition will produce it from
things you never said; the gate is in Python, where it applies to everyone.

## Notifications

```bash
python -m evee.notify --setup     # one command, start to finish
```

Generates a private topic, walks you through subscribing in the
[ntfy](https://ntfy.sh) app (free, no account), sends a real test notification, and
writes `.env` only once you confirm your phone actually buzzed — so you never end up
half-configured and finding out after a failed print. `--check` re-tests later.

You then get a push when a print starts, finishes, is cancelled, or the machine drops
off the serial link mid-print.

It runs as **its own process**, deliberately — not inside the MCP server. An MCP server
is spawned by your editor and dies with the session, so a poller living in it would only
notify you while you were sitting in front of it, which is when you least need telling.
`packaging/evee-notify.service` is a ready systemd user unit.

The daemon only reads. It cannot start, stop or alter a print. It is also the only thing
that records *how a print ended* — `print_finished` and `print_failed` land in
`output/print_log.jsonl` beside the commands, which is what lets the server suggest
re-running `calibrate_bed` after a print that went wrong.

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
| 6 | Async completion notification | ☑ |
| 7 | ~~Voice, push-to-talk~~ — built, then removed for the client's own | — |

Verified physically, not just in tests: a BNO085 sensor case was designed here, sliced
with `config/ender3_v3se.ini`, and printed. Prints have been started, cancelled and
completed through these tools against the live machine, and every refusal path has been
exercised.

The slicer profile's start sequence took four attempts to get right, and the story is
worth reading before you trust your own: see `CLAUDE.md` § The machine and its profile,
and `reference/cura_BNO_Case.gcode`, which is the known-good print that settled it.

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
