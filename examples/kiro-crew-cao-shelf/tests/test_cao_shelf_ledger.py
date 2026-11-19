"""A8: the on-disk ledger of closed helpers (ids, counts, times; never text)."""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

import cao_shelf_support as sup  # noqa: F401  (puts backend/ on sys.path)
from cao_shelf.ledger import Ledger

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=timezone.utc)


def entry(
    tid: str = "aaaaaaa1", rows: int = 2, last: str = "2026-11-15T10:00:00+00:00", **kw: Any
) -> Dict[str, Any]:
    base = {
        "session": "s",
        "terminal_id": tid,
        "rows": rows,
        "first_at": "2026-11-15T09:00:00+00:00",
        "last_at": last,
        "last_status": "delivered",
    }
    base.update(kw)
    return base


def test_merge_writes_a_private_file(tmp_path: Path) -> None:
    path = tmp_path / "data" / "ledger.json"
    led = Ledger(path)
    assert led.merge([entry()], NOW) is True
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    saved = json.loads(path.read_text())
    assert saved["entries"] == [entry()]
    assert not list(path.parent.glob("*.tmp"))


def test_counts_survive_a_restart(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    Ledger(path).merge([entry("aaaaaaa1"), entry("bbbbbbb1")], NOW)
    again = Ledger(path)
    assert again.count_for("s") == 2 and again.count_for("other") == 0


def test_merge_widens_and_does_not_rewrite_when_nothing_changed(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    led = Ledger(path)
    led.merge([entry(rows=2)], NOW)
    mtime = os.stat(path).st_mtime_ns
    assert led.merge([entry(rows=2)], NOW) is False
    assert os.stat(path).st_mtime_ns == mtime
    led.merge(
        [
            entry(
                rows=5,
                last="2026-11-15T11:00:00+00:00",
                last_status="failed",
                first_at="2026-11-14T00:00:00+00:00",
            )
        ],
        NOW,
    )
    merged = led.entries_for("s")[0]
    assert merged["rows"] == 5 and merged["last_at"] == "2026-11-15T11:00:00+00:00"
    assert merged["first_at"] == "2026-11-14T00:00:00+00:00" and merged["last_status"] == "failed"
    led.merge([entry(rows=1)], NOW)  # fewer rows (window moved on) never lowers the count
    assert led.entries_for("s")[0]["rows"] == 5


def test_entries_older_than_90_days_are_pruned(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    old = (NOW - timedelta(days=91)).isoformat()
    fresh = (NOW - timedelta(days=89)).isoformat()
    led.merge([entry("aaaaaaa1", last=old), entry("bbbbbbb1", last=fresh)], NOW)
    assert [e["terminal_id"] for e in led.entries_for("s")] == ["bbbbbbb1"]
    later = NOW + timedelta(days=5)
    led.merge([], later)
    assert led.entries_for("s") == []


def test_disabled_ledger_reads_and_writes_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    Ledger(path).merge([entry()], NOW)
    off = Ledger(path, enabled=False)
    assert off.entries_for("s") == [] and off.count_for("s") == 0
    before = path.read_text()
    assert off.merge([entry("bbbbbbb1")], NOW) is False
    assert path.read_text() == before
    fresh = tmp_path / "never.json"
    Ledger(fresh, enabled=False).merge([entry()], NOW)
    assert not fresh.exists()


def test_corrupted_file_is_ignored_and_replaced(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{ this is not json")
    led = Ledger(path)
    assert led.entries_for("s") == []
    assert json.loads(path.read_text())["entries"] == []
    led.merge([entry()], NOW)
    assert Ledger(path).count_for("s") == 1


def test_wrong_shape_and_bad_entries_are_dropped(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(
        json.dumps({"entries": [entry(), {"terminal_id": "x"}, "junk", entry("zzzzzzzz")]})
    )
    assert [e["terminal_id"] for e in Ledger(path).entries_for("s")] == ["aaaaaaa1"]
    path.write_text(json.dumps([1, 2]))
    assert Ledger(path).entries_for("s") == []


def test_ledger_never_holds_text(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    Ledger(path).merge([entry(message=sup.SENTINEL, text=sup.SENTINEL)], NOW)
    assert sup.SENTINEL not in path.read_text()


def test_set_enabled_toggle(tmp_path: Path) -> None:
    led = Ledger(tmp_path / "ledger.json")
    led.merge([entry()], NOW)
    led.set_enabled(False)
    assert led.entries_for("s") == []
    led.set_enabled(True)
    assert led.count_for("s") == 1
