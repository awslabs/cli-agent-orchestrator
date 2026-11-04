"""Tests for _resolve_child_allowed_tools in the MCP layer.

The child's allowlist is resolved from its own profile and then prechecked
against the parent's recorded delegation ceiling (``utils/delegation.py``).
The server performs the same check from the caller's stored row and is
authoritative; this layer only surfaces the refusal early with the same text.

Issue #141 asked that a child's explicit ``["*"]`` not be collapsed to the
parent's list. That still holds WITHIN the parent's ceiling: under a
supervisor (ceiling ``*``) the child stays unrestricted; under a restricted
parent whose ceiling does not include ``*`` the launch is refused with the
tools named, rather than silently narrowed.
"""

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.utils.delegation import DelegationCeilingError
from cli_agent_orchestrator.utils.orchestration import _resolve_child_allowed_tools


def _profile(allowed, role=None):
    profile = MagicMock()
    profile.allowedTools = allowed
    profile.role = role
    profile.mcpServers = None
    return profile


class TestResolveChildAllowedTools:
    """Tests for _resolve_child_allowed_tools function."""

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_wildcard_within_supervisor_ceiling_returns_unrestricted(
        self, mock_load, mock_resolve
    ):
        """#141 within the parent's ceiling: a supervisor (restricted allowlist,
        ceiling ``*``) launching a ``["*"]`` child gets an unrestricted child."""
        mock_load.return_value = _profile(["*"], role="developer")
        mock_resolve.return_value = ["*"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read", "fs_list"],
            child_profile_name="code-reviewer",
            parent_ceiling=["*"],
            parent_id="a1b2c3d4",
            parent_profile="supervisor",
        )

        assert result is None  # unrestricted

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_wildcard_under_restricted_ceiling_is_refused(self, mock_load, mock_resolve):
        """The other half of #141 as bounded: a restricted parent whose ceiling is
        its own list cannot launch an unrestricted child; the refusal names the
        parent, the child, the exceeding tool and the remedy."""
        mock_load.return_value = _profile(["*"], role="developer")
        mock_resolve.return_value = ["*"]

        with pytest.raises(DelegationCeilingError) as excinfo:
            _resolve_child_allowed_tools(
                parent_allowed_tools=["@builtin", "fs_read", "fs_list", "@cao-mcp-server"],
                child_profile_name="code-reviewer",
                parent_ceiling=["@builtin", "fs_read", "fs_list", "@cao-mcp-server"],
                parent_id="a1b2c3d4",
                parent_profile="reviewer",
            )

        message = str(excinfo.value)
        assert "a1b2c3d4" in message
        assert "reviewer" in message
        assert "code-reviewer" in message
        assert "cannot delegate: *" in message
        assert "delegateTools" in message
        assert excinfo.value.excess == ["*"]

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_reviewer_cannot_launch_developer(self, mock_load, mock_resolve):
        """The reported repro: reviewer (no bash) assigns developer (bash)."""
        mock_load.return_value = _profile(None, role="developer")
        mock_resolve.return_value = [
            "@builtin",
            "fs_*",
            "execute_bash",
            "web_fetch",
            "@cao-mcp-server",
        ]
        reviewer = ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]

        with pytest.raises(DelegationCeilingError) as excinfo:
            _resolve_child_allowed_tools(
                parent_allowed_tools=reviewer,
                child_profile_name="developer",
                parent_ceiling=reviewer,
                parent_id="a1b2c3d4",
                parent_profile="reviewer",
            )

        assert excinfo.value.excess == ["fs_*", "execute_bash", "web_fetch"]

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_supervisor_can_launch_reviewer_despite_builtin(self, mock_load, mock_resolve):
        """``@builtin`` grants nothing and is ignored: supervisor -> reviewer works
        even though the supervisor's lists never mention it."""
        mock_load.return_value = _profile(None, role="reviewer")
        mock_resolve.return_value = ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read", "fs_list"],
            child_profile_name="reviewer",
            parent_ceiling=["@cao-mcp-server", "fs_read", "fs_list"],
            parent_id="a1b2c3d4",
            parent_profile="restricted_supervisor",
        )

        assert result == "@builtin,fs_read,fs_list,@cao-mcp-server"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_missing_ceiling_falls_back_to_parent_allowlist(self, mock_load, mock_resolve):
        """A parent row from before the ceiling column (``delegate_tools`` None) is
        bounded by its own allowlist, the same strict reading the server applies."""
        mock_load.return_value = _profile(["fs_read", "execute_bash"])
        mock_resolve.return_value = ["fs_read", "execute_bash"]

        with pytest.raises(DelegationCeilingError) as excinfo:
            _resolve_child_allowed_tools(
                parent_allowed_tools=["@cao-mcp-server", "fs_read"],
                child_profile_name="developer",
                parent_ceiling=None,
            )

        assert excinfo.value.excess == ["execute_bash"]

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_none_inherits_parent_restrictions(self, mock_load, mock_resolve):
        """Child with no profile (FileNotFoundError) inherits parent's tools."""
        mock_load.side_effect = FileNotFoundError("not found")

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["fs_read", "fs_list"],
            child_profile_name="nonexistent",
            parent_ceiling=["fs_read", "fs_list"],
        )

        assert result == "fs_read,fs_list"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_unrestricted_parent_uses_child_tools(self, mock_load, mock_resolve):
        """Unrestricted parent lets child use its own tools."""
        mock_load.return_value = _profile(["fs_read", "execute_bash"])
        mock_resolve.return_value = ["fs_read", "execute_bash"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=None,
            child_profile_name="developer",
        )

        assert result == "fs_read,execute_bash"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_child_within_restricted_ceiling_uses_child_tools(self, mock_load, mock_resolve):
        """Both restricted and the child fits: child gets its own profile tools."""
        mock_load.return_value = _profile(["fs_read", "execute_bash"])
        mock_resolve.return_value = ["fs_read", "execute_bash"]

        result = _resolve_child_allowed_tools(
            parent_allowed_tools=["@cao-mcp-server", "fs_read"],
            child_profile_name="developer",
            parent_ceiling=["@cao-mcp-server", "fs_read", "execute_bash"],
        )

        assert result == "fs_read,execute_bash"

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_fs_glob_child_needs_fs_glob_or_all_atoms(self, mock_load, mock_resolve):
        mock_load.return_value = _profile(["fs_*"])
        mock_resolve.return_value = ["fs_*"]

        assert (
            _resolve_child_allowed_tools(
                ["fs_read"], "writer", parent_ceiling=["fs_read", "fs_list", "fs_write"]
            )
            == "fs_*"
        )
        with pytest.raises(DelegationCeilingError) as excinfo:
            _resolve_child_allowed_tools(["fs_read"], "writer", parent_ceiling=["fs_read"])
        assert excinfo.value.excess == ["fs_*"]


class TestDoubleNullParent:
    """Review B F6 at the precheck: an identified parent whose row records
    neither a ceiling nor an allowlist cannot delegate; never fall back to the
    profile on disk. An operator context (no parent id) stays unrestricted."""

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_identified_double_null_parent_is_refused(self, mock_load, mock_resolve):
        mock_load.return_value = _profile(None, role="reviewer")
        mock_resolve.return_value = ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]
        with pytest.raises(DelegationCeilingError, match="neither a delegation ceiling"):
            _resolve_child_allowed_tools(
                None, "reviewer", parent_ceiling=None, parent_id="a1b2c3d4", parent_profile="legacy"
            )

    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile")
    def test_operator_context_stays_unrestricted(self, mock_load, mock_resolve):
        mock_load.return_value = _profile(None, role="developer")
        mock_resolve.return_value = ["*"]
        assert _resolve_child_allowed_tools(None, "developer") is None
