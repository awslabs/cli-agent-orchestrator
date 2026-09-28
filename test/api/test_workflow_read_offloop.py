"""The four run READ endpoints must not run synchronous sqlite on the event loop.

PR #526 review round 3 — BLOCKING. ``get_workflow_run_endpoint``, the BATCH arm of
``get_workflow_run_events_endpoint``, ``compare_workflow_runs_endpoint`` and
``get_workflow_run_diagnostics_endpoint`` are all ``async def`` and all call the
synchronous ``workflow_journal`` DAL. Called bare, each blocks the single event
loop for the whole read — including the 250 ms SSE followers
(``_EVENTS_FOLLOW_POLL_INTERVAL_S``). ``/compare`` reads two runs' FULL event sets
and ``/diagnostics`` reads all events plus every step row, so these are the two
heaviest readers on the surface.

The PR's own SSE arm and DELETE endpoint already wrap every DAL call in
``asyncio.to_thread``; these four did not, so one route's two arms disagreed on
event-loop discipline.

WHY THESE TESTS ASSERT A THREAD IDENTITY. The obvious test — "is there an `await`
in the source" — is a text assertion that cannot observe behaviour, and a mock that
merely records a call cannot tell a bare call from an off-loaded one. What
distinguishes them is WHERE the DAL body runs: ``asyncio.to_thread`` executes it in a
worker thread, so the recorded ``threading.current_thread()`` differs from the thread
the endpoint coroutine is running on. Removing a ``to_thread`` wrapper puts that call
back on the loop thread and turns the matching test RED — verified by mutation, one
endpoint at a time, including a PARTIAL conversion of ``/compare``.

See the comment on ``_MAIN_THREAD_NAME`` for the subtlety that makes the naive
version of this test vacuous: under TestClient the loop is NOT the main thread.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Dict, List

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.workflow import WorkflowSpec, WorkflowStep
from cli_agent_orchestrator.services import workflow_journal, workflow_service

_SPEC = WorkflowSpec(
    name="wf",
    steps=[WorkflowStep(id="s1", provider="claude_code", agent="dev", prompt="go")],
)

# CRITICAL — what "off the loop" must be measured AGAINST.
#
# Under Starlette's TestClient the event loop does NOT run on the interpreter's main
# thread: it runs in a portal thread named "asyncio-portal-<id>". So asserting
# `recorded != threading.main_thread().name` is VACUOUS — a bare, on-loop sqlite
# call runs in the portal thread and sails past it. (Observed while writing these
# tests: an unwrapped `get_run_status` recorded "asyncio-portal-10993e150" while the
# off-loaded calls recorded "asyncio_0".)
#
# The honest comparison is against the thread the ENDPOINT COROUTINE itself runs on,
# captured from inside the live request by the `loop_thread` fixture below. A recorded
# thread equal to that one means the DAL body executed on the loop. The main-thread
# check is kept as a cheap secondary assertion, never as the primary one.
_MAIN_THREAD_NAME = threading.main_thread().name


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh temp journal DB + empty registry, mirroring the U6 endpoint tests."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    workflow_journal._event_migrated_paths.clear()
    monkeypatch.setattr(workflow_service, "run_registry", {})
    yield db_path
    workflow_journal._event_migrated_paths.clear()


@pytest.fixture
def client() -> TestClient:
    # base_url must be localhost or the host-header guard returns 400.
    return TestClient(app, base_url="http://localhost")


def _seed_run(run_id: str, *, state: str = "completed") -> None:
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot=_SPEC.model_dump_json(),
        inputs_json="{}",
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.insert_steps(run_id, [("s1", state)], "2026-07-27T00:00:00Z")
    workflow_journal.update_step(
        run_id, "s1", state, 1, "2026-07-27T00:00:01Z", output_json=None, error=None
    )
    workflow_journal.append_event(
        run_id, 1, "step.started", event_schema_version=1, ts="2026-07-27T00:00:00Z"
    )
    workflow_journal.persist_high_water(run_id, 1)


@pytest.fixture
def loop_thread(monkeypatch: pytest.MonkeyPatch) -> Dict[str, str]:
    """Capture the name of the thread the endpoint coroutine runs on.

    Hooks ``asyncio.to_thread`` itself: it is called FROM the coroutine, so at call
    time ``threading.current_thread()`` IS the loop thread. The real ``to_thread``
    still runs, so this observes without changing behaviour. If an endpoint made no
    ``to_thread`` call at all the capture stays empty, which the assertion treats as
    a failure rather than a pass.
    """
    captured: Dict[str, str] = {}
    real_to_thread = asyncio.to_thread

    async def _probe(func, /, *args, **kwargs):
        captured.setdefault("name", threading.current_thread().name)
        return await real_to_thread(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", _probe)
    return captured


def _record_threads(monkeypatch: pytest.MonkeyPatch, names: List[str]) -> Dict[str, List[str]]:
    """Wrap each named DAL function so it records the thread its body ran on.

    The real function is still called (so the endpoint returns a real response and
    a broken wrapper cannot pass by returning a stub).
    """
    seen: Dict[str, List[str]] = {n: [] for n in names}

    for name in names:
        real = getattr(workflow_journal, name)

        def _make(fn, key):
            def _wrapped(*args, **kwargs):
                seen[key].append(threading.current_thread().name)
                return fn(*args, **kwargs)

            return _wrapped

        monkeypatch.setattr(workflow_journal, name, _make(real, name))

    return seen


def _assert_all_offloop(
    seen: Dict[str, List[str]],
    loop_thread: Dict[str, str],
    *,
    expected_calls: Dict[str, int],
) -> None:
    loop_name = loop_thread.get("name")
    assert loop_name, (
        "no asyncio.to_thread call was observed at all, so the loop thread was "
        "never captured — the endpoint off-loads nothing"
    )

    for name, count in expected_calls.items():
        assert len(seen[name]) == count, (
            f"{name}: expected {count} call(s), recorded {len(seen[name])} "
            f"— the endpoint's DAL call shape changed, so this test is no longer "
            f"measuring what it claims"
        )
        for thread_name in seen[name]:
            assert thread_name != loop_name, (
                f"{name} ran on the EVENT-LOOP thread ({thread_name}) — a "
                f"synchronous sqlite call on the loop blocks every other task, "
                f"including the 250 ms SSE followers"
            )
            assert (
                thread_name != _MAIN_THREAD_NAME
            ), f"{name} ran on the main thread ({thread_name})"


def test_inspect_route_reads_off_the_event_loop(client, monkeypatch, loop_thread):
    """FR-1.1 — GET /workflows/runs/{run_id}: every DAL call off-loop.

    The recorded counts are higher than this route's own two calls because
    ``workflow_service.get_run_status`` reads the journal itself on a registry cache
    miss (the cold-read fallback). That call is off-loaded too — found while writing
    this test: left bare it recorded the PORTAL thread, i.e. it was running sqlite
    on the loop, which the original main-thread-only assertion could not see.

    The counts are asserted so a change in call shape fails loudly instead of
    silently narrowing what is measured; the load-bearing assertion is that NOT ONE
    recorded call ran on the loop thread.
    """
    _seed_run("r1")
    seen = _record_threads(monkeypatch, ["get_run", "get_steps"])

    resp = client.get("/workflows/runs/r1")

    assert resp.status_code == 200
    _assert_all_offloop(seen, loop_thread, expected_calls={"get_run": 3, "get_steps": 2})


def test_events_batch_arm_reads_off_the_event_loop(client, monkeypatch, loop_thread):
    """FR-1.1 — the BATCH arm of .../events.

    This is the arm most easily missed in the fix: it sits AFTER the SSE
    ``StreamingResponse`` return inside the content-negotiation branch, so it does
    not read like a sibling of the other three endpoints. The SSE arm was already
    off-loop, which made the gap invisible on a grep for ``to_thread``.
    """
    _seed_run("r1")
    seen = _record_threads(monkeypatch, ["read_events_with_gaps"])

    resp = client.get("/workflows/runs/r1/events")

    assert resp.status_code == 200
    _assert_all_offloop(seen, loop_thread, expected_calls={"read_events_with_gaps": 1})


def test_compare_route_offloads_all_six_dal_calls(client, monkeypatch, loop_thread):
    """FR-1.1 — /compare is the heaviest reader: 2 get_run + 2 get_steps + 2 reads.

    Asserting the exact count of SIX matters: a partial conversion that off-loads
    the two ``get_run`` calls and leaves the four heavier reads bare would pass a
    "did any call go off-loop" check while still blocking the loop for the actual
    payload. Each of the six is checked individually.
    """
    _seed_run("a")
    _seed_run("b")
    seen = _record_threads(monkeypatch, ["get_run", "get_steps", "read_events_with_gaps"])

    resp = client.get("/workflows/runs/a/compare", params={"against": "b"})

    assert resp.status_code == 200
    _assert_all_offloop(
        seen, loop_thread, expected_calls={"get_run": 2, "get_steps": 2, "read_events_with_gaps": 2}
    )


def test_diagnostics_route_reads_off_the_event_loop(client, monkeypatch, loop_thread):
    """FR-1.1 — /diagnostics: get_run + all events + all step rows, off-loop."""
    _seed_run("r1")
    seen = _record_threads(monkeypatch, ["get_run", "read_events_with_gaps", "get_steps"])

    resp = client.get("/workflows/runs/r1/diagnostics")

    assert resp.status_code == 200
    _assert_all_offloop(
        seen, loop_thread, expected_calls={"get_run": 1, "read_events_with_gaps": 1, "get_steps": 1}
    )


def test_compare_404_precedence_is_unchanged_by_the_offload(client, monkeypatch):
    """FR-1.2 / BR-8 — the off-load must NOT become an ``asyncio.gather``.

    The two ``get_run`` calls are separated by their own 404 raises, so a missing
    BASELINE must 404 on the baseline id without ever reading the compare target.
    Gathering the six calls would change which 404 fires and would read the second
    run for a request that is already a 404. Guarded here because the temptation to
    gather is exactly what a "make it concurrent" refactor would do next.
    """
    _seed_run("b")  # target exists; baseline deliberately does not
    seen = _record_threads(monkeypatch, ["get_run"])

    resp = client.get("/workflows/runs/missing/compare", params={"against": "b"})

    assert resp.status_code == 404
    assert "missing" in resp.json()["detail"]
    # Short-circuited: the target was never read.
    assert len(seen["get_run"]) == 1
