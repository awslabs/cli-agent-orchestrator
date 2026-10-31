"""The pinned Kiro v3 engine schema and the kiro-cli version allow-list (#1167 Phase 1).

Each allow-listed kiro-cli version maps to one pinned engine schema, CAO's
reading of that engine's agent-file keys, rules, capabilities and tool tags.
Any other version refuses v3. The 0.66.70 entry is frozen here as the Phase 1
design records it; 2.36.0 ships the same engine and is pending operator
verification. Provisional (#1167).
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.providers.kiro_v3_schema import (
    KAS_0_66_70,
    SUPPORTED_KIRO_CLI_VERSIONS,
    KiroV3UnsupportedVersionError,
    kiro_v3_schema_for_cli_version,
    pinned_kiro_v3_schemas,
)


def test_the_allow_list_pins_2_35_1_and_marks_2_36_0_pending():
    assert set(SUPPORTED_KIRO_CLI_VERSIONS) == {"2.35.1", "2.36.0"}
    assert SUPPORTED_KIRO_CLI_VERSIONS["2.35.1"].pending_operator_verification is False
    assert SUPPORTED_KIRO_CLI_VERSIONS["2.36.0"].pending_operator_verification is True
    assert "pending operator verification" in SUPPORTED_KIRO_CLI_VERSIONS["2.36.0"].basis
    for version in ("2.35.1", "2.36.0"):
        assert kiro_v3_schema_for_cli_version(version) is KAS_0_66_70
    assert pinned_kiro_v3_schemas() == (KAS_0_66_70,)
    for version in (None, "", "2.35", "2.35.1 ", "v2.35.1"):
        with pytest.raises(KiroV3UnsupportedVersionError):
            kiro_v3_schema_for_cli_version(version)


def test_the_pinned_0_66_70_schema_is_frozen():
    """The Phase 1 design's pinned entry, read from the installed 0.66.70 bundle."""
    schema = KAS_0_66_70
    assert schema.engine == "kas-0.66.70"
    assert schema.effects == {"allow", "deny", "ask", "evaluate"}
    assert schema.capabilities == {
        "fs_read",
        "fs_write",
        "shell",
        "web_fetch",
        "web_search",
        "mcp",
        "subagent",
        "skill",
        "power",
        "context",
        "diagnostics",
        "workflow",
        "sandbox_network",
    }
    assert schema.meta_capabilities == {"all", "builtin", "filesystem"}
    assert schema.rule_keys == {"capability", "match", "exclude", "effect"}
    assert set(schema.agent_keys) == {
        "name",
        "description",
        "prompt",
        "tools",
        "excludedTools",
        "model",
        "effortLevel",
        "includeMcpJson",
        "includePowers",
        "mcpServers",
        "resources",
        "permissions",
        "welcomeMessage",
        "dispatchKind",
        "hooks",
    }
    assert schema.v2_only_keys == {"allowedTools", "toolsSettings"}
    assert schema.mcp_server_fields == {
        "command",
        "args",
        "cwd",
        "env",
        "timeout",
        "disabledTools",
        "autoApprove",
        "waitForReady",
        "versionNegotiation",
        "url",
        "headers",
        "oauth",
        "oauthScopes",
        "disabled",
        "type",
    }
    assert schema.tags["read"] == {
        "read_file",
        "list_directory",
        "file_search",
        "grep_search",
        "code",
        "introspect",
    }
    assert schema.tags["write"] == {"fs_write", "fs_append", "str_replace", "delete_file", "code"}
    assert schema.tags["shell"] == {"execute_bash"}
    assert schema.tags["web"] == {"web_fetch", "remote_web_search"}
    assert schema.select("glob") == schema.select("grep") == frozenset()
    assert schema.select("fs_read") == schema.tags["read"]
    assert schema.always_added == frozenset() and schema.always_added_known is False
    assert schema.fallback_agents == {"Default", "default"}


def test_the_unsupported_version_refusal_names_the_supported_versions_and_v2():
    with pytest.raises(KiroV3UnsupportedVersionError) as excinfo:
        kiro_v3_schema_for_cli_version("2.37.0")
    message = str(excinfo.value)
    assert message.startswith("kiro_v3_unsupported_version:")
    assert "kiro-cli 2.37.0" in message
    assert "2.35.1, 2.36.0" in message
    assert "engine 'v2'" in message
    assert excinfo.value.version == "2.37.0"


@pytest.mark.parametrize(
    ("entry", "selected"),
    [
        ("fs_read", KAS_0_66_70.tags["read"]),
        ("web_search", KAS_0_66_70.tags["web"]),
        ("write", KAS_0_66_70.tags["write"]),
        ("shell", frozenset({"execute_bash"})),
        ("subagent", frozenset({"subagent/*"})),
        ("code", frozenset({"code"})),
        ("knowledge", frozenset({"knowledge"})),
        ("use_aws", frozenset()),
        ("goal", frozenset()),
        ("@cao-mcp-server", frozenset()),
        ("@other/tool", frozenset()),
    ],
)
def test_selection_reads_each_entry_worst_case(entry, selected):
    assert KAS_0_66_70.select(entry) == selected
