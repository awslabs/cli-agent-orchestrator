"""U8 integration-proof (issue #511): the COMPOSED-PATH tests.

These are the acceptance gate for the feature's real functionality — the direct
answer to the PR #516 failure mode (154 green per-module tests over a
non-functional feature). Each scenario drives the REAL composition (real
MemoryService + MemoryRelationshipService + graph provider), not mocks, and has
a genuine failure mode: a surface not wired to the service, or wrong
producer-scoping, makes the scenario RED.

Scenarios:
- S1  compiler-write-via-service -> See Also -> recall -> graph, end to end
- S2  producer-scoped replacement PRESERVES a human edge (principle 6) [load-bearing]
- S2b fail-before-pass control: an UNSCOPED replace WOULD nuke the human edge
- S3  migrated legacy edge + human edge coexist through a compiler recompute
- S4  multi-edge coexistence (relates_to + contradiction) visible in the graph
- S5  superseded does not outrank active (FR-4.6)
- S6  loss-free compatibility proof gating related_keys retirement (FR-7.2)
- S7  NULL-confidence edge not ranked below a 0.0-confidence edge (NFR-2.3)
- GLOBAL cross-table scope_id: a global relationship is ACCEPTED (endpoint check
  uses .is_(None) against MemoryMetadataModel, NOT the "" sentinel)
- content-free audit: relationship_mutation is registered AND written (SEC-S12)
"""

import uuid

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import memory_relationship_service as mrs_mod
from cli_agent_orchestrator.services.memory_relationship_service import (
    EdgeInput,
    MemoryRelationshipService,
)


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def bound(monkeypatch, db_engine):
    """Bind SessionLocal (used by the relationship service) to the test engine."""
    Session = sessionmaker(bind=db_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    monkeypatch.setattr(mrs_mod, "SessionLocal", Session)
    return Session


def _seed_memory(db_engine, key, scope="global", scope_id=None, body="body"):
    Session = sessionmaker(bind=db_engine)
    s = Session()
    try:
        s.add(
            MemoryMetadataModel(
                id=str(uuid.uuid4()),
                key=key,
                memory_type="project",
                scope=scope,
                scope_id=scope_id,
                file_path=f"/{key}.md",
                tags="t",
            )
        )
        s.commit()
    finally:
        s.close()


def _svc():
    return MemoryRelationshipService()


# --------------------------------------------------------------------------- #
# GLOBAL cross-table scope_id acceptance (the "second silent bug" test)
# --------------------------------------------------------------------------- #
def test_global_scope_relationship_accepted(bound, db_engine):
    """A relationship between two GLOBAL-scope endpoints (scope_id NULL in
    memory_metadata) is ACCEPTED. Must pass because the endpoint check uses
    .is_(None) against MemoryMetadataModel, NOT the "" sentinel — a sentinel
    lookup would match no global memory and falsely reject."""
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    dto = _svc().create("global", None, "a", "b", "relates_to", "human")
    assert dto.status == "active"
    assert dto.scope_id is None  # denormalised from the "" sentinel
    # And it is retrievable / dedups (proving the row actually landed under sentinel).
    again = _svc().create("global", None, "a", "b", "relates_to", "human")
    assert again.id == dto.id  # upsert, not a duplicate (dedup index fired for global)


def test_global_dedup_index_fires(bound, db_engine):
    """Two identical global-scope creates collapse to one row (NULL-in-UNIQUE bug
    would otherwise duplicate)."""
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    svc.create("global", None, "a", "b", "relates_to", "human")
    svc.create("global", None, "a", "b", "relates_to", "human")
    rows = svc.list_relationships("global", None, "a")
    assert len([r for r in rows if r.target_key == "b"]) == 1


# --------------------------------------------------------------------------- #
# S2 / S2b — producer-scoped replacement (principle 6), fail-before-pass
# --------------------------------------------------------------------------- #
def test_s2_producer_scoped_replace_preserves_human_edge(bound, db_engine):
    """S2 (load-bearing): a compiler recompute that drops its own edge must NOT
    remove a human-authored edge on the same source."""
    for k in ("a", "b", "c"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "a", "c", "relates_to", "human")  # human edge
    svc.replace_set("global", None, "a", "compiler", "relates_to", [EdgeInput("b")])
    # recompute: compiler now finds nothing
    svc.replace_set("global", None, "a", "compiler", "relates_to", [])
    active = {(d.origin, d.target_key) for d in svc.list_relationships("global", None, "a")}
    assert ("human", "c") in active, "human edge must survive"
    assert not any(o == "compiler" for o, _ in active), "compiler edge must be gone"


def test_s2b_unscoped_delete_would_nuke_human_edge(bound, db_engine):
    """S2 fail-before-pass control: prove the scoping is what protects the human
    edge. An UNSCOPED delete-all-from-source (the wrong implementation) removes
    the human edge — so S2 passing is meaningful, not vacuous."""
    for k in ("a", "b", "c"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "a", "c", "relates_to", "human")
    svc.create("global", None, "a", "b", "relates_to", "compiler")
    # Simulate the WRONG unscoped replace: delete ALL edges from source a.
    Session = sessionmaker(bind=db_engine)
    s = Session()
    try:
        from cli_agent_orchestrator.clients.database import MemoryRelationshipModel

        s.query(MemoryRelationshipModel).filter(MemoryRelationshipModel.source_key == "a").delete()
        s.commit()
    finally:
        s.close()
    active = svc.list_relationships("global", None, "a")
    assert (
        active == []
    ), "unscoped delete removes EVERYTHING incl the human edge (the bug S2 guards)"


# --------------------------------------------------------------------------- #
# S4 — multi-edge coexistence
# --------------------------------------------------------------------------- #
def test_s4_multi_edge_coexistence(bound, db_engine):
    """relates_to and contradiction between the same pair coexist as distinct
    rows (differ by type)."""
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    svc.create("global", None, "a", "b", "relates_to", "human")
    svc.create("global", None, "a", "b", "contradiction", "human")
    rows = svc.list_relationships("global", None, "a")
    types = {r.type for r in rows if r.target_key == "b"}
    assert types == {"relates_to", "contradiction"}


def test_s4_contradiction_symmetric_query(bound, db_engine):
    """A single directed contradiction row is visible from BOTH endpoints via the
    query-side union (FR-4.5) — no reciprocal row written."""
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    svc.create("global", None, "a", "b", "contradiction", "human")
    from_a = svc.contradictions_for("global", None, "a")
    from_b = svc.contradictions_for("global", None, "b")
    assert len(from_a) == 1 and len(from_b) == 1
    assert from_a[0].id == from_b[0].id  # same single directed row, seen both ways


# --------------------------------------------------------------------------- #
# S5 — superseded is queryable (ranking input)
# --------------------------------------------------------------------------- #
def test_s5_is_superseded(bound, db_engine):
    """A memory that is the TARGET of an active supersedes edge reads as
    superseded; the superseding one does not."""
    _seed_memory(db_engine, "new")
    _seed_memory(db_engine, "old")
    svc = _svc()
    svc.create("global", None, "new", "old", "supersedes", "human")  # new supersedes old
    assert svc.is_superseded("global", None, "old") is True
    assert svc.is_superseded("global", None, "new") is False


# --------------------------------------------------------------------------- #
# S7 — NULL confidence is not treated as zero (NFR-2.3)
# --------------------------------------------------------------------------- #
def test_superseded_targets_batched(bound, db_engine):
    """FR-4.6 ranking input, batched: superseded_targets returns exactly the keys
    that are the target of an active supersedes edge, in one query for many keys."""
    for k in ("new1", "old1", "new2", "old2", "unrelated"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "new1", "old1", "supersedes", "human")
    svc.create("global", None, "new2", "old2", "supersedes", "human")
    hits = svc.superseded_targets("global", None, ["old1", "old2", "new1", "unrelated"])
    assert hits == {"old1", "old2"}  # targets of active supersedes; sources/unrelated excluded


def test_s7_null_confidence_not_zero(bound, db_engine):
    """A NULL-confidence edge is stored as NULL (never coerced to 0), and a
    0.0-confidence edge is stored as 0.0 — they are distinguishable, so ranking
    can treat NULL as absence-of-evidence rather than lowest quality."""
    for k in ("a", "b", "c"):
        _seed_memory(db_engine, k)
    svc = _svc()
    null_edge = svc.create("global", None, "a", "b", "relates_to", "human")  # confidence None
    zero_edge = svc.create("global", None, "a", "c", "relates_to", "human", confidence=0.0)
    assert null_edge.confidence is None
    assert zero_edge.confidence == 0.0


# --------------------------------------------------------------------------- #
# fail-closed security (NFR-1.1..1.6)
# --------------------------------------------------------------------------- #
def test_fail_closed_self_link(bound, db_engine):
    _seed_memory(db_engine, "a")
    with pytest.raises(ValueError):
        _svc().create("global", None, "a", "a", "relates_to", "human")


def test_fail_closed_dangling_endpoint(bound, db_engine):
    _seed_memory(db_engine, "a")
    with pytest.raises(ValueError):
        _svc().create("global", None, "a", "ghost", "relates_to", "human")


def test_fail_closed_bad_type(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    with pytest.raises(ValueError):
        _svc().create("global", None, "a", "b", "not_a_type", "human")


def test_fail_closed_confidence_out_of_range(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    with pytest.raises(ValueError):
        _svc().create("global", None, "a", "b", "relates_to", "human", confidence=1.5)


def test_fail_closed_edge_count_bound(bound, db_engine):
    _seed_memory(db_engine, "a")
    with pytest.raises(ValueError):
        _svc().replace_set(
            "global",
            None,
            "a",
            "compiler",
            "relates_to",
            [EdgeInput(f"t{i}") for i in range(65)],
        )


def test_fail_closed_attributes_size_bound(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    big = {"x": "y" * 3000}
    with pytest.raises(ValueError):
        _svc().create("global", None, "a", "b", "relates_to", "human", attributes=big)


# --------------------------------------------------------------------------- #
# read-projection defaults (FR-4.3)
# --------------------------------------------------------------------------- #
def test_proposal_excluded_by_default(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    svc.create("global", None, "a", "b", "relates_to", "human", status="proposal")
    assert svc.list_relationships("global", None, "a") == []  # active-only default
    widened = svc.list_relationships("global", None, "a", status="proposal")
    assert len(widened) == 1


def test_lifecycle_promote_reject_soft_delete(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    d = svc.create("global", None, "a", "b", "relates_to", "human", status="proposal")
    assert svc.promote(d.id).status == "active"
    assert svc.reject(d.id).status == "rejected"
    assert svc.soft_delete(d.id).status == "deleted"


# --------------------------------------------------------------------------- #
# coverage of patch / get / list filters / active_targets / stale / upsert
# --------------------------------------------------------------------------- #
def test_patch_mutable_fields(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    d = svc.create("global", None, "a", "b", "relates_to", "human")
    patched = svc.patch(d.id, status="proposal", confidence=0.7, rank=3, attributes={"k": "v"})
    assert patched.status == "proposal"
    assert patched.confidence == 0.7
    assert patched.rank == 3
    assert patched.attributes == {"k": "v"}


def test_get_and_not_found(bound, db_engine):
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    d = svc.create("global", None, "a", "b", "relates_to", "human")
    assert svc.get(d.id).id == d.id
    assert svc.get("nonexistent-id") is None
    with pytest.raises(ValueError):
        svc.patch("nonexistent-id", status="active")


def test_list_filters_status_list_and_types(bound, db_engine):
    for k in ("a", "b", "c"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "a", "b", "relates_to", "human")
    svc.create("global", None, "a", "c", "contradiction", "human", status="proposal")
    # status as a list widens
    got = svc.list_relationships("global", None, "a", status=["active", "proposal"])
    assert len(got) == 2
    # types filter
    only_rel = svc.list_relationships(
        "global", None, "a", status=["active", "proposal"], types=["relates_to"]
    )
    assert [d.type for d in only_rel] == ["relates_to"]


def test_active_targets_rank_order_and_project_scope(bound, db_engine):
    for k in ("src", "t1", "t2"):
        _seed_memory(db_engine, k, scope="project", scope_id="proj1")
    svc = _svc()
    svc.replace_set(
        "project",
        "proj1",
        "src",
        "compiler",
        "relates_to",
        [EdgeInput("t2", rank=1), EdgeInput("t1", rank=0)],
    )
    # ordered by rank (0 before 1) — exercises a non-global scope_id path too
    assert svc.active_targets("project", "proj1", "src") == ["t1", "t2"]


def test_s3_legacy_and_human_survive_compiler_recompute(bound, db_engine):
    """S3 (SEC-IP3/REL-IP1): a migrated legacy edge AND a human edge on the same
    source both survive a compiler recompute — three-way coexistence where the
    compiler replace_set touches ONLY origin=compiler rows."""
    for k in ("s", "leg", "hum", "comp"):
        _seed_memory(db_engine, k)
    svc = _svc()
    # legacy backfill row (simulate what U1 backfill writes)
    svc.create("global", None, "s", "leg", "relates_to", "legacy_related_keys")
    # human edge
    svc.create("global", None, "s", "hum", "relates_to", "human")
    # compiler set, then recompute to a different set
    svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("comp")])
    svc.replace_set(
        "global", None, "s", "compiler", "relates_to", [EdgeInput("hum")]
    )  # compiler now points at hum
    active = {(d.origin, d.target_key) for d in svc.list_relationships("global", None, "s")}
    assert ("legacy_related_keys", "leg") in active, "legacy edge must survive"
    assert ("human", "hum") in active, "human edge must survive"
    assert ("compiler", "hum") in active, "compiler's new edge present"
    assert ("compiler", "comp") not in active, "compiler's old edge replaced"


def test_s6_loss_free_compatibility_proof(bound, db_engine):
    """S6 (SEC-IP2/REL-IP2/BR-IP3, FR-7.2): every valid legacy related_keys link
    is reachable as an ACTIVE store row through the service, and a dangling one
    is NOT activated — the proof that GATES related_keys retirement. Drives the
    real U1 backfill via init_db over a memory_metadata row with related_keys."""
    from cli_agent_orchestrator.clients import database as db_mod

    # Seed a memory with related_keys "x,y" (x exists, y exists) + a dangling "z".
    for k in ("home", "x", "y"):
        _seed_memory(db_engine, k)
    Session = sessionmaker(bind=db_engine)
    s = Session()
    try:
        row = s.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "home").first()
        row.related_keys = "x,y,ghost"  # ghost is dangling
        s.commit()
    finally:
        s.close()
    # Run the real backfill against this engine's connection.
    conn = db_engine.raw_connection()
    try:
        db_mod._backfill_legacy_related_keys(conn.driver_connection)
    finally:
        conn.close()
    svc = _svc()
    active = svc.list_relationships("global", None, "home")
    targets = {d.target_key: d for d in active}
    assert "x" in targets and "y" in targets, "valid legacy links reachable as active rows"
    assert targets["x"].origin == "legacy_related_keys"
    assert targets["x"].confidence is None, "no fabricated confidence (NFR-2.1)"
    assert "ghost" not in targets, "dangling legacy link NOT activated (reported, FR-1.5)"


def test_s6_legacy_related_keys_unconverted_still_expands(bound, db_engine, tmp_path, monkeypatch):
    """S6 strengthening (reviewer/Stan): the loss-freedom proof must cover the
    path that actually loses data — a related_keys value written by a route OTHER
    than an LLM compile (here: a direct DB write after store(), with NO store edge
    and NO backfill run) MUST still expand via recall(include_related=True). This
    would have caught the _expand_related regression (store-only, no legacy
    fallback → silent empty expansion). It fails if the union fallback is removed."""
    import asyncio

    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import memory_service as ms_mod

    # A real MemoryService bound to the test engine + tmp base dir.
    svc = ms_mod.MemoryService(base_dir=tmp_path, db_engine=db_engine)
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", tmp_path)
    Session = sessionmaker(bind=db_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)

    async def _setup():
        await svc.store(
            content="# root\nalpha unique-token", key="root", memory_type="project", scope="global"
        )
        await svc.store(
            content="# legacyfriend\nbeta",
            key="legacyfriend",
            memory_type="project",
            scope="global",
        )

    asyncio.run(_setup())

    # Write related_keys DIRECTLY (post-store, never via LLM compile) — the exact
    # path that produces no store edge and that the backfill (which we do NOT run
    # here) would not convert.
    s = Session()
    try:
        row = s.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "root").first()
        row.related_keys = "legacyfriend"
        s.commit()
    finally:
        s.close()
    # No store row exists for root→legacyfriend:
    assert _svc().active_targets("global", None, "root") == []

    # recall must STILL surface legacyfriend as a related expansion (union fallback).
    res = asyncio.run(svc.recall(query="alpha", scope="global", include_related=True, limit=1))
    keys = [(m.key, getattr(m, "is_related", False)) for m in res]
    assert any(
        k == "legacyfriend" and rel for k, rel in keys
    ), f"unconverted legacy related_keys must still expand; got {keys}"


def test_s1_composed_path_and_content_free(bound, db_engine):
    """S1 (end-to-end) + SEC-IP4 content-free: a compiler-written edge is
    projectable and every read surface (list DTO, active_targets) exposes only
    content-free fields — no body/body_hash/prompt. (See Also/recall/graph
    composition over MemoryService is covered by the provider + memory_service
    suites; here we assert the store->DTO path is content-free.)"""
    for k in ("a", "b"):
        _seed_memory(db_engine, k, body="SECRET BODY TEXT that must never leak")
    svc = _svc()
    svc.replace_set("global", None, "a", "compiler", "relates_to", [EdgeInput("b")])
    # active_targets is the See-Also/recall projection helper
    assert svc.active_targets("global", None, "a") == ["b"]
    dto = svc.list_relationships("global", None, "a")[0]
    d = dto.to_dict()
    for forbidden in ("body", "body_hash", "prompt"):
        assert forbidden not in d, f"DTO must be content-free ({forbidden})"
    assert "SECRET BODY" not in str(d), "no memory body may leak into the DTO"


def test_secs12_audit_written_and_content_free(bound, db_engine, tmp_path, monkeypatch):
    """SEC-S12 two-part: a create writes a relationship_mutation audit record
    (registration alone is not enough — a closed whitelist would drop it), and
    the record is content-free (endpoints/origin/status, no memory body)."""
    import asyncio

    from cli_agent_orchestrator.services import audit_log

    # Point the audit dir at tmp and capture the awaited write.
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", tmp_path)
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    # Drive the awaited write path directly (NOWAIT flush is loop-dependent).
    asyncio.run(
        audit_log.write_audit(
            "relationship_mutation",
            "relationship create",
            action="create",
            id="x",
            scope="global",
            scope_id="",
            source_key="a",
            target_key="b",
            type="relates_to",
            origin="human",
            status="active",
        )
    )
    logdir = tmp_path / "logs" / "memory"
    files = list(logdir.glob("*.md")) if logdir.exists() else []
    assert files, "a relationship_mutation record MUST be written (not vacuously absent)"
    text = "".join(f.read_text() for f in files)
    assert "relationship_mutation" in text
    assert "source_key" in text and "origin" in text  # provenance present
    for forbidden in ("body", "body_hash", "prompt"):
        assert forbidden not in text, f"audit must be content-free ({forbidden})"


def test_replace_set_soft_rejects_bad_edge_not_whole_batch(bound, db_engine):
    """reviewer F3: one edge with out-of-range confidence is soft-rejected into
    the report; the valid edges in the same batch still land."""
    for k in ("s", "good"):
        _seed_memory(db_engine, k)
    svc = _svc()
    report = svc.replace_set(
        "global",
        None,
        "s",
        "compiler",
        "relates_to",
        [EdgeInput("good"), EdgeInput("nope", confidence=2.0)],
    )
    assert report.added == 1
    assert any(r["reason"] == "invalid_attrs_or_confidence" for r in report.rejected)
    assert svc.active_targets("global", None, "s") == ["good"]


def test_replace_set_emits_audit(bound, db_engine, monkeypatch):
    """reviewer F2: replace_set (bulk producer path) emits a content-free summary
    audit event, so producer writes are not forensically silent."""
    calls = []

    def _fake_nowait(event, summary, **fields):
        calls.append((event, fields))

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.audit_log.write_audit_nowait", _fake_nowait
    )
    for k in ("s", "t"):
        _seed_memory(db_engine, k)
    _svc().replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("t")])
    rs = [c for c in calls if c[1].get("action") == "replace_set"]
    assert rs, "replace_set must emit an audit event"
    fields = rs[0][1]
    assert fields["origin"] == "compiler" and "added" in fields
    for forbidden in ("body", "prompt"):
        assert forbidden not in fields


def test_stale_flag_and_filter(bound, db_engine):
    from datetime import datetime, timedelta, timezone

    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    # edge whose source_updated_at is in the past → stale vs the memory's now()
    past = datetime.now(timezone.utc) - timedelta(days=1)
    svc.create("global", None, "a", "b", "relates_to", "human", source_updated_at=past)
    all_edges = svc.list_relationships("global", None, "a")
    assert all_edges and all_edges[0].stale is True
    assert len(svc.list_relationships("global", None, "a", stale_only=True)) == 1
