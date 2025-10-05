"""Flow commands for CLI Agent Orchestrator."""

import click
from datetime import datetime

from cli_agent_orchestrator.clients.database import init_db
from cli_agent_orchestrator.services import flow_service


@click.group()
def flow():
    """Manage scheduled agent flows."""
    # Ensure database is initialized
    init_db()


@flow.command()
@click.argument('file_path', type=click.Path(exists=True))
def add(file_path):
    """Add a flow from file."""
    try:
        flow = flow_service.add_flow(file_path)
        click.echo(f"Flow '{flow.name}' added successfully")
        click.echo(f"  Schedule: {flow.schedule}")
        click.echo(f"  Agent: {flow.agent_profile}")
        click.echo(f"  Next run: {flow.next_run}")
    except Exception as e:
        raise click.ClickException(str(e))


@flow.command()
def list():
    """List all flows."""
    try:
        flows = flow_service.list_flows()
        if not flows:
            click.echo("No flows found")
            return
        
        click.echo(f"{'Name':<20} {'Schedule':<15} {'Agent':<15} {'Last Run':<20} {'Next Run':<20} {'Enabled':<8}")
        click.echo("-" * 110)
        
        for f in flows:
            last_run = f.last_run.strftime('%Y-%m-%d %H:%M') if f.last_run else 'Never'
            next_run = f.next_run.strftime('%Y-%m-%d %H:%M') if f.next_run else 'N/A'
            enabled = 'Yes' if f.enabled else 'No'
            
            click.echo(f"{f.name:<20} {f.schedule:<15} {f.agent_profile:<15} {last_run:<20} {next_run:<20} {enabled:<8}")
    except Exception as e:
        raise click.ClickException(str(e))


@flow.command()
@click.argument('name')
def remove(name):
    """Remove a flow."""
    try:
        flow_service.remove_flow(name)
        click.echo(f"Flow '{name}' removed successfully")
    except Exception as e:
        raise click.ClickException(str(e))


@flow.command()
@click.argument('name')
def disable(name):
    """Disable a flow."""
    try:
        flow_service.disable_flow(name)
        click.echo(f"Flow '{name}' disabled")
    except Exception as e:
        raise click.ClickException(str(e))


@flow.command()
@click.argument('name')
def enable(name):
    """Enable a flow."""
    try:
        flow_service.enable_flow(name)
        click.echo(f"Flow '{name}' enabled")
    except Exception as e:
        raise click.ClickException(str(e))


@flow.command()
@click.argument('name')
def run(name):
    """Manually run a flow."""
    try:
        executed = flow_service.execute_flow(name)
        if executed:
            click.echo(f"Flow '{name}' executed successfully")
        else:
            click.echo(f"Flow '{name}' skipped (execute=false)")
    except Exception as e:
        raise click.ClickException(str(e))
