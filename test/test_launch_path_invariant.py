"""Every route to a terminal launch threads a delegation ceiling or is a classified operator path.

HIGH-19 (PR #948) and each path missed after it slipped through the same gap:
creation paths were tested one by one, and a new path (the memory sidecar, the
script tier's run-step, remote placement, the CLI) could launch a terminal
without ever consulting the creator's recorded ceiling, with no test that knew
the path existed. This test closes that class the way ``test_enforcement_tables``
and ``test_bearer_scope_boundary`` close theirs: it ENUMERATES the paths from the
source with an AST walk rather than a hand-written list, and makes every one
declare itself.

Two kinds of launch site are found under ``src/``:

* in-process calls to ``terminal_service.create_terminal``,
  ``agent_step.run_agent_step`` or ``session_service.create_session``;
* HTTP posts to a launch or dispatch route: ``POST /sessions``,
  ``POST /sessions/{name}/terminals``, ``POST /terminals/run-step``, and the
  workflow run/submit/resume/replay routes, whose steps create terminals.

A site THREADS a ceiling when the key reaches THAT launch: an in-process call
passes ``caller_id``, ``delegating_terminal_id`` or ``caller_ceiling`` as a
keyword, or an HTTP post's payload (its ``json=``/``params=``/``data=``
argument) carries one of those keys. The payload is followed by data flow
inside the enclosing function: a dict literal with the key, a ``payload[key] =``
store, ``payload.update({key: ...})``, or the payload handed to a same-module
helper that does one of those to its parameter (``_attach_terminal_caller``).
Naming a key elsewhere in the function does NOT count: ``caller_id`` is also the
callback-routing field (#284), so a presence check would classify a new launch
inside such a function as bounded without the key ever being sent (the first
review of PR #948 showed exactly that gap). Every other site must appear in
``OPERATOR_PATHS`` below with a one-line justification saying why no ceiling
applies (an operator, not an agent, is the authority there). A launch path added
later fails this test until it threads a ceiling or is classified here, and a
classified path that disappears fails it too, so the allowlist cannot go stale.

This is a structural guard over the SHAPE of every site; the per-path mutation
tests (``test_delegation_forwarding_invariant.py`` and the kwarg-drop tests in
the workflow, handoff and script-tier suites) carry the behavioural guarantee
that a forwarded key actually causes the refusal.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import Dict, Optional, Set, Tuple

SRC = Path(__file__).resolve().parents[1] / "src"
PACKAGES = (SRC / "cli_agent_orchestrator", SRC / "cao_workflow")

LAUNCH_FUNCTIONS = {"create_terminal", "run_agent_step", "create_session"}
CEILING_KEYS = {"caller_id", "delegating_terminal_id", "caller_ceiling"}
HTTP_POST_HELPERS = {"post", "_post_json", "_post"}
PAYLOAD_KEYWORDS = {"json", "params", "data"}
ROUTE_CONSTANTS = {
    "_RUN_STEP_PATH": "/terminals/run-step",
    "TERMINALS_RUN_STEP_ROUTE": "/terminals/run-step",
}
LAUNCH_ROUTE = re.compile(
    r"(/sessions(/\{[^}]*\}/terminals)?|/terminals/run-step"
    r"|/workflows/runs(:submit|/\{[^}]*\}/resume|/\{[^}]*\}/steps/\{[^}]*\}:replay)?)$"
)
# Attribute bases that are backends or clients, whose ``create_session`` is a
# tmux/herdr primitive and whose ``create_terminal`` is the database row writer.
NOT_A_LAUNCHER = re.compile(r"(backend|client|tmux|database|db|server)$", re.IGNORECASE)

#: Operator paths: launches where no agent ceiling exists to thread, by design.
#: Key = ``<path under src/>::<enclosing function>``. Keep the justification to
#: one line; the test asserts every key is still a real launch site.
OPERATOR_PATHS: Dict[str, str] = {
    "cli_agent_orchestrator/services/flow_service.py::execute_flow": (
        "scheduled flows are operator-authored and started only through /flows "
        "(HTTP or cron); no MCP tool reaches them"
    ),
    "cli_agent_orchestrator/cli/commands/launch.py::launch": (
        "`cao launch` is the operator's shell; no CAO_TERMINAL_ID, the profile's "
        "declared ceiling is what the new terminal records"
    ),
    "cli_agent_orchestrator/ops_mcp_server/server.py::_launch_session_impl": (
        "ops MCP runs in the operator's own assistant outside any CAO terminal "
        "(docs: manage sessions from outside a CAO session)"
    ),
    "cli_agent_orchestrator/mcp_server/app_tools.py::_route_command": (
        "MCP Apps launch action is driven from the operator-facing widget; it "
        "posts an operator-shaped POST /sessions"
    ),
    "cli_agent_orchestrator/api/main.py::create_terminal_in_session_as_operator": (
        "the operator form of the in-session create (Web UI Add Agent): a distinct "
        "sub-path that refuses any caller identity (400) and records the profile's "
        "declared ceiling like POST /sessions; the sibling route keeps requiring "
        "caller_id (test_delegation_api.py pins both)"
    ),
    "cli_agent_orchestrator/utils/orchestration.py::_run_step_and_build_result": (
        "posts the payload _handoff_impl builds, which carries caller_id locally "
        "and caller_ceiling for target_host (test_handoff.py, test_remote_target.py, "
        "test_delegation_remote.py pin both)"
    ),
    "cao_workflow/__init__.py::_execute_step": (
        "script SDK: the server binds the step by the generation-fenced "
        "ScriptRunRecord (step_delegating_terminal_id) and refuses an orphan; "
        "the script asserts nothing (test_delegation_script_tier.py)"
    ),
}

#: Sites that MUST be found, so a broken walker cannot pass vacuously.
SENTINELS = {
    "cli_agent_orchestrator/services/agent_step.py::run_agent_step",
    "cli_agent_orchestrator/api/main.py::create_terminal_in_session",
    "cli_agent_orchestrator/api/main.py::create_terminal_in_session_as_operator",
    "cli_agent_orchestrator/services/workflow_service.py::_run_step",
    "cli_agent_orchestrator/utils/orchestration.py::_assign_remote",
}


def _module_string_constants(tree: ast.Module) -> Dict[str, object]:
    consts: Dict[str, object] = dict(ROUTE_CONSTANTS)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        consts[target.id] = node.value.value
    return consts


def _local_url_assignments(function: ast.AST) -> Dict[str, object]:
    """``name = <string or f-string>`` assignments inside one function."""
    found: Dict[str, object] = {}
    for node in ast.walk(function):
        if isinstance(node, ast.Assign) and isinstance(node.value, (ast.Constant, ast.JoinedStr)):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
    return found


def _render_url(node: ast.AST, consts: Dict[str, object]) -> str:
    """Flatten a URL expression to text, keeping ``{...}`` for interpolations.

    ``consts`` holds module-level string constants and, for the function being
    examined, local ``name = "..."``/f-string assignments (``cao launch`` builds
    its URL into a local first), so a URL passed by name still renders.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        out = []
        for part in node.values:
            if isinstance(part, ast.Constant):
                out.append(str(part.value))
            elif isinstance(part, ast.FormattedValue):
                inner = part.value
                if isinstance(inner, ast.Name) and inner.id in consts:
                    out.append(consts[inner.id])
                else:
                    out.append("{" + ast.unparse(inner) + "}")
        return "".join(out)
    if isinstance(node, ast.Name) and node.id in consts:
        rendered = consts[node.id]
        return rendered if isinstance(rendered, str) else _render_url(rendered, consts)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _render_url(node.left, consts) + _render_url(node.right, consts)
    return "{" + ast.unparse(node) + "}"


def _callee(call: ast.Call) -> Tuple[str, Optional[ast.AST]]:
    func = call.func
    if isinstance(func, ast.Name):
        return func.id, None
    if isinstance(func, ast.Attribute):
        return func.attr, func.value
    return "", None


def _is_launch_call(call: ast.Call) -> bool:
    name, base = _callee(call)
    if name not in LAUNCH_FUNCTIONS:
        return False
    if base is None:
        return True
    if isinstance(base, ast.Call):  # get_backend().create_session(...)
        return False
    text = ast.unparse(base).rsplit(".", 1)[-1]
    return not NOT_A_LAUNCHER.search(text)


def _http_launch_url(call: ast.Call, consts: Dict[str, object]) -> Optional[str]:
    name, base = _callee(call)
    url: Optional[ast.AST] = None
    if name in HTTP_POST_HELPERS and call.args:
        if name == "post" and base is not None:
            base_text = ast.unparse(base).rsplit(".", 1)[-1]
            if base_text not in ("requests", "api_http"):
                return None
        url = call.args[0]
    elif name == "_request_json" and len(call.args) >= 2:
        verb = call.args[0]
        if not (isinstance(verb, ast.Constant) and verb.value == "post"):
            return None
        url = call.args[1]
    if url is None:
        return None
    rendered = _render_url(url, consts)
    path = rendered.split("}", 1)[-1] if rendered.startswith("{") else rendered
    return rendered if LAUNCH_ROUTE.search(path) else None


def _dict_ceiling_keys(node: ast.AST) -> Set[str]:
    """Ceiling keys among the string keys of a dict literal (``{}`` or ``dict(...)``)."""
    if isinstance(node, ast.Dict):
        return {
            k.value
            for k in node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str) and k.value in CEILING_KEYS
        }
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "dict":
        return {kw.arg for kw in node.keywords if kw.arg in CEILING_KEYS}
    return set()


def _payload_names(expr: ast.AST) -> Set[str]:
    """Variable names an argument expression reads (``payload``, ``body or None``)."""
    return {n.id for n in ast.walk(expr) if isinstance(n, ast.Name)}


def _writes_ceiling_key_into(function: ast.AST, names: Set[str]) -> bool:
    """Does ``function`` put a ceiling key into any variable in ``names``?

    ``name = {key: ...}``, ``name[key] = ...``, ``name.update({key: ...})`` and
    ``name.setdefault(key, ...)`` all count; naming the key anywhere else does not.
    """
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id in names:
                    if node.value is not None and _dict_ceiling_keys(node.value):
                        return True
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id in names
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value in CEILING_KEYS
                ):
                    return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            base = node.func.value
            if isinstance(base, ast.Name) and base.id in names:
                if node.func.attr == "update" and node.args and _dict_ceiling_keys(node.args[0]):
                    return True
                if (
                    node.func.attr == "setdefault"
                    and node.args
                    and isinstance(node.args[0], ast.Constant)
                    and node.args[0].value in CEILING_KEYS
                ):
                    return True
    return False


def _http_payload_threads(
    call: ast.Call, function: Optional[ast.AST], functions: Dict[str, ast.AST]
) -> bool:
    """Whether a ceiling key flows into THIS post call's payload.

    Inline: the ``json=``/``params=``/``data=`` argument is a dict literal with the
    key. By name: the argument reads variables that the enclosing function writes
    the key into (``_writes_ceiling_key_into``), or that it passes positionally to
    a same-module helper which writes the key into the matching parameter.
    """
    payload_args = [kw.value for kw in call.keywords if kw.arg in PAYLOAD_KEYWORDS]
    if any(_dict_ceiling_keys(arg) for arg in payload_args):
        return True
    if function is None:
        return False
    names: Set[str] = set()
    for arg in payload_args:
        names |= _payload_names(arg)
    if not names:
        return False
    if _writes_ceiling_key_into(function, names):
        return True
    for node in ast.walk(function):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        helper = functions.get(node.func.id)
        if helper is None:
            continue
        params = [a.arg for a in helper.args.args]
        for position, arg in enumerate(node.args):
            if isinstance(arg, ast.Name) and arg.id in names and position < len(params):
                if _writes_ceiling_key_into(helper, {params[position]}):
                    return True
    return False


def _functions(tree: ast.Module) -> Dict[str, ast.AST]:
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _enclosing(tree: ast.Module) -> Dict[int, str]:
    """Map every call node id to its nearest enclosing function name."""
    owner: Dict[int, str] = {}

    def visit(node: ast.AST, current: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            current = node.name
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Call):
                owner[id(child)] = current
            visit(child, current)

    visit(tree, "<module>")
    return owner


def launch_sites() -> Dict[str, dict]:
    """Every launch site under src/, keyed ``<path>::<function>``."""
    sites: Dict[str, dict] = {}
    for package in PACKAGES:
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            consts = _module_string_constants(tree)
            functions = _functions(tree)
            owner = _enclosing(tree)
            rel = str(path.relative_to(SRC))
            for call in (n for n in ast.walk(tree) if isinstance(n, ast.Call)):
                kind: Optional[str] = None
                detail = ""
                function_name = owner.get(id(call), "<module>")
                scope = dict(consts)
                if function_name in functions:
                    scope.update(_local_url_assignments(functions[function_name]))
                if _is_launch_call(call):
                    kind, detail = "call", _callee(call)[0]
                else:
                    url = _http_launch_url(call, scope)
                    if url:
                        kind, detail = "http", url
                if kind is None:
                    continue
                function = owner.get(id(call), "<module>")
                if kind == "call" and function == detail:
                    # A launcher wrapping itself (create_terminal's guarded entry
                    # delegating to its body) is not a new path.
                    continue
                key = f"{rel}::{function}"
                if kind == "call":
                    call_kwargs = {kw.arg for kw in call.keywords if kw.arg}
                    threads = bool(call_kwargs & CEILING_KEYS)
                else:
                    threads = _http_payload_threads(call, functions.get(function), functions)
                entry = sites.setdefault(
                    key, {"kinds": set(), "details": set(), "threads": False, "line": call.lineno}
                )
                entry["kinds"].add(kind)
                entry["details"].add(detail)
                entry["threads"] = entry["threads"] or threads
    return sites


SITES = launch_sites()


def test_the_walker_finds_the_known_sites():
    missing = SENTINELS - set(SITES)
    assert not missing, f"walker lost known launch sites: {sorted(missing)}"
    assert len(SITES) >= 15, sorted(SITES)


def test_every_launch_path_threads_a_ceiling_or_is_a_classified_operator_path():
    unclassified = sorted(
        f"{key} (line {info['line']}, {'/'.join(sorted(info['kinds']))}: "
        f"{', '.join(sorted(info['details']))})"
        for key, info in SITES.items()
        if not info["threads"] and key not in OPERATOR_PATHS
    )
    assert not unclassified, (
        "launch path(s) that neither thread a delegation ceiling (caller_id / "
        "delegating_terminal_id / caller_ceiling) nor are classified as operator "
        "paths in OPERATOR_PATHS:\n  " + "\n  ".join(unclassified)
    )


def test_operator_paths_are_real_and_still_unbounded():
    stale = sorted(key for key in OPERATOR_PATHS if key not in SITES)
    assert not stale, f"OPERATOR_PATHS names launch sites that no longer exist: {stale}"
    now_threading = sorted(key for key in OPERATOR_PATHS if SITES[key]["threads"])
    assert not now_threading, (
        "these operator paths now thread a ceiling; remove them from OPERATOR_PATHS "
        f"so the classification stays honest: {now_threading}"
    )
    for key, why in OPERATOR_PATHS.items():
        assert len(why.split()) >= 6, f"{key}: justification too short"
