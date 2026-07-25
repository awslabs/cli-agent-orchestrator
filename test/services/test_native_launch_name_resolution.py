"""Every name a launch path calls must actually resolve.

A native Claude launch raised ``NameError: name 'claude_native_launch' is
not defined`` in production. The observed-model check had been added to
``_launch_native_tui``, but that function's local import block names only
``claude_native_readiness`` and ``kimi_native_bootstrap``, and there is no
module-level import either. Every native Claude launch that reached a
SessionStart proof therefore died with HTTP 500 *after* the pane and the
proof existed, leaving the row ``launching`` for a bind that could never
succeed.

Nothing caught it because the helper was tested directly and the launch
harnesses never execute that line. So this asserts the property that was
actually violated — a name used at runtime resolves — rather than any one
call site, which is what makes it hold for the next one too.

Deliberately dependency-free: a linter would find this faster, but a gate
that only runs where an optional tool is installed is a gate that does not
run.
"""

from __future__ import annotations

import ast
import builtins
import importlib
import pathlib

import pytest

#: The modules whose launch/bind paths a native generation traverses.
#: Scoped rather than repo-wide so a failure names something in this lane.
AUDITED = (
    "cli_agent_orchestrator.services.managed_launch_v2",
    "cli_agent_orchestrator.services.claude_native_readiness",
    "cli_agent_orchestrator.services.claude_native_launch",
    "cli_agent_orchestrator.services.kimi_native_bootstrap",
)


def _bound_names(node: ast.AST) -> set[str]:
    """Every name a function binds locally: args, assignments, imports."""
    bound: set[str] = set()
    for arg_group in ("args", "posonlyargs", "kwonlyargs"):
        for arg in getattr(node.args, arg_group, []) or []:
            bound.add(arg.arg)
    for extra in (node.args.vararg, node.args.kwarg):
        if extra is not None:
            bound.add(extra.arg)
    for sub in ast.walk(node):
        if isinstance(sub, (ast.Import, ast.ImportFrom)):
            for alias in sub.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Store):
            bound.add(sub.id)
        elif isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(sub.name)
        elif isinstance(sub, ast.ExceptHandler) and sub.name:
            bound.add(sub.name)
        elif isinstance(sub, (ast.comprehension,)):
            for target in ast.walk(sub.target):
                if isinstance(target, ast.Name):
                    bound.add(target.id)
        elif isinstance(sub, ast.withitem) and sub.optional_vars is not None:
            for target in ast.walk(sub.optional_vars):
                if isinstance(target, ast.Name):
                    bound.add(target.id)
    return bound


@pytest.mark.parametrize("module_name", AUDITED)
def test_every_referenced_name_resolves(module_name):
    module = importlib.import_module(module_name)
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_scope = set(vars(module)) | set(dir(builtins))

    unresolved: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _bound_names(node)
        for sub in ast.walk(node):
            if not (isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load)):
                continue
            if sub.id in local or sub.id in module_scope:
                continue
            unresolved.append(f"{module_name}:{sub.lineno} {node.name}() -> {sub.id!r}")

    assert unresolved == [], "names referenced but never bound:\n" + "\n".join(unresolved)
