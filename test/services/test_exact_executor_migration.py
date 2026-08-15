"""B3 successor/result schema fidelity and restart persistence (cond-0378).

- The additive successor reservation/result columns and their unique
  indexes are declared in ORM metadata, so ``Base.metadata.create_all``
  and the production startup migration enforce equivalent invariants:
  one successor terminal id and one successor generation per store, and
  one durable bounded result per operation.
- The raw migration is idempotent and additive: an existing B2 operation
  row keeps its bytes, gains the nullable columns, and the migration
  reruns as a no-op.
- A reserved successor and a recorded result survive a simulated restart
  (engine disposed and reopened at the same file).
"""

from __future__ import annotations

import sqlite3
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import exact_executor as xe
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

_B3_COLUMNS = {
    "successor_terminal_id",
    "successor_generation",
    "successor_incarnation_id",
    "result_state",
    "result_detail",
    "result_evidence_json",
    "result_at",
}

_B3_INDEXES = {
    "ix_reincarnation_operations_successor_terminal",
    "ix_reincarnation_operations_successor_generation",
}

_SLOT_COLUMNS = (
    "operation_id, request_digest, schema_version, session_name, agent_id, "
    "roster_revision, role, profile_family, lineage_id, harness, native_session_id, "
    "prior_terminal_id, prior_generation, prior_incarnation_id, lifecycle_epoch, "
    "lifecycle_observation, restore_contract_id, restore_contract_digest, "
    "restore_contract_schema, route_provider, model_requested, effort_requested, "
    "execution_mode_requested, compatibility_cell_ref, compatibility_cell_digest, "
    "phase, request_json, created_at, updated_at"
)

_SLOT_VALUES = (
    "'{op}','d','v1','cao-campaign-a','{agent}',2,'worker','developer','l1',"
    "'claude_code','n1','t1','g1','i1',0,'working','rc1','dd','sv1',"
    "'claude_code','m','e','native_tui','ref','cd','claimed','{{}}','t','t'"
)


def _b3_columns_present(conn: sqlite3.Connection) -> set:
    return {row[1] for row in conn.execute("PRAGMA table_info(reincarnation_operations)")}


def _index_ddl(conn: sqlite3.Connection, like: str) -> dict:
    return {
        row[0]: row[2]
        for row in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            f"WHERE type='index' AND name LIKE '{like}'"
        ).fetchall()
    }


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


def _contract_for(bind: dict, tmp_path):
    import hashlib
    import os

    binary = os.path.realpath(str(tmp_path / "claude"))
    with open(binary, "wb") as handle:
        handle.write(b"#!/bin/sh\n")
    os.chmod(binary, 0o755)
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
        working_directory=os.path.realpath(str(tmp_path)),
        executable=rc.ContractFact.present(
            {"path": binary, "sha256": hashlib.sha256(open(binary, "rb").read()).hexdigest()}
        ),
        profile_material=rc.ContractFact.present(
            {"profile_config_path": "/x/settings.json", "profile_config_sha256": "b" * 64}
        ),
        provider_home_facts=rc.ContractFact.unavailable("none"),
    )


# ---------------------------------------------------------------------------
# ORM metadata parity
# ---------------------------------------------------------------------------


def test_create_all_carries_the_b3_successor_result_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    Base.metadata.create_all(bind=engine)
    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    try:
        assert _B3_COLUMNS <= _b3_columns_present(conn)
        present = set(_index_ddl(conn, "ix_reincarnation_operations_successor%"))
        assert _B3_INDEXES <= present, f"missing B3 indexes: {_B3_INDEXES - present}"
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                    "successor_terminal_id, successor_generation) VALUES ("
                    f"{_SLOT_VALUES.format(op='op1', agent='a1')}, 'aaaa1111', 'g-succ-1')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                        "successor_terminal_id, successor_generation) VALUES ("
                        f"{_SLOT_VALUES.format(op='op2', agent='a2')}, 'aaaa1111', 'g-succ-2')"
                    )
                )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                        "successor_terminal_id, successor_generation) VALUES ("
                        f"{_SLOT_VALUES.format(op='op3', agent='a3')}, 'bbbb2222', 'g-succ-1')"
                    )
                )
        # NULL successors never collide (unclaimed operations coexist).
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}) VALUES ("
                    f"{_SLOT_VALUES.format(op='op4', agent='a4')})"
                )
            )
            connection.execute(
                text(
                    f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}) VALUES ("
                    f"{_SLOT_VALUES.format(op='op5', agent='a5')})"
                )
            )
    finally:
        conn.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# migration: idempotent, additive, ORM/raw parity
# ---------------------------------------------------------------------------


def test_migration_adds_b3_columns_idempotently(tmp_path, monkeypatch):
    db_path = tmp_path / "prod.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_stable_agent_roster()
    database._migrate_restore_contracts()
    database._migrate_operation_journal()

    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}) VALUES ("
                f"{_SLOT_VALUES.format(op='legacy-op', agent='a9')})"
            )
    finally:
        conn.close()

    # Re-running the full ladder is a no-op that preserves the row.
    database._migrate_operation_journal()
    conn = sqlite3.connect(str(db_path))
    try:
        assert _B3_COLUMNS <= _b3_columns_present(conn)
        present = set(_index_ddl(conn, "ix_reincarnation_operations_successor%"))
        assert _B3_INDEXES <= present
        row = conn.execute(
            "SELECT successor_terminal_id, result_state FROM reincarnation_operations "
            "WHERE operation_id = 'legacy-op'"
        ).fetchone()
        assert row == (None, None)
    finally:
        conn.close()

    # The raw-migrated store ENFORCES the successor uniqueness, not just
    # carries same-named indexes.
    conn = sqlite3.connect(str(db_path))
    try:
        with conn:
            conn.execute(
                f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                "successor_terminal_id, successor_generation) VALUES ("
                f"{_SLOT_VALUES.format(op='raw-op-1', agent='b1')}, 'dddd5555', 'g-raw-1')"
            )
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                    "successor_terminal_id, successor_generation) VALUES ("
                    f"{_SLOT_VALUES.format(op='raw-op-2', agent='b2')}, 'dddd5555', 'g-raw-2')"
                )
        with pytest.raises(sqlite3.IntegrityError):
            with conn:
                conn.execute(
                    f"INSERT INTO reincarnation_operations({_SLOT_COLUMNS}, "
                    "successor_terminal_id, successor_generation) VALUES ("
                    f"{_SLOT_VALUES.format(op='raw-op-3', agent='b3')}, 'eeee6666', 'g-raw-1')"
                )
    finally:
        conn.close()

    # The raw-DDL shape matches the ORM-created shape.
    orm_path = tmp_path / "orm.db"
    engine = create_engine(f"sqlite:///{orm_path}")
    Base.metadata.create_all(bind=engine)
    engine.dispose()
    raw_conn = sqlite3.connect(str(db_path))
    orm_conn = sqlite3.connect(str(orm_path))
    try:
        raw_cols = {
            row[1]: (row[2], row[3])
            for row in raw_conn.execute("PRAGMA table_info(reincarnation_operations)")
        }
        orm_cols = {
            row[1]: (row[2], row[3])
            for row in orm_conn.execute("PRAGMA table_info(reincarnation_operations)")
        }
        assert set(raw_cols) == set(orm_cols)
        for column in _B3_COLUMNS:
            assert raw_cols[column] == orm_cols[column], column
        raw_indexes = _index_ddl(raw_conn, "ix_reincarnation_operations_successor%")
        orm_indexes = _index_ddl(orm_conn, "ix_reincarnation_operations_successor%")
        assert set(raw_indexes) == set(orm_indexes)
    finally:
        raw_conn.close()
        orm_conn.close()


def test_successor_reservation_and_result_survive_restart(tmp_path, monkeypatch):
    db_path = tmp_path / "restart.db"
    engine = create_engine(f"sqlite:///{db_path}")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))

    agent_id = roster.derive_initial_agent_id("a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    bind = roster.bind_generation(
        _worker_binding(agent_id, "a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    )
    contract = _contract_for(bind, tmp_path)
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    agent = roster.get_agent(agent_id)
    request = oj.OperationRequest(
        operation_id=str(uuid.uuid4()),
        session_name="cao-campaign-a",
        agent_id=agent_id,
        roster_revision=agent["revision"],
        role=agent["role"],
        profile_family=agent["profile_family"],
        lineage_id=bind["lineage"]["lineage_id"],
        harness="claude_code",
        native_session_id=bind["lineage"]["native_session_id"],
        prior_terminal_id=bind["incarnation"]["terminal_id"],
        prior_generation=bind["incarnation"]["generation"],
        prior_incarnation_id=bind["incarnation"]["incarnation_id"],
        lifecycle_epoch=0,
        lifecycle_observation=sl.WORKING,
        restore_contract_id=rc.get_contract_by_incarnation(
            terminal_id=bind["incarnation"]["terminal_id"],
            generation=bind["incarnation"]["generation"],
        )["contract_id"],
        restore_contract_digest=contract.digest(),
        restore_contract_schema=rc.SCHEMA_VERSION,
        route_provider="claude_code",
        model_requested="claude-sonnet-4-5",
        effort_requested="high",
        execution_mode_requested="native_tui",
        compatibility_cell_ref="claude_code:anthropic:native_tui",
        compatibility_cell_digest="c" * 64,
    )
    oj.claim_operation(request)
    reservation = oj.reserve_successor(request.operation_id, "cccc3333", "g-restart-1")
    assert reservation["adopted"] is False
    successor_incarnation_id = str(uuid.uuid4())
    oj.record_result(
        request.operation_id,
        xe.OUTCOME_ACCEPTED,
        detail="restart probe",
        evidence={"successor_incarnation_id": "i1"},
        successor_incarnation_id=successor_incarnation_id,
    )
    engine.dispose()

    engine2 = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine2))
    try:
        stored = oj.get_operation(request.operation_id)
        assert stored["successor_terminal_id"] == "cccc3333"
        assert stored["successor_generation"] == "g-restart-1"
        assert stored["successor_incarnation_id"] == successor_incarnation_id
        assert stored["result_state"] == xe.OUTCOME_ACCEPTED
        # The reservation adopts across the restart: never a second successor.
        again = oj.reserve_successor(request.operation_id, "cccc3333", "g-restart-1")
        assert again["adopted"] is True
        # The final result is write-once: a later refusal cannot hide it.
        oj.record_result(
            request.operation_id, xe.OUTCOME_REFUSED, detail="late refusal", evidence={}
        )
        assert oj.get_operation(request.operation_id)["result_state"] == xe.OUTCOME_ACCEPTED
    finally:
        engine2.dispose()
