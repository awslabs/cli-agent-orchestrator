"""Deliver installed plugins' MCP servers into CAO's agent-profile shape.

This is the MCP counterpart to :mod:`projection`, and it exists for the same
reason: ``mcp_mapping`` can turn one plugin's ``mcp.json`` into CAO ``mcpServers``
entries, but *something* has to decide which of the installed plugins' servers an
agent actually receives, resolve name collisions by a rule, and hand the result
to the one place that already derives every provider's native MCP form. Without
this module the mapper's output has no consumer, which is exactly the state the
adoption audit found (finding R1).

Where the seam is
-----------------
``services/install_service.install_agent`` parses an agent profile, resolves the
target provider, and then runs CAO's own ``${VAR}`` resolution over
``profile.mcpServers`` before materializing each provider's config. Plugin
servers are merged into ``profile.mcpServers`` **between** those two steps:

* after provider resolution, because the transport matrix is provider-dependent
  (``mcp_mapping.PROVIDER_TRANSPORTS``) — OpenCode can only carry stdio, so a
  url-based plugin server must be skipped *for that provider only*;
* before CAO's resolution pass, because the whole purpose of the
  ``x-cao-pre-expanded`` marker is to be seen by that pass and skipped. Merging
  afterwards would leave the marker unread and un-stripped, and it would leak
  into provider config files.

Re-mapped, not persisted
------------------------
The mapped servers are recomputed from the installed ``PLUGIN_ROOT`` on every
profile build rather than stored in the install record. Both were available; this
one was chosen because a persisted mapping can go stale in ways nothing detects.
``PLUGIN_DATA`` paths are absolute and embedded in ``args``/``env``/``cwd``, so a
record written under one ``CAO_HOME_DIR`` would hand a provider paths that no
longer exist if the home moved; a plugin re-installed with ``--force`` from a
newer source would keep serving the old record's servers until something
invalidated it; and an operator who edited the installed tree would see the
record and the tree disagree with no signal. Re-mapping makes the delivered set a
pure function of what is on disk *now*, which is the same property
:mod:`projection` guarantees for skills by rebuilding rather than patching.

The cost is bounded and paid at the right time: it is one JSON read plus one
offline schema validation per installed plugin, on the **install/refresh** path.
Requirement 13.5's prohibition is on new *launch-time* filesystem work, and this
adds none — nothing here runs when a terminal starts.

Collision policy — identical in shape to skills
-----------------------------------------------
1. **A server name already in the profile always wins.** The profile is the
   operator's own declaration and CAO's built-in servers (``cao-mcp-server``)
   arrive that way; a plugin silently replacing one would be a privilege
   escalation dressed as a name clash.
2. **Among plugins, the lexicographically smallest plugin name wins**, exactly as
   in ``projection._elect_winners`` and for the same reason: ``installed_at``
   would make the delivered set depend on install *order*.

Losers are always reported. A dropped MCP server is invisible in a way a dropped
skill is not — a skill's absence shows up in a catalog, but a server that was
never registered simply does not answer — so silence here would be worse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Tuple

from cli_agent_orchestrator.agent_plugins.mcp_mapping import is_pre_expanded as is_plugin_mcp_entry
from cli_agent_orchestrator.agent_plugins.mcp_mapping import strip_marker as strip_plugin_mcp_marker
from cli_agent_orchestrator.agent_plugins.models import Finding, Severity
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpDeliveryResult:
    """Which plugin MCP servers an agent receives, and what was dropped."""

    servers: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    """Server name → CAO ``mcpServers`` entry, still carrying the pre-expanded
    marker. The marker is stripped by ``install_service`` immediately before a
    provider config is written, not here — this module's output is CAO-internal."""

    owners: Dict[str, str] = field(default_factory=dict)
    """Server name → the plugin that provided it. Powers provenance answers
    ("which plugin gave me this server?") without re-deriving the mapping."""

    findings: Tuple[Finding, ...] = ()

    @property
    def server_names(self) -> Tuple[str, ...]:
        return tuple(sorted(self.servers))


def collect_plugin_mcp_servers(
    *,
    provider: Optional[str] = None,
    store: Optional[InstalledPluginStore] = None,
) -> McpDeliveryResult:
    """Map every installed plugin's ``mcp.json`` and resolve plugin-vs-plugin clashes.

    Args:
        provider: Target provider, narrowing the transport matrix. ``None`` maps
            for CAO's internal shape without narrowing.
        store: Override the installed-plugin store (tests, alternate roots).

    Never raises. A store that cannot be read, a plugin whose ``mcp.json`` became
    unusable since install, or an outright bug in the mapper all degrade to "this
    plugin contributes no servers" plus a finding — because the caller is
    ``cao install <agent>``, and no agent install may fail because some unrelated
    plugin is broken.
    """
    # Imported here, not at module scope: ``mcp_mapping`` imports the pinned
    # schema loader from ``validation``, and ``validation`` reaches back into
    # this package's constants — a module-level import chain that is fine at call
    # time but circular at import time.
    from cli_agent_orchestrator.agent_plugins.mcp_mapping import load_and_map

    try:
        store = store or InstalledPluginStore()
        records = store.list_installed()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("Could not enumerate installed agent plugins for MCP delivery: %s", exc)
        return McpDeliveryResult()

    findings: List[Finding] = []
    # plugin name → {server name: config}. Built in plugin-name order so the
    # election below reads claims in the same total order the rule uses.
    claims: Dict[str, List[Tuple[str, Dict[str, Any]]]] = {}

    for record in records:
        try:
            result = load_and_map(
                store.plugin_root(record.name),
                store.plugin_data_dir(record.name),
                provider=provider,
                plugin_schema_id=record.schema_id or None,
            )
        except Exception as exc:  # pragma: no cover - mapper is total by contract
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp_delivery.mapping_failed",
                    spec_ref="§7.2.2",
                    message=(
                        f"Plugin '{record.name}' MCP configuration could not be mapped "
                        f"({exc}); its servers are not delivered. Its skills are unaffected."
                    ),
                    path=record.name,
                )
            )
            continue

        if not result.present:
            continue

        if not result.valid:
            # The install-time report already carried the detail. Re-stating it
            # here as one line per plugin keeps the refresh path honest about
            # having dropped something without replaying every sub-finding.
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp_delivery.unusable_config",
                    spec_ref="§7.2.2.2",
                    message=(
                        f"Plugin '{record.name}' has an unusable mcp.json; MCP is disabled "
                        f"for it and no server is delivered. Its skills are unaffected. "
                        f"Run `cao plugin validate` on it for the specific reason."
                    ),
                    path=record.name,
                )
            )
            continue

        # Entry-level findings (transport skips, credential warnings, containment
        # failures) belong to the caller's log, not swallowed here.
        findings.extend(result.findings)

        for server in result.servers:
            claims.setdefault(server.name, []).append((record.name, dict(server.config)))

    servers: Dict[str, Dict[str, Any]] = {}
    owners: Dict[str, str] = {}

    for server_name in sorted(claims):
        claimants = sorted(claims[server_name], key=lambda pair: pair[0])
        winner_plugin, winner_config = claimants[0]
        servers[server_name] = winner_config
        owners[server_name] = winner_plugin

        for loser_plugin, _ in claimants[1:]:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp_delivery.plugin_collision",
                    spec_ref="CAO policy",
                    message=(
                        f"MCP server '{server_name}' from plugin '{loser_plugin}' was not "
                        f"delivered: plugin '{winner_plugin}' declares the same server name "
                        f"and wins (lexicographically smallest plugin name)"
                    ),
                    path=server_name,
                )
            )

    return McpDeliveryResult(servers=servers, owners=owners, findings=tuple(findings))


def merge_plugin_mcp_servers(
    profile_servers: Optional[Mapping[str, Any]],
    *,
    provider: Optional[str] = None,
    store: Optional[InstalledPluginStore] = None,
) -> Tuple[Optional[Dict[str, Any]], McpDeliveryResult]:
    """Merge installed plugins' MCP servers into one profile's ``mcpServers``.

    Returns the merged dict (or ``None`` when there is nothing at all to
    declare, preserving the "no MCP" shape the providers already handle) and the
    delivery result, so the caller can log what was dropped.

    A name already present in ``profile_servers`` is left completely untouched:
    the plugin's entry is discarded with a finding rather than merged, renamed,
    or prefixed. Renaming would be worse than dropping — the plugin's own
    documentation, and any skill it ships that references the server by name,
    would be describing a server that no longer answers to that name.
    """
    delivery = collect_plugin_mcp_servers(provider=provider, store=store)

    existing = dict(profile_servers) if profile_servers else {}
    if not delivery.servers:
        return (existing or None), delivery

    findings = list(delivery.findings)
    merged = dict(existing)
    accepted: Dict[str, str] = {}

    for server_name in sorted(delivery.servers):
        if server_name in existing:
            findings.append(
                Finding(
                    severity=Severity.SKIPPED,
                    code="mcp_delivery.profile_collision",
                    spec_ref="CAO policy",
                    message=(
                        f"MCP server '{server_name}' from plugin "
                        f"'{delivery.owners[server_name]}' was not delivered: the agent profile "
                        f"already declares a server of that name, and the profile always wins"
                    ),
                    path=server_name,
                )
            )
            continue
        merged[server_name] = delivery.servers[server_name]
        accepted[server_name] = delivery.owners[server_name]

    return merged, McpDeliveryResult(
        servers={name: merged[name] for name in accepted},
        owners=accepted,
        findings=tuple(findings),
    )


def opencode_config_collision_finding(*, server_name: str, plugin: str) -> Finding:
    """Report a plugin server dropped to avoid clobbering a user's opencode.json entry.

    The "profile always wins" rule (``merge_plugin_mcp_servers``) is scoped to the
    *agent profile's* ``mcpServers``. OpenCode's shared ``opencode.json`` is a
    second namespace that rule never sees, so a plugin server whose name collides
    with a user's hand-written entry there would be silently overwritten. This is
    the install-side guard for it: the same drop-with-a-report policy, applied to
    the shared config. See design.md §10a (Finding 2).

    A stale CAO entry (written under a different ``CAO_HOME_DIR``) is
    indistinguishable from a user's without provenance, so it is reported here
    too rather than overwritten — the conservative side of the ambiguity.
    """
    return Finding(
        severity=Severity.SKIPPED,
        code="mcp_delivery.opencode_config_collision",
        spec_ref="CAO policy",
        message=(
            f"MCP server '{server_name}' from plugin '{plugin}' was not written to "
            f"opencode.json: an entry of that name already exists there and was not "
            f"placed by CAO, so overwriting it would destroy user configuration. The "
            f"plugin's server is dropped; rename the conflicting entry to deliver it."
        ),
        path=server_name,
    )


def log_delivery_findings(delivery: McpDeliveryResult, *, agent_name: str) -> None:
    """Log what MCP delivery dropped, at severities matched to the audience.

    Collisions and unusable configurations are surfaced at ``WARNING`` because an
    operator who installed a plugin expecting a tool needs to know it is not
    there. Everything else — credential-shape warnings, transport skips already
    reported at install time — is ``DEBUG``, so that re-materializing a dozen
    agents does not bury the log in duplicates of findings the install already
    printed once.
    """
    for finding in delivery.findings:
        if finding.code in _LOUD_CODES:
            logger.warning("Agent '%s': %s", agent_name, finding.message)
        else:
            logger.debug("Agent '%s': %s", agent_name, finding.message)


_LOUD_CODES = frozenset(
    {
        "mcp_delivery.plugin_collision",
        "mcp_delivery.profile_collision",
        "mcp_delivery.opencode_config_collision",
        "mcp_delivery.unusable_config",
        "mcp_delivery.mapping_failed",
    }
)


def apply_plugin_mcp_servers(
    profile: Any,
    *,
    provider: Optional[str] = None,
    persisted: bool = True,
    normalize_existing: bool = True,
    store: Optional[InstalledPluginStore] = None,
) -> McpDeliveryResult:
    """Merge plugin MCP servers into ``profile`` **in place** and normalise them.

    The single implementation of "what a profile's ``mcpServers`` looks like once
    Agent Plugins are taken into account", shared by the two places that need it:
    ``install_service`` (which persists provider config at install time) and
    :func:`with_plugin_mcp` (which is what the providers that re-read the profile
    at launch actually see).

    Extracted rather than duplicated because review on #584 found the two paths
    had *silently disagreed*: the merge existed only on the install path, so the
    five providers that call ``load_agent_profile()`` again at launch discarded
    it entirely. Two copies of this logic would be free to drift apart the same
    way; one copy cannot.

    ``persisted`` is passed through to CAO's own MCP resolution: ``True`` when the
    result is written to a config file a CLI reads later (prefer the stable PATH
    launcher), ``False`` when it is used for an in-memory launch command.

    ``normalize_existing`` controls whether the profile's **own** (non-plugin)
    entries are put through CAO's ``${VAR}`` resolution as well. ``True`` on the
    install path, which is where that resolution has always happened. ``False`` on
    the launch path, where the provider's own serializer is about to handle those
    entries and re-shaping them would change behaviour that has nothing to do
    with plugins — in particular it would turn a Pydantic ``McpServer`` into a
    plain dict and silently bypass the provider's ``model_dump`` path.

    Returns the delivery result so the caller can log findings. The profile is
    mutated because that is the shape every downstream serializer already reads.
    """
    merged, delivery = merge_plugin_mcp_servers(
        getattr(profile, "mcpServers", None), provider=provider, store=store
    )
    if merged:
        # An entry that came from an Agent Plugin's mcp.json is already fully
        # expanded by agent_plugins/mcp_mapping.py, which resolved exactly
        # ${PLUGIN_ROOT}/${PLUGIN_DATA} and deliberately left every other ${...}
        # literal. Re-running CAO's own resolution over it would expand those
        # literals too, which §9.2 forbids. The marker is stripped here so it
        # never reaches a provider's own config file.
        merged = {
            name: (
                strip_plugin_mcp_marker(cfg)
                if is_plugin_mcp_entry(cfg)
                else (
                    resolve_mcp_server_config(dict(cfg), persisted=persisted)
                    if normalize_existing
                    else cfg
                )
            )
            for name, cfg in merged.items()
        }
    profile.mcpServers = merged
    return delivery


def with_plugin_mcp(profile: Any, provider: Optional[str] = None) -> Any:
    """Return ``profile`` with the installed plugins' MCP servers merged in.

    The launch-time counterpart to the install-time merge, and the seam every
    provider that builds its MCP configuration **from a profile it re-read at
    launch** must pass through.

    Reproduced by review on #584: the merge lived only in ``install_service``,
    where it mutated the parsed profile in memory while ``_write_context_file``
    persisted the original raw text. Claude Code, Codex, Kimi, Antigravity and
    Cursor all call ``load_agent_profile()`` again when they build their launch
    command, so each discarded the merge and launched with no plugin server;
    Copilot never consulted the profile for MCP at all.

    Applied on **read** rather than persisted, which keeps the property that made
    re-mapping right in the first place: the expansions are absolute
    ``${PLUGIN_ROOT}``/``${PLUGIN_DATA}`` paths, so a persisted copy goes stale
    the moment ``CAO_HOME_DIR`` moves or a ``--force`` reinstall relocates the
    root. Recomputing here makes the delivered set a pure function of what is on
    disk now.

    Deliberately shaped as ``f(profile) -> profile`` wrapping the provider's
    existing ``load_agent_profile`` call rather than replacing that call with a
    combined loader: the providers' own tests patch ``load_agent_profile`` in each
    provider module, and a replacement would have silently escaped every one of
    those patches.

    ``persisted=False``: a launch command is built and used immediately, so the
    versioned executable path is the more precise choice. Never raises — a
    profile that launches without its plugin servers is degraded, but one that
    cannot launch at all is broken, and terminal creation is the wrong place to
    fail.
    """
    if profile is None:
        return profile
    try:
        delivery = apply_plugin_mcp_servers(
            profile, provider=provider, persisted=False, normalize_existing=False
        )
    except Exception as exc:
        logger.warning("Could not merge agent-plugin MCP servers for %s: %s", provider, exc)
        return profile
    log_delivery_findings(delivery, agent_name=getattr(profile, "name", None) or "unknown")
    return profile
