"""House defaults, loaded from config/defaults.toml.

That file is the source of truth for anything a template leaves unspecified.
Tuning the physical fit of a printed part happens there, not in geometry code.
"""

from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.toml"
OUTPUT_DIR = REPO_ROOT / "output"
ENV_PATH = REPO_ROOT / ".env"


@lru_cache(maxsize=1)
def load_defaults(path: Path | None = None) -> dict[str, Any]:
    """Parse config/defaults.toml. Cached — call `load_defaults.cache_clear()` after editing."""
    target = path or DEFAULTS_PATH
    if not target.is_file():
        raise FileNotFoundError(f"house defaults not found at {target}")
    with target.open("rb") as fh:
        return tomllib.load(fh)


def geometry_defaults() -> dict[str, float]:
    """Wall thickness, edge fillet, lid lip engagement depth — all mm."""
    return dict(load_defaults()["geometry"])


def standoff_defaults() -> dict[str, float]:
    """PCB standoff post diameter, height and screw pilot diameter — all mm."""
    return dict(load_defaults()["standoff"])


def clearance(fit: str = "press_fit") -> float:
    """Gap per side between a lid lip and the cavity wall, in mm.

    Named fits live under [clearance] in defaults.toml: press_fit (the default),
    snug, easy. Raise press_fit and reprint if a printed lid is too tight.
    """
    table = load_defaults()["clearance"]
    if fit not in table:
        raise KeyError(f"unknown fit {fit!r}; defaults.toml defines {sorted(table)}")
    return float(table[fit])


def export_tolerances() -> tuple[float, float]:
    """(linear mm, angular radians) tessellation tolerance for STL export."""
    table = load_defaults()["export"]
    return float(table["stl_linear_tolerance"]), float(table["stl_angular_tolerance"])


def _expand(argv: list[str]) -> list[str]:
    """Substitute ``{profile}`` with the absolute path of the verified profile.

    A relative path in defaults.toml would resolve against the process's working
    directory, and an MCP client launches this server from wherever it likes.
    """
    profile = str(slicer_profile())
    return [arg.replace("{profile}", profile) for arg in argv]


def viewer_settings() -> tuple[list[str], bool]:
    """(command argv, auto_open) for the Gate 1 model viewer.

    The command is a list, not a shell string, so a path with a space in it cannot
    turn into two arguments. STL paths are appended by the caller.
    """
    table = load_defaults()["viewer"]
    return _expand([str(arg) for arg in table["command"]]), bool(table["auto_open"])


def gcode_viewer_settings() -> tuple[list[str], bool, str]:
    """(command argv, auto_open, the key to name if it is off) for Gate 2.

    Gated on ``gcode_auto_open`` and additionally on ``auto_open``. The two say
    different things and both have to hold: ``auto_open`` is "this machine has a
    screen", one fact about the environment shared by both gates, while
    ``gcode_auto_open`` is "I want to look at toolpaths", a preference about this
    gate alone. Slicing runs headless either way.

    The third element is which of the two vetoed, so the reason a window did not
    open names the key someone would actually have to edit rather than making
    them guess between the pair.
    """
    table = load_defaults()["viewer"]
    screen = bool(table["auto_open"])
    wanted = bool(table["gcode_auto_open"])
    veto = "[viewer].auto_open" if not screen else "[viewer].gcode_auto_open"
    return _expand([str(arg) for arg in table["gcode_command"]]), screen and wanted, veto


def slicer_profile() -> Path:
    """Absolute path to the verified PrusaSlicer profile.

    Resolved against the repo root so it does not depend on the process's working
    directory — an MCP client launches the server from wherever it likes.
    """
    return REPO_ROOT / str(load_defaults()["slicing"]["profile"])


def slicer_timeout() -> int:
    """Seconds to let PrusaSlicer run before giving up on it."""
    return int(load_defaults()["slicing"]["timeout_seconds"])


def _profile_values(path: Path, keys: set[str]) -> dict[str, str]:
    """Read ``key = value`` lines from a PrusaSlicer config export.

    An exported profile is a flat list of assignments with no section header, so
    ``configparser`` cannot read it without being fed a fake one. A line scan is
    both simpler and honest about the format.
    """
    found: dict[str, str] = {}
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            if key in keys and key not in found:
                found[key] = value.strip()
    return found


@lru_cache(maxsize=1)
def bed_extents() -> tuple[float, float, float]:
    """(x, y, z) printable volume in mm, from the verified slicer profile.

    Read rather than hard-coded: ``config/ender3_v3se.ini`` already states the bed
    for the machine it was tuned against, and a second copy of "220" in Python is a
    copy that can drift.

    Raises:
        KeyError: the profile lacks ``bed_shape`` or ``max_print_height``. No
            default is substituted — a wrong bed size that silently passes a fit
            check is worse than having no check at all.
    """
    profile = slicer_profile()
    values = _profile_values(profile, {"bed_shape", "max_print_height"})

    missing = {"bed_shape", "max_print_height"} - set(values)
    if missing:
        raise KeyError(
            f"{profile} defines no {' or '.join(sorted(missing))}; cannot determine "
            f"the printable volume, and guessing one would be worse than not checking"
        )

    # "0x0,220x0,220x220,0x220" — corners of the bed polygon.
    xs, ys = [], []
    for corner in values["bed_shape"].split(","):
        x, _, y = corner.strip().partition("x")
        xs.append(float(x))
        ys.append(float(y))

    return max(xs) - min(xs), max(ys) - min(ys), float(values["max_print_height"])


@lru_cache(maxsize=1)
def _dotenv(path: Path | None = None) -> dict[str, str]:
    """Parse ``.env`` at the repo root. Missing file is not an error.

    Secrets are deliberately not in ``defaults.toml``: that file is committed.

    Reading the file rather than only ``os.environ`` is not belt-and-braces, it is
    the whole mechanism. An MCP client spawns this server with a scrubbed
    environment — ``HOME``, ``LOGNAME``, ``PATH``, ``SHELL``, ``TERM``, ``USER`` and
    nothing more — so ``OCTOPRINT_API_KEY`` exported in a shell never arrives. The
    same discovery that put :func:`vtp.viewer.discover_display` on disk applies here.

    Deliberately not a dotenv library and deliberately not exported into
    ``os.environ``: values are handed to the one caller that asked, so a secret does
    not end up in the environment of every subprocess this server spawns — the
    slicer and the viewers among them.
    """
    target = path or ENV_PATH
    if not target.is_file():
        return {}

    values: dict[str, str] = {}
    for line in target.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        line = line.removeprefix("export ").lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        value = value.strip()
        # Strip one matched pair of quotes. No inline-comment handling: a '#' is a
        # legal character in an API key, and eating one would corrupt a secret in a
        # way that surfaces as a baffling 403 rather than a parse error.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def env_value(name: str) -> str | None:
    """A setting from the real environment, else from ``.env``, else None.

    ``os.environ`` wins so a deployment can override the file without editing it;
    the file is the fallback that makes a scrubbed environment survivable. Empty is
    treated as absent — a blank line in ``.env.example`` copied over unfilled should
    read as "not configured", not as an empty API key.
    """
    value = os.environ.get(name) or _dotenv().get(name)
    value = (value or "").strip()
    return value or None


def octoprint_settings() -> tuple[str | None, str | None]:
    """``(base_url, api_key)`` for the Pi, either of which may be None.

    Missing values are returned rather than raised on: :mod:`vtp.printer` owns the
    wording of that failure, and it is the module whose error messages a client
    model reads. The URL loses any trailing slash so joining a path cannot double it.
    """
    url = env_value("OCTOPRINT_URL")
    return (url.rstrip("/") if url else None), env_value("OCTOPRINT_API_KEY")


def printer_timeout() -> float:
    """Seconds to wait on an ordinary OctoPrint request."""
    return float(load_defaults()["printer"]["timeout_seconds"])


def printer_upload_timeout() -> float:
    """Seconds to wait on a G-code upload, which is far slower than a status GET."""
    return float(load_defaults()["printer"]["upload_timeout_seconds"])


def bed_violations(size: tuple[float, float, float]) -> list[str]:
    """Reasons a part with this bounding box will not print, or an empty list.

    Each message names the axis, the size, the limit and the overshoot, because
    these reach a client model as its only chance to correct the parameters.
    """
    limits = bed_extents()
    reasons = []
    for axis, extent, limit in zip("XYZ", size, limits):
        if extent > limit:
            reasons.append(
                f"{axis} is {extent:g}mm but the printer's limit is {limit:g}mm "
                f"(over by {extent - limit:g}mm)"
            )
    return reasons
