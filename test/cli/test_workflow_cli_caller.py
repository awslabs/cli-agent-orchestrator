"""``cao workflow`` from inside a CAO terminal names the terminal (Review B, finding 1).

An agent may run the CLI from its shell instead of the MCP tool; with
``CAO_TERMINAL_ID`` set the run, resume and step-replay requests carry
``caller_id`` so the server bounds the steps by that terminal's ceiling. An
operator shell sends byte-identical requests to before.
"""

from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import workflow


def _post(status_code, body):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return MagicMock(return_value=response)


@pytest.fixture
def post(monkeypatch):
    post = _post(202, {"run_id": "r1", "state": "running"})
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.api_http.post", post)
    return post


def test_run_detach_sends_caller_id(monkeypatch, post):
    monkeypatch.setenv("CAO_TERMINAL_ID", "0000beef")
    result = CliRunner().invoke(workflow, ["run", "wf", "--detach", "--json"])
    assert result.exit_code == 0, result.output
    assert post.call_args.kwargs["json"]["caller_id"] == "0000beef"


def test_run_wait_sends_caller_id(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "0000beef")
    post = _post(200, {"run_id": "r1", "state": "completed", "steps": []})
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.api_http.post", post)
    result = CliRunner().invoke(workflow, ["run", "wf", "--wait", "--json"])
    assert result.exit_code == 0, result.output
    assert post.call_args.kwargs["json"]["caller_id"] == "0000beef"


def test_operator_shell_sends_no_caller(monkeypatch, post):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    result = CliRunner().invoke(workflow, ["run", "wf", "--detach", "--json"])
    assert result.exit_code == 0, result.output
    assert "caller_id" not in post.call_args.kwargs["json"]


def test_resume_sends_caller_id(monkeypatch):
    monkeypatch.setenv("CAO_TERMINAL_ID", "0000beef")
    post = _post(200, {"run_id": "r1", "state": "completed", "steps": []})
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.api_http.post", post)
    result = CliRunner().invoke(workflow, ["resume", "r1", "--json"])
    assert result.exit_code == 0, result.output
    assert post.call_args.kwargs["json"] == {"caller_id": "0000beef"}


def test_resume_from_operator_shell_sends_no_body(monkeypatch):
    monkeypatch.delenv("CAO_TERMINAL_ID", raising=False)
    post = _post(200, {"run_id": "r1", "state": "completed", "steps": []})
    monkeypatch.setattr("cli_agent_orchestrator.cli.commands.workflow.api_http.post", post)
    result = CliRunner().invoke(workflow, ["resume", "r1", "--json"])
    assert result.exit_code == 0, result.output
    assert post.call_args.kwargs["json"] is None
