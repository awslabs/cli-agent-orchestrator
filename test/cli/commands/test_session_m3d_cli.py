"""`cao session drain` / `occurrence` / `cohort pause-safe` (M3-D).

Over HTTP like the rest of the `cao session` group. What these tests pin is
what the operator is *shown*: a receipt only when it is spendable, an
unfinished drain that says so and names the way forward, and a safe Pause that
names a drain rather than asking a human to paste a digest.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import session as session_cli

SESSION = "cao-m3d-cli"
DIGEST = "d" * 64


def _response(payload: dict, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _drain(state="complete", **overrides):
    record = {
        "drain_id": "11111111-1111-4111-8111-111111111111",
        "session_name": SESSION,
        "intent": "pause",
        "state": state,
        "attempt": 0,
        "lifecycle_epoch": 3,
        "roster_revision": "r" * 64,
        "receipt_digest": DIGEST if state == "complete" else None,
        "reconciliation_reason": (
            None if state == "complete" else "1 member(s) reached no proven boundary: a1"
        ),
        "members": [
            {
                "agent_id": "a" * 36,
                "terminal_id": "term-1",
                "member_state": (
                    overrides.pop("member_state", None)
                    or ("drained" if state == "complete" else "reconciliation-required")
                ),
                "detail": None if state == "complete" else "still working",
            }
        ],
        "provenance": {},
    }
    record.update(overrides)
    return record


class TestDrain:
    def test_a_complete_drain_prints_the_receipt_and_how_to_spend_it(self):
        with (
            patch.object(session_cli.requests, "post") as post,
            patch.object(session_cli.requests, "get") as get,
        ):
            post.return_value = _response(_drain())
            get.return_value = _response(_drain())
            result = CliRunner().invoke(session_cli.session, ["drain", SESSION, "--by", "colin"])

        assert result.exit_code == 0, result.output
        assert DIGEST in result.output
        assert "cao session cohort pause-safe" in result.output
        assert "--drain 11111111-1111-4111-8111-111111111111" in result.output

    def test_an_unfinished_drain_never_prints_a_receipt_and_names_the_way_forward(self):
        record = _drain(state="reconciliation-required")
        with (
            patch.object(session_cli.requests, "post") as post,
            patch.object(session_cli.requests, "get") as get,
        ):
            post.return_value = _response(record)
            get.return_value = _response(record)
            result = CliRunner().invoke(session_cli.session, ["drain", SESSION, "--by", "colin"])

        assert result.exit_code == 0, result.output
        # An operator must never be handed a token that cannot be spent.
        assert DIGEST not in result.output
        assert "NOT FINISHED" in result.output
        assert "--retry" in result.output
        assert "a drain never promotes itself" in result.output

    def test_the_request_carries_a_minted_id_and_the_intent(self):
        with (
            patch.object(session_cli.requests, "post") as post,
            patch.object(session_cli.requests, "get") as get,
        ):
            post.return_value = _response(_drain(intent="stop"))
            get.return_value = _response(_drain(intent="stop"))
            CliRunner().invoke(
                session_cli.session,
                ["drain", SESSION, "--by", "colin", "--intent", "stop"],
            )

        body = post.call_args.kwargs["json"]
        assert body["intent"] == "stop"
        assert body["retry"] is False
        assert len(body["drain_id"]) == 36

    def test_a_retry_requires_the_drain_it_continues(self):
        result = CliRunner().invoke(
            session_cli.session, ["drain", SESSION, "--by", "colin", "--retry"]
        )

        assert result.exit_code != 0
        assert "pass --drain" in result.output

    def test_a_retry_reuses_the_named_drain_id(self):
        with (
            patch.object(session_cli.requests, "post") as post,
            patch.object(session_cli.requests, "get") as get,
        ):
            post.return_value = _response(_drain())
            get.return_value = _response(_drain())
            CliRunner().invoke(
                session_cli.session,
                [
                    "drain",
                    SESSION,
                    "--by",
                    "colin",
                    "--drain",
                    "11111111-1111-4111-8111-111111111111",
                    "--retry",
                ],
            )

        body = post.call_args.kwargs["json"]
        assert body["drain_id"] == "11111111-1111-4111-8111-111111111111"
        assert body["retry"] is True


class TestSafePause:
    def test_pause_safe_names_a_drain_rather_than_a_digest(self):
        """A digest typed by hand is a receipt spent on an unrelated claim."""
        params = {param.name for param in session_cli.cohort_pause_safe.params}
        assert "drain_id" in params
        assert "drain_receipt_digest" not in params
        assert "members" not in params

    def test_pause_safe_posts_the_drain_to_the_safe_drained_route(self):
        operation = {
            "operation_id": "22222222-2222-4222-8222-222222222222",
            "operation_kind": "pause",
            "current_mode": "safe",
            "state": "paused",
            "provenance": {"session_name": SESSION, "current_mode": "safe"},
        }
        with patch.object(session_cli.requests, "post") as post:
            post.return_value = _response(operation)
            result = CliRunner().invoke(
                session_cli.session,
                [
                    "cohort",
                    "pause-safe",
                    SESSION,
                    "--by",
                    "colin",
                    "--drain",
                    "11111111-1111-4111-8111-111111111111",
                ],
            )

        assert result.exit_code == 0, result.output
        assert post.call_args.args[0].endswith("/cohort/pause/safe-drained")
        assert post.call_args.kwargs["json"]["drain_id"] == ("11111111-1111-4111-8111-111111111111")

    def test_a_refused_safe_pause_surfaces_the_servers_reason(self):
        with patch.object(session_cli.requests, "post") as post:
            post.return_value = _response(
                {"detail": "drain X is 'reconciliation-required' and has no receipt"}, status=409
            )
            result = CliRunner().invoke(
                session_cli.session,
                [
                    "cohort",
                    "pause-safe",
                    SESSION,
                    "--by",
                    "colin",
                    "--drain",
                    "11111111-1111-4111-8111-111111111111",
                ],
            )

        assert result.exit_code != 0
        assert "has no receipt" in result.output


class TestOccurrences:
    RECORD = {
        "task_occurrence_id": "33333333-3333-4333-8333-333333333333",
        "session_name": SESSION,
        "agent_id": "44444444-4444-4444-8444-444444444444",
        "round_index": 2,
        "state": "open",
        "incarnation_id": "inc-9",
        "terminal_id": "term-9",
        "generation": "gen-9",
        "current": {"seed_quality": "truncated"},
        "finalized": {"seed_quality": None},
        "seed_verdict": {
            "family": "current",
            "quality": "truncated",
            "sufficient_for_fresh_start": False,
            "reason": "the seed is truncated: it reads as context while missing part of it",
        },
        "extensions": [
            {
                "extension_id": "claim-1",
                "extension_kind": "future.completion-claim/v9",
                "extension_version": "9",
                "decider": "cao-conductor",
                "routing_state": "pending-decider",
                "claims_final": True,
            }
        ],
    }

    def test_show_labels_the_effect_separately_from_the_task_id(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(self.RECORD)
            result = CliRunner().invoke(
                session_cli.session,
                ["occurrence", "show", "33333333-3333-4333-8333-333333333333"],
            )

        assert result.exit_code == 0, result.output
        assert "33333333-3333-4333-8333-333333333333" in result.output
        assert "incarnation inc-9" in result.output
        assert "terminal term-9" in result.output
        assert "generation gen-9" in result.output

    def test_show_says_out_loud_when_a_seed_cannot_start_a_fresh_worker(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(self.RECORD)
            result = CliRunner().invoke(
                session_cli.session,
                ["occurrence", "show", "33333333-3333-4333-8333-333333333333"],
            )

        assert "NOT enough for a fresh successor" in result.output
        assert "truncated" in result.output

    def test_show_marks_an_extension_that_is_still_waiting_on_its_decider(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(self.RECORD)
            result = CliRunner().invoke(
                session_cli.session,
                ["occurrence", "show", "33333333-3333-4333-8333-333333333333"],
            )

        assert "AWAITING DECIDER" in result.output
        assert "cao-conductor" in result.output

    def test_list_reports_the_round_state_and_seed_quality(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response({"occurrences": [self.RECORD], "count": 1})
            result = CliRunner().invoke(session_cli.session, ["occurrence", "list", SESSION])

        assert "round 2" in result.output
        assert "seed=truncated" in result.output

    def test_pending_extensions_lists_a_future_completion_claim(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(
                {
                    "extensions": [
                        {
                            "task_occurrence_id": "33333333-3333-4333-8333-333333333333",
                            "extension_id": "claim-1",
                            "extension_kind": "future.completion-claim/v9",
                            "extension_version": "9",
                            "decider": "cao-conductor",
                            "claims_final": True,
                        }
                    ],
                    "count": 1,
                }
            )
            result = CliRunner().invoke(
                session_cli.session, ["occurrence", "pending-extensions", SESSION]
            )

        assert "claims final" in result.output
        assert "cao-conductor" in result.output


class TestWakes:
    def test_an_undelivered_wake_is_never_reported_as_a_supervisor_that_was_told(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(
                {
                    "wakes": [
                        {
                            "wake_id": "55555555-5555-4555-8555-555555555555",
                            "source_kind": "resume-and-start",
                            "source_operation_id": "66666666-6666-4666-8666-666666666666",
                            "delivery_state": "undelivered",
                            "reason_code": "supervisor-pane-absent",
                            "detail": "",
                            "message": {"text": "[CAO reconcile] ..."},
                        }
                    ],
                    "count": 1,
                }
            )
            result = CliRunner().invoke(session_cli.session, ["cohort", "wakes", SESSION])

        assert "NOT TOLD" in result.output
        assert "supervisor-pane-absent" in result.output

    def test_a_delivered_wake_shows_the_exact_message(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(
                {
                    "wakes": [
                        {
                            "wake_id": "55555555-5555-4555-8555-555555555555",
                            "source_kind": "resume-and-start",
                            "source_operation_id": "66666666-6666-4666-8666-666666666666",
                            "delivery_state": "delivered",
                            "reason_code": None,
                            "detail": "",
                            "message": {"text": "[CAO reconcile 6666] exact=2 failed=1."},
                        }
                    ],
                    "count": 1,
                }
            )
            result = CliRunner().invoke(session_cli.session, ["cohort", "wakes", SESSION])

        assert "NOT TOLD" not in result.output
        assert "exact=2 failed=1" in result.output


class TestDrainRenderingMatchesIntent:
    """A drain proves one boundary for one intent (cond-0380 P1-3).

    A Pause drain steers workers to a boundary and announces no teardown; a
    Stop drain additionally records CAO's teardown before the panes go. So a
    Pause receipt is not Stop evidence, and printing the Stop command under a
    Pause drain invites an operator to spend it as though it were.
    """

    def _render(self, record):
        with (
            patch.object(session_cli.requests, "post") as post,
            patch.object(session_cli.requests, "get") as get,
        ):
            post.return_value = _response(record)
            get.return_value = _response(record)
            return CliRunner().invoke(
                session_cli.session,
                ["drain", SESSION, "--by", "colin", "--intent", record["intent"]],
            )

    def test_a_pause_drain_offers_only_the_pause_command(self):
        result = self._render(_drain(intent="pause"))

        assert result.exit_code == 0, result.output
        assert "cao session cohort pause-safe" in result.output
        assert "stop-safe" not in result.output

    def test_a_stop_drain_offers_only_the_stop_command(self):
        result = self._render(_drain(intent="stop", member_state="drained"))

        assert result.exit_code == 0, result.output
        assert "cao session cohort stop-safe" in result.output
        assert "pause-safe" not in result.output


class TestSafeStopSpendsAStopDrain:
    def test_stop_safe_resolves_a_named_drain_to_its_receipt(self):
        posted = {}

        def _post(url, json=None, **_kwargs):
            posted["url"] = url
            posted["json"] = json
            return _response({"operation_id": "op", "state": "stopped", "provenance": {}})

        with (
            patch.object(session_cli.requests, "post", side_effect=_post),
            patch.object(session_cli.requests, "get") as get,
        ):
            get.return_value = _response(_drain(intent="stop"))
            result = CliRunner().invoke(
                session_cli.session,
                [
                    "cohort",
                    "stop-safe",
                    SESSION,
                    "--by",
                    "colin",
                    "--drain",
                    "11111111-1111-4111-8111-111111111111",
                    "--yes",
                ],
            )

        assert result.exit_code == 0, result.output
        assert posted["json"]["drain_receipt_digest"] == DIGEST

    def test_stop_safe_refuses_a_drain_that_proved_no_boundary(self):
        with patch.object(session_cli.requests, "get") as get:
            get.return_value = _response(_drain(state="reconciliation-required", intent="stop"))
            result = CliRunner().invoke(
                session_cli.session,
                [
                    "cohort",
                    "stop-safe",
                    SESSION,
                    "--by",
                    "colin",
                    "--drain",
                    "11111111-1111-4111-8111-111111111111",
                    "--yes",
                ],
            )

        assert result.exit_code != 0
        assert "no receipt" in result.output

    def test_stop_safe_still_needs_some_evidence(self):
        result = CliRunner().invoke(
            session_cli.session, ["cohort", "stop-safe", SESSION, "--by", "colin", "--yes"]
        )

        assert result.exit_code != 0
        assert "stop drain" in result.output
