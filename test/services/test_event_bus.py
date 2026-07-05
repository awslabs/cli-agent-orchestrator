"""Tests for event_bus module."""

import asyncio
import logging
import time
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

            # Force the summary window to elapse so the next drop logs a summary
            time.sleep(1.05)
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
