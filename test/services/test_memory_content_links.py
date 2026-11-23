"""Content-authored links become producer-isolated relationship edges."""

from __future__ import annotations

import asyncio
from pathlib import Path

from click.testing import CliRunner
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.cli.commands.memory import repair_cmd
from cli_agent_orchestrator.clients.database import MemoryRelationshipModel
from cli_agent_orchestrator.services import memory_relationship_service as relationship_module
from cli_agent_orchestrator.services.memory_content_links import extract_content_link_targets
from cli_agent_orchestrator.services.memory_reconciliation import MemoryReconciliationService
from cli_agent_orchestrator.services.memory_relationship_service import (
    MAX_EDGES_PER_MUTATION,
    MemoryRelationshipService,
)
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_compiler import CompileResult, RelatedResult
from cli_agent_orchestrator.services.wiki_lint import run_lint


def _run(coro):
    return asyncio.run(coro)


def _edges(engine) -> set[tuple[str, str, str]]:
    with sessionmaker(bind=engine)() as db:
        return {
            (row.source_key, row.target_key, row.origin)
            for row in db.query(MemoryRelationshipModel).all()
        }


def _enable_content_links(monkeypatch) -> None:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)


def test_fresh_store_does_not_create_content_link_edges(tmp_path: Path, isolated_memory_db) -> None:
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("See [[target]].", scope="global", key="source"))

    assert _edges(isolated_memory_db) == set()


def test_store_creates_content_link_edges_when_enabled(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
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


def test_see_also_parser_ignores_non_memory_reference_shapes() -> None:
    content = """\
## See Also
- [Design doc](https://example.test/docs/design.md)
- https://example.test/guides/setup/
- [Upstream README](mailto:docs@example.test)
- See the testing guide in the wiki
**bold**
```markdown
## See Also
- target
```
## Next
- later
"""

    assert extract_content_link_targets(content, MemoryService._sanitize_key) == []


def test_see_also_parser_rejects_each_non_memory_rule() -> None:
    cases = (
        "- [External](https://example.test/key.md)",
        "- [Mail](mailto:docs@example.test)",
        "- [Absolute](/docs/key.md)",
        "-not-a-bullet",
        "```markdown\n## See Also\n- target\n```",
        "- prose words are not a key",
        "- [[../target]]",
    )

    for bullet in cases:
        content = f"## See Also\n{bullet}\n"
        assert extract_content_link_targets(content, MemoryService._sanitize_key) == []


def test_missing_and_cross_scope_links_are_not_edges_and_lint_reports_missing(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
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
    assert (
        descriptions.count("content link references missing key: missing-note") == 1
    ), descriptions
    assert any(
        "content link crosses scope: global-only" in text for text in descriptions
    ), descriptions


def test_repair_replaces_deleted_content_link_but_preserves_human_edge(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
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
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
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


def test_repair_strips_generated_see_also_lines_from_content_link_edges(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    source = _run(service.store("plain text", scope="global", key="source"))
    path = Path(source.file_path)
    path.write_text(
        path.read_text(encoding="utf-8") + "\n## See Also\n- [target](../global/target.md)\n",
        encoding="utf-8",
    )

    MemoryReconciliationService(tmp_path, isolated_memory_db).apply()

    assert _edges(isolated_memory_db) == set()


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


def test_content_links_false_keeps_identical_stores_as_distinct_entries(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: False)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)

    stored = _run(service.store("same fact", scope="global", key="source"))
    _run(service.store("same fact", scope="global", key="source"))

    rendered = Path(stored.file_path).read_text(encoding="utf-8")
    assert rendered.count("same fact") == 2


def test_content_links_flag_does_not_change_recall_shape(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    from cli_agent_orchestrator.services import settings_service

    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: False)
    _run(service.store("[[target]]", scope="global", key="source-off"))
    off = _run(service.recall(scope="global", record_access=False))
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)
    _run(service.store("[[target]]", scope="global", key="source-on"))
    on = _run(service.recall(scope="global", record_access=False))

    assert {
        (memory.key.removesuffix("-off").removesuffix("-on"), memory.content) for memory in off
    } == {
        (memory.key.removesuffix("-off").removesuffix("-on"), memory.content)
        for memory in on
        if memory.key != "source-off"
    }


def test_store_drops_dangling_targets_before_capping_valid_edges(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    targets = [f"target-{index:02d}" for index in range(MAX_EDGES_PER_MUTATION + 1)]
    for target in targets:
        _run(service.store(target, scope="global", key=target))

    missing = [f"missing-{index:02d}" for index in range(8)]
    body = " ".join(f"[[{target}]]" for target in [*missing, *targets])
    _run(service.store(body, scope="global", key="source"))

    edges = sorted(
        target
        for source, target, origin in _edges(isolated_memory_db)
        if source == "source" and origin == "content_link"
    )
    assert len(edges) == MAX_EDGES_PER_MUTATION
    assert set(edges) == set(targets[:MAX_EDGES_PER_MUTATION])


def test_edge_sync_failure_is_best_effort_and_retry_is_edge_idempotent(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    original_sync = service._sync_content_link_edges

    def broken_sync(*args, **kwargs):
        raise RuntimeError("relationship table unavailable")

    monkeypatch.setattr(service, "_sync_content_link_edges", broken_sync)
    stored = _run(service.store("[[target]]", scope="global", key="source"))
    monkeypatch.setattr(service, "_sync_content_link_edges", original_sync)
    _run(service.store("[[target]]", scope="global", key="source"))

    rendered = Path(stored.file_path).read_text(encoding="utf-8")
    assert rendered.count("[[target]]") == 2
    assert _edges(isolated_memory_db) == {("source", "target", "content_link")}


def test_mixed_see_also_strips_generated_lines_but_keeps_authored_links(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("generated", scope="global", key="generated"))
    _run(service.store("authored", scope="global", key="authored"))

    _run(
        service.store(
            "## See Also\n"
            "- [generated](../global/generated.md)\n"
            "- Authored context for [[authored]]\n",
            scope="global",
            key="source",
        )
    )

    assert _edges(isolated_memory_db) == {
        ("source", "authored", "content_link"),
    }


def test_compact_preserves_authored_see_also_links_through_store_and_repair(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    """Identity compile must not turn authored See Also links into generated-only links."""
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    monkeypatch.setattr(service, "_schedule_background_compile", lambda **kwargs: None)
    monkeypatch.setattr(
        relationship_module,
        "SessionLocal",
        sessionmaker(bind=isolated_memory_db),
    )

    async def identity_compile(existing, new_entry, **kwargs):
        return CompileResult(
            compiled_content=existing if not new_entry else f"{existing}\n{new_entry}",
            used_llm=True,
            fallback_reason=None,
            elapsed_ms=0,
        )

    async def no_related(*args, **kwargs):
        return RelatedResult([], True, None, 0)

    from cli_agent_orchestrator.services import wiki_compiler

    monkeypatch.setattr(wiki_compiler, "compile", identity_compile)
    monkeypatch.setattr(wiki_compiler, "find_related", no_related)
    for key in ("authored", "body-link", "plain-key"):
        _run(service.store(key, scope="global", key=key))
    stored = _run(
        service.store(
            "[[body-link]]\n\n## See Also\n"
            "- Authored context for [[authored]]\n"
            "- plain-key\n",
            scope="global",
            key="source",
        )
    )
    expected = {
        ("source", "authored", "content_link"),
        ("source", "body-link", "content_link"),
        ("source", "plain-key", "content_link"),
    }
    assert _edges(isolated_memory_db) == expected

    assert _run(service.compact(key="source"))["source"] == "applied"
    assert "Authored context for [[authored]]" in Path(stored.file_path).read_text(encoding="utf-8")
    assert _edges(isolated_memory_db) == expected

    _run(service.store("second entry", scope="global", key="source"))
    assert _edges(isolated_memory_db) == expected
    assert _run(service.compact(key="source"))["source"] == "applied"

    reconciliation = MemoryReconciliationService(tmp_path, isolated_memory_db)
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_reconciliation.MemoryReconciliationService",
        lambda: reconciliation,
    )
    result = CliRunner().invoke(repair_cmd, ["--apply"])

    assert result.exit_code == 0, result.output
    assert _edges(isolated_memory_db) == expected


def test_generated_see_also_does_not_become_a_self_sustaining_content_edge(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _run(service.store("target", scope="global", key="target"))
    source = _run(service.store("source", scope="global", key="source"))
    factory = sessionmaker(bind=isolated_memory_db)
    monkeypatch.setattr(relationship_module, "SessionLocal", factory)
    relationships = MemoryRelationshipService()
    human = relationships.create(
        "global",
        None,
        "source",
        "target",
        "relates_to",
        "human",
    )
    source_path = Path(source.file_path)
    source_path.write_text(
        source_path.read_text(encoding="utf-8")
        + "\n## See Also\n- [target](../global/target.md)\n",
        encoding="utf-8",
    )

    _run(service.store("plain second entry", scope="global", key="source"))
    relationships.soft_delete(human.id)

    assert relationships.active_targets("global", None, "source") == []
    assert not any(origin == "content_link" for _, _, origin in _edges(isolated_memory_db))


def _identity_compile_stubs(service, monkeypatch, isolated_memory_db, related_keys=()) -> None:
    from cli_agent_orchestrator.services import wiki_compiler

    monkeypatch.setattr(service, "_schedule_background_compile", lambda **kwargs: None)
    monkeypatch.setattr(relationship_module, "SessionLocal", sessionmaker(bind=isolated_memory_db))

    async def identity_compile(existing, new_entry, **kwargs):
        return CompileResult(
            compiled_content=existing if not new_entry else f"{existing}\n{new_entry}",
            used_llm=True,
            fallback_reason=None,
            elapsed_ms=0,
        )

    async def related(*args, **kwargs):
        return RelatedResult(list(related_keys), True, None, 0)

    monkeypatch.setattr(wiki_compiler, "compile", identity_compile)
    monkeypatch.setattr(wiki_compiler, "find_related", related)


def _see_also_section(path: str) -> str:
    text = Path(path).read_text(encoding="utf-8")
    return text.split("## See Also", 1)[1] if "## See Also" in text else ""


def test_authored_see_also_lines_are_deduped_in_first_seen_order_and_skip_blanks() -> None:
    first = (
        "# k\n\n## See Also\n- Authored context for [[a]]\n\n"
        "- [b](../global/b.md)\n   \n- plain-key\n"
    )
    second = "## See Also\n- plain-key\n- Authored context for [[a]]\n- another [[c]]\n"

    lines = MemoryService._authored_see_also_lines(first, second)

    assert lines == ["- Authored context for [[a]]", "- plain-key", "- another [[c]]"]


def test_repeated_compiles_do_not_grow_the_authored_see_also_section(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    _enable_content_links(monkeypatch)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _identity_compile_stubs(service, monkeypatch, isolated_memory_db)
    for key in ("authored", "body-link", "plain-key"):
        _run(service.store(key, scope="global", key=key))
    stored = _run(
        service.store(
            "[[body-link]]\n\n## See Also\n"
            "- Authored context for [[authored]]\n\n"
            "- plain-key\n",
            scope="global",
            key="source",
        )
    )
    expected = {
        ("source", "authored", "content_link"),
        ("source", "body-link", "content_link"),
        ("source", "plain-key", "content_link"),
    }
    sections = []
    for cycle in range(3):
        assert _run(service.compact(key="source"))["source"] == "applied"
        sections.append(_see_also_section(stored.file_path))
        assert _edges(isolated_memory_db) == expected
        _run(service.store(f"entry {cycle}", scope="global", key="source"))

    assert sections[1] == sections[2]
    assert sections[0].count("\n") == sections[2].count("\n")
    assert sections[2].count("Authored context for [[authored]]") == 1
    assert sections[2].count("- plain-key") == 1


def test_compile_with_content_links_off_replaces_authored_see_also_as_before(
    tmp_path: Path, isolated_memory_db, monkeypatch
) -> None:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: False)
    service = MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)
    _identity_compile_stubs(service, monkeypatch, isolated_memory_db, related_keys=["target"])
    _run(service.store("target", scope="global", key="target"))
    stored = _run(
        service.store(
            "body\n\n## See Also\n- Authored context for [[target]]\n- plain-key\n",
            scope="global",
            key="source",
        )
    )

    assert _run(service.compact(key="source"))["source"] == "applied"

    text = Path(stored.file_path).read_text(encoding="utf-8")
    assert "Authored context" not in text
    assert "plain-key" not in text
    assert "- [target](../global/target.md)" in text
