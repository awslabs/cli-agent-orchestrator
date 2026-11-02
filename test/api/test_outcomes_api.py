"""Tests for POST/GET /outcomes (self-learning Phase 1 HTTP surface).

Mirrors test/api/test_memory_export_api.py: the learning gate is patched at
the settings seam; storage tests use a real OutcomeService on a tmp engine
patched into the service constructor's session factory.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"


@pytest.fixture
def isolated_db(tmp_path):
    """Patch the global SessionLocal used by OutcomeService to a tmp engine."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'outcomes.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", factory):
        yield engine


BODY = {
    "session_name": "ssis-batch-1",
    "task_label": "convert package CustomerETL",
    "success": False,
    "workflow_name": "ssis-migration",
    "agent_profile": "transformer",
    "score": 40,
    "friction_notes": "Lookup component with partial cache not mapped.",
}


class TestOutcomesGates:
    def test_post_disabled_is_404(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            response = client.post("/outcomes", json=BODY)
        assert response.status_code == 404
        assert "disabled" in response.json()["detail"].lower()

    def test_get_disabled_is_404(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=False):
            response = client.get("/outcomes")
        assert response.status_code == 404


class TestOutcomesRoundTrip:
    def test_post_then_get(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json=BODY)
            assert response.status_code == 200, response.text
            outcome = response.json()["outcome"]
            assert outcome["id"]
            assert outcome["success"] is False
            assert outcome["score"] == 40

            listed = client.get("/outcomes?session_name=ssis-batch-1")
            assert listed.status_code == 200
            data = listed.json()
            assert data["count"] == 1
            assert data["outcomes"][0]["task_label"] == "convert package CustomerETL"

    def test_get_filters_by_agent(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            client.post("/outcomes", json=BODY)
            client.post(
                "/outcomes",
                json={**BODY, "agent_profile": "improver", "task_label": "patch backend"},
            )
            listed = client.get("/outcomes?agent_profile=improver")
            assert listed.json()["count"] == 1
            assert listed.json()["outcomes"][0]["agent_profile"] == "improver"

    def test_post_invalid_score_is_400(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json={**BODY, "score": 150})
        assert response.status_code == 400
        assert "score" in response.json()["detail"]

    def test_post_blank_task_label_is_400(self, client, isolated_db):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/outcomes", json={**BODY, "task_label": "  "})
        assert response.status_code == 400


# The report_outcome / list_outcomes tool cases moved to
# test/mcp_server/test_outcome_tools.py: those tools are HTTP clients now, so
# driving them against an in-process OutcomeService no longer exercises the real
# path. The store_lesson cases below are unchanged by that move.


class TestReportOutcomeErrorPaths:

    def test_post_learning_disabled_error_from_service_is_404(self, client, isolated_db):
        """Race: gate passes but service raises LearningDisabledError."""
        from cli_agent_orchestrator.services.outcome_service import LearningDisabledError

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(
                "cli_agent_orchestrator.services.outcome_service.OutcomeService.record_outcome",
                side_effect=LearningDisabledError("disabled"),
            ),
        ):
            response = client.post("/outcomes", json=BODY)
        assert response.status_code == 404


class TestOutcomesAuth:
    """Regression: GET /outcomes must be scope-gated when OAuth is enabled.

    Review finding (PR #515): the read route shipped without
    require_any_scope, serving outcome data (session names, agent identity,
    friction notes) to unauthenticated callers on auth-enabled servers.
    """

    def test_get_outcomes_has_scope_dependency(self):
        from cli_agent_orchestrator.api.main import app

        route = next(
            r for r in app.routes if getattr(r, "path", None) == "/outcomes" and "GET" in r.methods
        )
        stack = list(route.dependant.dependencies)
        found = False
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None and "require_any_scope" in getattr(call, "__qualname__", ""):
                found = True
                break
            stack.extend(getattr(dep, "dependencies", []))
        assert found, "GET /outcomes is missing a require_any_scope dependency"

    def test_get_outcomes_missing_token_is_401_when_auth_enabled(
        self, client, isolated_db, monkeypatch
    ):
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
        with patch(LEARNING_TARGET, return_value=True):
            response = client.get("/outcomes")
        assert response.status_code == 401


# The store_lesson tool cases moved to test/mcp_server/test_lesson_tool.py, and
# the POST /lessons route cases to test/api/test_lessons_api.py: store_lesson is
# an HTTP client now (#744), so driving it against an in-process MemoryService
# would pass while exercising a path production no longer takes.
