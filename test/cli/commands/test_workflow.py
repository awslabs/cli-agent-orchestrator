"""Tests for the Bolt-3 workflow run CLI verbs (issue #312, N5).

Covers ``cao workflow run`` / ``status`` / ``cancel`` as thin HTTP clients:
happy path, error-detail surfacing, ``--input k=v`` parsing + type coercion, and
the non-zero exit on a non-COMPLETED run. ``requests`` is mocked — no server.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import _coerce, _parse_inputs, workflow
from cli_agent_orchestrator.constants import MCP_REQUEST_TIMEOUT, WORKFLOW_RUN_REQUEST_TIMEOUT


@pytest.fixture
def runner():
    return CliRunner()


def _resp(status_code=200, json_body=None):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body if json_body is not None else {}
    return r


# ---------------------------------------------------------------------------
# --input parsing / coercion
# ---------------------------------------------------------------------------
def test_coerce_types():
    assert _coerce("true") is True
    assert _coerce("False") is False
    assert _coerce("42") == 42
    assert _coerce("hello") == "hello"
    assert _coerce("3.5") == "3.5"  # not an int -> stays string


def test_parse_inputs_ok():
    assert _parse_inputs(["a=1", "b=hi", "c=true"]) == {"a": 1, "b": "hi", "c": True}


def test_parse_inputs_missing_eq(runner):
    import click

    with pytest.raises(click.ClickException):
        _parse_inputs(["noequals"])


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------
def test_run_happy(runner):
    body = {
        "run_id": "run1",
        "state": "completed",
        "steps": [{"id": "s1", "state": "completed", "attempts": 1}],
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["run", "wf", "--input", "topic=cats"])
    assert result.exit_code == 0
    assert "run1" in result.output
    assert "completed" in result.output
    # Verify inputs were parsed into the payload.
    _, kwargs = mock_req.post.call_args
    assert kwargs["json"]["inputs"] == {"topic": "cats"}


def test_run_failed_state_nonzero_exit(runner):
    body = {"run_id": "run1", "state": "failed", "steps": []}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 1


def test_run_unknown_workflow_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(404, {"detail": "unknown workflow 'ghost'"})
        result = runner.invoke(workflow, ["run", "ghost"])
    assert result.exit_code != 0
    assert "unknown workflow" in result.output


def test_run_reserved_mode_501_surfaces_detail(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(501, {"detail": "mode 'parallel' is reserved"})
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code != 0
    assert "reserved" in result.output


def test_run_uses_long_client_timeout_not_flat_30s(runner):
    """B1 regression guard: ``run`` blocks for the whole workflow, so it must use
    the worst-case-covering run timeout, never the flat 30s MCP_REQUEST_TIMEOUT."""
    body = {"run_id": "run1", "state": "completed", "steps": []}
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["run", "wf"])
    assert result.exit_code == 0
    _, kwargs = mock_req.post.call_args
    assert kwargs["timeout"] == WORKFLOW_RUN_REQUEST_TIMEOUT
    assert WORKFLOW_RUN_REQUEST_TIMEOUT > MCP_REQUEST_TIMEOUT


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
def test_status_happy(runner):
    body = {
        "run_id": "run1",
        "state": "running",
        "current_step_id": "s1",
        "steps": [{"id": "s1", "state": "running", "attempts": 1}],
    }
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, body)
        result = runner.invoke(workflow, ["status", "run1"])
    assert result.exit_code == 0
    assert "running" in result.output


def test_status_unknown_404(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(404, {"detail": "unknown run 'ghost'"})
        result = runner.invoke(workflow, ["status", "ghost"])
    assert result.exit_code != 0
    assert "unknown run" in result.output


# ---------------------------------------------------------------------------
# cancel
# ---------------------------------------------------------------------------
def test_cancel_happy(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(200, {"success": True, "run_id": "run1"})
        result = runner.invoke(workflow, ["cancel", "run1"])
    assert result.exit_code == 0
    assert "cancelling" in result.output
    # cancel is a quick write — it correctly keeps the flat MCP_REQUEST_TIMEOUT.
    _, kwargs = mock_req.post.call_args
    assert kwargs["timeout"] == MCP_REQUEST_TIMEOUT


def test_cancel_finished_409(runner):
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.post.return_value = _resp(409, {"detail": "run 'run1' is already completed"})
        result = runner.invoke(workflow, ["cancel", "run1"])
    assert result.exit_code != 0
    assert "already completed" in result.output
