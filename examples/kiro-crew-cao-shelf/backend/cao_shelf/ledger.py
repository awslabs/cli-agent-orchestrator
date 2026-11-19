"""``data/ledger.json``: ids, counts and times of closed helpers. Never any message text.

It lets history counts outlive the 100-row inbox window and CAO's 14-day inbox sweep. With
``persist_history`` off nothing is read and nothing is written.

Entries are keyed by session name only, so the file belongs to one CAO address: its header holds
the normalised ``cao_url`` it was written for. A file whose header names another address is renamed
aside (``ledger.json.<UTC time>.aside``) and a new one starts, so two CAOs that both have a session
called ``x`` never mix their history. A file without a header (written before the header existed)
is adopted by the address in use.
"""

from __future__ import annotations

import json
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

from .config import atomic_write_json, move_aside
from .history import _when

RETENTION = timedelta(days=90)
MAX_ENTRIES = 5000
_ID_RE = re.compile(r"[a-f0-9]{8}")
_STATUSES = ("pending", "delivered", "failed")


def _clean(entry: Any) -> Optional[Dict[str, Any]]:
    """A well-formed entry with only the known keys, or ``None``."""
    if not isinstance(entry, Mapping):
        return None
    tid, session = entry.get("terminal_id"), entry.get("session")
    rows = entry.get("rows")
    first, last = entry.get("first_at"), entry.get("last_at")
    if not (
        isinstance(tid, str) and _ID_RE.fullmatch(tid) and isinstance(session, str) and session
    ):
        return None
    if isinstance(rows, bool) or not isinstance(rows, int) or rows < 1:
        return None
    if not (isinstance(first, str) and isinstance(last, str)):
        return None
    status = entry.get("last_status")
    return {
        "session": session[:80],
        "terminal_id": tid,
        "rows": rows,
        "first_at": first[:40],
        "last_at": last[:40],
        "last_status": status if status in _STATUSES else "delivered",
    }


class Ledger:
    def __init__(self, path: Path, enabled: bool = True) -> None:
        self._path = path
        self.enabled = enabled
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str], Dict[str, Any]] = {}
        self._loaded = False
        self._url: Optional[str] = None

    def set_cao_url(self, url: str) -> None:
        """Say which CAO the entries belong to (the normalised address). A change drops what is
        in memory; the next read sets the file aside if it was written for another address."""
        with self._lock:
            if url != self._url:
                self._url = url
                self._entries = {}
                self._loaded = False

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            if not enabled:
                self._entries = {}
                self._loaded = False

    def _load(self) -> None:
        if self._loaded or not self.enabled:
            return
        self._loaded = True
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            items = data.get("entries") if isinstance(data, dict) else None
            if not isinstance(items, list):
                raise ValueError("not a ledger")
        except (OSError, ValueError):
            self._entries = {}
            if self._path.exists():
                self._save()  # a damaged file is replaced by an empty ledger
            return
        header = data.get("cao_url")
        if self._url is not None and isinstance(header, str) and header != self._url:
            self._set_aside()
            return
        for item in items[:MAX_ENTRIES]:
            clean = _clean(item)
            if clean:
                self._entries[(clean["session"], clean["terminal_id"])] = clean

    def _set_aside(self) -> None:
        """Keep a ledger written for another CAO address, out of the way."""
        try:
            move_aside(self._path, datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"))
        except OSError:
            pass  # unreadable either way: the next save replaces it
        self._entries = {}

    def _save(self) -> None:
        doc: Dict[str, Any] = {"version": 2, "entries": list(self._entries.values())}
        if self._url is not None:
            doc["cao_url"] = self._url
        atomic_write_json(self._path, doc)

    def entries_for(self, session: str) -> List[Dict[str, Any]]:
        with self._lock:
            if not self.enabled:
                return []
            self._load()
            return [dict(e) for (s, _), e in self._entries.items() if s == session]

    def count_for(self, session: str) -> int:
        return len(self.entries_for(session))

    def merge(self, entries: Iterable[Mapping[str, Any]], now: datetime) -> bool:
        """Add or widen entries, prune old ones, write only when something changed."""
        with self._lock:
            if not self.enabled:
                return False
            self._load()
            changed = False
            for raw in entries:
                new = _clean(raw)
                if new is None:
                    continue
                key = (new["session"], new["terminal_id"])
                old = self._entries.get(key)
                if old is None:
                    self._entries[key] = new
                    changed = True
                    continue
                merged = dict(old)
                merged["rows"] = max(old["rows"], new["rows"])
                if _when(new["first_at"]) < _when(old["first_at"]):
                    merged["first_at"] = new["first_at"]
                if _when(new["last_at"]) > _when(old["last_at"]):
                    merged["last_at"] = new["last_at"]
                    merged["last_status"] = new["last_status"]
                if merged != old:
                    self._entries[key] = merged
                    changed = True
            cutoff = now.astimezone(timezone.utc) - RETENTION
            for key in [k for k, e in self._entries.items() if _when(e["last_at"]) < cutoff]:
                del self._entries[key]
                changed = True
            while len(self._entries) > MAX_ENTRIES:
                oldest = min(self._entries, key=lambda k: _when(self._entries[k]["last_at"]))
                del self._entries[oldest]
                changed = True
            if changed:
                self._save()
            return changed
