"""Local decision settings and content-free record management."""

import json
from typing import Any, Callable, Sequence

import click

from cli_agent_orchestrator.decisions import operator, packages, settings
from cli_agent_orchestrator.decisions.types import POINTS, ROUTE_POINTS, SWITCHES
from cli_agent_orchestrator.services.model_tiers import TIERS


def _run(action: Callable[[], Any]) -> Any:
    try:
        result = action()
        click.echo(json.dumps(result if result is not None else {"success": True}, indent=2))
        return result
    except (ValueError, OSError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error


@click.group(
    help="Manage local launch decisions. " + settings.SAME_USER + " " + packages.CONSENT_SAME_USER
)
def decisions() -> None:
    pass


@decisions.command()
def status() -> None:
    """Show effective local settings.

    Also shows each decider's last outcome and its 24-hour counts by reason, read from the
    database without changing it. A running server's flags are not visible here; the server
    logs its effective settings at startup and when they change.
    """
    _run(operator.status)


@decisions.command(name="set")
@click.argument("point", type=click.Choice(SWITCHES))
@click.argument("state", type=click.Choice(settings.STATES))
@click.option("--decider", default=None)
def set_point(point: str, state: str, decider: str | None) -> None:
    """Set a point's state and optionally its installed decider name."""
    _run(lambda: operator.set_point(point, state, decider))


@decisions.command()
@click.argument("provider")
@click.argument("tier", type=click.Choice(TIERS))
@click.argument("model", required=False)
@click.option("--unset", is_flag=True)
def tier(provider: str, tier: str, model: str | None, unset: bool) -> None:
    """Map a provider tier to a model, or remove its mapping."""
    if (model is None) == (not unset):
        raise click.UsageError("provide a model or --unset, exclusively")
    _run(lambda: settings.set_tier(provider, tier, None if unset else model))


@decisions.command()
@click.argument("point", type=click.Choice(ROUTE_POINTS))
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
# FloatRange lets `nan` through (its comparisons are false); `settings.tune()` refuses it, so
# `nan` exits 1 while `inf` and other out-of-range values exit 2 here.
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


@decisions.command()
def off() -> None:
    """Halt all points and wait for external requests to finish."""
    result = _run(operator.off)
    if not result["success"]:
        click.echo(result["message"], err=True)
        raise click.exceptions.Exit(1)


def _dispatch(name: str, action: str, extra: tuple[str, ...]) -> None:
    try:
        code = packages.dispatch(name, action, extra)
    except ValueError as error:
        raise click.UsageError(str(error)) from error
    except (OSError, RuntimeError) as error:
        raise click.ClickException(str(error)) from error
    raise click.exceptions.Exit(code)


@decisions.command(context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def enable(name: str, extra: tuple[str, ...]) -> None:
    """Run an installed decider's consent command.

    Consent needs an interactive terminal.
    """
    _dispatch(name, "consent", extra)


@decisions.command(context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def disable(name: str, extra: tuple[str, ...]) -> None:
    """Run an installed decider's revoke command."""
    _dispatch(name, "revoke", extra)


@decisions.command(context_settings={"ignore_unknown_options": True})
@click.argument("name")
@click.argument("extra", nargs=-1, type=click.UNPROCESSED)
def show(name: str, extra: tuple[str, ...]) -> None:
    """Run an installed decider's status command."""
    _dispatch(name, "status", extra)


# #1199 (provisional): the local model catalog, decisions.match. Read-only verbs: operators
# edit settings.json to change it. Nothing here asks a decider or creates the hash key.


@decisions.group(name="match")
def match_group() -> None:
    """Show, check and explain the local model catalog.

    The catalog is decisions.match in settings.json. It is never sent to any decider, and
    records carry only its keyed hash.
    """


def _hash_line(catalog: Any) -> tuple[str | None, str | None]:
    from cli_agent_orchestrator.constants import DB_DIR
    from cli_agent_orchestrator.decisions import catalog as cat

    key = cat.read_hash_key(DB_DIR / "decision-hash.key")
    digest = cat.catalog_hash(catalog, key) if key is not None else None
    return digest if digest is not None else (None, None)


@match_group.command(name="show")
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def match_show(as_json: bool) -> None:
    """Show the validated catalog, its warnings and its hash."""
    loaded = settings.load_settings().match_catalog
    digest, key_id = _hash_line(loaded)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "valid": loaded.valid,
                    "invalid": loaded.invalid,
                    "version": loaded.version,
                    "long_context_tokens": loaded.long_context_tokens,
                    "ignore_features": list(loaded.ignore_features),
                    "models": {
                        provider: {
                            model: {
                                "context_tokens": facts.context_tokens,
                                "cost": facts.cost,
                                "good_for": list(facts.good_for),
                            }
                            for model, facts in entries.items()
                        }
                        for provider, entries in loaded.models.items()
                    },
                    "warnings": list(loaded.warnings),
                    "catalog_hash": digest,
                    "hash_key_id": key_id,
                },
                indent=2,
            )
        )
        return
    if not loaded.valid:
        click.echo(
            f"catalog invalid: {loaded.invalid}. Matching is off for every launch; "
            "launches run as today."
        )
        return
    ignored = ", ".join(loaded.ignore_features) or "none"
    click.echo(
        f"catalog v{loaded.version} · {loaded.model_count()} models · long_context_tokens "
        f"{loaded.long_context_tokens} · ignore_features: {ignored}"
    )
    click.echo(
        f"hash hmac:{digest} (key {key_id})"
        if digest
        else "hash unavailable: no records key yet (the first decision record creates it)"
    )
    rows = [
        (provider, model, str(facts.context_tokens), facts.cost, ",".join(facts.good_for) or "-")
        for provider, entries in loaded.models.items()
        for model, facts in entries.items()
    ]
    _table([("provider", "model", "context", "cost", "good_for")] + rows)
    if loaded.warnings:
        click.echo("warnings:")
        for warning in loaded.warnings:
            click.echo(f"  {warning}")


def _table(rows: Sequence[Sequence[str]]) -> None:
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    for row in rows:
        click.echo("  ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip())


@match_group.command(name="check")
def match_check() -> None:
    """Check the catalog against model_tiers.

    Also checks it against the capability table. Exits 1 for an invalid catalog, a dropped
    entry or an unreachable declaration.
    """
    from cli_agent_orchestrator.decisions import catalog as cat
    from cli_agent_orchestrator.utils.ephemeral_capabilities import ephemeral_providers

    snapshot = settings.load_settings()
    lines, problems = cat.check(snapshot.match_catalog, snapshot.model_tiers, ephemeral_providers())
    for level, text in lines:
        click.echo(f"{level:<5} {text}")
    if problems:
        raise click.exceptions.Exit(1)


def _words(value: str | None, allowed: tuple[str, ...], what: str) -> tuple[str, ...]:
    items = tuple(item for item in (value or "").split(",") if item)
    unknown = [item for item in items if item not in allowed]
    if unknown:
        raise click.UsageError(f"unknown {what}: {unknown[0]} (allowed: {', '.join(allowed)})")
    if len(set(items)) != len(items):
        raise click.UsageError(f"{what} must not repeat")
    return items


@match_group.command(name="explain")
@click.option("--needs", default="", help="Comma-separated: long_context,deep_reasoning,read_only.")
@click.option("--size", required=True, type=click.Choice(TIERS), help="The size answer.")
@click.option("--options", default=None, help="Comma-separated CLIs the launch may run on.")
@click.option("--fallback", default=None, help="The creator's CLI (default: the first option).")
@click.option(
    "--tier-auto", is_flag=True, help="The tier may rise from the size to ephemeral.max_tier."
)
@click.option("--json", "as_json", is_flag=True, help="Print JSON.")
def match_explain(
    needs: str,
    size: str,
    options: str | None,
    fallback: str | None,
    tier_auto: bool,
    as_json: bool,
) -> None:
    """Show the candidates and the pick, asking no one.

    It uses your current settings. For a launch's records, pass its match_needs, its size (match_from, or the
    launch's tier), its options and its fallback; the pick, rule, tie-break and hash match.
    """
    from cli_agent_orchestrator.decisions.match import band, explain_rows, match_launch
    from cli_agent_orchestrator.decisions.types import ROUTING_FEATURES
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.utils.ephemeral_capabilities import (
        enforcement_label,
        ephemeral_providers,
    )

    if "needs_network" in (needs or "").split(","):
        raise click.UsageError("needs_network is advisory only and never routes")
    wanted = _words(needs, ROUTING_FEATURES, "need")
    supported = ephemeral_providers()
    listed = _words(options, supported, "option") or _words(fallback, supported, "fallback")
    if not listed:
        raise click.UsageError("provide --options or --fallback")
    ordered = tuple(provider for provider in supported if provider in listed)
    chosen = fallback or listed[0]
    if chosen not in ordered:
        raise click.UsageError("--fallback must be one of --options")
    snapshot = settings.load_settings()
    try:
        ephemeral = settings_service.get_ephemeral_settings()
    except settings_service.SettingsUnreadableError as error:
        raise click.ClickException(str(error)) from error
    floor, ceiling = ephemeral.get("min_tier"), ephemeral.get("max_tier")
    tiers = band(size, floor, ceiling, raise_allowed=tier_auto)
    labels = {provider: enforcement_label(provider) for provider in ordered}
    # The same call the engine makes, over the same validated catalog (AC16).
    result = match_launch(snapshot, ordered, labels, chosen, tiers, size, frozenset(wanted))
    digest, key_id = _hash_line(snapshot.match_catalog)
    rows = explain_rows(result)
    if as_json:
        click.echo(
            json.dumps(
                {
                    "rule": result.rule,
                    "needs": list(result.needs),
                    "covered": list(result.covered),
                    "tiebreak": result.tiebreak,
                    "pick": result.pick_label(),
                    "runs": f"{result.provider}/{result.tier}",
                    "catalog_version": snapshot.match_catalog.version,
                    "catalog_hash": digest,
                    "hash_key_id": key_id,
                    "options": list(ordered),
                    "labels": labels,
                    "fallback": chosen,
                    "tiers": list(tiers),
                    "candidates": rows,
                },
                indent=2,
            )
        )
        return
    loaded = snapshot.match_catalog
    head = f"catalog v{loaded.version}" if loaded.valid else f"catalog invalid: {loaded.invalid}"
    click.echo(
        f"{head} · hash hmac:{digest or 'unavailable'} · long_context_tokens "
        f"{loaded.long_context_tokens}"
    )
    band_text = f"{tiers[0]}..{tiers[-1]}" if len(tiers) > 1 else str(tiers[0])
    click.echo(
        "options: "
        + ", ".join(f"{p} ({labels[p]})" for p in ordered)
        + f" · tiers: {band_text} (size {size}, ceiling {ceiling or 'none'}) · fallback {chosen}"
    )
    click.echo("")
    if rows:
        header = ("candidate", "model", "context", "cost", *result.needs, "")
        body = [
            (
                row["candidate"],
                row["model"],
                str(row["context_tokens"]) if row["context_tokens"] is not None else "-",
                row["cost"] or "-",
                *("yes" if row["covers"][need] else "-" for need in result.needs),
                "◄ pick" if row["pick"] else "",
            )
            for row in rows
        ]
        _table([header, *body])
        click.echo("")
    if result.picked:
        click.echo(
            f"rule: covered ({', '.join(result.covered)}) · tie-break: {result.tiebreak} · "
            f"pick: {result.pick_label()}"
        )
    else:
        click.echo(f"rule: {result.rule} · runs the default: {result.provider}/{result.tier}")
