"""Every projection that names a terminal carries its ``runner_id`` (#1159, runner half).

A *runner* is a remote execution unit a terminal runs on; a terminal with no runner is
local and its ``runner_id`` is ``None``. ``runner_id`` is a plain label here: this file
makes no claim about tenants or owners (that half of #1159 stays on #778).

On ``main`` no row carries ``runner_id`` yet (the column lives on the 3.0 branch), so the
real value is always ``None`` here. The tests therefore prove the *projection* with
synthetic rows, and the PR says the real-runner path is unverified.

Layout:

* a registry + completeness guard, so a future AG-UI construct cannot ship without a
  decision about runner attribution;
* value tests over the pure builders (including a 3.0-shaped row as a negative control);
* wire tests over all three ``_fleet_snapshot`` call sites (legacy GET, conformant GET,
  ``POST /agui/v1/run``);
* a property test scoped to the *fleet snapshot* terminal view.
"""

from __future__ import annotations

import importlib
import inspect
import json
import pkgutil
from pathlib import Path
from test._support.agui_conformance import parse_sse
from test._support.agui_stream_harness import install
from typing import Any, Dict, List

import pytest
from fastapi.testclient import TestClient
from hypothesis import given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.services.agui.base import AguiConstruct, RecordingUiEmitter
from cli_agent_orchestrator.services.agui.supervisor_dashboard import (
    SupervisorDashboardStream,
)
from cli_agent_orchestrator.services.agui_stream import (
    AGUI_STATE_DELTA,
    AGUI_STATE_SNAPSHOT,
)
from cli_agent_orchestrator.services.ui_state_service import (
    build_agent_detail_snapshot,
    build_dashboard_snapshot,
    diff_snapshot,
)

client = TestClient(app, base_url="http://localhost")

# The documented terminal view of the fleet snapshot: this is the metadata-only wire
# contract. ``runner_id`` is a label; there is no output, tail, path or argument key.
FLEET_TERMINAL_KEYS = frozenset(
    {
        "id",
        "session_name",
        "provider",
        "agent_profile",
        "window",
        "status",
        "last_active",
        "runner_id",
    }
)

ROWS: List[Dict[str, Any]] = [
    {
        "id": "t1",
        "tmux_session": "alpha",
        "provider": "kiro_cli",
        "agent_profile": "dev",
        "runner_id": None,
    },
    {
        "id": "t2",
        "tmux_session": "alpha",
        "provider": "kiro_cli",
        "agent_profile": "dev",
        "runner_id": "rn-a",
    },
    # The on-main case: the row has no ``runner_id`` key at all.
    {"id": "t3", "tmux_session": "beta", "provider": "codex", "agent_profile": None},
]
EXPECTED = {"t1": None, "t2": "rn-a", "t3": None}


# --- registry + completeness guard ------------------------------------------------------


def _fleet_snapshot_terminals() -> List[Dict[str, Any]]:
    return build_dashboard_snapshot([], ROWS)["terminals"]


def _agent_detail_runner(row: Dict[str, Any]) -> Dict[str, Any]:
    return build_agent_detail_snapshot(row)


def _supervisor_by_runner() -> List[Dict[str, Any]]:
    dash = SupervisorDashboardStream(RecordingUiEmitter())
    dash.handle_frame(
        AGUI_STATE_SNAPSHOT,
        {"snapshot": build_dashboard_snapshot([], ROWS)},
        event_id=None,
    )
    return dash.by_runner()


def _host_entries() -> List[Dict[str, Any]]:
    from cli_agent_orchestrator.services import host_resources

    host_resources.reset_host_sampler()
    return host_resources.refresh_host_now()


#: Every projection that names a terminal, by the thing that builds it.
PROJECTIONS = {
    "fleet snapshot terminals (ui_state_service.build_dashboard_snapshot)": (
        _fleet_snapshot_terminals
    ),
    "agent detail snapshot (ui_state_service.build_agent_detail_snapshot)": (
        lambda: [_agent_detail_runner(r) for r in ROWS]
    ),
    "SupervisorDashboardStream.by_runner": _supervisor_by_runner,
    "host resource view (host_resources.sample_host)": _host_entries,
}

#: AG-UI constructs that deliberately do NOT carry ``runner_id`` today, with the reason.
#: A frame names a terminal by id, and a consumer joins to the fleet snapshot, which
#: carries ``runner_id``. Duplicating the label onto each of these would widen the wire
#: contract with no consumer, and on main the value is always null.
EXEMPT = {
    "SupervisorDashboardStream": "covered: by_runner() and hierarchy_by_runner()",
    "CrossProviderStateSync": "opaque shared state; carries no terminal field",
    "MultiAgentSessionTimeline": "entries name terminals by id; join to the snapshot",
    "AgentHandoffWithApproval": "interrupt metadata names a terminal by id; join to the snapshot",
}


def _all_constructs() -> List[type]:
    import cli_agent_orchestrator.services.agui as package

    for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
        importlib.import_module(module.name)

    found: Dict[str, type] = {}
    pending = [AguiConstruct]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            if sub.__name__ not in found:
                found[sub.__name__] = sub
                pending.append(sub)
    # Only the library's own constructs: other test modules define throwaway subclasses
    # (``_StubConstruct``) that are alive in the same process when the suite runs together.
    return [
        c
        for c in found.values()
        if not inspect.isabstract(c) and c.__module__.startswith(package.__name__ + ".")
    ]


def test_every_agui_construct_has_a_runner_decision() -> None:
    """A construct added without a runner decision fails here.

    This covers ``AguiConstruct`` subclasses only. A new builder in ``ui_state_service``
    (or anywhere else) that names a terminal is NOT caught: register it in ``PROJECTIONS``
    by hand.
    """
    names = {c.__name__ for c in _all_constructs()}
    assert names, "the walk found no constructs: the guard would pass vacuously"
    # Positive control: the walk reaches a construct that is only importable by walking.
    assert "SupervisorDashboardStream" in names
    undecided = sorted(names - set(EXEMPT))
    assert not undecided, f"constructs with no runner_id decision: {undecided}"
    stale = sorted(set(EXEMPT) - names)
    assert not stale, f"EXEMPT names a construct that no longer exists: {stale}"
    assert all(reason.strip() for reason in EXEMPT.values())


@pytest.mark.parametrize("name", sorted(PROJECTIONS))
def test_every_registered_projection_carries_runner_id(name: str) -> None:
    """Each registered builder yields rows, and every row has the key (present, not truthy)."""
    rows = PROJECTIONS[name]()
    assert rows, f"{name} produced nothing: the check would pass vacuously"
    assert all("runner_id" in row for row in rows), name


# --- value tests ------------------------------------------------------------------------


def test_fleet_snapshot_terminals_carry_runner_id_present() -> None:
    terminals = _fleet_snapshot_terminals()
    assert {t["id"]: t["runner_id"] for t in terminals} == EXPECTED
    assert all("runner_id" in t for t in terminals), "the key must be present, not merely truthy"


@pytest.mark.parametrize("row", ROWS, ids=[r["id"] for r in ROWS])
def test_agent_detail_carries_runner_id(row: Dict[str, Any]) -> None:
    detail = _agent_detail_runner(row)
    assert "runner_id" in detail
    assert detail["runner_id"] == EXPECTED[row["id"]]


def test_negative_control_three_point_oh_shaped_row_survives_real_builder() -> None:
    """A 3.0-beta1 listing dict (real ``runner_id``) through the real builder keeps it.

    Without this, the null-only world of main would pass a builder that hardcodes None.
    """
    row = {
        "id": "t9",
        "tmux_session": "gamma",
        "provider": "kiro_cli",
        "agent_profile": "dev",
        "tmux_window": "w",
        "status": "idle",
        "last_active": "2026-11-12T00:00:00+00:00",
        "runner_id": "rn-remote-1",
    }
    (view,) = build_dashboard_snapshot([], [row])["terminals"]
    assert view["runner_id"] == "rn-remote-1"
    assert build_agent_detail_snapshot(row)["runner_id"] == "rn-remote-1"


# --- SupervisorDashboardStream: by_runner + hierarchy ----------------------------------


def _dash(rows: List[Dict[str, Any]], sessions: List[Dict[str, Any]] | None = None):
    dash = SupervisorDashboardStream(RecordingUiEmitter())
    snapshot = build_dashboard_snapshot(sessions or [], rows)
    dash.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": snapshot}, event_id=None)
    return dash


def test_by_runner_groups_local_first_then_by_id() -> None:
    rows = [
        {"id": "a", "tmux_session": "s", "provider": "p", "runner_id": "rn-b", "status": "idle"},
        {"id": "b", "tmux_session": "s", "provider": "p", "runner_id": None, "status": "idle"},
        {
            "id": "c",
            "tmux_session": "s",
            "provider": "p",
            "runner_id": "rn-a",
            "status": "waiting_user_answer",
        },
    ]
    groups = _dash(rows).by_runner()
    assert [g["runner_id"] for g in groups] == [None, "rn-a", "rn-b"]
    assert [g["label"] for g in groups] == ["local", "rn-a", "rn-b"]
    assert groups[1]["terminal_count"] == 1
    assert groups[1]["waiting"] == ["c"]
    assert groups[1]["by_status"] == {"waiting_user_answer": 1}


def test_by_runner_default_is_exactly_one_local_group() -> None:
    rows = [{"id": "a", "tmux_session": "s", "provider": "p"}]
    groups = _dash(rows).by_runner()
    assert len(groups) == 1 and groups[0]["runner_id"] is None and groups[0]["label"] == "local"


def test_by_runner_empty_fleet_and_snapshot_key() -> None:
    empty = SupervisorDashboardStream(RecordingUiEmitter())
    assert empty.by_runner() == []
    assert empty.hierarchy_by_runner() == {}
    assert empty.supervisor_snapshot()["by_runner"] == []
    assert _dash(ROWS).supervisor_snapshot()["by_runner"][0]["runner_id"] is None


def test_hierarchy_by_runner_is_keyed_by_runner_and_session() -> None:
    """Two runners can both own a session called ``main``; they must not collide."""
    rows = [
        {"id": "a", "tmux_session": "main", "provider": "p", "runner_id": "rn-a"},
        {"id": "b", "tmux_session": "main", "provider": "p", "runner_id": "rn-b"},
        {"id": "c", "tmux_session": "main", "provider": "p"},
    ]
    dash = _dash(rows, sessions=[{"id": "main", "name": "main", "status": "active"}])
    keyed = dash.hierarchy_by_runner()
    assert set(keyed) == {(None, "main"), ("rn-a", "main"), ("rn-b", "main")}
    assert keyed[("rn-a", "main")]["terminal_ids"] == ["a"]
    assert keyed[("rn-b", "main")]["terminal_ids"] == ["b"]
    # hierarchy() keeps its session-name keys (back-compat) and reports who is in each.
    flat = dash.hierarchy()
    assert set(flat["main"]["terminal_ids"]) == {"a", "b", "c"}
    assert flat["main"]["runner_ids"] == [None, "rn-a", "rn-b"]


def test_round_trip_delta_regroups_terminal() -> None:
    before = build_dashboard_snapshot([], [dict(ROWS[0])])
    moved = dict(ROWS[0], runner_id="rn-z")
    after = build_dashboard_snapshot([], [moved])
    ops = diff_snapshot(before, after)
    assert [o["path"] for o in ops] == ["/terminals"] and ops[0]["op"] == "replace"

    dash = SupervisorDashboardStream(RecordingUiEmitter())
    dash.handle_frame(AGUI_STATE_SNAPSHOT, {"snapshot": before}, event_id=None)
    assert [g["runner_id"] for g in dash.by_runner()] == [None]
    dash.handle_frame(AGUI_STATE_DELTA, {"delta": ops}, event_id=None)
    assert [g["runner_id"] for g in dash.by_runner()] == ["rn-z"]


# --- MCP Apps contract ------------------------------------------------------------------


def test_mcp_apps_types_declare_runner_id() -> None:
    """Python and the TS contract file must not drift (``ui_state_service`` says they are in sync)."""
    types_ts = (
        Path(__file__).resolve().parents[3] / "cao_mcp_apps" / "src" / "shared" / "types.ts"
    ).read_text(encoding="utf-8")
    for interface in ("TerminalView", "AgentDetailSnapshot"):
        start = types_ts.index(f"export interface {interface} {{")
        body = types_ts[start : types_ts.index("\n}", start)]
        assert "runner_id: string | null;" in body, interface


# --- wire tests: all three _fleet_snapshot call sites -----------------------------------

WIRE_SESSIONS = [{"id": "alpha", "name": "alpha", "status": "active"}]
WIRE_TERMINALS = {
    "alpha": [
        {"id": "t1", "tmux_session": "alpha", "provider": "kiro_cli", "runner_id": None},
        {"id": "t2", "tmux_session": "alpha", "provider": "kiro_cli", "runner_id": "rn-a"},
        {"id": "t3", "tmux_session": "alpha", "provider": "codex"},
    ]
}


def _terminals_in(body: str) -> List[Dict[str, Any]]:
    """The terminals of the first snapshot found in an SSE body (either framing)."""
    for event in parse_sse(body):
        snap = event.get("snapshot") if isinstance(event, dict) else None
        if isinstance(snap, dict) and "terminals" in snap:
            return snap["terminals"]
    raise AssertionError(f"no STATE_SNAPSHOT with terminals in: {body[:300]!r}")


def _assert_wire(terminals: List[Dict[str, Any]]) -> None:
    assert {t["id"]: t["runner_id"] for t in terminals} == EXPECTED
    assert all("runner_id" in t for t in terminals)


def test_wire_conformant_get(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=WIRE_SESSIONS, terminals=WIRE_TERMINALS)
    with client.stream("GET", "/agui/v1/stream") as resp:
        assert resp.status_code == 200
        _assert_wire(_terminals_in("".join(resp.iter_text())))


def test_wire_legacy_get(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=WIRE_SESSIONS, terminals=WIRE_TERMINALS)
    with client.stream("GET", "/agui/v1/stream", params={"framing": "cao-legacy"}) as resp:
        assert resp.status_code == 200
        _assert_wire(_terminals_in("".join(resp.iter_text())))


def test_wire_run_plane_post(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=WIRE_SESSIONS, terminals=WIRE_TERMINALS)
    body = {
        "threadId": "thread-1",
        "runId": "run-1",
        "state": {},
        "messages": [],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }
    with client.stream("POST", "/agui/v1/run", json=body) as resp:
        assert resp.status_code == 200
        _assert_wire(_terminals_in("".join(resp.iter_text())))


# --- property: fleet snapshot terminal view ---------------------------------------------

_labels = st.one_of(st.none(), st.text(min_size=1, max_size=12))
_row = st.fixed_dictionaries(
    {"id": st.text(min_size=1, max_size=8), "tmux_session": st.text(max_size=8)},
    optional={
        "provider": st.text(max_size=8),
        "agent_profile": st.one_of(st.none(), st.text(max_size=8)),
        "status": st.one_of(st.none(), st.text(max_size=8)),
        "runner_id": _labels,
        # Things a row must never be able to push onto the wire through the fleet view.
        "output": st.text(max_size=20),
        "output_tail": st.text(max_size=20),
        "cmdline": st.lists(st.text(max_size=6), max_size=3),
        "path": st.text(max_size=20),
    },
)


@settings(max_examples=60, deadline=None)
@given(rows=st.lists(_row, max_size=6))
def test_property_fleet_snapshot_runner_id_and_allow_list(rows: List[Dict[str, Any]]) -> None:
    """Scoped to the fleet snapshot, not agent detail (``output_tail`` already lives there)."""
    out = build_dashboard_snapshot([], rows)["terminals"]
    assert len(out) == len(rows)
    for src, view in zip(rows, out):
        assert view["runner_id"] == src.get("runner_id")
        assert set(view) == FLEET_TERMINAL_KEYS
    json.dumps(out)  # a label, never an object


# --- the old name must not exist anywhere on the wire ------------------------------------


def test_no_projection_carries_the_old_runtime_id_key() -> None:
    """3.0 named the field ``runner_id``; ``runtime_id`` must not appear under any builder."""
    row = dict(ROWS[1], runtime_id="rn-old")  # a stray old-name key on the input row
    (view,) = build_dashboard_snapshot([], [row])["terminals"]
    assert "runtime_id" not in view and view["runner_id"] == "rn-a"
    assert "runtime_id" not in build_agent_detail_snapshot(row)
    for group in _supervisor_by_runner():
        assert "runtime_id" not in group


def test_the_wire_has_no_runtime_id_key(monkeypatch: pytest.MonkeyPatch) -> None:
    install(monkeypatch, sessions=WIRE_SESSIONS, terminals=WIRE_TERMINALS)
    with client.stream("GET", "/agui/v1/stream") as resp:
        body = "".join(resp.iter_text())
    assert "runtime_id" not in body and '"runner_id"' in body


def test_host_projection_entries_carry_runner_id() -> None:
    """The host view is a registered projection: every entry has ``runner_id`` present."""
    entries = PROJECTIONS["host resource view (host_resources.sample_host)"]()
    assert entries, "psutil/os give a value on every CI platform: the guard must not be vacuous"
    assert all("runner_id" in e and e["runner_id"] is None for e in entries)
    assert not any("runtime_id" in e for e in entries), "the old name must not be on the wire"
