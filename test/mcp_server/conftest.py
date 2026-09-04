"""Shared fixtures for MCP server tests.

``mcp_over_testclient`` is the seam that keeps MCP tool tests meaningful now that
the memory and outcome tools are HTTP clients. Before, those tests called the
tools in-process against a real SQLite file, which is exactly the coupling the
tools were changed to remove — so a test that keeps doing it would pass while
testing something the production path no longer does.

Instead the tool's real ``requests`` call is routed into an in-process
``TestClient`` over the real FastAPI routes. That exercises both halves that
matter: the request the tool constructs, and the status-code translation it
applies to the response. It also records every request, so a test can assert on
what did NOT travel — no cwd, no scope_id, no forged identity.

Generalized from the single-endpoint pattern in ``test_handoff_equivalence.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pytest


@dataclass
class RecordedRequest:
    method: str
    path: str
    params: Optional[Dict[str, Any]] = None
    json: Optional[Dict[str, Any]] = None

    def payload_fields(self) -> set:
        """Every field name the tool put on the wire, body or query."""
        return set(self.json or {}) | set(self.params or {})


@dataclass
class MCPSeam:
    """Handle onto the routed HTTP hop."""

    requests: List[RecordedRequest] = field(default_factory=list)
    terminal_record: Optional[Dict[str, Any]] = None
    working_directory: Optional[str] = "/repo"

    @property
    def last(self) -> RecordedRequest:
        assert self.requests, "no HTTP request was issued"
        return self.requests[-1]

    def paths(self) -> List[str]:
        return [r.path for r in self.requests]


@pytest.fixture
def mcp_over_testclient(monkeypatch):
    """Route ``mcp_server.utils.requests`` into an in-process TestClient.

    The patch target is ``utils``, not ``server``: the canonical HTTP helpers
    live there, so that is where the tools' calls actually originate.
    """
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api.main import app
    from cli_agent_orchestrator.mcp_server import utils as mcp_utils
    from cli_agent_orchestrator.plugins import PluginRegistry

    app.state.plugin_registry = PluginRegistry()
    client = TestClient(app, headers={"Host": "localhost"})

    seam = MCPSeam(
        terminal_record={
            "id": "abc12345",
            "session_name": "sess-1",
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
    )
    monkeypatch.setenv("CAO_TERMINAL_ID", "abc12345")

    def _strip(url: str) -> str:
        # helpers build f"{API_BASE_URL}{path}"; TestClient wants just the path
        for marker in ("://",):
            if marker in url:
                return "/" + url.split(marker, 1)[1].split("/", 1)[1]
        return url

    def _dispatch(method: str, url, **kwargs):
        path = _strip(str(url))
        params = kwargs.get("params")
        body = kwargs.get("json")
        seam.requests.append(RecordedRequest(method, path, params, body))

        # The identity lookups are stubbed rather than routed: they read live
        # terminal/tmux state, which no unit test should depend on.
        if path == f"/terminals/{seam.terminal_record['id']}" and method == "GET":
            return _FakeResponse(200, seam.terminal_record)
        if path.endswith("/working-directory"):
            return _FakeResponse(200, {"working_directory": seam.working_directory})

        return _RequestsLike(client.request(method, path, params=params, json=body))

    for verb in ("get", "post", "delete", "put", "patch"):
        monkeypatch.setattr(
            mcp_utils.requests,
            verb,
            lambda url, _v=verb, **kw: _dispatch(_v.upper(), url, **kw),
            raising=False,
        )
    yield seam


class _RequestsLike:
    """Present an httpx response the way ``requests`` would.

    Fidelity matters here, it is not cosmetic: TestClient is httpx-backed, so its
    ``raise_for_status()`` raises ``httpx.HTTPStatusError``. The tools catch
    ``requests.RequestException``, so an unwrapped httpx response would skip every
    status-code translation under test and fall through to the generic handler —
    the tests would pass while proving nothing about 404 -> disabled or
    409 -> partial_write.
    """

    def __init__(self, response) -> None:
        self._response = response

    @property
    def status_code(self) -> int:
        return self._response.status_code

    @property
    def text(self) -> str:
        return self._response.text

    def json(self) -> Any:
        return self._response.json()

    def raise_for_status(self) -> None:
        import requests

        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}", response=self)


class _FakeResponse:
    """Minimal requests.Response stand-in for the stubbed identity lookups."""

    def __init__(self, status_code: int, payload: Any) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> Any:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import requests

            raise requests.HTTPError(f"{self.status_code}", response=self)
