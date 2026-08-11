"""Sequence resolve → validate → publish → project, so failure changes nothing.

The ordering is load-bearing, not incidental:

1. Resolve into staging.
2. Validate **the staging copy**. Not loadable → return the report, publish
   nothing.
3. Refuse a name already in the installed set unless ``force`` (mirroring
   ``cli/commands/skills.py::_install_skill_folder``'s ``FileExistsError`` +
   ``--force`` semantics).
4. Publish atomically (stage → rename).
5. Rebuild the projection from the whole installed set.
6. Refresh baked provider artifacts through the **existing**
   ``utils/skill_injection.refresh_all_cao_managed_agents()``, exactly as
   ``cao skills add/remove`` already does. Skipping this would leave Copilot
   ``.agent.md`` catalogs stale — the one delivery path baked at install time
   rather than read at launch.

Steps 1–2 are what ``dry_run`` performs, and ``dry_run`` is what CI, the
author-side dogfooding, and ``cao plugin validate`` all use.

The install record is written as part of step 4 rather than after step 5, so a
published plugin root is never recorded-less; step 5 then updates the record's
``projected_skill_names`` in place. The observable contract — nothing published
unless loadable, everything consistent afterwards — is unchanged.
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tempfile import mkdtemp
from typing import Dict, List, Optional, Tuple

from cli_agent_orchestrator.agent_plugins.models import (
    AffectedSession,
    Finding,
    InstallOutcome,
    PluginRecord,
    PluginSource,
    PluginValidationReport,
    Severity,
    UninstallOutcome,
)
from cli_agent_orchestrator.agent_plugins.projection import current_projection, rebuild_projection
from cli_agent_orchestrator.agent_plugins.resolver import ResolverError, resolve
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore, PluginStoreError
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

logger = logging.getLogger(__name__)


class PluginInstallError(RuntimeError):
    """Raised when an install cannot proceed for a reason that is not a finding.

    A source that cannot be reached and a name that is already taken are
    *operator* errors with an obvious next step, not plugin defects — they get
    an exception the CLI renders as a ``ClickException`` and the API as a 400,
    rather than a validation report full of findings that would imply the
    plugin itself was at fault.
    """


def install(
    source: PluginSource,
    *,
    force: bool = False,
    dry_run: bool = False,
    store: Optional[InstalledPluginStore] = None,
    skills_dir: Optional[Path] = None,
    refresh_agents: bool = True,
) -> InstallOutcome:
    """Install one Agent Plugin from ``source``.

    Args:
        source: Local directory or git repository to install from.
        force: Replace an already-installed plugin of the same name.
        dry_run: Resolve and validate only. Nothing is published, nothing is
            projected, and no agent artifact is refreshed.
        store: Override the installed-plugin store (tests, alternate roots).
        skills_dir: Override the projection target (tests).
        refresh_agents: Refresh baked Copilot/Q agent prompts after a
            successful install. Off in tests that have no agent tree.

    Returns:
        An :class:`InstallOutcome`. ``outcome.installed`` is ``False`` whenever
        the plugin was not loadable, and in that case the installed set and the
        skill projection are byte-identical to their state before the call
        (correctness property P4).

    Raises:
        PluginInstallError: The source was unreachable, or the name is already
            installed and ``force`` was not supplied. Nothing was published.
    """
    store = store or InstalledPluginStore()
    staging = Path(mkdtemp(prefix="cao-agent-plugin-"))

    try:
        try:
            resolved = resolve(source, staging)
        except ResolverError as exc:
            raise PluginInstallError(str(exc)) from exc

        report = validate_plugin(resolved.root)

        if not report.loadable or report.manifest is None:
            return InstallOutcome(report=report, installed=False, dry_run=dry_run)

        if dry_run:
            return InstallOutcome(report=report, installed=False, dry_run=True)

        name = report.manifest.name
        if store.get(name) is not None or store.is_installed(name):
            if not force:
                raise PluginInstallError(
                    f"Agent plugin '{name}' is already installed. " f"Use --force to replace it."
                )

        record = PluginRecord(
            name=name,
            version=report.manifest.version,
            source=source,
            resolved_ref=resolved.resolved_ref,
            installed_at=datetime.now(timezone.utc),
            schema_id=report.manifest.schema_id,
            skill_names=report.skill_names,
            projected_skill_names=(),
            findings=report.findings,
        )

        # Snapshot before publishing: writing the new record overwrites the
        # previous `projected_skill_names`, and the rebuild needs the prior
        # projection both to report a winner change and to recognize CAO's own
        # already-placed entries as managed rather than pre-existing.
        prior_projection = current_projection(store)

        # §9.1's persistent directory, created at install rather than at first
        # use so an *update* can never be the operation that first creates it.
        #
        # Ordering contract: this runs BEFORE `store.publish`, because publish is
        # the commit point and nothing that can fail may follow it except the
        # projection rebuild, which is retryable. Creating the data directory
        # afterwards meant an ENOSPC/EROFS/EACCES on the data volume left root +
        # record installed with no data directory and no projection, and the retry
        # was then refused as already-installed — the operator had to pass --force
        # to recover from a failure they did not cause. Failing here instead aborts
        # before any commit, so there is nothing to roll back. A data directory
        # left behind by a subsequent publish failure is inert: §9.1 has it persist
        # across reinstalls anyway, and `create=True` is idempotent.
        store.plugin_data_dir(name, create=True)

        try:
            store.publish(resolved.root, record, force=force)
        except PluginStoreError as exc:
            raise PluginInstallError(str(exc)) from exc

        projection = rebuild_projection(store, skills_dir=skills_dir, previous=prior_projection)

        if refresh_agents:
            _refresh_agent_artifacts()

        # Re-read so the outcome carries the record the projection just updated.
        final_record = store.get(name) or record
        return InstallOutcome(
            report=report,
            installed=True,
            record=final_record,
            projection_findings=projection.findings,
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def uninstall(
    name: str,
    *,
    purge_data: bool = False,
    store: Optional[InstalledPluginStore] = None,
    skills_dir: Optional[Path] = None,
    refresh_agents: bool = True,
) -> UninstallOutcome:
    """Remove an installed Agent Plugin and rebuild the projection.

    The pre-install store state is restored exactly, except that the plugin's
    ``PLUGIN_DATA`` directory persists unless ``purge_data`` is set — §9.1
    permits either, and CAO's rule is explicit so the idempotence property (P5)
    is decidable rather than ambiguous.

    This function does **not** prompt. The warn-and-confirm gate belongs to the
    callers that have a user to ask: see :func:`affected_sessions`, which both
    the CLI and the web panel consult before getting here.
    """
    store = store or InstalledPluginStore()

    record = store.get(name)
    if record is None and not store.is_installed(name):
        raise PluginInstallError(f"Agent plugin '{name}' is not installed.")

    affected = tuple(affected_sessions(name, store=store))

    # Snapshotted before the record is deleted, for the same reason install
    # snapshots before publishing.
    prior_projection = current_projection(store)

    try:
        removed = store.unpublish(name, purge_data=purge_data)
    except ValueError as exc:
        raise PluginInstallError(str(exc)) from exc

    projection = rebuild_projection(store, skills_dir=skills_dir, previous=prior_projection)

    if refresh_agents:
        _refresh_agent_artifacts()

    return UninstallOutcome(
        name=name,
        removed=removed,
        purged_data=purge_data,
        affected_sessions=affected,
        projection_findings=projection.findings,
    )


def affected_sessions(
    name: str,
    *,
    store: Optional[InstalledPluginStore] = None,
) -> List[AffectedSession]:
    """Live sessions whose profile references a skill this plugin projects.

    Removal is not symmetric with install, because **two providers read
    ``SKILL.md`` from disk mid-session**: Kiro CLI resolves the ``skill://``
    globs rooted at ``SKILLS_DIR`` that ``install_service`` writes, and OpenCode
    reads through the ``OPENCODE_CONFIG_DIR/skills`` symlink. Neither
    snapshots content at launch, so removing a plugin can pull a skill out from
    under an agent that is mid-task and about to load it.

    This is a **warning input, not a veto**. The operator may legitimately want
    the plugin gone, and refusing removal while any long session runs would make
    the store un-cleanable. Callers report what this returns and ask; they do
    not refuse.

    A profile with no ``skills`` filter receives the *full* catalog, so it
    references every projected skill — that case counts as affected. Never
    raises: a database or tmux hiccup must not block a removal.
    """
    store = store or InstalledPluginStore()
    record = store.get(name)
    projected = set(record.projected_skill_names) if record else set()
    if not projected:
        return []

    return _match_live_terminals(_snapshot_live_terminals(), projected)


@dataclass(frozen=True)
class _LiveTerminal:
    """One live terminal's identity plus the only profile field that matters here.

    A snapshot type rather than the profile object: holding profiles would keep a
    parsed model per terminal alive for no reason, and ``skills`` is the single
    field the match needs.
    """

    terminal_id: str
    session_name: str
    profile_name: str
    skill_filter: Optional[List[str]]


def _snapshot_live_terminals() -> List[_LiveTerminal]:
    """Enumerate live terminals and their skill filters, once.

    Never raises: a database or tmux hiccup must not block a removal, and must not
    fail a plugin listing either. A partial snapshot is better than an error,
    because the result is a *warning input* — under-reporting an affected session
    degrades the warning, while raising would break the operation entirely.
    """
    snapshot: List[_LiveTerminal] = []
    try:
        from cli_agent_orchestrator.clients.database import list_terminals_by_session
        from cli_agent_orchestrator.services.session_service import list_sessions
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        # One profile load per distinct profile name, not per terminal: several
        # terminals commonly run the same profile, and loading parses YAML.
        filters: Dict[str, Optional[List[str]]] = {}

        for session in list_sessions():
            session_name = str(session.get("id", ""))
            for terminal in list_terminals_by_session(session_name):
                profile_name = terminal.get("agent_profile")
                if not profile_name:
                    continue
                profile_name = str(profile_name)

                if profile_name not in filters:
                    try:
                        profile = load_agent_profile(profile_name)
                    except Exception:
                        # A terminal whose profile has since been deleted cannot
                        # be matched, but it also cannot be reported as safe, so
                        # it is simply absent from the snapshot.
                        filters[profile_name] = _UNRESOLVABLE
                    else:
                        filters[profile_name] = getattr(profile, "skills", None)

                skill_filter = filters[profile_name]
                if skill_filter is _UNRESOLVABLE:
                    continue

                snapshot.append(
                    _LiveTerminal(
                        terminal_id=str(terminal.get("id", "")),
                        session_name=session_name,
                        profile_name=profile_name,
                        skill_filter=skill_filter,
                    )
                )
    except Exception as exc:
        logger.warning("Could not enumerate live sessions for agent-plugin impact: %s", exc)

    return snapshot


#: Sentinel for "this profile could not be loaded", distinct from ``None``, which
#: legitimately means "no skills filter, so the profile receives every skill".
_UNRESOLVABLE: List[str] = []


def _match_live_terminals(snapshot: List[_LiveTerminal], projected: set) -> List[AffectedSession]:
    """Which snapshot entries reference any of ``projected``."""
    affected: List[AffectedSession] = []
    for entry in snapshot:
        matched = _referenced_skills(entry.skill_filter, projected)
        if matched:
            affected.append(
                AffectedSession(
                    terminal_id=entry.terminal_id,
                    session_name=entry.session_name,
                    profile_name=entry.profile_name,
                    skill_names=matched,
                )
            )
    return affected


def affected_sessions_by_plugin(
    *,
    store: Optional[InstalledPluginStore] = None,
) -> Dict[str, List[AffectedSession]]:
    """``affected_sessions`` for every installed plugin, with **one** live-state walk.

    ``GET /plugins`` needs this for each installed plugin at once. Calling
    :func:`affected_sessions` in a loop re-walked ``list_sessions()`` ×
    ``list_terminals_by_session()`` × ``load_agent_profile()`` per plugin — work
    that is identical every iteration, on an endpoint a UI panel polls. The
    adoption audit flagged it (finding R2) alongside the missing scope gate,
    because the two together made an unauthenticated quadratic endpoint.

    The walk is hoisted; the per-plugin part that genuinely differs (matching each
    plugin's projected skill names) stays per-plugin.
    """
    store = store or InstalledPluginStore()
    records = store.list_installed()
    if not records:
        return {}

    snapshot = _snapshot_live_terminals()
    return {
        record.name: _match_live_terminals(snapshot, set(record.projected_skill_names))
        for record in records
    }


def _referenced_skills(skill_filter: Optional[List[str]], projected: set) -> Tuple[str, ...]:
    """Which projected skills a profile's ``skills`` filter would advertise.

    ``None`` means "no filter", which ``build_skill_catalog`` reads as the full
    catalog — so every projected skill is referenced. Patterns are matched the
    same way ``build_skill_catalog`` matches them (case-sensitive fnmatch), so
    this check cannot disagree with what the agent actually received.
    """
    import fnmatch

    if skill_filter is None:
        return tuple(sorted(projected))
    matched = {
        skill
        for skill in projected
        for pattern in skill_filter
        if fnmatch.fnmatchcase(skill, pattern)
    }
    return tuple(sorted(matched))


def validate_source(
    source: PluginSource,
    *,
    store: Optional[InstalledPluginStore] = None,
) -> PluginValidationReport:
    """Resolve and validate a source without installing it.

    This is ``cao plugin validate`` and the ``POST /plugins/validate`` endpoint;
    it is exactly :func:`install` with ``dry_run=True``, exposed under a name
    that says so.
    """
    outcome = install(source, dry_run=True, store=store, refresh_agents=False)
    return outcome.report


def _refresh_agent_artifacts() -> None:
    """Re-materialize what an agent-plugin change invalidates. Never fatal.

    Two independent artifacts, for two different reasons:

    1. **Baked skill catalogs** (Copilot's ``.agent.md`` prompt) — a plugin's
       skills change the catalog text that was composed at install time.
    2. **Provider MCP configs** — ``mcpServers`` is written into each provider's
       config at install time and never re-read, so a plugin's servers reach
       existing agents only if those configs are rewritten. This is also what
       makes uninstall complete: without it, a removed plugin's servers stay
       configured, pointing at a ``PLUGIN_ROOT`` that no longer exists.

    Both are best effort and independently guarded: a failure to refresh prompts
    must not prevent the MCP refresh, and neither may fail the plugin operation
    that triggered them.
    """
    try:
        from cli_agent_orchestrator.utils.skill_injection import refresh_all_cao_managed_agents

        refresh_all_cao_managed_agents()
    except Exception as exc:
        logger.warning("Could not refresh installed agent prompts after plugin change: %s", exc)

    try:
        from cli_agent_orchestrator.services.install_service import (
            refresh_installed_agents_for_plugin_mcp,
        )

        refresh_installed_agents_for_plugin_mcp()
    except Exception as exc:
        logger.warning("Could not refresh provider MCP configs after plugin change: %s", exc)


def installed_findings(record: PluginRecord) -> Tuple[Finding, ...]:
    """Findings recorded at install time, for ``list`` and the web panel."""
    return tuple(record.findings)


def has_fatal(findings: Tuple[Finding, ...]) -> bool:
    """Whether any finding is fatal. Small helper shared by the CLI and API."""
    return any(finding.severity is Severity.FATAL for finding in findings)
