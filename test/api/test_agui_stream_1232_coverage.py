"""The ``/agui/v1/stream`` route paths #1232's patch left untested (``api/main.py``).

Line numbers are ``main``'s at the time of writing; each test names the lines it reaches.
Every test drives the real route through ``TestClient`` with the in-memory event
log and bus from ``test/_support/agui_stream_harness``, so each stream ends
deterministically and no server or tmux is involved.
"""

from __future__ import annotations

import json
from test._support import agui_ambient_records as R
from test._support.agui_conformance import check_lifecycle, parse_sse, validate_frames
from test._support.agui_stream_harness import install
from typing import Any, Dict, Iterator, List

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api import main
from cli_agent_orchestrator.services.agui import ambient_run

client = TestClient(main.app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    ambient_run.reset_stop()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_session_incarnation",
        lambda name: f"inc-{name}",
    )
    yield
    ambient_run.reset_stop()


def _conformant(body: str) -> List[Dict[str, Any]]:
    events = parse_sse(body)
    validate_frames(events)
    check_lifecycle(events)
    return events


def _get(params: Dict[str, Any] = None, headers: Dict[str, str] = None) -> tuple:
    with client.stream("GET", "/agui/v1/stream", params=params or {}, headers=headers or {}) as r:
        return r.status_code, "".join(r.iter_text())


def _post(body: Dict[str, Any]) -> tuple:
    headers = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    with client.stream("POST", "/agui/v1/stream", content=json.dumps(body), headers=headers) as r:
        return r.status_code, "".join(r.iter_text())


RUN_INPUT = {
    "threadId": "thread-1",
    "runId": "run-1",
    "state": {},
    "messages": [],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


def test_a_sub_frame_last_event_id_for_an_unknown_record_replays_the_whole_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main.py:2332: no record has that id, so the replay falls through to ``after_id``,
    which (like the real event log) replays everything for an id it does not hold."""
    install(monkeypatch, history=[R.SESSION_LAUNCH, R.OTHER])
    code, body = _get(headers={"Last-Event-ID": "gone-record.0"})
    assert code == 200
    events = _conformant(body)
    started = [e for e in events if e["type"] == "SUBAGENT_STARTED"]
    assert [e["name"] for e in started] == [R.SESSION]  # the replayed session launch
    raw = [e for e in events if e["type"] == "RAW"]
    assert [e["event"]["kind"] for e in raw] == ["other"]


def test_a_failing_replay_lookup_still_streams_the_run(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """main.py:2336-2337: the replay is failure-isolated, as on the legacy path."""
    install(monkeypatch, live=[R.OTHER])

    class _BrokenLog:
        def history(self, **_: Any) -> List[Dict[str, Any]]:
            raise RuntimeError("event log unavailable")

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.event_log_service.get_event_log", lambda: _BrokenLog()
    )
    caplog.set_level("WARNING", logger=main.logger.name)
    code, body = _get({"since": "2026-11-04T09:59:59Z"})
    assert code == 200
    types = [e["type"] for e in _conformant(body)]
    assert types[0] == "RUN_STARTED" and types[-1] == "RUN_FINISHED"
    assert "RAW" in types  # the live record still arrives
    assert "agui_stream: history replay failed" in [r.getMessage() for r in caplog.records]


@pytest.mark.parametrize("value", [None, 2, 1.0, ["1", "0"]])
def test_a_protocol_version_that_is_not_a_string_is_uninterpretable(value: Any) -> None:
    """main.py:2706-2707. The POST route refuses a number earlier (422), so only a
    direct call reaches this guard."""
    assert main._agui_parse_protocol_version(value) is None


def test_post_stream_without_the_agui_extra_is_501(monkeypatch: pytest.MonkeyPatch) -> None:
    """main.py:2739-2740."""
    install(monkeypatch)
    monkeypatch.setattr("cli_agent_orchestrator.services.agui.run_plane.AG_UI_AVAILABLE", False)
    resp = client.post("/agui/v1/stream", json=RUN_INPUT)
    assert resp.status_code == 501
    assert "ag-ui-protocol is not installed" in resp.json()["detail"]


def test_post_stream_snapshot_skips_a_session_whose_terminal_listing_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """main.py:2833-2839: one session's listing error leaves the others in the snapshot."""
    install(
        monkeypatch,
        sessions=[{"id": "cao-ok", "name": "cao-ok"}, {"id": "cao-bad", "name": "cao-bad"}],
    )

    def list_terminals(session_id: str, **_: Any) -> List[Dict[str, Any]]:
        if session_id == "cao-bad":
            raise RuntimeError("tmux gone")
        return [{"id": "aaaa1111", "tmux_session": "cao-ok", "provider": "kiro_cli"}]

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_current_session_terminals",
        list_terminals,
    )
    code, body = _post(dict(RUN_INPUT, forwardedProps={"cao": {"maxSeconds": 1}}))
    assert code == 200
    (snapshot,) = [e for e in _conformant(body) if e["type"] == "STATE_SNAPSHOT"]
    assert [s["id"] for s in snapshot["snapshot"]["sessions"]] == ["cao-ok", "cao-bad"]
    assert [t["id"] for t in snapshot["snapshot"]["terminals"]] == ["aaaa1111"]
