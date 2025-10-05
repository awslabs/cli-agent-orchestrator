"""Main CLI entry point for CLI Agent Orchestrator."""

import click

from cli_agent_orchestrator.cli.commands.launch import launch
from cli_agent_orchestrator.cli.commands.init import init
from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.cli.commands.shutdown import shutdown


@click.group()
def cli():
    """CLI Agent Orchestrator."""
    pass


# Register commands
cli.add_command(launch)
cli.add_command(init)
cli.add_command(install)
cli.add_command(shutdown)


if __name__ == "__main__":
    cli()
