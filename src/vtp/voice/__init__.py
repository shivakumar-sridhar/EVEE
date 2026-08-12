"""Phase 7 — voice as a frontend that drives an agent, not a second pipeline.

The voice layer converts speech to text, hands the text to a Claude Agent SDK session
that has the ``vtp`` MCP server attached, and speaks the reply. **It does not talk to
the MCP server itself and it holds no geometry logic** — every rule about what may be
built and what may be printed still lives server-side, where every client is subject
to it.

Two constraints from ``BUILD_PLAN.md`` shape everything here:

- **Gate 1 needs a screen.** The point of that gate is looking at the preview; a 3D
  shape cannot be reviewed by ear. ``design_part`` already opens PrusaSlicer, and the
  spec sentence read aloud confirms the *numbers* well and the *shape* not at all.
- **Gate 3 must not be reachable from a transcript.** Enforced in :mod:`vtp.voice.gate`
  as a Python refusal on the agent's tool-permission callback, not as a line in a
  prompt.
"""

from vtp.voice.gate import DENIED, VOICE_ALLOWED_TOOLS, Decision, decide

__all__ = ["DENIED", "VOICE_ALLOWED_TOOLS", "Decision", "decide"]
