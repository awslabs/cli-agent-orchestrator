"""Tests for U2 — SessionEventModel + Event Logging (Phase 2).

Covers:
- SessionEventModel schema and table creation
- log_session_event() non-blocking insert
- get_session_timeline() ordered retrieval
- Event logging at lifecycle points (agent_launched, task_started, task_completed,
  handoff_returned, memory_stored)
- Non-blocking guarantee: log failures must not propagate
"""

import uuid
from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    SessionEventModel,
    get_session_timeline,
    log_session_event,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_engine():
    """Create a fresh in-memory SQLite database per test."""
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture()
def db_session(db_engine):
    """Provide a session factory bound to the test engine."""
    return sessionmaker(bind=db_engine)


# ---------------------------------------------------------------------------
# U2.1 — SessionEventModel schema
# ---------------------------------------------------------------------------


class TestSessionEventModelSchema:
    """Verify the ORM model has the expected columns and defaults."""

    def test_table_name(self):
        assert SessionEventModel.__tablename__ == "session_events"

    def test_columns_exist(self):
        col_names = {c.name for c in SessionEventModel.__table__.columns}
        expected = {
            "id",
            "session_name",
            "terminal_id",
            "provider",
            "event_type",
            "summary",
            "metadata_json",
            "created_at",
        }
        assert expected == col_names

    def test_session_name_indexed(self):
        col = SessionEventModel.__table__.columns["session_name"]
        assert col.index is True


# ---------------------------------------------------------------------------
# U2.2 — Table auto-created by create_all
# ---------------------------------------------------------------------------


class TestSessionEventsTableCreation:
    def test_table_created(self, db_engine):
        from sqlalchemy import inspect

        inspector = inspect(db_engine)
        tables = inspector.get_table_names()
        assert "session_events" in tables


# ---------------------------------------------------------------------------
# U2.3 — log_session_event() + get_session_timeline()
# ---------------------------------------------------------------------------


class TestLogSessionEvent:
    """Test log_session_event() inserts rows correctly."""

    def test_inserts_event(self, db_engine, db_session):
        """log_session_event should insert a row into session_events."""
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            log_session_event(
                session_name="cao-test-session",
                terminal_id="tid-001",
                provider="claude_code",
                event_type="agent_launched",
                summary="Agent code_supervisor launched",
            )

        with db_session() as s:
            rows = s.query(SessionEventModel).all()
            assert len(rows) == 1
            row = rows[0]
            assert row.session_name == "cao-test-session"
            assert row.terminal_id == "tid-001"
            assert row.provider == "claude_code"
            assert row.event_type == "agent_launched"
            assert row.summary == "Agent code_supervisor launched"
            assert row.metadata_json == "{}"
            assert row.created_at is not None

    def test_custom_metadata_json(self, db_engine, db_session):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            log_session_event(
                session_name="s1",
                terminal_id="t1",
                provider="codex",
                event_type="task_completed",
                summary="done",
                metadata_json='{"elapsed": 42}',
            )

        with db_session() as s:
            row = s.query(SessionEventModel).first()
            assert row.metadata_json == '{"elapsed": 42}'

    def test_unique_ids(self, db_engine, db_session):
        """Each event should get a unique UUID."""
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            for i in range(3):
                log_session_event(
                    session_name="s1",
                    terminal_id="t1",
                    provider="p",
                    event_type="agent_launched",
                    summary=f"event {i}",
                )

        with db_session() as s:
            ids = [r.id for r in s.query(SessionEventModel).all()]
            assert len(set(ids)) == 3


class TestLogSessionEventNonBlocking:
    """log_session_event must never raise — failures logged as warnings."""

    def test_db_error_does_not_raise(self, db_engine, db_session):
        """Simulate a DB error; log_session_event must swallow it."""
        bad_session = MagicMock()
        bad_session().__enter__ = MagicMock(side_effect=RuntimeError("db exploded"))
        bad_session().__exit__ = MagicMock(return_value=False)

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", bad_session):
            # Should NOT raise
            log_session_event(
                session_name="s1",
                terminal_id="t1",
                provider="p",
                event_type="agent_launched",
            )


class TestGetSessionTimeline:
    """Test get_session_timeline() returns ordered events."""

    def test_returns_ordered_events(self, db_engine, db_session):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            log_session_event(
                session_name="s1", terminal_id="t1", provider="p", event_type="agent_launched"
            )
            log_session_event(
                session_name="s1", terminal_id="t1", provider="p", event_type="task_started"
            )
            log_session_event(
                session_name="s1", terminal_id="t1", provider="p", event_type="task_completed"
            )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            timeline = get_session_timeline("s1")

        assert len(timeline) == 3
        assert timeline[0]["event_type"] == "agent_launched"
        assert timeline[1]["event_type"] == "task_started"
        assert timeline[2]["event_type"] == "task_completed"

    def test_filters_by_session(self, db_engine, db_session):
        """Events from other sessions must not appear."""
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            log_session_event(
                session_name="s1", terminal_id="t1", provider="p", event_type="agent_launched"
            )
            log_session_event(
                session_name="s2", terminal_id="t2", provider="p", event_type="agent_launched"
            )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            timeline = get_session_timeline("s1")

        assert len(timeline) == 1
        assert timeline[0]["session_name"] == "s1"

    def test_limit_parameter(self, db_engine, db_session):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            for i in range(10):
                log_session_event(
                    session_name="s1",
                    terminal_id="t1",
                    provider="p",
                    event_type=f"event_{i}",
                )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            timeline = get_session_timeline("s1", limit=3)

        assert len(timeline) == 3

    def test_returns_all_fields(self, db_engine, db_session):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            log_session_event(
                session_name="s1",
                terminal_id="t1",
                provider="claude_code",
                event_type="task_started",
                summary="hello world",
                metadata_json='{"key": "val"}',
            )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            timeline = get_session_timeline("s1")

        row = timeline[0]
        assert "id" in row
        assert row["session_name"] == "s1"
        assert row["terminal_id"] == "t1"
        assert row["provider"] == "claude_code"
        assert row["event_type"] == "task_started"
        assert row["summary"] == "hello world"
        assert row["metadata_json"] == '{"key": "val"}'
        assert "created_at" in row

    def test_empty_timeline(self, db_engine, db_session):
        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            timeline = get_session_timeline("nonexistent")
        assert timeline == []


# ---------------------------------------------------------------------------
# U2.4 — Event logging wired at lifecycle points (integration-style mocks)
# ---------------------------------------------------------------------------


class TestTerminalServiceEvents:
    """Verify create_terminal and send_input log events."""

    @patch("cli_agent_orchestrator.services.terminal_service.log_session_event")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    def test_create_terminal_logs_agent_launched(
        self,
        mock_skill,
        mock_profile,
        mock_db_create,
        mock_pm,
        mock_tmux,
        mock_log_event,
    ):
        """create_terminal() should log an agent_launched event."""
        mock_skill.return_value = ""
        mock_profile.return_value = MagicMock(mcpServers=None, allowedTools=None, role="developer")
        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "win-1"
        provider_inst = MagicMock()
        mock_pm.create_provider.return_value = provider_inst

        from cli_agent_orchestrator.services.terminal_service import create_terminal

        create_terminal(
            provider="claude_code",
            agent_profile="developer",
            session_name="cao-test",
        )

        # Find the agent_launched call
        launched_calls = [
            c
            for c in mock_log_event.call_args_list
            if c.kwargs.get("event_type") == "agent_launched"
            or (len(c.args) > 3 and c.args[3] == "agent_launched")
        ]
        assert len(launched_calls) >= 1

    @patch("cli_agent_orchestrator.services.terminal_service.log_session_event")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.tmux_client")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    def test_send_input_logs_task_started_on_first_message(
        self,
        mock_update,
        mock_pm,
        mock_tmux,
        mock_meta,
        mock_log_event,
    ):
        """send_input() should log task_started on the first message only."""
        from cli_agent_orchestrator.services.terminal_service import (
            _memory_injected_terminals,
            send_input,
        )

        tid = f"test-{uuid.uuid4().hex[:8]}"
        _memory_injected_terminals.discard(tid)

        mock_meta.return_value = {
            "tmux_session": "cao-s",
            "tmux_window": "w",
            "provider": "claude_code",
        }
        provider_inst = MagicMock()
        provider_inst.paste_enter_count = 1
        mock_pm.get_provider.return_value = provider_inst

        send_input(tid, "Build feature X")

        started_calls = [
            c
            for c in mock_log_event.call_args_list
            if c.kwargs.get("event_type") == "task_started"
            or (len(c.args) > 3 and c.args[3] == "task_started")
        ]
        assert len(started_calls) >= 1

        # Clean up
        _memory_injected_terminals.discard(tid)


# ---------------------------------------------------------------------------
# U2.5 — get_session_timeline integration round-trip
# ---------------------------------------------------------------------------


class TestHandoffReturnedEventWiring:
    """U10.2: handoff complete → handoff_returned event logged in DB."""

    @patch("cli_agent_orchestrator.mcp_server.server.requests")
    @patch("cli_agent_orchestrator.mcp_server.server.wait_until_terminal_status")
    @patch("cli_agent_orchestrator.mcp_server.server._create_terminal")
    @patch("cli_agent_orchestrator.mcp_server.server._send_direct_input_handoff")
    @patch("cli_agent_orchestrator.clients.database.log_session_event")
    @patch(
        "cli_agent_orchestrator.clients.database.get_terminal_metadata",
        return_value={"tmux_session": "cao-test", "tmux_window": "w1", "provider": "claude_code"},
    )
    def test_handoff_logs_handoff_returned_event(
        self, mock_get_meta, mock_log, mock_send, mock_create, mock_wait, mock_requests
    ):
        """_handoff_impl should log a handoff_returned event on success."""
        import asyncio

        mock_create.return_value = ("tid-hoff", "claude_code")
        mock_wait.return_value = True

        # Mock output retrieval and exit
        output_resp = MagicMock()
        output_resp.json.return_value = {"output": "handoff result"}
        exit_resp = MagicMock()
        mock_requests.get.return_value = output_resp
        mock_requests.post.return_value = exit_resp

        with (
            patch("cli_agent_orchestrator.providers.manager.provider_manager") as mock_pm,
            patch.dict("os.environ", {"CAO_TERMINAL_ID": "caller-tid"}),
        ):
            mock_pm.get_provider.return_value = None  # skip session context extraction

            from cli_agent_orchestrator.mcp_server.server import _handoff_impl

            result = asyncio.run(_handoff_impl("developer", "Build feature X"))

        assert result.success is True

        # Find handoff_returned call
        hr_calls = [
            c
            for c in mock_log.call_args_list
            if c.kwargs.get("event_type") == "handoff_returned"
        ]
        assert len(hr_calls) >= 1
        assert hr_calls[0].kwargs["provider"] == "claude_code"


class TestFullEventRoundtrip:
    """Full round-trip: log multiple event types, retrieve timeline."""

    def test_full_lifecycle(self, db_engine, db_session):
        events = [
            ("agent_launched", "Agent launched"),
            ("task_started", "Build feature X"),
            ("memory_stored", "Stored: design_decisions"),
            ("task_completed", "Task done in 12s"),
            ("handoff_returned", "Handoff completed"),
        ]

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", db_session):
            for etype, summary in events:
                log_session_event(
                    session_name="cao-lifecycle",
                    terminal_id="tid-lc",
                    provider="claude_code",
                    event_type=etype,
                    summary=summary,
                )

            timeline = get_session_timeline("cao-lifecycle")

        assert len(timeline) == 5
        types = [e["event_type"] for e in timeline]
        assert types == [
            "agent_launched",
            "task_started",
            "memory_stored",
            "task_completed",
            "handoff_returned",
        ]
