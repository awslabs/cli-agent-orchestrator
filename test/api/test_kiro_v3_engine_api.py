"""Kiro v3 engine values and refusals on the HTTP API (#1167 Phase 1).

``v3`` is canonical and ``kas`` an input alias stored as ``v3`` (design D1).
The CLI, model, fingerprint and refusal-point checks live in
``test/services/test_kiro_v3_engine_select.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from cli_agent_orchestrator.models.kiro_engine import KiroEngine
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.providers.kiro_attest import KiroAttestationError
from cli_agent_orchestrator.providers.kiro_capabilities import KiroV3LaunchError
from cli_agent_orchestrator.providers.kiro_v3_schema import KiroV3UnverifiedVersionError
from cli_agent_orchestrator.providers.kiro_workspace import KiroWorkspaceError

# ---------------------------------------------------------------------------
# API: query parameters, OpenAPI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spelling", ["v3", "kas"])
def test_post_sessions_accepts_kas_and_v3_and_returns_v3(client, spelling):
    terminal = Terminal(
        id="abcd1234",
        name="w",
        session_name="s",
        provider="kiro_cli",
        agent_profile="developer",
        engine=spelling,
    )
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.create_session = AsyncMock(return_value=terminal)
        response = client.post(
            "/sessions",
            params={"provider": "kiro_cli", "agent_profile": "developer", "engine": spelling},
        )

    assert response.status_code == 201
    assert response.json()["engine"] == "v3"
    assert svc.create_session.call_args.kwargs["engine"] is KiroEngine.V3


def test_post_sessions_rejects_an_unknown_engine_with_422(client):
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.create_session = AsyncMock()
        response = client.post(
            "/sessions",
            params={"provider": "kiro_cli", "agent_profile": "developer", "engine": "v4"},
        )

    assert response.status_code == 422
    svc.create_session.assert_not_called()


@pytest.mark.parametrize(
    ("refusal", "code"),
    [
        (
            KiroV3LaunchError("kiro_v3_unrestricted", "Retry with engine 'v2'."),
            "kiro_v3_unrestricted",
        ),
        (KiroV3UnverifiedVersionError("2.36.0"), "kiro_v3_unverified_version"),
        (KiroWorkspaceError("hooks", "/repo/.kiro/hooks"), "kiro_v3_workspace_config:hooks"),
        (KiroAttestationError("kiro_v3_tools_mismatch", "shell"), "kiro_v3_tools_mismatch"),
    ],
)
def test_post_sessions_maps_each_v3_refusal_to_400_with_its_code(client, refusal, code):
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.create_session = AsyncMock(side_effect=refusal)
        response = client.post(
            "/sessions",
            params={"provider": "kiro_cli", "agent_profile": "developer", "engine": "kas"},
        )

    assert response.status_code == 400
    assert code in response.json()["detail"]


def test_openapi_lists_v2_and_v3_once_and_never_kas():
    from cli_agent_orchestrator.api.main import app

    schema = app.openapi()
    enums = [
        node["enum"]
        for node in _walk(schema)
        if isinstance(node, dict) and set(node.get("enum") or []) & {"v2", "v3", "kas"}
    ]
    assert enums, "no engine enum found in the OpenAPI schema"
    assert all(values == ["v2", "v3"] for values in enums), enums


def _walk(node):
    yield node
    if isinstance(node, dict):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


@pytest.mark.parametrize("spelling", ["v3", "kas"])
def test_run_step_body_accepts_kas_and_forwards_the_v3_member(client, spelling):
    from cli_agent_orchestrator.constants import TERMINALS_RUN_STEP_ROUTE
    from cli_agent_orchestrator.models.terminal import AgentStepResult, TerminalStatus

    result = AgentStepResult(
        terminal_id="abc12345", last_message="done", status=TerminalStatus.COMPLETED
    )
    with patch(
        "cli_agent_orchestrator.api.main.run_agent_step", new=AsyncMock(return_value=result)
    ) as run_step:
        response = client.post(
            TERMINALS_RUN_STEP_ROUTE,
            json={"provider": "kiro_cli", "agent": "developer", "prompt": "go", "engine": spelling},
        )

    assert response.status_code == 200
    assert run_step.await_args.kwargs["engine"] is KiroEngine.V3


def test_an_unknown_engine_422_lists_each_value_once(client):
    """``kas`` is an alias of ``v3``: the 422 names ``v3`` once, in ``msg`` and ``ctx`` (#1167)."""
    with patch("cli_agent_orchestrator.api.main.session_service") as svc:
        svc.create_session = AsyncMock()
        response = client.post(
            "/sessions",
            params={"provider": "kiro_cli", "agent_profile": "developer", "engine": "v4"},
        )

    assert response.status_code == 422
    (error,) = [e for e in response.json()["detail"] if e["loc"][-1] == "engine"]
    assert error["msg"] == "Input should be 'v2' or 'v3'"
    assert error["ctx"]["expected"] == "'v2' or 'v3'"
    svc.create_session.assert_not_called()
