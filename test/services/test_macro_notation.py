"""Tests for the server-authoritative §5.3 macro-notation parser.

Two parsers exist — this Python authority (server-side) and Lane B's
TypeScript live preview — and both are tested against the same golden
vectors in ``test/fixtures/notation_vectors.json``, mirroring the digest
golden-vector precedent: the two sides cannot drift into spelling the
same macro two ways.  Every offset and message in that file was captured
from this parser's actual output; a behaviour change must regenerate the
vectors deliberately, not drift past them.
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import control_input_contract
from cli_agent_orchestrator.services.macro_notation import parse_notation

# Resolved from this file so the suite runs from any working directory.
VECTORS_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "notation_vectors.json"
VECTORS = json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


def _vector_id(vector):
    """Name a case by its notation so a failure reads as the macro itself."""
    return repr(vector["notation"])


class TestGoldenVectors:
    """The shared contract: every vector parses byte-for-byte as pinned."""

    @pytest.mark.parametrize("vector", VECTORS["valid"], ids=_vector_id)
    def test_a_valid_vector_parses_to_the_pinned_events_and_preview(self, vector):
        result = parse_notation(vector["notation"])
        assert result.errors == []
        assert result.events == vector["events"]
        assert result.preview == vector["preview"]

    @pytest.mark.parametrize("vector", VECTORS["invalid"], ids=_vector_id)
    def test_an_invalid_vector_fails_with_the_pinned_offset_and_message(self, vector):
        result = parse_notation(vector["notation"])
        # A failed parse carries no half-product: nothing to save or send.
        assert result.events is None
        assert result.preview is None
        assert [(e.offset, e.message) for e in result.errors] == [
            (e["offset"], e["message"]) for e in vector["errors"]
        ]

    @pytest.mark.parametrize("vector", VECTORS["valid"], ids=_vector_id)
    def test_every_valid_vector_is_a_sendable_v3_sequence(self, vector):
        """The parser's only product is the wire array, so it must be one.

        ``normalize_sequence_events`` is the send path's own structural
        gate; if a parsed sequence failed it, the macro route would accept
        what the control route refuses.
        """
        result = parse_notation(vector["notation"])
        assert control_input_contract.normalize_sequence_events(result.events) == result.events


class TestCaps:
    """The caps refuse rather than truncate: a macro is a short control
    burst, and a silently shortened one sends something the author did not
    write."""

    def test_the_aggregate_text_cap_bites_at_the_token_that_crosses_it(self):
        # Two in-cap texts whose sum crosses 512 bytes: the first parses,
        # the second is where the refusal lands.
        first = '"' + "a" * 300 + '"'
        notation = first + " " + first
        result = parse_notation(notation)
        assert result.events is None
        assert len(result.errors) == 1
        assert result.errors[0].offset == len(first) + 1
        assert "512-byte" in result.errors[0].message

    def test_a_repeat_expansion_counts_every_event_it_stands_for(self):
        # 30 + 3 crosses the 32-event cap only because the second repeat
        # expands; the refusal names the repeat token, not the count.
        notation = "up*30 up*3"
        result = parse_notation(notation)
        assert result.events is None
        assert len(result.errors) == 1
        assert result.errors[0].offset == notation.rindex("up*3")
        assert "32-event" in result.errors[0].message


class TestTextScreening:
    """ESC, C1 CSI, CR and LF can never be sent honestly through the
    control path, so a text carrying them is unrepresentable — refused at
    parse time, before such a macro could be saved."""

    @pytest.mark.parametrize("escape", ["\\r", "\\n", "\\u001b", "\\u009b"])
    def test_a_text_with_a_control_character_is_refused(self, escape):
        result = parse_notation(f'"a{escape}b"')
        assert result.events is None
        assert len(result.errors) == 1
        assert "control character" in result.errors[0].message


class TestChordMapping:
    """D7: ``ctrl+c`` is the provider-agnostic interrupt *key*; every other
    ``ctrl+<letter>`` is a *chord* whose admission is a per-build,
    send-time fact this parser deliberately does not decide."""

    @pytest.mark.parametrize("letter", "abcdefghijklmnopqrstuvwxyz")
    def test_only_ctrl_c_maps_to_a_key_event(self, letter):
        result = parse_notation(f"ctrl+{letter}")
        assert result.errors == []
        if letter == "c":
            assert result.events == [{"type": "key", "key": "C-c"}]
        else:
            assert result.events == [{"type": "chord", "chord": f"C-{letter}"}]


class TestFailFast:
    def test_a_parse_reports_at_most_one_error(self):
        """A parser that continues past the first error would be guessing
        at intent from then on, so the second bad token is never read."""
        result = parse_notation("enter f5 f6")
        assert result.events is None
        assert len(result.errors) == 1
        assert result.errors[0].offset == len("enter ")


_CAP_MESSAGE = (
    "this event brings the sequence past the 32-event cap; a repeat "
    "expansion counts every event it stands for"
)


class TestRepeatConversionSafety:
    """The r11 repeat guard, ported onto this canonical parser (Lane B steer
    033 → integration steer 044): a repeat count that can never fit the
    32-event budget fails BEFORE the integer conversion (CPython refuses
    over-long digit strings with a bare ValueError from its
    int-max-str-digits guard), so the failure keeps the ordinary
    offset-bearing shape and the endpoint answers 422, never 500."""

    def test_thousands_of_digits_repeat_is_the_cap_error(self):
        result = parse_notation("up*" + "9" * 5000)
        assert result.events is None
        assert [(e.offset, e.message) for e in result.errors] == [(0, _CAP_MESSAGE)]

    def test_the_cap_message_is_bounded_for_absurd_counts(self):
        result = parse_notation("up*" + "9" * 5000)
        assert len(result.errors) == 1
        # The message embeds no token, so it is bounded by construction.
        assert len(result.errors[0].message) < 120

    def test_three_digit_counts_fail_before_conversion(self):
        result = parse_notation("up*100")
        assert [(e.offset, e.message) for e in result.errors] == [(0, _CAP_MESSAGE)]

    def test_two_digit_counts_still_convert_and_check_the_budget(self):
        assert parse_notation("up*99").errors[0].message == _CAP_MESSAGE
        # Exactly at the budget the macro is legal.
        assert len(parse_notation("up*32").events) == 32


class TestLoneSurrogate:
    """A lone surrogate is valid JSON but not UTF-8-encodable: it must be an
    offset-bearing parse failure, never an encode crash (same defect class
    as the r11 repeat guard — the endpoint answers 422, never 500)."""

    def test_lone_surrogate_is_an_offset_error(self):
        result = parse_notation('"\\ud800"')
        assert result.events is None
        assert [(e.offset, e.message) for e in result.errors] == [
            (
                0,
                "the text contains a lone surrogate that is not UTF-8-encodable; "
                "an unrepresentable macro is refused, never approximated",
            )
        ]
