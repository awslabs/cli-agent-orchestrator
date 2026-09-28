"""Lightweight workflow runtime DTOs (issue #312, Bolt 2).

These are the transient, runtime-facing value objects of the workflow feature:
the derived index row, the structured step-output record, and the MCP return
envelope — plus the per-step ``StepState`` enum they share.

They live in a SEPARATE module from ``models/workflow.py`` ON PURPOSE: the spec
grammar in ``workflow.py`` imports ``jsonschema`` and ``yaml`` at module scope,
and the MCP server (``mcp_server/server.py``) must stay lightweight on the single
HTTP seam — it consumes ``ReturnAck`` but has no business pulling a JSON-schema
validator + YAML parser into its process just to name a Pydantic envelope. This
module imports neither. ``models/workflow.py`` re-exports every name here, so
existing ``from ...models.workflow import StepState`` call sites are unaffected.
"""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StepState(str, Enum):
    """Per-step run state. Defined in Bolt 1; instantiated by the engine (N5).

    ``recovery-decision-intake`` (issue #583, unit 12) appends the two AUTHORISED
    states — the durable form of a human's decision at a halted step (TD-2). They
    are the only members no engine writes: ``apply_decisions`` writes them and the
    replay gate reads them, one at rule 1 and one at rule 7's exclusion.

    **Additive and verified safe:** nothing in ``src/`` or ``test/`` iterates or
    asserts this member set (no ``for … in StepState``, no ``list(StepState)``, no
    ``__members__``), so appending breaks no exhaustive match.

    **``SKIPPED`` was NOT reused for the ``skip`` decision** (BR-3/TD-2). It already
    means "the engine did not run this step", and mechanically it could not serve:
    the gate treats anything that is not absent, ``rerun_authorized`` or ``running``
    as settled, so a ``SKIPPED`` row still reaches rule 7 and halts again — the very
    loop ``REPLAY_AUTHORIZED`` exists to close.
    """

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPLETED_UNVALIDATED = "completed_unvalidated"
    # issue #583, recovery-decision-intake (unit 12) — consent to re-execute. Rule 1
    # of the replay gate already admits this literal as ``EXECUTE`` (BR-2), and
    # ``begin_step`` consumes it by flipping the row to ``running`` (BR-9), which is
    # what bounds one decision to one attempt.
    RERUN_AUTHORIZED = "rerun_authorized"
    # issue #583, recovery-decision-intake (unit 12) — consent to use the STORED
    # result. Excluded from rule 7 ONLY (BR-4), so the row still needs an envelope
    # (rule 3), verifiable provenance (rules 4-5) and a matching fingerprint (rule 6)
    # before it falls to the catch-all and replays.
    REPLAY_AUTHORIZED = "replay_authorized"


class RunState(str, Enum):
    """Whole-run state. Defined in Bolt 1; instantiated by the engine (N5).

    Lives in this light module (re-exported by ``models/workflow.py``) so the MCP
    seam can name a run state without pulling the jsonschema/yaml grammar module.
    Bolt 3 adds ``CANCELLED`` (B3-BR-12) so a user-cancelled run is distinguishable
    from an engine-``FAILED`` run. Additive enum change — no existing value altered.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# recovery-decision-intake (issue #583, unit 12) — FR-7's escape hatch vocabulary
# ---------------------------------------------------------------------------
class RecoveryDecision(str, Enum):
    """What a human OPERATOR chooses at a halted step (issue #583, FR-7, BR-1).

    **This is NOT ``RecoveryPolicy``, and the one shared word is why the distinction
    is written down** (domain-entities):

    * ``RecoveryPolicy`` (``models/workflow.py``) is a DECLARATION by the step's
      author, made at authoring time in the script — "never a permission and never
      inferred". Its members are ``idempotent``, ``reconcile``, ``manual``.
    * ``RecoveryDecision`` is a PERMISSION from an operator, given at a halt on the
      resume command, and good for exactly ONE attempt (BR-9).

    TWO MEMBERS, NOT THREE. ``reconcile`` is deliberately absent (BR-1/TD-1): no
    reconciliation operation exists anywhere in ``src/`` and #583's frozen scope
    defers generic external-system reconciliation, so a third member would be a
    value the closed set cannot act on — the same trap that removed
    ``diverged_fields`` and ``attempts`` elsewhere in this Bolt. It also removes the
    name collision with ``RecoveryPolicy.RECONCILE``, which means something adjacent
    but different. When the operation ships, BR-1 is where the third member starts.

    It lives in THIS light module rather than beside ``RecoveryPolicy`` in
    ``models/workflow.py`` for the reason stated at the top of the file: the MCP
    server imports this module and must not pull ``jsonschema``/``yaml`` onto the
    HTTP seam, and ``workflow_resume`` needs this vocabulary to validate its
    ``decisions`` argument (BR-10). ``models/workflow.py`` re-exports both names, so
    ``from ...models.workflow import RecoveryDecision`` also resolves. A policy is
    spec GRAMMAR (authored in the file); a decision is RUNTIME input (typed at a
    halt), so the split also follows what each module is for.
    """

    RERUN = "rerun"  # consent to re-execute the step -> state ``rerun_authorized``
    SKIP = "skip"  # consent to use the stored result -> state ``replay_authorized``


def parse_decision(value: str) -> RecoveryDecision:
    """Parse one operator-supplied decision value. Total: returns or raises (BR-6).

    THE SINGLE VALIDATION IMPLEMENTATION for the closed set, shared by all three
    surfaces and by ``workflow_journal.apply_decisions`` (BR-10/TD-7) — the CLI's
    ``--decide``, the MCP ``workflow_resume(decisions=...)`` argument and the resume
    route must accept exactly the same values, and one function is how that stays
    true rather than being asserted three times.

    Mirrors ``parse_policy``'s shape (``models/workflow.py``) with one deliberate
    difference: there is no "undeclared" case here. A policy may legitimately be
    absent; a decision that is absent is simply not supplied, and an empty string is
    a typo like any other. No case-folding, no stripping, no aliasing.

    Raises:
        ValueError: naming the accepted values, because the caller is a human who
            has just mistyped one. The offending value is echoed TRUNCATED — the
            operator needs to see their own typo, and the bound keeps an abusive
            value from becoming an unbounded response body. It is never logged
            (SR-6 logs the success path only).
    """
    try:
        return RecoveryDecision(value)
    except ValueError as e:
        accepted = ", ".join(sorted(member.value for member in RecoveryDecision))
        raise ValueError(
            f"'{str(value)[:40]}' is not a recovery decision (accepted: {accepted})"
        ) from e


# ---------------------------------------------------------------------------
# Bolt 2 (N2/N4) — derived index row + structured-return record/ack
# ---------------------------------------------------------------------------
class WorkflowIndexRow(BaseModel):
    """A derived, non-authoritative projection of a ``WorkflowSpec`` (C4, B2-BR-2).

    Materializes a spec for fast listing. Never authored directly; the whole
    ``workflow_index`` table is droppable and rebuildable byte-identically from
    the YAML files on disk (B2-BR-3). Carries NO execution state — runs and
    per-step state are N5/N6, not here.
    """

    name: str
    source_path: str
    mode: str
    step_count: Optional[int]
    description: str = ""
    indexed_at: str


class StepOutputRecord(BaseModel):
    """The unit of the in-memory structured-return store (N4, C5, ADR-4).

    Keyed by ``(run_id, step_id)``. In-memory in the MVP; the same shape becomes
    the N6 journal row with no contract change. ``state`` is the candidate
    end-state the engine (N5, Bolt 3) acts on: ``COMPLETED`` when the output
    validated against the step ``output_schema``, else ``COMPLETED_UNVALIDATED``
    (B2-BR-7 / B2-BR-8). Bolt 2 *populates* these two values; it never drives the
    reprompt loop.
    """

    run_id: str
    step_id: str
    output: Dict[str, Any]
    validated: bool
    errors: List[str] = Field(default_factory=list)
    state: StepState


class ReturnAck(BaseModel):
    """Structured envelope the MCP ``workflow_return`` tool returns (C6, B2-BR-9).

    Mirrors the existing handoff-tool envelope shape; it is **never** an
    exception. ``ReturnAck.validated=False`` tells the worker its output did not
    validate — it does NOT claim the step ran or will run (Q1 honesty discipline);
    the recovery (reprompt-once) is the engine's, Bolt 3.
    """

    ok: bool = Field(description="Whether the endpoint accepted and stored the output")
    validated: bool = Field(description="Whether output passed the step output_schema")
    errors: List[str] = Field(default_factory=list, description="Schema-violation reasons, if any")


# ---------------------------------------------------------------------------
# Bolt 3 (N5) — run-engine seam DTOs
# ---------------------------------------------------------------------------
# These are the LIGHT, seam-facing value objects the run engine returns and the
# MCP server consumes (B3-BR-15: the MCP seam consumes only these DTOs, never the
# jsonschema/yaml grammar module). The engine-internal aggregate (``RunRecord`` /
# ``StepRunState``) lives in ``services/workflow_service.py`` because it holds the
# heavy ``WorkflowSpec`` and never crosses the HTTP seam.
class StepStatus(BaseModel):
    """One step's point-in-time status inside a ``RunStatus`` snapshot (B3-BR-8).

    Carries only the per-step floor (FR-6.4): id, state, attempt count. It does
    NOT carry the step's output or prompt (B3-SD-3 — status leaks no payload).
    """

    id: str
    state: StepState
    attempts: int


class RunStatus(BaseModel):
    """Point-in-time snapshot of a run (``get_run_status``, B3-BR-8 / Q3=A).

    A COPY of the registry record's observable state — never a live reference.
    Loop fields (``which_guard_fired``/``iterations_run``) are omitted in the MVP
    (B3-BR-11). Carries no per-step output or prompt (B3-SD-3).
    """

    run_id: str
    state: RunState
    current_step_id: Optional[str] = None
    steps: List[StepStatus] = Field(default_factory=list)


class StepResult(BaseModel):
    """Aggregated per-step result inside a ``WorkflowRunResult``.

    Unlike ``StepStatus`` this is the FINAL (run-complete) per-step record and may
    carry the collected structured output (the run owner already drove the run, so
    there is no status-leak concern here — this is the run's own result envelope).
    """

    id: str
    state: StepState
    attempts: int
    output: Optional[Dict[str, Any]] = None
    error: Optional[str] = None


class WorkflowRunResult(BaseModel):
    """Aggregated result returned by ``start_run`` / ``workflow_run`` (FR-5.2).

    The SINGLE run return type (supersedes C3's ``RunHandle`` name): the final
    ``RunState`` plus every step's terminal state, attempt count, and collected
    output.
    """

    run_id: str
    workflow_name: str
    state: RunState
    steps: List[StepResult] = Field(default_factory=list)
    started_at: str
    finished_at: Optional[str] = None
    # Bolt 3 (U4/C1) additive envelope fields — Optional/defaulted so a YAML run's
    # shape stays byte-identical (INV-5, M4 tripwire). A YAML run leaves
    # ``kind=None``, ``output=None``, ``warnings=[]``.
    kind: Optional[str] = None  # error|timeout|cancelled; None on COMPLETED (BR-3, Q1=A)
    output: Optional[Any] = None  # last-match CAO_WORKFLOW_OUTPUT: JSON (BR-7/8/9, Q2=A)
    warnings: List[str] = Field(default_factory=list)  # e.g. "malformed sentinel payload" (BR-9)
