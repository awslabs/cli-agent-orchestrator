"""Unit tests for the CAO → OpenCode permission translator."""

import pytest

from cli_agent_orchestrator.utils import opencode_permissions
from cli_agent_orchestrator.utils.opencode_permissions import (
    ALL_OPENCODE_TOOLS,
    cao_tools_to_opencode_permission,
)


class TestAllToolsUnrestricted:
    """'*' expands to every OpenCode tool set to allow."""

    def test_wildcard_allows_all_13_tools(self):
        result = cao_tools_to_opencode_permission(["*"])
        assert set(result.keys()) == set(ALL_OPENCODE_TOOLS)
        assert all(v == "allow" for v in result.values())

    def test_wildcard_includes_non_vocabulary_tools(self):
        result = cao_tools_to_opencode_permission(["*"])
        assert result["task"] == "allow"
        assert result["question"] == "allow"
        assert result["webfetch"] == "allow"
        assert result["websearch"] == "allow"
        assert result["codesearch"] == "allow"


class TestBuiltinShorthand:
    """'@builtin' is a selector, not a grant: on its own it enables no tool.

    ``utils/tool_mapping.py`` skips every ``@``-prefixed entry when computing a
    provider's native deny list, so ``@builtin`` grants nothing on Claude Code,
    Copilot or Grok.  OpenCode must agree, otherwise the shipped ``reviewer``
    role — ``["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]`` — is
    read-only everywhere except OpenCode, where it would get bash and write.
    """

    def test_builtin_alone_grants_no_vocabulary_tool(self):
        result = cao_tools_to_opencode_permission(["@builtin"])
        for tool in ("bash", "read", "edit", "write", "glob", "grep"):
            assert result[tool] == "deny", f"@builtin must not enable {tool}"

    def test_builtin_matches_empty_list(self):
        # Same output as no allowlist at all: the selector carries no grant.
        assert cao_tools_to_opencode_permission(["@builtin"]) == cao_tools_to_opencode_permission(
            []
        )

    def test_builtin_hardcoded_denies(self):
        result = cao_tools_to_opencode_permission(["@builtin"])
        assert result["task"] == "deny"
        assert result["question"] == "deny"
        assert result["webfetch"] == "deny"
        assert result["websearch"] == "deny"
        assert result["codesearch"] == "deny"

    def test_builtin_hardcoded_allows(self):
        result = cao_tools_to_opencode_permission(["@builtin"])
        assert result["todowrite"] == "allow"
        assert result["skill"] == "allow"

    def test_builtin_result_covers_all_13_tools(self):
        result = cao_tools_to_opencode_permission(["@builtin"])
        assert set(result.keys()) == set(ALL_OPENCODE_TOOLS)


class TestShippedRoleDefaults:
    """The role defaults in constants.py must mean the same thing on OpenCode."""

    def test_reviewer_default_is_read_only(self):
        from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

        result = cao_tools_to_opencode_permission(list(ROLE_TOOL_DEFAULTS["reviewer"]))
        assert result["read"] == "allow"
        assert result["glob"] == "allow"
        assert result["grep"] == "allow"
        assert result["bash"] == "deny"
        assert result["edit"] == "deny"
        assert result["write"] == "deny"

    def test_developer_default_keeps_full_access(self):
        from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

        result = cao_tools_to_opencode_permission(list(ROLE_TOOL_DEFAULTS["developer"]))
        for tool in ("bash", "read", "edit", "write", "glob", "grep"):
            assert result[tool] == "allow", f"developer must keep {tool}"

    def test_supervisor_default_has_no_shell_or_writes(self):
        from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS

        result = cao_tools_to_opencode_permission(list(ROLE_TOOL_DEFAULTS["supervisor"]))
        assert result["bash"] == "deny"
        assert result["edit"] == "deny"
        assert result["write"] == "deny"


class TestExplicitCategories:
    """Named CAO categories (execute_bash, fs_read, etc.)."""

    def test_execute_bash_and_fs_read(self):
        result = cao_tools_to_opencode_permission(["execute_bash", "fs_read"])
        assert result["bash"] == "allow"
        assert result["read"] == "allow"
        # fs_write and fs_list tools should be denied
        assert result["edit"] == "deny"
        assert result["write"] == "deny"
        assert result["glob"] == "deny"
        assert result["grep"] == "deny"

    def test_fs_star_expands_to_all_fs_tools(self):
        result = cao_tools_to_opencode_permission(["fs_*"])
        assert result["read"] == "allow"
        assert result["edit"] == "allow"
        assert result["write"] == "allow"
        assert result["glob"] == "allow"
        assert result["grep"] == "allow"
        assert result["bash"] == "deny"

    def test_fs_star_with_mcp_skips_mcp(self):
        """@<mcp-server> entries are excluded from the returned dict."""
        result = cao_tools_to_opencode_permission(["fs_*", "@cao-mcp-server"])
        # MCP tools are NOT in the returned dict (handled via opencode.json).
        for key in result:
            assert not key.startswith("cao-mcp-server")
        # fs_* tools should be allowed
        assert result["read"] == "allow"
        assert result["edit"] == "allow"
        # bash denied
        assert result["bash"] == "deny"

    def test_mcp_only_skips_all_mcp_entries(self):
        result = cao_tools_to_opencode_permission(["@my-server"])
        # No MCP-named keys; only the 13 OpenCode built-ins present
        assert set(result.keys()) == set(ALL_OPENCODE_TOOLS)


class TestHardcodedPolicies:
    """Non-vocabulary tools have fixed policies regardless of allowedTools."""

    @pytest.mark.parametrize("tool", ["task", "question", "webfetch", "websearch", "codesearch"])
    def test_always_denied(self, tool: str):
        for allowed in (["@builtin"], ["execute_bash"], []):
            result = cao_tools_to_opencode_permission(allowed)
            assert result[tool] == "deny", f"{tool} should be deny with allowed={allowed}"

    @pytest.mark.parametrize("tool", ["todowrite", "skill"])
    def test_always_allowed(self, tool: str):
        for allowed in (["@builtin"], ["execute_bash"], []):
            result = cao_tools_to_opencode_permission(allowed)
            assert result[tool] == "allow", f"{tool} should be allow with allowed={allowed}"


class TestEmptyAllowedTools:
    """Empty list → all CAO-vocabulary tools denied, non-vocabulary follow defaults."""

    def test_empty_list_denies_all_vocabulary(self):
        result = cao_tools_to_opencode_permission([])
        for tool in ["bash", "read", "edit", "write", "glob", "grep"]:
            assert result[tool] == "deny"

    def test_empty_list_hardcoded_still_applies(self):
        result = cao_tools_to_opencode_permission([])
        assert result["todowrite"] == "allow"
        assert result["skill"] == "allow"
        assert result["task"] == "deny"

    def test_returns_all_13_tools(self):
        result = cao_tools_to_opencode_permission([])
        assert set(result.keys()) == set(ALL_OPENCODE_TOOLS)


class TestResultContainsAllTools:
    """Every call returns exactly the 13 built-in OpenCode tools."""

    @pytest.mark.parametrize(
        "allowed",
        [
            ["*"],
            ["@builtin"],
            ["execute_bash", "fs_read"],
            ["fs_*", "@cao-mcp-server"],
            [],
        ],
    )
    def test_exactly_13_tools_returned(self, allowed: list):
        result = cao_tools_to_opencode_permission(allowed)
        assert set(result.keys()) == set(ALL_OPENCODE_TOOLS)


class TestUnhandledToolFailsLoudly:
    """A tool added to ALL_OPENCODE_TOOLS without a policy must raise AssertionError."""

    def test_unhandled_tool_raises(self, monkeypatch):
        monkeypatch.setattr(
            opencode_permissions,
            "ALL_OPENCODE_TOOLS",
            list(opencode_permissions.ALL_OPENCODE_TOOLS) + ["mystery_tool"],
        )
        with pytest.raises(AssertionError, match="unhandled tool 'mystery_tool'"):
            cao_tools_to_opencode_permission(["@builtin"])
