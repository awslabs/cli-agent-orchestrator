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


# ---------------------------------------------------------------------------
# list (Bug 1: a script-tier row's step_count=None must not crash the table)
# ---------------------------------------------------------------------------
def test_list_renders_none_step_count_as_dash(runner):
    """A script spec indexes with step_count=None (run-time-determined). The table
    must render that as '-', never crash formatting None with the :<6 field."""
    rows = [
        {"name": "yamlwf", "mode": "sequential", "step_count": 3, "description": "a yaml one"},
        {"name": "scriptwf", "mode": "script", "step_count": None, "description": "a script one"},
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    # The YAML row shows its numeric count; the script row shows the placeholder.
    # Assert on the rendered data lines specifically — the header underline is a
    # run of dashes, so a bare "'-' in output" would pass even without the fix.
    lines = result.output.splitlines()
    yaml_line = next(line for line in lines if line.startswith("yamlwf"))
    script_line = next(line for line in lines if line.startswith("scriptwf"))
    assert "3" in yaml_line.split()  # YAML row's numeric step count
    assert "-" in script_line.split()  # script row renders None as the placeholder
    assert "None" not in script_line  # never the literal None


def test_list_all_rows_script_none_step_count(runner):
    """Edge case: a listing of ONLY script specs (every step_count None) still
    renders every row without a TypeError."""
    rows = [
        {"name": "s1", "mode": "script", "step_count": None, "description": ""},
        {"name": "s2", "mode": "script", "step_count": None, "description": "second"},
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    assert "s1" in result.output and "s2" in result.output


def test_list_empty(runner):
    """Edge case: an empty index prints a friendly message, not an empty table."""
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, [])
        result = runner.invoke(workflow, ["list"])
    assert result.exit_code == 0
    assert "No workflows found" in result.output


def test_list_json_passthrough_preserves_none(runner):
    """The --json path emits rows verbatim (step_count stays null), never coerced."""
    rows = [{"name": "scriptwf", "mode": "script", "step_count": None, "description": ""}]
    with patch("cli_agent_orchestrator.cli.commands.workflow.requests") as mock_req:
        mock_req.get.return_value = _resp(200, rows)
        result = runner.invoke(workflow, ["list", "--json"])
    assert result.exit_code == 0
    assert '"step_count": null' in result.output
