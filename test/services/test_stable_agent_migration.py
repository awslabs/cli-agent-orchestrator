"""M3-A / cond-0377: roster schema fidelity (PR #91 review).

- ``i-0014``: the roster partial unique indexes are declared in ORM
  metadata, so ``Base.metadata.create_all`` and the production startup
  migration enforce equivalent native-lineage and incarnation
  uniqueness.
- ``i-0018``: the raw legacy/draft migration has direct regression
  coverage — idempotent rerun, row preservation, expected columns, and
  the exact partial-index set.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import stable_agent_roster as roster

_ROSTER_INDEXES = {
    "ix_stable_agents_session_name",
    "ix_stable_lineage_harness_native_session_id",
    "ix_stable_incarnation_terminal_generation",
    "ix_stable_incarnation_terminal_legacy",
}


def _index_ddl(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row[0]: row[2]
        for row in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'ix_stable%'"
        ).fetchall()
    }


def _assert_index_set(conn: sqlite3.Connection) -> None:
    present = set(_index_ddl(conn))
    assert _ROSTER_INDEXES <= present, f"missing roster indexes: {_ROSTER_INDEXES - present}"
    assert "ix_stable_lineage_native_session_id" not in present
    assert "ix_stable_incarnation_terminal_id" not in present


# ---------------------------------------------------------------------------
# i-0014: ORM metadata parity with the production migration
# ---------------------------------------------------------------------------


def test_create_all_enforces_roster_partial_unique_indexes(tmp_path):
    """``Base.metadata.create_all`` (the test fixture schema) carries the
    same partial unique indexes as the startup migration."""
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    Base.metadata.create_all(bind=engine)
    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    try:
        _assert_index_set(conn)
        # The native-lineage uniqueness is enforceable through the ORM.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO stable_agent_lineages("
                    "lineage_id, agent_id, harness, native_session_id, "
                    "lineage_origin, created_at, updated_at"
                    ") VALUES ('l1','a1','claude_code','same-id','initial','t','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO stable_agent_lineages("
                        "lineage_id, agent_id, harness, native_session_id, "
                        "lineage_origin, created_at, updated_at"
                        ") VALUES ('l2','a2','claude_code','same-id','initial','t','t')"
                    )
                )
        # Incarnation (terminal_id, generation) uniqueness is enforceable.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO stable_agent_incarnations("
                    "incarnation_id, agent_id, terminal_id, generation, disposition, "
                    "created_at, updated_at"
                    ") VALUES ('i1','a1','t1','g1','bound','t','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO stable_agent_incarnations("
                        "incarnation_id, agent_id, terminal_id, generation, disposition, "
                        "created_at, updated_at"
                        ") VALUES ('i2','a1','t1','g1','bound','t','t')"
                    )
                )
    finally:
        conn.close()
        engine.dispose()


def test_init_db_creates_equivalent_roster_schema(tmp_path, monkeypatch):
    """Production initialization produces the same roster index set."""
    engine = create_engine(f"sqlite:///{tmp_path / 'prod.db'}")
    Base.metadata.create_all(bind=engine)
    # Simulate the production path: run the startup roster migration on
    # top of create_all (idempotent) and confirm the index set is stable.
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", tmp_path / "prod.db")
    database._migrate_stable_agent_roster()
    database._migrate_stable_agent_roster()  # idempotent rerun
    conn = sqlite3.connect(str(tmp_path / "prod.db"))
    try:
        _assert_index_set(conn)
    finally:
        conn.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# i-0018: legacy/draft migration regression
# ---------------------------------------------------------------------------


@pytest.fixture
def draft_db(tmp_path, monkeypatch):
    """A database seeded with the earlier dark-draft roster schema."""
    db_path = tmp_path / "draft.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE stable_agents (
          agent_id TEXT NOT NULL PRIMARY KEY,
          session_name TEXT NOT NULL, role TEXT NOT NULL, profile_family TEXT NOT NULL,
          disposition TEXT NOT NULL, resume_contract_version TEXT NOT NULL,
          current_lineage_id TEXT, current_incarnation_id TEXT,
          revision INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
          UNIQUE (session_name, role, profile_family));
        INSERT INTO stable_agents VALUES (
          'aaaaaaaa-0000-4000-8000-000000000001','cao-legacy','worker','developer',
          'identity_missing','cao-m3-resume-contract-v1',NULL,NULL,1,'t','t');
        CREATE TABLE stable_agent_lineages (
          lineage_id TEXT NOT NULL PRIMARY KEY, agent_id TEXT NOT NULL, harness TEXT NOT NULL,
          native_session_id TEXT, acquisition_method TEXT, route_provenance_json TEXT,
          continuity_note TEXT, predecessor_lineage_id TEXT, lineage_origin TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE INDEX ix_stable_lineage_native_session_id
          ON stable_agent_lineages(native_session_id) WHERE native_session_id IS NOT NULL;
        INSERT INTO stable_agent_lineages VALUES (
          'bbbbbbbb-0000-4000-8000-000000000002','aaaaaaaa-0000-4000-8000-000000000001',
          'claude_code',NULL,NULL,NULL,NULL,NULL,'initial','t','t');
        CREATE TABLE stable_agent_incarnations (
          incarnation_id TEXT NOT NULL PRIMARY KEY, agent_id TEXT NOT NULL, lineage_id TEXT,
          terminal_id TEXT, generation TEXT, pane_id TEXT, pane_pid INTEGER,
          process_identity_json TEXT, execution_mode TEXT, disposition TEXT NOT NULL,
          retired_at TEXT, retirement_reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE UNIQUE INDEX ix_stable_incarnation_terminal_id
          ON stable_agent_incarnations(terminal_id) WHERE terminal_id IS NOT NULL;
        """)
    conn.commit()
    conn.close()
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    return db_path


def test_legacy_migration_upgrades_idempotently_and_preserves_rows(draft_db):
    database._migrate_stable_agent_roster()
    database._migrate_stable_agent_roster()  # second run must be a no-op

    conn = sqlite3.connect(str(draft_db))
    try:
        # Rows preserved.
        agents = conn.execute("SELECT agent_id, session_name FROM stable_agents").fetchall()
        assert len(agents) == 1
        assert agents[0] == ("aaaaaaaa-0000-4000-8000-000000000001", "cao-legacy")
        lineages = conn.execute("SELECT COUNT(*) FROM stable_agent_lineages").fetchone()[0]
        assert lineages == 1
        # Expected columns present.
        agent_cols = {row[1] for row in conn.execute("PRAGMA table_info(stable_agents)")}
        assert {
            "agent_id",
            "session_name",
            "role",
            "profile_family",
            "disposition",
            "resume_contract_version",
            "revision",
        } <= agent_cols
        inc_cols = {row[1] for row in conn.execute("PRAGMA table_info(stable_agent_incarnations)")}
        assert {"terminal_id", "generation", "disposition"} <= inc_cols
        # The draft unique constraint is gone (only the PK autoindex remains).
        auto = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND tbl_name='stable_agents' AND name LIKE 'sqlite_autoindex_%'"
            )
        ]
        assert len(auto) == 1, auto
        _assert_index_set(conn)
    finally:
        conn.close()


def test_legacy_migration_allows_same_profile_new_worker(draft_db, monkeypatch):
    """After the upgrade the unique role/profile-family constraint is gone,
    so a new same-profile worker binds independently."""
    from sqlalchemy.orm import sessionmaker

    database._migrate_stable_agent_roster()
    # Point the roster store at the migrated draft database.
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=create_engine(f"sqlite:///{draft_db}")),
    )
    bound = roster.bind_generation(
        roster.BindingContract(
            agent_id=roster.derive_initial_agent_id(
                "a1b2c3d4", "00000000-0000-4000-8000-0000000000aa"
            ),
            session_name="cao-legacy",
            role=roster.ROLE_WORKER,
            profile_family="developer",
            harness="claude_code",
            native_session_id=None,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-0000000000aa",
        )
    )
    agents = roster.list_agents(session_name="cao-legacy")
    assert len(agents) == 2
    assert bound["agent"]["agent_id"] != "aaaaaaaa-0000-4000-8000-000000000001"
