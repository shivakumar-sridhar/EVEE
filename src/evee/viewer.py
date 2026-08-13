"""Open exported STLs in a desktop 3D viewer, for the Gate 1 shape check.

A still image proves the numbers but not the shape: a port on the far wall is simply
not in the picture, and no amount of rendering care fixes that. So the design gate
opens the real mesh in a viewer the human can orbit.

Three rules shape this module:

**Failing to show a window must never fail a design.** The STLs are already on disk
and correct. A missing viewer, a headless box, a remote client — none of those make
the geometry wrong, so nothing here raises; :func:`open_model` reports what happened and
the caller falls back to the preview PNGs.

**The server may be nowhere near the human.** ``CLAUDE.md`` is a Claude Code file, but
this server also runs under OpenCode, Cline, or a voice shim, possibly over SSH. No
display means no launch, and that is a normal outcome rather than an error.

**A stdio MCP client scrubs the environment**, which is why :func:`discover_display`
exists. The SDK spawns this server with only ``HOME``, ``LOGNAME``, ``PATH``,
``SHELL``, ``TERM`` and ``USER`` — no ``DISPLAY``, no ``WAYLAND_DISPLAY``, no
``XDG_RUNTIME_DIR`` — even when the user is sitting in front of a desktop. Testing
``os.environ`` alone therefore reports "headless" on a machine with a screen, and the
viewer would never open in the one situation it was built for. We look for the
session's sockets on disk instead, and hand the child what it needs.

**The child process must not touch our stdio.** Under the stdio transport, stdout is
JSON-RPC. A viewer that logs a GL warning to an inherited stdout corrupts the protocol,
so the child gets ``DEVNULL`` on all three streams and its own session.

**Each gate owns exactly one window, and replaces it.** PrusaSlicer's
``--single-instance`` used to reuse one window, which sounded like the same thing and
is not: it *appends* each new design to the existing scene and never clears it, so
two design iterations left four objects stacked on the origin. It also made the
launched process forward its arguments and exit at once, leaving no pid to close.
So the flag is gone; instead the pid of the window each gate opened is remembered in
:func:`_state_path`, and the next launch closes it first.

**A window is only ours if its argv still matches.** Pids are reused. Before signalling
anything we re-read ``/proc/<pid>/cmdline`` and require it to equal the argv we
recorded, so a PrusaSlicer the human opened for their own work is never closed —
not even if it inherited the number of one of ours.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from evee.config import gcode_viewer_settings, viewer_settings

__all__ = [
    "ViewerLaunch",
    "close_gate",
    "display_available",
    "open_gcode",
    "open_model",
]

#: Gate names, used as keys in the state file. One window each.
DESIGN_GATE = "design"
GCODE_GATE = "gcode"

#: How long to let a viewer exit on SIGTERM before resorting to SIGKILL.
_CLOSE_POLL_SECONDS = 0.1
_CLOSE_POLL_COUNT = 20


@dataclass(frozen=True)
class ViewerLaunch:
    """Outcome of trying to show a model. Never an exception."""

    launched: bool
    command: list[str]
    #: Why nothing opened. ``None`` when it did.
    reason: str | None = None
    pid: int | None = None
    #: What happened to the window this gate opened last time, if anything.
    replaced: str | None = None

    def summary(self) -> str:
        """One line for the design gate's read-back."""
        if not self.launched:
            return f"No viewer window: {self.reason}"
        line = f"Opened in {Path(self.command[0]).name} (pid {self.pid})."
        if self.replaced:
            line += f" {self.replaced}"
        return line


#: X11 puts its sockets here as ``X0``, ``X1``, ... one per display number.
_X11_SOCKET_DIR = Path("/tmp/.X11-unix")

#: Where an X authority cookie turns up, most specific first. Under GNOME/Wayland
#: mutter writes one per session with a random suffix; a plain X session uses the
#: file in $HOME; gdm keeps its own.
_XAUTHORITY_GLOBS = (
    ".mutter-Xwaylandauth.*",
    "gdm/Xauthority",
)


def _find_xauthority(runtime: Path) -> str | None:
    """Locate an X authority cookie, or None.

    Without one, connecting to the X server is refused — and f3d reports that
    refusal by **exiting 0 with a window that never appears**, so this is not
    something a caller can detect after the fact. It has to be right up front.
    """
    configured = os.environ.get("XAUTHORITY")
    if configured and Path(configured).is_file():
        return configured

    for pattern in _XAUTHORITY_GLOBS:
        for candidate in sorted(runtime.glob(pattern)):
            if candidate.is_file() and os.access(candidate, os.R_OK):
                return str(candidate)

    home_cookie = Path.home() / ".Xauthority"
    if home_cookie.is_file() and os.access(home_cookie, os.R_OK):
        return str(home_cookie)
    return None


def _runtime_dir() -> Path:
    """The user's XDG runtime directory, derived if the variable was scrubbed.

    ``/run/user/<uid>`` is where systemd-logind puts it, and it belongs to this uid
    by construction — so deriving it is safe in a way that guessing another user's
    display socket would not be.
    """
    configured = os.environ.get("XDG_RUNTIME_DIR")
    return Path(configured) if configured else Path(f"/run/user/{os.getuid()}")


def discover_display() -> dict[str, str]:
    """Environment a GUI child needs, rebuilt from sockets on disk.

    Returns the variables to add to the child's environment, or ``{}`` when this
    machine has no usable session. Variables already set are trusted and kept: an
    explicitly configured ``DISPLAY`` is somebody's decision, not a guess.

    Both display protocols are advertised when both are present. Wayland is not
    preferred, despite being the native one on a Wayland desktop: f3d's VTK backend
    here is X11-only and exits immediately when handed ``WAYLAND_DISPLAY`` alone.
    A viewer that does speak Wayland will use it; one that does not falls back to
    XWayland, which is why ``DISPLAY`` must carry a working cookie with it.
    """
    env: dict[str, str] = {}
    runtime = _runtime_dir()
    if runtime.is_dir():
        env["XDG_RUNTIME_DIR"] = str(runtime)

    wayland = os.environ.get("WAYLAND_DISPLAY")
    if wayland and (runtime / wayland).exists():
        env["WAYLAND_DISPLAY"] = wayland
    elif runtime.is_dir():
        # `wayland-0`, but not the `wayland-0.lock` beside it.
        sockets = sorted(
            p for p in runtime.glob("wayland-*") if p.suffix != ".lock"
        )
        if sockets:
            env["WAYLAND_DISPLAY"] = sockets[0].name

    display = os.environ.get("DISPLAY")
    if not display and _X11_SOCKET_DIR.is_dir():
        for socket in sorted(_X11_SOCKET_DIR.glob("X[0-9]*")):
            # Another user's socket is visible but unusable; skip rather than
            # hand the child a DISPLAY that will fail to connect.
            if os.access(socket, os.W_OK):
                display = f":{socket.name[1:]}"
                break

    if display:
        # A DISPLAY without a cookie is worse than no DISPLAY: the viewer starts,
        # is refused by the X server, and exits 0 without a window.
        cookie = _find_xauthority(runtime)
        if cookie:
            env["DISPLAY"] = display
            env["XAUTHORITY"] = cookie

    if "WAYLAND_DISPLAY" not in env and "DISPLAY" not in env:
        return {}
    return env


def display_available() -> bool:
    """True if something on this machine could plausibly show a window."""
    return bool(discover_display())


# --------------------------------------------------------------------------- #
# Window ownership
# --------------------------------------------------------------------------- #


def _state_path() -> Path:
    """Where the pid each gate owns is remembered.

    The runtime directory is the honest home for this: per-user, per-boot, and
    cleared at logout — exactly the lifetime of "the window I opened for you".
    Putting it in ``output/`` would outlive the process it names and turn a stale
    pid into a file the user has to reason about.
    """
    runtime = _runtime_dir()
    if runtime.is_dir() and os.access(runtime, os.W_OK):
        return runtime / "evee-viewer.json"
    return Path(tempfile.gettempdir()) / f"evee-viewer-{os.getuid()}.json"


def _read_state() -> dict:
    """Remembered windows, or ``{}``. A corrupt file is treated as no memory."""
    try:
        data = json.loads(_state_path().read_text())
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(state: dict) -> None:
    """Remember the windows. Best-effort: tracking must never fail a design."""
    try:
        _state_path().write_text(json.dumps(state))
    except OSError:
        pass


def _cmdline(pid: int) -> list[str] | None:
    """A live process's argv, or ``None`` if it is gone.

    Zombies read back as empty and count as gone, which is the safe direction: we
    decline to signal rather than signal something we cannot identify.
    """
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    if not raw:
        return None
    return raw.decode(errors="replace").rstrip("\0").split("\0")


def _reap(pid: int) -> None:
    """Clear the zombie a closed viewer leaves behind, if it was our child.

    A viewer this process launched stays in the table as a zombie until someone
    waits on it, and nothing else here ever does. One launched by an *earlier* run
    of the server is not our child at all and ``waitpid`` says so — expected, not
    an error, so both outcomes are silent.
    """
    try:
        os.waitpid(pid, os.WNOHANG)
    except OSError:
        pass


def close_gate(gate: str) -> str | None:
    """Close the window *this gate opened last*, and report what happened.

    Returns ``None`` when there was nothing of ours to close — no memory of a
    window, the process already exited, or the pid now belongs to someone else.
    That last case is the point of comparing argv rather than trusting the pid:
    closing a stranger's PrusaSlicer because it inherited a number would be a far
    worse bug than the stale window this exists to prevent.

    SIGTERM first, and SIGKILL only if it will not go. Nothing here is unsaved
    work — the window is a review of files already on disk.
    """
    state = _read_state()
    entry = state.get(gate)
    if not isinstance(entry, dict):
        return None

    pid, recorded = entry.get("pid"), entry.get("cmdline")
    if not isinstance(pid, int) or not isinstance(recorded, list):
        return None
    if _cmdline(pid) != recorded:
        return None

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return f"could not close the previous window (pid {pid}): {exc}"

    for _ in range(_CLOSE_POLL_COUNT):
        time.sleep(_CLOSE_POLL_SECONDS)
        if _cmdline(pid) != recorded:
            _reap(pid)
            return f"Replaced the previous window (pid {pid})."

    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass
    _reap(pid)
    return f"Force-closed the previous window (pid {pid})."


def _remember(gate: str, pid: int, argv: list[str]) -> None:
    """Record the window this gate now owns, leaving the other gate's alone."""
    state = _read_state()
    state[gate] = {"pid": pid, "cmdline": argv}
    _write_state(state)


def _launch(
    argv: list[str],
    files: list[str],
    auto_open: bool,
    force: bool,
    config_key: str,
    gate: str,
    auto_open_key: str = "[viewer].auto_open",
) -> ViewerLaunch:
    """Start a GUI child on some files, or explain why not. Never raises.

    Shared by both gates: the display discovery, the detached session, the DEVNULL
    streams and the one-window-per-gate bookkeeping are properties of "launching a
    GUI from an MCP stdio server", not of any particular viewer.
    """
    if not argv:
        return ViewerLaunch(False, argv, f"{config_key} in defaults.toml is empty")
    if not files:
        return ViewerLaunch(False, argv, "no paths given")
    if not auto_open and not force:
        return ViewerLaunch(False, argv, f"{auto_open_key} is false in defaults.toml")
    session_env = discover_display()
    if not session_env:
        return ViewerLaunch(
            False,
            argv,
            "no display on the machine running this server (headless or remote); "
            "the file paths are still valid",
        )

    binary = shutil.which(argv[0])
    if binary is None:
        return ViewerLaunch(
            False,
            argv,
            f"viewer {argv[0]!r} is not on PATH — install it, or change "
            f"{config_key} in config/defaults.toml",
        )

    # Only now that the launch is going ahead: closing the old window first and
    # then failing to open a new one would leave the human with nothing at all.
    replaced = close_gate(gate)

    full = [binary, *argv[1:], *files]
    try:
        proc = subprocess.Popen(
            full,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            # Our own environment plus the session variables the MCP client stripped.
            env={**os.environ, **session_env},
            # Its own session: the viewer outlives this tool call, and a Ctrl-C in
            # the client's terminal should not take the window down with it.
            start_new_session=True,
        )
    except OSError as exc:
        return ViewerLaunch(False, argv, f"could not start {argv[0]!r}: {exc}")

    _remember(gate, proc.pid, full)
    return ViewerLaunch(True, argv, None, proc.pid, replaced)


def open_model(
    paths: list[Path] | list[str],
    command: list[str] | None = None,
    force: bool = False,
) -> ViewerLaunch:
    """Open exported geometry in the configured model viewer — Gate 1.

    Args:
        paths: Model files to show — normally the single review 3MF written by
            :func:`evee.cad.export_review_model`, which already holds every part
            laid out side by side. Passing the raw STLs instead works, but they
            are each centred on the origin in print pose and will occupy the same
            space; arranging them is the review file's whole job.
        command: Viewer argv. Defaults to ``[viewer].command`` in defaults.toml.
        force: Launch even when ``auto_open`` is false. For an explicit "show me
            that again" request, where the config default should not veto the user.

    Returns:
        A :class:`ViewerLaunch` describing what happened. Never raises.

    Any window this gate opened previously is closed first, so what is on screen
    is always the current design and only the current design.
    """
    configured, auto_open = viewer_settings()
    argv = list(command) if command else configured
    files = [str(Path(p).resolve()) for p in paths]
    return _launch(argv, files, auto_open, force, "[viewer].command", DESIGN_GATE)


def open_gcode(
    path: Path | str,
    command: list[str] | None = None,
    force: bool = False,
) -> ViewerLaunch:
    """Open sliced G-code in the configured toolpath viewer — Gate 2.

    Off unless ``gcode_auto_open`` is set: slicing is headless, and the layer
    count, gram weight and time estimate that ``slice_part`` returns are what say
    whether the slice is sane. This is only the optional look at toolpaths.

    Same contract as :func:`open_model` otherwise — the G-code is already written
    and correct by the time this runs, so a viewer that will not open is reported,
    never raised, and this gate replaces its own window rather than stacking them.
    """
    configured, auto_open, auto_open_key = gcode_viewer_settings()
    argv = list(command) if command else configured
    return _launch(
        argv,
        [str(Path(path).resolve())],
        auto_open,
        force,
        "[viewer].gcode_command",
        GCODE_GATE,
        auto_open_key,
    )
