"""Workflow authoring commands for the CLI Agent Orchestrator CLI (issue #312, N2).

Four authoring verbs — ``validate`` / ``list`` / ``get`` / ``delete`` — each a
thin HTTP client against the ``/workflows`` endpoints on the running cao-server
(single integration seam, B2-BR-10). This module NEVER imports
``workflow_spec_service`` or ``database`` directly (project Forbidden rule).

Scope discipline (Q1 / B2-BR-11): ``run`` / ``status`` / ``cancel`` are NOT
registered here — they land in Bolt 3 with the run engine. Invoking
``cao workflow run`` yields click's standard "no such command"; nothing here
stubs or depicts a run.
"""

import json as _json

import click
import requests

from cli_agent_orchestrator.constants import API_BASE_URL, MCP_REQUEST_TIMEOUT


def _extract_detail(response: requests.Response, fallback: str) -> str:
    """Pull the FastAPI ``detail`` string out of an error response."""
    try:
        body = response.json()
        if isinstance(body, dict) and "detail" in body:
            return str(body["detail"])
    except ValueError:
        pass
    return fallback


@click.group()
def workflow():
    """Author and inspect CAO workflow specs."""


@workflow.command(name="validate")
@click.argument("file")
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Emit the ValidationResult as JSON."
)
def validate_cmd(file, as_json):
    """Validate a workflow spec file WITHOUT running it.

    Exit codes:
      0  spec is valid (pass or pass_reserved)
      1  spec failed validation, or the request errored
    """
    try:
        response = requests.post(
            f"{API_BASE_URL}/workflows/validate",
            json={"path": file},
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 400:
        # Out-of-policy path / unreadable source — surfaced as a hard error.
        raise click.ClickException(_extract_detail(response, "invalid request"))
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    result = response.json()
    if as_json:
        click.echo(_json.dumps(result, indent=2))
    else:
        status = result.get("status", "fail")
        if status in ("pass", "pass_reserved"):
            click.echo("valid")
            for note in result.get("reserved_notes", []):
                click.echo(f"  note: {note}")
        else:
            click.echo("invalid", err=True)
            for err in result.get("errors", []):
                click.echo(f"  error: {err}", err=True)

    if result.get("status") == "fail":
        raise click.exceptions.Exit(1)


@workflow.command(name="list")
@click.option("--dir", "scan_dir", default=None, help="Directory to scan for spec files.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the rows as JSON.")
def list_cmd(scan_dir, as_json):
    """List indexed workflows (rebuilt from the spec files on disk)."""
    params = {}
    if scan_dir is not None:
        params["dir"] = scan_dir
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows", params=params, timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 400:
        raise click.ClickException(_extract_detail(response, "invalid request"))
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    rows = response.json()
    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("No workflows found.")
        return
    header = f"{'NAME':<30} {'MODE':<12} {'STEPS':<6} DESCRIPTION"
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            f"{row['name']:<30} {row['mode']:<12} {row['step_count']:<6} {row.get('description', '')}"
        )


@workflow.command(name="get")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the spec as JSON.")
def get_cmd(name, as_json):
    """Show the parsed/validated spec for a workflow name or file path."""
    try:
        response = requests.get(f"{API_BASE_URL}/workflows/{name}", timeout=MCP_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown workflow '{name}'")
    if response.status_code == 400:
        raise click.ClickException(_extract_detail(response, "invalid request"))
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    spec = response.json()
    if as_json:
        click.echo(_json.dumps(spec, indent=2))
        return
    click.echo(f"Name:        {spec['name']}")
    click.echo(f"Mode:        {spec['mode']}")
    click.echo(f"Description: {spec.get('description', '') or '(none)'}")
    click.echo(f"Steps:       {len(spec.get('steps', []))}")
    for step in spec.get("steps", []):
        click.echo(f"  - {step['id']} ({step['provider']}/{step['agent']})")


@workflow.command(name="delete")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
def delete_cmd(name, yes):
    """Delete a workflow's spec file and its index row."""
    if not yes:
        click.confirm(f"Delete workflow '{name}'?", abort=True)
    try:
        response = requests.delete(f"{API_BASE_URL}/workflows/{name}", timeout=MCP_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown workflow '{name}'")
    if response.status_code == 400:
        raise click.ClickException(_extract_detail(response, "invalid request"))
    if response.status_code not in (200, 204):
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))
    click.echo(f"deleted '{name}'")
