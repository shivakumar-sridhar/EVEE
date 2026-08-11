"""Registry, dispatch, export and preview tests."""

from __future__ import annotations

import pytest
import trimesh
from pydantic import ValidationError

from vtp.cad import design, render_preview
from vtp.templates import TEMPLATE_REGISTRY, UnknownTemplateError, get_template

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
