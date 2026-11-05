"""Tests for the tool_mapping utility module."""

import pytest

from cli_agent_orchestrator.utils.tool_mapping import (
    ALL_NATIVE_TOOLS,
    EPHEMERAL_ALWAYS_DISALLOWED,
    KIRO_BUILTIN_CHROME,
    TOOL_MAPPING,
    format_tool_summary,
    get_allowed_tools,
    get_disallowed_tools,
    get_launch_disallowed_tools,
    granted_mcp_servers,
    kiro_agent_tools,
    resolve_allowed_tools,
    tool_constraint_instruction,
)

# Claude Code's own agent-messaging and agent-spawning built-ins, written out
# here rather than imported so a name dropped from the code fails these tests.
CLAUDE_CODE_ALWAYS_CUT = (
    "SendMessage",
    "ListAgents",
    "SendFile",
    "FetchInboxMessage",
    "Workflow",
    "CronCreate",
    "CronDelete",
    "CronList",
    "ScheduleWakeup",
    "RemoteTrigger",
    "EnterWorktree",
    "ExitWorktree",
    "TeamCreate",
    "TeamDelete",
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

    def test_workflow_scout_role_defaults(self):
        """Shipped scout role: read + cao workflow list/get, not developer."""
        result = resolve_allowed_tools(None, "workflow_scout")
        assert result == ["@builtin", "fs_read", "execute_bash", "@cao-mcp-server"]
        assert "fs_*" not in result
        assert "fs_write" not in result
        assert "web_fetch" not in result
        assert "*" not in result

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

    def test_unknown_role_raises(self):
        """A typo must not grant ['*'] — that is more privilege than omitting role."""
        with pytest.raises(ValueError, match="Unknown role 'Supervisor'"):
            resolve_allowed_tools(None, "Supervisor")

    def test_explicit_tools_with_unknown_role_are_honored(self):
        """Explicit allowedTools short-circuit role lookup and do not raise."""
        result = resolve_allowed_tools(["fs_read"], "bogus_role")
        assert result == ["fs_read"]


class TestExplicitAllowedToolsIsTheWholeList:
    """Regression for #772: an explicit allowedTools does not get MCP refs appended.

    The append exists so that declaring a server in ``mcpServers`` is enough to use
    it. Applied to an explicit ``allowedTools`` it also meant the operator could not
    withhold one, and ``--allowed-tools`` never got the append, so the two spellings
    ``docs/tool-restrictions.md`` calls priority 2 and 3 resolved to different
    policies from the same list.
    """

    def test_a_declared_server_is_not_added_to_an_explicit_list(self):
        result = resolve_allowed_tools(["fs_read"], None, ["cao-mcp-server"])
        assert result == ["fs_read"]

    def test_the_same_holds_when_a_role_is_also_set(self):
        """``allowedTools`` outranks ``role``, so the role must not reintroduce it."""
        result = resolve_allowed_tools(["fs_read"], "developer", ["cao-mcp-server"])
        assert result == ["fs_read"]

    def test_naming_the_server_still_grants_it(self):
        """The supported way to keep the grant is to write it in the list."""
        result = resolve_allowed_tools(["fs_read", "@cao-mcp-server"], None, ["cao-mcp-server"])
        assert result == ["fs_read", "@cao-mcp-server"]

    def test_an_empty_list_denies_everything(self):
        """``allowedTools: []`` is a deny-all and used to resolve to one grant."""
        assert resolve_allowed_tools([], None, ["cao-mcp-server"]) == []

    def test_the_cli_and_the_profile_agree_on_the_same_list(self):
        """The disagreement in #772.

        ``cli/commands/launch.py`` assigns ``list(allowed_tools)`` for
        ``--allowed-tools`` and never calls this function, so the CLI spelling was
        already unappended. Matching it here is what makes the two priorities
        express one policy.
        """
        written_by_the_operator = ["fs_read", "execute_bash"]
        via_cli = list(written_by_the_operator)
        via_profile = resolve_allowed_tools(written_by_the_operator, None, ["cao-mcp-server"])
        assert via_profile == via_cli

    def test_the_role_branch_still_appends(self):
        """Unchanged: a role default is CAO's list, not the operator's."""
        result = resolve_allowed_tools(None, "supervisor", ["my-server"])
        assert "@my-server" in result

    def test_the_no_role_fallback_still_appends(self):
        result = resolve_allowed_tools(None, None, ["my-server"])
        assert "@my-server" in result

    def test_an_explicit_wildcard_is_still_untouched(self):
        assert resolve_allowed_tools(["*"], None, ["cao-mcp-server"]) == ["*"]


class TestToolConstraintInstruction:
    """Regression for the #803 review: the sentence has to hold for an empty list.

    Five soft-enforcement providers built this by joining the allowlist, so a
    deny-all produced a sentence that named no tools and read as an authoring
    slip rather than as a restriction.
    """

    def test_a_deny_all_says_so_in_words(self):
        assert tool_constraint_instruction([]) == (
            "You may not use any tools. Do not attempt to call one."
        )

    def test_a_restricted_policy_keeps_the_existing_wording(self):
        assert tool_constraint_instruction(["fs_read", "fs_list"]) == (
            "You only have access to these tools: fs_read, fs_list"
        )


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
        """Developer (fs_*, execute_bash, web_fetch) keeps every mapped tool.

        It loses only Claude Code's own agent-messaging and agent-spawning
        built-ins, which every restricted Claude Code grant loses.
        """
        result = get_disallowed_tools(
            "claude_code", ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"]
        )
        assert result == sorted(CLAUDE_CODE_ALWAYS_CUT)

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


class TestGrokCliToolMapping:
    """Grok restrictions use native deny rules, not prompt enforcement."""

    def test_supervisor_blocks_execution_write_and_network(self):
        disallowed = get_disallowed_tools("grok_cli", ["@cao-mcp-server", "fs_read", "fs_list"])

        assert "Bash" in disallowed
        assert "Edit" in disallowed
        assert "Write" in disallowed
        assert "NotebookEdit" in disallowed
        assert "WebFetch" in disallowed
        assert "WebSearch" in disallowed
        assert "Read" not in disallowed
        assert "Grep" not in disallowed
        assert "Glob" not in disallowed

    def test_reviewer_keeps_read_and_search_only(self):
        disallowed = get_disallowed_tools(
            "grok_cli", ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"]
        )

        assert "Read" not in disallowed
        assert "NotebookRead" not in disallowed
        assert "Grep" not in disallowed
        assert "Glob" not in disallowed
        assert {"Bash", "Edit", "Write", "NotebookEdit"}.issubset(disallowed)

    def test_developer_mapping_allows_every_category(self):
        assert (
            get_disallowed_tools(
                "grok_cli",
                ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"],
            )
            == []
        )

    def test_unrestricted_star_emits_no_deny_rules(self):
        assert get_disallowed_tools("grok_cli", ["*"]) == []

    def test_restricted_allowlist_returns_only_explicit_native_capabilities(self):
        assert get_allowed_tools("grok_cli", ["@cao-mcp-server", "fs_read", "fs_list"]) == [
            "Glob",
            "Grep",
            "NotebookRead",
            "Read",
        ]
        assert "Bash" not in get_allowed_tools(
            "grok_cli", ["@cao-mcp-server", "fs_read", "fs_list"]
        )

    def test_wildcard_allowlist_returns_all_native_capabilities(self):
        assert set(get_allowed_tools("grok_cli", ["*"])) == set(
            get_allowed_tools("grok_cli", ["fs_*", "execute_bash", "web_fetch"])
        )

    def test_each_category_remains_independently_governed(self):
        bash_only = get_disallowed_tools("grok_cli", ["execute_bash"])
        assert "Bash" not in bash_only
        assert {"Read", "Edit", "Grep", "WebFetch", "WebSearch"}.issubset(bash_only)

        web_only = get_disallowed_tools("grok_cli", ["web_fetch"])
        assert "WebFetch" not in web_only
        assert "WebSearch" not in web_only
        assert {"Bash", "Read", "Edit", "Grep"}.issubset(web_only)


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
        assert disallowed == sorted(CLAUDE_CODE_ALWAYS_CUT)

    def test_unrestricted_star_keeps_everything(self):
        assert get_disallowed_tools("claude_code", ["*"]) == []


# The Claude Code mapping before Claude Code's own agent tools were cut, kept
# so an explicit list can be checked to change by the new cuts and nothing else.
_CLAUDE_CODE_MAPPING_BEFORE = {
    "execute_bash": ["Bash", "BashOutput", "KillShell", "Task", "Agent", "Monitor"],
    "fs_read": ["Read"],
    "fs_write": ["Edit", "Write", "NotebookEdit"],
    "fs_list": ["Glob", "Grep"],
    "fs_*": ["Read", "Edit", "Write", "NotebookEdit", "Glob", "Grep"],
    "web_fetch": ["WebFetch", "WebSearch"],
}

# Every other provider's mapping on the base, frozen so a change to any of them
# fails test_other_providers_are_unchanged rather than moving its expectation.
_OTHER_PROVIDER_MAPPINGS_BEFORE = {
    "antigravity_cli": {
        "execute_bash": ["run_shell_command"],
        "fs_read": ["read_file", "list_directory", "search_file_content", "glob"],
        "fs_write": ["write_file", "replace"],
        "fs_list": ["list_directory", "glob", "search_file_content"],
        "fs_*": [
            "read_file",
            "write_file",
            "replace",
            "list_directory",
            "search_file_content",
            "glob",
        ],
        "web_fetch": ["web_fetch", "google_web_search"],
    },
    "copilot_cli": {
        "execute_bash": ["shell"],
        "fs_read": ["read"],
        "fs_write": ["write"],
        "fs_list": ["list", "grep"],
        "fs_*": ["read", "write", "list", "grep"],
    },
    "grok_cli": {
        "execute_bash": ["Bash"],
        "fs_read": ["Read", "NotebookRead"],
        "fs_write": ["Edit", "Write", "NotebookEdit"],
        "fs_list": ["Grep", "Glob"],
        "fs_*": ["Read", "NotebookRead", "Edit", "Write", "NotebookEdit", "Grep", "Glob"],
        "web_fetch": ["WebFetch", "WebSearch"],
    },
    "kiro_cli": {
        "execute_bash": ["execute_bash", "shell", "subagent", "use_aws"],
        "fs_read": ["fs_read", "read", "knowledge"],
        "fs_write": ["fs_write", "write", "code"],
        "fs_list": ["glob", "grep"],
        "fs_*": ["fs_read", "read", "knowledge", "fs_write", "write", "code", "glob", "grep"],
        "web_fetch": ["web_fetch", "web_search"],
    },
}

# Newly mapped Claude Code 2.1.318 built-ins and the CAO tools that keep them.
_NEWLY_MAPPED = {
    "TaskStop": {"execute_bash"},
    "GetTask": {"execute_bash"},
    "PowerShell": {"execute_bash"},
    "LSP": {"fs_read", "fs_*"},
}

_RESTRICTED_GRANTS = [
    [],
    ["@cao-mcp-server"],
    ["fs_read", "@cao-mcp-server"],
    ["@cao-mcp-server", "fs_read", "fs_list"],
    ["@builtin", "fs_read", "fs_list", "@cao-mcp-server"],
    ["@builtin", "fs_read", "execute_bash", "@cao-mcp-server"],
    ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"],
    ["fs_write", "web_fetch"],
    ["execute_bash"],
    ["@cao-mcp-server", "@other-server", "discovery"],
]


def _cut_before(allowed):
    every = {tool for tools in _CLAUDE_CODE_MAPPING_BEFORE.values() for tool in tools}
    kept = set()
    for cao_tool in allowed:
        kept.update(_CLAUDE_CODE_MAPPING_BEFORE.get(cao_tool, []))
    return every - kept


class TestClaudeCodeBuiltinAgentToolsCut:
    """Claude Code's own agent-messaging and agent-spawning built-ins.

    Found live: a Claude Code helper granted ``fs_read`` and
    ``@cao-mcp-server`` sent its answer with Claude Code's built-in
    ``SendMessage`` instead of CAO's ``send_message``, got "Not sent — no
    agent named '<supervisor id>' is reachable", and its answer never
    arrived. No CAO grant maps to these tools, so the cut never named them.
    """

    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS)
    def test_every_restricted_grant_cuts_them(self, allowed):
        disallowed = get_disallowed_tools("claude_code", allowed)
        assert set(CLAUDE_CODE_ALWAYS_CUT) <= set(disallowed)

    @pytest.mark.parametrize("allowed", [["*"], ["fs_read", "*", "@cao-mcp-server"]])
    def test_unrestricted_keeps_them(self, allowed):
        assert get_disallowed_tools("claude_code", allowed) == []

    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS)
    def test_cao_mcp_tools_are_never_named(self, allowed):
        """CAO's send_message stays reachable whatever the grant.

        A Claude Code 2.1.318 deny matches by exact name, a legacy alias, a
        ``*`` glob or an ``mcp__<server>`` prefix, and treats a name with ``_``
        as an MCP name. Every cut name is a bare built-in name, so none can
        reach ``mcp__cao-mcp-server__send_message``.
        """
        disallowed = get_disallowed_tools("claude_code", allowed)
        assert "mcp__cao-mcp-server__send_message" not in disallowed
        assert "mcp__cao-mcp-server" not in disallowed
        assert [
            tool
            for tool in disallowed
            if not tool.isalpha() or not tool[0].isupper() or "cao" in tool.lower()
        ] == []

    def test_mt_j_cc2_helper_grant(self):
        """The helper grant from the live run keeps Read and LSP only."""
        disallowed = get_disallowed_tools("claude_code", ["fs_read", "@cao-mcp-server"])
        assert "SendMessage" in disallowed
        assert "ListAgents" in disallowed
        assert "Read" not in disallowed
        assert "LSP" not in disallowed
        assert disallowed == sorted(
            _cut_before(["fs_read"])
            | set(CLAUDE_CODE_ALWAYS_CUT)
            | {"TaskStop", "GetTask", "PowerShell"}
        )

    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS)
    def test_explicit_lists_change_by_the_new_cuts_only(self, allowed):
        newly_cut = {tool for tool, keepers in _NEWLY_MAPPED.items() if not keepers & set(allowed)}
        expected = _cut_before(allowed) | set(CLAUDE_CODE_ALWAYS_CUT) | newly_cut
        assert get_disallowed_tools("claude_code", allowed) == sorted(expected)

    @pytest.mark.parametrize("tool", ["TaskStop", "GetTask", "PowerShell"])
    def test_shell_companions_follow_execute_bash(self, tool):
        assert tool in get_disallowed_tools("claude_code", ["fs_*", "web_fetch"])
        assert tool not in get_disallowed_tools("claude_code", ["execute_bash"])

    def test_lsp_follows_fs_read(self):
        assert "LSP" in get_disallowed_tools("claude_code", ["fs_list", "execute_bash"])
        assert "LSP" in get_disallowed_tools("claude_code", ["fs_write"])
        assert "LSP" not in get_disallowed_tools("claude_code", ["fs_read"])
        assert "LSP" not in get_disallowed_tools("claude_code", ["fs_*"])

    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS)
    def test_tool_search_is_never_cut(self, allowed):
        """MCP tools, CAO's included, load through ToolSearch when deferred."""
        assert "ToolSearch" not in get_disallowed_tools("claude_code", allowed)

    def test_the_subagent_tool_still_follows_execute_bash(self):
        """Whether helpers may use Claude Code subagents at all is not decided here."""
        with_bash = get_disallowed_tools("claude_code", ["fs_read", "execute_bash"])
        assert "Agent" not in with_bash and "Task" not in with_bash
        without_bash = get_disallowed_tools("claude_code", ["fs_read"])
        assert "Agent" in without_bash and "Task" in without_bash

    def test_no_other_provider_was_added_or_removed(self):
        assert set(TOOL_MAPPING) - {"claude_code"} == set(_OTHER_PROVIDER_MAPPINGS_BEFORE)

    @pytest.mark.parametrize("provider", sorted(_OTHER_PROVIDER_MAPPINGS_BEFORE))
    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS)
    def test_other_providers_are_unchanged(self, provider, allowed):
        """Pinned to the frozen base mappings, not the live table, so an edit to
        another provider's mapping fails here."""
        mapping = _OTHER_PROVIDER_MAPPINGS_BEFORE[provider]
        every = {tool for tools in mapping.values() for tool in tools}
        kept = set()
        for cao_tool in allowed:
            kept.update(mapping.get(cao_tool, []))
        assert ALL_NATIVE_TOOLS[provider] == every
        result = get_disallowed_tools(provider, allowed)
        assert result == sorted(every - kept)
        assert get_allowed_tools(provider, allowed) == sorted(kept)
        assert set(result).isdisjoint(CLAUDE_CODE_ALWAYS_CUT)


# The seven Claude Code built-ins an ephemeral helper never gets (ruling 376, F1),
# written out so a name dropped from the code fails these tests.
EPHEMERAL_HELPER_CUT = (
    "Agent",
    "Task",
    "Monitor",
    "Skill",
    "AskUserQuestion",
    "EnterPlanMode",
    "ExitPlanMode",
)

_HELPER_ATOMS = ["fs_read", "fs_list", "fs_write", "execute_bash", "web_fetch"]
_HELPER_GRANTS = (
    [[atom, "@cao-mcp-server"] for atom in _HELPER_ATOMS]
    + [[atom] for atom in _HELPER_ATOMS]
    + [_HELPER_ATOMS + ["@cao-mcp-server"], ["@cao-mcp-server"], []]
)


class TestEphemeralClaudeCodeHelperCut:
    """An ephemeral Claude Code helper never gets subagents, Monitor, skills,
    questions or plan mode, whatever its grant; nothing else changes."""

    @pytest.mark.parametrize("allowed", _HELPER_GRANTS)
    def test_every_helper_grant_cuts_the_seven(self, allowed):
        cut = get_launch_disallowed_tools("claude_code", allowed, ephemeral=True)
        for tool in EPHEMERAL_HELPER_CUT:
            assert cut.count(tool) == 1, tool
        assert cut == sorted(
            set(get_disallowed_tools("claude_code", allowed)) | set(EPHEMERAL_HELPER_CUT)
        )

    @pytest.mark.parametrize("allowed", [["*"], None])
    def test_a_helper_loses_the_seven_even_unrestricted(self, allowed):
        assert get_launch_disallowed_tools("claude_code", allowed, ephemeral=True) == sorted(
            EPHEMERAL_HELPER_CUT
        )

    @pytest.mark.parametrize("allowed", _RESTRICTED_GRANTS + _HELPER_GRANTS)
    def test_installed_launches_are_unchanged(self, allowed):
        assert get_launch_disallowed_tools("claude_code", allowed) == get_disallowed_tools(
            "claude_code", allowed
        )

    def test_installed_developer_default_and_star_are_unchanged(self):
        developer = ["@builtin", "fs_*", "execute_bash", "web_fetch", "@cao-mcp-server"]
        assert get_launch_disallowed_tools("claude_code", developer) == sorted(
            CLAUDE_CODE_ALWAYS_CUT
        )
        assert get_launch_disallowed_tools("claude_code", ["*"]) == []
        assert get_launch_disallowed_tools("claude_code", None) == []

    @pytest.mark.parametrize("provider", ["codex", "kiro_cli", "copilot_cli", "grok_cli"])
    @pytest.mark.parametrize("allowed", _HELPER_GRANTS)
    def test_no_other_provider_gets_the_helper_cut(self, provider, allowed):
        assert get_launch_disallowed_tools(
            provider, allowed, ephemeral=True
        ) == get_disallowed_tools(provider, allowed)

    def test_the_helper_cut_is_keyed_to_claude_code_only(self):
        assert set(EPHEMERAL_ALWAYS_DISALLOWED) == {"claude_code"}
        assert EPHEMERAL_ALWAYS_DISALLOWED["claude_code"] == EPHEMERAL_HELPER_CUT


class TestGrantedMcpServers:
    """The one matching rule both grant sites share.

    These pin the rule's vocabulary. They are NOT what proves the defect fixed:
    the rule was always easy to write correctly, and the gap was that neither
    call site applied one. That is asserted on the emitted artifacts in
    ``test/agent_plugins/test_no_auto_grant.py`` (OpenCode's ``opencode.json``)
    and ``test/providers/test_grok_cli_unit.py`` (Grok's launch command).
    """

    SERVERS = ["plugin-tools", "other-tools", "cao-mcp-server"]

    def test_a_glob_selects_the_matching_servers(self):
        assert granted_mcp_servers(["fs_read", "@plugin-*"], self.SERVERS) == ["plugin-tools"]

    def test_an_exact_reference_still_works(self):
        assert granted_mcp_servers(["@cao-mcp-server"], self.SERVERS) == ["cao-mcp-server"]

    def test_star_grants_every_delivered_server(self):
        assert granted_mcp_servers(["*"], self.SERVERS) == sorted(self.SERVERS)

    def test_an_empty_allowlist_grants_nothing(self):
        assert granted_mcp_servers([], self.SERVERS) == []

    def test_a_non_matching_glob_grants_nothing(self):
        assert granted_mcp_servers(["@ghost-*"], self.SERVERS) == []

    def test_matching_is_case_sensitive(self):
        """``fnmatchcase``, not ``fnmatch``: the latter case-folds on some hosts."""
        assert granted_mcp_servers(["@PLUGIN-*"], self.SERVERS) == []
        assert granted_mcp_servers(["@Plugin-Tools"], self.SERVERS) == []

    def test_expansion_is_over_the_given_names_only(self):
        """A pattern is never returned as though it were a server name."""
        assert granted_mcp_servers(["@plugin-*"], []) == []
        assert granted_mcp_servers(["@*"], None) == []

    def test_builtin_is_cao_vocabulary_not_a_server_reference(self):
        """``@builtin`` names a provider's own tool set, so it matches nothing."""
        assert granted_mcp_servers(["@builtin"], ["builtin", "plugin-tools"]) == []

    def test_a_bare_at_sign_matches_nothing(self):
        assert granted_mcp_servers(["@"], self.SERVERS) == []

    def test_an_exact_name_containing_glob_syntax_still_matches(self):
        """Exact membership is checked independently, so nothing the old rule
        granted is lost — even for a name ``fnmatch`` would read as syntax.

        Such a reference now ALSO matches what it denotes as a pattern
        (``@srv[1]`` is a one-character class), which is inherent to the
        documented glob semantics. Conventional MCP names contain no ``fnmatch``
        metacharacter, so the two readings coincide in practice, and Grok's
        ``_MCP_SERVER_REF`` refuses such a name outright.
        """
        assert granted_mcp_servers(["@srv[1]"], ["srv[1]", "srv1"]) == ["srv1", "srv[1]"]
        assert granted_mcp_servers(["@srv[1]"], ["srv[1]"]) == ["srv[1]"]

    def test_non_string_entries_are_ignored(self):
        assert granted_mcp_servers(["@plugin-*", None, 7], self.SERVERS) == ["plugin-tools"]


class TestKiroAgentTools:
    """``kiro_agent_tools`` writes the resolved CAO policy into Kiro's ``tools``.

    On Kiro ``tools`` is availability: a tool not listed does not exist for the
    agent. ``allowedTools`` (which CAO also writes) only names what runs without
    a prompt, and CAO launches ``--trust-all-tools``, so ``tools`` is the only
    field that restricts anything. Measured on kiro-cli 2.25.0 (2026-09-29):
    the inventory under ``tools: ["*"]`` is the 14 names in
    ``KIRO_NATIVE_INVENTORY_2_25`` below, the older ``fs_read``/``execute_bash``
    spellings still work as aliases, an unknown name is ignored, and a bare
    ``@builtin`` grants every built-in INCLUDING the shell.
    """

    # What `tools: ["*"]` exposes on kiro-cli 2.25.0. If Kiro adds a tool, this
    # test fails until someone decides which CAO capability gates it; until
    # then the new tool is simply unavailable to restricted profiles, which is
    # the safe direction.
    KIRO_NATIVE_INVENTORY_2_25 = {
        "code",
        "glob",
        "goal",
        "grep",
        "introspect",
        "knowledge",
        "read",
        "shell",
        "subagent",
        "todo_list",
        "use_aws",
        "web_fetch",
        "web_search",
        "write",
    }
    SHELL_CLASS = {"shell", "execute_bash", "subagent", "use_aws"}
    WRITE_CLASS = {"write", "fs_write", "code"}

    def test_wildcard_stays_wildcard(self):
        assert kiro_agent_tools(["*"]) == ["*"]
        assert kiro_agent_tools(["fs_read", "*"]) == ["*"]

    def test_empty_allowlist_is_an_agent_with_no_tools(self):
        assert kiro_agent_tools([]) == []

    def test_supervisor_default_has_no_shell_write_or_network(self):
        tools = set(kiro_agent_tools(resolve_allowed_tools(None, "supervisor", ["cao-mcp-server"])))
        assert "@cao-mcp-server" in tools
        assert {"read", "fs_read", "glob", "grep", "knowledge"} <= tools
        assert not (tools & self.SHELL_CLASS), tools
        assert not (tools & self.WRITE_CLASS), tools
        assert not (tools & {"web_fetch", "web_search"}), tools

    def test_builtin_grants_chrome_only_never_the_shell(self):
        """A bare ``@builtin`` in Kiro's ``tools`` is every built-in, shell included.

        The reviewer default carries ``@builtin``; written through it would hand
        a read-only reviewer a shell. It must become the harmless chrome only.
        """
        assert set(kiro_agent_tools(["@builtin"])) == set(KIRO_BUILTIN_CHROME)
        reviewer = set(
            kiro_agent_tools(resolve_allowed_tools(None, "reviewer", ["cao-mcp-server"]))
        )
        assert "@builtin" not in reviewer
        assert not (reviewer & self.SHELL_CLASS), reviewer
        assert not (reviewer & self.WRITE_CLASS), reviewer
        assert set(KIRO_BUILTIN_CHROME) <= reviewer

    def test_developer_default_covers_every_measured_builtin(self):
        """The unrestricted role must lose nothing: every 2.25.0 built-in is granted."""
        developer = set(
            kiro_agent_tools(resolve_allowed_tools(None, "developer", ["cao-mcp-server"]))
        )
        assert self.KIRO_NATIVE_INVENTORY_2_25 <= developer, (
            self.KIRO_NATIVE_INVENTORY_2_25 - developer
        )
        assert "@cao-mcp-server" in developer

    def test_mapping_knows_exactly_the_measured_inventory(self):
        """Every native name the mapping grants is a real 2.25.0 tool (or its alias),
        and every real tool is gated by some capability -- no tool is unreachable
        for the unrestricted role, none is invented."""
        aliases = {"fs_read", "fs_write", "execute_bash"}
        mapped = (ALL_NATIVE_TOOLS["kiro_cli"] - aliases) | set(KIRO_BUILTIN_CHROME)
        assert mapped == self.KIRO_NATIVE_INVENTORY_2_25, sorted(
            mapped ^ self.KIRO_NATIVE_INVENTORY_2_25
        )
        # The chrome is not gated by any capability, only by @builtin.
        assert not (set(KIRO_BUILTIN_CHROME) & ALL_NATIVE_TOOLS["kiro_cli"])

    def test_privilege_equivalent_tools_gate_with_the_capability_they_equal(self):
        assert self.SHELL_CLASS <= set(kiro_agent_tools(["execute_bash"]))
        assert self.WRITE_CLASS <= set(kiro_agent_tools(["fs_write"]))
        assert "knowledge" in kiro_agent_tools(["fs_read"])
        assert set(kiro_agent_tools(["fs_list"])) == {"glob", "grep"}
        assert set(kiro_agent_tools(["web_fetch"])) == {"web_fetch", "web_search"}

    def test_mcp_references_pass_through_verbatim(self):
        assert kiro_agent_tools(["@probe", "@probe/ping", "fs_read"]) == sorted(
            {"@probe", "@probe/ping", "fs_read", "read", "knowledge"}
        )

    def test_unknown_capability_grants_nothing(self):
        assert kiro_agent_tools(["not_a_capability"]) == []

    def test_output_is_sorted_and_deduplicated(self):
        out = kiro_agent_tools(["fs_*", "fs_read", "fs_write", "fs_list"])
        assert out == sorted(set(out))
