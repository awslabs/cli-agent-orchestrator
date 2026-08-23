"""Status presentation retains live configuration warnings."""

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultConfig, VaultSpec
from cli_agent_orchestrator.services.vault.status import get_vault_status


def test_status_keeps_combined_warn_inject_warning_from_live_config(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import status

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    monkeypatch.setattr(status, "SessionLocal", sessionmaker(bind=engine))
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

    result = get_vault_status(config)[0]

    assert result.warnings == ("mapping 'Mapped' has secret_gate='warn' with inject=true",)


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
