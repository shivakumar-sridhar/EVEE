"""The conversation loop: listen, ask the agent, speak the answer.

This is a **frontend**. It converts speech to text, hands the text to a persistent
``ClaudeSDKClient`` session with the ``vtp`` MCP server attached, and speaks the reply.
It contains no geometry, no slicer knowledge and no printer knowledge — every rule about
what may be built and what may be printed still lives server-side, where it applies to
every client rather than only to this one.

**Persistent session, not one query per utterance.** That is what makes *"make it 5mm
taller"* resolve against the part just designed, instead of being a standalone command
with no referent.

**Degrades rather than refusing to start.** No microphone means typed input; no voice
model means printed replies. Both are worth more than a clean failure: a session that
can still design and slice from the keyboard is a working session.

The one thing that does **not** degrade is the tool gate — see :mod:`vtp.voice.gate`.
There is no flag here to turn it off, and adding one would be the bug.
"""

from __future__ import annotations

import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from vtp.config import tts_settings
from vtp.voice.audio import microphone_available, record_until_enter
from vtp.voice.gate import DENIED
from vtp.voice.stt import Transcriber
from vtp.voice.tts import Speaker

__all__ = ["VoiceLoop", "reply_text", "should_quit"]

log = logging.getLogger("vtp.voice.loop")

#: Said once at startup, so the person knows what this can and cannot do before they
#: ask for something it will refuse.
GREETING = (
    "Ready. I can design a part, slice it, and check the printer. "
    "I can't start or stop a print — that needs a confirmation from you another way."
)

_QUIT_WORDS = {"quit", "exit", "stop", "goodbye", "bye", "that's all", "thats all"}


def should_quit(text: str) -> bool:
    """Whether an utterance means "we're done".

    Matched on the whole cleaned utterance, never on a substring: "stop" ends the
    session, but "make the walls stop at the rim" is a design request. A substring test
    would end the session on the second one.
    """
    cleaned = text.strip().strip(".!?,").lower()
    return cleaned in _QUIT_WORDS


def reply_text(message: Any) -> str:
    """Pull speakable text out of an SDK message, or "" if it carries none.

    Tool calls, results and thinking all arrive as messages too; only assistant text is
    worth reading aloud.
    """
    blocks = getattr(message, "content", None)
    if not isinstance(blocks, list):
        return ""
    return " ".join(
        block.text.strip()
        for block in blocks
        if getattr(block, "text", None) and isinstance(block.text, str)
    ).strip()


@dataclass
class VoiceLoop:
    """One conversation. Construct, ``await run()``."""

    speak: bool = True
    listen: bool = True
    transcriber: Transcriber = field(default_factory=Transcriber)
    speaker: Speaker = field(default_factory=Speaker)
    scratch: Path = field(default_factory=lambda: Path("/tmp/vtp-voice"))
    #: So a broken speaker is reported once, not on every single reply.
    _warned_silent: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.speak and not tts_settings()[2]:
            self.speak = False

    # -- output ------------------------------------------------------------- #

    def emit(self, text: str) -> None:
        """Say it if possible, print it either way.

        Printing always happens: a spoken sentence is gone the moment it is said, and a
        person who mishears a dimension has nothing to check it against.
        """
        if not text:
            return
        print(f"\n  {text}\n", flush=True)
        if not self.speak:
            return

        result = self.speaker.say(text, self.scratch / "reply.wav")
        if result.spoken:
            return

        # Told once, out loud in the transcript, not buried at debug level. Silently
        # dropping to text is how somebody ends up thinking the voice "just stopped
        # working" and has nothing to report but that. After the first time, stay
        # quiet — repeating it on every reply would be worse than the original bug.
        if not self._warned_silent:
            print(f"  (speech unavailable: {result.detail} — printing only)\n", flush=True)
            self._warned_silent = True
        log.warning("not spoken: %s", result.detail)

    # -- input -------------------------------------------------------------- #

    def hear(self) -> str | None:
        """One utterance, spoken or typed. None means the person is done."""
        if self.listen:
            recording = record_until_enter("  [speak, then press Enter] ")
            if recording is None:
                log.warning("microphone became unusable; switching to typed input")
                self.listen = False
            elif recording.silent:
                self.emit("I didn't catch that.")
                return ""
            else:
                heard = self.transcriber.transcribe(recording.samples)
                if heard.empty:
                    self.emit("I didn't catch that.")
                    return ""
                print(f'  heard: "{heard.text}"', flush=True)
                return heard.text

        try:
            return input("  you> ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

    # -- the loop ----------------------------------------------------------- #

    async def run(self) -> None:
        """Talk until the person stops. Requires the ``voice`` extra."""
        from claude_agent_sdk import ClaudeSDKClient

        from vtp.voice.session import build_options

        if self.listen and not sys.stdin.isatty():
            # Push-to-talk needs somewhere to press Enter. Piped or run from an agent's
            # shell there is no terminal, and trying to record anyway ends in an
            # EOFError from the middle of the capture. Typed input still works from a
            # pipe, so degrade to it and say why.
            print(
                "  No terminal to press Enter in, so push-to-talk is off.\n"
                "  Reading typed input instead — run this in a real terminal to speak.\n"
            )
            self.listen = False

        if self.listen:
            usable, reason = microphone_available()
            if not usable:
                print(f"  No microphone ({reason}).\n  Falling back to typed input.\n")
                self.listen = False

        if self.listen:
            # Load before the first utterance, not during it: a cold Whisper load is
            # seconds, and paying it while somebody waits for an answer feels broken.
            print("  Loading speech recognition...", flush=True)
            self.transcriber.load()
            print(f"  Whisper {self.transcriber.model_name} on {self.transcriber.device}.")

        if self.speak and not self.speaker.available:
            print("  No Piper voice installed; replies will be printed only.")
            self.speak = False

        async with ClaudeSDKClient(options=build_options()) as client:
            self.emit(GREETING)
            while True:
                said = self.hear()
                if said is None:
                    break
                if not said:
                    continue
                if should_quit(said):
                    self.emit("Okay, stopping there.")
                    break

                await client.query(said)
                spoken_anything = False
                async for message in client.receive_response():
                    text = reply_text(message)
                    if text:
                        self.emit(text)
                        spoken_anything = True

                if not spoken_anything:
                    # A turn that produced only tool calls and no words leaves the
                    # person listening to silence with no idea whether it worked.
                    self.emit("Done, but I have nothing to read back.")

    def explain_limits(self) -> str:
        """What this session will refuse, for printing at startup."""
        names = ", ".join(sorted(t.rsplit("__", 1)[-1] for t in DENIED))
        return f"Refused from voice: {names}."
