"""Workflow authoring commands for the CLI Agent Orchestrator CLI (issue #312, N2).

Four authoring verbs — ``validate`` / ``list`` / ``get`` / ``delete`` — each a
thin HTTP client against the ``/workflows`` endpoints on the running cao-server
(single integration seam, B2-BR-10). This module NEVER imports
``workflow_spec_service`` or ``database`` directly (project Forbidden rule).

The Bolt-3 run verbs — ``run`` / ``status`` / ``cancel`` — are thin HTTP clients
over the ``/workflows/runs`` engine endpoints (N5), mirroring the authoring-verb
style. They never import ``workflow_service`` directly (project Forbidden rule).
"""

import json as _json

import click
import requests

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    MCP_REQUEST_TIMEOUT,
    WORKFLOW_RUN_REQUEST_TIMEOUT,
)


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


def _parse_inputs(pairs):
    """Parse ``--input k=v`` pairs into an inputs dict with light type coercion.

    Each value is coerced from its string form: ``true``/``false`` -> bool,
    a bare integer -> int, everything else stays a string. This keeps the CLI
    ergonomic while the engine still validates every value against the spec's
    declared ``InputDecl`` types (a coercion that disagrees with the declared
    type surfaces as a 400 from the engine).
    """
    inputs = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.ClickException(f"--input must be k=v (got '{pair}')")
        key, _, raw = pair.partition("=")
        key = key.strip()
        if not key:
            raise click.ClickException(f"--input key is empty (got '{pair}')")
        inputs[key] = _coerce(raw)
    return inputs


def _coerce(raw):
    """Coerce a raw ``--input`` value string to bool / int / str."""
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


@workflow.command(name="run")
@click.argument("name_or_path")
@click.option("--input", "inputs", multiple=True, help="Run input as k=v (repeatable).")
@click.option("--run-id", "run_id", default=None, help="Optional explicit run id.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def run_cmd(name_or_path, inputs, run_id, as_json):
    """Run a workflow to completion (blocks until the run finishes).

    Exit codes:
      0  run reached COMPLETED
      1  run reached FAILED / CANCELLED, or the request errored
    """
    parsed = _parse_inputs(inputs)
    payload = {"name_or_path": name_or_path, "inputs": parsed}
    if run_id is not None:
        payload["run_id"] = run_id
    try:
        # ``run`` blocks until the whole workflow finishes (the server awaits the run
        # inline, Q1=A), so use the worst-case-covering run timeout, NOT the flat 30s
        # MCP_REQUEST_TIMEOUT (which would report a still-running run as a failure).
        response = requests.post(
            f"{API_BASE_URL}/workflows/runs",
            json=payload,
            timeout=WORKFLOW_RUN_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(_extract_detail(response, f"unknown workflow '{name_or_path}'"))
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    result = response.json()
    if as_json:
        click.echo(_json.dumps(result, indent=2))
    else:
        click.echo(f"Run:   {result.get('run_id')}")
        click.echo(f"State: {result.get('state')}")
        for step in result.get("steps", []):
            click.echo(f"  - {step['id']}: {step['state']} (attempts={step.get('attempts')})")

    if result.get("state") != "completed":
        raise click.exceptions.Exit(1)


@workflow.command(name="status")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the snapshot as JSON.")
def status_cmd(run_id, as_json):
    """Show a point-in-time status snapshot for a run."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs/{run_id}", timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown run '{run_id}'")
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    snapshot = response.json()
    if as_json:
        click.echo(_json.dumps(snapshot, indent=2))
        return
    click.echo(f"Run:     {snapshot.get('run_id')}")
    click.echo(f"State:   {snapshot.get('state')}")
    click.echo(f"Current: {snapshot.get('current_step_id') or '(none)'}")
    for step in snapshot.get("steps", []):
        click.echo(f"  - {step['id']}: {step['state']} (attempts={step.get('attempts')})")


@workflow.command(name="resume")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def resume_cmd(run_id, as_json):
    """Resume a crashed/failed run from its durable journal (blocks until done).

    Skips already-completed steps and re-runs the rest. Exit codes:
      0  run reached COMPLETED
      1  run reached FAILED / CANCELLED, or the request errored
    """
    try:
        # Resume re-drives the run inline (the server awaits it), so use the
        # worst-case-covering run timeout, not the flat MCP_REQUEST_TIMEOUT.
        response = requests.post(
            f"{API_BASE_URL}/workflows/runs/{run_id}/resume",
            timeout=WORKFLOW_RUN_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown run '{run_id}'")
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    result = response.json()
    if as_json:
        click.echo(_json.dumps(result, indent=2))
    else:
        click.echo(f"Run:   {result.get('run_id')}")
        click.echo(f"State: {result.get('state')}")
        for step in result.get("steps", []):
            click.echo(f"  - {step['id']}: {step['state']} (attempts={step.get('attempts')})")

    if result.get("state") != "completed":
        raise click.exceptions.Exit(1)


@workflow.command(name="cancel")
@click.argument("run_id")
def cancel_cmd(run_id):
    """Cooperatively cancel a running workflow."""
    try:
        response = requests.post(
            f"{API_BASE_URL}/workflows/runs/{run_id}/cancel", timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown run '{run_id}'")
    if response.status_code == 409:
        raise click.ClickException(_extract_detail(response, "run is already finished"))
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))
    click.echo(f"cancelling '{run_id}'")
