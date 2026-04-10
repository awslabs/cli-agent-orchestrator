"""Tests for the CAO operations MCP server."""

from typing import TypedDict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.ops_mcp_server.models import InstallResult, LaunchResult
from cli_agent_orchestrator.ops_mcp_server.server import (
    _launch_session_impl,
    discover_profiles,
    get_profile_details,
    get_session_info,
    install_profile,
    launch_session,
    list_sessions,
    main,
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

    async def test_discover_profiles_returns_non_empty_list(self) -> None:
        """Profile discovery should return the API list unchanged."""
        profiles = [{"name": "developer", "description": "Writes code", "source": "built-in"}]
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=profiles),
        ) as mock_request:
            result = await discover_profiles()

        assert result == profiles
        mock_request.assert_called_once_with(
            "get",
            "http://127.0.0.1:9889/agents/profiles",
            params=None,
        )

    async def test_discover_profiles_returns_empty_list(self) -> None:
        """Empty profile stores should return an empty list."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ):
            result = await discover_profiles()

        assert result == []

    async def test_discover_profiles_returns_error_dict_on_api_error(self) -> None:
        """Profile discovery should convert API errors into structured failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=500, json_data={"detail": "server exploded"}),
        ):
            result = await discover_profiles()

        assert result == {
            "success": False,
            "message": "Discover profiles failed: server exploded",
        }

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
            side_effect=Exception("boom"),
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
        """Env var maps should be serialized into the API's comma-separated format."""
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
                "env_vars": "API_TOKEN=secret,BASE_URL=http://localhost:27124",
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
            side_effect=RuntimeError("network down"),
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

    async def test_launch_session_without_prompt_returns_immediately(self) -> None:
        """Launching without a prompt should skip readiness waiting."""
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.generate_session_name",
                return_value="cao-generated",
            ),
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
                return_value=_response(json_data={"id": "term-123"}),
            ) as mock_request,
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.wait_until_terminal_status"
            ) as mock_wait,
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
        mock_wait.assert_not_called()

    async def test_launch_session_with_prompt_waits_and_sends_input(self) -> None:
        """Launching with a prompt should wait for readiness, settle, and send input."""
        responses = [
            _response(json_data={"id": "term-456"}),
            _response(json_data={"success": True}),
        ]
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
                side_effect=responses,
            ) as mock_request,
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.wait_until_terminal_status",
                return_value=True,
            ) as mock_wait,
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.asyncio.sleep",
                new=AsyncMock(),
            ) as mock_sleep,
        ):
            result = await launch_session(
                agent_profile="developer",
                prompt="Build feature X",
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
        assert mock_request.call_args_list[0].kwargs["params"] == {
            "provider": "codex",
            "agent_profile": "developer",
            "session_name": "custom-session",
            "working_directory": "/workspace/project",
        }
        assert mock_request.call_args_list[1].kwargs["params"] == {"message": "Build feature X"}
        mock_wait.assert_called_once()
        wait_args = mock_wait.call_args.args
        assert wait_args[0] == "term-456"
        assert wait_args[1]
        assert mock_wait.call_args.kwargs["timeout"] == 120.0
        mock_sleep.assert_awaited_once_with(2)

    async def test_launch_session_returns_failure_on_ready_timeout(self) -> None:
        """Prompted launches should fail if the terminal never becomes ready."""
        with (
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
                return_value=_response(json_data={"id": "term-timeout"}),
            ),
            patch(
                "cli_agent_orchestrator.ops_mcp_server.server.wait_until_terminal_status",
                return_value=False,
            ),
        ):
            result = await _launch_session_impl("developer", prompt="Build feature X")

        assert result == LaunchResult(
            success=False,
            message="Terminal term-timeout did not reach ready status within 120 seconds",
            session_name=result.session_name,
            terminal_id="term-timeout",
        )
        assert result.session_name is not None

    async def test_launch_session_returns_failure_on_api_error(self) -> None:
        """Session API errors should return failed LaunchResults."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(status_code=500, json_data={"detail": "server exploded"}),
        ):
            result = await _launch_session_impl("developer")

        assert result.success is False
        assert result.message == "Launch session failed: server exploded"

    async def test_list_sessions_returns_list(self) -> None:
        """Session listing should pass through the API payload."""
        sessions = [{"session_name": "cao-123", "terminal_count": 2}]
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=sessions),
        ):
            result = await list_sessions()

        assert result == sessions

    async def test_list_sessions_returns_empty_list(self) -> None:
        """Empty session lists should be returned unchanged."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            return_value=_response(json_data=[]),
        ):
            result = await list_sessions()

        assert result == []

    async def test_list_sessions_returns_failure_on_api_error(self) -> None:
        """Session list errors should be returned as failures."""
        with patch(
            "cli_agent_orchestrator.ops_mcp_server.server.requests.request",
            side_effect=RuntimeError("api offline"),
        ):
            result = await list_sessions()

        assert result == {"success": False, "message": "List sessions failed: api offline"}

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
            side_effect=RuntimeError("boom"),
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
            side_effect=RuntimeError("delete failed"),
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
