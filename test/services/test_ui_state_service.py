"""Unit tests for the snapshot projection + RFC 6902 diff service.

Covers ``build_dashboard_snapshot`` / ``build_agent_detail_snapshot`` field
mapping and ``diff_snapshot`` for scalar and collection changes, including
round-tripping the produced patch back onto the previous snapshot
(Correctness Property 5).
"""

from datetime import datetime, timezone
from typing import Any, Dict, List

from cli_agent_orchestrator.services.ui_state_service import (
    build_agent_detail_snapshot,
    build_dashboard_snapshot,
    diff_snapshot,
)


def _unescape(token: str) -> str:
    """Reverse RFC 6901 pointer escaping (``~1`` -> ``/``, ``~0`` -> ``~``)."""

    return token.replace("~1", "/").replace("~0", "~")


def apply_patch(doc: Dict[str, Any], ops: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Minimal RFC 6902 applier (add/remove/replace) for round-trip assertions."""

    import copy

    result = copy.deepcopy(doc)
    for op in ops:
        tokens = [_unescape(t) for t in op["path"].split("/")[1:]]
        target = result
        for token in tokens[:-1]:
            target = target[token]
        leaf = tokens[-1]
        if op["op"] in ("add", "replace"):
            target[leaf] = copy.deepcopy(op["value"])
        elif op["op"] == "remove":
            del target[leaf]
        else:  # pragma: no cover - applier only needs these three ops
            raise AssertionError(f"unexpected op {op['op']}")
    return result


def test_build_dashboard_snapshot_maps_real_fields() -> None:
    """Sessions/terminals project into the stable snapshot shape."""

    sessions = [{"id": "cao-foo", "name": "cao-foo", "status": "active"}]
    terminals = [
        {
            "id": "abcd1234",
            "tmux_session": "cao-foo",
            "tmux_window": "developer",
            "provider": "kiro_cli",
            "agent_profile": "developer",
            "last_active": datetime(2024, 1, 1, tzinfo=timezone.utc),
        }
    ]

    snap = build_dashboard_snapshot(sessions, terminals, scopes=["cao:read"])

    assert snap["counts"] == {"sessions": 1, "terminals": 1}
    assert snap["sessions"][0]["id"] == "cao-foo"
    term = snap["terminals"][0]
    assert term["session_name"] == "cao-foo"
    assert term["provider"] == "kiro_cli"
    assert term["window"] == "developer"
    assert term["last_active"] == "2024-01-01T00:00:00+00:00"
    assert snap["scopes"] == ["cao:read"]


def test_build_dashboard_snapshot_empty_is_well_formed() -> None:
    """Zero agents yields empty collections and zero counts (not missing keys)."""

    snap = build_dashboard_snapshot([], [], scopes=None)
    assert snap["sessions"] == []
    assert snap["terminals"] == []
    assert snap["counts"] == {"sessions": 0, "terminals": 0}
    assert snap["scopes"] == []


def test_build_agent_detail_snapshot() -> None:
    """A terminal row projects into the per-agent detail shape."""

    terminal = {
        "id": "abcd1234",
        "session_name": "cao-foo",
        "provider": "claude_code",
        "agent_profile": "reviewer",
        "status": "idle",
    }
    snap = build_agent_detail_snapshot(terminal, scopes=["cao:read"], output_tail="hello")
    assert snap["terminal_id"] == "abcd1234"
    assert snap["session_name"] == "cao-foo"
    assert snap["status"] == "idle"
    assert snap["output_tail"] == "hello"


def test_diff_snapshot_scalar_change_is_per_key() -> None:
    """A scalar change inside ``counts`` emits a per-key replace, not a whole replace."""

    prev = build_dashboard_snapshot([], [], scopes=[])
    curr = build_dashboard_snapshot(
        [{"id": "cao-a", "name": "cao-a", "status": "active"}], [], scopes=[]
    )
    ops = diff_snapshot(prev, curr)

    paths = {op["path"] for op in ops}
    assert "/counts/sessions" in paths  # per-key scalar replace
    assert "/sessions" in paths  # whole-key collection replace
    assert apply_patch(prev, ops) == curr


def test_diff_snapshot_collection_change_is_whole_key() -> None:
    """A terminals change emits exactly one whole-key replace at /terminals."""

    prev = build_dashboard_snapshot(
        [{"id": "cao-a", "name": "cao-a", "status": "active"}], [], scopes=[]
    )
    curr = build_dashboard_snapshot(
        [{"id": "cao-a", "name": "cao-a", "status": "active"}],
        [{"id": "11112222", "tmux_session": "cao-a", "provider": "kiro_cli"}],
        scopes=[],
    )
    ops = diff_snapshot(prev, curr)

    terminal_ops = [op for op in ops if op["path"] == "/terminals"]
    assert len(terminal_ops) == 1
    assert terminal_ops[0]["op"] == "replace"
    assert apply_patch(prev, ops) == curr


def test_diff_snapshot_no_change_is_empty() -> None:
    """Identical snapshots produce an empty patch."""

    snap = build_dashboard_snapshot(
        [{"id": "cao-a", "name": "cao-a", "status": "active"}], [], scopes=["cao:read"]
    )
    assert diff_snapshot(snap, dict(snap)) == []


def test_diff_snapshot_pointer_escaping_round_trip() -> None:
    """Keys containing '/' and '~' escape correctly and round-trip."""

    prev = {"a/b": 1, "c~d": 2}
    curr = {"a/b": 9, "c~d": 2, "new": 3}
    ops = diff_snapshot(prev, curr)
    assert apply_patch(prev, ops) == curr
