"""Printable-volume tests.

The bed size is read from the verified slicer profile rather than written in
Python, so these tests guard the parser and the "no silent default" rule as much
as the arithmetic.
"""

from __future__ import annotations

import pytest

from vtp.config import bed_extents, bed_violations, slicer_profile


def test_bed_comes_from_the_verified_profile():
    """0x0,220x0,220x220,0x220 and max_print_height = 250."""
    assert bed_extents() == (220.0, 220.0, 250.0)


def test_the_profile_actually_states_it():
    """Guards against the parser inventing a plausible answer."""
    text = slicer_profile().read_text(encoding="utf-8")
    assert "bed_shape = 0x0,220x0,220x220,0x220" in text
    assert "max_print_height = 250" in text


def test_a_profile_without_bed_keys_raises_rather_than_defaulting(tmp_path, monkeypatch):
    """A wrong bed size that silently passes is worse than no check."""
    profile = tmp_path / "bare.ini"
    profile.write_text("layer_height = 0.2\n", encoding="utf-8")
    monkeypatch.setattr("vtp.config.slicer_profile", lambda: profile)
    bed_extents.cache_clear()
    try:
        with pytest.raises(KeyError, match="bed_shape"):
            bed_extents()
    finally:
        bed_extents.cache_clear()


def test_a_part_within_the_bed_has_no_violations():
    assert bed_violations((30.6, 27.7, 11.0)) == []


def test_a_part_exactly_at_the_limit_is_allowed():
    assert bed_violations((220.0, 220.0, 250.0)) == []


@pytest.mark.parametrize(
    "size,axis,over",
    [
        ((220.1, 10, 10), "X", "0.1"),
        ((10, 300, 10), "Y", "80"),
        ((10, 10, 260), "Z", "10"),
    ],
)
def test_an_oversized_part_names_the_axis_and_the_overshoot(size, axis, over):
    """This message is a client model's only chance to correct the parameters."""
    reasons = bed_violations(size)
    assert len(reasons) == 1
    assert reasons[0].startswith(axis)
    assert f"over by {over}mm" in reasons[0]


def test_every_offending_axis_is_reported_not_just_the_first():
    assert len(bed_violations((300, 300, 300))) == 3
