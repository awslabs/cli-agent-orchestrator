"""Config commands for CLI Agent Orchestrator CLI (issue #357)."""

import json

import click

from cli_agent_orchestrator.services.config_service import ConfigService


def _coerce(value: str):
    """Best-effort coercion of a CLI string value to bool/int/float/JSON list."""
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    if value.startswith("[") or value.startswith("{"):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


@click.group()
def config():
    """Inspect and edit unified CAO configuration (settings.json)."""


@config.command(name="get")
@click.argument("key")
def get_cmd(key):
    """Get the resolved value for a dotted config KEY, e.g. terminal.backend."""
    value = ConfigService.get(key)
    click.echo(json.dumps(value))


@config.command(name="set")
@click.argument("key")
@click.argument("value")
def set_cmd(key, value):
    """Set config KEY to VALUE, persisting it to settings.json."""
    result = ConfigService.set(key, _coerce(value))
    click.echo(json.dumps(result))


@config.command(name="list")
def list_cmd():
    """List every known config key with its resolved value."""
    for key, value in ConfigService.list_all().items():
        click.echo(f"{key} = {json.dumps(value)}")


@config.command(name="path")
def path_cmd():
    """Print the absolute path to the unified settings.json file."""
    click.echo(str(ConfigService.path()))
