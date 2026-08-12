"""Speech to text, via faster-whisper.

**CPU is enough, and that was measured rather than assumed.** ``BUILD_PLAN.md`` names
``large-v3-turbo`` and this machine has an RTX 4060, so the GPU looked like the obvious
target. It is not needed: on a 4.6-second utterance, ``base.en`` transcribes in 0.4s on
CPU — about 11x realtime — and ``small.en`` in 0.9s. For push-to-talk, where the person
has just stopped speaking and is waiting, both are far inside the "feels instant" range.

Taking the GPU would have cost over a gigabyte of CUDA wheels (``libcublas.so.12`` and
cuDNN are not present on this machine and are not pulled in by ``faster-whisper``), for
a latency win nobody can perceive. So the default is CPU, and CUDA is opportunistic:
:func:`resolve_device` tries it and falls back with a reason rather than raising.

**The model is loaded once and reused.** A cold ``base.en`` load is ~10 seconds and
``small.en`` ~37; per utterance that would dominate everything else in the loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from vtp.config import stt_settings

__all__ = ["Transcriber", "Transcript", "resolve_device"]

log = logging.getLogger("vtp.voice.stt")


@dataclass(frozen=True)
class Transcript:
    """What was heard, and how much to trust it."""

    text: str
    #: Whisper's average log-probability, roughly -1.0 (poor) to 0.0 (confident).
    #: None when the model reported none.
    confidence: float | None
    seconds: float

    @property
    def empty(self) -> bool:
        """True when nothing usable was said — silence, or a stray keypress."""
        return not self.text.strip()


def resolve_device(preferred: str) -> tuple[str, str, str]:
    """Pick ``(device, compute_type, reason)``, falling back to CPU.

    ``preferred`` is ``"auto"``, ``"cuda"`` or ``"cpu"``. CUDA needs CUDA runtime
    libraries that ``faster-whisper`` does not install; when they are missing,
    CTranslate2 still reports a CUDA device and only fails later, at load. So this
    probes rather than trusting the device count.
    """
    if preferred == "cpu":
        return "cpu", "int8", "cpu requested"

    try:
        import ctranslate2

        if ctranslate2.get_cuda_device_count() < 1:
            raise RuntimeError("no CUDA device")
    except Exception as exc:  # noqa: BLE001 - any probe failure means "use the CPU"
        if preferred == "cuda":
            log.warning("cuda requested but unavailable (%s); using cpu", exc)
        return "cpu", "int8", f"cuda unavailable: {exc}"

    return "cuda", "float16", "cuda available"


class Transcriber:
    """Holds one loaded Whisper model. Construct once, call many times."""

    def __init__(
        self,
        model: str | None = None,
        device: str | None = None,
        *,
        _model_factory: Any = None,
    ) -> None:
        name, preferred, self._language = stt_settings()
        self.model_name = model or name
        self._preferred = device or preferred
        self._factory = _model_factory
        self._model: Any = None
        self.device = ""
        self.device_reason = ""

    def load(self) -> None:
        """Load the model. Slow — call it before the first utterance, not during.

        Falls back to CPU if a CUDA load raises: CTranslate2 reports a CUDA device even
        when the runtime libraries it needs are missing, and only fails here.
        """
        if self._model is not None:
            return

        factory = self._factory
        if factory is None:
            from faster_whisper import WhisperModel

            factory = WhisperModel

        device, compute, reason = resolve_device(self._preferred)
        try:
            self._model = factory(self.model_name, device=device, compute_type=compute)
        except Exception as exc:  # noqa: BLE001
            if device == "cpu":
                raise
            log.warning("cuda load failed (%s); falling back to cpu", exc)
            device, compute, reason = "cpu", "int8", f"cuda load failed: {exc}"
            self._model = factory(self.model_name, device=device, compute_type=compute)

        self.device, self.device_reason = device, reason
        log.info("whisper %s on %s (%s)", self.model_name, device, reason)

    def transcribe(self, audio: Any) -> Transcript:
        """Turn audio into text. ``audio`` is a path or a float32 numpy array at 16kHz."""
        self.load()
        source = str(audio) if isinstance(audio, (str, Path)) else audio
        segments, info = self._model.transcribe(source, language=self._language)

        parts, confidences = [], []
        for segment in segments:
            # Whisper segments carry a leading space of their own. Joining them raw
            # doubles every gap, and that doubled text is what gets read aloud and sent
            # to the agent — so strip first, then join.
            text = (segment.text or "").strip()
            if text:
                parts.append(text)
            probability = getattr(segment, "avg_logprob", None)
            if probability is not None:
                confidences.append(probability)

        return Transcript(
            text=" ".join(parts).strip(),
            confidence=(sum(confidences) / len(confidences)) if confidences else None,
            seconds=float(getattr(info, "duration", 0.0) or 0.0),
        )
