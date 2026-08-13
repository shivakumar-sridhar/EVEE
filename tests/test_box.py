"""Geometry tests for box_with_lid.

Per CLAUDE.md these assert on bounding boxes and volumes, never on exact meshes:
tessellation is an implementation detail and comparing triangles would break on
every tolerance change.
"""

from __future__ import annotations

import math

import pytest
from build123d import Align, Box, Location
from pydantic import ValidationError

from vtp.config import (
    clearance as default_clearance,
    geometry_defaults,
    standoff_defaults,
)
from vtp.templates.box import (
    _BOSS_HOLE_STOP,
    BoxWithLidParams,
    PortSpec,
    StandoffSpec,
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
# Standoffs
# --------------------------------------------------------------------------- #

#: One post well clear of the walls and the lid lip at the acceptance dimensions.
STANDOFF = dict(x=15.0, y=12.0, diameter=6.0, height=4.0)

#: Volume a single post adds to the body. The stub sunk into the floor overlaps
#: material that is already there, so only the part above the floor counts.
POST_VOLUME = math.pi * (STANDOFF["diameter"] / 2) ** 2 * STANDOFF["height"]


def test_a_standoff_adds_material_without_changing_the_envelope(parts):
    """A post grows into the cavity — the outer dimensions must not move."""
    bare, _ = parts
    posted, _ = box_with_lid(**EXPLICIT, standoffs=[StandoffSpec(**STANDOFF)])

    assert posted.volume > bare.volume
    assert bbox_size(posted) == pytest.approx(bbox_size(bare), abs=1e-6)


def test_a_solid_standoff_is_exactly_a_cylinder_on_the_floor(parts):
    """No hole means no hole: pi r^2 h of added material and nothing removed."""
    bare, _ = parts
    posted, _ = box_with_lid(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=0.0)]
    )

    assert posted.volume - bare.volume == pytest.approx(POST_VOLUME, rel=1e-4)


def test_a_screw_boss_is_that_cylinder_less_a_blind_bore():
    """The bore runs from the post top to _BOSS_HOLE_STOP above the cavity floor."""
    solid, _ = box_with_lid(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=3.0)]
    )
    unbored, _ = box_with_lid(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=0.0)]
    )

    bore = math.pi * 1.5**2 * (STANDOFF["height"] - _BOSS_HOLE_STOP)
    assert unbored.volume - solid.volume == pytest.approx(bore, rel=1e-4)


@pytest.mark.parametrize("hole_diameter", [0.0, 3.0])
def test_the_base_is_never_perforated(parts, hole_diameter):
    """The bore stops above the floor, so a slab through the base is untouched.

    This is the one that matters: a hole drilled a fraction too deep leaves a box
    that looks right on screen and leaks light through the bottom.
    """
    bare, _ = parts
    posted, _ = box_with_lid(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=hole_diameter)]
    )

    floor = Box(1000, 1000, WALL, align=(Align.CENTER, Align.CENTER, Align.MIN))
    assert (posted & floor).volume == pytest.approx((bare & floor).volume, rel=1e-9)
    assert posted.bounding_box().min.Z == pytest.approx(0.0, abs=1e-9)


def test_four_standoffs_each_add_their_own_post(parts):
    """A mounting pattern, at the corners of a board's hole spacing."""
    bare, _ = parts
    corners = [
        StandoffSpec(**{**STANDOFF, "x": sx * 15.0, "y": sy * 12.0})
        for sx in (1, -1)
        for sy in (1, -1)
    ]
    posted, _ = box_with_lid(**EXPLICIT, standoffs=corners)

    solid_posts = 4 * POST_VOLUME
    bore = 4 * math.pi * (corners[0].hole_diameter / 2) ** 2 * (
        STANDOFF["height"] - _BOSS_HOLE_STOP
    )
    assert posted.volume - bare.volume == pytest.approx(solid_posts - bore, rel=1e-4)


def test_standoffs_appear_in_the_read_back_sentence():
    params = BoxWithLidParams(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=3.0)]
    )
    sentence = resolved_spec_sentence(params)

    assert "1 standoff" in sentence
    assert "(15, 12) 6mm dia x 4mm tall" in sentence
    assert "3mm screw hole" in sentence
    # inner_h 18 - lip 3 - post 4. The number nobody can read off the outer dims.
    assert "Clear height above the tallest post, under the lid lip: 11mm" in sentence


def test_a_holeless_standoff_reads_back_as_a_spacer():
    params = BoxWithLidParams(
        **EXPLICIT, standoffs=[StandoffSpec(**STANDOFF, hole_diameter=0.0)]
    )
    assert "solid spacer" in resolved_spec_sentence(params)


def test_no_standoffs_says_nothing_about_standoffs():
    assert "standoff" not in resolved_spec_sentence(BoxWithLidParams(**EXPLICIT))


@pytest.mark.parametrize(
    "standoffs, expected",
    [
        pytest.param(
            [dict(x=22.0, y=0.0, diameter=6.0)],
            "cavity floor",
            id="post_hangs_over_the_wall",
        ),
        pytest.param(
            [dict(x=0.0, y=0.0, height=16.0)],
            "would not close",
            id="post_taller_than_the_lid_lip_leaves_room_for",
        ),
        pytest.param(
            [dict(x=0.0, y=0.0, diameter=4.0, hole_diameter=3.0)],
            "split",
            id="bore_leaves_too_little_wall",
        ),
        pytest.param(
            [dict(x=0.0, y=0.0, diameter=6.0, height=1.0, hole_diameter=3.0)],
            "too short",
            id="post_too_short_to_thread",
        ),
        pytest.param(
            [dict(x=0.0, y=0.0, diameter=6.0), dict(x=4.0, y=0.0, diameter=6.0)],
            "separate posts",
            id="posts_overlap",
        ),
    ],
)
def test_impossible_standoffs_are_rejected_with_a_message_naming_the_problem(
    standoffs, expected
):
    with pytest.raises(TemplateError) as excinfo:
        box_with_lid(**EXPLICIT, standoffs=[StandoffSpec(**s) for s in standoffs])
    assert expected in str(excinfo.value)
    assert "standoffs[0]" in str(excinfo.value)


def test_standoff_model_rejects_unknown_fields():
    """extra='forbid' holds for this nested model too — nesting is where it leaks."""
    with pytest.raises(ValidationError):
        StandoffSpec(x=0.0, y=0.0, side="left")


def test_standoff_dimensions_come_from_defaults_toml():
    """Post size is tuned in config/defaults.toml, not in the signature."""
    house = standoff_defaults()
    post = StandoffSpec(x=0.0, y=0.0)

    assert post.diameter == house["diameter"]
    assert post.height == house["height"]
    assert post.hole_diameter == house["hole_diameter"]


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


# --------------------------------------------------------------------------- #
# Lid posts
#
# The feature that turns a press-fit lid into a fastened one WITHOUT touching the
# body — which matters because the body may already be printed. A plain hole in
# the lid secures nothing: the lid's inner face is at the cavity rim while the
# board sits on the floor standoffs, so the screw would span air.
# --------------------------------------------------------------------------- #

from vtp.templates.box import LidPostSpec

CASE = dict(outer_l=30.6, outer_w=27.7, outer_h=14.0, wall=2.0)
CORNERS = [(sx * 10.16, sy * 8.89) for sx in (-1, 1) for sy in (-1, 1)]


def _posts(length=7.1, diameter=5.0, **kw):
    return [LidPostSpec(x=x, y=y, length=length, diameter=diameter, **kw) for x, y in CORNERS]


def test_posts_extend_the_lid_downward_by_their_length():
    _, plain = box_with_lid(**CASE)
    _, posted = box_with_lid(**CASE, lid_posts=_posts())

    # 2mm plate + 7.1mm post; the 3mm lip is swallowed by the taller post.
    assert posted.bounding_box().size.Z == pytest.approx(2.0 + 7.1, abs=0.01)
    assert plain.bounding_box().size.Z == pytest.approx(5.0, abs=0.01)
    # Footprint is unchanged — posts live inside the lip, not outside the plate.
    assert posted.bounding_box().size.X == pytest.approx(plain.bounding_box().size.X, abs=0.01)


def test_posts_do_not_touch_the_body():
    """The whole point: a lid you can fasten to a body that is already printed."""
    plain_body, _ = box_with_lid(**CASE)
    posted_body, _ = box_with_lid(**CASE, lid_posts=_posts())
    assert posted_body.volume == pytest.approx(plain_body.volume, rel=1e-9)


def test_the_hole_goes_all_the_way_through():
    """Unlike a standoff's blind pilot. The screw head has to land outside the lid,
    so this is the one hole in the design meant to come out the other side."""
    _, plain = box_with_lid(**CASE)
    _, posted = box_with_lid(**CASE, lid_posts=_posts())

    # Added material is post-beyond-lip minus four through-holes. If the bores were
    # blind, the volume would come out higher than this.
    added_post = 4 * math.pi * 2.5**2 * (7.1 - 3.0)
    drilled = 4 * math.pi * 1.4**2 * (2.0 + 7.1)
    assert posted.volume - plain.volume == pytest.approx(added_post - drilled, rel=0.02)


def test_a_post_reaching_past_the_lip_is_refused():
    """Outside the lip it would land on the rim and hold the lid open — and you
    cannot see that until the lid will not close."""
    with pytest.raises(TemplateError, match="past the lid's lip"):
        box_with_lid(**CASE, lid_posts=[LidPostSpec(x=13.0, y=0, length=7.1, diameter=5.0)])


def test_a_post_deeper_than_the_cavity_is_refused():
    with pytest.raises(TemplateError, match="deeper than"):
        box_with_lid(**CASE, lid_posts=[LidPostSpec(x=0, y=0, length=20.0, diameter=5.0)])


def test_a_clearance_hole_that_would_split_the_post_is_refused():
    """Caught for real: a 2.8mm M2.5 clearance hole in the 4mm default post leaves
    0.6mm of wall per side."""
    with pytest.raises(TemplateError, match="leaves under"):
        box_with_lid(**CASE, lid_posts=[LidPostSpec(x=0, y=0, length=7.1, diameter=4.0)])


def test_overlapping_posts_are_refused():
    with pytest.raises(TemplateError, match="merge into one blob"):
        box_with_lid(
            **CASE,
            lid_posts=[
                LidPostSpec(x=0, y=0, length=7.1, diameter=5.0),
                LidPostSpec(x=2.0, y=0, length=7.1, diameter=5.0),
            ],
        )


def test_posts_sit_directly_over_the_floor_standoffs():
    """The arrangement the feature exists for: one screw through lid, board and post
    into the standoff below it."""
    stand = [StandoffSpec(x=x, y=y) for x, y in CORNERS]
    body, lid = box_with_lid(**CASE, standoffs=stand, lid_posts=_posts())

    assert body.volume > 0 and lid.volume > 0
    # Same centres, so the screw axis is shared.
    assert {(s.x, s.y) for s in stand} == {(p.x, p.y) for p in _posts()}
