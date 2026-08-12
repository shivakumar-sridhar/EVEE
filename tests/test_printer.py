"""Printer client tests — mostly about what does NOT happen.

Every guard in :mod:`vtp.printer` is asserted twice: that it raises, and that the
request which would have started a print was never sent. The second half is the part
that matters. A guard that raises after issuing the command is not a guard, and a
test that only checks the exception cannot tell the difference.

No test here touches a real printer. ``conftest`` points credentials at a host that
cannot resolve, and each test supplies an ``httpx2.MockTransport`` that records what
it was asked to do.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from vtp.config import _dotenv, bed_extents
from vtp.printer import (
    JobStatus,
    OctoPrintClient,
    PrinterError,
    PrinterStatus,
    PrinterUnreachable,
    PrintRefused,
    _park_commands,
)

BASE = "http://printer.invalid"

OPERATIONAL = {
    "state": {
        "text": "Operational",
        "error": "",
        "flags": {
            "operational": True,
            "printing": False,
            "paused": False,
            "error": False,
            "closedOrError": False,
            "ready": True,
        },
    },
    "temperature": {
        "tool0": {"actual": 23.81, "target": 0.0, "offset": 0},
        "bed": {"actual": 23.42, "target": 0.0, "offset": 0},
    },
}

PRINTING = {
    "state": {
        "text": "Printing",
        "error": "",
        "flags": {
            "operational": True,
            "printing": True,
            "paused": False,
            "error": False,
            "closedOrError": False,
            "ready": False,
        },
    },
    "temperature": {
        "tool0": {"actual": 205.0, "target": 205.0},
        "bed": {"actual": 60.0, "target": 60.0},
    },
}


def job_payload(name="part.gcode", state="Operational", completion=0.0, left=None):
    return {
        "job": {"file": {"name": name, "origin": "local", "path": name}},
        "progress": {
            "completion": completion,
            "printTime": 0,
            "printTimeLeft": left,
        },
        "state": state,
    }


class Recorder:
    """A MockTransport handler that logs calls and replies from a routing table.

    Routes are matched on ``"METHOD /path"``. A missing route is a test bug, so it
    fails the test rather than quietly 404ing — an unexpected request is exactly the
    thing these tests exist to catch.
    """

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[bytes] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        key = f"{request.method} {request.url.path}"
        self.calls.append((request.method, request.url.path))
        self.bodies.append(request.read())
        if key not in self.routes:
            raise AssertionError(f"unexpected request: {key}")
        reply = self.routes[key]
        if callable(reply):
            reply = reply(request)
        status, payload = reply
        if payload is None:
            return httpx2.Response(status)
        return httpx2.Response(status, json=payload)

    @property
    def paths(self) -> list[str]:
        return [path for _method, path in self.calls]

    def sent(self, method: str, path: str) -> bool:
        return (method, path) in self.calls

    def body_for(self, method: str, path: str) -> dict:
        index = self.calls.index((method, path))
        return json.loads(self.bodies[index])


def client(routes, **kwargs) -> tuple[OctoPrintClient, Recorder]:
    recorder = Recorder(routes)
    return (
        OctoPrintClient(
            BASE, "test-key", transport=httpx2.MockTransport(recorder), **kwargs
        ),
        recorder,
    )


# --- credentials ------------------------------------------------------------


def test_missing_credentials_name_the_file_not_the_shell(monkeypatch):
    """The remedy is editing .env, so the message has to say .env.

    A user who exports the key in their shell and gets "not configured" needs to be
    told why that did not work — the MCP client scrubs the environment.
    """
    monkeypatch.setattr("vtp.printer.octoprint_settings", lambda: (None, None))
    with pytest.raises(PrinterUnreachable) as exc:
        OctoPrintClient()
    message = str(exc.value)
    assert "OCTOPRINT_URL" in message and "OCTOPRINT_API_KEY" in message
    assert ".env" in message
    assert "scrubbed environment" in message


def test_missing_key_alone_is_named_alone(monkeypatch):
    monkeypatch.setattr("vtp.printer.octoprint_settings", lambda: (BASE, None))
    with pytest.raises(PrinterUnreachable) as exc:
        OctoPrintClient()
    assert "OCTOPRINT_API_KEY" in str(exc.value)
    assert "OCTOPRINT_URL" not in str(exc.value)


def test_api_key_is_sent_as_a_header():
    printer, recorder = client({"GET /api/printer": (200, OPERATIONAL)})
    captured = {}

    def handler(request):
        captured["key"] = request.headers.get("X-Api-Key")
        return httpx2.Response(200, json=OPERATIONAL)

    printer._client = httpx2.Client(
        base_url=BASE, transport=httpx2.MockTransport(handler)
    )
    printer.get_status()
    assert captured["key"] == "test-key"


def test_rejected_key_does_not_echo_the_key():
    """A 403 message ends up in logs and agent transcripts. The key must not."""
    printer, _ = client({"GET /api/printer": (403, {"error": "invalid key"})})
    with pytest.raises(PrinterUnreachable) as exc:
        printer.get_status()
    assert "test-key" not in str(exc.value)
    assert ".env" in str(exc.value)


def test_dotenv_parsing(tmp_path):
    """Quotes stripped, `export` tolerated, comments skipped, '#' kept in values."""
    env = tmp_path / ".env"
    env.write_text(
        "\n".join(
            [
                "# a comment",
                "",
                "OCTOPRINT_URL=http://octopi.local",
                'export OCTOPRINT_API_KEY="ab#cd"',
                "QUOTED='single'",
                "NOT_AN_ASSIGNMENT",
            ]
        ),
        encoding="utf-8",
    )
    _dotenv.cache_clear()
    try:
        values = _dotenv(env)
    finally:
        _dotenv.cache_clear()

    assert values["OCTOPRINT_URL"] == "http://octopi.local"
    # An API key may legitimately contain '#'. Treating it as an inline comment
    # would corrupt the secret and surface later as a baffling 403.
    assert values["OCTOPRINT_API_KEY"] == "ab#cd"
    assert values["QUOTED"] == "single"
    assert "NOT_AN_ASSIGNMENT" not in values


# --- reads ------------------------------------------------------------------


def test_get_status_maps_the_response():
    printer, _ = client({"GET /api/printer": (200, OPERATIONAL)})
    status = printer.get_status()
    assert isinstance(status, PrinterStatus)
    assert status.state == "Operational"
    assert status.ready_to_print
    assert status.blocked_reason() is None
    assert status.tool_actual == pytest.approx(23.81)
    assert status.bed_actual == pytest.approx(23.42)


def test_get_status_falls_back_when_disconnected():
    """/api/printer answers 409 with no printer attached; that is a state, not a fault."""
    printer, recorder = client(
        {
            "GET /api/printer": (409, {"error": "Printer is not operational"}),
            "GET /api/connection": (200, {"current": {"state": "Closed"}}),
        }
    )
    status = printer.get_status()
    assert status.state == "Closed"
    assert not status.operational
    assert "not connected" in status.blocked_reason()
    assert recorder.sent("GET", "/api/connection")


def test_transient_flags_count_as_printing():
    """Cancelling and finishing are not moments to slip a new job in."""
    payload = {
        "state": {
            "text": "Cancelling",
            "error": "",
            "flags": {"operational": True, "cancelling": True},
        },
        "temperature": {},
    }
    printer, _ = client({"GET /api/printer": (200, payload)})
    status = printer.get_status()
    assert status.printing
    assert not status.ready_to_print


def test_null_temperatures_do_not_crash():
    payload = {
        "state": {"text": "Operational", "flags": {"operational": True}},
        "temperature": {"tool0": {"actual": None, "target": None}},
    }
    printer, _ = client({"GET /api/printer": (200, payload)})
    status = printer.get_status()
    assert status.tool_actual is None
    assert "?" in status.summary()


def test_get_job_maps_progress():
    printer, _ = client(
        {"GET /api/job": (200, job_payload("case.gcode", "Printing", 42.5, 1844))}
    )
    job = printer.get_job()
    assert isinstance(job, JobStatus)
    assert job.file_name == "case.gcode"
    assert job.completion == pytest.approx(42.5)
    assert job.print_time_left_seconds == 1844
    assert "30m" in job.summary()


def test_non_json_answer_is_diagnosed():
    """Pointing OCTOPRINT_URL at the wrong box should say so, not raise a JSON error."""

    def handler(request):
        return httpx2.Response(200, text="<html>router login</html>")

    printer = OctoPrintClient(
        BASE, "test-key", transport=httpx2.MockTransport(handler)
    )
    with pytest.raises(PrinterUnreachable) as exc:
        printer.get_status()
    assert "OctoPrint" in str(exc.value)


def test_unreachable_host_names_the_url():
    def handler(request):
        raise httpx2.ConnectError("connection refused")

    printer = OctoPrintClient(
        BASE, "test-key", transport=httpx2.MockTransport(handler)
    )
    with pytest.raises(PrinterUnreachable) as exc:
        printer.get_status()
    assert BASE in str(exc.value)


# --- upload -----------------------------------------------------------------


def gcode_file(tmp_path, name="part.gcode", text=";LAYER_CHANGE\nG1 X1\n"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def test_upload_sends_print_false():
    """The one flag that must never be true. print=true would erase Gate 3."""
    recorder = Recorder(
        {
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "part.gcode"}}},
            )
        }
    )
    printer = OctoPrintClient(
        BASE, "test-key", transport=httpx2.MockTransport(recorder)
    )
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "part.gcode"
        path.write_text(";LAYER_CHANGE\n", encoding="utf-8")
        printer.upload_gcode(path)

    body = recorder.bodies[0].decode("utf-8", "replace")
    assert 'name="print"' in body
    assert "true" not in body.split('name="print"')[1][:40].lower()
    assert 'name="select"' in body


def test_upload_returns_the_name_the_printer_chose(tmp_path):
    """OctoPrint sanitises filenames. start_print needs the stored one, not ours."""
    printer, _ = client(
        {
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "my_part.gcode"}}},
            )
        }
    )
    result = printer.upload_gcode(gcode_file(tmp_path, "my part.gcode"))
    assert result.remote_name == "my_part.gcode"
    assert "Not printing" in result.summary()


def test_upload_rejects_a_missing_file(tmp_path):
    printer, recorder = client({})
    with pytest.raises(PrinterError, match="no such G-code"):
        printer.upload_gcode(tmp_path / "nope.gcode")
    assert recorder.calls == []


def test_upload_rejects_a_non_gcode_file(tmp_path):
    printer, recorder = client({})
    stl = tmp_path / "part.stl"
    stl.write_text("solid\n", encoding="utf-8")
    with pytest.raises(PrinterError, match="not a G-code file"):
        printer.upload_gcode(stl)
    assert recorder.calls == []


def test_upload_rejects_an_empty_file(tmp_path):
    printer, recorder = client({})
    empty = tmp_path / "part.gcode"
    empty.write_text("", encoding="utf-8")
    with pytest.raises(PrinterError, match="empty"):
        printer.upload_gcode(empty)
    assert recorder.calls == []


# --- start_print: the guards ------------------------------------------------


def test_false_confirmation_sends_nothing():
    printer, recorder = client({})
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("part.gcode", bed_confirmed_clear=False)
    assert "bed_confirmed_clear was False" in str(exc.value)
    # The guard is first for a reason: nothing at all reached the network.
    assert recorder.calls == []


@pytest.mark.parametrize("value", ["yes", 1, "true", [1], {"ok": True}])
def test_truthy_is_not_confirmation(value):
    """`is not True`, not a truthiness test.

    A model that passes the string "yes" has produced something truthy without
    anyone having looked at a build plate. Truthiness would let that through.
    """
    printer, recorder = client({})
    with pytest.raises(PrintRefused):
        printer.start_print("part.gcode", bed_confirmed_clear=value)
    assert recorder.calls == []


def test_confirmation_is_keyword_only():
    """So it cannot arrive by argument position, or be defaulted in by a caller."""
    printer, _ = client({})
    with pytest.raises(TypeError):
        printer.start_print("part.gcode", True)


@pytest.mark.parametrize(
    "bad", ["/home/shiv/EVI/output/part.gcode", "sub/part.gcode", "..\\part.gcode", ""]
)
def test_a_path_is_not_a_filename(bad):
    printer, recorder = client({})
    with pytest.raises(PrintRefused):
        printer.start_print(bad, bed_confirmed_clear=True)
    assert recorder.calls == []


def test_path_refusal_suggests_the_basename():
    printer, _ = client({})
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("/tmp/output/case.gcode", bed_confirmed_clear=True)
    assert "'case.gcode'" in str(exc.value)


def test_missing_file_on_printer_is_refused():
    printer, recorder = client({"GET /api/files/local/part.gcode": (404, {})})
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("part.gcode", bed_confirmed_clear=True)
    assert "not on the printer" in str(exc.value)
    assert not recorder.sent("POST", "/api/job")


def test_busy_printer_is_refused():
    printer, recorder = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (200, PRINTING),
        }
    )
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("part.gcode", bed_confirmed_clear=True)
    assert "already running" in str(exc.value)
    assert not recorder.sent("POST", "/api/job")


def test_disconnected_printer_is_refused():
    printer, recorder = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (409, {}),
            "GET /api/connection": (200, {"current": {"state": "Closed"}}),
        }
    )
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("part.gcode", bed_confirmed_clear=True)
    assert "not connected" in str(exc.value)
    assert not recorder.sent("POST", "/api/job")


def test_selection_mismatch_refuses_without_starting():
    """The guard that stops us printing whatever the web UI had loaded.

    OctoPrint's start command names no file. If the select did not take, starting
    anyway would print the other file — so the selection is read back first.
    """
    printer, recorder = client(
        {
            "GET /api/files/local/wanted.gcode": (200, {"name": "wanted.gcode"}),
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local/wanted.gcode": (204, None),
            # The printer still reports something else loaded.
            "GET /api/job": (200, job_payload("someone_elses.gcode")),
        }
    )
    with pytest.raises(PrintRefused) as exc:
        printer.start_print("wanted.gcode", bed_confirmed_clear=True)
    message = str(exc.value)
    assert "someone_elses.gcode" in message
    assert "Nothing was started" in message
    assert not recorder.sent("POST", "/api/job")


def test_happy_path_selects_then_starts():
    printer, recorder = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode", "Printing", 0.0, 1844)),
            "POST /api/job": (204, None),
        }
    )
    job = printer.start_print("part.gcode", bed_confirmed_clear=True)

    # Order is the assertion: select must precede start, and the state check must
    # precede both.
    assert recorder.paths.index("/api/printer") < recorder.paths.index(
        "/api/files/local/part.gcode", 1
    )
    assert recorder.calls.index(("POST", "/api/files/local/part.gcode")) < recorder.calls.index(
        ("POST", "/api/job")
    )
    assert recorder.body_for("POST", "/api/job") == {"command": "start"}
    assert job.file_name == "part.gcode"


def test_select_never_asks_to_print():
    """`print: false` in the select command, for the same reason as the upload."""
    printer, recorder = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode")),
            "POST /api/job": (204, None),
        }
    )
    printer.start_print("part.gcode", bed_confirmed_clear=True)
    body = recorder.body_for("POST", "/api/files/local/part.gcode")
    assert body == {"command": "select", "print": False}


def test_start_is_recorded_in_the_audit_log(isolated_audit_log):
    printer, _ = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode")),
            "POST /api/job": (204, None),
        }
    )
    printer.start_print("part.gcode", bed_confirmed_clear=True)

    record = json.loads(isolated_audit_log.read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "start_print"
    assert record["file"] == "part.gcode"
    assert record["bed_confirmed_clear"] is True
    assert record["at"]


def test_refusals_are_not_recorded(isolated_audit_log):
    """The log is what was started, not what was asked for and declined."""
    printer, _ = client({})
    with pytest.raises(PrintRefused):
        printer.start_print("part.gcode", bed_confirmed_clear=False)
    assert not isolated_audit_log.exists()


def test_audit_failure_does_not_stop_a_print(monkeypatch, tmp_path):
    """The record is best effort. An unwritable log must not block an approved start."""
    monkeypatch.setattr("vtp.printer.AUDIT_LOG", tmp_path / "nope" / "x.jsonl")
    monkeypatch.setattr(
        "pathlib.Path.mkdir",
        lambda *a, **k: (_ for _ in ()).throw(OSError("read-only")),
    )
    printer, recorder = client(
        {
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode")),
            "POST /api/job": (204, None),
        }
    )
    printer.start_print("part.gcode", bed_confirmed_clear=True)
    assert recorder.sent("POST", "/api/job")


# --- cancel -----------------------------------------------------------------


def _cancel_routes(states=None, park=(204, None)):
    """Routes for a cancel, with the status sequence the park loop will see.

    ``states`` is consumed one entry per ``GET /api/printer``; the last one repeats,
    which is what lets a test say "printing forever" without listing it a hundred times.
    """
    states = list(states or [PRINTING, OPERATIONAL])
    seen = {"n": 0}

    def status(_request):
        index = min(seen["n"], len(states) - 1)
        seen["n"] += 1
        return (200, states[index])

    return {
        "GET /api/printer": status,
        "POST /api/job": (204, None),
        "GET /api/job": (200, job_payload("part.gcode", "Cancelling")),
        "POST /api/printer/command": park,
    }


def test_cancel_when_idle_is_an_error():
    printer, recorder = client({"GET /api/printer": (200, OPERATIONAL)})
    with pytest.raises(PrinterError, match="nothing to cancel"):
        printer.cancel_print(confirmed=True)
    assert not recorder.sent("POST", "/api/job")


def test_cancel_while_printing():
    printer, recorder = client(_cancel_routes())
    result = printer.cancel_print(confirmed=True)
    assert recorder.body_for("POST", "/api/job") == {"command": "cancel"}
    assert result.parked is True


@pytest.mark.parametrize("value", ["yes", 1, "true", [1], object()])
def test_cancel_rejects_anything_but_true(value):
    """A truthy value is evidence something was passed, not that a human decided."""
    printer, recorder = client({})
    with pytest.raises(PrintRefused, match="not True"):
        printer.cancel_print(confirmed=value)
    assert recorder.calls == []


def test_cancel_confirmation_is_keyword_only():
    """So it cannot arrive by argument position from a refactor."""
    printer, _ = client({})
    with pytest.raises(TypeError):
        printer.cancel_print(True)


def test_cancel_parks_the_head_after_the_cancel():
    """The point of the tool over the machine's own button: a reachable plate."""
    printer, recorder = client(_cancel_routes())
    printer.cancel_print(confirmed=True)

    assert recorder.body_for("POST", "/api/printer/command") == {
        "commands": list(_park_commands())
    }
    assert recorder.paths.index("/api/job") < recorder.paths.index(
        "/api/printer/command"
    ), "the park must not go out before the cancel"


def test_the_park_position_is_read_from_the_profile():
    """Never a literal 187: the bed is stated in the profile and read from it."""
    commands = _park_commands()
    expected = f"Y{bed_extents()[1] * 0.85:g}"
    assert any(expected in line for line in commands)
    assert "M84 X Y E" in commands, "releasing Z would drop the gantry"


def test_a_printer_that_never_goes_idle_is_reported_not_raised():
    """OctoPrint refuses commands while cancelling. Waiting forever is not an option.

    The cancel already succeeded by this point, so this must not read as a failure —
    it has to say the print is stopped and the head is not parked.
    """
    printer, recorder = client(_cancel_routes(states=[PRINTING]))
    result = printer.cancel_print(confirmed=True)

    assert result.parked is False
    assert "still" in result.park_detail
    assert not recorder.sent("POST", "/api/printer/command")


def test_a_refused_park_still_returns_a_successful_cancel():
    printer, _ = client(_cancel_routes(park=(409, None)))
    result = printer.cancel_print(confirmed=True)
    assert result.parked is False
    assert "refused" in result.park_detail


def test_cancel_is_recorded_with_the_file_it_stopped(isolated_audit_log):
    printer, _ = client(_cancel_routes())
    printer.cancel_print(confirmed=True)

    events = [
        json.loads(line) for line in isolated_audit_log.read_text().splitlines()
    ]
    cancel = next(e for e in events if e["event"] == "cancel_print")
    assert cancel["confirmed"] is True
    assert cancel["file"] == "part.gcode"


def test_no_public_method_accepts_a_gcode_command():
    """The Python enforcement of "narrow, not a passthrough".

    The park is a module constant precisely so a client model cannot reach the
    machine directly. A comment saying so is not a mechanism; this is.
    """
    import inspect

    forbidden = {"gcode", "command", "commands", "script", "lines"}
    for name, member in inspect.getmembers(OctoPrintClient, inspect.isfunction):
        if name.startswith("_"):
            continue
        params = set(inspect.signature(member).parameters)
        assert not (params & forbidden), f"{name} exposes a command parameter"


# --- upload_and_print: the combined path ------------------------------------
#
# Folding the upload into the start was an explicit decision (2026-08-12). These
# assert that folding them cost nothing but the intermediate human look: every
# guard still fires, and the ones that can fire before the upload do.


def test_combined_refuses_before_uploading_anything():
    """The bed check is first, so a refusal costs no transfer and leaves no file."""
    printer, recorder = client({})
    with pytest.raises(PrintRefused) as exc:
        printer.upload_and_print("/tmp/whatever.gcode", bed_confirmed_clear=False)
    assert "Nothing was uploaded" in str(exc.value)
    assert recorder.calls == []


@pytest.mark.parametrize("value", ["yes", 1, "true"])
def test_combined_rejects_truthy_confirmations(value):
    printer, recorder = client({})
    with pytest.raises(PrintRefused):
        printer.upload_and_print("/tmp/whatever.gcode", bed_confirmed_clear=value)
    assert recorder.calls == []


def test_combined_confirmation_is_keyword_only():
    printer, _ = client({})
    with pytest.raises(TypeError):
        printer.upload_and_print("/tmp/whatever.gcode", True)


def test_busy_printer_is_checked_before_the_upload(tmp_path):
    """The reason the state check is duplicated outside start_print.

    Uploading half a megabyte to a Pi and only then discovering it is mid-print
    leaves a file the human did not ask for on the SD card. One cheap GET first.
    """
    printer, recorder = client({"GET /api/printer": (200, PRINTING)})
    with pytest.raises(PrintRefused) as exc:
        printer.upload_and_print(
            gcode_file(tmp_path), bed_confirmed_clear=True
        )
    assert "already running" in str(exc.value)
    assert "Nothing was uploaded" in str(exc.value)
    assert not recorder.sent("POST", "/api/files/local")


def test_combined_happy_path_uploads_then_selects_then_starts(tmp_path):
    printer, recorder = client(
        {
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "part.gcode"}}},
            ),
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode", "Printing", 0.0, 1844)),
            "POST /api/job": (204, None),
        }
    )
    upload, job = printer.upload_and_print(
        gcode_file(tmp_path), bed_confirmed_clear=True
    )

    order = recorder.calls
    assert order.index(("POST", "/api/files/local")) < order.index(
        ("POST", "/api/files/local/part.gcode")
    )
    assert order.index(("POST", "/api/files/local/part.gcode")) < order.index(
        ("POST", "/api/job")
    )
    # The upload still says print=false; the start is still its own request.
    body = recorder.bodies[order.index(("POST", "/api/files/local"))]
    assert b'name="print"' in body
    assert recorder.body_for("POST", "/api/job") == {"command": "start"}
    assert upload.remote_name == "part.gcode"
    assert job.state == "Printing"


def test_combined_still_verifies_the_selected_file(tmp_path):
    """Folding upload in did not weaken the selection guard."""
    printer, recorder = client(
        {
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "part.gcode"}}},
            ),
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("someone_elses.gcode")),
        }
    )
    with pytest.raises(PrintRefused) as exc:
        printer.upload_and_print(gcode_file(tmp_path), bed_confirmed_clear=True)
    assert "someone_elses.gcode" in str(exc.value)
    assert not recorder.sent("POST", "/api/job")


def test_combined_uses_the_name_the_printer_chose(tmp_path):
    """A sanitised name must be what gets selected and started, not our stem."""
    printer, recorder = client(
        {
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "my_part.gcode"}}},
            ),
            "GET /api/files/local/my_part.gcode": (200, {"name": "my_part.gcode"}),
            "POST /api/files/local/my_part.gcode": (204, None),
            "GET /api/job": (200, job_payload("my_part.gcode", "Printing")),
            "POST /api/job": (204, None),
        }
    )
    printer.upload_and_print(
        gcode_file(tmp_path, "my part.gcode"), bed_confirmed_clear=True
    )
    assert recorder.sent("POST", "/api/files/local/my_part.gcode")


def test_combined_start_is_audited(tmp_path, isolated_audit_log):
    printer, _ = client(
        {
            "GET /api/printer": (200, OPERATIONAL),
            "POST /api/files/local": (
                201,
                {"done": True, "files": {"local": {"name": "part.gcode"}}},
            ),
            "GET /api/files/local/part.gcode": (200, {"name": "part.gcode"}),
            "POST /api/files/local/part.gcode": (204, None),
            "GET /api/job": (200, job_payload("part.gcode", "Printing")),
            "POST /api/job": (204, None),
        }
    )
    printer.upload_and_print(gcode_file(tmp_path), bed_confirmed_clear=True)
    record = json.loads(isolated_audit_log.read_text(encoding="utf-8").splitlines()[0])
    assert record["event"] == "start_print"
    assert record["file"] == "part.gcode"


# --- storing a bed mesh -----------------------------------------------------
#
# The interesting property is the acknowledgement. OctoPrint answers 204 the moment
# it queues the commands and has no "the probe finished" flag, so a mesh recorded
# without confirmation would be a claim every later slice trusts.


def _mesh_routes(targets):
    """Routes for a probe, with the nozzle targets the ack loop will read back."""
    seq = list(targets)
    seen = {"n": 0}

    def status(_request):
        index = min(seen["n"], len(seq) - 1)
        seen["n"] += 1
        payload = json.loads(json.dumps(OPERATIONAL))
        payload["temperature"]["tool0"]["target"] = seq[index]
        return (200, payload)

    return {"GET /api/printer": status, "POST /api/printer/command": (204, None)}


def test_storing_a_mesh_refuses_without_a_bed_confirmation():
    printer, recorder = client({})
    with pytest.raises(PrintRefused, match="not True"):
        printer.store_bed_mesh(bed_confirmed_clear="yes")
    assert recorder.calls == []


def test_storing_a_mesh_refuses_while_printing():
    printer, recorder = client({"GET /api/printer": (200, PRINTING)})
    with pytest.raises(PrintRefused, match="already running"):
        printer.store_bed_mesh(bed_confirmed_clear=True)
    assert not recorder.sent("POST", "/api/printer/command")


def test_a_confirmed_probe_is_recorded(isolated_mesh_state):
    """The sentinel: the target only appears once Marlin is past G29."""
    from vtp.calibration import mesh_state

    printer, recorder = client(_mesh_routes([0.0, 0.0, 42.0]))
    result = printer.store_bed_mesh(bed_confirmed_clear=True)

    assert result.stored is True
    assert recorder.body_for("POST", "/api/printer/command") == {
        "commands": ["G28", "G29", "M500", "M104 S42"]
    }
    assert isolated_mesh_state.is_file()
    assert mesh_state().usable is True


def test_an_unconfirmed_probe_records_nothing(isolated_mesh_state):
    """A probe that errored never reaches the M104, so the wait must not be trusted.

    This is the failure that would matter: a recorded mesh the printer does not have
    makes every later slice emit M420 S1, which Marlin does not refuse — it prints on
    a flat plane and mentions it only on the serial console.
    """
    from vtp.calibration import mesh_state

    printer, _ = client(_mesh_routes([0.0]))
    result = printer.store_bed_mesh(bed_confirmed_clear=True)

    assert result.stored is False
    assert "never acknowledged" in result.detail
    assert not isolated_mesh_state.is_file()
    assert mesh_state().usable is False


def test_the_nozzle_is_cooled_whether_or_not_the_probe_confirmed():
    """The sentinel warms the block a little. Do not walk away leaving it on."""
    for targets in ([0.0, 42.0], [0.0]):
        printer, recorder = client(_mesh_routes(targets))
        printer.store_bed_mesh(bed_confirmed_clear=True)
        sent = [
            json.loads(body)["commands"]
            for (method, path), body in zip(recorder.calls, recorder.bodies)
            if (method, path) == ("POST", "/api/printer/command")
        ]
        assert sent[-1] == ["M104 S0"]
