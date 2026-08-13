"""MCP server — the product boundary.

Everything above this file is the user's choice of client and model: Claude Code,
OpenCode, Cline, a voice shim. Everything below is ours. That asymmetry decides
where the rules live.

**The client's model is not trusted to be well-behaved.** The original plan leaned
on constrained decoding to stop a model inventing a parameter; we do not control an
arbitrary client's decoder, so ``extra="forbid"`` and the templates' cross-field
``_validate()`` are the only real gate. Every tool here validates before it builds.

**Tool descriptions are the portable ``CLAUDE.md``.** They ship with the server and
every client reads them; none of them read ``CLAUDE.md``. So the house rules — outer
dimensions, defaults from ``config/defaults.toml``, templates as a fixed whitelist —
are written into the descriptions below rather than assumed.

**Error messages are an API.** They are the client model's only feedback signal for
self-correction, so they must name the offending value. :func:`_describe_error`
exists to keep that true across the MCP boundary.

**Design, slice and print are separate tools, deliberately.** ``design_part`` opens the
model in a viewer and stops; ``slice_part`` takes an STL path and stops; ``start_print``
is its own call and demands a confirmation only a human can give. Nothing chains those
three, because the human approval between them is the whole point — a combined tool
would let a model skip a gate simply by choosing the shorter call.

**Uploading is inside ``start_print``, by explicit decision — 2026-08-12.** There was a
separate ``upload_gcode`` tool and it was removed at the owner's request, which also
restores ``BUILD_PLAN.md`` Phase 5's original signature. The cost was named and
accepted: the human no longer sees the file reach the printer before it commits, so
``bed_confirmed_clear`` is the only thing left between a tool call and a moving machine.
Every Python guard survived the change, and the two OctoPrint calls are still separate
requests with ``print=false`` on the upload. Do not fold slicing in as well.

**The print tools are thin.** Their guards live in :mod:`evee.printer` as Python
refusals, because a rule written only in a tool description is a rule the next client
can talk its way past. What is written here is the same rule stated for the model's
benefit, so it does not have to learn by being refused.

``cancel_print`` **is** a tool as of 2026-08-12, reversing the decision recorded here
before it. The objection was that a model could end an eight-hour print on a misread
sentence; the answer is an explicit ``confirmed`` flag checked with ``is not True``,
the same shape as ``bed_confirmed_clear``, so a misread sentence is not enough. What
made it worth having is that a bare cancel leaves the nozzle over the ruined part with
the plate unreachable — so the tool parks the head, which the button on the machine
does not do.

Parking is the only G-code this package emits, and it is a module constant in
:mod:`evee.printer`. No tool here takes a command string, and none should: a freeform
G-code path would end the property that this server can only do the things it
documents.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from pydantic import ValidationError

from evee.cad import design
from evee.calibration import mesh_state
from evee.config import OUTPUT_DIR
from evee.printer import OctoPrintClient, PrinterError
from evee.slicer import SlicerError, slice_stl
from evee.templates import TEMPLATE_REGISTRY, UnknownTemplateError, get_template
from evee.templates.box import TemplateError
from evee.viewer import open_gcode, open_model

__all__ = ["build_server", "main"]

_INSTRUCTIONS = """\
Parametric CAD for 3D printing. You pick a vetted template and fill its parameters;
the server builds the geometry.

There is no freeform geometry path. If no template fits the request, say so and stop —
do not approximate with a template that does not fit.

Workflow, with a human decision between every step:

  1. list_templates()          - what exists, and each template's parameter schema
  2. design_part(...)          - builds STLs and opens them in a 3D viewer
  3. ** GATE 1: the human approves the shape, or asks for changes **
     Changes mean calling design_part() again with adjusted parameters. Iterate here
     as many times as it takes; this step is cheap and reversible.
  4. slice_part(stl_path)      - only after the human has approved the design.
     Normally this is design_part's plate, which prints every part in one job.
  5. ** GATE 2: the human sees the time and filament cost and approves **
  6. get_printer_status()      - confirm the machine is idle and connected
  7. ** GATE 3: the human physically looks at the build plate and says it is clear **
  8. start_print(gcode_path, bed_confirmed_clear=True)  - uploads it and prints it

calibrate_bed(bed_confirmed_clear=True) is optional and sits outside this sequence.
It measures the bed once so later prints skip a multi-minute probe. Suggest it when
get_printer_status says recommend_recalibration, and never run it unasked: it moves
the machine for minutes and needs the same bed confirmation a print does.

House rules:
- Dimensions in a user's request are OUTER unless they say otherwise.
- Omitted parameters resolve from config/defaults.toml (2.0mm walls, 0.25mm press-fit
  clearance, 1.0mm edge fillet). Do not invent defaults; leave a parameter out.
- Always report the resolved inner dimensions alongside the outer ones. design_part()
  returns both, plus a spec sentence written for reading back to the user.

Each gate is a separate turn. Do not call two of these tools in one turn: the human
has not seen the previous result yet, so there is no approval to act on. Waiting is
the one thing this workflow requires of you.

Gate 3 is different in kind from the others. A print heats a nozzle to 200C and runs
for hours, possibly in an empty room, and a plate with the last part still stuck to it
wrecks the print and can damage the machine. Nobody but a human can check that, so
bed_confirmed_clear must come from a person who has looked at the plate and said so.
Never infer it, never carry it over from an earlier print, and never set it from a
voice transcript — ask through a channel the person is actually looking at. If you
have not asked in this conversation and heard a clear yes, you do not have it.

start_print uploads and starts in one call, so that confirmation is the last thing
standing between you and a running machine. There is no separate upload step to pause
at. Ask, wait for the answer, then call it.

Do not suggest starting a print when the person has said they are going out, going to
bed, or is away from the building. Offer to have it ready for when they are back.

Stopping a print: cancel_print(confirmed=True). It throws the part away and cannot be
undone, so ask in plain words first — "how is it going?" and "this looks wrong" are not
requests to cancel. It parks the head afterwards so the plate can be cleaned. Afterwards
the plate has a failed part on it, so the next print needs a fresh Gate 3.
"""


def _describe_error(exc: Exception) -> str:
    """Render an exception as a message a client model can act on.

    Pydantic nests its errors and prefixes them with a URL; flatten to
    ``field: message`` lines so the offending parameter is named in the first
    few words. Template errors already read well and pass through intact.
    """
    if isinstance(exc, ValidationError):
        lines = []
        for err in exc.errors():
            msg = err["msg"]
            # Cross-field checks surface as "Value error, <our message>"; our own
            # message already names the values, so the prefix is pure noise.
            msg = msg.removeprefix("Value error, ")
            loc = ".".join(str(p) for p in err["loc"])
            # A model_validator has no field location. Prefixing it "(root)" would
            # point the reader at a field that does not exist.
            lines.append(f"{loc}: {msg}" if loc else msg)
        return "; ".join(lines)
    return str(exc)


def build_server() -> MCPServer:
    """Construct the server. Separate from :func:`main` so tests can drive it."""
    server = MCPServer(
        name="evee",
        title="EVEE — Everyday Virtual Engineering Engine",
        instructions=_INSTRUCTIONS,
        version="0.1.0",
    )

    @server.tool()
    def list_templates() -> dict[str, Any]:
        """List every buildable template with its full parameter schema.

        Call this before design_part. The returned JSON Schema for each template is
        the exact contract design_part validates against: unknown parameters are
        rejected, so the schema is not advisory.

        Returns a mapping of template name to:
          description  - what the template is for, in prose
          part_names   - the solids it produces, e.g. ("body", "lid")
          schema       - JSON Schema for that template's parameters
        """
        return {
            name: {
                "description": spec.description,
                "part_names": list(spec.part_names),
                "schema": spec.params_model.model_json_schema(),
            }
            for name, spec in TEMPLATE_REGISTRY.items()
        }

    @server.tool()
    def design_part(template: str, params: dict[str, Any]) -> dict[str, Any]:
        """Build a part from a template and export STLs plus preview images.

        Args:
            template: A template name from list_templates(). Not a description of a
                shape — the exact registry key.
            params: Parameters for that template, matching its schema from
                list_templates(). Unknown keys are rejected rather than ignored.
                Omit a parameter to take the house default; do not guess a value.

        On success the exported STLs are opened in a desktop 3D viewer so the user
        can orbit the real mesh. Returns the resolved spec sentence, per-part STL
        paths and bounding boxes, preview PNG paths, the usable interior dimensions,
        and a "viewer" object saying whether a window actually opened.

        It also returns "plate": one STL holding every part side by side, in exactly
        the arrangement the viewer is showing. That is the normal thing to slice —
        it prints all the parts in one job, so one heat-up and one bed check instead
        of one per part. "plate.fits_bed" says whether it will fit; if it does not,
        slice the parts one at a time instead.

        THIS CALL ENDS YOUR TURN. It is the design gate: report the spec sentence to
        the user, tell them the model is on screen, and stop. Do not slice. If the
        viewer did not open (headless server, remote client — "viewer" says which),
        fall back to the preview PNGs and say so.

        To change the design, call this again with adjusted parameters. That is the
        expected loop, not a failure — iterate until the user approves.

        Raises a tool error naming the offending value if the parameters cannot
        produce valid geometry. When that happens, fix the named parameter and retry
        — do not switch templates to route around it.
        """
        try:
            spec = get_template(template)
        except UnknownTemplateError:
            known = ", ".join(sorted(TEMPLATE_REGISTRY))
            raise ValueError(
                f"unknown template {template!r}; this server builds only: {known}. "
                f"There is no freeform geometry fallback — if none of these fit the "
                f"request, say so rather than substituting one that does not."
            ) from None

        try:
            result = design(template, params)
        except (ValidationError, TemplateError) as exc:
            raise ValueError(
                f"invalid parameters for {spec.name!r}: {_describe_error(exc)}"
            ) from None

        # Showing the model is a convenience, not part of the deliverable: the STLs
        # are already written and correct. open_model never raises for this reason.
        # The review 3MF is what gets opened — it holds the parts spaced apart,
        # where the STLs are each centred on the origin and would coincide.
        showing = [result.review_path] if result.review_path else list(
            result.stl_paths.values()
        )
        launch = open_model(showing)

        return {
            "template": result.template,
            "spec_sentence": result.spec_sentence,
            "params_resolved": result.params,
            "stl_paths": {k: str(v) for k, v in result.stl_paths.items()},
            "review_path": str(result.review_path) if result.review_path else None,
            "plate": {
                "stl_path": str(result.plate_path) if result.plate_path else None,
                "parts": list(result.stl_paths),
                "size": (
                    dict(zip("xyz", result.plate_size)) if result.plate_size else None
                ),
                "fits_bed": not result.plate_bed_violations,
                "reason": "; ".join(result.plate_bed_violations) or None,
                "note": (
                    "Printed together, so one failure loses every part on the plate, "
                    "and the nozzle travels between them on each layer."
                ),
            },
            "bounding_boxes": {
                k: {"x": x, "y": y, "z": z}
                for k, (x, y, z) in result.bounding_boxes.items()
            },
            "preview_paths": {
                k: [str(p) for p in v] for k, v in result.preview_paths.items()
            },
            "inner_dims": (
                dict(zip("lwh", result.inner_dims)) if result.inner_dims else None
            ),
            "viewer": {
                "opened": launch.launched,
                "detail": launch.summary(),
            },
            "next_step": (
                "Ask the user to approve the shape on screen. Call slice_part only "
                "after they do; call design_part again if they want changes. When "
                "they approve, slice the plate — it prints every part in one job, "
                "positioned exactly as shown. Slice a single part's STL only if they "
                "want just that part, or if the plate does not fit the bed."
            ),
        }

    @server.tool()
    def slice_part(stl_path: str) -> dict[str, Any]:
        """Slice one approved STL to G-code with the verified printer profile.

        Call this ONLY after the user has looked at the design and approved it. If
        you have not shown them a model and heard back, you are calling this too
        early.

        Args:
            stl_path: Path to an STL previously produced by design_part. Must be a
                file this server exported; arbitrary paths are rejected.

                Normally this is design_part's plate — every part in one job. Pass a
                single part's STL when the user wants only that part, or when the
                plate does not fit the bed.

        Returns layer count, filament use in grams and millimetres, estimated print
        time, and the G-code path — plus a summary sentence for reading back. Report
        the time and the filament weight to the user; those are the numbers that say
        whether the slice came out sane.

        Slicing is headless. No window opens unless [viewer].gcode_auto_open is on in
        defaults.toml, in which case "viewer" says whether one appeared.

        A part larger than the printer's bed is refused here, before the slicer
        runs, with a message naming the axis and the overshoot.

        There is no profile parameter. Slicing always uses the hand-tuned
        config/ender3_v3se.ini. Do not ask for a different one and do not generate a
        slicer config; a stock profile has none of this machine's start sequence — the
        CR Touch probe, the anti-ooze idle temperature, the prime lines — and its
        first layer fails.

        THIS CALL ENDS YOUR TURN. It is the cost gate. Slicing produces a file and
        nothing else. Printing it is start_print, which uploads and starts in one go —
        so there is a human decision before that call, not after it.
        """
        candidate = Path(stl_path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise ValueError(
                f"no such STL: {stl_path}. Pass a path returned by design_part."
            ) from None

        # Only slice what this server made. A client model can put any string here,
        # and "slice /etc/passwd" should fail on the path, not deep inside PrusaSlicer.
        output_root = OUTPUT_DIR.resolve()
        if not resolved.is_relative_to(output_root):
            raise ValueError(
                f"refusing to slice {resolved}: only STLs this server exported to "
                f"{output_root} can be sliced. Run design_part first and pass the "
                f"path it returns."
            )

        try:
            result = slice_stl(resolved)
        except SlicerError as exc:
            raise ValueError(str(exc)) from None

        # Same rule as the design gate: the G-code is written and correct, so a
        # viewer that will not open is reported, not raised.
        launch = open_gcode(result.gcode_path)

        return {
            "summary": result.summary(),
            "viewer": {"opened": launch.launched, "detail": launch.summary()},
            "gcode_path": str(result.gcode_path),
            "stl_path": str(result.stl_path),
            "profile": str(result.profile),
            "layer_count": result.layer_count,
            "filament_g": result.filament_g,
            "filament_mm": result.filament_mm,
            "filament_cm3": result.filament_cm3,
            "print_time_text": result.print_time_text,
            "print_time_seconds": result.print_time_seconds,
            "bed_levelling": {
                # "probe" means the print measures the bed first, as it always has.
                # "stored" means calibrate_bed's saved mesh is loaded instead, which
                # saves minutes the estimate above never counted in the first place.
                "mode": result.levelling,
                "detail": result.levelling_detail,
            },
            "next_step": (
                "Report the time and the filament weight to the user and stop. If "
                "they want it printed, the next call is get_printer_status."
            ),
        }

    @server.tool()
    def get_printer_status() -> dict[str, Any]:
        """Read the printer's state, temperatures and current job. Changes nothing.

        Call this before uploading anything, and any time the user asks how a print
        is going. It is a read — safe to repeat.

        Returns the state text ("Operational", "Printing", ...), nozzle and bed
        temperatures, the current or last job with its progress, and:
          ready_to_print  - whether a new job could start right now
          blocked_reason  - if not, why, in a sentence you can read to the user
          bed_mesh        - whether a stored bed mesh will be used, and whether it
                            is worth re-running calibrate_bed first

        A blocked_reason is not something to work around. Report it and stop.

        If bed_mesh.recommend_recalibration is true, mention it before the print
        rather than after — it costs a few minutes now and a failed print later.
        """
        mesh = mesh_state()
        with OctoPrintClient() as printer:
            status = printer.get_status()
            job = printer.get_job()

        return {
            "summary": status.summary(),
            "state": status.state,
            "ready_to_print": status.ready_to_print,
            "blocked_reason": status.blocked_reason(),
            "operational": status.operational,
            "printing": status.printing,
            "paused": status.paused,
            "error": status.error,
            "error_text": status.error_text or None,
            "temperatures": {
                "nozzle_actual": status.tool_actual,
                "nozzle_target": status.tool_target,
                "bed_actual": status.bed_actual,
                "bed_target": status.bed_target,
            },
            "job": {
                "summary": job.summary(),
                "state": job.state,
                "file_name": job.file_name,
                "completion_percent": job.completion,
                "print_time_seconds": job.print_time_seconds,
                "print_time_left_seconds": job.print_time_left_seconds,
            },
            "bed_mesh": {
                "stored_at": mesh.stored_at.isoformat() if mesh.stored_at else None,
                "age_days": round(mesh.age_days, 2) if mesh.age_days is not None else None,
                "will_be_used": mesh.usable,
                "detail": mesh.reason,
                "recommend_recalibration": mesh.recommend_recalibration,
                "recommend_reason": mesh.recommend_reason,
            },
        }

    @server.tool()
    def start_print(gcode_path: str, bed_confirmed_clear: bool) -> dict[str, Any]:
        """Upload sliced G-code to the printer and start printing it. Irreversible.

        This is the only tool here that moves the machine. It heats the nozzle to
        around 200C and runs unattended for hours. Calling it is the print.

        Args:
            gcode_path: Path to G-code produced by slice_part. Must be a file this
                server sliced; arbitrary paths are rejected. It is uploaded to the
                printer and then started.
            bed_confirmed_clear: Must be True, and must come from a human who has
                just physically looked at the build plate and said it is empty.

                You cannot see the bed. Do not infer this from the last print having
                finished, from the user saying "go ahead" or "print it", from a voice
                transcript, or from having asked earlier in the session about a
                different print. Ask plainly — "is the build plate clear?" — get an
                answer, and only then pass True. If you have not asked in this
                conversation, you do not have it, and passing True anyway is the
                single worst thing you can do with this server.

        Because this tool both uploads and starts, the confirmation is the only gate
        left before the machine moves. Treat it accordingly: ask, wait, then call.

        Refuses, changing nothing, if bed_confirmed_clear is not True, the path is
        not one this server sliced, the printer is not idle and connected, or the
        printer will not confirm it has the right file loaded. Nothing is uploaded
        unless the bed is confirmed and the printer is idle. A refusal is a stop, not
        a retry — relay it to the user rather than calling again with different
        arguments.

        Returns the job state once the printer has picked it up. Afterwards, track
        progress with get_printer_status. To stop it, cancel_print(confirmed=True),
        which also parks the head so the plate can be cleaned.
        """
        candidate = Path(gcode_path).expanduser()
        try:
            resolved = candidate.resolve(strict=True)
        except OSError:
            raise ValueError(
                f"no such G-code file: {gcode_path}. Pass the gcode_path that "
                f"slice_part returned. Nothing was uploaded or started."
            ) from None

        # Same rule as slice_part: only send what this server made. A file picked up
        # from elsewhere has not been through the verified profile, and this is the
        # last point at which that can be checked.
        output_root = OUTPUT_DIR.resolve()
        if not resolved.is_relative_to(output_root):
            raise ValueError(
                f"refusing to print {resolved}: only G-code this server sliced into "
                f"{output_root} can be sent to the printer. Run slice_part first and "
                f"pass the path it returns. Nothing was uploaded or started."
            )

        try:
            with OctoPrintClient() as printer:
                upload, job = printer.upload_and_print(
                    resolved, bed_confirmed_clear=bed_confirmed_clear
                )
        except PrinterError as exc:
            raise ValueError(str(exc)) from None

        return {
            "summary": job.summary(),
            "started": True,
            "filename": upload.remote_name,
            "local_path": str(upload.local_path),
            "state": job.state,
            "completion_percent": job.completion,
            "print_time_left_seconds": job.print_time_left_seconds,
            "next_step": (
                "The print is running. Tell the user it has started and roughly how "
                "long it will take. Use get_printer_status to check on it; do not "
                "poll in a loop."
            ),
        }

    @server.tool()
    def calibrate_bed(bed_confirmed_clear: bool) -> dict[str, Any]:
        """Probe the bed once and store the mesh, so later prints skip the probe.

        The printer normally re-measures the whole bed at the start of every print,
        which costs several minutes each time. This does that measurement once and
        saves it in the printer's memory; from then on slice_part emits a "load the
        stored mesh" line instead of a fresh probe. Nothing is printed and no filament
        is used.

        Args:
            bed_confirmed_clear: Must be True, and must come from a human who has just
                physically looked at the build plate and said it is empty. The probe
                drives the nozzle down onto the plate at dozens of points; a part still
                stuck to it will be hit. Same rule as start_print — never inferred,
                never carried over from an earlier answer, never read out of a voice
                transcript.

        Refuses, changing nothing, if bed_confirmed_clear is not True, or if the
        printer is busy or not connected.

        Run it once, and again after moving the printer, changing the build plate,
        updating firmware, or after a print whose first layer went wrong. If the
        printer does not confirm within the timeout, nothing is stored and prints keep
        doing the full probe — slower and always correct, so that is a safe outcome to
        report rather than an error to retry blindly.

        THIS CALL ENDS YOUR TURN. It moves the machine for several minutes. Tell the
        user it is probing, suggest they watch it, and stop.
        """
        try:
            with OctoPrintClient() as printer:
                result = printer.store_bed_mesh(
                    bed_confirmed_clear=bed_confirmed_clear
                )
        except PrinterError as exc:
            raise ValueError(str(exc)) from None

        return {
            "summary": result.summary(),
            "stored": result.stored,
            "detail": result.detail,
            "next_step": (
                "Tell the user whether the mesh was stored and stop. If it was, later "
                "slices will say so in their bed_levelling field. If it was not, "
                "prints still work — they just probe the bed first, as before."
            ),
        }

    @server.tool()
    def cancel_print(confirmed: bool) -> dict[str, Any]:
        """Stop the print that is running right now, and park the head. Irreversible.

        The part on the plate is lost. A cancelled print cannot be resumed — the only
        way back is to start again from the beginning, at the full time and filament
        cost. Do not call this to pause a print, to change a setting, or because a
        print looks slow.

        Args:
            confirmed: Must be True, and must come from a human who asked for this
                print to be stopped, in this conversation, in plain words. Do not
                infer it from a question about how the print is going, from someone
                saying the part looks wrong, from a complaint about the noise, or
                from a voice transcript. If you have not asked and heard a clear yes,
                you do not have it.

        Refuses, changing nothing, if confirmed is not True or if nothing is printing.

        After cancelling it waits for the machine to actually stop — OctoPrint reports
        "Cancelling" for a while — then lifts the nozzle, brings the plate forward,
        turns both heaters off and releases the motors, so the plate can be cleaned.
        Parking is best effort: if the machine never went idle in time, the cancel
        still stood and "parked" is false with the reason. Report either way.

        THIS CALL ENDS YOUR TURN. Say the print is stopped, say whether the head
        parked, and stop. The plate is NOT clear afterwards — it has a failed part
        stuck to it, so the next start_print needs a fresh confirmation from someone
        who has looked at it.
        """
        try:
            with OctoPrintClient() as printer:
                result = printer.cancel_print(confirmed=confirmed)
        except PrinterError as exc:
            raise ValueError(str(exc)) from None

        return {
            "summary": result.summary(),
            "cancelled": True,
            "state": result.job.state,
            "file_name": result.job.file_name,
            "parked": result.parked,
            "park_detail": result.park_detail,
            "next_step": (
                "Tell the user the print is stopped and whether the head parked. The "
                "plate now has a failed part on it — it is not clear, and the next "
                "print needs a fresh bed confirmation. If they want to try again, "
                "the G-code is already sliced; a cancelled print is also a good "
                "reason to run calibrate_bed first."
            ),
        }

    return server


def main() -> None:
    """Entry point for ``python -m evee.server`` and for MCP client stdio launch."""
    build_server().run(transport="stdio")


if __name__ == "__main__":
    main()
