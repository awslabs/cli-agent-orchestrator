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

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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
run_registry: Dict[str, RunRecord] = {}


def _now() -> str:
    """ISO-8601 Z timestamp (bookkeeping only — never an ordering key)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        return

    # Attempts exhausted: every attempt raised StepExecutionError.
    st.state = StepState.FAILED
    if (step.on_failure or "halt") == "halt":
        # Engine halts; start_run skips the remaining steps (§1). on_failure ==
        # "continue" leaves the run RUNNING and sequencing continues.
        record.state = RunState.FAILED


def _skip_remaining(record: RunRecord, order: List[WorkflowStep], from_index: int) -> None:
    """Mark every still-PENDING step at/after ``from_index`` SKIPPED (§1).

    The SINGLE producer of ``SKIPPED`` — reached on a halt (successors of the
    failed step) and on a cancel (remaining PENDING steps). A halted step itself
    is ``FAILED``, never ``SKIPPED`` (B3-BR-4 / B3-BR-7).
    """
    for step in order[from_index:]:
        st = record.step_states[step.id]
        if st.state == StepState.PENDING:
            st.state = StepState.SKIPPED


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
    (-> 409) if the run_id is already registered, ``NotBuiltYetError`` (-> reserved
    seam) for a non-sequential mode, ``WorkflowEngineError`` (-> 500) on an
    engine-internal invariant violation (e.g. a bad template reference).
    """
    # 1. Validate run_id via the shared key validator (B3-BR-1).
    _validate_key_part(run_id, "run_id")
    if run_id in run_registry:
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
    # 4. Register.
    run_registry[run_id] = record

    # 5. Deterministic sequencing order.
    order = _topological_order(spec)

    # 6. Sequencing loop. An unexpected engine error mid-loop (e.g. _substitute
    # raising WorkflowEngineError on a bad template reference — raised OUTSIDE
    # _run_step's StepExecutionError guard) must NOT leave the already-registered
    # record stranded in RUNNING. Settle it to a terminal FAILED state (with
    # finished_at) before re-raising so the boundary still maps it to 500 and the
    # registry is always left consistent (domain-entities lifecycle, B3-RD-3/RD-5).
    try:
        for index, step in enumerate(order):
            if record.cancelled:  # B3-BR-7 — cancel observed at a step boundary
                _skip_remaining(record, order, from_index=index)
                record.state = RunState.CANCELLED
                break
            await _run_step(record, step)
            if record.state == RunState.FAILED:  # halt (B3-BR-4 on_failure=halt)
                _skip_remaining(record, order, from_index=index + 1)
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
        logger.error("start_run: run '%s' failed with an engine error", run_id)
        raise

    # 7. Finalize.
    if record.state not in (RunState.FAILED, RunState.CANCELLED):
        record.state = RunState.COMPLETED
    record.current_step_id = None
    record.finished_at = _now()

    # 8. Aggregate the result.
    return _build_result(record, order)


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


def resume_from_last_completed(run_id: str) -> WorkflowRunResult:
    """RESERVED (N6) — durable resume. Raises ``NotBuiltYetError`` (B3-BR-10)."""
    raise NotBuiltYetError("resume_from_last_completed is reserved (not built yet; unit N6)")


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
