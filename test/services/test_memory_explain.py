"""Explain uses recall's pools/ranking but never records operator access."""

import json
from test.services.test_scoring import (
    _run,
    db_engine,
    hybrid_starvation_corpus,
    svc,
)

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import MemoryMetadataModel
from cli_agent_orchestrator.services import settings_service


@pytest.fixture
def explained(svc, hybrid_starvation_corpus):
    return svc, dict(
        query="kafka streams", scope="global", limit=1, terminal_context=hybrid_starvation_corpus
    )


def _counts(engine):
    with sessionmaker(bind=engine)() as db:
        return {r.key: (r.access_count, r.last_accessed_at) for r in db.query(MemoryMetadataModel)}


def test_reports_recovered_old_exact_match(explained):
    service, args = explained
    result = _run(service.recall_explain(**args))
    assert result["schema"] == "cao.memory.explain/1"
    assert result["candidate_limit"] == 50
    assert result["pool_size"] == result["all_term_count"] == 2
    assert result["partial_fill_count"] == 0
    assert [(c["key"], c["recovered"]) for c in result["candidates"]] == [
        ("strong-old", True),
        ("weak-new", False),
    ]


def test_relevance_rank_matches_recall_relevance_order(explained):
    service, args = explained
    result = _run(service.recall_explain(**args))
    by_rank = sorted(
        (c for c in result["candidates"] if c["relevance_rank"] is not None),
        key=lambda c: c["relevance_rank"],
    )
    reported = [c["key"] for c in by_rank[: args["limit"]]]
    actual = [m.key for m in _run(service.recall(**args, sort_by="relevance"))]
    assert reported == actual


def test_recency_rank_matches_default_recall(explained):
    service, args = explained
    result = _run(service.recall_explain(**args))
    by_rank = sorted(
        (c for c in result["candidates"] if c["recency_rank"] is not None),
        key=lambda c: c["recency_rank"],
    )
    assert [c["key"] for c in by_rank] == [m.key for m in _run(service.recall(**args))]


def test_never_increments_access(explained, db_engine, monkeypatch):
    service, args = explained
    before = _counts(db_engine)

    def forbidden(*args, **kwargs):
        pytest.fail("explain must not finish recall or increment/expand")

    for name in ("_increment_access_count", "_finish_recall", "_expand_related"):
        monkeypatch.setattr(service, name, forbidden)
    assert _run(service.recall_explain(**args))["candidates"]
    assert _counts(db_engine) == before


def test_does_not_change_later_recall(explained, db_engine):
    service, args = explained
    before = [service._identity(m) for m in _run(service.recall(**args))]
    counts = _counts(db_engine)
    _run(service.recall_explain(**args))
    assert _counts(db_engine) == counts
    assert [service._identity(m) for m in _run(service.recall(**args))] == before


def test_candidate_keys_are_exactly_the_schema(explained):
    service, args = explained
    result = _run(service.recall_explain(**args))
    assert set(result) == {
        "schema",
        "query",
        "search_mode",
        "limit",
        "candidate_limit",
        "pool_size",
        "all_term_count",
        "partial_fill_count",
        "recency_floor",
        "recency_scale_days",
        "overlap_at_limit",
        "candidates",
        "eligibility_errors",
    }
    expected = {
        "key",
        "scope",
        "scope_id",
        "source_kind",
        "tier",
        "lexical",
        "recency_factor",
        "relevance",
        "relevance_rank",
        "recency_rank",
        "recovered",
        "superseded",
        "injection",
        "exclusion_reasons",
    }
    assert all(set(c) == expected for c in result["candidates"])
    assert len(result["candidates"]) <= 2 * args["limit"]


def test_no_content_tags_paths_or_raw_bm25_leak(explained, db_engine):
    service, args = explained
    _run(
        service.store(
            content="kafka streams CONTENT_SENTINEL",
            key="sentinel",
            scope="global",
            tags="TAG_SENTINEL",
            terminal_context=args["terminal_context"],
        )
    )
    args = dict(args, limit=10)
    memories = _run(service._metadata_recall(query=args["query"], scope="global", limit=10))
    assert any("CONTENT_SENTINEL" in m.content and "TAG_SENTINEL" in m.tags for m in memories)
    output = json.dumps(_run(service.recall_explain(**args)))
    for sentinel in (
        "CONTENT_SENTINEL",
        "TAG_SENTINEL",
        str(service.base_dir),
        "file_path",
        "source_path",
        "updated_at",
        "access_count",
        "raw_bm25",
    ):
        assert sentinel not in output


@pytest.mark.parametrize("limit,expected", [(1, 0.0), (2, 1.0)])
def test_overlap_at_limit(explained, limit, expected):
    service, args = explained
    assert _run(service.recall_explain(**dict(args, limit=limit)))["overlap_at_limit"] == expected


def test_empty_pool_has_null_overlap(svc):
    result = _run(svc.recall_explain(query="nonexistent", scope="global"))
    assert result["candidates"] == []
    assert result["overlap_at_limit"] is None


def test_disabled_by_setting(explained):
    service, args = explained
    settings_service.set_memory_setting("explain_enabled", False)
    assert _run(service.recall_explain(**args)) is None


def test_disabled_by_env(explained, monkeypatch):
    service, args = explained
    monkeypatch.setenv("CAO_MEMORY_EXPLAIN_ENABLED", "false")
    assert _run(service.recall_explain(**args)) is None


def test_memory_disabled(explained, monkeypatch):
    service, args = explained
    monkeypatch.setenv("CAO_MEMORY_ENABLED", "false")
    assert _run(service.recall_explain(**args)) is None


@pytest.mark.parametrize("query", ["", "   "])
def test_empty_query_rejected(svc, query):
    with pytest.raises(ValueError, match="query"):
        _run(svc.recall_explain(query=query))


def test_reports_exact_values_used_by_ordering(explained, monkeypatch):
    service, args = explained
    original = service._rank_relevance
    captured = []

    def rank(*args, **kwargs):
        scores = original(*args, **kwargs)
        captured.append(scores)
        return scores

    monkeypatch.setattr(service, "_rank_relevance", rank)
    result = _run(service.recall_explain(**args))
    assert len(captured) == 1
    for candidate in result["candidates"]:
        identity = (
            candidate["key"],
            candidate["scope"],
            candidate["scope_id"],
            candidate["source_kind"],
        )
        score = captured[0][identity]
        assert candidate["lexical"] == round(score.lexical, 4)
        assert candidate["recency_factor"] == round(score.recency_factor, 4)
        assert candidate["relevance"] == round(score.relevance, 4)


def test_default_only_candidate_has_no_relevance_rank(explained, monkeypatch):
    service, args = explained
    original = service._recall_pool

    async def pool(*args, **kwargs):
        result = await original(*args, **kwargs)
        if result.effective_sort == "relevance":
            result.rows = [m for m in result.rows if m.key != "weak-new"]
        return result

    monkeypatch.setattr(service, "_recall_pool", pool)
    result = _run(service.recall_explain(**args))
    default_only = next(c for c in result["candidates"] if c["key"] == "weak-new")
    assert default_only["recency_rank"] == 1
    assert default_only["relevance_rank"] is None
    assert default_only["lexical"] == default_only["relevance"] == 0.0
    assert default_only["recovered"] is False


def test_negative_normalized_lexical_is_zero(explained, monkeypatch):
    service, args = explained
    args = dict(args, limit=2)
    monkeypatch.setattr(
        service,
        "_bm25_relevance",
        lambda *a, **kw: {
            ("strong-old", "global", None, "native"): 1.0,
            ("weak-new", "global", None, "native"): -0.5,
        },
    )
    result = _run(service.recall_explain(**args))
    weak = next(c for c in result["candidates"] if c["key"] == "weak-new")
    assert weak["lexical"] == weak["relevance"] == 0.0
