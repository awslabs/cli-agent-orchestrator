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
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database as db_mod
from cli_agent_orchestrator.clients.database import (
    Base,
    MemoryMetadataModel,
    MemoryRelationshipModel,
)
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


def _seed_memory(db_engine, key, scope="global", scope_id=None, body=None, body_dir=None):
    """Seed a memory_metadata row.

    ``body`` is the memory's CONTENT. Memory bodies live in files, not in
    ``memory_metadata`` (the table has no body/content column), so a body is
    only storable when the caller supplies ``body_dir``: the file is written
    there and ``file_path`` points at it, making the content genuinely
    retrievable from the row.

    This signature deliberately refuses to accept-and-discard a body. An earlier
    version took ``body="body"`` and silently dropped it, which made every
    "the body must not leak" assertion pass vacuously — the body had never
    been written anywhere. If you pass ``body`` you MUST pass ``body_dir``.
    """
    if body is not None and body_dir is None:
        raise AssertionError(
            "_seed_memory: body requires body_dir — a body with nowhere to live "
            "would be discarded, making content-leak assertions vacuous"
        )
    if body is not None:
        file_path = str(Path(body_dir) / f"{key}.md")
        Path(body_dir).mkdir(parents=True, exist_ok=True)
        Path(file_path).write_text(body, encoding="utf-8")
    else:
        file_path = f"/{key}.md"
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
                file_path=file_path,
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


def test_s1_composed_path_and_content_free(bound, db_engine, tmp_path):
    """S1 (end-to-end) + SEC-IP4 content-free: a compiler-written edge is
    projectable and every read surface (list DTO, active_targets) exposes only
    content-free fields — no body/body_hash/prompt.

    The canary body is written to the seeded memories' real FILES, and those files are
    reachable from the store's own rows (``memory_metadata.file_path``). That is
    what gives this assertion a genuine failure mode: a projection that resolved
    an endpoint's ``file_path`` and inlined its content — the plausible way this
    boundary would actually leak — turns the test RED.

    Guarding the guard: the test asserts the canary IS retrievable through the
    row before asserting the DTO omits it. Without that first assertion the
    whole check passes vacuously whenever the content was never stored, which is
    precisely the defect this test previously had (it passed a ``body`` argument
    that ``_seed_memory`` silently discarded).
    """
    canary_body = "CANARY-MARKER-4f2a memory body text that must never leak"
    for k in ("a", "b"):
        _seed_memory(db_engine, k, body=canary_body, body_dir=tmp_path / "wiki")
    # PRE-ASSERTION: the canary body is genuinely stored and reachable from the row,
    # so "not in the DTO" is a real claim about the projection.
    Session = sessionmaker(bind=db_engine)
    s = Session()
    try:
        row = s.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "a").first()
        assert row is not None and row.file_path
        assert canary_body in Path(row.file_path).read_text(
            encoding="utf-8"
        ), "content must actually exist behind the row, or the leak check is vacuous"
    finally:
        s.close()

    svc = _svc()
    svc.replace_set("global", None, "a", "compiler", "relates_to", [EdgeInput("b")])
    # active_targets is the See-Also/recall projection helper
    targets = svc.active_targets("global", None, "a")
    assert targets == ["b"]
    assert canary_body not in str(targets), "active_targets must project keys only, never content"
    dto = svc.list_relationships("global", None, "a")[0]
    d = dto.to_dict()
    for forbidden in ("body", "body_hash", "prompt", "content", "file_path"):
        assert forbidden not in d, f"DTO must be content-free ({forbidden})"
    assert canary_body not in str(d), "no memory body may leak into the DTO"


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


def test_replace_set_does_not_resurrect_a_human_rejected_edge(bound, db_engine):
    """Human review (PR #524): an operator's ``relationships reject`` verdict must
    survive the next producer recompute.

    ``replace_set``'s existing-row query is deliberately status-agnostic (it owns
    every row of its producer tuple), so a rejected row lands in
    ``existing_by_target``. The keep-branch used to force ``status = "active"``
    unconditionally, so the very next compile silently resurrected an edge a human
    had explicitly rejected — no report entry, nothing in the output. That defeats
    the curation surface (``cao memory relationships reject``) this store ships.

    Drives the REAL path: reject via the service, then recompute through
    ``replace_set`` with the target STILL in the producer's set.
    """
    for k in ("s", "keepme"):
        _seed_memory(db_engine, k)
    svc = _svc()
    dto = svc.create("global", None, "s", "keepme", "relates_to", "compiler")
    assert svc.reject(dto.id).status == "rejected"

    # The producer recomputes and STILL reports the edge.
    report = svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("keepme")])

    row = svc.get(dto.id)
    assert row.status == "rejected", (
        "a producer recompute must NOT reactivate a human-rejected edge; "
        f"got status={row.status}"
    )
    # The refusal is visible, not silent.
    assert {"target": "keepme", "status": "rejected"} in report.preserved
    assert report.kept == 0, "a preserved row is not a kept row"
    # And it stays out of the active projection.
    assert svc.active_targets("global", None, "s") == []


def test_replace_set_does_not_delete_a_rejected_edge_dropped_by_the_producer(bound, db_engine):
    """The other half of the same guarantee: DROPPING the target from the
    producer's set must not delete the operator's verdict either.

    Deleting the row would discard the rejection just as surely as reactivating
    it — and worse, the next recompute that re-reports the target would insert a
    fresh ``active`` row with the rejection gone. So the delete-branch must skip
    curation-terminal rows too.
    """
    for k in ("s", "dropme", "other"):
        _seed_memory(db_engine, k)
    svc = _svc()
    dto = svc.create("global", None, "s", "dropme", "relates_to", "compiler")
    svc.reject(dto.id)

    # Recompute WITHOUT "dropme" — the producer no longer reports it.
    report = svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("other")])

    assert (
        svc.get(dto.id).status == "rejected"
    ), "dropping the target must not delete the rejection row"
    assert {"target": "dropme", "status": "rejected"} in report.preserved
    assert report.removed == 0, "a preserved row must not count as removed"

    # The rejection still suppresses a later recompute that re-reports it.
    svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("dropme")])
    assert (
        svc.active_targets("global", None, "s") == []
    ), "a re-reported rejected target must not come back as active"


def test_replace_set_still_reactivates_a_superseded_edge(bound, db_engine):
    """Scoping control for the fix: ``superseded`` is NOT curation-terminal.

    It is lifecycle-DERIVED rather than operator-authored, so a producer that
    still reports the edge is legitimate evidence the supersession no longer
    holds. This pins the boundary — a fix that over-broadly froze every
    non-active status would turn this test RED.
    """
    for k in ("s", "t"):
        _seed_memory(db_engine, k)
    svc = _svc()
    dto = svc.create("global", None, "s", "t", "relates_to", "compiler")
    svc.patch(dto.id, status="superseded")

    report = svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("t")])

    assert svc.get(dto.id).status == "active", "a superseded edge is reactivated by its producer"
    assert report.kept == 1
    assert report.preserved == []


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


# --------------------------------------------------------------------------- #
# PR #524 review-round regressions (each fails on the pre-fix code)
# --------------------------------------------------------------------------- #
def test_rank_then_created_handles_null_and_naive_created_at():
    """FR-3.1/FR-3.2: the sort key never raises on a NULL or naive ``created_at``.

    Why this is asserted on the sort FUNCTION and not through a DB round-trip:
    ``DATABASE_URL`` is SQLite-only (constants.py) and SQLite does not persist
    timezone offsets, so every ``created_at`` READ BACK from the DB is naive —
    including on the ``DateTime(timezone=True)`` column. A DB-level test can
    therefore never produce the naive/aware mix, and asserting there would pass
    whether or not the fix is present (a vacuous test).

    The mix IS reachable at the function boundary: ``_utcnow`` stamps an AWARE
    value, so a row object still holding its Python-side default (not yet
    refreshed from the DB) sorts aware, while a NULL row falls back to the
    sentinel and any DB-loaded row is naive. Pre-fix the key was
    ``r.created_at or datetime.min`` — a NAIVE sentinel — so mixing it with an
    aware value raised ``TypeError: can't compare offset-naive and offset-aware
    datetimes``. Both sides are now normalised to tz-aware UTC.

    Equal ranks force the datetime comparison; with distinct ranks the tuple's
    first element decides and ``created_at`` is never compared.
    """

    class _Row:
        def __init__(self, rank, created_at, target_key):
            self.rank = rank
            self.created_at = created_at
            self.target_key = target_key

    aware = _Row(1, datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc), "t-aware")
    naive = _Row(1, datetime(2026, 6, 1, 12, 0), "t-naive")
    nulled = _Row(1, None, "t-null")

    # Pre-fix: sorting these together raises TypeError.
    rows = [aware, nulled, naive]
    rows.sort(key=mrs_mod._rank_then_created)
    assert [r.target_key for r in rows] == ["t-null", "t-naive", "t-aware"], (
        "NULL sorts first (earliest instant), then the naive value read as UTC, "
        "then the later aware value"
    )
    # Every normalised key is tz-aware, so any pair is comparable.
    for row in rows:
        assert mrs_mod._sort_dt(row.created_at).tzinfo is not None


def test_active_targets_orders_by_rank_then_created_at(bound, db_engine):
    """FR-3.1 through the real DB path: a NULL ``created_at`` row alongside a
    populated one sorts without raising, and the NULL row comes first.

    This is the end-to-end companion to the function-level test above. On SQLite
    both values read back naive, so this cannot reproduce the naive/aware
    TypeError — it guards the NULL-fallback ordering itself.
    """
    for k in ("src", "t-null", "t-populated"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "src", "t-populated", "relates_to", "human", rank=1)
    svc.create("global", None, "src", "t-null", "relates_to", "human", rank=1)
    Session = sessionmaker(bind=db_engine)
    s = Session()
    try:
        row = (
            s.query(MemoryRelationshipModel)
            .filter(MemoryRelationshipModel.target_key == "t-null")
            .first()
        )
        assert row is not None
        row.created_at = None
        s.commit()
    finally:
        s.close()

    targets = svc.active_targets("global", None, "src")
    assert sorted(targets) == ["t-null", "t-populated"]
    assert targets[0] == "t-null", "the NULL-created_at row sorts first"


def test_active_targets_for_batches_many_sources(bound, db_engine):
    """FR-2.1/FR-2.5: the batched read returns per-source ordered targets and
    matches single-key ``active_targets`` for every source."""
    for k in ("s1", "s2", "s3", "t1", "t2", "t3"):
        _seed_memory(db_engine, k)
    svc = _svc()
    # s1 -> t2 (rank 2), t1 (rank 1): rank decides the order, not insertion.
    svc.create("global", None, "s1", "t2", "relates_to", "compiler", rank=2)
    svc.create("global", None, "s1", "t1", "relates_to", "compiler", rank=1)
    svc.create("global", None, "s2", "t3", "relates_to", "compiler")
    # s3 has no edges at all.
    out = svc.active_targets_for("global", None, ["s1", "s2", "s3"])
    assert out["s1"] == ["t1", "t2"], "must honour (rank, created_at) order"
    assert out["s2"] == ["t3"]
    assert "s3" not in out, "a source with no active edges is absent, not empty"
    # Batched result agrees with the single-key helper for every source.
    for k in ("s1", "s2", "s3"):
        assert out.get(k, []) == svc.active_targets("global", None, k)
    # Empty input short-circuits.
    assert svc.active_targets_for("global", None, []) == {}


def test_active_targets_for_excludes_non_active_and_other_types(bound, db_engine):
    """FR-2.5: the batched read applies the same status/type filters as the
    single-key helper — only ACTIVE edges of the requested type."""
    for k in ("s", "t-active", "t-rejected", "t-contra"):
        _seed_memory(db_engine, k)
    svc = _svc()
    svc.create("global", None, "s", "t-active", "relates_to", "human")
    rejected = svc.create("global", None, "s", "t-rejected", "relates_to", "human")
    svc.reject(rejected.id)
    svc.create("global", None, "s", "t-contra", "contradiction", "wiki_lint")
    out = svc.active_targets_for("global", None, ["s"])
    assert out["s"] == ["t-active"]
    contra = svc.active_targets_for("global", None, ["s"], type="contradiction")
    assert contra["s"] == ["t-contra"]


def test_list_relationships_source_keys_bounds_the_fetch(bound, db_engine):
    """FR-2.3: the ``source_keys`` filter bounds which rows are fetched."""
    for k in ("in1", "in2", "out1", "t"):
        _seed_memory(db_engine, k)
    svc = _svc()
    for src in ("in1", "in2", "out1"):
        svc.create("global", None, src, "t", "relates_to", "human")
    bounded = svc.list_relationships("global", None, source_keys=["in1", "in2"])
    assert sorted(d.source_key for d in bounded) == ["in1", "in2"]
    # An EMPTY node set means no edges — not "unfiltered".
    assert svc.list_relationships("global", None, source_keys=[]) == []
    # Absent filter still returns everything.
    assert len(svc.list_relationships("global", None)) == 3


def test_validate_attributes_rejects_unserialisable_as_valueerror(bound, db_engine):
    """FR-4.1/FR-4.2: a non-JSON-serialisable attribute raises ValueError, not
    TypeError.

    A TypeError would escape the REST layer's ``ValueError -> 400`` mapping (an
    unhandled 500) and defeat ``replace_set``'s per-edge soft rejection, which
    catches only ValueError.
    """
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    with pytest.raises(ValueError, match="JSON-serialisable"):
        svc.create("global", None, "a", "b", "relates_to", "human", attributes={"bad": {1, 2, 3}})


def test_replace_set_soft_rejects_unserialisable_edge_not_whole_batch(bound, db_engine):
    """FR-4.2: the per-edge soft rejection now covers an unserialisable
    attribute — pre-fix the TypeError aborted the entire batch."""
    for k in ("s", "good", "bad"):
        _seed_memory(db_engine, k)
    svc = _svc()
    report = svc.replace_set(
        "global",
        None,
        "s",
        "compiler",
        "relates_to",
        [EdgeInput("bad", attributes={"x": {1, 2}}), EdgeInput("good")],
    )
    assert report.added == 1, "the good edge must still land"
    assert report.rejected, "the unserialisable edge must be soft-rejected"
    assert svc.active_targets("global", None, "s") == ["good"]


def test_attributes_size_error_reports_bytes(bound, db_engine):
    """FR-4.3: the size message reports the unit actually enforced (bytes).

    A multi-byte payload is used so a char count and a byte count differ — a
    message still reporting chars would show the smaller, misleading number.
    """
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    # Each "€" is 3 UTF-8 bytes, so chars << bytes.
    oversized = {"k": "€" * 1200}
    with pytest.raises(ValueError, match=r"exceed 2048 bytes \(\d+ bytes\)") as e:
        svc.create("global", None, "a", "b", "relates_to", "human", attributes=oversized)
    assert "chars" not in str(e.value)


def test_superseded_keys_does_not_cross_project_scope_ids(bound, db_engine, monkeypatch):
    """FR-5.4: the same key in two DIFFERENT project scope_ids must not share a
    superseded verdict.

    ``memory_metadata`` is unique on ``(key, scope, scope_id)``, so "guide" in
    project P1 and "guide" in project P2 are DIFFERENT memories. Pre-fix
    ``_superseded_keys`` returned ``{(scope, key)}``, so superseding P1's copy
    marked P2's untouched copy as superseded too whenever a recall spanned both
    projects. The identity is now the full ``(scope, scope_id, key)`` 3-tuple.
    """
    from datetime import datetime as _dt

    from cli_agent_orchestrator.models.memory import Memory
    from cli_agent_orchestrator.services.memory_service import MemoryService

    # "guide" exists in BOTH projects; only p1's copy is superseded.
    _seed_memory(db_engine, "guide", scope="project", scope_id="p1")
    _seed_memory(db_engine, "newer", scope="project", scope_id="p1")
    _seed_memory(db_engine, "guide", scope="project", scope_id="p2")
    _svc().create("project", "p1", "newer", "guide", "supersedes", "human")

    def _mem(key, scope_id):
        now = _dt.now(timezone.utc)
        return Memory(
            id=str(uuid.uuid4()),
            key=key,
            memory_type="project",
            scope="project",
            scope_id=scope_id,
            file_path=f"/{key}.md",
            tags="",
            created_at=now,
            updated_at=now,
        )

    svc = MemoryService()
    p1_mem, p2_mem = _mem("guide", "p1"), _mem("guide", "p2")
    hits = svc._superseded_keys([p1_mem, p2_mem])

    # BEHAVIOURAL assertion driving the PRODUCTION predicate (``_is_superseded``,
    # which recall's demotion sort key calls) — NOT a local re-expression of it.
    # An earlier version of this test defined its own ``_is_demoted`` that OR-ed
    # in the loose ``(scope, key)`` form; that made it pass even when the real
    # sort key was mutated back to the cross-project-demoting comparison, so it
    # proved only that ``_superseded_keys`` was well-formed. Calling the shipped
    # predicate is what gives this test a genuine failure mode.
    assert svc._is_superseded(p1_mem, hits), "p1's superseded copy must be demoted"
    assert not svc._is_superseded(
        p2_mem, hits
    ), "p2's untouched copy must NOT be demoted — a (scope, key) set cross-project-demotes it"
    # And the identity stored is the full 3-tuple.
    assert ("project", "p1", "guide") in hits
    assert ("project", "p2", "guide") not in hits


def test_expand_related_does_not_query_the_store_per_primary(bound, db_engine, monkeypatch):
    """FR-2.2: the store read in recall's expansion is BATCHED per
    (scope, scope_id) — one call for N primaries, not one per primary.

    A correctness test cannot catch this: an N+1 returns exactly the right
    answers, just with N queries. So the guard counts service calls. The legacy
    ``related_keys`` fallback beside it was already batched; the store read was
    not, which made the AUTHORITATIVE path the slow one.
    """
    from datetime import datetime as _dt

    from cli_agent_orchestrator.models.memory import Memory
    from cli_agent_orchestrator.services import memory_service as ms_mod
    from cli_agent_orchestrator.services.memory_service import MemoryService

    primaries_keys = ["p1", "p2", "p3", "p4", "p5"]
    for k in primaries_keys + ["t1"]:
        _seed_memory(db_engine, k)
    svc_rel = _svc()
    for k in primaries_keys:
        svc_rel.create("global", None, k, "t1", "relates_to", "compiler")

    calls = {"batched": 0, "per_key": 0}
    real_batched = mrs_mod.MemoryRelationshipService.active_targets_for
    real_single = mrs_mod.MemoryRelationshipService.active_targets

    def _counting_batched(self, *a, **kw):
        calls["batched"] += 1
        return real_batched(self, *a, **kw)

    def _counting_single(self, *a, **kw):
        calls["per_key"] += 1
        return real_single(self, *a, **kw)

    monkeypatch.setattr(mrs_mod.MemoryRelationshipService, "active_targets_for", _counting_batched)
    monkeypatch.setattr(mrs_mod.MemoryRelationshipService, "active_targets", _counting_single)
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", Path(str(db_engine.url).split("///")[-1]).parent)

    def _mem(key):
        now = _dt.now(timezone.utc)
        return Memory(
            id=str(uuid.uuid4()),
            key=key,
            memory_type="project",
            scope="global",
            scope_id=None,
            file_path=f"/{key}.md",
            tags="",
            created_at=now,
            updated_at=now,
        )

    svc = MemoryService()
    svc._expand_related([_mem(k) for k in primaries_keys])

    assert calls["batched"] == 1, (
        f"5 primaries in ONE (scope, scope_id) must cost ONE batched store read, "
        f"got {calls['batched']}"
    )
    assert calls["per_key"] == 0, (
        f"the per-primary active_targets call is the N+1 — it must not be used in "
        f"the expansion path, got {calls['per_key']} calls"
    )


def test_patch_rejects_unserialisable_attributes_as_valueerror(bound, db_engine):
    """FR-4.1 on the PATCH path: ``patch`` validates attributes through the same
    helper as ``create``, so it must also raise ValueError (not TypeError).

    The PATCH handler maps ValueError to 404-or-400; a TypeError would surface as
    a 500 there exactly as it did on create. Reviewer finding: the create path had
    a test and the patch path did not, even though both call the same validator.
    """
    _seed_memory(db_engine, "a")
    _seed_memory(db_engine, "b")
    svc = _svc()
    dto = svc.create("global", None, "a", "b", "relates_to", "human")
    with pytest.raises(ValueError, match="JSON-serialisable"):
        svc.patch(dto.id, attributes={"bad": {1, 2}})
    # The row is unchanged (validation happens before the field is assigned).
    assert svc.get(dto.id).attributes is None


def test_list_relationships_source_keys_drops_unsanitizable_key(bound, db_engine):
    """A malformed key in the node set must not abort the whole query.

    Reviewer finding: ``source_keys`` sanitizes each key, and ``_sanitize_key``
    RAISES for a key that reduces to empty (e.g. "..."). If that propagated, the
    graph provider's ``except`` branch would degrade the ENTIRE scope to a
    relationship-free graph because of one bad index entry. Unsanitizable keys are
    dropped from the filter instead — they can never match a stored row anyway,
    since every stored source_key was sanitized at write time.
    """
    _seed_memory(db_engine, "good")
    _seed_memory(db_engine, "t")
    svc = _svc()
    svc.create("global", None, "good", "t", "relates_to", "human")
    dtos = svc.list_relationships("global", None, source_keys=["good", "...", "___"])
    assert [d.source_key for d in dtos] == ["good"], "the valid key's edge must survive"


def test_forget_purges_relationships_and_slug_reuse_inherits_nothing(
    bound, db_engine, tmp_path, monkeypatch
):
    """Human review (PR #524): ``forget()`` must purge the key's relationship rows.

    forget() removed the wiki file, the index entry and the memory_metadata row
    but left memory_relationships rows behind, still ``active``. Two consequences:
    every read path had to tolerate endpoints that no longer resolve, and a later
    memory created with the SAME slug silently INHERITED the dead memory's edges.

    Drives the real composed path: store -> edge -> forget -> re-store same slug.
    """
    import asyncio

    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import memory_service as ms_mod

    svc = ms_mod.MemoryService(base_dir=tmp_path, db_engine=db_engine)
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", tmp_path)
    Session = sessionmaker(bind=db_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)

    async def _setup():
        for k in ("doomed", "survivor"):
            await svc.store(content=f"# {k}\nbody", key=k, memory_type="project", scope="global")

    asyncio.run(_setup())

    rel = _svc()
    # An edge in EACH direction — both are dangling once "doomed" is gone.
    rel.create("global", None, "doomed", "survivor", "relates_to", "compiler")
    rel.create("global", None, "survivor", "doomed", "relates_to", "compiler")
    assert len(rel.list_relationships("global", None, "doomed")) == 1
    assert len(rel.list_relationships("global", None, "survivor")) == 1

    assert asyncio.run(svc.forget("doomed", scope="global", scope_id=None)) is True

    # Both directions are gone...
    assert rel.list_relationships("global", None, "doomed") == []
    assert (
        rel.list_relationships("global", None, "survivor") == []
    ), "an edge INTO the forgotten key is equally dangling and must also be purged"

    # ...and a NEW memory reusing the slug inherits nothing.
    asyncio.run(
        svc.store(
            content="# doomed\nreused slug", key="doomed", memory_type="project", scope="global"
        )
    )
    assert (
        rel.active_targets("global", None, "doomed") == []
    ), "a same-slug memory must not inherit the forgotten memory's edges"


def test_forget_purges_relationships_when_the_file_already_vanished(
    bound, db_engine, tmp_path, monkeypatch
):
    """The other forget() exit path: the wiki file is already gone (removed
    out-of-band), so forget() returns False early. The relationship rows are just
    as stale on that path and must be purged too — otherwise the cleanup depends
    on which branch happened to run."""
    import asyncio

    from cli_agent_orchestrator.clients import database as db_mod
    from cli_agent_orchestrator.services import memory_service as ms_mod

    svc = ms_mod.MemoryService(base_dir=tmp_path, db_engine=db_engine)
    monkeypatch.setattr(ms_mod, "MEMORY_BASE_DIR", tmp_path)
    Session = sessionmaker(bind=db_engine)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)

    async def _setup():
        for k in ("ghost", "other"):
            await svc.store(content=f"# {k}\nbody", key=k, memory_type="project", scope="global")

    asyncio.run(_setup())
    rel = _svc()
    rel.create("global", None, "ghost", "other", "relates_to", "compiler")

    # Remove the file out-of-band so forget() takes the not-exists branch.
    svc.get_wiki_path("global", None, "ghost").unlink()

    assert asyncio.run(svc.forget("ghost", scope="global", scope_id=None)) is False
    assert (
        rel.list_relationships("global", None, "ghost") == []
    ), "the vanished-file path must purge relationships too"


def test_stale_flag_is_live_after_the_source_is_edited(bound, db_engine):
    """Human review (PR #524): the ``stale`` flag was INERT.

    Staleness is derived from ``row.source_updated_at``, but NO production caller
    ever wrote that column — so the DTO field, the CLI's ``--stale`` and the REST
    ``?stale=true`` could only ever report False. ``replace_set`` now stamps the
    source memory's ``updated_at`` at write time, which makes the whole advertised
    surface real.

    Timeline: write the edge, THEN advance the source memory's updated_at. The
    edge was computed against the older body, so it is stale.
    """
    from datetime import timedelta

    for k in ("s", "t"):
        _seed_memory(db_engine, k)
    Session = sessionmaker(bind=db_engine)

    t0 = datetime.now(timezone.utc) - timedelta(hours=2)
    s = Session()
    try:
        row = s.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "s").first()
        row.updated_at = t0
        s.commit()
    finally:
        s.close()

    svc = _svc()
    svc.replace_set("global", None, "s", "compiler", "relates_to", [EdgeInput("t")])

    # Fresh right after the write — the stamp equals the source's updated_at.
    assert svc.list_relationships("global", None, "s")[0].stale is False

    # The source memory is edited AFTER the edge was computed.
    s = Session()
    try:
        row = s.query(MemoryMetadataModel).filter(MemoryMetadataModel.key == "s").first()
        row.updated_at = t0 + timedelta(hours=1)
        s.commit()
    finally:
        s.close()

    dto = svc.list_relationships("global", None, "s")[0]
    assert dto.stale is True, (
        "an edge computed before the source memory was edited must read stale; "
        "without a source_updated_at writer this can never be True"
    )
    # And the advertised filter surfaces it.
    assert [d.target_key for d in svc.list_relationships("global", None, "s", stale_only=True)] == [
        "t"
    ]


def test_concurrent_create_of_same_tuple_does_not_raise_integrityerror(
    bound, db_engine, monkeypatch
):
    """Human review (PR #524): the dedup race must not surface as a 500.

    ``create()`` is a read-then-insert with no transactional protection, so two
    concurrent creates of the same dedup tuple both miss the existence check and
    both insert. The loser hit the UNIQUE index and raised ``IntegrityError`` —
    not ``ValueError`` — which escaped every REST handler's ValueError->400
    mapping as an unhandled 500.

    Simulates the interleaving deterministically by making the LOSER's existence
    probe return None while a committed row already exists — exactly the state
    the losing thread observes. The probe is blinded via a one-shot flag on the
    service's own query helper rather than a string match on the SQL, so this
    test genuinely exercises the IntegrityError path: removing the handler turns
    it RED (verified by mutation).
    """
    for k in ("s", "t"):
        _seed_memory(db_engine, k)
    svc = _svc()
    first = svc.create("global", None, "s", "t", "relates_to", "human")
    assert first is not None

    # Blind exactly the next existence probe. ``_find_existing`` is the seam
    # create() uses to decide insert-vs-upsert; returning None there puts us on
    # the insert branch with the row already committed => UNIQUE violation.
    real_find = mrs_mod.MemoryRelationshipService._find_existing
    state = {"blinded": False}

    def _blind_once(self, db, scope, sentinel, src, tgt, type_, origin):
        if not state["blinded"]:
            state["blinded"] = True
            return None
        return real_find(self, db, scope, sentinel, src, tgt, type_, origin)

    monkeypatch.setattr(mrs_mod.MemoryRelationshipService, "_find_existing", _blind_once)

    # Must NOT raise IntegrityError; converges on the winning row.
    second = svc.create("global", None, "s", "t", "relates_to", "human")

    assert state["blinded"], "the probe must actually have been blinded"
    assert second.id == first.id, "the racing create must converge on the winning row"
    rows = svc.list_relationships("global", None, "s")
    assert len([r for r in rows if r.target_key == "t"]) == 1, "no duplicate row may land"
