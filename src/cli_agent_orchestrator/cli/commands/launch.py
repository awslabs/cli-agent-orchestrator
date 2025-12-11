"""Launch command for CLI Agent Orchestrator CLI."""

import subprocess

import click
import requests

from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS, SERVER_HOST, SERVER_PORT
from cli_agent_orchestrator.utils.context_files import get_context_provider


@click.command()
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider",
    help="Provider to use (default: provider in agent context or q_cli)",
)
def launch(agents, session_name, headless, provider):
    """Launch cao session with specified agent profile."""
    try:
        selected_provider = provider or get_context_provider(agents) or DEFAULT_PROVIDER

        # Validate provider
        if selected_provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{selected_provider}'. Available providers: {', '.join(PROVIDERS)}"
            )

        # Call API to create session
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions"
        params = {
            "provider": selected_provider,
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
