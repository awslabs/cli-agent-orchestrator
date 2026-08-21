"""Tests for event_bus module."""

import asyncio
import logging
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.event_bus import EventBus


@pytest.fixture
def small_queue_settings():
    """Force a tiny queue so we can trigger drops deterministically."""
    with patch("cli_agent_orchestrator.services.event_bus.get_server_settings") as m:
        m.return_value = {
            "mcp_request_timeout": 30,
            "event_bus_max_queue_size": 4,
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
        }
        yield m


class TestEventBusSubscribe:
    @patch("cli_agent_orchestrator.services.event_bus.get_server_settings")
    def test_subscribe_uses_configured_queue_size(self, mock_settings):
        """subscribe() creates queue with size from server settings."""
        mock_settings.return_value = {
            "mcp_request_timeout": 30,
            "event_bus_max_queue_size": 4096,
            "provider_init_timeout": 60,
            "startup_prompt_handler_timeout": 20,
        }
        bus = EventBus()
        queue = bus.subscribe("terminal.*.output")
        assert queue.maxsize == 4096


class TestQueueFullRateLimit:
    """Regression: under a real output burst _dispatch was called thousands of
    times per second and every drop logged an ERROR. A production run
    accumulated 42,000+ 'Queue full' log lines in ~20 minutes, which
    contributed to loop starvation. We now rate-limit drop reporting to at
    most one message per topic per second.
    """

    @pytest.mark.asyncio
    async def test_drop_logs_are_rate_limited(self, small_queue_settings, caplog):
        """1000 drops on a single topic should produce ≤ 5 log lines, not 1000."""
        bus = EventBus()
        loop = asyncio.get_running_loop()
        bus.set_loop(loop)

        # Subscribe with a queue that fills after 4 events
        queue = bus.subscribe("terminal.aaa.output")

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.event_bus"):
            # Publish 1000 events without draining — first 4 fill the queue,
            # the remaining 996 all fail with QueueFull.
            for i in range(1000):
                bus.publish("terminal.aaa.output", {"data": f"chunk-{i}"})

            # Let call_soon_threadsafe scheduled callbacks run
            await asyncio.sleep(0.05)

        drop_logs = [r for r in caplog.records if "queue full" in r.getMessage().lower()]
        # 1 first-drop warning + at most a handful of periodic summaries.
        # We publish all 1000 within milliseconds, so the 1-second interval
        # should not fire more than once.
        assert 1 <= len(drop_logs) <= 3, (
            f"expected 1-3 rate-limited drop logs, got {len(drop_logs)}: "
            f"{[r.getMessage() for r in drop_logs]}"
        )

        # The queue itself should have exactly maxsize items, confirming
        # the drops actually happened.
        assert queue.qsize() == 4

    @pytest.mark.asyncio
    async def test_drop_summary_reports_dropped_count(self, small_queue_settings, caplog):
        """The summary log should include a numeric count so operators can see
        how bad the back-pressure got."""
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        bus.subscribe("terminal.bbb.output")

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.event_bus"):
            for i in range(200):
                bus.publish("terminal.bbb.output", {"data": "x"})
            await asyncio.sleep(0.05)

            # Force the summary window to elapse so the next drop logs a summary.
            # Use asyncio.sleep (not time.sleep) so the event loop keeps running
            # the EventBus's call_soon_threadsafe dispatches during the wait.
            await asyncio.sleep(1.05)
            for i in range(50):
                bus.publish("terminal.bbb.output", {"data": "x"})
            await asyncio.sleep(0.05)

        drop_logs = [r for r in caplog.records if "queue full" in r.getMessage().lower()]
        summary_logs = [r for r in drop_logs if "dropped" in r.getMessage()]
        assert summary_logs, (
            f"expected at least one 'dropped N events' summary line, got: "
            f"{[r.getMessage() for r in drop_logs]}"
        )

    @pytest.mark.asyncio
    async def test_first_drop_per_topic_still_logs_immediately(self, small_queue_settings, caplog):
        """We must not silence the first drop for a topic — operators need to
        know back-pressure has started."""
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        bus.subscribe("terminal.ccc.output")

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.event_bus"):
            # 5 events with queue size 4 — 1 will drop
            for i in range(5):
                bus.publish("terminal.ccc.output", {"data": "x"})
            await asyncio.sleep(0.05)

        drop_logs = [r for r in caplog.records if "queue full" in r.getMessage().lower()]
        assert len(drop_logs) >= 1, "first drop must always log immediately"

    @pytest.mark.asyncio
    async def test_drops_on_different_topics_are_reported_separately(
        self, small_queue_settings, caplog
    ):
        """Two terminals overflowing simultaneously should each get their own
        first-drop log, not be conflated."""
        bus = EventBus()
        bus.set_loop(asyncio.get_running_loop())
        bus.subscribe("terminal.ddd.output")
        bus.subscribe("terminal.eee.output")

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.services.event_bus"):
            for i in range(10):
                bus.publish("terminal.ddd.output", {"data": "x"})
                bus.publish("terminal.eee.output", {"data": "x"})
            await asyncio.sleep(0.05)

        messages = [
            r.getMessage() for r in caplog.records if "queue full" in r.getMessage().lower()
        ]
        assert any("terminal.ddd.output" in m for m in messages)
        assert any("terminal.eee.output" in m for m in messages)


class TestDropStatePruning:
    """The per-topic drop-state maps must stay bounded on a long-running server
    that churns through many short-lived terminals (topics embed terminal IDs).
    """

    def test_stale_topics_are_evicted_once_over_cap(self):
        """Once the map exceeds the cap, a new topic triggers eviction of
        entries older than the TTL; fresh entries are retained."""
        import cli_agent_orchestrator.services.event_bus as eb

        bus = EventBus()
        # Seed the map past the cap. Pin the clock so the seeded last-logged
        # timestamps are unambiguously older than the TTL (time.monotonic()'s
        # origin is arbitrary, so absolute values like 1.0 can't be assumed
        # "old" — control the clock instead).
        seeded_at = 1000.0
        for i in range(eb._DROP_STATE_MAX_TOPICS):
            topic = f"terminal.stale-{i}.output"
            bus._drop_last_logged[topic] = seeded_at
            bus._drop_counts[topic] = 1

        assert len(bus._drop_last_logged) == eb._DROP_STATE_MAX_TOPICS

        # A brand-new topic dropping well past the TTL: every seeded entry is
        # stale and gets evicted before the new one registers.
        now = seeded_at + eb._DROP_STATE_TTL_SECS + 1.0
        with patch("cli_agent_orchestrator.services.event_bus.time.monotonic", return_value=now):
            bus._record_drop("terminal.fresh.output")

        # All stale entries gone; only the fresh first-drop remains (its count
        # is reset to 0 by the first-drop path, and it's tracked in last_logged).
        assert not any(k.startswith("terminal.stale-") for k in bus._drop_last_logged)
        assert "terminal.fresh.output" in bus._drop_last_logged
        assert len(bus._drop_last_logged) == 1

    def test_fresh_entries_survive_pruning(self):
        """Eviction only removes entries idle past the TTL — entries touched
        recently are kept even when the map is over the cap."""
        import cli_agent_orchestrator.services.event_bus as eb

        bus = EventBus()
        now = 1000.0
        # Half stale (well past TTL), half fresh (touched "now").
        half = eb._DROP_STATE_MAX_TOPICS // 2
        for i in range(half):
            bus._drop_last_logged[f"terminal.stale-{i}.output"] = now - eb._DROP_STATE_TTL_SECS - 1
            bus._drop_counts[f"terminal.stale-{i}.output"] = 1
        for i in range(eb._DROP_STATE_MAX_TOPICS - half):
            bus._drop_last_logged[f"terminal.fresh-{i}.output"] = now
            bus._drop_counts[f"terminal.fresh-{i}.output"] = 1

        with patch("cli_agent_orchestrator.services.event_bus.time.monotonic", return_value=now):
            bus._record_drop("terminal.new.output")

        # Stale evicted, fresh retained.
        assert not any(k.startswith("terminal.stale-") for k in bus._drop_last_logged)
        assert sum(k.startswith("terminal.fresh-") for k in bus._drop_last_logged) == (
            eb._DROP_STATE_MAX_TOPICS - half
        )

    def test_no_pruning_below_cap(self):
        """Below the cap the maps are never pruned, even with old entries —
        pruning cost is only paid once the map has actually grown."""
        bus = EventBus()
        bus._drop_last_logged["terminal.old.output"] = 1.0
        bus._drop_counts["terminal.old.output"] = 1

        bus._record_drop("terminal.new.output")

        # Old entry survives (map was below cap, so _prune_drop_state not called).
        assert "terminal.old.output" in bus._drop_last_logged
