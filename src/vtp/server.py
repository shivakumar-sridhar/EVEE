"""MCP server — the product boundary.

Everything above this file is the user's choice of client and model: Claude Code,
OpenCode, Cline, a voice shim. Everything below is ours. That asymmetry decides
where the rules live.

**The client's model is not trusted to be well-behaved.** The original plan leaned
on constrained decoding to stop a model inventing a parameter; we do not control an
arbitrary client's decoder, so ``extra="forbid"`` and the templates' cross-field
``_validate()`` are the only real gate. Every tool here validates before it builds.

**Tool descriptions are the portable ``CLAUDE.md``.** They ship with the server and
every client reads them; none of them read ``CLAUDE.md``. So the house rules — outer
dimensions, defaults from ``config/defaults.toml``, templates as a fixed whitelist —
are written into the descriptions below rather than assumed.

**Error messages are an API.** They are the client model's only feedback signal for
self-correction, so they must name the offending value. :func:`_describe_error`
exists to keep that true across the MCP boundary.

Phase 5 is deliberately partial: design tools only. No ``slice_part``, no
``start_print``, not even as stubs — an unimplemented print tool in the schema is an
invitation.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import ValidationError

from vtp.cad import design
from vtp.templates import TEMPLATE_REGISTRY, UnknownTemplateError, get_template
from vtp.templates.box import TemplateError

__all__ = ["build_server", "main"]

_INSTRUCTIONS = """\
Parametric CAD for 3D printing. You pick a vetted template and fill its parameters;
the server builds the geometry.

There is no freeform geometry path. If no template fits the request, say so and stop —
do not approximate with a template that does not fit.

Workflow: call list_templates() to see what exists and get each template's parameter
schema, then call design_part() with a template name and parameters.

House rules:
- Dimensions in a user's request are OUTER unless they say otherwise.
- Omitted parameters resolve from config/defaults.toml (2.0mm walls, 0.25mm press-fit
  clearance, 1.0mm edge fillet). Do not invent defaults; leave a parameter out.
- Always report the resolved inner dimensions alongside the outer ones. design_part()
  returns both, plus a spec sentence written for reading back to the user.

design_part() only designs. It does not slice and it does not print.
"""


def _describe_error(exc: Exception) -> str:
    """Render an exception as a message a client model can act on.

    Pydantic nests its errors and prefixes them with a URL; flatten to
    ``field: message`` lines so the offending parameter is named in the first
    few words. Template errors already read well and pass through intact.
    """
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors():
            msg = err["msg"]
            # Cross-field checks surface as "Value error, <our message>"; our own
            # message already names the values, so the prefix is pure noise.
            msg = msg.removeprefix("Value error, ")
            loc = ".".join(str(p) for p in err["loc"])
            # A model_validator has no field location. Prefixing it "(root)" would
            # point the reader at a field that does not exist.
            lines.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(lines)
    return str(exc)


def build_server() -> MCPServer:
    """Construct the server. Separate from :func:`main` so tests can drive it."""
    server = MCPServer(
        name="vtp",
        title="Voice-to-Print CAD",
        instructions=_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool()
    def list_templates() -> dict[str, Any]:
        """List every buildable template with its full parameter schema.

        Call this before design_part. The returned JSON Schema for each template is
        the exact contract design_part validates against: unknown parameters are
        rejected, so the schema is not advisory.

        Returns a mapping of template name to:
          description  - what the template is for, in prose
          part_names   - the solids it produces, e.g. ("body", "lid")
          schema       - JSON Schema for that template's parameters
        """
        return {
            name: {
                "description": spec.description,
                "part_names": list(spec.part_names),
                "schema": spec.params_model.model_json_schema(),
            }
            for name, spec in TEMPLATE_REGISTRY.items()
        }

    @server.tool()
    def design_part(template: str, params: dict[str, Any]) -> dict[str, Any]:
        """Build a part from a template and export STLs plus preview images.

        Args:
            template: A template name from list_templates(). Not a description of a
                shape — the exact registry key.
            params: Parameters for that template, matching its schema from
                list_templates(). Unknown keys are rejected rather than ignored.
                Omit a parameter to take the house default; do not guess a value.

        Returns the resolved spec sentence, per-part STL paths and bounding boxes,
        preview PNG paths, and the usable interior dimensions.

        The preview images are the point of the human review step that follows this
        call: a spec sentence confirms numbers, but only the render confirms shape.
        Show them to the user and wait before doing anything else with the result.

        Raises a tool error naming the offending value if the parameters cannot
        produce valid geometry. When that happens, fix the named parameter and retry
        — do not switch templates to route around it.
        """
        try:
            spec = get_template(template)
        except UnknownTemplateError:
            known = ", ".join(sorted(TEMPLATE_REGISTRY))
            raise ValueError(
                f"unknown template {template!r}; this server builds only: {known}. "
                f"There is no freeform geometry fallback — if none of these fit the "
                f"request, say so rather than substituting one that does not."
            ) from None

        try:
            result = design(template, params)
        except (ValidationError, TemplateError) as exc:
            raise ValueError(
                f"invalid parameters for {spec.name!r}: {_describe_error(exc)}"
            ) from None

        return {
            "template": result.template,
            "spec_sentence": result.spec_sentence,
            "params_resolved": result.params,
            "stl_paths": {k: str(v) for k, v in result.stl_paths.items()},
            "bounding_boxes": {
                k: {"x": x, "y": y, "z": z}
                for k, (x, y, z) in result.bounding_boxes.items()
            },
            "preview_paths": {
                k: [str(p) for p in v] for k, v in result.preview_paths.items()
            },
            "inner_dims": (
                dict(zip("lwh", result.inner_dims)) if result.inner_dims else None
            ),
        }

    return server


def main() -> None:
    """Entry point for ``python -m vtp.server`` and for MCP client stdio launch."""
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
