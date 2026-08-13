"""Phase 2 — STL in, G-code out, plus what the print will cost.

No model anywhere in this file. Slicing is a deterministic shell-out to PrusaSlicer
against one hand-tuned profile, and the numbers it reports are read back out of the
G-code rather than estimated.

**The profile is not a parameter the client chooses.** :func:`slice_stl` takes one so
tests can point at a fixture, but it defaults to the hand-tuned
``config/ender3_v3se.ini`` and the MCP tool exposes no way to override it. A generated
or stock profile has none of this machine's start sequence — the CR Touch probe, the
anti-ooze idle temperature, the prime lines — and its first layer fails. So "which
profile" is not a decision worth delegating.

**Bed levelling is decided here, after the export, not in the profile.** The profile
keeps ``G29``, which is always correct. When ``calibrate_bed`` has actually stored a
mesh and it is not stale, :func:`_apply_stored_mesh` rewrites that one line to
``M420 S1`` so the print skips a multi-minute probe. Every uncertain case leaves the
file untouched, because ``M420 S1`` against a mesh the printer does not have fails
silently — it prints on a flat plane and complains only to the serial console. See
:mod:`evee.calibration`.

**PrusaSlicer writes progress to stdout.** Under the stdio transport that stream is
JSON-RPC, so the child's output is captured, never inherited. Letting it through would
corrupt the protocol rather than merely look untidy.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from evee.calibration import mesh_state
from evee.config import (
    OUTPUT_DIR,
    bed_violations,
    slicer_profile,
    slicer_timeout,
)

__all__ = ["SliceResult", "SlicerError", "slice_stl"]


class SlicerError(RuntimeError):
    """PrusaSlicer failed, or produced G-code whose metadata we could not read."""


# The metadata block sits just before `; prusaslicer_config = begin`, which is itself
# followed by several hundred config lines. It is therefore NOT at the end of the file,
# and a parser that reads the tail finds nothing. We scan the whole file once instead.
_FIELDS: dict[str, re.Pattern[str]] = {
    "filament_mm": re.compile(r"^; filament used \[mm\] = ([0-9.]+)\s*$"),
    "filament_cm3": re.compile(r"^; filament used \[cm3\] = ([0-9.]+)\s*$"),
    "filament_g": re.compile(r"^; filament used \[g\] = ([0-9.]+)\s*$"),
}
_TIME = re.compile(r"^; estimated printing time \(normal mode\) = (.+?)\s*$")

#: PrusaSlicer emits no layer-count comment of any kind. Counting the per-layer
#: markers is the only way to get it, and it agrees with the verified print (55).
_LAYER_MARKER = ";LAYER_CHANGE"

#: "30m 44s", "1h 2m 3s", "2d 4h 30m 10s" — whitespace between tokens is optional.
_DURATION_TOKEN = re.compile(r"(\d+)\s*([dhms])")
_DURATION_SCALE = {"d": 86400, "h": 3600, "m": 60, "s": 1}

#: The exact line the verified profile emits, and the anchor for the stored-mesh swap.
#: Its spelling is part of an interface even though it looks like ordinary G-code —
#: ``tests/test_profile.py`` asserts the profile still produces it.
_G29_LINE = "G29 ; auto bed levelling"


@dataclass(frozen=True)
class SliceResult:
    """What Gate 2 shows the human before anything is uploaded to a printer."""

    stl_path: Path
    gcode_path: Path
    profile: Path
    print_time_seconds: int
    print_time_text: str
    filament_g: float
    filament_mm: float
    filament_cm3: float
    layer_count: int
    #: ``"probe"`` (the profile's G29, unchanged) or ``"stored"`` (M420 S1 swapped in).
    #: Defaulted so callers constructing this by keyword keep working.
    levelling: str = "probe"
    levelling_detail: str = ""

    def summary(self) -> str:
        """Read-back line for the approval gate.

        Templated here in Python from parsed values, never written by a model —
        the same rule the design gate's spec sentence follows.
        """
        line = (
            f"{self.stl_path.name} -> {self.gcode_path.name}: "
            f"{self.layer_count} layers, {self.filament_g:g} g "
            f"({self.filament_mm:g} mm), about {self.print_time_text}."
        )
        if self.levelling == "stored":
            # Worth saying out loud: the time above does NOT include the probe and
            # never did, so this saving is invisible in the estimate.
            line += " Bed: loading the stored mesh instead of probing."
        return line


def _apply_stored_mesh(gcode_path: Path) -> tuple[str, str]:
    """Swap the full bed probe for the stored mesh, if there is a trustworthy one.

    Returns ``(mode, detail)`` where mode is ``"probe"`` or ``"stored"``.

    Done here, on the exported file, rather than in the profile. The profile is the
    one hand-tuned artefact in this repo and it stays the source of the start
    sequence: it keeps ``G29``, which is always correct, and this rewrites exactly one
    line of the output when — and only when — a mesh has demonstrably been stored.
    A second profile would fork the verified file; passing ``--start-gcode`` would put
    a second copy of that twenty-line sequence in Python, where it would drift.

    Every uncertain case leaves the file alone. If the anchor is missing or appears
    more than once the profile has changed underneath us, and the safe reading of that
    is "probe the bed", not "guess which line was meant".
    """
    state = mesh_state()
    if not state.usable:
        return "probe", state.reason

    text = gcode_path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines(keepends=True)
    hits = [i for i, line in enumerate(lines) if line.strip() == _G29_LINE]

    if len(hits) != 1:
        return "probe", (
            f"expected exactly one {_G29_LINE!r} line in the exported G-code but "
            f"found {len(hits)}, so the bed probe was left alone. The profile's "
            f"start_gcode may have changed."
        )

    when = state.stored_at.strftime("%Y-%m-%d %H:%M UTC") if state.stored_at else "?"
    ending = "\n" if lines[hits[0]].endswith("\n") else ""
    # ASCII only: this line goes down a serial link to Marlin. Prose punctuation that
    # is fine everywhere else in this repo has no business in a G-code file.
    lines[hits[0]] = f"M420 S1 ; use stored bed mesh ({when}), G29 skipped{ending}"

    # Written beside the target and moved into place: a half-rewritten G-code file
    # must never be something a human can upload.
    scratch = gcode_path.with_suffix(gcode_path.suffix + ".tmp")
    scratch.write_text("".join(lines), encoding="utf-8")
    os.replace(scratch, gcode_path)

    return "stored", (
        f"loading the bed mesh stored {when} instead of probing, which saves the "
        f"probe time at the start of the print."
    )


def _parse_duration(text: str) -> int:
    """"30m 44s" -> 1844. Raises if no recognisable token is present."""
    tokens = _DURATION_TOKEN.findall(text)
    if not tokens:
        raise SlicerError(
            f"could not read a duration from estimated printing time {text!r}; "
            f"expected tokens like '1h 2m 3s'"
        )
    return sum(int(value) * _DURATION_SCALE[unit] for value, unit in tokens)


def _parse_gcode(path: Path) -> dict[str, object]:
    """Pull filament, time and layer count out of an exported G-code file.

    One pass over the file: the metadata lines are matched and the layer markers
    counted together, so a large G-code file is read exactly once.
    """
    found: dict[str, float] = {}
    time_text: str | None = None
    layers = 0

    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if line.startswith(_LAYER_MARKER):
                layers += 1
                continue
            if not line.startswith(";"):
                continue
            if time_text is None:
                match = _TIME.match(line)
                if match:
                    time_text = match.group(1)
                    continue
            for key, pattern in _FIELDS.items():
                if key in found:
                    continue
                match = pattern.match(line)
                if match:
                    found[key] = float(match.group(1))
                    break

    missing = [key for key in _FIELDS if key not in found]
    if time_text is None:
        missing.append("estimated printing time")
    if missing:
        raise SlicerError(
            f"{path.name} is missing expected PrusaSlicer metadata: "
            f"{', '.join(missing)}. Was it sliced by PrusaSlicer?"
        )
    if layers == 0:
        raise SlicerError(
            f"{path.name} contains no {_LAYER_MARKER} markers, so it has no layers "
            f"to print — the model may lie outside the bed volume."
        )

    return {
        "filament_mm": found["filament_mm"],
        "filament_cm3": found["filament_cm3"],
        "filament_g": found["filament_g"],
        "print_time_text": time_text,
        "print_time_seconds": _parse_duration(time_text),
        "layer_count": layers,
    }


def _bed_violations_for(stl_path: Path) -> list[str]:
    """Bed-fit problems with an STL on disk, or an empty list.

    A mesh that cannot be read is not reported as an oversized part: let
    PrusaSlicer be the authority on whether it can parse a file, and say so in its
    own words.
    """
    # trimesh is already a dependency, used by cad.render_preview.
    import trimesh

    try:
        mesh = trimesh.load_mesh(stl_path)
        lo, hi = mesh.bounds
    except Exception:  # noqa: BLE001 - any parse failure defers to the slicer
        return []
    return bed_violations((float(hi[0] - lo[0]), float(hi[1] - lo[1]), float(hi[2] - lo[2])))


def slice_stl(
    stl_path: Path | str,
    profile: Path | str | None = None,
    output: Path | str | None = None,
    executable: str = "prusa-slicer",
) -> SliceResult:
    """Slice one STL to G-code and read back the cost of printing it.

    Args:
        stl_path: An STL exported by :func:`evee.cad.design`.
        profile: PrusaSlicer ``.ini``. Defaults to the verified house profile;
            override only in tests.
        output: G-code destination. Defaults to ``output/<stem>.gcode``.
        executable: Slicer binary, for tests that stub it.

    Raises:
        SlicerError: the input is unusable, the slicer failed, or its output could
            not be parsed. The message names the offending file or the slicer's own
            stderr, because that message is a client model's only feedback.
    """
    stl_path = Path(stl_path).resolve()
    if not stl_path.is_file():
        raise SlicerError(f"no such STL: {stl_path}")
    if stl_path.suffix.lower() != ".stl":
        raise SlicerError(f"not an STL file: {stl_path.name}")
    if stl_path.stat().st_size == 0:
        raise SlicerError(f"STL is empty: {stl_path.name}")

    profile_path = Path(profile).resolve() if profile else slicer_profile()
    if not profile_path.is_file():
        raise SlicerError(
            f"slicer profile not found at {profile_path}. This profile is hand-tuned "
            f"and physically verified; do not generate a replacement."
        )

    if shutil.which(executable) is None:
        raise SlicerError(
            f"{executable!r} is not on PATH. Install PrusaSlicer, or pass the path "
            f"to the binary."
        )

    # PrusaSlicer rejects an oversized object too, but its message names neither the
    # axis nor the overshoot — and that message is a client model's only chance to
    # correct the parameters.
    violations = _bed_violations_for(stl_path)
    if violations:
        raise SlicerError(
            f"{stl_path.name} does not fit the printer: {'; '.join(violations)}. "
            f"Reduce the offending dimension and design the part again."
        )

    gcode_path = (
        Path(output).resolve() if output else OUTPUT_DIR / f"{stl_path.stem}.gcode"
    )
    gcode_path.parent.mkdir(parents=True, exist_ok=True)

    command = [
        executable,
        "--export-gcode",
        "--load",
        str(profile_path),
        "--output",
        str(gcode_path),
        str(stl_path),
    ]

    try:
        # capture_output is load-bearing, not tidiness: PrusaSlicer prints progress
        # to stdout, which under stdio transport is the JSON-RPC channel.
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=slicer_timeout(),
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise SlicerError(
            f"PrusaSlicer did not finish within {slicer_timeout()}s slicing "
            f"{stl_path.name}"
        ) from exc

    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "no output"
        raise SlicerError(
            f"PrusaSlicer exited {proc.returncode} slicing {stl_path.name}: {detail}"
        )
    # A zero exit is not proof of an export; check the artifact itself.
    if not gcode_path.is_file() or gcode_path.stat().st_size == 0:
        detail = (proc.stderr or proc.stdout or "").strip() or "no output"
        raise SlicerError(
            f"PrusaSlicer reported success but wrote no G-code to {gcode_path}: "
            f"{detail}"
        )

    # Before the metadata is read, so the numbers reported at Gate 2 describe the file
    # that will actually be uploaded.
    levelling, levelling_detail = _apply_stored_mesh(gcode_path)

    metadata = _parse_gcode(gcode_path)
    return SliceResult(
        stl_path=stl_path,
        gcode_path=gcode_path,
        profile=profile_path,
        levelling=levelling,
        levelling_detail=levelling_detail,
        **metadata,  # type: ignore[arg-type]
    )
