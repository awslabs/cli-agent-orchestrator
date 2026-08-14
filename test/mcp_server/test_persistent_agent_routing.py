"""Tests for opt-in semantic routing to persistent CAO agents."""

from unittest.mock import MagicMock, patch


def _response(payload, *, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def test_persistent_routing_is_disabled_by_default():
    from cli_agent_orchestrator.mcp_server import server

    with patch.object(server, "ENABLE_PERSISTENT_AGENT_ROUTING", False):
        result = server._list_persistent_agents_impl()

    assert result["success"] is False
    assert result["agents"] == []
    assert "disabled" in result["error"]


def test_list_persistent_agents_uses_exact_terminal_metadata():
    from cli_agent_orchestrator.mcp_server import server

    responses = [
        _response([{"id": "cao-a"}, {"id": "cao-b"}]),
        _response([{"id": "aaaa1111"}]),
        _response(
            {
                "id": "aaaa1111",
                "session_name": "cao-a",
                "agent_profile": "shaffer-estimating",
                "status": "completed",
                "metadata": {
                    "persistent_agent_id": "shaffer-estimating",
                    "display_name": "Estimating",
                    "organization_id": "shaffer",
                    "kind": "department",
                },
            }
        ),
        _response([{"id": "bbbb2222"}]),
        _response(
            {
                "id": "bbbb2222",
                "session_name": "cao-b",
                "agent_profile": "department-worker",
                "status": "completed",
                "metadata": None,
            }
        ),
    ]
    with (
        patch.object(server, "ENABLE_PERSISTENT_AGENT_ROUTING", True),
        patch.object(server.requests, "get", side_effect=responses) as get,
    ):
        result = server._discover_persistent_agent_routes_impl()

    assert result["success"] is True
    assert result["count"] == 1
    assert result["agents"][0] == {
        "agent_id": "shaffer-estimating",
        "terminal_id": "aaaa1111",
        "display_name": "Estimating",
        "organization_id": "shaffer",
        "kind": "department",
        "parent_agent_id": None,
        "agent_profile": "shaffer-estimating",
        "session_name": "cao-a",
        "status": "completed",
    }
    assert get.call_count == 5


def test_discovery_fails_closed_on_partial_session_failure():
    import requests

    from cli_agent_orchestrator.mcp_server import server

    bad = _response({"detail": "session temporarily unavailable"}, status_code=500)
    error = requests.HTTPError("500")
    error.response = bad
    bad.raise_for_status.side_effect = error
    with (
        patch.object(server, "ENABLE_PERSISTENT_AGENT_ROUTING", True),
        patch.object(
            server.requests,
            "get",
            side_effect=[_response([{"id": "cao-a"}]), bad],
        ),
    ):
        result = server._discover_persistent_agent_routes_impl()

    assert result["success"] is False
    assert result["agents"] == []
    assert "failed closed" in result["error"]


def test_send_to_persistent_agent_resolves_at_call_time():
    from cli_agent_orchestrator.mcp_server import server

    discovered = {
        "success": True,
        "agents": [
            {
                "agent_id": "shaffer-estimating",
                "terminal_id": "aaaa1111",
                "session_name": "cao-shaffer-estimating",
            }
        ],
    }
    with (
        patch.object(server, "_discover_persistent_agent_routes_impl", return_value=discovered),
        patch.object(
            server,
            "_send_message_impl",
            return_value={"success": True, "message_id": 7, "receiver_id": "aaaa1111"},
        ) as send,
    ):
        result = server._send_message_to_persistent_agent_impl("shaffer-estimating", "bounded task")

    send.assert_called_once_with("aaaa1111", "bounded task", semantic_resolved=True)
    assert result["persistent_agent_id"] == "shaffer-estimating"
    assert "resolved_terminal_id" not in result
    assert "receiver_id" not in result
    assert "sender_id" not in result
    assert result["resolved_session_name"] == "cao-shaffer-estimating"


def test_send_to_persistent_agent_rejects_missing_and_duplicate_ids():
    from cli_agent_orchestrator.mcp_server import server

    with patch.object(
        server,
        "_discover_persistent_agent_routes_impl",
        return_value={"success": True, "agents": []},
    ):
        missing = server._send_message_to_persistent_agent_impl("missing", "x")
    assert missing["success"] is False
    assert "No live persistent agent" in missing["error"]

    duplicate = {
        "success": True,
        "agents": [
            {"agent_id": "dup", "terminal_id": "aaaa1111"},
            {"agent_id": "dup", "terminal_id": "bbbb2222"},
        ],
    }
    with patch.object(server, "_discover_persistent_agent_routes_impl", return_value=duplicate):
        ambiguous = server._send_message_to_persistent_agent_impl("dup", "x")
    assert ambiguous["success"] is False
    assert "ambiguous" in ambiguous["error"]
    assert "aaaa1111" not in ambiguous["error"]
    assert "bbbb2222" not in ambiguous["error"]


def test_public_list_redacts_terminal_ids():
    from cli_agent_orchestrator.mcp_server import server

    discovered = {
        "success": True,
        "count": 1,
        "agents": [
            {
                "agent_id": "shaffer-estimating",
                "terminal_id": "aaaa1111",
                "display_name": "Estimating",
                "session_name": "cao-shaffer-estimating",
            }
        ],
    }
    with patch.object(server, "_discover_persistent_agent_routes_impl", return_value=discovered):
        result = server._list_persistent_agents_impl()

    assert result["success"] is True
    assert result["agents"][0]["agent_id"] == "shaffer-estimating"
    assert "terminal_id" not in result["agents"][0]


def test_semantic_only_mode_rejects_raw_receiver_but_allows_internal_resolution():
    from cli_agent_orchestrator.mcp_server import server

    with (
        patch.object(server, "REQUIRE_SEMANTIC_PERSISTENT_ROUTING", True),
        patch.object(server, "_send_to_inbox", return_value={"success": True}) as inbox,
        patch.dict("os.environ", {"CAO_TERMINAL_ID": "deadbeef"}),
    ):
        raw = server._send_message_impl("aaaa1111", "raw")
        semantic = server._send_message_impl("aaaa1111", "semantic", semantic_resolved=True)

    assert raw["success"] is False
    assert "Raw terminal receiver IDs are disabled" in raw["error"]
    assert semantic["success"] is True
    inbox.assert_called_once()
