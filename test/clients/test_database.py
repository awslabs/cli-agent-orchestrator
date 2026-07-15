"""Tests for the database client."""

import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    FlowModel,
    InboxModel,
    TerminalModel,
    create_flow,
    create_inbox_message,
    create_terminal,
    delete_flow,
    delete_terminal,
    delete_terminals_by_session,
    get_flow,
    get_inbox_messages,
    get_pending_messages,
    get_terminal_group,
    get_terminal_metadata,
    init_db,
    list_flows,
    list_pending_receiver_ids_by_provider,
    list_pending_receiver_ids_older_than,
    list_siblings_by_group_prefix,
    list_terminals_by_session,
    update_flow_enabled,
    update_flow_run_times,
    update_last_active,
    update_message_status,
    update_terminal_group,
    update_terminal_metadata,
    update_terminal_shell_command,
)
from cli_agent_orchestrator.models.inbox import MessageStatus


@pytest.fixture
def test_db():
    """Create an in-memory test database."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    TestSession = sessionmaker(bind=engine)
    return TestSession


class TestTerminalOperations:
    """Tests for terminal database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_terminal(self, mock_session_class):
        """Test creating a terminal record."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        result = create_terminal("test123", "cao-session", "window-0", "kiro_cli", "developer")

        assert result["id"] == "test123"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_metadata_found(self, mock_session_class):
        """Test getting terminal metadata that exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.id = "test123"
        mock_terminal.tmux_session = "cao-session"
        mock_terminal.tmux_window = "window-0"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "developer"
        mock_terminal.allowed_tools = None
        mock_terminal.group = None
        mock_terminal.metadata_json = None
        mock_terminal.last_active = datetime.now()

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_terminal_metadata("test123")

        assert result is not None
        assert result["id"] == "test123"
        assert result["group"] is None
        assert result["metadata"] is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_metadata_not_found(self, mock_session_class):
        """Test getting terminal metadata that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_terminal_metadata("nonexistent")

        assert result is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_last_active(self, mock_session_class):
        """Test updating last active timestamp."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_last_active("test123")

        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_shell_command(self, mock_session_class):
        """Test updating shell_command baseline for a terminal."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_shell_command("test123", "bash")

        assert result is True
        assert mock_terminal.shell_command == "bash"
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_shell_command_not_found(self, mock_session_class):
        """Test updating shell_command for a terminal that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_shell_command("nonexistent", "bash")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminal(self, mock_session_class):
        """Test deleting a terminal."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 1
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminal("test123")

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminal_not_found(self, mock_session_class):
        """Test deleting a terminal that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminal("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_terminals_by_session(self, mock_session_class):
        """Test listing terminals by session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.id = "test123"
        mock_terminal.tmux_session = "cao-session"
        mock_terminal.tmux_window = "window-0"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "developer"
        mock_terminal.last_active = datetime.now()

        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_terminal]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_terminals_by_session("cao-session")

        assert len(result) == 1
        assert result[0]["id"] == "test123"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_pending_receiver_ids_by_provider(self, mock_session_class):
        """Test listing pending receivers for a specific provider."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.join.return_value.filter.return_value.distinct.return_value.all.return_value = [
            ("receiver-1",),
            ("receiver-2",),
        ]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_pending_receiver_ids_by_provider("opencode_cli")

        assert result == ["receiver-1", "receiver-2"]

    def test_list_pending_receiver_ids_older_than(self, test_db):
        """Only messages pending past the grace window — whose receiver
        terminal still exists — are returned for reconciliation (issue #131).

        Uses the real in-memory DB (not a mocked session) so the age cutoff,
        status filter, and terminal join are actually exercised.
        """
        old = datetime.now() - timedelta(seconds=120)
        fresh = datetime.now()

        with test_db() as seed:
            seed.add_all(
                [
                    TerminalModel(
                        id="term-old",
                        tmux_session="cao-s",
                        tmux_window="w",
                        provider="kiro_cli",
                    ),
                    TerminalModel(
                        id="term-fresh",
                        tmux_session="cao-s",
                        tmux_window="w",
                        provider="kiro_cli",
                    ),
                    # Stuck long enough to reconcile, receiver still alive — kept.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-old",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=old,
                    ),
                    # Too recent — left to the immediate/watchdog paths.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-fresh",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=fresh,
                    ),
                    # Already delivered — not pending.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-old",
                        message="m",
                        status=MessageStatus.DELIVERED.value,
                        created_at=old,
                    ),
                    # Receiver terminal is gone — dropped by the join.
                    InboxModel(
                        sender_id="a",
                        receiver_id="term-ghost",
                        message="m",
                        status=MessageStatus.PENDING.value,
                        created_at=old,
                    ),
                ]
            )
            seed.commit()

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_pending_receiver_ids_older_than(30)

        assert result == ["term-old"]

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_terminals_by_session(self, mock_session_class):
        """Test deleting all terminals in a session."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 2
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_terminals_by_session("cao-session")

        assert result == 2


class TestGroupAndMetadata:
    """Tests for the #432 group/metadata columns and their CRUD helpers."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_terminal_persists_group_and_metadata(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        result = create_terminal(
            "test123",
            "cao-session",
            "window-0",
            "kiro_cli",
            "developer",
            group=["tenant_1", "project_5"],
            metadata={"task": "reviewing PR"},
        )

        assert result["group"] == ["tenant_1", "project_5"]
        assert result["metadata"] == {"task": "reviewing PR"}
        added_terminal = mock_session.add.call_args[0][0]
        assert added_terminal.group == '["tenant_1", "project_5"]'
        assert added_terminal.metadata_json == '{"task": "reviewing PR"}'

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_terminal_no_group_or_metadata_stores_null(self, mock_session_class):
        """Omitting group/metadata must not write the literal string 'null'."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        result = create_terminal("test123", "cao-session", "window-0", "kiro_cli", "developer")

        assert result["group"] is None
        assert result["metadata"] is None
        added_terminal = mock_session.add.call_args[0][0]
        assert added_terminal.group is None
        assert added_terminal.metadata_json is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_terminal_explicit_empty_group_and_metadata_normalized_to_none(
        self, mock_session_class
    ):
        """Self-ROAST finding: an explicit empty container (group=[], metadata={},
        as opposed to omitted/None) is stored as NULL -- the return dict must echo
        that same normalized None, not the raw [] / {} the caller passed in, or a
        create_terminal() response would disagree with an immediately-following
        get_terminal_metadata()/GET /terminals/{id} on the same row."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        result = create_terminal(
            "test123", "cao-session", "window-0", "kiro_cli", "developer", group=[], metadata={}
        )

        assert result["group"] is None
        assert result["metadata"] is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_metadata_decodes_group_and_metadata(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.id = "test123"
        mock_terminal.tmux_session = "cao-session"
        mock_terminal.tmux_window = "window-0"
        mock_terminal.provider = "kiro_cli"
        mock_terminal.agent_profile = "developer"
        mock_terminal.allowed_tools = None
        mock_terminal.group = '["tenant_1", "project_5"]'
        mock_terminal.metadata_json = '{"task": "reviewing PR"}'
        mock_terminal.last_active = datetime.now()

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_terminal_metadata("test123")

        assert result["group"] == ["tenant_1", "project_5"]
        assert result["metadata"] == {"task": "reviewing PR"}

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_group(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_group("test123", ["tenant_1", "project_9"])

        assert result is True
        assert mock_terminal.group == '["tenant_1", "project_9"]'
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_group_empty_list_clears(self, mock_session_class):
        """An empty list clears the group column (opts back out of discovery)."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.group = '["stale"]'
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_group("test123", [])

        assert result is True
        assert mock_terminal.group is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_group_not_found(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_group("nonexistent", ["a"])

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_metadata(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_metadata("test123", {"task": "writing tests"})

        assert result is True
        assert mock_terminal.metadata_json == '{"task": "writing tests"}'
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_terminal_metadata_not_found(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_terminal_metadata("nonexistent", {"task": "x"})

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_group_returns_decoded_list(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.group = '["tenant_1", "project_5"]'
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        assert get_terminal_group("test123") == ["tenant_1", "project_5"]

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_group_none_when_unset(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_terminal = MagicMock()
        mock_terminal.group = None
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_terminal
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        assert get_terminal_group("test123") is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_terminal_group_none_when_terminal_missing(self, mock_session_class):
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        assert get_terminal_group("nonexistent") is None


class TestListSiblingsByGroupPrefix:
    """Real-DB regression tests for the #432 sibling-discovery prefix match.

    Uses the in-memory-sqlite ``test_db`` fixture (not a mocked session) so
    the actual JSON decode + prefix comparison across multiple rows is
    exercised, not just the query-building calls.
    """

    def _seed(self, test_db, terminals):
        with test_db() as seed:
            seed.add_all(terminals)
            seed.commit()

    def test_matching_siblings_returned_with_group_and_metadata(self, test_db):
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="sib-1",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5", "folder_1"]',
                    metadata_json='{"task": "reviewing"}',
                ),
                TerminalModel(
                    id="sib-2",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5", "folder_2"]',
                ),
                TerminalModel(
                    id="other-tenant",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_2", "project_5", "folder_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        ids = {r["id"] for r in result}
        assert ids == {"sib-1", "sib-2"}
        by_id = {r["id"]: r for r in result}
        assert by_id["sib-1"]["group"] == ["tenant_1", "project_5", "folder_1"]
        assert by_id["sib-1"]["metadata"] == {"task": "reviewing"}
        assert by_id["sib-2"]["metadata"] is None

    def test_caller_excluded_from_its_own_results(self, test_db):
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="caller-1",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5"]',
                ),
                TerminalModel(
                    id="sib-1",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert {r["id"] for r in result} == {"sib-1"}

    def test_shorter_sibling_group_excluded_not_partially_matched(self, test_db):
        """A sibling whose group is shorter than the requested depth is
        excluded rather than compared partially or raising an IndexError
        (#432's own documented edge case)."""
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="shallow",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert result == []

    def test_longer_sibling_group_matches_on_shared_prefix(self, test_db):
        """A sibling with a LONGER group than the requested depth still
        matches as long as its leading elements agree."""
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="deep",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5", "folder_9", "subtask_2"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert [r["id"] for r in result] == ["deep"]

    def test_no_group_terminal_excluded_from_being_found(self, test_db):
        """A terminal with no group set is never returned as a sibling to
        anyone, regardless of what prefix is requested."""
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="no-group",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group=None,
                ),
                TerminalModel(
                    id="has-group",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1"])

        assert [r["id"] for r in result] == ["has-group"]

    def test_no_matching_prefix_returns_empty(self, test_db):
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="unrelated",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_9", "project_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert result == []

    def test_sql_prefilter_narrows_rows_before_python_decode(self, test_db):
        """Copilot review, PR #433: prove the SQL query itself narrows the
        candidate set, not just that the final (correct) results happen to
        match -- i.e. this is no longer a full-table scan + Python filter.

        ``json.loads`` is only ever called (inside the function) on
        ``row.group`` for rows the DB query actually returned, so its call
        count is a direct proxy for how many rows were pulled into Python
        for decoding. 5 non-matching siblings (different top-level tenant)
        plus 1 matching one are seeded -- a full scan would decode all 6; a
        working SQL prefilter decodes only the 1 that can possibly match.
        """
        import json as real_json

        non_matching = [
            TerminalModel(
                id=f"other-tenant-{i}",
                tmux_session="s",
                tmux_window="w",
                provider="kiro_cli",
                group=f'["tenant_{i}", "project_5"]',
            )
            for i in range(2, 7)
        ]
        self._seed(
            test_db,
            non_matching
            + [
                TerminalModel(
                    id="sib-1",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5", "folder_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            with patch("json.loads", wraps=real_json.loads) as mock_loads:
                result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert [r["id"] for r in result] == ["sib-1"]
        # Only the 1 matching row should ever reach json.loads -- proof the
        # other 5 were excluded at the SQL level, never loaded for decoding.
        assert mock_loads.call_count == 1

    def test_element_text_prefix_does_not_false_positive_match(self, test_db):
        """A sibling group element that merely shares a *text* prefix with a
        requested prefix element (e.g. "project_50" vs. requested
        "project_5") must not match -- the SQL LIKE prefilter operates on
        the JSON encoding, where each string element is quote-delimited, so
        this can't false-positive."""
        self._seed(
            test_db,
            [
                TerminalModel(
                    id="near-miss",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_50"]',
                ),
                TerminalModel(
                    id="exact",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group='["tenant_1", "project_5", "folder_1"]',
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["tenant_1", "project_5"])

        assert [r["id"] for r in result] == ["exact"]

    def test_group_element_with_like_special_characters_matches_literally(self, test_db):
        """Group elements containing SQL LIKE wildcards (``%``, ``_``) must
        be matched as literal text, not interpreted as wildcards, in the SQL
        prefilter (autoescape)."""
        import json as real_json

        self._seed(
            test_db,
            [
                TerminalModel(
                    id="literal-match",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group=real_json.dumps(["50%_off", "folder_1"]),
                ),
                TerminalModel(
                    id="unrelated",
                    tmux_session="s",
                    tmux_window="w",
                    provider="kiro_cli",
                    group=real_json.dumps(["completely_different", "folder_1"]),
                ),
            ],
        )

        with patch("cli_agent_orchestrator.clients.database.SessionLocal", test_db):
            result = list_siblings_by_group_prefix("caller-1", ["50%_off"])

        assert [r["id"] for r in result] == ["literal-match"]


class TestInboxOperations:
    """Tests for inbox database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_message_status(self, mock_session_class):
        """Test updating message status."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_message = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_message
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_message_status(1, MessageStatus.DELIVERED)

        mock_session.commit.assert_called_once()


class TestFlowOperations:
    """Tests for flow database operations."""

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flow_not_found(self, mock_session_class):
        """Test getting a flow that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flow("nonexistent")

        assert result is None

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled(self, mock_session_class):
        """Test updating flow enabled status."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        update_flow_enabled("test-flow", False)

        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_run_times(self, mock_session_class):
        """Test updating flow run times."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_run_times("test-flow", datetime.now(), datetime.now())

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_run_times_not_found(self, mock_session_class):
        """Test updating flow run times when flow doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_run_times("nonexistent", datetime.now(), datetime.now())

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled_not_found(self, mock_session_class):
        """Test updating flow enabled when flow doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_flow_enabled("nonexistent", False)

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_flow_enabled_with_next_run(self, mock_session_class):
        """Test updating flow enabled with next_run."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        next_run = datetime.now()
        result = update_flow_enabled("test-flow", True, next_run=next_run)

        assert result is True
        assert mock_flow.next_run == next_run

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_flow(self, mock_session_class):
        """Test creating a flow."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Setup mock to update flow attributes on refresh
        def mock_refresh(flow):
            flow.name = "test-flow"
            flow.file_path = "/path/to/file.yaml"
            flow.schedule = "0 * * * *"
            flow.agent_profile = "developer"
            flow.provider = "kiro_cli"
            flow.script = "echo test"
            flow.next_run = datetime.now()
            flow.last_run = None
            flow.enabled = True

        mock_session.refresh.side_effect = mock_refresh

        from cli_agent_orchestrator.clients.database import get_flows_to_run

        next_run = datetime.now()
        result = create_flow(
            name="test-flow",
            file_path="/path/to/file.yaml",
            schedule="0 * * * *",
            agent_profile="developer",
            provider="kiro_cli",
            script="echo test",
            next_run=next_run,
        )

        assert result.name == "test-flow"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flow_found(self, mock_session_class):
        """Test getting a flow that exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = mock_flow
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flow("test-flow")

        assert result is not None
        assert result.name == "test-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_list_flows(self, mock_session_class):
        """Test listing all flows."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "test-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.order_by.return_value.all.return_value = [mock_flow]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = list_flows()

        assert len(result) == 1
        assert result[0].name == "test-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_flow(self, mock_session_class):
        """Test deleting a flow."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 1
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_flow("test-flow")

        assert result is True
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_delete_flow_not_found(self, mock_session_class):
        """Test deleting a flow that doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.delete.return_value = 0
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = delete_flow("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_flows_to_run(self, mock_session_class):
        """Test getting flows that are due to run."""
        from cli_agent_orchestrator.clients.database import get_flows_to_run

        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_flow = MagicMock()
        mock_flow.name = "due-flow"
        mock_flow.file_path = "/path/to/file.yaml"
        mock_flow.schedule = "0 * * * *"
        mock_flow.agent_profile = "developer"
        mock_flow.provider = "kiro_cli"
        mock_flow.script = "echo test"
        mock_flow.last_run = None
        mock_flow.next_run = datetime.now()
        mock_flow.enabled = True

        mock_query = MagicMock()
        mock_query.filter.return_value.all.return_value = [mock_flow]
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = get_flows_to_run()

        assert len(result) == 1
        assert result[0].name == "due-flow"

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_last_active_not_found(self, mock_session_class):
        """Test updating last active when terminal doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_last_active("nonexistent")

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_update_message_status_not_found(self, mock_session_class):
        """Test updating message status when message doesn't exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)

        mock_query = MagicMock()
        mock_query.filter.return_value.first.return_value = None
        mock_session.query.return_value = mock_query
        mock_session_class.return_value = mock_session

        result = update_message_status(999, MessageStatus.DELIVERED)

        assert result is False

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_inbox_message(self, mock_session_class):
        """Test creating an inbox message when receiver terminal exists."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Receiver terminal exists
        mock_session.query.return_value.filter.return_value.first.return_value = MagicMock()

        # Setup mock to update message attributes on refresh
        def mock_refresh(msg):
            msg.id = 1
            msg.sender_id = "sender-123"
            msg.receiver_id = "receiver-456"
            msg.message = "Hello"
            msg.status = MessageStatus.PENDING.value
            msg.created_at = datetime.now()

        mock_session.refresh.side_effect = mock_refresh

        result = create_inbox_message("sender-123", "receiver-456", "Hello")

        assert result.sender_id == "sender-123"
        assert result.receiver_id == "receiver-456"
        assert result.message == "Hello"
        mock_session.add.assert_called_once()
        mock_session.commit.assert_called_once()

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_create_inbox_message_receiver_not_found(self, mock_session_class):
        """create_inbox_message raises ValueError when receiver terminal does not exist."""
        mock_session = MagicMock()
        mock_session.__enter__ = MagicMock(return_value=mock_session)
        mock_session.__exit__ = MagicMock(return_value=False)
        mock_session_class.return_value = mock_session

        # Receiver terminal does not exist
        mock_session.query.return_value.filter.return_value.first.return_value = None

        with pytest.raises(ValueError, match="not found"):
            create_inbox_message("sender-123", "dead-terminal", "Hello")


class TestInitDb:
    """Tests for init_db function."""

    @patch("cli_agent_orchestrator.clients.database.Base")
    @patch("cli_agent_orchestrator.clients.database._migrate_project_aliases_schema")
    def test_init_db(self, mock_alias_migrate, mock_base):
        """Test database initialization."""
        init_db()

        mock_base.metadata.create_all.assert_called_once()


class TestTerminalsSchemaMigration:
    """Tests for the terminals-table column-add migration (caller_id, issue #284)."""

    def test_caller_id_column_added_to_legacy_table(self, tmp_path, monkeypatch):
        """A pre-#284 terminals table gains the caller_id column."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, "
                "agent_profile TEXT, allowed_tools TEXT, shell_command TEXT, "
                "last_active TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
                "VALUES ('abc12345', 'cao-s', 'w-0', 'kiro_cli')"
            )
            conn.commit()

        # _migrate_terminals_schema reads DATABASE_FILE from constants at call time
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
            rows = conn.execute("SELECT id, caller_id FROM terminals").fetchall()
        assert "caller_id" in columns
        assert rows == [("abc12345", None)], "existing rows must get NULL caller_id"

    def test_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Running the migration twice must not fail or duplicate columns."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "current.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL)"
            )
            conn.commit()

        # _migrate_terminals_schema reads DATABASE_FILE from constants at call time
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()
        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(terminals)")]
        assert columns.count("caller_id") == 1
        assert columns.count("allowed_tools") == 1

    def test_group_and_metadata_columns_added_to_legacy_table(self, tmp_path, monkeypatch):
        """#432: a pre-existing terminals table (predating group/metadata) gains both
        columns, and existing rows get NULL rather than erroring."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "pre432.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL, "
                "agent_profile TEXT, allowed_tools TEXT, shell_command TEXT, "
                "caller_id TEXT, last_active TIMESTAMP)"
            )
            conn.execute(
                "INSERT INTO terminals (id, tmux_session, tmux_window, provider) "
                "VALUES ('abc12345', 'cao-s', 'w-0', 'kiro_cli')"
            )
            conn.commit()

        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(terminals)")}
            rows = conn.execute("SELECT id, \"group\", \"metadata\" FROM terminals").fetchall()
        assert {"group", "metadata"} <= columns
        assert rows == [("abc12345", None, None)]

    def test_group_and_metadata_migration_is_idempotent(self, tmp_path, monkeypatch):
        """Running the migration twice must not fail or duplicate the new columns."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "pre432_twice.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE terminals ("
                "id TEXT PRIMARY KEY, tmux_session TEXT NOT NULL, "
                "tmux_window TEXT NOT NULL, provider TEXT NOT NULL)"
            )
            conn.commit()

        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_terminals_schema()
        db_mod._migrate_terminals_schema()

        with sqlite3.connect(str(db_file)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(terminals)")]
        assert columns.count("group") == 1
        assert columns.count("metadata") == 1


class TestCallerIdRoundTrip:
    """caller_id must round-trip create→read (issue #284): a write path that
    persists it and a read path that drops it would silently break callback
    routing for every worker."""

    def test_caller_id_round_trips_through_real_db(self, tmp_path, monkeypatch):
        """create_terminal persists caller_id; get_terminal_metadata returns it."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod

        engine = create_engine(f"sqlite:///{tmp_path / 'rt.db'}")
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

        created = create_terminal(
            "abc12345", "cao-s", "w-0", "kiro_cli", "developer", caller_id="def67890"
        )
        assert created["caller_id"] == "def67890"

        fetched = get_terminal_metadata("abc12345")
        assert fetched is not None
        assert fetched["caller_id"] == "def67890"

    def test_caller_id_defaults_to_none(self, tmp_path, monkeypatch):
        """Operator-launched terminals (no caller) round-trip NULL."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database as db_mod

        engine = create_engine(f"sqlite:///{tmp_path / 'rt2.db'}")
        Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(db_mod, "SessionLocal", sessionmaker(bind=engine))

        created = create_terminal("abc12345", "cao-s", "w-0", "kiro_cli")
        assert created["caller_id"] is None

        fetched = get_terminal_metadata("abc12345")
        assert fetched is not None
        assert fetched["caller_id"] is None


class TestProjectAliasMigration:
    """Tests for the project_aliases alias-only primary-key migration."""

    def test_legacy_composite_pk_table_is_rebuilt(self, tmp_path, monkeypatch):
        """A legacy table with composite PK (project_id, alias) is dropped."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "legacy.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE project_aliases ("
                "project_id TEXT NOT NULL, alias TEXT NOT NULL, kind TEXT NOT NULL, "
                "created_at TEXT, PRIMARY KEY (project_id, alias))"
            )
            conn.execute("INSERT INTO project_aliases VALUES ('p1', 'a1', 'cwd_hash', NULL)")
            conn.commit()

        monkeypatch.setattr(db_mod, "DATABASE_FILE", db_file, raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_project_aliases_schema()

        with sqlite3.connect(str(db_file)) as conn:
            exists = conn.execute(
                "SELECT name FROM sqlite_master " "WHERE type='table' AND name='project_aliases'"
            ).fetchone()
        assert exists is None, "legacy table should be dropped for create_all to rebuild"

    def test_alias_only_pk_table_is_left_intact(self, tmp_path, monkeypatch):
        """A table already keyed on alias alone is not touched."""
        import sqlite3

        from cli_agent_orchestrator.clients import database as db_mod

        db_file = tmp_path / "current.db"
        with sqlite3.connect(str(db_file)) as conn:
            conn.execute(
                "CREATE TABLE project_aliases ("
                "alias TEXT PRIMARY KEY, project_id TEXT NOT NULL, kind TEXT NOT NULL, "
                "created_at TEXT)"
            )
            conn.execute("INSERT INTO project_aliases VALUES ('a1', 'p1', 'cwd_hash', NULL)")
            conn.commit()

        monkeypatch.setattr(db_mod, "DATABASE_FILE", db_file, raising=False)
        monkeypatch.setattr(
            "cli_agent_orchestrator.constants.DATABASE_FILE", db_file, raising=False
        )

        db_mod._migrate_project_aliases_schema()

        with sqlite3.connect(str(db_file)) as conn:
            rows = conn.execute("SELECT alias, project_id FROM project_aliases").fetchall()
        assert rows == [("a1", "p1")], "current-schema table must be left intact"
