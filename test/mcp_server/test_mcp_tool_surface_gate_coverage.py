"""Every tool on every MCP surface is gated, or the gap is declared here (#671).

The CLASS of defect #671 belongs to: **an authorization policy enforced per
registration, so a newly registered tool is ungated by default and the policy
silently stops covering the surface as it grows.** #769 and #811 added
``_tool_denied_reason`` at 7 call sites; the surface was 30 tools; nothing
noticed the other 23, because nothing in the suite ever asked the *registry*
what it contained.

The repo already solved this class once, on its other authorization surface:
``test/api/test_scope_coverage.py::test_every_mutating_route_is_scope_gated``
enumerates the live FastAPI route table and fails if any mutating route lacks a
``require_any_scope`` dependency, with an ``_EXEMPT`` set that makes each
exception a reviewable line of code. That guard was never applied to the MCP
tool surface. This file is that guard, for MCP.

Two levels, because the class has two shapes:

1. **Within a gated surface** — enumerate the real registry of every gated
   surface (not a synthetic one: a synthetic server can only prove the gate works
   on tools the test itself registered) and assert the installed gate denies a
   restricted bound caller for every tool, except that surface's explicitly
   reviewed exempt set.
2. **Across surfaces** — a second ``FastMCP`` instance in the package is the same
   defect one level up. Every ``FastMCP`` in ``src/`` must either carry the gate
   or be declared in ``UNGATED_SURFACES`` with its tool names pinned, so adding a
   tool to a knowingly-ungated surface fails this test instead of shipping
   quietly.

``UNGATED_SURFACES`` is EMPTY since #1062. It held ``cao-ops-mcp-server``, which
#972 recorded as knowingly ungated and left for #671's follow-up: a bound terminal
whose profile listed that server reached ``launch_session`` / ``install_profile``
/ ``shutdown_session`` and the ``decisions_*`` controls with its ``tools:``
allowlist unconsulted. Both surfaces now carry the gate, and both are enumerated
below as this file's own comment instructed.

The gate is driven directly rather than through ``fastmcp.Client`` so that no
real tool implementation runs: the decision, not the body, is what is under test.
``test/ops_mcp_server/test_ops_tool_surface_allowlist.py`` is the other half for
the ops surface -- real dispatch, real allowlists, per-decision behaviour.
"""

import asyncio
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastmcp.exceptions import ToolError

from cli_agent_orchestrator.mcp_server import server
from cli_agent_orchestrator.ops_mcp_server import gate as ops_gate
from cli_agent_orchestrator.ops_mcp_server import server as ops_server
from cli_agent_orchestrator.utils import caller_tools

_SRC = Path(__file__).resolve().parents[2] / "src" / "cli_agent_orchestrator"

BOUND = {"CAO_TERMINAL_ID": "a1b2c3d4"}

#: Tools on the CAO surface that a restricted bound caller may still reach.
#: Pinned literally: widening it is a diff reviewers can see, which is the whole
#: point. See ``server.CALLBACK_EXEMPT_TOOLS`` for why these four must stay open.
REVIEWED_UNGATED = frozenset(
    {
        "send_message",
        "complete_assignment",
        "workflow_return",
        "answer_user_prompt",
    }
)

#: Tools on the ops control plane that a restricted bound caller may still reach.
#: Pinned literally here and matched against the implementation below.
#: ``decisions_off`` is the operator's fail-safe: argument-free and off-only, so it
#: can do nothing but reduce what the node is doing (#955, #1062 item 2). See
#: ``ops_gate.FAIL_SAFE_EXEMPT_TOOLS``.
OPS_REVIEWED_UNGATED = frozenset({"decisions_off"})

#: MCP surfaces in this package that carry NO allowlist gate, with the tools they
#: expose. EMPTY since #1062 gated ``cao-ops-mcp-server``; a new ``FastMCP`` server
#: either installs the gate or is declared here, with its tool names pinned and the
#: reason it needs none, so adding a tool to a knowingly-ungated surface fails this
#: file instead of shipping quietly.
UNGATED_SURFACES: dict = {}

#: The two gated surfaces: the module that carries the registry, the selector its
#: gate requires, its reviewed exempt set, and where its caller-context resolver is
#: patched. The per-tool tests below are parametrized from the live registries, so a
#: tool added to either surface is covered without anyone editing a list.
_CAO = "cao-mcp-server"
_OPS = "cao-ops-mcp-server"
SURFACES = {
    _CAO: {
        "mcp": server.mcp,
        "selector": "@cao-mcp-server",
        "exempt": REVIEWED_UNGATED,
        # ``_tool_denied_reason`` reads this module global, so this is the seam.
        "context_patch": (server, "_get_terminal_context_from_env"),
        "implementation": lambda: set(server.CALLBACK_EXEMPT_TOOLS),
    },
    _OPS: {
        "mcp": ops_server.mcp,
        "selector": "@cao-ops-mcp-server",
        "exempt": OPS_REVIEWED_UNGATED,
        # ``ops_tool_denied_reason`` resolves through the module, for this reason.
        "context_patch": (caller_tools, "caller_terminal_context"),
        "implementation": lambda: set(ops_gate.FAIL_SAFE_EXEMPT_TOOLS),
    },
}


def _tool_names(mcp):
    return {tool.name for tool in asyncio.run(mcp._list_tools())}


def _gate(mcp):
    """The installed allowlist gate on ``mcp``, or None."""
    for middleware in getattr(mcp, "middleware", []):
        if type(middleware).__name__ == "_AllowlistGate":
            return middleware
    return None


def _decide(surface, tool_name, allowed_tools):
    """Ask ``surface``'s installed gate about ``tool_name``; no implementation runs.

    Returns the denial message, or None if the gate passed the call through.
    """
    spec = SURFACES[surface]
    gate = _gate(spec["mcp"])
    assert gate is not None, f"no allowlist gate installed on {surface}"
    context = SimpleNamespace(message=SimpleNamespace(name=tool_name, arguments={}))
    sentinel = object()

    async def call_next(_context):
        return sentinel

    ctx = {
        "terminal_id": "a1b2c3d4",
        "session_name": "cao-session",
        "provider": "codex",
        "agent_profile": "worker",
        "allowed_tools": allowed_tools,
    }
    target, attribute = spec["context_patch"]
    with patch.dict(os.environ, BOUND):
        with patch.object(target, attribute, return_value=ctx):
            try:
                assert asyncio.run(gate.on_call_tool(context, call_next)) is sentinel
                return None
            except ToolError as exc:
                return str(exc)


def _every_tool_on_every_surface():
    """``(surface, tool)`` for every tool the two live registries expose."""
    return [
        pytest.param(surface, tool, id=f"{surface}:{tool}")
        for surface, spec in SURFACES.items()
        for tool in sorted(_tool_names(spec["mcp"]))
    ]


@pytest.mark.parametrize("surface,tool", _every_tool_on_every_surface())
def test_every_registered_tool_is_gated(surface, tool):
    """The registry, not a hand-written list, decides what must be covered."""
    spec = SURFACES[surface]
    denial = _decide(surface, tool, ["fs_read"])
    if tool in spec["exempt"]:
        assert denial is None, f"{tool} is reviewed-ungated on {surface} but the gate denies it"
        return
    assert denial is not None, (
        f"{tool} is registered on {surface} and reachable by a bound terminal "
        f"whose allowlist omits '{spec['selector']}'. Gate it, or add it to this "
        f"surface's reviewed-ungated set here and to its exempt set in the "
        f"implementation, with a reason."
    )
    assert spec["selector"] in denial


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_reviewed_ungated_set_matches_the_implementation(surface):
    """The guard's allowlist and the server's exemption cannot drift apart."""
    spec = SURFACES[surface]
    assert spec["exempt"] == spec["implementation"]()


@pytest.mark.parametrize("surface", sorted(SURFACES))
def test_the_real_gate_denies_by_default_not_by_enumeration(surface):
    """A name the gate has never heard of is denied, on the real server object.

    The decision is taken before dispatch and keyed on the name alone, so a tool
    a later PR adds — or one a plugin's ``on_mcp_server`` hook adds — is covered
    without anyone remembering a guard. That inverted default is the fix for the
    class; this asserts it against the real ``mcp`` objects, not a stand-in.
    """
    denial = _decide(surface, "tool_from_a_later_pr", ["fs_read"])
    assert denial is not None and SURFACES[surface]["selector"] in denial


@pytest.mark.parametrize(
    "module,install",
    [
        ("mcp_server/server.py", "_install_allowlist_middleware(mcp)"),
        ("ops_mcp_server/server.py", "install_ops_allowlist_middleware(mcp)"),
    ],
)
def test_the_gate_is_installed_after_plugin_registration(module, install):
    """Install order, so a plugin-registered tool lands inside the gate."""
    source = (_SRC / module).read_text(encoding="utf-8")
    register = source.index("\nregister_mcp_server_surfaces(mcp)")
    assert register < source.index(f"\n{install}"), (
        f"{install} must run after register_mcp_server_surfaces(mcp) in {module}, "
        "or plugin-registered tools sit outside the gate."
    )


def test_every_fastmcp_surface_in_the_package_is_gated_or_declared():
    """A new ungated MCP surface is this defect one level up."""
    found = {
        path.relative_to(_SRC).as_posix()
        for path in _SRC.rglob("*.py")
        if re.search(r"^\s*\w+\s*=\s*FastMCP\(", path.read_text(encoding="utf-8"), re.MULTILINE)
    }
    assert found == {
        "mcp_server/server.py",
        "ops_mcp_server/server.py",
    }, (
        f"MCP surfaces changed: {sorted(found)}. A new FastMCP server is a new "
        f"authorization surface — install the allowlist gate on it, or declare it "
        f"in UNGATED_SURFACES with the reason it needs none."
    )
    assert not UNGATED_SURFACES, (
        f"a surface is declared ungated: {sorted(UNGATED_SURFACES)}. #1062 emptied "
        f"this; a new entry needs the same kind of justification and follow-up issue."
    )
    for name, spec in SURFACES.items():
        assert _gate(spec["mcp"]) is not None, f"{name} lost its allowlist gate"


def test_the_fail_safe_decisions_off_stays_argument_free():
    """Its exemption rests on it only ever turning points off (#955, #1062 item 2).

    The pin above is by name. A parameter added later (a state, a point) would widen
    an exempt tool without failing any of the tests above, so its input schema is
    pinned here.
    """
    tools = {tool.name: tool for tool in asyncio.run(ops_server.mcp._list_tools())}
    assert not tools["decisions_off"].parameters.get("properties"), (
        "decisions_off gained parameters while it is exempt from the "
        "cao-ops-mcp-server allowlist gate; re-justify "
        "ops_gate.FAIL_SAFE_EXEMPT_TOOLS."
    )
