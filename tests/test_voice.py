"""Voice-layer tests.

The load-bearing test in this file is that a voice session cannot start a print. That
constraint is stated in BUILD_PLAN.md Phase 7 and restated in the voice system prompt,
but neither of those is a mechanism — a prompt is a request, and the model on the other
end is bring-your-own. ``gate.decide`` is the mechanism, so it is what gets asserted.

Nothing here imports the agent SDK. The rule is a pure function over a tool name
precisely so it can be tested without the SDK, a microphone, or a machine.
"""

from __future__ import annotations

import json

import pytest

from vtp.voice.gate import DENIED, VOICE_ALLOWED_TOOLS, decide, denial_message
from vtp.voice.session import VOICE_SYSTEM_PROMPT, mcp_server_config

#: Every tool the server exposes, as of the seven-tool surface. Kept literal rather
#: than imported from the server so that adding a tool there does not silently update
#: this list and paper over the very drift `test_every_server_tool_is_classified`
#: exists to catch.
SERVER_TOOLS = {
    "mcp__vtp__list_templates",
    "mcp__vtp__design_part",
    "mcp__vtp__slice_part",
    "mcp__vtp__get_printer_status",
    "mcp__vtp__start_print",
    "mcp__vtp__cancel_print",
    "mcp__vtp__calibrate_bed",
}


# --------------------------------------------------------------------------- #
# The refusal that matters
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool",
    ["mcp__vtp__start_print", "mcp__vtp__cancel_print", "mcp__vtp__calibrate_bed"],
)
def test_a_voice_session_cannot_move_the_machine(tool):
    """The whole point of the module. "Sure, go ahead" is a cheap utterance."""
    verdict = decide(tool)
    assert verdict.allowed is False
    assert verdict.reason


def test_the_start_print_refusal_says_how_to_confirm_instead():
    """A refusal that does not say what to do next just gets retried."""
    reason = decide("mcp__vtp__start_print").reason
    assert "voice" in reason
    assert "phone" in reason


def test_design_and_slice_are_allowed():
    """Voice may design and slice freely — both are reversible and print nothing."""
    for tool in ("mcp__vtp__design_part", "mcp__vtp__slice_part"):
        assert decide(tool).allowed is True


def test_reads_are_allowed():
    for tool in ("mcp__vtp__list_templates", "mcp__vtp__get_printer_status"):
        assert decide(tool).allowed is True


# --------------------------------------------------------------------------- #
# Allowlist, not blocklist
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__vtp__some_future_tool",
        "mcp__other_server__start_print",
        "Bash",
        "Write",
        "",
    ],
)
def test_anything_not_named_is_denied(tool):
    """Including tools that do not exist yet.

    A blocklist would silently admit the next machine-moving tool somebody adds to the
    server. Adding a tool must not quietly widen what a microphone can do.
    """
    assert decide(tool).allowed is False


def test_the_denied_set_and_the_allowed_set_do_not_overlap():
    assert not set(VOICE_ALLOWED_TOOLS) & set(DENIED)


def test_every_server_tool_is_classified():
    """No tool may be merely un-mentioned.

    A machine-moving tool absent from both lists is still denied by the allowlist, but
    it would be denied with the generic message instead of one explaining *why* — and
    an unexplained refusal is one a person argues with. This fails when the server
    grows a tool, which is the moment to decide which list it belongs in.
    """
    classified = set(VOICE_ALLOWED_TOOLS) | set(DENIED)
    assert SERVER_TOOLS <= classified, SERVER_TOOLS - classified


def test_an_unknown_tool_still_gets_a_readable_refusal():
    message = denial_message("mcp__vtp__invented")
    assert "not available from a voice session" in message
    assert "design, slice" in message


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #


def test_the_server_command_is_read_from_mcp_json_not_restated(tmp_path):
    """A second copy of the launch command in Python is a copy that drifts."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"vtp": {"command": ".venv/bin/python", "args": ["-m", "vtp.server"]}}}
        ),
        encoding="utf-8",
    )
    config = mcp_server_config(tmp_path)

    assert config["args"] == ["-m", "vtp.server"]
    # Absolute: the agent may be launched from any working directory.
    assert config["command"] == str(tmp_path / ".venv/bin/python")
    assert config["cwd"] == str(tmp_path)


def test_the_real_mcp_json_still_declares_the_server():
    """Guards against the wiring going stale if .mcp.json is restructured."""
    config = mcp_server_config()
    assert config["args"] == ["-m", "vtp.server"]
    assert config["command"].endswith("python")


def test_a_missing_server_declaration_is_named(tmp_path):
    (tmp_path / ".mcp.json").write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="nothing to drive"):
        mcp_server_config(tmp_path)


def test_the_system_prompt_tells_the_model_it_cannot_print():
    """Not the mechanism — the mechanism is `decide`. But a model that knows it will
    be refused explains the refusal instead of retrying it three times.

    Whitespace is normalised before matching: the prompt is hard-wrapped prose, and a
    phrase that happens to straddle a line break is not a behaviour change.
    """
    prompt = " ".join(VOICE_SYSTEM_PROMPT.split())

    assert "cannot start a print" in prompt
    assert "spoken aloud" in prompt
    # Read-back of dimensions matters: speech recognition mangles numbers.
    assert "Read dimensions back" in prompt


# --------------------------------------------------------------------------- #
# The adapter, against the real SDK
#
# `decide` is pure and tested above without the SDK. These check the other half:
# that the adapter speaks the SDK's actual permission protocol. Skipped when the
# voice extra is not installed, because the server must install and run without it.
# --------------------------------------------------------------------------- #

sdk = pytest.importorskip("claude_agent_sdk", reason="voice extra not installed")


def _decide_via_sdk(tool: str):
    import asyncio

    from vtp.voice.session import _permission_callback

    return asyncio.run(_permission_callback(tool, {"outer_l": 40}, None))


def test_the_adapter_denies_start_print_in_the_sdks_own_protocol():
    """A deny the SDK doesn't recognise is a deny that doesn't happen."""
    result = _decide_via_sdk("mcp__vtp__start_print")
    assert result.behavior == "deny"
    assert "voice" in result.message
    # interrupt=False: the model should relay the refusal, not have the turn torn
    # down underneath it with nothing said to the person.
    assert result.interrupt is False


def test_the_adapter_allows_design_and_passes_the_input_through():
    result = _decide_via_sdk("mcp__vtp__design_part")
    assert result.behavior == "allow"
    assert result.updated_input == {"outer_l": 40}


def test_build_options_wires_the_gate_in():
    from vtp.voice.session import build_options

    options = build_options()
    assert options.can_use_tool is not None
    assert set(options.allowed_tools) == set(VOICE_ALLOWED_TOOLS)
    # bypassPermissions would auto-approve MCP tools and skip the callback entirely.
    assert options.permission_mode == "default"
    assert options.mcp_servers["vtp"]["args"] == ["-m", "vtp.server"]
