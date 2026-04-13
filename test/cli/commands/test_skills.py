"""Tests for the skills CLI command group."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.skills import skills
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.skill_injection import refresh_agent_json_prompt


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

    def test_add_refreshes_cao_managed_agent_prompt(self, runner, tmp_path, monkeypatch):
        """Adding a skill should refresh CAO-managed installed agent JSONs."""
        skill_store = tmp_path / "skill-store"
        local_store = tmp_path / "agent-store"
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        for path in (skill_store, local_store, context_dir, q_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skill_injection.Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.COPILOT_AGENTS_DIR", copilot_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        (local_store / "developer.md").write_text(
            "---\nname: developer\ndescription: Developer\nprompt: Base prompt\n---\nBody\n",
            encoding="utf-8",
        )
        context_file = context_dir / "developer.md"
        context_file.write_text("context", encoding="utf-8")
        agent_json = q_dir / "developer.json"
        agent_json.write_text(
            json.dumps(
                {
                    "name": "developer",
                    "description": "Developer",
                    "resources": [f"file://{context_file}"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code == 0
        refreshed_json = json.loads(agent_json.read_text())
        assert "python-testing" in refreshed_json["prompt"]
        assert "Refreshed 1 installed agent(s)" in result.output

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

    def test_add_with_no_installed_agents_prints_no_refresh_message(
        self, runner, tmp_path, monkeypatch
    ):
        """Adding a skill with zero installed CAO-managed agents should stay quiet."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.Q_AGENTS_DIR", tmp_path / "q"
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.COPILOT_AGENTS_DIR", tmp_path / "copilot"
        )

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code == 0
        assert "installed successfully" in result.output
        assert "Refreshed" not in result.output

    def test_add_does_not_roll_back_when_refresh_fails(self, runner, tmp_path, monkeypatch):
        """Skill install should succeed even if installed-agent refresh fails."""
        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.skills.refresh_all_cao_managed_agents",
            lambda: (_ for _ in ()).throw(RuntimeError("refresh boom")),
        )

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code == 0
        assert (skill_store / "python-testing" / "SKILL.md").exists()
        assert "Warning: failed to refresh installed agent prompts: refresh boom" in result.output

    def test_add_leaves_non_cao_managed_json_unchanged(self, runner, tmp_path, monkeypatch):
        """Non-CAO-managed installed JSONs should be untouched by skill add."""
        skill_store = tmp_path / "skill-store"
        q_dir = tmp_path / "q"
        for path in (skill_store, q_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skill_injection.Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.AGENT_CONTEXT_DIR", tmp_path / "context"
        )

        unmanaged_json = q_dir / "developer.json"
        unmanaged_json.write_text(
            json.dumps(
                {
                    "name": "developer",
                    "description": "Developer",
                    "resources": ["file:///tmp/not-cao.md"],
                    "prompt": "Manual prompt",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        before = unmanaged_json.read_bytes()

        source_dir = tmp_path / "python-testing"
        _create_skill(source_dir, "python-testing", "Pytest conventions")

        result = runner.invoke(skills, ["add", str(source_dir)])

        assert result.exit_code == 0
        assert unmanaged_json.read_bytes() == before


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

    def test_remove_refreshes_cao_managed_agent_prompt(self, runner, tmp_path, monkeypatch):
        """Removing a skill should refresh CAO-managed installed agent JSONs."""
        skill_store = tmp_path / "skill-store"
        local_store = tmp_path / "agent-store"
        context_dir = tmp_path / "agent-context"
        q_dir = tmp_path / "q"
        copilot_dir = tmp_path / "copilot"
        for path in (skill_store, local_store, context_dir, q_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.AGENT_CONTEXT_DIR", context_dir
        )
        monkeypatch.setattr("cli_agent_orchestrator.utils.skill_injection.Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.COPILOT_AGENTS_DIR", copilot_dir
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.agent_profiles.LOCAL_AGENT_STORE_DIR", local_store
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_agent_dirs", lambda: {}
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs", lambda: []
        )

        (local_store / "developer.md").write_text(
            "---\nname: developer\ndescription: Developer\nprompt: Base prompt\n---\nBody\n",
            encoding="utf-8",
        )
        _create_skill(skill_store / "python-testing", "python-testing", "Pytest conventions")
        context_file = context_dir / "developer.md"
        context_file.write_text("context", encoding="utf-8")
        agent_json = q_dir / "developer.json"
        agent_json.write_text(
            json.dumps(
                {
                    "name": "developer",
                    "description": "Developer",
                    "resources": [f"file://{context_file}"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        refresh_agent_json_prompt(
            agent_json,
            AgentProfile(name="developer", description="Developer", prompt="Base prompt"),
        )

        result = runner.invoke(skills, ["remove", "python-testing"])

        assert result.exit_code == 0
        refreshed_json = json.loads(agent_json.read_text())
        assert refreshed_json["prompt"] == "Base prompt"
        assert "Refreshed 1 installed agent(s)" in result.output

    def test_remove_leaves_non_cao_managed_json_unchanged(self, runner, tmp_path, monkeypatch):
        """Non-CAO-managed installed JSONs should be untouched by skill remove."""
        skill_store = tmp_path / "skill-store"
        q_dir = tmp_path / "q"
        for path in (skill_store, q_dir):
            path.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skills.SKILLS_DIR", skill_store)
        monkeypatch.setattr("cli_agent_orchestrator.utils.skill_injection.Q_AGENTS_DIR", q_dir)
        monkeypatch.setattr(
            "cli_agent_orchestrator.utils.skill_injection.AGENT_CONTEXT_DIR", tmp_path / "context"
        )

        _create_skill(skill_store / "python-testing", "python-testing", "Pytest conventions")
        unmanaged_json = q_dir / "developer.json"
        unmanaged_json.write_text(
            json.dumps(
                {
                    "name": "developer",
                    "description": "Developer",
                    "resources": ["file:///tmp/not-cao.md"],
                    "prompt": "Manual prompt",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        before = unmanaged_json.read_bytes()

        result = runner.invoke(skills, ["remove", "python-testing"])

        assert result.exit_code == 0
        assert unmanaged_json.read_bytes() == before


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
