"""Unit tests for BeadsClient bd CLI wrapper."""

import json
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.clients.beads_real import (
    BeadsClient, Task, extract_label_value, extract_context_files,
    resolve_workspace, resolve_context_files,
)


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


class TestCreateChild:
    """Tests for create_child() method."""

    def test_create_child_uses_parent_flag(self, client):
        """create_child() calls bd create with --parent flag."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="✓ Created issue: parent-1.1\n",
                stderr="",
            )
            client.create_child("parent-1", "Child task")
            # First call should be create with --parent
            create_call = mock_run.call_args_list[0]
            args = create_call[0][0]
            assert "--parent" in args
            assert "parent-1" in args

    def test_create_child_returns_task(self, client):
        """create_child() returns Task with parent_id set."""
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout="✓ Created issue: parent-1.1\n", stderr=""),
                MagicMock(
                    returncode=0,
                    stdout=json.dumps([{"id": "parent-1.1", "title": "Child", "priority": 2, "status": "open", "parent_id": "parent-1"}]),
                    stderr="",
                ),
            ]
            task = client.create_child("parent-1", "Child task")
            assert task.id == "parent-1.1"
            assert task.parent_id == "parent-1"


class TestGetChildren:
    """Tests for get_children() method."""

    def test_get_children_filters_by_parent(self, client):
        """get_children() returns only tasks with matching parent_id."""
        tasks = [
            {"id": "parent-1.1", "title": "Child 1", "priority": 2, "status": "open", "parent_id": "parent-1"},
            {"id": "parent-1.2", "title": "Child 2", "priority": 2, "status": "open", "parent_id": "parent-1"},
            {"id": "other-1", "title": "Other", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            children = client.get_children("parent-1")
            assert len(children) == 2
            assert all(c.parent_id == "parent-1" for c in children)


class TestListWithHierarchy:
    """Tests for list() returning parent_id."""

    def test_list_includes_parent_id(self, client):
        """list() returns tasks with parent_id populated."""
        tasks = [
            {"id": "parent-1", "title": "Parent", "priority": 2, "status": "open"},
            {"id": "parent-1.1", "title": "Child", "priority": 2, "status": "open", "parent_id": "parent-1"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            result = client.list()
            child = next(t for t in result if t.id == "parent-1.1")
            assert child.parent_id == "parent-1"


class TestTaskLabelsAndType:
    """Tests for labels and type fields on Task dataclass."""

    def test_issue_to_task_includes_labels(self, client):
        """_issue_to_task populates labels from bd JSON."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open",
                  "labels": ["auto_orchestrate", "queue:support"]}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            assert task.labels == ["auto_orchestrate", "queue:support"]

    def test_issue_to_task_includes_type_from_issue_type(self, client):
        """_issue_to_task maps bd's issue_type field to Task.type."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open",
                  "issue_type": "epic"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            assert task.type == "epic"

    def test_issue_to_task_missing_labels_defaults_none(self, client):
        """Missing labels field defaults to None."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            assert task.labels is None

    def test_issue_to_task_missing_type_defaults_none(self, client):
        """Missing issue_type field defaults to None."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            assert task.type is None

    def test_issue_to_task_empty_labels_becomes_none(self, client):
        """Empty labels list becomes None."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open", "labels": []}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            assert task.labels is None

    def test_task_dict_includes_new_fields(self, client):
        """Task.__dict__ includes labels and type for API serialization."""
        issue = [{"id": "x", "title": "T", "priority": 2, "status": "open",
                  "labels": ["type:epic"], "issue_type": "epic"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issue), stderr="")
            task = client.get("x")
            d = task.__dict__
            assert "labels" in d
            assert "type" in d
            assert d["labels"] == ["type:epic"]
            assert d["type"] == "epic"


class TestComments:
    """Tests for get_comments() and add_comment() methods."""

    def test_get_comments_returns_list(self, client):
        """get_comments() returns parsed JSON comment list."""
        comments = [{"id": 1, "body": "Found the root cause", "created_at": "2026-04-02"}]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(comments), stderr="")
            result = client.get_comments("task-1")
            assert len(result) == 1
            assert result[0]["body"] == "Found the root cause"

    def test_get_comments_empty(self, client):
        """get_comments() returns empty list when no comments."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            result = client.get_comments("task-1")
            assert result == []

    def test_add_comment_calls_bd_comments_add(self, client):
        """add_comment() calls bd comments add."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.add_comment("task-1", "Agent findings here")
            args = mock_run.call_args[0][0]
            assert "comments" in args
            assert "add" in args
            assert "task-1" in args
            assert "Agent findings here" in args

    def test_add_comment_returns_false_on_error(self, client):
        """add_comment() returns False on failure."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert not client.add_comment("bad-id", "text")


# ==================== Phase 2: Epic + Dependency + Label Tests ====================


class TestAddDependency:
    """Tests for add_dependency() method."""

    def test_calls_bd_dep_add(self, client):
        """add_dependency() calls bd dep add with correct args."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.add_dependency("task-2", "task-1")
            args = mock_run.call_args[0][0]
            assert "dep" in args
            assert "add" in args
            assert "task-2" in args
            assert "task-1" in args

    def test_returns_false_on_error(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert not client.add_dependency("bad", "bad")


class TestRemoveDependency:
    """Tests for remove_dependency() method."""

    def test_calls_bd_dep_remove(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.remove_dependency("task-2", "task-1")
            args = mock_run.call_args[0][0]
            assert "dep" in args
            assert "remove" in args

    def test_returns_false_on_error(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert not client.remove_dependency("bad", "bad")


class TestUpdateNotes:
    """Tests for update_notes() method."""

    def test_calls_bd_update_with_notes(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout=json.dumps([{"id": "x", "title": "T", "priority": 2, "status": "open"}]),
                stderr=""
            )
            client.update_notes("x", "Schema uses 5 tables")
            update_call = [c for c in mock_run.call_args_list if "--notes" in " ".join(c[0][0])]
            assert len(update_call) == 1
            assert "Schema uses 5 tables" in update_call[0][0][0]


class TestAddRemoveLabel:
    """Tests for add_label() and remove_label() methods."""

    def test_add_label(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.add_label("x", "priority:high")
            args = mock_run.call_args[0][0]
            assert "label" in args and "add" in args and "priority:high" in args

    def test_remove_label(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            assert client.remove_label("x", "priority:high")
            args = mock_run.call_args[0][0]
            assert "label" in args and "remove" in args

    def test_add_label_returns_false_on_error(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            assert not client.add_label("bad", "label")


class TestIsEpic:
    """Tests for is_epic() method."""

    def test_true_for_parent_with_children(self, client):
        tasks = [
            {"id": "p.1", "title": "Child", "priority": 2, "status": "open", "parent_id": "p"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(tasks), stderr="")
            assert client.is_epic("p")

    def test_false_for_leaf_task(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            assert not client.is_epic("leaf")


class TestReady:
    """Tests for ready() method."""

    def test_returns_all_ready_tasks(self, client):
        issues = [
            {"id": "a", "title": "Ready A", "priority": 2, "status": "open"},
            {"id": "b", "title": "Ready B", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            tasks = client.ready()
            assert len(tasks) == 2

    def test_filters_by_parent_id(self, client):
        issues = [
            {"id": "epic.1", "title": "Step 1", "priority": 2, "status": "open", "parent_id": "epic"},
            {"id": "other", "title": "Other", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            tasks = client.ready(parent_id="epic")
            assert len(tasks) == 1
            assert tasks[0].id == "epic.1"

    def test_filters_by_parent_id_prefix(self, client):
        """ready() also matches children by ID prefix (e.g., epic.1 is child of epic)."""
        issues = [
            {"id": "epic.1", "title": "Step 1", "priority": 2, "status": "open"},
        ]
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=json.dumps(issues), stderr="")
            tasks = client.ready(parent_id="epic")
            assert len(tasks) == 1

    def test_returns_empty_when_none_ready(self, client):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="[]", stderr="")
            assert client.ready() == []


class TestCreateEpic:
    """Tests for create_epic() method."""

    def test_creates_parent_and_children(self, client):
        """create_epic creates parent + N children."""
        call_log = []

        def mock_run(cmd, **kw):
            cmd_str = " ".join(cmd)
            call_log.append(cmd_str)
            if "create" in cmd_str and "--parent" not in cmd_str:
                return MagicMock(returncode=0, stdout="Created issue: epic-1", stderr="")
            if "create" in cmd_str and "--parent" in cmd_str:
                idx = len([c for c in call_log if "--parent" in c])
                return MagicMock(returncode=0, stdout=f"Created issue: epic-1.{idx}", stderr="")
            if "show" in cmd_str or "list" in cmd_str:
                return MagicMock(returncode=0, stdout=json.dumps([{
                    "id": "epic-1", "title": "My Epic", "priority": 2, "status": "open"
                }]), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            epic = client.create_epic("My Epic", ["Step A", "Step B", "Step C"])
            assert epic.id == "epic-1"

            # Should have 3 create calls with --parent
            parent_creates = [c for c in call_log if "--parent" in c and "create" in c]
            assert len(parent_creates) == 3

    def test_sequential_adds_dependencies(self, client):
        """create_epic with sequential=True chains deps."""
        dep_calls = []

        def mock_run(cmd, **kw):
            cmd_str = " ".join(cmd)
            if "dep add" in cmd_str:
                dep_calls.append(cmd_str)
            if "create" in cmd_str and "--parent" not in cmd_str:
                return MagicMock(returncode=0, stdout="Created issue: e", stderr="")
            if "create" in cmd_str:
                idx = len([c for c in dep_calls]) + 1  # rough child index
                return MagicMock(returncode=0, stdout=f"Created issue: e.{len(dep_calls) + 1}", stderr="")
            if "show" in cmd_str:
                return MagicMock(returncode=0, stdout=json.dumps([{
                    "id": "e", "title": "E", "priority": 2, "status": "open"
                }]), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            client.create_epic("E", ["A", "B", "C"], sequential=True)
            # Should have 2 dep add calls: B depends on A, C depends on B
            assert len(dep_calls) == 2

    def test_non_sequential_no_deps(self, client):
        """create_epic with sequential=False creates no deps."""
        dep_calls = []

        def mock_run(cmd, **kw):
            if "dep add" in " ".join(cmd):
                dep_calls.append(True)
            if "create" in " ".join(cmd):
                return MagicMock(returncode=0, stdout="Created issue: x", stderr="")
            if "show" in " ".join(cmd):
                return MagicMock(returncode=0, stdout=json.dumps([{
                    "id": "x", "title": "X", "priority": 2, "status": "open"
                }]), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            client.create_epic("X", ["A", "B", "C"], sequential=False)
            assert len(dep_calls) == 0

    def test_adds_type_epic_label(self, client):
        """create_epic adds type:epic label to parent."""
        label_calls = []

        def mock_run(cmd, **kw):
            cmd_str = " ".join(cmd)
            if "label" in cmd_str and "add" in cmd_str:
                label_calls.append(cmd_str)
            if "create" in cmd_str:
                return MagicMock(returncode=0, stdout="Created issue: e", stderr="")
            if "show" in cmd_str:
                return MagicMock(returncode=0, stdout=json.dumps([{
                    "id": "e", "title": "E", "priority": 2, "status": "open"
                }]), stderr="")
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=mock_run):
            client.create_epic("E", ["A"], labels=["custom:tag"])
            type_labels = [c for c in label_calls if "type:epic" in c]
            assert len(type_labels) == 1
            max_labels = [c for c in label_calls if "max_concurrent:" in c]
            assert len(max_labels) == 1
            custom_labels = [c for c in label_calls if "custom:tag" in c]
            assert len(custom_labels) == 1


# ==================== Label Utility Functions ====================


class TestExtractLabelValue:
    """Tests for extract_label_value()."""

    def test_finds_matching_prefix(self):
        assert extract_label_value(["workspace:/foo", "type:epic"], "workspace") == "/foo"

    def test_returns_none_when_not_found(self):
        assert extract_label_value(["type:epic"], "workspace") is None

    def test_handles_none_labels(self):
        assert extract_label_value(None, "workspace") is None

    def test_handles_empty_list(self):
        assert extract_label_value([], "workspace") is None

    def test_returns_first_match(self):
        assert extract_label_value(["workspace:/a", "workspace:/b"], "workspace") == "/a"


class TestExtractContextFiles:
    """Tests for extract_context_files()."""

    def test_extracts_context_labels(self):
        labels = ["context:/tmp/file.md", "type:epic", "context:/tmp/other.md"]
        assert extract_context_files(labels) == ["/tmp/file.md", "/tmp/other.md"]

    def test_empty_labels(self):
        assert extract_context_files([]) == []

    def test_none_labels(self):
        assert extract_context_files(None) == []

    def test_no_context_labels(self):
        assert extract_context_files(["type:epic", "workspace:/foo"]) == []


class TestResolveWorkspace:
    """Tests for resolve_workspace()."""

    def test_from_task_label(self):
        task = Task(id="t", title="T", labels=["workspace:/my/dir"])
        assert resolve_workspace(task, None, "/default") == "/my/dir"

    def test_inherits_from_parent(self):
        child = Task(id="c", title="Child", parent_id="p")
        parent = Task(id="p", title="Parent", labels=["workspace:/parent/dir"])
        mock_client = MagicMock()
        mock_client.get.return_value = parent
        assert resolve_workspace(child, mock_client, "/default") == "/parent/dir"

    def test_falls_back_to_default(self):
        task = Task(id="t", title="T")
        assert resolve_workspace(task, None, "/default") == "/default"

    def test_returns_none_when_no_default(self):
        task = Task(id="t", title="T")
        assert resolve_workspace(task, None) is None


class TestResolveContextFiles:
    """Tests for resolve_context_files()."""

    def test_from_task_labels(self):
        task = Task(id="t", title="T", labels=["context:/a.md", "context:/b.md"])
        assert resolve_context_files(task, None) == ["/a.md", "/b.md"]

    def test_inherits_from_parent(self):
        child = Task(id="c", title="C", parent_id="p", labels=["context:/child.md"])
        parent = Task(id="p", title="P", labels=["context:/parent.md"])
        mock_client = MagicMock()
        mock_client.get.return_value = parent
        result = resolve_context_files(child, mock_client)
        assert result == ["/child.md", "/parent.md"]

    def test_deduplicates(self):
        child = Task(id="c", title="C", parent_id="p", labels=["context:/shared.md"])
        parent = Task(id="p", title="P", labels=["context:/shared.md", "context:/extra.md"])
        mock_client = MagicMock()
        mock_client.get.return_value = parent
        result = resolve_context_files(child, mock_client)
        assert result == ["/shared.md", "/extra.md"]

    def test_no_context(self):
        task = Task(id="t", title="T", labels=["type:epic"])
        assert resolve_context_files(task, None) == []

    def test_none_labels(self):
        task = Task(id="t", title="T")
        assert resolve_context_files(task, None) == []
