"""Regression tests for required workflow MCP tool parameters (issue #697)."""

import inspect

import pytest

from cli_agent_orchestrator.mcp_server import server

_REQUIRED_WORKFLOW_PARAMETERS = (
    ("workflow_return", "output"),
    ("workflow_run", "name_or_path"),
    ("workflow_resume", "run_id"),
    ("workflow_cancel", "run_id"),
    ("workflow_start", "name_or_path"),
    ("workflow_plan_approval", "run_id"),
    ("workflow_status", "run_id"),
    ("workflow_result", "run_id"),
    ("workflow_wait", "run_id"),
    ("workflow_events", "run_id"),
)


@pytest.mark.parametrize("tool_name, parameter_name", _REQUIRED_WORKFLOW_PARAMETERS)
def test_required_workflow_parameters_are_required(tool_name, parameter_name):
    """Omitted required arguments fail at the Python call boundary, not in a tool body."""
    tool = getattr(server, tool_name)
    parameter = inspect.signature(tool).parameters[parameter_name]

    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        tool()
