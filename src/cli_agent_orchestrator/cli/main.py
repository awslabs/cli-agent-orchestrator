"""Main CLI entry point for CLI Agent Orchestrator."""

import click

from cli_agent_orchestrator.cli.commands.launch import launch
from cli_agent_orchestrator.cli.commands.init import init


@click.group()
def cli():
    """CLI Agent Orchestrator."""
    pass


# Register commands
cli.add_command(launch)
cli.add_command(init)


if __name__ == "__main__":
    cli()
