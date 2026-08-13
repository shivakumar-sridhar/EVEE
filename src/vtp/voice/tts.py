"""Text to speech, via Piper.

Piper over Coqui XTTS, which ``BUILD_PLAN.md`` rules out as abandoned and
non-commercially licensed. It runs on ONNX CPU and synthesises about 37x faster than
realtime here — a spoken sentence is ready before the person could have finished
hearing the previous one.

**Speaking is best effort and never raises.** Same rule the viewer follows: a design is
already correct whether or not a window opened, and an answer is already correct whether
or not it was spoken aloud. A missing voice model, an unplugged speaker, or absent
PortAudio must degrade to printed text rather than taking down a session in the middle
of a conversation about a hot machine.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vtp.config import tts_settings

__all__ = ["Speaker", "Utterance", "find_voice"]

log = logging.getLogger("vtp.voice.tts")


@dataclass(frozen=True)
class Utterance:
    """What happened when we tried to say something."""

    spoken: bool
    detail: str
    wav_path: Path | None = None


def find_voice(directory: Path | None = None, name: str | None = None) -> Path | None:
    """Locate a Piper ``.onnx`` voice, or None.

    Returns None rather than raising: "there is no voice installed" is a reason to print
    instead of speak, not a reason to fail.
    """
    voice_dir, voice_name = tts_settings()[:2]
    directory = directory or voice_dir
    name = name or voice_name

    candidate = Path(directory).expanduser() / f"{name}.onnx"
    if candidate.is_file():
        return candidate

    # Any voice beats no voice — a wrong accent is better than silence.
    for found in sorted(Path(directory).expanduser().glob("*.onnx")):
        log.warning("voice %s not found; using %s", name, found.name)
        return found
    return None


class Speaker:
    """Holds one loaded Piper voice and an audio output path."""

    #: Sentinel for "look one up", so that ``voice_path=None`` can mean what it says:
    #: this speaker has no voice. Without it there is no way to construct a silent
    #: Speaker, because None would trigger discovery and find the installed voice.
    DISCOVER = object()

    def __init__(self, voice_path: Any = DISCOVER, *, _voice_factory: Any = None) -> None:
        # Discovery is deferred to `load()`: a constructor that touches the filesystem
        # is a constructor that behaves differently on someone else's machine.
        self._requested = voice_path
        self._resolved = voice_path is not Speaker.DISCOVER
        self.voice_path: Path | None = None if not self._resolved else voice_path
        self._factory = _voice_factory
        self._voice: Any = None

    def _resolve(self) -> None:
        if not self._resolved:
            self.voice_path = find_voice()
            self._resolved = True

    @property
    def available(self) -> bool:
        self._resolve()
        return self.voice_path is not None

    def load(self) -> bool:
        """Load the voice. Returns whether speech is possible at all."""
        if self._voice is not None:
            return True
        if not self.available:
            return False

        factory = self._factory
        if factory is None:
            from piper import PiperVoice

            factory = PiperVoice.load
        try:
            self._voice = factory(self.voice_path)
        except Exception as exc:  # noqa: BLE001 - never take down a session for this
            log.warning("could not load voice %s: %s", self.voice_path, exc)
            self.voice_path = None
            return False
        return True

    def to_wav(self, text: str, path: Path) -> Utterance:
        """Synthesise to a WAV file. Never raises."""
        if not text.strip():
            return Utterance(False, "nothing to say")
        if not self.load():
            return Utterance(False, "no Piper voice installed; printing instead")

        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = wave.open(str(path), "wb")
            try:
                self._voice.synthesize_wav(text, handle)
            finally:
                # Closing a wave file that was never written raises "# channels not
                # specified", which would mask the real synthesis error with a
                # confusing one. The original is what a person needs to see.
                try:
                    handle.close()
                except Exception:  # noqa: BLE001
                    pass
        except Exception as exc:  # noqa: BLE001
            log.warning("synthesis failed: %s", exc)
            return Utterance(False, f"synthesis failed: {exc}")
        return Utterance(True, "synthesised", path)

    def say(self, text: str, scratch: Path | None = None) -> Utterance:
        """Synthesise and play. Never raises; returns what actually happened."""
        target = scratch or Path("/tmp") / "vtp-voice-say.wav"
        result = self.to_wav(text, target)
        if not result.spoken:
            return result

        played = _play(target)
        if not played:
            return Utterance(False, "synthesised but could not play it", target)
        return Utterance(True, "spoken", target)


#: Players to try, best first.
#:
#: ``pw-play`` leads because **``aplay`` is not safe on a PipeWire system**, which is
#: most desktop Linux now. Against PipeWire's ALSA compatibility layer, ``aplay`` plays
#: the sound and then **fails to exit**, holding the device open — so the first reply is
#: heard, every later one blocks behind a process that never finishes, and the session
#: silently degrades to text. Observed here with three ``aplay`` processes wedged for
#: fifteen minutes. ``pw-play`` played the same file repeatedly with no trouble.
#:
#: ``aplay`` stays last: on a machine with bare ALSA and no sound server it is the only
#: one present, and there it behaves.
_PLAYERS = ("pw-play", "paplay", "aplay")


def _wav_seconds(path: Path) -> float:
    """How long the file is, for sizing the timeout. 0.0 if unreadable."""
    try:
        with wave.open(str(path), "rb") as handle:
            return handle.getnframes() / float(handle.getframerate() or 1)
    except Exception:  # noqa: BLE001
        return 0.0


def _play(path: Path) -> bool:
    """Play a WAV with the first player that works. Never raises.

    The timeout is derived from the file's own length rather than a flat constant. A
    spoken reply is a few seconds; the old 120s ceiling meant one wedged player stalled
    the conversation for two minutes before anyone found out, which is indistinguishable
    from the program having crashed.
    """
    limit = max(10.0, _wav_seconds(path) * 2 + 5.0)

    for player in _PLAYERS:
        binary = shutil.which(player)
        if not binary:
            continue
        try:
            # capture_output: never let a player write to stdout, which under an MCP
            # stdio transport would be the JSON-RPC stream.
            done = subprocess.run(
                [binary, str(path)], capture_output=True, timeout=limit, check=False
            )
            if done.returncode == 0:
                return True
            log.warning("%s exited %s", player, done.returncode)
        except subprocess.TimeoutExpired:
            # subprocess.run kills it for us. Try the next player rather than giving
            # up: a hung player is exactly the failure this list exists to route round.
            log.warning(
                "%s did not finish within %.0fs and was killed; trying the next player",
                player,
                limit,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("%s failed: %s", player, exc)
    return False
