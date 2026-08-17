"""Render the case images the README embeds, using the repo's own preview code.

The point of generating these rather than drawing them: `cad.render_preview` is what
`design_part` shows a user at Gate 1, so the README depicts the actual output of the
pipeline and cannot drift into being a nicer picture than the tool produces.

Writes a light and a dark variant of each part into `docs/`. The dark one comes from
running the same function inside matplotlib's `dark_background` style — `render_preview`
sets only figsize and dpi, and never touches facecolor or text colors, so styling is
the caller's to set and no change to `cad.py` is needed.

    uv run python scripts/render_readme_assets.py
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"

# The part quoted in step 1 of the README: "A case for my BNO085, ports on both ends,
# screw posts in the lid." Sized around an Adafruit BNO085 breakout — 25.4 x 17.8mm,
# mounting holes 22.9 x 15.2 apart, hence the standoffs at (+/-11.45, +/-7.6).
#
# These numbers exist to make a representative picture, not to be a verified fit. The
# printed-and-measured ones live in config/defaults.toml.
PARAMS = {
    "outer_l": 33.0,
    "outer_w": 26.0,
    "outer_h": 14.0,
    "ports": [
        # STEMMA QT passthrough at each end, clearing the board it sits on.
        {"side": "left", "width": 9.0, "height": 5.0, "z_offset": 3.5},
        {"side": "right", "width": 9.0, "height": 5.0, "z_offset": 3.5},
    ],
    # 5.0mm rather than the 4.0mm house default: the lid post above each of these
    # carries a 2.8mm clearance hole, and the template refuses under 4.4mm. Matching
    # the standoff to it keeps the screwed stack one straight column.
    "standoffs": [
        {"x": 11.45, "y": 7.6, "diameter": 5.0},
        {"x": 11.45, "y": -7.6, "diameter": 5.0},
        {"x": -11.45, "y": 7.6, "diameter": 5.0},
        {"x": -11.45, "y": -7.6, "diameter": 5.0},
    ],
    "lid_posts": [
        # Directly above the standoffs, stopping just short of the board:
        # cavity 12 - standoff 3 - board 1.6 - 0.3 clearance.
        {"x": 11.45, "y": 7.6, "length": 7.1, "diameter": 5.0},
        {"x": 11.45, "y": -7.6, "length": 7.1, "diameter": 5.0},
        {"x": -11.45, "y": 7.6, "length": 7.1, "diameter": 5.0},
        {"x": -11.45, "y": -7.6, "length": 7.1, "diameter": 5.0},
    ],
}

# Suffix on the committed filename -> matplotlib style to render it under.
THEMES = {"": "default", "_dark": "dark_background"}


def main() -> None:
    import matplotlib.pyplot as plt

    from evee import cad

    DOCS.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # render=False: design's own previews would land here at default styling and
        # be thrown away. The ones we keep are rendered per-theme below.
        result = cad.design(
            "box_with_lid", PARAMS, output_dir=tmp, slug="case", render=False
        )
        print(result.summary())
        print()

        for suffix, style in THEMES.items():
            for part, stl in result.stl_paths.items():
                with plt.style.context(style):
                    written = cad.render_preview(stl, output_dir=tmp)
                # render_preview always writes iso and top; only iso is embedded.
                iso = next(p for p in written if p.stem.endswith("_iso"))
                dest = DOCS / f"case_{part}{suffix}.png"
                shutil.copyfile(iso, dest)
                print(f"  {dest.relative_to(REPO_ROOT)}  ({dest.stat().st_size:,} B)")


if __name__ == "__main__":
    main()
