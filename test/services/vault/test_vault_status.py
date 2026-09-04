"""Status presentation retains live configuration warnings."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, VaultRecallCounterModel
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
