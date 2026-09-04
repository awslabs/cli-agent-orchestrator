"""U9 — Composed-Path Integration Harness (issue #504, THE load-bearing guard).

This is a spec/test unit: it ships TESTS, no product code. Its purpose is the
PR #516 / #321 / #511 lesson — per-unit-green is NOT evidence the journal works.
U9 drives a REAL workflow through the actual ``start_run`` -> ``_drive`` loop (U1
substrate + U2 emission), reads it back through the REAL U4 events route
(``GET /workflows/runs/{run_id}/events``), and simulates a process restart. Only
the worker boundary (``run_agent_step`` / ``step_output_store``) is mocked — the
journal (seq allocation, ``persist_high_water``, ``append_event``, the drive-loop
``_journal_event`` sites, ``_rebuild_record_from_journal``) is the REAL code under
test. Mocking the journal would defeat the entire purpose.

Harness constructs (domain-entities.md):
  - ``RealDriveRunFixture`` — spins a real run through ``start_run`` producing
    genuine ``workflow_run_event`` rows via U2's ``_journal_event``.
  - ``SwallowInjector`` — forces a chosen DAL write (``append_event`` OR
    ``persist_high_water``) to raise for a specific emission, so the drive loop's
    best-effort swallow path is genuinely traversed (BR-2 / BR-3).
  - ``RestartSimulator`` — clears ``run_registry`` + ``_active_drives`` so the
    next read rebuilds via ``_rebuild_record_from_journal`` (the ADR-2 recovery
    path where the defect lived).
  - ``MigratorSpy`` — counts ``_migrate_workflow_run_event`` calls (BR-6).

BR-7 (guard-integrity) — each assertion below carries its DEFECT ORACLE; a test
that stays green under its defect is decorative and must be strengthened. The
oracles (self-verified by cp-aside mutation for the two headline cases):

  - BR-1  composed path         : stub ``_journal_event`` to a no-op -> RED (no
                                  events land, the route returns an empty page).
  - BR-2  swallowed append/gap  : renumber the sequence on rebuild (reuse the
                                  hole) -> RED (the gap disappears / seq reused).
  - BR-3  high-water floor      : mutate ``workflow_service.py:1257`` to the
                                  single-term ``record.event_seq =
                                  workflow_journal.max_event_seq(run_id)`` -> RED.
                                  (THE deciding test — the supervisor's finding.)
  - BR-3b reverse-fault floor   : mutate the re-seed to the OTHER single term,
                                  ``= persisted_high_water(run_id)`` -> RED.
  - BR-4  terminal-state guard  : drop the F-1 ``get_run`` terminal check in
                                  ``_follow_run_events`` -> the follower HANGS
                                  (caught here by a hard timeout, never a hang).
  - BR-6  no per-append migrate : migrate on every append -> RED (spy count > 1).

BR-3 fault-direction correction (verified empirically, NOT a paraphrase):
  The supervisor mutated the two-term re-seed ``max(persisted_high_water,
  max_event_seq)`` to the single term ``max_event_seq`` alone. That single term
  is byte-identical to the two-term value UNLESS ``persisted_high_water >
  max_event_seq`` — i.e. a seq was ALLOCATED, its high-water PERSISTED, but its
  ``append_event`` did NOT land (the append was swallowed). That is the
  "last-allocation-before-crash" forward fault. The reverse fault (high-water
  swallowed while the append succeeds) leaves ``max_event_seq >=
  persisted_high_water``, so ``max_event_seq`` alone STILL recovers the true floor
  and does NOT distinguish the mutation. BR-3 therefore drives the FORWARD fault
  (append of the terminal emission swallowed, its high-water persisted) — the
  only fault that goes RED under ``= max_event_seq`` — as HARD CONSTRAINT #3
  requires. business-rules.md BR-3's prose describes the reverse fault and names
  ``max_event_seq`` as the protecting floor; that guards the OTHER term and is
  covered by the supplementary BR-3b. Both terms of ``max(...)`` thus have a
  distinct mutation oracle (BR-7).

The journal points at a temp SQLite DB via the patched ``DATABASE_FILE``; the
event-migration memo is reset per test. Every live-follow / SSE assertion is
bounded by a hard timeout so a regression fails loudly and NEVER hangs the suite.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from pathlib import Path
from typing import Callable, Dict, List, Tuple
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.clients.database as dbmod
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.clients.database import (
    _migrate_workflow_run,
    _migrate_workflow_run_step,
)
from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus
from cli_agent_orchestrator.models.workflow import RunState, WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services import workflow_service as ws

# The FR-1.4 taxonomy a clean composed run must emit (business-rules U2 / U9 BR-1).
_TAXONOMY_MIN = frozenset(
    {
        "run.started",
        "step.started",
        "step.attempt.started",
        "terminal.created",
        "step.completed",
        "run.completed",
    }
)

# A generous hard cap for the SSE F-1 guard. The whole point of BR-4 is that a
# terminal-run stream CLOSES; if a regression re-broke it the request would hang
# forever and wedge CI — the timeout turns that into a loud failure instead.
_STREAM_TIMEOUT_S = 15.0


# ---------------------------------------------------------------------------
# Fixtures — a fresh temp journal DB + clean registry/migration memo per test.
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def _patched_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Point the journal at a temp DB, create the tables, clean the registry.

    Mirrors ``test_workflow_event_emission.py`` exactly; the event-migration memo
    is cleared so the fresh temp DB self-migrates on first event write (BR-6).
    """
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    _migrate_workflow_run()
    _migrate_workflow_run_step()
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()
    yield db_path
    workflow_journal._event_migrated_paths.clear()
    ws.run_registry.clear()
    ws._active_drives.clear()
    ws.step_output_store._store.clear()


class _TestClientWithHost(TestClient):
    """TestClient that always sends a valid Host header for TrustedHostMiddleware."""

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        if not any(k.lower() == "host" for k in headers):
            headers["Host"] = "localhost"
        kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def client() -> TestClient:
    """The REAL U4 route surface over the temp journal (same handler as U3/U4)."""
    app.state.plugin_registry = PluginRegistry()
    return _TestClientWithHost(app)


# ---------------------------------------------------------------------------
# Harness constructs (domain-entities.md) — the composition under test.
# ---------------------------------------------------------------------------
def _ok(terminal_id: str = "t1") -> AgentStepResult:
    return AgentStepResult(
        terminal_id=terminal_id, last_message="done", status=TerminalStatus.COMPLETED
    )


def _spec(*, step_ids: Tuple[str, ...] = ("s1",), name: str = "wf") -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        mode="sequential",
        steps=[
            WorkflowStep(id=sid, provider="kiro_cli", agent="dev", prompt="go") for sid in step_ids
        ],
    )


class RealDriveRunFixture:
    """Spins a real run through ``start_run``; mocks ONLY the worker boundary.

    The journal, seq allocation, ``persist_high_water``, ``append_event``, the
    drive-loop ``_journal_event`` sites and ``_rebuild_record_from_journal`` are
    all the REAL code under test — nothing below the worker is mocked.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch
        self.monkeypatch.setattr(ws, "run_agent_step", AsyncMock(return_value=_ok()))

    def drive(self, run_id: str, *, step_ids: Tuple[str, ...] = ("s1",)) -> RunState:
        """Drive a real spec to completion through the real ``start_run`` loop."""
        result = asyncio.run(ws.start_run(_spec(step_ids=step_ids), {}, run_id))
        return result.state


class SwallowInjector:
    """Force a chosen DAL write to raise for a specific emission (BR-2 / BR-3).

    Injects at the DAL boundary (``workflow_journal.append_event`` /
    ``workflow_journal.persist_high_water``) so the drive loop's REAL best-effort
    swallow path (the ``_journal_event`` try/except) is genuinely traversed.
    """

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.monkeypatch = monkeypatch

    def fail_append_when(self, predicate: Callable[[int, str], bool]) -> None:
        """Swallow ``append_event`` for the first emission matching ``(seq, type)``."""
        real_append = workflow_journal.append_event

        def _wrapped(run_id: str, seq: int, event_type: str, **kwargs: object) -> None:
            if predicate(seq, event_type):
                raise sqlite3.OperationalError("U9 SwallowInjector: append_event swallowed")
            real_append(run_id, seq, event_type, **kwargs)  # type: ignore[arg-type]

        self.monkeypatch.setattr(ws.workflow_journal, "append_event", _wrapped)

    def fail_high_water_for_event_type(self, event_type: str) -> None:
        """Swallow ``persist_high_water`` for ``event_type`` while its append LANDS.

        Wraps the REAL ``_journal_event`` and swaps ``persist_high_water`` for a
        raising stub only for the duration of the matching emission — so the
        emission's ``append_event`` still succeeds (the reverse fault, BR-3b). The
        swallow still happens inside the real best-effort ``_journal_event``.
        """
        real_journal_event = ws._journal_event
        real_high_water = workflow_journal.persist_high_water

        async def _wrapped(record: object, etype: str, **kwargs: object) -> None:
            if etype != event_type:
                await real_journal_event(record, etype, **kwargs)  # type: ignore[arg-type]
                return

            def _boom(run_id: str, seq: int) -> None:
                raise sqlite3.OperationalError("U9 SwallowInjector: persist_high_water swallowed")

            ws.workflow_journal.persist_high_water = _boom  # type: ignore[assignment]
            try:
                await real_journal_event(record, etype, **kwargs)  # type: ignore[arg-type]
            finally:
                ws.workflow_journal.persist_high_water = real_high_water  # type: ignore[assignment]

        self.monkeypatch.setattr(ws, "_journal_event", _wrapped)


class RestartSimulator:
    """Drop the live cache so the next read rebuilds via the journal (ADR-2 path)."""

    @staticmethod
    def restart() -> None:
        ws.run_registry.clear()
        ws._active_drives.clear()


class MigratorSpy:
    """Count ``_migrate_workflow_run_event`` invocations (BR-6, call-count NEVER timing)."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real_migrate = dbmod._migrate_workflow_run_event

        def _counting() -> None:
            self.calls += 1
            real_migrate()

        monkeypatch.setattr(dbmod, "_migrate_workflow_run_event", _counting)


# ---------------------------------------------------------------------------
# Read-back helpers — through the REAL U4 route (U3/U4's same handler).
# ---------------------------------------------------------------------------
def _read_events_via_route(client: TestClient, run_id: str) -> Tuple[List[Dict], List[Dict]]:
    """GET the batch ``EventTimelinePage`` (Accept: application/json). Returns (events, gaps)."""
    resp = client.get(f"/workflows/runs/{run_id}/events", headers={"Accept": "application/json"})
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    return body["events"], body["gaps"]


def _emit_after_restart(run_id: str, event_type: str, *, step_id: str = "probe") -> int:
    """Rebuild the record from the journal and emit ONE fresh event through U2.

    Exercises the composed resume-emission path: ``get_run_status`` rebuilds via
    ``_rebuild_record_from_journal`` (re-seeding ``record.event_seq``), then the
    REAL ``_journal_event`` allocates the next seq. Returns the max durable seq
    after the emission.
    """

    async def _scenario() -> int:
        ws.get_run_status(run_id)  # rebuild + re-seed the counter
        record = ws.run_registry[run_id]
        await ws._journal_event(record, event_type, step_id=step_id, state="running")
        return int(workflow_journal.max_event_seq(run_id))

    return asyncio.run(_scenario())


def _stream_with_timeout(client: TestClient, url: str):
    """GET an SSE stream on a daemon thread under a hard timeout (BR-4, never hangs).

    A daemon thread (not a joined pool) is deliberate: if a regression reopened the
    F-1 hang the request would never return, and a joined thread would itself block
    forever on teardown. This raises ``TimeoutError`` and abandons the daemon
    worker, so the hang surfaces as a loud failure instead of wedging the suite.
    """
    result: Dict[str, object] = {}

    def _run() -> None:
        try:
            result["resp"] = client.get(url, headers={"Accept": "text/event-stream"})
        except Exception as exc:  # pragma: no cover - surfaced via result below
            result["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=_STREAM_TIMEOUT_S)
    if worker.is_alive():
        raise TimeoutError(
            f"SSE stream did not close within {_STREAM_TIMEOUT_S}s for {url} "
            "(F-1 terminal-state guard regression, BR-4/BR-5)"
        )
    if "exc" in result:
        raise result["exc"]  # type: ignore[misc]
    return result["resp"]


def _parse_sse_event_types(text: str) -> List[str]:
    """Return the ``event:`` type of each SSE frame in order."""
    types: List[str] = []
    for block in (b for b in text.split("\n\n") if b.strip()):
        for line in block.split("\n"):
            if line.startswith("event: "):
                types.append(line[len("event: ") :])
    return types


# ===========================================================================
# BR-1 — composed path, not mocks. A real run read back through the U4 route.
# ORACLE: stub ``_journal_event`` to a no-op -> RED (no events land).
# ===========================================================================
def test_br1_composed_real_run_reads_back_through_u4_route(client, monkeypatch):
    """A REAL 2-step run drives through ``start_run``; its events read back through
    the REAL U4 route (``GET /workflows/runs/{run_id}/events``, batch JSON) in seq
    order with the FR-1.4 taxonomy present — proving the substrate is WIRED into
    the drive loop, not just individually green (BR-1, the inert-collaborator bar).
    """
    fixture = RealDriveRunFixture(monkeypatch)
    assert fixture.drive("R1", step_ids=("s1", "s2")) == RunState.COMPLETED

    events, gaps = _read_events_via_route(client, "R1")

    # Composed read-back: contiguous seq order, no holes on a clean run.
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs == list(range(1, len(seqs) + 1))
    assert gaps == []

    # FR-1.4 taxonomy present, run.* brackets the timeline.
    types = [e["event_type"] for e in events]
    assert _TAXONOMY_MIN.issubset(set(types))
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    # Two steps each went started -> attempt.started -> terminal.created -> completed.
    assert types.count("step.started") == 2
    assert types.count("step.completed") == 2
    # Every event carries its run_id (the composed rows really came from R1).
    assert all(e["run_id"] == "R1" for e in events)


# ===========================================================================
# BR-2 — swallowed append -> declared gap, no seq reuse across restart.
# ORACLE: renumber the sequence on rebuild (reuse the hole) -> RED.
# ===========================================================================
def test_br2_swallowed_append_declares_gap_and_no_reuse_across_restart(client, monkeypatch):
    """One mid-run ``append_event`` is swallowed; the run still COMPLETES; after a
    restart the U4 route DECLARES the gap with the correct range AND a subsequent
    emission takes a strictly-higher seq — the swallowed slot is never reused
    (event-append-must-not-break-a-run, across the recovery path).
    """
    fixture = RealDriveRunFixture(monkeypatch)
    injector = SwallowInjector(monkeypatch)
    # Swallow the append of seq 3 (s1.attempt.started for a 2-step run). Its
    # high-water persisted first (persist runs before append), so seq 3 becomes a
    # DECLARED hole, not a renumber.
    injector.fail_append_when(lambda seq, event_type: seq == 3)

    assert fixture.drive("R2", step_ids=("s1", "s2")) == RunState.COMPLETED

    pre_high_water = workflow_journal.persisted_high_water("R2")

    # Read back through the route: the gap is declared, seq 3 absent (not renumbered).
    events, gaps = _read_events_via_route(client, "R2")
    seqs = [e["seq"] for e in events]
    assert 3 not in seqs
    assert len(gaps) == 1
    gap = gaps[0]
    assert (gap["after_seq"], gap["before_seq"], gap["missing_count"]) == (2, 4, 1)
    assert gap["reason"] == "append_failed"

    # Restart, then a fresh emission must NOT reuse seq 3 — it takes a seq strictly
    # above the recovered high-water (the composed re-seed path).
    RestartSimulator.restart()
    assert "R2" not in ws.run_registry  # genuinely rebuilt from the journal
    new_seq = _emit_after_restart("R2", "step.started")
    assert new_seq > pre_high_water

    events_after, gaps_after = _read_events_via_route(client, "R2")
    seqs_after = [e["seq"] for e in events_after]
    assert 3 not in seqs_after  # the hole is preserved, never back-filled
    assert new_seq in seqs_after
    assert any((g["after_seq"], g["before_seq"]) == (2, 4) for g in gaps_after)


# ===========================================================================
# BR-3 — THE DECIDING TEST (the supervisor's finding).
# High-water PERSISTED + append SWALLOWED for the last allocation -> after a
# restart the durable events survive and a subsequent emission gets a strictly-
# higher seq. MUST go RED under the single-term ``= max_event_seq(run_id)``.
# ===========================================================================
def test_br3_forward_fault_high_water_floor_no_seq_reuse(client, monkeypatch):
    """Forward fault (last-allocation-before-crash): the terminal ``run.completed``
    emission allocates a seq and PERSISTS its high-water, but its ``append_event``
    is swallowed — so ``persisted_high_water > max_event_seq``. After a restart the
    re-seed MUST use ``max(persisted_high_water, max_event_seq)`` so the swallowed
    slot is NOT reused: a fresh emission takes a seq strictly above the persisted
    high-water and the swallowed slot stays a declared hole.

    ORACLE (HARD CONSTRAINT #3, the supervisor's exact mutation): change
    ``workflow_service.py:1257`` to ``record.event_seq =
    workflow_journal.max_event_seq(run_id)`` (single-term). Then the re-seed floor
    drops to ``max_event_seq`` (which lags by one because the terminal append never
    landed), the fresh emission REUSES the swallowed slot, and ``new_seq >
    pre_high_water`` is FALSE -> RED. Under the shipped two-term re-seed it PASSES.
    """
    fixture = RealDriveRunFixture(monkeypatch)
    injector = SwallowInjector(monkeypatch)
    # Swallow ONLY the terminal run.completed append; its high-water persists first.
    injector.fail_append_when(lambda seq, event_type: event_type == "run.completed")

    assert fixture.drive("R3", step_ids=("s1",)) == RunState.COMPLETED

    pre_high_water = workflow_journal.persisted_high_water("R3")
    pre_max_event = workflow_journal.max_event_seq("R3")
    # The forward fault is real: the allocated high-water is strictly ABOVE the last
    # durable event (the terminal append was swallowed). This gap between the two
    # terms is precisely what the single-term mutation loses.
    assert pre_high_water > pre_max_event

    # Restart -> rebuild re-seeds record.event_seq; a fresh emission follows.
    RestartSimulator.restart()
    assert "R3" not in ws.run_registry
    new_seq = _emit_after_restart("R3", "step.started")

    # THE biting assertion: the durable event's slot is never clobbered — the next
    # emission is strictly ABOVE the persisted high-water (no reuse). Single-term
    # would reseed to max_event_seq (== pre_high_water - 1), reuse pre_high_water,
    # and fail this. Assert both the strict form and the exact expected slot.
    assert new_seq > pre_high_water
    assert new_seq == pre_high_water + 1

    # Read back through the route: the swallowed slot stays a declared hole (a
    # subsequent emission did NOT silently back-fill it).
    events, gaps = _read_events_via_route(client, "R3")
    seqs = [e["seq"] for e in events]
    assert pre_high_water not in seqs  # the swallowed terminal slot never reused
    assert new_seq in seqs
    assert any(g["after_seq"] < pre_high_water < g["before_seq"] for g in gaps)


def test_br3b_reverse_fault_max_event_seq_floor_no_reuse(client, monkeypatch):
    """Reverse fault (business-rules.md BR-3's literal scenario): the terminal
    emission's ``persist_high_water`` is swallowed while its ``append_event``
    SUCCEEDS -> ``max_event_seq > persisted_high_water``. The durable event must
    SURVIVE: the re-seed's ``max_event_seq`` floor keeps a fresh emission strictly
    above the durable slot, never clobbering it.

    ORACLE: mutate the re-seed to the OTHER single term, ``record.event_seq =
    workflow_journal.persisted_high_water(run_id)`` -> the floor lags the durable
    event, the fresh emission reuses the durable slot -> RED. (Guards the second
    term of ``max(...)``; complements BR-3's forward-fault guard, BR-7.)
    """
    fixture = RealDriveRunFixture(monkeypatch)
    injector = SwallowInjector(monkeypatch)
    injector.fail_high_water_for_event_type("run.completed")

    assert fixture.drive("R3b", step_ids=("s1",)) == RunState.COMPLETED

    pre_high_water = workflow_journal.persisted_high_water("R3b")
    pre_max_event = workflow_journal.max_event_seq("R3b")
    # The reverse fault is real: the durable terminal event outranks the (stale)
    # high-water because the high-water write for its seq was swallowed.
    assert pre_max_event > pre_high_water

    RestartSimulator.restart()
    new_seq = _emit_after_restart("R3b", "step.started")

    # The durable terminal event survives — a fresh emission is strictly above it,
    # never reusing/clobbering the durable slot.
    assert new_seq > pre_max_event
    assert new_seq == pre_max_event + 1

    events, _gaps = _read_events_via_route(client, "R3b")
    seqs = [e["seq"] for e in events]
    assert pre_max_event in seqs  # the durable terminal event is intact
    assert new_seq in seqs


# ===========================================================================
# BR-4 — F-1 terminal-state guard: a swallowed terminal event must not hang a
# follower. The U4 SSE route checks get_run terminal state and CLOSES.
# ===========================================================================
def test_br4_swallowed_terminal_event_does_not_hang_the_follower(client, monkeypatch):
    """A REAL run whose terminal ``run.completed`` event append is swallowed — so
    the durable timeline has NO terminal event — must still close the SSE follower:
    the F-1 guard checks ``get_run`` (which projects COMPLETED via the separate
    write-through) after durable replay and stops, rather than waiting on the bus
    forever. A hard timeout turns any regression into a loud failure, never a hang.
    """
    fixture = RealDriveRunFixture(monkeypatch)
    injector = SwallowInjector(monkeypatch)
    injector.fail_append_when(lambda seq, event_type: event_type == "run.completed")

    assert fixture.drive("R4", step_ids=("s1",)) == RunState.COMPLETED

    # The run ROW is terminal even though its terminal EVENT was swallowed — this is
    # the exact F-1 condition (durable timeline lacks run.completed).
    run_row = workflow_journal.get_run("R4")
    assert run_row is not None and run_row.state == "completed"
    assert "run.completed" not in [e.event_type for e in workflow_journal.read_events("R4")]

    # Journal-authoritative: clear the cache so the guard can only rely on get_run.
    RestartSimulator.restart()

    resp = _stream_with_timeout(client, "/workflows/runs/R4/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    frame_types = _parse_sse_event_types(resp.text)
    # The durable (non-terminal) events replayed, the stream CLOSED (we got here),
    # and it closed WITHOUT ever seeing a terminal event — proving the get_run
    # guard, not the terminal event, is what stopped the follower.
    assert "run.started" in frame_types
    assert "run.completed" not in frame_types


# ===========================================================================
# BR-5 — journal-authoritative after restart (NFR-DUR-1).
# ===========================================================================
def test_br5_journal_authoritative_after_restart(client, monkeypatch):
    """After ``run_registry.clear()`` a run's state, steps, and FULL event timeline
    are all recoverable from the journal with NO ``run_registry`` entry — the cache
    is a rebuildable projection, the journal is the source of truth (NFR-DUR-1).
    """
    fixture = RealDriveRunFixture(monkeypatch)
    assert (
        fixture.drive(
            "R5",
            step_ids=("s1", "s2"),
        )
        == RunState.COMPLETED
    )

    RestartSimulator.restart()
    assert "R5" not in ws.run_registry  # no cache dependency

    # State + steps recover from the journal (rebuild on the cold read).
    snap = ws.get_run_status("R5")
    assert snap.state == RunState.COMPLETED
    assert {s.id for s in snap.steps} == {"s1", "s2"}

    # The run/step projections read straight from the durable tables.
    run_row = workflow_journal.get_run("R5")
    assert run_row is not None and run_row.workflow_name == "wf"
    step_states = {s.step_id: s.state for s in workflow_journal.get_steps("R5")}
    assert step_states == {"s1": "completed", "s2": "completed"}

    # The full event timeline replays through the U4 route after the restart.
    RestartSimulator.restart()
    events, gaps = _read_events_via_route(client, "R5")
    types = [e["event_type"] for e in events]
    assert types[0] == "run.started"
    assert types[-1] == "run.completed"
    assert _TAXONOMY_MIN.issubset(set(types))
    assert gaps == []


# ===========================================================================
# BR-6 — no per-append migration (call-count, NEVER timing).
# ORACLE: migrate on every append -> spy count > 1 -> RED.
# ===========================================================================
def test_br6_event_migrator_invoked_at_most_once_across_many_appends(monkeypatch):
    """A real run emitting N (>1) events invokes ``_migrate_workflow_run_event`` at
    most ONCE — the migration is memoized per db-path per process, not run per
    append (NFR-PERF-1). A pure call-count assertion, never a timing measurement.
    """
    spy = MigratorSpy(monkeypatch)
    fixture = RealDriveRunFixture(monkeypatch)

    assert fixture.drive("R6", step_ids=("s1", "s2")) == RunState.COMPLETED

    # The run emitted many events (the full FR-1.4 taxonomy over 2 steps)...
    n_appends = len(workflow_journal.read_events("R6"))
    assert n_appends > 1
    # ...yet the event migrator ran at most once.
    assert spy.calls <= 1


# ===========================================================================
# BR-7 — guard-integrity meta-rule: each assertion carries its defect oracle.
# ===========================================================================
def test_br7_guard_integrity_defect_oracles_are_documented():
    """Every U9 assertion is only kept if it FAILS under its corresponding defect.
    This meta-guard asserts the module docstring records each oracle, so a future
    edit that drops an assertion's defect mapping is caught rather than leaving a
    decorative test (BR-7).
    """
    doc = __doc__ or ""
    for marker in (
        "BR-1",
        "BR-2",
        "BR-3",
        "BR-4",
        "BR-6",
        "_journal_event",  # BR-1 oracle
        "max_event_seq",  # BR-3 oracle (the supervisor's single-term mutation)
        "persisted_high_water",  # BR-3b oracle
    ):
        assert marker in doc, f"BR-7: oracle marker {marker!r} missing from the module docstring"
