"""Workflow authoring commands for the CLI Agent Orchestrator CLI (issue #312, N2).

Four authoring verbs — ``validate`` / ``list`` / ``get`` / ``delete`` — each a
thin HTTP client against the ``/workflows`` endpoints on the running cao-server
(single integration seam, B2-BR-10). This module NEVER imports
``workflow_spec_service`` or ``database`` directly (project Forbidden rule).

The run-lifecycle verbs — ``run`` / ``runs`` / ``status`` / ``wait`` / ``result``
/ ``resume`` / ``cancel`` — are thin HTTP clients over the ``/workflows/runs``
engine endpoints (N5 + issue #505), mirroring the authoring-verb style. Bare
``run`` submits asynchronously via ``POST /workflows/runs:submit`` and then FOLLOWS
the run by polling ``GET /workflows/runs/{id}`` to a terminal state; ``--wait`` is
the explicit blocking escape hatch over the retained ``POST /workflows/runs`` path
and ``--detach`` submits without following. This module NEVER imports
``workflow_service`` / ``script_runner`` / ``workflow_journal`` / ``database``
directly (project Forbidden rule + issue #505 C-2, CI import guard) — every verb
reaches its data over the REST surface only.
"""

import json as _json
import sys
import time

import click
import requests

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    MCP_REQUEST_TIMEOUT,
    WORKFLOW_EVENTS_CONNECT_TIMEOUT,
    WORKFLOW_EVENTS_MAX_RECONNECTS,
    WORKFLOW_EVENTS_READ_TIMEOUT,
    WORKFLOW_POLL_INTERVAL_SECONDS,
    WORKFLOW_RUN_REQUEST_TIMEOUT,
)
from cli_agent_orchestrator.utils.workflow_events import SseFrame, parse_sse_frames

# Whole-run states that end the follow/poll loop (mirror ``RunState``'s terminal
# members without importing the engine model — C-2 keeps this a thin HTTP client).
_TERMINAL_RUN_STATES = frozenset({"completed", "failed", "cancelled"})


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
        # A script-tier spec has no static step count (it is determined at run
        # time), so its index row carries ``step_count=None``. Render that as a
        # ``-`` placeholder — formatting ``None`` with the ``:<6`` numeric field
        # would raise a TypeError and crash the whole listing.
        step_count = row.get("step_count")
        steps_cell = "-" if step_count is None else str(step_count)
        click.echo(
            f"{row['name']:<30} {row['mode']:<12} {steps_cell:<6} {row.get('description', '')}"
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


# The decisions ``cao workflow resume --decide`` accepts (issue #583, FR-7, BR-10).
# Mirrors ``RecoveryDecision``'s members WITHOUT importing the model, exactly as
# ``_TERMINAL_RUN_STATES`` above mirrors ``RunState`` — C-2 keeps this a thin HTTP
# client. ``test_recovery_decision_intake.py`` pins this tuple against the enum, so a
# rename or a third member fails loudly instead of leaving the CLI rejecting a value
# the server accepts (or worse, accepting one the server rejects).
_RECOVERY_DECISIONS = ("rerun", "skip")


def _parse_decisions(pairs):
    """Parse ``--decide <step_id>=<rerun|skip>`` pairs into a decisions dict (FR-7).

    Rejected locally rather than round-tripped, so a typo costs no request and the
    operator is told the accepted values. The server re-validates and remains the
    authority — this check exists to agree with it, never to replace it (BR-10).

    A REPEATED ``step_id`` is an error rather than last-wins: two decisions for one
    step is an operator mistake, and silently dropping one would apply a permission
    the operator did not think they were granting.
    """
    decisions = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.ClickException(f"--decide must be <step_id>=<decision> (got '{pair}')")
        step_id, _, raw = pair.partition("=")
        step_id = step_id.strip()
        value = raw.strip()
        if not step_id:
            raise click.ClickException(f"--decide step id is empty (got '{pair}')")
        if value not in _RECOVERY_DECISIONS:
            accepted = " | ".join(_RECOVERY_DECISIONS)
            raise click.ClickException(
                f"--decide {step_id}: '{value[:40]}' is not a recovery decision "
                f"(accepted: {accepted})"
            )
        if step_id in decisions:
            raise click.ClickException(f"--decide names step '{step_id}' more than once")
        decisions[step_id] = value
    return decisions


def _coerce(raw):
    """Coerce a raw ``--input`` value string to bool / int / str."""
    low = raw.strip().lower()
    if low in ("true", "false"):
        return low == "true"
    try:
        return int(raw)
    except ValueError:
        return raw


def _render_snapshot(snapshot):
    """Human-render one status snapshot line-set (run id, state, current, steps)."""
    click.echo(f"Run:     {snapshot.get('run_id')}")
    click.echo(f"State:   {snapshot.get('state')}")
    click.echo(f"Current: {snapshot.get('current_step_id') or '(none)'}")
    for step in snapshot.get("steps", []):
        click.echo(f"  - {step['id']}: {step['state']} (attempts={step.get('attempts')})")


def _render_result(result):
    """Human-render a terminal run result (run id, state, per-step lines).

    U5 renders the plain result; U9 enriches the human render with the failure
    envelope (``failing_step`` / ``attempt`` / ``error_kind`` / ``terminal_reference``
    / ``next_command``) for a failed/cancelled run. ``--json`` emits the server body
    verbatim (the envelope is already in the body), so this human-only enrichment
    never touches the machine contract (NFR-3).
    """
    click.echo(f"Run:   {result.get('run_id')}")
    click.echo(f"State: {result.get('state')}")
    kind = result.get("kind")
    if kind:
        click.echo(f"Kind:  {kind}")
    for step in result.get("steps", []):
        click.echo(f"  - {step['id']}: {step['state']} (attempts={step.get('attempts')})")
    _render_failure_envelope(result.get("failure_envelope"))


def _render_failure_envelope(envelope):
    """Human-render the U9 failure envelope beneath a failed/cancelled result (FR-7.1).

    A no-op when ``envelope`` is absent (a completed/non-terminal run carries none).
    The ``next_command`` hint points the operator at the diagnostic command.
    """
    if not envelope:
        return
    click.echo("Failure:")
    click.echo(f"  failing step:       {envelope.get('failing_step') or '(none)'}")
    click.echo(f"  attempt:            {envelope.get('attempt')}")
    click.echo(f"  error kind:         {envelope.get('error_kind') or '(none)'}")
    click.echo(f"  terminal reference: {envelope.get('terminal_reference')}")
    click.echo(f"  next command:       {envelope.get('next_command')}")


def _exit_for_state(state):
    """Exit-code contract (U5-EC-1): ``completed`` -> 0, any other terminal -> 1.

    Applies identically on a TTY, a non-TTY, and under ``--json`` (EC-2/EC-3) — the
    output FORMAT changes but the exit code always mirrors the terminal state.
    """
    if state != "completed":
        raise click.exceptions.Exit(1)


def _machine_mode(as_json):
    """Whether the async follower should emit stable machine output (EC-2/EC-3).

    Machine mode is on when ``--json`` is set OR stdout is not a TTY. A non-TTY
    follow (a pipe, a CI log, a captured subprocess) still follows to terminal and
    still exits by the terminal-status contract, but emits a single stable JSON
    object instead of the interactive human progress stream — so an automated
    consumer gets a parseable result and the mandated non-TTY exit-code test
    (NFR-2b) holds regardless of the render path.
    """
    return bool(as_json) or not sys.stdout.isatty()


def _poll_to_terminal(run_id, as_json):
    """Poll ``GET /workflows/runs/{run_id}`` until the run reaches a terminal state.

    ADR-4 Option A: a fixed-interval poll of the snapshot route (NOT the events
    stream — that live follower is U10, FP-2). Renders progress between polls
    unless ``as_json`` is set (a machine consumer wants only the final JSON, not a
    stream of human lines). Progress is keyed on the ``(state, current_step_id)`` PAIR,
    not the run state alone — the run state is ``running`` for the ENTIRE drive, so
    state-only keying prints one line and then goes silent for the whole run (FP-6).
    Each poll uses the normal per-call ``MCP_REQUEST_TIMEOUT``
    (FP-4 — the long ``WORKFLOW_RUN_REQUEST_TIMEOUT`` bounds only the ``--wait``
    blocking call, never a single snapshot read), sleeping
    ``WORKFLOW_POLL_INTERVAL_SECONDS`` between polls (FP-5).

    A poll transport error is NOT run death (FP-3): after ONE bounded retry it
    prints a "lost contact ... run continues" hint and returns ``None`` so the
    caller can exit 0 (a lost socket must never be reported as a failed run).
    Returns the terminal state string, or ``None`` on lost contact.
    """
    transport_failures = 0
    last_state = None
    last_step = None
    while True:
        try:
            response = requests.get(
                f"{API_BASE_URL}/workflows/runs/{run_id}", timeout=MCP_REQUEST_TIMEOUT
            )
        except requests.exceptions.RequestException as e:
            transport_failures += 1
            if transport_failures > 1:
                # One bounded retry exhausted — a lost socket is not a failed run.
                click.echo(
                    f"lost contact with cao-server ({e}); run '{run_id}' continues — "
                    f"use `cao workflow status {run_id}` to reconnect.",
                    err=True,
                )
                return None
            time.sleep(WORKFLOW_POLL_INTERVAL_SECONDS)
            continue

        if response.status_code == 404:
            raise click.ClickException(f"unknown run '{run_id}'")
        if response.status_code != 200:
            raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

        # A successful poll resets the bounded-retry budget.
        transport_failures = 0
        snapshot = response.json()
        state = snapshot.get("state")
        current = snapshot.get("current_step_id") or "(none)"
        # Print on a change of EITHER the run state OR the current step (FP-6). Run
        # state alone is not enough to be a progress display: it is ``running`` for
        # the ENTIRE drive, so a keying-on-state-only loop prints one line and then
        # goes silent until the run finishes — a 10-step, 40-minute workflow looked
        # identical to a hung one. ``current_step_id`` is already in the snapshot and
        # advances per step, so keying on the (state, step) PAIR turns the same poll
        # into real per-step progress with no extra requests.
        if not as_json and (state, current) != (last_state, last_step):
            click.echo(f"[{state}] current: {current}")
            last_state = state
            last_step = current

        if state in _TERMINAL_RUN_STATES:
            return state
        time.sleep(WORKFLOW_POLL_INTERVAL_SECONDS)


@workflow.command(name="run")
@click.argument("name_or_path")
@click.option("--input", "inputs", multiple=True, help="Run input as k=v (repeatable).")
@click.option("--run-id", "run_id", default=None, help="Optional explicit run id.")
@click.option(
    "--detach",
    is_flag=True,
    default=False,
    help="Submit and return the run id immediately without following (exit 0 on submit).",
)
@click.option(
    "--wait",
    is_flag=True,
    default=False,
    help="Block on the server inline until the run finishes (the retained blocking path).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def run_cmd(name_or_path, inputs, run_id, detach, wait, as_json):
    """Run a workflow.

    By default (FR-4, issue #505) ``run`` SUBMITS the run asynchronously, prints
    the run id immediately, then FOLLOWS it — polling status to a terminal state.
    Ctrl-C during the follow DETACHES (the run keeps running server-side; it is
    never cancelled). ``--detach`` submits and returns the id without following.
    ``--wait`` is the explicit blocking escape hatch (the retained inline path).

    Exit codes (identical on a TTY, a non-TTY, and under ``--json``):
      0  run reached COMPLETED (or ``--detach`` submitted, or Ctrl-C detached)
      1  run reached FAILED / CANCELLED, or the request errored
    """
    parsed = _parse_inputs(inputs)
    payload = {"name_or_path": name_or_path, "inputs": parsed}
    if run_id is not None:
        payload["run_id"] = run_id

    # --- --wait: the retained blocking path (VR-2, FR-4.5). ------------------
    # ``--wait`` blocks on the server inline until the whole workflow finishes, so
    # it keeps the worst-case-covering WORKFLOW_RUN_REQUEST_TIMEOUT — the flat 30s
    # MCP_REQUEST_TIMEOUT would report a still-running run as a failure.
    if wait:
        try:
            response = requests.post(
                f"{API_BASE_URL}/workflows/runs",
                json=payload,
                timeout=WORKFLOW_RUN_REQUEST_TIMEOUT,
            )
        except requests.exceptions.RequestException as e:
            raise click.ClickException(f"could not reach cao-server: {e}")
        if response.status_code == 404:
            raise click.ClickException(
                _extract_detail(response, f"unknown workflow '{name_or_path}'")
            )
        if response.status_code != 200:
            raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))
        result = response.json()
        if as_json:
            click.echo(_json.dumps(result, indent=2))
        else:
            _render_result(result)
        _exit_for_state(result.get("state"))
        return

    # --- default + --detach: submit asynchronously via the :submit spine. ----
    try:
        response = requests.post(
            f"{API_BASE_URL}/workflows/runs:submit",
            json=payload,
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(_extract_detail(response, f"unknown workflow '{name_or_path}'"))
    if response.status_code != 202:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    submitted = response.json()
    submitted_run_id = submitted.get("run_id")
    links = submitted.get("links", {})
    machine = _machine_mode(as_json)

    # FP-1: surface the run id IMMEDIATELY on the 202, before any follow loop, so
    # the handle survives a Ctrl-C on the very first poll. In machine mode the id
    # is carried by the final JSON object (and by the interrupt hint on stderr) —
    # a human "Run: id" line here would corrupt the stdout JSON.
    if not machine:
        click.echo(f"Run:   {submitted_run_id}")
        click.echo(f"State: {submitted.get('state')}")

    # --- --detach: return right after a successful submit (VR-1, FR-4.4). ----
    if detach:
        if machine:
            click.echo(_json.dumps(submitted, indent=2))
        else:
            for name, href in links.items():
                click.echo(f"  {name}: {href}")
            click.echo(f"detached; follow with `cao workflow wait {submitted_run_id}`")
        return

    # --- bare run: FOLLOW to terminal. Ctrl-C detaches, never cancels (CC-1). -
    try:
        terminal_state = _poll_to_terminal(submitted_run_id, machine)
    except KeyboardInterrupt:
        # CC-1 (load-bearing): a Ctrl-C during the follow DETACHES — it prints the
        # handle + hint (to stderr, so it never corrupts stdout JSON) and exits 0.
        # It MUST NOT POST the cancel route; the run keeps running server-side.
        click.echo(
            f"\ndetached from '{submitted_run_id}'; run still running — "
            f"use `cao workflow status {submitted_run_id}` or "
            f"`cao workflow cancel {submitted_run_id}`.",
            err=True,
        )
        return

    # FP-3: lost contact (poll transport error, retry exhausted) is not run death —
    # ``_poll_to_terminal`` already printed the hint and returns None; exit 0.
    if terminal_state is None:
        return

    if machine:
        # EC-2/EC-3: a non-TTY or ``--json`` follow emits ONE stable machine object.
        click.echo(_json.dumps({"run_id": submitted_run_id, "state": terminal_state}, indent=2))
    else:
        click.echo(f"State: {terminal_state}")

    # EC-1/EC-2: the exit code mirrors the terminal state on every output mode.
    _exit_for_state(terminal_state)


def _resolve_latest_run_id():
    """Resolve the most-recently-started run id via ``GET /workflows/runs?limit=1``.

    Backs no-id ``status`` (VR-4, FR-4.8): the list route is ORDER BY
    ``started_at DESC, run_id DESC`` (U1/U4), so the first row is the most recent.
    Returns ``None`` when no runs exist (the caller prints "no runs found").
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs",
            params={"limit": 1},
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))
    rows = response.json()
    if not rows:
        return None
    return rows[0].get("run_id")


@workflow.command(name="status")
@click.argument("run_id", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the snapshot as JSON.")
def status_cmd(run_id, as_json):
    """Show a point-in-time status snapshot for a run.

    With no RUN_ID (VR-4, FR-4.8) resolves the most-recently-started run and shows
    that one; an empty run list prints "no runs found" and exits 0.
    """
    if run_id is None:
        run_id = _resolve_latest_run_id()
        if run_id is None:
            click.echo("no runs found")
            return

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
    _render_snapshot(snapshot)


@workflow.command(name="runs")
@click.option("--state", "state", default=None, help="Filter by run state (e.g. running, failed).")
@click.option("--limit", "limit", type=int, default=None, help="Max rows to return (server caps).")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the rows as JSON.")
def runs_cmd(state, limit, as_json):
    """List workflow RUNS newest-first (distinct from ``list``, which lists specs).

    ``cao workflow runs`` shows submitted/finished runs and their state;
    ``cao workflow list`` shows the indexed workflow SPECS on disk. Optional
    ``--state`` filters by run state and ``--limit`` caps the row count.
    """
    params = {}
    if state is not None:
        params["state"] = state
    if limit is not None:
        params["limit"] = limit
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs", params=params, timeout=MCP_REQUEST_TIMEOUT
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
        click.echo("No runs found.")
        return
    header = f"{'RUN_ID':<28} {'WORKFLOW':<24} {'STATE':<10} {'TIER':<8} STARTED"
    click.echo(header)
    click.echo("-" * len(header))
    for row in rows:
        click.echo(
            f"{row.get('run_id', ''):<28} {row.get('workflow_name', ''):<24} "
            f"{row.get('state', ''):<10} {row.get('tier', ''):<8} {row.get('started_at', '')}"
        )


@workflow.command(name="wait")
@click.argument("run_id")
@click.option(
    "--json", "as_json", is_flag=True, default=False, help="Emit the terminal state as JSON."
)
def wait_cmd(run_id, as_json):
    """Follow an existing run by polling its status until it reaches a terminal state.

    Exit codes (identical on a TTY, a non-TTY, and under ``--json``):
      0  run reached COMPLETED (or contact with the server was lost mid-follow)
      1  run reached FAILED / CANCELLED, or the request errored
    """
    terminal_state = _poll_to_terminal(run_id, as_json)
    # FP-3: lost contact is not run death — the helper printed the hint; exit 0.
    if terminal_state is None:
        return
    if as_json:
        click.echo(_json.dumps({"run_id": run_id, "state": terminal_state}, indent=2))
    else:
        click.echo(f"Run:   {run_id}")
        click.echo(f"State: {terminal_state}")
    _exit_for_state(terminal_state)


@workflow.command(name="result")
@click.argument("run_id")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def result_cmd(run_id, as_json):
    """Show the complete retained result for a (finished or in-flight) run."""
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs/{run_id}/result", timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        raise click.ClickException(f"unknown run '{run_id}'")
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    result = response.json()
    if as_json:
        # EC-3 / NFR-3: emit the server body verbatim so it is stable/round-trippable.
        click.echo(_json.dumps(result, indent=2))
        return
    _render_result(result)


@workflow.command(name="resume")
@click.argument("run_id")
@click.option(
    "--decide",
    "decide",
    multiple=True,
    metavar="STEP_ID=DECISION",
    help=(
        "Resolve a halted step: --decide <step_id>=rerun|skip. 'rerun' authorises "
        "re-executing it; 'skip' authorises using its stored result. Repeatable."
    ),
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
def resume_cmd(run_id, decide, as_json):
    """Resume a crashed/failed run from its durable journal (blocks until done).

    A script-tier resume RE-EXECUTES THE SCRIPT TOP-TO-BOTTOM; completed steps are
    NOT skipped. Each step is decided as it arrives and is either REPLAYED (its
    stored result is returned and nothing runs), EXECUTED (it runs again), or HALTED
    (CAO will not decide alone, so the run stops there and waits for a --decide).
    A step whose script changed at the same key DIVERGES and fails the run instead.

    Exit codes:
      0  run reached COMPLETED
      1  run reached FAILED / CANCELLED, or the request errored

    ``--decide`` carries a decision for a step the run halted on (issue #583, FR-7).
    Each decision authorises exactly ONE attempt: if that attempt crashes before it
    settles, the next resume asks again rather than re-executing on old consent.
    """
    decisions = _parse_decisions(decide)
    try:
        # Resume re-drives the run inline (the server awaits it), so use the
        # worst-case-covering run timeout, not the flat MCP_REQUEST_TIMEOUT.
        # ``json=None`` sends NO body, so a decision-free resume is byte-identical to
        # the pre-#583 request.
        response = requests.post(
            f"{API_BASE_URL}/workflows/runs/{run_id}/resume",
            json={"decisions": decisions} if decisions else None,
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


# ===========================================================================
# events — U10 live-event follower (issue #505, FR-4.9). A CLIENT-SIDE consumer
# of #504's events-follow SSE route (``GET /workflows/runs/{id}/events`` with
# ``Accept: text/event-stream``). Thin HTTP client: it opens the stream, renders
# the DECLARED frames, resumes exactly via ``?after_seq``, and closes on a
# terminal projection state — importing NO engine / journal / event DAL (FR-7.4).
# ===========================================================================
def _open_events_stream(run_id: str, cursor):
    """Open the SSE variant of the events route, resuming from ``cursor`` when set.

    Requests ``Accept: text/event-stream`` for the live-follow variant. When a
    cursor (last-seen ``seq``) is present it is sent as ``?after_seq=<cursor>``
    (the authority — the contract has ``?after_seq`` WIN over ``Last-Event-ID``)
    AND mirrored into the ``Last-Event-ID`` header, so resume is exact and
    dedupe-free (RS-1/RS-3). A ``(connect, read)`` timeout tuple keeps a quiet
    run from tripping a spurious read timeout between frames.
    """
    params = {}
    headers = {"Accept": "text/event-stream"}
    if cursor is not None:
        params["after_seq"] = cursor
        headers["Last-Event-ID"] = str(cursor)
    return requests.get(
        f"{API_BASE_URL}/workflows/runs/{run_id}/events",
        params=params,
        headers=headers,
        stream=True,
        timeout=(WORKFLOW_EVENTS_CONNECT_TIMEOUT, WORKFLOW_EVENTS_READ_TIMEOUT),
    )


def _events_route_or_run_missing(run_id: str) -> click.ClickException:
    """Turn an ambiguous 404 from the events route into the RIGHT error (CD-1).

    The events route (``GET /workflows/runs/{run_id}/events``) ships with issue
    #504. Until that lands, every request to it 404s — for healthy runs included —
    and the naive reading ("unknown run") is actively misleading: it points the
    operator at their run instead of at the missing capability.

    The snapshot route is present in every build, so it discriminates: if the run
    is READABLE there, the 404 came from the absent route, not a missing run.
    A transport failure on the probe falls back to the run-scoped message rather
    than asserting a server capability it could not verify.
    """
    try:
        probe = requests.get(f"{API_BASE_URL}/workflows/runs/{run_id}", timeout=MCP_REQUEST_TIMEOUT)
    except requests.exceptions.RequestException:
        return click.ClickException(f"unknown run '{run_id}'")
    if probe.status_code == 200:
        return click.ClickException(
            f"this cao-server has no live event stream for run '{run_id}' "
            f"(GET /workflows/runs/{run_id}/events is not available on this build); "
            f"the run itself is fine — follow it with `cao workflow wait {run_id}` "
            f"or read a snapshot with `cao workflow status {run_id}`."
        )
    return click.ClickException(f"unknown run '{run_id}'")


def _render_event_frame(frame: SseFrame, machine: bool) -> None:
    """Render one NORMAL event frame (event_type, step_id, state, seq), in order.

    Machine mode emits one stable JSON line per frame (a JSONL follow stream);
    the human render is a single progress line.
    """
    if machine:
        click.echo(
            _json.dumps(
                {
                    "kind": "event",
                    "seq": frame.seq(),
                    "run_id": frame.data.get("run_id"),
                    "event_type": frame.event,
                    "step_id": frame.data.get("step_id"),
                    "state": frame.data.get("state"),
                    "ts": frame.data.get("ts"),
                }
            )
        )
    else:
        step = frame.data.get("step_id") or "(run)"
        click.echo(f"[seq {frame.seq()}] {frame.event}: {step} -> {frame.data.get('state')}")


def _render_gap_frame(frame: SseFrame, machine: bool) -> None:
    """Render a SERVER-DECLARED ``event: gap`` frame verbatim — RENDER, do not infer.

    Load-bearing (GD-1): this renders exactly what the gap frame's data DECLARES
    (``after_seq`` / ``before_seq`` / ``missing_count`` / ``reason``). It NEVER
    computes a gap from ``seq`` arithmetic — a gap exists on-screen iff the server
    sent an ``event: gap`` frame, so an independent client inference can never
    disagree with the server's authoritative declaration.
    """
    d = frame.data
    if machine:
        click.echo(
            _json.dumps(
                {
                    "kind": "gap",
                    "after_seq": d.get("after_seq"),
                    "before_seq": d.get("before_seq"),
                    "missing_count": d.get("missing_count"),
                    "reason": d.get("reason"),
                }
            )
        )
    else:
        click.echo(
            f"⚠ gap: {d.get('missing_count')} event(s) lost between seq "
            f"{d.get('after_seq')} and {d.get('before_seq')} (reason: {d.get('reason')})"
        )


def _stream_event_frames(run_id: str, cursor):
    """Open ONE SSE connection and YIELD its frames (a generator).

    Raises ``click.ClickException`` on 404 / non-200 (a hard error, never a
    reconnect). A transport error raised by the streamed ``iter_lines`` read
    propagates OUT of this generator to the caller — which is why the caller
    tracks the resume cursor in ITS OWN scope as frames are yielded: on a dropped
    connection the cursor is already advanced to the last seen ``seq``, so the
    reconnect resumes exactly (``?after_seq=<cursor>``) with no re-delivery.

    FD-1 (PR #525 review): the response is closed on EVERY exit path via
    ``try``/``finally``, matching the hardening already applied to the MCP twin
    ``workflow_events``. ``stream=True`` holds the connection open until it is
    explicitly closed or fully drained, and this generator is routinely abandoned
    WITHOUT draining: the caller ``break``s out of its loop the moment a terminal
    frame arrives, and every reconnect leaves the prior generator suspended. Without
    the ``finally`` the socket/FD survives until the generator is garbage collected —
    so a long follow with repeated reconnects accumulates live sockets. The ``finally``
    runs on the terminal-frame break too, because abandoning a suspended generator
    raises ``GeneratorExit`` into it at collection.
    """
    response = _open_events_stream(run_id, cursor)
    try:
        if response.status_code == 404:
            # A 404 here is AMBIGUOUS and the two causes need opposite messages (CD-1):
            # the RUN may be unknown, or the events ROUTE may not exist on this server
            # (it ships with issue #504; until that merges, this path 404s for every
            # perfectly healthy run). Reporting "unknown run" for a live run sends the
            # operator hunting a nonexistent problem, so discriminate against the
            # snapshot route — which this build always has — before naming the cause.
            raise _events_route_or_run_missing(run_id)
        if response.status_code != 200:
            raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))
        yield from parse_sse_frames(response.iter_lines(decode_unicode=True))
    finally:
        response.close()


def _final_events_status(run_id: str):
    """Read the snapshot route ONCE; return the terminal state, or ``None``.

    The F-1 terminal-guard safety net for the follower: if the stream ends (or the
    reconnect budget is exhausted) before a terminal EVENT was seen, this proves
    whether the run is actually terminal without depending on that swallowed
    event. A transport error or a non-terminal state both yield ``None`` — a lost
    socket is never reported as a failed run.
    """
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs/{run_id}", timeout=MCP_REQUEST_TIMEOUT
        )
    except requests.exceptions.RequestException:
        return None
    if response.status_code != 200:
        return None
    state = response.json().get("state")
    return state if state in _TERMINAL_RUN_STATES else None


def _events_batch_read(run_id: str, after_seq, as_json: bool) -> None:
    """One-shot batch read (``--no-follow``): the JSON variant of the same route.

    Content-negotiated: with no ``Accept: text/event-stream`` the route returns the
    batch EventRow list. Optional ``?after_seq`` trims to ``seq > n``. Still a thin
    consumer — no streaming, no engine import.
    """
    params = {}
    if after_seq is not None:
        params["after_seq"] = after_seq
    try:
        response = requests.get(
            f"{API_BASE_URL}/workflows/runs/{run_id}/events",
            params=params,
            timeout=MCP_REQUEST_TIMEOUT,
        )
    except requests.exceptions.RequestException as e:
        raise click.ClickException(f"could not reach cao-server: {e}")

    if response.status_code == 404:
        # Same ambiguity as the follow path (CD-1) — the batch variant lives on the
        # SAME route, so it is equally absent until #504 lands.
        raise _events_route_or_run_missing(run_id)
    if response.status_code != 200:
        raise click.ClickException(_extract_detail(response, f"status {response.status_code}"))

    rows = response.json()
    if as_json:
        click.echo(_json.dumps(rows, indent=2))
        return
    if not rows:
        click.echo("no events")
        return
    for row in rows:
        step = row.get("step_id") or "(run)"
        click.echo(f"[seq {row.get('seq')}] {row.get('event_type')}: {step} -> {row.get('state')}")


@workflow.command(name="events")
@click.argument("run_id")
@click.option(
    "--follow/--no-follow",
    "follow",
    default=True,
    help="Stream live SSE progress (default). --no-follow does a one-shot batch read.",
)
@click.option(
    "--after-seq",
    "after_seq",
    type=int,
    default=None,
    help="Resume strictly after this per-run seq (exact, dedupe-free).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit each frame as JSON.")
def events_cmd(run_id, follow, after_seq, as_json):
    """Follow a run's live event stream, rendering per-run ordered progress.

    Consumes #504's events-follow SSE route (``Accept: text/event-stream``). Each
    normal frame renders as a progress line (or a JSON line under ``--json`` /
    non-TTY); a server-DECLARED ``event: gap`` frame is rendered verbatim (the gap
    is DATA the server sends — the follower never computes one from seq numbering).
    On a dropped connection the follow reconnects, resuming exactly via
    ``?after_seq=<last-seen seq>`` so no event is re-delivered. Ctrl-C DETACHES
    (prints the handle + a hint, exits 0, never cancels).

    Exit codes (identical on a TTY, a non-TTY, and under ``--json``):
      0  run reached COMPLETED — or Ctrl-C detached, or the stream ended without a
         terminal (a final status check confirms the run is not yet terminal)
      1  run reached FAILED / CANCELLED
    """
    if not follow:
        _events_batch_read(run_id, after_seq, as_json)
        return

    machine = _machine_mode(as_json)
    cursor = after_seq
    reconnects = 0
    terminal_state = None

    try:
        while True:
            saw_terminal = False
            try:
                for frame in _stream_event_frames(run_id, cursor):
                    if frame.is_gap:
                        _render_gap_frame(frame, machine)
                        # A gap frame carries no ``id:``: it never advances the
                        # cursor and is never terminal — the stream continues.
                        continue
                    _render_event_frame(frame, machine)
                    seq = frame.seq()
                    if seq is not None:
                        # Advance the cursor in THIS scope so a mid-stream drop
                        # reconnects exactly after the last seen seq (RS-1/RS-3).
                        cursor = seq
                    if frame.is_terminal:
                        terminal_state = frame.terminal_state
                        saw_terminal = True
                        break
            except requests.exceptions.RequestException:
                # A dropped connection is not run death — reconnect from the last
                # seen seq (exact resume), bounded so a flapping stream cannot spin
                # forever. Budget exhausted -> fall through to the final status read.
                reconnects += 1
                if reconnects > WORKFLOW_EVENTS_MAX_RECONNECTS:
                    break
                time.sleep(WORKFLOW_POLL_INTERVAL_SECONDS)
                continue
            # A clean drain (terminal frame or the server closing the stream)
            # resets the reconnect budget and ends the follow loop.
            reconnects = 0
            if saw_terminal:
                break
            # The stream ended with no terminal frame — stop and let the final
            # status check (below) settle the outcome.
            break
    except KeyboardInterrupt:
        # CC-1 (consistent with U5's follow): Ctrl-C DETACHES — print the handle +
        # hint to stderr (so it never corrupts stdout JSON) and exit 0. It MUST NOT
        # POST cancel; the run keeps running server-side.
        click.echo(
            f"\ndetached from '{run_id}'; run still running — "
            f"use `cao workflow status {run_id}` or `cao workflow cancel {run_id}`.",
            err=True,
        )
        return

    # F-1 terminal guard: the follow must not hang on a swallowed terminal EVENT.
    # If no terminal frame arrived (stream ended / reconnect budget spent), a final
    # snapshot read closes the follow on the true terminal state.
    if terminal_state is None:
        terminal_state = _final_events_status(run_id)

    if terminal_state is None:
        # Stream gone AND the run is not (yet) terminal — report the stream ended;
        # never a false failure (exit 0).
        if machine:
            click.echo(_json.dumps({"kind": "stream_ended", "run_id": run_id, "state": None}))
        else:
            click.echo(
                f"stream ended; run '{run_id}' is not yet terminal — "
                f"reconnect with `cao workflow events {run_id}` or "
                f"`cao workflow status {run_id}`.",
                err=True,
            )
        return

    if machine:
        click.echo(_json.dumps({"kind": "terminal", "run_id": run_id, "state": terminal_state}))
    else:
        click.echo(f"State: {terminal_state}")
    # EC-1: the exit code mirrors the terminal state on every output mode.
    _exit_for_state(terminal_state)
