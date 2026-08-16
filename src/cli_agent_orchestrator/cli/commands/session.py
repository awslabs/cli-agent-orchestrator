"""Session commands for CLI Agent Orchestrator."""

import json
import sys
import time
import uuid
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


# --------------------------------------------------------------------------
# fleet cohort controls (M3-C C4)
# --------------------------------------------------------------------------
#
# Separate commands rather than `--force`, for the same reason the API has
# separate routes: a flag is something shell history repeats and a wrapper
# script sets once. `cao session cohort stop-force` cannot be reached by
# somebody who typed `stop-safe` and hit the up arrow.


def _cohort_url(session_name: str, *suffix: str) -> str:
    parts = "/".join(suffix)
    return f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/cohort/{parts}"


def _cohort_post(session_name: str, *suffix: str, **payload):
    payload.setdefault("operation_id", str(uuid.uuid4()))
    try:
        response = requests.post(_cohort_url(session_name, *suffix), json=payload)
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or ""
        except Exception:  # noqa: BLE001 - a non-JSON body is still worth showing
            detail = response.text.strip()
        raise click.ClickException(f"{response.status_code}: {detail}")
    return response.json()


_OUTCOME_LABELS = {
    "restored-exact": "restored on its own session",
    "restored-fresh": "restored fresh",
    "failed": "did NOT come back",
    "unresumable": "no resume path",
    "reconciliation-required": "needs reconciliation",
}


def _render_operation(record: dict) -> None:
    provenance = record.get("provenance") or {}
    mode = provenance.get("current_mode", record.get("current_mode"))
    kind = provenance.get("operation_kind", record.get("operation_kind"))
    click.echo(f"{kind} ({mode})  {record['state']}")
    click.echo(f"  operation       {record['operation_id']}")
    if provenance.get("promoted_to_force"):
        # Never let a promoted operation read as the safe one it started as.
        click.echo(
            f"  PROMOTED        safe -> force by {provenance.get('promoted_by')} "
            f"(receipt {str(provenance.get('promotion_receipt_digest'))[:12]}…)"
        )
    click.echo(f"  lifecycle epoch {provenance.get('lifecycle_epoch')}")
    click.echo(f"  roster revision {str(provenance.get('roster_revision', ''))[:12]}…")
    click.echo(
        f"  initiated by    {provenance.get('initiated_by')} "
        f"({provenance.get('initiator_kind')})"
    )
    if provenance.get("source_operation_id"):
        click.echo(f"  resumes         {provenance['source_operation_id']}")
        click.echo(f"  restores to     {provenance.get('resume_target')}")
    outcomes = provenance.get("member_outcomes") or {}
    if outcomes:
        click.echo("\nmembers:")
        for state, count in sorted(outcomes.items()):
            click.echo(f"  {count:>3}  {state:<26} {_OUTCOME_LABELS.get(state, '')}")
    failed = [
        item
        for item in (provenance.get("continuity") or [])
        if item["final_state"] in {"failed", "unresumable"}
    ]
    if failed:
        click.echo("\ndid not come back:")
        for item in failed:
            click.echo(
                f"  {item['terminal_id'] or '-':<12} {item['harness'] or '-':<14} "
                f"{item['final_state']}"
            )
    for retry in provenance.get("retries") or []:
        click.echo(
            f"\nretried by {retry['actor']} at {retry['created_at']} "
            f"(receipt {str(retry.get('receipt_digest'))[:12]}…)"
        )
    if provenance.get("retryable"):
        # Never let an unfinished operation read as a finished one. The
        # reason and the exact retry command are printed together because an
        # operator who cannot see how to continue reaches for force-Stop.
        click.echo(f"\nNOT FINISHED: {provenance.get('reconciliation_reason') or record['state']}")
        click.echo(
            f"  continue it:  cao session cohort resume-retry "
            f"{provenance.get('session_name')} --operation {record['operation_id']} --by <you>"
        )


@session.group(name="cohort")
def session_cohort():
    """Whole-fleet Pause, Stop, and Resume.

    Distinct from `cao session pause`, which asks the supervisor to settle the
    fleet and changes only the declared lifecycle. These commands act on every
    member of the cohort and record a durable operation you can read back.

    `pause-safe` takes a `--drain`, not a digest. A safe Pause consumes M3-D's
    drain receipt *and* its per-member classification, and neither is
    something a person can honestly type at a prompt — so the command names
    the durable drain and the server reads both from it. Run `cao session
    drain` first; it prints the exact command to spend the receipt.
    """


@session_cohort.command(name="pause-force")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True, help="Who is asking. Recorded.")
@click.option("--reason", default=None)
@click.option("--yes", "-y", is_flag=True, help="Skip the confirmation.")
@click.option("--json", "as_json", is_flag=True)
def cohort_pause_force(session_name, initiated_by, reason, yes, as_json):
    """Interrupt every worker's current turn, then the supervisor's.

    In-flight background work a worker started may be lost. Commits only on
    positive proof each turn actually stopped; anything unproven lands in
    reconciliation rather than being called paused.
    """
    if not yes:
        click.confirm(f"Interrupt every running turn in {session_name}?", abort=True)
    record = _cohort_post(
        session_name,
        "pause",
        "force",
        initiated_by=initiated_by,
        reason=reason,
        acknowledged_interrupt=True,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session_cohort.command(name="stop-safe")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True)
@click.option(
    "--drain",
    "drain_id",
    default=None,
    help="The complete STOP drain whose receipt this Stop spends. `cao session drains` lists them.",
)
@click.option(
    "--drain-receipt",
    default=None,
    help="The receipt of that stop drain, if you already hold the digest rather than the id.",
)
@click.option("--reason", default=None)
@click.option("--yes", "-y", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def cohort_stop_safe(session_name, initiated_by, drain_id, drain_receipt, reason, yes, as_json):
    """Stop once the fleet has drained to a boundary.

    Requires a *stop* drain. A Pause drain proves workers reached a boundary;
    only a Stop drain also records CAO's intent to collect each pane before it
    disappears, so a Pause receipt is refused here rather than quietly
    collecting panes nobody announced.
    """
    if not drain_id and not drain_receipt:
        raise click.ClickException(
            "safe Stop spends a stop drain; pass --drain <id> (or --drain-receipt <digest>)"
        )
    if drain_id and not drain_receipt:
        drain_receipt = _api_get(f"/drains/{quote(drain_id, safe='')}")["receipt_digest"]
        if not drain_receipt:
            raise click.ClickException(
                f"drain {drain_id} has no receipt; it did not prove a boundary"
            )
    _confirm_stop(session_name, yes)
    record = _cohort_post(
        session_name,
        "stop",
        "safe",
        initiated_by=initiated_by,
        reason=reason,
        drain_receipt_digest=drain_receipt,
        acknowledged_one_way=True,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session_cohort.command(name="stop-force")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True)
@click.option("--reason", default=None)
@click.option("--yes", "-y", is_flag=True)
@click.option("--json", "as_json", is_flag=True)
def cohort_stop_force(session_name, initiated_by, reason, yes, as_json):
    """Reap the fleet now, without waiting for anything to finish."""
    _confirm_stop(session_name, yes)
    if not yes:
        click.confirm("Reap without draining to a boundary?", abort=True)
    record = _cohort_post(
        session_name,
        "stop",
        "force",
        initiated_by=initiated_by,
        reason=reason,
        acknowledged_one_way=True,
        acknowledged_force=True,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


def _confirm_stop(session_name: str, yes: bool) -> None:
    if yes:
        return
    try:
        response = requests.get(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/stop-impact"
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    _render_impact(response.json())
    click.confirm(f"\nStop {session_name} and collect all of its panes?", abort=True)


@session_cohort.command(name="resume-paused")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True)
@click.option("--reason", default=None)
@click.option("--json", "as_json", is_flag=True)
def cohort_resume_paused(session_name, initiated_by, reason, as_json):
    """Bring a stopped fleet's panes back and leave it paused.

    Sends nothing: not a keystroke into any pane, and no bump to the
    supervisor. Use this when you want to look at the fleet before anything
    starts moving again.
    """
    record = _cohort_post(
        session_name, "resume", "paused", initiated_by=initiated_by, reason=reason
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session_cohort.command(name="resume-start")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True)
@click.option("--reason", default=None)
@click.option("--json", "as_json", is_flag=True)
def cohort_resume_start(session_name, initiated_by, reason, as_json):
    """Restore a stopped fleet and wake its supervisor once.

    The session returns to whatever it was doing when it was stopped. The
    supervisor gets exactly one reconciliation bump, sent only after every
    member's outcome is durable and describing all of them — so a fleet that
    came back with one worker missing still starts, and still says so.
    """
    record = _cohort_post(session_name, "resume", "start", initiated_by=initiated_by, reason=reason)
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session_cohort.command(name="resume-retry")
@click.argument("session_name")
@click.option(
    "--operation",
    "operation_id",
    required=True,
    help="The Resume operation to continue. `cao session cohort list` shows it.",
)
@click.option("--by", "initiated_by", required=True)
@click.option("--reason", default=None)
@click.option("--json", "as_json", is_flag=True)
def cohort_resume_retry(session_name, operation_id, initiated_by, reason, as_json):
    """Continue a Resume that stopped needing reconciliation.

    For the two ways a Resume stops short: a member whose result was
    ambiguous, and a supervisor wake that did not land. Both leave panes that
    already came back, so this finishes the same operation instead of starting
    a second one — no member that already has a decided outcome is touched,
    and the supervisor is not woken twice.
    """
    record = _cohort_post(
        session_name,
        "resume",
        "retry",
        operation_id=operation_id,
        initiated_by=initiated_by,
        reason=reason,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session_cohort.command(name="list")
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True)
def cohort_list(session_name, as_json):
    """Every durable fleet operation recorded for this session."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/sessions/{quote(session_name, safe='')}/cohort-operations"
        )
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    payload = response.json()
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["operations"]:
        click.echo("no fleet operations recorded")
        return
    for operation in payload["operations"]:
        promoted = (
            " (promoted from safe)"
            if operation["requested_mode"] != operation["current_mode"]
            else ""
        )
        click.echo(
            f"{operation['operation_id']}  {operation['operation_kind']:<7} "
            f"{operation['current_mode']:<5} {operation['state']:<24} "
            f"{operation['initiated_by']}{promoted}"
        )


@session_cohort.command(name="show")
@click.argument("operation_id")
@click.option("--json", "as_json", is_flag=True)
def cohort_show(operation_id, as_json):
    """The full durable record of one fleet operation."""
    try:
        response = requests.get(f"{API_BASE_URL}/cohort-operations/{quote(operation_id, safe='')}")
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or ""
        except Exception:  # noqa: BLE001
            detail = response.text.strip()
        raise click.ClickException(f"{response.status_code}: {detail}")
    record = response.json()
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


# --------------------------------------------------------------------------
# safe drain, occurrences, and reconciliation wakes (M3-D)
# --------------------------------------------------------------------------
#
# `cao session cohort pause-safe` becomes reachable here for the first time.
# It was deliberately absent while a safe Pause consumed evidence only M3-D
# could produce and no human could type; now the drain produces that evidence
# durably, so the command names a *drain* rather than asking an operator to
# paste a digest and a per-member classification.


def _api_get(path: str):
    try:
        response = requests.get(f"{API_BASE_URL}{path}")
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or ""
        except Exception:  # noqa: BLE001 - a non-JSON body is still worth showing
            detail = response.text.strip()
        raise click.ClickException(f"{response.status_code}: {detail}")
    return response.json()


def _api_post(path: str, **payload):
    try:
        response = requests.post(f"{API_BASE_URL}{path}", json=payload)
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"Failed to connect to cao-server: {e}")
    if response.status_code >= 400:
        try:
            detail = response.json().get("detail") or ""
        except Exception:  # noqa: BLE001
            detail = response.text.strip()
        raise click.ClickException(f"{response.status_code}: {detail}")
    return response.json()


_MEMBER_LABELS = {
    "drained": "drained to a boundary",
    "already-idle": "was already idle",
    "parked": "parked",
    "pending": "not attempted yet",
    "reconciliation-required": "NO PROVEN BOUNDARY",
}


def _render_drain(record: dict) -> None:
    provenance = record.get("provenance") or {}
    click.echo(f"{record['intent']} drain  {record['state']}  (attempt {record['attempt']})")
    click.echo(f"  drain           {record['drain_id']}")
    click.echo(f"  lifecycle epoch {record['lifecycle_epoch']}")
    click.echo(f"  roster revision {str(record['roster_revision'])[:12]}…")
    for member in record.get("members") or []:
        click.echo(
            f"  {str(member['terminal_id'] or '-'):<12} {member['member_state']:<26} "
            f"{_MEMBER_LABELS.get(member['member_state'], '')}"
        )
        if member["detail"]:
            click.echo(f"      {member['detail']}")
    if record["state"] == "complete":
        # The receipt is the only thing a safe Pause/Stop can spend, so it is
        # printed only when it exists. Showing the digest of an unfinished
        # drain would hand an operator a token that cannot be spent.
        #
        # And only the command matching this drain's intent. A Pause drain
        # steers workers to a boundary and announces no teardown; a Stop drain
        # additionally records CAO's intent to collect each pane before it
        # goes. Printing both invited an operator to spend a Pause receipt on a
        # Stop — which the server now refuses, so offering it here would only
        # be teaching a command that fails.
        verb = "pause-safe" if record["intent"] == "pause" else "stop-safe"
        click.echo(f"\nreceipt {record['receipt_digest']}")
        click.echo(
            f"  spend it:     cao session cohort {verb} "
            f"{record['session_name']} --drain {record['drain_id']} --by <you>"
        )
        return
    click.echo(f"\nNOT FINISHED: {record.get('reconciliation_reason') or record['state']}")
    click.echo(
        f"  continue it:  cao session drain {record['session_name']} "
        f"--drain {record['drain_id']} --intent {record['intent']} --retry --by <you>"
    )
    click.echo(
        "  or abandon the boundary deliberately with a force Pause/Stop; "
        "a drain never promotes itself"
    )


@session.command(name="drain")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True, help="Who is asking. Recorded.")
@click.option("--intent", type=click.Choice(["pause", "stop"]), default="pause")
@click.option(
    "--drain",
    "drain_id",
    default=None,
    help="Reuse an existing drain id. Required with --retry; a retry continues that drain.",
)
@click.option(
    "--retry",
    is_flag=True,
    help="Continue a drain that stopped needing reconciliation. Never re-steers a worker "
    "that was already steered.",
)
@click.option("--json", "as_json", is_flag=True)
def session_drain(session_name, initiated_by, intent, drain_id, retry, as_json):
    """Steer every non-idle worker to its next safe boundary, exactly once.

    Produces the durable receipt a safe Pause or Stop spends. A drain that
    cannot prove a boundary stops in reconciliation with no receipt — it never
    quietly becomes a force operation.
    """
    if retry and not drain_id:
        raise click.ClickException("--retry continues a named drain; pass --drain <id>")
    record = _api_post(
        f"/sessions/{quote(session_name, safe='')}/drain/safe",
        drain_id=drain_id or str(uuid.uuid4()),
        intent=intent,
        initiated_by=initiated_by,
        retry=retry,
    )
    if as_json:
        click.echo(json.dumps(record, indent=2))
        return
    _render_drain(_api_get(f"/drains/{quote(record['drain_id'], safe='')}"))


@session.command(name="drains")
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True)
def session_drains(session_name, as_json):
    """Every safe drain recorded for this session."""
    payload = _api_get(f"/sessions/{quote(session_name, safe='')}/drains")
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["drains"]:
        click.echo("no drains recorded")
        return
    for record in payload["drains"]:
        click.echo(
            f"{record['drain_id']}  {record['intent']:<5} {record['state']:<24} "
            f"attempt {record['attempt']}  {record['initiated_by']}"
        )


@session_cohort.command(name="pause-safe")
@click.argument("session_name")
@click.option("--by", "initiated_by", required=True)
@click.option(
    "--drain",
    "drain_id",
    required=True,
    help="The complete drain whose receipt this Pause spends. `cao session drains` lists them.",
)
@click.option("--reason", default=None)
@click.option("--json", "as_json", is_flag=True)
def cohort_pause_safe(session_name, initiated_by, drain_id, reason, as_json):
    """Pause the fleet at a boundary a drain proved it reached.

    Interrupts nothing. Names the drain rather than a digest so the receipt
    and the per-member classification are read from the same durable row —
    a digest typed alongside a hand-edited member list would spend a real
    receipt on a claim it does not describe.
    """
    record = _api_post(
        f"/sessions/{quote(session_name, safe='')}/cohort/pause/safe-drained",
        operation_id=str(uuid.uuid4()),
        drain_id=drain_id,
        initiated_by=initiated_by,
        reason=reason,
    )
    click.echo(json.dumps(record, indent=2)) if as_json else _render_operation(record)


@session.group(name="occurrence")
def session_occurrence():
    """Durable task/round occurrences.

    An occurrence is the *task* identity. It is deliberately not a terminal
    generation and not a provider-native conversation: a stable agent outlives
    many of each, so binding a task to one of them is how a resumed pane
    inherits a finished round.
    """


@session_occurrence.command(name="list")
@click.argument("session_name")
@click.option("--agent", "agent_id", default=None)
@click.option("--state", type=click.Choice(["open", "finalized"]), default=None)
@click.option("--json", "as_json", is_flag=True)
def occurrence_list(session_name, agent_id, state, as_json):
    """Every occurrence recorded for this session."""
    params = []
    if agent_id:
        params.append(f"agent_id={quote(agent_id, safe='')}")
    if state:
        params.append(f"state={state}")
    query = ("?" + "&".join(params)) if params else ""
    payload = _api_get(f"/sessions/{quote(session_name, safe='')}/task-occurrences{query}")
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["occurrences"]:
        click.echo("no task occurrences recorded")
        return
    for record in payload["occurrences"]:
        family = record["finalized"] if record["state"] == "finalized" else record["current"]
        click.echo(
            f"{record['task_occurrence_id']}  round {record['round_index']:<4} "
            f"{record['state']:<10} seed={family['seed_quality']:<9} "
            f"agent {record['agent_id'][:8]}  inc {record['incarnation_id']}"
        )


@session_occurrence.command(name="show")
@click.argument("task_occurrence_id")
@click.option("--json", "as_json", is_flag=True)
def occurrence_show(task_occurrence_id, as_json):
    """One occurrence, its extensions, and whether its seed is enough."""
    record = _api_get(f"/task-occurrences/{quote(task_occurrence_id, safe='')}")
    if as_json:
        click.echo(json.dumps(record, indent=2))
        return
    click.echo(f"{record['task_occurrence_id']}  {record['state']}")
    click.echo(f"  session         {record['session_name']}")
    click.echo(f"  stable agent    {record['agent_id']}")
    click.echo(f"  round           {record['round_index']}")
    # Printed together and labelled, because the whole point of the seam is
    # that the task id is not the effect id.
    click.echo(
        f"  effect          incarnation {record['incarnation_id']} "
        f"terminal {record['terminal_id']} generation {record['generation']}"
    )
    verdict = record.get("seed_verdict") or {}
    click.echo(f"  seed ({verdict.get('family')})    {verdict.get('quality')}")
    if not verdict.get("sufficient_for_fresh_start"):
        click.echo(f"      NOT enough for a fresh successor: {verdict.get('reason')}")
    for extension in record.get("extensions") or []:
        marker = "routed" if extension["routing_state"] == "routed" else "AWAITING DECIDER"
        click.echo(
            f"  extension       {extension['extension_id']} "
            f"{extension['extension_kind']}@{extension['extension_version']} "
            f"-> {extension['decider']} [{marker}]"
        )


@session_occurrence.command(name="pending-extensions")
@click.argument("session_name")
@click.option("--decider", default=None)
@click.option("--json", "as_json", is_flag=True)
def occurrence_pending_extensions(session_name, decider, as_json):
    """Extensions still waiting on the decider that owns them.

    An unrecognised extension — including a future build's completion claim —
    is preserved and listed here rather than interpreted or redispatched.
    """
    query = f"?session_name={quote(session_name, safe='')}"
    if decider:
        query += f"&decider={quote(decider, safe='')}"
    payload = _api_get(f"/task-occurrence-extensions/pending{query}")
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["extensions"]:
        click.echo("no extensions are awaiting a decider")
        return
    for extension in payload["extensions"]:
        claim = " (claims final)" if extension["claims_final"] else ""
        click.echo(
            f"{extension['task_occurrence_id']}  {extension['extension_id']:<20} "
            f"{extension['extension_kind']}@{extension['extension_version']} "
            f"-> {extension['decider']}{claim}"
        )


@session_cohort.command(name="wakes")
@click.argument("session_name")
@click.option("--json", "as_json", is_flag=True)
def cohort_wakes(session_name, as_json):
    """What each resumed supervisor was actually told, and whether it landed."""
    payload = _api_get(f"/sessions/{quote(session_name, safe='')}/reconciliation-wakes")
    if as_json:
        click.echo(json.dumps(payload, indent=2))
        return
    if not payload["wakes"]:
        click.echo("no supervisor reconciliation wakes recorded")
        return
    for wake in payload["wakes"]:
        click.echo(f"{wake['wake_id']}  {wake['source_kind']:<20} {wake['delivery_state']}")
        click.echo(f"  resume      {wake['source_operation_id']}")
        if wake["delivery_state"] != "delivered":
            click.echo(f"  NOT TOLD    {wake['reason_code'] or wake['detail'] or 'undelivered'}")
        click.echo(f"  message     {(wake.get('message') or {}).get('text', '')}")
