"""Restore-contract schema fidelity and restart persistence (cond-0378 B1).

- The ``restore_contracts`` partial unique indexes are declared in ORM
  metadata, so ``Base.metadata.create_all`` and the production startup
  migration enforce equivalent source-incarnation uniqueness.
- The raw migration is idempotent and additive: existing M3-A roster rows
  stay readable, and the new store is created beside them.
- A contract published through the real persistence layer survives a
  simulated restart (engine disposed and reopened at the same file).
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import stable_agent_roster as roster

_RESTORE_INDEXES = {
    "ix_restore_contracts_terminal_generation",
    "ix_restore_contracts_terminal_legacy",
    "ix_restore_contracts_agent_id",
    "ix_restore_contracts_lineage_id",
}

_RESTORE_COLUMNS = {
    "contract_id",
    "contract_digest",
    "schema_version",
    "agent_id",
    "lineage_id",
    "terminal_id",
    "generation",
    "native_session_id",
    "contract_json",
    "created_at",
}


def _restore_index_ddl(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row[0]: row[2]
        for row in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'ix_restore_contracts%'"
        ).fetchall()
    }


def _assert_restore_index_set(conn: sqlite3.Connection) -> None:
    present = set(_restore_index_ddl(conn))
    assert (
        _RESTORE_INDEXES <= present
    ), f"missing restore-contract indexes: {_RESTORE_INDEXES - present}"


def _worker_binding(agent_id: str, terminal_id: str, generation: str):
    return roster.BindingContract(
        agent_id=agent_id,
        session_name="cao-campaign-a",
        role=roster.ROLE_WORKER,
        profile_family="developer",
        harness="claude_code",
        native_session_id="11111111-2222-4333-8444-555555555555",
        acquisition_method="chosen_session_id",
        route_provenance={"provider_route": "anthropic"},
        terminal_id=terminal_id,
        generation=generation,
        execution_mode="native_tui",
    )


def _contract_for(bind: dict[str, dict]):
    return rc.RestoreContract(
        agent_id=bind["agent"]["agent_id"],
        lineage_id=bind["lineage"]["lineage_id"],
        terminal_id=bind["incarnation"]["terminal_id"],
        generation=bind["incarnation"]["generation"],
        native_session_id=bind["lineage"]["native_session_id"],
        harness="claude_code",
        provider="claude_code",
        route_provenance={"provider_route": "anthropic"},
        execution_mode="native_tui",
        model=rc.ContractFact.present("claude-sonnet-4-5"),
        effort=rc.ContractFact.present("high"),
        working_directory="/Users/colin/Projects/cao",
        trusted_project_root="/Users/colin/Projects/cao",
        executable=rc.ContractFact.present({"path": "/usr/local/bin/claude", "sha256": "a" * 64}),
        profile_material=rc.ContractFact.present(
            {
                "profile_config_path": "/Users/colin/.claude/settings.json",
                "profile_config_sha256": "b" * 64,
            }
        ),
        provider_home_facts=rc.ContractFact.unavailable(
            "no provider-home carrier facts at this source seam"
        ),
    )


# ---------------------------------------------------------------------------
# ORM metadata parity with the production migration
# ---------------------------------------------------------------------------


def test_create_all_enforces_restore_contract_partial_unique_indexes(tmp_path):
    """``Base.metadata.create_all`` (the test fixture schema) carries the same
    partial unique indexes as the startup migration."""
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    Base.metadata.create_all(bind=engine)
    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    try:
        _assert_restore_index_set(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(restore_contracts)")}
        assert _RESTORE_COLUMNS <= columns
        # Source-incarnation uniqueness is enforceable through the ORM.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO restore_contracts("
                    "contract_id, contract_digest, schema_version, agent_id, lineage_id, "
                    "terminal_id, generation, native_session_id, contract_json, created_at"
                    ") VALUES ('c1','d1','v1','a1','l1','t1','g1',NULL,'{}','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO restore_contracts("
                        "contract_id, contract_digest, schema_version, agent_id, lineage_id, "
                        "terminal_id, generation, native_session_id, contract_json, created_at"
                        ") VALUES ('c2','d2','v1','a1','l1','t1','g1',NULL,'{}','t')"
                    )
                )
            # The NULL-generation partial unique index permits exactly one
            # legacy (generation-less) contract per terminal id.
            connection.execute(
                text(
                    "INSERT INTO restore_contracts("
                    "contract_id, contract_digest, schema_version, agent_id, lineage_id, "
                    "terminal_id, generation, native_session_id, contract_json, created_at"
                    ") VALUES ('c3','d3','v1','a1','l1','t1',NULL,NULL,'{}','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO restore_contracts("
                        "contract_id, contract_digest, schema_version, agent_id, lineage_id, "
                        "terminal_id, generation, native_session_id, contract_json, created_at"
                        ") VALUES ('c4','d4','v1','a1','l1','t1',NULL,NULL,'{}','t')"
                    )
                )
    finally:
        conn.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# migration: idempotent, additive, legacy rows intact
# ---------------------------------------------------------------------------


def test_restore_contract_migration_is_idempotent_and_preserves_roster_rows(tmp_path, monkeypatch):
    """A database with the M3-A roster but no restore-contract store upgrades
    in place: roster rows stay readable, the new table appears once, and the
    migration reruns as a no-op."""
    db_path = tmp_path / "prod.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_stable_agent_roster()
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=create_engine(f"sqlite:///{db_path}")),
    )
    agent_id = roster.derive_initial_agent_id("a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    bind = roster.bind_generation(
        _worker_binding(agent_id, "a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    )

    database._migrate_restore_contracts()
    database._migrate_restore_contracts()  # idempotent rerun

    conn = sqlite3.connect(str(db_path))
    try:
        _assert_restore_index_set(conn)
        # The seeded roster row is intact.
        agents = conn.execute("SELECT COUNT(*) FROM stable_agents").fetchone()[0]
        assert agents == 1
        # A contract can be published for it after the migration.
        contract = _contract_for(bind)
        record = rc.publish_contract(contract)
        assert record["adopted"] is False
        assert (
            rc.get_contract_by_incarnation(
                terminal_id=bind["incarnation"]["terminal_id"],
                generation=bind["incarnation"]["generation"],
            )["contract_digest"]
            == contract.digest()
        )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# restart persistence with the real persistence layer
# ---------------------------------------------------------------------------


def test_contract_and_dormant_transition_survive_restart(tmp_path, monkeypatch):
    """Publish a contract and run the dormant transition through the real
    SQLite file, then dispose and reopen the engine (simulating a cao-server
    restart) — both reads come back intact and the transition replay adopts."""
    db_path = tmp_path / "restart.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_stable_agent_roster()
    database._migrate_restore_contracts()

    engines: list = []

    def _attach() -> None:
        engine = create_engine(f"sqlite:///{db_path}")
        engines.append(engine)
        monkeypatch.setattr(
            database,
            "SessionLocal",
            sessionmaker(bind=engine),
        )

    _attach()
    agent_id = roster.derive_initial_agent_id("a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    bind = roster.bind_generation(
        _worker_binding(agent_id, "a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    )
    contract = _contract_for(bind)
    rc.publish_contract(contract)
    first = roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    assert first["adopted"] is False

    # Restart: dispose the engines on this file and re-attach fresh ones.
    for engine in engines:
        engine.dispose()
    engines.clear()

    _attach()
    read_contract = rc.get_contract_by_incarnation(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert read_contract["contract_digest"] == contract.digest()
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    replay = roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    assert replay["adopted"] is True
