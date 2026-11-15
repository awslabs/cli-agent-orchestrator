"""#1329 P3: opt-in BM25-first recall through ``memory.ranking.default_sort``."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import settings_service
from cli_agent_orchestrator.services.memory_scoring import validate_default_sort
from cli_agent_orchestrator.services.memory_service import MemoryService


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def svc(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'memory.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    return MemoryService(base_dir=tmp_path, db_engine=engine)


def _set_updated_at(svc, key, stamp):
    session = sessionmaker(bind=svc._db_engine)
    with session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope="global").one()
        row.updated_at = stamp
        path = Path(row.file_path)
        db.commit()
    path.write_text(
        re.sub(
            r"## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
            f"## {stamp.strftime('%Y-%m-%dT%H:%M:%SZ')}",
            path.read_text(encoding="utf-8"),
        ),
        encoding="utf-8",
    )


def _seed(svc):
    for key, content in (
        ("orchid-strong", "orchid orchid orchid orchid reference"),
        ("orchid-new", "orchid status note"),
        ("other", "unrelated reference"),
    ):
        _run(
            svc.store(
                content=content,
                key=key,
                scope="global",
                memory_type="reference",
                terminal_context=None,
            )
        )
    _set_updated_at(svc, "orchid-strong", datetime(2020, 1, 1, tzinfo=timezone.utc))
    _set_updated_at(svc, "orchid-new", datetime(2025, 1, 1, tzinfo=timezone.utc))


def _keys(svc, **kwargs):
    return [memory.key for memory in _run(svc.recall(scope="global", limit=10, **kwargs))]


def test_recency_default_sort_keeps_current_query_order(svc):
    _seed(svc)

    assert _keys(svc, query="orchid") == _keys(
        svc, query="orchid", search_mode="hybrid", sort_by="recency"
    )


def test_bm25_default_sort_matches_explicit_bm25_query_recall(svc, monkeypatch):
    _seed(svc)
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"ranking": {"default_sort": "bm25"}},
    )

    configured = _keys(svc, query="orchid")
    assert configured == _keys(svc, query="orchid", search_mode="bm25")
    assert configured != _keys(svc, query="orchid", search_mode="hybrid", sort_by="recency")


def test_bm25_default_sort_keeps_no_query_listing_recency(svc, monkeypatch):
    _seed(svc)
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"ranking": {"default_sort": "bm25"}},
    )

    assert _keys(svc) == _keys(svc, search_mode="hybrid", sort_by="recency")


def test_bm25_default_sort_keeps_whitespace_query_listing_recency(svc, monkeypatch):
    _seed(svc)
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"ranking": {"default_sort": "bm25"}},
    )

    assert _keys(svc, query="   ") == _keys(
        svc, query="   ", search_mode="hybrid", sort_by="recency"
    )


def test_explicit_search_and_sort_win_over_bm25_default_sort(svc, monkeypatch):
    _seed(svc)
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"ranking": {"default_sort": "bm25"}},
    )

    assert _keys(svc, query="orchid", search_mode="metadata") == _keys(
        svc, query="orchid", search_mode="metadata", sort_by="recency"
    )
    assert _keys(svc, query="orchid", sort_by="usage") == _keys(
        svc, query="orchid", search_mode="hybrid", sort_by="usage"
    )


def test_invalid_default_sort_warns_and_falls_back_to_recency(svc, monkeypatch, caplog):
    _seed(svc)
    monkeypatch.setattr(
        settings_service,
        "get_memory_settings",
        lambda: {"ranking": {"default_sort": "not-a-sort"}},
    )

    with caplog.at_level(logging.WARNING):
        actual = _keys(svc, query="orchid")
        repeated = _keys(svc, query="orchid")

    assert actual == _keys(svc, query="orchid", search_mode="hybrid", sort_by="recency")
    assert repeated == actual
    assert caplog.text.count("Invalid memory.ranking.default_sort") == 1
    assert validate_default_sort("not-a-sort") == "recency"
