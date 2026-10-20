"""One registration-policy coverage table across every surface CAO registers on.

Why this module exists (#1005, from the sweep findings on #671, #829, #744):
CAO enforces authorization **per registration**, not at a chokepoint. A FastAPI
route carries its own ``require_any_scope`` dependency; a CAO MCP tool calls
``_tool_denied_reason`` in its own body; an MCP Apps tool declares its own
``_meta.ui.requiredScopes``. That design means **a newly registered tool or
route is ungated by default**, and the only thing standing between "someone adds
a surface" and "the surface ships unauthenticated" is a test that enumerates the
registry.

Those enumerating tests existed, but the knowledge was split three ways:

* ``test/api/test_scope_coverage.py`` — HTTP routes only;
* ``test/security/test_authz_matrix.py`` — what the HTTP tiers *admit*;
* ``test/mcp_server/test_tool_allowlist_enforcement.py`` — the #671 guard, which
  asserts enforcement for a **hard-coded list** of six delegation tools and never
  looks at the MCP tool registry.

No existing test enumerated an MCP registry for policy coverage. A few tests
call ``list_tools()`` for other reasons (auth-client checks, ephemeral-agent
tests, operator decisions), but none of them checks a tool against a caller
policy, and none touches the ops registry — which is exactly how the MCP
surface got missed in the first place, and why nobody noticed that the
``cao-ops-mcp-server`` tools are not gated at all.

So: one table, one invariant, every surface.

Each row declares (surface name, registry accessor, policy probe, exempt set
with a reason per entry) and gets the same two assertions:

* ``test_every_registration_is_policy_covered_or_exempt`` — nothing is ungated
  by omission; a new registration must be covered or consciously exempted;
* ``test_exemptions_are_not_stale`` — an exemption that no longer names a live,
  ungated registration is removed, so a stale entry cannot pre-authorize a
  future registration that reuses the name.

Plus ``test_the_table_names_every_known_surface``, a consistency check between
``SURFACES`` and the separately hand-maintained ``EXPECTED_SURFACES`` literal:
it catches a one-sided edit to either list, not the arrival of a new surface
that nobody wrote a row for in the first place.

RELATIONSHIP TO THE EXISTING TWO FILES: this module does not replace them and
deliberately does not copy their data. The HTTP rows *import* ``_EXEMPT`` and
``_OPEN_READS`` from ``test/api/test_scope_coverage.py`` — one source of truth,
no second list to drift — and that file keeps its enforcement tests (what a
``cao:read`` token actually gets), which are behavioural and out of scope for a
registration table. ``test_authz_matrix.py`` keeps the per-tier admit/deny
matrix for the same reason.

WHAT THIS TABLE DOES NOT CLAIM: it is a *registration* guard. A row passing
means every registration on that surface runs the surface's policy mechanism (or
is listed as exempt). It does not mean the policy decides correctly — that is
what the two files above test.
"""

from __future__ import annotations

import asyncio
import inspect
import os
from dataclasses import dataclass, field
from test.api.test_scope_coverage import _EXEMPT as HTTP_MUTATING_EXEMPT
from test.api.test_scope_coverage import _OPEN_READS as HTTP_OPEN_READS
from test.api.test_scope_coverage import _has_scope_dependency
from typing import Callable, Dict, Iterable, Optional, Set, Tuple
from unittest.mock import patch

import pytest
from fastapi.routing import APIRoute
from fastmcp import FastMCP
from starlette.routing import WebSocketRoute

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.mcp_server import server as cao_mcp
from cli_agent_orchestrator.ops_mcp_server import server as ops_mcp
from cli_agent_orchestrator.plugins import registry as plugin_registry

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


# --------------------------------------------------------------------------- #
# The row type.
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Surface:
    """One registration surface and its policy invariant.

    Attributes:
        name: Row id, used as the pytest parameter id.
        mechanism: Human-readable name of the policy mechanism, quoted in failures
            so the message tells the author what to add rather than just what broke.
        registrations: Returns ``{registration_id: covered?}`` for the live registry.
            Returning the *decision* (not the object) keeps each probe next to the
            accessor that knows how to read that registry.
        exempt: ``{registration_id: reason}``. Every entry needs a reason; the test
            asserts the reasons are non-empty so "exempt" can never be a bare list.
        exempt_may_be_covered: Relaxes the stale-exemption check from "live and
            still uncovered" to "live". Set only on the two HTTP rows, whose exempt
            sets are *imported* from ``test/api/test_scope_coverage.py`` and serve a
            second purpose there (``_EXEMPT`` is also the read-tier set
            ``test_authz_matrix.py`` imports as ``READ_TIER_MUTATING_ROUTES``). A
            route can therefore be both listed and gated without the listing being
            dead — ``POST /workflows/validate`` is exactly that today: it acquired
            a ``require_any_scope(READ, WRITE, ADMIN)`` dependency
            (``api/main.py:4981``) while staying in ``_EXEMPT`` because it still has
            to admit ``cao:read``. Owning rows keep the strict check.
        xfail_reason: Set when the surface genuinely fails today. Applied ONLY to
            the coverage assertion, and ``strict``, so it flips to a failure the
            moment the defect is fixed and the row must then be un-xfailed. The
            bookkeeping assertions are never xfailed: they pass for a failing
            surface and a strict xfail on them would report XPASS.
    """

    name: str
    mechanism: str
    registrations: Callable[[], Dict[str, bool]]
    exempt: Dict[str, str] = field(default_factory=dict)
    exempt_may_be_covered: bool = False
    xfail_reason: Optional[str] = None


# --------------------------------------------------------------------------- #
# Row 1 + 2 — FastAPI HTTP routes.
#
# The probe and both exempt sets are imported from test/api/test_scope_coverage.py
# rather than restated: two copies of "which routes are deliberately open" would
# drift, and that file's own stale-entry test already keeps them honest.
# --------------------------------------------------------------------------- #
def _http_mutating_registrations() -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods & _MUTATING_METHODS):
            out[f"{method} {route.path}"] = _has_scope_dependency(route)
    return out


def _http_read_registrations() -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        if "GET" not in methods or getattr(route, "dependant", None) is None:
            continue
        out[f"GET {route.path}"] = _has_scope_dependency(route)
    return out


_HTTP_MUTATING_EXEMPT_ROW = {
    f"{method} {path}": (
        "stateless validator / renderer: the config travels in a JSON body so the verb "
        "is POST, but nothing is persisted (see test/api/test_scope_coverage.py::_EXEMPT)"
    )
    for method, path in HTTP_MUTATING_EXEMPT
}

_HTTP_READ_EXEMPT_ROW = {
    f"GET {path}": (
        "deliberately open read, reasoned about in test/api/test_scope_coverage.py::_OPEN_READS"
    )
    for path in HTTP_OPEN_READS
}


# --------------------------------------------------------------------------- #
# Row 3 — WebSocket routes.
#
# A WS endpoint cannot carry a FastAPI dependency that returns 403: the handshake
# has to be refused before ``accept()``, so the policy lives in the handler body.
# The probe is therefore structural on the source: the handler must consult
# ``is_auth_enabled()`` and close with 4401. ``test_authz_matrix.py``
# (``test_terminal_websocket_requires_write_or_admin``) drives the real handshake
# and proves the close code; this row only proves no WS route *skipped* the check.
# --------------------------------------------------------------------------- #
_WS_CLOSE_UNAUTHORIZED = "4401"


def _ws_registrations() -> Dict[str, bool]:
    out: Dict[str, bool] = {}
    for route in app.routes:
        if not isinstance(route, WebSocketRoute):
            continue
        endpoint = getattr(route, "endpoint", None)
        try:
            source = inspect.getsource(inspect.unwrap(endpoint))
        except (OSError, TypeError):  # pragma: no cover - defensive
            source = ""
        covered = "is_auth_enabled()" in source and _WS_CLOSE_UNAUTHORIZED in source
        out[f"WS {route.path}"] = covered
    return out


# --------------------------------------------------------------------------- #
# Row 4 — CAO MCP tools (``@cao-mcp-server``).
#
# Mechanism: ``mcp_server/server.py::_tool_denied_reason``, the #671 guard. It
# enforces the caller terminal's effective ``allowed_tools`` against the
# ``@cao-mcp-server`` selector.
#
# The invariant is NOT "every CAO MCP tool calls the guard". Most of these tools
# act strictly inside the caller's own identity — ``memory_store`` writes to the
# caller's own memory scope, ``send_message`` posts as the caller, the
# ``workflow_*`` reads only read. Gating those on ``@cao-mcp-server`` would break
# every narrow-allowlist agent that is supposed to use them. What the guard exists
# to stop is **identity minting**: a tool that launches or resumes work under a
# caller-chosen ``agent_profile``, which gets a fresh allowlist and its own memory
# scope.
#
# So the row's probe is "runs the guard", and the exempt map is the explicit,
# reasoned list of identity-local tools. The *point* of enumerating the registry
# is that adding a 35th tool fails this row until its author writes down which
# side of that line it is on — which is precisely the review step that did not
# happen for the ops server (row 5).
# --------------------------------------------------------------------------- #
_GUARD_SYMBOL = "_tool_denied_reason"

_IDENTITY_LOCAL = "acts only inside the caller's own identity: mints no agent_profile"
_READ_ONLY = "read-only: returns state the caller can already see, mints nothing"

_CAO_MCP_EXEMPT: Dict[str, str] = {
    # Messaging / callback: speaks as the caller, to terminals it already knows.
    "send_message": _IDENTITY_LOCAL,
    "complete_assignment": _IDENTITY_LOCAL,
    "answer_user_prompt": _IDENTITY_LOCAL,
    "emit_ui": _IDENTITY_LOCAL,
    "workflow_return": _IDENTITY_LOCAL,
    # Memory and learning: writes land in the caller's own scope.
    "memory_store": _IDENTITY_LOCAL,
    "memory_recall": _IDENTITY_LOCAL,
    "memory_forget": _IDENTITY_LOCAL,
    "report_outcome": _IDENTITY_LOCAL,
    "store_lesson": _IDENTITY_LOCAL,
    "update_metadata": _IDENTITY_LOCAL,
    # Reads.
    "list_outcomes": _READ_ONLY,
    "list_siblings": _READ_ONLY,
    "find_profiles": _READ_ONLY,
    "get_handoff_result": _READ_ONLY,
    "load_skill": _READ_ONLY,
    "workflow_get": _READ_ONLY,
    "workflow_list": _READ_ONLY,
    "workflow_status": _READ_ONLY,
    "workflow_events": _READ_ONLY,
    "workflow_result": _READ_ONLY,
    "workflow_wait": _READ_ONLY,
    "workflow_validate": _READ_ONLY,
    # Authoring and lifecycle on work the caller already owns. These mutate, but
    # the thing they mutate is a spec file or a run the caller started; none of
    # them chooses an agent_profile, so the @cao-mcp-server selector is not the
    # right question to ask of them.
    "workflow_create": _IDENTITY_LOCAL,
    "workflow_update": _IDENTITY_LOCAL,
    "workflow_cancel": _IDENTITY_LOCAL,
    "workflow_plan_approval": _IDENTITY_LOCAL,
    # delete_terminal and the ephemeral tools do their own, narrower checks:
    # delete_terminal refuses a terminal the caller does not own
    # (_target_binding_denial), and create_ephemeral_agent IS guarded but is
    # registered conditionally (CAO_EPHEMERAL_ENABLED), so it only appears in the
    # registry on some configs; it is not listed here because this map must not
    # carry a name that is sometimes covered.
    "delete_terminal": "ownership-checked in-handler via _target_binding_denial",
}


def _mcp_tool_registrations(mcp: FastMCP) -> Dict[str, bool]:
    """``{tool_name: calls _tool_denied_reason?}`` for every tool on ``mcp``.

    Uses ``_list_tools`` rather than the public ``list_tools`` so app-only tools
    that are hidden from the model's tool list are still enumerated — a tool an
    MCP App host can call is a registration whether or not a model can see it.
    """
    tools = asyncio.run(mcp._list_tools())
    out: Dict[str, bool] = {}
    for tool in tools:
        try:
            source = inspect.getsource(inspect.unwrap(tool.fn))
        except (OSError, TypeError):  # pragma: no cover - defensive
            source = ""
        out[tool.name] = _GUARD_SYMBOL in source
    return out


def _cao_mcp_registrations() -> Dict[str, bool]:
    return _mcp_tool_registrations(cao_mcp.mcp)


# --------------------------------------------------------------------------- #
# Row 5 — ops MCP tools (``cao-ops-mcp-server``). LIVE DEFECT.
#
# Nothing in ``ops_mcp_server/`` consults ``_tool_denied_reason`` or any other
# caller policy. ``launch_session`` takes ``allowed_tools`` straight from the
# caller and forwards it to ``POST /sessions``, so a bound terminal holding a
# narrow allowlist that happens to have the ops server mounted can launch a
# session with ``allowed_tools=["*"]`` — the exact escalation the #671 guard was
# written to stop on the CAO server. ``install_profile``, ``send_session_message``
# and ``shutdown_session`` are the same shape.
#
# The row is ``xfail(strict=True)`` and NOT exempted away: #1005 says to report
# the defect, not to fix the product code here. Strict means that once the ops
# tools are gated, this row fails as XPASS and whoever fixes it has to delete the
# xfail — the table cannot quietly keep claiming a hole that is closed.
# --------------------------------------------------------------------------- #
_OPS_XFAIL = (
    "LIVE DEFECT (#1005, from the #671 guard analysis): cao-ops-mcp-server tools run no "
    "caller-policy check at all. launch_session() forwards caller-supplied allowed_tools "
    "to POST /sessions, so a bound terminal with a narrow allowlist can mint a session "
    "with allowed_tools=['*']. Remove this xfail when the ops tools are gated."
)


def _ops_mcp_registrations() -> Dict[str, bool]:
    return _mcp_tool_registrations(ops_mcp.mcp)


# --------------------------------------------------------------------------- #
# Row 6 — plugin-registered MCP tools (the ``on_mcp_server`` hook).
#
# ``PluginRegistry.register_mcp_server_surfaces`` hands any installed
# ``cao.plugins`` entry point the live FastMCP server and lets it register tools.
# That is a third registration path into CAO's MCP surface, reached by neither of
# the two MCP rows above when the plugin is disabled by default — which the
# built-in ``mcp_apps`` plugin is (``CAO_MCP_APPS_ENABLED``).
#
# These tools do not use ``_tool_denied_reason``; their mechanism is the SEP-1865
# ``_meta.ui.requiredScopes`` annotation that ``app_tools.register_app_tools``
# attaches, which ``_submit_command_impl`` then enforces. So the probe reads the
# annotation, and the read-only ``render_*`` / event views are the exempt set.
#
# The row registers the plugins onto a scratch FastMCP, never the live server.
# --------------------------------------------------------------------------- #
_PLUGIN_VIEW_EXEMPT = (
    "read-only MCP App view: returns a snapshot the Backplane already serves at the "
    "read floor and routes no mutation, so it declares requiredScopes=[] by design"
)

_PLUGIN_MCP_EXEMPT: Dict[str, str] = {
    "render_dashboard": _PLUGIN_VIEW_EXEMPT,
    "render_agent_view": _PLUGIN_VIEW_EXEMPT,
    "render_graph_view": _PLUGIN_VIEW_EXEMPT,
    "cao_fetch_history": _PLUGIN_VIEW_EXEMPT,
    "subscribe_events": _PLUGIN_VIEW_EXEMPT,
}


def _plugin_mcp_registrations() -> Dict[str, bool]:
    """``{tool_name: declares a non-empty _meta.ui.requiredScopes?}``.

    ``CAO_MCP_APPS_ENABLED`` is forced on for the probe: the point of the row is
    to see the surface a plugin *can* register, and leaving it default-off would
    make the row vacuously green on exactly the config that ships.
    """
    scratch = FastMCP(name="registration-policy-probe")
    with patch.dict(os.environ, {"CAO_MCP_APPS_ENABLED": "1", "CAO_AUTH_LOCAL_TOKEN": "probe"}):
        plugin_registry.register_mcp_server_surfaces(scratch)
    tools = asyncio.run(scratch._list_tools())
    out: Dict[str, bool] = {}
    for tool in tools:
        ui = (tool.meta or {}).get("ui") or {}
        out[tool.name] = bool(ui.get("requiredScopes"))
    return out


# --------------------------------------------------------------------------- #
# Row 7 — plugin event hooks.
#
# Dispatch is keyed on ``event_type`` strings set by the ``@hook`` decorator and
# resolved at registration time with no validation, so a typo'd or invented
# event name registers a handler that is simply never called — and an event CAO
# starts emitting under a new name silently reaches no declared hook. The policy
# here is the declared event vocabulary in ``plugins/events.py``: every hook an
# installed plugin declares must name a ``CaoEvent`` subclass that exists.
#
# This is the one row that is not about authorization. It is in the same table on
# purpose: it is the same failure mode (a registration nothing cross-checks), and
# the whole reason #1005 exists is that per-surface knowledge living in per-
# surface files is how a surface gets missed.
# --------------------------------------------------------------------------- #
def _known_event_types() -> Set[str]:
    from cli_agent_orchestrator.plugins import events as events_module

    known: Set[str] = set()
    for _, obj in inspect.getmembers(events_module, inspect.isclass):
        if issubclass(obj, events_module.CaoEvent) and obj is not events_module.CaoEvent:
            event_type = getattr(obj, "event_type", "")
            if event_type:
                known.add(event_type)
    return known


def _plugin_hook_registrations() -> Dict[str, bool]:
    import importlib.metadata as importlib_metadata

    from cli_agent_orchestrator.plugins.base import _HOOK_EVENT_ATTR, CaoPlugin

    known = _known_event_types()
    out: Dict[str, bool] = {}
    for entry_point in importlib_metadata.entry_points(group=plugin_registry.ENTRY_POINT_GROUP):
        try:
            plugin_class = entry_point.load()
        except Exception:  # pragma: no cover - a broken third-party install
            continue
        if not (isinstance(plugin_class, type) and issubclass(plugin_class, CaoPlugin)):
            continue
        for method_name, method in inspect.getmembers(plugin_class, inspect.isfunction):
            event_type = getattr(method, _HOOK_EVENT_ATTR, None)
            if event_type is None:
                continue
            out[f"{entry_point.name}.{method_name} -> {event_type}"] = event_type in known
    return out


# --------------------------------------------------------------------------- #
# The table.
# --------------------------------------------------------------------------- #
SURFACES: Tuple[Surface, ...] = (
    Surface(
        name="http-mutating-routes",
        mechanism="a require_any_scope dependency on the route",
        registrations=_http_mutating_registrations,
        exempt=_HTTP_MUTATING_EXEMPT_ROW,
        exempt_may_be_covered=True,
    ),
    Surface(
        name="http-read-routes",
        mechanism="a require_any_scope dependency on the route",
        registrations=_http_read_registrations,
        exempt=_HTTP_READ_EXEMPT_ROW,
        exempt_may_be_covered=True,
    ),
    Surface(
        name="websocket-routes",
        mechanism="an is_auth_enabled() scope check and a 4401 close present in the handler body",
        registrations=_ws_registrations,
    ),
    Surface(
        name="cao-mcp-tools",
        mechanism="a call to _tool_denied_reason() somewhere in the tool body",
        registrations=_cao_mcp_registrations,
        exempt=_CAO_MCP_EXEMPT,
    ),
    Surface(
        name="ops-mcp-tools",
        mechanism="a call to _tool_denied_reason() somewhere in the tool body",
        registrations=_ops_mcp_registrations,
        xfail_reason=_OPS_XFAIL,
    ),
    Surface(
        name="plugin-mcp-tools",
        mechanism="a non-empty _meta.ui.requiredScopes annotation",
        registrations=_plugin_mcp_registrations,
        exempt=_PLUGIN_MCP_EXEMPT,
    ),
    Surface(
        name="plugin-event-hooks",
        mechanism="an event_type naming a declared CaoEvent subclass",
        registrations=_plugin_hook_registrations,
    ),
)

# Every surface the table is expected to cover. Separate from ``SURFACES`` on
# purpose: deriving it from the value under test would make
# ``test_the_table_names_every_known_surface`` tautological.
EXPECTED_SURFACES = frozenset(
    {
        "http-mutating-routes",
        "http-read-routes",
        "websocket-routes",
        "cao-mcp-tools",
        "ops-mcp-tools",
        "plugin-mcp-tools",
        "plugin-event-hooks",
    }
)


def _coverage_rows() -> Iterable[object]:
    """Rows for the coverage assertion, with the xfail marks applied."""
    for surface in SURFACES:
        marks = (
            [pytest.mark.xfail(reason=surface.xfail_reason, strict=True)]
            if surface.xfail_reason
            else []
        )
        yield pytest.param(surface, id=surface.name, marks=marks)


# Bookkeeping rows carry no xfail: those assertions hold for a surface whose
# coverage is broken (an empty exempt map has nothing stale and nothing
# unexplained), so a strict xfail would turn them into XPASS failures.
_BOOKKEEPING_ROWS = tuple(pytest.param(s, id=s.name) for s in SURFACES)
_COVERAGE_ROWS = tuple(_coverage_rows())


@pytest.mark.parametrize("surface", _COVERAGE_ROWS)
def test_every_registration_is_policy_covered_or_exempt(surface: Surface):
    """The whole point: no registration on any surface is ungated by omission."""
    registrations = surface.registrations()
    assert registrations, f"{surface.name}: the registry accessor found nothing to check"
    uncovered = sorted(
        name
        for name, covered in registrations.items()
        if not covered and name not in surface.exempt
    )
    assert not uncovered, (
        f"{surface.name}: registration(s) with no policy coverage: "
        + ", ".join(uncovered)
        + f". Each must either carry {surface.mechanism}, or be added to this row's "
        "exempt map with a reason saying what it exposes and why that is acceptable."
    )


@pytest.mark.parametrize("surface", _BOOKKEEPING_ROWS)
def test_exemptions_are_not_stale(surface: Surface):
    """An exemption must name a registration that is live AND still uncovered.

    Both halves matter. A name that is gone (or renamed) would silently
    pre-authorize a future registration reusing it; a name that has since been
    gated would keep the row claiming an exception it no longer needs, which is
    how an exempt map grows into a list nobody reads.

    The second half is dropped for rows whose exempt set is imported and shared
    (``exempt_may_be_covered``); see the field's docstring.
    """
    registrations = surface.registrations()
    if surface.exempt_may_be_covered:
        allowed = set(registrations)
        what = "registrations"
    else:
        allowed = {name for name, covered in registrations.items() if not covered}
        what = "live, ungated registrations"
    stale = sorted(set(surface.exempt) - allowed)
    assert not stale, (
        f"{surface.name}: exempt entr(ies) that are no longer {what}: "
        + ", ".join(stale)
        + ". Remove them."
    )


@pytest.mark.parametrize("surface", _BOOKKEEPING_ROWS)
def test_every_exemption_carries_a_reason(surface: Surface):
    """ "Exempt" is never a bare name — the reason is the reviewable artifact."""
    unexplained = sorted(name for name, reason in surface.exempt.items() if not reason.strip())
    assert not unexplained, f"{surface.name}: exemption(s) with no reason: " + ", ".join(
        unexplained
    )


def test_the_table_names_every_known_surface():
    """``SURFACES`` and ``EXPECTED_SURFACES`` must name the same rows.

    ``EXPECTED_SURFACES`` is a second hand-written literal, not derived from
    the registries under test, so this only catches a one-sided edit between
    the two lists in this file — e.g. a row added to ``SURFACES`` without
    updating ``EXPECTED_SURFACES``, or vice versa. It does not discover a new
    registration surface (a mounted MCP server, a new router family) on its
    own; that still requires a human to write both entries.
    """
    declared = {surface.name for surface in SURFACES}
    assert declared == set(EXPECTED_SURFACES), (
        "the surface table and EXPECTED_SURFACES disagree. Missing rows: "
        f"{sorted(EXPECTED_SURFACES - declared)}; undeclared rows: "
        f"{sorted(declared - EXPECTED_SURFACES)}."
    )


# --------------------------------------------------------------------------- #
# The gate's own negative tests.
#
# A coverage table that cannot go red is worse than no table: it reports a clean
# bill of health for a surface it is not actually reading. These construct the
# failure each probe exists to catch, against a scratch registry, and assert the
# probe reports it as uncovered.
# --------------------------------------------------------------------------- #
def test_an_unguarded_scratch_mcp_tool_reads_as_uncovered():
    """Register a tool that does not call the guard; the MCP probe must see it."""
    scratch = FastMCP(name="scratch-unguarded")

    @scratch.tool()
    async def scratch_escalate(agent_profile: str) -> dict:
        """A tool that mints an identity and forgot the guard."""
        return {"ok": True, "agent_profile": agent_profile}

    registrations = _mcp_tool_registrations(scratch)
    assert registrations == {"scratch_escalate": False}


def test_a_guarded_scratch_mcp_tool_reads_as_covered():
    """The probe is not vacuously False — it does detect the guard call."""
    scratch = FastMCP(name="scratch-guarded")

    @scratch.tool()
    async def scratch_guarded(agent_profile: str) -> dict:
        """A tool that does call the guard."""
        denied = cao_mcp._tool_denied_reason("scratch_guarded")
        if denied:
            return {"ok": False, "error": denied}
        return {"ok": True}

    registrations = _mcp_tool_registrations(scratch)
    assert registrations == {"scratch_guarded": True}


def test_the_covered_or_exempt_assertion_fails_on_an_unguarded_registration():
    """The row assertion itself goes red for an ungated, unexempted registration.

    Drives the real test body against a synthetic row, so this covers the
    assertion that ships rather than a re-implementation of it.
    """
    row = Surface(
        name="scratch",
        mechanism="a scratch policy",
        registrations=lambda: {"guarded": True, "forgotten": False},
    )
    with pytest.raises(AssertionError) as excinfo:
        test_every_registration_is_policy_covered_or_exempt(row)
    assert "forgotten" in str(excinfo.value)
    assert "guarded" not in str(excinfo.value).split("coverage:")[1].split(". Each")[0]


def test_the_covered_or_exempt_assertion_passes_once_the_registration_is_exempted():
    """...and goes green again when the author records a reason."""
    row = Surface(
        name="scratch",
        mechanism="a scratch policy",
        registrations=lambda: {"guarded": True, "forgotten": False},
        exempt={"forgotten": "scratch reason"},
    )
    test_every_registration_is_policy_covered_or_exempt(row)


def test_an_unguarded_scratch_http_route_reads_as_uncovered():
    """The HTTP probe sees a route registered with no scope dependency."""
    from fastapi import FastAPI

    scratch = FastAPI()

    @scratch.post("/scratch/escalate")
    async def scratch_escalate() -> dict:
        return {"ok": True}

    api_routes = [r for r in scratch.routes if isinstance(r, APIRoute)]
    assert api_routes, "scratch app registered no APIRoute"
    assert not any(_has_scope_dependency(r) for r in api_routes)


def test_a_scratch_websocket_without_the_auth_check_reads_as_uncovered():
    """The WS probe sees a handshake handler that never consults the auth layer."""

    async def scratch_ws(websocket):  # pragma: no cover - never awaited
        await websocket.accept()

    source = inspect.getsource(scratch_ws)
    assert "is_auth_enabled()" not in source or _WS_CLOSE_UNAUTHORIZED not in source


def test_a_typod_plugin_hook_event_reads_as_uncovered():
    """The hook probe rejects an event name that no CaoEvent declares."""
    known = _known_event_types()
    assert "post_send_message" in known, "the declared event vocabulary is empty"
    assert "post_send_mesage" not in known
