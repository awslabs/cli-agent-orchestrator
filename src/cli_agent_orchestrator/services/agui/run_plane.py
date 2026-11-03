"""AG-UI run plane: POST /agui/v1/run with stock wire dialect.

Translates CAO's internal event vocabulary into the official ag-ui-protocol SDK
event models and streams them using EventEncoder (data:-only camelCase frames).

When the ``ag-ui-protocol`` package is not installed the module exposes
``AG_UI_AVAILABLE = False`` and the route returns HTTP 501 with an install hint.

Interrupt lifecycle:
- If open interrupts exist, emits STATE_SNAPSHOT then RUN_FINISHED with an
  ``interrupt`` outcome and closes the stream.
- If ``resume[]`` is provided in RunAgentInput, each entry is resolved through
  the idempotent approval registry before streaming begins.
- A resume entry for an unknown or expired interrupt is skipped with a
  server-side warning and the run proceeds; RUN_FINISHED lists it in
  ``result.skippedResume``. A resume that fails (ambiguous
  payload, delivery failure) produces RUN_ERROR. Interrupts still open after
  resume processing end the run with the ``interrupt`` outcome; none left ends
  it with ``success``. The body closes right after RUN_FINISHED or RUN_ERROR.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from typing import Any, AsyncGenerator, Dict, List, Optional

from cli_agent_orchestrator.services.agui_stream import CAO_GENERATIVE_UI_EVENT_NAME

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Conditional ag-ui-protocol import
# ---------------------------------------------------------------------------

AG_UI_AVAILABLE = False

try:
    # The SDK's own declaration of the spec version it was generated from
    # ("1.0" for ag-ui-protocol 1.0.x). AG-UI 1.0 requires a producer to send
    # it on RUN_STARTED, and the SDK leaves RunStartedEvent.protocol_version
    # unset by default, so the run plane passes it explicitly.
    from ag_ui.core import PROTOCOL_VERSION
    from ag_ui.core.events import (
        CustomEvent,
    )
    from ag_ui.core.events import Interrupt as AgUiInterrupt
    from ag_ui.core.events import (
        RunAgentInput,
        RunErrorEvent,
        RunFinishedEvent,
        RunFinishedInterruptOutcome,
        RunFinishedSuccessOutcome,
        RunStartedEvent,
        StateDeltaEvent,
        StateSnapshotEvent,
        StepFinishedEvent,
        StepStartedEvent,
        ToolCallEndEvent,
        ToolCallStartEvent,
    )
    from ag_ui.encoder import EventEncoder

    AG_UI_AVAILABLE = True
except ImportError:  # pragma: no cover - optional [agui] extra absent
    pass

# ---------------------------------------------------------------------------
# Internal event-type constants (match agui_stream.py vocabulary)
# ---------------------------------------------------------------------------

_AGUI_STATE_SNAPSHOT = "STATE_SNAPSHOT"
_AGUI_STATE_DELTA = "STATE_DELTA"
_AGUI_STEP_STARTED = "STEP_STARTED"
_AGUI_STEP_FINISHED = "STEP_FINISHED"
_AGUI_TOOL_CALL_START = "TOOL_CALL_START"
_AGUI_TOOL_CALL_END = "TOOL_CALL_END"
_AGUI_CUSTOM = "CUSTOM"
_AGUI_TEXT_MESSAGE_CONTENT = "TEXT_MESSAGE_CONTENT"
_AGUI_RAW = "RAW"
_AGUI_RUN_STARTED = "RUN_STARTED"
_AGUI_RUN_FINISHED = "RUN_FINISHED"
_AGUI_RUN_ERROR = "RUN_ERROR"


# ---------------------------------------------------------------------------
# Resume payload mapping
# ---------------------------------------------------------------------------


def _map_resume_payload(payload: Any) -> Optional[str]:
    """Map a resume entry payload to an ApprovalDecision string.

    ``approved`` decides, as the per-interrupt ``responseSchema`` documents
    (``approved`` required, ``editedArgs`` an optional string). Mapping rules,
    checked in this order:
    - {approved: false, ...} -> "deny", whatever ``editedArgs`` holds
    - non-blank ``editedArgs``/``edited_args`` -> "edit"; this covers
      {approved: true, editedArgs: "..."} and the legacy bare
      {editedArgs: "..."} with no ``approved`` key
    - {approved: true} (``editedArgs`` absent or blank) -> "approve"
    - Non-dict payloads, or dicts with no boolean ``approved`` and no
      non-blank ``editedArgs`` -> None (ambiguous; caller should treat as an
      error).

    Returns:
        Decision string ("approve", "deny", "edit") or None when the payload
        is ambiguous/malformed.
    """
    if not isinstance(payload, dict):
        return None

    approved = payload.get("approved")
    if approved is False:
        return "deny"
    edited = _extract_edited_text(payload)
    if edited is not None and edited.strip():
        return "edit"
    if approved is True:
        return "approve"

    # No explicit boolean ``approved`` and no non-blank editedArgs -- ambiguous.
    return None


def _extract_edited_text(payload: Any) -> Optional[str]:
    """Extract edited text from a resume payload for edit decisions."""
    if not isinstance(payload, dict):
        return None
    text = payload.get("editedArgs") or payload.get("edited_args")
    if isinstance(text, str):
        return text
    if isinstance(text, dict):
        return json.dumps(text)
    return None


def _skipped_result(skipped: List[Dict[str, str]]) -> Dict[str, Any]:
    """Keyword arguments that put skipped resume entries on ``RUN_FINISHED``.

    AG-UI 1.0 ``interrupt-resume.mdx`` asks the producer to "surface a warning"
    for an entry it cannot use, without naming a channel. CAO logs it and also
    lists it here, as ``result = {"skippedResume": [{"interruptId", "reason"}]}``
    (``reason`` is ``"unknown"`` or ``"expired"``; an evicted id is
    ``"unknown"``, because the registry no longer holds it). ``result`` is the
    spec's optional return value. Metadata only: ids and reasons, never prompt
    text. With nothing skipped this returns no keyword at all, so the frame is
    unchanged.
    """
    if not skipped:
        return {}
    return {"result": {"skippedResume": list(skipped)}}


def _response_schema(options: List[str]) -> Dict[str, Any]:
    """Build one interrupt's ``responseSchema`` from the options it offers.

    Follows ag-ui ``docs/concepts/interrupts.mdx``: ``approved`` is required,
    and the presence of ``editedArgs`` is the capability signal that a client
    may offer edit UI, so it appears only when the interrupt offers ``edit``.
    For CAO, ``editedArgs`` is the edited answer text typed into the agent's
    prompt in place of a yes/no answer, not replacement tool arguments, so it
    is a string.
    """
    properties: Dict[str, Any] = {
        "approved": {"type": "boolean", "description": "true=approve, false=deny"},
    }
    if "edit" in options:
        properties["editedArgs"] = {
            "type": "string",
            "description": "edited answer text, typed into the agent's prompt in place of yes/no",
        }
    return {"type": "object", "properties": properties, "required": ["approved"]}


# ---------------------------------------------------------------------------
# Run plane stream generator
# ---------------------------------------------------------------------------

# Production default for the idle SSE heartbeat (seconds). Overridable via env
# for deployment tuning; the per-call ``heartbeat_interval`` param overrides
# both (used by tests to run at sub-second cadence — F-SL6).
RUN_PLANE_HEARTBEAT_SECONDS = float(os.environ.get("CAO_AGUI_HEARTBEAT_SECONDS", "15.0"))


def get_run_plane_content_type(accept: Optional[str] = None) -> str:
    """Return the negotiated content type for the run plane response.

    Mirrors the ``EventEncoder(accept=...)`` negotiation used inside
    ``run_plane_stream``. Falls back to ``text/event-stream`` when the SDK
    is unavailable or ``accept`` is None.
    """
    if not AG_UI_AVAILABLE:
        return "text/event-stream"
    encoder = EventEncoder(accept=accept)
    return encoder.get_content_type() or "text/event-stream"


async def run_plane_stream(
    input_data: Dict[str, Any],
    approval_construct: Optional[Any] = None,
    snapshot_fn: Optional[Any] = None,
    bus_subscribe_fn: Optional[Any] = None,
    heartbeat_interval: Optional[float] = None,
    accept: Optional[str] = None,
) -> AsyncGenerator[str, None]:
    """Stream lifecycle-legal AG-UI SSE frames for a single run.

    Args:
        input_data: The raw JSON body (camelCase keys) from the client.
        approval_construct: The AgentHandoffWithApproval instance (or None).
        snapshot_fn: Callable returning the current fleet snapshot dict.
        bus_subscribe_fn: Async callable that yields live CAO events from SseBus.
        heartbeat_interval: Override for the SSE heartbeat interval (seconds).
        accept: The request Accept header value for content-type negotiation.
            Passed to ``EventEncoder(accept=...)``; the negotiated type is
            available via ``get_content_type()``. When None or absent, defaults
            to ``text/event-stream`` (standard SSE).

    Yields:
        SSE-formatted strings (``data: {...}\\n\\n``).
    """
    if not AG_UI_AVAILABLE:
        # Should not reach here (route gates on AG_UI_AVAILABLE), but safety.
        yield f"data: {json.dumps({'error': 'ag-ui-protocol not installed'})}\n\n"
        return

    # Parse RunAgentInput from the camelCase dict.
    run_input = RunAgentInput(**input_data)
    thread_id = run_input.thread_id
    run_id = run_input.run_id

    encoder = EventEncoder(accept=accept)

    # Guard: heartbeat comment frames are SSE-specific. If content negotiation
    # ever yields a non-SSE type, fall back to text/event-stream to avoid
    # emitting invalid `:keep-alive` comments into a non-SSE format.
    _content_type = encoder.get_content_type()
    _is_sse = "text/event-stream" in (_content_type or "text/event-stream")

    # Helper to emit one event.
    def _emit(event: Any) -> str:
        return encoder.encode(event)

    # Track lifecycle state for legality.
    finished = False

    # ── 1. RUN_STARTED ──────────────────────────────────────────────────
    run_started = RunStartedEvent(
        type="RUN_STARTED",
        thread_id=thread_id,
        run_id=run_id,
        protocol_version=PROTOCOL_VERSION,
    )
    yield _emit(run_started)

    # Resume entries skipped because they could not be answered (unknown or
    # evicted id, or an answer to an expired interrupt). Reported on
    # RUN_FINISHED.result so the client sees the warning in-band too.
    skipped_resume: List[Dict[str, str]] = []

    # ── 2. Process resume[] entries ─────────────────────────────────────
    if run_input.resume and approval_construct is not None:
        from cli_agent_orchestrator.services.agui.handoff_approval import (
            ApprovalDecision,
            DeliveryError,
        )

        for entry in run_input.resume:
            interrupt_id = entry.interrupt_id
            interrupt = approval_construct.get_interrupt(interrupt_id)
            if interrupt is None:
                # AG-UI 1.0 interrupt-resume.mdx, "An unrecognised entry": the
                # run SHOULD proceed without the entry, and the producer SHOULD
                # surface a warning rather than fail the run. This also covers
                # an interrupt CAO raised that has since been purged from the
                # registry. Whatever is still pending is re-reported below.
                logger.warning(
                    "run_plane: ignoring resume entry for unknown interrupt %s "
                    "(never raised, or no longer retained)",
                    interrupt_id,
                )
                skipped_resume.append({"interruptId": interrupt_id, "reason": "unknown"})
                continue

            if interrupt.resolved:
                # An expired interrupt can no longer be answered (same page,
                # "Resuming"), so an answer for it is dropped with a warning. It
                # can still be abandoned (status "cancelled"), which is how a
                # thread moves past it and needs no warning. Any other resolved
                # interrupt is an idempotent re-send and is skipped quietly.
                if interrupt.outcome == "expired" and entry.status != "cancelled":
                    logger.warning(
                        "run_plane: ignoring resume entry for expired interrupt %s; "
                        "an expired interrupt can no longer be answered",
                        interrupt_id,
                    )
                    skipped_resume.append({"interruptId": interrupt_id, "reason": "expired"})
                continue

            # Map payload to decision
            decision_str: Optional[str] = _map_resume_payload(entry.payload)
            if entry.status == "cancelled":
                decision_str = "deny"

            if decision_str is None:
                err = RunErrorEvent(
                    type="RUN_ERROR",
                    thread_id=thread_id,
                    run_id=run_id,
                    message=(
                        f"Ambiguous resume payload for interrupt {interrupt_id}: "
                        f"payload must include 'approved' (boolean) or 'editedArgs'"
                    ),
                )
                yield _emit(err)
                finished = True
                return

            edited_text = _extract_edited_text(entry.payload) if decision_str == "edit" else None

            try:
                decision_enum = ApprovalDecision(decision_str)
                answered = await approval_construct.resume(
                    interrupt_id=interrupt_id,
                    decision=decision_enum,
                    edited_text=edited_text,
                )
            except (KeyError, ValueError) as e:
                err = RunErrorEvent(
                    type="RUN_ERROR",
                    thread_id=thread_id,
                    run_id=run_id,
                    message=f"Resume failed for interrupt {interrupt_id}: {e}",
                )
                yield _emit(err)
                finished = True
                return
            except DeliveryError as e:
                # Delivery to the terminal failed; the interrupt is left
                # unresolved (retryable). Surface an explicit error rather than
                # finishing the run as a success (P1).
                err = RunErrorEvent(
                    type="RUN_ERROR",
                    thread_id=thread_id,
                    run_id=run_id,
                    message=f"Delivery failed for interrupt {interrupt_id} (retryable): {e}",
                )
                yield _emit(err)
                finished = True
                return

            if answered.outcome == "expired" and entry.status != "cancelled":
                # The interrupt was pending when checked above, but expired while
                # its delivery was queued behind a sibling delivery on the same
                # terminal (a status flap can expire one interrupt and open the
                # next while the first is still being typed). Expiry won with zero
                # keystrokes, so this answer was not used: warn and list it, the
                # same as an answer that arrived after expiry. Abandoning it
                # (status "cancelled") stays quiet, as it does in the pre-check.
                logger.warning(
                    "run_plane: ignoring resume entry for expired interrupt %s; "
                    "an expired interrupt can no longer be answered",
                    interrupt_id,
                )
                skipped_resume.append({"interruptId": interrupt_id, "reason": "expired"})

    # ── 3. Check for open interrupts ────────────────────────────────────
    if approval_construct is not None:
        pending = approval_construct.pending()
        if pending:
            # Emit STATE_SNAPSHOT with current fleet state
            if snapshot_fn is not None:
                try:
                    snapshot = snapshot_fn()
                    snap_evt = StateSnapshotEvent(
                        type="STATE_SNAPSHOT",
                        thread_id=thread_id,
                        run_id=run_id,
                        snapshot=snapshot,
                    )
                    yield _emit(snap_evt)
                except Exception:
                    logger.warning("run_plane: snapshot failed for interrupt state", exc_info=True)

            # Convert CAO Interrupts to ag-ui Interrupts. The interior metadata
            # is camelCased (terminalId/sessionName) to match the stock ag-ui
            # wire dialect, and response_schema is built per interrupt from the
            # options it offers, so a client knows how to answer it.
            ag_interrupts = []
            for p in pending:
                meta = p.metadata or {}
                ag_meta = {
                    "provider": meta.get("provider"),
                    "terminalId": meta.get("terminal_id"),
                    "sessionName": meta.get("session_name"),
                    "options": list(p.options),
                }
                ag_intr = AgUiInterrupt(
                    id=p.id,
                    reason=p.reason,
                    message=p.message,
                    metadata=ag_meta,
                    response_schema=_response_schema(p.options),
                )
                ag_interrupts.append(ag_intr)

            # Emit RUN_FINISHED with interrupt outcome
            outcome = RunFinishedInterruptOutcome(
                type="interrupt",
                interrupts=ag_interrupts,
            )
            run_finished = RunFinishedEvent(
                type="RUN_FINISHED",
                thread_id=thread_id,
                run_id=run_id,
                outcome=outcome,
                **_skipped_result(skipped_resume),
            )
            yield _emit(run_finished)
            finished = True
            return

    # ── 4. STATE_SNAPSHOT (initial fleet state) ─────────────────────────
    if snapshot_fn is not None:
        try:
            snapshot = snapshot_fn()
            snap_evt = StateSnapshotEvent(
                type="STATE_SNAPSHOT",
                thread_id=thread_id,
                run_id=run_id,
                snapshot=snapshot,
            )
            yield _emit(snap_evt)
        except Exception:
            logger.warning("run_plane: initial STATE_SNAPSHOT failed", exc_info=True)

    # ── 5. Live projection from SseBus ─────────────────────────────────
    if bus_subscribe_fn is not None:
        from cli_agent_orchestrator.services.agui.lifecycle_tracker import ToolCallLifecycleTracker
        from cli_agent_orchestrator.services.agui_stream import to_agui_event

        tracker = ToolCallLifecycleTracker()
        interval = (
            heartbeat_interval if heartbeat_interval is not None else RUN_PLANE_HEARTBEAT_SECONDS
        )
        aiter = bus_subscribe_fn().__aiter__()
        # Persistent task for the pending read: shielding it across wait_for
        # timeouts means a heartbeat never cancels the generator's in-flight
        # step (which would corrupt the async generator).
        next_task: Optional["asyncio.Task[Any]"] = None

        while not finished:
            if next_task is None:
                next_task = asyncio.ensure_future(aiter.__anext__())
            try:
                event = await asyncio.wait_for(asyncio.shield(next_task), timeout=interval)
            except asyncio.TimeoutError:
                # Idle for `interval` seconds: emit a proxy-friendly SSE comment
                # keep-alive so intermediaries don't drop the stream (P1-2). The
                # shielded read stays pending across the timeout.
                if _is_sse:
                    yield ":keep-alive\n\n"
                continue
            except StopAsyncIteration:
                next_task = None
                break
            next_task = None

            agui_type, data = to_agui_event(event)

            # Feed through the lifecycle tracker for TOOL_CALL bracketing
            for ftype, fdata in tracker.feed(event, (agui_type, data)):
                frame = _translate_live_frame(ftype, fdata, thread_id, run_id, encoder)
                if frame is not None:
                    yield frame

        # Close any remaining open tool calls
        for ftype, fdata in tracker.close_all():
            frame = _translate_live_frame(ftype, fdata, thread_id, run_id, encoder)
            if frame is not None:
                yield frame

    # ── 6. RUN_FINISHED (success) ───────────────────────────────────────
    if not finished:
        outcome = RunFinishedSuccessOutcome(type="success")
        run_finished = RunFinishedEvent(
            type="RUN_FINISHED",
            thread_id=thread_id,
            run_id=run_id,
            outcome=outcome,
            **_skipped_result(skipped_resume),
        )
        yield _emit(run_finished)


def _translate_live_frame(
    agui_type: str,
    data: Dict[str, Any],
    thread_id: str,
    run_id: str,
    encoder: "EventEncoder",
) -> Optional[str]:
    """Translate a CAO internal AG-UI frame into a stock SDK event and encode it.

    Returns the encoded SSE string or None if the frame type is not mappable.
    """
    if not AG_UI_AVAILABLE:  # pragma: no cover - optional [agui] extra absent
        return None

    try:
        if agui_type == _AGUI_STATE_SNAPSHOT:
            # data should contain a snapshot payload (from state_snapshot_frame)
            snapshot_value = data.get("snapshot") or data
            evt = StateSnapshotEvent(
                type="STATE_SNAPSHOT",
                thread_id=thread_id,
                run_id=run_id,
                snapshot=snapshot_value,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_STATE_DELTA:
            delta = data.get("delta") or data.get("ops") or []
            evt = StateDeltaEvent(
                type="STATE_DELTA",
                thread_id=thread_id,
                run_id=run_id,
                delta=delta,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_STEP_STARTED:
            step_id = data.get("step_id") or data.get("terminal_id") or str(uuid.uuid4())
            step_name = data.get("step_name") or data.get("provider") or "step"
            evt = StepStartedEvent(
                type="STEP_STARTED",
                thread_id=thread_id,
                run_id=run_id,
                step_id=step_id,
                step_name=step_name,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_STEP_FINISHED:
            step_id = data.get("step_id") or data.get("terminal_id") or "unknown"
            step_name = data.get("step_name") or "step"
            evt = StepFinishedEvent(
                type="STEP_FINISHED",
                thread_id=thread_id,
                run_id=run_id,
                step_id=step_id,
                step_name=step_name,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_TOOL_CALL_START:
            tool_call_id = data.get("tool_call_id") or str(uuid.uuid4())
            tool_call_name = data.get("tool_call_name") or "unknown"
            evt = ToolCallStartEvent(
                type="TOOL_CALL_START",
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
                tool_call_name=tool_call_name,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_TOOL_CALL_END:
            tool_call_id = data.get("tool_call_id") or "unknown"
            evt = ToolCallEndEvent(
                type="TOOL_CALL_END",
                thread_id=thread_id,
                run_id=run_id,
                tool_call_id=tool_call_id,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_CUSTOM:
            # Already a CUSTOM frame (the ambient mapping's generative-UI frame,
            # ``name=CAO_GENERATIVE_UI_EVENT_NAME``): re-wrap it for this run
            # with its own name and value, so the value is not nested twice.
            evt = CustomEvent(
                type="CUSTOM",
                thread_id=thread_id,
                run_id=run_id,
                name=data.get("name") or CAO_GENERATIVE_UI_EVENT_NAME,
                value=data.get("value"),
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_TEXT_MESSAGE_CONTENT:
            evt = CustomEvent(
                type="CUSTOM",
                thread_id=thread_id,
                run_id=run_id,
                name="cao.message_delivery",
                value=data,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_RAW:
            evt = CustomEvent(
                type="CUSTOM",
                thread_id=thread_id,
                run_id=run_id,
                name="cao.raw",
                value=data,
            )
            return encoder.encode(evt)

        elif agui_type == _AGUI_RUN_ERROR:
            message = data.get("message") or "unknown error"
            evt = RunErrorEvent(
                type="RUN_ERROR",
                thread_id=thread_id,
                run_id=run_id,
                message=message,
            )
            return encoder.encode(evt)

        else:
            # Unmapped type -> custom event with the raw data
            evt = CustomEvent(
                type="CUSTOM",
                thread_id=thread_id,
                run_id=run_id,
                name=f"cao.{agui_type.lower()}",
                value=data,
            )
            return encoder.encode(evt)

    except Exception:
        logger.warning("run_plane: failed to translate frame %s", agui_type, exc_info=True)
        return None


__all__ = [
    "AG_UI_AVAILABLE",
    "run_plane_stream",
]
