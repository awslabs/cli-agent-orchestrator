"""Pure, fail-closed CAO policy compiler for Kiro Agent System profiles."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Literal, Mapping, Sequence

from cli_agent_orchestrator.constants import ROLE_TOOL_DEFAULTS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.kiro_kas import KASPermissions
from cli_agent_orchestrator.utils.tool_mapping import _get_role_defaults

PolicySource = Literal["allowedTools", "role", "default"]

# Canonical actions observed in the released Kiro wrapper. Aliases such as
# executeCmd and fsRead are deliberately not accepted as policy inputs.
CAO_TO_KAS_ACTIONS: Mapping[str, tuple[str, ...]] = {
    "fs_read": ("fs_read",),
    "fs_list": ("glob", "grep"),
    "fs_write": ("fs_write",),
    "fs_*": ("fs_read", "fs_write", "glob", "grep"),
    "execute_bash": ("execute_bash", "execute_cmd", "use_aws"),
    "web_fetch": ("web_fetch",),
    "@builtin": ("agent_crew", "use_subagent"),
}
KNOWN_KAS_ACTIONS = frozenset(
    action for actions in CAO_TO_KAS_ACTIONS.values() for action in actions
)
_MCP_REF_RE = re.compile(r"^@([A-Za-z0-9_-]{1,64})(?:/([A-Za-z0-9_.:*?-]{1,128}))?$")
_MCP_SERVER_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_SAFE_ACTION_RE = re.compile(r"^[A-Za-z0-9_.:*?/-]{1,256}$")
_MCP_CONFIG_FIELDS = frozenset({"type", "command", "args", "env", "timeout"})


@dataclass(frozen=True)
class KiroPolicyDiagnostic:
    """Stable, reviewable policy compiler diagnostic."""

    code: str
    message: str


class KiroPolicyError(ValueError):
    """Typed fail-closed policy compilation error."""

    def __init__(self, code: str, message: str):
        self.diagnostic = KiroPolicyDiagnostic(code=code, message=message)
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CompiledKiroPolicy:
    """Deterministic KAS visibility and permission result."""

    source: PolicySource
    unrestricted: bool
    visible_tools: tuple[str, ...]
    denied_tools: tuple[str, ...]
    permissions: KASPermissions

    @property
    def allow_rule_count(self) -> int:
        return sum(rule.startswith("permit(") for rule in self.permissions.rules)

    @property
    def deny_rule_count(self) -> int:
        return sum(rule.startswith("forbid(") for rule in self.permissions.rules)


def _validate_unique_strings(values: Sequence[str], field: str) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value for value in values):
        raise KiroPolicyError("malformed-policy", f"{field} must contain non-empty strings")
    if len(set(values)) != len(values):
        raise KiroPolicyError("contradictory-policy", f"{field} contains duplicate entries")
    return tuple(values)


def _resolve_allowed(profile: AgentProfile) -> tuple[tuple[str, ...], PolicySource]:
    if profile.allowedTools is not None:
        return _validate_unique_strings(profile.allowedTools, "allowedTools"), "allowedTools"

    if profile.role:
        try:
            role_tools = _get_role_defaults(profile.role)
        except (AttributeError, TypeError, ValueError) as exc:
            raise KiroPolicyError(
                "malformed-role-policy", f"role {profile.role!r} has invalid tool settings"
            ) from exc
        if role_tools is None:
            raise KiroPolicyError(
                "unknown-role",
                f"role {profile.role!r} has no configured policy; define it or set allowedTools",
            )
        return _validate_unique_strings(role_tools, f"role {profile.role!r}"), "role"

    return _validate_unique_strings(ROLE_TOOL_DEFAULTS["developer"], "developer default"), "default"


def _mcp_action(reference: str, servers: frozenset[str]) -> str:
    match = _MCP_REF_RE.fullmatch(reference)
    if not match:
        if reference.startswith("@"):
            raise KiroPolicyError(
                "unsafe-mcp-grant",
                f"{reference!r} must name an explicit @server/tool or @server/* scope",
            )
        raise KiroPolicyError("unknown-capability", f"unknown CAO capability {reference!r}")
    server, tool = match.groups()
    if server not in servers:
        raise KiroPolicyError(
            "unknown-mcp-server",
            f"{reference!r} grants a server that is not declared in mcpServers",
        )
    return f"mcp::{server}::{tool or '*'}"


def _validate_mcp_servers(profile: AgentProfile) -> frozenset[str]:
    servers = profile.mcpServers or {}
    for name, config in servers.items():
        if not _MCP_SERVER_RE.fullmatch(name):
            raise KiroPolicyError("malformed-mcp", f"unsafe MCP server name {name!r}")
        if not isinstance(config, Mapping):
            raise KiroPolicyError("malformed-mcp", f"MCP server {name!r} must be an object")
        unknown = set(config) - _MCP_CONFIG_FIELDS
        if unknown:
            raise KiroPolicyError(
                "unsupported-mcp-field",
                f"MCP server {name!r} has unsupported field(s): {', '.join(sorted(unknown))}",
            )
        if not isinstance(config.get("command"), str) or not config["command"]:
            raise KiroPolicyError(
                "malformed-mcp", f"MCP server {name!r} requires a non-empty command"
            )
        args = config.get("args")
        if args is not None and (
            not isinstance(args, list) or any(not isinstance(arg, str) for arg in args)
        ):
            raise KiroPolicyError("malformed-mcp", f"MCP server {name!r} args must be strings")
        env = config.get("env")
        if env is not None and (
            not isinstance(env, dict)
            or any(
                not isinstance(key, str) or not isinstance(value, str) for key, value in env.items()
            )
        ):
            raise KiroPolicyError(
                "malformed-mcp", f"MCP server {name!r} env must map strings to strings"
            )
        timeout = config.get("timeout")
        if timeout is not None and (not isinstance(timeout, int) or timeout <= 0):
            raise KiroPolicyError(
                "malformed-mcp", f"MCP server {name!r} timeout must be a positive integer"
            )
    return frozenset(servers)


def _expand(
    entries: Iterable[str],
    servers: frozenset[str],
) -> frozenset[str]:
    actions: set[str] = set()
    for entry in entries:
        if entry in CAO_TO_KAS_ACTIONS:
            actions.update(CAO_TO_KAS_ACTIONS[entry])
        elif entry.startswith("@"):
            actions.add(_mcp_action(entry, servers))
        elif entry in KNOWN_KAS_ACTIONS:
            actions.add(entry)
        else:
            raise KiroPolicyError("unknown-capability", f"unknown CAO or KAS action {entry!r}")
    return frozenset(actions)


def _cedar_rule(effect: Literal["permit", "forbid"], action: str) -> str:
    if not _SAFE_ACTION_RE.fullmatch(action):
        raise KiroPolicyError("serialization-error", f"unsafe action identity {action!r}")
    return f'{effect}(principal, action == Action::"{action}", resource);'


def compile_kiro_policy(profile: AgentProfile) -> CompiledKiroPolicy:
    """Compile one profile to deterministic KAS visibility and Cedar rules."""
    if profile.toolAliases:
        raise KiroPolicyError(
            "unsafe-aliases",
            "toolAliases are not emitted for KAS because alias permission equivalence is unproven",
        )
    if profile.toolsSettings:
        raise KiroPolicyError(
            "unsupported-settings",
            "toolsSettings field-level restrictions have no proven KAS representation",
        )

    allowed, source = _resolve_allowed(profile)
    denied = _validate_unique_strings(profile.deniedTools or [], "deniedTools")
    if "*" in allowed and len(allowed) != 1:
        raise KiroPolicyError(
            "contradictory-policy", "allowedTools '*' cannot be combined with narrower grants"
        )
    if "*" in denied:
        raise KiroPolicyError("contradictory-policy", "deniedTools cannot contain '*'")

    servers = _validate_mcp_servers(profile)
    denied_actions = _expand(denied, servers)
    unrestricted = allowed == ("*",)
    allowed_actions = frozenset(KNOWN_KAS_ACTIONS) if unrestricted else _expand(allowed, servers)
    effective_actions = allowed_actions - denied_actions

    declared_tools = profile.tools
    if declared_tools and declared_tools != ["*"]:
        declared = _expand(_validate_unique_strings(declared_tools, "tools"), servers)
        missing = effective_actions - declared
        if missing:
            raise KiroPolicyError(
                "contradictory-policy",
                "tools visibility omits granted action(s): " + ", ".join(sorted(missing)),
            )
        effective_actions &= declared

    if unrestricted:
        visible: tuple[str, ...] = ("*",)
        rules = tuple(
            [_cedar_rule("permit", "*")]
            + [_cedar_rule("forbid", action) for action in sorted(denied_actions)]
        )
        excluded: tuple[str, ...] = tuple(sorted(denied_actions))
    else:
        visible = tuple(sorted(effective_actions))
        builtin_denies = KNOWN_KAS_ACTIONS - effective_actions
        all_denies = frozenset(builtin_denies | denied_actions)
        rules = tuple(
            [_cedar_rule("permit", action) for action in sorted(effective_actions)]
            + [_cedar_rule("forbid", action) for action in sorted(all_denies)]
        )
        excluded = tuple(sorted(all_denies))

    permissions = KASPermissions(
        rules=list(rules),
        includePowers=False,
        excludedTools=list(excluded),
    )
    return CompiledKiroPolicy(
        source=source,
        unrestricted=unrestricted,
        visible_tools=visible,
        denied_tools=excluded,
        permissions=permissions,
    )
