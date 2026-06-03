"""Shutdown command for CLI Agent Orchestrator."""

import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL


def _list_sessions():
    try:
        response = requests.get(f"{API_BASE_URL}/sessions")
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")


def _delete_session(name):
    try:
        response = requests.delete(f"{API_BASE_URL}/sessions/{name}")
        if response.status_code == 404:
            click.echo(f"Session '{name}' already removed", err=True)
            return False
        response.raise_for_status()
        return True
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")


@click.command()
@click.option("--all", "shutdown_all", is_flag=True, help="Shutdown all cao sessions")
@click.option("--session", help="Shutdown specific session")
def shutdown(shutdown_all, session):
    """Shutdown tmux sessions and cleanup terminal records."""

    if not shutdown_all and not session:
        raise click.ClickException("Must specify either --all or --session")

    if shutdown_all and session:
        raise click.ClickException("Cannot use --all and --session together")

    if shutdown_all:
        sessions = _list_sessions()
        sessions_to_shutdown = [s["name"] for s in sessions]
    else:
        sessions_to_shutdown = [session]

    if not sessions_to_shutdown:
        click.echo("No cao sessions found to shutdown")
        return

    for session_name in sessions_to_shutdown:
        try:
            if _delete_session(session_name):
                click.echo(f"✓ Shutdown session '{session_name}'")
        except click.ClickException as e:
            if not shutdown_all:
                raise
            click.echo(f"Error: {e.format_message()}", err=True)
