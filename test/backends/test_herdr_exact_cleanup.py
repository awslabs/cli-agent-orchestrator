"""Exact-identity teardown for herdr: the snapshot's terminal_id decides.

Herdr offers no atomic compare-and-close, so the contract is deliberately
conservative: any snapshot that cannot be trusted, any terminal id that resolves
to more than one pane, and any close the next fresh snapshot cannot confirm all
answer UNKNOWN or STILL_PRESENT rather than success. These tests pin that, and
that the workspace/tab LABEL is never used as a fallback -- labels are reusable
and a tombstone shares them with its replacement.
"""

import json
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend


@pytest.fixture
def backend():
    # __new__ skips __init__ (which probes for the herdr binary); only the
    # snapshot path and JSON parsing are under test here.
    return HerdrBackend.__new__(HerdrBackend)


def _completed(stdout="", returncode=0):
    mock = MagicMock()
    mock.stdout = stdout
    mock.returncode = returncode
    mock.stderr = ""
    return mock


def _snapshot(*panes, tabs=(), workspaces=()):
    return _completed(
        json.dumps(
            {
                "id": "cli:api:snapshot",
                "result": {
                    "snapshot": {
                        "panes": list(panes),
                        "tabs": list(tabs),
                        "workspaces": list(workspaces),
                    }
                },
            }
        )
    )


TARGET = {"pane_id": "w1:p1", "terminal_id": "tid-1", "tab_id": "w1:t1", "workspace_id": "w1"}


class TestAbsence:
    def test_absent_when_a_fresh_snapshot_has_no_such_terminal_id(self, backend):
        other = {"pane_id": "w1:p2", "terminal_id": "tid-other"}
        with patch.object(backend, "_run_herdr", return_value=_snapshot(other)) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.ABSENT
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_absent_when_the_snapshot_has_no_panes_at_all(self, backend):
        with patch.object(backend, "_run_herdr", return_value=_snapshot()):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.ABSENT

    def test_the_label_is_never_used_as_a_fallback(self, backend):
        """A replacement holds the old labels; the snapshot decides, not the name."""
        replacement = {
            "pane_id": "w1:p2",
            "terminal_id": "tid-new",
            "tab_id": "w1:t1",
            "workspace_id": "w1",
        }
        with patch.object(backend, "_run_herdr", return_value=_snapshot(replacement)) as run:
            result = backend.cleanup_terminal_exact("tid-old", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.ABSENT
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]


class TestUnknown:
    def test_a_failed_snapshot_is_unknown(self, backend):
        with patch.object(backend, "_run_herdr", return_value=_completed("", returncode=1)):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN

    def test_a_raising_snapshot_is_unknown(self, backend):
        with patch.object(backend, "_run_herdr", side_effect=OSError("herdr CLI not found")):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN

    @pytest.mark.parametrize(
        "payload",
        [
            "not json at all",
            json.dumps({"result": "weird"}),
            json.dumps({"result": {"snapshot": {"panes": "nope"}}}),
            json.dumps({"result": {"snapshot": {"panes": ["not-a-dict"]}}}),
            json.dumps({"result": {"snapshot": "not-a-dict"}}),
        ],
    )
    def test_a_malformed_snapshot_is_unknown(self, backend, payload):
        with patch.object(backend, "_run_herdr", return_value=_completed(payload)) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_duplicate_terminal_ids_are_unknown_and_close_nothing(self, backend):
        panes = [
            {"pane_id": "w1:p1", "terminal_id": "tid-1"},
            {"pane_id": "w1:p2", "terminal_id": "tid-1"},
        ]
        with patch.object(backend, "_run_herdr", return_value=_snapshot(*panes)) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_a_match_with_no_usable_pane_id_is_unknown(self, backend):
        with patch.object(
            backend, "_run_herdr", return_value=_snapshot({"terminal_id": "tid-1"})
        ) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_a_non_string_terminal_id_is_unknown(self, backend):
        """An id this code cannot even compare must not read as absence."""
        with patch.object(
            backend, "_run_herdr", return_value=_snapshot({"pane_id": "w1:p1", "terminal_id": 7})
        ) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_a_pane_without_a_terminal_id_is_not_an_error(self, backend):
        """Foreign panes are ordinary; their presence does not block absence."""
        panes = [{"pane_id": "w1:p1"}, {"pane_id": "w1:p2", "terminal_id": "tid-other"}]
        with patch.object(backend, "_run_herdr", return_value=_snapshot(*panes)):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.ABSENT

    def test_an_unconfirmable_post_close_snapshot_is_unknown(self, backend):
        responses = [_snapshot(TARGET), _completed(), _completed("", returncode=1)]
        with patch.object(backend, "_run_herdr", side_effect=responses):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN


class TestClose:
    def test_a_confirmed_close_is_deleted(self, backend):
        responses = [_snapshot(TARGET), _completed(), _snapshot({"pane_id": "w1:p2"})]
        with patch.object(backend, "_run_herdr", side_effect=responses) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.DELETED
        assert run.call_args_list[1] == call(["pane", "close", "w1:p1"], check=False)

    def test_a_close_the_next_snapshot_contradicts_is_still_present(self, backend):
        responses = [_snapshot(TARGET), _completed(), _snapshot(TARGET)]
        with patch.object(backend, "_run_herdr", side_effect=responses) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.STILL_PRESENT
        assert run.call_args_list[1] == call(["pane", "close", "w1:p1"], check=False)

    def test_a_failing_close_that_leaves_the_pane_is_still_present(self, backend):
        responses = [_snapshot(TARGET), _completed("", returncode=1), _snapshot(TARGET)]
        with patch.object(backend, "_run_herdr", side_effect=responses):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.STILL_PRESENT

    def test_a_failing_close_that_really_removed_it_is_deleted(self, backend):
        responses = [_snapshot(TARGET), _completed("", returncode=1), _snapshot()]
        with patch.object(backend, "_run_herdr", side_effect=responses):
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1")

        assert result.outcome is TerminalCleanupOutcome.DELETED

    def test_proof_only_reports_still_present_without_closing(self, backend):
        with patch.object(backend, "_run_herdr", return_value=_snapshot(TARGET)) as run:
            result = backend.cleanup_terminal_exact("tid-1", "sess-a", "win-1", close=False)

        assert result.outcome is TerminalCleanupOutcome.STILL_PRESENT
        assert run.call_args_list == [call(["api", "snapshot"], check=False)]

    def test_no_terminal_id_is_unknown(self, backend):
        with patch.object(backend, "_run_herdr", return_value=_snapshot(TARGET)) as run:
            result = backend.cleanup_terminal_exact("")

        assert result.outcome is TerminalCleanupOutcome.UNKNOWN
        assert run.call_args_list == []
