"""CLI wiring for opt-in native-topic heading repair."""

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import memory
from cli_agent_orchestrator.services import memory_reconciliation
from cli_agent_orchestrator.services.memory_reconciliation import RepairReport


def test_add_headings_is_forwarded_only_when_explicitly_requested(monkeypatch) -> None:
    calls: list[dict] = []

    class FakeReconciliationService:
        def reconcile(self, **kwargs):
            calls.append(kwargs)
            return RepairReport(records=(), applied=kwargs["apply"])

    monkeypatch.setattr(
        memory_reconciliation,
        "MemoryReconciliationService",
        FakeReconciliationService,
    )

    ordinary = CliRunner().invoke(memory, ["repair", "--apply"])
    with_headings = CliRunner().invoke(memory, ["repair", "--apply", "--add-headings"])

    assert ordinary.exit_code == 0, ordinary.output
    assert with_headings.exit_code == 0, with_headings.output
    assert calls == [
        {"apply": True, "receipt_id": None},
        {"apply": True, "receipt_id": None, "add_headings": True},
    ]
