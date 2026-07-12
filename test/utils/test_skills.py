"""Tests for skill utilities."""

import logging
from pathlib import Path
from unittest.mock import patch

import pytest

from cli_agent_orchestrator.models.skill import SkillMetadata
from cli_agent_orchestrator.utils.skills import (
    build_skill_catalog,
    list_skills,
    load_skill_content,
    load_skill_metadata,
    validate_skill_folder,
)


def _write_skill(folder: Path, name: str, description: str, body: str = "# Title\n\nBody") -> Path:
    """Create a skill folder with a valid SKILL.md file."""
    folder.mkdir(parents=True, exist_ok=True)
    skill_file = folder / "SKILL.md"
    skill_file.write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" f"{body}\n"
    )
    return skill_file


@pytest.fixture(autouse=True)
def _default_no_extra_skill_dirs(monkeypatch):
    """Default ``extra_skill_dirs`` to empty for every test.

    Existing tests patch only ``SKILLS_DIR``; without this they would read the
    developer's real ``settings.json``. Tests that exercise extra dirs override
    this via ``_use_skill_dirs``.
    """
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
        lambda: [],
    )


def _use_skill_dirs(monkeypatch, global_dir, extra_dirs):
    """Point skill resolution at a global store plus a list of extra directories."""
    monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", global_dir)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_extra_skill_dirs",
        lambda: [str(d) for d in extra_dirs],
    )


class TestLoadSkillMetadata:
    """Tests for load_skill_metadata."""

    @pytest.mark.parametrize("skill_name", ["", "   "])
    def test_rejects_empty_skill_name(self, tmp_path, monkeypatch, skill_name):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(ValueError, match="Skill name must not be empty"):
            load_skill_metadata(skill_name)

    def test_loads_metadata_from_skill_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(tmp_path / "python-testing", "python-testing", "Pytest conventions")

        metadata = load_skill_metadata("python-testing")

        assert metadata == SkillMetadata(
            name="python-testing",
            description="Pytest conventions",
        )

    @pytest.mark.parametrize("skill_name", ["../escape", "/absolute", r"..\\escape", r"bad\\name"])
    def test_rejects_path_traversal_inputs(self, tmp_path, monkeypatch, skill_name):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(ValueError, match="Invalid skill name"):
            load_skill_metadata(skill_name)

    def test_raises_for_missing_skill_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(FileNotFoundError, match="Skill folder does not exist"):
            load_skill_metadata("missing-skill")

    def test_raises_for_missing_skill_markdown(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        (tmp_path / "python-testing").mkdir()

        with pytest.raises(FileNotFoundError, match="Missing SKILL.md"):
            load_skill_metadata("python-testing")

    def test_raises_for_missing_frontmatter_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        skill_dir = tmp_path / "python-testing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: python-testing\n---\n\nBody\n")

        with pytest.raises(ValueError, match="Invalid skill metadata"):
            load_skill_metadata("python-testing")

    def test_raises_for_empty_frontmatter_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        skill_dir = tmp_path / "python-testing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text(
            "---\nname: python-testing\ndescription: '   '\n---\n\nBody\n"
        )

        with pytest.raises(ValueError, match="Invalid skill metadata"):
            load_skill_metadata("python-testing")

    def test_raises_for_malformed_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        skill_dir = tmp_path / "python-testing"
        skill_dir.mkdir()
        (skill_dir / "SKILL.md").write_text("---\nname: [python-testing\n---\n\nBody\n")

        with pytest.raises(ValueError, match="Failed to parse skill file"):
            load_skill_metadata("python-testing")

    def test_raises_for_folder_name_mismatch(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(tmp_path / "wrong-folder", "python-testing", "Pytest conventions")

        with pytest.raises(ValueError, match="does not match skill name"):
            load_skill_metadata("wrong-folder")


class TestLoadSkillContent:
    """Tests for load_skill_content."""

    @pytest.mark.parametrize("skill_name", ["", "   "])
    def test_rejects_empty_skill_name(self, tmp_path, monkeypatch, skill_name):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(ValueError, match="Skill name must not be empty"):
            load_skill_content(skill_name)

    def test_returns_markdown_body_without_frontmatter(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(
            tmp_path / "python-testing",
            "python-testing",
            "Pytest conventions",
            body="# Python Testing\n\nUse pytest fixtures.",
        )

        content = load_skill_content("python-testing")

        assert content == "# Python Testing\n\nUse pytest fixtures."

    @pytest.mark.parametrize("skill_name", ["../escape", "/absolute"])
    def test_rejects_path_traversal_inputs(self, tmp_path, monkeypatch, skill_name):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(ValueError, match="Invalid skill name"):
            load_skill_content(skill_name)

    def test_raises_for_missing_skill_folder(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)

        with pytest.raises(FileNotFoundError, match="Skill folder does not exist"):
            load_skill_content("missing-skill")


class TestListSkills:
    """Tests for list_skills."""

    def test_returns_sorted_valid_skills(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(tmp_path / "zebra", "zebra", "Last skill")
        _write_skill(tmp_path / "alpha", "alpha", "First skill")

        skills = list_skills()

        assert [skill.name for skill in skills] == ["alpha", "zebra"]

    def test_returns_empty_list_when_skill_store_missing(self, tmp_path, monkeypatch):
        missing_dir = tmp_path / "missing-store"
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", missing_dir)

        assert list_skills() == []

    def test_ignores_plain_files_in_skill_store(self, tmp_path, monkeypatch):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(tmp_path / "alpha", "alpha", "First skill")
        (tmp_path / "README.md").write_text("not a skill folder")

        skills = list_skills()

        assert [skill.name for skill in skills] == ["alpha"]

    def test_skips_invalid_skill_folders_with_warning(self, tmp_path, monkeypatch, caplog):
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", tmp_path)
        _write_skill(tmp_path / "valid-skill", "valid-skill", "Valid skill")
        broken_dir = tmp_path / "broken-skill"
        broken_dir.mkdir()
        (broken_dir / "SKILL.md").write_text("---\nname: wrong-name\ndescription: Broken\n---\n")

        skills = list_skills()

        assert [skill.name for skill in skills] == ["valid-skill"]
        assert "Skipping invalid skill folder" in caplog.text
        assert "broken-skill" in caplog.text


class TestExtraSkillDirs:
    """Resolving skills from ``extra_skill_dirs`` (mirrors ``extra_agent_dirs``)."""

    def test_load_metadata_from_extra_dir(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task workflow")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        metadata = load_skill_metadata("task")

        assert metadata == SkillMetadata(name="task", description="Project task workflow")

    def test_load_content_from_extra_dir(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task", body="# Task\n\nSteps.")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        assert load_skill_content("task") == "# Task\n\nSteps."

    def test_global_store_takes_precedence_over_extra_dir(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        _write_skill(global_dir / "task", "task", "Global task")
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        assert load_skill_metadata("task").description == "Global task"

    def test_list_includes_global_and_extra_dirs(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        _write_skill(global_dir / "alpha", "alpha", "Global alpha")
        extra = tmp_path / "project"
        _write_skill(extra / "beta", "beta", "Project beta")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        assert [skill.name for skill in list_skills()] == ["alpha", "beta"]

    def test_list_dedups_with_global_winning_over_extra(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        _write_skill(global_dir / "task", "task", "Global task")
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        skills = list_skills()

        assert [skill.name for skill in skills] == ["task"]
        assert skills[0].description == "Global task"

    def test_list_skips_non_skill_subdir_silently(self, tmp_path, monkeypatch, caplog):
        global_dir = tmp_path / "global"
        _write_skill(global_dir / "alpha", "alpha", "Global alpha")
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task")
        (extra / "node_modules").mkdir(parents=True)  # unrelated folder, no SKILL.md
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        skills = list_skills()

        assert [skill.name for skill in skills] == ["alpha", "task"]
        assert "node_modules" not in caplog.text

    def test_list_skips_missing_extra_dir(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        _write_skill(global_dir / "alpha", "alpha", "Global alpha")
        missing = tmp_path / "does-not-exist"
        _use_skill_dirs(monkeypatch, global_dir, [missing])

        assert [skill.name for skill in list_skills()] == ["alpha"]

    def test_missing_skill_falls_back_to_global_path_error(self, tmp_path, monkeypatch):
        global_dir = tmp_path / "global"
        global_dir.mkdir()
        extra = tmp_path / "project"
        extra.mkdir()
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        with pytest.raises(FileNotFoundError, match="Skill folder does not exist"):
            load_skill_metadata("nope")

    def test_invalid_earlier_skill_does_not_shadow_valid_extra(self, tmp_path, monkeypatch):
        """A broken same-named folder in an earlier dir must not hide a valid later one.

        ``list_skills`` already skips the invalid folder and advertises the
        valid extra-dir skill; ``_resolve_skill_path`` must agree so ``load_skill``
        succeeds for the same name ("first valid match wins").
        """
        global_dir = tmp_path / "global"
        # Invalid: folder name does not match the declared skill name.
        broken = global_dir / "task"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: other\ndescription: Broken\n---\n\nBody\n")
        extra = tmp_path / "project"
        _write_skill(extra / "task", "task", "Project task", body="# Task\n\nSteps.")
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        # list and load must agree: both resolve to the valid extra-dir skill.
        assert "task" in [skill.name for skill in list_skills()]
        assert load_skill_metadata("task").description == "Project task"
        assert load_skill_content("task") == "# Task\n\nSteps."

    def test_invalid_only_skill_surfaces_validation_error(self, tmp_path, monkeypatch):
        """When the only same-named folder is invalid, the real validation error is raised.

        The fallback preserves the meaningful error instead of degrading to a
        generic "Skill folder does not exist".
        """
        global_dir = tmp_path / "global"
        broken = global_dir / "task"
        broken.mkdir(parents=True)
        (broken / "SKILL.md").write_text("---\nname: other\ndescription: Broken\n---\n\nBody\n")
        extra = tmp_path / "project"
        extra.mkdir()
        _use_skill_dirs(monkeypatch, global_dir, [extra])

        with pytest.raises(ValueError, match="does not match skill name"):
            load_skill_metadata("task")


class TestValidateSkillFolder:
    """Tests for validate_skill_folder."""

    def test_validates_arbitrary_skill_folder(self, tmp_path):
        skill_dir = tmp_path / "code-style"
        _write_skill(skill_dir, "code-style", "Shared coding conventions")

        metadata = validate_skill_folder(skill_dir)

        assert metadata == SkillMetadata(
            name="code-style",
            description="Shared coding conventions",
        )

    def test_raises_when_path_is_not_directory(self, tmp_path):
        skill_file = tmp_path / "not-a-directory"
        skill_file.write_text("plain file")

        with pytest.raises(ValueError, match="not a directory"):
            validate_skill_folder(skill_file)

    def test_raises_when_skill_markdown_missing(self, tmp_path):
        skill_dir = tmp_path / "code-style"
        skill_dir.mkdir()

        with pytest.raises(FileNotFoundError, match="Missing SKILL.md"):
            validate_skill_folder(skill_dir)

    def test_raises_when_folder_name_does_not_match_declared_name(self, tmp_path):
        skill_dir = tmp_path / "code-style"
        _write_skill(skill_dir, "python-testing", "Shared coding conventions")

        with pytest.raises(ValueError, match="does not match skill name"):
            validate_skill_folder(skill_dir)


class TestDefaultBundledSkills:
    """Tests for packaged default skills."""

    @property
    def bundled_skills_dir(self) -> Path:
        """Return the package skills directory."""
        return Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator" / "skills"

    def test_default_skill_folders_exist_with_valid_metadata(self):
        skill_names = ["cao-memory", "cao-supervisor-protocols", "cao-worker-protocols"]

        for skill_name in skill_names:
            metadata = validate_skill_folder(self.bundled_skills_dir / skill_name)
            assert metadata.name == skill_name
            assert metadata.description

    def test_default_skills_cover_core_communication_primitives(self):
        supervisor_content = (
            self.bundled_skills_dir / "cao-supervisor-protocols" / "SKILL.md"
        ).read_text()
        worker_content = (self.bundled_skills_dir / "cao-worker-protocols" / "SKILL.md").read_text()

        assert "assign" in supervisor_content
        assert "handoff" in supervisor_content
        assert "send_message" in supervisor_content
        assert "idle" in supervisor_content.lower()
        assert "assign" in worker_content
        assert "handoff" in worker_content
        assert "send_message" in worker_content

    def test_memory_skill_covers_core_memory_tools(self):
        memory_content = (self.bundled_skills_dir / "cao-memory" / "SKILL.md").read_text()

        assert "memory_store" in memory_content
        assert "memory_recall" in memory_content
        assert "memory_forget" in memory_content


class TestBuildSkillCatalog:
    """Tests for build_skill_catalog."""

    @patch("cli_agent_orchestrator.utils.skills.list_skills", return_value=[])
    def test_returns_empty_string_when_no_skills_installed(self, mock_list_skills):
        """Empty skill stores should produce no injected catalog."""
        assert build_skill_catalog() == ""
        mock_list_skills.assert_called_once_with()

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_renders_all_installed_skills(self, mock_list_skills):
        """All installed skills should appear in the global catalog."""
        mock_list_skills.return_value = [
            SkillMetadata(name="cao-worker-protocols", description="Worker communication"),
            SkillMetadata(name="python-testing", description="Pytest conventions"),
        ]

        assert build_skill_catalog() == (
            "## Available Skills\n\n"
            "The following skills are available exclusively in this CAO orchestration context. "
            "To load a skill's full content, use the `mcp__cao-mcp-server__load_skill` "
            "tool (NOT the Skill command). These skills are not accessible through "
            "provider-native skill commands or directories.\n\n"
            "- **cao-worker-protocols**: Worker communication\n"
            "- **python-testing**: Pytest conventions"
        )


class TestBuildSkillCatalogFilter:
    """Tests for per-agent skill-catalog scoping via ``skill_filter``."""

    @staticmethod
    def _skills():
        return [
            SkillMetadata(name="ads-db", description="DB access"),
            SkillMetadata(name="ads-task", description="Task workflow"),
            SkillMetadata(name="cao-worker-protocols", description="Worker comms"),
            SkillMetadata(name="linkedapi-task", description="LinkedAPI workflow"),
        ]

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_none_filter_lists_every_skill(self, mock_list_skills):
        """``None`` (the default) keeps the original full-catalog behaviour."""
        mock_list_skills.return_value = self._skills()

        catalog = build_skill_catalog(None)

        for name in ("ads-db", "ads-task", "cao-worker-protocols", "linkedapi-task"):
            assert f"**{name}**" in catalog

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_exact_names_allowlist(self, mock_list_skills):
        """An exact-name allowlist advertises only the named skills."""
        mock_list_skills.return_value = self._skills()

        catalog = build_skill_catalog(["ads-task"])

        assert "**ads-task**" in catalog
        assert "**ads-db**" not in catalog
        assert "**linkedapi-task**" not in catalog

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_glob_prefix_scopes_to_project(self, mock_list_skills):
        """A glob pattern scopes the catalog to one project's skills."""
        mock_list_skills.return_value = self._skills()

        catalog = build_skill_catalog(["ads-*"])

        assert "**ads-db**" in catalog
        assert "**ads-task**" in catalog
        assert "**linkedapi-task**" not in catalog
        assert "**cao-worker-protocols**" not in catalog

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_mixed_exact_and_glob(self, mock_list_skills):
        """Exact names and globs can be combined."""
        mock_list_skills.return_value = self._skills()

        catalog = build_skill_catalog(["ads-task", "cao-*"])

        assert "**ads-task**" in catalog
        assert "**cao-worker-protocols**" in catalog
        assert "**ads-db**" not in catalog
        assert "**linkedapi-task**" not in catalog

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_empty_allowlist_advertises_nothing(self, mock_list_skills):
        """An empty list hides every skill (no catalog block)."""
        mock_list_skills.return_value = self._skills()

        assert build_skill_catalog([]) == ""

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_match_is_case_sensitive(self, mock_list_skills):
        """Patterns match skill names case-sensitively (fnmatchcase), since skill
        names are case-sensitive identifiers on disk."""
        mock_list_skills.return_value = self._skills()

        assert build_skill_catalog(["ADS-*"]) == ""
        assert build_skill_catalog(["ADS-DB"]) == ""

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_unmatched_patterns_are_logged(self, mock_list_skills, caplog):
        """Patterns matching no skill are warned about (to catch profile typos),
        without suppressing the patterns that do match."""
        mock_list_skills.return_value = self._skills()

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.utils.skills"):
            catalog = build_skill_catalog(["ads-*", "missing-skill"])

        assert "**ads-db**" in catalog  # the matching pattern still resolves
        messages = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        # The unmatched pattern is rendered with repr() so a hostile / newline-y
        # name cannot garble the log line.
        assert any("'missing-skill'" in m for m in messages)
        assert not any("ads-*" in m for m in messages)

    @patch("cli_agent_orchestrator.utils.skills.list_skills")
    def test_overlapping_patterns_emit_no_warning(self, mock_list_skills, caplog):
        """Redundant/overlapping patterns (a glob plus an exact name it already
        covers) are all counted as matched — no spurious unmatched warning. Guards
        against a future 'break on first match' optimisation regressing this."""
        mock_list_skills.return_value = self._skills()

        with caplog.at_level(logging.WARNING, logger="cli_agent_orchestrator.utils.skills"):
            catalog = build_skill_catalog(["ads-*", "ads-db"])

        assert "**ads-db**" in catalog
        assert "**ads-task**" in catalog
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []
