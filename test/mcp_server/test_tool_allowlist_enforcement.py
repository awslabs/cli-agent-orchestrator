"""Enforcement of the caller's effective allowed-tools policy for CAO's MCP tools (#671).

``assign`` and ``handoff`` mint a new agent identity under a caller-chosen
profile. The provider-native restrictions built by ``utils/tool_mapping`` can
never cover them: ``get_disallowed_tools`` skips every ``@``-prefixed entry
because MCP server references have no native tool names.

Two things these tests pin down, both from review on #769:

* The policy is the one CAO already has. The effective list is the terminal's
  recorded ``allowed_tools``, or the profile resolution that ``None`` stands
  for, and the grant is the documented ``@cao-mcp-server`` selector or ``*``.
* The guard fails closed. ``None`` from ``_get_terminal_context_from_env`` is
  not proof of an operator context, so boundness comes from the environment.

The seam under test is the MCP boundary, not ``_assign_impl`` /
``_handoff_impl``: those are shared with ``cao assign`` / ``cao handoff``,
where the caller is a human operator and no agent allowlist applies.
"""

import os
from unittest.mock import patch

import pytest
import requests

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.models.agent_profile import AgentProfile

BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}


def _ctx(allowed_tools=None, profile_name="worker"):
    return {
        "terminal_id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": profile_name,
        "allowed_tools": allowed_tools,
    }


def _profile(name="worker", allowed_tools=None, role=None):
    return AgentProfile(
        name=name, description="test profile", allowedTools=allowed_tools, role=role
    )


def _patch_profile(profile):
    return patch(
        "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
        return_value=profile,
    )


def _unbound_env():
    return {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}


class TestOperatorContext:
    """An unbound caller is the supported operator path and is not restricted."""

    def test_unset_terminal_id_allows(self):
        with patch.dict(os.environ, _unbound_env(), clear=True):
            assert server._tool_denied_reason("assign") is None

    def test_unset_terminal_id_does_not_even_look_up(self):
        with patch.dict(os.environ, _unbound_env(), clear=True):
            with patch.object(server, "_get_terminal_context_from_env") as lookup:
                assert server._tool_denied_reason("assign") is None
            lookup.assert_not_called()


class TestFailsClosed:
    """[P1] An unknown authorization result must never become permission."""

    def test_transport_failure_denies(self):
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server,
                "_get_terminal_context_from_env",
                side_effect=requests.RequestException("cao-server down"),
            ):
                reason = server._tool_denied_reason("assign")
        assert reason is not None
        assert "assign" in reason

    def test_unexpected_error_denies(self):
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", side_effect=RuntimeError("boom")
            ):
                assert server._tool_denied_reason("assign") is not None

    def test_bound_caller_that_does_not_resolve_denies(self):
        """A malformed CAO_TERMINAL_ID and a 404 both arrive here as None."""
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_get_terminal_context_from_env", return_value=None):
                reason = server._tool_denied_reason("assign")
        assert reason is not None
        assert "CAO_TERMINAL_ID" in reason

    def test_unreadable_profile_denies(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(server, "_get_terminal_context_from_env", return_value=_ctx()),
                patch(
                    "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
                    side_effect=FileNotFoundError("no such profile"),
                ),
            ):
                assert server._tool_denied_reason("assign") is not None

    def test_unresolvable_policy_denies(self):
        """Recorded None with no profile to fall back to cannot be resolved."""
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server,
                "_get_terminal_context_from_env",
                return_value=_ctx(profile_name=None),
            ):
                assert server._tool_denied_reason("assign") is not None


class TestRecordedPolicy:
    """[P2] The recorded allowed_tools IS the effective list."""

    def test_server_selector_grants(self):
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server,
                "_get_terminal_context_from_env",
                return_value=_ctx(["fs_read", "@cao-mcp-server"]),
            ):
                assert server._tool_denied_reason("assign") is None

    def test_wildcard_grants(self):
        with patch.dict(os.environ, BOUND):
            with patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(["*"])):
                assert server._tool_denied_reason("assign") is None

    def test_narrow_list_denies(self):
        """A narrow --allowed-tools grant is now enforced against these tools."""
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
            ):
                reason = server._tool_denied_reason("assign")
        assert reason is not None
        assert "@cao-mcp-server" in reason

    def test_bare_tool_name_does_not_grant(self):
        """MCP tools are granted by server selector, never by bare name."""
        with patch.dict(os.environ, BOUND):
            with patch.object(
                server, "_get_terminal_context_from_env", return_value=_ctx(["assign"])
            ):
                assert server._tool_denied_reason("assign") is not None


class TestProfileFallback:
    """Recorded None means resolve from the profile, matching create_terminal."""

    def test_profile_allowed_tools_grant(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(server, "_get_terminal_context_from_env", return_value=_ctx()),
                _patch_profile(_profile(allowed_tools=["@cao-mcp-server"])),
            ):
                assert server._tool_denied_reason("assign") is None

    def test_profile_allowed_tools_deny(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(server, "_get_terminal_context_from_env", return_value=_ctx()),
                _patch_profile(_profile(allowed_tools=["fs_read"])),
            ):
                assert server._tool_denied_reason("assign") is not None

    def test_role_default_grants(self):
        """ROLE_TOOL_DEFAULTS gives developer @cao-mcp-server, so a role-only profile passes."""
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(server, "_get_terminal_context_from_env", return_value=_ctx()),
                _patch_profile(_profile(role="developer")),
            ):
                assert server._tool_denied_reason("assign") is None


class TestToolsRefuse:
    """The guard as reached through the registered MCP tools."""

    @pytest.mark.asyncio
    async def test_assign_refuses_and_never_reaches_the_impl(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(
                    server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
                ),
                patch.object(server, "_assign_impl") as impl,
            ):
                result = await server.assign(agent_profile="developer", message="do work")
        assert result["success"] is False
        assert "@cao-mcp-server" in result["error"]
        impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_handoff_refuses_and_never_reaches_the_impl(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(
                    server, "_get_terminal_context_from_env", return_value=_ctx(["fs_read"])
                ),
                patch.object(server, "_handoff_impl") as impl,
            ):
                result = await server.handoff(agent_profile="developer", message="do work")
        assert result.success is False
        assert "@cao-mcp-server" in result.message
        impl.assert_not_called()

    @pytest.mark.asyncio
    async def test_assign_runs_for_a_granted_caller(self):
        with patch.dict(os.environ, BOUND):
            with (
                patch.object(server, "_get_terminal_context_from_env", return_value=_ctx(["*"])),
                patch.object(server, "_assign_impl", return_value={"success": True}) as impl,
            ):
                result = await server.assign(agent_profile="developer", message="do work")
        assert result == {"success": True}
        impl.assert_called_once()
