"""Dark physical Pause/Stop executor for one M3-C cohort operation.

The durable cohort journal is the authority for *what* operation won.  This
module performs only the corresponding fork-owned physical effects and writes
their bounded outcomes back to that same journal.  It deliberately has no API,
CLI, UI, conductor, task-occurrence, or Resume surface.

Every external effect is preceded by a durable cohort transition.  Retries use
the same control id or exact terminal generation and therefore adopt prior
progress after response loss instead of issuing a second logical effect.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Sequence, cast

from cli_agent_orchestrator.services import cohort_journal as cohort
from cli_agent_orchestrator.services import (
    control_input_service,
    operation_journal,
    provider_controls,
)
from cli_agent_orchestrator.services import stable_agent_roster as roster
from cli_agent_orchestrator.services import (
    terminal_service,
)
from cli_agent_orchestrator.services.control_input_contract import (
    ACCEPTED,
    CONTROL_INPUT_PROTOCOL,
)

SCHEMA_VERSION = "cao-m3-cohort-effects-v1"

OBS_ACTIVE = "active"
OBS_IDLE = "idle"
OBS_PARKED = "parked"
OBS_INTERRUPTED = "interrupted"
OBS_EXITED = "exited"
OBS_UNKNOWN = "unknown"
OBSERVATION_STATES = frozenset(
    {OBS_ACTIVE, OBS_IDLE, OBS_PARKED, OBS_INTERRUPTED, OBS_EXITED, OBS_UNKNOWN}
)


class CohortEffectsError(RuntimeError):
    code = "cohort-effects-error"


class CohortEffectsInvalid(CohortEffectsError):
    code = "cohort-effects-invalid"


class CohortEffectsConflict(CohortEffectsError):
    code = "cohort-effects-conflict"


@dataclass(frozen=True)
class PauseObservation:
    """Exact post/pre-interrupt work observation supplied by the fork seam.

    ``tracked_children_clear`` is intentionally tri-state.  ``None`` means the
    caller could not prove whether known task children survived.  That can
    never terminalize Force Pause, even when the TUI reports an aborted turn
    (the Codex 0.147 canary left its child alive in exactly that sequence).
    """

    state: str
    tracked_children_clear: Optional[bool]
    detail: str = ""
    evidence_digest: Optional[str] = None

    def __post_init__(self) -> None:
        if self.state not in OBSERVATION_STATES:
            raise CohortEffectsInvalid(
                f"pause observation state must be one of {sorted(OBSERVATION_STATES)}"
            )
        if self.tracked_children_clear not in {True, False, None}:
            raise CohortEffectsInvalid("tracked_children_clear must be true, false, or null")


@dataclass(frozen=True)
class SafePauseRequest:
    operation_id: str
    commit_transition_id: str
    actor: str
    drain_receipt_digest: str
    member_results: tuple[cohort.MemberResult, ...]
    reason: Optional[str] = None


@dataclass(frozen=True)
class ForcePauseRequest:
    operation_id: str
    expected_state_epoch: int
    interrupt_transition_id: str
    commit_transition_id: str
    reconciliation_transition_id: str
    actor: str
    reason: Optional[str] = None


@dataclass(frozen=True)
class StopEffectsRequest:
    operation_id: str
    expected_state_epoch: int
    teardown_transition_id: str
    commit_transition_id: str
    reconciliation_transition_id: str
    actor: str
    drain_receipt_digest: Optional[str] = None
    promote_to_force: bool = False
    reason: Optional[str] = None


PauseObserver = Callable[[Mapping[str, Any], str], PauseObservation]
InterruptDeliverer = Callable[
    [Mapping[str, Any], Sequence[Mapping[str, Any]], str],
    control_input_service.ControlInputResult,
]
TerminalReaper = Callable[[Mapping[str, Any]], bool]
SafeOperationDrainer = Callable[[Mapping[str, Any]], bool]
WaitInterruptor = Callable[[str, str], Sequence[Mapping[str, Any]]]


def _digest(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _operation(operation_id: str) -> dict[str, Any]:
    try:
        return cohort.get_operation(operation_id)
    except cohort.CohortJournalError as exc:
        raise CohortEffectsConflict(str(exc)) from exc


def _included_members(operation: Mapping[str, Any]) -> list[dict[str, Any]]:
    members = [dict(member) for member in operation.get("members", ()) if member["included"]]
    # Workers first, supervisor last.  Force Pause must never interrupt the
    # coordinator before its workers, and Stop retains the same deterministic
    # order for reproducible partial-progress evidence.
    return sorted(
        members,
        key=lambda member: (member.get("role") == roster.ROLE_SUPERVISOR, member["agent_id"]),
    )


def _receipt(operation_id: str, label: str, results: Sequence[Mapping[str, Any]]) -> str:
    return _digest(
        {
            "schema": SCHEMA_VERSION,
            "operation_id": operation_id,
            "effect": label,
            "results": list(results),
        }
    )


def _record_result(
    operation_id: str,
    member: Mapping[str, Any],
    *,
    final_state: str,
    risk: str,
    interrupt_action: Optional[str] = None,
    interrupt_outcome: Optional[str] = None,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    request = cohort.MemberResult(
        operation_id=operation_id,
        agent_id=member["agent_id"],
        expected_result_revision=int(member["result_revision"]),
        final_state=final_state,
        background_command_loss_risk=risk,
        task_occurrence_id=member.get("task_occurrence_id"),
        boundary_digest=member.get("boundary_digest"),
        report_digest=member.get("report_digest"),
        checkpoint_digest=member.get("checkpoint_digest"),
        interrupt_action=interrupt_action,
        interrupt_outcome=interrupt_outcome,
        result_detail=detail,
    )
    return cohort.record_member_result(request)


def execute_safe_pause(request: SafePauseRequest) -> dict[str, Any]:
    """Consume an opaque M3-D drain receipt and terminalize Safe Pause.

    The member records arrive already classified by M3-D.  This function
    validates only their physical cohort identity and delegates the closed
    terminal vocabulary to ``cohort_journal``; it never interprets task text,
    completion, or the receipt's contents.
    """
    operation = _operation(request.operation_id)
    if operation["operation_kind"] != cohort.KIND_PAUSE:
        raise CohortEffectsConflict("safe Pause executor requires a Pause operation")
    if operation["current_mode"] != cohort.MODE_SAFE:
        raise CohortEffectsConflict("safe Pause executor requires a still-safe operation")
    if operation["state"] == cohort.STATE_PAUSED:
        return operation
    if operation["state"] != cohort.STATE_DRAINING:
        raise CohortEffectsConflict(
            f"safe Pause requires {cohort.STATE_DRAINING!r}; got {operation['state']!r}"
        )

    member_ids = {member["agent_id"] for member in operation["members"] if member["included"]}
    supplied_ids = {member.agent_id for member in request.member_results}
    if member_ids != supplied_ids:
        raise CohortEffectsConflict(
            "safe Pause member evidence must cover the exact included cohort; "
            f"missing={sorted(member_ids - supplied_ids)}, extra={sorted(supplied_ids - member_ids)}"
        )
    for result in request.member_results:
        cohort.record_member_result(result)

    committed = cohort.commit_terminal(
        cohort.TerminalCommitRequest(
            transition_id=request.commit_transition_id,
            operation_id=request.operation_id,
            expected_state_epoch=int(operation["state_epoch"]),
            actor=request.actor,
            receipt_digest=request.drain_receipt_digest,
            reason=request.reason,
        )
    )
    return cast(dict[str, Any], committed["operation"])


def _default_observer(member: Mapping[str, Any], phase: str) -> PauseObservation:
    """Conservative built-in observation: never invent child termination.

    A dead pane is a positive exit.  A live pane without a task-child proof is
    unknown even if a screen detector looks idle; callers with an exact tracked
    child/turn observer supply it explicitly.  This makes the dark default
    useful for exit recovery without recreating the Codex false-pause bug.
    """
    terminal_id = member.get("terminal_id")
    if not terminal_id:
        return PauseObservation(
            OBS_EXITED,
            None,
            "member has no live terminal and no exact tracked-child proof",
        )
    resolved = control_input_service.resolve_control_identity(str(terminal_id))
    if resolved is None:
        return PauseObservation(
            OBS_EXITED,
            None,
            "exact terminal pane is absent but tracked-child state is unknown",
        )
    expected_generation = member.get("generation")
    if expected_generation and resolved.terminal_generation != expected_generation:
        return PauseObservation(
            OBS_UNKNOWN,
            None,
            "terminal id now names a different generation; refusing to infer exit",
        )
    if resolved.pane_dead:
        return PauseObservation(
            OBS_EXITED,
            None,
            "exact terminal pane exited but tracked-child state is unknown",
        )
    return PauseObservation(
        OBS_UNKNOWN,
        None,
        f"{phase} observation has no exact tracked-child proof",
    )


def _default_interrupt(
    member: Mapping[str, Any], events: Sequence[Mapping[str, Any]], control_id: str
) -> control_input_service.ControlInputResult:
    expected = {
        "terminal_id": member.get("terminal_id"),
        "terminal_generation": member.get("generation"),
        "pane_birth_id": member.get("pane_id"),
        "provider": member.get("harness"),
        "native_session_id": member.get("native_session_id"),
    }
    return control_input_service.deliver_control_input(
        str(member["terminal_id"]),
        control_id=control_id,
        events=list(events),
        expected_identity={key: value for key, value in expected.items() if value is not None},
        protocol=CONTROL_INPUT_PROTOCOL,
    )


def _control_id(operation_id: str, agent_id: str) -> str:
    return f"m3c-{operation_id.replace('-', '')[:20]}-{agent_id.replace('-', '')[:20]}"


def _interrupt_events(member: Mapping[str, Any]) -> Optional[list[Mapping[str, Any]]]:
    terminal_id = member.get("terminal_id")
    if not terminal_id:
        return None
    resolved = control_input_service.resolve_control_identity(str(terminal_id))
    if resolved is None or resolved.provider != member.get("harness"):
        return None
    controls = provider_controls.controls_for(str(resolved.provider), resolved.provider_version)
    stop_events = controls.get("stop") if controls is not None else None
    if not stop_events:
        return None
    return [dict(event) for event in stop_events]


def _observation_terminal(observation: PauseObservation, *, after_interrupt: bool) -> Optional[str]:
    if observation.tracked_children_clear is not True:
        return None
    if observation.state == OBS_EXITED:
        return cohort.FINAL_EXITED
    if observation.state == OBS_PARKED:
        return cohort.FINAL_PARKED
    if observation.state == OBS_IDLE:
        return cohort.FINAL_ALREADY_IDLE if not after_interrupt else cohort.FINAL_INTERRUPTED
    if after_interrupt and observation.state == OBS_INTERRUPTED:
        return cohort.FINAL_INTERRUPTED
    return None


def execute_force_pause(
    request: ForcePauseRequest,
    *,
    observer: PauseObserver = _default_observer,
    interrupt: InterruptDeliverer = _default_interrupt,
) -> dict[str, Any]:
    """Interrupt workers then supervisor and commit only on positive quiescence."""
    operation = _operation(request.operation_id)
    if operation["operation_kind"] != cohort.KIND_PAUSE:
        raise CohortEffectsConflict("force Pause executor requires a Pause operation")
    if operation["state"] == cohort.STATE_PAUSED:
        return operation
    if operation["current_mode"] != cohort.MODE_FORCE:
        raise CohortEffectsConflict("force Pause executor requires explicit force mode")
    if operation["state"] == cohort.STATE_PREPARING:
        operation = cohort.transition_operation(
            cohort.TransitionRequest(
                transition_id=request.interrupt_transition_id,
                operation_id=request.operation_id,
                expected_state_epoch=request.expected_state_epoch,
                to_state=cohort.STATE_INTERRUPTING,
                actor=request.actor,
                reason=request.reason,
            )
        )["operation"]
    elif operation["state"] == cohort.STATE_RECONCILIATION_REQUIRED:
        return operation
    elif operation["state"] != cohort.STATE_INTERRUPTING:
        raise CohortEffectsConflict(f"force Pause cannot execute from state {operation['state']!r}")

    results: list[dict[str, Any]] = []
    needs_reconciliation = False
    for member in _included_members(_operation(request.operation_id)):
        if member["final_state"] in cohort.PAUSE_TERMINAL_MEMBER_STATES[cohort.MODE_FORCE]:
            results.append({"agent_id": member["agent_id"], "final_state": member["final_state"]})
            continue
        before = observer(member, "before")
        terminal = _observation_terminal(before, after_interrupt=False)
        action: Optional[str] = None
        outcome: Optional[str] = None
        detail = before.detail
        risk = cohort.LOSS_NONE
        if terminal is None:
            events = _interrupt_events(member)
            if events is None:
                terminal = cohort.FINAL_RECONCILIATION_REQUIRED
                detail = "no exact-build provider interrupt is available for this member"
                risk = cohort.LOSS_UNKNOWN
            else:
                action = json.dumps(events, sort_keys=True, separators=(",", ":"))
                delivered = interrupt(
                    member,
                    events,
                    _control_id(request.operation_id, member["agent_id"]),
                )
                outcome = str(delivered.outcome or "in-flight")
                detail = delivered.detail
                risk = cohort.LOSS_POSSIBLE
                if delivered.outcome == ACCEPTED:
                    after = observer(member, "after")
                    terminal = _observation_terminal(after, after_interrupt=True)
                    detail = after.detail or delivered.detail
                if terminal is None:
                    terminal = cohort.FINAL_RECONCILIATION_REQUIRED
        if terminal == cohort.FINAL_RECONCILIATION_REQUIRED:
            needs_reconciliation = True
        recorded = _record_result(
            request.operation_id,
            member,
            final_state=terminal,
            risk=risk,
            interrupt_action=action,
            interrupt_outcome=outcome,
            detail=detail,
        )
        results.append(
            {
                "agent_id": member["agent_id"],
                "final_state": recorded["final_state"],
                "result_revision": recorded["result_revision"],
            }
        )

    current = _operation(request.operation_id)
    receipt = _receipt(request.operation_id, "force-pause", results)
    if needs_reconciliation:
        transitioned = cohort.transition_operation(
            cohort.TransitionRequest(
                transition_id=request.reconciliation_transition_id,
                operation_id=request.operation_id,
                expected_state_epoch=int(current["state_epoch"]),
                to_state=cohort.STATE_RECONCILIATION_REQUIRED,
                actor=request.actor,
                reason="one or more members lack positive turn/task-child termination proof",
                receipt_digest=receipt,
            )
        )
        return cast(dict[str, Any], transitioned["operation"])
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


def _default_reaper(member: Mapping[str, Any]) -> bool:
    terminal_id = member.get("terminal_id")
    if not terminal_id:
        return True
    generation = member.get("generation")
    session_name = member.get("session_name")
    if generation:
        return terminal_service.delete_terminal(
            str(terminal_id),
            expected_generation=str(generation),
            expected_session=str(session_name) if session_name else None,
        )
    return terminal_service.retire_observed_terminal(
        str(terminal_id),
        expected_session=str(session_name) if session_name else None,
        expected_pane_id=(str(member["pane_id"]) if member.get("pane_id") else None),
        backend_already_closed=False,
    )


def _member_reap_view(operation: Mapping[str, Any], member: Mapping[str, Any]) -> dict[str, Any]:
    return {**dict(member), "session_name": operation["session_name"]}


def _pending_reincarnations(session_name: str) -> list[dict[str, Any]]:
    return [
        operation
        for operation in operation_journal.list_operations(session_name=session_name)
        if operation.get("result_state")
        not in {
            operation_journal.RESULT_ACCEPTED,
            operation_journal.RESULT_REFUSED,
        }
    ]


def _default_wait_interruptor(session_name: str, operation_id: str) -> Sequence[Mapping[str, Any]]:
    """Dark M7 seam: there is no registered-wait store in this fork yet.

    M7 owns registration meaning and the durable ``interrupted-by-stop``
    records.  M3-C owns when that exact-retry interruptor runs: immediately
    after the Stop barrier and before member teardown.  Until M7 supplies the
    callback, the empty sequence truthfully means no fork registrations were
    available to interrupt; this module never invents or resumes a wait.
    """
    del session_name, operation_id
    return ()


def reap_reincarnation_resources(
    operation: Mapping[str, Any], *, reaper: TerminalReaper = _default_reaper
) -> list[dict[str, Any]]:
    """Reap a B3 operation's exact reserved/materialized successor, if any.

    This is also called by B3 when the Stop barrier wins after pane creation.
    The reserved terminal id + generation are immutable, so a retry cannot
    select a replacement resource.
    """
    terminal_id = operation.get("successor_terminal_id")
    generation = operation.get("successor_generation")
    if not terminal_id or not generation:
        return []
    member = {
        "terminal_id": terminal_id,
        "generation": generation,
        "session_name": operation.get("session_name"),
        "pane_id": None,
    }
    try:
        reaped = bool(reaper(member))
        return [
            {
                "operation_id": operation["operation_id"],
                "terminal_id": terminal_id,
                "generation": generation,
                "reaped": reaped,
            }
        ]
    except Exception as exc:  # noqa: BLE001 - Stop reports each exact failure
        return [
            {
                "operation_id": operation["operation_id"],
                "terminal_id": terminal_id,
                "generation": generation,
                "reaped": False,
                "detail": str(exc)[: cohort.MAX_DETAIL_LEN],
            }
        ]


def _reconcile_interrupted_reincarnation(
    stop_operation_id: str,
    operation: Mapping[str, Any],
    resource_results: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Settle an interrupted M3-B row after its exact query/reap.

    A crashed B3 executor may never return to write its own result after Stop
    wins.  Force Stop records the same truthful terminal outcome itself.  The
    M3-B result is write-once, so a concurrent executor's result is adopted.
    """
    operation_id = str(operation["operation_id"])
    state = operation.get("result_state")
    terminal_states = {
        operation_journal.RESULT_ACCEPTED,
        operation_journal.RESULT_REFUSED,
        operation_journal.RESULT_RECONCILIATION_REQUIRED,
    }
    if state != operation_journal.RESULT_PENDING:
        return {
            "operation_id": operation_id,
            "journal_result_state": state,
            "reconciled": state in terminal_states,
        }
    try:
        recorded = operation_journal.record_result(
            operation_id,
            operation_journal.RESULT_RECONCILIATION_REQUIRED,
            detail="the session Stop barrier interrupted this exact reincarnation",
            evidence={
                "stop_operation_id": stop_operation_id,
                "stop_resource_receipt_digest": _digest(list(resource_results)),
            },
        )["operation"]
        return {
            "operation_id": operation_id,
            "journal_result_state": recorded["result_state"],
            "reconciled": recorded["result_state"] in terminal_states,
        }
    except Exception as exc:  # noqa: BLE001 - Stop still continues exact reaps
        return {
            "operation_id": operation_id,
            "journal_result_state": "write-failed",
            "reconciled": False,
            "detail": str(exc)[: cohort.MAX_DETAIL_LEN],
        }


def _transition_reconciliation(
    request: StopEffectsRequest, operation: Mapping[str, Any], receipt: str, reason: str
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


def execute_stop(
    request: StopEffectsRequest,
    *,
    reaper: TerminalReaper = _default_reaper,
    safe_operation_drainer: Optional[SafeOperationDrainer] = None,
    wait_interruptor: WaitInterruptor = _default_wait_interruptor,
) -> dict[str, Any]:
    """Execute Safe or Force Stop after atomically installing its barrier.

    Safe Stop refuses physical teardown while a pre-barrier M3-B operation
    cannot be positively drained.  Force Stop never calls the drainer: it
    immediately reaps the captured cohort and every known exact successor,
    then records the remaining operation truth for later reconciliation.
    """
    operation = _operation(request.operation_id)
    if operation["operation_kind"] != cohort.KIND_STOP:
        raise CohortEffectsConflict("Stop executor requires a Stop operation")
    if operation["state"] == cohort.STATE_STOPPED:
        # A response-loss retry still reconciles any B3 resource that
        # materialized late before returning the durable terminal state.
        for pending in _pending_reincarnations(operation["session_name"]):
            resource_results = reap_reincarnation_resources(pending, reaper=reaper)
            _reconcile_interrupted_reincarnation(request.operation_id, pending, resource_results)
        return operation
    if operation[
        "state"
    ] == cohort.STATE_RECONCILIATION_REQUIRED and request.expected_state_epoch != int(
        operation["state_epoch"]
    ):
        # The caller is replaying the request that already produced this
        # terminal-for-that-attempt answer.  Do not turn response loss into a
        # new teardown attempt.  An explicit operator/supervisor retry uses
        # the current epoch and a fresh transition id.
        return operation

    teardown = cohort.begin_stop_teardown(
        cohort.StopTeardownRequest(
            transition_id=request.teardown_transition_id,
            operation_id=request.operation_id,
            expected_state_epoch=request.expected_state_epoch,
            actor=request.actor,
            reason=request.reason,
            receipt_digest=request.drain_receipt_digest,
            promote_to_force=request.promote_to_force,
        )
    )["operation"]
    force = teardown["current_mode"] == cohort.MODE_FORCE
    wait_results: list[dict[str, Any]] = []
    wait_interruption_failed = False
    try:
        wait_results = [
            dict(result)
            for result in wait_interruptor(teardown["session_name"], request.operation_id)
        ]
    except Exception as exc:  # noqa: BLE001 - Stop still reaps cohort siblings
        wait_interruption_failed = True
        wait_results = [
            {
                "state": cohort.FINAL_RECONCILIATION_REQUIRED,
                "detail": str(exc)[: cohort.MAX_DETAIL_LEN],
            }
        ]
    in_flight = _pending_reincarnations(teardown["session_name"])

    if not force and in_flight:
        drained: list[dict[str, Any]] = []
        for reincarnation in in_flight:
            ok = bool(safe_operation_drainer and safe_operation_drainer(reincarnation))
            drained.append({"operation_id": reincarnation["operation_id"], "drained": ok})
        if not all(item["drained"] for item in drained):
            receipt = _receipt(
                request.operation_id, "safe-stop-undrained", [*wait_results, *drained]
            )
            return _transition_reconciliation(
                request,
                teardown,
                receipt,
                "safe Stop retained resources because a pre-barrier reincarnation did not drain",
            )
        still_in_flight = _pending_reincarnations(teardown["session_name"])
        if still_in_flight:
            receipt = _receipt(
                request.operation_id,
                "safe-stop-drain-unconfirmed",
                [
                    {"operation_id": item["operation_id"], "drained": False}
                    for item in still_in_flight
                ],
            )
            return _transition_reconciliation(
                request,
                teardown,
                receipt,
                "safe Stop retained resources because the drain result was not durable",
            )

    member_results: list[dict[str, Any]] = []
    needs_reconciliation = wait_interruption_failed
    for member in _included_members(_operation(request.operation_id)):
        if member["final_state"] in cohort.STOP_TERMINAL_MEMBER_STATES:
            member_results.append(
                {"agent_id": member["agent_id"], "final_state": member["final_state"]}
            )
            continue
        view = _member_reap_view(teardown, member)
        try:
            member_reaped = bool(reaper(view))
            state = cohort.FINAL_STOPPED if member_reaped else cohort.FINAL_RECONCILIATION_REQUIRED
            detail = (
                "exact terminal generation reaped" if member_reaped else "exact reap returned false"
            )
        except Exception as exc:  # noqa: BLE001 - best-effort Stop continues siblings
            state = cohort.FINAL_RECONCILIATION_REQUIRED
            detail = str(exc)[: cohort.MAX_DETAIL_LEN]
        if state == cohort.FINAL_RECONCILIATION_REQUIRED:
            needs_reconciliation = True
        recorded = _record_result(
            request.operation_id,
            member,
            final_state=state,
            risk=cohort.LOSS_POSSIBLE if force else cohort.LOSS_NONE,
            detail=detail,
        )
        member_results.append(
            {"agent_id": member["agent_id"], "final_state": recorded["final_state"]}
        )

    reincarnation_results: list[dict[str, Any]] = []
    # Force reaps rather than drains.  Safe also performs an exact post-drain
    # cleanup scan so a response-loss retry adopts/removes the same resource.
    for reincarnation in _pending_reincarnations(teardown["session_name"]):
        resource_results = reap_reincarnation_resources(reincarnation, reaper=reaper)
        reincarnation_results.extend(resource_results)
        reconciled = _reconcile_interrupted_reincarnation(
            request.operation_id, reincarnation, resource_results
        )
        reincarnation_results.append(reconciled)
        if not reconciled["reconciled"]:
            needs_reconciliation = True
    if any(result.get("reaped") is False for result in reincarnation_results):
        needs_reconciliation = True

    summary = [*wait_results, *member_results, *reincarnation_results]
    receipt = _receipt(request.operation_id, "force-stop" if force else "safe-stop", summary)
    current = _operation(request.operation_id)
    if needs_reconciliation:
        return _transition_reconciliation(
            request,
            current,
            receipt,
            "one or more exact Stop resources require reconciliation",
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
