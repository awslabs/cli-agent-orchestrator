"""``cao workflow run`` prints a step-params refusal without a traceback (contract section 3).

Both run routes answer 422 with the messages in a string ``detail``. The CLI is a thin
HTTP client, so ``requests`` is mocked, as in ``test_workflow.py``.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.workflow import workflow

DETAIL = (
    "step 'read-orders': missing required param 'table_name' (declared by profile "
    "'orders-agent'); step 'read-orders': param 'limit' must be a whole number"
)


def _resp(status_code: int, body: dict) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = body
    return response


@pytest.mark.parametrize("args", [["run", "orders-check"], ["run", "orders-check", "--wait"]])
def test_a_422_prints_the_detail_and_exits_1(args) -> None:
    with patch("cli_agent_orchestrator.cli.commands.workflow.api_http") as api:
        api.post.return_value = _resp(422, {"detail": DETAIL})
        result = CliRunner().invoke(workflow, args)

    assert result.exit_code == 1
    assert DETAIL in result.output
    assert "Traceback" not in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)


def test_validate_prints_each_step_param_error() -> None:
    errors = [
        "step 'read-orders': missing required param 'table_name' "
        "(declared by profile 'orders-agent')",
        "step 'read-orders': param 'limit' must be a whole number",
    ]
    with patch("cli_agent_orchestrator.cli.commands.workflow.api_http") as api:
        api.post.return_value = _resp(
            200, {"status": "fail", "reserved_notes": [], "errors": errors}
        )
        result = CliRunner().invoke(workflow, ["validate", "orders-check.yaml"])

    assert result.exit_code == 1
    for error in errors:
        assert f"  error: {error}" in result.output
