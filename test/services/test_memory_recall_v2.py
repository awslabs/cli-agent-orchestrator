"""Recall-v2 snippets, optional fields, and exact-key reads (#1329 P2)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from test.fixtures.vault_factory import build_vault_fixture

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.models.memory import (
    Memory,
    _fallback_snippet,
    mcp_recall_wire_dict,
)
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.memory_service import (
    MemoryAmbiguousScopeError,
    MemoryNotFoundError,
    MemoryService,
)
from cli_agent_orchestrator.services.vault.reader import MEMORY_MANAGER_PROFILE


@pytest.fixture
def service(tmp_path: Path) -> MemoryService:
    engine = create_engine(
        f"sqlite:///{tmp_path / 'metadata.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return MemoryService(tmp_path, engine)


def _topic(
    service: MemoryService,
    key: str,
    body: str,
    *,
    scope: str = "global",
    timestamps: tuple[str, ...] = (),
) -> Path:
    container = service.base_dir / ("federated" if scope == "federated" else "global")
    path = container / "wiki" / scope / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    stamped = "".join(f"## {stamp}\n{entry}\n\n" for stamp, entry in timestamps)
    path.write_text(
        f"# {key}\n"
        f"<!-- id: {uuid.uuid4()} | scope: {scope} | type: reference | tags: recall-v2 -->\n"
        f"\n{stamped}{body}\n",
        encoding="utf-8",
    )
    index = container / "wiki" / "index.md"
    existing = index.read_text(encoding="utf-8") if index.exists() else "# CAO Memory Index\n"
    section = f"\n## {scope}\n"
    if section not in existing:
        existing += section
    existing += (
        f"- [{key}]({scope}/{key}.md) — type:reference tags:recall-v2 "
        "~18tok updated:2026-11-14T05:54:00Z\n"
    )
    index.write_text(existing, encoding="utf-8")
    return path


def _memory() -> Memory:
    at = datetime(2026, 11, 14, 5, 54, tzinfo=timezone.utc)
    return Memory(
        id="00000000-0000-0000-0000-000000000001",
        key="shape-lock",
        memory_type="reference",
        scope="global",
        file_path="/memory/global/wiki/global/shape-lock.md",
        tags="recall-v2",
        created_at=at,
        updated_at=at,
        content="Wire shape stays unchanged.",
    )


def test_v2_fields_off_keeps_the_pinned_mcp_wire_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: False)

    assert mcp_recall_wire_dict(_memory(), detail="full") == {
        "key": "shape-lock",
        "content": "Wire shape stays unchanged.",
        "memory_type": "reference",
        "scope": "global",
        "tags": "recall-v2",
        "file_path": "/memory/global/wiki/global/shape-lock.md",
        "source_kind": "native",
        "source_path": None,
        "indexed_at": None,
        "index_freshness": None,
        "content_truncated": False,
        "updated_at": "2026-11-14T05:54:00Z",
    }


def test_recall_route_omits_v2_fields_and_content_for_snippets(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: False)
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    _topic(
        service,
        "route-snippet",
        "Cobalt route content.",
        timestamps=(("2026-11-14T05:54:00Z", ""),),
    )

    response = TestClient(main.app, base_url="http://localhost").post(
        "/internal/memory/recall",
        json={"query": "cobalt", "detail": "snippet"},
    )

    assert response.status_code == 200
    result = response.json()["memories"][0]
    assert "fact_date" not in result
    assert "content" not in result
    assert "cobalt" in result["snippet"].lower()


def test_snippet_contains_matched_terms_and_get_hint(service: MemoryService) -> None:
    _topic(
        service,
        "snippet-note",
        "A short prefix. The cobalt release lease belongs to the build worker. A short suffix.",
        timestamps=(("2026-11-14T05:54:00Z", ""),),
    )

    rows = asyncio.run(
        service.recall(
            query="cobalt lease",
            scope="global",
            search_mode="metadata",
            detail="snippet",
        )
    )
    wire = mcp_recall_wire_dict(rows[0], detail="snippet", include_v2_fields=False)

    assert "content" not in wire
    assert "cobalt" in wire["snippet"].lower()
    assert "lease" in wire["snippet"].lower()
    assert wire["get"] == "memory_get(key='snippet-note', scope='global')"


def test_snippet_match_terms_only_claim_terms_in_the_window(service: MemoryService) -> None:
    _topic(
        service,
        "long-snippet",
        "cobalt " + ("padding " * 80) + "lease",
        timestamps=(("2026-11-14T05:54:00Z", ""),),
    )

    rows = asyncio.run(
        service.recall(
            query="cobalt lease",
            scope="global",
            search_mode="metadata",
            detail="snippet",
        )
    )

    assert "cobalt" in rows[0].snippet.lower()
    assert "lease" not in rows[0].snippet.lower()
    assert rows[0].match["terms"] == ["cobalt"]


def test_snippets_keep_a_match_after_turkish_capital_i(service: MemoryService) -> None:
    content = ("İ" * 600) + " target " + ("z" * 600)

    assert "target" in service._snippet(content, ["target"]).lower()
    assert (
        "target"
        in _fallback_snippet(
            _memory().model_copy(update={"content": content, "match": {"terms": ["target"]}})
        ).lower()
    )
    query_content = ("z" * 600) + " İstanbul " + ("z" * 600)
    assert "İstanbul" in service._snippet(query_content, service._query_terms("İstanbul"))
    assert "İstanbul" in _fallback_snippet(
        _memory().model_copy(update={"content": query_content, "match": {"terms": ["İstanbul"]}})
    )
    memory = _memory().model_copy(update={"content": query_content})
    service._apply_recall_detail([memory], "İstanbul", "metadata", "snippet")
    assert "İstanbul" in memory.snippet
    assert memory.match["terms"] == ["İstanbul"]


def test_recall_status_uses_lifecycle_state(service: MemoryService) -> None:
    memory = _memory().model_copy(update={"lifecycle_status": "superseded"})
    service._apply_recall_detail([memory], None, "metadata", "full")
    assert mcp_recall_wire_dict(memory, include_v2_fields=True)["status"] == "superseded"


def test_remote_recall_tool_preserves_snippet_detail_and_owner_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.mcp_server import server
    from cli_agent_orchestrator.services import memory_gateway

    memory = _memory().model_copy(update={"v2_fields_present": False, "snippet": "Wire shape"})
    received = []

    async def remote_recall(**kwargs):
        received.append(kwargs)
        return [memory]

    monkeypatch.setattr(memory_gateway, "remote_memory_url", lambda: "http://memory-owner:9889")
    monkeypatch.setattr(memory_gateway, "recall_memory", remote_recall)
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    tool = getattr(server.memory_recall, "fn", server.memory_recall)
    result = asyncio.run(
        tool(
            query="wire",
            scope="global",
            memory_type=None,
            limit=10,
            search_mode="metadata",
            sort_by=None,
            include_related=False,
            detail="snippet",
        )
    )

    assert result["success"] is True
    assert received[0]["detail"] == "snippet"
    assert "content" not in result["memories"][0]
    assert "fact_date" not in result["memories"][0]


def test_v2_fields_expose_dates_match_and_hidden_history(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    _topic(
        service,
        "history-note",
        "",
        timestamps=(
            ("2026-10-01T01:02:03Z", "Earlier release detail."),
            ("2026-11-14T05:54:00Z", "Current release detail."),
        ),
    )

    rows = asyncio.run(
        service.recall(
            query="current release",
            scope="global",
            search_mode="metadata",
            detail="snippet",
        )
    )
    legacy_wire = mcp_recall_wire_dict(rows[0], detail="full", include_v2_fields=False)
    wire = mcp_recall_wire_dict(rows[0], detail="snippet", include_v2_fields=True)

    assert legacy_wire["content_truncated"] is False
    assert "earlier_entries" not in legacy_wire
    assert wire["fact_date"] == "2026-10-01T01:02:03Z"
    assert wire["edited_at"] == "2026-11-14T05:54:00Z"
    assert wire["match"] == {
        "mode": "metadata",
        "terms": ["current", "release"],
        "rank_reason": "metadata match",
    }
    assert wire["status"] == "active"
    assert wire["earlier_entries"] == 1
    assert wire["content_truncated"] is True


def test_v2_fields_and_get_count_all_hidden_entries(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    _topic(
        service,
        "long-history",
        "",
        timestamps=(
            ("2026-08-01T01:02:03Z", "First entry."),
            ("2026-09-01T01:02:03Z", "Second entry."),
            ("2026-10-01T01:02:03Z", "Third entry."),
            ("2026-11-14T05:54:00Z", "Current entry."),
        ),
    )

    recalled = asyncio.run(
        service.recall(scope="global", search_mode="metadata", record_access=False)
    )
    fetched = asyncio.run(
        service.get_memory(
            "long-history",
            scope="global",
            include_history=True,
            max_chars=8000,
        )
    )

    assert recalled[0].earlier_entries == 3
    assert fetched["earlier_entries"] == 3
    assert len(fetched["history"]) == 4


def test_entry_count_ignores_non_heading_and_unparseable_timestamps(
    service: MemoryService,
) -> None:
    _topic(
        service,
        "valid-history-only",
        (
            "A body token ## 2026-10-01T01:02:03Z is not an entry.\n"
            "### 2026-10-02T01:02:03Z\n"
            "A sub-heading is not an entry.\n"
            "## 2026-13-01T01:02:03Z\n"
            "An invalid timestamp is not an entry.\n"
            "## 2026-02-30T01:02:03Z\n"
            "An invalid calendar day is not an entry.\n"
            "## 2026-01-01T24:00:00Z\n"
            "An invalid time is not an entry."
        ),
        timestamps=(
            ("2026-09-01T01:02:03Z", "Earlier real entry."),
            ("2026-11-14T05:54:00Z", "Current real entry."),
        ),
    )

    result = asyncio.run(service.get_memory("valid-history-only", scope="global"))

    assert result["earlier_entries"] == 1


def test_lenient_entry_count_ignores_invalid_timestamp(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        settings_service, "get_memory_settings", lambda: {"parser": {"lenient": True}}
    )
    path = _topic(
        service,
        "lenient-invalid",
        ("## Entry 2026-13-01T01:02:03Z\ninvalid\n\n" "## Entry 2026-11-14T05:54:00Z\nvalid"),
    )
    memory = service._parse_wiki_file(
        path,
        path.read_text(encoding="utf-8"),
        {"key": "lenient-invalid", "scope": "global", "memory_type": "reference", "tags": ""},
        count_entries=True,
    )

    assert memory is not None
    assert memory.earlier_entries == 0


def test_memory_get_history_uses_only_parseable_entry_boundaries(service: MemoryService) -> None:
    _topic(
        service,
        "history-boundaries",
        (
            "A body token ## 2026-10-01T01:02:03Z is not an entry.\n"
            "### Standup 2026-10-02T01:02:03Z\n"
            "A dated sub-heading remains in the current entry.\n"
            "## 2026-13-01T01:02:03Z\n"
            "An invalid timestamp is not an entry."
        ),
        timestamps=(
            ("2026-09-01T01:02:03Z", "Earlier real entry."),
            ("2026-11-14T05:54:00Z", "Current real entry."),
        ),
    )

    result = asyncio.run(
        service.get_memory("history-boundaries", scope="global", include_history=True)
    )

    assert len(result["history"]) == result["earlier_entries"] + 1
    assert [item["date"] for item in result["history"]] == [
        "2026-11-14T05:54:00Z",
        "2026-09-01T01:02:03Z",
    ]


@pytest.mark.parametrize("search_mode", ["bm25", "metadata"])
def test_memory_manager_bm25_fills_limit_from_visible_scopes(
    service: MemoryService, search_mode: str
) -> None:
    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    for number in range(3):
        asyncio.run(
            service.store(
                content=f"cobalt visible global note {number}",
                scope="global",
                memory_type="reference",
                key=f"visible-{number}",
            )
        )
    for number in range(4):
        asyncio.run(
            service.store(
                content=f"cobalt cobalt cobalt private agent note {number}",
                scope="agent",
                memory_type="reference",
                key=f"private-{number}",
                terminal_context=context,
            )
        )

    rows = asyncio.run(
        service.recall(
            query="cobalt",
            limit=3,
            search_mode=search_mode,
            terminal_context=context,
            record_access=False,
        )
    )

    assert len(rows) == 3
    assert {row.scope for row in rows} == {"global"}


def test_metadata_pool_excludes_curator_hidden_scopes_before_limit(
    service: MemoryService,
) -> None:
    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    asyncio.run(
        service.store(
            content="cobalt visible", scope="global", memory_type="reference", key="visible"
        )
    )
    asyncio.run(
        service.store(
            content="cobalt private",
            scope="agent",
            memory_type="reference",
            key="private",
            terminal_context=context,
        )
    )

    rows = asyncio.run(
        service._metadata_recall(
            query="cobalt",
            scope=None,
            memory_type=None,
            limit=10,
            terminal_context=context,
            allowed_scopes={"session", "project", "global"},
        )
    )

    assert [row.key for row in rows] == ["visible"]


def test_related_v2_rows_count_hidden_entries(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    _topic(service, "primary-related", "Primary search body.")
    _topic(
        service,
        "related-history",
        "",
        timestamps=(
            ("2026-01-01T01:02:03Z", "Oldest body."),
            ("2026-01-02T01:02:03Z", "Middle body."),
            ("2026-01-03T01:02:03Z", "Newest body."),
        ),
    )
    monkeypatch.setattr(
        service,
        "_related_target_plan",
        lambda primaries: [(primary, ["related-history"]) for primary in primaries],
    )

    rows = asyncio.run(
        service.recall(
            query="primary-related",
            scope="global",
            search_mode="metadata",
            include_related=True,
            record_access=False,
        )
    )

    related = next(row for row in rows if row.key == "related-history")
    wire = mcp_recall_wire_dict(related, include_v2_fields=True)
    assert wire["earlier_entries"] == 2
    assert wire["content_truncated"] is True


@pytest.mark.parametrize("sort_by", [None, "score"])
def test_related_v2_rows_count_hidden_entries_with_recorded_access(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch, sort_by: str | None
) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    _topic(service, "primary-related", "Primary search body.")
    _topic(
        service,
        "related-history",
        "",
        timestamps=(
            ("2026-01-01T01:02:03Z", "Oldest body."),
            ("2026-01-02T01:02:03Z", "Middle body."),
            ("2026-01-03T01:02:03Z", "Newest body."),
        ),
    )
    monkeypatch.setattr(
        service,
        "_related_target_plan",
        lambda primaries: [(primary, ["related-history"]) for primary in primaries],
    )
    options = {"sort_by": sort_by} if sort_by is not None else {}
    rows = asyncio.run(
        service.recall(
            query="primary-related",
            scope="global",
            search_mode="metadata",
            include_related=True,
            record_access=True,
            **options,
        )
    )
    related = next(row for row in rows if row.key == "related-history")
    wire = mcp_recall_wire_dict(related, include_v2_fields=True)
    assert wire["earlier_entries"] == 2
    assert wire["content_truncated"] is True
    if sort_by is None:
        from cli_agent_orchestrator.mcp_server import server
        from cli_agent_orchestrator.services import memory_gateway, memory_service

        monkeypatch.setattr(memory_gateway, "remote_memory_url", lambda: None)
        monkeypatch.setattr(memory_service, "MemoryService", lambda: service)
        monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: None)
        tool = getattr(server.memory_recall, "fn", server.memory_recall)
        response = asyncio.run(
            tool(
                query="primary-related",
                scope="global",
                memory_type=None,
                limit=10,
                search_mode="metadata",
                sort_by=None,
                include_related=True,
                detail="full",
            )
        )
        tool_related = next(row for row in response["memories"] if row["key"] == "related-history")
        assert tool_related["earlier_entries"] == 2
        assert tool_related["content_truncated"] is True


@pytest.mark.parametrize("search_mode", ["metadata", "bm25"])
@pytest.mark.parametrize("detail", ["full", "snippet"])
@pytest.mark.parametrize("query", ["Widgets widgets", "WIDGETS Widgets widgets"])
def test_recall_match_terms_deduplicate_after_normalizing(
    service: MemoryService, search_mode: str, detail: str, query: str
) -> None:
    _topic(service, "widgets-note", "Widgets", timestamps=(("2026-01-01T01:02:03Z", ""),))
    rows = asyncio.run(
        service.recall(query=query, scope="global", search_mode=search_mode, detail=detail)
    )
    assert rows[0].match["terms"] == ["widgets"]


def _configure_vault_exact_read(
    service: MemoryService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, dict[str, str]]:
    from cli_agent_orchestrator.services import memory_service
    from cli_agent_orchestrator.services.memory_service import resolve_project_id
    from cli_agent_orchestrator.services.vault import reader
    from cli_agent_orchestrator.services.vault import reconcile as reconcile_module
    from cli_agent_orchestrator.services.vault.config import VaultConfig

    session_factory = sessionmaker(bind=service._db_engine)
    monkeypatch.setattr(reconcile_module, "SessionLocal", session_factory)
    monkeypatch.setattr(reader, "SessionLocal", session_factory)
    monkeypatch.setattr(reconcile_module, "_replace_vault_edges", lambda _notes, **_kwargs: None)
    monkeypatch.setattr(reconcile_module, "_clear_stale_vault_edges", lambda *_args: None)
    monkeypatch.setattr(reconcile_module, "_emit_audit_events", lambda *_args: None)
    monkeypatch.setattr(memory_service, "_is_memory_enabled", lambda: True)
    ordinary_policy = lambda require_injectable, **_kwargs: reader.VaultInjectionPolicy(
        require_injectable, "test", False
    )
    monkeypatch.setattr(reader, "_resolve_injection_policy", ordinary_policy)
    monkeypatch.setattr(memory_service, "_resolve_injection_policy", ordinary_policy)

    fixture = build_vault_fixture(tmp_path)
    context = {"agent_profile": "fixture-reference", "cwd": str(tmp_path)}
    mappings = [
        (
            mapping.model_copy(update={"scope_id": resolve_project_id(tmp_path)})
            if mapping.scope == "project"
            else mapping
        )
        for mapping in fixture.vault.mappings
    ]
    vault = fixture.vault.model_copy(update={"mappings": mappings})
    monkeypatch.setattr(
        settings_service, "get_vault_config", lambda: VaultConfig(enabled=True, vaults=[vault])
    )
    return fixture.root, context


def test_memory_get_without_scope_finds_callers_agent_vault_note(
    service: MemoryService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, context = _configure_vault_exact_read(service, tmp_path, monkeypatch)
    from cli_agent_orchestrator.services.vault.identity import derive_cao_key
    from cli_agent_orchestrator.services.vault.reconcile import reconcile

    reconcile(settings_service.get_vault_config().vaults[0], apply=True, run_id="exact-agent")

    result = asyncio.run(
        service.get_memory(derive_cao_key("Glossary.md"), terminal_context=context)
    )

    assert result["scope"] == "agent"
    assert result["content"] == "Glossary"


def test_memory_get_without_scope_detects_agent_and_project_vault_ambiguity(
    service: MemoryService, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, context = _configure_vault_exact_read(service, tmp_path, monkeypatch)
    (root / "Projects" / "CAO Design" / "Glossary.md").write_text(
        "Project glossary.\n", encoding="utf-8"
    )
    from cli_agent_orchestrator.services.vault.identity import derive_cao_key
    from cli_agent_orchestrator.services.vault.reconcile import reconcile

    reconcile(settings_service.get_vault_config().vaults[0], apply=True, run_id="exact-ambiguous")

    with pytest.raises(MemoryAmbiguousScopeError) as caught:
        asyncio.run(service.get_memory(derive_cao_key("Glossary.md"), terminal_context=context))

    assert caught.value.scopes == ["project", "agent"]


def test_memory_get_without_scope_resolves_and_parses_sources_once(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _topic(service, "single-lookup", "Global body.")
    resolve_calls = 0
    index_parse_calls = 0
    original_resolve_sources = service._resolve_sources
    original_parse_index = service._parse_index

    def counting_resolve_sources(*args, **kwargs):
        nonlocal resolve_calls
        resolve_calls += 1
        return original_resolve_sources(*args, **kwargs)

    def counting_parse_index(*args, **kwargs):
        nonlocal index_parse_calls
        index_parse_calls += 1
        return original_parse_index(*args, **kwargs)

    monkeypatch.setattr(service, "_resolve_sources", counting_resolve_sources)
    monkeypatch.setattr(service, "_parse_index", counting_parse_index)

    result = asyncio.run(service.get_memory("single-lookup"))

    assert result["scope"] == "global"
    assert resolve_calls == 1
    assert index_parse_calls == 1


@pytest.mark.parametrize("scope", ["agent", "federated"])
def test_memory_get_hides_noninjectable_scopes_from_memory_manager(
    service: MemoryService, scope: str
) -> None:
    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    asyncio.run(
        service.store(
            content=f"Synthetic {scope} private body.",
            scope=scope,
            memory_type="reference",
            key=f"{scope}-private",
            terminal_context=context,
        )
    )

    with pytest.raises(MemoryNotFoundError):
        asyncio.run(
            service.get_memory(
                f"{scope}-private",
                scope=scope,
                terminal_context=context,
            )
        )


@pytest.mark.parametrize("scope", ["agent", "federated"])
def test_internal_memory_get_hides_noninjectable_scopes_from_memory_manager(
    service: MemoryService, scope: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    asyncio.run(
        service.store(
            content=f"Synthetic remote {scope} private body.",
            scope=scope,
            memory_type="reference",
            key=f"remote-{scope}-private",
            terminal_context=context,
        )
    )
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)

    response = TestClient(main.app, base_url="http://localhost").post(
        "/internal/memory/get",
        json={
            "key": f"remote-{scope}-private",
            "scope": scope,
            "terminal_context": context,
        },
    )

    assert response.status_code == 404
    assert response.json()["error_kind"] == "not_found"


def test_internal_memory_get_maps_success_ambiguity_and_record_access(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    global_path = _topic(service, "route-note", "Global route body.", scope="global")
    _topic(service, "route-note", "Federated route body.", scope="federated")
    service._upsert_metadata(
        key="route-note",
        memory_type="reference",
        scope="global",
        scope_id=None,
        file_path=str(global_path),
        tags="recall-v2",
        source_provider=None,
        source_terminal_id=None,
        token_estimate=3,
    )
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    client = TestClient(main.app, base_url="http://localhost")

    success = client.post(
        "/internal/memory/get",
        json={"key": "route-note", "scope": "global", "record_access": False},
    )
    ambiguous = client.post("/internal/memory/get", json={"key": "route-note"})

    assert success.status_code == 200
    assert success.json()["memory"]["content"] == "Global route body."
    assert ambiguous.status_code == 409
    assert ambiguous.json()["error_kind"] == "ambiguous_scope"
    assert ambiguous.content == (
        b'{"error_kind":"ambiguous_scope","error":"memory key \'route-note\' exists in '
        b'scopes: global, federated","scopes":["global","federated"]}'
    )
    with service._get_db_session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key="route-note", scope="global").one()
        assert row.access_count == 0


def test_internal_memory_get_errors_do_not_stringify_exceptions(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    _topic(service, "same-key", "Global body.", scope="global")
    _topic(service, "same-key", "Federated body.", scope="federated")
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)

    def unexpected_stringification(_error: Exception) -> str:
        raise AssertionError("response must not stringify the exception")

    monkeypatch.setattr(MemoryAmbiguousScopeError, "__str__", unexpected_stringification)
    monkeypatch.setattr(MemoryNotFoundError, "__str__", unexpected_stringification)
    client = TestClient(main.app, base_url="http://localhost")

    ambiguous = client.post("/internal/memory/get", json={"key": "SAME-KEY!"})
    missing = client.post("/internal/memory/get", json={"key": "Missing-Note!", "scope": "global"})

    assert ambiguous.status_code == 409
    assert ambiguous.content == (
        b'{"error_kind":"ambiguous_scope","error":"memory key \'same-key\' exists in '
        b'scopes: global, federated","scopes":["global","federated"]}'
    )
    assert missing.status_code == 404
    assert (
        missing.content
        == b'{"error_kind":"not_found","error":"memory not found: \'missing-note\'"}'
    )


def test_memory_get_returns_headingless_body_and_counts_access(service: MemoryService) -> None:
    path = _topic(
        service,
        "visible-now",
        "The full headingless body that was invisible before the lenient parser.",
    )
    service._upsert_metadata(
        key="visible-now",
        memory_type="reference",
        scope="global",
        scope_id=None,
        file_path=str(path),
        tags="recall-v2",
        source_provider=None,
        source_terminal_id=None,
        token_estimate=18,
    )

    result = asyncio.run(service.get_memory("visible-now", scope="global"))

    assert result["content"] == (
        "The full headingless body that was invisible before the lenient parser."
    )
    assert result["key"] == "visible-now"
    assert result["scope"] == "global"
    assert result["status"] == "active"
    assert result["earlier_entries"] == 0
    with service._get_db_session() as db:
        assert db.query(MemoryMetadataModel).filter_by(key="visible-now").one().access_count == 1


def test_compiled_article_keeps_earliest_canonical_fact_date(service: MemoryService) -> None:
    path = _topic(
        service,
        "compiled-dates",
        "<!-- cao-compiled: 2026-11-14T06:00:00Z entries=2 -->\n"
        "## 2026-11-14T05:54:00Z\n\n"
        "### Entry 2026-10-01T01:02:03Z\nEarlier detail.\n\n"
        "### Entry 2026-11-14T05:54:00Z\nCurrent detail.\n\n"
        "## Overview\nCurrent compiled article.",
    )
    entry = {
        "key": "compiled-dates",
        "scope": "global",
        "scope_id": None,
        "memory_type": "reference",
        "tags": "recall-v2",
    }

    memory = service._parse_wiki_file(path, path.read_text(encoding="utf-8"), entry)

    assert memory is not None
    assert memory.fact_date == datetime(2026, 10, 1, 1, 2, 3, tzinfo=timezone.utc)
    assert memory.edited_at == datetime(2026, 11, 14, 5, 54, tzinfo=timezone.utc)
    assert memory.earlier_entries == 0
    assert memory.content_truncated is False


def test_compiled_article_accepts_minute_precision_entry_date(service: MemoryService) -> None:
    path = _topic(
        service,
        "compiled-minutes",
        "<!-- cao-compiled: 2026-11-14T06:00:00Z entries=2 -->\n"
        "## 2026-11-14T05:54:00Z\n\n"
        "### Entry 2026-03-01T08:15Z\nEarlier detail.\n\n"
        "### Entry 2026-11-14T05:54:00Z\nCurrent detail.\n\n"
        "## Overview\nCurrent compiled article.",
    )
    entry = {
        "key": "compiled-minutes",
        "scope": "global",
        "scope_id": None,
        "memory_type": "reference",
        "tags": "recall-v2",
    }

    memory = service._parse_wiki_file(path, path.read_text(encoding="utf-8"), entry)

    assert memory is not None
    assert memory.fact_date == datetime(2026, 3, 1, 8, 15, tzinfo=timezone.utc)


def test_get_links_are_bounded_in_both_directions(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    _topic(service, "bounded-links", "Visible body.")
    edges = [
        SimpleNamespace(type="relates_to", source_key="bounded-links", target_key=f"out-{index}")
        for index in range(7)
    ] + [
        SimpleNamespace(type="relates_to", source_key=f"in-{index}", target_key="bounded-links")
        for index in range(7)
    ]
    monkeypatch.setattr(MemoryRelationshipService, "list_relationships", lambda *_args: edges)

    result = asyncio.run(service.get_memory("bounded-links", scope="global"))

    assert [link["key"] for link in result["links_out"]] == [f"out-{index}" for index in range(5)]
    assert [link["key"] for link in result["links_in"]] == [f"in-{index}" for index in range(5)]


def test_memory_get_ambiguous_key_lists_visible_scopes(service: MemoryService) -> None:
    _topic(service, "same-key", "Global body.", scope="global")
    _topic(service, "same-key", "Federated body.", scope="federated")

    with pytest.raises(MemoryAmbiguousScopeError) as caught:
        asyncio.run(service.get_memory("same-key"))

    assert caught.value.scopes == ["global", "federated"]


def test_memory_get_returns_existing_links(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.services import memory_relationship_service
    from cli_agent_orchestrator.services.memory_relationship_service import (
        MemoryRelationshipService,
    )

    first = _topic(service, "first-note", "First body.")
    second = _topic(service, "second-note", "Second body.")
    for key, path in (("first-note", first), ("second-note", second)):
        service._upsert_metadata(
            key=key,
            memory_type="reference",
            scope="global",
            scope_id=None,
            file_path=str(path),
            tags="recall-v2",
            source_provider=None,
            source_terminal_id=None,
            token_estimate=3,
        )
    monkeypatch.setattr(
        memory_relationship_service,
        "SessionLocal",
        sessionmaker(bind=service._db_engine),
    )
    MemoryRelationshipService().create(
        "global",
        None,
        "first-note",
        "second-note",
        "relates_to",
        "human",
    )

    first_result = asyncio.run(service.get_memory("first-note", scope="global"))
    second_result = asyncio.run(service.get_memory("second-note", scope="global"))

    assert first_result["links_out"] == [{"type": "relates_to", "key": "second-note"}]
    assert second_result["links_in"] == [{"type": "relates_to", "key": "first-note"}]


def test_memory_get_not_found(service: MemoryService) -> None:
    with pytest.raises(MemoryNotFoundError):
        asyncio.run(service.get_memory("missing-note", scope="global"))


def test_memory_get_tool_reports_exact_error_kinds(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.mcp_server import server

    _topic(service, "same-key", "Global body.", scope="global")
    _topic(service, "same-key", "Federated body.", scope="federated")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_service.MemoryService",
        lambda *args, **kwargs: service,
    )
    tool = getattr(server.memory_get, "fn", server.memory_get)

    ambiguous = asyncio.run(
        tool(key="SAME-KEY!", scope=None, include_history=False, max_chars=8000)
    )
    missing = asyncio.run(
        tool(key="Missing-Note!", scope="global", include_history=False, max_chars=8000)
    )

    assert ambiguous["error_kind"] == "ambiguous_scope"
    assert ambiguous["scopes"] == ["global", "federated"]
    assert ambiguous["error"] == "memory key 'same-key' exists in scopes: global, federated"
    assert missing["error_kind"] == "not_found"
    assert missing["error"] == "memory not found: 'missing-note'"


def test_memory_get_tool_does_not_stringify_exact_errors(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.mcp_server import server

    _topic(service, "same-key", "Global body.", scope="global")
    _topic(service, "same-key", "Federated body.", scope="federated")
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.memory_service.MemoryService",
        lambda *args, **kwargs: service,
    )

    def unexpected_stringification(_error: Exception) -> str:
        raise AssertionError("response must not stringify the exception")

    monkeypatch.setattr(MemoryAmbiguousScopeError, "__str__", unexpected_stringification)
    monkeypatch.setattr(MemoryNotFoundError, "__str__", unexpected_stringification)
    tool = getattr(server.memory_get, "fn", server.memory_get)

    ambiguous = asyncio.run(tool(key="same-key", scope=None, include_history=False, max_chars=8000))
    missing = asyncio.run(
        tool(key="missing-note", scope="global", include_history=False, max_chars=8000)
    )

    assert ambiguous == {
        "success": False,
        "error_kind": "ambiguous_scope",
        "error": "memory key 'same-key' exists in scopes: global, federated",
        "scopes": ["global", "federated"],
    }
    assert missing == {
        "success": False,
        "error_kind": "not_found",
        "error": "memory not found: 'missing-note'",
    }


def test_memory_get_tool_rejects_bad_scope_before_remote_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cli_agent_orchestrator.mcp_server import server
    from cli_agent_orchestrator.services import memory_gateway

    async def unexpected_remote_call(**_kwargs):
        pytest.fail("invalid scope reached remote memory owner")

    monkeypatch.setattr(memory_gateway, "remote_memory_url", lambda: "http://memory-owner:9889")
    monkeypatch.setattr(memory_gateway, "get_memory", unexpected_remote_call)
    tool = getattr(server.memory_get, "fn", server.memory_get)
    result = asyncio.run(
        tool(key="missing-note", scope="bad-scope", include_history=False, max_chars=8000)
    )

    assert result["error_kind"] == "not_found"
    assert result["error"] == "memory not found: 'missing-note'"


def test_memory_get_history_is_newest_first_and_capped(service: MemoryService) -> None:
    _topic(
        service,
        "history-get",
        "",
        timestamps=(
            ("2026-10-01T01:02:03Z", "Earlier entry body."),
            ("2026-11-14T05:54:00Z", "Current entry body."),
        ),
    )

    result = asyncio.run(
        service.get_memory(
            "history-get",
            scope="global",
            include_history=True,
            max_chars=28,
        )
    )

    assert result["history"][0]["content"] == "Current entry body."
    assert result["history"][0]["date"] == "2026-11-14T05:54:00Z"
    assert result["earlier_entries"] == 1
    assert result["content_truncated"] is True


def test_memory_get_marks_earlier_entries_as_truncated_without_history(
    service: MemoryService,
) -> None:
    _topic(
        service,
        "hidden-history",
        "",
        timestamps=(
            ("2026-10-01T01:02:03Z", "Earlier entry."),
            ("2026-11-14T05:54:00Z", "Current entry."),
        ),
    )

    result = asyncio.run(service.get_memory("hidden-history", scope="global"))

    assert result["content"] == "Current entry."
    assert result["content_truncated"] is True


def test_memory_get_clips_plain_content_at_max_chars(service: MemoryService) -> None:
    _topic(service, "clip-plain", "abcdefghijklmnopqrstuvwxyz")

    result = asyncio.run(service.get_memory("clip-plain", scope="global", max_chars=8))

    assert result["content"] == "abcdefgh"
    assert result["content_truncated"] is True


def test_memory_get_complete_history_is_not_truncated(service: MemoryService) -> None:
    _topic(
        service,
        "complete-history",
        "",
        timestamps=(
            ("2026-10-01T01:02:03Z", "Earlier entry body."),
            ("2026-11-14T05:54:00Z", "Current entry body."),
        ),
    )

    result = asyncio.run(
        service.get_memory(
            "complete-history",
            scope="global",
            include_history=True,
            max_chars=8000,
        )
    )

    assert len(result["history"]) == 2
    assert result["content_truncated"] is False


def test_memory_get_retains_reader_truncation_with_history(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def read_with_truncation(*_args, **_kwargs):
        return _memory().model_copy(
            update={"content_truncated": True, "lifecycle_status": "superseded"}
        )

    monkeypatch.setattr(service, "read_exact_key", read_with_truncation)
    monkeypatch.setattr(
        service,
        "_memory_history",
        lambda _memory: [
            {"date": "2026-11-14T05:54:00Z", "content": "Wire shape stays unchanged."}
        ],
    )
    result = asyncio.run(service.get_memory("shape-lock", scope="global", include_history=True))

    assert result["content_truncated"] is True
    assert result["status"] == "superseded"


def test_history_shorter_than_entry_count_is_truncated(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    _topic(
        service,
        "short-history",
        "",
        timestamps=(
            ("2026-01-01T01:02:03Z", "Earlier body."),
            ("2026-01-02T01:02:03Z", "Current body."),
        ),
    )
    monkeypatch.setattr(
        service,
        "_memory_history",
        lambda _memory: [{"date": "2026-01-02T01:02:03Z", "content": "Current body."}],
    )

    result = asyncio.run(service.get_memory("short-history", scope="global", include_history=True))

    assert result["earlier_entries"] == 1
    assert len(result["history"]) == 1
    assert result["content_truncated"] is True


def test_default_recall_does_not_count_entry_boundaries(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cli_agent_orchestrator.services import memory_service

    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: False)
    _topic(service, "uncounted-note", "Visible body.")

    def unexpected_count(*_args, **_kwargs):
        pytest.fail("default recall counted entry headings")

    monkeypatch.setattr(memory_service, "_parseable_entry_boundaries", unexpected_count)
    rows = asyncio.run(
        service.recall(query="uncounted-note", scope="global", search_mode="metadata")
    )
    assert [row.key for row in rows] == ["uncounted-note"]


@pytest.mark.parametrize(
    ("headings", "bodies"),
    [
        (
            (
                "## Entry 2026-01-01T01:02:03Z",
                "## Entry 2026-01-02T01:02:03Z",
                "## Entry 2026-01-03T01:02:03Z",
            ),
            ("First entry.", "Second entry.", "Third entry."),
        ),
        (
            ("## 2026-01-01T01:02Z", "## 2026-01-02T01:02Z"),
            ("First entry.", "Second entry."),
        ),
        (
            ("### 2026-01-01T01:02Z", "### 2026-01-02T01:02Z"),
            ("First entry.", "Second entry."),
        ),
        *[
            (
                tuple(pattern.format(stamp) for stamp in timestamps),
                ("First entry.", "Second entry.", "Third entry."),
            )
            for pattern in ("### {}", "#### {}", "## {} Standup", "## {} ")
            for timestamps in [
                ("2026-01-01T01:02:03Z", "2026-01-02T01:02:03Z", "2026-01-03T01:02:03Z")
            ]
        ],
    ],
)
def test_memory_get_lenient_history_uses_counted_boundaries(
    service: MemoryService,
    monkeypatch: pytest.MonkeyPatch,
    headings: tuple[str, ...],
    bodies: tuple[str, ...],
) -> None:
    monkeypatch.setattr(settings_service, "is_memory_recall_v2_fields_enabled", lambda: True)
    path = _topic(service, "lenient-history", "Body to replace.")
    text = path.read_text(encoding="utf-8")
    path.write_text(
        text.replace(
            "Body to replace.",
            "\n\n".join(f"{heading}\n{body}" for heading, body in zip(headings, bodies)),
        ),
        encoding="utf-8",
    )

    result = asyncio.run(service.get_memory("lenient-history", scope="global"))
    assert result["earlier_entries"] == len(bodies) - 1
    assert result["content_truncated"] is True
    rows = asyncio.run(
        service.recall(query="lenient-history", scope="global", search_mode="metadata")
    )
    wire = mcp_recall_wire_dict(rows[0], include_v2_fields=True)
    assert wire["earlier_entries"] == len(bodies) - 1
    assert wire["content_truncated"] is True

    result = asyncio.run(
        service.get_memory("lenient-history", scope="global", include_history=True)
    )

    assert result["earlier_entries"] == len(bodies) - 1
    assert [item["content"] for item in result["history"]] == list(reversed(bodies))
    assert result["content_truncated"] is False

    if len(bodies) == 3 and headings[0].startswith(("### 2026", "#### 2026", "## 2026")):
        monkeypatch.setattr(
            settings_service, "get_memory_settings", lambda: {"parser": {"lenient": False}}
        )
        strict = asyncio.run(
            service.get_memory("lenient-history", scope="global", include_history=True)
        )
        strict_rows = asyncio.run(
            service.recall(query="lenient-history", scope="global", search_mode="metadata")
        )
        assert strict["earlier_entries"] == strict_rows[0].earlier_entries == 0
        assert strict_rows[0].content_truncated is False
        assert len(strict["history"]) == 1


def test_memory_get_does_not_read_unrelated_topic_files(
    service: MemoryService, monkeypatch: pytest.MonkeyPatch
) -> None:
    wanted = _topic(service, "wanted-note", "Wanted body.")
    unrelated = _topic(service, "unrelated-note", "Unrelated body.")
    original_read_text = Path.read_text
    topic_reads: list[Path] = []

    def recording_read_text(path: Path, *args, **kwargs) -> str:
        if path.suffix == ".md":
            topic_reads.append(path)
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", recording_read_text)

    result = asyncio.run(service.get_memory("wanted-note", scope="global"))

    assert result["content"] == "Wanted body."
    assert wanted in topic_reads
    assert unrelated not in topic_reads


def test_memory_get_sanitizes_long_key(service: MemoryService) -> None:
    stored_key = "a" * 60
    _topic(service, stored_key, "Long-key body.")

    result = asyncio.run(service.get_memory("A" * 70, scope="global"))

    assert result["key"] == stored_key


def test_recall_v2_setting_defaults_false_and_round_trips() -> None:
    from cli_agent_orchestrator.services.config_service import ConfigService, MemoryConfig

    assert settings_service.is_memory_recall_v2_fields_enabled() is False
    assert MemoryConfig().recall.v2_fields is False
    assert ConfigService.get("memory.recall.v2_fields") is False

    ConfigService.set("memory.recall.v2_fields", True)

    assert settings_service.is_memory_recall_v2_fields_enabled() is True
    assert ConfigService.get_config().memory.recall.v2_fields is True


def test_recall_v2_setting_honors_environment_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings_service, "_load", lambda: {"memory": {"recall": {"v2_fields": False}}}
    )
    monkeypatch.setenv("CAO_MEMORY_RECALL_V2_FIELDS", "true")

    assert settings_service.is_memory_recall_v2_fields_enabled() is True
