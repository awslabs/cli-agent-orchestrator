"""Issue #1082: report_outcome's optional terminal_id joins the outcome to one
launch. ``POST /outcomes`` authorises the link from the STORED launch record,
which outlives teardown: a self-report, or a report from the launch's direct
creator (its recorded ``caller_id``). The MCP tool keeps #912's
``_target_binding_denial`` pre-check, which only stops a live outsider early."""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.mcp_server.server import report_outcome

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"
OWN_ID = "abc12345"


@pytest.fixture
def isolated_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outcomes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", factory):
        yield engine


@pytest.fixture
def learning_on():
    with patch(LEARNING_TARGET, return_value=True):
        yield


@pytest.fixture
def api(isolated_db, learning_on):
    """Direct HTTP client for the route, which is the binding's authority."""
    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.plugins import PluginRegistry

    app.state.plugin_registry = PluginRegistry()
    return TestClient(app, headers={"Host": "localhost"})


def _run(coro):
    return asyncio.run(coro)


def _report(**overrides):
    kwargs = dict(
        task_label="t",
        success=True,
        workflow_name=None,
        agent_profile=None,
        score=None,
        friction_notes="",
        terminal_id=None,
    )
    kwargs.update(overrides)
    return report_outcome(**kwargs)


def _post(api, *, launch, source, success=True):
    body = {"session_name": "s", "task_label": "t", "success": success}
    if launch is not None:
        body["launch_terminal_id"] = launch
    if source is not None:
        body["source_terminal_id"] = source
    return api.post("/outcomes", json=body)


def _seed_target_terminal(*, terminal_id: str, caller_id):
    """Register a live terminal row so GET /terminals/{terminal_id} -- which
    _target_binding_denial issues through the SAME ``requests`` module
    mcp_over_testclient routes into the real FastAPI app -- resolves against
    real data."""
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        db.add(
            database.TerminalModel(
                id=terminal_id,
                tmux_session="child-session",
                tmux_window="w",
                provider="kiro_cli",
                caller_id=caller_id,
            )
        )
        db.commit()


def _seed_launch(terminal_id: str, caller_id, *, torn_down: bool = True) -> int:
    """Write a launch record (no live terminal row) and return its id."""
    from cli_agent_orchestrator.clients import database

    database.create_launch_record(
        terminal_id=terminal_id,
        session_name="s",
        provider="kiro_cli",
        target_name="developer",
        target_kind="installed",
        launched_model=None,
        model_honored=None,
        caller_id=caller_id,
    )
    if torn_down:
        database.finalize_launch_records([terminal_id], "deleted")
    with database.SessionLocal() as db:
        return max(
            row.id
            for row in db.query(database.LaunchRecordModel).filter_by(terminal_id=terminal_id)
        )


def _record(record_id: int) -> dict:
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        row = db.query(database.LaunchRecordModel).filter_by(id=record_id).one()
        return database._launch_record_dict(row)


def _outcome_count() -> int:
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        return db.query(database.WorkflowOutcomeModel).count()


class TestReportOutcomeWithoutTerminalId:
    def test_default_omits_launch_terminal_id(self, mcp_over_testclient, isolated_db, learning_on):
        """Unaffected default behavior: no terminal_id means no link attempt."""
        result = _run(_report())
        assert result["success"] is True, result
        assert "launch_linked" not in result
        assert mcp_over_testclient.last.json["launch_terminal_id"] is None


class TestTornDownBinding:
    """The cases the live-row check cannot see: the target is gone."""

    def test_creator_links_after_teardown(self, mcp_over_testclient, isolated_db, learning_on):
        """Ruling test 1. Mutation guard: comparing ``caller_id`` against the
        LAUNCH's own id instead of the reporter breaks this."""
        record_id = _seed_launch("feedf00d", caller_id=OWN_ID)

        result = _run(_report(terminal_id="feedf00d", success=False))

        assert result["success"] is True, result
        assert result["launch_linked"] is True
        record = _record(record_id)
        assert record["outcome"] == "failure"
        assert record["outcome_reporter_id"] == OWN_ID
        assert record["outcome_recorded_at"] is not None

    def test_outsider_on_torn_down_launch_is_refused_and_writes_nothing(
        self, mcp_over_testclient, isolated_db, learning_on
    ):
        """Ruling test 2 (the fail-open case): the MCP pre-check lets a 404
        through, so only the route can refuse. Nothing is written: neither
        the launch record nor a workflow_outcomes row."""
        record_id = _seed_launch("feedf00d", caller_id="someone-else")
        before = _outcome_count()

        result = _run(_report(terminal_id="feedf00d", success=False))

        assert result["success"] is False
        assert "feedf00d" in result["error"] and OWN_ID in result["error"]
        assert "launch_linked" not in result
        record = _record(record_id)
        assert record["outcome"] is None
        assert record["outcome_reporter_id"] is None
        assert _outcome_count() == before

    @pytest.mark.parametrize("b_torn_down", [False, True])
    def test_grandparent_is_refused(
        self, mcp_over_testclient, isolated_db, learning_on, b_torn_down
    ):
        """Ruling test 4: A -> B -> C with C torn down. Only C's direct
        creator B may report on C; A, one level up, may not -- whether or
        not B's own launch record (whose caller_id is A) is still live.

        B must have a launch record of its own here, as the real create path
        always writes one. Without it, a resolver that wrongly extended
        itself one level up -- from C's creator B to B's OWN creator A, via
        ``launch_records.caller_id`` -- would have nothing to walk, and this
        test would miss that bug."""
        _seed_target_terminal(terminal_id="bbbb2222", caller_id=OWN_ID)  # B, live row
        _seed_launch("bbbb2222", caller_id=OWN_ID, torn_down=b_torn_down)  # B's own launch
        record_id = _seed_launch("cccc3333", caller_id="bbbb2222")  # C, torn down
        before = _outcome_count()

        result = _run(_report(terminal_id="cccc3333"))

        assert result["success"] is False
        assert _record(record_id)["outcome"] is None
        assert _outcome_count() == before

    def test_unknown_id_writes_the_outcome_unlinked(
        self, mcp_over_testclient, isolated_db, learning_on
    ):
        """Ruling test 6: no record for the id (a remote worker, a failed
        best-effort write, a pre-migration launch) is not a refusal."""
        before = _outcome_count()

        result = _run(_report(terminal_id="0badc0de"))

        assert result["success"] is True, result
        assert result["launch_linked"] is False
        assert _outcome_count() == before + 1


class TestLiveBinding:
    def test_unrelated_live_terminal_is_refused_and_posts_nothing(
        self, mcp_over_testclient, isolated_db, learning_on
    ):
        """Ruling test 3: a live outsider is still stopped at the MCP layer --
        no /outcomes request is ever issued."""
        _seed_target_terminal(terminal_id="deadbeef", caller_id="someone-else")
        _seed_launch("deadbeef", caller_id="someone-else", torn_down=False)

        result = _run(_report(terminal_id="deadbeef"))

        assert result["success"] is False
        assert "deadbeef" in result["error"]
        assert [r for r in mcp_over_testclient.requests if r.path == "/outcomes"] == []

    def test_live_child_is_linked(self, mcp_over_testclient, isolated_db, learning_on):
        _seed_target_terminal(terminal_id="deadbeef", caller_id=OWN_ID)
        record_id = _seed_launch("deadbeef", caller_id=OWN_ID, torn_down=False)

        result = _run(_report(terminal_id="deadbeef"))

        assert result["success"] is True, result
        assert result["launch_linked"] is True
        assert _record(record_id)["outcome"] == "success"

    def test_self_report_links(self, mcp_over_testclient, isolated_db, learning_on):
        """Ruling test 5, first half."""
        record_id = _seed_launch(OWN_ID, caller_id="creator1", torn_down=False)

        result = _run(_report(terminal_id=OWN_ID))

        assert result["success"] is True, result
        assert result["launch_linked"] is True
        assert _record(record_id)["outcome_reporter_id"] == OWN_ID


class TestWorkflowStepBinding:
    """Ruling test 5, second half: a workflow step's record has no
    ``caller_id`` (its delegating terminal is not its callback parent), so
    only a self-report links and the run's starter is refused."""

    def test_step_self_report_links(self, api):
        record_id = _seed_launch("step0001", caller_id=None, torn_down=False)

        response = _post(api, launch="step0001", source="step0001")

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is True
        assert _record(record_id)["outcome_reporter_id"] == "step0001"

    def test_run_starter_is_refused(self, api):
        record_id = _seed_launch("step0002", caller_id=None)
        before = _outcome_count()

        response = _post(api, launch="step0002", source="starter1")

        assert response.status_code == 403
        assert _record(record_id)["outcome"] is None
        assert _outcome_count() == before


class TestRouteBinding:
    def test_binding_read_failure_writes_the_outcome_unlinked(self, api, monkeypatch):
        """Ruling test 7: a failing binding read never fails the outcome, and
        links nothing."""
        record_id = _seed_launch("feedf00d", caller_id=OWN_ID)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.find_bound_launch_record", _boom
        )
        before = _outcome_count()

        response = _post(api, launch="feedf00d", source=OWN_ID)

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is False
        assert _record(record_id)["outcome"] is None
        assert _outcome_count() == before + 1

    @pytest.mark.parametrize("seeded", [None, "creator", "no-caller"])
    def test_launch_id_without_source_is_refused(self, api, seeded):
        """Ruling test 8: the new field fails closed without a reporter --
        whether or not records exist for the id, and also for a record with
        no ``caller_id`` (a workflow step's), which an anonymous report must
        never match."""
        if seeded == "creator":
            _seed_launch("feedf00d", caller_id=OWN_ID)
        elif seeded == "no-caller":
            _seed_launch("feedf00d", caller_id=None)
        before = _outcome_count()

        response = _post(api, launch="feedf00d", source=None)

        assert response.status_code == 403
        assert _outcome_count() == before

    def test_creator_verdict_replaces_self_report(self, api):
        """Ruling test 9: last write wins between the two allowed reporters,
        and ``outcome_reporter_id`` says whose verdict stands."""
        record_id = _seed_launch("worker01", caller_id="creator1", torn_down=False)

        assert _post(api, launch="worker01", source="worker01", success=True).status_code == 200
        assert _record(record_id)["outcome"] == "success"
        assert _post(api, launch="worker01", source="creator1", success=False).status_code == 200

        record = _record(record_id)
        assert record["outcome"] == "failure"
        assert record["outcome_reporter_id"] == "creator1"

    def test_reused_id_links_each_creator_to_its_own_launch(self, api):
        """Surrogate-key test 3: under a reused terminal id, each creator's
        report lands on the launch it created, never on the other one."""
        old_id = _seed_launch("reused01", caller_id="creatorA")
        new_id = _seed_launch("reused01", caller_id="creatorB", torn_down=False)

        assert _post(api, launch="reused01", source="creatorA", success=True).status_code == 200
        assert _post(api, launch="reused01", source="creatorB", success=False).status_code == 200
        assert _post(api, launch="reused01", source="outsider", success=True).status_code == 403

        assert (_record(old_id)["outcome"], _record(old_id)["outcome_reporter_id"]) == (
            "success",
            "creatorA",
        )
        assert (_record(new_id)["outcome"], _record(new_id)["outcome_reporter_id"]) == (
            "failure",
            "creatorB",
        )

        # A self-report under the same reused id binds only the newest,
        # still-open row (iterating oldest-first would bind this to old_id
        # instead).
        assert _post(api, launch="reused01", source="reused01", success=True).status_code == 200
        assert (_record(new_id)["outcome"], _record(new_id)["outcome_reporter_id"]) == (
            "success",
            "reused01",
        )
        assert (_record(old_id)["outcome"], _record(old_id)["outcome_reporter_id"]) == (
            "success",
            "creatorA",
        )

    def test_self_report_unlinked_when_the_only_row_for_the_id_is_closed(self, api):
        """If the id's newest row is already finalized -- a stale incarnation
        after reuse, since the new one's best-effort write may have failed --
        a self-report must not reopen its verdict. It lands unlinked
        instead."""
        old_id = _seed_launch("stale001", caller_id="creatorA")
        _post(api, launch="stale001", source="creatorA", success=False)
        assert _record(old_id)["outcome"] == "failure"
        before = _outcome_count()

        response = _post(api, launch="stale001", source="stale001", success=True)

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is False
        assert _record(old_id)["outcome"] == "failure"
        assert _record(old_id)["outcome_reporter_id"] == "creatorA"
        assert _outcome_count() == before + 1

    def test_newest_closed_older_open_self_report_unlinked(self, api):
        """The newest row for the id governs a self-report, not the newest
        OPEN row. Here the newest row is closed while an older one (whose
        own finalize was lost) is still open; a self-report must still land
        unlinked, and must not reopen the older row's verdict."""
        old_id = _seed_launch("stale002", caller_id="creatorA", torn_down=False)
        new_id = _seed_launch("stale002", caller_id="creatorB", torn_down=True)
        before = _outcome_count()

        response = _post(api, launch="stale002", source="stale002", success=True)

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is False
        assert _record(old_id)["outcome"] is None
        assert _record(new_id)["outcome"] is None
        assert _outcome_count() == before + 1

    def test_link_failure_never_fails_the_outcome_write(self, api, monkeypatch):
        """AC: a failure writing the record must never fail report_outcome's
        own success. The injected failure replaces link_launch_outcome
        entirely, so this tests the route's own wrapper."""
        _seed_launch("feedf00d", caller_id=OWN_ID)

        def _boom(*_args, **_kwargs):
            raise RuntimeError("db is down")

        monkeypatch.setattr("cli_agent_orchestrator.clients.database.link_launch_outcome", _boom)

        response = _post(api, launch="feedf00d", source=OWN_ID)

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is False
        assert "id" in response.json()["outcome"]

    def test_link_never_copies_free_text_into_the_record(self, api):
        """Privacy: task_label and friction_notes stay in workflow_outcomes;
        the launch record gets only the verdict, its time and the reporter."""
        canary = "synthetic-canary-5d21a7"
        record_id = _seed_launch("feedf00d", caller_id=OWN_ID)

        response = api.post(
            "/outcomes",
            json={
                "session_name": "s",
                "task_label": f"label {canary}",
                "friction_notes": f"notes {canary}",
                "success": True,
                "source_terminal_id": OWN_ID,
                "launch_terminal_id": "feedf00d",
            },
        )

        assert response.status_code == 200, response.text
        assert response.json()["launch_linked"] is True
        for column, value in _record(record_id).items():
            assert canary not in str(value), f"canary leaked into {column!r}"
