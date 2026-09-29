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


class TestStoreLessonMcpTool:
    """store_lesson targets the WORKER's agent scope, not the caller's.

    Review finding (PR #515): memory_store resolves agent scope from the
    calling terminal, so retrospector lessons landed under 'retrospector'
    and promotion for the worker found nothing.
    """

    def _retro_ctx(self):
        return {
            "terminal_id": "term-retro",
            "session_name": "sess-retro",
            "agent_profile": "retrospector",
            "provider": "claude_code",
            "cwd": "/tmp",
        }

    def test_disabled_returns_disabled_payload(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server.server import store_lesson

        with patch(LEARNING_TARGET, return_value=False):
            result = asyncio.run(
                store_lesson(
                    target_agent_profile="transformer",
                    content="A lesson.",
                    key=None,
                    tags=None,
                )
            )
        assert result["success"] is False
        assert result["disabled"] is True

    def test_blank_target_is_error(self, isolated_db):
        import asyncio

        from cli_agent_orchestrator.mcp_server.server import store_lesson

        with patch(LEARNING_TARGET, return_value=True):
            result = asyncio.run(
                store_lesson(target_agent_profile="  ", content="A lesson.", key=None, tags=None)
            )
        assert result["success"] is False
        assert "target_agent_profile" in result["error"]

    def test_lesson_lands_in_target_worker_scope(self, isolated_db, tmp_path):
        """Called from a retrospector terminal, the persisted scope_id is the WORKER."""
        import asyncio

        from sqlalchemy import create_engine

        from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import store_lesson
        from cli_agent_orchestrator.services.memory_service import MemoryService

        engine = create_engine(
            f"sqlite:///{tmp_path / 'lesson.db'}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)
        svc = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(
                "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
                return_value=True,
            ),
            patch.object(srv, "_get_terminal_context_from_env", return_value=self._retro_ctx()),
            patch(
                "cli_agent_orchestrator.services.memory_service.MemoryService",
                return_value=svc,
            ),
        ):
            result = asyncio.run(
                store_lesson(
                    target_agent_profile="transformer",
                    content="Honor Lookup cache modes. Applies when: translating Lookups.",
                    key="honor-lookup-cache-mode",
                    tags=None,
                )
            )

        assert result["success"] is True, result
        assert result["scope"] == "agent"
        assert result["scope_id"] == "transformer"  # the worker, NOT 'retrospector'

        # And the persisted metadata row agrees.
        from sqlalchemy.orm import sessionmaker

        Session = sessionmaker(bind=engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="honor-lookup-cache-mode").one()
            assert row.scope == "agent"
            assert row.scope_id == "transformer"
            assert row.memory_type == "feedback"
            # Provenance still identifies the actual caller.
            assert row.source_terminal_id == "term-retro"


class TestStoreLessonAuthorization:
    """Cross-agent lesson writes require server-side authorization.

    Review finding (PR #515): store_lesson accepted any target_agent_profile
    from any caller — a persistent cross-agent instruction-injection path.
    Authorization is the caller PROFILE's 'store_lesson' capability, resolved
    server-side from the terminal record + profile frontmatter, never from
    tool arguments.
    """

    def _ctx(self, profile: str) -> dict:
        return {
            "terminal_id": "term-attacker",
            "session_name": "sess-x",
            "agent_profile": profile,
            "provider": "claude_code",
            "cwd": "/tmp",
        }

    def test_non_privileged_caller_cannot_write_other_scope(self, isolated_db):
        """A worker without the capability is refused a cross-agent write."""
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import store_lesson

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(
                srv, "_get_terminal_context_from_env", return_value=self._ctx("developer")
            ),
            patch.object(srv, "_caller_has_store_lesson_capability", return_value=False),
        ):
            result = asyncio.run(
                store_lesson(
                    target_agent_profile="reviewer",
                    content="Attacker-chosen instruction. Applies when: always.",
                    key="evil-lesson",
                    tags=None,
                )
            )
        assert result["success"] is False
        assert "not authorized" in result["error"]

    def test_context_free_caller_is_refused(self, isolated_db):
        """Missing terminal context fails closed — no anonymous writes."""
        import asyncio

        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import store_lesson

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(srv, "_get_terminal_context_from_env", return_value=None),
        ):
            result = asyncio.run(
                store_lesson(
                    target_agent_profile="reviewer",
                    content="Anonymous instruction.",
                    key=None,
                    tags=None,
                )
            )
        assert result["success"] is False
        assert "terminal context" in result["error"]

    def test_self_write_needs_no_capability(self, isolated_db, tmp_path):
        """target == caller grants nothing beyond memory_store; allowed."""
        import asyncio

        from sqlalchemy import create_engine

        from cli_agent_orchestrator.clients.database import Base
        from cli_agent_orchestrator.mcp_server import server as srv
        from cli_agent_orchestrator.mcp_server.server import store_lesson
        from cli_agent_orchestrator.services.memory_service import MemoryService

        engine = create_engine(
            f"sqlite:///{tmp_path / 's.db'}", connect_args={"check_same_thread": False}
        )
        Base.metadata.create_all(bind=engine)
        svc = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(
                "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
                return_value=True,
            ),
            patch.object(
                srv, "_get_terminal_context_from_env", return_value=self._ctx("developer")
            ),
            patch.object(srv, "_caller_has_store_lesson_capability", return_value=False),
            patch(
                "cli_agent_orchestrator.services.memory_service.MemoryService",
                return_value=svc,
            ),
        ):
            result = asyncio.run(
                store_lesson(
                    target_agent_profile="developer",
                    content="My own lesson. Applies when: always.",
                    key="own-lesson",
                    tags=None,
                )
            )
        assert result["success"] is True, result
        assert result["scope_id"] == "developer"

    def test_capability_check_uses_profile_frontmatter(self):
        """The real capability helper reads the caller profile's frontmatter."""
        from cli_agent_orchestrator.mcp_server.server import (
            _caller_has_store_lesson_capability,
        )

        # Built-in retrospector declares the capability.
        assert _caller_has_store_lesson_capability("retrospector") is True
        # Built-in memory_manager does not.
        assert _caller_has_store_lesson_capability("memory_manager") is False
        # Unknown/missing profiles fail closed.
        assert _caller_has_store_lesson_capability("no-such-profile") is False
        assert _caller_has_store_lesson_capability(None) is False
