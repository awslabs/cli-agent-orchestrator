"""Tests for POST /lessons — the server-side lesson write path (#744).

``store_lesson`` used to decide the learning gate and open SQLite inside the
AGENT's process. Both now happen here, which is also where the caller's profile
and the ``store_lesson`` capability can be resolved from artifacts the agent
cannot forge: its own terminal record and the profile file's frontmatter.

Three contracts carry the weight:

* an unreadable ``settings.json`` answers 503, never the gate's 404 — the tool
  turns a gate 404 into ``disabled: true``, which ``skills/cao-learning`` tells
  agents to skip SILENTLY;
* a cross-agent write without the capability is 403, not a silent success;
* the lesson lands in the TARGET profile's scope while provenance still names
  the real caller.
"""

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"
STATUS_TARGET = "cli_agent_orchestrator.services.settings_service.learning_status"
CAPABILITY_TARGET = "cli_agent_orchestrator.utils.agent_profiles.profile_declares_capability"
METADATA_TARGET = "cli_agent_orchestrator.api.main.get_terminal_metadata"
MEMORY_SERVICE_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"

BODY = {
    "target_agent_profile": "transformer",
    "content": "Honor Lookup cache modes. Applies when: translating Lookups.",
    "key": "honor-lookup-cache-mode",
    "tags": "ssis",
    "source_terminal_id": "term-retro",
}


def _record(profile: str = "retrospector") -> dict:
    return {
        "id": "term-retro",
        "tmux_session": "sess-retro",
        "provider": "claude_code",
        "agent_profile": profile,
    }


@pytest.fixture
def memory_service(tmp_path):
    """A real MemoryService on a tmp engine, patched into the route's factory."""
    from cli_agent_orchestrator.services.memory_service import MemoryService

    engine = create_engine(
        f"sqlite:///{tmp_path / 'lesson.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    service = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)
    with (
        patch("cli_agent_orchestrator.clients.database.SessionLocal", factory),
        patch(
            "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
            return_value=True,
        ),
        patch(MEMORY_SERVICE_TARGET, return_value=service),
    ):
        service.engine = engine
        yield service


class TestLessonsGate:
    def test_disabled_is_404_with_the_discriminator(self, client):
        from cli_agent_orchestrator.services.outcome_service import LEARNING_DISABLED_CODE

        with patch(LEARNING_TARGET, return_value=False):
            response = client.post("/lessons", json=BODY)
        assert response.status_code == 404
        assert LEARNING_DISABLED_CODE in response.json()["detail"]

    def test_unreadable_settings_is_503_not_the_gates_404(self, client):
        """A filesystem fault must not present itself as a deliberate opt-out."""
        from cli_agent_orchestrator.services.outcome_service import LEARNING_DISABLED_CODE
        from cli_agent_orchestrator.services.settings_service import LearningStatus

        unreadable = LearningStatus(
            False, True, "settings.json could not be read (PermissionError)"
        )
        with (
            patch(LEARNING_TARGET, return_value=False),
            patch(STATUS_TARGET, return_value=unreadable),
        ):
            response = client.post("/lessons", json=BODY)
        assert response.status_code == 503
        assert "PermissionError" in response.json()["detail"]
        assert LEARNING_DISABLED_CODE not in response.json()["detail"]


class TestLessonsAuthorization:
    def test_blank_target_is_400(self, client):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/lessons", json={**BODY, "target_agent_profile": "  "})
        assert response.status_code == 400
        assert "target_agent_profile" in response.json()["detail"]

    def test_missing_source_terminal_is_400(self, client):
        with patch(LEARNING_TARGET, return_value=True):
            response = client.post("/lessons", json={**BODY, "source_terminal_id": None})
        assert response.status_code == 400
        assert "source_terminal_id" in response.json()["detail"]

    def test_unregistered_terminal_is_400(self, client):
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=None),
        ):
            response = client.post("/lessons", json=BODY)
        assert response.status_code == 400
        assert "not registered" in response.json()["detail"]

    def test_cross_agent_write_without_the_capability_is_403(self, client):
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("developer")),
            patch(CAPABILITY_TARGET, return_value=False),
        ):
            response = client.post("/lessons", json=BODY)
        assert response.status_code == 403
        assert "not authorized" in response.json()["detail"]

    def test_capability_lookup_fault_is_503_not_the_denials_403(self, client):
        """A profile-store read fault is not a known "no" — reporting 403 here is
        the exact fault-laundering this issue is about, just moved server-side."""
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("developer")),
            patch(
                CAPABILITY_TARGET,
                side_effect=RuntimeError(
                    "Failed to load agent profile 'developer': Permission denied"
                ),
            ),
        ):
            response = client.post("/lessons", json=BODY)
        assert response.status_code == 503
        assert "developer" in response.json()["detail"]

    def test_capability_is_read_for_the_record_profile_not_the_body(self, client, memory_service):
        """The authorization input is the terminal record's profile, full stop."""
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("developer")),
            patch(CAPABILITY_TARGET, return_value=False) as cap,
        ):
            client.post("/lessons", json=BODY)
        cap.assert_called_once_with("developer", "store_lesson")

    def test_self_write_needs_no_capability(self, client, memory_service):
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("developer")),
            patch(CAPABILITY_TARGET, return_value=False),
        ):
            response = client.post("/lessons", json={**BODY, "target_agent_profile": "developer"})
        assert response.status_code == 200, response.text
        assert response.json()["scope_id"] == "developer"


class TestLessonsWrite:
    def test_lesson_lands_in_the_target_worker_scope(self, client, memory_service):
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("retrospector")),
            patch(CAPABILITY_TARGET, return_value=True),
        ):
            response = client.post("/lessons", json=BODY)

        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["success"] is True
        assert payload["scope"] == "agent"
        assert payload["scope_id"] == "transformer"  # the worker, NOT 'retrospector'
        assert payload["target_agent_profile"] == "transformer"

        Session = sessionmaker(bind=memory_service.engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="honor-lookup-cache-mode").one()
            assert row.scope == "agent"
            assert row.scope_id == "transformer"
            assert row.memory_type == "feedback"
            # Provenance still identifies the actual caller.
            assert row.source_terminal_id == "term-retro"

    def test_non_slug_key_is_normalized_not_rejected(self, client, memory_service):
        """LenientMemoryKey: the MCP tool has always let the service sanitize."""
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(METADATA_TARGET, return_value=_record("retrospector")),
            patch(CAPABILITY_TARGET, return_value=True),
        ):
            response = client.post("/lessons", json={**BODY, "key": "Honor Lookup Cache"})
        assert response.status_code == 200, response.text


class TestLessonsAuth:
    def test_route_is_scope_gated(self):
        """A write route serving cross-agent instruction injection must be gated."""
        from cli_agent_orchestrator.api.main import app

        route = next(
            r for r in app.routes if getattr(r, "path", None) == "/lessons" and "POST" in r.methods
        )
        stack = list(route.dependant.dependencies)
        found = False
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None and "require_any_scope" in getattr(call, "__qualname__", ""):
                found = True
            stack.extend(dep.dependencies)
        assert found, "POST /lessons has no require_any_scope dependency"
