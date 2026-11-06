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
    # No test reaches the real SessionLocal: by default CAO knows no terminal, and
    # a test that needs one names it with _known_terminals().
    _known_terminals(monkeypatch)
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


def test_killed_session_finishes_its_subagents_and_leaves_the_run_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A kill is SUBAGENT_FINISHED with no outcome plus metadata.cao.endReason
    # "killed". A stock 1.0 consumer reads an absent outcome as success (the 1.0
    # subagent outcome has only success and suspended), so "killed" is CAO's
    # extension; this test pins that shape and that the run itself stays open.
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


# ---------------------------------------------------------------------------
# #1232 review P3-1 / Note 3: only a terminal the run has open, or one CAO knows,
# owns an event. A label such as "operator" (MCP Apps operator tools) or
# "supervisor" (cao-mcp-server without CAO_TERMINAL_ID) is not a subagent.
# ---------------------------------------------------------------------------


def _known_terminals(monkeypatch: pytest.MonkeyPatch, *known: str) -> None:
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.terminal_exists",
        lambda terminal_id: terminal_id in known,
        raising=False,
    )


def _session_launch(rid: str, session: str, ts: str) -> Dict[str, Any]:
    return dict(R.SESSION_LAUNCH, id=rid, session_name=session, timestamp=ts)


def _terminal_launch(rid: str, session: str, tid: str, agent: str, ts: str) -> Dict[str, Any]:
    return dict(
        R.TERMINAL_A_LAUNCH,
        id=rid,
        session_name=session,
        terminal_id=tid,
        timestamp=ts,
        detail={"event_type": "post_create_terminal", "agent_name": agent, "provider": "kiro_cli"},
    )


def _assign(rid: str, session: str, sender: str, receiver: str, ts: str) -> Dict[str, Any]:
    return dict(
        R.HANDOFF,
        id=rid,
        session_name=session,
        terminal_id=None,
        timestamp=ts,
        detail={"sender": sender, "receiver": receiver, "orchestration_type": "assign"},
    )


def _session_kill(rid: str, session: str, ts: str) -> Dict[str, Any]:
    return dict(R.SESSION_KILL, id=rid, session_name=session, timestamp=ts)


def _no_tag_outside_its_invocation(events: List[Dict[str, Any]]) -> None:
    """Every subagentRunId is used only between its SUBAGENT_STARTED and FINISHED."""
    open_ids: set = set()
    finished: set = set()
    for i, e in enumerate(events):
        sid = e.get("subagentRunId")
        if e["type"] == "SUBAGENT_STARTED":
            assert sid not in finished, (i, e)
            open_ids.add(sid)
        elif e["type"] == "SUBAGENT_FINISHED":
            open_ids.discard(sid)
            finished.add(sid)
        elif sid is not None:
            assert sid in open_ids, f"event {i} {e['type']} tagged {sid}, not an open invocation"


@pytest.mark.parametrize("sender", ["operator", "supervisor"])
def test_a_sender_that_is_not_a_terminal_is_not_a_subagent(
    monkeypatch: pytest.MonkeyPatch, sender: str
) -> None:
    # The stream from the #1232 review: two sessions, an assign from a label into
    # each, and the first session ends between them.
    _known_terminals(monkeypatch, "abcd1234", "ef015678")
    install(
        monkeypatch,
        live=[
            _session_launch("p-1", "cao-a", "2026-11-04T10:00:00.000000+00:00"),
            _terminal_launch("p-2", "cao-a", "abcd1234", "dev", "2026-11-04T10:00:01.000000+00:00"),
            _assign("p-3", "cao-a", sender, "abcd1234", "2026-11-04T10:00:02.000000+00:00"),
            _session_kill("p-4", "cao-a", "2026-11-04T10:00:03.000000+00:00"),
            _session_launch("p-5", "cao-b", "2026-11-04T10:00:04.000000+00:00"),
            _terminal_launch("p-6", "cao-b", "ef015678", "rev", "2026-11-04T10:00:05.000000+00:00"),
            _assign("p-7", "cao-b", sender, "ef015678", "2026-11-04T10:00:06.000000+00:00"),
        ],
    )
    _, body = _get()
    events = _conformant(f"review-P3-1-{sender}", body)

    started = {e["subagentRunId"]: e for e in events if e["type"] == "SUBAGENT_STARTED"}
    assert f"terminal:{sender}" not in started, "a label was announced as a subagent"
    assert all(e.get("name") != "terminal" for e in started.values()), started
    _no_tag_outside_its_invocation(events)

    sessions = {e["name"]: sid for sid, e in started.items() if sid.startswith("session:")}
    calls = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert [c["toolCallId"] for c in calls] == ["p-3", "p-7"]
    # Kept on the receiver's session, and the sender is still visible to a client.
    assert [c.get("subagentRunId") for c in calls] == [sessions["cao-a"], sessions["cao-b"]]
    assert [c["metadata"]["cao"]["sender"] for c in calls] == [sender, sender]


def test_a_known_terminal_sender_owns_its_call_before_its_launch_is_seen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The fix must not drop real terminals: one CAO knows (launched before this
    # run's window) still owns its call.
    _known_terminals(monkeypatch, "cccc3333", "bbbb2222")
    install(
        monkeypatch,
        live=[
            R.SESSION_LAUNCH,
            R.TERMINAL_B_LAUNCH,
            _assign("k-1", R.SESSION, "cccc3333", "bbbb2222", "2026-11-04T10:00:03.000000+00:00"),
        ],
    )
    _, body = _get()
    events = _conformant("review-P3-1-known-sender", body)
    (call,) = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert call["subagentRunId"] == "terminal:cccc3333"
    assert "terminal:cccc3333" in {
        e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"
    }


def test_a_finished_terminal_owns_no_later_event(monkeypatch: pytest.MonkeyPatch) -> None:
    # P3-1 result 2: nothing may be tagged with an invocation after its
    # SUBAGENT_FINISHED, even when the sender is a real, known terminal.
    _known_terminals(monkeypatch, "aaaa1111", "bbbb2222")
    kill_a = dict(R.TERMINAL_B_KILL, id="f-1", terminal_id="aaaa1111")
    late = _assign("f-2", R.SESSION, "aaaa1111", "bbbb2222", "2026-11-04T10:00:12.000000+00:00")
    late_ui = dict(R.GENERATIVE_UI, id="f-3", terminal_id="aaaa1111")
    install(
        monkeypatch,
        live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, R.TERMINAL_B_LAUNCH, kill_a, late, late_ui],
    )
    _, body = _get()
    events = _conformant("review-P3-1-finished-sender", body)
    _no_tag_outside_its_invocation(events)
    (call,) = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert call["subagentRunId"].startswith("session:")
    assert call["metadata"]["cao"]["sender"] == "aaaa1111"


def test_a_session_owned_call_closes_before_its_session_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A call kept on a session (its sender is not a terminal) whose receiver is in
    # another session must still close before the owning session's
    # SUBAGENT_FINISHED, so it is never tagged with a finished invocation.
    _known_terminals(monkeypatch, "ef015678")
    install(
        monkeypatch,
        live=[
            _session_launch("x-1", "cao-a", "2026-11-04T10:00:00.000000+00:00"),
            _session_launch("x-2", "cao-b", "2026-11-04T10:00:01.000000+00:00"),
            _terminal_launch("x-3", "cao-b", "ef015678", "rev", "2026-11-04T10:00:02.000000+00:00"),
            _assign("x-4", "cao-a", "operator", "ef015678", "2026-11-04T10:00:03.000000+00:00"),
            _session_kill("x-5", "cao-a", "2026-11-04T10:00:04.000000+00:00"),
            dict(R.TERMINAL_B_KILL, id="x-6", session_name="cao-b", terminal_id="ef015678"),
        ],
    )
    _, body = _get()
    events = _conformant("review-P3-1-cross-session-call", body)
    _no_tag_outside_its_invocation(events)
    types = [(e["type"], e.get("toolCallId") or e.get("subagentRunId")) for e in events]
    end = types.index(("TOOL_CALL_END", "x-4"))
    session_a = next(
        e["subagentRunId"]
        for e in events
        if e["type"] == "SUBAGENT_STARTED" and e["name"] == "cao-a"
    )
    assert end < types.index(("SUBAGENT_FINISHED", session_a))


def test_a_failing_terminal_lookup_keeps_the_event_on_the_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A DB error in the "does CAO know this terminal" check must not break the
    # stream: the event stays on the receiver's session and the run continues.
    def _boom(terminal_id: str) -> bool:
        raise RuntimeError("database unavailable")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.terminal_exists", _boom)
    install(
        monkeypatch,
        live=[
            R.SESSION_LAUNCH,
            R.TERMINAL_B_LAUNCH,
            _assign("e-1", R.SESSION, "cccc3333", "bbbb2222", "2026-11-04T10:00:03.000000+00:00"),
            R.OTHER,
        ],
    )
    _, body = _get()
    events = _conformant("review-P3-1-lookup-error", body)
    assert "RUN_ERROR" not in _types(events)
    assert events[-1]["type"] == "RUN_FINISHED"
    session = next(
        e["subagentRunId"]
        for e in events
        if e["type"] == "SUBAGENT_STARTED" and e["name"] == R.SESSION
    )
    (call,) = [e for e in events if e["type"] == "TOOL_CALL_START"]
    assert call["subagentRunId"] == session
    assert "terminal:cccc3333" not in {
        e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"
    }
    assert "RAW" in _types(events)  # the record after the failed lookup still arrives


def test_emit_ui_for_an_unknown_terminal_announces_no_subagent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Review Note 3: emit_ui with a terminal_id CAO does not know.
    _known_terminals(monkeypatch)
    ui = dict(R.GENERATIVE_UI, id="u-1", terminal_id="deadbeef")
    install(monkeypatch, live=[R.SESSION_LAUNCH, ui])
    _, body = _get()
    events = _conformant("review-note3-emit-ui-unknown", body)
    assert "terminal:deadbeef" not in {
        e["subagentRunId"] for e in events if e["type"] == "SUBAGENT_STARTED"
    }
    (custom,) = [e for e in events if e["type"] == "CUSTOM" and e["name"] == "cao.generative_ui"]
    assert custom.get("subagentRunId") != "terminal:deadbeef"
    _no_tag_outside_its_invocation(events)


# ---------------------------------------------------------------------------
# #1232 review Note 1: mutants that survived the focused tests.
# ---------------------------------------------------------------------------


def test_a_killed_terminal_is_never_announced_again(monkeypatch: pytest.MonkeyPatch) -> None:
    relaunch = dict(R.TERMINAL_A_LAUNCH, id="m-2", timestamp="2026-11-04T10:00:12.000000+00:00")
    kill_a = dict(R.TERMINAL_B_KILL, id="m-1", terminal_id="aaaa1111")
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.TERMINAL_A_LAUNCH, kill_a, relaunch])
    _, body = _get()
    events = _conformant("review-note1-no-reannounce", body)
    starts = [
        e
        for e in events
        if e["type"] == "SUBAGENT_STARTED" and e["subagentRunId"] == "terminal:aaaa1111"
    ]
    assert len(starts) == 1


def test_a_record_in_both_the_replay_and_the_live_feed_is_narrated_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install(monkeypatch, history=[R.SESSION_LAUNCH, R.OTHER], live=[R.OTHER])
    _, body = _get({"since": "2026-11-04T09:00:00.000000+00:00"})
    events = _conformant("review-note1-replay-dedupe", body)
    raws = [e for e in events if e["type"] == "RAW"]
    assert len(raws) == 1, [e["type"] for e in events]


def test_a_stream_opened_after_the_exit_signal_closes_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # request_stop() from the server-exit path is sticky by default: a stream that
    # connects after it ends at once with server_stop, not when its source ends.
    install(monkeypatch, live=[R.SESSION_LAUNCH, R.OTHER])
    ambient_run.request_stop()
    _, body = _get()
    events = _conformant("review-note1-sticky-stop", body)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert events[-1]["metadata"]["cao"]["endReason"] == "server_stop"


def test_run_started_echoes_since_in_its_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    since = "2026-11-04T09:00:00.000000+00:00"
    install(monkeypatch, history=[R.SESSION_LAUNCH])
    _, body = _get({"since": since})
    events = _conformant("review-note1-since", body)
    assert events[0]["type"] == "RUN_STARTED"
    assert events[0]["metadata"]["cao"]["since"] == since


def test_cao_server_main_installs_the_exit_hook_before_uvicorn_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock, patch

    import uvicorn

    import cli_agent_orchestrator.api.main as main_module

    calls: List[str] = []
    monkeypatch.setattr(ambient_run, "install_uvicorn_exit_hook", lambda: calls.append("hook"))
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: calls.append("run"))
    with (
        patch.dict(os.environ),
        patch("argparse.ArgumentParser.parse_args") as parse_args,
    ):
        parse_args.return_value = MagicMock(agents_dir=None, host=None, port=None, terminal=None)
        main_module.main()
    assert calls == ["hook", "run"]


def test_the_lifespan_shutdown_signals_open_ambient_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: List[Dict[str, Any]] = []
    monkeypatch.setattr(ambient_run, "request_stop", lambda **kw: seen.append(kw))
    with TestClient(app, base_url="http://localhost"):
        assert seen == []
    assert seen == [{"sticky": False}]
