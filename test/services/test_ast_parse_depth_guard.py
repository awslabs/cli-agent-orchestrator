"""Guard tests: every parser call on untrusted text, reachable by import-resolved dotted
name, stays behind a length bound (#969).

The defect class, not the instance. A function contracted to be total ("never raises
on input content") reaches an UNGATED NATIVE-RECURSION primitive -- CPython's C parser
via ``ast.parse`` -- on unbounded caller-supplied text. Pre-3.12 CPython's AST build
recurses in C with no ``_Py_EnterRecursiveCall`` gate, so deep nesting CAN exhaust the C
stack and segfault with no exception to catch -- but where that cliff sits is host- and
version-dependent, not a fixed line: this repo's DevSpace (CPython 3.10) segfaults around
150,000-200,000 nesting levels, while the GitHub Actions runner (CPython 3.11) instead
raises a clean ``RecursionError`` well before reaching a segfault on the same input.
Neither direction is universal, so a fix must handle both outcomes: an exception to
convert, and -- on a host whose cliff sits closer in -- no exception to see at all.
``sys.setrecursionlimit`` does not reach that native-recursion code either way.

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
from typing import Dict, List, Set, Tuple

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
# already-allowlisted function is still caught -- with the bound that keeps its input finite.
# A NEW entry means a parser call landed with no length bound in front of it -- add the bound
# first, then the row.
_ALLOWED_PARSER_CALLS: Dict[Tuple[str, str, str], str] = {
    (
        "cli_agent_orchestrator/services/script_lint.py",
        "lint_script",
        "ast.parse",
    ): "SCRIPT_LINT_MAX_SOURCE_CHARS, checked before the parse in the same function",
    (
        "cli_agent_orchestrator/services/workflow_spec_service.py",
        "_extract_inputs",
        "ast.parse",
    ): (
        "NO BOUND OF ITS OWN -- borrows lint_script's. Both callers run lint_script first "
        "and bail on its oversize finding: _read_script_spec (:1012) skips extraction when "
        "a rule_id == 'syntax' finding exists, _validated_script_spec (:1087) raises on "
        "status == 'fail'. test_oversize_rejection_keeps_the_rule_id_extract_inputs_skips_on "
        "pins that coupling; giving the oversize rejection its own rule_id reopens #969."
    ),
    (
        "cli_agent_orchestrator/services/workflow_spec_service.py",
        "_extract_inputs",
        "ast.literal_eval",
    ): (
        "Same shield as the ast.parse row above: these calls only run on sub-nodes of the "
        "tree that call already produced, so they inherit its coupling to lint_script's "
        "oversize rejection rather than needing a bound of their own."
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


def _qualname_of_parser_calls(tree: ast.Module) -> List[Tuple[str, str]]:
    """Collect (canonical call target, enclosing qualname) for each parser call."""

    aliases = _import_aliases(tree)
    found: List[Tuple[str, str]] = []

    def canonical(node: ast.expr) -> str:
        parts = _dotted(node).split(".")
        if parts and parts[0] in aliases:
            parts[0] = aliases[parts[0]]
        return ".".join(parts)

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = f"{scope}.{child.name}" if scope else child.name
            if isinstance(child, ast.Call) and canonical(child.func) in _PARSER_CALLS:
                found.append((canonical(child.func), scope or "<module>"))
            visit(child, inner)

    visit(tree, "")
    return found


def test_every_ast_parse_site_is_allowlisted_with_a_length_bound() -> None:
    """Enumerate the class, not the instance: no unbounded parser call may exist in src/."""

    seen: Set[Tuple[str, str, str]] = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        rel = path.relative_to(_SRC_ROOT).as_posix()
        for target, qualname in _qualname_of_parser_calls(tree):
            seen.add((rel, qualname, target))

    expected = set(_ALLOWED_PARSER_CALLS)
    unbounded = sorted(f"{p}::{q}::{t}" for p, q, t in seen - expected)
    assert not unbounded, (
        "new parser call on possibly-untrusted text with no recorded length bound: "
        f"{unbounded}. Bound the input BEFORE the parse (see SCRIPT_LINT_MAX_SOURCE_CHARS "
        "and #969), then add the site to _ALLOWED_PARSER_CALLS."
    )
    stale = sorted(f"{p}::{q}::{t}" for p, q, t in expected - seen)
    assert not stale, f"_ALLOWED_PARSER_CALLS has rotted; these sites are gone: {stale}"


# Driven in a CHILD interpreter on purpose: if the guard regresses this segfaults, and a
# segfault in the test process reports nothing at all. ``cao_home`` keeps the import chain
# off the live database.
_CRASH_PROBE = """
import sys
from cli_agent_orchestrator.constants import WORKFLOW_MAX_SPEC_BYTES
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
#    it is a byte budget. Parse a worst-case dense source at exactly that bound, since
#    that is the deepest tree _extract_inputs can ever be handed if the lint-cap
#    coupling above it is ever bypassed. Research measured this to survive with only
#    ~1.3x of margin to the segfault cliff on this host; CPython 3.11 on the CI runner
#    hits a RecursionError here instead (its cliff is further out than this host's, so
#    it raises cleanly rather than overflowing the C stack). Either outcome -- a
#    successful parse, or _extract_inputs' own RecursionError/MemoryError handling
#    converting the overrun into a ValueError -- is "control returned", which is what
#    this probe exists to prove; only an uncaught crash (segfault or an unconverted
#    exception) is a real regression.
worst_case = "a" + ".b" * ((WORKFLOW_MAX_SPEC_BYTES - 2) // 2) + "\\n"
assert len(worst_case) <= WORKFLOW_MAX_SPEC_BYTES
try:
    wss._extract_inputs(worst_case)
except ValueError:
    pass
print("SURVIVED")
"""


def test_pathological_nesting_crashes_no_child_interpreter(tmp_path: Path) -> None:
    """The exact #969 input, through BOTH parser entry points, must return control."""

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


def test_oversize_rejection_keeps_the_rule_id_extract_inputs_skips_on() -> None:
    """``_extract_inputs``' uncapped ``ast.parse`` is shielded only by this taxonomy."""

    # FLAT oversize source, not a deep one: this assertion runs in-process, and the whole
    # point of the class is that a deep source would take this process down rather than
    # report. Depth belongs in the subprocess test above; here only the LENGTH branch
    # matters, and length is what the guard keys on.
    result = lint_script("x = 1\n" * (SCRIPT_LINT_MAX_SOURCE_CHARS // 6 + 1), "flat.py")
    oversize = [f for f in result.findings if "over the" in f.message]
    assert len(oversize) == 1, result.findings
    # workflow_spec_service.py:1012 keys its skip on this exact rule_id ...
    assert oversize[0].rule_id == "syntax"
    # ... and :1087 keys its raise on status == "fail", which needs this severity.
    assert oversize[0].severity == "error"
    assert result.status == "fail"


def test_lint_cap_stays_below_the_measured_segfault_cliff() -> None:
    """Make the two measurements recorded at the constant executable rather than prose."""

    # Measured cliff: 150_000-200_000 nesting levels. A dense all-nesting source spends
    # ~2 chars per level, so 128 KiB admits 65_536 levels -- ~2.6x of margin.
    assert SCRIPT_LINT_MAX_SOURCE_CHARS <= 128 * 1024, (
        "raising this cap narrows the margin to the ast.parse segfault cliff; re-measure "
        "the cliff on the target runner first (#969)"
    )
    largest = _REPO_ROOT / "examples" / "workflows" / "pr-health" / "pr_health.py"
    assert largest.is_file(), largest
    assert SCRIPT_LINT_MAX_SOURCE_CHARS > len(largest.read_text(encoding="utf-8")), (
        "the cap must stay above the largest workflow script shipped in this repo, or the "
        "crash guard starts rejecting legitimate scripts"
    )
