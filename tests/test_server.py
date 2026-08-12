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

from vtp.config import OUTPUT_DIR
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


def test_exposed_tools_are_the_five_gated_steps(server):
    """The surface is a whitelist. Anything else here is a gate someone can skip."""

    async def go():
        return await server.list_tools()

    names = {t.name for t in anyio.run(go)}
    assert names == {
        "list_templates",
        "design_part",
        "slice_part",
        "get_printer_status",
        "start_print",
    }
    # upload_gcode was folded into start_print on 2026-08-12 at the owner's request.
    # If it reappears as a tool, the workflow in _INSTRUCTIONS is stale too.
    assert "upload_gcode" not in names


def test_no_cancel_tool(server):
    """printer.py implements cancel; exposing it lets a model end an 8-hour print."""

    async def go():
        return await server.list_tools()

    names = {t.name for t in anyio.run(go)}
    assert "cancel_print" not in names
    assert "pause_print" not in names


def test_no_tool_combines_steps(server):
    """The human approval between design, slice and print is the whole design.
    A single tool spanning two of them would let a model skip a gate."""

    async def go():
        return await server.list_tools()

    tools = anyio.run(go)
    names = {t.name for t in tools}
    assert not {"design_and_slice", "print_part", "make_part", "design_and_print"} & names

    for tool in tools:
        schema = json.dumps(tool.input_schema)
        assert "auto_print" not in schema
        # The bed confirmation belongs to exactly one tool. On any other it would
        # be a way to carry the confirmation forward into a step that does not
        # deserve it.
        if tool.name != "start_print":
            assert "bed_confirmed_clear" not in schema

    # Uploading must not be reachable from the slicing tool, or Gate 2 disappears.
    slice_schema = next(t for t in tools if t.name == "slice_part").input_schema
    assert set(slice_schema["properties"]) == {"stl_path"}


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


def test_invented_standoff_parameter_is_rejected(server):
    """StandoffSpec forbids extras too, for the same reason PortSpec does."""
    err, msg = call(
        server,
        "design_part",
        template="box_with_lid",
        params={**GOOD, "standoffs": [{"x": 0, "y": 0, "thread": "M3"}]},
    )
    assert err
    assert "thread" in msg


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


# --------------------------------------------------------------------------- #
# slice_part
# --------------------------------------------------------------------------- #


def test_design_part_reports_whether_a_window_opened(server):
    """A headless or remote client must be told to fall back to the PNGs."""
    err, payload = call(server, "design_part", template="box_with_lid", params=GOOD)
    assert not err
    assert payload["viewer"]["opened"] is False
    assert "suppressed during tests" in payload["viewer"]["detail"]


def test_slice_part_refuses_a_path_outside_the_output_directory(server):
    """A client model can put any string here; /etc/passwd must fail on the path,
    not somewhere inside PrusaSlicer."""
    err, msg = call(server, "slice_part", stl_path="/etc/passwd")
    assert err
    assert "only STLs this server exported" in msg


def test_slice_part_refuses_a_traversal_out_of_output(server, tmp_path):
    escape = str(OUTPUT_DIR / ".." / ".." / "etc" / "hostname")
    err, msg = call(server, "slice_part", stl_path=escape)
    assert err
    # Rejected for being outside output/, having been resolved first.
    assert "refusing to slice" in msg or "no such STL" in msg


def test_slice_part_names_a_missing_file(server):
    err, msg = call(server, "slice_part", stl_path=str(OUTPUT_DIR / "absent.stl"))
    assert err
    assert "no such STL" in msg


def test_slice_part_takes_no_profile_parameter(server):
    """The verified profile is not a decision worth delegating to a client model."""

    async def go():
        return await server.list_tools()

    tool = next(t for t in anyio.run(go) if t.name == "slice_part")
    assert set(tool.input_schema["properties"]) == {"stl_path"}


def test_slice_part_returns_the_metadata_and_a_readback(server, monkeypatch):
    from pathlib import Path

    from vtp.slicer import SliceResult

    stl = OUTPUT_DIR / "box_with_lid_body.stl"
    if not stl.is_file():
        pytest.skip("no exported STL to point at")

    fake = SliceResult(
        stl_path=stl,
        gcode_path=OUTPUT_DIR / "box_with_lid_body.gcode",
        profile=Path("config/ender3_v3se.ini"),
        print_time_seconds=1844,
        print_time_text="30m 44s",
        filament_g=4.25,
        filament_mm=1426.18,
        filament_cm3=3.43,
        layer_count=55,
    )
    monkeypatch.setattr("vtp.server.slice_stl", lambda path: fake)

    err, payload = call(server, "slice_part", stl_path=str(stl))
    assert not err
    assert payload["layer_count"] == 55
    assert payload["filament_g"] == 4.25
    assert payload["print_time_text"] == "30m 44s"
    assert "55 layers" in payload["summary"]
    # Slicing hands off to the status check, never straight to a printer.
    assert "get_printer_status" in payload["next_step"]


def test_slice_part_reports_whether_the_gcode_viewer_opened(server, monkeypatch):
    from pathlib import Path

    from vtp.slicer import SliceResult

    stl = OUTPUT_DIR / "box_with_lid_body.stl"
    if not stl.is_file():
        pytest.skip("no exported STL to point at")

    monkeypatch.setattr(
        "vtp.server.slice_stl",
        lambda path: SliceResult(
            stl_path=stl,
            gcode_path=OUTPUT_DIR / "box_with_lid_body.gcode",
            profile=Path("config/ender3_v3se.ini"),
            print_time_seconds=1844,
            print_time_text="30m 44s",
            filament_g=4.25,
            filament_mm=1426.18,
            filament_cm3=3.43,
            layer_count=55,
        ),
    )

    err, payload = call(server, "slice_part", stl_path=str(stl))
    assert not err
    assert payload["viewer"]["opened"] is False
    assert "suppressed during tests" in payload["viewer"]["detail"]


def test_slice_part_refuses_an_oversized_part_naming_the_axis(server, tmp_path):
    """The bed limit reaches the client as a message it can act on."""
    from vtp.cad import design

    designed = design(
        "box_with_lid",
        {"outer_l": 400.0, "outer_w": 30.0, "outer_h": 15.0},
        output_dir=OUTPUT_DIR,
        slug="oversized_probe",
        render=False,
    )
    try:
        err, msg = call(server, "slice_part", stl_path=str(designed.stl_paths["body"]))
        assert err
        assert "X is 400mm" in msg
        assert "220mm" in msg
    finally:
        for path in designed.stl_paths.values():
            path.unlink(missing_ok=True)


# --------------------------------------------------------------------------- #
# The print gate
#
# These assert the boundary, not the guards — the guards themselves are tested
# against a mock transport in tests/test_printer.py. What matters here is that a
# refusal reaches the client intact, and that no shortcut exists in the schema.
# --------------------------------------------------------------------------- #




class FakePrinter:
    """Stands in for OctoPrintClient, recording what the tool asked it to do."""

    calls: list[tuple] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return None

    def get_status(self):
        from vtp.printer import PrinterStatus

        return PrinterStatus(
            state="Operational",
            operational=True,
            printing=False,
            paused=False,
            error=False,
            error_text="",
            tool_actual=23.8,
            tool_target=0.0,
            bed_actual=23.4,
            bed_target=0.0,
        )

    def get_job(self):
        from vtp.printer import JobStatus

        return JobStatus(
            state="Operational",
            file_name="case.gcode",
            completion=100.0,
            print_time_seconds=2042,
            print_time_left_seconds=0,
        )

    def upload_and_print(self, path, *, bed_confirmed_clear):
        from vtp.printer import JobStatus, PrintRefused, UploadResult

        FakePrinter.calls.append(("upload_and_print", str(path), bed_confirmed_clear))
        if bed_confirmed_clear is not True:
            raise PrintRefused(
                f"refusing to print: bed_confirmed_clear was {bed_confirmed_clear!r}, "
                f"not True. A human has to physically look at the build plate. "
                f"Nothing was uploaded."
            )
        return (
            UploadResult(local_path=path, remote_name="case.gcode", size_bytes=1234),
            JobStatus(
                state="Printing",
                file_name="case.gcode",
                completion=0.0,
                print_time_seconds=0,
                print_time_left_seconds=1844,
            ),
        )


@pytest.fixture
def fake_printer(monkeypatch):
    FakePrinter.calls = []
    monkeypatch.setattr("vtp.server.OctoPrintClient", FakePrinter)
    return FakePrinter


@pytest.fixture
def probe_gcode():
    """A G-code file inside output/, which is the only place start_print accepts."""
    path = OUTPUT_DIR / "_print_probe.gcode"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(";LAYER_CHANGE\nG1 X1\n", encoding="utf-8")
    yield path
    path.unlink(missing_ok=True)


def test_start_print_takes_a_local_path_and_the_confirmation(server):
    """Matches BUILD_PLAN Phase 5: start_print(gcode_path, bed_confirmed_clear)."""

    async def go():
        return await server.list_tools()

    schema = next(t for t in anyio.run(go) if t.name == "start_print").input_schema
    assert set(schema["required"]) == {"gcode_path", "bed_confirmed_clear"}
    field = schema["properties"]["bed_confirmed_clear"]
    assert field["type"] == "boolean"
    # No default, ever. A default is a print nobody asked for.
    assert "default" not in field


def test_start_print_description_forbids_inferring_the_confirmation(server):
    """The description is the portable CLAUDE.md — the rule has to be in it.

    An OpenCode or Cline user gets no CLAUDE.md at all, so if this text does not
    say it, most users of this server never see the rule.
    """

    async def go():
        return await server.list_tools()

    tool = next(t for t in anyio.run(go) if t.name == "start_print")
    # Collapse whitespace: the docstring is hard-wrapped, so a phrase can straddle a
    # line break. A reflow must not fail this test, and rewording must.
    text = " ".join((tool.description or "").lower().split())
    assert "voice transcript" in text
    assert "infer" in text
    assert "irreversible" in text
    # It uploads too, so the description must say so — a model that thinks the file
    # still needs uploading afterwards will report the wrong thing to the user.
    assert "upload" in text


def test_instructions_carry_gate_three(server):
    text = server.instructions.lower()
    assert "gate 3" in text
    assert "bed_confirmed_clear" in text
    # The away-from-home rule is safety-relevant and lives nowhere else portable.
    assert "bed" in text and "away" in text
    # The workflow must not still describe a separate upload step.
    assert "upload_gcode" not in text


def test_get_printer_status_reports_readiness(server, fake_printer):
    err, payload = call(server, "get_printer_status")
    assert not err
    assert payload["state"] == "Operational"
    assert payload["ready_to_print"] is True
    assert payload["blocked_reason"] is None
    assert payload["temperatures"]["nozzle_actual"] == 23.8
    assert payload["job"]["file_name"] == "case.gcode"


def test_start_print_refuses_a_path_outside_output(server, fake_printer):
    """Only G-code this server sliced. It is the last point that can be checked."""
    err, msg = call(
        server, "start_print", gcode_path="/etc/passwd", bed_confirmed_clear=True
    )
    assert err
    assert "refusing to print" in msg
    assert "Nothing was uploaded" in msg
    assert FakePrinter.calls == []


def test_start_print_refuses_a_traversal_out_of_output(server, fake_printer, tmp_path):
    outside = tmp_path / "evil.gcode"
    outside.write_text(";LAYER_CHANGE\n", encoding="utf-8")
    sneaky = str(OUTPUT_DIR / ".." / ".." / outside.relative_to(tmp_path.anchor))
    err, msg = call(
        server, "start_print", gcode_path=sneaky, bed_confirmed_clear=True
    )
    assert err
    assert FakePrinter.calls == []


def test_start_print_names_a_missing_file(server, fake_printer):
    err, msg = call(
        server,
        "start_print",
        gcode_path=str(OUTPUT_DIR / "nope.gcode"),
        bed_confirmed_clear=True,
    )
    assert err
    assert "no such G-code" in msg
    assert FakePrinter.calls == []


def test_start_print_refusal_reaches_the_client_intact(server, fake_printer, probe_gcode):
    """A refusal is the client model's only signal. It must not be swallowed."""
    err, msg = call(
        server, "start_print", gcode_path=str(probe_gcode), bed_confirmed_clear=False
    )
    assert err
    assert "bed_confirmed_clear was False" in msg
    assert "physically look" in msg
    assert "Nothing was uploaded" in msg


def test_start_print_does_not_default_the_confirmation(server, fake_printer, probe_gcode):
    """Omitting it is a schema error, not a print."""
    err, msg = call(server, "start_print", gcode_path=str(probe_gcode))
    assert err
    assert "bed_confirmed_clear" in msg
    assert FakePrinter.calls == []


def test_start_print_uploads_and_starts(server, fake_printer, probe_gcode):
    err, payload = call(
        server, "start_print", gcode_path=str(probe_gcode), bed_confirmed_clear=True
    )
    assert not err
    assert payload["started"] is True
    assert payload["state"] == "Printing"
    # The filename comes back from the printer, not from our local stem.
    assert payload["filename"] == "case.gcode"
    assert FakePrinter.calls == [
        ("upload_and_print", str(probe_gcode), True)
    ]
