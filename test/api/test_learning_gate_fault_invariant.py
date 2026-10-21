"""Invariant guard: no learning-gated route reports a fault as a policy answer (#744).

THE CLASS, not the instance. ``settings_service`` deliberately keeps a lossy
and a lossless reader of the same state side by side: ``is_learning_enabled()``
answers True/False, while ``learning_status()`` also carries ``unreadable`` —
"I could not read the policy", which is not "the policy says no". #744 was a
surface that consumed the lossy one, so an unreadable ``settings.json`` reached
the caller as a deliberate opt-out; ``skills/cao-learning`` then tells the agent
to drop the lesson SILENTLY, and the fault left no trace anywhere.

Per-route tests cannot close this, and that is why it slipped: each one patches
``is_learning_enabled`` to a bool, which makes the third state unreachable by
construction. So these tests enumerate the surface instead of sampling it:

* every route gated on learning must be in ``LEARNING_GATED_ROUTES`` (AST);
* every route in that table must answer 503 to a REAL unreadable settings file
  — no boolean is patched, the file is genuinely unreadable;
* no route handler may consult the lossy predicate directly and skip the gate;
* the capability check may not re-widen its ``except`` back over ``RuntimeError``.

Together those are closed: a new learning-gated route either goes through
``_require_learning_enabled`` (and the first two tests bind it) or reads the
flag itself (and the third fails it).
"""

import ast
import os
from pathlib import Path
from unittest.mock import patch

import pytest

import cli_agent_orchestrator.api.main as main_mod
from cli_agent_orchestrator.services.outcome_service import LEARNING_DISABLED_CODE

MAIN_PY = Path(main_mod.__file__)

# (method, path, body) for every route gated on workflow self-learning.
# Kept explicit so adding a gated route is a deliberate edit, not a silent one;
# `test_every_learning_gated_route_is_in_the_table` enforces that.
LEARNING_GATED_ROUTES = [
    ("POST", "/outcomes", {"session_name": "s", "task_label": "t", "success": True}),
    ("GET", "/outcomes", None),
    (
        "POST",
        "/lessons",
        {
            "target_agent_profile": "transformer",
            "content": "A lesson.",
            "source_terminal_id": "term-1",
        },
    ),
]

# A handler may read the lossy predicate only if it also reads a fault-carrying
# companion in the same body — `GET /settings/memory` reports `learning_enabled`
# as DATA next to `settings_readable()`, so the fault is not hidden behind the
# boolean. This is an earned exemption rather than a name allowlist: a new
# handler cannot join it without also surfacing the unreadable state.
FAULT_CARRYING_READERS = {"settings_readable", "learning_status"}

_VERBS = {"get", "post", "put", "patch", "delete"}


def _route_handlers():
    """Yield (method, path, FunctionDef) for every @app.<verb>("<path>") handler."""
    tree = ast.parse(MAIN_PY.read_text())
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            func = dec.func if isinstance(dec, ast.Call) else dec
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id == "app"
                and func.attr in _VERBS
                and isinstance(dec, ast.Call)
                and dec.args
                and isinstance(dec.args[0], ast.Constant)
            ):
                yield func.attr.upper(), dec.args[0].value, node


def _calls(node) -> set:
    return {
        n.func.id
        for n in ast.walk(node)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
    }


def test_every_learning_gated_route_is_in_the_table():
    """A new gated route must be added to the table, where the 503 test binds it."""
    gated = {
        (method, path)
        for method, path, fn in _route_handlers()
        if "_require_learning_enabled" in _calls(fn)
    }
    assert gated == {(m, p) for m, p, _ in LEARNING_GATED_ROUTES}


def test_no_route_handler_gates_on_the_lossy_predicate():
    """Reading `is_learning_enabled()` in a handler means the 503 branch is gone."""
    offenders = []
    for method, path, fn in _route_handlers():
        calls = _calls(fn)
        if "is_learning_enabled" in calls and not (calls & FAULT_CARRYING_READERS):
            offenders.append(f"{method} {path} ({fn.name})")
    assert not offenders, (
        "these handlers decide the learning gate from the two-valued predicate, so an "
        f"unreadable settings.json reaches the caller as a deliberate opt-out: {sorted(offenders)}."
        " Call _require_learning_enabled(), or report settings_readable() alongside."
    )


@pytest.mark.parametrize(
    "method,path,body", LEARNING_GATED_ROUTES, ids=[f"{m} {p}" for m, p, _ in LEARNING_GATED_ROUTES]
)
def test_unreadable_settings_is_503_on_every_gated_route(client, tmp_path, method, path, body):
    """A genuinely unreadable settings.json — no predicate patched anywhere."""
    # A directory at the settings path: read_text() raises IsADirectoryError, a
    # real OSError off the real filesystem, exactly as a permissions fault does.
    unreadable = tmp_path / "settings.json"
    unreadable.mkdir()
    env = {k: v for k, v in os.environ.items() if k != "CAO_MEMORY_LEARNING_ENABLED"}

    with (
        patch("cli_agent_orchestrator.services.settings_service.SETTINGS_FILE", unreadable),
        patch.dict(os.environ, env, clear=True),
    ):
        response = client.request(method, path, json=body)

    detail = str(response.json().get("detail", ""))
    assert response.status_code == 503, f"{method} {path} answered {response.status_code}: {detail}"
    assert LEARNING_DISABLED_CODE not in detail, (
        f"{method} {path} returned the learning-disabled discriminator for a filesystem "
        "fault; the MCP layer reads that code as the gate and skips SILENTLY"
    )


def test_capability_check_does_not_swallow_store_read_faults():
    """`profile_declares_capability` is an authz predicate: the same class applies.

    Catching ``RuntimeError`` (what ``load_agent_profile`` wraps I/O and
    permission faults in) would answer "capability not declared" — a 403 — for
    "I could not read the profile store". Only the genuinely-known negatives
    may be caught.
    """
    import cli_agent_orchestrator.utils.agent_profiles as profiles_mod

    tree = ast.parse(Path(profiles_mod.__file__).read_text())
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "profile_declares_capability"
    )
    caught = set()
    for handler in (n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler)):
        if handler.type is None:
            caught.add("bare except")
        for name in ast.walk(handler.type) if handler.type else []:
            if isinstance(name, ast.Name):
                caught.add(name.id)
    assert caught == {"FileNotFoundError", "ValueError"}, (
        f"profile_declares_capability catches {sorted(caught)}; anything beyond a missing or "
        "unparseable profile turns an unknown answer into a confident 403"
    )


# The 503 branch above only protects callers that actually reach the route. The
# other half of #744 was the gate being resolved in the AGENT's process, where
# there is no 503 to return and no operator to read it. These names are the
# server-side-only machinery; a learning tool referencing any of them has moved
# the decision back across the boundary.
SERVER_SIDE_ONLY = {
    "is_learning_enabled",
    "learning_status",
    "MemoryService",
    "OutcomeService",
    "load_agent_profile",
    "profile_declares_capability",
}
LEARNING_MCP_TOOLS = {"report_outcome", "list_outcomes", "store_lesson"}


def test_learning_mcp_tools_do_not_resolve_the_gate_in_the_agent_process():
    """The agent process must ask cao-server, not the filesystem (#734, #744)."""
    import cli_agent_orchestrator.mcp_server.server as server_mod

    tree = ast.parse(Path(server_mod.__file__).read_text())
    offenders = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in LEARNING_MCP_TOOLS
        ):
            # Names, not just calls: the pre-#744 code hid the import INSIDE the
            # function body, so a module-level import lint would never see it.
            used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
                a.name.split(".")[-1]
                for n in ast.walk(node)
                if isinstance(n, (ast.Import, ast.ImportFrom))
                for a in n.names
            }
            if used & SERVER_SIDE_ONLY:
                offenders[node.name] = sorted(used & SERVER_SIDE_ONLY)
    assert not offenders, (
        f"learning MCP tools reference server-side-only machinery: {offenders}. In the agent "
        "process an unreadable settings.json is indistinguishable from an operator opt-out, and "
        "skills/cao-learning tells the agent to skip SILENTLY on the latter"
    )
