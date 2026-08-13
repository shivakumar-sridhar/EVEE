"""Start-G-code tests for the verified profile.

The profile is hand-tuned and not generated, so there is no code here to test —
only the text itself. These assertions exist because the shape of ``start_gcode``
is load-bearing in ways nothing else would catch.

**The nozzle must already be where it will extrude, at print height, when it gets
hot.** Four cancelled prints were spent on the alternative: park somewhere safe,
reach temperature, then travel to the prime line. Every one of those travels is
time in which ooze collects and gets carried, and the blob welded itself to the
nozzle each time. ``output/BNO_Case.gcode`` — a Cura print of this same part on
this same machine that came out clean — does none of it: it moves to the prime
start *first*, heats there, and the very next command draws the line. This file
pins that ordering.

**``G29 ; auto bed levelling`` is an anchor, not just a command.** It is the string
the stored-mesh substitution matches on, so its exact spelling is part of an
interface even though it looks like ordinary G-code.
"""

from __future__ import annotations

import pytest

from vtp.config import _profile_values, slicer_profile

#: Where the prime line begins. Everything before the heat commands exists to get
#: the nozzle here.
PRIME_START = "G1 X2.0 Y20 Z0.28"


@pytest.fixture(scope="module")
def start_gcode() -> str:
    """The raw ``start_gcode`` value, with its literal ``\\n`` escapes intact."""
    values = _profile_values(slicer_profile(), {"start_gcode"})
    assert "start_gcode" in values, "the verified profile defines no start_gcode"
    return values["start_gcode"]


@pytest.fixture(scope="module")
def start_lines(start_gcode: str) -> list[str]:
    """``start_gcode`` split into the lines the printer will actually execute."""
    return [line.strip() for line in start_gcode.split("\\n") if line.strip()]


def _index(lines: list[str], prefix: str) -> int:
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


def test_the_nozzle_never_lifts_to_z50(start_gcode: str):
    """The original ooze bug in one assertion. Z50 is 50mm of free fall onto the
    prime line."""
    assert "Z50" not in start_gcode


def test_nothing_moves_between_reaching_temperature_and_extruding(start_lines: list[str]):
    """The single most important property here, and the one every failed fix broke.

    Between ``M109`` returning and the first millimetre of extrusion there must be
    no motion at all — no purge, no lift, no travel, no drop. Each of those is a
    window in which drool accumulates on a moving nozzle instead of being pinned to
    the plate and dragged away by the line that follows. Cura's sequence has exactly
    zero commands in this gap; so must ours.
    """
    hot = _index(start_lines, "M109 ")
    first_extrusion = next(
        i for i, line in enumerate(start_lines)
        if i > hot and line.startswith("G1 ") and " E" in line
    )
    between = start_lines[hot + 1 : first_extrusion]
    assert between == [], f"motion between M109 and the first extrusion: {between}"


def test_the_nozzle_reaches_the_prime_start_before_it_is_heated(start_lines: list[str]):
    """Heating happens in position, at print height — not parked and then travelled to.

    Ooze during the wait then lands on the spot the prime line starts from, which is
    the one place on the plate where it does no harm: the next command extrudes
    straight through it.
    """
    in_position = _index(start_lines, PRIME_START)
    bed = _index(start_lines, "M190 ")
    nozzle = _index(start_lines, "M109 ")
    assert in_position < bed < nozzle, "the head must be at the prime start before it heats"


def test_the_first_layer_is_printed_at_200c(start_gcode: str):
    """Drool begins at about 190C — measured on this machine, watching it happen.

    210 meant twenty degrees of weeping while the heater caught up; 200 halves that,
    and 200 is what the clean Cura print of this part used. Raising this back is not
    a free tuning knob: it buys nothing on a first layer and it lengthens the drool
    window.
    """
    values = _profile_values(slicer_profile(), {"first_layer_temperature"})
    assert values["first_layer_temperature"] == "200"


def test_the_nozzle_is_cold_until_after_levelling(start_lines: list[str]):
    """The nozzle used to hold 150C through G28 and G29 — ~187 seconds measured, from
    the plate print running 3416s against a 3229s estimate. PLA weeps slowly at 150,
    and slowly for three minutes is a blob welded to the nozzle. Cura never had this
    because Cura never probes: it loads a stored mesh and is hot for about thirty
    seconds.
    """
    cold = start_lines.index("M104 S0 ; nozzle stays COLD through homing. Nothing molten, "
                             "nothing to weep. With a stored mesh there is no probe to "
                             "keep it warm for")
    home = _index(start_lines, "G28")
    level = _index(start_lines, "G29")
    heat = _index(start_lines, "M109 S{first_layer_temperature")

    assert cold < home < level < heat, "the nozzle must not be warm while it levels"


def test_nothing_warms_the_nozzle_during_levelling(start_lines: list[str]):
    """No M104 with a non-zero target may sneak back in between homing and levelling —
    that is precisely the 'keep it warm to shorten the ramp' convenience that cost three
    prints."""
    home = _index(start_lines, "G28")
    level = _index(start_lines, "G29")
    between = start_lines[home:level]
    assert not [line for line in between if line.startswith("M104") and "S0" not in line]


def test_nothing_retracts_before_there_is_anything_molten(start_lines: list[str]):
    """A retract only makes sense once filament has been pushed through.

    Cold, there is nothing to pull back and yanking solid filament grinds a flat on
    it — that was a real bug. The retract at the end, after both prime lines, is the
    opposite case and is Cura's own: it relieves pressure so the head can leave the
    prime line without stringing.
    """
    first_extrusion = next(
        i for i, line in enumerate(start_lines) if line.startswith("G1 ") and " E" in line
    )
    retracts = [i for i, line in enumerate(start_lines) if line.startswith("G1 E-")]
    assert all(i > first_extrusion for i in retracts), "a retract on cold filament"


def test_the_pointless_warmup_dwell_is_gone(start_gcode: str):
    """30 seconds of a hot nozzle doing nothing. M104 does not block and G28+G29 take
    minutes, so the dwell bought no warmth — only ooze."""
    assert "G4 S30" not in start_gcode


def test_both_prime_lines_survive(start_lines: list[str]):
    """Rewriting the sequence must not eat the priming itself. Two fat lines, 15mm of
    filament each under relative extrusion — the same 30mm total Cura purges, which is
    2.3x what the earlier sequence laid down. A fat line is what carries a blob away."""
    primes = [line for line in start_lines if "prime the nozzle" in line]
    assert len(primes) == 2, primes
    assert all(" E15 " in line for line in primes), f"a prime line that extrudes thinly: {primes}"


def test_homing_and_levelling_are_still_there(start_lines: list[str]):
    """The CR Touch probe is what makes the first layer land at all."""
    assert "G28 ; home all axis" in start_lines
    assert start_lines.count("G29 ; auto bed levelling") == 1


def test_the_profile_never_mentions_m420(start_gcode: str):
    """Loading a stored mesh is a post-export substitution, never a profile edit.

    ``M420 S1`` against a mesh that was never stored does not fail in Marlin — it
    warns on serial and prints on a flat plane. Putting it in the profile would make
    every print depend on EEPROM state this repo cannot read back, so the profile
    keeps the always-correct ``G29`` and the swap happens in ``slicer.py`` only when
    a mesh has demonstrably been stored.
    """
    text = slicer_profile().read_text(encoding="utf-8")
    assert "M420" not in text
