"""Regression tests: cao-session-monitor fixtures vs. real CAO source shapes.

The fixtures under ``examples/cao-session-monitor/fixtures/`` were captured
live against a running cao-server (Task 1 spike, see
``openspec/changes/cao-session-monitor/tasks.md`` item 1.1/1.3) and are frozen
ground truth for the herdr plugin's parsing code (Tasks 4/5). This suite pins
each fixture against the actual ``cli_agent_orchestrator`` function or Pydantic
model it mirrors, so drift between a fixture and the real source shape fails
loudly here instead of silently breaking the plugin later.

Independence: rather than re-asserting fixture content back at itself, each
test re-derives the expected shape from the real source -- calling
``build_dashboard_snapshot`` / ``diff_snapshot`` with synthetic raw rows, or
introspecting the Pydantic models directly -- and compares that against the
fixture content.

Covers (numbering matches the task spec):
1. ``test_fixture_is_valid_nonempty_json`` -- all 4 files parse and are non-empty.
2. ``test_state_snapshot_top_level_keys_match_source``,
   ``test_state_snapshot_terminal_field_set_matches_source``,
   ``test_state_snapshot_session_field_set_matches_source`` -- exact key sets.
3. ``test_state_delta_ops_are_well_formed_rfc6902``,
   ``test_state_delta_round_trips_onto_baseline_to_reconstruct_snapshot``,
   ``test_state_delta_matches_real_diff_snapshot_output`` -- RFC-6902 validity
   and round-trip.
4. ``test_flows_entries_match_flow_model_field_set_and_types``,
   ``test_workflows_entries_match_workflow_index_row_model_field_set_and_types``.
5. ``test_terminal_window_matches_generate_window_name_pattern``.
6. ``test_state_snapshot_is_bare_not_sse_wrapped``,
   ``test_state_delta_is_bare_ops_array_not_sse_wrapped``.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List

import pytest
from pydantic import ValidationError

from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.models.workflow_runtime import WorkflowIndexRow
from cli_agent_orchestrator.services.ui_state_service import (
    build_dashboard_snapshot,
    diff_snapshot,
)
from cli_agent_orchestrator.utils.terminal import validate_tmux_name

FIXTURES_DIR = Path(__file__).resolve().parent.parent / "fixtures"

_VALID_RFC6902_OPS = {"add", "remove", "replace", "move", "copy", "test"}


def _load(filename: str) -> Any:
    return json.loads((FIXTURES_DIR / filename).read_text())


# ---------------------------------------------------------------------------
# 1. All 4 fixtures are valid, non-empty JSON
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "filename",
    ["state_snapshot.json", "state_delta.json", "flows.json", "workflows.json"],
)
def test_fixture_is_valid_nonempty_json(filename: str) -> None:
    data = _load(filename)
    assert data, f"{filename} must be non-empty"


# ---------------------------------------------------------------------------
# 2 & 6. state_snapshot.json: bare DashboardSnapshot shape, no SSE wrapper
# ---------------------------------------------------------------------------


def test_state_snapshot_is_bare_not_sse_wrapped() -> None:
    """Must be the bare DashboardSnapshot dict, not agui_stream's
    ``state_snapshot_frame`` envelope (``{"snapshot": {...}}``)."""
    snap = _load("state_snapshot.json")
    assert isinstance(snap, dict)
    assert "snapshot" not in snap


def test_state_snapshot_top_level_keys_match_source() -> None:
    """Top-level keys must equal build_dashboard_snapshot()'s real output keys."""
    expected_keys = set(build_dashboard_snapshot([], [], scopes=[]).keys())
    snap = _load("state_snapshot.json")
    assert set(snap.keys()) == expected_keys


def test_state_snapshot_terminal_field_set_matches_source() -> None:
    """Each terminal entry has exactly the fields ui_state_service.py projects.

    Uses a synthetic terminal row (not derived from the fixture) so this is an
    independent check of the field set, not a restatement of fixture content.
    """
    synthetic_terminal = {
        "id": "x",
        "session_name": "y",
        "provider": "z",
        "agent_profile": "a",
        "tmux_window": "a-0000",
        "status": "idle",
        "last_active": "2024-01-01T00:00:00",
    }
    expected_fields = set(
        build_dashboard_snapshot([], [synthetic_terminal], scopes=[])["terminals"][0].keys()
    )
    snap = _load("state_snapshot.json")
    assert snap["terminals"], "fixture must contain at least one terminal to validate"
    for term in snap["terminals"]:
        assert set(term.keys()) == expected_fields


def test_state_snapshot_session_field_set_matches_source() -> None:
    """Each session entry has exactly the fields ui_state_service.py projects."""
    synthetic_session = {"id": "x", "name": "y", "status": "active"}
    expected_fields = set(
        build_dashboard_snapshot([synthetic_session], [], scopes=[])["sessions"][0].keys()
    )
    snap = _load("state_snapshot.json")
    assert snap["sessions"], "fixture must contain at least one session to validate"
    for sess in snap["sessions"]:
        assert set(sess.keys()) == expected_fields


def test_state_snapshot_matches_build_dashboard_snapshot_output() -> None:
    """Feeding plausible raw backend rows through the REAL projector reproduces
    the fixture byte-for-byte -- the direct fixture-vs-source tie."""
    snap = _load("state_snapshot.json")

    raw_sessions = [
        {"id": s["id"], "name": s["name"], "status": s["status"]} for s in snap["sessions"]
    ]
    raw_terminals = [
        {
            "id": t["id"],
            "tmux_session": t["session_name"],
            "provider": t["provider"],
            "agent_profile": t["agent_profile"],
            "tmux_window": t["window"],
            "status": t["status"],
            "last_active": t["last_active"],
        }
        for t in snap["terminals"]
    ]

    rebuilt = build_dashboard_snapshot(raw_sessions, raw_terminals, scopes=snap["scopes"])
    assert rebuilt == snap


# ---------------------------------------------------------------------------
# 3. state_delta.json: valid RFC-6902 ops array + round-trip
# ---------------------------------------------------------------------------


def test_state_delta_is_bare_ops_array_not_sse_wrapped() -> None:
    """Must be the bare ops list, not agui_stream's ``state_delta_frame``
    envelope (``{"delta": [...]}``)."""
    delta = _load("state_delta.json")
    assert isinstance(delta, list)


def test_state_delta_ops_are_well_formed_rfc6902() -> None:
    delta = _load("state_delta.json")
    assert delta, "fixture must contain at least one op"
    for op in delta:
        assert "op" in op and "path" in op
        assert op["op"] in _VALID_RFC6902_OPS
        assert op["path"].startswith("/")
        if op["op"] in ("add", "replace", "test"):
            assert "value" in op
        if op["op"] in ("move", "copy"):
            assert "from" in op


def _rfc6902_apply(doc: Dict[str, Any], ops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Minimal, independent RFC-6902 applier (add/remove/replace only).

    Deliberately does NOT call diff_snapshot or any of its helpers, so a bug
    shared between the ops generator and this applier can't hide a broken
    round-trip.
    """

    def unescape(token: str) -> str:
        return token.replace("~1", "/").replace("~0", "~")

    result = copy.deepcopy(doc)
    for op in ops:
        tokens = [unescape(t) for t in op["path"].split("/")[1:]]
        target = result
        for token in tokens[:-1]:
            target = target[token]
        leaf = tokens[-1]
        if op["op"] in ("add", "replace"):
            target[leaf] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del target[leaf]
        else:
            raise AssertionError(f"fixture uses an op this applier doesn't support: {op['op']}")
    return result


def _baseline_before_delta(snap: Dict[str, Any]) -> Dict[str, Any]:
    """The pre-delta snapshot implied by state_delta.json's ops.

    Per diff_snapshot()'s granularity rules (ui_state_service.py): ``/sessions``
    and ``/terminals`` are whole-key replaced, ``/counts/*`` is per-key
    replaced, and an unchanged key emits no op at all. state_delta.json's ops
    touch only ``counts/sessions``, ``counts/terminals``, ``/sessions`` and
    ``/terminals`` -- never ``/scopes`` -- so the implied baseline shares the
    fixture's scopes unchanged, with empty sessions/terminals/counts (the only
    prior state consistent with a *replace*, not an *add*, on every one of
    those keys).
    """
    return build_dashboard_snapshot([], [], scopes=snap["scopes"])


def test_state_delta_round_trips_onto_baseline_to_reconstruct_snapshot() -> None:
    """Applying the fixture's ops to the implied baseline reconstructs
    state_snapshot.json exactly, via the independent applier above."""
    snap = _load("state_snapshot.json")
    delta = _load("state_delta.json")
    baseline = _baseline_before_delta(snap)

    assert _rfc6902_apply(baseline, delta) == snap


def test_state_delta_matches_real_diff_snapshot_output() -> None:
    """The fixture's ops equal what the real diff_snapshot() computes between
    the implied baseline and state_snapshot.json (ties fixture to source)."""
    snap = _load("state_snapshot.json")
    delta = _load("state_delta.json")
    baseline = _baseline_before_delta(snap)

    assert diff_snapshot(baseline, snap) == delta


# ---------------------------------------------------------------------------
# 4. flows.json / workflows.json match the real Pydantic models
# ---------------------------------------------------------------------------


def test_flows_entries_match_flow_model_field_set_and_types() -> None:
    flows = _load("flows.json")
    assert flows, "fixture must contain at least one flow"
    expected_fields = set(Flow.model_fields.keys())
    for entry in flows:
        assert set(entry.keys()) == expected_fields
        assert isinstance(entry["enabled"], bool)
        try:
            Flow(**entry)
        except ValidationError as e:
            pytest.fail(f"flows.json entry {entry.get('name')!r} violates the Flow model: {e}")


def test_workflows_entries_match_workflow_index_row_model_field_set_and_types() -> None:
    workflows = _load("workflows.json")
    assert workflows, "fixture must contain at least one workflow"
    expected_fields = set(WorkflowIndexRow.model_fields.keys())
    for entry in workflows:
        assert set(entry.keys()) == expected_fields
        assert entry["step_count"] is None or isinstance(entry["step_count"], int)
        try:
            WorkflowIndexRow(**entry)
        except ValidationError as e:
            pytest.fail(
                f"workflows.json entry {entry.get('name')!r} violates the "
                f"WorkflowIndexRow model: {e}"
            )


# ---------------------------------------------------------------------------
# 5. Window-name values follow generate_window_name()'s real pattern
# ---------------------------------------------------------------------------


def test_terminal_window_matches_generate_window_name_pattern() -> None:
    """Each terminal's window is ``<agent_profile>-<4 lowercase hex>`` (the
    ``generate_window_name()`` contract in utils/terminal.py), and
    independently passes the real ``validate_tmux_name()`` gate used to
    construct it."""
    snap = _load("state_snapshot.json")
    for term in snap["terminals"]:
        window = term["window"]
        agent_profile = term["agent_profile"]
        assert window is not None

        # The real production gate -- raises ValueError on anything outside
        # [A-Za-z0-9_][A-Za-z0-9_-]{0,63}.
        validate_tmux_name(window, "window")

        pattern = rf"^{re.escape(agent_profile)}-[0-9a-f]{{4}}$"
        assert re.fullmatch(pattern, window), (
            f"window {window!r} does not match the "
            f"f'{{agent_profile}}-{{uuid4().hex[:4]}}' pattern for "
            f"agent_profile={agent_profile!r}"
        )
