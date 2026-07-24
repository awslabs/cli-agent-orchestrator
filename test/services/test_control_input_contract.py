"""Tests for the shared control-input wire contract."""

import pytest

from cli_agent_orchestrator.services import control_input_contract as contract


class TestProtocolIdentity:
    def test_protocol_and_schema_are_pinned(self):
        """Both sides negotiate on these exact strings; drift is a break."""
        assert contract.CONTROL_INPUT_PROTOCOL == "cao-control-input-v1"
        assert contract.CONTROL_INPUT_SCHEMA_VERSION == 1

    def test_outcomes_are_a_closed_set(self):
        assert contract.CONTROL_INPUT_OUTCOMES == {
            "accepted",
            "refused",
            "ambiguous",
            "unsupported",
        }

    def test_only_a_refusal_may_be_reattempted(self):
        """A refusal is decided before any write; nothing else proves that."""
        assert contract.REATTEMPTABLE_OUTCOMES == {contract.REFUSED}
        assert contract.ACCEPTED not in contract.REATTEMPTABLE_OUTCOMES
        assert contract.AMBIGUOUS not in contract.REATTEMPTABLE_OUTCOMES
        assert contract.UNSUPPORTED not in contract.REATTEMPTABLE_OUTCOMES

    def test_reason_codes_cover_the_named_refusals(self):
        for reason in (
            contract.REASON_UNKNOWN_TERMINAL,
            contract.REASON_IDENTITY_MISMATCH,
            contract.REASON_STALE_GENERATION,
            contract.REASON_PANE_BUSY,
            contract.REASON_ILLEGAL_CONTROL_BYTES,
            contract.REASON_MULTILINE_REJECTED,
            contract.REASON_PROVIDER_UNSUPPORTED,
            contract.REASON_CONTROL_ROUTE_ABSENT,
        ):
            assert reason in contract.CONTROL_INPUT_REASON_CODES


class TestBracketedPasteSentinels:
    def test_sentinels_are_the_decset_2004_sequences(self):
        assert contract.BRACKETED_PASTE_START == "\x1b[200~"
        assert contract.BRACKETED_PASTE_END == "\x1b[201~"

    @pytest.mark.parametrize(
        "payload",
        [
            "\x1b[200~hello",
            "hello\x1b[201~",
            "a\x1b[200~b\x1b[201~c",
            b"\x1b[200~hello",
            b"hello\x1b[201~",
        ],
    )
    def test_detects_sentinels_in_text_and_bytes(self, payload):
        assert contract.contains_bracketed_paste_sentinel(payload) is True

    @pytest.mark.parametrize(
        "payload",
        [
            "/compact",
            "",
            "^[[200~ rendered by a leaking pane",
            "\x1b[2J",
            b"/compact",
            b"",
        ],
    )
    def test_clean_payloads_are_clean(self, payload):
        assert contract.contains_bracketed_paste_sentinel(payload) is False


class TestTransportClassification:
    def test_two_hundred_defers_to_the_typed_body(self):
        assert contract.classify_transport_status(200) is None

    def test_no_response_is_ambiguous_not_absent(self):
        """A lost response is not evidence that nothing was delivered."""
        assert contract.classify_transport_status(None) == contract.AMBIGUOUS

    @pytest.mark.parametrize("status", [404, 405, 501])
    def test_missing_route_is_unsupported(self, status):
        assert contract.classify_transport_status(status) == contract.UNSUPPORTED

    def test_protocol_literal_rejection_is_unsupported(self):
        assert (
            contract.classify_transport_status(422, protocol_mismatch=True) == contract.UNSUPPORTED
        )

    def test_plain_unprocessable_entity_is_a_refusal(self):
        """A malformed body is the caller's bug, not an old server."""
        assert contract.classify_transport_status(422) == contract.REFUSED

    @pytest.mark.parametrize("status", [400, 401, 403, 409, 429, 418])
    def test_client_errors_are_refusals(self, status):
        assert contract.classify_transport_status(status) == contract.REFUSED

    @pytest.mark.parametrize("status", [408, 425, 500, 502, 503, 504, 599])
    def test_interrupted_or_failed_requests_are_ambiguous(self, status):
        assert contract.classify_transport_status(status) == contract.AMBIGUOUS

    @pytest.mark.parametrize("status", [100, 204, 302, 0, -1])
    def test_unrecognised_results_fail_towards_ambiguous(self, status):
        assert contract.classify_transport_status(status) == contract.AMBIGUOUS

    def test_every_status_yields_a_typed_outcome_or_a_body_read(self):
        """No status may leave a caller without a typed answer to report."""
        statuses = [None] + list(range(100, 600))
        for status in statuses:
            for mismatch in (False, True):
                outcome = contract.classify_transport_status(status, protocol_mismatch=mismatch)
                assert outcome is None or outcome in contract.CONTROL_INPUT_OUTCOMES
                if status != 200:
                    assert outcome is not None

    def test_no_status_is_reported_as_accepted_without_a_body(self):
        """Acceptance is a fact the server states, never one inferred here."""
        for status in [None] + list(range(100, 600)):
            assert contract.classify_transport_status(status) != contract.ACCEPTED
