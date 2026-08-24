"""Tests for profile frontmatter validation as a shared service.

Covers the structured contract that both ``cao profile validate`` and
``POST /agents/profiles/validate`` sit on top of. The CLI's rendered
``[error]`` / ``[warn]`` string form is covered separately in
``test/cli/test_profile_cmd.py``, which is deliberately left unchanged so it
also serves as the no-behaviour-change guard for the extraction.

Ref: https://github.com/awslabs/cli-agent-orchestrator/issues/510
"""

import pytest

import cli_agent_orchestrator.services.profile_validator as profile_validator
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.profile_validator import (
    _MAX_FINDING_CHARS,
    _MAX_FINDING_PATH_BYTES,
    _MAX_FINDING_TEXT_BYTES,
    _MAX_FINDINGS,
    _MAX_RENDERED_BYTES,
    _OMISSION_MESSAGE,
    ValidationMessage,
    _capped,
    _FindingCollector,
    load_profile_schema,
    validate_frontmatter,
    validate_profile_text,
)


class TestFindingMessageCap:
    """Schema finding text, including its marker, must fit the declared cap."""

    def test_truncation_suffix_counts_toward_the_cap(self) -> None:
        message = "x" * (_MAX_FINDING_CHARS + 1)

        capped = _capped(message)

        assert len(capped) == _MAX_FINDING_CHARS
        assert capped.endswith(f"... (message truncated, {len(message)} chars)")


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


def _alias_amplified_yaml(levels: int, leaf: str = "{k: v}", tail: str = "") -> str:
    """A profile whose *expanded* value count is exponential in ``levels``.

    Each anchor references the previous one twice, so ``yaml.safe_load`` returns
    ``levels + 1`` dicts while a full expansion of them contains ~2**levels
    values. Nested under ``toolsSettings``/``hooks`` because those fields are
    free-form objects, which keeps the document otherwise *valid*: a document
    rejected on its own merits would never reach the expensive steps anyway.

    ``tail`` appends a final line, used to plant a schema error whose offending
    instance is the amplified node.
    """
    lines = ["---", "name: bomb", "description: A profile.", "hooks:", f"  a0: &a0 {leaf}"]
    for level in range(1, levels + 1):
        lines.append(f"  a{level}: &a{level} {{x: *a{level - 1}, y: *a{level - 1}}}")
    if tail:
        lines.append(tail)
    return "\n".join(lines) + "\n---\n\nBody.\n"


class TestAliasAmplificationIsBounded:
    """A YAML-anchor bomb must not reach anything that pays for its expansion.

    Two rounds of review on #585 landed here. Round 2 added a non-string mapping
    key check whose only bound was a recursion depth cap, which bounded the wrong
    dimension: aliases resolve to repeated references to the *same* object, so the
    walk revisited shared subtrees exponentially while the document stayed tiny. A
    640-byte body took ~1s, doubling per anchor level. Reported by @haofeif.

    Further testing found the larger half. jsonschema builds each error
    message eagerly, interpolating ``repr`` of the offending instance, so an
    amplified value that trips one ``type`` error produced a 25 MB message at 20
    levels and 101 MB at 22, which is an allocation ceiling rather than a stall and
    was reachable on merged ``main`` independently of this PR.

    Both are now closed ahead of either step, by rejecting a document whose
    expansion exceeds a ceiling. Every assertion below is deterministic: a
    regression fails on a count or a length rather than hanging until CI's job
    timeout, which is what the earlier timing-only assertions would have done.
    """

    def test_an_anchor_bomb_is_rejected_rather_than_traversed(self) -> None:
        document = _alias_amplified_yaml(40)
        assert len(document) < 1500  # the whole point: tiny input, huge expansion

        findings = validate_profile_text(document)
        errors = [f for f in findings if f.severity == "error"]

        assert len(errors) == 1
        assert "renders to more than" in errors[0].message

    def test_the_rejection_does_not_grow_with_the_bomb(self) -> None:
        """Rejecting must not itself render the document.

        This is the regression guard for the jsonschema message vector: the
        offending instance below is the amplified node, so before the ceiling
        existed the returned message *was* its full ``repr``. Asserting a bound on
        the response size catches that without measuring time.
        """
        for levels in (20, 22, 30):
            document = _alias_amplified_yaml(levels, tail="toolsSettings: [*a%d]" % levels)

            findings = validate_profile_text(document)

            assert len(findings) == 1, levels
            assert len(findings[0].message) < 1000, (
                f"{levels} levels produced a {len(findings[0].message)}-char message; "
                f"the offending instance is being rendered"
            )

    CYCLIC = "---\nname: cyc\ndescription: cyclic\ntoolsSettings: &c {self: *c}\n---\n\nB.\n"

    def test_a_cyclic_document_is_rejected(self) -> None:
        """A cycle is not merely small, it is unrenderable.

        The first version of this guard gave a back-edge a provisional size of 1,
        which made a cycle look finite, and an earlier revision of this test
        asserted the resulting document was *valid*. Reported by @haofeif.
        """
        findings = validate_profile_text(self.CYCLIC)
        errors = [f for f in findings if f.severity == "error"]

        assert len(errors) == 1
        assert "circular" in errors[0].message

    @pytest.mark.parametrize(
        "frontmatter",
        [
            "hooks:\n  a: &a {b: {c: {d: *a}}}",
            "hooks: &a [*a]",
            "hooks: &a [{inner: *a}]",
        ],
        ids=["indirect through mappings", "sequence self-reference", "sequence to mapping"],
    )
    def test_a_cycle_is_rejected_whatever_shape_it_takes(self, frontmatter: str) -> None:
        """The back-edge need not be a top-level self-reference in a mapping.

        Only the mapping form was reported. Tracking in-progress identities catches
        any of these, and pinning the shapes keeps a later refactor from narrowing
        the check to the one case that was raised.
        """
        document = f"---\nname: c\ndescription: d\n{frontmatter}\n---\n\nB.\n"

        errors = [f for f in validate_profile_text(document) if f.severity == "error"]

        assert len(errors) == 1
        assert "circular" in errors[0].message

    def test_merge_keys_cannot_amplify_either(self) -> None:
        """``<<`` is a second alias mechanism, and renders its target in each copy.

        Not reported, found while probing the fix. A merge key copies the target's
        entries into the merging mapping, so the values are shared but rendered
        again per copy, which is the same content multiplication as an aliased
        scalar reached by a different route.
        """
        blob = "y" * 60_000
        copies = "\n".join(f"  d{index}: {{<<: *s}}" for index in range(3_000))
        document = (
            f"---\nname: t\ndescription: d\nhooks:\n  s: &s {{k: {blob}}}\n"
            f"{copies}\n---\n\nB.\n"
        )

        errors = [f for f in validate_profile_text(document) if f.severity == "error"]

        assert len(errors) == 1
        assert "renders to more than" in errors[0].message

    def test_ordinary_merge_keys_still_pass(self) -> None:
        """``<<`` is also a normal YAML convenience and must not be rejected."""
        document = (
            "---\nname: t\ndescription: d\nhooks:\n  base: &b {timeout: 30}\n"
            "  a: {<<: *b}\n  b: {<<: *b}\n---\n\nB.\n"
        )

        assert [f for f in validate_profile_text(document) if f.severity == "error"] == []

    def test_the_rejected_cycle_is_indeed_unusable(self) -> None:
        """Anchors the reason for rejecting, so the rule is not arbitrary.

        A cyclic profile parses, so nothing before this guard objects, but the Kiro
        materialization path serializes ``toolsSettings`` and Pydantic refuses. The
        write gate accepting it would persist a profile the runtime cannot install,
        which is the same failure mode the non-string key rule exists for.
        """
        import pytest as _pytest

        from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
        from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text

        profile = parse_agent_profile_text(self.CYCLIC, "cyc")
        config = KiroAgentConfig(
            name="cyc", description="cyclic", toolsSettings=profile.toolsSettings
        )

        with _pytest.raises(Exception) as excinfo:
            config.model_dump_json(indent=2, exclude_none=True)

        assert "Circular reference" in str(excinfo.value)

    @pytest.mark.parametrize(
        "scalar_length, alias_count",
        [(2_048, 5_000), (190_000, 15_000)],
        ids=["2KB scalar x5000", "190KB scalar x15000"],
    )
    def test_an_aliased_scalar_cannot_amplify_the_response(
        self, scalar_length: int, alias_count: int
    ) -> None:
        """Counting occurrences missed this; counting rendered bytes catches it.

        Every scalar used to contribute 1 regardless of length, so a single large
        scalar referenced thousands of times passed the ceiling while the one
        jsonschema instance rendering ran to megabytes, and at the larger size to a
        2.85 GB lower bound from a request under the 256 KB cap. Both cases here are
        @haofeif's, and the assertion is on the response size rather than a clock.
        """
        scalar = "x" * scalar_length
        aliases = ", ".join(["*s"] * alias_count)
        document = (
            f"---\nname: t\ndescription: d\nhooks:\n  s: &s {scalar}\n"
            f"toolsSettings: [{aliases}]\n---\n\nB.\n"
        )

        findings = validate_profile_text(document)

        assert len(findings) == 1
        assert "renders to more than" in findings[0].message
        assert len(findings[0].message) < 1000, (
            f"a {len(document)}-byte request produced a " f"{len(findings[0].message)}-char message"
        )

    def test_a_bad_key_in_a_shared_subtree_is_reported_exactly_once(self) -> None:
        """Deterministic proof of the memoization, with no reliance on a clock.

        Ten levels is 1024 paths to the single node every alias resolves to, and
        expands to well under the ceiling so the walk still runs. One finding
        confirms shared nodes are visited once; the unmemoized walk emitted 1024
        copies of it.
        """
        document = _alias_amplified_yaml(10, leaf="{1: one}")

        findings = validate_profile_text(document)
        key_errors = [f for f in findings if "not a string" in f.message]

        assert len(key_errors) == 1
        assert key_errors[0].severity == "error"
        assert key_errors[0].path == "hooks.a0.1"

    def test_legitimate_anchor_reuse_still_validates_clean(self) -> None:
        """Anchors are a normal YAML convenience, not inherently suspect.

        The ceiling is on expansion, not on aliasing, so ordinary reuse has to
        pass. Without this, satisfying the bound by rejecting anchors outright
        would look like a fix.
        """
        document = (
            "---\nname: shared\ndescription: A profile.\ntoolsSettings:\n"
            "  common: &common {timeout: 30}\n  fs: *common\n  web: *common\n---\n\nBody.\n"
        )

        assert validate_profile_text(document) == []

    def test_exceeding_a_ceiling_is_an_error_not_silence(self) -> None:
        """A document past a ceiling is rejected, not called valid.

        Reporting nothing would present an uninspected document as clean, which is
        the failure mode of the depth cap this replaced: it returned an empty list.

        The oversized case is a document that is simply large rather than aliased,
        which is why it has to exceed a megabyte to trip the ceiling. Over HTTP the
        256 KB cap on ``content`` gets there first, so the reachable caller for this
        branch is ``cao profile validate`` on a local file, which has no such cap.
        """
        deep: dict = {"name": "deep", "description": "A profile."}
        node = deep
        for _ in range(70):
            node["toolsSettings"] = {}
            node = node["toolsSettings"]
        oversized = {
            "name": "big",
            "description": "A profile.",
            "toolsSettings": {"blob": "x" * (_MAX_RENDERED_BYTES + 1)},
        }

        for metadata, expected in ((deep, "nests more than"), (oversized, "renders to more")):
            errors = [f for f in validate_frontmatter(metadata) if f.severity == "error"]
            assert len(errors) == 1
            assert expected in errors[0].message

    def test_a_large_but_unaliased_document_still_passes(self) -> None:
        """The ceiling is on rendering, so size alone below it must not reject.

        Guards against tightening the bound into something that rejects ordinary
        large profiles, which is the opposite failure from the one above.
        """
        metadata = {
            "name": "big",
            "description": "A profile.",
            "toolsSettings": {f"k{index}": "v" * 20 for index in range(2_000)},
        }
        assert len(repr(metadata)) > 50_000  # genuinely large, still under the ceiling

        assert [f for f in validate_frontmatter(metadata) if f.severity == "error"] == []


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


class TestAggregateFindingBudget:
    """Every producer shares one bounded response budget."""

    @staticmethod
    def _text_bytes(findings: list[ValidationMessage]) -> int:
        return sum(
            len(finding.message.encode("utf-8"))
            + len(finding.path.encode("utf-8") if finding.path else b"")
            for finding in findings
        )

    def test_non_string_keys_stop_at_one_error_marker(self) -> None:
        metadata = {"name": "agent", **{index: "value" for index in range(200)}}

        findings = validate_frontmatter(metadata)

        assert len(findings) == _MAX_FINDINGS
        assert sum(f.message == _OMISSION_MESSAGE for f in findings) == 1
        assert findings[-1] == ValidationMessage("error", _OMISSION_MESSAGE)

    def test_unknown_tools_keep_warning_only_result_advisory(self) -> None:
        findings = validate_frontmatter(
            {"name": "agent", "allowedTools": [f"unknown-{index}" for index in range(200)]}
        )

        assert len(findings) == _MAX_FINDINGS
        assert sum(f.message == _OMISSION_MESSAGE for f in findings) == 1
        assert findings[-1] == ValidationMessage("warning", _OMISSION_MESSAGE)
        assert not any(f.severity == "error" for f in findings)

    def test_aggregate_text_and_each_path_stay_within_byte_limits(self) -> None:
        collector = _FindingCollector()
        long_unicode = "界" * 2_000
        for index in range(200):
            if not collector.add(
                ValidationMessage("error", long_unicode, f"field.{long_unicode}.{index}")
            ):
                break

        findings = collector.finalize()

        assert len(findings) <= _MAX_FINDINGS
        assert self._text_bytes(findings) <= _MAX_FINDING_TEXT_BYTES
        assert all(
            finding.path is None or len(finding.path.encode("utf-8")) <= _MAX_FINDING_PATH_BYTES
            for finding in findings
        )

    def test_schema_iterator_consumes_only_remaining_plus_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        consumed = 0

        class FakeError:
            def __init__(self, index: int) -> None:
                self.path = [f"field{index}"]
                self.absolute_path = self.path
                self.message = f"error {index}"

        class FakeValidator:
            def __init__(self, schema: dict) -> None:
                del schema

            def iter_errors(self, metadata: dict):
                nonlocal consumed
                del metadata
                for index in range(200):
                    consumed += 1
                    yield FakeError(index)

        monkeypatch.setattr(profile_validator, "Draft202012Validator", FakeValidator)
        metadata = {"name": "agent", **{index: "value" for index in range(10)}}

        findings = validate_frontmatter(metadata)

        remaining_after_key_findings = (_MAX_FINDINGS - 1) - 10
        assert consumed == remaining_after_key_findings + 1
        assert len(findings) == _MAX_FINDINGS
        assert findings[-1] == ValidationMessage("error", _OMISSION_MESSAGE)

    def test_ordered_additional_properties_is_lazy_with_real_validator(self) -> None:
        """The real keyword handler must not inspect the omitted tail."""
        from itertools import islice

        class CountingDict(dict):
            def __init__(self) -> None:
                super().__init__((f"srv{index:04}", {}) for index in range(300))
                self.visited = 0

            def items(self):
                for item in super().items():
                    self.visited += 1
                    yield item

        servers = CountingDict()
        validator = profile_validator.Draft202012Validator(load_profile_schema())

        errors = list(
            islice(
                validator.iter_errors({"name": "agent", "mcpServers": servers}),
                _MAX_FINDINGS,
            )
        )

        assert len(errors) == _MAX_FINDINGS
        assert servers.visited == _MAX_FINDINGS

    def test_schema_prefix_is_stable_across_hash_seeds(self) -> None:
        """Truncation must select the same document-order prefix in every worker."""
        import json
        import os
        import subprocess
        import sys

        script = """
import json
from cli_agent_orchestrator.services.profile_validator import validate_frontmatter

metadata = {
    "name": "agent",
    "mcpServers": {f"srv{index:04}": {} for index in range(299, -1, -1)},
}
findings = validate_frontmatter(metadata)
print(json.dumps([
    {"severity": finding.severity, "message": finding.message, "path": finding.path}
    for finding in findings
]))
"""
        outputs = []
        for seed in ("0", "1", "2", "3"):
            environment = os.environ.copy()
            environment["PYTHONHASHSEED"] = seed
            completed = subprocess.run(
                [sys.executable, "-c", script],
                check=True,
                capture_output=True,
                text=True,
                env=environment,
            )
            outputs.append(completed.stdout)

        assert len(set(outputs)) == 1
        findings = json.loads(outputs[0])
        selected_document_prefix = [
            f"mcpServers.srv{index:04}" for index in range(299, 299 - (_MAX_FINDINGS - 1), -1)
        ]
        assert [finding["path"] for finding in findings[:-1]] == sorted(selected_document_prefix)
        assert findings[-1] == {
            "severity": "error",
            "message": _OMISSION_MESSAGE,
            "path": None,
        }
