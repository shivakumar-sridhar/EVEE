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

import logging
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


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m vtp.notify``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    server, topic = ntfy_settings()
    if not topic:
        log.error(
            "NTFY_TOPIC is not set in .env, so there is nowhere to send. Pick a long "
            "random topic name — anyone who knows it can read your notifications — "
            "and subscribe to it in the ntfy app."
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
