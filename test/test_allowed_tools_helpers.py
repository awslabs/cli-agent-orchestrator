"""Non-live coverage for allowed-tools E2E helpers."""

from test.e2e.test_allowed_tools import _terminal_tmux_target

import pytest


def test_terminal_tmux_target_uses_terminal_api_name_field() -> None:
    terminal_api_response = {
        "id": "terminal-id",
        "session_name": "e2e-session",
        "name": "agent-window",
    }

    assert _terminal_tmux_target(terminal_api_response) == "e2e-session:agent-window"


def test_terminal_tmux_target_rejects_incomplete_api_response() -> None:
    with pytest.raises(ValueError, match="session_name or name"):
        _terminal_tmux_target({"session_name": "e2e-session"})
