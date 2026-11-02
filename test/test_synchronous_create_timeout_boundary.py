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
declare which regime it is in: derive its budget from
:func:`cli_agent_orchestrator.utils.init_allowance.synchronous_create_timeout`,
defer unconditionally, or hand the budget to its caller (a parameterized helper,
whose callers are then checked one call at a time). There is no allowlist.

What the scan can and cannot see
--------------------------------
The scan is an AST walk over KNOWN SHAPES, not a behavior test. It recognises:

- a POST as a call whose callee name contains ``post`` (``requests.post``,
  ``_post_json``) or any call carrying a string argument equal to ``post`` in
  any case (``requests.request("POST", ...)``, ``_request_json("post", ...)``);
- a create route as a string or f-string whose shape (interpolations reduced to
  ``{}``, any query string dropped) ends in ``/sessions`` or
  ``/sessions/{}/terminals``, including when the string is a module-level
  constant or a concatenation of them;
- the derived budget as a *call* to ``synchronous_create_timeout`` in the value
  of a ``timeout=``/``create_timeout=`` keyword or a ``"timeout"`` dict key,
  directly or through one local name bound from such a call. A comment, a
  docstring or an unrelated local that names the helper counts for nothing;
- an unconditional defer as a request field -- a dict key, a subscript store or
  a keyword -- named ``initial_message`` or ``defer_init``, reached outside every
  branch. An argument to ``.pop``/``.get`` is not a request field;
- a delegating create helper as ``_create_terminal`` called by bare name or as
  an attribute.

Anything else -- a route built by ``urljoin``, a budget threaded through a
dataclass, a create issued by a helper this file does not name -- is invisible
to it. ``test_the_scanner_catches_the_known_evasions`` pins the shapes above on
a scratch module so a scanner regression is caught here rather than discovered
in a review.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, NamedTuple, Optional, Set

import cli_agent_orchestrator
from cli_agent_orchestrator.utils.init_allowance import init_allowance

_PACKAGE_ROOT = Path(cli_agent_orchestrator.__file__).parent

# URL shapes of the two routes that initialize synchronously. An f-string is
# reduced to a shape (every interpolation becomes ``{}``), so
# ``f"{API_BASE_URL}/sessions"`` and the bare path ``"/sessions"`` both match
# while ``DELETE /sessions/{name}`` ("{}/sessions/{}") does not.
_CREATE_ROUTE = re.compile(r"/sessions$|/sessions/\{\}/terminals$")

# Telling the server to defer puts the call in the fast regime (api/main.py:
# ``POST /sessions`` defers iff ``initial_message`` is present). Recognised as a
# request field -- a dict key, a subscript store or a keyword argument -- never
# as a bare local of the same name or as an argument to ``.pop``/``.get``, and
# only when it is UNCONDITIONAL. A function that sets it on one branch and not
# on another has both regimes behind one POST, which is the exact shape of
# #710: ``cao launch`` deferred when ``--headless`` and initialized synchronously
# otherwise, from one ``mcp_request_timeout``.
_DEFER_FIELDS = ("initial_message", "defer_init")

# The only sanctioned way to size the slow regime. Deliberately NOT
# ``init_allowance``: that is the raw second count, and ``launch.py`` already had
# a same-named local helper (``_init_allowance``, #566) sizing a DIFFERENT wait
# (the post-create delivery poll). Matching the bare name made the pre-#710
# ``launch`` -- the buggy one -- read as compliant, so the marker is the request
# helper that returns a ``requests`` timeout, not the number behind it.
_DERIVED_HELPER = "synchronous_create_timeout"

# Keyword names (and dict keys) that carry a request budget.
_BUDGET_KEYS = ("timeout", "create_timeout")

# Branching statements. A defer field reached only through one of these is
# conditional; ``with``-wrapped straight-line code is not.
_BRANCHES = (ast.If, ast.Try, ast.While, ast.For, ast.AsyncFor, ast.Match)

# Helpers whose create POST is parameterized: they take the budget from the
# caller, so the obligation passes to every caller (checked per call below).
_DELEGATING_HELPERS = ("_create_terminal",)


class _Site(NamedTuple):
    derived: bool
    delegating: bool
    defers: bool


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _callee_name(func: ast.AST) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _module_strings(tree: ast.Module) -> Dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a route kept in a constant is seen."""

    found: Dict[str, str] = {}
    for statement in tree.body:
        targets: List[ast.expr] = []
        value: Optional[ast.expr] = None
        if isinstance(statement, ast.Assign):
            targets, value = statement.targets, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
            targets, value = [statement.target], statement.value
        if value is None:
            continue
        shape = _shape(value, {})
        if not shape:
            continue
        for target in targets:
            if isinstance(target, ast.Name):
                found[target.id] = shape
    return found


def _shape(node: ast.AST, module_strings: Dict[str, str]) -> str:
    """Reduce a string, f-string, module constant or concatenation to a route shape.

    A query string is dropped: ``/sessions?trace=1`` is still a create route.
    """

    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.split("?", 1)[0]
    if isinstance(node, ast.JoinedStr):
        parts: List[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            else:
                parts.append("{}")
        return "".join(parts).split("?", 1)[0]
    if isinstance(node, ast.Name):
        return module_strings.get(node.id, "")
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _shape(node.left, module_strings)
        right = _shape(node.right, module_strings)
        if left or right:
            return ((left or "{}") + (right or "{}")).split("?", 1)[0]
    return ""


def _is_post_call(call: ast.Call) -> bool:
    if "post" in _callee_name(call.func).lower():
        return True
    arguments: Iterable[ast.AST] = (*call.args, *(kw.value for kw in call.keywords))
    return any(
        isinstance(argument, ast.Constant)
        and isinstance(argument.value, str)
        and argument.value.lower() == "post"
        for argument in arguments
    )


def _body_nodes(func: ast.AST) -> Iterator[ast.AST]:
    """Every node in the function's BODY -- not its decorators or signature.

    ``ast.walk(func)`` would include ``@app.post("/sessions")``, which is the
    server's own route handler, not a client making a create call.
    """

    for statement in getattr(func, "body", []):
        yield from ast.walk(statement)


def _names_a_create_route(func: ast.AST, module_strings: Dict[str, str]) -> bool:
    nested = {
        id(inner)
        for node in _body_nodes(func)
        if isinstance(node, ast.JoinedStr)
        for inner in node.values
    }
    for node in _body_nodes(func):
        if id(node) in nested:
            continue
        if isinstance(node, (ast.Constant, ast.JoinedStr, ast.Name, ast.BinOp)):
            if _CREATE_ROUTE.search(_shape(node, module_strings)):
                return True
    return False


def _budget_values(func: ast.AST) -> List[ast.expr]:
    """Every expression passed as a request budget inside ``func``.

    A ``timeout=``/``create_timeout=`` keyword on any call, or the value under a
    ``"timeout"`` key of a dict literal (the ``**post_kwargs`` shape).
    """

    values: List[ast.expr] = []
    for node in ast.walk(func):
        if isinstance(node, ast.keyword) and node.arg in _BUDGET_KEYS:
            values.append(node.value)
        if isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value in _BUDGET_KEYS:
                    values.append(value)
    return values


def _derived_names(func: ast.AST) -> Set[str]:
    """Locals bound from an expression that calls the derived helper."""

    names: Set[str] = set()
    for node in ast.walk(func):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if value is None or not _calls_helper(value, set()):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
    return names


def _calls_helper(expr: ast.AST, derived_names: Set[str]) -> bool:
    for node in ast.walk(expr):
        if isinstance(node, ast.Call) and _callee_name(node.func) == _DERIVED_HELPER:
            return True
        if isinstance(node, ast.Name) and node.id in derived_names:
            return True
    return False


def _mentions_any(expr: ast.AST, names: Set[str]) -> bool:
    return any(isinstance(node, ast.Name) and node.id in names for node in ast.walk(expr))


def _defers_unconditionally(func: ast.AST) -> bool:
    """True if the function sets a defer field as a request field outside every branch."""

    def is_request_field(statement: ast.stmt) -> bool:
        for node in ast.walk(statement):
            if isinstance(node, ast.keyword) and node.arg in _DEFER_FIELDS:
                return True
            if isinstance(node, ast.Dict):
                for key in node.keys:
                    if isinstance(key, ast.Constant) and key.value in _DEFER_FIELDS:
                        return True
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Subscript)
                        and isinstance(target.slice, ast.Constant)
                        and target.slice.value in _DEFER_FIELDS
                    ):
                        return True
        return False

    def walk(statements: List[ast.stmt]) -> bool:
        for statement in statements:
            if isinstance(statement, _BRANCHES):
                continue
            if isinstance(statement, (ast.With, ast.AsyncWith)):
                if walk(statement.body):
                    return True
                continue
            if is_request_field(statement):
                return True
        return False

    return walk(getattr(func, "body", []))


def _functions(tree: ast.AST) -> Iterator[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


# ---------------------------------------------------------------------------
# Enumeration
# ---------------------------------------------------------------------------


def _create_post_functions(root: Path = _PACKAGE_ROOT) -> Dict[str, _Site]:
    """Map ``<relpath>::<function>`` to how each create-POSTing function sizes its wait."""

    found: Dict[str, _Site] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        module_strings = _module_strings(tree)
        for func in _functions(tree):
            posts = [
                node
                for node in _body_nodes(func)
                if isinstance(node, ast.Call) and _is_post_call(node)
            ]
            if not posts or not _names_a_create_route(func, module_strings):
                continue
            arguments = func.args  # type: ignore[attr-defined]
            budget_params = {
                argument.arg
                for argument in (*arguments.args, *arguments.kwonlyargs)
                if "timeout" in argument.arg
            }
            derived_names = _derived_names(func)
            budgets = _budget_values(func)
            name = func.name  # type: ignore[attr-defined]
            found[f"{path.relative_to(root)}::{name}"] = _Site(
                derived=any(_calls_helper(value, derived_names) for value in budgets),
                delegating=any(_mentions_any(value, budget_params) for value in budgets),
                defers=_defers_unconditionally(func),
            )
    return found


def _delegating_calls(root: Path = _PACKAGE_ROOT) -> Dict[str, List[str]]:
    """Map ``<relpath>::<function>`` to the regime of each of its create-helper calls."""

    found: Dict[str, List[str]] = {}
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text())
        for func in _functions(tree):
            name = func.name  # type: ignore[attr-defined]
            if name in _DELEGATING_HELPERS:
                continue
            calls = [
                node
                for node in ast.walk(func)
                if isinstance(node, ast.Call) and _callee_name(node.func) in _DELEGATING_HELPERS
            ]
            if calls:
                derived_names = _derived_names(func)
                found[f"{path.relative_to(root)}::{name}"] = [
                    _classify_call(call, derived_names) for call in calls
                ]
    return found


def _classify_call(call: ast.Call, derived_names: Set[str]) -> str:
    """Return the regime one create-helper call declares, or ``"unsized"``."""

    keywords = {kw.arg: kw.value for kw in call.keywords if kw.arg}
    budget = keywords.get("create_timeout")
    if budget is not None and _calls_helper(budget, derived_names):
        return "derived"
    defer = keywords.get("defer_init")
    if isinstance(defer, ast.Constant) and defer.value is True:
        return "deferred"
    return "unsized"


def _unsized_sites(root: Path = _PACKAGE_ROOT) -> List[str]:
    """Every create site under ``root`` that declares no regime."""

    unsized = [
        key
        for key, site in _create_post_functions(root).items()
        if not (site.derived or site.delegating or site.defers)
    ]
    unsized.extend(key for key, regimes in _delegating_calls(root).items() if "unsized" in regimes)
    return sorted(set(unsized))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_every_synchronous_create_site_sizes_its_wait_from_provider_init_timeout():
    """No create POST may be left on a budget unrelated to server-side init."""

    unsized = _unsized_sites()
    assert not unsized, (
        "create call site(s) neither defer init nor size their wait from "
        "provider_init_timeout (issue #710). Pass "
        "``synchronous_create_timeout(...)`` from "
        "``cli_agent_orchestrator.utils.init_allowance`` as the request timeout, "
        f"or tell the server to defer unconditionally: {unsized}"
    )


def test_every_caller_of_a_delegating_create_helper_sizes_its_wait():
    """``_create_terminal`` takes the budget from the caller, one call at a time."""

    callers = _delegating_calls()
    assert callers, "enumeration found no callers of the create helpers -- scanner is blind"
    unsized = sorted(key for key, regimes in callers.items() if "unsized" in regimes)
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


def test_the_enumeration_classifies_the_known_create_sites():
    """Each known site declares the regime it is actually in.

    ``launch`` and ``_launch_session_impl`` have both regimes behind one POST
    and therefore MUST derive; ``_create_terminal`` is the delegating helper;
    ``_handoff_impl`` derives per call and ``_assign_impl`` defers per call.
    """

    posts = _create_post_functions()
    assert posts["cli/commands/launch.py::launch"].derived
    assert not posts["cli/commands/launch.py::launch"].defers
    assert posts["mcp_server/app_tools.py::_route_command"].derived
    assert posts["ops_mcp_server/server.py::_launch_session_impl"].derived
    assert not posts["ops_mcp_server/server.py::_launch_session_impl"].defers
    assert posts["utils/orchestration.py::_create_terminal"].delegating

    callers = _delegating_calls()
    assert callers["utils/orchestration.py::_handoff_impl"] == ["derived"]
    assert callers["utils/orchestration.py::_assign_impl"] == ["deferred"]


_PROBE_MODULE = '''
"""Scratch module: six evasions of the #710 scanner and three controls."""
import requests
from cli_agent_orchestrator.utils import orchestration
from cli_agent_orchestrator.utils.init_allowance import synchronous_create_timeout
from cli_agent_orchestrator.utils.orchestration import _create_terminal

API_BASE_URL = "http://127.0.0.1:9889"
_CREATE_PATH = "/sessions"


def probe_comment_names_helper(params):
    # size this from synchronous_create_timeout() one day
    return requests.post(f"{API_BASE_URL}/sessions", params=params, timeout=30)


def probe_route_in_module_constant(params):
    return requests.post(API_BASE_URL + _CREATE_PATH, params=params, timeout=30)


def probe_route_with_query_string(params):
    return requests.post(f"{API_BASE_URL}/sessions?trace=1", params=params, timeout=30)


def probe_upper_case_verb(params):
    return requests.request("POST", f"{API_BASE_URL}/sessions", params=params, timeout=30)


def probe_pop_is_not_a_defer(body):
    body.pop("initial_message", None)
    return requests.post(f"{API_BASE_URL}/sessions", json=body, timeout=30)


def probe_attribute_call_to_helper(profile):
    return orchestration._create_terminal(profile, None)


def probe_helper_bound_but_not_passed(params, profile):
    budget = synchronous_create_timeout(profile)
    return requests.post(f"{API_BASE_URL}/sessions", params=params, timeout=(5, 300))


def probe_control(params, profile):
    return requests.post(
        f"{API_BASE_URL}/sessions", params=params, timeout=synchronous_create_timeout(profile)
    )


def probe_control_through_a_local(params, profile):
    create_timeout = synchronous_create_timeout(profile)
    post_kwargs = {"params": params, "timeout": create_timeout}
    return requests.post(f"{API_BASE_URL}/sessions", **post_kwargs)


def probe_helper_control(profile):
    return _create_terminal(profile, None, create_timeout=synchronous_create_timeout(profile))


def probe_defer_control(params, message):
    body = {"initial_message": message}
    return requests.post(f"{API_BASE_URL}/sessions", params=params, json=body, timeout=30)
'''


def test_the_scanner_catches_the_known_evasions(tmp_path):
    """Pins the shapes the scanner must see (review on PR #1002, P3-2).

    Each probe is a synchronous create on a fixed budget that an earlier
    version of this scanner let through: a comment naming the helper, the route
    in a module constant or with a query string, an upper-case verb, a
    ``.pop("initial_message")`` read as a defer, the create helper called as an
    attribute, and the helper called but its result not passed as the budget
    (the shape the text match could never tell apart). The four controls --
    direct, through a local and ``**kwargs``, via the create helper, and an
    unconditional defer -- must still pass.
    """

    (tmp_path / "zz_probe_sites.py").write_text(_PROBE_MODULE)

    unsized = _unsized_sites(tmp_path)

    assert unsized == [
        "zz_probe_sites.py::probe_attribute_call_to_helper",
        "zz_probe_sites.py::probe_comment_names_helper",
        "zz_probe_sites.py::probe_helper_bound_but_not_passed",
        "zz_probe_sites.py::probe_pop_is_not_a_defer",
        "zz_probe_sites.py::probe_route_in_module_constant",
        "zz_probe_sites.py::probe_route_with_query_string",
        "zz_probe_sites.py::probe_upper_case_verb",
    ]


def test_the_init_allowance_is_not_the_mcp_request_budget():
    """The two knobs are independent, and the init one is always the larger."""

    stock = {"provider_init_timeout": 60, "mcp_request_timeout": 30}
    assert init_allowance(None, stock) > stock["mcp_request_timeout"]
    # Raising the server's init budget must move the client's allowance with it.
    raised = {**stock, "provider_init_timeout": 600}
    assert init_allowance(None, raised) > init_allowance(None, stock)
