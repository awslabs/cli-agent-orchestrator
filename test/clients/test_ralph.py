"""Unit tests for RalphRunner."""
import tempfile
from pathlib import Path
import pytest
from cli_agent_orchestrator.clients.ralph import RalphRunner, RalphState


@pytest.fixture
def ralph():
    """Create RalphRunner with temp state file."""
    with tempfile.TemporaryDirectory() as tmpdir:
        state_path = Path(tmpdir) / "ralph.json"
        yield RalphRunner(state_path)


def test_start_loop(ralph):
    state = ralph.start("Test prompt", min_iter=3, max_iter=10, promise="DONE")
    assert state.prompt == "Test prompt"
    assert state.minIterations == 3
    assert state.maxIterations == 10
    assert state.completionPromise == "DONE"
    assert state.iteration == 1
    assert state.status == "running"
    assert state.active is True


def test_status_no_loop(ralph):
    assert ralph.status() is None


def test_status_active_loop(ralph):
    ralph.start("Test")
    state = ralph.status()
    assert state is not None
    assert state.prompt == "Test"


def test_stop_loop(ralph):
    ralph.start("Test")
    assert ralph.stop() is True
    state = ralph.status()
    assert state.status == "stopped"
    assert state.active is False


def test_stop_no_loop(ralph):
    assert ralph.stop() is False


def test_feedback_increments_iteration(ralph):
    ralph.start("Test", max_iter=10)
    state = ralph.feedback(score=7, summary="Good progress")
    assert state.iteration == 2
    assert state.previousFeedback["qualityScore"] == 7
    assert state.previousFeedback["qualitySummary"] == "Good progress"


def test_feedback_max_iterations(ralph):
    ralph.start("Test", max_iter=2)
    ralph.feedback(score=5, summary="Iter 1")
    state = ralph.feedback(score=6, summary="Iter 2")
    assert state.status == "max_iterations"
    assert state.active is False


def test_complete_loop(ralph):
    ralph.start("Test")
    state = ralph.complete()
    assert state.status == "completed"
    assert state.active is False


def test_start_with_task_id(ralph):
    state = ralph.start("Test", task_id="task-123")
    assert state.taskId == "task-123"


def test_start_with_work_dir(ralph):
    state = ralph.start("Test", work_dir="/tmp/work")
    assert state.workDir == "/tmp/work"
