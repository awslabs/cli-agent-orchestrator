"""Tests for the event-log ring buffer (services/event_log_service.py).

Hypothesis property tests for the ring-buffer bound and history ordering, plus
unit tests for TTL eviction, kind/since filtering, the privacy boundary, and the
singleton accessor.
"""

from datetime import datetime, timedelta, timezone

from hypothesis import given
from hypothesis import strategies as st

from cli_agent_orchestrator.services.event_log_service import (
    RING_CAPACITY,
    EventLog,
    get_event_log,
)
from cli_agent_orchestrator.services.event_primitives import PRIMITIVES


def _fill(log: EventLog, count: int) -> None:
    """Append ``count`` synthetic events to the log."""

    for i in range(count):
        log.append("launch", f"term-{i}", f"sess-{i}", {"event_type": "post_create_terminal"})


class TestRingBufferBoundAndOrder:
    """Properties 1 (ring-buffer bound) and 2 (history order).

    **Validates: Requirements 3.1, 3.2, 3.5, 3.6**
    """

    @given(
        n=st.integers(min_value=0, max_value=1200),
        limit=st.integers(min_value=0, max_value=2000),
    )
    def test_history_returns_at_most_min_limit_capacity(self, n: int, limit: int) -> None:
        """Property 1: history(limit) returns at most min(limit, 500) events."""

        log = EventLog()
        _fill(log, n)

        # The buffer itself never exceeds the cap.
        assert len(log) <= RING_CAPACITY

        result = log.history(limit=limit)
        assert len(result) <= min(limit, RING_CAPACITY)
        # And never more than what was actually retained.
        assert len(result) <= min(n, RING_CAPACITY)

    @given(n=st.integers(min_value=0, max_value=900))
    def test_history_is_non_decreasing_in_timestamp(self, n: int) -> None:
        """Property 2: events are returned in non-decreasing timestamp order."""

        log = EventLog()
        _fill(log, n)

        result = log.history()
        timestamps = [e["timestamp"] for e in result]
        assert timestamps == sorted(timestamps)

    def test_buffer_never_exceeds_capacity_after_overfill(self) -> None:
        """Property 1: appending well past the cap keeps len == capacity."""

        log = EventLog()
        _fill(log, RING_CAPACITY + 250)
        assert len(log) == RING_CAPACITY
        assert len(log.history(limit=10_000)) == RING_CAPACITY


class TestHistoryFiltering:
    """Unit tests for TTL, kinds, and since filters."""

    def test_kinds_filter_returns_only_requested_kinds(self) -> None:
        log = EventLog()
        log.append("launch", "t1", None, {})
        log.append("error", "t2", None, {})
        log.append("handoff", "t3", None, {})

        result = log.history(kinds=["launch", "handoff"])
        assert {e["kind"] for e in result} == {"launch", "handoff"}

    def test_since_filter_excludes_at_or_before_marker(self) -> None:
        log = EventLog()
        first = log.append("launch", "t1", None, {})
        log.append("launch", "t2", None, {})

        result = log.history(since=first["timestamp"])
        # Strictly greater than the marker, so the first event is excluded.
        assert all(e["timestamp"] > first["timestamp"] for e in result)
        assert first["id"] not in {e["id"] for e in result}

    def test_ttl_excludes_events_older_than_24h(self) -> None:
        log = EventLog()
        # Manually inject a stale event past the 24h TTL.
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with log._lock:  # noqa: SLF001 - test reaches in to simulate an aged row
            log._buf.append(
                {
                    "id": "stale",
                    "kind": "launch",
                    "terminal_id": "old",
                    "session_name": None,
                    "timestamp": stale_ts,
                    "detail": {},
                }
            )
        log.append("launch", "fresh", None, {})

        result = log.history()
        ids = {e["id"] for e in result}
        assert "stale" not in ids
        assert "fresh" in {e["terminal_id"] for e in result}


class TestEventShapeAndPrivacy:
    """Unit tests for stored event shape and the privacy boundary."""

    def test_append_returns_row_with_required_fields(self) -> None:
        log = EventLog()
        event = log.append("launch", "t1", "s1", {"event_type": "post_create_terminal"})
        assert set(event) == {
            "id",
            "kind",
            "terminal_id",
            "session_name",
            "timestamp",
            "detail",
        }
        assert event["kind"] == "launch"
        assert event["terminal_id"] == "t1"
        assert event["session_name"] == "s1"

    def test_detail_stores_only_what_is_given(self) -> None:
        """The buffer stores metadata verbatim; no message body is introduced."""

        log = EventLog()
        detail = {"event_type": "post_send_message", "sender": "a", "receiver": "b"}
        event = log.append("handoff", "b", None, detail)
        assert event["detail"] == detail
        assert "message" not in event["detail"]

    def test_stored_kinds_are_within_vocabulary(self) -> None:
        log = EventLog()
        for kind in PRIMITIVES + ("other",):
            log.append(kind, "t", None, {})
        assert {e["kind"] for e in log.history()} <= set(PRIMITIVES + ("other",))


class TestSingleton:
    """Unit tests for the module-level singleton accessor."""

    def test_get_event_log_returns_same_instance(self) -> None:
        assert get_event_log() is get_event_log()


class TestSinceFilterIsoForms:
    """The ``since`` lower bound is compared chronologically, not lexically.

    Lexicographic comparison of ISO-8601 is unreliable across equally-valid
    forms (``Z`` vs ``+00:00``, differing fractional precision); these assert the
    exclusive boundary is honored regardless of the form the caller supplies, and
    that an unparseable cursor is ignored rather than filtering everything out
    (Copilot C2).

    **Validates: Copilot review (C2)**
    """

    @staticmethod
    def _append_at(log: EventLog, ts: str) -> dict:
        # Append, then pin the stored timestamp so ordering is deterministic.
        ev = log.append("launch", "t", "s", {"event_type": "post_create_terminal"})
        ev["timestamp"] = ts
        return ev

    def test_since_z_form_excludes_boundary_event(self) -> None:
        log = EventLog()
        now = datetime.now(timezone.utc).replace(microsecond=0)
        boundary = now - timedelta(minutes=2)
        e1 = self._append_at(log, boundary.isoformat())  # ...+00:00, == boundary
        e2 = self._append_at(log, (now - timedelta(minutes=1)).isoformat())  # later

        # Caller supplies the boundary in ``Z`` form; the equal event is excluded
        # (strict >) and the later event is included.
        since_z = boundary.isoformat().replace("+00:00", "Z")
        ids = [e["id"] for e in log.history(since=since_z)]
        assert e1["id"] not in ids
        assert e2["id"] in ids

    def test_since_fractional_precision(self) -> None:
        log = EventLog()
        now = datetime.now(timezone.utc)
        earlier = self._append_at(
            log, (now - timedelta(seconds=3)).replace(microsecond=100000).isoformat()
        )
        later = self._append_at(
            log, (now - timedelta(seconds=1)).replace(microsecond=900000).isoformat()
        )
        boundary = (now - timedelta(seconds=2)).replace(microsecond=500000).isoformat()

        ids = [e["id"] for e in log.history(since=boundary)]
        assert earlier["id"] not in ids
        assert later["id"] in ids

    def test_unparseable_since_is_ignored(self) -> None:
        log = EventLog()
        recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        ev = self._append_at(log, recent)
        # A garbage cursor must neither raise nor filter everything out.
        ids = [e["id"] for e in log.history(since="not-a-timestamp")]
        assert ev["id"] in ids


class TestEventLogEdgeCases:
    """Naive-timestamp coercion, malformed rows, and the reset helper."""

    def test_naive_since_is_treated_as_utc(self) -> None:
        # A naive ``since`` (no tzinfo) must be coerced to UTC and still filter
        # chronologically without raising.
        log = EventLog()
        now = datetime.now(timezone.utc)
        old = log.append("launch", None, None, {})
        log._buf[-1]["timestamp"] = (now - timedelta(hours=1)).isoformat()
        naive_cursor = (now - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        ids = [e["id"] for e in log.history(since=naive_cursor)]
        assert old["id"] not in ids  # predates the (UTC-coerced) cursor

    def test_malformed_stored_timestamp_is_skipped(self) -> None:
        log = EventLog()
        log.append("launch", None, None, {})
        log._buf[-1]["timestamp"] = "not-a-timestamp"  # ValueError branch
        assert log.history() == []

    def test_missing_stored_timestamp_is_skipped(self) -> None:
        log = EventLog()
        log.append("launch", None, None, {})
        del log._buf[-1]["timestamp"]  # KeyError branch
        assert log.history() == []

    def test_naive_stored_timestamp_is_treated_as_utc(self) -> None:
        log = EventLog()
        ev = log.append("launch", None, None, {})
        log._buf[-1]["timestamp"] = (
            (datetime.now() - timedelta(minutes=1)).replace(tzinfo=None).isoformat()
        )
        ids = [e["id"] for e in log.history()]
        assert ev["id"] in ids  # naive-but-recent row is retained

    def test_reset_event_log_drops_singleton(self) -> None:
        from cli_agent_orchestrator.services.event_log_service import reset_event_log

        first = get_event_log()
        reset_event_log()
        assert get_event_log() is not first


class TestAfterId:
    """``after_id`` — the id-based server cursor for ``Last-Event-ID`` reconnect.

    Powers the AG-UI stream's reconnect replay (PR #436, F1): given the last
    event id the client saw, return every fresh record strictly after it, in
    chronological order.
    """

    def test_returns_records_strictly_after_the_id(self) -> None:
        log = EventLog()
        e1 = log.append("launch", "t1", None, {})
        e2 = log.append("completion", "t2", None, {})
        e3 = log.append("handoff", "t3", None, {})

        result = log.after_id(e1["id"])
        ids = [e["id"] for e in result]
        # e1 itself is excluded (strictly after); e2, e3 follow in order.
        assert ids == [e2["id"], e3["id"]]

    def test_last_id_returns_empty(self) -> None:
        log = EventLog()
        log.append("launch", "t1", None, {})
        last = log.append("launch", "t2", None, {})
        assert log.after_id(last["id"]) == []

    def test_unknown_id_replays_all_fresh(self) -> None:
        """An id not in the buffer (evicted / never seen) replays everything
        currently retained — the client dedupes by id, so over-delivery is safe
        and avoids a silent gap."""
        log = EventLog()
        e1 = log.append("launch", "t1", None, {})
        e2 = log.append("launch", "t2", None, {})
        ids = [e["id"] for e in log.after_id("no-such-id")]
        assert ids == [e1["id"], e2["id"]]

    def test_ttl_is_applied(self) -> None:
        log = EventLog()
        stale_ts = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        with log._lock:  # noqa: SLF001
            log._buf.append(
                {
                    "id": "stale",
                    "kind": "launch",
                    "terminal_id": "old",
                    "session_name": None,
                    "timestamp": stale_ts,
                    "detail": {},
                }
            )
        fresh = log.append("launch", "t", None, {})
        # A stale id is not found among fresh rows -> replay all fresh, and the
        # stale row itself is TTL-excluded.
        ids = [e["id"] for e in log.after_id("stale")]
        assert ids == [fresh["id"]]

    def test_empty_log_returns_empty(self) -> None:
        assert EventLog().after_id("anything") == []
