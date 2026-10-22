"""Guard tests: every parser call on untrusted text, reachable by import-resolved dotted
name, stays behind a length bound (#969).

The defect class, not the instance. A function contracted to be total ("never raises
on input content") reaches an UNGATED NATIVE-RECURSION primitive -- CPython's C parser
via ``ast.parse`` -- on unbounded caller-supplied text. CPython 3.10's AST build recurses
in C with no recursion gate, so deep nesting exhausts the C stack and segfaults with no
exception to catch; CPython 3.11 added the gate, so 3.11+ raise a clean ``RecursionError``
from about 3,500 levels instead. Where 3.10's cliff sits depends on the host's stack: the
two hosts measured (x86_64 Linux and macOS arm64, 8 MiB stack) both segfault between
130,500 and 131,000 levels of ``a.b.b...`` -- see ``SCRIPT_LINT_MAX_SOURCE_CHARS`` for the
table. A fix must therefore handle both outcomes: an exception to convert on 3.11+, and on
3.10 no exception to see at all. ``sys.setrecursionlimit`` does not reach that
native-recursion code either way.

The tooling half of the class is why a reader found this and a test did not. The original
``test_deeply_nested_expression_is_total`` asserted the right property in the wrong
executor: it called ``lint_script`` IN-PROCESS, and the failure mode under test kills the
asserting process. A regression therefore produced a truncated run, never a red test.
Hence the rule these tests encode: a test that asserts "this does not crash the
interpreter" MUST NOT run its subject in the interpreter running the test.

Scope limit: the enumeration test is a static scan. It resolves ``import``/``from...import``
aliases, so a rename like ``import ast as _ast`` is still caught, but it does NOT follow
assignment or attribute indirection -- ``p = ast.parse; p(src)`` or
``getattr(ast, "parse")(src)`` is invisible to it. That is a known, accepted gap (a static
scan loses any indirection arms race); the mechanism this file leans on is the child-process
crash probe below, which measures the real interpreter regardless of how the call got there.

Same family as ``test/test_http_only_boundary.py`` and ``test/test_no_ffi_guard.py``:
AST-scan ``src/`` and lock an invariant the codebase already satisfies.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Set, Tuple

import pytest

import cli_agent_orchestrator
from cli_agent_orchestrator.constants import SCRIPT_LINT_MAX_SOURCE_CHARS
from cli_agent_orchestrator.services import workflow_spec_service as wss
from cli_agent_orchestrator.services.script_lint import lint_script

_SRC_ROOT = Path(cli_agent_orchestrator.__file__).parent.parent
_REPO_ROOT = _SRC_ROOT.parent

# Call targets that recurse natively over their input. ``ast.parse``/``compile`` build an
# AST in C; ``ast.literal_eval`` walks one; ``exec``/``eval`` compile first.
_PARSER_CALLS = frozenset({"ast.parse", "ast.literal_eval", "compile", "exec", "eval"})

# Every parser call in ``src/``, keyed by (path relative to src/, enclosing qualname, call
# target) -- the target is part of the key so a SECOND, different parser call added inside an
# already-allowlisted function is still caught. The value names the constant the enclosing
# function compares its input against BEFORE the call, plus why; the test checks that the
# comparison really exists (a row is a claim, the scan is the proof -- #970 review P2-1 found
# a row whose "bound" was another function's ordering). A NEW entry means a parser call landed
# with no length bound in front of it -- add the bound first, then the row.
_ALLOWED_PARSER_CALLS: Dict[Tuple[str, str, str], Tuple[str, str]] = {
    (
        "cli_agent_orchestrator/services/script_lint.py",
        "lint_script",
        "ast.parse",
    ): (
        "SCRIPT_LINT_MAX_SOURCE_CHARS",
        "checked before the parse in the same function; the verdict is a syntax finding",
    ),
    (
        "cli_agent_orchestrator/services/workflow_spec_service.py",
        "_extract_inputs",
        "ast.parse",
    ): (
        "SCRIPT_LINT_MAX_SOURCE_CHARS",
        "checked before the parse in the same function; the verdict is a ValueError. Until "
        "#970's P2-1 fix this row borrowed lint_script's cap through the callers' lint-first "
        "ordering, and 256 KiB of a.b.b... reached ast.parse and segfaulted CPython 3.10.",
    ),
    (
        "cli_agent_orchestrator/services/workflow_spec_service.py",
        "_extract_inputs",
        "ast.literal_eval",
    ): (
        "SCRIPT_LINT_MAX_SOURCE_CHARS",
        "same check as the ast.parse row above: these calls only run on sub-nodes of the "
        "tree that call already produced.",
    ),
}


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_dotted(node.value)}.{node.attr}"
    return ""


def _import_aliases(tree: ast.Module) -> Dict[str, str]:
    """Map each local binding to the canonical dotted target it refers to.

    Needed because matching the literal source spelling is defeated by one rename:
    ``import ast as _ast`` makes ``_ast.parse`` invisible to a name match, and that is
    exactly how a sibling would land. Resolve the alias instead of trusting the spelling.
    """

    aliases: Dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name.split(".")[0]] = (
                    a.name if a.asname else a.name.split(".")[0]
                )
        elif isinstance(node, ast.ImportFrom) and node.module:
            for a in node.names:
                aliases[a.asname or a.name] = f"{node.module}.{a.name}"
    return aliases


class _ParserCall(NamedTuple):
    target: str  # canonical dotted call target, e.g. ``ast.parse``
    qualname: str  # enclosing def/class chain, or ``<module>``
    lineno: int
    scope: ast.AST  # the enclosing def/class node (or the module) the bound must live in


def _parser_calls(tree: ast.Module) -> List[_ParserCall]:
    """Collect every parser call with its enclosing scope, in source order."""

    aliases = _import_aliases(tree)
    found: List[_ParserCall] = []

    def canonical(node: ast.expr) -> str:
        parts = _dotted(node).split(".")
        if parts and parts[0] in aliases:
            parts[0] = aliases[parts[0]]
        return ".".join(parts)

    def visit(node: ast.AST, scope: str, scope_node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            inner, inner_node = scope, scope_node
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
                inner_node = child
            if isinstance(child, ast.Call) and canonical(child.func) in _PARSER_CALLS:
                found.append(
                    _ParserCall(
                        canonical(child.func), scope or "<module>", child.lineno, scope_node
                    )
                )
            visit(child, inner, inner_node)

    visit(tree, "", tree)
    return found


def _bound_is_checked_before(scope: ast.AST, bound: str, call_lineno: int) -> bool:
    """True when ``scope`` compares something against ``bound`` on a line before the call.

    Deliberately syntactic: a ``Compare`` whose left side or any comparator is the named
    constant (``len(source) > SCRIPT_LINT_MAX_SOURCE_CHARS``). It does not prove the branch
    refuses the input -- the unit tests on each site do that -- only that a bound is
    present in the SAME function as the parse, so that no caller's ordering is relied on.
    """

    for node in ast.walk(scope):
        if isinstance(node, ast.Compare) and node.lineno < call_lineno:
            operands = [node.left, *node.comparators]
            if any(_dotted(operand).split(".")[-1] == bound for operand in operands):
                return True
    return False


def test_every_ast_parse_site_is_allowlisted_with_a_length_bound() -> None:
    """Enumerate the class, not the instance: no unbounded parser call may exist in src/.

    Two checks. The allowlist must equal the set of parser calls found (a new call, or a
    stale row, fails). AND each allowlisted call's enclosing function must compare its
    input against the named bound before the call -- so a row cannot claim a bound that
    lives in some other function (#970 review P2-1: the ``_extract_inputs`` row said
    "borrows lint_script's", and 256 KiB of nesting reached its ``ast.parse``).
    """

    seen: Dict[Tuple[str, str, str], _ParserCall] = {}
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for call in _parser_calls(tree):
            # First occurrence wins: the bound must precede the EARLIEST call of that
            # target in the function.
            seen.setdefault((rel, call.qualname, call.target), call)

    expected = set(_ALLOWED_PARSER_CALLS)
    unbounded = sorted(f"{p}::{q}::{t}" for p, q, t in set(seen) - expected)
    assert not unbounded, (
        "new parser call on possibly-untrusted text with no recorded length bound: "
        f"{unbounded}. Bound the input BEFORE the parse (see SCRIPT_LINT_MAX_SOURCE_CHARS "
        "and #969), then add the site to _ALLOWED_PARSER_CALLS."
    )
    stale = sorted(f"{p}::{q}::{t}" for p, q, t in expected - set(seen))
    assert not stale, f"_ALLOWED_PARSER_CALLS has rotted; these sites are gone: {stale}"

    unguarded = [
        f"{p}::{q}::{t} (line {seen[(p, q, t)].lineno}) has no comparison against {bound} "
        "before the call in the same function"
        for (p, q, t), (bound, _why) in _ALLOWED_PARSER_CALLS.items()
        if not _bound_is_checked_before(seen[(p, q, t)].scope, bound, seen[(p, q, t)].lineno)
    ]
    assert not unguarded, (
        "an allowlisted parser call relies on a bound outside its own function; that is a "
        f"coupling, not a bound (#969, #970 P2-1): {unguarded}"
    )


# Driven in a CHILD interpreter on purpose: if the guard regresses this segfaults, and a
# segfault in the test process reports nothing at all. ``cao_home`` keeps the import chain
# off the live database.
_CRASH_PROBE = """
import sys
from cli_agent_orchestrator.constants import SCRIPT_LINT_MAX_SOURCE_CHARS, WORKFLOW_MAX_SPEC_BYTES
from cli_agent_orchestrator.services.script_lint import lint_script
from cli_agent_orchestrator.services import workflow_spec_service as wss

# 1. The exact #969 input, through the linter's own cap.
source = "a" + ".b" * 200_000 + "\\n"
assert lint_script(source, "deep.py").status == "fail"

# 2. Same input through the write-path gate, which is what keeps it away from
#    _extract_inputs' uncapped ast.parse.
try:
    wss._validated_script_spec("deep", source, "/tmp/deep.py")
except ValueError:
    pass
else:
    sys.exit("oversize source was accepted by the write-path gate")

# 3. The read path admits up to WORKFLOW_MAX_SPEC_BYTES, which is NOT a crash guard --
#    it is a byte budget. A dense source at exactly that size is 131,071 nesting levels,
#    which is PAST the measured CPython 3.10 cliff (about 130,700 levels on both hosts
#    measured, see SCRIPT_LINT_MAX_SOURCE_CHARS). Until #970's P2-1 fix this parse
#    segfaulted here on 3.10; _extract_inputs now refuses it on its own length bound
#    before ast.parse runs, and that refusal is what this step asserts.
worst_case = "a" + ".b" * ((WORKFLOW_MAX_SPEC_BYTES - 2) // 2) + "\\n"
assert len(worst_case) <= WORKFLOW_MAX_SPEC_BYTES
try:
    wss._extract_inputs(worst_case)
except ValueError:
    pass
else:
    sys.exit("_extract_inputs parsed a source over SCRIPT_LINT_MAX_SOURCE_CHARS")

# 4. The deepest source the cap ADMITS, through both parser entry points. This is the
#    margin check that used to run in the pytest process (#970 review P3-4): raise
#    SCRIPT_LINT_MAX_SOURCE_CHARS past the host's cliff and this child segfaults (rc -11)
#    and the test goes red, instead of the pytest process dumping core. On CPython 3.10
#    65,535 levels parse; 3.11+ raise RecursionError, which both callers convert.
at_cap = "a" + ".b" * ((SCRIPT_LINT_MAX_SOURCE_CHARS - 2) // 2) + "\\n"
assert len(at_cap) <= SCRIPT_LINT_MAX_SOURCE_CHARS
assert lint_script(at_cap, "at_cap.py").status in ("pass", "fail")
try:
    wss._extract_inputs(at_cap)
except ValueError as e:
    assert "too deeply nested" in str(e) or "too complex" in str(e), e
print("SURVIVED")
"""


def test_pathological_nesting_crashes_no_child_interpreter(tmp_path: Path) -> None:
    """The #969 input and the deepest in-cap source, through BOTH parser entry points,
    must return control."""

    env = {**os.environ, "CAO_HOME_DIR": str(tmp_path), "PYTHONFAULTHANDLER": "1"}
    proc = subprocess.run(
        [sys.executable, "-c", _CRASH_PROBE],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 0, (
        f"a deeply nested source took the interpreter down (returncode {proc.returncode}; "
        f"-11 is SIGSEGV). Either lint_script's cap regressed, or the worst-case source "
        f"WORKFLOW_MAX_SPEC_BYTES admits is past this machine's ast.parse C-stack cliff -- "
        f"in which case _extract_inputs needs a crash cap of its own, not a byte budget "
        f"(#969). stderr:\n{proc.stderr}"
    )
    assert "SURVIVED" in proc.stdout


def test_extract_inputs_converts_recursion_error_to_value_error(scoped_ast_parse) -> None:
    """The backstop arm in ``_extract_inputs`` must fire, not just be unreachable.

    The child-process probe above measures a REAL interpreter cliff, which is host- and
    version-dependent (see the module docstring) -- on a host whose cliff sits further out,
    that probe passes via a successful parse and never exercises these ``except`` arms at
    all. Faking ``ast.parse`` forces the arm deterministically, on every host. The fake is
    scoped to this one source: a process-wide fake also breaks pytest's own failure
    rendering and turns a red test into ``INTERNALERROR`` (#970 review, P3-3).
    """

    scoped_ast_parse("INPUTS = {}\n", RecursionError("maximum recursion depth exceeded"))
    with pytest.raises(ValueError, match="too deeply nested"):
        wss._extract_inputs("INPUTS = {}\n")


def test_extract_inputs_converts_memory_error_to_value_error(scoped_ast_parse) -> None:
    """Same backstop, the other arm: see
    ``test_extract_inputs_converts_recursion_error_to_value_error`` for why this must be
    forced rather than relied on to fire naturally."""

    scoped_ast_parse("INPUTS = {}\n", MemoryError("out of memory"))
    with pytest.raises(ValueError, match="too complex"):
        wss._extract_inputs("INPUTS = {}\n")


# --- The tooling half of the class: no test may fake the parser for the whole process ---

_TEST_ROOT = _REPO_ROOT / "test"
_SANCTIONED_PARSE_FAKE = _TEST_ROOT / "services" / "conftest.py"  # ``scoped_ast_parse``


def _fakes_ast_parse(call: ast.Call) -> bool:
    """True for ``monkeypatch.setattr(<x>.ast, "parse", ...)``, ``setattr(ast, "parse", ...)``,
    ``patch.object(ast, "parse", ...)`` and the string forms ``setattr("...ast.parse", ...)``
    / ``patch("...ast.parse")``."""

    func = _dotted(call.func).split(".")[-1]
    if func not in {"setattr", "patch", "object"}:
        return False
    args = list(call.args)
    for i, arg in enumerate(args):
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            if arg.value == "ast.parse" or arg.value.endswith(".ast.parse"):
                return True
        elif _dotted(arg).split(".")[-1] == "ast" and i + 1 < len(args):
            nxt = args[i + 1]
            if isinstance(nxt, ast.Constant) and nxt.value == "parse":
                return True
    return False


def test_no_test_fakes_ast_parse_globally() -> None:
    """Every fake of ``ast.parse`` in ``test/`` goes through ``scoped_ast_parse``.

    ``<module>.ast`` is the ``ast`` module, so a direct ``setattr(mod.ast, "parse", fake)``
    replaces the parser for pytest too; when the test then fails, pytest's assertion
    renderer calls the fake, it raises again, and the session dies with ``INTERNALERROR``
    and the rest of the run unexecuted (#970 review, P3-3). Three tests in this PR had that
    shape; the fixture scopes the fake to one source and delegates the rest to the real
    parser, and this scan keeps a fourth from landing.
    """

    offenders: List[str] = []
    for path in sorted(_TEST_ROOT.rglob("*.py")):
        if path == _SANCTIONED_PARSE_FAKE:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _fakes_ast_parse(node):
                offenders.append(f"{path.relative_to(_REPO_ROOT).as_posix()}:{node.lineno}")
    assert not offenders, (
        "these tests replace ast.parse for the whole process; use the scoped_ast_parse "
        f"fixture (test/services/conftest.py) instead: {offenders}"
    )


def test_extract_inputs_refuses_oversize_source_before_parsing(scoped_ast_parse) -> None:
    """P2-1: ``_extract_inputs`` owns its bound; it does not borrow the callers' lint-first order."""

    # FLAT oversize source: this runs in-process, and a deep one would take the process
    # down rather than report. Depth is the child probe's job; here only the LENGTH branch
    # matters, and the recorded parser calls prove it fired BEFORE the parse.
    source = "x = 1\n" * (SCRIPT_LINT_MAX_SOURCE_CHARS // 6 + 1)
    assert len(source) > SCRIPT_LINT_MAX_SOURCE_CHARS
    parse_calls = scoped_ast_parse(source)
    with pytest.raises(ValueError, match=f"over the {SCRIPT_LINT_MAX_SOURCE_CHARS}-character"):
        wss._extract_inputs(source)
    assert parse_calls == [], "ast.parse ran on a source over the cap"


def test_oversize_rejection_keeps_the_rule_id_extract_inputs_skips_on() -> None:
    """The oversize verdict stays a ``syntax`` ERROR, which both callers key on.

    No longer what keeps ``_extract_inputs`` safe (it has its own bound since P2-1), but
    still load-bearing for behaviour: ``_read_script_spec`` skips extraction on a
    ``rule_id == "syntax"`` finding so a broken file on disk still loads with its finding
    intact, and ``_validated_script_spec`` refuses on ``status == "fail"``. Give the
    oversize rejection its own rule_id and the read path would extract (and refuse) instead
    of degrading gracefully.
    """

    result = lint_script("x = 1\n" * (SCRIPT_LINT_MAX_SOURCE_CHARS // 6 + 1), "flat.py")
    oversize = [f for f in result.findings if "over the" in f.message]
    assert len(oversize) == 1, result.findings
    # _read_script_spec keys its skip on this exact rule_id ...
    assert oversize[0].rule_id == "syntax"
    # ... and _validated_script_spec keys its raise on status == "fail", which needs this
    # severity.
    assert oversize[0].severity == "error"
    assert result.status == "fail"


def test_lint_cap_is_pinned_and_clears_the_largest_shipped_script() -> None:
    """Pin the constant's value and its floor. The margin itself is measured by probe step 4.

    This test measures nothing (#970 review P3-4 on its predecessor's name): it is a
    tripwire so that raising SCRIPT_LINT_MAX_SOURCE_CHARS is a deliberate act that
    re-reads the measurements recorded at the constant, and a floor so the crash guard
    never rejects a workflow script this repo ships.
    """

    # 128 KiB admits 65,535 levels of a.b.b...; the measured CPython 3.10 cliff is about
    # 130,700 levels on both hosts measured (see the constant), about 2.0x of margin.
    assert SCRIPT_LINT_MAX_SOURCE_CHARS == 128 * 1024, (
        "SCRIPT_LINT_MAX_SOURCE_CHARS changed; re-measure the ast.parse cliff on CPython "
        "3.10 (the child probe's step 4 does so on every run) and update the numbers "
        "recorded at the constant (#969)"
    )
    largest = _REPO_ROOT / "examples" / "workflows" / "pr-health" / "pr_health.py"
    assert largest.is_file(), largest
    assert SCRIPT_LINT_MAX_SOURCE_CHARS > len(largest.read_text(encoding="utf-8")), (
        "the cap must stay above the largest workflow script shipped in this repo, or the "
        "crash guard starts rejecting legitimate scripts"
    )
