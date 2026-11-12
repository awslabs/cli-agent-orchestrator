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
from pydantic import BaseModel, Field, SkipValidation, model_validator

from cli_agent_orchestrator.constants import (
    WORKFLOW_MAX_INPUTS,
    WORKFLOW_MAX_RETRIES,
    WORKFLOW_MAX_SPEC_BYTES,
    WORKFLOW_MAX_STEP_PARAMS,
    WORKFLOW_MAX_STEPS,
    WORKFLOW_NAME_RE,
    WORKFLOW_OUTPUT_SCHEMA_MAX_DEPTH,
    WORKFLOW_PLACEHOLDER_RE,
    WORKFLOW_SHIPPED_UNITS,
    WORKFLOW_STEP_PARAM_KEY_RE,
    WORKFLOW_STEP_PARAM_LINE_BREAK_RE,
    WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS,
)
from cli_agent_orchestrator.models.kiro_engine import KiroEngine

# Re-exported from the light runtime module so existing
# ``from ...models.workflow import StepState / WorkflowIndexRow / ...`` call
# sites are unaffected. The DTOs themselves live in ``workflow_runtime`` (which
# imports no jsonschema/yaml) so the MCP server can consume ``ReturnAck`` without
# pulling this grammar module's heavy deps onto the HTTP seam.
from cli_agent_orchestrator.models.workflow_runtime import (  # noqa: F401
    RecoveryDecision,
    ReturnAck,
    RunState,
    RunStatus,
    StepOutputRecord,
    StepResult,
    StepState,
    StepStatus,
    WorkflowIndexRow,
    WorkflowRunResult,
    parse_decision,
)

logger = logging.getLogger(__name__)

# Compiled once at import; validate_grammar is a pure function of the spec.
_NAME_RE = re.compile(WORKFLOW_NAME_RE)
_PARAM_KEY_RE = re.compile(WORKFLOW_STEP_PARAM_KEY_RE)
_LINE_BREAK_RE = re.compile(WORKFLOW_STEP_PARAM_LINE_BREAK_RE)
_PLACEHOLDER_RE = re.compile(WORKFLOW_PLACEHOLDER_RE)
_WHITESPACE_RUN_RE = re.compile(r"\s+")


# ---------------------------------------------------------------------------
# Reserved-construct tier registry (BR-3) — the load-bearing rule
# ---------------------------------------------------------------------------
# Maps each construct token to the units-generation unit that *implements* its
# execution. A construct is "reserved" iff its implementing unit is not in
# WORKFLOW_SHIPPED_UNITS. ``sequential`` is the one executable mode (from N5);
# it has no entry here because it never tags reserved (its absence => shipped
# by definition once N5 lands; in Bolt 1 nothing executes, but a sequential-only
# spec is the canonical "pass" case per the Bolt-1 flag-flip test).
#
# ``assign`` and ``send_message`` are step ``call`` types. ``handoff`` (or no
# ``call``) is the only one the engine runs, so it has no entry; the other two
# validate as reserved and are refused at run start.
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
    "assign": "reserved",
    "send_message": "reserved",
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


class RecoveryPolicy(str, Enum):
    """What a step's author declares about re-running it (issue #583, FR-5/FR-7).

    A DECLARATION about the step, never a permission and never inferred (SR-2/SR-3):
    CAO has no mechanism to prove a step's external effects and is not required to.

    Absence is NOT a member — it is ``None`` (BR-1/BR-2). "Undeclared" and ``MANUAL``
    behave identically wherever the alternative is re-execution, and differently where a
    verified replay is available: ``MANUAL`` halts anyway because a human asked to see the
    step; undeclared replays, because replay executes nothing.
    """

    IDEMPOTENT = "idempotent"
    RECONCILE = "reconcile"
    MANUAL = "manual"


def parse_policy(value: Optional[str]) -> Optional[RecoveryPolicy]:
    """Parse a declared recovery policy. Total: returns or raises (INV-1).

    ``None``/``""`` -> ``None`` (not declared — a distinct state, NEVER coerced to
    ``MANUAL``, BR-2). Any other value must be one of the three; anything else raises
    ``ValueError`` so a typo becomes a 422 rather than a silent downgrade to undeclared
    (BR-3). No case-folding, no stripping, no aliasing (BR-4).

    NOTE ON REACHABILITY (TD-1): the run-step route does NOT call this — ``recovery`` is
    typed as the enum on ``RunStepRequest``, so Pydantic validates at the boundary. This
    function serves in-process callers.
    """
    if value is None or value == "":
        return None
    return RecoveryPolicy(value)


class NotBuiltYetError(Exception):
    """Raised by the run engine (N5+) when sequencing reaches a reserved
    construct whose implementing unit has not shipped.

    DEFINED in Bolt 1 (N1) so the vocabulary exists; NEVER raised here. Bolt 1
    only validates-and-stores reserved constructs; it never executes a spec.
    """


class StepParamError(ValueError):
    """A run refused at start because step params do not fit their profiles' declarations.

    Raised before any journal row is written, by ``start_run`` and by the submit
    route, from the errors ``services.step_params.check_step_params`` returns. Both
    run routes map it to 422 with the messages joined in ``detail``. A ``ValueError``
    subclass, so a caller that maps ``ValueError`` broadly still answers 4xx; the run
    routes catch it first.
    """

    def __init__(self, errors: List[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


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

    ``call`` and ``params`` come from the workflow builder. Both are optional and
    default to ``None``, so a spec that omits them, and a run journaled before they
    existed, behaves exactly as before.
    """

    id: str
    provider: str
    agent: str
    prompt: str
    engine: Optional[KiroEngine] = None
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
    # How the engine hands the step to its agent. ``None`` means ``handoff``, the
    # only call type the engine runs. ``assign`` and ``send_message`` are reserved:
    # they validate as ``pass_reserved`` and a run that uses them is refused before
    # any journal row exists.
    call: Optional[Literal["handoff", "assign", "send_message"]] = None
    # Literal per-step parameters, added to the end of the step prompt in declaration
    # order (``workflow_service._build_step_prompt``). Journaled in plaintext with the
    # spec snapshot, like inputs, so never a secret. ``SkipValidation`` keeps the
    # field type from rejecting a non-string value before ``validate_grammar`` runs:
    # the grammar check names the step and the key, aggregated with every other error
    # (BR-7), where a field error would say only "Input should be a valid string".
    params: Optional[SkipValidation[Dict[str, str]]] = None

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

        # 4c. Per-step params: a mapping of at most WORKFLOW_MAX_STEP_PARAMS entries,
        # each key matching WORKFLOW_STEP_PARAM_KEY_RE and each value a single-line
        # string of at most WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS characters.
        for step in self.steps:
            errors.extend(_param_errors(step))

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

    ``rule_id`` is a CLOSED set, and it GATES the linter's rule catalogue:
    ``script_lint.py`` cannot emit an id that is not listed here. **Adding a
    lint rule therefore means widening this ``Literal`` in the same change as
    the rule itself** — the prose catalogue in ``script_lint.py``'s module
    docstring describes the rules, but this is what admits them.

    Getting that wrong does not fail gracefully. ``LintFinding(...)`` raises
    ``ValidationError`` for an unlisted id, and it is constructed inside
    ``_walk_tree``, which runs OUTSIDE ``lint_script``'s ``try`` — so the raise
    escapes a function contracted never to raise on input content, as a 500 on
    the validate route (``api/main.py``) and in place of the intended
    ``ScriptLintError`` at the run-path lint gate (``script_runner.py``).

    Do not reuse an existing id to avoid widening: ``workflow_spec_service``
    treats ``rule_id == "syntax"`` as "no parseable AST" and zeroes
    ``spec.inputs``, so borrowing that id silently drops INPUTS extraction.
    """

    rule_id: Literal[
        "syntax",
        "disallowed-import",
        "nondeterminism",
        "dynamic-import",
        # issue #583 — the recovery-policy rules (script_lint.py's catalogue).
        "missing-recovery-policy",
        "unverifiable-recovery-policy",
        "unenforced-recovery-policy",
        # issue #753 — literal script step ids rejected by the run-step route.
        "invalid-step-id",
    ]
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


class StaleSpecError(ValueError):
    """An update presented a source hash that no longer matches the spec on disk.

    Closes FR-8's second criterion (issue #583, Bolt 3, unit 4): "an update
    presenting a stale source hash is rejected".

    A ``ValueError`` subclass so existing broad handlers still catch it, but a
    DISTINCT type so the API boundary can map it to **409** — a resource conflict,
    not a malformed request — before any bare ``ValueError`` catch, exactly as
    :class:`TierCollisionError` above is mapped.

    The distinction is actionable rather than cosmetic: "your content is wrong"
    (400) and "someone else changed this" (409) demand different responses — fix
    the source, versus re-read and re-apply. A caller that cannot tell them apart
    will retry the same overwrite, which is the behaviour the check exists to
    prevent.

    Carries BOTH hashes so a diagnostician can see which values disagreed.
    """

    def __init__(self, name: str, expected: str, actual: str) -> None:
        super().__init__(
            f"workflow '{name}' has changed on disk since it was read "
            f"(expected source hash {expected}, found {actual}); "
            "re-read the spec and re-apply your changes"
        )
        self.name = name
        self.expected = expected
        self.actual = actual


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


class StepResultEnvelope(BaseModel):
    """The durable record of what a step produced — the only thing replay returns (#583, FR-1).

    Written UNCONDITIONALLY at settle (BR-1), which is what makes its absence on a settled
    row FR-4's guard-2 signal. Distinct from ``output_json``, which is legitimately NULL
    whenever a worker returned nothing structured (BR-9) — conflating them would destroy
    guard 2, because "settled with no envelope" would then describe almost every ordinary
    step.

    ``truncated``/``redacted`` self-report (BR-4): FR-12 asks a later agent to reconstruct
    events from persisted evidence alone, and evidence that hides its own lossiness is worse
    than none — a reader cannot otherwise tell a short message from a shortened one.

    ``redacted`` is a BOOLEAN and nothing more (SR-4). The pattern names ``redact_secrets``
    reports are deliberately NOT stored: redaction cascades, so a later pattern can match an
    earlier ``[REDACTED:<name>]`` marker, and a stored name list would look like precise
    evidence of which credential type was present while not being that.

    ``terminal_id`` names a DEAD terminal by replay time and is retained anyway (BR-6): it
    links a replayed result back to the original run's evidence, which FR-12 wants.
    ADR-583-13's ``replayed`` flag on the response is what stops a consumer treating the id
    as live. ``None`` is a legal value (a handoff caller may reuse a terminal).

    Built and read exclusively through ``services/step_result.py``; this model holds no
    logic of its own.
    """

    last_message: str
    status: str
    terminal_id: Optional[str] = None
    truncated: bool = False
    redacted: bool = False


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


def _yaml_kind(value: Any) -> str:
    """Name a parsed YAML value's type the way a spec author would write it."""
    if value is None:
        return "null"
    if isinstance(value, str):
        return "a string"
    if isinstance(value, bool):  # before int: bool is an int subclass
        return "a boolean"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, dict):
        return "a mapping"
    return type(value).__name__


def _param_errors(step: "WorkflowStep") -> List[str]:
    """Return the grammar errors in one step's ``params``, each naming the step and key.

    ``params`` is a ``SkipValidation`` field, so the raw parsed value arrives here
    unchecked. Every rule is enforced here, which keeps each violation in the one
    aggregated ``ValueError`` (BR-7) instead of a bare Pydantic field error.
    """
    params: Any = step.params
    if params is None:
        return []
    if not isinstance(params, dict):
        return [
            f"step '{step.id}': params must be a mapping of names to string values, "
            f"not {_yaml_kind(params)}"
        ]
    errors: List[str] = []
    if len(params) > WORKFLOW_MAX_STEP_PARAMS:
        errors.append(
            f"step '{step.id}' declares {len(params)} params (max {WORKFLOW_MAX_STEP_PARAMS})"
        )
    for key, value in params.items():
        if not isinstance(key, str) or not _PARAM_KEY_RE.fullmatch(key):
            errors.append(
                f"step '{step.id}': param name {key!r} is invalid "
                f"(must match {WORKFLOW_STEP_PARAM_KEY_RE})"
            )
        elif not isinstance(value, str):
            errors.append(
                f"step '{step.id}': param '{key}' must be a string, not {_yaml_kind(value)} "
                "(quote the value)"
            )
        elif len(value) > WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS:
            errors.append(
                f"step '{step.id}': param '{key}' is {len(value)} characters "
                f"(max {WORKFLOW_STEP_PARAM_VALUE_MAX_CHARS})"
            )
        elif _LINE_BREAK_RE.search(value):
            errors.append(f"step '{step.id}': param '{key}' must be one line (no line breaks)")
        elif _has_placeholder(value):
            errors.append(
                f"step '{step.id}': param '{key}' contains a {{{{...}}}} placeholder. "
                "Param values are fixed when the workflow is built."
            )
    return errors


def _has_placeholder(value: str) -> bool:
    """True when the engine would find a ``{{...}}`` placeholder in ``value``.

    Uses the engine's own pattern (``WORKFLOW_PLACEHOLDER_RE``) on ``value`` with each
    run of whitespace cut to one space. That keeps the answer: a match is ``{{``, a run of
    characters that are not braces, then ``}}``, and cutting whitespace changes neither
    the braces nor whether a run is empty. It also bounds the time. On a long whitespace
    run the pattern's ``\\s*`` and ``[^{}]+?`` can split the run in many ways, and a
    512-character value took about 24 ms to search.
    """
    return _PLACEHOLDER_RE.search(_WHITESPACE_RUN_RE.sub(" ", value)) is not None


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
    diffs are stable (RD-3.1). A step's ``call`` emits its own value as the token
    (``assign``, ``send_message``) unless it is ``None`` or ``handoff``, the call
    type the engine runs.
    """
    used: List[str] = []
    if spec.mode != "sequential":
        used.append(spec.mode)
    for step in spec.steps:
        if step.call is not None and step.call != "handoff" and step.call not in used:
            used.append(step.call)
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

    The result of :func:`validate_spec_text`, without the parsed spec.
    """
    return validate_spec_text(text)[0]


def validate_spec_text(text: str) -> tuple[ValidationResult, Optional[WorkflowSpec]]:
    """Validate a workflow spec WITHOUT running it, and return the spec it parsed.

    The spec is ``None`` unless grammar passed. ``POST /workflows/validate`` uses it to
    check each step's params against its profile, from the same parse, so the text is
    not read or parsed a second time. NEVER raises.

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
        return (
            ValidationResult(
                status="fail",
                errors=[f"spec exceeds byte cap (max {WORKFLOW_MAX_SPEC_BYTES})"],
            ),
            None,
        )

    # Parse YAML, then construct the spec (triggers validate_grammar).
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        logger.debug("validate_only: malformed YAML: %s", exc)
        return ValidationResult(status="fail", errors=[f"malformed YAML: {exc}"]), None

    if not isinstance(data, dict):
        return (
            ValidationResult(status="fail", errors=["spec root must be a mapping (YAML object)"]),
            None,
        )

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
        return ValidationResult(status="fail", errors=reasons or [message]), None

    reserved = [_reserved_note(c) for c in _constructs_used(spec) if is_reserved(c)]
    if reserved:
        return ValidationResult(status="pass_reserved", reserved_notes=reserved), spec
    return ValidationResult(status="pass"), spec


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
