"""Compose the GitHub social preview card from assets already in the repo.

GitHub unfurls this image whenever the repo is linked on X, LinkedIn, Slack or
Discord. Without one, a shared link is a blank grey box.

It composites rather than renders: the spider from `logo/`, and the suit-coloured
part renders from `docs/`, which `render_readme_assets.py` produces. Run that first
if the geometry has changed.

Uploading is manual and cannot be scripted — GitHub exposes no API for it:

    Settings -> General -> Social preview -> Upload an image

    uv run python scripts/render_social_card.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS = REPO_ROOT / "docs"
LOGO = REPO_ROOT / "logo" / "spider-white.png"
OUT = DOCS / "social-preview.png"

# GitHub's stated size. It is displayed much smaller than this in most unfurls,
# which is why nothing on the card is small.
SIZE = (1280, 640)

# Where the render column starts. Text must not cross it.
PART_X = 690

BACKGROUND = (13, 17, 23)  # GitHub dark
TEXT = (230, 237, 243)
MUTED = (139, 148, 158)

FONT_DIRS = [
    Path("/usr/share/fonts/truetype/noto"),
    Path("/usr/share/fonts/truetype/dejavu"),
    Path("/usr/share/fonts/truetype/liberation"),
]
BOLD_NAMES = ["NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"]
REGULAR_NAMES = ["NotoSans-Regular.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"]


def find_font(names: list[str]):
    from PIL import ImageFont

    for directory in FONT_DIRS:
        for name in names:
            candidate = directory / name
            if candidate.exists():
                return str(candidate)
    raise SystemExit(f"no font found among {names} in {[str(d) for d in FONT_DIRS]}")


def fit(img, box_w: int, box_h: int):
    """Scale to fit inside a box, preserving aspect."""
    scale = min(box_w / img.width, box_h / img.height)
    return img.resize((round(img.width * scale), round(img.height * scale)))


def main() -> None:
    from PIL import Image, ImageDraw, ImageFont

    parts = [DOCS / "case_body.png", DOCS / "case_lid.png"]
    missing = [p for p in [LOGO, *parts] if not p.exists()]
    if missing:
        sys.exit(
            "missing assets: "
            + ", ".join(str(p.relative_to(REPO_ROOT)) for p in missing)
            + "\nrun: uv run python scripts/render_readme_assets.py"
        )

    card = Image.new("RGB", SIZE, BACKGROUND)
    draw = ImageDraw.Draw(card)

    bold = find_font(BOLD_NAMES)
    regular = find_font(REGULAR_NAMES)
    title_font = ImageFont.truetype(bold, 120)
    sub_font = ImageFont.truetype(bold, 44)
    body_font = ImageFont.truetype(regular, 27)
    foot_font = ImageFont.truetype(regular, 24)  # the stack line is the longest

    # --- the two parts first, so the text column can be checked against them ---
    # Right of PART_X; every line of text is asserted to end before it.
    body = fit(Image.open(parts[0]).convert("RGBA"), 330, 300)
    lid = fit(Image.open(parts[1]).convert("RGBA"), 300, 260)
    card.paste(lid, (SIZE[0] - lid.width - 55, 165), lid)
    card.paste(body, (PART_X + 10, 300), body)

    # --- wordmark: spider + EVEE, spider scaled to the cap band ---
    cap = draw.textbbox((0, 0), "EVEE", font=title_font)
    cap_h = cap[3] - cap[1]
    spider = fit(Image.open(LOGO).convert("RGBA"), cap_h, cap_h)

    left, baseline = 80, 200
    card.paste(spider, (left, baseline - spider.height), spider)
    draw.text(
        (left + spider.width + 26, baseline), "EVEE", font=title_font, fill=TEXT, anchor="ls"
    )

    lines = [
        (275, "An AI assistant for", sub_font, TEXT),
        (330, "hardware prototyping", sub_font, TEXT),
        (410, "Describe a part in a sentence.", body_font, MUTED),
        (448, "Review the real geometry. Approve the cost.", body_font, MUTED),
        (486, "Let it print.", body_font, MUTED),
        (545, "MCP server  ·  build123d  ·  PrusaSlicer  ·  OctoPrint", foot_font, MUTED),
    ]
    for y, text, font, fill in lines:
        draw.text((left, y), text, font=font, fill=fill)
        right = left + draw.textlength(text, font=font)
        # The failure mode this catches is silent: a longer string or a different
        # font on another machine slides text under the renders and it only shows
        # up once the card is already on GitHub.
        if right > PART_X:
            raise SystemExit(
                f"text overruns the render column by {right - PART_X:.0f}px: {text!r}"
            )

    card.save(OUT, optimize=True)
    print(f"  {OUT.relative_to(REPO_ROOT)}  {card.size[0]}x{card.size[1]}  "
          f"({OUT.stat().st_size:,} B)")
    print("\n  upload: Settings -> General -> Social preview")


if __name__ == "__main__":
    main()
