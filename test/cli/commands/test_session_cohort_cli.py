"""`cao session cohort` — the operator's fleet Pause/Stop/Resume surface.

The contract here is mostly about what an operator can reach by accident.
Force is a different command, not a flag; a partial restore is rendered as a
partial restore; and a promoted safe operation never reads as safe.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import session as session_cli

SESSION = "cao-fleet"


def _response(payload: dict, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _operation(**overrides):
    provenance = {
        "operation_id": "11111111-1111-4111-8111-111111111111",
        "session_name": SESSION,
        "operation_kind": "resume",
        "state": "settled",
        "state_epoch": 2,
        "lifecycle_epoch": 4,
        "lifecycle_observation": "stopped",
        "roster_revision": "ab" * 32,
        "member_snapshot_digest": "cd" * 32,
        "requested_mode": "safe",
        "current_mode": "safe",
        "promoted_to_force": False,
        "promotion_receipt_digest": None,
        "promoted_by": None,
        "initiator_kind": "operator",
        "initiated_by": "colin",
        "source_operation_id": "22222222-2222-4222-8222-222222222222",
        "resume_target": "working",
        "member_outcomes": {"restored-exact": 2, "failed": 1},
        "continuity": [
            {
                "agent_id": "a1",
                "role": "supervisor",
                "included": True,
                "exclusion_reason": None,
                "lineage_id": "l1",
                "harness": "claude_code",
                "native_session_id": "n1",
                "incarnation_id": "i1",
                "terminal_id": "term-1",
                "generation": "g1",
                "final_state": "restored-exact",
            },
            {
                "agent_id": "a3",
                "role": "worker",
                "included": True,
                "exclusion_reason": None,
                "lineage_id": "l3",
                "harness": "codex_cli",
                "native_session_id": "n3",
                "incarnation_id": "i3",
                "terminal_id": "term-3",
                "generation": "g3",
                "final_state": "failed",
            },
        ],
    }
    provenance.update(overrides.pop("provenance", {}))
    record = {
        "operation_id": provenance["operation_id"],
        "state": provenance["state"],
        "operation_kind": provenance["operation_kind"],
        "current_mode": provenance["current_mode"],
        "provenance": provenance,
    }
    record.update(overrides)
    return record


# ---------------------------------------------------------------------------
# safe and force are separate commands
# ---------------------------------------------------------------------------


def test_there_is_no_force_flag_on_any_cohort_command():
    """Force is reachable only by naming it."""
    names = {command.name for command in session_cli.session_cohort.commands.values()}
    assert {"stop-safe", "stop-force", "pause-force", "resume-paused", "resume-start"} <= names
    for command in session_cli.session_cohort.commands.values():
        flags = {option.name for option in command.params}
        assert "force" not in flags
        assert "mode" not in flags


def test_stop_force_asks_twice_and_sends_both_acknowledgements():
    posted = {}

    def _post(url, json=None, **_kwargs):
        posted["url"] = url
        posted["json"] = json
        return _response(_operation())

    with patch.object(session_cli.requests, "post", side_effect=_post):
        result = CliRunner().invoke(
            session_cli.session,
            ["cohort", "stop-force", SESSION, "--by", "colin", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert posted["url"].endswith(f"/sessions/{SESSION}/cohort/stop/force")
    assert posted["json"]["acknowledged_one_way"] is True
    assert posted["json"]["acknowledged_force"] is True


def test_stop_safe_requires_the_drain_receipt():
    result = CliRunner().invoke(
        session_cli.session, ["cohort", "stop-safe", SESSION, "--by", "colin", "--yes"]
    )

    assert result.exit_code != 0
    assert "--drain-receipt" in result.output


def test_stop_safe_forwards_the_opaque_receipt_untouched():
    posted = {}

    def _post(url, json=None, **_kwargs):
        posted["json"] = json
        return _response(_operation())

    with patch.object(session_cli.requests, "post", side_effect=_post):
        result = CliRunner().invoke(
            session_cli.session,
            [
                "cohort",
                "stop-safe",
                SESSION,
                "--by",
                "colin",
                "--drain-receipt",
                "f" * 64,
                "--yes",
            ],
        )

    assert result.exit_code == 0, result.output
    assert posted["json"]["drain_receipt_digest"] == "f" * 64


def test_pause_force_names_the_interrupt_before_doing_it():
    with patch.object(session_cli.requests, "post", return_value=_response(_operation())):
        aborted = CliRunner().invoke(
            session_cli.session,
            ["cohort", "pause-force", SESSION, "--by", "colin"],
            input="n\n",
        )

    assert aborted.exit_code != 0
    assert "Interrupt every running turn" in aborted.output


def test_each_command_mints_its_own_operation_id():
    seen = []

    def _post(_url, json=None, **_kwargs):
        seen.append(json["operation_id"])
        return _response(_operation())

    with patch.object(session_cli.requests, "post", side_effect=_post):
        for _ in range(2):
            CliRunner().invoke(
                session_cli.session,
                ["cohort", "resume-paused", SESSION, "--by", "colin"],
            )

    assert len(set(seen)) == 2


# ---------------------------------------------------------------------------
# rendering the durable truth
# ---------------------------------------------------------------------------


def test_a_partial_restore_is_rendered_as_settled_and_names_what_was_lost():
    with patch.object(session_cli.requests, "post", return_value=_response(_operation())):
        result = CliRunner().invoke(
            session_cli.session, ["cohort", "resume-start", SESSION, "--by", "colin"]
        )

    assert result.exit_code == 0, result.output
    assert "settled" in result.output
    assert "restored-exact" in result.output
    assert "did not come back" in result.output
    assert "term-3" in result.output
    assert "codex_cli" in result.output


def test_a_promoted_safe_operation_never_renders_as_safe():
    record = _operation(
        provenance={
            "operation_kind": "stop",
            "requested_mode": "safe",
            "current_mode": "force",
            "promoted_to_force": True,
            "promoted_by": "colin",
            "promotion_receipt_digest": "9" * 64,
            "member_outcomes": {"stopped": 2},
            "continuity": [],
        }
    )
    with patch.object(session_cli.requests, "get", return_value=_response(record)):
        result = CliRunner().invoke(session_cli.session, ["cohort", "show", record["operation_id"]])

    assert result.exit_code == 0, result.output
    assert "PROMOTED" in result.output
    assert "safe -> force" in result.output
    assert "stop (force)" in result.output


def test_resume_output_shows_the_stop_it_descends_from():
    with patch.object(session_cli.requests, "post", return_value=_response(_operation())):
        result = CliRunner().invoke(
            session_cli.session, ["cohort", "resume-paused", SESSION, "--by", "colin"]
        )

    assert "resumes         22222222-2222-4222-8222-222222222222" in result.output
    assert "restores to     working" in result.output


def test_the_list_marks_a_promoted_operation():
    payload = {
        "operations": [
            {
                "operation_id": "33333333-3333-4333-8333-333333333333",
                "operation_kind": "stop",
                "requested_mode": "safe",
                "current_mode": "force",
                "state": "stopped",
                "initiated_by": "colin",
            }
        ],
        "count": 1,
    }
    with patch.object(session_cli.requests, "get", return_value=_response(payload)):
        result = CliRunner().invoke(session_cli.session, ["cohort", "list", SESSION])

    assert result.exit_code == 0, result.output
    assert "promoted from safe" in result.output


def test_an_empty_list_says_so_rather_than_printing_nothing():
    with patch.object(
        session_cli.requests, "get", return_value=_response({"operations": [], "count": 0})
    ):
        result = CliRunner().invoke(session_cli.session, ["cohort", "list", SESSION])

    assert "no fleet operations recorded" in result.output


def test_a_server_refusal_is_surfaced_with_its_detail():
    refusal = _response({"detail": "session cao-fleet is working, not stopped"}, status=409)
    with patch.object(session_cli.requests, "post", return_value=refusal):
        result = CliRunner().invoke(
            session_cli.session, ["cohort", "resume-start", SESSION, "--by", "colin"]
        )

    assert result.exit_code != 0
    assert "409" in result.output
    assert "not stopped" in result.output


def test_a_reconciling_operation_is_never_rendered_as_finished():
    record = _operation(
        provenance={
            "state": "reconciliation-required",
            "retryable": True,
            "reconciliation_reason": (
                "the fleet was restored but its supervisor reconciliation wake did not land"
            ),
            "member_outcomes": {"restored-exact": 1, "failed": 1},
        },
        state="reconciliation-required",
    )
    with patch.object(session_cli.requests, "post", return_value=_response(record)):
        result = CliRunner().invoke(
            session_cli.session, ["cohort", "resume-start", SESSION, "--by", "colin"]
        )

    assert result.exit_code == 0, result.output
    assert "NOT FINISHED" in result.output
    assert "wake did not land" in result.output
    # And it prints the exact command that continues it, so nobody reaches
    # for force-Stop to escape a fleet that is already back.
    assert "cao session cohort resume-retry" in result.output
    assert "--operation 11111111-1111-4111-8111-111111111111" in result.output
    # The decided failure is still reported alongside.
    assert "did not come back" in result.output


def test_retry_names_the_operation_it_continues_and_mints_nothing():
    posted = {}

    def _post(url, json=None, **_kwargs):
        posted["url"] = url
        posted["json"] = json
        return _response(_operation())

    with patch.object(session_cli.requests, "post", side_effect=_post):
        result = CliRunner().invoke(
            session_cli.session,
            [
                "cohort",
                "resume-retry",
                SESSION,
                "--operation",
                "11111111-1111-4111-8111-111111111111",
                "--by",
                "colin",
            ],
        )

    assert result.exit_code == 0, result.output
    assert posted["url"].endswith(f"/sessions/{SESSION}/cohort/resume/retry")
    assert posted["json"]["operation_id"] == "11111111-1111-4111-8111-111111111111"
    assert posted["json"]["initiated_by"] == "colin"


def test_retry_requires_the_operation_id():
    result = CliRunner().invoke(
        session_cli.session, ["cohort", "resume-retry", SESSION, "--by", "colin"]
    )

    assert result.exit_code != 0
    assert "--operation" in result.output


def test_a_completed_retry_is_shown_with_who_did_it():
    record = _operation(
        provenance={
            "retries": [
                {
                    "transition_id": "t1",
                    "from_state_epoch": 2,
                    "actor": "colin",
                    "reason": None,
                    "receipt_digest": "ab" * 32,
                    "created_at": "2026-08-15T01:00:00Z",
                }
            ],
        }
    )
    with patch.object(session_cli.requests, "get", return_value=_response(record)):
        result = CliRunner().invoke(session_cli.session, ["cohort", "show", record["operation_id"]])

    assert result.exit_code == 0, result.output
    assert "retried by colin" in result.output
    assert "NOT FINISHED" not in result.output


def test_resume_paused_help_promises_zero_input():
    result = CliRunner().invoke(session_cli.session, ["cohort", "resume-paused", "--help"])

    assert result.exit_code == 0
    assert "Sends nothing" in result.output
