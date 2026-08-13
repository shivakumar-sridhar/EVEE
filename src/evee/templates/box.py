"""box_with_lid — a rectangular box with a press-fit capping lid.

Geometry notes (see CLAUDE.md for why these override BUILD_PLAN.md's text):

* **Capping lid.** The lid is a full-outer-dimension plate. Its lip drops *into*
  the cavity, inset from the lid edge by ``wall + clearance``, so the lip's outer
  dimensions plus a clearance gap per side equal the body's inner dimensions.

* **The cavity is a boolean subtraction, not a shell.** With ``fillet=1.0`` and
  ``wall=2.0`` a negative shell offset implies a -1.0mm inner corner radius,
  which OpenCascade only survives via ``Kind.INTERSECTION``. Subtracting an
  explicit inner box is robust and makes the inner dimensions exact — which
  matters, because the lid is dimensioned off them.

* **The lid exports lip-up.** Plate on the bed, lip pointing +Z, so it slices
  without supports.

* **Ports are cut after the fillet.** A port is a rectangular window through one
  wall — for a cable, a connector, or a button. It is subtracted from the
  finished body, so its own edges stay sharp and the vertical fillets are
  untouched. A port never breaks the top rim: material is always left above it,
  which the printer bridges.

* **Standoffs are added last, and their screw holes are blind.** A standoff is a
  cylindrical post on the cavity floor that a PCB sits on. Its hole stops short
  of the floor, so the base of the box is never perforated, and the post itself
  is sunk slightly into the floor so the union has no coincident faces. Posts
  print without support — they rise straight off the floor in print pose.

Cross-section, press_fit::

      +======================+      lid: full outer L x W
      |     +----------+     |
      +=====+          +=====+
   +--------+          +--------+
   | wall                  wall |
   |        (cavity)            |
   +----------------------------+

Elevation of a walled face carrying one port::

   +----------------------------+  <- top rim
   |         (bridged)          |
   |        +==========+        |  <- port top = z_offset + height
   |        |          |        |
   +--------+          +--------+  <- port bottom = cavity floor + z_offset

Section through two standoffs carrying a board::

   |      :  :            :  :      |  <- pilot holes, stopping above the floor
   |     +----+          +----+     |  <- post top = board underside
   |     |    |  height  |    |     |
   +-----+----+----------+----+-----+  <- cavity floor
   +--------------------------------+  <- base, never perforated
"""

from __future__ import annotations

from typing import Literal

from build123d import (
    Align,
    Axis,
    Box,
    Cylinder,
    Location,
    Part,
    fillet as fillet_edges,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from evee.config import (
    clearance as default_clearance,
    geometry_defaults,
    standoff_defaults,
)

__all__ = [
    "BoxWithLidParams",
    "PART_NAMES",
    "PortSpec",
    "StandoffSpec",
    "TemplateError",
    "box_with_lid",
    "inner_dims",
    "resolved_spec_sentence",
]

PART_NAMES = ("body", "lid")

LidStyle = Literal["press_fit", "sliding"]

#: Which wall a port is cut through. left/right are the ends of ``outer_l`` (the
#: +/-X faces); front/back are the ends of ``outer_w`` (the -/+Y faces).
PortSide = Literal["left", "right", "front", "back"]

#: Radii below this are treated as "no fillet" — OCC rejects a zero-radius fillet.
_MIN_FILLET = 1e-6

#: How far a port cutter pokes past each wall face. Coincident faces make the
#: boolean ambiguous; this is the same trick the cavity uses.
_OVERCUT = 1.0

#: Material left above a port so the top rim survives and the lid still seats.
#: The printer bridges this; keep it at least a couple of layers.
_MIN_PORT_HEADER = 1.0

#: Wall left on each side of a standoff's pilot hole. Thinner than this and the
#: boss splits when a self-tapping screw cuts its thread.
_MIN_BOSS_WALL = 0.8

#: How far short of the cavity floor a standoff's pilot hole stops. Keeps the
#: base solid and keeps the hole's bottom face out of the floor plane.
_BOSS_HOLE_STOP = 0.5

#: A post shorter than this cannot hold a screw thread worth cutting.
_MIN_SCREW_BOSS_HEIGHT = 1.5


class TemplateError(ValueError):
    """Requested parameters cannot produce valid geometry."""


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


class PortSpec(BaseModel):
    """One rectangular window through one wall of the body.

    Positions are measured from features a human can see, not from the origin:
    ``z_offset`` rises from the cavity floor (the inside of the base, where a PCB
    would rest), and ``offset`` slides along the wall from its centre.
    """

    model_config = ConfigDict(extra="forbid")

    side: PortSide = Field(
        description=(
            "Which wall to cut. left/right are the ends of outer_l; "
            "front/back are the ends of outer_w."
        )
    )
    width: float = Field(gt=0, description="Opening width in mm, along the wall.")
    height: float = Field(gt=0, description="Opening height in mm.")
    z_offset: float = Field(
        default=0.0,
        ge=0,
        description="Height in mm of the opening's bottom edge above the cavity floor.",
    )
    offset: float = Field(
        default=0.0,
        description="Shift in mm along the wall from its centre. Positive is +X or +Y.",
    )


class StandoffSpec(BaseModel):
    """One cylindrical post rising from the cavity floor, for mounting a PCB.

    Positions are measured from the centre of the cavity floor, which is also the
    centre of the box — so a board's mounting holes translate directly: a hole
    pattern 20mm x 18mm apart is four posts at ``(+/-10, +/-9)``.

    A post with ``hole_diameter`` is a screw boss: the hole is a pilot for a
    self-tapping screw, stopping short of the floor so the base stays solid. A
    post with ``hole_diameter=0`` is a plain spacer that lifts and locates the
    board without fixing it.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(
        description="Post centre in mm along X, measured from the interior centre."
    )
    y: float = Field(
        description="Post centre in mm along Y, measured from the interior centre."
    )
    diameter: float = Field(
        default_factory=lambda: standoff_defaults()["diameter"],
        gt=0,
        description="Outer diameter of the post in mm.",
    )
    height: float = Field(
        default_factory=lambda: standoff_defaults()["height"],
        gt=0,
        description=(
            "Post height in mm above the cavity floor — how far the board is "
            "lifted off the base."
        ),
    )
    hole_diameter: float = Field(
        default_factory=lambda: standoff_defaults()["hole_diameter"],
        ge=0,
        description=(
            "Pilot hole diameter in mm for a self-tapping screw (2.1 for M2.5, "
            "1.7 for M2). 0 makes a solid spacer with no screw hole."
        ),
    )


class LidPostSpec(BaseModel):
    """One post hanging from the underside of the lid, with a screw hole through it.

    This is what turns a press-fit lid into a fastened one **without touching the
    body**. A plain hole in the lid secures nothing: the lid's inner face sits at the
    cavity rim while the board sits down on the floor standoffs, so a screw dropped
    through it spans air and tightening pulls on nothing. A post bridges that gap, and
    the screw then clamps one solid stack — lid, post, board, standoff — with a single
    fastener.

    Positions use the same convention as :class:`StandoffSpec`: measured from the
    centre, so a post at the same ``(x, y)`` as a floor standoff lands directly above
    it, which is the arrangement this exists for.

    ``length`` is measured from the lid's inner face, and should stop just short of the
    board rather than resting on it. **Err short.** A post 0.3mm shy leaves the board a
    little play; a post 0.3mm long stops the lid seating on the rim at all, and there is
    no way to tell by looking.
    """

    model_config = ConfigDict(extra="forbid")

    x: float = Field(
        description="Post centre in mm along X, measured from the lid centre."
    )
    y: float = Field(
        description="Post centre in mm along Y, measured from the lid centre."
    )
    length: float = Field(
        gt=0,
        description=(
            "How far the post reaches below the lid's inner face, in mm. Size it to "
            "stop just short of the board: cavity depth minus standoff height minus "
            "board thickness, less about 0.3mm."
        ),
    )
    diameter: float = Field(
        default_factory=lambda: standoff_defaults()["diameter"],
        gt=0,
        description="Outer diameter of the post in mm.",
    )
    hole_diameter: float = Field(
        default=2.8,
        gt=0,
        description=(
            "Clearance hole through the post and the lid, in mm. A clearance hole, not "
            "a pilot: the screw must turn freely here and bite only in the body's "
            "standoff. 2.8mm suits M2.5, 2.3mm suits M2."
        ),
    )


class BoxWithLidParams(BaseModel):
    """Validated parameters for :func:`box_with_lid`.

    ``extra="forbid"`` is load-bearing: Phase 4 feeds ``model_json_schema()`` to
    Ollama's constrained decoding, so the extraction model cannot invent a field.

    Defaults come from ``config/defaults.toml`` at instantiation time, not from
    literals here — that file is the source of truth for house rules.
    """

    model_config = ConfigDict(extra="forbid")

    outer_l: float = Field(gt=0, description="Outer length in mm, along X.")
    outer_w: float = Field(gt=0, description="Outer width in mm, along Y.")
    outer_h: float = Field(gt=0, description="Outer height in mm, along Z.")
    wall: float = Field(
        default_factory=lambda: geometry_defaults()["wall"],
        gt=0,
        description="Wall and floor thickness in mm.",
    )
    lid_style: LidStyle = Field(
        default="press_fit",
        description=(
            "press_fit: lid caps the box, its lip dropping into the cavity. "
            "sliding: not implemented yet."
        ),
    )
    clearance: float = Field(
        default_factory=default_clearance,
        ge=0,
        description=(
            "Gap per side between lid lip and cavity wall in mm. "
            "Larger is looser: 0.2 snug, 0.25 default, 0.3 easy."
        ),
    )
    fillet: float = Field(
        default_factory=lambda: geometry_defaults()["fillet"],
        ge=0,
        description="Radius in mm applied to the vertical outer edges.",
    )
    lip_height: float = Field(
        default_factory=lambda: geometry_defaults()["lip_height"],
        gt=0,
        description="How deep the lid lip engages the cavity, in mm.",
    )
    ports: list[PortSpec] = Field(
        default_factory=list,
        description=(
            "Rectangular openings cut through the walls, for cables, connectors "
            "or buttons. Empty means a sealed box."
        ),
    )
    standoffs: list[StandoffSpec] = Field(
        default_factory=list,
        description=(
            "Posts on the cavity floor for mounting a PCB, positioned from the "
            "interior centre to match the board's mounting holes. Empty means a "
            "bare floor and a board that slides around."
        ),
    )

    lid_posts: list[LidPostSpec] = Field(
        default_factory=list,
        description=(
            "Posts under the lid with screw holes through them, so one screw holds "
            "the lid, the board and the base together. Put each at the same (x, y) as "
            "a floor standoff."
        ),
    )

    @model_validator(mode="after")
    def _check_geometry(self) -> "BoxWithLidParams":
        _validate(
            self.outer_l,
            self.outer_w,
            self.outer_h,
            self.wall,
            self.clearance,
            self.fillet,
            self.lip_height,
            self.ports,
            self.standoffs,
        )
        return self


def _validate(
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float,
    clearance: float,
    fillet: float,
    lip_height: float,
    ports: "list[PortSpec] | None" = None,
    standoffs: "list[StandoffSpec] | None" = None,
    lid_posts: "list[LidPostSpec] | None" = None,
) -> None:
    """Cross-field checks. Raises :class:`TemplateError` naming the bad values.

    Shared by the bare function and the Pydantic model so direct callers get the
    same guarantees. Pydantic re-wraps these as ``ValidationError``; the message
    survives intact.
    """
    smallest = min(outer_l, outer_w)

    for name, value in (
        ("outer_l", outer_l),
        ("outer_w", outer_w),
        ("outer_h", outer_h),
        ("wall", wall),
        ("lip_height", lip_height),
    ):
        if value <= 0:
            raise TemplateError(f"{name} must be positive, got {value}mm")
    if clearance < 0:
        raise TemplateError(f"clearance must be >= 0, got {clearance}mm")
    if fillet < 0:
        raise TemplateError(f"fillet must be >= 0, got {fillet}mm")

    if wall * 2 >= smallest:
        raise TemplateError(
            f"walls consume the whole footprint: wall={wall}mm leaves no cavity "
            f"in the {smallest}mm side (need wall < {smallest / 2}mm)"
        )
    if wall >= outer_h:
        raise TemplateError(
            f"no interior height left: wall={wall}mm against outer_h={outer_h}mm "
            f"(need wall < {outer_h}mm)"
        )
    if fillet * 2 >= smallest:
        raise TemplateError(
            f"fillet={fillet}mm is too large for the {smallest}mm side "
            f"(need fillet < {smallest / 2}mm)"
        )
    if 2 * wall + 2 * clearance >= smallest:
        raise TemplateError(
            f"lid lip would have non-positive width: wall={wall}mm and "
            f"clearance={clearance}mm consume the {smallest}mm side"
        )
    if lip_height >= outer_h - wall:
        raise TemplateError(
            f"lip_height={lip_height}mm is deeper than the "
            f"{outer_h - wall}mm cavity (outer_h={outer_h}mm, wall={wall}mm)"
        )

    for index, port in enumerate(ports or ()):
        _validate_port(index, port, outer_l, outer_w, outer_h, wall, fillet)

    standoffs = list(standoffs or ())
    lid_posts = list(lid_posts or ())
    for index, standoff in enumerate(standoffs):
        _validate_standoff(index, standoff, outer_l, outer_w, outer_h, wall, lip_height)
    _validate_standoffs_disjoint(standoffs)
    for index, post in enumerate(lid_posts):
        _validate_lid_post(index, post, outer_l, outer_w, outer_h, wall, clearance)
    _validate_lid_posts_disjoint(lid_posts)


def _validate_port(
    index: int,
    port: "PortSpec",
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float,
    fillet: float,
) -> None:
    """Check one port fits its wall, leaving corners and the top rim intact."""
    where = f"ports[{index}] ({port.side})"

    if port.width <= 0 or port.height <= 0:
        raise TemplateError(
            f"{where}: width and height must be positive, got "
            f"{port.width}x{port.height}mm"
        )
    if port.z_offset < 0:
        raise TemplateError(f"{where}: z_offset must be >= 0, got {port.z_offset}mm")

    # The wall the port runs along, and how much of it the fillets have eaten.
    span = outer_w if port.side in ("left", "right") else outer_l
    usable = span - 2 * fillet
    reach = port.width / 2 + abs(port.offset)
    if reach > usable / 2:
        raise TemplateError(
            f"{where}: a {port.width}mm opening offset {port.offset}mm reaches "
            f"{reach}mm from centre, past the {usable / 2}mm of flat wall on a "
            f"{span}mm side with {fillet}mm fillets"
        )

    cavity_h = outer_h - wall
    top = port.z_offset + port.height
    if top > cavity_h - _MIN_PORT_HEADER:
        raise TemplateError(
            f"{where}: the opening reaches {top}mm above the cavity floor, "
            f"leaving less than {_MIN_PORT_HEADER}mm of wall to bridge under the "
            f"{cavity_h}mm rim (lower z_offset={port.z_offset}mm or "
            f"height={port.height}mm, or raise outer_h={outer_h}mm)"
        )


def _validate_standoff(
    index: int,
    standoff: "StandoffSpec",
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float,
    lip_height: float,
) -> None:
    """Check one post fits the floor, clears the lid lip, and can hold a screw."""
    where = f"standoffs[{index}] at ({_fmt(standoff.x)}, {_fmt(standoff.y)})"

    if standoff.diameter <= 0 or standoff.height <= 0:
        raise TemplateError(
            f"{where}: diameter and height must be positive, got "
            f"{standoff.diameter}mm dia x {standoff.height}mm tall"
        )

    # The post must land on the cavity floor, not inside a wall.
    radius = standoff.diameter / 2
    for axis, centre, inner in (
        ("X", standoff.x, outer_l - 2 * wall),
        ("Y", standoff.y, outer_w - 2 * wall),
    ):
        reach = abs(centre) + radius
        if reach > inner / 2:
            raise TemplateError(
                f"{where}: a {standoff.diameter}mm post reaches {_fmt(reach)}mm from "
                f"centre in {axis}, past the {_fmt(inner / 2)}mm half-width of the "
                f"{_fmt(inner)}mm cavity floor (move it inboard or shrink diameter)"
            )

    # Anything taller than this is inside the volume the lid's lip drops into.
    headroom = outer_h - wall - lip_height
    if standoff.height > headroom:
        raise TemplateError(
            f"{where}: a {standoff.height}mm post is taller than the {_fmt(headroom)}mm "
            f"of cavity below the lid lip, so the lid would not close "
            f"(lower height, lower lip_height={lip_height}mm, or raise "
            f"outer_h={outer_h}mm)"
        )

    if standoff.hole_diameter <= 0:
        return

    if standoff.hole_diameter + 2 * _MIN_BOSS_WALL > standoff.diameter:
        raise TemplateError(
            f"{where}: a {standoff.hole_diameter}mm hole in a {standoff.diameter}mm "
            f"post leaves under {_MIN_BOSS_WALL}mm of wall per side and would split "
            f"when the screw bites (need diameter >= "
            f"{_fmt(standoff.hole_diameter + 2 * _MIN_BOSS_WALL)}mm)"
        )
    if standoff.height < _MIN_SCREW_BOSS_HEIGHT:
        raise TemplateError(
            f"{where}: a {standoff.height}mm post is too short to hold a screw "
            f"thread (need height >= {_MIN_SCREW_BOSS_HEIGHT}mm, or set "
            f"hole_diameter=0 for a plain spacer)"
        )


def _validate_lid_post(
    index: int,
    post: "LidPostSpec",
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float,
    clearance: float,
) -> None:
    """Reject a lid post that cannot be printed or would stop the lid closing."""
    where = f"lid_posts[{index}] at ({_fmt(post.x)}, {_fmt(post.y)})"

    # The post has to sit within the lip's footprint. Outside it, the post would be
    # over the rim or the wall and the lid simply would not go down.
    lip_l = outer_l - 2 * wall - 2 * clearance
    lip_w = outer_w - 2 * wall - 2 * clearance
    half = post.diameter / 2
    if abs(post.x) + half > lip_l / 2 or abs(post.y) + half > lip_w / 2:
        raise TemplateError(
            f"{where}: a {post.diameter}mm post reaches past the lid's lip "
            f"({_fmt(lip_l)}x{_fmt(lip_w)}mm), so it would land on the rim and hold "
            f"the lid open. Move it toward the centre or reduce diameter"
        )

    cavity_depth = outer_h - wall
    if post.length > cavity_depth:
        raise TemplateError(
            f"{where}: a {post.length}mm post is deeper than the {_fmt(cavity_depth)}mm "
            f"cavity, so it would hit the floor before the lid seats "
            f"(shorten it, or raise outer_h={outer_h}mm)"
        )

    if post.hole_diameter + 2 * _MIN_BOSS_WALL > post.diameter:
        raise TemplateError(
            f"{where}: a {post.hole_diameter}mm hole in a {post.diameter}mm post "
            f"leaves under {_MIN_BOSS_WALL}mm of wall per side (need diameter >= "
            f"{_fmt(post.hole_diameter + 2 * _MIN_BOSS_WALL)}mm)"
        )


def _validate_lid_posts_disjoint(posts: "list[LidPostSpec]") -> None:
    """Reject posts that intersect each other — a merged blob is not a mount."""
    for i, first in enumerate(posts):
        for j, second in enumerate(posts[i + 1 :], start=i + 1):
            gap = ((first.x - second.x) ** 2 + (first.y - second.y) ** 2) ** 0.5
            if gap < (first.diameter + second.diameter) / 2:
                raise TemplateError(
                    f"lid_posts[{i}] at ({_fmt(first.x)}, {_fmt(first.y)}) and "
                    f"lid_posts[{j}] at ({_fmt(second.x)}, {_fmt(second.y)}) are "
                    f"{_fmt(gap)}mm apart and would merge into one blob "
                    f"(need at least {_fmt((first.diameter + second.diameter) / 2)}mm)"
                )


def _validate_standoffs_disjoint(standoffs: "list[StandoffSpec]") -> None:
    """Reject posts that intersect each other — a merged blob is not a mount."""
    for i, first in enumerate(standoffs):
        for j, second in enumerate(standoffs[i + 1 :], start=i + 1):
            gap = ((first.x - second.x) ** 2 + (first.y - second.y) ** 2) ** 0.5
            touching = (first.diameter + second.diameter) / 2
            if gap < touching:
                raise TemplateError(
                    f"standoffs[{i}] at ({_fmt(first.x)}, {_fmt(first.y)}) and "
                    f"standoffs[{j}] at ({_fmt(second.x)}, {_fmt(second.y)}) are "
                    f"{_fmt(gap)}mm apart but need {_fmt(touching)}mm to stay "
                    f"separate posts"
                )


# --------------------------------------------------------------------------- #
# Derived quantities
# --------------------------------------------------------------------------- #


def inner_dims(params: BoxWithLidParams) -> tuple[float, float, float]:
    """Usable interior (length, width, height) in mm, with the lid on."""
    return (
        params.outer_l - 2 * params.wall,
        params.outer_w - 2 * params.wall,
        params.outer_h - params.wall,
    )


def lip_dims(params: BoxWithLidParams) -> tuple[float, float]:
    """Outer (length, width) of the lid lip in mm — inner dims less a gap per side."""
    inner_l, inner_w, _ = inner_dims(params)
    return inner_l - 2 * params.clearance, inner_w - 2 * params.clearance


def _fmt(value: float) -> str:
    """Millimetre value without pointless trailing zeros: 50.0 -> '50', 0.25 -> '0.25'."""
    return f"{value:.3f}".rstrip("0").rstrip(".") or "0"


def resolved_spec_sentence(params: BoxWithLidParams) -> str:
    """The Gate 1 read-back, templated from *validated* params.

    Generated in Python rather than by the extraction model, so it always
    describes what will actually be built rather than what a model intended.
    """
    inner_l, inner_w, inner_h = inner_dims(params)
    style = params.lid_style.replace("_", "-")
    return (
        f"Outer {_fmt(params.outer_l)}x{_fmt(params.outer_w)}x{_fmt(params.outer_h)}mm, "
        f"{_fmt(params.wall)}mm walls, "
        f"{style} lid at {_fmt(params.clearance)}mm clearance "
        f"with a {_fmt(params.lip_height)}mm lip, "
        f"{_fmt(params.fillet)}mm edge fillet. "
        f"Usable interior {_fmt(inner_l)}x{_fmt(inner_w)}x{_fmt(inner_h)}mm."
        f"{_ports_phrase(params.ports)}"
        f"{_standoffs_phrase(params.standoffs, inner_h, params.lip_height)}"
        f"{_lid_posts_phrase(params.lid_posts)}"
    )


def _ports_phrase(ports: list[PortSpec]) -> str:
    """The ports half of the read-back. Empty string when there are none."""
    if not ports:
        return ""
    described = []
    for port in ports:
        text = (
            f"{port.side} {_fmt(port.width)}x{_fmt(port.height)}mm, "
            f"{_fmt(port.z_offset)}mm above the floor"
        )
        if port.offset:
            text += f", {_fmt(abs(port.offset))}mm off-centre"
        described.append(text)
    noun = "opening" if len(ports) == 1 else "openings"
    return f" Wall {noun}: " + "; ".join(described) + "."


def _lid_posts_phrase(posts: "list[LidPostSpec]") -> str:
    """The lid-post half of the read-back.

    Says the screw length, because that is the number somebody has to go and buy and
    it is not any of the dimensions they typed. It is the sum of what the screw passes
    through, which is not obvious from the part on screen.
    """
    if not posts:
        return ""

    listed = "; ".join(
        f"({_fmt(post.x)}, {_fmt(post.y)}) {_fmt(post.diameter)}mm dia x "
        f"{_fmt(post.length)}mm long, {_fmt(post.hole_diameter)}mm clearance hole"
        for post in posts
    )
    deepest = max(post.length for post in posts)
    return (
        f" {len(posts)} lid post{'s' if len(posts) != 1 else ''} reaching down from "
        f"the lid: {listed}. Screws pass right through the lid and bite in the body's "
        f"standoffs, so allow about {_fmt(deepest + 4)}mm of screw."
    )


def _standoffs_phrase(
    standoffs: list[StandoffSpec], inner_h: float, lip_height: float
) -> str:
    """The standoffs half of the read-back, with the headroom left above them.

    The clear height matters more than the post height: it is what has to swallow
    the board, its components and its cables, and it is not a number anyone can
    read off the outer dimensions.
    """
    if not standoffs:
        return ""

    described = []
    for standoff in standoffs:
        bore = (
            f"{_fmt(standoff.hole_diameter)}mm screw hole"
            if standoff.hole_diameter > 0
            else "solid spacer"
        )
        described.append(
            f"({_fmt(standoff.x)}, {_fmt(standoff.y)}) {_fmt(standoff.diameter)}mm dia "
            f"x {_fmt(standoff.height)}mm tall, {bore}"
        )

    tallest = max(standoff.height for standoff in standoffs)
    clear = inner_h - lip_height - tallest
    noun = "standoff" if len(standoffs) == 1 else "standoffs"
    return (
        f" {len(standoffs)} {noun} from the interior centre: "
        + "; ".join(described)
        + f". Clear height above the tallest post, under the lid lip: {_fmt(clear)}mm."
    )


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


#: Centred in X and Y, sitting on Z=0 — every solid here is built in print pose.
_ON_BED = (Align.CENTER, Align.CENTER, Align.MIN)


def _fillet_vertical(part: Part, radius: float) -> Part:
    """Round the vertical (Z-parallel) edges. No-op below _MIN_FILLET."""
    if radius < _MIN_FILLET:
        return part
    return fillet_edges(part.edges().filter_by(Axis.Z), radius=radius)


def _port_cutter(
    port: PortSpec, outer_l: float, outer_w: float, wall: float
) -> Part:
    """A prism spanning one wall's thickness, to be subtracted from the body.

    Overshoots both wall faces by ``_OVERCUT`` so neither boolean face is
    coincident with an existing one.
    """
    through = wall + 2 * _OVERCUT
    # Centre of the wall's thickness — the overcut is symmetric, so it cancels.
    if port.side in ("left", "right"):
        cutter = Box(through, port.width, port.height, align=_ON_BED)
        sign = 1.0 if port.side == "right" else -1.0
        centre = (sign * (outer_l / 2 - wall / 2), port.offset)
    else:
        cutter = Box(port.width, through, port.height, align=_ON_BED)
        sign = 1.0 if port.side == "back" else -1.0
        centre = (port.offset, sign * (outer_w / 2 - wall / 2))

    # z_offset is measured from the cavity floor, which sits `wall` above the bed.
    return cutter.locate(Location((centre[0], centre[1], wall + port.z_offset)))


def _lid_post(post: "LidPostSpec", wall: float) -> Part:
    """The post itself, rising from the lid's inner face in print orientation.

    The lid prints plate-down with its lip pointing +Z, so a post points the same way
    and needs no support. Where it overlaps the lip it simply unions with it.
    """
    return Cylinder(
        radius=post.diameter / 2, height=post.length, align=_ON_BED
    ).locate(Location((post.x, post.y, wall)))


def _lid_post_hole(post: "LidPostSpec", wall: float) -> Part:
    """The clearance hole, drilled through the post *and* the lid plate.

    All the way through, unlike a standoff's blind pilot: the screw head has to land
    on the outside of the lid, so this is the one hole in the design that is meant to
    come out the other side.
    """
    return Cylinder(
        radius=post.hole_diameter / 2,
        height=wall + post.length + 2 * _OVERCUT,
        align=_ON_BED,
    ).locate(Location((post.x, post.y, -_OVERCUT)))


def _standoff_post(standoff: StandoffSpec, wall: float) -> Part:
    """The post itself, sunk into the floor so the union has no coincident face.

    The sink is capped at half the wall so a short standoff on a thin base can
    never poke out of the bottom of the box.
    """
    sink = min(wall / 2, _OVERCUT)
    post = Cylinder(
        radius=standoff.diameter / 2, height=standoff.height + sink, align=_ON_BED
    )
    return post.locate(Location((standoff.x, standoff.y, wall - sink)))


def _standoff_hole(standoff: StandoffSpec, wall: float) -> Part:
    """The blind pilot hole, from above the post's top down to near the floor.

    Overshoots the top by ``_OVERCUT`` so the open end is unambiguous, and stops
    ``_BOSS_HOLE_STOP`` above the cavity floor so the base stays solid.
    """
    depth = standoff.height - _BOSS_HOLE_STOP
    cutter = Cylinder(
        radius=standoff.hole_diameter / 2, height=depth + _OVERCUT, align=_ON_BED
    )
    return cutter.locate(Location((standoff.x, standoff.y, wall + _BOSS_HOLE_STOP)))


def box_with_lid(
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float | None = None,
    lid_style: LidStyle = "press_fit",
    clearance: float | None = None,
    fillet: float | None = None,
    lip_height: float | None = None,
    ports: list[PortSpec] | None = None,
    standoffs: list[StandoffSpec] | None = None,
    lid_posts: list[LidPostSpec] | None = None,
) -> tuple[Part, Part]:
    """Build a box body and a matching press-fit lid.

    All dimensions are in millimetres and **outer** unless named otherwise.
    ``None`` for wall / clearance / fillet / lip_height resolves from
    ``config/defaults.toml``. ``ports`` cuts rectangular windows through the
    walls; ``None`` or ``[]`` gives a sealed box. ``standoffs`` adds mounting
    posts to the cavity floor; ``None`` or ``[]`` leaves it bare.

    Returns ``(body, lid)``. Both sit on the Z=0 plane in print orientation:
    the body opening faces +Z, the lid rests plate-down with its lip pointing +Z.

    Raises:
        TemplateError: the parameters cannot produce valid geometry.
        NotImplementedError: ``lid_style="sliding"``.
    """
    geo = geometry_defaults()
    wall = geo["wall"] if wall is None else wall
    fillet = geo["fillet"] if fillet is None else fillet
    lip_height = geo["lip_height"] if lip_height is None else lip_height
    clearance = default_clearance() if clearance is None else clearance

    if lid_style == "sliding":
        raise NotImplementedError(
            "lid_style='sliding' is not implemented yet. A sliding lid changes the "
            "body too (grooves in two opposing walls, one wall shortened for entry) "
            "and needs its own physical verification print. Use 'press_fit'."
        )
    if lid_style != "press_fit":
        raise TemplateError(f"unknown lid_style {lid_style!r}")

    ports = list(ports or ())
    standoffs = list(standoffs or ())
    lid_posts = list(lid_posts or ())
    _validate(
        outer_l,
        outer_w,
        outer_h,
        wall,
        clearance,
        fillet,
        lip_height,
        ports,
        standoffs,
        lid_posts,
    )

    inner_l = outer_l - 2 * wall
    inner_w = outer_w - 2 * wall
    # An outer radius of `fillet` offset inward by `wall` leaves this much.
    # Zero means sharp inner corners, which print fine.
    inner_fillet = max(fillet - wall, 0.0)

    # --- body: filleted outer prism minus an over-tall inner prism ---------- #
    body = _fillet_vertical(Box(outer_l, outer_w, outer_h, align=_ON_BED), fillet)
    # Height outer_h (not outer_h - wall) so the cavity pokes out of the top:
    # a coincident top face would make the boolean ambiguous.
    cavity = _fillet_vertical(Box(inner_l, inner_w, outer_h, align=_ON_BED), inner_fillet)
    body = body - cavity.locate(Location((0, 0, wall)))

    # Ports come out after the fillet, so rounding the corners never rounds an
    # opening and the openings never interrupt a fillet.
    for port in ports:
        body = body - _port_cutter(port, outer_l, outer_w, wall)

    # Posts go on after the ports, then every hole is drilled — so a post is
    # never left with a half-cut bore because a later boolean landed on it.
    for standoff in standoffs:
        body = body + _standoff_post(standoff, wall)
    for standoff in standoffs:
        if standoff.hole_diameter > 0:
            body = body - _standoff_hole(standoff, wall)

    # --- lid: plate at full outer dims, lip sized off the cavity ------------ #
    lip_l = inner_l - 2 * clearance
    lip_w = inner_w - 2 * clearance
    # Shrinking a rounded corner by `clearance` shrinks its radius by the same.
    lip_fillet = max(inner_fillet - clearance, 0.0)

    plate = _fillet_vertical(Box(outer_l, outer_w, wall, align=_ON_BED), fillet)
    lip = _fillet_vertical(Box(lip_l, lip_w, lip_height, align=_ON_BED), lip_fillet)
    lid = plate + lip.locate(Location((0, 0, wall)))

    # Same order as the body: every post on first, then every hole drilled, so a
    # post is never left with a half-cut bore because a later boolean landed on it.
    for post in lid_posts:
        lid = lid + _lid_post(post, wall)
    for post in lid_posts:
        lid = lid - _lid_post_hole(post, wall)

    return body, lid


def build(params: BoxWithLidParams) -> tuple[Part, Part]:
    """Registry entry point: build from a validated params model."""
    return box_with_lid(
        outer_l=params.outer_l,
        outer_w=params.outer_w,
        outer_h=params.outer_h,
        wall=params.wall,
        lid_style=params.lid_style,
        clearance=params.clearance,
        fillet=params.fillet,
        lip_height=params.lip_height,
        ports=params.ports,
        standoffs=params.standoffs,
        lid_posts=params.lid_posts,
    )
