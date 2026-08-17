"""Static script linter (issue #312, Bolt 2 / U1, C2; extended by issue #583).

Issue #312 established the module and its first four rules; issue #583's
``recovery-decision-intake`` added the three recovery-policy rules catalogued
below. Rule ids and ``BR-n`` citations therefore come from TWO rule sets, so
each is named with its source rather than left bare.

A pure, dependency-free function of the source text: one ``ast.parse`` plus
exactly one ``ast.walk`` — no filesystem, no network, no state, and **no
import and no execution of the target** (FR-2.1, the M2 no-execution
guarantee). Path safety is the caller's job; ``display_path`` is used only
for message rendering. ``lint_script`` never raises on bad input — a syntax
error is a finding, not an exception (U1-BR-6).

Rule catalogue — seven rules (U1 business-rules.md; the three
recovery-policy rules are issue #583's business-rules.md):
- ``syntax`` (ERROR) — ``ast.parse`` failed; anchored at ``e.lineno`` or 1.
- ``disallowed-import`` (ERROR) — static or literal-string dynamic import
  whose first dotted segment is in ``SCRIPT_LINT_DISALLOWED_IMPORT_PREFIXES``
  (scripts reach CAO over HTTP only; the ``cao_workflow`` shim is the
  sanctioned surface and is not in the set).
- ``dynamic-import`` (WARNING) — ``importlib.import_module``/``__import__``
  with a non-literal target: static analysis cannot verify it (Q1=A).
  Best-effort boundary: a keyword-form literal call
  (``import_module(name="...")``) downgrades to this WARNING rather than the
  literal ERROR path, and aliased importlib (``import importlib as il``) is
  not tracked at all.
- ``nondeterminism`` (WARNING) — import of a module in
  ``SCRIPT_LINT_NONDETERMINISM_MODULES``; resume re-executes the frozen script,
  so deterministic control flow keeps repeated work predictable (FR-1.7 — a
  warning never blocks).
- ``missing-recovery-policy`` (ERROR) — a ``step()`` call declaring no
  ``recovery=`` keyword and no ``**`` unpacking (#583 FR-5; ADR-583-7's
  enforcement half). Checks the keyword's PRESENCE only, never its value: the
  closed set of policy values belongs to the ``cao_workflow`` shim and to the
  route's Pydantic enum, and a third copy here would be a third thing to drift.
- ``unverifiable-recovery-policy`` (WARNING) — a ``step()`` call carrying ``**``
  and no explicit ``recovery=``. The policy may well be inside that dict:
  ``step(p, a, x, **opts)`` parses to ``keywords=[keyword(arg=None)]``, which is
  indistinguishable from an omission unless ``arg is None`` is tested
  separately. Mirrors ``dynamic-import``'s answer to the same predicament, and
  the resume-time gate fails closed for whatever it cannot verify.
- ``unenforced-recovery-policy`` (WARNING) — a ``run_step()`` call carrying a
  ``recovery=`` keyword. ``run_step`` accepts it, the server stores it and the
  replay gate honours it, AND the route's ``Optional[RecoveryPolicy]`` field
  rejects an unknown value with 422. What is missing is only ``step()``'s
  CLIENT-SIDE preflight, which refuses a value outside the closed set before any
  HTTP attempt — so on ``run_step`` a typo costs a round-trip and fails that step
  mid-run. (This entry, and the finding's message, previously said the value was
  "never validated"; that was false, corrected by PR #628's review.) ``step()``
  is the surface that checks early, and this rule is the only layer that can see
  the difference statically.

``status == "fail"`` iff at least one ERROR finding (U1-BR-1); ERRORs are
mirrored into the legacy ``errors`` list as ``"line N: [rule_id] message"``
(Q5=A); warnings are never mirrored. ``pass_reserved`` is never emitted —
a YAML reserved-construct concept with no script analogue.

Adding a rule means widening ``LintFinding.rule_id`` in the SAME change. That
``Literal`` in ``models/workflow.py`` is a closed set and is what admits an id
at all: this catalogue is prose, that ``Literal`` is the enforcement. An
unlisted id raises ``ValidationError`` from inside ``_walk_tree``, which runs
OUTSIDE ``lint_script``'s ``try``, so it escapes as a 500 on the validate route
and displaces ``ScriptLintError`` at the run-path lint gate.

The recovery rules have two blind spots, and they point in OPPOSITE directions:
- ``f = step; f(...)`` — an alias MISSES a genuine omission. Accepted (§12);
  provenance is deliberately not tracked, and the resume-time gate fails closed.
- a user's own ``def step(...)``, called bare, is OVER-BLOCKED. This is the
  module's FIRST rule that can reject valid work: every other rule here errs
  toward admitting something invalid, so a reader cannot infer this direction
  from the module's character. Stated here rather than left for whoever's
  correct script stops running to discover.
"""

from __future__ import annotations

import ast
import logging
from typing import List, Literal

from cli_agent_orchestrator.constants import (
    SCRIPT_LINT_DISALLOWED_IMPORT_PREFIXES,
    SCRIPT_LINT_NONDETERMINISM_MODULES,
)
from cli_agent_orchestrator.models.workflow import LintFinding, ScriptValidationResult

logger = logging.getLogger(__name__)


def lint_script(source: str, display_path: str) -> ScriptValidationResult:
    """Lint a workflow script's source text. Total: never raises on input content.

    The only operation that can raise on input content is ``ast.parse``; it is
    wrapped once with exactly three narrow arms (no broad except — totality by
    construction, proven by the hypothesis property test).
    """
    findings: List[LintFinding] = []
    try:
        tree = ast.parse(source, filename=display_path)
    except SyntaxError as e:
        # Unparsable tree cannot be walked — the syntax finding is the sole
        # finding. e.lineno may be None on pathological inputs; anchor line 1.
        logger.warning("syntax error parsing %s: %s", display_path, e.msg)
        findings.append(
            LintFinding(
                rule_id="syntax",
                severity="error",
                line=e.lineno or 1,
                message=e.msg or "invalid syntax",
            )
        )
        return _build_result(findings)
    except ValueError:
        # CPython <=3.11: null bytes raise ValueError ("source code string
        # cannot contain null bytes"); 3.12+ reclassified this as SyntaxError
        # (gh-96670), delivered via the arm above. CI floor is 3.10, so this
        # arm is load-bearing there; tests assert totality, never which arm.
        logger.warning("unparsable source (ValueError) for %s", display_path)
        findings.append(
            LintFinding(
                rule_id="syntax",
                severity="error",
                line=1,
                message="source contains bytes the parser cannot process",
            )
        )
        return _build_result(findings)
    except RecursionError:
        logger.warning("recursion limit hit parsing %s", display_path)
        findings.append(
            LintFinding(
                rule_id="syntax",
                severity="error",
                line=1,
                message="source is too deeply nested to parse",
            )
        )
        return _build_result(findings)

    _walk_tree(tree, findings)
    return _build_result(findings)


def _walk_tree(tree: ast.AST, findings: List[LintFinding]) -> None:
    """The module's single ``ast.walk`` (U1-A2/A3, #583 BR-1).

    Classifies import-shaped nodes AND the shim's ``step``/``run_step`` calls.
    One walk is a contract, not an implementation detail: the module docstring
    states it and two ``hypothesis`` properties rest on it, so a new rule
    becomes another arm here rather than a second traversal.

    Every ``LintFinding`` in this function is constructed OUTSIDE
    ``lint_script``'s ``try``, so anything that raises here escapes a function
    contracted never to raise on input content. That is why the predicates guard
    their attribute access and why every ``rule_id`` below must appear in
    ``LintFinding``'s ``Literal``.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_module(alias.name, node.lineno, findings)
        elif isinstance(node, ast.ImportFrom):
            # level>0 (relative import) cannot name an absolute CAO path.
            if node.level == 0 and node.module:
                _check_module(node.module, node.lineno, findings)
        elif isinstance(node, ast.Call) and _is_dynamic_import_call(node.func):
            if (
                node.args
                and isinstance(node.args[0], ast.Constant)
                and isinstance(node.args[0].value, str)
            ):
                # Literal target — fully static, judged like a static import.
                _check_module(node.args[0].value, node.lineno, findings)
            else:
                findings.append(
                    LintFinding(
                        rule_id="dynamic-import",
                        severity="warning",
                        line=node.lineno,
                        message=(
                            "dynamic import with a non-literal target cannot be "
                            "verified against the CAO-internal import prohibition"
                        ),
                    )
                )
        elif isinstance(node, ast.Call) and _is_step_call(node.func):
            # Three-way, and the ORDER is load-bearing (#583 BR-4/BR-5/BR-6).
            # ``step(p, a, x, **opts)`` parses to ``keywords=[keyword(arg=None)]``,
            # so ``any(kw.arg == "recovery")`` is False for BOTH a genuine
            # omission and a policy hidden inside a dict. Testing ``arg is None``
            # separately is what keeps those two apart; collapse them and every
            # dynamic-kwargs author is blocked by an ERROR. An explicit keyword
            # is checked FIRST, so a call carrying both it and ``**`` is clean.
            if not any(kw.arg == "recovery" for kw in node.keywords):
                if any(kw.arg is None for kw in node.keywords):
                    findings.append(
                        LintFinding(
                            rule_id="unverifiable-recovery-policy",
                            severity="warning",
                            line=node.lineno,
                            message=(
                                "step() passes ** keyword unpacking, so a recovery= "
                                "inside it cannot be verified statically; declare "
                                "recovery= explicitly to have it checked here"
                            ),
                        )
                    )
                else:
                    findings.append(
                        LintFinding(
                            rule_id="missing-recovery-policy",
                            severity="error",
                            line=node.lineno,
                            message=(
                                "step() declares no recovery policy — pass an explicit "
                                "recovery= keyword (see the authoring guide)"
                            ),
                        )
                    )
        elif isinstance(node, ast.Call) and _is_run_step_call(node.func):
            # BR-7: run_step() ACCEPTS recovery=, the server stores it and the
            # replay gate honours it. Warn, never block — and note this arm is
            # reached only when the step arm above did not match, which
            # exact-equality matching guarantees.
            #
            # THE MESSAGE SAID "never validated" AND THAT WAS FALSE (PR #628
            # review, Copilot F3). The keyword lands in run_step's **opts, is
            # posted as an ordinary body field, and RunStepRequest.recovery is
            # typed Optional[RecoveryPolicy] — so an unknown value is REJECTED
            # WITH 422 at the route boundary. What run_step actually lacks is
            # step()'s client-side preflight against the closed set, which
            # raises ShimError before any HTTP attempt. The difference is WHEN
            # a typo is caught, not WHETHER: on step() it costs nothing, on
            # run_step it costs the round-trip and fails that step mid-run.
            # The message now says exactly that, because a lint finding that
            # overstates a gap teaches an author to distrust the linter.
            if any(kw.arg == "recovery" for kw in node.keywords):
                findings.append(
                    LintFinding(
                        rule_id="unenforced-recovery-policy",
                        severity="warning",
                        line=node.lineno,
                        message=(
                            "recovery= on run_step() reaches the server and the replay gate "
                            "honours it, and the route rejects an unknown value with 422 — but "
                            "run_step runs no client-side check, so a typo fails this step "
                            "mid-run instead of before the first HTTP call; declare the policy "
                            "on step() instead"
                        ),
                    )
                )


def _is_dynamic_import_call(func: ast.expr) -> bool:
    """Match ``importlib.import_module(...)``, bare ``import_module(...)``
    (from-import shape), and ``__import__(...)`` on a best-effort static basis."""
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "import_module"
            and isinstance(func.value, ast.Name)
            and func.value.id == "importlib"
        )
    if isinstance(func, ast.Name):
        return func.id in ("__import__", "import_module")
    return False


def _is_step_call(func: ast.expr) -> bool:
    """Match the shim's ``step(...)`` — bare, or qualified as ``cao_workflow.step(...)``.

    EXACT equality on the name, never a suffix or substring test:
    ``"run_step".endswith("step")`` is ``True``, so a suffix match would fire the
    BLOCKING ``missing-recovery-policy`` on every existing ``run_step`` script
    (#583 BR-2).

    The attribute form is qualified BY RECEIVER so that ``optimizer.step()``,
    ``scheduler.step()`` and ``self.step()`` — the identical
    ``ast.Attribute(attr="step")`` shape — cannot be blocked (BR-3). Because this
    rule is an ERROR, a false positive stops the author's own correct script; the
    false-positive class has to be empty, not merely small.

    The ``isinstance`` guard is LOAD-BEARING, not stylistic, and copies
    ``_is_dynamic_import_call`` above for exactly that reason: ``func.value`` is
    an ``Attribute`` for ``a.b.step(1)``, a ``Call`` for ``f().step(1)`` and a
    ``Subscript`` for ``d["k"].step(1)``, none of which has ``.id``. Reading it
    unguarded raises ``AttributeError`` on valid Python, from inside the walk,
    which sits outside ``lint_script``'s ``try``.
    """
    if isinstance(func, ast.Attribute):
        return (
            func.attr == "step"
            and isinstance(func.value, ast.Name)
            and func.value.id == "cao_workflow"
        )
    if isinstance(func, ast.Name):
        return func.id == "step"
    return False


def _is_run_step_call(func: ast.expr) -> bool:
    """Match ``run_step(...)`` and ``<receiver>.run_step(...)`` — for BR-7's WARNING only.

    Deliberately NOT qualified by receiver, unlike ``_is_step_call``: BR-7 is a
    warning that can never change ``status``, so a broad match is safe here where
    it would be unacceptable there. Exact equality still matters in the other
    direction — ``step`` must not match this predicate, or a bare ``step()`` call
    would be classified as the wrong rule.

    Touches ``attr``/``id`` only, never ``func.value.id``, so it carries no
    SR-1 hazard.
    """
    if isinstance(func, ast.Attribute):
        return func.attr == "run_step"
    if isinstance(func, ast.Name):
        return func.id == "run_step"
    return False


def _check_module(dotted: str, lineno: int, findings: List[LintFinding]) -> None:
    """Classify one dotted module path against the two constants.py frozensets."""
    first = dotted.split(".")[0]
    if first in SCRIPT_LINT_DISALLOWED_IMPORT_PREFIXES:
        findings.append(
            LintFinding(
                rule_id="disallowed-import",
                severity="error",
                line=lineno,
                message=(
                    f"import of CAO internal module '{dotted}' is not allowed — "
                    "scripts reach CAO over HTTP only (see the authoring guide)"
                ),
            )
        )
    elif first in SCRIPT_LINT_NONDETERMINISM_MODULES:
        findings.append(
            LintFinding(
                rule_id="nondeterminism",
                severity="warning",
                line=lineno,
                message=(
                    f"importing '{first}' may make resumed behavior unpredictable; "
                    "resume re-executes the frozen script, so repeated calls may "
                    "differ (see the determinism obligation in the authoring guide)"
                ),
            )
        )


def _build_result(findings: List[LintFinding]) -> ScriptValidationResult:
    """Derive status/errors from findings (U1-A4). Only ever emits pass/fail —
    ``pass_reserved`` never appears for the script tier, by construction."""
    status: Literal["pass", "fail"] = (
        "fail" if any(f.severity == "error" for f in findings) else "pass"
    )
    errors = [
        f"line {f.line}: [{f.rule_id}] {f.message}" for f in findings if f.severity == "error"
    ]
    return ScriptValidationResult(status=status, errors=errors, findings=findings)
