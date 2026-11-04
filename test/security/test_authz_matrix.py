"""Route-by-scope authorization matrix with the auth layer enabled.

Derived from the ``authz_matrix`` security harness written by Augusto Santos
(@gutosantos82) while reviewing CAO's CI security gates, converted to pytest and
pinned to the behaviour the scope tiers are meant to produce.

Every HTTP route in the live FastAPI table is requested four times with auth
enabled: with no token, and with tokens granting exactly ``cao:read``,
``cao:write`` and ``cao:admin``. Every route handler is stubbed, so only the
dependency tree (feature gates, authentication, scope check) and request
validation execute; nothing touches tmux, the database or the filesystem. A cell
is DENIED on 401/403 and ADMITTED on anything else: the stub's 200, or a 422 from
path/body validation, both of which lie *past* the scope dependency.

This complements ``test/api/test_scope_coverage.py``. That suite proves each
route *declares* a scope dependency; this one proves what the declared tiers
actually admit, across the whole table at once, so a route that quietly moves to
a weaker tier shows up as a changed cell rather than as nothing.
"""

from __future__ import annotations

import inspect
import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from test.api.test_scope_coverage import _EXEMPT as READ_TIER_MUTATING_ROUTES
from test.api.test_scope_coverage import _OPEN_READS
from typing import Dict, Iterator, List, Optional

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.responses import Response
from starlette.websockets import WebSocketDisconnect

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.plugins import PluginRegistry
from cli_agent_orchestrator.security import auth

SERVER = "localhost:9889"
WS_PATH = "/terminals/deadbeef/ws"

# Token name -> scopes granted. ``None`` means "send no Authorization header".
TOKENS: Dict[str, Optional[List[str]]] = {
    "none": None,
    "read": [auth.SCOPE_READ],
    "write": [auth.SCOPE_WRITE],
    "admin": [auth.SCOPE_ADMIN],
}
DENIED = frozenset({401, 403})
MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
STUB_STATUS = 200
WS_UNAUTHORIZED = 4401
WS_TERMINAL_NOT_FOUND = 4004  # after accept, from the stubbed lookup: every gate passed

# Routes gated by ``require_any_scope(SCOPE_ADMIN)`` alone: a ``cao:write`` token
# is refused. Pinned as literals so moving a route in or out of the admin tier is
# a visible diff here, not a silent change. ``/workflows/plans/approve`` is
# deliberately narrower than its sibling workflow routes (see the comment on the
# endpoint): an agent holding a write token must not be able to approve its own plan.
ADMIN_ONLY = {
    ("DELETE", "/sessions/{session_name}"),
    ("DELETE", "/terminals/{terminal_id}"),
    ("DELETE", "/flows/{name}"),
    ("DELETE", "/workflows/{name}"),
    ("DELETE", "/memory"),
    ("DELETE", "/memory/{key}"),
    ("POST", "/workflows/plans/approve"),
}

# Mutating verbs that admit ``cao:read``: POST-with-body *reads*. The validate /
# preview routes are the stateless validators ``test_scope_coverage`` exempts from
# scope gating; the internal memory routes are recall/context lookups whose query
# travels in a JSON body; ``/agui/v1/run`` serves a dashboard snapshot at the read
# floor and escalates to ``cao:write`` inside the handler only when ``resume[]`` is
# non-empty, which a stubbed handler cannot observe (``test_agui_run_endpoint``
# pins that escalation).
READ_TIER_MUTATING = READ_TIER_MUTATING_ROUTES | {
    ("POST", "/agui/v1/run"),
    ("POST", "/internal/memory/recall"),
    ("POST", "/internal/memory/context"),
}

# Mutating-verb routes that carry NO scope dependency at all, so they answer
# without a token even with auth enabled. All three are pure validators: the
# config travels in the body and nothing is persisted or disclosed beyond the
# schema check / template render. Pinned so a fourth cannot appear unnoticed;
# ``POST /workflows/validate`` is exempt from scope *tiering* too but is gated at
# the read floor, which is why it is absent here.
UNGATED_MUTATING = {
    ("POST", "/agents/profiles/templates/validate"),
    ("POST", "/agents/profiles/templates/preview"),
    ("POST", "/agents/profiles/validate"),
}

# Path parameters that must look like a name rather than a hex id.
_NAME_PARAMS = {"name", "session_name"}


@dataclass
class Cell:
    method: str
    path: str
    status: Dict[str, int] = field(default_factory=dict)

    @property
    def key(self):
        return (self.method, self.path)

    def admitted(self, token: str) -> bool:
        return self.status[token] not in DENIED

    def __str__(self) -> str:
        return f"{self.method} {self.path} {self.status}"


def _fill(path: str) -> str:
    return re.sub(
        r"\{([^}:]+)(:[^}]+)?\}",
        lambda m: "cao-x" if m.group(1) in _NAME_PARAMS else "deadbeef",
        path,
    )


def _fake_extract_scopes(token: str) -> List[str]:
    scopes = TOKENS.get(token)
    if scopes is None:
        raise ValueError("unknown test token")
    return list(scopes)


@contextmanager
def _handlers_stubbed() -> Iterator[None]:
    """Replace every route handler with one that answers ``STUB_STATUS``.

    The dependency tree still runs in full: FastAPI solves dependencies and
    validates parameters before calling ``dependant.call``. Coroutine-ness is
    preserved because the request handler decided how to await the endpoint
    when the route was built.
    """
    originals = []
    for route in api_main.app.routes:
        if not isinstance(route, APIRoute):
            continue
        originals.append((route, route.dependant.call))
        if inspect.iscoroutinefunction(route.dependant.call):

            async def _async_stub(**_kw):
                return Response(status_code=STUB_STATUS)

            route.dependant.call = _async_stub
        else:

            def _sync_stub(**_kw):
                return Response(status_code=STUB_STATUS)

            route.dependant.call = _sync_stub
    try:
        yield
    finally:
        for route, call in originals:
            route.dependant.call = call


_MATRIX: Optional[List[Cell]] = None
_WS: Optional[Dict[str, object]] = None


@pytest.fixture
def auth_on(monkeypatch):
    """Enable the auth layer with an in-process scope oracle; open the feature gates.

    The feature gates (agent plugins, ephemeral agents, AG-UI) answer 404 before
    the scope dependency runs when their surface is off, which would read as
    "admitted" in the matrix. Overriding them lets the scope tier be observed on
    those routes too; the gates themselves are pinned by their own suites.
    """
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.example/jwks")
    monkeypatch.delenv("CAO_AUTH_LOCAL_TOKEN", raising=False)
    monkeypatch.setattr(auth, "extract_scopes_from_token", _fake_extract_scopes)
    monkeypatch.setattr(api_main, "extract_scopes_from_token", _fake_extract_scopes)
    monkeypatch.setattr(api_main, "WS_ALLOWED_CLIENTS", ["*"])
    monkeypatch.setattr(api_main, "get_terminal_metadata", lambda *_a, **_k: None)
    api_main.app.state.plugin_registry = PluginRegistry()
    overrides = api_main.app.dependency_overrides
    overrides[api_main._require_plugins_enabled] = lambda: None
    overrides[api_main._require_agui_enabled] = lambda: None
    overrides[api_main._require_ephemeral_enabled] = lambda: {}
    try:
        yield
    finally:
        for dep in (
            api_main._require_plugins_enabled,
            api_main._require_agui_enabled,
            api_main._require_ephemeral_enabled,
        ):
            overrides.pop(dep, None)


@pytest.fixture
def matrix(auth_on) -> List[Cell]:
    """Every (method, path) in the route table, probed with each token, computed once."""
    global _MATRIX
    if _MATRIX is None:
        client = TestClient(
            api_main.app, base_url=f"http://{SERVER}", raise_server_exceptions=False
        )
        cells: List[Cell] = []
        with _handlers_stubbed():
            for route in api_main.app.routes:
                if not isinstance(route, APIRoute):
                    continue
                for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                    cell = Cell(method, route.path)
                    for token, scopes in TOKENS.items():
                        headers = {} if scopes is None else {"Authorization": f"Bearer {token}"}
                        cell.status[token] = client.request(
                            method, _fill(route.path), headers=headers
                        ).status_code
                    cells.append(cell)
        _MATRIX = cells
    return _MATRIX


@pytest.fixture
def ws_matrix(auth_on) -> Dict[str, object]:
    """The terminal WebSocket probed with no token and each scope via ``?token=``."""
    global _WS
    if _WS is None:
        client = TestClient(
            api_main.app, base_url=f"http://{SERVER}", raise_server_exceptions=False
        )
        results: Dict[str, object] = {}
        for token in ("none", "read", "write", "admin"):
            query = "" if token == "none" else f"?token={token}"
            try:
                with client.websocket_connect(
                    f"ws://{SERVER}{WS_PATH}{query}", headers={"origin": f"http://{SERVER}"}
                ) as ws:
                    try:
                        ws.receive_text()
                    except WebSocketDisconnect as closed_after_accept:
                        results[token] = ("accepted", closed_after_accept.code)
                    else:
                        results[token] = ("accepted", None)
            except WebSocketDisconnect as refused:
                results[token] = refused.code
        _WS = results
    return _WS


def _offending(cells: List[Cell], predicate) -> str:
    return "\n".join(str(c) for c in cells if predicate(c))


def test_matrix_covers_the_route_table_without_server_errors(matrix):
    """A 5xx in any cell would mean a dependency blew up, not that auth decided."""
    assert len(matrix) >= 90, "route table unexpectedly small; is the app fully imported?"
    assert not _offending(
        matrix, lambda c: any(s >= 500 for s in c.status.values())
    ), "cells answered 5xx:\n" + _offending(
        matrix, lambda c: any(s >= 500 for s in c.status.values())
    )


def test_no_token_is_denied_on_every_mutating_route_outside_the_ungated_validators(matrix):
    bad = _offending(
        matrix,
        lambda c: c.method in MUTATING
        and c.key not in UNGATED_MUTATING
        and c.status["none"] != 401,
    )
    assert not bad, "mutating routes reachable without a token:\n" + bad
    gated_now = _offending(matrix, lambda c: c.key in UNGATED_MUTATING and not c.admitted("none"))
    assert not gated_now, (
        "validators pinned as ungated now require a token (remove them from "
        "UNGATED_MUTATING):\n" + gated_now
    )
    assert (
        UNGATED_MUTATING <= READ_TIER_MUTATING_ROUTES
    ), "an ungated mutating route must also be one test_scope_coverage exempts as stateless"


def test_read_token_is_denied_on_every_mutating_route_outside_the_read_tier(matrix):
    """``cao:read`` is refused (403) on every mutating route except the pinned
    POST-with-body reads, which must keep admitting it."""
    bad = _offending(
        matrix,
        lambda c: c.method in MUTATING
        and c.key not in READ_TIER_MUTATING
        and c.status["read"] != 403,
    )
    assert not bad, (
        "mutating routes that admit a read-only token and are not pinned in "
        "READ_TIER_MUTATING:\n" + bad
    )
    tightened = _offending(matrix, lambda c: c.key in READ_TIER_MUTATING and not c.admitted("read"))
    assert not tightened, (
        "pinned read-tier routes no longer admit read (update READ_TIER_MUTATING):\n" + tightened
    )


def test_write_token_is_admitted_on_every_mutating_route_except_the_admin_tier(matrix):
    weaker = _offending(matrix, lambda c: c.key in ADMIN_ONLY and c.status["write"] != 403)
    assert not weaker, "admin-tier routes that admit a write token:\n" + weaker
    stronger = _offending(
        matrix,
        lambda c: c.method in MUTATING and c.key not in ADMIN_ONLY and not c.admitted("write"),
    )
    assert not stronger, (
        "routes refusing a write token that are not pinned in ADMIN_ONLY "
        "(add them there if the move to the admin tier is intended):\n" + stronger
    )


def test_pinned_route_sets_have_no_stale_entries(matrix):
    """A stale entry would silently pre-authorize a future route reusing the path."""
    registered = {c.key for c in matrix}
    for name, pinned in (
        ("ADMIN_ONLY", ADMIN_ONLY),
        ("READ_TIER_MUTATING", READ_TIER_MUTATING),
        ("UNGATED_MUTATING", UNGATED_MUTATING),
    ):
        stale = sorted(pinned - registered)
        assert not stale, f"{name} lists routes no longer in the table: {stale}"


def test_admin_token_is_admitted_everywhere(matrix):
    bad = _offending(matrix, lambda c: not c.admitted("admin"))
    assert not bad, "routes refusing an admin token:\n" + bad


def test_no_token_is_denied_on_every_get_outside_the_open_reads(matrix):
    """The read floor: a GET without a token is 401 unless it is a deliberately open
    read listed in ``test_scope_coverage._OPEN_READS``."""
    bad = _offending(
        matrix,
        lambda c: c.method == "GET" and c.path not in _OPEN_READS and c.status["none"] != 401,
    )
    assert not bad, "GET routes reachable without a token that are not listed open:\n" + bad
    closed = _offending(
        matrix, lambda c: c.method == "GET" and c.path in _OPEN_READS and not c.admitted("none")
    )
    assert not closed, "listed-open reads now require a token (update _OPEN_READS):\n" + closed


def test_read_token_is_admitted_on_every_get(matrix):
    bad = _offending(matrix, lambda c: c.method == "GET" and not c.admitted("read"))
    assert not bad, "GET routes refusing a read token:\n" + bad


def test_terminal_websocket_requires_write_or_admin(ws_matrix):
    """Keystroke injection is code execution: ``cao:read`` is not enough for the PTY."""
    assert ws_matrix["none"] == WS_UNAUTHORIZED
    assert ws_matrix["read"] == WS_UNAUTHORIZED
    assert ws_matrix["write"] == ("accepted", WS_TERMINAL_NOT_FOUND)
    assert ws_matrix["admin"] == ("accepted", WS_TERMINAL_NOT_FOUND)
