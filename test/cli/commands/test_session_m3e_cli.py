"""`cao session handoff` (M3-E), read-only.

What these tests pin is what the operator is *shown*: that a pending handback
says plainly what it is holding, and that an unproven quiescence is never
displayed as though it had been proven.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import session as session_cli

SESSION = "cao-m3e-cli"
HANDOFF_ID = "22222222-2222-4222-8222-222222222222"
DONOR = "aaaaaaaa-1111-4111-8111-111111111111"
RECIPIENT = "bbbbbbbb-2222-4222-8222-222222222222"


def _response(payload, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _handoff(state="pending", turn_state="terminal", turn_proven=True, **overrides):
    record = {
        "handoff_id": HANDOFF_ID,
        "session_name": SESSION,
        "task_occurrence_id": "cccccccc-3333-4333-8333-333333333333",
        "from_agent_id": DONOR,
        "to_agent_id": RECIPIENT,
        "from_incarnation_id": "inc-1",
        "packet_control_id": "dddddddd-4444-4444-8444-444444444444",
        "delivery_state": "pending",
        "successor_occurrence_id": None,
        "state": state,
        "quiescence": {
            "turn_state": turn_state,
            "turn_proven": turn_proven,
            "known_child_effects": "none-tracked",
        },
    }
    record.update(overrides)
    return record


def _invoke(payload, args):
    with patch.object(session_cli.requests, "get") as get:
        get.return_value = _response(payload)
        return CliRunner().invoke(session_cli.session, args)


class TestHandoffShow:
    def test_a_pending_handback_says_what_it_is_holding(self):
        result = _invoke(_handoff(), ["handoff", "show", HANDOFF_ID])
        assert result.exit_code == 0, result.output
        assert "both agents refuse ordinary task input" in result.output
        assert "only the packet control id reaches the recipient" in result.output

    def test_an_unproven_turn_is_never_displayed_as_proven(self):
        # Accepted, because not every installed provider runs a heartbeat
        # producer -- but an operator reading this must not mistake it for an
        # observation that was actually made.
        result = _invoke(
            _handoff(turn_state="unknown", turn_proven=False), ["handoff", "show", HANDOFF_ID]
        )
        assert result.exit_code == 0, result.output
        assert "unknown (UNPROVEN)" in result.output

        proven = _invoke(_handoff(), ["handoff", "show", HANDOFF_ID])
        assert "terminal (proven)" in proven.output

    def test_a_settled_handback_stops_claiming_to_hold_anything(self):
        result = _invoke(
            _handoff(state="rolled-back"),
            ["handoff", "show", HANDOFF_ID],
        )
        assert result.exit_code == 0, result.output
        assert "refuse ordinary task input" not in result.output


class TestHandoffGroup:
    def test_the_group_offers_no_verbs_and_one_question(self):
        # A `handoff start` would imply an atomicity this CLI cannot provide:
        # a handback spans an exact resume and a packet delivery it does not
        # own. And one read, not two: a refused steer names its handoff id
        # verbatim, so the operator's loop is refusal -> id -> `show`. "What is
        # in flight" needs a session name nothing currently makes them ask for.
        assert set(session_cli.session_handoff.commands) == {"show"}
