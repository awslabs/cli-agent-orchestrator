"""Launch command for CLI Agent Orchestrator CLI."""

import os
import subprocess

import click
import requests

from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS, SERVER_HOST, SERVER_PORT

# Providers that require workspace folder access
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {"claude_code", "codex", "kiro_cli"}


@click.command()
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider", default=DEFAULT_PROVIDER, help=f"Provider to use (default: {DEFAULT_PROVIDER})"
)
@click.option("--yes", "-y", is_flag=True, help="Skip workspace access confirmation")
def launch(agents, session_name, headless, provider, yes):
    """Launch cao session with specified agent profile."""
    try:
        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )

        working_directory = os.path.realpath(os.getcwd())

        # Ask for workspace access confirmation for providers that need it.
        # Note: CAO itself does not access the workspace — it is the underlying
        # provider (e.g. claude_code, codex) that reads and writes files there.
        if provider in PROVIDERS_REQUIRING_WORKSPACE_ACCESS and not yes:
            click.echo(
                f"Note: CAO does not access your workspace directly. "
                f"The underlying provider ({provider}) will read and operate in:\n"
                f"  {working_directory}\n"
            )
            if not click.confirm("Allow provider workspace access?", default=True):
                raise click.ClickException("Launch cancelled by user")

        # Call API to create session
        url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions"
        params = {
            "provider": provider,
            "agent_profile": agents,
            "working_directory": working_directory,
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
