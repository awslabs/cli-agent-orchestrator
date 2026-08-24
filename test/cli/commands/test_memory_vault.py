"""CLI boundary tests for the local-only vault maintenance commands."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

from click.testing import CliRunner

from cli_agent_orchestrator.cli.commands.memory import memory
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.migrate import MigrationReport
from cli_agent_orchestrator.services.vault.reconcile import ReconcileReport
from cli_agent_orchestrator.services.vault.status import VaultStatus


def _config(vault: object) -> SimpleNamespace:
    return SimpleNamespace(enabled=True, vaults=[vault])


def _binding(vault: object) -> VaultBinding:
    mapping = SimpleNamespace(writable=True)
    return VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="vault",
        root="/fixture/vault",
        mapping=mapping,
    )


def test_vault_status_labels_unmapped_writes_as_process_local(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import status

    configured_vault = SimpleNamespace(id="vault")
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: _config(configured_vault))
    monkeypatch.setattr(
        status,
        "get_vault_status",
        lambda _config: (
            VaultStatus(
                "vault",
                (("indexed", 2),),
                (),
                (),
                7,
            ),
        ),
    )

    result = CliRunner().invoke(memory, ["vault", "status"])

    assert result.exit_code == 0
    assert (
        "process_local_unmapped_project_writes: unavailable in a fresh CLI process" in result.output
    )
    assert "7" not in result.output


def test_vault_scan_is_read_only_and_accepts_dry_run(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import scan

    configured_vault = SimpleNamespace(id="vault")
    scanned = []
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: _config(configured_vault))
    monkeypatch.setattr(
        scan,
        "scan_vault",
        lambda vault: scanned.append(vault)
        or SimpleNamespace(notes=(), total_bytes_scanned=0, max_total_bytes=1024),
    )

    result = CliRunner().invoke(memory, ["vault", "scan", "--dry-run"])

    assert result.exit_code == 0
    assert scanned == [configured_vault]
    assert "Scanned 0 note(s)" in result.output


def test_vault_reconcile_defaults_to_dry_run_and_forwards_apply(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import reconcile

    configured_vault = SimpleNamespace(id="vault")
    calls = []
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: _config(configured_vault))
    monkeypatch.setattr(
        reconcile,
        "reconcile",
        lambda vault, *, apply: calls.append((vault, apply))
        or ReconcileReport("vault", "run", 0, 0, 0, 0, 0, False),
    )

    dry_run = CliRunner().invoke(memory, ["vault", "reconcile"])
    applied = CliRunner().invoke(memory, ["vault", "reconcile", "--apply"])

    assert dry_run.exit_code == 0
    assert applied.exit_code == 0
    assert calls == [(configured_vault, False), (configured_vault, True)]


def test_vault_rebuild_requires_apply_and_names_access_count_reset(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import reconcile

    configured_vault = SimpleNamespace(id="vault")
    rebuilt = []
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: _config(configured_vault))
    monkeypatch.setattr(
        reconcile,
        "rebuild",
        lambda vault: rebuilt.append(vault) or ReconcileReport("vault", "run", 0, 0, 0, 0, 0, True),
    )

    refused = CliRunner().invoke(memory, ["vault", "rebuild"])
    applied = CliRunner().invoke(memory, ["vault", "rebuild", "--apply"])
    help_result = CliRunner().invoke(memory, ["vault", "rebuild", "--help"])

    assert refused.exit_code != 0
    assert "--apply is required" in refused.output
    assert applied.exit_code == 0
    assert rebuilt == [configured_vault]
    assert "vault access counts reset" in help_result.output


def test_vault_migrate_forwards_dry_run_apply_and_confirmed_delete(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import binding, migrate

    configured_vault = SimpleNamespace(id="vault")
    bound = _binding(configured_vault)
    calls = []
    service = MagicMock()
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: _config(configured_vault))
    monkeypatch.setattr(binding, "resolve", lambda *_args, **_kwargs: bound)
    monkeypatch.setattr(
        migrate,
        "migrate_scope",
        lambda *args, **kwargs: calls.append((args, kwargs))
        or MigrationReport(dry_run=not kwargs["apply"]),
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.memory._get_memory_service",
        lambda: service,
    )

    dry_run = CliRunner().invoke(memory, ["vault", "migrate", "--scope", "global"])
    applied = CliRunner().invoke(memory, ["vault", "migrate", "--scope", "global", "--apply"])
    deleted = CliRunner().invoke(
        memory,
        [
            "vault",
            "migrate",
            "--scope",
            "global",
            "--apply",
            "--delete-source",
            "--confirm-delete-source",
        ],
    )

    assert dry_run.exit_code == 0
    assert applied.exit_code == 0
    assert deleted.exit_code == 0
    assert [kwargs["apply"] for _args, kwargs in calls] == [False, True, True]
    assert [kwargs["delete_source"] for _args, kwargs in calls] == [False, False, True]
    assert [kwargs["confirm_delete_source"] for _args, kwargs in calls] == [False, False, True]


def test_vault_migrate_delete_source_requires_both_named_flags() -> None:
    runner = CliRunner()

    missing_apply = runner.invoke(
        memory,
        ["vault", "migrate", "--scope", "global", "--delete-source"],
    )
    missing_confirmation = runner.invoke(
        memory,
        ["vault", "migrate", "--scope", "global", "--apply", "--delete-source"],
    )

    assert missing_apply.exit_code != 0
    assert "--delete-source requires --apply" in missing_apply.output
    assert missing_confirmation.exit_code != 0
    assert "--delete-source requires --confirm-delete-source" in missing_confirmation.output
