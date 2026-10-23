"""Literal default-output pins for related, curator, vault and wire gaps in C1.

Run unchanged against bfad0d4b and HEAD. Only the fixture root is normalized
in serialized paths; UUIDs and timestamps are fixed in the wiki fixtures.
"""

import asyncio
import json
import os
import re
from datetime import timedelta

from test.services.test_memory_861_default_parity import (  # isort: skip  (per-name noqa below; see the comment)
    CTX,
    STAMP,
    _guard_distinct_bm25_matches,
    corpus,  # noqa: F401  # pytest fixture, injected by name
    defaults,  # noqa: F401  # pytest fixture, injected by name
)
from test.services.test_scoring import _row, _stamp_updated_at
from test.services.vault.test_vault_injection_renderer import _injectable_renderer

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import MemoryMetadataModel, MemoryRelationshipModel
from cli_agent_orchestrator.services import memory_relationship_service


@pytest.fixture
def extended(corpus, monkeypatch):
    service, engine, factory = corpus
    for key, content, memory_type, days in [
        ("global-extra", "pin pin extra", "feedback", 4),
        ("global-ballast", "orchard gardening", "reference", 5),
    ]:
        asyncio.run(
            service.store(key=key, content=content, memory_type=memory_type, scope="global")
        )
        _stamp_updated_at(engine, key, "global", STAMP - timedelta(days=days))
        service._update_index(
            "global",
            None,
            key,
            memory_type,
            "",
            content,
            (STAMP - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "update",
        )
    monkeypatch.setattr(
        service,
        "resolve_scope_id",
        lambda scope, ctx: {
            "project": (
                "other-project" if (ctx or {}).get("cwd") == "/pins/other" else "pin-project"
            ),
            "session": "pin-session",
        }.get(scope),
    )
    asyncio.run(
        service.store(
            key="other-project",
            content="other project pin",
            scope="project",
            terminal_context={"cwd": "/pins/other"},
        )
    )
    _stamp_updated_at(engine, "other-project", "project", STAMP + timedelta(days=1))
    service._update_index(
        "project",
        "other-project",
        "other-project",
        "reference",
        "",
        "other project pin",
        "2026-01-02T00:00:00Z",
        "update",
    )
    ids = {
        "global-new": 1,
        "global-old": 2,
        "global-extra": 3,
        "global-ballast": 4,
        "project-pin": 5,
        "session-pin": 6,
        "other-project": 7,
    }
    with factory() as db:
        for row in db.query(MemoryMetadataModel):
            path = service.get_wiki_path(row.scope, row.scope_id, row.key)
            path.write_text(
                re.sub(
                    r"<!-- id: [a-f0-9\-]+",
                    f"<!-- id: 00000000-0000-0000-0000-{ids[row.key]:012d}",
                    path.read_text(),
                )
            )
        db.query(MemoryMetadataModel).filter_by(key="global-old").update(
            {"related_keys": "global-extra"}
        )
        db.add(
            MemoryRelationshipModel(
                scope="global",
                scope_id="",
                source_key="global-new",
                target_key="global-old",
                type="relates_to",
                status="active",
                origin="human",
            )
        )
        db.commit()
    return service, engine, factory


NATIVE_ROWS = {
    "global-new": (
        "global",
        None,
        "native",
        "pin pin pin pin pin pin pin pin body",
        "2025-12-30T00:00:00+00:00",
        "project",
    ),
    "global-old": ("global", None, "native", "pin body", "2025-12-29T00:00:00+00:00", "project"),
    "global-extra": (
        "global",
        None,
        "native",
        "pin pin extra",
        "2025-12-28T00:00:00+00:00",
        "feedback",
    ),
    "project-pin": (
        "project",
        "pin-project",
        "native",
        "pin pin pin pin body",
        "2025-12-31T00:00:00+00:00",
        "project",
    ),
    "other-project": (
        "project",
        "other-project",
        "native",
        "other project pin",
        "2026-01-02T00:00:00+00:00",
        "project",
    ),
}


BM25_CELLS = {
    "related-store": ("new", [("global-new", False), ("global-old", True)], {"global-new": 1}),
    "related-legacy": ("old", [("global-old", False), ("global-extra", True)], {"global-old": 1}),
    "curator": (
        "pin",
        [("global-old", False), ("global-extra", False)],
        {"global-old": 1, "global-extra": 1},
    ),
    "memory-type": ("pin", [("global-extra", False)], {"global-extra": 1}),
    "current-project": ("pin", [("project-pin", False)], {"project-pin": 1}),
    "scan-all": ("pin", [("project-pin", False)], {"project-pin": 1}),
    "truncating-slice": (
        "pin",
        [("global-old", False), ("global-extra", False)],
        {"global-old": 1, "global-extra": 1},
    ),
}


@pytest.mark.parametrize("search_mode", ["metadata", "hybrid", "bm25"])
@pytest.mark.parametrize(
    "cell,kwargs,expected,deltas",
    [
        (
            "related-store",
            {"include_related": True, "limit": 1},
            [("global-new", False), ("global-old", True)],
            {"global-new": 1},
        ),
        (
            "related-legacy",
            {"query": "global-old", "include_related": True, "limit": 1},
            [("global-old", False), ("global-extra", True)],
            {"global-old": 1},
        ),
        (
            "curator",
            {"terminal_context": dict(CTX, agent_profile="memory_manager"), "limit": 2},
            [("global-new", False), ("global-old", False)],
            {"global-new": 1, "global-old": 1},
        ),
        (
            "memory-type",
            {"memory_type": "feedback", "limit": 2},
            [("global-extra", False)],
            {"global-extra": 1},
        ),
        (
            "current-project",
            {"scope": "project", "limit": 1},
            [("project-pin", False)],
            {"project-pin": 1},
        ),
        (
            "scan-all",
            {"scope": "project", "scan_all": True, "limit": 1},
            [("other-project", False)],
            {"other-project": 1},
        ),
        (
            "truncating-slice",
            {"limit": 2},
            [("global-new", False), ("global-old", False)],
            {"global-new": 1, "global-old": 1},
        ),
    ],
)
def test_default_recall_cells(extended, cell, kwargs, expected, deltas, search_mode, monkeypatch):
    service, engine, factory = extended
    with factory() as db:
        before = {r.key: r.access_count for r in db.query(MemoryMetadataModel)}
        assert db.query(MemoryMetadataModel).filter_by(scope="global").count() == 4
    args = {"scope": "global", "search_mode": search_mode, "terminal_context": CTX, **kwargs}
    if search_mode == "bm25":
        _guard_distinct_bm25_matches(monkeypatch)
        query, expected, deltas = BM25_CELLS[cell]
        args["query"] = query
    rows = asyncio.run(service.recall(**args))
    assert [(m.key, m.is_related) for m in rows] == expected
    assert [
        (
            m.key,
            m.scope,
            m.scope_id,
            m.source_kind,
            m.content,
            m.updated_at.isoformat(),
            m.memory_type,
            m.is_related,
        )
        for m in rows
    ] == [(key, *NATIVE_ROWS[key], related) for key, related in expected]
    assert all(getattr(m, "lifecycle_status", None) is None for m in rows)
    with factory() as db:
        assert {
            r.key: r.access_count - before[r.key]
            for r in db.query(MemoryMetadataModel)
            if r.access_count != before[r.key]
        } == deltas


MCP_PIN = {
    "success": True,
    "memories": [
        {
            "key": "global-new",
            "content": "pin pin pin pin pin pin pin pin body",
            "memory_type": "project",
            "scope": "global",
            "tags": "",
            "file_path": "<ROOT>/global/wiki/global/global-new.md",
            "source_kind": "native",
            "source_path": None,
            "indexed_at": None,
            "index_freshness": None,
            "content_truncated": False,
            "updated_at": "2025-12-30T00:00:00Z",
        },
        {
            "key": "global-old",
            "content": "pin body",
            "memory_type": "project",
            "scope": "global",
            "tags": "",
            "file_path": "<ROOT>/global/wiki/global/global-old.md",
            "source_kind": "native",
            "source_path": None,
            "indexed_at": None,
            "index_freshness": None,
            "content_truncated": False,
            "updated_at": "2025-12-29T00:00:00Z",
        },
    ],
}
INTERNAL_PIN = {
    "memories": [
        {
            "id": "00000000-0000-0000-0000-000000000001",
            "key": "global-new",
            "memory_type": "project",
            "scope": "global",
            "scope_id": None,
            "file_path": "<ROOT>/global/wiki/global/global-new.md",
            "tags": "",
            "source_provider": None,
            "source_terminal_id": None,
            "created_at": "2025-12-30T00:00:00Z",
            "updated_at": "2025-12-30T00:00:00Z",
            "access_count": 0,
            "last_compiled_at": None,
            "related_keys": None,
            "content": "pin pin pin pin pin pin pin pin body",
            "source_kind": "native",
            "source_path": None,
            "indexed_at": None,
            "index_freshness": None,
            "content_truncated": False,
            "token_estimate": None,
        },
        {
            "id": "00000000-0000-0000-0000-000000000002",
            "key": "global-old",
            "memory_type": "project",
            "scope": "global",
            "scope_id": None,
            "file_path": "<ROOT>/global/wiki/global/global-old.md",
            "tags": "",
            "source_provider": None,
            "source_terminal_id": None,
            "created_at": "2025-12-29T00:00:00Z",
            "updated_at": "2025-12-29T00:00:00Z",
            "access_count": 0,
            "last_compiled_at": None,
            "related_keys": None,
            "content": "pin body",
            "source_kind": "native",
            "source_path": None,
            "indexed_at": None,
            "index_freshness": None,
            "content_truncated": False,
            "token_estimate": None,
        },
    ]
}


@pytest.mark.parametrize("surface", ["mcp", "internal"])
def test_default_serialized_dict_and_json(extended, monkeypatch, surface):
    service, engine, _ = extended
    if surface == "mcp":
        from cli_agent_orchestrator.mcp_server import server

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.memory_service.MemoryService", lambda *a, **kw: service
        )
        monkeypatch.setattr(server, "_get_terminal_context_from_env", lambda: CTX)
        tool = getattr(server.memory_recall, "fn", server.memory_recall)
        actual = asyncio.run(
            tool(
                query=None,
                scope="global",
                memory_type=None,
                limit=1,
                search_mode="metadata",
                sort_by="recency",
                include_related=True,
            )
        )
        serialized = json.dumps(actual)
        expected = MCP_PIN
    else:
        from cli_agent_orchestrator.api import main

        monkeypatch.setattr(main, "_get_memory_service", lambda: service)
        response = TestClient(main.app, base_url="http://localhost").post(
            "/internal/memory/recall",
            json={
                "scope": "global",
                "limit": 1,
                "search_mode": "metadata",
                "include_related": True,
                "terminal_context": CTX,
            },
        )
        assert response.status_code == 200, response.text
        actual = response.json()
        serialized = response.text
        expected = INTERNAL_PIN
    serialized = serialized.replace(str(service.base_dir), "<ROOT>")
    normalized = json.loads(json.dumps(actual).replace(str(service.base_dir), "<ROOT>"))
    assert normalized == expected
    assert json.loads(serialized) == expected
    # Preserve recursive key order as well as primitive JSON types.
    assert json.dumps(json.loads(serialized), separators=(",", ":")) == json.dumps(
        expected, separators=(",", ":")
    )
    assert _row(engine, "global-new").access_count == 8
    assert _row(engine, "global-old").access_count == 3


@pytest.mark.parametrize("profile", ["developer", "memory_manager"])
@pytest.mark.parametrize("related", [False, True])
def test_default_vault_recall(
    tmp_path, monkeypatch, defaults, isolated_memory_db, profile, related
):
    from cli_agent_orchestrator.services.vault import reconcile as rm

    replace = rm._replace_vault_edges
    service, factory, root, config = _injectable_renderer(tmp_path, monkeypatch)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    monkeypatch.setattr(rm, "_replace_vault_edges", replace)
    folder = root / "Projects" / "CAO Design"
    (folder / "Replacement.md").write_text(
        "---\ncao:\n  key: replacement\n  links:\n    - type: supersedes\n      to: design\n    - type: relates_to\n      to: notes\n---\nReplacement kafka streams"
    )
    (folder / "Notes.md").write_text("---\ncao:\n  key: notes\n---\nNotes kafka streams")
    (folder / "Design.md").write_text("---\ncao:\n  key: design\n---\nDesign kafka streams")
    for i, name in enumerate(["Design.md", "Replacement.md", "Notes.md"]):
        os.utime(folder / name, (1767225600 + i * 86400, 1767225600 + i * 86400))
    rm.reconcile(config.vaults[0], apply=True, run_id="extended-pin")
    with factory() as db:
        assert (
            db.query(MemoryRelationshipModel)
            .filter_by(type="supersedes", status="active", target_key="design")
            .count()
            == 1
        )
    context = {
        "terminal_id": "worker",
        "session_name": "renderer-session",
        "agent_profile": profile,
        "provider": "claude_code",
        "cwd": str(tmp_path / "project"),
    }
    rows = asyncio.run(
        service.recall(
            query="Replacement" if related else None,
            scope="project",
            search_mode="metadata",
            limit=1 if related else 3,
            include_related=related,
            terminal_context=context,
        )
    )
    replacement = (
        "replacement",
        "project",
        "fixture-project",
        "vault",
        "Projects/CAO Design/Replacement.md",
        "Replacement kafka streams",
        "2026-01-02T00:00:00+00:00",
        False,
    )
    expected = (
        [
            replacement,
            (
                "notes",
                "project",
                "fixture-project",
                "vault",
                "Projects/CAO Design/Notes.md",
                "Notes kafka streams",
                "2026-01-03T00:00:00+00:00",
                True,
            ),
        ]
        if related
        else [
            (
                "notes",
                "project",
                "fixture-project",
                "vault",
                "Projects/CAO Design/Notes.md",
                "Notes kafka streams",
                "2026-01-03T00:00:00+00:00",
                False,
            ),
            replacement,
            (
                "design",
                "project",
                "fixture-project",
                "vault",
                "Projects/CAO Design/Design.md",
                "Design kafka streams",
                "2026-01-01T00:00:00+00:00",
                False,
            ),
        ]
    )
    assert [
        (
            m.key,
            m.scope,
            m.scope_id,
            m.source_kind,
            m.source_path,
            m.content,
            m.updated_at.isoformat(),
            m.is_related,
        )
        for m in rows
    ] == expected
