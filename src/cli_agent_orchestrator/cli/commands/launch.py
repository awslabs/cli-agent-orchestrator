"""Launch command for CLI Agent Orchestrator CLI."""

import subprocess

import click
import requests

from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS, SERVER_HOST, SERVER_PORT
from cli_agent_orchestrator.clients.beads import BeadsClient


@click.command()
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider", default=DEFAULT_PROVIDER, help=f"Provider to use (default: {DEFAULT_PROVIDER})"
)
@click.option("--from-queue", is_flag=True, help="Auto-assign next task from Beads queue")
@click.option("--task", "task_id", help="Launch for specific Beads task ID")
@click.option("--priority", type=int, help="Priority filter for --from-queue (1/2/3)")
def launch(agents, session_name, headless, provider, from_queue, task_id, priority):
    """Launch cao session with specified agent profile."""
    try:
        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )

        # Handle Beads task assignment
        task = None
        if from_queue or task_id:
            beads = BeadsClient()
            if task_id:
                task = beads.get(task_id)
                if not task:
                    raise click.ClickException(f"Task {task_id} not found")
            elif from_queue:
                task = beads.next(priority=priority)
                if not task:
                    raise click.ClickException("No open tasks in queue")
            if task:
                beads.wip(task.id, assignee=agents)
                click.echo(f"Assigned task [{task.id}] {task.title}")

        # Call API to create session
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions"
        params = {
            "provider": provider,
            "agent_profile": agents,
        }
        if session_name:
            params["session_name"] = session_name

        response = requests.post(url, params=params)
        response.raise_for_status()

        terminal = response.json()

        click.echo(f"Session created: {terminal['session_name']}")
        click.echo(f"Terminal created: {terminal['name']}")

        # Attach to tmux session unless headless
        if not headless:
            subprocess.run(["tmux", "attach-session", "-t", terminal["session_name"]])

    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {str(e)}")
    except Exception as e:
        raise click.ClickException(str(e))
