"""AG-UI 1.0: the run plane declares its protocol version on RUN_STARTED.

``docs/spec/1.0/basic/versioning.mdx`` says an implementation of this version
MUST send its declaration, and the producer's declaration is the
``protocolVersion`` field on ``RUN_STARTED``. The ag-ui-protocol SDK does not
set it for us (``RunStartedEvent.protocol_version`` defaults to ``None``), so
these tests read the wire frames rather than the model.

The replay test uses ag-ui's own ``current-peer-declares-protocol-version``
fixture, vendored under ``fixtures/agui_conformance/`` (provenance in the
README there).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest
from ag_ui.core import PROTOCOL_VERSION, Event
from pydantic import TypeAdapter

from cli_agent_orchestrator.services.agui.run_plane import run_plane_stream

_FIXTURE = (
    Path(__file__).parent
    / "fixtures"
    / "agui_conformance"
    / "current-peer-declares-protocol-version.json"
)


def _run_input(thread_id: str, run_id: str, **extra: Any) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    data.update(extra)
    return data


async def _wire_events(input_data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Run the plane and return each SSE ``data:`` frame as parsed JSON."""
    events: List[Dict[str, Any]] = []
    async for frame in run_plane_stream(input_data=input_data):
        for line in frame.strip().split("\n"):
            if line.startswith("data: "):
                events.append(json.loads(line[len("data: ") :]))
    return events


def test_sdk_protocol_version_is_1_0() -> None:
    """The floor in pyproject (>=1.0.0,<1.1.0) resolves to an SDK that speaks 1.0."""
    assert PROTOCOL_VERSION == "1.0"


@pytest.mark.asyncio
async def test_run_started_declares_protocol_version_on_the_wire() -> None:
    events = await _wire_events(_run_input("thread-pv", "run-pv"))

    run_started = events[0]
    assert run_started["type"] == "RUN_STARTED"
    # camelCase on the wire; the snake_case model name must not leak.
    assert run_started.get("protocolVersion") == "1.0", run_started
    assert "protocol_version" not in run_started


@pytest.fixture(scope="module")
def fixture() -> Dict[str, Any]:
    data: Dict[str, Any] = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    assert data["name"] == "current-peer-declares-protocol-version"
    return data


class TestCurrentPeerDeclaresProtocolVersionFixture:
    """Replay of ag-ui ``spec/1.0/conformance/streams/current-peer-declares-protocol-version``.

    The fixture is written for a client: a current peer's stream declares
    ``protocolVersion`` on ``RUN_STARTED``, and the client's own request
    declares it too. CAO is the peer on the other side, so the replay checks
    that CAO accepts that request and produces that stream.
    """

    def test_fixture_frames_are_valid_1_0_events(self, fixture: Dict[str, Any]) -> None:
        adapter: TypeAdapter[Any] = TypeAdapter(Event)
        for frame in fixture["stream"]:
            adapter.validate_python(frame)

    @pytest.mark.asyncio
    async def test_cao_produces_the_fixture_stream(self, fixture: Dict[str, Any]) -> None:
        expected = fixture["stream"]
        first = expected[0]
        request = _run_input(first["threadId"], first["runId"], **fixture["expect"]["request"])

        actual = await _wire_events(request)

        assert [e["type"] for e in actual] == [e["type"] for e in expected]
        # RUN_STARTED must match the fixture frame exactly, declaration included.
        assert actual[0] == expected[0]
        # RUN_FINISHED: every field the fixture carries matches. CAO also sends
        # outcome {type: "success"}, which is the fixture's expected "completed".
        assert fixture["expect"]["outcome"] == "completed"
        assert {k: actual[-1][k] for k in expected[-1]} == expected[-1]
        assert actual[-1]["outcome"] == {"type": "success"}
