"""Session identity regressions using real SQLite persistence and selection."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.flow import Flow
from cli_agent_orchestrator.services import flow_service as fs
from cli_agent_orchestrator.services import session_service as ss
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock


@pytest.fixture(autouse=True)
def isolated_lifecycle(isolated_memory_db, tmp_path, monkeypatch):
    monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path / "logs")


def add_terminal(
    terminal_id, name, incarnation=None, *, current=False, failed=False, reclaimed=False
):
    db.create_terminal(
        terminal_id,
        name,
        f"w-{terminal_id}",
        "mock_cli",
        agent_profile=terminal_id,
        session_incarnation_id=incarnation,
        new_session_incarnation=current,
    )
    if failed:
        db.update_terminal_deferred_init_failure(terminal_id, {"message": f"failure-{terminal_id}"})
    if reclaimed:
        db.update_terminal_deferred_init_runtime_reclaimed(terminal_id, True)


def test_new_incarnation_and_terminal_roll_back_together():
    db.create_terminal(
        "original",
        "cao-s",
        "w-original",
        "mock_cli",
        session_incarnation_id="inc-old",
        new_session_incarnation=True,
        idempotency_key="key",
        request_fingerprint="original",
    )
    with pytest.raises(IntegrityError):
        db.create_terminal(
            "replacement",
            "cao-s",
            "w-replacement",
            "mock_cli",
            session_incarnation_id="inc-new",
            new_session_incarnation=True,
            idempotency_key="key",
            request_fingerprint="replacement",
        )
    assert db.get_session_incarnation("cao-s") == "inc-old"
    assert db.get_terminal_metadata("replacement") is None


def test_legacy_schema_migration_creates_pointer_table_idempotently(tmp_path, monkeypatch):
    import sqlite3

    from cli_agent_orchestrator import constants

    path = tmp_path / "legacy.db"
    with sqlite3.connect(path) as conn:
        conn.execute(
            "CREATE TABLE terminals (id TEXT PRIMARY KEY, tmux_session TEXT, tmux_window TEXT, "
            "provider TEXT, agent_profile TEXT, last_active DATETIME)"
        )
        conn.execute(
            "INSERT INTO terminals VALUES ('legacy', 'cao-s', 'w-legacy', 'mock_cli', NULL, NULL)"
        )
    engine = create_engine(f"sqlite:///{path}")
    monkeypatch.setattr(constants, "DATABASE_FILE", path)
    monkeypatch.setattr(db, "engine", engine)
    monkeypatch.setattr(db, "SessionLocal", sessionmaker(bind=engine))
    try:
        db.init_db()
        assert db.get_terminal_metadata("legacy")["session_incarnation_id"] is None
        assert db.update_terminals_session_incarnation(
            ["legacy"], "inc-current", session_name="cao-s"
        )
        db.init_db()
        assert db.get_session_incarnation("cao-s") == "inc-current"
        assert db.get_terminal_metadata("legacy")["session_incarnation_id"] == "inc-current"
    finally:
        engine.dispose()


@pytest.mark.parametrize("bad_id", ["missing", "other-session", "conflicting"])
def test_backfill_rejects_missing_or_conflicting_identity_atomically(bad_id):
    add_terminal("legacy", "cao-s")
    add_terminal("other-session", "cao-other")
    add_terminal("conflicting", "cao-s", "another-inc")
    assert (
        db.update_terminals_session_incarnation(
            ["legacy", bad_id], "inc-current", session_name="cao-s"
        )
        is False
    )
    assert db.get_session_incarnation("cao-s") is None
    assert db.get_terminal_metadata("legacy")["session_incarnation_id"] is None


def test_delete_retry_preserves_old_failure_and_sidecar(monkeypatch):
    add_terminal("old-fail", "cao-s", "inc-old", failed=True, reclaimed=True)
    add_terminal("current", "cao-s", "inc-current", current=True)
    assert ts._write_deferred_failure_fallback("old-fail", {"message": "retained"})
    sidecar = ts._deferred_failure_fallback_path("old-fail")
    backend = MagicMock()
    backend.session_exists_strict.side_effect = [True, False]
    backend.kill_session.return_value = True
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    monkeypatch.setattr(ts, "capture_terminal_snapshot", db.get_terminal_metadata)
    monkeypatch.setattr(ts, "dismantle_terminal_runtime", lambda *args, **kwargs: True)

    assert ss.delete_session("cao-s") == {"deleted": ["cao-s"], "errors": []}
    assert ss.delete_session("cao-s") == {"deleted": ["cao-s"], "errors": []}
    assert db.get_terminal_metadata("current") is None
    assert db.get_terminal_metadata("old-fail")["deferred_init_failure"] is not None
    assert sidecar.is_file()
    assert db.get_session_incarnation("cao-s") == "inc-current"


def test_failed_only_current_incarnation_stays_visible_after_restart(monkeypatch):
    add_terminal("old-fail", "cao-s", "inc-old", failed=True, reclaimed=True)
    add_terminal("current", "cao-s", "inc-current", current=True, failed=True)
    # A new ORM session reads only durable state; no provider or process-local
    # identity survives this simulated restart.
    backend = MagicMock()
    backend.session_exists.return_value = True
    backend.list_sessions.return_value = [{"id": "cao-s"}]
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == ["current"]
    result = ss.get_session("cao-s")
    assert [(row["id"], row["status"]) for row in result["terminals"]] == [("current", "error")]
    assert ss.list_sessions()[0]["agent_profile"] == "current"
    with session_lifecycle_lock("cao-s"):
        assert ts._resolve_existing_session_incarnation_locked("cao-s") == "inc-current"


def test_old_pending_row_does_not_define_replacement(monkeypatch):
    db.create_terminal(
        "old-pending",
        "cao-s",
        "w-old",
        "mock_cli",
        deferred_init_external_owner=True,
        session_incarnation_id="inc-old",
    )
    ts._purge_stale_session_rows_for_recreate("cao-s")
    add_terminal("current", "cao-s", "inc-current", current=True)
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == ["current"]
    with session_lifecycle_lock("cao-s"):
        assert ts._resolve_existing_session_incarnation_locked("cao-s") == "inc-current"
    assert db.get_terminal_metadata("old-pending") is not None


def test_failed_legacy_terminal_is_backfilled_only_after_exact_proof(monkeypatch):
    add_terminal("old-fail", "cao-s", "inc-old", failed=True, reclaimed=True)
    add_terminal("legacy", "cao-s", failed=True)
    backend = MagicMock()
    backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(
        TerminalCleanupOutcome.STILL_PRESENT
    )
    monkeypatch.setattr(ts, "get_backend", lambda: backend)
    with session_lifecycle_lock("cao-s"):
        incarnation = ts._resolve_existing_session_incarnation_locked("cao-s")
    backend.cleanup_terminal_exact.assert_called_once_with(
        "legacy", "cao-s", "w-legacy", close=False
    )
    assert db.get_session_incarnation("cao-s") == incarnation
    assert db.get_terminal_metadata("legacy")["session_incarnation_id"] == incarnation
    assert db.get_terminal_metadata("old-fail")["session_incarnation_id"] == "inc-old"
    add_terminal("new-window", "cao-s", incarnation)
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == ["legacy", "new-window"]


def test_failed_legacy_sibling_joins_healthy_conductor_after_exact_proof(monkeypatch):
    add_terminal("conductor", "cao-s")
    add_terminal("sibling", "cao-s", failed=True)
    add_terminal("historical", "cao-s", failed=True)
    backend = MagicMock()
    backend.cleanup_terminal_exact.side_effect = [
        TerminalCleanupResult(TerminalCleanupOutcome.STILL_PRESENT),
        TerminalCleanupResult(TerminalCleanupOutcome.ABSENT),
    ]
    monkeypatch.setattr(ts, "get_backend", lambda: backend)
    with session_lifecycle_lock("cao-s"):
        incarnation = ts._resolve_existing_session_incarnation_locked("cao-s")
    assert db.get_terminal_metadata("conductor")["session_incarnation_id"] == incarnation
    assert db.get_terminal_metadata("sibling")["session_incarnation_id"] == incarnation
    assert db.get_terminal_metadata("historical")["session_incarnation_id"] is None
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == ["conductor", "sibling"]


def test_legacy_live_view_and_deferred_teardown_keep_exact_sibling_membership(monkeypatch):
    add_terminal("conductor", "cao-s")
    add_terminal("sibling", "cao-s", failed=True)
    add_terminal("historical", "cao-s", failed=True)
    backend = MagicMock()
    backend.cleanup_terminal_exact.side_effect = (
        lambda terminal_id, *args, **kwargs: TerminalCleanupResult(
            TerminalCleanupOutcome.STILL_PRESENT
            if terminal_id == "sibling"
            else TerminalCleanupOutcome.ABSENT
        )
    )
    backend.session_exists_strict.side_effect = [True, False, False]
    backend.kill_session.return_value = True
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == ["conductor", "sibling"]
    monkeypatch.setattr(ts, "capture_terminal_snapshot", db.get_terminal_metadata)
    attempts = iter([True, False, True])
    monkeypatch.setattr(ts, "dismantle_terminal_runtime", lambda *args, **kwargs: next(attempts))
    assert ss.delete_session("cao-s")["errors"]
    incarnation = db.get_session_incarnation("cao-s")
    assert incarnation is not None
    assert db.get_terminal_metadata("sibling")["session_incarnation_id"] == incarnation
    backend.cleanup_terminal_exact.side_effect = AssertionError(
        "backend identity is no longer available"
    )
    assert ss.delete_session("cao-s")["errors"] == []
    assert ss.delete_session("cao-s")["errors"] == []
    assert db.get_terminal_metadata("sibling") is None
    assert db.get_terminal_metadata("historical")["deferred_init_failure"] is not None


@pytest.mark.parametrize("kind", ["failure", "completion"])
@pytest.mark.parametrize("bad_write", [False, True])
def test_sidecars_complete_short_writes_and_reject_false_byte_counts(monkeypatch, kind, bad_write):
    failure = {"kind": "provider_init_error", "message": 'model "invalid" is not configured'}
    if kind == "failure":
        writer = lambda: ts._write_deferred_failure_fallback("failed", failure)
        target = ts._deferred_failure_fallback_path("failed")
    else:
        writer = lambda: ts._write_deferred_init_complete_fallback("failed")
        target = ts._deferred_init_complete_fallback_path("failed")
    assert writer()
    original = target.read_bytes()
    write = ts.os.write

    def short_write(fd, payload):
        count = write(fd, payload[: max(1, len(payload) // 2)])
        return len(payload) if bad_write else count

    monkeypatch.setattr(ts.os, "write", short_write)
    assert writer() is (not bad_write)
    assert target.read_bytes() == original
    assert not target.with_name(target.name + ".tmp").exists()
    if kind == "failure":
        assert ts._read_deferred_failure_fallback("failed") == failure


def test_pointer_read_failure_blocks_all_teardown(monkeypatch):
    add_terminal("current", "cao-s", "inc-current", current=True)
    backend = MagicMock()
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    monkeypatch.setattr(
        ss, "get_session_incarnation", MagicMock(side_effect=RuntimeError("DB unavailable"))
    )
    capture = MagicMock()
    monkeypatch.setattr(ts, "capture_terminal_snapshot", capture)
    with pytest.raises(RuntimeError, match="DB unavailable"):
        ss.delete_session("cao-s")
    capture.assert_not_called()
    backend.kill_session.assert_not_called()
    assert db.get_terminal_metadata("current") is not None


def test_conflicting_unanchored_rows_block_session_teardown(monkeypatch):
    add_terminal("one", "cao-s", "inc-one")
    add_terminal("two", "cao-s", "inc-two")
    backend = MagicMock()
    capture = MagicMock()
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    monkeypatch.setattr(ts, "capture_terminal_snapshot", capture)
    with pytest.raises(ts.TerminalRecordCorruptError, match="conflicting incarnation"):
        ss.delete_session("cao-s")
    backend.kill_session.assert_not_called()
    capture.assert_not_called()
    assert len(db.list_terminals_by_session("cao-s")) == 2


def test_unknown_failed_legacy_identity_blocks_teardown(monkeypatch):
    add_terminal("failed", "cao-s", failed=True)
    backend = MagicMock()
    backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(
        TerminalCleanupOutcome.UNKNOWN
    )
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    with pytest.raises(ts.TerminalRecordCorruptError, match="Could not verify"):
        ss.delete_session("cao-s")
    backend.kill_session.assert_not_called()
    assert db.get_terminal_metadata("failed") is not None


def test_unanchored_historical_failure_is_never_claimed_by_delete(monkeypatch):
    add_terminal("old-fail", "cao-s", "inc-old", failed=True, reclaimed=True)
    backend = MagicMock()
    backend.session_exists_strict.return_value = False
    monkeypatch.setattr(ss, "get_backend", lambda: backend)
    monkeypatch.setattr(ts, "capture_terminal_snapshot", MagicMock())
    ss.delete_session("cao-s")
    assert db.get_terminal_metadata("old-fail") is not None
    assert ss.list_current_session_terminals("cao-s", backend_exists=True) == []
    assert len(ss.list_current_session_terminals("cao-s", backend_exists=False)) == 1


@pytest.mark.asyncio
async def test_create_and_add_window_share_identity_even_after_failure(monkeypatch):
    backend = MagicMock()
    backend.session_exists.side_effect = [False, True]
    backend.supports_event_inbox.return_value = True
    backend.create_window.return_value = "w-new-window"
    monkeypatch.setattr(registry, "_backend", backend)
    ids = iter(["first001", "second01"])
    monkeypatch.setattr(ts, "generate_terminal_id", lambda: next(ids))
    monkeypatch.setattr(
        ts, "load_agent_profile", lambda _: AgentProfile(name="developer", description="Developer")
    )
    monkeypatch.setattr(ts, "get_herdr_inbox_service", lambda: None)
    provider_manager = MagicMock()
    provider_manager.create_provider.return_value = AsyncMock()
    monkeypatch.setattr(ts, "provider_manager", provider_manager)
    first = await ts.create_terminal(
        "mock_cli", "developer", session_name="cao-s", new_session=True
    )
    db.update_terminal_deferred_init_failure(first.id, {"message": "provider rejected model"})
    second = await ts.create_terminal(
        "mock_cli", "developer", session_name="cao-s", new_session=False
    )
    assert first.session_incarnation_id == second.session_incarnation_id
    assert db.get_session_incarnation("cao-s") == first.session_incarnation_id
    assert [
        row["id"] for row in ss.list_current_session_terminals("cao-s", backend_exists=True)
    ] == [first.id, second.id]


@pytest.mark.asyncio
async def test_absent_flow_preserves_history_and_retries_ordinary_cleanup(monkeypatch):
    name = "cao-flow-review"
    add_terminal("old-fail", name, "inc-old", failed=True, reclaimed=True)
    add_terminal("cleanup", name, "inc-current", current=True)
    flow = Flow(
        name="review",
        file_path="/unused",
        schedule="* * * * *",
        agent_profile="developer",
        provider="mock_cli",
        script="",
        enabled=True,
        next_run=datetime.now(),
    )
    backend = MagicMock()
    backend.session_exists.return_value = False
    monkeypatch.setattr(fs, "get_backend", lambda: backend)
    monkeypatch.setattr(fs, "get_flow", lambda _: flow)
    monkeypatch.setattr(fs, "_parse_flow_file", lambda _: ({}, "prompt"))
    monkeypatch.setattr(fs, "db_update_flow_run_times", MagicMock())
    monkeypatch.setattr(fs, "send_input", MagicMock())
    launch = AsyncMock(return_value=SimpleNamespace(id="new-window"))
    monkeypatch.setattr(fs, "create_terminal", launch)
    provider_manager = MagicMock()
    provider_manager.cleanup_provider.side_effect = [False, True]
    monkeypatch.setattr(fs, "provider_manager", provider_manager)
    assert await fs.execute_flow("review") is False
    launch.assert_not_called()
    assert db.get_terminal_metadata("cleanup") is not None
    assert await fs.execute_flow("review") is True
    assert db.get_terminal_metadata("cleanup") is None
    assert db.get_terminal_metadata("old-fail")["deferred_init_failure"] is not None
    assert [call.args[0] for call in provider_manager.cleanup_provider.call_args_list] == [
        "cleanup",
        "cleanup",
    ]
