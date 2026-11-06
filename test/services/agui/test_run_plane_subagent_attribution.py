"""#1193 follow-up: run-plane interrupts name the terminal subagent that raised them.

An ``Interrupt`` raised by a CAO terminal carries ``subagentRunId`` equal to that
terminal's id on the ambient stream (``terminal:<id>``), so a client can tie the
prompt to the SUBAGENT_* invocation it already shows. The key is a string or
absent, never null; it never appears on RUN_* itself, and the run plane emits no
SUBAGENT_* events.
"""

from __future__ import annotations

import json
from test.services.agui.test_run_plane_interrupt_shoulds import (
    _KIRO_PROMPT,
    _SELECTOR_PROMPT,
    _construct,
    _events,
    _raise,
)
from typing import Any, Dict, Iterator, List

import pytest

from cli_agent_orchestrator.services.agui.attribution import subagent_run_id_for_terminal
from cli_agent_orchestrator.services.agui.handoff_approval import Interrupt


def _walk(value: Any) -> Iterator[tuple]:
    if isinstance(value, dict):
        for k, v in value.items():
            yield k, v
            yield from _walk(v)
    elif isinstance(value, list):
        for v in value:
            yield from _walk(v)


@pytest.mark.asyncio
async def test_interrupt_subagent_run_id_is_absent_or_string() -> None:
    construct = _construct()
    _raise(construct, "aaaa1111", _KIRO_PROMPT)
    _raise(construct, "bbbb2222", _SELECTOR_PROMPT)

    events = await _events(construct)

    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED"
    interrupts = finished["outcome"]["interrupts"]
    by_terminal: Dict[str, Any] = {i["metadata"]["terminalId"]: i for i in interrupts}
    assert set(by_terminal) == {"aaaa1111", "bbbb2222"}
    for tid, intr in by_terminal.items():
        assert intr["subagentRunId"] == subagent_run_id_for_terminal(tid)

    # Never null anywhere, never on RUN_* itself, and no SUBAGENT_* on this plane.
    for event in events:
        for key, value in _walk(event):
            if key == "subagentRunId":
                assert isinstance(value, str) and value, event
        if event["type"].startswith("RUN_"):
            assert "subagentRunId" not in event
        assert not event["type"].startswith("SUBAGENT_")


class _NoTerminalConstruct:
    """A construct whose pending interrupts name no terminal.

    ``AgentHandoffWithApproval.raise_interrupt`` always records a terminal id,
    so the real construct cannot produce this case; with no resume entries,
    ``run_plane_stream`` calls only ``pending()``.
    """

    def __init__(self, interrupts: List[Interrupt]) -> None:
        self._interrupts = interrupts

    def pending(self) -> List[Interrupt]:
        return list(self._interrupts)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "metadata",
    [
        pytest.param({"provider": "kiro_cli", "session_name": "s-1"}, id="no-key"),
        pytest.param({"provider": "kiro_cli", "terminal_id": None}, id="none"),
        pytest.param({"provider": "kiro_cli", "terminal_id": ""}, id="empty"),
    ],
)
async def test_interrupt_without_a_terminal_has_no_subagent_run_id(
    metadata: Dict[str, Any],
) -> None:
    construct = _NoTerminalConstruct(
        [Interrupt(reason="kiro:permission_request", metadata=metadata, options=["y", "n"])]
    )

    events = await _events(construct)  # type: ignore[arg-type]

    finished = events[-1]
    assert finished["type"] == "RUN_FINISHED"
    (interrupt,) = finished["outcome"]["interrupts"]
    assert "subagentRunId" not in interrupt, interrupt
    for event in events:
        assert "subagentRunId" not in json.dumps(event), event
