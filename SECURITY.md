# Security

This project starts prints on a real machine that heats to 200C, sometimes when
nobody is in the room. "Security" here means physical safety at least as much as it
means software.

## Before you point this at a printer

- Confirm `THERMAL_RUNAWAY_PROTECTION` is enabled in your firmware. Some older
  Creality boards shipped with it off.
- Check the hotend wiring.
- Have a working smoke detector in the room.

Nothing in this repo substitutes for any of that.

## The safety model

Five rules, all enforced as Python refusals in `src/evee/printer.py` and restated in
the MCP tool descriptions that ship with the server:

1. `start_print()` requires an explicit `bed_confirmed_clear=True`, with no default
   anywhere in the chain, checked with `is not True` so a truthy `"yes"` fails. No
   agent can look at a build plate; that argument is a human asserting they did.
2. No tool spans two human gates. There is no call that goes from design to print.
3. OctoPrint's `print=true` upload flag is never used. Upload and start stay two
   separate requests.
4. `start_print` names a file and the server proves the printer selected *that* file
   before starting — OctoPrint's start command carries no filename and will
   otherwise print whatever a human last selected in the web UI.
5. Parking the head is the only G-code this package emits. No public method accepts
   a G-code string, and a test enforces it by introspection.

**These are mechanisms, not documentation.** If you find one holding only because of
a comment or a Markdown file, that is the bug — please report it.

## Credentials

The OctoPrint URL and API key live in `.env`, which is gitignored. `config._dotenv()`
reads that file off disk and deliberately does **not** export into `os.environ`, so
the key stays out of the environment of the slicer and viewer subprocesses.

`.githooks/pre-commit` refuses to commit credential-shaped files and lines. Enable it
with `git config core.hooksPath .githooks`.

An OctoPrint API key is full control of your printer. Treat it accordingly, and
scrub it out of any log you paste into an issue.

## Known residual risk

`output/bed_mesh.json` is a **claim, not a reading.** OctoPrint cannot read a mesh
back out of the printer, so a firmware update or an `M502` invalidates the stored
mesh while the file still says it was stored yesterday. The age limit in
`[bed_mesh] max_age_days` and the `get_printer_status` prompt are the mitigations.
The risk is real and is accepted knowingly.

## Reporting

Open a normal GitHub issue for anything that does not expose a credential. For
something you would rather not post publicly, use GitHub's **Report a vulnerability**
button on the Security tab.

This is a personal project maintained by one person. There is no SLA — expect a
best-effort reply, not a coordinated disclosure process.
