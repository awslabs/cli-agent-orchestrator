"""Tests for the tool_mapping utility module."""

import pytest

from cli_agent_orchestrator.utils.tool_mapping import (
    format_tool_summary,
    get_disallowed_tools,
    resolve_allowed_tools,
)


class TestResolveAllowedTools:
    """Tests for resolve_allowed_tools."""

    def test_explicit_profile_tools_used(self):
        """Profile's explicit allowedTools take precedence."""
        result = resolve_allowed_tools(["fs_read", "@cao-mcp-server"], "developer")
        assert result == ["fs_read", "@cao-mcp-server"]

    def test_role_defaults_when_no_profile_tools(self):
        """Role-based defaults used when profile has no allowedTools."""
        result = resolve_allowed_tools(None, "supervisor")
        assert result == ["@cao-mcp-server"]

    def test_reviewer_role_defaults(self):
        result = resolve_allowed_tools(None, "reviewer")
        assert "@builtin" in result
        assert "fs_read" in result
        assert "fs_list" in result
        assert "execute_bash" not in result

    def test_developer_role_defaults(self):
        result = resolve_allowed_tools(None, "developer")
        assert "execute_bash" in result
        assert "fs_*" in result

    def test_default_role_when_none(self):
        """None role defaults to developer."""
        result = resolve_allowed_tools(None, None)
        assert "execute_bash" in result

    def test_mcp_servers_appended(self):
        """MCP server names appended as @server_name."""
        result = resolve_allowed_tools(None, "supervisor", ["my-server"])
        assert "@cao-mcp-server" in result
        assert "@my-server" in result

    def test_mcp_servers_not_duplicated(self):
        """Already present MCP server refs not duplicated."""
        result = resolve_allowed_tools(
            ["@cao-mcp-server"], "supervisor", ["cao-mcp-server"]
        )
        assert result.count("@cao-mcp-server") == 1

    def test_wildcard_preserved(self):
        """Wildcard '*' in profile tools is preserved."""
        result = resolve_allowed_tools(["*"], "supervisor")
        assert result == ["*"]


class TestGetDisallowedTools:
    """Tests for get_disallowed_tools."""

    def test_wildcard_returns_empty(self):
        """Wildcard allows everything — no tools blocked."""
        result = get_disallowed_tools("claude_code", ["*"])
        assert result == []

    def test_unknown_provider_returns_empty(self):
        """Unknown provider has no mapping — no tools blocked."""
        result = get_disallowed_tools("unknown_provider", ["fs_read"])
        assert result == []

    def test_claude_code_supervisor_blocks_bash(self):
        """Supervisor with only @cao-mcp-server should block all native tools."""
        result = get_disallowed_tools("claude_code", ["@cao-mcp-server"])
        assert "Bash" in result
        assert "Read" in result
        assert "Edit" in result
        assert "Write" in result

    def test_claude_code_developer_allows_all(self):
        """Developer with fs_* and execute_bash should not block anything."""
        result = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_*", "execute_bash", "@cao-mcp-server"]
        )
        assert result == []

    def test_claude_code_reviewer_blocks_write(self):
        """Reviewer with fs_read and fs_list should block Edit, Write, Bash."""
        result = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]
        )
        assert "Bash" in result
        assert "Edit" in result
        assert "Write" in result
        assert "Read" not in result

    def test_copilot_cli_supervisor(self):
        """Copilot supervisor blocks all tools."""
        result = get_disallowed_tools("copilot_cli", ["@cao-mcp-server"])
        assert "shell" in result
        assert "read" in result
        assert "write" in result

    def test_gemini_cli_reviewer(self):
        """Gemini reviewer blocks write tools."""
        result = get_disallowed_tools(
            "gemini_cli", ["@builtin", "fs_read", "fs_list"]
        )
        assert "run_shell_command" in result
        assert "write_file" in result
        assert "replace" in result

    def test_mcp_refs_ignored(self):
        """@-prefixed MCP refs don't map to native tools."""
        result = get_disallowed_tools("claude_code", ["@cao-mcp-server", "@custom"])
        # Should block all native tools since no CAO tool categories are allowed
        assert len(result) > 0


class TestFormatToolSummary:
    """Tests for format_tool_summary."""

    def test_wildcard(self):
        assert format_tool_summary(["*"]) == "ALL TOOLS (unrestricted)"

    def test_normal_tools(self):
        result = format_tool_summary(["fs_read", "@cao-mcp-server"])
        assert result == "fs_read, @cao-mcp-server"

    def test_empty_list(self):
        assert format_tool_summary([]) == ""
