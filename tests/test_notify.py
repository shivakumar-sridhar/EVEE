"""Notification daemon tests.

The transition logic is the whole risk here, and it is deliberately a pure function of
(previous, status, job) so it can be tested without a printer, a network or a clock.
The two failures that matter are opposite in kind: **missing a failed print**, which is
what the daemon exists to prevent, and **crying wolf**, which trains a human to swipe
the notification away — so a dropped packet must not be reported as a ruined print.

Nothing here reaches the network. ``conftest`` already points credentials at a host
that cannot resolve; the notifier is exercised against a stub transport.
"""

from __future__ import annotations

import json

import httpx2
import pytest

from vtp.notify import Notifier, PrintEvent, Watcher, _Seen, classify, fetch_snapshot
from vtp.printer import JobStatus, PrinterStatus, PrinterUnreachable


def status(
    *, printing=False, operational=True, error=False, state="Operational", error_text=""
) -> PrinterStatus:
    return PrinterStatus(
        state=state,
        operational=operational,
        printing=printing,
        paused=False,
        error=error,
        error_text=error_text,
        tool_actual=205.0 if printing else 24.0,
        tool_target=205.0 if printing else 0.0,
        bed_actual=60.0 if printing else 24.0,
        bed_target=60.0 if printing else 0.0,
    )


def job(name="plate.gcode", completion=50.0, elapsed=600, state="Printing") -> JobStatus:
    return JobStatus(
        state=state,
        file_name=name,
        completion=completion,
        print_time_seconds=elapsed,
        print_time_left_seconds=None,
    )


PRINTING_MIDWAY = _Seen(
    printing=True, operational=True, file_name="plate.gcode", completion=50.0, elapsed=600
)


# --------------------------------------------------------------------------- #
# The transition table
# --------------------------------------------------------------------------- #


def test_idle_stays_quiet():
    seen, event = classify(_Seen(), status(), job(name=None, completion=None, elapsed=None))
    assert event is None
    assert seen.printing is False


def test_a_print_beginning_is_announced():
    _seen, event = classify(_Seen(), status(printing=True), job(completion=0.2))
    assert event is not None
    assert event.kind == "started"
    assert "plate.gcode" in event.message


def test_printing_along_is_not_an_event_every_poll():
    """Otherwise a 54-minute print sends 650 notifications."""
    seen, event = classify(PRINTING_MIDWAY, status(printing=True), job(completion=51.0))
    assert event is None
    assert seen.completion == 51.0


def test_reaching_the_end_is_a_finish():
    seen = PRINTING_MIDWAY.__class__(**{**PRINTING_MIDWAY.__dict__, "completion": 100.0})
    _seen, event = classify(seen, status(), job(completion=100.0, state="Operational"))
    assert event is not None
    assert event.kind == "finished"
    assert "10m 0s" in event.message  # the elapsed we last saw, not a fresh reading


def test_stopping_short_is_a_cancel_not_a_finish():
    """Catches somebody pressing cancel on the machine, which nothing else sees."""
    _seen, event = classify(PRINTING_MIDWAY, status(), job(completion=50.0, state="Operational"))
    assert event is not None
    assert event.kind == "failed"
    assert event.reason == "cancelled"
    assert "50%" in event.message
    # The plate is not clear afterwards, and the message has to say so.
    assert "stuck to the plate" in event.message


def test_an_error_outranks_a_completion_check():
    _seen, event = classify(
        PRINTING_MIDWAY, status(error=True, error_text="MINTEMP"), job(completion=50.0)
    )
    assert event.reason == "error"
    assert "MINTEMP" in event.message
    assert event.priority == 5


def test_dropping_offline_mid_print_is_urgent():
    _seen, event = classify(
        PRINTING_MIDWAY, status(operational=False, state="Closed"), job(completion=50.0)
    )
    assert event.reason == "disconnected"
    assert event.priority == 5


def test_elapsed_survives_the_job_ending():
    """OctoPrint stops reporting printTime once a job ends. "unknown time" is a poor
    notification, so the last reading is carried forward."""
    seen, _event = classify(PRINTING_MIDWAY, status(), job(completion=100.0, elapsed=None))
    assert seen.elapsed == 600


# --------------------------------------------------------------------------- #
# Polling
# --------------------------------------------------------------------------- #


class FakeClient:
    base_url = "http://printer.invalid"

    def __init__(self, pairs):
        self.pairs = list(pairs)

    def get_status(self):
        self.current = self.pairs.pop(0) if len(self.pairs) > 1 else self.pairs[0]
        return self.current[0]

    def get_job(self):
        return self.current[1]


class RecordingNotifier(Notifier):
    def __init__(self):
        super().__init__("https://ntfy.invalid", "topic")
        self.sent = []

    def send(self, event, snapshot=None):
        self.sent.append((event, snapshot))
        return True


def test_a_finished_print_is_pushed_and_recorded(isolated_audit_log, monkeypatch):
    """The audit record is the half that outlives the notification."""
    monkeypatch.setattr("vtp.notify.fetch_snapshot", lambda *_a, **_k: None)
    client = FakeClient(
        [
            (status(printing=True), job(completion=99.0)),
            (status(), job(completion=100.0, state="Operational")),
        ]
    )
    watcher = Watcher(client, RecordingNotifier())
    watcher._primed = True  # tested separately; here we want the transitions
    assert watcher.poll_once().kind == "started"
    event = watcher.poll_once()

    assert event.kind == "finished"
    events = [json.loads(l) for l in isolated_audit_log.read_text().splitlines()]
    assert [e["event"] for e in events] == ["print_started", "print_finished"]
    assert events[-1]["file"] == "plate.gcode"
    assert events[-1]["source"] == "notify"


def test_an_unreachable_printer_is_not_reported_as_a_ruined_print(isolated_audit_log):
    """A blinked wifi link must not look like a disaster.

    Crying wolf here is the expensive failure: a human who has been told twice that a
    good print died stops reading these.
    """

    class Unreachable(FakeClient):
        def get_status(self):
            raise PrinterUnreachable("the Pi did not answer")

    notifier = RecordingNotifier()
    watcher = Watcher(Unreachable([]), notifier)
    watcher.seen = PRINTING_MIDWAY

    assert watcher.poll_once() is None
    assert notifier.sent == []
    assert not isolated_audit_log.exists() or isolated_audit_log.read_text() == ""
    # And it must still believe a print is running, so the real ending is not missed.
    assert watcher.seen.printing is True


def test_a_start_carries_no_snapshot(monkeypatch):
    """Nothing has been printed yet, so the picture is of an empty plate."""
    called = []
    monkeypatch.setattr(
        "vtp.notify.fetch_snapshot", lambda *a, **k: called.append(1) or b"jpeg"
    )
    notifier = RecordingNotifier()
    watcher = Watcher(FakeClient([(status(printing=True), job(completion=0.0))]), notifier)
    watcher._primed = True
    watcher.poll_once()

    assert called == []
    assert notifier.sent[0][1] is None


# --------------------------------------------------------------------------- #
# ntfy, and surviving it
# --------------------------------------------------------------------------- #


EVENT = PrintEvent(
    kind="finished", title="Print finished", message="done", priority=4, tags="ok"
)


def test_a_push_failure_is_swallowed(monkeypatch):
    """A watcher that dies because it could not report is worse than no watcher."""

    def explode(*_a, **_k):
        raise httpx2.ConnectError("no route to host")

    monkeypatch.setattr(httpx2, "post", explode)
    assert Notifier("https://ntfy.invalid", "t").send(EVENT) is False


def test_nothing_is_sent_without_a_topic(monkeypatch):
    def explode(*_a, **_k):
        raise AssertionError("should not have tried to send")

    monkeypatch.setattr(httpx2, "post", explode)
    monkeypatch.setattr(httpx2, "put", explode)
    assert Notifier("https://ntfy.invalid", "").send(EVENT) is False


def test_a_snapshot_is_attached_with_the_text_in_headers(monkeypatch):
    captured = {}

    def put(url, content=None, headers=None, timeout=None):
        captured.update(url=url, content=content, headers=headers)
        return httpx2.Response(200, request=httpx2.Request("PUT", url))

    monkeypatch.setattr(httpx2, "put", put)
    assert Notifier("https://ntfy.sh", "topic").send(EVENT, b"\xff\xd8jpeg") is True

    assert captured["url"] == "https://ntfy.sh/topic"
    assert captured["content"] == b"\xff\xd8jpeg"
    assert captured["headers"]["Filename"] == "snapshot.jpg"
    assert captured["headers"]["Message"] == "done"


def test_non_ascii_text_cannot_break_the_header(monkeypatch):
    """HTTP headers are latin-1. A filename from a printer is not ours to trust."""
    captured = {}
    monkeypatch.setattr(
        httpx2,
        "put",
        lambda url, content=None, headers=None, timeout=None: (
            captured.update(headers=headers)
            or httpx2.Response(200, request=httpx2.Request("PUT", url))
        ),
    )
    event = PrintEvent(
        kind="finished", title="Prïnt — done", message="café", priority=4, tags="ok"
    )
    assert Notifier("https://ntfy.sh", "t").send(event, b"jpeg") is True
    captured["headers"]["Title"].encode("ascii")
    captured["headers"]["Message"].encode("ascii")


def test_a_missing_webcam_costs_nothing(monkeypatch):
    def explode(*_a, **_k):
        raise httpx2.ConnectError("no camera")

    monkeypatch.setattr(httpx2, "get", explode)
    assert fetch_snapshot("http://printer.invalid") is None


# --------------------------------------------------------------------------- #
# What this buys the calibration prompt
# --------------------------------------------------------------------------- #


def test_a_recorded_failure_upgrades_the_recalibration_hint(isolated_audit_log):
    """Before the daemon, this could only be inferred from an explicit cancel."""
    from vtp.calibration import _last_print_went_badly

    isolated_audit_log.write_text(
        json.dumps({"event": "start_print", "file": "a.gcode"})
        + "\n"
        + json.dumps({"event": "print_failed", "reason": "error"})
        + "\n",
        encoding="utf-8",
    )
    assert _last_print_went_badly() is True


def test_a_recorded_success_clears_it(isolated_audit_log):
    from vtp.calibration import _last_print_went_badly

    isolated_audit_log.write_text(
        json.dumps({"event": "cancel_print"})
        + "\n"
        + json.dumps({"event": "start_print", "file": "b.gcode"})
        + "\n"
        + json.dumps({"event": "print_finished", "file": "b.gcode"})
        + "\n",
        encoding="utf-8",
    )
    assert _last_print_went_badly() is False


def test_without_the_daemon_a_bare_start_is_not_treated_as_failure(isolated_audit_log):
    """Ambiguous is not bad news. Nagging on a guess trains people to ignore it."""
    from vtp.calibration import _last_print_went_badly

    isolated_audit_log.write_text(
        json.dumps({"event": "start_print", "file": "c.gcode"}) + "\n", encoding="utf-8"
    )
    assert _last_print_went_badly() is False


def test_starting_the_daemon_mid_print_does_not_announce_a_start(isolated_audit_log):
    """Otherwise every restart claims a print just began, and the log fills with lies.

    The ending of that print is still caught, which is the half that matters.
    """
    notifier = RecordingNotifier()
    client = FakeClient(
        [
            (status(printing=True), job(completion=40.0)),
            (status(), job(completion=100.0, state="Operational")),
        ]
    )
    watcher = Watcher(client, notifier, snapshots=False)

    assert watcher.poll_once() is None, "the print was already running"
    assert notifier.sent == []
    assert watcher.seen.printing is True

    assert watcher.poll_once().kind == "finished"
    assert len(notifier.sent) == 1
