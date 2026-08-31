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
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from cli_agent_orchestrator.constants import (
    API_BASE_URL,
    WORKFLOW_JOURNAL_RESULT_MAX_BYTES,
    WORKFLOW_SCRIPT_LOG_CAP,
    WORKFLOW_SCRIPT_SCRATCH_DIR,
    WORKFLOW_SCRIPT_TERM_GRACE,
    WORKFLOW_SCRIPT_TIMEOUT,
)
from cli_agent_orchestrator.models.workflow_runtime import (
    RunState,
    StepResult,
    StepState,
    WorkflowRunResult,
)
from cli_agent_orchestrator.services import (
    approval_gate,
    manifest_freeze,
    terminal_service,
    workflow_journal,
)
from cli_agent_orchestrator.services.script_lint import lint_script
from cli_agent_orchestrator.services.secret_gate import redact_json_leaves, redact_secrets
from cli_agent_orchestrator.services.step_output_store import _validate_key_part, step_output_store
from cli_agent_orchestrator.services.step_result import build_envelope, serialise_envelope
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
def build_env(
    run_id: str,
    generation: str,
    inputs: Optional[Dict[str, Any]] = None,
    *,
    resume: bool = False,
) -> Dict[str, str]:
    """Build the exact 6-key constructed spawn env (INV-2, NFR-SEC-2, BR-26, BR-A5).

    U2 (issue #505) promotes this from the module-private ``_build_env`` to a
    PUBLIC seam (ADR-3): the async submission path's background task in
    ``api/main.py`` constructs the script env here rather than reaching into a
    leading-underscore name, and the 6-key env-construction contract stays
    single-homed in this module. ``_build_env`` remains as a backward-compat alias
    (below) so existing internal/test callers are byte-identical (CR-1).

    The spawn env is CONSTRUCTED, never ``os.environ`` inherited-and-extended:
    exactly ``{CAO_WORKFLOW_RUN_ID, CAO_WORKFLOW_GENERATION, CAO_API_BASE_URL,
    CAO_WORKFLOW_INPUTS, PATH, HOME}`` (+ ``CAO_WORKFLOW_RESUME=1`` on resume). No
    secret in the API process environment can leak into the child. ``PATH``/
    ``HOME`` are the OS floor a Python subprocess needs to exec + resolve its
    interpreter/home; they are deliberately NOT in U2's forwarded allowlist (a
    script that tries to forward them on a run-step call is 422'd — the two-clause
    fence, B1).

    Unit A (FR-A3, ADR-2): ``CAO_WORKFLOW_INPUTS`` carries the compact-JSON
    RESOLVED inputs map (defaults filled, types checked at the route). ``inputs``
    defaults to ``{}`` so the two-arg legacy call site (no inputs) still yields
    ``"{}"``. NO cap check here — the 32KiB cap is enforced at the run route
    BEFORE any journal write (ADR-5), not on this delivery seam.
    """
    inputs = inputs or {}
    env = {
        "CAO_WORKFLOW_RUN_ID": run_id,
        "CAO_WORKFLOW_GENERATION": generation,
        "CAO_API_BASE_URL": API_BASE_URL,
        "CAO_WORKFLOW_INPUTS": json.dumps(inputs, separators=(",", ":")),
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
    }
    if resume:
        env["CAO_WORKFLOW_RESUME"] = "1"
    return env


# Backward-compat alias for the pre-U2 module-private name. The 6-key env contract
# is single-homed in ``build_env`` (above); this alias keeps existing internal
# callers and tests byte-identical (CR-1) — it is the SAME function object.
_build_env = build_env


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
) -> Optional[Callable[[str, str], None]]:
    """Build the ``on_step_terminal_ready`` callback for a script-tier run-step call.

    Returns ``None`` (no-op) unless the call carries both ``CAO_WORKFLOW_RUN_ID``
    and ``CAO_WORKFLOW_STEP_ID`` AND that run is a live ``ScriptRunRecord`` in the
    registry — i.e. only genuine script run-step calls record a terminal for the
    sweep, so YAML and handoff callers are wholly unaffected and reach no journal
    write and no redaction at all (BR-9/SR-9).

    The returned callback takes ``(terminal_id, call_fingerprint)`` and does three
    things, in order, for the terminal this step will run on — whether
    ``run_agent_step`` made it or reused one (issue #583, unit
    ``settlement-rewire`` BR-3):

    1. records ``terminal_id`` into the shared record's ``step_states[step_id]``
       (BR-31, unchanged), seeding a RUNNING ``StepRunState`` if the key is not
       yet present so a mid-flight call is visible even before its first journal
       write;
    2. PUBLISHES ``call_fingerprint`` onto that same ``StepRunState`` (BR-2). The
       value is computed by ``run_agent_step`` in the one window
       ``step-fingerprint``'s BR-5 permits and cannot be published there:
       ``workflow_service`` — which owns ``StepRunState`` and ``run_registry`` —
       imports ``run_agent_step``, so the reverse import would be circular. It
       therefore arrives as this callback's second argument;
    3. writes the durable RUNNING row via ``workflow_journal.begin_step``, so a
       crash in the execution window leaves the row ``running`` rather than
       absent (FR-4 guard 1 / INV-2).

    The journal write is BEST-EFFORT (BR-10/INV-4): ``begin_step`` raises and this
    caller catches, because failing to record a RUNNING row degrades resumability
    and must never fail a step that is about to run.
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

    def _record(terminal_id: str, call_fingerprint: str) -> None:
        from cli_agent_orchestrator.models.workflow import StepState

        st = record.step_states.get(step_id)
        if st is None:
            st = StepRunState(step_id=step_id, state=StepState.RUNNING)
            record.step_states[step_id] = st
        st.terminal_id = terminal_id
        # BR-2: in-memory publication. The durable column is begin_step's to write.
        st.call_fingerprint = call_fingerprint

        try:
            workflow_journal.begin_step(run_id, step_id, _now(), call_fingerprint)
        except (
            Exception
        ) as e:  # noqa: BLE001 — journal write is best-effort; resumability degraded only (INV-4)
            # The fingerprint is NEVER echoed into this line (SR-7) — a digest in a
            # log is noise, and the habit of logging "the value that failed" is what
            # would eventually put a prompt there.
            logger.warning(
                "journal: script step '%s/%s': failed to write the running row "
                "(resumability degraded): %s",
                run_id,
                step_id,
                e,
            )

    return _record


# ---------------------------------------------------------------------------
# In-memory recorder for a REPLAYED step (PR #628 review, Copilot F4)
# ---------------------------------------------------------------------------
def record_step_replay(env_vars: Optional[Dict[str, str]]) -> Optional[Callable[[], None]]:
    """Build the callback that makes a REPLAYED step visible in the run's result.

    THE THIRD SIBLING OF ``make_step_terminal_recorder`` AND ``record_step_completion``, with
    the identical guard (both env vars AND a live ``ScriptRunRecord``), and the one that only
    the replay path calls. It exists because the replay branch returns BEFORE
    ``run_agent_step``, which is deliberate — that early return is the only way to create no
    terminal, fire neither of the other two callbacks and write no durable row
    (``run-step-replay-branch`` BR-4). The cost was that ``ScriptRunRecord.step_states`` never
    learned the step happened, and ``_finalize`` builds ``WorkflowRunResult.steps`` from that
    map ALONE while ``resume_script_run`` reconstructs the record with ``step_states={}``. A
    fully replayed resume therefore returned ``steps=[]`` with every journal row intact — the
    run reported doing nothing, having correctly done nothing twice.

    IN MEMORY ONLY. It writes NOTHING to the journal, which is what keeps BR-4 true: a replay
    still creates no terminal and issues no ``begin_step``/``settle_step``. The distinction
    matters — "record that the step is settled" and "settle the step" are different acts, and
    only the second would falsify BR-4 (and bump ``attempts`` for work nothing performed).

    HYDRATED FROM THE DURABLE ROW, NOT FROM THE ENVELOPE OR A DEFAULT. The row is authoritative
    about ``state``, ``attempts``, ``output_json`` and ``error``; the envelope carries only
    ``status`` as free text plus the terminal id. Reconstructed with the SAME field binding
    ``workflow_service._rebuild_record_from_journal`` uses for the YAML tier, so the two tiers
    describe a journal-sourced step identically rather than each inventing a shape.

    It costs ONE extra journal read, and only on the replay path. The gate's own
    single-read budget (``replay-gate`` BR-13/NFR-2) is a property of ``decide``, which is
    unchanged; this read is the caller's, taken once per REPLAYED step in exchange for a
    truthful result. ``attempts`` is deliberately NOT incremented: nothing ran.

    BEST-EFFORT (INV-4, the posture every other bookkeeping call here takes). A read failure or
    an unreadable row degrades the run's step LIST — a reporting loss — and must never fail a
    step, so the caller wraps this and the body degrades to a minimal state rather than raising.
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

    def _record_replay() -> None:
        from cli_agent_orchestrator.models.workflow import StepState
        from cli_agent_orchestrator.services.workflow_service import _record_from_json

        row = workflow_journal.get_step(run_id, step_id)
        if row is None:
            # Not reachable through the gate (a REPLAY verdict requires a row it just read),
            # but this is bookkeeping: recording nothing is strictly better than raising into
            # a step that has already succeeded.
            logger.warning(
                "journal: script step '%s/%s': replayed with no readable row; "
                "the step will be absent from the run result",
                run_id,
                step_id,
            )
            return

        try:
            state = StepState(row.state)
        except ValueError:
            # One unknown state value must not cost the whole entry. The same degradation
            # ``_rebuild_record_from_journal`` takes on a corrupt row, with the same reasoning:
            # a wrong-but-plausible state is worse than an honest fallback, and RUNNING is what
            # the step's own in-memory seed would have said.
            logger.warning(
                "journal: script step '%s/%s': replayed row carries an unrecognised state; "
                "recording it as running",
                run_id,
                step_id,
            )
            state = StepState.RUNNING

        st = StepRunState(
            step_id=step_id,
            state=state,
            attempts=row.attempts,
            output=_record_from_json(row.output_json),
            error=row.error,
            # ``terminal_id`` IS DELIBERATELY LEFT UNSET, and this is the one field where
            # copying the durable value would be a defect rather than a fidelity win. The
            # terminal a replayed step originally ran on NO LONGER EXISTS — that is exactly
            # what ``RunStepResponse.replayed`` was added to tell a consumer (SR-4). This field
            # is read by ``_reconcile_orphans`` to pick terminals to TEAR DOWN, so a dead id
            # here is an instruction to delete something that is gone. Today the sweep would
            # skip it anyway (it also requires a non-terminal step state, and a replayed step
            # is settled), but that is a second condition holding the safety, not this one —
            # and rule 8 of the replay gate is the reminder that a state set can gain members.
            terminal_id=None,
            call_fingerprint=row.call_fingerprint,
        )
        record.step_states[step_id] = st

    return _record_replay


# ---------------------------------------------------------------------------
# Settle-time sanitisation of the two free-content columns (issue #583, unit
# ``settlement-rewire`` SR-1..SR-6). ``settle_step`` persists what it is given and
# ``build_envelope`` sanitises ``last_message`` only, so ``error`` and
# ``output_json`` arrive raw and unbounded — and ``error`` is where a provider's
# failure text lands, the likeliest place in the system for a credential to appear
# verbatim. THE TWO COLUMNS GET DELIBERATELY DIFFERENT TREATMENT (TD-6), because
# they are two kinds of thing: ``error`` is free text where truncation is harmless,
# ``output_json`` is a document both readers ``json.loads`` (``api/main.py``'s
# ``_json_or_none`` and ``workflow_service``'s ``_record_from_json``).
#
# Both transformations are LOSSY BUT TOTAL (SR-6): neither raises, so a verbose
# agent cannot fail a run by talking too much and a settle never strands a step
# that already succeeded.
# ---------------------------------------------------------------------------
_ERROR_TRUNCATION_MARKER = (
    "[... error truncated: {dropped} leading bytes dropped; showing tail ...]\n"
)
"""Prepended to a truncated ``error`` (SR-3).

``error`` has no ``truncated`` flag column — ``StepResultEnvelope`` carries one for
``result_json``, this column carries nothing — so a truncation would otherwise be
invisible. Tail-first truncation makes that actively misleading: the stored text
begins mid-traceback with no ``Traceback (most recent call last):`` header, so a
human sees something that looks MALFORMED rather than TRUNCATED and debugs the
wrong thing. Redaction needs no such marker: ``[REDACTED:<name>]`` is already
inline and announces itself.
"""


def _sanitise_error(error: Optional[str]) -> Optional[str]:
    """Redact, then bound tail-first, then mark. TOTAL — never raises (SR-1/SR-2/SR-3).

    THE ORDER IS A SECURITY REQUIREMENT, not an implementation preference
    (``result-envelope`` SR-1). Bounding first would show the redactor only the kept
    region, so a credential in the dropped head would never be seen — and a
    credential STRADDLING the boundary would be cut in half, defeating the pattern
    match while persisting the surviving fragment. A partial credential is not safe
    for being partial: an AWS key prefix or a PEM header is itself a signal.

    THE KEPT REGION IS THE TAIL, deliberately diverging from ``build_envelope``,
    which keeps the PREFIX of ``last_message`` (SR-2). Not an inconsistency —
    different data. A message's meaning is front-loaded; a Python traceback's is
    back-loaded, with the innermost frames and the actual exception last, so
    truncating a traceback head-first keeps the least useful end. This is also why
    no second byte constant was introduced (TD-2): the problem with 32 KiB for a
    traceback was never the size, it was the direction.

    THE MARKER IS SIZED INTO THE BOUND, NOT ADDED AFTER IT (SR-3) — otherwise the
    rule enforcing the cap is what breaks it. The marker names the dropped byte
    count, so its own length is not a constant and ``cap - len(marker)`` is
    circular. Resolved by reserving the marker's length AT ITS UPPER BOUND (the
    count rendered as the whole input's byte length): the real dropped count can
    never exceed that, so it can never render wider, and
    ``len(marker) + len(kept) <= cap`` holds by construction.
    """
    if error is None:
        return None

    # 1. REDACT FIRST — the whole text, before anything is dropped.
    text, _fired = redact_secrets(error)
    # ``fired`` is deliberately discarded: redaction cascades, so a later pattern can
    # match an earlier ``[REDACTED:<name>]`` marker and the name would be evidence
    # that looks precise and is not (unit 2's SR-4). The inline markers are the record.

    cap = WORKFLOW_JOURNAL_RESULT_MAX_BYTES
    encoded = text.encode("utf-8")
    total = len(encoded)
    if total <= cap:
        # Inclusive boundary: text of exactly the bound is NOT truncated, matching
        # ``build_envelope``. No marker, because nothing was dropped.
        return text

    # 2. BOUND, keeping the TAIL, with the marker's worst-case length reserved.
    reserve = len(_ERROR_TRUNCATION_MARKER.format(dropped=total).encode("utf-8"))
    keep = cap - reserve
    if keep <= 0:
        # Unreachable at the shipped 32 KiB cap (the marker is ~70 bytes) and kept
        # anyway, because a future edit LOWERING the cap is exactly how this becomes
        # reachable — and the one thing that must not happen then is the marker
        # itself breaching the cap it exists to advertise.
        return (
            _ERROR_TRUNCATION_MARKER.format(dropped=total)
            .encode("utf-8")[:cap]
            .decode("utf-8", errors="ignore")
        )
    # Slicing the UTF-8 encoding can split a multi-byte character at the head of the
    # kept region; ``errors="ignore"`` drops that partial sequence rather than
    # raising or emitting U+FFFD (``build_envelope``'s precedent).
    tail = encoded[total - keep :].decode("utf-8", errors="ignore")

    # 3. MARK, with the EXACT dropped count — measured after the decode, so a partial
    # character the decode discarded is counted as dropped rather than as kept.
    dropped = total - len(tail.encode("utf-8"))
    return _ERROR_TRUNCATION_MARKER.format(dropped=dropped) + tail


def _output_placeholder(reason: str, byte_length: int) -> str:
    """A small, VALID JSON document standing in for an ``output_json`` that was dropped.

    Used for both SR-5 (the re-serialised document exceeds the cap) and SR-4's
    unparseable input. Truncating a JSON document at a byte offset almost always
    invalidates it, and both readers parse this column — so the failure would surface
    at READ time, far from the write that caused it. A valid replacement recording the
    drop is the only action consistent with "correct by construction", and it is
    deliberately NOT a prefix of the original.
    """
    return json.dumps(
        {
            "cao_output_dropped": reason,
            "original_bytes": byte_length,
            "detail": (
                "the step's structured output was not persisted; see the run's "
                "result envelope and error for what the step reported"
            ),
        },
        separators=(",", ":"),
    )


def _sanitise_output_json(output_json: Optional[str]) -> Optional[str]:
    """Parse, redact each leaf string, re-serialise. TOTAL — never raises (SR-4/SR-5).

    NEVER A TEXT-LEVEL REDACTION. A ``redact_secrets`` run over the serialised
    document can match ACROSS a structural boundary — a value, its closing quote, part
    of the next key — and corrupt it. Parsing first makes the transformation correct by
    construction: redaction cannot break structure it never sees.

    An input that does not parse was never a valid document, so its shape is unknown
    and guessing at it is exactly what this function refuses to do — it stores the
    placeholder instead. An over-cap re-serialisation likewise becomes a placeholder
    rather than a truncation (SR-5).
    """
    if output_json is None:
        return None
    raw_bytes = len(output_json.encode("utf-8"))
    try:
        document = json.loads(output_json)
    except Exception:  # noqa: BLE001 — totality is the contract (SR-6); see the docstring
        return _output_placeholder("unparseable", raw_bytes)
    try:
        serialised = json.dumps(redact_json_leaves(document), separators=(",", ":"))
    except Exception:  # noqa: BLE001 — a value the walk cannot re-serialise (or a depth
        # limit) must still settle the step, so it degrades to the same placeholder.
        return _output_placeholder("unserialisable", raw_bytes)
    encoded_length = len(serialised.encode("utf-8"))
    if encoded_length > WORKFLOW_JOURNAL_RESULT_MAX_BYTES:
        return _output_placeholder("oversize", encoded_length)
    return serialised


# ---------------------------------------------------------------------------
# Per-step completion transition — wired into the server-side run-step path
# ---------------------------------------------------------------------------
def record_step_completion(
    env_vars: Optional[Dict[str, str]],
) -> Optional[Callable[[Optional[str], Optional[str], Optional[str], Optional[str]], None]]:
    """Build the RUNNING->COMPLETED/FAILED transition for a script-tier step.

    Mirrors ``make_step_terminal_recorder``'s guard exactly (BR-31 pattern):
    returns ``None`` (no-op) unless the call carries both ``CAO_WORKFLOW_RUN_ID``
    and ``CAO_WORKFLOW_STEP_ID`` AND that run is a live ``ScriptRunRecord`` in the
    registry — so YAML/handoff callers are wholly unaffected.

    The terminal-ready recorder seeds a step ``RUNNING`` when its terminal appears
    but nothing ever transitions it, so a completed script run would report every
    step frozen at ``running``/``attempts=0``/``output=null``. The returned callback
    settles the step at the end of a run-step call, matching the YAML tier's
    per-step transition (``workflow_service._run_step``):

    - success -> ``COMPLETED`` (or ``COMPLETED_UNVALIDATED`` when the worker's
      structured output is present but failed schema validation — the same
      missing==invalid distinction the YAML tier records), attempts incremented,
      any structured output copied onto the step state;
    - ``StepExecutionError`` (a crashed/timed-out step) -> ``FAILED`` with the
      error string recorded.

    THE CALLBACK TAKES ``(terminal_id, error, last_message, response_status=None)``.
    ``last_message`` is the step's raw text result, needed for the durable result
    envelope: a settled row with no envelope is precisely what FR-4 guard 1 exists
    to prevent, and an envelope built without the step's own output would satisfy
    the guard's letter while making every future replay serve an empty result.
    ``response_status`` is the original successful HTTP response status, which can
    intentionally differ from the journal state when structured output is
    unvalidated; its ``None`` default means no response existed, so FAILED arms
    retain their historical envelope status derived from ``StepState.FAILED``.
    ``last_message`` and ``response_status`` are both ``None`` on every failure arm,
    where the step produced no message — a FAILED step still gets an envelope (unit
    2 BR-1), because envelope ABSENCE must keep meaning *a crash between the
    writes* and never *the step failed*.

    ``provider``/``agent``/``prompt`` WERE PARAMETERS AND ARE GONE (TD-5). They
    existed only to compute the call fingerprint here, and the fingerprint no longer
    comes from here: ``run_agent_step`` computes it in the one window BR-5 permits
    and the terminal-ready hook publishes it onto ``StepRunState``. An inert
    parameter that reads as authoritative is the trap this issue has already hit
    twice (``diverged_fields``, ``attempts``), so they are dropped rather than kept
    and ignored.

    ONE DURABLE WRITE (BR-6). The former ``append_step`` + ``update_step`` pair is
    replaced by a single ``settle_step``, so state, count, envelope, output and
    error land in one statement and no window exists in which the row reads settled
    and carries no result (ADR-583-4). No ``attempts`` argument is passed — the
    count is SQL-owned and the parameter does not exist (unit 6 BR-5/BR-6).

    ``error`` and ``output_json`` are REDACTED AND BOUNDED here, by
    :func:`_sanitise_error` and :func:`_sanitise_output_json` (SR-1..SR-6). Unit 6
    assigned that debt to this unit: ``settle_step`` persists what it is given and
    ``build_envelope`` sanitises ``last_message`` only, so until this call site
    existed a settled row carried a redacted, bounded envelope beside a raw,
    unbounded ``error``.

    A journal failure only degrades durable status; it never fails the step
    (INV-4/BR-10).
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

    def _settle(
        terminal_id: Optional[str],
        error: Optional[str],
        last_message: Optional[str],
        response_status: Optional[str] = None,
    ) -> None:
        st = record.step_states.get(step_id)
        if st is None:
            # No prior RUNNING seed (e.g. the terminal-ready callback never fired) —
            # create the state so the transition is still recorded.
            st = StepRunState(step_id=step_id, state=StepState.RUNNING)
            record.step_states[step_id] = st
        if terminal_id is not None:
            st.terminal_id = terminal_id
        st.attempts += 1

        if error is not None:
            st.state = StepState.FAILED
            st.error = error
        else:
            # Adopt any structured output the worker returned via
            # ``workflow_return`` (keyed by the same run/step ids). A present but
            # unvalidated record settles COMPLETED_UNVALIDATED, mirroring the YAML
            # tier's _collect_structured_output; absent output stays COMPLETED.
            rec = step_output_store.get(run_id, step_id)
            if rec is not None:
                st.output = rec
                st.state = StepState.COMPLETED if rec.validated else StepState.COMPLETED_UNVALIDATED
            else:
                st.state = StepState.COMPLETED
            st.error = None

        # ONE best-effort durable write (BR-6): state, attempts, envelope, output and
        # error settle atomically, so the row can never read settled with no result.
        # Never raises into the step (INV-4).
        #
        # The in-memory ``st.error`` above stays RAW on purpose — it is process-local
        # and already surfaced by the existing status read. Sanitisation belongs at
        # the persistence boundary, which is the durable copy this unit is
        # accountable for (INV-5 is a persistence invariant).
        now = _now()
        try:
            raw_output_json = json.dumps(st.output.output) if st.output is not None else None
        except Exception:  # noqa: BLE001 — an unserialisable output must still settle (SR-6)
            raw_output_json = None
            logger.warning(
                "journal: script step '%s/%s': structured output could not be serialised "
                "and was not persisted",
                run_id,
                step_id,
            )
        try:
            existed = workflow_journal.settle_step(
                run_id=run_id,
                step_id=step_id,
                state=st.state.value,
                updated_at=now,
                result_json=serialise_envelope(
                    build_envelope(
                        last_message or "",
                        response_status if response_status is not None else st.state.value,
                        st.terminal_id,
                    )
                ),
                output_json=_sanitise_output_json(raw_output_json),
                error=_sanitise_error(st.error),
            )
            if not existed:
                # AN OBSERVATION, NEVER A CONCLUSION (BR-7/SR-8, unit 6 TD-2a). The
                # bool is ASYMMETRIC: ``settle_step``'s pre-upsert SELECT shares no
                # transaction with its upsert, so in a two-process race ``False`` can
                # be reported while the row did exist. A conclusion-shaped message
                # ("the terminal-ready callback never fired") would send a human
                # hunting a callback bug that did not happen, and misleading evidence
                # is worse than none. Two identifiers only.
                logger.warning(
                    "journal: script step '%s/%s': no prior row observed at settle",
                    run_id,
                    step_id,
                )
        except (
            Exception
        ) as e:  # noqa: BLE001 — journal write is best-effort; resumability degraded only (INV-4)
            logger.warning(
                "journal: script step '%s/%s' completion write failed "
                "(resumability degraded): %s",
                run_id,
                step_id,
                e,
            )

    return _settle


# ---------------------------------------------------------------------------
# _materialize_snapshot (BR-30) — engine-owned temp file, 0o600 under 0o700 root
# ---------------------------------------------------------------------------
def _materialize_snapshot(run_id: str, source: str) -> str:
    """Write the frozen ``spec_snapshot.source`` to an engine-owned temp file (BR-30).

    Resume re-drives the FROZEN snapshot, not the author's on-disk file (INV-7),
    so a resumed run executes the same source even if the author edits the file.
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
    interpret the exit + sentinel scan. Only the lint gate raises, and it raises
    exactly one type — ``ScriptLintError`` — while a run failure/timeout returns a
    FAILED result instead. Naming the type matters: it is the only exception a
    caller of this function must handle, so an ``except Exception`` here would be
    both too broad and a silent way to swallow it.
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

    # --- Step 0b: approval gate (issue #583 Bolt 2, ``approval-gate``) ---
    # Built ONCE here and handed to the INSERT below unchanged, so the manifest that is CHECKED is
    # byte-identical to the one STORED — and ADR-583-4's one-write discipline is preserved.
    manifest_json = await asyncio.to_thread(
        manifest_freeze.build_manifest_json,
        source_hash=spec.content_hash,
        inputs=inputs,
    )
    # Placed here, and not lower, for two reasons that are both about leaving nothing behind:
    #   * BEFORE the registry write and the journal INSERT, so a refused start leaves no live record
    #     and no ``workflow_run`` row. Every first run of a new plan is refused by design, so
    #     recording them would durably record runs that never happened.
    #   * OUTSIDE the ``try`` around ``insert_run`` below, which swallows EVERY exception by design
    #     ("journal insert is best-effort; live floor still serves"). A refusal raised inside it would
    #     be logged and the run would continue — the gate would appear to work and would authorise
    #     nothing.
    # No-ops entirely when enforcement is disabled, which is the default.
    approval_gate.ensure_plan_approved(tier="script", manifest_json=manifest_json)

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
            # issue #583 Bolt 2, ``manifest-freeze``: the frozen manifest rides the SAME INSERT as
            # the run row. ADR-583-4's lesson — Bolt 1's critical hazard was two writes for one
            # logical settle, and a crash between them left a state nothing could detect. Here that
            # window would produce a NULL manifest indistinguishable from a YAML run and from a
            # failed freeze. ``build_manifest_json`` is TOTAL and returns None on any failure, which
            # writes NULL and fails CLOSED at the approval gate.
            # ``approval-gate`` (Bolt 2 unit 7) built this value at Step 0b and has already gated on
            # it; reusing it rather than rebuilding is what makes checked-equals-stored true.
            manifest_json,
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
    # Deliver the RESOLVED inputs (already validated + capped at the route, and
    # journaled above as json.dumps(inputs)) to the child via CAO_WORKFLOW_INPUTS.
    env = build_env(run_id, "1", inputs, resume=False)
    _active_drives.add(run_id)
    try:
        return await _drive_process(record, spec.path, env)
    finally:
        _active_drives.discard(run_id)


async def run_script_workflow_prepared(
    record: ScriptRunRecord, spec_path: str, env: Dict[str, str]
) -> WorkflowRunResult:
    """Drive an already-linted, already-journaled, already-registered script run (U2, ADR-3).

    The DEDICATED prepared entry the async submission path's background task
    (``_run_in_background``) invokes for the script tier. It is the EXACT tail of
    :func:`run_script_workflow` (the ``_active_drives.add`` -> ``_drive_process``
    -> ``finally discard`` block) with the pre-drive key-validate, lint gate,
    record build, registration, and durable insert REMOVED — the async handler
    (C1) has already done all of those BEFORE acking with 202.

    Re-entering the blocking :func:`run_script_workflow` here would re-run the lint
    gate and re-``insert_run`` (a plain INSERT -> ``IntegrityError`` on the
    already-journaled id) and would ack a lint-failing script only AFTER a 202 +
    RUNNING row already existed (ADR-3). This drive-only entry re-runs none of that;
    ``_drive_process`` is reused UNCHANGED so the async drive spawns, drains, reaps,
    and settles the terminal state / journal write-throughs identically to a
    blocking run. The ``finally`` clears the ``_active_drives`` liveness mark on
    EVERY exit path so a settled run stays resumable.
    """
    _active_drives.add(record.run_id)
    try:
        return await _drive_process(record, spec_path, env)
    finally:
        _active_drives.discard(record.run_id)


# ---------------------------------------------------------------------------
# A2 — resume_script_run (S2 flow, M3, US-C1/C2)
# ---------------------------------------------------------------------------
async def resume_script_run(
    run_id: str, decisions: Optional[Mapping[str, str]] = None
) -> WorkflowRunResult:
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
    a ``finally`` after reap. Generation fencing is active; U3's replay lookup
    remains a reserved journal primitive and is not wired into this drive.

    ``decisions`` (issue #583, ``recovery-decision-intake``) carries the human's
    per-step answers to a halt — ``step_id`` -> ``rerun``|``skip`` — and is applied
    inside this function on purpose, between admission and the spawn:

    * **BEFORE the spawn** (BR-7), because the replay gate reads journal rows, so a
      decision applied after the spawn would be invisible to the step it was meant
      to resolve.
    * **AFTER admission, and after this resume has claimed the drive** (SC-3, and the
      SR-2 threat class it belongs to). A decision is durable consent to re-execute a
      side-effecting step, so it must never be written by a resume that then reports
      failure. Applied at the ROUTE instead, a second concurrent resume of the same
      run would write its consent, hit gate 2 here and return 409 — leaving that
      consent live under the FIRST resume's drive. Applying it after
      ``_active_drives.add`` closes that window, because gate 2 has already run and
      the claim is held.

    ``None``/empty is the ordinary resume and performs only the re-decision gate's
    journal read; it writes no decision and emits no decision log line. A caller that
    supplies decisions gets a ``ValueError`` for an unknown ``step_id`` or value
    (BR-6), which the resume route's existing bare-``ValueError`` arm maps to 400.
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
    record: Optional[ScriptRunRecord] = None
    result: Optional[WorkflowRunResult] = None
    # ``step_id`` -> the state the row held before this resume's decision was applied (PR #628
    # review, F6). Declared BEFORE the ``try`` so the ``finally`` can always read it, empty for
    # the ordinary no-decision resume, which therefore takes no extra read and no extra write.
    granted: Dict[str, str] = {}
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

        # --- Gate 5: recovery consent is scoped to one resume -> 409 ---
        # Read BEFORE apply_decisions: that write turns a fresh decision and a stale,
        # crash-surviving authorisation into the same durable state. The subtraction is
        # per-step so a fresh decision for one step cannot launder consent left on another.
        authorised_states = set(workflow_journal.DECISION_STATES.values())
        for journal_step in await asyncio.to_thread(workflow_journal.get_steps, run_id):
            if journal_step.state in authorised_states and (
                decisions is None or journal_step.step_id not in decisions
            ):
                raise ResumeNotAllowedError(
                    f"run '{run_id}' step '{journal_step.step_id}' has unconsumed recovery consent; "
                    "supply a fresh decision for this step to resume"
                )

        # --- Gate 6: plan approval -> 403 (issue #583 Bolt 2, ``approval-gate``) ---
        # LAST of the admission gates, and that position is the requirement rather than a preference.
        #
        # It was authored as gate 5 against Bolt 2's base and RENUMBERED to 6 when Bolt 2 rebased onto
        # a merged Bolt 1: PR #628's review added the recovery-consent gate above, at the same place,
        # after Bolt 2 had branched. Both gates survive and the ORDERING PRINCIPLE is what decided
        # which goes first — consent is a fact about the RUN, like gates 1-4, while approval is a fact
        # about the PLAN, so approval stays last among admission checks.
        #
        # It must come:
        #   * AFTER gates 1-5, so "unknown run", "already live", "not resumable", "corrupt snapshot"
        #     and "unconsumed consent" keep their own answers — a caller must be able to tell those
        #     from "needs approval", and the concurrent-resume question is settled by the drive claim
        #     above before this gate is ever asked.
        #   * BEFORE ``apply_decisions`` below and BEFORE the generation bump further down. The bump
        #     fences out any still-live process for this run, so bumping and then refusing would kill
        #     a possibly-healthy run on behalf of a resume that was itself rejected — two losses from
        #     one refusal. ``apply_decisions`` writes, and a refused resume must leave nothing behind.
        # ``plan_id`` comes from the manifest ALREADY on the row and is never recomputed: recomputing
        # would re-read the script from disk, so an edit between start and resume would refuse a run
        # whose approval is perfectly valid for what it actually froze.
        approval_gate.ensure_plan_approved(tier=row.tier, manifest_json=row.manifest_json)

        # --- The human's recovery decisions (issue #583, FR-7). NOT a gate: admission
        # is over, this resume holds the drive claim, and nothing has been spawned yet.
        # That position is the requirement — see the docstring. A ValueError here
        # aborts the resume having written nothing (the whole map is validated first),
        # and the ``finally`` below still releases the claim.
        #
        # THE PRIOR STATES ARE CAPTURED HERE AND REVOKED IN THE ``finally`` (PR #628 review,
        # Copilot F6): consent is good for THIS drive and no other, so anything this drive did
        # not consume is taken back when the drive ends — however it ends.
        if decisions:
            granted = await asyncio.to_thread(workflow_journal.apply_decisions, run_id, decisions)

        # Unit A (FR-A6, ADR-3, REL-A1): re-deliver the RESOLVED inputs journaled
        # at the original run VERBATIM — read row.inputs_json and hand it to
        # _build_env unchanged. NO re-validation against the (possibly edited)
        # INPUTS declaration; the frozen-contract-per-run is what makes resume a
        # deterministic replay (BR-A7). A corrupt/non-object inputs_json degrades
        # to {} rather than aborting the resume (resume delivers what it can).
        journaled_inputs: Dict[str, Any] = {}
        try:
            parsed_inputs = json.loads(row.inputs_json)
            if isinstance(parsed_inputs, dict):
                journaled_inputs = parsed_inputs
        except (ValueError, TypeError) as e:
            logger.warning(
                "resume: run '%s' inputs_json unparseable; delivering empty inputs: %s",
                run_id,
                e,
            )

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

        env = _build_env(run_id, record.generation, journaled_inputs, resume=True)
        snapshot_path = _materialize_snapshot(run_id, source)
        result = await _drive_process(record, snapshot_path, env)
    finally:
        _active_drives.discard(run_id)
        _delete_temp_file(snapshot_path)  # ALWAYS deleted after reap (BR-30)
        # BR-9's second half (PR #628 review, F6). ONE decision authorises exactly ONE
        # attempt, and this drive was that attempt — so any consent it did not consume is
        # revoked now, and the next resume asks again. In the ``finally`` because EVERY exit
        # path ends the attempt: a raising generation bump or snapshot materialisation (which
        # left consent live for a resume that reported failure), a drive that never reached the
        # decided step, and a clean completion — where a ``skip`` would otherwise stand
        # forever, since a replay writes no row by design and nothing else consumes it.
        #
        # BEST-EFFORT, and it must be: a raise here would replace the drive's real outcome
        # (including its exception) with a bookkeeping error. Failing to revoke leaves consent
        # live for one more resume, which is a strictly smaller fault than losing the run's
        # result — and the revoke is a compare-and-set, so retrying it later is safe.
        #
        # CALLED DIRECTLY, NOT THROUGH ``asyncio.to_thread``, unlike every other journal call
        # on this path — and the reason is stated as the DEFENSIVE choice it is, not as a bug
        # fix, because the difference was measured and is not observable today. An ``await``
        # inside a ``finally`` completes only if cancellation is not re-delivered at that
        # suspension point; that depends on the interpreter version and on whether the task is
        # cancelled again, and ``CancelledError`` is a ``BaseException``, so the guard below
        # would not catch it. BOTH FORMS PASS
        # ``test_a_cancelled_resume_still_revokes`` AND a real ``task.cancel()`` probe on
        # CPython 3.12 (verified — do not "restore" the threaded form on the belief that this
        # comment describes a reproduced failure). The direct call is preferred only because it
        # removes the question: it is one short transaction over at most a handful of single-row
        # UPDATEs, so it costs microseconds on the loop and cannot be interrupted at all, and
        # a cancelled resume is the exit path on which silently keeping consent would be least
        # acceptable — it consumed nothing.
        if granted:
            try:
                revoked = workflow_journal.revoke_unconsumed_decisions(run_id, granted)
            except (
                Exception
            ) as e:  # noqa: BLE001 — never mask the drive's outcome; consent lives one resume longer
                logger.warning(
                    "resume: run '%s' failed to revoke unconsumed recovery consent "
                    "(it remains live for the next resume): %s",
                    run_id,
                    e,
                )
            else:
                # A skip replays from its temporary ``replay_authorized`` row, so the
                # recorder correctly hydrates that state before this finally revokes it.
                # The database return is the atomic answer to which grants survived; mirror
                # only those rows back into the still-live record and already-built result.
                # A cancellation has no result to mutate, but the retained record is still
                # served by the bounded status window and MUST be normalized all the same.
                try:
                    for step_id in revoked:
                        try:
                            prior_state = StepState(granted[step_id])
                        except ValueError:
                            # Unlike ``record_step_replay``, this settled replay already has a
                            # typed state. Publishing RUNNING would invent an in-flight state
                            # and alter ``_reconcile_orphans``' terminal-state condition, so
                            # retain it while reporting the failed mirror honestly.
                            logger.warning(
                                "resume: run '%s' step '%s': recovery consent was revoked but "
                                "the unrecognised prior state could not be mirrored",
                                run_id,
                                step_id,
                            )
                            continue
                        if record is not None and step_id in record.step_states:
                            record.step_states[step_id].state = prior_state
                        if result is not None:
                            for step in result.steps:
                                if step.id == step_id:
                                    step.state = prior_state
                except (
                    Exception
                ) as e:  # noqa: BLE001 — never mask the drive's outcome with cache bookkeeping
                    logger.warning(
                        "resume: run '%s': recovery consent was revoked but the live state "
                        "could not be normalized: %s",
                        run_id,
                        e,
                    )
    if result is None:
        # Reached only after a drive returned without a typed result: exceptions, including
        # cancellation, propagate through the ``finally`` instead. Raised rather than relying
        # on an assert, which Python optimisation may remove.
        raise RuntimeError(f"resume: run '{run_id}' drive returned no WorkflowRunResult")
    return result


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

    # The API route rejects this as 409. Keep the service safe for direct callers:
    # cancellation must never rewrite a retained COMPLETED/FAILED record.
    if record.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
        logger.info(
            "cancel: run '%s' already terminal (%s) — no-op",
            record.run_id,
            record.state.value,
        )
        return

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
