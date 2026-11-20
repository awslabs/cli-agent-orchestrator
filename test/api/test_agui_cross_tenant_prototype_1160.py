"""Prototype cross-tenant conformance test for AG-UI (``/agui/v1``), issue #1160.

CAO has **no tenant model yet** (#778 is open): no request carries a tenant, an owner
or a membership, and nothing in ``/agui/v1`` filters by one. So this file cannot, and
does not, prove a tenant boundary. It pins what the surface does **today**, for a
server with one operator, so that the day a tenant model lands these are the tests
that must change on purpose:

* with the surface off (``CAO_AGUI_ENABLED`` and ``CAO_MCP_APPS_ENABLED`` unset) every
  ``/agui/v1`` route answers 404 with the same body to every caller, whatever
  tenant-looking header the caller sends;
* with the surface on, the stream is one shared feed: two callers that present
  different tenant-looking headers receive the same frames, including each other's
  sessions. The header is ignored because nothing reads it.

"Tenant A" and "tenant B" below are only two request headers. They stand in for the
two callers a real tenant model would distinguish.
"""

from __future__ import annotations

from test._support.agui_stream_harness import LegacyFramingClient, install
from typing import Any, Dict, List, Optional, Tuple

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

TENANT_A = {"X-Tenant": "tenant-a"}
TENANT_B = {"X-Tenant": "tenant-b"}

#: Every route of the surface, so a new ungated route cannot be added unnoticed.
#: ``emit_ui`` gets a well-formed body on purpose: its handler checks the gate after body
#: validation, so a malformed body would answer 422 and the test would not reach the gate.
AGUI_ROUTES: List[Tuple[str, str, Optional[Dict[str, Any]]]] = [
    ("get", "/agui/v1/stream", None),
    ("post", "/agui/v1/stream", None),
    ("post", "/agui/v1/emit_ui", {"component": "metric", "props": {"label": "x", "value": 1}}),
    ("post", "/agui/v1/interrupts/some-interrupt/resume", None),
    ("post", "/agui/v1/run", None),
    ("get", "/agui/v1/capabilities", None),
]


def _event(event_id: int, session: str, terminal: str) -> Dict[str, Any]:
    return {
        "id": event_id,
        "kind": "launch",
        "terminal_id": terminal,
        "session_name": session,
        "timestamp": f"2026-07-04T00:00:0{event_id}Z",
        "detail": {"agent_name": "worker", "provider": "mock_cli"},
    }


@pytest.fixture(autouse=True)
def _flags_unset(monkeypatch):
    for flag in ("CAO_AGUI_ENABLED", "CAO_MCP_APPS_ENABLED"):
        monkeypatch.delenv(flag, raising=False)


@pytest.mark.parametrize("method,path,body", AGUI_ROUTES)
def test_surface_off_answers_every_caller_the_same_404(method, path, body):
    """Flag unset: 404, and the same bytes for both callers (no tenant-dependent output)."""
    client = TestClient(app, base_url="http://localhost")
    kwargs = {"json": body} if body else {}
    responses = [getattr(client, method)(path, headers=h, **kwargs) for h in (TENANT_A, TENANT_B)]

    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].content == responses[1].content


def test_surface_on_stream_is_one_shared_feed_with_no_tenant_filter(monkeypatch):
    """Flag set: both callers see both sessions' events; the tenant header changes nothing.

    This pins today's single-tenant behaviour, not a boundary. When #778 adds one, the
    expected result here becomes "A sees only A's sessions" and this test is rewritten.
    """
    install(
        monkeypatch,
        history=[_event(1, "tenant-a-session", "term-a"), _event(2, "tenant-b-session", "term-b")],
    )
    client = LegacyFramingClient(app, base_url="http://localhost")
    params = {"since": "2026-01-01T00:00:00Z"}

    body_a = client.get("/agui/v1/stream", params=params, headers=TENANT_A)
    body_b = client.get("/agui/v1/stream", params=params, headers=TENANT_B)

    assert body_a.status_code == body_b.status_code == 200
    for body in (body_a.text, body_b.text):
        assert "tenant-a-session" in body
        assert "tenant-b-session" in body
    assert body_a.text == body_b.text
