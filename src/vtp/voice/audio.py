"""Microphone capture, push-to-talk.

**Push-to-talk, not a wake word** — ``BUILD_PLAN.md`` settles that for v1, and it is the
right default for a device that can drive a 200C machine: nothing is recorded until a
person presses a key, so the microphone is not live in the room.

``sounddevice`` binds PortAudio, which is a **system** package rather than a wheel, so a
successful ``pip install`` is not enough and the import raises without it.

Rather than making that a hard requirement, capture falls back to ALSA's ``arecord``,
which ships in ``alsa-utils`` on most desktop Linux installs — this machine has it and
no PortAudio, so push-to-talk works here today with no ``sudo`` at all. ``aplay`` covers
the output side the same way in :mod:`vtp.voice.tts`.

If neither works, :func:`microphone_available` says so without raising and the loop falls
back to typed input rather than dying at startup: a session that can still design and
slice from the keyboard is worth more than a clean stack trace.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vtp.config import push_to_talk_settings

__all__ = ["Recording", "microphone_available", "record_until_enter"]

log = logging.getLogger("vtp.voice.audio")


@dataclass(frozen=True)
class Recording:
    """Captured audio, ready for Whisper: float32 mono at ``sample_rate``."""

    samples: Any
    sample_rate: int
    seconds: float
    truncated: bool = False

    @property
    def silent(self) -> bool:
        """True when nothing was actually said.

        Whisper hallucinates confidently on silence — it will happily transcribe an
        empty room as "Thank you." or a subtitle credit — so near-silent audio is
        dropped here rather than sent on and second-guessed later.

        **A zero-length recording is silent.** ``np.abs([]).max()`` raises on an empty
        array, and an earlier version let that fall through to "not silent", which sent
        zero samples to Whisper and took its hallucination as the utterance. Emptiness
        is the most silent a recording can be; it is checked first.
        """
        # Not `self.samples or []`: a numpy array has no truth value and raises.
        size = getattr(self.samples, "size", None)
        if size is None:
            try:
                size = len(self.samples)
            except TypeError:
                size = 0
        if not size:
            return True
        try:
            import numpy as np

            return bool(np.abs(self.samples).max() < 0.01)
        except Exception:  # noqa: BLE001
            # Unknown rather than empty — let it through and let Whisper decide.
            return False


def _arecord_available() -> bool:
    """Whether ALSA's ``arecord`` can be used instead of PortAudio.

    Worth checking before telling somebody to install a system package: ``alsa-utils``
    ships on most desktop Linux installs, and it means push-to-talk works today
    without sudo. ``aplay`` already covers the output side the same way.
    """
    if shutil.which("arecord") is None:
        return False
    try:
        listed = subprocess.run(
            ["arecord", "-l"], capture_output=True, text=True, timeout=10, check=False
        )
    except Exception:  # noqa: BLE001
        return False
    return "card" in (listed.stdout or "")


def microphone_available() -> tuple[bool, str]:
    """``(usable, reason)``. Never raises.

    Tries PortAudio first, then ALSA's ``arecord``. Checks for an input *device* as
    well as a library: both import and run fine on a machine with no microphone, and
    failing at the first recording rather than at startup is a worse experience.
    """
    try:
        import sounddevice as sd

        inputs = [d for d in sd.query_devices() if d.get("max_input_channels", 0) > 0]
        if inputs:
            return True, f"{len(inputs)} input device(s) via PortAudio"
        portaudio_reason = "PortAudio reports no input device"
    except Exception as exc:  # noqa: BLE001
        portaudio_reason = f"PortAudio unusable ({exc})"

    if _arecord_available():
        return True, "using arecord (PortAudio not available)"

    return False, (
        f"{portaudio_reason}, and arecord found no capture device. Install one of them "
        f"— on Debian or Ubuntu: sudo apt install libportaudio2  (or alsa-utils)"
    )


def record_until_enter(prompt: str = "[press Enter to stop] ") -> Recording | None:
    """Record from the default microphone until the human presses Enter.

    Returns None if the microphone is unusable — the caller falls back to typing.

    Enter rather than key-held-down deliberately: reading a held key needs either a raw
    terminal or an X grab, and neither survives being run over SSH or inside an editor's
    terminal. Enter works everywhere, which matters more than the ergonomics.
    """
    usable, reason = microphone_available()
    if not usable:
        log.warning("no microphone: %s", reason)
        return None

    import numpy as np

    rate, max_seconds, _silence = push_to_talk_settings()

    try:
        import sounddevice as sd
    except Exception:  # noqa: BLE001
        return _record_with_arecord(prompt, rate, max_seconds)

    frames: list[Any] = []

    def collect(indata, _frames, _time, status):
        if status:
            log.debug("audio status: %s", status)
        frames.append(indata.copy())

    try:
        with sd.InputStream(
            samplerate=rate, channels=1, dtype="float32", callback=collect
        ):
            input(prompt)
    except Exception as exc:  # noqa: BLE001
        log.warning("recording failed: %s", exc)
        return None

    if not frames:
        return Recording(np.zeros(0, dtype="float32"), rate, 0.0)

    samples = np.concatenate(frames, axis=0).reshape(-1)
    truncated = False
    limit = int(max_seconds * rate)
    if samples.size > limit:
        # A bound on a stuck key or a walked-away-from session, not a trim of normal
        # speech: 30s is far longer than any sentence describing a box.
        samples, truncated = samples[:limit], True

    return Recording(samples, rate, samples.size / rate, truncated)


def _record_with_arecord(prompt: str, rate: int, max_seconds: float) -> Recording | None:
    """Record via ALSA's ``arecord`` when PortAudio is not installed.

    Same contract as :func:`record_until_enter`: capture until Enter, never raise,
    return None if it could not be done. ``arecord`` writes 16-bit PCM, which is
    converted to the float32 Whisper wants.

    ``-d`` bounds the recording even if the Enter never comes — a subprocess left
    running because a session was abandoned would otherwise fill a disk.
    """
    import numpy as np

    with tempfile.TemporaryDirectory() as scratch:
        target = Path(scratch) / "utterance.wav"
        command = [
            "arecord",
            "-q",
            "-f", "S16_LE",
            "-r", str(rate),
            "-c", "1",
            "-d", str(int(max_seconds) + 1),
            "-t", "wav",
            str(target),
        ]
        try:
            # stdout captured: arecord is chatty, and under an MCP stdio transport
            # stray output would land on the JSON-RPC stream.
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("arecord failed to start: %s", exc)
            return None

        try:
            input(prompt)
        except (EOFError, KeyboardInterrupt):
            # No terminal to press Enter in, or the person gave up. Both mean "no
            # utterance", not "crash". `finally` alone would let this escape and take
            # the whole conversation down with a traceback.
            log.debug("capture interrupted before Enter")
            return None
        finally:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

        if not target.is_file() or target.stat().st_size == 0:
            log.warning("arecord produced no audio")
            return Recording(np.zeros(0, dtype="float32"), rate, 0.0)

        try:
            with wave.open(str(target), "rb") as handle:
                frames = handle.readframes(handle.getnframes())
                captured_rate = handle.getframerate()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not read the recording: %s", exc)
            return None

    # S16_LE -> float32 in [-1, 1], which is what Whisper expects.
    samples = np.frombuffer(frames, dtype="<i2").astype("float32") / 32768.0
    truncated = samples.size >= int(max_seconds * captured_rate)
    return Recording(samples, captured_rate, samples.size / captured_rate, truncated)
