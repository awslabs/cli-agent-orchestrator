"""Tests for the init CLI command."""

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands.init import init, seed_default_skills
from cli_agent_orchestrator.clients import database as db_module
from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import memory_reconciliation
from cli_agent_orchestrator.services.memory_reconciliation import (
    MemoryIdentity,
    MemoryReconciliationError,
    RepairAction,
    RepairFinding,
    RepairRecord,
    RepairReport,
)


def _create_bundled_skill(root: Path, name: str, description: str) -> None:
    """Create a bundled default skill for init seeding tests."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n" f"name: {name}\n" f"description: {description}\n" "---\n\n" "# Bundled Skill\n"
    )
    (skill_dir / "extra.txt").write_text("extra")


def _write_agent_topic(base: Path, key: str) -> None:
    path = base / "global" / "wiki" / "agent" / "code_supervisor" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {key}\n"
        f"<!-- id: {uuid.uuid4()} | scope: agent | "
        "type: reference | tags: init repair -->\n\n"
        "## 2026-07-16T02:00:00Z\n"
        "surviving content\n",
        encoding="utf-8",
    )


class TestInitCommand:
    """Tests for the init command."""

    @pytest.fixture
    def runner(self):
        """Create a CLI test runner."""
        return CliRunner()

    @pytest.fixture(autouse=True)
    def isolate_memory_reconciliation(self):
        """Keep command tests away from the developer's configured memory root."""
        with patch(
            "cli_agent_orchestrator.cli.commands.init.reconcile_memory_startup",
            return_value=None,
        ):
            yield

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

    @patch("cli_agent_orchestrator.cli.commands.init.seed_default_skills")
    @patch("cli_agent_orchestrator.cli.commands.init.reconcile_memory_startup")
    @patch("cli_agent_orchestrator.cli.commands.init.init_db")
    def test_init_exits_nonzero_after_reconciliation_failure(
        self, mock_init_db, mock_reconcile, mock_seed, runner
    ):
        """Schema initialization remains complete when bounded repair fails."""
        report = RepairReport(
            records=(
                RepairRecord(
                    identity=MemoryIdentity("failed", "global"),
                    file_path="/memory/failed.md",
                    actions=(RepairAction.FAILED,),
                    status="failed",
                    finding=RepairFinding("unexpected_error", "injected"),
                ),
            ),
            applied=True,
        )
        mock_reconcile.side_effect = MemoryReconciliationError(report)

        result = runner.invoke(init)

        assert result.exit_code != 0
        mock_init_db.assert_called_once()
        mock_seed.assert_not_called()
        assert "failed=1" in result.output
        assert "cao memory repair --apply" in result.output

    def test_init_repairs_replacement_database_and_rerun_is_noop(
        self, tmp_path, monkeypatch, runner
    ):
        """The command initializes schema before running the real bounded repair."""
        base = tmp_path / "memory"
        _write_agent_topic(base, "surviving-agent")
        replacement_engine = create_engine(
            f"sqlite:///{tmp_path / 'replacement.db'}",
            connect_args={"check_same_thread": False},
        )
        sessions = sessionmaker(autocommit=False, autoflush=False, bind=replacement_engine)
        monkeypatch.setattr(db_module, "SessionLocal", sessions)
        monkeypatch.setattr(memory_reconciliation, "MEMORY_BASE_DIR", base)

        def initialize_replacement() -> None:
            Base.metadata.create_all(bind=replacement_engine)

        with (
            patch(
                "cli_agent_orchestrator.cli.commands.init.init_db",
                side_effect=initialize_replacement,
            ),
            patch(
                "cli_agent_orchestrator.cli.commands.init.reconcile_memory_startup",
                side_effect=memory_reconciliation.reconcile_memory_startup,
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
                return_value=True,
            ),
            patch(
                "cli_agent_orchestrator.cli.commands.init.seed_default_skills",
                return_value=0,
            ),
        ):
            first = runner.invoke(init)
            second = runner.invoke(init)

        assert first.exit_code == 0
        assert "repaired=1" in first.output
        assert second.exit_code == 0
        assert "unchanged=1" in second.output
        with sessions() as db:
            row = db.query(MemoryMetadataModel).one()
            assert (row.key, row.scope, row.scope_id) == (
                "surviving-agent",
                "agent",
                "code_supervisor",
            )


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
