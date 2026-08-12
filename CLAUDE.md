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
3. Never use OctoPrint's `print=true` upload flag. The upload and the start stay two
   separate REST requests, with `print=false` on the upload. **They are driven by one
   MCP tool** — `start_print(gcode_path, bed_confirmed_clear)` uploads then starts;
   see the 2026-08-12 note below.
4. Never suggest starting a print when the user is asleep or away from the building.
5. `start_print` names a file, and the server proves the printer selected that file
   before starting. OctoPrint's start command carries no filename — see `printer.py`.

All five are Python refusals in `src/vtp/printer.py` and are restated in the MCP tool
descriptions. If you ever find one holding only because of this file, that is the bug.

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
- Only use `config/ender3_v3se.ini`. It is hand-tuned. Do not generate slicer configs.
  Its `start_gcode` was edited 2026-08-12 to fix the first-layer ooze, so it is not
  currently *physically* verified — see § The machine.
- `slice_part` exposes **no profile parameter** — that is the enforcement, not this bullet.
  It exposes no levelling parameter either: whether the stored mesh is used is *state*,
  decided by `calibration.mesh_state()`, never a knob a client can turn.
- The normal input is the plate. Its arrangement comes from `cad.arrange_along_x`, the
  same function the review 3MF uses. **Never** hand the parts to PrusaSlicer as separate
  STLs with `--merge`: it would arrange them itself and the preview-equals-print
  guarantee would die with nothing failing to say so.

## Testing
- Never test by starting a real print. Use `slice_part` output metadata for verification.
  The printer's refusal paths *are* testable live — they refuse. The upload leg is
  testable live too, but only below MCP now: call `OctoPrintClient.upload_gcode`
  directly, since the `start_print` tool would go on to print. `start_print` succeeding
  is not something to verify on a whim; the human decides when a print happens.
- No test may touch the real printer by accident. `tests/conftest.py` repoints
  credentials at `printer.invalid` suite-wide, and it patches `vtp.printer`, not
  `vtp.config` — the module binds the name at import, so patching the definition site
  silently does nothing and the tests would hit the real Ender.
- `pytest tests/` for unit tests; geometry tests assert on volume and bounding box, not exact meshes.

---

## Current state (Phases 0–3 and 5 done; Phase 6 next)

### The 2026-08-12 fixes, from the first real print

The first print through `start_print` ran and was cancelled. Four problems came out of
it; all four are fixed. Seven MCP tools now, not five.

1. **One plate, not one part per job.** `cad.arrange_along_x` is shared by the review
   3MF and the new `<stem>_plate.stl`, and that sharing is the guarantee that what Gate
   1 shows is what prints. Slicing the plate is the normal path; a single part is the
   reprint path. Verified: plate is 75 layers, the same as the tallest part alone, not
   the sum — `;LAYER_CHANGE` counting stays correct because `complete_objects = 0`.
2. **The bed probe is optional** — see § Bed levelling below.
3. **`cancel_print` is a tool, and parks the head** — see § Printer control.
4. **The first-layer ooze fix** — see § The machine.

Not done, and deliberately: completion tracking. `_audit` still records only starts and
cancels, so "the last print failed" is inferred from a `cancel_print` entry being the
newest event. That is a proxy, said out loud in `calibration.py`, and the recommendation
stays quiet on ambiguity rather than nagging. Real completion tracking is Phase 6.

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
  4.25 g, 30m44s) and the print completed. That verification covered the profile *as it
  stood then*; the 2026-08-12 `start_gcode` ooze fix has not been through a print yet.
- **Phase 5, design half — `src/vtp/server.py`.** MCP stdio server exposing
  `list_templates` and `design_part`. Registered in `.mcp.json`. Verified end to end
  against a real MCP client over a subprocess.

- **Phase 2 — complete.** `src/vtp/slicer.py` + the `slice_part` MCP tool. Acceptance
  met: the Phase 1 body slices to 55 layers, 4.25 g, 30m 44s — matching the physically
  verified print exactly.
- **Gate 1 opens a real window.** `src/vtp/viewer.py` opens the review 3MF in
  PrusaSlicer's GUI after `design_part`. One window, replaced on each iteration.
  Previews are still written and are still the fallback when no display exists.
  Gate 2's toolpath window is opt-in and off by default — `slice_part` reports the
  time and filament weight instead.
- **`box_with_lid` grew a `standoffs` parameter (`StandoffSpec`)** — posts on the cavity
  floor for mounting a PCB, positioned from the interior centre, with an optional blind
  pilot hole for a self-tapping screw. Sunk into the floor on union; the bore stops
  `_BOSS_HOLE_STOP` above it, so the base is never perforated. Post size is tuned in
  `[standoff]` in `defaults.toml`. Second worked example of the add-a-feature shape.
- **Bed limits are enforced in Python too.** `config.bed_extents()` reads `bed_shape`
  and `max_print_height` out of the verified profile — never a `220` literal — and
  `slice_stl` refuses an oversized part before the slicer runs, naming the axis and the
  overshoot.

- **Phase 3 — complete.** `src/vtp/printer.py`, and with it the rest of Phase 5:
  `get_printer_status` and `start_print`. Five tools, three gates.
  Verified against the live machine for every read, for a real upload, and for every
  refusal path. **A print has never been started by `start_print`** — that is the
  human's to do, and the testing rule below still holds.

Not started:
- Phase 6 notify, Phase 7 voice.
- Phase 4 is superseded — see below.

### Slicing and the viewer (`slicer.py`, `viewer.py`)

- **PrusaSlicer's metadata is not at the end of the file.** The `filament used` /
  `estimated printing time` block sits *before* `; prusaslicer_config = begin` and the
  several hundred config lines after it. A tail-reading parser finds nothing. Worse,
  `; filament used [g]` appears a second time inside that config block with a different
  value, so first match wins, not last.
- **There is no layer-count comment.** Counting `;LAYER_CHANGE` markers is the only way.
- **PrusaSlicer writes progress to stdout**, which under stdio transport is JSON-RPC.
  `capture_output=True` is load-bearing, not tidiness.
- **An MCP client scrubs the environment.** The SDK spawns a stdio server with only
  `HOME`, `LOGNAME`, `PATH`, `SHELL`, `TERM`, `USER` — no `DISPLAY`, `WAYLAND_DISPLAY`,
  `XDG_RUNTIME_DIR` or `XAUTHORITY`. Reading `os.environ` therefore reports "headless"
  on a desktop machine, so `discover_display()` finds the session's sockets on disk.
- **`XAUTHORITY` is required and its absence is silent.** f3d handed a `DISPLAY` it
  cannot authenticate against **exits 0 with no window and no error**. So a `DISPLAY`
  without a cookie is worse than none, and discovery drops it.
- **Wayland alone does not work** with f3d — its VTK backend is X11-only and exits
  immediately on `WAYLAND_DISPLAY` by itself. Both are advertised; XWayland does the work.
- **A viewer that cannot open must never fail a design or a slice.** The STLs and the
  G-code are already correct. `open_model` / `open_gcode` never raise; they return a
  reason and the tool reports it.
- **PrusaSlicer is the review app, but never the slicer of record.** `--load {profile}`
  puts the verified profile in the GUI session. The actual slice stays a headless
  `--export-gcode` call, because a GUI export returns no metadata, leaves no record,
  and would follow whatever profile the UI has loaded.
- **`--single-instance` was removed — corrected 2026-08-12.** An earlier version of this
  file recommended it for reusing one window across design iterations. It does reuse the
  window, but it *appends* to that window's scene and never clears it: two designs left
  four objects stacked on the origin. It also made the launched process forward its
  arguments and exit at once, leaving no pid to close later. Do not add it back.
- **Each gate owns one window and replaces it.** `viewer.py` records the pid and argv of
  the window it opened in `$XDG_RUNTIME_DIR/vtp-viewer.json`, keyed by gate, and closes
  it before opening the next — SIGTERM, then SIGKILL. **Identity is the recorded argv,
  never the pid alone**: pids get reused, and closing a PrusaSlicer the human opened for
  their own work would be far worse than a stale window. Compare `/proc/<pid>/cmdline`
  before signalling anything.
- **Do not read `/proc/<pid>/cmdline` straight after spawning.** Until `execve` lands it
  still reads back the *parent's* argv. `_launch` records the argv it is about to run,
  which sidesteps this; a test that samples /proc instead has to wait for the exec.
- **Both parts cannot be handed to the viewer as STLs.** They are each centred on the
  origin in print pose, so they land in the same place and you see one shape where there
  are two. `cad.export_review_model()` writes a single `_review.3mf` with the parts
  spaced along X, and that is what the viewer opens. It is review-only — never sliced,
  and the STLs stay untouched at the origin where a slicer needs them.
- **Gate 2's window is off by default** (`gcode_auto_open = false`). Slicing was always
  headless; this is only the toolpath viewer. The layer count, grams and time that
  `slice_part` returns are what say the slice is sane — that is how Phase 2 was verified.
  `auto_open` and `gcode_auto_open` say different things: one is "this machine has a
  screen", the other "I want to look at toolpaths". The refusal message names whichever
  one vetoed, so nobody edits the wrong line.
- **PrusaSlicer's process is named `slic3r_main`**, not `prusa-slicer`. `pgrep -x
  prusa-slicer` finds nothing and will fool you into thinking the window died.
- **Reap the viewers you spawn.** Nothing waits on them otherwise and each closed window
  lingers as a zombie under the long-lived server process.

### Printer control (`printer.py`)

- **OctoPrint's start command takes no filename.** `POST /api/job {"command":"start"}`
  prints whatever file is *currently selected*, which may be one a human picked in the
  web UI hours ago. This is the least obvious hazard in the whole pipeline and the one
  most likely to be reintroduced by someone simplifying the code. `start_print` selects
  the file it was asked for, reads the selection back, and refuses on a mismatch without
  sending the start. Tests assert the refusal *and* that no `POST /api/job` went out.
- **The bed confirmation is checked with `is not True`**, not for truthiness. A model
  that passes `"yes"` has produced something truthy without anyone having looked at a
  build plate. It is also keyword-only, so it cannot arrive by argument position.
- **`.env` is read off disk, not from `os.environ`** — the same MCP-scrubs-the-
  environment problem that `discover_display()` solves for `DISPLAY`. A key exported in
  a shell never reaches a server the client spawned. `config._dotenv()` does not export
  into `os.environ` either, so the key stays out of the environment of the slicer and
  viewer subprocesses.
- **`GET /api/printer` answers 409, not 200, when the printer is disconnected.** That is
  a state worth reporting, not an error worth raising on, so `get_status()` falls back to
  `/api/connection` for the state text.
- **`mcp` 2.0 brings `httpx2`, not `httpx`** (2.10.0, same API). It is now named in
  `pyproject.toml` directly rather than relied on transitively.
- **httpx2's request log goes to stderr**, via logging's last-resort handler — verified
  by speaking JSON-RPC to the server by hand and checking stdout carried only the
  response. Do not add a stdout logging handler.
- **Uploads read back the stored filename.** OctoPrint sanitises names, so
  `OctoPrintClient.upload_gcode` returns what the printer called the file, and the
  select-and-start sequence uses that name, never the local stem.
- **`output/print_log.jsonl` records every start and cancel** before the command goes
  out. Best-effort — an unwritable log never blocks an approved print — and refusals are
  not recorded, because the log is what was started, not what was asked for.
- **`cancel_print` became an MCP tool on 2026-08-12**, reversing the rule that used to
  sit here ("exists in the client but is not an MCP tool; a human at the machine has a
  button"). The objection stands — a model must not end a long print on a misread
  sentence — so the answer is a `confirmed` flag checked with `is not True`, keyword-only
  in the client, exactly like `bed_confirmed_clear`. What made it worth having is that
  the machine's own button does not park the head: a bare cancel leaves a hot nozzle
  over the ruined part with the plate unreachable.
- **Parking is the only G-code this package emits**, and it is `printer._park_commands()`,
  a module constant. No public method takes a command string and none should — a freeform
  G-code path would end the property that this server only does what it documents.
  `test_no_public_method_accepts_a_gcode_command` enforces it by introspection, because
  a comment is not a mechanism.
- **The park has to wait for real idle.** `PrinterStatus.printing` folds in OctoPrint's
  `cancelling` and `finishing` flags, which is exactly the state a fresh cancel leaves
  and exactly when OctoPrint rejects commands. `_wait_until_idle` polls a **fixed attempt
  count**, not a wall-clock deadline — `conftest` patches `time.sleep` to a no-op, so a
  deadline loop would spin thousands of mock requests instead of iterating twice.
- **A park failure is reported, never raised.** By then the print is already stopped,
  which is what was asked for.
- **`store_bed_mesh` confirms with a temperature sentinel.** OctoPrint answers 204 the
  moment it queues `G28`/`G29`/`M500` and exposes no "probe finished" flag, so a fourth
  command `M104 S42` is appended and the nozzle *target* is polled until it reads back.
  OctoPrint learns that target from the printer's own report, so it cannot appear until
  Marlin has executed past `G29`. A failed probe never reaches it, the wait times out,
  and **no mesh is recorded** — the fail-safe holds by construction rather than by care.
- **G-code we write must be ASCII.** It goes down a serial link. An em-dash in the
  `M420 S1` comment is what caught this; prose punctuation is fine everywhere else in
  this repo and has no business in a `.gcode` file.
- **`start_print` uploads as well as starts — owner's decision, 2026-08-12.** There was
  briefly a separate `upload_gcode` MCP tool with Gate 3 sitting between the two. The
  owner asked for one call; the cost was stated and accepted. This also restores
  `BUILD_PLAN.md` Phase 5's original signature, which always took a `gcode_path`.
  `OctoPrintClient.upload_gcode` and `.start_print` are still separate methods and the
  two REST requests are still separate — `upload_and_print` just composes them.
- **The bed check and the idle check both run before the upload**, not only inside
  `start_print`. Transferring 500 KB to a Pi and only then refusing would leave a file
  the human never asked for on the SD card. The check inside `start_print` still runs
  after, for the window where a print began mid-upload.
- **The upload leg can no longer be exercised live through MCP without printing.** That
  is the real cost of folding the tools together. Verify it one level down by calling
  `OctoPrintClient.upload_gcode` directly — it selects the file and prints nothing.

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
its CLI is stable and its config is one flat `.ini`.

**Bed levelling — corrected 2026-08-11.** An earlier version of this file said `M420 S1`
(load saved mesh) was load-bearing in the profile. It is not there, and it is not
needed: `config/ender3_v3se.ini` runs `G28 ; home all axis` then `G29 ; auto bed
levelling` in `start_gcode`, probing the CR Touch fresh on every print. That is what the
physically verified print did. `M420 S1` is the alternative — faster, but it depends on
a mesh having been stored with `G29` + `M500` beforehand. Do not "fix" the profile by
adding it; the levelling is already handled.

**Stored meshes — added 2026-08-12, reversing the paragraph above in part.** The owner
asked for the per-print probe to be optional. `M420 S1` is now used, but **still not in
the profile**. The profile keeps `G29`, which is always correct and is also the
substitution anchor. The swap happens in `slicer._apply_stored_mesh`, on the exported
G-code, rewriting exactly one line — verified by diff: 1 line of 24,810 changes and the
metadata is identical.

- `calibrate_bed` runs `G28`/`G29`/`M500` once. Only then does a mesh exist.
- Every uncertain case falls back to probing: no state file, an unreadable one, one
  older than `[bed_mesh] max_age_days`, or an anchor that is missing or doubled.
- **This direction is the whole design.** `M420 S1` against a mesh Marlin does not have
  does not fail — it prints on a flat plane and says so only on the serial console.
  Slower and correct beats faster and silently wrong.
- **`output/bed_mesh.json` is a claim, not a reading.** OctoPrint cannot read a mesh
  back out of the printer, so nothing can verify one still exists. A firmware update or
  an `M502` invalidates it while the file still says "stored yesterday". That residual
  risk is real; the age limit and the `get_printer_status` prompt are the mitigations.
- `tests/conftest.py` isolates `vtp.calibration.MESH_STATE` for the same reason it
  isolates the audit log, but with a physical consequence: a test that left a state file
  behind would make the next *real* slice emit `M420 S1` for a mesh that was never
  stored.

**The nozzle waits near the plate — fixed 2026-08-12.** `start_gcode` used to run
`G1 Z50 F240` after `G29`, park over the start of the prime line, and only *then* ramp
150 → 210C and wait out both `M190` and `M109`. Everything that oozed during that wait
fell 50mm onto the exact spot the prime line begins, and the prime line dragged the blob
into the part. First layers were visibly bad while the G-code metadata — layers, grams,
time — looked completely normal, which is why this survived a physically verified print.

The lift is now `G1 Z2.0 F240`, so the wait happens 2mm off the plate and ooze is pinned
where the prime line will wipe it. That is what Cura did, and why the same geometry
printed cleanly there. Two moves were also appended after the prime lines — a `Z2.0`
lift and a travel to `X10 Y10` — so nothing left on the prime lane is dragged into the
first layer.

Re-slicing the case body across the change: 70 layers and 5.23 g / 1754.53 mm both
identical, time 38m10s → 37m46s. The 24s is the `Z50` round-trip at F240 that no longer
happens; no extrusion changed. **`tests/test_profile.py` guards all of it**, including
that `Z50` never comes back.

**The profile's "physically verified" status has lapsed** as of this edit and stays
lapsed until a real print runs with it and is watched through the first layer. The
geometry, the bed size and every extrusion setting are untouched, so the risk is narrow
— but "verified" means a human watched it, and nobody has watched this one yet.

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
