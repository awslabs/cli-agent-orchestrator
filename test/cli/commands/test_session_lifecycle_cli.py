"""`cao session` lifecycle verbs.

Over HTTP like the rest of the group, unlike `cao issue` and
`cao attachment` which call their services directly. The reason differs:
those exist to be usable when the server is the broken thing, whereas
declaring a session paused is meaningless without a server — the
supervisor that has to settle the fleet lives behind it.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands import session as session_cli

SESSION = "cao-p1-closure"


def _response(payload: dict, status: int = 200) -> MagicMock:
    response = MagicMock()
    response.status_code = status
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


class TestStopShowsTheCostBeforeAsking:
    """A command called stop must never silently be a delete."""

    IMPACT = {
        "live_workers": 3,
        "not_resumable": [
            {
                "terminal_id": "a1b2c3d4",
                "provider": "claude_code",
                "agent_profile": "developer",
                "reason": "no-recorded-native-session-id",
            },
            {
                "terminal_id": "e5f6a7b8",
                "provider": "muse_cli",
                "agent_profile": "reviewer",
                "reason": "provider-has-no-resume-path",
            },
        ],
        "resumable": [],
        "resume_machinery_available": False,
        "resume_machinery_reason": "no provider has a working resume path yet",
        "one_way_for_every_worker": True,
    }

    def test_it_names_every_worker_that_will_not_come_back(self):
        with patch.object(session_cli.requests, "get", return_value=_response(self.IMPACT)):
            result = CliRunner().invoke(session_cli.session, ["stop-impact", SESSION])
        assert result.exit_code == 0
        assert "will NOT come back" in result.output
        assert "no-recorded-native-session-id" in result.output
        assert "provider-has-no-resume-path" in result.output
        assert "no provider has a working resume path yet" in result.output

    def test_declining_the_confirmation_records_nothing(self):
        with (
            patch.object(session_cli.requests, "get", return_value=_response(self.IMPACT)),
            patch.object(session_cli.requests, "post") as post,
        ):
            result = CliRunner().invoke(
                session_cli.session, ["stop", SESSION, "--by", "colin"], input="n\n"
            )
        assert result.exit_code != 0
        post.assert_not_called()

    def test_confirming_acknowledges_the_one_way_door(self):
        with (
            patch.object(session_cli.requests, "get", return_value=_response(self.IMPACT)),
            patch.object(
                session_cli.requests,
                "post",
                return_value=_response(
                    {"session_name": SESSION, "lifecycle": "stopped", "restore_to": "working"}
                ),
            ) as post,
        ):
            result = CliRunner().invoke(
                session_cli.session, ["stop", SESSION, "--by", "colin", "--yes"]
            )
        assert result.exit_code == 0
        assert post.call_args.kwargs["json"]["acknowledged_one_way"] is True

    def test_the_impact_is_shown_before_the_prompt_not_after(self):
        """An operator asked to confirm must already have read the cost."""
        with (
            patch.object(session_cli.requests, "get", return_value=_response(self.IMPACT)),
            patch.object(session_cli.requests, "post"),
        ):
            result = CliRunner().invoke(
                session_cli.session, ["stop", SESSION, "--by", "colin"], input="n\n"
            )
        assert result.output.index("will NOT come back") < result.output.index("collect")

    def test_help_states_collection_preservation_and_one_way(self):
        """Stop snapshots/collects panes, preserves the row/env/artifacts, and is
        currently one-way — not a declaration-only record."""
        result = CliRunner().invoke(session_cli.session, ["stop", "--help"])
        assert result.exit_code == 0
        help_text = result.output.lower()
        assert "collect" in help_text and "snapshot" in help_text
        assert "preserved" in help_text or "preserves" in help_text
        assert "one-way" in help_text or "resume is not implemented" in help_text

    def test_the_confirmation_asks_to_collect_not_just_record(self):
        with (
            patch.object(session_cli.requests, "get", return_value=_response(self.IMPACT)),
            patch.object(session_cli.requests, "post"),
        ):
            result = CliRunner().invoke(
                session_cli.session, ["stop", SESSION, "--by", "colin"], input="n\n"
            )
        assert "collect" in result.output.lower()


class TestThePauseVerbs:
    def test_requesting_a_pause_says_it_is_not_yet_paused(self):
        with patch.object(
            session_cli.requests,
            "post",
            return_value=_response(
                {"session_name": SESSION, "lifecycle": "pausing", "kind": "campaign"}
            ),
        ):
            result = CliRunner().invoke(session_cli.session, ["pause", SESSION, "--by", "colin"])
        assert result.exit_code == 0
        assert "waiting for the supervisor" in result.output

    def test_settling_is_a_separate_verb(self):
        with patch.object(
            session_cli.requests,
            "post",
            return_value=_response(
                {"session_name": SESSION, "lifecycle": "paused", "kind": "campaign"}
            ),
        ) as post:
            result = CliRunner().invoke(
                session_cli.session, ["pause-settled", SESSION, "--by", "supervisor-terra"]
            )
        assert result.exit_code == 0
        assert post.call_args.kwargs["json"]["declared_by"] == "supervisor-terra"

    def test_every_verb_requires_a_name(self):
        for argv in (
            ["complete", SESSION],
            ["pause", SESSION],
            ["pause-settled", SESSION],
            ["stop", SESSION],
        ):
            result = CliRunner().invoke(session_cli.session, argv)
            assert result.exit_code != 0, argv
            assert "--by" in result.output, argv


class TestReporting:
    def test_a_suppressing_session_says_so(self):
        record = {
            "session_name": SESSION,
            "lifecycle": "complete",
            "kind": "campaign",
            "declared_by": "supervisor-terra",
            "suppresses_marshal": True,
        }
        with patch.object(session_cli.requests, "get", return_value=_response(record)):
            result = CliRunner().invoke(session_cli.session, ["lifecycle", SESSION])
        assert "the fire marshal will not fire" in result.output

    def test_an_overdue_pause_is_called_out(self):
        record = {
            "session_name": SESSION,
            "lifecycle": "pausing",
            "kind": "campaign",
            "pause_deadline_at": "2026-08-07T00:00:00Z",
            "pause_overdue": True,
            "suppresses_marshal": False,
        }
        with patch.object(session_cli.requests, "get", return_value=_response(record)):
            result = CliRunner().invoke(session_cli.session, ["lifecycle", SESSION])
        assert "OVERDUE" in result.output
        assert "will not fire" not in result.output

    def test_a_refusal_surfaces_the_servers_reason(self):
        with patch.object(
            session_cli.requests,
            "post",
            return_value=_response({"detail": "session is 'complete'; no running fleet"}, 409),
        ):
            result = CliRunner().invoke(session_cli.session, ["pause", SESSION, "--by", "colin"])
        assert result.exit_code != 0
        assert "no running fleet" in result.output
