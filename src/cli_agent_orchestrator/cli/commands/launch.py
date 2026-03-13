"""Launch command for CLI Agent Orchestrator CLI."""

import os
import subprocess

import click
import requests

from cli_agent_orchestrator.constants import DEFAULT_PROVIDER, PROVIDERS, SERVER_HOST, SERVER_PORT

# Providers that require workspace folder access
PROVIDERS_REQUIRING_WORKSPACE_ACCESS = {
    "claude_code",
    "codex",
    "kiro_cli",
    "kimi_cli",
    "gemini_cli",
}


@click.command()
@click.option("--agents", required=True, help="Agent profile to launch")
@click.option("--session-name", help="Name of the session (default: auto-generated)")
@click.option("--headless", is_flag=True, help="Launch in detached mode")
@click.option(
    "--provider", default=DEFAULT_PROVIDER, help=f"Provider to use (default: {DEFAULT_PROVIDER})"
)
@click.option("--yolo", is_flag=True, help="Skip workspace trust confirmation")
def launch(agents, session_name, headless, provider, yolo):
    """Launch cao session with specified agent profile."""
    try:
        # Validate provider
        if provider not in PROVIDERS:
            raise click.ClickException(
                f"Invalid provider '{provider}'. Available providers: {', '.join(PROVIDERS)}"
            )
        working_directory = os.path.realpath(os.getcwd())

        # Ask for workspace trust confirmation for providers that need it.
        # Note: CAO itself does not access the workspace — it is the underlying
        # provider (e.g. claude_code, codex) that reads, writes, and executes
        # commands in the workspace directory.
        if provider in PROVIDERS_REQUIRING_WORKSPACE_ACCESS and not yolo:
            click.echo(
                f"The underlying provider ({provider}) will be trusted to perform all actions "
                f"(read, write, and execute) in:\n"
                f"  {working_directory}\n\n"
                f"To skip this confirmation, use: cao launch --yolo\n"
            )
            if not click.confirm("Do you trust all the actions in this folder?", default=True):
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
