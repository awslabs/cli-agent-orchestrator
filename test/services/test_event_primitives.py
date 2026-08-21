"""Tests for semantic event normalization (services/event_primitives.py).

Includes a Hypothesis property test for normalization totality plus targeted
unit tests for each lifecycle mapping rule.
"""

from hypothesis import given
from hypothesis import strategies as st

from cli_agent_orchestrator.services.event_primitives import (
    KINDS,
    PRIMITIVES,
    normalize_kind,
)


class TestNormalizeKindMapping:
    """Unit tests for each documented lifecycle -> primitive mapping rule."""

    def test_create_terminal_maps_to_launch(self) -> None:
        assert normalize_kind("post_create_terminal") == "launch"

    def test_create_session_maps_to_launch(self) -> None:
        assert normalize_kind("post_create_session") == "launch"

    def test_kill_terminal_maps_to_completion(self) -> None:
        assert normalize_kind("post_kill_terminal") == "completion"

    def test_kill_session_maps_to_completion(self) -> None:
        assert normalize_kind("post_kill_session") == "completion"

    def test_send_message_a2a_maps_to_a2a_delegation(self) -> None:
        assert (
            normalize_kind("post_send_message", {"orchestration_type": "a2a"}) == "a2a_delegation"
        )

    def test_send_message_a2a_substring_case_insensitive(self) -> None:
        assert (
            normalize_kind("post_send_message", {"orchestration_type": "A2A_DELEGATE"})
            == "a2a_delegation"
        )

    def test_send_message_non_a2a_maps_to_handoff(self) -> None:
        for otype in ("send_message", "handoff", "assign", ""):
            assert normalize_kind("post_send_message", {"orchestration_type": otype}) == "handoff"

    def test_send_message_missing_detail_defaults_to_handoff(self) -> None:
        assert normalize_kind("post_send_message") == "handoff"
        assert normalize_kind("post_send_message", {}) == "handoff"

    def test_unmapped_event_type_passes_through_as_other(self) -> None:
        assert normalize_kind("post_something_unknown") == "other"
        assert normalize_kind("") == "other"


class TestNormalizeKindTotality:
    """Property 4: normalization totality.

    **Validates: Requirements 1.1, 1.6, 1.7**
    """

    @given(st.text())
    def test_any_string_returns_a_member_of_the_closed_set(self, event_type: str) -> None:
        """For any string input, the result is in the closed KINDS set and is not None."""

        result = normalize_kind(event_type)
        assert result is not None
        assert result in KINDS

    @given(
        st.text(),
        st.dictionaries(
            keys=st.text(),
            values=st.one_of(st.text(), st.integers(), st.none(), st.booleans()),
        ),
    )
    def test_any_string_and_detail_returns_closed_set(self, event_type: str, detail: dict) -> None:
        """Totality holds even with arbitrary detail dicts; never raises."""

        result = normalize_kind(event_type, detail)
        assert result in KINDS

    def test_other_is_not_a_primitive(self) -> None:
        """'other' is a reserved pass-through sentinel, not a first-class primitive."""

        assert "other" not in PRIMITIVES
        assert "other" in KINDS
