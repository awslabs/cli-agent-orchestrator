"""Pinned Kiro v3 engine schemas and the kiro-cli version allow-list (#1167 Phase 1).

Kiro ships its v3 agent engine (``@kiro/agent``) inside each kiro-cli release,
and engines differ in what an agent file may say: the rule-effect enum, the
capability set, tag membership. A rule one engine accepts can make another fall
back to its full-tool ``Default`` agent, and a misspelled capability is dropped
with only a log line. So CAO does not trust any version that merely advertises
the flags: each supported kiro-cli version maps to one pinned engine schema, and
any other version refuses v3 before allocation. v2 is never affected.

Each schema records CAO's reading of one engine: the agent-file keys, the MCP
server fields, the rule keys, effects and capabilities, the selector-to-tool map
(worst case where the evidence is inferred), the tools added after exclusion,
and the fallback agent names. The renderer and validator use it; nothing here
runs Kiro. Adding a version is a maintainer change: one table row, plus a new
schema when the engine differs, backed by a static diff of the new bundle and
the operator checklist on that version.

Everything in this module is provisional for #1167 Phase 1.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Mapping, Optional, Tuple

#: The tags the v3 engine attaches to its built-in tools (``tt`` in the bundle).
#: Only these four select built-ins CAO maps; ``subagent`` selects the
#: per-agent dispatch tools, written ``subagent/<agent>``.
_TAGS_0_66_70: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        # ``code`` is tagged READ and WRITE (measured on 2.35.1: the ``read``
        # tag exposes it); ``introspect`` is READ too.
        "read": frozenset(
            {"read_file", "list_directory", "file_search", "grep_search", "code", "introspect"}
        ),
        "write": frozenset({"fs_write", "fs_append", "str_replace", "delete_file", "code"}),
        "shell": frozenset({"execute_bash"}),
        "web": frozenset({"web_fetch", "remote_web_search"}),
        "subagent": frozenset({"subagent/*"}),
    }
)


@dataclass(frozen=True)
class KiroV3EngineSchema:
    """CAO's pinned reading of one v3 engine version."""

    engine: str
    #: Top-level agent-file keys the engine's schema declares, with their types.
    agent_keys: Mapping[str, str]
    #: v2-only keys that make the engine skip a file lacking ``permissions``.
    v2_only_keys: frozenset[str]
    rule_keys: frozenset[str]
    #: Every effect the engine accepts. CAO emits only ``allow`` and ``deny``.
    effects: frozenset[str]
    #: Named capabilities, meta capabilities excluded.
    capabilities: frozenset[str]
    meta_capabilities: frozenset[str]
    mcp_server_fields: frozenset[str]
    tags: Mapping[str, frozenset[str]]
    #: Built-in tool ids a ``tools`` entry selects by its own name.
    tool_ids: frozenset[str]
    #: ``tools`` entries read worst case: what they could select beyond their
    #: own name. Kiro's docs say the 2.x name ``fs_read`` "maps to the read
    #: tag during migration", so it is read as the whole tag; ``web_search``
    #: (the display name of ``remote_web_search``) as the ``web`` tag.
    worst_case_selectors: Mapping[str, frozenset[str]]
    #: Entries known to select no built-in on this engine (``glob``, ``grep``:
    #: TESTED; the rest are CLI 2.x-only names with no v3 tool).
    selects_nothing: frozenset[str]
    #: Tools the engine adds after ``excludedTools`` is applied. Their members
    #: were not measured, so the set is empty and ``always_added_known`` False
    #: until a live run on that engine records them.
    always_added: frozenset[str] = field(default_factory=frozenset)
    always_added_known: bool = False
    #: The engine's fallback agent, as its header draws it and its notice names it.
    fallback_agents: frozenset[str] = field(default_factory=frozenset)
    #: The reasons in ``agent "<n>" <reason>, using "default"``.
    fallback_reasons: Tuple[str, ...] = ()

    def select(self, entry: str) -> frozenset[str]:
        """The built-in tool ids one ``tools`` entry may select, worst case.

        MCP references (``@server``, ``@server/tool``) select no built-in. An
        entry the schema does not know selects nothing it can name; the
        validator refuses unknown tag and meta selectors separately.
        """
        if entry.startswith("@"):
            return frozenset()
        selected: set[str] = set()
        if entry in self.tool_ids:
            selected.add(entry)
        selected.update(self.tags.get(entry, ()))
        selected.update(self.worst_case_selectors.get(entry, ()))
        return frozenset(selected)


KAS_0_66_70 = KiroV3EngineSchema(
    engine="kas-0.66.70",
    # The engine's custom-agent schema, read as text from the installed
    # bundle (``dist/server/acp-server.js``), never executed.
    agent_keys=MappingProxyType(
        {
            "name": "string",
            "description": "string | null",
            "prompt": "string | null",
            "tools": '"*" | string[]',
            "excludedTools": "string[]",
            "model": "string | null",
            "effortLevel": "string",
            "includeMcpJson": "boolean (default false)",
            "includePowers": "boolean (default false)",
            "mcpServers": "object",
            "resources": "unknown[]",
            "permissions": "object",
            "welcomeMessage": "string | null",
            "dispatchKind": '"sub-agent" | "custom-agent" | "spec"',
            "hooks": "array",
        }
    ),
    v2_only_keys=frozenset({"allowedTools", "toolsSettings"}),
    rule_keys=frozenset({"capability", "match", "exclude", "effect"}),
    effects=frozenset({"allow", "deny", "ask", "evaluate"}),
    capabilities=frozenset(
        {
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
    ),
    meta_capabilities=frozenset({"all", "builtin", "filesystem"}),
    mcp_server_fields=frozenset(
        {
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
    ),
    tags=_TAGS_0_66_70,
    tool_ids=frozenset(
        {
            "read_file",
            "list_directory",
            "file_search",
            "grep_search",
            "code",
            "fs_write",
            "fs_append",
            "str_replace",
            "delete_file",
            "execute_bash",
            "web_fetch",
            "remote_web_search",
            "invoke_sub_agent",
            "disclose_context",
            "introspect",
            "knowledge",
        }
    ),
    worst_case_selectors=MappingProxyType(
        {"fs_read": _TAGS_0_66_70["read"], "web_search": _TAGS_0_66_70["web"]}
    ),
    selects_nothing=frozenset({"glob", "grep", "use_aws", "goal", "todo_list"}),
    fallback_agents=frozenset({"Default", "default"}),
    fallback_reasons=(
        "not found",
        "needs upgrading for this agent engine",
        "has an invalid config",
    ),
)


@dataclass(frozen=True)
class KiroCliVersionEntry:
    """One allow-listed kiro-cli release and the engine schema it ships."""

    cli_version: str
    schema: KiroV3EngineSchema
    #: True when the entry was derived statically and no live run on this
    #: kiro-cli version has measured it yet. The operator checklist clears it.
    pending_operator_verification: bool
    basis: str


#: The kiro-cli versions CAO supports for v3. Any other version refuses v3.
SUPPORTED_KIRO_CLI_VERSIONS: Mapping[str, KiroCliVersionEntry] = MappingProxyType(
    {
        "2.35.1": KiroCliVersionEntry(
            cli_version="2.35.1",
            schema=KAS_0_66_70,
            pending_operator_verification=False,
            basis="measured live with engine 0.66.70 (#1167)",
        ),
        # PENDING OPERATOR VERIFICATION. Derived statically, never run: this
        # release's installed engine tree (@kiro/agent 0.66.70, 693 files) is
        # byte-identical to 2.35.1's, and its `chat --help` is identical too.
        # Only the TUI and the CLI binary changed. It reuses KAS_0_66_70
        # unchanged, so it cannot widen anything: the render, the tags and the
        # rules are 2.35.1's. It only lets the v3 probe accept 2.36.0; whether a
        # pending entry may admit a launch is decided at launch (#1167).
        "2.36.0": KiroCliVersionEntry(
            cli_version="2.36.0",
            schema=KAS_0_66_70,
            pending_operator_verification=True,
            basis=(
                "static: engine bundle identical to 2.35.1's; reuses KAS_0_66_70 unchanged, "
                "so it cannot widen the render, tags or rules; pending operator verification"
            ),
        ),
    }
)


class KiroV3UnsupportedVersionError(ValueError):
    """The probed kiro-cli version has no pinned v3 engine schema."""

    code = "kiro_v3_unsupported_version"

    def __init__(self, version: Optional[str]) -> None:
        detected = f"kiro-cli {version}" if version else "an unreadable kiro-cli version"
        supported = ", ".join(sorted(SUPPORTED_KIRO_CLI_VERSIONS))
        super().__init__(
            f"{self.code}: Kiro engine 'v3' is not supported on {detected}. CAO supports "
            f"v3 only on kiro-cli {supported}, each with a pinned engine schema. "
            "Retry with engine 'v2', or use a supported kiro-cli version."
        )
        self.version = version


def kiro_v3_schema_for_cli_version(version: Optional[str]) -> KiroV3EngineSchema:
    """The pinned engine schema for ``version``; refuses any other version."""
    entry = SUPPORTED_KIRO_CLI_VERSIONS.get(version or "")
    if entry is None:
        raise KiroV3UnsupportedVersionError(version)
    return entry.schema


def pinned_kiro_v3_schemas() -> Tuple[KiroV3EngineSchema, ...]:
    """Every distinct pinned schema, in engine order. Install validates against all."""
    unique = {entry.schema.engine: entry.schema for entry in SUPPORTED_KIRO_CLI_VERSIONS.values()}
    return tuple(unique[engine] for engine in sorted(unique))
