"""U8 contract coverage for native deletion and vault deindexing."""

from __future__ import annotations

import asyncio
from test.fixtures.vault_factory import build_vault_fixture

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.services import memory_service, settings_service
from cli_agent_orchestrator.services.memory_service import ForgetResult, MemoryService
from cli_agent_orchestrator.services.vault import reader
from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
from cli_agent_orchestrator.services.vault.config import VaultConfig


def _vault_service(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_args: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)
    fixture = build_vault_fixture(tmp_path)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    return (
        MemoryService(base_dir=tmp_path / "native", db_engine=engine),
        fixture,
        Session,
    )


def test_forget_result_preserves_bool_contract() -> None:
    assert bool(ForgetResult("deleted", "native", "global/topic.md")) is True
    assert bool(ForgetResult("deindexed", "vault", "CAO/topic.md")) is True
    assert bool(ForgetResult("absent", "native", "global/missing.md")) is False


def test_forget_deindexes_vault_note_without_unlinking(tmp_path, monkeypatch) -> None:
    service, fixture, Session = _vault_service(tmp_path, monkeypatch)

    stored = asyncio.run(
        service.store(
            content="managed vault body",
            scope="global",
            memory_type="reference",
            key="managed-topic",
        )
    )
    note_path = fixture.root / "CAO" / "managed-topic.md"
    assert stored.source_kind == "vault"
    assert note_path.exists()

    result = asyncio.run(service.forget("managed-topic", scope="global"))

    assert result.action == "deindexed"
    assert result.source_kind == "vault"
    assert result.path == "CAO/managed-topic.md"
    assert note_path.exists()
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="managed-topic").one()
        assert note.status == "excluded"

    reconcile_module.reconcile(fixture.vault, apply=True, run_id="after-forget")

    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="managed-topic").one()
        vault_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="managed-topic").all()
        )
        assert note.status == "excluded"
        assert vault_metadata == []


def test_memory_forget_reports_legacy_bool_and_authoritative_action(
    monkeypatch,
) -> None:
    """MCP callers keep ``deleted`` while receiving the non-destructive outcome."""
    from cli_agent_orchestrator.mcp_server import server

    class _Service:
        async def forget(self, **_kwargs):
            return ForgetResult("deindexed", "vault", "CAO/managed-topic.md")

    monkeypatch.setattr(memory_service, "MemoryService", _Service)
    monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: None)

    result = asyncio.run(server.memory_forget(key="managed-topic", scope="global"))

    assert result == {
        "success": True,
        "deleted": True,
        "action": "deindexed",
        "path": "CAO/managed-topic.md",
        "key": "managed-topic",
        "scope": "global",
    }


def test_unmapped_project_store_records_native_fallback_after_resolution(
    tmp_path, monkeypatch
) -> None:
    """Configured project mappings make a native project fallback observable."""
    from cli_agent_orchestrator.services.vault import binding

    fixture = build_vault_fixture(tmp_path)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    calls: list[str | None] = []
    monkeypatch.setattr(
        binding,
        "record_unmapped_project_write",
        lambda scope_id, **_kwargs: calls.append(scope_id),
    )
    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    service = MemoryService(base_dir=tmp_path / "native", db_engine=engine)

    asyncio.run(
        service.store(
            content="native fallback",
            scope="project",
            memory_type="project",
            key="unmapped-project",
            terminal_context={"cwd": str(tmp_path / "other-project")},
        )
    )

    assert calls and calls[0] is not None


def test_store_refuses_non_writable_vault_binding_without_native_fallback(
    tmp_path, monkeypatch
) -> None:
    service, fixture, _Session = _vault_service(tmp_path, monkeypatch)
    monkeypatch.setattr(
        service,
        "resolve_scope_id",
        lambda scope, _terminal_context: ("fixture-project" if scope == "project" else None),
    )
    native_path = service.get_wiki_path("project", "fixture-project", "read-only-topic")

    from cli_agent_orchestrator.services.vault import binding

    binding._reset_unmapped_project_write_count()
    with pytest.raises(ValueError, match="not writable"):
        asyncio.run(
            service.store(
                content="must not fall back",
                scope="project",
                memory_type="project",
                key="read-only-topic",
            )
        )

    assert not native_path.exists()
    assert not (fixture.root / "CAO" / "read-only-topic.md").exists()
    assert binding.non_writable_write_refusal_count(fixture.vault.id) == 1


def test_store_refuses_vault_write_when_native_peer_exists(tmp_path, monkeypatch) -> None:
    service, fixture, _Session = _vault_service(tmp_path, monkeypatch)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    native_config = VaultConfig(enabled=False, vaults=[])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: native_config)
    asyncio.run(
        service.store(
            content="native first",
            scope="global",
            memory_type="reference",
            key="shared-topic",
        )
    )
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)

    with pytest.raises(ValueError, match="already exists in the native store"):
        asyncio.run(
            service.store(
                content="vault second",
                scope="global",
                memory_type="reference",
                key="shared-topic",
            )
        )

    assert not (fixture.root / "CAO" / "shared-topic.md").exists()


def test_store_refuses_native_write_when_vault_peer_exists(tmp_path, monkeypatch) -> None:
    service, fixture, _Session = _vault_service(tmp_path, monkeypatch)
    native_config = VaultConfig(enabled=False, vaults=[])
    asyncio.run(
        service.store(
            content="vault first",
            scope="global",
            memory_type="reference",
            key="shared-topic",
        )
    )
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: native_config)

    with pytest.raises(ValueError, match="already exists in the vault store"):
        asyncio.run(
            service.store(
                content="native second",
                scope="global",
                memory_type="reference",
                key="shared-topic",
            )
        )

    assert not service.get_wiki_path("global", None, "shared-topic").exists()


def test_vault_forget_removes_preexisting_native_peer_and_reports_both(
    tmp_path, monkeypatch
) -> None:
    service, fixture, _Session = _vault_service(tmp_path, monkeypatch)
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    native_config = VaultConfig(enabled=False, vaults=[])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: native_config)
    asyncio.run(
        service.store(
            content="native peer",
            scope="global",
            memory_type="reference",
            key="duplicated-topic",
        )
    )

    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    original_collision_check = service._assert_no_cross_tier_collision
    monkeypatch.setattr(service, "_assert_no_cross_tier_collision", lambda *_args, **_kwargs: None)
    asyncio.run(
        service.store(
            content="vault peer",
            scope="global",
            memory_type="reference",
            key="duplicated-topic",
        )
    )
    monkeypatch.setattr(service, "_assert_no_cross_tier_collision", original_collision_check)

    result = asyncio.run(service.forget("duplicated-topic", scope="global"))

    assert result.action == "deleted_and_deindexed"
    assert result.source_kind == "both"
    assert not service.get_wiki_path("global", None, "duplicated-topic").exists()
    assert asyncio.run(service.recall(scope="global", limit=10)) == []
