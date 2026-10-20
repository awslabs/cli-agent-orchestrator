"""Legacy backfill needs exact backend proof before adopting failed terminal rows."""

from unittest.mock import MagicMock

import pytest

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.backends.base import TerminalCleanupOutcome, TerminalCleanupResult
from cli_agent_orchestrator.clients import database as db
from cli_agent_orchestrator.services import terminal_service as ts
from cli_agent_orchestrator.services.session_lock import session_lifecycle_lock


@pytest.fixture(autouse=True)
def isolated_incarnation(isolated_memory_db, monkeypatch, tmp_path):
    monkeypatch.setattr(ts, "TERMINAL_LOG_DIR", tmp_path / "logs")


def legacy_failure():
    db.create_terminal("failed", "cao-legacy", "worker", "kimi_cli")
    db.update_terminal_deferred_init_failure("failed", {"message": "Retained init failure"})


def backend_result(monkeypatch, outcome):
    backend = MagicMock()
    backend.cleanup_terminal_exact.return_value = TerminalCleanupResult(outcome, "identity probe")
    monkeypatch.setattr(registry, "_backend", backend)
    return backend


def test_unknown_failed_sibling_blocks_backfill_without_mutating_any_membership(monkeypatch):
    db.create_terminal("healthy", "cao-legacy", "conductor", "kimi_cli")
    legacy_failure()
    backend = backend_result(monkeypatch, TerminalCleanupOutcome.UNKNOWN)

    with session_lifecycle_lock("cao-legacy"):
        with pytest.raises(ts.TerminalRecordCorruptError, match="legacy failed sibling"):
            ts._resolve_existing_session_incarnation_locked("cao-legacy")

    assert db.get_session_incarnation("cao-legacy") is None
    assert db.get_terminal_metadata("healthy")["session_incarnation_id"] is None
    assert db.get_terminal_metadata("failed")["session_incarnation_id"] is None
    backend.cleanup_terminal_exact.assert_called_once_with(
        "failed", "cao-legacy", "worker", close=False
    )
    backend.kill_session.assert_not_called()
    backend.kill_window.assert_not_called()


def test_absent_failed_sibling_is_not_adopted_by_a_healthy_replacement(monkeypatch):
    db.create_terminal("healthy", "cao-legacy", "conductor", "kimi_cli")
    legacy_failure()
    backend = backend_result(monkeypatch, TerminalCleanupOutcome.ABSENT)

    with session_lifecycle_lock("cao-legacy"):
        incarnation = ts._resolve_existing_session_incarnation_locked("cao-legacy")

    assert db.get_session_incarnation("cao-legacy") == incarnation
    assert db.get_terminal_metadata("healthy")["session_incarnation_id"] == incarnation
    failed = db.get_terminal_metadata("failed")
    assert failed["session_incarnation_id"] is None
    assert failed["deferred_init_failure"] == {"message": "Retained init failure"}
    backend.cleanup_terminal_exact.assert_called_once_with(
        "failed", "cao-legacy", "worker", close=False
    )


@pytest.mark.parametrize(
    "outcome,reason",
    [
        (TerminalCleanupOutcome.UNKNOWN, "Could not verify session incarnation"),
        (TerminalCleanupOutcome.ABSENT, "No terminal proves the current incarnation"),
    ],
)
def test_failed_only_session_cannot_claim_an_unproven_incarnation(monkeypatch, outcome, reason):
    legacy_failure()
    backend = backend_result(monkeypatch, outcome)

    with session_lifecycle_lock("cao-legacy"):
        with pytest.raises(ts.TerminalRecordCorruptError, match=reason):
            ts._resolve_existing_session_incarnation_locked("cao-legacy")

    assert db.get_session_incarnation("cao-legacy") is None
    assert db.get_terminal_metadata("failed")["session_incarnation_id"] is None
    backend.cleanup_terminal_exact.assert_called_once_with(
        "failed", "cao-legacy", "worker", close=False
    )
