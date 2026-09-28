"""THE COMPOSITION GUARD for the async workflow-run lifecycle (issue #505, integration-proof).

This is the integration-proof unit: it owns NO production surface. It composes the
finished U1-U9 surfaces (submit / list / status / result / cancel routes, the journal
DAL, the YAML engine drive, and the real script subprocess runner) and asserts the
whole lifecycle actually works end to end -- submit -> follow -> detach -> reconnect ->
cancel -> result -- once per engine tier. It exists specifically so #505 does not repeat
PR #516: 154 green tests + 35 green checks accompanied a feature that wired NONE of its
collaborators, because nothing tested composition. Every assertion here is load-bearing:
it MUST fail if any composed unit is wired-but-inert (a 202 with no durable row, a status
that cannot answer detached, an unresolvable cancel link, a result that depends on the
live connection).

TEST VEHICLE -- deliberate design decision, documented here per the honesty standard
====================================================================================
The functional-design plan (Step 1) called for a real ``cao-server`` subprocess via the
shipped ``cao_server`` session fixture. We deliberately do NOT use it here, for a reason
that must be disclosed rather than papered over:

- There is no in-process real-server fixture that CI can rely on. ``test/e2e``'s
  ``require_cao_server`` SKIPS the entire e2e session when no server is listening, and CI
  runs none -- so a subprocess-based guard would be a session-wide SKIP in CI, i.e. no
  guard at all. A guard that silently skips is exactly the #516 failure mode in a new
  costume.

- Instead we drive the REAL assembled ASGI app via Starlette's ``TestClient`` used as a
  CONTEXT MANAGER (a single persistent portal event loop that survives across requests,
  so the fire-and-forget background drive task is NOT cancelled between calls -- a bare
  per-request TestClient tears that task down at the first ``await`` and the run never
  advances). This exercises REAL route resolution (so a wired-but-inert route, or a
  ``GET /workflows/runs`` shadowed by the ``/workflows/{name}`` catch-all, BREAKS the
  test), the REAL SQLite journal (a real temp DB via monkeypatched ``DATABASE_FILE`` +
  the real migrators, exactly as the sibling api tests isolate it), the REAL YAML engine
  state machine and its journal write-throughs, and a REAL script subprocess spawned and
  reaped by the real ``script_runner``. The one leaf we stub is ``run_agent_step`` -- the
  tmux/provider substrate CI cannot supply -- with a controllable coroutine that honors
  ``cancel_event`` EXACTLY as the real substrate does; the engine drive, journal, and
  routes above it are entirely real.

DOCUMENTED LIMITATIONS (disclosed, never a passing stub):
- TestClient exercises the real app in-process but CANNOT prove true cross-OS-process
  durability across an actual server restart, nor the real ``cao`` CLI / ``cao-mcp-server``
  clients (which speak real ``requests`` to a listening socket the in-process transport
  does not expose). Those -- and the AA-3 CLI Ctrl-C-detaches-not-cancels behavior, which
  is a property of the CLI follower process -- remain owned by the real-``cao-server`` e2e
  suite and the per-unit CLI/MCP tests. This module proves server-side composition; it
  does not claim to prove the client-process surfaces or physical restart durability.
- U10 live-event following (``events --follow`` / SSE) is now UNBLOCKED (#504's U4 SSE
  surface landed; the frame contract is FINAL). #505 builds only the CLIENT follower and
  consumes the events route over HTTP (BR-9 / FR-7.4). #504's server route is not in this
  tree yet, so T12 below STUBS the streamed SSE response against the FINAL frame contract
  and asserts the follower renders ordered progress and renders a DECLARED gap as declared
  (render-not-infer). Real end-to-end wiring against the live route is validated at the
  post-#504-merge rebase. The exhaustive per-frame CLI/MCP follower tests live in
  ``test/cli/commands/test_workflow_events.py`` and ``test/mcp_server/test_workflow_events.py``.

Style: pytest + pytest-asyncio parity with ``test/api/``; black + isort (line 100).
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from test.api.conftest import TestClientWithHost
from typing import Callable, Optional
from unittest import mock

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import (
    ScriptSpec,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import StepOutputRecord
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import workflow_journal, workflow_service

# Poll budget for driving a run to a terminal state under the portal loop. Each GET
# pumps the loop so the background task advances between polls; the sleep yields the
# GIL to the portal thread. Comfortably bounds the fast in-process runs used here.
_POLL_ATTEMPTS = 400
_POLL_SLEEP = 0.02

_YAML_SPEC = WorkflowSpec(
    name="wf-int",
    steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
)

# A LONG-RUNNING script whose step sleeps well beyond harness/interrupt latency, so the
# run is still RUNNING when a detached cancel or a mid-flight read lands (AA-3/AA-5
# assumption). The sentinel line is what a completed script prints for its run output.
_SCRIPT_LONG = "import time\ntime.sleep(30)\nprint('CAO_WORKFLOW_OUTPUT: {\"done\": true}')\n"
# A fast script that completes on its own (bounded), for the happy-path composed flow.
_SCRIPT_FAST = "import time\ntime.sleep(0.2)\nprint('CAO_WORKFLOW_OUTPUT: {\"done\": true}')\n"


# ---------------------------------------------------------------------------
# Harness: a REAL temp SQLite journal + a persistent-portal TestClient.
# ---------------------------------------------------------------------------
@pytest.fixture
def journal_db(monkeypatch, tmp_path):
    """Point the journal at a REAL temp SQLite DB and run the real migrators.

    Mirrors the sibling api tests' isolation (monkeypatch ``DATABASE_FILE`` + the real
    ``_migrate_workflow_run*`` migrators). Clears the process-local registry / drive set
    on both sides so no run leaks between tests.
    """
    from cli_agent_orchestrator.clients.database import (
        _migrate_workflow_run,
        _migrate_workflow_run_step,
    )

    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()
    yield db_path
    workflow_service.run_registry.clear()
    workflow_service._active_drives.clear()


@pytest.fixture
def portal_client(journal_db):
    """A REAL-app TestClient used as a CONTEXT MANAGER (persistent portal loop).

    The context-manager form runs the app lifespan and, crucially, keeps ONE portal
    event loop alive across requests -- so the fire-and-forget background drive
    (``asyncio.create_task``) is not cancelled between calls and actually advances the
    run. (A bare ``TestClient(app)`` tears down its per-request loop, cancelling the
    drive at its first ``await`` -- the run would never leave RUNNING.)
    """
    app.state.plugin_registry = PluginRegistry()
    with TestClientWithHost(app) as client:
        yield client


def _point_spec(monkeypatch, spec) -> None:
    """Resolve every ``get_workflow`` lookup to ``spec`` (both submit arms use it)."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.workflow_spec_service.get_workflow",
        lambda name_or_path, scan_dir=None: spec,
    )


def _write_script_spec(monkeypatch, tmp_path, source: str, name: str) -> ScriptSpec:
    """Write a real ``.py`` to a temp path and point the resolver at its ScriptSpec."""
    path = tmp_path / f"{name}.py"
    path.write_text(source)
    spec = ScriptSpec(name=name, path=str(path), source=source, content_hash="h")
    _point_spec(monkeypatch, spec)
    return spec


def _instant_yaml_leaf() -> Callable:
    """A stubbed ``run_agent_step`` that records a validated output and completes at once.

    Stubs ONLY the tmux/provider leaf CI lacks; the engine drive, journal write-throughs,
    and routes above it stay real. Records a validated structured output so the real
    ``_collect_structured_output`` path settles the step COMPLETED.
    """

    async def _leaf(*args, **kwargs):
        run_id = kwargs["env_vars"]["CAO_WORKFLOW_RUN_ID"]
        step_id = kwargs["env_vars"]["CAO_WORKFLOW_STEP_ID"]
        workflow_service.step_output_store.put(
            run_id,
            step_id,
            StepOutputRecord(
                run_id=run_id,
                step_id=step_id,
                output={"ok": 1},
                validated=True,
                errors=[],
                state=StepState.COMPLETED,
            ),
        )
        return AgentStepResult(
            terminal_id="t-int", last_message="done", status=TerminalStatus.COMPLETED
        )

    return _leaf


def _long_yaml_leaf() -> Callable:
    """A stubbed ``run_agent_step`` that stays in-flight until cancel_event fires.

    Honors ``cancel_event`` EXACTLY as the real substrate does: it raises
    ``StepCancelledError`` the instant the engine sets the event, so the real drive
    converges the run to CANCELLED through its real code path (not a shortcut).
    """
    from cli_agent_orchestrator.services.agent_step import StepCancelledError

    async def _leaf(*args, **kwargs):
        cancel_event = kwargs["cancel_event"]
        for _ in range(5000):
            if cancel_event.is_set():
                raise StepCancelledError(terminal_id="t-int")
            await asyncio.sleep(_POLL_SLEEP)
        return AgentStepResult(
            terminal_id="t-int", last_message="done", status=TerminalStatus.COMPLETED
        )

    return _leaf


def _submit(client, name_or_path: str, run_id: str) -> dict:
    """POST the async submit route and return the 202 body (run_id + links)."""
    resp = client.post(
        "/workflows/runs:submit",
        json={"name_or_path": name_or_path, "inputs": {}, "run_id": run_id},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


def _poll_journal_terminal(run_id: str, client) -> Optional[str]:
    """Poll the run to a terminal state, pumping the portal loop via GET between polls."""
    final: Optional[str] = None
    for _ in range(_POLL_ATTEMPTS):
        client.get(f"/workflows/runs/{run_id}")  # pump the background drive
        row = workflow_journal.get_run(run_id)
        final = row.state if row is not None else None
        if final in ("completed", "failed", "cancelled"):
            break
        time.sleep(_POLL_SLEEP)
    return final


def _wait_running(run_id: str, client) -> None:
    """Wait until the run is observably RUNNING (so a subsequent cancel is not a 409)."""
    for _ in range(_POLL_ATTEMPTS):
        if client.get(f"/workflows/runs/{run_id}").json().get("state") == "running":
            return
        time.sleep(_POLL_SLEEP)


# ===========================================================================
# T1 (AA-1, IP-1): submit -> durable-before-ack -> drive -> result (YAML tier).
# The whole happy-path composed flow, once per tier. AA-1's write invariant is
# proven end-to-end: the run is readable the INSTANT the 202 returns.
# ===========================================================================
def test_composed_flow_yaml_tier(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _instant_yaml_leaf())

    body = _submit(portal_client, "wf-int", "int-yaml")
    # AA-1 / IP-1: durable-before-ack -- the id is readable the instant the 202 returns,
    # proving the awaited insert preceded the ack (not after the background drive).
    row = workflow_journal.get_run("int-yaml")
    assert row is not None, "durable-before-ack BROKEN: 202 returned but no journal row"
    assert row.tier == "yaml"
    status = portal_client.get("/workflows/runs/int-yaml")
    assert status.status_code == 200
    # links are the composition contract the later steps consume.
    assert body["links"]["cancel"] == "/workflows/runs/int-yaml/cancel"
    assert body["links"]["result"] == "/workflows/runs/int-yaml/result"

    # Drive to terminal via the REAL engine, then retrieve the full result (AA-6 read).
    final = _poll_journal_terminal("int-yaml", portal_client)
    assert final == "completed", final
    result = portal_client.get("/workflows/runs/int-yaml/result")
    assert result.status_code == 200
    body_r = result.json()
    assert body_r["state"] == "completed"
    assert body_r["run_id"] == "int-yaml"
    assert [s["id"] for s in body_r["steps"]] == ["s1"]


# ===========================================================================
# T1 (AA-1, IP-1) SCRIPT TIER: the same composed happy path against a REAL
# subprocess spawned + reaped by the real script_runner (CG-3 two-tier parity).
# ===========================================================================
def test_composed_flow_script_tier(portal_client, monkeypatch, tmp_path):
    _write_script_spec(monkeypatch, tmp_path, _SCRIPT_FAST, "scr-fast")

    body = _submit(portal_client, "scr-fast", "int-scr")
    # AA-1 / IP-1: durable-before-ack, and journaled as tier=script.
    row = workflow_journal.get_run("int-scr")
    assert row is not None, "durable-before-ack BROKEN: 202 returned but no journal row"
    assert row.tier == "script"
    assert portal_client.get("/workflows/runs/int-scr").status_code == 200
    assert body["links"]["cancel"] == "/workflows/runs/int-scr/cancel"

    final = _poll_journal_terminal("int-scr", portal_client)
    assert final == "completed", final
    # AA-6: the retained result assembles from durable state on both tiers.
    result = portal_client.get("/workflows/runs/int-scr/result")
    assert result.status_code == 200
    assert result.json()["state"] == "completed"
    assert result.json()["run_id"] == "int-scr"


# ===========================================================================
# T4/T6 (AA-4, AA-6, IP-2): journal-authoritative, DETACHED. After clearing the
# registry (submitter-gone / restart-ish), a COMPLETED run is STILL answerable via
# GET status and retrievable via GET result -- from the journal alone.
# ===========================================================================
def test_detached_run_answerable_from_journal_yaml(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _instant_yaml_leaf())

    _submit(portal_client, "wf-int", "int-detach")
    assert _poll_journal_terminal("int-detach", portal_client) == "completed"

    # DETACH: drop the in-memory registry entry entirely (simulate the submitter
    # process being gone). Everything below must answer from the journal alone.
    workflow_service.run_registry.clear()

    status = portal_client.get("/workflows/runs/int-detach")
    assert status.status_code == 200, "AA-4 BROKEN: status cannot answer a detached run"
    assert status.json()["state"] == "completed"

    result = portal_client.get("/workflows/runs/int-detach/result")
    assert result.status_code == 200, "AA-6 BROKEN: result depends on the live registry"
    assert result.json()["state"] == "completed"
    assert [s["id"] for s in result.json()["steps"]] == ["s1"]


def test_detached_run_answerable_from_journal_script(portal_client, monkeypatch, tmp_path):
    _write_script_spec(monkeypatch, tmp_path, _SCRIPT_FAST, "scr-fast")

    _submit(portal_client, "scr-fast", "int-scr-detach")
    assert _poll_journal_terminal("int-scr-detach", portal_client) == "completed"

    workflow_service.run_registry.clear()  # detach

    status = portal_client.get("/workflows/runs/int-scr-detach")
    assert status.status_code == 200, "AA-4 BROKEN: script status cannot answer detached"
    result = portal_client.get("/workflows/runs/int-scr-detach/result")
    assert result.status_code == 200, "AA-6 BROKEN: script result depends on live registry"
    assert result.json()["state"] == "completed"


# ===========================================================================
# T5 (AA-3/AA-5, IP-2): cancel composes. A submitted-and-RUNNING run can be
# cancelled via links.cancel and reaches CANCELLED, answerable afterward. Run once
# per tier. The long-runner keeps the run RUNNING at cancel time (AA-3/AA-5
# assumption) so we never flake on a 409-because-already-completed.
# ===========================================================================
def test_cancel_composes_live_yaml(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _long_yaml_leaf())

    body = _submit(portal_client, "wf-int", "int-cancel")
    _wait_running("int-cancel", portal_client)

    # Cancel via the EXACT relative link the 202 handed back (ADR-1 round-trip).
    cancelled = portal_client.post(body["links"]["cancel"])
    assert cancelled.status_code == 200, cancelled.text

    final = _poll_journal_terminal("int-cancel", portal_client)
    assert final == "cancelled", final
    # answerable afterward (from the journal).
    assert portal_client.get("/workflows/runs/int-cancel").json()["state"] == "cancelled"


def test_cancel_composes_live_script(portal_client, monkeypatch, tmp_path):
    _write_script_spec(monkeypatch, tmp_path, _SCRIPT_LONG, "scr-long")

    body = _submit(portal_client, "scr-long", "int-scr-cancel")
    _wait_running("int-scr-cancel", portal_client)

    cancelled = portal_client.post(body["links"]["cancel"])
    assert cancelled.status_code == 200, cancelled.text

    final = _poll_journal_terminal("int-scr-cancel", portal_client)
    assert final == "cancelled", final
    assert portal_client.get("/workflows/runs/int-scr-cancel").json()["state"] == "cancelled"


# ===========================================================================
# T5 (AA-5, IP-2): cancel works DETACHED. After the registry is cleared, the run
# is cancelled from the journal alone via links.cancel -- no live connection, no
# registry entry. Cancel is issued FIRST (before any rebuilding GET) so the
# journal-fallback arm handles it, exactly as a real restarted server would.
# ===========================================================================
def test_detached_cancel_via_link_yaml(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _long_yaml_leaf())

    body = _submit(portal_client, "wf-int", "int-dcancel")
    _wait_running("int-dcancel", portal_client)

    workflow_service.run_registry.clear()  # submitter gone / restart-ish

    cancelled = portal_client.post(body["links"]["cancel"])
    assert cancelled.status_code == 200, "AA-5 BROKEN: cancel link unresolvable when detached"

    status = portal_client.get("/workflows/runs/int-dcancel")
    assert status.json()["state"] == "cancelled"
    result = portal_client.get("/workflows/runs/int-dcancel/result")
    assert result.status_code == 200
    assert result.json()["state"] == "cancelled"


# ===========================================================================
# T7 (CG-1): fail-on-wired-but-inert -- THE #516 GUARD. We monkeypatch the submit
# path's durable insert to a no-op (a run that is 202'd-but-not-journaled), and
# assert the composed durable-before-ack invariant BREAKS -- i.e. the guard DETECTS
# the inert wiring. This proves per-unit green cannot pass while composition is
# broken: the very assertion T1 relies on flips to failing under inert wiring.
# ===========================================================================
def test_cg1_inert_insert_is_detected(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _instant_yaml_leaf())

    # Inert collaborator: the atomic durable insert is wired but does NOTHING.
    def _inert_insert(*args, **kwargs):
        return None

    monkeypatch.setattr(workflow_journal, "insert_run_with_steps", _inert_insert)

    resp = portal_client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf-int", "inputs": {}, "run_id": "int-inert"},
    )
    # The route still 202s (it does not know its collaborator was gutted)...
    assert resp.status_code == 202

    # ...but the durable-before-ack invariant that T1 asserts is now FALSE. This is
    # the guard biting: the exact ``get_run is not None`` assertion T1 depends on fails.
    assert (
        workflow_journal.get_run("int-inert") is None
    ), "guard precondition: the inert insert must leave no row"
    with pytest.raises(AssertionError):
        row = workflow_journal.get_run("int-inert")
        assert row is not None, "durable-before-ack BROKEN: 202 returned but no journal row"

    # And the composed HTTP read surface exposes the inert wiring once the masking
    # in-memory cache is cleared: a detached status read 404s (the run was never durable).
    workflow_service.run_registry.clear()
    assert portal_client.get("/workflows/runs/int-inert").status_code == 404


def test_cg1_sanity_durable_before_ack_fails_for_absent_id(portal_client):
    """LOAD-BEARING sanity: the durable-before-ack predicate is not vacuously true.

    Pointed at an id that was never submitted, the very assertion T1 relies on MUST
    fail -- confirming the check bites on a broken composition rather than always
    passing (the mutation check the plan mandates for the key assertions)."""
    with pytest.raises(AssertionError):
        row = workflow_journal.get_run("never-submitted")
        assert row is not None, "durable-before-ack BROKEN: 202 returned but no journal row"


# ===========================================================================
# T10 (TR-1): crash-between-inserts at the COMPOSED (HTTP) layer -- MANDATED real
# crash, not a mock return. A real sqlite3.Error is injected into the step-seed
# INSERT *inside* the same transaction as the run-row INSERT, so the whole
# transaction rolls back. Asserted through the HTTP surface: the submit returns
# 5xx AND no phantom RUNNING row is visible via GET list or GET status.
# ===========================================================================
def test_tr1_crash_between_inserts_no_phantom_row(portal_client, monkeypatch):
    _point_spec(monkeypatch, _YAML_SPEC)
    monkeypatch.setattr(workflow_service, "run_agent_step", _instant_yaml_leaf())

    real_connect = workflow_journal._connect

    def _crashing_insert(
        run_id,
        workflow_name,
        spec_snapshot,
        inputs_json,
        state,
        started_at,
        steps,
        updated_at,
        tier="yaml",
        generation="1",
    ):
        # Do the REAL run-row INSERT, then raise a REAL sqlite3.Error before the step
        # seed -- inside ONE ``with conn`` transaction, so sqlite rolls BOTH back. This
        # is the genuine crash-between-inserts, not a return-value mock.
        with real_connect() as conn:
            conn.execute(
                "INSERT INTO workflow_run "
                "(run_id, workflow_name, spec_snapshot, inputs_json, state, "
                " current_step_id, started_at, finished_at, tier, generation) "
                "VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, ?, ?)",
                (
                    run_id,
                    workflow_name,
                    spec_snapshot,
                    inputs_json,
                    state,
                    started_at,
                    tier,
                    generation,
                ),
            )
            raise sqlite3.OperationalError("disk I/O error during step seed")

    monkeypatch.setattr(workflow_journal, "insert_run_with_steps", _crashing_insert)

    resp = portal_client.post(
        "/workflows/runs:submit",
        json={"name_or_path": "wf-int", "inputs": {}, "run_id": "int-crash"},
    )
    assert resp.status_code == 500
    assert resp.status_code != 202

    # No-orphan-row invariant, proven through the HTTP surface (not just the DAL unit):
    # journal, GET list, and GET status all show NO phantom RUNNING row.
    assert workflow_journal.get_run("int-crash") is None
    listed = portal_client.get("/workflows/runs")
    assert listed.status_code == 200
    assert not any(r["run_id"] == "int-crash" for r in listed.json())
    assert portal_client.get("/workflows/runs/int-crash").status_code == 404
    # And no orphaned in-memory record was registered either.
    assert "int-crash" not in workflow_service.run_registry


# ===========================================================================
# T12 (U10): live event following, now UNBLOCKED (#504's U4 SSE surface landed;
# the frame contract is FINAL). #505 builds only the CLIENT follower and consumes
# the events route over HTTP (BR-9 / FR-7.4). The server route is NOT in this tree
# yet (HEAD is the base commit), so we STUB the streamed SSE response against the
# FINAL frame contract and assert the client follower renders ordered progress AND
# renders a DECLARED gap as declared (render-not-infer). Real end-to-end wiring
# against #504's live route is validated at the post-#504-merge rebase.
#
# Marker: ``integration`` (NOT e2e) so CI's ``-m 'not e2e'`` still exercises this
# guard — an e2e marker would make it silently absent. The exhaustive per-frame
# CLI/MCP follower tests live in test/cli/commands/test_workflow_events.py and
# test/mcp_server/test_workflow_events.py; this composition-guard proves the CLI
# follower is wired to the events route with the render-not-infer invariant intact.
# ===========================================================================
def _sse_stream_resp(*frames: str):
    """A mock streamed SSE response whose ``iter_lines`` replays the frame lines.

    Each ``frame`` is a full SSE block (its ``event:``/``data:``/``id:`` lines plus
    the terminating blank line), authored exactly as #504's FINAL contract puts
    them on the wire. ``iter_lines(decode_unicode=True)`` yields each line without
    the trailing newline, keeping the empty strings that terminate a frame.
    """
    import requests as _requests

    lines: list[str] = []
    for frame in frames:
        lines.extend(frame.split("\n"))
    resp = mock.MagicMock(spec=_requests.Response)
    resp.status_code = 200
    resp.iter_lines.return_value = iter(lines)
    resp.close.return_value = None
    return resp


def _u10_event_frame(seq: int, event_type: str, step_id, state: str) -> str:
    body = {
        "seq": seq,
        "run_id": "run1",
        "event_type": event_type,
        "step_id": step_id,
        "state": state,
        "ts": "2026-07-28T00:00:00Z",
    }
    return f"event: {event_type}\ndata: {json.dumps(body)}\nid: {seq}\n"


def _u10_gap_frame(after_seq: int, before_seq: int, missing_count: int) -> str:
    body = {
        "after_seq": after_seq,
        "before_seq": before_seq,
        "missing_count": missing_count,
        "reason": "append_failed",
    }
    return f"event: gap\ndata: {json.dumps(body)}\n"


@pytest.mark.integration
def test_u10_live_event_following_renders_ordered_progress_and_declared_gap():
    """UNBLOCKED: the CLI follower consumes ``GET /workflows/runs/{run_id}/events``
    (SSE variant), renders per-run progress in seq order, and renders a
    SERVER-DECLARED ``event: gap`` frame AS DECLARED — closing on the terminal
    run.completed frame with exit 0."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.workflow import workflow

    stream = _sse_stream_resp(
        _u10_event_frame(19, "step.completed", "s1", "completed"),
        _u10_gap_frame(after_seq=19, before_seq=23, missing_count=3),
        _u10_event_frame(23, "run.completed", None, "completed"),
    )
    # CliRunner is non-TTY, so force the human render (``_machine_mode`` -> False)
    # to assert the interactive progress + gap lines.
    with (
        mock.patch(
            "cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream
        ),
        mock.patch(
            "cli_agent_orchestrator.cli.commands.workflow._machine_mode", return_value=False
        ),
    ):
        result = CliRunner().invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    out = result.output
    # Ordered progress render (seq 19 before seq 23).
    assert out.index("seq 19") < out.index("seq 23")
    # The DECLARED gap is rendered as declared (the frame's numbers + reason).
    assert "gap" in out
    assert "3 event(s) lost" in out
    assert "append_failed" in out


@pytest.mark.integration
def test_u10_follower_does_not_infer_gap_from_seq_numbering():
    """Render-not-infer (GD-1, load-bearing): the SAME seq jump 19 -> 23 with NO
    declared gap frame must NOT be reported as a gap. This FAILS if the follower
    infers gaps from numbering instead of consuming the ``event: gap`` frame."""
    from click.testing import CliRunner

    from cli_agent_orchestrator.cli.commands.workflow import workflow

    stream = _sse_stream_resp(
        _u10_event_frame(19, "step.completed", "s1", "completed"),
        # seq jumps to 23 with NO gap frame declared between them.
        _u10_event_frame(23, "run.completed", None, "completed"),
    )
    with mock.patch(
        "cli_agent_orchestrator.cli.commands.workflow.requests.get", return_value=stream
    ):
        result = CliRunner().invoke(workflow, ["events", "run1"])
    assert result.exit_code == 0
    assert "gap" not in result.output
    assert "lost" not in result.output
