"""AG-UI event-stream adapter for CAO.


AG-UI clients (CopilotKit, the AG-UI Dojo, or a plain ``EventSource``) consume
``GET /agui/v1/stream`` as an SSE stream of AG-UI typed events.

Semantic event vocabulary
=========================
Upstream normalizes every internal lifecycle event to one of six semantic
primitives (``services/event_primitives.py``) before it hits the ``SseBus`` /
``EventLog`` ring buffer. The canonical record the bus fans out looks like::

    {"id": "...", "kind": "launch", "terminal_id": "...",
     "session_name": "...", "timestamp": "...", "detail": {...}}

where ``kind`` is one of ``launch | handoff | a2a_delegation | file_mod |
completion | error`` (or the ``other`` pass-through sentinel). This adapter
maps that closed primitive vocabulary onto AG-UI typed events. Mapping by the
*normalized primitive* (rather than raw event-type strings) is the correct L1
layer: it inherits the privacy boundary and total-ness of ``normalize_kind``,
and a change to CAO's internal event names never ripples into the AG-UI wire.

Primitive → AG-UI map (L1):

| CAO primitive (kind) | AG-UI type                       | Disambiguation            |
|----------------------|----------------------------------|---------------------------|
| launch (session)     | RUN_STARTED                      | terminal_id is None       |
| launch (terminal)    | STEP_STARTED                     | terminal_id present       |
| completion (session) | RUN_FINISHED                     | terminal_id is None       |
| completion (terminal)| STEP_FINISHED                    | terminal_id present       |
| handoff              | TEXT_MESSAGE_CONTENT (empty delta)| message dispatch          |
| a2a_delegation       | TOOL_CALL_START                  | cross-agent A2A task       |
| file_mod             | STATE_DELTA                      | (RFC-6902 patch: see note)|
| error                | RUN_ERROR                        |                           |
| other / unknown      | RAW                              | reducer dispatches on kind|

Backward compatibility: a legacy envelope shape ``{"type": "session.created",
"payload": {...}}`` (an earlier SSE bus shape emitted dotted names) is still
accepted and routed through the legacy mapping, so nothing that emits the old
shape breaks. The dispatcher picks the primitive path whenever the record
carries a top-level ``kind`` in the closed vocabulary.

Privacy boundary: message bodies are NEVER carried on the wire (same contract
as the ``EventLog`` / ``SseBus``, which store metadata only). ``handoff`` emits
an empty ``delta``; the client renders metadata only.

Note (file_mod → STATE_DELTA): each ``file_mod`` record is mapped to a real
RFC-6902 patch (an ``add`` op on ``/last_file_mod``) derived from the record's
own metadata, so the client's shared state reflects the change. The fleet-wide
``STATE_SNAPSHOT`` / ``STATE_DELTA`` channel (session/terminal topology) is
computed separately by ``services/ui_state_service.py`` and emitted by the
stream endpoint after each event. Debouncing high-rate file churn is a
follow-up at the stream layer.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Tuple

from cli_agent_orchestrator.services.ui_state_service import diff_snapshot

# AG-UI typed-event names. Pinned at the v1 spec families — when AG-UI evolves,
# the mapping is the one-file change.
AGUI_RUN_STARTED = "RUN_STARTED"
AGUI_RUN_FINISHED = "RUN_FINISHED"
AGUI_RUN_ERROR = "RUN_ERROR"
AGUI_STEP_STARTED = "STEP_STARTED"
AGUI_STEP_FINISHED = "STEP_FINISHED"
AGUI_TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
AGUI_TOOL_CALL_START = "TOOL_CALL_START"
AGUI_STATE_DELTA = "STATE_DELTA"
AGUI_STATE_SNAPSHOT = "STATE_SNAPSHOT"
AGUI_GENERATIVE_UI = "GENERATIVE_UI"
AGUI_RAW = "RAW"

# ---------------------------------------------------------------------------
# Generative UI — the safety allow-list
# ---------------------------------------------------------------------------
#
# CAO's differentiator: a *heterogeneous* fleet (Claude Code, Kiro, Codex,
# Cursor, ...) can each author a UI intent, and CAO renders it *uniformly* on
# one surface. The safety model that makes this shippable — and distinct from
# MCP Apps' arbitrary-HTML-in-an-iframe approach — is that agents may only emit
# a *closed vocabulary of named components with JSON props*. There is no HTML,
# no script, no eval on the wire. An unknown component name is refused (mapped
# to RAW with a rejection marker) rather than rendered. This is what lets an
# untrusted CLI agent drive UI without an iframe sandbox.
GENERATIVE_UI_COMPONENTS = frozenset(
    {
        "approval_card",  # a request the operator can approve/reject (handoff, destructive op)
        "choice_prompt",  # a bounded multiple-choice question from an agent
        "diff_summary",  # a compact file-change summary (paths + +/- counts; no bodies)
        "progress",  # a determinate/indeterminate progress indicator for a long step
        "metric",  # a single labelled metric (tokens, latency, cost)
        "agent_card",  # a compact agent identity/status card
    }
)

# Maximum serialized size of a generative-UI props payload (defense-in-depth
# against an agent emitting a huge blob onto the fan-out bus).
_MAX_GENERATIVE_PROPS_BYTES = 8 * 1024

# The closed primitive vocabulary CAO's normalizer emits (see
# services/event_primitives.py: PRIMITIVES + the "other" sentinel). A record
# carrying a top-level ``kind`` in this set is routed through the primitive
# mapping; anything else falls back to the legacy dotted-name mapping.
_PRIMITIVE_KINDS = frozenset(
    {"launch", "handoff", "a2a_delegation", "file_mod", "completion", "error", "other"}
)


def _base(event: Dict[str, Any], detail: Dict[str, Any]) -> Dict[str, Any]:
    """Common envelope fields carried on every primitive-path AG-UI event."""
    return {
        "event_id": event.get("id"),
        "terminal_id": event.get("terminal_id"),
        "session_name": event.get("session_name"),
        "timestamp": event.get("timestamp"),
        # traceparent may ride on the record or inside detail (OTel context).
        "traceparent": event.get("traceparent") or detail.get("traceparent"),
    }


def _from_primitive(event: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Map a normalized event record (by ``kind``) to AG-UI."""
    kind = event.get("kind", "other")
    detail: Dict[str, Any] = event.get("detail") or {}
    event_type = detail.get("event_type", "")
    is_session = event.get("terminal_id") is None
    data = _base(event, detail)

    if kind == "launch":
        if is_session or event_type == "post_create_session":
            data.update(thread_id=event.get("session_name"), run_id=event.get("session_name"))
            return AGUI_RUN_STARTED, data
        data.update(
            step_id=event.get("terminal_id"),
            step_name=detail.get("agent_name"),
            provider=detail.get("provider"),
        )
        return AGUI_STEP_STARTED, data

    if kind == "completion":
        if is_session or event_type == "post_kill_session":
            data.update(
                thread_id=event.get("session_name"),
                run_id=event.get("session_name"),
                status="terminated",
            )
            return AGUI_RUN_FINISHED, data
        data.update(step_id=event.get("terminal_id"), step_name=detail.get("agent_name"))
        return AGUI_STEP_FINISHED, data

    if kind == "handoff":
        # Message dispatch between agents. Body is intentionally redacted — the
        # detail is metadata-only (routing) by the EventLogPublisher contract.
        data.update(
            message_id=detail.get("receiver"),
            role="assistant",
            delta="",
            metadata={
                "sender": detail.get("sender"),
                "receiver": detail.get("receiver"),
                "orchestration_type": detail.get("orchestration_type"),
            },
        )
        return AGUI_TEXT_MESSAGE_CONTENT, data

    if kind == "a2a_delegation":
        data.update(
            tool_call_id=event.get("id"),
            tool_call_name="a2a_delegation",
            metadata={
                "sender": detail.get("sender"),
                "receiver": detail.get("receiver"),
                "orchestration_type": detail.get("orchestration_type"),
            },
        )
        return AGUI_TOOL_CALL_START, data

    if kind == "file_mod":
        # Emit a real RFC-6902 patch derived from the record so a client's
        # shared state reflects the change. ``add`` on an object member is
        # valid whether or not the key already exists (RFC-6902 §4.1), so this
        # is safe against any STATE_SNAPSHOT shape. High-rate file churn can be
        # debounced/coalesced at the stream layer as a follow-up.
        op = {
            "op": "add",
            "path": "/last_file_mod",
            "value": {
                "path": detail.get("path") or detail.get("file") or detail.get("file_path"),
                "terminal_id": event.get("terminal_id"),
                "session_name": event.get("session_name"),
                "timestamp": event.get("timestamp"),
            },
        }
        data.update(delta=[op], metadata=detail)
        return AGUI_STATE_DELTA, data

    if kind == "error":
        data.update(
            message=detail.get("event_type", "error"),
            metadata={k: v for k, v in detail.items() if k != "message"},
        )
        return AGUI_RUN_ERROR, data

    # "other" and any unmapped kind: RAW preserves original semantics so the
    # client reducer can dispatch on cao_kind / cao_type.
    data.update(cao_kind=kind, cao_type=event_type, detail=detail)
    return AGUI_RAW, data


def _from_legacy(event: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Legacy mapping for the original dotted-name envelope shape.

    Kept for backward compatibility with any producer emitting
    ``{"type": "session.created", "payload": {...}, "traceparent": "..."}``.
    """
    event_type = event.get("type", "")
    payload = event.get("payload") or {}
    traceparent = event.get("traceparent")

    if event_type == "session.created":
        return AGUI_RUN_STARTED, {
            "thread_id": payload.get("session_name"),
            "run_id": payload.get("session_name"),
            "traceparent": traceparent,
        }
    if event_type == "session.killed":
        return AGUI_RUN_FINISHED, {
            "thread_id": payload.get("session_name"),
            "run_id": payload.get("session_name"),
            "status": "terminated",
            "traceparent": traceparent,
        }
    if event_type == "terminal.created":
        return AGUI_STEP_STARTED, {
            "step_id": payload.get("terminal_id"),
            "step_name": payload.get("agent_name"),
            "provider": payload.get("provider"),
            "traceparent": traceparent,
        }
    if event_type == "terminal.killed":
        return AGUI_STEP_FINISHED, {
            "step_id": payload.get("terminal_id"),
            "step_name": payload.get("agent_name"),
            "traceparent": traceparent,
        }
    if event_type == "message.sent":
        return AGUI_TEXT_MESSAGE_CONTENT, {
            "message_id": payload.get("receiver"),
            "role": "assistant",
            "delta": "",
            "metadata": {
                "sender": payload.get("sender"),
                "receiver": payload.get("receiver"),
                "orchestration_type": payload.get("orchestration_type"),
            },
            "traceparent": traceparent,
        }
    return AGUI_RAW, {
        "cao_type": event_type,
        "payload": payload,
        "traceparent": traceparent,
    }


def _extract_ui_intent(event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the agent-authored UI intent block, if this event carries one.

    A generative-UI intent may ride at the top level (``event["ui"]``) or inside
    the normalized ``detail`` (``event["detail"]["ui"]``). It must be a mapping
    with a ``component`` key to be considered a UI intent.
    """
    for candidate in (event.get("ui"), (event.get("detail") or {}).get("ui")):
        if isinstance(candidate, dict) and candidate.get("component"):
            return candidate
    return None


def _from_generative_ui(event: Dict[str, Any], ui: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Map an agent-authored UI intent to a safe AG-UI GENERATIVE_UI frame.

    Safety is the whole point (see GENERATIVE_UI_COMPONENTS): only an
    allow-listed component name with JSON props is ever emitted. An unknown
    component is *refused* — routed to RAW with a ``rejected_component`` marker
    — never rendered. Props are validated to be JSON-serializable and bounded
    in size; a non-conforming payload is dropped to an empty mapping so a
    malformed intent degrades to an empty (harmless) component rather than
    breaking the stream.
    """
    detail: Dict[str, Any] = event.get("detail") or {}
    component = ui.get("component")
    base = _base(event, detail)

    if component not in GENERATIVE_UI_COMPONENTS:
        # Refuse: never render an unknown/unsafe component. Preserve the intent
        # under RAW so a client could log/inspect it, but do not treat it as UI.
        base.update(cao_kind="generative_ui", rejected_component=component)
        return AGUI_RAW, base

    props = ui.get("props")
    if not isinstance(props, dict):
        props = {}
    else:
        try:
            encoded = json.dumps(props)
            if len(encoded.encode("utf-8")) > _MAX_GENERATIVE_PROPS_BYTES:
                props = {"_truncated": True}
        except (TypeError, ValueError):
            # Non-serializable props => drop to empty (never crash the stream).
            props = {}

    base.update(component=component, props=props)
    return AGUI_GENERATIVE_UI, base


def to_agui_event(event: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Translate one CAO event record to an AG-UI ``(type, data)`` pair.

    Dispatches on shape:

    - **Generative UI first.** If the record carries an agent-authored UI intent
      (``ui.component``, top-level or in ``detail``), map it to a safe,
      allow-listed ``GENERATIVE_UI`` frame (unknown components are refused).
    - If the record carries a top-level ``kind`` in the closed six-primitive
      vocabulary (the ``SseBus`` / ``EventLog`` record), it is mapped
      via the primitive path — the normal, re-based path.
    - Otherwise the record is treated as the legacy dotted-name envelope and
      routed through the legacy mapping (backward compatibility).

    Privacy: message bodies are NEVER included in the returned payload.
    """
    ui = _extract_ui_intent(event)
    if ui is not None:
        return _from_generative_ui(event, ui)
    if event.get("kind") in _PRIMITIVE_KINDS:
        return _from_primitive(event)
    return _from_legacy(event)


# ---------------------------------------------------------------------------
# Shared-state channel (AG-UI STATE_SNAPSHOT / STATE_DELTA)
# ---------------------------------------------------------------------------
#
# AG-UI's shared-state feature lets a client hold a live projection of the
# agent/fleet state and keep it current via minimal RFC-6902 patches. CAO's
# authoritative projection + diff already exist as pure functions in
# ``services/ui_state_service.py`` (``build_dashboard_snapshot`` /
# ``diff_snapshot``); these two frames wrap them in AG-UI SSE envelopes so the
# ``/agui/v1/stream`` endpoint can emit a full ``STATE_SNAPSHOT`` on connect and
# incremental ``STATE_DELTA`` patches as the fleet changes. Keeping the framing
# here (not in the endpoint) keeps it pure and unit-testable.


def state_snapshot_frame(snapshot: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
    """Wrap a ``DashboardSnapshot`` as an AG-UI ``STATE_SNAPSHOT`` frame."""
    return AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}


def state_delta_frame(
    prev: Dict[str, Any], curr: Dict[str, Any]
) -> Optional[Tuple[str, Dict[str, Any]]]:
    """Wrap the ``prev -> curr`` change as an AG-UI ``STATE_DELTA`` frame.

    Returns ``None`` when the snapshots are equal (no RFC-6902 ops), so the
    caller emits nothing on the wire for a no-op tick.
    """
    ops: List[Dict[str, Any]] = diff_snapshot(prev, curr)
    if not ops:
        return None
    return AGUI_STATE_DELTA, {"delta": ops}


__all__ = [
    "AGUI_RAW",
    "AGUI_RUN_ERROR",
    "AGUI_RUN_FINISHED",
    "AGUI_RUN_STARTED",
    "AGUI_STATE_DELTA",
    "AGUI_STATE_SNAPSHOT",
    "AGUI_STEP_FINISHED",
    "AGUI_GENERATIVE_UI",
    "AGUI_STEP_STARTED",
    "AGUI_TEXT_MESSAGE_CONTENT",
    "AGUI_TOOL_CALL_START",
    "GENERATIVE_UI_COMPONENTS",
    "state_delta_frame",
    "state_snapshot_frame",
    "to_agui_event",
]
