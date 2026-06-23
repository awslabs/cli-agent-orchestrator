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
from typing import Any, Dict, List

from pydantic import BaseModel, Field


class StepState(str, Enum):
    """Per-step run state. Defined in Bolt 1; instantiated by the engine (N5)."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"
    COMPLETED_UNVALIDATED = "completed_unvalidated"


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
