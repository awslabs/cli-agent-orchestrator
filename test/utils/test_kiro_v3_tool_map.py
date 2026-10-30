"""The Kiro v3 tool map and rules, for every grant (#1167 Phase 1, gates G1 and G2).

The universal agent file's ``tools`` holds v2 names minus ``read`` plus the
grant's v3 ids; ``excludedTools`` removes what those entries could select on a
pinned engine beyond the grant; the rules allow the grant and deny every other
capability unconditionally. Provisional (#1167).
"""

from __future__ import annotations

import itertools

import pytest

from cli_agent_orchestrator.providers.kiro_v3_schema import pinned_kiro_v3_schemas
from cli_agent_orchestrator.utils.tool_mapping import (
    KIRO_V3_FORBIDDEN_SELECTORS,
    kiro_agent_tools,
    kiro_universal_tools,
    kiro_v3_effective_tools,
    kiro_v3_exact_tools,
    kiro_v3_excluded_tools,
    kiro_v3_rules,
)

ATOMS = ("fs_read", "fs_list", "fs_write", "execute_bash", "web_fetch")
MCP_VARIANTS = ((), ("@cao-mcp-server",), ("@other/tool",))
SERVERS = ["cao-mcp-server", "other"]


def _grants():
    for size in range(len(ATOMS) + 1):
        for atoms in itertools.combinations(ATOMS, size):
            for mcp in MCP_VARIANTS:
                yield list(atoms) + list(mcp)
    yield ["fs_*"]
    yield ["fs_*", "@builtin", "@cao-mcp-server"]
    yield ["@builtin"]
    yield ["@builtin", "execute_bash"]


GRANTS = list(_grants())
SCHEMAS = pinned_kiro_v3_schemas()
CAPABILITIES = (
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
)


def _ids(grant):
    return "+".join(grant) or "empty"


def _granted_capabilities(grant):
    atoms = set(grant)
    if "fs_*" in atoms:
        atoms.update(("fs_read", "fs_list", "fs_write"))
    caps = set()
    if atoms & {"fs_read", "fs_list"}:
        caps.update(("fs_read", "diagnostics", "context", "skill"))
    if "fs_write" in atoms:
        caps.add("fs_write")
    if "execute_bash" in atoms:
        caps.update(("shell", "subagent"))
    if "web_fetch" in atoms:
        caps.update(("web_fetch", "web_search"))
    return caps


def test_the_grant_matrix_is_complete():
    assert len(GRANTS) == 2**5 * 3 + 4


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
@pytest.mark.parametrize("schema", SCHEMAS, ids=lambda s: s.engine)
def test_g1_effective_v3_set_equals_the_exact_set(grant, schema):
    tools = kiro_universal_tools(grant)
    excluded = kiro_v3_excluded_tools(grant, SCHEMAS)
    effective = kiro_v3_effective_tools(tools, excluded, schema)

    assert effective == kiro_v3_exact_tools(grant)
    granted_write = "fs_write" in grant or "fs_*" in grant
    assert ("code" in effective) is granted_write
    assert not set(tools) & {"read", "web", "@builtin", "@mcp", "*"}
    assert not set(tools) & KIRO_V3_FORBIDDEN_SELECTORS
    assert tools == sorted(tools)


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
def test_g1_universal_tools_keep_every_v2_name_but_read(grant):
    """The file still works on v2: every v2 name it had is there except ``read``."""
    tools = set(kiro_universal_tools(grant))
    assert set(kiro_agent_tools(grant)) - {"read"} <= tools
    assert "read" not in tools


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
def test_g1_code_is_excluded_unless_write_is_granted(grant):
    excluded = set(kiro_v3_excluded_tools(grant, SCHEMAS))
    granted_write = "fs_write" in grant or "fs_*" in grant
    reads = bool(set(grant) & {"fs_read", "fs_*"})
    assert "knowledge" in excluded
    if reads and not granted_write:
        assert "code" in excluded
    if granted_write:
        assert "code" not in excluded


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
def test_g2_denies_are_complete_and_unconditional(grant):
    rules = kiro_v3_rules(grant, SERVERS)
    granted = _granted_capabilities(grant)
    unconditional_denies = {
        rule["capability"]
        for rule in rules
        if rule["effect"] == "deny" and "match" not in rule and "exclude" not in rule
    }
    allows = {rule["capability"] for rule in rules if rule["effect"] == "allow"}

    for capability in set(CAPABILITIES) - granted - {"mcp"}:
        assert capability in unconditional_denies, capability
    assert allows - {"mcp"} == granted
    assert not (allows - {"mcp"}) & unconditional_denies
    assert {"power", "workflow"} <= unconditional_denies


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
def test_g2_rule_shapes_stay_inside_the_safe_subset(grant):
    rules = kiro_v3_rules(grant, SERVERS)
    for rule in rules:
        assert set(rule) <= {"capability", "match", "exclude", "effect"}
        assert rule["effect"] in {"allow", "deny"}
        assert rule["capability"] in CAPABILITIES
    excluding = [rule for rule in rules if "exclude" in rule]
    mcp_grants = [entry for entry in grant if entry.startswith("@") and entry != "@builtin"]
    if mcp_grants:
        assert len(excluding) == 1
        complement = excluding[0]
        assert complement["capability"] == "mcp"
        assert complement["match"] == ["*"]
        assert complement["effect"] == "deny"
        assert "mcp" not in {
            rule["capability"] for rule in rules if rule["effect"] == "deny" and "match" not in rule
        }
    else:
        assert excluding == []
        assert {"capability": "mcp", "effect": "deny"} in rules


@pytest.mark.parametrize("grant", GRANTS, ids=_ids)
def test_g2_allows_come_first_and_each_half_is_sorted(grant):
    rules = kiro_v3_rules(grant, SERVERS)
    effects = [rule["effect"] for rule in rules]
    assert effects == sorted(effects)  # "allow" < "deny"
    for effect in ("allow", "deny"):
        keys = [
            (rule["capability"], (rule.get("match") or [""])[0])
            for rule in rules
            if rule["effect"] == effect
        ]
        assert keys == sorted(keys)


def test_d6_example_supervisor_like_grant_renders_exactly():
    """The Phase 1 design's worked example (a supervisor-like grant), frozen."""
    grant = ["fs_read", "fs_list", "@cao-mcp-server"]
    assert kiro_universal_tools(grant) == [
        "@cao-mcp-server",
        "file_search",
        "fs_read",
        "glob",
        "grep",
        "grep_search",
        "knowledge",
        "list_directory",
        "read_file",
    ]
    assert kiro_v3_excluded_tools(grant, SCHEMAS) == ["code", "introspect", "knowledge"]
    assert kiro_v3_rules(grant, ["cao-mcp-server"]) == [
        {"capability": "context", "effect": "allow"},
        {"capability": "diagnostics", "effect": "allow"},
        {"capability": "fs_read", "effect": "allow"},
        {"capability": "mcp", "match": ["cao-mcp-server/*"], "effect": "allow"},
        {"capability": "skill", "effect": "allow"},
        {"capability": "fs_write", "effect": "deny"},
        {
            "capability": "mcp",
            "match": ["*"],
            "exclude": ["cao-mcp-server/*"],
            "effect": "deny",
        },
        {"capability": "power", "effect": "deny"},
        {"capability": "shell", "effect": "deny"},
        {"capability": "subagent", "effect": "deny"},
        {"capability": "web_fetch", "effect": "deny"},
        {"capability": "web_search", "effect": "deny"},
        {"capability": "workflow", "effect": "deny"},
    ]


def test_mcp_glob_grants_expand_over_concrete_servers_only():
    rules = kiro_v3_rules(["@plugin-*"], ["plugin-a", "plugin-b", "other"])
    allows = [
        rule["match"][0]
        for rule in rules
        if rule["capability"] == "mcp" and rule["effect"] == "allow"
    ]
    assert allows == ["plugin-a/*", "plugin-b/*"]
    assert not any("plugin-*" in str(rule) for rule in rules)


def test_unrestricted_policies_are_refused_not_rendered():
    with pytest.raises(ValueError, match="unrestricted"):
        kiro_universal_tools(["*"])
    with pytest.raises(ValueError, match="unrestricted"):
        kiro_v3_rules(["*"], [])
