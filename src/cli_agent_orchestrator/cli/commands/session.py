"""Session commands for CLI Agent Orchestrator."""

import json
import sys
import time
from urllib.parse import quote

import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.utils.terminal import poll_until_done

# Default poll timeout for sync send (seconds). Pass --timeout to override.
_DEFAULT_SEND_TIMEOUT = 300


def _get_sessions():
    response = requests.get(f"{API_BASE_URL}/sessions")
    response.raise_for_status()
    return response.json()


def _get_terminals(session_name):
    response = requests.get(f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/terminals")
    response.raise_for_status()
    return response.json()


def _get_terminal(terminal_id):
    response = requests.get(f"{API_BASE_URL}/terminals/{terminal_id}")
    response.raise_for_status()
    return response.json()


def _get_terminal_output(terminal_id):
    response = requests.get(
        f"{API_BASE_URL}/terminals/{terminal_id}/output", params={"mode": "last"}
    )
    response.raise_for_status()
    return response.json()


def _resolve_conductor(session_name):
    terminals = _get_terminals(session_name)
    if not terminals:
        raise click.ClickException(f"No terminals found for session '{session_name}'")
    return terminals[0], terminals


@click.group()
def session():
    """Manage CAO sessions."""


@session.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_sessions(as_json):
    """List all active CAO sessions."""
    try:
        sessions = _get_sessions()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")

    if not sessions:
        if as_json:
            click.echo("[]")
        else:
            click.echo("No active sessions")
        return

    rows = []
    for s in sessions:
        try:
            terminals = _get_terminals(s["name"])
            conductor = terminals[0] if terminals else None
            if conductor:
                conductor = _get_terminal(conductor["id"])
            rows.append((s["name"], conductor, len(terminals)))
        except requests.exceptions.RequestException:
            continue

    if as_json:
        result = []
        for name, conductor, terminal_count in rows:
            result.append(
                {
                    "session": name,
                    "conductor": (
                        {
                            "id": conductor["id"],
                            "agent_profile": conductor.get("agent_profile"),
                            "provider": conductor.get("provider"),
                            "status": conductor.get("status"),
                        }
                        if conductor
                        else None
                    ),
                    "terminal_count": terminal_count,
                }
            )
        click.echo(json.dumps(result, indent=2))
    else:
        click.echo(f"{'SESSION':<25} {'CONDUCTOR':<12} {'STATUS':<15} {'TERMINALS':<10}")
        click.echo("-" * 65)
        for name, conductor, terminal_count in rows:
            conductor_id = conductor["id"] if conductor else "N/A"
            status = conductor.get("status", "N/A") if conductor else "N/A"
            click.echo(f"{name:<25} {conductor_id:<12} {status:<15} {terminal_count:<10}")


@session.command()
@click.argument("session_name")
@click.option("--terminal", "terminal_id", help="Target a specific terminal ID")
@click.option(
    "--workers",
    is_flag=True,
    help="Show all non-conductor terminals (ignored when --terminal is set)",
)
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(session_name, terminal_id, workers, as_json):
    """Show status of a session's conductor (or specific terminal)."""
    try:
        if terminal_id:
            target = _get_terminal(terminal_id)
            all_terminals = []
        else:
            conductor_raw, all_terminals = _resolve_conductor(session_name)
            target = _get_terminal(conductor_raw["id"])
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")

    try:
        output_data = _get_terminal_output(target["id"])
        last_output = output_data.get("output")
    except requests.exceptions.RequestException:
        last_output = None

    if as_json:
        result = {
            "session": session_name,
            "conductor": {
                "id": target["id"],
                "agent_profile": target.get("agent_profile"),
                "provider": target.get("provider"),
                "status": target.get("status"),
                "last_output": last_output,
            },
        }
        if workers and not terminal_id:
            result["workers"] = [
                {
                    "id": t["id"],
                    "agent_profile": t.get("agent_profile"),
                    "provider": t.get("provider"),
                    "status": t.get("status"),
                }
                for t in all_terminals[1:]
            ]
        click.echo(json.dumps(result, indent=2))
        return

    click.echo(f"Session:  {session_name}")
    click.echo(f"Terminal: {target['id']}")
    click.echo(f"Agent:    {target.get('agent_profile', 'N/A')}")
    click.echo(f"Provider: {target.get('provider', 'N/A')}")
    click.echo(f"Status:   {target.get('status', 'N/A')}")

    if last_output:
        lines = last_output.splitlines()
        truncated = lines[:20]
        click.echo("\nLast response:")
        click.echo("\n".join(truncated))
        if len(lines) > 20:
            click.echo(f"... ({len(lines) - 20} more lines)")
    else:
        click.echo("\nNo last response available")

    if workers and not terminal_id:
        worker_terminals = all_terminals[1:]
        if worker_terminals:
            click.echo(f"\n{'ID':<12} {'AGENT':<20} {'PROVIDER':<15} {'STATUS':<15}")
            click.echo("-" * 65)
            for t in worker_terminals:
                click.echo(
                    f"{t['id']:<12} {t.get('agent_profile', 'N/A'):<20} "
                    f"{t.get('provider', 'N/A'):<15} {t.get('status', 'N/A'):<15}"
                )
        else:
            click.echo("\nNo worker terminals")


@session.command()
@click.argument("session_name")
@click.argument("message")
@click.option("--terminal", "terminal_id", help="Send to a specific terminal ID")
@click.option(
    "--async", "is_async", is_flag=True, help="Send and return immediately without waiting"
)
@click.option(
    "--timeout",
    "timeout",
    type=int,
    default=None,
    help=f"Timeout in seconds (default: {_DEFAULT_SEND_TIMEOUT}s; ignored with --async)",
)
def send(session_name, message, terminal_id, is_async, timeout):
    """Send a message to a session's conductor (or specific terminal)."""
    try:
        if terminal_id:
            target_id = terminal_id
        else:
            conductor, _ = _resolve_conductor(session_name)
            target_id = conductor["id"]

        status_resp = requests.get(f"{API_BASE_URL}/terminals/{target_id}")
        status_resp.raise_for_status()
        current_status = status_resp.json().get("status")
        # "completed" is a valid pre-send state: the terminal has finished its
        # previous task and is ready to accept a new message.
        if current_status not in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
            raise click.ClickException(
                f"Terminal {target_id} is currently {current_status}. Wait for it to finish before sending."
            )

        response = requests.post(
            f"{API_BASE_URL}/terminals/{target_id}/input",
            params={"message": message},
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")

    if is_async:
        click.echo(f"Message sent to terminal {target_id}")
        return

    time.sleep(3)
    effective_timeout = timeout if timeout is not None else _DEFAULT_SEND_TIMEOUT
    interrupted = False
    try:
        poll_until_done(target_id, effective_timeout)
    except KeyboardInterrupt:
        interrupted = True

    try:
        output_resp = requests.get(
            f"{API_BASE_URL}/terminals/{target_id}/output",
            params={"mode": "last"},
        )
        output_resp.raise_for_status()
        output = output_resp.json().get("output", "")
        if output:
            click.echo(output)
    except requests.exceptions.RequestException:
        pass

    if interrupted:
        sys.exit(130)
