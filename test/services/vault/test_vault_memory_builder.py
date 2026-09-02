"""Vault recall builder coverage."""

import asyncio
import errno
import os
import sys
from datetime import datetime, timezone
from test.fixtures.vault_factory import build_vault_fixture
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    VaultNoteModel,
    VaultRecallCounterModel,
)
from cli_agent_orchestrator.services.vault.binding import VaultBinding
from cli_agent_orchestrator.services.vault.config import FolderMapping
from cli_agent_orchestrator.services.vault.reader import (
    increment_counter,
    load_candidate,
    resolve_candidates,
)
from cli_agent_orchestrator.services.vault.reconcile import reconcile


@pytest.fixture(autouse=True)
def _legacy_reader_calls_are_ordinary_requesters(monkeypatch):
    """Keep U6 boundary tests focused on their declared containment contract."""
    from cli_agent_orchestrator.services.vault import reader

    monkeypatch.setattr(
        reader,
        "_resolve_injection_policy",
        lambda require_injectable, **_kwargs: reader.VaultInjectionPolicy(
            require_injectable, "test", False
        ),
    )


def test_ordinary_vault_note_builds_memory_without_native_timestamp_heading(tmp_path, monkeypatch):
    """An Obsidian body has no native ``## <ISO8601Z>`` section requirement."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)

    fixture = build_vault_fixture(tmp_path)
    reconcile(
        fixture.vault,
        apply=True,
        run_id="builder",
        run_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )

    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    memory = next(
        load_candidate(candidate, max_body_chars=4096, require_injectable=False)
        for candidate in candidates
        if candidate.metadata.key == "design"
    )

    assert memory is not None
    assert memory.id
    assert memory.content == "Design"
    assert memory.source_kind == "vault"
    assert memory.source_path == "Projects/CAO Design/Design.md"
    assert memory.index_freshness == "fresh"
    assert memory.content_truncated is False


def test_reader_rejects_non_injectable_mapping_when_required(tmp_path, monkeypatch):
    """The explicit injection policy is enforced again at the byte boundary."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="inject-gate")
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )
    candidate = resolve_candidates(
        binding,
        keys=["design"],
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )[0]

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=True) is None


def test_reader_refuses_stale_projection_when_mapping_index_is_disabled(tmp_path, monkeypatch):
    """A configured disabled mapping remains authoritative but supplies no candidates."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="index-disabled")
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    disabled_mapping = mapping.model_copy(update={"index": False})
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=disabled_mapping,
    )

    resolution = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    assert resolution.exit_arm == "not_indexable"
    assert list(resolution) == []


@pytest.mark.parametrize("consumer", ["explicit_recall", "injected_context"])
@pytest.mark.parametrize("require_injectable", [False, True])
@pytest.mark.parametrize("keys", [None, [], ["design"]])
def test_not_indexable_gate_precedes_every_database_read(
    tmp_path, monkeypatch, consumer, require_injectable, keys
):
    from cli_agent_orchestrator.services.vault import reader

    mapping = FolderMapping(
        folder="Mapped",
        scope="project",
        scope_id="fixture-project",
        index=False,
    )
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id="fixture",
        root=str(tmp_path),
        mapping=mapping,
    )

    def forbidden_session():
        raise AssertionError("index=false must return before SQL")

    monkeypatch.setattr(reader, "SessionLocal", forbidden_session)

    resolution = resolve_candidates(
        binding,
        keys=keys,
        scope="project",
        scope_id="fixture-project",
        require_injectable=require_injectable,
        terminal_id=None,
        consumer=consumer,
    )

    assert resolution.exit_arm == "not_indexable"
    assert list(resolution) == []


def test_metadata_recall_uses_vault_builder_not_native_wiki_parser(tmp_path, monkeypatch):
    """Vault recall returns ordinary Markdown that native parsing would drop."""
    from cli_agent_orchestrator.services import memory_service, settings_service
    from cli_agent_orchestrator.services.memory_service import MemoryService
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
    from cli_agent_orchestrator.services.vault.config import VaultConfig

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)

    fixture = build_vault_fixture(tmp_path)
    (fixture.root / "CAO" / "Guide.md").write_text(
        "---\ncao:\n  key: guide\n---\n# Guide\n\nordinary vault prose",
        encoding="utf-8",
    )
    config = VaultConfig(enabled=True, vaults=[fixture.vault])
    monkeypatch.setattr(settings_service, "get_vault_config", lambda: config)
    reconcile(
        fixture.vault,
        apply=True,
        run_id="metadata-builder",
        run_started_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )

    memories = asyncio.run(
        MemoryService(base_dir=tmp_path / "native")._metadata_recall(
            query="ordinary",
            scope="global",
            limit=10,
        )
    )

    assert [(memory.key, memory.source_kind, memory.content) for memory in memories] == [
        ("guide", "vault", "ordinary vault prose")
    ]


def test_recall_counter_uses_durable_vault_counter_table(tmp_path, monkeypatch):
    """Recall outcomes are process-independent counters, never module globals."""
    from cli_agent_orchestrator.services.vault import reader

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reader, "SessionLocal", Session)

    increment_counter("fixture", "injection_budget_exceeded.scopes_clipped", 2)
    increment_counter("fixture", "injection_budget_exceeded.memories_dropped", 7)

    with Session() as db:
        rows = {
            row.counter_name: row.value
            for row in db.query(VaultRecallCounterModel)
            .filter(VaultRecallCounterModel.vault_id == "fixture")
            .all()
        }
    assert rows == {
        "injection_budget_exceeded.memories_dropped": 7,
        "injection_budget_exceeded.scopes_clipped": 2,
    }


def test_vault_memory_body_budget_marks_content_stale_and_truncated(tmp_path, monkeypatch):
    """A post-index edit is bounded and freshness-stamped without reconciliation."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="body-budget")
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )
    candidate = next(
        item
        for item in resolve_candidates(
            binding,
            scope="project",
            scope_id="fixture-project",
            require_injectable=False,
            terminal_id=None,
            consumer="explicit_recall",
        )
        if item.metadata.key == "design"
    )
    (fixture.root / "Projects" / "CAO Design" / "Design.md").write_text("x" * 200, encoding="utf-8")

    memory = load_candidate(candidate, max_body_chars=64, require_injectable=False)

    assert memory is not None
    assert memory.index_freshness == "stale"
    assert memory.content_truncated is True
    assert memory.content.endswith("[Content truncated for recall]")
    assert len(memory.content) == 64
    assert memory.token_estimate == 16


def _indexed_reader_candidate(tmp_path, monkeypatch, *, run_id: str):
    """Build one real indexed row so read-boundary tests exercise recall."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id=run_id)
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )
    candidate = resolve_candidates(
        binding,
        keys=["design"],
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )[0]
    return reader, Session, fixture, candidate


@pytest.mark.parametrize(
    ("file_path", "prepare"),
    [
        ("../../etc/passwd", lambda _tmp_path, _fixture: None),
        ("/etc/passwd", lambda _tmp_path, _fixture: None),
        (
            "Notes/link/N.md",
            lambda tmp_path, fixture: (
                (tmp_path / "outside").mkdir(),
                (tmp_path / "outside" / "N.md").write_text("must not be read", encoding="utf-8"),
                (fixture.root / "Notes").mkdir(),
                os.symlink(tmp_path / "outside", fixture.root / "Notes" / "link"),
            ),
        ),
    ],
    ids=["parent", "absolute", "symlink-outside"],
)
def test_reader_refuses_and_counts_every_escaped_metadata_path(
    tmp_path, monkeypatch, caplog, file_path, prepare
):
    """Every poisoned path form is refused and visible in recall counters."""
    _reader, Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id=f"reader-escape-{file_path!r}"
    )
    prepare(tmp_path, fixture)
    candidate.metadata.file_path = file_path

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None
    with Session() as db:
        counter = (
            db.query(VaultRecallCounterModel)
            .filter_by(vault_id="fixture", counter_name="path_escapes_root")
            .one()
        )
    assert counter.value == 1
    assert any("arm=lexical" in record.getMessage() for record in caplog.records)


def test_reader_counts_an_intermediate_symlink_swap_after_resolution(tmp_path, monkeypatch):
    """A platform-specific intermediate-link failure is reported as an escape."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-symlink-race"
    )
    note = fixture.root / candidate.metadata.file_path
    link = note.parent / "swap"
    inside = link / "N.md"
    inside.parent.mkdir()
    inside.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "N.md").write_text("must not be read", encoding="utf-8")
    candidate.metadata.file_path = str(inside.relative_to(fixture.root))
    realpath = reader.os.path.realpath

    def resolve_then_swap(path):
        resolved = realpath(path)
        if path.endswith(candidate.metadata.file_path):
            link.rename(tmp_path / "original-link")
            os.symlink(outside, link)
        return resolved

    monkeypatch.setattr(reader.os.path, "realpath", resolve_then_swap)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None
    with Session() as db:
        counter = (
            db.query(VaultRecallCounterModel)
            .filter_by(vault_id="fixture", counter_name="path_escapes_root")
            .one()
        )
    assert counter.value == 1


def test_reader_counts_a_final_symlink_swap_after_resolution(tmp_path, monkeypatch, caplog):
    """The final-file ELOOP conversion makes this race visible to status."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-final-symlink-race"
    )
    target = fixture.root / "Projects" / "CAO Design" / "Swapped.md"
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("must not be read", encoding="utf-8")
    candidate.metadata.file_path = str(target.relative_to(fixture.root))
    segments = candidate.metadata.file_path.split(os.sep)
    assert not os.path.isabs(candidate.metadata.file_path)
    assert all(segment not in {"", ".", ".."} for segment in segments)
    realpath = reader.os.path.realpath

    def resolve_then_swap(path):
        resolved = realpath(path)
        if path.endswith(candidate.metadata.file_path):
            target.unlink()
            os.symlink(outside, target)
        return resolved

    monkeypatch.setattr(reader.os.path, "realpath", resolve_then_swap)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None
    with Session() as db:
        counter = (
            db.query(VaultRecallCounterModel)
            .filter_by(vault_id="fixture", counter_name="path_escapes_root")
            .one()
        )
    assert counter.value == 1
    assert any("arm=eloop" in record.getMessage() for record in caplog.records)


def test_reader_counts_a_root_symlink_swap_after_resolution(tmp_path, monkeypatch, caplog):
    """A root swap is counted when macOS reports it as ENOTDIR."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-root-symlink-race"
    )
    segments = candidate.metadata.file_path.split(os.sep)
    assert not os.path.isabs(candidate.metadata.file_path)
    assert all(segment not in {"", ".", ".."} for segment in segments)
    outside = tmp_path / "outside-root"
    outside.mkdir()
    realpath = reader.os.path.realpath
    root_swapped = False

    def resolve_then_swap_root(path):
        nonlocal root_swapped
        resolved = realpath(path)
        if path.endswith(candidate.metadata.file_path):
            fixture.root.rename(tmp_path / "vault-before-root-swap")
            os.symlink(outside, fixture.root)
            root_swapped = True
        return resolved

    monkeypatch.setattr(reader.os.path, "realpath", resolve_then_swap_root)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None
    assert root_swapped is True
    with Session() as db:
        counter = (
            db.query(VaultRecallCounterModel)
            .filter_by(vault_id="fixture", counter_name="path_escapes_root")
            .one()
        )
    assert counter.value == 1
    assert any("arm=eloop" in record.getMessage() for record in caplog.records)


def test_reader_counts_a_root_eloop_from_the_initial_open(tmp_path, monkeypatch, caplog):
    """The root-open ELOOP handler is independently exercised."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-root-eloop"
    )
    segments = candidate.metadata.file_path.split(os.sep)
    assert not os.path.isabs(candidate.metadata.file_path)
    assert all(segment not in {"", ".", ".."} for segment in segments)
    original_open = reader.os.open
    root_opened = False

    def root_eloop(path, flags, *, dir_fd=None):
        nonlocal root_opened
        if path == str(fixture.root) and dir_fd is None:
            root_opened = True
            raise OSError(errno.ELOOP, "symlink loop", path)
        return original_open(path, flags, dir_fd=dir_fd)

    monkeypatch.setattr(reader.os, "open", root_eloop)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None
    assert root_opened is True
    with Session() as db:
        counter = (
            db.query(VaultRecallCounterModel)
            .filter_by(vault_id="fixture", counter_name="path_escapes_root")
            .one()
        )
    assert counter.value == 1
    assert any("arm=eloop" in record.getMessage() for record in caplog.records)


def test_reader_refuses_a_post_resolution_intermediate_symlink(tmp_path):
    """The held-fd walk rejects an intermediate component swapped to a symlink."""
    from cli_agent_orchestrator.services.vault.reader import _open_confined_fd

    root = tmp_path / "vault"
    target = root / "Notes" / "nested" / "N.md"
    target.parent.mkdir(parents=True)
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "N.md").write_text("outside", encoding="utf-8")
    real_path = os.path.realpath(target)
    (root / "Notes" / "nested").rename(tmp_path / "nested-before-swap")
    os.symlink(outside, root / "Notes" / "nested")

    with pytest.raises(ValueError, match="symlink escapes vault root"):
        _open_confined_fd(os.path.realpath(root), real_path)


def test_reader_refuses_a_post_resolution_final_symlink(tmp_path):
    """The held-fd walk rejects a final file swap; successful reads cover its integration."""
    from cli_agent_orchestrator.services.vault.reader import _open_confined_fd

    root = tmp_path / "vault"
    target = root / "Notes" / "N.md"
    target.parent.mkdir(parents=True)
    target.write_text("inside", encoding="utf-8")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    real_path = os.path.realpath(target)
    target.unlink()
    os.symlink(outside, target)

    with pytest.raises(ValueError, match="symlink escapes vault root"):
        _open_confined_fd(os.path.realpath(root), real_path)


def test_reader_refuses_a_discovery_inode_mismatch(tmp_path, monkeypatch):
    """The candidate read must remain tied to the inode checked before open."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, _Session, _fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-inode-mismatch"
    )
    original_open = reader._open_confined_fd
    other = tmp_path / "other.md"
    other.write_text("other", encoding="utf-8")

    def mismatched_open(root, real_path):
        fd, _expected = original_open(root, real_path)
        return fd, os.stat(other)

    monkeypatch.setattr(reader, "_open_confined_fd", mismatched_open)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None


def test_reader_refuses_a_changed_read_window(tmp_path, monkeypatch):
    """Bytes are not served when the opened file changes during the read."""
    from cli_agent_orchestrator.services.vault import reader

    _reader, _Session, fixture, candidate = _indexed_reader_candidate(
        tmp_path, monkeypatch, run_id="reader-window-change"
    )
    target = fixture.root / candidate.metadata.file_path
    original_fstat = reader.os.fstat
    fstat_calls = 0

    def mutate_before_after_stat(fd):
        nonlocal fstat_calls
        fstat_calls += 1
        if fstat_calls == 2:
            target.write_text("changed after read", encoding="utf-8")
        return original_fstat(fd)

    monkeypatch.setattr(reader.os, "fstat", mutate_before_after_stat)

    assert load_candidate(candidate, max_body_chars=4096, require_injectable=False) is None


def test_quarantined_note_is_not_a_recall_candidate_even_with_live_metadata(tmp_path, monkeypatch):
    """Recall joins vault_note status rather than trusting metadata alone."""
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="quarantined-join")
    with Session() as db:
        db.query(VaultNoteModel).filter(VaultNoteModel.cao_key == "design").update(
            {"status": "quarantined"}
        )
        db.commit()
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )

    assert (
        resolve_candidates(
            binding,
            keys=["design"],
            scope="project",
            scope_id="fixture-project",
            require_injectable=False,
            terminal_id=None,
            consumer="explicit_recall",
        )
        == []
    )


def test_recall_truncation_does_not_introduce_a_third_content_digest() -> None:
    """Recall has no served-body digest; scanner and writer retain one identity."""
    from cli_agent_orchestrator.services.vault import scan, writer

    raw = "\ufeffline one\r\nline two\r\n"
    assert scan._sha256(scan._normalize_line_endings(raw)) == writer._sha256(raw)


def test_resolve_candidates_never_joins_a_native_metadata_row(tmp_path, monkeypatch, caplog):
    """The vault source discriminator prevents same-key native disclosure."""
    from cli_agent_orchestrator.services.vault import reader

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    root = tmp_path / "vault"
    root.mkdir()
    mapping = FolderMapping(folder="Mapped", scope="global", inject=True)
    binding = VaultBinding(
        scope="global",
        scope_id=None,
        vault_id="fixture",
        root=str(root),
        mapping=mapping,
    )
    with Session() as db:
        db.add(
            MemoryMetadataModel(
                id="metadata-id",
                key="shared",
                memory_type="reference",
                scope="global",
                scope_id=None,
                source_kind="vault",
                file_path="native.md",
                tags="",
            )
        )
        db.add(
            VaultNoteModel(
                note_uid="vault-note",
                vault_id="fixture",
                scope="global",
                scope_id="",
                cao_key="shared",
                vault_relpath="Mapped/Shared.md",
                managed=False,
                status="indexed",
            )
        )
        db.commit()

    positive_control = resolve_candidates(
        binding,
        scope="global",
        scope_id=None,
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    with Session() as db:
        db.get(MemoryMetadataModel, "metadata-id").source_kind = "native"
        db.commit()

    assert (
        resolve_candidates(
            binding,
            scope="global",
            scope_id=None,
            require_injectable=False,
            terminal_id=None,
            consumer="explicit_recall",
        )
        == []
    )
    assert [candidate.metadata.key for candidate in positive_control] == ["shared"]
    assert any("arm=no_rows" in record.getMessage() for record in caplog.records)


def test_bm25_reads_a_vault_only_scope_without_native_candidates(tmp_path, monkeypatch):
    """The vault corpus is a real corpus, not gated by native discovery."""
    from cli_agent_orchestrator.services.memory_service import MemoryService
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="bm25-vault")
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )
    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )

    memories = MemoryService(base_dir=tmp_path / "native")._bm25_search(
        query="design",
        scope="project",
        scope_id="fixture-project",
        memory_type=None,
        limit=10,
        exclude_keys=set(),
        terminal_context=None,
        scan_all=False,
        vault_candidates=candidates,
    )

    assert [(memory.key, memory.source_kind) for memory in memories] == [("design", "vault")]


def test_bm25_vault_corpus_uses_all_indexed_candidates_with_one_read_each(tmp_path, monkeypatch):
    """Eligibility filters cannot shrink the vault BM25 document population."""
    from cli_agent_orchestrator.services import memory_service
    from cli_agent_orchestrator.services.memory_service import MemoryService
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

    engine = create_engine(f"sqlite:///{tmp_path / 'state.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", Session)
    monkeypatch.setattr(reader, "SessionLocal", Session)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    fixture = build_vault_fixture(tmp_path)
    reconcile(fixture.vault, apply=True, run_id="bm25-corpus")
    mapping = next(item for item in fixture.vault.mappings if item.scope == "project")
    binding = VaultBinding(
        scope="project",
        scope_id="fixture-project",
        vault_id=fixture.vault.id,
        root=fixture.vault.root,
        mapping=mapping,
    )
    candidates = resolve_candidates(
        binding,
        scope="project",
        scope_id="fixture-project",
        require_injectable=False,
        terminal_id=None,
        consumer="explicit_recall",
    )
    reads: list[str] = []
    original_load = memory_service.load_candidate

    def count_load(candidate, *, max_body_chars, require_injectable):
        reads.append(candidate.metadata.key)
        return original_load(
            candidate,
            max_body_chars=max_body_chars,
            require_injectable=require_injectable,
        )

    corpus_sizes: list[int] = []

    class RecordingBm25:
        def __init__(self, corpus):
            corpus_sizes.append(len(corpus))

        def get_scores(self, _query):
            return [1.0] * corpus_sizes[-1]

    monkeypatch.setattr(memory_service, "load_candidate", count_load)
    monkeypatch.setitem(sys.modules, "rank_bm25", SimpleNamespace(BM25Okapi=RecordingBm25))

    MemoryService(base_dir=tmp_path / "native")._bm25_search(
        query="design",
        scope="project",
        scope_id="fixture-project",
        memory_type=None,
        limit=10,
        exclude_keys={"design"},
        terminal_context=None,
        scan_all=False,
        vault_candidates=candidates,
    )

    assert corpus_sizes == [len(candidates)]
    assert sorted(reads) == sorted(candidate.metadata.key for candidate in candidates)
