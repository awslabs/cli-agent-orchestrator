"""Subprocess-lifecycle engine for the script tier (issue #312, Bolt 3 / U4, C1).

Owns the ONLY component that spawns and signals an OS process. Composes five
algorithms + two helpers, driven from the single blocking POST /workflows/runs.
Never runs a script in-process (INV-1); constructed env only (INV-2); terminals
only via terminal_service (INV-3); best-effort teardown/journal never raise into
the drive path (INV-4); one tier-neutral result shape (INV-5); generation
monotonic through every (re)spawn/cancel/timeout (INV-6).

The five algorithms + two helpers (business-logic-model A1-A7):

- A1 ``run_script_workflow`` — lint gate -> journal row -> spawn -> serve
  run-step calls while awaiting exit -> sentinel scan -> ``WorkflowRunResult``.
- A2 ``resume_script_run`` — typed admission (delegated to U3) -> generation
  bump -> materialize frozen snapshot -> re-spawn with ``CAO_WORKFLOW_RESUME=1``.
- A3 ``cancel_script_run`` — signal-first -> sweep -> journal CANCELLED,
  idempotent, never raises into the caller.
- A4 ``_terminate`` — shared SIGTERM -> grace -> SIGKILL escalation.
- A5 ``_reconcile_orphans`` — best-effort teardown of in-flight terminals.
- A6 ``_scan_sentinel`` — last-match ``CAO_WORKFLOW_OUTPUT:`` scan (exit 0 only).
- A7 ``_pump`` / ``_RingBuffer`` — bounded, concurrent pipe drain (no deadlock).

U4 raises typed admission errors (``ScriptLintError``, ``ResumeNotAllowedError``,
``ResumeCorruptError``, ``KeyError``) for U5 to map to HTTPException; a run
FAILURE/timeout/cancel is NEVER an exception — it returns a FAILED/CANCELLED
``WorkflowRunResult`` (base discipline).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    WORKFLOW_SCRIPT_LOG_CAP,
    WORKFLOW_SCRIPT_SCRATCH_DIR,
    WORKFLOW_SCRIPT_TERM_GRACE,
    WORKFLOW_SCRIPT_TIMEOUT,
)
from cli_agent_orchestrator.models.workflow_runtime import (
    RunState,
    StepResult,
    WorkflowRunResult,
)
from cli_agent_orchestrator.services import terminal_service, workflow_journal
from cli_agent_orchestrator.services.script_lint import lint_script
from cli_agent_orchestrator.services.step_output_store import _validate_key_part
from cli_agent_orchestrator.services.workflow_service import (
    ResumeCorruptError,
    ResumeNotAllowedError,
    StepRunState,
    _active_drives,
    _is_resumable_for_tier,
    run_registry,
)

logger = logging.getLogger(__name__)

# The stdout sentinel prefix a script prints to return a run-level output value
# (ADR-4, BR-7). Last-match-wins on exit 0 (A6).
_SENTINEL_PREFIX = "CAO_WORKFLOW_OUTPUT:"

# Terminal step states — a step in one of these is NOT in-flight and its terminal
# has already been released, so the orphan sweep skips it (A5, BR-14).
_TERMINAL_STEP_STATES = {"completed", "failed", "skipped", "completed_unvalidated"}


class ScriptLintError(Exception):
    """The pre-spawn lint gate failed (BR-1) — U5 maps this to 422 with findings.

    The ONLY exception ``run_script_workflow`` raises: it fires BEFORE any journal
    row or subprocess exists, so zero script code ran (BR-1). Carries the U1
    ``findings`` list so U5 can render the 422 body.
    """

    def __init__(self, findings: List[Any]) -> None:
        super().__init__("workflow script failed lint; run rejected before execution")
        self.findings = findings


class TimeoutBound(Exception):
    """Raised by ``_await_exit_within_bound`` when the wall-clock bound elapses.

    Internal control-flow signal only — never crosses the U4 boundary. The
    timeout arm converts it to a FAILED ``WorkflowRunResult`` (kind=timeout).
    """


# ---------------------------------------------------------------------------
# E1 — ScriptRunRecord (in-memory registry entry, Q6=A). NEVER persisted.
# ---------------------------------------------------------------------------
@dataclass
class ScriptRunRecord:
    """The live, in-memory record for a running script (domain-entities E1).

    Registered in the SAME tier-tagged ``run_registry`` YAML runs use (Q6=A). It
    holds a live ``Process`` handle so it is never persisted; the journal (U3) is
    the sole durable truth. Carries the FULL attribute surface the base
    ``get_run_status`` snapshot + cancel dispatch read (``state``, ``cancelled``,
    ``current_step_id``, ``step_states``, timestamps) so those work unmodified on
    a script record. NO persistent ``source``/``path`` field (BR-30) — the durable
    source lives in the journal's ``spec_snapshot``.
    """

    run_id: str
    workflow_name: str
    state: RunState
    cancelled: bool
    current_step_id: Optional[str]
    step_states: Dict[str, StepRunState]
    process: Optional[asyncio.subprocess.Process]
    generation: str
    started_at: str
    finished_at: Optional[str]
    tier: str = "script"


# ---------------------------------------------------------------------------
# A7 — bounded ring-buffer capture (Q7=A, NFR-REL-1 intent)
# ---------------------------------------------------------------------------
class _RingBuffer:
    """A bounded, tail-retaining byte buffer for one subprocess stream (E3).

    Appends chunks; once the accumulated size exceeds ``cap`` the oldest bytes
    are dropped and ``truncated`` latches True. ``text()`` decodes the retained
    tail and, when truncated, prepends a one-line marker so a reader (sentinel
    scan / error field) knows the head was dropped. Bounding memory this way is
    what stops a chatty/runaway child from OOMing the single API process.
    """

    __slots__ = ("_buf", "_cap", "truncated")

    def __init__(self, cap: int) -> None:
        self._buf = bytearray()
        self._cap = cap
        self.truncated = False

    def append(self, chunk: bytes) -> None:
        self._buf.extend(chunk)
        if len(self._buf) > self._cap:
            # Drop the oldest overflow, keep the last ``cap`` bytes (tail).
            overflow = len(self._buf) - self._cap
            del self._buf[:overflow]
            self.truncated = True

    def text(self) -> str:
        tail = self._buf.decode("utf-8", errors="replace")
        if self.truncated:
            return (
                "[... output truncated: exceeded "
                f"{self._cap} bytes; showing tail only ...]\n" + tail
            )
        return tail


async def _pump(stream: Optional[asyncio.StreamReader], ring: _RingBuffer) -> None:
    """Drain one subprocess pipe into a bounded ring buffer (A7, M2 no-deadlock).

    Runs as a dedicated asyncio reader task for the life of the process, so both
    pipes are drained CONCURRENTLY with the exit await. A literal ``await exit;
    then read`` deadlocks: once the ~64KB OS pipe buffer fills, the child blocks
    on write while U4 waits for an exit that can never arrive. Reads in bounded
    chunks (never ``.read()`` unbounded) so the ring cap actually bounds memory.
    """
    if stream is None:
        return
    while True:
        chunk = await stream.read(65536)
        if not chunk:  # EOF — the write end closed
            return
        ring.append(chunk)


# ---------------------------------------------------------------------------
# A6 — sentinel last-match scan (Q2=A, ADR-4)
# ---------------------------------------------------------------------------
def _scan_sentinel(stdout_text: str) -> Tuple[Optional[Any], List[str]]:
    """Extract the run-level ``output`` from the captured stdout tail (A6).

    Last-match-wins over lines prefixed ``CAO_WORKFLOW_OUTPUT:`` (robust to a
    script that prints progress then a final result). Zero matches -> ``(None,
    [])`` (absent -> null, ADR-4). A last line whose payload is not valid JSON
    keeps the run COMPLETED (exit 0 already succeeded — the author's encoding bug
    is not a run failure) but records ``output=None`` + a warnings note so the bug
    stays visible (BR-9). Only ever reached on exit 0 (BR-9a).
    """
    matches = [line for line in stdout_text.splitlines() if line.startswith(_SENTINEL_PREFIX)]
    if not matches:
        return (None, [])
    payload = matches[-1][len(_SENTINEL_PREFIX) :]
    try:
        return (json.loads(payload), [])
    except (json.JSONDecodeError, ValueError):
        return (
            None,
            [
                "malformed sentinel payload: CAO_WORKFLOW_OUTPUT: line present but "
                "not valid JSON — output recorded as null"
            ],
        )


# ---------------------------------------------------------------------------
# Env construction (INV-2) — constructed allowlist, nothing inherited-and-extended
# ---------------------------------------------------------------------------
def _build_env(run_id: str, generation: str, *, resume: bool = False) -> Dict[str, str]:
    """Build the exact 5-key constructed spawn env (INV-2, NFR-SEC-2, BR-26).

    The spawn env is CONSTRUCTED, never ``os.environ`` inherited-and-extended:
    exactly ``{CAO_WORKFLOW_RUN_ID, CAO_WORKFLOW_GENERATION, CAO_API_BASE_URL,
    PATH, HOME}`` (+ ``CAO_WORKFLOW_RESUME=1`` on resume). No secret in the API
    process environment can leak into the child. ``PATH``/``HOME`` are the OS
    floor a Python subprocess needs to exec + resolve its interpreter/home; they
    are deliberately NOT in U2's forwarded allowlist (a script that tries to
    forward them on a run-step call is 422'd — the two-clause fence, B1).
    """
    env = {
        "CAO_WORKFLOW_RUN_ID": run_id,
        "CAO_WORKFLOW_GENERATION": generation,
        "CAO_API_BASE_URL": API_BASE_URL,
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    if resume:
        env["CAO_WORKFLOW_RESUME"] = "1"
    return env


def _now() -> str:
    """ISO-8601 Z timestamp (bookkeeping only — never an ordering key)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _bump(generation: str) -> str:
    """Monotonically bump a string generation (INV-6). Non-int -> restart at '1'->'2'."""
    try:
        return str(int(generation) + 1)
    except (ValueError, TypeError):
        # A corrupt/non-integer generation must still advance so a straggler is
        # fenced; anchor to "2" (one past the "1" default) rather than raising.
        return "2"


# ---------------------------------------------------------------------------
# A4 — _terminate (shared SIGTERM -> grace -> SIGKILL escalation, Q3=A)
# ---------------------------------------------------------------------------
async def _terminate(process: asyncio.subprocess.Process, grace: float) -> None:
    """Escalate a subprocess to exit within ``grace`` (A4, BR-10/11/12).

    Signals the OS PROCESS (``record.process``), NOT the process group: a group
    kill could reach the API server's own session (Q3=A). Child agent terminals
    are torn down explicitly by the sweep (A5), not by a group signal. Cooperative
    SIGTERM first, then a hard SIGKILL if the child does not exit within ``grace``.
    Used identically by the timeout reaper and cancel, so the observable total
    bound stays the single value ``WORKFLOW_SCRIPT_TIMEOUT + TERM_GRACE``.
    """
    if process.returncode is not None:
        return  # already exited — nothing to signal or reap
    try:
        process.terminate()  # SIGTERM — cooperative
    except ProcessLookupError:
        return  # raced to exit between the check and the signal
    try:
        await asyncio.wait_for(process.wait(), timeout=grace)
    except asyncio.TimeoutError:
        try:
            process.kill()  # SIGKILL — hard stop
        except ProcessLookupError:
            return
        await process.wait()  # reap the zombie


async def _await_exit_within_bound(process: asyncio.subprocess.Process, timeout: float) -> None:
    """Await the process exit under the wall-clock bound (A1 Step 3 reaper).

    A thin ``asyncio.wait_for(process.wait())`` wrapper that converts the elapsed
    bound into a ``TimeoutBound`` the caller's timeout arm handles (reap ->
    sweep -> FAILED,kind=timeout). Any other exit (natural, signal) returns.
    """
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutBound(
            f"script subprocess did not exit within {timeout}s wall-clock bound"
        ) from e


# ---------------------------------------------------------------------------
# A5 — orphan reconciliation sweep (_reconcile_orphans, Q4=A, FR-1.5)
# ---------------------------------------------------------------------------
async def _reconcile_orphans(run_id: str) -> None:
    """Tear down in-flight step terminals on any abnormal end (A5, best-effort).

    Keyed off the IN-FLIGHT step set (not a single ``current_step_id``) so
    concurrent fan-out terminals are all reclaimed (Q4=A, BR-14). The live source
    of ``terminal_id`` is the in-memory ``ScriptRunRecord.step_states`` (BR-31
    fallback 5b, code-generation-plan CONTRADICTION #5): a crash means the API
    process (and its child terminals) are gone anyway, so the durable case is
    moot. Falls back to the journal's step rows if no live record is present.

    Honors the project non-blocking Mandate: every failure is logged + swallowed
    (``# noqa: BLE001``) — a teardown failure NEVER fails the run (INV-4).
    """
    try:
        terminal_ids: List[str] = []
        record = run_registry.get(run_id)
        if isinstance(record, ScriptRunRecord):
            for st in record.step_states.values():
                if st.terminal_id is not None and st.state.value not in _TERMINAL_STEP_STATES:
                    terminal_ids.append(st.terminal_id)
        else:
            # No live record (e.g. rebuilt-then-discarded) — best-effort journal read.
            steps = await asyncio.to_thread(workflow_journal.get_steps, run_id)
            for srow in steps:
                # StepRow carries no terminal_id column (BR-31 5b), so this branch
                # can only reclaim terminals if a durable source is ever added (5a).
                tid = getattr(srow, "terminal_id", None)
                if tid is not None and srow.state not in _TERMINAL_STEP_STATES:
                    terminal_ids.append(tid)
            if not terminal_ids:
                # The journal has no durable terminal_id source (BR-31 5b), so this
                # fallback can reclaim nothing — log it so operators aren't misled
                # into thinking a sweep happened when there was no source to sweep.
                logger.info(
                    "orphan sweep: run '%s' has no live record and no durable "
                    "terminal_id source (journal fallback reclaimed nothing)",
                    run_id,
                )

        for terminal_id in terminal_ids:
            try:
                terminal_service.delete_terminal(terminal_id)
            except (
                Exception
            ) as exc:  # noqa: BLE001 — teardown is best-effort; never fail the run (INV-4)
                logger.warning(
                    "orphan sweep: run '%s' failed to tear down terminal '%s': %s",
                    run_id,
                    terminal_id,
                    exc,
                )
    except (
        Exception
    ):  # noqa: BLE001 — non-blocking Mandate: the sweep never raises into the drive path
        logger.warning(
            "orphan reconciliation for run '%s' failed (best-effort)", run_id, exc_info=True
        )


# ---------------------------------------------------------------------------
# BR-31 in-memory terminal recorder — wired into the server-side run-step path
# ---------------------------------------------------------------------------
def make_step_terminal_recorder(
    env_vars: Optional[Dict[str, str]],
) -> Optional[Callable[[str], None]]:
    """Build the ``on_terminal_created`` callback for a script-tier run-step call.

    Returns ``None`` (no-op) unless the call carries both ``CAO_WORKFLOW_RUN_ID``
    and ``CAO_WORKFLOW_STEP_ID`` AND that run is a live ``ScriptRunRecord`` in the
    registry — i.e. only genuine script run-step calls record a terminal for the
    sweep. The returned callback records the created ``terminal_id`` into the
    shared record's ``step_states[step_id]`` at creation time (BR-31), creating a
    RUNNING ``StepRunState`` if the key is not yet present so a mid-flight call is
    visible even before its first journal write.
    """
    if not env_vars:
        return None
    run_id = env_vars.get("CAO_WORKFLOW_RUN_ID")
    step_id = env_vars.get("CAO_WORKFLOW_STEP_ID")
    if not run_id or not step_id:
        return None
    record = run_registry.get(run_id)
    if not isinstance(record, ScriptRunRecord):
        return None

    def _record(terminal_id: str) -> None:
        from cli_agent_orchestrator.models.workflow import StepState

        st = record.step_states.get(step_id)
        if st is None:
            st = StepRunState(step_id=step_id, state=StepState.RUNNING)
            record.step_states[step_id] = st
        st.terminal_id = terminal_id

    return _record


# ---------------------------------------------------------------------------
# _materialize_snapshot (BR-30) — engine-owned temp file, 0o600 under 0o700 root
# ---------------------------------------------------------------------------
def _materialize_snapshot(run_id: str, source: str) -> str:
    """Write the frozen ``spec_snapshot.source`` to an engine-owned temp file (BR-30).

    Resume re-drives the FROZEN snapshot, not the author's on-disk file (INV-7):
    a naive re-exec of an edited file would mass-trigger ``ReplayDivergenceError``.
    The temp file lives under ``WORKFLOW_SCRIPT_SCRATCH_DIR`` (0o700, created if
    absent) with mode 0o600 so a co-tenant cannot read or swap the source between
    materialize and exec. The filename is derived from the engine-validated
    ``run_id`` (no author-controllable path segment — the scratch path is an
    engine-GENERATED category, distinct from the author-supplied-path validator
    Mandate). The caller deletes it in a ``finally`` after reap.
    """
    scratch = WORKFLOW_SCRIPT_SCRATCH_DIR
    scratch.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(scratch, 0o700)
    except OSError as exc:
        # Non-fatal: the dir exists; log if we could not tighten its mode.
        logger.warning("script scratch dir '%s' chmod 0o700 failed: %s", scratch, exc)
    path = scratch / f"resume-{run_id}.py"
    # Open with O_CREAT|O_EXCL-free write but an explicit restrictive mode: create
    # owner-only, truncating any stale file from a prior aborted resume.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except OSError:
        # os.fdopen failed to wrap the fd, so it never took ownership — close the
        # raw fd ourselves to avoid a descriptor leak, then re-raise.
        os.close(fd)
        raise
    with fh:
        fh.write(source)
    # Re-assert 0o600 in case an inherited umask widened O_CREAT's mode.
    try:
        os.chmod(path, 0o600)
    except OSError as exc:
        logger.warning("resume snapshot '%s' chmod 0o600 failed: %s", path, exc)
    return str(path)


def _delete_temp_file(path: Optional[str]) -> None:
    """Best-effort delete of a materialized snapshot temp file (BR-30 lifecycle)."""
    if path is None:
        return
    try:
        os.unlink(path)
    except FileNotFoundError:
        return
    except (
        OSError
    ) as exc:  # noqa: BLE001 — cleanup is best-effort; a leaked temp file must not fail resume
        logger.warning("failed to delete resume snapshot temp file '%s': %s", path, exc)


# ---------------------------------------------------------------------------
# _finalize (INV-5) — construct the tier-neutral WorkflowRunResult
# ---------------------------------------------------------------------------
def _journal_run_state(record: ScriptRunRecord) -> None:
    """Best-effort terminal-state write-through (INV-4/INV-5). Never raises."""
    try:
        workflow_journal.update_run_state(record.run_id, record.state.value, record.finished_at)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; result still returned (INV-4)
        logger.warning(
            "journal: script run '%s' terminal state write failed (resumability degraded): %s",
            record.run_id,
            e,
        )


def _build_steps(record: ScriptRunRecord) -> List[StepResult]:
    """Aggregate the record's per-step states into the result's step list."""
    steps: List[StepResult] = []
    for step_id, st in record.step_states.items():
        steps.append(
            StepResult(
                id=step_id,
                state=st.state,
                attempts=st.attempts,
                output=st.output.output if st.output is not None else None,
                error=st.error,
            )
        )
    return steps


async def _finalize(
    record: ScriptRunRecord,
    *,
    state: RunState,
    kind: Optional[str],
    output: Optional[Any] = None,
    warnings: Optional[List[str]] = None,
    error: Optional[str] = None,
) -> WorkflowRunResult:
    """Settle the record to a terminal state and build the tier-neutral result (INV-5).

    Writes the terminal run state through U3's write-through (best-effort), sets
    ``finished_at``, leaves the record in the registry for a bounded status
    window, and constructs the SAME ``WorkflowRunResult`` shape a YAML run returns
    plus the additive ``kind``/``output``/``warnings`` fields (E2). A script
    failure/timeout/cancel NEVER raises — it returns a FAILED/CANCELLED result.
    """
    record.state = state
    record.current_step_id = None
    record.finished_at = _now()
    await asyncio.to_thread(_journal_run_state, record)
    # ``WorkflowRunResult`` has no top-level ``error`` field (per-step only), so a
    # run-level error (stderr tail on crash/timeout) is surfaced in ``warnings`` —
    # the FAILED state + ``kind`` already carry the failure semantics; the tail is
    # the diagnostic detail (US-B5 observability).
    all_warnings = list(warnings or [])
    if error:
        all_warnings.append(error)
    return WorkflowRunResult(
        run_id=record.run_id,
        workflow_name=record.workflow_name,
        state=state,
        steps=_build_steps(record),
        started_at=record.started_at,
        finished_at=record.finished_at,
        kind=kind,
        output=output,
        warnings=all_warnings,
    )


# ---------------------------------------------------------------------------
# Shared drive: spawn -> concurrent drain -> reap -> exit interp -> finalize
# ---------------------------------------------------------------------------
async def _drive_process(
    record: ScriptRunRecord, script_path: str, env: Dict[str, str]
) -> WorkflowRunResult:
    """Spawn, drain both pipes concurrently, reap under the bound, interpret exit.

    THE single execution path for both a fresh run (A1) and a resume (A2) — the
    only difference is the env (``CAO_WORKFLOW_RESUME``) and the script path
    (author file vs materialized snapshot). Never ``shell=True`` (C-2).
    """
    try:
        record.process = await asyncio.create_subprocess_exec(
            sys.executable,
            script_path,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (
        Exception
    ) as exc:  # noqa: BLE001 — spawn failure should not raise past the runner boundary
        # The OS/interpreter refused to spawn (e.g. the executable vanished, or a
        # bad exec argument) — this is a run failure, not an engine invariant
        # violation (module contract: only the lint gate and admission gates
        # raise). Sweep any already-recorded in-flight terminals and settle FAILED.
        logger.warning("script run '%s' failed to spawn: %s", record.run_id, exc)
        await _reconcile_orphans(record.run_id)
        return await _finalize(
            record,
            state=RunState.FAILED,
            kind="error",
            error=f"spawn failed: {exc}",
        )
    process = record.process

    stdout_ring = _RingBuffer(WORKFLOW_SCRIPT_LOG_CAP)
    stderr_ring = _RingBuffer(WORKFLOW_SCRIPT_LOG_CAP)
    # Drain BOTH pipes concurrently for the life of the process (M2 no-deadlock).
    drain = [
        asyncio.create_task(_pump(process.stdout, stdout_ring)),
        asyncio.create_task(_pump(process.stderr, stderr_ring)),
    ]

    try:
        await _await_exit_within_bound(process, WORKFLOW_SCRIPT_TIMEOUT)
        await asyncio.gather(*drain)  # flush both tails after a clean exit
    except TimeoutBound:
        # Timeout arm: reap -> sweep -> bump+persist generation (INV-6, the
        # straggler fence a timeout-reaped run needs) -> FAILED,kind=timeout.
        await _terminate(process, WORKFLOW_SCRIPT_TERM_GRACE)
        for task in drain:
            task.cancel()
        await asyncio.gather(*drain, return_exceptions=True)
        await _reconcile_orphans(record.run_id)
        record.generation = _bump(record.generation)
        await _persist_generation_best_effort(record)
        stderr_tail = stderr_ring.text()
        return await _finalize(
            record,
            state=RunState.FAILED,
            kind="timeout",
            error=(stderr_tail + "\n[wall-clock timeout]").strip(),
            warnings=[f"run exceeded the {WORKFLOW_SCRIPT_TIMEOUT}s wall-clock bound"],
        )

    if record.cancelled or record.state == RunState.CANCELLED:
        # A concurrent cancel_script_run already signalled, swept, and journaled
        # CANCELLED (A3) — the drive must not overwrite that with FAILED/COMPLETED
        # just because the process happened to exit after the cancel fired.
        await _reconcile_orphans(record.run_id)
        return await _finalize(record, state=RunState.CANCELLED, kind="cancelled")

    rc = process.returncode
    if rc == 0:
        output, warnings = _scan_sentinel(stdout_ring.text())
        return await _finalize(
            record, state=RunState.COMPLETED, kind=None, output=output, warnings=warnings
        )
    # Nonzero / signal death -> sweep -> FAILED,kind=error (sentinel SKIPPED, BR-9a).
    await _reconcile_orphans(record.run_id)
    return await _finalize(
        record,
        state=RunState.FAILED,
        kind="error",
        error=stderr_ring.text().strip(),
    )


async def _persist_generation_best_effort(record: ScriptRunRecord) -> None:
    """Persist a bumped generation on the timeout arm (best-effort, INV-4/INV-6).

    Unlike the load-bearing pre-spawn/cancel bumps (which surface a persist
    failure to the caller), the timeout-arm bump happens while finalizing a run
    that already failed — a persist failure here only degrades the straggler
    fence for an already-dead run, so it is logged + swallowed rather than raised.
    """
    from cli_agent_orchestrator.services.workflow_service import update_run_generation

    try:
        await asyncio.to_thread(update_run_generation, record.run_id, record.generation)
    except (
        Exception
    ) as e:  # noqa: BLE001 — timeout-arm gen persist is best-effort; run already FAILED (INV-4)
        logger.warning(
            "journal: timeout-arm generation bump persist for run '%s' failed: %s",
            record.run_id,
            e,
        )


# ---------------------------------------------------------------------------
# A1 — run_script_workflow (S1 flow, M1 + M2 run-path gate)
# ---------------------------------------------------------------------------
async def run_script_workflow(spec: Any, inputs: Dict[str, Any], run_id: str) -> WorkflowRunResult:
    """Run a script workflow to completion, awaited inline (A1, S1, US-B1/B4/B5).

    ``spec`` is the resolved ``ScriptSpec`` (U5/C4) — duck-typed here (U5 owns the
    concrete model): it exposes ``.source`` (script text), ``.path`` (display path
    for lint messages + exec), ``.name``, and optionally ``.content_hash``.

    Steps: (0) lint gate — a ``fail`` raises ``ScriptLintError`` before any journal
    row or subprocess (BR-1); (1) journal the run row (tier=script, gen=1) +
    register the live record; (2) spawn with the constructed env (INV-2); (3) drain
    both pipes concurrently while awaiting exit under the wall-clock bound; (4)
    interpret the exit + sentinel scan. Only the lint gate raises — a run
    failure/timeout returns a FAILED result.
    """
    # --- Step -1: validate the run_id key BEFORE any journal/registry/path use
    # (shared validator, mirrors base start_run at workflow_service.py:713). A
    # traversal run_id would flow into the resume snapshot path and exec — reject
    # at the earliest boundary. Raises ValueError (-> 400) at the U5 boundary. ---
    _validate_key_part(run_id, "run_id")

    # --- Step 0: lint gate (M2 on the run path, US-B4 AC-1, BR-1) ---
    result = lint_script(spec.source, spec.path)
    if result.status == "fail":
        raise ScriptLintError(result.findings)  # ZERO code ran, no journal row yet

    # --- Step 1: register the live record + journal the durable run row ---
    record = ScriptRunRecord(
        run_id=run_id,
        workflow_name=spec.name,
        state=RunState.RUNNING,
        cancelled=False,
        current_step_id=None,
        step_states={},
        process=None,
        generation="1",
        started_at=_now(),
        finished_at=None,
        tier="script",
    )
    # M3 (traceability): a registered record lives for the process lifetime — it is
    # NOT evicted on finalize, mirroring the base YAML registry, so a bounded
    # post-run status window keeps serving. Registry eviction/TTL is deferred to
    # U5/base scope (no per-tier eviction here).
    run_registry[run_id] = record

    # The durable spec_snapshot carries the frozen source (resume reads it back).
    spec_snapshot = json.dumps(
        {
            "source": spec.source,
            "path": spec.path,
            "content_hash": getattr(spec, "content_hash", None),
        }
    )
    try:
        await asyncio.to_thread(
            workflow_journal.insert_run,
            run_id,
            spec.name,
            spec_snapshot,
            json.dumps(inputs),
            RunState.RUNNING.value,
            record.started_at,
            "script",
            "1",
        )
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal insert is best-effort; live floor still serves (INV-4)
        logger.warning("journal: script insert_run for '%s' failed (run continues): %s", run_id, e)

    # --- Step 2: spawn (constructed env) + Step 3/4: drive, reap, interpret ---
    # Mark the drive live for the whole spawn->reap window so Gate-2 of a
    # concurrent resume sees this run as executing (b4c1 liveness truth, mirrors
    # base start_run at workflow_service.py:755). The ``finally`` clears it on
    # EVERY exit path (complete, fail, timeout) so a settled run stays resumable.
    env = _build_env(run_id, "1", resume=False)
    _active_drives.add(run_id)
    try:
        return await _drive_process(record, spec.path, env)
    finally:
        _active_drives.discard(run_id)


# ---------------------------------------------------------------------------
# A2 — resume_script_run (S2 flow, M3, US-C1/C2)
# ---------------------------------------------------------------------------
async def resume_script_run(run_id: str) -> WorkflowRunResult:
    """Resume a crashed/failed/cancelled script run from its journal (A2, S2).

    Admission is DELEGATED entirely to U3 (Q8=A, BR-27): U4 open-codes no inline
    liveness/terminal-state/corrupt check. The two-gate admission
    (code-generation-plan CONTRADICTION #1 — reconciled against the REAL code):

    1. ``get_run(run_id)`` is None -> ``KeyError`` -> 404 (run absent).
    2. ``run_id in _active_drives`` -> ``ResumeNotAllowedError`` -> 409 (the live
       registry is the b4c1 liveness truth; ``_is_resumable_for_tier`` documents
       that it does NOT do the liveness check — that is the caller's job).
    3. ``not _is_resumable_for_tier(row)`` -> ``ResumeNotAllowedError`` -> 409
       (terminal-state / tier decision — the single delegated predicate).
    4. A corrupt ``spec_snapshot`` -> ``ResumeCorruptError`` -> 422.

    Execution (only after admission): bump + persist generation BEFORE spawn
    (INV-6); materialize the frozen snapshot to an engine-owned temp file (BR-30);
    re-spawn with ``CAO_WORKFLOW_RESUME=1``; drive as A1; delete the temp file in
    a ``finally`` after reap. The replay/fence machinery is U3's, server-side.
    """
    from cli_agent_orchestrator.services.workflow_service import update_run_generation

    # --- Gate 0: validate the run_id key BEFORE any journal/registry/path use
    # (shared validator, mirrors base resume_from_last_completed at
    # workflow_service.py:1057). A traversal run_id (e.g. "../../../tmp/evil")
    # would otherwise flow into scratch/resume-{run_id}.py and get exec'd —
    # arbitrary file write + code exec. Raises ValueError (-> 400). ---
    _validate_key_part(run_id, "run_id")

    # --- Gate 1: run absent -> 404 ---
    row = await asyncio.to_thread(workflow_journal.get_run, run_id)
    if row is None:
        raise KeyError(f"unknown run_id '{run_id}'")

    # --- Gate 2: liveness (b4c1) -> 409. The shared _active_drives set is the
    # single liveness truth; _is_resumable_for_tier deliberately does NOT check it.
    if run_id in _active_drives:
        raise ResumeNotAllowedError(
            f"run '{run_id}' is currently executing; cannot resume a live run"
        )

    # Mark the drive live IMMEDIATELY after Gate 2 passes — before the generation
    # bump or any other await — so a second concurrent resume for the SAME run_id
    # hits Gate 2 even while this resume is still pre-spawn (TOCTOU: without this,
    # two resumes could both pass Gate 2 and double-drive). The ``finally`` spans
    # the ENTIRE remainder of the function so the discard (and temp-file delete)
    # still fire on every exit path — including a Gate-3/Gate-4 rejection or an
    # ``update_run_generation`` raise — not just the happy spawn path.
    _active_drives.add(run_id)
    snapshot_path: Optional[str] = None
    try:
        # --- Gate 3: terminal-state / tier resumability (delegated to U3) -> 409 ---
        if not _is_resumable_for_tier(row):
            raise ResumeNotAllowedError(f"run '{run_id}' is {row.state}; not resumable")

        # --- Gate 4: corrupt snapshot -> 422 (script-tier rebuild, NOT the YAML rebuild) ---
        try:
            snapshot = json.loads(row.spec_snapshot)
            source = snapshot["source"]
            if not isinstance(source, str):
                raise ValueError("spec_snapshot.source is not a string")
        except (ValueError, TypeError, KeyError) as e:
            raise ResumeCorruptError(f"run '{run_id}' snapshot is corrupt: {e}") from e

        # Script-tier record reconstruction (CONTRADICTION #3): minimal, from RunRow.
        # Does NOT reuse the YAML _rebuild_record_from_journal (which YAML-validates
        # spec_snapshot and would degrade a ScriptSpec snapshot to corrupt).
        record = ScriptRunRecord(
            run_id=row.run_id,
            workflow_name=row.workflow_name,
            state=RunState.RUNNING,
            cancelled=False,
            current_step_id=None,
            step_states={},
            process=None,
            generation=row.generation,
            started_at=row.started_at,
            finished_at=None,
            tier="script",
        )

        # --- Execution: bump + PERSIST generation BEFORE spawn (INV-6, load-bearing) ---
        record.generation = _bump(row.generation)
        # NOT best-effort: an unpersisted bump would let an orphan's old-generation
        # calls through (U3's update_run_generation raises on failure by design).
        await asyncio.to_thread(update_run_generation, run_id, record.generation)
        run_registry[run_id] = record

        # Re-open the durable row to RUNNING (best-effort) so a status read reflects it.
        try:
            await asyncio.to_thread(
                workflow_journal.update_run_state, run_id, RunState.RUNNING.value, None
            )
        except (
            Exception
        ) as e:  # noqa: BLE001 — journal reopen write is best-effort; live floor serves (INV-4)
            logger.warning("journal: resume reopen state write for '%s' failed: %s", run_id, e)

        env = _build_env(run_id, record.generation, resume=True)
        snapshot_path = _materialize_snapshot(run_id, source)
        return await _drive_process(record, snapshot_path, env)
    finally:
        _active_drives.discard(run_id)
        _delete_temp_file(snapshot_path)  # ALWAYS deleted after reap (BR-30)


# ---------------------------------------------------------------------------
# A3 — cancel_script_run (S3 flow, signal-first, Q5=A, US-C2)
# ---------------------------------------------------------------------------
async def cancel_script_run(record: ScriptRunRecord) -> None:
    """Cancel a running script run: signal -> sweep -> journal CANCELLED (A3).

    NEVER raises into the caller. Idempotent (BR-19): a second cancel on an
    already-cancelling record is a logged no-op. Order is load-bearing (BR-16,
    Q5=A): (1) bump + persist generation (BR-17, DR-11) so a reparented/unkillable
    subprocess's late run-step calls are fenced across the whole cancel->resume
    window; (2) SIGNAL FIRST via ``_terminate`` so the subprocess emits no new
    run-step calls; (3) THEN sweep in-flight terminals; (4) THEN journal CANCELLED
    (retained, resumable for scripts — BR-18/DR-8). Bounded by the same
    ``WORKFLOW_SCRIPT_TERM_GRACE`` the reaper uses (NFR-REL-1).
    """
    from cli_agent_orchestrator.services.workflow_service import update_run_generation

    # --- Idempotency: a second cancel is a no-op (BR-19) ---
    if record.cancelled:
        logger.info("cancel: run '%s' already cancelling — no-op", record.run_id)
        return
    record.cancelled = True

    # 1. Bump generation on cancel too (DR-11) — fence a reparented straggler.
    record.generation = _bump(record.generation)
    try:
        await asyncio.to_thread(update_run_generation, record.run_id, record.generation)
    except (
        Exception
    ) as e:  # noqa: BLE001 — cancel must never raise into the caller (INV-4); fence degraded only
        logger.warning(
            "cancel: generation bump persist for run '%s' failed (fence degraded): %s",
            record.run_id,
            e,
        )

    # 2. SIGNAL FIRST: escalate the subprocess so it emits no new run-step calls.
    if record.process is not None:
        try:
            await _terminate(record.process, WORKFLOW_SCRIPT_TERM_GRACE)
        except Exception as e:  # noqa: BLE001 — cancel must never raise into the caller (INV-4)
            logger.warning("cancel: _terminate for run '%s' failed: %s", record.run_id, e)

    # 3. THEN sweep in-flight terminals (best-effort, self-guarding).
    await _reconcile_orphans(record.run_id)

    # 4. THEN journal CANCELLED (retained -> resumable for scripts, BR-18).
    record.state = RunState.CANCELLED
    record.finished_at = _now()
    try:
        await asyncio.to_thread(
            workflow_journal.update_run_state,
            record.run_id,
            RunState.CANCELLED.value,
            record.finished_at,
        )
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; cancel never raises (INV-4)
        logger.warning(
            "cancel: journal CANCELLED write for run '%s' failed (resumability degraded): %s",
            record.run_id,
            e,
        )
