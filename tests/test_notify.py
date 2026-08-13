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
    events = [json.loads(l) for l in isolated_audit_log.read_text(encoding="utf-8").splitlines()]
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
    assert not isolated_audit_log.exists() or isolated_audit_log.read_text(encoding="utf-8") == ""
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


# --------------------------------------------------------------------------- #
# Writing to .env
#
# This edits a file holding a live printer API key. The tests are about what must
# NOT change, not about the one line that does.
# --------------------------------------------------------------------------- #

from vtp.config import _dotenv, env_value, write_env_value

ENV_SAMPLE = """\
# Copy to .env and fill in. .env is gitignored — never commit the real key.

# OctoPrint. Settings -> Application Keys on the Pi.
OCTOPRINT_URL=http://192.168.0.113
OCTOPRINT_API_KEY=SECRETKEYVALUE

LLM_BACKEND=ollama
OLLAMA_HOST=http://localhost:11434
"""


def test_writing_a_key_leaves_every_other_line_alone(tmp_path):
    """The file holds a live API key and hand-written comments. Only one line may move."""
    env = tmp_path / ".env"
    env.write_text(ENV_SAMPLE, encoding="utf-8")

    write_env_value("NTFY_TOPIC", "vtp-abc123", env)
    after = env.read_text(encoding="utf-8").splitlines()

    for line in ENV_SAMPLE.splitlines():
        assert line in after, f"lost: {line!r}"
    assert "NTFY_TOPIC=vtp-abc123" in after
    # Exactly one line added, nothing reordered.
    assert len(after) == len(ENV_SAMPLE.splitlines()) + 1


def test_the_api_key_survives_verbatim(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV_SAMPLE, encoding="utf-8")

    write_env_value("NTFY_TOPIC", "vtp-abc123", env)

    assert "OCTOPRINT_API_KEY=SECRETKEYVALUE" in env.read_text(encoding="utf-8")


def test_an_existing_key_is_replaced_in_place_not_appended(tmp_path):
    """_dotenv takes the LAST occurrence, so an appended duplicate would silently win
    while the top of the file still showed the old value."""
    env = tmp_path / ".env"
    env.write_text(ENV_SAMPLE + "NTFY_TOPIC=vtp-old\nTRAILING=yes\n", encoding="utf-8")

    write_env_value("NTFY_TOPIC", "vtp-new", env)
    lines = env.read_text(encoding="utf-8").splitlines()

    assert lines.count("NTFY_TOPIC=vtp-new") == 1
    assert "NTFY_TOPIC=vtp-old" not in lines
    # Replaced where it was — the line after it did not move to the top.
    assert lines.index("NTFY_TOPIC=vtp-new") < lines.index("TRAILING=yes")


def test_pre_existing_duplicates_are_collapsed(tmp_path):
    env = tmp_path / ".env"
    env.write_text("NTFY_TOPIC=one\nOTHER=x\nNTFY_TOPIC=two\n", encoding="utf-8")

    write_env_value("NTFY_TOPIC", "three", env)
    lines = env.read_text(encoding="utf-8").splitlines()

    assert lines.count("NTFY_TOPIC=three") == 1
    assert not any("one" in line or "two" in line for line in lines)
    assert "OTHER=x" in lines


def test_an_exported_key_is_still_recognised(tmp_path):
    """.env.example documents `export KEY=` as legal, so the writer must match it."""
    env = tmp_path / ".env"
    env.write_text("export NTFY_TOPIC=old\nKEEP=1\n", encoding="utf-8")

    write_env_value("NTFY_TOPIC", "new", env)
    text = env.read_text(encoding="utf-8")

    assert "old" not in text
    assert "NTFY_TOPIC=new" in text
    assert "KEEP=1" in text


def test_a_commented_out_key_is_not_treated_as_the_key(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# NTFY_TOPIC=example-from-docs\nKEEP=1\n", encoding="utf-8")

    write_env_value("NTFY_TOPIC", "real", env)
    lines = env.read_text(encoding="utf-8").splitlines()

    assert "# NTFY_TOPIC=example-from-docs" in lines  # comment preserved
    assert "NTFY_TOPIC=real" in lines


def test_the_previous_file_is_backed_up(tmp_path):
    env = tmp_path / ".env"
    env.write_text(ENV_SAMPLE, encoding="utf-8")

    write_env_value("NTFY_TOPIC", "vtp-abc123", env)

    assert (tmp_path / ".env.bak").read_text(encoding="utf-8") == ENV_SAMPLE


def test_a_missing_env_is_created_from_the_example(tmp_path):
    (tmp_path / ".env.example").write_text("# docs\nOCTOPRINT_URL=\n", encoding="utf-8")
    env = tmp_path / ".env"

    write_env_value("NTFY_TOPIC", "vtp-abc123", env)
    text = env.read_text(encoding="utf-8")

    assert "# docs" in text  # arrives documented, not as a bare key=value
    assert "NTFY_TOPIC=vtp-abc123" in text


def test_a_missing_env_with_no_example_still_works(tmp_path):
    env = tmp_path / ".env"
    write_env_value("NTFY_TOPIC", "vtp-abc123", env)
    assert env.read_text(encoding="utf-8") == "NTFY_TOPIC=vtp-abc123\n"


def test_a_value_containing_a_hash_survives(tmp_path):
    """'#' is legal in a key, and _dotenv deliberately does not strip inline comments."""
    env = tmp_path / ".env"
    write_env_value("OCTOPRINT_API_KEY", "ab#cd", env)

    _dotenv.cache_clear()
    try:
        assert _dotenv(env)["OCTOPRINT_API_KEY"] == "ab#cd"
    finally:
        _dotenv.cache_clear()


def test_the_cache_is_cleared_so_the_new_value_is_readable(tmp_path, monkeypatch):
    """_dotenv is lru_cached — without a clear, a running process keeps the old value."""
    env = tmp_path / ".env"
    env.write_text("NTFY_TOPIC=old\n", encoding="utf-8")
    monkeypatch.setattr("vtp.config.ENV_PATH", env)
    monkeypatch.delenv("NTFY_TOPIC", raising=False)

    _dotenv.cache_clear()
    assert env_value("NTFY_TOPIC") == "old"

    write_env_value("NTFY_TOPIC", "new", env)
    assert env_value("NTFY_TOPIC") == "new"


# --------------------------------------------------------------------------- #
# The setup wizard
#
# The property under test is restraint: it must not write to .env until a human
# has confirmed a notification actually arrived on a phone. A wizard that reports
# success on a 204 has not tested the thing that usually goes wrong — being
# subscribed to the wrong topic.
# --------------------------------------------------------------------------- #

from types import SimpleNamespace

from vtp import notify as notify_module


@pytest.fixture
def wizard(tmp_path, monkeypatch):
    """Isolate .env and stub the network, returning a record of what was sent."""
    env = tmp_path / ".env"
    monkeypatch.setattr("vtp.config.ENV_PATH", env)
    monkeypatch.setattr("vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", None))

    # These tests drive the interactive wizard, so they must claim a terminal —
    # pytest's stdin is a pipe, which the wizard correctly refuses to prompt into.
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)

    sent: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "vtp.notify._send_test",
        lambda server, topic: (sent.append((server, topic)), True)[1],
    )
    return SimpleNamespace(env=env, sent=sent)


def _answers(monkeypatch, *replies):
    queue = list(replies)
    monkeypatch.setattr("builtins.input", lambda *_a: queue.pop(0))


def test_a_generated_topic_is_not_guessable():
    topic = notify_module.generate_topic()
    assert topic.startswith("vtp-")
    assert len(topic) > 14
    assert topic != notify_module.generate_topic()


def test_setup_writes_env_only_after_the_phone_buzzed(wizard, monkeypatch):
    _answers(monkeypatch, "y", "y")  # subscribed? yes.  buzzed? yes.
    assert notify_module.setup() == 0

    assert "NTFY_TOPIC=vtp-" in wizard.env.read_text(encoding="utf-8")
    assert len(wizard.sent) == 1


def test_setup_writes_nothing_when_the_phone_did_not_buzz(wizard, monkeypatch):
    """The failure this exists to catch: subscribed to the wrong topic."""
    _answers(monkeypatch, "y", "n")  # subscribed? yes.  buzzed? no.
    assert notify_module.setup() == 1

    assert not wizard.env.exists(), "half-configured is worse than unconfigured"


def test_setup_writes_nothing_if_the_person_backs_out(wizard, monkeypatch):
    _answers(monkeypatch, "n")
    assert notify_module.setup() == 1

    assert not wizard.env.exists()
    assert wizard.sent == []


def test_setup_writes_nothing_when_the_send_fails(wizard, monkeypatch):
    monkeypatch.setattr("vtp.notify._send_test", lambda *a: False)
    _answers(monkeypatch, "y")
    assert notify_module.setup() == 1

    assert not wizard.env.exists()


def test_an_explicit_topic_is_used_verbatim(wizard, monkeypatch):
    _answers(monkeypatch, "y", "y")
    assert notify_module.setup("vtp-chosen-by-hand") == 0

    assert "NTFY_TOPIC=vtp-chosen-by-hand" in wizard.env.read_text(encoding="utf-8")
    assert wizard.sent[0][1] == "vtp-chosen-by-hand"


def test_an_existing_topic_is_offered_for_reuse(tmp_path, monkeypatch):
    """Regenerating silently would unsubscribe somebody's phone without telling them."""
    env = tmp_path / ".env"
    monkeypatch.setattr("vtp.config.ENV_PATH", env)
    monkeypatch.setattr(
        "vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", "vtp-already-set")
    )
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    sent = []
    monkeypatch.setattr(
        "vtp.notify._send_test", lambda s, t: (sent.append(t), True)[1]
    )

    _answers(monkeypatch, "n", "y", "y")  # regenerate? no.  subscribed? yes.  buzzed? yes.
    assert notify_module.setup() == 0
    assert sent == ["vtp-already-set"]


def test_check_reports_a_missing_topic_and_names_the_fix(monkeypatch, capsys):
    monkeypatch.setattr("vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", None))
    assert notify_module.check() == 2
    assert "--setup" in capsys.readouterr().out


def test_the_daemon_points_at_setup_rather_than_explaining_by_hand(monkeypatch, caplog):
    monkeypatch.setattr("vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", None))
    with caplog.at_level("ERROR"):
        assert notify_module.main([]) == 2
    assert "--setup" in caplog.text


def test_a_pipe_is_not_mistaken_for_a_refusal(monkeypatch, capsys, tmp_path):
    """Without a terminal, input() raises EOFError immediately and _ask reads it as
    "no" — so the wizard used to end with "Stopped. Nothing was written.", which is the
    right action attached to a misleading reason. It has to say the terminal is missing
    and give a path that works anyway."""
    monkeypatch.setattr("vtp.config.ENV_PATH", tmp_path / ".env")
    monkeypatch.setattr("vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", None))
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)

    assert notify_module.setup() == 2

    out = capsys.readouterr().out
    assert "not one" in out            # names the actual problem
    assert "--check --topic" in out    # and a way through it
    assert "--save-topic" in out
    assert not (tmp_path / ".env").exists()


def test_check_can_test_a_topic_before_it_is_saved(monkeypatch, capsys):
    """The two-step path needs to test a candidate without committing to it."""
    sent = []
    monkeypatch.setattr("vtp.notify.ntfy_settings", lambda: ("https://ntfy.test", None))
    monkeypatch.setattr(
        "vtp.notify._send_test", lambda s, t: (sent.append(t), True)[1]
    )

    assert notify_module.check("vtp-candidate") == 0
    assert sent == ["vtp-candidate"]


def test_save_topic_writes_only_the_one_key(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("OCTOPRINT_API_KEY=SECRET\n", encoding="utf-8")
    monkeypatch.setattr("vtp.config.ENV_PATH", env)

    assert notify_module.save_topic("vtp-confirmed") == 0
    text = env.read_text(encoding="utf-8")

    assert "NTFY_TOPIC=vtp-confirmed" in text
    assert "OCTOPRINT_API_KEY=SECRET" in text


def test_save_topic_refuses_an_empty_topic(tmp_path, monkeypatch):
    monkeypatch.setattr("vtp.config.ENV_PATH", tmp_path / ".env")
    assert notify_module.save_topic("   ") == 2
    assert not (tmp_path / ".env").exists()
