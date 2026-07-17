"""In-process, bounded, time-windowed fleet event ring buffer.

This is the single source of truth for *historical replay* of CAO fleet events
in the MCP App. CAO's existing ``event_bus.py`` is live pub/sub only (no replay);
the SQLite store holds durable domain state, not a semantic event timeline. The
``EventLog`` fills that gap: a thread-safe ``deque`` capped at 500 events with a
24-hour TTL, expressing every row in the six-primitive vocabulary so the iframe
can re-hydrate its governance ticker after any re-mount.

Privacy boundary: the ``detail`` field stores **metadata only** — message bodies
are never persisted here. Callers (notably ``EventLogPublisher``) are responsible
for passing only metadata; this module never inspects or strips bodies, it simply
stores what it is given verbatim, so the contract is "give me metadata only".
"""

import threading
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Deque, Dict, List, Optional

# Maximum number of events retained in the ring buffer at any time.
RING_CAPACITY = 500

# Events older than this are excluded from ``history()`` results.
TTL = timedelta(hours=24)


class EventLog:
    """Thread-safe, bounded, time-windowed fleet event buffer.

    The buffer is a ``deque(maxlen=RING_CAPACITY)`` so appends past the cap
    evict the oldest event in O(1). A single lock guards all mutation and
    snapshotting; ``history()`` copies the buffer under the lock and then
    filters outside it to keep the critical section short.
    """

    def __init__(self) -> None:
        """Create an empty ring buffer."""

        self._buf: Deque[Dict] = deque(maxlen=RING_CAPACITY)
        self._lock = threading.Lock()

    def append(
        self,
        kind: str,
        terminal_id: Optional[str],
        session_name: Optional[str],
        detail: dict,
    ) -> Dict:
        """Append a normalized event and return the stored row.

        Args:
            kind: A semantic primitive (or ``"other"``) from the normalizer.
            terminal_id: The terminal the event relates to, if any.
            session_name: The session the event relates to, if any.
            detail: Metadata-only payload. Message bodies MUST NOT be passed.

        Returns:
            The stored event dict (including its generated ``id`` and
            ISO-8601 UTC ``timestamp``).
        """

        event: Dict = {
            "id": str(uuid.uuid4()),
            "kind": kind,
            "terminal_id": terminal_id,
            "session_name": session_name,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "detail": detail,  # metadata only — never message bodies
        }
        with self._lock:
            self._buf.append(event)
        return event

    def history(
        self,
        limit: int = RING_CAPACITY,
        since: Optional[str] = None,
        kinds: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Return recent events, newest-last, after TTL/kind/since filtering.

        Args:
            limit: Maximum number of events to return. The result is at most
                ``min(limit, RING_CAPACITY)`` since the buffer itself is bounded.
            since: If given, only events whose timestamp is strictly greater
                than this ISO-8601 value are returned. It is parsed once into an
                aware datetime and compared chronologically (not as a string):
                lexicographic comparison of ISO-8601 is unreliable across valid
                forms (``Z`` vs ``+00:00``, differing fractional precision). An
                unparseable ``since`` is ignored (no filter), consistent with the
                tolerant TTL handling that skips rows with bad timestamps.
            kinds: If given, only events whose ``kind`` is in this collection
                are returned.

        Returns:
            Events in non-decreasing timestamp order (insertion order, which is
            chronological because ``append`` stamps monotonically as it inserts).
        """

        cutoff = datetime.now(timezone.utc) - TTL

        # Parse the ``since`` lower bound once into an aware datetime so the
        # comparison below is chronological rather than lexicographic. ``Z`` is
        # normalized to ``+00:00`` for Python 3.10 (whose ``fromisoformat`` does
        # not accept the ``Z`` suffix); a naive value is assumed UTC.
        since_dt: Optional[datetime] = None
        if since is not None:
            try:
                since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                since_dt = None
            else:
                if since_dt.tzinfo is None:
                    since_dt = since_dt.replace(tzinfo=timezone.utc)

        with self._lock:
            items = list(self._buf)

        out: List[Dict] = []
        for event in items:
            # TTL filter: drop anything older than 24h.
            try:
                ts = datetime.fromisoformat(event["timestamp"])
            except (KeyError, ValueError):
                continue
            # Defensive: treat a naive stored timestamp as UTC so the comparisons
            # below never raise on aware/naive mismatch.
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts < cutoff:
                continue
            if kinds is not None and event["kind"] not in kinds:
                continue
            # Exclusive lower bound, compared as datetimes (see ``since_dt``).
            if since_dt is not None and ts <= since_dt:
                continue
            out.append(event)

        # ``limit <= 0`` returns nothing. NB: ``out[-0:]`` is the *whole* list,
        # so a naive ``out[-limit:]`` would wrongly return everything for
        # limit == 0 — slice from an explicit start index instead.
        if limit <= 0:
            return []
        if limit >= len(out):
            return out
        return out[len(out) - limit :]

    def after_id(self, event_id: str) -> List[Dict]:
        """Return fresh events strictly after the row with ``event_id``.

        Powers the AG-UI stream's ``Last-Event-ID`` reconnect replay (F1): given
        the last event id a client received, return every currently-retained
        record that follows it, in chronological (insertion) order, after TTL
        filtering.

        If ``event_id`` is not found among the fresh rows — it was evicted from
        the bounded buffer, aged out, or never existed — every fresh record is
        returned instead of nothing. Over-delivering is safe (the client dedupes
        by event id) and strictly better than a silent gap.

        Args:
            event_id: The client's last-seen event id (exclusive lower bound).

        Returns:
            Events in non-decreasing timestamp order, after ``event_id``.
        """

        # Reuse history()'s TTL/ordering guarantees (bounded by the ring
        # capacity), then slice after the id.
        fresh = self.history(limit=RING_CAPACITY)

        for i, event in enumerate(fresh):
            if event.get("id") == event_id:
                return fresh[i + 1 :]
        # Id not found among fresh rows: replay everything (client dedupes).
        return fresh

    def __len__(self) -> int:
        """Return the current number of buffered events (<= RING_CAPACITY)."""

        with self._lock:
            return len(self._buf)


_log: Optional[EventLog] = None
_log_lock = threading.Lock()


def get_event_log() -> EventLog:
    """Return the process-wide singleton ``EventLog`` (lazily created)."""

    global _log
    if _log is None:
        with _log_lock:
            if _log is None:
                _log = EventLog()
    return _log


def reset_event_log() -> None:
    """Drop the singleton (used by tests to start clean)."""

    global _log
    with _log_lock:
        _log = None
