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

Cross-section, press_fit::

      +======================+      lid: full outer L x W
      |     +----------+     |
      +=====+          +=====+
   +--------+          +--------+
   | wall                  wall |
   |        (cavity)            |
   +----------------------------+
"""

from __future__ import annotations

from typing import Literal

from build123d import Align, Axis, Box, Location, Part, fillet as fillet_edges
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vtp.config import clearance as default_clearance, geometry_defaults

__all__ = [
    "BoxWithLidParams",
    "PART_NAMES",
    "TemplateError",
    "box_with_lid",
    "inner_dims",
    "resolved_spec_sentence",
]

PART_NAMES = ("body", "lid")

LidStyle = Literal["press_fit", "sliding"]

#: Radii below this are treated as "no fillet" — OCC rejects a zero-radius fillet.
_MIN_FILLET = 1e-6


class TemplateError(ValueError):
    """Requested parameters cannot produce valid geometry."""


# --------------------------------------------------------------------------- #
# Parameters
# --------------------------------------------------------------------------- #


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
    )


# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


def _fillet_vertical(part: Part, radius: float) -> Part:
    """Round the vertical (Z-parallel) edges. No-op below _MIN_FILLET."""
    if radius < _MIN_FILLET:
        return part
    return fillet_edges(part.edges().filter_by(Axis.Z), radius=radius)


def box_with_lid(
    outer_l: float,
    outer_w: float,
    outer_h: float,
    wall: float | None = None,
    lid_style: LidStyle = "press_fit",
    clearance: float | None = None,
    fillet: float | None = None,
    lip_height: float | None = None,
) -> tuple[Part, Part]:
    """Build a box body and a matching press-fit lid.

    All dimensions are in millimetres and **outer** unless named otherwise.
    ``None`` for wall / clearance / fillet / lip_height resolves from
    ``config/defaults.toml``.

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

    _validate(outer_l, outer_w, outer_h, wall, clearance, fillet, lip_height)

    inner_l = outer_l - 2 * wall
    inner_w = outer_w - 2 * wall
    # An outer radius of `fillet` offset inward by `wall` leaves this much.
    # Zero means sharp inner corners, which print fine.
    inner_fillet = max(fillet - wall, 0.0)

    on_bed = (Align.CENTER, Align.CENTER, Align.MIN)

    # --- body: filleted outer prism minus an over-tall inner prism ---------- #
    body = _fillet_vertical(Box(outer_l, outer_w, outer_h, align=on_bed), fillet)
    # Height outer_h (not outer_h - wall) so the cavity pokes out of the top:
    # a coincident top face would make the boolean ambiguous.
    cavity = _fillet_vertical(Box(inner_l, inner_w, outer_h, align=on_bed), inner_fillet)
    body = body - cavity.locate(Location((0, 0, wall)))

    # --- lid: plate at full outer dims, lip sized off the cavity ------------ #
    lip_l = inner_l - 2 * clearance
    lip_w = inner_w - 2 * clearance
    # Shrinking a rounded corner by `clearance` shrinks its radius by the same.
    lip_fillet = max(inner_fillet - clearance, 0.0)

    plate = _fillet_vertical(Box(outer_l, outer_w, wall, align=on_bed), fillet)
    lip = _fillet_vertical(Box(lip_l, lip_w, lip_height, align=on_bed), lip_fillet)
    lid = plate + lip.locate(Location((0, 0, wall)))

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
    )
