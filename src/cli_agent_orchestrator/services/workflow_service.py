"""Workflow run engine (issue #312, Bolt 3 / N5).

The deterministic, in-memory orchestration engine: it holds the process-local run
registry and the sequencing / bounded-retry / reprompt-once / templating logic that
drives a validated ``WorkflowSpec`` through ``run_agent_step`` one step at a time.

Boundary discipline (B3-BR-14/B3-BR-15):

- The engine runs IN the API process and calls ``run_agent_step`` directly,
  in-process — it NEVER reaches back through the HTTP API (single seam).
- A step failure is recorded as ``StepState.FAILED`` ON THE RUN RECORD; it does
  NOT raise into the run loop. Only engine-internal invariant violations raise the
  typed ``WorkflowEngineError`` (mapped to HTTP 500 at the boundary). Domain errors
  map narrowly at the boundary: unknown run/spec -> 404, invalid spec/inputs -> 400,
  cancel-of-finished -> 409.
- Reserved seams (parallel / loop / resume) raise ``NotBuiltYetError`` when reached
  — never silently downgraded to sequential (B3-BR-10, honest-tiering).

The algorithm is implemented exactly per ``functional-design/business-logic-model.md``
(§1 start_run, §2/§2a the two nested loops, §3 _collect_structured_output, §4
_substitute, §5 cancel_run, §6 get_run_status, §7 reserved seams).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Set, Union

if TYPE_CHECKING:  # avoid a runtime circular import (script_runner imports this module)
    from cli_agent_orchestrator.services.script_runner import ScriptRunRecord

from pydantic import ValidationError

from cli_agent_orchestrator.constants import (
    WORKFLOW_DEFAULT_STEP_RETRIES,
    WORKFLOW_STEP_TIMEOUT,
)
from cli_agent_orchestrator.models.workflow import (
    NotBuiltYetError,
    RunState,
    StepState,
    WorkflowSpec,
    WorkflowStep,
)
from cli_agent_orchestrator.models.workflow_runtime import (
    RunStatus,
    StepOutputRecord,
    StepResult,
    StepStatus,
    WorkflowRunResult,
)
from cli_agent_orchestrator.services import workflow_journal
from cli_agent_orchestrator.services.agent_step import StepExecutionError, run_agent_step
from cli_agent_orchestrator.services.step_output_store import (
    _validate_key_part,
    step_output_store,
)

logger = logging.getLogger(__name__)


class WorkflowEngineError(Exception):
    """An engine-internal invariant violation (mapped to HTTP 500 at the boundary).

    Distinct from a step run-failure (recorded as ``StepState.FAILED`` on the
    record, NOT raised) and from domain input errors (``ValueError`` -> 400). This
    is raised only when the engine reaches a state that should be impossible —
    e.g. a template references an upstream step that never produced output
    (an authoring error surfaced loudly, never a silent blank fill, B3-BR-14).
    """


class ResumeNotAllowedError(ValueError):
    """Resume rejected because the run is terminal or still live (B4-BR-7/7a -> 409).

    A ``ValueError`` subclass so any caller catching ``ValueError`` broadly still
    works (no contract break); the resume route catches THIS type before any bare
    ``ValueError`` to map it to 409 (business-logic-model §5, reviewer N1).
    """


class ResumeCorruptError(ValueError):
    """Resume rejected because ``spec_snapshot`` will not deserialize (B4 -> 422).

    A ``ValueError`` subclass; the resume route catches it before any bare
    ``ValueError`` to map an undeserializable snapshot to 422 (business-logic-model
    §5). The run is unresumable and the failure is surfaced clearly, never silently.
    """


class StaleGenerationError(ValueError):
    """A script ``run-step`` call carried an old generation (A3, ADR-9 -> 409).

    U3 addition (issue #312, script-tier journal extension, C3). A ``ValueError``
    subclass so a broad ``except ValueError`` at the API boundary still maps it,
    but the route should catch this type BEFORE any bare ``ValueError`` to map it
    to 409 specifically (business-rules DR-5, same precedent as
    ``ResumeNotAllowedError``). Raised by ``check_generation`` when the caller's
    generation does not match the run's current journaled generation — this is
    what fences an orphaned/reparented predecessor subprocess out after a resume
    or cancel bumps the generation (INV-6).
    """


class ReplayDivergenceError(Exception):
    """A resumed script call's fingerprint diverged from its journaled row (A2, DR-4).

    U3 addition (issue #312, script-tier journal extension, C3). NOT a
    ``ValueError`` / NOT mapped to an HTTP status at the resume route boundary
    (business-rules "Error-to-status mapping" table) — the script changed between
    runs at the same ``(run_id, step_id)`` key, so resume cannot honor the replay
    determinism contract (ADR-5). The run is failed loudly (state -> FAILED,
    surfaced in the run result), never silently re-executed.
    """


# ---------------------------------------------------------------------------
# In-memory run aggregate (engine-internal; never crosses the HTTP seam)
# ---------------------------------------------------------------------------
@dataclass
class StepRunState:
    """Per-step run state (domain-entities ``StepRunState``).

    Loop fields (``which_guard_fired`` / ``iterations_run``) are RESERVED for N8
    and always None in the MVP (B3-BR-11).
    """

    step_id: str
    state: StepState = StepState.PENDING
    attempts: int = 0
    reprompted: bool = False
    output: Optional[StepOutputRecord] = None
    terminal_id: Optional[str] = None
    error: Optional[str] = None
    which_guard_fired: Optional[str] = None  # RESERVED (N8) — always None in MVP
    iterations_run: Optional[int] = None  # RESERVED (N8) — always None in MVP


@dataclass
class RunRecord:
    """The run aggregate root (domain-entities ``RunRecord``), held in the registry."""

    run_id: str
    workflow_name: str
    spec: WorkflowSpec
    inputs: Dict[str, Any]
    state: RunState = RunState.RUNNING
    current_step_id: Optional[str] = None
    cancelled: bool = False
    step_states: Dict[str, StepRunState] = field(default_factory=dict)
    started_at: str = ""
    finished_at: Optional[str] = None


# Process-local run registry (ADR-8, B3-LC-2 singleton). A process restart loses
# in-flight runs — the explicit, documented pre-N6 gap (A-5).
#
# U4 (issue #312, script tier, C1) widens the value type to a union: a script run
# registers a ``ScriptRunRecord`` (a separate dataclass carrying a live subprocess
# handle) in the SAME map YAML runs use, so status/cancel dispatch is one lookup
# path (code-generation-plan CONTRADICTION #6). U5 dispatches by tier via
# ``getattr(record, "tier", "yaml")`` — ``RunRecord`` has no ``tier`` attribute, so
# the default keeps YAML records on the YAML path.
run_registry: Dict[str, Union[RunRecord, "ScriptRunRecord"]] = {}

# run_ids whose drive loop is executing IN THIS PROCESS right now (B4-BR-7a / F4).
# The registry alone cannot answer "is anything actually driving this run?" — a
# status read may rebuild a crash remnant into the cache with state == RUNNING
# even though nothing is executing. Membership here is the liveness truth: added
# when a drive starts (start_run / _drive_resume), removed in a ``finally`` on
# EVERY exit path (complete, fail, engine error, cancel).
_active_drives: Set[str] = set()


def _now() -> str:
    """ISO-8601 Z timestamp (bookkeeping only — never an ordering key)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# N6 — durable run journal write-through (best-effort, B4-BR-5)
# ---------------------------------------------------------------------------
# Every engine transition mutates the in-memory ``RunRecord`` FIRST (preserving
# the FR-5.5 live-read floor), THEN writes the journal here. A write failure is
# logged and SWALLOWED — it must NEVER raise into the engine drive loop (only
# durability/resumability is degraded for that run, B4-BR-5 / B4-RD-1). The
# terminal run-state write is logged at WARNING because it gates resumability;
# the per-step/current-step writes are logged at DEBUG.


def _output_json(record: Optional[StepOutputRecord]) -> Optional[str]:
    """Serialize a step's structured output to JSON, or ``None`` (E2)."""
    if record is None:
        return None
    return json.dumps(record.output)


def _record_from_json(output_json: Optional[str]) -> Optional[StepOutputRecord]:
    """Rebuild a minimal ``StepOutputRecord`` carrying the persisted output (§2).

    On rebuild only the structured ``output`` map is needed for ``{{step.field}}``
    templating into successors — a kept (COMPLETED) step's output is reused. The
    record is reconstructed as ``validated=True`` (it was persisted as a settled
    output); ``errors`` is empty. Returns ``None`` when no output was persisted.
    A malformed JSON blob degrades to ``None`` (never raises into the rebuild).
    """
    if output_json is None:
        return None
    try:
        data = json.loads(output_json)
    except (ValueError, TypeError) as e:
        logger.debug("journal: dropping unparseable step output_json: %s", e)
        return None
    if not isinstance(data, dict):
        return None
    return StepOutputRecord(
        run_id="",
        step_id="",
        output=data,
        validated=True,
        errors=[],
        state=StepState.COMPLETED,
    )


def _journal_insert_run(record: RunRecord) -> None:
    """Best-effort INSERT of the run + its seeded steps at ``start_run`` (§1)."""
    try:
        workflow_journal.insert_run(
            run_id=record.run_id,
            workflow_name=record.workflow_name,
            spec_snapshot=record.spec.model_dump_json(),
            inputs_json=json.dumps(record.inputs),
            state=record.state.value,
            started_at=record.started_at,
        )
        workflow_journal.insert_steps(
            record.run_id,
            [(step.id, StepState.PENDING.value) for step in record.spec.steps],
            _now(),
        )
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; in-memory floor still serves live reads
        logger.warning("journal: insert_run for '%s' failed (run continues): %s", record.run_id, e)


def _journal_step(record: RunRecord, step_id: str) -> None:
    """Best-effort UPDATE of one step's durable state from the in-memory record."""
    st = record.step_states[step_id]
    try:
        workflow_journal.update_step(
            run_id=record.run_id,
            step_id=step_id,
            state=st.state.value,
            attempts=st.attempts,
            updated_at=_now(),
            output_json=_output_json(st.output),
            error=st.error,
        )
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; in-memory floor still serves live reads
        logger.debug("journal: update_step '%s/%s' failed: %s", record.run_id, step_id, e)


def _journal_current_step(record: RunRecord) -> None:
    """Best-effort UPDATE of ``workflow_run.current_step_id``."""
    try:
        workflow_journal.update_run_current_step(record.run_id, record.current_step_id)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; in-memory floor still serves live reads
        logger.debug("journal: update_run_current_step '%s' failed: %s", record.run_id, e)


def _journal_run_state(record: RunRecord) -> None:
    """Best-effort UPDATE of the terminal run state (logged at WARNING — gates resume)."""
    try:
        workflow_journal.update_run_state(record.run_id, record.state.value, record.finished_at)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal write is best-effort; in-memory floor still serves live reads
        logger.warning(
            "journal: terminal state write for '%s' failed (resumability degraded): %s",
            record.run_id,
            e,
        )


async def _ajournal(fn: Any, *args: Any) -> None:
    """Run a sync best-effort journal helper off the event loop.

    The journal helpers do blocking sqlite3 I/O; from async engine code they must
    not stall the loop. ``to_thread`` is awaited sequentially at each call site,
    so write ordering is preserved; the no-raise promise holds because the
    try/except lives inside the sync helper itself. Sync callers (the
    ``get_run_status`` rebuild path) keep calling the helpers directly.
    """
    await asyncio.to_thread(fn, *args)


# ---------------------------------------------------------------------------
# §4 — prompt templating (closed-form; no eval / format / f-string evaluation)
# ---------------------------------------------------------------------------
# ONLY two reference shapes resolve (FR-4.2; richer templating is reserved):
#   {{workflow.inputs.<name>}}     -> record.inputs[name]
#   {{steps.<id>.output.<field>}}  -> record.step_states[id].output.output[field]
# Everything else is left untouched UNLESS it is a {{...}} placeholder we cannot
# resolve, which is a hard WorkflowEngineError (B3-BR-14). The regex captures the
# inner reference text; a dict lookup resolves it — NEVER str.format(**record),
# NEVER an f-string, NEVER eval (B3-SD-2/4, honest tiering).
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]+?)\s*\}\}")
_INPUT_REF_RE = re.compile(r"^workflow\.inputs\.([A-Za-z0-9_-]+)$")
_STEP_REF_RE = re.compile(r"^steps\.([A-Za-z0-9_-]+)\.output\.([A-Za-z0-9_-]+)$")


def _substitute(template: str, record: RunRecord) -> str:
    """Resolve the two supported placeholder shapes against the run record (§4).

    Closed-form: regex-capture each ``{{...}}`` placeholder, match it against the
    two allowed reference shapes, and look the value up in the record. An unknown
    reference shape, an unknown input name, or a reference to a step that has not
    produced a (validated) output yet is a hard ``WorkflowEngineError`` — never a
    silent blank fill (honest-tiering). The spec text is NEVER evaluated.
    """

    def _resolve(match: "re.Match[str]") -> str:
        ref = match.group(1).strip()

        input_match = _INPUT_REF_RE.match(ref)
        if input_match is not None:
            name = input_match.group(1)
            if name not in record.inputs:
                raise WorkflowEngineError(
                    f"template references unknown input '{name}' " f"(reference '{{{{{ref}}}}}')"
                )
            return str(record.inputs[name])

        step_match = _STEP_REF_RE.match(ref)
        if step_match is not None:
            step_id, field_name = step_match.group(1), step_match.group(2)
            st = record.step_states.get(step_id)
            if st is None or st.output is None:
                raise WorkflowEngineError(
                    f"template references step '{step_id}' output, but that step "
                    f"has produced no output (reference '{{{{{ref}}}}}')"
                )
            output_map = st.output.output
            if field_name not in output_map:
                raise WorkflowEngineError(
                    f"template references field '{field_name}' not present in "
                    f"step '{step_id}' output (reference '{{{{{ref}}}}}')"
                )
            return str(output_map[field_name])

        raise WorkflowEngineError(
            f"unsupported template reference '{{{{{ref}}}}}' "
            f"(only {{{{workflow.inputs.<name>}}}} and "
            f"{{{{steps.<id>.output.<field>}}}} are supported)"
        )

    return _PLACEHOLDER_RE.sub(_resolve, template)


def _reprompt_prompt(step: WorkflowStep) -> str:
    """A corrective prompt restating the schema and asking for a valid return (§3)."""
    return (
        f"{step.prompt}\n\n"
        "Your previous turn did not produce a valid structured output for this "
        "workflow step. Call the `workflow_return` tool with output that matches "
        f"this JSON-Schema:\n{step.output_schema}"
    )


# ---------------------------------------------------------------------------
# §2/§3 — input validation, topo-sort, the two nested loops
# ---------------------------------------------------------------------------
def _validate_inputs(spec: WorkflowSpec, inputs: Dict[str, Any]) -> Dict[str, Any]:
    """Validate ``inputs`` against ``spec.inputs`` BEFORE any step runs (B3-BR-2).

    Every required input must be present; each value must match its declared type;
    ``path``-typed inputs are canonicalized through the SHARED path validator
    (never reimplemented). A failure raises ``ValueError`` -> 400 and NO terminal
    is ever created (fail fast). Returns the resolved input map (path values
    replaced by their canonicalized form; declared defaults filled in).
    """
    resolved: Dict[str, Any] = {}
    for name, decl in spec.inputs.items():
        if name in inputs:
            value = inputs[name]
        elif decl.default is not None:
            value = decl.default
        elif decl.required:
            raise ValueError(f"missing required input '{name}'")
        else:
            continue

        if decl.type == "bool":
            if not isinstance(value, bool):
                raise ValueError(f"input '{name}' must be a bool")
        elif decl.type == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"input '{name}' must be an int")
        elif decl.type == "string":
            if not isinstance(value, str):
                raise ValueError(f"input '{name}' must be a string")
        elif decl.type == "path":
            if not isinstance(value, str):
                raise ValueError(f"input '{name}' must be a path string")
            # Reuse the shared path validator (project Mandated rule) — canonicalize
            # via realpath + reject blocked system dirs. A bad path raises ValueError.
            from cli_agent_orchestrator.clients.tmux import tmux_client

            value = tmux_client._resolve_and_validate_working_directory(value)
        else:  # pragma: no cover — grammar restricts type to the four above
            raise ValueError(f"input '{name}' has unknown declared type '{decl.type}'")

        resolved[name] = value

    # Reject unknown inputs the spec does not declare (fail fast on a typo).
    for name in inputs:
        if name not in spec.inputs:
            raise ValueError(f"unknown input '{name}' (not declared by the workflow)")

    return resolved


def _topological_order(spec: WorkflowSpec) -> List[WorkflowStep]:
    """Deterministic topological order of steps by ``needs:`` (B3-BR-6, RD-3.1).

    Ties are broken by DECLARATION ORDER so the same spec always sequences
    identically (byte-identical step order). The MVP grammar (``WorkflowStep``)
    has no ``needs:`` field yet, so the order is simply declaration order today;
    this helper is the single deterministic ordering seam that a future ``needs:``
    field plugs into without changing the engine loop. A cycle (once ``needs:``
    exists) raises ``ValueError`` -> 400.
    """
    # Declaration index for the stable tie-break.
    decl_index = {step.id: i for i, step in enumerate(spec.steps)}
    # ``needs:`` is reserved/not-yet-on-the-model; default to no edges so the
    # order is declaration order. Read defensively via getattr for forward-compat.
    edges: Dict[str, List[str]] = {}
    for step in spec.steps:
        needs = getattr(step, "needs", None) or []
        edges[step.id] = list(needs)

    visited: Dict[str, int] = {}  # 0 = visiting, 1 = done
    order: List[str] = []

    def _visit(step_id: str) -> None:
        mark = visited.get(step_id)
        if mark == 1:
            return
        if mark == 0:
            raise ValueError(f"workflow has a dependency cycle at step '{step_id}'")
        visited[step_id] = 0
        # Deterministic: visit prerequisites in declaration order.
        for dep in sorted(edges.get(step_id, []), key=lambda d: decl_index.get(d, 0)):
            if dep not in decl_index:
                raise ValueError(f"step '{step_id}' needs unknown step '{dep}'")
            _visit(dep)
        visited[step_id] = 1
        order.append(step_id)

    for step in spec.steps:  # declaration order roots -> stable tie-break
        _visit(step.id)

    by_id = {step.id: step for step in spec.steps}
    return [by_id[sid] for sid in order]


async def _collect_structured_output(record: RunRecord, step: WorkflowStep) -> StepState:
    """Collect the structured return for a COMPLETED step; reprompt once (§3).

    - No ``output_schema`` -> free-form, trivially ``COMPLETED``.
    - Validated record present -> ``COMPLETED`` (output copied onto the step state).
    - Missing OR invalid record, not yet reprompted -> spend the ONE reprompt on a
      fresh terminal, then re-collect. A crash DURING the reprompt re-raises the
      ``StepExecutionError`` so the OUTER attempt loop (§2) consumes an attempt.
    - Still missing/invalid after the reprompt -> ``COMPLETED_UNVALIDATED``
      (missing == invalid, decision note D1).
    """
    st = record.step_states[step.id]

    if step.output_schema is None:
        return StepState.COMPLETED

    rec = step_output_store.get(record.run_id, step.id)  # immediate read (Q2=A)
    if rec is not None and rec.validated:
        st.output = rec
        return StepState.COMPLETED

    if not st.reprompted:
        st.reprompted = True
        # Re-run on a FRESH terminal with a corrective prompt. A crash here is a
        # run-failure: let the StepExecutionError propagate so the OUTER loop
        # consumes an attempt (Q6=A / Trace C).
        result = await run_agent_step(
            provider=step.provider,
            agent=step.agent,
            prompt=_reprompt_prompt(step),
            teardown=True,
            timeout=WORKFLOW_STEP_TIMEOUT,
            env_vars={
                "CAO_WORKFLOW_RUN_ID": record.run_id,
                "CAO_WORKFLOW_STEP_ID": step.id,
            },
        )
        st.terminal_id = result.terminal_id
        rec = step_output_store.get(record.run_id, step.id)
        if rec is not None and rec.validated:
            st.output = rec
            return StepState.COMPLETED

    # Missing/invalid after the reprompt: record what we have (may be None or an
    # invalid record) and mark unvalidated (NOT a crash — decision note D1).
    st.output = rec
    return StepState.COMPLETED_UNVALIDATED


async def _run_step(record: RunRecord, step: WorkflowStep) -> None:
    """Run one step through the two nested recovery loops (§2/§2a).

    OUTER loop = bounded run-failure retry (B3-BR-4): a ``StepExecutionError``
    (worker error / readiness or completion timeout) consumes an attempt and
    retries the SAME prompt. INNER loop = the reprompt-once inside
    ``_collect_structured_output`` (B3-BR-5), called INSIDE the outer ``try`` so a
    reprompt crash is caught by the same ``except`` and consumes an attempt.

    On attempt exhaustion the step is ``FAILED``; ``on_failure`` (default ``halt``)
    decides whether the run halts (``record.state = FAILED``) or continues.
    """
    n_retries = step.retries if step.retries is not None else WORKFLOW_DEFAULT_STEP_RETRIES
    record.current_step_id = step.id
    st = record.step_states[step.id]
    st.state = StepState.RUNNING
    # Journal write-through (§1): record the step RUNNING + the live current step.
    # Awaited sequentially off the loop (blocking sqlite must not stall the engine).
    await _ajournal(_journal_step, record, step.id)
    await _ajournal(_journal_current_step, record)

    # OUTER: run-failure retry loop. attempts range 1..n_retries+1.
    for attempt in range(1, n_retries + 2):
        if record.cancelled:  # boundary check (B3-BR-7)
            return
        st.attempts = attempt
        st.error = None
        prompt = _substitute(step.prompt, record)  # §4 templating
        try:
            result = await run_agent_step(
                provider=step.provider,
                agent=step.agent,
                prompt=prompt,
                teardown=True,
                timeout=WORKFLOW_STEP_TIMEOUT,
                env_vars={
                    "CAO_WORKFLOW_RUN_ID": record.run_id,
                    "CAO_WORKFLOW_STEP_ID": step.id,
                },
            )
            st.terminal_id = result.terminal_id
            # Resolve the structured return INSIDE the try so a crash during the
            # reprompt (§3 re-raises StepExecutionError) is caught below and
            # consumes an attempt (Q6=A, Trace C).
            outcome = await _collect_structured_output(record, step)
        except StepExecutionError as exc:
            st.error = str(exc)
            if exc.terminal_id is not None:
                st.terminal_id = exc.terminal_id
            continue  # consume an attempt, retry the same prompt
        # Settled (COMPLETED or COMPLETED_UNVALIDATED) — neither is a run-failure.
        st.state = outcome
        # §1: persist settled state + output + attempts
        await _ajournal(_journal_step, record, step.id)
        return

    # Attempts exhausted: every attempt raised StepExecutionError.
    st.state = StepState.FAILED
    # §1: persist FAILED state + last error + attempts
    await _ajournal(_journal_step, record, step.id)
    if (step.on_failure or "halt") == "halt":
        # Engine halts; start_run skips the remaining steps (§1). on_failure ==
        # "continue" leaves the run RUNNING and sequencing continues.
        record.state = RunState.FAILED


async def _skip_remaining(record: RunRecord, order: List[WorkflowStep], from_index: int) -> None:
    """Mark every still-PENDING step at/after ``from_index`` SKIPPED (§1).

    The SINGLE producer of ``SKIPPED`` — reached on a halt (successors of the
    failed step) and on a cancel (remaining PENDING steps). A halted step itself
    is ``FAILED``, never ``SKIPPED`` (B3-BR-4 / B3-BR-7).
    """
    for step in order[from_index:]:
        st = record.step_states[step.id]
        if st.state == StepState.PENDING:
            st.state = StepState.SKIPPED
            await _ajournal(_journal_step, record, step.id)  # §1: persist the skip


def _build_result(record: RunRecord, order: List[WorkflowStep]) -> WorkflowRunResult:
    """Aggregate a ``RunRecord`` into the run's ``WorkflowRunResult`` (§1 step 8)."""
    steps: List[StepResult] = []
    for step in order:
        st = record.step_states[step.id]
        steps.append(
            StepResult(
                id=step.id,
                state=st.state,
                attempts=st.attempts,
                output=st.output.output if st.output is not None else None,
                error=st.error,
            )
        )
    return WorkflowRunResult(
        run_id=record.run_id,
        workflow_name=record.workflow_name,
        state=record.state,
        steps=steps,
        started_at=record.started_at,
        finished_at=record.finished_at,
    )


async def _drive(record: RunRecord, order: List[WorkflowStep]) -> WorkflowRunResult:
    """Sequence ``record`` over ``order``, finalize, and aggregate (§1 steps 6-8).

    THE single execution path (B4-RD-5): ``start_run`` and
    ``resume_from_last_completed`` both drive through here, so resume cannot
    diverge from the normal drive. A non-``PENDING`` (kept) step is skipped —
    on a fresh run every step starts PENDING so the skip is a no-op; on a resume
    it is exactly the B4-BR-9 keep boundary (done steps are never re-run, their
    output stays available for templating).

    An unexpected engine error mid-loop (e.g. ``_substitute`` raising
    ``WorkflowEngineError`` on a bad template reference — raised OUTSIDE
    ``_run_step``'s ``StepExecutionError`` guard) must NOT leave the registered
    record stranded in RUNNING. Settle it to a terminal FAILED state (with
    ``finished_at``) before re-raising so the boundary still maps it to 500 and
    the registry is always left consistent (domain-entities lifecycle,
    B3-RD-3/RD-5).
    """
    try:
        for index, step in enumerate(order):
            if record.cancelled:  # B3-BR-7 — cancel observed at a step boundary
                await _skip_remaining(record, order, from_index=index)
                record.state = RunState.CANCELLED
                break
            st = record.step_states[step.id]
            if st.state in (StepState.COMPLETED, StepState.COMPLETED_UNVALIDATED):
                continue  # kept on resume (B4-BR-9) — do not re-run a done step
            await _run_step(record, step)
            if record.state == RunState.FAILED:  # halt (B3-BR-4 on_failure=halt)
                await _skip_remaining(record, order, from_index=index + 1)
                break
    except WorkflowEngineError:
        # Settle the registered record into a terminal FAILED state before the
        # error propagates to the boundary (-> 500). Mark the in-flight step FAILED
        # too. The exception is NOT masked — it is re-raised unchanged.
        if record.current_step_id is not None:
            cur = record.step_states.get(record.current_step_id)
            if cur is not None and cur.state not in (
                StepState.COMPLETED,
                StepState.COMPLETED_UNVALIDATED,
            ):
                cur.state = StepState.FAILED
        record.state = RunState.FAILED
        record.current_step_id = None
        record.finished_at = _now()
        # Persist the terminal FAILED state (best-effort) before re-raising (§1).
        await _ajournal(_journal_current_step, record)
        await _ajournal(_journal_run_state, record)
        logger.error("drive: run '%s' failed with an engine error", record.run_id)
        raise

    # Finalize.
    if record.state not in (RunState.FAILED, RunState.CANCELLED):
        record.state = RunState.COMPLETED
    record.current_step_id = None
    record.finished_at = _now()
    # Journal the terminal run state + cleared current step (§1, B4-BR-5).
    await _ajournal(_journal_current_step, record)
    await _ajournal(_journal_run_state, record)

    # Aggregate the result.
    return _build_result(record, order)


# ---------------------------------------------------------------------------
# §1 — start_run entry point
# ---------------------------------------------------------------------------
async def start_run(spec: WorkflowSpec, inputs: Dict[str, Any], run_id: str) -> WorkflowRunResult:
    """Run a validated workflow spec to completion, awaited inline (§1, Q1=A).

    Steps: validate the run_id key (B3-BR-1, shared validator) and the inputs
    against ``spec.inputs`` (B3-BR-2) — failing BEFORE any terminal is created;
    build + register the RunRecord; topo-sort the steps deterministically
    (B3-BR-6); sequence them with a cancel-check + halt-handling loop; finalize.

    Raises ``ValueError`` (-> 400) on a bad run_id / invalid inputs, ``KeyError``
    (-> 409) if the run_id is already registered, actively driving, or journaled
    (a durable row must never be clobbered by a reused id), ``NotBuiltYetError``
    (-> reserved seam) for a non-sequential mode, ``WorkflowEngineError`` (-> 500)
    on an engine-internal invariant violation (e.g. a bad template reference).
    """
    # 1. Validate run_id via the shared key validator (B3-BR-1).
    _validate_key_part(run_id, "run_id")
    if run_id in run_registry or run_id in _active_drives:
        raise KeyError(f"run_id '{run_id}' already exists")
    # A durable journal row also claims the id: after a restart the registry is
    # empty, but re-using an old run_id must NOT overwrite its journal history.
    # Best-effort read only — a broken journal must never block NEW runs.
    # Read stays on-loop deliberately: a small point read via the sync helper
    # shared with the sync status path.
    try:
        existing_row = workflow_journal.get_run(run_id)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal read is best-effort; a broken journal must not block new runs (B4-BR-5)
        logger.debug("journal: start_run get_run('%s') failed; proceeding: %s", run_id, e)
        existing_row = None
    if existing_row is not None:
        raise KeyError(f"run_id '{run_id}' already exists")

    # 2. Validate inputs BEFORE any side effect (B3-BR-2 / FR-1.5, fail fast).
    resolved_inputs = _validate_inputs(spec, inputs)

    # Non-sequential mode dispatches to a reserved seam — NEVER silently run as
    # sequential (B3-BR-6/B3-BR-10).
    if spec.mode != "sequential":
        _dispatch_reserved_mode(spec)

    # 3. Build the RunRecord.
    record = RunRecord(
        run_id=run_id,
        workflow_name=spec.name,
        spec=spec,
        inputs=resolved_inputs,
        state=RunState.RUNNING,
        current_step_id=None,
        cancelled=False,
        step_states={step.id: StepRunState(step_id=step.id) for step in spec.steps},
        started_at=_now(),
    )
    # 4. Register (in-memory floor) + mark the drive live + journal the run +
    # seed every step (§1). The ``finally`` guarantees the liveness mark is
    # cleared on EVERY exit path (complete, fail, engine error, cancel).
    run_registry[run_id] = record
    _active_drives.add(run_id)
    try:
        await _ajournal(_journal_insert_run, record)

        # 5. Deterministic sequencing order.
        order = _topological_order(spec)

        # 6-8. Sequence, finalize, aggregate — the single shared drive (B4-RD-5).
        return await _drive(record, order)
    finally:
        _active_drives.discard(run_id)


# ---------------------------------------------------------------------------
# §5 — cancel_run
# ---------------------------------------------------------------------------
def cancel_run(run_id: str) -> None:
    """Cooperatively cancel a running workflow (§5, B3-BR-7).

    Sets the ``cancelled`` flag; the engine observes it at the NEXT step boundary
    (the in-flight step runs to natural completion and self-tears-down its own
    terminal). Never raises into the engine/worker path. Raises ``KeyError`` (->
    404) for an unknown run and ``ValueError`` (-> 409) for a run already in a
    terminal state.
    """
    record = run_registry.get(run_id)
    if record is None:
        raise KeyError(f"unknown run_id '{run_id}'")
    if record.state in (RunState.COMPLETED, RunState.FAILED, RunState.CANCELLED):
        raise ValueError(f"run '{run_id}' is already {record.state.value}; cannot cancel")
    record.cancelled = True
    # Best-effort: there is no separate live terminal for cancel to tear down (the
    # running step owns and releases its own). Any future best-effort cleanup must
    # be wrapped + logged and must never raise into this path.
    logger.info("cancel_run: run '%s' flagged for cooperative cancel", run_id)


# ---------------------------------------------------------------------------
# §6 — get_run_status
# ---------------------------------------------------------------------------
def get_run_status(run_id: str) -> RunStatus:
    """Return a point-in-time SNAPSHOT copy of a run's state (§6, B3-BR-8).

    No locks (Q3=A): the single asyncio loop mutates the record only between
    awaits, so a reader at an await boundary never observes a half-written record.
    The snapshot carries no per-step output or prompt (B3-SD-3). Raises
    ``KeyError`` (-> 404) for an unknown run.
    """
    record = run_registry.get(run_id)
    if record is None:
        # Cache miss (cold read / after a restart): rebuild from the journal ONCE,
        # then re-populate the cache (§2, B4-BR-4). A genuinely-absent run (absent
        # from BOTH cache and journal) raises KeyError -> 404 (F1, contract
        # unchanged). The rebuild returns None on absent and NEVER raises ValueError
        # on this status read path.
        record = _rebuild_record_from_journal(run_id)
        if record is not None:
            run_registry[run_id] = record
    if record is None:
        raise KeyError(f"unknown run_id '{run_id}'")
    return RunStatus(
        run_id=record.run_id,
        state=record.state,
        current_step_id=record.current_step_id,
        steps=[
            StepStatus(id=sid, state=st.state, attempts=st.attempts)
            for sid, st in record.step_states.items()
        ],
    )


# ---------------------------------------------------------------------------
# §2 — registry-as-cache rebuild from the durable journal (Q1=B, B4-BR-3/4)
# ---------------------------------------------------------------------------
def _rebuild_record_from_journal(run_id: str) -> Optional[RunRecord]:
    """Rebuild the in-memory ``RunRecord`` from the durable journal (§2, F2/F8).

    Returns ``None`` when no ``workflow_run`` row exists for ``run_id`` (the caller
    raises ``KeyError`` itself — this helper never raises on an absent run, F1).
    The reconstructed dataclass uses the EXACT shipped ``RunRecord`` field binding
    (``workflow_name`` is required; ``started_at``/``finished_at`` are restored from
    the row — F2). ``step_states`` is seeded for EVERY step of the snapshotted spec
    (PENDING) BEFORE overlaying persisted rows, so a partial ``insert_steps`` write
    can never leave a step absent from the rebuilt record (F8 / B4-RD-3) — the
    engine drive's ``record.step_states[step.id]`` stays total.

    ``reprompted``/``terminal_id`` are NOT journaled (F3): on a kept step they are
    irrelevant (it is skipped), on a reset step they are cleared anyway (B4-BR-9),
    so they default here.
    """
    try:
        row = workflow_journal.get_run(run_id)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal read is best-effort; a missing/failed table degrades to "absent" (B4-RD-4), the caller raises KeyError -> 404
        logger.debug("journal: get_run('%s') failed; treating as absent: %s", run_id, e)
        return None
    if row is None:
        return None

    try:
        spec = WorkflowSpec.model_validate_json(row.spec_snapshot)
        inputs = json.loads(row.inputs_json)
        if not isinstance(inputs, dict):
            # "null" / arrays parse fine but leave record.inputs non-dict, which
            # would later escape _drive as an unmapped TypeError. Corrupt -> absent.
            raise ValueError(f"inputs_json is not a JSON object: {row.inputs_json!r}")
        record = RunRecord(
            run_id=row.run_id,
            workflow_name=row.workflow_name,
            spec=spec,
            inputs=inputs,
            state=RunState(row.state),
            current_step_id=row.current_step_id,
            started_at=row.started_at,
            finished_at=row.finished_at,
        )
    except (ValidationError, ValueError, TypeError) as e:
        # ValidationError subclasses ValueError (pydantic v2) but is named for clarity;
        # ValueError also covers json.JSONDecodeError and RunState coercion. Corrupt
        # journal data degrades to "absent" (B4-RD-4) -> caller raises KeyError -> 404.
        logger.debug("journal: run '%s' row is corrupt; treating as absent: %s", run_id, e)
        return None
    # F8: seed EVERY spec step PENDING first, then overlay durable rows.
    for step in spec.steps:
        record.step_states[step.id] = StepRunState(step_id=step.id, state=StepState.PENDING)
    try:
        step_rows = workflow_journal.get_steps(run_id)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal read is best-effort; on failure the seeded PENDING states stand (B4-RD-3)
        logger.debug("journal: get_steps('%s') failed; using seeded PENDING: %s", run_id, e)
        step_rows = []
    for srow in step_rows:
        if srow.step_id not in record.step_states:
            # A persisted step no longer present in the snapshotted spec — skip it
            # rather than resurrecting a ghost step into the rebuilt record.
            logger.debug(
                "journal: run '%s' step row '%s' not in spec snapshot; skipping",
                run_id,
                srow.step_id,
            )
            continue
        try:
            record.step_states[srow.step_id] = StepRunState(
                step_id=srow.step_id,
                state=StepState(srow.state),
                attempts=srow.attempts,
                output=_record_from_json(srow.output_json),
                error=srow.error,
            )
        except ValueError as e:
            # Unknown ``state`` value etc. — one bad row must never abort the
            # rebuild; the seeded PENDING default stands for this step (B4-RD-3).
            logger.debug(
                "journal: run '%s' step row '%s' is corrupt; keeping seeded PENDING: %s",
                run_id,
                srow.step_id,
                e,
            )
    return record


# ---------------------------------------------------------------------------
# U3 additions (issue #312, script-tier journal extension, C3) — additive only,
# INV-1: no function above this block is modified by U3.
# ---------------------------------------------------------------------------
def check_generation(run_id: str, generation: str) -> None:
    """Reject a stale-generation script call (A3, the ADR-9 anti-double-drive).

    Every script ``run-step`` call carries ``CAO_WORKFLOW_GENERATION`` (forwarded
    by U2); the run-step handler (U5, ``api/main.py::run_step``) calls this BEFORE
    doing any work whenever the request's ``env_vars`` carries BOTH
    ``CAO_WORKFLOW_RUN_ID`` and ``CAO_WORKFLOW_GENERATION`` (VR-3). Resume bumps
    the run's generation BEFORE spawning (A4), so a reparented predecessor
    subprocess that survived a crash still carries the OLD generation — its late
    calls land here and are fenced out (DR-5 -> ``StaleGenerationError`` -> 409).

    Raises ``KeyError`` (-> 404 at the boundary, same precedent as
    ``get_run_status``) when ``run_id`` is unknown to the journal — that is a
    different failure mode than a stale generation and must not be conflated
    with it (DR-5 requires the *matching-run* generation to differ, not "no run
    at all"). Raises ``StaleGenerationError`` on a generation mismatch (DR-5);
    returns ``None`` on a match (DR-6, proceed).
    """
    row = workflow_journal.get_run(run_id)
    if row is None:
        raise KeyError(f"unknown run_id '{run_id}'")
    if generation != row.generation:
        raise StaleGenerationError(
            f"run '{run_id}': call carried generation '{generation}' but the run's "
            f"current generation is '{row.generation}'"
        )


def update_run_generation(run_id: str, generation: str) -> None:
    """Persist a run's bumped generation (A4 write-side helper).

    A thin additive write helper alongside the base's
    ``update_run_current_step``/``update_run_state`` family. Per A4, the resume
    (and DR-11 cancel) drive sequence is bump-then-persist-then-spawn: the
    generation is bumped and persisted here BEFORE the resumed/cancelled run's
    process (re)spawns, so any older-generation orphan is fenced by
    ``check_generation`` from the moment it is stale, not from whenever it
    happens to make its next call. Best-effort is deliberately NOT applied here
    (unlike the ``_journal_*`` write-through helpers above): a failed generation
    persist must be visible to the caller, since a silently-unpersisted bump
    would let an orphan's old-generation calls through — this raises
    ``sqlite3.Error`` on a DB failure, matching ``workflow_journal``'s documented
    write-helper contract (module docstring), and the caller (U4/U5, out of
    scope for U3) decides whether to retry or abort the resume/cancel.
    """
    import sqlite3

    from cli_agent_orchestrator.constants import DATABASE_FILE

    with sqlite3.connect(str(DATABASE_FILE)) as conn:
        conn.execute(
            "UPDATE workflow_run SET generation = ? WHERE run_id = ?",
            (generation, run_id),
        )


def _is_resumable_for_tier(row: workflow_journal.RunRow) -> bool:
    """Tier-aware resumability predicate (DR-7/DR-8, code-generation-plan precision fix).

    **U3 supplies this journal primitive only — wiring it into the actual resume
    ROUTE (replacing the inline check in ``resume_from_last_completed`` below) is
    OUT OF SCOPE for U3; that is U4/U5's job.** ``resume_from_last_completed``'s
    own inline check is untouched by this addition (INV-1): a YAML run's resume
    behavior is byte-identical to before U3.

    The pinned base tip's inline rule (verified at code-gen against
    ``resume_from_last_completed``, not a function literally named
    ``_is_resumable``) is: ``state in (COMPLETED, CANCELLED)`` -> not resumable;
    everything else (``FAILED``, or a crash-remnant ``RUNNING`` row with no live
    registry entry) -> resumable. Q3=A / DR-8 extends this for the SCRIPT tier
    only: a ``CANCELLED`` script run IS resumable (a cancel-then-resume workflow,
    US-C2) — but a ``CANCELLED`` YAML run stays non-resumable, matching the base
    exactly. This function carves CANCELLED out of the shared "terminal, not
    resumable" bucket per-tier; it does NOT relax ``COMPLETED`` for either tier
    (DR-7 — a successfully finished run is never resumable), and it does NOT
    perform the liveness-registry check (DR-9, layer 1) — that is the caller's
    job, same as the base's own two-step check (registry, then journaled state).
    """
    state = RunState(row.state)
    if state == RunState.COMPLETED:
        return False
    if state == RunState.CANCELLED:
        return row.tier == "script"
    return True


# ---------------------------------------------------------------------------
# §7 — reserved seams (raise NotBuiltYetError; never fake-run, B3-BR-10)
# ---------------------------------------------------------------------------
def _dispatch_reserved_mode(spec: WorkflowSpec) -> None:
    """Route a non-sequential ``mode`` to its reserved seam (B3-BR-6/B3-BR-10).

    Each branch hits the owning reserved seam (which raises ``NotBuiltYetError``)
    so the failure names the implementing unit; a non-sequential mode is NEVER
    silently downgraded to sequential.
    """
    if spec.mode in ("parallel", "pipeline"):
        _run_parallel(None, None)
    if spec.mode == "loop":
        _run_loop(None, None)
    # Defensive: grammar restricts ``mode`` to the four literals, so any other
    # non-sequential value still fails loudly rather than running.
    raise NotBuiltYetError(f"workflow mode '{spec.mode}' is reserved (not built yet)")


async def resume_from_last_completed(run_id: str) -> WorkflowRunResult:
    """Resume a crashed/failed run from its durable journal (§3, FR-6.2, N6).

    Un-reserves the B3-BR-10 stub. The algorithm (business-logic-model §3):

    1. Validate ``run_id`` via the shared key validator (B4-BR-2) -> ``ValueError``
       (-> 400) on a malformed/traversal key.
    2. **Liveness guard** (B4-BR-7a, F4): if the run is in ``_active_drives`` — a
       drive loop is actually executing it IN THIS PROCESS -> a
       ``ResumeNotAllowedError`` (-> 409). Never double-drive a live run. A cached
       registry record with ``state == RUNNING`` is NOT enough: a status read may
       rebuild a crash remnant into the cache as RUNNING with nothing executing,
       and that remnant must stay resumable.
    3. Load the ``workflow_run`` row; absent -> ``KeyError`` (-> 404, F1).
    4. A ``COMPLETED``/``CANCELLED`` run is terminal and not resumable ->
       ``ResumeNotAllowedError`` (-> 409, B4-BR-7). A ``FAILED`` run, or a
       ``RUNNING`` row with NO live record (a crash remnant), IS resumable.
    5. Deserialize ``spec_snapshot`` (Q2=B, B4-BR-8); a corrupt snapshot ->
       ``ResumeCorruptError`` (-> 422).
    6. Rebuild the ``RunRecord`` cache (§2, seeds all steps), then apply the Q3=A
       skip/re-run boundary (B4-BR-9): keep ``COMPLETED``/``COMPLETED_UNVALIDATED``
       (output reused for templating), UNCONDITIONALLY reset every other state to
       ``PENDING`` (attempts=0, terminal_id=None, error=None, reprompted=False) so
       the failed step + its halted successors re-run with a fresh terminal
       (B4-BR-10).
    7. Re-enter the SAME Bolt-3 drive over the snapshotted spec's topo order (a
       single execution path, no resume/normal divergence — B4-RD-5). The drive
       skips non-``PENDING`` steps, so the reset above is the only resume-specific
       transformation; persisted inputs are the RESOLVED snapshot, not re-validated
       (B4-BR-10a).
    """
    # 1. Validate the run_id key via the shared validator (B4-BR-2).
    _validate_key_part(run_id, "run_id")

    # 2. Liveness guard (B4-BR-7a / F4): never resume a run a drive loop is
    # actively executing in THIS process. Do NOT trust a cached record's RUNNING
    # state — a rebuilt crash remnant is RUNNING in the cache with no live drive.
    if run_id in _active_drives:
        raise ResumeNotAllowedError(
            f"run '{run_id}' is currently executing; cannot resume a live run"
        )

    # 3. Load the durable row; absent (or an unreadable journal) -> KeyError -> 404 (F1).
    # Read stays on-loop deliberately: a small point read via the sync helper
    # shared with the sync status path.
    try:
        row = workflow_journal.get_run(run_id)
    except (
        Exception
    ) as e:  # noqa: BLE001 — journal read is best-effort; an unreadable journal means nothing to resume (B4-RD-4)
        logger.debug("journal: resume get_run('%s') failed; treating as absent: %s", run_id, e)
        row = None
    if row is None:
        raise KeyError(f"unknown run_id '{run_id}'")

    # 4. Terminal runs are not resumable (B4-BR-7). A corrupt (non-enum) state
    # string is corrupt journal data -> 422, consistent with a corrupt snapshot —
    # never a bare ValueError that would mislead as a 400 "bad request".
    try:
        state = RunState(row.state)
    except ValueError as e:
        raise ResumeCorruptError(f"run '{run_id}' has corrupt state '{row.state}'") from e
    if state in (RunState.COMPLETED, RunState.CANCELLED):
        raise ResumeNotAllowedError(f"run '{run_id}' is {state.value}; not resumable")

    # 5. Deserialize the snapshotted spec (Q2=B, B4-BR-8); corrupt -> 422.
    try:
        spec = WorkflowSpec.model_validate_json(row.spec_snapshot)
    except ValidationError as e:
        raise ResumeCorruptError(f"run '{run_id}' snapshot is corrupt: {e}") from e

    # 6. Rebuild the cache (§2) and apply the Q3=A skip/re-run boundary (B4-BR-9).
    # Rebuild reads stay on-loop deliberately: small point reads via the sync
    # helpers shared with the sync status path.
    record = _rebuild_record_from_journal(run_id)
    if record is None:
        # The row existed above but the rebuild degraded it to absent (e.g.
        # corrupt inputs_json) — surface as unknown rather than resuming garbage.
        raise KeyError(f"unknown run_id '{run_id}'")
    for st in record.step_states.values():
        if st.state in (StepState.COMPLETED, StepState.COMPLETED_UNVALIDATED):
            continue  # keep done; output reused for {{steps.<id>.output.<field>}}
        st.state = StepState.PENDING
        st.attempts = 0
        st.reprompted = False
        st.terminal_id = None  # fresh terminal on re-run (B4-BR-10)
        st.error = None
        st.output = None

    # Re-open the run and re-register the rebuilt record as the live cache.
    record.state = RunState.RUNNING
    record.cancelled = False
    record.current_step_id = None
    record.finished_at = None
    run_registry[run_id] = record
    # Mark the drive live BEFORE the first await: the off-loop journal writes
    # below yield, and a concurrent resume for the same run_id must hit the
    # liveness guard during that window (B4-BR-7a — never double-drive). The
    # ``finally`` clears the mark on EVERY exit path (complete, fail, engine
    # error, cancel, journal-write error).
    _active_drives.add(run_id)
    try:
        try:
            await asyncio.to_thread(
                workflow_journal.update_run_state, run_id, RunState.RUNNING.value, None
            )
        except (
            Exception
        ) as e:  # noqa: BLE001 — journal write is best-effort; in-memory floor still serves live reads
            logger.warning("journal: resume reopen state write for '%s' failed: %s", run_id, e)
        # Persist the cleared current_step_id too — otherwise the durable row stays
        # stale until the first resumed step journals (best-effort, B4-BR-5).
        await _ajournal(_journal_current_step, record)

        # 7. Re-enter the SAME shared drive over the snapshotted spec's topo order
        # (B4-RD-5).
        return await _drive(record, _topological_order(spec))
    finally:
        _active_drives.discard(run_id)


def _run_parallel(record: Optional[RunRecord], steps: Any) -> None:
    """RESERVED (N7) — parallel/pipeline execution. Raises ``NotBuiltYetError``."""
    raise NotBuiltYetError("parallel/pipeline execution is reserved (not built yet; unit N7)")


def _run_loop(record: Optional[RunRecord], step: Any) -> None:
    """RESERVED (N8) — loop execution. Raises ``NotBuiltYetError``."""
    raise NotBuiltYetError("loop execution is reserved (not built yet; unit N8)")


def _eval_on_stall(paths: Any, n: Any) -> None:
    """RESERVED (N8) — stall guard. Raises ``NotBuiltYetError``."""
    raise NotBuiltYetError("on_stall guard is reserved (not built yet; unit N8)")


def _eval_on_no_progress(values: Any, k: Any, eps: Any) -> None:
    """RESERVED (N8) — no-progress guard. Raises ``NotBuiltYetError``."""
    raise NotBuiltYetError("on_no_progress guard is reserved (not built yet; unit N8)")
