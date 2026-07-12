"""U4 — API route tests for GET /graph/{provider} and POST /graph/{provider}/export.

Uses the real "stub" provider (U3) and a test-file-local sink registered via
register_sink("stub-test-sink") in a fixture — the test sink is NOT shipped in
graph/sinks/. The graph registries are snapshotted/restored by the autouse
fixture in test/graph/conftest.py, so registering the test sink here does not
leak into other tests.
"""

import inspect
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.graph.models import GraphView, Node
from cli_agent_orchestrator.graph.sinks import base as sinks_base
from cli_agent_orchestrator.graph.sinks.base import GraphSink
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.security import auth


class _TestClientWithHost(TestClient):
    """TestClient that always sends a localhost Host header (TrustedHostMiddleware)."""

    def request(self, method, url, **kwargs):
        headers = kwargs.get("headers") or {}
        if not any(k.lower() == "host" for k in headers):
            headers["Host"] = "localhost"
        kwargs["headers"] = headers
        return super().request(method, url, **kwargs)


@pytest.fixture
def client():
    app.state.plugin_registry = PluginRegistry()
    return _TestClientWithHost(app)


# A per-run spy the test sink records into, so tests can assert whether
# export() was actually invoked (secret-gate short-circuit assertions).
_EXPORT_SPY = MagicMock()


@pytest.fixture
def stub_test_sink():
    """Register a capturing test sink under 'stub-test-sink' for the test.

    The autouse registry-isolation fixture (conftest) restores the registry
    afterwards, so no manual deregistration is needed.
    """
    _EXPORT_SPY.reset_mock()

    @sinks_base.register_sink("stub-test-sink")
    class _StubTestSink(GraphSink):
        def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
            _EXPORT_SPY(view=view, dest=dest, options=options)
            return [f"{dest}/stub-a.md", f"{dest}/index.md"]

    return _EXPORT_SPY


@pytest.fixture
def auth_on(monkeypatch):
    """Enable the auth enforcement layer for scope-gating tests."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(auth.get_current_scopes, None)


def _override_scopes(scopes):
    async def _dep():
        return list(scopes)

    return _dep


# ── GET /graph/{provider} ────────────────────────────────────────────────


def test_get_graph_happy_path(client):
    """GET against the stub provider returns 200 and a GraphView-shaped body."""
    resp = client.get("/graph/stub")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"nodes", "edges", "meta"}
    assert {n["id"] for n in body["nodes"]} == {"stub-a", "stub-b", "stub-c"}
    assert body["edges"][0]["source"] == "stub-a"
    assert body["meta"]["provider"] == "stub"


def test_get_graph_unregistered_provider_404(client):
    """GET on an unregistered provider name is a 404."""
    resp = client.get("/graph/does-not-exist")
    assert resp.status_code == 404


def test_get_graph_is_ungated_even_with_auth_enabled(client, auth_on):
    """With auth ENABLED, a no-Authorization GET still returns 200.

    Proves the GET route carries no scope dependency (FR-12): if it were
    gated, an unauthenticated request would be 401, not 200.
    """
    resp = client.get("/graph/stub")
    assert resp.status_code == 200


# ── POST /graph/{provider}/export ────────────────────────────────────────


def test_post_export_happy_path(client, stub_test_sink):
    """POST export resolves provider+sink, projects, exports, returns the envelope."""
    resp = client.post(
        "/graph/stub/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/does-not-matter", "options": {}},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "written_files": ["/tmp/does-not-matter/stub-a.md", "/tmp/does-not-matter/index.md"],
        "sink": "stub-test-sink",
        "dest": "/tmp/does-not-matter",
    }
    # provider projected + sink.export called exactly once.
    assert stub_test_sink.call_count == 1


def test_post_export_unregistered_sink_404(client):
    """An unregistered sink name is a 404."""
    resp = client.post(
        "/graph/stub/export",
        json={"sink": "no-such-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 404


def test_post_export_unregistered_provider_404(client, stub_test_sink):
    """An unregistered provider name is a 404."""
    resp = client.post(
        "/graph/no-such-provider/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 404


def test_post_export_no_token_401(client, stub_test_sink, auth_on):
    """With auth enabled, a request with no token is 401 (authentication)."""
    resp = client.post(
        "/graph/stub/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 401


def test_post_export_read_only_scope_403(client, stub_test_sink, auth_on):
    """A cao:read-only token is 403 on the write-gated export route."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([auth.SCOPE_READ])
    resp = client.post(
        "/graph/stub/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 403


@pytest.mark.parametrize("scope", ["cao:write", "cao:admin"])
def test_post_export_write_or_admin_scope_admitted(client, stub_test_sink, auth_on, scope):
    """A cao:write or cao:admin token passes the scope gate (200)."""
    app.dependency_overrides[auth.get_current_scopes] = _override_scopes([scope])
    resp = client.post(
        "/graph/stub/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 200


def test_post_export_secret_gate_422_and_sink_not_called(client, monkeypatch):
    """A secret in an attrs value -> 422; sink.export is NEVER called; no bytes leak.

    Registers a secret-carrying provider and a spy sink, then asserts the
    422 fires before any write and the response body does not echo the
    matched substring.
    """
    from cli_agent_orchestrator.graph.providers import base as providers_base
    from cli_agent_orchestrator.graph.providers.base import GraphProvider

    secret_value = "AKIA" + "A" * 16  # matches secret_gate's aws_access_key pattern

    @providers_base.register_provider("secret-provider")
    class _SecretProvider(GraphProvider):
        async def project(self, **filters: Any) -> GraphView:
            return GraphView(
                nodes=[Node(id="n1", kind="stub", label="N1", attrs={"token": secret_value})],
                edges=[],
            )

    spy = MagicMock()

    @sinks_base.register_sink("spy-sink")
    class _SpySink(GraphSink):
        def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
            spy(dest=dest)
            return [dest]

    resp = client.post(
        "/graph/secret-provider/export",
        json={"sink": "spy-sink", "dest": "/tmp/x"},
    )
    assert resp.status_code == 422
    assert "aws_access_key" in resp.json()["detail"]
    # The matched bytes MUST NOT leak into the response.
    assert secret_value not in resp.text
    # export() must never have run on the rejected branch.
    spy.assert_not_called()


# ── ValueError -> 400 route mapping (error-taxonomy AC) ──────────────────
#
# These doubles are test-local (registered via register_provider/register_sink
# in fixtures, restored by conftest's registry-isolation fixture) — they are
# NOT shipped in graph/providers/ or graph/sinks/. They exercise the route's
# except ValueError -> 400 mapping, which the sink-level traversal tests never
# hit (those assert ValueError at the sink API directly, not through a route).


@pytest.fixture
def value_error_provider():
    """Register a provider whose project(**filters) always raises ValueError."""
    from cli_agent_orchestrator.graph.providers import base as providers_base
    from cli_agent_orchestrator.graph.providers.base import GraphProvider

    @providers_base.register_provider("boom-provider")
    class _BoomProvider(GraphProvider):
        async def project(self, **filters: Any) -> GraphView:
            raise ValueError("bad filter value")

    return "boom-provider"


@pytest.fixture
def value_error_sink():
    """Register a sink whose export() always raises ValueError."""

    @sinks_base.register_sink("boom-sink")
    class _BoomSink(GraphSink):
        def export(self, view: GraphView, dest: str, **options: Any) -> list[str]:
            raise ValueError("bad dest / options")

    return "boom-sink"


def test_get_graph_provider_value_error_400(client, value_error_provider):
    """A provider ValueError on GET is mapped to 400 by the route."""
    resp = client.get(f"/graph/{value_error_provider}?scope=bogus")
    assert resp.status_code == 400
    assert "bad filter value" in resp.json()["detail"]


def test_post_export_provider_value_error_400(client, value_error_provider, stub_test_sink):
    """A provider ValueError on POST /export is mapped to 400 by the route.

    Regression guard for the previously-unwrapped prov.project() call: this
    test FAILS against the pre-fix code (the ValueError escaped -> 500) and
    PASSES once the handler wraps project() in try/except -> 400.
    """
    resp = client.post(
        f"/graph/{value_error_provider}/export",
        json={"sink": "stub-test-sink", "dest": "/tmp/x", "options": {}},
    )
    assert resp.status_code == 400
    assert "bad filter value" in resp.json()["detail"]


def test_post_export_sink_value_error_400(client, value_error_sink):
    """A sink ValueError on POST /export is mapped to 400 by the route."""
    resp = client.post(
        "/graph/stub/export",
        json={"sink": value_error_sink, "dest": "/tmp/x", "options": {}},
    )
    assert resp.status_code == 400
    assert "bad dest / options" in resp.json()["detail"]


# ── NFR-5: no name-branching in the route bodies ─────────────────────────


def test_routes_have_no_name_branching():
    """The two route handlers contain no if/elif over the provider/sink NAME.

    NFR-5: resolution goes through the registry only. The only conditionals
    allowed are try/except on resolution outcome — assert no `== "<name>"`
    style comparison appears in either handler source.
    """
    from cli_agent_orchestrator.api import main as api_main

    for fn in (api_main.get_graph_endpoint, api_main.export_graph_endpoint):
        src = inspect.getsource(fn)
        assert "if provider ==" not in src
        assert "elif provider ==" not in src
        assert "if body.sink ==" not in src
        assert "elif body.sink ==" not in src
        # No equality comparison against a hardcoded provider/sink literal.
        for literal in ("stub", "memory", "okf", "obsidian", "graphml"):
            assert f'== "{literal}"' not in src
