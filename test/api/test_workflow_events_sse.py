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

from cli_agent_orchestrator.api.main import _follow_run_events, _merge_ordered_sse_frames
from cli_agent_orchestrator.services import workflow_journal, workflow_service
from cli_agent_orchestrator.services.workflow_journal import GapMarker

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


def _seed_running_run(run_id: str = "r1") -> None:
    """Insert a NON-terminal ``workflow_run`` row so the follow enters live-follow.

    Required because an ABSENT run row is no longer a path into the live loop: a
    run that does not exist can never go terminal, so the generator declares it
    absent and closes rather than polling forever. A test that wants the live
    loop must seed a real running run.
    """
    workflow_journal.insert_run(
        run_id=run_id,
        workflow_name="wf",
        spec_snapshot="{}",
        inputs_json="{}",
        state="running",
        started_at="2026-07-27T00:00:00Z",
    )


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
    _seed_running_run("r1")  # a REAL non-terminal run -> live loop
    _append("r1", 1)  # replayable event

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


# ---------------------------------------------------------------------------
# PR 526 review — MUST-FIX 2: an ABSENT run must terminate the stream.
#
# Before the fix the Phase-2 guard read `run is not None and state in TERMINAL`,
# so `get_run() -> None` skipped it entirely and Phase 3 entered an unbounded
# `while True` poll. A typo'd id or a deleted run pinned a connection and a poll
# cycle forever. These drive the generator directly with hard timeouts, so a
# regression HANGS-then-fails rather than passing.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_follow_closes_for_a_run_id_that_never_existed():
    """An unknown id declares `run_absent` and STOPS — never enters live-follow."""
    gen = _follow_run_events("no-such-run", None)
    try:
        frame = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "event: run_absent" in frame
        assert '"run_id": "no-such-run"' in frame
        # And the stream ENDS — it does not fall through to the poll loop.
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_follow_closes_for_a_deleted_run_with_orphan_events():
    """Events present but the run row gone (deleted / retention-swept): replay
    what is durable, then declare absent and close rather than polling forever."""
    _append("r1", 1)  # an orphan event row, no run row
    gen = _follow_run_events("r1", None)
    try:
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 1" in frame1  # Phase 1 replay still serves durable rows
        frame2 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "event: run_absent" in frame2
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_follow_closes_when_the_run_is_deleted_mid_follow():
    """The Phase-3 arm of the same hole: a run that VANISHES while being followed
    (DELETE endpoint or retention sweep) must close, not poll forever."""
    _seed_running_run("r1")
    _append("r1", 1)

    gen = _follow_run_events("r1", None)
    try:
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 1" in frame1

        # Delete the run out from under the live follower.
        workflow_journal.delete_run("r1")
        frame2 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "event: run_absent" in frame2
        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
    finally:
        await gen.aclose()


def test_absent_run_stream_over_http_terminates(client):
    """End-to-end through the route: the SSE arm for an unknown id returns a
    COMPLETE body (the absent frame) instead of hanging the request."""
    resp = _get_with_timeout(
        client, "/workflows/runs/ghost/events", {"Accept": "text/event-stream"}
    )
    assert resp.status_code == 200
    frames = _parse_sse(resp.text)
    assert [f["event"] for f in frames] == ["run_absent"]
    assert frames[0]["data"]["run_id"] == "ghost"


# ---------------------------------------------------------------------------
# PR 526 human review — BLOCKING 1: the SSE arm silently DROPPED trailing gaps.
#
# `_merge_ordered_sse_frames` matched gaps by `gaps_by_before.get(event.seq)` —
# it only emitted a gap whose `before_seq` equalled a DELIVERED event's seq. The
# trailing marker synthesized for a terminal run whose last append(s) were
# swallowed carries `before_seq = high_water + 1`, a sentinel that by
# construction matches NO stored event, so the "run ended, the last N events are
# lost" fault was declared by the batch arm and the diagnostics bundle but NEVER
# by the live stream. There was zero SSE test coverage of a trailing gap.
#
# Every test below seeds the fault the same way the DAL suite does: land some
# events, persist a HIGHER high-water (the seq was allocated but its append was
# lost), and put the run in a terminal state.
# ---------------------------------------------------------------------------
def _seed_swallowed_trailing_append(run_id: str, landed: tuple, high_water: int) -> None:
    """Land ``landed`` seqs, then claim ``high_water`` was allocated and lost."""
    _append(run_id, *landed)
    workflow_journal.persist_high_water(run_id, high_water)


def test_sse_declares_a_trailing_gap_for_a_terminal_run(client):
    """Events 1,2 landed; seq 3 was allocated and its append swallowed; the run
    then ended. The stream MUST carry an ``event: gap`` frame with
    ``reason=append_failed_trailing`` — before the fix it carried none."""
    _seed_terminal_run("r1")
    _seed_swallowed_trailing_append("r1", (1, 2), high_water=3)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))

    gap_frames = [f for f in frames if f["event"] == "gap"]
    assert len(gap_frames) == 1, f"expected exactly one gap frame, got {frames}"
    gap = gap_frames[0]["data"]
    assert gap["reason"] == "append_failed_trailing"
    assert gap["after_seq"] == 2
    assert gap["before_seq"] == 4  # high_water + 1 sentinel
    assert gap["missing_count"] == 1
    assert "id" not in gap_frames[0]  # a gap owns no seq of its own

    # And it lands at the END of the stream, past every delivered event.
    assert [f.get("id") for f in frames if f["event"] != "gap"] == ["1", "2"]
    assert frames[-1]["event"] == "gap"


def test_sse_trailing_gap_declared_on_an_empty_caught_up_page(client):
    """The most severe shape: a follower already at the cursor gets NO events, so
    there is nothing at all to hang a gap off. The declaration must still arrive
    (this is precisely the case the delivered-event match could never serve)."""
    _seed_terminal_run("r1")
    _seed_swallowed_trailing_append("r1", (1, 2), high_water=3)

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events?after_seq=2", {}))

    assert [f["event"] for f in frames] == ["gap"]
    assert frames[0]["data"]["reason"] == "append_failed_trailing"
    assert frames[0]["data"]["after_seq"] == 2
    assert frames[0]["data"]["missing_count"] == 1


def test_sse_interior_gap_still_lands_immediately_before_its_bounding_event(client):
    """Placement regression guard: the leftover drain must NOT relocate an
    interior gap to the end. Interior AND trailing gaps coexist here, so a fix
    that simply appended every gap would fail this ordering assertion."""
    _seed_terminal_run("r1")
    _append("r1", 1, 3)  # seq 2 lost -> interior gap before event 3
    workflow_journal.persist_high_water("r1", 5)  # 4,5 lost -> trailing gap

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))

    shape = [f["event"] if f["event"] == "gap" else f["id"] for f in frames]
    assert shape == ["1", "gap", "3", "gap"]
    interior, trailing = (f["data"] for f in frames if f["event"] == "gap")
    assert interior["reason"] == "append_failed"
    assert (interior["after_seq"], interior["before_seq"]) == (1, 3)
    assert trailing["reason"] == "append_failed_trailing"
    assert (trailing["after_seq"], trailing["missing_count"]) == (3, 2)


@pytest.mark.asyncio
async def test_live_follow_emits_no_trailing_gap_while_the_run_is_running():
    """The terminal-only guard, at the SSE layer: a RUNNING run legitimately sits
    one seq ahead for the duration of every in-flight append, so the live loop
    must stay silent — a phantom gap on every poll would be worse than the bug."""
    _seed_running_run("r1")
    _seed_swallowed_trailing_append("r1", (1,), high_water=2)  # looks like a hole

    gen = _follow_run_events("r1", None)
    try:
        frame1 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "id: 1" in frame1
        assert "event: gap" not in frame1

        # Nothing further is delivered while the run stays live: the next frame
        # can only arrive once something real happens. Prove it by appending the
        # event that WAS in flight and seeing that arrive rather than a gap.
        _append("r1", 2)
        frame2 = await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
        assert "event: gap" not in frame2
        assert "id: 2" in frame2
    finally:
        await gen.aclose()


@pytest.mark.asyncio
async def test_live_follow_declares_the_trailing_gap_when_the_run_goes_terminal():
    """The Phase-3 arm: a run followed LIVE that ends with a swallowed final
    append must declare the hole before the stream closes. Until it is terminal
    the DAL declares nothing, so this gap can only ever surface on the terminal
    drain — the second of the two reads on the transition pass."""
    _seed_running_run("r1")
    _seed_swallowed_trailing_append("r1", (1,), high_water=2)

    gen = _follow_run_events("r1", None)
    collected: List[str] = []
    try:
        collected.append(await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S))
        assert "id: 1" in collected[0]

        # The run ends WITHOUT seq 2 ever landing.
        workflow_journal.update_run_state("r1", "failed", "2026-07-27T00:00:05Z")

        # Drain to exhaustion; the trailing gap must appear before StopAsyncIteration.
        while True:
            try:
                collected.append(await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S))
            except StopAsyncIteration:
                break
    finally:
        await gen.aclose()

    gap_frames = [f for f in collected if f.startswith("event: gap")]
    assert len(gap_frames) == 1, f"expected exactly one gap frame, got {collected}"
    assert '"reason": "append_failed_trailing"' in gap_frames[0]
    assert '"after_seq": 1' in gap_frames[0]
    assert '"missing_count": 1' in gap_frames[0]


def test_trailing_gap_is_declared_at_most_once_per_connection():
    """Every read of a terminal run re-synthesizes the SAME trailing marker, and
    the follow loop reads TWICE on the terminal-transition pass (the poll read,
    then the drain read), so the per-connection ``declared`` set must keep the
    follower from being told about one hole twice.

    Driven directly against ``_merge_ordered_sse_frames`` with ONE shared set and
    two SEPARATELY-CONSTRUCTED markers of the same span — that is precisely what
    the two reads hand it (distinct instances, so object identity would not
    dedupe them). Exercising this through the generator is not reliable: whether
    the transition is observed by the Phase-2 guard (one read) or the Phase-3
    poll+drain (two reads) is a scheduling race, so a generator-driven version
    passes vacuously whenever Phase 2 wins.
    """
    declared: set = set()
    gap_read_1 = GapMarker(
        after_seq=2, before_seq=4, missing_count=1, reason="append_failed_trailing"
    )
    gap_read_2 = GapMarker(
        after_seq=2, before_seq=4, missing_count=1, reason="append_failed_trailing"
    )
    assert gap_read_1 is not gap_read_2  # distinct instances, as two reads produce

    first = _merge_ordered_sse_frames([], [gap_read_1], declared)
    second = _merge_ordered_sse_frames([], [gap_read_2], declared)

    assert len(first) == 1 and first[0].startswith("event: gap")
    assert second == [], f"the same hole was declared twice: {second}"


def test_two_distinct_gaps_are_both_declared_on_one_connection():
    """The dedupe must key on the gap's SPAN, not merely on "a gap was emitted" —
    otherwise a second, genuinely different hole later in the same stream would be
    swallowed. Guards against over-correcting the duplicate fix."""
    declared: set = set()
    interior = GapMarker(after_seq=1, before_seq=3, missing_count=1, reason="append_failed")
    trailing = GapMarker(
        after_seq=3, before_seq=6, missing_count=2, reason="append_failed_trailing"
    )

    first = _merge_ordered_sse_frames([], [interior], declared)
    second = _merge_ordered_sse_frames([], [trailing], declared)

    assert len(first) == 1
    assert len(second) == 1, "a different hole must still be declared"
    assert '"after_seq": 3' in second[0]


def test_sse_healthy_terminal_run_declares_no_gap(client):
    """The negative control: every allocated seq landed -> no gap frame at all.
    Without this, a fix that unconditionally appended a gap would pass the rest."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)
    workflow_journal.persist_high_water("r1", 3)  # everything landed

    frames = _parse_sse(_stream_text(client, "/workflows/runs/r1/events", {}))
    assert [f["id"] for f in frames] == ["1", "2", "3"]
    assert all(f["event"] != "gap" for f in frames)


# ---------------------------------------------------------------------------
# PR 526 human review — NIT N6: _TERMINAL_RUN_STATES was triplicated.
#
# The same set was defined independently in workflow_journal.py, api/main.py, and
# (cross-language) RunDetail.tsx. The Python copies could drift: the journal's
# copy gates whether a TRAILING GAP is declared, main.py's gates whether the SSE
# stream CLOSES, so a divergence means the stream closes on a run the journal
# still treats as live (or hangs on one it treats as ended). main.py now imports
# the journal's set.
# ---------------------------------------------------------------------------
def test_terminal_run_states_is_one_shared_object_not_a_copy():
    """Identity, not equality: an equal-but-separate copy could still drift later."""
    from cli_agent_orchestrator.api import main as api_main

    assert api_main._TERMINAL_RUN_STATES is workflow_journal._TERMINAL_RUN_STATES


def test_terminal_run_states_membership_is_exactly_the_three_states():
    """The literal contract, hard-coded rather than read off the constant under
    test — a fixture sourced from the production value would follow a bad edit
    and stay green."""
    assert set(workflow_journal._TERMINAL_RUN_STATES) == {"completed", "failed", "cancelled"}
    # "running" and "pending" are NOT terminal: treating either as terminal would
    # close every live follower immediately.
    assert "running" not in workflow_journal._TERMINAL_RUN_STATES
    assert "pending" not in workflow_journal._TERMINAL_RUN_STATES


@pytest.mark.asyncio
async def test_phase3_poll_loop_drains_the_trailing_gap_on_the_terminal_pass():
    """The Phase-3 drain specifically — NOT the Phase-2 guard.

    `test_live_follow_declares_the_trailing_gap_when_the_run_goes_terminal` looks
    like it covers the Phase-3 loop, but it does not: the state flips before the
    generator's Phase-2 read, so that guard always answers first. Reverting ONLY
    the Phase-3 drain leaves the whole SSE suite green (PR #526 review fix cycle
    1, I-3), which means those four lines were unprotected.

    This test pins them by making the run go terminal strictly AFTER Phase 2 has
    already seen it live: `get_run` is wrapped so the first two calls (the Phase-2
    guard, then the first Phase-3 poll) report RUNNING, and only the third — well
    inside the poll loop — reports the terminal state. The gap can then only reach
    the client through the Phase-3 terminal drain.
    """
    _seed_running_run("r1")
    _seed_swallowed_trailing_append("r1", (1,), high_water=2)

    real_get_run = workflow_journal.get_run
    calls = {"n": 0}

    def _late_terminal(run_id: str):
        # Count the follow loop's state reads. On the 3rd — the second Phase-3
        # poll — flip the run terminal FOR REAL in the DB, so the DAL's own
        # `_is_terminal_run` (which reads the row, not this mock) also sees it and
        # the trailing gap becomes declarable exactly there.
        calls["n"] += 1
        if calls["n"] == 3:
            real_update("r1", "failed", "2026-07-27T00:00:05Z")
        return real_get_run(run_id)

    real_update = workflow_journal.update_run_state
    # main.py calls `workflow_journal.get_run` through the module object, so the
    # patch has to land on the module attribute itself.
    workflow_journal.get_run = _late_terminal  # type: ignore[assignment]
    collected: List[str] = []
    try:
        gen = _follow_run_events("r1", None)
        try:
            collected.append(await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S))
            assert "id: 1" in collected[0]
            while True:
                try:
                    collected.append(
                        await asyncio.wait_for(gen.__anext__(), timeout=_STREAM_TIMEOUT_S)
                    )
                except StopAsyncIteration:
                    break
        finally:
            await gen.aclose()
    finally:
        workflow_journal.get_run = real_get_run  # type: ignore[assignment]

    # Phase 2 saw a live run (calls 1-2), so the declaration cannot have come from
    # there — it can only be the Phase-3 terminal drain.
    assert calls["n"] >= 3, f"the poll loop was never reached (get_run calls: {calls['n']})"
    gap_frames = [f for f in collected if f.startswith("event: gap")]
    assert len(gap_frames) == 1, f"the Phase-3 drain declared no gap: {collected}"
    assert '"reason": "append_failed_trailing"' in gap_frames[0]
    assert '"after_seq": 1' in gap_frames[0]


# ---------------------------------------------------------------------------
# PR #526 review round 3 — BLOCKING: `after_seq` had no lower bound.
#
# The sibling `limit` carries ge=1, but `after_seq` carried no `ge` at all, so a
# negative cursor reached the reader and fabricated a phantom GapMarker on a
# healthy, lossless run (reason="append_failed", missing_count=abs(cursor)) —
# inverting the contract that a declared gap means an event was actually lost.
#
# Defence in depth: the ROUTE rejects the meaningless input (these tests), and the
# READER clamps regardless of caller (test_workflow_journal_events.py). Neither
# alone is sufficient — a route-only bound leaves /compare, /diagnostics and any
# future caller exposed; a reader-only clamp answers 200 for nonsense input.
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("cursor", [-1, -5, -100])
def test_negative_after_seq_is_rejected_with_422(client, cursor: int):
    """FR-2.1 — a negative cursor is meaningless (seqs start at 1) and is refused
    BEFORE the handler runs. RED before the fix: it returned 200 with a phantom gap."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)

    resp = client.get("/workflows/runs/r1/events", params={"after_seq": cursor})

    assert resp.status_code == 422


def test_after_seq_zero_is_accepted_as_the_from_start_cursor(client):
    """FR-2.1 boundary — the bound is ge=0, NOT ge=1: 0 is the legitimate
    from-start cursor and must still be accepted. A fix that used ge=1 would break
    every client that pages from 0."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)

    resp = client.get("/workflows/runs/r1/events", params={"after_seq": 0})

    assert resp.status_code == 200
    body = resp.json()
    assert [e["seq"] for e in body["events"]] == [1, 2, 3]
    assert body["gaps"] == []


def test_positive_after_seq_still_pages_normally(client):
    """FR-2.1 / NFR-4 — the bound must not disturb the normal cursor path."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)

    resp = client.get("/workflows/runs/r1/events", params={"after_seq": 1})

    assert resp.status_code == 200
    body = resp.json()
    assert [e["seq"] for e in body["events"]] == [2, 3]
    assert body["gaps"] == []


def test_limit_lower_bound_is_untouched_by_the_after_seq_fix(client):
    """Guard against the likely slip: `limit` sits three lines below `after_seq` in
    the same signature, so a fix could easily land ge=0 on the WRONG parameter.
    `limit=0` must still be a 422 (its bound is ge=1)."""
    _seed_terminal_run("r1")
    _append("r1", 1, 2, 3)

    assert client.get("/workflows/runs/r1/events", params={"limit": 0}).status_code == 422
    assert client.get("/workflows/runs/r1/events", params={"limit": 1}).status_code == 200
