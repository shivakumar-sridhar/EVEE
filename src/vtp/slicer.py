"""Phase 2 — STL in, G-code out, plus what the print will cost.

No model anywhere in this file. Slicing is a deterministic shell-out to PrusaSlicer
against one hand-tuned profile, and the numbers it reports are read back out of the
G-code rather than estimated.

**The profile is not a parameter the client chooses.** :func:`slice_stl` takes one so
tests can point at a fixture, but it defaults to the verified
``config/ender3_v3se.ini`` and the MCP tool exposes no way to override it. A generated
or stock profile omits ``M420 S1``, the CR Touch mesh is then ignored, and the first
layer fails — so "which profile" is not a decision worth delegating.

**PrusaSlicer writes progress to stdout.** Under the stdio transport that stream is
JSON-RPC, so the child's output is captured, never inherited. Letting it through would
corrupt the protocol rather than merely look untidy.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from vtp.config import (
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

    def summary(self) -> str:
        """Read-back line for the approval gate.

        Templated here in Python from parsed values, never written by a model —
        the same rule the design gate's spec sentence follows.
        """
        return (
            f"{self.stl_path.name} -> {self.gcode_path.name}: "
            f"{self.layer_count} layers, {self.filament_g:g} g "
            f"({self.filament_mm:g} mm), about {self.print_time_text}."
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
        stl_path: An STL exported by :func:`vtp.cad.design`.
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

    metadata = _parse_gcode(gcode_path)
    return SliceResult(
        stl_path=stl_path,
        gcode_path=gcode_path,
        profile=profile_path,
        **metadata,  # type: ignore[arg-type]
    )
