"""Focused regression for the Kimi 0.30 steer-effect ACK predicate
(Sol r17, cond steers 138/139): only the exact PROVIDER-BULLETED output row
(``● {ack}``) proves the effect.  A bare ``{ack}`` continuation row — the
shape a wrapped queue/context echo takes — must be rejected, and the
✨-marked instruction row and pre-steer content never satisfy it.

Lives in the ordinary unit-test collection path (NOT under ``test/e2e/``,
which CI's ``--ignore=test/e2e`` unit matrix skips): the predicate is a
pure function of transcript text, so the CI unit matrix executes it on
every run.
"""

from __future__ import annotations

from test.e2e.test_kimi_0300_text_control_live import _ack_rows

ACK = "STEER-ACK-18fc26f8d8"


def test_a_genuine_provider_bulleted_ack_row_is_accepted():
    transcript = (
        "  499\n  500\n\n"
        " ✨ STEERME-1051c741: reply with exactly the line STEER-ACK-18fc26f8d8 and nothing else.\n"
        " ● I should just output it.\n"
        f" ● {ACK}\n"
    )
    assert _ack_rows(transcript, ACK) == [f"● {ACK}"]


def test_a_bare_ack_continuation_row_is_rejected():
    """The wrapped-echo shape: the queued instruction's ACK token landed
    alone on a continuation row.  This must NEVER count as the effect."""
    transcript = (
        " ✨ STEERME-1051c741: reply with exactly the line\n" f"    {ACK} and nothing else.\n"
    )
    # Even after stripping the wrapped indent, the bare row is not a
    # provider-bulleted output row.
    assert _ack_rows(transcript, ACK) == []
    bare = f"{ACK}\n"
    assert _ack_rows(bare, ACK) == []


def test_the_instruction_row_itself_is_rejected():
    transcript = f" ✨ STEERME-1051c741: reply with exactly the line {ACK} and nothing else.\n"
    assert _ack_rows(transcript, ACK) == []


def test_pre_steer_content_is_rejected():
    transcript = "  1\n  2\n  3\n"
    assert _ack_rows(transcript, ACK) == []


def test_a_bulleted_row_with_extra_content_is_rejected():
    """The exact-content rule: bulleted but not exactly the ACK."""
    transcript = f" ● {ACK} — done\n ● {ACK} extra\n ● STEER-ACK-other\n"
    assert _ack_rows(transcript, ACK) == []
