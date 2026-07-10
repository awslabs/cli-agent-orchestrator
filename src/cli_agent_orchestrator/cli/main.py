"""Main CLI entry point for CLI Agent Orchestrator."""

from importlib.metadata import PackageNotFoundError, version

import click

from cli_agent_orchestrator.cli.commands.config import config
from cli_agent_orchestrator.cli.commands.env import env
from cli_agent_orchestrator.cli.commands.info import info
from cli_agent_orchestrator.cli.commands.init import init
from cli_agent_orchestrator.cli.commands.install import install
from cli_agent_orchestrator.cli.commands.launch import launch
from cli_agent_orchestrator.cli.commands.mcp_server import mcp_server
from cli_agent_orchestrator.cli.commands.memory import memory
from cli_agent_orchestrator.cli.commands.profile import profile
from cli_agent_orchestrator.cli.commands.schedule import flow, schedule
from cli_agent_orchestrator.cli.commands.session import session
from cli_agent_orchestrator.cli.commands.shutdown import shutdown
from cli_agent_orchestrator.cli.commands.skills import skills
from cli_agent_orchestrator.cli.commands.terminal import terminal
from cli_agent_orchestrator.cli.commands.workflow import workflow

try:
    __version__ = version("cli-agent-orchestrator")
except PackageNotFoundError:
    __version__ = "unknown"


@click.group()
@click.version_option(__version__, "-V", "--version", prog_name="cao")
def cli():
    """CLI Agent Orchestrator."""


# Register commands
cli.add_command(profile)
cli.add_command(launch)
cli.add_command(config)
cli.add_command(init)
cli.add_command(install)
cli.add_command(shutdown)
cli.add_command(schedule)
cli.add_command(flow)  # deprecated alias for 'schedule' (issue #378)
cli.add_command(env)
cli.add_command(mcp_server)
cli.add_command(info)
cli.add_command(memory)
cli.add_command(skills)
cli.add_command(session)
cli.add_command(terminal)
cli.add_command(workflow)


if __name__ == "__main__":
    cli()
