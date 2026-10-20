"""Public boundary regressions for unsupported vault-relative paths."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, or_
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
    VaultFindingModel,
    VaultMigrationReceiptModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.services import memory_service, settings_service
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault import migrate
from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
from cli_agent_orchestrator.services.vault import vault_lock
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig, VaultSpec
from cli_agent_orchestrator.services.vault.findings import FindingCode
from cli_agent_orchestrator.utils import atomic_file


@dataclass
class _BoundaryState:
    service: MemoryService
    vault: VaultSpec
    engine: Engine
    session: Any
    config: dict[str, VaultConfig]
    root: Path


def _boundary_state(tmp_path, monkeypatch, *, enabled: bool = True) -> _BoundaryState:
    monkeypatch.setattr(atomic_file, "LOCK_DIR", tmp_path / "atomic-locks")
    monkeypatch.setattr(vault_lock, "LOCK_DIR", tmp_path / "vault-locks")
    engine = create_engine(
        f"sqlite:///{tmp_path / 'state.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.vault.reader.SessionLocal",
        Session,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_relationship_service.SessionLocal",
        Session,
    )
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)

    root = tmp_path / "vault"
    root.mkdir()
    managed = root / "CAO"
    managed.mkdir()
    vault = VaultSpec(
        id="boundary-vault",
        root=str(root),
        managed_folder="CAO",
        mappings=[FolderMapping(folder="CAO", scope="global", writable=True)],
    )
    config = {
        "value": VaultConfig(
            enabled=enabled,
            vaults=[vault] if enabled else [],
        )
    }
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config["value"])
    service = MemoryService(base_dir=tmp_path / "native", db_engine=engine)
    monkeypatch.setattr(migrate, "MEMORY_BASE_DIR", service.base_dir)
    return _BoundaryState(service, vault, engine, Session, config, root)


def _write_refused_and_healthy(state: _BoundaryState) -> None:
    managed = state.root / "CAO"
    (managed / "\\refused.md").write_text("refused payload", encoding="utf-8")
    (managed / "healthy.md").write_text("healthy payload", encoding="utf-8")


def test_reconcile_refuses_bad_entry_without_projection_or_healthy_identity_churn(
    tmp_path, monkeypatch
) -> None:
    state = _boundary_state(tmp_path, monkeypatch)
    _write_refused_and_healthy(state)

    first = reconcile_module.reconcile(state.vault, apply=True, run_id="boundary-first")
    with state.session() as db:
        refused = db.query(VaultNoteModel).filter_by(vault_relpath="CAO/\\refused.md").one()
        healthy = db.query(VaultNoteModel).filter_by(vault_relpath="CAO/healthy.md").one()
        healthy_identity = (healthy.note_uid, healthy.cao_key)
        assert refused.status == "skipped"
        assert refused.content_sha256 is None
        assert (
            db.query(MemoryMetadataModel)
            .filter_by(
                key=refused.cao_key,
                scope=refused.scope,
                source_kind="vault",
            )
            .count()
            == 0
        )
        assert (
            db.query(MemoryRelationshipModel)
            .filter(
                or_(
                    MemoryRelationshipModel.source_key == refused.cao_key,
                    MemoryRelationshipModel.target_key == refused.cao_key,
                )
            )
            .count()
            == 0
        )
        finding = db.query(VaultFindingModel).filter_by(vault_relpath="CAO/\\refused.md").one()
        assert finding.code == FindingCode.PATH_ESCAPES_ROOT.value

    recalled = asyncio.run(
        state.service.recall(
            query="healthy payload",
            scope="global",
            search_mode="metadata",
            limit=10,
        )
    )
    assert [(item.key, item.source_kind) for item in recalled] == [(healthy_identity[1], "vault")]
    assert (
        asyncio.run(
            state.service.recall(
                query="refused payload",
                scope="global",
                search_mode="metadata",
                limit=10,
            )
        )
        == []
    )

    second = reconcile_module.reconcile(state.vault, apply=True, run_id="boundary-second")
    with state.session() as db:
        repeated_healthy = db.query(VaultNoteModel).filter_by(vault_relpath="CAO/healthy.md").one()
        assert (repeated_healthy.note_uid, repeated_healthy.cao_key) == healthy_identity
        assert db.query(VaultNoteModel).count() == 2
        assert db.query(MemoryMetadataModel).filter_by(source_kind="vault").count() == 1

    assert (first.indexed, first.skipped, first.quarantined) == (1, 1, 0)
    assert (second.indexed, second.skipped, second.quarantined) == (1, 1, 0)
    state.engine.dispose()


def test_public_store_refresh_succeeds_with_refused_entry_present(tmp_path, monkeypatch) -> None:
    state = _boundary_state(tmp_path, monkeypatch)
    (state.root / "CAO" / "\\refused.md").write_text("refused", encoding="utf-8")

    stored = asyncio.run(
        state.service.store(
            content="public store payload",
            scope="global",
            memory_type="reference",
            key="stored",
        )
    )

    assert stored.action == "created"
    assert stored.source_kind == "vault"
    assert (state.root / "CAO" / "stored.md").exists()
    with state.session() as db:
        assert (
            db.query(VaultNoteModel).filter_by(vault_relpath="CAO/\\refused.md").one().status
            == "skipped"
        )
        assert db.query(MemoryMetadataModel).filter_by(key="stored").one().source_kind == "vault"
    state.engine.dispose()


def test_public_migration_refresh_succeeds_with_refused_entry_present(
    tmp_path, monkeypatch
) -> None:
    state = _boundary_state(tmp_path, monkeypatch, enabled=False)
    asyncio.run(
        state.service.store(
            content="native migration payload",
            scope="global",
            memory_type="reference",
            key="migrated",
        )
    )
    (state.root / "CAO" / "\\refused.md").write_text("refused", encoding="utf-8")
    state.config["value"] = VaultConfig(enabled=True, vaults=[state.vault])
    binding = VaultBinding.from_spec(state.vault, state.vault.mappings[0], "global", None)

    report = migrate.migrate_scope(
        state.service,
        state.vault,
        binding,
        scope="global",
        scope_id=None,
        apply=True,
    )

    assert report.migrated == 1
    assert report.failed == 0
    assert (state.root / "CAO" / "migrated.md").exists()
    with state.session() as db:
        assert (
            db.query(VaultNoteModel).filter_by(vault_relpath="CAO/\\refused.md").one().status
            == "skipped"
        )
        assert (
            db.query(MemoryMetadataModel)
            .filter_by(key="migrated", source_kind="vault")
            .one()
            .source_kind
            == "vault"
        )
    state.engine.dispose()


def test_unsupported_receipt_relpath_keeps_native_identity_dormant(tmp_path, monkeypatch) -> None:
    state = _boundary_state(tmp_path, monkeypatch)
    with state.session() as db:
        db.add(
            VaultMigrationReceiptModel(
                receipt_id="unsupported-managed-relpath",
                scope="global",
                scope_id="",
                cao_key="migrated",
                native_relpath="global/wiki/global/migrated.md",
                native_snapshot_sha256="native-digest",
                vault_id=state.vault.id,
                managed_relpath="CAO/\\refused.md",
                vault_note_uid="receipt-note",
                published_content_sha256="published-digest",
                superseded_edges="[]",
                status="rolled_back",
            )
        )
        db.commit()

    assert state.service._native_identity_is_migration_dormant(
        "migrated",
        "global",
        None,
    )
    state.engine.dispose()
