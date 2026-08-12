"""Geometry tests for box_with_lid.

Per CLAUDE.md these assert on bounding boxes and volumes, never on exact meshes:
tessellation is an implementation detail and comparing triangles would break on
every tolerance change.
"""

from __future__ import annotations

import pytest
from build123d import Align, Box, Location
from pydantic import ValidationError

from vtp.config import clearance as default_clearance, geometry_defaults
from vtp.templates.box import (
    BoxWithLidParams,
    PortSpec,
    TemplateError,
    box_with_lid,
    inner_dims,
    lip_dims,
    resolved_spec_sentence,
)

# The acceptance-print dimensions from BUILD_PLAN.md Phase 1.
ACCEPTANCE = dict(outer_l=50.0, outer_w=40.0, outer_h=20.0)

WALL = 2.0
CLEARANCE = 0.25
FILLET = 1.0
LIP_HEIGHT = 3.0

EXPLICIT = dict(
    **ACCEPTANCE, wall=WALL, clearance=CLEARANCE, fillet=FILLET, lip_height=LIP_HEIGHT
)


@pytest.fixture(scope="module")
def parts():
    """(body, lid) at the acceptance dimensions. Module-scoped — OCC is not cheap."""
    return box_with_lid(**EXPLICIT)


def bbox_size(part) -> tuple[float, float, float]:
    size = part.bounding_box().size
    return (size.X, size.Y, size.Z)


def measure_lip(lid) -> tuple[float, float]:
    """Outer (length, width) of the lid's lip, measured off the actual solid.

    Intersects a thin slab safely above the plate, where the lip is the only
    material, so this reads the built geometry rather than trusting the maths.
    """
    slab = Box(1000, 1000, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    region = lid & slab.locate(Location((0, 0, WALL + 0.5)))
    size = region.bounding_box().size
    return (size.X, size.Y)


# --------------------------------------------------------------------------- #
# Dimensions
# --------------------------------------------------------------------------- #


def test_body_bounding_box_is_the_requested_outer_size(parts):
    body, _ = parts
    assert bbox_size(body) == pytest.approx((50.0, 40.0, 20.0), abs=1e-6)


def test_lid_bounding_box_is_outer_footprint_by_plate_plus_lip(parts):
    _, lid = parts
    assert bbox_size(lid) == pytest.approx((50.0, 40.0, WALL + LIP_HEIGHT), abs=1e-6)


def test_both_parts_sit_on_the_bed(parts):
    """Print orientation: nothing below Z=0, so neither part needs repositioning."""
    for part in parts:
        assert part.bounding_box().min.Z == pytest.approx(0.0, abs=1e-9)


def test_inner_dims_are_outer_less_walls():
    params = BoxWithLidParams(**EXPLICIT)
    assert inner_dims(params) == pytest.approx((46.0, 36.0, 18.0))


def test_lid_lip_plus_clearance_equals_body_inner_dims(parts):
    """The Phase 1 acceptance assertion: the lid actually fits the body.

    Measured off the built solid, not recomputed from the same formula that
    produced it.
    """
    _, lid = parts
    params = BoxWithLidParams(**EXPLICIT)
    inner_l, inner_w, _ = inner_dims(params)
    lip_l, lip_w = measure_lip(lid)

    assert lip_l + 2 * CLEARANCE == pytest.approx(inner_l, abs=1e-6)
    assert lip_w + 2 * CLEARANCE == pytest.approx(inner_w, abs=1e-6)
    # ...and the helper agrees with the geometry.
    assert (lip_l, lip_w) == pytest.approx(lip_dims(params), abs=1e-6)


def test_looser_clearance_shrinks_the_lip(parts):
    """Clearance is the tuning knob for the physical fit — prove it reaches the solid."""
    _, snug_lid = parts
    _, easy_lid = box_with_lid(**{**EXPLICIT, "clearance": 0.4})

    snug_l, snug_w = measure_lip(snug_lid)
    easy_l, easy_w = measure_lip(easy_lid)

    # 0.15mm looser per side => 0.30mm smaller overall.
    assert snug_l - easy_l == pytest.approx(0.3, abs=1e-6)
    assert snug_w - easy_w == pytest.approx(0.3, abs=1e-6)


# --------------------------------------------------------------------------- #
# Volume
# --------------------------------------------------------------------------- #


def test_body_volume_is_a_shell_slightly_trimmed_by_fillets(parts):
    body, _ = parts
    sharp = 50 * 40 * 20 - 46 * 36 * 18  # same shell with square corners
    assert body.volume < sharp  # rounding the corners can only remove material
    assert body.volume > sharp * 0.97
    assert body.volume < 50 * 40 * 20  # sanity: the cavity really got subtracted


def test_lid_volume_is_plate_plus_lip(parts):
    _, lid = parts
    lip_l, lip_w = lip_dims(BoxWithLidParams(**EXPLICIT))
    sharp = 50 * 40 * WALL + lip_l * lip_w * LIP_HEIGHT
    assert lid.volume < sharp
    assert lid.volume > sharp * 0.97


# --------------------------------------------------------------------------- #
# Ports
# --------------------------------------------------------------------------- #

PORT = dict(width=9.0, height=5.5, z_offset=0.0)


def test_a_port_removes_material_without_changing_the_envelope(parts):
    """A window is subtracted, and only from the inside of the bounding box."""
    solid, _ = parts
    ported, _ = box_with_lid(**EXPLICIT, ports=[PortSpec(side="right", **PORT)])

    assert ported.volume < solid.volume
    assert bbox_size(ported) == pytest.approx(bbox_size(solid), abs=1e-6)


def test_port_volume_is_its_cross_section_through_one_wall(parts):
    """The hole is exactly width x height x wall — nothing more, nothing less."""
    solid, _ = parts
    ported, _ = box_with_lid(**EXPLICIT, ports=[PortSpec(side="right", **PORT)])

    removed = solid.volume - ported.volume
    assert removed == pytest.approx(PORT["width"] * PORT["height"] * WALL, rel=1e-6)


def test_ports_on_opposite_walls_each_cut_their_own_hole(parts):
    solid, _ = parts
    ported, _ = box_with_lid(
        **EXPLICIT,
        ports=[PortSpec(side="left", **PORT), PortSpec(side="right", **PORT)],
    )

    removed = solid.volume - ported.volume
    assert removed == pytest.approx(2 * PORT["width"] * PORT["height"] * WALL, rel=1e-6)


#: side -> (axis index, outward sign). left/right are the ends of outer_l.
SIDE_AXES = {"right": (0, 1), "left": (0, -1), "back": (1, 1), "front": (1, -1)}


def wall_slab(body, side):
    """The part of `body` inside a thin slab running down the middle of one wall."""
    axis, sign = SIDE_AXES[side]
    span = (ACCEPTANCE["outer_l"], ACCEPTANCE["outer_w"])[axis]

    dims = [1000.0, 1000.0, 1000.0]
    dims[axis] = WALL * 0.5
    origin = [0.0, 0.0, 0.0]
    origin[axis] = sign * (span / 2 - WALL / 2)

    slab = Box(*dims, align=(Align.CENTER, Align.CENTER, Align.CENTER))
    return body & slab.locate(Location(tuple(origin)))


@pytest.mark.parametrize("side", list(SIDE_AXES))
def test_each_side_cuts_the_wall_it_names_and_no_other(parts, side):
    """A port opens through the face it names, and leaves the other three whole.

    Probed wall by wall: the slab down the named wall must lose exactly the
    port's cross-section, and every other wall must be untouched to the last
    cubic micron.
    """
    solid, _ = parts
    ported, _ = box_with_lid(**EXPLICIT, ports=[PortSpec(side=side, **PORT)])

    for face in SIDE_AXES:
        removed = wall_slab(solid, face).volume - wall_slab(ported, face).volume
        expected = PORT["width"] * PORT["height"] * WALL * 0.5 if face == side else 0.0
        assert removed == pytest.approx(expected, abs=1e-6), f"{side} port hit {face}"


def test_port_leaves_the_top_rim_intact():
    """The lid must still seat: no port may reach the rim, so the top is solid."""
    tall_port = PortSpec(side="right", width=9.0, height=5.5, z_offset=0.0)
    body, _ = box_with_lid(**EXPLICIT, ports=[tall_port])

    # A slab spanning the topmost millimetre of the body.
    outer_h = ACCEPTANCE["outer_h"]
    slab = Box(1000, 1000, 1.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    rim = body & slab.locate(Location((0, 0, outer_h - 1.0)))

    size = rim.bounding_box().size
    assert (size.X, size.Y) == pytest.approx(
        (ACCEPTANCE["outer_l"], ACCEPTANCE["outer_w"]), abs=1e-6
    )
    # A continuous ring, not a broken one: same volume as the unported rim.
    unported, _ = box_with_lid(**EXPLICIT)
    assert rim.volume == pytest.approx(
        (unported & slab.locate(Location((0, 0, outer_h - 1.0)))).volume, rel=1e-9
    )


def test_port_bottom_sits_at_the_cavity_floor_by_default():
    """z_offset is measured from the inside of the base, where a board rests."""
    body, _ = box_with_lid(**EXPLICIT, ports=[PortSpec(side="right", **PORT)])

    # A slab through the floor must be untouched; one just above it must not be.
    floor = Box(1000, 1000, WALL, align=(Align.CENTER, Align.CENTER, Align.MIN))
    intact, _ = box_with_lid(**EXPLICIT)
    assert (body & floor).volume == pytest.approx((intact & floor).volume, rel=1e-9)

    just_above = Box(1000, 1000, 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
    probe = just_above.locate(Location((0, 0, WALL)))
    assert (body & probe).volume < (intact & probe).volume


def test_ports_appear_in_the_read_back_sentence():
    params = BoxWithLidParams(
        **EXPLICIT,
        ports=[PortSpec(side="left", **PORT), PortSpec(side="right", **PORT)],
    )
    sentence = resolved_spec_sentence(params)

    assert "Wall openings" in sentence
    assert "left 9x5.5mm" in sentence
    assert "right 9x5.5mm" in sentence
    assert "0mm above the floor" in sentence


def test_no_ports_says_nothing_about_ports():
    assert "opening" not in resolved_spec_sentence(BoxWithLidParams(**EXPLICIT))


@pytest.mark.parametrize(
    "port, expected",
    [
        pytest.param(
            dict(side="right", width=60.0, height=5.0), "flat wall", id="wider_than_wall"
        ),
        pytest.param(
            dict(side="right", width=9.0, height=5.0, offset=18.0),
            "flat wall",
            id="offset_into_the_corner",
        ),
        pytest.param(
            dict(side="right", width=9.0, height=18.0), "bridge", id="reaches_the_rim"
        ),
        pytest.param(
            dict(side="right", width=9.0, height=5.0, z_offset=14.0),
            "bridge",
            id="z_offset_pushes_it_to_the_rim",
        ),
    ],
)
def test_impossible_ports_are_rejected_with_a_message_naming_the_problem(
    port, expected
):
    with pytest.raises(TemplateError) as excinfo:
        box_with_lid(**EXPLICIT, ports=[PortSpec(**port)])
    assert expected in str(excinfo.value)
    assert "ports[0]" in str(excinfo.value)


def test_port_model_rejects_unknown_fields_and_bad_sides():
    """extra='forbid' holds for the nested model too — Phase 4 depends on it."""
    with pytest.raises(ValidationError):
        PortSpec(side="right", width=9.0, height=5.0, diameter=3.0)
    with pytest.raises(ValidationError):
        PortSpec(side="topside", width=9.0, height=5.0)


# --------------------------------------------------------------------------- #
# Defaults
# --------------------------------------------------------------------------- #


def test_omitted_params_come_from_defaults_toml():
    """config/defaults.toml is the source of truth, not literals in the signature."""
    geo = geometry_defaults()
    params = BoxWithLidParams(**ACCEPTANCE)

    assert params.wall == geo["wall"]
    assert params.fillet == geo["fillet"]
    assert params.lip_height == geo["lip_height"]
    assert params.clearance == default_clearance("press_fit")


def test_bare_function_resolves_the_same_defaults(parts):
    """box_with_lid(...) with Nones must match the explicit call."""
    body_default, lid_default = box_with_lid(**ACCEPTANCE)
    body_explicit, lid_explicit = parts

    assert bbox_size(body_default) == pytest.approx(bbox_size(body_explicit), abs=1e-9)
    assert lid_default.volume == pytest.approx(lid_explicit.volume, abs=1e-6)


def test_resolved_spec_sentence_reports_outer_inner_and_fit():
    sentence = resolved_spec_sentence(BoxWithLidParams(**EXPLICIT))
    assert "50x40x20mm" in sentence  # outer, as requested
    assert "46x36x18mm" in sentence  # inner, as computed
    assert "0.25mm clearance" in sentence
    assert "press-fit" in sentence
    assert "2mm walls" in sentence  # no stray trailing zeros


# --------------------------------------------------------------------------- #
# Rejection
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "overrides, expected",
    [
        pytest.param(
            dict(outer_l=10, outer_w=10, wall=5), "footprint", id="walls_fill_footprint"
        ),
        pytest.param(dict(outer_h=2, wall=2), "interior height", id="no_interior_height"),
        pytest.param(dict(fillet=20), "fillet", id="fillet_exceeds_half_side"),
        pytest.param(dict(clearance=-0.1), "clearance", id="negative_clearance"),
        pytest.param(dict(lip_height=18), "deeper than", id="lip_deeper_than_cavity"),
        pytest.param(
            dict(outer_l=10, outer_w=10, wall=4.5, clearance=0.6),
            "lid lip",
            id="lip_would_vanish",
        ),
        pytest.param(dict(outer_w=-5), "positive", id="negative_dimension"),
    ],
)
def test_invalid_geometry_is_rejected_with_a_message_naming_the_problem(
    overrides, expected
):
    with pytest.raises(TemplateError) as excinfo:
        box_with_lid(**{**EXPLICIT, **overrides})
    assert expected in str(excinfo.value)


def test_params_model_rejects_the_same_geometry():
    """Cross-field checks fire through Pydantic too, message intact."""
    with pytest.raises(ValidationError, match="footprint"):
        BoxWithLidParams(outer_l=10, outer_w=10, outer_h=20, wall=5)


def test_sliding_lid_is_deferred_not_silently_wrong():
    with pytest.raises(NotImplementedError, match="press_fit"):
        box_with_lid(**{**EXPLICIT, "lid_style": "sliding"})
