"""The status monitor never does placement DB I/O on its event loop (#745).

``StatusMonitor.run`` used to call ``_belongs_to_a_runtime`` inline on the loop,
which reaches a synchronous SQLite ``get_terminal_metadata`` read on a cold
placement cache — blocking the loop that services every terminal's output
(haofeif #11 / Copilot status_monitor.py:203 on #802). The in-memory fast check
stays on the loop; the DB-backed placement check moves into the worker thread
alongside chunk processing.
"""

import asyncio
import threading

import pytest

from cli_agent_orchestrator.services.event_bus import bus
from cli_agent_orchestrator.services.status_monitor import StatusMonitor


@pytest.mark.asyncio
async def test_placement_check_offloaded_from_the_loop(monkeypatch):
    loop_thread = threading.get_ident()
    recorded = {}

    def fake_get_terminal_metadata(terminal_id):
        recorded["thread"] = threading.get_ident()
        # A remote row, so the terminal is classified remote and no local
        # processing follows — the read itself is what the test is about.
        return {"id": terminal_id, "metadata": {"runtime_id": "worker-1"}}

    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        fake_get_terminal_metadata,
    )

    loop = asyncio.get_running_loop()
    bus.set_loop(loop)
    monitor = StatusMonitor()
    task = asyncio.create_task(monitor.run())
    try:
        # Let run() reach its bus.subscribe() before we publish.
        await asyncio.sleep(0.05)
        # A terminal id not bound in any registry, so placement falls to the DB.
        bus.publish("terminal.qq4unique.output", {"data": {"data": "hello"}})
        for _ in range(100):
            if "thread" in recorded:
                break
            await asyncio.sleep(0.02)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        bus.set_loop(None)

    assert "thread" in recorded, "placement read never happened"
    assert recorded["thread"] != loop_thread, "placement DB read ran on the event loop thread"
