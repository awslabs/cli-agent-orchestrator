"""AG-UI 1.0 conformance helpers for tests (a PRE-CHECK; TypeScript has the final word).

Two checks, used by the ambient-stream tests and self-tested against the upstream
1.0 conformance corpus (``test/fixtures/agui_conformance_1_0/``):

* :func:`validate_frames` parses every event with the pinned SDK's discriminated
  ``ag_ui.core.Event`` union and rejects any key the event's model does not declare
  (the SDK models use ``extra="allow"``, so validation alone would let an
  undescribed snake_case key through). It recurses into ``outcome.interrupts``.
* :func:`check_lifecycle` is a Python port of the subset of ``verifyEvents``
  (``@ag-ui/client`` ``src/verify/verify.ts`` at ag-ui ``08eae9dc``) these tests
  rely on: first event, nested and closed runs, open/content/close for text
  messages and tool calls, steps, the subagent lifecycle and ownership, and
  ``subagentRunId: null``. Its error texts mirror ``verify.ts`` so a corpus
  fixture's ``errorContains`` matches. Chunk events are out of its scope.

The authoritative check is the pinned ``@ag-ui/client@1.0.3`` ``verifyEvents``,
run over every capture by ``examples/ag-ui/ag-ui-stock-client-live/verify-ambient.mjs``.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Iterable, List, Optional, Set

from pydantic import BaseModel, TypeAdapter

_EVENT_ADAPTER: Optional[TypeAdapter] = None


class ConformanceError(AssertionError):
    """A stream violated a rule this port checks."""


def _adapter() -> TypeAdapter:
    global _EVENT_ADAPTER
    if _EVENT_ADAPTER is None:
        from ag_ui.core import Event

        _EVENT_ADAPTER = TypeAdapter(Event)
    return _EVENT_ADAPTER


def parse_sse(body: str) -> List[Dict[str, Any]]:
    """Return the JSON object of every ``data:`` line, in order (comments and other fields ignored)."""
    events: List[Dict[str, Any]] = []
    for line in body.split("\n"):
        if line.startswith("data: "):
            events.append(json.loads(line[len("data: ") :]))
        elif line.startswith("data:"):
            events.append(json.loads(line[len("data:") :]))
    return events


def _declared_aliases(model: BaseModel) -> Set[str]:
    names: Set[str] = set()
    for name, field in type(model).model_fields.items():
        names.add(field.alias or name)
    return names


def _strict_keys(raw: Dict[str, Any], model: BaseModel, where: str) -> None:
    allowed = _declared_aliases(model)
    extra = sorted(k for k in raw if k not in allowed)
    if extra:
        raise ConformanceError(f"{where}: keys not described by the schema: {extra}")


def validate_frames(events: Iterable[Dict[str, Any]]) -> None:
    """Validate each event against the pinned SDK and reject undescribed keys."""
    for i, raw in enumerate(events):
        if "type" not in raw:
            raise ConformanceError(f"event {i}: no 'type' in the payload: {sorted(raw)}")
        model = _adapter().validate_python(raw)
        _strict_keys(raw, model, f"event {i} ({raw['type']})")
        outcome = raw.get("outcome")
        if isinstance(outcome, dict) and outcome.get("type") == "interrupt":
            for j, intr in enumerate(outcome.get("interrupts") or []):
                parsed = model.outcome.interrupts[j]  # type: ignore[union-attr]
                _strict_keys(intr, parsed, f"event {i} interrupt {j}")


def _owner(event: Dict[str, Any]) -> str:
    return event.get("subagentRunId") or "(the parent agent)"


def check_lifecycle(events: Iterable[Dict[str, Any]]) -> None:
    """Raise :class:`ConformanceError` on the first rule a stream breaks."""
    first = True
    run_active = run_finished = run_errored = False
    messages: Dict[str, str] = {}
    tool_calls: Dict[str, str] = {}
    steps: Dict[str, str] = {}
    subagents_active: Set[str] = set()
    subagents_finished: Set[str] = set()

    def fail(msg: str) -> None:
        raise ConformanceError(msg)

    for event in events:
        etype = event.get("type")
        if "subagentRunId" in event and event["subagentRunId"] is None:
            fail(
                f"Cannot send '{etype}' with 'subagentRunId: null'. The field is optional — omit it entirely."
            )

        if first:
            first = False
            if etype not in ("RUN_STARTED", "RUN_ERROR"):
                fail("First event must be 'RUN_STARTED'")
        elif etype == "RUN_STARTED":
            if run_active and not run_finished and not run_errored:
                fail(
                    "Cannot send 'RUN_STARTED' while a run is still active. The previous run must be "
                    "finished with 'RUN_FINISHED' before starting a new run."
                )
        else:
            if run_errored:
                fail(
                    f"Cannot send event type '{etype}': The run has already errored with 'RUN_ERROR'. "
                    "No further events can be sent."
                )
            if run_finished and etype != "RUN_ERROR":
                fail(
                    f"Cannot send event type '{etype}': The run has already finished with 'RUN_FINISHED'. "
                    "Start a new run with 'RUN_STARTED'."
                )

        if etype == "RUN_STARTED":
            run_active, run_finished, run_errored = True, False, False
            messages.clear()
            tool_calls.clear()
            steps.clear()
            subagents_active.clear()
            subagents_finished.clear()
        elif etype == "RUN_FINISHED":
            if messages:
                fail(
                    f"Cannot send 'RUN_FINISHED' while text messages are still active: {', '.join(messages)}"
                )
            if tool_calls:
                fail(
                    f"Cannot send 'RUN_FINISHED' while tool calls are still active: {', '.join(tool_calls)}"
                )
            if steps:
                fail(f"Cannot send 'RUN_FINISHED' while steps are still active: {', '.join(steps)}")
            if subagents_active:
                fail(
                    "Cannot send 'RUN_FINISHED' while subagents are still active: "
                    f"{', '.join(sorted(subagents_active))}"
                )
            if "subagentRunId" in event:
                fail("RUN_FINISHED is run-scoped and carries no attribution")
            outcome = event.get("outcome") or {}
            for intr in outcome.get("interrupts") or []:
                if "subagentRunId" in intr and intr["subagentRunId"] is None:
                    fail(
                        f"Cannot send 'RUN_FINISHED' with an interrupt (id '{intr.get('id')}') carrying "
                        "'subagentRunId: null'. The field is optional — omit it entirely."
                    )
            run_finished = True
        elif etype == "RUN_ERROR":
            run_errored = True
        elif etype == "TEXT_MESSAGE_START":
            mid = event.get("messageId")
            if mid in messages:
                fail(
                    f"Cannot send 'TEXT_MESSAGE_START' event: A text message with ID '{mid}' is already in "
                    "progress. Complete it with 'TEXT_MESSAGE_END' first."
                )
            messages[mid] = _owner(event)
        elif etype in ("TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"):
            mid = event.get("messageId")
            if mid not in messages:
                fail(
                    f"Cannot send '{etype}' event: No active text message found with ID '{mid}'. "
                    "Start a text message with 'TEXT_MESSAGE_START' first."
                )
            if "subagentRunId" in event and _owner(event) != messages[mid]:
                fail(
                    f"Cannot send '{etype}': subagentRunId '{event['subagentRunId']}' does not match the "
                    f"message '{mid}' opener's subagent '{messages[mid]}'."
                )
            if etype == "TEXT_MESSAGE_END":
                del messages[mid]
        elif etype == "TOOL_CALL_START":
            tid = event.get("toolCallId")
            if tid in tool_calls:
                fail(
                    f"Cannot send 'TOOL_CALL_START' event: A tool call with ID '{tid}' is already in "
                    "progress. Complete it with 'TOOL_CALL_END' first."
                )
            tool_calls[tid] = _owner(event)
        elif etype in ("TOOL_CALL_ARGS", "TOOL_CALL_END"):
            tid = event.get("toolCallId")
            if tid not in tool_calls:
                fail(
                    f"Cannot send '{etype}' event: No active tool call found with ID '{tid}'. "
                    "A 'TOOL_CALL_START' event must be sent first."
                )
            if "subagentRunId" in event and _owner(event) != tool_calls[tid]:
                fail(
                    f"Cannot send '{etype}': subagentRunId '{event['subagentRunId']}' does not match the "
                    f"tool call '{tid}' opener's subagent '{tool_calls[tid]}'."
                )
            if etype == "TOOL_CALL_END":
                del tool_calls[tid]
        elif etype == "STEP_STARTED":
            key = f"{event.get('stepName')}@{_owner(event)}"
            if key in steps:
                fail(f"Step \"{event.get('stepName')}\" is already active for 'STEP_STARTED'")
            steps[key] = _owner(event)
        elif etype == "STEP_FINISHED":
            key = f"{event.get('stepName')}@{_owner(event)}"
            if key not in steps:
                fail(
                    f"Cannot send 'STEP_FINISHED' for step \"{event.get('stepName')}\" that was not started"
                )
            del steps[key]
        elif etype == "SUBAGENT_STARTED":
            sid = event.get("subagentRunId")
            if not isinstance(sid, str):
                fail("Cannot send 'SUBAGENT_STARTED' without a 'subagentRunId'.")
            if not isinstance(event.get("name"), str):
                fail("Cannot send 'SUBAGENT_STARTED' without a 'name'.")
            if sid in subagents_active:
                fail(
                    f"Cannot send 'SUBAGENT_STARTED': subagent '{sid}' is already active. "
                    "Finish it with 'SUBAGENT_FINISHED' first."
                )
            if sid in subagents_finished:
                fail(
                    f"Cannot send 'SUBAGENT_STARTED': subagent '{sid}' has already finished in this run. "
                    "Subagent IDs are per-invocation and cannot be reused."
                )
            parent = event.get("parentSubagentRunId")
            if (
                parent is not None
                and parent not in subagents_active
                and parent not in subagents_finished
            ):
                fail(
                    f"Cannot send 'SUBAGENT_STARTED': parentSubagentRunId '{parent}' has not been started "
                    "in this run."
                )
            subagents_active.add(sid)
        elif etype in ("SUBAGENT_FINISHED", "SUBAGENT_ERROR"):
            sid = event.get("subagentRunId")
            if not isinstance(sid, str):
                fail(f"Cannot send '{etype}' without a 'subagentRunId'.")
            if etype == "SUBAGENT_ERROR" and not isinstance(event.get("message"), str):
                fail("Cannot send 'SUBAGENT_ERROR' without a 'message'.")
            if sid not in subagents_active:
                fail(
                    f"Cannot send '{etype}': no active subagent found with ID '{sid}'. "
                    "A 'SUBAGENT_STARTED' event must be sent first."
                )
            outcome = event.get("outcome")
            if outcome is not None and outcome.get("type") not in ("success", "suspended"):
                fail(
                    f"Cannot send '{etype}' with outcome type '{outcome.get('type')}'. "
                    'The outcome is either { type: "success" } or { type: "suspended" }.'
                )
            subagents_active.discard(sid)
            subagents_finished.add(sid)
