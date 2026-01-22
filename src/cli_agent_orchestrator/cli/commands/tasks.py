"""CLI commands for Beads task queue."""
import click
from cli_agent_orchestrator.clients.beads import BeadsClient

beads = BeadsClient()

@click.group()
def tasks():
    """Manage Beads task queue."""

@tasks.command("list")
@click.option("-s", "--status", help="Filter by status (open/wip/closed)")
@click.option("-p", "--priority", type=int, help="Filter by priority (1/2/3)")
def list_tasks(status, priority):
    """List tasks from queue."""
    for t in beads.list(status=status, priority=priority):
        s = {"open": "○", "wip": "●", "closed": "✓"}.get(t.status, "?")
        click.echo(f"P{t.priority} {s} [{t.id}] {t.title}")

@tasks.command("next")
@click.option("-p", "--priority", type=int, help="Priority filter")
def next_task(priority):
    """Show next priority task."""
    t = beads.next(priority=priority)
    if t:
        click.echo(f"P{t.priority} [{t.id}] {t.title}\n{t.description or '(no description)'}")
    else:
        click.echo("No open tasks")

@tasks.command("add")
@click.argument("title")
@click.option("-d", "--description", default="", help="Task description")
@click.option("-p", "--priority", type=int, default=2, help="Priority (1/2/3)")
def add_task(title, description, priority):
    """Add task to queue."""
    t = beads.add(title, description, priority)
    click.echo(f"Created [{t.id}] {t.title}")

@tasks.command("wip")
@click.argument("task_id")
@click.option("-a", "--assignee", help="Assignee name")
def wip_task(task_id, assignee):
    """Mark task in progress."""
    t = beads.wip(task_id, assignee)
    if t:
        click.echo(f"WIP: [{t.id}] {t.title}")
    else:
        click.echo("Task not found")

@tasks.command("close")
@click.argument("task_id")
def close_task(task_id):
    """Close completed task."""
    t = beads.close(task_id)
    if t:
        click.echo(f"Closed: [{t.id}] {t.title}")
    else:
        click.echo("Task not found")
