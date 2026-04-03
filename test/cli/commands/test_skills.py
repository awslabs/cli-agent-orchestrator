"""Tests for the skills CLI command group."""

from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.skills import skills


def _create_skill(folder: Path, name: str, description: str, body: str = "# Skill\n\nBody") -> None:
    """Create a skill folder with SKILL.md and optional content."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" f"{body}\n"
    )


class TestSkillsHelp:
    """Tests for skills command help output."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    def test_skills_help(self, runner):
        """The skills group should be accessible from the CLI."""
        result = runner.invoke(skills, ["--help"])

        assert result.exit_code == 0
        assert "Manage installed skills" in result.output
        assert "add" in result.output
        assert "remove" in result.output
        assert "list" in result.output

    def test_skills_add_help(self, runner):
        """The add subcommand should provide help text."""
        result = runner.invoke(skills, ["add", "--help"])

        assert result.exit_code == 0
        assert "Install a skill from a local folder path" in result.output

    def test_skills_remove_help(self, runner):
        """The remove subcommand should provide help text."""
        result = runner.invoke(skills, ["remove", "--help"])

        assert result.exit_code == 0
        assert "Remove an installed skill" in result.output

    def test_skills_list_help(self, runner):
        """The list subcommand should provide help text."""
        result = runner.invoke(skills, ["list", "--help"])

        assert result.exit_code == 0
        assert "List installed skills" in result.output


class TestSkillsAddCommand:
    """Tests for `cao skills add`."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    def test_add_installs_valid_skill_folder(self, runner, tmp_path, monkeypatch):
        """A valid skill folder should be copied into the skill store."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")
        (source_dir / "examples.txt").write_text("example data")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code == 0
        assert "installed successfully" in result.output
        assert (skill_store / "python-testing" / "SKILL.md").exists()
        assert (skill_store / "python-testing" / "examples.txt").read_text() == "example data"

    def test_add_rejects_duplicate_without_force(self, runner, tmp_path, monkeypatch):
        """Adding the same skill twice without --force should fail clearly."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")
        (skill_store / "python-testing").mkdir(parents=True)
        (skill_store / "python-testing" / "SKILL.md").write_text("existing")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code != 0
        assert "already exists" in result.output

    def test_add_force_overwrites_existing_skill(self, runner, tmp_path, monkeypatch):
        """--force should replace an existing installed skill folder."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Updated description", body="# Updated")
        (source_dir / "new-file.txt").write_text("new")

        existing_dir = skill_store / "python-testing"
        existing_dir.mkdir(parents=True)
        (existing_dir / "SKILL.md").write_text("---\nname: python-testing\ndescription: Old\n---\n")
        (existing_dir / "old-file.txt").write_text("old")

        result = runner.invoke(skills, ["add", str(source_dir), "--force"])

        assert result.exit_code == 0
        assert not (existing_dir / "old-file.txt").exists()
        assert (existing_dir / "new-file.txt").read_text() == "new"
        assert "Updated description" in (existing_dir / "SKILL.md").read_text()

    def test_add_rejects_invalid_skill_folder(self, runner, tmp_path, monkeypatch):
        """Invalid skill folders should fail validation before install."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        invalid_dir = tmp_path / "python-testing"
        invalid_dir.mkdir()

        result = runner.invoke(skills, ["add", str(invalid_dir)])

        assert result.exit_code != 0
        assert "Missing SKILL.md" in result.output

    def test_add_rejects_path_traversal_name(self, runner, tmp_path, monkeypatch):
        """Frontmatter names with traversal content should be rejected."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        source_dir = tmp_path / r"bad\name"
        _create_skill(source_dir, r"bad\name", "Traversal attempt")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code != 0
        assert "Invalid skill name" in result.output


class TestSkillsRemoveCommand:
    """Tests for `cao skills remove`."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    def test_remove_deletes_existing_skill(self, runner, tmp_path, monkeypatch):
        """Removing an installed skill should delete its folder."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        installed_dir = skill_store / "python-testing"
        _create_skill(installed_dir, "python-testing", "Pytest conventions")

        result = runner.invoke(skills, ["remove", "python-testing"])

        assert result.exit_code == 0
        assert not installed_dir.exists()
        assert "removed successfully" in result.output

    def test_remove_rejects_path_traversal_name(self, runner, tmp_path, monkeypatch):
        """Traversal names should be rejected before touching the filesystem."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        result = runner.invoke(skills, ["remove", "../evil"])

        assert result.exit_code != 0
        assert "Invalid skill name" in result.output

    def test_remove_errors_when_skill_missing(self, runner, tmp_path, monkeypatch):
        """Removing a missing skill should return a clear error."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)

        result = runner.invoke(skills, ["remove", "missing-skill"])

        assert result.exit_code != 0
        assert "does not exist" in result.output


class TestSkillsListCommand:
    """Tests for `cao skills list`."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    def test_list_displays_name_and_description_columns(self, runner, tmp_path, monkeypatch):
        """Installed skills should be rendered in a table with both columns."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)

        _create_skill(skill_store / "alpha", "alpha", "Alpha skill")
        _create_skill(skill_store / "beta", "beta", "Beta skill")

        result = runner.invoke(skills, ["list"])

        assert result.exit_code == 0
        assert "Name" in result.output
        assert "Description" in result.output
        assert "alpha" in result.output
        assert "Alpha skill" in result.output
        assert "beta" in result.output
        assert "Beta skill" in result.output

    def test_list_empty_store(self, runner, tmp_path, monkeypatch):
        """An empty skill store should print a friendly message."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)

        result = runner.invoke(skills, ["list"])

        assert result.exit_code == 0
        assert "No skills found" in result.output
