"""Tests for the static script linter (issue #312, Bolt 2 / U1, C2).

Deterministic fixtures are the primary suite (verdict correctness); the
hypothesis property test is the safety net proving totality (BR-6
never-raises) over the input space fixtures can't enumerate. Tests assert on
rule_id/line/severity, never on parser message prose (it varies by CPython
minor), and never on WHICH catch arm fired (the null-byte case moved from
ValueError to SyntaxError in 3.12, gh-96670 — totality is the invariant).

The recovery-policy rules (#583) add a class the property tests CANNOT reach.
Both properties draw from ``st.text()``, which will not generate ``a.b.step(1)``
at any realistic probability, so the receiver-shape crash guard is covered by
explicit fixtures in ``TestStepReceiverShapeTotality`` instead. A safety net
aimed at a different failure mode is not a safety net for this one.
"""

import time

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cli_agent_orchestrator.models.workflow import LintFinding, ScriptValidationResult
from cli_agent_orchestrator.services import script_lint as script_lint_module
from cli_agent_orchestrator.services.script_lint import lint_script


def _findings_by_rule(result: ScriptValidationResult, rule_id: str) -> list:
    return [f for f in result.findings if f.rule_id == rule_id]


class TestHappyPath:
    def test_clean_script_passes(self):
        source = "import json\nimport cao_workflow\n\nprint(json.dumps({}))\n"
        result = lint_script(source, "clean.py")
        assert result.status == "pass"
        assert result.findings == []
        assert result.errors == []
        assert result.tier == "script"

    def test_cao_workflow_shim_is_allowed(self):
        # BR-3: the shim is the sanctioned import surface, never flagged.
        result = lint_script("from cao_workflow import run_step\n", "shim.py")
        assert result.status == "pass"
        assert result.findings == []

    def test_relative_import_is_skipped(self):
        # level>0 cannot name an absolute CAO path.
        result = lint_script("from . import helpers\n", "rel.py")
        assert result.status == "pass"
        assert result.findings == []


class TestSyntaxRule:
    def test_syntax_error_fails_with_line_anchor(self):
        result = lint_script("def broken(:\n    pass\n", "broken.py")
        assert result.status == "fail"
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.rule_id == "syntax"
        assert f.severity == "error"
        assert f.line >= 1

    def test_syntax_error_short_circuits_walk(self):
        # The disallowed import after the syntax error is never reported —
        # an unparsable tree cannot be walked.
        source = "def broken(:\nimport cli_agent_orchestrator\n"
        result = lint_script(source, "broken.py")
        assert [f.rule_id for f in result.findings] == ["syntax"]

    def test_null_byte_input_is_total(self):
        # Version skew: ValueError on <=3.11, SyntaxError on 3.12+ — assert
        # totality and the fail-closed verdict, NOT which arm fired.
        result = lint_script("x = 1\x00", "null.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status == "fail"
        assert result.findings[0].rule_id == "syntax"
        assert result.findings[0].severity == "error"
        assert result.findings[0].line == 1

    def test_deeply_nested_expression_is_total(self):
        # Deep attribute chains MAY overflow the parser's recursion (3.10/3.11)
        # or MAY parse cleanly (3.12+ raised its limits, and CPython's C parser
        # is not gated by sys.setrecursionlimit). Per the never-assert-which-arm
        # rule (gh-96670), assert only totality: a result is returned, the status
        # is in-domain, and any failure is fail-closed as a syntax ERROR.
        source = "a" + ".b" * 200_000 + "\n"
        result = lint_script(source, "deep.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status in ("pass", "fail")
        if result.status == "fail":
            f = result.findings[0]
            assert f.rule_id == "syntax"
            assert f.severity == "error"

    def test_recursion_error_arm_is_fail_closed_syntax(self, monkeypatch):
        # Deterministic coverage of the RecursionError arm on ALL supported
        # versions: sys.setrecursionlimit does not gate CPython's C parser, so
        # instead force ast.parse to raise RecursionError and assert the arm
        # converts it to a fail-closed syntax ERROR anchored at line 1.
        def _raise(*args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(script_lint_module.ast, "parse", _raise)
        result = lint_script("a.b.c\n", "deep.py")
        assert result.status == "fail"
        f = result.findings[0]
        assert f.rule_id == "syntax"
        assert f.severity == "error"
        assert f.line == 1


class TestDisallowedImportRule:
    def test_static_import_is_error(self):
        result = lint_script("import cli_agent_orchestrator\n", "bad.py")
        assert result.status == "fail"
        f = result.findings[0]
        assert f.rule_id == "disallowed-import"
        assert f.severity == "error"
        assert f.line == 1
        assert "cli_agent_orchestrator" in f.message

    def test_submodule_from_import_is_error(self):
        # Q2=A: prefix match on the first dotted segment catches submodules.
        source = "from cli_agent_orchestrator.services import agent_step\n"
        result = lint_script(source, "bad.py")
        assert result.status == "fail"
        f = result.findings[0]
        assert f.rule_id == "disallowed-import"
        assert "cli_agent_orchestrator.services" in f.message

    def test_literal_importlib_is_error(self):
        source = "import importlib\nimportlib.import_module('cli_agent_orchestrator.clients')\n"
        result = lint_script(source, "dyn.py")
        assert result.status == "fail"
        errors = _findings_by_rule(result, "disallowed-import")
        assert len(errors) == 1
        assert errors[0].line == 2

    def test_literal_dunder_import_is_error(self):
        result = lint_script("__import__('cli_agent_orchestrator')\n", "dyn.py")
        assert result.status == "fail"
        assert _findings_by_rule(result, "disallowed-import")

    def test_literal_from_imported_import_module_is_error(self):
        source = "from importlib import import_module\nimport_module('cli_agent_orchestrator')\n"
        result = lint_script(source, "dyn.py")
        assert result.status == "fail"
        errors = _findings_by_rule(result, "disallowed-import")
        assert errors and errors[0].line == 2


class TestNondeterminismRule:
    @pytest.mark.parametrize("module", ["random", "secrets", "uuid", "time", "datetime"])
    def test_nondeterminism_import_warns_but_passes(self, module):
        result = lint_script(f"import {module}\n", "warn.py")
        assert result.status == "pass"  # FR-1.7: warnings never fail
        f = result.findings[0]
        assert f.rule_id == "nondeterminism"
        assert f.severity == "warning"
        assert f.line == 1
        assert module in f.message
        assert result.errors == []  # warnings never mirrored (BR-2)

    def test_literal_dynamic_import_of_nondeterminism_module_warns(self):
        source = "import importlib\nimportlib.import_module('random')\n"
        result = lint_script(source, "warn.py")
        assert result.status == "pass"
        warns = _findings_by_rule(result, "nondeterminism")
        assert warns and warns[0].line == 2


class TestDynamicImportRule:
    def test_non_literal_target_warns(self):
        source = "import importlib\nname = 'os'\nimportlib.import_module(name)\n"
        result = lint_script(source, "dyn.py")
        assert result.status == "pass"  # a warning, not an error (Q1=A)
        f = _findings_by_rule(result, "dynamic-import")[0]
        assert f.severity == "warning"
        assert f.line == 3

    def test_non_literal_dunder_import_warns(self):
        result = lint_script("mod = 'os'\n__import__(mod)\n", "dyn.py")
        warns = _findings_by_rule(result, "dynamic-import")
        assert warns and warns[0].line == 2

    def test_keyword_form_literal_downgrades_to_dynamic_import_warning(self):
        # Documented best-effort boundary: only positional-literal targets are
        # judged like static imports; import_module(name="...") has no
        # positional args, so it falls through to the dynamic-import WARNING —
        # a deliberate downgrade, not an ERROR, even for a disallowed prefix.
        source = (
            "import importlib\n" "importlib.import_module(name='cli_agent_orchestrator.clients')\n"
        )
        result = lint_script(source, "dyn.py")
        assert result.status == "pass"
        assert not _findings_by_rule(result, "disallowed-import")
        warns = _findings_by_rule(result, "dynamic-import")
        assert warns and warns[0].line == 2


class TestResultAssembly:
    def test_errors_field_mirrors_only_errors(self):
        # Q5=A: "line N: [rule_id] message" per ERROR; warnings only in findings.
        source = "import random\nimport cli_agent_orchestrator\n"
        result = lint_script(source, "mixed.py")
        assert result.status == "fail"
        assert len(result.errors) == 1
        assert result.errors[0].startswith("line 2: [disallowed-import]")
        assert len(result.findings) == 2

    def test_findings_ordered_by_walk(self):
        source = "import cli_agent_orchestrator\nimport random\n"
        result = lint_script(source, "order.py")
        assert [f.line for f in result.findings] == [1, 2]

    def test_never_pass_reserved_and_reserved_notes_empty(self):
        for source in ("", "import random\n", "import cli_agent_orchestrator\n", "bad(:\n"):
            result = lint_script(source, "any.py")
            assert result.status in ("pass", "fail")
            assert result.reserved_notes == []

    def test_finding_fields_are_complete(self):
        result = lint_script("import cli_agent_orchestrator\n", "f.py")
        f = result.findings[0]
        assert isinstance(f, LintFinding)
        assert f.rule_id and f.severity and f.message
        assert isinstance(f.line, int) and f.line >= 1


class TestTotalityProperty:
    @given(st.text())
    def test_lint_script_never_raises(self, source):
        # BR-6 proven over arbitrary unicode incl. null bytes / surrogates /
        # control chars. Asserts type + status domain only, never which arm.
        result = lint_script(source, "prop.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status in ("pass", "fail")

    @given(st.text(alphabet="([{)]}\n "))
    def test_nesting_heavy_input_never_raises(self, source):
        # Biased toward the RecursionError arm without a hand-written fixture.
        result = lint_script(source, "nest.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status in ("pass", "fail")


SHIM_IMPORT = "from cao_workflow import run_step, step\n\n"


class TestMissingRecoveryPolicyRule:
    """BR-5 — the blocking ERROR. FR-5's rejection-at-validation."""

    def test_bare_step_without_recovery_is_blocking_error(self):
        source = SHIM_IMPORT + "step('provider', 'agent', 'prompt')\n"
        result = lint_script(source, "s.py")
        assert result.status == "fail"
        findings = _findings_by_rule(result, "missing-recovery-policy")
        assert len(findings) == 1
        f = findings[0]
        assert f.rule_id == "missing-recovery-policy"
        assert f.severity == "error"
        assert f.line == 3
        # Q5=A mirror: the ERROR reaches the legacy ``errors`` list verbatim.
        assert result.errors == [f"line 3: [missing-recovery-policy] {f.message}"]

    def test_qualified_cao_workflow_step_without_recovery_is_error(self):
        source = "import cao_workflow\n\ncao_workflow.step('p', 'a', 'x')\n"
        result = lint_script(source, "s.py")
        assert result.status == "fail"
        f = _findings_by_rule(result, "missing-recovery-policy")[0]
        assert f.severity == "error"
        assert f.line == 3

    def test_line_anchors_the_call_not_the_enclosing_function(self):
        # BR-5 names this explicitly: node.lineno of the CALL, so an author
        # editing a long function is pointed at the offending line.
        source = SHIM_IMPORT + "def outer():\n    x = 1\n    step('p', 'a', x)\n"
        result = lint_script(source, "s.py")
        f = _findings_by_rule(result, "missing-recovery-policy")[0]
        assert f.line == 5  # the call, not `def outer` on line 3

    def test_step_with_explicit_recovery_yields_no_finding(self):
        # BR-4, the success path.
        source = SHIM_IMPORT + "step('p', 'a', 'x', recovery='idempotent')\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert result.findings == []
        assert result.errors == []

    def test_step_with_non_literal_recovery_value_yields_no_finding(self):
        # BR-4: the rule checks the keyword's PRESENCE, never its value, so a
        # variable must satisfy it. Asserting only the literal case would leave
        # presence-vs-value untested.
        source = SHIM_IMPORT + "policy_var = choose()\nstep('p', 'a', 'x', recovery=policy_var)\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert result.findings == []


class TestUnverifiableRecoveryPolicyRule:
    """BR-6 — ``**`` unpacking is a WARNING under its own id, never the ERROR."""

    def test_kwargs_unpacking_is_warning_and_status_stays_pass(self):
        source = SHIM_IMPORT + "step('p', 'a', 'x', **opts)\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"  # INV-4: a warning never blocks
        assert len(result.findings) == 1
        f = result.findings[0]
        assert f.rule_id == "unverifiable-recovery-policy"
        assert f.severity == "warning"
        assert f.line == 3
        assert result.errors == []  # warnings are never mirrored
        # TD-4: the ERROR must NOT fire here. `keywords=[keyword(arg=None)]` is
        # indistinguishable from an omission unless `arg is None` is tested
        # separately, and collapsing them blocks every dynamic-kwargs author.
        assert _findings_by_rule(result, "missing-recovery-policy") == []

    def test_explicit_recovery_beside_unpacking_yields_no_finding(self):
        # BR-6/TD-4: step 1 wins — an explicit keyword is present and verifiable,
        # so the `**` is irrelevant.
        source = SHIM_IMPORT + "step('p', 'a', 'x', recovery='manual', **opts)\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert result.findings == []


class TestRunStepIsNeverBlocked:
    """BR-2/INV-2/SR-3 — the assertion that catches a suffix match.

    ``"run_step".endswith("step")`` is True, so a suffix or substring match
    would fire the BLOCKING ``missing-recovery-policy`` on every existing
    ``run_step`` script. The positive cases elsewhere would all still pass; only
    this negative one fails, which is why it is asserted first-class.
    """

    def test_run_step_without_recovery_yields_no_finding_at_all(self):
        source = (
            SHIM_IMPORT + "run_step('p', 'a', 'one')\n"
            "run_step('p', 'a', 'two')\n"
            "obj.run_step('p', 'a', 'three')\n"
        )
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert _findings_by_rule(result, "missing-recovery-policy") == []
        assert result.findings == []
        assert result.errors == []

    def test_run_step_with_only_kwargs_unpacking_yields_no_finding(self):
        # The BR-5a hole in its dict form is statically INVISIBLE:
        # run_step(**{"recovery": ...}) carries keyword(arg=None) and no
        # recovery= to see. Recorded as an accepted miss, not fixed — BR-7
        # catches the explicit form, which is the one authors write.
        source = SHIM_IMPORT + "run_step('p', 'a', **opts)\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert result.findings == []


class TestUnenforcedRecoveryPolicyRule:
    """BR-7 — closes ``shim-step-surface`` BR-5a at the only layer that sees it."""

    def test_run_step_with_recovery_warns_and_is_not_the_error(self):
        source = SHIM_IMPORT + "run_step('p', 'a', 'x', recovery='manual')\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        findings = _findings_by_rule(result, "unenforced-recovery-policy")
        assert len(findings) == 1
        assert findings[0].severity == "warning"
        assert findings[0].line == 3
        assert len(result.findings) == 1
        assert result.errors == []
        # The half that matters: proves BR-2's exact-equality match has not
        # leaked into the step() arm.
        assert _findings_by_rule(result, "missing-recovery-policy") == []

    def test_the_message_does_not_claim_the_value_is_never_validated(self):
        """PR #628 review (Copilot F3) — THE OLD MESSAGE WAS FACTUALLY WRONG.

        It read "recovery= on run_step() is accepted but never validated, so it is not
        enforcement". The keyword lands in ``run_step``'s ``**opts``, is posted as an ordinary
        body field, and ``RunStepRequest.recovery`` is typed ``Optional[RecoveryPolicy]`` — so
        an unknown value is REJECTED WITH 422 at the route. ``test/api/test_run_step_replay_
        branch.py::TestRecoveryField::test_unknown_recovery_value_is_rejected`` is the proof,
        and ``test_the_message_names_the_real_gap_and_the_route_agrees`` in that file pins this
        message against that behaviour so the two cannot drift apart again.

        Asserted as a NEGATIVE on the false phrasings as well as a positive on the true one: a
        lint message that overstates a gap teaches an author to distrust every other finding,
        and the overstatement is what took four documents with it.
        """
        source = SHIM_IMPORT + "run_step('p', 'a', 'x', recovery='manual')\n"
        message = _findings_by_rule(lint_script(source, "s.py"), "unenforced-recovery-policy")[
            0
        ].message

        assert "never validated" not in message
        assert "validated by nothing" not in message
        assert "not enforcement" not in message
        # The two true facts: the server DOES reject a bad value, and the real gap is that
        # ``run_step`` does not check before sending.
        assert "422" in message
        assert "client-side" in message
        # Still points at the surface that checks early.
        assert "step()" in message

    def test_attribute_form_run_step_with_recovery_also_warns(self):
        # BR-7 is deliberately NOT receiver-qualified: a warning cannot block,
        # so the broad match is safe here where it would be unacceptable for the
        # ERROR.
        source = "runner.run_step('p', 'a', recovery='manual')\n"
        result = lint_script(source, "s.py")
        assert result.status == "pass"
        assert _findings_by_rule(result, "unenforced-recovery-policy")[0].line == 1


class TestStepReceiverFalsePositives:
    """BR-3/INV-3/SR-3 — the false-positive class must be EMPTY, not small.

    ``optimizer.step()`` is the identical ``ast.Attribute(attr="step")`` shape as
    ``cao_workflow.step()``, and this rule is an ERROR, so an unqualified match
    would not warn about these scripts — it would stop them running.
    """

    @pytest.mark.parametrize("receiver", ["optimizer", "scheduler", "machine", "self"])
    def test_unrelated_receiver_step_call_yields_nothing(self, receiver):
        source = f"{receiver}.step()\n{receiver}.step(1, 2)\n"
        result = lint_script(source, "fp.py")
        assert result.status == "pass"
        assert result.findings == []
        assert result.errors == []

    def test_aliased_step_is_an_accepted_miss(self):
        # §12 accepts this direction: provenance is not tracked, so `f = step`
        # hides the call. Asserted so the accepted blind spot is visible in the
        # suite rather than only in the docstring.
        source = SHIM_IMPORT + "f = step\nf('p', 'a', 'x')\n"
        result = lint_script(source, "alias.py")
        assert result.status == "pass"
        assert result.findings == []

    def test_user_defined_step_is_over_blocked_and_that_is_accepted(self):
        # The module's FIRST rule that can reject VALID work (BR-9/SR-3, Q3=A).
        # Asserted in the direction it actually behaves, so the accepted
        # trade-off is discoverable from the suite and not folklore.
        source = "def step(n):\n    return n\n\nstep(1)\n"
        result = lint_script(source, "own.py")
        assert result.status == "fail"
        assert _findings_by_rule(result, "missing-recovery-policy")[0].line == 4


class TestStepReceiverShapeTotality:
    """SR-1 — the crash guard. ``func.value.id`` raises on VALID Python.

    ``func.value`` is an Attribute for ``a.b.step(1)``, a Call for
    ``f().step(1)`` and a Subscript for ``d["k"].step(1)``; none has ``.id``.
    The walk runs OUTSIDE ``lint_script``'s ``try``, so an unguarded read escapes
    a function contracted never to raise and 500s the validate route.

    Neither ``hypothesis`` property reaches this — both draw from ``st.text()``.
    These fixtures are the actual control.
    """

    @pytest.mark.parametrize(
        "source",
        [
            "a.b.step(1)\n",
            "f().step(1)\n",
            "d['k'].step(1)\n",
            "a.b.c.d.step(1)\n",
            "(x or y).step(1)\n",
            "[o][0].step(1)\n",
            "obj.attr.step('p', 'a', **opts)\n",
            "a.b.run_step(1, recovery='manual')\n",
            "f().run_step(1, recovery='manual')\n",
            "d['k'].run_step(1)\n",
        ],
    )
    def test_non_name_receiver_returns_rather_than_raises(self, source):
        result = lint_script(source, "recv.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status == "pass"
        # None of these is a shim step() call, so none may be BLOCKED.
        assert _findings_by_rule(result, "missing-recovery-policy") == []

    @pytest.mark.parametrize(
        "source",
        [
            "(lambda n: n)(1)\n",
            "handlers[0](1)\n",
            "make()(1)\n",
            "handlers['step'](1)\n",
        ],
    )
    def test_callee_that_is_neither_name_nor_attribute_is_handled(self, source):
        # Exercises the `return False` fallback in ALL THREE predicates: `func`
        # here is a Lambda / Subscript / Call, so nothing can read `.attr` or
        # `.id` off it. `handlers['step'](1)` is also the aliasing blind spot in
        # its subscript form — a genuine omission this rule cannot see, asserted
        # in the direction it behaves.
        result = lint_script(source, "callee.py")
        assert isinstance(result, ScriptValidationResult)
        assert result.status == "pass"
        assert result.findings == []


class TestRecoveryMessagesEchoNoSource:
    """SR-2 — the three new messages carry no source-derived content."""

    def test_no_argument_receiver_or_identifier_reaches_the_message(self):
        source = (
            SHIM_IMPORT + "step(SENTINEL_PROVIDER, SENTINEL_AGENT, 'SENTINEL_PROMPT')\n"
            "step(SENTINEL_PROVIDER, SENTINEL_AGENT, **SENTINEL_OPTS)\n"
            "run_step(SENTINEL_PROVIDER, recovery='SENTINEL_POLICY')\n"
            "SENTINEL_RECEIVER.run_step(1, recovery='SENTINEL_POLICY')\n"
        )
        result = lint_script(source, "sentinel.py")
        assert len(result.findings) == 4
        sentinels = (
            "SENTINEL_PROVIDER",
            "SENTINEL_AGENT",
            "SENTINEL_PROMPT",
            "SENTINEL_OPTS",
            "SENTINEL_POLICY",
            "SENTINEL_RECEIVER",
        )
        for f in result.findings:
            for token in sentinels:
                assert token not in f.message
        for mirrored in result.errors:
            for token in sentinels:
                assert token not in mirrored


class TestRecoveryRuleIdSeverityAndModelAdmission:
    """BR-10a and INV-5 — the closed ``Literal`` is what admits an id at all."""

    @pytest.mark.parametrize(
        "rule_id,severity",
        [
            ("missing-recovery-policy", "error"),
            ("unverifiable-recovery-policy", "warning"),
            ("unenforced-recovery-policy", "warning"),
        ],
    )
    def test_new_rule_id_is_admitted_by_lint_finding(self, rule_id, severity):
        # Regression guard for the defect that made this unit unbuildable: the
        # ids raised ValidationError from inside the walk, which is outside
        # lint_script's try, so it escaped as a 500 on validate and displaced
        # ScriptLintError at the run-path lint gate. Narrowing the Literal again
        # fails HERE instead of in production.
        f = LintFinding(rule_id=rule_id, severity=severity, line=1, message="m")
        assert f.rule_id == rule_id
        assert f.severity == severity

    def test_each_new_rule_id_carries_exactly_one_severity(self):
        source = (
            SHIM_IMPORT + "step('p', 'a', 'x')\n"
            "step('p', 'a', **opts)\n"
            "run_step('p', 'a', recovery='manual')\n"
        )
        result = lint_script(source, "all3.py")
        by_id = {}
        for f in result.findings:
            by_id.setdefault(f.rule_id, set()).add(f.severity)
        assert by_id == {
            "missing-recovery-policy": {"error"},
            "unverifiable-recovery-policy": {"warning"},
            "unenforced-recovery-policy": {"warning"},
        }
        assert result.status == "fail"  # exactly one ERROR among the three
        assert len(result.errors) == 1


class TestSyntaxRuleIdIsNotBorrowed:
    """``workflow_spec_service`` reads ``any(f.rule_id == "syntax" …)`` and zeroes
    ``spec.inputs`` when it is True. A new id mistaken for ``syntax`` would
    silently drop INPUTS extraction for every script missing a recovery policy —
    which is why reusing an existing id was rejected in favour of widening.
    """

    def test_recovery_findings_never_carry_the_syntax_rule_id(self):
        source = (
            SHIM_IMPORT + "step('p', 'a', 'x')\n"
            "step('p', 'a', **opts)\n"
            "run_step('p', 'a', recovery='manual')\n"
        )
        result = lint_script(source, "all3.py")
        assert len(result.findings) == 3
        # The exact predicate the caller applies.
        assert not any(f.rule_id == "syntax" for f in result.findings)

    def test_a_syntax_error_still_short_circuits_before_the_recovery_arms(self):
        # An unparsable tree is never walked, so the recovery rules cannot add to
        # a syntax finding. That is what keeps the caller's inputs-zeroing branch
        # meaning exactly what it meant before this unit.
        source = "def broken(:\nstep('p', 'a', 'x')\n"
        result = lint_script(source, "broken.py")
        assert [f.rule_id for f in result.findings] == ["syntax"]


class TestPerformanceSanity:
    def test_thousand_line_script_under_one_second(self):
        # The proportionate bound from performance-requirements.md: a sanity
        # ceiling against an accidental quadratic re-walk, not a tuned budget.
        lines = []
        for i in range(250):
            lines.append(f"import json  # block {i}")
            lines.append(f"def f_{i}(x):")
            lines.append(f"    return x + {i}")
            lines.append(f"v_{i} = f_{i}({i})")
        source = "\n".join(lines) + "\n"
        assert source.count("\n") == 1000
        start = time.monotonic()
        result = lint_script(source, "big.py")
        elapsed = time.monotonic() - start
        assert result.status == "pass"
        assert elapsed < 1.0
