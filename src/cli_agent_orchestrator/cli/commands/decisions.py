"""Local decision settings and content-free record management."""

import json
from typing import Any, Callable

import click

from cli_agent_orchestrator.decisions import operator, settings
from cli_agent_orchestrator.decisions.types import POINTS


def _run(action: Callable[[], Any]) -> None:
    try:
        result = action()
        click.echo(json.dumps(result if result is not None else {"success": True}, indent=2))
    except (ValueError, OSError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error


@click.group(help="Manage local launch decisions. " + settings.SAME_USER)
def decisions() -> None:
    pass


@decisions.command()
def status() -> None:
    """Show effective local settings; running server flags are not visible."""
    _run(operator.status)


@decisions.command(name="set")
@click.argument("point", type=click.Choice(tuple(POINTS)))
@click.argument("state", type=click.Choice(("off", "shadow", "on")))
@click.option("--decider", default=None)
def set_point(point: str, state: str, decider: str | None) -> None:
    """Set a point's state and optionally its installed decider name."""
    _run(lambda: settings.set_point(point, state, decider))


@decisions.command()
@click.argument("provider")
@click.argument("tier", type=click.Choice(("small", "medium", "large")))
@click.argument("model", required=False)
@click.option("--unset", is_flag=True)
def tier(provider: str, tier: str, model: str | None, unset: bool) -> None:
    """Map a provider tier to a model, or remove its mapping."""
    if (model is None) == (not unset):
        raise click.UsageError("provide a model or --unset, exclusively")
    _run(lambda: settings.set_tier(provider, tier, None if unset else model))


@decisions.command()
@click.argument("point", type=click.Choice(tuple(POINTS)))
@click.option("--profile", default=None)
@click.option("--role", default=None)
@click.argument("option")
def table(point: str, profile: str | None, role: str | None, option: str) -> None:
    """Set the fixed table's profile or role answer."""
    _run(lambda: settings.set_table(point, option, profile=profile, role=role))


@decisions.command()
@click.option("--add", default=None)
@click.option("--remove", default=None)
def exclude(add: str | None, remove: str | None) -> None:
    """Pin an installed profile to its existing model resolution."""
    _run(lambda: settings.set_exclusions(add=add, remove=remove))


@decisions.command()
@click.option("--on-timeout-ms", type=click.IntRange(*settings.ON_TIMEOUT_MS_RANGE))
@click.option("--threshold", type=click.FloatRange(*settings.THRESHOLD_RANGE))
@click.option("--retention-days", type=click.IntRange(*settings.RETENTION_DAYS_RANGE))
def tune(on_timeout_ms: int | None, threshold: float | None, retention_days: int | None) -> None:
    """Change timeout, confidence threshold or retention."""
    if all(value is None for value in (on_timeout_ms, threshold, retention_days)):
        raise click.UsageError("provide a tuning option")
    _run(
        lambda: settings.tune(
            on_timeout_ms=on_timeout_ms, threshold=threshold, retention_days=retention_days
        )
    )


@decisions.command(name="list")
@click.option("--point", type=click.Choice(tuple(POINTS)))
@click.option("--since", default=None)
@click.option("--limit", type=click.IntRange(1, 10000), default=100)
def list_records(point: str | None, since: str | None, limit: int) -> None:
    """List content-free records, newest first."""
    _run(lambda: operator.records(point=point, since=since, limit=limit))


@decisions.command()
@click.option("--before", default=None)
@click.option("--all", "all_records", is_flag=True)
@click.option("--rotate-key", is_flag=True)
def purge(before: str | None, all_records: bool, rotate_key: bool) -> None:
    """Purge records and optionally invalidate the message hash key."""
    if (before is None) == (not all_records):
        raise click.UsageError("provide exactly one of --before or --all")
    _run(lambda: operator.purge(before=before, all_records=all_records, rotate=rotate_key))
