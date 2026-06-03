"""Environment variable management commands for CLI Agent Orchestrator."""

import click

from cli_agent_orchestrator.constants import CAO_ENV_FILE
from cli_agent_orchestrator.utils.env import (
    list_env_vars,
    load_env_vars,
    set_env_var,
    unset_env_var,
)


@click.group(name="env", invoke_without_command=True)
@click.pass_context
def env(ctx: click.Context) -> None:
    """Manage CAO environment variables."""
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


@env.command("set")
@click.argument("key")
@click.argument("value")
def env_set(key: str, value: str) -> None:
    """Set a managed environment variable."""
    set_env_var(key, value)
    click.echo(f"✓ Set {key} in {CAO_ENV_FILE}")


@env.command("get")
@click.argument("key")
def env_get(key: str) -> None:
    """Get a managed environment variable."""
    value = load_env_vars().get(key)
    if value is None:
        click.echo(f"Error: Environment variable '{key}' not found.", err=True)
        raise click.exceptions.Exit(1)

    click.echo(value)


@env.command("list")
def env_list() -> None:
    """List managed environment variables."""
    env_vars = list_env_vars()
    if not env_vars:
        click.echo(f"No env vars set in {CAO_ENV_FILE}")
        return

    for key, value in env_vars.items():
        click.echo(f"{key}={value}")


@env.command("unset")
@click.argument("key")
def env_unset(key: str) -> None:
    """Unset a managed environment variable."""
    unset_env_var(key)
    click.echo(f"✓ Unset {key} in {CAO_ENV_FILE}")
