"""claim_terminal_async keeps the placement DB read off the frame loop (#745).

``claim_terminal`` reads the durable placement row synchronously; called from
the channel's single frame reader that runs on the event loop, that SQLite read
blocks the loop. The async variant makes the same decision but runs the cold
read in ``asyncio.to_thread`` and re-checks the binding under the lock after
the read returns.
"""

import threading

import pytest

from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

TID = "abcd1234"


@pytest.mark.asyncio
async def test_the_cold_claim_reads_placement_off_the_loop(monkeypatch):
    reg = RuntimeChannelRegistry()
    loop_thread = threading.get_ident()
    seen = {}

    def fake_meta(tid):
        seen["thread"] = threading.get_ident()
        return None  # absent row -> the claim is allowed

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", fake_meta)

    assert await reg.claim_terminal_async(TID, "worker-1") is True
    assert reg.runtime_for_terminal(TID) == "worker-1"
    assert seen["thread"] != loop_thread, "the placement read ran on the loop thread"


@pytest.mark.asyncio
async def test_a_continuation_needs_no_db_read(monkeypatch):
    reg = RuntimeChannelRegistry()
    reg.bind_terminal(TID, "worker-1")

    def explode(tid):
        raise AssertionError("the DB must not be read for a continuation")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", explode)
    assert await reg.claim_terminal_async(TID, "worker-1") is True


@pytest.mark.asyncio
async def test_a_tombstoned_terminal_is_refused_without_a_db_read(monkeypatch):
    reg = RuntimeChannelRegistry()
    reg.unbind_terminal(TID, deleted=True)

    def explode(tid):
        raise AssertionError("the DB must not be read for a tombstoned id")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", explode)
    assert await reg.claim_terminal_async(TID, "worker-1") is False


@pytest.mark.asyncio
async def test_a_terminal_bound_elsewhere_is_refused_without_a_db_read(monkeypatch):
    reg = RuntimeChannelRegistry()
    reg.bind_terminal(TID, "worker-2")

    def explode(tid):
        raise AssertionError("the DB must not be read when already bound elsewhere")

    monkeypatch.setattr("cli_agent_orchestrator.clients.database.get_terminal_metadata", explode)
    assert await reg.claim_terminal_async(TID, "worker-1") is False
    assert reg.runtime_for_terminal(TID) == "worker-2"


@pytest.mark.asyncio
async def test_a_row_naming_another_runtime_is_refused(monkeypatch):
    reg = RuntimeChannelRegistry()
    monkeypatch.setattr(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        lambda tid: {"metadata": {"runtime_id": "worker-2"}},
    )
    assert await reg.claim_terminal_async(TID, "worker-1") is False
