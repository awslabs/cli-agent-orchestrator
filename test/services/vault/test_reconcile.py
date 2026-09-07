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
    VaultExclusionModel,
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
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes, **_kwargs: None)
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
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes, **_kwargs: None)
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
    monkeypatch.setattr(module, "_replace_vault_edges", lambda _notes, **_kwargs: None)
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


def test_same_path_replacement_does_not_steal_forgotten_identity(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "One.md"
    source.write_text("---\ncao:\n  key: old-key\n---\nbody", encoding="utf-8")
    reconcile(vault, apply=True, run_id="excluded-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="old-key").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="old-key").delete()
        db.commit()

    source.unlink()
    source.write_text("---\ncao:\n  key: new-key\n---\nreplacement", encoding="utf-8")
    reconcile(vault, apply=True, run_id="replacement")

    with Session() as db:
        replacement = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/One.md").one()
        exclusions = db.query(VaultExclusionModel).all()
        metadata_keys = {
            row.key for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    assert (replacement.cao_key, replacement.status) == ("new-key", "indexed")
    assert metadata_keys == {"new-key"}
    assert [row.cao_key for row in exclusions] == ["old-key"]

    (mapped / "Reintroduced.md").write_text(
        "---\ncao:\n  key: old-key\n---\nforgotten body",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, run_id="reintroduced")

    with Session() as db:
        forgotten = db.query(VaultNoteModel).filter_by(cao_key="old-key").one()
        old_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="old-key").count()
        )
    assert (forgotten.vault_relpath, forgotten.status, old_metadata) == (
        "Mapped/Reintroduced.md",
        "excluded",
        0,
    )


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


def test_recreated_former_path_cannot_steal_retained_rename_identity(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    new_path = mapped / "New.md"
    old_path.write_text("original content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="former-path-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_uid, original_key = original.note_uid, original.cao_key

    old_path.rename(new_path)
    reconcile(vault, apply=True, run_id="former-path-renamed")
    old_path.write_text("new occupant", encoding="utf-8")
    reconcile(vault, apply=True, run_id="former-path-reused")

    def snapshot():
        with Session() as db:
            notes = {
                row.vault_relpath: (row.note_uid, row.cao_key, row.status)
                for row in db.query(VaultNoteModel).all()
            }
            aliases = {
                row.former_relpath: row.cao_key for row in db.query(VaultNoteAliasModel).all()
            }
            metadata = [
                (row.key, row.file_path)
                for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
            ]
            findings = [
                (row.code, row.vault_relpath)
                for row in db.query(VaultFindingModel).filter_by(code="key_collision").all()
            ]
        return notes, aliases, metadata, findings

    first = snapshot()
    notes, aliases, metadata, findings = first
    assert notes["Mapped/New.md"] == (original_uid, original_key, "indexed")
    reused_uid, reused_key, reused_status = notes["Mapped/Old.md"]
    assert reused_uid != original_uid
    assert reused_key.startswith(f"{original_key}-collision-")
    assert reused_status == "quarantined"
    assert aliases["Mapped/Old.md"] == original_key
    assert metadata == [(original_key, "Mapped/New.md")]
    assert findings == [("key_collision", "Mapped/Old.md")]

    reconcile(vault, apply=True, run_id="former-path-stable")
    assert snapshot() == first


def test_pure_rename_of_deindexed_note_keeps_it_excluded(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rename-excluded-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).one())
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


def test_rebuild_keeps_authored_key_tombstone_after_rename(tmp_path, monkeypatch):
    """A rebuild carries an exclusion by authored identity, not its former path."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-rebuild-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.rename(old_path.with_name("New.md"))
    reconcile(vault, apply=True, rebuild=True, run_id="authored-rebuild-after")

    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="canonical").one()
        metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").count()
        )
    assert (note.vault_relpath, note.status, metadata) == ("Mapped/New.md", "excluded", 0)


def test_rebuild_keeps_authored_tombstone_through_quarantine_and_restoration(tmp_path, monkeypatch):
    """A rebuild cannot replace an authored exclusion with a transient quarantine."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-rebuild-quarantine-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.rename(new_path)
    new_path.write_text(
        "---\ncao:\n  key: canonical\n---\npassword: hunter2sixteen",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, rebuild=True, run_id="authored-rebuild-quarantine-middle")
    with Session() as db:
        middle = db.query(VaultNoteModel).filter_by(cao_key="canonical").one()
        middle_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").count()
        )
    assert (middle.vault_relpath, middle.status, middle_metadata) == (
        "Mapped/New.md",
        "excluded",
        0,
    )

    new_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-rebuild-quarantine-after")

    with Session() as db:
        restored = db.query(VaultNoteModel).filter_by(cao_key="canonical").one()
        metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").count()
        )
    assert (restored.vault_relpath, restored.status, metadata) == (
        "Mapped/New.md",
        "excluded",
        0,
    )


def test_rebuild_does_not_apply_authored_tombstone_to_former_path_replacement(
    tmp_path, monkeypatch
):
    """An authored tombstone follows its identity, not a different note at its old path."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-rebuild-replacement-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.rename(new_path)
    old_path.write_text("different replacement", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="authored-rebuild-replacement-after")

    with Session() as db:
        notes = db.query(VaultNoteModel).order_by(VaultNoteModel.vault_relpath).all()
        metadata_keys = {
            row.key for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    authored_note, replacement_note = notes
    assert [(note.vault_relpath, note.cao_key, note.status) for note in notes] == [
        ("Mapped/New.md", "canonical", "excluded"),
        ("Mapped/Old.md", replacement_note.cao_key, "indexed"),
    ]
    assert authored_note.cao_key != replacement_note.cao_key
    assert metadata_keys == {replacement_note.cao_key}


def test_malformed_renamed_note_keeps_identity_excluded_without_suppressing_replacement(
    tmp_path, monkeypatch
):
    """Invalid frontmatter cannot erase a forgotten identity or bind its old path."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="malformed-rename-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.rename(new_path)
    new_path.write_text("---\ncao: [\n---\nunparseable", encoding="utf-8")
    old_path.write_text("different replacement", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="malformed-rename-middle")

    with Session() as db:
        replacement = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/Old.md").one()
        exclusions = db.query(VaultExclusionModel).all()
        metadata_keys = {
            row.key for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    assert replacement.status == "indexed"
    assert metadata_keys == {replacement.cao_key}
    assert [(row.cao_key, row.last_known_relpath) for row in exclusions] == [
        ("canonical", "Mapped/Old.md")
    ]

    new_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="malformed-rename-after")

    with Session() as db:
        restored = db.query(VaultNoteModel).filter_by(cao_key="canonical").one()
        canonical_metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").count()
        )
    assert (restored.vault_relpath, restored.status, canonical_metadata) == (
        "Mapped/New.md",
        "excluded",
        0,
    )


def test_reconcile_report_and_audit_count_final_excluded_projection(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    source = tmp_path / "vault" / "Mapped" / "One.md"
    source.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="count-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    emitted = []
    monkeypatch.setattr(module, "_emit_audit_events", lambda *args: emitted.append(args))
    report = reconcile(vault, apply=True, run_id="count-after")

    assert (report.indexed, report.quarantined, report.skipped) == (0, 0, 0)
    assert len(emitted) == 1
    assert emitted[0][3:] == (0, 0, 0)


def test_reused_former_path_alias_cannot_replace_live_renamed_identity(tmp_path, monkeypatch):
    """C.md -> A.md -> D.md cannot replace B.md's retained A.md alias."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    original = mapped / "A.md"
    original.write_text("B content", encoding="utf-8")
    other = mapped / "C.md"
    other.write_text("C content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="alias-owner-before")
    with Session() as db:
        retained = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/A.md").one()
        retained_uid, retained_key = retained.note_uid, retained.cao_key

    original.rename(mapped / "B.md")
    reconcile(vault, apply=True, run_id="alias-owner-b")
    other.rename(mapped / "A.md")
    reconcile(vault, apply=True, run_id="alias-owner-reused")
    (mapped / "A.md").rename(mapped / "D.md")
    reconcile(vault, apply=True, run_id="alias-owner-d")
    reconcile(vault, apply=True, run_id="alias-owner-unchanged")

    with Session() as db:
        retained = db.get(VaultNoteModel, retained_uid)
        alias = db.get(
            VaultNoteAliasModel,
            {"vault_id": vault.id, "former_relpath": "Mapped/A.md"},
        )
        metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key=retained_key).one()
        )
    assert (retained.cao_key, retained.vault_relpath) == (retained_key, "Mapped/B.md")
    assert alias.cao_key == retained_key
    assert (metadata.key, metadata.file_path) == (retained_key, "Mapped/B.md")


def test_authored_key_rename_preserves_exclusion_through_quarantine(tmp_path, monkeypatch):
    """An explicitly forgotten authored identity cannot republish after a move."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="authored-tombstone-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.write_text(
        "---\ncao:\n  key: canonical\n---\npassword: hunter2sixteen",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, run_id="authored-tombstone-quarantined")
    old_path.rename(old_path.with_name("New.md"))
    (tmp_path / "vault" / "Mapped" / "New.md").write_text(
        "---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8"
    )
    reconcile(vault, apply=True, run_id="authored-tombstone-renamed")

    with Session() as db:
        note = db.query(VaultNoteModel).filter_by(cao_key="canonical").one()
        metadata = (
            db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").count()
        )
    assert (note.vault_relpath, note.status, metadata) == ("Mapped/New.md", "excluded", 0)


def test_authored_key_tombstone_survives_rename_into_quarantine_and_restoration(
    tmp_path, monkeypatch
):
    """A move through an unresolved quarantined identity cannot resurrect a forgotten note."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="quarantined-rename-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).filter_by(cao_key="canonical").one())
        db.query(MemoryMetadataModel).filter_by(source_kind="vault", key="canonical").delete()
        db.commit()

    old_path.rename(new_path)
    new_path.write_text("password: hunter2sixteen", encoding="utf-8")
    reconcile(vault, apply=True, run_id="quarantined-rename-middle")
    with Session() as db:
        middle = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/New.md").one()
        middle_metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert (middle.status, middle_metadata) == ("quarantined", 0)

    new_path.write_text("---\ncao:\n  key: canonical\n---\nsafe", encoding="utf-8")
    reconcile(vault, apply=True, run_id="quarantined-rename-after")

    with Session() as db:
        restored = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/New.md").one()
        metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        retained = (
            db.query(VaultFindingModel)
            .filter_by(code="deindexed_retained", vault_relpath="Mapped/New.md")
            .count()
        )
    assert (restored.cao_key, restored.status, metadata) == ("canonical", "excluded", 0)
    assert retained == 1


def test_reconcile_relationship_audits_emit_after_commit_and_not_after_rollback(
    tmp_path, monkeypatch
):
    from cli_agent_orchestrator.services import memory_relationship_service
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "Source.md"
    source.write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    committed_edge_counts = []

    def record_replace_audit(self, *_args):
        with Session() as db:
            committed_edge_counts.append(
                db.query(MemoryRelationshipModel).filter_by(origin="vault").count()
            )

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService,
        "_audit_replace_set",
        record_replace_audit,
    )
    reconcile(vault, apply=True, run_id="audit-after-commit")

    assert committed_edge_counts
    assert set(committed_edge_counts) == {1}

    committed_edge_counts.clear()
    source.write_text("no links", encoding="utf-8")
    monkeypatch.setattr(
        module,
        "_persist_findings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("induced rollback")),
    )

    with pytest.raises(RuntimeError, match="induced rollback"):
        reconcile(vault, apply=True, run_id="audit-after-rollback")

    assert committed_edge_counts == []
    with Session() as db:
        assert db.query(MemoryRelationshipModel).filter_by(origin="vault").count() == 1


def test_empty_and_scalar_frontmatter_aliases_are_normalized_for_link_projection(
    tmp_path, monkeypatch
):
    """Null aliases are empty; a scalar alias is one alias, never characters."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "Source.md").write_text("[[Whole Alias]] [[Missing]]", encoding="utf-8")
    (mapped / "Target.md").write_text("---\naliases: Whole Alias\n---\ntarget", encoding="utf-8")
    (mapped / "Empty.md").write_text("---\naliases:\n---\nempty", encoding="utf-8")

    report = reconcile(vault, apply=True, run_id="normalized-aliases")

    with Session() as db:
        edges = db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
        dangling = db.query(VaultFindingModel).filter_by(code="link_dangling").count()
    assert report.indexed == 3
    assert [(edge.source_key, edge.target_key) for edge in edges] == [
        (derive_cao_key("Source.md"), derive_cao_key("Target.md"))
    ]
    assert dangling == 1


def test_invalid_frontmatter_alias_shape_is_ignored_without_aborting_reconcile(
    tmp_path, monkeypatch
):
    """A non-string alias member is ignored without aborting reconciliation."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    (mapped / "Source.md").write_text("[[42]]", encoding="utf-8")
    (mapped / "Target.md").write_text("---\naliases: [42]\n---\ntarget", encoding="utf-8")

    report = reconcile(vault, apply=True, run_id="invalid-aliases")

    with Session() as db:
        edge_count = db.query(MemoryRelationshipModel).filter_by(origin="vault").count()
        dangling = db.query(VaultFindingModel).filter_by(code="link_dangling").count()
    assert (report.indexed, edge_count, dangling) == (2, 0, 1)


def test_path_reuse_across_repeated_renames_upserts_former_path_alias(tmp_path, monkeypatch):
    """A -> B -> A -> B must not collide on the former-path alias primary key."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    current = mapped / "A.md"
    current.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="path-reuse-before")
    for run_id, name in (
        ("path-reuse-b", "B.md"),
        ("path-reuse-a", "A.md"),
        ("path-reuse-b-again", "B.md"),
    ):
        next_path = mapped / name
        current.rename(next_path)
        current = next_path
        reconcile(vault, apply=True, run_id=run_id)

    with Session() as db:
        notes = db.query(VaultNoteModel).all()
        aliases = db.query(VaultNoteAliasModel).order_by(VaultNoteAliasModel.former_relpath).all()
    assert [(note.vault_relpath, note.status) for note in notes] == [("Mapped/B.md", "indexed")]
    assert [alias.former_relpath for alias in aliases] == ["Mapped/A.md", "Mapped/B.md"]


def test_reconcile_rolls_back_stale_edge_retraction_when_projection_fails(tmp_path, monkeypatch):
    """A failed apply leaves edge, projection, and findings at the prior committed state."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source = mapped / "Source.md"
    source.write_text("[[Target]]", encoding="utf-8")
    (mapped / "Target.md").write_text("target", encoding="utf-8")
    reconcile(vault, apply=True, run_id="atomic-retraction-before")

    original_upsert = module._upsert_note

    def fail_quarantined_source(db, vault_id, item, started):
        if item.note.vault_relpath == "Mapped/Source.md":
            raise RuntimeError("induced projection failure")
        return original_upsert(db, vault_id, item, started)

    monkeypatch.setattr(module, "_upsert_note", fail_quarantined_source)
    source.write_text("password: hunter2sixteen", encoding="utf-8")

    with pytest.raises(RuntimeError, match="induced projection failure"):
        reconcile(vault, apply=True, run_id="atomic-retraction-after")

    with Session() as db:
        source_note = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/Source.md").one()
        source_metadata = (
            db.query(MemoryMetadataModel)
            .filter_by(source_kind="vault", key=source_note.cao_key)
            .count()
        )
        vault_edges = db.query(MemoryRelationshipModel).filter_by(origin="vault").count()
        findings = db.query(VaultFindingModel).count()
    assert (source_note.status, source_metadata, vault_edges, findings) == ("indexed", 1, 1, 0)


def test_rebuild_preserves_deindexed_tombstones(tmp_path, monkeypatch):
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    source = tmp_path / "vault" / "Mapped" / "One.md"
    source.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rebuild-excluded-before")
    with Session() as db:
        _exclude_note(db, db.query(VaultNoteModel).one())
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


def test_rebuild_preserves_path_derived_tombstone_when_first_observing_rename(
    tmp_path, monkeypatch
):
    """A rebuild resolves a forgotten pure rename before deleting prior identity rows."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="direct-rebuild-rename-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(new_path)
    report = reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id="direct-rebuild-rename-after",
    )

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        exclusion = db.query(VaultExclusionModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        retained = (
            db.query(VaultFindingModel)
            .filter_by(code="deindexed_retained", vault_relpath="Mapped/New.md")
            .count()
        )
    assert rebuilt.cao_key != original_key
    assert (rebuilt.vault_relpath, rebuilt.status, metadata_count) == (
        "Mapped/New.md",
        "excluded",
        0,
    )
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        rebuilt.cao_key,
        "Mapped/New.md",
    )
    assert (report.indexed, retained) == (0, 1)


def test_rebuild_first_observed_rename_does_not_exclude_former_path_replacement(
    tmp_path, monkeypatch
):
    """Exact content follows the rename when its former path is simultaneously reused."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="direct-rebuild-reuse-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(new_path)
    old_path.write_text("unrelated replacement", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="direct-rebuild-reuse-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        exclusion = db.query(VaultExclusionModel).one()
    renamed_key, renamed_status = notes["Mapped/New.md"]
    replacement_key, replacement_status = notes["Mapped/Old.md"]
    assert renamed_key != original_key
    assert renamed_status == "excluded"
    assert replacement_status == "indexed"
    assert renamed_key != replacement_key
    assert metadata == {"Mapped/Old.md"}
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        renamed_key,
        "Mapped/New.md",
    )


@pytest.mark.parametrize("moved_name", ["A-Moved.md", "Z-Moved.md"])
def test_rebuild_authored_identity_claim_blocks_hash_tombstone_move(
    tmp_path, monkeypatch, moved_name
):
    """An exact authored owner and its competing hash claimant are quarantined."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / moved_name
    authored_path = mapped / "M-Authored.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"authored-claim-{moved_name}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nauthored claimant",
        encoding="utf-8",
    )
    report = reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id=f"authored-claim-{moved_name}-conflict",
    )

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        exclusion = db.query(VaultExclusionModel).one()
    assert {status for _key, status in notes.values()} == {"quarantined"}
    assert all(key != original_key for key, _status in notes.values())
    assert metadata_count == 0
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        original_key,
        "Mapped/Old.md",
    )
    assert (report.indexed, report.quarantined) == (0, 2)

    repeated = reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id=f"authored-claim-{moved_name}-repeated",
    )
    with Session() as db:
        repeated_statuses = {row.status for row in db.query(VaultNoteModel).all()}
        repeated_metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        repeated_exclusion = db.query(VaultExclusionModel).one()
    assert repeated_statuses == {"quarantined"}
    assert repeated_metadata == 0
    assert (repeated_exclusion.cao_key, repeated_exclusion.last_known_relpath) == (
        original_key,
        "Mapped/Old.md",
    )
    assert (repeated.indexed, repeated.quarantined) == (0, 2)


@pytest.mark.parametrize("moved_name", ["A-Renamed.md", "Z-Renamed.md"])
def test_rebuild_authored_identity_claim_blocks_alias_carried_owner(
    tmp_path, monkeypatch, moved_name
):
    """An unchanged alias-carried owner keeps its tombstone through rebuild."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / moved_name
    authored_path = mapped / "M-Authored.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"alias-authored-{moved_name}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    reconcile(vault, apply=True, run_id=f"alias-authored-{moved_name}-renamed")
    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nauthored claimant",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, run_id=f"alias-authored-{moved_name}-ordinary")

    moved_relpath = f"Mapped/{moved_name}"
    with Session() as db:
        ordinary = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        ordinary_metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        assert db.query(VaultNoteAliasModel).count() == 1
    assert ordinary[moved_relpath] == (original_key, "excluded")
    assert ordinary["Mapped/M-Authored.md"][1] == "quarantined"
    assert ordinary_metadata == 0

    for suffix in ("rebuilt", "stable"):
        report = reconcile(
            vault,
            apply=True,
            rebuild=True,
            run_id=f"alias-authored-{moved_name}-{suffix}",
        )
        with Session() as db:
            notes = {
                row.vault_relpath: (row.cao_key, row.status)
                for row in db.query(VaultNoteModel).all()
            }
            metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
            exclusion = db.query(VaultExclusionModel).one()
            alias_count = db.query(VaultNoteAliasModel).count()
        assert notes[moved_relpath] == (original_key, "excluded")
        assert notes["Mapped/M-Authored.md"][1] == "quarantined"
        assert all(key != original_key for key, status in notes.values() if status == "quarantined")
        assert metadata_count == 0
        assert exclusion.cao_key == original_key
        assert alias_count == 1
        assert (report.indexed, report.quarantined) == (0, 1)
        moved_path.write_text(
            f"edited forgotten content after {suffix}",
            encoding="utf-8",
        )


@pytest.mark.parametrize("moved_name", ["A-Edited.md", "Z-Edited.md"])
def test_rebuild_authored_identity_claim_blocks_edited_alias_owner(
    tmp_path, monkeypatch, moved_name
):
    """An edited alias-carried owner retains its tombstone against an authored claim."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / moved_name
    authored_path = mapped / "M-Authored.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"edited-authored-{moved_name}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    reconcile(vault, apply=True, run_id=f"edited-authored-{moved_name}-renamed")
    moved_path.write_text("edited forgotten content", encoding="utf-8")
    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nauthored claimant",
        encoding="utf-8",
    )

    moved_relpath = f"Mapped/{moved_name}"
    for suffix in ("rebuilt", "stable"):
        report = reconcile(
            vault,
            apply=True,
            rebuild=True,
            run_id=f"edited-authored-{moved_name}-{suffix}",
        )
        with Session() as db:
            notes = {
                row.vault_relpath: (row.cao_key, row.status)
                for row in db.query(VaultNoteModel).all()
            }
            metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
            exclusion = db.query(VaultExclusionModel).one()
        assert notes[moved_relpath] == (original_key, "excluded")
        assert notes["Mapped/M-Authored.md"][1] == "quarantined"
        assert all(key != original_key for key, status in notes.values() if status == "quarantined")
        assert metadata_count == 0
        assert exclusion.cao_key == original_key
        assert (report.indexed, report.quarantined) == (0, 1)


@pytest.mark.parametrize("moved_name", ["A-Quarantined.md", "Z-Quarantined.md"])
def test_rebuild_authored_identity_claim_preserves_quarantined_alias_owner(
    tmp_path, monkeypatch, moved_name
):
    """Transient quarantine cannot revoke an alias-carried tombstone owner."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / moved_name
    authored_path = mapped / "M-Authored.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"quarantined-authored-{moved_name}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    reconcile(vault, apply=True, run_id=f"quarantined-authored-{moved_name}-renamed")
    moved_path.write_text("password: hunter2sixteen", encoding="utf-8")
    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nauthored claimant",
        encoding="utf-8",
    )

    moved_relpath = f"Mapped/{moved_name}"
    for suffix in ("quarantined", "repeated"):
        reconcile(
            vault,
            apply=True,
            rebuild=True,
            run_id=f"quarantined-authored-{moved_name}-{suffix}",
        )
        with Session() as db:
            notes = {
                row.vault_relpath: (row.cao_key, row.status)
                for row in db.query(VaultNoteModel).all()
            }
            metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
            alias_count = db.query(VaultNoteAliasModel).count()
        assert notes[moved_relpath] == (original_key, "excluded")
        assert notes["Mapped/M-Authored.md"][1] == "quarantined"
        assert metadata_count == 0
        assert alias_count == 1

    moved_path.write_text("safe restored content", encoding="utf-8")
    reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id=f"quarantined-authored-{moved_name}-restored",
    )
    with Session() as db:
        restored = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        restored_metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        exclusion = db.query(VaultExclusionModel).one()
        alias_count = db.query(VaultNoteAliasModel).count()
    assert restored[moved_relpath] == (original_key, "excluded")
    assert restored["Mapped/M-Authored.md"][1] == "quarantined"
    assert restored_metadata == 0
    assert exclusion.cao_key == original_key
    assert alias_count == 1


def test_rebuild_duplicate_authored_claimants_quarantine_hash_claimant(tmp_path, monkeypatch):
    """Pre-quarantined authored duplicates still reserve their canonical identity."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / "Moved.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="duplicate-authored-claim-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    for name in ("A-Authored.md", "Z-Authored.md"):
        (mapped / name).write_text(
            f"---\ncao:\n  key: {original_key}\n---\n{name}",
            encoding="utf-8",
        )
    reconcile(vault, apply=True, rebuild=True, run_id="duplicate-authored-claim-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        exclusion = db.query(VaultExclusionModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert {status for _key, status in notes.values()} == {"quarantined"}
    assert all(key != original_key for key, _status in notes.values())
    assert metadata_count == 0
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        original_key,
        "Mapped/Old.md",
    )


def test_rebuild_reconsiders_hash_claim_after_authored_claimant_removal_and_restoration(
    tmp_path, monkeypatch
):
    """A removed exact claimant releases a unique move without losing its tombstone."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    moved_path = mapped / "Moved.md"
    authored_path = mapped / "Authored.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="claimant-lifecycle-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(moved_path)
    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nauthored claimant",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, rebuild=True, run_id="claimant-lifecycle-conflict")
    authored_path.unlink()
    reconcile(vault, apply=True, rebuild=True, run_id="claimant-lifecycle-removed")

    with Session() as db:
        moved = db.query(VaultNoteModel).one()
        exclusion = db.query(VaultExclusionModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert (moved.vault_relpath, moved.status, metadata_count) == (
        "Mapped/Moved.md",
        "excluded",
        0,
    )
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        moved.cao_key,
        "Mapped/Moved.md",
    )

    authored_path.write_text(
        f"---\ncao:\n  key: {original_key}\n---\nrestored claimant",
        encoding="utf-8",
    )
    reconcile(vault, apply=True, rebuild=True, run_id="claimant-lifecycle-restored")
    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    assert notes["Mapped/Moved.md"] == (moved.cao_key, "excluded")
    assert notes["Mapped/Authored.md"] == (original_key, "indexed")
    assert metadata == {"Mapped/Authored.md"}


@pytest.mark.parametrize("cycle_size", [2, 3])
def test_rebuild_carries_tombstone_through_reused_path_cycle(tmp_path, monkeypatch, cycle_size):
    """Every changed path is a destination, so forgotten content follows a swap."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    paths = [mapped / f"{letter}.md" for letter in ("A", "B", "C")[:cycle_size]]
    for index, path in enumerate(paths):
        path.write_text(f"cycle-content-{index}", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"cycle-{cycle_size}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/A.md").one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(
            source_kind="vault",
            key=original_key,
        ).delete()
        db.commit()

    temporary = mapped / "cycle.tmp"
    paths[0].rename(temporary)
    for index in range(1, cycle_size):
        paths[index].rename(paths[index - 1])
    temporary.rename(paths[-1])
    forgotten_relpath = f"Mapped/{paths[-1].name}"
    reconcile(vault, apply=True, rebuild=True, run_id=f"cycle-{cycle_size}-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status, row.content_sha256)
            for row in db.query(VaultNoteModel).all()
        }
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        exclusion = db.query(VaultExclusionModel).one()
    forgotten_key, forgotten_status, forgotten_hash = notes[forgotten_relpath]
    assert forgotten_status == "excluded"
    assert metadata == {f"Mapped/{path.name}" for path in paths[:-1]}
    assert (exclusion.cao_key, exclusion.last_known_relpath, exclusion.content_sha256) == (
        forgotten_key,
        forgotten_relpath,
        forgotten_hash,
    )

    reconcile(vault, apply=True, rebuild=True, run_id=f"cycle-{cycle_size}-stable")
    with Session() as db:
        stable = db.query(VaultNoteModel).filter_by(vault_relpath=forgotten_relpath).one()
        stable_metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    assert (stable.cao_key, stable.status) == (forgotten_key, "excluded")
    assert stable_metadata == metadata


def test_rebuild_swaps_multiple_tombstones_without_consuming_either(tmp_path, monkeypatch):
    """A cycle migrates all exclusions from one ownership snapshot."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    first = mapped / "A.md"
    second = mapped / "B.md"
    first.write_text("first forgotten content", encoding="utf-8")
    second.write_text("second forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="multi-tombstone-before")
    with Session() as db:
        for note in db.query(VaultNoteModel).all():
            _exclude_note(db, note)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    temporary = mapped / "swap.tmp"
    first.rename(temporary)
    second.rename(first)
    temporary.rename(second)
    reconcile(vault, apply=True, rebuild=True, run_id="multi-tombstone-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status, row.content_sha256)
            for row in db.query(VaultNoteModel).all()
        }
        exclusions = {
            row.last_known_relpath: (row.cao_key, row.content_sha256)
            for row in db.query(VaultExclusionModel).all()
        }
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert {status for _key, status, _hash in notes.values()} == {"excluded"}
    assert metadata_count == 0
    assert exclusions == {
        path: (key, content_hash) for path, (key, _status, content_hash) in notes.items()
    }


def test_rebuild_counts_hash_claims_by_identity_not_shared_former_path(tmp_path, monkeypatch):
    """Distinct exclusions sharing path provenance can each move uniquely."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    first = mapped / "A.md"
    second = mapped / "B.md"
    first.write_text("first forgotten content", encoding="utf-8")
    second.write_text("second forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="shared-former-path-before")
    with Session() as db:
        for note in db.query(VaultNoteModel).all():
            _exclude_note(db, note)
        for exclusion in db.query(VaultExclusionModel).all():
            exclusion.last_known_relpath = "Mapped/Shared.md"
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.query(VaultNoteModel).delete()
        db.commit()

    first.unlink()
    second.unlink()
    (mapped / "C.md").write_text("first forgotten content", encoding="utf-8")
    (mapped / "D.md").write_text("second forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="shared-former-path-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        exclusions = {
            (row.cao_key, row.last_known_relpath) for row in db.query(VaultExclusionModel).all()
        }
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert {status for _key, status in notes.values()} == {"excluded"}
    assert metadata_count == 0
    assert exclusions == {(key, path) for path, (key, _status) in notes.items()}


def test_rebuild_claim_graph_resolution_rolls_back_atomically(tmp_path, monkeypatch):
    """A failure after graph resolution preserves the prior projection and tombstone."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    first = mapped / "A.md"
    second = mapped / "B.md"
    first.write_text("forgotten content", encoding="utf-8")
    second.write_text("other content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="claim-rollback-before")
    with Session() as db:
        original = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/A.md").one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(
            source_kind="vault",
            key=original_key,
        ).delete()
        db.commit()
        before_notes = sorted(
            (
                row.vault_relpath,
                row.cao_key,
                row.status,
                row.content_sha256,
            )
            for row in db.query(VaultNoteModel).all()
        )
        before_exclusion_row = db.query(VaultExclusionModel).one()
        before_exclusion = (
            before_exclusion_row.cao_key,
            before_exclusion_row.last_known_relpath,
            before_exclusion_row.content_sha256,
            before_exclusion_row.created_at,
        )
        before_metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        before_aliases = [
            (row.former_relpath, row.cao_key, row.content_sha256)
            for row in db.query(VaultNoteAliasModel).all()
        ]
        before_findings = [
            (row.code, row.vault_relpath, row.detail) for row in db.query(VaultFindingModel).all()
        ]
        before_relationships = [
            (row.source_key, row.target_key, row.status)
            for row in db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
        ]

    temporary = mapped / "swap.tmp"
    first.rename(temporary)
    second.rename(first)
    temporary.rename(second)
    persist_findings = module._persist_findings
    monkeypatch.setattr(
        module,
        "_persist_findings",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("induced rollback")),
    )
    with pytest.raises(RuntimeError, match="induced rollback"):
        reconcile(vault, apply=True, rebuild=True, run_id="claim-rollback-failed")

    with Session() as db:
        after_notes = sorted(
            (
                row.vault_relpath,
                row.cao_key,
                row.status,
                row.content_sha256,
            )
            for row in db.query(VaultNoteModel).all()
        )
        after_exclusion_row = db.query(VaultExclusionModel).one()
        after_exclusion = (
            after_exclusion_row.cao_key,
            after_exclusion_row.last_known_relpath,
            after_exclusion_row.content_sha256,
            after_exclusion_row.created_at,
        )
        after_metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        after_aliases = [
            (row.former_relpath, row.cao_key, row.content_sha256)
            for row in db.query(VaultNoteAliasModel).all()
        ]
        after_findings = [
            (row.code, row.vault_relpath, row.detail) for row in db.query(VaultFindingModel).all()
        ]
        after_relationships = [
            (row.source_key, row.target_key, row.status)
            for row in db.query(MemoryRelationshipModel).filter_by(origin="vault").all()
        ]
    assert (
        after_notes,
        after_exclusion,
        after_metadata,
        after_aliases,
        after_findings,
        after_relationships,
    ) == (
        before_notes,
        before_exclusion,
        before_metadata,
        before_aliases,
        before_findings,
        before_relationships,
    )

    monkeypatch.setattr(module, "_persist_findings", persist_findings)
    reconcile(vault, apply=True, rebuild=True, run_id="claim-rollback-retried")
    with Session() as db:
        moved = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/B.md").one()
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
    assert moved.status == "excluded"
    assert metadata == {"Mapped/A.md"}


def test_rebuild_does_not_carry_tombstone_between_hashless_notes(tmp_path, monkeypatch):
    """Missing hashes are not proof that an unrelated new path is the same note."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="hashless-rebuild-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.write_text("x" * (vault.max_note_bytes + 1), encoding="utf-8")
    reconcile(vault, apply=True, run_id="hashless-rebuild-forgotten-oversize")
    with Session() as db:
        forgotten = db.query(VaultNoteModel).one()
        assert (forgotten.cao_key, forgotten.status, forgotten.content_sha256) == (
            original_key,
            "excluded",
            None,
        )

    old_path.unlink()
    new_path.write_text("y" * (vault.max_note_bytes + 1), encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="hashless-rebuild-unrelated")

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        exclusion = db.query(VaultExclusionModel).one()
        assert (rebuilt.vault_relpath, rebuilt.status, rebuilt.content_sha256) == (
            "Mapped/New.md",
            "skipped",
            None,
        )
        assert (exclusion.cao_key, exclusion.last_known_relpath) == (
            original_key,
            "Mapped/Old.md",
        )

    new_path.write_text("unrelated readable content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="hashless-rebuild-readable")

    with Session() as db:
        readable = db.query(VaultNoteModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        exclusion = db.query(VaultExclusionModel).one()
    assert (readable.vault_relpath, readable.status, metadata_count) == (
        "Mapped/New.md",
        "indexed",
        1,
    )
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        original_key,
        "Mapped/Old.md",
    )


def test_rebuild_treats_hashless_to_readable_reused_path_as_destination(tmp_path, monkeypatch):
    """A None-to-hash transition can receive a unique forgotten identity."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    forgotten_path = mapped / "A.md"
    reused_path = mapped / "B.md"
    forgotten_path.write_text("forgotten content", encoding="utf-8")
    reused_path.write_text("x" * (vault.max_note_bytes + 1), encoding="utf-8")
    reconcile(vault, apply=True, run_id="hashless-destination-before")
    with Session() as db:
        forgotten = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/A.md").one()
        original_key = forgotten.cao_key
        hashless = db.query(VaultNoteModel).filter_by(vault_relpath="Mapped/B.md").one()
        assert (hashless.status, hashless.content_sha256) == ("skipped", None)
        _exclude_note(db, forgotten)
        db.query(MemoryMetadataModel).filter_by(
            source_kind="vault",
            key=original_key,
        ).delete()
        db.commit()

    forgotten_path.unlink()
    reused_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="hashless-destination-after")

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        exclusion = db.query(VaultExclusionModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert (rebuilt.vault_relpath, rebuilt.status, metadata_count) == (
        "Mapped/B.md",
        "excluded",
        0,
    )
    assert rebuilt.content_sha256 is not None
    assert (exclusion.cao_key, exclusion.last_known_relpath, exclusion.content_sha256) == (
        rebuilt.cao_key,
        "Mapped/B.md",
        rebuilt.content_sha256,
    )


def test_rebuild_does_not_overwrite_stationary_destination_tombstone(tmp_path, monkeypatch):
    """A unique move cannot consume an exclusion already owned at its destination."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    source_path = mapped / "A.md"
    destination_path = mapped / "B.md"
    source_path.write_text("first forgotten content", encoding="utf-8")
    destination_path.write_text("second forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="stationary-destination-before")
    with Session() as db:
        originals = {row.vault_relpath: row for row in db.query(VaultNoteModel).all()}
        original_keys = {path: row.cao_key for path, row in originals.items()}
        for note in originals.values():
            _exclude_note(db, note)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    source_path.unlink()
    destination_path.write_text("first forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="stationary-destination-after")

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        exclusions = {
            (row.cao_key, row.last_known_relpath) for row in db.query(VaultExclusionModel).all()
        }
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert (rebuilt.vault_relpath, rebuilt.status, metadata_count) == (
        "Mapped/B.md",
        "excluded",
        0,
    )
    assert exclusions == {
        (original_keys["Mapped/A.md"], "Mapped/A.md"),
        (original_keys["Mapped/B.md"], "Mapped/B.md"),
    }


def test_rebuild_preserves_alias_carried_tombstone_after_content_edit(tmp_path, monkeypatch):
    """An established rename identity stays forgotten when its current content changes."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("original content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="edited-rebuild-rename-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        original_hash = original.content_sha256
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(new_path)
    reconcile(vault, apply=True, run_id="edited-rebuild-rename-carried")
    with Session() as db:
        carried = db.query(VaultNoteModel).one()
        assert (carried.cao_key, carried.status) == (original_key, "excluded")
        assert db.query(VaultNoteAliasModel).count() == 1

    new_path.write_text("edited after identity-preserving rename", encoding="utf-8")
    report = reconcile(
        vault,
        apply=True,
        rebuild=True,
        run_id="edited-rebuild-rename-after",
    )

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        exclusion = db.query(VaultExclusionModel).one()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert rebuilt.cao_key != original_key
    assert rebuilt.content_sha256 != original_hash
    assert (rebuilt.vault_relpath, rebuilt.status, metadata_count) == (
        "Mapped/New.md",
        "excluded",
        0,
    )
    assert (
        exclusion.cao_key,
        exclusion.last_known_relpath,
        exclusion.content_sha256,
    ) == (
        rebuilt.cao_key,
        "Mapped/New.md",
        rebuilt.content_sha256,
    )
    assert report.indexed == 0


@pytest.mark.parametrize("moved_name", ["A-Moved.md", "Z-Moved.md"])
def test_rebuild_exact_hash_move_outranks_alias_path_replacement(tmp_path, monkeypatch, moved_name):
    """An exact-hash move exclusively owns an alias-carried tombstone."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    current_path = old_path.with_name("New.md")
    moved_path = old_path.with_name(moved_name)
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id=f"alias-reuse-{moved_name}-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(current_path)
    reconcile(vault, apply=True, run_id=f"alias-reuse-{moved_name}-carried")
    with Session() as db:
        assert db.query(VaultNoteAliasModel).count() == 1
        assert db.query(VaultNoteModel).one().status == "excluded"

    current_path.rename(moved_path)
    current_path.write_text("unrelated replacement", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id=f"alias-reuse-{moved_name}-rebuilt")

    moved_relpath = f"Mapped/{moved_name}"
    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        exclusions = db.query(VaultExclusionModel).all()
    moved_key, moved_status = notes[moved_relpath]
    replacement_key, replacement_status = notes["Mapped/New.md"]
    assert moved_status == "excluded"
    assert replacement_status == "indexed"
    assert moved_key != replacement_key
    assert metadata == {"Mapped/New.md"}
    assert [(row.cao_key, row.last_known_relpath) for row in exclusions] == [
        (moved_key, moved_relpath)
    ]


def test_rebuild_ambiguous_hash_moves_do_not_exclude_alias_path_replacement(tmp_path, monkeypatch):
    """Contested forgotten hashes are quarantined without hiding a replacement."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    current_path = old_path.with_name("New.md")
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="ambiguous-alias-reuse-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(current_path)
    reconcile(vault, apply=True, run_id="ambiguous-alias-reuse-carried")
    with Session() as db:
        assert db.query(VaultNoteAliasModel).count() == 1
        assert db.query(VaultNoteModel).one().status == "excluded"

    current_path.rename(old_path.with_name("A.md"))
    old_path.with_name("B.md").write_text("forgotten content", encoding="utf-8")
    current_path.write_text("unrelated replacement", encoding="utf-8")
    for suffix in ("rebuilt", "stable"):
        reconcile(
            vault,
            apply=True,
            rebuild=True,
            run_id=f"ambiguous-alias-reuse-{suffix}",
        )

        with Session() as db:
            notes = {row.vault_relpath: row.status for row in db.query(VaultNoteModel).all()}
            metadata = {
                row.file_path
                for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
            }
            exclusion = db.query(VaultExclusionModel).one()
            ambiguous_paths = {
                row.vault_relpath
                for row in db.query(VaultFindingModel).filter_by(code="rename_ambiguous").all()
            }
        assert notes == {
            "Mapped/A.md": "quarantined",
            "Mapped/B.md": "quarantined",
            "Mapped/New.md": "indexed",
        }
        assert metadata == {"Mapped/New.md"}
        assert (exclusion.cao_key, exclusion.last_known_relpath) == (
            original_key,
            "Mapped/Old.md",
        )
        assert ambiguous_paths == {"Mapped/A.md", "Mapped/B.md"}


def test_rebuild_ambiguous_hash_moves_do_not_exclude_former_path_replacement(tmp_path, monkeypatch):
    """A reused former path cannot suppress the forgotten-content claim source."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    mapped = tmp_path / "vault" / "Mapped"
    old_path = mapped / "Old.md"
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="ambiguous-former-reuse-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        original_hash = original.content_sha256
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    (mapped / "A.md").write_text("forgotten content", encoding="utf-8")
    (mapped / "B.md").write_text("forgotten content", encoding="utf-8")
    old_path.write_text("unrelated replacement", encoding="utf-8")

    for suffix in ("rebuilt", "stable"):
        reconcile(
            vault,
            apply=True,
            rebuild=True,
            run_id=f"ambiguous-former-reuse-{suffix}",
        )
        with Session() as db:
            notes = {
                row.vault_relpath: (row.cao_key, row.status)
                for row in db.query(VaultNoteModel).all()
            }
            metadata = {
                row.file_path
                for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
            }
            exclusion = db.query(VaultExclusionModel).one()
            ambiguous_paths = {
                row.vault_relpath
                for row in db.query(VaultFindingModel).filter_by(code="rename_ambiguous").all()
            }
        assert notes["Mapped/A.md"][1] == "quarantined"
        assert notes["Mapped/B.md"][1] == "quarantined"
        assert notes["Mapped/Old.md"][1] == "indexed"
        assert notes["Mapped/Old.md"][0] != original_key
        assert metadata == {"Mapped/Old.md"}
        assert (
            exclusion.cao_key,
            exclusion.last_known_relpath,
            exclusion.content_sha256,
        ) == (original_key, "Mapped/Old.md", original_hash)
        assert ambiguous_paths == {"Mapped/A.md", "Mapped/B.md"}


def test_rebuild_preserves_path_derived_tombstone_after_rename(tmp_path, monkeypatch):
    """A rebuild migrates a forgotten rename-carried identity to the current path key."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("same content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rebuild-renamed-excluded-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        original_key = original.cao_key
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(new_path)
    reconcile(vault, apply=True, run_id="rebuild-renamed-excluded-incremental")
    with Session() as db:
        renamed = db.query(VaultNoteModel).one()
        assert (renamed.vault_relpath, renamed.cao_key, renamed.status) == (
            "Mapped/New.md",
            original_key,
            "excluded",
        )
        assert db.query(VaultNoteAliasModel).count() == 1

    reconcile(vault, apply=True, rebuild=True, run_id="rebuild-renamed-excluded-after")

    with Session() as db:
        rebuilt = db.query(VaultNoteModel).one()
        rebuilt_key = rebuilt.cao_key
        exclusions = db.query(VaultExclusionModel).all()
        metadata_count = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
        retained = (
            db.query(VaultFindingModel)
            .filter_by(code="deindexed_retained", vault_relpath="Mapped/New.md")
            .count()
        )
        alias_count = db.query(VaultNoteAliasModel).count()
    assert rebuilt_key != original_key
    assert (rebuilt.vault_relpath, rebuilt.status, metadata_count) == (
        "Mapped/New.md",
        "excluded",
        0,
    )
    assert [(row.cao_key, row.last_known_relpath) for row in exclusions] == [
        (rebuilt_key, "Mapped/New.md")
    ]
    assert retained == 1
    assert alias_count == 0

    reconcile(vault, apply=True, rebuild=True, run_id="rebuild-renamed-excluded-stable")
    with Session() as db:
        stable = db.query(VaultNoteModel).one()
        stable_metadata = db.query(MemoryMetadataModel).filter_by(source_kind="vault").count()
    assert (stable.cao_key, stable.status, stable_metadata) == (rebuilt_key, "excluded", 0)


def test_rebuild_tombstone_migration_does_not_exclude_former_path_replacement(
    tmp_path, monkeypatch
):
    """Content provenance keeps a replacement at the former path recallable."""
    from cli_agent_orchestrator.services.vault import reconcile as module

    Session = _session(tmp_path, monkeypatch, module)
    vault = _rename_vault(tmp_path)
    old_path = tmp_path / "vault" / "Mapped" / "Old.md"
    new_path = old_path.with_name("New.md")
    old_path.write_text("forgotten content", encoding="utf-8")
    reconcile(vault, apply=True, run_id="rebuild-replacement-before")
    with Session() as db:
        original = db.query(VaultNoteModel).one()
        _exclude_note(db, original)
        db.query(MemoryMetadataModel).filter_by(source_kind="vault").delete()
        db.commit()

    old_path.rename(new_path)
    reconcile(vault, apply=True, run_id="rebuild-replacement-renamed")
    old_path.write_text("different replacement", encoding="utf-8")
    reconcile(vault, apply=True, rebuild=True, run_id="rebuild-replacement-after")

    with Session() as db:
        notes = {
            row.vault_relpath: (row.cao_key, row.status) for row in db.query(VaultNoteModel).all()
        }
        metadata = {
            row.file_path
            for row in db.query(MemoryMetadataModel).filter_by(source_kind="vault").all()
        }
        exclusion = db.query(VaultExclusionModel).one()
    renamed_key, renamed_status = notes["Mapped/New.md"]
    replacement_key, replacement_status = notes["Mapped/Old.md"]
    assert renamed_status == "excluded"
    assert replacement_status == "indexed"
    assert renamed_key != replacement_key
    assert metadata == {"Mapped/Old.md"}
    assert (exclusion.cao_key, exclusion.last_known_relpath) == (
        renamed_key,
        "Mapped/New.md",
    )


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


def _exclude_note(db, note: VaultNoteModel) -> None:
    note.status = "excluded"
    db.add(
        VaultExclusionModel(
            vault_id=note.vault_id,
            scope=note.scope,
            scope_id=note.scope_id,
            cao_key=note.cao_key,
            last_known_relpath=note.vault_relpath,
            content_sha256=note.content_sha256,
        )
    )


def _session(tmp_path, monkeypatch, module):
    from cli_agent_orchestrator.services import memory_relationship_service

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(module, "SessionLocal", Session)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", Session)
    monkeypatch.setattr(module, "_emit_audit_events", lambda *_args: None)
    return Session
