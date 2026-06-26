"""Tests for the workflow_return MCP tool (issue #312, Bolt 2 / N4).

The tool POSTs a structured output to the single-seam output endpoint and
returns a ReturnAck envelope on EVERY path — it never raises into the agent loop
(best-effort non-blocking promise, B2-BR-9). Covered: success ack, schema-fail
ack (validated=False, no raise), missing env-var context ack, and a transport
error ack.
"""

import asyncio
import os
from unittest.mock import MagicMock, patch

import requests

from cli_agent_orchestrator.mcp_server.server import workflow_return

_ENV = {"CAO_WORKFLOW_RUN_ID": "run1", "CAO_WORKFLOW_STEP_ID": "stepA"}


def _resp(status_code, json_body):
    m = MagicMock(spec=requests.Response)
    m.status_code = status_code
    m.json.return_value = json_body
    return m


class TestWorkflowReturn:
    def test_success_returns_ok_envelope(self):
        resp = _resp(200, {"validated": True, "errors": [], "state": "completed"})
        with (
            patch.dict(os.environ, _ENV),
            patch("cli_agent_orchestrator.mcp_server.server.requests.post", return_value=resp),
        ):
            ack = asyncio.run(workflow_return({"value": 1}, output_schema=None))
        assert ack["ok"] is True
        assert ack["validated"] is True
        assert ack["errors"] == []

    def test_schema_fail_returns_unvalidated_envelope_no_raise(self):
        resp = _resp(
            200,
            {
                "validated": False,
                "errors": ["'x' is not of type 'integer'"],
                "state": "completed_unvalidated",
            },
        )
        with (
            patch.dict(os.environ, _ENV),
            patch("cli_agent_orchestrator.mcp_server.server.requests.post", return_value=resp),
        ):
            ack = asyncio.run(workflow_return({"value": "x"}, output_schema={"type": "object"}))
        # ok=True (the endpoint accepted/stored it) but validated=False.
        assert ack["ok"] is True
        assert ack["validated"] is False
        assert ack["errors"]

    def test_missing_env_returns_error_envelope_no_raise(self):
        # No CAO_WORKFLOW_* env -> structured error ack, never an exception.
        with patch.dict(os.environ, {}, clear=True):
            ack = asyncio.run(workflow_return({"value": 1}))
        assert ack["ok"] is False
        assert ack["validated"] is False
        assert ack["errors"]

    def test_transport_error_returns_error_envelope_no_raise(self):
        with (
            patch.dict(os.environ, _ENV),
            patch(
                "cli_agent_orchestrator.mcp_server.server.requests.post",
                side_effect=requests.ConnectionError("no server"),
            ),
        ):
            ack = asyncio.run(workflow_return({"value": 1}))
        assert ack["ok"] is False
        assert ack["validated"] is False
        assert any("could not reach" in e for e in ack["errors"])

    def test_non_200_returns_error_envelope(self):
        resp = _resp(400, {"detail": "run_id 'bad' is invalid"})
        # _extract_error_detail reads .json(); ensure it surfaces the detail.
        with (
            patch.dict(os.environ, _ENV),
            patch("cli_agent_orchestrator.mcp_server.server.requests.post", return_value=resp),
        ):
            ack = asyncio.run(workflow_return({"value": 1}))
        assert ack["ok"] is False
        assert ack["validated"] is False
