"""Template dispatch: validated params in, STL files and preview PNGs out.

This module is the boundary Gate 1 sits on. :func:`design` returns everything a
human needs to decide "is this the right shape" — the read-back sentence, the
bounding boxes, and two rendered views per part — and nothing that starts a print.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import trimesh
from build123d import Compound, Location, Mesher, Part, export_stl

from evee.config import OUTPUT_DIR, bed_violations, export_tolerances, plate_margin
from evee.templates import TEMPLATE_REGISTRY, get_template

__all__ = [
    "DesignResult",
    "arrange_along_x",
    "design",
    "export_plate",
    "export_review_model",
    "render_preview",
]

#: (elevation, azimuth) for the two preview views.
_VIEWS: dict[str, tuple[float, float]] = {
    "iso": (25.0, -45.0),
    "top": (90.0, -90.0),
}


@dataclass(frozen=True)
class DesignResult:
    """What Gate 1 shows the human."""

    template: str
    params: dict
    spec_sentence: str
    stl_paths: dict[str, Path]
    bounding_boxes: dict[str, tuple[float, float, float]]
    preview_paths: dict[str, list[Path]] = field(default_factory=dict)
    inner_dims: tuple[float, float, float] | None = None
    #: One 3MF holding every part, spaced out — what the viewer opens. Never sliced.
    review_path: Path | None = None
    #: One STL holding every part in the same arrangement — what the slicer gets.
    plate_path: Path | None = None
    plate_size: tuple[float, float, float] | None = None
    #: Why the plate will not fit, if it will not. Empty when it fits.
    plate_bed_violations: tuple[str, ...] = ()

    def summary(self) -> str:
        """Human-readable block for the approval gate."""
        lines = [self.spec_sentence, ""]
        for part, (x, y, z) in self.bounding_boxes.items():
            lines.append(f"  {part:<6} {x:g} x {y:g} x {z:g} mm  ->  {self.stl_paths[part].name}")
        if self.plate_path and self.plate_size:
            x, y, z = self.plate_size
            fits = "" if not self.plate_bed_violations else "  DOES NOT FIT THE BED"
            lines.append(
                f"  {'plate':<6} {x:g} x {y:g} x {z:g} mm  ->  {self.plate_path.name}{fits}"
            )
        return "\n".join(lines)


def design(
    template: str,
    params: dict,
    output_dir: Path | None = None,
    slug: str | None = None,
    render: bool = True,
) -> DesignResult:
    """Build a template's parts, export STLs, and render previews.

    Args:
        template: A key of :data:`evee.templates.TEMPLATE_REGISTRY`.
        params: Raw parameters; validated against the template's Pydantic model.
        output_dir: Where to write. Defaults to the repo's ``output/``.
        slug: Filename stem. Defaults to the template name.
        render: Set False to skip preview PNGs (they dominate the runtime).

    Raises:
        UnknownTemplateError: no such template.
        pydantic.ValidationError: params are invalid for this template.
    """
    spec = get_template(template)
    validated = spec.validate_params(params)

    target = Path(output_dir) if output_dir else OUTPUT_DIR
    target.mkdir(parents=True, exist_ok=True)
    stem = slug or spec.name

    parts = spec.build(validated)
    if len(parts) != len(spec.part_names):
        raise RuntimeError(
            f"template {spec.name!r} returned {len(parts)} solids but declares "
            f"part_names={spec.part_names}"
        )

    stl_paths: dict[str, Path] = {}
    bounding_boxes: dict[str, tuple[float, float, float]] = {}
    preview_paths: dict[str, list[Path]] = {}

    for name, part in zip(spec.part_names, parts):
        stl_path = target / f"{stem}_{name}.stl"
        _export(part, stl_path)
        stl_paths[name] = stl_path
        size = part.bounding_box().size
        bounding_boxes[name] = (size.X, size.Y, size.Z)
        if render:
            preview_paths[name] = render_preview(stl_path)

    review_path = export_review_model(parts, spec.part_names, target / f"{stem}_review.3mf")

    # Written for every template, including a hypothetical one-part one — where it is
    # simply that part at the origin. Predictability is worth more than the file it
    # saves: "slice the plate" stays one unconditional instruction to the client model.
    plate_path = export_plate(parts, target / f"{stem}_plate.stl")
    plate_bbox = Compound(children=arrange_along_x(parts)).bounding_box().size
    plate_size = (plate_bbox.X, plate_bbox.Y, plate_bbox.Z)

    return DesignResult(
        template=spec.name,
        params=validated.model_dump(),
        spec_sentence=spec.spec_sentence(validated),
        stl_paths=stl_paths,
        bounding_boxes=bounding_boxes,
        preview_paths=preview_paths,
        inner_dims=spec.inner_dims(validated),
        review_path=review_path,
        plate_path=plate_path,
        plate_size=plate_size,
        # The margin covers what the raw bounding box does not: a skirt loop, and the
        # prime lines the start G-code draws down the left edge of the bed.
        plate_bed_violations=tuple(bed_violations(plate_size, margin=plate_margin())),
    )


#: Gap between parts, in mm. Wide enough to read as separate objects at a glance,
#: narrow enough that a two-part design still fits the bed.
#:
#: Used by BOTH the review 3MF and the printed plate, and that shared use is the
#: guarantee: what the human approves at Gate 1 is where the parts actually land.
#: Give the plate its own spacing and the preview quietly stops meaning anything.
PART_GAP = 8.0


def arrange_along_x(parts: Sequence[Part], gap: float = PART_GAP) -> list[Part]:
    """Lay parts side by side along X, the row centred on the origin.

    Y and Z are untouched, so every part keeps the print pose it was built in —
    sitting on Z=0, which is where a slicer needs it.

    This is the one place a multi-part layout is decided. Both callers use it.
    """
    widths = [part.bounding_box().size.X for part in parts]
    total = sum(widths) + gap * (len(widths) - 1)

    placed = []
    cursor = -total / 2
    for part, width in zip(parts, widths):
        placed.append(part.moved(Location((cursor + width / 2, 0, 0))))
        cursor += width + gap
    return placed


def export_plate(parts: Sequence[Part], path: Path) -> Path:
    """Write every part into one STL, arranged exactly as the review 3MF shows them.

    This is what gets sliced when the human wants both parts in one job: one heat-up,
    one bed check, one print. The per-part STLs are still written and are still the
    right input for reprinting a single part.

    **One STL is one PrusaSlicer object**, so the slicer's default centring moves the
    whole arrangement as a unit and the relative layout survives. Do not "improve"
    this by passing the parts as separate STLs with ``--merge``: PrusaSlicer would
    then arrange them itself and the preview-equals-print guarantee would die
    silently, with nothing failing to say so.
    """
    _export(Compound(children=arrange_along_x(parts)), path)
    return path


def export_review_model(
    parts: Sequence[Part], names: Sequence[str], path: Path
) -> Path:
    """Write every part into one 3MF, laid out side by side along X.

    This file exists to be looked at and nothing else. The STLs stay exactly where
    a slicer needs them — each centred on the origin in print pose — and that is
    precisely why they cannot be shown as they are: handed to a viewer together,
    the body and its lid occupy the same space and you see one shape where there
    are two.

    The arrangement comes from :func:`arrange_along_x`, the same function the printed
    plate uses. That is deliberate: the whole point of showing a layout is that it is
    the layout that gets printed.

    Positions are baked into the mesh coordinates, so no viewer has to be asked
    politely to arrange anything.
    """
    placed = arrange_along_x(parts)

    linear, angular = export_tolerances()
    mesher = Mesher()
    for shape, name in zip(placed, names):
        mesher.add_shape(
            shape,
            linear_deflection=linear,
            angular_deflection=angular,
            part_number=name,
        )
    mesher.write(path)

    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"Mesher wrote nothing to {path}")
    return path


def _export(part: Part, path: Path) -> None:
    """Tessellate to STL at the tolerances from defaults.toml."""
    linear, angular = export_tolerances()
    if not export_stl(part, path, tolerance=linear, angular_tolerance=angular):
        raise RuntimeError(f"export_stl reported failure writing {path}")
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f"export_stl wrote nothing to {path}")


def _camera_direction(elev_deg: float, azim_deg: float) -> np.ndarray:
    """Unit vector from the part toward the camera, in mplot3d's convention."""
    elev, azim = np.radians(elev_deg), np.radians(azim_deg)
    return np.array(
        [np.cos(elev) * np.cos(azim), np.cos(elev) * np.sin(azim), np.sin(elev)]
    )


def render_preview(stl_path: Path, output_dir: Path | None = None) -> list[Path]:
    """Render an STL to two PNGs: isometric and top-down.

    matplotlib's ``mplot3d`` only, deliberately. Offscreen GL (pyrender/pyglet) on
    headless Linux is not worth the debugging time, and these images exist to
    answer "is the shape right", not to be pretty.

    ``Poly3DCollection`` has no z-buffer, so a hollow part drawn naively renders
    its own back faces over the front and reads as a solid block — which would
    hide exactly the feature Gate 1 exists to check. So we cull back faces
    ourselves and shade the rest by normal.

    Returns the written paths, ``[<stem>_iso.png, <stem>_top.png]``.
    """
    # Imported lazily: matplotlib is a heavy import and design(render=False) skips it.
    import matplotlib

    matplotlib.use("Agg")  # no display on this box, and none wanted
    import matplotlib.pyplot as plt
    from matplotlib.ticker import MaxNLocator
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    stl_path = Path(stl_path)
    target = Path(output_dir) if output_dir else stl_path.parent
    target.mkdir(parents=True, exist_ok=True)

    mesh = trimesh.load_mesh(stl_path)
    lo, hi = mesh.bounds
    size = hi - lo
    margin = float(size.max()) * 0.04

    written: list[Path] = []
    for view, (elev, azim) in _VIEWS.items():
        camera = _camera_direction(elev, azim)

        facing = mesh.face_normals @ camera
        visible = facing > 1e-9
        triangles = mesh.triangles[visible]
        # Painter's algorithm over the survivors: farthest first.
        order = np.argsort(mesh.triangles_center[visible] @ camera)
        triangles = triangles[order]
        # Lambert term against a light just off the camera axis, so coplanar
        # faces at different depths still separate visually.
        light = camera + np.array([0.25, 0.15, 0.35])
        light /= np.linalg.norm(light)
        shade = np.clip(mesh.face_normals[visible][order] @ light, 0.0, 1.0)
        intensity = 0.45 + 0.55 * shade
        colors = np.column_stack(
            [0.42 * intensity, 0.60 * intensity, 0.78 * intensity, np.ones_like(intensity)]
        )

        fig = plt.figure(figsize=(6, 6), dpi=110)
        ax = fig.add_subplot(projection="3d")
        # matplotlib re-sorts by projected depth and carries facecolors along,
        # so our ordering is belt-and-braces; the culling above is what matters.
        ax.add_collection3d(
            Poly3DCollection(
                triangles, facecolors=colors, edgecolors="#1d3348", linewidths=0.12
            )
        )

        ax.set_xlim(lo[0] - margin, hi[0] + margin)
        ax.set_ylim(lo[1] - margin, hi[1] + margin)
        ax.set_zlim(lo[2] - margin, hi[2] + margin)
        # True proportions without wasting frame on a bounding cube.
        ax.set_box_aspect(tuple(np.maximum(size, size.max() * 0.02)))
        ax.view_init(elev=elev, azim=azim)

        # A flat part (a lid is 10x wider than tall) gets a short axis; the
        # default six ticks then collapse into an unreadable smear. Thin them in
        # proportion rather than distorting the aspect to make room.
        for axis, extent in zip((ax.xaxis, ax.yaxis, ax.zaxis), size):
            bins = int(np.clip(round(6 * extent / size.max()), 2, 6))
            axis.set_major_locator(MaxNLocator(nbins=bins))

        ax.set_xlabel("X (mm)")
        ax.set_ylabel("Y (mm)")
        ax.set_zlabel("Z (mm)")
        ax.set_title(
            f"{stl_path.stem} — {view}\n"
            f"{size[0]:g} x {size[1]:g} x {size[2]:g} mm",
            fontsize=10,
        )

        png = target / f"{stl_path.stem}_{view}.png"
        fig.savefig(png, bbox_inches="tight")
        plt.close(fig)
        written.append(png)

    return written


def list_templates() -> dict[str, str]:
    """Template name -> description. Phase 4 renders this into the prompt."""
    return {name: spec.description for name, spec in TEMPLATE_REGISTRY.items()}
