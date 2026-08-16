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
_TRANSITION_NAMESPACE = uuid.UUID("03790000-c400-4c40-a7e3-000000000003")

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
    """Who is resuming what. Every transition identity is derived, not passed.

    An earlier shape had the caller mint a restore/commit/reconcile id per
    request. That put a correctness rule — "an attempt may not reuse a
    transition id a previous attempt already consumed" — in the caller's
    hands, and the caller got it wrong: a retry re-derived the *same* restore
    id from the operation id, whose payload differed from the one already
    stored, so every retry died on "already exists with different immutable
    request content" and the only way out of `reconciliation-required` was to
    force-Stop the session and resume it again.

    Deriving each id from ``(operation_id, label, state_epoch)`` makes the rule
    structural instead. Each attempt starts at a distinct epoch, so it gets
    distinct ids; a replay of the *same* attempt recomputes identical ids and
    adopts.
    """

    operation_id: str
    actor: str
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


def attempt_transition_id(resume_operation_id: str, label: str, state_epoch: int) -> str:
    """The transition id one attempt uses for one step.

    Keyed by the state epoch the attempt starts from, which is exactly what
    distinguishes attempts: entering `restoring` bumps the epoch, so the next
    reconcile-then-retry cycle derives a fresh set. A lost response replays
    the same epoch and therefore the same ids, which the journal adopts.
    """
    return str(uuid.uuid5(_TRANSITION_NAMESPACE, f"{resume_operation_id}:{label}:{state_epoch}"))


def retry_receipt(operation: Mapping[str, Any]) -> str:
    """The opaque receipt an operator retry carries into the journal.

    Derived from the durable evidence the operator is acting on — the state
    epoch and every member's recorded outcome — rather than minted, so it is
    reproducible: a retry whose response was lost recomputes the identical
    digest and adopts its own transition instead of being refused as a
    different request. It is a reference to evidence, never a summary of it;
    what the evidence *means* stays M3-D's.
    """
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "effect": "resume-retry",
            "operation_id": operation["operation_id"],
            "state_epoch": int(operation["state_epoch"]),
            "members": sorted(
                (
                    {
                        "agent_id": member["agent_id"],
                        "final_state": member["final_state"],
                        "result_revision": int(member["result_revision"]),
                    }
                    for member in operation.get("members", ())
                ),
                key=lambda item: str(item["agent_id"]),
            ),
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
    """The M3-D seam, resolved at call time rather than imported at module load.

    M3-D owns what a resumption bump *says* and what the supervisor does with
    it; this module owns only that there is exactly one and that it comes last.
    The delegation is deliberately late and lazy: M3-D imports this module for
    its ``SupervisorWaker``/``SupervisorWake`` contract, so binding it here at
    import would be a cycle, and resolving it per call is what lets an owner
    (or a test) replace this default and actually have it replaced.

    A build without the M3-D authority keeps the original honest answer — the
    fleet is back and nobody has been told, which is a reconciliation rather
    than a settled Resume.
    """
    try:
        from cli_agent_orchestrator.services import supervisor_reconciliation
    except ImportError:  # pragma: no cover - the module ships with this build
        return SupervisorWake(
            False,
            detail="no supervisor reconciliation authority is available in this build",
        )
    return await supervisor_reconciliation.make_waker()(operation, results, wake)


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


def _begin_restore(request: ResumeRequest, operation: Mapping[str, Any]) -> dict[str, Any]:
    """First attempt: release the Stop barrier and declare the target."""
    return cast(
        dict[str, Any],
        cohort.begin_resume_restore(
            cohort.ResumeRestoreRequest(
                transition_id=attempt_transition_id(
                    request.operation_id, "restore", int(operation["state_epoch"])
                ),
                operation_id=request.operation_id,
                expected_state_epoch=int(operation["state_epoch"]),
                actor=request.actor,
                reason=request.reason,
            )
        )["operation"],
    )


def _begin_retry(request: ResumeRequest, operation: Mapping[str, Any]) -> dict[str, Any]:
    """A later attempt at the *same* operation, out of reconciliation.

    Deliberately not a second Resume. The barrier is already open, the
    lifecycle already declared, and the source cohort's membership already
    fixed — none of that is touched again. Only members without a decided
    outcome are re-attempted, so a retry replays no input into a pane that
    already came back.
    """
    epoch = int(operation["state_epoch"])
    return cast(
        dict[str, Any],
        cohort.transition_operation(
            cohort.TransitionRequest(
                transition_id=attempt_transition_id(request.operation_id, "retry", epoch),
                operation_id=request.operation_id,
                expected_state_epoch=epoch,
                to_state=cohort.STATE_RESTORING,
                actor=request.actor,
                reason=request.reason or "operator retry after reconciliation",
                receipt_digest=retry_receipt(operation),
            )
        )["operation"],
    )


def _reconcile(
    request: ResumeRequest, operation: Mapping[str, Any], receipt: str, reason: str
) -> dict[str, Any]:
    epoch = int(operation["state_epoch"])
    transitioned = cohort.transition_operation(
        cohort.TransitionRequest(
            transition_id=attempt_transition_id(request.operation_id, "reconcile", epoch),
            operation_id=request.operation_id,
            expected_state_epoch=epoch,
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
    retry: bool = False,
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

    state = operation["state"]
    if state == cohort.STATE_SETTLED:
        # Response-loss replay of a finished Resume. Returning the durable
        # answer is what stops a retry becoming a second wake.
        return operation
    if state == cohort.STATE_RECONCILIATION_REQUIRED:
        if not retry:
            # Idempotent adoption. A re-sent Resume is a caller asking "did
            # mine land?", and the honest answer is the durable result — not a
            # silent second attempt the operator never asked for. Continuing
            # is an explicit, separately authenticated act.
            return operation
        operation = _begin_retry(request, operation)
    elif state == cohort.STATE_PREPARING:
        if retry:
            raise CohortResumeConflict(
                f"cohort operation {request.operation_id} has not been attempted yet; there is "
                "nothing to retry"
            )
        operation = _begin_restore(request, operation)
    elif state == cohort.STATE_RESTORING:
        # A previous attempt died mid-restore. Continue it rather than
        # starting a second operation.
        if retry:
            raise CohortResumeConflict(
                f"cohort operation {request.operation_id} is already restoring; retry applies "
                f"only to {cohort.STATE_RECONCILIATION_REQUIRED!r}"
            )
    else:
        raise CohortResumeConflict(f"Resume cannot execute from state {state!r}")

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
            transition_id=attempt_transition_id(
                request.operation_id, "commit", int(current["state_epoch"])
            ),
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


async def execute_resume_retry(
    request: ResumeRequest,
    *,
    restorer: Optional[MemberRestorer] = None,
    waker: Optional[SupervisorWaker] = None,
) -> dict[str, Any]:
    """Continue *this* Resume out of ``reconciliation-required``.

    The recovery path for the two ways a Resume stops short: a member whose
    physical result was ambiguous, and a wake that did not land. Both leave
    real state behind — panes that came back, a lifecycle already declared, a
    barrier already released — so the repair is to finish the operation, not
    to start a second one.

    What it deliberately does not do: claim a new boundary, re-observe the
    roster, re-release the barrier, or re-restore a member that already has a
    decided outcome. The wake id is the operation's, unchanged, so a wake that
    was delivered before is adopted rather than repeated.

    The target decides whether there is a wake at all, exactly as it does on
    the first attempt: a Resume-paused retry stays strictly zero input.
    """
    operation = _operation(request.operation_id)
    target = operation.get("resume_target")
    if target == sl.PAUSED:
        return await _execute(
            request,
            expected_target=frozenset({sl.PAUSED}),
            surface="resume-paused",
            restorer=restorer or _default_restorer,
            waker=None,
            retry=True,
        )
    return await _execute(
        request,
        expected_target=frozenset({sl.WORKING, sl.COMPLETE}),
        surface="resume-and-start",
        restorer=restorer or _default_restorer,
        waker=waker or _default_waker,
        retry=True,
    )
