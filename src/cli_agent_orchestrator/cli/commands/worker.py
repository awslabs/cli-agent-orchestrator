"""Worker commands for CLI Agent Orchestrator.

`cao worker` is `cao session` for an agent that is not on this machine. The verbs
line up on purpose — list, status, send, sessions — because the thing at the other
end IS a cao-server; only the route to it differs, and it differs enough that it
cannot be the same command. See `utils/cluster.py` for that route.

What is deliberately NOT here: create. Workers are minted by a supervisor agent
calling `assign_elastic`, sized to the task it is delegating. A `cao worker create`
would make an agentless worker, which nothing then talks to.
"""

import json
import sys
import time

import click

from cli_agent_orchestrator.utils.cluster import LIVE_STATES, ClusterClient
from cli_agent_orchestrator.utils.terminal import poll_until_done

# Default poll timeout for a sync send, matching `cao session send`. A worker is
# doing delegated work, so the wait is the same order of magnitude.
_DEFAULT_SEND_TIMEOUT = 300


def _rows(workers, *, live_only):
    return [w for w in workers if not live_only or w.get("state") in LIVE_STATES]


def _print_table(workers):
    click.echo(f"{'WORKER':<10} {'STATE':<12} {'AGENT':<22} {'PROVIDER':<14} {'AGE':<8} REASON")
    click.echo("-" * 100)
    for w in workers:
        reason = w.get("reason") or ""
        click.echo(
            f"{w.get('worker_id', ''):<10} {w.get('state', ''):<12} "
            f"{w.get('agent_profile') or 'N/A':<22} {w.get('provider') or 'N/A':<14} "
            f"{str(w.get('age_seconds', '')) + 's':<8} {reason}"
        )


@click.group()
def worker():
    """Inspect and talk to workers in a CAO cluster."""


@worker.command("list")
@click.option("--all", "show_all", is_flag=True, help="Include settled leases and why they settled")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def list_workers(show_all, as_json):
    """List workers in the cluster.

    Settled leases are hidden by default and are the interesting rows when a
    delegation claimed success and produced nothing: `--all` shows the state the
    broker recorded and the reason it recorded it, which the supervisor's own
    transcript does not contain.
    """
    workers = _rows(ClusterClient.from_env().workers(), live_only=not show_all)
    if as_json:
        click.echo(json.dumps(workers, indent=2))
        return
    if not workers:
        click.echo("No live workers" if not show_all else "No workers")
        return
    _print_table(workers)


@worker.command()
@click.argument("worker_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(worker_id, as_json):
    """Show a worker's lease and what its agent is doing.

    Two sources, and both are needed: the lease says whether the cluster still
    considers this worker alive, and the terminal says whether the agent inside it
    is working. They disagree in exactly the case worth catching — a Ready pod
    whose agent finished minutes ago without saying so.
    """
    client = ClusterClient.from_env()
    lease = next((w for w in client.workers() if w.get("worker_id") == worker_id), None)
    terminal = None
    last_output = None
    if lease is None or lease.get("state") in LIVE_STATES:
        terminal = client.sole_terminal(worker_id)
        terminal = client.terminal(worker_id, terminal["id"])
        last_output = client.terminal_output(worker_id, terminal["id"])

    if as_json:
        click.echo(
            json.dumps(
                {
                    "worker_id": worker_id,
                    "lease": lease,
                    "terminal": terminal,
                    "last_output": last_output,
                },
                indent=2,
            )
        )
        return

    click.echo(f"Worker:   {worker_id}")
    if lease:
        click.echo(f"Lease:    {lease.get('state')} ({lease.get('age_seconds')}s)")
        if lease.get("reason"):
            click.echo(f"Reason:   {lease['reason']}")
    else:
        # Leases live in the broker's memory; the workload lives in the cluster.
        # After a broker restart a perfectly healthy worker has no lease row.
        click.echo("Lease:    not in the broker's ledger (it may have restarted)")
    if terminal:
        click.echo(f"Terminal: {terminal['id']}")
        click.echo(f"Agent:    {terminal.get('agent_profile', 'N/A')}")
        click.echo(f"Provider: {terminal.get('provider', 'N/A')}")
        click.echo(f"Status:   {terminal.get('status', 'N/A')}")
    if last_output:
        lines = last_output.splitlines()
        click.echo("\nLast response:")
        click.echo("\n".join(lines[:20]))
        if len(lines) > 20:
            click.echo(f"... ({len(lines) - 20} more lines)")


@worker.command()
@click.argument("worker_id")
@click.argument("message")
@click.option("--async", "is_async", is_flag=True, help="Send and return immediately")
@click.option(
    "--timeout",
    type=int,
    default=None,
    help=f"Timeout in seconds (default: {_DEFAULT_SEND_TIMEOUT}s; ignored with --async)",
)
def send(worker_id, message, is_async, timeout):
    """Send a message to a worker's agent and print its reply."""
    client = ClusterClient.from_env()
    terminal_id = client.sole_terminal(worker_id)["id"]
    client.send_input(worker_id, terminal_id, message)
    if is_async:
        click.echo(f"Message sent to worker {worker_id} (terminal {terminal_id})")
        return

    time.sleep(3)
    interrupted = False
    try:
        # Same done-detection as a local send, reading status through the broker.
        # Duplicating that logic here would mean a kiro worker looked hung.
        poll_until_done(
            terminal_id,
            timeout if timeout is not None else _DEFAULT_SEND_TIMEOUT,
            read_status=lambda tid: client.terminal_status(worker_id, tid),
        )
    except KeyboardInterrupt:
        interrupted = True

    output = client.terminal_output(worker_id, terminal_id)
    if output:
        click.echo(output)
    if interrupted:
        sys.exit(130)


@worker.command()
@click.argument("worker_id")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def sessions(worker_id, as_json):
    """List the sessions and terminals inside a worker."""
    client = ClusterClient.from_env()
    rows = [
        {"session": s["name"], "terminals": client.terminals(worker_id, s["name"])}
        for s in client.sessions(worker_id)
    ]
    if as_json:
        click.echo(json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo(f"Worker {worker_id} has no sessions")
        return
    for row in rows:
        click.echo(f"{row['session']}")
        for t in row["terminals"]:
            click.echo(
                f"  {t['id']:<10} {t.get('agent_profile', 'N/A'):<22} "
                f"{t.get('provider', 'N/A'):<14} {t.get('status', 'N/A')}"
            )


@worker.command()
@click.argument("worker_id")
def attach(worker_id):
    """Talk to a worker's agent turn by turn until you exit.

    Not a PTY. The real attach is a WebSocket onto the worker's pty, and that
    cannot be proxied through the broker's allowlist without publishing an
    unauthenticated shell on every worker — so this is a send/read loop over the
    same two routes `cao worker send` uses. It gives a conversation, not a
    terminal: no scrollback, no keystrokes, no Ctrl-C into the agent.

    Type `exit` or press Ctrl-D to leave. Leaving does not release the worker.
    """
    client = ClusterClient.from_env()
    terminal = client.sole_terminal(worker_id)
    terminal_id = terminal["id"]
    click.echo(
        f"Attached to worker {worker_id} (terminal {terminal_id}, "
        f"{terminal.get('agent_profile', 'unknown profile')}). "
        "Type 'exit' to leave; the worker keeps its lease."
    )
    while True:
        try:
            message = click.prompt(f"{worker_id}>", prompt_suffix=" ")
        except (EOFError, click.Abort):
            click.echo()
            return
        if message.strip() in {"exit", "quit"}:
            return
        client.send_input(worker_id, terminal_id, message)
        time.sleep(3)
        try:
            poll_until_done(
                terminal_id,
                _DEFAULT_SEND_TIMEOUT,
                read_status=lambda tid: client.terminal_status(worker_id, tid),
            )
        except KeyboardInterrupt:
            # Stop waiting on this turn, keep the session. The agent is still
            # working; the next prompt will show where it got to.
            click.echo("\n(stopped waiting; the agent is still working)")
            continue
        output = client.terminal_output(worker_id, terminal_id)
        if output:
            click.echo(output)


@worker.command()
@click.argument("worker_id")
@click.option("-n", "--tail", "tail_lines", type=int, default=200, help="Lines to show")
@click.option("-f", "--follow", is_flag=True, help="Stream new lines as they arrive")
def logs(worker_id, tail_lines, follow):
    """Print a worker's container log.

    This is the command for a worker that never became usable. `cao worker status`
    can only report what the lease and the terminal say, and neither exists yet
    when the failure is an image pull or a profile install that died at boot.
    """
    client = ClusterClient.from_env()
    if not follow:
        click.echo(client.logs(worker_id, tail_lines=tail_lines).rstrip("\n"))
        return
    try:
        for line in client.follow_logs(worker_id, tail_lines=tail_lines):
            click.echo(line)
    except KeyboardInterrupt:
        return


@worker.command()
@click.argument("worker_id")
def release(worker_id):
    """Release one worker, deleting it and the session it was running.

    The lease's own expiry is the safety net, not the plan: a worker that finished
    without saying so squats a node's worth of memory until the broker's
    completion deadline. This is how you take it back now.
    """
    ClusterClient.from_env().release(worker_id)
    click.echo(f"✓ Released worker {worker_id}")
