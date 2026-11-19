"""A7 (pure part): closed helpers rebuilt from inbox rows, against the shared expected files."""

from __future__ import annotations

import json
from typing import Any, Dict, List

import cao_shelf_support as sup
import pytest
from cao_shelf import history

DAYS = ["quiet", "busy"]


def _cases() -> List[Any]:
    out = []
    for day in DAYS:
        for path in sorted((sup.FIXTURES / "expected" / day).glob("history.*.json")):
            out.append(
                pytest.param(
                    day, path.name[len("history.") : -len(".json")], id=f"{day}-{path.stem}"
                )
            )
    return out


@pytest.mark.parametrize("day,name", _cases())
def test_rebuild_matches_expected(day: str, name: str) -> None:
    view, _ = sup.fixture_view(day)
    live = {t["id"] for rows in view.values() for t in rows}
    base = sup.FIXTURES / "cao" / day
    conductor = view[f"cao-{name}"][0]["id"]
    rows = sup.load(base / f"inbox.{conductor}.json")
    got = history.rebuild(rows, live, session=name)
    assert got == sup.load(sup.FIXTURES / "expected" / day / f"history.{name}.json")


def row(sender: str, at: str, status: str = "delivered", text: str = "x") -> Dict[str, Any]:
    return {"sender_id": sender, "created_at": at, "status": status, "message": text}


def test_live_helpers_are_never_listed() -> None:
    rows = [
        row("aaaaaaa1", "2026-11-15T10:00:00+00:00"),
        row("bbbbbbb1", "2026-11-15T09:00:00+00:00"),
    ]
    got = history.rebuild(rows, {"aaaaaaa1"}, session="s")
    assert [c["terminal_id"] for c in got["closed"]] == ["bbbbbbb1"]
    assert got["external_senders"] == []


def test_external_senders_are_counted_separately() -> None:
    rows = [
        row("cao-ops-mcp", "2026-11-15T10:00:00+00:00"),
        row("supervisor", "2026-11-15T09:00:00+00:00"),
        row("cao-ops-mcp", "2026-11-15T08:00:00+00:00"),
        row("ABCDEF12", "2026-11-15T07:00:00+00:00"),
    ]
    got = history.rebuild(rows, set(), session="s")
    assert got["closed"] == []
    assert got["external_senders"] == [
        {"sender_id": "ABCDEF12", "rows": 1},
        {"sender_id": "cao-ops-mcp", "rows": 2},
        {"sender_id": "supervisor", "rows": 1},
    ]


def test_window_full_flags_incomplete() -> None:
    rows = [
        row("aaaaaaa1", f"2026-11-15T{h:02d}:{m:02d}:00+00:00") for h in range(2) for m in range(50)
    ]
    assert len(rows) == 100
    got = history.rebuild(rows, set(), session="s")
    assert got["complete"] is False and got["rows_seen"] == 100 and got["window_rows"] == 100
    assert history.rebuild(rows[:99], set(), session="s")["complete"] is True


def test_more_than_the_window_is_cut() -> None:
    rows = [row("aaaaaaa1", "2026-11-15T10:00:00+00:00")] * 150
    got = history.rebuild(rows, set(), session="s")
    assert got["rows_seen"] == 100


def test_first_last_and_status_per_helper() -> None:
    rows = [
        row("aaaaaaa1", "2026-11-15T10:00:00+00:00", "failed"),
        row("aaaaaaa1", "2026-11-15T08:00:00+00:00", "delivered"),
        row("aaaaaaa1", "2026-11-15T09:00:00+00:00", "pending"),
    ]
    only = history.rebuild(rows, set(), session="s")["closed"][0]
    assert (only["rows"], only["first_at"], only["last_at"], only["last_status"]) == (
        3,
        "2026-11-15T08:00:00+00:00",
        "2026-11-15T10:00:00+00:00",
        "failed",
    )
    assert only["label"] is None


def test_newest_first_ordering_across_helpers() -> None:
    rows = [
        row("aaaaaaa1", "2026-11-15T01:00:00+00:00"),
        row("bbbbbbb1", "2026-11-15T05:00:00+00:00"),
        row("cccccccc", "2026-11-15T03:00:00+00:00"),
    ]
    got = history.rebuild(rows, set(), session="s")
    assert [c["terminal_id"] for c in got["closed"]] == ["bbbbbbb1", "cccccccc", "aaaaaaa1"]


def test_unknown_status_is_clamped_and_junk_rows_are_ignored() -> None:
    junk: List[Any] = [{"nonsense": 1}, "text"]
    rows = [row("aaaaaaa1", "2026-11-15T01:00:00+00:00", "mystery"), *junk]
    got = history.rebuild(rows, set(), session="s")
    assert got["closed"][0]["last_status"] == "delivered"


def test_rebuild_output_has_no_message_text() -> None:
    rows = [row("aaaaaaa1", "2026-11-15T01:00:00+00:00", text=sup.SENTINEL)]
    assert sup.SENTINEL not in json.dumps(history.rebuild(rows, set(), session="s"))


def test_merge_with_ledger_keeps_counts_beyond_the_window() -> None:
    window = history.rebuild([row("aaaaaaa1", "2026-11-15T10:00:00+00:00")], set(), session="s")
    ledger = [
        {
            "session": "s",
            "terminal_id": "aaaaaaa1",
            "rows": 5,
            "first_at": "2026-11-10T00:00:00+00:00",
            "last_at": "2026-11-15T09:00:00+00:00",
            "last_status": "delivered",
        },
        {
            "session": "s",
            "terminal_id": "bbbbbbb1",
            "rows": 2,
            "first_at": "2026-11-01T00:00:00+00:00",
            "last_at": "2026-11-02T00:00:00+00:00",
            "last_status": "failed",
        },
    ]
    merged = history.merge_with_ledger(window, ledger, live_ids=set())
    by_id = {c["terminal_id"]: c for c in merged["closed"]}
    assert by_id["aaaaaaa1"]["rows"] == 5
    assert by_id["aaaaaaa1"]["first_at"] == "2026-11-10T00:00:00+00:00"
    assert by_id["aaaaaaa1"]["last_at"] == "2026-11-15T10:00:00+00:00"
    assert by_id["bbbbbbb1"]["last_status"] == "failed"
    assert [c["terminal_id"] for c in merged["closed"]] == ["aaaaaaa1", "bbbbbbb1"]


def test_merge_with_ledger_skips_live_ids_and_other_sessions() -> None:
    window = history.rebuild([], set(), session="s")
    ledger = [
        {
            "session": "s",
            "terminal_id": "aaaaaaa1",
            "rows": 1,
            "first_at": "2026-11-01T00:00:00+00:00",
            "last_at": "2026-11-01T00:00:00+00:00",
            "last_status": "delivered",
        },
        {
            "session": "other",
            "terminal_id": "bbbbbbb1",
            "rows": 1,
            "first_at": "2026-11-01T00:00:00+00:00",
            "last_at": "2026-11-01T00:00:00+00:00",
            "last_status": "delivered",
        },
    ]
    assert history.merge_with_ledger(window, ledger, live_ids={"aaaaaaa1"})["closed"] == []


def test_closed_entries_for_ledger_have_no_text() -> None:
    window = history.rebuild([row("aaaaaaa1", "2026-11-15T10:00:00+00:00")], set(), session="s")
    entries = history.ledger_entries("s", window)
    assert entries == [
        {
            "session": "s",
            "terminal_id": "aaaaaaa1",
            "rows": 1,
            "first_at": "2026-11-15T10:00:00+00:00",
            "last_at": "2026-11-15T10:00:00+00:00",
            "last_status": "delivered",
        }
    ]


def test_cut_row_text() -> None:
    assert history.row_text("a" * 3000) == "a" * 2000
    assert history.row_text("a\x1b[31mb\x07c") == "abc"
    assert history.row_text(None) == ""
