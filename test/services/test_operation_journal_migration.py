"""Operation-journal schema fidelity and restart persistence (cond-0378 B2).

- The operation-journal tables' partial/unique indexes are declared in ORM
  metadata, so ``Base.metadata.create_all`` and the production startup
  migration enforce equivalent invariants: the winner slot has at most one
  operation, and per-operation effect intents are queryable.
- The raw migration is idempotent and additive: existing B1 restore-contract
  rows and M3-A roster rows stay readable, and the new store is created
  beside them.
- An operation, its effect intent, and a claimed barrier survive a simulated
  restart (engine disposed and reopened at the same file).
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

_OPERATION_INDEXES = {
    "ix_reincarnation_operations_slot",
    "ix_reincarnation_operations_session",
    "ix_reincarnation_operations_agent",
    "ix_reincarnation_effect_intents_operation",
    "ix_reincarnation_effect_intents_step",
}

_OPERATION_COLUMNS = {
    "operation_id",
    "request_digest",
    "schema_version",
    "session_name",
    "agent_id",
    "roster_revision",
    "role",
    "profile_family",
    "lineage_id",
    "harness",
    "native_session_id",
    "prior_terminal_id",
    "prior_generation",
    "prior_incarnation_id",
    "lifecycle_epoch",
    "lifecycle_observation",
    "restore_contract_id",
    "restore_contract_digest",
    "restore_contract_schema",
    "route_provider",
    "model_requested",
    "effort_requested",
    "execution_mode_requested",
    "compatibility_cell_ref",
    "compatibility_cell_digest",
    "phase",
    "request_json",
    "created_at",
    "updated_at",
}

_INTENT_COLUMNS = {
    "effect_id",
    "operation_id",
    "effect_step",
    "effect_digest",
    "effect_payload_json",
    "recorded_at",
}

_BARRIER_COLUMNS = {
    "session_name",
    "state",
    "claimed_by",
    "reason",
    "epoch",
    "created_at",
    "updated_at",
}


def _journal_index_ddl(conn: sqlite3.Connection) -> dict[str, str]:
    return {
        row[0]: row[2]
        for row in conn.execute(
            "SELECT name, tbl_name, sql FROM sqlite_master "
            "WHERE type='index' AND name LIKE 'ix_reincarnation%'"
        ).fetchall()
    }


def _assert_journal_index_set(conn: sqlite3.Connection) -> None:
    present = set(_journal_index_ddl(conn))
    assert (
        _OPERATION_INDEXES <= present
    ), f"missing operation-journal indexes: {_OPERATION_INDEXES - present}"


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


def test_create_all_enforces_operation_journal_invariants(tmp_path):
    """``Base.metadata.create_all`` (the test fixture schema) carries the same
    indexes as the startup migration, and the winner slot is enforceable."""
    engine = create_engine(f"sqlite:///{tmp_path / 'meta.db'}")
    Base.metadata.create_all(bind=engine)
    conn = sqlite3.connect(str(tmp_path / "meta.db"))
    try:
        _assert_journal_index_set(conn)
        op_cols = {row[1] for row in conn.execute("PRAGMA table_info(reincarnation_operations)")}
        intent_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(reincarnation_effect_intents)")
        }
        barrier_cols = {
            row[1] for row in conn.execute("PRAGMA table_info(session_effect_barriers)")
        }
        assert _OPERATION_COLUMNS <= op_cols
        assert _INTENT_COLUMNS <= intent_cols
        assert _BARRIER_COLUMNS <= barrier_cols
        # The winner slot (agent, prior incarnation, lifecycle epoch, roster
        # revision) admits exactly one operation.
        slot_cols = (
            "operation_id, request_digest, schema_version, session_name, agent_id, "
            "roster_revision, role, profile_family, lineage_id, harness, native_session_id, "
            "prior_terminal_id, prior_generation, prior_incarnation_id, lifecycle_epoch, "
            "lifecycle_observation, restore_contract_id, restore_contract_digest, "
            "restore_contract_schema, route_provider, model_requested, effort_requested, "
            "execution_mode_requested, compatibility_cell_ref, compatibility_cell_digest, "
            "phase, request_json, created_at, updated_at"
        )
        with engine.begin() as connection:
            connection.execute(
                text(
                    f"INSERT INTO reincarnation_operations({slot_cols}) VALUES ("
                    "'op1','d','v1','cao-campaign-a','a1',2,'worker','developer','l1',"
                    "'claude_code','n1','t1','g1','i1',0,'working','rc1','dd','sv1',"
                    "'claude_code','m','e','native_tui','ref','cd','claimed','{}','t','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        f"INSERT INTO reincarnation_operations({slot_cols}) VALUES ("
                        "'op2','d','v1','cao-campaign-a','a1',2,'worker','developer','l1',"
                        "'claude_code','n1','t1','g1','i1',0,'working','rc1','dd','sv1',"
                        "'claude_code','m','e','native_tui','ref','cd','claimed','{}','t','t')"
                    )
                )
        # One logical physical step (operation, step) admits exactly one
        # intent, regardless of caller-minted effect ids.
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reincarnation_effect_intents("
                    "effect_id, operation_id, effect_step, effect_digest, "
                    "effect_payload_json, recorded_at"
                    ") VALUES ('e1','op1','fence_prior','dd','{}','t')"
                )
            )
            with pytest.raises(IntegrityError):
                connection.execute(
                    text(
                        "INSERT INTO reincarnation_effect_intents("
                        "effect_id, operation_id, effect_step, effect_digest, "
                        "effect_payload_json, recorded_at"
                        ") VALUES ('e2','op1','fence_prior','dd','{}','t')"
                    )
                )
    finally:
        conn.close()
        engine.dispose()


# ---------------------------------------------------------------------------
# migration: idempotent, additive, existing rows intact
# ---------------------------------------------------------------------------


def test_operation_journal_migration_is_idempotent_and_preserves_prior_rows(tmp_path, monkeypatch):
    """A database with the M3-A roster and B1 restore contracts but no journal
    upgrades in place: existing rows stay readable, the new tables appear
    once, and the migration reruns as a no-op."""
    db_path = tmp_path / "prod.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_stable_agent_roster()
    database._migrate_restore_contracts()
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=create_engine(f"sqlite:///{db_path}")),
    )
    agent_id = roster.derive_initial_agent_id("a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    bind = roster.bind_generation(
        _worker_binding(agent_id, "a1b2c3d4", "00000000-0000-4000-8000-000000000001")
    )
    contract = _contract_for(bind)
    rc.publish_contract(contract)
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )

    database._migrate_operation_journal()
    database._migrate_operation_journal()  # idempotent rerun

    conn = sqlite3.connect(str(db_path))
    try:
        _assert_journal_index_set(conn)
        agents = conn.execute("SELECT COUNT(*) FROM stable_agents").fetchone()[0]
        contracts = conn.execute("SELECT COUNT(*) FROM restore_contracts").fetchone()[0]
        assert agents == 1
        assert contracts == 1
        # A B2 operation can be claimed for the migrated source.
        agent = roster.get_agent(agent_id)
        record = oj.claim_operation(
            oj.OperationRequest(
                operation_id="11111111-2222-4333-8444-555555555555",
                session_name=agent["session_name"],
                agent_id=agent_id,
                roster_revision=agent["revision"],
                role=agent["role"],
                profile_family=agent["profile_family"],
                lineage_id=agent["current_lineage_id"],
                harness="claude_code",
                native_session_id="11111111-2222-4333-8444-555555555555",
                prior_terminal_id=contract.terminal_id,
                prior_generation=contract.generation,
                prior_incarnation_id=agent["current_incarnation_id"],
                lifecycle_epoch=0,
                lifecycle_observation=sl.WORKING,
                restore_contract_id=rc.get_contract_by_incarnation(
                    terminal_id=contract.terminal_id, generation=contract.generation
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
        )
        assert record["adopted"] is False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# restart persistence with the real persistence layer
# ---------------------------------------------------------------------------


def test_operation_intent_and_barrier_survive_restart(tmp_path, monkeypatch):
    """Claim an operation, authorize an effect intent, and claim the barrier
    through the real SQLite file, then dispose and reopen the engine — every
    read returns the same truth and the replay claim adopts."""
    db_path = tmp_path / "restart.db"
    monkeypatch.setattr("cli_agent_orchestrator.constants.DATABASE_FILE", db_path)
    database._migrate_stable_agent_roster()
    database._migrate_restore_contracts()
    database._migrate_operation_journal()

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
    roster.transition_dormant(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        agent_id=contract.agent_id,
        lineage_id=contract.lineage_id,
        contract_digest=contract.digest(),
        reason="pane lost",
    )
    agent = roster.get_agent(agent_id)
    operation_id = "22222222-3333-4333-8444-555555555555"
    request = oj.OperationRequest(
        operation_id=operation_id,
        session_name=agent["session_name"],
        agent_id=agent_id,
        roster_revision=agent["revision"],
        role=agent["role"],
        profile_family=agent["profile_family"],
        lineage_id=agent["current_lineage_id"],
        harness="claude_code",
        native_session_id="11111111-2222-4333-8444-555555555555",
        prior_terminal_id=contract.terminal_id,
        prior_generation=contract.generation,
        prior_incarnation_id=agent["current_incarnation_id"],
        lifecycle_epoch=0,
        lifecycle_observation=sl.WORKING,
        restore_contract_id=rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
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
    claimed = oj.claim_operation(request)
    intent = oj.authorize_effect_intent(
        operation_id,
        effect_id="33333333-4444-4333-8444-555555555555",
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": contract.generation},
    )

    for engine in engines:
        engine.dispose()
    engines.clear()

    # Restart: the claim replay adopts while the barrier is still unclaimed.
    _attach()
    replay = oj.claim_operation(request)
    assert replay["adopted"] is True
    read = oj.get_operation(operation_id)
    assert read["request_digest"] == claimed["operation"]["request_digest"]
    assert read["phase"] == oj.EFFECT_STEP_FENCE_PRIOR
    by_slot = oj.get_operation_by_slot(
        agent_id=agent_id,
        prior_incarnation_id=agent["current_incarnation_id"],
        lifecycle_epoch=0,
        roster_revision=read["roster_revision"],
    )
    assert by_slot["operation_id"] == operation_id
    stored_intent = oj.get_effect_intent("33333333-4444-4333-8444-555555555555")
    assert stored_intent["effect_digest"] == intent["intent"]["effect_digest"]
    step_winner = oj.get_effect_intent_by_step(operation_id, oj.EFFECT_STEP_FENCE_PRIOR)
    assert step_winner["effect_id"] == "33333333-4444-4333-8444-555555555555"

    # The barrier claim survives the restart and freezes later phases, while
    # an exact claim replay still adopts the durable winner.
    oj.claim_session_barrier("cao-campaign-a", claimed_by=operation_id, reason="stop")
    assert oj.get_session_barrier("cao-campaign-a")["state"] == oj.BARRIER_CLAIMED
    replay2 = oj.claim_operation(request)
    assert replay2["adopted"] is True
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id="44444444-5555-4333-8444-555555555555",
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": contract.generation},
            expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
        )
