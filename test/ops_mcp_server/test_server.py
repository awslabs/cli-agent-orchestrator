"""Tests for the CAO operations MCP server."""

from typing import TypedDict
from unittest.mock import MagicMock, patch

import pytest
import requests

from cli_agent_orchestrator.ops_mcp_server.models import (
    InstallResult,
    LaunchResult,
    ProfileListResult,
    SendMessageResult,
    SessionListResult,
)
from cli_agent_orchestrator.ops_mcp_server.server import (
    _launch_session_impl,
    get_profile_details,
    get_session_info,
    install_profile,
    launch_session,
    list_profiles,
    list_sessions,
    main,
    send_session_message,
    shutdown_session,
)


class InstallPayload(TypedDict):
    """Typed payload used for InstallResult assertions."""

    success: bool
    message: str
    agent_name: str
    context_file: str
    agent_file: str | None
    unresolved_vars: list[str] | None


def _response(
    *,
    status_code: int = 200,
    json_data=None,
    text: str = "",
):
    """Create a mock HTTP response."""
    response = MagicMock()
    response.status_code = status_code
    response.text = text
    response.json.return_value = json_data
    return response


@pytest.mark.asyncio
class TestProfileTools:
    """Tests for profile management tools."""

    async def test_list_profiles_returns_non_empty_list(self) -> None:
        """Profile listing should wrap the API list in a ProfileListResult."""
        profiles = [{"name": "developer", "description": "Writes code", "source": "built-in"}]
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=profiles),
        ) as mock_request:
            result = await list_profiles()

        assert result == ProfileListResult(success=True, profiles=profiles)
        mock_request.assert_called_once_with(
            "get",
            "http://127.0.0.1:9889/agents/profiles",
            params=None,
        )

    async def test_list_profiles_returns_empty_list(self) -> None:
        """Empty profile stores should still be a successful result."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ):
            result = await list_profiles()

        assert result == ProfileListResult(success=True, profiles=[])

    async def test_list_profiles_returns_failure_on_api_error(self) -> None:
        """Profile listing should convert API errors into failed results."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=500, json_data={"detail": "server exploded"}),
        ):
            result = await list_profiles()

        assert result == ProfileListResult(
            success=False,
            message="List profiles failed: server exploded",
            profiles=[],
        )

    async def test_get_profile_details_returns_profile(self) -> None:
        """Profile details should return the parsed profile payload."""
        profile = {"name": "developer", "description": "Writes code", "system_prompt": "Build it"}
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=profile),
        ):
            result = await get_profile_details("developer")

        assert result == profile

    async def test_get_profile_details_returns_failure_for_missing_profile(self) -> None:
        """Missing profiles should be returned as a tool failure."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=404, json_data={"detail": "Profile not found"}),
        ):
            result = await get_profile_details("missing")

        assert result == {
            "success": False,
            "message": "Get profile details for 'missing' failed: Profile not found",
        }

    async def test_get_profile_details_returns_failure_on_request_exception(self) -> None:
        """Transport errors should be returned instead of raised."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("boom"),
        ):
            result = await get_profile_details("developer")

        assert result == {
            "success": False,
            "message": "Get profile details for 'developer' failed: boom",
        }

    async def test_install_profile_returns_result_for_name_source(self) -> None:
        """Installing by agent name should return InstallResult."""
        payload: InstallPayload = {
            "success": True,
            "message": "Agent 'developer' installed successfully",
            "agent_name": "developer",
            "context_file": "/tmp/developer.md",
            "agent_file": "/tmp/developer.json",
            "unresolved_vars": None,
        }
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=payload),
        ) as mock_request:
            result = await install_profile("developer", provider="kiro_cli")

        assert result == InstallResult(**payload)
        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/agents/profiles/install",
            params={"source": "developer", "provider": "kiro_cli"},
        )

    async def test_install_profile_returns_result_for_url_source(self) -> None:
        """Installing by URL should pass the URL through unchanged."""
        payload: InstallPayload = {
            "success": True,
            "message": "Agent 'remote' installed successfully",
            "agent_name": "remote",
            "context_file": "/tmp/remote.md",
            "agent_file": "/tmp/remote.json",
            "unresolved_vars": ["BASE_URL"],
        }
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=payload),
        ) as mock_request:
            result = await install_profile("https://example.com/remote.md", provider="q_cli")

        assert result == InstallResult(**payload)
        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/agents/profiles/install",
            params={"source": "https://example.com/remote.md", "provider": "q_cli"},
        )

    async def test_install_profile_serializes_env_vars(self) -> None:
        """Env var maps should be serialized into the API's JSON format."""
        payload = {
            "success": True,
            "message": "installed",
            "agent_name": "developer",
            "context_file": "/tmp/developer.md",
            "agent_file": None,
            "unresolved_vars": None,
        }
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=payload),
        ) as mock_request:
            await install_profile(
                "developer",
                provider="kiro_cli",
                env_vars={"API_TOKEN": "secret", "BASE_URL": "http://localhost:27124"},
            )

        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/agents/profiles/install",
            params={
                "source": "developer",
                "provider": "kiro_cli",
                "env_vars": '{"API_TOKEN": "secret", "BASE_URL": "http://localhost:27124"}',
            },
        )

    async def test_install_profile_returns_failure_for_invalid_provider(self) -> None:
        """Invalid provider responses should become failed InstallResults."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=400, json_data={"detail": "Invalid provider"}),
        ):
            result = await install_profile("developer", provider="bad_provider")

        assert result == InstallResult(
            success=False,
            message="Install profile 'developer' failed: Invalid provider",
            agent_name=None,
            context_file=None,
            agent_file=None,
            unresolved_vars=None,
        )

    async def test_install_profile_returns_failure_on_api_error(self) -> None:
        """Transport failures should return failed InstallResults."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("network down"),
        ):
            result = await install_profile("developer")

        assert result == InstallResult(
            success=False,
            message="Install profile 'developer' failed: network down",
            agent_name=None,
            context_file=None,
            agent_file=None,
            unresolved_vars=None,
        )


@pytest.mark.asyncio
class TestSessionLifecycleTools:
    """Tests for session lifecycle tools."""

    async def test_launch_session_returns_success_with_session_identifiers(self) -> None:
        """Launching a session should return session_name and terminal_id immediately."""
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.generate_session_name",
                return_value="cao-generated",
            ),
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
                return_value=_response(json_data={"id": "term-123"}),
            ) as mock_request,
        ):
            result = await _launch_session_impl(
                agent_profile="developer",
                provider="kiro_cli",
                allowed_tools=["fs_read", "execute_bash"],
            )

        assert result == LaunchResult(
            success=True,
            message="Session 'cao-generated' launched successfully",
            session_name="cao-generated",
            terminal_id="term-123",
        )
        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/sessions",
            params={
                "provider": "kiro_cli",
                "agent_profile": "developer",
                "session_name": "cao-generated",
                "allowed_tools": "fs_read,execute_bash",
            },
        )

    async def test_launch_session_passes_custom_params(self) -> None:
        """Custom session name and working directory should be forwarded to the API."""
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
                return_value=_response(json_data={"id": "term-456"}),
            ) as mock_request,
        ):
            result = await launch_session(
                agent_profile="developer",
                provider="codex",
                session_name="custom-session",
                working_directory="/workspace/project",
            )

        assert result == LaunchResult(
            success=True,
            message="Session 'custom-session' launched successfully",
            session_name="custom-session",
            terminal_id="term-456",
        )
        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/sessions",
            params={
                "provider": "codex",
                "agent_profile": "developer",
                "session_name": "custom-session",
                "working_directory": "/workspace/project",
            },
        )

    async def test_launch_session_returns_failure_on_api_error(self) -> None:
        """Session API errors should return failed LaunchResults."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=500, json_data={"detail": "server exploded"}),
        ):
            result = await _launch_session_impl("developer")

        assert result.success is False
        assert result.message == "Launch session failed: server exploded"

    async def test_launch_session_returns_failure_on_missing_id_in_response(self) -> None:
        """Session payloads without an ``id`` field should be treated as failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data={"foo": "bar"}),
        ):
            result = await _launch_session_impl("developer")

        assert result.success is False
        assert result.message == "Launch session failed: invalid session response"
        assert result.terminal_id is None
        assert result.session_name is not None

    async def test_launch_session_returns_failure_on_non_dict_response(self) -> None:
        """Non-dict session payloads should also be treated as failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=["unexpected", "list"]),
        ):
            result = await _launch_session_impl("developer")

        assert result.success is False
        assert result.message == "Launch session failed: invalid session response"
        assert result.terminal_id is None

    async def test_send_session_message_queues_message(self) -> None:
        """A successful inbox delivery should return SendMessageResult with success."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data={"success": True}),
        ) as mock_request:
            result = await send_session_message(terminal_id="term-123", message="Build feature X")

        assert result == SendMessageResult(
            success=True,
            message="Message queued for terminal 'term-123'",
            terminal_id="term-123",
        )
        mock_request.assert_called_once_with(
            "post",
            "http://127.0.0.1:9889/terminals/term-123/inbox/messages",
            params={"sender_id": "cao-ops-mcp", "message": "Build feature X"},
        )

    async def test_send_session_message_returns_failure_for_not_found(self) -> None:
        """A 404 response should return a failed SendMessageResult."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=404, json_data={"detail": "Terminal not found"}),
        ):
            result = await send_session_message(terminal_id="missing", message="hello")

        assert result == SendMessageResult(
            success=False,
            message="Send message to terminal 'missing' failed: Terminal not found",
            terminal_id="missing",
        )

    async def test_send_session_message_returns_failure_on_api_error(self) -> None:
        """Transport errors should return failed SendMessageResults."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("api offline"),
        ):
            result = await send_session_message(terminal_id="term-123", message="hello")

        assert result == SendMessageResult(
            success=False,
            message="Send message to terminal 'term-123' failed: api offline",
            terminal_id="term-123",
        )

    async def test_send_session_message_includes_terminal_id_on_failure(self) -> None:
        """The terminal_id should always be echoed back regardless of outcome."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=500, text="internal error"),
        ):
            result = await send_session_message(terminal_id="term-abc", message="ping")

        assert result.success is False
        assert result.terminal_id == "term-abc"

    async def test_list_sessions_returns_list(self) -> None:
        """Session listing should wrap the API payload in a SessionListResult."""
        sessions = [{"session_name": "cao-123", "terminal_count": 2}]
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=sessions),
        ):
            result = await list_sessions()

        assert result == SessionListResult(success=True, sessions=sessions)

    async def test_list_sessions_returns_empty_list(self) -> None:
        """Empty session lists should still be a successful result."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ):
            result = await list_sessions()

        assert result == SessionListResult(success=True, sessions=[])

    async def test_list_sessions_returns_failure_on_api_error(self) -> None:
        """Session list errors should be returned as failed results."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("api offline"),
        ):
            result = await list_sessions()

        assert result == SessionListResult(
            success=False,
            message="List sessions failed: api offline",
            sessions=[],
        )

    async def test_get_session_info_returns_payload(self) -> None:
        """Session details should be returned unchanged."""
        payload = {"name": "cao-123", "terminals": [{"id": "term-1"}]}
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=payload),
        ):
            result = await get_session_info("cao-123")

        assert result == payload

    async def test_get_session_info_returns_failure_for_not_found(self) -> None:
        """Missing sessions should be converted into failure dicts."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=404, json_data={"detail": "Session not found"}),
        ):
            result = await get_session_info("missing")

        assert result == {
            "success": False,
            "message": "Get session info for 'missing' failed: Session not found",
        }

    async def test_get_session_info_returns_failure_on_api_error(self) -> None:
        """Transport errors should be returned for session info lookups."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("boom"),
        ):
            result = await get_session_info("cao-123")

        assert result == {
            "success": False,
            "message": "Get session info for 'cao-123' failed: boom",
        }

    async def test_shutdown_session_returns_success_payload(self) -> None:
        """Shutdown should return the API success payload."""
        payload = {"success": True, "deleted_terminals": 2}
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=payload),
        ):
            result = await shutdown_session("cao-123")

        assert result == payload

    async def test_shutdown_session_returns_failure_for_not_found(self) -> None:
        """Missing sessions should be surfaced as failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=404, json_data={"detail": "Session not found"}),
        ):
            result = await shutdown_session("missing")

        assert result == {
            "success": False,
            "message": "Shutdown session 'missing' failed: Session not found",
        }

    async def test_shutdown_session_returns_failure_on_api_error(self) -> None:
        """Shutdown transport errors should be converted into failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=requests.ConnectionError("delete failed"),
        ):
            result = await shutdown_session("cao-123")

        assert result == {
            "success": False,
            "message": "Shutdown session 'cao-123' failed: delete failed",
        }


def test_main_runs_mcp_server() -> None:
    """The module main entry point should call mcp.run()."""
    with patch("cli_agent_orchestrator.ops_mcp_server.server.mcp.run") as mock_run:
        main()

    mock_run.assert_called_once_with()
