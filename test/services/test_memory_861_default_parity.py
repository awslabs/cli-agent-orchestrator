"""Default-output pins for #861, with non-tied query fixtures verified on base."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from test.services.test_scoring import _row, _stamp_updated_at
from test.services.vault.test_vault_injection_renderer import _injectable_renderer

import pytest
from click.testing import CliRunner
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
)
from cli_agent_orchestrator.services import memory_relationship_service
from cli_agent_orchestrator.services.memory_service import MemoryService

STAMP = datetime(2026, 1, 1, tzinfo=timezone.utc)
CTX = {"cwd": "/pins/project", "session_name": "pin-session", "caller_scope": "global"}


@pytest.fixture(params=["absent", "explicit"])
def defaults(request, monkeypatch):
    for name, value in {
        "CAO_MEMORY_LIFECYCLE_ENABLED": "false",
        "CAO_MEMORY_NON_ACCESSING_INSPECTION": "false",
        "CAO_MEMORY_EXPLAIN_ENABLED": "true",
    }.items():
        if request.param == "explicit":
            monkeypatch.setenv(name, value)
        else:
            monkeypatch.delenv(name, raising=False)


def _guard_distinct_bm25_matches(monkeypatch):
    """Check the actual ranking batch, not an independently recomputed corpus."""
    import rank_bm25

    original = rank_bm25.BM25Okapi

    def guarded(corpus_tokens, *args, **kwargs):
        model = original(corpus_tokens, *args, **kwargs)
        get_scores = model.get_scores

        def checked(query_tokens):
            scores = get_scores(query_tokens)
            terms = set(query_tokens)
            matched = [
                float(scores[i])
                for i, tokens in enumerate(corpus_tokens)
                if terms.intersection(tokens)
            ]
            assert len(matched) == len(set(matched)), (
                "Parity pin fixture has tied raw BM25 scores; vary query term frequency/length "
                f"instead of relying on filesystem listing order: query={query_tokens}, scores={matched}"
            )
            return scores

        model.get_scores = checked
        return model

    monkeypatch.setattr(rank_bm25, "BM25Okapi", guarded)


def _edge(factory, target, scope="global", scope_id="", source="replacement", origin="human"):
    with factory() as db:
        db.add(
            MemoryRelationshipModel(
                scope=scope,
                scope_id=scope_id,
                source_key=source,
                target_key=target,
                type="supersedes",
                status="active",
                origin=origin,
            )
        )
        db.commit()


@pytest.fixture
def corpus(tmp_path, monkeypatch, defaults):
    engine = create_engine(f"sqlite:///{tmp_path / 'pin.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    svc = MemoryService(base_dir=tmp_path / "native", db_engine=engine)
    monkeypatch.setattr(
        svc,
        "resolve_scope_id",
        lambda scope, ctx: {"project": "pin-project", "session": "pin-session"}.get(scope),
    )
    # Legacy BM25 keeps corpus order on ties; distinct term frequencies remove
    # macOS/Linux filesystem enumeration from these literal parity expectations.
    for key, scope, days, usage, content in [
        ("session-pin", "session", 0, 0, "pin pin body"),
        ("project-pin", "project", 1, 0, "pin pin pin pin body"),
        ("global-new", "global", 2, 7, "pin pin pin pin pin pin pin pin body"),
        ("global-old", "global", 3, 3, "pin body"),
    ]:
        asyncio.run(svc.store(content=content, key=key, scope=scope, terminal_context=CTX))
        _stamp_updated_at(engine, key, scope, STAMP - timedelta(days=days))
        svc._update_index(
            scope,
            svc.resolve_scope_id(scope, CTX),
            key,
            "reference",
            "",
            content,
            (STAMP - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "update",
        )
        with factory() as db:
            db.query(MemoryMetadataModel).filter_by(key=key).update({"access_count": usage})
            db.commit()
    _edge(factory, "global-new", source="global-old")
    return svc, engine, factory


class TestRecallParity:
    @pytest.mark.parametrize("sort_by", ["recency", "score", "usage"])
    @pytest.mark.parametrize("search_mode", ["hybrid", "metadata", "bm25"])
    @pytest.mark.parametrize("scope", [None, "global"])
    def test_recall_pinned(self, corpus, sort_by, search_mode, scope, monkeypatch):
        svc, engine, factory = corpus
        if search_mode == "bm25":
            _guard_distinct_bm25_matches(monkeypatch)
        with factory() as db:
            before = {r.key: r.access_count for r in db.query(MemoryMetadataModel)}
        rows = asyncio.run(
            svc.recall(
                query="pin",
                scope=scope,
                sort_by=sort_by,
                search_mode=search_mode,
                limit=20,
                terminal_context=CTX,
            )
        )
        expected_global = [
            (
                "global-new",
                "global",
                None,
                "native",
                "pin pin pin pin pin pin pin pin body",
                "2025-12-30T00:00:00+00:00",
                False,
            ),
            (
                "global-old",
                "global",
                None,
                "native",
                "pin body",
                "2025-12-29T00:00:00+00:00",
                False,
            ),
        ]
        if sort_by == "score" or (sort_by == "recency" and search_mode == "bm25"):
            expected_global.reverse()
        expected = (
            expected_global
            if scope
            else [
                (
                    "session-pin",
                    "session",
                    "pin-session",
                    "native",
                    "pin pin body",
                    "2026-01-01T00:00:00+00:00",
                    False,
                ),
                (
                    "project-pin",
                    "project",
                    "pin-project",
                    "native",
                    "pin pin pin pin body",
                    "2025-12-31T00:00:00+00:00",
                    False,
                ),
                *expected_global,
            ]
        )
        if scope is None and search_mode == "bm25" and sort_by == "recency":
            # Pure legacy BM25 uses lexical rank before any scope ordering.
            # These literals were observed on bfad0d4b with the distinct corpus.
            expected = [
                (
                    "global-new",
                    "global",
                    None,
                    "native",
                    "pin pin pin pin pin pin pin pin body",
                    "2025-12-30T00:00:00+00:00",
                    False,
                ),
                (
                    "project-pin",
                    "project",
                    "pin-project",
                    "native",
                    "pin pin pin pin body",
                    "2025-12-31T00:00:00+00:00",
                    False,
                ),
                (
                    "session-pin",
                    "session",
                    "pin-session",
                    "native",
                    "pin pin body",
                    "2026-01-01T00:00:00+00:00",
                    False,
                ),
                (
                    "global-old",
                    "global",
                    None,
                    "native",
                    "pin body",
                    "2025-12-29T00:00:00+00:00",
                    False,
                ),
            ]
        assert [
            (
                m.key,
                m.scope,
                m.scope_id,
                m.source_kind,
                m.content,
                m.updated_at.isoformat(),
                m.is_related,
            )
            for m in rows
        ] == expected
        assert {
            k: _row(engine, k, s).access_count - before[k]
            for k, s in [
                ("session-pin", "session"),
                ("project-pin", "project"),
                ("global-new", "global"),
                ("global-old", "global"),
            ]
        } == {
            "session-pin": int(scope is None),
            "project-pin": int(scope is None),
            "global-new": 1,
            "global-old": 1,
        }

    @pytest.mark.parametrize("sort_by", ["recency", "usage", "score"])
    def test_superseded_order_pinned(self, corpus, sort_by):
        svc, _, _ = corpus
        rows = asyncio.run(svc.recall(query="pin", scope="global", sort_by=sort_by))
        assert [m.key for m in rows] == (
            ["global-old", "global-new"] if sort_by == "score" else ["global-new", "global-old"]
        )


class TestInjectionParity:
    def test_native_block_pinned(self, corpus):
        svc, engine, factory = corpus
        asyncio.run(svc.forget("project-pin", scope="project", terminal_context=CTX))
        for i in range(11):
            key = f"cap-{i:02}"
            asyncio.run(
                svc.store(content=f"body {i:02}", key=key, scope="project", terminal_context=CTX)
            )
            _stamp_updated_at(engine, key, "project", STAMP + timedelta(days=11 - i))
            svc._update_index(
                "project",
                "pin-project",
                key,
                "reference",
                "",
                f"body {i:02}",
                (STAMP + timedelta(days=11 - i)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "update",
            )
        with factory() as db:
            db.query(MemoryMetadataModel).filter_by(key="cap-00").update({"related_keys": "cap-10"})
            db.commit()
        _edge(factory, "cap-00", "project", "pin-project", "cap-01")
        assert svc.get_memory_context(CTX) == (
            "<cao-memory>\n## Context from CAO Memory\n"
            "- [session] session-pin: pin pin body\n"
            "- [project] cap-00: body 00\n- [project] cap-01: body 01\n"
            "- [project] cap-02: body 02\n- [project] cap-03: body 03\n"
            "- [project] cap-04: body 04\n- [project] cap-05: body 05\n"
            "- [project] cap-06: body 06\n- [project] cap-07: body 07\n"
            "- [project] cap-08: body 08\n- [project] cap-09: body 09\n"
            "- [project] cap-10 [related]: body 10\n"
            "- [global] global-new: pin pin pin pin pin pin pin pin body\n- [global] global-old: pin body\n</cao-memory>"
        )

    def test_vault_block_pinned(self, tmp_path, monkeypatch, defaults, isolated_memory_db):
        from cli_agent_orchestrator.services.vault import reconcile as reconcile_module

        replace = reconcile_module._replace_vault_edges
        svc, factory, root, config = _injectable_renderer(tmp_path, monkeypatch)
        monkeypatch.setattr(database, "SessionLocal", factory)
        monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
        monkeypatch.setattr(reconcile_module, "_replace_vault_edges", replace)
        source = root / "Projects" / "CAO Design" / "Replacement.md"
        source.write_text(
            "---\ncao:\n  key: replacement\n  links:\n    - type: supersedes\n      to: design\n---\nReplacement"
        )
        for name in ("Design.md", "Replacement.md"):
            os.utime(source.parent / name, (1767225600, 1767225600))
        reconcile_module.reconcile(config.vaults[0], apply=True, run_id="pin-links")
        with factory() as db:
            assert (
                db.query(MemoryRelationshipModel)
                .filter_by(type="supersedes", status="active", target_key="design")
                .count()
                == 1
            )
        assert svc.get_memory_context_for_terminal("worker") == (
            "<cao-memory>\n## Context from CAO Memory\n"
            "- [project] design: Design\n- [project] replacement: Replacement\n</cao-memory>"
        )


class TestInspectionParity:
    @pytest.mark.parametrize("surface", ["list", "show", "http-list", "http-show"])
    def test_operator_reads_bump_access(self, corpus, monkeypatch, surface):
        from cli_agent_orchestrator.api import main
        from cli_agent_orchestrator.cli.commands import memory

        svc, engine, _ = corpus
        monkeypatch.setattr(memory, "_get_memory_service", lambda: svc)
        monkeypatch.setattr(main, "_get_memory_service", lambda: svc)
        before = _row(engine, "global-new").access_count
        if surface == "list":
            result = CliRunner().invoke(memory.list_memories, ["--scope", "global"])
            assert result.exit_code == 0, result.output
        elif surface == "show":
            result = CliRunner().invoke(memory.show, ["global-new", "--scope", "global"])
            assert result.exit_code == 0, result.output
        else:
            client = TestClient(main.app, base_url="http://localhost")
            response = client.get(
                "/memory" if surface == "http-list" else "/memory/global-new",
                params={"scope": "global"},
            )
            assert response.status_code == 200, response.text
        assert _row(engine, "global-new").access_count == before + 1
