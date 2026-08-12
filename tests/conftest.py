"""Shared test setup.

Two side effects have to be contained for the suite to be safe to run: opening
windows, and touching the printer.

``design_part`` opens a 3D viewer on success, which is right in use and wrong in a
test run — nobody wants a dozen f3d windows during ``pytest``. The stub below is
applied to the name ``server.py`` imported, so ``tests/test_viewer.py`` still
exercises the real launch logic.

The printer is the serious one. A real ``.env`` sits at the repo root pointing at a
real machine on the LAN, so a test that constructs an ``OctoPrintClient`` without
saying otherwise would talk to it. Credentials are therefore replaced suite-wide with
a host that cannot resolve, and tests that want printer behaviour inject an
``httpx2.MockTransport`` explicitly.
"""

from __future__ import annotations

import pytest

from vtp.viewer import ViewerLaunch


@pytest.fixture(autouse=True)
def no_viewer_windows(monkeypatch):
    """Stop the MCP tools from opening real windows during tests."""
    suppressed = ViewerLaunch(False, [], "suppressed during tests")
    monkeypatch.setattr("vtp.server.open_model", lambda paths, **kwargs: suppressed)
    monkeypatch.setattr("vtp.server.open_gcode", lambda path, **kwargs: suppressed)


@pytest.fixture(autouse=True)
def isolated_viewer_state(tmp_path_factory, monkeypatch):
    """Keep window bookkeeping out of the developer's real runtime directory.

    ``_remember`` writes wherever ``_state_path`` points, and a test that fakes
    ``Popen`` still reaches it — so without this, running the suite leaves a fake
    pid in the file a live session uses to decide what to close.

    Returns the scratch path, so a test that wants to inspect or seed the
    bookkeeping asks for this fixture rather than patching it a second time.
    """
    path = tmp_path_factory.mktemp("viewer-state") / "vtp-viewer.json"
    monkeypatch.setattr("vtp.viewer._state_path", lambda: path)
    return path


@pytest.fixture(autouse=True)
def offline_printer_credentials(monkeypatch):
    """Point every OctoPrintClient at a host that cannot resolve.

    Not merely hygiene: without this, a test that forgets to inject a transport
    reaches the real Ender on the LAN using the real key in ``.env``. Reads would be
    harmless; a bug in a ``start_print`` test would not be. ``.invalid`` is reserved
    by RFC 2606 and never resolves, so such a test fails loudly instead.

    Patched on ``vtp.printer``, not ``vtp.config``: the module imports the name at
    import time, so patching the definition site leaves the already-bound reference
    pointing at the real credentials.
    """
    monkeypatch.setattr(
        "vtp.printer.octoprint_settings",
        lambda: ("http://printer.invalid", "test-key"),
    )


@pytest.fixture(autouse=True)
def isolated_audit_log(tmp_path_factory, monkeypatch):
    """Keep test prints out of the real output/print_log.jsonl.

    That file is a record of what was sent to a physical machine. Fake starts from a
    test run must not appear in it.
    """
    path = tmp_path_factory.mktemp("audit") / "print_log.jsonl"
    monkeypatch.setattr("vtp.printer.AUDIT_LOG", path)
    return path


@pytest.fixture(autouse=True)
def isolated_mesh_state(tmp_path_factory, monkeypatch):
    """Keep test runs from claiming the real printer has a stored bed mesh.

    Not hygiene, safety. ``slice_stl`` reads this file to decide whether to swap the
    profile's ``G29`` for ``M420 S1``. A test that records a mesh would leave
    ``output/bed_mesh.json`` behind, and the next *real* slice would emit "load the
    stored mesh" for a mesh the printer never stored — which Marlin does not refuse.
    It prints on a flat plane.

    Returns the scratch path so a test can seed or inspect it.
    """
    path = tmp_path_factory.mktemp("mesh") / "bed_mesh.json"
    monkeypatch.setattr("vtp.calibration.MESH_STATE", path)
    return path


@pytest.fixture(autouse=True)
def no_post_command_sleep(monkeypatch):
    """Drop the settle delay after start/cancel. It exists for the human, not us."""
    monkeypatch.setattr("vtp.printer.time.sleep", lambda _seconds: None)
