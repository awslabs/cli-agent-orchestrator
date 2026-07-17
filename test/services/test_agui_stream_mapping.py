"""Tests for the CAO→AG-UI event mapping.


Privacy boundary asserted: message bodies are never carried on the
wire, even when the CAO event's payload includes them.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.services.agui_stream import (
    AGUI_RAW,
    AGUI_RUN_FINISHED,
    AGUI_RUN_STARTED,
    AGUI_STEP_FINISHED,
    AGUI_STEP_STARTED,
    AGUI_TEXT_MESSAGE_CONTENT,
    to_agui_event,
)


class TestRunStartedFinished:
    def test_session_created_maps_to_run_started(self) -> None:
        agui_type, data = to_agui_event(
            {
                "type": "session.created",
                "payload": {"session_name": "cao-foo"},
                "traceparent": "tp",
            }
        )
        assert agui_type == AGUI_RUN_STARTED
        assert data["thread_id"] == "cao-foo"
        assert data["run_id"] == "cao-foo"
        assert data["traceparent"] == "tp"

    def test_session_killed_maps_to_run_finished(self) -> None:
        agui_type, data = to_agui_event(
            {"type": "session.killed", "payload": {"session_name": "cao-foo"}}
        )
        assert agui_type == AGUI_RUN_FINISHED
        assert data["status"] == "terminated"


class TestStepStartedFinished:
    def test_terminal_created_maps_to_step_started(self) -> None:
        agui_type, data = to_agui_event(
            {
                "type": "terminal.created",
                "payload": {
                    "terminal_id": "abc12345",
                    "agent_name": "developer",
                    "provider": "claude_code",
                },
            }
        )
        assert agui_type == AGUI_STEP_STARTED
        assert data["step_id"] == "abc12345"
        assert data["step_name"] == "developer"
        assert data["provider"] == "claude_code"

    def test_terminal_killed_maps_to_step_finished(self) -> None:
        agui_type, data = to_agui_event(
            {
                "type": "terminal.killed",
                "payload": {"terminal_id": "abc12345", "agent_name": "developer"},
            }
        )
        assert agui_type == AGUI_STEP_FINISHED
        assert data["step_id"] == "abc12345"


class TestTextMessage:
    def test_message_sent_maps_to_text_message_content(self) -> None:
        agui_type, data = to_agui_event(
            {
                "type": "message.sent",
                "payload": {
                    "sender": "s",
                    "receiver": "r",
                    "orchestration_type": "handoff",
                },
            }
        )
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT
        assert data["role"] == "assistant"
        assert data["message_id"] == "r"
        # Privacy: never include the body.
        assert data["delta"] == ""

    def test_message_body_redacted_even_when_payload_includes_it(self) -> None:
        agui_type, data = to_agui_event(
            {
                "type": "message.sent",
                "payload": {
                    "sender": "s",
                    "receiver": "r",
                    "orchestration_type": "handoff",
                    "message": "SECRET — must not appear on the wire",
                },
            }
        )
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT
        # The body never appears in any field, not in delta and not in
        # metadata. Stringify the whole payload to be thorough.
        as_str = str(data)
        assert "SECRET" not in as_str
        assert "must not appear" not in as_str


class TestRaw:
    @pytest.mark.parametrize(
        "kind",
        [
            "custom.metric.updated",
            "anything.else.we.havent.mapped",
        ],
    )
    def test_unmapped_falls_back_to_raw(self, kind: str) -> None:
        agui_type, data = to_agui_event({"type": kind, "payload": {"terminal_id": "abc12345"}})
        assert agui_type == AGUI_RAW
        # RAW preserves the original semantics so the PWA's reducer
        # can dispatch on the cao_type field.
        assert data["cao_type"] == kind
        assert data["payload"]["terminal_id"] == "abc12345"


class TestNullSafety:
    def test_empty_event(self) -> None:
        agui_type, data = to_agui_event({})
        assert agui_type == AGUI_RAW
        assert data["cao_type"] == ""


# ---------------------------------------------------------------------------
# Re-based primitive path: map upstream's normalized six-primitive event
# records (the SseBus / EventLog shape) onto AG-UI typed events.
# ---------------------------------------------------------------------------

from cli_agent_orchestrator.services.agui_stream import (  # noqa: E402
    AGUI_RUN_ERROR,
    AGUI_STATE_DELTA,
    AGUI_TOOL_CALL_START,
)


def _record(kind: str, *, terminal_id=None, session_name=None, detail=None) -> dict:
    """Build an upstream-shaped normalized event record."""
    return {
        "id": "evt-1",
        "kind": kind,
        "terminal_id": terminal_id,
        "session_name": session_name,
        "timestamp": "2026-07-04T00:00:00+00:00",
        "detail": detail or {},
    }


class TestPrimitivePath:
    def test_launch_session_maps_to_run_started(self) -> None:
        agui_type, data = to_agui_event(
            _record("launch", session_name="cao-foo", detail={"event_type": "post_create_session"})
        )
        assert agui_type == AGUI_RUN_STARTED
        assert data["thread_id"] == "cao-foo"
        assert data["run_id"] == "cao-foo"
        assert data["event_id"] == "evt-1"

    def test_launch_terminal_maps_to_step_started(self) -> None:
        agui_type, data = to_agui_event(
            _record(
                "launch",
                terminal_id="abc12345",
                session_name="cao-foo",
                detail={
                    "event_type": "post_create_terminal",
                    "agent_name": "developer",
                    "provider": "claude_code",
                },
            )
        )
        assert agui_type == AGUI_STEP_STARTED
        assert data["step_id"] == "abc12345"
        assert data["step_name"] == "developer"
        assert data["provider"] == "claude_code"

    def test_completion_session_maps_to_run_finished(self) -> None:
        agui_type, data = to_agui_event(
            _record(
                "completion", session_name="cao-foo", detail={"event_type": "post_kill_session"}
            )
        )
        assert agui_type == AGUI_RUN_FINISHED
        assert data["status"] == "terminated"

    def test_completion_terminal_maps_to_step_finished(self) -> None:
        agui_type, data = to_agui_event(
            _record(
                "completion",
                terminal_id="abc12345",
                detail={"event_type": "post_kill_terminal", "agent_name": "developer"},
            )
        )
        assert agui_type == AGUI_STEP_FINISHED
        assert data["step_id"] == "abc12345"

    def test_handoff_maps_to_text_message_content_and_redacts_body(self) -> None:
        agui_type, data = to_agui_event(
            _record(
                "handoff",
                terminal_id="r",
                detail={"sender": "s", "receiver": "r", "orchestration_type": "handoff"},
            )
        )
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT
        assert data["role"] == "assistant"
        assert data["message_id"] == "r"
        assert data["delta"] == ""

    def test_a2a_delegation_maps_to_tool_call_start(self) -> None:
        agui_type, data = to_agui_event(
            _record(
                "a2a_delegation",
                detail={"sender": "s", "receiver": "r", "orchestration_type": "a2a_send"},
            )
        )
        assert agui_type == AGUI_TOOL_CALL_START
        assert data["tool_call_name"] == "a2a_delegation"

    def test_file_mod_maps_to_state_delta(self) -> None:
        agui_type, data = to_agui_event(
            _record("file_mod", terminal_id="t", detail={"path": "x.py"})
        )
        assert agui_type == AGUI_STATE_DELTA
        # A real RFC-6902 patch derived from the record (not an empty delta).
        assert isinstance(data["delta"], list) and len(data["delta"]) == 1
        op = data["delta"][0]
        assert op["op"] == "add"
        assert op["path"] == "/last_file_mod"
        assert op["value"]["path"] == "x.py"
        assert op["value"]["terminal_id"] == "t"

    def test_error_maps_to_run_error(self) -> None:
        agui_type, data = to_agui_event(_record("error", detail={"event_type": "boom"}))
        assert agui_type == AGUI_RUN_ERROR

    def test_other_falls_back_to_raw(self) -> None:
        agui_type, data = to_agui_event(
            _record("other", detail={"event_type": "post_pause_terminal"})
        )
        assert agui_type == AGUI_RAW
        assert data["cao_kind"] == "other"
        assert data["cao_type"] == "post_pause_terminal"

    def test_handoff_never_leaks_body_even_if_detail_has_one(self) -> None:
        # The publisher never puts bodies in detail, but assert defensively.
        agui_type, data = to_agui_event(
            _record("handoff", terminal_id="r", detail={"receiver": "r", "message": "SECRET-BODY"})
        )
        assert agui_type == AGUI_TEXT_MESSAGE_CONTENT
        assert "SECRET-BODY" not in str(data)


# ---------------------------------------------------------------------------
# Shared-state channel: STATE_SNAPSHOT on connect + STATE_DELTA on change.
# ---------------------------------------------------------------------------

from cli_agent_orchestrator.services.agui_stream import (  # noqa: E402
    AGUI_STATE_SNAPSHOT,
    state_delta_frame,
    state_snapshot_frame,
)
from cli_agent_orchestrator.services.ui_state_service import (  # noqa: E402
    build_dashboard_snapshot,
)


class TestStateChannel:
    def test_snapshot_frame_wraps_dashboard_snapshot(self) -> None:
        snap = build_dashboard_snapshot(
            sessions=[{"id": "cao-foo", "name": "cao-foo", "status": "running"}],
            terminals=[{"id": "t1", "tmux_session": "cao-foo", "provider": "claude_code"}],
            scopes=["cao:read"],
        )
        agui_type, data = state_snapshot_frame(snap)
        assert agui_type == AGUI_STATE_SNAPSHOT
        assert data["snapshot"]["counts"] == {"sessions": 1, "terminals": 1}
        assert data["snapshot"]["scopes"] == ["cao:read"]

    def test_delta_frame_none_when_unchanged(self) -> None:
        snap = build_dashboard_snapshot(sessions=[], terminals=[], scopes=None)
        assert state_delta_frame(snap, snap) is None

    def test_delta_frame_emits_rfc6902_ops_on_change(self) -> None:
        prev = build_dashboard_snapshot(sessions=[], terminals=[], scopes=None)
        curr = build_dashboard_snapshot(
            sessions=[{"id": "cao-foo", "name": "cao-foo", "status": "running"}],
            terminals=[],
            scopes=None,
        )
        frame = state_delta_frame(prev, curr)
        assert frame is not None
        agui_type, data = frame
        assert agui_type == AGUI_STATE_DELTA
        # A JSON-Patch op list that moves prev -> curr (sessions + counts changed).
        assert isinstance(data["delta"], list) and data["delta"]
        assert all("op" in op and "path" in op for op in data["delta"])


# ---------------------------------------------------------------------------
# Generative UI: agent-authored, allow-listed, safe-by-construction components
# rendered uniformly across heterogeneous CLI providers.
# ---------------------------------------------------------------------------

from cli_agent_orchestrator.services.agui_stream import (  # noqa: E402
    AGUI_GENERATIVE_UI,
    GENERATIVE_UI_COMPONENTS,
)


class TestGenerativeUI:
    def test_allow_listed_component_maps_to_generative_ui(self) -> None:
        agui_type, data = to_agui_event(
            {
                "id": "e1",
                "kind": "other",
                "terminal_id": "t1",
                "detail": {
                    "event_type": "post_agent_ui",
                    "ui": {
                        "component": "approval_card",
                        "props": {"title": "Approve handoff to security?", "risk": "medium"},
                    },
                },
            }
        )
        assert agui_type == AGUI_GENERATIVE_UI
        assert data["component"] == "approval_card"
        assert data["props"]["title"] == "Approve handoff to security?"
        assert data["terminal_id"] == "t1"

    def test_top_level_ui_intent_is_honored(self) -> None:
        agui_type, data = to_agui_event(
            {"id": "e2", "ui": {"component": "metric", "props": {"label": "tokens/s", "value": 42}}}
        )
        assert agui_type == AGUI_GENERATIVE_UI
        assert data["component"] == "metric"

    def test_unknown_component_is_refused_not_rendered(self) -> None:
        # An agent asking for an off-list component must NOT become GENERATIVE_UI.
        agui_type, data = to_agui_event(
            {"id": "e3", "ui": {"component": "iframe", "props": {"src": "http://evil"}}}
        )
        assert agui_type == AGUI_RAW
        assert data["rejected_component"] == "iframe"
        assert data["cao_kind"] == "generative_ui"

    def test_every_allow_listed_component_round_trips(self) -> None:
        for comp in GENERATIVE_UI_COMPONENTS:
            agui_type, data = to_agui_event({"id": "x", "ui": {"component": comp, "props": {}}})
            assert agui_type == AGUI_GENERATIVE_UI
            assert data["component"] == comp

    def test_non_serializable_props_degrade_to_empty(self) -> None:
        agui_type, data = to_agui_event(
            {"id": "e4", "ui": {"component": "progress", "props": {"cb": lambda: 1}}}
        )
        assert agui_type == AGUI_GENERATIVE_UI
        assert data["props"] == {}

    def test_non_dict_props_degrade_to_empty(self) -> None:
        # ``props`` that isn't a dict at all (here a string) must degrade to an
        # empty dict rather than crash the mapping.
        agui_type, data = to_agui_event(
            {"id": "e5", "ui": {"component": "progress", "props": "not-a-dict"}}
        )
        assert agui_type == AGUI_GENERATIVE_UI
        assert data["props"] == {}

    def test_oversized_props_are_truncated(self) -> None:
        big = {"blob": "x" * 20000}
        agui_type, data = to_agui_event(
            {"id": "e5", "ui": {"component": "diff_summary", "props": big}}
        )
        assert agui_type == AGUI_GENERATIVE_UI
        assert data["props"] == {"_truncated": True}
