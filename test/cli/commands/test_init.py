"""Tests for the init CLI command."""

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.init import init, seed_default_skills


def _create_bundled_skill(root: Path, name: str, description: str) -> None:
    """Create a bundled default skill for init seeding tests."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" "# Bundled Skill\n"
    )
    (skill_dir / "extra.txt").write_text("extra")


class TestInitCommand:
    """Tests for the init command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_success(self, mock_init_db, runner):
        """Test successful initialization."""
        mock_init_db.return_value = None

        with patch("cli_agent_orchestrator.cli.commands.init.seed_default_skills") as mock_seed:
            mock_seed.return_value = 2
            result = runner.invoke(init)

        assert result.exit_code == 0
        assert "CLI Agent Orchestrator initialized successfully" in result.output
        assert "Seeded 2 builtin skills." in result.output
        mock_init_db.assert_called_once()
        mock_seed.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_failure(self, mock_init_db, runner):
        """Test initialization failure."""
        mock_init_db.side_effect = Exception("Database error")

        result = runner.invoke(init)

        assert result.exit_code != 0
        assert "Database error" in result.output
        mock_init_db.assert_called_once()

    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_permission_error(self, mock_init_db, runner):
        """Test initialization with permission error."""
        mock_init_db.side_effect = PermissionError("Permission denied")

        result = runner.invoke(init)

        assert result.exit_code != 0
        assert "Permission denied" in result.output


class TestSeedDefaultSkills:
    """Tests for default skill seeding during init."""

    def test_seed_default_skills_creates_store_and_copies_bundled_skills(
        self, tmp_path, monkeypatch
    ):
        """Bundled skills should be copied into the local skill store."""
        bundled_root = tmp_path / "bundled"
        _create_bundled_skill(bundled_root, "alpha", "Alpha skill")
        _create_bundled_skill(bundled_root, "beta", "Beta skill")

        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.init.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.init.resources.files", lambda _: bundled_root
        )

        seeded_count = seed_default_skills()

        assert (skill_store / "alpha" / "SKILL.md").exists()
        assert (skill_store / "alpha" / "extra.txt").read_text() == "extra"
        assert (skill_store / "beta" / "SKILL.md").exists()
        assert seeded_count == 2

    def test_seed_default_skills_skips_existing_skills(self, tmp_path, monkeypatch):
        """Existing installed skills should not be overwritten on re-run."""
        bundled_root = tmp_path / "bundled"
        _create_bundled_skill(bundled_root, "alpha", "Bundled alpha")

        skill_store = tmp_path / "skill-store"
        existing_dir = skill_store / "alpha"
        existing_dir.mkdir(parents=True)
        (existing_dir / "SKILL.md").write_text("---\nname: alpha\ndescription: User edit\n---\n")
        (existing_dir / "custom.txt").write_text("keep me")

        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.init.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.init.resources.files", lambda _: bundled_root
        )

        seeded_count = seed_default_skills()

        assert (existing_dir / "custom.txt").read_text() == "keep me"
        assert "User edit" in (existing_dir / "SKILL.md").read_text()
        assert seeded_count == 0

    def test_seed_default_skills_seeds_new_bundled_skills_on_rerun(self, tmp_path, monkeypatch):
        """Re-running init should seed newly added bundled skills without replacing old ones."""
        bundled_root = tmp_path / "bundled"
        _create_bundled_skill(bundled_root, "alpha", "Alpha skill")

        skill_store = tmp_path / "skill-store"
        monkeypatch.setattr("cli_agent_orchestrator.cli.commands.init.SKILLS_DIR", skill_store)
        monkeypatch.setattr(
            "cli_agent_orchestrator.cli.commands.init.resources.files", lambda _: bundled_root
        )

        first_seed_count = seed_default_skills()

        _create_bundled_skill(bundled_root, "beta", "Beta skill")
        second_seed_count = seed_default_skills()

        assert (skill_store / "alpha" / "SKILL.md").exists()
        assert (skill_store / "beta" / "SKILL.md").exists()
        assert first_seed_count == 1
        assert second_seed_count == 1
