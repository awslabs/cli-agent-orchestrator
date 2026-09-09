"""Enforcement of a profile's ``tools:`` allowlist for CAO's own MCP tools (#671).

The allowlist was only ever translated into provider-native restrictions
(``utils/tool_mapping.py``), which name no CAO tool, so a profile declaring a
narrow allowlist kept ``assign`` and ``handoff``, the two tools that mint a new
agent identity under a caller-chosen profile.

The seam under test is the MCP boundary, not ``_assign_impl`` /
``_handoff_impl``: those are shared with ``cao assign`` / ``cao handoff``, where
the caller is a human operator and no agent allowlist applies.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.agent_profile import AgentProfile


def _profile(name, tools):
    return AgentProfile(name=name, description="test profile", tools=tools)


def _ctx(profile_name):
    return {
        "terminal_id": "t-1",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": profile_name,
    }


class TestDeniedReason:
    """The guard itself, in isolation."""

    def test_declared_allowlist_without_the_tool_is_denied(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("narrow")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("narrow", ["memory_recall"]),
            ),
        ):
            reason = server._tool_denied_reason("assign")
        assert reason is not None
        assert "narrow" in reason and "assign" in reason

    def test_declared_allowlist_containing_the_tool_is_allowed(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("wide")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("wide", ["memory_recall", "assign"]),
            ),
        ):
            assert server._tool_denied_reason("assign") is None

    def test_no_tools_key_is_unrestricted(self):
        """Every profile CAO ships omits `tools:`, so the default install is unchanged."""
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("plain")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("plain", None),
            ),
        ):
            assert server._tool_denied_reason("assign") is None

    def test_wildcard_is_unrestricted(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("star")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("star", ["*"]),
            ),
        ):
            assert server._tool_denied_reason("assign") is None

    def test_unreadable_profile_fails_closed(self):
        """A named profile whose rules cannot be read is denied, as store_lesson does."""
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("gone")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                side_effect=FileNotFoundError("no such profile"),
            ),
        ):
            reason = server._tool_denied_reason("assign")
        assert reason is not None
        assert "gone" in reason

    def test_no_terminal_context_is_unrestricted(self):
        """No resolvable identity means no profile is in play, so nothing is declared."""
        with patch.object(server, "_get_terminal_context_from_env", return_value=None):
            assert server._tool_denied_reason("assign") is None

    def test_unreachable_server_does_not_deny(self):
        """A transport failure is not an authorization decision."""
        import requests

        with patch.object(
            server, "_get_terminal_context_from_env", side_effect=requests.RequestException("down")
        ):
            assert server._tool_denied_reason("assign") is None


class TestToolsRefuse:
    """The guard as reached through the registered MCP tools."""

    @pytest.mark.asyncio
    async def test_assign_refuses_and_never_reaches_the_impl(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("narrow")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("narrow", ["memory_recall"]),
            ),
            patch.object(server, "_assign_impl") as impl,
        ):
            result = await server.assign(agent_profile="developer", message="do work")

        assert result["success"] is False
        assert "narrow" in result["error"]
        impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_handoff_refuses_and_never_reaches_the_impl(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("narrow")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("narrow", ["memory_recall"]),
            ),
            patch.object(server, "_handoff_impl") as impl,
        ):
            result = await server.handoff(agent_profile="developer", message="do work")

        assert result.success is False
        assert "narrow" in result.message
        impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_still_runs_for_an_unrestricted_profile(self):
        with (
            patch.object(server, "_get_terminal_context_from_env", return_value=_ctx("plain")),
            patch(
                "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                return_value=_profile("plain", None),
            ),
            patch.object(server, "_assign_impl", return_value={"success": True}) as impl,
        ):
            result = await server.assign(agent_profile="developer", message="do work")

        assert result == {"success": True}
        impl.assert_called_once()
