"""Tests for GET /agui/v1/capabilities (#1192).

AG-UI 1.0 ``basic/capabilities.mdx``: "An implementation that exposes
capabilities by any means MUST use this shape for them." The shape is the
schema's ``AgentCapabilities``; retrieval is the implementation's choice, and
CAO serves it beside the run plane, at the same scope floor.
"""

from __future__ import annotations

from test.services.agui.agui_schema import schema_errors
from typing import Any, Dict

import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")

ROUTE = "/agui/v1/capabilities"


@pytest.fixture(autouse=True)
def _agui_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")


@pytest.fixture(autouse=True)
def _auth_off(monkeypatch):
    monkeypatch.setattr(main, "is_auth_enabled", lambda: False)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: False)


def test_capabilities_route_returns_the_declaration():
    resp = client.get(ROUTE)
    assert resp.status_code == 200
    body: Dict[str, Any] = resp.json()
    assert body["humanInTheLoop"] == {"supported": True, "approvals": True, "interrupts": True}


def test_capabilities_404_when_agui_disabled(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "false")
    monkeypatch.delenv("CAO_MCP_APPS_ENABLED", raising=False)
    assert client.get(ROUTE).status_code == 404


def test_capabilities_body_is_the_1_0_schema_shape():
    """The body validates against ``#/$defs/AgentCapabilities`` in the vendored 1.0
    schema (Draft 2020-12). The schema closes every group with
    ``unevaluatedProperties: false``, so an undescribed key fails here; the SDK
    models are ``extra="allow"`` and would not catch it."""
    body = client.get(ROUTE).json()
    assert schema_errors(body, "AgentCapabilities") == []


def test_the_schema_check_rejects_an_undescribed_capability():
    """Guard for the guard: an extra key, or a wrong type, fails the schema."""
    body = client.get(ROUTE).json()
    body["humanInTheLoop"]["editWithArgs"] = True
    assert schema_errors(body, "AgentCapabilities")
    body = client.get(ROUTE).json()
    body["humanInTheLoop"]["interrupts"] = "yes"
    assert schema_errors(body, "AgentCapabilities")


def test_capabilities_omit_approve_with_edits_and_resumable():
    """#1192: approveWithEdits is left to the per-interrupt responseSchema; and
    the HTTP binding has no resumption, so transport.resumable is never declared."""
    body = client.get(ROUTE).json()
    assert "approveWithEdits" not in body.get("humanInTheLoop", {})
    assert "resumable" not in body.get("transport", {})


def test_capabilities_requires_read_scope(monkeypatch):
    """Same floor as POST /agui/v1/run: a token with no scope is refused."""
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token", lambda tok: []
    )
    resp = client.get(ROUTE, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 403


def test_capabilities_read_scope_is_enough(monkeypatch):
    monkeypatch.setattr(main, "is_auth_enabled", lambda: True)
    monkeypatch.setattr("cli_agent_orchestrator.security.auth.is_auth_enabled", lambda: True)
    monkeypatch.setattr(
        "cli_agent_orchestrator.security.auth.extract_scopes_from_token",
        lambda tok: ["cao:read"],
    )
    resp = client.get(ROUTE, headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
