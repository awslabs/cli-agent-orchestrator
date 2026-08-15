"""Tests for synchronous semantic requests to persistent agents."""

from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server import server


def _response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    return response


def _route(status="completed"):
    return {
        "success": True,
        "agents": [
            {
                "agent_id": "shaffer-estimating",
                "terminal_id": "aaaa1111",
                "provider": "codex",
                "agent_profile": "shaffer-estimating",
                "session_name": "cao-shaffer-estimating",
                "status": status,
            }
        ],
    }


def test_request_reuses_exact_persistent_terminal_and_redacts_runtime_address():
    response = _response(
        {"terminal_id": "aaaa1111", "last_message": "reviewed result", "status": "completed"}
    )
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=_route()),
        patch.object(server.requests, "post", return_value=response) as post,
    ):
        result = server._request_persistent_agent_impl(
            "shaffer-estimating", "bounded estimate check", timeout=120
        )

    assert result == {
        "success": True,
        "persistent_agent_id": "shaffer-estimating",
        "output": "reviewed result",
        "status": "completed",
    }
    assert not any("terminal" in key for key in result)
    post.assert_called_once()
    assert post.call_args.args[0].endswith("/terminals/run-step")
    payload = post.call_args.kwargs["json"]
    assert payload["reuse_terminal_id"] == "aaaa1111"
    assert payload["teardown"] is False
    assert payload["provider"] == "codex"
    assert payload["agent"] == "shaffer-estimating"
    assert payload["timeout"] == 120.0
    assert "assign_and_wait" in payload["prompt"]
    assert "Do not use send_message" in payload["prompt"]
    assert post.call_args.kwargs["timeout"] == 140.0


def test_request_fails_closed_if_target_is_busy():
    with (
        patch.object(
            server, "_discover_persistent_agent_routes_impl", return_value=_route("processing")
        ),
        patch.object(server.requests, "post") as post,
    ):
        result = server._request_persistent_agent_impl("shaffer-estimating", "task")
    assert result["success"] is False
    assert "busy" in result["error"]
    post.assert_not_called()


def test_request_fails_closed_on_missing_duplicate_and_runtime_mismatch():
    with patch.object(
        server,
        "_discover_persistent_agent_routes_impl",
        return_value={"success": True, "agents": []},
    ):
        missing = server._request_persistent_agent_impl("missing", "task")
    assert missing["success"] is False
    assert "No live persistent agent" in missing["error"]

    duplicate = _route()
    duplicate["agents"].append(dict(duplicate["agents"][0], terminal_id="bbbb2222"))
    with patch.object(server, "_discover_persistent_agent_routes_impl", return_value=duplicate):
        ambiguous = server._request_persistent_agent_impl("shaffer-estimating", "task")
    assert ambiguous["success"] is False
    assert "ambiguous" in ambiguous["error"]
    assert "aaaa1111" not in ambiguous["error"]
    assert "bbbb2222" not in ambiguous["error"]

    mismatch = _response(
        {"terminal_id": "bbbb2222", "last_message": "wrong", "status": "completed"}
    )
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=_route()),
        patch.object(server.requests, "post", return_value=mismatch),
    ):
        wrong = server._request_persistent_agent_impl("shaffer-estimating", "task")
    assert wrong["success"] is False
    assert "unexpected runtime terminal" in wrong["error"]
    assert "aaaa1111" not in wrong["error"]
    assert "bbbb2222" not in wrong["error"]


def test_request_timeout_and_error_do_not_expose_runtime_address():
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=_route()),
        patch.object(server.requests, "post", side_effect=requests.Timeout()),
    ):
        timed = server._request_persistent_agent_impl("shaffer-estimating", "task", timeout=20)
    assert timed["success"] is False
    assert "timed out after 20 seconds" in timed["error"]
    assert "aaaa1111" not in timed["error"]

    error_response = _response(
        {"detail": {"message": "worker exploded", "kind": "error", "terminal_id": "aaaa1111"}},
        status_code=502,
    )
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=_route()),
        patch.object(server.requests, "post", return_value=error_response),
    ):
        errored = server._request_persistent_agent_impl("shaffer-estimating", "task")
    assert errored["success"] is False
    assert "worker exploded" in errored["error"]
    assert "aaaa1111" not in errored["error"]


def test_fire_and_forget_semantic_send_can_be_forbidden():
    with (
        patch.object(server, "REQUIRE_PERSISTENT_AGENT_REQUEST", True),
        patch.object(server, "_discover_persistent_agent_routes_impl") as discover,
    ):
        result = server._send_message_to_persistent_agent_impl("shaffer-estimating", "task")
    assert result["success"] is False
    assert "request_persistent_agent" in result["error"]
    discover.assert_not_called()


def test_assign_and_wait_default_is_within_enforced_maximum():
    import inspect

    sig = inspect.signature(server.assign_and_wait)
    field = sig.parameters["timeout"].default
    assert field.default == 540
    assert field.metadata
    assert server._assign_and_wait_impl.__defaults__[0] == 540


def test_dispatch_persistent_agent_is_async_and_redacts_terminal_address():
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=_route()),
        patch.object(
            server, "_send_message_impl", return_value={"success": True, "message_id": 91}
        ) as send,
    ):
        result = server._dispatch_persistent_agent_impl("shaffer-estimating", "estimate task")

    assert result["success"] is True
    assert result["status"] == "dispatched"
    assert result["persistent_agent_id"] == "shaffer-estimating"
    assert len(result["request_id"]) == 32
    assert not any("terminal" in key for key in result)
    send.assert_called_once()
    args, kwargs = send.call_args
    assert args[0] == "aaaa1111"
    assert kwargs["semantic_resolved"] is True
    assert kwargs["suppress_runtime_address"] is True
    assert result["request_id"] in args[1]
    assert "assign_async" in args[1]
    assert "reply_to_persistent_request" in args[1]


def test_reply_to_persistent_request_resolves_original_sender_internally():
    lookup = _response({"message_id": 17, "sender_id": "cccc3333", "status": "delivered"})
    with (
        patch.dict("os.environ", {"CAO_TERMINAL_ID": "aaaa1111"}),
        patch.object(server.requests, "get", return_value=lookup) as get,
        patch.object(server, "_send_message_impl", return_value={"success": True}) as send,
    ):
        result = server._reply_to_persistent_request_impl("a" * 32, "reviewed result")

    assert result == {"success": True, "request_id": "a" * 32, "status": "returned"}
    assert get.call_args.args[0].endswith(f"/terminals/aaaa1111/inbox/managed-request/{'a' * 32}")
    send.assert_called_once_with(
        "cccc3333",
        f"[Persistent department result request_id={'a' * 32}]\nreviewed result",
        semantic_resolved=True,
        suppress_runtime_address=True,
    )


def test_managed_persistent_dispatch_gate_prefers_async_but_keeps_wait_optional():
    with (
        patch.object(server, "REQUIRE_MANAGED_PERSISTENT_DISPATCH", True),
        patch.object(server, "REQUIRE_PERSISTENT_AGENT_REQUEST", False),
        patch.object(server, "_discover_persistent_agent_routes_impl") as discover,
    ):
        result = server._send_message_to_persistent_agent_impl("shaffer-estimating", "task")
    assert result["success"] is False
    assert "dispatch_persistent_agent" in result["error"]
    assert "request_persistent_agent" in result["error"]
    discover.assert_not_called()


def test_managed_semantic_send_never_injects_terminal_address_into_payload():
    with (
        patch.object(server, "ENABLE_SENDER_ID_INJECTION", True),
        patch.dict("os.environ", {"CAO_TERMINAL_ID": "deadbeef"}, clear=True),
        patch.object(server, "_send_to_inbox", return_value={"success": True}) as inbox,
    ):
        result = server._send_message_impl(
            "aaaa1111",
            "managed payload",
            semantic_resolved=True,
            suppress_runtime_address=True,
        )

    assert result["success"] is True
    inbox.assert_called_once_with("aaaa1111", "managed payload", defer_delivery=False)
    assert "deadbeef" not in inbox.call_args.args[1]
    assert "Message from terminal" not in inbox.call_args.args[1]


def test_managed_semantic_delivery_error_redacts_runtime_address():
    response = _response({"detail": "Terminal aaaa1111 vanished"}, status_code=404)
    error = requests.HTTPError("404")
    error.response = response
    with (
        patch.object(server, "ENABLE_SENDER_ID_INJECTION", True),
        patch.dict("os.environ", {"CAO_TERMINAL_ID": "deadbeef"}, clear=True),
        patch.object(server, "_send_to_inbox", side_effect=error),
    ):
        result = server._send_message_impl(
            "aaaa1111",
            "managed payload",
            semantic_resolved=True,
            suppress_runtime_address=True,
        )

    assert result["success"] is False
    assert "aaaa1111" not in result["error"]
    assert "[runtime address]" in result["error"]
