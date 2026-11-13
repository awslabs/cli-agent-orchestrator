"""The fleet snapshot carries each terminal's live status, not null.

The terminals table has no status column, so a snapshot built from its rows had
``status: null`` for every terminal, and the dashboard printed "unknown" for an idle
agent. The live status is the status monitor's in-memory reading, the same one
``get_terminal`` reports. These tests seed that reading and check that every route that
builds a terminal view carries it.

The enrichment must stay cheap and safe: in memory only (no tmux, no subprocess, no
``get_status`` re-detection that could capture a pane or write), and read only.
"""

from __future__ import annotations

import subprocess
from test._support.agui_conformance import parse_sse
from test._support.agui_stream_harness import install
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import status_monitor as status_monitor_module
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.ui_state_service import (
    build_agent_detail_snapshot,
    build_dashboard_snapshot,
)

client = TestClient(app, base_url="http://localhost")

ROWS = [
    {"id": "t-idle", "tmux_session": "alpha", "provider": "kiro_cli", "agent_profile": "dev"},
    {"id": "t-none", "tmux_session": "alpha", "provider": "kiro_cli", "agent_profile": "dev"},
    {
        "id": "t-failed",
        "tmux_session": "alpha",
        "provider": "kiro_cli",
        "agent_profile": "dev",
        "deferred_init_failure": {"kind": "init_failed"},
    },
    {"id": "t-busy", "tmux_session": "alpha", "provider": "kiro_cli", "agent_profile": "dev"},
]
SESSIONS = [{"id": "alpha", "name": "alpha", "status": "active"}]
EXPECTED = {"t-idle": "idle", "t-none": None, "t-failed": "error", "t-busy": "processing"}
STATUS_ENUM = {s.value for s in TerminalStatus}


@pytest.fixture(autouse=True)
def _seed_monitor(monkeypatch: pytest.MonkeyPatch):
    """Seed the in-memory reading and forbid everything that is not an in-memory read."""
    monkeypatch.setattr(
        status_monitor,
        "_last_status",
        {
            "t-idle": TerminalStatus.IDLE,
            "t-failed": TerminalStatus.IDLE,  # the durable failure marker wins over this
            "t-busy": TerminalStatus.PROCESSING,
        },
    )

    def _forbidden(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("the snapshot must not re-detect, capture a pane or fork")

    for name in ("get_status", "status_observation", "_fresh_capture_pane_status", "_redetect"):
        monkeypatch.setattr(status_monitor, name, _forbidden)
    monkeypatch.setattr(subprocess, "run", _forbidden)
    monkeypatch.setattr(subprocess, "Popen", _forbidden)
    # Nor may it read the database or the failure sidecar (get_terminal does both), or
    # publish on the status bus (get_status can).
    for name in (
        "get_terminal_metadata",
        "get_deferred_init_failure",
        "_read_deferred_failure_fallback",
    ):
        monkeypatch.setattr(terminal_service, name, _forbidden)
    monkeypatch.setattr(status_monitor_module.bus, "publish", _forbidden)


def _statuses(terminals: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {t["id"]: t["status"] for t in terminals}


def _snapshot_terminals(body: str) -> List[Dict[str, Any]]:
    for event in parse_sse(body):
        snap = event.get("snapshot")
        if isinstance(snap, dict) and "terminals" in snap:
            return snap["terminals"]
    raise AssertionError("no STATE_SNAPSHOT")


def _post_body() -> Dict[str, Any]:
    return {
        "threadId": "t",
        "runId": "r",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _rows() -> List[Dict[str, Any]]:
    return [dict(r) for r in ROWS]


def test_conformant_get(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=SESSIONS, terminals={"alpha": _rows()})
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert _statuses(_snapshot_terminals("".join(resp.iter_text()))) == EXPECTED


def test_legacy_get(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=SESSIONS, terminals={"alpha": _rows()})
    with client.stream("GET", "/agui/v1/stream", params={"framing": "cao-legacy"}) as resp:
        assert _statuses(_snapshot_terminals("".join(resp.iter_text()))) == EXPECTED


def test_post_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=SESSIONS, terminals={"alpha": _rows()})
    with client.stream(
        "POST", "/agui/v1/stream", json=_post_body(), headers={"Accept": "text/event-stream"}
    ) as resp:
        assert _statuses(_snapshot_terminals("".join(resp.iter_text()))) == EXPECTED


def test_run_plane_hydrate(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=SESSIONS, terminals={"alpha": _rows()})
    with client.stream("POST", "/agui/v1/run", json=_post_body()) as resp:
        assert _statuses(_snapshot_terminals("".join(resp.iter_text()))) == EXPECTED


def test_rest_session_listing_feeds_the_mcp_apps_dashboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """MCP Apps builds its snapshot from ``GET /sessions/{name}/terminals`` over HTTP."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.session_service.list_current_session_terminals",
        lambda name, **_: _rows(),
    )
    listed = client.get("/sessions/alpha/terminals").json()
    assert _statuses(listed) == EXPECTED
    snapshot = build_dashboard_snapshot(SESSIONS, listed)
    assert _statuses(snapshot["terminals"]) == EXPECTED


def test_agent_detail_carries_the_status_get_terminal_reports() -> None:
    # A guard, not RED evidence: this passes on base too, because the detail builder already
    # copies the ``status`` of the ``GET /terminals/{id}`` body it is given.
    assert build_agent_detail_snapshot({"id": "t", "status": "idle"})["status"] == "idle"
    assert build_agent_detail_snapshot({"id": "t"})["status"] is None


def test_statuses_stay_inside_the_existing_enum_and_add_no_key() -> None:
    rows = terminal_service.with_live_status(_rows())
    assert {r["status"] for r in rows} <= STATUS_ENUM | {None}
    assert [set(r) - set(o) for r, o in zip(rows, ROWS)] == [{"status"}] * len(ROWS)


def test_enrichment_is_read_only_and_does_not_mutate_its_input() -> None:
    rows = _rows()
    before = [dict(r) for r in rows]
    seen = dict(status_monitor._last_status)
    terminal_service.with_live_status(rows)
    assert rows == before and status_monitor._last_status == seen


def test_a_pending_initial_delivery_masks_idle_exactly_as_get_terminal_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(terminal_service, "_pending_initial_delivery", {"t-idle"})
    got = _statuses(terminal_service.with_live_status(_rows()))
    assert got["t-idle"] == "unknown" and got["t-busy"] == "processing"


def test_an_external_owner_error_without_a_marker_is_not_published_as_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(status_monitor._last_status, "t-none", TerminalStatus.ERROR)
    row = {"id": "t-none", "deferred_init_external_owner": True}
    assert terminal_service.with_live_status([row])[0]["status"] == "unknown"
    plain = {"id": "t-none"}
    assert terminal_service.with_live_status([plain])[0]["status"] == "error"


def test_a_row_without_an_id_has_no_status() -> None:
    assert terminal_service.with_live_status([{}])[0]["status"] is None


def test_the_snapshot_cost_is_dictionary_lookups(monkeypatch: pytest.MonkeyPatch) -> None:
    """500 terminals with real readings: values come through, and nothing is re-detected
    (the autouse fixture raises if it is)."""
    cycle = [TerminalStatus.IDLE, TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]
    many = [{"id": f"x{i}", "tmux_session": "alpha"} for i in range(500)]
    monkeypatch.setattr(
        status_monitor, "_last_status", {f"x{i}": cycle[i % 3] for i in range(0, 500, 2)}
    )
    out = terminal_service.with_live_status(many)
    assert len(out) == 500
    for i, row in enumerate(out):
        assert row["status"] == (cycle[i % 3].value if i % 2 == 0 else None)
