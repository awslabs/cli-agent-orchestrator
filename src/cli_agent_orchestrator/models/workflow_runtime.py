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
    """Per-step run state. Defined in Bolt 1; instantiated by the engine (N5)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPLETED_UNVALIDATED = "completed_unvalidated"


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
    step_count: int
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
