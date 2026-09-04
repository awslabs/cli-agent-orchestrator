"""The outcome MCP tools, exercised as HTTP clients.

``report_outcome`` and ``list_outcomes`` used to instantiate ``OutcomeService()``
and open SQLite in the agent's own process, so they failed for any agent that did
not share a filesystem with cao-server — and the memory gateway
(``CAO_MEMORY_API_URL``) does not cover outcomes, so configuration could not work
around it. They now call the existing ``POST``/``GET /outcomes`` routes.

Two response translations carry most of the weight:

* a 404 (the route's learning gate) becomes ``disabled: True``, which
  ``skills/cao-learning`` tells agents to skip SILENTLY;
* a 503 (settings unreadable) and a transport failure must therefore NEVER become
  ``disabled`` — that is what let an unreadable config present itself as a
  deliberate opt-out.

Run over ``mcp_over_testclient`` (see conftest), which routes the tools' real
``requests`` calls into the real FastAPI routes in-process and records what
travelled. These tests deliberately do NOT call the tools against a local
service: that is the coupling being removed, so a test that kept doing it would
pass while exercising a path production no longer takes.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.mcp_server.server import list_outcomes, report_outcome

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"


@pytest.fixture
def isolated_db(tmp_path):
    """Point the routes' OutcomeService at a tmp engine."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outcomes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", factory):
        yield engine


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
    )
    kwargs.update(overrides)
    return report_outcome(**kwargs)


def _list(**overrides):
    kwargs = dict(session_name=None, agent_profile=None, workflow_name=None, limit=50)
    kwargs.update(overrides)
    return list_outcomes(**kwargs)


class TestReportOutcome:
    def test_round_trip_returns_only_the_id(self, mcp_over_testclient, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(task_label="convert package X", score=90, friction_notes="note"))
        assert result["success"] is True, result
        # Only the id: the route returns the whole record, and echoing it would
        # newly surface friction_notes back to the agent that wrote them.
        assert set(result) == {"success", "outcome_id"}

    def test_identity_defaults_from_the_terminal_record(self, mcp_over_testclient, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            _run(_report())
        body = mcp_over_testclient.last.json
        assert body["session_name"] == "sess-1"
        assert body["agent_profile"] == "developer"
        assert body["source_terminal_id"] == "abc12345"

    def test_explicit_agent_profile_wins(self, mcp_over_testclient, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            _run(_report(agent_profile="transformer"))
        assert mcp_over_testclient.last.json["agent_profile"] == "transformer"

    def test_no_terminal_context_is_error_and_issues_no_request(
        self, mcp_over_testclient, isolated_db, monkeypatch
    ):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report())
        assert result["success"] is False
        assert "terminal context" in result["error"]
        assert mcp_over_testclient.requests == []

    def test_disabled_learning_returns_a_disabled_payload(self, mcp_over_testclient, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            result = _run(_report())
        assert result["success"] is False
        assert result["disabled"] is True
        assert "memory.learning_enabled" in result["error"]

    def test_invalid_score_is_a_plain_error_not_disabled(self, mcp_over_testclient, isolated_db):
        """The route 400s on a bad score; that must not read as a feature gate."""
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_report(score=150))
        assert result["success"] is False
        assert "disabled" not in result
        assert "score" in result["error"]


class TestListOutcomes:
    def test_defaults_to_the_caller_session(self, mcp_over_testclient, isolated_db):
        from cli_agent_orchestrator.services.outcome_service import OutcomeService

        with patch(LEARNING_TARGET, return_value=True):
            OutcomeService().record_outcome(
                session_name="sess-1", task_label="in-session", success=True
            )
            OutcomeService().record_outcome(
                session_name="other-session", task_label="elsewhere", success=True
            )
            result = _run(_list())
        assert result["success"] is True, result
        assert result["count"] == 1
        assert result["outcomes"][0]["task_label"] == "in-session"

    def test_explicit_session_wins(self, mcp_over_testclient, isolated_db):
        from cli_agent_orchestrator.services.outcome_service import OutcomeService

        with patch(LEARNING_TARGET, return_value=True):
            OutcomeService().record_outcome(
                session_name="other-session", task_label="elsewhere", success=True
            )
            result = _run(_list(session_name="other-session"))
        assert result["count"] == 1

    def test_disabled_learning_keeps_the_empty_list_key(self, mcp_over_testclient, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            result = _run(_list(session_name="sess-1"))
        assert result["disabled"] is True
        assert result["outcomes"] == []

    def test_oversized_limit_is_clamped_not_rejected(self, mcp_over_testclient, isolated_db):
        """limit=500 works today (the service clamps); Query(le=200) would 422."""
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_list(session_name="sess-1", limit=500))
        assert result["success"] is True, result
        assert mcp_over_testclient.last.params["limit"] == 200


class TestListOutcomesFailClosed:
    """No unfiltered cross-session query, ever.

    A transient context-lookup failure must not fall back to listing every
    session's outcomes — the rows carry other sessions' friction notes. The guard
    stays client-side because ``GET /outcomes`` deliberately permits an
    unfiltered listing for operator and UI use.
    """

    def test_context_lookup_failure_issues_no_request(
        self, mcp_over_testclient, isolated_db, monkeypatch
    ):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_list())
        assert result["success"] is False
        assert result["outcomes"] == []
        assert "session" in result["error"]
        assert mcp_over_testclient.requests == []

    def test_context_without_session_name_issues_no_request(self, mcp_over_testclient, isolated_db):
        seam = mcp_over_testclient
        seam.terminal_record = {
            "id": "abc12345",
            "session_name": None,
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_list())
        assert result["success"] is False
        assert result["outcomes"] == []
        assert [r for r in seam.requests if r.path == "/outcomes"] == []

    def test_explicit_session_still_works(self, mcp_over_testclient, isolated_db):
        from cli_agent_orchestrator.services.outcome_service import OutcomeService

        with patch(LEARNING_TARGET, return_value=True):
            OutcomeService().record_outcome(
                session_name="named-session", task_label="t", success=True
            )
            result = _run(_list(session_name="named-session"))
        assert result["success"] is True
        assert result["count"] == 1


class TestFailureTranslation:
    """The honesty rules: only a feature gate may read as "disabled"."""

    @pytest.fixture
    def _transport_failure(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server import utils as mcp_utils

        def boom(*_a, **_kw):
            raise requests.ConnectionError("connection refused")

        for verb in ("get", "post"):
            monkeypatch.setattr(mcp_utils.requests, verb, boom)

    @pytest.fixture
    def _unreadable_settings(self, monkeypatch):
        from cli_agent_orchestrator.mcp_server import utils as mcp_utils

        class _Resp:
            status_code = 503

            def json(self):
                return {"detail": "CAO settings.json could not be read (PermissionError)"}

        def unavailable(*_a, **_kw):
            raise requests.HTTPError("503", response=_Resp())

        for verb in ("get", "post"):
            monkeypatch.setattr(mcp_utils.requests, verb, unavailable)

    def test_report_outcome_unreachable_server(
        self, mcp_over_testclient, isolated_db, _transport_failure
    ):
        result = _run(_report())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "connect to cao-server" in result["error"]

    def test_list_outcomes_unreachable_server(
        self, mcp_over_testclient, isolated_db, _transport_failure
    ):
        result = _run(_list(session_name="sess-1"))
        assert result["success"] is False
        assert "disabled" not in result, result
        assert result["outcomes"] == []

    def test_report_outcome_unreadable_settings_is_not_disabled(
        self, mcp_over_testclient, isolated_db, _unreadable_settings
    ):
        result = _run(_report())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "PermissionError" in result["error"]

    def test_list_outcomes_unreadable_settings_is_not_disabled(
        self, mcp_over_testclient, isolated_db, _unreadable_settings
    ):
        result = _run(_list(session_name="sess-1"))
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "PermissionError" in result["error"]
