"""Issue #1082: every teardown path stamps the launch record with WHY the launch
ended, through its real entry point (provisional vocabulary,
``database.LAUNCH_END_REASONS``). Reuses the ephemeral runtime fixtures that
already drive these same paths for ephemeral release, so the launch here is a
real ephemeral launch and its ``target_kind`` is checked for real too."""

import asyncio
from datetime import timedelta
from test.services.test_ephemeral_bind import (  # noqa: F401
    CHILD,
    launch_runtime,
    release_runtime,
    runtime_store,
)
from test.services.test_ephemeral_claim import NOW, claim, claimed_store  # noqa: F401
from test.services.test_ephemeral_service import CALLER, create_store  # noqa: F401
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.services import terminal_service


def _ended(reason):
    record = database.get_launch_record(CHILD)
    assert record is not None
    assert record["end_reason"] == reason
    assert record["ended_at"] is not None
    return record


@pytest.mark.asyncio
async def test_launch_record_of_a_real_ephemeral_launch(release_runtime):
    await launch_runtime(release_runtime)
    record = database.get_launch_record(CHILD)
    assert record["target_kind"] == "ephemeral"
    assert record["caller_id"] == CALLER
    assert record["provider"] == "claude_code"
    assert record["ended_at"] is None


@pytest.mark.asyncio
async def test_api_delete_records_deleted(release_runtime, monkeypatch):
    from cli_agent_orchestrator.api import main
    from cli_agent_orchestrator.plugins.registry import PluginRegistry

    monkeypatch.setattr(main.app.state, "plugin_registry", PluginRegistry(), raising=False)
    await launch_runtime(release_runtime)

    response = TestClient(main.app, base_url="http://localhost").delete("/terminals/" + CHILD)

    assert response.status_code == 200
    _ended("deleted")


@pytest.mark.asyncio
async def test_agent_step_teardown_records_deleted(release_runtime, monkeypatch):
    from cli_agent_orchestrator.services import agent_step

    env, name, _, _ = release_runtime
    token = claim(env, name)["claim_id"]
    monkeypatch.setattr(agent_step, "wait_until_status", AsyncMock(return_value=True))
    monkeypatch.setattr(agent_step, "_wait_for_completion", AsyncMock())
    monkeypatch.setattr(agent_step.frozen_run_memory, "frozen_memory_for", lambda *args: None)
    monkeypatch.setattr(terminal_service, "send_input", lambda *args, **kw: None)
    monkeypatch.setattr(terminal_service, "get_output", lambda *args: "done")

    result = await agent_step.run_agent_step(
        "claude_code", name, "inspect", session_name="cao-session", caller_id=CALLER, claim_id=token
    )

    assert result.last_message == "done"
    _ended("deleted")


@pytest.mark.asyncio
async def test_session_delete_records_session_teardown(release_runtime):
    from cli_agent_orchestrator.services import session_service

    await launch_runtime(release_runtime)
    assert "cao-session" in session_service.delete_session("cao-session")["deleted"]
    _ended("session_teardown")


@pytest.mark.asyncio
async def test_flow_recycle_records_session_teardown(release_runtime):
    """Flow recycling tears down through ``delete_terminals_by_ids`` only."""
    from cli_agent_orchestrator.services import flow_service

    await launch_runtime(release_runtime)
    flow_service._delete_recycled_terminal_rows([CHILD])
    assert database.get_terminal_metadata(CHILD) is None
    _ended("session_teardown")


@pytest.mark.asyncio
async def test_herdr_pane_closed_records_pane_closed(release_runtime, monkeypatch):
    from cli_agent_orchestrator.services import herdr_inbox_service

    env, _, _, _ = release_runtime
    await launch_runtime(release_runtime)
    service = herdr_inbox_service.HerdrInboxService(socket_path=str(env[2] / "fake.sock"))
    service._pane_to_terminal = {"child": CHILD, "parent": CALLER}
    service._terminal_to_pane = {CHILD: "child", CALLER: "parent"}
    monkeypatch.setattr(service, "_label_still_live", lambda _: False)

    service._handle_lifecycle_event("pane.closed", {"pane_id": "child"})

    _ended("pane_closed")


@pytest.mark.asyncio
async def test_herdr_ghost_delete_records_pane_closed(release_runtime):
    from cli_agent_orchestrator.services import herdr_inbox_service

    await launch_runtime(release_runtime)
    herdr_inbox_service._delete_ghost_terminal_row(CHILD)
    _ended("pane_closed")


@pytest.mark.asyncio
async def test_retention_sweep_records_stale_cleanup(release_runtime, monkeypatch):
    from types import SimpleNamespace

    from sqlalchemy import update

    from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome
    from cli_agent_orchestrator.services import cleanup_service

    env, _, backend, _ = release_runtime
    # The sweep only ages out a row whose runtime is provably gone; answer
    # from the fixture backend so no real tmux/herdr is ever asked.
    backend.cleanup_terminal_exact.return_value = SimpleNamespace(
        outcome=TerminalCleanupOutcome.ABSENT, detail=""
    )
    monkeypatch.setattr(cleanup_service, "get_backend", lambda: backend)
    await launch_runtime(release_runtime)
    with env[1]() as db:
        db.execute(
            update(database.TerminalModel)
            .where(database.TerminalModel.id == CHILD)
            .values(last_active=NOW - timedelta(days=30))
        )
        db.commit()
    monkeypatch.setattr(cleanup_service, "SessionLocal", env[1])

    cleanup_service.cleanup_old_data()

    _ended("stale_cleanup")


@pytest.mark.asyncio
async def test_session_recreate_purge_records_stale_cleanup(release_runtime):
    await launch_runtime(release_runtime)
    terminal_service._purge_stale_session_rows_for_recreate("cao-session")
    _ended("stale_cleanup")


@pytest.mark.asyncio
async def test_deferred_init_failure_records_launch_failed(release_runtime, monkeypatch):
    """The main worker path (every MCP assign): the record is written once
    init is scheduled, and the failed init ends it as ``launch_failed`` --
    not as an ordinary ``deleted``."""
    _, name, _, factory = release_runtime
    factory.return_value.initialize.side_effect = RuntimeError("launch failed")
    schedule = terminal_service._schedule_deferred_init
    owned = []

    def capture(*args, **kwargs):
        task = schedule(*args, **kwargs)
        owned.append(task)
        return task

    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", capture)
    await terminal_service.create_terminal(
        "claude_code",
        name,
        session_name="cao-session",
        caller_id=CALLER,
        initial_message="inspect",
        defer_init=True,
    )
    assert database.get_launch_record(CHILD)["ended_at"] is None  # written at schedule
    await asyncio.wait_for(owned[0], timeout=10)

    record = _ended("launch_failed")
    assert record["target_kind"] == "ephemeral"


@pytest.mark.asyncio
async def test_deferred_failure_retained_for_external_owner_records_launch_failed(
    release_runtime,
):
    """A failure whose row is kept for an external owner still ends the
    launch at the failure, so a later delete cannot relabel it."""
    await launch_runtime(release_runtime)

    owned = await terminal_service._surface_deferred_init_failure(
        CHILD,
        kind="provider_init_error",
        message="boom",
        registry=None,
        delete_on_failure=False,
    )

    assert owned is False
    assert database.get_terminal_metadata(CHILD) is not None  # row retained
    _ended("launch_failed")
    assert terminal_service.delete_terminal(CHILD, end_reason="deleted")
    _ended("launch_failed")  # first reason wins


@pytest.mark.asyncio
async def test_sync_init_failure_writes_no_record(release_runtime):
    """On the synchronous path the record is written only after init, so a
    rolled-back create leaves nothing behind."""
    env, name, _, factory = release_runtime
    token = claim(env, name)["claim_id"]
    factory.return_value.initialize.side_effect = RuntimeError("launch failed")
    with pytest.raises(RuntimeError):
        await terminal_service.create_terminal(
            "claude_code", name, session_name="cao-session", caller_id=CALLER, claim_id=token
        )
    assert database.get_launch_record(CHILD) is None
