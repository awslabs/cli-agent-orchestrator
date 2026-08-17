"""Immutable restore-contract substrate + atomic dormant transition (cond-0378 B1).

B1 is the fork-owned primitive that lets a later B2/B3 executor decide
whether an exact same-native-session resume is supported *without
reconstructing mutable ambient state*: a versioned, append-only,
immutable restore-contract record tied to the stable agent, native
lineage, and exact source incarnation, plus a narrow roster transition
that atomically retires the exact live source incarnation and marks its
stable agent dormant while preserving/linking that contract.

Every published contract is bound to the authoritative M3-A roster source:
``publish_contract`` resolves the exact roster incarnation and verifies the
contract against it (agent, lineage, terminal/generation, lineage harness,
native session id including truthful ``None``, bounded route provenance, and
incarnation execution mode) before the immutable slot is consumed.  The
dormant transition revalidates the stored contract against the same
authoritative rows and serializes concurrent transitions with a
conditional-write/CAS so exactly one call performs the live->retired/dormant
mutation and the other adopts.

These tests are the deterministic store contract, written before the
service changes that satisfy them.  No provider, tmux, or network I/O is
touched: every assertion runs against the ORM store via
``isolated_memory_db`` (or a real file database for the concurrency tests)
and the module's own clock.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.clients.database import Base
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import stable_agent_roster as roster

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

_DIGEST64 = "a" * 64
_NATIVE_ID = "11111111-2222-4333-8444-555555555555"

# Canonical-by-construction fixture paths. The contract validator requires
# canonical absolute paths (no '.', '..', or symlink aliasing, checked with
# os.path.realpath), so a literal naming a real operator path — the previous
# data used /Users/colin/... — fails on any machine where a component is a
# symlink (a dotfile-managed ~/.claude is one). These names exist on no real
# machine, so realpath returns them byte-identical everywhere.
_WORKTREE = "/cao-test-fixture/worktree"
_ALT_WORKTREE = "/cao-test-fixture/other"
_PROFILE_CONFIG = "/cao-test-fixture/profile/settings.json"


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
        "working_directory": _WORKTREE,
        "trusted_project_root": _WORKTREE,
        "executable": _fact({"path": "/usr/local/bin/claude", "sha256": _DIGEST64}),
        "profile_material": _fact(
            {
                "profile_config_path": _PROFILE_CONFIG,
                "profile_config_sha256": "b" * 64,
            }
        ),
        "provider_home_facts": rc.ContractFact.unavailable(
            "no provider-home carrier facts at this source seam"
        ),
    }
    payload.update(changes)
    return rc.RestoreContract(**payload)


def _bound_worker(agent_id=None, **bind_changes):
    """Bind a roster source and return (bind, contract bound to that source)."""
    bind = _bind_worker(agent_id=agent_id, **bind_changes)
    return bind, _contract_for(bind)


def _detached_contract(**changes):
    """A free-standing contract with fixed valid identities (construction-only)."""
    payload = {
        "agent_id": "aaaaaaaa-1111-4111-8111-111111111111",
        "lineage_id": "bbbbbbbb-2222-4222-8222-222222222222",
        "terminal_id": "a1b2c3d4",
        "generation": "00000000-0000-4000-8000-000000000001",
        "native_session_id": _NATIVE_ID,
        "harness": "claude_code",
        "provider": "claude_code",
        "route_provenance": {"provider_route": "anthropic"},
        "execution_mode": "native_tui",
        "model": _fact("claude-sonnet-4-5"),
        "effort": _fact("high"),
        "working_directory": _WORKTREE,
        "trusted_project_root": _WORKTREE,
        "executable": _fact({"path": "/usr/local/bin/claude", "sha256": _DIGEST64}),
        "profile_material": _fact(
            {
                "profile_config_path": _PROFILE_CONFIG,
                "profile_config_sha256": "b" * 64,
            }
        ),
        "provider_home_facts": rc.ContractFact.unavailable(
            "no provider-home carrier facts at this source seam"
        ),
    }
    payload.update(changes)
    return rc.RestoreContract(**payload)


def _transition(bind, contract, **changes):
    args = {
        "terminal_id": contract.terminal_id,
        "generation": contract.generation,
        "agent_id": contract.agent_id,
        "lineage_id": contract.lineage_id,
        "contract_digest": contract.digest(),
        "reason": "pane lost",
    }
    args.update(changes)
    return roster.transition_dormant(**args)


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


def _corrupt_contract_json(isolated_memory_db, terminal_id, generation, new_json):
    """Overwrite the persisted restore-contract payload directly (raw SQL), the
    only way a malformed/corrupt stored contract can exist now that publication
    binds identity.

    The stored digest is recomputed to match the new payload, so the refusal the
    transition produces comes from the stored-record decoder/validator, not from
    a content/digest divergence.
    """
    new_digest = hashlib.sha256(new_json.encode("utf-8")).hexdigest()
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE restore_contracts SET contract_json=:j, contract_digest=:d "
                "WHERE terminal_id=:t AND generation=:g"
            ),
            {"j": new_json, "d": new_digest, "t": terminal_id, "g": generation},
        )


def _corrupt_contract_json_keep_digest(isolated_memory_db, terminal_id, generation, new_json):
    """Overwrite only the payload, leaving the stored digest column untouched —
    the content/digest-divergence corruption."""
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "UPDATE restore_contracts SET contract_json=:j "
                "WHERE terminal_id=:t AND generation=:g"
            ),
            {"j": new_json, "t": terminal_id, "g": generation},
        )


def _corrupt_column(isolated_memory_db, terminal_id, generation, column, value):
    """Overwrite one duplicated restore-contract row column directly (raw SQL),
    leaving the stored JSON and digest untouched — the column-only mismatch."""
    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                f"UPDATE restore_contracts SET {column}=:v "
                "WHERE terminal_id=:t AND generation=:g"
            ),
            {"v": value, "t": terminal_id, "g": generation},
        )


def _read_stored_payload(isolated_memory_db, terminal_id, generation) -> str:
    with isolated_memory_db.begin() as conn:
        row = conn.execute(
            text(
                "SELECT contract_json FROM restore_contracts "
                "WHERE terminal_id=:t AND generation=:g"
            ),
            {"t": terminal_id, "g": generation},
        ).fetchone()
    return row[0]


def _rewrite_stored_payload(isolated_memory_db, bind, contract, mutate) -> str:
    """Rewrite the persisted contract_json through ``mutate(parsed)``, keeping the
    record digest-consistent (json AND digest column), and return the NEW digest
    so the caller can present it to the transition.  This exercises the stored-
    record decoder/validator boundary rather than the caller-vs-stored digest
    check."""
    parsed = json.loads(
        _read_stored_payload(isolated_memory_db, contract.terminal_id, contract.generation)
    )
    mutate(parsed)
    new_json = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    _corrupt_contract_json(isolated_memory_db, contract.terminal_id, contract.generation, new_json)
    return hashlib.sha256(new_json.encode("utf-8")).hexdigest()


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
# immutable exact replay / conflict / append history
# ---------------------------------------------------------------------------


def test_identical_publication_adopts_existing_contract(isolated_memory_db):
    """Repeating the identical contract publication adopts/returns the existing
    record — one row, one digest, no history rewritten."""
    bind, contract = _bound_worker()
    first = rc.publish_contract(contract)
    second = rc.publish_contract(contract)
    assert second["adopted"] is True
    assert second["contract_id"] == first["contract_id"]
    assert second["contract_digest"] == first["contract_digest"]
    assert second["created_at"] == first["created_at"]
    assert len(rc.list_contracts()) == 1


def test_changed_content_for_same_source_incarnation_conflicts(isolated_memory_db):
    """Changed content for the same source incarnation conflicts rather than
    overwriting; the original record stays byte-identical."""
    bind, contract = _bound_worker()
    original = rc.publish_contract(contract)
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, working_directory=_ALT_WORKTREE))
    contracts = rc.list_contracts()
    assert len(contracts) == 1
    assert contracts[0]["contract_digest"] == original["contract_digest"]


def test_successor_source_incarnation_appends_history(isolated_memory_db):
    """A fresh physical incarnation of the SAME stable agent, SAME native
    lineage, and SAME native session id appends a second contract; both records
    remain readable and share the identity."""
    agent_id = str(uuid.uuid4())
    first_bind = _bind_worker(
        agent_id=agent_id,
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
    )
    first_contract = _contract_for(first_bind)
    rc.publish_contract(first_contract)
    # Ordinary teardown retires the first physical incarnation so a fresh one
    # can bind on the same native lineage.
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="pane lost",
    )
    second_bind = _bind_worker(
        agent_id=agent_id,
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000003",
    )
    assert second_bind["lineage"]["lineage_id"] == first_bind["lineage"]["lineage_id"]
    second_contract = _contract_for(second_bind)
    record = rc.publish_contract(second_contract)
    assert record["adopted"] is False

    contracts = rc.list_contracts(agent_id=agent_id)
    assert len(contracts) == 2
    assert {c["terminal_id"] for c in contracts} == {"a1b2c3d4", "d4e5f607"}
    assert {c["agent_id"] for c in contracts} == {agent_id}
    assert {c["lineage_id"] for c in contracts} == {first_bind["lineage"]["lineage_id"]}
    assert {c["native_session_id"] for c in contracts} == {_NATIVE_ID}
    assert contracts[0]["contract_digest"] == first_contract.digest()


def test_canonical_payload_digest_is_deterministic(isolated_memory_db):
    """Canonical serialization is deterministic across SEPARATELY constructed
    equivalent contracts: dict key order and fact construction order do not
    change the digest, which is a 64-hex sha256 over the canonical JSON."""
    c1 = _detached_contract(
        profile_material=_fact(
            {
                "profile_config_path": _PROFILE_CONFIG,
                "profile_config_sha256": "b" * 64,
            }
        ),
        route_provenance={"provider_route": "anthropic"},
    )
    c2 = _detached_contract(
        profile_material=_fact(
            {
                "profile_config_sha256": "b" * 64,
                "profile_config_path": _PROFILE_CONFIG,
            }
        ),
        route_provenance={"provider_route": "anthropic"},
    )
    assert c1 is not c2
    assert c1.digest() == c2.digest()
    assert rc.contract_digest(c1) == rc.contract_digest(c2)
    payload = rc.canonical_payload(c1)
    assert len(c1.digest()) == 64
    assert all(ch in "0123456789abcdef" for ch in c1.digest())
    assert digest_bytes(payload) == c1.digest()


def digest_bytes(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def test_schema_version_is_part_of_the_immutable_identity(isolated_memory_db):
    """The schema version is part of the deterministic payload/digest, and an
    unknown future version is refused at the substrate boundary rather than
    being silently coerced to this binary's vocabulary."""
    contract = _detached_contract()
    assert contract.schema_version in rc.canonical_payload(contract)
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(schema_version="cao-m3-restore-contract-v9")


# ---------------------------------------------------------------------------
# authoritative M3-A identity binding
# ---------------------------------------------------------------------------


def test_publish_refuses_detached_contract_without_roster_source(isolated_memory_db):
    """A contract with no roster incarnation behind its source identity cannot
    consume the immutable slot: publication resolves the exact roster source
    and refuses a detached/arbitrary contract."""
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_detached_contract())
    assert rc.list_contracts() == []


def test_publish_refuses_wrong_agent(isolated_memory_db):
    """Same terminal/generation/native id but a different stable agent refuses
    with zero contract slot consumed and zero roster mutation."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, agent_id=str(uuid.uuid4())))
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_refuses_wrong_lineage(isolated_memory_db):
    """A contract naming a lineage the source incarnation is not bound to is
    refused before the slot is consumed."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, lineage_id=str(uuid.uuid4())))
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_refuses_wrong_harness(isolated_memory_db):
    """A contract naming a harness the source lineage does not belong to is
    refused — native ids never cross harness domains."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, harness="muse_cli"))
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_refuses_wrong_route_provenance(isolated_memory_db):
    """A contract whose bounded route provenance disagrees with the lineage's
    recorded provenance is refused."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, route_provenance={"provider_route": "glm"}))
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_refuses_wrong_execution_mode(isolated_memory_db):
    """A contract whose execution mode disagrees with the source incarnation is
    refused — modes are separate launch branches and never cross."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(_contract_for(bind, execution_mode="acp"))
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_refuses_wrong_native_session_id(isolated_memory_db):
    """A contract that misclaims the lineage's native session id is refused at
    publication — the provider-native lineage is immutable."""
    bind, contract = _bound_worker()
    with pytest.raises(rc.RestoreContractConflict):
        rc.publish_contract(
            _contract_for(bind, native_session_id="99999999-9999-4999-8999-999999999999")
        )
    assert rc.list_contracts() == []
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_publish_matches_identity_missing_none(isolated_memory_db):
    """A truthful ``identity_missing`` lineage (native session id None) matches
    a contract recording None, without fabricating an id, and can transition
    dormant."""
    bind = _bind_worker(native_session_id=None, acquisition_method=None)
    contract = _contract_for(bind)
    assert contract.native_session_id is None
    record = rc.publish_contract(contract)
    assert record["adopted"] is False
    assert record["native_session_id"] is None
    result = _transition(bind, contract)
    assert result["adopted"] is False
    assert result["agent"]["disposition"] == roster.DISPOSITION_DORMANT
    assert result["incarnation"]["disposition"] == roster.INCARNATION_RETIRED


def test_empty_route_provenance_binds_and_publishes(isolated_memory_db):
    """An empty route-provenance launch ({} accepted by both APIs) normalizes to
    None consistently and can bind plus publish a matching restore contract —
    empty provenance and absent provenance are the same semantic fact."""
    bind = _bind_worker(route_provenance={})
    # The roster stores the empty provenance as None (its canonical form).
    assert bind["lineage"]["route_provenance"] is None
    contract = _contract_for(bind, route_provenance={})
    # The restore contract normalizes the empty map to None too, so the
    # identity comparison sees two absences, not "{}" vs NULL.
    assert contract.route_provenance is None
    record = rc.publish_contract(contract)
    assert record["adopted"] is False
    result = _transition(bind, contract)
    assert result["adopted"] is False
    assert result["agent"]["disposition"] == roster.DISPOSITION_DORMANT


def test_structured_fact_values_are_immutable(isolated_memory_db):
    """A structured fact map is frozen at construction: a direct mutation
    through the contract is refused (read-only snapshot), so validated state
    cannot be changed after construction."""
    bind, contract = _bound_worker()
    with pytest.raises(TypeError):
        contract.profile_material.value["profile_config_sha256"] = "c" * 64
    with pytest.raises(TypeError):
        contract.executable.value["path"] = "/usr/local/bin/other"
    with pytest.raises(TypeError):
        contract.route_provenance["provider_route"] = "glm"
    # The contract still serializes exactly its validated state.
    assert contract.profile_material.value["profile_config_sha256"] == "b" * 64
    assert contract.executable.value["path"] == "/usr/local/bin/claude"


def test_mutating_caller_input_dict_does_not_change_contract(isolated_memory_db):
    """Mutating the caller's ORIGINAL input dict after construction cannot change
    the contract or its deterministic digest — the validated facts are fresh
    snapshots, never aliases of the caller's input."""
    profile = {
        "profile_config_path": _PROFILE_CONFIG,
        "profile_config_sha256": "b" * 64,
    }
    contract = _detached_contract(profile_material=_fact(profile))
    digest_before = contract.digest()
    profile["profile_config_sha256"] = "c" * 64
    assert contract.profile_material.value["profile_config_sha256"] == "b" * 64
    assert contract.digest() == digest_before


def test_mutated_fact_map_cannot_be_published(isolated_memory_db):
    """A post-construction mutation of a validated fact map is impossible (the
    map is frozen), so no unvalidated state can ever reach publication — the
    contract publishes exactly its validated state."""
    bind, contract = _bound_worker()
    with pytest.raises(TypeError):
        contract.profile_material.value["settings_content"] = "apiKey=super-secret"
    record = rc.publish_contract(contract)
    assert record["adopted"] is False
    stored = rc.get_contract_by_incarnation(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert "apiKey" not in stored["contract_json"]


def test_transition_refuses_corrupt_stored_contract(isolated_memory_db):
    """A legacy/corrupt stored contract whose stored payload no longer matches
    the authoritative roster identity never retires the source: the transition
    revalidates the STORED contract before any mutation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db,
        bind,
        contract,
        lambda parsed: parsed.__setitem__("agent_id", str(uuid.uuid4())),
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE
    assert (
        roster.get_incarnation_by_terminal(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["disposition"]
        == roster.INCARNATION_BOUND
    )


def test_transition_refuses_invalid_json_stored_payload(isolated_memory_db):
    """An invalid-JSON stored payload (partial write) cannot authorize the
    transition: refused with zero roster mutation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_json = "not-json{{"
    new_digest = hashlib.sha256(new_json.encode("utf-8")).hexdigest()
    _corrupt_contract_json(isolated_memory_db, contract.terminal_id, contract.generation, new_json)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE
    assert (
        roster.get_incarnation_by_terminal(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["disposition"]
        == roster.INCARNATION_BOUND
    )


def test_transition_refuses_non_dict_stored_payload(isolated_memory_db):
    """A stored payload that is valid JSON but not a mapping (partial write or
    schema drift) cannot authorize the transition: refused, never a crash."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_json = json.dumps([1, 2, 3])
    new_digest = hashlib.sha256(new_json.encode("utf-8")).hexdigest()
    _corrupt_contract_json(isolated_memory_db, contract.terminal_id, contract.generation, new_json)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_missing_required_identity_key(isolated_memory_db):
    """A stored payload missing a required identity key is refused with a typed
    StableAgentConflict — never a raw KeyError."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db, bind, contract, lambda parsed: parsed.pop("agent_id", None)
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_unknown_schema_version_but_read_stays_lenient(isolated_memory_db):
    """An unknown stored schema_version stays readable through the read API but
    cannot authorize this binary's dormant transition."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db,
        bind,
        contract,
        lambda parsed: parsed.__setitem__("schema_version", "cao-m3-restore-contract-v9"),
    )
    # The read API stays lenient — the unknown-schema record is still readable.
    read = rc.get_contract_by_incarnation(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert read is not None
    assert read["contract"]["schema_version"] == "cao-m3-restore-contract-v9"
    # But it cannot authorize this binary's dormant transition.
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_missing_relaunch_fact(isolated_memory_db):
    """Deleting a required non-identity relaunch fact (working_directory) from
    the stored record leaves an incomplete contract that must NOT authorize the
    dormant transition — zero roster mutation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db, bind, contract, lambda parsed: parsed.pop("working_directory", None)
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE
    assert (
        roster.get_incarnation_by_terminal(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["disposition"]
        == roster.INCARNATION_BOUND
    )


def test_transition_refuses_missing_model_fact(isolated_memory_db):
    """Deleting the model fact (a ContractFact, not a plain identity key) from
    the stored record is refused at the transition boundary."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db, bind, contract, lambda parsed: parsed.pop("model", None)
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_malformed_relaunch_fact(isolated_memory_db):
    """A malformed relaunch fact (a non-dict executable) that the constructor
    would refuse is refused at the transition boundary — the stored record must
    fully decode into a valid typed contract."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db,
        bind,
        contract,
        lambda parsed: parsed.__setitem__(
            "executable", {"state": "present", "value": "not-a-mapping", "reason": None}
        ),
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_stored_source_identity_mismatch(isolated_memory_db):
    """A stored record that claims a different terminal/generation than the exact
    authoritative source incarnation is refused — the transition binds to the
    exact source identity, never to a detached record."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    new_digest = _rewrite_stored_payload(
        isolated_memory_db,
        bind,
        contract,
        lambda parsed: parsed.__setitem__("terminal_id", "ffffffff"),
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest=new_digest)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_stored_digest_divergence(isolated_memory_db):
    """A stored payload whose bytes no longer hash to the stored digest column
    (content/digest divergence) is refused — the stored record must prove its
    own canonical digest before authorizing the transition."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    parsed = json.loads(
        _read_stored_payload(isolated_memory_db, contract.terminal_id, contract.generation)
    )
    parsed["working_directory"] = _ALT_WORKTREE
    _corrupt_contract_json_keep_digest(
        isolated_memory_db,
        contract.terminal_id,
        contract.generation,
        json.dumps(parsed, sort_keys=True, separators=(",", ":")),
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_row_column_identity_mismatch(isolated_memory_db):
    """An accidental column-only edit (the row's agent_id column changed while the
    JSON/digest are intact) produces contradictory reads and must not authorize
    the transition — zero roster mutation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    _corrupt_column(
        isolated_memory_db,
        contract.terminal_id,
        contract.generation,
        "agent_id",
        str(uuid.uuid4()),
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE
    assert (
        roster.get_incarnation_by_terminal(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["disposition"]
        == roster.INCARNATION_BOUND
    )


def test_transition_refuses_noncanonical_json_with_matching_digest(isolated_memory_db):
    """A semantically valid but NON-canonical stored JSON (unsorted keys) that
    hashes to its own stored digest is still refused — the stored bytes must
    equal the decoded contract's canonical serialization, not merely hash to a
    digest."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    parsed = json.loads(
        _read_stored_payload(isolated_memory_db, contract.terminal_id, contract.generation)
    )
    # Re-serialize with the keys in REVERSE order: same parsed payload (JSON
    # objects are unordered), non-canonical bytes.
    reordered = {key: parsed[key] for key in reversed(list(parsed))}
    noncanonical = json.dumps(reordered, sort_keys=False, separators=(",", ":"))
    canonical = json.dumps(parsed, sort_keys=True, separators=(",", ":"))
    assert noncanonical != canonical
    _corrupt_contract_json(
        isolated_memory_db,
        contract.terminal_id,
        contract.generation,
        noncanonical,
    )
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract)
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


# ---------------------------------------------------------------------------
# canonical realpaths at the immutable boundary
# ---------------------------------------------------------------------------


def test_contract_refuses_dot_and_parent_path_aliases(isolated_memory_db):
    """Noncanonical absolute paths ('./' and '..' aliases) are refused rather
    than silently normalized — the exact canonical path is the immutable fact."""
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(working_directory="/cao-test-fixture/./worktree")
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(working_directory="/cao-test-fixture/worktree/..")
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(trusted_project_root="/cao-test-fixture/./worktree")
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(
            executable=_fact({"path": "/usr/local/./bin/claude", "sha256": _DIGEST64})
        )
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(
            profile_material=_fact(
                {
                    "profile_config_path": "/cao-test-fixture/profile/./settings.json",
                    "profile_config_sha256": "b" * 64,
                }
            )
        )


def test_contract_refuses_symlink_path_alias(isolated_memory_db, tmp_path):
    """A symlink alias to a canonical directory is refused; the canonical real
    path is accepted."""
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real_dir, target_is_directory=True)
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(working_directory=str(link))
    canonical = os.path.realpath(str(real_dir))
    _detached_contract(working_directory=canonical)  # accepted


def test_canonical_real_path_accepted(isolated_memory_db):
    """A canonical real path is the positive case and is accepted."""
    contract = _detached_contract(
        working_directory=_WORKTREE,
        trusted_project_root=_WORKTREE,
    )
    assert contract.working_directory == _WORKTREE


def test_executable_refuses_unknown_keys(isolated_memory_db):
    """The executable fact rejects unknown keys rather than silently dropping
    them — an exact executable identity carries exactly path/sha256(/version)."""
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(
            executable=_fact({"path": "/usr/local/bin/claude", "sha256": _DIGEST64, "env": "extra"})
        )


def test_strict_restore_contract_validators(isolated_memory_db):
    """The strict RestoreContract validators reject obvious violations: a
    non-UUID agent id, an unknown execution mode, an empty model, and a
    non-digest executable sha256."""
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(agent_id="not-a-uuid")
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(execution_mode="unknown_mode")
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(model=_fact(""))
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(
            executable=_fact({"path": "/usr/local/bin/claude", "sha256": "not-a-64-hex-digest"})
        )


# ---------------------------------------------------------------------------
# no secrets / references-and-digests-only
# ---------------------------------------------------------------------------


def test_persisted_contract_stores_references_and_digests_only(isolated_memory_db):
    """The PERSISTED raw contract_json carries only references (paths) and
    digests, never profile-file contents.  The key-shape and digest-slot rules
    are cooperative schema discipline — they refuse obviously mislabeled
    values, not a generic secret scanner."""
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(
            profile_material=_fact({"settings_content": "apiKey=super-secret-value"})
        )
    with pytest.raises(rc.RestoreContractInvalid):
        _detached_contract(profile_material=_fact({"settings_sha256": "not-a-64-hex-digest"}))

    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    from sqlalchemy import text as sa_text

    with isolated_memory_db.begin() as conn:
        row = conn.execute(
            sa_text(
                "SELECT contract_json FROM restore_contracts "
                "WHERE terminal_id=:t AND generation=:g"
            ),
            {"t": contract.terminal_id, "g": contract.generation},
        ).fetchone()
    raw = row[0]
    assert "apiKey" not in raw
    assert "super-secret" not in raw
    parsed = json.loads(raw)
    assert parsed["profile_material"]["state"] == "present"
    assert parsed["profile_material"]["value"]["profile_config_path"] == _PROFILE_CONFIG
    assert parsed["profile_material"]["value"]["profile_config_sha256"] == "b" * 64


def test_typed_unavailable_facts_are_truthful_and_replay_stable(isolated_memory_db):
    """A launch path that cannot truthfully supply a fact records a typed
    unavailable/missing state — never a fabricated value — and that state is
    part of the deterministic identity."""
    bind, _ = _bound_worker()
    contract = _contract_for(
        bind,
        executable=rc.ContractFact.unavailable("legacy launch supplies no binary digest"),
        model=rc.ContractFact.missing(),
    )
    record = rc.publish_contract(contract)
    stored = record["contract"]
    assert stored["executable"]["state"] == "unavailable"
    assert stored["executable"]["reason"].startswith("legacy")
    assert stored["model"]["state"] == "missing"
    assert rc.publish_contract(contract)["adopted"] is True


# ---------------------------------------------------------------------------
# legacy rows: no contract reads truthfully
# ---------------------------------------------------------------------------


def test_legacy_row_with_no_contract_reads_truthfully(isolated_memory_db):
    """An M3-A roster row created before the restore-contract substrate reads
    back with no contract, and the roster itself stays fully readable."""
    bind = _bind_worker()
    assert (
        rc.get_contract_by_incarnation(
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
        )
        is None
    )
    assert rc.list_contracts(agent_id=bind["agent"]["agent_id"]) == []
    assert len(roster.list_agents()) == 1
    assert len(roster.list_incarnations()) == 1


def test_read_apis_round_trip_and_scope(isolated_memory_db):
    """The small read surface: by id, by exact source incarnation, and scoped
    list by agent and lineage."""
    bind, contract = _bound_worker()
    record = rc.publish_contract(contract)
    by_id = rc.get_contract(record["contract_id"])
    assert by_id["contract_id"] == record["contract_id"]
    assert by_id["contract_digest"] == contract.digest()
    by_incarnation = rc.get_contract_by_incarnation(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert by_incarnation["contract_id"] == record["contract_id"]
    by_agent = rc.list_contracts(agent_id=contract.agent_id)
    assert len(by_agent) == 1 and by_agent[0]["contract_id"] == record["contract_id"]
    by_lineage = rc.list_contracts(lineage_id=contract.lineage_id)
    assert len(by_lineage) == 1
    # A successor contract on the same agent scopes the agent list to two.
    roster.retire_incarnation(
        terminal_id=contract.terminal_id,
        generation=contract.generation,
        reason="pane lost",
    )
    second_bind = _bind_worker(
        agent_id=contract.agent_id,
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000003",
    )
    rc.publish_contract(_contract_for(second_bind))
    assert len(rc.list_contracts(agent_id=contract.agent_id)) == 2
    assert len(rc.list_contracts(lineage_id=contract.lineage_id)) == 2
    with pytest.raises(rc.RestoreContractNotFound):
        rc.get_contract("no-such-contract")


# ---------------------------------------------------------------------------
# atomic dormant transition
# ---------------------------------------------------------------------------


def test_transition_dormant_retires_exact_source_and_marks_dormant(isolated_memory_db):
    """The narrow transition atomically retires the exact live source
    incarnation and marks its stable agent dormant, preserving the immutable
    restore contract."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    result = _transition(bind, contract, reason="pane lost")
    assert result["adopted"] is False
    assert result["incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    assert result["incarnation"]["retirement_reason"] == "pane lost"
    assert result["agent"]["disposition"] == roster.DISPOSITION_DORMANT
    assert result["contract"]["contract_digest"] == contract.digest()
    assert (
        rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["contract_digest"]
        == contract.digest()
    )
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED


def test_legacy_generationless_source_incarnation_transitions(isolated_memory_db):
    """A legacy generation-less incarnation (unmanaged launch) publishes a
    contract with generation=None and transitions dormant on the same exact
    source identity — the NULL-generation source-incarnation key."""
    agent_id = str(uuid.uuid4())
    bind = _bind_worker(agent_id=agent_id, terminal_id="a1b2c3d4", generation=None)
    contract = _contract_for(bind)
    assert contract.generation is None
    record = rc.publish_contract(contract)
    assert record["adopted"] is False
    result = _transition(bind, contract)
    assert result["adopted"] is False
    assert result["agent"]["disposition"] == roster.DISPOSITION_DORMANT
    assert result["incarnation"]["disposition"] == roster.INCARNATION_RETIRED


def test_transition_exact_replay_converges(isolated_memory_db):
    """Replaying the identical dormant transition converges (adopts) instead of
    failing: the same request returns the same durable state, with no extra
    revision bump."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    first = _transition(bind, contract)
    revision_after_first = first["agent"]["revision"]
    second = _transition(bind, contract)
    assert second["adopted"] is True
    assert second["agent"]["disposition"] == roster.DISPOSITION_DORMANT
    assert second["incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    assert second["agent"]["revision"] == revision_after_first


def test_transition_refuses_stale_generation(isolated_memory_db):
    """A stale/absent generation for a terminal is refused with zero mutation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, generation="00000000-0000-4000-8000-00000000ffff")
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_wrong_agent(isolated_memory_db):
    """A transition that names a different stable agent is refused."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, agent_id=str(uuid.uuid4()))
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_wrong_lineage(isolated_memory_db):
    """A transition that names a different native lineage is refused."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, lineage_id=str(uuid.uuid4()))
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_mismatched_contract_digest(isolated_memory_db):
    """A transition that does not name the exact contract digest is refused
    with zero mutation (changed content conflicts)."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract, contract_digest="f" * 64)
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_when_no_contract_recorded(isolated_memory_db):
    """A live incarnation with no restore contract cannot transition dormant:
    the contract is the durable basis of a later exact resume."""
    bind, _ = _bound_worker()
    with pytest.raises(roster.StableAgentConflict):
        roster.transition_dormant(
            terminal_id=bind["incarnation"]["terminal_id"],
            generation=bind["incarnation"]["generation"],
            agent_id=bind["agent"]["agent_id"],
            lineage_id=bind["lineage"]["lineage_id"],
            contract_digest="f" * 64,
            reason="pane lost",
        )
    assert roster.get_agent(bind["agent"]["agent_id"])["disposition"] == roster.DISPOSITION_LIVE


def test_transition_refuses_already_live_successor(isolated_memory_db):
    """Once a successor incarnation is live, retiring the prior (already
    retired) source incarnation again is refused — one live incarnation per
    stable agent, and the dormant transition never crosses into it."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    _transition(bind, contract)
    # The stable agent reincarnates on a fresh physical incarnation.
    _bind_worker(
        agent_id=contract.agent_id,
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000003",
    )
    assert roster.get_agent(contract.agent_id)["disposition"] == roster.DISPOSITION_LIVE
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract)
    live = [
        i
        for i in roster.list_incarnations(agent_id=contract.agent_id)
        if i["disposition"] in roster.LIVE_INCARNATION_DISPOSITIONS
    ]
    assert len(live) == 1


def test_transition_refuses_historical_replay_of_retired_successor(isolated_memory_db):
    """Replaying a prior incarnation's transition after the agent moved to a
    successor (and that successor was itself retired, leaving the whole agent
    dormant) is refused as historical/stale — exact replay requires the source
    incarnation is still the agent's current incarnation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    _transition(bind, contract)
    successor_bind = _bind_worker(
        agent_id=contract.agent_id,
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000003",
    )
    rc.publish_contract(_contract_for(successor_bind))
    _transition(successor_bind, _contract_for(successor_bind))
    # The whole agent is dormant again, but its current incarnation is the
    # successor — the prior source incarnation is historical.
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation_id"] == successor_bind["incarnation"]["incarnation_id"]
    with pytest.raises(roster.StableAgentConflict):
        _transition(bind, contract)


def test_transition_cannot_create_two_live_incarnations(isolated_memory_db):
    """A B1 transition can never leave two live incarnations: it retires the
    source in the same transaction, and a later bind stays a single live
    incarnation."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    _transition(bind, contract)
    _bind_worker(
        agent_id=contract.agent_id,
        terminal_id="d4e5f607",
        generation="00000000-0000-4000-8000-000000000003",
    )
    live = [
        i
        for i in roster.list_incarnations(agent_id=contract.agent_id)
        if i["disposition"] in roster.LIVE_INCARNATION_DISPOSITIONS
    ]
    assert len(live) == 1
    with pytest.raises(roster.StableAgentConflict):
        _bind_worker(
            agent_id=contract.agent_id,
            terminal_id="e5f60718",
            generation="00000000-0000-4000-8000-000000000004",
        )


def test_transition_does_not_change_provider_native_lineage(isolated_memory_db):
    """The dormant transition retires the disposable incarnation and never
    alters the provider-native lineage: same lineage id, same native session
    id, same acquisition method, same harness."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    lineage_before = roster.list_lineages(agent_id=contract.agent_id)[0]
    _transition(bind, contract)
    lineage_after = roster.list_lineages(agent_id=contract.agent_id)[0]
    assert lineage_after["lineage_id"] == lineage_before["lineage_id"]
    assert lineage_after["native_session_id"] == lineage_before["native_session_id"]
    assert lineage_after["acquisition_method"] == lineage_before["acquisition_method"]
    assert lineage_after["harness"] == lineage_before["harness"]
    contract_read = rc.get_contract_by_incarnation(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert contract_read["native_session_id"] == contract.native_session_id
    assert contract_read["lineage_id"] == contract.lineage_id


def test_transition_rolls_back_with_outer_transaction(isolated_memory_db):
    """The transition participates in the caller's transaction: rolling back an
    outer transaction leaves the roster unchanged."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    session = database.SessionLocal()
    try:
        session.begin()
        result = roster.transition_dormant(
            terminal_id=contract.terminal_id,
            generation=contract.generation,
            agent_id=contract.agent_id,
            lineage_id=contract.lineage_id,
            contract_digest=contract.digest(),
            reason="pane lost",
            db=session,
        )
        assert result["adopted"] is False
        session.rollback()
    finally:
        session.close()
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_LIVE
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_BOUND
    assert (
        rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
        )
        is not None
    )


def test_publish_participates_in_outer_rollback(isolated_memory_db):
    """publish_contract(db=session) writes in the caller's transaction: rolling
    back the caller's outer transaction leaves NO contract."""
    bind, contract = _bound_worker()
    session = database.SessionLocal()
    try:
        session.begin()
        record = rc.publish_contract(contract, db=session)
        assert record["adopted"] is False
        session.rollback()
    finally:
        session.close()
    assert (
        rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
        )
        is None
    )
    assert rc.list_contracts() == []


def test_publish_and_transition_atomic_rollback(isolated_memory_db):
    """publish(db=session) + transition_dormant(db=session) in one outer
    transaction roll back together: no contract, the source stays live/bound,
    and the agent revision is unchanged."""
    bind, contract = _bound_worker()
    revision_before = bind["agent"]["revision"]
    session = database.SessionLocal()
    try:
        session.begin()
        rc.publish_contract(contract, db=session)
        result = roster.transition_dormant(
            terminal_id=contract.terminal_id,
            generation=contract.generation,
            agent_id=contract.agent_id,
            lineage_id=contract.lineage_id,
            contract_digest=contract.digest(),
            reason="pane lost",
            db=session,
        )
        assert result["adopted"] is False
        session.rollback()
    finally:
        session.close()
    assert (
        rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
        )
        is None
    )
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_LIVE
    assert agent["revision"] == revision_before
    assert (
        roster.get_incarnation_by_terminal(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["disposition"]
        == roster.INCARNATION_BOUND
    )


def test_publish_and_transition_atomic_commit(isolated_memory_db):
    """publish(db=session) + transition_dormant(db=session) commit together: one
    contract and the retired/dormant state appear atomically."""
    bind, contract = _bound_worker()
    session = database.SessionLocal()
    try:
        session.begin()
        rc.publish_contract(contract, db=session)
        result = roster.transition_dormant(
            terminal_id=contract.terminal_id,
            generation=contract.generation,
            agent_id=contract.agent_id,
            lineage_id=contract.lineage_id,
            contract_digest=contract.digest(),
            reason="pane lost",
            db=session,
        )
        assert result["adopted"] is False
        session.commit()
    finally:
        session.close()
    assert (
        rc.get_contract_by_incarnation(
            terminal_id=contract.terminal_id, generation=contract.generation
        )["contract_digest"]
        == contract.digest()
    )
    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED


# ---------------------------------------------------------------------------
# concurrent dormant transitions
# ---------------------------------------------------------------------------


def test_concurrent_transitions_one_mutation_one_adoption(file_db, monkeypatch):
    """Two threads transition the same live source through a real SQLite file:
    exactly one performs the live->retired/dormant mutation, the other adopts
    the committed state, the first retirement reason is stable, the agent
    revision increments exactly once, and no raw OperationalError escapes."""
    bind, contract = _bound_worker()
    rc.publish_contract(contract)
    revision_before = roster.get_agent(contract.agent_id)["revision"]

    barrier = threading.Barrier(2)
    monkeypatch.setattr(
        rc, "identity_mismatch_reason", _gate_first_call(rc.identity_mismatch_reason, barrier)
    )

    results: list[dict] = []
    errors: list[BaseException] = []

    def run(reason: str) -> None:
        try:
            results.append(
                roster.transition_dormant(
                    terminal_id=contract.terminal_id,
                    generation=contract.generation,
                    agent_id=contract.agent_id,
                    lineage_id=contract.lineage_id,
                    contract_digest=contract.digest(),
                    reason=reason,
                )
            )
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=("pane lost A",)),
        threading.Thread(target=run, args=("pane lost B",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"unexpected transition errors: {errors}"
    assert len(results) == 2
    adopted_false = [r for r in results if not r["adopted"]]
    adopted_true = [r for r in results if r["adopted"]]
    assert len(adopted_false) == 1, f"expected exactly one mutation, got {results}"
    assert len(adopted_true) == 1, f"expected exactly one adoption, got {results}"
    winner = adopted_false[0]

    agent = roster.get_agent(contract.agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["revision"] == revision_before + 1
    stored = roster.get_incarnation_by_terminal(
        terminal_id=contract.terminal_id, generation=contract.generation
    )
    assert stored["disposition"] == roster.INCARNATION_RETIRED
    assert stored["retirement_reason"] == winner["incarnation"]["retirement_reason"]
    assert stored["retirement_reason"] in {"pane lost A", "pane lost B"}


# ---------------------------------------------------------------------------
# concurrent publishers
# ---------------------------------------------------------------------------


def test_concurrent_identical_publishers_converge(file_db, monkeypatch):
    """Two threads publishing the identical contract against one SQLite file
    converge to exactly one row and one adoption."""
    bind, contract = _bound_worker()
    barrier = threading.Barrier(2)
    monkeypatch.setattr(rc, "_now", _gate_first_call(rc._now, barrier))

    results: list[dict] = []
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            results.append(rc.publish_contract(contract))
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"unexpected publish errors: {errors}"
    assert len(results) == 2
    assert len([r for r in results if not r["adopted"]]) == 1
    assert len([r for r in results if r["adopted"]]) == 1
    assert len(rc.list_contracts()) == 1


def test_concurrent_differing_publishers_one_winner_typed_conflict(file_db, monkeypatch):
    """Two threads publishing different content for the same source incarnation
    yield exactly one winner plus a typed RestoreContractConflict — never
    RestoreContractUnavailable."""
    bind, contract = _bound_worker()
    alt = _contract_for(bind, working_directory=_ALT_WORKTREE)
    assert alt.digest() != contract.digest()
    barrier = threading.Barrier(2)
    monkeypatch.setattr(rc, "_now", _gate_first_call(rc._now, barrier))

    results: list[dict] = []
    conflicts: list[BaseException] = []
    others: list[BaseException] = []

    def publish(which: rc.RestoreContract) -> None:
        try:
            results.append(rc.publish_contract(which))
        except rc.RestoreContractConflict as exc:
            conflicts.append(exc)
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            others.append(exc)

    threads = [
        threading.Thread(target=publish, args=(contract,)),
        threading.Thread(target=publish, args=(alt,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert others == [], f"unexpected non-Conflict publish errors: {others}"
    assert len(results) == 1, f"expected exactly one winner, got {results}"
    assert len(conflicts) == 1, f"expected exactly one typed conflict, got {len(conflicts)}"
    assert len(rc.list_contracts()) == 1


def test_caller_owned_two_session_contention_is_typed_and_recoverable(file_db, monkeypatch):
    """Two caller-owned sessions racing the same source slot: exactly one wins;
    the OTHER receives exactly one typed RestoreContractUnavailable (never a raw
    SQLite IntegrityError/OperationalError), rolls back its now-unusable
    transaction, and retries the whole caller-owned call to adopt the winner's
    row.  The barrier at the pre-flush seam makes the typed mapping
    deterministic."""
    bind, contract = _bound_worker()
    barrier = threading.Barrier(2)
    monkeypatch.setattr(rc, "_now", _gate_first_call(rc._now, barrier))

    outcomes: list[dict] = []
    refusals: list[str] = []
    errors: list[BaseException] = []

    def run() -> None:
        session = database.SessionLocal()
        try:
            session.begin()
            try:
                outcomes.append(rc.publish_contract(contract, db=session))
                session.commit()
            except rc.RestoreContractUnavailable as exc:
                # Record the typed refusal, then recover: the loser's caller-
                # owned transaction is unusable after the unique-slot collision,
                # so roll it back and retry the whole call.
                refusals.append(str(exc))
                session.rollback()
                session.begin()
                outcomes.append(rc.publish_contract(contract, db=session))
                session.commit()
        except BaseException as exc:  # noqa: BLE001 - surface for assertions
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=run) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert errors == [], f"unexpected caller-owned publish errors: {errors}"
    # The barrier forces both callers to race the empty slot, so exactly one
    # caller observes the typed refusal and the other publishes without one.
    assert len(refusals) == 1, f"expected exactly one typed refusal, got {len(refusals)}"
    assert len(outcomes) == 2
    assert len([o for o in outcomes if not o["adopted"]]) == 1
    assert len([o for o in outcomes if o["adopted"]]) == 1
    assert len(rc.list_contracts()) == 1


def test_null_generation_contract_publishes_and_adopts(isolated_memory_db):
    """The NULL-generation source-incarnation key: a legacy contract with
    generation=None publishes once and identical replay adopts."""
    bind = _bind_worker(terminal_id="a1b2c3d4", generation=None)
    contract = _contract_for(bind)
    assert contract.generation is None
    first = rc.publish_contract(contract)
    assert first["adopted"] is False
    second = rc.publish_contract(contract)
    assert second["adopted"] is True
    assert len(rc.list_contracts()) == 1
