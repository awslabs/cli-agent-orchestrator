"""Workflow spec domain model + grammar validation (issue #312, unit N1).

This module defines the native multi-agent workflow object: a trusted-author
YAML spec describing a DAG of agent steps. Bolt 1 ships only the *grammar* —
the Pydantic model, the aggregating ``validate_grammar`` model-validator, and
the non-raising ``validate_only`` service surface. It NEVER executes a spec.

Key invariants (see functional-design/business-rules.md):
- BR-1: spec name + every step id match ``WORKFLOW_NAME_RE``; step ids unique.
- BR-3: a construct being *reserved* (unit not shipped) is NEVER a grammar
  error. Reserved tagging is ``validate_only``'s job, surfaced honestly
  ("construct X is reserved (not built yet)") — never "X will run".
- BR-4: a loop step MUST declare all three guards (max_iterations, on_stall,
  on_no_progress) — a *structural* floor enforced now even though loop
  execution is N8.
- BR-6: ``output_schema``, if present, MUST be well-formed JSON-Schema
  (validated with the jsonschema library's own meta-schema — SD-3.1).
- BR-7: ``validate_grammar`` raises one aggregated ``ValueError`` (never
  fail-on-first); ``validate_only`` NEVER raises — it returns a
  ``ValidationResult``.

``NotBuiltYetError`` is *defined* here but RAISED only by the run engine (N5,
Bolt 3) when sequencing reaches a reserved construct. Bolt 1 never raises it.
"""

from __future__ import annotations

import logging
import re
from enum import Enum
from typing import Any, Dict, List, Literal, Optional, Union

import jsonschema  # type: ignore[import-untyped]  # no bundled stubs; meta-schema API is stable
import yaml
from pydantic import BaseModel, Field, model_validator

from cli_agent_orchestrator.constants import (
    WORKFLOW_MAX_INPUTS,
    WORKFLOW_MAX_RETRIES,
    WORKFLOW_MAX_SPEC_BYTES,
    WORKFLOW_MAX_STEPS,
    WORKFLOW_NAME_RE,
    WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH,
    WORKFLOW_SHIPPED_UNITS,
)

# Re-exported from the light runtime module so existing
# ``from ...models.workflow import StepState / WorkflowIndexRow / ...`` call
# sites are unaffected. The DTOs themselves live in ``workflow_runtime`` (which
# imports no jsonschema/yaml) so the MCP server can consume ``ReturnAck`` without
# pulling this grammar module's heavy deps onto the HTTP seam.
from cli_agent_orchestrator.models.workflow_runtime import (  # noqa: F401
    ReturnAck,
    RunState,
    RunStatus,
    StepOutputRecord,
    StepResult,
    StepState,
    StepStatus,
    WorkflowIndexRow,
    WorkflowRunResult,
)

logger = logging.getLogger(__name__)

# Compiled once at import; validate_grammar is a pure function of the spec.
_NAME_RE = re.compile(WORKFLOW_NAME_RE)


# ---------------------------------------------------------------------------
# Reserved-construct tier registry (BR-3) — the load-bearing rule
# ---------------------------------------------------------------------------
# Maps each construct token to the units-generation unit that *implements* its
# execution. A construct is "reserved" iff its implementing unit is not in
# WORKFLOW_SHIPPED_UNITS. ``sequential`` is the one executable mode (from N5);
# it has no entry here because it never tags reserved (its absence => shipped
# by definition once N5 lands; in Bolt 1 nothing executes, but a sequential-only
# spec is the canonical "pass" case per the Bolt-1 flag-flip test).
TIER_REGISTRY: Dict[str, str] = {
    "parallel": "N7",
    "pipeline": "N7",
    "loop": "N8",
    "until": "N8",
    "max_iterations": "N8",
    "on_stall": "N8",
    "on_no_progress": "N8",
    "when": "reserved",
    "rich_templating": "reserved",
}


def is_reserved(construct: str) -> bool:
    """Return True if ``construct``'s implementing unit has NOT shipped yet.

    Determined solely by TIER_REGISTRY + WORKFLOW_SHIPPED_UNITS (constants.py)
    — deterministic, no env-dependent branching (REL-2/RD-3.1). An unknown
    construct (not in the registry) is treated as non-reserved.
    """
    unit = TIER_REGISTRY.get(construct)
    if unit is None:
        return False
    return unit not in WORKFLOW_SHIPPED_UNITS


# ---------------------------------------------------------------------------
# Enums / reserved vocabulary (defined here; animated by N5+)
# ---------------------------------------------------------------------------
class LoopGuard(str, Enum):
    """Loop guardrail vocabulary (FR-8.2). Validated structurally now (BR-4),
    enforced at run time by N8."""

    MAX_ITERATIONS = "max_iterations"
    ON_STALL = "on_stall"
    ON_NO_PROGRESS = "on_no_progress"
    UNTIL_SATISFIED = "until_satisfied"


class NotBuiltYetError(Exception):
    """Raised by the run engine (N5+) when sequencing reaches a reserved
    construct whose implementing unit has not shipped.

    DEFINED in Bolt 1 (N1) so the vocabulary exists; NEVER raised here. Bolt 1
    only validates-and-stores reserved constructs; it never executes a spec.
    """


# ---------------------------------------------------------------------------
# Spec entities
# ---------------------------------------------------------------------------
class InputDecl(BaseModel):
    """A typed run-time input declaration (FR-1.5).

    A ``path``-typed input is validated by the shared path validator at run
    start (N5), never re-implemented here (project Mandated rule). Declared
    here so the model carries the contract.
    """

    type: Literal["string", "int", "bool", "path"]
    required: bool = False
    default: Optional[Union[str, int, bool]] = None


class WorkflowStep(BaseModel):
    """A single agent step in a workflow DAG.

    Reserved fields (``when`` and the loop fields) validate structurally in
    Bolt 1 but never execute; the engine (N5/N8) animates them later.
    """

    id: str
    provider: str
    agent: str
    prompt: str
    output_schema: Optional[Dict[str, Any]] = None
    # RESERVED — conditional execution (no MVP unit). Validates, never runs.
    when: Optional[str] = None
    # RESERVED loop fields (N8). Validate-but-inert; BR-4 requires all-three if any.
    until: Optional[str] = None
    max_iterations: Optional[int] = None
    on_stall: Optional[str] = None
    on_no_progress: Optional[str] = None
    # Per-step failure policy (FR-5.3). Semantics are N5's.
    on_failure: Optional[Literal["halt", "continue"]] = None
    # Per-step run-failure retry budget (FR-5.3, B3-BR-3). Separate from
    # ``on_failure``: ``retries`` = how many EXTRA attempts on a run-failure
    # (None -> engine default of WORKFLOW_DEFAULT_STEP_RETRIES); ``on_failure`` =
    # what to do once attempts are exhausted. ``retries: 0`` => exactly one
    # attempt, no retry. Validated in ``validate_grammar`` (0..WORKFLOW_MAX_RETRIES).
    retries: Optional[int] = None

    def is_loop(self) -> bool:
        """True if this step declares ANY loop field (so BR-4 applies)."""
        return any(
            v is not None
            for v in (self.until, self.max_iterations, self.on_stall, self.on_no_progress)
        )


class WorkflowSpec(BaseModel):
    """A workflow spec aggregate root (C1).

    Construction triggers ``validate_grammar`` (a Pydantic ``mode="after"``
    model validator) which aggregates ALL structural violations into one
    ``ValueError`` (BR-7). Reserved-ness is NOT a grammar error (BR-3).
    """

    name: str
    description: str = ""
    mode: Literal["sequential", "parallel", "pipeline", "loop"] = "sequential"
    inputs: Dict[str, InputDecl] = Field(default_factory=dict)
    steps: List[WorkflowStep]

    @model_validator(mode="after")
    def validate_grammar(self) -> "WorkflowSpec":
        """Aggregate every structural/name/guard/schema violation, then raise
        one ``ValueError`` (BR-1/BR-4/BR-5/BR-6/BR-7).

        Pure function of the spec — no clock/random/network/FS. Same bytes =>
        same stably-ordered error set (RD-3.1). Reserved-ness is intentionally
        NOT checked here (BR-3); a ``mode: parallel`` spec passes this validator.
        """
        errors: List[str] = []

        # 1. Names (BR-1)
        if not _NAME_RE.fullmatch(self.name):
            errors.append(f"workflow name '{self.name}' is invalid (must match {WORKFLOW_NAME_RE})")
        seen: set[str] = set()
        for step in self.steps:
            if not _NAME_RE.fullmatch(step.id):
                errors.append(f"step id '{step.id}' is invalid (must match {WORKFLOW_NAME_RE})")
            if step.id in seen:
                errors.append(f"duplicate step id '{step.id}'")
            seen.add(step.id)

        # 2. Size caps (structural; byte cap is enforced pre-parse in validate_only)
        if len(self.steps) < 1:
            errors.append("workflow must declare at least one step")
        if len(self.steps) > WORKFLOW_MAX_STEPS:
            errors.append(f"workflow has {len(self.steps)} steps (max {WORKFLOW_MAX_STEPS})")
        if len(self.inputs) > WORKFLOW_MAX_INPUTS:
            errors.append(
                f"workflow declares {len(self.inputs)} inputs (max {WORKFLOW_MAX_INPUTS})"
            )

        # 3. Typed inputs (BR-5): default must match declared type.
        for key, decl in self.inputs.items():
            if decl.default is not None and not _default_matches_type(decl.default, decl.type):
                errors.append(
                    f"input '{key}': default {decl.default!r} does not match declared "
                    f"type '{decl.type}'"
                )

        # 4. output_schema well-formedness (BR-6/SD-3.1) + depth cap.
        for step in self.steps:
            if step.output_schema is not None:
                schema_error = _check_output_schema(step.output_schema)
                if schema_error is not None:
                    errors.append(f"step '{step.id}': {schema_error}")

        # 4b. Per-step retry budget (B3-BR-3): if present, must be an integer in
        # [0, WORKFLOW_MAX_RETRIES]. Omitted -> engine default (regression-safe:
        # a spec without ``retries`` parses exactly as before). ``bool`` is a
        # subclass of ``int`` so reject it explicitly (``retries: true`` is not 1).
        for step in self.steps:
            if step.retries is not None:
                if isinstance(step.retries, bool) or not isinstance(step.retries, int):
                    errors.append(
                        f"step '{step.id}': retries must be an integer "
                        f"(0..{WORKFLOW_MAX_RETRIES})"
                    )
                elif not (0 <= step.retries <= WORKFLOW_MAX_RETRIES):
                    errors.append(
                        f"step '{step.id}': retries {step.retries} out of range "
                        f"(0..{WORKFLOW_MAX_RETRIES})"
                    )

        # 5. Loop guardrail completeness — structural floor (BR-4/FR-8.2).
        for step in self.steps:
            if step.is_loop():
                missing = [
                    name
                    for name, value in (
                        ("max_iterations", step.max_iterations),
                        ("on_stall", step.on_stall),
                        ("on_no_progress", step.on_no_progress),
                    )
                    if value is None
                ]
                if missing:
                    errors.append(
                        f"step '{step.id}': loop must declare all three guards "
                        f"(missing {', '.join(missing)})"
                    )

        if errors:
            raise ValueError("; ".join(errors))
        return self


# ---------------------------------------------------------------------------
# Validation result (returned by validate_only; never raises) — FR-1.3
# ---------------------------------------------------------------------------
class ValidationResult(BaseModel):
    """Structured outcome of ``validate_only``.

    ``pass`` — valid, uses only shipped (executable) constructs.
    ``pass_reserved`` — structurally valid, but uses ≥1 reserved construct;
        ``reserved_notes`` carries honesty-worded "not built yet" notes.
    ``fail`` — structural/name/guard/schema/parse error; ``errors`` has reasons.
    """

    status: Literal["pass", "pass_reserved", "fail"]
    reserved_notes: List[str] = Field(default_factory=list)
    errors: List[str] = Field(default_factory=list)


class LintFinding(BaseModel):
    """One script-lint observation (Bolt 2, U1/C2 — the FR-2.3 findings shape).

    Every finding carries all four fields; ``line`` is a REQUIRED 1-based
    source anchor (FR-2.3 — no finding may omit it).
    """

    rule_id: Literal["syntax", "disallowed-import", "nondeterminism", "dynamic-import"]
    severity: Literal["error", "warning"]
    line: int
    message: str


class TierCollisionError(ValueError):
    """Same-stem name exists in both tiers within one scan dir (U5, FR-4.2).

    A ``ValueError`` subclass — mapped to 409 (a resource conflict, not a
    malformed request) at the API boundary, BEFORE any bare ``ValueError``
    catch, matching the precedent U3 established for
    ``ResumeNotAllowedError``/``ResumeCorruptError``.
    """

    def __init__(self, stem: str) -> None:
        super().__init__(
            f"workflow '{stem}' exists in both the script and YAML tiers "
            "(same-stem collision across tiers)"
        )
        self.stem = stem


class ScriptSpec(BaseModel):
    """The ``.py`` counterpart to ``WorkflowSpec`` (U5, E1, C4).

    A pydantic ``BaseModel`` (not a plain dataclass) — every route that
    serializes whatever ``get_workflow`` returns calls ``.model_dump()``
    unconditionally, with no ``isinstance`` branch on the read side
    (BR-7a). TRANSIENT: resolved fresh per ``get_workflow`` call, never
    persisted as a row.
    """

    name: str
    path: str
    source: str
    content_hash: str
    findings: List[LintFinding] = Field(default_factory=list)
    # Typed run-time input declarations extracted from a module-level ``INPUTS``
    # dict literal in the script source (Unit A, FR-A1). AST-parsed at read time,
    # never executed. Absent ``INPUTS`` -> empty dict. Reuses ``InputDecl`` (the
    # same contract the YAML tier carries) so the shared ``_validate_inputs``
    # generalizes across both tiers without a second declaration shape.
    inputs: Dict[str, InputDecl] = Field(default_factory=dict)


class ScriptValidationResult(ValidationResult):
    """The script tier's validate outcome — additive over ``ValidationResult``.

    Subclasses the shipped base (U1-BR-7, Q4=A) so YAML validate output stays
    byte-identical (FR-5.1) and every base-vocabulary consumer renders a
    script result unmodified. ``status`` is INHERITED unnarrowed; a script
    result is never ``pass_reserved`` — enforced by construction in
    ``lint_script`` (only ``pass``/``fail`` are ever emitted), not by
    re-typing the field. ``errors`` carries the Q5=A mirror: one
    ``"line N: [rule_id] message"`` string per ERROR finding; warnings appear
    ONLY in ``findings``.
    """

    tier: Literal["script"] = "script"
    findings: List[LintFinding] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _default_matches_type(default: Union[str, int, bool], declared: str) -> bool:
    """Return True if ``default`` is consistent with the declared input type.

    Note ``bool`` is checked BEFORE ``int`` because ``bool`` is a subclass of
    ``int`` in Python — otherwise ``True`` would spuriously satisfy ``int``.
    """
    if declared == "bool":
        return isinstance(default, bool)
    if declared == "int":
        return isinstance(default, int) and not isinstance(default, bool)
    # "string" and "path" both carry their value as a string at authoring time.
    return isinstance(default, str)


def _schema_depth(node: Any, _depth: int = 1, _cap: Optional[int] = None) -> int:
    """Max nesting depth of a JSON-Schema-like dict/list structure.

    Bails as soon as the depth exceeds ``_cap`` (when given) instead of fully
    descending first. A spec nested deeper than Python's recursion limit is
    reachable well within the byte cap, so a full descent would raise
    ``RecursionError`` and escape the non-raising ``validate_only`` contract
    (FR-1.3). Returning ``_cap + 1`` is enough for the caller's ``> cap`` check.
    """
    if _cap is not None and _depth > _cap:
        return _depth
    if isinstance(node, dict):
        if not node:
            return _depth
        return max(_schema_depth(v, _depth + 1, _cap) for v in node.values())
    if isinstance(node, list):
        if not node:
            return _depth
        return max(_schema_depth(v, _depth + 1, _cap) for v in node)
    return _depth


def _check_output_schema(schema: Dict[str, Any]) -> Optional[str]:
    """Validate an ``output_schema`` is well-formed JSON-Schema + within depth.

    Uses the jsonschema library's OWN meta-schema (Draft 2020-12) so the author
    schema and N4's runtime worker-output validation agree (same validator
    family) and the parser-divergence bypass is closed (SD-3.1). Returns an
    error string, or None if the schema is acceptable.
    """
    # Pass the cap so the walk short-circuits instead of fully descending a
    # pathologically nested schema (RecursionError would escape validate_only).
    depth = _schema_depth(schema, _cap=WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH)
    if depth > WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH:
        return (
            f"output_schema nesting depth {depth} exceeds max "
            f"{WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH}"
        )
    try:
        jsonschema.Draft202012Validator.check_schema(schema)
    except jsonschema.exceptions.SchemaError as exc:
        # The library's message can be multi-line; collapse to the first line
        # so the aggregated ValueError stays single-line and stably ordered.
        first_line = str(exc).splitlines()[0] if str(exc) else "invalid JSON-Schema"
        return f"output_schema is not valid JSON-Schema: {first_line}"
    return None


def _constructs_used(spec: WorkflowSpec) -> List[str]:
    """Collect the construct tokens a spec actually uses (stable order).

    Used by ``validate_only`` to compute reserved notes. Order is deterministic
    (mode first, then per-step fields in declaration order) so reserved_notes
    diffs are stable (RD-3.1).
    """
    used: List[str] = []
    if spec.mode != "sequential":
        used.append(spec.mode)
    for step in spec.steps:
        for token, value in (
            ("when", step.when),
            ("until", step.until),
            ("max_iterations", step.max_iterations),
            ("on_stall", step.on_stall),
            ("on_no_progress", step.on_no_progress),
        ):
            if value is not None and token not in used:
                used.append(token)
    return used


def _reserved_note(construct: str) -> str:
    """Honesty-worded reserved note (BR-3) — NEVER implies the construct runs."""
    unit = TIER_REGISTRY.get(construct, "reserved")
    if unit == "reserved":
        return f"construct '{construct}' is reserved (not built yet)"
    return f"construct '{construct}' is reserved (not built yet; implemented by unit {unit})"


def validate_only(text: str) -> ValidationResult:
    """Validate a workflow spec WITHOUT running it (FR-1.3). NEVER raises.

    Accepts the raw YAML **text** of a spec (NOT a path). The byte-cap is
    enforced BEFORE parsing so an oversized spec fails fast without constructing
    a giant object graph. On any parse/grammar failure returns ``status="fail"``
    with split reasons (BR-7). On success, reserved constructs (per
    TIER_REGISTRY + WORKFLOW_SHIPPED_UNITS) yield ``pass_reserved`` with
    honesty-worded notes; a fully-shipped (sequential-only) spec yields ``pass``.

    This function does NO filesystem access (SD-1.2): it never opens a path, so
    no user-controlled value reaches a file API here. File reading lives behind
    the shared path validator in ``workflow_spec_service`` (``_safe_spec_path``),
    which decodes the bytes and passes the resulting text down to us. Keeping the
    model text-only removes the path-injection sink at the source rather than
    relying on the scanner to recognize a sanitizer across a call boundary.
    """
    # Byte cap on the supplied text. The service enforces the same cap on raw
    # bytes before decoding; this guards direct text callers (e.g. the API
    # validate endpoint and unit tests) and any oversized post-decode content.
    if len(text.encode("utf-8")) > WORKFLOW_MAX_SPEC_BYTES:
        return ValidationResult(
            status="fail",
            errors=[f"spec exceeds byte cap (max {WORKFLOW_MAX_SPEC_BYTES})"],
        )

    # Parse YAML, then construct the spec (triggers validate_grammar).
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.debug("validate_only: malformed YAML: %s", exc)
        return ValidationResult(status="fail", errors=[f"malformed YAML: {exc}"])

    if not isinstance(data, dict):
        return ValidationResult(status="fail", errors=["spec root must be a mapping (YAML object)"])

    try:
        spec = WorkflowSpec(**data)
    except (ValueError, TypeError) as exc:
        # Pydantic ValidationError is a ValueError subclass; our aggregated
        # grammar ValueError lands here too. TypeError is caught because YAML
        # admits non-string mapping keys (e.g. ``123: foo``) and ``**``-unpacking
        # such a dict raises TypeError — which must not escape the never-raise
        # contract (FR-1.3). Split on our "; " join so each structural reason is
        # a separate entry; Pydantic field errors arrive as a single
        # (multi-line) string, kept whole.
        message = _extract_validation_message(exc)
        reasons = [r for r in (part.strip() for part in message.split("; ")) if r]
        logger.debug("validate_only: spec failed grammar: %s", reasons)
        return ValidationResult(status="fail", errors=reasons or [message])

    reserved = [_reserved_note(c) for c in _constructs_used(spec) if is_reserved(c)]
    if reserved:
        return ValidationResult(status="pass_reserved", reserved_notes=reserved)
    return ValidationResult(status="pass")


def _extract_validation_message(exc: Exception) -> str:
    """Pull a readable message out of an exception (incl. Pydantic's nested form).

    Our ``validate_grammar`` raises a plain ``ValueError("a; b; c")``; Pydantic
    wraps validator ValueErrors in a ``ValidationError`` whose ``str`` is
    multi-line; a non-string YAML key surfaces as a ``TypeError``. We return the
    joined grammar message when present, else the full string.
    """
    # Pydantic ValidationError exposes .errors(); pull each msg out so our
    # aggregated "a; b; c" survives the wrapping.
    errors_fn = getattr(exc, "errors", None)
    if callable(errors_fn):
        try:
            msgs = []
            for err in errors_fn():
                msg = err.get("msg", "")
                # Pydantic prefixes value errors with "Value error, "
                msgs.append(msg.replace("Value error, ", "", 1))
            if msgs:
                return "; ".join(msgs)
        except Exception:  # noqa: BLE001 — defensive: fall back to str(exc) on any odd shape
            logger.debug("validate_only: could not introspect ValidationError, using str()")
    return str(exc)
