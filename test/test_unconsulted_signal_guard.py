"""Guard test: a detection signal that is computed must be consulted (#752).

GH#752 was two bugs with one cause: ``get_status()`` computed
``placeholder_in_tail`` and never read it, so the only plan-independent
readiness marker was silently dropped and the processing indicator was left
unanchored. Nothing in the gate can see that shape -- the Code Quality job runs
black, isort and mypy only (``.github/workflows/ci.yml:277-288``), and mypy has
no unused-local check at any strictness, so an assigned-but-never-read local
survives review and CI indefinitely.

Two checks lock the class:

1. ``test_no_function_computes_a_local_it_never_reads`` -- the F841 shape, over
   every module in the package. Allowlist is deliberately EMPTY: the tree
   satisfies this today, so any new dead signal fails the build.
2. ``test_module_level_signals_are_consulted_or_declared_dormant`` -- the same
   class one scope up: a module-level constant (typically a detection pattern)
   declared and never loaded anywhere in ``src/`` or ``test/``. Dormant-on-
   purpose constants are listed with the reason their own comment gives, so
   adding an unconsulted signal costs an explicit entry rather than passing
   silently.
"""

from __future__ import annotations

import ast
import re
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterator, List, Set, Tuple, Union

import cli_agent_orchestrator

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent
_REPO_ROOT = _PACKAGE_ROOT.parents[1]
_TEST_ROOT = _REPO_ROOT / "test"

# ruff's default dummy-variable convention: a leading underscore marks a value
# whose result is deliberately discarded.
_DUMMY_RE = re.compile(r"^_")
_CONSTANT_RE = re.compile(r"[A-Z][A-Z0-9_]{2,}")

# (module path relative to the package root, constant name) -> why it is dormant.
# Every entry below carries that justification in a comment at its definition.
_DORMANT_SIGNALS: Dict[Tuple[str, str], str] = {
    (
        "constants.py",
        "MEMORY_ARCHIVE_MAX_DECOMPRESSED_BYTES",
    ): "RESERVED for the tar backend (#345)",
    ("constants.py", "MEMORY_ARCHIVE_MAX_FILE_BYTES"): "RESERVED for the tar backend (#345)",
    ("constants.py", "MEMORY_ARCHIVE_MAX_GZIP_RATIO"): "RESERVED for the tar backend (#345)",
    ("decisions/types.py", "UNSURE"): "closed-contract literal, compared as a string",
    ("providers/claude_code.py", "THINKING_BEFORE_SEPARATOR_PATTERN"): (
        "documented reference pattern; get_status uses an inline anchored version"
    ),
    ("providers/claude_code.py", "NEW_TUI_SPINNER_PATTERN"): (
        "loose reference shape; the box-anchored NEW_TUI_BOX_SPINNER_PATTERN is what gates"
    ),
    ("providers/kimi_cli.py", "NEW_TUI_SPINNER_PATTERN"): (
        "named glyph class; the shared classifier decides structurally"
    ),
    ("providers/kimi_cli.py", "NEW_TUI_MOON_TIP_PATTERN"): "named so the exclusion is explicit",
    ("providers/kimi_cli.py", "NEW_TUI_BOOT_CHROME_PATTERN"): (
        "shapes documented; the shared classifier decides structurally"
    ),
    ("providers/kimi_transcript.py", "BULLET_LINE_RE"): "documented dialect pattern",
    ("services/wiki_lint.py", "LOG_SOFT_SIZE_CAP_BYTES"): "T8.f forward dep",
}


FunctionNode = Union[ast.FunctionDef, ast.AsyncFunctionDef]


def _python_files(root: Path) -> List[Path]:
    return sorted(root.rglob("*.py"))


def _functions(tree: ast.AST) -> Iterator[FunctionNode]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _own_body(func: FunctionNode) -> Iterator[ast.AST]:
    """Nodes belonging to ``func`` itself, not to a nested function or class."""

    stack: List[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)):
            continue
        stack.extend(ast.iter_child_nodes(node))


def _dead_locals(func: FunctionNode) -> List[Tuple[str, int]]:
    """Plain local bindings in ``func`` that no expression ever loads."""

    stores: Dict[str, int] = {}
    for node in _own_body(func):
        targets: List[ast.Name] = []
        if isinstance(node, ast.Assign):
            targets = [t for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets = [node.target] if isinstance(node.target, ast.Name) else []
        elif isinstance(node, ast.NamedExpr):
            targets = [node.target] if isinstance(node.target, ast.Name) else []
        elif isinstance(node, ast.withitem):
            targets = [node.optional_vars] if isinstance(node.optional_vars, ast.Name) else []
        for target in targets:
            if not _DUMMY_RE.match(target.id):
                stores.setdefault(target.id, target.lineno)
    if not stores:
        return []

    # Reads from anywhere in the subtree, INCLUDING nested closures, plus
    # del/global/nonlocal and augmented assignment (which reads before writing).
    reads: Set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
            reads.add(node.id)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            reads.update(node.names)
        elif isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
            reads.add(node.target.id)
    return sorted((name, line) for name, line in stores.items() if name not in reads)


def test_no_function_computes_a_local_it_never_reads() -> None:
    """The GH#752 shape: a signal is derived and then never consulted."""

    offenders: List[str] = []
    for path in _python_files(_PACKAGE_ROOT):
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        for func in _functions(tree):
            for name, line in _dead_locals(func):
                rel = path.relative_to(_PACKAGE_ROOT)
                offenders.append(f"{rel}:{line}: {func.name}() binds {name!r} and never reads it")
    assert not offenders, (
        "Dead signal(s): a value is computed and no code path consults it. Either wire it into "
        "the decision it was written for, or prefix it with '_' to say the result is discarded.\n"
        + "\n".join(offenders)
    )


@lru_cache(maxsize=1)
def _loaded_names() -> Set[str]:
    """Every name loaded, attribute-accessed or imported across src/ and test/."""

    names: Set[str] = set()
    for root in (_PACKAGE_ROOT, _TEST_ROOT):
        for path in _python_files(root):
            tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Name) and not isinstance(node.ctx, ast.Store):
                    names.add(node.id)
                elif isinstance(node, ast.Attribute):
                    names.add(node.attr)
                elif isinstance(node, ast.ImportFrom):
                    names.update(alias.name for alias in node.names)
    return names


def test_module_level_signals_are_consulted_or_declared_dormant() -> None:
    """A declared detection constant must be read somewhere, or listed as dormant."""

    loaded = _loaded_names()
    offenders: List[str] = []
    for path in _python_files(_PACKAGE_ROOT):
        rel = str(path.relative_to(_PACKAGE_ROOT))
        tree = ast.parse(path.read_text(encoding="utf-8"), str(path))
        exported: Set[str] = set()
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets
            ):
                exported = {
                    c.value
                    for c in ast.walk(node)
                    if isinstance(c, ast.Constant) and isinstance(c.value, str)
                }
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            for target in node.targets:
                if not isinstance(target, ast.Name) or not _CONSTANT_RE.fullmatch(target.id):
                    continue
                if target.id in loaded or target.id in exported:
                    continue
                if (rel, target.id) in _DORMANT_SIGNALS:
                    continue
                offenders.append(f"{rel}:{target.lineno}: {target.id} declared, never consulted")
    assert not offenders, (
        "Declared signal(s) nothing reads -- the GH#752 class one scope up. Consult it on the "
        "path it was written for, or add it to _DORMANT_SIGNALS with the reason.\n"
        + "\n".join(offenders)
    )


def test_dormant_allowlist_has_no_stale_entries() -> None:
    """A wired-up or deleted constant must leave the allowlist, so it stays honest."""

    loaded = _loaded_names()
    stale = [
        f"{rel}:{name}"
        for (rel, name) in _DORMANT_SIGNALS
        if name in loaded or not (_PACKAGE_ROOT / rel).exists()
    ]
    assert not stale, "Stale _DORMANT_SIGNALS entries (now consulted or gone): " + ", ".join(stale)
