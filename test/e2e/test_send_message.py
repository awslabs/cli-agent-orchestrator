"""End-to-end send_message (inbox delivery) tests for all providers.

Tests the send_message / inbox flow:
1. Create two terminals (sender and receiver) in the same session
2. Wait for both to reach IDLE
3. Send message from sender to receiver's inbox via API
4. Verify message appears in receiver's inbox
5. Verify receiver processes the message (status transitions)
6. Cleanup

Requires: running CAO server, authenticated CLI tools, tmux.

Run:
    uv run pytest -m e2e test/e2e/test_send_message.py -v
"""

import time
import uuid
from test.e2e.conftest import (
    cleanup_terminal,
    create_terminal,
    get_terminal_status,
    wait_for_status,
)

import pytest
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


def _create_terminal_in_session(session_name: str, provider: str, agent_profile: str):
    """Create a terminal in an existing session.

    Returns (terminal_id, window_name).
    """
    resp = requests.post(
        f"{API_BASE_URL}/sessions/{session_name}/terminals",
        params={
            "provider": provider,
            "agent_profile": agent_profile,
        },
    )
    assert resp.status_code in (
        200,
        201,
    ), f"Terminal creation in session failed: {resp.status_code} {resp.text}"
    data = resp.json()
    return data["id"]


def _send_inbox_message(sender_id: str, receiver_id: str, message: str):
    """Send a message to a terminal's inbox via the API."""
    resp = requests.post(
        f"{API_BASE_URL}/terminals/{receiver_id}/inbox/messages",
        params={"sender_id": sender_id, "message": message},
    )
    assert resp.status_code == 200, f"Inbox message send failed: {resp.status_code} {resp.text}"
    return resp.json()


def _get_inbox_messages(terminal_id: str, status_filter: str = None):
    """Get inbox messages for a terminal."""
    params = {"limit": 50}
    if status_filter:
        params["status"] = status_filter
    resp = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/inbox/messages",
        params=params,
    )
    assert resp.status_code == 200, f"Get inbox messages failed: {resp.status_code} {resp.text}"
    return resp.json()


def _run_send_message_test(provider: str, agent_profile: str):
    """Core send_message test: create two terminals, send message via inbox.

    Tests:
    - Message is created in receiver's inbox
    - Message has correct sender_id
    - Message content is preserved
    """
    session_suffix = uuid.uuid4().hex[:6]
    session_name = f"e2e-sendmsg-{provider}-{session_suffix}"
    sender_id = None
    receiver_id = None
    actual_session = None

    try:
        # Step 1: Create first terminal (acts as sender / supervisor)
        sender_id, actual_session = create_terminal(provider, agent_profile, session_name)
        assert sender_id, "Sender terminal ID should not be empty"

        # Step 2: Wait for sender to reach IDLE
        assert wait_for_status(
            sender_id, "idle", timeout=90.0
        ), f"Sender terminal did not reach IDLE within 90s (provider={provider})"

        # Step 3: Create second terminal in the same session (acts as receiver)
        receiver_id = _create_terminal_in_session(actual_session, provider, agent_profile)
        assert receiver_id, "Receiver terminal ID should not be empty"

        # Step 4: Wait for receiver to reach IDLE
        assert wait_for_status(
            receiver_id, "idle", timeout=90.0
        ), f"Receiver terminal did not reach IDLE within 90s (provider={provider})"

        # Step 5: Send message from sender to receiver's inbox
        test_message = f"E2E test message from {sender_id} at {time.time()}"
        result = _send_inbox_message(sender_id, receiver_id, test_message)
        assert result.get("message_id"), "Message should have an ID"
        assert result.get("sender_id") == sender_id, "Sender ID should match"
        assert result.get("receiver_id") == receiver_id, "Receiver ID should match"

        # Step 6: Verify message appears in receiver's inbox
        # Give the inbox service a moment to process
        time.sleep(3)
        messages = _get_inbox_messages(receiver_id)
        assert len(messages) > 0, "Receiver should have at least one inbox message"

        # Find our message
        found = False
        for msg in messages:
            if msg.get("sender_id") == sender_id and test_message in msg.get("message", ""):
                found = True
                break
        assert found, (
            f"Test message not found in receiver's inbox. "
            f"Messages: {[m.get('message', '')[:50] for m in messages]}"
        )

        # Step 7: Verify receiver processes the message (should transition from IDLE)
        # After inbox delivery, the receiver gets the message as input.
        # Wait briefly and check that the receiver is no longer idle.
        # Acceptable states: processing (working), completed (done),
        # waiting_user_answer (provider showing approval prompt for the message).
        time.sleep(5)
        receiver_status = get_terminal_status(receiver_id)
        assert receiver_status in (
            "processing",
            "completed",
            "waiting_user_answer",
        ), f"Receiver should have transitioned from IDLE after inbox delivery, got: {receiver_status}"

    finally:
        if sender_id and actual_session:
            cleanup_terminal(sender_id, actual_session)
        if receiver_id and actual_session:
            # Receiver is in the same session, just exit it
            try:
                requests.post(f"{API_BASE_URL}/terminals/{receiver_id}/exit")
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Codex provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestCodexSendMessage:
    """E2E send_message tests for the Codex provider."""

    def test_send_message_to_inbox(self, require_codex):
        """Send a message to another Codex terminal's inbox and verify delivery."""
        _run_send_message_test(provider="codex", agent_profile="developer")


# ---------------------------------------------------------------------------
# Claude Code provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestClaudeCodeSendMessage:
    """E2E send_message tests for the Claude Code provider."""

    def test_send_message_to_inbox(self, require_claude):
        """Send a message to another Claude Code terminal's inbox and verify delivery."""
        _run_send_message_test(provider="claude_code", agent_profile="developer")


# ---------------------------------------------------------------------------
# Kiro CLI provider
# ---------------------------------------------------------------------------


@pytest.mark.e2e
class TestKiroCliSendMessage:
    """E2E send_message tests for the Kiro CLI provider."""

    def test_send_message_to_inbox(self, require_kiro):
        """Send a message to another Kiro CLI terminal's inbox and verify delivery."""
        _run_send_message_test(provider="kiro_cli", agent_profile="developer")
