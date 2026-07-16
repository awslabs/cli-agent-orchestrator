"""CLI tests for ``cao memory repair``."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import repair_cmd
from cli_agent_orchestrator.services.memory_reconciliation import (
    MemoryIdentity,
    MemoryReconciliationError,
    RepairAction,
    RepairFinding,
    RepairRecord,
    RepairReport,
)


def test_repair_is_dry_run_by_default() -> None:
    service = MagicMock()
    service.reconcile.return_value = RepairReport(
        records=(
            RepairRecord(
                identity=MemoryIdentity("topic", "global"),
                file_path="/memory/topic.md",
                actions=(RepairAction.CREATE_METADATA,),
                status="planned",
            ),
        ),
        applied=False,
    )
    with patch(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        return_value=service,
    ):
        result = CliRunner().invoke(repair_cmd)

    assert result.exit_code == 0
    service.reconcile.assert_called_once_with(apply=False)
    assert "mode=dry-run" in result.output
    assert "create_metadata" in result.output


def test_repair_apply_and_unresolved_exit_codes() -> None:
    service = MagicMock()
    service.reconcile.return_value = RepairReport(
        records=(
            RepairRecord(
                identity=MemoryIdentity("bad", "global"),
                file_path="/memory/bad.md",
                actions=(RepairAction.MALFORMED,),
                status="skipped",
                finding=RepairFinding("malformed_header", "metadata header is malformed"),
            ),
        ),
        applied=True,
    )
    with patch(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        return_value=service,
    ):
        result = CliRunner().invoke(repair_cmd, ["--apply"])

    assert result.exit_code == 1
    service.reconcile.assert_called_once_with(apply=True)
    assert "mode=apply" in result.output
    assert "metadata header is malformed" in result.output


def test_repair_failure_also_renders_skipped_actionable_findings() -> None:
    service = MagicMock()
    report = RepairReport(
        records=(
            RepairRecord(
                identity=MemoryIdentity("malformed", "global"),
                file_path="/memory/malformed.md",
                actions=(RepairAction.MALFORMED,),
                status="skipped",
                finding=RepairFinding("malformed_header", "fix the metadata header"),
            ),
            RepairRecord(
                identity=MemoryIdentity("failed", "global"),
                file_path="/memory/failed.md",
                actions=(RepairAction.FAILED,),
                status="failed",
                finding=RepairFinding("unexpected_error", "retry the repair"),
            ),
        ),
        applied=True,
    )
    service.reconcile.side_effect = MemoryReconciliationError(report)
    with patch(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        return_value=service,
    ):
        result = CliRunner().invoke(repair_cmd, ["--apply"])

    assert result.exit_code != 0
    assert "fix the metadata header" in result.output
    assert "retry the repair" in result.output
