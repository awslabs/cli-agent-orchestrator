"""Lifecycle eligibility across native, vault, related and curator paths."""

from datetime import datetime, timedelta, timezone

from test.services.test_scoring import (  # isort: skip  (per-name noqa below; see the comment)
    _ctx,
    _run,
    _stamp_updated_at,
    db_engine,  # noqa: F401  # pytest fixture, injected by name
    svc,  # noqa: F401  # pytest fixture, injected by name
)
from test.services.vault.test_vault_injection_renderer import _counter_values, _injectable_renderer

import pytest
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import MemoryMetadataModel
from cli_agent_orchestrator.services import memory_relationship_service


class FixtureSource:
    name = "fixture.source"

    def __init__(self, keys=("blocked",), enabled=True, reason="promoted"):
        self.keys = set(keys)
        self.enabled = enabled
        self.reason = reason
        self.calls = []
        self.requests = []

    def is_enabled(self):
        return self.enabled

    def excluded(self, request, candidates):
        self.requests.append(request)
        self.calls.append(list(candidates))
        return {identity: self.reason for identity in candidates if identity.key in self.keys}


@pytest.fixture
def registry(monkeypatch):
    from cli_agent_orchestrator.services import memory_eligibility as eligibility

    monkeypatch.setattr(eligibility, "_SOURCES", {})
    return eligibility


@pytest.fixture
def seeded(svc, db_engine, monkeypatch):
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    ctx = _ctx(caller_scope="global")
    now = datetime.now(timezone.utc)
    for key, days in [("blocked", 0), ("survivor", 1), ("related", 2)]:
        _run(
            svc.store(key=key, content="kafka streams " + key, scope="global", terminal_context=ctx)
        )
        stamp = now - timedelta(days=days)
        _stamp_updated_at(db_engine, key, "global", stamp)
        svc._update_index(
            "global",
            None,
            key,
            "reference",
            "",
            "kafka streams " + key,
            stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "update",
        )
    return svc, factory, ctx


def test_no_enabled_source_returns_none(registry):
    registry.register_source(FixtureSource(enabled=False))
    assert (
        registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
        is None
    )


@pytest.mark.parametrize(
    "path",
    [
        "native",
        "vault",
        "related",
        "curated_metadata",
        "curated_bm25_topup",
        "curated_widened",
        "curated_related",
    ],
)
def test_fixture_source_excludes_key_on_every_path(
    path, seeded, registry, tmp_path, monkeypatch, isolated_memory_db
):
    service, factory, ctx = seeded
    blocked = "related" if path in ("related", "curated_related") else "blocked"
    source = FixtureSource(keys=(blocked,))
    registry.register_source(source)
    if path == "vault":
        (tmp_path / "renderer").mkdir()
        service, factory, root, config = _injectable_renderer(tmp_path / "renderer", monkeypatch)
        source.keys = {"design"}
        block = service.get_memory_context_for_terminal("worker")
        assert "design: Design" not in block
        assert source.calls
        return
    if path in ("related", "curated_related"):
        with factory() as db:
            db.query(MemoryMetadataModel).filter_by(key="blocked").update(
                {"related_keys": "related"}
            )
            db.commit()
        if path == "related":
            index = service.get_index_path("global", None)
            index.write_text(
                "\n".join(
                    line for line in index.read_text().splitlines() if "[related]" not in line
                )
                + "\n"
            )
    if path == "native" or path == "related":
        block = service.get_memory_context(ctx)
        assert f"] {blocked}" not in block
        assert "survivor: kafka streams survivor" in block
    else:
        ctx = dict(ctx, agent_profile="memory_manager")
        if path == "curated_bm25_topup":
            from pathlib import Path

            with factory() as db:
                related_path = Path(
                    db.query(MemoryMetadataModel).filter_by(key="related").one().file_path
                )
            related_path.write_text(
                related_path.read_text().replace("kafka streams related", "orchard gardening")
            )
            service.get_index_path("global", None).write_text("")
        args = dict(query="kafka streams", scope="global", terminal_context=ctx, limit=1)
        if path == "curated_metadata":
            args["search_mode"] = "metadata"
        elif path == "curated_widened":
            args["sort_by"] = "score"
        elif path == "curated_related":
            args["include_related"] = True
        rows = _run(service.recall(**args))
        assert blocked not in [m.key for m in rows]
        assert rows
        if path != "curated_related":
            assert rows[0].key == "survivor"
    assert source.calls


def test_exclusion_precedes_per_scope_cap(seeded, registry):
    service, factory, ctx = seeded
    for i in range(11):
        key = f"entry-{i:02}"
        _run(service.store(key=key, content=key, scope="global", terminal_context=ctx))
        service._update_index(
            "global", None, key, "reference", "", key, f"2027-01-{12-i:02}T00:00:00Z", "update"
        )
    registry.register_source(FixtureSource(keys=("entry-00",)))
    block = service.get_memory_context(ctx)
    assert "entry-00:" not in block
    assert "entry-10:" in block
    assert block.count("- [global]") == 10


def test_lazy_chunking_evaluates_one_chunk_when_nothing_excluded(seeded, registry, monkeypatch):
    service, factory, ctx = seeded
    for scope in ("session", "project", "global"):
        for i in range(11):
            _run(
                service.store(
                    key=f"chunk-{scope}-{i}", content="body", scope=scope, terminal_context=ctx
                )
            )
    source = FixtureSource(keys=())
    registry.register_source(source)
    calls = []
    original = registry.EligibilityCheck.reasons

    def reasons(self, candidates):
        calls.append(list(candidates))
        return original(self, candidates)

    monkeypatch.setattr(registry.EligibilityCheck, "reasons", reasons)
    service.get_memory_context(ctx)
    assert len(calls) == len(source.calls) == 3
    assert [(batch[0].scope, len(batch)) for batch in calls] == [
        ("session", 10),
        ("project", 10),
        ("global", 10),
    ]


def test_reasons_memoize_identity_across_paths(registry):
    source = FixtureSource()
    registry.register_source(source)
    check = registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
    identity = registry.MemoryIdentity("blocked", "global", None, "native")
    assert check.reasons([identity]) == check.reasons([identity]) == {identity: ("promoted",)}
    assert len(source.calls) == 1


def test_duplicate_source_name_rejected(registry):
    registry.register_source(FixtureSource())
    with pytest.raises(ValueError, match="duplicate"):
        registry.register_source(FixtureSource())


@pytest.mark.parametrize("invalid", ["bad reason", "secret\nbody", 123])
def test_invalid_reason_token_fails_open(registry, invalid, caplog):
    registry.register_source(FixtureSource(reason=invalid))
    check = registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
    identity = registry.MemoryIdentity("blocked", "global", None, "native")
    assert check.reasons([identity]) == {}
    assert check.errors == ["fixture.source"]
    assert "blocked" not in caplog.text and "secret" not in caplog.text


def test_failing_source_fails_open_with_content_free_warning(registry, caplog):
    class Broken(FixtureSource):
        def excluded(self, request, candidates):
            raise RuntimeError("blocked secret body")

    registry.register_source(Broken())
    check = registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
    assert check.reasons([registry.MemoryIdentity("blocked", "global", None, "native")]) == {}
    assert check.errors == ["fixture.source"]
    assert "memory_eligibility_failed_open" in caplog.text
    assert "blocked" not in caplog.text and "secret body" not in caplog.text


def test_failing_source_does_not_disable_others(registry):
    class Broken(FixtureSource):
        name = "broken"

        def excluded(self, request, candidates):
            raise OSError("unavailable")

    registry.register_source(Broken())
    registry.register_source(FixtureSource())
    identity = registry.MemoryIdentity("blocked", "global", None, "native")
    check = registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
    assert check.reasons([identity]) == {identity: ("promoted",)}
    assert check.errors == ["broken"]


def test_is_enabled_error_treated_as_disabled(registry, caplog):
    class Broken(FixtureSource):
        def is_enabled(self):
            raise OSError("secret blocked")

    registry.register_source(Broken())
    assert (
        registry.begin_check(registry.EligibilityRequest.from_context("injected_context", None))
        is None
    )
    assert "secret" not in caplog.text and "blocked" not in caplog.text


def test_reasons_follow_registration_order_verdict_order_independent(registry):
    first, second = FixtureSource(reason="promoted"), FixtureSource(reason="superseded")
    second.name = "second"
    identity = registry.MemoryIdentity("blocked", "global", None, "native")
    for sources, expected in [
        ([first, second], ("promoted", "superseded")),
        ([second, first], ("superseded", "promoted")),
    ]:
        registry._SOURCES.clear()
        for source in sources:
            registry.register_source(source)
        check = registry.begin_check(
            registry.EligibilityRequest.from_context("injected_context", None)
        )
        assert check.reasons([identity]) == {identity: expected}


def test_request_fields_from_local_and_internal_context(registry):
    ctx = _ctx()
    ctx.update(allowed_tools=["private"], ephemeral=True)
    request = registry.EligibilityRequest.from_context("curated_recall", ctx)
    assert request.purpose == "curated_recall"
    assert (
        request.terminal_id,
        request.session_name,
        request.agent_profile,
        request.provider,
        request.cwd,
    ) == ("term-001", "test-session", "developer", "claude_code", "/home/user/project")
    assert not hasattr(request, "allowed_tools") and not hasattr(request, "ephemeral")


def test_explain_reports_verdict_and_errors(seeded, registry):
    service, _, ctx = seeded
    source = FixtureSource()
    registry.register_source(source)
    result = _run(
        service.recall_explain(query="kafka streams", scope="global", terminal_context=ctx, limit=3)
    )
    blocked = next(row for row in result["candidates"] if row["key"] == "blocked")
    assert blocked["injection"] == "excluded"
    assert blocked["exclusion_reasons"] == ["promoted"]
    assert result["eligibility_errors"] == []
    assert all(request.purpose == "injected_context" for request in source.requests)
    assert "blocked" in [
        m.key
        for m in _run(service.recall(query="kafka streams", scope="global", terminal_context=ctx))
    ]


def test_vault_clip_counter_ignores_excluded(tmp_path, monkeypatch, isolated_memory_db, registry):
    service, factory, root, config = _injectable_renderer(tmp_path, monkeypatch)
    note = root / "Projects" / "CAO Design" / "Design.md"
    note.write_text("---\ncao:\n  key: design\n---\n" + "x" * 4096)
    from cli_agent_orchestrator.services.vault.reconcile import reconcile

    reconcile(config.vaults[0], apply=True, run_id="eligibility-clip")
    registry.register_source(FixtureSource(keys=("design",)))
    assert service.get_memory_context_for_terminal("worker") == ""
    assert _counter_values(factory) == {}


def test_excluded_related_target_does_not_consume_fanout_cap(seeded, registry):
    service, factory, ctx = seeded
    targets = [f"fanout-{i}" for i in range(6)]
    for key in ["primary-a", "primary-b", *targets]:
        _run(service.store(key=key, content="body", scope="global", terminal_context=ctx))
    with factory() as db:
        for key, value in [
            ("primary-a", ",".join(targets[:3])),
            ("primary-b", ",".join(targets[3:])),
        ]:
            db.query(MemoryMetadataModel).filter_by(key=key).update({"related_keys": value})
        db.commit()
    index = service.get_index_path("global", None)
    index.write_text(
        "\n".join(
            line
            for line in index.read_text().splitlines()
            if not any(f"[{key}]" in line for key in targets)
        )
        + "\n"
    )
    registry.register_source(FixtureSource(keys=("fanout-0",)))
    block = service.get_memory_context(ctx)
    assert "fanout-0 [related]" not in block
    assert "fanout-5 [related]" in block
    assert block.count("[related]") == 5


@pytest.mark.parametrize("identified_by", ["policy", "profile"])
def test_curated_purpose_by_policy_or_profile(seeded, registry, monkeypatch, identified_by):
    from dataclasses import replace

    from cli_agent_orchestrator.services import memory_service

    service, _, ctx = seeded
    source = FixtureSource()
    registry.register_source(source)
    if identified_by == "profile":
        ctx = dict(ctx, agent_profile="memory_manager")
    else:
        original = memory_service._resolve_injection_policy
        monkeypatch.setattr(
            memory_service,
            "_resolve_injection_policy",
            lambda *a, **kw: replace(original(*a, **kw), is_curator=True),
        )
    rows = _run(
        service.recall(query="kafka streams", scope="global", terminal_context=ctx, limit=1)
    )
    assert [m.key for m in rows] == ["survivor"]
    assert source.requests and all(
        request.purpose == "curated_recall" for request in source.requests
    )


def test_internal_context_supplies_same_request_fields(seeded, registry, monkeypatch):
    from fastapi.testclient import TestClient

    from cli_agent_orchestrator.api import main

    service, _, ctx = seeded
    source = FixtureSource()
    registry.register_source(source)
    service.get_memory_context(ctx)
    local_request = source.requests[-1]
    monkeypatch.setattr(main, "_get_memory_service", lambda: service)
    response = TestClient(main.app, base_url="http://localhost").post(
        "/internal/memory/context", json={"terminal_context": ctx, "budget_chars": 3000}
    )
    assert response.status_code == 200, response.text
    assert "blocked:" not in response.json()["context"]
    assert source.requests[-1] == local_request


def test_bm25_corpus_unfiltered_idf_unchanged(seeded, registry, monkeypatch):
    import rank_bm25

    service, _, ctx = seeded
    for i in range(4):
        _run(
            service.store(
                key=f"ballast-{i}",
                content="orchard gardening flowers",
                scope="global",
                terminal_context=ctx,
            )
        )
    corpora = []
    original = rank_bm25.BM25Okapi

    def observe(corpus, *args, **kwargs):
        corpora.append([list(tokens) for tokens in corpus])
        return original(corpus, *args, **kwargs)

    monkeypatch.setattr(rank_bm25, "BM25Okapi", observe)
    args = dict(
        query="kafka streams",
        scope="global",
        scope_id=None,
        memory_type=None,
        limit=10,
        exclude_keys=set(),
        terminal_context=ctx,
        scan_all=False,
    )
    unfiltered = service._bm25_search(**args)
    registry.register_source(FixtureSource())
    check = registry.begin_check(registry.EligibilityRequest.from_context("curated_recall", ctx))
    filtered = service._bm25_search(**args, eligibility=check)
    assert len(corpora) == 2
    assert len(corpora[0]) == 7
    assert corpora[1] == corpora[0]
    assert "blocked" in [m.key for m in unfiltered]
    assert "blocked" not in [m.key for m in filtered]
    assert "survivor" in [m.key for m in filtered]


def test_explain_failure_is_visible_without_excluding_recall(seeded, registry, caplog):
    class Broken(FixtureSource):
        name = "broken"

        def excluded(self, request, candidates):
            raise OSError("blocked secret body")

    service, _, ctx = seeded
    registry.register_source(Broken())
    registry.register_source(FixtureSource())
    result = _run(
        service.recall_explain(query="kafka streams", scope="global", terminal_context=ctx, limit=3)
    )
    assert result["eligibility_errors"] == ["broken"]
    assert any(
        row["key"] == "blocked" and row["injection"] == "excluded" for row in result["candidates"]
    )
    assert "secret body" not in caplog.text


def test_known_exclusion_precedes_unknown_supersedes_group(seeded, registry, monkeypatch):
    service, _, ctx = seeded
    registry.register_source(FixtureSource())
    registry.register_source(registry.SupersedesSource())
    monkeypatch.setenv("CAO_MEMORY_LIFECYCLE_ENABLED", "true")

    def broken(*args, **kwargs):
        raise OSError("/PATH_SENTINEL/KEY_SENTINEL.md")

    monkeypatch.setattr(
        memory_relationship_service.MemoryRelationshipService, "superseded_targets", broken
    )
    result = _run(
        service.recall_explain(query="kafka streams", scope="global", limit=3, terminal_context=ctx)
    )
    by_key = {row["key"]: row for row in result["candidates"]}
    assert by_key["blocked"]["injection"] == "excluded"
    assert by_key["blocked"]["exclusion_reasons"] == ["promoted"]
    assert by_key["survivor"]["injection"] == "unknown"
    assert result["eligibility_errors"] == ["lifecycle.supersedes"]


@pytest.fixture
def same_key_related_targets(svc, db_engine, monkeypatch, registry):
    """Project exclusion must not shadow the eligible global related target."""
    factory = sessionmaker(bind=db_engine)
    monkeypatch.setattr(database, "SessionLocal", factory)
    monkeypatch.setattr(memory_relationship_service, "SessionLocal", factory)
    ctx = _ctx(caller_scope="global")
    primaries = [
        ("primary-project", "project"),
        ("primary-global-a", "global"),
        ("primary-global-b", "global"),
    ]
    now = datetime.now(timezone.utc)
    for days, (key, scope) in enumerate(primaries):
        _run(svc.store(key=key, content="primary-body", scope=scope, terminal_context=ctx))
        _stamp_updated_at(db_engine, key, scope, now - timedelta(days=days))
    for scope, content in [("project", "EXCLUDED TARGET"), ("global", "ELIGIBLE TARGET")]:
        _run(svc.store(key="shared-related", content=content, scope=scope, terminal_context=ctx))
        # Keep related targets out of the primary pool, as in the existing
        # fan-out fixture. The real wiki and metadata rows remain available.
        index = svc.get_index_path(scope, svc.resolve_scope_id(scope, ctx))
        index.write_text(
            "\n".join(
                line for line in index.read_text().splitlines() if "[shared-related]" not in line
            )
            + "\n"
        )
    with factory() as db:
        for key, scope in primaries:
            db.query(MemoryMetadataModel).filter_by(key=key, scope=scope).update(
                {"related_keys": "shared-related"}
            )
        db.commit()

    class ProjectExclusion(FixtureSource):
        def excluded(self, request, candidates):
            return {
                identity: self.reason
                for identity in candidates
                if identity.key == "shared-related" and identity.scope == "project"
            }

    registry.register_source(ProjectExclusion())
    return svc, ctx


def test_excluded_related_target_does_not_shadow_other_scope_in_curator_recall(
    same_key_related_targets,
):
    service, ctx = same_key_related_targets
    rows = _run(
        service.recall(
            query="primary-body",
            search_mode="metadata",
            terminal_context=dict(ctx, agent_profile="memory_manager"),
            include_related=True,
            limit=3,
        )
    )
    assert [(m.key, m.scope) for m in rows if not m.is_related] == [
        ("primary-project", "project"),
        ("primary-global-a", "global"),
        ("primary-global-b", "global"),
    ]
    related = [(m.key, m.scope, m.content) for m in rows if m.is_related]
    # The two eligible global primaries still inject their same-key target once.
    assert related == [("shared-related", "global", "ELIGIBLE TARGET")]


def test_excluded_related_target_does_not_shadow_other_scope_in_injection(
    same_key_related_targets,
):
    service, ctx = same_key_related_targets
    block = service.get_memory_context(ctx)
    assert "EXCLUDED TARGET" not in block
    assert "shared-related [related]: ELIGIBLE TARGET" in block
    assert block.count("shared-related [related]") == 1
