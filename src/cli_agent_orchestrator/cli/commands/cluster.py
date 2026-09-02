"""Cluster commands for CLI Agent Orchestrator.

`cao cluster` is the fleet-wide view of what `cao worker` shows one row of. Both
speak to a broker over HTTP and neither knows what Kubernetes is; see
`utils/cluster.py` for the contract and the one port-forward it needs.

Note the name that is already taken: `cao shutdown` stops tmux sessions on THIS
machine. `cao cluster shutdown` releases workers in a remote cluster. They are
different verbs on different things, which is why the second one is spelled out.
"""

import json
from collections import Counter

import click

from cli_agent_orchestrator.utils.cluster import LIVE_STATES, ClusterClient


@click.group()
def cluster():
    """Inspect and tear down a CAO cluster's workers."""


@cluster.command()
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def status(as_json):
    """Summarise the cluster: is the broker there, and what is it holding.

    A settled count is not an error count. `completed` and `released` are the
    normal end of a task; `terminated`, `failed` and `expired` are the three the
    broker records a reason for, and `cao worker list --all` prints those reasons.
    """
    client = ClusterClient.from_env()
    workers = client.workers()
    counts = Counter(w.get("state", "unknown") for w in workers)
    live = [w for w in workers if w.get("state") in LIVE_STATES]

    if as_json:
        click.echo(
            json.dumps(
                {"broker": client.url, "live": len(live), "states": dict(counts)},
                indent=2,
            )
        )
        return

    click.echo(f"Broker:  {client.url}")
    click.echo(f"Live:    {len(live)} worker(s)")
    if counts:
        click.echo("States:  " + ", ".join(f"{state}={n}" for state, n in sorted(counts.items())))
    else:
        click.echo("States:  no leases on record")


@cluster.command()
@click.option("--yes", is_flag=True, help="Do not ask for confirmation")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON")
def shutdown(yes, as_json):
    """Release every live worker in the cluster.

    This deletes the workers and the agent sessions inside them; nothing is
    resumable afterwards, because a released worker's state volume goes with it.
    It does NOT touch the supervisor, the panel, or the cluster itself — those are
    deployed by the cluster's manifests and are removed the same way, so this
    command cannot leave you without the fleet you would use to make new workers.

    Workers whose lease has already settled are skipped: the broker released them
    when it settled them, so there is nothing left to delete.
    """
    client = ClusterClient.from_env()
    live = [w for w in client.workers() if w.get("state") in LIVE_STATES]
    if not live:
        if as_json:
            click.echo(json.dumps({"released": [], "failed": []}, indent=2))
        else:
            click.echo("No live workers to release")
        return

    if not yes and not as_json:
        click.echo(f"About to release {len(live)} worker(s):")
        for w in live:
            click.echo(
                f"  {w.get('worker_id')}  {w.get('agent_profile') or 'N/A'}"
                f"  {w.get('age_seconds')}s"
            )
        click.confirm("Release them and lose their sessions?", abort=True)

    released, failed = [], []
    for w in live:
        worker_id = w.get("worker_id")
        try:
            client.release(worker_id)
            released.append(worker_id)
        except click.ClickException as exc:
            # Keep going. One unreachable worker must not strand the rest — the
            # whole reason to run this is that something is already wrong.
            failed.append({"worker_id": worker_id, "error": exc.format_message()})

    if as_json:
        click.echo(json.dumps({"released": released, "failed": failed}, indent=2))
        return
    for worker_id in released:
        click.echo(f"✓ Released worker {worker_id}")
    for entry in failed:
        click.echo(f"✗ {entry['worker_id']}: {entry['error']}", err=True)
    if failed:
        raise click.ClickException(f"{len(failed)} of {len(live)} worker(s) could not be released")
