"""An omitted working directory is inherited, not forwarded as None (#802).

`assign`/`handoff` through the shared MCP path resolve an omitted cwd via
`GET /terminals/{caller}/working-directory`, and that handler asked the central
server's LOCAL tmux. On a control-plane server there is no tmux, so it failed, the
value reached the remote launch as `None`, and the bridge fell back to its own
process cwd — starting the worker outside the caller's checkout, where
`use_worktree` can select the wrong repository (Copilot review on #802).

The launch-time cwd is already persisted on the terminal row. That is the trusted
value here: the server recorded it when it placed the terminal and no agent can
rewrite it.
"""

from unittest.mock import MagicMock, patch

import pytest


class TestTheCwdLookupIsRemoteAware:
    def test_a_remote_terminal_uses_the_recorded_directory(self):
        """Probing local tmux for a remote pane is the bug, not the fallback."""
        from cli_agent_orchestrator.services import terminal_service

        row = {
            "tmux_session": "cao-abcd1234",
            "tmux_window": "w",
            "working_directory": "/home/cao/checkout",
        }
        with (
            patch.object(terminal_service, "get_terminal_metadata", return_value=row),
            patch(
                "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
                return_value=True,
            ),
            patch.object(terminal_service, "get_backend") as backend,
        ):
            assert terminal_service.get_working_directory("abcd1234") == "/home/cao/checkout"
            backend.assert_not_called()

    def test_a_local_terminal_still_prefers_the_live_pane(self):
        """The agent may have cd'd, so a live answer wins when it is available."""
        from cli_agent_orchestrator.services import terminal_service

        row = {
            "tmux_session": "cao-abcd1234",
            "tmux_window": "w",
            "working_directory": "/launch/dir",
        }
        be = MagicMock()
        be.get_pane_working_directory.return_value = "/where/the/agent/went"
        with (
            patch.object(terminal_service, "get_terminal_metadata", return_value=row),
            patch(
                "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
                return_value=False,
            ),
            patch.object(terminal_service, "get_backend", return_value=be),
        ):
            assert terminal_service.get_working_directory("abcd1234") == "/where/the/agent/went"

    def test_a_backend_failure_falls_back_rather_than_answering_none(self):
        """ "No directory" is the answer that produced the bug downstream."""
        from cli_agent_orchestrator.services import terminal_service

        row = {
            "tmux_session": "cao-abcd1234",
            "tmux_window": "w",
            "working_directory": "/launch/dir",
        }
        be = MagicMock()
        be.get_pane_working_directory.side_effect = RuntimeError("no tmux server running")
        with (
            patch.object(terminal_service, "get_terminal_metadata", return_value=row),
            patch(
                "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
                return_value=False,
            ),
            patch.object(terminal_service, "get_backend", return_value=be),
        ):
            assert terminal_service.get_working_directory("abcd1234") == "/launch/dir"


class TestAFalsyBackendAnswerFallsBackToo:
    """TmuxClient RETURNS None on failure; it does not raise.

    Guarding only the exception left the fallback unreachable for the default
    backend, so the caller still got None — the outcome that made an assign forward
    no working directory at all (own review of this PR).
    """

    def test_a_none_return_uses_the_recorded_directory(self):
        from unittest.mock import MagicMock, patch

        from cli_agent_orchestrator.services import terminal_service

        row = {
            "tmux_session": "cao-abcd1234",
            "tmux_window": "w",
            "working_directory": "/launch/dir",
        }
        be = MagicMock()
        be.get_pane_working_directory.return_value = None  # tmux's real failure mode
        with (
            patch.object(terminal_service, "get_terminal_metadata", return_value=row),
            patch(
                "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
                return_value=False,
            ),
            patch.object(terminal_service, "get_backend", return_value=be),
        ):
            assert terminal_service.get_working_directory("abcd1234") == "/launch/dir"

    def test_an_empty_string_return_also_falls_back(self):
        from unittest.mock import MagicMock, patch

        from cli_agent_orchestrator.services import terminal_service

        row = {"tmux_session": "s", "tmux_window": "w", "working_directory": "/launch/dir"}
        be = MagicMock()
        be.get_pane_working_directory.return_value = ""
        with (
            patch.object(terminal_service, "get_terminal_metadata", return_value=row),
            patch(
                "cli_agent_orchestrator.runtime_channel.registry.runtime_registry.is_remote",
                return_value=False,
            ),
            patch.object(terminal_service, "get_backend", return_value=be),
        ):
            assert terminal_service.get_working_directory("abcd1234") == "/launch/dir"
