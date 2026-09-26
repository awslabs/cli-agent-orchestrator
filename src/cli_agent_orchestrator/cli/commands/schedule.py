"""Schedule commands for CLI Agent Orchestrator (scheduled agent flows).

With a shared server selected (CAO_API_BASE_URL, #745), every subcommand
reads/changes the server's flow state through the HTTP API; the local
database is never opened. Local mode is unchanged.
"""

import asyncio
from pathlib import Path

import click

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.security.principal import LOCAL_PRINCIPAL
from cli_agent_orchestrator.services import flow_service
from cli_agent_orchestrator.utils.remote_server import api_request, is_remote_server


@click.group()
def schedule():
    """Manage scheduled agent flows."""
    # Local mode owns a database; a shared server owns its own (#745).
    if not is_remote_server():
        init_db()


def _echo_flow_added(name, schedule_expr, agent_profile, next_run):
    click.echo(f"Flow '{name}' added successfully")
    click.echo(f"  Schedule: {schedule_expr}")
    click.echo(f"  Agent: {agent_profile}")
    click.echo(f"  Next run: {next_run}")


def _remote_script_body(script: str, file_path: str) -> str:
    """Read the flow's pre-script here and return its CONTENTS to upload.

    The flow file does not travel to a shared server — only its parsed fields do
    — so a path in ``script`` is meaningless there: a relative one resolves
    against the server's own flows directory (which never saw the client's
    checkout), and an absolute one the server refuses as an arbitrary-file
    execution vector. Between them no path could ever register (guojing1217 on
    #802). So the client reads the file it can actually see — resolving a
    relative path against the flow file's directory, exactly as a local run
    would — and sends the bytes; the server writes its own copy beside the flow
    and runs that. Reading here also surfaces a missing script while a human is
    still watching the command, rather than minutes later inside a run.
    """
    script_path = Path(script)
    if not script_path.is_absolute():
        script_path = Path(file_path).resolve().parent / script_path
    if not script_path.is_file():
        raise click.ClickException(
            f"Pre-script '{script}' for {file_path} was not found at {script_path}. "
            "The path is resolved on THIS machine (relative to the flow file), because "
            "its contents are uploaded to the server rather than a path it could not read."
        )
    return script_path.read_text()


def _remote_add(file_path: str) -> None:
    """Parse the client-local flow file and register it on the shared server.

    The file lives on the client; only its parsed fields travel. Engine and
    pre-script front-matter are preserved (never silently dropped, #745).
    """
    metadata, content = flow_service._parse_flow_file(Path(file_path).resolve())
    for field in ("name", "schedule", "agent_profile"):
        if field not in metadata:
            raise click.ClickException(f"Missing required field: {field}")
    body = {
        "name": metadata["name"],
        "schedule": metadata["schedule"],
        "agent_profile": metadata["agent_profile"],
        "prompt_template": content,
    }
    if metadata.get("provider"):
        body["provider"] = metadata["provider"]
    if metadata.get("engine"):
        body["engine"] = metadata["engine"]
    if metadata.get("script"):
        body["script_body"] = _remote_script_body(metadata["script"], file_path)
    flow = api_request("post", "/flows", json=body).json()
    _echo_flow_added(flow["name"], flow["schedule"], flow["agent_profile"], flow.get("next_run"))


@schedule.command()
@click.argument("file_path", type=click.Path(exists=True))
def add(file_path):
    """Add a flow from file."""
    try:
        if is_remote_server():
            _remote_add(file_path)
            return
        # Local registration: the owner is the single-user installation's named
        # principal (#745). LOCAL_PRINCIPAL rather than None — "nobody recorded
        # an owner" and "the local user owns this" are different facts, and the
        # revocation gate treats them differently.
        added = flow_service.add_flow(file_path, owner=LOCAL_PRINCIPAL.id)
        _echo_flow_added(added.name, added.schedule, added.agent_profile, added.next_run)
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


def _echo_flow_table(rows) -> None:
    if not rows:
        click.echo("No flows found")
        return
    click.echo(
        f"{'Name':<20} {'Schedule':<15} {'Agent':<15} {'Last Run':<20} {'Next Run':<20} {'Enabled':<8}"
    )
    click.echo("-" * 110)
    for name, schedule_expr, agent, last_run, next_run, enabled in rows:
        click.echo(
            f"{name:<20} {schedule_expr:<15} {agent:<15} {last_run:<20} {next_run:<20} {enabled:<8}"
        )


def _fmt_ts(value) -> str:
    """Render a datetime or ISO string as YYYY-MM-DD HH:MM."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.replace("T", " ")[:16]
    return str(value.strftime("%Y-%m-%d %H:%M"))


@schedule.command()
def list():
    """List all flows."""
    try:
        if is_remote_server():
            flows = api_request("get", "/flows").json()
            _echo_flow_table(
                [
                    (
                        f["name"],
                        f["schedule"],
                        f["agent_profile"],
                        _fmt_ts(f.get("last_run")) or "Never",
                        _fmt_ts(f.get("next_run")) or "N/A",
                        "Yes" if f.get("enabled") else "No",
                    )
                    for f in flows
                ]
            )
            return
        flows = flow_service.list_flows()
        _echo_flow_table(
            [
                (
                    f.name,
                    f.schedule,
                    f.agent_profile,
                    _fmt_ts(f.last_run) or "Never",
                    _fmt_ts(f.next_run) or "N/A",
                    "Yes" if f.enabled else "No",
                )
                for f in flows
            ]
        )
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def remove(name):
    """Remove a flow."""
    try:
        if is_remote_server():
            api_request("delete", f"/flows/{name}")
        else:
            flow_service.remove_flow(name)
        click.echo(f"Flow '{name}' removed successfully")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def disable(name):
    """Disable a flow."""
    try:
        if is_remote_server():
            api_request("post", f"/flows/{name}/disable")
        else:
            flow_service.disable_flow(name)
        click.echo(f"Flow '{name}' disabled")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def enable(name):
    """Enable a flow."""
    try:
        if is_remote_server():
            api_request("post", f"/flows/{name}/enable")
        else:
            flow_service.enable_flow(name)
        click.echo(f"Flow '{name}' enabled")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


async def _run_flow_with_pipeline(name):
    """Run a flow with the in-process event pipeline bootstrapped.

    ``execute_flow`` -> ``create_terminal`` -> ``provider.initialize`` relies on
    the StatusMonitor buffer being populated by the FIFO reader -> EventBus ->
    StatusMonitor pipeline. Outside the server that pipeline isn't running, so
    the event loop must be registered with the bus and the StatusMonitor/LogWriter
    consumers started here; otherwise ``wait_for_shell``/``wait_until_status``
    never see output and initialization hangs until timeout.
    """
    from cli_agent_orchestrator.services.event_bus import bus
    from cli_agent_orchestrator.services.log_writer import log_writer
    from cli_agent_orchestrator.services.status_monitor import status_monitor

    bus.set_loop(asyncio.get_running_loop())
    status_task = asyncio.create_task(status_monitor.run())
    log_task = asyncio.create_task(log_writer.run())
    try:
        return await flow_service.execute_flow(name)
    finally:
        status_task.cancel()
        log_task.cancel()
        await asyncio.gather(status_task, log_task, return_exceptions=True)


@schedule.command()
@click.argument("name")
def run(name):
    """Manually run a flow."""
    try:
        if is_remote_server():
            # The flow executes on the shared server (its runtime), never in
            # this CLI process (#745).
            executed = api_request("post", f"/flows/{name}/run", timeout=300).json().get("executed")
        else:
            # execute_flow is async in the event-driven architecture (it awaits
            # the async create_terminal); drive it to completion from this sync
            # command with the event pipeline bootstrapped.
            executed = asyncio.run(_run_flow_with_pipeline(name))
        if executed:
            click.echo(f"Flow '{name}' executed successfully")
        else:
            click.echo(f"Flow '{name}' skipped (execute=false)")
    except click.ClickException:
        raise
    except Exception as e:
        raise click.ClickException(str(e))


@click.group(name="flow", hidden=True)
def flow():
    """[Deprecated] Alias for 'cao schedule'."""
    click.secho(
        "Warning: 'cao flow' is deprecated; use 'cao schedule' instead.",
        fg="yellow",
        err=True,
    )
    if not is_remote_server():
        init_db()


# Share the same subcommand objects so alias behavior is identical (issue #378).
for _cmd in schedule.commands.values():
    flow.add_command(_cmd)
