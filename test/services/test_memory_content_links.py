"""Content-authored links become producer-isolated relationship edges."""

from __future__ import annotations

import asyncio
from pathlib import Path

from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import MemoryRelationshipModel
from cli_agent_orchestrator.services import memory_relationship_service as relationship_module
from cli_agent_orchestrator.services.memory_reconciliation import MemoryReconciliationService
from cli_agent_orchestrator.services.memory_relationship_service import (
    MemoryRelationshipService,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_lint import run_lint


def _run(coro):
    return asyncio.run(coro)


def _edges(engine) -> set[tuple[str, str, str]]:
    with sessionmaker(bind=engine)() as db:
        return {
            (row.source_key, row.target_key, row.origin)
            for row in db.query(MemoryRelationshipModel).all()
        }


def test_store_parses_wikilinks_and_see_also(tmp_path: Path, isolated_memory_db) -> None:
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("other", scope="global", key="other"))

    _run(
        service.store(
            "See [[target]].\n\n## See Also\n- [Other](../global/other.md)",
            scope="global",
            key="source",
        )
    )

    assert _edges(isolated_memory_db) == {
        ("source", "other", "content_link"),
        ("source", "target", "content_link"),
    }


def test_missing_and_cross_scope_links_are_not_edges_and_lint_reports_missing(
    tmp_path: Path, isolated_memory_db
) -> None:
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("private", scope="global", key="global-only"))
    ctx = {"cwd": str(tmp_path / "project")}
    _run(
        service.store(
            "Broken [[missing-note]] and cross-scope [[global-only]].",
            scope="project",
            key="source",
            terminal_context=ctx,
        )
    )

    assert _edges(isolated_memory_db) == set()
    issues = _run(
        run_lint(
            "fixture",
            scope="project",
            max_pairs=0,
            base_dir=tmp_path,
            db_engine=isolated_memory_db,
        )
    )
    descriptions = [issue.description for issue in issues]
    assert any(
        "content link references missing key: missing-note" in text for text in descriptions
    ), descriptions
    assert any(
        "content link crosses scope: global-only" in text for text in descriptions
    ), descriptions


def test_repair_replaces_deleted_content_link_but_preserves_human_edge(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    for key in ("kept-by-human", "deleted-from-text"):
        _run(service.store(key, scope="global", key=key))
    source = _run(
        service.store(
            "[[deleted-from-text]]",
            scope="global",
            key="source",
        )
    )
    factory = sessionmaker(bind=isolated_memory_db)
    monkeypatch.setattr(relationship_module, "SessionLocal", factory)
    MemoryRelationshipService().create(
        "global",
        None,
        "source",
        "kept-by-human",
        "relates_to",
        "human",
    )
    source_path = Path(source.file_path)
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace("[[deleted-from-text]]", "plain text"),
        encoding="utf-8",
    )

    MemoryReconciliationService(tmp_path, isolated_memory_db).apply()

    assert _edges(isolated_memory_db) == {
        ("source", "kept-by-human", "human"),
    }


def test_fresh_repair_builds_links_after_all_endpoint_metadata(
    tmp_path: Path, isolated_memory_db
) -> None:
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("[[target]]", scope="global", key="source"))
    with sessionmaker(bind=isolated_memory_db)() as db:
        from cli_agent_orchestrator.clients.database import MemoryMetadataModel

        db.query(MemoryRelationshipModel).delete()
        db.query(MemoryMetadataModel).delete()
        db.commit()

    MemoryReconciliationService(tmp_path, isolated_memory_db).apply()

    assert _edges(isolated_memory_db) == {("source", "target", "content_link")}


def test_content_links_false_preserves_recall_and_writes_no_edges(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: False)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    before = [
        memory.model_dump(mode="json")
        for memory in _run(service.recall(scope="global", record_access=False))
    ]
    _run(service.store("See [[target]].", scope="global", key="source"))
    after = [
        memory.model_dump(mode="json")
        for memory in _run(service.recall(scope="global", record_access=False))
    ]

    assert _edges(isolated_memory_db) == set()
    assert [item for item in after if item["key"] == "target"] == before
