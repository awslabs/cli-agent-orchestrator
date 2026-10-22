"""Guard test: a create the server answers only after init must be sized from init.

Issue #710. ``POST /sessions`` and ``POST /sessions/{name}/terminals`` have two
latency regimes. They answer immediately when the caller tells the server to
defer (``initial_message`` in the body on the former, ``defer_init=true`` on the
latter); otherwise the server holds the response until ``provider.initialize()``
returns. The budget for the second regime is a function of
``server.provider_init_timeout`` -- the value the SERVER is itself waiting out --
and of nothing else.

The defect class is a seam between two independent knobs: a client deadline
sized from ``mcp_request_timeout`` (or from a hand-written literal) for a wait
bounded by ``provider_init_timeout``. Raising the real knob then moves no client
deadline, and a successful create is reported to the caller as a transport
failure while the server goes on building the worker.

This test enumerates every create call site in the package and requires each to
declare which regime it is in. New call sites are caught by construction: a
create that neither defers nor derives its budget from
:mod:`cli_agent_orchestrator.utils.init_allowance` fails here rather than in the
field.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, List, Tuple

import cli_agent_orchestrator
from cli_agent_orchestrator.utils.init_allowance import init_allowance

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent

# URL shapes of the two routes that initialize synchronously. An f-string is
# reduced to a shape (every interpolation becomes ``{}``), so
# ``f"{API_BASE_URL}/sessions"`` and the bare path ``"/sessions"`` both match
# while ``DELETE /sessions/{name}`` ("{}/sessions/{}") does not.
_CREATE_ROUTE = re.compile(r"/sessions$|/sessions/\{\}/terminals$")

# The route shape alone also appears in read paths, so only functions that
# actually POST are in scope. ``"post"`` covers helpers that take the verb as an
# argument (``ops_mcp_server._request_json``).
_POST_MARKERS = (".post(", "_post_json(", '"post"', "'post'")

# Telling the server to defer puts the call in the fast regime (api/main.py:
# ``POST /sessions`` defers iff ``initial_message`` is present). Recognised as a
# request field -- a dict key, a query-param name or a keyword argument -- never
# as a bare local of the same name, and only when it is UNCONDITIONAL. A function
# that sets it on one branch and not on another has both regimes behind one POST,
# which is the exact shape of #710: ``cao launch`` deferred when ``--headless``
# and initialized synchronously otherwise, from one ``mcp_request_timeout``.
_DEFER_MARKERS = ("initial_message", "defer_init")

# The only sanctioned way to size the slow regime. Deliberately NOT
# ``init_allowance``: that is the raw second count, and ``launch.py`` already had
# a same-named local helper (``_init_allowance``, #566) sizing a DIFFERENT wait
# (the post-create delivery poll). Matching the bare name made the pre-#710
# ``launch`` -- the buggy one -- read as compliant, so the marker is the request
# helper that returns a ``requests`` timeout, not the number behind it.
_DERIVED_MARKERS = ("synchronous_create_timeout",)

# Branching statements. A defer marker reached only through one of these is
# conditional; ``with``/``for``-free straight-line code is not.
_BRANCHES = (ast.If, ast.Try, ast.While, ast.For, ast.AsyncFor, ast.Match)

# Helpers whose create POST is parameterized: they take the budget from the
# caller, so the obligation passes to every caller (checked separately below).
_DELEGATING_HELPERS = ("_create_terminal", "_create_session")


def _shape(node: ast.AST) -> str:
    """Reduce a string literal or f-string to a comparable route shape."""

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts)
    return ""


def _create_post_functions() -> Dict[str, Tuple[str, bool, Tuple[str, ...]]]:
    """Map ``<relpath>::<function>`` to (source, defers-unconditionally, parameters)."""

    found: Dict[str, Tuple[str, bool, Tuple[str, ...]]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        source = path.read_text()
        tree = ast.parse(source)
        # Literals nested in an f-string are fragments, not routes of their own.
        nested = {
            id(inner)
            for node in ast.walk(tree)
            if isinstance(node, ast.JoinedStr)
            for inner in node.values
        }
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(source, func) or ""
            if not any(marker in body for marker in _POST_MARKERS):
                continue
            routes = [
                node
                for node in ast.walk(func)
                if isinstance(node, (ast.Constant, ast.JoinedStr))
                and id(node) not in nested
                and _CREATE_ROUTE.search(_shape(node))
            ]
            if routes:
                arguments = func.args
                parameters = tuple(
                    argument.arg
                    for argument in (*arguments.args, *arguments.kwonlyargs)
                    if "timeout" in argument.arg
                )
                found[f"{path.relative_to(_PACKAGE_ROOT)}::{func.name}"] = (
                    body,
                    _defers_unconditionally(func),
                    parameters,
                )
    return found


def _defers_unconditionally(func: ast.AST) -> bool:
    """True if the function names a defer field outside every branch."""

    found = False

    def names_a_defer_field(statement: ast.stmt) -> bool:
        for node in ast.walk(statement):
            if isinstance(node, ast.keyword) and node.arg in _DEFER_MARKERS:
                return True
            if isinstance(node, ast.Constant) and node.value in _DEFER_MARKERS:
                return True
        return False

    def walk(statements: List[ast.stmt]) -> None:
        nonlocal found
        for statement in statements:
            if isinstance(statement, _BRANCHES):
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                walk(statement.body)
                continue
            if names_a_defer_field(statement):
                found = True

    walk(getattr(func, "body", []))
    return found


def _delegating_calls() -> Dict[str, List[ast.Call]]:
    """Map ``<relpath>::<function>`` to its calls into a create helper."""

    found: Dict[str, List[ast.Call]] = {}
    for path in sorted(_PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if func.name in _DELEGATING_HELPERS:
                continue
            calls = [
                node
                for node in ast.walk(func)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in _DELEGATING_HELPERS
            ]
            if calls:
                found[f"{path.relative_to(_PACKAGE_ROOT)}::{func.name}"] = calls
    return found


def _classify_call(key: str, call: ast.Call) -> str:
    """Return the regime one create-helper call declares, or ``"unsized"``."""

    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    budget = keywords.get("create_timeout")
    if budget is not None and any(marker in ast.dump(budget) for marker in _DERIVED_MARKERS):
        return "derived"
    defer = keywords.get("defer_init")
    if isinstance(defer, ast.Constant) and defer.value is True:
        return "deferred"
    return "unsized"


def test_every_synchronous_create_site_sizes_its_wait_from_provider_init_timeout():
    """No create POST may be left on a budget unrelated to server-side init."""

    unsized: List[str] = []
    for key, (body, defers, parameters) in _create_post_functions().items():
        if any(marker in body for marker in _DERIVED_MARKERS):
            continue
        if any(f"{name} if" in body or f"{name} is not None" in body for name in parameters):
            continue  # delegating helper: the obligation is on its callers
        if defers:
            continue
        unsized.append(key)
    assert not unsized, (
        "create call site(s) neither defer init nor size their wait from "
        "provider_init_timeout (issue #710). Pass "
        "``synchronous_create_timeout(...)`` from "
        "``cli_agent_orchestrator.utils.init_allowance``, or tell the server to "
        f"defer unconditionally: {sorted(unsized)}"
    )


def test_every_caller_of_a_delegating_create_helper_sizes_its_wait():
    """``_create_terminal``/``_create_session`` take the budget from the caller."""

    callers = _delegating_calls()
    assert callers, "enumeration found no callers of the create helpers -- scanner is blind"
    unsized = sorted(
        key
        for key, calls in callers.items()
        if any(_classify_call(key, call) == "unsized" for call in calls)
    )
    assert not unsized, (
        "caller(s) of a synchronous create helper pass no init-derived "
        f"create_timeout and do not defer (issue #710): {unsized}"
    )


def test_the_enumeration_still_sees_the_known_create_sites():
    """Anti-vacuity: a refactor must not quietly empty the scan."""

    sites = set(_create_post_functions()) | set(_delegating_calls())
    for expected in (
        "cli/commands/launch.py::launch",
        "mcp_server/app_tools.py::_route_command",
        "utils/orchestration.py::_create_terminal",
        "utils/orchestration.py::_handoff_impl",
        "utils/orchestration.py::_assign_impl",
        "ops_mcp_server/server.py::_launch_session_impl",
    ):
        assert expected in sites, (
            f"{expected} is no longer enumerated as a create call site; the guard "
            "for issue #710 has gone blind -- update the scanner, not this list"
        )


def test_the_init_allowance_is_not_the_mcp_request_budget():
    """The two knobs are independent, and the init one is always the larger."""

    stock = {"provider_init_timeout": 60, "mcp_request_timeout": 30}
    assert init_allowance(None, stock) > stock["mcp_request_timeout"]
    # Raising the server's init budget must move the client's allowance with it.
    raised = {**stock, "provider_init_timeout": 600}
    assert init_allowance(None, raised) > init_allowance(None, stock)
