"""The Kiro v3 agent-file validator (#1167 Phase 1, gates G3 and G11, content side).

Kiro falls back to its full-tool ``Default`` agent on a malformed file or rule,
drops a rule with an unknown capability, and its own validator passes broken v3
files, so CAO validates every file itself. Each negative here must refuse with
its own code. Codes are provisional (#1167).
"""

from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest

from cli_agent_orchestrator.providers.kiro_v3_schema import KAS_0_66_70, pinned_kiro_v3_schemas
from cli_agent_orchestrator.services.kiro_v3_validate import (
    MAX_AGENT_FILE_BYTES,
    KiroV3InvalidAgentError,
    require_valid_for_every_pinned_schema,
    validate_kiro_v3_agent,
    validate_kiro_v3_agent_bytes,
)
from cli_agent_orchestrator.utils.tool_mapping import (
    kiro_universal_tools,
    kiro_v3_excluded_tools,
    kiro_v3_rules,
)

FIXTURES = Path(__file__).parent / "fixtures" / "kiro_v3_agents"
GRANT = ["fs_read", "fs_list", "@cao-mcp-server"]
SERVER = {"type": "stdio", "command": "cao-mcp-server", "args": [], "timeout": 1200000}


def _render(grant=GRANT, name="reviewer", servers=None):
    """A universal file as CAO renders it, built from the tool-map functions."""
    servers = {"cao-mcp-server": dict(SERVER)} if servers is None else servers
    data = {
        "name": name,
        "description": "Reviewer",
        "tools": kiro_universal_tools(grant),
        "allowedTools": list(grant),
        "excludedTools": kiro_v3_excluded_tools(grant, pinned_kiro_v3_schemas()),
        "includeMcpJson": False,
        "resources": ["file:///fixture/context/reviewer.md"],
        "prompt": "Review.",
        "permissions": {"rules": kiro_v3_rules(grant, sorted(servers))},
    }
    if servers:
        data["mcpServers"] = servers
    return data


def _codes(data, **kwargs):
    kwargs.setdefault("expected_name", "reviewer")
    return {f.code for f in validate_kiro_v3_agent(data, KAS_0_66_70, **kwargs)}


def test_the_rendered_universal_file_is_valid_for_every_pinned_schema():
    data = _render()
    for schema in pinned_kiro_v3_schemas():
        assert validate_kiro_v3_agent(data, schema, expected_name="reviewer") == []
    assert _codes(data, launch_allowlist=GRANT) == set()


def _set(path, value):
    def mutate(data):
        target = data
        for key in path[:-1]:
            target = target[key]
        target[path[-1]] = value

    return mutate


def _rule(rule):
    return lambda data: data["permissions"]["rules"].append(rule)


def _drop_rule(capability, effect):
    def mutate(data):
        data["permissions"]["rules"] = [
            r
            for r in data["permissions"]["rules"]
            if not (r["capability"] == capability and r["effect"] == effect and "match" not in r)
        ]

    return mutate


def _on_denies(key, value, capability=None):
    """Add ``key: value`` to every unconditional deny (or the one for ``capability``)."""

    def mutate(data):
        for rule in data["permissions"]["rules"]:
            unconditional = "match" not in rule and "exclude" not in rule
            if rule["effect"] == "deny" and unconditional:
                if capability is None or rule["capability"] == capability:
                    rule[key] = list(value)

    return mutate


def _first_rule(key, value):
    return lambda data: data["permissions"]["rules"][0].__setitem__(key, value)


def _tools_add(entry):
    return lambda data: data["tools"].append(entry)


def _server_field(field, value):
    return lambda data: data["mcpServers"]["cao-mcp-server"].__setitem__(field, value)


NEGATIVES = {
    "effect_bogus": (_first_rule("effect", "bogus"), "rule_effect"),
    "effect_ask": (_first_rule("effect", "ask"), "rule_effect"),
    "effect_evaluate": (_first_rule("effect", "evaluate"), "rule_effect"),
    "capability_typo": (_first_rule("capability", "shel"), "rule_capability"),
    "capability_meta_all": (_first_rule("capability", "all"), "rule_capability"),
    "capability_meta_filesystem": (_first_rule("capability", "filesystem"), "rule_capability"),
    "capability_sandbox_network": (_first_rule("capability", "sandbox_network"), "rule_capability"),
    "rule_unknown_key": (_first_rule("when", "x"), "rule_key"),
    "rule_match_not_list": (_first_rule("match", "x"), "rule"),
    "policies": (
        _set(("permissions", "policies"), ["permit(principal, action, resource);"]),
        "forbidden_key",
    ),
    "missing_shell_deny": (_drop_rule("shell", "deny"), "render_mismatch"),
    "extra_shell_allow": (_rule({"capability": "shell", "effect": "allow"}), "render_mismatch"),
    # An empty match/exclude is not an absent one: CAO renders each deny with
    # neither key, and how Kiro reads an empty list is unmeasured (#1167 review).
    "denies_with_empty_match": (_on_denies("match", []), "render_mismatch"),
    "denies_with_empty_exclude": (_on_denies("exclude", []), "render_mismatch"),
    "shell_deny_with_empty_match": (_on_denies("match", [], "shell"), "render_mismatch"),
    "shell_deny_with_empty_exclude": (_on_denies("exclude", [], "shell"), "render_mismatch"),
    "kiro_upgrade_marker": (_set(("kiroUpgrade",), {"revision": 2}), "forbidden_key"),
    "hooks": (_set(("hooks",), []), "forbidden_key"),
    "tools_settings": (_set(("toolsSettings",), {}), "forbidden_key"),
    "tool_aliases": (_set(("toolAliases",), {}), "forbidden_key"),
    "welcome_message": (_set(("welcomeMessage",), "hi"), "forbidden_key"),
    "dispatch_kind": (_set(("dispatchKind",), "custom-agent"), "forbidden_key"),
    "effort_level": (_set(("effortLevel",), "high"), "forbidden_key"),
    "include_mcp_json_true": (_set(("includeMcpJson",), True), "include_mcp_json"),
    "include_powers_true": (_set(("includePowers",), True), "include_powers"),
    "unknown_key": (_set(("surprise",), 1), "unknown_key"),
    "tools_star_string": (_set(("tools",), "*"), "tools"),
    "tools_star_array": (_tools_add("*"), "tools_selector"),
    "tools_read_tag": (_tools_add("read"), "tools_selector"),
    "tools_web_tag": (_tools_add("web"), "tools_selector"),
    "tools_builtin_meta": (_tools_add("@builtin"), "tools_selector"),
    "tools_mcp_meta": (_tools_add("@mcp"), "tools_selector"),
    "tools_glob": (_tools_add("read_*"), "tools_selector"),
    "tools_adds_a_name": (_tools_add("execute_bash"), "render_mismatch"),
    "excluded_tools_missing_code": (
        lambda d: d["excludedTools"].remove("code"),
        "render_mismatch",
    ),
    "excluded_tools_not_list": (_set(("excludedTools",), "code"), "excluded_tools"),
    "mcp_auto_approve": (_server_field("autoApprove", ["*"]), "mcp_server_field"),
    "mcp_disabled_tools": (_server_field("disabledTools", []), "mcp_server_field"),
    "mcp_oauth": (_server_field("oauth", {}), "mcp_server_field"),
    "mcp_url": (_server_field("url", "https://example.invalid/mcp"), "mcp_server_field"),
    "mcp_server_not_granted": (
        _set(("mcpServers", "decoy"), {"command": "decoy"}),
        "mcp_server_not_granted",
    ),
    "allowed_tools_star": (_set(("allowedTools",), ["*"]), "unrestricted"),
    "allowed_tools_unknown_atom": (
        _set(("allowedTools",), ["fs_read", "fs_reed"]),
        "allowed_tools",
    ),
    "name_mismatch": (_set(("name",), "developer"), "name_mismatch"),
    "fallback_name_default": (_set(("name",), "Default"), "fallback_name"),
    "fallback_name_lower": (_set(("name",), "default"), "fallback_name"),
    "fallback_name_kiro_default": (_set(("name",), "kiro_default"), "fallback_name"),
    "missing_permissions": (lambda d: d.pop("permissions"), "missing_key"),
    "missing_excluded_tools": (lambda d: d.pop("excludedTools"), "missing_key"),
    "missing_allowed_tools": (lambda d: d.pop("allowedTools"), "missing_key"),
}


@pytest.mark.parametrize("case", sorted(NEGATIVES))
def test_g3_each_negative_refuses_with_its_code(case):
    mutate, code = NEGATIVES[case]
    data = _render()
    mutate(data)
    assert code in _codes(data)


def test_g3_negatives_cover_the_design_list():
    assert len(NEGATIVES) >= 35


def test_g11_a_reordered_but_equal_tools_list_still_passes():
    data = _render()
    data["tools"] = list(reversed(data["tools"]))
    data["permissions"]["rules"] = list(reversed(data["permissions"]["rules"]))
    assert _codes(data) == set()


@pytest.mark.parametrize(
    ("raw", "code"),
    [
        (b"{" + b" " * MAX_AGENT_FILE_BYTES + b"}", "too_large"),
        (b'{"name": "reviewer", "name": "reviewer"}', "duplicate_key"),
        (b'{"a": {"b": 1, "b": 2}}', "duplicate_key"),
        (b"\xff\xfe{}", "not_utf8"),
        (b"{not json", "not_json"),
        (b'{"timeout": NaN}', "not_json"),
        (b"[]", "not_object"),
    ],
    ids=[
        "too_large",
        "duplicate_key",
        "nested_duplicate_key",
        "not_utf8",
        "not_json",
        "nan",
        "not_object",
    ],
)
def test_g3_file_level_negatives(raw, code):
    findings = validate_kiro_v3_agent_bytes(raw, KAS_0_66_70, expected_name="reviewer")
    assert [f.code for f in findings] == [code]


def test_exactly_one_mib_is_still_read():
    data = _render()
    text = json.dumps(data)
    raw = text.encode() + b" " * (MAX_AGENT_FILE_BYTES - len(text.encode()))
    assert len(raw) == MAX_AGENT_FILE_BYTES
    assert validate_kiro_v3_agent_bytes(raw, KAS_0_66_70, expected_name="reviewer") == []


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (_set(("model",), 5), "value_type"),
        (_set(("description",), ["x"]), "value_type"),
        (_set(("prompt",), {"text": "x"}), "value_type"),
        (_set(("includeMcpJson",), 0), "include_mcp_json"),
        (_set(("includeMcpJson",), "false"), "include_mcp_json"),
        (_server_field("timeout", "1200000"), "value_type"),
        (_server_field("timeout", 0), "value_type"),
        (_server_field("timeout", True), "value_type"),
        (_server_field("args", "serve"), "value_type"),
        (_server_field("env", {"A": 1}), "value_type"),
        (_server_field("command", ["cao-mcp-server"]), "value_type"),
        (_server_field("type", 1), "value_type"),
    ],
    ids=[
        "model_number",
        "description_list",
        "prompt_object",
        "include_mcp_json_zero",
        "include_mcp_json_string",
        "timeout_string",
        "timeout_zero",
        "timeout_bool",
        "args_string",
        "env_number",
        "command_list",
        "type_number",
    ],
)
def test_a_value_of_the_wrong_type_refuses(mutate, code):
    """The engine rejects the whole file for these, and falls back to ``Default``."""
    data = _render()
    mutate(data)
    assert code in _codes(data)


@pytest.mark.parametrize("value", [["deny"], {"effect": "deny"}, None, 1])
def test_a_non_string_effect_or_capability_is_a_finding_not_a_crash(value):
    for key, code in (("effect", "rule_effect"), ("capability", "rule_capability")):
        data = _render()
        data["permissions"]["rules"][0][key] = value
        assert code in _codes(data), (key, value)


@pytest.mark.parametrize(
    ("fixture", "code"),
    [
        ("v3bad.json", "rule_effect"),
        ("v3typo.json", "rule_capability"),
        ("v2ro.json", "missing_key"),
        ("upgraded-v2ro.json", "forbidden_key"),
        ("v3star-string.json", "tools"),
        ("v3star-array.json", "tools_selector"),
        ("v3ask.json", "missing_key"),
        ("v3ro.json", "missing_key"),
        ("t-v3ids-upgrader.json", "missing_key"),
        ("cao-null-v3.json", "missing_key"),
    ],
)
def test_g3_the_measured_evidence_files_refuse(fixture, code):
    """Agent files from the 2.35.1 measurements, with local paths removed."""
    raw = (FIXTURES / fixture).read_bytes()
    name = json.loads(raw)["name"]
    codes = {f.code for f in validate_kiro_v3_agent_bytes(raw, KAS_0_66_70, expected_name=name)}
    assert code in codes


def test_g11_kiros_upgraded_file_refuses_with_the_reinstall_remedy():
    raw = (FIXTURES / "upgraded-v2ro.json").read_bytes()
    with pytest.raises(KiroV3InvalidAgentError) as excinfo:
        require_valid_for_every_pinned_schema(
            raw,
            expected_name="v2ro",
            remedy="Reinstall: `cao install v2ro --provider kiro_cli --engine v3`.",
        )
    assert "kiro_v3_invalid:forbidden_key" in excinfo.value.codes
    assert "kiroUpgrade" in str(excinfo.value)
    assert "cao install v2ro --provider kiro_cli --engine v3" in str(excinfo.value)


def test_g11_kiros_regenerated_rules_refuse():
    data = _render()
    data["permissions"]["rules"] = json.loads((FIXTURES / "upgraded-v2ro.json").read_text())[
        "permissions"
    ]["rules"]
    assert "render_mismatch" in _codes(data)


def test_launch_refuses_a_grant_wider_than_the_launch_allowlist():
    assert "grant_exceeds_launch" in _codes(_render(), launch_allowlist=["fs_read"])
    assert _codes(_render(), launch_allowlist=["fs_*", "@cao-mcp-server"]) == set()


def test_launch_refuses_when_the_probed_engine_selects_more():
    """A file rendered for the pinned engines, launched on one whose ``read`` tag widened."""
    widened = replace(
        KAS_0_66_70,
        engine="kas-test-widened",
        tags=MappingProxyType(
            {**KAS_0_66_70.tags, "read": KAS_0_66_70.tags["read"] | {"secret_tool"}}
        ),
        worst_case_selectors=MappingProxyType(
            {
                "fs_read": KAS_0_66_70.tags["read"] | {"secret_tool"},
                "web_search": KAS_0_66_70.tags["web"],
            }
        ),
    )
    findings = validate_kiro_v3_agent(
        _render(), widened, expected_name="reviewer", launch_allowlist=GRANT
    )
    assert "effective_set" in {f.code for f in findings}
    # Install-time validation (no launch allowlist) does not run the launch checks.
    assert validate_kiro_v3_agent(_render(), widened, expected_name="reviewer") == []


def test_the_launch_checks_run_only_at_launch():
    data = _render()
    assert _codes(data) == set()
    assert "grant_exceeds_launch" not in _codes(data)


def test_findings_name_the_check_and_the_input_is_never_mutated():
    data = _render()
    snapshot = copy.deepcopy(data)
    data["permissions"]["rules"][0]["effect"] = "ask"
    before = copy.deepcopy(data)
    findings = validate_kiro_v3_agent(data, KAS_0_66_70, expected_name="reviewer")
    assert data == before
    assert findings[0].refusal == "kiro_v3_invalid:rule_effect"
    assert snapshot != data


def test_meta_capabilities_refuse_even_when_a_schema_lists_them():
    """The engine's own capability set includes ``all``, ``builtin`` and ``filesystem``.

    A schema pinned straight from that set must still refuse them: meta
    capabilities expand differently per engine version, so CAO never emits one.
    """
    engine_set = replace(
        KAS_0_66_70,
        engine="kas-test-engine-set",
        capabilities=KAS_0_66_70.capabilities | KAS_0_66_70.meta_capabilities,
    )
    for meta in sorted(KAS_0_66_70.meta_capabilities):
        data = _render()
        data["permissions"]["rules"][0]["capability"] = meta
        codes = {f.code for f in validate_kiro_v3_agent(data, engine_set, expected_name="reviewer")}
        assert "rule_capability" in codes, meta
