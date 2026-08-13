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
    assert "read aloud" in prompt


def test_the_system_prompt_asks_for_short_spoken_replies():
    """The complaint that prompted this: replies read like documents, not speech."""
    prompt = " ".join(VOICE_SYSTEM_PROMPT.split())

    assert "Two or three sentences" in prompt
    assert "No file paths" in prompt
    # A worked contrast beats an adjective — "be concise" alone did not work.
    assert "Good:" in prompt and "Bad:" in prompt


def test_the_system_prompt_forbids_claiming_an_unmade_lookup():
    """Observed live: it said "got the board size from Adafruit" for numbers it had
    only recalled, naming a source it never opened. A number nobody checked ends up
    in a printed part."""
    prompt = " ".join(VOICE_SYSTEM_PROMPT.split())

    assert "Never say you looked something up unless you actually called a tool" in prompt
    assert "from memory" in prompt


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


# --------------------------------------------------------------------------- #
# Transcription, synthesis and the loop
#
# None of these touch a microphone, a speaker, or a model download. The audio
# stack is injected or probed, because a test that needs hardware is a test that
# does not run.
# --------------------------------------------------------------------------- #


import wave
from pathlib import Path
from types import SimpleNamespace

from vtp.voice.loop import GREETING, VoiceLoop, reply_text, should_quit
from vtp.voice.stt import Transcriber, resolve_device
from vtp.voice.tts import Speaker, find_voice


def test_cpu_is_chosen_when_asked_for():
    device, compute, reason = resolve_device("cpu")
    assert (device, compute) == ("cpu", "int8")
    assert "cpu" in reason


def test_cuda_falls_back_to_cpu_rather_than_raising(monkeypatch):
    """CTranslate2 reports a CUDA device even when its runtime libraries are absent,
    so an unavailable GPU must degrade rather than take down the session."""
    import builtins

    real_import = builtins.__import__

    def no_ctranslate2(name, *args, **kwargs):
        if name == "ctranslate2":
            raise ImportError("libcublas.so.12 is not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ctranslate2)
    device, compute, reason = resolve_device("cuda")

    assert (device, compute) == ("cpu", "int8")
    assert "cuda unavailable" in reason


def test_a_failed_cuda_load_retries_on_cpu():
    """The failure surfaces at model load, not at device probe."""
    attempts = []

    def factory(model, device, compute_type):
        attempts.append(device)
        if device == "cuda":
            raise RuntimeError("Library libcublas.so.12 is not found")
        return SimpleNamespace(transcribe=lambda *a, **k: ([], SimpleNamespace(duration=0)))

    transcriber = Transcriber(model="base.en", device="auto", _model_factory=factory)
    transcriber.load()

    assert transcriber.device == "cpu"
    assert "cuda load failed" in transcriber.device_reason


def test_transcribe_joins_segments_and_averages_confidence():
    segments = [
        SimpleNamespace(text=" Make me a box", avg_logprob=-0.2),
        SimpleNamespace(text=" fifty long.", avg_logprob=-0.4),
    ]
    factory = lambda *a, **k: SimpleNamespace(  # noqa: E731
        transcribe=lambda *a, **k: (iter(segments), SimpleNamespace(duration=4.6))
    )
    heard = Transcriber(device="cpu", _model_factory=factory).transcribe("x.wav")

    assert heard.text == "Make me a box fifty long."
    assert heard.confidence == pytest.approx(-0.3)
    assert heard.seconds == pytest.approx(4.6)
    assert heard.empty is False


def test_an_empty_transcript_is_recognised_as_nothing_said():
    factory = lambda *a, **k: SimpleNamespace(  # noqa: E731
        transcribe=lambda *a, **k: (iter([]), SimpleNamespace(duration=0.0))
    )
    assert Transcriber(device="cpu", _model_factory=factory).transcribe("x.wav").empty


def test_a_missing_voice_directory_returns_none_rather_than_raising(tmp_path):
    """No voice installed is a reason to print, not a reason to fail."""
    assert find_voice(tmp_path, "absent") is None


def test_any_voice_beats_no_voice(tmp_path):
    (tmp_path / "en_GB-other-medium.onnx").write_bytes(b"x")
    assert find_voice(tmp_path, "en_US-lessac-medium").name == "en_GB-other-medium.onnx"


def test_speaking_without_a_voice_is_reported_not_raised(tmp_path):
    """`voice_path=None` means this speaker has no voice — not "go and find one"."""
    speaker = Speaker(voice_path=None)
    result = speaker.say("hello", tmp_path / "out.wav")
    assert result.spoken is False
    assert "no Piper voice" in result.detail


def test_a_synthesis_failure_never_escapes(tmp_path):
    """A session about a hot machine must not die because a voice model misbehaved."""

    def exploding_voice(_path):
        return SimpleNamespace(
            synthesize_wav=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
        )

    speaker = Speaker(voice_path=tmp_path / "v.onnx", _voice_factory=exploding_voice)
    result = speaker.to_wav("hello", tmp_path / "out.wav")

    assert result.spoken is False
    assert "boom" in result.detail


# -- the loop --------------------------------------------------------------- #


@pytest.mark.parametrize("said", ["quit", "Stop.", "goodbye", "  Exit  ", "that's all"])
def test_quit_words_end_the_session(said):
    assert should_quit(said) is True


@pytest.mark.parametrize(
    "said",
    [
        "make the walls stop at the rim",
        "add a stop block",
        "design a box",
        "can you stop the print",  # refused by the gate, not by the quit check
    ],
)
def test_a_quit_word_inside_a_sentence_does_not_end_the_session(said):
    """Matched on the whole utterance, never a substring — otherwise "make the walls
    stop at the rim" hangs up on the person mid-design."""
    assert should_quit(said) is False


def test_reply_text_takes_only_assistant_text():
    message = SimpleNamespace(
        content=[
            SimpleNamespace(text="Outer 40 by 30 by 15."),
            SimpleNamespace(name="mcp__vtp__design_part", input={}),  # a tool call
            SimpleNamespace(text="It's on screen."),
        ]
    )
    assert reply_text(message) == "Outer 40 by 30 by 15. It's on screen."


def test_reply_text_ignores_messages_with_no_content():
    assert reply_text(SimpleNamespace()) == ""
    assert reply_text(SimpleNamespace(content=None)) == ""


def test_the_greeting_says_what_it_cannot_do():
    """Said before the person asks for something that will be refused."""
    assert "can't start or stop a print" in GREETING


def test_the_limits_line_names_every_denied_tool():
    line = VoiceLoop(speak=False, listen=False).explain_limits()
    for tool in DENIED:
        assert tool.rsplit("__", 1)[-1] in line


def test_emit_prints_even_when_speech_is_off(capsys):
    """A spoken sentence is gone the moment it is said; a misheard dimension needs
    something to check against."""
    VoiceLoop(speak=False, listen=False).emit("Outer 40 by 30 by 15 millimetres.")
    assert "Outer 40 by 30 by 15" in capsys.readouterr().out


def test_a_speaker_with_no_voice_never_discovers_one():
    """The distinction the DISCOVER sentinel exists for."""
    assert Speaker(voice_path=None).available is False


def test_a_speaker_discovers_by_default_but_not_in_the_constructor(monkeypatch):
    """Construction must not touch the filesystem — otherwise the object behaves
    differently depending on which machine built it."""
    calls = []
    monkeypatch.setattr("vtp.voice.tts.find_voice", lambda *a, **k: calls.append(1))

    speaker = Speaker()
    assert calls == []          # nothing looked up yet
    speaker.available
    assert calls == [1]         # looked up once, on demand
    speaker.available
    assert calls == [1]         # and cached


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #

from vtp.voice.audio import Recording, microphone_available


def test_an_empty_recording_is_silent():
    """Regression. `np.abs([]).max()` raises, and letting that fall through to
    "not silent" sent zero samples to Whisper — which hallucinates a plausible
    sentence out of nothing and hands it to the agent as what the person said."""
    import numpy as np

    assert Recording(np.zeros(0, dtype="float32"), 16000, 0.0).silent is True


def test_near_silence_is_silent_and_speech_is_not():
    import numpy as np

    quiet = np.full(16000, 0.001, dtype="float32")
    loud = np.full(16000, 0.4, dtype="float32")

    assert Recording(quiet, 16000, 1.0).silent is True
    assert Recording(loud, 16000, 1.0).silent is False


def test_microphone_availability_never_raises_and_explains_itself():
    usable, reason = microphone_available()
    assert isinstance(usable, bool)
    assert reason
    if not usable:
        # A "no" has to say what to install, or it is just a dead end.
        assert "install" in reason.lower()


def test_a_machine_with_neither_backend_says_what_to_install(monkeypatch):
    import builtins

    real_import = builtins.__import__

    def no_sounddevice(name, *args, **kwargs):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sounddevice)
    monkeypatch.setattr("vtp.voice.audio._arecord_available", lambda: False)

    usable, reason = microphone_available()
    assert usable is False
    assert "libportaudio2" in reason
    assert "alsa-utils" in reason


def test_arecord_is_used_when_portaudio_is_missing(monkeypatch):
    """This machine has arecord and no PortAudio, so the fallback is the live path."""
    import builtins

    real_import = builtins.__import__

    def no_sounddevice(name, *args, **kwargs):
        if name == "sounddevice":
            raise OSError("PortAudio library not found")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_sounddevice)
    monkeypatch.setattr("vtp.voice.audio._arecord_available", lambda: True)

    usable, reason = microphone_available()
    assert usable is True
    assert "arecord" in reason


def test_capture_interrupted_before_enter_returns_none_not_a_traceback(monkeypatch):
    """Regression, and it was on this machine's live path.

    `_record_with_arecord` had input() inside try/finally with no except, so with no
    terminal the EOFError escaped and took the whole conversation down mid-capture.
    "No utterance" is not "crash"."""
    from vtp.voice import audio

    class FakeProc:
        def terminate(self): ...
        def wait(self, timeout=None): ...
        def kill(self): ...

    monkeypatch.setattr(audio.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(
        "vtp.voice.audio.wait_for_key",
        lambda *_a, **_k: (_ for _ in ()).throw(EOFError("no tty")),
    )

    assert audio._record_with_arecord(16000, 30.0) is None


def test_a_keyboard_interrupt_during_capture_is_also_not_a_crash(monkeypatch):
    from vtp.voice import audio

    class FakeProc:
        def terminate(self): ...
        def wait(self, timeout=None): ...
        def kill(self): ...

    monkeypatch.setattr(audio.subprocess, "Popen", lambda *a, **k: FakeProc())
    monkeypatch.setattr(
        "vtp.voice.audio.wait_for_key",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    assert audio._record_with_arecord(16000, 30.0) is None


# --------------------------------------------------------------------------- #
# Playback
#
# Regression tests for a live failure: on a PipeWire system `aplay` played the
# first reply, never exited, and held the device — so the greeting was heard and
# every later reply queued behind a process that would never finish. Three aplay
# processes were found wedged for fifteen minutes.
# --------------------------------------------------------------------------- #

from vtp.voice.tts import _PLAYERS, _play, _wav_seconds


def test_pipewire_player_is_preferred_over_aplay():
    """Order is the fix. `aplay` hangs against PipeWire's ALSA layer; pw-play doesn't.
    It stays in the list because on bare ALSA it is the only player present."""
    assert _PLAYERS[0] == "pw-play"
    assert _PLAYERS[-1] == "aplay"


def test_a_hung_player_falls_through_to_the_next(monkeypatch, tmp_path):
    """A wedged player must route around, not end the conversation."""
    import subprocess as sp

    wav = tmp_path / "x.wav"
    with wave.open(str(wav), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(16000)
        h.writeframes(b"\x00\x00" * 16000)

    tried = []

    def run(cmd, **kwargs):
        tried.append(Path(cmd[0]).name)
        if len(tried) == 1:
            raise sp.TimeoutExpired(cmd, kwargs.get("timeout", 1))
        return sp.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("vtp.voice.tts.shutil.which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr("vtp.voice.tts.subprocess.run", run)

    assert _play(wav) is True
    assert len(tried) == 2, "should have moved on to the next player"


def test_the_timeout_is_sized_from_the_audio_not_a_flat_two_minutes(tmp_path):
    """The old flat 120s meant a wedged player stalled the conversation for two
    minutes, which is indistinguishable from a crash."""
    wav = tmp_path / "x.wav"
    with wave.open(str(wav), "wb") as h:
        h.setnchannels(1)
        h.setsampwidth(2)
        h.setframerate(16000)
        h.writeframes(b"\x00\x00" * 16000 * 3)  # 3 seconds

    assert _wav_seconds(wav) == pytest.approx(3.0)

    captured = {}

    def run(cmd, **kwargs):
        import subprocess as sp

        captured["timeout"] = kwargs.get("timeout")
        return sp.CompletedProcess(cmd, 0, b"", b"")

    import pytest as _pytest  # noqa: F401

    with _pytest.MonkeyPatch.context() as mp:
        mp.setattr("vtp.voice.tts.shutil.which", lambda n: f"/usr/bin/{n}")
        mp.setattr("vtp.voice.tts.subprocess.run", run)
        _play(wav)

    assert captured["timeout"] < 30, "a 3s clip must not get a 2-minute budget"


def test_an_unreadable_file_still_gets_a_usable_timeout(tmp_path):
    missing = tmp_path / "nope.wav"
    assert _wav_seconds(missing) == 0.0


def test_a_broken_speaker_is_reported_once_not_silently(capsys):
    """The bug behind "it just stopped talking": the reason was at debug level, so
    the session degraded to text with nothing said about it."""
    from vtp.voice.tts import Utterance

    class Mute(Speaker):
        def __init__(self):
            super().__init__(voice_path=None)

        def say(self, text, scratch=None):
            return Utterance(False, "player wedged")

    loop = VoiceLoop(speak=True, listen=False, speaker=Mute())
    loop.speak = True  # tts_settings() may have switched it off

    loop.emit("first reply")
    loop.emit("second reply")
    out = capsys.readouterr().out

    assert out.count("speech unavailable") == 1, "say it once, not on every reply"
    assert "player wedged" in out
    assert "first reply" in out and "second reply" in out


# --------------------------------------------------------------------------- #
# Space-toggle capture, web tools, pinned model
# --------------------------------------------------------------------------- #


def test_web_tools_are_allowed_but_the_machine_still_is_not():
    """Looking a part up is most of what designing a case is. Reading a web page
    moves no machine, so it is outside what the gate exists to stop."""
    assert decide("WebSearch").allowed is True
    assert decide("WebFetch").allowed is True

    for tool in DENIED:
        assert decide(tool).allowed is False


def test_the_model_is_pinned_not_inherited():
    """Unpinned, voice quality drifts with an unrelated CLI setting and the person
    hears the difference without being able to explain it."""
    from vtp.config import voice_model
    from vtp.voice.session import build_options

    assert voice_model() == "opus"
    assert build_options().model == "opus"


def test_a_quit_key_ends_the_session_rather_than_recording(monkeypatch):
    from vtp.voice import audio

    monkeypatch.setattr(audio, "microphone_available", lambda: (True, "fake"))
    monkeypatch.setattr(audio, "wait_for_key", lambda *a, **k: None)

    assert audio.record_utterance() is None


def test_capture_runs_between_two_presses(monkeypatch):
    """Space to start, space to stop — the second press is what ends the recording."""
    from vtp.voice import audio

    presses = []
    monkeypatch.setattr(audio, "microphone_available", lambda: (True, "fake"))
    monkeypatch.setattr(
        audio, "wait_for_key", lambda *a, **k: (presses.append(1), " ")[1]
    )
    monkeypatch.setattr(audio, "_capture_until_space", lambda: "RECORDING")

    assert audio.record_utterance() == "RECORDING"
    assert len(presses) == 1, "the start press; the stop press is inside the capture"


def test_wait_for_key_returns_none_without_a_terminal(monkeypatch):
    """Piped stdin must end the loop rather than spin on an unreadable fd."""
    from vtp.voice import audio

    monkeypatch.setattr(
        "vtp.voice.audio.termios.tcgetattr",
        lambda _fd: (_ for _ in ()).throw(OSError("not a tty")),
    )
    assert audio.wait_for_key() is None
