"""Unit tests for BeadsClient bd CLI wrapper."""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.beads_real import BeadsClient


@pytest.fixture
def client():
    """Create BeadsClient instance."""
    return BeadsClient()


class TestRunBd:
    """Tests for _run_bd() method."""

    def test_executes_command(self, client):
        """_run_bd executes subprocess with bd command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="output", stderr="")
            client._run_bd("list")
            mock_run.assert_called_once()
            args = mock_run.call_args[0][0]
            assert args[0] == "bd"
            assert "list" in args

    def test_returns_stdout(self, client):
        """_run_bd returns stdout string."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="  output  \n", stderr="")
            result = client._run_bd("list")
            assert result == "output"

    def test_raises_on_error(self, client):
        """_run_bd raises RuntimeError on command failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="command failed")
            with pytest.raises(RuntimeError, match="bd error"):
                client._run_bd("invalid")


class TestParseCreateOutput:
    """Tests for _parse_create_output() method."""

    def test_extracts_id(self, client):
        """_parse_create_output extracts ID from create response."""
        output = "✓ Created issue: abducabd-xyz\n  Title: test"
        result = client._parse_create_output(output)
        assert result == "abducabd-xyz"

    def test_handles_missing_id(self, client):
        """_parse_create_output returns empty string for invalid output."""
        result = client._parse_create_output("invalid output")
        assert result == ""


class TestParseJson:
    """Tests for _parse_json() method."""

    def test_parses_array(self, client):
        """_parse_json parses JSON array."""
        output = '[{"id": "abc", "title": "test"}]'
        result = client._parse_json(output)
        assert isinstance(result, list)
        assert result[0]["id"] == "abc"

    def test_handles_empty(self, client):
        """_parse_json returns empty list for empty input."""
        result = client._parse_json("")
        assert result == []

    def test_handles_invalid(self, client):
        """_parse_json returns empty list for invalid JSON."""
        result = client._parse_json("not valid json")
        assert result == []


class TestCaoToBeadsPriority:
    """Tests for _cao_to_beads_priority() method."""

    def test_cao_priority_1_to_beads_p1(self, client):
        """CAO priority 1 (high) maps to Beads P1."""
        assert client._cao_to_beads_priority(1) == 1

    def test_cao_priority_2_to_beads_p2(self, client):
        """CAO priority 2 (medium) maps to Beads P2."""
        assert client._cao_to_beads_priority(2) == 2

    def test_cao_priority_3_to_beads_p3(self, client):
        """CAO priority 3 (low) maps to Beads P3."""
        assert client._cao_to_beads_priority(3) == 3

    def test_invalid_priority_defaults_to_p2(self, client):
        """Invalid CAO priority defaults to Beads P2."""
        assert client._cao_to_beads_priority(99) == 2
        assert client._cao_to_beads_priority(0) == 2


class TestBeadsToCaoPriority:
    """Tests for _beads_to_cao_priority() method."""

    def test_beads_p0_to_cao_1(self, client):
        """Beads P0 maps to CAO priority 1 (high)."""
        assert client._beads_to_cao_priority(0) == 1

    def test_beads_p1_to_cao_1(self, client):
        """Beads P1 maps to CAO priority 1 (high)."""
        assert client._beads_to_cao_priority(1) == 1

    def test_beads_p2_to_cao_2(self, client):
        """Beads P2 maps to CAO priority 2 (medium)."""
        assert client._beads_to_cao_priority(2) == 2

    def test_beads_p3_to_cao_3(self, client):
        """Beads P3 maps to CAO priority 3 (low)."""
        assert client._beads_to_cao_priority(3) == 3

    def test_beads_p4_to_cao_3(self, client):
        """Beads P4 maps to CAO priority 3 (low)."""
        assert client._beads_to_cao_priority(4) == 3

    def test_invalid_priority_defaults_to_2(self, client):
        """Invalid Beads priority defaults to CAO 2."""
        assert client._beads_to_cao_priority(99) == 2


class TestCaoToBeadsStatus:
    """Tests for _cao_to_beads_status() method."""

    def test_cao_open_to_beads_open(self, client):
        """CAO 'open' maps to Beads 'open'."""
        assert client._cao_to_beads_status("open") == "open"

    def test_cao_wip_to_beads_in_progress(self, client):
        """CAO 'wip' maps to Beads 'in_progress'."""
        assert client._cao_to_beads_status("wip") == "in_progress"

    def test_cao_closed_to_beads_closed(self, client):
        """CAO 'closed' maps to Beads 'closed'."""
        assert client._cao_to_beads_status("closed") == "closed"

    def test_invalid_status_defaults_to_open(self, client):
        """Invalid CAO status defaults to Beads 'open'."""
        assert client._cao_to_beads_status("invalid") == "open"


class TestBeadsToCaoStatus:
    """Tests for _beads_to_cao_status() method."""

    def test_beads_open_to_cao_open(self, client):
        """Beads 'open' maps to CAO 'open'."""
        assert client._beads_to_cao_status("open") == "open"

    def test_beads_in_progress_to_cao_wip(self, client):
        """Beads 'in_progress' maps to CAO 'wip'."""
        assert client._beads_to_cao_status("in_progress") == "wip"

    def test_beads_closed_to_cao_closed(self, client):
        """Beads 'closed' maps to CAO 'closed'."""
        assert client._beads_to_cao_status("closed") == "closed"

    def test_invalid_status_defaults_to_open(self, client):
        """Invalid Beads status defaults to CAO 'open'."""
        assert client._beads_to_cao_status("invalid") == "open"


class TestList:
    """Tests for list() method."""

    def test_list_returns_tasks(self, client):
        """list() returns Task objects from bd list --json."""
        issues = [{"id": "abc", "title": "Test", "priority": 2, "status": "open"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            tasks = client.list()
            assert len(tasks) == 1
            assert tasks[0].id == "abc"
            assert tasks[0].title == "Test"

    def test_list_filters_by_status(self, client):
        """list() passes status filter to bd command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            client.list(status="wip")
            args = mock_run.call_args[0][0]
            assert "--status" in args
            assert "in_progress" in args

    def test_list_filters_by_priority(self, client):
        """list() filters tasks by priority."""
        issues = [
            {"id": "a", "title": "High", "priority": 1, "status": "open"},
            {"id": "b", "title": "Low", "priority": 3, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            tasks = client.list(priority=1)
            assert len(tasks) == 1
            assert tasks[0].id == "a"


class TestGet:
    """Tests for get() method."""

    def test_get_returns_task(self, client):
        """get() returns Task from bd show --json (list output)."""
        issue = [{"id": "abc", "title": "Test", "priority": 2, "status": "open"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("abc")
            assert task is not None
            assert task.id == "abc"

    def test_get_returns_none_for_missing(self, client):
        """get() returns None for non-existent task."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="not found")
            task = client.get("nonexistent")
            assert task is None


class TestAdd:
    """Tests for add() method."""

    def test_add_creates_task(self, client):
        """add() creates task and returns Task object."""
        with patch("subprocess.run") as mock_run:
            # First call: create, second call: show
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="✓ Created issue: xyz", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout=json.dumps(
                        [{"id": "xyz", "title": "New", "priority": 2, "status": "open"}]
                    ),
                    stderr="",
                ),
            ]
            task = client.add("New")
            assert task.id == "xyz"

    def test_add_with_description(self, client):
        """add() passes description with -d flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="✓ Created issue: xyz", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout=json.dumps(
                        [{"id": "xyz", "title": "New", "priority": 2, "status": "open"}]
                    ),
                    stderr="",
                ),
            ]
            client.add("New", description="Details")
            args = mock_run.call_args_list[0][0][0]
            assert "-d" in args
            assert "Details" in args


class TestUpdate:
    """Tests for update() method."""

    def test_update_modifies_fields(self, client):
        """update() uses --status flag for status changes."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [{"id": "abc", "title": "Test", "priority": 2, "status": "in_progress"}]
                ),
                stderr="",
            )
            client.update("abc", status="wip")
            # Find the update call (not the show call)
            update_call = [c for c in mock_run.call_args_list if "update" in c[0][0]][0]
            args = update_call[0][0]
            assert "--status" in args
            assert "in_progress" in args
            assert "--state" not in args

    def test_update_with_priority(self, client):
        """update() passes priority with -p flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [{"id": "abc", "title": "Test", "priority": 1, "status": "open"}]
                ),
                stderr="",
            )
            client.update("abc", priority=1)
            update_call = [c for c in mock_run.call_args_list if "update" in c[0][0]][0]
            args = update_call[0][0]
            assert "-p" in args


class TestDelete:
    """Tests for delete() method."""

    def test_delete_removes_task(self, client):
        """delete() uses --force flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            client.delete("abc")
            args = mock_run.call_args[0][0]
            assert "--force" in args
            assert "-y" not in args


class TestClose:
    """Tests for close() method."""

    def test_close_changes_status(self, client):
        """close() calls bd close command."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [{"id": "abc", "title": "Test", "priority": 2, "status": "closed"}]
                ),
                stderr="",
            )
            task = client.close("abc")
            # Find the close call
            close_call = [c for c in mock_run.call_args_list if "close" in c[0][0]][0]
            args = close_call[0][0]
            assert "close" in args
            assert "abc" in args


class TestNext:
    """Tests for next() method."""

    def test_next_returns_first_ready_task(self, client):
        """next() returns first task from bd ready --json."""
        issues = [
            {"id": "a", "title": "First", "priority": 2, "status": "open"},
            {"id": "b", "title": "Second", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            task = client.next()
            assert task is not None
            assert task.id == "a"

    def test_next_returns_none_when_empty(self, client):
        """next() returns None when no ready tasks."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            task = client.next()
            assert task is None

    def test_next_filters_by_priority(self, client):
        """next() filters tasks by priority."""
        issues = [
            {"id": "a", "title": "High", "priority": 1, "status": "open"},
            {"id": "b", "title": "Medium", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            task = client.next(priority=2)
            assert task is not None
            assert task.id == "b"


class TestWip:
    """Tests for wip() method."""

    def test_wip_sets_status_in_progress(self, client):
        """wip() uses --status in_progress flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [{"id": "abc", "title": "Test", "priority": 2, "status": "in_progress"}]
                ),
                stderr="",
            )
            client.wip("abc")
            update_call = [c for c in mock_run.call_args_list if "update" in c[0][0]][0]
            args = update_call[0][0]
            assert "--status" in args
            assert "in_progress" in args

    def test_wip_sets_assignee(self, client):
        """wip() sets assignee when provided."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps(
                    [
                        {
                            "id": "abc",
                            "title": "Test",
                            "priority": 2,
                            "status": "in_progress",
                            "assignee": "session-123",
                        }
                    ]
                ),
                stderr="",
            )
            client.wip("abc", assignee="session-123")
            assignee_call = [c for c in mock_run.call_args_list if "--assignee" in c[0][0]]
            assert len(assignee_call) == 1
            args = assignee_call[0][0][0]
            assert "session-123" in args


class TestClearAssigneeBySession:
    """Tests for clear_assignee_by_session() method."""

    def test_clears_matching_tasks_uses_status_flag(self, client):
        """clear_assignee_by_session() uses --status flag (not --state)."""
        tasks = [
            {
                "id": "a",
                "title": "Task A",
                "priority": 2,
                "status": "in_progress",
                "assignee": "session-123",
            },
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            client.clear_assignee_by_session("session-123")
            update_calls = [c for c in mock_run.call_args_list if "update" in c[0][0]]
            assert len(update_calls) >= 1
            args = update_calls[0][0][0]
            assert "--status" in args
            assert "--state" not in args

    def test_returns_count_of_cleared(self, client):
        """clear_assignee_by_session() returns count of cleared tasks."""
        tasks = [
            {
                "id": "a",
                "title": "Task A",
                "priority": 2,
                "status": "in_progress",
                "assignee": "session-123",
            },
            {
                "id": "b",
                "title": "Task B",
                "priority": 2,
                "status": "in_progress",
                "assignee": "session-123",
            },
            {
                "id": "c",
                "title": "Task C",
                "priority": 2,
                "status": "in_progress",
                "assignee": "other",
            },
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            count = client.clear_assignee_by_session("session-123")
            assert count == 2

    def test_clears_assignee_field(self, client):
        """clear_assignee_by_session() clears assignee with empty string."""
        tasks = [
            {
                "id": "a",
                "title": "Task A",
                "priority": 2,
                "status": "in_progress",
                "assignee": "session-123",
            },
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            client.clear_assignee_by_session("session-123")
            update_calls = [c for c in mock_run.call_args_list if "update" in c[0][0]]
            assert len(update_calls) >= 1
            args = update_calls[0][0][0]
            assert "--assignee" in args
            assignee_idx = args.index("--assignee")
            assert args[assignee_idx + 1] == ""
