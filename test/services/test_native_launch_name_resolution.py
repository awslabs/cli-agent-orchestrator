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


def _own_scope_body(node: ast.AST):
    """Every node in this scope, not descending into nested function scopes."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        yield child
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Yielded (so its name binds) but not descended into.
            continue
        stack.extend(ast.iter_child_nodes(child))


def _bound_names(node: ast.AST) -> set[str]:
    """Every name a function binds locally: args, assignments, imports."""
    bound: set[str] = set()
    for arg_group in ("args", "posonlyargs", "kwonlyargs"):
        for arg in getattr(node.args, arg_group, []) or []:
            bound.add(arg.arg)
    for extra in (node.args.vararg, node.args.kwarg):
        if extra is not None:
            bound.add(extra.arg)
    # Deliberately NOT ast.walk: a nested function's body is a different
    # scope, and collecting its bindings here would publish them to the
    # parent -- from which every sibling inherits them. A name assigned
    # only inside sibling ``a`` would then resolve inside sibling ``b``,
    # where it does not exist, and the audit would pass over exactly the
    # unbound reference it exists to find. The nested function's NAME is
    # still bound, because that is what the parent really gets.
    for sub in _own_scope_body(node):
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


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _own_scope_nodes(node: ast.AST) -> tuple[list[ast.Name], list[ast.AST]]:
    """Split a function body into its OWN loads and its nested scopes.

    Nested functions are not descended into here. They open a new scope in
    which their parameters are bound and the enclosing function's locals
    are free, so auditing them with the outer function's binding set
    reports both as unresolved: the parameter is not an outer local, and
    the closed-over name is not an inner one. Both readings are wrong, and
    an audit that cannot be trusted on correct code stops being read.
    """
    loads: list[ast.Name] = []
    nested: list[ast.AST] = []
    stack = list(ast.iter_child_nodes(node))
    while stack:
        child = stack.pop()
        if isinstance(child, _SCOPES):
            nested.append(child)
            # Decorators and default arguments DO evaluate in this scope,
            # so they are audited here rather than with the nested body.
            for outer in list(getattr(child, "decorator_list", [])) + list(
                getattr(child.args, "defaults", []) or []
            ):
                stack.append(outer)
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            loads.append(child)
        stack.extend(ast.iter_child_nodes(child))
    return loads, nested


def _audit(node, enclosing, module_name, module_scope, unresolved, name):
    scope = enclosing | _bound_names(node)
    loads, nested = _own_scope_nodes(node)
    for load in loads:
        if load.id in scope or load.id in module_scope:
            continue
        unresolved.append(f"{module_name}:{load.lineno} {name}() -> {load.id!r}")
    for child in nested:
        _audit(
            child,
            scope,
            module_name,
            module_scope,
            unresolved,
            getattr(child, "name", "<lambda>"),
        )


@pytest.mark.parametrize("module_name", AUDITED)
def test_every_referenced_name_resolves(module_name):
    module = importlib.import_module(module_name)
    source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    module_scope = set(vars(module)) | set(dir(builtins))

    unresolved: list[str] = []
    for node in ast.walk(tree):
        # Only top-level-in-their-parent functions start an audit; nested
        # ones are reached through their enclosing scope so they inherit it.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not any(
            node in _own_scope_nodes(other)[1]
            for other in ast.walk(tree)
            if isinstance(other, _SCOPES) and other is not node
        ):
            _audit(node, set(), module_name, module_scope, unresolved, node.name)

    assert unresolved == [], "names referenced but never bound:\n" + "\n".join(unresolved)


def test_the_audit_still_catches_a_genuinely_unbound_name():
    """The fix for closures must not turn the gate off.

    A nested scope now inherits its enclosing bindings, which is exactly
    the change that could make everything resolve. This drives the audit
    over a module that closes over one real name and references one that
    was never bound anywhere, and requires it to report only the second.
    """
    source = (
        "def outer():\n"
        "    captured = 1\n"
        "    def inner(param):\n"
        "        return captured + param + never_bound\n"
        "    return inner\n"
    )
    tree = ast.parse(source)
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "outer":
            _audit(node, set(), "probe", set(dir(builtins)), unresolved, node.name)

    assert [u.split("-> ")[1] for u in unresolved] == ["'never_bound'"]


def test_a_sibling_local_does_not_resolve_in_another_sibling():
    """Bindings must not leak out of the scope that makes them.

    ``x`` is local to ``a``; ``b`` closes over nothing and references it.
    Collecting bindings with a plain walk publishes ``x`` to ``outer``,
    from which ``b`` inherits it, and the audit reports nothing -- passing
    over precisely the unbound reference it exists to find.
    """
    source = (
        "def outer():\n"
        "    def a():\n"
        "        x = 1\n"
        "        return x\n"
        "    def b():\n"
        "        return x\n"
        "    return a, b\n"
    )
    tree = ast.parse(source)
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "outer":
            _audit(node, set(), "probe", set(dir(builtins)), unresolved, node.name)

    assert [u.split("-> ")[1] for u in unresolved] == ["'x'"]


def test_a_nested_functions_own_name_still_binds_in_its_parent():
    """The parent really does get the name, so it must stay bound."""
    source = "def outer():\n    def inner():\n        return 1\n    return inner()\n"
    tree = ast.parse(source)
    unresolved: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "outer":
            _audit(node, set(), "probe", set(dir(builtins)), unresolved, node.name)

    assert unresolved == []
