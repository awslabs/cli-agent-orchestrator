"""``store_lesson``, exercised as an HTTP client (#744).

The tool used to call ``is_learning_enabled()`` and instantiate
``MemoryService()`` in the agent's own process, so it failed outright for any
agent that does not share a filesystem with cao-server, and an unreadable
``settings.json`` resolved to ``{"disabled": true}`` — which
``skills/cao-learning`` tells agents to skip SILENTLY, so the lesson vanished
with no error anywhere. It now calls ``POST /lessons``.

The translation rules match the outcome tools (see test_outcome_tools.py):

* the route's learning gate 404 — and ONLY a 404 carrying
  ``LEARNING_DISABLED_CODE`` — becomes ``disabled: True``;
* a generic 404 (route missing on an older cao-server, or a proxy that does not
  know the path) stays an explicit failure;
* a 503 (settings unreadable) and a transport failure are never ``disabled``.

Run over ``mcp_over_testclient`` (see conftest), which routes the tool's real
``requests`` calls into the real FastAPI routes in-process.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.mcp_server.server import store_lesson

LEARNING_TARGET = "cli_agent_orchestrator.services.settings_service.is_learning_enabled"
CAPABILITY_TARGET = "cli_agent_orchestrator.utils.agent_profiles.profile_declares_capability"
METADATA_TARGET = "cli_agent_orchestrator.api.main.get_terminal_metadata"
MEMORY_SERVICE_TARGET = "cli_agent_orchestrator.api.main._get_memory_service"


def _run(coro):
    return asyncio.run(coro)


def _store(**overrides):
    kwargs = dict(
        target_agent_profile="transformer",
        content="Honor Lookup cache modes. Applies when: translating Lookups.",
        key="honor-lookup-cache-mode",
        tags=None,
    )
    kwargs.update(overrides)
    return store_lesson(**kwargs)


@pytest.fixture
def server_side(tmp_path):
    """Give the route a tmp MemoryService, a caller record and the capability."""
    from cli_agent_orchestrator.services.memory_service import MemoryService

    engine = create_engine(
        f"sqlite:///{tmp_path / 'lesson.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    service = MemoryService(base_dir=tmp_path / "mem", db_engine=engine)
    record = {
        "id": "abc12345",
        "tmux_session": "sess-1",
        "provider": "kiro_cli",
        "agent_profile": "retrospector",
    }
    with (
        patch("cli_agent_orchestrator.clients.database.SessionLocal", factory),
        patch(
            "cli_agent_orchestrator.services.memory_service._is_memory_enabled",
            return_value=True,
        ),
        patch(MEMORY_SERVICE_TARGET, return_value=service),
        patch(METADATA_TARGET, return_value=record),
        patch(CAPABILITY_TARGET, return_value=True),
    ):
        service.engine = engine
        yield service


class TestStoreLessonOverHttp:
    def test_round_trip_targets_the_worker_scope(self, mcp_over_testclient, server_side):
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store())
        assert result["success"] is True, result
        assert result["scope"] == "agent"
        assert result["scope_id"] == "transformer"
        assert result["target_agent_profile"] == "transformer"

    def test_it_posts_to_lessons_and_opens_no_local_service(self, mcp_over_testclient, server_side):
        with patch(LEARNING_TARGET, return_value=True):
            _run(_store())
        assert mcp_over_testclient.last.path == "/lessons"
        assert mcp_over_testclient.last.method == "POST"

    def test_only_the_terminal_id_travels_never_a_caller_profile(
        self, mcp_over_testclient, server_side
    ):
        """The caller profile is the authorization input, so it is not sent.

        If the agent process could put ``agent_profile`` on the wire the
        capability check would be self-attested — which is the whole point of
        resolving it from the terminal record server-side.
        """
        with patch(LEARNING_TARGET, return_value=True):
            _run(_store())
        body = mcp_over_testclient.last.json
        assert body["source_terminal_id"] == "abc12345"
        assert "agent_profile" not in body
        assert "terminal_context" not in body
        assert "caller_profile" not in body

    def test_blank_target_issues_no_request(self, mcp_over_testclient, server_side):
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store(target_agent_profile="  "))
        assert result["success"] is False
        assert "target_agent_profile" in result["error"]
        assert mcp_over_testclient.requests == []

    def test_no_terminal_id_issues_no_request(self, mcp_over_testclient, server_side, monkeypatch):
        monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store())
        assert result["success"] is False
        assert "terminal context" in result["error"]
        assert mcp_over_testclient.requests == []

    def test_cross_agent_write_without_capability_is_a_plain_error(
        self, mcp_over_testclient, server_side
    ):
        with (
            patch(LEARNING_TARGET, return_value=True),
            patch(CAPABILITY_TARGET, return_value=False),
        ):
            result = _run(_store())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "not authorized" in result["error"]


class TestStoreLessonFailureTranslation:
    """The honesty rules: only a feature gate may read as "disabled"."""

    def test_disabled_learning_returns_a_disabled_payload(self, mcp_over_testclient, server_side):
        from cli_agent_orchestrator.services.outcome_service import LEARNING_DISABLED_CODE

        with patch(LEARNING_TARGET, return_value=False):
            result = _run(_store())
        assert result["success"] is False
        assert result["disabled"] is True
        assert LEARNING_DISABLED_CODE in result["error"]

    def test_unreadable_settings_is_not_disabled(self, mcp_over_testclient, server_side):
        """The regression this issue is about: a filesystem fault is not an opt-out."""
        from cli_agent_orchestrator.services.settings_service import LearningStatus

        unreadable = LearningStatus(
            False, True, "CAO settings.json could not be read (PermissionError)"
        )
        with (
            patch(LEARNING_TARGET, return_value=False),
            patch(
                "cli_agent_orchestrator.services.settings_service.learning_status",
                return_value=unreadable,
            ),
        ):
            result = _run(_store())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "PermissionError" in result["error"]

    def test_unreachable_server_is_an_explicit_failure(
        self, mcp_over_testclient, server_side, monkeypatch
    ):
        """#744: the down-server path was untested and surfaced a raw exception."""
        from cli_agent_orchestrator.mcp_server import utils as mcp_utils

        def boom(*_a, **_kw):
            raise requests.ConnectionError("connection refused")

        monkeypatch.setattr(mcp_utils.requests, "post", boom)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "connect to cao-server" in result["error"]

    @staticmethod
    def _patch_lessons_404(monkeypatch, detail: str):
        from cli_agent_orchestrator.mcp_server import utils as mcp_utils

        class _Resp:
            status_code = 404

            def json(self):
                return {"detail": detail}

        real = mcp_utils.requests.post

        def fake(url, *a, **kw):
            if "/lessons" in str(url):
                raise requests.HTTPError("404", response=_Resp())
            return real(url, *a, **kw)

        monkeypatch.setattr(mcp_utils.requests, "post", fake)

    def test_route_missing_404_is_an_explicit_failure(
        self, mcp_over_testclient, server_side, monkeypatch
    ):
        """A bare 404 means "no such route here", not "the feature is off"."""
        self._patch_lessons_404(monkeypatch, "Not Found")
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store())
        assert result["success"] is False
        assert "disabled" not in result, result
        assert "Not Found" in result["error"]

    def test_gate_404_still_reads_as_disabled(self, mcp_over_testclient, server_side, monkeypatch):
        from cli_agent_orchestrator.services.outcome_service import (
            LEARNING_DISABLED_CODE,
            LEARNING_DISABLED_MESSAGE,
        )

        self._patch_lessons_404(monkeypatch, LEARNING_DISABLED_MESSAGE)
        with patch(LEARNING_TARGET, return_value=True):
            result = _run(_store())
        assert result["disabled"] is True
        assert LEARNING_DISABLED_CODE in result["error"]

    def test_partial_write_keeps_its_typed_payload(
        self, mcp_over_testclient, server_side, monkeypatch
    ):
        """A repairable on-disk state must not collapse into a generic 500 string."""
        from cli_agent_orchestrator.services.memory_service import MemoryPartialWriteError

        with (
            patch(LEARNING_TARGET, return_value=True),
            patch.object(
                server_side,
                "store",
                side_effect=MemoryPartialWriteError(
                    key="honor-lookup-cache-mode",
                    scope="agent",
                    scope_id="transformer",
                    file_path="/tmp/lesson.md",
                ),
            ),
        ):
            result = _run(_store())
        assert result["success"] is False
        assert result["error_kind"] == MemoryPartialWriteError.error_kind
        assert result["partial_write"]["key"] == "honor-lookup-cache-mode"
        assert result["partial_write"]["scope_id"] == "transformer"
        assert result["partial_write"]["repair_command"]


class TestCapabilityHelperLivesServerSide:
    """The capability list is on the cao-server node, so the check must be too.

    An agent that cannot read the profile store used to fail closed here and be
    refused a write it was authorized for.
    """

    def test_reads_the_profile_frontmatter(self):
        from cli_agent_orchestrator.utils.agent_profiles import profile_declares_capability

        # Built-in retrospector declares the capability.
        assert profile_declares_capability("retrospector", "store_lesson") is True
        # Built-in memory_manager does not.
        assert profile_declares_capability("memory_manager", "store_lesson") is False
        # Unknown/missing profiles fail closed.
        assert profile_declares_capability("no-such-profile", "store_lesson") is False
        assert profile_declares_capability(None, "store_lesson") is False

    def test_profile_store_fault_raises_rather_than_fails_closed(self):
        """A PermissionError reading the profile store is not a known "no" — it
        must not be swallowed into False, which would answer a confident 403 to
        an authorization question the lookup never actually answered."""
        from cli_agent_orchestrator.utils.agent_profiles import profile_declares_capability

        with patch(
            "cli_agent_orchestrator.utils.agent_profiles._read_agent_profile_source",
            side_effect=PermissionError("Permission denied"),
        ):
            with pytest.raises(RuntimeError):
                profile_declares_capability("retrospector", "store_lesson")
