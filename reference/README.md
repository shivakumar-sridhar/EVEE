# reference/

Artefacts kept because they are **evidence**, not because anything imports them.

## `cura_BNO_Case.gcode`

Cura 5.13.0, 2026-08-11. The BNO085 sensor case body, sliced for this exact Ender-3
V3 SE, printed, and came out clean. It is the control sample for every argument about
`config/ender3_v3se.ini`.

It settled the first-layer drool bug after three fixes designed from theory had each
cost a cancelled print. Its first eleven lines answer questions that are expensive to
answer any other way:

```gcode
G28 ;Home
M420 S1; Use saved mesh leveling data      <- loads a stored mesh, never probes
G92 E0 ;Reset Extruder
G1 Z2.0 F3000 ;Move Z Axis up
G1 X-3 Y20 Z0.28 F5000.0 ;Move to start position   <- in position BEFORE heating
M190 S60 ; Set bed temperature and wait
M109 S200 ; Set hotend temperature and wait        <- 200C, not 210
G1 X-3 Y100.0 Z0.28 F1500.0 E15 ;Draw the first line   <- extrudes immediately
```

Three facts, none of which we had guessed right:

- **200C**, and `M104 S200` later in the file shows the *whole* print ran at 200.
- **Zero moves between reaching temperature and extruding.** The nozzle waits at the
  prime line's start, at print height, and the next command draws through whatever
  oozed there.
- **No `G29` anywhere.** Cura is hot for about thirty seconds; probing kept ours hot
  for about 187.

`config/ender3_v3se.ini` is now a translation of this block. The differences are
forced, not chosen: `X2.0` instead of `X-3` because our `bed_shape` starts at 0, and
`E15` twice instead of `E15`/`E30` because we run `M83` relative where Cura runs
absolute — the same 30mm.

**Do not delete this to save half a megabyte.** It lived in `output/`, which is
gitignored, and was therefore invisible to anyone cloning the repo and easy to lose
locally. It is here so that it survives.

**Do not slice from it either.** It is a record of a print that happened, not an input
to anything. Nothing in `src/` reads this directory.
