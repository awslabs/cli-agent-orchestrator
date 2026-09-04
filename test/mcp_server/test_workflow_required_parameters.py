"""Regression tests for required workflow MCP tool parameters (issue #697)."""

import asyncio
import inspect
import json as jsonlib
from unittest.mock import patch

import pytest
import requests

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


def test_direct_workflow_run_call_uses_json_safe_omitted_optional_defaults():
    """A required-only direct call must not send a FieldInfo input sentinel."""
    payloads = []

    def post(_url, *, json, **_kwargs):
        # Exercise the same serialization boundary as requests' JSON handling.
        jsonlib.dumps(json)
        payloads.append(json)
        raise requests.ConnectionError("down")

    with patch("cli_agent_orchestrator.mcp_server.server.requests.post", side_effect=post):
        result = asyncio.run(server.workflow_run("demo"))

    assert inspect.signature(server.workflow_run).parameters["inputs"].default is None
    assert payloads == [{"name_or_path": "demo", "inputs": {}}]
    assert result["ok"] is False


def test_direct_workflow_return_call_uses_json_safe_omitted_optional_defaults(
    monkeypatch,
):
    """A required-only direct call must not send a FieldInfo schema sentinel."""
    monkeypatch.setenv("CAO_WORKFLOW_RUN_ID", "run1")
    monkeypatch.setenv("CAO_WORKFLOW_STEP_ID", "step1")
    payloads = []

    def post(_url, *, json, **_kwargs):
        # Exercise the same serialization boundary as requests' JSON handling.
        jsonlib.dumps(json)
        payloads.append(json)
        raise requests.ConnectionError("down")

    with patch("cli_agent_orchestrator.mcp_server.server.requests.post", side_effect=post):
        result = asyncio.run(server.workflow_return({"answer": 42}))

    assert inspect.signature(server.workflow_return).parameters["output_schema"].default is None
    assert payloads == [{"output": {"answer": 42}}]
    assert result["ok"] is False
