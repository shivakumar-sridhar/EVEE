"""Phase 3 — OctoPrint REST client. The only code in this repo that moves a motor.

Everything before this file produces artifacts: a mesh, a G-code file, a picture. They
can all be thrown away. This module heats a 200°C nozzle in a room the human may have
left, so its guards are written as code and not as advice.

**The guards live here, not in a prompt.** ``CLAUDE.md`` is read by exactly one client;
this module is read by all of them. Every rule below is a Python refusal with a message
that names what was wrong, and the MCP tool descriptions repeat them for the model's
benefit — but the refusal is the mechanism and the description is the courtesy.

Four constraints, in the order they bite:

1. **``start_print`` takes ``bed_confirmed_clear`` and has no default for it.** No agent
   can look at a bed. The check is ``is not True`` rather than a truthiness test, so a
   non-empty string or a 1 cannot stand in for a human having actually looked.
2. **The printer must be idle.** A start command sent mid-print is refused by name and
   state, before it reaches the wire.
3. **Upload and start are separate calls, and ``print`` is false in both places** — the
   upload flag and the select command. Collapsing them removes the gate.
4. **A start is verified against a filename.** This is the guard that is easy to miss:
   OctoPrint's ``POST /api/job {"command": "start"}`` takes *no filename*. It prints
   whatever file is currently selected, which may be one a human picked in the web UI
   an hour ago. So :meth:`OctoPrintClient.start_print` selects the file it was asked
   for, reads the selection back, and refuses if it does not match.

**Nothing OctoPrint-shaped leaves this file.** Responses are mapped onto
:class:`PrinterStatus` and :class:`JobStatus` at the boundary, so a later move to
Klipper/Moonraker changes this module and nothing above it.

**The API key never appears in an exception.** Errors quote the URL and the status
code; a key in a traceback ends up in a log, a bug report, or an agent transcript.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from evee.calibration import record_mesh_stored
from evee.config import (
    OUTPUT_DIR,
    bed_extents,
    mesh_probe_settings,
    octoprint_settings,
    park_settings,
    printer_timeout,
    printer_upload_timeout,
    slicer_profile,
)

__all__ = [
    "CancelResult",
    "JobStatus",
    "MeshResult",
    "OctoPrintClient",
    "PrinterError",
    "PrinterStatus",
    "PrinterUnreachable",
    "PrintRefused",
    "UploadResult",
    "stale_reason",
]

#: G-code suffixes OctoPrint will accept for a print job.
_GCODE_SUFFIXES = (".gcode", ".gco", ".g")

#: Appended to on every start and cancel. An irreversible physical action with no
#: record is not auditable after the fact, and "which file did it actually run" is
#: the first question asked when a print goes wrong.
AUDIT_LOG = OUTPUT_DIR / "print_log.jsonl"


#: Home, probe the whole bed, save the mesh to EEPROM. A constant for the same reason
#: :func:`_park_commands` is one — see its docstring.
_MESH_COMMANDS = ("G28", "G29", "M500")


def stale_reason(gcode_path: Path | str) -> str | None:
    """Why this G-code should not be sent, or None if it is current.

    G-code is a *snapshot*. It is compiled from an STL and a slicer profile, and neither
    input leaves any trace in the file — so a G-code that was correct an hour ago can
    describe a shape that has changed or a start sequence that has been rewritten, and
    look identical from the outside.

    This is not hypothetical. On 2026-08-13 a start-sequence fix was verified by slicing
    to a scratch file, and the *stale* copy in ``output/`` was uploaded and started. The
    printer ran the sequence the fix had just replaced, and it was caught only by reading
    the file after the print was already going.

    **The check lives here, not in the MCP tool, deliberately.** That mistake was made by
    calling this client directly and bypassing the tool. A guard the caller can step
    around is a guard for the callers who did not need it.

    Two inputs are compared, both by modification time:

    - **the slicer profile**, since editing ``start_gcode`` invalidates every G-code ever
      sliced, silently and all at once
    - **the sibling STL**, found by the naming convention :func:`evee.slicer.slice_stl`
      writes with (same stem, ``.stl``), for the case where the geometry moved on
    """
    path = Path(gcode_path)
    try:
        cut_at = path.stat().st_mtime
    except OSError:
        return None  # a missing file is upload_gcode's error to report, with its wording

    for label, source in (
        ("the slicer profile", slicer_profile()),
        ("its STL", path.with_suffix(".stl")),
    ):
        try:
            changed_at = source.stat().st_mtime
        except OSError:
            continue  # nothing to compare against is not evidence of staleness
        if changed_at > cut_at:
            return (
                f"it was sliced before {label} last changed ({source.name}), so it is "
                f"stale — it does not contain the current settings or shape. Re-slice "
                f"it and print the new file. Nothing was uploaded."
            )
    return None


def _park_commands() -> tuple[str, ...]:
    """The only G-code this package ever puts on the wire.

    A cancelled print leaves the nozzle sitting over the ruined part, hot, with the
    plate unreachable. This lifts it, brings the bed forward, kills the heaters and
    releases the motors, so a human can walk up and clean the plate.

    **This is a function returning a constant, not a parameter.** No public method on
    :class:`OctoPrintClient` accepts a command, and there is no free-text passthrough
    to the printer anywhere in this package — the moment one exists, "the server can
    only do the five things it documents" stops being true, and a client model gains
    the ability to drive the machine directly. ``tests/test_printer.py`` asserts the
    absence by introspection, because a comment is not a mechanism.
    """
    present_y = bed_extents()[1] * 0.85  # the spot end_gcode presents a finished print at
    return (
        "G91",                            # relative, so the lift works from any height
        "G1 Z10 F600",
        "G90",
        f"G1 X5 Y{present_y:g} F3000",
        "M104 S0",
        "M140 S0",
        "M107",
        "M84 X Y E",                      # Z stays energised; a released Z drops the gantry
    )




class PrinterError(RuntimeError):
    """Base class: anything that stopped a printer operation from completing."""


class PrinterUnreachable(PrinterError):
    """The Pi did not answer, or answered in a way we could not use.

    Distinct from :class:`PrintRefused` because the remedy is different: this is a
    network, credential or configuration problem, not a rule saying no.
    """


class PrintRefused(PrinterError):
    """A guard in this module declined to start a print.

    Raised only for the safety conditions — the bed confirmation, the printer's
    state, the identity of the selected file. A client model that sees this should
    relay it to the human and stop, never retry with different arguments.
    """


@dataclass(frozen=True)
class PrinterStatus:
    """The machine's state, mapped off OctoPrint's response shape."""

    state: str
    operational: bool
    printing: bool
    paused: bool
    error: bool
    error_text: str
    tool_actual: float | None
    tool_target: float | None
    bed_actual: float | None
    bed_target: float | None

    @property
    def ready_to_print(self) -> bool:
        """True only if a new job could legitimately start right now."""
        return self.operational and not (self.printing or self.paused or self.error)

    def blocked_reason(self) -> str | None:
        """Why a print cannot start, phrased for a human, or None if it can.

        Ordered by what a person would want told first: an error over a state, a
        running job over a paused one.
        """
        if self.error:
            detail = f": {self.error_text}" if self.error_text else ""
            return f"the printer reports an error{detail}"
        if not self.operational:
            return (
                f"the printer is not connected (state {self.state!r}); connect it in "
                f"OctoPrint before starting a print"
            )
        if self.printing:
            return "a print is already running"
        if self.paused:
            return "a print is paused — resume or cancel it before starting another"
        return None

    def summary(self) -> str:
        """Read-back line for the human."""

        def temp(actual: float | None, target: float | None) -> str:
            if actual is None:
                return "?"
            if target:
                return f"{actual:.1f}->{target:.0f}C"
            return f"{actual:.1f}C"

        return (
            f"{self.state}: nozzle {temp(self.tool_actual, self.tool_target)}, "
            f"bed {temp(self.bed_actual, self.bed_target)}."
        )


@dataclass(frozen=True)
class JobStatus:
    """The current or most recent job. Every field can be absent when idle."""

    state: str
    file_name: str | None
    completion: float | None
    print_time_seconds: int | None
    print_time_left_seconds: int | None

    def summary(self) -> str:
        if not self.file_name:
            return f"{self.state}, no file loaded."
        parts = [f"{self.state}: {self.file_name}"]
        if self.completion is not None:
            parts.append(f"{self.completion:.1f}% done")
        if self.print_time_left_seconds:
            parts.append(f"about {_human_duration(self.print_time_left_seconds)} left")
        return ", ".join(parts) + "."


@dataclass(frozen=True)
class CancelResult:
    """What a cancel did, and whether the plate was left reachable.

    ``parked`` is reported rather than raised on: by the time it is false the print
    is already stopped, which is what the human asked for.
    """

    job: JobStatus
    parked: bool
    park_detail: str

    def summary(self) -> str:
        where = (
            "the head is parked and the heaters are off"
            if self.parked
            else f"the head was NOT parked — {self.park_detail}"
        )
        return f"Cancelled: {self.job.file_name or 'the job'}. {where}."


@dataclass(frozen=True)
class MeshResult:
    """Whether a bed probe was confirmed, and what to tell the human either way."""

    stored: bool
    detail: str

    def summary(self) -> str:
        head = "Bed mesh stored" if self.stored else "No bed mesh stored"
        return f"{head}: {self.detail}."


@dataclass(frozen=True)
class UploadResult:
    """What landed on the printer, under the name the printer chose for it."""

    local_path: Path
    remote_name: str
    size_bytes: int

    def summary(self) -> str:
        kb = self.size_bytes / 1024
        return (
            f"Uploaded {self.local_path.name} to the printer as {self.remote_name} "
            f"({kb:.0f} KB). Not printing — it is selected and waiting."
        )


def _human_duration(seconds: int) -> str:
    """1844 -> "30m 44s". Mirrors how the slicer reports an estimate."""
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"


def _as_float(value: Any) -> float | None:
    """Temperatures arrive as numbers, as null, or missing entirely."""
    return float(value) if isinstance(value, (int, float)) else None


def _audit(event: str, **fields: Any) -> None:
    """Append one line to :data:`AUDIT_LOG`. Best effort, never raises.

    A failure to write the record must not stop a print the guards already approved
    — but the record is written *before* the command goes out, so an attempt that
    then fails on the wire still leaves a trace.
    """
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        record = {"at": datetime.now(UTC).isoformat(timespec="seconds"), "event": event}
        record.update(fields)
        with AUDIT_LOG.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError:
        pass


class OctoPrintClient:
    """Five operations against OctoPrint, and the rules about when they are allowed.

    Args:
        base_url: Overrides ``OCTOPRINT_URL`` from ``.env``.
        api_key: Overrides ``OCTOPRINT_API_KEY`` from ``.env``.
        transport: An ``httpx2`` transport, for tests. Supplying one keeps the
            header and credential handling under test rather than bypassing it,
            which a caller-supplied ``Client`` would not.
        timeout: Seconds for ordinary requests. Uploads use their own, longer value.
    """

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        timeout: float | None = None,
    ) -> None:
        configured_url, configured_key = octoprint_settings()
        self.base_url = (base_url or configured_url or "").rstrip("/")
        self._api_key = api_key or configured_key
        self._timeout = timeout if timeout is not None else printer_timeout()

        if not self.base_url or not self._api_key:
            missing = []
            if not self.base_url:
                missing.append("OCTOPRINT_URL")
            if not self._api_key:
                missing.append("OCTOPRINT_API_KEY")
            raise PrinterUnreachable(
                f"the printer is not configured: {' and '.join(missing)} is not set. "
                f"Copy .env.example to .env at the repo root and fill it in "
                f"(OctoPrint: Settings -> Application Keys). Exporting it in a shell "
                f"is not enough — an MCP client starts this server with a scrubbed "
                f"environment, so the value has to be in the file."
            )

        self._client = httpx2.Client(
            base_url=self.base_url,
            timeout=self._timeout,
            transport=transport,
            # A redirect would carry the X-Api-Key header to wherever it points,
            # because it is a header we set by hand rather than auth httpx manages.
            # An OctoPrint on the LAN has no reason to redirect.
            follow_redirects=False,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> OctoPrintClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- transport -------------------------------------------------------------

    def _request(
        self,
        method: str,
        path: str,
        *,
        allow: tuple[int, ...] = (),
        timeout: float | None = None,
        **kwargs: Any,
    ) -> httpx2.Response:
        """One request, with transport failures turned into readable refusals.

        Args:
            allow: Status codes to return to the caller instead of raising on.
                Used where a code carries meaning — 404 for "not uploaded yet",
                409 for "printer disconnected".
        """
        headers = {"X-Api-Key": self._api_key}
        try:
            response = self._client.request(
                method,
                path,
                headers=headers,
                timeout=timeout if timeout is not None else self._timeout,
                **kwargs,
            )
        except httpx2.TimeoutException as exc:
            raise PrinterUnreachable(
                f"{self.base_url} did not answer {method} {path} within "
                f"{timeout or self._timeout:g}s. The Pi may be busy driving the "
                f"serial link, or off."
            ) from exc
        except httpx2.RequestError as exc:
            # str(exc) is the connection failure, never the request headers.
            raise PrinterUnreachable(
                f"cannot reach OctoPrint at {self.base_url}: {exc}. Check the Pi is "
                f"on and OCTOPRINT_URL in .env is right."
            ) from exc

        if response.status_code in allow or response.is_success:
            return response

        if response.status_code in (401, 403):
            raise PrinterUnreachable(
                f"OctoPrint rejected the API key ({response.status_code}). Generate a "
                f"new application key on the Pi and put it in .env as "
                f"OCTOPRINT_API_KEY."
            )

        detail = response.text.strip()[:200] or "no detail"
        raise PrinterError(
            f"OctoPrint returned {response.status_code} for {method} {path}: {detail}"
        )

    def _json(self, response: httpx2.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PrinterUnreachable(
                f"{self.base_url} answered {response.request.url.path} with "
                f"something that is not JSON. Is that URL really an OctoPrint?"
            ) from exc
        if not isinstance(payload, dict):
            raise PrinterUnreachable(
                f"expected a JSON object from {response.request.url.path}, "
                f"got {type(payload).__name__}"
            )
        return payload

    # -- reads -----------------------------------------------------------------

    def get_status(self) -> PrinterStatus:
        """Current state and temperatures.

        ``GET /api/printer`` answers 409 rather than 200 when the printer is
        disconnected, which is a state worth reporting and not an error worth
        raising on — so that case falls back to ``/api/connection``, which answers
        whether or not a printer is attached.
        """
        response = self._request("GET", "/api/printer", allow=(409,))
        if response.status_code == 409:
            return self._disconnected_status()

        payload = self._json(response)
        state = payload.get("state") or {}
        flags = state.get("flags") or {}
        temps = payload.get("temperature") or {}
        tool = temps.get("tool0") or {}
        bed = temps.get("bed") or {}

        return PrinterStatus(
            state=str(state.get("text") or "Unknown"),
            operational=bool(flags.get("operational")),
            printing=bool(flags.get("printing"))
            or bool(flags.get("pausing"))
            or bool(flags.get("resuming"))
            or bool(flags.get("cancelling"))
            or bool(flags.get("finishing")),
            paused=bool(flags.get("paused")),
            error=bool(flags.get("error")) or bool(flags.get("closedOrError")),
            error_text=str(state.get("error") or ""),
            tool_actual=_as_float(tool.get("actual")),
            tool_target=_as_float(tool.get("target")),
            bed_actual=_as_float(bed.get("actual")),
            bed_target=_as_float(bed.get("target")),
        )

    def _disconnected_status(self) -> PrinterStatus:
        """Build a status for a printer OctoPrint is not talking to."""
        payload = self._json(self._request("GET", "/api/connection"))
        current = payload.get("current") or {}
        return PrinterStatus(
            state=str(current.get("state") or "Disconnected"),
            operational=False,
            printing=False,
            paused=False,
            error=False,
            error_text="",
            tool_actual=None,
            tool_target=None,
            bed_actual=None,
            bed_target=None,
        )

    def get_job(self) -> JobStatus:
        """The running job, or the last one to finish if the printer is idle."""
        payload = self._json(self._request("GET", "/api/job"))
        job = payload.get("job") or {}
        progress = payload.get("progress") or {}
        file_info = job.get("file") or {}

        left = progress.get("printTimeLeft")
        elapsed = progress.get("printTime")
        return JobStatus(
            state=str(payload.get("state") or "Unknown"),
            file_name=file_info.get("name") or None,
            completion=_as_float(progress.get("completion")),
            print_time_seconds=int(elapsed) if isinstance(elapsed, (int, float)) else None,
            print_time_left_seconds=int(left) if isinstance(left, (int, float)) else None,
        )

    # -- writes ----------------------------------------------------------------

    def upload_gcode(self, path: Path | str) -> UploadResult:
        """Send a G-code file to the printer without printing it.

        ``select=true`` marks the file active so the human can see it in the web UI;
        ``print=false`` is the half that matters. OctoPrint's ``print=true`` upload
        flag would start the job on the same request that carried the file, which
        removes the gate this whole module is built around — it is never used here.

        Returns:
            The name OctoPrint actually stored it under, read back from the
            response. It sanitises filenames, so assuming our own stem would give a
            name that :meth:`start_print` then fails to find.
        """
        path = Path(path).expanduser().resolve()
        if not path.is_file():
            raise PrinterError(f"no such G-code file: {path}")
        if path.suffix.lower() not in _GCODE_SUFFIXES:
            raise PrinterError(
                f"not a G-code file: {path.name} (expected one of "
                f"{', '.join(_GCODE_SUFFIXES)}). Slice an STL first."
            )
        size = path.stat().st_size
        if size == 0:
            raise PrinterError(f"G-code file is empty: {path.name}")

        with path.open("rb") as fh:
            response = self._request(
                "POST",
                "/api/files/local",
                files={"file": (path.name, fh, "application/octet-stream")},
                data={"select": "true", "print": "false"},
                timeout=printer_upload_timeout(),
            )

        payload = self._json(response)
        stored = ((payload.get("files") or {}).get("local") or {}).get("name")
        return UploadResult(
            local_path=path,
            remote_name=str(stored or path.name),
            size_bytes=size,
        )

    def start_print(self, filename: str, *, bed_confirmed_clear: bool) -> JobStatus:
        """Start printing a file already on the printer. The last irreversible step.

        Args:
            filename: The name on the printer, as returned by :meth:`upload_gcode`.
                Not a local path.
            bed_confirmed_clear: A human has physically looked at the build plate
                and it is empty. Keyword-only and with no default, so it cannot be
                supplied by accident or by argument order.

        Raises:
            PrintRefused: A guard said no. In order: the bed was not confirmed, the
                filename is not a bare name, the file is not on the printer, the
                printer is not idle, or the file that ended up selected is not the
                one asked for.
        """
        # 1. The confirmation. `is not True` on purpose: a truthy value is evidence
        #    that something was passed, not that a person looked at a build plate.
        if bed_confirmed_clear is not True:
            raise PrintRefused(
                "refusing to start: bed_confirmed_clear was "
                f"{bed_confirmed_clear!r}, not True. A human has to physically look "
                "at the build plate and confirm it is empty — this cannot be "
                "inferred, assumed, or read out of a voice transcript. Ask the "
                "person, wait for their answer, and do not call this again until "
                "you have one."
            )

        # 2. A bare filename. Also keeps a path fragment out of the request URL.
        name = filename.strip()
        if not name or "/" in name or "\\" in name or name.startswith("."):
            suggestion = Path(filename).name
            raise PrintRefused(
                f"{filename!r} is not a filename on the printer. Pass the name "
                f"upload_gcode returned"
                + (f" — did you mean {suggestion!r}?" if suggestion else "")
            )

        # 3. The file exists there. A 404 here is far kinder than starting whatever
        #    else happened to be selected.
        probe = self._request("GET", f"/api/files/local/{name}", allow=(404,))
        if probe.status_code == 404:
            raise PrintRefused(
                f"{name!r} is not on the printer. Upload it with upload_gcode first; "
                f"do not start a different file that happens to be there."
            )

        # 4. The machine is idle.
        status = self.get_status()
        blocked = status.blocked_reason()
        if blocked:
            raise PrintRefused(f"refusing to start {name}: {blocked}.")

        # 5. Make the selection match the request. This is the guard that is easy to
        #    leave out: the start command below names no file, so without this it
        #    prints whatever was last selected — possibly by a human in the web UI,
        #    possibly hours ago. `print: false` again, for the same reason as upload.
        self._request(
            "POST",
            f"/api/files/local/{name}",
            json={"command": "select", "print": False},
        )
        selected = self.get_job().file_name
        if selected != name:
            raise PrintRefused(
                f"refusing to start: asked the printer to select {name!r} but it "
                f"reports {selected!r} as the loaded file. Nothing was started. "
                f"Check the file in OctoPrint's web UI before trying again."
            )

        _audit(
            "start_print",
            file=name,
            bed_confirmed_clear=True,
            printer=self.base_url,
            state_before=status.state,
        )

        self._request("POST", "/api/job", json={"command": "start"})

        # OctoPrint returns 204 and picks the job up asynchronously, so an immediate
        # read can still say "Operational". Give it a moment purely so the summary
        # handed back to the human reflects reality.
        time.sleep(0.5)
        return self.get_job()

    def upload_and_print(
        self, path: Path | str, *, bed_confirmed_clear: bool
    ) -> tuple[UploadResult, JobStatus]:
        """Send a file to the printer and start it. One call, every guard intact.

        This is what the ``start_print`` MCP tool drives, and it is the shape
        ``BUILD_PLAN.md`` Phase 5 always specified. It does not weaken any check —
        :meth:`upload_gcode` still uploads with ``print=false`` and
        :meth:`start_print` still runs its full sequence, including selecting the
        file and reading the selection back. What it removes is the human's chance
        to see the file land before it commits, which was a decision made
        deliberately and with that cost understood.

        **The state check happens before the upload**, not only inside
        :meth:`start_print`. Uploading to a busy printer and only then refusing
        would leave a file the human did not ask for on the SD card, and would
        charge them a 500 KB transfer to learn something one cheap GET could have
        told us. The check inside :meth:`start_print` still runs afterwards; it
        catches the narrow window where a print began while we were uploading.
        """
        if bed_confirmed_clear is not True:
            raise PrintRefused(
                "refusing to print: bed_confirmed_clear was "
                f"{bed_confirmed_clear!r}, not True. A human has to physically look "
                "at the build plate and confirm it is empty — this cannot be "
                "inferred, assumed, or read out of a voice transcript. Nothing was "
                "uploaded. Ask the person, wait for their answer, and do not call "
                "this again until you have one."
            )

        stale = stale_reason(path)
        if stale:
            raise PrintRefused(f"refusing to print {Path(path).name}: {stale}")

        status = self.get_status()
        blocked = status.blocked_reason()
        if blocked:
            raise PrintRefused(
                f"refusing to print {Path(path).name}: {blocked}. Nothing was "
                f"uploaded."
            )

        result = self.upload_gcode(path)
        job = self.start_print(
            result.remote_name, bed_confirmed_clear=bed_confirmed_clear
        )
        return result, job

    def _send_commands(self, commands: Sequence[str]) -> None:
        """Put G-code lines on the wire. Private, and only ever called with a constant.

        See :func:`_park_commands` for why this takes no caller-supplied string in
        practice and why no public method exposes one.
        """
        self._request(
            "POST", "/api/printer/command", json={"commands": list(commands)}
        )

    def _wait_until_idle(self) -> tuple[bool, PrinterStatus]:
        """Poll until the machine is genuinely idle. ``(idle, last status)``.

        A fixed number of attempts rather than a wall-clock deadline, because the test
        suite patches :func:`time.sleep` to a no-op — a deadline loop would then spin
        thousands of requests through a mock transport instead of iterating twice.

        The predicate leans on :attr:`PrinterStatus.printing` already folding in
        OctoPrint's ``cancelling`` and ``finishing`` flags, which is exactly the state
        a fresh cancel leaves behind and exactly the state during which OctoPrint
        rejects commands.
        """
        timeout, poll = park_settings()
        attempts = max(1, math.ceil(timeout / poll))

        status = self.get_status()
        for _ in range(attempts):
            if status.operational and not (status.printing or status.paused):
                return True, status
            time.sleep(poll)
            status = self.get_status()
        return False, status

    def cancel_print(self, *, confirmed: bool) -> CancelResult:
        """Stop the running job and park the head so the plate can be cleaned.

        Args:
            confirmed: A human asked for this print to be stopped, in words. Checked
                with ``is not True`` and keyword-only, for the same reasons as
                ``bed_confirmed_clear``: a truthy value is evidence that something was
                passed, not that a person decided to throw a print away.

        Raises:
            PrintRefused: ``confirmed`` was not exactly ``True``. Nothing is sent.
            PrinterError: nothing is printing, so there is nothing to cancel.

        Parking is best effort and never raises. By the time it runs the print is
        already stopped — which is what was asked for — so a printer that never went
        idle, or refused the command, is reported in the result rather than turned
        into a failure that would imply the cancel did not happen.
        """
        if confirmed is not True:
            raise PrintRefused(
                f"refusing to cancel: confirmed was {confirmed!r}, not True. "
                "Cancelling throws away the part on the plate and every minute "
                "already spent printing it, and it cannot be resumed. A human has to "
                "ask for this in plain words — it cannot be inferred from a question "
                "about how the print is going. Nothing was sent."
            )

        status = self.get_status()
        if not (status.printing or status.paused):
            raise PrinterError(
                f"nothing to cancel: the printer is {status.state!r}, not printing."
            )

        job = self.get_job()
        _audit(
            "cancel_print",
            confirmed=True,
            file=job.file_name,
            printer=self.base_url,
            state_before=status.state,
        )
        self._request("POST", "/api/job", json={"command": "cancel"})
        time.sleep(0.5)

        parked, detail = self._park_after_cancel()
        _audit("park_head", parked=parked, detail=detail, printer=self.base_url)
        return CancelResult(job=self.get_job(), parked=parked, park_detail=detail)

    def store_bed_mesh(self, *, bed_confirmed_clear: bool) -> MeshResult:
        """Probe the bed once and store the mesh, so later prints can skip the probe.

        Sends ``G28``, ``G29``, ``M500`` — home, probe, save to EEPROM. Nothing is
        printed and no filament moves, but the nozzle is driven down onto the plate at
        every probe point, which is why the bed still has to be confirmed clear.

        Args:
            bed_confirmed_clear: Must be True, from a human who has just looked at the
                plate. Same rule and same check as starting a print: a part still stuck
                to the bed will be hit by the probe.

        **Confirming it worked is the hard part.** ``POST /api/printer/command``
        returns 204 the instant OctoPrint queues the lines, the probe then takes
        minutes, and no OctoPrint state flag ever says "the probe finished" — the
        printer stays ``Operational`` throughout. So a fourth command is appended:
        ``M104 S<ack>``, a nozzle temperature low enough to be harmless. OctoPrint
        learns the target from the printer's own temperature reports, so that target
        cannot appear until Marlin has executed that line — which it only reaches
        *after* ``G29`` returns. Seeing it is a genuine acknowledgement.

        The failure direction follows for free: a probe that errors never reaches the
        ``M104``, the wait times out, and no mesh is recorded, so slicing keeps
        emitting the full ``G29``. Recording optimistically is the one mistake that
        would matter, because every later print would trust it.
        """
        if bed_confirmed_clear is not True:
            raise PrintRefused(
                f"refusing to probe: bed_confirmed_clear was {bed_confirmed_clear!r}, "
                "not True. The probe drives the nozzle down onto the plate at dozens "
                "of points, so a part still stuck to it gets hit. A human has to look "
                "at the plate and say it is empty. Nothing was sent."
            )

        status = self.get_status()
        blocked = status.blocked_reason()
        if blocked:
            raise PrintRefused(f"refusing to probe the bed: {blocked}.")

        ack, timeout, poll = mesh_probe_settings()
        _audit(
            "store_bed_mesh",
            bed_confirmed_clear=True,
            printer=self.base_url,
            state_before=status.state,
        )
        self._send_commands((*_MESH_COMMANDS, f"M104 S{ack:g}"))

        confirmed = self._await_probe_ack(ack, timeout, poll)
        # Whether or not it worked, do not leave the nozzle warming.
        try:
            self._send_commands(("M104 S0",))
        except PrinterError:
            pass

        if not confirmed:
            return MeshResult(
                stored=False,
                detail=(
                    f"the printer never acknowledged finishing the probe within "
                    f"{timeout:g}s, so no mesh has been recorded and prints will keep "
                    f"probing the bed — which is slower and always correct. Watch the "
                    f"machine and check OctoPrint's terminal for a probe error before "
                    f"running this again."
                ),
            )

        record_mesh_stored(self.base_url)
        return MeshResult(
            stored=True,
            detail="the bed mesh is stored; prints will load it instead of probing",
        )

    def _await_probe_ack(self, ack: float, timeout: float, poll: float) -> bool:
        """Wait for the nozzle target to read back the sentinel. See above."""
        attempts = max(1, math.ceil(timeout / poll))
        for _ in range(attempts):
            time.sleep(poll)
            target = self.get_status().tool_target
            if target is not None and abs(target - ack) < 0.5:
                return True
        return False

    def _park_after_cancel(self) -> tuple[bool, str]:
        """Wait out the cancel, then park. Returns ``(parked, why not)``."""
        idle, status = self._wait_until_idle()
        if not idle:
            timeout, _ = park_settings()
            return False, (
                f"the printer was still {status.state!r} after {timeout:g}s, and "
                f"OctoPrint will not take commands until a job has finished winding "
                f"down. The print is stopped; move the head from the machine or "
                f"OctoPrint's control tab before cleaning the plate"
            )
        try:
            self._send_commands(_park_commands())
        except PrinterError as exc:
            return False, f"the park command was refused: {exc}"
        return True, "lifted, moved clear, heaters off"
