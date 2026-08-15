"""Tests for managed assign + durable callback claiming."""

import os
from unittest.mock import MagicMock, patch

from cli_agent_orchestrator.mcp_server import server


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_bare_assign_can_be_blocked_before_worker_creation():
    with (
        patch.object(server, "REQUIRE_MANAGED_CHILD_CALLBACK", True),
        patch.object(server, "_create_terminal") as create,
    ):
        result = server._assign_impl("department-worker", "task")

    assert result["success"] is False
    assert result["terminal_id"] is None
    assert "Use assign_and_wait" in result["message"]
    create.assert_not_called()


def test_assign_and_wait_rejects_timeout_that_would_hit_codex_tool_limit():
    result = server._assign_and_wait_impl("department-worker", "task", timeout=541)
    assert result["success"] is False
    assert "between 1 and 540 seconds" in result["message"]


def test_assign_and_wait_uses_managed_assign_then_claims_callback():
    with (
        patch.dict(os.environ, {"CAO_TERMINAL_ID": "aaaa1111"}),
        patch.object(
            server,
            "_assign_impl",
            return_value={"success": True, "terminal_id": "bbbb2222"},
        ) as assign,
        patch.object(
            server,
            "_claim_managed_callback",
            return_value={
                "success": True,
                "terminal_id": "bbbb2222",
                "message_id": 42,
                "callback": "proof-token",
                "callback_status": "delivered",
            },
        ) as claim,
    ):
        result = server._assign_and_wait_impl("department-worker", "task", timeout=30)

    assert result["success"] is True
    assert result["callback"] == "proof-token"
    assert result["agent_profile"] == "department-worker"
    assign.assert_called_once_with(
        "department-worker",
        "task",
        None,
        engine=None,
        model=None,
        use_worktree=False,
        managed_wait=True,
    )
    claim.assert_called_once_with("aaaa1111", "bbbb2222", 30)


def test_claim_managed_callback_claims_pending_row_without_tui_delivery():
    pending = _response(
        [
            {
                "id": 77,
                "sender_id": "bbbb2222",
                "receiver_id": "aaaa1111",
                "message": "proof-token",
                "status": "pending",
            }
        ]
    )
    claimed = _response(
        {
            "success": True,
            "message_id": 77,
            "message": "proof-token",
            "status": "delivered",
        }
    )
    with (
        patch.object(server.requests, "get", return_value=pending) as get,
        patch.object(server.requests, "post", return_value=claimed) as post,
    ):
        result = server._claim_managed_callback("aaaa1111", "bbbb2222", 5)

    assert result == {
        "success": True,
        "terminal_id": "bbbb2222",
        "message_id": 77,
        "callback": "proof-token",
        "callback_status": "delivered",
    }
    get.assert_called_once()
    assert "/terminals/aaaa1111/inbox/messages" in get.call_args.args[0]
    post.assert_called_once()
    assert post.call_args.kwargs["params"] == {"sender_id": "bbbb2222"}


def test_managed_worker_forces_recorded_caller_and_deferred_delivery():
    own = _response({"id": "bbbb2222", "caller_id": "aaaa1111"})
    with (
        patch.dict(
            os.environ,
            {"CAO_TERMINAL_ID": "bbbb2222", "CAO_MANAGED_CALLBACK": "true"},
            clear=True,
        ),
        patch.object(server.requests, "get", return_value=own),
        patch.object(server, "_send_to_inbox", return_value={"success": True}) as inbox,
        patch.object(server, "ENABLE_SENDER_ID_INJECTION", True),
    ):
        result = server._send_message_impl(None, "proof-token")

    assert result["success"] is True
    inbox.assert_called_once_with("aaaa1111", "proof-token", defer_delivery=True)


def test_managed_worker_rejects_explicit_receiver_without_lookup_or_send():
    with (
        patch.dict(
            os.environ,
            {"CAO_TERMINAL_ID": "bbbb2222", "CAO_MANAGED_CALLBACK": "true"},
            clear=True,
        ),
        patch.object(server.requests, "get") as get,
        patch.object(server, "_send_to_inbox") as inbox,
    ):
        result = server._send_message_impl("aaaa1111", "proof-token")

    assert result["success"] is False
    assert "managed callback" in result["error"]
    assert "Omit receiver_id" in result["error"]
    get.assert_not_called()
    inbox.assert_not_called()
