"""Tests for the read-only profile HTTP surface.

Covers ``/agents/profiles/search``, the scaffold template routes, and the two
non-mutating ``validate`` / ``preview`` routes. These endpoints exist so
future UI, TUI, and external clients can consume the same ranking and validation
paths as the CLI instead of reimplementing them.
"""

from unittest.mock import patch

import pytest

from cli_agent_orchestrator.services.profile_search import DEFAULT_LIMIT


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
