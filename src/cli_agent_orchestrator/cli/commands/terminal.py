"""Terminal commands for CLI Agent Orchestrator."""

import json
import os

import click
import requests

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import API_BASE_URL, TERMINAL_LOG_DIR


@click.group()
def terminal():
    """Manage CAO terminals."""


@terminal.command("restore")
@click.argument("terminal_id")
def restore(terminal_id: str):
    """Restore a deleted terminal from its snapshot.

    Creates a plain shell window in the original session at the original
    working directory and loads the saved scrollback history into the pane.
    The session must still exist.
    """
    snapshot_path = TERMINAL_LOG_DIR / f"{terminal_id}.snapshot.json"
    scrollback_path = TERMINAL_LOG_DIR / f"{terminal_id}.scrollback"

    if not snapshot_path.exists():
        raise click.ClickException(f"No snapshot found for terminal {terminal_id}")

    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except Exception as e:
        raise click.ClickException(f"Failed to read snapshot: {e}")

    session_name = snapshot["session_name"]
    working_directory = snapshot.get("working_directory")
    original_window = snapshot.get("window_name", terminal_id)

    # Verify session exists
    try:
        response = requests.get(f"{API_BASE_URL}/sessions/{session_name}")
        if response.status_code == 404:
            raise click.ClickException(
                f"Session '{session_name}' no longer exists. Cannot restore."
            )
        response.raise_for_status()
    except requests.exceptions.ConnectionError:
        raise click.ClickException("Failed to connect to cao-server")

    # Create a plain window (no agent) in the existing session
    # Pass the scrollback file as the initial command: cat prints it as output,
    # then exec replaces cat with the user's login shell (tmux-resurrect pattern).
    window_name = f"restored-{original_window}"
    login_shell = os.environ.get("SHELL", "bash")

    if scrollback_path.exists():
        window_shell = f"cat '{scrollback_path}'; exec {login_shell} -l"
    else:
        window_shell = f"exec {login_shell} -l"

    try:
        tmux_client.create_window(
            session_name,
            window_name,
            terminal_id,
            working_directory,
            window_shell=window_shell,
        )
    except Exception as e:
        raise click.ClickException(f"Failed to create window: {e}")

    click.echo(
        f"Restored terminal {terminal_id} as window '{window_name}' in session '{session_name}'"
    )
    click.echo(
        f"Original agent: {snapshot.get('agent_profile', 'N/A')} | Original window: {original_window}"
    )
    if working_directory:
        click.echo(f"Working directory: {working_directory}")
