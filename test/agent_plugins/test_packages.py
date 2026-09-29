"""CAO's own Agent Plugins packages — conformance, allowlists, and the drift guard.

**Validates: Requirements 1.1–1.6, 2.1–2.8, 3.1–3.5, 23.2**

Both packages must validate as loadable with zero fatal findings, each
allowlist must be enforced *independently*, and the absence of
``cao-contributing`` from the contributor package must not read as a failure —
it depends on PR #448, which is open and still a draft.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.agent_plugins.installer import install
from cli_agent_orchestrator.agent_plugins.models import PluginSource, Severity
from cli_agent_orchestrator.agent_plugins.validation import validate_plugin

from .conftest import PLUGIN_SCHEMA_ID

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGES_DIR = REPO_ROOT / "agent-plugin"

sys.path.insert(0, str(REPO_ROOT / "scripts"))

import build_agent_plugin as builder  # noqa: E402

OPERATOR_DIR = PACKAGES_DIR / "cao"
CONTRIBUTOR_DIR = PACKAGES_DIR / "cao-contributor"


def manifest_of(package_dir: Path) -> dict:
    return json.loads((package_dir / "plugin.json").read_text(encoding="utf-8"))


class TestBothPackagesAreConformant:
    """Requirement 3.4 / 23.2 — the conformance half of the CI guard."""

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_package_is_loadable_with_zero_fatals(self, package_dir):
        report = validate_plugin(package_dir)

        fatals = [f for f in report.findings if f.severity is Severity.FATAL]
        assert fatals == [], fatals
        assert report.loadable

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_schema_is_pinned_to_the_vendored_version(self, package_dir):
        assert manifest_of(package_dir)["$schema"] == PLUGIN_SCHEMA_ID

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_version_is_synced_from_cao_package_metadata(self, package_dir):
        """Requirement 3.2 — the two values cannot diverge without a build failure."""
        assert manifest_of(package_dir)["version"] == builder.package_version()

    def test_the_parent_directory_is_not_itself_a_plugin_root(self):
        """``agent-plugin/`` is a container; the plugin roots are its children."""
        assert not (PACKAGES_DIR / "plugin.json").exists()
        assert not validate_plugin(PACKAGES_DIR).loadable

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_the_claude_code_overlay_mirrors_the_root_manifest(self, package_dir):
        """The overlay is identity-only and cannot disagree with ``plugin.json``.

        Verified empirically against Claude Code 2.1.226: skills are discovered
        from the standard layout unchanged, but identity comes only from
        ``.claude-plugin/plugin.json``. Exactly three mirrored fields — a fourth
        would be a second source of truth for something.
        """
        overlay = json.loads(
            (package_dir / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8")
        )
        root = manifest_of(package_dir)
        assert set(overlay) == {"name", "version", "description"}
        assert all(overlay[key] == root[key] for key in overlay)

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_skills_are_copies_not_symlinks(self, package_dir):
        """§4.1 permits a symlink resolving inside the root; ``../../skills/`` escapes it.

        A copy plus the drift guard is the only conformant option.
        """
        for entry in (package_dir / "skills").iterdir():
            assert not entry.is_symlink(), entry


class TestOperatorPackage:
    def test_name_is_cao(self):
        assert manifest_of(OPERATOR_DIR)["name"] == "cao"

    def test_it_ships_the_operator_skills(self):
        """Requirements 1.2, 1.3."""
        names = set(validate_plugin(OPERATOR_DIR).skill_names)
        assert {"cao-session-management", "cao-agent-routing"} <= names
        # Maintainer-tunable, currently included.
        assert {"cao-supervisor-protocols", "cao-worker-protocols"} <= names

    def test_it_excludes_contributor_and_vendored_skills(self):
        """Requirement 1.4."""
        names = set(validate_plugin(OPERATOR_DIR).skill_names)
        assert "cao-provider" not in names
        assert "cao-plugin" not in names
        assert "cao-event-plugin" not in names
        assert not any(name.startswith("ext-apps") for name in names)
        for vendored in ("create-mcp-app", "add-app-to-server", "migrate-oai-app"):
            assert vendored not in names

    def test_the_description_states_every_prerequisite(self):
        """Requirement 1.5 — surfaced in the manifest, not merely implied."""
        description = manifest_of(OPERATOR_DIR)["description"]
        assert "uv" in description
        assert "PATH" in description
        assert "http://127.0.0.1:9889" in description
        assert "localhost-only" in description

    def test_it_ships_an_mcp_json_declaring_the_ops_server(self):
        """Requirement 19 — Increment 2. See TestPackagedMcpServer for the details."""
        assert (OPERATOR_DIR / "mcp.json").is_file()
        report = validate_plugin(OPERATOR_DIR)
        assert report.mcp_present is True
        assert [server.name for server in report.mcp_servers] == ["cao-ops"]

    def test_the_mcp_overlay_is_byte_identical_to_the_standard_file(self):
        """One server list, two filenames.

        Claude Code reads ``.mcp.json``; the standard's file is ``mcp.json``.
        Any divergence means two clients would launch different servers from
        the same package, which is worse than either file alone.
        """
        assert (OPERATOR_DIR / ".mcp.json").read_bytes() == (OPERATOR_DIR / "mcp.json").read_bytes()

    def test_the_session_management_skill_is_discovered(self):
        """Requirement 1.6."""
        assert "cao-session-management" in validate_plugin(OPERATOR_DIR).skill_names


class TestContributorPackage:
    def test_it_is_a_distinct_package(self):
        """Requirement 2.2 — AC3 calls for two packages, not one."""
        assert OPERATOR_DIR != CONTRIBUTOR_DIR
        assert manifest_of(CONTRIBUTOR_DIR)["name"] == "cao-contributor"

    def test_it_ships_the_authoring_skills(self):
        """Requirement 2.3, under the current pre-M4 skill name."""
        names = set(validate_plugin(CONTRIBUTOR_DIR).skill_names)
        assert "cao-provider" in names
        assert "cao-plugin" in names or "cao-event-plugin" in names

    def test_it_excludes_operator_skills(self):
        """Requirement 2.4."""
        names = set(validate_plugin(CONTRIBUTOR_DIR).skill_names)
        assert "cao-session-management" not in names
        assert "cao-agent-routing" not in names
        assert "cao-supervisor-protocols" not in names

    def test_it_never_ships_an_mcp_json(self):
        """Requirement 2.5 — in *either* increment.

        Authoring skills read and write repo files through the host agent's own
        tools and need no CAO runtime, so the uv/cao-server prerequisites do not
        apply to this package at all.
        """
        assert not (CONTRIBUTOR_DIR / "mcp.json").exists()
        assert not (CONTRIBUTOR_DIR / ".mcp.json").exists()

    def test_the_absence_of_cao_contributing_is_not_a_failure(self):
        """Requirement 2.6 — PR #448 is open and still a draft."""
        report = validate_plugin(CONTRIBUTOR_DIR)

        assert "cao-contributing" not in report.skill_names
        assert report.loadable
        assert [f for f in report.findings if f.severity is Severity.FATAL] == []

    def test_adding_cao_contributing_is_one_allowlist_line(self, tmp_path):
        """Requirement 2.7 — no change to structure, tooling, or CI.

        Simulated by building with an extended allowlist against a stub skill,
        which is exactly the edit #448 landing would require.
        """
        from dataclasses import replace

        stub_skills = tmp_path / "skills"
        stub_skills.mkdir()
        for skill in [*builder.CONTRIBUTOR.skills, "cao-contributing"]:
            folder = stub_skills / skill
            folder.mkdir()
            (folder / "SKILL.md").write_text(
                f"---\nname: {skill}\ndescription: d\n---\n\nx\n", encoding="utf-8"
            )

        extended = replace(
            builder.CONTRIBUTOR, skills=[*builder.CONTRIBUTOR.skills, "cao-contributing"]
        )

        original = builder.CANONICAL_SKILLS_DIR
        try:
            builder.CANONICAL_SKILLS_DIR = stub_skills
            built = builder.build_package(extended, "9.9.9", tmp_path / "out")
        finally:
            builder.CANONICAL_SKILLS_DIR = original

        report = validate_plugin(built)
        assert report.loadable
        assert "cao-contributing" in report.skill_names


class TestAllowlistsAreEnforcedIndependently:
    """Requirement 3.5 — one package's failure must not mask the other's."""

    @pytest.mark.parametrize(
        "config, package_dir",
        [(builder.OPERATOR, OPERATOR_DIR), (builder.CONTRIBUTOR, CONTRIBUTOR_DIR)],
    )
    def test_discovered_skills_equal_the_allowlist(self, config, package_dir):
        assert sorted(validate_plugin(package_dir).skill_names) == sorted(config.skills)

    def test_the_two_allowlists_are_disjoint(self):
        """The audience split is the point; overlap would defeat it."""
        assert not (set(builder.OPERATOR.skills) & set(builder.CONTRIBUTOR.skills))

    def test_check_reports_both_packages_when_both_drift(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(builder, "PACKAGES_DIR", tmp_path / "empty")

        assert builder.main(["--check"]) == 1

        error = capsys.readouterr().err
        assert "cao:" in error
        assert "cao-contributor:" in error

    def test_check_reports_the_second_package_when_only_it_drifts(
        self, tmp_path, monkeypatch, capsys
    ):
        """The specific regression Requirement 3.5 names."""
        staging = tmp_path / "agent-plugin"
        staging.mkdir()
        version = builder.package_version()
        builder.build_package(builder.OPERATOR, version, staging)
        # Contributor package deliberately not built.
        monkeypatch.setattr(builder, "PACKAGES_DIR", staging)

        assert builder.main(["--check"]) == 1

        error = capsys.readouterr().err
        assert "cao-contributor" in error


class TestDriftGuard:
    def test_check_passes_against_the_committed_tree(self, capsys):
        assert builder.main(["--check"]) == 0
        assert "OK" in capsys.readouterr().out

    def test_check_fails_on_a_hand_edited_manifest(self, tmp_path, monkeypatch, capsys):
        staging = tmp_path / "agent-plugin"
        staging.mkdir()
        version = builder.package_version()
        for config in builder.PACKAGES:
            builder.build_package(config, version, staging)

        manifest = staging / "cao" / "plugin.json"
        data = json.loads(manifest.read_text(encoding="utf-8"))
        data["description"] = "hand edited"
        manifest.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

        monkeypatch.setattr(builder, "PACKAGES_DIR", staging)
        assert builder.main(["--check"]) == 1
        assert "CONTENT DIFFERS" in capsys.readouterr().err

    def test_check_fails_when_a_skill_is_added_out_of_band(self, tmp_path, monkeypatch, capsys):
        staging = tmp_path / "agent-plugin"
        staging.mkdir()
        version = builder.package_version()
        for config in builder.PACKAGES:
            builder.build_package(config, version, staging)

        rogue = staging / "cao" / "skills" / "rogue-skill"
        rogue.mkdir()
        (rogue / "SKILL.md").write_text(
            "---\nname: rogue-skill\ndescription: d\n---\n\nx\n", encoding="utf-8"
        )

        monkeypatch.setattr(builder, "PACKAGES_DIR", staging)
        assert builder.main(["--check"]) == 1
        error = capsys.readouterr().err
        assert "UNEXPECTED" in error or "not allowlisted" in error

    def test_a_missing_canonical_skill_fails_the_build(self, tmp_path):
        from dataclasses import replace

        bad = replace(builder.OPERATOR, skills=["no-such-skill"])
        with pytest.raises(builder.BuildError, match="no-such-skill"):
            builder.build_package(bad, "9.9.9", tmp_path)

    def test_the_build_is_reproducible(self, tmp_path):
        """Two builds of the same inputs produce byte-identical trees."""
        version = builder.package_version()
        first = builder.build_package(builder.OPERATOR, version, tmp_path / "a")
        second = builder.build_package(builder.OPERATOR, version, tmp_path / "b")

        for rel in builder._relative_files(first):
            assert (first / rel).read_bytes() == (second / rel).read_bytes(), rel


class TestPackagesInstallThroughTheRealPipeline:
    """AC3: the contributor plugin validates and installs *the same way*."""

    @pytest.mark.parametrize("package_dir", [OPERATOR_DIR, CONTRIBUTOR_DIR])
    def test_the_package_installs_and_projects_its_skills(self, package_dir, store, skills_dir):
        outcome = install(
            PluginSource(kind="path", location=str(package_dir)),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )

        assert outcome.installed
        assert set(outcome.record.projected_skill_names) == set(
            validate_plugin(package_dir).skill_names
        )

    def test_both_packages_install_side_by_side(self, store, skills_dir):
        """Their disjoint allowlists mean no collision between them."""
        for package_dir in (OPERATOR_DIR, CONTRIBUTOR_DIR):
            install(
                PluginSource(kind="path", location=str(package_dir)),
                store=store,
                skills_dir=skills_dir,
                refresh_agents=False,
            )

        assert {record.name for record in store.list_installed()} == {"cao", "cao-contributor"}
        collisions = [
            f for record in store.list_installed() for f in record.findings if "collision" in f.code
        ]
        assert collisions == []

    def test_subdir_addressing_works_against_this_repository(self, store, skills_dir):
        """Requirement 8.3, dogfooded: CAO's packages live in a subdirectory."""
        outcome = install(
            PluginSource(kind="path", location=str(REPO_ROOT), subdir="agent-plugin/cao"),
            store=store,
            skills_dir=skills_dir,
            refresh_agents=False,
        )
        assert outcome.installed
        assert outcome.record.name == "cao"
