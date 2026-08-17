"""Render the case images the README embeds, from the exported STLs.

These are renders of the *same STL files* the slicer is handed, so the README shows the
real mesh rather than a drawing of one.

f3d does the rendering — the mesh viewer this repo already offers as the Gate 1
alternative to PrusaSlicer (see `[viewer]` in `config/defaults.toml`). It is used here
instead of `cad.render_preview` for one reason: matplotlib's `Poly3DCollection` has no
z-buffer, so those previews are a hand-rolled painter's algorithm and show triangle
bleed. They are good enough to answer "is the shape right" at a gate, and not good
enough to be the first thing a visitor sees.

Backgrounds are transparent, so one image per part reads correctly on both GitHub
themes and no light/dark pair is needed.

Requires f3d on PATH (`apt install f3d`) and a display — it renders through OpenGL.

    uv run python scripts/render_readme_assets.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
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

# Steep enough to see the cavity floor past the near wall — at a shallower angle the
# walls hide two of the four standoffs and the part reads as an empty tray.
CAMERA_DIRECTION = "-0.6,-0.55,-1.2"

RENDER_ARGS = [
    "--up=+Z",
    f"--camera-direction={CAMERA_DIRECTION}",
    "--camera-zoom-factor=0.9",
    "--resolution=1600,1200",
    "--color=0.34,0.54,0.74",
    "--roughness=0.4",
    "--metallic=0.0",
    "--ambient-occlusion",  # reads the depth of ports, holes and the lip
    "--anti-aliasing",
    "--anti-aliasing-mode=ssaa",
    "--tone-mapping",
    "--hdri-ambient",
    "--no-background",  # transparent: one image serves light and dark themes
]

# Rendered wide, then downscaled — supersampling the whole frame is what keeps the
# fillet highlights and hole rims clean at README size. 800 is roughly 2x the width
# these display at, and stays full-colour: quantising to a palette halves the file
# again but posterises the smooth shading, which is the thing worth paying for here.
TARGET_WIDTH = 800


def render(stl: Path, out: Path) -> None:
    result = subprocess.run(
        ["f3d", str(stl), f"--output={out}", *RENDER_ARGS],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not out.exists():
        raise RuntimeError(
            f"f3d failed on {stl.name} (exit {result.returncode}):\n{result.stderr}"
        )


def trim_and_scale(png: Path) -> None:
    """Crop the transparent margin f3d leaves, then downscale to TARGET_WIDTH."""
    from PIL import Image

    img = Image.open(png).convert("RGBA")
    bbox = img.getchannel("A").getbbox()
    if bbox is None:
        raise RuntimeError(f"{png.name} is fully transparent — nothing rendered")
    img = img.crop(bbox)

    pad = round(max(img.size) * 0.02)
    padded = Image.new("RGBA", (img.width + 2 * pad, img.height + 2 * pad), (0, 0, 0, 0))
    padded.paste(img, (pad, pad))

    if padded.width > TARGET_WIDTH:
        height = round(padded.height * TARGET_WIDTH / padded.width)
        padded = padded.resize((TARGET_WIDTH, height), Image.LANCZOS)

    padded.save(png, optimize=True)


def main() -> None:
    if shutil.which("f3d") is None:
        sys.exit("f3d not found on PATH. apt install f3d")

    from evee import cad

    DOCS.mkdir(exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        # render=False: design's matplotlib previews are not what we want here.
        result = cad.design(
            "box_with_lid", PARAMS, output_dir=tmp, slug="case", render=False
        )
        print(result.summary())
        print()

        for part, stl in result.stl_paths.items():
            dest = DOCS / f"case_{part}.png"
            render(stl, dest)
            trim_and_scale(dest)
            print(f"  {dest.relative_to(REPO_ROOT)}  ({dest.stat().st_size:,} B)")


if __name__ == "__main__":
    main()
