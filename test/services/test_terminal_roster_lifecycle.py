"""M3-A / cond-0377: unmanaged-terminal roster lifecycle at create time.

Covers the two PR #91 review repairs on the unmanaged launch seam:

- ``i-0003`` (P1): a launch that fails AFTER the roster bind must retire
  the exact committed incarnation during unwind, so a dead terminal is
  never left live in the roster and the agent can be reincarnated.
- ``i-0015`` (P2): the standalone synchronous roster bind runs OFF the
  asyncio event loop (``asyncio.to_thread``) while still being awaited
  before any task input or a successful return.

The full mock stack mirrors ``test_terminal_service_full``'s
``TestCreateTerminal``; the roster store is real via
``isolated_memory_db`` so binds/retirements are durable.
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.terminal_service import create_terminal


@pytest.fixture(autouse=True)
def _patch_clear_session_env():
    """These tests exercise create_terminal orchestration, not the env
    store; stub the strict new-session pre-clear."""
    with patch("cli_agent_orchestrator.services.terminal_service.clear_session_env"):
        yield


@pytest.fixture
def launch_mocks(monkeypatch):
    """Configure the mock stack for a successful unmanaged launch and a
    durable roster row."""
    from cli_agent_orchestrator.services import terminal_service as ts

    with (
        patch("cli_agent_orchestrator.services.terminal_service.status_monitor") as status_monitor,
        patch("cli_agent_orchestrator.services.terminal_service.fifo_manager") as fifo_manager,
        patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR") as fifo_dir,
        patch(
            "cli_agent_orchestrator.services.terminal_service.provider_manager"
        ) as provider_manager,
        patch(
            "cli_agent_orchestrator.services.terminal_service.db_create_terminal"
        ) as db_create_terminal,
        patch("cli_agent_orchestrator.backends.registry._backend") as tmux,
        patch(
            "cli_agent_orchestrator.services.terminal_service.generate_window_name"
        ) as gen_window,
        patch(
            "cli_agent_orchestrator.services.terminal_service.generate_session_name"
        ) as gen_session,
        patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id") as gen_id,
        patch(
            "cli_agent_orchestrator.services.terminal_service.load_agent_profile"
        ) as load_profile,
    ):
        gen_id.return_value = "test1234"
        gen_session.return_value = "cao-session"
        gen_window.return_value = "developer-abcd"
        tmux.session_exists.return_value = False
        tmux.window_identity.return_value = {
            "pane_id": "%88",
            "window_id": "w88",
            "server_socket_path": "/tmp/cao-roster.sock",
            "session_id": "1",
            "pane_pid": 4321,
        }
        load_profile.return_value = AgentProfile(name="developer", description="Developer")
        fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        provider_manager.create_provider.return_value = mock_provider
        monkeypatch.setattr(
            ts, "get_terminal_metadata", lambda terminal_id: {"provider": "claude_code"}
        )
        yield {
            "tmux": tmux,
            "provider_manager": provider_manager,
            "db_create_terminal": db_create_terminal,
        }


@pytest.mark.asyncio
async def test_failed_unmanaged_launch_retires_bound_incarnation(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """i-0003: a launch that fails AFTER the roster bind retires the exact
    committed incarnation, so the failed terminal is not left live and the
    same agent can be reincarnated on a fresh terminal."""
    launch_mocks["provider_manager"].create_provider.side_effect = RuntimeError("init boom")

    with pytest.raises(RuntimeError, match="init boom"):
        await create_terminal("claude_code", "developer", new_session=True)

    agent_id = roster.derive_initial_agent_id("test1234")
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 1
    incarnation = incarnations[0]
    assert incarnation["terminal_id"] == "test1234"
    assert incarnation["disposition"] == roster.INCARNATION_RETIRED
    assert incarnation["retirement_reason"] == "terminal teardown"
    agent = roster.get_agent(agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED

    # The same stable agent may reincarnate on a fresh terminal.
    renewed = roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name="cao-session",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id="11111111-2222-4333-8444-555555555555",
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-0000000000aa",
        )
    )
    assert renewed["agent"]["agent_id"] == agent_id
    assert renewed["incarnation"]["disposition"] == roster.INCARNATION_BOUND
    assert len(roster.list_incarnations(agent_id=agent_id)) == 2


@pytest.mark.asyncio
async def test_unmanaged_roster_bind_runs_off_the_event_loop(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """i-0015: the standalone synchronous roster bind executes on a worker
    thread via ``asyncio.to_thread``, never on the event-loop thread, and
    is awaited before the terminal is returned."""
    loop_thread = threading.current_thread()
    captured: dict = {}

    def _recorder(contract, db=None):
        captured["thread"] = threading.current_thread()
        captured["contract"] = contract
        return {
            "agent": {"agent_id": contract.agent_id},
            "lineage": {"lineage_id": "x"},
            "incarnation": {"incarnation_id": "y"},
            "adopted": True,
        }

    monkeypatch.setattr(roster, "bind_generation", _recorder)

    result = await create_terminal("claude_code", "developer", new_session=True)
    assert result.id == "test1234"
    assert captured["contract"].agent_id == roster.derive_initial_agent_id("test1234")
    assert captured["thread"] is not loop_thread
    # The event loop remained free while the bind ran in a worker.
    assert asyncio.get_running_loop() is not None


@pytest.mark.asyncio
async def test_cancelled_unmanaged_bind_retires_committed_incarnation(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """repair-3 P1-1: cancelling create_terminal while the off-thread bind
    is in flight must not orphan a live incarnation.  Cleanup/lock
    ownership is held until the worker reaches a known outcome; if it
    committed, the exact incarnation is retired during the cancellation
    teardown and no terminal row remains.  No late write occurs after
    cancellation returns."""
    real_bind = roster.bind_generation
    entered = threading.Event()
    release = threading.Event()

    def _blocking_bind(contract, db=None):
        entered.set()
        release.wait(timeout=10)
        return real_bind(contract, db=db)

    monkeypatch.setattr(roster, "bind_generation", _blocking_bind)

    task = asyncio.create_task(create_terminal("claude_code", "developer", new_session=True))
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set(), "the off-thread bind never started"

    task.cancel()
    await asyncio.sleep(0.05)  # cancellation delivered; handler waits for the worker
    assert not task.done(), "cleanup/ownership must be held while the worker can still commit"

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    agent_id = roster.derive_initial_agent_id("test1234")
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    agent = roster.get_agent(agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    # The terminal row delete ran (cleanup executed).
    launch_mocks["db_create_terminal"].assert_called()

    # No late write after cancellation returns.
    await asyncio.sleep(0.1)
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED


@pytest.mark.asyncio
async def test_repeated_cancellation_of_off_thread_bind_converges(
    isolated_memory_db, launch_mocks, monkeypatch
):
    """repair-3 P1-1: repeated cancellation while the worker is in flight
    is tolerated — the handler keeps waiting for the worker, then retires
    the committed incarnation and re-raises the cancellation."""
    real_bind = roster.bind_generation
    entered = threading.Event()
    release = threading.Event()

    def _blocking_bind(contract, db=None):
        entered.set()
        release.wait(timeout=10)
        return real_bind(contract, db=db)

    monkeypatch.setattr(roster, "bind_generation", _blocking_bind)

    task = asyncio.create_task(create_terminal("claude_code", "developer", new_session=True))
    for _ in range(400):
        if entered.is_set():
            break
        await asyncio.sleep(0.01)
    assert entered.is_set()

    task.cancel()
    await asyncio.sleep(0.02)
    task.cancel()  # repeated cancellation while the handler waits
    await asyncio.sleep(0.02)
    assert not task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert task.cancelled()

    agent_id = roster.derive_initial_agent_id("test1234")
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 1
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    assert roster.get_agent(agent_id)["disposition"] == roster.DISPOSITION_DORMANT
