"""Consolidated AC5 exit-gate (spec task 19.1).

A single gate asserting all four AC5 criteria together so CI fails if any one
regresses (the audit noted equivalent coverage existed but was distributed
across files — this is the single consolidated assertion the task requires):

  1. Each of the four L2 constructs has docs + a runnable example.
  2. approve AND deny each land exactly once, via BOTH the REST resume route
     and the run-plane ``resume[]``.
  3. ``converges_with(...)`` is true across kiro_cli / claude_code / codex.
  4. The AC3 run-plane stream is lifecycle-legal (RUN_STARTED first, terminal
     RUN_FINISHED last) — stock-client verifiable.
"""

from __future__ import annotations

import json
import pathlib

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui.approval_bridge import ApprovalBridge
from cli_agent_orchestrator.services.agui.base import RecordingUiEmitter
from cli_agent_orchestrator.services.agui.cross_provider_sync import CrossProviderStateSync
from cli_agent_orchestrator.services.agui.handoff_approval import AgentHandoffWithApproval
from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
_CONSTRUCT_EXAMPLES = [
    "ag-ui-supervisor-dashboard",
    "ag-ui-session-timeline",
    "ag-ui-handoff-approval",
    "ag-ui-cross-provider-sync",
]


def _run_input(resume=None):
    data = {
        "threadId": "gate-thread",
        "runId": "gate-run",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    if resume is not None:
        data["resume"] = resume
    return data


async def _collect(gen):
    return [json.loads(f[6:]) for f in [x async for x in gen] if f.startswith("data: ")]


# --- Criterion 1: four constructs each have docs + a runnable example --------


def test_ac5_each_construct_has_docs_and_example() -> None:
    for name in _CONSTRUCT_EXAMPLES:
        d = _REPO_ROOT / "examples" / "ag-ui" / name
        assert (d / "run.sh").is_file(), f"{name}: missing runnable example"
        assert (d / "README.md").is_file(), f"{name}: missing docs"
    assert (_REPO_ROOT / "docs" / "agui.md").is_file()


# --- Criterion 2a: approve/deny exactly once via the REST resume route -------


@pytest.fixture()
def _rest_bridge(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "1")
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    app.state.approval_bridge = ApprovalBridge(construct=construct)
    yield construct
    if hasattr(app.state, "approval_bridge"):
        del app.state.approval_bridge


@pytest.mark.parametrize(
    "provider,prompt,decision,expected",
    [
        ("claude_code", "\u2191/\u2193 to navigate", "approve", "approve"),
        ("codex", "Approve execution? (y/n)", "deny", "deny"),
    ],
)
def test_ac5_rest_resume_lands_exactly_once(
    _rest_bridge, provider, prompt, decision, expected
) -> None:
    client = TestClient(app, base_url="http://localhost")
    interrupt = _rest_bridge.on_provider_waiting("t-1", provider, prompt)
    first = client.post(f"/agui/v1/interrupts/{interrupt.id}/resume", json={"decision": decision})
    assert first.status_code == 200
    assert first.json()["outcome"] == expected
    assert first.json()["resolved"] is True
    # Exactly once: a replay returns the SAME recorded outcome (idempotent).
    second = client.post(f"/agui/v1/interrupts/{interrupt.id}/resume", json={"decision": decision})
    assert second.status_code == 200
    assert second.json()["outcome"] == expected


# --- Criterion 2b: approve/deny via the run-plane resume[] --------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("approved,expected", [(True, "approve"), (False, "deny")])
async def test_ac5_run_plane_resume_lands_exactly_once(approved, expected) -> None:
    construct = AgentHandoffWithApproval(emitter=RecordingUiEmitter(), answer_delivery=None)
    interrupt = construct.on_provider_waiting(
        terminal_id="t-1",
        provider="claude_code",
        raw_prompt="\u2191/\u2193 to navigate",
        session_name="s",
    )
    frames = await _collect(
        run_plane_stream(
            input_data=_run_input(
                resume=[
                    {
                        "interruptId": interrupt.id,
                        "status": "resolved",
                        "payload": {"approved": approved},
                    }
                ]
            ),
            approval_construct=construct,
        )
    )
    assert construct.get_interrupt(interrupt.id).resolved
    assert construct.get_interrupt(interrupt.id).outcome == expected
    assert frames[0]["type"] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_FINISHED"


# --- Criterion 3: converges across kiro_cli / claude_code / codex ------------


def test_ac5_converges_across_three_providers() -> None:
    from cli_agent_orchestrator.services.agui_stream import AGUI_STATE_SNAPSHOT

    snapshot = {
        "terminals": [
            {"id": "t1", "session_name": "main", "provider": "kiro_cli", "status": "running"},
            {"id": "t2", "session_name": "main", "provider": "claude_code", "status": "idle"},
            {"id": "t3", "session_name": "main", "provider": "codex", "status": "running"},
        ],
    }
    sync = CrossProviderStateSync(RecordingUiEmitter())
    sync.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, event_id=None)
    assert set(sync.providers_seen()) == {"kiro_cli", "claude_code", "codex"}
    assert sync.converges_with(snapshot) is True


# --- Criterion 4: AC3 run-plane lifecycle legality ---------------------------


@pytest.mark.asyncio
async def test_ac5_run_plane_lifecycle_legal() -> None:
    frames = await _collect(run_plane_stream(input_data=_run_input()))
    assert len(frames) >= 2
    assert frames[0]["type"] == "RUN_STARTED"
    assert frames[-1]["type"] == "RUN_FINISHED"
