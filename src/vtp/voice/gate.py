"""Which tools a voice session may reach, and — more importantly — which it may not.

``BUILD_PLAN.md`` Phase 7 states the constraint plainly: **Gate 3 must not be reachable
from a transcript.** ``bed_confirmed_clear`` is supplied by a human through a non-voice
channel. *"Sure, go ahead"* is a cheap utterance, speech recognition mishears, and the
thing on the other end heats to 200C.

This module is where that stops being a sentence in a document and starts being a
refusal. :func:`decide` is handed to the Claude Agent SDK as its ``can_use_tool``
callback, which the SDK invokes **before** any tool runs, and it returns a denial for
every tool that moves the machine — no matter what the model asked for, no matter what
the transcript said, no matter how the prompt was worded.

**Two layers, deliberately.** The session also passes an ``allowed_tools`` list that
simply omits the machine-moving tools, so they are never auto-approved. This callback
is the one that holds if that list is ever edited carelessly: a name absent from an
allowlist fails open into a permission prompt, and a headless voice loop has nobody to
prompt. A name denied here fails closed.

**Why a pure function.** The decision takes a tool name and returns a verdict, with no
SDK types in its signature. That keeps the rule testable without the agent SDK, without
a microphone, and without a printer — which is the only way this stays honest.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "DENIED",
    "VOICE_ALLOWED_TOOLS",
    "Decision",
    "decide",
    "denial_message",
]

#: Tools a voice session may call. Everything here is reversible and costs nothing but
#: time: listing templates, building geometry, slicing to G-code, reading the printer.
#: Slicing writes a file and stops — the file is not a print.
VOICE_ALLOWED_TOOLS: tuple[str, ...] = (
    "mcp__vtp__list_templates",
    "mcp__vtp__design_part",
    "mcp__vtp__slice_part",
    "mcp__vtp__get_printer_status",
    # Looking a part up is most of what designing a case *is* — "a box for a BNO085"
    # is only answerable if the board's dimensions can be found. Without these the
    # model has to answer from memory, and observed behaviour was worse than useless:
    # it said "got the board size from Adafruit" for numbers it had merely recalled,
    # naming a source it had never opened. Reading a web page moves no machine, so it
    # is outside what this gate exists to stop.
    "WebSearch",
    "WebFetch",
)

#: Tools that move the machine, mapped to why voice may not reach them. Each of these
#: already refuses in Python inside the server; this is the second lock, on the door
#: the voice loop opens.
DENIED: dict[str, str] = {
    "mcp__vtp__start_print": (
        "starting a print cannot be authorised by voice. It heats the nozzle to 200C "
        "and runs unattended, and the bed confirmation has to come from a human who "
        "has looked at the plate — through a channel that is not a microphone. Ask "
        "them to confirm on their phone."
    ),
    "mcp__vtp__cancel_print": (
        "cancelling a print cannot be authorised by voice. Speech recognition "
        "mishears, and a misheard word would throw away hours of printing that "
        "cannot be resumed."
    ),
    "mcp__vtp__calibrate_bed": (
        "probing the bed cannot be authorised by voice. It drives the nozzle down "
        "onto the plate at dozens of points, so it needs the same human bed check a "
        "print does."
    ),
}


@dataclass(frozen=True)
class Decision:
    """Whether a voice session may call a tool, and what to say if not."""

    allowed: bool
    #: A sentence for the model to relay to the person. Empty when allowed.
    reason: str = ""


def denial_message(tool_name: str) -> str:
    """The refusal for a tool, phrased for reading aloud."""
    known = DENIED.get(tool_name)
    if known:
        return f"Refused: {known}"
    return (
        f"Refused: {tool_name!r} is not available from a voice session. This session "
        f"can design, slice and read the printer's status, and nothing else."
    )


def decide(tool_name: str) -> Decision:
    """Allow or deny one tool call from a voice session.

    **An allowlist, not a blocklist.** Anything not named in
    :data:`VOICE_ALLOWED_TOOLS` is denied, including tools that do not exist yet. A
    blocklist would silently admit the next machine-moving tool somebody adds to the
    server; this way, adding a tool to the server does not quietly widen what a
    microphone can do — somebody has to come here and decide.
    """
    if tool_name in VOICE_ALLOWED_TOOLS:
        return Decision(allowed=True)
    return Decision(allowed=False, reason=denial_message(tool_name))
