"""Tests for the U1 profile-management API endpoints (#510).

Covers the additive ``/agents/profiles/*`` routes: search (R1), templates (R2),
template schema (R3), validate (R4), create (R5), edit/clone (R6). The reused
services (``search_profiles``, ``agent_scaffold``, the CLI's
``_validate_frontmatter``, and the containment path helpers) are proven correct
by their own suites; these tests prove the ROUTES surface that behavior
faithfully — parity, not reimplementation.

Fixture/patch conventions mirror the sibling ``test_api_profiles.py`` (the shared
``client`` fixture from ``conftest.py`` + ``patch``/``monkeypatch``).
"""

import importlib.resources as resources
from unittest.mock import MagicMock

import frontmatter
import pytest

import cli_agent_orchestrator.api.main as main_mod
from cli_agent_orchestrator.cli.commands.profile import _validate_frontmatter
from cli_agent_orchestrator.security import auth
from cli_agent_orchestrator.services.agent_scaffold import get_template_schema, list_templates
from cli_agent_orchestrator.services.profile_search import search_profiles

_LIST_PROFILES = "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles"


def _profile(
    name,
    description="",
    *,
    tags=None,
    capabilities=None,
    role="developer",
    source="local",
    loadable=True,
):
    """Build a profile-catalog row the way ``list_agent_profiles`` emits it."""
    return {
        "name": name,
        "description": description,
        "tags": tags or [],
        "capabilities": capabilities or [],
        "role": role,
        "source": source,
        "loadable": loadable,
    }


# --- A valid config + valid rendered profile for create tests -----------------

_VALID_SQS_CONFIG = {
    "profile": "myprofile",
    "region": "us-east-1",
    "queue_url": "https://sqs.us-east-1.amazonaws.com/123456789012/myqueue",
}


@pytest.fixture
def local_store(tmp_path, monkeypatch):
    """Redirect the local agent store to a temp dir for write-path tests."""
    store = tmp_path / "agent-store"
    store.mkdir()
    monkeypatch.setattr(main_mod, "LOCAL_AGENT_STORE_DIR", store)
    return store


# =============================================================================
# R1 — search (ranking parity, FR1)
# =============================================================================


class TestSearchRankingParity:
    """The search route is a pure pass-through to ``search_profiles``; these
    tests prove it surfaces the service order verbatim (no re-sort/slice/
    re-filter/re-map)."""

    def test_endpoint_equals_service_output_exactly(self, client, monkeypatch):
        """Order- AND membership-sensitive parity: the endpoint returns exactly
        ``search_profiles(q, limit)``. This single deep-equality assertion
        catches ANY route-level drift (reorder, re-filter, slice, re-map)."""
        corpus = [
            _profile("aaa-partial", "sqs"),
            _profile("zzz-full", "sqs monitoring queue"),
            _profile("mmm-mid", "sqs monitoring"),
            _profile("no-match", "unrelated dynamodb thing"),
        ]
        monkeypatch.setattr(_LIST_PROFILES, lambda: corpus)

        expected = search_profiles("sqs monitoring", limit=10)
        assert expected  # sanity: fixture actually matches something

        resp = client.get("/agents/profiles/search", params={"q": "sqs monitoring", "limit": 10})
        assert resp.status_code == 200
        assert resp.json() == expected

    def test_name_ascending_tie_break_preserved(self, client, monkeypatch):
        """Two profiles that tie on coverage AND BM25 (identical searchable text)
        are returned name-ascending — surfaced from the service order verbatim."""
        corpus = [
            _profile("zebra-agent", "sqs monitoring"),
            _profile("apple-agent", "sqs monitoring"),
        ]
        monkeypatch.setattr(_LIST_PROFILES, lambda: corpus)

        resp = client.get("/agents/profiles/search", params={"q": "sqs monitoring"})
        assert resp.status_code == 200
        names = [r["name"] for r in resp.json()]
        # Fixture is inserted zebra-first; the service (and thus the route) must
        # order them apple, zebra by the name tie-break.
        assert names == ["apple-agent", "zebra-agent"]

    def test_unloadable_profiles_excluded(self, client, monkeypatch):
        """Unloadable profiles are excluded by the service; the route surfaces
        the filtered set (BR-3)."""
        corpus = [
            _profile("good-monitor", "sqs monitoring", loadable=True),
            _profile("broken-monitor", "sqs monitoring", loadable=False),
        ]
        monkeypatch.setattr(_LIST_PROFILES, lambda: corpus)

        resp = client.get("/agents/profiles/search", params={"q": "sqs monitoring"})
        assert resp.status_code == 200
        assert [r["name"] for r in resp.json()] == ["good-monitor"]

    @pytest.mark.parametrize(
        "params",
        [
            {"q": ""},  # empty query
            {"q": "   "},  # whitespace-only
            {"q": "--- !!!"},  # all-punctuation (tokenizes empty)
            {"q": "sqs", "limit": 0},  # limit == 0
            {"q": "sqs", "limit": -3},  # limit < 0
        ],
    )
    def test_empty_and_zero_limit_return_empty(self, client, monkeypatch, params):
        """Empty/whitespace/punctuation query or limit<=0 → [] (BR-4)."""
        monkeypatch.setattr(_LIST_PROFILES, lambda: [_profile("sqs-a", "sqs monitoring")])
        resp = client.get("/agents/profiles/search", params=params)
        assert resp.status_code == 200
        assert resp.json() == []


# =============================================================================
# R2 / R3 — templates & schema (FR4)
# =============================================================================


class TestTemplatesAndSchema:
    def test_list_templates_matches_service(self, client):
        resp = client.get("/agents/profiles/templates")
        assert resp.status_code == 200
        expected = list_templates()
        assert resp.json() == expected
        # Contract: every item carries name/description/path.
        for item in resp.json():
            assert set(item.keys()) == {"name", "description", "path"}

    def test_template_schema_found(self, client):
        resp = client.get("/agents/profiles/templates/aws/sqs-monitor/schema")
        assert resp.status_code == 200
        assert resp.json() == get_template_schema("aws/sqs-monitor")

    def test_template_schema_missing_returns_404(self, client):
        resp = client.get("/agents/profiles/templates/aws/does-not-exist/schema")
        assert resp.status_code == 404


# =============================================================================
# R4 — validate (validation parity, FR2)
# =============================================================================


class TestValidateParity:
    def test_schema_invalid_profile_reports_errors(self, client):
        """A schema-invalid profile → valid:false with the same [error]s the CLI
        emits (sorted by path). Non-mutating: still a 200 body."""
        content = "---\nname: has spaces\n---\nbody\n"
        resp = client.post("/agents/profiles/validate", json={"content": content})
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        cli_errors = [
            m for m in _validate_frontmatter({"name": "has spaces"}) if m.startswith("[error]")
        ]
        assert cli_errors  # sanity
        assert body["errors"] == cli_errors

    def test_warnings_only_profile_is_valid(self, client):
        """A profile with only [warn]s (e.g. a non-built-in role) is valid:true —
        warnings never block, mirroring the CLI's 'exit 1 only on [error]'."""
        metadata = {"name": "ok-agent", "role": "totally-made-up-role"}
        resp = client.post("/agents/profiles/validate", json={"metadata": metadata})
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["errors"] == []
        assert body["warnings"]  # at least the custom-role warning

    def test_missing_both_inputs_is_422(self, client):
        resp = client.post("/agents/profiles/validate", json={})
        assert resp.status_code == 422


# =============================================================================
# R5 — create + F-1 / F-2 (FR4, supervisor-mandated)
# =============================================================================


class TestCreateProfile:
    def test_f1_provider_model_are_frontmatter_not_config(self, client, local_store, monkeypatch):
        """F-1: provider/model are set as top-level frontmatter on the RENDERED
        profile and are NEVER merged into the template config passed to
        render_template."""
        captured = {}
        real_render = main_mod.render_template

        def capturing_render(template_name, config):
            captured["config"] = config
            return real_render(template_name, config)

        monkeypatch.setattr(main_mod, "render_template", capturing_render)

        resp = client.post(
            "/agents/profiles",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )

        # (A) render_template's config must be untouched by provider/model.
        assert "provider" not in captured["config"]
        assert "model" not in captured["config"]
        # (B) create succeeded and the SAVED profile carries them as frontmatter.
        assert resp.status_code == 201
        result = resp.json()
        assert result["source"] == "local"
        saved = frontmatter.loads((local_store / f"{result['name']}.md").read_text())
        assert saved["provider"] == "claude_code"
        assert saved["model"] == "opus"

    def test_f2_rendered_invalid_profile_400_writes_nothing(self, client, local_store, monkeypatch):
        """F-2: a create whose RENDERED profile is schema-invalid returns 400
        (not 201) and writes nothing — proving validate_profile runs before the
        write."""

        def bad_render(template_name, config):
            # Schema-invalid: name violates ^[A-Za-z0-9_-]{1,64}$.
            return "---\nname: bad name\ndescription: x\n---\nbody\n"

        monkeypatch.setattr(main_mod, "render_template", bad_render)

        resp = client.post(
            "/agents/profiles",
            json={
                "template_name": "aws/sqs-monitor",
                "config": {},
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert list(local_store.glob("*.md")) == []

    @pytest.mark.parametrize(
        "body",
        [
            {"template_name": "aws/sqs-monitor", "config": _VALID_SQS_CONFIG, "model": "opus"},
            {
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
            },
        ],
    )
    def test_create_requires_provider_and_model(self, client, local_store, body):
        """Omitting provider or model is rejected before any write (ADR-006)."""
        resp = client.post("/agents/profiles", json=body)
        assert resp.status_code == 422  # Pydantic required-field rejection
        assert list(local_store.glob("*.md")) == []

    def test_create_bad_config_400(self, client, local_store):
        """An invalid template config is a 400 (via render_template's
        validate_config → ValueError), with nothing written."""
        resp = client.post(
            "/agents/profiles",
            json={
                "template_name": "aws/sqs-monitor",
                "config": {"profile": "p"},  # missing required region/queue_url
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert list(local_store.glob("*.md")) == []

    def test_create_containment_refuses_escaping_name(self, client, local_store, monkeypatch):
        """A rendered profile whose derived name escapes the local store is
        refused with no write (validation/containment guard)."""

        def escaping_render(template_name, config):
            return "---\nname: ../evil\ndescription: x\n---\nbody\n"

        monkeypatch.setattr(main_mod, "render_template", escaping_render)
        resp = client.post(
            "/agents/profiles",
            json={
                "template_name": "aws/sqs-monitor",
                "config": {},
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert list(local_store.rglob("*.md")) == []


# =============================================================================
# R5-preview — render-only preview (FR4/AC4.2, additive U1 addendum)
# =============================================================================


class TestPreviewProfile:
    def test_preview_returns_rendered_text_and_valid(self, client, local_store):
        """A valid config renders the profile text and reports valid:true, with
        provider/model present as frontmatter — and writes NOTHING."""
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is True
        assert body["errors"] == []
        # Rendered text carries the patched provider/model frontmatter (F-1).
        rendered = frontmatter.loads(body["text"])
        assert rendered["provider"] == "claude_code"
        assert rendered["model"] == "opus"
        # NON-MUTATING: nothing written to the local store.
        assert list(local_store.rglob("*.md")) == []

    def test_preview_never_writes_even_when_valid(self, client, local_store):
        """Explicit non-mutation guard: a fully valid preview leaves the store
        empty. Pins the 'preview never writes' property (fail-before-pass:
        inserting a write into the handler makes this fail)."""
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 200
        assert list(local_store.rglob("*")) == []

    def test_preview_f1_provider_model_not_in_render_config(self, client, local_store, monkeypatch):
        """F-1 for the preview path: provider/model are patched onto the RENDERED
        output, never merged into the config passed to render_template."""
        captured = {}
        real_render = main_mod.render_template

        def capturing_render(template_name, config):
            captured["config"] = config
            return real_render(template_name, config)

        monkeypatch.setattr(main_mod, "render_template", capturing_render)
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 200
        assert "provider" not in captured["config"]
        assert "model" not in captured["config"]

    def test_preview_invalid_rendered_profile_reports_errors_no_write(
        self, client, local_store, monkeypatch
    ):
        """A schema-invalid RENDERED profile is a normal 200 body with
        valid:false (not an exception, matching /validate) and writes nothing."""

        def bad_render(template_name, config):
            return "---\nname: bad name\ndescription: x\n---\nbody\n"

        monkeypatch.setattr(main_mod, "render_template", bad_render)
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": {},
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["valid"] is False
        assert body["errors"]
        assert list(local_store.rglob("*.md")) == []

    def test_preview_bad_config_400_no_write(self, client, local_store):
        """An invalid template config is a 400 (render_template's validate_config
        → ValueError), with nothing written."""
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": {"profile": "p"},  # missing required region/queue_url
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert list(local_store.rglob("*.md")) == []

    def test_preview_requires_provider_and_model(self, client, local_store):
        """provider and model are required (ADR-006); omission → 422 before any
        render/write."""
        resp = client.post(
            "/agents/profiles/preview",
            json={"template_name": "aws/sqs-monitor", "config": _VALID_SQS_CONFIG},
        )
        assert resp.status_code == 422
        assert list(local_store.rglob("*.md")) == []


# =============================================================================
# R6 — edit + clone (FR5, FR6)
# =============================================================================


class TestEditProfile:
    def _valid_profile_text(self, name="my-agent"):
        return f"---\nname: {name}\ndescription: an edited agent\nprovider: claude_code\nmodel: opus\n---\n\nBody.\n"

    def test_edit_updates_local_profile_and_revalidates(self, client, local_store):
        target = local_store / "my-agent.md"
        target.write_text(self._valid_profile_text(), encoding="utf-8")

        updated = self._valid_profile_text().replace("an edited agent", "updated description")
        resp = client.put(
            "/agents/profiles/my-agent",
            json={
                "content": updated,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 200
        assert resp.json()["source"] == "local"
        assert "updated description" in target.read_text()

    def test_edit_invalid_content_400_no_write(self, client, local_store):
        target = local_store / "my-agent.md"
        original = self._valid_profile_text()
        target.write_text(original, encoding="utf-8")

        invalid = "---\nname: has spaces\n---\nbody\n"
        resp = client.put(
            "/agents/profiles/my-agent",
            json={
                "content": invalid,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert target.read_text() == original  # unchanged (re-validation blocked)

    def test_edit_builtin_is_refused(self, client, local_store):
        """A name with no local-store file (e.g. a built-in) is refused — built-ins
        are read-only (FR6, BR-13)."""
        resp = client.put(
            "/agents/profiles/developer",
            json={
                "content": self._valid_profile_text("developer"),
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert not (local_store / "developer.md").exists()

    def test_edit_traversal_name_refused(self, client, local_store):
        resp = client.put(
            "/agents/profiles/..evil",
            json={
                "content": self._valid_profile_text("evil"),
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 400
        assert list(local_store.rglob("*.md")) == []

    def test_clone_creates_new_local_and_leaves_builtin_unchanged(self, client, local_store):
        """Clone-to-customize (FR6/AC6.2): cloning a built-in writes a NEW local
        profile via the content-mode create route (from-content), under a NEW
        name, and never mutates the packaged built-in. No PUT on the built-in."""
        builtin = resources.files("cli_agent_orchestrator.agent_store") / "developer.md"
        before = builtin.read_text(encoding="utf-8")

        # Client clones by submitting the built-in's (edited) content under a NEW
        # local name via the authorized content-create route.
        cloned = (
            "---\nname: developer-copy\ndescription: cloned locally\n"
            "provider: claude_code\nmodel: opus\n---\n\nBody.\n"
        )
        resp = client.post(
            "/agents/profiles/from-content",
            json={
                "name": "developer-copy",
                "content": cloned,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 201
        assert resp.json() == {
            "name": "developer-copy",
            "source": "local",
            "path": str(local_store / "developer-copy.md"),
        }
        # A NEW local profile was written...
        assert (local_store / "developer-copy.md").exists()
        assert "cloned locally" in (local_store / "developer-copy.md").read_text()
        # ...and the packaged built-in is byte-for-byte unchanged (no PUT, no
        # write to the built-in's name).
        assert builtin.read_text(encoding="utf-8") == before
        assert not (local_store / "developer.md").exists()


# =============================================================================
# R6-clone — content-mode create (POST /agents/profiles/from-content, U4 addendum)
# =============================================================================


class TestCreateFromContent:
    _CONTENT = (
        "---\nname: cloned-agent\ndescription: a cloned agent\n"
        "provider: claude_code\nmodel: opus\n---\n\nBody.\n"
    )

    def _body(self, name="cloned-agent", content=None):
        return {
            "name": name,
            "content": content if content is not None else self._CONTENT,
            "provider": "claude_code",
            "model": "opus",
        }

    def test_writes_new_local_profile(self, client, local_store):
        resp = client.post("/agents/profiles/from-content", json=self._body())
        assert resp.status_code == 201
        assert resp.json()["source"] == "local"
        assert (local_store / "cloned-agent.md").exists()
        assert "a cloned agent" in (local_store / "cloned-agent.md").read_text()

    def test_refuses_overwrite_of_existing_name(self, client, local_store):
        """A clone must never silently clobber an existing local profile → 400,
        and the original content is left intact."""
        existing = local_store / "cloned-agent.md"
        existing.write_text(
            "---\nname: cloned-agent\ndescription: ORIGINAL\n---\nbody\n", encoding="utf-8"
        )
        resp = client.post("/agents/profiles/from-content", json=self._body())
        assert resp.status_code == 400
        assert "already exists" in str(resp.json()["detail"])
        # Untouched.
        assert "ORIGINAL" in existing.read_text()

    def test_invalid_content_400_no_write(self, client, local_store):
        """A schema-invalid content → 400, nothing written (F-2 shape)."""
        bad = "---\nname: has spaces\n---\nbody\n"
        resp = client.post(
            "/agents/profiles/from-content", json=self._body(name="new-one", content=bad)
        )
        assert resp.status_code == 400
        assert list(local_store.rglob("*.md")) == []

    def test_requires_provider_and_model(self, client, local_store):
        resp = client.post(
            "/agents/profiles/from-content",
            json={"name": "cloned-agent", "content": self._CONTENT, "provider": "claude_code"},
        )
        assert resp.status_code == 422  # Pydantic required-field rejection
        assert list(local_store.rglob("*.md")) == []

    def test_containment_refuses_traversal_name(self, client, local_store):
        resp = client.post("/agents/profiles/from-content", json=self._body(name="../evil"))
        assert resp.status_code == 400
        assert list(local_store.rglob("*.md")) == []


# =============================================================================
# Contract / auth / routing (FR7, ADR-001/002)
# =============================================================================


class TestRoutingAndContract:
    def test_search_path_resolves_to_search_handler_not_name(self, client, monkeypatch):
        """GET-ordering: /agents/profiles/search must resolve to the search
        handler, NOT GET {name} with name='search'. If the sub-paths were
        registered after {name}, load_agent_profile('search') would be called and
        the response would 404."""
        monkeypatch.setattr(_LIST_PROFILES, lambda: [_profile("sqs-a", "sqs monitoring")])
        sentinel = MagicMock(side_effect=AssertionError("get {name} handler was reached"))
        monkeypatch.setattr(main_mod, "load_agent_profile", sentinel)

        resp = client.get("/agents/profiles/search", params={"q": "sqs"})
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)
        sentinel.assert_not_called()

    def test_existing_list_endpoint_still_includes_all(self, client, monkeypatch):
        """Guard: the existing GET /agents/profiles is unchanged — it still
        returns include-all (unloadable retained), unlike search (BR-14)."""
        corpus = [
            _profile("loadable-one", "x", loadable=True),
            _profile("unloadable-one", "y", loadable=False),
        ]
        monkeypatch.setattr(_LIST_PROFILES, lambda: corpus)
        resp = client.get("/agents/profiles")
        assert resp.status_code == 200
        names = {p["name"] for p in resp.json()}
        assert names == {"loadable-one", "unloadable-one"}


@pytest.fixture
def auth_on(monkeypatch):
    """Enable the auth layer (default-off otherwise)."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main_mod.app.dependency_overrides.pop(auth.get_current_scopes, None)


def _override_scopes(scopes):
    async def _dep():
        return list(scopes)

    return _dep


class TestScopeGating:
    def test_create_forbidden_for_read_token(self, client, auth_on):
        main_mod.app.dependency_overrides[auth.get_current_scopes] = _override_scopes(
            [auth.SCOPE_READ]
        )
        resp = client.post(
            "/agents/profiles",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 403

    def test_edit_forbidden_for_read_token(self, client, auth_on):
        main_mod.app.dependency_overrides[auth.get_current_scopes] = _override_scopes(
            [auth.SCOPE_READ]
        )
        resp = client.put(
            "/agents/profiles/my-agent",
            json={
                "content": "---\nname: my-agent\n---\nbody\n",
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 403

    def test_search_is_open_read(self, client, auth_on, monkeypatch):
        """Read routes carry no scope dependency: search is reachable even with a
        read-only token (and would be with none)."""
        monkeypatch.setattr(_LIST_PROFILES, lambda: [_profile("sqs-a", "sqs monitoring")])
        main_mod.app.dependency_overrides[auth.get_current_scopes] = _override_scopes(
            [auth.SCOPE_READ]
        )
        resp = client.get("/agents/profiles/search", params={"q": "sqs"})
        assert resp.status_code != 403
        assert resp.status_code == 200

    def test_preview_is_open_read(self, client, auth_on):
        """Preview is non-mutating (renders + validates, never writes), so it
        carries no scope dependency — a read-only token is not 403'd (open-read
        like /validate; #510 R5-preview)."""
        main_mod.app.dependency_overrides[auth.get_current_scopes] = _override_scopes(
            [auth.SCOPE_READ]
        )
        resp = client.post(
            "/agents/profiles/preview",
            json={
                "template_name": "aws/sqs-monitor",
                "config": _VALID_SQS_CONFIG,
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code != 403
        assert resp.status_code == 200

    def test_from_content_forbidden_for_read_token(self, client, auth_on):
        """from-content genuinely writes → scope-gated WRITE|ADMIN, so a
        read-only token is 403'd (NOT in _EXEMPT; #510 R6-clone)."""
        main_mod.app.dependency_overrides[auth.get_current_scopes] = _override_scopes(
            [auth.SCOPE_READ]
        )
        resp = client.post(
            "/agents/profiles/from-content",
            json={
                "name": "cloned-agent",
                "content": "---\nname: cloned-agent\nprovider: claude_code\nmodel: opus\n---\nbody\n",
                "provider": "claude_code",
                "model": "opus",
            },
        )
        assert resp.status_code == 403
