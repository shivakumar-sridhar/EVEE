"""MCP server tests.

These drive the server object directly rather than over a subprocess: the
transport is the SDK's business, ours is that the right things are exposed and
that bad input is refused at the boundary.

The load-bearing tests here are the rejection ones. With a bring-your-own-model
client we do not control the decoder, so server-side validation is the only thing
standing between an arbitrary model and the geometry kernel.
"""

from __future__ import annotations

import json

import anyio
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from vtp.server import build_server

GOOD = {"outer_l": 40, "outer_w": 30, "outer_h": 15}


@pytest.fixture(scope="module")
def server():
    return build_server()


def call(server, name: str, **arguments):
    """Invoke a tool and return ``(is_error, parsed_or_message)``.

    Called in-process a failing tool raises :class:`ToolError`; over a real
    transport the kernel converts the same failure into a ``CallToolResult`` with
    ``is_error=True`` carrying the message. Both are handled so these tests assert
    on the message a client would actually receive either way.
    """

    async def go():
        return await server.call_tool(name, arguments)

    try:
        result = anyio.run(go)
    except ToolError as exc:
        return True, str(exc)

    text = result.content[0].text
    if result.is_error:
        return True, text
    return False, json.loads(text)


# --------------------------------------------------------------------------- #
# Surface
# --------------------------------------------------------------------------- #


def test_only_design_tools_are_exposed(server):
    """No slice or print tool exists yet — an unimplemented print tool is an invitation."""

    async def go():
        return await server.list_tools()

    names = {t.name for t in anyio.run(go)}
    assert names == {"list_templates", "design_part"}


def test_design_part_takes_a_template_and_params_not_free_text(server):
    """A free-text `description` would put extraction back inside the server."""

    async def go():
        return await server.list_tools()

    schema = next(t for t in anyio.run(go) if t.name == "design_part").input_schema
    assert set(schema["required"]) == {"template", "params"}
    assert "description" not in schema["properties"]


def test_list_templates_exposes_the_parameter_contract(server):
    """The schema is what the client model fills, so it has to be reachable."""
    err, data = call(server, "list_templates")
    assert not err

    box = data["box_with_lid"]
    assert box["part_names"] == ["body", "lid"]
    assert box["description"]

    schema = box["schema"]
    # extra="forbid" must survive into the advertised schema, or a client model has
    # no way to know that inventing a field is fatal.
    assert schema["additionalProperties"] is False
    assert {"outer_l", "outer_w", "outer_h", "ports"} <= set(schema["properties"])


# --------------------------------------------------------------------------- #
# Designing
# --------------------------------------------------------------------------- #


@pytest.mark.slow
def test_design_part_returns_everything_the_review_gate_needs(server, tmp_path):
    err, data = call(server, "design_part", template="box_with_lid", params=GOOD)
    assert not err

    assert "40x30x15mm" in data["spec_sentence"]
    assert data["inner_dims"] == {"l": 36.0, "w": 26.0, "h": 13.0}
    assert set(data["stl_paths"]) == {"body", "lid"}
    assert data["bounding_boxes"]["body"] == {"x": 40.0, "y": 30.0, "z": 15.0}
    # The render is the point of the human gate; a spec sentence confirms numbers,
    # only the image confirms shape.
    assert sum(len(v) for v in data["preview_paths"].values()) == 4


@pytest.mark.slow
def test_omitted_params_resolve_from_house_defaults(server):
    """The model should leave a parameter out rather than guess it."""
    err, data = call(server, "design_part", template="box_with_lid", params=GOOD)
    assert not err
    assert data["params_resolved"]["wall"] == 2.0
    assert data["params_resolved"]["clearance"] == 0.25
    assert data["params_resolved"]["fillet"] == 1.0


# --------------------------------------------------------------------------- #
# Rejection — the reason this boundary exists
# --------------------------------------------------------------------------- #


def test_invented_parameter_is_rejected_across_the_mcp_boundary(server):
    """extra='forbid' must hold here, not just in the Pydantic model.

    This is the test that matters for bring-your-own-model: we cannot assume the
    client used constrained decoding, so an invented field has to die at the server.
    """
    err, msg = call(
        server, "design_part", template="box_with_lid", params={**GOOD, "diameter": 5}
    )
    assert err
    assert "diameter" in msg
    assert "Extra inputs are not permitted" in msg


def test_invented_nested_parameter_is_rejected(server):
    """PortSpec forbids extras too — nesting is where schemas usually leak."""
    err, msg = call(
        server,
        "design_part",
        template="box_with_lid",
        params={**GOOD, "ports": [{"side": "left", "width": 9, "height": 5, "shape": "round"}]},
    )
    assert err
    assert "shape" in msg


def test_impossible_geometry_is_rejected_with_the_value_named(server):
    """The message is the client model's only self-correction signal."""
    err, msg = call(
        server,
        "design_part",
        template="box_with_lid",
        params={"outer_l": 3, "outer_w": 3, "outer_h": 10},
    )
    assert err
    assert "footprint" in msg
    # Cleaned up: no pydantic "(root)" or "Value error," noise in front of it.
    assert "(root)" not in msg
    assert "Value error" not in msg


def test_unknown_template_names_the_alternatives_and_refuses_to_substitute(server):
    err, msg = call(server, "design_part", template="sphere", params={})
    assert err
    assert "box_with_lid" in msg
    # Without this, a model that wanted a sphere will reach for the box instead.
    assert "freeform" in msg


def test_a_port_that_would_breach_the_rim_is_rejected(server):
    """Geometry validation reaches through the boundary, not just field types."""
    err, msg = call(
        server,
        "design_part",
        template="box_with_lid",
        params={**GOOD, "ports": [{"side": "left", "width": 10, "height": 40}]},
    )
    assert err
    assert "ports[0]" in msg


def test_sliding_lid_reports_deferred_rather_than_building_something_wrong(server):
    err, msg = call(
        server, "design_part", template="box_with_lid", params={**GOOD, "lid_style": "sliding"}
    )
    assert err
