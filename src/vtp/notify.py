"""Phase 6 — watch the printer and push a notification when something happens.

**This runs as its own process, not inside the MCP server.** That is the whole point.
An MCP server is spawned by the editor and dies with the session, so a poller living
inside it would notify you only while you were sitting at the machine with the agent
open — precisely when you do not need telling. Run this as a daemon:

    python -m vtp.notify            # foreground, Ctrl-C to stop
    systemctl --user start vtp-notify

It shares :class:`vtp.printer.OctoPrintClient` with the server and issues only reads.
It cannot start, stop or alter a print, and it takes no arguments that could make it
do so. The worst a bug here can do is send you a wrong notification.

**It is also the first thing in this repo that records how a print ended.** Until now
``print_log.jsonl`` held starts and cancels — what was *commanded*, never what
happened, which is why :mod:`vtp.calibration` could only treat "the last print was
cancelled" as a proxy. This appends ``print_finished`` and ``print_failed``, so that
question gets a real answer whenever the daemon was running. When it was not, the log
simply lacks those events and the old proxy behaviour stands.

**Notification failures never stop the loop.** ntfy being down, the network being out,
the topic being wrong — all of that is logged to stderr and the poller carries on. A
watcher that dies because it could not report is worse than no watcher.
"""

from __future__ import annotations

import argparse
import logging
import secrets
import signal
import sys
import time
from dataclasses import dataclass

import httpx2

from vtp.config import (
    notify_settings,
    ntfy_settings,
    octoprint_settings,
    printer_timeout,
    write_env_value,
)
from vtp.printer import JobStatus, OctoPrintClient, PrinterError, PrinterStatus, _audit

__all__ = ["PrintEvent", "Watcher", "main"]

log = logging.getLogger("vtp.notify")

#: Below this, a job that stopped did not finish — it was cancelled or it failed.
#: OctoPrint reports completion as a percentage of the file, so a genuine finish lands
#: on 100.0; anything short of essentially-100 means the job ended early.
_DONE_THRESHOLD = 99.5


@dataclass(frozen=True)
class PrintEvent:
    """Something worth telling a human about."""

    kind: str  # started | finished | failed
    title: str
    message: str
    #: ntfy priority: 5 urgent, 4 high, 3 default, 2 low.
    priority: int
    tags: str
    reason: str | None = None
    file_name: str | None = None
    elapsed_seconds: int | None = None


@dataclass(frozen=True)
class _Seen:
    """The last thing we saw, so a transition can be recognised.

    Deliberately a value rather than a pile of instance attributes: deciding what
    changed is then a pure function of (previous, current), which is what makes the
    transition table testable without a printer or a clock.
    """

    printing: bool = False
    operational: bool = False
    file_name: str | None = None
    completion: float | None = None
    elapsed: int | None = None


def _human(seconds: int | None) -> str:
    if not seconds:
        return "unknown time"
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def classify(
    previous: _Seen, status: PrinterStatus, job: JobStatus
) -> tuple[_Seen, PrintEvent | None]:
    """Decide what changed. Pure — no I/O, no clock, no printer.

    The interesting cases are all "was printing, now is not", and they are told apart
    by *why*: the machine went offline, it reported an error, or it simply stopped —
    and a stop at less than ~100% of the file is a cancel, not a finish. That last one
    is the only way this catches somebody pressing cancel on the machine itself, which
    nothing else in this repo can see.
    """
    current = _Seen(
        printing=status.printing,
        operational=status.operational,
        file_name=job.file_name,
        completion=job.completion,
        # Keep the last known elapsed: once a job ends OctoPrint stops reporting it,
        # and "it took unknown time" is a poor notification.
        elapsed=job.print_time_seconds or previous.elapsed,
    )

    if status.printing and not previous.printing:
        return current, PrintEvent(
            kind="started",
            title="Print started",
            message=f"{job.file_name or 'a job'} is printing.",
            priority=2,
            tags="printer",
            file_name=job.file_name,
        )

    # Still going, or still idle. Either way there is nothing to say — and saying it
    # anyway would mean a notification every poll, which is 650 of them on an hour-long
    # print.
    if status.printing or not previous.printing:
        return current, None

    # From here on: it was printing a moment ago and now it is not.
    name = previous.file_name or job.file_name or "the print"
    took = _human(previous.elapsed)

    if not status.operational:
        return current, PrintEvent(
            kind="failed",
            title="Printer disconnected mid-print",
            message=(
                f"{name} was printing and the printer is now {status.state}. "
                f"The part is almost certainly ruined."
            ),
            priority=5,
            tags="rotating_light",
            reason="disconnected",
            file_name=name,
            elapsed_seconds=previous.elapsed,
        )

    if status.error:
        detail = f": {status.error_text}" if status.error_text else ""
        return current, PrintEvent(
            kind="failed",
            title="Print error",
            message=f"{name} stopped with an error{detail}.",
            priority=5,
            tags="rotating_light",
            reason="error",
            file_name=name,
            elapsed_seconds=previous.elapsed,
        )

    # The reading from *this* poll, because that is the one taken after the job ended:
    # a finish lands on 100.0 here while the previous poll may still say 99. But a
    # cancel can leave it null — that is what the real machine did on 2026-08-12 — so
    # fall back to the last figure seen rather than treating null as zero.
    completion = job.completion if job.completion is not None else previous.completion
    completion = completion if completion is not None else 0.0
    if completion >= _DONE_THRESHOLD:
        return current, PrintEvent(
            kind="finished",
            title="Print finished",
            message=f"{name} finished after {took}. Let it cool before removing it.",
            priority=4,
            tags="white_check_mark",
            file_name=name,
            elapsed_seconds=previous.elapsed,
        )

    return current, PrintEvent(
        kind="failed",
        title="Print cancelled",
        message=(
            f"{name} stopped at {completion:.0f}% after {took}. "
            f"There is a part stuck to the plate."
        ),
        priority=4,
        tags="x",
        reason="cancelled",
        file_name=name,
        elapsed_seconds=previous.elapsed,
    )


class Notifier:
    """Pushes to ntfy. Every failure is swallowed and logged."""

    def __init__(self, server: str, topic: str, timeout: float = 10.0) -> None:
        self.server = server.rstrip("/")
        self.topic = topic
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.topic)

    def send(self, event: PrintEvent, snapshot: bytes | None = None) -> bool:
        """Publish one notification. Returns whether it went out."""
        if not self.configured:
            log.warning("no ntfy topic configured; not sending %r", event.title)
            return False

        url = f"{self.server}/{self.topic}"
        # ntfy carries the text in headers when the body is a file, and HTTP headers
        # are latin-1. Everything here is templated in Python, but a filename from a
        # printer is not ours, so encode defensively rather than trust it.
        headers = {
            "Title": _header_safe(event.title),
            "Priority": str(event.priority),
            "Tags": event.tags,
        }
        try:
            if snapshot:
                headers["Message"] = _header_safe(event.message)
                headers["Filename"] = "snapshot.jpg"
                httpx2.put(
                    url, content=snapshot, headers=headers, timeout=self.timeout
                ).raise_for_status()
            else:
                httpx2.post(
                    url,
                    content=event.message.encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                ).raise_for_status()
        except Exception as exc:  # noqa: BLE001 - a watcher must survive its own reporting
            log.warning("ntfy push failed (%s): %s", event.title, exc)
            return False
        return True


def _header_safe(text: str) -> str:
    """ASCII-fold for an HTTP header. Non-ASCII would raise on encode."""
    return text.encode("ascii", "replace").decode("ascii")


def fetch_snapshot(base_url: str, timeout: float = 6.0) -> bytes | None:
    """Grab a webcam still, or None. Never raises.

    Best effort by design: a notification that a print finished is worth far more than
    the picture attached to it, and a camera that is unplugged, slow or misconfigured
    must not cost you the alert.
    """
    try:
        response = httpx2.get(
            f"{base_url.rstrip('/')}/webcam/?action=snapshot", timeout=timeout
        )
        if response.status_code == 200 and response.content:
            return response.content
    except Exception as exc:  # noqa: BLE001
        log.debug("no webcam snapshot: %s", exc)
    return None


class Watcher:
    """Polls the printer and pushes an event whenever the state changes."""

    def __init__(
        self,
        client: OctoPrintClient,
        notifier: Notifier,
        *,
        snapshots: bool = True,
    ) -> None:
        self.client = client
        self.notifier = notifier
        self.snapshots = snapshots
        self.seen = _Seen()
        self._stop = False
        # The first poll adopts whatever it finds without announcing it. Start the
        # daemon while a print is already running and, without this, it would report
        # "Print started" for a job that began an hour ago — and it would do so again
        # on every restart. The ending of that print is still notified, which is the
        # part that matters.
        self._primed = False

    def stop(self, *_signal: object) -> None:
        self._stop = True

    def poll_once(self) -> PrintEvent | None:
        """One read, one decision. Returns the event it pushed, if any."""
        try:
            status = self.client.get_status()
            job = self.client.get_job()
        except PrinterError as exc:
            # Unreachable is not the same as disconnected: the Pi may be rebooting, or
            # the wifi may have blinked. Reporting a ruined print on a dropped packet
            # would train you to ignore these, so it is logged and skipped. A printer
            # that is genuinely gone answers, and says it is offline.
            log.warning("could not read the printer: %s", exc)
            return None

        self.seen, event = classify(self.seen, status, job)

        if not self._primed:
            self._primed = True
            if event is not None:
                log.info("already %s at startup; not announcing it", status.state.lower())
            return None

        if event is None:
            return None

        _audit(
            f"print_{event.kind}",
            file=event.file_name,
            reason=event.reason,
            elapsed_seconds=event.elapsed_seconds,
            source="notify",
        )

        snapshot = (
            fetch_snapshot(self.client.base_url)
            if self.snapshots and event.kind != "started"
            else None
        )
        self.notifier.send(event, snapshot)
        return event

    def run(self) -> None:
        """Poll until stopped. Never exits because of a failed read or push."""
        printing_interval, idle_interval = notify_settings()
        log.info("watching %s", self.client.base_url)
        while not self._stop:
            self.poll_once()
            time.sleep(printing_interval if self.seen.printing else idle_interval)


def generate_topic() -> str:
    """A topic nobody will guess.

    On a public ntfy server the topic **is** the credential — anyone who knows the
    string can read your notifications and publish fake ones to you. Generating it
    removes the failure mode where somebody picks ``shiv-printer`` because it is easy to
    remember, and with it the chance that a stranger learns when your house is empty
    because a print just finished.
    """
    return f"vtp-{secrets.token_hex(6)}"


def _ask(question: str, default: bool = True) -> bool:
    """A yes/no question. Treats a bare Enter as the default and EOF as 'no'."""
    hint = "[Y/n]" if default else "[y/N]"
    try:
        answer = input(f"{question} {hint} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    if not answer:
        return default
    return answer in {"y", "yes"}


def _interactive() -> bool:
    """Whether there is a human at a keyboard to answer questions.

    Without this the wizard is indistinguishable from a person declining: ``input()``
    raises ``EOFError`` immediately when stdin is a pipe, :func:`_ask` reads that as
    "no", and the run ends with "Stopped. Nothing was written." — which is the correct
    *action* attached to a misleading *reason*. Run through an agent's shell, a CI job,
    or ``cmd | python -m vtp.notify --setup``, it looks like a refusal rather than a
    missing terminal.
    """
    return sys.stdin.isatty()


def _send_test(server: str, topic: str) -> bool:
    """Send a real notification through the same path the daemon uses."""
    event = PrintEvent(
        kind="finished",
        title="voice-to-print is connected",
        message=(
            "If you can read this, notifications are working. You'll get one of these "
            "when a print finishes, is cancelled, or the printer drops offline."
        ),
        priority=3,
        tags="white_check_mark",
    )
    return Notifier(server, topic).send(event)


def setup(argv_topic: str | None = None) -> int:
    """Walk somebody from nothing to a phone that buzzes.

    Deliberately refuses to write anything until a notification has **actually been
    delivered**. A setup command that reports success because an HTTP request returned
    204 has taught the user nothing — the failure mode it needs to catch is "subscribed
    to the wrong topic", and only a human looking at a phone can catch that.
    """
    server, existing = ntfy_settings()

    if not _interactive():
        topic = argv_topic or existing or generate_topic()
        print(
            f"\n  This needs a terminal it can ask questions in, and stdin is not one.\n"
            f"  Run it directly in a shell:\n\n"
            f"      python -m vtp.notify --setup\n\n"
            f"  Or do it in two steps, which works anywhere:\n\n"
            f"      1. subscribe your phone to:  {topic}\n"
            f"         ({server}/{topic})\n"
            f"      2. python -m vtp.notify --check --topic {topic}\n"
            f"      3. once your phone buzzes:\n"
            f"         python -m vtp.notify --save-topic {topic}\n"
        )
        return 2

    print("\n  Push notifications for print progress.\n")

    topic = argv_topic or existing
    if existing and not argv_topic:
        print(f"  .env already has a topic: {existing}")
        if not _ask("  Generate a new one?", default=False):
            print("  Keeping it.\n")
        else:
            topic = generate_topic()
    if not topic:
        topic = generate_topic()

    if topic != existing:
        print(f"\n  Your topic:  {topic}")
        print("  Keep it private — on a public server, anyone who knows it can read")
        print("  your notifications and send you fake ones.\n")

    print("  1. Install the 'ntfy' app  (Android / iOS, free, no account needed)")
    print("  2. Tap + to subscribe, and either:")
    print(f"       - type the topic:  {topic}")
    print(f"       - or open on your phone:  {server}/{topic}\n")

    if not _ask("  Subscribed and ready for a test?"):
        print("\n  Stopped. Nothing was written. Run this again when you're ready.\n")
        return 1

    print("\n  Sending a test notification...")
    if not _send_test(server, topic):
        print(
            f"\n  Could not reach {server}. Check the network and try again.\n"
            f"  Nothing was written to .env.\n"
        )
        return 1

    if not _ask("  Did your phone buzz?"):
        print(
            "\n  Then the subscription doesn't match. The usual cause is a typo in the\n"
            f"  topic — it must be exactly:  {topic}\n"
            "  Nothing was written to .env, so nothing is half-configured.\n"
        )
        return 1

    write_env_value("NTFY_TOPIC", topic)
    print("\n  Saved to .env  (your other settings were left untouched;")
    print("  the previous file is at .env.bak)\n")

    print("  To have it watch every print, run it in the background:\n")
    print("    cp packaging/vtp-notify.service ~/.config/systemd/user/")
    print("    systemctl --user daemon-reload")
    print("    systemctl --user enable --now vtp-notify")
    print("    loginctl enable-linger $USER      # keep running when logged out\n")
    print("  Or just run it in a terminal:  python -m vtp.notify\n")
    return 0


def check(topic: str | None = None) -> int:
    """Send one test notification. ``topic`` overrides whatever is in ``.env``.

    The override is what makes the two-step, no-terminal path work: test a candidate
    topic before committing it, then save it separately once the phone has buzzed.
    """
    server, configured = ntfy_settings()
    topic = topic or configured
    if not topic:
        print("\n  No NTFY_TOPIC in .env. Run:  python -m vtp.notify --setup\n")
        return 2

    print(f"\n  Sending a test to {server}/{topic} ...")
    if not _send_test(server, topic):
        print("  Failed to send. Check the network.\n")
        return 1
    print("  Sent. If your phone didn't buzz, the subscription doesn't match.\n")
    return 0


def save_topic(topic: str) -> int:
    """Write a topic to ``.env``, for the two-step path where no terminal exists.

    Separate from :func:`check` on purpose. Running this is the human's confirmation
    that their phone actually buzzed — the same gate the interactive wizard asks for,
    just expressed as "you chose to run the second command".
    """
    if not topic.strip():
        print("\n  No topic given.\n")
        return 2
    write_env_value("NTFY_TOPIC", topic.strip())
    print(
        f"\n  Saved {topic.strip()} to .env  (other settings untouched; "
        f"previous file at .env.bak)\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m vtp.notify``."""
    parser = argparse.ArgumentParser(
        prog="vtp.notify", description="Watch the printer and push notifications."
    )
    parser.add_argument(
        "--setup", action="store_true", help="guided first-time setup (do this first)"
    )
    parser.add_argument(
        "--check", action="store_true", help="send one test notification and exit"
    )
    parser.add_argument("--topic", help="use this topic instead of generating one")
    parser.add_argument(
        "--save-topic",
        metavar="TOPIC",
        help="write a topic to .env (use after --check made your phone buzz)",
    )
    args = parser.parse_args(argv)

    if args.save_topic:
        return save_topic(args.save_topic)
    if args.setup:
        return setup(args.topic)
    if args.check:
        return check(args.topic)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    server, topic = ntfy_settings()
    if not topic:
        log.error(
            "No NTFY_TOPIC in .env, so there is nowhere to send. "
            "Run:  python -m vtp.notify --setup"
        )
        return 2

    base_url, _key = octoprint_settings()
    notifier = Notifier(server, topic)
    try:
        client = OctoPrintClient(timeout=printer_timeout())
    except PrinterError as exc:
        log.error("%s", exc)
        return 2

    watcher = Watcher(client, notifier)
    signal.signal(signal.SIGINT, watcher.stop)
    signal.signal(signal.SIGTERM, watcher.stop)

    log.info("notifying %s/%s about %s", server, topic, base_url)
    try:
        watcher.run()
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
