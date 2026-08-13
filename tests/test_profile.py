"""Start-G-code tests for the verified profile.

The profile is hand-tuned and not generated, so there is no code here to test —
only the text itself. These assertions exist because two properties of
``start_gcode`` are load-bearing in ways nothing else would catch:

**The nozzle must wait for temperature near the plate, not above it.** The first
version of this profile lifted to ``Z50`` and then ramped 150 -> 210C and waited out
both ``M190`` and ``M109`` parked directly over the start of the prime line.
Everything that oozed during that wait fell 50mm onto the exact spot the prime line
begins, and the prime line dragged the blob into the part. The first layer was
visibly bad and the cause was invisible in the G-code metadata — layers, grams and
time were all normal.

**``G29 ; auto bed levelling`` is an anchor, not just a command.** It is the string
the stored-mesh substitution matches on, so its exact spelling is part of an
interface even though it looks like ordinary G-code.
"""

from __future__ import annotations

import pytest

from vtp.config import _profile_values, slicer_profile


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


def test_the_nozzle_never_lifts_to_z50(start_gcode: str):
    """The ooze bug in one assertion. Z50 is 50mm of free fall onto the prime line."""
    assert "Z50" not in start_gcode


def test_it_parks_away_from_where_the_prime_line_starts(start_lines: list[str]):
    """The blob forms wherever the nozzle waits, so it must not wait on the prime line.

    Waiting at 2mm stopped ooze free-falling, but at that height it piles into a blob
    and welds to the nozzle instead. Parking at Y150 puts that blob at the far end of
    the lane, where nothing afterwards goes.
    """
    park = next(i for i, line in enumerate(start_lines) if line.startswith("G1 X2.0 Y150"))
    wait_for_nozzle = next(
        i for i, line in enumerate(start_lines) if line.startswith("M109 ")
    )
    assert park < wait_for_nozzle, "the park has to happen before the wait it exists for"


def test_no_prime_move_returns_to_the_park_point(start_lines: list[str]):
    """The documented failure this whole change exists for.

    The old second prime line ran back to Y10 — the spot the nozzle had been sitting
    on — and picked the blob straight back up. Nothing may end at the park Y again.
    """
    park_y = 150.0
    for line in start_lines:
        if not line.startswith("G1 ") or " Y" not in line:
            continue
        y = float(line.split(" Y")[1].split()[0])
        assert y <= park_y, f"{line!r} goes past the park point at Y{park_y:g}"

    extruding = [line for line in start_lines if "prime the nozzle" in line]
    ends = [float(line.split(" Y")[1].split()[0]) for line in extruding]
    assert max(ends) < park_y, "a prime line ends on the blob"


def test_a_retract_precedes_the_temperature_wait(start_lines: list[str]):
    """Pulls the melt back up the nozzle so less escapes during M190/M109."""
    retract = next(i for i, line in enumerate(start_lines) if line.startswith("G1 E-"))
    wait = next(i for i, line in enumerate(start_lines) if line.startswith("M109 "))
    assert retract < wait


def test_the_purge_is_deliberate_and_then_abandoned(start_lines: list[str]):
    """Whatever cooked during the probe comes out on purpose, at the park spot, and
    the head lifts clear before travelling — otherwise it drags the blob along the bed
    to the prime start, which is the original bug with extra steps."""
    purge = next(i for i, line in enumerate(start_lines) if line.startswith("G1 E6"))
    lift = next(
        i for i, line in enumerate(start_lines) if line.startswith("G1 Z1.0") and i > purge
    )
    travel = next(
        i for i, line in enumerate(start_lines) if line.startswith("G1 X2.0 Y140") and i > purge
    )
    assert purge < lift < travel


def test_the_pointless_warmup_dwell_is_gone(start_gcode: str):
    """30 seconds of a hot nozzle doing nothing. M104 does not block and G28+G29 take
    minutes, so the dwell bought no warmth — only ooze."""
    assert "G4 S30" not in start_gcode


def test_the_head_lifts_and_wipes_after_priming(start_lines: list[str]):
    """Whatever the prime lines left behind must not be dragged into the part."""
    assert start_lines[-2] == "G1 Z2.0 F240 ; lift off the prime line"
    assert start_lines[-1] == "G1 X10 Y10 F5000 ; wipe away from the prime line"


def test_both_prime_lines_survive(start_lines: list[str]):
    """The fix reorders and rewrites. It must not have eaten the priming itself."""
    primes = [line for line in start_lines if "prime the nozzle" in line]
    assert len(primes) == 2, primes
    assert all(" E" in line for line in primes), "a prime line that extrudes nothing"


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
