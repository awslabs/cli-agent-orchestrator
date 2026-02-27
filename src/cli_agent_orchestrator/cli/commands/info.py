"""Info command for CLI Agent Orchestrator CLI."""

import subprocess

import click
import requests

from cli_agent_orchestrator.constants import (
    DATABASE_FILE,
    SERVER_HOST,
    SERVER_PORT,
    SESSION_PREFIX,
)


@click.command()
def info():
    """Display information about the current session."""
    try:
        # Display database path
        click.echo(f"Database path: {DATABASE_FILE}")

        # Try to get current session name from tmux
        session_name = None
        try:
            result = subprocess.run(
                ["tmux", "display-message", "-p", "#S"],
                capture_output=True,
                text=True,
                check=True,
            )
            session_name = result.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            pass

        if session_name and session_name.startswith(SESSION_PREFIX):
            try:
                # Call API to get session details
                url = f"http://{SERVER_HOST}:{SERVER_PORT}/sessions/{session_name}"
                response = requests.get(url)

                if response.status_code == 200:
                    data = response.json()
                    terminals = data.get("terminals", [])
                    click.echo(f"Session ID: {session_name}")
                    click.echo(f"Active terminals: {len(terminals)}")
                else:
                    click.echo(
                        f"Session ID: {session_name} (Warning: Session not found in CAO server)"
                    )
            except requests.exceptions.RequestException:
                click.echo(f"Session ID: {session_name} (Warning: Could not connect to CAO server)")
        else:
            click.echo("Not currently in a CAO session.")

    except Exception as e:
        raise click.ClickException(str(e))
