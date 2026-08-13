"""A persistent agent session with the vtp MCP server attached.

Uses ``ClaudeSDKClient`` rather than one-shot ``query()`` calls, because the whole point
of a voice assistant is that *"make it 5mm taller"* resolves against the part you just
designed. A fresh session per utterance would lose that and make every request a
standalone command.

The SDK surface used here was verified against ``code.claude.com/docs/en/agent-sdk``
rather than taken from ``BUILD_PLAN.md``, which said to do exactly that.

**The safety wiring is the interesting part**, and it is two independent layers:

1. ``allowed_tools`` lists only the reversible tools, so nothing else is
   auto-approved.
2. ``can_use_tool`` runs :func:`vtp.voice.gate.decide` before any tool executes and
   denies everything not on that list.

Layer 2 is the one that matters. A name missing from an allowlist fails *open* into a
permission prompt, and a voice loop has nobody to prompt — so an allowlist alone would
leave the decision to whatever the SDK does with an unanswered prompt. A name denied by
the callback fails *closed*, with a sentence the model can read to the person.

``permission_mode`` is left at its default on purpose. ``bypassPermissions`` would
auto-approve MCP tools and skip the callback this module exists for.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from vtp.config import REPO_ROOT, voice_model
from vtp.voice.gate import VOICE_ALLOWED_TOOLS, decide

__all__ = ["VOICE_SYSTEM_PROMPT", "build_options", "mcp_server_config"]

#: Read to the model, not to the user. It says what voice changes about the workflow —
#: the server's own instructions still carry the gates, the house rules and the
#: template contract, and they reach this session the same way they reach any client.
VOICE_SYSTEM_PROMPT = """\
You are driving a 3D printing pipeline for someone speaking to you out loud. Their words
reach you through speech recognition, so expect mishearings, especially in numbers and
units.

HOW TO TALK
Everything you write is read aloud by a speech synthesiser. Talk like a person on the
phone, not like a document.

- Two or three sentences. One is often right.
- Lead with the answer, not the reasoning.
- Say only the numbers that matter, and say them once. Not every dimension you used —
  the ones they need to decide.
- No file paths, no markdown, no bullet lists, no asterisks, no headings. They are
  unpronounceable and the listener cannot see them anyway.
- Do not re-read numbers they just gave you back to them as a summary. Confirm the one
  you are unsure of, or none.
- Do not narrate what you are about to do. Just do it and report the result.

Good: "Done, it's on screen. Eighty by sixty by thirty outside, with the two cable
slots." Bad: "I've now completed the design based on the dimensions you provided. The
outer dimensions are 80mm by 60mm by 30mm, with 2mm walls giving an interior of 76mm by
56mm by 28mm, and I've added STEMMA QT openings measuring 8mm by 6mm on each end..."

BEING HONEST ABOUT WHAT YOU KNOW
You have web search and fetch. Use them when a real-world dimension matters — a board
size, a connector size, a screw spec.

Never say you looked something up unless you actually called a tool in this turn. If you
are working from memory, say so: "from memory, the BNO085 is about 25 by 22" is fine and
useful. "Got it from Adafruit" when you did not open Adafruit is not — they will trust a
number that nothing checked, and it ends up in a printed part.

If you cannot find something and cannot recall it, say so in one sentence and ask for
the one measurement you need.

WHAT YOU CAN AND CANNOT DO
You can design parts, slice them, and read the printer's status. You cannot start a
print, cancel one, or run a bed probe — those need a human confirmation a microphone
cannot supply, and the attempt is refused in code. If asked, say it needs confirming
another way, and stop.

When a design is ready, say it is on screen. A shape cannot be checked by ear.
"""


def mcp_server_config(repo_root: Path | None = None) -> dict[str, Any]:
    """The ``vtp`` stdio server, read from ``.mcp.json`` rather than restated here.

    The repo already declares how to launch the server for every other MCP client. A
    second copy in Python would be a copy that drifts — the same reason
    ``config.bed_extents()`` reads the profile instead of hard-coding 220.

    The command is resolved to an absolute path: ``.mcp.json`` gives it relative to the
    repo root, and the agent may be launched from anywhere.
    """
    root = repo_root or REPO_ROOT
    declared = json.loads((root / ".mcp.json").read_text(encoding="utf-8"))
    try:
        server = declared["mcpServers"]["vtp"]
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"{root / '.mcp.json'} does not declare an 'vtp' server; the voice session "
            f"has nothing to drive."
        ) from exc

    command = Path(server["command"])
    if not command.is_absolute():
        command = root / command
    return {
        "command": str(command),
        "args": list(server.get("args", [])),
        # cwd matters: the server resolves output/ and config/ against the repo root.
        "cwd": str(root),
    }


async def _permission_callback(
    tool_name: str, input_data: dict[str, Any], context: object
) -> Any:
    """Adapt :func:`vtp.voice.gate.decide` to the SDK's ``can_use_tool`` signature.

    Imported lazily so :mod:`vtp.voice.gate` — and its tests — never need the agent SDK
    installed. The rule is in ``gate``; this is only the adapter.
    """
    from claude_agent_sdk.types import PermissionResultAllow, PermissionResultDeny

    verdict = decide(tool_name)
    if verdict.allowed:
        return PermissionResultAllow(updated_input=input_data)
    # interrupt=False: the model should hear the refusal and tell the person why,
    # not have the turn torn down under it.
    return PermissionResultDeny(message=verdict.reason, interrupt=False)


def build_options(repo_root: Path | None = None) -> Any:
    """``ClaudeAgentOptions`` for a voice session. Imports the SDK lazily."""
    from claude_agent_sdk import ClaudeAgentOptions

    return ClaudeAgentOptions(
        # Pinned rather than inherited. Unpinned, a voice session follows whatever the
        # `claude` CLI happens to be set to, so its reasoning about geometry and units
        # silently changes with an unrelated setting — and the person hears the
        # difference without being able to explain it.
        model=voice_model(),
        system_prompt=VOICE_SYSTEM_PROMPT,
        mcp_servers={"vtp": mcp_server_config(repo_root)},
        allowed_tools=list(VOICE_ALLOWED_TOOLS),
        can_use_tool=_permission_callback,
        # Left at the default deliberately — see the module docstring.
        permission_mode="default",
    )
