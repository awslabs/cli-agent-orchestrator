"""Init command for CLI Agent Orchestrator CLI."""

import click

from cli_agent_orchestrator.utils.database import init_database


@click.command()
def init():
    """Initialize CLI Agent Orchestrator database."""
    try:
        init_database()
        click.echo("CLI Agent Orchestrator initialized successfully")
    except Exception as e:
        raise click.ClickException(str(e))
