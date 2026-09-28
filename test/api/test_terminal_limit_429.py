"""API-boundary mapping tests for TerminalLimitError -> HTTP 429.

The per-node cap (CAO_MAX_TERMINALS, one-agent-per-pod topology) is raised by
``terminal_service.create_terminal``; each of the three creation endpoints must
surface it as 429 Too Many Requests — a capacity rejection the caller should
retry on another node — never a 400/404/500.
"""

from unittest.mock import AsyncMock, patch

from cli_agent_orchestrator.models.terminal import TerminalLimitError

_LIMIT_ERROR = TerminalLimitError(
    "Terminal limit reached: this node already has 1 tracked terminal(s) and "
    "CAO_MAX_TERMINALS/server.max_terminals is 1. Delete a terminal or target "
    "a different node."
)


class TestTerminalLimitMapsTo429:
    def test_create_session_returns_429(self, client):
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(side_effect=_LIMIT_ERROR)
            response = client.post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
            )
        assert response.status_code == 429
        assert "Terminal limit reached" in response.json()["detail"]

    def test_create_terminal_in_session_returns_429(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal = AsyncMock(side_effect=_LIMIT_ERROR)
            response = client.post(
                "/sessions/cao-test/terminals",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
            )
        assert response.status_code == 429
        assert "Terminal limit reached" in response.json()["detail"]

    def test_run_step_returns_429(self, client):
        with patch(
            "cli_agent_orchestrator.api.main.run_agent_step",
            new=AsyncMock(side_effect=_LIMIT_ERROR),
        ):
            response = client.post(
                "/terminals/run-step",
                json={"provider": "kiro_cli", "agent": "developer", "prompt": "do it"},
            )
        assert response.status_code == 429
        assert "Terminal limit reached" in response.json()["detail"]
