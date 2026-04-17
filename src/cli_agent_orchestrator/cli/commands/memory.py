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


def _validate_key(key: str) -> str:
    """Validate memory key to prevent path traversal. Only [a-z0-9-] allowed."""
    if not _VALID_KEY_RE.match(key):
        raise click.BadParameter(
            f"Invalid key '{key}'. Keys may only contain lowercase letters, digits, and hyphens.",
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
            svc.recall(query=key, scope=scope, limit=100, terminal_context=_cwd_context(), scan_all=True)
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
            result = _run_async(svc.forget(key=mem.key, scope=scope, terminal_context=ctx))
            if result:
                deleted_count += 1
        except Exception:
            click.echo(f"Warning: Failed to delete '{mem.key}'.", err=True)

    click.echo(f"Cleared {deleted_count} {scope}-scoped memory(ies).")
