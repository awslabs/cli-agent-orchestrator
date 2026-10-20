"""Lifecycle regressions for durable native-to-vault migration receipts."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands.memory import memory
from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
    VaultExclusionModel,
    VaultMigrationReceiptModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.services import memory_service, settings_service
from cli_agent_orchestrator.services.memory_reconciliation import (
    MemoryReconciliationService,
    RepairAction,
)
from cli_agent_orchestrator.services.memory_relationship_service import (
    MemoryRelationshipService,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault import migrate
from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
from cli_agent_orchestrator.services.vault import vault_lock
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig, VaultSpec
from cli_agent_orchestrator.services.vault.parser import parse_note
from cli_agent_orchestrator.services.vault.status import (
    migration_receipt_inconsistency_reason,
)
from cli_agent_orchestrator.services.vault.writer import write_managed_note
from cli_agent_orchestrator.utils import atomic_file


@dataclass
class _Lifecycle:
    service: MemoryService
    vault: VaultSpec
    binding: VaultBinding
    session: object
    config: dict[str, VaultConfig]


def _lifecycle(tmp_path, monkeypatch) -> _Lifecycle:
    lock_dir = tmp_path / "locks"
    monkeypatch.setattr(atomic_file, "LOCK_DIR", lock_dir)
    monkeypatch.setattr(vault_lock, "LOCK_DIR", lock_dir)
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_relationship_service.SessionLocal",
        Session,
    )
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    vault = VaultSpec(
        id="receipt-vault",
        root=str(root),
        managed_folder="CAO",
        mappings=[FolderMapping(folder="CAO", scope="global", writable=True)],
    )
    config = {"value": VaultConfig(enabled=False)}
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config["value"])
    service = MemoryService(base_dir=tmp_path / "native", db_engine=engine)
    monkeypatch.setattr(migrate, "MEMORY_BASE_DIR", service.base_dir)
    mapping = vault.mappings[0]
    return _Lifecycle(
        service=service,
        vault=vault,
        binding=VaultBinding.from_spec(vault, mapping, "global", None),
        session=Session,
        config=config,
    )


def _store(state: _Lifecycle, content: str, *, key: str = "migrated"):
    return asyncio.run(
        state.service.store(
            content=content,
            scope="global",
            memory_type="reference",
            key=key,
        )
    )


def _migrate(state: _Lifecycle):
    state.config["value"] = VaultConfig(enabled=True, vaults=[state.vault])
    return migrate.migrate_scope(
        state.service,
        state.vault,
        state.binding,
        scope="global",
        scope_id=None,
        apply=True,
    )


def _assert_native_is_dormant(
    state: _Lifecycle,
    tmp_path,
    monkeypatch,
    *,
    query: str = "retained",
) -> None:
    for search_mode in ("metadata", "bm25"):
        recalled = asyncio.run(
            state.service.recall(
                query=query,
                scope="global",
                search_mode=search_mode,
                limit=100,
            )
        )
        assert all(item.source_kind != "native" for item in recalled)
    assert query not in state.service.get_memory_context(
        {"cwd": str(tmp_path), "terminal_id": "worker"},
        budget_chars=10_000,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.memory._get_memory_service",
        lambda: state.service,
    )
    listed = CliRunner().invoke(memory, ["list", "--scope", "global"])
    assert listed.exit_code == 0
    assert "migrated" not in listed.output


def test_default_migration_receipt_allows_exact_matching_store_update(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "native history")

    report = _migrate(state)

    assert report.migrated == 1
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        baseline = receipt.published_content_sha256
        assert receipt.status == "active"
        assert receipt.scope == "global"
        assert receipt.scope_id == ""
        assert receipt.cao_key == "migrated"
        assert receipt.native_relpath == "global/wiki/global/migrated.md"
        assert receipt.managed_relpath == "CAO/migrated.md"
        assert receipt.superseded_edges == "[]"

    target = tmp_path / "vault" / "CAO" / "migrated.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nHuman canonical edit.\n",
        encoding="utf-8",
    )
    reconcile_module.reconcile(state.vault, apply=True)
    updated = _store(state, "post-migration append")
    _store(state, "second post-migration append")

    assert updated.source_kind == "vault"
    body = target.read_text(encoding="utf-8")
    assert body.count("native history") == 1
    assert body.count("Human canonical edit.") == 1
    assert body.count("\npost-migration append\n") == 1
    assert body.count("second post-migration append") == 1
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        assert receipt.status == "active"
        assert receipt.published_content_sha256 == baseline


def test_rerun_after_vault_edit_skips_publication(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    target = tmp_path / "vault" / "CAO" / "migrated.md"
    target.write_text(
        target.read_text(encoding="utf-8") + "\nHuman canonical edit.\n",
        encoding="utf-8",
    )
    reconcile_module.reconcile(state.vault, apply=True)
    before = target.read_bytes()
    with state.session() as db:
        baseline = db.query(VaultMigrationReceiptModel).one().published_content_sha256

    report = _migrate(state)

    assert report.migrated == 0
    assert report.skipped_vault_authoritative == {"migrated": "target_content_drift"}
    assert target.read_bytes() == before
    with state.session() as db:
        assert db.query(VaultMigrationReceiptModel).one().published_content_sha256 == baseline


def test_rerun_after_native_source_drift_skips_without_rewriting_receipt(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    report = _migrate(state)
    assert report.failed == 0, report.errors
    target = tmp_path / "vault" / "CAO" / "migrated.md"
    before = target.read_bytes()
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        baseline = receipt.published_content_sha256
        native_baseline = receipt.native_snapshot_sha256
    state.service.get_wiki_path("global", None, "migrated").write_text(
        "operator changed retained native source",
        encoding="utf-8",
    )

    report = _migrate(state)

    assert report.migrated == 0
    assert report.skipped_vault_authoritative == {"migrated": "native_source_drift"}
    assert target.read_bytes() == before
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        assert receipt.published_content_sha256 == baseline
        assert receipt.native_snapshot_sha256 == native_baseline


def test_exact_rerun_is_idempotent_without_republishing(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    target = tmp_path / "vault" / "CAO" / "migrated.md"
    before = target.read_bytes()

    monkeypatch.setattr(
        migrate,
        "write_managed_note",
        lambda **_kwargs: pytest.fail("idempotent rerun must not republish"),
    )
    report = _migrate(state)

    assert report.migrated == 0
    assert report.failed == 0
    assert report.skipped_vault_authoritative == {"migrated": "already_migrated"}
    assert target.read_bytes() == before


@pytest.mark.parametrize("apply", (False, True))
@pytest.mark.parametrize(
    "control",
    ("native_source_changed", "target_human_edited", "canonical_archived", "unmodified"),
)
def test_rolled_back_receipt_refuses_remigration_in_dry_run_and_apply(
    tmp_path, monkeypatch, control: str, apply: bool
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
    rolled_back = repair.reconcile(apply=True, receipt_id=receipt_id)
    assert rolled_back.records[0].status == "repaired"

    canonical = tmp_path / "vault" / "CAO" / "migrated.md"
    observed = canonical
    if control == "native_source_changed":
        native = state.service.get_wiki_path("global", None, "migrated")
        native.write_text(
            native.read_text(encoding="utf-8") + "\nchanged after rollback\n",
            encoding="utf-8",
        )
    elif control == "target_human_edited":
        canonical.write_text(
            canonical.read_text(encoding="utf-8") + "\nHuman edit after rollback.\n",
            encoding="utf-8",
        )
        reconcile_module.reconcile(state.vault, apply=True)
    elif control == "canonical_archived":
        archive = tmp_path / "vault" / "Archive"
        archive.mkdir()
        observed = archive / "migrated.md"
        canonical.rename(observed)
        reconcile_module.reconcile(state.vault, apply=True)
    before_bytes = observed.read_bytes()
    with state.session() as db:
        receipt = db.get(VaultMigrationReceiptModel, receipt_id)
        before_receipt = (
            receipt.status,
            receipt.native_snapshot_sha256,
            receipt.published_content_sha256,
            receipt.superseded_edges,
        )
        before_note_count = db.query(VaultNoteModel).count()
        before_exclusion_count = db.query(VaultExclusionModel).count()

    report = migrate.migrate_scope(
        state.service,
        state.vault,
        state.binding,
        scope="global",
        scope_id=None,
        apply=apply,
    )

    assert report.skipped_vault_authoritative == {"migrated": "receipt_not_active"}
    assert report.migrated == 0
    assert report.failed == 0
    assert report.errors == {}
    assert observed.read_bytes() == before_bytes
    if control == "canonical_archived":
        assert not canonical.exists()
    with state.session() as db:
        receipt = db.get(VaultMigrationReceiptModel, receipt_id)
        assert (
            receipt.status,
            receipt.native_snapshot_sha256,
            receipt.published_content_sha256,
            receipt.superseded_edges,
        ) == before_receipt
        assert db.query(VaultMigrationReceiptModel).count() == 1
        assert db.query(VaultNoteModel).count() == before_note_count
        assert db.query(VaultExclusionModel).count() == before_exclusion_count


def test_remigration_refusal_is_scoped_to_the_rolled_back_identity(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "first native bytes")
    _store(state, "target native bytes", key="target")
    relationships = MemoryRelationshipService()
    restored = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        status="proposal",
    )
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    assert repair.reconcile(apply=True, receipt_id=receipt_id).records[0].status == "repaired"
    state.config["value"] = VaultConfig(enabled=False)
    _store(state, "second native bytes", key="second")

    report = _migrate(state)

    assert report.planned == 2
    assert report.migrated == 1
    assert report.failed == 0
    assert report.skipped_vault_authoritative == {"migrated": "receipt_not_active"}
    assert (tmp_path / "vault" / "CAO" / "second.md").is_file()
    with state.session() as db:
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "rolled_back"
        assert db.query(VaultMigrationReceiptModel).count() == 2
        assert db.get(MemoryRelationshipModel, restored.id).status == "proposal"


def test_record_receipt_refuses_an_existing_receipt_row(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    relationships = MemoryRelationshipService()
    state.config["value"] = VaultConfig(enabled=False)
    _store(state, "target bytes", key="target")
    untouched = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
    )
    with state.session() as db:
        row = (
            db.query(MemoryMetadataModel)
            .filter_by(key="migrated", scope="global", source_kind="native")
            .one()
        )
        receipt = db.query(VaultMigrationReceiptModel).one()
        receipt_id = receipt.receipt_id
        before_receipt = (
            receipt.status,
            receipt.native_snapshot_sha256,
            receipt.published_content_sha256,
            receipt.superseded_edges,
        )
        native = migrate._read_native_snapshot(state.service, row)

        with pytest.raises(ValueError, match="receipt_already_recorded"):
            migrate._record_receipt(
                state.service,
                row=row,
                receipt_id=receipt_id,
                native=native,
                vault=state.vault,
                published_content_sha256="replacement",
                copied_edges=[untouched],
                relationship_service=relationships,
            )

    with state.session() as db:
        receipt = db.get(VaultMigrationReceiptModel, receipt_id)
        assert (
            receipt.status,
            receipt.native_snapshot_sha256,
            receipt.published_content_sha256,
            receipt.superseded_edges,
        ) == before_receipt
        assert db.query(VaultMigrationReceiptModel).count() == 1
        assert db.get(MemoryRelationshipModel, untouched.id).status == "active"


@pytest.mark.parametrize(
    "mismatch",
    (
        "changed_native_file",
        "different_vault",
        "changed_managed_folder",
        "missing_target",
        "orphan_receipt",
    ),
)
def test_receipt_guard_refuses_exact_mismatches(tmp_path, monkeypatch, mismatch: str) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)

    if mismatch == "changed_native_file":
        state.service.get_wiki_path("global", None, "migrated").write_text(
            "changed source",
            encoding="utf-8",
        )
    elif mismatch == "different_vault":
        changed = VaultSpec(
            id="different-vault",
            root=state.vault.root,
            managed_folder="CAO",
            mappings=[FolderMapping(folder="CAO", scope="global", writable=True)],
        )
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
    elif mismatch == "changed_managed_folder":
        (tmp_path / "vault" / "Other").mkdir()
        changed = VaultSpec(
            id=state.vault.id,
            root=state.vault.root,
            managed_folder="Other",
            mappings=[FolderMapping(folder="Other", scope="global", writable=True)],
        )
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
    elif mismatch == "missing_target":
        (tmp_path / "vault" / "CAO" / "migrated.md").unlink()
        reconcile_module.reconcile(state.vault, apply=True)
    elif mismatch == "orphan_receipt":
        with state.session() as db:
            db.query(MemoryMetadataModel).filter_by(
                key="migrated",
                scope="global",
                source_kind="native",
            ).delete()
            db.commit()

    with pytest.raises(ValueError, match="resolve the cross-tier collision"):
        _store(state, "must be refused")
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        assert (
            migration_receipt_inconsistency_reason(
                db,
                receipt,
                state.config["value"].vaults[0],
                memory_base_dir=state.service.base_dir,
            )
            == {
                "changed_native_file": "native_snapshot_mismatch",
                "different_vault": "target_vault_mismatch",
                "changed_managed_folder": "managed_relpath_mismatch",
                "missing_target": "target_note_missing",
                "orphan_receipt": "native_row_missing",
            }[mismatch]
        )


def test_unrelated_collision_without_receipt_still_refuses(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "native only")
    state.config["value"] = VaultConfig(enabled=True, vaults=[state.vault])

    with pytest.raises(ValueError, match="resolve the cross-tier collision"):
        _store(state, "must be refused")


def test_migration_rejects_symlinked_managed_folder(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    managed = tmp_path / "vault" / "CAO"
    managed.rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    managed.symlink_to(outside, target_is_directory=True)

    report = _migrate(state)

    assert report.failed == 1
    assert "symlink" in report.errors["migrated"]
    assert list(outside.iterdir()) == []
    with state.session() as db:
        assert db.query(VaultMigrationReceiptModel).count() == 0


def test_receipt_insert_failure_leaves_no_exemption(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")

    def fail_receipt(*_args, **_kwargs):
        raise RuntimeError("receipt transaction failed")

    monkeypatch.setattr(migrate, "_record_receipt", fail_receipt)
    report = _migrate(state)

    assert report.failed == 1
    assert report.errors == {"migrated": "receipt transaction failed"}
    assert (tmp_path / "vault" / "CAO" / "migrated.md").exists()
    with state.session() as db:
        assert db.query(VaultMigrationReceiptModel).count() == 0
    with pytest.raises(ValueError, match="resolve the cross-tier collision"):
        _store(state, "must be refused")


def test_receipt_transaction_failure_rolls_back_edge_supersession(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    copied = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        status="proposal",
    )
    with state.session() as db:
        db.execute(
            text(
                "CREATE TRIGGER fail_migrated_receipt "
                "BEFORE INSERT ON vault_migration_receipt "
                "WHEN NEW.cao_key = 'migrated' "
                "BEGIN SELECT RAISE(ABORT, 'receipt insert failed'); END"
            )
        )
        db.commit()
    state.config["value"] = VaultConfig(enabled=True, vaults=[state.vault])

    report = migrate.migrate_scope(
        state.service,
        state.vault,
        state.binding,
        scope="global",
        scope_id=None,
        apply=True,
        relationship_service=relationships,
    )

    assert report.failed == 1
    assert "receipt insert failed" in report.errors["migrated"]
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, copied.id).status == "proposal"
        assert db.query(VaultMigrationReceiptModel).filter_by(cao_key="migrated").count() == 0


def test_migration_receipt_supersedes_only_copied_edges_atomically(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _store(state, "target bytes", key="target")
    _store(state, "other bytes", key="other")
    relationships = MemoryRelationshipService()
    copied = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        status="proposal",
    )
    untouched = relationships.create(
        "global",
        None,
        "other",
        "target",
        "relates_to",
        "human",
    )
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="other",
            scope="global",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()

    state.config["value"] = VaultConfig(enabled=True, vaults=[state.vault])
    report = migrate.migrate_scope(
        state.service,
        state.vault,
        state.binding,
        scope="global",
        scope_id=None,
        apply=True,
        relationship_service=relationships,
    )

    assert report.migrated == 2
    _store(state, "post-migration append")
    target_path = tmp_path / "vault" / "CAO" / "migrated.md"
    parsed = parse_note(
        target_path.read_text(encoding="utf-8"),
        max_frontmatter_bytes=state.vault.max_frontmatter_bytes,
        secret_gate="reject",
    )
    assert parsed.cao["links"][0]["to"] == "target"
    assert "retained native bytes" in parsed.region.body
    assert "post-migration append" in parsed.region.body
    with state.session() as db:
        copied_row = db.get(MemoryRelationshipModel, copied.id)
        untouched_row = db.get(MemoryRelationshipModel, untouched.id)
        receipt = db.query(VaultMigrationReceiptModel).filter_by(cao_key="migrated").one()
        assert copied_row.status == "superseded"
        assert untouched_row.status == "active"
        recorded = json.loads(receipt.superseded_edges)
        assert recorded == [
            {
                "id": copied.id,
                "origin": "human",
                "prior_status": "proposal",
                "scope": "global",
                "scope_id": "",
                "source_key": "migrated",
                "target_key": "target",
                "type": "relates_to",
            }
        ]
        current_hash = (
            db.query(VaultNoteModel)
            .filter_by(cao_key="migrated", vault_id=state.vault.id)
            .one()
            .content_sha256
        )

    write_managed_note(
        vault=state.vault,
        binding=state.binding,
        key="migrated",
        body=parsed.region.body,
        cao={"type": "reference", "links": []},
        expected_content_sha256=current_hash,
        refresh=lambda _path: reconcile_module.reconcile(state.vault, apply=True),
    )
    assert (
        parse_note(
            target_path.read_text(encoding="utf-8"),
            max_frontmatter_bytes=state.vault.max_frontmatter_bytes,
            secret_gate="reject",
        ).cao["links"]
        == []
    )
    projected = relationships.list_relationships(
        "global",
        None,
        source_key="migrated",
        include_non_active=True,
    )
    assert [(edge.id, edge.status) for edge in projected] == [(copied.id, "superseded")]


def test_migration_receipt_restores_recorded_proposed_edge_via_repair(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    for key in ("migrated", "second", "target", "other-target", "unrelated"):
        _store(state, f"{key} bytes", key=key)
    relationships = MemoryRelationshipService()
    proposed = relationships.create(
        "global", None, "migrated", "target", "relates_to", "human", status="proposal"
    )
    second = relationships.create("global", None, "second", "other-target", "relates_to", "human")
    unrelated = relationships.create(
        "global", None, "unrelated", "target", "contradiction", "human"
    )
    with state.session() as db:
        for key in ("target", "other-target", "unrelated"):
            db.query(MemoryMetadataModel).filter_by(
                key=key,
                scope="global",
                source_kind="native",
            ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        receipt_id = (
            db.query(VaultMigrationReceiptModel).filter_by(cao_key="migrated").one().receipt_id
        )
    archive_dir = tmp_path / "vault" / "Archive"
    archive_dir.mkdir()
    canonical = tmp_path / "vault" / "CAO" / "migrated.md"
    archived = archive_dir / "migrated.md"
    canonical.rename(archived)
    archived_bytes = archived.read_bytes()
    reconcile_module.reconcile(state.vault, apply=True)
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    dry_run = repair.reconcile(receipt_id=receipt_id)
    applied = repair.reconcile(apply=True, receipt_id=receipt_id)

    assert dry_run.records[0].actions == (RepairAction.MIGRATION_ROLLBACK,)
    assert dry_run.records[0].status == "planned"
    assert applied.records[0].status == "repaired"
    assert archived.read_bytes() == archived_bytes
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, proposed.id).status == "proposal"
        assert db.get(MemoryRelationshipModel, second.id).status == "superseded"
        assert db.get(MemoryRelationshipModel, unrelated.id).status == "active"
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "rolled_back"


def test_authored_origin_round_trips_while_receipt_uses_stored_origin(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    edge = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        attributes={"authored_origin": "compiler"},
    )
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    report = _migrate(state)
    assert report.failed == 0, report.errors
    target = tmp_path / "vault" / "CAO" / "migrated.md"
    parsed = parse_note(
        target.read_text(encoding="utf-8"),
        max_frontmatter_bytes=state.vault.max_frontmatter_bytes,
        secret_gate="reject",
    )
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        receipt_id = receipt.receipt_id
        snapshot = json.loads(receipt.superseded_edges)

    assert parsed.cao["links"][0]["origin"] == "compiler"
    assert snapshot[0]["origin"] == "human"
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    assert repair.reconcile(apply=True, receipt_id=receipt_id).records[0].status == "repaired"
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, edge.id).status == "active"


def test_more_than_64_edges_receipts_exact_published_prefix_only(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    relationships = MemoryRelationshipService()
    for index in range(65):
        key = f"target-{index}"
        _store(state, f"{key} bytes", key=key)
        relationships.create(
            "global",
            None,
            "migrated",
            key,
            "relates_to",
            "human",
        )
    loaded = relationships.list_relationships(
        "global",
        None,
        source_key="migrated",
        include_non_active=True,
    )
    expected_ids = [edge.id for edge in loaded[:64]]
    overflow = loaded[64]
    relationships.patch(overflow.id, status="proposal")
    with state.session() as db:
        db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.key.like("target-%"),
            MemoryMetadataModel.source_kind == "native",
        ).update({"source_kind": "vault"}, synchronize_session=False)
        db.commit()

    report = _migrate(state)

    assert report.migrated == 1
    assert report.lossy_fields["migrated"]["cao.links"] == 1
    target = tmp_path / "vault" / "CAO" / "migrated.md"
    published = parse_note(
        target.read_text(encoding="utf-8"),
        max_frontmatter_bytes=state.vault.max_frontmatter_bytes,
        secret_gate="reject",
    )
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        receipt_id = receipt.receipt_id
        recorded_ids = [item["id"] for item in json.loads(receipt.superseded_edges)]
        assert recorded_ids == expected_ids
        assert db.get(MemoryRelationshipModel, overflow.id).status == "proposal"
    assert len(published.cao["links"]) == 64

    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    assert repair.reconcile(apply=True, receipt_id=receipt_id).records[0].status == "repaired"
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, overflow.id).status == "proposal"


def test_repair_preserves_archived_canonical_note_and_current_exclusion_policy(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    edge = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        status="proposal",
    )
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    canonical = tmp_path / "vault" / "CAO" / "migrated.md"
    canonical.write_text(
        canonical.read_text(encoding="utf-8") + "\nHuman canonical edit.\n",
        encoding="utf-8",
    )
    reconcile_module.reconcile(state.vault, apply=True)
    asyncio.run(state.service.forget("migrated", scope="global"))
    archive_dir = tmp_path / "vault" / "Archive"
    archive_dir.mkdir()
    archived = archive_dir / "migrated.md"
    canonical.rename(archived)
    archived_bytes = archived.read_bytes()
    reconcile_module.reconcile(state.vault, apply=True)
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
        exclusions_before = [
            (
                row.vault_id,
                row.scope,
                row.scope_id,
                row.cao_key,
                row.last_known_relpath,
                row.content_sha256,
            )
            for row in db.query(VaultExclusionModel).all()
        ]
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile(apply=True, receipt_id=receipt_id)

    assert report.records[0].status == "skipped"
    assert report.records[0].finding.kind == "native_source_missing"
    assert archived.read_bytes() == archived_bytes
    assert b"Human canonical edit." in archived_bytes
    with state.session() as db:
        exclusions_after = [
            (
                row.vault_id,
                row.scope,
                row.scope_id,
                row.cao_key,
                row.last_known_relpath,
                row.content_sha256,
            )
            for row in db.query(VaultExclusionModel).all()
        ]
        assert exclusions_after == exclusions_before
        assert db.get(MemoryRelationshipModel, edge.id) is None
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "active"
    _assert_native_is_dormant(state, tmp_path, monkeypatch)


@pytest.mark.parametrize(
    "policy_change",
    ("forget_tombstone", "configured_exclusion", "index_disabled"),
)
def test_successful_rollback_respects_current_visibility_policy(
    tmp_path, monkeypatch, policy_change: str
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        receipt_id = receipt.receipt_id
        if policy_change == "forget_tombstone":
            db.add(
                VaultExclusionModel(
                    vault_id=receipt.vault_id,
                    scope=receipt.scope,
                    scope_id=receipt.scope_id,
                    cao_key=receipt.cao_key,
                    last_known_relpath=receipt.managed_relpath,
                    content_sha256=receipt.published_content_sha256,
                )
            )
            db.commit()
    if policy_change == "configured_exclusion":
        changed = state.vault.model_copy(update={"exclude": ["CAO/migrated.md"]})
        state.vault = changed
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
    elif policy_change == "index_disabled":
        changed = state.vault.model_copy(
            update={
                "managed_folder": "Writes",
                "mappings": [
                    FolderMapping(
                        folder="CAO",
                        scope="global",
                        index=False,
                        writable=False,
                    ),
                    FolderMapping(
                        folder="Writes",
                        scope="agent",
                        scope_id="writer-agent",
                        index=True,
                        writable=True,
                    ),
                ],
            }
        )
        state.vault = changed
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile(apply=True, receipt_id=receipt_id)

    assert report.records[0].status == "repaired"
    with state.session() as db:
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "rolled_back"
        if policy_change == "forget_tombstone":
            assert (
                db.query(VaultExclusionModel)
                .filter_by(vault_id=state.vault.id, cao_key="migrated")
                .count()
                == 1
            )
    _assert_native_is_dormant(state, tmp_path, monkeypatch)


def test_successful_rollback_restores_native_on_all_surfaces_once_scope_is_unmapped(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    canonical = tmp_path / "vault" / "CAO" / "migrated.md"
    archive_dir = tmp_path / "vault" / "Archive"
    archive_dir.mkdir()
    archived = archive_dir / "migrated.md"
    canonical.rename(archived)
    archived_bytes = archived.read_bytes()
    reconcile_module.reconcile(state.vault, apply=True)
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
        assert db.query(VaultExclusionModel).count() == 0

    unmapped_vault = state.vault.model_copy(
        update={
            "mappings": [
                FolderMapping(
                    folder="CAO",
                    scope="project",
                    scope_id="different-project",
                    writable=True,
                )
            ]
        }
    )
    state.config["value"] = VaultConfig(enabled=True, vaults=[unmapped_vault])
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile(apply=True, receipt_id=receipt_id)

    metadata = asyncio.run(
        state.service.recall(
            query="retained",
            scope="global",
            search_mode="metadata",
            limit=100,
        )
    )
    bm25 = asyncio.run(
        state.service.recall(
            query="retained",
            scope="global",
            search_mode="bm25",
            limit=100,
        )
    )
    injection = state.service.get_memory_context(
        {"cwd": str(tmp_path), "terminal_id": "worker"},
        budget_chars=10_000,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.memory._get_memory_service",
        lambda: state.service,
    )
    listed = CliRunner().invoke(memory, ["list", "--scope", "global"])

    assert report.records[0].status == "repaired"
    assert [(item.key, item.source_kind) for item in metadata] == [("migrated", "native")]
    assert [(item.key, item.source_kind) for item in bm25] == [("migrated", "native")]
    assert "retained native bytes" in injection
    assert listed.exit_code == 0
    assert "migrated" in listed.output
    assert archived.read_bytes() == archived_bytes
    with state.session() as db:
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "rolled_back"
        assert db.query(VaultExclusionModel).count() == 0


@pytest.mark.parametrize(
    "invalid_snapshot",
    ("unparseable", "too_many", "duplicate", "missing_field", "type", "origin", "status"),
)
def test_receipt_repair_refuses_invalid_edge_snapshot(
    tmp_path, monkeypatch, invalid_snapshot: str
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    _migrate(state)
    valid = {
        "id": "edge",
        "prior_status": "active",
        "scope": "global",
        "scope_id": "",
        "source_key": "migrated",
        "target_key": "target",
        "type": "relates_to",
        "origin": "human",
    }
    if invalid_snapshot == "unparseable":
        raw = "{"
    elif invalid_snapshot == "too_many":
        raw = json.dumps([{**valid, "id": f"edge-{index}"} for index in range(65)])
    elif invalid_snapshot == "duplicate":
        raw = json.dumps([valid, valid])
    elif invalid_snapshot == "missing_field":
        raw = json.dumps([{key: value for key, value in valid.items() if key != "origin"}])
    else:
        bad = dict(valid)
        bad[invalid_snapshot if invalid_snapshot != "status" else "prior_status"] = "invalid"
        raw = json.dumps([bad])
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).one()
        receipt_id = receipt.receipt_id
        receipt.superseded_edges = raw
        db.commit()
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile(apply=True, receipt_id=receipt_id)

    assert report.has_unresolved
    assert report.records[0].finding.kind == "edge_snapshot_invalid"
    with state.session() as db:
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "active"


@pytest.mark.parametrize(
    ("drift", "expected_reason"),
    (
        ("status", "edge_status_drift"),
        ("endpoint", "edge_identity_drift"),
        ("type", "edge_identity_drift"),
        ("origin", "edge_identity_drift"),
        ("scope", "edge_identity_drift"),
        ("missing", "edge_missing"),
    ),
)
def test_receipt_repair_is_scoped_and_fail_closed_on_edge_drift(
    tmp_path,
    monkeypatch,
    drift: str,
    expected_reason: str,
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    for key in ("migrated", "target-a", "target-b", "alternate"):
        _store(state, f"{key} bytes", key=key)
    relationships = MemoryRelationshipService()
    first = relationships.create("global", None, "migrated", "target-a", "relates_to", "human")
    second = relationships.create(
        "global", None, "migrated", "target-b", "contradiction", "compiler"
    )
    with state.session() as db:
        for key in ("target-a", "target-b", "alternate"):
            db.query(MemoryMetadataModel).filter_by(
                key=key,
                scope="global",
                source_kind="native",
            ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        receipt = db.query(VaultMigrationReceiptModel).filter_by(cao_key="migrated").one()
        receipt_id = receipt.receipt_id
        row = db.get(MemoryRelationshipModel, first.id)
        if drift == "status":
            row.status = "active"
        elif drift == "endpoint":
            row.target_key = "alternate"
        elif drift == "type":
            row.type = "contradiction"
        elif drift == "origin":
            row.origin = "compiler"
        elif drift == "scope":
            row.scope = "project"
            row.scope_id = "project-id"
        elif drift == "missing":
            db.delete(row)
        db.commit()
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile(apply=True, receipt_id=receipt_id)

    assert report.has_unresolved
    assert report.records[0].status == "skipped"
    assert report.records[0].finding.kind == expected_reason
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, second.id).status == "superseded"
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "active"


def test_receipt_rollback_transaction_failure_restores_no_edge(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    edge = relationships.create(
        "global",
        None,
        "migrated",
        "target",
        "relates_to",
        "human",
        status="proposal",
    )
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
        db.execute(
            text(
                "CREATE TRIGGER fail_receipt_rollback "
                "BEFORE UPDATE OF status ON vault_migration_receipt "
                "WHEN NEW.status = 'rolled_back' "
                "BEGIN SELECT RAISE(ABORT, 'receipt rollback failed'); END"
            )
        )
        db.commit()
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    with pytest.raises(IntegrityError, match="receipt rollback failed"):
        repair.reconcile(apply=True, receipt_id=receipt_id)

    with state.session() as db:
        assert db.get(MemoryRelationshipModel, edge.id).status == "superseded"
        assert db.get(VaultMigrationReceiptModel, receipt_id).status == "active"


def test_repair_without_receipt_reports_drift_without_restoring(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    edge = relationships.create("global", None, "migrated", "target", "relates_to", "human")
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        db.get(MemoryRelationshipModel, edge.id).status = "proposal"
        db.commit()
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )

    report = repair.reconcile()

    receipt_records = [
        record
        for record in report.records
        if record.finding is not None and record.finding.kind == "edge_status_drift"
    ]
    assert len(receipt_records) == 1
    assert receipt_records[0].status == "unchanged"
    assert report.has_unresolved is False
    with state.session() as db:
        assert db.get(MemoryRelationshipModel, edge.id).status == "proposal"
        assert db.query(VaultMigrationReceiptModel).one().status == "active"


def test_corpus_repair_cli_exits_zero_when_only_receipt_drift_remains(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    native = state.service.get_wiki_path("global", None, "migrated")
    native.write_text(
        native.read_text(encoding="utf-8") + "\nvalid retained-source edit\n",
        encoding="utf-8",
    )
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        lambda: repair,
    )

    result = CliRunner().invoke(memory, ["repair", "--apply"])

    assert result.exit_code == 0
    assert "repaired=1" in result.output
    assert "native_snapshot_mismatch" in result.output
    with state.session() as db:
        assert db.query(VaultMigrationReceiptModel).one().status == "active"


def test_corpus_repair_cli_still_exits_one_for_actual_unresolved_topic(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "malformed topic")
    native = state.service.get_wiki_path("global", None, "migrated")
    native.write_text("# malformed\n\nmissing canonical metadata\n", encoding="utf-8")
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        lambda: repair,
    )

    result = CliRunner().invoke(memory, ["repair", "--apply"])

    assert result.exit_code == 1
    assert "skipped=1" in result.output
    assert "topic heading does not match its canonical path identity" in result.output


def test_receipt_scoped_repair_cli_still_exits_one_on_source_drift(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    native = state.service.get_wiki_path("global", None, "migrated")
    native.write_text(
        native.read_text(encoding="utf-8") + "\nvalid retained-source edit\n",
        encoding="utf-8",
    )
    with state.session() as db:
        receipt_id = db.query(VaultMigrationReceiptModel).one().receipt_id
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        lambda: repair,
    )

    result = CliRunner().invoke(
        memory,
        ["repair", "--apply", "--receipt", receipt_id],
    )

    assert result.exit_code == 1
    assert "skipped=1" in result.output
    assert "native_snapshot_mismatch" in result.output


def test_unknown_receipt_refuses_without_falling_back_to_topic_repair(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    monkeypatch.setattr(
        repair,
        "plan",
        lambda: pytest.fail("unknown receipt must not fall back to topic discovery"),
    )

    with pytest.raises(ValueError, match="migration receipt not found"):
        repair.reconcile(receipt_id="missing-receipt")


def test_unknown_receipt_cli_points_to_status_and_valid_receipt_id(tmp_path, monkeypatch) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    repair = MemoryReconciliationService(
        base_dir=state.service.base_dir,
        db_engine=state.service._db_engine,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        lambda: repair,
    )

    result = CliRunner().invoke(
        memory,
        ["repair", "--receipt", "missing-receipt"],
    )

    assert result.exit_code == 1
    assert "cao memory vault status" in result.output
    assert "valid receipt id" in result.output
    assert "run `cao memory repair --apply`" not in result.output


def test_edge_snapshot_drift_does_not_expand_five_condition_write_guard(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "source bytes")
    _store(state, "target bytes", key="target")
    relationships = MemoryRelationshipService()
    edge = relationships.create("global", None, "migrated", "target", "relates_to", "human")
    with state.session() as db:
        db.query(MemoryMetadataModel).filter_by(
            key="target",
            source_kind="native",
        ).one().source_kind = "vault"
        db.commit()
    _migrate(state)
    with state.session() as db:
        db.get(MemoryRelationshipModel, edge.id).status = "proposal"
        db.commit()

    stored = _store(state, "write remains permitted")

    assert stored.source_kind == "vault"
    assert "write remains permitted" in (tmp_path / "vault" / "CAO" / "migrated.md").read_text(
        encoding="utf-8"
    )


@pytest.mark.parametrize(
    "policy_change",
    ("forget", "exclude", "index_disabled", "target_missing", "source_drift"),
)
def test_receipt_dormancy_never_restores_native_after_policy_or_integrity_change(
    tmp_path, monkeypatch, policy_change: str
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)

    if policy_change == "forget":
        asyncio.run(state.service.forget("migrated", scope="global"))
    elif policy_change == "exclude":
        changed = state.vault.model_copy(update={"exclude": ["CAO/migrated.md"]})
        state.vault = changed
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
        reconcile_module.reconcile(changed, apply=True)
    elif policy_change == "index_disabled":
        (tmp_path / "vault" / "Writes").mkdir()
        changed = state.vault.model_copy(
            update={
                "managed_folder": "Writes",
                "mappings": [
                    FolderMapping(folder="CAO", scope="global", index=False),
                    FolderMapping(
                        folder="Writes",
                        scope="agent",
                        scope_id="writer-agent",
                        index=True,
                        writable=True,
                    ),
                ],
            }
        )
        state.vault = changed
        state.config["value"] = VaultConfig(enabled=True, vaults=[changed])
        reconcile_module.reconcile(changed, apply=True)
    elif policy_change == "target_missing":
        (tmp_path / "vault" / "CAO" / "migrated.md").unlink()
        reconcile_module.reconcile(state.vault, apply=True)
    elif policy_change == "source_drift":
        state.service.get_wiki_path("global", None, "migrated").write_text(
            "changed dormant native bytes",
            encoding="utf-8",
        )

    _assert_native_is_dormant(state, tmp_path, monkeypatch)


def test_receipt_dormancy_covers_metadata_bm25_list_injection_and_related_expansion(
    tmp_path, monkeypatch
) -> None:
    state = _lifecycle(tmp_path, monkeypatch)
    _store(state, "retained native bytes")
    _migrate(state)
    state.config["value"] = VaultConfig(enabled=False)

    metadata = asyncio.run(
        state.service.recall(
            query="retained",
            scope="global",
            search_mode="metadata",
            limit=100,
        )
    )
    bm25 = asyncio.run(
        state.service.recall(
            query="retained",
            scope="global",
            search_mode="bm25",
            limit=100,
        )
    )
    injection = state.service.get_memory_context(
        {"cwd": str(tmp_path), "terminal_id": "worker"},
        budget_chars=10_000,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.cli.commands.memory._get_memory_service",
        lambda: state.service,
    )
    listed = CliRunner().invoke(memory, ["list", "--scope", "global"])

    _store(state, "visible source", key="related-source")
    with state.session() as db:
        source = (
            db.query(MemoryMetadataModel)
            .filter_by(
                key="related-source",
                scope="global",
                source_kind="native",
            )
            .one()
        )
        source.related_keys = "migrated"
        db.commit()
    related = asyncio.run(
        state.service.recall(
            query="visible",
            scope="global",
            search_mode="metadata",
            include_related=True,
            limit=100,
        )
    )

    assert metadata == []
    assert bm25 == []
    assert "retained native bytes" not in injection
    assert listed.exit_code == 0
    assert "migrated" not in listed.output
    assert [item.key for item in related] == ["related-source"]
