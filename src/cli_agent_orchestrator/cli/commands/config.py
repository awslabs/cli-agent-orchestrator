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


_ENV_ONLY_SECTIONS = ("network.", "auth.")


@config.command(name="set")
@click.argument("key")
@click.argument("value")
def set_cmd(key, value):
    """Set config KEY to VALUE, persisting it to settings.json."""
    try:
        result = ConfigService.set(key, _coerce(value))
    except (ValueError, KeyError) as exc:
        # settings_service setters raise ValueError/KeyError for invalid keys
        # or out-of-range values (e.g. memory.flush_threshold). Surface a clean
        # CLI error instead of an unhandled Python traceback.
        raise click.ClickException(str(exc))
    click.echo(json.dumps(result))
    if key.startswith(_ENV_ONLY_SECTIONS):
        click.echo(
            f"warning: '{key}' is stored but has no runtime effect yet — "
            "only its CAO_* env var is read (see docs/configuration.md).",
            err=True,
        )


@config.command(name="list")
def list_cmd():
    """List every known config key with its resolved value."""
    for key, value in ConfigService.list_all().items():
        click.echo(f"{key} = {json.dumps(value)}")


@config.command(name="path")
def path_cmd():
    """Print the absolute path to the unified settings.json file."""
    click.echo(str(ConfigService.path()))
