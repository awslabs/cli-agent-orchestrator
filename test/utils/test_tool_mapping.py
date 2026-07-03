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
        assert result == ["@cao-mcp-server", "fs_read", "fs_list"]

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
        assert "web_fetch" in result

    def test_developer_default_when_no_role_no_tools(self):
        """No role + no allowedTools = developer defaults (secure default)."""
        result = resolve_allowed_tools(None, None)
        assert "execute_bash" in result
        assert "fs_*" in result
        assert "@cao-mcp-server" in result
        assert "*" not in result

    def test_mcp_servers_appended(self):
        """MCP server names appended as @server_name."""
        result = resolve_allowed_tools(None, "supervisor", ["my-server"])
        assert "@cao-mcp-server" in result
        assert "@my-server" in result

    def test_mcp_servers_not_duplicated(self):
        """Already present MCP server refs not duplicated."""
        result = resolve_allowed_tools(["@cao-mcp-server"], "supervisor", ["cao-mcp-server"])
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
        """Developer (fs_*, execute_bash, web_fetch) should not block anything."""
        result = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"]
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

    def test_antigravity_cli_reviewer(self):
        """Antigravity reviewer blocks write tools."""
        result = get_disallowed_tools("antigravity_cli", ["@builtin", "fs_read", "fs_list"])
        assert "run_shell_command" in result
        assert "write_file" in result
        assert "replace" in result

    def test_mcp_refs_ignored(self):
        """@-prefixed MCP refs don't map to native tools."""
        result = get_disallowed_tools("claude_code", ["@cao-mcp-server", "@custom"])
        # Should block all native tools since no CAO tool categories are allowed
        assert len(result) > 0


class TestClaudeCodeWebFetch:
    """The web_fetch category gates Claude Code's network tools.

    Before this category existed, WebFetch/WebSearch were unmapped and therefore
    never blocked — a read-only reviewer or orchestration-only supervisor could
    still reach the network (an exfiltration/SSRF surface). web_fetch makes that
    governable: only profiles that grant it keep network access.
    """

    def test_web_fetch_allows_network_tools(self):
        """A profile granting web_fetch does not block WebFetch/WebSearch."""
        disallowed = get_disallowed_tools("claude_code", ["web_fetch"])
        assert "WebFetch" not in disallowed
        assert "WebSearch" not in disallowed

    def test_supervisor_blocks_network_tools(self):
        """Supervisor (no web_fetch) blocks both network tools."""
        disallowed = get_disallowed_tools("claude_code", ["@cao-mcp-server", "fs_read", "fs_list"])
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed

    def test_reviewer_blocks_network_tools(self):
        """Reviewer (no web_fetch) blocks both network tools."""
        disallowed = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]
        )
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed

    def test_execute_bash_does_not_grant_network(self):
        """Network access is its own category — execute_bash alone blocks it.

        Guards against folding the network tools into execute_bash: an agent
        allowed only shell should not silently gain web access.
        """
        disallowed = get_disallowed_tools("claude_code", ["execute_bash"])
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed

    def test_antigravity_web_fetch_mapping(self):
        """Antigravity has the equivalent network category (web_fetch, google_web_search)."""
        disallowed = get_disallowed_tools("antigravity_cli", ["fs_read"])
        assert "web_fetch" in disallowed
        assert "google_web_search" in disallowed
        # Granting it unblocks both.
        granted = get_disallowed_tools("antigravity_cli", ["fs_read", "web_fetch"])
        assert "web_fetch" not in granted
        assert "google_web_search" not in granted

    def test_web_fetch_noop_for_unmapped_provider(self):
        """For a provider with no network entry (copilot), web_fetch is a
        harmless no-op — it maps to nothing and blocks nothing extra."""
        assert get_disallowed_tools("copilot_cli", ["web_fetch", "fs_read"]) == sorted(
            {"shell", "write", "list", "grep"}
        )


class TestFormatToolSummary:
    """Tests for format_tool_summary."""

    def test_wildcard(self):
        assert format_tool_summary(["*"]) == "ALL TOOLS (unrestricted)"

    def test_normal_tools(self):
        result = format_tool_summary(["fs_read", "@cao-mcp-server"])
        assert result == "fs_read, @cao-mcp-server"

    def test_empty_list(self):
        assert format_tool_summary([]) == ""


class TestClaudeCodeSubagentEscape:
    """A tool-restricted claude_code agent must not escape via subagents.

    Observed in the allowed-tools e2e: a reviewer (no Bash/Write) created a
    file anyway — first "via a delegated subagent that ran the write through
    a shell command" (Task), then on retry via "the Monitor tool" (background
    shell scripts). Everything execution-capable must gate with execute_bash;
    NotebookEdit writes .ipynb files, so it must gate with fs_write.
    """

    def test_restricted_supervisor_blocks_task(self):
        disallowed = get_disallowed_tools("claude_code", ["@cao-mcp-server"])
        # Both the legacy (`Task`) and current (`Agent`) subagent tool names
        # must be blocked — current Claude Code exposes only `Agent`.
        assert "Task" in disallowed
        assert "Agent" in disallowed
        assert "Bash" in disallowed
        assert "Monitor" in disallowed
        assert "NotebookEdit" in disallowed

    def test_reviewer_blocks_task_and_notebook_write(self):
        disallowed = get_disallowed_tools("claude_code", ["fs_read", "fs_list"])
        assert "Task" in disallowed
        assert "Agent" in disallowed
        assert "Monitor" in disallowed
        assert "NotebookEdit" in disallowed
        assert "Write" in disallowed

    def test_developer_with_bash_keeps_task(self):
        disallowed = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"]
        )
        assert "Task" not in disallowed
        assert "Agent" not in disallowed
        assert disallowed == []

    def test_unrestricted_star_keeps_everything(self):
        assert get_disallowed_tools("claude_code", ["*"]) == []
