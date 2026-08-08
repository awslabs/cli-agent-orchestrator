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
    """The session's conductor, resolved over live terminals only.

    This used to return ``terminals[0]`` of the raw listing. With several
    stale rows in a session that reliably named a dead one and reported
    its status, which is a guaranteed disagreement with the dashboard
    rather than a race. A demoted row is now excluded outright rather than
    ranked last: ranking still picks a dead row when that is all there is,
    which is exactly the case that went wrong.
    """
    terminals = _get_terminals(session_name)
    if not terminals:
        raise click.ClickException(f"No terminals found for session '{session_name}'")
    # An absent lifecycle is not live. It used to default to ``live``,
    # which reads "we do not know" as "it is fine" — and the peer that
    # answers with no lifecycle at all is exactly the too-old server this
    # check exists for. Fail closed and say why, rather than selecting a
    # conductor on the strength of a field nobody sent.
    live = [t for t in terminals if t.get("lifecycle_state") == "live"]
    if not live:
        unanswered = [t for t in terminals if t.get("lifecycle_state") is None]
        if len(unanswered) == len(terminals):
            raise click.ClickException(
                f"No conductor can be resolved for session '{session_name}': the server "
                f"published no lifecycle for any of its {len(terminals)} terminals, so none "
                "of them can be shown to be live. A server that predates observed liveness "
                "cannot answer this, and guessing would name a dead row."
            )
        # Says what was found instead of silently substituting one of them:
        # the operator needs to know these rows exist and are finalizable,
        # not be handed one as though it were serving.
        demoted = ", ".join(
            f"{t.get('terminal_id', t.get('id'))}="
            f"{t.get('lifecycle_state') or 'no-lifecycle-published'}"
            for t in terminals
        )
        raise click.ClickException(
            f"No live conductor for session '{session_name}'; "
            f"{len(terminals)} superseded/dead rows ({demoted})"
        )
    return live[0], live


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


# --------------------------------------------------------------------------
# lifecycle
# --------------------------------------------------------------------------
#
# Over HTTP like the rest of this group, unlike `cao issue` and
# `cao attachment` which call their services directly. The reason differs:
# those exist to be usable when the server is the broken thing, whereas
# declaring a session paused is meaningless without a server — the supervisor
# that has to settle the fleet lives behind it.


def _lifecycle_url(session_name: str, *suffix: str) -> str:
    parts = "/".join(suffix)
    tail = f"/{parts}" if parts else ""
    return f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/lifecycle{tail}"


def _lifecycle_post(session_name: str, *suffix: str, **payload):
    response = requests.post(_lifecycle_url(session_name, *suffix), json=payload)
    if response.status_code >= 400:
        detail = ""
        try:
            detail = response.json().get("detail") or ""
        except Exception:  # noqa: BLE001 - a non-JSON body is still worth showing
            detail = response.text.strip()
        raise click.ClickException(f"{response.status_code}: {detail}")
    return response.json()


def _render(record: dict) -> None:
    click.echo(f"{record['session_name']}  {record['lifecycle']}")
    if record.get("restore_to"):
        click.echo(f"  restores to     {record['restore_to']}")
    if record.get("archived"):
        click.echo("  archived        yes")
    click.echo(f"  kind            {record.get('kind')}")
    if record.get("declared_by"):
        click.echo(f"  declared by     {record['declared_by']}")
    if record.get("pause_deadline_at"):
        overdue = " (OVERDUE)" if record.get("pause_overdue") else ""
        click.echo(f"  pause deadline  {record['pause_deadline_at']}{overdue}")
    if record.get("diverges"):
        click.echo(f"  WARNING         {record['diverges']}")
    if record.get("suppresses_marshal"):
        click.echo("  the fire marshal will not fire on this session")


@session.command(name="lifecycle")
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True)
def session_lifecycle_show(session_name, as_json):
    """Show what a session has declared it is doing."""
    try:
        response = requests.get(_lifecycle_url(session_name))
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    record = response.json()
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)


@session.command(name="complete")
@click.argument("session_name")
@click.option("--by", "declared_by", required=True, help="Who is declaring this. Recorded.")
@click.option("--note", default=None)
@click.option("--json", "as_json", is_flag=True)
def session_complete(session_name, declared_by, note, as_json):
    """Declare a session's goal achieved.

    A declaration, not a teardown: nothing is collected and no worker is
    retired. A mistaken `complete` that tore the fleet down would destroy
    the evidence needed to tell that it was mistaken.
    """
    record = _lifecycle_post(session_name, declared_by=declared_by, lifecycle="complete", note=note)
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)


@session.command(name="pause")
@click.argument("session_name")
@click.option("--by", "requested_by", required=True, help="Who is asking. Recorded.")
@click.option(
    "--deadline-seconds",
    default=None,
    type=int,
    help="How long the supervisor has to settle before the session returns to the marshal.",
)
@click.option("--note", default=None)
@click.option("--json", "as_json", is_flag=True)
def session_pause(session_name, requested_by, deadline_seconds, note, as_json):
    """Ask for a pause. Does not grant one.

    The session enters `pausing` immediately. Only the supervisor can say
    the fleet actually settled, because only the supervisor knows whether
    the work is at a resumable boundary — so this returns before the pause
    is real, and `cao session lifecycle` is how you watch for it.
    """
    payload = {"requested_by": requested_by, "note": note}
    if deadline_seconds is not None:
        payload["deadline_seconds"] = deadline_seconds
    record = _lifecycle_post(session_name, "pause-request", **payload)
    if not as_json:
        click.echo("pause requested; waiting for the supervisor to settle the fleet")
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)


@session.command(name="pause-settled")
@click.argument("session_name")
@click.option("--by", "declared_by", required=True, help="The supervisor declaring this.")
@click.option("--note", default=None)
@click.option("--json", "as_json", is_flag=True)
def session_pause_settled(session_name, declared_by, note, as_json):
    """The supervisor's half: the fleet is settled, the session is paused."""
    record = _lifecycle_post(session_name, "pause-settled", declared_by=declared_by, note=note)
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)


@session.command(name="resume")
@click.argument("session_name")
@click.option("--by", "declared_by", required=True)
@click.option("--note", default=None)
@click.option("--json", "as_json", is_flag=True)
def session_resume_working(session_name, declared_by, note, as_json):
    """Return a paused session to `working`.

    Only meaningful for a *paused* session, whose panes are still live. A
    `stopped` session has no panes to return to and needs a resume path
    that does not exist yet for any provider.
    """
    record = _lifecycle_post(session_name, declared_by=declared_by, lifecycle="working", note=note)
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)


@session.command(name="stop-impact")
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True)
def session_stop_impact(session_name, as_json):
    """What stopping this session would cost, per live worker."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/stop-impact"
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    impact = response.json()
    if as_json:
        click.echo(json.dumps(impact, indent=2))
        return
    _render_impact(impact)


def _render_impact(impact: dict) -> None:
    click.echo(f"{impact['live_workers']} live worker(s)")
    if impact.get("unreadable"):
        click.echo(f"  could not be read: {impact['unreadable']}")
        return
    if impact["not_resumable"]:
        click.echo("\nwill NOT come back:")
        for worker in impact["not_resumable"]:
            profile = worker.get("agent_profile") or "-"
            click.echo(
                f"  {worker['terminal_id']:<12} {worker['provider']:<14} {profile:<16} "
                f"{worker['reason']}"
            )
    if impact["resumable"]:
        click.echo("\nstructurally resumable:")
        for worker in impact["resumable"]:
            click.echo(f"  {worker['terminal_id']:<12} {worker['provider']}")
    if not impact.get("resume_machinery_available", False):
        click.echo(f"\n{impact['resume_machinery_reason']}")


@session.command(name="stop")
@click.argument("session_name")
@click.option("--by", "declared_by", required=True)
@click.option("--note", default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.option("--json", "as_json", is_flag=True)
def session_stop(session_name, declared_by, note, yes, as_json):
    """Stop a session: snapshot and tear down every pane.

    Shows what will not come back before asking. Each pane is snapshotted and
    collected; the lifecycle row is left `stopped` with its restore target, the
    forwarded environment, and the recovery/snapshot artifacts preserved for a
    future resume. Resume is not implemented yet, so this is currently one-way
    for every worker — proceeding is allowed, proceeding unknowingly is not.
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/stop-impact"
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    impact = response.json()

    if not yes:
        _render_impact(impact)
        click.confirm(f"\nStop {session_name} and collect all of its panes?", abort=True)

    record = _lifecycle_post(
        session_name,
        "stop",
        declared_by=declared_by,
        acknowledged_one_way=True,
        note=note,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render(record)
