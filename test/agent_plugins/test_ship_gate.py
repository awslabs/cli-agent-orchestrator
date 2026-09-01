"""Requirement 16.5 ship-gate: the agent-plugin surface must not execute by default.

Review finding F1: hiding the Click group from ``cao --help`` and relying on scope
dependencies is advertisement control, not an execution gate — ``cao plugin add``
still ran and all four ``/plugins*`` routes still executed. These tests pin the
enforced behaviour: with ``CAO_AGENT_PLUGINS_ENABLED`` unset the surface is absent,
with it set the surface behaves exactly as before.

Every test here manages the environment variable itself (``monkeypatch.delenv``
rather than assuming a clean environment), because the package ``conftest``
enables the flag for the rest of the suite — the suites that exercise the surface
must opt in, and these tests must be immune to that opt-in.
"""

from __future__ import annotations

import pytest
from click.testing import CliRunner
from fastapi import HTTPException
from fastapi.testclient import TestClient

from cli_agent_orchestrator.agent_plugins.gate import ENV_VAR, agent_plugins_surface_enabled
from cli_agent_orchestrator.cli.main import cli


@pytest.fixture
def gate_off(monkeypatch):
    monkeypatch.delenv(ENV_VAR, raising=False)


@pytest.fixture
def gate_on(monkeypatch):
    monkeypatch.setenv(ENV_VAR, "1")


class TestPredicate:
    """The single predicate both surfaces consult."""

    def test_absent_env_var_means_disabled(self, gate_off):
        assert agent_plugins_surface_enabled() is False

    def test_empty_env_var_means_disabled(self, monkeypatch):
        monkeypatch.setenv(ENV_VAR, "")
        assert agent_plugins_surface_enabled() is False

    @pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", " Yes "])
    def test_truthy_spellings_match_the_agui_precedent(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert agent_plugins_surface_enabled() is True

    @pytest.mark.parametrize("value", ["0", "false", "no", "off", "enabled"])
    def test_anything_else_stays_disabled(self, monkeypatch, value):
        monkeypatch.setenv(ENV_VAR, value)
        assert agent_plugins_surface_enabled() is False

    def test_the_value_is_read_per_call_not_at_import(self, monkeypatch):
        """An operator exporting the flag must not have to restart the process."""
        monkeypatch.delenv(ENV_VAR, raising=False)
        assert agent_plugins_surface_enabled() is False
        monkeypatch.setenv(ENV_VAR, "1")
        assert agent_plugins_surface_enabled() is True


@pytest.fixture
def api_client():
    from cli_agent_orchestrator.api.main import app

    return TestClient(app, base_url="http://localhost")


#: The whole surface, so a future route cannot be added ungated without noticing.
PLUGIN_ROUTES = [
    ("get", "/plugins", None),
    ("post", "/plugins", {"source": "/tmp/nonexistent-plugin"}),
    ("post", "/plugins/validate", {"source": "/tmp/nonexistent-plugin"}),
    ("delete", "/plugins/demo", None),
]


class TestApiGate:
    @pytest.mark.parametrize("method,path,body", PLUGIN_ROUTES)
    def test_every_route_is_absent_by_default(self, api_client, gate_off, method, path, body):
        response = getattr(api_client, method)(path, **({"json": body} if body else {}))

        assert response.status_code == 404
        assert "disabled" in response.json()["detail"].lower()

    def test_the_gate_precedes_the_work(self, api_client, gate_off, monkeypatch):
        """A 404 must come from the gate, not from the install failing anyway."""

        def _explode(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("install ran behind a closed gate")

        monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.installer.install", _explode)

        response = api_client.post("/plugins", json={"source": "/tmp/nonexistent-plugin"})

        assert response.status_code == 404

    @pytest.mark.parametrize("path", ["/plugins", "/plugins/validate"])
    def test_a_malformed_body_does_not_disclose_the_route(self, api_client, gate_off, path):
        """The gate must precede request *validation*, not just the handler body.

        Found by an independent audit of the first attempt, which applied the gate
        as each handler's first statement. FastAPI validates the body — and solves
        the scope dependency — before the handler runs, so a malformed payload came
        back 422 and an unauthorized caller 401/403, each of which tells a prober
        that the gated route exists. An absent surface answers everything 404, which
        is what a route-level dependency delivers.
        """
        response = api_client.post(path, json={"not-the-expected-field": 1})

        assert response.status_code == 404, (
            "a 422 discloses that the gated route exists; the gate must be solved "
            "before body validation"
        )

    def test_the_gate_outranks_the_scope_dependency(self, api_client, gate_off, monkeypatch):
        """With auth on, a closed surface must 404 rather than 401/403."""
        import cli_agent_orchestrator.api.main as api_main

        def _unauthorized(*args, **kwargs):
            raise HTTPException(status_code=401, detail="unauthorized")

        monkeypatch.setattr(api_main, "require_any_scope", lambda *a, **k: _unauthorized)

        response = api_client.get("/plugins")

        assert response.status_code == 404

    def test_enabling_the_flag_restores_the_route(self, api_client, gate_on, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path / "p"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", tmp_path / "d"
        )

        response = api_client.get("/plugins")

        assert response.status_code == 200
        assert response.json()["plugins"] == []


class TestCliGate:
    @pytest.mark.parametrize(
        "argv",
        [
            ["plugin", "list"],
            ["plugin", "add", "/tmp/nonexistent-plugin"],
            ["plugin", "validate", "/tmp/nonexistent-plugin"],
            ["plugin", "remove", "demo"],
        ],
    )
    def test_every_verb_refuses_by_default(self, gate_off, argv):
        result = CliRunner().invoke(cli, argv)

        assert result.exit_code != 0
        assert ENV_VAR in result.output

    def test_the_gate_precedes_the_work(self, gate_off, monkeypatch):
        def _explode(*args, **kwargs):  # pragma: no cover - must never be reached
            raise AssertionError("install ran behind a closed gate")

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.agent_plugin.install", _explode)

        result = CliRunner().invoke(cli, ["plugin", "add", "/tmp/nonexistent-plugin"])

        assert result.exit_code != 0

    def test_enabling_the_flag_restores_the_verb(self, gate_on, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", tmp_path / "p"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", tmp_path / "d"
        )

        result = CliRunner().invoke(cli, ["plugin", "list"])

        assert result.exit_code == 0
        assert "No agent plugins installed" in result.output

    @pytest.mark.parametrize("state", ["off", "on"])
    def test_help_never_advertises_the_group(self, monkeypatch, state):
        """M1 is still open in both states — the gate does not un-hide the verb."""
        if state == "on":
            monkeypatch.setenv(ENV_VAR, "1")
        else:
            monkeypatch.delenv(ENV_VAR, raising=False)

        result = CliRunner().invoke(cli, ["--help"])

        assert result.exit_code == 0
        assert "plugin" not in result.output
