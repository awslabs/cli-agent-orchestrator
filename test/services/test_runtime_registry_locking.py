"""The runtime registry's shared state is only touched under its lock.

The channel loop mutates the registry while worker threads (status polling,
synchronous senders, reconcile writers) read it. Every access to the shared maps
therefore happens inside ``with self._lock`` or in a helper whose name ends in
``_locked`` (called with the lock held). ``_loop`` is exempt: it is assigned once
from the loop thread and read as a single reference.
"""

import ast
import pathlib
import threading

from cli_agent_orchestrator.runtime_channel.registry import RuntimeChannelRegistry

_REGISTRY = (
    pathlib.Path(__file__).resolve().parents[2]
    / "src"
    / "cli_agent_orchestrator"
    / "runtime_channel"
    / "registry.py"
)
_EXEMPT = {"_lock", "_loop"}


def _class_node():
    tree = ast.parse(_REGISTRY.read_text())
    return next(
        n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "RuntimeChannelRegistry"
    )


def _shared_attributes(cls):
    init = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == "__init__")
    names = set()
    for node in ast.walk(init):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
            and isinstance(node.ctx, ast.Store)
        ):
            names.add(node.attr)
    return names - _EXEMPT


def _holds_lock(node):
    if not isinstance(node, (ast.With, ast.AsyncWith)):
        return False
    return any(
        isinstance(item.context_expr, ast.Attribute)
        and item.context_expr.attr == "_lock"
        and isinstance(item.context_expr.value, ast.Name)
        and item.context_expr.value.id == "self"
        for item in node.items
    )


def _unlocked_accesses():
    cls = _class_node()
    shared = _shared_attributes(cls)
    found = []

    def walk(node, locked, method):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                continue
            inner = locked or _holds_lock(child)
            if (
                isinstance(child, ast.Attribute)
                and child.attr in shared
                and isinstance(child.value, ast.Name)
                and child.value.id == "self"
                and not inner
            ):
                found.append(f"{method}:{child.lineno} self.{child.attr}")
            walk(child, inner, method)

    for item in cls.body:
        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if item.name == "__init__" or item.name.endswith("_locked"):
                continue
            walk(item, False, item.name)
    return found


def test_every_shared_state_access_holds_the_lock():
    offenders = _unlocked_accesses()
    assert not offenders, "registry state read or written without the lock:\n  " + "\n  ".join(
        offenders
    )


def test_a_teardown_during_the_placement_read_is_not_undone(monkeypatch):
    """The sync claim re-checks under the lock after reading the row.

    A reconcile writer claims from a worker thread while the channel loop can
    delete the terminal. A delete that lands during the placement read tombstones
    the id; binding afterwards would resurrect routing for a deleted terminal.
    """
    registry = RuntimeChannelRegistry()

    def placement_read_racing_a_teardown(terminal_id):
        registry.unbind_terminal(terminal_id, deleted=True)
        return ("absent", None)

    monkeypatch.setattr(registry, "_placement_state", placement_read_racing_a_teardown)

    result = {}
    worker = threading.Thread(
        target=lambda: result.update(ok=registry.claim_terminal("t1", "rt-1"))
    )
    worker.start()
    worker.join()

    assert result["ok"] is False
    assert registry.runtime_for_terminal.__self__ is registry
    with registry._lock:
        assert "t1" not in registry._terminal_runtime
