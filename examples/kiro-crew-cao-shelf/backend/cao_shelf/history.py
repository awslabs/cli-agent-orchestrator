"""Closed helpers rebuilt from a conductor's inbox rows (pure: no I/O, no clock).

A closed helper is a distinct ``sender_id`` shaped like a terminal id that is no longer a live
terminal anywhere in the fleet. CAO keeps no name for it, so ``label`` is always ``None``.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set

from .text import strip_invisible

WINDOW_ROWS = 100
ROW_TEXT_CAP = 2000
TIME_CAP = 40  # CAO's own time text is copied, so it is cut like the ledger cuts it
STATUSES = ("pending", "delivered", "failed")

_ID_RE = re.compile(r"[a-f0-9]{8}")
_ANSI_RE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)?|[@-Z\\-_])")
_FRACTION_RE = re.compile(r"\.(\d+)")
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def _when(value: Any) -> datetime:
    if not isinstance(value, str):
        return _EPOCH
    text = value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
    text = _FRACTION_RE.sub(lambda m: "." + (m.group(1) + "000000")[:6], text, count=1)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return _EPOCH
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _status(value: Any) -> str:
    return value if isinstance(value, str) and value in STATUSES else "delivered"


def rebuild(
    rows: Sequence[Mapping[str, Any]],
    live_ids: Iterable[str],
    *,
    session: str = "",
    window: int = WINDOW_ROWS,
) -> Dict[str, Any]:
    """The ``history.schema.json`` document for one session.

    ``rows`` are inbox rows, newest first, as ``GET /terminals/{c}/inbox/messages?order=desc``
    returns them. Only ``sender_id``, ``created_at`` and ``status`` are read; no text is kept.
    """
    live: Set[str] = set(live_ids)
    window_rows = [r for r in list(rows)[:window] if isinstance(r, Mapping)]
    closed_rows: Dict[str, List[Mapping[str, Any]]] = {}
    external: Dict[str, int] = {}
    for item in window_rows:
        sender = item.get("sender_id")
        if not isinstance(sender, str) or not sender:
            continue
        if _ID_RE.fullmatch(sender):
            if sender not in live:
                closed_rows.setdefault(sender, []).append(item)
        else:
            external[sender] = external.get(sender, 0) + 1
    closed: List[Dict[str, Any]] = []
    for sender, group in closed_rows.items():
        group.sort(key=lambda r: _when(r.get("created_at")))
        closed.append(
            {
                "terminal_id": sender,
                "label": None,
                "rows": len(group),
                "first_at": str(group[0].get("created_at", ""))[:TIME_CAP],
                "last_at": str(group[-1].get("created_at", ""))[:TIME_CAP],
                "last_status": _status(group[-1].get("status")),
            }
        )
    closed.sort(key=lambda c: _when(c["last_at"]), reverse=True)
    return {
        "session": session,
        "source": "inbox_rows",
        "complete": len(window_rows) < window,
        "window_rows": window,
        "rows_seen": len(window_rows),
        "closed": closed,
        "external_senders": [{"sender_id": k, "rows": v} for k, v in sorted(external.items())],
    }


def ledger_entries(session: str, built: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """What the on-disk ledger keeps for each closed helper: ids, counts and times. No text."""
    return [
        {
            "session": session,
            "terminal_id": c["terminal_id"],
            "rows": c["rows"],
            "first_at": c["first_at"],
            "last_at": c["last_at"],
            "last_status": c["last_status"],
        }
        for c in built["closed"]
    ]


def merge_with_ledger(
    built: Mapping[str, Any],
    entries: Sequence[Mapping[str, Any]],
    *,
    live_ids: Iterable[str],
) -> Dict[str, Any]:
    """Add ledger-only helpers and widen counts and times of those the window also shows."""
    live: Set[str] = set(live_ids)
    session = str(built.get("session", ""))
    merged: Dict[str, Dict[str, Any]] = {c["terminal_id"]: dict(c) for c in built["closed"]}
    for entry in entries:
        tid = entry.get("terminal_id")
        if not isinstance(tid, str) or tid in live or entry.get("session") != session:
            continue
        current = merged.get(tid)
        if current is None:
            merged[tid] = {
                "terminal_id": tid,
                "label": None,
                "rows": int(entry.get("rows") or 1),
                "first_at": str(entry.get("first_at", "")),
                "last_at": str(entry.get("last_at", "")),
                "last_status": _status(entry.get("last_status")),
            }
            continue
        current["rows"] = max(int(current["rows"]), int(entry.get("rows") or 0))
        if _when(entry.get("first_at")) < _when(current["first_at"]):
            current["first_at"] = str(entry["first_at"])
        if _when(entry.get("last_at")) > _when(current["last_at"]):
            current["last_at"] = str(entry["last_at"])
            current["last_status"] = _status(entry.get("last_status"))
    out = dict(built)
    out["closed"] = sorted(merged.values(), key=lambda c: _when(c["last_at"]), reverse=True)
    return out


def row_text(raw: Optional[str]) -> str:
    """A row body prepared for the page: ANSI, control, bidi and zero-width characters out,
    cut at 2000 chars. Tab and newline stay."""
    if not isinstance(raw, str):
        return ""
    cleaned = strip_invisible(_ANSI_RE.sub("", raw))
    cleaned = "".join(ch for ch in cleaned if ch in "\t\n" or (ord(ch) >= 32 and ord(ch) != 127))
    return cleaned[:ROW_TEXT_CAP]
