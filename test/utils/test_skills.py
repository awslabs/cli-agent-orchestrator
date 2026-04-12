"""Tests for skill utilities."""

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
        skill_names = ["cao-supervisor-protocols", "cao-worker-protocols"]

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
            "To load a skill's full content, use the `load_skill` MCP tool provided by the "
            "CAO MCP server. These skills are not accessible through provider-native skill "
            "commands or directories.\n\n"
            "- **cao-worker-protocols**: Worker communication\n"
            "- **python-testing**: Pytest conventions"
        )
