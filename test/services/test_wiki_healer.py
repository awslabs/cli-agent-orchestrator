"""Wiki Healer tests (Phase 4 U1).

Covers the locked decisions and design invariants:

- Dry-run default mutates NOTHING (no file, no DB).
- Each of the 3 fixes (orphan_page, contradiction, stale_claim) applied.
- poison_frequency gated behind --apply AND --aggressive (dual gate).
- graph_density never mutates / never reported.
- lint_error bookkeeping rows ignored.
- caps / truncation reported, no silent drops.
- audit event emitted per applied mutation + one heal_run_completed.
- SQL-row-authoritative behaviour for contradiction / poison.
- concurrency lock (.heal.lock) raises HealConflictError.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import audit_log, wiki_healer
from cli_agent_orchestrator.services.memory_service import MemoryService
from cli_agent_orchestrator.services.wiki_healer import (
    MAX_HEAL_ACTIONS,
    STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES,
    HealConflictError,
    _parse_stale_identifier,
    _strip_stale_paragraph,
    heal,
)
from cli_agent_orchestrator.services.wiki_lint import _make_issue


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db_engine(tmp_path):
    db_path = tmp_path / "test.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    return engine


@pytest.fixture
def svc(tmp_path, db_engine):
    return MemoryService(base_dir=tmp_path, db_engine=db_engine)


@pytest.fixture
def audit_base(tmp_path, monkeypatch):
    """Redirect audit log writes to a tmp dir so we can assert on emitted events."""
    base = tmp_path / "audit-base"
    base.mkdir()
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", base)
    return base


def _read_audit(base: Path) -> str:
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    p = base / "logs" / "memory" / f"{date_str}.md"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def _store(svc, key, content="body content here", *, scope="global", tags="t"):
    return _run(
        svc.store(content=content, scope=scope, memory_type="reference", key=key, tags=tags)
    )


def _row(svc, key, scope="global", scope_id=None):
    with svc._get_db_session() as db:
        q = db.query(MemoryMetadataModel).filter(
            MemoryMetadataModel.key == key,
            MemoryMetadataModel.scope == scope,
            (
                MemoryMetadataModel.scope_id == scope_id
                if scope_id is not None
                else MemoryMetadataModel.scope_id.is_(None)
            ),
        )
        return q.first()


# ===========================================================================
# Strip algorithm unit tests
# ===========================================================================


class TestStripAlgorithm:
    def test_parse_file_identifier(self):
        assert _parse_stale_identifier("file not found: src/config.py") == "src/config.py"

    def test_parse_symbol_identifier(self):
        assert _parse_stale_identifier("symbol not found in source: MY_FUNC") == "MY_FUNC"

    def test_parse_unknown_returns_none(self):
        assert _parse_stale_identifier("something else entirely") is None

    def test_strip_first_matching_paragraph(self):
        content = (
            "First paragraph fine.\n"
            "Still fine.\n"
            "\n"
            "This one mentions src/config.py and is stale.\n"
            "\n"
            "Last paragraph fine.\n"
        )
        new, pre = _strip_stale_paragraph(content, "src/config.py")
        assert pre is not None
        assert "src/config.py" in pre
        assert "src/config.py" not in new
        assert "First paragraph fine." in new
        assert "Last paragraph fine." in new

    def test_strip_stops_at_first_match(self):
        content = "para one foo\n\npara two foo\n"
        new, pre = _strip_stale_paragraph(content, "foo")
        # First paragraph stripped, second remains.
        assert "para one" not in new
        assert "para two foo" in new

    def test_word_boundary_no_partial_match(self):
        content = "we reconfigured the system here\n"
        new, pre = _strip_stale_paragraph(content, "config")
        assert pre is None  # "config" inside "reconfigured" must NOT match
        assert new == content

    def test_no_match_returns_unchanged(self):
        content = "nothing relevant here\n"
        new, pre = _strip_stale_paragraph(content, "absent.py")
        assert pre is None
        assert new == content


# ===========================================================================
# Dry-run: mutates nothing
# ===========================================================================


class TestDryRun:
    def test_dry_run_no_file_or_db_mutation(self, svc, audit_base):
        _store(svc, "orphan-one")
        wiki_path = svc.get_wiki_path("global", None, "orphan-one")
        assert wiki_path.exists()

        issues = [_make_issue(issue_type="orphan_page", key="orphan-one", description="orphan")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=False, svc=svc))

        assert report.apply is False
        assert report.dry_run_summary is not None
        assert all(a.status == "planned" for a in report.actions)
        # Nothing deleted.
        assert wiki_path.exists()
        assert _row(svc, "orphan-one") is not None

    def test_dry_run_emits_only_run_completed(self, svc, audit_base):
        _store(svc, "orphan-one")
        issues = [_make_issue(issue_type="orphan_page", key="orphan-one")]
        _run(heal(issues, scope="global", scope_id=None, apply=False, svc=svc))
        log = _read_audit(audit_base)
        assert "[heal_run_completed]" in log
        assert "[orphan_pruned]" not in log
        assert "apply=false" in log


# ===========================================================================
# orphan_page fix
# ===========================================================================


class TestOrphanFix:
    def test_orphan_applied_deletes_everything(self, svc, audit_base):
        _store(svc, "stale-orphan")
        wiki_path = svc.get_wiki_path("global", None, "stale-orphan")
        assert wiki_path.exists()
        assert _row(svc, "stale-orphan") is not None

        issues = [_make_issue(issue_type="orphan_page", key="stale-orphan")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))

        assert report.apply is True
        assert len(report.actions) == 1
        assert report.actions[0].status == "applied"
        assert report.actions[0].issue_type == "orphan_pruned"
        assert not wiki_path.exists()
        assert _row(svc, "stale-orphan") is None

        log = _read_audit(audit_base)
        assert "[orphan_pruned]" in log
        assert "key=stale-orphan" in log

    def test_orphan_missing_file_still_cleans(self, svc, audit_base):
        # No file at all — healer should not crash, should emit audit.
        issues = [_make_issue(issue_type="orphan_page", key="ghost")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert report.actions[0].status == "applied"
        assert "[orphan_pruned]" in _read_audit(audit_base)


# ===========================================================================
# contradiction fix (SQL-row authoritative)
# ===========================================================================


class TestContradictionFix:
    def test_keeps_newer_forgets_older(self, svc, audit_base):
        _store(svc, "older")
        _store(svc, "newer")
        # Force the timestamps so "newer" wins regardless of clock resolution.
        with svc._get_db_session() as db:
            old_row = db.query(MemoryMetadataModel).filter_by(key="older").first()
            new_row = db.query(MemoryMetadataModel).filter_by(key="newer").first()
            old_row.updated_at = datetime(2020, 1, 1, tzinfo=timezone.utc)
            new_row.updated_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
            db.commit()

        older_path = svc.get_wiki_path("global", None, "older")
        newer_path = svc.get_wiki_path("global", None, "newer")

        issues = [
            _make_issue(
                issue_type="contradiction",
                key="older",
                related_key="newer",
                description="they disagree",
            )
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))

        assert report.actions[0].status == "applied"
        # Loser (older) gone; winner (newer) kept.
        assert not older_path.exists()
        assert newer_path.exists()
        assert _row(svc, "older") is None
        assert _row(svc, "newer") is not None

        log = _read_audit(audit_base)
        assert "[contradiction_resolved]" in log
        assert "winner_key=newer" in log
        assert "loser_key=older" in log

    def test_sql_authoritative_skip_when_row_missing(self, svc, audit_base):
        # Only one of the two rows exists in DB → SKIP (trust DB, not payload).
        _store(svc, "only-one")
        issues = [_make_issue(issue_type="contradiction", key="only-one", related_key="phantom")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert report.actions[0].status == "skipped"
        # Nothing forgotten.
        assert _row(svc, "only-one") is not None

    def test_same_second_tie_is_deterministic_keep_smaller_key(self, svc, audit_base):
        # Equal updated_at → deterministic tiebreak keeps the lexicographically
        # smaller key ("alpha"), regardless of which side is key_a/related_key.
        _store(svc, "alpha")
        _store(svc, "bravo")
        same = datetime(2026, 1, 1, tzinfo=timezone.utc)
        with svc._get_db_session() as db:
            for k in ("alpha", "bravo"):
                db.query(MemoryMetadataModel).filter_by(key=k).first().updated_at = same
            db.commit()

        # Present the pair "loser-order" (bravo as key_a) to prove order doesn't decide.
        issues = [_make_issue(issue_type="contradiction", key="bravo", related_key="alpha")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))

        assert report.actions[0].status == "applied"
        assert _row(svc, "alpha") is not None  # smaller key survives the tie
        assert _row(svc, "bravo") is None
        assert "winner_key=alpha" in _read_audit(audit_base)


# ===========================================================================
# stale_claim fix
# ===========================================================================


class TestStaleClaimFix:
    def test_strips_paragraph_and_rewrites(self, svc, audit_base):
        body = (
            "Intro paragraph that is fine.\n"
            "\n"
            "This refers to src/gone.py which no longer exists.\n"
            "\n"
            "Closing paragraph that is fine.\n"
        )
        _store(svc, "article-x", content=body)
        wiki_path = svc.get_wiki_path("global", None, "article-x")

        issues = [
            _make_issue(
                issue_type="stale_claim",
                key="article-x",
                description="file not found: src/gone.py",
                severity="error",
            )
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))

        assert report.actions[0].status == "applied"
        assert report.actions[0].pre_strip_paragraph is not None
        assert "src/gone.py" in report.actions[0].pre_strip_paragraph

        new_content = wiki_path.read_text(encoding="utf-8")
        assert "src/gone.py" not in new_content
        assert "Intro paragraph" in new_content
        assert "Closing paragraph" in new_content

        log = _read_audit(audit_base)
        assert "[stale_claim_pruned]" in log
        assert "stale_identifier=src/gone.py" in log

    def test_unparseable_description_skipped(self, svc, audit_base):
        _store(svc, "article-y", content="some body\n")
        issues = [
            _make_issue(issue_type="stale_claim", key="article-y", description="weird format")
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert report.actions[0].status == "skipped"

    def test_dry_run_does_not_count_unparseable_as_actionable(self, svc, audit_base):
        # The "related_keys references missing key:" stale_claim sub-type has no
        # paragraph to strip; the dry-run plan must show it skipped, not planned,
        # and must not advertise it in the "Would apply N" count.
        issues = [
            _make_issue(
                issue_type="stale_claim",
                key="article-q",
                description="related_keys references missing key: gone-topic",
            )
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=False, svc=svc))
        assert report.actions[0].status == "skipped"
        assert "Would apply 0 of 1" in (report.dry_run_summary or "")

    def test_pre_strip_size_capped(self, svc, audit_base):
        huge_para = "src/gone.py " + ("X" * (STALE_CLAIM_PRESTRIP_PARAGRAPH_MAX_BYTES + 500))
        body = f"intro\n\n{huge_para}\n\noutro\n"
        _store(svc, "article-z", content=body)
        issues = [
            _make_issue(
                issue_type="stale_claim",
                key="article-z",
                description="file not found: src/gone.py",
            )
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        pre = report.actions[0].pre_strip_paragraph
        assert pre is not None
        assert pre.endswith("[…truncated]")


# ===========================================================================
# poison_frequency dual gate
# ===========================================================================


class TestPoisonGate:
    def _seed_poison(self, svc):
        _store(svc, "poisoned")
        with svc._get_db_session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="poisoned").first()
            row.access_count = 999
            db.commit()

    def test_apply_without_aggressive_skips_poison(self, svc, audit_base):
        self._seed_poison(svc)
        issues = [_make_issue(issue_type="poison_frequency", key="poisoned")]
        report = _run(
            heal(issues, scope="global", scope_id=None, apply=True, aggressive=False, svc=svc)
        )
        # Not even reported.
        assert report.actions == []
        assert int(_row(svc, "poisoned").access_count) == 999
        assert "[poison_access_zeroed]" not in _read_audit(audit_base)

    def test_dry_run_aggressive_does_not_mutate(self, svc, audit_base):
        self._seed_poison(svc)
        issues = [_make_issue(issue_type="poison_frequency", key="poisoned")]
        report = _run(
            heal(issues, scope="global", scope_id=None, apply=False, aggressive=True, svc=svc)
        )
        # apply=False → poison gated out entirely (dual gate needs apply too).
        assert report.actions == []
        assert int(_row(svc, "poisoned").access_count) == 999

    def test_apply_and_aggressive_zeroes(self, svc, audit_base):
        self._seed_poison(svc)
        issues = [_make_issue(issue_type="poison_frequency", key="poisoned")]
        report = _run(
            heal(issues, scope="global", scope_id=None, apply=True, aggressive=True, svc=svc)
        )
        assert report.actions[0].status == "applied"
        assert int(_row(svc, "poisoned").access_count) == 0
        log = _read_audit(audit_base)
        assert "[poison_access_zeroed]" in log
        assert "access_count_was=999" in log


# ===========================================================================
# Audit emitted only AFTER a successful commit (rollback drops the audit)
# ===========================================================================


class TestAuditAfterCommit:
    def _force_commit_failure(self, svc, monkeypatch):
        """Wrap _get_db_session so the returned session's commit() raises."""
        real_get = svc._get_db_session

        def _patched():
            db = real_get()
            monkeypatch.setattr(db, "commit", _raise_commit)
            return db

        def _raise_commit():
            raise RuntimeError("simulated commit failure")

        monkeypatch.setattr(svc, "_get_db_session", _patched)

    def test_poison_rollback_emits_no_audit_and_keeps_count(self, svc, audit_base, monkeypatch):
        # poison_frequency's ONLY side effect is the in-session DB write. A
        # commit failure must roll it back AND leave no false audit record.
        _store(svc, "poisoned")
        with svc._get_db_session() as db:
            db.query(MemoryMetadataModel).filter_by(key="poisoned").first().access_count = 999
            db.commit()

        self._force_commit_failure(svc, monkeypatch)
        issues = [_make_issue(issue_type="poison_frequency", key="poisoned")]
        report = _run(
            heal(issues, scope="global", scope_id=None, apply=True, aggressive=True, svc=svc)
        )

        assert report.actions[0].status == "error"
        # Row reverted by rollback — count never zeroed.
        assert int(_row(svc, "poisoned").access_count) == 999
        # And crucially: NO mutation audit recorded for the rolled-back write.
        log = _read_audit(audit_base)
        assert "[poison_access_zeroed]" not in log

    def test_orphan_rollback_emits_no_mutation_audit(self, svc, audit_base, monkeypatch):
        _store(svc, "orphan-rb")
        self._force_commit_failure(svc, monkeypatch)
        issues = [_make_issue(issue_type="orphan_page", key="orphan-rb")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))

        assert report.actions[0].status == "error"
        assert "[orphan_pruned]" not in _read_audit(audit_base)

    def test_successful_commit_still_emits_audit(self, svc, audit_base):
        # Control: the happy path still emits the mutation audit (post-commit).
        _store(svc, "orphan-ok")
        issues = [_make_issue(issue_type="orphan_page", key="orphan-ok")]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert report.actions[0].status == "applied"
        assert "[orphan_pruned]" in _read_audit(audit_base)


# ===========================================================================
# Skipped no-ops must not consume the cap budget
# ===========================================================================


class TestSkippedDoesNotConsumeCap:
    def test_skipped_stale_claim_does_not_eat_cap(self, svc, audit_base, monkeypatch):
        # Cap stale_claim at 1. An unparseable (skipped) issue ordered first must
        # NOT consume that single slot — the real fix after it should still apply.
        monkeypatch.setitem(wiki_healer.ISSUE_CAPS, "stale_claim", 1)
        _store(svc, "real-article", content="intro\n\nrefers to src/gone.py here\n\noutro\n")
        issues = [
            _make_issue(issue_type="stale_claim", key="bogus", description="weird format"),
            _make_issue(
                issue_type="stale_claim",
                key="real-article",
                description="file not found: src/gone.py",
            ),
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        statuses = {a.key: a.status for a in report.actions}
        assert statuses["bogus"] == "skipped"
        assert statuses["real-article"] == "applied"  # not crowded out by the skip
        assert report.truncated_by_type.get("stale_claim", 0) == 0

    def test_dry_run_skipped_does_not_eat_cap(self, svc, audit_base, monkeypatch):
        monkeypatch.setitem(wiki_healer.ISSUE_CAPS, "stale_claim", 1)
        _store(svc, "real-article", content="intro\n\nrefers to src/gone.py here\n\noutro\n")
        issues = [
            _make_issue(issue_type="stale_claim", key="bogus", description="weird format"),
            _make_issue(
                issue_type="stale_claim",
                key="real-article",
                description="file not found: src/gone.py",
            ),
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=False, svc=svc))
        statuses = {a.key: a.status for a in report.actions}
        assert statuses["bogus"] == "skipped"
        assert statuses["real-article"] == "planned"
        assert report.truncated_by_type.get("stale_claim", 0) == 0


# ===========================================================================
# graph_density flag-only + lint_error ignored
# ===========================================================================


class TestFilterRules:
    def test_graph_density_never_mutates(self, svc, audit_base):
        _store(svc, "popular")
        issues = [_make_issue(issue_type="graph_density", key="popular", description="hot")]
        report = _run(
            heal(issues, scope="global", scope_id=None, apply=True, aggressive=True, svc=svc)
        )
        assert report.actions == []
        assert _row(svc, "popular") is not None

    def test_lint_error_rows_ignored(self, svc, audit_base):
        issues = [
            _make_issue(
                issue_type="lint_error", key="run_lint", description="lint_run_completed: 5/5"
            ),
            _make_issue(issue_type="lint_error", key="orphan_page", description="truncated"),
        ]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert report.actions == []


# ===========================================================================
# Caps / truncation reporting
# ===========================================================================


class TestCaps:
    def test_per_type_cap_reported(self, svc, audit_base, monkeypatch):
        # Shrink the orphan cap so we can exercise truncation cheaply.
        monkeypatch.setitem(wiki_healer.ISSUE_CAPS, "orphan_page", 2)
        for i in range(5):
            _store(svc, f"orphan-{i}")
        issues = [_make_issue(issue_type="orphan_page", key=f"orphan-{i}") for i in range(5)]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert len([a for a in report.actions if a.status == "applied"]) == 2
        assert report.truncated_by_type.get("orphan_page") == 3
        assert report.total_suppressed == 3
        log = _read_audit(audit_base)
        assert "truncation_breakdown=orphan_page:3" in log

    def test_run_level_cap_reported(self, svc, audit_base, monkeypatch):
        monkeypatch.setattr(wiki_healer, "MAX_HEAL_ACTIONS", 2)
        for i in range(4):
            _store(svc, f"orphan-{i}")
        issues = [_make_issue(issue_type="orphan_page", key=f"orphan-{i}") for i in range(4)]
        report = _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        assert len(report.actions) == 2
        assert report.truncated_run_level == 2
        assert report.total_suppressed == 2


# ===========================================================================
# Concurrency lock
# ===========================================================================


class TestLock:
    def test_conflict_when_lock_held(self, svc, audit_base):
        lock_path = wiki_healer._heal_lock_path(svc, "global", None)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _store(svc, "orphan-one")
            issues = [_make_issue(issue_type="orphan_page", key="orphan-one")]
            with pytest.raises(HealConflictError):
                _run(heal(issues, scope="global", scope_id=None, apply=True, svc=svc))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_no_lock_for_dry_run(self, svc, audit_base):
        # Even with the lock held, a dry-run must not require it.
        lock_path = wiki_healer._heal_lock_path(svc, "global", None)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(lock_path), os.O_WRONLY | os.O_CREAT, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            _store(svc, "orphan-one")
            issues = [_make_issue(issue_type="orphan_page", key="orphan-one")]
            report = _run(heal(issues, scope="global", scope_id=None, apply=False, svc=svc))
            assert report.apply is False
            assert report.actions[0].status == "planned"
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
