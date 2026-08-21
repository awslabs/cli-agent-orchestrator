"""Focused tests for filesystem-first memory metadata reconciliation."""

from __future__ import annotations

import asyncio
import fcntl
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Barrier
from typing import Any

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services.memory_reconciliation import (
    DEFAULTED_METADATA_FIELDS,
    MemoryIdentity,
    MemoryReconciliationError,
    MemoryReconciliationService,
    RepairAction,
    RepairRecord,
    RepairReport,
)
from cli_agent_orchestrator.services.memory_service import MemoryService


@pytest.fixture
def engine(tmp_path: Path) -> Any:
    db_engine = create_engine(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=db_engine)
    return db_engine


def _topic_path(base: Path, scope: str, scope_id: str | None, key: str) -> Path:
    if scope == "project":
        assert scope_id is not None
        return base / scope_id / "wiki" / scope / f"{key}.md"
    if scope in {"session", "agent"}:
        assert scope_id is not None
        return base / "global" / "wiki" / scope / scope_id / f"{key}.md"
    return base / "global" / "wiki" / scope / f"{key}.md"


def _write_topic(
    base: Path,
    scope: str,
    scope_id: str | None,
    key: str,
    *,
    memory_type: str = "reference",
    tags: str = "one, two",
    body: str = "durable body",
) -> Path:
    path = _topic_path(base, scope, scope_id, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f"# {key}\n"
        f"<!-- id: {uuid.uuid4()} | scope: {scope} | "
        f"type: {memory_type} | tags: {tags} -->\n\n"
        "## 2026-07-15T01:00:00Z\n"
        f"{body}\n\n"
        "## 2026-07-16T02:00:00Z\n"
        "latest entry\n",
        encoding="utf-8",
    )
    return path


def _rows(engine: Any) -> list[MemoryMetadataModel]:
    with sessionmaker(bind=engine)() as db:
        return db.query(MemoryMetadataModel).order_by(MemoryMetadataModel.key).all()


@pytest.mark.parametrize(
    ("scope", "scope_id"),
    [
        ("global", None),
        ("project", "project-one"),
        ("session", "session-one"),
        ("agent", "code_supervisor"),
    ],
)
def test_discovers_and_repairs_every_canonical_scope(
    tmp_path: Path, engine: Any, scope: str, scope_id: str | None
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, scope, scope_id, f"{scope}-topic")
    original = topic.read_bytes()
    service = MemoryReconciliationService(base, engine)

    first_plan = service.plan()
    second_plan = service.plan()

    assert first_plan == second_plan
    assert first_plan.records[0].identity == MemoryIdentity(f"{scope}-topic", scope, scope_id)
    assert RepairAction.CREATE_METADATA in first_plan.records[0].actions
    assert RepairAction.REBUILD_INDEX in first_plan.records[0].actions
    assert topic.read_bytes() == original
    assert _rows(engine) == []

    applied = service.apply()
    assert applied.counts["repaired"] == 1
    assert applied.records[0].defaulted_fields == DEFAULTED_METADATA_FIELDS
    assert topic.read_bytes() == original

    row = _rows(engine)[0]
    assert (row.key, row.scope, row.scope_id) == (f"{scope}-topic", scope, scope_id)
    assert row.id != first_plan.records[0].identity.key
    assert row.tags == "one,two"
    assert row.source_provider is None
    assert row.source_terminal_id is None
    assert row.access_count == 0
    assert row.last_accessed_at is None
    assert row.last_compiled_at is None
    assert row.related_keys is None
    assert row.created_at == datetime(2026, 7, 15, 1, 0)
    assert row.updated_at == datetime(2026, 7, 16, 2, 0)
    assert row.token_estimate == len(topic.read_text(encoding="utf-8")) // 4

    second_apply = service.apply()
    assert second_apply.counts["unchanged"] == 1
    assert len(_rows(engine)) == 1


def test_updates_reconstructable_fields_and_preserves_private_metadata(
    tmp_path: Path, engine: Any
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "global", None, "preserve", tags="fixed")
    row_id = str(uuid.uuid4())
    last_accessed = datetime(2026, 6, 1, tzinfo=timezone.utc)
    last_compiled = datetime(2026, 6, 2, tzinfo=timezone.utc)
    with sessionmaker(bind=engine)() as db:
        db.add(
            MemoryMetadataModel(
                id=row_id,
                key="preserve",
                memory_type="user",
                scope="global",
                scope_id=None,
                file_path=str(topic),
                tags="stale",
                source_provider="codex",
                source_terminal_id="terminal-one",
                token_estimate=1,
                created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2020, 1, 2, tzinfo=timezone.utc),
                access_count=7,
                last_accessed_at=last_accessed,
                last_compiled_at=last_compiled,
                related_keys="other",
            )
        )
        db.commit()

    service = MemoryReconciliationService(base, engine)
    assert RepairAction.UPDATE_METADATA in service.plan().records[0].actions
    service.apply()

    row = _rows(engine)[0]
    assert row.id == row_id
    assert row.memory_type == "reference"
    assert row.tags == "fixed"
    assert row.access_count == 7
    assert row.source_provider == "codex"
    assert row.source_terminal_id == "terminal-one"
    assert row.last_accessed_at == last_accessed.replace(tzinfo=None)
    assert row.last_compiled_at == last_compiled.replace(tzinfo=None)
    assert row.related_keys == "other"


def test_nullable_timestamps_are_repaired_without_blocking_other_topics(
    tmp_path: Path, engine: Any
) -> None:
    base = tmp_path / "memory"
    first = _write_topic(base, "global", None, "nullable")
    _write_topic(base, "global", None, "independent")
    with sessionmaker(bind=engine)() as db:
        db.add(
            MemoryMetadataModel(
                id=str(uuid.uuid4()),
                key="nullable",
                memory_type="reference",
                scope="global",
                scope_id=None,
                file_path=str(first),
                tags="one,two",
            )
        )
        db.commit()
        db.execute(
            MemoryMetadataModel.__table__.update()
            .where(MemoryMetadataModel.key == "nullable")
            .values(created_at=None, updated_at=None)
        )
        db.commit()

    report = MemoryReconciliationService(base, engine).apply()

    assert report.counts["repaired"] == 2
    rows = {row.key: row for row in _rows(engine)}
    assert rows["nullable"].created_at == datetime(2026, 7, 15, 1, 0)
    assert rows["nullable"].updated_at == datetime(2026, 7, 16, 2, 0)
    assert "independent" in rows


def test_store_generated_tags_with_spaces_and_angle_are_repair_idempotent(
    tmp_path: Path, engine: Any
) -> None:
    base = tmp_path / "memory"
    store = MemoryService(base, engine)
    asyncio.run(
        store.store(
            content="durable body",
            scope="global",
            memory_type="reference",
            key="tag-grammar",
            tags="foo bar, angle>tag, pipe|tag",
        )
    )
    asyncio.run(
        store.store(
            content="second entry",
            scope="global",
            memory_type="reference",
            key="tag-grammar",
            tags="foo bar, angle>tag, pipe|tag",
        )
    )
    service = MemoryReconciliationService(base, engine)

    service.apply()
    second = service.apply()

    assert second.counts["unchanged"] == 1
    expected_tags = "foo bar,angle>tag,pipe|tag"
    assert _rows(engine)[0].tags == expected_tags
    parsed = store._parse_index(base / "global" / "wiki" / "index.md")
    assert parsed[0]["tags"] == expected_tags
    topic = _topic_path(base, "global", None, "tag-grammar")
    recalled = store._parse_wiki_file(topic, topic.read_text(encoding="utf-8"), parsed[0])
    assert recalled is not None
    assert recalled.tags == expected_tags


def test_rebuilds_only_matching_index_entry_and_collapses_duplicates(
    tmp_path: Path, engine: Any
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "agent", "developer", "target", tags="new")
    index = base / "global" / "wiki" / "index.md"
    index.write_text(
        "# CAO Memory Index\n<!-- Updated: old -->\n\n"
        "## agent\n"
        "- [target](agent/developer/target.md) — type:user tags:old ~1tok updated:old\n"
        "- [target](agent/developer/target.md) — type:user tags:old ~1tok updated:old\n"
        "- [orphan](agent/reviewer/orphan.md) — type:user tags:keep ~1tok updated:old\n",
        encoding="utf-8",
    )

    MemoryReconciliationService(base, engine).apply()

    content = index.read_text(encoding="utf-8")
    assert content.count("[target]") == 1
    assert "[orphan](agent/reviewer/orphan.md)" in content
    assert "type:reference tags:new" in content
    assert topic.exists()


def test_shared_index_is_read_and_written_constant_times(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "memory"
    for number in range(12):
        _write_topic(base, "global", None, f"topic-{number}")
    index = base / "global" / "wiki" / "index.md"
    index.write_text("# CAO Memory Index\n<!-- Updated: old -->\n", encoding="utf-8")
    reads = 0
    writes = 0
    original_read = Path.read_text
    original_write = Path.write_text

    def count_read(path: Path, *args: Any, **kwargs: Any) -> str:
        nonlocal reads
        if path == index:
            reads += 1
        return original_read(path, *args, **kwargs)

    def count_write(path: Path, *args: Any, **kwargs: Any) -> int:
        nonlocal writes
        if path.name == ".index.md.tmp" and path.parent == index.parent:
            writes += 1
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", count_read)
    monkeypatch.setattr(Path, "write_text", count_write)

    MemoryReconciliationService(base, engine).apply()

    assert reads == 3
    assert writes == 1
    assert index.read_text(encoding="utf-8").count("](global/topic-") == 12


def test_apply_metadata_revalidation_uses_bounded_table_loads(tmp_path: Path, engine: Any) -> None:
    base = tmp_path / "memory"
    for number in range(12):
        project = f"project-{number}"
        _write_topic(base, "project", project, f"topic-{number}")
    service = MemoryReconciliationService(base, engine)
    service.apply()
    metadata_selects: list[str] = []

    def count_metadata_selects(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lstrip().upper().startswith("SELECT") and "memory_metadata" in statement:
            metadata_selects.append(statement)

    event.listen(engine, "before_cursor_execute", count_metadata_selects)
    try:
        report = service.apply()
    finally:
        event.remove(engine, "before_cursor_execute", count_metadata_selects)

    assert report.counts["unchanged"] == 12
    assert len(metadata_selects) == 2


def test_directory_enumeration_failure_does_not_block_independent_scope(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "memory"
    _write_topic(base, "global", None, "good")
    blocked = base / "global" / "wiki" / "agent"
    blocked.mkdir(parents=True)
    original_iterdir = Path.iterdir

    def fail_one_directory(path: Path):
        if path == blocked:
            raise PermissionError("injected")
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", fail_one_directory)

    report = MemoryReconciliationService(base, engine).apply()

    assert report.counts["repaired"] == 1
    assert report.counts["skipped"] == 1
    assert any(
        record.finding is not None and record.finding.kind == "directory_enumeration_failed"
        for record in report.records
    )
    assert [row.key for row in _rows(engine)] == ["good"]


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda text: text.replace("<!-- id:", "<!-- broken:"), RepairAction.MALFORMED),
        (lambda text: text.replace("scope: global", "scope: agent"), RepairAction.CONFLICT),
        (lambda text: text.replace("## 2026-", "## never-", 2), RepairAction.MALFORMED),
    ],
)
def test_malformed_and_conflicting_topics_are_reported_without_mutation(
    tmp_path: Path, engine: Any, mutate: Any, expected: RepairAction
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "global", None, "broken")
    topic.write_text(mutate(topic.read_text(encoding="utf-8")), encoding="utf-8")
    before = topic.read_bytes()

    report = MemoryReconciliationService(base, engine).apply()

    assert report.records[0].actions == (expected,)
    assert report.records[0].status == "skipped"
    assert topic.read_bytes() == before
    assert _rows(engine) == []


def test_escaping_symlink_is_reported_and_not_read(tmp_path: Path, engine: Any) -> None:
    base = tmp_path / "memory"
    outside = tmp_path / "outside.md"
    outside.write_text("private outside data", encoding="utf-8")
    link = _topic_path(base, "global", None, "escaped")
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)

    report = MemoryReconciliationService(base, engine).plan()

    assert report.records[0].actions == (RepairAction.UNSAFE_PATH,)
    assert "private outside data" not in report.records[0].finding.message
    assert _rows(engine) == []


def test_in_base_project_parent_symlink_is_unsafe_and_apply_does_not_relock(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "project", "project-a", "shared")
    original = topic.read_bytes()
    (base / "project-b").symlink_to(base / "project-a", target_is_directory=True)
    service = MemoryReconciliationService(base, engine)

    plan = service.plan()

    repairable = [record for record in plan.records if record.status != "skipped"]
    assert [record.identity for record in repairable] == [
        MemoryIdentity("shared", "project", "project-a")
    ]
    unsafe = [record for record in plan.records if record.actions == (RepairAction.UNSAFE_PATH,)]
    assert len(unsafe) == 1
    assert unsafe[0].finding is not None
    assert unsafe[0].finding.kind == "symlink_component"

    held_locks: set[Path] = set()

    def track_flock(lock_fd: Any, operation: int) -> None:
        lock_path = Path(lock_fd.name).resolve()
        if operation == fcntl.LOCK_EX:
            assert lock_path not in held_locks
            held_locks.add(lock_path)
        elif operation == fcntl.LOCK_UN:
            held_locks.remove(lock_path)

    monkeypatch.setattr(fcntl, "flock", track_flock)

    report = service.apply()

    assert report.counts["repaired"] == 1
    assert report.counts["skipped"] == 1
    assert held_locks == set()
    assert topic.read_bytes() == original
    assert [row.key for row in _rows(engine)] == ["shared"]


def test_apply_rejects_duplicate_resolved_paths_before_locking(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "project", "project-a", "shared")
    service = MemoryReconciliationService(base, engine)
    records = tuple(
        RepairRecord(
            identity=MemoryIdentity("shared", "project", project),
            file_path=str(topic),
            actions=(RepairAction.CREATE_METADATA,),
            status="planned",
        )
        for project in ("project-a", "project-b")
    )
    monkeypatch.setattr(service, "plan", lambda: RepairReport(records=records, applied=False))

    def fail_flock(_lock_fd: Any, _operation: int) -> None:
        raise AssertionError("duplicate paths must be rejected before lock acquisition")

    monkeypatch.setattr(fcntl, "flock", fail_flock)

    report = service.apply()

    assert report.counts["skipped"] == 2
    assert all(record.actions == (RepairAction.CONFLICT,) for record in report.records)
    assert all(
        record.finding is not None and record.finding.kind == "duplicate_resolved_topic_path"
        for record in report.records
    )
    assert _rows(engine) == []


def test_duplicate_and_wrong_path_rows_are_conflicts(tmp_path: Path, engine: Any) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "global", None, "conflicted")
    with sessionmaker(bind=engine)() as db:
        for suffix in ("one", "two"):
            db.add(
                MemoryMetadataModel(
                    id=str(uuid.uuid4()),
                    key="conflicted",
                    memory_type="reference",
                    scope="global",
                    scope_id=None,
                    file_path=str(topic.with_name(f"{suffix}.md")),
                    tags="",
                )
            )
        db.commit()

    report = MemoryReconciliationService(base, engine).apply()

    assert report.records[0].actions == (RepairAction.CONFLICT,)
    assert report.records[0].finding.kind == "duplicate_database_identity"
    assert len(_rows(engine)) == 2


def test_unexpected_failure_does_not_rollback_other_records(
    tmp_path: Path, engine: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    base = tmp_path / "memory"
    _write_topic(base, "global", None, "a-good")
    _write_topic(base, "global", None, "z-fails")
    service = MemoryReconciliationService(base, engine)
    original = service._repair_metadata

    def fail_last(topic: Any, action: RepairAction) -> None:
        if topic.identity.key == "z-fails":
            raise RuntimeError("injected")
        original(topic, action)

    monkeypatch.setattr(service, "_repair_metadata", fail_last)

    with pytest.raises(MemoryReconciliationError) as caught:
        service.apply()

    assert caught.value.report.counts["repaired"] == 1
    assert caught.value.report.counts["failed"] == 1
    assert [row.key for row in _rows(engine)] == ["a-good"]


def test_concurrent_store_and_repair_converge_without_duplicate_identity(
    tmp_path: Path, engine: Any
) -> None:
    base = tmp_path / "memory"
    topic = _write_topic(base, "global", None, "concurrent", body="original")
    repair = MemoryReconciliationService(base, engine)
    store = MemoryService(base, engine)
    barrier = Barrier(2)

    def run_repair() -> None:
        barrier.wait()
        repair.apply()

    def run_store() -> None:
        barrier.wait()
        asyncio.run(
            store.store(
                content="concurrent append",
                scope="global",
                memory_type="reference",
                key="concurrent",
            )
        )

    import asyncio

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(run_repair), pool.submit(run_store)]
        for future in futures:
            future.result(timeout=10)

    assert len(_rows(engine)) == 1
    assert "original" in topic.read_text(encoding="utf-8")
    assert "concurrent append" in topic.read_text(encoding="utf-8")
    index = base / "global" / "wiki" / "index.md"
    assert index.read_text(encoding="utf-8").count("[concurrent]") == 1
