"""Every first-party client of CAO's own scope-gated API attaches a bearer (#1005).

The ROUTE side of the scope gate is centrally tested — ``test/security/test_authz_matrix.py``
and ``test/api/test_auth_read_gating.py`` walk the route table, so adding a gated route is
safe by construction. The CALLER side is per-module convention: each client module has its
own ``_auth_headers()`` (``mcp_server/utils.py``, ``mcp_server/app_tools.py``,
``ops_mcp_server/server.py``, ``utils/orchestration.py``) and every ``requests`` call is
supposed to pass ``headers=_auth_headers() or None``. A convention does not stop the next
call site from omitting it. #990 found fourteen ``mcp_server/server.py`` sites that had, and
#991 attached the helper at all fourteen; the five ``mcp_server`` cases below are the
end-to-end regression tests for that fix, driving the real tool through the real route.
One surface is still broken: the script-tier shim (``cao_workflow``) is stdlib-only and has
no credential to attach. #829 tracks it; its unmerged ``agent/gh-829`` branch adds a
run-scoped token (``security/run_tokens.py``).

The two guards that exist test the wrong axis. ``test/test_bearer_scope_boundary.py`` (#822)
checks a bearer is never paired with a REMOTE url — it says nothing about a local url paired
with NO helper. ``test/api/test_workflow_authoring_client_auth.py`` (#699) drives real
clients against the real routes, but only the four workflow-authoring tools and the ``cao
workflow`` verbs. ``test/test_bearer_call_site_census.py`` scans source text for
``requests.<verb>(`` calls, so it cannot see a client that goes through ``api_http`` or
``urllib``.

So this module is the generic version: it SAMPLES the first-party clients, one or more entry
points per attachment point (each client module's ``_auth_headers()``, ``api_http._send``
for the CLI, the ``urllib`` shim for the script tier), drives the REAL client entry point
against the REAL route table with auth enabled, and asserts no request the client made was
refused at the auth boundary (401/403). The sampling unit is the attachment point, not the
call site: a module whose every call goes through one helper is covered by one case that
goes through that helper, and the census (which does see every ``requests`` call site) is
what proves the other call sites use the same helper. It does not
assert the call SUCCEEDS — an admitted request may still 404 or 500 because no tmux session,
no skill and no live terminal exist in a unit test. The only claim is that the client got
past the gate, which is the one thing nothing else checks.

Each surface declares every hop it is meant to make (``_Surface.hops``) and the test
asserts each one occurred, so a client that stops after its first request cannot pass on
the strength of that request alone. The two-hop tools (``answer_user_prompt``,
``list_siblings``, ``update_metadata``) first ``GET /terminals/{own}`` and give up if the
row is missing, so the fixture stubs the terminal service with one row for ``TERMINAL_ID``
-- waiting on a user answer, unrestricted tools, created by ``CALLER_ID`` -- and the
no-op writes behind ``/input``, ``/siblings`` and ``/metadata``. The routes, and their
auth dependencies, are the real ones; only the service layer under them is stubbed.

Auth mode: local-token (#706, ``security/auth.py``). ``CAO_AUTH_LOCAL_TOKEN`` alone switches
``is_auth_enabled()`` on for the server AND is what ``get_local_bearer()`` hands the client,
and a match grants the full scope set. That isolates the single variable this module is
about — is a credential attached at all — with no JWKS stub, no subprocess and no per-route
scope bookkeeping, so the whole module runs in about a second. Scope CORRECTNESS per route
is the authz matrix's job, not this one's.

Known-broken surfaces are marked with ``_broken_until(<probe>)``, never a bare
``xfail``. A bare ``xfail(strict=True)`` encodes a BELIEF ("this is broken") that only a
human can retire, so when the fix lands on another branch the marker goes stale and the
strict XPASS lands as a red on whichever PR merges second (that is exactly what happened
to the five ``mcp_server`` markers this file first shipped with, 28 minutes after #991 was
opened). ``_broken_until`` ties the marker to a PROBE for the fix's footprint instead: the
case asserts the fixed behaviour from day one, is expected to fail only while the probe
says the fix is absent, and lifts itself the moment the fix lands. If the probe is wrong
in either direction the strict marker still goes red, so a stale probe cannot rot into a
permanent exemption either. ``test_known_broken_markers_lift_themselves`` enumerates the
class: no surface may carry an xfail that was not built this way.

First-party callers that have NO case here, and why:

* The ``utils/orchestration.py`` local arms other than ``send_message``: ``handoff`` and
  ``assign`` create a terminal (``POST /sessions``, ``POST /sessions/{name}/terminals``)
  and then wait on it, which needs a tmux backend and a live provider. They share
  ``_auth_headers()`` with the ``send_message`` case, which is the most-used arm.
* The other ``api_http`` CLI commands (``cao info``, ``cao launch``, ``cao shutdown``,
  ``cao terminal``) and the ``api_http`` helpers in ``utils/terminal.py``: every one goes
  through ``api_http._send``, which is what the ``cao session list`` case covers. The
  census cannot see these at all, which is why one of them is here.
* The other ``@mcp.tool`` functions in ``mcp_server/server.py`` and ``ops_mcp_server``:
  each goes through the helper its module's cases exercise; the census pins the
  per-call-site convention.
* The clients that are not Python: ``web/src/api.ts`` and ``tui/``. They carry their own
  token handling and are outside a pytest file's reach.

Surfaces deliberately NOT covered, with reasons:

* ``services/agui/base.py`` / ``services/agui/stream_reader.py`` carry their own
  ``self._token`` — an AG-UI client credential supplied by the caller, not this node's
  ``CAO_AUTH_LOCAL_TOKEN``.
* ``utils/fleet.py``, ``services/terminal_service.py`` and the ``utils/orchestration.py``
  remote arms address ANOTHER node or the elastic-worker gateway, which hold their own
  credentials. Withholding ours there is the #822 rule, already enforced statically.
* The terminal WebSocket (``api/main.py``, ``/terminals/{id}/ws``) needs a real listener
  (the ``cao_server_with_auth`` fixture), not ``TestClient``; covering it here would cost a
  subprocess per case and break the "keep each test fast" budget.
* ``services/install_service.py`` fetches from outside CAO.
"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

import pytest
import requests
from click.testing import CliRunner
from fastapi.testclient import TestClient

import cao_workflow
from cao_workflow import _transport as shim_transport
from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.cli.commands import session as cli_session
from cli_agent_orchestrator.mcp_server import app_tools
from cli_agent_orchestrator.mcp_server import server as mcp_server
from cli_agent_orchestrator.ops_mcp_server import server as ops_server
from cli_agent_orchestrator.security import auth as auth_module
from cli_agent_orchestrator.services import terminal_service
from cli_agent_orchestrator.utils import orchestration

LOCAL_TOKEN = "first-party-client-auth-local-token"

# Passes the TerminalId path pattern (``^[a-f0-9]{8}$``) so a refusal can only come
# from the auth dependency, never from path validation.
TERMINAL_ID = "abcdef12"
# The terminal recorded as having created TERMINAL_ID via assign/handoff.
CALLER_ID = "0badcafe"

REFUSED = (401, 403)

# One (method, path) the client is expected to put on the wire.
_Expected = Tuple[str, str]


@dataclass
class _Hop:
    """One client -> API request, as the route table saw it."""

    method: str
    path: str
    status: int
    bearer: Optional[str]

    def __str__(self) -> str:  # pragma: no cover - failure messages only
        who = "no bearer" if self.bearer is None else "bearer"
        return f"{self.method.upper()} {self.path} -> {self.status} ({who})"


@dataclass
class _ApiBoundary:
    """Routes a client's outbound HTTP into the real FastAPI app and records each hop."""

    client: TestClient
    hops: List[_Hop]

    @staticmethod
    def _path(url: str) -> str:
        return urlsplit(url).path or "/"

    def _send(self, method: str, url: str, *, headers=None, **kwargs: Any):
        headers = dict(headers or {})
        response = self.client.request(
            method,
            self._path(url),
            headers=headers,
            params=kwargs.get("params"),
            json=kwargs.get("json"),
        )
        self.hops.append(
            _Hop(method, self._path(url), response.status_code, headers.get("Authorization"))
        )
        return response

    # The surface of ``requests`` the client modules use. They all do a plain
    # ``import requests``, so one patch of the module object covers every caller.
    def get(self, url: str, **kwargs: Any):
        return self._send("get", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self._send("post", url, **kwargs)

    def put(self, url: str, **kwargs: Any):
        return self._send("put", url, **kwargs)

    def patch(self, url: str, **kwargs: Any):
        return self._send("patch", url, **kwargs)

    def delete(self, url: str, **kwargs: Any):
        return self._send("delete", url, **kwargs)

    def request(self, method: str, url: str, **kwargs: Any):
        return self._send(method, url, **kwargs)

    def urlopen(self, request, timeout=None):
        """``urllib`` entry point, for the stdlib-only script-tier shim (BR-8)."""
        headers = {key.title(): value for key, value in request.header_items()}
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        response = self.client.request(
            request.get_method(),
            self._path(request.full_url),
            headers=headers,
            json=body,
        )
        self.hops.append(
            _Hop(
                request.get_method(),
                self._path(request.full_url),
                response.status_code,
                headers.get("Authorization"),
            )
        )
        if response.status_code >= 400:
            raise HTTPError(
                request.full_url,
                response.status_code,
                "error",
                response.headers,  # type: ignore[arg-type]
                io.BytesIO(response.content),
            )
        return _FakeUrlopenResult(response.content, response.status_code)


class _FakeUrlopenResult:
    def __init__(self, content: bytes, status: int) -> None:
        self._content = content
        self._status = status

    def __enter__(self):
        return self

    def __exit__(self, *_exc: Any) -> None:
        return None

    def read(self) -> bytes:
        return self._content

    def getcode(self) -> int:
        return self._status


def _new_boundary() -> _ApiBoundary:
    return _ApiBoundary(TestClient(app, base_url="http://localhost"), [])


def _terminal_row(terminal_id: str) -> Dict[str, Any]:
    """What ``GET /terminals/{id}`` serves for any id the clients ask about.

    Shaped so every two-hop tool proceeds to its second call: ``status`` lets
    ``answer_user_prompt`` go on to ``POST .../input``; ``allowed_tools=None`` is the
    unrestricted grant ``_require_discovery_marker`` accepts; ``caller_id`` gives a
    reply-to-caller ``send_message`` an address.
    """
    return {
        "id": terminal_id,
        "name": "worker",
        "provider": "claude_code",
        "session_name": "first-party-client-auth",
        "agent_profile": "developer",
        "caller_id": CALLER_ID if terminal_id == TERMINAL_ID else None,
        "allowed_tools": None,
        "status": "waiting_user_answer",
        "metadata": {},
    }


def _stub_terminal_service(monkeypatch) -> None:
    """One terminal row, and inert writes, under the REAL terminal routes."""
    monkeypatch.setattr(terminal_service, "get_terminal", _terminal_row)
    monkeypatch.setattr(terminal_service, "update_metadata", lambda terminal_id, metadata: True)
    monkeypatch.setattr(
        terminal_service,
        "list_siblings",
        lambda caller_id, depth=None, cross_session=False: [],
    )
    monkeypatch.setattr(terminal_service, "send_input", lambda *args, **kwargs: True)


@pytest.fixture
def boundary(monkeypatch):
    """Auth on (local-token mode), and every client's HTTP wired to the real app.

    Local-token mode, not the ``auth_enabled_env`` fixture's Auth0 mode: in Auth0 mode
    ``get_local_bearer()`` returns ``None``, so every client would attach nothing and every
    surface would 401 -- the gate would have no signal to tell a correct client from a broken
    one.
    """
    for var in ("AUTH0_DOMAIN", "AUTH0_AUDIENCE", "CAO_AUTH_JWKS_URI", "CAO_AUTH_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", LOCAL_TOKEN)
    auth_module.get_jwks_cache().clear()
    assert auth_module.is_auth_enabled() is True
    assert auth_module.is_local_token_mode() is True
    assert auth_module.get_local_bearer() == LOCAL_TOKEN

    # The tools that act on "my own terminal" need an identity to act as, and a row
    # for it on the server so their second hop is reached (see _terminal_row).
    monkeypatch.setenv("CAO_TERMINAL_ID", TERMINAL_ID)
    _stub_terminal_service(monkeypatch)

    adapter = _new_boundary()
    for verb in ("get", "post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(requests, verb, getattr(adapter, verb))
    monkeypatch.setattr(shim_transport, "urlopen", adapter.urlopen)
    yield adapter
    auth_module.get_jwks_cache().clear()


def _tool(module: Any, name: str) -> Callable[..., Any]:
    """The undecorated function behind an ``@mcp.tool()``."""
    tool = getattr(module, name)
    return getattr(tool, "fn", tool)


def _call(fn: Callable[..., Any], **kwargs: Any) -> Any:
    result = fn(**kwargs)
    if asyncio.iscoroutine(result):
        return asyncio.run(result)
    return result


def _drive(fn: Callable[[], Any]) -> None:
    """Run a client entry point for its WIRE EFFECT; its return value is irrelevant.

    Clients disagree about what a refusal looks like coming back out: most return a
    structured ``{"success": False, "error": ...}``, some let ``raise_for_status()``
    propagate, the script-tier shim raises ``ShimHTTPError``. None of that is what this
    module asserts on -- the evidence is the recorded hop. Swallowing the exception keeps
    a client's error-reporting style from deciding whether the gate fires, and the
    ``assert boundary.hops`` in each test is what stops a swallowed exception from
    turning a short-circuit into a false pass.
    """
    try:
        fn()
    except Exception:  # noqa: BLE001 - see docstring
        pass


def _cli(command: Any, args: List[str]) -> Any:
    return CliRunner().invoke(command, args)


def _shim_run_step() -> Any:
    """Drive the script tier's one call site the way a workflow script does."""
    with patch.dict(
        os.environ,
        {
            "CAO_WORKFLOW_RUN_ID": "run-9007",
            "CAO_WORKFLOW_GENERATION": "1",
            "CAO_API_BASE_URL": "http://localhost",
        },
    ):
        return cao_workflow.run_step("claude", "developer", "noop", step_id="gate")


# ---------------------------------------------------------------------------
# What credential each surface is expected to put on the wire.
# ---------------------------------------------------------------------------


def _this_nodes_bearer(bearer: Optional[str]) -> bool:
    """The default: the operator-provisioned local token, verbatim."""
    return bearer == f"Bearer {LOCAL_TOKEN}"


def _some_credential(bearer: Optional[str]) -> bool:
    """A surface whose credential is NOT this node's bearer by design.

    The script tier runs inside a workflow step and must not hold the node's
    operator token; #829 gives it a run-scoped token instead. Until that lands
    the shim sends nothing, so "any Authorization header at all" is the weakest
    assertion that is true after the fix and false before it. Tighten this to
    the run-token shape once #829 is on main.
    """
    return bool(bearer)


# ---------------------------------------------------------------------------
# Known-broken markers that lift themselves.
# ---------------------------------------------------------------------------

_SELF_LIFTING_MARKS: List[Any] = []


def _broken_until(fixed: Callable[[], bool], *, reason: str) -> Any:
    """Mark a surface as known-broken until ``fixed()`` reports the fix has landed.

    The case keeps asserting the FIXED behaviour. While the probe is false the
    case is a strict xfail; once the probe is true the marker is inert and the
    case is a plain assertion. Both mismatches stay loud: a fix that lands
    without flipping the probe is a strict XPASS, and a probe that flips before
    the behaviour is fixed is a plain failure. Neither needs a human to notice
    anything -- only to correct the probe, and only when the probe is wrong.
    """
    mark = pytest.mark.xfail(
        condition=not fixed(),
        strict=True,
        reason=f"{reason} [lifts itself when {fixed.__name__}() is true]",
    )
    _SELF_LIFTING_MARKS.append(mark.mark)
    return mark


def _run_scoped_tokens_landed() -> bool:
    """#829's footprint: the server-side run-token module its branch adds.

    Named in the #829 branch as ``security/run_tokens.py``. If the fix lands
    under another name the script-tier case XPASSes strictly and this probe
    gets corrected -- the same cost as a bare xfail, but only in that case.
    """
    return importlib.util.find_spec("cli_agent_orchestrator.security.run_tokens") is not None


_BROKEN_SCRIPT_TIER = _broken_until(
    _run_scoped_tokens_landed,
    reason=(
        "#829: the script-tier shim has no credential to attach; the filed #829 ticket's "
        "unmerged agent/gh-829 branch (security/run_tokens.py) implements the fix in flight"
    ),
)


# ---------------------------------------------------------------------------
# The surfaces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Surface:
    """One first-party client entry point and what it is expected to do on the wire."""

    id: str
    drive: Callable[[], Any]
    # Every request the entry point is meant to make. The test asserts each one was
    # seen, so a case covers exactly the hops it names and no fewer.
    hops: Tuple[_Expected, ...]
    credential: Callable[[Optional[str]], bool] = _this_nodes_bearer
    marks: Tuple[Any, ...] = ()

    def param(self) -> Any:
        return pytest.param(self, id=self.id, marks=self.marks)


_OWN = f"/terminals/{TERMINAL_ID}"

SURFACES: List[_Surface] = [
    # --- mcp_server tools: the #990 class, fixed by #991 ---------------------
    _Surface(
        id="mcp.load_skill",
        drive=lambda: _call(_tool(mcp_server, "load_skill"), name="cao-memory"),
        hops=(("GET", "/skills/cao-memory"),),
    ),
    _Surface(
        id="mcp.emit_ui",
        drive=lambda: _call(_tool(mcp_server, "emit_ui"), component="progress", props={"pct": 1}),
        hops=(("POST", "/agui/v1/emit_ui"),),
    ),
    _Surface(
        id="mcp.answer_user_prompt",
        drive=lambda: _call(
            _tool(mcp_server, "answer_user_prompt"), terminal_id=TERMINAL_ID, answer="1"
        ),
        hops=(("GET", _OWN), ("POST", f"{_OWN}/input")),
    ),
    _Surface(
        id="mcp.list_siblings",
        drive=lambda: _call(_tool(mcp_server, "list_siblings"), depth=None, cross_session=False),
        hops=(("GET", _OWN), ("GET", f"{_OWN}/siblings")),
    ),
    _Surface(
        id="mcp.update_metadata",
        drive=lambda: _call(_tool(mcp_server, "update_metadata"), metadata={"doing": "gate"}),
        hops=(("GET", _OWN), ("PATCH", f"{_OWN}/metadata")),
    ),
    _Surface(
        id="mcp.workflow_list",
        drive=lambda: _call(_tool(mcp_server, "workflow_list")),
        hops=(("GET", "/workflows/runs"),),
    ),
    _Surface(
        id="mcp.workflow_status",
        drive=lambda: _call(_tool(mcp_server, "workflow_status"), run_id="run-9007"),
        hops=(("GET", "/workflows/runs/run-9007"),),
    ),
    # --- script tier: the shim cannot see a credential at all (#829) ---------
    _Surface(
        id="script_tier.run_step",
        drive=_shim_run_step,
        hops=(("POST", "/terminals/run-step"),),
        credential=_some_credential,
        marks=(_BROKEN_SCRIPT_TIER,),
    ),
    # --- orchestration, local arm: a worker replying to its recorded caller ----
    _Surface(
        id="orchestration.send_message",
        drive=lambda: orchestration._send_message_impl(receiver_id=None, message="gate"),
        hops=(("GET", _OWN), ("POST", f"/terminals/{CALLER_ID}/inbox/messages")),
    ),
    # --- the other client modules ---------------------------------------------
    _Surface(
        id="ops_mcp.list_profiles",
        drive=lambda: _call(_tool(ops_server, "list_profiles")),
        hops=(("GET", "/agents/profiles"),),
    ),
    _Surface(
        id="ops_mcp.list_sessions",
        drive=lambda: _call(_tool(ops_server, "list_sessions")),
        hops=(("GET", "/sessions"),),
    ),
    _Surface(
        id="mcp_apps.render_dashboard",
        drive=lambda: app_tools._render_dashboard_impl(),
        hops=(("GET", "/sessions"),),
    ),
    _Surface(
        id="cli.session_list",
        drive=lambda: _cli(cli_session.session, ["list"]),
        hops=(("GET", "/sessions"),),
    ),
]

PARAMS = [surface.param() for surface in SURFACES]


@pytest.mark.parametrize("surface", PARAMS)
def test_first_party_client_is_not_refused_at_the_auth_boundary(boundary, surface):
    """The real client reaches the real route with auth on and is not 401/403'd."""
    _drive(surface.drive)

    assert boundary.hops, (
        "the client made no HTTP call, so this case proves nothing -- the entry point "
        "short-circuited before its request (check the fixture's env and arguments)"
    )
    seen = {(hop.method.upper(), hop.path) for hop in boundary.hops}
    missing = [hop for hop in surface.hops if hop not in seen]
    assert not missing, (
        f"the client never made {missing}, so this case does not cover them -- it stopped "
        "after: " + "; ".join(str(h) for h in boundary.hops)
    )
    refused = [hop for hop in boundary.hops if hop.status in REFUSED]
    assert not refused, "refused at the auth boundary: " + "; ".join(str(h) for h in refused)


@pytest.mark.parametrize("surface", PARAMS)
def test_first_party_client_sends_its_credential_on_every_hop(boundary, surface):
    """Stronger form: every hop carries the credential this surface is meant to send.

    Separate from the status assertion so a surface that is admitted for the WRONG
    reason -- a route that quietly stopped being gated -- still fails here.
    """
    _drive(surface.drive)

    assert boundary.hops
    bare = [hop for hop in boundary.hops if not surface.credential(hop.bearer)]
    assert not bare, "sent no / wrong credential: " + "; ".join(str(h) for h in bare)


def test_every_surface_declares_the_hops_it_covers():
    """The class guard for "covers only its first hop": a case must name what it covers.

    A surface with no declared hops would fall back to "made at least one call", which is
    the shape that let three two-hop tools pass on their lookup alone.
    """
    for surface in SURFACES:
        assert surface.hops, f"{surface.id}: declares no hops"
        for method, path in surface.hops:
            assert method == method.upper() and path.startswith("/"), (surface.id, method, path)


def test_known_broken_markers_lift_themselves():
    """The class guard: no surface may carry a bare ``xfail``.

    Every xfail on a surface must have been built by ``_broken_until`` -- strict, and
    conditioned on a probe for the fix -- so a marker can never again encode a belief
    that only a human can retire.
    """
    for surface in SURFACES:
        for mark in surface.marks:
            mark = getattr(mark, "mark", mark)
            if mark.name != "xfail":
                continue
            assert mark in _SELF_LIFTING_MARKS, (
                f"{surface.id}: xfail not built by _broken_until(); a known-broken marker "
                "must name the probe that lifts it"
            )
            assert mark.kwargs.get("strict") is True, f"{surface.id}: xfail must be strict"
            assert isinstance(
                mark.kwargs.get("condition"), bool
            ), f"{surface.id}: xfail must carry the probe's verdict as its condition"


def test_the_gate_catches_a_removed_bearer_attachment():
    """Proof the gate works: strip the header from a client that HAS it and it goes red.

    ``ops_mcp_server._auth_headers`` is the attachment point for both of that module's
    ``requests.request`` calls. Neutralising it is exactly the regression this module
    exists to catch, so the parametrised assertion above must fail when it is gone.
    """
    with patch.object(ops_server, "_auth_headers", return_value={}):
        adapter = _new_boundary()
        with patch.dict(os.environ, {"CAO_AUTH_LOCAL_TOKEN": LOCAL_TOKEN}, clear=False):
            for var in ("AUTH0_DOMAIN", "CAO_AUTH_JWKS_URI"):
                os.environ.pop(var, None)
            auth_module.get_jwks_cache().clear()
            with patch.object(requests, "request", adapter.request):
                _call(_tool(ops_server, "list_profiles"))

    assert adapter.hops, "the scratch client made no call"
    assert [h.status for h in adapter.hops] == [401] * len(adapter.hops), [
        str(h) for h in adapter.hops
    ]
    assert all(h.bearer is None for h in adapter.hops)
