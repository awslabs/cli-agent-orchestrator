"""Main CLI entry point for CLI Agent Orchestrator.

Commands load lazily: ``cao --help`` reads only the static ``_COMMANDS`` table,
so it imports no application code and touches no files. A command's module is
imported the first time that command is resolved.
"""

import importlib
from importlib.metadata import PackageNotFoundError, version

import click

from cli_agent_orchestrator.cli.agent_guide import AGENT_GUIDE, FOOTER

try:
    __version__ = version("cli-agent-orchestrator")
except PackageNotFoundError:
    __version__ = "unknown"

_PKG = "cli_agent_orchestrator.cli.commands."

# (name, module, attribute, short help, hidden). The short help must equal the
# command's own get_short_help_str(); test/cli/test_agent_help.py enforces it.
_COMMANDS: tuple[tuple[str, str, str, str, bool], ...] = (
    ("agent", _PKG + "agent", "agent", "Orchestrate other agents from the shell.", False),
    (
        "config",
        _PKG + "config",
        "config",
        "Inspect and edit unified CAO configuration (settings.json).",
        False,
    ),
    ("env", _PKG + "env", "env", "Manage CAO environment variables.", False),
    ("fleet", _PKG + "fleet", "fleet", "Inspect and tear down a CAO fleet's workers.", False),
    ("flow", _PKG + "schedule", "flow", "[Deprecated] Alias for 'cao schedule'.", True),
    ("info", _PKG + "info", "info", "Display information about the current session.", False),
    ("init", _PKG + "init", "init", "Initialize CLI Agent Orchestrator database.", False),
    (
        "install",
        _PKG + "install",
        "install",
        "Install an agent by name, file path, or URL.",
        False,
    ),
    (
        "launch",
        _PKG + "launch",
        "launch",
        "Launch cao session with specified agent profile.",
        False,
    ),
    ("mcp-server", _PKG + "mcp_server", "mcp_server", "Start the CAO MCP server.", False),
    ("memory", _PKG + "memory", "memory", "Manage CAO memories.", False),
    (
        "plugin",
        _PKG + "agent_plugin",
        "agent_plugin",
        "Manage agent plugins (Agent Plugins 1.0.0).",
        True,
    ),
    ("profile", _PKG + "profile", "profile", "Manage agent profiles.", False),
    ("schedule", _PKG + "schedule", "schedule", "Manage scheduled agent flows.", False),
    ("session", _PKG + "session", "session", "Manage CAO sessions.", False),
    (
        "shutdown",
        _PKG + "shutdown",
        "shutdown",
        "Shutdown tmux sessions and cleanup terminal records.",
        False,
    ),
    ("skills", _PKG + "skills", "skills", "Manage installed skills.", False),
    ("terminal", _PKG + "terminal", "terminal", "Manage CAO terminals.", False),
    ("tui", _PKG + "tui", "tui", "Launch the terminal UI (bundled Rust binary).", False),
    ("update", _PKG + "update", "update", "Update CAO to the latest version.", False),
    ("worker", _PKG + "worker", "worker", "Inspect and talk to workers in a CAO cluster.", False),
    ("workflow", _PKG + "workflow", "workflow", "Author and inspect CAO workflow specs.", False),
)
_BY_NAME = {entry[0]: entry for entry in _COMMANDS}


def _add_footer(cmd: click.Command) -> None:
    """Append the agent footer to a group's help and to every nested group's help."""
    if not isinstance(cmd, click.Group):
        return
    if cmd.epilog is None:
        cmd.epilog = FOOTER
    elif FOOTER not in cmd.epilog:
        cmd.epilog = f"{cmd.epilog}\n\n{FOOTER}"
    for sub in cmd.commands.values():
        _add_footer(sub)


class _LazyGroup(click.Group):
    """Root group that imports a command's module only when the command is resolved."""

    def list_commands(self, ctx: click.Context) -> list[str]:
        return sorted(_BY_NAME)

    def get_command(self, ctx: click.Context, cmd_name: str) -> click.Command | None:
        entry = _BY_NAME.get(cmd_name)
        if entry is None:
            return None
        _, module, attr, _, _ = entry
        cmd: click.Command = getattr(importlib.import_module(module), attr)
        _add_footer(cmd)
        return cmd

    def format_commands(self, ctx: click.Context, formatter: click.HelpFormatter) -> None:
        # Click's default resolves every command to read its short help, which
        # would import the whole application. Read the table instead.
        rows = [(name, short) for name, _, _, short, hidden in _COMMANDS if not hidden]
        limit = formatter.width - 6 - max(len(name) for name, _ in rows)
        with formatter.section("Commands"):
            formatter.write_dl(
                [(name, click.utils.make_default_short_help(short, limit)) for name, short in rows]
            )


def _print_agent_guide(ctx: click.Context, _param: click.Parameter, value: bool) -> None:
    if not value or ctx.resilient_parsing:
        return
    click.echo(AGENT_GUIDE, nl=False)
    ctx.exit()


@click.group(cls=_LazyGroup, epilog=FOOTER)
@click.version_option(__version__, "-V", "--version", prog_name="cao")
@click.option(
    "--skill",
    is_flag=True,
    is_eager=True,
    expose_value=False,
    callback=_print_agent_guide,
    help="Print the guide for AI agents (a SKILL.md) and exit.",
)
def cli() -> None:
    """CLI Agent Orchestrator."""


if __name__ == "__main__":
    cli()
