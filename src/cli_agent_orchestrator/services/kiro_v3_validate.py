"""The one validator for CAO's Kiro v3 agent files (#1167 Phase 1).

Kiro never refuses a bad v3 agent file: a malformed file or rule falls back to
its full-tool ``Default`` agent, an unknown capability drops its rule with only
a log warning, and ``kiro-cli agent validate`` knows v2 only and passes broken
v3 files with exit 0. So CAO validates its own files against the
pinned engine schema, at ``cao install`` (every pinned schema, before anything
is written) and at every v3 create (the probed schema, against the file on
disk, before allocation) -- never after a task is typed.

The checks are pure and return typed findings. Beyond shape, the file must be
exactly what CAO renders for the grant it records in ``allowedTools`` (set
equality of ``tools``, ``excludedTools`` and the rules; no MCP server outside
the grant), so a Kiro upgrader rewrite or a hand edit refuses with the
reinstall remedy.

Codes and messages are provisional (#1167): ``kiro_v3_invalid:<code>``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple, TypeGuard

from cli_agent_orchestrator.providers.kiro_identity import KIRO_FALLBACK_AGENTS
from cli_agent_orchestrator.providers.kiro_v3_schema import (
    KiroV3EngineSchema,
    KiroV3Refusal,
    pinned_kiro_v3_schemas,
)
from cli_agent_orchestrator.utils.delegation import tools_exceeding
from cli_agent_orchestrator.utils.tool_mapping import (
    KIRO_V3_FORBIDDEN_SELECTORS,
    TOOL_MAPPING,
    granted_mcp_servers,
    kiro_universal_tools,
    kiro_v3_effective_tools,
    kiro_v3_exact_tools,
    kiro_v3_excluded_tools,
    kiro_v3_rules,
    split_mcp_reference,
)

#: Kiro's own loader would be the next reader; a file this large is not CAO's.
MAX_AGENT_FILE_BYTES = 1024 * 1024

#: The top-level keys CAO writes into a universal file.
CAO_UNIVERSAL_KEYS = frozenset(
    {
        "name",
        "description",
        "prompt",
        "tools",
        "allowedTools",
        "excludedTools",
        "includeMcpJson",
        "resources",
        "mcpServers",
        "model",
        "permissions",
    }
)
#: Keys that refuse on sight, each for its own reason: Kiro's upgrader marker,
#: fields CAO does not translate for v3, raw Cedar, and engine fields CAO never
#: sets.
FORBIDDEN_KEYS = (
    "kiroUpgrade",
    "hooks",
    "toolsSettings",
    "toolAliases",
    "policies",
    "welcomeMessage",
    "dispatchKind",
    "effortLevel",
)
#: The MCP server fields CAO emits. ``autoApprove``, ``disabledTools``,
#: ``oauth`` and everything else the engine knows refuse.
CAO_MCP_SERVER_FIELDS = frozenset({"command", "args", "env", "timeout", "type"})
#: Value types the engine's schema requires. A value of another type fails the
#: whole file, and a failed file starts Kiro's full-tool ``Default`` agent.
_NULLABLE_STRING_KEYS = ("description", "prompt", "model")
_MCP_FIELD_TYPES = {
    "command": lambda value: isinstance(value, str),
    "args": lambda value: _string_list(value),
    "env": lambda value: isinstance(value, dict)
    and all(isinstance(item, str) for item in value.values()),
    # JSON numbers; ``bool`` is an ``int`` in Python but not a number to the engine.
    "timeout": lambda value: isinstance(value, (int, float))
    and not isinstance(value, bool)
    and value >= 1,
    "type": lambda value: isinstance(value, str),
}
_RULE_EFFECTS = frozenset({"allow", "deny"})
_NEVER_CAPABILITIES = frozenset({"sandbox_network"})
_CAO_GRANT_ATOMS = frozenset(TOOL_MAPPING["kiro_cli"]) | {"@builtin"}


@dataclass(frozen=True)
class KiroV3Finding:
    """One failed check: a stable code and a human detail."""

    code: str
    detail: str

    @property
    def refusal(self) -> str:
        return f"kiro_v3_invalid:{self.code}"


class KiroV3InvalidAgentError(KiroV3Refusal):
    """A Kiro v3 agent file failed validation; carries every finding."""

    def __init__(self, agent: str, findings: Sequence[KiroV3Finding], remedy: str) -> None:
        self.agent = agent
        self.findings = tuple(findings)
        self.codes = tuple(finding.refusal for finding in self.findings)
        self.code = self.codes[0] if self.codes else "kiro_v3_invalid"
        details = "; ".join(f"{f.refusal}: {f.detail}" for f in self.findings)
        super().__init__(
            f"{self.code}: the Kiro v3 agent file "
            f"for '{agent}' failed validation ({details}). {remedy}"
        )


class _DuplicateKey(ValueError):
    pass


def _reject_duplicates(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    seen: Dict[str, Any] = {}
    for key, value in pairs:
        if key in seen:
            raise _DuplicateKey(key)
        seen[key] = value
    return seen


def _reject_constant(name: str) -> Any:
    # JSON.parse rejects NaN and Infinity; Python's json accepts them.
    raise ValueError(f"non-standard JSON constant {name}")


def parse_kiro_v3_agent_bytes(raw: bytes) -> Tuple[Optional[Dict[str, Any]], List[KiroV3Finding]]:
    """Parse one agent file: at most 1 MiB, UTF-8, one JSON object, no duplicate keys."""
    if len(raw) > MAX_AGENT_FILE_BYTES:
        return None, [KiroV3Finding("too_large", f"{len(raw)} bytes > {MAX_AGENT_FILE_BYTES}")]
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        return None, [KiroV3Finding("not_utf8", str(exc))]
    try:
        data = json.loads(
            text, object_pairs_hook=_reject_duplicates, parse_constant=_reject_constant
        )
    except _DuplicateKey as exc:
        return None, [KiroV3Finding("duplicate_key", f"key {exc} appears twice")]
    except ValueError as exc:
        return None, [KiroV3Finding("not_json", str(exc))]
    if not isinstance(data, dict):
        return None, [KiroV3Finding("not_object", "the file is not a JSON object")]
    return data, []


def _string_list(value: Any) -> TypeGuard[List[str]]:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _grant_findings(allowed: Any) -> List[KiroV3Finding]:
    if not _string_list(allowed):
        return [KiroV3Finding("allowed_tools", "allowedTools is not a list of strings")]
    findings = []
    for entry in allowed:
        if entry == "*":
            findings.append(KiroV3Finding("unrestricted", "allowedTools grants '*'"))
        elif entry not in _CAO_GRANT_ATOMS and split_mcp_reference(entry) is None:
            findings.append(KiroV3Finding("allowed_tools", f"{entry!r} is not a CAO grant entry"))
    return findings


def _rule_key(
    rule: Mapping[str, Any],
) -> Tuple[Any, Optional[Tuple[str, ...]], Optional[Tuple[str, ...]], Any]:
    """One rule as a comparable value; ``match``/``exclude`` order is significant.

    An absent ``match`` or ``exclude`` (None) is not an empty one (``()``). CAO
    renders each out-of-grant deny with neither key, and how Kiro reads an empty
    list is unmeasured, so a deny rewritten with ``"match": []`` must not compare
    equal to CAO's render (#1167).
    """
    return (
        rule.get("capability"),
        tuple(rule["match"]) if "match" in rule else None,
        tuple(rule["exclude"]) if "exclude" in rule else None,
        rule.get("effect"),
    )


def _rule_findings(permissions: Any, schema: KiroV3EngineSchema) -> List[KiroV3Finding]:
    if not isinstance(permissions, dict):
        return [KiroV3Finding("permissions", "permissions is not an object")]
    findings = []
    if "policies" in permissions:
        findings.append(KiroV3Finding("forbidden_key", "permissions.policies (raw Cedar)"))
    extra = sorted(set(permissions) - {"rules", "policies"})
    if extra:
        findings.append(KiroV3Finding("permissions", f"unknown permissions keys {extra}"))
    rules = permissions.get("rules")
    if not isinstance(rules, list):
        return findings + [KiroV3Finding("permissions", "permissions.rules is not a list")]
    for index, rule in enumerate(rules):
        if not isinstance(rule, dict):
            findings.append(KiroV3Finding("rule", f"rule {index} is not an object"))
            continue
        unknown = sorted(set(rule) - set(schema.rule_keys))
        if unknown:
            findings.append(KiroV3Finding("rule_key", f"rule {index} has keys {unknown}"))
        effect = rule.get("effect")
        if (
            not isinstance(effect, str)
            or effect not in _RULE_EFFECTS
            or effect not in schema.effects
        ):
            findings.append(KiroV3Finding("rule_effect", f"rule {index} effect {effect!r}"))
        capability = rule.get("capability")
        if (
            not isinstance(capability, str)
            or capability not in schema.capabilities
            or capability in schema.meta_capabilities
            or capability in _NEVER_CAPABILITIES
        ):
            findings.append(
                KiroV3Finding("rule_capability", f"rule {index} capability {capability!r}")
            )
        for key in ("match", "exclude"):
            if key in rule and not _string_list(rule[key]):
                findings.append(KiroV3Finding("rule", f"rule {index} {key} is not a string list"))
    return findings


def _mcp_findings(servers: Any, schema: KiroV3EngineSchema) -> List[KiroV3Finding]:
    if not isinstance(servers, dict):
        return [KiroV3Finding("mcp_servers", "mcpServers is not an object")]
    findings = []
    allowed_fields = CAO_MCP_SERVER_FIELDS & schema.mcp_server_fields
    for name, server in sorted(servers.items()):
        if not isinstance(server, dict):
            findings.append(KiroV3Finding("mcp_servers", f"server {name!r} is not an object"))
            continue
        extra = sorted(set(server) - allowed_fields)
        if extra:
            findings.append(KiroV3Finding("mcp_server_field", f"server {name!r} sets {extra}"))
        for field, valid in _MCP_FIELD_TYPES.items():
            if field in server and not valid(server[field]):
                findings.append(
                    KiroV3Finding("value_type", f"server {name!r} {field} {server[field]!r}")
                )
    return findings


def validate_kiro_v3_agent(
    data: Mapping[str, Any],
    schema: KiroV3EngineSchema,
    *,
    expected_name: str,
    launch_allowlist: Optional[List[str]] = None,
    render_schemas: Optional[Iterable[KiroV3EngineSchema]] = None,
) -> List[KiroV3Finding]:
    """Check one parsed universal file against ``schema``; empty means valid.

    ``expected_name`` is the file stem and the requested agent. With
    ``launch_allowlist`` (the launch's allowlist, or Step 0d's ceiling) the
    launch-only checks run too: the file's grant must fit it, and the effective
    v3 set under ``schema`` must equal the grant's exact set. ``render_schemas``
    are the schemas CAO renders ``excludedTools`` over (every pinned one).
    """
    findings: List[KiroV3Finding] = []
    for key in FORBIDDEN_KEYS:
        if key in data:
            findings.append(KiroV3Finding("forbidden_key", key))
    if data.get("includePowers") is True:
        findings.append(KiroV3Finding("include_powers", "includePowers is true"))
    if "includeMcpJson" in data and data["includeMcpJson"] is not False:
        findings.append(KiroV3Finding("include_mcp_json", "includeMcpJson is not false"))
    for key in _NULLABLE_STRING_KEYS:
        if data.get(key) is not None and not isinstance(data[key], str):
            findings.append(KiroV3Finding("value_type", f"{key} is not a string"))
    # Keys CAO writes that the engine's schema also declares, plus allowedTools
    # (the grant the file records). ``includePowers: false`` is still unknown:
    # CAO never writes it, and v2 lists such files as "(v3 format)".
    allowed_keys = (CAO_UNIVERSAL_KEYS & set(schema.agent_keys)) | {"allowedTools"}
    reported = set(FORBIDDEN_KEYS) | (
        {"includePowers"} if data.get("includePowers") is True else set()
    )
    unknown = sorted(set(data) - allowed_keys - reported)
    if unknown:
        findings.append(KiroV3Finding("unknown_key", f"keys {unknown}"))
    missing = sorted({"name", "tools", "allowedTools", "excludedTools", "permissions"} - set(data))
    if missing:
        findings.append(KiroV3Finding("missing_key", f"keys {missing}"))

    name = data.get("name")
    if not isinstance(name, str):
        findings.append(KiroV3Finding("name", "name is not a string"))
    elif name in KIRO_FALLBACK_AGENTS or name in schema.fallback_agents:
        findings.append(KiroV3Finding("fallback_name", f"name {name!r} is a fallback agent"))
    elif name != expected_name:
        findings.append(KiroV3Finding("name_mismatch", f"{name!r} != {expected_name!r}"))

    tools = data.get("tools")
    if not _string_list(tools):
        findings.append(KiroV3Finding("tools", "tools is not a list of strings"))
    else:
        for entry in tools:
            if entry in KIRO_V3_FORBIDDEN_SELECTORS or (
                not entry.startswith("@") and any(char in entry for char in "*?")
            ):
                findings.append(KiroV3Finding("tools_selector", f"tools holds {entry!r}"))
    if "excludedTools" in data and not _string_list(data["excludedTools"]):
        findings.append(KiroV3Finding("excluded_tools", "excludedTools is not a string list"))
    if "resources" in data and not _string_list(data.get("resources")):
        findings.append(KiroV3Finding("resources", "resources is not a list of strings"))

    if "permissions" in data:
        findings.extend(_rule_findings(data["permissions"], schema))
    servers = data.get("mcpServers", {})
    findings.extend(_mcp_findings(servers, schema))
    grant_findings = _grant_findings(data.get("allowedTools"))
    findings.extend(grant_findings)
    if findings:
        return findings

    # Set equality with CAO's render for the grant the file records, so a
    # Kiro upgrader rewrite or a hand edit refuses with the reinstall remedy.
    tools = list(data["tools"])
    excluded = list(data["excludedTools"])
    grant: List[str] = list(data["allowedTools"])
    server_names = sorted(servers)
    schemas = tuple(render_schemas) if render_schemas is not None else pinned_kiro_v3_schemas()
    expected_tools = set(kiro_universal_tools(grant))
    if set(tools) != expected_tools:
        findings.append(
            KiroV3Finding(
                "render_mismatch", f"tools differ by {sorted(set(tools) ^ expected_tools)}"
            )
        )
    expected_excluded = set(kiro_v3_excluded_tools(grant, schemas))
    if set(excluded) != expected_excluded:
        findings.append(
            KiroV3Finding(
                "render_mismatch",
                f"excludedTools differ by {sorted(set(excluded) ^ expected_excluded)}",
            )
        )
    rules = data["permissions"]["rules"]
    expected_rules = {_rule_key(rule) for rule in kiro_v3_rules(grant, server_names)}
    if {_rule_key(rule) for rule in rules} != expected_rules or len(rules) != len(expected_rules):
        findings.append(KiroV3Finding("render_mismatch", "permissions.rules differ from the grant"))
    ungranted = sorted(set(server_names) - set(granted_mcp_servers(grant, server_names)))
    if ungranted:
        findings.append(KiroV3Finding("mcp_server_not_granted", f"servers {ungranted}"))

    if launch_allowlist is not None and not findings:
        exceeding = tools_exceeding(grant, launch_allowlist)
        if exceeding:
            findings.append(
                KiroV3Finding("grant_exceeds_launch", f"{exceeding} beyond {launch_allowlist}")
            )
        effective = kiro_v3_effective_tools(tools, excluded, schema)
        exact = kiro_v3_exact_tools(grant)
        if effective != exact:
            findings.append(
                KiroV3Finding(
                    "effective_set",
                    f"{schema.engine} would expose {sorted(effective ^ exact)} beyond the grant",
                )
            )
    return findings


def validate_kiro_v3_agent_bytes(
    raw: bytes,
    schema: KiroV3EngineSchema,
    *,
    expected_name: str,
    launch_allowlist: Optional[List[str]] = None,
    render_schemas: Optional[Iterable[KiroV3EngineSchema]] = None,
) -> List[KiroV3Finding]:
    """Parse and validate one agent file's bytes; empty means valid."""
    data, findings = parse_kiro_v3_agent_bytes(raw)
    if data is None:
        return findings
    return validate_kiro_v3_agent(
        data,
        schema,
        expected_name=expected_name,
        launch_allowlist=launch_allowlist,
        render_schemas=render_schemas,
    )


def require_valid_for_every_pinned_schema(raw: bytes, *, expected_name: str, remedy: str) -> None:
    """Install-time gate: the bytes must pass every pinned schema, or nothing is written."""
    findings: List[KiroV3Finding] = []
    for schema in pinned_kiro_v3_schemas():
        for finding in validate_kiro_v3_agent_bytes(raw, schema, expected_name=expected_name):
            if finding not in findings:
                findings.append(finding)
    if findings:
        raise KiroV3InvalidAgentError(expected_name, findings, remedy)


__all__ = [
    "KiroV3Finding",
    "KiroV3InvalidAgentError",
    "MAX_AGENT_FILE_BYTES",
    "parse_kiro_v3_agent_bytes",
    "require_valid_for_every_pinned_schema",
    "validate_kiro_v3_agent",
    "validate_kiro_v3_agent_bytes",
]
