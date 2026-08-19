"""Muse composer facts that were measured on the installed build.

The 0.2.1 pin is a live measurement rather than a binary read, so these
tests exist to keep the measured value from silently reverting to the
0.0 floor that preceded it.  The failure that motivated them is not a
missed submission: at 0 ms and 50 ms the Enter is *demoted to a newline*
(0/10 submitted), and at 100 ms it submits 6/10 while silently merging
two separately-intended messages into one turn.  A caller that reads a
0.0 settle therefore sees a submission and the provider receives the
wrong message.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services import muse_native_control


def test_installed_build_pins_a_measured_settle() -> None:
    """0.2.1 carries the measured settle, not an inherited floor."""
    pin = muse_native_control._PROVEN_COMPOSER_NEWLINE["0.2.1"]

    assert pin["keystroke"] == "C-j"
    assert pin["submit_settle_seconds"] == pytest.approx(0.2)


def test_settle_floor_is_the_safe_end_of_the_measured_range() -> None:
    """An unmeasured build inherits the slowest proven interval.

    ``docs/provider-version-policy.md`` §3: a missing measurement selects
    the safe end of the range, never the null value.  The floor is derived
    from the table, so pinning a slower build must raise it rather than
    requiring a second edit here.
    """
    assert muse_native_control._SUBMIT_SETTLE_FLOOR_SECONDS == pytest.approx(0.2)
    assert muse_native_control._SUBMIT_SETTLE_FLOOR_SECONDS == max(
        float(entry["submit_settle_seconds"])
        for entry in muse_native_control._PROVEN_COMPOSER_NEWLINE.values()
    )


def test_plan_on_the_installed_build_states_a_proven_settle() -> None:
    plan = muse_native_control.plan_composer_keystrokes(
        "one\ntwo",
        provider_version="Muse Code 0.2.1 (0.2.1-R1215.1)",
    )

    assert plan["soft_newline_keystroke"] == "C-j"
    assert plan["submit_settle_seconds"] == pytest.approx(0.2)
    assert plan["submit_settle_proven"] is True


def test_plan_on_an_unproven_build_never_settles_at_zero() -> None:
    """The regression this file exists to catch."""
    plan = muse_native_control.plan_composer_keystrokes(
        "one\ntwo",
        provider_version="Muse Code 9.9.9 (9.9.9-RZZZ.1)",
    )

    assert plan["submit_settle_proven"] is False
    assert plan["submit_settle_seconds"] > 0.0
    assert plan["submit_settle_seconds"] == pytest.approx(0.2)


def test_carriage_return_is_never_the_newline_keystroke() -> None:
    """``C-m`` submits through tmux, so it can never carry a line break.

    Measured on 0.2.1-R1215.1 with tmux ``extended-keys`` both on and off:
    ``C-m`` submitted in both modes even though the build's keymap lists
    it among the newline chords.  It is carriage return, so a derivation
    that ever selected it would truncate the message mid-send.
    """
    for version in muse_native_control._PROVEN_COMPOSER_NEWLINE:
        assert muse_native_control._PROVEN_COMPOSER_NEWLINE[version]["keystroke"] != "C-m"

    plan = muse_native_control.plan_composer_keystrokes(
        "one\ntwo",
        provider_version="Muse Code 0.2.1 (0.2.1-R1215.1)",
    )
    assert plan["soft_newline_keystroke"] != "C-m"


def test_carriage_return_never_reaches_the_composer_as_a_character() -> None:
    with pytest.raises(muse_native_control.NativeControlInvalid):
        muse_native_control.plan_composer_keystrokes(
            "one\rtwo",
            provider_version="Muse Code 0.2.1 (0.2.1-R1215.1)",
        )
