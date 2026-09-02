"""Database-backed tests for the vault reconciliation projection."""

from datetime import datetime, timezone
from test.fixtures.vault_factory import build_vault_fixture

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
    VaultFindingModel,
    VaultNoteAliasModel,
    VaultNoteModel,
)
from cli_agent_orchestrator.services.vault.config import FolderMapping, VaultSpec
from cli_agent_orchestrator.services.vault.identity import derive_cao_key
from cli_agent_orchestrator.services.vault.reconcile import reconcile
from cli_agent_orchestrator.services.vault.scan import scan_vault


def test_rebuild_deletes_only_vault_rows_and_groups_same_code_findings(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(module, "_emit_audit_events", lambda *_args: None)
    (tmp_path / "vault").mkdir()
    vault = _vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    mapped.mkdir(parents=True)
    (tmp_path / "vault" / "CAO").mkdir()
    (mapped / "Links.md").write_text("[[Missing]] [[Missing Again]]", encoding="utf-8")
    with Session() as db:
        db.add(
            MemoryMetadataModel(
                id="native",
                key="native",
                memory_type="reference",
                scope="global",
                scope_id=None,
                source_kind="native",
                file_path="native.md",
            )
        )
        db.add(
            MemoryRelationshipModel(
                id="native-edge",
                scope="global",
                scope_id="",
                source_key="native",
                target_key="other",
                type="relates_to",
                origin="human",
                status="active",
            )
        )
        db.commit()

    report = reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id="run-1",
        run_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    with Session() as db:
        assert db.query(MemoryMetadataModel).filter_by(source_kind="native").count() == 1
        assert db.query(MemoryRelationshipModel).filter_by(origin="human").count() == 1
        assert db.query(VaultNoteModel).count() == 1
        findings = db.query(VaultFindingModel).all()
    assert report.indexed == 1
    dangling = [row for row in findings if row.code == "link_dangling"]
    assert len(dangling) == 1
    assert dangling[0].detail == "count=2; code=link_dangling; detail=link_dangling"


def test_reconcile_emits_completion_note_and_secret_audits(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes: None)
    events = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.audit_log.write_audit_nowait",
        lambda event, summary, **fields: events.append((event, summary, fields)),
    )
    (tmp_path / "vault").mkdir()
    vault = _vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    mapped.mkdir(parents=True)
    (tmp_path / "vault" / "CAO").mkdir()
    (mapped / "Secret.md").write_text("password: hunter2sixteen", encoding="utf-8")

    reconcile(vault, apply=True, run_id="run-2")

    assert [event[0] for event in events] == [
        "vault_reconcile_completed",
        "vault_secret_quarantined",
        "vault_note_quarantined",
    ]
    assert events[2][2]["codes"] == "secret_detected"


def test_warn_mode_secret_still_emits_detection_audit(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes: None)
    events = []
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.audit_log.write_audit_nowait",
        lambda event, summary, **fields: events.append((event, fields)),
    )
    (tmp_path / "vault").mkdir()
    vault = _vault(tmp_path)
    vault.mappings[0] = vault.mappings[0].model_copy(update={"secret_gate": "warn"})
    mapped = tmp_path / "vault" / "Mapped"
    mapped.mkdir()
    (tmp_path / "vault" / "CAO").mkdir()
    (mapped / "Secret.md").write_text("password: hunter2sixteen", encoding="utf-8")

    report = reconcile(vault, apply=True, run_id="warn-secret")

    assert report.indexed == 1
    assert [event for event, _fields in events] == [
        "vault_reconcile_completed",
        "vault_secret_quarantined",
    ]


def test_vault_edges_use_the_relationship_service_with_vault_endpoints(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services import memory_relationship_service
    from cli_agent_orchestrator.services.vault import reconcile as module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", Session)
    monkeypatch.setattr(module, "_emit_audit_events", lambda *_args: None)
    (tmp_path / "vault").mkdir()
    vault = _vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    mapped.mkdir()
    (tmp_path / "vault" / "CAO").mkdir()
    (mapped / "One.md").write_text("[[Two]]", encoding="utf-8")
    (mapped / "Two.md").write_text("two", encoding="utf-8")

    reconcile(vault, apply=True, run_id="edge-run")

    with Session() as db:
        edges = db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
    assert len(edges) == 1
    assert (edges[0].source_key, edges[0].target_key) == (
        derive_cao_key("One.md"),
        derive_cao_key("Two.md"),
    )


def test_fixed_mtime_reverse_fixture_scans_without_unstable_results(tmp_path):
    forward = build_vault_fixture(tmp_path / "forward", fixed_mtimes=True)
    reverse = build_vault_fixture(tmp_path / "reverse", creation_order="reverse", fixed_mtimes=True)

    forward_report = scan_vault(forward.vault)
    reverse_report = scan_vault(reverse.vault)

    assert all(
        finding.code.value != "unstable_skipped"
        for report in (forward_report, reverse_report)
        for note in report.notes
        for finding in note.findings
    )


def test_path_derived_pure_rename_preserves_identity_and_records_alias(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rename-before")
    with Session() as db:
        before = db.query(VaultNoteModel).one()
        original_uid, original_key = before.note_uid, before.cao_key

    old_path.rename(old_path.with_name("New.md"))
    reconcile(vault, apply=True, run_id="rename-after")

    with Session() as db:
        notes = db.query(VaultNoteModel).all()
        aliases = db.query(VaultNoteAliasModel).all()
        metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
    assert [(note.note_uid, note.cao_key, note.vault_relpath) for note in notes] == [
        (original_uid, original_key, "Mapped/New.md")
    ]
    assert [(alias.former_relpath, alias.cao_key) for alias in aliases] == [
        ("Mapped/Old.md", original_key)
    ]
    assert [(row.key, row.file_path) for row in metadata] == [(original_key, "Mapped/New.md")]


def test_authored_key_pure_rename_preserves_canonical_identity(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsame content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-before")
    old_path.rename(old_path.with_name("New.md"))
    reconcile(vault, apply=True, run_id="authored-after")

    with Session() as db:
        notes = db.query(VaultNoteModel).all()
        aliases = db.query(VaultNoteAliasModel).all()
    assert [(note.cao_key, note.vault_relpath) for note in notes] == [
        ("canonical", "Mapped/New.md")
    ]
    assert aliases == []


def test_rename_plus_edit_reports_without_guessing_identity(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("original", encoding="utf-8")
    reconcile(vault, apply=True, run_id="edit-before")
    old_path.rename(old_path.with_name("New.md"))
    (tmp_path / "vault" / "Mapped" / "New.md").write_text("edited", encoding="utf-8")
    report = reconcile(vault, apply=True, run_id="edit-after")

    with Session() as db:
        notes = db.query(VaultNoteModel).all()
        finding = db.query(VaultFindingModel).filter_by(code="rename_with_edit_unresolved").one()
    assert report.findings == 1
    assert [note.vault_relpath for note in notes] == ["Mapped/New.md"]
    assert finding.detail == (
        "count=1; code=rename_with_edit_unresolved; detail=rename_with_edit_unresolved"
    )


def test_rename_plus_edit_dry_run_reports_without_writing_state(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("original", encoding="utf-8")
    reconcile(vault, apply=True, run_id="edit-preview-before")
    old_path.rename(old_path.with_name("New.md"))
    (tmp_path / "vault" / "Mapped" / "New.md").write_text("edited", encoding="utf-8")

    report = reconcile(vault, apply=False, run_id="edit-preview")

    with Session() as db:
        assert [note.vault_relpath for note in db.query(VaultNoteModel).all()] == ["Mapped/Old.md"]
        assert db.query(VaultFindingModel).count() == 0
    assert report.findings == 1
    assert report.deleted == 0


def test_rename_plus_edit_removes_former_source_edges_through_service(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    old_path.write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="edge-before")
    with Session() as db:
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 1

    old_path.rename(mapped / "New.md")
    (mapped / "New.md").write_text("edited", encoding="utf-8")
    reconcile(vault, apply=True, run_id="edge-after")

    with Session() as db:
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 0


def test_duplicate_content_rename_reports_ambiguity_without_guessing(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "One.md").write_text("same", encoding="utf-8")
    (mapped / "Two.md").write_text("same", encoding="utf-8")
    reconcile(vault, apply=True, run_id="ambiguous-before")
    (mapped / "One.md").unlink()
    (mapped / "Two.md").unlink()
    (mapped / "New.md").write_text("same", encoding="utf-8")
    reconcile(vault, apply=True, run_id="ambiguous-after")

    with Session() as db:
        finding = db.query(VaultFindingModel).filter_by(code="rename_ambiguous").one()
    assert finding.detail == "count=1; code=rename_ambiguous; detail=rename_ambiguous"


def test_duplicate_content_rename_dry_run_reports_without_writing_state(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "One.md").write_text("same", encoding="utf-8")
    (mapped / "Two.md").write_text("same", encoding="utf-8")
    reconcile(vault, apply=True, run_id="ambiguous-preview-before")
    (mapped / "One.md").unlink()
    (mapped / "Two.md").unlink()
    (mapped / "New.md").write_text("same", encoding="utf-8")

    report = reconcile(vault, apply=False, run_id="ambiguous-preview")

    with Session() as db:
        assert sorted(note.vault_relpath for note in db.query(VaultNoteModel).all()) == [
            "Mapped/One.md",
            "Mapped/Two.md",
        ]
        assert db.query(VaultFindingModel).count() == 0
    assert report.findings == 1
    assert report.deleted == 0


def test_indexed_note_retracts_and_reindexes_metadata_and_edges(tmp_path, monkeypatch):
    """Design §1101 permits exposure only until the next reconcile, which retracts projections."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "One.md"
    source.write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="indexed")
    with Session() as db:
        source_key = (
            db.query(MemoryMetadataModel)
            .filter_by(source_kind="vault", file_path="Mapped/One.md")
            .one()
            .key
        )

    source.write_text("password: hunter2sixteen", encoding="utf-8")
    quarantined = reconcile(vault, apply=True, run_id="quarantined")
    with Session() as db:
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 0
        assert (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key=source_key).count()
            == 0
        )
        assert db.query(MemoryMetadataModel).filter_by(source_kind="vault").count() == 1
        assert (
            db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one().status
            == "quarantined"
        )
    assert quarantined.quarantined == 1

    source.write_text("[[Target]]", encoding="utf-8")
    reindexed = reconcile(vault, apply=True, run_id="reindexed")
    with Session() as db:
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 1
        assert (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key=source_key).count()
            == 1
        )
        assert db.query(MemoryMetadataModel).filter_by(source_kind="vault").count() == 2
        assert (
            db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one().status
            == "indexed"
        )
    assert reindexed.indexed == 2


def test_duplicate_authored_keys_quarantine_both_notes_with_one_finding_per_path(
    tmp_path, monkeypatch
):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    content = "---\ncao:\n  key: shared\n---\nbody"
    (mapped / "One.md").write_text(content, encoding="utf-8")
    (mapped / "Two.md").write_text(content, encoding="utf-8")

    report = reconcile(vault, apply=True, run_id="collision")

    with Session() as db:
        notes = db.query(VaultNoteModel).order_by(VaultNoteModel.vault_relpath).all()
        findings = db.query(VaultFindingModel).filter_by(code="key_collision").all()
        assert [(note.vault_relpath, note.status) for note in notes] == [
            ("Mapped/One.md", "quarantined"),
            ("Mapped/Two.md", "quarantined"),
        ]
        assert {finding.vault_relpath for finding in findings} == {
            "Mapped/One.md",
            "Mapped/Two.md",
        }
        assert db.query(MemoryMetadataModel).filter_by(source_kind="vault").count() == 0
    assert (report.indexed, report.quarantined) == (0, 2)


def test_incremental_upsert_never_updates_native_metadata(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    (tmp_path / "vault" / "Mapped" / "Vault.md").write_text(
        "---\ncao:\n  key: shared\n---\nvault",
        encoding="utf-8",
    )
    with Session() as db:
        db.add(
            MemoryMetadataModel(
                id="native-shared",
                key="shared",
                memory_type="reference",
                scope="project",
                scope_id="project",
                source_kind="native",
                file_path="native.md",
                tags="native",
            )
        )
        db.commit()

    reconcile(vault, apply=True, run_id="upsert-native")

    with Session() as db:
        native = db.query(MemoryMetadataModel).filter_by(id="native-shared").one()
        assert (native.source_kind, native.file_path, native.tags) == (
            "native",
            "native.md",
            "native",
        )


def test_incremental_delete_never_deletes_native_metadata(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    note = tmp_path / "vault" / "Mapped" / "Vault.md"
    note.write_text("---\ncao:\n  key: shared\n---\nvault", encoding="utf-8")
    reconcile(vault, apply=True, run_id="delete-native-before")
    with Session() as db:
        db.add(
            MemoryMetadataModel(
                id="native-shared",
                key="shared",
                memory_type="reference",
                scope="project",
                scope_id="project",
                source_kind="native",
                file_path="native.md",
                tags="native",
            )
        )
        db.commit()

    note.unlink()
    reconcile(vault, apply=True, run_id="delete-native-after")

    with Session() as db:
        assert db.query(MemoryMetadataModel).filter_by(id="native-shared").count() == 1


def _vault(tmp_path) -> VaultSpec:
    return VaultSpec(
        id="reconcile-test",
        root=str(tmp_path / "vault"),
        managed_folder="CAO",
        max_note_bytes=4096,
        max_notes=100,
        max_frontmatter_bytes=1024,
        mappings=[
            FolderMapping(folder="Mapped", scope="project", scope_id="project"),
            FolderMapping(folder="CAO", scope="global", writable=True),
        ],
    )


def _rename_vault(tmp_path) -> VaultSpec:
    root = tmp_path / "vault"
    root.mkdir()
    (root / "Mapped").mkdir()
    (root / "CAO").mkdir()
    return _vault(tmp_path)


def _session(tmp_path, monkeypatch, module):
    from cli_agent_orchestrator.services import memory_relationship_service

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", Session)
    monkeypatch.setattr(module, "_emit_audit_events", lambda *_args: None)
    return Session
