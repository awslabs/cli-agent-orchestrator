"""Durable idempotent reincarnation-operation journal + session-effect seam
(cond-0378 B2).

B2 owns the durable physical-reincarnation operation journal and the narrow
shared per-session effect seam that B3 and later M3-C consume:

- ``claim_operation`` claims the winning operation for one exact source slot
  (stable agent, prior incarnation, lifecycle epoch, roster revision) in one
  short database transaction, binding every immutable fact the brief names.
  An exact operation-id/request replay adopts; changed immutable input under
  one operation id conflicts; a concurrent different id for the same slot
  never creates a second winner — the loser queries/adopts the durable
  winner.  SQLite unique/busy races surface as typed, retryable outcomes,
  never raw driver errors.
- ``authorize_effect_intent`` is the shared seam: it CAS-records the next
  physical effect intent only while the exact operation is the winner and in
  the expected journal phase, the session lifecycle is still the bound epoch
  and is not ``stopped``, the fork-owned session barrier is unclaimed, and
  the bound stable-agent/source/restore facts still agree with the operation.
  Every later phase rechecks the same facts.
- ``claim_session_barrier`` / ``get_session_barrier`` expose the durable
  per-session barrier primitive M3-C will claim during Stop.  Barriers never
  expire and are never cleared automatically.

Effect-intent-wins-then-barrier preserves the in-flight intent for later
M3-C reconciliation; barrier-wins-then-effect admits no later phase.

This slice performs NO tmux, provider, native attachment, terminal creation,
input, Stop/Pause, conductor, task, or supervisor effect.  Every assertion
runs against the ORM store via ``isolated_memory_db`` (or a real file
database for the concurrency tests).

These tests were written before the service changes that satisfy them.
"""

from __future__ import annotations

import re
import threading
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DIGEST64 = "a" * 64
_NATIVE_ID = "11111111-2222-4333-8444-555555555555"
_CELL_REF = "claude_code:anthropic:native_tui"
_CELL_DIGEST = "c" * 64


def _fact(value):
    return rc.ContractFact.present(value)


def _bind_worker(agent_id=None, **bind_changes):
    """Bind a roster worker/lineage/incarnation; returns the bind dict."""
    payload = {
        "agent_id": agent_id or str(uuid.uuid4()),
        "session_name": "cao-campaign-a",
        "role": roster.ROLE_WORKER,
        "profile_family": "developer",
        "harness": "claude_code",
        "native_session_id": _NATIVE_ID,
        "acquisition_method": "chosen_session_id",
        "route_provenance": {"provider_route": "anthropic"},
        "terminal_id": "a1b2c3d4",
        "generation": "00000000-0000-4000-8000-000000000001",
        "pane_id": "%101",
        "pane_pid": 4242,
        "process_identity": {"pid": 4242, "start_marker": "2026-08-09T00:00:00Z"},
        "execution_mode": "native_tui",
    }
    payload.update(bind_changes)
    return roster.bind_generation(roster.BindingContract(**payload))


def _contract_for(bind, **changes):
    """A restore contract bound to a live roster bind (authoritative identity)."""
    payload = {
        "agent_id": bind["agent"]["agent_id"],
        "lineage_id": bind["lineage"]["lineage_id"],
        "terminal_id": bind["incarnation"]["terminal_id"],
        "generation": bind["incarnation"]["generation"],
        "native_session_id": bind["lineage"]["native_session_id"],
        "harness": bind["lineage"]["harness"],
        "provider": "claude_code",
        "route_provenance": bind["lineage"]["route_provenance"],
        "execution_mode": bind["incarnation"]["execution_mode"],
        "model": _fact("claude-sonnet-4-5"),
        "effort": _fact("high"),
        "working_directory": "/Users/colin/Projects/cao",
        "trusted_project_root": "/Users/colin/Projects/cao",
        "executable": _fact({"path": "/usr/local/bin/claude", "sha256": _DIGEST64}),
        "profile_material": _fact(
            {
                "profile_config_path": "/Users/colin/.claude/settings.json",
                "profile_config_sha256": "b" * 64,
            }
        ),
        "provider_home_facts": rc.ContractFact.unavailable(
            "no provider-home carrier facts at this source seam"
        ),
    }
    payload.update(changes)
    return rc.RestoreContract(**payload)


def _dormant_worker(agent_id=None, **bind_changes):
    """Bind a roster source, publish its restore contract, and atomically
    retire the source (live -> dormant).  Returns (bind, contract) for the
    exact retired prior incarnation a B2 operation is bound to."""
    bind = _bind_worker(agent_id=agent_id, **bind_changes)
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
    return bind, contract


def _operation_request(bind, contract, operation_id=None, **changes):
    """An OperationRequest exactly bound to the dormant source + the declared
    lifecycle (undeclared => working at epoch 0 by default)."""
    agent = roster.get_agent(bind["agent"]["agent_id"])
    payload = {
        "operation_id": operation_id or str(uuid.uuid4()),
        "session_name": bind["agent"]["session_name"],
        "agent_id": bind["agent"]["agent_id"],
        "roster_revision": agent["revision"],
        "role": bind["agent"]["role"],
        "profile_family": bind["agent"]["profile_family"],
        "lineage_id": bind["lineage"]["lineage_id"],
        "harness": bind["lineage"]["harness"],
        "native_session_id": bind["lineage"]["native_session_id"],
        "prior_terminal_id": bind["incarnation"]["terminal_id"],
        "prior_generation": bind["incarnation"]["generation"],
        "prior_incarnation_id": bind["incarnation"]["incarnation_id"],
        "lifecycle_epoch": 0,
        "lifecycle_observation": sl.WORKING,
        "restore_contract_digest": contract.digest(),
        "restore_contract_schema": rc.SCHEMA_VERSION,
        "route_provider": "claude_code",
        "model_requested": "claude-sonnet-4-5",
        "effort_requested": "high",
        "execution_mode_requested": "native_tui",
        "compatibility_cell_ref": _CELL_REF,
        "compatibility_cell_digest": _CELL_DIGEST,
    }
    if "restore_contract_id" not in changes:
        payload["restore_contract_id"] = rc.get_contract_by_incarnation(
            terminal_id=bind["incarnation"]["terminal_id"],
            generation=bind["incarnation"]["generation"],
        )["contract_id"]
    payload.update(changes)
    return oj.OperationRequest(**payload)


def _claim(bind, contract, operation_id=None, **changes):
    """Claim an operation for the dormant source and return its record."""
    request = _operation_request(bind, contract, operation_id=operation_id, **changes)
    return oj.claim_operation(request)


def _gate_first_call(fn, barrier):
    """Wrap ``fn`` so each thread syncs ONCE at a barrier (its first call) then
    behaves normally.  Concurrent writers reach the pre-write seam together
    while retries inside the same thread do not re-sync."""
    local = threading.local()

    def gated(*args, **kwargs):
        if not getattr(local, "entered", False):
            local.entered = True
            barrier.wait(timeout=10)
        return fn(*args, **kwargs)

    return gated


def _declare(session_name, lifecycle, declared_by="operator"):
    return sl.declare(session_name, lifecycle, declared_by=declared_by)


def _stop_session(session_name):
    return sl.stop(session_name, declared_by="operator")


def _update_operation_column(engine, operation_id, column, value):
    with engine.begin() as conn:
        conn.execute(
            text(f"UPDATE reincarnation_operations SET {column} = :value WHERE operation_id = :id"),
            {"value": value, "id": operation_id},
        )


def _corrupt_request_json(engine, operation_id, new_json):
    _update_operation_column(engine, operation_id, "request_json", new_json)


def _corrupt_stored_contract(isolated_memory_db, terminal_id, generation, new_json):
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE restore_contracts SET contract_json = :json "
                "WHERE terminal_id = :tid AND generation = :gen"
            ),
            {"json": new_json, "tid": terminal_id, "gen": generation},
        )


@pytest.fixture
def file_db(tmp_path, monkeypatch):
    """A real SQLite-file store for concurrency tests (two sessions, one file)."""
    engine = create_engine(f"sqlite:///{tmp_path / 'conc.db'}")
    Base.metadata.create_all(bind=engine)
    monkeypatch.setattr(
        database,
        "SessionLocal",
        sessionmaker(bind=engine),
    )
    yield engine
    engine.dispose()


# ---------------------------------------------------------------------------
# 1. exact operation replay adopts one durable winner; GET after response loss
# ---------------------------------------------------------------------------


def test_exact_operation_replay_adopts_one_durable_winner(isolated_memory_db):
    """Replaying the identical operation request under the same operation id
    converges: one row, the second claim adopts, and the request digest and
    bound facts are identical."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    first = _claim(bind, contract, operation_id=operation_id)
    assert first["adopted"] is False
    second = _claim(bind, contract, operation_id=operation_id)
    assert second["adopted"] is True
    assert second["operation"]["operation_id"] == first["operation"]["operation_id"]
    assert second["operation"]["request_digest"] == first["operation"]["request_digest"]
    assert second["operation"]["phase"] == oj.PHASE_CLAIMED
    assert len(oj.list_operations()) == 1


def test_response_loss_resolves_by_get_never_a_second_claim(isolated_memory_db):
    """After a simulated response loss, the exact winning operation is
    retrieved by id and by slot with identical bytes — no second claim is
    needed and none can create a second winner."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    _claim(bind, contract, operation_id=operation_id)

    by_id = oj.get_operation(operation_id)
    by_slot = oj.get_operation_by_slot(
        agent_id=bind["agent"]["agent_id"],
        prior_incarnation_id=bind["incarnation"]["incarnation_id"],
        lifecycle_epoch=0,
        roster_revision=by_id["roster_revision"],
    )
    assert by_slot["operation_id"] == operation_id
    assert by_slot["request_digest"] == by_id["request_digest"]
    assert by_slot["request_json"] == by_id["request_json"]
    assert len(oj.list_operations(agent_id=bind["agent"]["agent_id"])) == 1


# ---------------------------------------------------------------------------
# 2. changed immutable input under one operation id conflicts
# ---------------------------------------------------------------------------


def test_changed_request_under_same_operation_id_conflicts(isolated_memory_db):
    """Reusing an operation id with changed request facts is a typed conflict
    with zero second intent."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    _claim(bind, contract, operation_id=operation_id)
    with pytest.raises(oj.OperationJournalConflict):
        _claim(
            bind,
            contract,
            operation_id=operation_id,
            model_requested="claude-opus-5",
        )
    assert len(oj.list_operations()) == 1


def test_changed_immutable_identity_under_same_operation_id_conflicts(isolated_memory_db):
    """Changing an immutable identity fact (the prior source incarnation)
    under one operation id conflicts rather than silently rebinding."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    _claim(bind, contract, operation_id=operation_id)
    with pytest.raises(oj.OperationJournalConflict):
        _claim(
            bind,
            contract,
            operation_id=operation_id,
            prior_terminal_id="deadbeef",
        )
    assert len(oj.list_operations()) == 1


# ---------------------------------------------------------------------------
# 3. two operation ids racing the same slot -> one winner (file SQLite)
# ---------------------------------------------------------------------------


def test_concurrent_different_operation_ids_one_winner(file_db, monkeypatch):
    """Two threads claim different operation ids for the same source slot
    through a real SQLite file: exactly one winner, one typed conflict naming
    the winner, one durable row, and the loser queries the winner."""
    bind, contract = _dormant_worker()
    barrier = threading.Barrier(2)
    monkeypatch.setattr(oj, "_now", _gate_first_call(oj._now, barrier))

    results: list[dict] = []
    conflicts: list[BaseException] = []
    others: list[BaseException] = []

    def run() -> None:
        try:
            results.append(_claim(bind, contract))
        except oj.OperationJournalConflict as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            others.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert others == [], f"unexpected non-Conflict claim errors: {others}"
    assert len(results) == 1, f"expected exactly one winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one typed conflict, got {len(conflicts)}"
    assert len(oj.list_operations()) == 1
    winner_id = results[0]["operation"]["operation_id"]
    assert winner_id in str(conflicts[0])
    # The loser can query and adopt the durable winner.
    by_slot = oj.get_operation_by_slot(
        agent_id=bind["agent"]["agent_id"],
        prior_incarnation_id=bind["incarnation"]["incarnation_id"],
        lifecycle_epoch=0,
        roster_revision=results[0]["operation"]["roster_revision"],
    )
    assert by_slot["operation_id"] == winner_id


def test_caller_owned_two_session_slot_contention_typed_and_recoverable(file_db, monkeypatch):
    """Two caller-owned sessions racing the same source slot: exactly one wins;
    the other receives exactly one typed OperationJournalUnavailable (never a
    raw SQLite IntegrityError/OperationalError), rolls back its now-unusable
    transaction, and on retry observes the typed slot conflict naming the
    durable winner — which it then queries and adopts."""
    bind, contract = _dormant_worker()
    barrier = threading.Barrier(2)
    monkeypatch.setattr(oj, "_now", _gate_first_call(oj._now, barrier))

    outcomes: list[dict] = []
    refusals: list[str] = []
    conflicts: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        session = database.SessionLocal()
        try:
            session.begin()
            try:
                outcomes.append(oj.claim_operation(_operation_request(bind, contract), db=session))
                session.commit()
            except oj.OperationJournalUnavailable as exc:
                # Record the typed refusal, then recover: the loser's caller-
                # owned transaction is unusable after the unique-slot collision,
                # so roll it back and retry the whole caller-owned call.  The
                # retry reads the winner's row and surfaces the typed slot
                # conflict naming it.
                refusals.append(str(exc))
                session.rollback()
                session.begin()
                try:
                    oj.claim_operation(_operation_request(bind, contract), db=session)
                    session.commit()
                except oj.OperationJournalConflict as conflict:
                    conflicts.append(str(conflict))
                    session.rollback()
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"unexpected caller-owned claim errors: {errors}"
    assert len(refusals) == 1, f"expected exactly one typed refusal, got {len(refusals)}"
    assert len(conflicts) == 1, f"expected exactly one typed slot conflict, got {len(conflicts)}"
    assert len(outcomes) == 1, f"expected exactly one winner, got {outcomes}"
    assert outcomes[0]["adopted"] is False
    winner_id = outcomes[0]["operation"]["operation_id"]
    assert winner_id in conflicts[0]
    assert len(oj.list_operations()) == 1
    # The loser adopts the durable winner by replaying its exact request.
    winner_request = oj.get_operation(winner_id)["request"]
    adopted = oj.claim_operation(oj.OperationRequest(**winner_request))
    assert adopted["adopted"] is True


# ---------------------------------------------------------------------------
# 4. refusal matrix before an operation can authorize effects
# ---------------------------------------------------------------------------


def test_claim_refuses_wrong_agent(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, agent_id=str(uuid.uuid4()))


def test_claim_refuses_wrong_session_name(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, session_name="cao-campaign-b")


def test_claim_refuses_wrong_lineage(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, lineage_id=str(uuid.uuid4()))


def test_claim_refuses_wrong_harness(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, harness="muse_code")


def test_claim_refuses_wrong_native_session_id(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, native_session_id="22222222-3333-4333-8444-555555555555")


def test_claim_refuses_wrong_source_incarnation(isolated_memory_db):
    """The prior incarnation id must be the agent's exact current incarnation —
    a noncurrent source refuses."""
    bind, contract = _dormant_worker()
    other = _bind_worker(
        terminal_id="c0ffee00",
        generation="00000000-0000-4000-8000-000000000099",
        native_session_id="22222222-3333-4333-8444-555555555555",
    )
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, prior_incarnation_id=other["incarnation"]["incarnation_id"])


def test_claim_refuses_wrong_prior_terminal_generation(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, prior_terminal_id="deadbeef")


def test_claim_refuses_wrong_role(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, role=roster.ROLE_SUPERVISOR)


def test_claim_refuses_wrong_profile_family(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, profile_family="reviewer")


def test_claim_refuses_non_dormant_agent(isolated_memory_db):
    """A still-live (never transitioned) agent refuses: reincarnation binds
    the exact retired prior incarnation."""
    bind, contract = _bound_live_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract)


def _bound_live_worker(agent_id=None, **bind_changes):
    """A bound worker with a published contract but NO dormant transition."""
    bind = _bind_worker(agent_id=agent_id, **bind_changes)
    contract = _contract_for(bind)
    rc.publish_contract(contract)
    return bind, contract


def test_claim_refuses_roster_revision_drift(isolated_memory_db):
    """A request carrying a stale roster revision refuses (the revision is an
    exact post-B1 fact)."""
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(
            bind,
            contract,
            roster_revision=roster.get_agent(bind["agent"]["agent_id"])["revision"] - 1,
        )


def test_claim_refuses_missing_restore_contract(isolated_memory_db):
    """A dormant source whose restore contract row was removed refuses."""
    bind, contract = _dormant_worker()
    contract_id = rc.get_contract_by_incarnation(
        terminal_id=bind["incarnation"]["terminal_id"],
        generation=bind["incarnation"]["generation"],
    )["contract_id"]
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text("DELETE FROM restore_contracts WHERE contract_id = :cid"),
            {"cid": contract_id},
        )
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, restore_contract_id=contract_id)


def test_claim_refuses_mismatched_restore_contract_digest(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, restore_contract_digest="d" * 64)


def test_claim_refuses_wrong_restore_contract_id(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, restore_contract_id=str(uuid.uuid4()))


def test_claim_refuses_wrong_restore_contract_schema(isolated_memory_db):
    bind, contract = _dormant_worker()
    with pytest.raises(oj.OperationJournalInvalid):
        _claim(bind, contract, restore_contract_schema="not-a-known-schema")


def test_claim_refuses_corrupt_stored_restore_contract(isolated_memory_db):
    """A stored restore contract whose canonical JSON was corrupted refuses
    the claim with a typed conflict."""
    bind, contract = _dormant_worker()
    _corrupt_stored_contract(
        isolated_memory_db,
        bind["incarnation"]["terminal_id"],
        bind["incarnation"]["generation"],
        '{"broken": true}',
    )
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract)
    assert len(oj.list_operations()) == 0


# ---------------------------------------------------------------------------
# 5. lifecycle binding policy
# ---------------------------------------------------------------------------


def test_undeclared_working_session_binds_epoch_zero_truthfully(isolated_memory_db):
    """A session nobody has declared anything about is working at epoch 0, and
    an operation binding that observation claims normally."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract, lifecycle_epoch=0, lifecycle_observation=sl.WORKING)
    assert record["operation"]["lifecycle_epoch"] == 0
    assert record["operation"]["lifecycle_observation"] == sl.WORKING


def test_declared_working_session_binds_at_its_exact_epoch(isolated_memory_db):
    bind, contract = _dormant_worker()
    _declare("cao-campaign-a", sl.WORKING)
    record = _claim(bind, contract, lifecycle_epoch=1, lifecycle_observation=sl.WORKING)
    assert record["operation"]["lifecycle_epoch"] == 1


def test_declared_paused_session_binds_per_accepted_design(isolated_memory_db):
    """An exact declared paused session follows the accepted design: the
    operation binds the paused observation at its epoch (effects still refuse
    once stopped, never while paused)."""
    bind, contract = _dormant_worker()
    _declare("cao-campaign-a", sl.PAUSED)
    record = _claim(bind, contract, lifecycle_epoch=1, lifecycle_observation=sl.PAUSED)
    assert record["operation"]["lifecycle_observation"] == sl.PAUSED
    intent = oj.authorize_effect_intent(
        record["operation"]["operation_id"],
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    assert intent["adopted"] is False


def test_stopped_session_refuses_at_claim(isolated_memory_db):
    """A stopped session refuses before any operation or effect can begin."""
    bind, contract = _dormant_worker()
    _stop_session("cao-campaign-a")
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, lifecycle_epoch=1, lifecycle_observation=sl.STOPPED)
    assert len(oj.list_operations()) == 0


def test_claim_refuses_lifecycle_observation_mismatch(isolated_memory_db):
    """The request's lifecycle observation must equal the declared one."""
    bind, contract = _dormant_worker()
    _declare("cao-campaign-a", sl.PAUSED)
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, lifecycle_epoch=1, lifecycle_observation=sl.WORKING)


def test_claim_refuses_lifecycle_epoch_drift(isolated_memory_db):
    """The request's lifecycle epoch must equal the declared one."""
    bind, contract = _dormant_worker()
    _declare("cao-campaign-a", sl.WORKING)
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract, lifecycle_epoch=0, lifecycle_observation=sl.WORKING)


# ---------------------------------------------------------------------------
# 6. effect-intent vs barrier linearization
# ---------------------------------------------------------------------------


def test_effect_intent_wins_then_barrier_claim_preserves_in_flight_intent(isolated_memory_db):
    """An effect intent authorized immediately before a barrier claim is
    preserved for later M3-C reconciliation: the intent row stays readable
    and the operation's phase reflects the authorized step, while no later
    step can begin."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]

    intent = oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    assert intent["adopted"] is False
    assert intent["operation"]["phase"] == oj.EFFECT_STEP_FENCE_PRIOR

    barrier = oj.claim_session_barrier("cao-campaign-a", claimed_by=operation_id, reason="stop")
    assert barrier["adopted"] is False
    assert barrier["state"] == oj.BARRIER_CLAIMED

    # The in-flight intent is preserved exactly for M3-C to adopt/drain.
    stored = oj.get_effect_intent(intent["intent"]["effect_id"])
    assert stored["effect_id"] == intent["intent"]["effect_id"]
    assert stored["effect_digest"] == intent["intent"]["effect_digest"]
    assert len(oj.list_effect_intents(operation_id)) == 1

    # The correct next step is still refused by the barrier: no later phase
    # may begin after Stop claimed the session.
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
        )


def test_barrier_wins_then_effect_admits_no_later_phase(isolated_memory_db):
    """A barrier claimed first admits no later effect phase and no intent
    row is created."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    assert oj.list_effect_intents(record["operation"]["operation_id"]) == []


def test_claim_refuses_when_barrier_already_claimed(isolated_memory_db):
    """Claiming a reincarnation operation into a session whose barrier is
    already claimed refuses: no future effect can ever be authorized."""
    bind, contract = _dormant_worker()
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    with pytest.raises(oj.OperationJournalConflict):
        _claim(bind, contract)
    assert len(oj.list_operations()) == 0


def test_barrier_is_never_auto_cleared(isolated_memory_db):
    """A claimed barrier stays claimed forever: replaying the claim adopts it,
    a different claimer cannot overwrite it, and there is no expiry."""
    first = oj.claim_session_barrier(
        "cao-campaign-a", claimed_by="stop-op-1", reason="operator stop"
    )
    second = oj.claim_session_barrier(
        "cao-campaign-a", claimed_by="stop-op-2", reason="second stop"
    )
    assert second["adopted"] is True
    assert second["claimed_by"] == "stop-op-1"
    assert oj.get_session_barrier("cao-campaign-a")["state"] == oj.BARRIER_CLAIMED
    assert "expires" not in oj.get_session_barrier("cao-campaign-a")


def test_barriers_are_per_session(isolated_memory_db):
    bind, contract = _dormant_worker()
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    assert oj.get_session_barrier("cao-campaign-a")["state"] == oj.BARRIER_CLAIMED
    assert oj.get_session_barrier("cao-campaign-b") is None
    bind_b, contract_b = _dormant_worker(
        session_name="cao-campaign-b",
        terminal_id="b2b2b2b2",
        generation="00000000-0000-4000-8000-0000000000bb",
        native_session_id="33333333-4444-4333-8444-555555555555",
    )
    record = _claim(bind_b, contract_b)
    assert record["adopted"] is False


# ---------------------------------------------------------------------------
# 7. every subsequent phase authorization rechecks the same facts
# ---------------------------------------------------------------------------


def test_second_effect_phase_requires_the_next_step_and_current_phase(isolated_memory_db):
    """The seam authorizes only the exact NEXT step after the current journal
    phase: a stale expected phase refuses, and the correct next step with the
    current phase succeeds and advances the phase to that step."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    # A second step with a stale expected phase refuses...
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.PHASE_CLAIMED,
        )
    # ...and the exact next step with the current phase succeeds.
    later = oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_REAP_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
        expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
    )
    assert later["adopted"] is False
    assert later["operation"]["phase"] == oj.EFFECT_STEP_REAP_PRIOR
    assert len(oj.list_effect_intents(operation_id)) == 2


def test_phase_authorization_refuses_after_lifecycle_stopped(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _stop_session("cao-campaign-a")
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    assert oj.list_effect_intents(record["operation"]["operation_id"]) == []


def test_phase_authorization_refuses_after_lifecycle_epoch_drift(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _declare("cao-campaign-a", sl.WORKING)  # epoch 0 -> 1
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_phase_authorization_refuses_after_barrier_claimed(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_phase_authorization_refuses_after_roster_revision_drift(isolated_memory_db):
    """A concurrent roster transition that bumps the live revision after the
    claim refuses every later effect phase."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text("UPDATE stable_agents SET revision = revision + 1 WHERE agent_id = :aid"),
            {"aid": bind["agent"]["agent_id"]},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_phase_authorization_refuses_after_agent_no_longer_dormant(isolated_memory_db):
    """A successor that re-livens the agent (disposition no longer dormant)
    refuses every later effect phase."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text("UPDATE stable_agents SET disposition = 'live' WHERE agent_id = :aid"),
            {"aid": bind["agent"]["agent_id"]},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_phase_authorization_refuses_after_restore_contract_digest_drift(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _corrupt_stored_contract(
        isolated_memory_db,
        bind["incarnation"]["terminal_id"],
        bind["incarnation"]["generation"],
        '{"broken": true}',
    )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_phase_authorization_refuses_after_source_incarnation_released(isolated_memory_db):
    """The prior incarnation must stay retired: a re-livened source refuses."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE stable_agent_incarnations SET disposition = 'bound' "
                "WHERE incarnation_id = :iid"
            ),
            {"iid": bind["incarnation"]["incarnation_id"]},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_exact_effect_intent_replay_adopts(isolated_memory_db):
    """Replaying the identical effect intent with the ORIGINAL call arguments
    (same effect id, step, payload, and expected phase) converges to one
    intent row even after the phase advanced."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    effect_id = str(uuid.uuid4())
    payload = {"generation": bind["incarnation"]["generation"]}
    first = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    assert first["adopted"] is False
    assert first["operation"]["phase"] == oj.EFFECT_STEP_FENCE_PRIOR
    # The retry uses the exact original arguments — no expected_phase, no
    # updated phase — and adopts the durable intent.
    second = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    assert second["adopted"] is True
    assert second["intent"]["effect_id"] == effect_id
    assert len(oj.list_effect_intents(operation_id)) == 1


def test_changed_effect_payload_for_same_effect_id_conflicts(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    effect_id = str(uuid.uuid4())
    oj.authorize_effect_intent(
        record["operation"]["operation_id"],
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=effect_id,
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": "other"},
        )
    assert len(oj.list_effect_intents(record["operation"]["operation_id"])) == 1


# ---------------------------------------------------------------------------
# coordinator repairs: replay adopts durable truth; step uniqueness/progression
# ---------------------------------------------------------------------------


def test_operation_replay_adopts_after_post_commit_barrier_claim(isolated_memory_db):
    """An exact operation claim replay (the ORIGINAL stored request bytes)
    adopts the durable winner even after a Stop barrier was claimed following
    the first commit — the committed row is the truth, not the changed live
    state."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    record = _claim(bind, contract, operation_id=operation_id)
    request = oj.get_operation(operation_id)["request"]
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    replay = oj.claim_operation(oj.OperationRequest(**request))
    assert replay["adopted"] is True
    assert replay["operation"]["operation_id"] == record["operation"]["operation_id"]
    assert len(oj.list_operations()) == 1


def test_operation_replay_adopts_after_lifecycle_and_roster_drift(isolated_memory_db):
    """An exact replay of the ORIGINAL stored request adopts after
    lifecycle-epoch drift and roster-revision drift land post-commit, while a
    changed request under the same id still conflicts."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    _claim(bind, contract, operation_id=operation_id)
    request = oj.get_operation(operation_id)["request"]
    _declare("cao-campaign-a", sl.WORKING)  # lifecycle epoch 0 -> 1
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text("UPDATE stable_agents SET revision = revision + 1 WHERE agent_id = :aid"),
            {"aid": bind["agent"]["agent_id"]},
        )
    replay = oj.claim_operation(oj.OperationRequest(**request))
    assert replay["adopted"] is True
    with pytest.raises(oj.OperationJournalConflict):
        oj.claim_operation(oj.OperationRequest(**{**request, "model_requested": "claude-opus-5"}))
    assert len(oj.list_operations()) == 1


def test_effect_replay_with_original_expected_phase_adopts_after_phase_advance_and_barrier(
    isolated_memory_db,
):
    """An exact effect-intent replay with the ORIGINAL call arguments adopts
    after the phase advanced AND after a barrier/lifecycle change lands, with
    zero new mutation — while a genuinely new intent stays refused by the
    barrier."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    effect_id = str(uuid.uuid4())
    payload = {"generation": bind["incarnation"]["generation"]}
    first = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    assert first["adopted"] is False
    assert first["operation"]["phase"] == oj.EFFECT_STEP_FENCE_PRIOR

    # Replay with the exact original arguments after the phase advanced.
    replay = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    assert replay["adopted"] is True
    # An older caller passing the original default expected phase explicitly
    # adopts just the same: the recorded intent is checked before any phase
    # gate.
    replay_explicit = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
        expected_phase=oj.PHASE_CLAIMED,
    )
    assert replay_explicit["adopted"] is True

    # The barrier claimed after the intent still lets the exact replay adopt.
    oj.claim_session_barrier("cao-campaign-a", claimed_by="stop", reason="stop")
    _declare("cao-campaign-a", sl.WORKING)  # lifecycle epoch also drifts
    replay2 = oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    assert replay2["adopted"] is True
    assert len(oj.list_effect_intents(operation_id)) == 1

    # A genuinely new intent is refused by the barrier.
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
        )


def test_same_logical_step_cannot_gain_two_effect_ids(isolated_memory_db):
    """One logical physical step has exactly one intent: a different effect id
    for an already-won step surfaces the durable winner through a typed
    conflict and the step read path — never a second intent."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    first = oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    with pytest.raises(oj.OperationJournalConflict) as excinfo:
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
        )
    assert first["intent"]["effect_id"] in str(excinfo.value)
    assert len(oj.list_effect_intents(operation_id)) == 1
    winner = oj.get_effect_intent_by_step(operation_id, oj.EFFECT_STEP_FENCE_PRIOR)
    assert winner["effect_id"] == first["intent"]["effect_id"]


def test_concurrent_same_step_different_effect_ids_one_winner(file_db, monkeypatch):
    """Two threads authorize the SAME logical step with different effect ids
    against one SQLite file: exactly one intent row, one winner, and one typed
    conflict naming the winner — never two intents for one physical step."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    barrier = threading.Barrier(2)
    monkeypatch.setattr(oj, "_now", _gate_first_call(oj._now, barrier))

    results: list[dict] = []
    conflicts: list[str] = []
    others: list[BaseException] = []

    def run() -> None:
        try:
            results.append(
                oj.authorize_effect_intent(
                    operation_id,
                    effect_id=str(uuid.uuid4()),
                    effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
                    effect_payload={"generation": bind["incarnation"]["generation"]},
                )
            )
        except oj.OperationJournalConflict as exc:
            conflicts.append(str(exc))
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            others.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert others == [], f"unexpected step-race errors: {others}"
    assert len(results) == 1, f"expected exactly one step winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one typed conflict, got {len(conflicts)}"
    assert len(oj.list_effect_intents(operation_id)) == 1
    assert results[0]["intent"]["effect_id"] in conflicts[0]
    winner = oj.get_effect_intent_by_step(operation_id, oj.EFFECT_STEP_FENCE_PRIOR)
    assert winner["effect_id"] == results[0]["intent"]["effect_id"]


def test_skipped_step_cannot_be_authorized(isolated_memory_db):
    """A step that skips ahead of the accepted sequence refuses with zero
    intent rows."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_CREATE_PANE,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_ADMIT_INPUT,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    assert oj.list_effect_intents(operation_id) == []
    assert oj.get_operation(operation_id)["phase"] == oj.PHASE_CLAIMED


def test_reversed_step_cannot_be_authorized(isolated_memory_db):
    """A step earlier in the accepted sequence refuses once the operation has
    moved past it: the logical step is already won, and the progression check
    never moves backwards."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_REAP_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
        expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
    )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.EFFECT_STEP_REAP_PRIOR,
        )
    assert len(oj.list_effect_intents(operation_id)) == 2


def test_concurrent_out_of_order_step_cannot_be_authorized(file_db):
    """A step racing ahead of the accepted sequence is refused in every
    interleaving: the progression check is read from the operation row, so the
    out-of-order caller can never win."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]

    results: list[dict] = []
    conflicts: list[BaseException] = []
    others: list[BaseException] = []

    def run(step: str) -> None:
        try:
            results.append(
                oj.authorize_effect_intent(
                    operation_id,
                    effect_id=str(uuid.uuid4()),
                    effect_step=step,
                    effect_payload={"generation": bind["incarnation"]["generation"]},
                )
            )
        except oj.OperationJournalConflict as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            others.append(exc)

    threads = [
        threading.Thread(target=run, args=(oj.EFFECT_STEP_FENCE_PRIOR,)),
        threading.Thread(target=run, args=(oj.EFFECT_STEP_CREATE_PANE,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert others == [], f"unexpected out-of-order errors: {others}"
    assert len(results) == 1, f"expected exactly one in-order winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one skip refusal, got {len(conflicts)}"
    assert len(oj.list_effect_intents(operation_id)) == 1


def test_session_name_aliases_converge_to_canonical_request(isolated_memory_db):
    """Bare and canonical session-name spellings converge to the same canonical
    request: identical digest, identical stored session name, and the second
    claim adopts the first."""
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    bare = _operation_request(bind, contract, operation_id=operation_id, session_name="campaign-a")
    canonical = _operation_request(
        bind, contract, operation_id=operation_id, session_name="cao-campaign-a"
    )
    assert bare.session_name == "cao-campaign-a"
    assert bare.digest() == canonical.digest()
    record = oj.claim_operation(bare)
    assert record["operation"]["session_name"] == "cao-campaign-a"
    adopted = oj.claim_operation(canonical)
    assert adopted["adopted"] is True


def test_partial_compatibility_evidence_is_invalid(isolated_memory_db):
    """A compatibility-cell reference without its digest (or vice versa) is
    refused at construction: partial compatibility evidence never reaches the
    store."""
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(
            None, None, compatibility_cell_ref=_CELL_REF, compatibility_cell_digest=None
        )
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(
            None, None, compatibility_cell_ref=None, compatibility_cell_digest=_CELL_DIGEST
        )
    # Absent cell (no route/mode variation) remains valid.
    request = _operation_request_constructor(
        None, None, compatibility_cell_ref=None, compatibility_cell_digest=None
    )
    assert request.compatibility_cell_ref is None
    assert request.compatibility_cell_digest is None


# ---------------------------------------------------------------------------
# coordinator repair pass 2: intent byte truth, observed phase, checked CAS
# ---------------------------------------------------------------------------


def test_corrupt_stored_effect_payload_replay_is_typed_refusal(isolated_memory_db):
    """An intent replay whose STORED canonical payload bytes were corrupted is
    a bounded typed conflict — never adopted — even when the digest column is
    intact, and the read surface still returns the stored bytes."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    effect_id = str(uuid.uuid4())
    payload = {"generation": bind["incarnation"]["generation"]}
    oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE reincarnation_effect_intents SET effect_payload_json = :json "
                "WHERE effect_id = :eid"
            ),
            {"json": '{"broken": true}', "eid": effect_id},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=effect_id,
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload=payload,
        )
    assert len(oj.list_effect_intents(operation_id)) == 1
    stored = oj.get_effect_intent(effect_id)
    assert stored["effect_payload_json"] == '{"broken": true}'


def test_corrupt_stored_effect_digest_replay_is_typed_refusal(isolated_memory_db):
    """A corrupted stored digest column is likewise a typed conflict, never an
    adoption."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    effect_id = str(uuid.uuid4())
    payload = {"generation": bind["incarnation"]["generation"]}
    oj.authorize_effect_intent(
        operation_id,
        effect_id=effect_id,
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload=payload,
    )
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE reincarnation_effect_intents SET effect_digest = :digest "
                "WHERE effect_id = :eid"
            ),
            {"digest": "0" * 64, "eid": effect_id},
        )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=effect_id,
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload=payload,
        )
    assert len(oj.list_effect_intents(operation_id)) == 1


def test_stale_delayed_authorization_requires_observed_phase(isolated_memory_db):
    """A delayed caller that never observed the current journal phase cannot
    authorize the next step: the mandatory expected phase must equal the
    operation's current phase.  The convenient first-step default
    (``claimed``) is refused once the operation has advanced."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    # A stale delayed caller using the first-step default while the operation
    # is at fence_prior is refused: it never observed the transition.
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    assert len(oj.list_effect_intents(operation_id)) == 1
    # A caller that observed the current phase authorizes normally.
    later = oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_REAP_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
        expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
    )
    assert later["adopted"] is False


def test_lost_phase_cas_leaves_no_intent(file_db, monkeypatch):
    """A lost phase CAS is a checked one-winner transition: when the phase no
    longer matches the observed phase at the CAS, the caller receives a typed
    conflict and NO intent row survives — the operation phase never represents
    an intent it did not win."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    real_update = oj.sa_update

    def lost_cas_update(*args, **kwargs):
        # Force the CAS to match zero rows: an impossible additional predicate
        # makes the UPDATE a deterministic no-op with rowcount 0.
        statement = real_update(*args, **kwargs)
        return statement.where(
            database.ReincarnationOperationModel.operation_id == "no-such-operation"
        )

    monkeypatch.setattr(oj, "sa_update", lost_cas_update)
    session = database.SessionLocal()
    try:
        with pytest.raises(oj.OperationJournalConflict):
            oj.authorize_effect_intent(
                operation_id,
                effect_id=str(uuid.uuid4()),
                effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
                effect_payload={"generation": bind["incarnation"]["generation"]},
                db=session,
            )
        session.rollback()
    finally:
        session.close()
    assert oj.list_effect_intents(operation_id) == []
    assert oj.get_operation(operation_id)["phase"] == oj.PHASE_CLAIMED


def test_normalized_session_name_is_bounded(isolated_memory_db):
    """The normalized (prefix-bearing) session name is bounded, so adding the
    ``cao-`` prefix cannot exceed the limit."""
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, session_name="a" * oj.MAX_SESSION_LEN)
    request = _operation_request_constructor(
        None, None, session_name="a" * (oj.MAX_SESSION_LEN - 4)
    )
    assert len(request.session_name) <= oj.MAX_SESSION_LEN


# ---------------------------------------------------------------------------
# 8. restart and migration preserve operation, winner slot, barrier, intent
# ---------------------------------------------------------------------------


def test_operation_intent_and_barrier_survive_restart(tmp_path, monkeypatch):
    """Claim an operation, authorize an intent, and claim the barrier through
    a real SQLite file, then dispose and reopen the engine (simulating a
    cao-server restart): every query returns the same truth and the replay
    claim adopts."""
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
    bind, contract = _dormant_worker()
    operation_id = str(uuid.uuid4())
    record = _claim(bind, contract, operation_id=operation_id)
    intent = oj.authorize_effect_intent(
        operation_id,
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )

    for engine in engines:
        engine.dispose()
    engines.clear()

    # Restart: the claim replay adopts while the barrier is still unclaimed.
    _attach()
    replay = _claim(bind, contract, operation_id=operation_id)
    assert replay["adopted"] is True
    read = oj.get_operation(operation_id)
    assert read["request_digest"] == record["operation"]["request_digest"]
    assert read["phase"] == oj.EFFECT_STEP_FENCE_PRIOR
    by_slot = oj.get_operation_by_slot(
        agent_id=bind["agent"]["agent_id"],
        prior_incarnation_id=bind["incarnation"]["incarnation_id"],
        lifecycle_epoch=0,
        roster_revision=read["roster_revision"],
    )
    assert by_slot["operation_id"] == operation_id
    assert (
        oj.get_effect_intent(intent["intent"]["effect_id"])["effect_digest"]
        == intent["intent"]["effect_digest"]
    )

    # The barrier claim survives the restart and freezes later phases.
    barrier = oj.claim_session_barrier("cao-campaign-a", claimed_by=operation_id, reason="stop")
    assert barrier["state"] == oj.BARRIER_CLAIMED
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_REAP_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
            expected_phase=oj.EFFECT_STEP_FENCE_PRIOR,
        )
    # A claim replay after the barrier is claimed ADOPTS the exact durable
    # winner: response loss after Stop resolves by adoption/query, never a
    # second claim or a false conflict.
    replay2 = _claim(bind, contract, operation_id=operation_id)
    assert replay2["adopted"] is True


# ---------------------------------------------------------------------------
# 9. malformed/corrupt stored JSON or duplicated columns -> bounded typed
# ---------------------------------------------------------------------------


def test_corrupt_stored_request_json_typed_refusal(isolated_memory_db):
    """A corrupted stored request payload refuses every later phase with a
    typed conflict — never a raw JSON/SQL error — and the read surface still
    returns the stored bytes without crashing."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _corrupt_request_json(isolated_memory_db, record["operation"]["operation_id"], "{oops")
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    stored = oj.get_operation(record["operation"]["operation_id"])
    assert stored["request_json"] == "{oops"


def test_corrupt_stored_request_digest_typed_refusal(isolated_memory_db):
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _update_operation_column(
        isolated_memory_db, record["operation"]["operation_id"], "request_digest", "0" * 64
    )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_duplicated_column_drift_typed_refusal(isolated_memory_db):
    """A duplicated column that drifts away from the decoded request refuses
    every later phase with a typed conflict."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    _update_operation_column(
        isolated_memory_db, record["operation"]["operation_id"], "agent_id", str(uuid.uuid4())
    )
    with pytest.raises(oj.OperationJournalConflict):
        oj.authorize_effect_intent(
            record["operation"]["operation_id"],
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )


def test_stored_operation_refusal_maps_malformed_shapes_to_reasons(isolated_memory_db):
    """The bounded reconciliation predicate maps every malformed shape to a
    typed reason string and returns None only for a complete canonical row."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)["operation"]
    assert oj.stored_operation_refusal(record) is None

    missing_json = dict(record)
    missing_json.pop("request_json")
    assert isinstance(oj.stored_operation_refusal(missing_json), str)

    corrupt = dict(record)
    corrupt["request_json"] = "{not json"
    assert isinstance(oj.stored_operation_refusal(corrupt), str)

    non_canonical = dict(record)
    non_canonical["request_json"] = '{"operation_id": "x"}'
    assert isinstance(oj.stored_operation_refusal(non_canonical), str)

    drift = dict(record)
    drift["agent_id"] = str(uuid.uuid4())
    assert isinstance(oj.stored_operation_refusal(drift), str)


def test_malformed_effect_payload_refused_at_authorize(isolated_memory_db):
    """Malformed effect intents (non-mapping payload, oversized, non-string
    values, unknown step) refuse with zero rows."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)
    operation_id = record["operation"]["operation_id"]
    with pytest.raises(oj.OperationJournalInvalid):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload=["not", "a", "mapping"],
        )
    with pytest.raises(oj.OperationJournalInvalid):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"pid": 4242},
        )
    with pytest.raises(oj.OperationJournalInvalid):
        oj.authorize_effect_intent(
            operation_id,
            effect_id=str(uuid.uuid4()),
            effect_step="not-a-step",
            effect_payload={"generation": bind["incarnation"]["generation"]},
        )
    assert oj.list_effect_intents(operation_id) == []


def test_unknown_operation_and_unknown_intent_are_typed_not_found(isolated_memory_db):
    with pytest.raises(oj.OperationJournalNotFound):
        oj.get_operation(str(uuid.uuid4()))
    with pytest.raises(oj.OperationJournalNotFound):
        oj.authorize_effect_intent(
            str(uuid.uuid4()),
            effect_id=str(uuid.uuid4()),
            effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
            effect_payload={"generation": "g"},
        )


def test_operation_record_stores_no_verdict_and_no_unexpected_keys(isolated_memory_db):
    """The compatibility cell is recorded as a bounded reference/digest — never
    inferred as passing — and the stored record carries exactly the bound
    schema columns, no verdict or capability flag."""
    bind, contract = _dormant_worker()
    record = _claim(bind, contract)["operation"]
    assert record["compatibility_cell_ref"] == _CELL_REF
    assert record["compatibility_cell_digest"] == _CELL_DIGEST
    assert "compatibility_verified" not in record
    assert "capability_verdict" not in record
    stored = oj.get_operation(record["operation_id"])
    assert stored["request"]["compatibility_cell_digest"] == _CELL_DIGEST


def test_malformed_operation_requests_refuse_at_construction(isolated_memory_db):
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(bind=None, contract=None, operation_id="not-a-uuid")


def _operation_request_constructor(bind, contract, **changes):
    """Construction-only request (no store interaction) for input validation."""
    payload = {
        "operation_id": str(uuid.uuid4()),
        "session_name": "cao-campaign-a",
        "agent_id": "aaaaaaaa-1111-4111-8111-111111111111",
        "roster_revision": 2,
        "role": roster.ROLE_WORKER,
        "profile_family": "developer",
        "lineage_id": "bbbbbbbb-2222-4222-8222-222222222222",
        "harness": "claude_code",
        "native_session_id": _NATIVE_ID,
        "prior_terminal_id": "a1b2c3d4",
        "prior_generation": "00000000-0000-4000-8000-000000000001",
        "prior_incarnation_id": "cccccccc-3333-4333-8333-333333333333",
        "lifecycle_epoch": 0,
        "lifecycle_observation": sl.WORKING,
        "restore_contract_id": "dddddddd-4444-4444-8444-444444444444",
        "restore_contract_digest": _DIGEST64,
        "restore_contract_schema": rc.SCHEMA_VERSION,
        "route_provider": "claude_code",
        "model_requested": "claude-sonnet-4-5",
        "effort_requested": "high",
        "execution_mode_requested": "native_tui",
        "compatibility_cell_ref": _CELL_REF,
        "compatibility_cell_digest": _CELL_DIGEST,
    }
    payload.update(changes)
    return oj.OperationRequest(**payload)


def test_invalid_operation_request_fields_refuse(isolated_memory_db):
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, native_session_id=None)
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, restore_contract_digest="short")
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, lifecycle_epoch=-1)
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, execution_mode_requested="bogus-mode")
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, role="not-a-role")
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, lifecycle_observation="not-a-lifecycle")
    with pytest.raises(oj.OperationJournalInvalid):
        _operation_request_constructor(None, None, compatibility_cell_digest="zzz")


# ---------------------------------------------------------------------------
# 10. this slice invokes no physical or supervisory effect
# ---------------------------------------------------------------------------


def test_slice_imports_no_physical_or_supervisory_effect_modules():
    """B2 is a pure durable-store seam: the module may import only the store
    and identity readers, never a tmux/provider/attachment/input/Stop/control
    module."""
    source = Path(oj.__file__).read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", source, flags=re.M)
    banned_prefixes = (
        "cli_agent_orchestrator.clients.tmux",
        "cli_agent_orchestrator.services.tmux",
        "tmux",
        "managed_launch",
        "native_attachment",
        "native_pane_input",
        "native_tui_launch",
        "provider_launcher",
        "provider_controls",
        "provider_contracts",
        "control_input",
        "delivery_journal",
        "terminal_service",
        "session_service",
        "pane_observer",
        "claude_native",
        "codex_native",
        "kimi_native",
        "muse_native",
        "glm_native",
        "native_status_repair",
        "status_fusion",
        "operator_message",
        "workflow",
        "agent_scaffold",
        "agent_step",
        "task",
        "supervisor",
        "flow_service",
        "conductor",
    )
    for name in imports:
        for banned in banned_prefixes:
            assert not name.startswith(banned), (
                f"operation_journal imports effectful module {name!r}; "
                "B2 must be a pure durable-store seam"
            )


def test_claim_and_authorize_touch_only_durable_store_rows(isolated_memory_db, tmp_path):
    """The full claim -> intent -> barrier flow creates rows only in the three
    new journal tables (plus the pre-seeded roster/contract rows): no tmux,
    terminal, session-env, attachment, inbox, or control rows are produced."""
    bind, contract = _dormant_worker()
    before = _table_row_counts(isolated_memory_db)
    record = _claim(bind, contract)
    oj.authorize_effect_intent(
        record["operation"]["operation_id"],
        effect_id=str(uuid.uuid4()),
        effect_step=oj.EFFECT_STEP_FENCE_PRIOR,
        effect_payload={"generation": bind["incarnation"]["generation"]},
    )
    oj.claim_session_barrier(
        "cao-campaign-a", claimed_by=record["operation"]["operation_id"], reason="stop"
    )
    after = _table_row_counts(isolated_memory_db)
    new_tables = {name for name, count in after.items() if count != before.get(name, 0)}
    assert new_tables <= {
        "reincarnation_operations",
        "reincarnation_effect_intents",
        "session_effect_barriers",
    }, f"unexpected rows written to: {sorted(new_tables)}"


def _table_row_counts(engine) -> dict[str, int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")
        ).fetchall()
        counts: dict[str, int] = {}
        for (name,) in rows:
            try:
                counts[name] = conn.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
            except Exception:  # noqa: BLE001 - view/odd table: skip
                continue
    return counts
