"""The off-list ``emit_ui`` refusal record (#1158, Option A).

A real off-list call still gets the same 400, and the server now also publishes
a metadata-only refusal record: built from a fixed schema, with no props,
attributed only to a terminal CAO knows, rate-limited, appended to the event log
and published lossily. These tests drive the real route.
"""

from __future__ import annotations

import asyncio
import json
from test._support.agui_conformance import check_lifecycle, parse_sse, validate_frames
from test._support.agui_stream_harness import install
from typing import Any, Dict, Iterator, List, Optional

import httpx
import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services import event_log_service, sse_bus
from cli_agent_orchestrator.services.agui import ambient_run
from cli_agent_orchestrator.services.agui.refusal import reset_refusal_limiter

client = TestClient(app, base_url="http://localhost")

MARKER = "PROPS-MARKER-7f3a9c"


@pytest.fixture(autouse=True)
def _fresh(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    monkeypatch.delenv("AUTH0_DOMAIN", raising=False)
    monkeypatch.delenv("CAO_AUTH_JWKS_URI", raising=False)
    event_log_service.reset_event_log()
    sse_bus.reset_bus()
    reset_refusal_limiter()
    ambient_run.reset_stop()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.terminal_exists", lambda tid: False
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_session_incarnation",
        lambda name: f"inc-{name}",
    )
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)
    event_log_service.reset_event_log()
    sse_bus.reset_bus()
    reset_refusal_limiter()
    ambient_run.reset_stop()


def _refusals() -> List[Dict[str, Any]]:
    return [
        e
        for e in event_log_service.get_event_log().history()
        if (e.get("detail") or {}).get("event_type") == "agent_ui_refused"
    ]


def _off_list(component: str = "iframe", **extra: Any) -> httpx.Response:
    body = {"component": component, "props": {"src": MARKER}, **extra}
    return client.post("/agui/v1/emit_ui", json=body)


# A `since` before every record: the stream replays history only when asked.
EPOCH = "1970-01-01T00:00:00Z"


def _stream(monkeypatch: pytest.MonkeyPatch, headers: Optional[Dict[str, str]] = None, **params):
    """GET the stream over the records the real event log now holds."""
    if not headers:
        params.setdefault("since", EPOCH)
    log = event_log_service.get_event_log()
    install(monkeypatch)
    monkeypatch.setattr(event_log_service, "get_event_log", lambda: log)
    with client.stream("GET", "/agui/v1/stream", params=params, headers=headers or {}) as r:
        body = "".join(r.iter_text())
    events = parse_sse(body)
    validate_frames(events)
    check_lifecycle(events)
    return body, events


def _raw_refusals(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [e for e in events if e["type"] == "RAW" and "rejected_component" in e["event"]]


def test_the_caller_still_gets_the_same_400() -> None:
    resp = _off_list()
    assert resp.status_code == 400
    assert resp.json()["detail"] == (
        "Unknown UI component 'iframe'. Allowed: "
        "['agent_card', 'approval_card', 'choice_prompt', 'diff_summary', 'metric', 'progress']"
    )


def test_an_off_list_call_publishes_one_metadata_only_refusal_record() -> None:
    _off_list()
    [record] = _refusals()
    assert record["kind"] == "other"
    assert record["terminal_id"] is None and record["session_name"] is None
    assert set(record["detail"]) == {"event_type", "ui"}
    assert set(record["detail"]["ui"]) == {"component"}
    assert record["detail"]["ui"]["component"].startswith("iframe.")
    assert MARKER not in json.dumps(record)


def test_the_props_marker_never_appears_in_the_stream_bytes(monkeypatch) -> None:
    _off_list(component=f"x-{MARKER[:4]}", session_name=MARKER, extra=MARKER)
    body, events = _stream(monkeypatch)
    [raw] = _raw_refusals(events)
    assert "props" not in raw["event"]
    assert MARKER not in body


def test_a_name_that_cleans_to_an_allowed_name_is_streamed_as_a_refusal(monkeypatch) -> None:
    for name in ("metric!", "metric "):
        assert _off_list(component=name).status_code == 400
    body, events = _stream(monkeypatch)
    assert not [e for e in events if e["type"] == "CUSTOM"]
    names = [e["event"]["rejected_component"] for e in _raw_refusals(events)]
    assert [n.split(".")[0] for n in names] == ["metric_", "metric_"]
    assert len(set(names)) == 2


def test_a_spoofed_subagent_and_an_unknown_terminal_are_ignored(monkeypatch) -> None:
    _off_list(
        terminal_id="not-a-terminal",
        session_name="spoofed-session",
        subagent="evil",
        subagentRunId="terminal:evil",
        subagent_run_id="terminal:evil",
    )
    [record] = _refusals()
    assert record["terminal_id"] is None and record["session_name"] is None
    body, events = _stream(monkeypatch)
    [raw] = _raw_refusals(events)
    assert "subagentRunId" not in raw
    for spoof in ("terminal:evil", "not-a-terminal", "spoofed-session"):
        assert spoof not in body


def test_a_known_terminal_is_attributed_and_its_session_comes_from_cao(monkeypatch) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.terminal_exists", lambda tid: tid == "t-known"
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda tid: {"tmux_session": "cao-real"},
    )
    _off_list(terminal_id="t-known", session_name="spoofed-session", subagentRunId="x")
    [record] = _refusals()
    assert (record["terminal_id"], record["session_name"]) == ("t-known", "cao-real")
    _, events = _stream(monkeypatch)
    [raw] = _raw_refusals(events)
    assert raw["subagentRunId"] == "terminal:t-known"


def test_an_overlong_terminal_id_is_not_looked_up(monkeypatch) -> None:
    looked: List[str] = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.terminal_exists",
        lambda tid: looked.append(tid) or True,
    )
    _off_list(terminal_id="t" * 129)
    assert looked == []
    assert _refusals()[0]["terminal_id"] is None


def test_the_same_call_twice_publishes_once() -> None:
    _off_list()
    _off_list()
    assert len(_refusals()) == 1


def test_folded_refusals_reach_the_stream_as_a_count(monkeypatch) -> None:
    from cli_agent_orchestrator.services.agui import refusal

    clock = {"now": 0.0}
    monkeypatch.setattr(
        refusal, "_limiter", refusal.RefusalLimiter(clock=lambda: clock["now"], per_bucket=1)
    )
    _off_list("a")
    _off_list("b")
    _off_list("c")
    clock["now"] = 60.0
    _off_list("d")
    _, events = _stream(monkeypatch)
    raws = _raw_refusals(events)
    assert [r["event"].get("more_refused") for r in raws] == [None, 2]


def test_without_cao_write_nothing_is_published(monkeypatch) -> None:
    """Design point D: with auth on, the scope dependency answers before the
    handler runs, so a caller without cao:write gets 403 (not the 400) and no
    record is published. With cao:write the 400 and the record come back."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    app.dependency_overrides[auth.get_current_scopes] = lambda: [auth.SCOPE_READ]
    assert _off_list().status_code == 403
    assert _refusals() == []
    app.dependency_overrides[auth.get_current_scopes] = lambda: [auth.SCOPE_WRITE]
    assert _off_list().status_code == 400
    assert len(_refusals()) == 1


def test_a_reconnect_from_a_refusal_id_does_not_replay_the_history(monkeypatch) -> None:
    """Design point E: the refusal is in the event log, so a Last-Event-ID naming
    it resumes after it, instead of being unknown and replaying everything."""
    first = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": {"v": 1}})
    _off_list()
    refusal_id = _refusals()[0]["id"]
    later = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": {"v": 2}})
    _, events = _stream(monkeypatch, headers={"Last-Event-ID": refusal_id})
    seen = [e["metadata"]["cao"]["eventId"] for e in events if e["type"] in ("CUSTOM", "RAW")]
    assert seen == [later.json()["event_id"]]
    assert first.json()["event_id"] not in seen


def test_a_since_reconnect_does_not_duplicate_the_refusal(monkeypatch) -> None:
    _off_list()
    [record] = _refusals()
    later = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": {}})
    _, events = _stream(monkeypatch, since=record["timestamp"])
    assert _raw_refusals(events) == []
    assert [e["metadata"]["cao"]["eventId"] for e in events if e["type"] == "CUSTOM"] == [
        later.json()["event_id"]
    ]


def test_a_sustained_flood_holds_at_most_a_tenth_of_the_ring(monkeypatch) -> None:
    """FIX 3 (#1158 review): the event log is a 500-record ring shared with real
    events, and it is how far a since / Last-Event-ID replay reaches back. Over 30
    rate-limit windows the limiter alone would let 150 refusals in beside 450 real
    events, evicting 100 real ones. Refusals stop at 50 (folded into
    more_refused), so every real event stays replayable."""
    from cli_agent_orchestrator.services.agui import refusal
    from cli_agent_orchestrator.services.event_log_service import RING_CAPACITY

    clock = {"now": 0.0}
    monkeypatch.setattr(refusal, "_limiter", refusal.RefusalLimiter(clock=lambda: clock["now"]))
    real_ids: List[str] = []
    for window in range(30):
        clock["now"] = window * 10.0
        for i in range(5):
            assert _off_list(f"w{window}-{i}").status_code == 400
        for i in range(15):
            r = client.post("/agui/v1/emit_ui", json={"component": "metric", "props": {"i": i}})
            real_ids.append(r.json()["event_id"])
    history = event_log_service.get_event_log().history(limit=RING_CAPACITY)
    assert len(_refusals()) == RING_CAPACITY // 10
    assert [e["id"] for e in history if e["id"] in set(real_ids)] == real_ids
    _, events = _stream(monkeypatch)
    replayed = [e["metadata"]["cao"]["eventId"] for e in events if e["type"] == "CUSTOM"]
    assert replayed == real_ids
    raws = _raw_refusals(events)
    assert len(raws) == RING_CAPACITY // 10


def test_a_failing_terminal_lookup_still_answers_400(monkeypatch, caplog) -> None:
    def boom(_tid: str) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.terminal_exists", boom)
    caplog.set_level("WARNING")
    assert _off_list(terminal_id="t1").status_code == 400
    assert _refusals() == []
    assert "emit_ui: the refusal record was not published" in caplog.text


@pytest.mark.asyncio
async def test_a_10k_off_list_flood_loses_no_real_event_and_ends_no_stream(monkeypatch) -> None:
    """Design point A, through the route: a slow subscriber already holds 250 real
    events, then 10,000 off-list calls (varied names and terminal ids) arrive,
    then 6 more real ones. Published like a real event, the few refusals the
    limiter lets through would take the last slots and end the stream with
    RUN_ERROR; published lossily, they stop at the high-water mark and all 256
    real events fit."""
    bus = sse_bus.get_bus()
    sub = bus.register(overflow_close=True)
    real_ids: List[str] = []
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as ac:

        async def real(i: int) -> None:
            r = await ac.post("/agui/v1/emit_ui", json={"component": "progress", "props": {"i": i}})
            real_ids.append(r.json()["event_id"])
            # Let the bus deliver, as a server's loop does between requests (the
            # in-process transport never suspends). The consumer drains nothing.
            await asyncio.sleep(0)

        for i in range(250):
            await real(i)
        for i in range(10_000):
            r = await ac.post(
                "/agui/v1/emit_ui",
                json={"component": f"x{i}", "props": {"m": MARKER}, "terminal_id": f"t{i}"},
            )
            assert r.status_code == 400
            await asyncio.sleep(0)
        for i in range(250, 256):
            await real(i)
    assert len(_refusals()) > 0  # the limiter let some through, onto the bus
    queued: List[Dict[str, Any]] = []
    while not sub.queue.empty():
        queued.append(sub.queue.get_nowait())
    bus.unregister(sub)
    assert sub.overflowed is False
    assert sse_bus.OVERFLOW_SENTINEL not in queued
    assert [e["id"] for e in queued] == real_ids

    install(monkeypatch, live=queued)
    with client.stream("GET", "/agui/v1/stream") as r:
        body = "".join(r.iter_text())
    events = parse_sse(body)
    assert "RUN_ERROR" not in [e["type"] for e in events]
    assert len([e for e in events if e["type"] == "CUSTOM"]) == len(real_ids) == 256
    assert MARKER not in body
