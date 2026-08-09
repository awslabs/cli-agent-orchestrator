"""M3-A / cond-0377: stable CAO-agent roster store semantics.

The roster is the fork-owned durable record that lets a CAO session
reclaim disposable panes without erasing the coding agents' identity:

    CAO session -> stable CAO agent (role/profile family)
        -> harness-native conversation lineage (append-only)
            -> disposable incarnation (terminal/generation/pane/process)

``agent_id`` is an explicit immutable identity minted from the durable
initial physical launch identity — never inferred from role/profile, so
many workers of one profile in a session stay distinct.  Role, profile
family, and session are attributes that must MATCH on replay, not a
uniqueness key.

These tests are the deterministic store contract, written before the
service changes that satisfy them.  No provider, tmux, or network I/O is
touched: every assertion runs against the ORM store via
``isolated_memory_db`` and the module's own clock.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone

import pytest

from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services.stable_agent_roster import BindingContract

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _rfc3339(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _contract(agent_id: str | None = None, **changes) -> BindingContract:
    payload = {
        "agent_id": agent_id or str(uuid.uuid4()),
        "session_name": "cao-campaign-a",
        "role": roster.ROLE_WORKER,
        "profile_family": "developer",
        "harness": "claude_code",
        "native_session_id": "11111111-2222-4333-8444-555555555555",
        "acquisition_method": "chosen_session_id",
        "route_provenance": {"provider_route": "anthropic"},
        "terminal_id": "a1b2c3d4",
        "generation": "00000000-0000-4000-8000-000000000001",
        "pane_id": "%101",
        "pane_pid": 4242,
        "process_identity": {"pid": 4242, "start_marker": "2026-08-09T00:00:00Z"},
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return BindingContract(**payload)


def _worker_contract(agent_id: str | None = None, **changes) -> BindingContract:
    return _contract(agent_id, **changes)


def _supervisor_contract(agent_id: str | None = None, **changes) -> BindingContract:
    payload = {
        "agent_id": agent_id or str(uuid.uuid4()),
        "session_name": "cao-campaign-a",
        "role": roster.ROLE_SUPERVISOR,
        "profile_family": "code_supervisor",
        "harness": "claude_code",
        "native_session_id": None,
        "acquisition_method": None,
        "terminal_id": "b2c3d4e5",
        "generation": "00000000-0000-4000-8000-000000000002",
        "pane_id": "%102",
        "pane_pid": 4243,
        "process_identity": {"pid": 4243, "start_marker": "2026-08-09T00:00:01Z"},
        "execution_mode": "native_tui",
    }
    payload.update(changes)
    return BindingContract(**payload)


@pytest.fixture(autouse=True)
def _clock(monkeypatch):
    moments = iter(
        [
            datetime(2026, 8, 9, 12, 0, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 15, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 25, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 35, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 40, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 45, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 50, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 0, 55, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 15, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 25, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 30, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 35, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 40, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 45, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 50, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 1, 55, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 0, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 5, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 10, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 15, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 20, tzinfo=timezone.utc),
            datetime(2026, 8, 9, 12, 2, 25, tzinfo=timezone.utc),
        ]
    )

    def _now() -> str:
        return _rfc3339(next(moments))

    monkeypatch.setattr(roster, "_now", _now)
    return _now


# ---------------------------------------------------------------------------
# P1-1: agent_id is explicit and immutable; same-profile workers are distinct
# ---------------------------------------------------------------------------


def test_two_live_workers_same_profile_are_distinct_agents(isolated_memory_db):
    """Two live workers in one session with identical role/profile but
    different terminal/generation/native ids produce distinct stable
    agents — the identity key is the explicit agent_id, never the
    role/profile family."""
    first = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    second = roster.bind_generation(
        _worker_contract(
            terminal_id="c3d4e5f6",
            generation="00000000-0000-4000-8000-000000000002",
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    assert first["agent"]["agent_id"] != second["agent"]["agent_id"]
    assert first["agent"]["role"] == second["agent"]["role"] == roster.ROLE_WORKER
    assert first["agent"]["profile_family"] == second["agent"]["profile_family"] == "developer"
    assert len(roster.list_agents(session_name="cao-campaign-a")) == 2
    assert {a["disposition"] for a in roster.list_agents()} == {roster.DISPOSITION_LIVE}


def test_exact_replay_returns_one_stable_agent(isolated_memory_db):
    """Replay of one physical launch (same agent_id + same contract)
    adopts the same agent, lineage, and incarnation."""
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(_worker_contract(agent_id))
    second = roster.bind_generation(_worker_contract(agent_id))
    assert second["agent"]["agent_id"] == first["agent"]["agent_id"] == agent_id
    assert second["adopted"] is True
    assert second["lineage"]["lineage_id"] == first["lineage"]["lineage_id"]
    assert second["incarnation"]["incarnation_id"] == first["incarnation"]["incarnation_id"]
    assert len(roster.list_agents()) == 1
    assert len(roster.list_lineages(agent_id=agent_id)) == 1
    assert len(roster.list_incarnations(agent_id=agent_id)) == 1


def test_retirement_and_new_incarnation_bind_to_prior_agent_id(isolated_memory_db):
    """Retirement and an explicit new-incarnation bind to the SAME
    agent_id preserve one agent and append incarnation history."""
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    first_lineage_id = first["lineage"]["lineage_id"]
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="pane lost",
    )
    resumed = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-000000000003",
            native_session_id="33333333-4444-4555-8666-777777777777",
        )
    )
    assert resumed["agent"]["agent_id"] == agent_id
    assert len(roster.list_agents()) == 1
    assert resumed["incarnation"]["incarnation_id"] != first["incarnation"]["incarnation_id"]
    assert resumed["lineage"]["predecessor_lineage_id"] == first_lineage_id
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 2
    assert {i["disposition"] for i in incarnations} == {
        roster.INCARNATION_RETIRED,
        roster.INCARNATION_BOUND,
    }
    agent = roster.get_agent(agent_id)
    assert agent["current_incarnation"]["terminal_id"] == "d4e5f607"


def test_changed_immutable_facts_for_existing_agent_id_refused(isolated_memory_db):
    """A supplied existing agent_id with changed session/role/profile
    immutable facts conflicts with zero mutation."""
    agent_id = str(uuid.uuid4())
    roster.bind_generation(_worker_contract(agent_id))
    for changes in (
        {"session_name": "cao-campaign-b"},
        {"role": roster.ROLE_SUPERVISOR},
        {"profile_family": "reviewer"},
    ):
        with pytest.raises(roster.StableAgentConflict):
            roster.bind_generation(
                _worker_contract(
                    agent_id,
                    terminal_id="e5f60718",
                    native_session_id=None,
                    acquisition_method=None,
                    **changes,
                )
            )
    # The original rows are untouched.
    assert len(roster.list_agents()) == 1
    assert roster.get_agent(agent_id)["session_name"] == "cao-campaign-a"
    assert len(roster.list_lineages(agent_id=agent_id)) == 1


def test_supervisor_and_multiple_same_profile_workers_coexist(isolated_memory_db):
    supervisor = roster.bind_generation(_supervisor_contract())
    worker_a = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
        )
    )
    worker_b = roster.bind_generation(
        _worker_contract(
            terminal_id="c3d4e5f6",
            generation="00000000-0000-4000-8000-000000000002",
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    agents = roster.list_agents(session_name="cao-campaign-a")
    assert len(agents) == 3
    assert {a["agent_id"] for a in agents} == {
        supervisor["agent"]["agent_id"],
        worker_a["agent"]["agent_id"],
        worker_b["agent"]["agent_id"],
    }
    assert {a["role"] for a in agents} == {roster.ROLE_SUPERVISOR, roster.ROLE_WORKER}


# ---------------------------------------------------------------------------
# survival of disposable incarnation retirement
# ---------------------------------------------------------------------------


def test_agent_survives_incarnation_retirement(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    bound = roster.bind_generation(_worker_contract(agent_id))
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="pane cleaned up at a safe boundary",
    )
    agent = roster.get_agent(agent_id)
    assert agent["agent_id"] == agent_id
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert [i["incarnation_id"] for i in incarnations] == [bound["incarnation"]["incarnation_id"]]
    assert incarnations[0]["disposition"] == roster.INCARNATION_RETIRED
    assert incarnations[0]["retirement_reason"] == "pane cleaned up at a safe boundary"
    assert len(roster.list_agents(session_name="cao-campaign-a")) == 1


# ---------------------------------------------------------------------------
# deterministic create/adopt, concurrency, conflicts
# ---------------------------------------------------------------------------


def test_exact_replay_idempotency_after_response_loss(isolated_memory_db):
    """A crash after the durable write but before the response must adopt."""
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(_worker_contract(agent_id))
    replay = roster.bind_generation(_worker_contract(agent_id))
    assert replay["agent"] == first["agent"]
    assert replay["lineage"] == first["lineage"]
    assert replay["incarnation"] == first["incarnation"]
    assert replay["adopted"] is True


def test_concurrent_duplicate_bindings_converge(isolated_memory_db):
    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)
    agent_id = str(uuid.uuid4())

    def _bind() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(roster.bind_generation(_worker_contract(agent_id)))
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [threading.Thread(target=_bind) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not errors
    assert len(results) == 2
    agent_ids = {r["agent"]["agent_id"] for r in results}
    lineage_ids = {r["lineage"]["lineage_id"] for r in results}
    incarnation_ids = {r["incarnation"]["incarnation_id"] for r in results}
    assert len(agent_ids) == 1
    assert len(lineage_ids) == 1
    assert len(incarnation_ids) == 1
    assert {r["adopted"] for r in results} == {True, False}


def test_conflicting_immutable_identity_refused(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    roster.bind_generation(_worker_contract(agent_id))
    # Same terminal/generation bound under a different native identity is a
    # different immutable identity, never a silent rewrite.
    with pytest.raises(roster.StableAgentConflict):
        roster.bind_generation(
            _worker_contract(
                agent_id,
                native_session_id="99999999-8888-4777-8666-555555555555",
                acquisition_method="chosen_session_id",
            )
        )
    # The same native id cannot move to a different terminal either.
    with pytest.raises(roster.StableAgentConflict):
        roster.bind_generation(
            _worker_contract(
                agent_id,
                terminal_id="f1e2d3c4",
                generation="00000000-0000-4000-8000-000000000004",
            )
        )
    assert len(roster.list_lineages(agent_id=agent_id)) == 1
    assert len(roster.list_incarnations(agent_id=agent_id)) == 1


# ---------------------------------------------------------------------------
# exclusive native identity, scoped to (harness, native_session_id)
# ---------------------------------------------------------------------------


def test_one_native_id_cannot_live_attach_to_two_agents(isolated_memory_db):
    roster.bind_generation(_worker_contract())
    with pytest.raises(roster.StableAgentConflict):
        roster.bind_generation(
            _worker_contract(
                terminal_id="c3d4e5f6",
                generation="00000000-0000-4000-8000-000000000002",
            )
        )
    assert len(roster.list_agents()) == 1


def test_native_id_uniqueness_is_scoped_to_harness(isolated_memory_db):
    """Two unrelated harnesses may legally emit the same textual id: a
    Claude and a Muse lineage with the same raw string are independent.
    The same (harness, id) can never map to two agents or lineages."""
    raw_id = "11111111-2222-4333-8444-555555555555"
    claude = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
            native_session_id=raw_id,
        )
    )
    muse = roster.bind_generation(
        _worker_contract(
            harness="muse_cli",
            terminal_id="c3d4e5f6",
            generation="00000000-0000-4000-8000-000000000002",
            native_session_id=raw_id,
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
        )
    )
    assert claude["lineage"]["lineage_id"] != muse["lineage"]["lineage_id"]
    assert claude["lineage"]["harness"] == "claude_code"
    assert muse["lineage"]["harness"] == "muse_cli"
    assert len(roster.list_agents()) == 2

    # Same harness + same id in a second agent refuses.
    with pytest.raises(roster.StableAgentConflict):
        roster.bind_generation(
            _worker_contract(
                harness="claude_code",
                terminal_id="d4e5f607",
                generation="00000000-0000-4000-8000-000000000003",
                native_session_id=raw_id,
            )
        )


def test_one_native_id_cannot_live_attach_to_two_incarnations(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    bound = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
        )
    )
    lineage_id = bound["lineage"]["lineage_id"]
    # A second live incarnation for the same lineage (an attempted resume
    # while the first pane is still live) is refused by the roster itself.
    with pytest.raises(roster.StableAgentConflict):
        roster.bind_generation(
            _worker_contract(
                agent_id,
                terminal_id="d4e5f607",
                generation="00000000-0000-4000-8000-000000000005",
                pane_id="%103",
                pane_pid=9999,
                process_identity={"pid": 9999, "start_marker": "2026-08-09T00:00:02Z"},
            )
        )
    # After the first incarnation is retired, a fresh pane may resume the
    # exact same native conversation: same lineage, new incarnation.
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="pane lost",
    )
    resumed = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-000000000005",
            pane_id="%103",
            pane_pid=9999,
            process_identity={"pid": 9999, "start_marker": "2026-08-09T00:00:02Z"},
        )
    )
    assert resumed["lineage"]["lineage_id"] == lineage_id
    assert resumed["incarnation"]["incarnation_id"] != bound["incarnation"]["incarnation_id"]
    agent = roster.get_agent(agent_id)
    assert agent["current_incarnation_id"] == resumed["incarnation"]["incarnation_id"]
    incarnations = roster.list_incarnations(agent_id=agent_id)
    assert len(incarnations) == 2
    assert {i["disposition"] for i in incarnations} == {
        roster.INCARNATION_RETIRED,
        roster.INCARNATION_BOUND,
    }


# ---------------------------------------------------------------------------
# fresh lineage never overwrites predecessor history
# ---------------------------------------------------------------------------


def test_fresh_lineage_links_predecessor_and_never_overwrites(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
        )
    )
    first_lineage_id = first["lineage"]["lineage_id"]
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="pane lost",
    )
    fallback = roster.bind_generation(
        _worker_contract(
            agent_id,
            native_session_id="77777777-6666-4555-8444-333333333333",
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-000000000005",
            pane_id="%103",
            pane_pid=9999,
            process_identity={"pid": 9999, "start_marker": "2026-08-09T00:00:02Z"},
        )
    )
    assert fallback["lineage"]["lineage_id"] != first_lineage_id
    assert fallback["lineage"]["predecessor_lineage_id"] == first_lineage_id
    assert fallback["lineage"]["lineage_origin"] == roster.LINEAGE_ORIGIN_FALLBACK

    agent = roster.get_agent(agent_id)
    assert agent["current_lineage_id"] == fallback["lineage"]["lineage_id"]
    lineages = roster.list_lineages(agent_id=agent_id)
    assert {l["lineage_id"] for l in lineages} == {
        first_lineage_id,
        fallback["lineage"]["lineage_id"],
    }
    by_id = {l["lineage_id"]: l for l in lineages}
    assert by_id[first_lineage_id]["native_session_id"] == "11111111-2222-4333-8444-555555555555"
    assert by_id[first_lineage_id]["lineage_origin"] == roster.LINEAGE_ORIGIN_INITIAL


# ---------------------------------------------------------------------------
# route provenance and harness domain isolation
# ---------------------------------------------------------------------------


def test_route_provenance_preserved_and_same_id_independent_across_harnesses(
    isolated_memory_db,
):
    bound = roster.bind_generation(
        _worker_contract(route_provenance={"provider_route": "deepseek"})
    )
    lineage = roster.list_lineages(agent_id=bound["agent"]["agent_id"])[0]
    assert lineage["route_provenance"] == {"provider_route": "deepseek"}
    assert lineage["harness"] == "claude_code"
    # The same raw id under a DIFFERENT harness is an independent lineage
    # (uniqueness is scoped to (harness, native_session_id)).
    codex = roster.bind_generation(
        _worker_contract(
            harness="codex",
            terminal_id="e5f60718",
            generation="00000000-0000-4000-8000-000000000006",
            acquisition_method=roster.ACQUISITION_ZERO_TURN_BOOTSTRAP,
        )
    )
    assert codex["lineage"]["lineage_id"] != lineage["lineage_id"]
    assert codex["lineage"]["harness"] == "codex"


def test_route_provenance_bounded_and_closed(isolated_memory_db):
    with pytest.raises(roster.StableAgentInvalid):
        roster.bind_generation(
            _worker_contract(
                route_provenance={"provider_route": "anthropic", "extra": "not-allowed"}
            )
        )
    with pytest.raises(roster.StableAgentInvalid):
        roster.bind_generation(_worker_contract(route_provenance={"provider_route": "x" * 600}))


# ---------------------------------------------------------------------------
# durable admitted state gates real task input
# ---------------------------------------------------------------------------


def test_admission_impossible_before_durable_binding_state(isolated_memory_db):
    terminal_id = "a1b2c3d4"
    generation = "00000000-0000-4000-8000-000000000001"
    agent_id = str(uuid.uuid4())
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.assert_admission_ready(terminal_id=terminal_id, generation=generation)

    roster.bind_generation(
        _worker_contract(agent_id, native_session_id=None, acquisition_method=None)
    )
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.assert_admission_ready(terminal_id=terminal_id, generation=generation)

    roster.bind_generation(_worker_contract(agent_id))
    roster.assert_admission_ready(terminal_id=terminal_id, generation=generation)

    roster.retire_incarnation(terminal_id=terminal_id, generation=generation, reason="done")
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.assert_admission_ready(terminal_id=terminal_id, generation=generation)


def test_mark_admitted_is_idempotent_and_keeps_admission_open(isolated_memory_db):
    terminal_id = "a1b2c3d4"
    generation = "00000000-0000-4000-8000-000000000001"
    agent_id = str(uuid.uuid4())
    roster.bind_generation(_worker_contract(agent_id))
    first = roster.mark_admitted(terminal_id=terminal_id, generation=generation)
    assert first["disposition"] == roster.INCARNATION_ADMITTED
    second = roster.mark_admitted(terminal_id=terminal_id, generation=generation)
    assert second["disposition"] == roster.INCARNATION_ADMITTED
    roster.assert_admission_ready(terminal_id=terminal_id, generation=generation)


# ---------------------------------------------------------------------------
# response loss / restart adopts durable state
# ---------------------------------------------------------------------------


def test_response_loss_adopts_durable_state_not_duplicates(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(_worker_contract(agent_id))
    second = roster.bind_generation(_worker_contract(agent_id))
    assert second["agent"]["agent_id"] == agent_id
    assert len(roster.list_agents()) == 1
    assert len(roster.list_lineages(agent_id=agent_id)) == 1
    assert len(roster.list_incarnations(agent_id=agent_id)) == 1
    roster.mark_admitted(terminal_id="a1b2c3d4", generation="00000000-0000-4000-8000-000000000001")
    third = roster.bind_generation(_worker_contract(agent_id))
    assert third["incarnation"]["disposition"] == roster.INCARNATION_ADMITTED
    assert len(roster.list_lineages(agent_id=agent_id)) == 1
    assert len(roster.list_incarnations(agent_id=agent_id)) == 1


# ---------------------------------------------------------------------------
# legacy / missing / corrupt rows degrade truthfully
# ---------------------------------------------------------------------------


def test_legacy_missing_rows_do_not_crash_reads_or_block_launches(
    isolated_memory_db,
):
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        db.add(
            database.StableAgentModel(
                agent_id="00000000-0000-4000-8000-0000000000aa",
                session_name="cao-legacy",
                role=roster.ROLE_WORKER,
                profile_family="developer",
                disposition=roster.DISPOSITION_IDENTITY_MISSING,
                resume_contract_version="unknown-version-0",
                revision=1,
                created_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                updated_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            )
        )
        db.commit()

    with database.SessionLocal() as db:
        db.add(
            database.StableAgentLineageModel(
                lineage_id="00000000-0000-4000-8000-0000000000bb",
                agent_id="00000000-0000-4000-8000-0000000000aa",
                harness="claude_code",
                native_session_id=None,
                route_provenance_json="{not json",
                lineage_origin=roster.LINEAGE_ORIGIN_INITIAL,
                created_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                updated_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            )
        )
        db.commit()

    agents = roster.list_agents()
    assert len(agents) == 1
    audit = roster.audit_dry_run()
    assert audit["agents_total"] >= 1
    assert audit["identity_missing_count"] >= 1
    assert audit["problems"]  # the corrupt lineage is reported, not fatal

    fresh = roster.bind_generation(_worker_contract())
    assert fresh["agent"]["disposition"] == roster.DISPOSITION_LIVE


def test_unknown_disposition_degrades_truthfully(isolated_memory_db):
    from cli_agent_orchestrator.clients import database

    with database.SessionLocal() as db:
        db.add(
            database.StableAgentModel(
                agent_id="00000000-0000-4000-8000-0000000000cc",
                session_name="cao-odd",
                role=roster.ROLE_WORKER,
                profile_family="reviewer",
                disposition="sentient",
                resume_contract_version=roster.RESUME_CONTRACT_VERSION,
                revision=1,
                created_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
                updated_at=_rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc)),
            )
        )
        db.commit()
    agents = roster.list_agents()
    assert agents[0]["disposition"] == "sentient"
    assert agents[0]["disposition_known"] is False
    audit = roster.audit_dry_run()
    assert any("disposition" in str(p) for p in audit["problems"])


# ---------------------------------------------------------------------------
# supervisor and worker share one contract; roles are explicit
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "role,family",
    [(roster.ROLE_SUPERVISOR, "code_supervisor"), (roster.ROLE_WORKER, "developer")],
)
def test_supervisor_and_worker_share_the_same_identity_contract(isolated_memory_db, role, family):
    if role == roster.ROLE_SUPERVISOR:
        contract = _supervisor_contract()
    else:
        contract = _worker_contract(native_session_id=None, acquisition_method=None)
    bound = roster.bind_generation(contract)
    agent = bound["agent"]
    assert agent["role"] == role
    assert agent["profile_family"] == family
    assert agent["session_name"] == "cao-campaign-a"
    assert bound["lineage"]["native_session_id"] is None
    assert agent["disposition"] == roster.DISPOSITION_IDENTITY_MISSING
    replay = roster.bind_generation(contract)
    assert replay["agent"]["agent_id"] == agent["agent_id"]


def test_custom_supervisor_profile_is_bound_by_explicit_role(isolated_memory_db):
    """Role is launch truth, not a profile-name heuristic: a custom
    supervisor profile stays a supervisor only when its owning operation
    says so, and a worker profile named like a supervisor stays a
    worker."""
    supervisor = roster.bind_generation(_supervisor_contract(profile_family="my-custom-supervisor"))
    assert supervisor["agent"]["role"] == roster.ROLE_SUPERVISOR
    assert supervisor["agent"]["profile_family"] == "my-custom-supervisor"

    worker = roster.bind_generation(
        _worker_contract(
            profile_family="supervisor",
            terminal_id="c3d4e5f6",
            generation="00000000-0000-4000-8000-000000000002",
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    assert worker["agent"]["role"] == roster.ROLE_WORKER
    assert worker["agent"]["profile_family"] == "supervisor"
    assert len(roster.list_agents()) == 2


def test_role_is_closed_in_the_contract(isolated_memory_db):
    with pytest.raises(roster.StableAgentInvalid):
        _worker_contract(role="overseer")
    with pytest.raises(roster.StableAgentInvalid):
        _worker_contract(agent_id="not-a-uuid")


# ---------------------------------------------------------------------------
# honest provider-contract representations (deterministic, no live calls)
# ---------------------------------------------------------------------------


def test_kimi_lazy_identity_represented_honestly(isolated_memory_db):
    pending = roster.bind_generation(
        _worker_contract(
            harness="kimi_cli",
            native_session_id=None,
            acquisition_method=None,
        )
    )
    assert pending["lineage"]["native_session_id"] is None
    assert pending["agent"]["disposition"] == roster.DISPOSITION_IDENTITY_MISSING

    agent_id = pending["agent"]["agent_id"]
    bound = roster.bind_generation(
        _worker_contract(
            agent_id,
            harness="kimi_cli",
            native_session_id="kimi-session-0192a7b4",
            acquisition_method=roster.ACQUISITION_ACP_BOOTSTRAP,
        )
    )
    assert bound["lineage"]["native_session_id"] == "kimi-session-0192a7b4"
    assert bound["lineage"]["acquisition_method"] == roster.ACQUISITION_ACP_BOOTSTRAP
    assert bound["lineage"]["harness"] == "kimi_cli"


def test_codex_pre_turn_thread_limitation_represented_honestly(isolated_memory_db):
    bound = roster.bind_generation(
        _worker_contract(
            harness="codex",
            native_session_id="thr_0192a7b4",
            acquisition_method=roster.ACQUISITION_ZERO_TURN_BOOTSTRAP,
            route_provenance={"provider_route": "anthropic"},
            continuity_note="pre-turn thread has no persisted rollout; one-turn canary pending",
        )
    )
    lineage = bound["lineage"]
    assert lineage["harness"] == "codex"
    assert lineage["acquisition_method"] == roster.ACQUISITION_ZERO_TURN_BOOTSTRAP
    assert lineage["continuity_note"] == (
        "pre-turn thread has no persisted rollout; one-turn canary pending"
    )


def test_claude_route_provenance_keeps_one_harness_domain(isolated_memory_db):
    """DeepSeek and Z.ai are Claude Code routes, not separate harness
    identity domains: route provenance travels beside the id, and the
    harness domain of the id never changes."""
    agent_ids = [str(uuid.uuid4()) for _ in range(3)]
    for index, (agent_id, route) in enumerate(zip(agent_ids, ("anthropic", "deepseek", "zai"))):
        roster.bind_generation(
            _worker_contract(
                agent_id,
                native_session_id=f"claude-{route}-11111111",
                route_provenance={"provider_route": route},
                terminal_id=f"a1b2c3d{index}",
                generation=f"00000000-0000-4000-8000-0000000000{index}",
            )
        )
    agents = roster.list_agents(session_name="cao-campaign-a")
    assert len(agents) == 3
    for agent in agents:
        for lineage in roster.list_lineages(agent_id=agent["agent_id"]):
            assert lineage["harness"] == "claude_code"
            assert lineage["route_provenance"]["provider_route"] in (
                "anthropic",
                "deepseek",
                "zai",
            )


def test_muse_enrollment_contract_truthful(isolated_memory_db):
    bound = roster.bind_generation(
        _worker_contract(
            harness="muse_cli",
            native_session_id="11111111-2222-4333-8444-5555555555aa",
            acquisition_method=roster.ACQUISITION_CHOSEN_SESSION_ID,
        )
    )
    assert bound["lineage"]["harness"] == "muse_cli"
    assert bound["lineage"]["acquisition_method"] == roster.ACQUISITION_CHOSEN_SESSION_ID


# ---------------------------------------------------------------------------
# identity_missing repair seam
# ---------------------------------------------------------------------------


def test_identity_missing_is_repairable_and_never_overwritten(isolated_memory_db):
    terminal_id = "b2c3d4e5"
    generation = "00000000-0000-4000-8000-000000000002"
    missing = roster.bind_generation(_supervisor_contract())
    missing_lineage_id = missing["lineage"]["lineage_id"]

    repaired = roster.record_native_identity(
        terminal_id=terminal_id,
        native_session_id="11111111-2222-4333-8444-5555555555bb",
        harness="claude_code",
    )
    assert repaired["lineage"]["lineage_id"] == missing_lineage_id
    assert repaired["lineage"]["native_session_id"] == "11111111-2222-4333-8444-5555555555bb"
    assert repaired["lineage"]["lineage_origin"] == roster.LINEAGE_ORIGIN_REPAIR
    assert repaired["agent"]["disposition"] == roster.DISPOSITION_LIVE

    with pytest.raises(roster.StableAgentConflict):
        roster.record_native_identity(
            terminal_id=terminal_id,
            native_session_id="99999999-8888-4777-8666-5555555555cc",
            harness="claude_code",
        )
    agent = roster.get_agent(missing["agent"]["agent_id"])
    assert agent["current_lineage_id"] == missing_lineage_id
    assert agent["current_lineage"]["native_session_id"] == "11111111-2222-4333-8444-5555555555bb"
    assert len(roster.list_lineages(agent_id=agent["agent_id"])) == 1


def test_identity_missing_does_not_block_stop(isolated_memory_db):
    bound = roster.bind_generation(_supervisor_contract())
    assert bound["agent"]["disposition"] == roster.DISPOSITION_IDENTITY_MISSING
    retired = roster.retire_incarnation(
        terminal_id="b2c3d4e5",
        generation="00000000-0000-4000-8000-000000000002",
        reason="stop",
    )
    assert retired["disposition"] == roster.INCARNATION_RETIRED
    agent = roster.get_agent(bound["agent"]["agent_id"])
    assert agent["disposition"] == roster.DISPOSITION_DORMANT


# ---------------------------------------------------------------------------
# P2: get_agent returns the CURRENT incarnation (never a terminal-id mixup)
# ---------------------------------------------------------------------------


def test_get_agent_returns_current_incarnation_by_id(isolated_memory_db):
    agent_id = str(uuid.uuid4())
    roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-000000000001",
        )
    )
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-000000000001",
        reason="done",
    )
    second = roster.bind_generation(
        _worker_contract(
            agent_id,
            native_session_id="77777777-6666-4555-8444-333333333333",
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-000000000005",
        )
    )
    agent = roster.get_agent(agent_id)
    assert agent["current_incarnation"]["incarnation_id"] == second["incarnation"]["incarnation_id"]
    assert agent["current_incarnation"]["terminal_id"] == "d4e5f607"
    assert agent["current_lineage"]["lineage_id"] == second["lineage"]["lineage_id"]


# ---------------------------------------------------------------------------
# pass 2 (coordinator): incarnation identity is (terminal_id, generation)
# ---------------------------------------------------------------------------


def test_two_historical_generations_of_one_terminal_id_coexist(isolated_memory_db):
    """A later generation may reuse a terminal id; both historical
    incarnations stay readable and never collide."""
    gen1 = "00000000-0000-4000-8000-0000000000a1"
    gen2 = "00000000-0000-4000-8000-0000000000a2"
    first = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation=gen1,
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    roster.retire_incarnation(terminal_id="a1b2c3d4", generation=gen1, reason="done")
    # An unrelated initial launch reuses the terminal id with a new
    # generation: it is a different stable agent with a different derived id.
    second = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation=gen2,
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    assert second["agent"]["agent_id"] != first["agent"]["agent_id"]

    incarnations = roster.list_incarnations()
    assert len(incarnations) == 2
    by_generation = {i["generation"]: i for i in incarnations}
    assert by_generation[gen1]["terminal_id"] == "a1b2c3d4"
    assert by_generation[gen1]["disposition"] == roster.INCARNATION_RETIRED
    assert by_generation[gen2]["terminal_id"] == "a1b2c3d4"
    assert by_generation[gen2]["disposition"] == roster.INCARNATION_BOUND
    assert by_generation[gen2]["agent_id"] == second["agent"]["agent_id"]

    # Terminal-only read resolves the unique LIVE incarnation.
    live = roster.get_incarnation_by_terminal("a1b2c3d4")
    assert live["generation"] == gen2
    # Exact reads resolve each generation.
    exact = roster.get_incarnation_by_terminal("a1b2c3d4", generation=gen1)
    assert exact["generation"] == gen1


def test_exact_generation_admission_retirement_repair(isolated_memory_db):
    gen1 = "00000000-0000-4000-8000-0000000000b1"
    gen2 = "00000000-0000-4000-8000-0000000000b2"
    roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation=gen1,
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    # Admission is exact: gen1 passes, the never-launched gen2 refuses.
    roster.assert_admission_ready(terminal_id="a1b2c3d4", generation=gen1)
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.assert_admission_ready(terminal_id="a1b2c3d4", generation=gen2)

    # Retirement is exact: retiring the wrong generation refuses.
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.retire_incarnation(terminal_id="a1b2c3d4", generation=gen2, reason="wrong gen")
    retired = roster.retire_incarnation(terminal_id="a1b2c3d4", generation=gen1, reason="done")
    assert retired["disposition"] == roster.INCARNATION_RETIRED

    # Repair is exact: a missing lineage on gen1 repairs; gen2 refuses.
    missing = roster.bind_generation(
        _supervisor_contract(
            terminal_id="b2c3d4e5",
            generation=gen1,
        )
    )
    repaired = roster.record_native_identity(
        terminal_id="b2c3d4e5",
        generation=gen1,
        native_session_id="33333333-4444-4555-8666-777777777777",
        harness="claude_code",
    )
    assert repaired["agent"]["agent_id"] == missing["agent"]["agent_id"]
    with pytest.raises(roster.StableAgentAdmissionRefused):
        roster.record_native_identity(
            terminal_id="b2c3d4e5",
            generation=gen2,
            native_session_id="44444444-5555-4666-8777-888888888888",
            harness="claude_code",
        )


def test_ambiguous_terminal_only_lookup_refuses(isolated_memory_db):
    """When two live incarnations share a terminal id (corrupt/legacy
    state), a terminal-only lookup refuses instead of picking a row."""
    gen1 = "00000000-0000-4000-8000-0000000000c1"
    gen2 = "00000000-0000-4000-8000-0000000000c2"
    roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation=gen1,
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    # A different agent may legitimately own gen2 of the same terminal id
    # (an unrelated initial launch); both live is ambiguous for reads.
    roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation=gen2,
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    with pytest.raises(roster.StableAgentConflict, match="ambiguous"):
        roster.get_incarnation_by_terminal("a1b2c3d4")
    # Exact lookups still work.
    assert roster.get_incarnation_by_terminal("a1b2c3d4", generation=gen1)["generation"] == gen1


def test_derive_initial_agent_id_includes_generation(isolated_memory_db):
    gen1 = "00000000-0000-4000-8000-0000000000d1"
    gen2 = "00000000-0000-4000-8000-0000000000d2"
    assert roster.derive_initial_agent_id(
        "a1b2c3d4", generation=gen1
    ) != roster.derive_initial_agent_id("a1b2c3d4", generation=gen2)
    assert roster.derive_initial_agent_id(
        "a1b2c3d4", generation=gen1
    ) == roster.derive_initial_agent_id("a1b2c3d4", generation=gen1)
    # Deterministic across calls.
    assert roster.derive_initial_agent_id("a1b2c3d4", generation=gen1) == str(
        roster.derive_initial_agent_id("a1b2c3d4", generation=gen1)
    )


# ---------------------------------------------------------------------------
# pass 2 (coordinator): one live incarnation per stable agent
# ---------------------------------------------------------------------------


def test_one_live_incarnation_per_agent_across_lineages(isolated_memory_db):
    """Binding the same agent_id to a new native id/new terminal while its
    old lineage is still live must refuse with zero new lineage or
    incarnation mutation; after retirement it succeeds and links history."""
    agent_id = str(uuid.uuid4())
    first = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-0000000000e1",
            native_session_id="11111111-2222-4333-8444-555555555555",
        )
    )
    lineages_before = len(roster.list_lineages(agent_id=agent_id))
    incarnations_before = len(roster.list_incarnations(agent_id=agent_id))

    with pytest.raises(roster.StableAgentConflict, match="live incarnation"):
        roster.bind_generation(
            _worker_contract(
                agent_id,
                terminal_id="d4e5f607",
                generation="00000000-0000-4000-8000-0000000000e2",
                native_session_id="22222222-3333-4444-8555-666666666666",
            )
        )
    # Zero mutation: no new lineage, no new incarnation.
    assert len(roster.list_lineages(agent_id=agent_id)) == lineages_before
    assert len(roster.list_incarnations(agent_id=agent_id)) == incarnations_before

    # After the predecessor is retired, the same agent may take a fresh
    # fallback with a new native id and a new lineage linked to history.
    roster.retire_incarnation(
        terminal_id="a1b2c3d4",
        generation="00000000-0000-4000-8000-0000000000e1",
        reason="pane lost",
    )
    resumed = roster.bind_generation(
        _worker_contract(
            agent_id,
            terminal_id="d4e5f607",
            generation="00000000-0000-4000-8000-0000000000e2",
            native_session_id="22222222-3333-4444-8555-666666666666",
        )
    )
    assert resumed["agent"]["agent_id"] == agent_id
    assert resumed["lineage"]["predecessor_lineage_id"] == first["lineage"]["lineage_id"]
    assert len(roster.list_incarnations(agent_id=agent_id)) == 2


# ---------------------------------------------------------------------------
# pass 2 (coordinator): repair race stays typed and converges
# ---------------------------------------------------------------------------


def test_repair_race_same_harness_native_id_is_typed_and_converges(
    isolated_memory_db,
):
    """Two threads repairing the same (harness, native_session_id) onto
    different incarnations: exactly one lineage wins and the loser gets a
    typed refusal — never a raw SQLAlchemy error and never a duplicate
    lineage.  The production unique index is migration-owned, so the test
    installs it explicitly, exactly as init_db would."""
    from sqlalchemy import text

    with isolated_memory_db.begin() as conn:
        conn.execute(
            text(
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "ix_stable_lineage_harness_native_session_id "
                "ON stable_agent_lineages(harness, native_session_id) "
                "WHERE native_session_id IS NOT NULL"
            )
        )
    native_id = "55555555-6666-4777-8888-999999999999"

    agent_a = roster.bind_generation(
        _worker_contract(
            terminal_id="a1b2c3d4",
            generation="00000000-0000-4000-8000-0000000000f1",
            native_session_id=None,
            acquisition_method=None,
        )
    )
    agent_b = roster.bind_generation(
        _worker_contract(
            terminal_id="c3d4e5f6",
            generation="00000000-0000-4000-8000-0000000000f2",
            native_session_id=None,
            acquisition_method=None,
        )
    )
    assert agent_a["agent"]["agent_id"] != agent_b["agent"]["agent_id"]

    results: list[dict] = []
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def _repair(terminal_id: str) -> None:
        try:
            barrier.wait(timeout=10)
            results.append(
                roster.record_native_identity(
                    terminal_id=terminal_id,
                    native_session_id=native_id,
                    harness="claude_code",
                )
            )
        except Exception as exc:  # noqa: BLE001 - recorded for the assertion
            errors.append(exc)

    threads = [
        threading.Thread(target=_repair, args=(agent_a["incarnation"]["terminal_id"],)),
        threading.Thread(target=_repair, args=(agent_b["incarnation"]["terminal_id"],)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 1
    assert len(errors) == 1
    assert isinstance(errors[0], roster.StableAgentError)
    assert not any("IntegrityError" in type(e).__name__ for e in errors)
    # Exactly one lineage holds the id.
    lineages = roster.list_lineages()
    holders = [l for l in lineages if l["native_session_id"] == native_id]
    assert len(holders) == 1
    # The winning agent owns it; the loser's incarnation is unchanged.
    winner = results[0]
    assert winner["lineage"]["native_session_id"] == native_id
    assert winner["lineage"]["lineage_origin"] == roster.LINEAGE_ORIGIN_REPAIR


# ---------------------------------------------------------------------------
# PR #91 review: i-0025 repair refuses a retired incarnation
# ---------------------------------------------------------------------------


def test_repair_refuses_retired_incarnation(isolated_memory_db):
    """Native-identity repair must refuse an exact incarnation that is
    already retired, transactionally: a repair/teardown race must not
    revive a dead terminal or persist a live agent with no live
    incarnation."""
    terminal_id = "b2c3d4e5"
    generation = "00000000-0000-4000-8000-0000000000c1"
    bound = roster.bind_generation(
        _supervisor_contract(
            terminal_id=terminal_id,
            generation=generation,
        )
    )
    agent_id = bound["agent"]["agent_id"]
    lineage_id = bound["lineage"]["lineage_id"]

    roster.retire_incarnation(terminal_id=terminal_id, generation=generation, reason="done")
    with pytest.raises(roster.StableAgentConflict, match="retired"):
        roster.record_native_identity(
            terminal_id=terminal_id,
            generation=generation,
            native_session_id="11111111-2222-4333-8444-5555555555cc",
            harness="claude_code",
        )

    agent = roster.get_agent(agent_id)
    assert agent["disposition"] == roster.DISPOSITION_DORMANT
    assert agent["current_incarnation"]["disposition"] == roster.INCARNATION_RETIRED
    # The lineage was untouched by the refused repair.
    assert roster.list_lineages(agent_id=agent_id)[0]["lineage_id"] == lineage_id
    assert roster.list_lineages(agent_id=agent_id)[0]["native_session_id"] is None


def test_audit_flags_live_agent_with_retired_current_incarnation(isolated_memory_db):
    """The audit validates agent/incarnation disposition consistency: a
    LIVE agent whose current incarnation is retired is reported as a
    problem, and a DORMANT agent with a live current incarnation too."""
    from cli_agent_orchestrator.clients import database

    stamp = _rfc3339(datetime(2026, 1, 1, tzinfo=timezone.utc))
    agent_id = "00000000-0000-4000-8000-0000000000c2"
    with database.SessionLocal() as db:
        db.add(
            database.StableAgentModel(
                agent_id=agent_id,
                session_name="cao-odd",
                role=roster.ROLE_WORKER,
                profile_family="developer",
                disposition=roster.DISPOSITION_LIVE,
                resume_contract_version=roster.RESUME_CONTRACT_VERSION,
                current_incarnation_id="00000000-0000-4000-8000-0000000000c3",
                revision=1,
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.add(
            database.StableAgentIncarnationModel(
                incarnation_id="00000000-0000-4000-8000-0000000000c3",
                agent_id=agent_id,
                lineage_id=None,
                terminal_id="a1b2c3d4",
                generation="00000000-0000-4000-8000-0000000000c4",
                disposition=roster.INCARNATION_RETIRED,
                retired_at=stamp,
                retirement_reason="done",
                created_at=stamp,
                updated_at=stamp,
            )
        )
        db.commit()

    audit = roster.audit_dry_run()
    assert any(
        p["kind"] == "live-agent-with-retired-current-incarnation" for p in audit["problems"]
    )
