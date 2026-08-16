"""Supervisor-owned worker retire, resume, and lost-pane recovery (M3-D).

Three acts a supervisor performs on one of its own workers, and the reason
each one lives here rather than in the fork substrates it calls:

``retire_worker_pane``
    Ending a worker is a *task* decision before it is a pane decision. The
    occurrence is finalized first, so the round's evidence is preserved
    write-once, and only then is the incarnation retired and the pane
    collected. Doing it the other way round loses the report at exactly the
    moment somebody needs it.

``resume_worker``
    Exact same-native-session restoration through M3-B, and nothing else. A
    worker M3-B refuses is ``failed``; it is never quietly relaunched fresh,
    because the whole value of an exact resume is that the transcript is the
    same transcript, and a silent downgrade would give the supervisor a worker
    that looks continuous and is not.

``recover_lost_pane``
    The pane is gone and the supervisor has to decide what replaces it. Three
    outcomes, and the third is the important one:

    * exact restoration if CAO still holds the identity;
    * a fresh fallback **only** with the supervisor summary, the digest-bound
      artifacts, and an explicit ``complete`` seed quality behind it;
    * otherwise the worker stays **paused for reconciliation** — no fresh
      pane, no replayed input, no reconstructed instruction.

    A truncated seed is not a small problem: it reads as context while missing
    the part that mattered, so a fresh worker started on it continues
    confidently from the wrong place. Guessing at a lost worker's task, or
    replaying the input it was last sent, is worse than pausing — the pause is
    visible and recoverable, and the guess is neither.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional

from cli_agent_orchestrator.services import exact_executor
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import task_occurrence as occurrence
from cli_agent_orchestrator.services import terminal_service

SCHEMA_VERSION = "cao-m3d-worker-ops-v1"

MODE_EXACT = "exact"
MODE_FRESH = "fresh"
MODE_PAUSED = "paused-for-reconciliation"
RECOVERY_MODES = frozenset({MODE_EXACT, MODE_FRESH, MODE_PAUSED})

OUTCOME_EXACT_RESTORED = "exact-restored"
OUTCOME_FRESH_FALLBACK = "fresh-fallback"
OUTCOME_PAUSED = MODE_PAUSED
OUTCOME_FAILED = "failed"
#: Not an outcome but the absence of one: the physical result was ambiguous.
OUTCOME_UNDECIDED = "undecided"

#: What admission decided, before any pane exists. A fresh launch happens on
#: exactly one of these.
ADMIT_LAUNCH = "launch"
ADMIT_ADOPT_OCCURRENCE = "adopt-occurrence"
ADMIT_ADOPT_INCARNATION = "adopt-incarnation"
ADMIT_REFUSED = "refused"

_MEMBER_NAMESPACE = uuid.UUID("03800000-d000-4d00-a7e3-000000000004")
_OCCURRENCE_NAMESPACE = uuid.UUID("03800000-d000-4d00-a7e3-000000000005")


class WorkerOpsError(RuntimeError):
    code = "worker-ops-error"


class WorkerOpsInvalid(WorkerOpsError):
    code = "worker-ops-invalid"


class WorkerOpsConflict(WorkerOpsError):
    code = "worker-ops-conflict"


class WorkerOpsNotFound(WorkerOpsError):
    code = "worker-ops-not-found"


def resume_operation_id(recovery_id: str, agent_id: str) -> str:
    """The exact M3-B operation id one recovery uses for one worker.

    Derived rather than minted so a retried recovery adopts the reincarnation
    the previous attempt already claimed instead of racing a second one for
    the same stable agent.
    """
    return str(uuid.uuid5(_MEMBER_NAMESPACE, f"{recovery_id}:{agent_id}"))


def successor_occurrence_id(recovery_id: str, agent_id: str) -> str:
    """The occurrence id a fresh fallback opens for the successor.

    Derived for the same reason: a retried fresh fallback re-opens *the same*
    occurrence and adopts it, rather than opening a second round for a worker
    that already has one.
    """
    return str(uuid.uuid5(_OCCURRENCE_NAMESPACE, f"{recovery_id}:{agent_id}"))


@dataclass(frozen=True)
class RetireRequest:
    """Retire one worker's pane, preserving its round first."""

    session_name: str
    agent_id: str
    retired_by: str
    disposition: str = occurrence.DISPOSITION_REPORTED
    reason: str = "supervisor retired the worker"

    def __post_init__(self) -> None:
        try:
            object.__setattr__(
                self, "session_name", occurrence._normalise_session_name(self.session_name)
            )
            object.__setattr__(
                self, "agent_id", occurrence._require_uuid(self.agent_id, field_name="agent_id")
            )
        except occurrence.TaskOccurrenceInvalid as exc:
            raise WorkerOpsInvalid(str(exc)) from exc
        if self.disposition not in occurrence.FINAL_DISPOSITIONS:
            raise WorkerOpsInvalid(
                f"disposition must be one of {sorted(occurrence.FINAL_DISPOSITIONS)}; "
                f"got {self.disposition!r}"
            )
        if not isinstance(self.retired_by, str) or not self.retired_by:
            raise WorkerOpsInvalid("retired_by must name the supervisor; it is recorded")


def _agent(agent_id: str) -> dict[str, Any]:
    try:
        return roster.get_agent(agent_id)
    except roster.StableAgentNotFound as exc:
        raise WorkerOpsNotFound(str(exc)) from exc
    except roster.StableAgentError as exc:
        raise WorkerOpsConflict(str(exc)) from exc


def retire_worker_pane(request: RetireRequest) -> dict[str, Any]:
    """Finalize the round, retire the incarnation, then collect the pane.

    The order is the guarantee. Finalizing first means a failure anywhere
    after it leaves the round's evidence intact and write-once; collecting
    first would destroy the pane that produced it and leave an open occurrence
    pointing at an incarnation that no longer exists.

    Idempotent throughout: a finalized occurrence adopts, a retired
    incarnation adopts, and terminal deletion converges.
    """
    if not isinstance(request, RetireRequest):
        raise WorkerOpsInvalid(f"request must be a RetireRequest; got {type(request).__name__}")
    agent = _agent(request.agent_id)
    if agent["session_name"] != request.session_name:
        raise WorkerOpsConflict(
            f"stable agent {request.agent_id} belongs to session {agent['session_name']!r}, "
            f"not {request.session_name!r}"
        )
    if agent["role"] == roster.ROLE_SUPERVISOR:
        raise WorkerOpsConflict(
            "a supervisor does not retire its own pane through the worker path; the fleet "
            "lifecycle owns that"
        )

    finalized: Optional[dict[str, Any]] = None
    open_record = occurrence.open_occurrence_for_agent(request.agent_id)
    if open_record is not None:
        finalized = occurrence.finalize_occurrence(
            occurrence.FinalizeRequest(
                task_occurrence_id=open_record["task_occurrence_id"],
                expected_revision=int(open_record["revision"]),
                disposition=request.disposition,
                finalized_by=request.retired_by,
                note=request.reason,
            )
        )

    incarnation = agent.get("current_incarnation") or {}
    terminal_id = incarnation.get("terminal_id")
    generation = incarnation.get("generation")
    retired: Optional[dict[str, Any]] = None
    collected = False
    if terminal_id:
        try:
            retired = roster.retire_incarnation(
                terminal_id=str(terminal_id),
                generation=str(generation) if generation else None,
                reason=request.reason,
            )
        except roster.StableAgentError:
            # Roster bookkeeping never blocks a teardown; the pane collection
            # below is the act that matters and it is still exact.
            retired = None
        collected = bool(
            terminal_service.delete_terminal(
                str(terminal_id),
                expected_generation=str(generation) if generation else None,
                expected_session=request.session_name,
            )
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "session_name": request.session_name,
        "agent_id": request.agent_id,
        "finalized_occurrence": finalized,
        "retired_incarnation": retired,
        "terminal_id": terminal_id,
        "generation": generation,
        "pane_collected": collected,
    }


def _exact_resume_request(
    *, operation_id: str, agent: Mapping[str, Any], session_name: str
) -> oj.OperationRequest:
    incarnation = agent.get("current_incarnation") or {}
    lineage = agent.get("current_lineage") or {}
    contract = rc.get_contract_by_incarnation(
        str(incarnation.get("terminal_id") or ""),
        incarnation.get("generation"),
    )
    if contract is None:
        raise WorkerOpsConflict(
            f"stable agent {agent['agent_id']} has no immutable restore contract for its exact "
            "incarnation; an exact resume has nothing to restore against"
        )
    lifecycle = sl.describe(session_name)
    return oj.OperationRequest(
        operation_id=operation_id,
        session_name=session_name,
        agent_id=str(agent["agent_id"]),
        roster_revision=int(agent["revision"] or 0),
        role=str(agent["role"]),
        profile_family=str(agent["profile_family"]),
        lineage_id=str(lineage.get("lineage_id")),
        harness=str(lineage.get("harness")),
        native_session_id=str(lineage.get("native_session_id")),
        prior_terminal_id=str(incarnation.get("terminal_id")),
        prior_generation=incarnation.get("generation"),
        prior_incarnation_id=str(incarnation.get("incarnation_id")),
        lifecycle_epoch=int(lifecycle.get("epoch") or 0),
        lifecycle_observation=str(lifecycle.get("lifecycle")),
        restore_contract_id=str(contract["contract_id"]),
        restore_contract_digest=str(contract["contract_digest"]),
        restore_contract_schema=rc.SCHEMA_VERSION,
    )


def has_exact_resume_identity(agent: Mapping[str, Any]) -> bool:
    """Whether CAO still holds everything an exact restoration needs."""
    incarnation = agent.get("current_incarnation") or {}
    lineage = agent.get("current_lineage") or {}
    if not incarnation.get("terminal_id") or not incarnation.get("incarnation_id"):
        return False
    if not lineage.get("lineage_id") or not lineage.get("native_session_id"):
        return False
    contract = rc.get_contract_by_incarnation(
        str(incarnation["terminal_id"]), incarnation.get("generation")
    )
    return contract is not None


async def resume_worker(
    session_name: str, agent_id: str, *, recovery_id: str, requested_by: str
) -> dict[str, Any]:
    """Bring one worker back on its own native session, or say why not.

    There is no fresh fallback in here at all. That is deliberate: a caller
    that wants one asks for it through ``recover_lost_pane``, where the seed
    completeness gate lives.
    """
    del requested_by
    agent = _agent(agent_id)
    operation_id = resume_operation_id(recovery_id, agent_id)
    try:
        request = _exact_resume_request(
            operation_id=operation_id, agent=agent, session_name=session_name
        )
    except WorkerOpsError:
        raise
    except oj.OperationJournalError as exc:
        return {
            "outcome": OUTCOME_FAILED,
            "agent_id": agent_id,
            "operation_id": operation_id,
            "detail": str(exc)[: occurrence.MAX_TEXT_LEN],
        }
    try:
        accepted = await exact_executor.execute(request)
    except exact_executor.ExactExecutorReconciliation as exc:
        # M3-B's word for "the physical result was ambiguous". Passing it
        # through as undecided is the only honest translation; calling it
        # failed would license a second restore against a pane that may exist.
        return {
            "outcome": OUTCOME_UNDECIDED,
            "agent_id": agent_id,
            "operation_id": operation_id,
            "detail": str(exc)[: occurrence.MAX_TEXT_LEN],
        }
    except exact_executor.ExactExecutorError as exc:
        return {
            "outcome": OUTCOME_FAILED,
            "agent_id": agent_id,
            "operation_id": operation_id,
            "detail": str(exc)[: occurrence.MAX_TEXT_LEN],
        }
    return {
        "outcome": OUTCOME_EXACT_RESTORED,
        "agent_id": agent_id,
        "operation_id": operation_id,
        "successor_incarnation_id": accepted.get("successor_incarnation_id"),
        "successor_terminal_id": accepted.get("successor_terminal_id"),
        "successor_generation": accepted.get("successor_generation"),
        "detail": "exact same-native-session reincarnation accepted",
    }


@dataclass(frozen=True)
class FreshFallback:
    """What a fresh successor is allowed to be started with.

    All three parts are required, and each closes a different way for a fresh
    worker to start on a story it does not have:

    * ``summary_digest`` binds the successor to *the* supervisor summary
      rather than to whatever summary is lying around;
    * the artifacts are digest-bound, so the successor reads the content the
      record describes and not a file that changed since;
    * ``quality`` is stated by the component that produced the summary,
      because only it can tell truncation from completeness.
    """

    seed: occurrence.TaskSeed
    dispatch_digest: str
    round_index: int

    def __post_init__(self) -> None:
        if not isinstance(self.seed, occurrence.TaskSeed):
            raise WorkerOpsInvalid("seed must be a TaskSeed")
        try:
            occurrence._require_digest(self.dispatch_digest, field_name="dispatch_digest")
        except occurrence.TaskOccurrenceInvalid as exc:
            raise WorkerOpsInvalid(str(exc)) from exc
        if (
            not isinstance(self.round_index, int)
            or isinstance(self.round_index, bool)
            or self.round_index < 0
        ):
            raise WorkerOpsInvalid("round_index must be a non-negative integer")


def _predecessor_occurrence(session_name: str, agent_id: str) -> Optional[dict[str, Any]]:
    """The round the lost pane was running, open or already closed.

    Its recorded ``incarnation_id`` is the durable name of the pane that was
    lost, which is what lets a retry tell "this agent is still on the dead
    pane" from "a previous attempt already launched its replacement".
    """
    records = occurrence.list_occurrences(session_name, agent_id=agent_id)
    return records[-1] if records else None


def admit_fresh_successor(
    session_name: str,
    agent_id: str,
    *,
    recovery_id: str,
    requested_by: str,
) -> dict[str, Any]:
    """Decide, and make it true, that a fresh pane may be created. No effect.

    Admission runs entirely before the launcher, because the alternative is
    what the first cut did: launch, then discover the stable agent still held
    an open round, and raise — leaving a pane nobody points at and a retry that
    makes another one. In the realistic lost-pane case that open round is the
    *normal* state, since the thing that would have closed it is the pane that
    vanished.

    Two facts are established here, in this order:

    * the predecessor round is resolved — finalized ``lost``, which is
      write-once and therefore idempotent;
    * the lost incarnation is retired, so the stable agent is dormant.

    A launch is then legal precisely because the agent has no live incarnation
    and no open round. That is also what makes the retry safe: an agent that is
    *live* at admission time, on an incarnation the predecessor round does not
    name, is one a previous attempt already launched — so it is adopted rather
    than launched again.
    """
    occurrence_id = successor_occurrence_id(recovery_id, agent_id)
    try:
        existing = occurrence.get_occurrence(occurrence_id)
    except occurrence.TaskOccurrenceNotFound:
        existing = None
    except occurrence.TaskOccurrenceError as exc:
        return {
            "mode": ADMIT_REFUSED,
            "task_occurrence_id": occurrence_id,
            "reason": str(exc)[: occurrence.MAX_TEXT_LEN],
        }
    if existing is not None:
        return {
            "mode": ADMIT_ADOPT_OCCURRENCE,
            "task_occurrence_id": occurrence_id,
            "occurrence": existing,
            "reason": "this recovery already opened its successor round",
        }

    agent = _agent(agent_id)
    predecessor = _predecessor_occurrence(session_name, agent_id)
    incarnation = agent.get("current_incarnation") or {}
    live = incarnation.get("disposition") in roster.LIVE_INCARNATION_DISPOSITIONS

    if live and predecessor is not None:
        if predecessor["incarnation_id"] != incarnation.get("incarnation_id"):
            # A previous attempt launched and lost its response before opening
            # the round. Adopting is the only answer that does not leave the
            # first pane orphaned behind a second one.
            return {
                "mode": ADMIT_ADOPT_INCARNATION,
                "task_occurrence_id": occurrence_id,
                "incarnation": incarnation,
                "lineage": agent.get("current_lineage") or {},
                "reason": "a previous attempt already launched this successor",
            }
    elif live and predecessor is None:
        # No round on record, so nothing distinguishes the lost pane from a
        # successor somebody else made. Refusing is the only safe answer: a
        # wrong guess here is either an orphan or a duplicate.
        return {
            "mode": ADMIT_REFUSED,
            "task_occurrence_id": occurrence_id,
            "reason": (
                "this agent has a live incarnation and no round on record, so a fresh launch "
                "cannot be told apart from one that already happened"
            ),
        }

    open_round = occurrence.open_occurrence_for_agent(agent_id)
    if open_round is not None:
        try:
            occurrence.finalize_occurrence(
                occurrence.FinalizeRequest(
                    task_occurrence_id=open_round["task_occurrence_id"],
                    expected_revision=int(open_round["revision"]),
                    disposition=occurrence.DISPOSITION_LOST,
                    finalized_by=requested_by,
                    note=f"pane lost; recovery {recovery_id}",
                )
            )
        except occurrence.TaskOccurrenceError as exc:
            return {
                "mode": ADMIT_REFUSED,
                "task_occurrence_id": occurrence_id,
                "reason": str(exc)[: occurrence.MAX_TEXT_LEN],
            }

    if live and incarnation.get("terminal_id"):
        try:
            roster.retire_incarnation(
                terminal_id=str(incarnation["terminal_id"]),
                generation=(
                    str(incarnation["generation"]) if incarnation.get("generation") else None
                ),
                reason=f"pane lost; recovery {recovery_id}",
            )
        except roster.StableAgentError as exc:
            return {
                "mode": ADMIT_REFUSED,
                "task_occurrence_id": occurrence_id,
                "reason": str(exc)[: occurrence.MAX_TEXT_LEN],
            }

    if occurrence.open_occurrence_for_agent(agent_id) is not None:
        return {
            "mode": ADMIT_REFUSED,
            "task_occurrence_id": occurrence_id,
            "reason": "the agent still holds an open round after admission",
        }
    return {
        "mode": ADMIT_LAUNCH,
        "task_occurrence_id": occurrence_id,
        "reason": "the predecessor round is resolved and the agent holds no live pane",
    }


#: ``(agent, fallback, successor_occurrence_id) -> mapping`` describing the new
#: incarnation. There is no default: launching a fresh worker is a deliberate
#: authority an owner supplies, never something this path reaches for when an
#: exact restore is merely inconvenient.
FreshLauncher = Callable[[Mapping[str, Any], FreshFallback, str], Awaitable[Mapping[str, Any]]]


def plan_lost_pane_recovery(
    session_name: str, agent_id: str, *, fallback: Optional[FreshFallback] = None
) -> dict[str, Any]:
    """Decide what may replace a lost pane. Pure read; performs no effect.

    Exposed separately so an operator surface can show the decision — and, in
    particular, show *why* a worker is going to stay paused — before anything
    happens.
    """
    agent = _agent(agent_id)
    if agent["session_name"] != occurrence._normalise_session_name(session_name):
        raise WorkerOpsConflict(
            f"stable agent {agent_id} belongs to session {agent['session_name']!r}, "
            f"not {session_name!r}"
        )
    open_record = occurrence.open_occurrence_for_agent(agent_id)
    verdict = occurrence.seed_verdict(open_record) if open_record else None
    if has_exact_resume_identity(agent):
        return {
            "mode": MODE_EXACT,
            "agent_id": agent_id,
            "reason": "CAO still holds this worker's native session and restore contract",
            "open_occurrence": open_record,
            "seed_verdict": verdict,
        }
    if fallback is None:
        return {
            "mode": MODE_PAUSED,
            "agent_id": agent_id,
            "reason": (
                "no exact resume identity and no fresh fallback was supplied; a successor "
                "started without one would be guessing at this worker's task"
            ),
            "open_occurrence": open_record,
            "seed_verdict": verdict,
        }
    if not fallback.seed.sufficient_for_fresh_start:
        return {
            "mode": MODE_PAUSED,
            "agent_id": agent_id,
            "reason": (
                f"the supplied seed is {fallback.seed.quality!r}: a fresh successor would "
                "continue from context it only partly has"
            ),
            "open_occurrence": open_record,
            "seed_verdict": verdict,
        }
    return {
        "mode": MODE_FRESH,
        "agent_id": agent_id,
        "reason": (
            "no exact resume identity; a complete supervisor summary and digest-bound "
            "artifacts were supplied"
        ),
        "open_occurrence": open_record,
        "seed_verdict": verdict,
    }


async def recover_lost_pane(
    session_name: str,
    agent_id: str,
    *,
    recovery_id: str,
    requested_by: str,
    fallback: Optional[FreshFallback] = None,
    fresh_launcher: Optional[FreshLauncher] = None,
) -> dict[str, Any]:
    """Replace a lost pane exactly, freshly-with-context, or not at all.

    The third branch is the point of the whole function. A worker whose
    context cannot be reconstructed honestly stays paused for reconciliation:
    nothing is launched, no input is replayed, and the supervisor is left with
    a visible, recoverable gap rather than a worker confidently doing the
    wrong thing.
    """
    plan = plan_lost_pane_recovery(session_name, agent_id, fallback=fallback)
    if plan["mode"] == MODE_EXACT:
        result = await resume_worker(
            session_name, agent_id, recovery_id=recovery_id, requested_by=requested_by
        )
        return {**plan, **result, "mode": MODE_EXACT}
    if plan["mode"] == MODE_PAUSED:
        return {**plan, "outcome": OUTCOME_PAUSED}
    assert fallback is not None  # plan() returns MODE_FRESH only with one
    if fresh_launcher is None:
        return {
            **plan,
            "mode": MODE_PAUSED,
            "outcome": OUTCOME_PAUSED,
            "reason": (
                "a complete seed was supplied but this build has no fresh-launch authority; "
                "the worker stays paused rather than being reconstructed"
            ),
        }

    # Admission first, and it is the whole repair: nothing physical happens
    # until the predecessor round is resolved and the agent provably holds no
    # live pane. A launch that ran first and *then* discovered an open round
    # would leave a pane nobody points at, and a retry would make another.
    admission = admit_fresh_successor(
        session_name, agent_id, recovery_id=recovery_id, requested_by=requested_by
    )
    if admission["mode"] == ADMIT_REFUSED:
        return {
            **plan,
            "mode": MODE_PAUSED,
            "outcome": OUTCOME_PAUSED,
            "reason": f"the successor could not be admitted: {admission['reason']}",
        }
    occurrence_id = admission["task_occurrence_id"]
    if admission["mode"] == ADMIT_ADOPT_OCCURRENCE:
        return _fresh_result(plan, fallback, admission["occurrence"])

    if admission["mode"] == ADMIT_ADOPT_INCARNATION:
        launched: Mapping[str, Any] = {
            "incarnation_id": admission["incarnation"].get("incarnation_id"),
            "terminal_id": admission["incarnation"].get("terminal_id"),
            "generation": admission["incarnation"].get("generation"),
            "lineage_id": (admission.get("lineage") or {}).get("lineage_id"),
            "native_session_id": (admission.get("lineage") or {}).get("native_session_id"),
        }
    else:
        agent = _agent(agent_id)
        try:
            launched = await fresh_launcher(agent, fallback, occurrence_id)
        except Exception as exc:  # noqa: BLE001 - an ambiguous launch is undecided
            return {
                **plan,
                "outcome": OUTCOME_UNDECIDED,
                "detail": str(exc)[: occurrence.MAX_TEXT_LEN],
            }
        if not launched or not launched.get("incarnation_id"):
            return {
                **plan,
                "outcome": OUTCOME_FAILED,
                "detail": "the fresh launcher returned no incarnation",
            }

    # The successor gets a *new* occurrence carrying the seed explicitly,
    # including its completeness. A fresh worker is a new round by definition:
    # reusing the predecessor's occurrence would let a restarted worker inherit
    # a round somebody else already reported on.
    try:
        opened = occurrence.open_occurrence(
            occurrence.OpenRequest(
                task_occurrence_id=occurrence_id,
                session_name=session_name,
                agent_id=agent_id,
                round_index=fallback.round_index,
                dispatch_digest=fallback.dispatch_digest,
                incarnation=occurrence.EffectIncarnation(
                    incarnation_id=str(launched["incarnation_id"]),
                    terminal_id=str(launched.get("terminal_id") or ""),
                    generation=launched.get("generation"),
                    lineage_id=launched.get("lineage_id"),
                    native_session_id=launched.get("native_session_id"),
                ),
                dispatch_provenance={
                    "recovery_id": recovery_id,
                    "requested_by": requested_by,
                    "mode": MODE_FRESH,
                    "seed_quality": fallback.seed.quality,
                },
                seed=fallback.seed,
            )
        )
    except occurrence.TaskOccurrenceError as exc:
        # The pane exists and its round does not. Undecided rather than
        # failed: a retry adopts that pane through admission instead of
        # launching a second one.
        return {
            **plan,
            "outcome": OUTCOME_UNDECIDED,
            "successor_incarnation_id": launched["incarnation_id"],
            "successor_terminal_id": launched.get("terminal_id"),
            "detail": str(exc)[: occurrence.MAX_TEXT_LEN],
        }
    return _fresh_result(plan, fallback, opened)


def _fresh_result(
    plan: Mapping[str, Any], fallback: "FreshFallback", opened: Mapping[str, Any]
) -> dict[str, Any]:
    """One shape for a fresh fallback, whether it was launched or adopted."""
    return {
        **plan,
        "outcome": OUTCOME_FRESH_FALLBACK,
        "successor_incarnation_id": opened["incarnation_id"],
        "successor_terminal_id": opened["terminal_id"],
        "successor_generation": opened["generation"],
        "task_occurrence_id": opened["task_occurrence_id"],
        "seed_quality": fallback.seed.quality,
        "seed_summary_digest": fallback.seed.summary_digest,
        "seed_artifact_digest": fallback.seed.artifact_seed_digest,
    }


def recover_lost_pane_sync(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Blocking wrapper for callers that are not already async."""
    return asyncio.run(recover_lost_pane(*args, **kwargs))


__all__ = [
    "FreshFallback",
    "MODE_EXACT",
    "MODE_FRESH",
    "MODE_PAUSED",
    "OUTCOME_EXACT_RESTORED",
    "OUTCOME_FAILED",
    "OUTCOME_FRESH_FALLBACK",
    "OUTCOME_PAUSED",
    "OUTCOME_UNDECIDED",
    "ADMIT_ADOPT_INCARNATION",
    "ADMIT_ADOPT_OCCURRENCE",
    "ADMIT_LAUNCH",
    "ADMIT_REFUSED",
    "RetireRequest",
    "WorkerOpsConflict",
    "WorkerOpsError",
    "WorkerOpsInvalid",
    "WorkerOpsNotFound",
    "admit_fresh_successor",
    "has_exact_resume_identity",
    "plan_lost_pane_recovery",
    "recover_lost_pane",
    "recover_lost_pane_sync",
    "resume_operation_id",
    "resume_worker",
    "retire_worker_pane",
    "successor_occurrence_id",
]
