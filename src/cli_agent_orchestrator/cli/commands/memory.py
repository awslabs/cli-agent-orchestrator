"""Memory commands for CLI Agent Orchestrator CLI."""

import asyncio
import os
import re

import click

from cli_agent_orchestrator.models.memory import MemoryScope, MemoryType
from cli_agent_orchestrator.services.memory_service import MemoryService


def _get_memory_service() -> MemoryService:
    return MemoryService()


def _cwd_context() -> dict:
    """Build terminal context from current working directory for scope resolution."""
    return {"cwd": os.path.realpath(os.getcwd())}


def _run_async(coro):
    """Run an async coroutine synchronously."""
    return asyncio.run(coro)


_VALID_KEY_RE = re.compile(r"^[a-z0-9\-]+$")
_MAX_KEY_LENGTH = 60  # mirrors MemoryService._sanitize_key


def _validate_key(key: str) -> str:
    """Validate memory key. Only [a-z0-9-] up to 60 chars (matches service)."""
    if not _VALID_KEY_RE.match(key):
        raise click.BadParameter(
            f"Invalid key '{key}'. Keys may only contain lowercase letters, digits, and hyphens.",
            param_hint="'KEY'",
        )
    if len(key) > _MAX_KEY_LENGTH:
        raise click.BadParameter(
            f"Key '{key}' exceeds {_MAX_KEY_LENGTH}-character limit.",
            param_hint="'KEY'",
        )
    return key


@click.group()
def memory():
    """Manage CAO memories."""


@memory.command(name="list")
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    default=None,
    help="Filter by scope (global, project, session, agent, federated).",
)
@click.option(
    "--type",
    "memory_type",
    type=click.Choice([t.value for t in MemoryType], case_sensitive=False),
    default=None,
    help="Filter by memory type (user, feedback, project, reference).",
)
@click.option(
    "--all",
    "scan_all",
    is_flag=True,
    default=False,
    help="Show memories from all projects, not just the current working directory.",
)
def list_memories(scope, memory_type, scan_all):
    """List stored memories.

    By default shows global memories and memories for the current working directory.
    Use --all to show memories across all projects.
    """
    svc = _get_memory_service()
    try:
        terminal_context = {"cwd": os.path.realpath(os.getcwd())}
        memories = _run_async(
            svc.recall(
                scope=scope,
                memory_type=memory_type,
                limit=100,
                terminal_context=terminal_context,
                scan_all=scan_all,
            )
        )
    except Exception as e:
        raise click.ClickException(str(e))

    if not memories:
        click.echo("No memories found.")
        return

    # Table header
    header = f"{'KEY':<30} {'SCOPE':<10} {'TYPE':<12} {'TAGS':<20} {'UPDATED'}"
    click.echo(header)
    click.echo("-" * len(header))

    for mem in memories:
        updated = mem.updated_at.strftime("%Y-%m-%d %H:%M")
        tags = mem.tags if mem.tags else ""
        click.echo(f"{mem.key:<30} {mem.scope:<10} {mem.memory_type:<12} {tags:<20} {updated}")


@memory.command()
@click.argument("key")
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    default=None,
    help="Scope to search in. Searches all scopes if omitted.",
)
def show(key, scope):
    """Display full content of a memory."""
    _validate_key(key)
    svc = _get_memory_service()
    try:
        memories = _run_async(
            svc.recall(
                query=key, scope=scope, limit=100, terminal_context=_cwd_context(), scan_all=True
            )
        )
    except Exception as e:
        raise click.ClickException(str(e))

    # Find exact key match
    match = None
    for mem in memories:
        if mem.key == key:
            match = mem
            break

    if not match:
        raise click.ClickException(f"Memory '{key}' not found.")

    click.echo(f"Key:     {match.key}")
    click.echo(f"Scope:   {match.scope}")
    click.echo(f"Type:    {match.memory_type}")
    click.echo(f"Tags:    {match.tags or '(none)'}")
    click.echo(f"Created: {match.created_at.strftime('%Y-%m-%d %H:%M:%S')}")
    click.echo(f"Updated: {match.updated_at.strftime('%Y-%m-%d %H:%M:%S')}")
    click.echo(f"File:    {match.file_path}")
    click.echo()
    click.echo(match.content)


@memory.command()
@click.argument("key")
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    default="project",
    help="Scope of the memory to delete (default: project).",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def delete(key, scope, yes):
    """Delete a memory by key."""
    _validate_key(key)
    if not yes:
        click.confirm(f"Delete memory '{key}'?", abort=True)

    svc = _get_memory_service()
    try:
        deleted = _run_async(svc.forget(key=key, scope=scope, terminal_context=_cwd_context()))
    except Exception as e:
        raise click.ClickException(str(e))

    if deleted:
        click.echo(f"Deleted memory '{key}' (scope: {scope}).")
    else:
        raise click.ClickException(f"Memory '{key}' not found in scope '{scope}'.")


@memory.command()
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    required=True,
    help="Scope to clear (required). One of: global, project, session, agent, federated.",
)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def clear(scope, yes):
    """Clear all memories for a given scope. Requires --scope."""
    if not yes:
        click.confirm(f"Clear all {scope}-scoped memories?", abort=True)

    svc = _get_memory_service()
    ctx = _cwd_context()
    try:
        memories = _run_async(svc.recall(scope=scope, limit=1000, terminal_context=ctx))
    except Exception as e:
        raise click.ClickException(str(e))

    if not memories:
        click.echo(f"No {scope}-scoped memories to clear.")
        return

    deleted_count = 0
    for mem in memories:
        try:
            # Pass scope_id from the recalled memory so session/agent
            # deletes target the nested on-disk path (the CLI cwd
            # context lacks session_name/agent_profile).
            result = _run_async(
                svc.forget(
                    key=mem.key,
                    scope=scope,
                    terminal_context=ctx,
                    scope_id=mem.scope_id,
                )
            )
            if result:
                deleted_count += 1
        except Exception:
            click.echo(f"Warning: Failed to delete '{mem.key}'.", err=True)

    click.echo(f"Cleared {deleted_count} {scope}-scoped memory(ies).")


@memory.command(name="lint")
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    default=None,
    help="Restrict lint to one scope (default: all four).",
)
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format. JSON includes ISO-8601 detected_at per row.",
)
def lint_cmd(scope, out_format):
    """Run wiki lint detectors and print findings.

    Exit codes:
      0  no error-severity issues found
      1  one or more error-severity issues found
      2  CLI / project resolution failure (handled by Click)
    """
    import json as _json

    from cli_agent_orchestrator.services.wiki_lint import (
        compute_exit_code,
        run_lint,
    )

    svc = _get_memory_service()
    ctx = _cwd_context()
    try:
        # Resolve project_hash via the same chain `cao memory list` uses.
        project_hash = svc.resolve_scope_id("project", ctx) or "unknown"
    except Exception as e:
        raise click.ClickException(f"failed to resolve project identity: {e}")

    try:
        issues = _run_async(run_lint(project_hash, scope=scope))
    except Exception as e:
        raise click.ClickException(f"lint run failed: {e}")

    is_json = out_format.lower() == "json"

    # Emit a top-line completion summary for visibility even when the result
    # list is empty. Routed to stderr under --format json so stdout stays a
    # clean, parseable JSON stream.
    completion = next(
        (
            i.description
            for i in issues
            if i.issue_type == "lint_error" and i.description.startswith("lint_run_completed:")
        ),
        "lint_run_completed: 0/5",
    )
    click.echo(completion, err=is_json)

    # The completion summary is echoed above; drop it from the rendered
    # payload/table and the exit-code computation so it isn't duplicated and
    # the "No lint issues found." branch can still fire on a clean run.
    issues = [
        i
        for i in issues
        if not (i.issue_type == "lint_error" and i.description.startswith("lint_run_completed:"))
    ]

    if is_json:
        payload = [
            {
                "issue_type": i.issue_type,
                "key": i.key,
                "related_key": i.related_key,
                "description": i.description,
                "severity": i.severity,
                "detected_at": i.detected_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
            for i in issues
        ]
        click.echo(_json.dumps(payload, indent=2))
    else:
        if not issues:
            click.echo("No lint issues found.")
            raise click.exceptions.Exit(compute_exit_code(issues))
        header = f"{'SEVERITY':<8} {'TYPE':<18} {'KEY':<30} {'DETECTED':<22} DESCRIPTION"
        click.echo(header)
        click.echo("-" * len(header))
        for i in issues:
            ts = i.detected_at.strftime("%Y-%m-%d %H:%M:%SZ")
            click.echo(f"{i.severity:<8} {i.issue_type:<18} {i.key:<30} {ts:<22} {i.description}")

    raise click.exceptions.Exit(compute_exit_code(issues))


@memory.command(name="compact")
@click.option(
    "--scope",
    type=click.Choice([s.value for s in MemoryScope], case_sensitive=False),
    default="global",
    show_default=True,
    help="Scope to compact.",
)
@click.option(
    "--key",
    default=None,
    help="Compact a single topic unconditionally (default: all stale topics).",
)
def compact_cmd(scope, key):
    """Compact wiki topics with the LLM compiler (repair sweep).

    Compiles every topic whose article changed since its last compile —
    the catch-all for background compiles that were dropped, timed out, or
    lost a concurrency race. Drives the locally installed coding-agent CLI
    (claude / codex / kiro-cli); requires no API key. Compiles run one at a
    time and can take a minute or two each.
    """
    if key is not None:
        key = _validate_key(key)

    svc = _get_memory_service()
    ctx = _cwd_context()
    scope_id = None
    if scope != MemoryScope.GLOBAL.value:
        scope_id = svc.resolve_scope_id(scope, ctx)
        if scope_id is None:
            raise click.ClickException(f"could not resolve scope_id for scope '{scope}'")

    try:
        results = _run_async(svc.compact(scope=scope, scope_id=scope_id, key=key))
    except Exception as e:
        raise click.ClickException(f"compact failed: {e}")

    summary = results.pop("_summary", {})
    if not results:
        click.echo("Nothing to compact — all topics are up to date.")
        return
    for topic_key, status in sorted(results.items()):
        click.echo(f"{status:<22} {topic_key}")
    click.echo(f"\nSummary: {summary}")


@memory.command(name="heal")
@click.option(
    "--scope",
    # Only global/project are resolvable from cwd context here; session/agent
    # need a session_name/agent_profile that this CLI path cannot derive, so
    # offering them would only ever raise "could not resolve scope_id".
    type=click.Choice([MemoryScope.GLOBAL.value, MemoryScope.PROJECT.value], case_sensitive=False),
    default="project",
    show_default=True,
    help="Scope to heal (global or project).",
)
@click.option(
    "--apply",
    "do_apply",
    is_flag=True,
    default=False,
    help="Apply mutations. Without this flag, prints a dry-run plan only.",
)
@click.option(
    "--aggressive",
    is_flag=True,
    default=False,
    help="Enable destructive poison_frequency healing (requires --apply too).",
)
@click.option(
    "--issue-type",
    "issue_type",
    type=click.Choice(
        ["orphan_page", "contradiction", "stale_claim", "poison_frequency"],
        case_sensitive=False,
    ),
    default=None,
    help="Restrict healing to a single issue type.",
)
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format.",
)
def heal_cmd(scope, do_apply, aggressive, issue_type, out_format):
    """Repair wiki lint findings (orphan pages, contradictions, stale claims).

    Dry-run by DEFAULT — prints what would change. Pass --apply to mutate.
    poison_frequency healing additionally requires --aggressive.
    graph_density is flag-only and never mutated.
    """
    import json as _json

    from cli_agent_orchestrator.services import wiki_healer
    from cli_agent_orchestrator.services.wiki_lint import run_lint

    svc = _get_memory_service()
    ctx = _cwd_context()

    scope_id = None
    if scope != MemoryScope.GLOBAL.value:
        scope_id = svc.resolve_scope_id(scope, ctx)
        if scope_id is None:
            raise click.ClickException(f"could not resolve scope_id for scope '{scope}'")

    project_hash = scope_id or "unknown"

    try:
        issues = _run_async(run_lint(project_hash, scope=scope))
    except Exception as e:
        raise click.ClickException(f"lint run failed: {e}")

    if issue_type is not None:
        issues = [i for i in issues if i.issue_type == issue_type]

    try:
        report = _run_async(
            wiki_healer.heal(
                issues,
                scope=scope,
                scope_id=scope_id,
                apply=do_apply,
                aggressive=aggressive,
            )
        )
    except wiki_healer.HealConflictError as e:
        raise click.ClickException(str(e))
    except Exception as e:
        raise click.ClickException(f"heal failed: {e}")

    is_json = out_format.lower() == "json"
    if is_json:
        payload = {
            "scope": report.scope,
            "scope_id": report.scope_id,
            "apply": report.apply,
            "aggressive": report.aggressive,
            "dry_run_summary": report.dry_run_summary,
            "truncated_by_type": report.truncated_by_type,
            "truncated_run_level": report.truncated_run_level,
            "total_suppressed": report.total_suppressed,
            "actions": [
                {
                    "issue_type": a.issue_type,
                    "key": a.key,
                    "related_key": a.related_key,
                    "description": a.description,
                    "status": a.status,
                }
                for a in report.actions
            ],
        }
        click.echo(_json.dumps(payload, indent=2))
        return

    if report.dry_run_summary:
        click.echo(report.dry_run_summary)
    if not report.actions:
        click.echo("Nothing to heal.")
        return
    header = f"{'STATUS':<10} {'TYPE':<22} {'KEY':<30} DESCRIPTION"
    click.echo(header)
    click.echo("-" * len(header))
    for a in report.actions:
        click.echo(f"{a.status:<10} {a.issue_type:<22} {a.key:<30} {a.description}")
    if report.total_suppressed:
        click.echo(
            f"\n{report.total_suppressed} action(s) suppressed by caps "
            f"(by type: {report.truncated_by_type}, run-level: {report.truncated_run_level})."
        )
