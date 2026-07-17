"""Static script linter (issue #312, Bolt 2 / U1, C2).

A pure, dependency-free function of the source text: one ``ast.parse`` plus
exactly one ``ast.walk`` — no filesystem, no network, no state, and **no
import and no execution of the target** (FR-2.1, the M2 no-execution
guarantee). Path safety is the caller's job; ``display_path`` is used only
for message rendering. ``lint_script`` never raises on bad input — a syntax
error is a finding, not an exception (U1-BR-6).

Rule catalogue (U1 business-rules.md):
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

``status == "fail"`` iff at least one ERROR finding (U1-BR-1); ERRORs are
mirrored into the legacy ``errors`` list as ``"line N: [rule_id] message"``
(Q5=A); warnings are never mirrored. ``pass_reserved`` is never emitted —
a YAML reserved-construct concept with no script analogue.
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

    _walk_imports(tree, findings)
    return _build_result(findings)


def _walk_imports(tree: ast.AST, findings: List[LintFinding]) -> None:
    """One ``ast.walk`` classifying import-shaped nodes (U1-A2/A3)."""
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
