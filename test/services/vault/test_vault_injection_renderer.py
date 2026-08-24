"""Renderer integration coverage for injectable vault-backed memories."""

from __future__ import annotations

from datetime import datetime, timezone
from test.fixtures.vault_factory import build_vault_fixture

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session as OrmSession
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.models.memory import Memory
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault.config import VaultConfig
from cli_agent_orchestrator.services.vault.reconcile import reconcile


def _injectable_renderer(tmp_path, monkeypatch):
    """Build a vault-only project scope backed by a temporary index database."""
    from cli_agent_orchestrator.services import memory_service, settings_service
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)
    monkeypatch.setattr(reader, "SessionLocal", session_factory)
    monkeypatch.setattr(reconcile_module, "SessionLocal", session_factory)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_args: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)

    fixture = build_vault_fixture(tmp_path)
    project_dir = fixture.root / "Projects" / "CAO Design"
    for path in project_dir.glob("*.md"):
        if path.name != "Design.md":
            path.unlink()
    mappings = [
        (
            mapping.model_copy(update={"inject": True})
            if mapping.folder == "Projects/CAO Design"
            else mapping
        )
        for mapping in fixture.vault.mappings
    ]
    vault = fixture.vault.model_copy(update={"mappings": mappings, "max_note_bytes": 8192})
    config = VaultConfig(enabled=True, vaults=[vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    reconcile(
        vault,
        apply=True,
        run_id="renderer",
        run_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    service = MemoryService(base_dir=tmp_path / "native", db_engine=engine)
    context = {
        "terminal_id": "worker",
        "session_name": "renderer-session",
        "agent_profile": "developer",
        "provider": "claude_code",
        "cwd": str(tmp_path / "project"),
    }
    service._get_terminal_context = lambda _terminal_id: context  # type: ignore[method-assign]
    service.resolve_scope_id = lambda scope, _context: (  # type: ignore[method-assign]
        "fixture-project" if scope == "project" else None
    )
    return service, session_factory, fixture.root, config


def _counter_values(session_factory) -> dict[str, int]:
    with session_factory() as db:
        return {
            row.counter_name: row.value
            for row in db.query(VaultRecallCounterModel)
            .filter(VaultRecallCounterModel.vault_id == "fixture")
            .all()
        }


def test_renderer_injects_real_injectable_vault_candidate(tmp_path, monkeypatch) -> None:
    """The deterministic injection builder serves an injectable vault-only scope."""
    service, _session_factory, _vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)

    block = service.get_memory_context_for_terminal("worker")

    assert "- [project] design: Design" in block


def test_fitting_vault_injection_performs_no_counter_commit(tmp_path, monkeypatch) -> None:
    """A vault candidate within its scope cap leaves no counter row or commit."""
    service, session_factory, _vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)
    commits: list[object] = []

    def record_commit(session):
        commits.append(session)

    event.listen(OrmSession, "after_commit", record_commit)
    try:
        before = _counter_values(session_factory)
        block = service.get_memory_context_for_terminal("worker")
        after = _counter_values(session_factory)
    finally:
        event.remove(OrmSession, "after_commit", record_commit)

    assert "- [project] design: Design" in block
    assert commits == []
    assert before == after == {}


def test_renderer_clips_real_vault_candidate_at_shipped_scope_budget(tmp_path, monkeypatch) -> None:
    """The 4096-char vault body is clipped by the 1000-char default scope cap."""
    service, session_factory, vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)
    note = vault_root / "Projects" / "CAO Design" / "Design.md"
    note.write_text("---\ncao:\n  key: design\n---\n" + "x" * 4096, encoding="utf-8")

    # Reconcile the changed body so this is a fresh real candidate, not a
    # stale-read artefact. The helper's configured vault is still in effect.
    from cli_agent_orchestrator.services.settings_service import get_vault_config

    reconcile(
        get_vault_config().vaults[0],
        apply=True,
        run_id="renderer-clip",
        run_started_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    block = service.get_memory_context_for_terminal("worker")

    assert "x" * 1000 not in block
    assert _counter_values(session_factory) == {
        "injection_budget_exceeded.memories_dropped": 1,
        "injection_budget_exceeded.scopes_clipped": 1,
    }


def test_renderer_clip_counter_excludes_related_entries_from_dropped_magnitude(
    tmp_path, monkeypatch
) -> None:
    """A primary break records only entries a clean render would emit."""
    service, session_factory, vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)
    (vault_root / "Projects" / "CAO Design" / "Other.md").write_text(
        "---\ncao:\n  key: other\n---\n" + "x" * 4096,
        encoding="utf-8",
    )
    from cli_agent_orchestrator.services.settings_service import get_vault_config

    reconcile(
        get_vault_config().vaults[0],
        apply=True,
        run_id="renderer-p5",
        run_started_at=datetime(2025, 1, 2, tzinfo=timezone.utc),
    )
    with session_factory() as db:
        db.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "design").update(
            {"updated_at": datetime(2025, 1, 3, tzinfo=timezone.utc)}
        )
        db.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "other").update(
            {"updated_at": datetime(2025, 1, 2, tzinfo=timezone.utc)}
        )
        db.commit()

    lookup_kinds: list[str] = []

    def related_lookup(keys, scope, scope_id, *, source_kind="native"):
        lookup_kinds.append(source_kind)
        return {"design": "related"} if source_kind == "vault" else {}

    related = Memory(
        id="related",
        key="related",
        memory_type="reference",
        scope="project",
        scope_id="fixture-project",
        file_path="/internal",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        content="related body",
        source_kind="vault",
    )
    recorded: list[list[Memory]] = []
    monkeypatch.setattr(service, "_related_keys_lookup", related_lookup)
    monkeypatch.setattr(
        service,
        "_load_related_vault_memory",
        lambda *_args, **_kwargs: related.model_copy(deep=True),
    )
    monkeypatch.setattr(
        service,
        "_record_vault_injection_clip",
        lambda _scope, _scope_id, dropped: recorded.append(dropped),
    )

    block = service.get_memory_context_for_terminal("worker")

    assert "- [project] design: Design" in block
    assert lookup_kinds == ["native", "vault"] * 3
    assert len(recorded) == 1
    assert [memory.key for memory in recorded[0]] == ["other"]
    assert all(not memory.is_related for memory in recorded[0])


def test_renderer_records_a_related_vault_entry_skipped_by_the_scope_budget(
    tmp_path, monkeypatch
) -> None:
    """A too-large related extra reaches its distinct accounting branch."""
    service, _session_factory, _vault_root, _config = _injectable_renderer(tmp_path, monkeypatch)
    related = Memory(
        id="related",
        key="related",
        memory_type="reference",
        scope="project",
        scope_id="fixture-project",
        file_path="/internal",
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        updated_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        content="x" * 4096,
        source_kind="vault",
    )
    related_skips: list[Memory] = []
    monkeypatch.setattr(
        service,
        "_related_keys_lookup",
        lambda _keys, _scope, _scope_id, *, source_kind="native": (
            {"design": "related"} if source_kind == "vault" else {}
        ),
    )
    monkeypatch.setattr(
        service,
        "_load_related_vault_memory",
        lambda *_args, **_kwargs: related.model_copy(deep=True),
    )
    monkeypatch.setattr(
        service,
        "_record_vault_related_injection_skip",
        lambda memory: related_skips.append(memory),
    )

    block = service.get_memory_context_for_terminal("worker")

    assert "- [project] design: Design" in block
    assert "x" * 1000 not in block
    assert [memory.key for memory in related_skips] == ["related"]
