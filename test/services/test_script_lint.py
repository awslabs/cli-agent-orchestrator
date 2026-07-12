"""Tests for the static script linter (issue #312, Bolt 2 / U1, C2).

Deterministic fixtures are the primary suite (verdict correctness); the
hypothesis property test is the safety net proving totality (BR-6
never-raises) over the input space fixtures can't enumerate. Tests assert on
rule_id/line/severity, never on parser message prose (it varies by CPython
minor), and never on WHICH catch arm fired (the null-byte case moved from
ValueError to SyntaxError in 3.12, gh-96670 — totality is the invariant).
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
