"""Recall-v2 snippets, optional fields, and exact-key reads (#1329 P2)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.models.memory import Memory, mcp_recall_wire_dict
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


def test_v2_fields_and_get_count_all_hidden_entries(service: MemoryService) -> None:
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
            "An invalid timestamp is not an entry."
        ),
        timestamps=(
            ("2026-09-01T01:02:03Z", "Earlier real entry."),
            ("2026-11-14T05:54:00Z", "Current real entry."),
        ),
    )

    result = asyncio.run(service.get_memory("valid-history-only", scope="global"))

    assert result["earlier_entries"] == 1


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
    with service._get_db_session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key="route-note", scope="global").one()
        assert row.access_count == 0


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

    ambiguous = asyncio.run(tool(key="same-key", scope=None, include_history=False, max_chars=8000))
    missing = asyncio.run(
        tool(key="missing-note", scope="global", include_history=False, max_chars=8000)
    )

    assert ambiguous["error_kind"] == "ambiguous_scope"
    assert ambiguous["scopes"] == ["global", "federated"]
    assert missing["error_kind"] == "not_found"


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
