"""House defaults, loaded from config/defaults.toml.

That file is the source of truth for anything a template leaves unspecified.
Tuning the physical fit of a printed part happens there, not in geometry code.
"""

from __future__ import annotations

import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULTS_PATH = REPO_ROOT / "config" / "defaults.toml"
OUTPUT_DIR = REPO_ROOT / "output"


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
