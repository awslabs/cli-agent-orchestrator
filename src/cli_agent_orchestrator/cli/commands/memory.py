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
    help="Filter by scope (global, project, session, agent).",
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
    help="Scope to clear (required). One of: global, project, session, agent.",
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


# Scopes that participate in import/export. Mirrors
# _archive_format.IMPORTABLE_SCOPES: ``agent`` is banned (devsecops T11) and
# ``federated`` (PR #314) is a first-class scope value, so we cannot reuse the
# MemoryScope enum (which lacks ``federated`` and still lists ``agent``).
_IMPORTABLE_SCOPE_CHOICES = ["session", "project", "global", "federated"]


@memory.command(name="export")
@click.option(
    "--scope",
    type=click.Choice(_IMPORTABLE_SCOPE_CHOICES, case_sensitive=False),
    required=True,
    help="Scope to export. One of: session, project, global, federated.",
)
@click.option(
    "--scope-id",
    default=None,
    help="Explicit scope_id. Resolved from cwd when omitted (ignored for global).",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False),
    required=True,
    help="Output bundle path (must end in .tar.gz or .tgz).",
)
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format for the export report.",
)
def export_cmd(scope, scope_id, out_path, out_format):
    """Export a project's memories to a deterministic .tar.gz bundle.

    Resolves scope_id the same way ``cao memory heal``/``compact`` do: global
    carries no scope_id; other scopes resolve from the current working
    directory unless ``--scope-id`` is supplied. The bundle is content-hashed
    so import-side tampering is detectable.
    """
    import json as _json
    from pathlib import Path

    from cli_agent_orchestrator.services import memory_export

    scope = scope.lower()
    svc = _get_memory_service()
    ctx = _cwd_context()

    if scope == MemoryScope.GLOBAL.value:
        resolved_scope_id = None
    elif scope_id is not None:
        resolved_scope_id = scope_id
    else:
        resolved_scope_id = svc.resolve_scope_id(scope, ctx)
        if resolved_scope_id is None:
            raise click.ClickException(f"could not resolve scope_id for scope '{scope}'")

    try:
        report = _run_async(
            memory_export.export(
                scope,
                resolved_scope_id,
                output_path=Path(out_path),
            )
        )
    except Exception as e:
        raise click.ClickException(f"export failed: {e}")

    # The service honors a non-blocking promise: failures land in
    # report.errors rather than raising. Surface them as a CLI error.
    if report.errors:
        raise click.ClickException("export failed: " + "; ".join(str(x) for x in report.errors))

    if out_format.lower() == "json":
        click.echo(
            _json.dumps(
                {
                    "archive_path": str(report.archive_path),
                    "project_id": report.project_id,
                    "id_kind": report.id_kind,
                    "format_version": report.format_version,
                    "n_wiki_files": report.n_wiki_files,
                    "n_metadata_rows": report.n_metadata_rows,
                    "bytes_written": report.bytes_written,
                    "content_hash": report.content_hash,
                },
                indent=2,
            )
        )
    else:
        click.echo(f"Wrote {report.archive_path}")
        click.echo(f"  project_id:   {report.project_id} ({report.id_kind})")
        click.echo(f"  wiki files:   {report.n_wiki_files}")
        click.echo(f"  metadata rows: {report.n_metadata_rows}")
        click.echo(f"  bytes:        {report.bytes_written}")
        click.echo(f"  content_hash: {report.content_hash}")


@memory.command(name="import")
@click.argument("bundle", type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--target-project-id",
    "target_project_id",
    default=None,
    help=(
        "Pin the project_id that project-scoped rows import into. "
        "Resolved from the current working directory when omitted. "
        "(global/federated rows always import with a NULL scope_id.)"
    ),
)
@click.option(
    "--on-conflict",
    "conflict_policy",
    type=click.Choice(["skip", "replace", "merge"], case_sensitive=False),
    default="skip",
    show_default=True,
    help="How to handle keys that already exist.",
)
@click.option(
    "--apply",
    "apply_changes",
    is_flag=True,
    default=False,
    help="Apply the import. Without this flag the import is a dry-run plan.",
)
@click.option(
    "--format",
    "out_format",
    type=click.Choice(["table", "json"], case_sensitive=False),
    default="table",
    show_default=True,
    help="Output format for the import report.",
)
def import_cmd(bundle, target_project_id, conflict_policy, apply_changes, out_format):
    """Import memories from a bundle. Dry-run by default; pass --apply to write.

    Without ``--apply`` the command prints the plan (N new, M conflicts, K
    rejected) without touching any memory. Project-scoped rows import into the
    project_id resolved from the current working directory unless
    ``--target-project-id`` pins one explicitly.
    """
    import json as _json
    from pathlib import Path

    try:
        from cli_agent_orchestrator.services import memory_import
    except ImportError:
        raise click.ClickException(
            "memory import is not available in this build (import service not installed)."
        )

    conflict_policy = conflict_policy.lower()

    try:
        report = _run_async(
            memory_import.import_archive(
                Path(bundle),
                conflict_policy=conflict_policy,
                dry_run=not apply_changes,
                target_project_id=target_project_id,
            )
        )
    except click.ClickException:
        raise
    except Exception as e:
        # The cwd_hash-no-target refusal (issue #316) and other conflict /
        # refusal errors surface here. Give the operator the resolution.
        msg = str(e)
        if "cwd_hash" in msg or "target" in msg.lower():
            raise click.ClickException(
                f"import refused: {msg}. " "Re-run with --target-project-id to pin a destination."
            )
        raise click.ClickException(f"import failed: {msg}")

    actions = list(report.actions)
    rejections = list(report.rejections)
    _CONFLICT_DECISIONS = {
        "skip_existing",
        "replace_existing",
        "merge_winner_existing",
        "merge_winner_imported",
    }
    n_new = sum(1 for a in actions if a.decision == "insert")
    n_conflict = sum(1 for a in actions if a.decision in _CONFLICT_DECISIONS)
    n_rejected = len(rejections)

    if out_format.lower() == "json":
        click.echo(
            _json.dumps(
                {
                    "archive_path": str(report.archive_path),
                    "project_id_in_archive": report.project_id_in_archive,
                    "project_id_applied": report.project_id_applied,
                    "format_version": report.format_version,
                    "dry_run": report.dry_run,
                    "summary": {
                        "new": n_new,
                        "conflicts": n_conflict,
                        "rejected": n_rejected,
                    },
                    "actions": [
                        {
                            "key": a.key,
                            "scope": a.scope,
                            "scope_id": a.scope_id,
                            "decision": a.decision,
                            "reason": a.reason,
                        }
                        for a in actions
                    ],
                    "rejections": [
                        {"member": r.member, "reason": r.reason, "detail": r.detail}
                        for r in rejections
                    ],
                },
                indent=2,
            )
        )
    else:
        mode = "DRY-RUN (no changes written)" if report.dry_run else "APPLIED"
        click.echo(f"Import {mode}")
        click.echo(f"  archive project_id: {report.project_id_in_archive}")
        click.echo(f"  applied project_id: {report.project_id_applied}")
        click.echo(f"  plan: {n_new} new, {n_conflict} conflicts, {n_rejected} rejected")
        if actions:
            click.echo()
            header = f"{'DECISION':<10} {'SCOPE':<10} {'KEY':<30} REASON"
            click.echo(header)
            click.echo("-" * len(header))
            for a in actions:
                click.echo(f"{a.decision:<10} {a.scope:<10} {a.key:<30} {a.reason or ''}")
        if rejections:
            click.echo()
            click.echo("Rejections:")
            for r in rejections:
                click.echo(f"  {r.reason:<28} {r.member}  {r.detail or ''}")
        if report.dry_run and (actions or rejections):
            click.echo("\nRe-run with --apply to perform the import.")


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
