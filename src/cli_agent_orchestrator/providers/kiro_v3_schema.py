"""Pinned Kiro v3 engine schemas and the kiro-cli version allow-list (#1167 Phase 1).

Kiro ships its v3 agent engine (``@kiro/agent``) inside each kiro-cli release,
and engines differ in what an agent file may say: the rule-effect enum, the
capability set, tag membership. A rule one engine accepts can make another fall
back to its full-tool ``Default`` agent, and a misspelled capability is dropped
with only a log line. So CAO does not trust any version that merely advertises
the flags: each supported kiro-cli version maps to one pinned engine schema, and
strict mode refuses other versions before allocation. Observed admission may
borrow an eligible same-major schema by operator opt-in. v2 is never affected.

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

import re
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

    #: Static inventory only, not read by CAO's select(), render, granted tags
    #: or exact sets. This records tools beyond CAO's selection model.
    unmodelled_tools: Mapping[str, Tuple[frozenset[str], Optional[str]]] = field(
        default_factory=lambda: MappingProxyType({})
    )

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


# PENDING OPERATOR VERIFICATION. Derived statically, never run (#1167).
# A static comparison of the 0.66.70 and 0.66.91 engines found equal CAO tags
# and render-facing fields. This inventory is not read by CAO's select().
# See unmodelled_tools below for the process-tool tags and capabilities.
# The 0.66.70 inventory is not recorded here.
# Removed in 0.66.91: c2s_query, c2s_describe_function, c2s_describe_module,
# c2s_list_functions, c2s_list_modules, c2s_list_packages,
# c2s_traverse_functions and c2s_traverse_modules (spec/@builtin-only),
# c2s_render_architecture and subagent_response. Renders do not change,
# because CAO renders from its own tables. Of these tools, only
# c2s_render_architecture had a tag that CAO renders: write and shell in
# 0.66.70, when the c2s feature and KIRO_ARCHIFY_CLI_PATH enable it.
# _TAGS_0_66_70 does not list it.
# The c2s_show_component_diagram and c2s_show_system_overview tools remain.
# update_pbt_status also lost its fs_write capability (spec/@builtin-only);
# it likewise does not affect renders. The XDG_CACHE_HOME c2s store is unchanged.
# Windows execute_pwsh/control_pwsh_process remain shell-capability variants.
# MCP disclosure tool_load/tool_call/load_resource retain read, @builtin and
# @mcp-disclosure tags with no capability. They stay outside CAO's read map.
KAS_0_66_91 = KiroV3EngineSchema(
    engine="kas-0.66.91",
    agent_keys=KAS_0_66_70.agent_keys,
    v2_only_keys=KAS_0_66_70.v2_only_keys,
    rule_keys=KAS_0_66_70.rule_keys,
    effects=KAS_0_66_70.effects,
    capabilities=KAS_0_66_70.capabilities,
    meta_capabilities=KAS_0_66_70.meta_capabilities,
    mcp_server_fields=KAS_0_66_70.mcp_server_fields,
    tags=_TAGS_0_66_70,
    tool_ids=KAS_0_66_70.tool_ids,
    worst_case_selectors=KAS_0_66_70.worst_case_selectors,
    selects_nothing=KAS_0_66_70.selects_nothing,
    always_added=KAS_0_66_70.always_added,
    always_added_known=KAS_0_66_70.always_added_known,
    fallback_agents=KAS_0_66_70.fallback_agents,
    fallback_reasons=KAS_0_66_70.fallback_reasons,
    unmodelled_tools=MappingProxyType(
        {
            "control_bash_process": (frozenset({"shell", "@builtin"}), "shell"),
            "list_processes": (frozenset({"shell", "@builtin"}), None),
            "get_process_output": (frozenset({"shell", "@builtin"}), None),
            # _h("c2s") gated; code_index_build may write .kiro/specs/ under
            # KIRO_C2S_DEEP_ANALYSIS=1 and spend model credits.
            "code_index_build": (frozenset({"spec", "@builtin"}), "fs_read"),
            "code_index_explore": (frozenset({"spec", "@builtin"}), "fs_read"),
            # UNIVERSAL_BACKGROUND_TOOL / jobTools gated.
            "job_list": (frozenset({"@builtin"}), None),
            "job_cancel": (frozenset({"@builtin"}), None),
        }
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


#: Pinned kiro-cli versions for v3; strict admission refuses any other version.
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
        # PENDING OPERATOR VERIFICATION. Derived statically, never run, as for
        # 2.36.0: this release's installed engine tree (@kiro/agent 0.66.70, 693
        # files) is byte-identical to 2.35.1's, and its `chat --help` is
        # identical too. The CLI now ships as one binary, and its root --help
        # changed layout (the probe reads flags from both help screens). It
        # reuses KAS_0_66_70 unchanged, so it cannot widen anything: the render,
        # the tags and the rules are 2.35.1's. It only lets the v3 probe accept
        # 2.37.0; a launch on it needs the operator opt-in (#1167).
        "2.37.0": KiroCliVersionEntry(
            cli_version="2.37.0",
            schema=KAS_0_66_70,
            pending_operator_verification=True,
            basis=(
                "static: engine bundle identical to 2.35.1's; reuses KAS_0_66_70 unchanged, "
                "so it cannot widen the render, tags or rules; pending operator verification"
            ),
        ),
        # PENDING OPERATOR VERIFICATION. Derived statically, never run (#1167).
        "2.37.1": KiroCliVersionEntry(
            cli_version="2.37.1",
            schema=KAS_0_66_70,
            pending_operator_verification=True,
            basis=(
                "static: engine package byte-identical to 2.35.1's "
                "(acp-server.js sha256 "
                "bf79b2b0fa7ae68653abdf117b4a4dbf064fec6fa7ca49500115f3f2f67581b1, "
                "externals 99bf8b686ced7f999ffc2733565fdb92d895705272454445b46a6286db17b14c, "
                "package.json 017660a7415dc49d09c0d2503fc4a1e475c219a2641f1ed218996c499fb61e42, "
                "17-file package list a2c68bd4…); the tree differs from 2.37.0's in tslib "
                "only; reuses KAS_0_66_70 unchanged, so it cannot widen the render, tags "
                "or rules; pending operator verification"
            ),
        ),
        # PENDING OPERATOR VERIFICATION. Derived statically, never run, as for
        # 2.37.0. This release ships the same engine, @kiro/agent 0.66.70: the
        # 17-file @kiro/agent package, acp-server.js included, is byte-identical
        # to 2.35.1's and 2.37.0's. That package is a subset of the engine's
        # whole installed tree, and unlike 2.36.0's and 2.37.0's tree (693
        # files, identical to 2.35.1's), 3.0.0's tree has 694 files: the semver
        # bump (7.8.5 -> 7.8.6) added one new file and changed three others, and
        # the tslib bump (2.8.1 -> 2.9.1) changed five files with none added or
        # removed, so 693 + 1 = 694. Neither bundled library is loaded by
        # acp-server.js (semver is reached only through global-agent's
        # install-time script; tslib only through async-mutex inside
        # z3-solver, used for code-constraint analysis, not permissions).
        # `chat --help` and the root --help are identical to 2.37.0's, and
        # Kiro's legacy copies read the same in its TUI and its binary. It
        # reuses KAS_0_66_70 unchanged, so it cannot widen anything: the
        # render, the tags and the rules are 2.35.1's. It only lets the v3
        # probe accept 3.0.0; a launch on it needs the operator opt-in (#1167).
        "3.0.0": KiroCliVersionEntry(
            cli_version="3.0.0",
            schema=KAS_0_66_70,
            pending_operator_verification=True,
            basis=(
                "static: engine package byte-identical to 2.35.1's and 2.37.0's; the "
                "engine tree differs from 2.37.0's only in two bundled helper libraries "
                "(semver, tslib) that the engine doesn't load; reuses KAS_0_66_70 "
                "unchanged, so it cannot widen the render, tags or rules; pending "
                "operator verification"
            ),
        ),
        # PENDING OPERATOR VERIFICATION. Derived statically, never run (#1167).
        "3.1.0": KiroCliVersionEntry(
            cli_version="3.1.0",
            schema=KAS_0_66_91,
            pending_operator_verification=True,
            basis=(
                "static: a different engine, @kiro/agent 0.66.91 "
                "(acp-server.js sha256 "
                "e359aeb393a4a3c7daf8b64d9d457362bcccd9799c660e379b06c12e391fc30b, "
                "externals a7914c971f11604ae3b0f066220a57f1a35137f6dfa323b6e000d4ff6ef02db8, "
                "package.json f7e29484e475d65c4219935d31e6e5609232ff0561e1b4370a0675e6d30fe6c1), "
                "derived statically by a bounded review; tag maps equal to KAS_0_66_70's "
                "for CAO's tags, so renders are unchanged; pending operator verification"
            ),
        ),
    }
)


class KiroV3Refusal(ValueError):
    """Base of every Kiro v3 launch refusal; ``code`` is its provisional string.

    Each is raised before any task is typed: before allocation for the version,
    environment, agent-file and workspace checks, and after the header for the
    pane checks. The API maps every one to 400.
    """

    code = "kiro_v3_refused"


#: What a version refusal suggests by default. An ephemeral helper runs only on
#: v3, so its refusals pass their own remedy instead (#1167 Phase 2).
UNSUPPORTED_VERSION_REMEDY = "Retry with engine 'v2', or use a supported kiro-cli version."
UNVERIFIED_VERSION_REMEDY = "Retry with engine 'v2'."


class KiroV3UnsupportedVersionError(KiroV3Refusal):
    """The probed kiro-cli version has no pinned v3 engine schema."""

    code = "kiro_v3_unsupported_version"

    def __init__(self, version: Optional[str], *, remedy: str = UNSUPPORTED_VERSION_REMEDY) -> None:
        detected = f"kiro-cli {version}" if version else "an unreadable kiro-cli version"
        supported = ", ".join(sorted(SUPPORTED_KIRO_CLI_VERSIONS))
        super().__init__(
            f"{self.code}: Kiro engine 'v3' is not supported on {detected}. CAO supports "
            f"v3 only on kiro-cli {supported}, each with a pinned engine schema. {remedy}"
        )
        self.version = version


def kiro_v3_schema_for_cli_version(version: Optional[str]) -> KiroV3EngineSchema:
    """The pinned engine schema for ``version``; refuses any other version."""
    entry = SUPPORTED_KIRO_CLI_VERSIONS.get(version or "")
    if entry is None:
        raise KiroV3UnsupportedVersionError(version)
    return entry.schema


class KiroV3UnverifiedVersionError(KiroV3Refusal):
    """The probed kiro-cli version is allow-listed but not yet operator-verified."""

    code = "kiro_v3_unverified_version"

    def __init__(
        self,
        version: str,
        *,
        remedy: str = UNVERIFIED_VERSION_REMEDY,
        observed_version: Optional[str] = None,
    ) -> None:
        if observed_version is not None:
            message = (
                f"{self.code}: kiro-cli {observed_version} would borrow kiro-cli "
                f"{version}'s schema, which is pending operator verification, so CAO "
                f"does not launch v3 on it. {remedy}"
            )
        else:
            message = (
                f"{self.code}: kiro-cli {version}'s v3 engine schema was derived statically and "
                f"is pending operator verification, so CAO does not launch v3 on it. {remedy}"
            )
        super().__init__(message)
        self.version = version


def require_launchable_kiro_v3_version(
    version: Optional[str], *, allow_unverified: bool = False
) -> KiroCliVersionEntry:
    """The allow-list entry a v3 launch on ``version`` runs under.

    A version absent from the allow-list refuses whatever ``allow_unverified``
    says: it has no pinned schema. An entry still pending operator verification
    refuses unless ``allow_unverified`` is set, which only the operator's
    ``kiro.v3_allow_unverified_versions`` opt-in passes. Provisional (#1167).
    """
    entry = SUPPORTED_KIRO_CLI_VERSIONS.get(version or "")
    if entry is None:
        raise KiroV3UnsupportedVersionError(version)
    if entry.pending_operator_verification and not allow_unverified:
        raise KiroV3UnverifiedVersionError(entry.cli_version)
    return entry


class KiroV3ObservedRefusal(KiroV3Refusal):
    """An unlisted version failed an observed-admission condition."""

    code = "kiro_v3_observed_refused"

    def __init__(self, version: Optional[str], condition: str) -> None:
        super().__init__(
            f"{self.code}: kiro-cli {version or 'unknown'}: {condition}. "
            "Use a supported kiro-cli version, or have the operator qualify its schema "
            "and configure kiro.v3_allow_unverified_versions for pending rows."
        )
        self.version = version


def observed_kiro_v3_entry(
    version: Optional[str], *, allow_unverified: bool = False
) -> KiroCliVersionEntry:
    """Borrow the newest same-major predecessor for an unlisted version.

    Listed versions retain their strict launch rules. This selects a schema,
    never replaces the capability probe or any live launch check.
    """
    if version in SUPPORTED_KIRO_CLI_VERSIONS:
        return require_launchable_kiro_v3_version(version, allow_unverified=allow_unverified)
    if (
        not version
        or re.fullmatch(r"(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})\.(0|[1-9][0-9]{0,5})", version)
        is None
    ):
        raise KiroV3ObservedRefusal(version, "version must parse as MAJOR.MINOR.PATCH")
    try:
        parsed = tuple(int(part) for part in version.split("."))
    except ValueError:
        raise KiroV3ObservedRefusal(version, "version must parse as MAJOR.MINOR.PATCH") from None
    if parsed < (2, 35, 1):
        raise KiroV3ObservedRefusal(version, "version is below the 2.35.1 floor")
    if parsed >= (4, 0, 0):
        raise KiroV3ObservedRefusal(version, "version reaches the 4.0.0 ceiling")
    predecessors = [
        entry
        for entry in SUPPORTED_KIRO_CLI_VERSIONS.values()
        if int(entry.cli_version.split(".")[0]) == parsed[0]
        and tuple(int(part) for part in entry.cli_version.split(".")) <= parsed
    ]
    if not predecessors:
        raise KiroV3ObservedRefusal(version, "no same major schema row at or below this version")
    newest = max(
        predecessors, key=lambda entry: tuple(int(part) for part in entry.cli_version.split("."))
    )
    if newest.pending_operator_verification and not allow_unverified:
        raise KiroV3UnverifiedVersionError(
            newest.cli_version,
            observed_version=version,
            remedy=(
                "Use a supported verified kiro-cli version, or enable "
                "kiro.v3_allow_unverified_versions to admit this borrowed schema."
            ),
        )
    return newest


def pinned_kiro_v3_schemas() -> Tuple[KiroV3EngineSchema, ...]:
    """Every distinct pinned schema, in engine order. Install validates against all."""
    unique = {entry.schema.engine: entry.schema for entry in SUPPORTED_KIRO_CLI_VERSIONS.values()}
    return tuple(unique[engine] for engine in sorted(unique))
