"""Viewer launch tests.

The contract under test is mostly about what does NOT happen: a viewer that cannot
open must not fail a design, and a viewer that does open must not touch this
process's stdio.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time

import pytest

from vtp.viewer import (
    ViewerLaunch,
    _cmdline,
    _remember,
    close_gate,
    discover_display,
    display_available,
    open_gcode,
    open_model,
)


@pytest.fixture
def stl(tmp_path):
    path = tmp_path / "part.stl"
    path.write_bytes(b"solid x\nendsolid x\n")
    return path


@pytest.fixture
def with_display(monkeypatch):
    monkeypatch.setattr("vtp.viewer.discover_display", lambda: {"DISPLAY": ":0"})


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr("vtp.viewer.discover_display", lambda: {})


# --------------------------------------------------------------------------- #
# Refusals — each returns a reason, none raise
# --------------------------------------------------------------------------- #


def test_headless_declines_without_raising(stl, headless):
    launch = open_model([stl])
    assert isinstance(launch, ViewerLaunch)
    assert launch.launched is False
    assert "no display" in launch.reason


def test_missing_binary_names_it_and_the_config_key(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: None)
    launch = open_model([stl], command=["definitely-not-installed"])
    assert launch.launched is False
    assert "definitely-not-installed" in launch.reason
    assert "config/defaults.toml" in launch.reason


def test_auto_open_false_is_respected(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.viewer_settings", lambda: (["true"], False))
    assert open_model([stl]).launched is False


def test_force_overrides_auto_open_false(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.viewer_settings", lambda: (["true"], False))
    assert open_model([stl], force=True).launched is True


def test_empty_path_list_declines(with_display):
    assert open_model([], command=["true"]).launched is False


def test_empty_command_declines(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.viewer_settings", lambda: ([], True))
    launch = open_model([stl])
    assert launch.launched is False
    assert "empty" in launch.reason


def test_unstartable_binary_is_reported_not_raised(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/bin/whatever")
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
    )
    launch = open_model([stl], command=["whatever"])
    assert launch.launched is False
    assert "nope" in launch.reason


# --------------------------------------------------------------------------- #
# Launching
# --------------------------------------------------------------------------- #


def test_launch_appends_every_path_after_the_flags(stl, tmp_path, with_display, monkeypatch):
    second = tmp_path / "lid.stl"
    second.write_bytes(b"solid y\nendsolid y\n")
    seen = {}

    class FakeProc:
        pid = 4321

    def popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return FakeProc()

    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/usr/bin/f3d")
    monkeypatch.setattr(subprocess, "Popen", popen)

    launch = open_model([stl, second], command=["f3d", "--up=+Z"])
    assert launch.launched is True
    assert launch.pid == 4321
    assert seen["argv"][:2] == ["/usr/bin/f3d", "--up=+Z"]
    assert seen["argv"][2:] == [str(stl.resolve()), str(second.resolve())]


def test_child_gets_no_inherited_stdio(stl, with_display, monkeypatch):
    """stdout is JSON-RPC under stdio transport. A viewer logging to it corrupts
    the protocol, so all three streams go to DEVNULL."""
    seen = {}

    class FakeProc:
        pid = 1

    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/usr/bin/f3d")
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kw: (seen.update(kw), FakeProc())[1]
    )
    open_model([stl], command=["f3d"])

    assert seen["stdin"] is subprocess.DEVNULL
    assert seen["stdout"] is subprocess.DEVNULL
    assert seen["stderr"] is subprocess.DEVNULL
    # Its own session, so Ctrl-C in the client's terminal leaves the window alone.
    assert seen["start_new_session"] is True


def test_summary_is_readable_in_both_outcomes(stl, with_display, monkeypatch):
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: None)
    assert "No viewer window" in open_model([stl], command=["absent"]).summary()

    class FakeProc:
        pid = 77

    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/usr/bin/f3d")
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: FakeProc())
    assert "f3d" in open_model([stl], command=["f3d"]).summary()


# --------------------------------------------------------------------------- #
# Display discovery
#
# An MCP stdio client spawns the server with only HOME, LOGNAME, PATH, SHELL, TERM
# and USER. Reading os.environ therefore reports "headless" on a desktop machine,
# which is exactly the case the viewer exists for — so discovery goes to the sockets.
# --------------------------------------------------------------------------- #


@pytest.fixture
def scrubbed_env(monkeypatch):
    """What the MCP SDK actually hands a stdio server."""
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "XDG_RUNTIME_DIR", "XAUTHORITY"):
        monkeypatch.delenv(var, raising=False)


def test_finds_wayland_socket_when_the_client_scrubbed_the_env(
    tmp_path, scrubbed_env, monkeypatch
):
    (tmp_path / "wayland-0").touch()
    (tmp_path / "wayland-0.lock").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: tmp_path)
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", tmp_path / "absent")

    env = discover_display()
    # The .lock beside the socket is not a display.
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert env["XDG_RUNTIME_DIR"] == str(tmp_path)
    assert display_available() is True


def test_falls_back_to_an_x11_socket(tmp_path, scrubbed_env, monkeypatch):
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / ".mutter-Xwaylandauth.AB12CD").write_text("cookie")
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X1").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: runtime)
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", x11)

    env = discover_display()
    assert env["DISPLAY"] == ":1"
    assert env["XAUTHORITY"].endswith(".mutter-Xwaylandauth.AB12CD")
    assert "WAYLAND_DISPLAY" not in env


def test_no_sockets_anywhere_is_headless(tmp_path, scrubbed_env, monkeypatch):
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", tmp_path / "also-absent")
    assert discover_display() == {}
    assert display_available() is False


def test_an_explicitly_set_display_is_trusted_over_discovery(tmp_path, monkeypatch):
    cookie = tmp_path / "cookie"
    cookie.write_text("x")
    monkeypatch.setenv("DISPLAY", ":7")
    monkeypatch.setenv("XAUTHORITY", str(cookie))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: tmp_path / "absent")

    env = discover_display()
    assert env["DISPLAY"] == ":7"
    assert env["XAUTHORITY"] == str(cookie)


def test_display_is_dropped_when_no_cookie_can_be_found(
    tmp_path, scrubbed_env, monkeypatch
):
    """The trap this guards: f3d handed a DISPLAY it cannot authenticate against
    exits 0 with no window and no error, so the failure is invisible. Better to
    report "no display" than to launch something that silently does nothing."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X0").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: runtime)
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", x11)
    monkeypatch.setattr("vtp.viewer.Path.home", lambda: tmp_path / "nohome")

    assert discover_display() == {}


def test_home_xauthority_is_the_last_resort(tmp_path, scrubbed_env, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".Xauthority").write_text("cookie")
    runtime = tmp_path / "run"
    runtime.mkdir()
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X0").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: runtime)
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", x11)
    monkeypatch.setattr("vtp.viewer.Path.home", lambda: home)

    assert discover_display()["XAUTHORITY"] == str(home / ".Xauthority")


def test_wayland_survives_without_a_cookie(tmp_path, scrubbed_env, monkeypatch):
    """A Wayland-native viewer needs no X authority; dropping DISPLAY must not
    also drop the Wayland socket."""
    runtime = tmp_path / "run"
    runtime.mkdir()
    (runtime / "wayland-0").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: runtime)
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", tmp_path / "absent")
    monkeypatch.setattr("vtp.viewer.Path.home", lambda: tmp_path / "nohome")

    env = discover_display()
    assert env["WAYLAND_DISPLAY"] == "wayland-0"
    assert "DISPLAY" not in env


def test_unreadable_x11_socket_is_skipped(tmp_path, scrubbed_env, monkeypatch):
    """Another user's socket is visible but unusable."""
    x11 = tmp_path / "x11"
    x11.mkdir()
    (x11 / "X0").touch()
    monkeypatch.setattr("vtp.viewer._runtime_dir", lambda: tmp_path / "absent")
    monkeypatch.setattr("vtp.viewer._X11_SOCKET_DIR", x11)
    monkeypatch.setattr("vtp.viewer.os.access", lambda p, mode: False)
    assert discover_display() == {}


def test_child_receives_the_rediscovered_session_variables(stl, monkeypatch):
    """The whole point: the client stripped these, so we put them back."""
    seen = {}

    class FakeProc:
        pid = 5

    monkeypatch.setattr(
        "vtp.viewer.discover_display",
        lambda: {"WAYLAND_DISPLAY": "wayland-0", "XDG_RUNTIME_DIR": "/run/user/1000"},
    )
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/usr/bin/f3d")
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kw: (seen.update(kw), FakeProc())[1]
    )

    assert open_model([stl], command=["f3d"]).launched is True
    assert seen["env"]["WAYLAND_DISPLAY"] == "wayland-0"
    assert seen["env"]["XDG_RUNTIME_DIR"] == "/run/user/1000"
    # Inherited variables survive alongside the injected ones.
    assert "PATH" in seen["env"]


# --------------------------------------------------------------------------- #
# Gate 2 — G-code viewer
# --------------------------------------------------------------------------- #


def test_open_gcode_uses_its_own_configured_command(tmp_path, with_display, monkeypatch):
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; hi", encoding="utf-8")
    seen = {}

    class FakeProc:
        pid = 9

    monkeypatch.setattr(
        "vtp.viewer.gcode_viewer_settings",
        lambda: (["prusa-gcodeviewer"], True, "[viewer].gcode_auto_open"),
    )
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: "/usr/bin/prusa-gcodeviewer")
    monkeypatch.setattr(
        subprocess, "Popen", lambda argv, **kw: (seen.update({"argv": argv}), FakeProc())[1]
    )

    launch = open_gcode(gcode)
    assert launch.launched is True
    assert seen["argv"] == ["/usr/bin/prusa-gcodeviewer", str(gcode.resolve())]


def test_open_gcode_declines_headless_without_raising(tmp_path, headless):
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; hi", encoding="utf-8")
    assert open_gcode(gcode).launched is False


def test_open_gcode_names_its_own_config_key(tmp_path, with_display, monkeypatch):
    """The reason must point at the key to edit, not the other gate's key."""
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; hi", encoding="utf-8")
    monkeypatch.setattr("vtp.viewer.shutil.which", lambda _: None)
    reason = open_gcode(gcode, command=["absent"], force=True).reason
    assert "gcode_command" in reason


def test_gate_2_is_off_by_default_and_says_which_key_turns_it_on(tmp_path, with_display):
    """Slicing is headless; the toolpath window is opt-in and must say so."""
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; hi", encoding="utf-8")

    launch = open_gcode(gcode)
    assert launch.launched is False
    # Not the shared auto_open — the human would edit the wrong line.
    assert "gcode_auto_open" in launch.reason


def test_no_screen_beats_the_gate_2_preference(tmp_path, headless, monkeypatch):
    """With auto_open off it is the machine, not the preference, that is reported."""
    gcode = tmp_path / "part.gcode"
    gcode.write_text("; hi", encoding="utf-8")
    monkeypatch.setattr(
        "vtp.viewer.gcode_viewer_settings",
        lambda: (["prusa-gcodeviewer"], False, "[viewer].auto_open"),
    )
    assert "[viewer].auto_open" in open_gcode(gcode).reason


# --------------------------------------------------------------------------- #
# One window per gate
# --------------------------------------------------------------------------- #


@pytest.fixture
def own_state(isolated_viewer_state):
    """The scratch state file conftest's autouse fixture already installed.

    Deliberately not a second monkeypatch of ``_state_path``: two fixtures racing
    to patch the same name is how these tests silently passed against the wrong
    file once already.
    """
    return isolated_viewer_state


LIVE_ARGV = ["tail", "-f", "/dev/null"]


def live_process():
    """A harmless long-running child, waited on until its exec has landed.

    Straight after the fork, ``/proc/<pid>/cmdline`` still reads back the
    *parent's* argv — python's — so a test that samples it immediately records
    the wrong identity and then fails to recognise its own process. Production
    does not have this problem: ``_launch`` records the argv it is about to run,
    not whatever /proc says in the instant after spawning.
    """
    proc = subprocess.Popen(
        LIVE_ARGV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    for _ in range(100):
        if _cmdline(proc.pid) == LIVE_ARGV:
            return proc
        time.sleep(0.02)

    proc.kill()
    proc.wait()
    raise AssertionError(f"{LIVE_ARGV} never appeared in /proc/{proc.pid}/cmdline")


def test_close_gate_does_nothing_when_it_remembers_no_window(own_state):
    assert close_gate("design") is None


def test_close_gate_closes_the_window_it_recorded(own_state):
    proc = live_process()
    try:
        _remember("design", proc.pid, _cmdline(proc.pid))
        detail = close_gate("design")

        assert detail is not None and str(proc.pid) in detail
        assert proc.wait(timeout=5) is not None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_close_gate_spares_a_pid_that_is_no_longer_ours(own_state):
    """The safety property: a reused pid must never be signalled.

    A stranger's PrusaSlicer inheriting the number of one we opened is the whole
    reason identity is the recorded argv rather than the pid alone.
    """
    proc = live_process()
    try:
        own_state.write_text(
            json.dumps(
                {"design": {"pid": proc.pid, "cmdline": ["/usr/bin/prusa-slicer", "old"]}}
            )
        )
        assert close_gate("design") is None
        assert proc.poll() is None, "closed a process that was not ours"
    finally:
        proc.kill()
        proc.wait()


def test_a_second_design_replaces_the_first_window(tmp_path, with_display, own_state):
    """The bug this exists for: two designs must not leave two windows."""
    stl = tmp_path / "part.stl"
    stl.write_text("solid x\nendsolid x\n", encoding="utf-8")

    first = open_model([stl], command=["tail", "-f"], force=True)
    assert first.launched is True
    assert first.replaced is None

    second = open_model([stl], command=["tail", "-f"], force=True)
    assert second.launched is True
    assert second.replaced is not None and str(first.pid) in second.replaced
    assert _cmdline(first.pid) is None, "the first window is still running"

    os.kill(second.pid, signal.SIGKILL)


def test_the_two_gates_do_not_close_each_others_windows(own_state):
    _remember("design", 111, ["prusa-slicer", "a.3mf"])
    _remember("gcode", 222, ["prusa-gcodeviewer", "b.gcode"])

    state = json.loads(own_state.read_text())
    assert state["design"]["pid"] == 111
    assert state["gcode"]["pid"] == 222


def test_a_corrupt_state_file_is_treated_as_no_memory(own_state):
    own_state.write_text("{ not json")
    assert close_gate("design") is None
