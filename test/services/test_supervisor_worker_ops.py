"""M3-D supervisor-owned worker retire, resume, and lost-pane recovery."""

from __future__ import annotations

import asyncio
import uuid

import pytest

from cli_agent_orchestrator.services import exact_executor
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import supervisor_worker_ops as ops
from cli_agent_orchestrator.services import task_occurrence as occ
from cli_agent_orchestrator.services import terminal_service

SESSION = "cao-m3d-ops"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64


@pytest.fixture(autouse=True)
def _db(isolated_memory_db, monkeypatch, tmp_path):
    from cli_agent_orchestrator import constants

    monkeypatch.setattr(constants, "COMPANION_DIR", tmp_path / "companion")
    return isolated_memory_db


@pytest.fixture
def _no_tmux(monkeypatch):
    """Pane collection is the fork's; this slice only proves it is ordered."""
    calls: list[tuple[str, str]] = []

    def _delete(terminal_id, registry=None, **kwargs):
        calls.append((terminal_id, kwargs.get("expected_generation")))
        return True

    monkeypatch.setattr(terminal_service, "delete_terminal", _delete)
    return calls


def _bind(*, suffix="1", role=roster.ROLE_WORKER, agent_id=None):
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id or str(uuid.uuid4()),
            session_name=SESSION,
            role=role,
            profile_family="supervisor" if role == roster.ROLE_SUPERVISOR else "developer",
            harness="claude_code",
            native_session_id=f"native-{suffix}",
            acquisition_method="chosen_session_id",
            terminal_id=f"term-{suffix}",
            generation=str(uuid.uuid4()),
            pane_id=f"%{suffix}",
            pane_pid=8000 + int(suffix),
            process_identity={"pid": 8000 + int(suffix), "start_marker": f"m-{suffix}"},
            execution_mode="native_tui",
            admitted=True,
        )
    )


def _open_round(bound, *, round_index=0, seed=None):
    return occ.open_occurrence(
        occ.OpenRequest(
            task_occurrence_id=str(uuid.uuid4()),
            session_name=SESSION,
            agent_id=bound["agent"]["agent_id"],
            round_index=round_index,
            dispatch_digest=_DIGEST_A,
            incarnation=occ.EffectIncarnation(
                incarnation_id=bound["incarnation"]["incarnation_id"],
                terminal_id=bound["incarnation"]["terminal_id"],
                generation=bound["incarnation"]["generation"],
            ),
            seed=seed or occ.EMPTY_SEED,
        )
    )


def _complete_seed():
    return occ.TaskSeed(
        occ.SEED_COMPLETE,
        summary_digest=_DIGEST_B,
        artifacts=(
            occ.ArtifactReference(
                artifact_id="handoff",
                kind="markdown",
                reference="/tmp/handoff.md",
                content_digest=_DIGEST_C,
            ),
        ),
        produced_by="supervisor",
    )


# ---------------------------------------------------------------------------
# retire pane
# ---------------------------------------------------------------------------


def test_retire_finalizes_the_round_before_the_pane_is_collected(_no_tmux, monkeypatch):
    worker = _bind()
    record = _open_round(worker)
    occ.record_boundary(
        occ.BoundaryRecord(
            task_occurrence_id=record["task_occurrence_id"],
            expected_revision=0,
            recorded_by="worker",
            report_digest=_DIGEST_B,
        )
    )
    order: list[str] = []
    real_finalize = occ.finalize_occurrence

    def _finalize(request, db=None):
        order.append("finalize")
        return real_finalize(request, db)

    def _delete(terminal_id, registry=None, **kwargs):
        order.append("collect")
        return True

    monkeypatch.setattr(occ, "finalize_occurrence", _finalize)
    monkeypatch.setattr(terminal_service, "delete_terminal", _delete)

    result = ops.retire_worker_pane(
        ops.RetireRequest(
            session_name=SESSION,
            agent_id=worker["agent"]["agent_id"],
            retired_by="supervisor",
        )
    )

    assert order == ["finalize", "collect"]
    assert result["pane_collected"] is True
    finished = occ.get_occurrence(record["task_occurrence_id"])
    assert finished["state"] == occ.STATE_FINALIZED
    assert finished["finalized"]["report_digest"] == _DIGEST_B


def test_retire_is_idempotent_and_leaves_the_agent_free_for_a_new_round(_no_tmux):
    worker = _bind()
    record = _open_round(worker)
    request = ops.RetireRequest(
        session_name=SESSION, agent_id=worker["agent"]["agent_id"], retired_by="supervisor"
    )

    ops.retire_worker_pane(request)
    again = ops.retire_worker_pane(request)

    assert again["finalized_occurrence"] is None  # nothing left open to finalize
    assert occ.open_occurrence_for_agent(worker["agent"]["agent_id"]) is None
    assert occ.get_occurrence(record["task_occurrence_id"])["state"] == occ.STATE_FINALIZED


def test_retire_refuses_a_supervisor(_no_tmux):
    supervisor = _bind(role=roster.ROLE_SUPERVISOR)

    with pytest.raises(ops.WorkerOpsConflict, match="does not retire its own pane"):
        ops.retire_worker_pane(
            ops.RetireRequest(
                session_name=SESSION,
                agent_id=supervisor["agent"]["agent_id"],
                retired_by="supervisor",
            )
        )


def test_retire_refuses_an_agent_from_another_session(_no_tmux):
    worker = _bind()

    with pytest.raises(ops.WorkerOpsConflict, match="belongs to session"):
        ops.retire_worker_pane(
            ops.RetireRequest(
                session_name="cao-other",
                agent_id=worker["agent"]["agent_id"],
                retired_by="supervisor",
            )
        )


# ---------------------------------------------------------------------------
# worker resume
# ---------------------------------------------------------------------------


def _publish_contract(bound, tmp_path):
    """The immutable restore contract an exact resume needs to exist at all."""
    from cli_agent_orchestrator.services import restore_contract as rc

    unavailable = rc.ContractFact.unavailable("not captured at this test seam")
    return rc.publish_contract(
        rc.RestoreContract(
            agent_id=bound["agent"]["agent_id"],
            lineage_id=bound["lineage"]["lineage_id"],
            terminal_id=bound["incarnation"]["terminal_id"],
            generation=bound["incarnation"]["generation"],
            harness="claude_code",
            provider="claude_code",
            native_session_id=bound["lineage"]["native_session_id"],
            execution_mode="native_tui",
            working_directory=str(tmp_path),
            model=unavailable,
            effort=unavailable,
            executable=unavailable,
            profile_material=unavailable,
            provider_home_facts=unavailable,
        )
    )


def test_exact_resume_is_used_when_the_identity_survives(monkeypatch, tmp_path):
    worker = _bind()
    _publish_contract(worker, tmp_path)
    seen: list[str] = []

    async def _execute(request):
        seen.append(request.operation_id)
        return {
            "successor_incarnation_id": "inc-next",
            "successor_terminal_id": "term-next",
            "successor_generation": "gen-next",
        }

    monkeypatch.setattr(exact_executor, "execute", _execute)
    recovery = str(uuid.uuid4())

    result = asyncio.run(
        ops.resume_worker(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=recovery,
            requested_by="supervisor",
        )
    )

    assert result["outcome"] == ops.OUTCOME_EXACT_RESTORED
    assert seen == [ops.resume_operation_id(recovery, worker["agent"]["agent_id"])]


def test_a_refused_exact_resume_is_failed_and_never_silently_fresh(monkeypatch, tmp_path):
    worker = _bind()
    _publish_contract(worker, tmp_path)

    async def _execute(request):
        raise exact_executor.ExactExecutorRefused("provider refused the resume")

    monkeypatch.setattr(exact_executor, "execute", _execute)

    result = asyncio.run(
        ops.resume_worker(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
        )
    )

    assert result["outcome"] == ops.OUTCOME_FAILED
    assert "successor_incarnation_id" not in result


def test_an_ambiguous_exact_resume_is_undecided_not_failed(monkeypatch, tmp_path):
    worker = _bind()
    _publish_contract(worker, tmp_path)

    async def _execute(request):
        raise exact_executor.ExactExecutorReconciliation("the physical result was ambiguous")

    monkeypatch.setattr(exact_executor, "execute", _execute)

    result = asyncio.run(
        ops.resume_worker(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
        )
    )

    assert result["outcome"] == ops.OUTCOME_UNDECIDED


def test_a_resume_retry_reuses_the_same_m3b_operation_id():
    recovery = str(uuid.uuid4())
    agent = str(uuid.uuid4())
    assert ops.resume_operation_id(recovery, agent) == ops.resume_operation_id(recovery, agent)
    assert ops.resume_operation_id(recovery, agent) != ops.resume_operation_id(
        str(uuid.uuid4()), agent
    )


# ---------------------------------------------------------------------------
# lost-pane recovery
# ---------------------------------------------------------------------------


def test_recovery_prefers_exact_restoration_when_cao_still_holds_the_identity(
    monkeypatch, tmp_path
):
    worker = _bind()
    _publish_contract(worker, tmp_path)

    async def _execute(request):
        return {
            "successor_incarnation_id": "inc-next",
            "successor_terminal_id": "term-next",
            "successor_generation": "gen-next",
        }

    monkeypatch.setattr(exact_executor, "execute", _execute)

    plan = ops.plan_lost_pane_recovery(SESSION, worker["agent"]["agent_id"])
    assert plan["mode"] == ops.MODE_EXACT

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=_never_launch,
        )
    )
    assert result["outcome"] == ops.OUTCOME_EXACT_RESTORED


async def _never_launch(agent, fallback, occurrence_id):  # pragma: no cover - must not run
    raise AssertionError("a fresh worker must never be launched when an exact resume is possible")


def test_a_lost_pane_with_no_identity_and_no_context_stays_paused():
    worker = _bind()  # no restore contract published: the identity is gone
    _open_round(worker)

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
        )
    )

    assert result["mode"] == ops.MODE_PAUSED
    assert result["outcome"] == ops.OUTCOME_PAUSED
    assert "guessing" in result["reason"]
    # Nothing was launched and the round was neither replayed nor closed.
    assert occ.open_occurrence_for_agent(worker["agent"]["agent_id"]) is not None


@pytest.mark.parametrize(
    "seed",
    [
        occ.TaskSeed(occ.SEED_TRUNCATED, summary_digest=_DIGEST_B),
        occ.EMPTY_SEED,
    ],
    ids=["truncated", "empty"],
)
def test_a_partial_or_absent_seed_pauses_rather_than_starting_a_fresh_worker(seed):
    worker = _bind()
    _open_round(worker)

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(seed, _DIGEST_A, 1),
            fresh_launcher=_never_launch,
        )
    )

    assert result["mode"] == ops.MODE_PAUSED
    assert result["outcome"] == ops.OUTCOME_PAUSED
    assert seed.quality in result["reason"]


def test_a_complete_seed_starts_a_fresh_successor_bound_to_its_digests():
    worker = _bind()
    first = _open_round(worker, seed=_complete_seed())
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=first["task_occurrence_id"],
            expected_revision=0,
            disposition=occ.DISPOSITION_LOST,
            finalized_by="supervisor",
        )
    )
    seen: list[tuple[str, str]] = []

    async def _launch(agent, fallback, occurrence_id):
        seen.append((fallback.seed.quality, occurrence_id))
        return {
            "incarnation_id": "inc-fresh",
            "terminal_id": "term-fresh",
            "generation": "gen-fresh",
            "lineage_id": "lin-fresh",
            "native_session_id": "native-fresh",
        }

    recovery = str(uuid.uuid4())
    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=recovery,
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=_launch,
        )
    )

    assert result["outcome"] == ops.OUTCOME_FRESH_FALLBACK
    assert result["seed_quality"] == occ.SEED_COMPLETE
    assert result["seed_summary_digest"] == _DIGEST_B
    assert result["seed_artifact_digest"] == _complete_seed().artifact_seed_digest
    assert seen == [
        (occ.SEED_COMPLETE, ops.successor_occurrence_id(recovery, worker["agent"]["agent_id"]))
    ]

    # The successor gets a *new* occurrence, explicitly carrying its seed and
    # its completeness — it never inherits the round somebody already closed.
    successor = occ.get_occurrence(result["task_occurrence_id"])
    assert successor["task_occurrence_id"] != first["task_occurrence_id"]
    assert successor["round_index"] == 1
    assert successor["current"]["seed_quality"] == occ.SEED_COMPLETE
    assert successor["seed_verdict"]["sufficient_for_fresh_start"] is True
    assert successor["incarnation_id"] == "inc-fresh"


def test_a_complete_seed_with_no_fresh_authority_still_pauses():
    worker = _bind()
    _open_round(worker)

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
        )
    )

    assert result["outcome"] == ops.OUTCOME_PAUSED
    assert "no fresh-launch authority" in result["reason"]


def test_a_fresh_launch_that_raises_is_undecided_not_a_second_attempt():
    worker = _bind()
    _open_round(worker)

    async def _launch(agent, fallback, occurrence_id):
        raise RuntimeError("the launcher died after creating a pane")

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=_launch,
        )
    )

    assert result["outcome"] == ops.OUTCOME_UNDECIDED
    assert "launcher died" in result["detail"]


def test_a_retried_fresh_fallback_reopens_the_same_successor_occurrence():
    worker = _bind()
    _open_round(worker)
    occ.finalize_occurrence(
        occ.FinalizeRequest(
            task_occurrence_id=occ.open_occurrence_for_agent(worker["agent"]["agent_id"])[
                "task_occurrence_id"
            ],
            expected_revision=0,
            disposition=occ.DISPOSITION_LOST,
            finalized_by="supervisor",
        )
    )

    async def _launch(agent, fallback, occurrence_id):
        return {
            "incarnation_id": "inc-fresh",
            "terminal_id": "term-fresh",
            "generation": "gen-fresh",
        }

    recovery = str(uuid.uuid4())
    kwargs = dict(
        recovery_id=recovery,
        requested_by="supervisor",
        fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
        fresh_launcher=_launch,
    )
    first = asyncio.run(ops.recover_lost_pane(SESSION, worker["agent"]["agent_id"], **kwargs))
    again = asyncio.run(ops.recover_lost_pane(SESSION, worker["agent"]["agent_id"], **kwargs))

    assert again["task_occurrence_id"] == first["task_occurrence_id"]
    assert len(occ.list_occurrences(SESSION, agent_id=worker["agent"]["agent_id"])) == 2


# ---------------------------------------------------------------------------
# admission before any physical effect (cond-0380 P1-1)
# ---------------------------------------------------------------------------
#
# The realistic lost-pane case is the one the first cut got wrong: nobody
# finalized the round, because the thing that would have finalized it is the
# pane that vanished. So the predecessor occurrence is still *open* when
# recovery runs, and opening the successor against a stable agent that already
# holds an open round is a conflict — one that used to be raised *after* the
# launcher had already created a pane.


class _CountingLauncher:
    def __init__(self):
        self.calls: list[str] = []

    async def __call__(self, agent, fallback, occurrence_id):
        self.calls.append(occurrence_id)
        return {
            "incarnation_id": f"inc-fresh-{len(self.calls)}",
            "terminal_id": f"term-fresh-{len(self.calls)}",
            "generation": f"gen-fresh-{len(self.calls)}",
        }


def test_a_lost_worker_with_an_open_round_admits_before_it_launches(_no_tmux):
    worker = _bind()
    predecessor = _open_round(worker)
    launcher = _CountingLauncher()

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=launcher,
        )
    )

    assert result["outcome"] == ops.OUTCOME_FRESH_FALLBACK
    assert len(launcher.calls) == 1
    # The predecessor round is resolved rather than left open behind a live
    # successor: two open rounds for one agent is the state the whole seam
    # exists to make impossible.
    closed = occ.get_occurrence(predecessor["task_occurrence_id"])
    assert closed["state"] == occ.STATE_FINALIZED
    assert closed["finalized"]["disposition"] == occ.DISPOSITION_LOST
    successor = occ.get_occurrence(result["task_occurrence_id"])
    assert successor["state"] == occ.STATE_OPEN
    assert successor["incarnation_id"] == "inc-fresh-1"


def test_a_pane_is_never_created_when_admission_cannot_succeed(_no_tmux, monkeypatch):
    """No orphan: if the round cannot be resolved, nothing is launched."""
    worker = _bind()
    _open_round(worker)
    launcher = _CountingLauncher()

    def _refuse(request, db=None):
        raise occ.TaskOccurrenceConflict("the predecessor round cannot be resolved")

    monkeypatch.setattr(occ, "finalize_occurrence", _refuse)

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            worker["agent"]["agent_id"],
            recovery_id=str(uuid.uuid4()),
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=launcher,
        )
    )

    assert launcher.calls == []
    assert result["outcome"] == ops.OUTCOME_PAUSED
    assert "cannot be resolved" in result["reason"]


def test_a_retry_after_a_lost_open_response_adopts_the_pane_it_already_made(_no_tmux):
    """The orphan window: launched, then the process died before the open.

    A second launch here would leave the first pane running with nothing
    pointing at it — the exact duplicate-pane failure a derived id alone does
    not prevent, because the derived id only helps once a row exists.
    """
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    _open_round(worker)
    recovery = str(uuid.uuid4())
    launcher = _CountingLauncher()

    # First attempt: admit, launch, then lose the response before the occurrence
    # is opened. The pane exists and the roster knows about it.
    admitted = ops.admit_fresh_successor(
        SESSION, agent_id, recovery_id=recovery, requested_by="supervisor"
    )
    assert admitted["mode"] == ops.ADMIT_LAUNCH
    launched = asyncio.run(launcher(None, None, admitted["task_occurrence_id"]))
    bound = _bind_successor(agent_id, launched)

    result = asyncio.run(
        ops.recover_lost_pane(
            SESSION,
            agent_id,
            recovery_id=recovery,
            requested_by="supervisor",
            fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
            fresh_launcher=launcher,
        )
    )

    assert len(launcher.calls) == 1  # adopted, not launched again
    assert result["outcome"] == ops.OUTCOME_FRESH_FALLBACK
    # The adopted successor is the pane that already exists, named by the
    # roster rather than by whatever the launcher happened to return.
    assert result["successor_terminal_id"] == launched["terminal_id"]
    assert result["successor_incarnation_id"] == bound["incarnation"]["incarnation_id"]
    assert len(occ.list_occurrences(SESSION, agent_id=agent_id)) == 2


def test_a_retry_after_a_complete_recovery_adopts_without_launching(_no_tmux):
    worker = _bind()
    agent_id = worker["agent"]["agent_id"]
    _open_round(worker)
    recovery = str(uuid.uuid4())
    launcher = _CountingLauncher()
    kwargs = dict(
        recovery_id=recovery,
        requested_by="supervisor",
        fallback=ops.FreshFallback(_complete_seed(), _DIGEST_A, 1),
        fresh_launcher=launcher,
    )

    first = asyncio.run(ops.recover_lost_pane(SESSION, agent_id, **kwargs))
    second = asyncio.run(ops.recover_lost_pane(SESSION, agent_id, **kwargs))

    assert len(launcher.calls) == 1
    assert second["task_occurrence_id"] == first["task_occurrence_id"]
    assert second["successor_incarnation_id"] == first["successor_incarnation_id"]
    assert len(occ.list_occurrences(SESSION, agent_id=agent_id)) == 2


def _bind_successor(agent_id, launched):
    """Bind the fresh pane a launcher created, as a real launcher would."""
    agent = roster.get_agent(agent_id)
    return roster.bind_generation(
        roster.BindingContract(
            agent_id=agent_id,
            session_name=SESSION,
            role=agent["role"],
            profile_family=agent["profile_family"],
            harness="claude_code",
            native_session_id=f"native-{launched['incarnation_id']}",
            acquisition_method="chosen_session_id",
            terminal_id=launched["terminal_id"],
            generation=launched["generation"],
            pane_id="%77",
            pane_pid=8077,
            process_identity={"pid": 8077, "start_marker": "m-77"},
            execution_mode="native_tui",
            admitted=True,
        )
    )
