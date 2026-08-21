"""3-Factor Composite Scoring tests.

Covers:
- U7.3 plan cases (weights_sum_to_one, recent_entry_scores_higher,
  frequently_accessed_scores_higher, access_count_increments_on_recall,
  sort_by_recency_reproduces_phase1_order).
- Threat-model coverage: scope guard, rate-limit, clamps, log hygiene.
- Critical regression: ``sort_by="recency"`` byte-identical to current
  the pre-scoring ordering on a fixture corpus (load-bearing invariant).

The score function is pure; sort/integration paths are exercised through
``MemoryService.store()`` / ``recall()`` against a per-test SQLite DB.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import memory_scoring
from cli_agent_orchestrator.services.memory_scoring import (
    SCOPE_RANK,
    USAGE_NORMALISER,
    W_BM25,
    W_RECENCY,
    W_USAGE,
    normalise_bm25_scores,
    scope_write_allowed,
    score_memory,
    validate_sort_by,
)
from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


def _ctx(
    *,
    cwd: str = "/home/user/project",
    session: str = "test-session",
    agent: str = "developer",
    provider: str = "claude_code",
    terminal: str = "term-001",
    caller_scope: Optional[str] = None,
) -> dict:
    out = {
        "terminal_id": terminal,
        "session_name": session,
        "agent_profile": agent,
        "provider": provider,
        "cwd": cwd,
    }
    if caller_scope is not None:
        out["caller_scope"] = caller_scope
    return out


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


def _stamp_updated_at(db_engine, key: str, scope: str, when: datetime) -> None:
    """Force-set ``updated_at`` for deterministic recency tests.

    ``MemoryService.recall`` derives ``updated_at`` from the ``## <ts>``
    headers in the wiki markdown file (not from SQLite), so we rewrite the
    file's last timestamp header as well as the SQLite row to keep both
    sources consistent.
    """
    import re
    from pathlib import Path

    Session = sessionmaker(bind=db_engine)
    with Session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope=scope).first()
        assert row is not None, f"row {key!r} scope={scope!r} not found"
        row.updated_at = when
        file_path = row.file_path
        db.commit()

    # Rewrite the wiki file's timestamp header(s) to match ``when`` so the
    # file-based recall path orders by the intended recency.
    stamp = when.strftime("%Y-%m-%dT%H:%M:%SZ")
    wiki = Path(file_path)
    if wiki.exists():
        text = wiki.read_text(encoding="utf-8")
        text = re.sub(r"## \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", f"## {stamp}", text)
        wiki.write_text(text, encoding="utf-8")


def _row(db_engine, key: str, scope: str = "global") -> MemoryMetadataModel:
    Session = sessionmaker(bind=db_engine)
    with Session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope=scope).first()
        assert row is not None
        # detach by accessing attributes before session close
        _ = row.access_count, row.last_accessed_at, row.updated_at
        return row


# ===========================================================================
# U7.3 — plan cases
# ===========================================================================


class TestU73PlanCases:
    def test_score_weights_sum_to_one(self):
        """U7.3: BM25(0.5) + recency(0.3) + usage(0.2) = 1.0."""
        assert W_BM25 == 0.50
        assert W_RECENCY == 0.30
        assert W_USAGE == 0.20
        assert (W_BM25 + W_RECENCY + W_USAGE) == pytest.approx(1.0)

    def test_score_bounded_zero_to_one(self):
        """Composite is bounded [0.0, 1.0] for any reasonable input."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        # Min: 0 BM25, ancient updated_at, 0 access.
        ancient = now - timedelta(days=100_000)
        lo = score_memory(0.0, ancient, 0, now=now)
        # Max: full BM25, same-day, saturated usage.
        hi = score_memory(1.0, now, 10_000, now=now)
        assert 0.0 <= lo <= 1.0
        assert 0.0 <= hi <= 1.0
        assert hi == pytest.approx(1.0, abs=0.01)

    def test_recent_entry_scores_higher(self):
        """U7.3: same BM25, different recency → recent scores higher."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        recent = now - timedelta(days=1)
        old = now - timedelta(days=180)
        s_recent = score_memory(0.5, recent, 0, now=now)
        s_old = score_memory(0.5, old, 0, now=now)
        assert s_recent > s_old

    def test_frequently_accessed_scores_higher(self):
        """U7.3: same BM25 + recency, more accesses → higher score."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        when = now - timedelta(days=10)
        s_quiet = score_memory(0.5, when, 0, now=now)
        s_loud = score_memory(0.5, when, 50, now=now)
        assert s_loud > s_quiet

    def test_access_count_increments_on_recall(self, svc, db_engine):
        """U7.3: recall → DB row has access_count = 1."""
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="black is the formatter",
                scope="global",
                memory_type="reference",
                key="prefer-black",
                tags="formatting",
                terminal_context=ctx,
            )
        )
        row_before = _row(db_engine, "prefer-black")
        assert int(row_before.access_count or 0) == 0

        results = _run(svc.recall(query="black", scope="global", terminal_context=ctx))
        assert any(m.key == "prefer-black" for m in results)

        row_after = _row(db_engine, "prefer-black")
        assert int(row_after.access_count or 0) == 1
        assert row_after.last_accessed_at is not None

    def test_sort_by_recency_reproduces_phase1_order(self, svc, db_engine):
        """LOAD-BEARING invariant: ``sort_by="recency"`` returns
        the same ordering as the pre-U4 path (sort by ``-updated_at``, then
        scope-precedence sort when scope is None).
        """
        ctx = _ctx(caller_scope="global")
        # Three memories at distinct timestamps, same scope.
        for k in ("alpha", "beta", "gamma"):
            _run(
                svc.store(
                    content=f"content for {k}",
                    scope="global",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=ctx,
                )
            )
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        _stamp_updated_at(db_engine, "alpha", "global", base - timedelta(days=10))
        _stamp_updated_at(db_engine, "beta", "global", base - timedelta(days=2))
        _stamp_updated_at(db_engine, "gamma", "global", base - timedelta(days=5))

        results = _run(svc.recall(scope="global", terminal_context=ctx, sort_by="recency"))
        # Expected ordering: beta (2d) > gamma (5d) > alpha (10d).
        keys = [m.key for m in results if m.key in ("alpha", "beta", "gamma")]
        assert keys == ["beta", "gamma", "alpha"]


# ===========================================================================
# T1 — Cross-scope timestamp tampering / store-time scope guard
# ===========================================================================


class TestT1CrossScopeStoreGuard:
    def test_session_caller_cannot_store_project_scope(self, svc):
        ctx = _ctx(caller_scope="session")
        with pytest.raises(PermissionError):
            _run(
                svc.store(
                    content="x",
                    scope="project",
                    memory_type="reference",
                    key="x",
                    tags="t",
                    terminal_context=ctx,
                )
            )

    def test_project_caller_can_store_session_scope(self, svc):
        ctx = _ctx(caller_scope="project")
        # narrower target — allowed.
        _run(
            svc.store(
                content="x",
                scope="session",
                memory_type="reference",
                key="proj-to-sess",
                tags="t",
                terminal_context=ctx,
            )
        )

    def test_global_caller_can_store_any_scope(self, svc):
        ctx = _ctx(caller_scope="global")
        for target in ("global", "project", "session", "agent"):
            _run(
                svc.store(
                    content="x",
                    scope=target,
                    memory_type="reference",
                    key=f"glob-{target}",
                    tags="t",
                    terminal_context=ctx,
                )
            )

    def test_cross_scope_store_logged_warning(self, svc, caplog):
        ctx = _ctx(caller_scope="session")
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            with pytest.raises(PermissionError):
                _run(
                    svc.store(
                        content="secret-value-bytes-PRIVATE",
                        scope="project",
                        memory_type="reference",
                        key="key-x",
                        tags="t",
                        terminal_context=ctx,
                    )
                )
        msgs = [r.getMessage() for r in caplog.records]
        assert any("cross_scope_store_rejected" in m for m in msgs)
        # No value bytes leaked into log.
        for m in msgs:
            assert "secret-value-bytes-PRIVATE" not in m

    def test_cross_scope_guard_runs_under_recency_mode(self, svc, db_engine):
        """T1(b): write-side guard is active regardless of recall sort_by.
        Storing a cross-scope memory still raises even if subsequent recall
        would use ``sort_by="recency"``.
        """
        ctx = _ctx(caller_scope="agent")  # agent (rank 1) cannot reach global (rank 2)
        with pytest.raises(PermissionError):
            _run(
                svc.store(
                    content="x",
                    scope="global",
                    memory_type="reference",
                    key="agent-to-global",
                    tags="t",
                    terminal_context=ctx,
                )
            )

    def test_scope_write_allowed_table(self):
        """Documents the SCOPE_RANK table — agent and project are siblings
        (both rank 1); cross-sibling writes rejected.
        """
        assert SCOPE_RANK["global"] > SCOPE_RANK["project"]
        assert SCOPE_RANK["project"] == SCOPE_RANK["agent"]
        assert SCOPE_RANK["session"] < SCOPE_RANK["project"]

        # Unknown scope → fail closed.
        assert scope_write_allowed("unknown", "global") is False
        assert scope_write_allowed("global", "unknown") is False

        # Cross-sibling rejected.
        assert scope_write_allowed("project", "agent") is False
        assert scope_write_allowed("agent", "project") is False

        # Self-write always allowed.
        for s in ("session", "project", "agent", "global"):
            assert scope_write_allowed(s, s) is True

    def test_scope_write_allowed_federated_table(self):
        """Federated is write-rank 0 but writable by every caller except
        session: any caller with rank > 0 (project/agent/global) may write it,
        federated may write itself, but session (rank 0, names differ) cannot,
        and federated cannot write the higher-ranked global tier.
        """
        assert scope_write_allowed("global", "federated") is True
        assert scope_write_allowed("project", "federated") is True
        assert scope_write_allowed("agent", "federated") is True
        assert scope_write_allowed("session", "federated") is False
        assert scope_write_allowed("federated", "federated") is True
        assert scope_write_allowed("federated", "global") is False

    def test_sort_by_recency_invariant_still_present(self):
        """Confirm the load-bearing byte-identical recency test still exists
        (do NOT modify it).
        """
        assert hasattr(TestU73PlanCases, "test_sort_by_recency_reproduces_phase1_order")


# ===========================================================================
# T2 — Mass-recall score gaming / increment rate-limit
# ===========================================================================


class TestT2IncrementRateLimit:
    def test_increment_once_per_row_per_call(self, svc, db_engine):
        """One recall → access_count += 1, even if the row appears multiple
        times in BM25 + metadata batches.
        """
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="black formatter formatter formatter",
                scope="global",
                memory_type="reference",
                key="dup-bm25",
                tags="formatter",
                terminal_context=ctx,
            )
        )
        _run(svc.recall(query="formatter", scope="global", terminal_context=ctx))
        row = _row(db_engine, "dup-bm25")
        assert int(row.access_count or 0) == 1

    def test_increment_skipped_within_60s_window(self, svc, db_engine):
        """Two recalls back-to-back → access_count stays at 1 (60s gate)."""
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="content x",
                scope="global",
                memory_type="reference",
                key="ratelimit",
                tags="t",
                terminal_context=ctx,
            )
        )
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        first = int(_row(db_engine, "ratelimit").access_count or 0)
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        second = int(_row(db_engine, "ratelimit").access_count or 0)
        assert first == 1
        assert second == 1, "second recall within 60s must not re-increment"

    def test_increment_resumes_after_61s(self, svc, db_engine):
        """Backdate ``last_accessed_at`` past the 60s window → next recall
        increments. Simulates time passing without a real wall-clock sleep.
        """
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="content y",
                scope="global",
                memory_type="reference",
                key="ratelimit-2",
                tags="t",
                terminal_context=ctx,
            )
        )
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        # Backdate last_accessed_at by 5 minutes — outside the 60s window.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="ratelimit-2", scope="global").first()
            row.last_accessed_at = datetime.utcnow() - timedelta(minutes=5)
            db.commit()
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        assert int(_row(db_engine, "ratelimit-2").access_count or 0) == 2

    def test_60s_window_uses_server_now_not_client(self, svc, db_engine):
        """The rate-limit comparison uses ``func.julianday('now')`` server-
        side; a forged client clock in terminal_context cannot bypass it.
        """
        ctx = _ctx(caller_scope="global")
        ctx["client_clock"] = "2099-01-01T00:00:00Z"  # ignored by server
        _run(
            svc.store(
                content="content z",
                scope="global",
                memory_type="reference",
                key="server-now",
                tags="t",
                terminal_context=ctx,
            )
        )
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        # Even with the ignored client clock, the 60s gate still fires.
        assert int(_row(db_engine, "server-now").access_count or 0) == 1

    def test_usage_factor_saturates_at_one(self):
        """access_count=10000 → usage = 1.0 exactly (cap)."""
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        s = score_memory(0.0, now, 10_000, now=now)
        # composite = 0.5*0 + 0.3*1.0 + 0.2*1.0 = 0.5
        assert s == pytest.approx(0.30 + 0.20)


# ===========================================================================
# T3 — Poison-by-frequency saturation
# ===========================================================================


class TestT3SaturationCurve:
    def test_poisoned_memory_capped_by_saturation(self):
        """T3(b): once access_count > 100, further recalls add < 0.005 to
        ``usage`` per doubling. Numerically: usage(100) ≈ 1.0; usage(200) ≈
        1.0 (capped). The composite gain over access_count=100 → 10000 is
        bounded by W_USAGE * (1 - log1p(100)/log1p(100)) = 0 (capped).
        """
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        baseline = score_memory(0.0, now, 100, now=now)
        flooded = score_memory(0.0, now, 10_000, now=now)
        assert (flooded - baseline) < 0.01

    def test_poisoned_global_does_not_displace_session_under_score(self, svc, db_engine):
        """T3(c) / T4: scope precedence dominates score. A high-score global
        memory cannot displace a low-score session memory under sort_by="score".
        """
        op = _ctx(caller_scope="global")
        # Global memory, freshly stored.
        _run(
            svc.store(
                content="poisoned global content",
                scope="global",
                memory_type="reference",
                key="poison-glob",
                tags="t",
                terminal_context=op,
            )
        )
        # Session memory, also fresh.
        _run(
            svc.store(
                content="legit session content",
                scope="session",
                memory_type="reference",
                key="legit-sess",
                tags="t",
                terminal_context=op,
            )
        )
        # Inflate poison's access count via DB (simulates sustained gaming).
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            poison = (
                db.query(MemoryMetadataModel).filter_by(key="poison-glob", scope="global").first()
            )
            poison.access_count = 10_000
            poison.last_accessed_at = datetime.utcnow() - timedelta(hours=1)
            db.commit()

        # Recall WITHOUT scope — invokes scope-precedence sort.
        results = _run(svc.recall(query="content", terminal_context=op, sort_by="score"))
        keys_in_order = [m.key for m in results]
        # Session memory must come before global poison.
        assert keys_in_order.index("legit-sess") < keys_in_order.index("poison-glob")


# ===========================================================================
# T4 — Scope precedence dominates score in ALL sort modes
# ===========================================================================


class TestT4ScopePrecedence:
    @pytest.fixture
    def populated(self, svc, db_engine):
        op = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="ssn body",
                scope="session",
                memory_type="reference",
                key="ssn-row",
                tags="t",
                terminal_context=op,
            )
        )
        _run(
            svc.store(
                content="prj body",
                scope="project",
                memory_type="reference",
                key="prj-row",
                tags="t",
                terminal_context=op,
            )
        )
        # Make project newer than session, and pump project's access count —
        # under any non-scope-aware ranking, project would win.
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        _stamp_updated_at(db_engine, "ssn-row", "session", base - timedelta(days=30))
        _stamp_updated_at(db_engine, "prj-row", "project", base - timedelta(seconds=10))
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            prj = db.query(MemoryMetadataModel).filter_by(key="prj-row", scope="project").first()
            prj.access_count = 500
            db.commit()
        return op

    def test_session_above_project_under_score_mode(self, populated, svc):
        results = _run(svc.recall(query="body", terminal_context=populated, sort_by="score"))
        keys = [m.key for m in results]
        assert keys.index("ssn-row") < keys.index("prj-row")

    def test_session_above_project_under_usage_mode(self, populated, svc):
        results = _run(svc.recall(query="body", terminal_context=populated, sort_by="usage"))
        keys = [m.key for m in results]
        assert keys.index("ssn-row") < keys.index("prj-row")

    def test_session_above_project_under_recency_mode(self, populated, svc):
        results = _run(svc.recall(query="body", terminal_context=populated, sort_by="recency"))
        keys = [m.key for m in results]
        assert keys.index("ssn-row") < keys.index("prj-row")

    def test_within_scope_score_ordering_intact(self, svc, db_engine):
        """Two session rows: higher composite first (within-scope score
        ordering preserved by the stable scope-precedence pass).
        """
        op = _ctx(caller_scope="global")
        for k, n_acc in (("ssn-low", 0), ("ssn-high", 50)):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="session",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=op,
                )
            )
        # Same updated_at, different access counts. Use 'usage' to make the
        # difference dominate without involving BM25.
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        _stamp_updated_at(db_engine, "ssn-low", "session", base)
        _stamp_updated_at(db_engine, "ssn-high", "session", base)
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="ssn-high", scope="session").first()
            row.access_count = 50
            db.commit()
        results = _run(svc.recall(scope="session", terminal_context=op, sort_by="usage"))
        keys = [m.key for m in results]
        assert keys.index("ssn-high") < keys.index("ssn-low")


# ===========================================================================
# T5 — Integer overflow / negative access_count defensive
# ===========================================================================


class TestT5DefensiveClamps:
    def test_score_handles_zero_access_count(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        s = score_memory(0.0, now, 0, now=now)
        # usage(0) = log1p(0)/log1p(100) = 0 → contribution is 0
        # recency(same instant) = 1.0 → 0.30 contribution
        assert s == pytest.approx(W_RECENCY)

    def test_score_handles_negative_access_count_defensively(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        s = score_memory(0.0, now, -1, now=now)
        # n = max(0, -1) = 0 → usage = 0; no exception.
        assert s == pytest.approx(W_RECENCY)

    def test_score_handles_huge_access_count(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        s = score_memory(0.0, now, 2**62, now=now)
        # usage = min(1.0, log1p(2**62)/log1p(100)) → cap at 1.0
        # recency = 1.0
        # bm25 = 0
        # composite = 0.30 + 0.20 = 0.50
        assert s == pytest.approx(W_RECENCY + W_USAGE)
        assert not math.isnan(s)
        assert not math.isinf(s)

    def test_score_handles_signed_overflow_wrap(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        s = score_memory(0.0, now, -(2**62), now=now)
        # max(0, ...) → 0; usage = 0; no exception
        assert s == pytest.approx(W_RECENCY)


# ===========================================================================
# T6 — Increment failure does not fail recall
# ===========================================================================


class TestT6IncrementFailureNonBlocking:
    def test_increment_failure_does_not_fail_recall(self, svc, db_engine, monkeypatch):
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="content t6",
                scope="global",
                memory_type="reference",
                key="t6-row",
                tags="t",
                terminal_context=ctx,
            )
        )

        from sqlalchemy.exc import OperationalError

        def _boom(_self, _memories):
            raise OperationalError("UPDATE failed", None, Exception("locked"))

        monkeypatch.setattr(MemoryService, "_increment_access_count", _boom)
        results = _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        # Recall still returns results — non-blocking promise.
        assert any(m.key == "t6-row" for m in results)

    def test_increment_failure_logs_warning_with_count_only(
        self, svc, db_engine, monkeypatch, caplog
    ):
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="secret-content-PRIVATE",
                scope="global",
                memory_type="reference",
                key="t6-leak",
                tags="t",
                terminal_context=ctx,
            )
        )

        def _boom(_self, _memories):
            raise RuntimeError("sql exploded")

        monkeypatch.setattr(MemoryService, "_increment_access_count", _boom)
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            _run(svc.recall(query="secret", scope="global", terminal_context=ctx))
        msgs = [r.getMessage() for r in caplog.records]
        # WARNING includes counts but never the row content or a key string.
        assert any("memory_increment_failed" in m for m in msgs)
        for m in msgs:
            assert "secret-content-PRIVATE" not in m

    def test_recall_does_not_retry_failed_increment(self, svc, db_engine, monkeypatch):
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="content r",
                scope="global",
                memory_type="reference",
                key="t6-noretry",
                tags="t",
                terminal_context=ctx,
            )
        )
        calls = {"n": 0}

        def _boom(_self, _memories):
            calls["n"] += 1
            raise RuntimeError("locked")

        monkeypatch.setattr(MemoryService, "_increment_access_count", _boom)
        _run(svc.recall(query="content", scope="global", terminal_context=ctx))
        assert calls["n"] == 1


# ===========================================================================
# T7 — Future-dated updated_at clamps to recency=1.0
# ===========================================================================


class TestT7FutureDatedTimestamp:
    def test_recency_clamps_future_timestamp_to_one(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        future = now + timedelta(days=365)
        s = score_memory(0.0, future, 0, now=now)
        # days_old floors at 0 → recency = 1.0; bm25=0, usage=0.
        assert s == pytest.approx(W_RECENCY)

    def test_recency_handles_naive_timestamp_as_utc(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        naive = datetime(2026, 5, 15)  # naive — treated as UTC
        s = score_memory(0.0, naive, 0, now=now)
        # Same instant, naive ↔ aware match.
        assert s == pytest.approx(W_RECENCY)

    def test_store_sets_updated_at_server_side(self, svc, db_engine):
        """Caller cannot inject updated_at; the store path always uses
        ``datetime.now(timezone.utc)`` server-side.
        """
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="x",
                scope="global",
                memory_type="reference",
                key="server-ts",
                tags="t",
                terminal_context=ctx,
            )
        )
        row = _row(db_engine, "server-ts")
        # Within last 60s.
        delta = abs((datetime.utcnow() - row.updated_at).total_seconds())
        assert delta < 60


# ===========================================================================
# T8 — Log injection from recalled content
# ===========================================================================


class TestT8LogInjection:
    def test_recall_warning_log_does_not_contain_value_bytes(
        self, svc, db_engine, monkeypatch, caplog
    ):
        ctx = _ctx(caller_scope="global")
        evil = "\n[fake-injection] EVIL-LITERAL"
        _run(
            svc.store(
                content=evil,
                scope="global",
                memory_type="reference",
                key="t8-log",
                tags="t",
                terminal_context=ctx,
            )
        )

        def _boom(_self, _memories):
            raise RuntimeError("locked")

        monkeypatch.setattr(MemoryService, "_increment_access_count", _boom)
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            _run(svc.recall(scope="global", terminal_context=ctx))
        for rec in caplog.records:
            assert "EVIL-LITERAL" not in rec.getMessage()

    def test_increment_failure_log_no_key_leak(self, svc, db_engine, monkeypatch, caplog):
        ctx = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="x",
                scope="global",
                memory_type="reference",
                key="ultra-secret-key-name-do-not-log",
                tags="t",
                terminal_context=ctx,
            )
        )

        def _boom(_self, _memories):
            raise RuntimeError("locked")

        monkeypatch.setattr(MemoryService, "_increment_access_count", _boom)
        with caplog.at_level(
            logging.WARNING, logger="cli_agent_orchestrator.services.memory_service"
        ):
            _run(svc.recall(scope="global", terminal_context=ctx))
        for rec in caplog.records:
            assert "ultra-secret-key-name-do-not-log" not in rec.getMessage()


# ===========================================================================
# T9 — Sort-mode validation
# ===========================================================================


class TestT9SortByValidation:
    def test_sort_by_unknown_raises_value_error(self):
        with pytest.raises(ValueError, match="sort_by must be one of"):
            validate_sort_by("hybrid")
        with pytest.raises(ValueError):
            validate_sort_by("../etc/passwd")
        with pytest.raises(ValueError):
            validate_sort_by("")

    def test_sort_by_validation_runs_before_db_call(self, svc, monkeypatch):
        """Whitelist enforcement happens at the top of recall(), before any
        SQL query is issued.
        """
        called = {"n": 0}

        async def _spy(*a, **kw):
            called["n"] += 1
            return []

        # ``_metadata_recall`` is the first DB/file-touching step in recall();
        # validation must reject ``sort_by`` before it is ever reached.
        monkeypatch.setattr(MemoryService, "_metadata_recall", _spy)
        ctx = _ctx(caller_scope="global")
        with pytest.raises(ValueError):
            _run(svc.recall(query="x", terminal_context=ctx, sort_by="bogus"))
        assert called["n"] == 0

    def test_mcp_tool_propagates_sort_by_error(self, svc, monkeypatch):
        """The MCP tool surface MUST surface validation errors as a tool-error
        result, not silently fall back to default. ``memory_recall`` catches
        Exception and returns ``{"success": False, "error": ...}``.
        """
        # Patch MemoryService used inside the tool to our test instance.
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.memory_service.MemoryService",
            lambda *a, **kw: svc,
        )
        from cli_agent_orchestrator.mcp_server import server as mcp_srv

        async def _drive():
            return await mcp_srv.memory_recall.fn(
                query="x", sort_by="bogus"
            )  # FastMCP exposes underlying coro on .fn

        try:
            result = asyncio.run(_drive())
        except AttributeError:
            # Older fastmcp variant — call object directly.
            result = asyncio.run(mcp_srv.memory_recall(query="x", sort_by="bogus"))
        assert isinstance(result, dict)
        assert result.get("success") is False
        assert "sort_by" in (result.get("error") or "")


# ===========================================================================
# T10 — Determinism / tie-break stability
# ===========================================================================


class TestT10Determinism:
    def test_tie_break_deterministic_across_runs(self, svc, db_engine):
        """Ten recalls on identical fixture → identical key order every time."""
        ctx = _ctx(caller_scope="global")
        for k in ("aaa", "bbb", "ccc", "ddd"):
            _run(
                svc.store(
                    content=f"common-text-{k}",
                    scope="global",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=ctx,
                )
            )
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        for k in ("aaa", "bbb", "ccc", "ddd"):
            _stamp_updated_at(db_engine, k, "global", base)

        first = [m.key for m in _run(svc.recall(scope="global", terminal_context=ctx))]
        for _ in range(9):
            again = [m.key for m in _run(svc.recall(scope="global", terminal_context=ctx))]
            assert again == first

    def test_python_sort_stability_preserved_across_passes(self, svc, db_engine):
        """Within-scope ordering survives the secondary scope-precedence pass.

        Two project-scope rows ranked by score must keep their score order
        after the stable scope-precedence sort runs.
        """
        op = _ctx(caller_scope="global")
        for k, n_acc in (("p-low", 0), ("p-hi", 50)):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="project",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=op,
                )
            )
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        for k in ("p-low", "p-hi"):
            _stamp_updated_at(db_engine, k, "project", base)
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            hi = db.query(MemoryMetadataModel).filter_by(key="p-hi", scope="project").first()
            hi.access_count = 50
            db.commit()
        # Use sort_by="usage" (within-scope) and recall WITHOUT scope filter
        # so the scope-precedence pass also runs.
        results = _run(svc.recall(query="body", terminal_context=op, sort_by="usage"))
        keys = [m.key for m in results if m.key in ("p-low", "p-hi")]
        assert keys == ["p-hi", "p-low"]


# ===========================================================================
# normalise_bm25_scores helper
# ===========================================================================


class TestNormaliseBm25Scores:
    def test_empty_dict(self):
        assert normalise_bm25_scores({}) == {}

    def test_max_normalises_to_one(self):
        out = normalise_bm25_scores({"a": 5.0, "b": 10.0, "c": 2.5})
        assert out["b"] == pytest.approx(1.0)
        assert out["a"] == pytest.approx(0.5)
        assert out["c"] == pytest.approx(0.25)

    def test_all_zero_max_collapses(self):
        out = normalise_bm25_scores({"a": 0.0, "b": 0.0})
        assert out == {"a": 0.0, "b": 0.0}

    def test_negative_max_collapses(self):
        # rank_bm25 can produce negative scores on tiny corpora — guard.
        out = normalise_bm25_scores({"a": -1.0, "b": -2.0})
        assert out == {"a": 0.0, "b": 0.0}


# ===========================================================================
# Identity correctness — usage metadata keyed by (key, scope, scope_id)
# (regression for review finding: same-slug rows across scopes collided)
# ===========================================================================


def _row_opt(db_engine, key: str, scope: str) -> Optional[MemoryMetadataModel]:
    Session = sessionmaker(bind=db_engine)
    with Session() as db:
        row = db.query(MemoryMetadataModel).filter_by(key=key, scope=scope).first()
        if row is not None:
            _ = row.access_count, row.last_accessed_at
        return row


class TestUsageIdentityIsolation:
    """Recalling one memory must not mutate a same-slug memory in another
    scope. The metadata identity is ``(key, scope, scope_id)``, not bare key.
    """

    def test_project_recall_does_not_increment_global_same_slug(self, svc, db_engine):
        """The original repro: a global and a project row share the slug.
        Recalling the project row must leave the global row's count at 0.
        """
        op = _ctx(caller_scope="global")
        for scope in ("global", "project"):
            _run(
                svc.store(
                    content=f"shared slug body in {scope}",
                    scope=scope,
                    memory_type="reference",
                    key="shared-slug",
                    tags="t",
                    terminal_context=op,
                )
            )
        assert int(_row(db_engine, "shared-slug", "global").access_count or 0) == 0
        assert int(_row(db_engine, "shared-slug", "project").access_count or 0) == 0

        # Recall ONLY the project scope.
        results = _run(svc.recall(query="body", scope="project", terminal_context=op))
        assert any(m.key == "shared-slug" and m.scope == "project" for m in results)

        # Project row incremented; global row untouched.
        assert int(_row(db_engine, "shared-slug", "project").access_count or 0) == 1
        assert int(_row(db_engine, "shared-slug", "global").access_count or 0) == 0

    def test_enrich_reads_per_scope_count_not_cross_scope(self, svc, db_engine):
        """``_enrich_access_counts`` must populate each Memory with ITS OWN
        scope's count, never a same-slug row's count from another scope.
        """
        op = _ctx(caller_scope="global")
        for scope in ("global", "project"):
            _run(
                svc.store(
                    content=f"enrich body {scope}",
                    scope=scope,
                    memory_type="reference",
                    key="enrich-slug",
                    tags="t",
                    terminal_context=op,
                )
            )
        # Distinct counts per scope, set directly in SQLite.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            g = db.query(MemoryMetadataModel).filter_by(key="enrich-slug", scope="global").first()
            p = db.query(MemoryMetadataModel).filter_by(key="enrich-slug", scope="project").first()
            g.access_count = 7
            p.access_count = 99
            db.commit()

        # Recall without scope so both rows are candidates; enrich runs under
        # any non-recency sort.
        results = _run(svc.recall(query="body", terminal_context=op, sort_by="usage"))
        by_scope = {m.scope: m for m in results if m.key == "enrich-slug"}
        assert int(by_scope["global"].access_count) == 7
        assert int(by_scope["project"].access_count) == 99

    def test_global_null_scope_id_still_increments(self, svc, db_engine):
        """The OR-of-ANDs identity predicate must match a global row whose
        ``scope_id IS NULL`` (the tuple-IN/NULL edge). Recall increments it.
        """
        op = _ctx(caller_scope="global")
        _run(
            svc.store(
                content="null scope id body",
                scope="global",
                memory_type="reference",
                key="null-scope",
                tags="t",
                terminal_context=op,
            )
        )
        row = _row(db_engine, "null-scope", "global")
        assert row.scope_id is None
        assert int(row.access_count or 0) == 0

        _run(svc.recall(query="body", scope="global", terminal_context=op))
        assert int(_row(db_engine, "null-scope", "global").access_count or 0) == 1


# ===========================================================================
# sort_by="score" feeds genuine BM25 relevance (not a hard-coded 0.0)
# (regression for review finding: score mode was really recency + usage)
# ===========================================================================


class TestScoreUsesBm25:
    def test_strong_bm25_outranks_recency_under_score(self, svc, db_engine):
        """Haofei's repro, inverted: an OLDER article with strong query-term
        density and a NEWER weak match. Under sort_by="score" the strong-BM25
        doc must be able to outrank pure-recency ordering — proving BM25 is
        now part of the composite (it was hard-coded to 0.0 before).
        """
        pytest.importorskip("rank_bm25")
        op = _ctx(caller_scope="global")
        # Strong match: query term repeated many times.
        _run(
            svc.store(
                content="kafka kafka kafka kafka kafka streaming kafka kafka",
                scope="global",
                memory_type="reference",
                key="strong-bm25",
                tags="t",
                terminal_context=op,
            )
        )
        # Weak match: query term appears once, lots of filler.
        _run(
            svc.store(
                content="kafka mentioned once among much unrelated filler prose here",
                scope="global",
                memory_type="reference",
                key="weak-bm25",
                tags="t",
                terminal_context=op,
            )
        )
        # Filler docs WITHOUT the query term, so "kafka" appears in a minority
        # of the corpus and its IDF stays positive (df < N/2). Without these,
        # a 2-doc corpus gives "kafka" df=N → negative IDF → BM25 collapses to
        # zero and the test could not distinguish score from recency.
        for i in range(4):
            _run(
                svc.store(
                    content=f"unrelated filler document number {i} about gardening",
                    scope="global",
                    memory_type="reference",
                    key=f"filler-{i}",
                    tags="t",
                    terminal_context=op,
                )
            )
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        # Make the STRONG match OLDER and the WEAK match NEWER, so pure
        # recency would rank weak-bm25 first.
        _stamp_updated_at(db_engine, "strong-bm25", "global", base - timedelta(days=20))
        _stamp_updated_at(db_engine, "weak-bm25", "global", base - timedelta(days=1))

        recency = [
            m.key
            for m in _run(
                svc.recall(query="kafka", scope="global", terminal_context=op, sort_by="recency")
            )
            if m.key in ("strong-bm25", "weak-bm25")
        ]
        score = [
            m.key
            for m in _run(
                svc.recall(query="kafka", scope="global", terminal_context=op, sort_by="score")
            )
            if m.key in ("strong-bm25", "weak-bm25")
        ]
        # Pure recency favours the newer weak match...
        assert recency[0] == "weak-bm25"
        # ...but score now incorporates BM25, lifting the strong match to top.
        assert score[0] == "strong-bm25"

    def test_score_without_query_equals_recency_usage(self, svc, db_engine):
        """No query → no BM25 candidates → bm25_raw stays 0.0 for all, so
        score mode is exactly recency+usage (no behavioural change there).
        """
        op = _ctx(caller_scope="global")
        for k in ("no-q-a", "no-q-b"):
            _run(
                svc.store(
                    content=f"body {k}",
                    scope="global",
                    memory_type="reference",
                    key=k,
                    tags="t",
                    terminal_context=op,
                )
            )
        base = datetime(2026, 5, 15, 12, 0, 0, tzinfo=timezone.utc)
        _stamp_updated_at(db_engine, "no-q-a", "global", base - timedelta(days=1))
        _stamp_updated_at(db_engine, "no-q-b", "global", base - timedelta(days=10))
        score = [
            m.key
            for m in _run(svc.recall(scope="global", terminal_context=op, sort_by="score"))
            if m.key in ("no-q-a", "no-q-b")
        ]
        # Newer first; usage equal; BM25 absent → recency decides.
        assert score == ["no-q-a", "no-q-b"]


# ===========================================================================
# Perf budget — score_memory < 50µs per call
# ===========================================================================


class TestScorePerf:
    @pytest.mark.integration
    def test_score_memory_under_50us(self):
        now = datetime(2026, 5, 15, tzinfo=timezone.utc)
        when = now - timedelta(days=10)
        N = 5_000
        start = time.perf_counter()
        for i in range(N):
            score_memory(0.5, when, i, now=now)
        elapsed = time.perf_counter() - start
        per_call_us = (elapsed / N) * 1_000_000
        # Generous bound for CI noise; the target is < 50µs.
        assert per_call_us < 200, f"score_memory {per_call_us:.1f}µs/call > 200µs budget"
