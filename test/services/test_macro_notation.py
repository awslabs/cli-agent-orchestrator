"""Tests for the §5.3 macro-notation authority parser.

Two layers:

* the **shared golden vectors** (``web/src/test/fixtures/macroNotationVectors.json``)
  that this parser and the TypeScript live-preview parser must both satisfy
  byte-for-byte — offsets, messages, canonical notation, and preview strings —
  mirroring the digest golden-vector precedent;
* parser-specific behavior: caps with offsets, round-trip stability, and the
  contract boundary (parse results are always valid wire sequences).
"""

import json
from pathlib import Path

import pytest

from cli_agent_orchestrator.services.control_input_contract import (
    MAX_SEQUENCE_EVENTS,
    MAX_SEQUENCE_TEXT_BYTES,
    normalize_sequence_events,
)
from cli_agent_orchestrator.services.macro_notation import (
    NotationError,
    parse_notation,
    parse_with_preview,
    render_notation,
    render_preview,
)

VECTORS_PATH = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "src"
    / "test"
    / "fixtures"
    / "macroNotationVectors.json"
)


@pytest.fixture(scope="module")
def vectors():
    return json.loads(VECTORS_PATH.read_text(encoding="utf-8"))


class TestGoldenVectors:
    def test_ok_vectors_parse(self, vectors):
        for case in vectors["ok"]:
            events = parse_notation(case["notation"])
            assert events == case["events"], case["name"]

    def test_ok_vectors_canonical_and_preview(self, vectors):
        for case in vectors["ok"]:
            assert render_notation(case["events"]) == case["canonical"], case["name"]
            assert render_preview(case["events"]) == case["preview"], case["name"]

    def test_ok_vectors_round_trip(self, vectors):
        for case in vectors["ok"]:
            assert parse_notation(case["canonical"]) == case["events"], case["name"]

    def test_error_vectors(self, vectors):
        for case in vectors["errors"]:
            with pytest.raises(NotationError) as excinfo:
                parse_notation(case["notation"])
            assert excinfo.value.offset == case["offset"], case["name"]
            assert excinfo.value.message == case["message"], case["name"]


class TestCaps:
    def test_single_event_past_the_cap_carries_its_offset(self):
        notation = " ".join(["up"] * (MAX_SEQUENCE_EVENTS + 1))
        with pytest.raises(NotationError) as excinfo:
            parse_notation(notation)
        # The 33rd "up" token starts after 32 tokens plus their separators.
        assert excinfo.value.offset == 32 * len("up ")
        assert excinfo.value.message == (f"sequence holds at most {MAX_SEQUENCE_EVENTS} events")

    def test_exactly_at_the_event_cap_parses(self):
        events = parse_notation(" ".join(["up"] * MAX_SEQUENCE_EVENTS))
        assert len(events) == MAX_SEQUENCE_EVENTS

    def test_text_byte_cap_carries_the_offending_token_offset(self):
        first = "x" * (MAX_SEQUENCE_TEXT_BYTES - 10)
        second = "y" * 20
        notation = f'"{first}" "{second}"'
        with pytest.raises(NotationError) as excinfo:
            parse_notation(notation)
        assert excinfo.value.offset == len(first) + 3  # quote, close, space
        assert "512-byte aggregate cap" in excinfo.value.message

    def test_exactly_at_the_byte_cap_parses(self):
        events = parse_notation(f'"{ "x" * MAX_SEQUENCE_TEXT_BYTES }"')
        assert events == [{"type": "text", "text": "x" * MAX_SEQUENCE_TEXT_BYTES}]


class TestRepeatConversionSafety:
    """r11 repair: a repeat count that can never fit the 32-event budget
    fails BEFORE the integer conversion, with the ordinary offset-bearing
    cap error — never a bare conversion ValueError (CPython's
    int-max-str-digits guard) and never an HTTP 500."""

    def test_thousands_of_digits_repeat_is_a_cap_error(self):
        with pytest.raises(NotationError) as excinfo:
            parse_notation("up*" + "9" * 5000)
        assert excinfo.value.offset == 0
        assert excinfo.value.message.endswith("expands past the 32-event cap")
        assert "int" not in excinfo.value.message.lower() or "digit" not in excinfo.value.message

    def test_long_count_token_is_bounded_in_the_message(self):
        with pytest.raises(NotationError) as excinfo:
            parse_notation("up*" + "9" * 5000)
        # The embedded token is display-bounded even for absurd inputs.
        assert len(excinfo.value.message) < 100

    def test_three_digit_count_fails_before_conversion(self):
        with pytest.raises(NotationError) as excinfo:
            parse_notation("up*100")
        assert excinfo.value.offset == 0
        assert excinfo.value.message == "repeat 'up*100' expands past the 32-event cap"

    def test_two_digit_count_still_converts_and_checks_the_budget(self):
        with pytest.raises(NotationError) as excinfo:
            parse_notation("up*99")
        assert excinfo.value.offset == 0
        assert excinfo.value.message == "repeat 'up*99' expands past the 32-event cap"
        # And exactly at the budget it parses.
        assert len(parse_notation("up*32")) == 32


class TestContractBoundary:
    def test_parse_results_are_valid_wire_sequences(self):
        events = parse_notation('"/compact" enter up*2 ctrl+s ctrl+c')
        assert events == normalize_sequence_events(events)

    def test_rendered_notation_reparses_identically(self):
        events = parse_notation('"/model" enter up*3 enter ctrl+s escape*2')
        assert parse_notation(render_notation(events)) == events

    def test_parse_with_preview_matches_render_preview(self):
        events, preview = parse_with_preview('"a" up*2')
        assert preview == render_preview(events)


class TestRendererRefusals:
    """Forms the notation cannot represent are refused, never approximated."""

    def test_key_c_s_has_no_notation_name(self):
        # ctrl+s parses to a *chord*; a wire key C-s has no notation name.
        with pytest.raises(ValueError, match="no notation name"):
            render_notation([{"type": "key", "key": "C-s"}])

    def test_chord_c_c_would_not_round_trip(self):
        # ctrl+c parses to key C-c, so a chord C-c cannot be rendered.
        with pytest.raises(ValueError, match="no notation form"):
            render_notation([{"type": "chord", "chord": "C-c"}])

    def test_multi_modifier_chord_has_no_notation_form(self):
        with pytest.raises(ValueError, match="no notation form"):
            render_notation([{"type": "chord", "chord": "C-Up"}])

    def test_unknown_key_has_no_notation_name(self):
        with pytest.raises(ValueError, match="no notation name"):
            render_notation([{"type": "key", "key": "F1"}])

    def test_unknown_event_type_has_no_notation_form(self):
        # A bare {'type': ...} probe normalizes fine; notation cannot name it.
        with pytest.raises(ValueError, match="no notation form"):
            render_notation([{"type": "future-event"}])
