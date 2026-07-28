"""U4 endpoint tests — events-follow SSE surface (issue #504, FR-6).

Covers the SSE live-follow arm content-negotiated onto the SAME
``GET /workflows/runs/{run_id}/events`` path as U3's batch read, over a REAL
durable journal (temp SQLite DB) — the point is to prove the exact wire contract
#505's client follower (U10) consumes:

- **Durable replay** (BR-1/BR-3): connecting with ``Accept: text/event-stream``
  replays the run's events as named SSE frames in seq order, each carrying
  ``id: <seq>`` so a native EventSource sets Last-Event-ID for reconnect.
- **Reconnect cursor** (BR-3): ``?after_seq=n`` / ``Last-Event-ID: n`` returns
  only seq > n — no duplicates, no spurious gaps; ``?after_seq=`` wins when both
  are supplied.
- **Declared gap** (BR-4): a hole (append 1,2,4) surfaces as a distinct
  ``event: gap`` frame carrying {after_seq, before_seq, missing_count, reason}
  interleaved at its position — not renumbered away.
- **F-1 terminal-state guard** (BR-5): an already-terminal run replays its
  durable events and CLOSES — it must NOT hang the follower. Guarded by a hard
  timeout so a regression fails loudly instead of blocking CI.
- **Journal-authoritative** (BR-6): the follow serves entirely from the durable
  table after ``run_registry.clear()`` — no in-memory dependency.
- **Live delivery**: an event appended while the stream is open is delivered
  within a bounded number of polls (driven directly against the generator with
  an ``asyncio.wait_for`` hard timeout so the infinite live loop can never hang
  the test).
- **Batch path unchanged**: a normal (non-stream) GET still returns U3's
  ``EventTimelinePage`` JSON byte-behavior-identical.

The journal is pointed at a temp DB via the patched ``DATABASE_FILE`` and the
event migration memo is reset, mirroring ``test_workflow_inspection_replay.py``.
"""

from __future__ import annotations

import asyncio
import json
import threading
from pathlib import Path
from typing import Dict, List

import pytest

from cli_agent_orchestrator.api.main import _follow_run_events
from cli_agent_orchestrator.services import workflow_journal, workflow_service

_SSE_HEADERS = {"Accept": "text/event-stream"}
# A generous hard cap: the whole point of the F-1 guard is that a terminal-run
# stream CLOSES. If a regression re-broke it, the request would hang forever and
# block CI — the timeout turns that into a loud failure instead.
_STREAM_TIMEOUT_S = 15.0


@pytest.fixture(autouse=True)
def _isolated_journal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh temp journal DB + clean registry/migration memo for each test."""
    db_path = tmp_path / "wf.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path, raising=True)
    workflow_journal._event_migrated_paths.clear()
    monkeypatch.setattr(workflow_service, "run_registry", {})
    yield db_path
    workflow_journal._event_migrated_paths.clear()


# ---------------------------------------------------------------------------
# Seed helpers — write directly into the durable journal (no live record).
# ---------------------------------------------------------------------------
def _append(run_id: str, *seqs: int, event_type: str = "step.started") -> None:
    for seq in seqs:
        workflow_journal.append_event(
            run_id, seq, event_type, event_schema_version=1, ts="2026-07-27T00:00:00Z"
        )


def _seed_terminal_run(run_id: str = "r1", state: str = "completed") -> None:
    """Insert a terminal ``workflow_run`` row so the follow generator closes.

    Every TestClient-driven SSE test seeds a terminal run first: the F-1 guard
    (BR-5) then closes the stream after durable replay, so ``client.get`` returns
    the full body deterministically instead of blocking on the live loop.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state=state,
        started_at="2026-07-27T00:00:00Z",
    )
    workflow_journal.update_run_state(run_id, state, "2026-07-27T00:00:05Z")


def _parse_sse(text: str) -> List[Dict]:
    """Parse an SSE body into a list of ``{event, data?, id?}`` frames."""
    frames: List[Dict] = []
    for block in (b for b in text.split("\n\n") if b.strip()):
        frame: Dict = {}
        for line in block.split("\n"):
            if line.startswith("event: "):
                frame["event"] = line[len("event: ") :]
            elif line.startswith("data: "):
                frame["data"] = json.loads(line[len("data: ") :])
            elif line.startswith("id: "):
                frame["id"] = line[len("id: ") :]
        frames.append(frame)
    return frames


def _get_with_timeout(client, url: str, headers: Dict[str, str]):
    """Run ``client.get`` on a daemon thread under a hard ``_STREAM_TIMEOUT_S``.

    A daemon thread (not a joined pool) is used deliberately: if a regression
    reopened the F-1 hang (BR-5) the request would never return, and a joined
    ``ThreadPoolExecutor``/``thread.join()`` would itself block forever on
    teardown. This helper raises ``TimeoutError`` and lets the (daemon) worker be
    abandoned, so the hang surfaces as a loud test failure instead of wedging the
    whole suite.
    """
    result: Dict = {}

    def _run() -> None:
        try:
            result["resp"] = client.get(url, headers={**_SSE_HEADERS, **headers})
        except Exception as exc:  # pragma: no cover - surfaced via result below
            result["exc"] = exc

    worker = threading.Thread(target=_run, daemon=True)
    worker.start()
    worker.join(timeout=_STREAM_TIMEOUT_S)
    if worker.is_alive():
        raise TimeoutError(
            f"SSE stream did not close within {_STREAM_TIMEOUT_S}s for {url} "
            "(F-1 terminal-state guard regression, BR-5)"
        )
    if "exc" in result:
        raise result["exc"]
    return result["resp"]


def _stream_text(client, url: str, headers: Dict[str, str]) -> str:
    """GET an SSE stream under a hard timeout; return the full response text.

    Every caller streams a TERMINAL run, so the F-1 guard (BR-5) closes the
    generator on its own well within the timeout.
    """
    resp = _get_with_timeout(client, url, headers)
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    return resp.text


# ---------------------------------------------------------------------------
# Durable replay (BR-1 / BR-3) — the #505-consumed frame contract.
# ---------------------------------------------------------------------------
def test_sse_replays_events_as_named_frames_with_id_equal_to_seq(client):
    """A terminal run streamed over SSE replays each event as ``event: <type>`` +
    ``data: <json>`` + ``id: <seq>`` in seq order, then closes (BR-1/BR-5)."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))

    assert [f["event"] for f in frames] == ["step.started", "step.started", "step.started"]
    assert [f["id"] for f in frames] == ["1", "2", "3"]  # id == seq for reconnect
    # BR-1 minimum fields present on each event frame.
    for f in frames:
        for key in ("seq", "run_id", "event_type", "state", "ts"):
            assert key in f["data"]
    assert [f["data"]["seq"] for f in frames] == [1, 2, 3]
    assert all(f["data"]["run_id"] == "r1" for f in frames)


def test_sse_selected_via_stream_query_flag(client):
    """``?stream=true`` selects the SSE arm even without the Accept header."""
    _seed_terminal_run("r1")
    _append("r1", 1)
    # No Accept: text/event-stream header — the ?stream=true flag alone selects SSE.
    resp = _get_with_timeout(client, "/workflows/runs/r1/events?stream=true", {"Accept": "*/*"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    assert [f["id"] for f in _parse_sse(resp.text)] == ["1"]


# ---------------------------------------------------------------------------
# Reconnect cursor (BR-3) — exact, dedupe-free.
# ---------------------------------------------------------------------------
def test_sse_after_seq_returns_only_later_seqs_no_dupes_no_gaps(client):
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3, 4)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events?after_seq=2", {}))

    assert [f["id"] for f in frames] == ["3", "4"]  # strictly > 2
    assert all(f["event"] != "gap" for f in frames)  # no spurious gap at the cursor


def test_sse_last_event_id_header_resumes_after_that_seq(client):
    """A native-EventSource reconnect carries the cursor in Last-Event-ID."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3, 4)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {"Last-Event-ID": "2"}))

    assert [f["id"] for f in frames] == ["3", "4"]


def test_sse_after_seq_query_takes_precedence_over_last_event_id(client):
    """BR-3 precedence: ``?after_seq=`` wins when both cursors are supplied."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3, 4)

    # after_seq=1 (query) vs Last-Event-ID=3 (header) -> query wins -> seq 2,3,4.
    frames = _parse_sse(
        _stream_text(client, "/workflows/runs/r1/events?after_seq=1", {"Last-Event-ID": "3"})
    )

    assert [f["id"] for f in frames] == ["2", "3", "4"]


def test_sse_malformed_last_event_id_replays_from_start(client):
    """A non-integer Last-Event-ID is ignored (replay from start), never a 400 —
    a reconnecting client must not be rejected for a garbled cursor."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2)

    frames = _parse_sse(
        _stream_text(client, "/workflows/runs/r1/events", {"Last-Event-ID": "not-a-number"})
    )

    assert [f["id"] for f in frames] == ["1", "2"]


# ---------------------------------------------------------------------------
# Declared gap (BR-4).
# ---------------------------------------------------------------------------
def test_sse_declared_gap_emitted_as_distinct_frame_at_position(client):
    """append 1,2,4 (seq 3 swallowed) -> a distinct ``event: gap`` frame carrying
    {after_seq, before_seq, missing_count, reason} is interleaved BEFORE seq 4 —
    the hole is declared, not renumbered away (BR-4)."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 4)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))

    events_seq = [f["id"] for f in frames if f["event"] != "gap"]
    assert events_seq == ["1", "2", "4"]  # not renumbered to 1,2,3

    gap_frames = [f for f in frames if f["event"] == "gap"]
    assert len(gap_frames) == 1
    gap = gap_frames[0]["data"]
    assert gap["after_seq"] == 2
    assert gap["before_seq"] == 4
    assert gap["missing_count"] == 1
    assert gap["reason"] == "append_failed"
    assert "id" not in gap_frames[0]  # a gap owns no seq of its own

    # Positioned between event 2 and event 4.
    seq_of = [f.get("event") if f["event"] == "gap" else f["id"] for f in frames]
    assert seq_of == ["1", "2", "gap", "4"]


# ---------------------------------------------------------------------------
# F-1 terminal-state guard (BR-5) — MUST replay-and-close, never hang.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("terminal_state", ["completed", "failed", "cancelled"])
def test_sse_already_terminal_run_replays_then_closes(client, terminal_state):
    """F-1 (BR-5): a run already in a terminal state replays its durable events
    and the stream CLOSES — it does not enter the live loop. The hard timeout in
    ``_stream_text`` fails loudly if a regression reintroduces the hang."""
    _seed_terminal_run("r1", state=terminal_state)
    _append("r1", 1, 2, event_type="step.started")
    _append("r1", 3, event_type=f"run.{terminal_state}")

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))

    # All durable events replayed, and the stream closed (we got here).
    assert [f["id"] for f in frames] == ["1", "2", "3"]
    assert frames[-1]["event"] == f"run.{terminal_state}"


def test_sse_terminal_run_with_no_events_closes_immediately(client):
    """A terminal run with an empty timeline still closes (empty body), never
    hangs — the guard fires on ``get_run`` even with nothing to replay."""
    _seed_terminal_run("r1")
    body = _stream_text(client, "/workflows/runs/r1/events", {})
    assert _parse_sse(body) == []


# ---------------------------------------------------------------------------
# Journal-authoritative (BR-6).
# ---------------------------------------------------------------------------
def test_sse_is_journal_authoritative_after_registry_cleared(client):
    """BR-6 / NFR-DUR-1: the follow serves entirely from the durable table with
    no in-memory dependency — clearing ``run_registry`` does not affect it."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)
    workflow_service.run_registry.clear()

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))
    assert [f["id"] for f in frames] == ["1", "2", "3"]


# ---------------------------------------------------------------------------
# Live delivery — an event appended while the stream is open (bounded + timeout).
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_live_follow_delivers_a_newly_appended_event():
    """A NON-terminal run enters the live-follow loop; an event appended after
    connect is delivered within a bounded number of polls. Driven directly
    against the generator with ``asyncio.wait_for`` hard timeouts so the infinite
    live loop can never hang the test (the generator is explicitly closed)."""
    _append("r1", 1)  # replayable event; no run row -> non-terminal -> live loop

    gen = _follow_run_events("r1", None)
    try:
        # Phase 1 replay delivers event 1.
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 1" in frame1

        # Append a new event while the generator is suspended; the live loop must
        # pick it up on a subsequent poll (bounded by the timeout).
        _append("r1", 2)
        frame2 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 2" in frame2
        assert '"seq": 2' in frame2
    finally:
        await gen.aclose()  # cancel-safe close (GeneratorExit path)


@pytest.mark.asyncio
async def test_live_follow_closes_when_run_reaches_terminal_state():
    """The live loop terminates when ``get_run`` reports a terminal state — the
    generator raises ``StopAsyncIteration`` (closes) rather than looping forever
    (BR-5). Bounded by ``asyncio.wait_for``."""
    _append("r1", 1)
    # A live (running) run row: replay yields event 1, then the loop polls.
    workflow_journal.insert_run(
        run_id="r1",
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-27T00:00:00Z",
    )

    gen = _follow_run_events("r1", None)
    try:
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 1" in frame1

        # Flip the run terminal; the next poll must drain and close.
        workflow_journal.update_run_state("r1", "completed", "2026-07-27T00:00:05Z")
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
    finally:
        await gen.aclose()


# ---------------------------------------------------------------------------
# Batch path unchanged (BR: U4 extends, does not rewrite).
# ---------------------------------------------------------------------------
def test_batch_json_path_unchanged_for_non_stream_request(client):
    """A normal (non-stream) GET still returns U3's ``EventTimelinePage`` JSON,
    byte-behavior-identical — the SSE arm is additive."""
    _append("r1", 1, 2, 3)
    resp = client.get("/workflows/runs/r1/events")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert body["gaps"] == []
    assert body["next_after_seq"] == 3


def test_batch_json_path_with_explicit_application_json_accept(client):
    """Accept: application/json (not text/event-stream) selects the batch arm."""
    _append("r1", 1, 2)
    resp = client.get("/workflows/runs/r1/events", headers={"Accept": "application/json"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    assert [e["seq"] for e in resp.json()["events"]] == [1, 2]
