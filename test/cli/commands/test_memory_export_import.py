"""Tests for `cao memory export` / `cao memory import` (#345 Unit 3, D6).

Follows test/cli/commands/test_memory.py: mock MemoryService via the
_get_memory_service factory to isolate command logic; a couple of tests
run against a real service in tmp dirs for the end-to-end CLI path.
"""

import asyncio
import tarfile
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine

from cli_agent_orchestrator.cli.commands.memory import export_cmd, import_cmd
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services.memory_archive.base import ExportReport, ImportReport
from cli_agent_orchestrator.services.memory_service import MemoryDisabledError, MemoryService

FACTORY = "cli_agent_orchestrator.cli.commands.memory._get_memory_service"


@pytest.fixture
def runner():
    return CliRunner()


def _real_svc(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'cli.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return MemoryService(base_dir=tmp_path / "memory", db_engine=engine)


class TestExportPrivateScopeGate:
    """D5: session/agent error without --include-private, before any write."""

    @pytest.mark.parametrize("scope", ["session", "agent"])
    def test_private_scope_errors_without_flag(self, runner, tmp_path, scope):
        svc = MagicMock()
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(export_cmd, ["--scope", scope, "-o", str(tmp_path / "out")])
        assert result.exit_code != 0
        assert "--include-private" in result.output
        # Whole-command error: no service call, nothing written.
        svc.export_memories.assert_not_called()
        assert not (tmp_path / "out").exists()

    def test_private_scope_allowed_with_flag(self, runner, tmp_path):
        svc = MagicMock()
        svc.export_memories.return_value = ExportReport(exported=1)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(
                export_cmd,
                ["--scope", "session", "-o", str(tmp_path / "out"), "--include-private"],
            )
        assert result.exit_code == 0, result.output
        svc.export_memories.assert_called_once()


class TestExportCommand:
    def test_output_is_required(self, runner):
        result = runner.invoke(export_cmd, ["--scope", "global"])
        assert result.exit_code != 0
        assert "-o" in result.output or "output" in result.output.lower()

    def test_unknown_format_is_cli_error(self, runner, tmp_path):
        svc = _real_svc(tmp_path)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(
                export_cmd, ["--scope", "global", "--format", "nope", "-o", str(tmp_path / "o")]
            )
        assert result.exit_code != 0
        assert "Unknown memory archive format" in result.output

    def test_export_dir_end_to_end_prints_report(self, runner, tmp_path):
        svc = _real_svc(tmp_path)
        asyncio.run(svc.store(content="cli fact", scope="global", key="cli-topic"))
        dest = tmp_path / "bundle"
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(export_cmd, ["--scope", "global", "-o", str(dest)])
        assert result.exit_code == 0, result.output
        assert "exported: 1" in result.output
        assert (dest / "cli-topic.md").exists()

    def test_export_empty_scope_yields_valid_bundle(self, runner, tmp_path):
        svc = _real_svc(tmp_path)
        dest = tmp_path / "empty-bundle"
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(export_cmd, ["--scope", "global", "-o", str(dest)])
        assert result.exit_code == 0, result.output
        assert "exported: 0" in result.output
        assert sorted(p.name for p in dest.glob("*.md")) == ["index.md", "manifest.md"]

    def test_tar_gz_output_routes_to_tar_helper(self, runner, tmp_path):
        svc = _real_svc(tmp_path)
        asyncio.run(svc.store(content="tar fact", scope="global", key="tar-topic"))
        tar_path = tmp_path / "out.tar.gz"
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(export_cmd, ["--scope", "global", "-o", str(tar_path)])
        assert result.exit_code == 0, result.output
        with tarfile.open(tar_path, "r:gz") as tar:
            names = sorted(tar.getnames())
        assert names == ["index.md", "manifest.md", "tar-topic.md"]


class TestImportCommand:
    def test_scope_choices_exclude_private(self, runner, tmp_path):
        bundle = tmp_path / "b"
        bundle.mkdir()
        for scope in ("session", "agent"):
            result = runner.invoke(import_cmd, [str(bundle), "--scope", scope])
            assert result.exit_code != 0
            assert "Invalid value" in result.output

    def test_unknown_format_is_cli_error(self, runner, tmp_path):
        bundle = tmp_path / "b"
        bundle.mkdir()
        svc = _real_svc(tmp_path)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(
                import_cmd, [str(bundle), "--scope", "global", "--format", "nope"]
            )
        assert result.exit_code != 0
        assert "Unknown memory archive format" in result.output

    def test_import_end_to_end_prints_report(self, runner, tmp_path):
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "imported-topic.md").write_text(
            "---\ntype: reference\n---\nimported body\n", encoding="utf-8"
        )
        svc = _real_svc(tmp_path)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(import_cmd, [str(bundle), "--scope", "global"])
        assert result.exit_code == 0, result.output
        assert "imported: 1" in result.output
        assert svc.get_wiki_path("global", None, "imported-topic").exists()

    def test_dry_run_echoes_resolved_project_id(self, runner, tmp_path, monkeypatch):
        """Design test 13, CLI surface: dry-run output carries the project id."""
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "topic.md").write_text("---\ntype: reference\n---\nbody\n", encoding="utf-8")
        project_dir = tmp_path / "proj"
        project_dir.mkdir()
        monkeypatch.chdir(project_dir)
        svc = _real_svc(tmp_path)
        expected = svc.resolve_scope_id("project", {"cwd": str(project_dir)})
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(import_cmd, [str(bundle), "--scope", "project", "--dry-run"])
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert expected in result.output
        assert not svc.get_wiki_path("project", expected, "topic").exists()

    def test_missing_bundle_is_cli_error(self, runner, tmp_path):
        svc = _real_svc(tmp_path)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(import_cmd, [str(tmp_path / "nope"), "--scope", "global"])
        assert result.exit_code != 0
        assert "does not exist" in result.output

    def test_conflict_and_dry_run_flags_forwarded(self, runner, tmp_path):
        bundle = tmp_path / "b"
        bundle.mkdir()
        svc = MagicMock()
        svc.import_memories.return_value = ImportReport(target_scope="global", dry_run=True)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(
                import_cmd,
                [str(bundle), "--scope", "global", "--conflict", "merge", "--dry-run"],
            )
        assert result.exit_code == 0, result.output
        kwargs = svc.import_memories.call_args.kwargs
        assert kwargs["conflict_policy"] == "merge"
        assert kwargs["dry_run"] is True


class TestImportErrorMapping:
    """Domain errors from import map to clean ClickExceptions, no traceback."""

    @pytest.mark.parametrize(
        "exc",
        [
            MemoryDisabledError("memory subsystem is disabled"),
            PermissionError("caller scope 'project' may not write target scope 'global'"),
        ],
    )
    def test_domain_error_is_clean_cli_error(self, runner, tmp_path, exc):
        bundle = tmp_path / "b"
        bundle.mkdir()
        svc = MagicMock()
        svc.import_memories.side_effect = exc
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(import_cmd, [str(bundle), "--scope", "global"])
        assert result.exit_code != 0
        assert str(exc) in result.output
        assert "Traceback" not in result.output

    def test_disabled_memory_import_end_to_end(self, runner, tmp_path, monkeypatch):
        # Real service with memory.enabled=False: import must surface the
        # disabled message as a CLI error, not a traceback.
        bundle = tmp_path / "b"
        bundle.mkdir()
        (bundle / "topic.md").write_text("---\ntype: project\n---\nbody\n", encoding="utf-8")
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
            lambda: False,
        )
        svc = _real_svc(tmp_path)
        with patch(FACTORY, return_value=svc):
            result = runner.invoke(import_cmd, [str(bundle), "--scope", "global"])
        assert result.exit_code != 0
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "disabled" in result.output
        assert "Traceback" not in result.output
