"""Unit tests for BeadsClient."""
import tempfile
from pathlib import Path
import pytest
from cli_agent_orchestrator.clients.beads import BeadsClient, Task


@pytest.fixture
def beads():
    """Create BeadsClient with temp database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test.db"
        yield BeadsClient(db_path)


def test_add_task(beads):
    task = beads.add("Test task", "Description", priority=1)
    assert task.title == "Test task"
    assert task.description == "Description"
    assert task.priority == 1
    assert task.status == "open"


def test_list_tasks(beads):
    beads.add("Task 1", priority=1)
    beads.add("Task 2", priority=2)
    tasks = beads.list()
    assert len(tasks) == 2
    assert tasks[0].priority == 1  # Sorted by priority


def test_list_filter_status(beads):
    t1 = beads.add("Open task")
    t2 = beads.add("WIP task")
    beads.wip(t2.id)
    open_tasks = beads.list(status="open")
    assert len(open_tasks) == 1
    assert open_tasks[0].id == t1.id


def test_next_task(beads):
    beads.add("P2 task", priority=2)
    beads.add("P1 task", priority=1)
    task = beads.next()
    assert task.title == "P1 task"


def test_next_with_priority_filter(beads):
    beads.add("P1 task", priority=1)
    beads.add("P2 task", priority=2)
    task = beads.next(priority=2)
    assert task.title == "P2 task"


def test_wip_task(beads):
    task = beads.add("Test")
    updated = beads.wip(task.id, assignee="agent-1")
    assert updated.status == "wip"
    assert updated.assignee == "agent-1"


def test_close_task(beads):
    task = beads.add("Test")
    closed = beads.close(task.id)
    assert closed.status == "closed"
    assert closed.closed_at is not None


def test_delete_task(beads):
    task = beads.add("Test")
    assert beads.delete(task.id) is True
    assert beads.get(task.id) is None


def test_update_task(beads):
    task = beads.add("Original")
    updated = beads.update(task.id, title="Updated", priority=1)
    assert updated.title == "Updated"
    assert updated.priority == 1
