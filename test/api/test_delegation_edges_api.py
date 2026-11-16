"""API wiring for delegation edges.

Terminal IDs are 8 lowercase hex characters (TerminalId), so the fixtures use
hex IDs rather than short names.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from cli_agent_orchestrator.models.delegation import EdgeSource
from cli_agent_orchestrator.models.terminal import Terminal

MAIN = "cli_agent_orchestrator.api.main"
CALLER = "a1b2c3d4"
WORKER = "aa11bb22"
RECEIVER = "bb22cc33"


def _terminal() -> Terminal:
    return Terminal(id=WORKER, name="developer", provider="claude_code", session_name="cao-s")


def test_create_terminal_with_expect_callback_opens_edge(client):
    terminal = _terminal()
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=terminal)),
        patch(f"{MAIN}.create_delegation_edge") as create_edge,
        patch(f"{MAIN}.find_any_edge", return_value=None),
    ):
        client.post(
            "/sessions/cao-s/terminals",
            params={
                "agent_profile": "developer",
                "provider": "claude_code",
                "caller_id": CALLER,
                "expect_callback": "true",
            },
        )
    create_edge.assert_called_once_with(CALLER, WORKER, EdgeSource.ASSIGN)


def test_create_terminal_default_opens_no_edge(client):
    terminal = _terminal()
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=terminal)),
        patch(f"{MAIN}.create_delegation_edge") as create_edge,
        patch(f"{MAIN}.find_any_edge", return_value=None),
    ):
        client.post(
            "/sessions/cao-s/terminals",
            params={"agent_profile": "developer", "provider": "claude_code", "caller_id": CALLER},
        )
    create_edge.assert_not_called()


def test_create_terminal_replay_does_not_duplicate_edge(client):
    terminal = _terminal()
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=terminal)),
        patch(f"{MAIN}.create_delegation_edge") as create_edge,
        patch(f"{MAIN}.find_any_edge", return_value=MagicMock()),
    ):
        client.post(
            "/sessions/cao-s/terminals",
            params={
                "agent_profile": "developer",
                "provider": "claude_code",
                "caller_id": CALLER,
                "expect_callback": "true",
            },
        )
    create_edge.assert_not_called()


def test_inbox_post_applies_edge_rules(client):
    msg = MagicMock(id=42, sender_id=CALLER, receiver_id=RECEIVER)
    msg.created_at.isoformat.return_value = "2026-11-06T00:00:00"
    with (
        patch(f"{MAIN}.create_inbox_message", return_value=msg),
        patch(f"{MAIN}.inbox_service"),
        patch(f"{MAIN}.delegation_callback_service.on_send_message") as rules,
    ):
        client.post(
            f"/terminals/{RECEIVER}/inbox/messages",
            params={"sender_id": CALLER, "message": "next step", "expect_reply": "true"},
        )
    rules.assert_called_once_with(
        sender_id=CALLER, receiver_id=RECEIVER, message_id=42, expect_reply=True
    )
