"""Validator tests — correctness properties P1 (totality), P2 (fatality), P7 (siblings).

The single most important thing asserted here is that ``validate_plugin`` is
**total**: it returns a report for every input, including inputs designed to
break it. Five callers depend on that (CLI, API, web panel, installer, CI) and
three of them must render partial success.
"""

from __future__ import annotations

import json
import os
import stat

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from cli_agent_orchestrator.agent_plugins.models import PluginValidationReport, Severity
from cli_agent_orchestrator.agent_plugins.validation import (
    PLUGIN_SCHEMA_FILENAME,
    supported_schema_id,
    validate_plugin,
)

from .conftest import CANONICAL_EXAMPLE_DIR, PLUGIN_SCHEMA_ID, build_plugin, write_skill


def codes(report: PluginValidationReport) -> list:
    return [finding.code for finding in report.findings]


def severities(report: PluginValidationReport, code: str) -> list:
    return [f.severity for f in report.findings if f.code == code]


class TestHappyPath:
    def test_minimal_plugin_is_loadable(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo")
        report = validate_plugin(root)
        assert report.loadable
        assert report.manifest is not None and report.manifest.name == "demo"
        assert report.skills == ()
        assert report.mcp_present is False

    def test_skills_are_discovered(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        report = validate_plugin(root)
        assert report.loadable
        assert report.skill_names == ("alpha", "beta")

    def test_canonical_example_package_validates_clean(self):
        """Requirement 23.4: the upstream example is a known-good fixture."""
        report = validate_plugin(CANONICAL_EXAMPLE_DIR)
        assert report.loadable, codes(report)
        assert report.findings == ()
        assert "migrate-agent-plugin" in report.skill_names

    def test_pinned_schema_id_matches_the_specification(self):
        assert supported_schema_id(PLUGIN_SCHEMA_FILENAME) == PLUGIN_SCHEMA_ID


class TestManifestFatality:
    """Requirement 6 / property P2 — what is tolerated and what rejects."""

    def test_missing_manifest_is_fatal(self, tmp_path):
        root = tmp_path / "p"
        (root / "skills" / "alpha").mkdir(parents=True)
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.missing" in codes(report)
        assert report.skills == ()  # nothing loads once the manifest is fatal

    def test_invalid_json_is_fatal(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", manifest_text="{not json")
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.invalid_json" in codes(report)

    def test_non_object_manifest_is_fatal(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", manifest_text="[1, 2, 3]")
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.not_an_object" in codes(report)

    def test_missing_schema_field_is_fatal(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", schema_id=None)
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.schema_missing" in codes(report)

    def test_unrecognized_schema_version_is_fatal_and_names_the_pin(self, tmp_path):
        root = build_plugin(
            tmp_path / "p",
            "demo",
            schema_id="https://agent-plugins.org/schemas/9.9.9/plugin.schema.json",
        )
        report = validate_plugin(root)
        assert not report.loadable
        finding = next(f for f in report.findings if f.code == "manifest.schema_unsupported")
        assert finding.spec_ref == "§5.2"
        assert PLUGIN_SCHEMA_ID in finding.message

    def test_missing_name_is_fatal(self, tmp_path):
        root = tmp_path / "p"
        root.mkdir(parents=True)
        (root / "plugin.json").write_text(json.dumps({"$schema": PLUGIN_SCHEMA_ID}))
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.invalid" in codes(report)

    @pytest.mark.parametrize(
        "bad_name",
        [
            "Uppercase",  # §5.5: lowercase only
            "-leading",  # must start alphanumeric
            "trailing-",  # must end alphanumeric
            "double--hyphen",  # no `--`
            "double..dot",  # no `..`
            "has space",
            "a" * 65,  # maxLength 64
            "",  # minLength 1
        ],
    )
    def test_name_constraint_violations_are_fatal(self, tmp_path, bad_name):
        root = build_plugin(tmp_path / "p", bad_name, skills=["alpha"])
        report = validate_plugin(root)
        assert not report.loadable, bad_name
        assert "manifest.name_invalid" in codes(report)
        assert report.skills == ()  # zero components discovered on a fatal manifest

    def test_name_finding_cites_the_name_clause(self, tmp_path):
        root = build_plugin(tmp_path / "p", "Bad Name")
        report = validate_plugin(root)
        assert severities(report, "manifest.name_invalid") == [Severity.FATAL]
        assert all(
            f.spec_ref == "§5.5" for f in report.findings if f.code == "manifest.name_invalid"
        )


class TestManifestTolerances:
    """The two — and only two — non-fatal manifest deviations."""

    def test_unknown_top_level_field_is_a_warning_and_the_plugin_loads(self, tmp_path):
        root = build_plugin(
            tmp_path / "p", "demo", skills=["alpha"], extra_manifest={"hooks": {"pre": "x"}}
        )
        report = validate_plugin(root)
        assert report.loadable
        assert severities(report, "manifest.unknown_field") == [Severity.WARNING]
        assert report.skill_names == ("alpha",)  # ignored field, plugin still loads

    def test_non_object_extensions_is_a_warning_and_the_plugin_loads(self, tmp_path):
        root = build_plugin(
            tmp_path / "p", "demo", skills=["alpha"], extra_manifest={"extensions": "nope"}
        )
        report = validate_plugin(root)
        assert report.loadable
        assert severities(report, "manifest.extensions_not_object") == [Severity.WARNING]
        assert report.skill_names == ("alpha",)

    def test_unimplemented_extension_namespace_produces_no_finding_at_all(self, tmp_path):
        """§8.1: ignore an unimplemented namespace *without validating* it."""
        root = build_plugin(
            tmp_path / "p",
            "demo",
            extra_manifest={"extensions": {"com.example.client": {"anything": [1, 2]}}},
        )
        report = validate_plugin(root)
        assert report.loadable
        assert report.findings == ()

    def test_extension_namespace_contents_are_never_validated(self, tmp_path):
        """A namespace value the schema would reject must still not be validated.

        The pinned schema declares ``extensions`` values as objects. If CAO let
        the schema see them, a non-object namespace value would be fatal —
        which would be CAO validating the contents of a namespace it does not
        implement, exactly what §8.1 forbids.
        """
        root = build_plugin(
            tmp_path / "p", "demo", extra_manifest={"extensions": {"com.example": 42}}
        )
        report = validate_plugin(root)
        assert report.loadable
        assert report.findings == ()


class TestComponentDiscovery:
    def test_missing_skills_directory_is_not_an_error(self, tmp_path):
        """§6.2: a missing fixed component location is tolerated."""
        root = build_plugin(tmp_path / "p", "demo")
        report = validate_plugin(root)
        assert report.loadable
        assert report.findings == ()

    def test_skills_present_but_not_a_directory_skips_only_that_type(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", with_mcp=True)
        (root / "skills").write_text("not a directory", encoding="utf-8")
        report = validate_plugin(root)
        assert report.loadable
        assert severities(report, "skills.not_a_directory") == [Severity.SKIPPED]
        # MCP detection is unaffected by the skills component being invalid.
        assert report.mcp_present is True

    def test_skill_discovery_is_not_recursive(self, tmp_path):
        """§7.1: skills are *immediate* children of ``skills/``."""
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        write_skill(root / "skills" / "alpha" / "nested" / "buried", "buried")
        report = validate_plugin(root)
        assert report.skill_names == ("alpha",)

    def test_stray_file_under_skills_is_ignored_silently(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        (root / "skills" / "README.md").write_text("notes", encoding="utf-8")
        report = validate_plugin(root)
        assert report.skill_names == ("alpha",)
        assert report.findings == ()

    def test_skill_escaping_the_root_by_symlink_is_skipped(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        outside = tmp_path / "outside-skill"
        write_skill(outside, "escaped")
        (root / "skills" / "escaped").symlink_to(outside, target_is_directory=True)

        report = validate_plugin(root)
        assert report.loadable
        assert report.skill_names == ("alpha",)
        assert severities(report, "skill.escapes_root") == [Severity.SKIPPED]

    def test_skill_symlink_inside_the_root_is_permitted(self, tmp_path):
        """§4.1 permits a symlink whose target resolves within the root."""
        root = build_plugin(tmp_path / "p", "demo")
        (root / "skills").mkdir()
        real = root / "_sources" / "gamma"
        write_skill(real, "gamma")
        (root / "skills" / "gamma").symlink_to(real, target_is_directory=True)

        report = validate_plugin(root)
        assert report.loadable
        assert report.skill_names == ("gamma",)


class TestSiblingIndependence:
    """Property P7 — one broken skill never takes down its siblings."""

    def test_invalid_sibling_is_skipped_with_a_report(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha", "beta"])
        # Frontmatter name that disagrees with the folder name: invalid per the
        # Agent Skills specification, and unprojectable either way.
        (root / "skills" / "broken").mkdir()
        (root / "skills" / "broken" / "SKILL.md").write_text(
            "---\nname: something-else\ndescription: d\n---\n\nx\n", encoding="utf-8"
        )
        report = validate_plugin(root)
        assert report.loadable
        assert report.skill_names == ("alpha", "beta")
        assert severities(report, "skill.invalid") == [Severity.SKIPPED]

    def test_skill_directory_without_skill_md_is_skipped_with_a_report(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        (root / "skills" / "empty").mkdir()
        report = validate_plugin(root)
        assert report.skill_names == ("alpha",)
        assert severities(report, "skill.missing_skill_md") == [Severity.SKIPPED]

    def test_discovered_set_is_independent_of_directory_order(self, tmp_path):
        """Requirement 12.2, asserted by shuffling the on-disk creation order."""
        names = ["zulu", "alpha", "mike", "bravo"]
        root = build_plugin(tmp_path / "p", "demo")
        (root / "skills").mkdir()
        for name in names:
            write_skill(root / "skills" / name, name)

        first = validate_plugin(root).skill_names
        # Recreate in the opposite order into a fresh tree.
        other = build_plugin(tmp_path / "q", "demo")
        (other / "skills").mkdir()
        for name in reversed(names):
            write_skill(other / "skills" / name, name)

        assert first == validate_plugin(other).skill_names == tuple(sorted(names))

    @pytest.mark.parametrize("name", ["true", "false", "no", "on", "null", "y"])
    def test_a_yaml_boolean_shaped_skill_name_survives_the_fixture(self, tmp_path, name):
        """A skill named ``true`` or ``no`` is an ordinary, valid skill.

        YAML 1.1 implicit typing turns an unquoted ``name: true`` into a *bool*,
        which then compares unequal to the folder name and gets skipped. That is a
        hazard for a plugin author hand-writing ``SKILL.md``, and it was a hazard
        for this suite: ``write_skill`` used to interpolate the name unquoted, so
        any test that used one of these names would have exercised the
        skill-invalid path while claiming to build a valid skill.

        This is pinned as a test rather than only fixed in the fixture, because
        the failure mode is a *silently weaker* test elsewhere rather than a
        visible break here — the fixture could be un-fixed and every other test
        would still pass.
        """
        root = build_plugin(tmp_path / "p", "demo")
        (root / "skills").mkdir()
        write_skill(root / "skills" / name, name)

        report = validate_plugin(root)

        assert report.loadable
        assert report.skill_names == (name,)
        assert [f for f in report.findings if f.code == "skill.invalid"] == []


class TestMcpComponentDiscovery:
    """Increment 2: mcp.json is parsed, validated, and mapped.

    Whatever happens to it, **the plugin's skills are unaffected** — an unusable
    MCP configuration disables MCP for that plugin and nothing else. That is
    property P6's other half, and it is what keeps a skills-only consumer of a
    dual-component plugin working.
    """

    def test_a_valid_mcp_json_is_mapped(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"], with_mcp=True)
        report = validate_plugin(root)

        assert report.loadable
        assert report.mcp_present is True
        assert [server.name for server in report.mcp_servers] == ["demo"]
        assert report.skill_names == ("alpha",)

    def test_malformed_mcp_json_disables_mcp_but_not_skills(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"], mcp_text="}{ not json at all")
        report = validate_plugin(root)

        assert report.loadable  # not a manifest violation
        assert report.mcp_present is True
        assert report.mcp_servers == ()
        assert severities(report, "mcp.invalid_json") == [Severity.SKIPPED]
        assert report.skill_names == ("alpha",)

    def test_a_version_mismatched_mcp_json_disables_mcp(self, tmp_path):
        """§7.2.2.2 — the two documents must target the same spec version."""
        import json as _json

        root = build_plugin(
            tmp_path / "p",
            "demo",
            skills=["alpha"],
            mcp_text=_json.dumps(
                {
                    "$schema": "https://agent-plugins.org/schemas/9.9.9/mcp.schema.json",
                    "mcpServers": {},
                }
            ),
        )
        report = validate_plugin(root)

        assert report.loadable
        assert report.mcp_servers == ()
        assert "mcp.schema_unsupported" in codes(report)
        assert report.skill_names == ("alpha",)

    def test_mcp_json_that_is_a_directory_is_skipped(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        (root / "mcp.json").mkdir()
        report = validate_plugin(root)
        assert report.loadable
        assert report.mcp_present is False
        assert "mcp.not_a_file" in codes(report)


class TestTotalityOnHostileInputs:
    """Property P1 — concrete hostile cases; the fuzzed version is below."""

    def test_nonexistent_directory(self, tmp_path):
        report = validate_plugin(tmp_path / "absent")
        assert isinstance(report, PluginValidationReport)
        assert not report.loadable

    def test_a_file_instead_of_a_directory(self, tmp_path):
        target = tmp_path / "afile"
        target.write_text("x", encoding="utf-8")
        assert not validate_plugin(target).loadable

    def test_manifest_is_a_directory(self, tmp_path):
        root = tmp_path / "p"
        (root / "plugin.json").mkdir(parents=True)
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.not_a_file" in codes(report)

    def test_zero_byte_manifest(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", manifest_text="")
        assert not validate_plugin(root).loadable

    def test_non_utf8_manifest(self, tmp_path):
        root = tmp_path / "p"
        root.mkdir()
        (root / "plugin.json").write_bytes(b"\xff\xfe\x00invalid")
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.invalid_encoding" in codes(report)

    def test_manifest_symlinked_outside_the_root(self, tmp_path):
        root = tmp_path / "p"
        root.mkdir()
        outside = tmp_path / "elsewhere.json"
        outside.write_text(json.dumps({"$schema": PLUGIN_SCHEMA_ID, "name": "demo"}))
        (root / "plugin.json").symlink_to(outside)
        report = validate_plugin(root)
        assert not report.loadable
        assert "manifest.escapes_root" in codes(report)

    def test_symlink_loop_in_the_skills_tree(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        a = root / "skills" / "loop-a"
        b = root / "skills" / "loop-b"
        a.symlink_to(b)
        b.symlink_to(a)
        report = validate_plugin(root)
        assert report.loadable
        assert report.skill_names == ("alpha",)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses file mode restrictions")
    def test_unreadable_manifest_mode(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo")
        manifest = root / "plugin.json"
        manifest.chmod(0)
        try:
            report = validate_plugin(root)
            assert not report.loadable
            assert "manifest.unreadable" in codes(report)
        finally:
            manifest.chmod(stat.S_IRUSR | stat.S_IWUSR)

    @pytest.mark.skipif(os.geteuid() == 0, reason="root bypasses directory mode restrictions")
    def test_unreadable_skills_directory_skips_only_that_type(self, tmp_path):
        root = build_plugin(tmp_path / "p", "demo", skills=["alpha"])
        skills = root / "skills"
        skills.chmod(0)
        try:
            report = validate_plugin(root)
            assert report.loadable
            assert "skills.unreadable" in codes(report)
        finally:
            skills.chmod(stat.S_IRWXU)


# --- Property 1: Validation totality ---------------------------------------
# Validates: Requirements 5.1, 5.2, 5.3, 5.4

_MANIFEST_BYTES = st.one_of(
    st.binary(max_size=200),
    st.text(max_size=200).map(lambda s: s.encode("utf-8", errors="ignore")),
    st.builds(
        lambda payload: json.dumps(payload).encode("utf-8"),
        st.recursive(
            st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)),
            lambda children: st.one_of(
                st.lists(children, max_size=4),
                st.dictionaries(st.text(max_size=12), children, max_size=4),
            ),
            max_leaves=8,
        ),
    ),
)


@given(
    manifest=_MANIFEST_BYTES,
    extra_dirs=st.lists(st.sampled_from(["skills", "mcp.json", "x"]), max_size=3),
)
@settings(
    max_examples=250, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_validation_is_total(tmp_path_factory, manifest, extra_dirs):
    """``validate_plugin`` never raises, and ``loadable`` always tracks FATAL."""
    root = tmp_path_factory.mktemp("fuzz")
    (root / "plugin.json").write_bytes(manifest)
    for name in extra_dirs:
        target = root / name
        if not target.exists():
            target.mkdir()

    report = validate_plugin(root)

    assert isinstance(report, PluginValidationReport)
    assert report.loadable == (not any(f.severity is Severity.FATAL for f in report.findings))
    if not report.loadable:
        # A fatal violation rejects the plugin *before any component loads*.
        assert report.skills == ()
    assert all(isinstance(f.spec_ref, str) and f.spec_ref for f in report.findings)


# --- Property 2: Fatality classification ------------------------------------
# Validates: Requirements 6.1, 6.2, 6.3, 6.4

_TOLERATED = st.one_of(
    st.fixed_dictionaries({"hooks": st.just({"pre": "cmd"})}),
    st.fixed_dictionaries({"agents": st.just(["a"])}),
    st.fixed_dictionaries({"mcpServers": st.just({})}),
    st.fixed_dictionaries({"extensions": st.one_of(st.just("string"), st.just(7), st.just([]))}),
)

_FATAL_MUTATIONS = st.sampled_from(
    [
        {"name": "Bad Name"},
        {"name": 5},
        {"name": ""},
        {"version": 1},
        {"keywords": "not-a-list"},
        {"author": {"unexpected": "field"}},
        {"$schema": "https://example.invalid/other.schema.json"},
    ]
)


@given(tolerated=_TOLERATED)
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_tolerated_deviations_still_load(tmp_path_factory, tolerated):
    root = tmp_path_factory.mktemp("tolerated")
    build_plugin(root, "demo", skills=["alpha"], extra_manifest=dict(tolerated))
    report = validate_plugin(root)

    assert report.loadable
    assert report.skill_names == ("alpha",)  # components still load


@given(mutation=_FATAL_MUTATIONS)
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_other_violations_are_fatal_with_no_components(tmp_path_factory, mutation):
    root = tmp_path_factory.mktemp("fatal")
    build_plugin(root, "demo", skills=["alpha"], extra_manifest=dict(mutation))
    report = validate_plugin(root)

    assert not report.loadable
    assert report.skills == ()
    assert report.manifest is None


# --- Property 7: Sibling independence ---------------------------------------
# Validates: Requirements 12.1, 12.2


@given(
    valid=st.integers(min_value=0, max_value=5),
    invalid=st.integers(min_value=0, max_value=5),
)
@settings(
    max_examples=60, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
def test_property_sibling_independence(tmp_path_factory, valid, invalid):
    """N skill dirs of which k are invalid → exactly N−k discovered, k skipped."""
    root = tmp_path_factory.mktemp("siblings")
    build_plugin(root, "demo")
    (root / "skills").mkdir(exist_ok=True)

    for index in range(valid):
        write_skill(root / "skills" / f"good-{index}", f"good-{index}")
    for index in range(invalid):
        broken = root / "skills" / f"bad-{index}"
        broken.mkdir()
        (broken / "SKILL.md").write_text(
            "---\nname: mismatched\ndescription: d\n---\n\nx\n", encoding="utf-8"
        )

    report = validate_plugin(root)

    assert report.loadable
    assert len(report.skills) == valid
    assert len([f for f in report.findings if f.code == "skill.invalid"]) == invalid
