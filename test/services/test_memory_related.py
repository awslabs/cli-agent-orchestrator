"""The content-free memory_related read surface."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.services import memory_eligibility
from cli_agent_orchestrator.services.memory_relationship_service import (
    MemoryRelationshipService,
)
from cli_agent_orchestrator.services.memory_service import InvalidRelatedArgument, MemoryService
from cli_agent_orchestrator.services.vault.reader import MEMORY_MANAGER_PROFILE


def _run(coro):
    return asyncio.run(coro)


class _ExcludeTarget:
    name = "fixture.related-policy"

    def is_enabled(self):
        return True

    def excluded(self, request, candidates):
        return {identity: "fixture" for identity in candidates if identity.key == "target"}


class _ExcludeHidden:
    name = "fixture.related-hidden"

    def is_enabled(self):
        return True

    def excluded(self, request, candidates):
        return {identity: "fixture" for identity in candidates if identity.key == "a-hidden"}


@pytest.fixture
def service(tmp_path: Path, isolated_memory_db, monkeypatch) -> MemoryService:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(memory_eligibility, "_SOURCES", {})
    monkeypatch.setattr(settings_service, "is_memory_content_links_enabled", lambda: True)
    return MemoryService(base_dir=tmp_path, db_engine=isolated_memory_db)


def test_related_shape_is_content_free_and_policy_filters_neighbours(service) -> None:
    _run(service.store("secret target body", scope="global", key="target"))
    _run(service.store("Source hook. [[target]]", scope="global", key="source"))

    result = _run(
        service.related(
            "source",
            scope="global",
            terminal_context={"agent_profile": "memory_manager"},
        )
    )
    assert result["success"] is True
    assert result["edges"] == [
        {
            "source_key": "source",
            "target_key": "target",
            "type": "relates_to",
            "origin": "content_link",
            "status": "active",
            "confidence": None,
            "direction": "out",
            "depth": 1,
        }
    ]
    assert result["neighbours"] == [
        {
            "key": "target",
            "kind": "project",
            "hook": "secret target body",
            "status": "active",
        }
    ]
    assert "content" not in result
    assert "proposals" not in result

    memory_eligibility.register_source(_ExcludeTarget())
    filtered = _run(
        service.related(
            "source",
            scope="global",
            terminal_context={"agent_profile": "memory_manager"},
        )
    )
    assert filtered["edges"] == []
    assert filtered["neighbours"] == []


def test_related_not_found_and_ambiguous_scope(service) -> None:
    assert _run(service.related("missing")) == {
        "success": False,
        "error_kind": "not_found",
        "error": "memory not found: missing",
    }
    _run(service.store("global", scope="global", key="same-key"))
    _run(service.store("federated", scope="federated", key="same-key"))

    ambiguous = _run(service.related("same-key"))
    assert ambiguous == {
        "success": False,
        "error_kind": "ambiguous_scope",
        "error": "memory key is present in multiple scopes: same-key",
        "scopes": ["global", "federated"],
    }


def test_memory_related_tool_calls_local_service(monkeypatch) -> None:
    expected = {"success": True, "key": "source", "edges": [], "neighbours": []}
    related = monkeypatch.setattr
    related(MemoryService, "related", lambda self, *args, **kwargs: _async_result(expected))
    related(server, "_get_terminal_context_from_env", lambda: None)

    tool = getattr(server.memory_related, "fn", server.memory_related)
    assert _run(tool("source")) == expected


def test_memory_related_tool_uses_remote_gateway(monkeypatch) -> None:
    from cli_agent_orchestrator.services import memory_gateway

    expected = {"success": True, "key": "remote", "edges": [], "neighbours": []}
    monkeypatch.setattr(memory_gateway, "remote_memory_url", lambda: "http://localhost")

    async def remote(**kwargs):
        assert kwargs["key"] == "source"
        return expected

    monkeypatch.setattr(memory_gateway, "related_memory", remote)
    monkeypatch.setattr(MemoryService, "related", lambda *args, **kwargs: pytest.fail("local read"))
    tool = getattr(server.memory_related, "fn", server.memory_related)
    assert _run(tool("source")) == expected


def test_related_does_not_record_access(service, isolated_memory_db) -> None:
    from cli_agent_orchestrator.clients.database import MemoryMetadataModel

    _run(service.store("target", scope="global", key="target"))
    _run(service.store("source", scope="global", key="source"))
    MemoryRelationshipService(sessionmaker(bind=isolated_memory_db)).create(
        "global", None, "source", "target", "relates_to", "human"
    )
    with sessionmaker(bind=isolated_memory_db)() as db:
        before = {row.key: row.access_count for row in db.query(MemoryMetadataModel).all()}
    _run(service.related("source", scope="global"))
    with sessionmaker(bind=isolated_memory_db)() as db:
        after = {row.key: row.access_count for row in db.query(MemoryMetadataModel).all()}
    assert after == before


def test_vault_neighbour_hook_uses_latest_entry_body(
    service, monkeypatch, isolated_memory_db
) -> None:
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("source", scope="global", key="source"))
    MemoryRelationshipService(sessionmaker(bind=isolated_memory_db)).create(
        "global", None, "source", "target", "relates_to", "human"
    )
    original = service.read_exact_key

    async def vault_read(key, **kwargs):
        memory = await original(key, **kwargs)
        if key == "target" and memory:
            return memory.model_copy(
                update={
                    "source_kind": "vault",
                    "content": "## 2026-11-23T10:28:26Z\nold entry\n\n## 2026-11-23T10:29:26Z\n"
                    + "new entry " * 30,
                }
            )
        return memory

    monkeypatch.setattr(service, "read_exact_key", vault_read)
    assert (
        _run(service.related("source", scope="global"))["neighbours"][0]["hook"]
        == ("new entry " * 30).strip()[:160]
    )


@pytest.mark.parametrize("scope", ["agent", "federated"])
def test_memory_manager_cannot_read_private_scope_related_locally(service, scope) -> None:
    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    _run(service.store("target hook", scope=scope, key=f"{scope}-target", terminal_context=context))
    _run(
        service.store(
            f"[[{scope}-target]]",
            scope=scope,
            key=f"{scope}-source",
            terminal_context=context,
        )
    )

    result = _run(
        service.related(
            f"{scope}-source",
            scope=scope,
            terminal_context=context,
        )
    )

    assert result["success"] is False
    assert result["error_kind"] == "not_found"


@pytest.mark.parametrize("scope", ["agent", "federated"])
def test_memory_manager_cannot_read_private_scope_related_via_internal_route(
    service, scope, monkeypatch
) -> None:
    context = {"agent_profile": MEMORY_MANAGER_PROFILE}
    _run(service.store("target hook", scope=scope, key=f"{scope}-target", terminal_context=context))
    _run(
        service.store(
            f"[[{scope}-target]]",
            scope=scope,
            key=f"{scope}-source",
            terminal_context=context,
        )
    )
    monkeypatch.setattr(api_main, "_get_memory_service", lambda: service)
    body = api_main.InternalMemoryRelatedRequest(
        key=f"{scope}-source",
        scope=scope,
        terminal_context=api_main.InternalMemoryContext(**context),
    )

    result = _run(api_main.internal_memory_related(body, _scopes=[]))
    if hasattr(result, "body"):
        result = json.loads(result.body)

    assert result["success"] is False
    assert result["error_kind"] == "not_found"


def test_related_keeps_superseded_memories_visible_for_explicit_callers(
    service, isolated_memory_db, monkeypatch
) -> None:
    from cli_agent_orchestrator.services import settings_service

    monkeypatch.setattr(settings_service, "is_memory_lifecycle_enabled", lambda: True)
    _run(service.store("old hook", scope="global", key="old-fact"))
    _run(service.store("new hook", scope="global", key="new-fact"))
    relationships = MemoryRelationshipService(sessionmaker(bind=isolated_memory_db))
    relationships.create(
        "global",
        None,
        "new-fact",
        "old-fact",
        "supersedes",
        "human",
    )

    from_new = _run(service.related("new-fact", scope="global", types=["supersedes"]))
    from_old = _run(service.related("old-fact", scope="global", types=["supersedes"]))

    assert from_new["neighbours"] == [
        {
            "key": "old-fact",
            "kind": "project",
            "hook": "old hook",
            "status": "superseded",
        }
    ]
    assert from_old["success"] is True
    assert from_old["key"] == "old-fact"


def test_related_keeps_all_edge_types_to_one_neighbour(service, isolated_memory_db) -> None:
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("source", scope="global", key="source"))
    relationships = MemoryRelationshipService(sessionmaker(bind=isolated_memory_db))
    relationships.create("global", None, "source", "target", "relates_to", "human")
    relationships.create("global", None, "source", "target", "supersedes", "human")

    result = _run(service.related("source", scope="global", limit=1))

    assert {edge["type"] for edge in result["edges"]} == {"relates_to", "supersedes"}
    assert [neighbour["key"] for neighbour in result["neighbours"]] == ["target"]


def test_hidden_middle_cannot_bridge_visible_memories(service, isolated_memory_db) -> None:
    for key in ("source", "a-hidden", "target"):
        _run(service.store(key, scope="global", key=key))
    relationships = MemoryRelationshipService(sessionmaker(bind=isolated_memory_db))
    relationships.create("global", None, "source", "a-hidden", "relates_to", "human")
    relationships.create("global", None, "a-hidden", "target", "relates_to", "human")
    memory_eligibility.register_source(_ExcludeHidden())

    result = _run(
        service.related(
            "source",
            scope="global",
            depth=2,
            terminal_context={"agent_profile": MEMORY_MANAGER_PROFILE},
        )
    )

    assert result["edges"] == []
    assert result["neighbours"] == []


def test_related_ignores_rejected_and_deleted_edges(service, isolated_memory_db) -> None:
    for key in ("source", "rejected", "deleted"):
        _run(service.store(key, scope="global", key=key))
    relationships = MemoryRelationshipService(sessionmaker(bind=isolated_memory_db))
    rejected = relationships.create("global", None, "source", "rejected", "relates_to", "human")
    deleted = relationships.create("global", None, "source", "deleted", "relates_to", "human")
    relationships.reject(rejected.id)
    relationships.soft_delete(deleted.id)

    result = _run(service.related("source", scope="global"))

    assert result["edges"] == []
    assert result["neighbours"] == []


def test_related_hook_is_clipped_to_160_characters(service) -> None:
    _run(service.store("x" * 200, scope="global", key="target"))
    _run(service.store("[[target]]", scope="global", key="source"))

    result = _run(service.related("source", scope="global"))

    assert result["neighbours"][0]["hook"] == "x" * 160


def test_hidden_neighbour_does_not_consume_visible_limit(service, isolated_memory_db) -> None:
    for key in ("source", "a-hidden", "b-visible"):
        _run(service.store(key, scope="global", key=key))
    relationships = MemoryRelationshipService(sessionmaker(bind=isolated_memory_db))
    relationships.create("global", None, "source", "a-hidden", "relates_to", "human")
    relationships.create("global", None, "source", "b-visible", "relates_to", "human")
    memory_eligibility.register_source(_ExcludeHidden())

    result = _run(
        service.related(
            "source",
            scope="global",
            limit=1,
            terminal_context={"agent_profile": MEMORY_MANAGER_PROFILE},
        )
    )

    assert [neighbour["key"] for neighbour in result["neighbours"]] == ["b-visible"]


def test_related_uses_exact_reads_instead_of_full_recall(service, monkeypatch) -> None:
    _run(service.store("target", scope="global", key="target"))
    _run(service.store("[[target]]", scope="global", key="source"))

    async def fail_recall(*args, **kwargs):
        raise AssertionError("related must not scan the full store")

    monkeypatch.setattr(service, "recall", fail_recall)
    assert _run(service.related("source", scope="global"))["success"] is True


def test_internal_related_maps_validation_and_read_errors(monkeypatch) -> None:
    class Stub:
        async def related(self, key, **kwargs):
            if key == "bad":
                raise InvalidRelatedArgument("direction")
            if key == "other":
                raise ValueError("secret detail that must not be echoed")
            return {
                "success": False,
                "error_kind": "not_found",
                "error": f"memory not found: {key}",
            }

    monkeypatch.setattr(api_main, "_get_memory_service", Stub)
    missing = _run(
        api_main.internal_memory_related(
            api_main.InternalMemoryRelatedRequest(key="missing"),
            _scopes=[],
        )
    )
    assert missing.status_code == 404

    invalid = _run(
        api_main.internal_memory_related(
            api_main.InternalMemoryRelatedRequest(key="bad"),
            _scopes=[],
        )
    )
    assert invalid.status_code == 422
    assert json.loads(invalid.body) == {
        "success": False,
        "error_kind": "invalid_argument",
        "error": "direction must be one of: in, out, both",
    }
    other = _run(
        api_main.internal_memory_related(
            api_main.InternalMemoryRelatedRequest(key="other"),
            _scopes=[],
        )
    )
    assert other.status_code == 422
    assert json.loads(other.body)["error"] == "invalid related-memory argument"


async def _async_result(value):
    return value


def test_remote_related_end_to_end_through_the_gateway_and_the_route(service, monkeypatch) -> None:
    """The gateway's _post talks to the real route: success, 404, 409 and 422 keep their reasons."""
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.services import memory_gateway

    _run(service.store("target body", scope="global", key="target"))
    _run(service.store("Source hook. [[target]]", scope="global", key="source"))
    _run(service.store("first copy", scope="global", key="dup"))
    _run(service.store("second copy", scope="federated", key="dup"))
    client = TestClient(api_main.app, base_url="http://localhost")
    monkeypatch.setenv("CAO_MEMORY_API_URL", "http://localhost")
    monkeypatch.setattr(api_main, "_get_memory_service", lambda: service)
    monkeypatch.setattr(
        memory_gateway.requests,
        "post",
        lambda url, json=None, headers=None, timeout=None: client.post(
            url.removeprefix("http://localhost"), json=json
        ),
    )

    ok = memory_gateway._post("/internal/memory/related", {"key": "source"})
    missing = memory_gateway._post("/internal/memory/related", {"key": "missing"})
    ambiguous = memory_gateway._post("/internal/memory/related", {"key": "dup"})
    invalid = memory_gateway._post(
        "/internal/memory/related", {"key": "source", "direction": "sideways"}
    )

    assert [edge["key"] for edge in ok["neighbours"]] == ["target"]
    assert missing["error_kind"] == "not_found"
    assert ambiguous["error_kind"] == "ambiguous_scope"
    assert invalid["error_kind"] == "invalid_argument"
    assert "direction must be one of" in invalid["error"]

    for payload, message in (
        ({"key": "source", "scope": "bogus-SECRET"}, "invalid memory scope"),
        ({"key": "..."}, "key is empty after sanitization"),
    ):
        response = client.post("/internal/memory/related", json=payload)
        assert response.status_code == 422
        assert response.json() == {
            "success": False,
            "error_kind": "invalid_argument",
            "error": message,
        }
        assert "SECRET" not in response.text

    response = client.post("/internal/memory/related", json={"key": "dup"})
    assert response.status_code == 409


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"direction": "sideways-SECRET"}, "direction must be one of: in, out, both"),
        ({"types": ["bogus-SECRET"]}, "invalid relationship type"),
        ({"limit": 101}, "limit must be between 1 and 100"),
    ],
)
def test_related_rejects_bad_arguments_with_fixed_messages_that_never_echo_the_value(
    service, kwargs, message
) -> None:
    _run(service.store("a note", scope="global", key="source"))

    with pytest.raises(InvalidRelatedArgument) as raised:
        _run(service.related("source", **kwargs))

    assert str(raised.value) == message
    assert "SECRET" not in str(raised.value)


def test_internal_related_422_returns_only_the_fixed_literal(service, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    _run(service.store("a note", scope="global", key="source"))
    monkeypatch.setattr(api_main, "_get_memory_service", lambda: service)
    client = TestClient(api_main.app, base_url="http://localhost")

    bad_direction = client.post(
        "/internal/memory/related", json={"key": "source", "direction": "sideways-SECRET"}
    )
    bad_type = client.post(
        "/internal/memory/related", json={"key": "source", "types": ["bogus-SECRET"]}
    )

    assert bad_direction.status_code == bad_type.status_code == 422
    assert bad_direction.json() == {
        "success": False,
        "error_kind": "invalid_argument",
        "error": "direction must be one of: in, out, both",
    }
    assert bad_type.json()["error"] == "invalid relationship type"
    assert "SECRET" not in bad_direction.text + bad_type.text
