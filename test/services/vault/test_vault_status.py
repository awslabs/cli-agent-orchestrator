"""Status presentation retains live configuration warnings."""

import hashlib

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    VaultMigrationReceiptModel,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig, VaultSpec
from cli_agent_orchestrator.services.vault.status import get_vault_status


def test_status_keeps_combined_warn_inject_warning_from_live_config(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(status, "SessionLocal", Session)
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    (root / "Mapped").mkdir()
    config = VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="status-test",
                root=str(root),
                managed_folder="CAO",
                mappings=[
                    FolderMapping(
                        folder="Mapped",
                        scope="agent",
                        scope_id="agent",
                        inject=True,
                        secret_gate="warn",
                    ),
                    FolderMapping(folder="CAO", scope="global", writable=True),
                ],
            )
        ],
    )
    with Session() as db:
        db.add_all(
            [
                VaultRecallCounterModel(
                    vault_id="status-test",
                    counter_name="injection_redaction.memories_redacted",
                    value=2,
                ),
                VaultRecallCounterModel(
                    vault_id="status-test",
                    counter_name="injection_redaction.pattern_matches",
                    value=3,
                ),
            ]
        )
        db.commit()

    result = get_vault_status(config)[0]

    assert result.warnings == (
        "mapping 'Mapped' has secret_gate='warn' with inject=true",
        "agent-scoped mapping 'Mapped' is recall-only and is not injected in this release",
    )
    assert dict(result.recall_counters) == {
        "injection_redaction.memories_redacted": 2,
        "injection_redaction.pattern_matches": 3,
    }
    assert "hunter2sixteen" not in repr(result)


def test_status_surfaces_binding_warning_details(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status
    from cli_agent_orchestrator.services.vault.binding import BindingWarning

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(status, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(
        status,
        "collect_binding_warnings",
        lambda _config: (
            BindingWarning(
                kind="orphaned_mapping",
                mapping="Mapped",
                detail="mapping 'Mapped' is not bound to a known project",
            ),
        ),
    )
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    (root / "Mapped").mkdir()
    config = VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="status-binding",
                root=str(root),
                managed_folder="CAO",
                mappings=[
                    FolderMapping(folder="Mapped", scope="agent", scope_id="agent"),
                    FolderMapping(folder="CAO", scope="global", writable=True),
                ],
            )
        ],
    )

    result = get_vault_status(config)[0]

    assert result.warnings == ("mapping 'Mapped' is not bound to a known project",)


def test_status_names_unmapped_writes_as_process_local(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(status, "SessionLocal", sessionmaker(bind=engine))
    monkeypatch.setattr(status, "unmapped_project_write_count", lambda: 3)
    monkeypatch.setattr(status, "unmapped_project_identity_count", lambda: 2)
    monkeypatch.setattr(status, "non_writable_write_refusal_count", lambda _vault_id: 4)
    monkeypatch.setattr(status, "secret_gate_write_refusal_count", lambda _vault_id: 5)
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    config = VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="status-local",
                root=str(root),
                managed_folder="CAO",
                mappings=[FolderMapping(folder="CAO", scope="global", writable=True)],
            )
        ],
    )

    result = get_vault_status(config)[0]

    assert result.process_local_unmapped_project_writes == 3
    assert result.process_local_unmapped_project_identities == 2
    assert result.process_local_non_writable_write_refusals == 4
    assert result.process_local_secret_gate_write_refusals == 5


def test_status_reports_receipt_health_and_boundary_refusals(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(status, "SessionLocal", Session)
    monkeypatch.setattr(status, "boundary_write_refusal_count", lambda _vault_id: 6)
    native_base = tmp_path / "native"
    native_path = native_base / "global" / "wiki" / "global" / "migrated.md"
    native_path.parent.mkdir(parents=True)
    native_bytes = b"retained native source"
    native_path.write_bytes(native_bytes)
    monkeypatch.setattr(status, "MEMORY_BASE_DIR", native_base)
    root = tmp_path / "vault"
    root.mkdir()
    (root / "CAO").mkdir()
    config = VaultConfig(
        enabled=True,
        vaults=[
            VaultSpec(
                id="receipt-vault",
                root=str(root),
                managed_folder="CAO",
                mappings=[FolderMapping(folder="CAO", scope="global", writable=True)],
            )
        ],
    )
    with Session() as db:
        db.add_all(
            [
                MemoryMetadataModel(
                    id="native",
                    key="migrated",
                    memory_type="reference",
                    scope="global",
                    scope_id=None,
                    source_kind="native",
                    file_path=str(native_path),
                    tags="",
                ),
                VaultNoteModel(
                    note_uid="note",
                    vault_id="receipt-vault",
                    scope="global",
                    scope_id="",
                    cao_key="migrated",
                    vault_relpath="CAO/migrated.md",
                    managed=True,
                    content_sha256="b" * 64,
                    status="indexed",
                ),
                VaultMigrationReceiptModel(
                    receipt_id="receipt",
                    scope="global",
                    scope_id="",
                    cao_key="migrated",
                    native_relpath="global/wiki/global/migrated.md",
                    native_snapshot_sha256=hashlib.sha256(native_bytes).hexdigest(),
                    vault_id="receipt-vault",
                    managed_relpath="CAO/migrated.md",
                    vault_note_uid="note",
                    published_content_sha256="a" * 64,
                    superseded_edges="[]",
                    status="active",
                ),
            ]
        )
        db.commit()

    healthy = get_vault_status(config)[0]
    native_path.write_bytes(b"changed native source")
    drifted = get_vault_status(config)[0]
    native_path.write_bytes(native_bytes)
    with Session() as db:
        db.query(VaultMigrationReceiptModel).one().superseded_edges = "{"
        db.commit()
    invalid_edges = get_vault_status(config)[0]

    assert dict(healthy.migration_receipts) == {"active": 1, "inconsistent": 0}
    assert healthy.process_local_boundary_write_refusals == 6
    assert dict(drifted.migration_receipts) == {"active": 1, "inconsistent": 1}
    warning = next(item for item in drifted.warnings if "receipt=receipt" in item)
    assert "native_snapshot_mismatch" in warning
    assert "retained native source" not in repr(drifted)
    assert "changed native source" not in repr(drifted)
    assert "a" * 64 not in repr(drifted)
    assert dict(invalid_edges.migration_receipts) == {"active": 1, "inconsistent": 1}
    assert any("edge_snapshot_invalid" in item for item in invalid_edges.warnings)
