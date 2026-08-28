"""Tests for the read-only profile HTTP surface.

Covers ``/agents/profiles/search``, the scaffold template routes, and the two
non-mutating ``validate`` / ``preview`` routes. These endpoints exist so
future UI, TUI, and external clients can consume the same ranking and validation
paths as the CLI instead of reimplementing them.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.profile_search import DEFAULT_LIMIT
from cli_agent_orchestrator.services.profile_validator import (
    _MAX_FINDINGS,
    _OMISSION_MESSAGE,
)


@pytest.fixture(autouse=True)
def _known_template_catalog():
    """Provide the enumerated names that endpoint tests may delegate with."""
    templates = [
        {"name": "aws/stepfunction", "description": "Step Functions agent", "path": "/t/sf"},
        {"name": "aws/nothing", "description": "No-schema fixture", "path": "/t/none"},
        {"name": "aws/nope", "description": "Missing-file fixture", "path": "/t/nope"},
    ]
    with patch(
        "cli_agent_orchestrator.services.agent_scaffold.list_templates",
        return_value=templates,
    ):
        yield


class TestSearchAgentProfilesEndpoint:
    """Tests for GET /agents/profiles/search."""

    def test_delegates_to_search_service_and_returns_results(self, client) -> None:
        """Results should be passed through from the shared search service verbatim."""
        results = [
            {
                "name": "monitor-tgo-sqs",
                "description": "Monitor an SQS queue",
                "capabilities": ["poll sqs queue"],
                "tags": ["sqs", "monitor"],
                "role": "monitor",
                "source": "local",
                "coverage": 2,
                "score": 2.4912,
            }
        ]

        with patch(
            "cli_agent_orchestrator.services.profile_search.search_profiles",
            return_value=results,
        ) as mock_search:
            response = client.get("/agents/profiles/search", params={"q": "monitor sqs"})

        assert response.status_code == 200
        assert response.json() == results
        mock_search.assert_called_once_with("monitor sqs", limit=DEFAULT_LIMIT)

    def test_default_limit_tracks_the_service_constant(self, client) -> None:
        """The endpoint default must not drift from ``profile_search.DEFAULT_LIMIT``.

        A hardcoded default here previously drifted from the service constant on
        the MCP surface; this test pins them together.
        """
        with patch(
            "cli_agent_orchestrator.services.profile_search.search_profiles",
            return_value=[],
        ) as mock_search:
            client.get("/agents/profiles/search", params={"q": "anything"})

        assert mock_search.call_args.kwargs["limit"] == DEFAULT_LIMIT

    def test_forwards_explicit_limit(self, client) -> None:
        """An explicit limit should reach the service unchanged."""
        with patch(
            "cli_agent_orchestrator.services.profile_search.search_profiles",
            return_value=[],
        ) as mock_search:
            response = client.get("/agents/profiles/search", params={"q": "monitor", "limit": 3})

        assert response.status_code == 200
        mock_search.assert_called_once_with("monitor", limit=3)

    def test_rejects_out_of_range_limit(self, client) -> None:
        """Limits outside 1..100 should be rejected before reaching the service."""
        assert (
            client.get("/agents/profiles/search", params={"q": "x", "limit": 0}).status_code == 422
        )
        assert (
            client.get("/agents/profiles/search", params={"q": "x", "limit": 101}).status_code
            == 422
        )

    def test_requires_query(self, client) -> None:
        """A missing ``q`` is a validation error, not an empty result."""
        assert client.get("/agents/profiles/search").status_code == 422

    def test_search_is_not_captured_as_a_profile_name(self, client) -> None:
        """Pins route ordering: ``/search`` must resolve before ``/{name}``.

        If the static route were declared below ``/agents/profiles/{name}``,
        FastAPI would route this request to the profile-detail handler with
        name="search" and this test would see its 404/400 instead.
        """
        with patch(
            "cli_agent_orchestrator.services.profile_search.search_profiles",
            return_value=[],
        ) as mock_search:
            response = client.get("/agents/profiles/search", params={"q": "anything"})

        assert response.status_code == 200
        assert mock_search.called

    def test_maps_search_service_failure_to_500(self, client) -> None:
        """Unexpected search-service failures should use the API error envelope."""
        with patch(
            "cli_agent_orchestrator.services.profile_search.search_profiles",
            side_effect=RuntimeError("search unavailable"),
        ):
            response = client.get("/agents/profiles/search", params={"q": "anything"})

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to search agent profiles: search unavailable"


class TestListProfileTemplatesEndpoint:
    """Tests for GET /agents/profiles/templates."""

    def test_returns_only_public_template_metadata(self, client) -> None:
        """Internal template paths must not cross the public HTTP boundary."""
        templates = [
            {"name": "aws/stepfunction", "description": "Step Functions agent", "path": "/t/sf"}
        ]

        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.list_templates",
            return_value=templates,
        ):
            response = client.get("/agents/profiles/templates")

        assert response.status_code == 200
        assert response.json() == [
            {"name": "aws/stepfunction", "description": "Step Functions agent"}
        ]
        assert "path" not in response.json()[0]

    def test_templates_is_not_captured_as_a_profile_name(self, client) -> None:
        """Pins route ordering for the ``/templates`` static path."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.list_templates",
            return_value=[],
        ) as mock_list:
            response = client.get("/agents/profiles/templates")

        assert response.status_code == 200
        assert mock_list.called

    def test_maps_template_service_failure_to_500(self, client) -> None:
        """Unexpected catalog failures should use the API error envelope."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.list_templates",
            side_effect=RuntimeError("catalog unavailable"),
        ):
            response = client.get("/agents/profiles/templates")

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to list profile templates: catalog unavailable"


class TestGetProfileTemplateSchemaEndpoint:
    """Tests for GET /agents/profiles/templates/{category}/{name}/schema."""

    def test_returns_schema(self, client) -> None:
        """A known template should return its JSON-Schema."""
        schema = {"type": "object", "properties": {"queue_url": {"type": "string"}}}

        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.get_template_schema",
            return_value=schema,
        ) as mock_get:
            response = client.get("/agents/profiles/templates/aws/stepfunction/schema")

        assert response.status_code == 200
        assert response.json() == schema
        mock_get.assert_called_once_with("aws/stepfunction")

    def test_returns_404_when_template_has_no_schema(self, client) -> None:
        """A ``None`` return from the service means no schema file exists."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.get_template_schema",
            return_value=None,
        ):
            response = client.get("/agents/profiles/templates/aws/nothing/schema")

        assert response.status_code == 404
        assert "No schema found" in response.json()["detail"]

    def test_rejects_invalid_path_segment_before_services(self, client) -> None:
        """An invalid segment must reach the handler and fail its allowlist check."""
        with (
            patch("cli_agent_orchestrator.services.agent_scaffold.list_templates") as mock_list,
            patch("cli_agent_orchestrator.services.agent_scaffold.get_template_schema") as mock_get,
        ):
            response = client.get("/agents/profiles/templates/aws/b@d/schema")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid template name: aws/b@d"
        assert not mock_list.called
        assert not mock_get.called

    def test_surfaces_containment_failure_as_400(self, client) -> None:
        """A containment error from the service should not become a 500."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.get_template_schema",
            side_effect=FileNotFoundError("Template path escapes templates root"),
        ):
            response = client.get("/agents/profiles/templates/aws/stepfunction/schema")

        assert response.status_code == 400
        assert "escapes templates root" in response.json()["detail"]


class TestValidateProfileTemplateConfigEndpoint:
    """Tests for POST /agents/profiles/templates/validate."""

    def test_valid_config_reports_no_errors(self, client) -> None:
        """An empty error list from the service means the config is valid."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.validate_config",
            return_value=[],
        ) as mock_validate:
            response = client.post(
                "/agents/profiles/templates/validate",
                json={"template": "aws/stepfunction", "config": {"queue_url": "https://q"}},
            )

        assert response.status_code == 200
        assert response.json() == {"valid": True, "errors": []}
        mock_validate.assert_called_once_with("aws/stepfunction", {"queue_url": "https://q"})

    def test_invalid_config_reports_errors(self, client) -> None:
        """Schema errors should be returned as a list with ``valid`` false."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.validate_config",
            return_value=["queue_url: 'x' is not a 'uri'"],
        ):
            response = client.post(
                "/agents/profiles/templates/validate",
                json={"template": "aws/stepfunction", "config": {"queue_url": "x"}},
            )

        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert response.json()["errors"] == ["queue_url: 'x' is not a 'uri'"]

    def test_rejects_malformed_template_name(self, client) -> None:
        """The allowlist pattern should reject traversal in the body field."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.validate_config"
        ) as mock_validate:
            response = client.post(
                "/agents/profiles/templates/validate",
                json={"template": "../../etc/passwd", "config": {}},
            )

        assert response.status_code == 422
        assert not mock_validate.called

    def test_rejects_single_segment_template_name(self, client) -> None:
        """Template identifiers are ``category/name``; a bare name is invalid."""
        response = client.post(
            "/agents/profiles/templates/validate",
            json={"template": "stepfunction", "config": {}},
        )

        assert response.status_code == 422

    def test_rejects_unknown_well_formed_template_before_service(self, client) -> None:
        """A catalog miss must not pass the caller's string to the scaffold service."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.validate_config"
        ) as mock_validate:
            response = client.post(
                "/agents/profiles/templates/validate",
                json={"template": "aws/not-in-catalog", "config": {}},
            )

        assert response.status_code == 404
        assert response.json()["detail"] == "Template not found: aws/not-in-catalog"
        assert not mock_validate.called

    def test_config_defaults_to_empty_dict(self, client) -> None:
        """Omitting ``config`` should validate an empty config, not 422."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.validate_config",
            return_value=["queue_url: 'queue_url' is a required property"],
        ) as mock_validate:
            response = client.post(
                "/agents/profiles/templates/validate", json={"template": "aws/stepfunction"}
            )

        assert response.status_code == 200
        mock_validate.assert_called_once_with("aws/stepfunction", {})


class TestPreviewProfileTemplateEndpoint:
    """Tests for POST /agents/profiles/templates/preview."""

    def test_returns_rendered_content(self, client) -> None:
        """A successful render should return the markdown and echo the template."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.render_template",
            return_value="---\nname: sf\n---\nBody",
        ) as mock_render:
            response = client.post(
                "/agents/profiles/templates/preview",
                json={"template": "aws/stepfunction", "config": {"queue_url": "https://q"}},
            )

        assert response.status_code == 200
        assert response.json() == {
            "template": "aws/stepfunction",
            "content": "---\nname: sf\n---\nBody",
        }
        mock_render.assert_called_once_with("aws/stepfunction", {"queue_url": "https://q"})

    def test_invalid_config_returns_400(self, client) -> None:
        """``render_template`` validates first, so bad config is a 400 not partial output."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.render_template",
            side_effect=ValueError("Config validation failed for 'aws/stepfunction'"),
        ):
            response = client.post(
                "/agents/profiles/templates/preview",
                json={"template": "aws/stepfunction", "config": {}},
            )

        assert response.status_code == 400
        assert "Config validation failed" in response.json()["detail"]

    def test_missing_template_returns_404(self, client) -> None:
        """An unknown template should be a 404."""
        with patch(
            "cli_agent_orchestrator.services.agent_scaffold.render_template",
            side_effect=FileNotFoundError("Template 'aws/nope' not found"),
        ):
            response = client.post(
                "/agents/profiles/templates/preview",
                json={"template": "aws/nope", "config": {}},
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_rejects_malformed_template_name(self, client) -> None:
        """Traversal in the body field must not reach the render service."""
        with patch("cli_agent_orchestrator.services.agent_scaffold.render_template") as mock_render:
            response = client.post(
                "/agents/profiles/templates/preview",
                json={"template": "aws/../../etc", "config": {}},
            )

        assert response.status_code == 422
        assert not mock_render.called


_VALID_PROFILE = """---
name: test-agent
description: A test agent
---

You are a test agent.
"""


class TestValidateAgentProfileEndpoint:
    """Tests for POST /agents/profiles/validate."""

    def test_valid_profile_reports_valid_with_no_messages(self, client) -> None:
        """A schema-clean profile with no advisories should come back empty."""
        response = client.post("/agents/profiles/validate", json={"content": _VALID_PROFILE})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is True
        assert body["messages"] == []

    def test_schema_violation_is_an_error_with_a_path(self, client) -> None:
        """JSON-Schema failures must be error severity and carry the field path."""
        content = "---\nname: test-agent\nengine: v3\n---\n\nBody.\n"
        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        errors = [m for m in body["messages"] if m["severity"] == "error"]
        assert any(m["path"] == "engine" for m in errors)

    def test_grok_native_workflows_opt_in_is_validated(self, client) -> None:
        """The install/validate schema accepts the typed Grok opt-in."""
        content = (
            "---\n"
            "name: grok-native\n"
            "provider: grok_cli\n"
            "grokNativeWorkflows: true\n"
            "---\n\n"
            "Body.\n"
        )
        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        assert response.json() == {"valid": True, "messages": []}

    def test_missing_required_name_is_an_error(self, client) -> None:
        """``name`` is the only required field; omitting it must invalidate."""
        content = "---\ndescription: no name here\n---\n\nBody.\n"
        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert any("name" in m["message"] for m in body["messages"])

    def test_deprecated_field_yields_a_deprecation_warning(self, client) -> None:
        """A deprecated key produces a warning and, separately, a schema error.

        ``additionalProperties: false`` rejects the unknown key, so the profile
        is not valid. The warning exists to explain *why* in useful terms rather
        than leaving only the generic schema message.
        """
        content = "---\nname: test-agent\nautoApproveTools: true\n---\n\nBody.\n"
        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        warnings = [m for m in body["messages"] if m["severity"] == "warning"]
        assert any("deprecated" in m["message"] for m in warnings)
        assert body["valid"] is False

    def test_non_builtin_role_warns_without_invalidating(self, client) -> None:
        """Custom roles are legal but advisory-flagged, as in the CLI.

        This is the property the UI relies on to decide whether to block a save:
        a warning-only profile must still report ``valid: true``.
        """
        content = "---\nname: test-agent\nrole: not-a-real-role\n---\n\nBody.\n"
        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is True
        warnings = [m for m in body["messages"] if m["severity"] == "warning"]
        assert any("role" in m["message"] for m in warnings)

    def test_oversized_content_is_rejected_by_the_model(self, client) -> None:
        """The body is length-bounded so an unbounded parse cannot be forced."""
        response = client.post(
            "/agents/profiles/validate",
            json={"content": "x" * 300_000},
        )

        assert response.status_code == 422


class TestAgentProfileSchemaEndpoint:
    """Tests for GET /agents/profiles/schema."""

    def test_returns_the_profile_schema(self, client) -> None:
        """The served document must be the profile schema itself."""
        response = client.get("/agents/profiles/schema")

        assert response.status_code == 200
        schema = response.json()
        assert schema["required"] == ["name"]
        assert schema["additionalProperties"] is False
        assert "engine" in schema["properties"]
        assert schema["properties"]["grokNativeWorkflows"] == {
            "type": "boolean",
            "default": False,
            "description": (
                "Grok Build only. Explicitly permits Grok-native subagents, "
                "workflows, and /goal in this CAO terminal."
            ),
        }

    def test_is_not_shadowed_by_the_name_route(self, client) -> None:
        """Route ordering regression guard.

        ``GET /agents/profiles/{name}`` is declared after this route. If the two
        are ever reordered, FastAPI matches in declaration order and this path
        would be captured as a profile literally named "schema", surfacing as a
        404 from the profile lookup rather than the schema document.
        """
        with patch("cli_agent_orchestrator.utils.agent_profiles.load_agent_profile") as mock_load:
            response = client.get("/agents/profiles/schema")

        assert response.status_code == 200
        assert not mock_load.called
        assert "properties" in response.json()


class TestValidateEndpointOnMalformedInput:
    """The endpoint must diagnose bad documents, not 500 on them.

    Regression guard for the P3 finding on #575: these three shapes raised
    ``TypeError`` inside the handler, which is not caught by its ``except
    ValueError``, so the client received HTTP 500 instead of the findings it
    asked for. Asserting the status explicitly is the point of these tests.
    """

    def test_unhashable_allowed_tools_entry_returns_findings(self, client) -> None:
        content = "---\nname: x\nallowedTools:\n  - [Read]\n---\n\nBody.\n"

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert any(m["severity"] == "error" for m in body["messages"])

    def test_unhashable_role_returns_findings(self, client) -> None:
        content = "---\nname: x\nrole:\n  - developer\n---\n\nBody.\n"

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        assert response.json()["valid"] is False

    def test_mixed_type_mapping_keys_return_findings(self, client) -> None:
        content = "---\nname: x\nmcpServers:\n  1: {}\n  x: {}\n---\n\nBody.\n"

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        assert response.json()["valid"] is False


# --------------------------------------------------------------------------
# Write endpoints (POST / PUT / DELETE) and the authoring read
# --------------------------------------------------------------------------

VALID_PROFILE = "---\nname: {name}\ndescription: A test profile.\n---\n\nYou are a test agent.\n"


@pytest.fixture()
def write_store(tmp_path, monkeypatch):
    """Point the local profile store at a tmp dir for the write routes.

    Both module references must be patched. ``profile_store`` and
    ``agent_profiles`` each import ``LOCAL_AGENT_STORE_DIR`` by value, so the
    write routes read one copy and the source route reads the other. Patching
    only ``profile_store`` leaves the source route resolving against the real
    store on the developer's machine.
    """
    from cli_agent_orchestrator.services import profile_store
    from cli_agent_orchestrator.utils import agent_profiles

    target = tmp_path / "agent-store"
    monkeypatch.setattr(profile_store, "LOCAL_AGENT_STORE_DIR", target)
    monkeypatch.setattr(agent_profiles, "LOCAL_AGENT_STORE_DIR", target)
    return target


class TestCreateAgentProfileEndpoint:
    """POST /agents/profiles -- create from a supplied document."""

    def test_creates_a_profile_and_returns_201(self, client, write_store) -> None:
        response = client.post(
            "/agents/profiles",
            json={"name": "fresh", "content": VALID_PROFILE.format(name="fresh")},
        )

        assert response.status_code == 201
        assert response.json()["name"] == "fresh"
        assert (write_store / "fresh.md").exists()

    def test_conflicting_name_returns_409(self, client, write_store) -> None:
        """Conflict is detected inside the write lock, not by a pre-check."""
        body = {"name": "dupe", "content": VALID_PROFILE.format(name="dupe")}
        assert client.post("/agents/profiles", json=body).status_code == 201

        assert client.post("/agents/profiles", json=body).status_code == 409

    def test_invalid_profile_is_rejected_and_not_written(self, client, write_store) -> None:
        """Validation runs before persistence, so nothing reaches disk."""
        content = "---\nname: bad\nengine: v3\n---\n\nBody.\n"

        response = client.post("/agents/profiles", json={"name": "bad", "content": content})

        assert response.status_code == 400
        assert not (write_store / "bad.md").exists()
        assert response.json()["detail"]["errors"]

    def test_frontmatter_name_mismatch_is_rejected(self, client, write_store) -> None:
        """The storage name and the frontmatter name must agree.

        Without this the two silently diverge: the profile loads under its
        frontmatter name while being addressed by its filename stem.
        """
        content = VALID_PROFILE.format(name="something-else")

        response = client.post("/agents/profiles", json={"name": "declared", "content": content})

        assert response.status_code == 400
        assert "does not match" in response.json()["detail"]["message"]
        assert not (write_store / "declared.md").exists()

    def test_unsafe_name_is_rejected(self, client, write_store) -> None:
        content = "---\nname: ok\n---\n\nBody.\n"

        response = client.post("/agents/profiles", json={"name": "../escape", "content": content})

        assert response.status_code == 400

    def test_warnings_do_not_block_the_write(self, client, write_store) -> None:
        """A warning-only profile is written, with the warnings returned.

        This is the block/allow contract: only errors reject a save.
        """
        content = "---\nname: warned\nrole: archaeologist\n---\n\nBody.\n"

        response = client.post("/agents/profiles", json={"name": "warned", "content": content})

        assert response.status_code == 201
        assert response.json()["warnings"]
        assert (write_store / "warned.md").exists()

    def test_oversized_content_is_rejected_by_the_model(self, client, write_store) -> None:
        response = client.post("/agents/profiles", json={"name": "big", "content": "x" * 262_145})

        assert response.status_code == 422


class TestReplaceAgentProfileEndpoint:
    """PUT /agents/profiles/{name} -- update only, never insert."""

    def test_replaces_an_existing_profile(self, client, write_store) -> None:
        client.post(
            "/agents/profiles",
            json={"name": "target", "content": VALID_PROFILE.format(name="target")},
        )
        updated = "---\nname: target\ndescription: Updated.\n---\n\nNew body.\n"

        response = client.put("/agents/profiles/target", json={"content": updated})

        assert response.status_code == 200
        assert "New body." in (write_store / "target.md").read_text(encoding="utf-8")

    def test_missing_profile_returns_404_and_creates_nothing(self, client, write_store) -> None:
        content = VALID_PROFILE.format(name="ghost")

        response = client.put("/agents/profiles/ghost", json={"content": content})

        assert response.status_code == 404
        assert not (write_store / "ghost.md").exists()

    def test_built_in_profile_cannot_be_shadowed(self, client, write_store) -> None:
        """A PUT naming a built-in must 404, not create a shadowing local file.

        ``code_supervisor`` ships with the package. An upsert would write a local
        file of the same name that wins on load, which is the condition
        ``duplicated_in`` exists to report.
        """
        content = VALID_PROFILE.format(name="code_supervisor")

        response = client.put("/agents/profiles/code_supervisor", json={"content": content})

        assert response.status_code == 404
        assert not (write_store / "code_supervisor.md").exists()

    def test_frontmatter_name_mismatch_is_rejected(self, client, write_store) -> None:
        client.post(
            "/agents/profiles",
            json={"name": "keeper", "content": VALID_PROFILE.format(name="keeper")},
        )

        response = client.put(
            "/agents/profiles/keeper", json={"content": VALID_PROFILE.format(name="renamed")}
        )

        assert response.status_code == 400
        assert "does not match" in response.json()["detail"]["message"]

    def test_invalid_profile_does_not_overwrite(self, client, write_store) -> None:
        client.post(
            "/agents/profiles",
            json={"name": "guarded", "content": VALID_PROFILE.format(name="guarded")},
        )
        original = (write_store / "guarded.md").read_text(encoding="utf-8")

        response = client.put(
            "/agents/profiles/guarded",
            json={"content": "---\nname: guarded\nengine: v3\n---\n\nBody.\n"},
        )

        assert response.status_code == 400
        assert (write_store / "guarded.md").read_text(encoding="utf-8") == original


class TestDeleteAgentProfileEndpoint:
    """DELETE /agents/profiles/{name} -- local store only."""

    def test_deletes_an_existing_profile(self, client, write_store) -> None:
        client.post(
            "/agents/profiles",
            json={"name": "doomed", "content": VALID_PROFILE.format(name="doomed")},
        )

        response = client.delete("/agents/profiles/doomed")

        assert response.status_code == 204
        assert not (write_store / "doomed.md").exists()

    def test_missing_profile_returns_404(self, client, write_store) -> None:
        assert client.delete("/agents/profiles/never-existed").status_code == 404

    def test_built_in_profile_cannot_be_deleted(self, client, write_store) -> None:
        """Built-ins are not in the local store, so they are not deletable."""
        assert client.delete("/agents/profiles/code_supervisor").status_code == 404

    def test_unsafe_name_is_rejected(self, client, write_store) -> None:
        """A single-segment unsafe name reaches the handler and is rejected there.

        An encoded traversal such as ``..%2Fescape`` never gets this far: the URL
        normalises to a different path and routing answers 405, so it does not
        exercise the name guard.
        """
        assert client.delete("/agents/profiles/bad@name").status_code == 400


class TestAgentProfileSourceEndpoint:
    """GET /agents/profiles/{name}/source -- unresolved authoring read."""

    def test_returns_the_document_as_stored(self, client, write_store) -> None:
        content = VALID_PROFILE.format(name="sourced")
        client.post("/agents/profiles", json={"name": "sourced", "content": content})

        response = client.get("/agents/profiles/sourced/source")

        assert response.status_code == 200
        assert response.json()["content"] == content

    def test_placeholders_are_not_resolved(self, client, write_store) -> None:
        """The whole point of this route.

        ``GET /agents/profiles/{name}`` runs resolve_env_vars over the raw text
        before parsing, so a managed variable would come back substituted and an
        edit round-trip would persist the resolved value. Here the placeholder
        must survive verbatim.
        """
        content = "---\nname: templated\ndescription: Uses a variable.\n---\n\nToken: ${MY_TOKEN}\n"
        client.post("/agents/profiles", json={"name": "templated", "content": content})

        response = client.get("/agents/profiles/templated/source")

        assert "${MY_TOKEN}" in response.json()["content"]

    def test_is_not_shadowed_by_the_name_route(self, client, write_store) -> None:
        """Route-ordering guard.

        ``GET /agents/profiles/{name}`` is declared first. It must not capture
        ``foo/source`` as a profile named "foo/source", and this route must not be
        served by the parsed-profile handler.
        """
        client.post(
            "/agents/profiles",
            json={"name": "distinct", "content": VALID_PROFILE.format(name="distinct")},
        )

        source = client.get("/agents/profiles/distinct/source").json()
        parsed = client.get("/agents/profiles/distinct").json()

        assert set(source) == {"name", "content"}
        assert "system_prompt" in parsed

    def test_missing_profile_returns_404(self, client, write_store) -> None:
        assert client.get("/agents/profiles/absent/source").status_code == 404


class TestNonStringMappingKeysAreRejected:
    """A profile the runtime cannot load must not reach disk.

    Reported as a P2 in round 1 of review on #585. The request body is YAML,
    which allows any scalar as a mapping key, but the gate was JSON Schema, where
    object keys are strings by definition. jsonschema saw nothing wrong with
    ``mcpServers: {1: {command: echo}}``, so the write returned 201 and created
    the file, and ``parse_agent_profile_text`` then refused to load it with a
    Pydantic error at ``mcpServers.1.[key]``. The profile saved and could not be
    read or launched, which contradicts the route's guarantee.

    The check lives in the validator rather than only on this path, so
    ``cao profile validate`` and ``POST /agents/profiles/validate`` agree with
    the write routes. A UI that validates before saving would otherwise be told
    the document is fine and then handed a 400.
    """

    CASES = {
        "mcpServers integer key": "---\nname: probe\nmcpServers:\n  1:\n    command: echo\n---\n\nB.\n",
        "toolAliases integer key": "---\nname: probe\ntoolAliases:\n  1: Read\n---\n\nB.\n",
        # YAML auto-types an unquoted date, so this key is a datetime.date. The
        # same mismatch, reached without anyone writing a number.
        "unquoted date key": "---\nname: probe\ntoolAliases:\n  2026-01-01: Read\n---\n\nB.\n",
    }

    @pytest.mark.parametrize("label", list(CASES))
    def test_create_rejects_and_writes_nothing(self, client, write_store, label) -> None:
        response = client.post(
            "/agents/profiles", json={"name": "probe", "content": self.CASES[label]}
        )

        assert response.status_code == 400, label
        assert not (write_store / "probe.md").exists(), f"{label}: profile was persisted"

    @pytest.mark.parametrize("label", list(CASES))
    def test_replace_rejects_and_leaves_the_original(self, client, write_store, label) -> None:
        """The existing document must survive a rejected update byte-for-byte."""
        write_store.mkdir(parents=True, exist_ok=True)
        original = VALID_PROFILE.format(name="probe")
        (write_store / "probe.md").write_text(original, encoding="utf-8")

        response = client.put("/agents/profiles/probe", json={"content": self.CASES[label]})

        assert response.status_code == 400, label
        assert (write_store / "probe.md").read_text(encoding="utf-8") == original, label

    def test_the_rejected_document_is_indeed_unloadable(self, write_store) -> None:
        """Anchors the reason for rejecting: the runtime cannot parse these.

        Without this, the rule above reads as an arbitrary restriction. Asserting
        the load failure keeps the justification in the suite rather than only in
        a commit message.
        """
        import pytest as _pytest

        from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

        with _pytest.raises(Exception) as excinfo:
            parse_agent_profile_text(self.CASES["mcpServers integer key"], "probe")

        assert "mcpServers" in str(excinfo.value)


class TestWriteRejectionShape:
    """Every 400 from the profile write and source routes uses one ``detail`` shape.

    A client should not have to switch on ``type(detail)``. Before this was
    unified, a schema failure returned a dict while a name mismatch and a parse
    failure returned bare strings, from the same endpoint.

    Two kinds of 400 reach the client and both are covered below: validation
    findings raised inside ``_validate_profile_for_write``, and the
    service-raised ``InvalidProfileNameError``, which ``DELETE`` reaches because
    it has no body to validate first. Round 1 of review on #585 caught that this
    class asserted the suite-wide contract while exercising only ``POST``, and
    that ``DELETE``'s name error was in fact still a bare string. The parameters
    below exist so that gap fails a test rather than merely contradicting a
    docstring.

    404 and 409 deliberately keep FastAPI's conventional bare-string ``detail``
    and are not covered here: the status code already discriminates and there are
    no findings to attach.
    """

    # (label, client method, url, json body or None)
    REJECTIONS = [
        (
            "post: schema error",
            "post",
            "/agents/profiles",
            {"name": "x", "content": "---\nname: x\nengine: v3\n---\n\nB.\n"},
        ),
        (
            "post: name mismatch",
            "post",
            "/agents/profiles",
            {"name": "x", "content": "---\nname: other\n---\n\nB.\n"},
        ),
        (
            "post: unparseable",
            "post",
            "/agents/profiles",
            {"name": "x", "content": "---\nname: [unclosed\n  b: : y\n---\n\nB.\n"},
        ),
        (
            "post: missing name",
            "post",
            "/agents/profiles",
            {"name": "x", "content": "---\ndescription: none\n---\n\nB.\n"},
        ),
        (
            "post: non-string mapping key",
            "post",
            "/agents/profiles",
            {
                "name": "x",
                "content": "---\nname: x\nmcpServers:\n  1:\n    command: echo\n---\n\nB.\n",
            },
        ),
        (
            "put: name mismatch",
            "put",
            "/agents/profiles/x",
            {"content": "---\nname: other\n---\n\nB.\n"},
        ),
        ("delete: unsafe name", "delete", "/agents/profiles/bad@name", None),
    ]

    @pytest.mark.parametrize("label,method,url,body", REJECTIONS, ids=[r[0] for r in REJECTIONS])
    def test_every_rejection_has_the_same_detail_shape(
        self, client, write_store, label, method, url, body
    ) -> None:
        call = getattr(client, method)
        response = call(url) if body is None else call(url, json=body)

        assert response.status_code == 400, label
        detail = response.json()["detail"]
        assert isinstance(detail, dict), f"{label}: detail was {type(detail).__name__}"
        assert set(detail) == {"message", "errors"}, label
        assert isinstance(detail["message"], str), label
        assert isinstance(detail["errors"], list), label

    def test_field_level_failures_carry_a_path(self, client, write_store) -> None:
        """A schema failure must say which field, so a form can render it."""
        response = client.post(
            "/agents/profiles",
            json={"name": "x", "content": "---\nname: x\nengine: v3\n---\n\nB.\n"},
        )

        errors = response.json()["detail"]["errors"]
        assert errors
        assert any(e["path"] == "engine" for e in errors)

    def test_non_field_failures_carry_an_empty_error_list(self, client, write_store) -> None:
        """A parse failure is not attributable to a field, so ``errors`` is empty.

        The key is still present, so a client can iterate it unconditionally.
        """
        response = client.post(
            "/agents/profiles",
            json={"name": "x", "content": "---\nname: [unclosed\n  b: : y\n---\n\nB.\n"},
        )

        assert response.json()["detail"]["errors"] == []


class TestValidateEndpointResistsAliasAmplification:
    """The unauthenticated validate route must not be stallable or OOM'd by its body.

    Two findings, both on this route. Round 2 of review on #585 added a
    non-string mapping key check bounded only by a depth cap, so the walk
    revisited alias-shared subtrees exponentially. Further testing then
    found the larger half: jsonschema interpolates ``repr`` of an offending
    instance into every error message it builds, so an amplified value that trips
    one ``type`` error yielded a 25 MB message at 20 anchor levels and 101 MB at
    22, which the route would then serialise into its response body.

    This route is the exposed one: it is in the scope-exemption set, so it answers
    without credentials even when OAuth is configured, and it is declared
    ``async``, so work on its thread delays every other request rather than only
    the caller's own. That exemption is pinned in
    ``test/api/test_scope_coverage.py::_EXEMPT``, which is the one place it is
    asserted; if it is ever removed, the reasoning here changes.

    Both are closed by rejecting a document whose expansion exceeds a ceiling,
    ahead of either step. The assertions are on status and response size rather
    than elapsed time, so a regression fails rather than hanging to CI's timeout.
    """

    @staticmethod
    def _bomb(levels: int, tail: str = "") -> str:
        lines = [
            "---",
            "name: bomb",
            "description: A profile.",
            "hooks:",
            "  a0: &a0 {k: v}",
        ]
        for level in range(1, levels + 1):
            lines.append(f"  a{level}: &a{level} {{x: *a{level - 1}, y: *a{level - 1}}}")
        if tail:
            lines.append(tail)
        return "\n".join(lines) + "\n---\n\nBody.\n"

    def test_an_anchor_bomb_is_rejected_with_a_small_response(self, client) -> None:
        content = self._bomb(40)
        assert len(content) < 1500

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is False
        assert any("renders to more than" in m["message"] for m in body["messages"])
        assert (
            len(response.content) < 2000
        ), f"a {len(content)}-byte body produced a {len(response.content)}-byte response"

    def test_a_bomb_that_trips_a_schema_error_does_not_render_itself(self, client) -> None:
        """The response must not grow with the bomb.

        Before the ceiling, the offending instance here was the amplified node, so
        the schema error's message was its full expansion: 25 MB of JSON out of a
        sub-kilobyte request.
        """
        for levels in (20, 22, 30):
            content = self._bomb(levels, tail="toolsSettings: [*a%d]" % levels)

            response = client.post("/agents/profiles/validate", json={"content": content})

            assert response.status_code == 200, levels
            assert response.json()["valid"] is False, levels
            assert (
                len(response.content) < 2000
            ), f"{levels} levels produced a {len(response.content)}-byte response"

    @pytest.mark.parametrize(
        "scalar_length, alias_count",
        [(2_048, 5_000), (190_000, 15_000)],
        ids=["2KB scalar x5000", "190KB scalar x15000"],
    )
    def test_an_aliased_scalar_cannot_amplify_the_response(
        self, client, scalar_length, alias_count
    ) -> None:
        """A big scalar referenced many times must not become a big response.

        The round-4 ceiling counted value occurrences, so a scalar contributed 1
        however long it was. @haofeif's two cases: the smaller returned a
        10,260,109-byte response from a 22 KB request, and the larger sat under the
        256 KB content cap while the one jsonschema instance rendering had a 2.85 GB
        lower bound. The ceiling now counts rendered bytes, the unit that cost is
        actually paid in.
        """
        scalar = "x" * scalar_length
        aliases = ", ".join(["*s"] * alias_count)
        content = (
            f"---\nname: t\ndescription: d\nhooks:\n  s: &s {scalar}\n"
            f"toolsSettings: [{aliases}]\n---\n\nB.\n"
        )

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        assert response.json()["valid"] is False
        assert len(response.content) < 2000, (
            f"a {len(content)}-byte request produced a " f"{len(response.content)}-byte response"
        )

    def test_a_cyclic_profile_is_not_persisted(self, client, write_store) -> None:
        """The write gate must not accept a profile the runtime cannot materialize.

        Reported by @haofeif on the round-4 head, where this returned 201 and wrote
        the file. Installing it then failed in the Kiro path, because
        ``model_dump_json`` refuses a circular reference. Same failure mode as the
        non-string key rule: valid enough to parse, unusable once written.
        """
        content = "---\nname: cyc\ndescription: cyclic\ntoolsSettings: &c {self: *c}\n---\n\nB.\n"

        response = client.post("/agents/profiles", json={"name": "cyc", "content": content})

        assert response.status_code == 400
        assert not (write_store / "cyc.md").exists()
        assert any("circular" in e["message"] for e in response.json()["detail"]["errors"])

    def test_a_profile_using_anchors_normally_is_still_valid(self, client) -> None:
        """The ceiling is on expansion, not on anchors."""
        content = (
            "---\nname: shared\ndescription: A profile.\ntoolsSettings:\n"
            "  common: &common {timeout: 30}\n  fs: *common\n  web: *common\n---\n\nBody.\n"
        )

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        assert response.json()["valid"] is True


class TestValidateEndpointBoundsAggregateFindings:
    """A compact permitted-size request cannot produce an unbounded response."""

    @staticmethod
    def _content(entry: str) -> str:
        items = ",".join([entry] * 130_000)
        return f"---\nname: t\ndescription: d\nallowedTools: [{items}]\n---\n\nB.\n"

    @pytest.mark.parametrize(
        "entry,expected_valid,marker_severity",
        [("0", False, "error"), ("z", True, "warning")],
        ids=["integer schema errors", "unknown string warnings"],
    )
    def test_130k_entries_return_one_bounded_result_set(
        self, client, entry: str, expected_valid: bool, marker_severity: str
    ) -> None:
        content = self._content(entry)
        assert len(content) < 262_144

        response = client.post("/agents/profiles/validate", json={"content": content})

        assert response.status_code == 200
        body = response.json()
        assert body["valid"] is expected_valid
        assert len(body["messages"]) == _MAX_FINDINGS
        assert sum(m["message"] == _OMISSION_MESSAGE for m in body["messages"]) == 1
        assert body["messages"][-1] == {
            "severity": marker_severity,
            "message": _OMISSION_MESSAGE,
            "path": None,
        }
        assert len(response.content) < 120_000


class TestUrlBasedMcpServersAreWritable:
    """A url-based MCP entry is a supported form and must survive the write gate.

    Reported as a P2 in round 2 of review on #585. ``agent_profile.schema.json``
    required ``command`` on every ``mcpServers`` entry, while
    ``resolve_mcp_server_config`` documents command-less entries shaped
    ``{"type": "http", "url": ...}`` as passing through untouched. Making that
    schema the blocking gate in front of persistence turned an incomplete
    description into a rejected save, the mirror image of the round-1 P2: that one
    let unloadable profiles through, this one blocked loadable ones.
    """

    URL_PROFILE = (
        "---\nname: {name}\ndescription: A test profile.\nmcpServers:\n"
        "  docs:\n    type: http\n    url: https://example.test/mcp\n---\n\nYou are a test agent.\n"
    )

    def test_create_accepts_a_url_based_server(self, client, write_store) -> None:
        response = client.post(
            "/agents/profiles",
            json={"name": "remote", "content": self.URL_PROFILE.format(name="remote")},
        )

        assert response.status_code == 201, response.json()
        assert (write_store / "remote.md").exists()

    def test_the_written_profile_loads_and_keeps_its_transport(self, client, write_store) -> None:
        """Accepting it is only correct if the runtime can then use it.

        Guards against fixing the gate by loosening it past what CAO supports:
        the entry has to survive both the profile parse and MCP resolution with
        its ``type``/``url`` intact.
        """
        from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text
        from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config

        client.post(
            "/agents/profiles",
            json={"name": "remote", "content": self.URL_PROFILE.format(name="remote")},
        )
        stored = (write_store / "remote.md").read_text(encoding="utf-8")

        profile = parse_agent_profile_text(stored, "remote")
        resolved = resolve_mcp_server_config(dict(profile.mcpServers["docs"]))

        assert resolved == {"type": "http", "url": "https://example.test/mcp"}

    def test_replace_accepts_a_url_based_server(self, client, write_store) -> None:
        write_store.mkdir(parents=True, exist_ok=True)
        (write_store / "remote.md").write_text(
            VALID_PROFILE.format(name="remote"), encoding="utf-8"
        )

        response = client.put(
            "/agents/profiles/remote",
            json={"content": self.URL_PROFILE.format(name="remote")},
        )

        assert response.status_code == 200, response.json()
        assert "url: https://example.test/mcp" in (write_store / "remote.md").read_text()

    def test_an_entry_naming_no_transport_is_still_rejected(self, client, write_store) -> None:
        """The rule gained a branch; it was not removed."""
        content = (
            "---\nname: broken\ndescription: A test profile.\nmcpServers:\n"
            "  docs:\n    type: http\n---\n\nBody.\n"
        )

        response = client.post("/agents/profiles", json={"name": "broken", "content": content})

        assert response.status_code == 400
        assert not (write_store / "broken.md").exists()
        assert response.json()["detail"]["errors"]
