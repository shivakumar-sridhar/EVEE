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


def test_it_waits_for_temperature_close_to_the_plate(start_lines: list[str]):
    """Cura's behaviour: be near the bed before the heaters are waited on.

    Ooze pinned against the plate is a smear the prime line wipes away. Ooze from
    50mm up is a string that lands wherever it likes.
    """
    approach = start_lines.index("G1 Z2.0 F240 ; wait near the plate, not 50mm above the prime line")
    wait_for_nozzle = next(
        i for i, line in enumerate(start_lines) if line.startswith("M109 ")
    )
    assert approach < wait_for_nozzle


def test_the_head_lifts_and_wipes_after_priming(start_lines: list[str]):
    """Whatever the prime lines left behind must not be dragged into the part."""
    assert start_lines[-2] == "G1 Z2.0 F240 ; lift off the prime line"
    assert start_lines[-1] == "G1 X10 Y10 F5000 ; wipe away from the prime line"


def test_both_prime_lines_survive(start_lines: list[str]):
    """The fix reorders and appends. It must not have eaten the priming itself."""
    primes = [line for line in start_lines if line.endswith("; prime the nozzle")]
    assert primes == [
        "G1 X2.0 Y140 E10 F1500 ; prime the nozzle",
        "G1 X2.3 Y10 E10 F1200 ; prime the nozzle",
    ]


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
