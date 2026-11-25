"""The background failure backstop must never rewrite settled runs."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.clients.database import _migrate_workflow_run
from cli_agent_orchestrator.services import workflow_journal


@given(
    state=st.sampled_from(["completed", "failed", "cancelled"]),
    finished_at=st.datetimes(timezones=st.just(timezone.utc)).map(datetime.isoformat),
    backstop_at=st.datetimes(timezones=st.just(timezone.utc)).map(datetime.isoformat),
)
@settings(suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_backstop_preserves_terminal_state(
    state: str, finished_at: str, backstop_at: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failure backstop cannot overwrite any terminal state or its finish time."""
    monkeypatch.setattr(
        "cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "property-journal.db"
    )
    _migrate_workflow_run()
    run_id = str(uuid4())
    workflow_journal.insert_run(run_id, "wf", "{}", "{}", state, "2026-01-01T00:00:00Z")
    workflow_journal.update_run_state(run_id, state, finished_at)
    assert not workflow_journal.settle_run_state_if_running(run_id, "failed", backstop_at)
    row = workflow_journal.get_run(run_id)
    assert row is not None
    assert (row.state, row.finished_at) == (state, finished_at)
