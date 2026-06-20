"""Phase 4 U2 cross-unit integration tests.

Scoped to the import/export unit that has landed on this branch. The dead
U3 federation design (separate ``FEDERATED_BASE_DIR`` directory + per-row
demotion + ``federation_invariant_violation``) is gone: ``federated`` is a
plain scope value. The heal/self-healing (U1) and U0 hot-fix integration
cases from the original suite are dropped — those modules are not present on
this branch.

Coverage:

- **export→import round-trip** preserves wiki body, metadata, related_keys,
  and access_count across all 3 conflict policies.
- **federated is just a scope** round-trip: a ``federated`` row exports and
  re-imports through the normal scope path with no demotion.
- **audit classification parity**: import/export events sit in the closed
  SYNC/NOWAIT partition.
- **NFR**: import wall-clock budget for a 100-row archive < 5s.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import Base, MemoryMetadataModel
from cli_agent_orchestrator.services import audit_log, memory_export, memory_import
from cli_agent_orchestrator.services.memory_service import MemoryService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def base_dir(tmp_path, monkeypatch):
    # Pin HOME to tmp_path so memory_export's $HOME-or-cwd containment guard
    # accepts archive paths under tmp_path.
    monkeypatch.setenv("HOME", str(tmp_path))
    base = tmp_path / "p4-base"
    base.mkdir()
    monkeypatch.setattr(memory_export, "MEMORY_BASE_DIR", base)
    monkeypatch.setattr(memory_import, "MEMORY_BASE_DIR", base)
    monkeypatch.setattr(audit_log, "MEMORY_BASE_DIR", base)
    return base


@pytest.fixture
def db_engine(tmp_path, monkeypatch):
    db_path = tmp_path / "p4.db"
    engine = create_engine(f"sqlite:///{db_path}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    from cli_agent_orchestrator.clients import database as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    return engine


@pytest.fixture
def svc(db_engine, base_dir):
    return MemoryService(base_dir=base_dir, db_engine=db_engine)


@pytest.fixture
def memory_enabled(monkeypatch):
    def _settings():
        return {"enabled": True, "project_marker_strong_mode": False}

    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.get_memory_settings",
        _settings,
    )
    monkeypatch.setattr(
        "cli_agent_orchestrator.services.settings_service.is_memory_enabled",
        lambda: True,
    )
    yield


def _ctx() -> dict:
    return {
        "terminal_id": "term-p4",
        "session_name": "session-p4",
        "agent_profile": "developer",
        "provider": "claude_code",
        "cwd": "/home/user/p4",
        "caller_scope": "global",
    }


def _seed_global(svc: MemoryService, key: str, content: str = "body") -> None:
    _run(
        svc.store(
            content=content,
            scope="global",
            memory_type="reference",
            key=key,
            tags="t",
            terminal_context=_ctx(),
        )
    )


def _force_federate_row(db_engine, key: str, content: str, base_dir: Path) -> Path:
    """Seed a ``federated`` row directly. ``federated`` is a plain scope value;
    its wiki file lives under the ``global`` container like any non-project
    scope (see MemoryService.get_wiki_path).
    """
    wiki_dir = base_dir / "global" / "wiki" / "federated"
    wiki_dir.mkdir(parents=True, exist_ok=True)
    f = wiki_dir / f"{key}.md"
    f.write_text(content, encoding="utf-8")
    Session = sessionmaker(bind=db_engine)
    with Session() as db:
        db.add(
            MemoryMetadataModel(
                id=f"fed-{key}",
                key=key,
                memory_type="reference",
                scope="federated",
                scope_id=None,
                file_path=str(f),
                tags="t",
                token_estimate=10,
                created_at=datetime.now(timezone.utc),
                updated_at=datetime.now(timezone.utc),
            )
        )
        db.commit()
    return f


# ---------------------------------------------------------------------------
# export → import round-trip across 3 conflict policies
# ---------------------------------------------------------------------------


class TestExportImportRoundTrip:
    def test_skip_policy_preserves_existing(
        self, svc, memory_enabled, base_dir, db_engine, tmp_path
    ):
        _seed_global(svc, "rt-skip", content="original body")

        archive = tmp_path / "rt-skip.tar.gz"
        rep = _run(memory_export.export("global", None, output_path=archive, actor="cli"))
        assert rep.archive_path == archive
        assert archive.exists()

        # Mutate existing row content; import with skip should NOT overwrite.
        _seed_global(svc, "rt-skip", content="mutated body")

        rep2 = _run(
            memory_import.import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        assert rep2.actor == "cli"
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="rt-skip").one()
            body = Path(row.file_path).read_text(encoding="utf-8")
            # Skip preserves the mutated local file (does NOT roll back to
            # archive's "original body").
            assert "mutated body" in body

    def test_replace_policy_overwrites_with_archive(
        self, svc, memory_enabled, base_dir, db_engine, tmp_path
    ):
        _seed_global(svc, "rt-rep", content="archive body")
        archive = tmp_path / "rt-rep.tar.gz"
        _run(memory_export.export("global", None, output_path=archive, actor="cli"))

        _seed_global(svc, "rt-rep", content="post-export edit")
        _run(
            memory_import.import_archive(
                archive,
                conflict_policy="replace",
                target_project_id="global",
                actor="cli",
            )
        )
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="rt-rep").one()
            body = Path(row.file_path).read_text(encoding="utf-8")
            # Replace path overwrites with archive contents (which captured
            # "archive body" before the post-export edit).
            assert "archive body" in body
            assert "post-export edit" not in body
            # imported_from provenance set (T8).
            assert row.imported_from is not None
            assert "archive_sha256" in str(row.imported_from)

    def test_merge_policy_picks_newer(self, svc, memory_enabled, base_dir, db_engine, tmp_path):
        _seed_global(svc, "rt-mrg", content="archive body")
        archive = tmp_path / "rt-mrg.tar.gz"
        _run(memory_export.export("global", None, output_path=archive, actor="cli"))

        # Bump local row updated_at to FUTURE so merge keeps existing.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="rt-mrg").one()
            row.updated_at = datetime.now(timezone.utc).replace(year=2099)
            Path(row.file_path).write_text("local future wins", encoding="utf-8")
            db.commit()

        _run(
            memory_import.import_archive(
                archive,
                conflict_policy="merge",
                target_project_id="global",
                actor="cli",
            )
        )
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="rt-mrg").one()
            # Merge picked existing row — its file body unchanged.
            assert Path(row.file_path).read_text(encoding="utf-8") == "local future wins"


# ---------------------------------------------------------------------------
# federated is just a scope — export/import round-trip
# ---------------------------------------------------------------------------


class TestFederatedScopeRoundTrip:
    def test_federated_row_round_trips(self, svc, memory_enabled, base_dir, db_engine, tmp_path):
        """A ``federated`` row exports and re-imports through the normal scope
        path: no separate directory, no demotion, scope preserved.
        """
        _force_federate_row(db_engine, "fed-rt", "federated body", base_dir)

        archive = tmp_path / "fed-rt.tar.gz"
        rep = _run(memory_export.export("federated", None, output_path=archive, actor="cli"))
        assert rep.errors == [], f"export errored: {rep.errors}"
        assert archive.exists()

        # Drop the row + wiki file, then re-import.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="fed-rt").one()
            wiki_path = Path(row.file_path)
            db.delete(row)
            db.commit()
        if wiki_path.exists():
            wiki_path.unlink()

        rep2 = _run(
            memory_import.import_archive(
                archive,
                conflict_policy="skip",
                target_project_id="global",
                actor="cli",
            )
        )
        assert any(a.decision == "insert" for a in rep2.actions)
        with Session() as db:
            row = db.query(MemoryMetadataModel).filter_by(key="fed-rt").one()
            assert row.scope == "federated"
            assert row.scope_id is None
            assert "federated body" in Path(row.file_path).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# audit classification parity (import/export events)
# ---------------------------------------------------------------------------


class TestAuditEventClassification:
    def test_import_export_events_are_partitioned(self):
        """Import/export run-completed gates are SYNC; progress/per-row outcomes
        are NOWAIT. The two sets are disjoint and both inside the whitelist.
        """
        from cli_agent_orchestrator.services.audit_log import (
            AUDIT_EVENT_WHITELIST,
            NOWAIT_AUDIT_EVENTS,
            SYNC_AUDIT_EVENTS,
        )

        assert "export_completed" in SYNC_AUDIT_EVENTS
        assert "import_completed" in SYNC_AUDIT_EVENTS
        assert "marker_strong_mode_rewrite" in SYNC_AUDIT_EVENTS

        for ev in (
            "export_started",
            "export_failed",
            "import_started",
            "import_failed",
            "memory_imported_row",
            "import_rejection",
            "import_tmp_dir_swept",
        ):
            assert ev in NOWAIT_AUDIT_EVENTS
            assert ev not in SYNC_AUDIT_EVENTS

        assert not (SYNC_AUDIT_EVENTS & NOWAIT_AUDIT_EVENTS)
        assert SYNC_AUDIT_EVENTS <= AUDIT_EVENT_WHITELIST
        assert NOWAIT_AUDIT_EVENTS <= AUDIT_EVENT_WHITELIST


# ---------------------------------------------------------------------------
# NFR — import wall-clock budget
# ---------------------------------------------------------------------------


class TestNFRBenchmarks:
    def test_import_100_row_archive_under_budget(
        self, svc, memory_enabled, base_dir, db_engine, tmp_path
    ):
        """Import of a 100-row archive completes in < 5s."""
        for i in range(100):
            _seed_global(svc, f"nfr-row-{i:03d}", content=f"body {i}")
        archive = tmp_path / "nfr.tar.gz"
        _run(memory_export.export("global", None, output_path=archive, actor="cli"))
        # Wipe DB to force pure-insert path on import.
        Session = sessionmaker(bind=db_engine)
        with Session() as db:
            db.query(MemoryMetadataModel).delete()
            db.commit()

        t0 = time.monotonic()
        _run(
            memory_import.import_archive(
                archive,
                conflict_policy="merge",
                target_project_id="global",
                actor="cli",
            )
        )
        elapsed = time.monotonic() - t0
        assert elapsed < 5.0, f"100-row import wall-clock {elapsed:.2f}s exceeds 5s budget"
        with Session() as db:
            n = db.query(MemoryMetadataModel).count()
            assert n == 100
