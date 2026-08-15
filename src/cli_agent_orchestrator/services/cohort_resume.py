"""Operator-only Resume of a stopped cohort (cond-0379 M3-C C4).

Two surfaces, deliberately not one call with a flag:

``execute_resume_paused``
    Bring the fleet's panes back and stop there. **Strict zero input**: no
    control byte, no inbox payload, no supervisor wake, no task admission.
    The session lands in ``paused``, which is the honest description of a
    fleet that exists and is doing nothing. An operator who wants it working
    asks for that separately, and that ask is a different, louder button.

``execute_resume_and_start``
    The same restore, followed by **exactly one** durable, opaque supervisor
    reconciliation wake — issued only after every member's restore outcome is
    durably recorded, so the supervisor's first observation of its fleet is
    complete rather than half-written.

What this module does *not* own. The wake's meaning, content, and the
reconciliation the supervisor then performs are M3-D's; M3-C mints one opaque
wake id per Resume operation and hands it to a seam, never composing a summary
or inferring a task occurrence from CAO's physical roster. The exact
same-native-session restore itself is M3-B's ``exact_executor``; this module
chooses *which* members to restore, in what order, and how their bounded
outcomes are recorded.

The member outcomes are the whole point of the durable record, and **all four
are terminal**:

``restored-exact``
    M3-B brought the stable agent back on its own native session.
``restored-fresh``
    An explicit, proven fresh-restore seam produced a new incarnation for the
    same stable agent. There is no silent fallback to this: exact continuity
    is never downgraded because an exact restore was merely inconvenient, and
    no fresh authority ships in this slice, so it appears only when an owner
    supplies one deliberately.
``failed``
    CAO had a resume identity for this member and could not use it.
``unresumable``
    The boundary recorded no resume identity at all — the worker the operator
    was shown and acknowledged on the stop-impact confirmation.

A partial restore therefore **settles**. One failed member does not hold its
restored siblings, or the supervisor, in limbo: they come back and start, and
the single reconciliation wake carries every outcome so the supervisor learns
what it lost from an authoritative record rather than by noticing a silence.

``reconciliation-required`` is reserved for outcomes that are genuinely
undecided — a member still pending, or a restore whose result this process
could not determine.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping, Optional, Sequence, cast

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import exact_executor
from cli_agent_orchestrator.services import operation_journal as oj
from cli_agent_orchestrator.services import restore_contract as rc
from cli_agent_orchestrator.services import session_lifecycle as sl
from cli_agent_orchestrator.services import stable_agent_roster as roster

SCHEMA_VERSION = "cao-m3-cohort-resume-v1"

#: Deterministic namespaces. A retry of the same Resume mints the same member
#: operation ids and the same wake id, which is what makes "exactly one" hold
#: across a lost response rather than only within one process.
_MEMBER_NAMESPACE = uuid.UUID("03790000-c400-4c40-a7e3-000000000001")
_WAKE_NAMESPACE = uuid.UUID("03790000-c400-4c40-a7e3-000000000002")

OUTCOME_EXACT = "exact"
OUTCOME_FRESH = "fresh"
OUTCOME_FAILED = "failed"
OUTCOME_UNRESUMABLE = "unresumable"
#: Not a restore outcome but an *absence* of one: the restorer could not
#: determine what happened to this member. It is the only thing that blocks a
#: terminal commit, because it is the only thing that is not yet a fact.
OUTCOME_UNDECIDED = "undecided"
RESTORE_OUTCOMES = frozenset(
    {OUTCOME_EXACT, OUTCOME_FRESH, OUTCOME_FAILED, OUTCOME_UNRESUMABLE, OUTCOME_UNDECIDED}
)
TERMINAL_OUTCOMES = frozenset({OUTCOME_EXACT, OUTCOME_FRESH, OUTCOME_FAILED, OUTCOME_UNRESUMABLE})

_FINAL_FOR_OUTCOME = {
    OUTCOME_EXACT: cohort.FINAL_RESTORED_EXACT,
    OUTCOME_FRESH: cohort.FINAL_RESTORED_FRESH,
    OUTCOME_FAILED: cohort.FINAL_FAILED,
    OUTCOME_UNRESUMABLE: cohort.FINAL_UNRESUMABLE,
    OUTCOME_UNDECIDED: cohort.FINAL_RECONCILIATION_REQUIRED,
}


class CohortResumeError(RuntimeError):
    code = "cohort-resume-error"


class CohortResumeInvalid(CohortResumeError):
    code = "cohort-resume-invalid"


class CohortResumeConflict(CohortResumeError):
    code = "cohort-resume-conflict"


@dataclass(frozen=True)
class MemberRestore:
    """One member's bounded restore outcome.

    ``evidence`` carries only references and digests — terminal/generation/
    incarnation ids — never provider output, task text, or environment values.
    """

    outcome: str
    detail: str = ""
    incarnation_id: Optional[str] = None
    terminal_id: Optional[str] = None
    generation: Optional[str] = None

    def __post_init__(self) -> None:
        if self.outcome not in RESTORE_OUTCOMES:
            raise CohortResumeInvalid(
                f"restore outcome must be one of {sorted(RESTORE_OUTCOMES)}; got {self.outcome!r}"
            )


@dataclass(frozen=True)
class SupervisorWake:
    """The opaque result of one supervisor reconciliation wake.

    M3-C never inspects ``receipt_digest``. It records it as provenance and
    treats ``delivered`` as the only thing it is entitled to branch on: a wake
    that did not land means the fleet is restored but nobody has been told, and
    that is a reconciliation, not a settled campaign.
    """

    delivered: bool
    receipt_digest: Optional[str] = None
    detail: str = ""


@dataclass(frozen=True)
class ResumeRequest:
    operation_id: str
    expected_state_epoch: int
    restore_transition_id: str
    commit_transition_id: str
    reconciliation_transition_id: str
    actor: str
    retry_receipt_digest: Optional[str] = None
    reason: Optional[str] = None


#: ``(member_snapshot, resume_operation) -> MemberRestore``
MemberRestorer = Callable[[Mapping[str, Any], Mapping[str, Any]], Awaitable[MemberRestore]]
#: ``(resume_operation, member_results, wake_id) -> SupervisorWake``
SupervisorWaker = Callable[
    [Mapping[str, Any], Sequence[Mapping[str, Any]], str], Awaitable[SupervisorWake]
]


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _receipt(operation_id: str, label: str, results: Sequence[Mapping[str, Any]]) -> str:
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "operation_id": operation_id,
            "effect": label,
            "results": list(results),
        }
    )


def member_operation_id(resume_operation_id: str, agent_id: str) -> str:
    """The exact M3-B operation id this Resume uses for one member.

    Derived rather than minted so a retried Resume adopts the reincarnation
    the previous attempt already claimed, instead of racing a second one for
    the same stable agent.
    """
    return str(uuid.uuid5(_MEMBER_NAMESPACE, f"{resume_operation_id}:{agent_id}"))


def wake_id(resume_operation_id: str) -> str:
    """The one opaque supervisor-reconciliation wake id for this Resume.

    One Resume operation has exactly one wake id, for its whole life. The
    delivering seam is required to be idempotent under it — the same contract
    ``control_input_service`` already gives a control id — so a wake that was
    delivered and then lost its response is adopted rather than repeated.
    """
    return str(uuid.uuid5(_WAKE_NAMESPACE, resume_operation_id))


def _operation(operation_id: str) -> dict[str, Any]:
    try:
        return cohort.get_operation(operation_id)
    except cohort.CohortJournalError as exc:
        raise CohortResumeConflict(str(exc)) from exc


def _included_members(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = [dict(member) for member in operation.get("members", ()) if member["included"]]
    # Supervisor first, deliberately the reverse of Pause/Stop. A supervisor
    # that is already back when its workers appear can attribute them; workers
    # that come back first have nobody to report to.
    return sorted(
        members,
        key=lambda member: (member.get("role") != roster.ROLE_SUPERVISOR, member["agent_id"]),
    )


def _has_resume_identity(member: Mapping[str, Any]) -> bool:
    return all(
        member.get(field)
        for field in (
            "lineage_id",
            "harness",
            "native_session_id",
            "incarnation_id",
            "terminal_id",
            "restore_contract_id",
            "restore_contract_digest",
        )
    )


async def _default_restorer(
    member: Mapping[str, Any], operation: Mapping[str, Any]
) -> MemberRestore:
    """Restore one member through the exact M3-B same-native-session path.

    The dark default performs no fresh relaunch. A member M3-B refuses is
    ``failed`` rather than quietly downgraded to a new native session: exact
    restoration is what makes the resumed transcript the same transcript, and
    silently starting a fresh one would be the corruption this whole slice is
    arranged to avoid. Fresh restore is an explicit, separately supplied
    authority, never a fallback this path reaches for on its own.

    The one outcome that is not a fact is M3-B's own
    ``reconciliation-required``: that is its word for "the physical result was
    ambiguous", and passing it through as ``undecided`` is the only honest
    translation. A typed refusal, by contrast, proves no effect happened and
    is a settled ``failed``.
    """
    if not _has_resume_identity(member):
        return MemberRestore(
            OUTCOME_UNRESUMABLE,
            "the boundary recorded no native session, incarnation, and restore contract "
            "for this member",
        )
    try:
        request = oj.OperationRequest(
            operation_id=member_operation_id(operation["operation_id"], member["agent_id"]),
            session_name=operation["session_name"],
            agent_id=member["agent_id"],
            roster_revision=int(member["agent_revision"]),
            role=member["role"],
            profile_family=member["profile_family"],
            lineage_id=member["lineage_id"],
            harness=member["harness"],
            native_session_id=member["native_session_id"],
            prior_terminal_id=member["terminal_id"],
            prior_generation=member["generation"],
            prior_incarnation_id=member["incarnation_id"],
            # The lifecycle ``begin_resume_restore`` just declared, not the
            # stopped boundary the cohort claimed: M3-B binds the live row.
            lifecycle_epoch=int(operation["lifecycle_epoch"]) + 1,
            lifecycle_observation=str(operation["resume_target"]),
            restore_contract_id=member["restore_contract_id"],
            restore_contract_digest=member["restore_contract_digest"],
            restore_contract_schema=rc.SCHEMA_VERSION,
        )
    except oj.OperationJournalError as exc:
        return MemberRestore(OUTCOME_FAILED, str(exc)[: cohort.MAX_TEXT_LEN])
    try:
        accepted = await exact_executor.execute(request)
    except exact_executor.ExactExecutorReconciliation as exc:
        return MemberRestore(OUTCOME_UNDECIDED, str(exc)[: cohort.MAX_TEXT_LEN])
    except exact_executor.ExactExecutorError as exc:
        return MemberRestore(OUTCOME_FAILED, str(exc)[: cohort.MAX_TEXT_LEN])
    return MemberRestore(
        OUTCOME_EXACT,
        "exact same-native-session reincarnation accepted",
        incarnation_id=accepted.get("successor_incarnation_id"),
        terminal_id=accepted.get("successor_terminal_id"),
        generation=accepted.get("successor_generation"),
    )


async def _default_waker(
    operation: Mapping[str, Any], results: Sequence[Mapping[str, Any]], wake: str
) -> SupervisorWake:
    """Dark M3-D seam: this fork has no supervisor-reconciliation authority.

    M3-D owns what a resumption bump *says* and what the supervisor does with
    it. Until it supplies the deliverer, the truthful answer is that the fleet
    is back and nobody has been told — which is a reconciliation, not a
    settled Resume, and is reported as exactly that.
    """
    del operation, results, wake
    return SupervisorWake(
        False,
        detail="no supervisor reconciliation authority is available in this build",
    )


def _record(operation_id: str, member: Mapping[str, Any], restore: MemberRestore) -> dict[str, Any]:
    detail = restore.detail or None
    evidence = {
        key: value
        for key, value in (
            ("incarnation_id", restore.incarnation_id),
            ("terminal_id", restore.terminal_id),
            ("generation", restore.generation),
        )
        if value
    }
    if evidence:
        suffix = " " + json.dumps(evidence, sort_keys=True, separators=(",", ":"))
        detail = f"{detail or ''}{suffix}".strip()
    return cohort.record_member_result(
        cohort.MemberResult(
            operation_id=operation_id,
            agent_id=member["agent_id"],
            expected_result_revision=int(member["result_revision"]),
            final_state=_FINAL_FOR_OUTCOME[restore.outcome],
            background_command_loss_risk=cohort.LOSS_NONE,
            task_occurrence_id=member.get("task_occurrence_id"),
            boundary_digest=member.get("boundary_digest"),
            report_digest=member.get("report_digest"),
            checkpoint_digest=member.get("checkpoint_digest"),
            result_detail=(detail or None) if detail else None,
        )
    )


def _enter_restoring(request: ResumeRequest, operation: Mapping[str, Any]) -> dict[str, Any]:
    state = operation["state"]
    if state == cohort.STATE_PREPARING:
        return cast(
            dict[str, Any],
            cohort.begin_resume_restore(
                cohort.ResumeRestoreRequest(
                    transition_id=request.restore_transition_id,
                    operation_id=request.operation_id,
                    expected_state_epoch=request.expected_state_epoch,
                    actor=request.actor,
                    reason=request.reason,
                )
            )["operation"],
        )
    if state == cohort.STATE_RECONCILIATION_REQUIRED:
        if request.retry_receipt_digest is None:
            raise CohortResumeConflict(
                "an explicit Resume retry out of reconciliation-required needs a receipt digest "
                "naming the evidence the operator acted on"
            )
        return cast(
            dict[str, Any],
            cohort.transition_operation(
                cohort.TransitionRequest(
                    transition_id=request.restore_transition_id,
                    operation_id=request.operation_id,
                    expected_state_epoch=request.expected_state_epoch,
                    to_state=cohort.STATE_RESTORING,
                    actor=request.actor,
                    reason=request.reason,
                    receipt_digest=request.retry_receipt_digest,
                )
            )["operation"],
        )
    if state == cohort.STATE_RESTORING:
        return dict(operation)
    raise CohortResumeConflict(f"Resume cannot execute from state {state!r}")


def _reconcile(
    request: ResumeRequest, operation: Mapping[str, Any], receipt: str, reason: str
) -> dict[str, Any]:
    transitioned = cohort.transition_operation(
        cohort.TransitionRequest(
            transition_id=request.reconciliation_transition_id,
            operation_id=request.operation_id,
            expected_state_epoch=int(operation["state_epoch"]),
            to_state=cohort.STATE_RECONCILIATION_REQUIRED,
            actor=request.actor,
            reason=reason,
            receipt_digest=receipt,
        )
    )
    return cast(dict[str, Any], transitioned["operation"])


async def _execute(
    request: ResumeRequest,
    *,
    expected_target: frozenset[str],
    surface: str,
    restorer: MemberRestorer,
    waker: Optional[SupervisorWaker],
) -> dict[str, Any]:
    if not isinstance(request, ResumeRequest):
        raise CohortResumeInvalid(f"request must be a ResumeRequest; got {type(request).__name__}")
    operation = _operation(request.operation_id)
    if operation["operation_kind"] != cohort.KIND_RESUME:
        raise CohortResumeConflict(f"{surface} requires a Resume operation")
    if operation["initiator_kind"] != cohort.INITIATOR_OPERATOR:
        raise CohortResumeConflict("only an operator may resume a stopped campaign")
    if operation["resume_target"] not in expected_target:
        raise CohortResumeConflict(
            f"{surface} restores to {sorted(expected_target)}; this operation targets "
            f"{operation['resume_target']!r}"
        )
    if operation["state"] == cohort.STATE_SETTLED:
        # Response-loss replay of a finished Resume. Returning the durable
        # answer is what stops a retry becoming a second wake.
        return operation
    if operation["state"] == cohort.STATE_RECONCILIATION_REQUIRED and int(
        operation["state_epoch"]
    ) != int(request.expected_state_epoch):
        # The caller is replaying the request that already produced this
        # terminal-for-that-attempt answer. An explicit operator retry uses
        # the current epoch, fresh transition ids, and a receipt.
        return operation

    _enter_restoring(request, operation)

    results: list[dict[str, Any]] = []
    undecided = False
    for member in _included_members(_operation(request.operation_id)):
        if member["final_state"] in cohort.RESUME_TERMINAL_MEMBER_STATES:
            results.append({"agent_id": member["agent_id"], "final_state": member["final_state"]})
            continue
        try:
            restore = await restorer(member, _operation(request.operation_id))
        except Exception as exc:  # noqa: BLE001 - one member never stops its siblings
            # An unexpected raise is not a proven failure. The restorer may
            # have created a pane before it died, so the only honest record is
            # that this member's outcome is not known.
            restore = MemberRestore(OUTCOME_UNDECIDED, str(exc)[: cohort.MAX_TEXT_LEN])
        if restore.outcome not in TERMINAL_OUTCOMES:
            undecided = True
        recorded = _record(request.operation_id, member, restore)
        results.append(
            {
                "agent_id": member["agent_id"],
                "final_state": recorded["final_state"],
                "result_revision": recorded["result_revision"],
            }
        )

    current = _operation(request.operation_id)
    if undecided:
        # Only an *undecided* member blocks the commit. A settled failure does
        # not: its siblings and supervisor are restored and must be allowed to
        # start, and the wake below tells the supervisor exactly what it lost.
        return _reconcile(
            request,
            current,
            _receipt(request.operation_id, f"{surface}-members", results),
            "one or more cohort members have no decided restore outcome",
        )

    receipt = _receipt(request.operation_id, surface, results)
    if waker is not None:
        # Exactly one wake, and only here: after every member outcome is
        # durable, so the supervisor never reconciles against a half-written
        # fleet, and before the terminal commit, so a fleet is never declared
        # settled with nobody told.
        identifier = wake_id(request.operation_id)
        try:
            wake = await waker(current, results, identifier)
        except Exception as exc:  # noqa: BLE001 - an undelivered wake reconciles
            wake = SupervisorWake(False, detail=str(exc)[: cohort.MAX_TEXT_LEN])
        if not wake.delivered:
            return _reconcile(
                request,
                current,
                _receipt(
                    request.operation_id,
                    f"{surface}-wake",
                    [*results, {"wake_id": identifier, "delivered": False, "detail": wake.detail}],
                ),
                "the fleet was restored but its supervisor reconciliation wake did not land",
            )
        receipt = wake.receipt_digest or _receipt(
            request.operation_id,
            f"{surface}-wake",
            [*results, {"wake_id": identifier, "delivered": True}],
        )

    committed = cohort.commit_terminal(
        cohort.TerminalCommitRequest(
            transition_id=request.commit_transition_id,
            operation_id=request.operation_id,
            expected_state_epoch=int(current["state_epoch"]),
            actor=request.actor,
            receipt_digest=receipt,
            reason=request.reason,
        )
    )
    return cast(dict[str, Any], committed["operation"])


async def execute_resume_paused(
    request: ResumeRequest, *, restorer: Optional[MemberRestorer] = None
) -> dict[str, Any]:
    """Restore the stopped cohort's panes and stop. Strict zero input.

    There is no waker parameter, because there is no wake: a paused fleet is
    one an operator can look at before anything starts moving, and the value
    of that is destroyed by a single byte typed on the operator's behalf.
    """
    return await _execute(
        request,
        expected_target=frozenset({sl.PAUSED}),
        surface="resume-paused",
        restorer=restorer or _default_restorer,
        waker=None,
    )


async def execute_resume_and_start(
    request: ResumeRequest,
    *,
    restorer: Optional[MemberRestorer] = None,
    waker: Optional[SupervisorWaker] = None,
) -> dict[str, Any]:
    """Restore the stopped cohort, then wake its supervisor exactly once.

    The seams resolve at call time rather than being captured as parameter
    defaults, so an owner (or a test) that replaces the module's default
    restorer/waker actually replaces it. A default captured at import is one
    a monkeypatch appears to change and does not, which is how a test ends up
    passing against the implementation it meant to stub out.
    """
    return await _execute(
        request,
        expected_target=frozenset({sl.WORKING, sl.COMPLETE}),
        surface="resume-and-start",
        restorer=restorer or _default_restorer,
        waker=waker or _default_waker,
    )
