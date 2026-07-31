"""Tests for `cao memory promote` (Phase 2).

CLI tests mock PromotionService to isolate command logic, mirroring
test_memory.py conventions.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import promote_cmd
from cli_agent_orchestrator.services.promotion_service import (
    PromotionCandidate,
    PromotionDisabledError,
    PromotionPlan,
    PromotionReport,
)

SVC_TARGET = "cli_agent_orchestrator.services.promotion_service.PromotionService"


def _plan(profile_path: Path, candidates=None) -> PromotionPlan:
    plan = PromotionPlan(agent_profile="transformer", profile_path=profile_path)
    plan.candidates = candidates or []
    return plan


def _candidate(key: str = "k1", action: str = "add") -> PromotionCandidate:
    return PromotionCandidate(key=key, text="Lesson text.", access_count=5, action=action)


class TestPromoteCmd:
    def test_dry_run_default(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile, [_candidate()])
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile)]
            )
        assert result.exit_code == 0, result.output
        assert "DRY RUN" in result.output
        assert "[add] k1" in result.output
        mock_svc.apply.assert_not_called()

    def test_apply(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile, [_candidate()])
        mock_svc.apply.return_value = PromotionReport(
            agent_profile="transformer", added=["k1"], updated=[], skipped=[]
        )
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile), "--apply"]
            )
        assert result.exit_code == 0, result.output
        assert "added=1" in result.output
        mock_svc.apply.assert_called_once()

    def test_empty_plan(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile)
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile), "--apply"]
            )
        assert result.exit_code == 0
        assert "No promotable lessons" in result.output
        mock_svc.apply.assert_not_called()

    def test_disabled_is_clean_error(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile, [_candidate()])
        mock_svc.apply.side_effect = PromotionDisabledError("instruction promotion is disabled")
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile), "--apply"]
            )
        assert result.exit_code != 0
        assert "disabled" in result.output

    def test_missing_profile_path_is_error(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            promote_cmd,
            ["transformer", "--profile-path", str(tmp_path / "ghost.md")],
        )
        assert result.exit_code != 0
        assert "not found" in result.output

    def test_min_recalls_forwarded(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile)
        with patch(SVC_TARGET, return_value=mock_svc):
            CliRunner().invoke(
                promote_cmd,
                ["transformer", "--profile-path", str(profile), "--min-recalls", "7"],
            )
        assert mock_svc.plan.call_args.kwargs["min_access_count"] == 7


class TestResolveProfilePath:
    def test_finds_flat_profile_in_agent_dir(self, tmp_path: Path) -> None:
        from cli_agent_orchestrator.cli.commands.memory import _resolve_profile_path

        agent_dir = tmp_path / "agents"
        agent_dir.mkdir()
        (agent_dir / "transformer.md").write_text("# T\n")
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value={"claude_code": str(agent_dir)},
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            ),
        ):
            assert _resolve_profile_path("transformer") == agent_dir / "transformer.md"

    def test_finds_nested_profile_layout(self, tmp_path: Path) -> None:
        from cli_agent_orchestrator.cli.commands.memory import _resolve_profile_path

        agent_dir = tmp_path / "agents"
        (agent_dir / "transformer").mkdir(parents=True)
        (agent_dir / "transformer" / "agent.md").write_text("# T\n")
        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value={"claude_code": str(agent_dir)},
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            ),
        ):
            assert _resolve_profile_path("transformer") == agent_dir / "transformer" / "agent.md"

    def test_missing_profile_is_click_error(self, tmp_path: Path) -> None:
        import click

        from cli_agent_orchestrator.cli.commands.memory import _resolve_profile_path

        with (
            patch(
                "cli_agent_orchestrator.services.settings_service.get_agent_dirs",
                return_value={"claude_code": str(tmp_path / "empty")},
            ),
            patch(
                "cli_agent_orchestrator.services.settings_service.get_extra_agent_dirs",
                return_value=[],
            ),
        ):
            with pytest.raises(click.ClickException, match="No writable profile"):
                _resolve_profile_path("ghost")


class TestPromoteSkippedOutput:
    def test_skipped_lessons_reported(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile, [_candidate()])
        mock_svc.apply.return_value = PromotionReport(
            agent_profile="transformer", added=[], updated=[], skipped=["overflow-key"]
        )
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile), "--apply"]
            )
        assert result.exit_code == 0
        assert "overflow-key" in result.output


class TestBuiltinProfileRefusal:
    """--profile-path must not bypass the built-in-package-profile refusal.

    Review finding (PR #515): the refusal lived only in _resolve_profile_path,
    so an explicit --profile-path pointing into the installed agent_store
    could mutate a bundled profile.
    """

    def test_profile_path_into_builtin_store_is_refused(self) -> None:
        import cli_agent_orchestrator.agent_store as agent_store_pkg

        builtin = Path(next(iter(agent_store_pkg.__path__))) / "retrospector.md"
        assert builtin.is_file(), "test needs a real built-in profile"
        mock_svc = MagicMock()
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["retrospector", "--profile-path", str(builtin)]
            )
        assert result.exit_code != 0
        assert "built-in" in result.output.lower()
        mock_svc.plan.assert_not_called()

    def test_agent_dir_profile_path_still_allowed(self, tmp_path: Path) -> None:
        profile = tmp_path / "transformer.md"
        profile.write_text("# T\n")
        mock_svc = MagicMock()
        mock_svc.plan.return_value = _plan(profile)
        with patch(SVC_TARGET, return_value=mock_svc):
            result = CliRunner().invoke(
                promote_cmd, ["transformer", "--profile-path", str(profile)]
            )
        assert result.exit_code == 0
        mock_svc.plan.assert_called_once()
