"""CLI commands for Ralph iterative loops."""
import click
from cli_agent_orchestrator.clients.ralph import RalphRunner

ralph = RalphRunner()

@click.group()
def ralph_cmd():
    """Manage Ralph iterative loops."""

@ralph_cmd.command("start")
@click.argument("prompt")
@click.option("-n", "--min-iter", type=int, default=3, help="Minimum iterations")
@click.option("-m", "--max-iter", type=int, default=10, help="Maximum iterations")
@click.option("-p", "--promise", default="COMPLETE", help="Completion promise phrase")
@click.option("-t", "--task-id", help="Link to Beads task ID")
@click.option("-d", "--work-dir", help="Working directory")
def start(prompt, min_iter, max_iter, promise, task_id, work_dir):
    """Start a Ralph loop."""
    s = ralph.start(prompt, min_iter, max_iter, promise, task_id, work_dir)
    click.echo(f"Started [{s.id}] iter 1/{s.maxIterations}")
    click.echo(f"Promise: {s.completionPromise}")

@ralph_cmd.command("status")
def status():
    """Show current Ralph loop status."""
    s = ralph.status()
    if not s or not s.active:
        click.echo("No active Ralph loop")
        return
    pct = int((s.iteration / s.maxIterations) * 100)
    bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
    click.echo(f"[{s.id}] {s.status.upper()}")
    click.echo(f"Iteration: {s.iteration}/{s.maxIterations} [{bar}]")
    click.echo(f"Prompt: {s.prompt[:60]}...")
    if s.previousFeedback:
        click.echo(f"Quality: {s.previousFeedback.get('qualityScore', '?')}/10")

@ralph_cmd.command("stop")
def stop():
    """Stop running Ralph loop."""
    if ralph.stop():
        click.echo("Ralph loop stopped")
    else:
        click.echo("No active loop to stop")
