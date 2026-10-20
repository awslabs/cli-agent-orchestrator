"""Durable restart recovery must retry unreadable rows without losing failure truth."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service as ts


@pytest.fixture(autouse=True)
def isolated_recovery(isolated_memory_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path / "logs")


def pending(terminal_id):
    db.create_terminal(
        terminal_id,
        "cao-restart",
        f"window-{terminal_id}",
        "kimi_cli",
        deferred_init_external_owner=True,
    )


@pytest.mark.asyncio
async def test_mixed_restart_cohort_preserves_success_and_retries_only_unreadable_rows(monkeypatch):
    for terminal_id in ("unreadable", "failed", "complete", "interrupted"):
        pending(terminal_id)
    failure = {"kind": "provider_init_error", "message": "Original actionable provider error"}
    db.update_terminal_deferred_init_failure("failed", failure)
    assert ts._write_deferred_init_complete_fallback("complete")
    enumerate_pending = MagicMock(
        side_effect=[
            ["unreadable", "already-deleted", "failed", "complete", "interrupted"],
            ["unreadable", "failed", "interrupted"],
        ]
    )
    monkeypatch.setattr(
        ts, "list_pending_deferred_init_external_owner_terminal_ids", enumerate_pending
    )
    notify = MagicMock()
    monkeypatch.setattr(ts, "_notify_caller_of_deferred_failure", notify)
    monkeypatch.setattr(ts.status_monitor, "get_status", lambda terminal_id: TerminalStatus.ERROR)

    def read_with_one_outage(terminal_id):
        if terminal_id == "unreadable":
            raise OSError("temporary database read failure")
        return db.get_terminal_metadata(terminal_id)

    monkeypatch.setattr(ts, "get_terminal_metadata", read_with_one_outage)
    assert await ts.recover_interrupted_deferred_init_external_owners() is False

    assert db.get_terminal_metadata("unreadable")["deferred_init_failure"] is None
    assert ts.get_terminal("failed")["deferred_init_failure"] == failure
    completed = ts.get_terminal("complete")
    assert completed["status"] == "error"  # ordinary post-init errors stay visible
    assert completed["deferred_init_failure"] is None
    assert not db.get_terminal_metadata("complete")["deferred_init_external_owner"]
    assert not ts._deferred_init_complete_fallback_path("complete").exists()
    interrupted = ts.get_terminal("interrupted")
    assert interrupted["status"] == "error"
    assert interrupted["deferred_init_failure"]["kind"] == "interrupted_init"
    assert interrupted["deferred_init_failure"]["exception_type"] == "ServerRestart"
    assert notify.call_count == 1
    assert notify.call_args.args[0] == "interrupted"
    assert notify.call_args.args[3] is False

    monkeypatch.setattr(ts, "get_terminal_metadata", db.get_terminal_metadata)
    assert await ts.recover_interrupted_deferred_init_external_owners() is True
    assert ts.get_terminal("unreadable")["deferred_init_failure"]["kind"] == "interrupted_init"
    assert ts.get_terminal("failed")["deferred_init_failure"] == failure
    assert (
        ts.get_terminal("interrupted")["deferred_init_failure"]
        == interrupted["deferred_init_failure"]
    )
    assert [entry.args[0] for entry in notify.call_args_list] == ["interrupted", "unreadable"]


@pytest.mark.asyncio
async def test_restart_retry_survives_unexpected_error_and_respects_backoff_cap(monkeypatch):
    recover = AsyncMock(side_effect=[OSError("transient scan error"), False, False, True])
    sleep = AsyncMock()
    monkeypatch.setattr(ts, "recover_interrupted_deferred_init_external_owners", recover)
    monkeypatch.setattr(ts.asyncio, "sleep", sleep)

    await ts.retry_interrupted_deferred_init_external_owners(initial_delay=0.1, max_delay=0.3)

    assert recover.await_count == 4
    assert sleep.await_args_list == [call(0.1), call(0.2), call(0.3), call(0.3)]


@pytest.mark.asyncio
async def test_restart_retry_propagates_cancellation_without_another_scan(monkeypatch):
    recover = AsyncMock(side_effect=asyncio.CancelledError)
    sleep = AsyncMock()
    monkeypatch.setattr(ts, "recover_interrupted_deferred_init_external_owners", recover)
    monkeypatch.setattr(ts.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        await ts.retry_interrupted_deferred_init_external_owners(initial_delay=0)

    recover.assert_awaited_once_with()
    sleep.assert_awaited_once_with(0.0)


@pytest.mark.parametrize("kind", ["failure", "complete"])
def test_deleted_terminal_cannot_acquire_a_late_sidecar(kind):
    pending("deleted")
    assert ts.delete_terminal_row("deleted", None)

    if kind == "failure":
        published = ts._publish_deferred_failure_fallback("deleted", {"message": "late failure"})
    else:
        published = ts._publish_deferred_init_complete_fallback("deleted")

    assert published is False
    assert not ts._deferred_failure_fallback_path("deleted").exists()
    assert not ts._deferred_init_complete_fallback_path("deleted").exists()


def test_success_sidecar_preserves_public_error_during_repeated_ownership_write_failure(
    monkeypatch,
):
    pending("complete")
    assert ts._write_deferred_init_complete_fallback("complete")
    sidecar = ts._deferred_init_complete_fallback_path("complete")
    monkeypatch.setattr(ts.status_monitor, "get_status", lambda terminal_id: TerminalStatus.ERROR)
    update = MagicMock(side_effect=OSError("database remains locked"))
    monkeypatch.setattr(ts, "update_terminal_deferred_init_external_owner", update)

    for _ in range(2):
        result = ts.get_terminal("complete")
        assert result["status"] == "error"
        assert result["deferred_init_failure"] is None
        assert not ts.should_retain_deferred_failure_tombstone("complete")
        assert sidecar.read_bytes() == b"complete\n"

    monkeypatch.setattr(
        ts,
        "update_terminal_deferred_init_external_owner",
        db.update_terminal_deferred_init_external_owner,
    )
    assert ts.get_terminal("complete")["status"] == "error"
    assert not sidecar.exists()
    assert not db.get_terminal_metadata("complete")["deferred_init_external_owner"]
