"""AG-UI 1.0 conformance of the ambient stream (``GET``/``POST /agui/v1/stream``).

One test (or more) per MUST row A-M1..A-M10 of the #1193 audit, plus the edge
cases the design review added. Every conformant stream a test produces is checked
by the Python pre-check (``test/_support/agui_conformance.py``) and, when
``CAO_AGUI_CAPTURE_DIR`` is set, written there as ``<test>.sse`` so the pinned
``@ag-ui/client`` ``verifyEvents`` gets the final word (``verify-ambient.mjs``).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

try:
    from cli_agent_orchestrator.services.agui import ambient_run
except ImportError:  # pragma: no cover - only before the conformant encoder existed (red run)
    ambient_run = None  # type: ignore[assignment]
from test._support import agui_ambient_records as R
from test._support.agui_conformance import check_lifecycle, parse_sse, validate_frames
from test._support.agui_stream_harness import install

client = TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def _isolate_stop_and_incarnations(monkeypatch: pytest.MonkeyPatch):
    if ambient_run is not None:
        ambient_run.reset_stop()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_session_incarnation",
        lambda name: f"inc-{name}",
    )
    yield
    if ambient_run is not None:
        ambient_run.reset_stop()


def _capture(name: str, body: str) -> None:
    out = os.environ.get("CAO_AGUI_CAPTURE_DIR")
    if out:
        Path(out).mkdir(parents=True, exist_ok=True)
        (Path(out) / f"{name}.sse").write_text(body, encoding="utf-8")


def _get(
    params: Optional[Dict[str, Any]] = None, headers: Optional[Dict[str, str]] = None
) -> tuple:
    with client.stream(
        "GET", "/agui/v1/stream", params=params or {}, headers=headers or {}
    ) as resp:
        return resp.status_code, "".join(resp.iter_text())


def _post(body: Any, headers: Optional[Dict[str, str]] = None) -> tuple:
    hdrs = {"Accept": "text/event-stream", "Content-Type": "application/json"}
    hdrs.update(headers or {})
    data = body if isinstance(body, str) else json.dumps(body)
    with client.stream("POST", "/agui/v1/stream", content=data, headers=hdrs) as resp:
        return resp.status_code, "".join(resp.iter_text())


def _conformant(name: str, body: str) -> List[Dict[str, Any]]:
    events = parse_sse(body)
    validate_frames(events)
    check_lifecycle(events)
    _capture(name, body)
    return events


def _types(events: List[Dict[str, Any]]) -> List[str]:
    return [e["type"] for e in events]


def _run_input(**extra: Any) -> Dict[str, Any]:
    body = {
        "threadId": "thread-app-1",
        "runId": "run-app-1",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    body.update(extra)
    return body


ALL = R.PRIMITIVE_RECORDS
SINCE = "2026-11-04T09:59:59Z"


# --- A-M1 ---------------------------------------------------------------------


def test_every_frame_carries_type_in_data(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, history=ALL[:6], live=ALL[6:])
    code, body = _get({"since": SINCE})
    assert code == 200
    events = _conformant("A-M1-get", body)
    assert all("type" in e for e in events)
    assert not re.search(r"(?m)^event:", body)


def test_every_frame_carries_type_in_data_on_post(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, history=ALL[:6], live=ALL[6:])
    code, body = _post(_run_input(forwardedProps={"cao": {"since": SINCE}}))
    assert code == 200
    events = _conformant("A-M1-post", body)
    assert all("type" in e for e in events)
    assert not re.search(r"(?m)^event:", body)


# --- A-M2 ---------------------------------------------------------------------


@pytest.mark.parametrize("replay", [True, False], ids=["with-replay", "no-replay"])
def test_stream_opens_with_run_started(monkeypatch: pytest.MonkeyPatch, replay: bool) -> None:
    install(monkeypatch, history=ALL[:6] if replay else [], live=ALL[6:8])
    code, body = _get({"since": SINCE} if replay else {})
    events = _conformant(f"A-M2-{'replay' if replay else 'plain'}", body)
    assert events[0]["type"] == "RUN_STARTED"
    types = _types(events)
    assert types.index("STATE_SNAPSHOT") > 0


# --- A-M3 ---------------------------------------------------------------------

SNAKE = (
    "thread_id",
    "run_id",
    "step_name",
    "step_id",
    "tool_call_id",
    "tool_call_name",
    "message_id",
    "event_id",
)


@pytest.mark.parametrize("record", ALL, ids=[r["id"] for r in ALL])
def test_required_fields_are_schema_names(
    monkeypatch: pytest.MonkeyPatch, record: Dict[str, Any]
) -> None:
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, record])
    code, body = _get()
    events = _conformant(f"A-M3-{record['id']}", body)
    for e in events:
        assert not (set(e) & set(SNAKE)), e


# --- A-M4 ---------------------------------------------------------------------


def test_timestamp_is_integer_epoch_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, live=[R.SESSION_LAUNCH])
    code, body = _get()
    events = _conformant("A-M4", body)
    started = [e for e in events if e["type"] == "SUBAGENT_STARTED"][0]
    assert isinstance(started["timestamp"], int)
    assert started["timestamp"] == 1793786400000  # 2026-11-04T10:00:00Z in ms


# --- A-M5 ---------------------------------------------------------------------


def test_two_sessions_do_not_open_two_runs(monkeypatch: pytest.MonkeyPatch) -> None:
    other = dict(R.SESSION_LAUNCH, id="evt-x1", session_name="cao-other")
    # A terminal launch seen before its session's launch still gets the session first.
    early_terminal = dict(
        R.TERMINAL_A_LAUNCH, id="evt-x2", session_name="cao-late", terminal_id="cccc3333"
    )
    install(monkeypatch, live=[R.SESSION_LAUNCH, other, early_terminal])
    code, body = _get()
    events = _conformant("A-M5", body)
    assert _types(events).count("RUN_STARTED") == 1
    started = [e for e in events if e["type"] == "SUBAGENT_STARTED"]
    late_session = next(i for i, e in enumerate(started) if e["name"] == "cao-late")
    late_terminal = next(
        i for i, e in enumerate(started) if e["subagentRunId"] == "terminal:cccc3333"
    )
    assert late_session < late_terminal
    assert started[late_terminal]["parentSubagentRunId"] == started[late_session]["subagentRunId"]


# --- A-M6 ---------------------------------------------------------------------


def test_send_message_emits_no_text_message(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.SEND_MESSAGE])
    code, body = _get()
    events = _conformant("A-M6", body)
    assert not [e for e in events if e["type"].startswith("TEXT_MESSAGE")]
    dispatched = [
        e for e in events if e["type"] == "CUSTOM" and e["name"] == "cao.message_dispatched"
    ]
    assert len(dispatched) == 1 and dispatched[0]["value"]["receiver"] == "bbbb2222"


# --- A-M7 ---------------------------------------------------------------------


def test_ids_are_never_reused(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, live=[R.SESSION_LAUNCH])
    _, first = _get({"threadId": "shared-thread"})
    install(monkeypatch, live=[R.SESSION_LAUNCH])
    _, second = _get({"threadId": "shared-thread"})
    a, b = _conformant("A-M7-a", first)[0], _conformant("A-M7-b", second)[0]
    assert a["threadId"] == b["threadId"] == "shared-thread"
    assert a["runId"] != b["runId"]


def test_relaunched_session_gets_a_new_subagent_run_id(monkeypatch: pytest.MonkeyPatch) -> None:
    incarnations = iter(["inc-1", "inc-2"])
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_session_incarnation",
        lambda name: next(incarnations),
    )
    relaunch = dict(R.SESSION_LAUNCH, id="evt-14", timestamp="2026-11-04T10:00:13.000000+00:00")
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.SESSION_KILL, relaunch])
    _, body = _get()
    events = _conformant("A-M7-relaunch", body)
    ids = [e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"]
    assert len(ids) == 2 and ids[0] != ids[1]
    calls = [e["toolCallId"] for e in events if e["type"] == "TOOL_CALL_START"]
    assert len(calls) == len(set(calls))


# --- A-M8 ---------------------------------------------------------------------


def test_killed_session_is_not_reported_as_success(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, R.SESSION_KILL],
    )
    _, body = _get()
    events = _conformant("A-M8-session-kill", body)
    kill_at = [
        i
        for i, e in enumerate(events)
        if e.get("metadata", {}).get("cao", {}).get("endReason") == "killed"
    ]
    finished = [events[i] for i in kill_at]
    assert [e["type"] for e in finished] == ["SUBAGENT_FINISHED"] * 3
    assert all("outcome" not in e for e in finished)
    assert finished[-1]["subagentRunId"].startswith("session:")  # children before the session
    # The session kill is not a RUN_FINISHED; the run itself is still open until the stream ends.
    assert _types(events[: kill_at[-1] + 1]).count("RUN_FINISHED") == 0


def test_killed_terminal_closes_its_tool_calls_first(monkeypatch: pytest.MonkeyPatch) -> None:
    kill_a = dict(R.TERMINAL_B_KILL, id="evt-15", terminal_id="aaaa1111")
    install(
        monkeypatch,
        live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, R.HANDOFF, kill_a],
    )
    _, body = _get()
    events = _conformant("A-M8-terminal-kill", body)
    types = _types(events)
    end_i = types.index("TOOL_CALL_END")
    fin_i = next(
        i
        for i, e in enumerate(events)
        if e["type"] == "SUBAGENT_FINISHED" and e["subagentRunId"] == "terminal:aaaa1111"
    )
    assert end_i < fin_i
    assert events[fin_i]["metadata"]["cao"]["endReason"] == "killed"


def _blocking_bus(monkeypatch: pytest.MonkeyPatch, live: List[Dict[str, Any]]) -> None:
    import cli_agent_orchestrator.api.main as main  # noqa: F401  (installs the env via harness)

    install(monkeypatch, live=live)

    class _Blocking:
        def register(self, overflow_close: bool = False) -> Any:
            return object()

        def unregister(self, sub: Any) -> None:
            pass

        async def drain(self, sub: Any):
            for event in live:
                yield event
            await asyncio.Event().wait()  # never ends on its own
            yield {}  # pragma: no cover

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _Blocking())


def test_server_stop_ends_run_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    _blocking_bus(monkeypatch, [R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.HANDOFF])
    # The hook cao-server's main() installs before uvicorn.run; restored afterwards.
    monkeypatch.setattr(uvicorn.Server, "handle_exit", uvicorn.Server.handle_exit)
    ambient_run.install_uvicorn_exit_hook()
    server = uvicorn.Server(uvicorn.Config(app))
    # TestClient only hands the body back once the app finishes, so the signal is
    # delivered the way uvicorn delivers SIGTERM: from another thread, while the
    # stream is blocked on a bus that never ends on its own.
    timer = threading.Timer(1.0, server.handle_exit, args=(signal.SIGTERM, None))
    timer.start()
    try:
        code, body = _get()
    finally:
        timer.cancel()
    assert server.should_exit  # uvicorn's own exit flag was set too
    events = _conformant("A-M8-server-stop", body)
    last = events[-1]
    assert last["type"] == "RUN_FINISHED" and last["outcome"] == {"type": "cancelled"}
    assert last["metadata"]["cao"]["endReason"] == "server_stop"
    assert "TOOL_CALL_END" in _types(events)  # the open call was closed first


def test_max_seconds_ends_run_cancelled(monkeypatch: pytest.MonkeyPatch) -> None:
    _blocking_bus(monkeypatch, [R.SESSION_LAUNCH])
    code, body = _get({"maxSeconds": 1})
    events = _conformant("A-M8-max-seconds", body)
    assert events[-1]["outcome"] == {"type": "cancelled"}
    assert events[-1]["metadata"]["cao"]["endReason"] == "max_seconds"


# --- A-M9 ---------------------------------------------------------------------


def test_terminal_error_is_subagent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        live=[
            R.SESSION_LAUNCH,
            R.TERMINAL_A_LAUNCH,
            R.TERMINAL_B_LAUNCH,
            R.TERMINAL_ERROR,
            R.GENERATIVE_UI,
        ],
    )
    _, body = _get()
    events = _conformant("A-M9-terminal-error", body)
    errors = [e for e in events if e["type"] == "SUBAGENT_ERROR"]
    assert len(errors) == 1 and errors[0]["subagentRunId"] == "terminal:bbbb2222"
    assert "RUN_ERROR" not in _types(events)
    assert _types(events).index("SUBAGENT_ERROR") < _types(events).index("CUSTOM")


def test_unattributed_error_is_custom_and_run_continues(monkeypatch: pytest.MonkeyPatch) -> None:
    session_error = dict(R.TERMINAL_ERROR, id="evt-16", terminal_id=None)
    install(monkeypatch, live=[R.SESSION_LAUNCH, session_error, R.OTHER])
    _, body = _get()
    events = _conformant("A-M9-unattributed-error", body)
    assert "RUN_ERROR" not in _types(events)
    types = _types(events)
    err_i = next(
        i for i, e in enumerate(events) if e["type"] == "CUSTOM" and e["name"] == "cao.error"
    )
    assert "RAW" in types[err_i + 1 :]  # the next record is still delivered: the run continues
    assert types[-1] == "RUN_FINISHED"


def test_overflow_ends_with_run_error_then_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH], overflowed=True)
    _, body = _get()
    events = _conformant("A-M9-overflow", body)
    assert events[-1]["type"] == "RUN_ERROR"
    assert events[-1]["code"] == "cao.stream_overflow"


# --- A-M10 --------------------------------------------------------------------


def test_run_started_declares_protocol_version(monkeypatch: pytest.MonkeyPatch) -> None:
    from ag_ui.core import PROTOCOL_VERSION

    install(monkeypatch)
    _, body = _get()
    events = _conformant("A-M10", body)
    assert events[0]["protocolVersion"] == PROTOCOL_VERSION


# --- added by the design review ---------------------------------------------------


def test_replayed_finish_of_pre_window_invocation_synthesizes_start(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, history=[R.TERMINAL_B_KILL])
    _, body = _get({"since": SINCE})
    events = _conformant("added-pre-window", body)
    lifecycle = [
        (e["type"], e["subagentRunId"]) for e in events if e["type"].startswith("SUBAGENT")
    ]
    assert lifecycle[:3] == [
        ("SUBAGENT_STARTED", lifecycle[0][1]),
        ("SUBAGENT_STARTED", "terminal:bbbb2222"),
        ("SUBAGENT_FINISHED", "terminal:bbbb2222"),
    ]
    assert lifecycle[0][1].startswith("session:")


def test_tool_call_events_agree_on_owner(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        live=[
            R.SESSION_LAUNCH,
            R.TERMINAL_A_LAUNCH,
            R.TERMINAL_B_LAUNCH,
            R.HANDOFF,
            R.A2A,
            R.TERMINAL_B_KILL,
        ],
    )
    _, body = _get()
    events = _conformant("added-tool-owner", body)
    owners: Dict[str, Optional[str]] = {}
    for e in events:
        if e["type"] == "TOOL_CALL_START":
            owners[e["toolCallId"]] = e.get("subagentRunId")
        if e["type"] == "TOOL_CALL_END":
            assert e.get("subagentRunId") == owners[e["toolCallId"]]
    assert owners  # premise: there were tool calls


def _walk(value: Any):
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


def test_no_null_subagent_run_id_anywhere(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, history=ALL[:6], live=ALL[6:])
    _, body = _get({"since": SINCE})
    events = _conformant("added-no-null", body)
    for e in events:
        for k, v in _walk(e):
            if k == "subagentRunId":
                assert isinstance(v, str), e


def test_parent_announced_before_child(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, live=[R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, R.SESSION_LAUNCH])
    _, body = _get()
    events = _conformant("added-parent-first", body)
    seen = set()
    for e in events:
        if e["type"] == "SUBAGENT_STARTED":
            parent = e.get("parentSubagentRunId")
            assert parent is None or parent in seen, e
            seen.add(e["subagentRunId"])


def test_reconnect_is_new_run_same_thread_keeps_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, history=ALL[:6], live=[])
    _, first = _get({"threadId": "t-reconnect"})
    install(monkeypatch, history=ALL[:6], live=[])
    _, second = _get({"threadId": "t-reconnect"}, headers={"Last-Event-ID": "evt-03"})
    a = _conformant("added-reconnect-a", first)
    b = _conformant("added-reconnect-b", second)
    assert a[0]["threadId"] == b[0]["threadId"] == "t-reconnect"
    assert a[0]["runId"] != b[0]["runId"]
    assert re.search(r"(?m)^id: evt-04$", second)  # replayed record frames keep their id: line
    assert "evt-02" not in second.split("evt-04")[0]  # replay started after the cursor


def test_a2a_delegation_emits_no_tool_call_result(monkeypatch: pytest.MonkeyPatch) -> None:
    install(
        monkeypatch,
        live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, R.A2A, R.TERMINAL_B_KILL],
    )
    _, body = _get()
    events = _conformant("added-no-result", body)
    calls = [e["type"] for e in events if e["type"].startswith("TOOL_CALL")]
    assert calls == ["TOOL_CALL_START", "TOOL_CALL_END"]


def test_post_echoes_run_id_and_reads_protocol_version(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    install(monkeypatch)
    code, body = _post(_run_input(protocolVersion="1.0"))
    events = _conformant("added-post-echo", body)
    assert code == 200
    assert events[0]["threadId"] == "thread-app-1" and events[0]["runId"] == "run-app-1"
    assert events[-1]["type"] == "RUN_FINISHED" and events[-1]["runId"] == "run-app-1"

    # versioning#version-negotiation: a producer MAY reject only a declaration from
    # a major line it does not implement, and the value is the published
    # MAJOR.MINOR identifier. Only a well-formed MAJOR.MINOR of another major is
    # refused.
    for refused in ("2.0", "0.9"):
        install(monkeypatch)
        code, _ = _post(_run_input(protocolVersion=refused))
        assert code == 400, refused

    # Anything that is not MAJOR.MINOR cannot be interpreted, and is handled like a
    # newer declaration: proceed and warn (corpus
    # producer-declares-uninterpretable-version-warns). That includes "2" and
    # "2.0.0", and a number too long to convert.
    for served, name in (
        ("1.9", "newer-minor"),
        ("not-a-version", "unparseable"),
        ("draft", "draft"),
        ("2", "major-only"),
        ("2.0.0", "three-part-other-major"),
        ("1.0.0", "patch-form"),
        ("9" * 5000 + ".0", "overlong-major"),
    ):
        install(monkeypatch)
        caplog.clear()
        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.api.main"):
            code, body = _post(_run_input(protocolVersion=served))
        assert code == 200, served
        _conformant(f"added-post-version-{name}", body)
        assert any("protocolVersion" in r.getMessage() for r in caplog.records), served


def test_post_rejects_resume_and_bad_input(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch)
    code, text = _post(
        _run_input(
            resume=[{"interruptId": "i-1", "status": "resolved", "payload": {"approved": True}}]
        )
    )
    assert code == 400 and "/agui/v1/run" in text
    install(monkeypatch)
    code, _ = _post("{not json")
    assert code == 422
    install(monkeypatch)
    code, _ = _post({"threadId": "t"})  # missing required fields
    assert code == 422
    install(monkeypatch)
    code, _ = _post(_run_input(forwardedProps={"cao": {"maxSeconds": 0}}))
    assert code == 400
    install(monkeypatch)
    code, _ = _post(_run_input(forwardedProps={"cao": {"since": "yesterday"}}))
    assert code == 400


def test_post_with_input_state_drops_replayed_deltas_before_the_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, history=[R.SESSION_LAUNCH, R.TERMINAL_B_LAUNCH, R.FILE_MOD])
    _, body = _post(_run_input(state={"app": 1}, forwardedProps={"cao": {"since": SINCE}}))
    events = _conformant("added-post-state", body)
    snap = _types(events).index("STATE_SNAPSHOT")
    assert "STATE_DELTA" not in _types(events)[:snap]


def test_exit_hook_is_idempotent_and_keeps_uvicorns_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn.Server, "handle_exit", uvicorn.Server.handle_exit)
    ambient_run.install_uvicorn_exit_hook()
    first = uvicorn.Server.handle_exit
    ambient_run.install_uvicorn_exit_hook()
    assert uvicorn.Server.handle_exit is first  # not wrapped twice
    server = uvicorn.Server(uvicorn.Config(app))
    server.handle_exit(signal.SIGTERM, None)
    assert server.should_exit  # uvicorn's own handler still ran


def test_sub_frame_cursor_replays_that_record_whole(monkeypatch: pytest.MonkeyPatch) -> None:
    # A record that maps to several frames gives the earlier ones `<id>.<i>` ids
    # and the last one `<id>`. A client that dropped between them reconnects
    # with `<id>.0`; the new run replays that record whole, not the entire
    # buffer and not only what follows it.
    history = [R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.HANDOFF, R.TERMINAL_B_KILL]
    install(monkeypatch, history=history)
    _, full = _get({"since": SINCE})
    sub_ids = re.findall(r"(?m)^id: (\S+\.\d+)$", full)
    assert sub_ids, "premise: some record maps to more than one frame"
    cursor = sub_ids[0]
    base = cursor.rsplit(".", 1)[0]
    install(monkeypatch, history=history)
    _, body = _get(headers={"Last-Event-ID": cursor})
    _conformant("added-sub-frame-cursor", body)
    replayed = re.findall(r"(?m)^id: ([^.\s]+)", body)
    ordered = [r["id"] for r in history]
    assert replayed[0] == base
    assert set(replayed) == set(ordered[ordered.index(base) :])


def test_max_seconds_holds_on_a_bus_that_never_drains(monkeypatch: pytest.MonkeyPatch) -> None:
    # A bus that always has a record queued never lets the wait time out, so the
    # deadline must be checked on every pass, not only on an idle one. The bus
    # gives up after 3 s so that, without the check, the run ends source_ended
    # (and this test fails) instead of hanging.
    import time

    install(monkeypatch)

    class _Busy:
        def register(self, overflow_close: bool = False) -> Any:
            return object()

        def unregister(self, sub: Any) -> None:
            pass

        async def drain(self, sub: Any):
            end = time.monotonic() + 3.0
            n = 0
            while time.monotonic() < end:
                n += 1
                yield dict(R.OTHER, id=f"busy-{n}")  # always ready: no idle pass

    monkeypatch.setattr("cli_agent_orchestrator.services.sse_bus.get_bus", lambda: _Busy())
    started = time.monotonic()
    _, body = _get({"maxSeconds": 1})
    elapsed = time.monotonic() - started
    events = _conformant("review-F3-busy-max-seconds", body)
    assert events[-1]["outcome"] == {"type": "cancelled"}
    assert events[-1]["metadata"]["cao"]["endReason"] == "max_seconds"
    assert elapsed < 2.5, elapsed


def test_request_stop_is_reentrant_on_the_lock_holding_thread() -> None:
    # request_stop() runs inside uvicorn's signal handler, on the loop thread,
    # which may already hold the waiters lock (registering or unregistering a
    # stream). A non-reentrant lock would deadlock the server's shutdown there.
    done = threading.Event()

    def holder() -> None:
        with ambient_run._waiters_lock:
            ambient_run.request_stop(sticky=False)
        done.set()

    t = threading.Thread(target=holder, daemon=True)
    t.start()
    t.join(2.0)
    assert done.is_set(), "request_stop deadlocked while its thread held the waiters lock"
