"""Dedicated, one-shot recovery for a callback stranded by an ACP refusal."""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import os
import re
import secrets
import shlex
import threading
import weakref
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import and_, or_, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import object_session

from cli_agent_orchestrator.clients import database
from cli_agent_orchestrator.models.inbox import (
    CallbackRecoveryCallbackRequest,
    CallbackRecoveryCompletionRequest,
    CallbackRecoveryDispositionRequest,
    CallbackRecoveryRequest,
    CallbackRecoveryResolutionRequest,
    InboxMessage,
    MessageStatus,
)
from cli_agent_orchestrator.services import (
    callback_text_contract,
    companion_receipts,
    control_input_service,
    model_turn_receipt_contract,
)
from cli_agent_orchestrator.services.control_input_contract import REASON_MANAGED_ACP_PANE

STATE_INTENT = "intent"
STATE_PENDING = "pending"
STATE_SUBMITTED = "recovery-submitted"
STATE_COMPLETED = "callback-completed"
STATE_REFUSED = "refused"
STATE_AMBIGUOUS = "ambiguous"
STATE_RESOLVED = "manual-resolved"
STATE_CALLBACK_UNDELIVERABLE = "callback-undeliverable"
KNOWN_STATES = frozenset(
    {
        STATE_INTENT,
        STATE_PENDING,
        STATE_SUBMITTED,
        STATE_COMPLETED,
        STATE_REFUSED,
        STATE_AMBIGUOUS,
        STATE_RESOLVED,
        STATE_CALLBACK_UNDELIVERABLE,
    }
)
RELEASABLE_STATES = frozenset(
    {STATE_COMPLETED, STATE_REFUSED, STATE_RESOLVED, STATE_CALLBACK_UNDELIVERABLE}
)
TERMINAL_STATES = RELEASABLE_STATES
_ZERO_EFFECT_REFUSAL_REASONS = frozenset(
    {
        "artifact-path-invalid",
        "callback-digest-mismatch",
        "source-reservation-ambiguous",
        "source-not-admitted",
        "source-identity-mismatch",
        "source-generation-replaced",
        "supervisor-authority-mismatch",
        "supervisor-generation-mismatch",
        "source-caller-mismatch",
        "refusal-identity-mismatch",
        "refusal-occurrence-mismatch",
        "refusal-occurrence-absent",
        "terminal-identity-ambiguous",
        "workflow-identity-mismatch",
        "recovery-lifecycle-fenced-before-provider-io",
        "w13-fenced-before-provider-io",
    }
)


class CallbackRecoveryError(RuntimeError):
    reason_code = "callback-recovery-invalid"


class CallbackRecoveryConflict(CallbackRecoveryError):
    reason_code = "callback-recovery-conflict"


class CallbackRecoveryIdentityConflict(CallbackRecoveryConflict):
    """A typed, rowless submitted-request ownership conflict."""

    reason_code = "identity-owned-elsewhere"

    def __init__(self, response: dict[str, Any], *, detail: str) -> None:
        super().__init__(detail)
        self.response = response


class CallbackRecoveryAmbiguous(CallbackRecoveryConflict):
    reason_code = "callback-recovery-ambiguous"


class CallbackRecoveryRefused(CallbackRecoveryError):
    def __init__(self, detail: str, *, reason_code: str) -> None:
        super().__init__(detail)
        self.reason_code = reason_code


class CallbackRecoveryNotFound(CallbackRecoveryError):
    reason_code = "callback-recovery-not-found"


class CallbackRecoveryPending(CallbackRecoveryError):
    reason_code = "callback-recovery-pending"


@dataclass(frozen=True)
class RecoveryAdmission:
    operation: dict[str, Any]
    message: Optional[InboxMessage]
    replayed: bool


_LIFECYCLE_CLAIMS = threading.local()

logger = logging.getLogger(__name__)


@contextmanager
def generation_lifecycle_claim(terminal_id: str, generation: str):
    """Serialize recovery admission with teardown for one exact incarnation."""
    with generation_lifecycle_claims(((terminal_id, generation),)):
        yield


def _claim_lock_path(key: tuple[str, str, str]) -> Path:
    """The flock file for one canonical lifecycle key.

    Hex-encodes every component so distinct identities (e.g. ``a/b`` vs
    ``a-b``) never share a lock — the encoding is reversible and filename-safe.
    Shared by the sync claim and the async session claim so they serialize
    against each other on the same file.
    """
    from cli_agent_orchestrator.constants import COMPANION_DIR

    terminal_id, claim_kind, generation = key
    return (
        Path(COMPANION_DIR)
        / terminal_id.encode("utf-8").hex()
        / claim_kind.encode("utf-8").hex()
        / generation.encode("utf-8").hex()
        / ".callback-recovery-lifecycle.lock"
    )


def _flock_acquire(path: Path) -> int:
    """Create (if needed) and exclusively flock the lock file; return the fd.

    The fd owns the lock; ``_flock_release`` releases it. Blocking, so callers
    that must not stall an event loop acquire it off-loop (see
    ``async_session_lifecycle_claim``).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def _flock_release(descriptor: int) -> None:
    """Drop the exclusive flock and close the fd that owned it."""
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


@contextmanager
def generation_lifecycle_claims(keys):
    """Claim exact terminal incarnations in one canonical global order.

    Any operation that needs more than one lifecycle key must pass the complete
    set here.  Acquiring source then supervisor (or model generation then pane)
    in caller order can deadlock against session retirement, which necessarily
    observes the same keys in a different order.
    """
    held = getattr(_LIFECYCLE_CLAIMS, "held", set())
    canonical = sorted(
        {
            (
                (str(key[0]), str(key[1]), str(key[2]))
                if len(key) == 3
                else (str(key[0]), "model-generation", str(key[1]))
            )
            for key in keys
        }
    )
    missing = [key for key in canonical if key not in held]
    if not missing:
        yield
        return

    if held and min(missing) < max(held):
        raise RuntimeError(
            "lifecycle keys must be acquired together in canonical order; "
            "nested acquisition would invert the global lock order"
        )

    with ExitStack() as stack:
        for key in missing:
            descriptor = _flock_acquire(_claim_lock_path(key))
            stack.callback(_flock_release, descriptor)
        _LIFECYCLE_CLAIMS.held = {*held, *missing}
        try:
            yield
        finally:
            _LIFECYCLE_CLAIMS.held = held


def terminal_lifecycle_claim_set(*terminals: Any) -> set[tuple[str, str, str]]:
    """Derive typed ownership claims from an already-closed terminal snapshot."""
    claims: set[tuple[str, str, str]] = set()
    for terminal in terminals:
        if terminal is None:
            continue
        if isinstance(terminal, dict):
            terminal_id = str(terminal.get("id") or "")
            read_value = terminal.get
        else:
            terminal_id = str(getattr(terminal, "id", "") or "")
            read_value = lambda name: getattr(terminal, name, None)
        if not terminal_id:
            continue
        for kind, value in (
            ("model-generation", read_value("generation")),
            ("callback-target-generation", read_value("callback_target_generation")),
            ("pane", read_value("pane_id")),
        ):
            if value:
                claims.add((terminal_id, kind, str(value)))
    return claims


@contextmanager
def session_lifecycle_claim(backend_kind: str, session_or_workspace: str):
    """Serialize physical session/workspace creation and destruction.

    Session names are reusable backend resources, rather than terminal
    incarnations.  Keep their claim in the same global ordering namespace as
    terminal claims so a stale destroy cannot race a newly-created window.
    """
    with generation_lifecycle_claims(
        # The empty synthetic terminal identity sorts before every real terminal
        # id.  Session ownership is therefore always acquired before typed
        # terminal claims, including ordinary hexadecimal ids beginning with a
        # digit; ``__session__`` sorted after those ids and inverted the order.
        ((("", "session-workspace", f"{backend_kind}:{session_or_workspace}"),))
    ):
        yield


@contextmanager
def session_lifecycle_write_claim(session_name: str):
    """Serialize every declared-lifecycle write for one session.

    cond-0221 P1: a ``stop_session`` holds the physical session-workspace
    claim across admission, the ``stopped`` write, and pane collection, but an
    ordinary transition (``declare``/``set_kind``/…) wrote the lifecycle row
    through ``_write`` under *no* shared claim, so a last-write-wins
    ``declare(WORKING)`` could commit over a mid-collection stop and leave a
    collected fleet declared ``working``. This claim closes that seam: every
    lifecycle write takes it, and ``stop_session`` holds it across the whole
    stop, so a competing mutation cannot commit during the stop's critical
    section — it waits, then sees ``stopped`` and is refused by the
    transition itself.

    Keyed by session name alone (the lifecycle module is deliberately
    backend-free), and named so it sorts *after* the physical
    ``session-workspace`` claim and *before* any terminal-generation claim.
    That fixes the lock order as physical < write < generation wherever more
    than one is held (only ``stop_session``/``delete_session`` nest them), and
    the canonical-order guard below turns an inversion into a ``RuntimeError``
    rather than a deadlock.
    """
    with generation_lifecycle_claims((("", "session-workspace-write", session_name),)):
        yield


# Per-event-loop async gates for the physical session claim. Keyed by loop so a
# gate created under one ``asyncio.run`` cannot leak into another (locks bind to
# the loop that first uses them); the WeakKeyDictionary drops the gate set when
# its loop is garbage-collected.
_ASYNC_SESSION_GATES: (
    "weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]]"
) = weakref.WeakKeyDictionary()


def _async_session_gate(session_key: str) -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    inner = _ASYNC_SESSION_GATES.get(loop)
    if inner is None:
        inner = {}
        _ASYNC_SESSION_GATES[loop] = inner
    lock = inner.get(session_key)
    if lock is None:
        lock = asyncio.Lock()
        inner[session_key] = lock
    return lock


class _AcquireHandoff:
    """Cancellation-safe ownership transfer for an off-loop flock acquire.

    The worker that acquires the descriptor either hands it to the caller (the
    caller then owns release) or, if the caller abandoned the wait, releases the
    descriptor itself. The lock makes the check-and-handoff atomic, so exactly
    one of the two ever owns the descriptor — a task cancelled while the acquire
    is in flight can never orphan the lock.
    """

    __slots__ = ("descriptor", "abandoned", "lock")

    def __init__(self) -> None:
        self.descriptor: int | None = None
        self.abandoned = False
        self.lock = threading.Lock()


def _safe_flock_release(descriptor: int) -> None:
    """Release a flock descriptor without raising (cleanup-only path)."""
    try:
        _flock_release(descriptor)
    except Exception:
        logger.warning("flock release failed for descriptor %s", descriptor, exc_info=True)


def _acquire_or_abandon(path: Path, handoff: _AcquireHandoff) -> int:
    """Acquire the flock off-loop, then hand the descriptor off or release it.

    Runs in a worker thread. If the caller has abandoned the wait (cancelled
    before or around delivery), the worker releases the descriptor it just
    acquired itself — so an abandoned acquisition can never leave the lock held
    by a discarded descriptor.
    """
    descriptor = _flock_acquire(path)
    release = False
    with handoff.lock:
        if handoff.abandoned:
            release = True
        else:
            handoff.descriptor = descriptor
    if release:
        _safe_flock_release(descriptor)
    return descriptor


async def _release_claim_descriptor(descriptor: int | None) -> None:
    """Release a held flock descriptor off-loop, surviving caller cancellation.

    Runs off the event loop and is shielded, so a cancellation during release
    cannot abort the executor's ``_flock_release`` — the release completes
    regardless of caller cancellation. A secondary ``CancelledError`` raised by
    the shield is swallowed so the body's original exception/cancellation
    propagates unchanged.
    """
    if descriptor is None:
        return
    try:
        await asyncio.shield(asyncio.to_thread(_safe_flock_release, descriptor))
    except asyncio.CancelledError:
        # The shielded release still completes off-loop.
        pass


@asynccontextmanager
async def async_session_lifecycle_claim(backend_kind: str, session_name: str):
    """Task-owned, non-blocking physical session claim for async creation.

    The sync ``session_lifecycle_claim`` uses a ``threading.local`` reentrancy
    set, which two asyncio tasks on one event-loop thread would share: while
    task A is suspended in ``await provider.initialize()`` holding the claim,
    task B would see A's key and enter the supposedly exclusive create section
    without blocking. This closes that seam:

    * a per-session ``asyncio.Lock`` gates tasks on the loop — the second task
      *awaits* the gate (it does not block the event loop), and a child task
      does not inherit the parent's ownership (the lock is task-owned, not
      re-entrant);
    * the same cross-process file lock the sync claim uses is then acquired
      *off* the loop (a worker thread), so a contended sync stop/delete in
      another process does not stall the loop either, and async and sync paths
      still serialize on the canonical key.

    Cancellation safety: the off-loop acquire goes through an ownership handoff
    (``_AcquireHandoff``). If the task is cancelled while the worker is still
    acquiring, the task abandons and the worker releases the descriptor itself
    once it lands; if the worker already delivered it, the task owns and
    releases it. Release is shielded off-loop, so cancellation during release
    cannot orphan the descriptor either. A cancelled waiter never enters the
    claim body, and the gate is always released.

    Canonical lock order is unchanged: this is the physical session key, which
    sorts before every terminal-generation key, and it is the only claim async
    creation takes (it does not nest generation claims).
    """
    session_key = f"{backend_kind}:{session_name}"
    canonical = ("", "session-workspace", session_key)
    gate = _async_session_gate(session_key)
    await gate.acquire()
    descriptor: int | None = None
    try:
        handoff = _AcquireHandoff()
        try:
            descriptor = await asyncio.to_thread(
                _acquire_or_abandon, _claim_lock_path(canonical), handoff
            )
        except BaseException:
            # Cancelled (or failed) while the worker may still be acquiring.
            # Mark the wait abandoned: if the worker has not yet handed off it
            # will release the descriptor itself when it lands; if it already
            # did, this task owns it now and releases it.
            with handoff.lock:
                handoff.abandoned = True
                descriptor = handoff.descriptor
            if descriptor is not None:
                await _release_claim_descriptor(descriptor)
            descriptor = None
            raise
        try:
            yield
        finally:
            await _release_claim_descriptor(descriptor)
            descriptor = None
    finally:
        gate.release()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _request_payload(body: CallbackRecoveryRequest) -> dict[str, Any]:
    return body.model_dump(mode="json")


def _workflow_identity(body: CallbackRecoveryRequest) -> dict[str, Any]:
    return {
        "project": body.project,
        "task_id": body.task_id,
        "run_id": body.run_id,
        "source_terminal_id": body.source_terminal_id,
        "source_generation": body.source_generation,
        "expected_provider": body.expected_provider,
        "expected_provider_session_id": body.expected_provider_session_id,
        "expected_execution_mode": body.expected_execution_mode,
        "supervisor_id": body.supervisor_id,
        "supervisor_session": body.supervisor_session,
        "supervisor_generation": body.supervisor_generation,
        "supervisor_pane_id": body.supervisor_pane_id,
        "callback_occurrence_id": body.callback_occurrence_id,
    }


def _recovery_identity(body: CallbackRecoveryRequest) -> dict[str, Any]:
    """The authoritative one-shot refusal consumption identity.

    Callback/workflow fields are intentionally excluded: one durable refusal
    occurrence can authorize at most one operation, regardless of which
    caller-supplied callback occurrence or operation id is presented.
    """
    return {
        "refusal_control_id": body.refusal_control_id,
        "refusal_occurrence_sha256": body.refusal_occurrence_sha256,
        "refusal_request_sha256": body.refusal_request_sha256,
    }


def _operation_key(body: CallbackRecoveryRequest) -> str:
    return _digest(
        {
            "schema": "cao-callback-recovery-operation-key-v1",
            "workflow": _workflow_identity(body),
            "operation_id": body.operation_id,
        }
    )


REQUEST_SCHEMA = "cao-callback-recovery-request-v1"
OPERATION_SCHEMA = "cao-callback-recovery-operation-v2"
CALLBACK_LOOKUP_SCHEMA = "cao-callback-recovery-callback-lookup-v1"
CALLBACK_ATTEMPT_NOT_REGISTERED = "not-registered"
CALLBACK_ATTEMPT_REGISTERED = "registered"
CALLBACK_ATTEMPT_EFFECT_CLAIMED = "effect-claimed"
CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS = "effect-ambiguous"
CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED = "zero-effect-refused"
CALLBACK_ATTEMPT_EFFECT_COMMITTED = "effect-committed"
_CALLBACK_ATTEMPT_STATES = frozenset(
    {
        CALLBACK_ATTEMPT_NOT_REGISTERED,
        CALLBACK_ATTEMPT_REGISTERED,
        CALLBACK_ATTEMPT_EFFECT_CLAIMED,
        CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS,
        CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED,
        CALLBACK_ATTEMPT_EFFECT_COMMITTED,
    }
)


def lifecycle_v2_enabled() -> bool:
    """Read the process-local lifecycle-v2 rollout toggle strictly.

    Configuration is intentionally evaluated per process invocation.  The
    deployment contract requires a restart for a changed value, so no request
    can observe an in-process cache that differs from capability advertisement.
    """
    return (
        os.environ.get("CAO_CALLBACK_RECOVERY_LIFECYCLE_V2_ENABLED", "").lower()
        in {
            "1",
            "true",
            "yes",
        }
        and database.callback_recovery_migration_ready()
    )


def operation_identity(body: CallbackRecoveryRequest) -> tuple[str, str]:
    """Return the address key and complete canonical request proof."""
    return _operation_key(body), _digest(_request_payload(body))


def _identity_conflict(
    body: CallbackRecoveryRequest, owner: Any, *, reason_code: str
) -> CallbackRecoveryIdentityConflict:
    """Build a strict conflict proof while the detecting write lock is held."""
    submitted_operation_key, submitted_request_sha256 = operation_identity(body)
    owner_request = _stored_request(owner)
    owner_request_sha256 = str(owner.request_sha256)
    if reason_code == "operation-key-owned":
        relation = {
            "operation_key": {
                "submitted": submitted_operation_key,
                "owner": owner.operation_key,
            }
        }
    elif reason_code == "refusal-occurrence-owned":
        relation = {"refusal": _recovery_identity(body)}
    elif reason_code == "callback-occurrence-owned":
        relation = {
            "callback_occurrence": {
                "project": body.project,
                "task_id": body.task_id,
                "run_id": body.run_id,
                "callback_occurrence_id": body.callback_occurrence_id,
            }
        }
    else:  # Keep a future caller fail closed rather than inventing a proof.
        raise CallbackRecoveryConflict("unknown identity ownership relation")
    ownership_identity_sha256 = _digest(
        {
            "schema": "cao-callback-recovery-ownership-identity-v1",
            "reason_code": reason_code,
            "submitted_request": _request_payload(body),
            "owner_request": owner_request,
            "relation": relation,
        }
    )
    detail = {
        "operation-key-owned": "operation id is already bound to different workflow or recovery bytes",
        "refusal-occurrence-owned": "this refusal occurrence already has a one-shot recovery operation",
        "callback-occurrence-owned": "this callback occurrence already has a one-shot recovery operation",
    }[reason_code]
    return CallbackRecoveryIdentityConflict(
        {
            "schema": "cao-callback-recovery-identity-conflict-v1",
            "outcome": "identity-owned-elsewhere",
            "reason_code": reason_code,
            "submitted_operation_key": submitted_operation_key,
            "submitted_request_sha256": submitted_request_sha256,
            "owner_operation_key": owner.operation_key,
            "owner_request_sha256": owner_request_sha256,
            "ownership_identity_sha256": ownership_identity_sha256,
            "proven_zero_provider_bytes_for_submitted_request": True,
        },
        detail=detail,
    )


def _json_object(raw: Optional[str], *, field: str) -> dict[str, Any]:
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object) if raw is not None else None
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CallbackRecoveryConflict(f"managed {field} is unreadable") from exc
    if not isinstance(value, dict):
        raise CallbackRecoveryConflict(f"managed {field} is absent")
    return value


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key {key!r}")
        value[key] = item
    return value


def _reservation_identity(db: Any, terminal_id: str) -> tuple[Any, dict[str, str]]:
    v1 = (
        db.query(database.ManagedLaunchReservationModel)
        .filter(database.ManagedLaunchReservationModel.terminal_id == terminal_id)
        .one_or_none()
    )
    v2 = (
        db.query(database.ManagedLaunchV2ReservationModel)
        .filter(database.ManagedLaunchV2ReservationModel.terminal_id == terminal_id)
        .one_or_none()
    )
    if (v1 is None) == (v2 is None):
        raise CallbackRecoveryRefused(
            "source terminal does not resolve to exactly one managed reservation",
            reason_code="source-reservation-ambiguous",
        )
    row = v1 if v1 is not None else v2
    if v1 is not None:
        request = _json_object(v1.request_json, field="v1 request")
        mode = str(request.get("execution_mode") or "acp")
        readiness = _json_object(v1.readiness_json, field="v1 readiness")
        provider_session_id = readiness.get("provider_session_id")
        admission = _json_object(v1.admission_json, field="v1 admission")
        context = admission.get("context")
        if not isinstance(context, dict):
            raise CallbackRecoveryRefused(
                "v1 admission lacks authoritative workflow context",
                reason_code="workflow-identity-mismatch",
            )
        project = request.get("project")
        task_id = request.get("task_id")
        run_id = context.get("run_id")
        attempt_id = ""
        fencing_token_id = ""
    else:
        mode = str(v2.execution_mode or "acp")
        request = _json_object(v2.request_json, field="v2 request")
        binding = _json_object(v2.binding_json, field="v2 binding")
        provider_session_id = binding.get("native_session_id")
        project = request.get("project")
        task_id = v2.task_id
        run_id = v2.run_id
        attempt_id = binding.get("attempt_id")
        fencing_token_id = binding.get("fencing_token_id")
    if row.state != "admitted":
        raise CallbackRecoveryRefused(
            f"source managed generation is {row.state!r}, not admitted",
            reason_code="source-not-admitted",
        )
    return row, {
        "terminal_id": str(row.terminal_id),
        "generation": str(row.generation),
        "provider": str(row.provider),
        "provider_session_id": str(provider_session_id or ""),
        "execution_mode": mode,
        "caller_id": str(row.caller_id or ""),
        "session_name": str(row.session_name or ""),
        "project": str(project or ""),
        "task_id": str(task_id or ""),
        "run_id": str(run_id or ""),
        "attempt_id": str(attempt_id or ""),
        "fencing_token_id": str(fencing_token_id or ""),
    }


def _terminal_row(db: Any, terminal_id: str) -> Any:
    v1 = (
        db.query(database.TerminalModel)
        .filter(database.TerminalModel.id == terminal_id)
        .one_or_none()
    )
    try:
        v2 = (
            db.query(database.ManagedLaunchV2TerminalModel)
            .filter(database.ManagedLaunchV2TerminalModel.id == terminal_id)
            .one_or_none()
        )
    except Exception as exc:  # noqa: BLE001 - an unreadable vintage is not absence
        if "no such table" not in str(exc).lower():
            raise
        v2 = None
    if (v1 is None) == (v2 is None):
        raise CallbackRecoveryRefused(
            f"terminal {terminal_id!r} does not resolve to exactly one authoritative row",
            reason_code="terminal-identity-ambiguous",
        )
    return v1 if v1 is not None else v2


def _callback_line(body: CallbackRecoveryRequest) -> str:
    return callback_text_contract.canonical_callback_text(
        status=body.callback_status,
        task_id=body.task_id,
        report_path=body.report_path,
        summary=body.callback_summary,
    )


def _recovery_prompt(
    body: CallbackRecoveryRequest, *, operation_key: str, callback_token: str
) -> str:
    command = " ".join(
        (
            f"CAO_CALLBACK_RECOVERY_KEY={shlex.quote(operation_key)}",
            f"CAO_CALLBACK_RECOVERY_TOKEN={shlex.quote(callback_token)}",
            "conduct report",
            "--task",
            shlex.quote(body.task_id),
            "--project",
            shlex.quote(body.project),
            "--status",
            body.callback_status,
            "--report",
            shlex.quote(body.report_path),
            "--summary",
            shlex.quote(callback_text_contract.canonical_summary_text(body.callback_summary)),
        )
    )
    return (
        "Your existing report artifact is complete, but its original callback "
        f"occurrence {body.callback_occurrence_id!r} is stranded. Run exactly "
        "this one route-unattested callback command now; do not edit the report, "
        "do not add --model, --effort, or --route-evidence, and do not run any "
        f"other command:\n\n{command}"
    )


def _refusal_occurrence_from_record(record: Any) -> tuple[str, dict[str, Any]]:
    matching = [
        event
        for event in record.events
        if event.get("to_state") == "refused"
        and event.get("reason_code") == REASON_MANAGED_ACP_PANE
    ]
    if not matching:
        raise CallbackRecoveryRefused(
            "the named control has no managed-ACP zero-byte refusal occurrence",
            reason_code="refusal-occurrence-absent",
        )
    event = matching[-1]
    occurrence = {
        "control_id": record.request_id,
        "terminal_id": record.terminal_id,
        "generation": record.generation,
        "request_sha256": record.request_sha256,
        "reason_code": REASON_MANAGED_ACP_PANE,
        "refused_at": event.get("at"),
        "occurrence_index": len(matching),
    }
    return _digest(occurrence), occurrence


def refusal_occurrence(control_id: str) -> dict[str, Any]:
    record = control_input_service.get_control_input_journal().find(control_id)
    if record is None or record.state != "refused" or record.reason_code != REASON_MANAGED_ACP_PANE:
        raise CallbackRecoveryNotFound(f"control {control_id!r} has no current managed-ACP refusal")
    digest, occurrence = _refusal_occurrence_from_record(record)
    return {"refusal_occurrence_sha256": digest, **occurrence}


def _operation_dict(row: Any) -> dict[str, Any]:
    callback_delivery_status = None
    session = object_session(row)
    if session is not None and row.callback_message_id is not None:
        callback = session.get(database.InboxModel, row.callback_message_id)
        if callback is not None:
            callback_delivery_status = callback.status
    callback_consumed = bool(row.callback_consumed_at) or (
        callback_delivery_status == MessageStatus.DELIVERED.value
    )
    request = _stored_request(row)
    operation = {
        "schema": OPERATION_SCHEMA,
        "operation_key": row.operation_key,
        "operation_id": row.operation_id,
        "request_identity_schema": REQUEST_SCHEMA,
        "request_sha256": row.request_sha256,
        "request": request,
        "state": row.state,
        "reason_code": row.reason_code,
        "proven_zero_bytes": row.state in {STATE_REFUSED, STATE_RESOLVED},
        "project": row.project,
        "task_id": row.task_id,
        "run_id": row.run_id,
        "source_terminal_id": row.source_terminal_id,
        "source_generation": row.source_generation,
        "expected_provider": row.expected_provider,
        "expected_provider_session_id": row.expected_provider_session_id,
        "expected_execution_mode": row.expected_execution_mode,
        "supervisor_id": row.supervisor_id,
        "supervisor_session": row.supervisor_session,
        "supervisor_generation": row.supervisor_generation,
        "supervisor_pane_id": row.supervisor_pane_id,
        "refusal_control_id": row.refusal_control_id,
        "refusal_occurrence_sha256": row.refusal_occurrence_sha256,
        "refusal_request_sha256": row.refusal_request_sha256,
        "callback_occurrence_id": row.callback_occurrence_id,
        "callback_status": row.callback_status,
        "callback_summary": row.callback_summary,
        "callback_message_sha256": row.callback_message_sha256,
        "report_path": row.report_path,
        "report_sha256": row.report_sha256,
        "source_head": row.source_head,
        "publishing_lease_state": row.publishing_lease_state,
        "publishing_lease_sha256": row.publishing_lease_sha256,
        "manifest_path": row.manifest_path,
        "manifest_sha256": row.manifest_sha256,
        "finalization_identity_sha256": row.finalization_identity_sha256,
        "inbox_message_id": row.inbox_message_id,
        "recovery_prompt_sha256": row.recovery_prompt_sha256,
        "message_created_at": row.message_created_at,
        "sender_generation": row.sender_generation,
        "callback_message_id": row.callback_message_id,
        "callback_delivery_status": callback_delivery_status,
        "callback_consumed_at": row.callback_consumed_at,
        "callback_consumed": callback_consumed,
        "callback_attempt_state": row.callback_attempt_state,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }
    if row.admission_response_json:
        operation["admission_response"] = _strict_json_object(
            row.admission_response_json, field="admission response"
        )
    for name, raw in (
        ("provider_turn_receipt", row.provider_turn_receipt_json),
        ("callback_registration_receipt", row.callback_registration_receipt_json),
        ("callback_effect_receipt", row.callback_effect_receipt_json),
        ("callback_disposition", row.callback_disposition_json),
        ("callback_admin_disposition", row.callback_admin_disposition_json),
    ):
        if raw is not None:
            operation[name] = _strict_json_object(raw, field=name)
    # The paired conductor consumes governed terminal evidence through these
    # stable public names.  Keep the descriptive storage aliases above for
    # backwards-compatible diagnostics, but never make reconciliation infer a
    # terminal from an opaque implementation field name.
    if "callback_admin_disposition" in operation:
        operation["disposition"] = operation["callback_admin_disposition"]
    if row.resolution_json is not None:
        operation["resolution"] = _strict_json_object(row.resolution_json, field="resolution")
    return operation


def _stored_request(row: Any) -> dict[str, Any]:
    """Load and prove the persisted canonical start request on every read."""
    if getattr(row, "request_identity_schema", None) != REQUEST_SCHEMA:
        raise CallbackRecoveryConflict("canonical request schema is absent or unsupported")
    request = _strict_json_object(getattr(row, "request_json", None), field="canonical request")
    try:
        normalized = CallbackRecoveryRequest.model_validate(request).model_dump(mode="json")
    except Exception as exc:  # Pydantic error details are not durable evidence.
        raise CallbackRecoveryConflict("canonical request is malformed") from exc
    if normalized != request:
        raise CallbackRecoveryConflict("canonical request is not normalized")
    if _digest(request) != row.request_sha256:
        raise CallbackRecoveryConflict("canonical request digest contradicts operation")
    column_projection = {field: getattr(row, field) for field in request if hasattr(row, field)}
    if column_projection != request:
        raise CallbackRecoveryConflict("canonical request contradicts projected operation fields")
    return request


def _admission_response(row: Any, inbox: Any, *, replayed: bool) -> dict[str, Any]:
    return {
        "outcome": row.state,
        "operation_key": row.operation_key,
        "operation_id": row.operation_id,
        "message_id": inbox.id,
        "message_sha256": inbox.message_sha256,
        "sender_id": inbox.sender_id,
        "sender_generation": inbox.sender_generation,
        "receiver_id": inbox.receiver_id,
        "receiver_generation": inbox.expected_receiver_generation,
        "provider": inbox.expected_provider,
        "provider_session_id": inbox.expected_provider_session_id,
        "execution_mode": inbox.expected_execution_mode,
        "callback_occurrence_id": row.callback_occurrence_id,
        "report_sha256": row.report_sha256,
        "source_head": row.source_head,
        "created_at": inbox.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "status": inbox.status,
        "replayed": replayed,
    }


def _stored_admission_response(row: Any) -> dict[str, Any]:
    response = _strict_json_object(row.admission_response_json, field="admission response")
    required = {
        "outcome",
        "operation_key",
        "operation_id",
        "message_id",
        "message_sha256",
        "sender_id",
        "sender_generation",
        "receiver_id",
        "receiver_generation",
        "provider",
        "provider_session_id",
        "execution_mode",
        "callback_occurrence_id",
        "report_sha256",
        "source_head",
        "created_at",
        "status",
        "replayed",
    }
    if set(response) != required:
        raise CallbackRecoveryConflict("stored admission response has an unknown shape")
    return response


def _strict_json_object(raw: Optional[str], *, field: str) -> dict[str, Any]:
    value = _json_object(raw, field=field)
    if any(not isinstance(key, str) for key in value):
        raise CallbackRecoveryConflict(f"managed {field} has invalid keys")
    return value


def _provider_turn_receipt_expected(row: Any) -> dict[str, str]:
    """Return the immutable admission context for one provider receipt."""
    return {
        "message_id": str(row.inbox_message_id),
        "message_sha256": str(row.recovery_prompt_sha256),
        "message_created_at": str(row.message_created_at),
        "sender_id": str(row.supervisor_id),
        "sender_generation": str(row.sender_generation),
        "receiver_id": str(row.source_terminal_id),
        "receiver_generation": str(row.source_generation),
        "provider": str(row.expected_provider),
        "provider_session_id": str(row.expected_provider_session_id),
    }


def _validate_callback_registration_receipt(row: Any) -> None:
    """Reject a callback registration receipt that is not this recovery's immutable record."""
    registration = _strict_json_object(
        row.callback_registration_receipt_json, field="callback registration receipt"
    )
    required = {
        "schema",
        "operation_key",
        "request_sha256",
        "callback_message_id",
        "callback_message_sha256",
        "callback_created_at",
        "sender_id",
        "receiver_id",
        "source_generation",
        "supervisor_generation",
        "supervisor_pane_id",
        "callback_occurrence_id",
        "registered_at",
    }
    expected = {
        "schema": "cao-callback-registration-receipt-v1",
        "operation_key": row.operation_key,
        "request_sha256": row.request_sha256,
        "callback_message_id": row.callback_message_id,
        "callback_message_sha256": row.callback_message_sha256,
        "sender_id": row.source_terminal_id,
        "receiver_id": row.supervisor_id,
        "source_generation": row.source_generation,
        "supervisor_generation": row.supervisor_generation,
        "supervisor_pane_id": row.supervisor_pane_id,
        "callback_occurrence_id": row.callback_occurrence_id,
    }
    if (
        set(registration) != required
        or {key: registration.get(key) for key in expected} != expected
    ):
        raise CallbackRecoveryConflict(
            "callback registration receipt contradicts recovery identity"
        )
    response = _strict_json_object(row.callback_response_json, field="callback response")
    if registration["callback_created_at"] != response.get("created_at"):
        raise CallbackRecoveryConflict(
            "callback registration receipt contradicts callback timestamp"
        )
    registered_at = registration["registered_at"]
    try:
        if not isinstance(registered_at, str) or not registered_at.endswith("Z"):
            raise ValueError("registration timestamp is not canonical UTC Z")
        datetime.fromisoformat(registered_at.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc:
        raise CallbackRecoveryConflict(
            "callback registration receipt has invalid registration time"
        ) from exc


def _validate_lifecycle_row(row: Any) -> None:
    """Reject malformed or contradictory lifecycle storage on every read."""
    if row.state not in KNOWN_STATES:
        raise CallbackRecoveryConflict(f"callback recovery carries unknown state {row.state!r}")
    required_strings = (
        "operation_key",
        "operation_id",
        "workflow_identity_sha256",
        "recovery_identity_sha256",
        "project",
        "task_id",
        "run_id",
        "source_terminal_id",
        "source_generation",
        "expected_provider",
        "expected_provider_session_id",
        "expected_execution_mode",
        "supervisor_id",
        "supervisor_session",
        "refusal_control_id",
        "refusal_occurrence_sha256",
        "refusal_request_sha256",
        "callback_occurrence_id",
        "callback_message_sha256",
        "report_path",
        "report_sha256",
        "source_head",
        "publishing_lease_state",
        "publishing_lease_sha256",
        "manifest_path",
        "manifest_sha256",
        "finalization_identity_sha256",
        "request_sha256",
        "created_at",
        "updated_at",
    )
    for field in required_strings:
        value = getattr(row, field, None)
        if not isinstance(value, str) or not value:
            raise CallbackRecoveryConflict(f"callback recovery field {field!r} is malformed")
    _stored_request(row)
    if getattr(row, "callback_attempt_state", None) not in _CALLBACK_ATTEMPT_STATES:
        raise CallbackRecoveryConflict("callback recovery has an invalid delivery attempt state")
    if row.state != STATE_REFUSED:
        for field in (
            "supervisor_generation",
            "supervisor_pane_id",
            "callback_token_sha256",
        ):
            value = getattr(row, field, None)
            if not isinstance(value, str) or not value:
                raise CallbackRecoveryConflict(f"callback recovery field {field!r} is malformed")
    if row.state not in {STATE_INTENT, STATE_REFUSED}:
        for field in (
            "inbox_message_id",
            "recovery_prompt_sha256",
            "message_created_at",
            "sender_generation",
        ):
            value = getattr(row, field, None)
            if value is None or (isinstance(value, str) and not value):
                raise CallbackRecoveryConflict(
                    f"callback recovery state {row.state!r} lacks {field}"
                )
        if not row.admission_response_json:
            raise CallbackRecoveryConflict(
                f"callback recovery state {row.state!r} lacks its admission response"
            )
        response = _stored_admission_response(row)
        expected_response = {
            "operation_key": row.operation_key,
            "operation_id": row.operation_id,
            "message_id": row.inbox_message_id,
            "message_sha256": row.recovery_prompt_sha256,
            "sender_id": row.supervisor_id,
            "sender_generation": row.sender_generation,
            "receiver_id": row.source_terminal_id,
            "receiver_generation": row.source_generation,
            "provider": row.expected_provider,
            "provider_session_id": row.expected_provider_session_id,
            "execution_mode": row.expected_execution_mode,
            "callback_occurrence_id": row.callback_occurrence_id,
            "report_sha256": row.report_sha256,
            "source_head": row.source_head,
            "created_at": row.message_created_at,
        }
        if {key: response.get(key) for key in expected_response} != expected_response:
            raise CallbackRecoveryConflict("stored admission response changed identity")
    if row.state == STATE_SUBMITTED:
        if not row.provider_turn_receipt_json:
            raise CallbackRecoveryConflict("submitted callback recovery lacks provider receipt")
        try:
            model_turn_receipt_contract.validate_receipt(
                _strict_json_object(row.provider_turn_receipt_json, field="provider turn receipt"),
                expected=_provider_turn_receipt_expected(row),
            )
        except CallbackRecoveryConflict as exc:
            raise CallbackRecoveryConflict("stored provider receipt is malformed") from exc
        except model_turn_receipt_contract.ReceiptValidationError as exc:
            raise CallbackRecoveryConflict("stored provider receipt contradicts recovery") from exc
    callback_attempt = row.callback_attempt_state
    if callback_attempt in {
        CALLBACK_ATTEMPT_REGISTERED,
        CALLBACK_ATTEMPT_EFFECT_CLAIMED,
        CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS,
        CALLBACK_ATTEMPT_EFFECT_COMMITTED,
    }:
        if row.callback_message_id is None or row.callback_response_json is None:
            raise CallbackRecoveryConflict("callback attempt lacks immutable registration identity")
    elif (
        callback_attempt != CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
        and row.callback_registration_receipt_json is not None
    ):
        raise CallbackRecoveryConflict("unregistered callback attempt carries a receipt")
    if callback_attempt == CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED:
        if row.callback_registration_receipt_json is None:
            if row.callback_message_id is not None or row.callback_response_json is not None:
                raise CallbackRecoveryConflict("pre-registration refusal carries callback identity")
        elif row.callback_message_id is None or row.callback_response_json is None:
            raise CallbackRecoveryConflict(
                "zero-effect callback refusal has partial callback identity"
            )
        if (
            row.state != STATE_CALLBACK_UNDELIVERABLE
            and row.reason_code not in _ZERO_EFFECT_REFUSAL_REASONS
        ):
            raise CallbackRecoveryConflict("zero-effect callback refusal lacks a refusal reason")
    if row.state == STATE_COMPLETED:
        completion = _strict_json_object(row.completion_json, field="completion")
        required = {
            "schema",
            "callback_message_id",
            "callback_message_sha256",
            "callback_created_at",
            "finalization_identity_sha256",
            "source_terminal_id",
            "source_generation",
            "supervisor_id",
            "supervisor_generation",
            "supervisor_pane_id",
            "callback_occurrence_id",
            "completed_at",
        }
        if set(completion) != required:
            raise CallbackRecoveryConflict("stored callback completion has an unknown shape")
        effect = _strict_json_object(
            row.callback_effect_receipt_json, field="callback effect receipt"
        )
        if effect.get("schema") != "cao-callback-effect-receipt-v1":
            raise CallbackRecoveryConflict("completed recovery lacks callback effect receipt")
        if row.callback_attempt_state != CALLBACK_ATTEMPT_EFFECT_COMMITTED:
            raise CallbackRecoveryConflict("completed recovery has non-committed callback attempt")
    elif row.completion_json is not None:
        raise CallbackRecoveryConflict("non-completed callback recovery carries completion state")
    if row.callback_response_json is not None:
        response = _strict_json_object(row.callback_response_json, field="callback response")
        required = {
            "message_id",
            "sender_id",
            "receiver_id",
            "created_at",
            "callback_occurrence_id",
            "replayed",
        }
        if set(response) != required:
            raise CallbackRecoveryConflict("stored callback response has an unknown shape")
    if row.callback_registration_receipt_json is not None:
        _validate_callback_registration_receipt(row)
    if row.callback_disposition_json is not None:
        intent = _strict_json_object(
            row.callback_disposition_json, field="callback delivery intent"
        )
        required = {
            "schema",
            "operation_key",
            "request_sha256",
            "callback_message_id",
            "callback_message_sha256",
            "callback_created_at",
            "finalization_identity_sha256",
            "registered_at",
        }
        if set(intent) != required or intent.get("schema") != "cao-callback-delivery-intent-v1":
            raise CallbackRecoveryConflict("callback delivery intent has an unknown shape")
        if (
            intent["operation_key"] != row.operation_key
            or intent["request_sha256"] != row.request_sha256
            or intent["callback_message_id"] != row.callback_message_id
            or intent["callback_message_sha256"] != row.callback_message_sha256
            or intent["finalization_identity_sha256"] != row.finalization_identity_sha256
        ):
            raise CallbackRecoveryConflict("callback delivery intent contradicts recovery identity")
    if row.state == STATE_CALLBACK_UNDELIVERABLE:
        disposition = _strict_json_object(
            row.callback_admin_disposition_json, field="callback ADMIN disposition"
        )
        required = {
            "schema",
            "outcome",
            "operation_key",
            "request_sha256",
            "provider_turn_receipt_sha256",
            "callback_registration_receipt_sha256",
            "callback_effect_receipt_sha256",
            "callback_attempt_state",
            "certainty",
            "actor_scope",
            "evidence_sha256",
            "detail",
            "disposed_at",
        }
        if (
            set(disposition) != required
            or disposition.get("schema") != "cao-callback-recovery-disposition-v1"
            or disposition.get("outcome") != "provider-effect-proven-callback-undeliverable"
            or disposition.get("operation_key") != row.operation_key
            or disposition.get("request_sha256") != row.request_sha256
            or disposition.get("callback_attempt_state")
            not in {
                CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED,
                CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS,
            }
            or disposition.get("certainty")
            not in {"proven-zero-callback-effect", "callback-effect-unknown"}
            or disposition.get("actor_scope") != "ADMIN"
        ):
            raise CallbackRecoveryConflict("callback ADMIN disposition contradicts recovery")
        if row.provider_turn_receipt_json is None or row.callback_effect_receipt_json is not None:
            raise CallbackRecoveryConflict(
                "callback-undeliverable lacks exact effect preconditions"
            )
        try:
            receipt = model_turn_receipt_contract.validate_receipt(
                _strict_json_object(row.provider_turn_receipt_json, field="provider turn receipt"),
                expected=_provider_turn_receipt_expected(row),
            )
        except model_turn_receipt_contract.ReceiptValidationError as exc:
            raise CallbackRecoveryConflict(
                "callback-undeliverable has an invalid provider receipt"
            ) from exc
        if disposition.get("provider_turn_receipt_sha256") != _digest(receipt):
            raise CallbackRecoveryConflict("callback-undeliverable provider receipt changed")
        registration = (
            _strict_json_object(
                row.callback_registration_receipt_json,
                field="callback registration receipt",
            )
            if row.callback_registration_receipt_json is not None
            else None
        )
        if (
            disposition.get("callback_registration_receipt_sha256")
            != (_digest(registration) if registration is not None else None)
            or disposition.get("callback_effect_receipt_sha256") is not None
        ):
            raise CallbackRecoveryConflict("callback-undeliverable callback observations changed")
        if disposition.get("actor_scope") != "ADMIN":
            raise CallbackRecoveryConflict("callback-undeliverable lacks ADMIN authority")
    elif row.callback_admin_disposition_json is not None:
        raise CallbackRecoveryConflict("non-terminal recovery carries an ADMIN disposition")
    if row.state == STATE_RESOLVED:
        resolution = _strict_json_object(row.resolution_json, field="resolution")
        if set(resolution) != {
            "schema",
            "outcome",
            "evidence_sha256",
            "detail",
            "resolved_at",
        }:
            raise CallbackRecoveryConflict("stored callback resolution has an unknown shape")
    elif row.resolution_json is not None:
        raise CallbackRecoveryConflict("non-resolved callback recovery carries manual resolution")
    if row.state == STATE_REFUSED:
        if row.reason_code not in _ZERO_EFFECT_REFUSAL_REASONS:
            raise CallbackRecoveryConflict(
                "refused callback recovery lacks a proven zero-effect reason"
            )
    elif (
        row.state != STATE_AMBIGUOUS
        and row.callback_attempt_state
        not in {
            CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED,
            CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS,
        }
        and row.reason_code is not None
    ):
        raise CallbackRecoveryConflict("callback recovery reason is inconsistent with its state")


def _store_refusal(db: Any, row: Any, exc: CallbackRecoveryRefused) -> None:
    if exc.reason_code not in _ZERO_EFFECT_REFUSAL_REASONS:
        row.state = STATE_AMBIGUOUS
        row.reason_code = "unclassified-refusal-manual-resolution-required"
        row.updated_at = _now()
        db.commit()
        raise CallbackRecoveryAmbiguous(
            "callback recovery refusal is not classified as proven zero effect"
        ) from exc
    row.state = STATE_REFUSED
    row.reason_code = exc.reason_code
    row.updated_at = _now()
    db.commit()


def admit(body: CallbackRecoveryRequest) -> RecoveryAdmission:
    with generation_lifecycle_claims(
        (
            (body.source_terminal_id, "model-generation", body.source_generation),
            (body.supervisor_id, "model-generation", body.supervisor_generation),
            (body.supervisor_id, "callback-target-generation", body.supervisor_generation),
            (body.supervisor_id, "pane", body.supervisor_pane_id),
        )
    ):
        return _admit_locked(body)


def _admit_locked(body: CallbackRecoveryRequest) -> RecoveryAdmission:
    """Journal intent, authorize the refusal, and enqueue under one DB lock."""
    callback_line = _callback_line(body)
    payload = _request_payload(body)
    request_sha256 = _digest(payload)
    operation_key = _operation_key(body)
    recovery_identity_sha256 = _digest(_recovery_identity(body))
    workflow_sha256 = _digest(_workflow_identity(body))

    refusal: Optional[CallbackRecoveryRefused] = None
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        existing = (
            db.query(database.CallbackRecoveryModel)
            .filter(database.CallbackRecoveryModel.operation_key == operation_key)
            .one_or_none()
        )
        if existing is not None:
            _validate_lifecycle_row(existing)
            if existing.request_sha256 != request_sha256:
                raise _identity_conflict(body, existing, reason_code="operation-key-owned")
            if existing.state == STATE_REFUSED:
                raise CallbackRecoveryRefused(
                    "this recovery operation is durably refused",
                    reason_code=str(existing.reason_code or "callback-recovery-refused"),
                )
            if existing.state == STATE_AMBIGUOUS:
                raise CallbackRecoveryAmbiguous(
                    "this recovery operation has an effect-possible ambiguous outcome"
                )
            if existing.state == STATE_RESOLVED:
                db.commit()
                return RecoveryAdmission(_operation_dict(existing), None, True)
            message_row = (
                db.query(database.InboxModel)
                .filter(database.InboxModel.id == existing.inbox_message_id)
                .one_or_none()
            )
            if message_row is None and existing.state not in RELEASABLE_STATES:
                raise CallbackRecoveryConflict("accepted recovery lost its immutable inbox row")
            db.commit()
            if message_row is None:
                return RecoveryAdmission(_operation_dict(existing), None, True)
            return RecoveryAdmission(
                _operation_dict(existing), database._inbox_message_from_row(message_row), True
            )

        consumed = (
            db.query(database.CallbackRecoveryModel)
            .filter(
                database.CallbackRecoveryModel.recovery_identity_sha256 == recovery_identity_sha256
            )
            .one_or_none()
        )
        if consumed is not None:
            _validate_lifecycle_row(consumed)
            raise _identity_conflict(body, consumed, reason_code="refusal-occurrence-owned")
        consumed_occurrence = (
            db.query(database.CallbackRecoveryModel)
            .filter(
                database.CallbackRecoveryModel.project == body.project,
                database.CallbackRecoveryModel.task_id == body.task_id,
                database.CallbackRecoveryModel.run_id == body.run_id,
                database.CallbackRecoveryModel.callback_occurrence_id
                == body.callback_occurrence_id,
            )
            .one_or_none()
        )
        if consumed_occurrence is not None:
            _validate_lifecycle_row(consumed_occurrence)
            raise _identity_conflict(
                body, consumed_occurrence, reason_code="callback-occurrence-owned"
            )
        moment = _now()
        row = database.CallbackRecoveryModel(
            operation_key=operation_key,
            operation_id=body.operation_id,
            workflow_identity_sha256=workflow_sha256,
            recovery_identity_sha256=recovery_identity_sha256,
            state=STATE_INTENT,
            project=body.project,
            task_id=body.task_id,
            run_id=body.run_id,
            source_terminal_id=body.source_terminal_id,
            source_generation=body.source_generation,
            expected_provider=body.expected_provider,
            expected_provider_session_id=body.expected_provider_session_id,
            expected_execution_mode=body.expected_execution_mode,
            supervisor_id=body.supervisor_id,
            supervisor_session=body.supervisor_session,
            supervisor_generation=body.supervisor_generation,
            supervisor_pane_id=body.supervisor_pane_id,
            refusal_control_id=body.refusal_control_id,
            refusal_occurrence_sha256=body.refusal_occurrence_sha256,
            refusal_request_sha256=body.refusal_request_sha256,
            callback_occurrence_id=body.callback_occurrence_id,
            callback_status=body.callback_status,
            callback_summary=body.callback_summary,
            callback_message_sha256=body.callback_message_sha256,
            report_path=body.report_path,
            report_sha256=body.report_sha256,
            source_head=body.source_head,
            publishing_lease_state=body.publishing_lease_state,
            publishing_lease_sha256=body.publishing_lease_sha256,
            manifest_path=body.manifest_path,
            manifest_sha256=body.manifest_sha256,
            finalization_identity_sha256=body.finalization_identity_sha256,
            request_sha256=request_sha256,
            request_identity_schema=REQUEST_SCHEMA,
            request_json=_canonical(payload).decode("utf-8"),
            callback_attempt_state=CALLBACK_ATTEMPT_NOT_REGISTERED,
            created_at=moment,
            updated_at=moment,
        )
        db.add(row)
        db.flush()
        try:
            if not os.path.isabs(body.report_path) or not os.path.isabs(body.manifest_path):
                raise CallbackRecoveryRefused(
                    "report and manifest paths must be absolute",
                    reason_code="artifact-path-invalid",
                )
            if (
                hashlib.sha256(callback_line.encode("utf-8")).hexdigest()
                != body.callback_message_sha256
            ):
                raise CallbackRecoveryRefused(
                    "callback digest does not match the canonical callback",
                    reason_code="callback-digest-mismatch",
                )
            _reservation, identity = _reservation_identity(db, body.source_terminal_id)
            expected_identity = {
                "generation": body.source_generation,
                "provider": body.expected_provider,
                "provider_session_id": body.expected_provider_session_id,
                "execution_mode": body.expected_execution_mode,
                "caller_id": body.supervisor_id,
                "session_name": body.supervisor_session,
            }
            observed_identity = {key: identity[key] for key in expected_identity}
            if observed_identity != expected_identity:
                raise CallbackRecoveryRefused(
                    "source reservation identity contradicts the requested recovery",
                    reason_code="source-identity-mismatch",
                )
            expected_workflow = {
                "project": body.project,
                "task_id": body.task_id,
                "run_id": body.run_id,
            }
            observed_workflow = {key: identity[key] for key in expected_workflow}
            if observed_workflow != expected_workflow:
                raise CallbackRecoveryRefused(
                    "recovery project/task/run contradict authoritative admission",
                    reason_code="workflow-identity-mismatch",
                )
            supervisor = _terminal_row(db, body.supervisor_id)
            if str(supervisor.tmux_session) != body.supervisor_session or getattr(
                supervisor, "caller_id", None
            ):
                raise CallbackRecoveryRefused(
                    "the authoritative supervisor is not the root caller in the reservation session",
                    reason_code="supervisor-authority-mismatch",
                )
            supervisor_generation = str(
                getattr(supervisor, "callback_target_generation", None)
                or getattr(supervisor, "generation", None)
                or ""
            )
            supervisor_pane_id = str(getattr(supervisor, "pane_id", None) or "")
            if not supervisor_generation:
                raise CallbackRecoveryRefused(
                    "authoritative supervisor generation is absent",
                    reason_code="supervisor-generation-mismatch",
                )
            if supervisor_generation != body.supervisor_generation:
                raise CallbackRecoveryRefused(
                    "authoritative supervisor generation changed before recovery admission",
                    reason_code="supervisor-generation-mismatch",
                )
            if not supervisor_pane_id or supervisor_pane_id != body.supervisor_pane_id:
                raise CallbackRecoveryRefused(
                    "authoritative supervisor pane identity changed before recovery admission",
                    reason_code="supervisor-authority-mismatch",
                )
            source = _terminal_row(db, body.source_terminal_id)
            if (
                str(source.tmux_session) != body.supervisor_session
                or str(source.caller_id or "") != body.supervisor_id
                or str(source.generation or "") != body.source_generation
            ):
                raise CallbackRecoveryRefused(
                    "source terminal caller/session/generation does not match the reservation",
                    reason_code="source-caller-mismatch",
                )
            control = control_input_service.get_control_input_journal().find(
                body.refusal_control_id
            )
            if (
                control is None
                or control.state != "refused"
                or control.reason_code != REASON_MANAGED_ACP_PANE
                or control.terminal_id != body.source_terminal_id
                or str(control.generation or "") != body.source_generation
                or control.request_sha256 != body.refusal_request_sha256
            ):
                raise CallbackRecoveryRefused(
                    "the named control is not the exact current managed-ACP refusal",
                    reason_code="refusal-identity-mismatch",
                )
            occurrence_sha256, _occurrence = _refusal_occurrence_from_record(control)
            if occurrence_sha256 != body.refusal_occurrence_sha256:
                raise CallbackRecoveryRefused(
                    "the named refusal occurrence digest does not match durable evidence",
                    reason_code="refusal-occurrence-mismatch",
                )
            callback_token = secrets.token_urlsafe(32)
            row.callback_token_sha256 = hashlib.sha256(callback_token.encode("utf-8")).hexdigest()
            prompt = _recovery_prompt(
                body,
                operation_key=operation_key,
                callback_token=callback_token,
            )
            inbox = database.InboxModel(
                sender_id=body.supervisor_id,
                receiver_id=body.source_terminal_id,
                message=prompt,
                status=MessageStatus.PENDING.value,
                message_sha256=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                sender_generation=supervisor_generation,
                expected_receiver_generation=body.source_generation,
                expected_provider_session_id=body.expected_provider_session_id,
                expected_execution_mode=body.expected_execution_mode,
                expected_provider=body.expected_provider,
                callback_recovery_key=operation_key,
                created_at=datetime.now(timezone.utc).replace(tzinfo=None),
            )
            db.add(inbox)
            db.flush()
            row.inbox_message_id = inbox.id
            row.recovery_prompt_sha256 = inbox.message_sha256
            row.message_created_at = inbox.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            row.sender_generation = supervisor_generation
            row.state = STATE_PENDING
            row.updated_at = _now()
            row.admission_response_json = json.dumps(
                _admission_response(row, inbox, replayed=False),
                sort_keys=True,
            )
            db.commit()
            db.refresh(row)
            db.refresh(inbox)
            return RecoveryAdmission(
                _operation_dict(row), database._inbox_message_from_row(inbox), False
            )
        except CallbackRecoveryRefused as exc:
            refusal = exc
            _store_refusal(db, row, exc)
    assert refusal is not None
    raise refusal


def get(operation_key: str) -> dict[str, Any]:
    with database.SessionLocal() as db:
        rows = (
            db.query(database.CallbackRecoveryModel)
            .filter(database.CallbackRecoveryModel.operation_key == operation_key)
            .all()
        )
        if len(rows) != 1:
            if not rows:
                raise CallbackRecoveryNotFound(f"no callback recovery {operation_key!r}")
            raise CallbackRecoveryConflict("duplicate callback recovery operation rows")
        row = rows[0]
        _validate_lifecycle_row(row)
        result = _operation_dict(row)
        if row.state != STATE_COMPLETED and row.inbox_message_id is not None:
            inbox = db.get(database.InboxModel, row.inbox_message_id)
            if inbox is None:
                raise CallbackRecoveryConflict("open recovery lost its immutable inbox row")
            observed = {
                "message_created_at": inbox.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                "recovery_prompt_sha256": inbox.message_sha256,
                "sender_generation": inbox.sender_generation,
            }
            expected = {key: result.get(key) for key in observed}
            if observed != expected:
                raise CallbackRecoveryConflict(
                    "recovery prompt row contradicts stored operation identity"
                )
        return result


def mark_delivery_refused(
    operation_key: str, *, reason_code: str, proven_before_provider_io: bool
) -> None:
    from cli_agent_orchestrator.services import generation_fence

    if not proven_before_provider_io or reason_code not in _ZERO_EFFECT_REFUSAL_REASONS:
        raise CallbackRecoveryConflict(
            "delivery refusal lacks exact proven-before-provider-I/O evidence"
        )
    with database.SessionLocal() as lookup:
        row = lookup.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        source_terminal_id = str(row.source_terminal_id)
        source_generation = str(row.source_generation)
        supervisor_id = str(row.supervisor_id)
        supervisor_generation = str(row.supervisor_generation)
        supervisor_pane_id = str(row.supervisor_pane_id)
    with generation_lifecycle_claims(
        (
            (source_terminal_id, "model-generation", source_generation),
            (supervisor_id, "model-generation", supervisor_generation),
            (supervisor_id, "callback-target-generation", supervisor_generation),
            (supervisor_id, "pane", supervisor_pane_id),
        )
    ):
        with generation_fence.reconciliation_critical_section(
            companion_receipts.COMPANION_DIR,
            source_terminal_id,
            source_generation,
        ):
            if turn_receipt(operation_key) is not None:
                raise CallbackRecoveryConflict(
                    "cannot refuse recovery delivery after a durable provider receipt"
                )
            _mark_delivery_refused_locked(operation_key, reason_code=reason_code)


def _mark_delivery_refused_locked(operation_key: str, *, reason_code: str) -> None:
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.state in TERMINAL_STATES:
            db.commit()
            return
        if row.state not in {STATE_INTENT, STATE_PENDING}:
            raise CallbackRecoveryConflict(
                f"cannot refuse recovery delivery from state {row.state!r}"
            )
        row.state = STATE_REFUSED
        row.reason_code = reason_code
        row.updated_at = _now()
        if row.inbox_message_id is not None:
            (
                db.query(database.InboxModel)
                .filter(
                    database.InboxModel.id == row.inbox_message_id,
                    database.InboxModel.status == MessageStatus.PENDING.value,
                )
                .update({database.InboxModel.status: MessageStatus.FAILED.value})
            )
        db.commit()


def mark_delivery_ambiguous(operation_key: str, *, reason_code: str) -> None:
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.state in TERMINAL_STATES or row.state == STATE_AMBIGUOUS:
            db.commit()
            return
        if row.state != STATE_PENDING:
            raise CallbackRecoveryConflict(
                f"cannot mark recovery ambiguous from state {row.state!r}"
            )
        row.state = STATE_AMBIGUOUS
        row.reason_code = reason_code
        row.updated_at = _now()
        db.commit()


def turn_receipt(operation_key: str) -> Optional[dict[str, str]]:
    """Return only an exact strict receipt revalidated against immutable rows."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.state == STATE_REFUSED:
            return None
        inbox = db.get(database.InboxModel, row.inbox_message_id)
        if inbox is None:
            raise CallbackRecoveryConflict("recovery inbox row is absent")
        receipt = companion_receipts.get_strict_message_ack(
            row.source_terminal_id, row.source_generation, inbox.id
        )
        if row.state == STATE_RESOLVED:
            if receipt is not None or row.provider_turn_receipt_json is not None:
                raise CallbackRecoveryConflict(
                    "zero-effect resolution contradicts a durable provider receipt"
                )
            db.commit()
            return None
        if receipt is None:
            db.commit()
            return None
        expected = _provider_turn_receipt_expected(row)
        strict = model_turn_receipt_contract.validate_receipt(receipt, expected=expected)
        if row.provider_turn_receipt_json is not None:
            try:
                stored = json.loads(
                    row.provider_turn_receipt_json,
                    object_pairs_hook=_unique_object,
                )
            except (json.JSONDecodeError, ValueError) as exc:
                raise CallbackRecoveryConflict("stored provider receipt is malformed") from exc
            model_turn_receipt_contract.validate_receipt(stored, expected=expected)
            if stored != strict:
                raise CallbackRecoveryConflict("stored provider receipt changed")
        else:
            if row.state not in {STATE_PENDING, STATE_AMBIGUOUS}:
                raise CallbackRecoveryConflict(
                    f"provider receipt cannot be attached in state {row.state!r}"
                )
            row.provider_turn_receipt_json = json.dumps(strict, sort_keys=True)
            row.state = STATE_SUBMITTED
            row.reason_code = None
            row.updated_at = _now()
        db.commit()
        return strict


def assert_provider_delivery_admissible(
    operation_key: str,
    *,
    terminal_id: str,
    generation: str,
    message_id: str,
) -> None:
    """Final lifecycle CAS checked under the provider admission lock."""
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        expected = {
            "source_terminal_id": terminal_id,
            "source_generation": generation,
            "inbox_message_id": str(message_id),
        }
        observed = {
            "source_terminal_id": row.source_terminal_id,
            "source_generation": row.source_generation,
            "inbox_message_id": str(row.inbox_message_id),
        }
        if observed != expected:
            raise CallbackRecoveryConflict(
                "provider delivery identity contradicts recovery lifecycle"
            )
        if row.state != STATE_PENDING:
            raise CallbackRecoveryRefused(
                f"provider delivery is fenced by recovery state {row.state!r}",
                reason_code="recovery-lifecycle-fenced-before-provider-io",
            )


def create_callback(operation_key: str, body: CallbackRecoveryCallbackRequest) -> dict[str, Any]:
    """Create a callback while sharing both generation teardown locks."""
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        source_terminal_id = str(row.source_terminal_id)
        source_generation = str(row.source_generation)
        supervisor_id = str(row.supervisor_id)
        supervisor_generation = str(row.supervisor_generation)
        supervisor_pane_id = str(row.supervisor_pane_id)
    with generation_lifecycle_claims(
        (
            (source_terminal_id, "model-generation", source_generation),
            (supervisor_id, "model-generation", supervisor_generation),
            (supervisor_id, "callback-target-generation", supervisor_generation),
            (supervisor_id, "pane", supervisor_pane_id),
        )
    ):
        replay = _committed_callback_replay(operation_key, body)
        if replay is not None:
            return replay
        return _create_callback_locked(operation_key, body)


def _committed_callback_replay(
    operation_key: str,
    body: CallbackRecoveryCallbackRequest,
) -> Optional[dict[str, Any]]:
    """Adopt a committed callback before any live-terminal revalidation."""
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.callback_attempt_state == CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED:
            raise CallbackRecoveryRefused(
                "the callback was already refused before registration",
                reason_code=str(row.reason_code),
            )
        supplied_token = hashlib.sha256(body.callback_token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(supplied_token, str(row.callback_token_sha256 or "")):
            raise CallbackRecoveryConflict("callback recovery producer token is invalid")
        expected = {
            "sender_id": row.source_terminal_id,
            "receiver_id": row.supervisor_id,
            "callback_occurrence_id": row.callback_occurrence_id,
            "message_sha256": row.callback_message_sha256,
        }
        observed = {
            "sender_id": body.sender_id,
            "receiver_id": body.receiver_id,
            "callback_occurrence_id": body.callback_occurrence_id,
            "message_sha256": hashlib.sha256(body.message.encode("utf-8")).hexdigest(),
        }
        if observed != expected:
            raise CallbackRecoveryConflict("callback producer identity contradicts recovery")
        if row.callback_response_json is None:
            return None
        response = _strict_json_object(row.callback_response_json, field="callback response")
        if (
            response.get("sender_id") != body.sender_id
            or response.get("receiver_id") != body.receiver_id
            or response.get("callback_occurrence_id") != body.callback_occurrence_id
        ):
            raise CallbackRecoveryConflict("recovery callback replay contradicts stored receipt")
        return {**response, "replayed": True}


def _create_callback_locked(
    operation_key: str, body: CallbackRecoveryCallbackRequest
) -> dict[str, Any]:
    """Create the callback row through the authenticated recovery-only path."""
    # The provider receipt is old-generation evidence and must be reconciled
    # before any later replacement is interpreted as a zero-effect refusal.
    receipt = turn_receipt(operation_key)
    if receipt is None:
        raise CallbackRecoveryPending("the exact recovery provider turn has no strict receipt yet")
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        supplied_token = hashlib.sha256(body.callback_token.encode("utf-8")).hexdigest()
        if not secrets.compare_digest(supplied_token, str(row.callback_token_sha256 or "")):
            raise CallbackRecoveryConflict("callback recovery producer token is invalid")
        expected = {
            "sender_id": row.source_terminal_id,
            "receiver_id": row.supervisor_id,
            "callback_occurrence_id": row.callback_occurrence_id,
        }
        observed = {
            "sender_id": body.sender_id,
            "receiver_id": body.receiver_id,
            "callback_occurrence_id": body.callback_occurrence_id,
        }
        if observed != expected:
            raise CallbackRecoveryConflict("callback producer identity contradicts recovery")
        digest = hashlib.sha256(body.message.encode("utf-8")).hexdigest()
        if digest != row.callback_message_sha256:
            raise CallbackRecoveryConflict("callback producer bytes contradict recovery")
        existing = (
            db.query(database.InboxModel)
            .filter(database.InboxModel.callback_completion_key == operation_key)
            .one_or_none()
        )
        if existing is None and row.callback_response_json is not None:
            response = _strict_json_object(row.callback_response_json, field="callback response")
            if (
                response.get("sender_id") != body.sender_id
                or response.get("receiver_id") != body.receiver_id
                or response.get("callback_occurrence_id") != body.callback_occurrence_id
            ):
                raise CallbackRecoveryConflict(
                    "recovery callback replay contradicts stored receipt"
                )
            db.commit()
            return {**response, "replayed": True}
        if existing is not None:
            if (
                existing.sender_id != body.sender_id
                or existing.receiver_id != body.receiver_id
                or existing.message != body.message
                or existing.expected_receiver_generation != row.supervisor_generation
            ):
                raise CallbackRecoveryConflict("recovery callback replay contradicts stored row")
            response = _strict_json_object(row.callback_response_json, field="callback response")
            db.commit()
            return {**response, "replayed": True}
        if row.state != STATE_SUBMITTED:
            raise CallbackRecoveryPending(f"recovery callback is not admissible from {row.state!r}")
        supervisor = _terminal_row(db, row.supervisor_id)
        live_supervisor = {
            "session": str(supervisor.tmux_session),
            "generation": str(
                getattr(supervisor, "callback_target_generation", None)
                or getattr(supervisor, "generation", None)
                or ""
            ),
            "pane_id": str(getattr(supervisor, "pane_id", None) or ""),
            "caller_id": str(getattr(supervisor, "caller_id", None) or ""),
        }
        expected_supervisor = {
            "session": row.supervisor_session,
            "generation": row.supervisor_generation,
            "pane_id": row.supervisor_pane_id,
            "caller_id": "",
        }
        if live_supervisor != expected_supervisor:
            # This is the one refusal that is proved before a callback inbox
            # row or any delivery adapter call exists.  Preserve it durably so
            # a lost response cannot be mistaken for a future eligible retry.
            row.callback_attempt_state = CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
            row.reason_code = "supervisor-generation-mismatch"
            row.updated_at = _now()
            db.commit()
            raise CallbackRecoveryRefused(
                "the original supervisor generation was replaced; callback remains held",
                reason_code="supervisor-generation-mismatch",
            )
        callback = database.InboxModel(
            sender_id=body.sender_id,
            receiver_id=body.receiver_id,
            message=body.message,
            status=MessageStatus.PENDING.value,
            message_sha256=digest,
            sender_generation=row.source_generation,
            expected_receiver_generation=row.supervisor_generation,
            callback_completion_key=operation_key,
            created_at=datetime.now(timezone.utc).replace(tzinfo=None),
        )
        db.add(callback)
        db.flush()
        response = {
            "message_id": callback.id,
            "sender_id": callback.sender_id,
            "receiver_id": callback.receiver_id,
            "created_at": callback.created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "callback_occurrence_id": row.callback_occurrence_id,
            "replayed": False,
        }
        registration = {
            "schema": "cao-callback-registration-receipt-v1",
            "operation_key": row.operation_key,
            "request_sha256": row.request_sha256,
            "callback_message_id": callback.id,
            "callback_message_sha256": callback.message_sha256,
            "callback_created_at": response["created_at"],
            "sender_id": callback.sender_id,
            "receiver_id": callback.receiver_id,
            "source_generation": row.source_generation,
            "supervisor_generation": row.supervisor_generation,
            "supervisor_pane_id": row.supervisor_pane_id,
            "callback_occurrence_id": row.callback_occurrence_id,
            "registered_at": _now(),
        }
        row.callback_message_id = callback.id
        row.callback_response_json = json.dumps(response, sort_keys=True)
        row.callback_registration_receipt_json = json.dumps(registration, sort_keys=True)
        row.callback_attempt_state = CALLBACK_ATTEMPT_REGISTERED
        row.reason_code = None
        row.updated_at = registration["registered_at"]
        db.commit()
        return response


def callback_receipt(operation_key: str) -> Optional[dict[str, Any]]:
    """Read the immutable callback creation receipt without reposting."""
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.callback_response_json is None:
            return None
        response = _strict_json_object(row.callback_response_json, field="callback response")
        return {**response, "replayed": True}


def callback_lookup(operation_key: str) -> dict[str, Any]:
    """Return the strict nullable callback-registration lookup surface."""
    with database.SessionLocal() as db:
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        callback = None
        if row.callback_registration_receipt_json is not None:
            callback = _strict_json_object(
                row.callback_registration_receipt_json, field="callback registration receipt"
            )
        return {
            "schema": CALLBACK_LOOKUP_SCHEMA,
            "operation_key": row.operation_key,
            "request_sha256": row.request_sha256,
            "callback": callback,
        }


def _validated_callback_completion(
    db: Any, row: Any, body: CallbackRecoveryCompletionRequest
) -> Any:
    """Return the exact registered callback row after validating a v2 intent."""
    if body.finalization_identity_sha256 != row.finalization_identity_sha256:
        raise CallbackRecoveryConflict("finalization identity changed")
    callback = db.get(database.InboxModel, body.callback_message_id)
    if callback is None:
        raise CallbackRecoveryConflict("callback inbox row is absent")
    observed_digest = hashlib.sha256(str(callback.message).encode("utf-8")).hexdigest()
    try:
        if not body.callback_created_at.endswith("Z"):
            raise ValueError("callback timestamp is not canonical UTC Z")
        supplied_created = datetime.fromisoformat(body.callback_created_at.replace("Z", "+00:00"))
        stored_created = callback.created_at
        if stored_created.tzinfo is None:
            stored_created = stored_created.replace(tzinfo=timezone.utc)
        created_matches = supplied_created.astimezone(timezone.utc) == stored_created.astimezone(
            timezone.utc
        )
    except (TypeError, ValueError):
        created_matches = False
    expected_callback = {
        "id": row.callback_message_id,
        "sender_id": row.source_terminal_id,
        "receiver_id": row.supervisor_id,
        "message_sha256": row.callback_message_sha256,
        "callback_completion_key": row.operation_key,
        "expected_receiver_generation": row.supervisor_generation,
    }
    observed_callback = {
        "id": callback.id,
        "sender_id": callback.sender_id,
        "receiver_id": callback.receiver_id,
        "message_sha256": observed_digest,
        "callback_completion_key": callback.callback_completion_key,
        "expected_receiver_generation": callback.expected_receiver_generation,
    }
    if (
        observed_callback != expected_callback
        or body.callback_message_sha256 != observed_digest
        or not created_matches
    ):
        raise CallbackRecoveryConflict("callback row contradicts recovery identity")
    return callback


def complete(operation_key: str, body: CallbackRecoveryCompletionRequest) -> dict[str, Any]:
    """Register a completion intent; delivery itself owns terminal completion.

    The producer endpoint proves which immutable callback is intended, but it
    cannot assert that the supervisor pane observed bytes.  The delivery path
    calls :func:`claim_callback_effect` immediately before its native bridge
    send and :func:`commit_callback_effect` only after that bridge succeeds.
    """
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.state == STATE_COMPLETED:
            stored = _json_object(row.completion_json, field="completion")
            incoming = body.model_dump(mode="json")
            if {key: stored.get(key) for key in incoming} != incoming:
                raise CallbackRecoveryConflict("completion replay contradicts stored callback")
            db.commit()
            return _operation_dict(row)
        if row.state != STATE_SUBMITTED:
            raise CallbackRecoveryPending(
                f"recovery prompt is {row.state!r}; callback completion is not admissible"
            )
        if row.callback_attempt_state not in {
            CALLBACK_ATTEMPT_REGISTERED,
            CALLBACK_ATTEMPT_EFFECT_CLAIMED,
        }:
            raise CallbackRecoveryPending("callback delivery is not registered for an exact effect")
        callback = _validated_callback_completion(db, row, body)
        intent = {
            "schema": "cao-callback-delivery-intent-v1",
            "operation_key": row.operation_key,
            "request_sha256": row.request_sha256,
            "callback_message_id": callback.id,
            "callback_message_sha256": callback.message_sha256,
            "callback_created_at": body.callback_created_at,
            "finalization_identity_sha256": body.finalization_identity_sha256,
            "registered_at": _now(),
        }
        if row.callback_disposition_json is not None:
            stored_intent = _strict_json_object(
                row.callback_disposition_json, field="callback delivery intent"
            )
            stable_keys = tuple(key for key in intent if key != "registered_at")
            if {key: stored_intent.get(key) for key in stable_keys} != {
                key: intent[key] for key in stable_keys
            }:
                raise CallbackRecoveryConflict(
                    "callback delivery intent replay contradicts storage"
                )
        else:
            row.callback_disposition_json = json.dumps(intent, sort_keys=True)
            row.updated_at = intent["registered_at"]
        db.commit()
        return _operation_dict(row)


def claim_callback_effect(operation_key: str, callback_message_id: int) -> None:
    """Durably claim the one allowed callback adapter attempt before I/O."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if (
            row.state != STATE_SUBMITTED
            or row.callback_attempt_state != CALLBACK_ATTEMPT_REGISTERED
        ):
            raise CallbackRecoveryConflict(
                "callback effect is not eligible for a first adapter attempt"
            )
        if row.callback_message_id != callback_message_id:
            raise CallbackRecoveryConflict("callback effect message contradicts registration")
        if row.callback_disposition_json is None:
            raise CallbackRecoveryConflict("callback effect lacks a completion intent")
        callback = db.get(database.InboxModel, callback_message_id)
        if callback is None or callback.status != MessageStatus.PENDING.value:
            raise CallbackRecoveryConflict("callback effect message is not pending")
        if (
            callback.callback_completion_key != row.operation_key
            or callback.message_sha256 != row.callback_message_sha256
            or callback.sender_id != row.source_terminal_id
            or callback.receiver_id != row.supervisor_id
            or callback.expected_receiver_generation != row.supervisor_generation
        ):
            raise CallbackRecoveryConflict("callback effect message contradicts registration")
        terminal = _terminal_row(db, row.supervisor_id)
        if (
            str(
                getattr(terminal, "callback_target_generation", None)
                or getattr(terminal, "generation", None)
                or ""
            )
            != row.supervisor_generation
            or str(getattr(terminal, "pane_id", None) or "") != row.supervisor_pane_id
            or str(terminal.tmux_session) != row.supervisor_session
            or getattr(terminal, "caller_id", None)
        ):
            # Registration created no pane/provider effect.  The final target
            # proof is intentionally inside the owned attempt transition, so a
            # later sweep cannot replay a replaced supervisor incarnation.
            row.callback_attempt_state = CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
            row.reason_code = "supervisor-generation-mismatch"
            row.updated_at = _now()
            db.commit()
            raise CallbackRecoveryRefused(
                "the original supervisor generation is no longer live",
                reason_code="supervisor-generation-mismatch",
            )
        row.callback_attempt_state = CALLBACK_ATTEMPT_EFFECT_CLAIMED
        row.updated_at = _now()
        db.commit()


def mark_callback_effect_ambiguous(operation_key: str) -> None:
    """Stop automatic callback replay when an adapter result is unknowable."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.callback_attempt_state == CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS:
            db.commit()
            return
        if (
            row.state != STATE_SUBMITTED
            or row.callback_attempt_state != CALLBACK_ATTEMPT_EFFECT_CLAIMED
        ):
            raise CallbackRecoveryConflict("callback effect ambiguity has no owned adapter attempt")
        row.callback_attempt_state = CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS
        row.reason_code = "callback-effect-ambiguous-manual-resolution-required"
        row.updated_at = _now()
        db.commit()


def commit_callback_effect(operation_key: str, callback_message_id: int) -> dict[str, Any]:
    """Commit the post-effect receipt and terminal transition atomically."""
    with database.SessionLocal() as db:
        db.execute(text("BEGIN IMMEDIATE"))
        row = db.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        _validate_lifecycle_row(row)
        if row.state == STATE_COMPLETED:
            if row.callback_message_id != callback_message_id:
                raise CallbackRecoveryConflict("callback effect replay contradicts completion")
            db.commit()
            return _operation_dict(row)
        if (
            row.state != STATE_SUBMITTED
            or row.callback_attempt_state != CALLBACK_ATTEMPT_EFFECT_CLAIMED
        ):
            raise CallbackRecoveryConflict("callback effect has no claimed adapter attempt")
        callback = db.get(database.InboxModel, callback_message_id)
        if callback is None or row.callback_message_id != callback.id:
            raise CallbackRecoveryConflict("callback effect row is absent or contradictory")
        if (
            callback.callback_completion_key != row.operation_key
            or callback.message_sha256 != row.callback_message_sha256
            or callback.sender_id != row.source_terminal_id
            or callback.receiver_id != row.supervisor_id
            or callback.expected_receiver_generation != row.supervisor_generation
        ):
            raise CallbackRecoveryConflict("callback effect row contradicts registration")
        completed_at = _now()
        created_at = callback.created_at.replace(tzinfo=timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        completion = {
            "schema": "cao-callback-recovery-completion-v1",
            "callback_message_id": callback.id,
            "callback_message_sha256": callback.message_sha256,
            "callback_created_at": created_at,
            "finalization_identity_sha256": row.finalization_identity_sha256,
            "source_terminal_id": row.source_terminal_id,
            "source_generation": row.source_generation,
            "supervisor_id": row.supervisor_id,
            "supervisor_generation": row.supervisor_generation,
            "supervisor_pane_id": row.supervisor_pane_id,
            "callback_occurrence_id": row.callback_occurrence_id,
            "completed_at": completed_at,
        }
        effect = {
            "schema": "cao-callback-effect-receipt-v1",
            "operation_key": row.operation_key,
            "request_sha256": row.request_sha256,
            "callback_message_id": callback.id,
            "callback_message_sha256": callback.message_sha256,
            "callback_occurrence_id": row.callback_occurrence_id,
            "supervisor_id": row.supervisor_id,
            "supervisor_session": row.supervisor_session,
            "supervisor_generation": row.supervisor_generation,
            "supervisor_pane_id": row.supervisor_pane_id,
            "delivery_adapter": "managed-provider-bridge",
            "effect_at": completed_at,
        }
        callback.status = MessageStatus.DELIVERED.value
        row.callback_consumed_at = completed_at
        row.callback_effect_receipt_json = json.dumps(effect, sort_keys=True)
        row.completion_json = json.dumps(completion, sort_keys=True)
        row.callback_attempt_state = CALLBACK_ATTEMPT_EFFECT_COMMITTED
        row.state = STATE_COMPLETED
        row.reason_code = None
        row.updated_at = completed_at
        db.commit()
        db.refresh(row)
        return _operation_dict(row)


def resolve_ambiguity(
    operation_key: str, body: CallbackRecoveryResolutionRequest
) -> dict[str, Any]:
    """Apply an explicit, evidence-bound manual zero-effect disposition."""
    from cli_agent_orchestrator.services import generation_fence

    with database.SessionLocal() as lookup:
        row = lookup.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        terminal_id = str(row.source_terminal_id)
        generation = str(row.source_generation)
    with generation_lifecycle_claim(terminal_id, generation):
        with generation_fence.reconciliation_critical_section(
            companion_receipts.COMPANION_DIR, terminal_id, generation
        ):
            with database.SessionLocal() as db:
                db.execute(text("BEGIN IMMEDIATE"))
                row = db.get(database.CallbackRecoveryModel, operation_key)
                if row is None:
                    raise CallbackRecoveryNotFound(operation_key)
                _validate_lifecycle_row(row)
                if row.state == STATE_RESOLVED:
                    stored = _strict_json_object(row.resolution_json, field="resolution")
                    incoming = body.model_dump(mode="json")
                    if {key: stored.get(key) for key in incoming} != incoming:
                        raise CallbackRecoveryConflict(
                            "manual resolution replay contradicts stored evidence"
                        )
                    db.commit()
                    return _operation_dict(row)
                if row.state != STATE_AMBIGUOUS:
                    raise CallbackRecoveryConflict(
                        f"manual resolution requires ambiguous state, not {row.state!r}"
                    )
                if row.provider_turn_receipt_json is not None:
                    raise CallbackRecoveryConflict(
                        "manual zero-effect resolution contradicts stored provider receipt"
                    )
                if row.inbox_message_id is None:
                    raise CallbackRecoveryConflict(
                        "ambiguous recovery lacks its provider message identity"
                    )
                receipt = companion_receipts.get_strict_message_ack(
                    row.source_terminal_id,
                    row.source_generation,
                    row.inbox_message_id,
                )
                if receipt is not None:
                    expected = {
                        "message_id": str(row.inbox_message_id),
                        "message_sha256": row.recovery_prompt_sha256,
                        "message_created_at": row.message_created_at,
                        "sender_id": row.supervisor_id,
                        "sender_generation": row.sender_generation,
                        "receiver_id": row.source_terminal_id,
                        "receiver_generation": row.source_generation,
                        "provider": row.expected_provider,
                        "provider_session_id": row.expected_provider_session_id,
                    }
                    strict = model_turn_receipt_contract.validate_receipt(
                        receipt, expected=expected
                    )
                    row.provider_turn_receipt_json = json.dumps(strict, sort_keys=True)
                    row.state = STATE_SUBMITTED
                    row.reason_code = None
                    row.updated_at = _now()
                    db.commit()
                    raise CallbackRecoveryConflict(
                        "manual zero-effect resolution contradicts a durable provider receipt"
                    )
                resolution = {
                    "schema": "cao-callback-recovery-resolution-v1",
                    **body.model_dump(mode="json"),
                    "resolved_at": _now(),
                }
                row.resolution_json = json.dumps(resolution, sort_keys=True)
                row.state = STATE_RESOLVED
                row.reason_code = None
                row.updated_at = resolution["resolved_at"]
                (
                    db.query(database.InboxModel)
                    .filter(
                        database.InboxModel.id == row.inbox_message_id,
                        database.InboxModel.status == MessageStatus.PENDING.value,
                    )
                    .update({database.InboxModel.status: MessageStatus.FAILED.value})
                )
                db.commit()
                db.refresh(row)
                return _operation_dict(row)


def dispose_callback_undeliverable(
    operation_key: str, body: CallbackRecoveryDispositionRequest
) -> dict[str, Any]:
    """Apply the sole ADMIN terminal for a proven provider effect with no callback effect."""
    with database.SessionLocal() as lookup:
        row = lookup.get(database.CallbackRecoveryModel, operation_key)
        if row is None:
            raise CallbackRecoveryNotFound(operation_key)
        claim_keys = (
            (str(row.source_terminal_id), "model-generation", str(row.source_generation)),
            (
                str(row.supervisor_id),
                "model-generation",
                str(row.supervisor_generation),
            ),
            (
                str(row.supervisor_id),
                "callback-target-generation",
                str(row.supervisor_generation),
            ),
            (str(row.supervisor_id), "pane", str(row.supervisor_pane_id)),
        )
    with generation_lifecycle_claims(claim_keys):
        with database.SessionLocal() as db:
            db.execute(text("BEGIN IMMEDIATE"))
            row = db.get(database.CallbackRecoveryModel, operation_key)
            if row is None:
                raise CallbackRecoveryNotFound(operation_key)
            _validate_lifecycle_row(row)
            if row.state == STATE_CALLBACK_UNDELIVERABLE:
                stored = _strict_json_object(
                    row.callback_admin_disposition_json, field="callback ADMIN disposition"
                )
                incoming = body.model_dump(mode="json")
                if {key: stored.get(key) for key in incoming} != incoming:
                    raise CallbackRecoveryConflict("ADMIN disposition replay contradicts storage")
                db.commit()
                return _operation_dict(row)
            if row.state != STATE_SUBMITTED:
                raise CallbackRecoveryConflict(
                    f"callback-undeliverable requires submitted recovery, not {row.state!r}"
                )
            if (
                row.provider_turn_receipt_json is None
                or row.callback_effect_receipt_json is not None
            ):
                raise CallbackRecoveryConflict("callback-undeliverable effect proof is incomplete")
            if row.callback_attempt_state not in {
                CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED,
                CALLBACK_ATTEMPT_EFFECT_AMBIGUOUS,
            }:
                raise CallbackRecoveryConflict("callback attempt is not disposable")
            if db.get(database.InboxModel, row.inbox_message_id) is None:
                raise CallbackRecoveryConflict("callback-undeliverable recovery inbox is absent")
            try:
                receipt = model_turn_receipt_contract.validate_receipt(
                    _strict_json_object(
                        row.provider_turn_receipt_json, field="provider turn receipt"
                    ),
                    expected=_provider_turn_receipt_expected(row),
                )
            except model_turn_receipt_contract.ReceiptValidationError as exc:
                raise CallbackRecoveryConflict(
                    "callback-undeliverable has an invalid provider receipt"
                ) from exc
            registration = (
                _strict_json_object(
                    row.callback_registration_receipt_json,
                    field="callback registration receipt",
                )
                if row.callback_registration_receipt_json is not None
                else None
            )
            disposed_at = _now()
            disposition = {
                "schema": "cao-callback-recovery-disposition-v1",
                "outcome": "provider-effect-proven-callback-undeliverable",
                "operation_key": row.operation_key,
                "request_sha256": row.request_sha256,
                "provider_turn_receipt_sha256": _digest(receipt),
                "callback_registration_receipt_sha256": (
                    _digest(registration) if registration is not None else None
                ),
                "callback_effect_receipt_sha256": None,
                "callback_attempt_state": row.callback_attempt_state,
                "certainty": (
                    "proven-zero-callback-effect"
                    if row.callback_attempt_state == CALLBACK_ATTEMPT_ZERO_EFFECT_REFUSED
                    else "callback-effect-unknown"
                ),
                "actor_scope": "ADMIN",
                **body.model_dump(mode="json"),
                "disposed_at": disposed_at,
            }
            row.callback_admin_disposition_json = json.dumps(disposition, sort_keys=True)
            row.state = STATE_CALLBACK_UNDELIVERABLE
            row.reason_code = None
            row.updated_at = disposed_at
            if row.callback_message_id is not None:
                (
                    db.query(database.InboxModel)
                    .filter(
                        database.InboxModel.id == row.callback_message_id,
                        database.InboxModel.status == MessageStatus.PENDING.value,
                    )
                    .update({database.InboxModel.status: MessageStatus.FAILED.value})
                )
            db.commit()
            db.refresh(row)
            return _operation_dict(row)


def terminal_has_open_recovery(
    terminal_id: str,
    generation: Optional[str] = None,
    *,
    _schema_retry: bool = False,
) -> bool:
    try:
        with database.SessionLocal() as db:
            source_match = database.CallbackRecoveryModel.source_terminal_id == terminal_id
            supervisor_match = database.CallbackRecoveryModel.supervisor_id == terminal_id
            if generation is not None:
                source_match = and_(
                    source_match,
                    database.CallbackRecoveryModel.source_generation == generation,
                )
                supervisor_match = and_(
                    supervisor_match,
                    database.CallbackRecoveryModel.supervisor_generation == generation,
                )
            query = db.query(database.CallbackRecoveryModel).filter(
                or_(source_match, supervisor_match)
            )
            rows = query.all()
            for row in rows:
                try:
                    _validate_lifecycle_row(row)
                except CallbackRecoveryError:
                    return True
                if row.state not in RELEASABLE_STATES:
                    return True
                if row.state == STATE_COMPLETED:
                    if (
                        row.callback_attempt_state != CALLBACK_ATTEMPT_EFFECT_COMMITTED
                        or row.callback_effect_receipt_json is None
                    ):
                        return True
            return False
    except OperationalError as exc:
        if "no such table: callback_recovery_operations" in str(exc):
            return False
        if any(
            f"no such column: callback_recovery_operations.{column}" in str(exc)
            for column in (
                "supervisor_pane_id",
                "callback_consumed_at",
                "callback_admin_disposition_json",
            )
        ):
            if _schema_retry:
                raise
            database._migrate_callback_recovery_schema()
            return terminal_has_open_recovery(terminal_id, generation, _schema_retry=True)
        raise


def held_inbox_ids() -> set[int]:
    """Inbox rows that retention must preserve while lifecycle truth is open."""
    held: set[int] = set()
    try:
        with database.SessionLocal() as db:
            for row in db.query(database.CallbackRecoveryModel).all():
                try:
                    _validate_lifecycle_row(row)
                    open_state = row.state not in RELEASABLE_STATES
                    if row.state == STATE_COMPLETED:
                        open_state = (
                            row.callback_attempt_state != CALLBACK_ATTEMPT_EFFECT_COMMITTED
                            or row.callback_effect_receipt_json is None
                        )
                except CallbackRecoveryError:
                    open_state = True
                if open_state and row.inbox_message_id is not None:
                    held.add(int(row.inbox_message_id))
                callback = (
                    db.query(database.InboxModel.id)
                    .filter(database.InboxModel.callback_completion_key == row.operation_key)
                    .one_or_none()
                )
                if open_state and callback is not None:
                    held.add(int(callback[0]))
    except OperationalError as exc:
        if "no such table: callback_recovery_operations" not in str(exc):
            raise
    return held


def current_delivery_binding_matches(message: InboxMessage) -> bool:
    if message.callback_recovery_key is None and message.callback_completion_key is None:
        return False
    try:
        with database.SessionLocal() as db:
            if message.callback_completion_key is not None:
                recovery = db.get(
                    database.CallbackRecoveryModel,
                    message.callback_completion_key,
                )
                if recovery is None:
                    return False
                _validate_lifecycle_row(recovery)
                terminal = _terminal_row(db, message.receiver_id)
                live_generation = str(
                    getattr(terminal, "callback_target_generation", None)
                    or getattr(terminal, "generation", None)
                    or ""
                )
                live_pane_id = str(getattr(terminal, "pane_id", None) or "")
                return (
                    recovery.state == STATE_SUBMITTED
                    and recovery.callback_attempt_state
                    in {
                        CALLBACK_ATTEMPT_REGISTERED,
                        CALLBACK_ATTEMPT_EFFECT_CLAIMED,
                    }
                    and message.receiver_id == recovery.supervisor_id
                    and message.sender_id == recovery.source_terminal_id
                    and message.expected_receiver_generation == recovery.supervisor_generation
                    and live_generation == recovery.supervisor_generation
                    and live_pane_id == recovery.supervisor_pane_id
                    and str(terminal.tmux_session) == recovery.supervisor_session
                    and not getattr(terminal, "caller_id", None)
                )
            _row, identity = _reservation_identity(db, message.receiver_id)
        return (
            identity["generation"] == message.expected_receiver_generation
            and identity["provider_session_id"] == message.expected_provider_session_id
            and identity["execution_mode"] == message.expected_execution_mode
            and identity["provider"] == message.expected_provider
        )
    except CallbackRecoveryError:
        return False


def binding_matches(
    terminal_id: str,
    *,
    generation: str,
    provider: str,
    provider_session_id: str,
    execution_mode: str,
) -> bool:
    try:
        with database.SessionLocal() as db:
            _row, identity = _reservation_identity(db, terminal_id)
        return (
            identity["generation"] == generation
            and identity["provider"] == provider
            and identity["provider_session_id"] == provider_session_id
            and identity["execution_mode"] == execution_mode
        )
    except CallbackRecoveryError:
        return False
