"""Prototype cross-tenant conformance test for Agent Plugins (``/plugins*``, ``cao plugin``), #1160.

CAO has **no tenant model yet** (#778 is open): no request carries a tenant, an owner
or a membership, and the plugin store is one directory per server. So this file does
not prove a tenant boundary. It pins what the surface does **today**, for a server
with one operator, so that a future tenant model has to change these tests on purpose:

* with ``CAO_AGENT_PLUGINS_ENABLED`` unset the routes answer 404 with the same body to
  every caller, and ``cao plugin`` refuses, whatever tenant-looking header the caller
  sends;
* with the flag set, the installed set is one store shared by every caller: a plugin
  installed by one caller is listed to, and removable by, another caller that presents a
  different tenant-looking header. The header is ignored because nothing reads it.

"Tenant A" and "tenant B" are only two request headers. They stand in for the two
callers a real tenant model would distinguish.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient

from cli_agent_orchestrator.agent_plugins.gate import ENV_VAR
from cli_agent_orchestrator.cli.main import cli

from .conftest import build_plugin

TENANT_A = {"X-Tenant": "tenant-a"}
TENANT_B = {"X-Tenant": "tenant-b"}

PLUGIN_ROUTES = [
    ("get", "/plugins", None),
    ("post", "/plugins", {"source": "/nonexistent-plugin"}),
    ("post", "/plugins/validate", {"source": "/nonexistent-plugin"}),
    ("delete", "/plugins/demo", None),
]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient over a tmp-path plugin store, with the surface enabled."""
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    store = "cli_agent_orchestrator.agent_plugins.store"
    monkeypatch.setattr(f"{store}.AGENT_PLUGINS_DIR", tmp_path / "agent-plugins")
    monkeypatch.setattr(f"{store}.AGENT_PLUGIN_DATA_DIR", tmp_path / "agent-plugin-data")
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )
    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


@pytest.mark.parametrize("method,path,body", PLUGIN_ROUTES)
def test_surface_off_answers_every_caller_the_same_404(client, monkeypatch, method, path, body):
    """Flag unset: 404, and the same bytes for both callers (no tenant-dependent output)."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    kwargs = {"json": body} if body else {}

    responses = [getattr(client, method)(path, headers=h, **kwargs) for h in (TENANT_A, TENANT_B)]

    assert [r.status_code for r in responses] == [404, 404]
    assert responses[0].content == responses[1].content


def test_surface_off_cli_refuses_regardless_of_tenant_environment(monkeypatch):
    """Flag unset: ``cao plugin`` refuses; a tenant-looking environment variable is not read."""
    monkeypatch.delenv(ENV_VAR, raising=False)
    monkeypatch.setenv("CAO_TENANT", "tenant-b")

    result = CliRunner().invoke(cli, ["plugin", "list"])

    assert result.exit_code != 0
    assert ENV_VAR in result.output


def test_surface_on_store_is_one_shared_set_with_no_tenant_scoping(client, tmp_path):
    """Flag set: A installs, B lists and removes it; the tenant header changes nothing.

    This pins today's single-tenant behaviour, not a boundary. When #778 adds one, the
    expected result here becomes "B neither sees nor removes A's plugin" and this test
    is rewritten.
    """
    source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])

    installed = client.post("/plugins", json={"source": str(source)}, headers=TENANT_A)
    listed_by_b = client.get("/plugins", headers=TENANT_B)
    removed_by_b = client.delete("/plugins/demo", headers=TENANT_B)
    listed_after = client.get("/plugins", headers=TENANT_A)

    assert installed.status_code == 201, installed.text
    assert [p["name"] for p in listed_by_b.json()["plugins"]] == ["demo"]
    assert removed_by_b.status_code == 200, removed_by_b.text
    assert listed_after.json()["plugins"] == []
