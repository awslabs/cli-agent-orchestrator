"""Tests for profile frontmatter validation as a shared service.

Covers the structured contract that both ``cao profile validate`` and
``POST /agents/profiles/validate`` sit on top of. The CLI's rendered
``[error]`` / ``[warn]`` string form is covered separately in
``test/cli/test_profile_cmd.py``, which is deliberately left unchanged so it
also serves as the no-behaviour-change guard for the extraction.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/510
"""

import time

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


class TestMalformedButParseableInput:
    """Schema-invalid values must be *reported*, never raise.

    Regression guard for the P3 finding on #575. The advisory checks test set
    membership, which hashes the value, so an unhashable one (a list) raised
    ``TypeError``; and the schema-error sort key used raw path components, so
    mixed-type mapping keys could not be ordered. Both escaped the endpoint's
    ``except ValueError`` and surfaced as HTTP 500 from a route whose entire
    purpose is reporting what is wrong with a document.

    Every case below is syntactically valid YAML that the schema already rejects,
    so the correct outcome is an error finding rather than an exception.
    """

    def test_unhashable_allowed_tools_entry_is_reported(self) -> None:
        findings = validate_frontmatter({"name": "x", "allowedTools": [["Read"]]})

        assert any(f.severity == "error" for f in findings)

    def test_unhashable_role_is_reported(self) -> None:
        findings = validate_frontmatter({"name": "x", "role": ["developer"]})

        assert any(f.severity == "error" for f in findings)

    def test_mixed_type_mapping_keys_are_reported(self) -> None:
        """Path components of different types must not break the error sort."""
        findings = validate_frontmatter({"name": "x", "mcpServers": {1: {}, "x": {}}})

        assert any(f.severity == "error" for f in findings)

    def test_non_string_role_does_not_produce_a_spurious_warning(self) -> None:
        """The advisory role check stands aside; the schema owns the type error."""
        findings = validate_frontmatter({"name": "x", "role": 7})

        assert any(f.severity == "error" for f in findings)
        assert not any(f.severity == "warning" for f in findings)

    def test_non_string_allowed_tool_does_not_produce_a_spurious_warning(self) -> None:
        findings = validate_frontmatter({"name": "x", "allowedTools": [{"a": 1}]})

        assert any(f.severity == "error" for f in findings)
        assert not any(f.severity == "warning" for f in findings)

    def test_well_formed_values_still_warn(self) -> None:
        """The type guards must not silence the checks they protect."""
        tool_findings = validate_frontmatter({"name": "x", "allowedTools": ["not_a_real_tool"]})
        role_findings = validate_frontmatter({"name": "x", "role": "archaeologist"})

        assert any(f.severity == "warning" for f in tool_findings)
        assert any(f.severity == "warning" for f in role_findings)


def _alias_amplified_yaml(levels: int, leaf: str = "{k: v}") -> str:
    """A schema-valid profile whose value graph is 2**``levels`` paths.

    Each anchor references the previous one twice, so ``yaml.safe_load`` returns
    ``levels + 1`` dicts while an unmemoized walk sees an exponential number of
    paths through them. Nested under ``toolsSettings`` because that field is a
    free-form object, which keeps the document *valid* -- the point being that a
    rejected document would never reach a full traversal anyway.
    """
    lines = ["---", "name: bomb", "description: A profile.", "toolsSettings:", f"  a0: &a0 {leaf}"]
    for level in range(1, levels + 1):
        lines.append(f"  a{level}: &a{level} {{x: *a{level - 1}, y: *a{level - 1}}}")
    return "\n".join(lines) + "\n---\n\nBody.\n"


class TestAliasAmplificationIsBounded:
    """A YAML-anchor bomb must not stall the key walk.

    Round 2 of review on #585 added a non-string mapping key check whose only
    bound was a recursion depth cap. That bounded the wrong dimension: YAML
    aliases resolve to repeated references to the *same* object, so the walk
    revisited shared subtrees exponentially while the document stayed tiny. A
    640-byte, schema-valid body took ~1s, doubling per added anchor level, on a
    scope-exempt ``async`` route -- a denial of service reachable without
    credentials. Reported by @haofeif.

    The fix skips containers already walked, keyed on identity, so these tests
    pin both halves of that: the traversal terminates, and skipping repeats does
    not lose a finding.
    """

    def test_a_forty_level_bomb_validates_promptly(self) -> None:
        """Forty levels is 2**40 paths: unbounded, this never returns."""
        document = _alias_amplified_yaml(40)
        assert len(document) < 1500  # the whole point: tiny input, huge graph

        started = time.perf_counter()
        findings = validate_profile_text(document)
        elapsed = time.perf_counter() - started

        assert findings == []
        # Measured at ~0.0001s. The bound is loose enough to survive a loaded
        # CI runner while still being unreachable for an exponential walk.
        assert elapsed < 5.0, f"walk took {elapsed:.2f}s; the traversal bound is not holding"

    def test_a_self_referential_document_terminates(self) -> None:
        """An anchor that contains itself is a cycle, not merely deep nesting."""
        document = (
            "---\nname: cyc\ndescription: A profile.\ntoolsSettings: &c {self: *c}\n---\n\nB.\n"
        )

        started = time.perf_counter()
        findings = validate_profile_text(document)

        assert findings == []
        assert time.perf_counter() - started < 5.0

    def test_a_bad_key_in_a_shared_subtree_is_reported_exactly_once(self) -> None:
        """Deterministic proof of the memoization, with no reliance on a clock.

        The offending key sits in the one node every alias resolves to. Reported
        once, it confirms shared nodes are visited once; the pre-fix walk would
        have emitted 2**20 copies of the same finding.
        """
        document = _alias_amplified_yaml(20, leaf="{1: one}")

        findings = validate_profile_text(document)
        key_errors = [f for f in findings if "not a string" in f.message]

        assert len(key_errors) == 1
        assert key_errors[0].severity == "error"
        assert key_errors[0].path == "toolsSettings.a0.1"

    def test_legitimate_anchor_reuse_still_validates_clean(self) -> None:
        """Anchors are a normal YAML convenience, not inherently suspect."""
        document = (
            "---\nname: shared\ndescription: A profile.\ntoolsSettings:\n"
            "  common: &common {timeout: 30}\n  fs: *common\n  web: *common\n---\n\nBody.\n"
        )

        findings = validate_profile_text(document)

        assert findings == []

    def test_exceeding_a_bound_is_an_error_not_silence(self) -> None:
        """A document too large to traverse is rejected, not called valid.

        Identity memoization bounds an *aliased* document; these bounds cover one
        that is merely enormous. Returning no findings there would report an
        unchecked document as clean, which is the failure mode being avoided.
        """
        deep: dict = {"name": "deep", "description": "A profile."}
        node = deep
        for _ in range(70):
            node["toolsSettings"] = {}
            node = node["toolsSettings"]
        wide = {
            "name": "wide",
            "description": "A profile.",
            "toolsSettings": {f"k{index}": index for index in range(25_000)},
        }

        for metadata, expected in ((deep, "nested more than"), (wide, "holds more than")):
            errors = [f for f in validate_frontmatter(metadata) if f.severity == "error"]
            assert len(errors) == 1
            assert expected in errors[0].message


class TestMcpServerTransports:
    """``mcpServers`` entries may be command-launched *or* url-based.

    The schema required ``command`` unconditionally, which made the write routes
    reject a form CAO supports: ``resolve_mcp_server_config`` documents entries
    without a ``command`` (``{"type": "http", "url": ...}``) as passing through
    untouched, and providers forward them to their own MCP config. Because
    #585 made this schema the blocking gate in front of persistence, a latent
    description gap became a broken save path. Reported by @haofeif.
    """

    ACCEPTED = {
        "http url": {"docs": {"type": "http", "url": "https://example.test/mcp"}},
        "sse url": {"docs": {"type": "sse", "url": "https://example.test/sse"}},
        "url with headers": {
            "docs": {"type": "http", "url": "https://example.test/mcp", "headers": {"A": "b"}}
        },
        "command": {"fs": {"command": "npx", "args": ["-y", "server"]}},
        "bundled cao server": {"cao-mcp-server": {"command": "cao-mcp-server", "args": []}},
        "command and url together": {"z": {"command": "npx", "url": "https://example.test/mcp"}},
    }

    @pytest.mark.parametrize("label", sorted(ACCEPTED))
    def test_supported_forms_validate(self, label: str) -> None:
        findings = validate_frontmatter(
            {"name": "x", "description": "d", "mcpServers": self.ACCEPTED[label]}
        )

        assert [f for f in findings if f.severity == "error"] == []

    @pytest.mark.parametrize(
        "entry", [{"type": "http"}, {}, {"args": ["-y"]}], ids=["type only", "empty", "args only"]
    )
    def test_an_entry_with_neither_command_nor_url_is_rejected(self, entry: dict) -> None:
        """Widening the rule must not widen it into accepting anything.

        An entry naming no transport cannot be launched or reached, so the gate
        still has to catch it -- the fix is a second permitted shape, not the
        removal of the requirement.
        """
        findings = validate_frontmatter(
            {"name": "x", "description": "d", "mcpServers": {"broken": entry}}
        )
        errors = [f for f in findings if f.severity == "error"]

        assert len(errors) == 1
        assert errors[0].path == "mcpServers.broken"

    def test_url_is_described_rather_than_merely_tolerated(self) -> None:
        """The field is typed, so a form generator can render it and catch a typo.

        The inner object does not set ``additionalProperties: false``, so a url
        entry would pass even with no ``url`` property declared. Declaring it is
        what makes ``GET /agents/profiles/schema`` describe the shape, and what
        makes a wrong type a finding.
        """
        inner = load_profile_schema()["properties"]["mcpServers"]["additionalProperties"]
        assert inner["properties"]["url"] == {"type": "string"}
        assert inner["anyOf"] == [{"required": ["command"]}, {"required": ["url"]}]

        findings = validate_frontmatter(
            {"name": "x", "description": "d", "mcpServers": {"docs": {"url": 7}}}
        )

        assert any(f.severity == "error" and f.path == "mcpServers.docs.url" for f in findings)
