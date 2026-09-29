"""Structural regressions for workflow authoring CodeQL path guards."""

from __future__ import annotations

import ast
import inspect

from cli_agent_orchestrator.services import workflow_spec_service as svc


def _calls(function: object) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(function))
    return [node for node in ast.walk(tree) if isinstance(node, ast.Call)]


def _call_name(call: ast.Call) -> str | None:
    return call.func.id if isinstance(call.func, ast.Name) else None


def test_authoring_uses_identity_only_lock_entrypoint() -> None:
    """A contained workflow path must not flow into target-path lock resolution."""
    for function in (svc.create_workflow, svc.update_workflow):
        call_names = {_call_name(call) for call in _calls(function)}
        assert "strict_identity_lock" in call_names
        assert "strict_target_lock" not in call_names
