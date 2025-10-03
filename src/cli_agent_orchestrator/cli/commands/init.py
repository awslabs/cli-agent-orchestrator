"""Init command for CLI Agent Orchestrator CLI."""

import click

from cli_agent_orchestrator.clients.database import init_db


@click.command()
def init():
    """Initialize CLI Agent Orchestrator database."""
    try:
        init_db()
        click.echo("CLI Agent Orchestrator initialized successfully")
    except Exception as e:
        raise click.ClickException(str(e))
