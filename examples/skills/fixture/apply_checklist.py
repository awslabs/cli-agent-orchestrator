#!/usr/bin/env python3
"""Apply the installed skill's guidance to the fixture — the example's fixture task.

This script stands in for an agent: it loads the skill through the same
retrieval function the ``load_skill`` MCP tool serves, then applies each
checklist item to the Python snippet in ``sample_note.md`` using ``ast``:

- Missing imports   — a name is loaded but never imported or defined
- Mutable defaults  — a list/dict/set literal (or call) as a default argument
- Bare ``except:``  — an except handler with no exception type
- Missing type hints— a public function without full annotations

It prints one finding per item and finishes with the skill's output contract:

    Checklist applied: <applied>/<total> items

If the skill or the fixture is missing, the script exits non-zero and prints
no contract line — which is the example's documented failure behaviour, and
the reason the line means anything when it does appear.

Usage:
    apply_checklist.py [fixture]   # defaults to sample_note.md beside this file

Exit codes: 0 checklist applied; 1 skill or fixture missing/unusable; 2 skill
loaded but contains no checklist items; 3 fixture has no parsable Python code.
"""

from __future__ import annotations

import ast
import builtins
import sys
from pathlib import Path

from cli_agent_orchestrator.utils.skills import load_skill_content

SKILL_NAME = "python-review-checklist"
DEFAULT_FIXTURE = Path(__file__).resolve().parent / "sample_note.md"
CHECKLIST_PREFIX = "- [ ]"


def _extract_python_code(markdown_text: str) -> str | None:
    """Return the first fenced ```python block in the Markdown, if any."""
    lines = markdown_text.splitlines()
    inside = False
    block: list[str] = []
    for line in lines:
        if not inside and line.strip().startswith("```python"):
            inside = True
            continue
        if inside and line.strip() == "```":
            break
        if inside:
            block.append(line)
    return "\n".join(block) if block else None


def _undefined_names(tree: ast.Module) -> list[tuple[int, str]]:
    """Names loaded but never defined, as (first use line, name) pairs.

    Deliberately simple — a teaching example, not pyflakes: definitions come
    from imports, function/class names, assignments, and parameters; every
    other loaded Name that is not a builtin is reported.
    """
    defined = set(dir(builtins))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import | ast.ImportFrom):
            for alias in node.names:
                defined.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            defined.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            defined.add(node.id)
        elif isinstance(node, ast.arg):
            defined.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            defined.add(node.name)
    first_use: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            if node.id not in defined and node.id not in first_use:
                first_use[node.id] = node.lineno
    return [(lineno, name) for name, lineno in sorted(first_use.items(), key=lambda kv: kv[1])]


def _mutable_defaults(tree: ast.Module) -> list[tuple[int, str]]:
    """Functions whose default argument is a mutable literal or constructor."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        defaults = list(node.args.defaults) + [d for d in node.args.kw_defaults if d]
        for default in defaults:
            is_literal = isinstance(default, ast.List | ast.Dict | ast.Set)
            is_constructor = (
                isinstance(default, ast.Call)
                and isinstance(default.func, ast.Name)
                and default.func.id in {"list", "dict", "set"}
            )
            if is_literal or is_constructor:
                findings.append((node.lineno, node.name))
                break
    return findings


def _bare_excepts(tree: ast.Module) -> list[int]:
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.ExceptHandler) and node.type is None
    ]


def _missing_type_hints(tree: ast.Module) -> list[tuple[int, str]]:
    """Public functions without full parameter and return annotations."""
    findings: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name.startswith("_"):
            continue
        args = node.args.posonlyargs + node.args.args + node.args.kwonlyargs
        if any(a.annotation is None for a in args) or node.returns is None:
            findings.append((node.lineno, node.name))
    return findings


def main() -> int:
    try:
        content = load_skill_content(SKILL_NAME)
    except Exception as exc:
        print(f"error: skill '{SKILL_NAME}' is not available: {exc}", file=sys.stderr)
        return 1

    items = [
        line.strip() for line in content.splitlines() if line.strip().startswith(CHECKLIST_PREFIX)
    ]
    if not items:
        print("error: skill body contains no checklist items", file=sys.stderr)
        return 2

    fixture_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_FIXTURE
    try:
        markdown = fixture_path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"error: cannot read fixture '{fixture_path}': {exc}", file=sys.stderr)
        return 1

    code = _extract_python_code(markdown)
    if not code or not code.strip():
        print(f"error: '{fixture_path}' contains no Python code block to review", file=sys.stderr)
        return 3
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        print(f"error: fixture code does not parse: {exc}", file=sys.stderr)
        return 3

    findings: list[str] = []
    for lineno, name in _undefined_names(tree):
        findings.append(f"line {lineno}: '{name}' is used but never imported")
    for lineno, name in _mutable_defaults(tree):
        findings.append(f"line {lineno}: mutable default argument in '{name}'")
    for lineno in _bare_excepts(tree):
        findings.append(f"line {lineno}: bare 'except:' clause")
    for lineno, name in _missing_type_hints(tree):
        findings.append(f"line {lineno}: '{name}' is missing type hints")

    for finding in findings:
        print(finding)
    print(f"Checklist applied: {len(items)}/{len(items)} items")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
