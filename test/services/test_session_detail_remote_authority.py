"""Session enumeration and detail read from the same placement authority (#745).

Stream Q, #802:

- Q2: ``_remote_sessions`` builds the remote listing from ONE registry snapshot
  (``live_remote_bindings``) rather than ``remote_terminal_ids`` followed by a
  per-row ``runtime_for_terminal``.
- Q3 (haofeif #10): ``get_session`` treats a session present only in remote
  bindings as existing, and reads every terminal's status through
  ``effective_status`` — the same authority ``list_sessions`` uses — so a
  remote-only session's detail no longer 404s and a hybrid same-name session
  reports the runtime's status rather than local UNKNOWN.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import session_service


class TestRemoteSessionsSingleSnapshot:
    def test_uses_live_remote_bindings_not_per_row_runtime_for_terminal(self):
        registry = MagicMock()
        registry.live_remote_bindings.return_value = [("term-r", "worker-1")]
        with (
            patch.object(session_service, "runtime_registry", registry),
            patch.object(
                session_service,
                "list_terminals_by_ids",
                return_value=[{"id": "term-r", "tmux_session": "cao-remote"}],
            ),
        ):
            result = session_service._remote_sessions(set())

        registry.live_remote_bindings.assert_called_once_with()
        # The per-row second observation is gone.
        registry.runtime_for_terminal.assert_not_called()
        registry.remote_terminal_ids.assert_not_called()
        assert result == [
            {
                "id": "cao-remote",
                "name": "cao-remote",
                "status": "detached",
                "runtimes": ["worker-1"],
            }
        ]


class TestGetSessionRemoteAuthority:
    @patch("cli_agent_orchestrator.utils.terminal.effective_status")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_remote_only_session_detail_returns_terminals_with_runtime_status(
        self, mock_get_backend, mock_list_by_ids, mock_list_by_session, mock_effective
    ):
        # No local tmux session of this name, but a live remote binding places it.
        mock_get_backend.return_value.session_exists.return_value = False
        mock_get_backend.return_value.list_sessions.return_value = []
        mock_list_by_ids.return_value = [{"id": "term-r", "tmux_session": "cao-remote"}]
        mock_list_by_session.return_value = [{"id": "term-r", "tmux_session": "cao-remote"}]
        mock_effective.return_value = TerminalStatus.COMPLETED

        registry = MagicMock()
        registry.live_remote_bindings.return_value = [("term-r", "worker-1")]
        with patch.object(session_service, "runtime_registry", registry):
            result = session_service.get_session("cao-remote")

        assert result["session"]["id"] == "cao-remote"
        assert result["terminals"][0]["status"] == "completed"

    @patch("cli_agent_orchestrator.services.status_monitor.status_monitor.get_status")
    @patch("cli_agent_orchestrator.utils.terminal.effective_status")
    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_session")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_hybrid_same_name_session_reports_runtime_status_for_remote_terminal(
        self, mock_get_backend, mock_list_by_session, mock_effective, mock_local_status
    ):
        # A local tmux session shares the name; one of its terminals is remote.
        mock_get_backend.return_value.session_exists.return_value = True
        mock_get_backend.return_value.list_sessions.return_value = [{"id": "cao-hybrid"}]
        mock_list_by_session.return_value = [
            {"id": "term-local", "tmux_session": "cao-hybrid"},
            {"id": "term-remote", "tmux_session": "cao-hybrid"},
        ]
        # Pre-fix path would read the local monitor and report UNKNOWN for the
        # remote terminal; pin it so the assertion below is about the authority,
        # not about an unmocked backend call.
        mock_local_status.return_value = TerminalStatus.UNKNOWN
        mock_effective.side_effect = lambda tid: {
            "term-local": TerminalStatus.IDLE,
            "term-remote": TerminalStatus.COMPLETED,
        }[tid]

        result = session_service.get_session("cao-hybrid")

        statuses = {t["id"]: t["status"] for t in result["terminals"]}
        assert statuses["term-remote"] == "completed"
        assert statuses["term-local"] == "idle"

    @patch("cli_agent_orchestrator.services.session_service.list_terminals_by_ids")
    @patch("cli_agent_orchestrator.services.session_service.get_backend")
    def test_unknown_session_still_not_found(self, mock_get_backend, mock_list_by_ids):
        mock_get_backend.return_value.session_exists.return_value = False
        mock_get_backend.return_value.list_sessions.return_value = []
        mock_list_by_ids.return_value = []
        registry = MagicMock()
        registry.live_remote_bindings.return_value = []
        with patch.object(session_service, "runtime_registry", registry):
            with pytest.raises(ValueError, match="not found"):
                session_service.get_session("cao-nope")
