"""Tests for dry-run-first native-to-vault migration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from test.fixtures.vault_factory import build_vault_fixture

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel, VaultNoteModel
from cli_agent_orchestrator.services.memory_relationship_service import RelationshipDTO
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.vault import migrate
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.parser import parse_note


class _Relationships:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.calls = []

    def list_relationships(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.rows


@pytest.fixture
def svc(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    Base.metadata.create_all(engine)
    service = MemoryService(base_dir=tmp_path / "memory", db_engine=engine)
    monkeypatch.setattr(migrate, "MEMORY_BASE_DIR", service.base_dir)
    return service


def _binding(fixture) -> VaultBinding:
    mapping = next(mapping for mapping in fixture.vault.mappings if mapping.writable)
    return VaultBinding(
        scope=mapping.scope,
        scope_id=mapping.scope_id,
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )


def _store(svc, key, content="native content", *, tags=""):
    return asyncio.run(
        svc.store(
            content=content,
            scope="global",
            memory_type="reference",
            key=key,
            tags=tags,
        )
    )


def _migrate(svc, fixture, **kwargs):
    defaults = {
        "scope": "global",
        "scope_id": None,
        "relationship_service": _Relationships(),
        "refresh": lambda _path: None,
    }
    defaults.update(kwargs)
    return migrate.migrate_scope(svc, fixture.vault, _binding(fixture), **defaults)


def test_dry_run_performs_no_write_row_change_or_commit(tmp_path, svc, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "dry-run")
    commits = []
    original_commit = Session.commit

    def record_commit(self):
        commits.append(self)
        return original_commit(self)

    monkeypatch.setattr(Session, "commit", record_commit)
    writes = []
    monkeypatch.setattr(migrate, "write_managed_note", lambda **kwargs: writes.append(kwargs))
    with svc._get_db_session() as db:
        before = db.query(MemoryMetadataModel).count()

    report = _migrate(svc, fixture)

    with svc._get_db_session() as db:
        after = db.query(MemoryMetadataModel).count()
    assert report.dry_run is True
    assert report.planned == 1
    assert writes == []
    assert before == after == 1
    assert commits == []
    assert not (fixture.root / "CAO" / "dry-run.md").exists()


def test_delete_source_requires_apply_and_second_confirmation(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)

    with pytest.raises(ValueError, match=r"--delete-source requires --apply"):
        _migrate(svc, fixture, delete_source=True)
    with pytest.raises(ValueError, match=r"--delete-source requires --confirm-delete-source"):
        _migrate(svc, fixture, apply=True, delete_source=True)


def test_confirmed_delete_source_removes_native_only_after_a_successful_write(
    tmp_path, svc
) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "delete-me")

    report = _migrate(
        svc,
        fixture,
        apply=True,
        delete_source=True,
        confirm_delete_source=True,
    )

    assert report.migrated == 1
    assert report.deleted_source == 1
    assert not svc.get_wiki_path("global", None, "delete-me").exists()
    assert (fixture.root / "CAO" / "delete-me.md").exists()


def test_delete_source_targets_native_memory_not_the_new_vault_note(
    tmp_path, monkeypatch, svc
) -> None:
    """Migration must not deindex the vault destination it just published."""
    from cli_agent_orchestrator.services import settings_service
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
    from cli_agent_orchestrator.services.vault.config import VaultConfig

    fixture = build_vault_fixture(tmp_path)
    engine = svc._db_engine
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_args: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: VaultConfig(enabled=False))
    _store(svc, "native-source")
    native_path = svc.get_wiki_path("global", None, "native-source")

    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    report = _migrate(
        svc,
        fixture,
        apply=True,
        delete_source=True,
        confirm_delete_source=True,
        refresh=lambda _path: reconcile_module.reconcile(fixture.vault, apply=True),
    )

    assert report.deleted_source == 1
    assert not native_path.exists()
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="native-source").one()
        assert note.status == "indexed"


def test_migration_reports_each_named_lossy_field(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)
    memory = _store(svc, "lossy", content="first")
    source = svc.get_wiki_path("global", None, "lossy")
    source.write_text(
        "# lossy\n<!-- id: native | scope: global | type: reference | tags:  -->\n\n"
        "## 2025-01-01T00:00:00Z\n" + ("a" * 3900) + "\n\n## 2025-01-02T00:00:00Z\nsecond\n",
        encoding="utf-8",
    )
    with svc._get_db_session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=memory.key).one()
        row.access_count = 2
        row.last_accessed_at = datetime(2025, 1, 1, tzinfo=timezone.utc)
        row.last_compiled_at = datetime(2025, 1, 2, tzinfo=timezone.utc)
        row.source_provider = "provider"
        row.source_terminal_id = "terminal"
        row.related_keys = "other"
        db.commit()

    report = _migrate(svc, fixture)

    assert set(report.lossy_fields["lossy"]) == {
        "access_count",
        "last_accessed_at",
        "last_compiled_at",
        "source_provider",
        "source_terminal_id",
        "related_keys",
        "append_only_section_history",
    }


def test_typed_relationships_are_written_to_cao_links_and_round_trip(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "linked", tags="one,two")
    relationships = _Relationships(
        [
            RelationshipDTO(
                id="edge",
                scope="global",
                scope_id=None,
                source_key="linked",
                target_key="target",
                type="relates_to",
                origin="human",
                status="active",
                confidence=0.75,
                rank=None,
                attributes=None,
                source_updated_at=None,
                created_at=None,
                updated_at=None,
            )
        ]
    )

    report = _migrate(svc, fixture, apply=True, relationship_service=relationships)

    text = (fixture.root / "CAO" / "linked.md").read_text(encoding="utf-8")
    parsed = parse_note(
        text,
        max_frontmatter_bytes=fixture.vault.max_frontmatter_bytes,
        secret_gate="reject",
    )
    assert report.migrated == 1
    assert relationships.calls
    assert parsed.cao["links"] == [
        {
            "to": "target",
            "type": "relates_to",
            "status": "active",
            "origin": "human",
            "confidence": 0.75,
        }
    ]
    assert parsed.frontmatter["tags"] == ["one", "two"]


def test_default_apply_keeps_native_row_when_vault_projection_is_refreshed(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "coexists")

    def refresh(_path):
        with svc._get_db_session() as db:
            db.add(
                MemoryMetadataModel(
                    id="vault-row",
                    key="coexists",
                    memory_type="reference",
                    scope="global",
                    scope_id=None,
                    source_kind="vault",
                    file_path="CAO/coexists.md",
                )
            )
            db.commit()

    report = _migrate(svc, fixture, apply=True, refresh=refresh)

    with svc._get_db_session() as db:
        rows = (
            db.query(MemoryMetadataModel)
            .filter_by(key="coexists", scope="global")
            .order_by(MemoryMetadataModel.source_kind)
            .all()
        )
    assert report.migrated == 1
    assert [row.source_kind for row in rows] == ["native", "vault"]


def test_secret_bearing_item_is_reported_while_later_items_migrate(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "credential", content="password: hunter2sixteen")
    _store(svc, "ordinary", content="ordinary migration body")

    report = _migrate(svc, fixture, apply=True)

    assert report.migrated == 1
    assert report.failed == 1
    assert "credential" in report.errors
    assert "note matched credential pattern" in report.errors["credential"]
    assert (fixture.root / "CAO" / "ordinary.md").exists()
    assert not (fixture.root / "CAO" / "credential.md").exists()


def test_more_than_maximum_links_is_reported_without_silent_truncation(tmp_path, svc) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "many-links")
    rows = [
        RelationshipDTO(
            id=str(index),
            scope="global",
            scope_id=None,
            source_key="many-links",
            target_key=f"target-{index}",
            type="relates_to",
            origin="human",
            status="active",
            confidence=None,
            rank=None,
            attributes=None,
            source_updated_at=None,
            created_at=None,
            updated_at=None,
        )
        for index in range(65)
    ]

    report = _migrate(svc, fixture, relationship_service=_Relationships(rows))

    assert report.lossy_fields["many-links"]["cao.links"] == 1


def test_native_source_guard_raises_its_own_error(tmp_path, svc, monkeypatch) -> None:
    fixture = build_vault_fixture(tmp_path)
    _store(svc, "guarded")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    monkeypatch.setattr(migrate, "MEMORY_BASE_DIR", svc.base_dir)
    monkeypatch.setattr(
        svc,
        "get_wiki_path",
        lambda *_args, **_kwargs: outside,
    )

    report = _migrate(svc, fixture, apply=True)

    assert report.failed == 1
    assert report.errors["guarded"] == "native migration source escapes memory base"
