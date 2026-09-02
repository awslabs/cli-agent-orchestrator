"""Database-backed tests for the vault reconciliation projection."""

from datetime import datetime, timezone
from test.fixtures.vault_factory import build_vault_fixture

import pytest
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


def test_vault_reconciliation_merges_canonical_and_body_links(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "Source.md").write_text(
        """---
cao:
  links:
    - to: target-a
      type: relates_to
      status: proposal
      origin: human
      confidence: 0.7
---
[[Target A#body-fragment]] [[Target B]]
""",
        encoding="utf-8",
    )
    (mapped / "Target A.md").write_text(
        "---\ncao:\n  key: target-a\n---\ntarget a", encoding="utf-8"
    )
    (mapped / "Target B.md").write_text(
        "---\ncao:\n  key: target-b\n---\ntarget b", encoding="utf-8"
    )

    reconcile(vault, apply=True, run_id="canonical-links")

    with Session() as db:
        edges = (
            db.query(MemoryRelationshipModel)
            .filter_by(origin="vault")
            .order_by(MemoryRelationshipModel.target_key)
            .all()
        )
    assert {(edge.type, edge.target_key) for edge in edges} == {
        ("relates_to", "target-a"),
        ("relates_to", "target-b"),
    }
    canonical = next(edge for edge in edges if edge.target_key == "target-a")
    assert canonical.status == "proposal"
    assert canonical.confidence == 0.7
    assert canonical.attributes_json == (
        '{"attested_by":["body","frontmatter"],"authored_origin":"human",'
        '"fragment":"body-fragment"}'
    )


def test_index_disabled_mapping_retracts_projection_and_edges(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "Source.md").write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")

    reconcile(vault, apply=True, run_id="index-on")
    with Session() as db:
        assert db.query(MemoryMetadataModel).filter_by(source_kind="vault").count() == 2
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 1

    vault.mappings[0] = vault.mappings[0].model_copy(update={"index": False})
    reconcile(vault, apply=True, run_id="index-off")

    with Session() as db:
        assert db.query(VaultNoteModel).filter_by(scope="project").count() == 0
        assert (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", scope="project").count()
            == 0
        )
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 0


def test_typed_canonical_link_is_additive_and_removed_by_next_reconcile(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "Source.md"
    source.write_text(
        """---
cao:
  links:
    - {to: target, type: contradiction, status: proposal, confidence: 0.8}
---
[[Target]]
""",
        encoding="utf-8",
    )
    (mapped / "Target.md").write_text("---\ncao:\n  key: target\n---\ntarget", encoding="utf-8")

    reconcile(vault, apply=True, run_id="typed-union")
    with Session() as db:
        rows = (
            db.query(MemoryRelationshipModel)
            .filter_by(origin="vault")
            .order_by(MemoryRelationshipModel.type)
            .all()
        )
        assert [(row.type, row.status, row.confidence) for row in rows] == [
            ("contradiction", "proposal", 0.8),
            ("relates_to", "active", None),
        ]

    source.write_text("[[Target]]", encoding="utf-8")
    reconcile(vault, apply=True, run_id="typed-removed")
    with Session() as db:
        rows = db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
        assert [(row.type, row.status) for row in rows] == [("relates_to", "active")]


def test_conflicting_canonical_duplicates_emit_finding_and_no_edge(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "Source.md"
    (mapped / "Target.md").write_text("---\ncao:\n  key: target\n---\ntarget", encoding="utf-8")
    first = (
        "    - {to: target, type: relates_to, status: active}\n"
        "    - {to: target, type: relates_to, status: proposal}\n"
    )
    second = (
        "    - {to: target, type: relates_to, status: proposal}\n"
        "    - {to: target, type: relates_to, status: active}\n"
    )
    dumps = []
    for run_id, links in (("conflict-a", first), ("conflict-b", second)):
        source.write_text(f"---\ncao:\n  links:\n{links}---\n", encoding="utf-8")
        reconcile(vault, apply=True, run_id=run_id)
        with Session() as db:
            dumps.append(
                [
                    (
                        row.source_key,
                        row.target_key,
                        row.type,
                        row.status,
                        row.attributes_json,
                    )
                    for row in db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
                ]
            )
            assert (
                db.query(VaultFindingModel)
                .filter_by(code="cao_link_conflict", vault_relpath="Mapped/Source.md")
                .count()
                == 1
            )
    assert dumps == [[], []]


def test_body_edges_are_bounded_without_aborting_reconcile(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    targets = [f"target-{index:02d}" for index in range(70)]
    (mapped / "Source.md").write_text(
        " ".join(f"[[{target}]]" for target in targets), encoding="utf-8"
    )
    for target in targets:
        (mapped / f"{target}.md").write_text(
            f"---\ncao:\n  key: {target}\n---\n{target}", encoding="utf-8"
        )

    reconcile(vault, apply=True, run_id="edge-bound")

    with Session() as db:
        rows = (
            db.query(MemoryRelationshipModel)
            .filter_by(origin="vault", type="relates_to")
            .order_by(MemoryRelationshipModel.target_key)
            .all()
        )
        assert [row.target_key for row in rows] == targets[:64]
        assert (
            db.query(VaultFindingModel)
            .filter_by(code="edge_limit_exceeded", vault_relpath="Mapped/Source.md")
            .count()
            == 1
        )


def test_same_path_authored_key_change_migrates_projection_without_old_state(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "One.md"
    source.write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="key-before")
    with Session() as db:
        old_note = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one()
        old_key = old_note.cao_key
        old_scope = old_note.scope
        old_scope_id = old_note.scope_id
        target_key = (
            db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/Target.md").one().cao_key
        )
        metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault", key=old_key).one()
        metadata.access_count = 9
        db.add(
            MemoryRelationshipModel(
                id="incoming-old-key",
                scope=old_scope,
                scope_id=old_scope_id,
                source_key=target_key,
                target_key=old_key,
                type="supersedes",
                origin="vault",
                status="active",
            )
        )
        db.add(
            VaultNoteAliasModel(
                vault_id=vault.id,
                former_relpath="Mapped/Former.md",
                cao_key=old_key,
                scope=old_scope,
                scope_id=old_scope_id,
                content_sha256="old-content",
                created_at=datetime.now(timezone.utc),
            )
        )
        db.commit()

    source.write_text("---\ncao:\n  key: new-key\n---\n[[Target]]", encoding="utf-8")
    reconcile(vault, apply=True, run_id="key-after")

    with Session() as db:
        rows = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").all()
        old_metadata_count = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key=old_key).count()
        )
        old_edge_count = (
            db.query(MemoryRelationshipModel).filter_by(origin="vault", source_key=old_key).count()
            + db.query(MemoryRelationshipModel)
            .filter_by(origin="vault", target_key=old_key)
            .count()
        )
        old_alias_count = (
            db.query(VaultNoteAliasModel).filter_by(vault_id=vault.id, cao_key=old_key).count()
        )
        new_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="new-key").one()
        )
    assert [(row.cao_key, row.vault_relpath) for row in rows] == [("new-key", "Mapped/One.md")]
    assert (
        old_metadata_count,
        old_edge_count,
        old_alias_count,
        new_metadata.access_count,
    ) == (0, 0, 0, 0)


def test_same_path_authored_key_a_to_b_migrates_without_old_edges(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "One.md"
    source.write_text("---\ncao:\n  key: old-key\n---\n[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-key-before")

    source.write_text("---\ncao:\n  key: new-key\n---\n[[Target]]", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-key-after")

    with Session() as db:
        rows = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").all()
        old_metadata_count = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="old-key").count()
        )
        old_edge_count = (
            db.query(MemoryRelationshipModel)
            .filter_by(origin="vault", source_key="old-key")
            .count()
        )
    assert [(row.cao_key, row.vault_relpath) for row in rows] == [("new-key", "Mapped/One.md")]
    assert old_metadata_count == old_edge_count == 0


def test_same_path_key_change_keeps_forgotten_note_excluded(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    source = tmp_path / "vault" / "Mapped" / "One.md"
    source.write_text("---\ncao:\n  key: old-key\n---\nbody", encoding="utf-8")
    reconcile(vault, apply=True, run_id="excluded-before")
    with Session() as db:
        db.query(VaultNoteModel).filter_by(cao_key="old-key").update({"status": "excluded"})
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="old-key").delete()
        db.commit()

    source.write_text("---\ncao:\n  key: new-key\n---\nbody", encoding="utf-8")
    reconcile(vault, apply=True, run_id="excluded-after")

    with Session() as db:
        migrated = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one()
        vault_metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        retained = (
            db.query(VaultFindingModel)
            .filter_by(
                code="deindexed_retained",
                vault_relpath="Mapped/One.md",
            )
            .one()
        )
    assert migrated.cao_key == "new-key"
    assert migrated.status == "excluded"
    assert vault_metadata_count == 0
    assert "deindexed_retained" in retained.detail


def test_removing_authored_key_transitions_to_current_path_derived_key(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    source = tmp_path / "vault" / "Mapped" / "One.md"
    source.write_text("---\ncao:\n  key: authored-key\n---\nbody", encoding="utf-8")
    reconcile(vault, apply=True, run_id="key-present")

    source.write_text("body without authored key", encoding="utf-8")
    reconcile(vault, apply=True, run_id="key-removed")

    expected = derive_cao_key("One.md")
    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one()
        metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
    assert note.cao_key == expected
    assert [(row.key, row.file_path) for row in metadata] == [(expected, "Mapped/One.md")]


def test_same_path_identity_migration_rolls_back_atomically_on_failure(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "One.md"
    source.write_text("---\ncao:\n  key: old-key\n---\n[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="atomic-before")

    original_upsert = module._upsert_note

    def fail_new_identity(db, vault_id, item, started):
        if item.key == "new-key":
            raise RuntimeError("induced migration failure")
        return original_upsert(db, vault_id, item, started)

    monkeypatch.setattr(module, "_upsert_note", fail_new_identity)
    source.write_text("---\ncao:\n  key: new-key\n---\n[[Target]]", encoding="utf-8")

    with pytest.raises(RuntimeError, match="induced migration failure"):
        reconcile(vault, apply=True, run_id="atomic-after")

    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one()
        old_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="old-key").count()
        )
        old_edges = (
            db.query(MemoryRelationshipModel)
            .filter_by(origin="vault", source_key="old-key")
            .count()
        )
    assert note.cao_key == "old-key"
    assert old_metadata == 1
    assert old_edges == 1


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


def test_pure_rename_of_deindexed_note_keeps_it_excluded(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rename-excluded-before")
    with Session() as db:
        db.query(VaultNoteModel).update({"status": "excluded"})
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(old_path.with_name("New.md"))
    reconcile(vault, apply=True, run_id="rename-excluded-after")

    with Session() as db:
        note = db.query(VaultNoteModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        finding = (
            db.query(VaultFindingModel)
            .filter_by(code="deindexed_retained", vault_relpath="Mapped/New.md")
            .one()
        )
    assert (note.vault_relpath, note.status) == ("Mapped/New.md", "excluded")
    assert metadata_count == 0
    assert "deindexed_retained" in finding.detail


def test_rebuild_preserves_deindexed_tombstones(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    source = tmp_path / "vault" / "Mapped" / "One.md"
    source.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rebuild-excluded-before")
    with Session() as db:
        db.query(VaultNoteModel).update({"status": "excluded"})
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    reconcile(vault, apply=True, rebuild=True, run_id="rebuild-excluded-after")

    with Session() as db:
        note = db.query(VaultNoteModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        finding = (
            db.query(VaultFindingModel)
            .filter_by(code="deindexed_retained", vault_relpath="Mapped/One.md")
            .one()
        )
    assert (note.vault_relpath, note.status) == ("Mapped/One.md", "excluded")
    assert metadata_count == 0
    assert "deindexed_retained" in finding.detail


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
