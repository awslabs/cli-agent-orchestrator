"""Tests for profile frontmatter validation as a shared service.

Covers the structured contract that both ``cao profile validate`` and
``POST /agents/profiles/validate`` sit on top of. The CLI's rendered
``[error]`` / ``[warn]`` string form is covered separately in
``test/cli/test_profile_cmd.py``, which is deliberately left unchanged so it
also serves as the no-behaviour-change guard for the extraction.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/510
"""

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.profile_validator import (
    ValidationMessage,
    load_profile_schema,
    validate_frontmatter,
    validate_profile_text,
)


class TestLoadProfileSchema:
    """Tests for load_profile_schema."""

    def test_returns_the_profile_schema(self) -> None:
        """The packaged schema must resolve regardless of module position.

        The loader is anchored via importlib.resources rather than a relative
        parent walk, so this also guards against the module being moved.
        """
        schema = load_profile_schema()

        assert schema["required"] == ["name"]
        assert schema["additionalProperties"] is False
        assert "engine" in schema["properties"]

    def test_is_cached(self) -> None:
        """Repeated calls must not re-read and re-parse the packaged file."""
        assert load_profile_schema() is load_profile_schema()


class TestValidateFrontmatter:
    """Tests for validate_frontmatter."""

    def test_valid_metadata_yields_no_findings(self) -> None:
        assert validate_frontmatter({"name": "agent", "description": "d"}) == []

    def test_missing_required_name_is_an_error(self) -> None:
        findings = validate_frontmatter({"description": "no name"})

        assert any(f.severity == "error" for f in findings)
        assert any("name" in f.message for f in findings)

    def test_schema_error_carries_the_field_path(self) -> None:
        """Errors must be locatable, so the UI can point at the offending key."""
        findings = validate_frontmatter({"name": "agent", "engine": "v3"})

        errors = [f for f in findings if f.severity == "error"]
        assert any(f.path == "engine" for f in errors)

    def test_root_level_error_uses_the_root_sentinel(self) -> None:
        """A document-level failure has no key, so path falls back to (root)."""
        findings = validate_frontmatter({})

        errors = [f for f in findings if f.severity == "error"]
        assert errors
        assert all(f.path is not None for f in errors)
        assert any(f.path == "(root)" for f in errors)

    def test_deprecated_field_yields_a_deprecation_warning(self) -> None:
        """The deprecation notice itself is advisory and not tied to a key path.

        Note this does not mean the profile is valid: ``additionalProperties:
        false`` separately rejects the unknown key as an error. Filtering on the
        field name alone would match both findings, so this narrows to the
        deprecation notice.
        """
        findings = validate_frontmatter({"name": "agent", "autoApproveTools": True})

        deprecated = [f for f in findings if "deprecated" in f.message]
        assert deprecated
        assert all(f.severity == "warning" for f in deprecated)
        assert all(f.path is None for f in deprecated)

    def test_deprecated_field_is_also_a_schema_error(self) -> None:
        """Documents the double-report, which the ordering test then constrains.

        ``additionalProperties: false`` is a document-level constraint, so the
        error is reported at ``(root)`` and names the offending key in its
        message rather than in its path. Keyed errors like a bad ``engine``
        enum do carry the field path; the two shapes differ.
        """
        findings = validate_frontmatter({"name": "agent", "autoApproveTools": True})

        errors = [f for f in findings if f.severity == "error"]
        assert any(f.path == "(root)" and "autoApproveTools" in f.message for f in errors)

    def test_deprecated_finding_precedes_the_schema_error(self) -> None:
        """Ordering is load-bearing.

        ``additionalProperties: false`` also rejects a deprecated key, but with a
        less helpful message. The deprecation notice is emitted first so it is
        the one a user reads.
        """
        findings = validate_frontmatter({"name": "agent", "autoApproveTools": True})

        first_deprecated = next(i for i, f in enumerate(findings) if "deprecated" in f.message)
        first_error = next(i for i, f in enumerate(findings) if f.severity == "error")
        assert first_deprecated < first_error

    def test_unrecognized_allowed_tool_warns(self) -> None:
        findings = validate_frontmatter({"name": "agent", "allowedTools": ["shell:aws*"]})

        warnings = [f for f in findings if f.severity == "warning"]
        assert any("shell:aws*" in f.message for f in warnings)

    def test_known_allowed_tool_does_not_warn(self) -> None:
        """Guards against the vocabulary check firing on legitimate entries."""
        findings = validate_frontmatter({"name": "agent", "allowedTools": ["fs_read"]})

        assert not any("not in CAO's recognized" in f.message for f in findings)

    def test_non_builtin_role_warns_but_stays_valid(self) -> None:
        """Custom roles are legal; the warning exists only to catch typos."""
        findings = validate_frontmatter({"name": "agent", "role": "not-a-real-role"})

        assert not any(f.severity == "error" for f in findings)
        assert any(f.severity == "warning" and "role" in f.message for f in findings)

    def test_findings_are_validation_message_instances(self) -> None:
        """The service must not leak the CLI's pre-formatted string shape."""
        findings = validate_frontmatter({"name": "agent", "engine": "v3"})

        assert all(isinstance(f, ValidationMessage) for f in findings)
        assert all(not f.message.startswith("[") for f in findings)


class TestValidateProfileText:
    """Tests for validate_profile_text."""

    def test_parses_frontmatter_and_delegates(self) -> None:
        text = "---\nname: agent\ndescription: d\n---\n\nBody.\n"

        assert validate_profile_text(text) == []

    def test_surfaces_findings_from_the_parsed_frontmatter(self) -> None:
        text = "---\nname: agent\nengine: v3\n---\n\nBody.\n"

        findings = validate_profile_text(text)
        assert any(f.severity == "error" and f.path == "engine" for f in findings)

    def test_unparseable_frontmatter_raises_value_error(self) -> None:
        """The HTTP layer maps this to 400, so the exception type is a contract.

        A parse failure is distinct from a validation failure: there is nothing
        to validate, so it cannot be reported as a finding.
        """
        text = "---\nname: [unclosed\n  bad: : yaml\n---\n\nBody.\n"

        with pytest.raises(ValueError, match="Error reading profile"):
            validate_profile_text(text)

    def test_body_only_text_validates_as_empty_frontmatter(self) -> None:
        """Markdown with no frontmatter block is empty metadata, not an error."""
        findings = validate_profile_text("Just a body, no frontmatter.\n")

        assert any(f.severity == "error" and "name" in f.message for f in findings)


class TestCaoNativeFields:
    """Tests for the CAO-native ``container`` and ``provider_init_timeout`` fields.

    Both are documented in ``docs/agent-profile.md`` and read at runtime by
    ``providers/base.py``, but were absent from the schema, so
    ``additionalProperties: false`` rejected them as unknown keys. A profile
    following the documented format therefore failed its own validator.
    """

    def test_documented_container_and_timeout_example_is_valid(self) -> None:
        """The worked example from docs/agent-profile.md must validate cleanly.

        This is the regression guard: the schema and the documented profile
        format have to agree, or the validator rejects profiles CAO itself
        tells users to write.
        """
        metadata = {
            "name": "containerized-agent",
            "container": {
                "path_maps": [
                    {
                        "host": "/home/user/.aws/cli-agent-orchestrator/tmp",
                        "guest": "/workspace/cao-tmp",
                    }
                ]
            },
            "provider_init_timeout": 180,
        }

        assert validate_frontmatter(metadata) == []

    def test_path_map_requires_both_host_and_guest(self) -> None:
        """A half-specified mapping cannot be applied, so it is an error."""
        metadata = {"name": "agent", "container": {"path_maps": [{"host": "/a"}]}}

        findings = validate_frontmatter(metadata)
        assert any(f.severity == "error" and "guest" in f.message for f in findings)

    def test_nested_error_path_is_dotted_and_indexed(self) -> None:
        """Clients render errors against fields, so nested paths must be precise.

        A bare ``container`` path would be useless for a form with one input per
        mapping; the index identifies which row is wrong.
        """
        metadata = {
            "name": "agent",
            "container": {"path_maps": [{"host": "", "guest": "/g"}]},
        }

        findings = validate_frontmatter(metadata)
        assert any(f.path == "container.path_maps.0.host" for f in findings)

    def test_provider_init_timeout_must_be_an_integer(self) -> None:
        """YAML quoting mistakes are the common failure here."""
        findings = validate_frontmatter({"name": "agent", "provider_init_timeout": "180"})

        assert any(f.severity == "error" and f.path == "provider_init_timeout" for f in findings)

    def test_provider_init_timeout_rejects_non_positive(self) -> None:
        """The value is used directly as a timeout, so 0 means instant failure.

        ``providers/base.py`` returns this verbatim in place of the server
        default rather than treating a falsy value as "unset" or "no limit".
        """
        findings = validate_frontmatter({"name": "agent", "provider_init_timeout": 0})

        assert any(f.severity == "error" and f.path == "provider_init_timeout" for f in findings)

    def test_unknown_top_level_key_is_still_rejected(self) -> None:
        """Widening the schema must not weaken typo detection."""
        findings = validate_frontmatter({"name": "agent", "provider_init_timeoutt": 180})

        assert any(f.severity == "error" for f in findings)


class TestSchemaModelParity:
    """Guards the schema against the AgentProfile model drifting away from it.

    ``GET /agents/profiles/schema`` invites clients to build create and edit
    forms from the served schema. A field the model accepts but the schema
    omits is therefore invisible to those clients *and* rejected by the
    validator, which is how ``container`` and ``provider_init_timeout`` came to
    be documented, functional, and unvalidatable at the same time.
    """

    # Model fields that are deliberately not frontmatter keys.
    #
    # ``system_prompt`` is assigned from the Markdown body rather than read
    # from frontmatter (see ``parse_agent_profile_text``), so it must not
    # appear in a schema that validates the frontmatter block.
    _NOT_FRONTMATTER = {"system_prompt"}

    def test_every_model_field_is_a_schema_property(self) -> None:
        expected = set(AgentProfile.model_fields) - self._NOT_FRONTMATTER
        missing = expected - set(load_profile_schema()["properties"])

        assert not missing, (
            f"AgentProfile accepts {sorted(missing)} but the schema omits them, so "
            "additionalProperties:false will reject valid profiles and "
            "schema-driven forms will not offer the fields."
        )

    def test_every_schema_property_is_a_model_field(self) -> None:
        """The reverse direction: the schema must not advertise dead fields."""
        extra = set(load_profile_schema()["properties"]) - set(AgentProfile.model_fields)

        assert not extra, (
            f"The schema declares {sorted(extra)} but AgentProfile has no such "
            "field, so a client filling them in would have them silently dropped."
        )
