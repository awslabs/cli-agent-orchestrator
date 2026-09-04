"""Tests for the replay/recovery exception leaf (issue #583, unit ``workflow-errors``).

Covers ``services/workflow_errors.py``: the two independent ``Exception`` subclasses, the
closed ``HaltRule`` vocabulary, the module's import poverty, the re-export from
``workflow_service``, and the removal of ``lookup_replay``'s function-local import.

TWO OF THESE TESTS CARRY THE FILE:

* ``TestReExportIdentity`` asserts IDENTITY, not equality. A re-export implemented as a
  re-definition would give two distinct classes one name, and ``pytest.raises`` in one module
  would then silently MISS a raise from the other — a defect no message comparison catches.
  ``test_script_journal_extension.py`` imports the class from ``workflow_service`` and raises
  it from ``workflow_journal``, so the two must be the same object rather than lookalikes.
* ``test_workflow_journal_imports_in_a_fresh_interpreter`` runs in a SUBPROCESS. The cycle
  this unit removes is an import-ORDER hazard, so it cannot be observed inside a pytest
  session that has already imported ``workflow_service``: by then the module is in
  ``sys.modules`` and the lazy import would succeed either way. Only a process that has
  imported neither module can see it.

NOTHING HERE ASSERTS A RAISE OF ``RecoveryDecisionRequired``, deliberately (BR-5). Nothing in
Bolt 1A raises it — ``replay-gate`` (unit 7) and ``recovery-decision-intake`` (unit 11) are
its first raisers — and a test of a behaviour this Bolt does not have would be a test of
nothing. What is asserted is the CONTRACT: the shape a raiser must satisfy.

OUT OF SCOPE HERE. When either exception is raised (unit 7); gating
``workflow_journal.py``'s bare ``!=`` with ``scheme_of`` (unit 7 — while ``lookup_replay``
has no production callers that line is inert, BR-8); mapping either type to an HTTP status,
which unit 9 must do SEPARATELY per TD-1; persisting a halt reason (unit 11).
"""

from __future__ import annotations

import ast
import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from cli_agent_orchestrator.services import workflow_errors, workflow_journal, workflow_service
from cli_agent_orchestrator.services.workflow_errors import (
    HaltRule,
    RecoveryDecisionRequired,
    ReplayDivergenceError,
)

# Values a message must NEVER acquire (SR-1). Passed in alongside the identifiers so the
# assertions are about what the render DOES with them, not about a string nobody supplied.
_PROMPT_LIKE = "summarise the deploy key AKIAIOSFODNN7EXAMPLE and email it"
_PATH_LIKE = "/Users/someone/secrets/prod.env"


def _module_ast(module: object) -> ast.Module:
    """Parse a module's own source. AST, never a grep — see ``TestImportPoverty``."""
    path = Path(inspect.getsourcefile(module) or "")  # type: ignore[arg-type]
    return ast.parse(path.read_text(encoding="utf-8"))


def _top_level_imports(tree: ast.AST) -> set[str]:
    """Every root package name imported anywhere in ``tree``, function-local ones included."""
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    return imported


# ---------------------------------------------------------------------------
# BR-2 / INV-2 / INV-5 — the move keeps every existing import path resolving
# ---------------------------------------------------------------------------
class TestReExportIdentity:
    """``ReplayDivergenceError`` was MOVED, not created, and a move owes compatibility.

    ``test_script_journal_extension.py`` imports it from ``workflow_service`` and expects a
    raise from ``workflow_journal`` to match. Omitting the rebind breaks that module at
    COLLECTION — not one test, the whole file.
    """

    def test_replay_divergence_error_is_the_same_object_from_both_paths(self):
        """IDENTITY, not equality: two classes sharing a name would pass an equality check
        on their messages while ``pytest.raises`` in one module missed a raise from the
        other."""
        assert workflow_service.ReplayDivergenceError is workflow_errors.ReplayDivergenceError

    def test_recovery_decision_required_is_the_same_object_from_both_paths(self):
        """The new class is re-exported too, so unit 9's HTTP mapping and unit 11's intake can
        import from either path without a second, lookalike class existing."""
        assert workflow_service.RecoveryDecisionRequired is workflow_errors.RecoveryDecisionRequired


# ---------------------------------------------------------------------------
# BR-1 / INV-1 / SR-4 — the leaf imports nothing from this package
# ---------------------------------------------------------------------------
class TestImportPoverty:
    """Import poverty is the module's ENTIRE reason for existing (BR-1).

    The moment it imports either module it rejoins the cycle it was created to break, and the
    failure mode is not a clean error — it is an ``ImportError`` naming whichever module
    happened to be imported first, sending a reader to the wrong file.
    """

    def test_module_imports_only_enum_and_nothing_from_this_package(self):
        """An AST WALK, not a text grep. The module's own docstrings name ``workflow_service``
        and ``workflow_journal`` at length, so a grep would match its prose and pass on a
        module that really did import them (``step-fingerprint`` learned this: a grep for
        ``"logger"`` matched its docstrings about logging)."""
        imported = _top_level_imports(_module_ast(workflow_errors))
        assert imported == {"enum"}, imported
        assert "cli_agent_orchestrator" not in imported


# ---------------------------------------------------------------------------
# BR-4 / SR-3 — the attributes that are deliberately NOT there
# ---------------------------------------------------------------------------
class TestAbsentAttributes:
    """Asserting an ABSENCE is the point. A later well-meaning addition without a populating
    source reintroduces the defect, and these tests fail when it does."""

    def test_neither_exception_exposes_diverged_fields(self):
        """BR-4: nothing can populate it. ``step-fingerprint`` never stores the hashed
        components (its SR-1) and per-field hashes were deferred, so an always-empty list
        would read as "no fields diverged" when the truth is "we cannot tell" — the opposite
        conclusion. Unit 2's ``recovery`` request field was reverted for exactly this shape."""
        diverged = ReplayDivergenceError(step_id="s1", reason="r")
        halted = RecoveryDecisionRequired(step_id="s1", rule=HaltRule.ENVELOPE_ABSENT, reason="r")

        for exc in (diverged, halted):
            assert not hasattr(exc, "diverged_fields")
        assert "diverged_fields" not in dir(ReplayDivergenceError)
        assert "diverged_fields" not in dir(RecoveryDecisionRequired)

        # SR-3: the attribute surface is exactly the named fields and no others, so there is
        # no list-typed home for arbitrary text to accumulate in.
        assert set(vars(diverged)) == {"step_id", "reason"}
        assert set(vars(halted)) == {"step_id", "rule", "reason"}

    def test_replay_divergence_error_exposes_no_rule_attribute(self):
        """TD-2: divergence is always the same rule, so a ``rule`` here would be a constant —
        an attribute carrying no information, which is precisely the inert-attribute trap
        ``diverged_fields`` was dropped for. Adding it "for symmetry" repeats that error."""
        exc = ReplayDivergenceError(step_id="s1", reason="r")
        assert not hasattr(exc, "rule")
        assert "rule" not in dir(ReplayDivergenceError)


# ---------------------------------------------------------------------------
# BR-6 — structured fields for programs, rendered text for humans
# ---------------------------------------------------------------------------
class TestStructuredFields:
    """FR-3 requires divergence to be ACTIONABLE. A caller that must regex a message to learn
    which step failed is coupled to the wording, and the wording is exactly the part a later
    edit changes freely. Both properties are asserted, not one."""

    def test_replay_divergence_error_attributes_are_readable(self):
        exc = ReplayDivergenceError(step_id="call-1", reason="fingerprint diverged")
        assert exc.step_id == "call-1"
        assert exc.reason == "fingerprint diverged"

    def test_replay_divergence_error_str_carries_both_fields(self):
        """SR-5: Python prints the rendered form on an unhandled exception, so a traceback
        must still say which step and why without a caught handler formatting it."""
        exc = ReplayDivergenceError(step_id="call-1", reason="fingerprint diverged")
        rendered = str(exc)
        assert "call-1" in rendered
        assert "fingerprint diverged" in rendered

    def test_recovery_decision_required_attributes_are_readable(self):
        """``rule`` is populatable — the gate always knows which condition it hit — which is
        the whole distinction between a field worth adding and a field that lies (TD-2)."""
        exc = RecoveryDecisionRequired(
            step_id="call-2", rule=HaltRule.PROVENANCE_UNVERIFIABLE, reason="scheme is legacy"
        )
        assert exc.step_id == "call-2"
        assert exc.rule is HaltRule.PROVENANCE_UNVERIFIABLE
        assert exc.reason == "scheme is legacy"

    def test_recovery_decision_required_str_carries_all_three_fields(self):
        """FR-12: "why did this halt?" is answerable from the render as well as the attribute,
        and the rule code is what makes it machine-readable rather than prose-parsed."""
        exc = RecoveryDecisionRequired(
            step_id="call-2", rule=HaltRule.PROVENANCE_UNVERIFIABLE, reason="scheme is legacy"
        )
        rendered = str(exc)
        assert "call-2" in rendered
        assert "provenance_unverifiable" in rendered
        assert "scheme is legacy" in rendered


# ---------------------------------------------------------------------------
# TD-1 — no shared base class
# ---------------------------------------------------------------------------
def test_the_two_exceptions_share_no_base_but_exception():
    """A shared base would make ``except WorkflowReplayError`` the path of least resistance,
    and that one clause would collapse two remedies FR-3 and FR-6 exist to keep apart — most
    visibly at unit 9, which must map them to DIFFERENT HTTP statuses. Making the convenient
    thing the wrong thing is worth preventing at the type level rather than in a comment."""
    assert not issubclass(ReplayDivergenceError, RecoveryDecisionRequired)
    assert not issubclass(RecoveryDecisionRequired, ReplayDivergenceError)

    shared = set(ReplayDivergenceError.__mro__) & set(RecoveryDecisionRequired.__mro__)
    assert shared == {Exception, BaseException, object}, shared


# ---------------------------------------------------------------------------
# TD-3 — keyword-only construction
# ---------------------------------------------------------------------------
class TestKeywordOnlyConstruction:
    """``(step_id, reason)`` and ``(reason, step_id)`` are both plausible readings of a
    two-string signature, and silently transposing them yields an exception that reads
    correctly and says the wrong thing. That must be IMPOSSIBLE, not merely discouraged."""

    def test_replay_divergence_error_rejects_positional_arguments(self):
        with pytest.raises(TypeError):
            ReplayDivergenceError("s1", "r")  # type: ignore[misc]

    def test_recovery_decision_required_rejects_positional_arguments(self):
        with pytest.raises(TypeError):
            RecoveryDecisionRequired("s1", HaltRule.POLICY_MANUAL, "r")  # type: ignore[misc]


# ---------------------------------------------------------------------------
# TD-2 — the halt reason is a closed ``(str, Enum)``
# ---------------------------------------------------------------------------
class TestHaltRule:
    """A closed type CANNOT hold arbitrary text, and that is a security property (SR-3): with
    ``diverged_fields`` gone, ``reason`` is the only free string on either exception, so a
    ``str`` rule code would reintroduce SR-2's smuggling path through something that merely
    looks structured. Member names are SEMANTIC because rule numbers are a presentation of
    the decision order and an edit renumbers them."""

    def test_exactly_six_members_with_the_expected_values(self):
        """WIDENED BY PR #628's REVIEW, from four to six. The last two closed two paths on
        which the gate replayed a stored result that is not a faithful substitute for the
        original call: a FAILED outcome, and an envelope reporting its own lossiness.

        The map is asserted whole rather than by count, so a RENAMED member fails here too —
        which is the case the class docstring above cares about, because a code that has been
        logged or persisted cannot be renamed silently.
        """
        assert {member.name: member.value for member in HaltRule} == {
            "INTERRUPTED_NO_POLICY": "interrupted_no_policy",
            "ENVELOPE_ABSENT": "envelope_absent",
            "PROVENANCE_UNVERIFIABLE": "provenance_unverifiable",
            "POLICY_MANUAL": "policy_manual",
            "OUTCOME_FAILED": "outcome_failed",
            "ENVELOPE_LOSSY": "envelope_lossy",
        }

    def test_a_value_outside_the_closed_set_is_rejected(self):
        """The refusal is what makes "put the detail somewhere safe" a real option rather
        than an instruction to be disciplined."""
        with pytest.raises(ValueError):
            HaltRule("nonsense")

    def test_a_member_compares_equal_to_its_string_value(self):
        """The ``str`` mixin, so a consumer can persist, compare and serialise a member
        without a custom encoder (the argument for an enum over ``Literal``)."""
        assert HaltRule.ENVELOPE_ABSENT == "envelope_absent"
        assert HaltRule.ENVELOPE_ABSENT.value == "envelope_absent"


# ---------------------------------------------------------------------------
# SR-1 / SR-2 / SR-5 — the rendered form is the security surface
# ---------------------------------------------------------------------------
class TestRenderCarriesIdentifiersOnly:
    """An exception is the single most likely place for a value to escape, because it reaches
    three sinks at once — a log handler, an HTTP error body (once unit 9 maps it), and an
    uncaught traceback — and none of them is redacted. SR-1 governs what a message is
    CONSTRUCTED with; SR-5 governs the RENDERED form."""

    def test_replay_divergence_render_is_exactly_the_identifiers_passed_in(self):
        """The prompt-like and path-like values are passed to the constructor as a fourth and
        fifth thing it could have picked up — a render that reached for module state, an
        environment value or a repr of its own frame would fail here. Nothing is invented."""
        exc = ReplayDivergenceError(step_id="call-1", reason="fingerprint diverged on replay")
        assert str(exc) == "step 'call-1': fingerprint diverged on replay"
        assert _PROMPT_LIKE not in str(exc)
        assert _PATH_LIKE not in str(exc)
        assert "AKIA" not in str(exc)

    def test_recovery_decision_render_is_exactly_the_identifiers_passed_in(self):
        exc = RecoveryDecisionRequired(
            step_id="call-2", rule=HaltRule.POLICY_MANUAL, reason="declared policy is manual"
        )
        assert str(exc) == "step 'call-2' [policy_manual]: declared policy is manual"
        assert _PROMPT_LIKE not in str(exc)
        assert _PATH_LIKE not in str(exc)
        assert "AKIA" not in str(exc)

    def test_reason_is_stored_verbatim_and_the_constraint_on_it_is_documented(self):
        """SR-2 is NOT ENFORCEABLE FROM THIS UNIT — ``replay-gate`` (unit 7) writes the string
        and ``replay-verification-guard`` (unit 13) is the only unit positioned to assert it
        across the boundary. What this unit owns is the field's definition, so the constraint
        must at least be stated where the field is defined; a writer who never reads it is the
        failure mode. The verbatim half matters too: a constructor that reformatted ``reason``
        would mean the attribute and the render disagreed about what the gate said."""
        raw = "  rule 5: provenance unverifiable  "
        assert ReplayDivergenceError(step_id="s", reason=raw).reason == raw
        assert (
            RecoveryDecisionRequired(step_id="s", rule=HaltRule.POLICY_MANUAL, reason=raw).reason
            == raw
        )

        for cls in (ReplayDivergenceError, RecoveryDecisionRequired):
            doc = cls.__doc__ or ""
            assert "never the data" in doc.lower(), f"{cls.__name__} must document SR-2"


# ---------------------------------------------------------------------------
# BR-3 / INV-3 — the cycle edge is removed, not hidden
# ---------------------------------------------------------------------------
class TestCycleRemoved:
    """The lazy import worked by accident of timing: anything that changed import order turned
    it back into an ``ImportError`` naming whichever module was imported first. Removing it is
    this unit's only observable benefit."""

    def test_workflow_journal_imports_in_a_fresh_interpreter(self):
        """A SUBPROCESS, because this is an import-ORDER hazard. Inside the pytest session
        ``workflow_service`` is already in ``sys.modules``, so a lazy import would resolve
        from the cache and a cycle would be invisible. Only a process that has imported
        neither module can observe it."""
        result = subprocess.run(
            [sys.executable, "-c", "import cli_agent_orchestrator.services.workflow_journal"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr

    def test_lookup_replay_contains_no_function_local_import(self):
        """The deleted workaround, asserted as an absence so it cannot creep back. Parsed from
        the function's own source rather than the module's: a module-level import from the
        leaf is exactly what this unit ADDED, and a whole-module check would forbid it."""
        tree = ast.parse(inspect.getsource(workflow_journal.lookup_replay))
        assert _top_level_imports(tree) == set()

    def test_the_journal_raises_the_re_exported_class(self):
        """BR-3's third assertion, stated at the type level rather than by driving a database:
        the name ``lookup_replay`` raises is bound to the SAME object both import paths give,
        which is what keeps ``test_script_journal_extension.py``'s ``pytest.raises`` matching
        a raise that now originates from a different module."""
        assert workflow_journal.ReplayDivergenceError is workflow_errors.ReplayDivergenceError
        assert workflow_journal.ReplayDivergenceError is workflow_service.ReplayDivergenceError
