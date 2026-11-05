"""AG-UI 1.0 interrupt/resume SHOULDs on the run plane (#1191).

Spec: ag-ui ``docs/spec/1.0/basic/patterns/interrupt-resume.mdx``.

- "An unrecognised entry — one naming an interrupt the producer did not raise:
  the run SHOULD proceed without the entry, and the producer SHOULD surface a
  warning rather than fail a run over an answer it never asked for."
- An interrupt CAO raised that has since expired can no longer be answered
  (same page, "an interrupt the consumer judges expired can no longer be
  *answered*"), so an answer for it is skipped with a warning too. It can still
  be abandoned, which needs no warning.

ag-ui ``docs/concepts/interrupts.mdx`` (guidance, outside ``spec/1.0``): the
``responseSchema`` carries ``"required": ["approved"]``, and the presence of
``editedArgs`` is the capability signal that edit UI may be offered. For CAO,
``editedArgs`` is the edited answer text typed into the agent's prompt, so it
is a string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
from typing import Any, Dict, List, Optional

import pytest

from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.handoff_approval import (
    AgentHandoffWithApproval,
    ApprovalDecision,
)
from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

_RUN_PLANE_LOGGER = "cli_agent_orchestrator.services.agui.run_plane"

# A selector-style permission dialog with no free-text field. Its reason has a
# full-reason override in handoff_approval._REASON_OPTIONS that does NOT offer edit.
_SELECTOR_PROMPT = ("claude_code", "\u2191/\u2193 to navigate")
# Kiro CLI's y/n/t prompt: a permission_request that DOES offer edit.
_KIRO_PROMPT = ("kiro_cli", "Allow this action? [y/n/t]:")


def _run_input(resume: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "threadId": "thread-1191",
        "runId": "run-1191",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        data["resume"] = resume
    return data


async def _events(construct: AgentHandoffWithApproval, **input_kw: Any) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    async for frame in run_plane_stream(
        input_data=_run_input(**input_kw), approval_construct=construct
    ):
        for line in frame.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def _construct() -> AgentHandoffWithApproval:
    return AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)


def _raise(construct: AgentHandoffWithApproval, terminal_id: str, prompt: tuple) -> Any:
    provider, raw = prompt
    return construct.on_provider_waiting(
        terminal_id=terminal_id, provider=provider, raw_prompt=raw, session_name="s-1"
    )


def _warnings(caplog: pytest.LogCaptureFixture) -> List[str]:
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == _RUN_PLANE_LOGGER and r.levelno == logging.WARNING
    ]


# ---------------------------------------------------------------------------
# (a) unknown or expired resume entries: warn and proceed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_entry_warns_and_run_proceeds(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    construct = _construct()

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}}
        ],
    )

    types = [e["type"] for e in events]
    assert "RUN_ERROR" not in types, events
    assert types == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[-1]["outcome"] == {"type": "success"}
    assert any("never-raised" in w for w in _warnings(caplog)), caplog.records


@pytest.mark.asyncio
async def test_unknown_entry_does_not_block_a_valid_entry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The run proceeds *without* the entry: later entries are still applied."""
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    construct = _construct()
    known = _raise(construct, "t-1", _SELECTOR_PROMPT)

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}},
            {"interruptId": known.id, "status": "resolved", "payload": {"approved": True}},
        ],
    )

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[-1]["outcome"] == {"type": "success"}
    assert construct.get_interrupt(known.id).outcome == ApprovalDecision.APPROVE.value
    assert any("never-raised" in w for w in _warnings(caplog))


@pytest.mark.asyncio
async def test_unknown_entry_re_reports_what_is_still_pending() -> None:
    construct = _construct()
    still_open = _raise(construct, "t-1", _SELECTOR_PROMPT)

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}}
        ],
    )

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    outcome = events[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    assert [i["id"] for i in outcome["interrupts"]] == [still_open.id]


@pytest.mark.asyncio
async def test_answer_for_expired_interrupt_warns_and_run_proceeds(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    construct = _construct()
    expired = _raise(construct, "t-1", _SELECTOR_PROMPT)
    construct.expire("t-1")

    events = await _events(
        construct,
        resume=[{"interruptId": expired.id, "status": "resolved", "payload": {"approved": True}}],
    )

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[-1]["outcome"] == {"type": "success"}
    # Nothing was delivered for it: the interrupt keeps its expired outcome.
    assert construct.get_interrupt(expired.id).outcome == "expired"
    assert any(expired.id in w and "expired" in w for w in _warnings(caplog)), caplog.records


@pytest.mark.asyncio
async def test_abandoning_an_expired_interrupt_is_quiet(caplog: pytest.LogCaptureFixture) -> None:
    """Abandonment is how a thread moves past an expired interrupt; no warning."""
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    construct = _construct()
    expired = _raise(construct, "t-1", _SELECTOR_PROMPT)
    construct.expire("t-1")

    events = await _events(
        construct,
        resume=[{"interruptId": expired.id, "status": "cancelled"}],
    )

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert events[-1]["outcome"] == {"type": "success"}
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_re_sent_answer_for_resolved_interrupt_is_quiet(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry for an interrupt already answered stays an idempotent, silent skip."""
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    construct = _construct()
    answered = _raise(construct, "t-1", _SELECTOR_PROMPT)
    entry = {"interruptId": answered.id, "status": "resolved", "payload": {"approved": True}}
    await _events(construct, resume=[entry])
    caplog.clear()

    events = await _events(construct, resume=[entry])

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"]
    assert _warnings(caplog) == []


# ---------------------------------------------------------------------------
# (b), (c) responseSchema built per interrupt
# ---------------------------------------------------------------------------


async def _pending_schemas(construct: AgentHandoffWithApproval) -> Dict[str, Dict[str, Any]]:
    events = await _events(construct)
    outcome = events[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    return {i["metadata"]["terminalId"]: i["responseSchema"] for i in outcome["interrupts"]}


@pytest.mark.asyncio
async def test_response_schema_requires_approved_on_every_interrupt() -> None:
    construct = _construct()
    _raise(construct, "t-no-edit", _SELECTOR_PROMPT)
    _raise(construct, "t-edit", _KIRO_PROMPT)

    schemas = await _pending_schemas(construct)

    assert set(schemas) == {"t-no-edit", "t-edit"}
    for schema in schemas.values():
        assert schema["type"] == "object"
        assert schema["required"] == ["approved"]
        assert schema["properties"]["approved"]["type"] == "boolean"


@pytest.mark.asyncio
async def test_response_schema_offers_edited_args_only_where_edit_is_offered() -> None:
    construct = _construct()
    no_edit = _raise(construct, "t-no-edit", _SELECTOR_PROMPT)
    with_edit = _raise(construct, "t-edit", _KIRO_PROMPT)
    # Guard the premise: the two prompts really do differ in whether edit is offered.
    assert "edit" not in no_edit.options
    assert "edit" in with_edit.options

    schemas = await _pending_schemas(construct)

    assert "editedArgs" not in schemas["t-no-edit"]["properties"]
    assert "editedArgs" in schemas["t-edit"]["properties"]


@pytest.mark.asyncio
async def test_edited_args_is_typed_string() -> None:
    construct = _construct()
    _raise(construct, "t-edit", _KIRO_PROMPT)

    schemas = await _pending_schemas(construct)

    assert schemas["t-edit"]["properties"]["editedArgs"]["type"] == "string"


# ---------------------------------------------------------------------------
# P2-1 (#1198 review): ``approved`` decides; ``editedArgs`` cannot override a deny
# ---------------------------------------------------------------------------


class _RecordingDelivery:
    """Records what would be typed into the agent's terminal."""

    def __init__(self) -> None:
        self.calls: List[tuple] = []

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        self.calls.append(("input", terminal_id, text))

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        self.calls.append(("key", terminal_id, key))
        return True


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, decision, delivered",
    [
        pytest.param({"approved": False, "editedArgs": "x"}, "deny", "n", id="deny-with-text"),
        pytest.param({"approved": True, "editedArgs": ""}, "approve", "y", id="approve-blank"),
        pytest.param({"approved": False, "editedArgs": ""}, "deny", "n", id="deny-blank"),
        pytest.param({"approved": True, "editedArgs": "x"}, "edit", "x", id="approve-with-text"),
        pytest.param({"editedArgs": "x"}, "edit", "x", id="legacy-bare-edit"),
    ],
)
async def test_approved_decides_and_edited_args_cannot_override_a_deny(
    payload: Dict[str, Any], decision: str, delivered: str
) -> None:
    """Every payload here is valid against the per-interrupt responseSchema
    (``approved`` required, optional string ``editedArgs``), except the legacy
    bare ``{editedArgs}`` form, which stays an edit for existing clients."""
    delivery = _RecordingDelivery()
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
    interrupt = _raise(construct, "t-edit", _KIRO_PROMPT)
    assert "edit" in interrupt.options  # premise: this prompt offers edit

    events = await _events(
        construct,
        resume=[{"interruptId": interrupt.id, "status": "resolved", "payload": payload}],
    )

    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_FINISHED"], events
    assert events[-1]["outcome"] == {"type": "success"}
    assert construct.get_interrupt(interrupt.id).outcome == decision
    assert delivery.calls == [("input", "t-edit", delivered)]


# ---------------------------------------------------------------------------
# In-band report of skipped entries (#1198 review note "In-band warning"):
# RUN_FINISHED.result = {"skippedResume": [{"interruptId", "reason"}]}
# ---------------------------------------------------------------------------


def _skipped(event: Dict[str, Any]) -> Any:
    return (event.get("result") or {}).get("skippedResume")


async def _raw_frames(construct: AgentHandoffWithApproval, **input_kw: Any) -> List[str]:
    return [
        frame
        async for frame in run_plane_stream(
            input_data=_run_input(**input_kw), approval_construct=construct
        )
    ]


@pytest.mark.asyncio
async def test_unknown_entry_is_listed_on_run_finished() -> None:
    construct = _construct()

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}}
        ],
    )

    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED"
    assert finished["outcome"] == {"type": "success"}
    assert finished["result"] == {
        "skippedResume": [{"interruptId": "never-raised", "reason": "unknown"}]
    }


@pytest.mark.asyncio
async def test_answer_for_expired_interrupt_is_listed_on_run_finished() -> None:
    construct = _construct()
    expired = _raise(construct, "t-1", _SELECTOR_PROMPT)
    construct.expire("t-1")

    events = await _events(
        construct,
        resume=[{"interruptId": expired.id, "status": "resolved", "payload": {"approved": True}}],
    )

    assert _skipped(events[-1]) == [{"interruptId": expired.id, "reason": "expired"}]
    # Metadata only: the listed entry carries exactly the id and the reason.
    assert set(_skipped(events[-1])[0]) == {"interruptId", "reason"}


@pytest.mark.asyncio
async def test_unknown_plus_valid_lists_only_the_unknown_entry() -> None:
    delivery = _RecordingDelivery()
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
    known = _raise(construct, "t-edit", _KIRO_PROMPT)

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}},
            {"interruptId": known.id, "status": "resolved", "payload": {"approved": True}},
        ],
    )

    assert delivery.calls == [("input", "t-edit", "y")]
    assert construct.get_interrupt(known.id).outcome == ApprovalDecision.APPROVE.value
    assert events[-1]["outcome"] == {"type": "success"}
    assert _skipped(events[-1]) == [{"interruptId": "never-raised", "reason": "unknown"}]


@pytest.mark.asyncio
async def test_skipped_entries_ride_the_interrupt_outcome_when_others_are_pending() -> None:
    construct = _construct()
    still_open = _raise(construct, "t-1", _SELECTOR_PROMPT)

    events = await _events(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}}
        ],
    )

    outcome = events[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    assert [i["id"] for i in outcome["interrupts"]] == [still_open.id]
    assert _skipped(events[-1]) == [{"interruptId": "never-raised", "reason": "unknown"}]


@pytest.mark.asyncio
async def test_nothing_skipped_leaves_run_finished_byte_identical() -> None:
    """No skipped entry: no ``result`` key, and the frame is exactly today's."""
    construct = _construct()
    known = _raise(construct, "t-1", _SELECTOR_PROMPT)

    frames = await _raw_frames(
        construct,
        resume=[{"interruptId": known.id, "status": "resolved", "payload": {"approved": True}}],
    )

    assert frames[-1] == (
        'data: {"type":"RUN_FINISHED","threadId":"thread-1191","runId":"run-1191",'
        '"outcome":{"type":"success"}}\n\n'
    )


@pytest.mark.asyncio
async def test_quiet_skips_are_not_listed() -> None:
    """Abandoning an expired interrupt and re-sending a resolved answer stay unlisted."""
    construct = _construct()
    expired = _raise(construct, "t-1", _SELECTOR_PROMPT)
    construct.expire("t-1")
    answered = _raise(construct, "t-2", _SELECTOR_PROMPT)
    entry = {"interruptId": answered.id, "status": "resolved", "payload": {"approved": True}}
    await _events(construct, resume=[entry])

    events = await _events(
        construct,
        resume=[{"interruptId": expired.id, "status": "cancelled"}, entry],
    )

    assert events[-1]["outcome"] == {"type": "success"}
    assert "result" not in events[-1]


# ---------------------------------------------------------------------------
# #1198 round 3 (fanhongy, haofeif): an answer whose interrupt expires while
# its delivery is QUEUED behind a sibling delivery on the same terminal.
# ---------------------------------------------------------------------------


class _BlockingDelivery:
    """Records keystrokes; the first ``send_input`` blocks until released.

    Runs in the worker thread ``_deliver_and_commit`` uses (``asyncio.to_thread``),
    so the event loop stays free while interrupt A is "being typed".
    """

    def __init__(self) -> None:
        self.calls: List[tuple] = []
        self.entered = threading.Event()
        self.release = threading.Event()

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        self.calls.append(("input", terminal_id, text))
        if not self.entered.is_set():
            self.entered.set()
            assert self.release.wait(timeout=10), "test never released the blocked delivery"

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        self.calls.append(("key", terminal_id, key))
        return True


async def _until(predicate: Any, what: str, timeout: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        assert loop.time() < deadline, f"timed out waiting for: {what}"
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status, payload, listed",
    [
        pytest.param("resolved", {"approved": True}, True, id="answer-is-listed-expired"),
        pytest.param("cancelled", None, False, id="cancel-stays-quiet"),
    ],
)
async def test_entry_that_expires_while_queued(
    caplog: pytest.LogCaptureFixture,
    status: str,
    payload: Optional[Dict[str, Any]],
    listed: bool,
) -> None:
    """A is being typed; a status flap expires A and opens B on the same
    terminal; the run for B queues on the per-terminal delivery lock and B
    expires before its turn. B gets zero keystrokes (expiry-before-send).

    An answer for B was not used, so it is warned about and listed as expired.
    Abandoning B (status "cancelled") is quiet and unlisted, the same as
    cancelling an interrupt that had already expired before the run."""
    caplog.set_level(logging.WARNING, logger=_RUN_PLANE_LOGGER)
    delivery = _BlockingDelivery()
    try:
        construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery)
        a = _raise(construct, "t-race", _KIRO_PROMPT)

        # A is being typed: its worker thread is blocked inside send_input.
        a_task = asyncio.create_task(construct.resume(a.id, ApprovalDecision.APPROVE))
        await _until(delivery.entered.is_set, "A's delivery to start")

        # The flap: A expires and B opens on the same terminal.
        assert construct.expire("t-race") is not None
        b = _raise(construct, "t-race", _KIRO_PROMPT)
        assert b.id != a.id and not b.resolved

        # The run for B passes the pending check and queues behind A on the lock.
        entry: Dict[str, Any] = {"interruptId": b.id, "status": status}
        if payload is not None:
            entry["payload"] = payload
        b_run = asyncio.create_task(_events(construct, resume=[entry]))
        await _until(
            lambda: construct._delivery_locks["t-race"].refs == 2,
            "B to queue on the terminal lock",
        )

        # B's prompt goes away before its turn; then A's typing finishes.
        assert construct.expire("t-race") is not None
        delivery.release.set()
        await asyncio.gather(a_task, return_exceptions=True)
        events = await asyncio.wait_for(b_run, timeout=10)
    finally:
        # Never leave the worker thread blocked if an assertion above fails.
        delivery.release.set()

    assert construct.get_interrupt(b.id).outcome == "expired"
    assert delivery.calls == [("input", "t-race", "y")]  # only A's keystroke
    assert events[-1]["type"] == "RUN_FINISHED", events
    assert events[-1]["outcome"] == {"type": "success"}
    b_warnings = [w for w in _warnings(caplog) if b.id in w]
    if listed:
        assert _skipped(events[-1]) == [{"interruptId": b.id, "reason": "expired"}]
        assert any("expired" in w for w in b_warnings), b_warnings
    else:
        assert "result" not in events[-1], events[-1]
        assert b_warnings == []


class _FailingDelivery:
    """Every delivery raises, so ``resume`` raises ``DeliveryError``."""

    def send_input(self, terminal_id: str, text: str, **kwargs: Any) -> None:
        raise RuntimeError("backend down")

    def send_special_key(self, terminal_id: str, key: str) -> bool:
        raise RuntimeError("backend down")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt, delivery, entry, message",
    [
        # run_plane: an ambiguous payload ends the run before resume() is called.
        pytest.param(
            _KIRO_PROMPT, _RecordingDelivery, {}, "Ambiguous resume payload", id="ambiguous"
        ),
        # run_plane: resume() raises ValueError, because this prompt offers no edit.
        pytest.param(
            _SELECTOR_PROMPT,
            _RecordingDelivery,
            {"editedArgs": "x"},
            "Resume failed",
            id="invalid-decision",
        ),
        # run_plane: resume() raises DeliveryError.
        pytest.param(
            _KIRO_PROMPT,
            _FailingDelivery,
            {"approved": True},
            "Delivery failed",
            id="delivery-error",
        ),
    ],
)
async def test_run_error_after_a_skipped_entry_carries_no_list(
    prompt: tuple, delivery: Any, entry: Dict[str, Any], message: str
) -> None:
    """An unknown id is skipped, then the next entry ends the run on one of the
    three RUN_ERROR exits. RUN_ERROR never carries ``result.skippedResume``."""
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=delivery())
    pending = _raise(construct, "t-fail", prompt)

    frames = await _raw_frames(
        construct,
        resume=[
            {"interruptId": "never-raised", "status": "resolved", "payload": {"approved": True}},
            {"interruptId": pending.id, "status": "resolved", "payload": entry},
        ],
    )
    events = [
        json.loads(line[len("data: ") :])
        for frame in frames
        for line in frame.strip().split("\n")
        if line.startswith("data: ")
    ]

    assert events[-1]["type"] == "RUN_ERROR", events
    assert message in events[-1]["message"], events[-1]
    assert "result" not in events[-1]
    assert "skippedResume" not in frames[-1]
