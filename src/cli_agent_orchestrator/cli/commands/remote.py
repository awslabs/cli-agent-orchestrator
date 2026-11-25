"""Experimental, read-only Remote CAO prerequisites."""

from __future__ import annotations

import json

import click

from cli_agent_orchestrator.services.remote.checks import check_box, check_json
from cli_agent_orchestrator.services.remote.registry import (
    RemoteBox,
    RemoteConfigError,
    list_boxes,
    load,
    remove,
    save,
)


@click.group()
def remote() -> None:
    """Check experimental Remote CAO laptop prerequisites."""


@remote.command("add")
@click.argument("box")
@click.option("--instance-id", required=True)
@click.option("--region", required=True)
@click.option("--profile", required=True)
@click.option("--mode", type=click.Choice(["herdr", "tmux"]), default="herdr", show_default=True)
@click.option("--user")
def add(box: str, instance_id: str, region: str, profile: str, mode: str, user: str | None) -> None:
    """Register a Remote CAO box locally."""
    try:
        remote_box = RemoteBox(box, instance_id, region, profile, mode, user or "")
        save(remote_box)
    except RemoteConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(f"Added remote box '{remote_box.name}'.")


@remote.command("list")
def list_cmd() -> None:
    """List locally registered Remote CAO boxes."""
    boxes = list_boxes()
    if not boxes:
        click.echo("No remote boxes registered.")
        return
    for box in boxes:
        click.echo(
            f"{box.name}\t{box.instance_id}\t{box.region}\t{box.aws_profile}\t{box.mode}\t{box.user}"
        )


@remote.command("remove")
@click.argument("box")
def remove_cmd(box: str) -> None:
    """Remove a local Remote CAO box registration."""
    try:
        remove(box)
    except RemoteConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    click.echo(f"Removed remote box '{box}'.")


@remote.command("check")
@click.argument("box")
@click.option("--json", "as_json", is_flag=True, help="Print machine-readable check results.")
def check_cmd(box: str, as_json: bool) -> None:
    """Read-only prerequisite checks; never modifies the laptop or box."""
    try:
        checks = check_box(load(box))
    except RemoteConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    if as_json:
        click.echo(json.dumps(check_json(checks), sort_keys=True))
    else:
        for result in checks:
            click.echo(result.rendered())
    if any(result.status == "FIX" for result in checks):
        raise click.exceptions.Exit(1)
