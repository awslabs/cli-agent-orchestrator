"""PR #103 review fixes for migration outcome persistence."""

from __future__ import annotations

import sqlite3
import threading
from test.services import test_legacy_identity_migration as migration_tests
from test.services.test_legacy_identity_migration import (
    SESSION_ID,
    _call_with_candidate,
    _candidate_for,
    _intent_row,
    _migration_call,
    _migration_rows,
    _seed_legacy,
    _seed_roster,
    _uuid,
    claude_composer_rows,
    claude_panel_rows,
)
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError

from cli_agent_orchestrator.services import legacy_identity_migration as lim


@pytest.fixture
def harness(tmp_path, monkeypatch):
    """Reuse the migration suite's full tmux/repair harness."""
    migration_tests._sandbox.__wrapped__(tmp_path, monkeypatch)
    return migration_tests.harness.__wrapped__(monkeypatch)


def _failing_commits(monkeypatch, *, n: int = 1) -> None:
    """Make the next ``n`` migration-store commits raise SQLite lock errors."""
    real_factory = lim.database.SessionLocal
    counter = [0]

    class _FailingSession:
        def __init__(self) -> None:
            self._inner = real_factory()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def __enter__(self) -> "_FailingSession":
            return self

        def __exit__(self, *args: Any) -> None:
            self._inner.close()

        def commit(self) -> Any:
            counter[0] += 1
            if counter[0] <= n:
                self._inner.rollback()
                raise OperationalError("COMMIT", (), sqlite3.OperationalError("database is locked"))
            return self._inner.commit()

    monkeypatch.setattr(lim.database, "SessionLocal", lambda: _FailingSession())


def _failing_cas_updates(monkeypatch, *, n: int = 1) -> None:
    """Make the next ``n`` migration CAS UPDATE statements contend."""
    real_factory = lim.database.SessionLocal
    counter = [0]

    class _FailingQuery:
        def __init__(self, inner: Any) -> None:
            self._inner = inner

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def filter(self, *args: Any, **kwargs: Any) -> "_FailingQuery":
            return _FailingQuery(self._inner.filter(*args, **kwargs))

        def update(self, *args: Any, **kwargs: Any) -> Any:
            counter[0] += 1
            if counter[0] <= n:
                raise OperationalError("UPDATE", (), sqlite3.OperationalError("database is locked"))
            return self._inner.update(*args, **kwargs)

    class _FailingSession:
        def __init__(self) -> None:
            self._inner = real_factory()

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def __enter__(self) -> "_FailingSession":
            return self

        def __exit__(self, *args: Any) -> None:
            self._inner.close()

        def query(self, *args: Any, **kwargs: Any) -> _FailingQuery:
            return _FailingQuery(self._inner.query(*args, **kwargs))

    monkeypatch.setattr(lim.database, "SessionLocal", lambda: _FailingSession())


class TestCommittedOutcomePreservation:
    def test_exact_duplicate_revalidation_refusal_never_downgrades_migrated(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )

        real_revalidate = lim._revalidate_migration_candidate
        entered = threading.Event()
        release = threading.Event()
        count = [0]

        def _spy(**kwargs: Any) -> dict[str, Any]:
            count[0] += 1
            if count[0] == 1:
                entered.set()
                release.wait(timeout=60)
            return real_revalidate(**kwargs)

        monkeypatch.setattr(lim, "_revalidate_migration_candidate", _spy)
        results: dict[str, Any] = {}

        def _caller(name: str) -> None:
            results[name] = _call_with_candidate(candidate, operation_id)

        first = threading.Thread(target=_caller, args=("first",))
        first.start()
        assert entered.wait(timeout=30)

        results["second"] = _call_with_candidate(candidate, operation_id)
        assert results["second"]["status"] == lim.MIGRATION_MIGRATED

        release.set()
        first.join(timeout=30)
        assert results["first"]["status"] == lim.MIGRATION_MIGRATED
        assert results["first"]["repair_operation_id"] == results["second"]["repair_operation_id"]
        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_MIGRATED
        assert rows[0].native_session_id == SESSION_ID

    def test_pre_cas_refusal_cannot_finalize_an_attempt_started_operation(
        self, isolated_memory_db, harness, monkeypatch
    ):
        """A stale pre-CAS refusal must not outrun committed repair evidence."""
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())

        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )

        real_revalidate = lim._revalidate_migration_candidate
        revalidation_entered = threading.Event()
        revalidation_release = threading.Event()
        revalidation_count = [0]

        def _slow_first_revalidation(**kwargs: Any) -> dict[str, Any]:
            revalidation_count[0] += 1
            if revalidation_count[0] == 1:
                revalidation_entered.set()
                revalidation_release.wait(timeout=60)
            return real_revalidate(**kwargs)

        monkeypatch.setattr(lim, "_revalidate_migration_candidate", _slow_first_revalidation)

        real_record = lim._record_migration_outcome
        migration_record_entered = threading.Event()
        migration_record_release = threading.Event()

        def _block_migrated_record(**kwargs: Any) -> dict[str, Any]:
            if kwargs["status"] == lim.MIGRATION_MIGRATED and not migration_record_entered.is_set():
                migration_record_entered.set()
                migration_record_release.wait(timeout=60)
            return real_record(**kwargs)

        monkeypatch.setattr(lim, "_record_migration_outcome", _block_migrated_record)
        results: dict[str, Any] = {}

        def _caller(name: str) -> None:
            results[name] = _call_with_candidate(candidate, operation_id)

        first = threading.Thread(target=_caller, args=("first",))
        first.start()
        assert revalidation_entered.wait(timeout=30)

        second = threading.Thread(target=_caller, args=("second",))
        second.start()
        assert migration_record_entered.wait(timeout=30)

        # PR #99 has committed repair evidence, but its migration outcome is
        # paused. The stale caller now sees the identity as known; its
        # pre-CAS refusal must not transition attempt-started to refused.
        revalidation_release.set()
        first.join(timeout=30)
        assert results["first"]["status"] == lim.MIGRATION_REFUSED
        assert results["first"]["reason"] == lim.MIGRATION_REFUSED_IN_PROGRESS

        migration_record_release.set()
        second.join(timeout=30)
        assert results["second"]["status"] == lim.MIGRATION_MIGRATED
        retry = _call_with_candidate(candidate, operation_id)
        assert retry["status"] == lim.MIGRATION_MIGRATED

        rows = _migration_rows()
        assert len(rows) == 1
        assert rows[0].status == lim.MIGRATION_MIGRATED
        assert sum(entry.get("text") == "/status" for entry in harness.typed) == 1


class TestTypedCommitContention:
    def test_intent_commit_contention_is_typed_persistence_unavailable(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        _failing_commits(monkeypatch)

        outcome = _migration_call()

        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "persistence-unavailable"
        assert _migration_rows() == []
        assert harness.typed == []

    def test_cas_commit_contention_is_typed_in_progress(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )
        _failing_commits(monkeypatch, n=2)

        outcome = _call_with_candidate(candidate, operation_id)

        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] in (
            lim.MIGRATION_REFUSED_IN_PROGRESS,
            lim.MIGRATION_REFUSED_REPAIR_UNRESOLVED,
        )
        assert harness.typed == []

    def test_cas_update_contention_is_typed_in_progress(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )
        # Both the initial CAS and its bounded retry contend before the
        # statement executes, so this caller must not type any repair bytes.
        _failing_cas_updates(monkeypatch, n=2)

        outcome = _call_with_candidate(candidate, operation_id)

        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == lim.MIGRATION_REFUSED_IN_PROGRESS
        assert harness.typed == []

    def test_cas_retry_commit_success_proceeds_through_repair(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code")
        _seed_roster("claude_code", generation=None)
        harness.screens.append(claude_panel_rows())
        harness.styled_screens.append(claude_composer_rows())
        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )
        # The first claim commit contends; the bounded retry commits and is
        # therefore the repair owner, not an in-progress loser.
        _failing_commits(monkeypatch, n=1)

        outcome = _call_with_candidate(candidate, operation_id)

        assert outcome["status"] == lim.MIGRATION_MIGRATED
        assert sum(entry.get("text") == "/status" for entry in harness.typed) == 1
        retry = _call_with_candidate(candidate, operation_id)
        assert retry["status"] == lim.MIGRATION_MIGRATED

    def test_outcome_commit_contention_is_typed_persistence_unavailable(
        self, isolated_memory_db, harness, monkeypatch
    ):
        _seed_legacy("claude_code", native_session_id=SESSION_ID)
        _seed_roster("claude_code", generation=None)
        candidate = _candidate_for()
        operation_id = _uuid()
        request_digest = lim.migration_request_digest(
            terminal_id=candidate["terminal_id"],
            provider=candidate["provider"],
            generation=None,
            physical_occurrence=candidate["physical_occurrence"],
            provider_version="2.1.226",
            audit_occurrence_id=candidate["occurrence_id"],
            audit_candidate_digest=candidate["evidence_digest"],
        )
        _intent_row(
            operation_id=operation_id,
            request_digest=request_digest,
            candidate=candidate,
            repair_operation_id=lim._repair_operation_id(operation_id),
            status=lim.MIGRATION_PENDING,
        )
        _failing_commits(monkeypatch)

        outcome = _call_with_candidate(candidate, operation_id)

        assert outcome["status"] == lim.MIGRATION_REFUSED
        assert outcome["reason"] == "persistence-unavailable"
        assert harness.typed == []
