"""Whether the printer has a stored bed mesh, and whether it can be trusted.

The Ender re-probes the whole bed at the start of every print, which costs several
minutes each time for a mesh that rarely changes. Marlin can store one with ``M500``
and load it with ``M420 S1`` instead. This module owns the one question that makes
that safe to do: **has a mesh actually been stored, and is it recent enough to use?**

**The default is always the probe.** No state file, an unreadable one, a stale one —
every uncertain case answers "not usable", and :mod:`vtp.slicer` leaves the profile's
``G29`` alone. That direction matters because the failure is silent: ``M420 S1``
against a mesh Marlin does not have prints on a flat plane and warns only on the
serial console, which nobody is watching. Slower and correct beats faster and wrong.

**The state file is a claim, not a reading.** OctoPrint's REST API cannot read a mesh
back out of the printer, so nothing here can verify one still exists. A firmware
update, an ``M502``, or a rehomed Z can invalidate the mesh while this file still says
it was stored yesterday. That is why the age limit exists and why the tool that writes
this file tells the human to re-run it after touching the machine.

**"The last print failed" is only as good as the daemon.** :func:`vtp.printer._audit`
records what this process *commands* — a start, a cancel. Whether a print then finished
or failed is observed by :mod:`vtp.notify`, which appends ``print_finished`` and
``print_failed``. With that daemon running the question has a real answer. Without it
the log holds intent only, a lone ``start_print`` is ambiguous between finished and
silently failed, and this reads it as "not bad news" on purpose. Staying quiet on
ambiguity is the point: a prompt that fires on a guess is one people learn to dismiss.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from vtp.config import OUTPUT_DIR, mesh_max_age_days

__all__ = ["MESH_STATE", "MeshState", "mesh_state", "record_mesh_stored"]

#: Written by ``calibrate_bed``, read by every slice. Deliberately in ``output/``
#: beside the print log: both are records of what was done to a physical machine.
MESH_STATE = OUTPUT_DIR / "bed_mesh.json"


@dataclass(frozen=True)
class MeshState:
    """What we believe about the printer's stored bed mesh."""

    stored_at: datetime | None
    printer: str | None
    age_days: float | None
    #: May :mod:`vtp.slicer` swap ``G29`` for ``M420 S1``?
    usable: bool
    #: Why, in a sentence that can be read to a human.
    reason: str
    recommend_recalibration: bool
    recommend_reason: str | None


def record_mesh_stored(printer: str) -> None:
    """Note that a mesh was stored, having actually confirmed it.

    Only called once the printer has acknowledged executing past ``G29`` — see
    ``OctoPrintClient.store_bed_mesh``. Writing this optimistically would be the one
    mistake that matters here, because every later print would trust it.
    """
    MESH_STATE.parent.mkdir(parents=True, exist_ok=True)
    MESH_STATE.write_text(
        json.dumps(
            {
                "stored_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "printer": printer,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _audit_path() -> Path:
    """The print log, resolved late.

    Imported inside the function on purpose. ``tests/conftest.py`` redirects
    ``vtp.printer.AUDIT_LOG`` to a scratch file, and a module-level ``from vtp.printer
    import AUDIT_LOG`` would bind the real path at import time and read the real
    machine's log right past that patch — the same hazard the printer module
    documents for ``octoprint_settings``. It also keeps :mod:`vtp.printer` free to
    import this module at load time.
    """
    from vtp import printer

    return printer.AUDIT_LOG


#: Events that say how a print turned out, mapped to "is this bad news".
#: ``print_finished`` / ``print_failed`` come from the :mod:`vtp.notify` daemon, the
#: only thing that observes an *outcome*. ``start_print`` and ``cancel_print`` are
#: written by this process when it issues a command, so they record intent. Both kinds
#: share the log; the newest one wins.
_OUTCOME_EVENTS = {
    "start_print": False,
    "cancel_print": True,
    "print_finished": False,
    "print_failed": True,
}


def _last_print_went_badly() -> bool:
    """True if the newest outcome-bearing audit event was a bad one.

    Newest *event*, not newest failure: a start after a cancel means the human already
    moved on, and nagging about a mesh at that point is noise.

    How much this knows depends on whether the notify daemon was running. With it, a
    print that failed is recorded as failed. Without it the log holds only commands, so
    a print that finished perfectly and one that failed silently both leave a lone
    ``start_print`` — which reads here as "not bad news", deliberately. Staying quiet on
    ambiguity is the point: a prompt that fires on a guess is one people learn to
    dismiss.
    """
    path = _audit_path()
    if not path.is_file():
        return False

    last = None
    try:
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            name = event.get("event")
            if name in _OUTCOME_EVENTS:
                last = name
    except OSError:
        return False
    return _OUTCOME_EVENTS.get(last or "", False)


def mesh_state() -> MeshState:
    """Read the stored-mesh claim and decide whether a slice may rely on it."""
    went_badly = _last_print_went_badly()

    def absent(reason: str) -> MeshState:
        return MeshState(
            stored_at=None,
            printer=None,
            age_days=None,
            usable=False,
            reason=reason,
            recommend_recalibration=True,
            recommend_reason=reason,
        )

    if not MESH_STATE.is_file():
        return absent(
            "no bed mesh has been stored, so every print probes the bed first. Run "
            "calibrate_bed once to skip that."
        )

    try:
        payload = json.loads(MESH_STATE.read_text(encoding="utf-8"))
        stored_at = datetime.fromisoformat(payload["stored_at"])
    except (OSError, ValueError, KeyError, TypeError):
        return absent(
            f"{MESH_STATE.name} could not be read, so the stored mesh is being "
            f"ignored and prints will probe the bed. Run calibrate_bed to rewrite it."
        )

    if stored_at.tzinfo is None:
        stored_at = stored_at.replace(tzinfo=UTC)

    age_days = (datetime.now(UTC) - stored_at).total_seconds() / 86400
    limit = mesh_max_age_days()
    printer = payload.get("printer")

    if age_days > limit:
        reason = (
            f"the stored bed mesh is {age_days:.1f} days old, past the {limit:g}-day "
            f"limit, so prints will probe the bed instead. Run calibrate_bed to "
            f"refresh it."
        )
        return MeshState(
            stored_at=stored_at,
            printer=printer,
            age_days=age_days,
            usable=False,
            reason=reason,
            recommend_recalibration=True,
            recommend_reason=reason,
        )

    return MeshState(
        stored_at=stored_at,
        printer=printer,
        age_days=age_days,
        usable=True,
        reason=(
            f"a bed mesh stored {age_days:.1f} days ago will be loaded instead of "
            f"probing."
        ),
        recommend_recalibration=went_badly,
        recommend_reason=(
            "the last print did not finish. If it went wrong at the first layer, "
            "re-run calibrate_bed before trying again — a stored mesh can go stale "
            "without anything reporting it."
            if went_badly
            else None
        ),
    )
