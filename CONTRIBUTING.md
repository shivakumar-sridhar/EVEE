# Contributing

Read this first, because the usual answer here is unusual: **this is one person's
workflow, published so you can clone it and change it.** It is not a product trying
to fit every setup. The machine is a specific Ender-3 V3 SE, the profile is
hand-tuned for it, and the numbers in `config/` came from printing things and
looking at them.

So the best thing you can do with it is often **fork it, point it at your printer,
and re-verify** — not send a patch that makes it configurable for both of us.

Pull requests are still welcome. These are the things worth knowing before you open
one.

## Set up

```bash
git clone git@github.com:shivakumar-sridhar/EVEE.git
cd EVEE
uv sync
uv run pytest
git config core.hooksPath .githooks    # refuses to commit credentials
```

`uv sync` and `pytest` are safe on any machine: the suite repoints printer
credentials at `printer.invalid`, so nothing can reach a real printer by accident.

## Never test by starting a real print

Use `slice_part` metadata — layer count, grams, time — to check a change. Refusal
paths *are* testable live, because they refuse. The upload leg is testable live only
below MCP, by calling `OctoPrintClient.upload_gcode` directly, since `start_print`
would go on to print.

Geometry tests assert on **volume and bounding box**, never on exact meshes. OCC
changes its triangulation between versions and a mesh assertion would fail for no
reason a user would care about.

## Safety rules are Python, never prose

There are five, they live in `src/evee/printer.py`, and they are restated in the MCP
tool descriptions that ship with the server. **Only Claude Code reads `CLAUDE.md`** —
an OpenCode or Cline user gets none of it. So a rule that lives only in a document
does not exist for most users.

If you find yourself relying on a comment or a doc to keep something safe, that is a
bug in `server.py`, and a PR fixing it is very welcome.

Concretely, a change is wrong if it:

- gives `bed_confirmed_clear` a default, or checks it for truthiness instead of
  `is not True`
- adds a tool that spans two human gates (design → slice → print in one call)
- uses OctoPrint's `print=true` upload flag
- adds any public method that accepts a G-code command string. Parking the head is
  the only G-code this package emits, and
  `test_no_public_method_accepts_a_gcode_command` enforces that by introspection

## Adding to a template has a fixed shape

New *dimensions* are free. A new *feature* costs this, and that is the intended
trade:

1. extend the Pydantic params model — `extra="forbid"`, including nested models
2. add cross-field checks to `_validate()`, with a message **naming the offending
   value** (those messages are an API: they are the client model's only
   self-correction signal)
3. subtract or add the geometry in the build function
4. assert on volume and bounding box in `tests/`

`ports`, `standoffs` and `lid_posts` in `src/evee/templates/box.py` are the worked
examples.

## Things that are non-goals

- **Freeform geometry.** No tool takes a free-text description and improvises a
  solid. Templates being vetted in advance is what makes the output predictable
  enough to send to a hot machine.
- **Generated slicer configs.** There is one hand-tuned profile and `slice_part`
  exposes no profile parameter. That absence is the enforcement.
- **An extraction model inside the server.** The connecting client's model picks the
  template and fills its parameters from the JSON schema; the server validates.

## Style

Match the surrounding code. Comments here tend to record *why* something is the way
it is — usually because the obvious alternative was tried and failed — so if you are
fixing something subtle, the reason is worth more than the diff.
