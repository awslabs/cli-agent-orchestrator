"""Unit tests for the HandoffResultModel CRUD helpers (issue #447).

Uses an in-memory SQLite database so no file system state is required.
"""

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    HandoffResultModel,
    delete_old_handoff_results,
    get_handoff_result,
    upsert_handoff_result,
)


@pytest.fixture(autouse=True)
def _use_test_db(monkeypatch):
    """Redirect all DB calls to a fresh in-memory SQLite DB."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    monkeypatch.setattr("cli_agent_orchestrator.clients.database.SessionLocal", TestSession)
    yield


def _utcnow():
    return datetime.now(timezone.utc)


class TestUpsertHandoffResult:
    def test_creates_new_record_on_first_call(self):
        upsert_handoff_result("job-1", "running")
        record = get_handoff_result("job-1")
        assert record is not None
        assert record["state"] == "running"
        assert record["last_message"] is None

    def test_updates_existing_record_on_second_call(self):
        upsert_handoff_result("job-2", "running")
        upsert_handoff_result("job-2", "completed", last_message="ok", terminal_id="abc12345")
        record = get_handoff_result("job-2")
        assert record["state"] == "completed"
        assert record["last_message"] == "ok"
        assert record["terminal_id"] == "abc12345"

    def test_error_state_stored_with_message(self):
        upsert_handoff_result("job-3", "running")
        upsert_handoff_result("job-3", "error", error_message="worker crashed")
        record = get_handoff_result("job-3")
        assert record["state"] == "error"
        assert record["error_message"] == "worker crashed"

    def test_partial_update_does_not_overwrite_nones(self):
        upsert_handoff_result("job-4", "completed", last_message="original", terminal_id="t1")
        # Calling with no last_message must not clear the existing value.
        upsert_handoff_result("job-4", "completed")
        record = get_handoff_result("job-4")
        assert record["last_message"] == "original"
        assert record["terminal_id"] == "t1"


class TestGetHandoffResult:
    def test_returns_none_for_unknown_job(self):
        assert get_handoff_result("no-such-job") is None

    def test_returns_dict_with_expected_keys(self):
        upsert_handoff_result("job-5", "running")
        record = get_handoff_result("job-5")
        assert set(record.keys()) == {
            "job_id",
            "state",
            "terminal_id",
            "last_message",
            "error_message",
            "created_at",
            "updated_at",
        }


class TestDeleteOldHandoffResults:
    def test_deletes_records_older_than_cutoff(self):
        upsert_handoff_result("old-1", "completed")
        upsert_handoff_result("old-2", "error")
        upsert_handoff_result("new-1", "running")
        # Backdate old-1 and old-2 by reaching into the DB directly.
        from cli_agent_orchestrator.clients.database import HandoffResultModel, SessionLocal

        past = _utcnow() - timedelta(days=20)
        with SessionLocal() as db:
            for jid in ("old-1", "old-2"):
                row = db.query(HandoffResultModel).filter(HandoffResultModel.job_id == jid).first()
                row.created_at = past
            db.commit()

        cutoff = _utcnow() - timedelta(days=10)
        deleted = delete_old_handoff_results(cutoff)
        assert deleted == 2
        assert get_handoff_result("old-1") is None
        assert get_handoff_result("old-2") is None
        assert get_handoff_result("new-1") is not None

    def test_returns_zero_when_nothing_to_delete(self):
        upsert_handoff_result("recent", "completed")
        cutoff = _utcnow() - timedelta(days=30)
        deleted = delete_old_handoff_results(cutoff)
        assert deleted == 0
