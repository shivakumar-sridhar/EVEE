"""Registry, dispatch, export and preview tests."""

from __future__ import annotations

import pytest
import trimesh
from build123d import Compound, Mesher
from pydantic import ValidationError

from evee.cad import PART_GAP, arrange_along_x, design, render_preview
from evee.config import bed_violations
from evee.templates import TEMPLATE_REGISTRY, UnknownTemplateError, get_template

ACCEPTANCE = dict(outer_l=50.0, outer_w=40.0, outer_h=20.0)


@pytest.fixture(scope="module")
def result(tmp_path_factory):
    """One full design run, shared — export plus four renders is the slow path."""
    out = tmp_path_factory.mktemp("design")
    return design("box_with_lid", ACCEPTANCE, output_dir=out)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #


def test_box_template_is_registered():
    spec = get_template("box_with_lid")
    assert spec.name == "box_with_lid"
    assert spec.part_names == ("body", "lid")
    assert "box" in spec.description.lower()


def test_unknown_template_lists_what_does_exist():
    with pytest.raises(UnknownTemplateError, match="box_with_lid"):
        get_template("gearbox_housing")


@pytest.mark.parametrize("name", sorted(TEMPLATE_REGISTRY))
def test_every_template_forbids_extra_fields(name):
    """Phase 4 relies on this: constrained decoding cannot invent a parameter."""
    spec = TEMPLATE_REGISTRY[name]
    assert spec.params_model.model_config.get("extra") == "forbid"


def test_params_model_rejects_an_invented_field():
    spec = get_template("box_with_lid")
    with pytest.raises(ValidationError, match="corner_radius"):
        spec.validate_params({**ACCEPTANCE, "corner_radius": 3.0})


def test_params_model_emits_a_json_schema_for_constrained_decoding():
    schema = get_template("box_with_lid").params_model.model_json_schema()
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"outer_l", "outer_w", "outer_h"}
    assert set(schema["properties"]) >= {
        "outer_l",
        "outer_w",
        "outer_h",
        "wall",
        "lid_style",
        "clearance",
        "fillet",
        "lip_height",
    }
    # Every field carries a description the extraction model can read.
    assert all("description" in prop for prop in schema["properties"].values())


# --------------------------------------------------------------------------- #
# design()
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_design_writes_one_stl_per_part(result):
    assert set(result.stl_paths) == {"body", "lid"}
    for path in result.stl_paths.values():
        assert path.is_file()
        assert path.stat().st_size > 1000


@pytest.mark.slow
def test_exported_stls_are_watertight(result):
    """The cheapest real check that the boolean did not leave a broken solid."""
    for name, path in result.stl_paths.items():
        mesh = trimesh.load_mesh(path)
        assert mesh.is_watertight, f"{name} mesh is not watertight"
        assert mesh.volume > 0


@pytest.mark.slow
def test_exported_stl_matches_the_reported_bounding_box(result):
    """Tessellation must not have moved the part's outer dimensions."""
    for name, path in result.stl_paths.items():
        mesh = trimesh.load_mesh(path)
        lo, hi = mesh.bounds
        assert tuple(hi - lo) == pytest.approx(result.bounding_boxes[name], abs=0.01)


@pytest.mark.slow
def test_design_renders_two_previews_per_part(result):
    for name, paths in result.preview_paths.items():
        assert [p.name.rsplit("_", 1)[-1] for p in paths] == ["iso.png", "top.png"]
        for png in paths:
            assert png.is_file()
            assert png.stat().st_size > 5000, f"{png.name} looks blank"


def test_design_reports_resolved_dimensions(result):
    assert result.template == "box_with_lid"
    assert result.inner_dims == pytest.approx((46.0, 36.0, 18.0))
    assert result.bounding_boxes["body"] == pytest.approx((50.0, 40.0, 20.0), abs=1e-6)
    assert "46x36x18mm" in result.spec_sentence


def test_design_reports_defaults_it_filled_in(result):
    """Gate 1 must show what was assumed, not just what was asked for."""
    assert result.params["wall"] == 2.0
    assert result.params["clearance"] == 0.25
    assert result.params["lid_style"] == "press_fit"


def test_summary_names_every_part_and_its_file(result):
    summary = result.summary()
    assert result.spec_sentence in summary
    for name, path in result.stl_paths.items():
        assert name in summary
        assert path.name in summary


def test_design_can_skip_rendering(tmp_path):
    """render=False is the fast path for tests and for re-slicing an existing design."""
    out = design("box_with_lid", ACCEPTANCE, output_dir=tmp_path, render=False)
    assert out.preview_paths == {}
    assert not list(tmp_path.glob("*.png"))
    assert all(p.is_file() for p in out.stl_paths.values())


def test_design_rejects_invalid_params_before_touching_the_filesystem(tmp_path):
    with pytest.raises(ValidationError):
        design("box_with_lid", {**ACCEPTANCE, "wall": 25.0}, output_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_slug_controls_the_filenames(tmp_path):
    out = design(
        "box_with_lid", ACCEPTANCE, output_dir=tmp_path, slug="battery_tray", render=False
    )
    assert out.stl_paths["body"].name == "battery_tray_body.stl"
    assert out.stl_paths["lid"].name == "battery_tray_lid.stl"


# --------------------------------------------------------------------------- #
# The review model
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_design_writes_one_review_model_holding_every_part(result):
    assert result.review_path is not None
    assert result.review_path.suffix == ".3mf"
    assert result.review_path.is_file()
    assert result.review_path.stat().st_size > 1000


@pytest.mark.slow
def test_the_review_model_spaces_the_parts_out(result):
    """The bug this file exists for: body and lid must not occupy one spot.

    Both STLs are centred on the origin in print pose, so a viewer handed them
    together shows one shape where there are two. The review model bakes the
    offsets into the coordinates, which is why its X span is wider than either
    part and its Y span is not.
    """
    shapes = Mesher().read(result.review_path)
    assert len(shapes) == 2, "the review model should hold both parts as objects"

    scene = Compound(children=shapes).bounding_box()
    body_x, body_y, _ = result.bounding_boxes["body"]
    lid_x, _, _ = result.bounding_boxes["lid"]

    assert scene.size.X == pytest.approx(body_x + lid_x + PART_GAP, abs=0.05)
    assert scene.size.Y == pytest.approx(body_y, abs=0.05)

    # And they really are apart, not merely spanning a wide box together.
    centres = sorted(shape.bounding_box().center().X for shape in shapes)
    assert centres[1] - centres[0] > PART_GAP


@pytest.mark.slow
def test_the_parts_themselves_stay_centred_for_slicing(result):
    """Spacing is a property of the arrangement, never of the single-part STLs.

    Doing double duty since the plate arrived: neither the review model nor
    ``export_plate`` may move the per-part STLs, which are what a single-part
    reprint slices.
    """
    for name, path in result.stl_paths.items():
        mesh = trimesh.load_mesh(path)
        lo, hi = mesh.bounds
        assert (lo[0] + hi[0]) / 2 == pytest.approx(0.0, abs=0.01), f"{name} moved in X"
        assert (lo[1] + hi[1]) / 2 == pytest.approx(0.0, abs=0.01), f"{name} moved in Y"
        assert lo[2] == pytest.approx(0.0, abs=0.01), f"{name} is off the bed"


# --------------------------------------------------------------------------- #
# arrange_along_x() and the printed plate
# --------------------------------------------------------------------------- #


def test_arrange_centres_the_row_on_the_origin():
    from build123d import Box

    parts = [Box(10, 4, 2), Box(20, 4, 2)]
    placed = arrange_along_x(parts)
    centres = [p.bounding_box().center().X for p in placed]

    assert centres[1] - centres[0] == pytest.approx(15 + PART_GAP)
    span = Compound(children=placed).bounding_box()
    assert span.center().X == pytest.approx(0.0, abs=1e-9)
    assert span.size.X == pytest.approx(30 + PART_GAP)


def test_arrange_leaves_y_and_z_alone():
    """Print pose is the part's own. Only X may change."""
    from build123d import Box

    part = Box(10, 4, 2)
    before = part.bounding_box()
    after = arrange_along_x([part])[0].bounding_box()

    assert after.center().X == pytest.approx(before.center().X)
    assert after.center().Y == pytest.approx(before.center().Y)
    assert after.min.Z == pytest.approx(before.min.Z)


@pytest.mark.slow
def test_design_writes_a_plate_holding_every_part(result):
    assert result.plate_path is not None
    assert result.plate_path.name.endswith("_plate.stl")

    mesh = trimesh.load_mesh(result.plate_path)
    assert len(mesh.split(only_watertight=False)) == 2, "the plate lost a part"

    lo, hi = mesh.bounds
    body_x, body_y, body_z = result.bounding_boxes["body"]
    lid_x, _, _ = result.bounding_boxes["lid"]

    assert hi[0] - lo[0] == pytest.approx(body_x + lid_x + PART_GAP, abs=0.05)
    assert hi[1] - lo[1] == pytest.approx(body_y, abs=0.05)
    assert hi[2] - lo[2] == pytest.approx(body_z, abs=0.05)
    assert lo[2] == pytest.approx(0.0, abs=0.01), "the plate must sit on the bed"


@pytest.mark.slow
def test_the_plate_keeps_all_the_material(result):
    """Nothing merged, nothing dropped: volume is the sum of the parts."""
    plate = trimesh.load_mesh(result.plate_path).volume
    parts = sum(trimesh.load_mesh(p).volume for p in result.stl_paths.values())
    assert plate == pytest.approx(parts, rel=1e-6)


@pytest.mark.slow
def test_the_plate_is_laid_out_exactly_as_the_review_model(result):
    """The guarantee, asserted: what Gate 1 shows is where the parts print.

    Both files come from ``arrange_along_x``. If someone later gives the plate its
    own spacing — a wider gap "for cooling", a different order — the preview stops
    describing the print and nothing else in the suite would notice. This is the
    test that notices.
    """
    review = sorted(
        shape.bounding_box().center().X for shape in Mesher().read(result.review_path)
    )
    plate = sorted(
        float(body.bounds.mean(axis=0)[0])
        for body in trimesh.load_mesh(result.plate_path).split(only_watertight=False)
    )

    assert len(review) == len(plate) == 2
    for shown, printed in zip(review, plate):
        assert printed == pytest.approx(shown, abs=0.05)


@pytest.mark.slow
def test_an_oversized_plate_is_reported_while_the_parts_still_fit(tmp_path):
    """Two parts that each fit can still make a plate that does not.

    The plate is refused by name here rather than deep inside PrusaSlicer, and the
    per-part STLs remain a usable fallback — which is exactly what the message has
    to tell the model to do.
    """
    out = design(
        "box_with_lid",
        dict(outer_l=140.0, outer_w=40.0, outer_h=20.0),
        output_dir=tmp_path,
        render=False,
    )
    assert out.plate_bed_violations, "280mm of plate should not fit a 220mm bed"
    assert out.plate_bed_violations[0].startswith("X")
    for size in out.bounding_boxes.values():
        assert not bed_violations(size), "each part on its own still fits"


# --------------------------------------------------------------------------- #
# render_preview()
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_render_preview_handles_a_flat_part(tmp_path):
    """A lid is 10x wider than tall — the degenerate case for 3D axis scaling."""
    out = design("box_with_lid", ACCEPTANCE, output_dir=tmp_path, render=False)
    pngs = render_preview(out.stl_paths["lid"])
    assert len(pngs) == 2
    assert all(p.is_file() and p.stat().st_size > 5000 for p in pngs)


@pytest.mark.slow
def test_render_preview_can_write_elsewhere(tmp_path):
    out = design("box_with_lid", ACCEPTANCE, output_dir=tmp_path, render=False)
    elsewhere = tmp_path / "previews"
    pngs = render_preview(out.stl_paths["body"], output_dir=elsewhere)
    assert all(p.parent == elsewhere for p in pngs)
