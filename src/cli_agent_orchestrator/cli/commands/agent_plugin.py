"""``cao plugin`` — manage Agent Plugins (the portable open specification).

Structured as a direct sibling of ``cli/commands/skills.py``: the same
``click.group()`` shape, the same ``raise click.ClickException(str(exc))``
convention, and the same post-mutation refresh of baked agent prompts.

**Naming is not settled.** design.md records the verb as maintainer decision
**M1** — ``cao plugin`` (recommended) versus ``cao agent-plugin`` — because
``docs/plugins.md`` publicly promises ``cao plugin list/info/enable/disable``
as a future surface for *event* plugins, and taking the noun retracts that
promise. This module is built and tested under the recommended verb as a
working placeholder. Per requirements.md 16.5, it must not ship to end users
until maintainers resolve M1; that gate blocks release, not construction, and
changing the verb is a one-line edit in ``cli/main.py`` plus this group's name.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import click

from cli_agent_orchestrator.agent_plugins.gate import agent_plugins_surface_enabled
from cli_agent_orchestrator.agent_plugins.installer import (
    PluginInstallError,
    affected_sessions,
    install,
    uninstall,
)
from cli_agent_orchestrator.agent_plugins.models import (
    AffectedSession,
    Finding,
    PluginSource,
    Severity,
)
from cli_agent_orchestrator.agent_plugins.projection import sweep_dangling_projections
from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

UNTRUSTED_CONTENT_WARNING = (
    "Installing an agent plugin runs untrusted code and content from that "
    "source: its skills become instructions injected into your agents' prompts. "
    "CAO implements no trust model, signing, or provenance verification for "
    "agent plugins — the Agent Plugins specification defers all three."
)

_SEVERITY_LABEL = {
    Severity.FATAL: "FATAL",
    Severity.SKIPPED: "SKIPPED",
    Severity.WARNING: "WARNING",
    Severity.INFO: "INFO",
}


def _looks_like_git(location: str) -> bool:
    """Whether a source string should be resolved as a repository.

    Deliberately syntactic: a URL scheme, an ``scp``-style SSH target, or a
    ``.git`` suffix. Anything else is a local path, which keeps a directory
    literally named ``github.com`` from being cloned instead of copied.
    """
    candidate = location.strip()
    if candidate.startswith(("http://", "https://", "git://", "ssh://", "git+")):
        return True
    if candidate.endswith(".git"):
        return True
    return "@" in candidate and ":" in candidate.split("@", 1)[1] and not Path(candidate).exists()


def _make_source(location: str, ref: Optional[str], subdir: Optional[str]) -> PluginSource:
    return PluginSource(
        kind="git" if _looks_like_git(location) else "path",
        location=location,
        ref=ref,
        subdir=subdir,
    )


def _echo_findings(findings: List[Finding]) -> None:
    """Render findings in severity order, each citing its specification clause."""
    if not findings:
        return
    order = [Severity.FATAL, Severity.SKIPPED, Severity.WARNING, Severity.INFO]
    for severity in order:
        for finding in (f for f in findings if f.severity is severity):
            location = f" [{finding.path}]" if finding.path else ""
            click.echo(
                f"  {_SEVERITY_LABEL[severity]:<8} {finding.spec_ref} "
                f"{finding.code}{location}: {finding.message}"
            )


def _echo_affected(affected: List[AffectedSession]) -> None:
    click.echo("The following live sessions reference skills this plugin provides:")
    for session in affected:
        click.echo(
            f"  session {session.session_name}  terminal {session.terminal_id}  "
            f"profile {session.profile_name}  skills: {', '.join(session.skill_names)}"
        )
    click.echo(
        "Removing it now can leave an agent that is mid-task holding a stale "
        "reference to a skill that no longer resolves."
    )


# Two separate gates, doing two different jobs. ``hidden=True`` keeps the verb out
# of ``cao --help`` because M1 has not settled what the verb should be called; the
# group callback below is Requirement 16.5's *execution* gate, because hiding help
# text leaves dispatch untouched and every subcommand still ran. Click has no
# built-in "disabled group", so the callback — which Click invokes before any
# subcommand — is the mechanism. When M1 lands, ``hidden`` flips and the gate's
# default flips; they are independent decisions.
@click.group("plugin", hidden=True)
def agent_plugin() -> None:
    """Manage agent plugins (Agent Plugins 1.0.0).

    Not to be confused with CAO's event-plugin system (see docs/plugins.md),
    which reacts to CAO lifecycle events and is managed separately.

    Hidden from ``cao --help`` and refusing to execute unless
    ``CAO_AGENT_PLUGINS_ENABLED`` is set, pending maintainer decision M1
    (Requirement 16.5).
    """
    if not agent_plugins_surface_enabled():
        raise click.ClickException(
            "The agent plugin surface is not released yet (Requirement 16.5, "
            "pending maintainer decision M1). Set CAO_AGENT_PLUGINS_ENABLED=1 to "
            "enable it."
        )


@agent_plugin.command("add")
@click.argument("source")
@click.option("--ref", default=None, help="Git branch or tag to clone (git sources only).")
@click.option("--subdir", default=None, help="Subdirectory of the source holding the plugin root.")
@click.option("--force", is_flag=True, help="Replace an already-installed plugin of the same name.")
@click.option("--dry-run", is_flag=True, help="Resolve and validate only; install nothing.")
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
def add(
    source: str,
    ref: Optional[str],
    subdir: Optional[str],
    force: bool,
    dry_run: bool,
    as_json: bool,
) -> None:
    """Install an agent plugin from a local path or a git URL."""
    try:
        if not as_json and not dry_run:
            click.echo(f"Warning: {UNTRUSTED_CONTENT_WARNING}", err=True)

        outcome = install(_make_source(source, ref, subdir), force=force, dry_run=dry_run)
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps(outcome.to_dict(), indent=2))
    else:
        report = outcome.report
        name = report.manifest.name if report.manifest else source
        if dry_run:
            click.echo(f"{name}: {'loadable' if report.loadable else 'NOT loadable'}")
        elif outcome.installed:
            projected = outcome.record.projected_skill_names if outcome.record else ()
            click.echo(
                f"Agent plugin '{name}' installed"
                + (f" ({len(projected)} skill(s) projected)" if projected else "")
            )
        else:
            click.echo(f"Agent plugin '{name}' was NOT installed", err=True)
        _echo_findings(list(outcome.findings))

    # A plugin that could not be loaded is a non-zero outcome: CI and the
    # fleet's completion predicate both branch on the exit status.
    if not outcome.report.loadable:
        raise SystemExit(1)


@agent_plugin.command("list")
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
def list_command(as_json: bool) -> None:
    """List installed agent plugins."""
    try:
        store = InstalledPluginStore()
        # Listing is also when CAO tidies up after an out-of-band store
        # mutation. It never raises, so it cannot fail the command.
        sweep_dangling_projections(store)
        records = store.list_installed()
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    if as_json:
        click.echo(json.dumps([record.to_dict() for record in records], indent=2))
        return

    if not records:
        click.echo("No agent plugins installed")
        return

    click.echo(f"{'Name':<32} {'Version':<12} {'Skills'}")
    click.echo("-" * 100)
    for record in records:
        skills = ", ".join(record.projected_skill_names) or "-"
        click.echo(f"{record.name:<32} {record.version or '-':<12} {skills}")

        unprojected = set(record.skill_names) - set(record.projected_skill_names)
        if unprojected:
            click.echo(f"{'':<32} {'':<12} not projected: {', '.join(sorted(unprojected))}")


@agent_plugin.command("validate")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable report.")
def validate(path: Path, as_json: bool) -> None:
    """Validate a candidate plugin directory. Installs nothing."""
    report = validate_plugin(path)

    if as_json:
        click.echo(json.dumps(report.to_dict(), indent=2))
    else:
        name = report.manifest.name if report.manifest else str(path)
        click.echo(f"{name}: {'loadable' if report.loadable else 'NOT loadable'}")
        if report.skills:
            click.echo(f"  skills: {', '.join(report.skill_names)}")
        if report.mcp_present:
            click.echo("  mcp.json: present")
        _echo_findings(list(report.findings))

    if not report.loadable:
        raise SystemExit(1)


@agent_plugin.command("remove")
@click.argument("name")
@click.option("--purge-data", is_flag=True, help="Also delete the plugin's persistent data.")
@click.option("--yes", is_flag=True, help="Skip the live-session confirmation prompt.")
def remove(name: str, purge_data: bool, yes: bool) -> None:
    """Remove an installed agent plugin.

    Warns — and never refuses — when a live session's profile references a skill
    this plugin provides. Blocking removal on any live session would make the
    store un-cleanable while a long session runs, and the operator may
    legitimately want the plugin gone.
    """
    try:
        store = InstalledPluginStore()
        if store.get(name) is None and not store.is_installed(name):
            raise click.ClickException(f"Agent plugin '{name}' is not installed.")

        affected = affected_sessions(name, store=store)
        if affected and not yes:
            _echo_affected(affected)
            click.confirm("Remove it anyway?", abort=True)

        outcome = uninstall(name, purge_data=purge_data)
    except click.Abort:
        raise
    except click.ClickException:
        raise
    except PluginInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Agent plugin '{name}' removed")
    if purge_data:
        click.echo("Its persistent data directory was deleted")
    _echo_findings(list(outcome.projection_findings))
