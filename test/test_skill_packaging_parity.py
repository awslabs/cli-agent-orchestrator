"""Guard tests locking the skills single-source-of-truth invariant.

Canonical skills live in the repo-root ``skills/`` tree; the package copies under
``src/cli_agent_orchestrator/skills/`` are generated mirrors kept in lockstep by
``scripts/sync_skills.py``. These tests fail loudly (with the fix command) if the
mirror drifts, if a shipped skill is invalid, or if the MCP Apps skill bridge
loses its required cross-references.
"""

import filecmp
import importlib.util
from pathlib import Path
from typing import List

import pytest

from cli_agent_orchestrator.cli.commands.init import seed_default_skills
from cli_agent_orchestrator.utils.skills import validate_skill_folder

REPO_ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILLS_DIR = REPO_ROOT / "skills"
PACKAGE_SKILLS_DIR = REPO_ROOT / "src" / "cli_agent_orchestrator" / "skills"
SYNC_COMMAND = "python scripts/sync_skills.py"


def _load_shipped_skills() -> List[str]:
    """Import ``SHIPPED_SKILLS`` from the sync script (single source of truth)."""
    spec = importlib.util.spec_from_file_location(
        "_sync_skills", REPO_ROOT / "scripts" / "sync_skills.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return list(module.SHIPPED_SKILLS)


SHIPPED_SKILLS = _load_shipped_skills()


def _relative_files(root: Path) -> set:
    """Relative paths of every regular file under ``root`` (excludes pycache)."""
    if not root.is_dir():
        return set()
    return {
        path.relative_to(root)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }


class TestPackagingParity:
    """Repo-root canonical skills must be byte-identical to the package mirror."""

    def test_shipped_skills_allowlist_is_non_empty(self):
        assert SHIPPED_SKILLS, "SHIPPED_SKILLS must list the skills that ship in the package."

    @pytest.mark.parametrize("name", SHIPPED_SKILLS)
    def test_canonical_and_package_file_sets_match(self, name):
        source_files = _relative_files(CANONICAL_SKILLS_DIR / name)
        dest_files = _relative_files(PACKAGE_SKILLS_DIR / name)

        assert source_files, f"Canonical skills/{name}/ has no files."
        assert source_files == dest_files, (
            f"File set differs for '{name}'. "
            f"Missing in package: {sorted(map(str, source_files - dest_files))}; "
            f"stale in package: {sorted(map(str, dest_files - source_files))}. "
            f"Run `{SYNC_COMMAND}`."
        )

    @pytest.mark.parametrize("name", SHIPPED_SKILLS)
    def test_every_file_is_byte_identical(self, name):
        source_dir = CANONICAL_SKILLS_DIR / name
        dest_dir = PACKAGE_SKILLS_DIR / name

        for rel in sorted(_relative_files(source_dir)):
            assert filecmp.cmp(
                source_dir / rel, dest_dir / rel, shallow=False
            ), f"Content differs for '{name}/{rel}'. Run `{SYNC_COMMAND}`."

    @pytest.mark.parametrize("name", SHIPPED_SKILLS)
    def test_references_subdir_is_mirrored(self, name):
        """If the canonical skill has a references/ subdir, the mirror must too."""
        source_refs = CANONICAL_SKILLS_DIR / name / "references"
        dest_refs = PACKAGE_SKILLS_DIR / name / "references"
        if not source_refs.is_dir():
            pytest.skip(f"{name} has no references/ subdir")

        source_files = _relative_files(source_refs)
        dest_files = _relative_files(dest_refs)
        assert source_files == dest_files, f"references/ drift for '{name}'. Run `{SYNC_COMMAND}`."

    @pytest.mark.parametrize("name", SHIPPED_SKILLS)
    def test_canonical_skill_validates(self, name):
        metadata = validate_skill_folder(CANONICAL_SKILLS_DIR / name)
        assert metadata.name == name
        assert metadata.description

    @pytest.mark.parametrize("name", SHIPPED_SKILLS)
    def test_package_skill_validates(self, name):
        metadata = validate_skill_folder(PACKAGE_SKILLS_DIR / name)
        assert metadata.name == name
        assert metadata.description

    def test_shipped_set_is_subset_of_package_set(self):
        package_skill_dirs = {
            child.name
            for child in PACKAGE_SKILLS_DIR.iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        }
        missing = set(SHIPPED_SKILLS) - package_skill_dirs
        assert not missing, f"Shipped skills missing from package: {sorted(missing)}."

    @pytest.mark.parametrize("name", ["cao-plugin", "cao-provider"])
    def test_plugin_and_provider_skills_are_packaged(self, name):
        """Workstream B: cao-plugin / cao-provider must ship so `cao init` seeds them."""
        assert (PACKAGE_SKILLS_DIR / name / "SKILL.md").is_file()
        validate_skill_folder(PACKAGE_SKILLS_DIR / name)


class TestSeedDefaultSkillsClassroom:
    """A clean-room seed must include the MCP-relevant default skills."""

    def test_seed_includes_mcp_and_plugin_provider_skills(self, tmp_path, monkeypatch):
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.init.SKILLS_DIR", skill_store)

        seeded_count = seed_default_skills()

        assert seeded_count >= len(SHIPPED_SKILLS)
        for name in ["cao-mcp-apps", "mcp-apps-builder", "cao-plugin", "cao-provider"]:
            assert (skill_store / name / "SKILL.md").is_file(), f"{name} not seeded by cao init"
        # references/ subdirs must survive the seed copy.
        assert (skill_store / "cao-plugin" / "references").is_dir()
        assert (skill_store / "cao-provider" / "references").is_dir()


class TestMcpAppsBuilderBridge:
    """Workstream D: the ext-apps bridge skill must stay complete."""

    @property
    def builder_text(self) -> str:
        return (PACKAGE_SKILLS_DIR / "mcp-apps-builder" / "SKILL.md").read_text()

    @pytest.mark.parametrize(
        "ext_apps_skill",
        ["create-mcp-app", "add-app-to-server", "migrate-oai-app", "convert-web-app"],
    )
    def test_references_all_four_ext_apps_skills(self, ext_apps_skill):
        assert ext_apps_skill in self.builder_text

    @pytest.mark.parametrize(
        "canonical_source",
        [
            "modelcontextprotocol.io/extensions/apps/overview",  # overview
            "modelcontextprotocol.io/extensions/apps/build",  # build guide
            "specification/2026-01-26/apps.mdx",  # stable spec
            "@modelcontextprotocol/ext-apps",  # SDK package
        ],
    )
    def test_references_canonical_sources(self, canonical_source):
        assert canonical_source in self.builder_text


class TestCaoMcpAppsCrossReference:
    """Workstream D: cao-mcp-apps must point back at mcp-apps-builder."""

    def test_cao_mcp_apps_present_in_package(self):
        assert (PACKAGE_SKILLS_DIR / "cao-mcp-apps" / "SKILL.md").is_file()
        validate_skill_folder(PACKAGE_SKILLS_DIR / "cao-mcp-apps")

    def test_extending_section_mentions_mcp_apps_builder(self):
        text = (PACKAGE_SKILLS_DIR / "cao-mcp-apps" / "SKILL.md").read_text()
        assert "## Extending the surface" in text
        extending = text.split("## Extending the surface", 1)[1]
        assert (
            "mcp-apps-builder" in extending
        ), "cao-mcp-apps 'Extending the surface' section must mention mcp-apps-builder."
