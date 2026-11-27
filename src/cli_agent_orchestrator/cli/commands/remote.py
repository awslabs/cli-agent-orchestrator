"""Experimental Remote CAO registration, checks and consent-gated connect."""

from __future__ import annotations

import json
from pathlib import Path

import click

from cli_agent_orchestrator.services.remote.checks import check_box, check_json
from cli_agent_orchestrator.services.remote.connect import (
    ConnectError,
    connect,
    has_ssh_artifacts,
    remove_ssh_artifacts,
)
from cli_agent_orchestrator.services.remote.registry import (
    RemoteBox,
    RemoteConfigError,
    list_boxes,
    load,
    remove,
    save,
)
from cli_agent_orchestrator.services.remote.runner import Runner


@click.group()
def remote() -> None:
    """Register, check and connect to experimental Remote CAO boxes."""


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
@click.option("--yes", is_flag=True, help="Approve SSH artifact cleanup without prompting.")
def remove_cmd(box: str, yes: bool) -> None:
    """Remove a local Remote CAO box registration."""
    try:
        remote_box = load(box)
        home = Path.home()
        if has_ssh_artifacts(remote_box, home):
            action = f"Remove managed SSH entries for cao-{remote_box.name}"
            click.echo(action)
            if not (yes or click.confirm(f"{action}?", default=False)):
                raise click.exceptions.Exit(1)
            remove_ssh_artifacts(remote_box, home)
        remove(remote_box.name)
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


@remote.command("connect")
@click.argument("box")
@click.option("--no-attach", is_flag=True, help="Print connection instructions without attaching.")
@click.option("--yes", is_flag=True, help="Approve each setup action without prompting.")
@click.option("--rekey", is_flag=True, help="Replace a changed pinned host key after confirmation.")
def connect_cmd(box: str, no_attach: bool, yes: bool, rekey: bool) -> None:
    """Set up an experimental Remote CAO connection after confirmation."""
    try:
        remote_box = load(box)
        status = connect(
            remote_box,
            runner=Runner(),
            home=Path.home(),
            ask=lambda action: yes or click.confirm(f"{action}?", default=False),
            tell=click.echo,
            no_attach=no_attach,
            rekey=rekey,
        )
        if status:
            raise click.exceptions.Exit(status)
    except RemoteConfigError as exc:
        raise click.UsageError(str(exc)) from exc
    except ConnectError as exc:
        click.echo(str(exc), err=True)
        raise click.exceptions.Exit(1) from exc
