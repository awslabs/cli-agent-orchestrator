"""HTTP API tests for the Agent Plugins endpoints.

The endpoints construct their own store from the module-level constants, so
these tests patch those symbols the way the CLI tests do — exercising the real
handler bodies rather than a re-implementation of them.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.agent_plugins.store import InstalledPluginStore

from .conftest import CANONICAL_EXAMPLE_DIR, build_plugin, write_skill


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose plugin store and skill store are tmp-path backed."""
    plugins_dir = tmp_path / "agent-plugins"
    data_dir = tmp_path / "agent-plugin-data"
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()

    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGINS_DIR", plugins_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.store.AGENT_PLUGIN_DATA_DIR", data_dir
    )
    monkeypatch.setattr("cli_agent_orchestrator.agent_plugins.projection.SKILLS_DIR", skills_dir)
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skills_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.agent_plugins.installer._refresh_agent_artifacts", lambda: None
    )

    from cli_agent_orchestrator.api.main import app

    # `base_url` is not decorative: the app's host guard rejects the
    # TestClient's default `testserver` host with a 400, so every existing API
    # test pins localhost the same way.
    test_client = TestClient(app, base_url="http://localhost")
    test_client.plugins_dir = plugins_dir  # type: ignore[attr-defined]
    test_client.skills_dir = skills_dir  # type: ignore[attr-defined]
    test_client.store = InstalledPluginStore(plugins_dir, data_dir)  # type: ignore[attr-defined]
    return test_client


class TestListPlugins:
    def test_empty_store_lists_nothing(self, client):
        response = client.get("/plugins")

        assert response.status_code == 200
        assert response.json()["plugins"] == []

    def test_the_response_states_the_untrusted_content_warning(self, client):
        """Requirement 22.1 — the API surface carries the statement too."""
        body = client.get("/plugins").json()
        assert "untrusted" in body["untrusted_content_warning"].lower()

    def test_installed_plugins_report_findings_and_projected_skills(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        entry = client.get("/plugins").json()["plugins"][0]

        assert entry["name"] == "demo"
        assert entry["projected_skill_names"] == ["alpha"]
        assert "findings" in entry
        assert entry["affected_sessions"] == []

    def test_non_fatal_findings_are_reported(self, client, tmp_path):
        write_skill(client.skills_dir / "alpha", "alpha")
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        entry = client.get("/plugins").json()["plugins"][0]
        assert entry["projected_skill_names"] == []
        assert entry["skill_names"] == ["alpha"]


class TestInstall:
    def test_install_from_a_local_path(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        response = client.post("/plugins", json={"source": str(source)})

        assert response.status_code == 201, response.text
        assert response.json()["installed"] is True
        assert (client.skills_dir / "alpha" / "SKILL.md").is_file()

    def test_install_the_canonical_example(self, client):
        response = client.post("/plugins", json={"source": str(CANONICAL_EXAMPLE_DIR)})

        assert response.status_code == 201
        assert response.json()["record"]["name"] == "agent-plugins-example"

    def test_an_unloadable_plugin_returns_the_full_report(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", manifest_text="{ broken")
        response = client.post("/plugins", json={"source": str(source)})

        assert response.status_code == 422
        detail = response.json()["detail"]
        codes = [f["code"] for f in detail["report"]["findings"]]
        assert "manifest.invalid_json" in codes

    def test_an_unreachable_source_is_a_client_error(self, client, tmp_path):
        response = client.post("/plugins", json={"source": str(tmp_path / "nope")})

        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    def test_duplicate_install_is_refused_until_forced(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo")
        client.post("/plugins", json={"source": str(source)})

        assert client.post("/plugins", json={"source": str(source)}).status_code == 400
        forced = client.post("/plugins", json={"source": str(source), "force": True})
        assert forced.status_code == 201

    def test_the_source_kind_is_inferred_the_same_way_the_cli_infers_it(self, client, tmp_path):
        """A source that installs from the CLI must install from the panel."""
        from cli_agent_orchestrator.api.main import PluginInstallRequest, _plugin_source

        assert _plugin_source(PluginInstallRequest(source="/tmp/x")).kind == "path"
        assert _plugin_source(PluginInstallRequest(source="https://github.com/o/r")).kind == "git"

    def test_an_explicit_kind_overrides_inference(self, client):
        from cli_agent_orchestrator.api.main import PluginInstallRequest, _plugin_source

        source = _plugin_source(PluginInstallRequest(source="/tmp/x", kind="git"))
        assert source.kind == "git"


class TestValidate:
    def test_validate_reports_without_installing(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        response = client.post("/plugins/validate", json={"source": str(source)})

        assert response.status_code == 200
        assert response.json()["loadable"] is True
        assert list(client.skills_dir.iterdir()) == []

    def test_validate_of_an_unloadable_plugin_is_still_a_200_report(self, client, tmp_path):
        """Validation answers a question; an unloadable plugin is a valid answer."""
        source = build_plugin(tmp_path / "src", "demo", schema_id=None)
        response = client.post("/plugins/validate", json={"source": str(source)})

        assert response.status_code == 200
        body = response.json()
        assert body["loadable"] is False
        assert any(f["code"] == "manifest.schema_missing" for f in body["findings"])

    def test_every_finding_cites_a_clause(self, client, tmp_path):
        source = build_plugin(
            tmp_path / "src",
            "demo",
            skills=["alpha"],
            extra_manifest={"hooks": {"pre": "x"}},
            mcp_text="}{ not json",
        )
        body = client.post("/plugins/validate", json={"source": str(source)}).json()

        assert body["findings"]
        assert all(f["spec_ref"] for f in body["findings"])


class TestUninstall:
    def test_delete_removes_the_plugin(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        response = client.request("DELETE", "/plugins/demo")

        assert response.status_code == 200
        assert response.json()["removed"] is True
        assert not (client.skills_dir / "alpha").exists()

    def test_deleting_an_absent_plugin_is_a_404(self, client):
        assert client.request("DELETE", "/plugins/ghost").status_code == 404

    def test_purge_data_is_opt_in(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        body = client.request("DELETE", "/plugins/demo?purge_data=true").json()
        assert body["purged_data"] is True
        assert not client.store.plugin_data_dir("demo").exists()

    def test_the_delete_response_reports_affected_sessions(self, client, tmp_path, monkeypatch):
        """Requirement 17.5 — reported so a client can render the warning."""
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-live"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": ["alpha"]})(),
        )

        body = client.request("DELETE", "/plugins/demo").json()

        assert body["affected_sessions"][0]["terminal_id"] == "abcd1234"
        assert body["affected_sessions"][0]["skill_names"] == ["alpha"]

    def test_the_list_response_reports_affected_sessions_before_any_delete(
        self, client, tmp_path, monkeypatch
    ):
        """The panel needs this *before* the DELETE, to gate on it."""
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.session_service.list_sessions",
            lambda: [{"id": "cao-live"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            lambda name: [{"id": "abcd1234", "agent_profile": "worker"}],
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.load_agent_profile",
            lambda name: type("P", (), {"skills": ["alpha"]})(),
        )

        entry = client.get("/plugins").json()["plugins"][0]
        assert entry["affected_sessions"][0]["session_name"] == "cao-live"


class TestRoutesRegistered:
    def test_all_four_endpoints_exist(self):
        from cli_agent_orchestrator.api.main import app

        paths = {(r.path, tuple(sorted(r.methods))) for r in app.routes if hasattr(r, "methods")}
        assert ("/plugins", ("GET",)) in paths
        assert ("/plugins", ("POST",)) in paths
        assert ("/plugins/validate", ("POST",)) in paths
        assert ("/plugins/{name}", ("DELETE",)) in paths


class TestScopeEnforcement:
    """All four ``/plugins`` routes, with auth enabled.

    **Validates: Requirement 17 (scope posture)**

    Two halves, because either alone is misleading. ``test_scope_coverage.py``
    asserts the *structural* half — the dependency exists on the route object —
    which is the half that cannot be faked by ambient config. These assert the
    *behavioural* half: with auth on, the right token passes and the wrong one is
    refused. A structural test alone would pass if ``require_any_scope`` were
    handed the wrong scope constants; a behavioural test alone would pass with no
    dependency at all, because the dependency is a no-op when auth is off.

    403 rather than 401 throughout: ``require_any_scope`` raises 403 for a token
    that authenticated but lacks the scope, while 401 comes from
    ``get_current_scopes`` upstream on a missing or invalid token.
    """

    @pytest.fixture
    def auth_on(self, monkeypatch):
        monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")

    @pytest.fixture(autouse=True)
    def _clear_overrides(self):
        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.security import auth

        yield
        app.dependency_overrides.pop(auth.get_current_scopes, None)

    @staticmethod
    def _as(scopes):
        from cli_agent_orchestrator.api.main import app
        from cli_agent_orchestrator.security import auth

        async def _dep():
            return list(scopes)

        app.dependency_overrides[auth.get_current_scopes] = _dep

    # `POST /plugins/validate` resolves a source and `POST /plugins` installs, so
    # both are given a body that would fail *later* if the gate let them through —
    # the assertions below only ever check the gate, never the outcome.
    _BODY = {"source": "/nonexistent-plugin-source"}

    ROUTES = [
        ("get", "/plugins", None),
        ("post", "/plugins", _BODY),
        ("post", "/plugins/validate", _BODY),
        ("delete", "/plugins/whatever", None),
    ]

    @pytest.mark.parametrize("method,path,body", ROUTES)
    def test_a_scopeless_token_is_refused_on_every_plugins_route(
        self, client, auth_on, method, path, body
    ):
        """No route in the group is reachable without at least one scope."""
        self._as([])

        response = getattr(client, method)(path, **({"json": body} if body else {}))

        assert response.status_code == 403, f"{method.upper()} {path} admitted a scopeless token"

    def test_a_read_token_can_list_but_cannot_install_or_remove(self, client, auth_on):
        """The read floor is a floor, not a promotion.

        This is the assertion that distinguishes "gated" from "gated correctly":
        adding a scope dependency to ``GET /plugins`` would be worthless if the
        write routes had been widened to READ at the same time.
        """
        from cli_agent_orchestrator.security import auth

        self._as([auth.SCOPE_READ])

        assert client.get("/plugins").status_code != 403
        assert client.post("/plugins", json=self._BODY).status_code == 403
        assert client.post("/plugins/validate", json=self._BODY).status_code == 403
        assert client.delete("/plugins/whatever").status_code == 403

    @pytest.mark.parametrize("scope_name", ["SCOPE_READ", "SCOPE_WRITE", "SCOPE_ADMIN"])
    def test_every_scope_in_the_read_floor_can_list(self, client, auth_on, scope_name):
        """Guards the over-restriction failure mode.

        Gating the list on write/admin only would lock out the read-only callers
        the endpoint exists for — the web panel and status scripts.
        """
        from cli_agent_orchestrator.security import auth

        self._as([getattr(auth, scope_name)])

        assert client.get("/plugins").status_code != 403

    @pytest.mark.parametrize("scope_name", ["SCOPE_WRITE", "SCOPE_ADMIN"])
    def test_write_and_admin_are_both_admitted_on_the_mutating_routes(
        self, client, auth_on, scope_name
    ):
        from cli_agent_orchestrator.security import auth

        self._as([getattr(auth, scope_name)])

        assert client.post("/plugins", json=self._BODY).status_code != 403
        assert client.post("/plugins/validate", json=self._BODY).status_code != 403
        assert client.delete("/plugins/whatever").status_code != 403


class TestListDoesOneLiveStateWalk:
    """Adoption-audit finding R2, cost half — the walk is hoisted, not per plugin."""

    def test_sessions_are_enumerated_once_regardless_of_plugin_count(self, client, tmp_path):
        """``list_sessions`` is called once per request, not once per plugin.

        Asserted by counting calls rather than by timing: a timing assertion on a
        3-plugin store would be noise, while the call count states the actual
        invariant and fails the moment someone reintroduces the per-record call.
        """
        from cli_agent_orchestrator.agent_plugins import installer

        for name in ("alpha", "beta", "gamma"):
            source = build_plugin(tmp_path / "src" / name, name, skills=[f"skill-{name}"])
            assert client.post("/plugins", json={"source": str(source)}).status_code == 201

        calls = []

        def counting_snapshot():
            calls.append(1)
            return []

        # Patched at the module attribute the handler reaches through, so the
        # substitution is on the real call path.
        original = installer._snapshot_live_terminals
        installer._snapshot_live_terminals = counting_snapshot
        try:
            response = client.get("/plugins")
        finally:
            installer._snapshot_live_terminals = original

        assert response.status_code == 200
        assert len(response.json()["plugins"]) == 3
        assert len(calls) == 1, f"live state walked {len(calls)} times for 3 plugins"

    def test_every_plugin_still_reports_its_own_affected_sessions_key(self, client, tmp_path):
        """Hoisting must not collapse the per-plugin answer into a shared one."""
        for name in ("alpha", "beta"):
            source = build_plugin(tmp_path / "src" / name, name, skills=[f"skill-{name}"])
            client.post("/plugins", json={"source": str(source)})

        plugins = client.get("/plugins").json()["plugins"]

        assert {p["name"] for p in plugins} == {"alpha", "beta"}
        assert all("affected_sessions" in p for p in plugins)


class TestEveryPluginResponseCarriesTheWarning:
    """Requirement 22.1 on the API surface — ported from impl (WP4.6).

    A warning present only on ``GET /plugins`` is satisfiable by a client that
    never calls it. Putting it on every response describing a plugin means a
    client cannot render an install affordance without having been handed the text
    to show beside it.
    """

    def _warning(self, body: dict) -> str:
        assert "untrusted_content_warning" in body, sorted(body)
        return body["untrusted_content_warning"]

    def test_the_list_response_carries_it(self, client):
        assert "untrusted" in self._warning(client.get("/plugins").json()).lower()

    def test_a_successful_install_carries_it(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])

        response = client.post("/plugins", json={"source": str(source)})

        assert response.status_code == 201
        assert "untrusted" in self._warning(response.json()).lower()

    def test_a_successful_validate_carries_it(self, client, tmp_path):
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])

        response = client.post("/plugins/validate", json={"source": str(source)})

        assert response.status_code == 200
        assert "untrusted" in self._warning(response.json()).lower()

    def test_the_unloadable_install_422_carries_it_too(self, client, tmp_path):
        """The case most worth covering, and the easiest to miss.

        "This plugin is not loadable" is exactly the moment an operator decides
        whether to try a different source — a decision about trust. The 422 body
        goes out through ``HTTPException(detail=...)``, a different path from the
        success return, so it needs its own assertion.
        """
        source = build_plugin(tmp_path / "src", "broken", skills=["alpha"], schema_id=None)

        response = client.post("/plugins", json={"source": str(source)})

        assert response.status_code == 422
        assert "untrusted" in self._warning(response.json()["detail"]).lower()

    def test_the_warning_is_the_same_text_the_cli_prints(self, client):
        """One statement, not two that can drift apart.

        An operator who reads the warning in the panel and then reads a different
        one in the terminal has been given two different security stories.
        """
        from cli_agent_orchestrator.cli.commands.agent_plugin import UNTRUSTED_CONTENT_WARNING

        assert self._warning(client.get("/plugins").json()) == UNTRUSTED_CONTENT_WARNING

    def test_removal_is_deliberately_not_warned(self, client, tmp_path):
        """Removal is the safe direction; warning there trains people to ignore it."""
        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        client.post("/plugins", json={"source": str(source)})

        response = client.delete("/plugins/demo")

        assert response.status_code == 200
        assert "untrusted_content_warning" not in response.json()


class TestPackagingFailuresAreNotBlamedOnThePlugin:
    """WP4.5 — the ``SchemaUnavailableError`` diagnostic distinction.

    When CAO cannot load its own pinned schema, *every* plugin fails validation.
    Reporting that as a plugin defect sends the operator to fix something that is
    fine, so the report says whose fault it is.
    """

    def test_a_broken_schema_marks_the_report_as_cao_blocked(self, client, tmp_path, monkeypatch):
        from cli_agent_orchestrator.agent_plugins import validation

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])

        def unavailable(filename):
            raise validation.SchemaUnavailableError(f"vendored schema {filename!r} is missing")

        monkeypatch.setattr(validation, "_offline_validator", unavailable)

        response = client.post("/plugins/validate", json={"source": str(source)})
        body = response.json()

        assert response.status_code == 200
        assert body["loadable"] is False
        assert body["blocked_by_cao"] is True

    def test_the_finding_says_it_is_cao_and_how_to_fix_it(self, client, tmp_path, monkeypatch):
        from cli_agent_orchestrator.agent_plugins import validation

        source = build_plugin(tmp_path / "src", "demo", skills=["alpha"])
        monkeypatch.setattr(
            validation,
            "_offline_validator",
            lambda filename: (_ for _ in ()).throw(validation.SchemaUnavailableError("gone")),
        )

        body = client.post("/plugins/validate", json={"source": str(source)}).json()
        codes = [finding["code"] for finding in body["findings"]]
        messages = " ".join(finding["message"] for finding in body["findings"])

        assert validation.CAO_SCHEMA_UNAVAILABLE in codes
        assert "not with the plugin" in messages
        assert "Reinstall CAO" in messages or "refresh-agent-plugins-schemas" in messages

    def test_an_ordinary_invalid_plugin_is_not_marked_cao_blocked(self, client, tmp_path):
        """The distinction has to discriminate, not just exist.

        Without this, ``blocked_by_cao`` could be hardcoded true and the test above
        would still pass — and every real plugin defect would be excused as CAO's
        fault, which is the same conflation in the opposite direction.
        """
        source = build_plugin(tmp_path / "src", "broken", skills=["alpha"], schema_id=None)

        body = client.post("/plugins/validate", json={"source": str(source)}).json()

        assert body["loadable"] is False
        assert body["blocked_by_cao"] is False
