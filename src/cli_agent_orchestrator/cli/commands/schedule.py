"""Schedule commands for CLI Agent Orchestrator (scheduled agent flows)."""

import asyncio

import click

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.services import flow_service


@click.group()
def schedule():
    """Manage scheduled agent flows."""
    # Ensure database is initialized
    init_db()


@schedule.command()
@click.argument("file_path", type=click.Path(exists=True))
def add(file_path):
    """Add a flow from file."""
    try:
        added = flow_service.add_flow(file_path)
        click.echo(f"Flow '{added.name}' added successfully")
        click.echo(f"  Schedule: {added.schedule}")
        click.echo(f"  Agent: {added.agent_profile}")
        click.echo(f"  Next run: {added.next_run}")
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
def list():
    """List all flows."""
    try:
        flows = flow_service.list_flows()
        if not flows:
            click.echo("No flows found")
            return

        click.echo(
            f"{'Name':<20} {'Schedule':<15} {'Agent':<15} {'Last Run':<20} {'Next Run':<20} {'Enabled':<8}"
        )
        click.echo("-" * 110)

        for f in flows:
            last_run = f.last_run.strftime("%Y-%m-%d %H:%M") if f.last_run else "Never"
            next_run = f.next_run.strftime("%Y-%m-%d %H:%M") if f.next_run else "N/A"
            enabled = "Yes" if f.enabled else "No"

            click.echo(
                f"{f.name:<20} {f.schedule:<15} {f.agent_profile:<15} {last_run:<20} {next_run:<20} {enabled:<8}"
            )
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def remove(name):
    """Remove a flow."""
    try:
        flow_service.remove_flow(name)
        click.echo(f"Flow '{name}' removed successfully")
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def disable(name):
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        click.echo(f"Flow '{name}' disabled")
    except Exception as e:
        raise click.ClickException(str(e))


@schedule.command()
@click.argument("name")
def enable(name):
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        click.echo(f"Flow '{name}' enabled")
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
        # execute_flow is async in the event-driven architecture (it awaits the
        # async create_terminal); drive it to completion from this sync command
        # with the event pipeline bootstrapped (see _run_flow_with_pipeline).
        executed = asyncio.run(_run_flow_with_pipeline(name))
        if executed:
            click.echo(f"Flow '{name}' executed successfully")
        else:
            click.echo(f"Flow '{name}' skipped (execute=false)")
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
    init_db()


# Share the same subcommand objects so alias behavior is identical (issue #378).
for _cmd in schedule.commands.values():
    flow.add_command(_cmd)
