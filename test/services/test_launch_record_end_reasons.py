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
from cli_agent_orchestrator.services import ephemeral_service, terminal_service
from cli_agent_orchestrator.utils import atomic_file


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
async def test_herdr_pane_closed_db_outage_confirmed_loss_records_pane_closed(
    release_runtime, monkeypatch
):
    """The DB-outage branch of ``_handle_lifecycle_event`` --
    ``get_terminal_metadata`` raises, herdr's own snapshot confirms the pane
    is really gone, and there is no deferred-failure tombstone to retain --
    tears the terminal down with the same reason as the normal path."""
    from cli_agent_orchestrator.clients import database as database_module
    from cli_agent_orchestrator.services import herdr_inbox_service

    env, _, _, _ = release_runtime
    await launch_runtime(release_runtime)
    service = herdr_inbox_service.HerdrInboxService(socket_path=str(env[2] / "fake.sock"))
    service._pane_to_terminal = {"child": CHILD, "parent": CALLER}
    service._terminal_to_pane = {CHILD: "child", CALLER: "parent"}

    real_get_terminal_metadata = database_module.get_terminal_metadata
    calls = []

    def _boom_once(terminal_id):
        calls.append(terminal_id)
        if len(calls) == 1:
            raise RuntimeError("database is locked")
        return real_get_terminal_metadata(terminal_id)

    monkeypatch.setattr(database_module, "get_terminal_metadata", _boom_once)
    monkeypatch.setattr(service, "_fetch_snapshot", lambda: {"panes": []})

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
async def test_teardown_before_the_record_write_on_the_deferred_path_finalizes_the_record(
    release_runtime, monkeypatch
):
    """The row commits, a teardown removes it before the launch record is
    written, then the init fails. The failure path skips a missing row, so
    the create's re-read of the row is what closes the record."""
    _, name, _, factory = release_runtime
    factory.return_value.initialize.side_effect = RuntimeError("launch failed")
    schedule = terminal_service._schedule_deferred_init
    owned = []

    def capture(*args, **kwargs):
        task = schedule(*args, **kwargs)
        owned.append(task)
        return task

    bind = terminal_service._bind_decision_launch
    torn_down = []

    def teardown_then_bind(launch_plan, **kwargs):
        if kwargs.get("launch_status") == "launched":
            torn_down.append(database.delete_terminal(CHILD, end_reason="deleted"))
            torn_down.append(database.get_launch_record(CHILD))
        return bind(launch_plan, **kwargs)

    monkeypatch.setattr(terminal_service, "_schedule_deferred_init", capture)
    monkeypatch.setattr(terminal_service, "_bind_decision_launch", teardown_then_bind)
    await terminal_service.create_terminal(
        "claude_code",
        name,
        session_name="cao-session",
        caller_id=CALLER,
        initial_message="inspect",
        defer_init=True,
    )
    assert torn_down == [True, None]  # the row was gone before any record existed
    await asyncio.wait_for(owned[0], timeout=10)

    assert database.get_terminal_metadata(CHILD) is None
    _ended(terminal_service._UNSTAMPED_TEARDOWN_END_REASON)


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
async def test_a_raising_finalize_never_fails_the_deferred_failure_path(
    release_runtime, monkeypatch
):
    """The failure path still finishes (row retained, owner notified) when
    the ``launch_failed`` finalize raises; the record simply stays open."""
    await launch_runtime(release_runtime)
    notified = []

    def raising_finalize(terminal_ids, end_reason):
        raise RuntimeError("db is down")

    monkeypatch.setattr(terminal_service, "db_finalize_launch_records", raising_finalize)
    monkeypatch.setattr(
        terminal_service,
        "_notify_caller_of_deferred_failure",
        lambda terminal_id, message, *args, **kwargs: notified.append(terminal_id),
    )

    owned = await terminal_service._surface_deferred_init_failure(
        CHILD,
        kind="provider_init_error",
        message="boom",
        registry=None,
        delete_on_failure=False,
    )

    assert owned is False
    assert notified == [CHILD]
    assert database.get_terminal_metadata(CHILD) is not None
    assert database.get_launch_record(CHILD)["ended_at"] is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize("startup", [True, False])
async def test_ephemeral_sweep_leaves_a_live_launch_open(release_runtime, monkeypatch, startup):
    """The sweep never collects a launched name whose terminal still exists,
    on a startup pass or past the launch grace on a periodic pass."""
    env, name, _, _ = release_runtime
    monkeypatch.setattr(atomic_file, "LOCK_DIR", env[2] / "locks")
    await launch_runtime(release_runtime)
    if not startup:
        monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=601))

    assert ephemeral_service.sweep(startup=startup) == {}

    assert database.get_ephemeral_agent(name)["state"] == "launched"
    assert database.get_launch_record(CHILD)["ended_at"] is None


@pytest.mark.asyncio
async def test_ephemeral_sweep_periodic_past_grace_collects_a_gone_terminal(
    release_runtime, monkeypatch
):
    """Companion control for the test above: past the grace, a periodic
    pass does collect a name whose terminal is actually gone, and ending the
    launch earlier keeps that end_reason (first reason wins)."""
    env, name, _, _ = release_runtime
    monkeypatch.setattr(atomic_file, "LOCK_DIR", env[2] / "locks")
    await launch_runtime(release_runtime)
    with monkeypatch.context() as lost:
        lost.setattr(ephemeral_service, "release", lambda *args: None)
        assert terminal_service.delete_terminal(CHILD, end_reason="deleted")
    ended_at = _ended("deleted")["ended_at"]
    monkeypatch.setattr(database, "_utcnow", lambda: NOW + timedelta(seconds=601))

    assert ephemeral_service.sweep(startup=False).get("terminal_gone") == 1

    assert database.get_ephemeral_agent(name)["gc_reason"] == "terminal_gone"
    assert _ended("deleted")["ended_at"] == ended_at


@pytest.mark.asyncio
async def test_sweep_after_a_lost_release_keeps_the_teardown_end_reason(
    release_runtime, monkeypatch
):
    """Teardown ends the launch; when its best-effort release is lost, the
    sweep collects the name as terminal_gone without relabelling the launch."""
    env, name, _, _ = release_runtime
    monkeypatch.setattr(atomic_file, "LOCK_DIR", env[2] / "locks")
    await launch_runtime(release_runtime)
    with monkeypatch.context() as lost:
        lost.setattr(ephemeral_service, "release", lambda *args: None)
        assert terminal_service.delete_terminal(CHILD, end_reason="deleted")
    ended_at = _ended("deleted")["ended_at"]
    assert database.get_ephemeral_agent(name)["state"] == "launched"

    assert ephemeral_service.sweep(startup=True).get("terminal_gone") == 1

    assert database.get_ephemeral_agent(name)["gc_reason"] == "terminal_gone"
    assert _ended("deleted")["ended_at"] == ended_at


@pytest.mark.asyncio
async def test_policy_changed_collection_writes_no_launch_record(release_runtime):
    """A name collected as policy_changed is refused before any terminal exists."""
    env, name, backend, factory = release_runtime
    env[4]["ephemeral"]["allowed_providers"] = ["codex"]

    with pytest.raises(env[0].EphemeralPolicyError) as refused:
        await launch_runtime(release_runtime)

    assert refused.value.rule == "policy_changed_since_create:provider_not_allowed"
    assert database.get_ephemeral_agent(name)["gc_reason"] == "policy_changed"
    assert database.get_launch_record(CHILD) is None
    backend.create_window.assert_not_called()
    factory.assert_not_called()
