"""Every first-party client of CAO's own scope-gated API attaches a bearer (#9007).

The ROUTE side of the scope gate is centrally tested — ``test/security/test_authz_matrix.py``
and ``test/api/test_auth_read_gating.py`` walk the route table, so adding a gated route is
safe by construction. The CALLER side is per-module convention: each client module has its
own ``_auth_headers()`` (``mcp_server/utils.py:21``, ``mcp_server/app_tools.py:105``,
``ops_mcp_server/server.py:74``, ``utils/orchestration.py:65``) and every ``requests`` call
is supposed to pass ``headers=_auth_headers() or None``. A convention does not stop the next
call site from omitting it, and ten of them already have (#829).

The two guards that exist test the wrong axis. ``test/test_bearer_scope_boundary.py`` (#822)
checks a bearer is never paired with a REMOTE url — it says nothing about a local url paired
with NO helper. ``test/api/test_workflow_authoring_client_auth.py`` (#699) drives real
clients against the real routes, but only the four workflow-authoring tools and the ``cao
workflow`` verbs.

So this module is the generic version: one parametrised case per first-party client surface
that drives the REAL client entry point against the REAL route table with auth enabled, and
asserts no request the client made was refused at the auth boundary (401/403). It does not
assert the call SUCCEEDS — an admitted request may still 404 or 500 because no tmux session,
no skill and no terminal row exist in a unit test. The only claim is that the client got
past the gate, which is the one thing nothing else checks.

Auth mode: local-token (#706, ``security/auth.py:109-131``). ``CAO_AUTH_LOCAL_TOKEN`` alone
switches ``is_auth_enabled()`` on for the server AND is what ``get_local_bearer()`` hands the
client, and a match grants the full scope set. That isolates the single variable this module
is about — is a credential attached at all — with no JWKS stub, no subprocess and no
per-route scope bookkeeping, so the whole module runs in about a second. Scope CORRECTNESS
per route is the authz matrix's job, not this one's.

Surfaces deliberately NOT covered, with reasons:

* ``services/agui/base.py:111`` / ``services/agui/stream_reader.py:116`` carry their own
  ``self._token`` — an AG-UI client credential supplied by the caller, not this node's
  ``CAO_AUTH_LOCAL_TOKEN``.
* ``utils/fleet.py:123,209``, ``services/terminal_service.py:2098,2123`` and the
  ``utils/orchestration.py`` remote arms (``:442,485,519,1890``) address ANOTHER node or the
  elastic-worker gateway, which hold their own credentials. Withholding ours there is the
  #822 rule, already enforced statically.
* The terminal WebSocket (``api/main.py:8111``) needs a real listener (the
  ``cao_server_with_auth`` fixture), not ``TestClient``; covering it here would cost a
  subprocess per case and break the "keep each test fast" budget.
* ``services/install_service.py:250`` fetches from outside CAO.
"""

from __future__ import annotations

import asyncio
import io
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional
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

LOCAL_TOKEN = "first-party-client-auth-local-token"

# Passes the TerminalId path pattern (``^[a-f0-9]{8}$``) so a refusal can only come
# from the auth dependency, never from path validation.
TERMINAL_ID = "abcdef12"

REFUSED = (401, 403)


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
    hops: List[_Hop] = field(default_factory=list)

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


@pytest.fixture
def boundary(monkeypatch):
    """Auth on (local-token mode), and every client's HTTP wired to the real app."""
    for var in ("AUTH0_DOMAIN", "AUTH0_AUDIENCE", "CAO_AUTH_JWKS_URI", "CAO_AUTH_ISSUER"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", LOCAL_TOKEN)
    auth_module.get_jwks_cache().clear()
    assert auth_module.is_auth_enabled() is True
    assert auth_module.is_local_token_mode() is True
    assert auth_module.get_local_bearer() == LOCAL_TOKEN

    # The two tools that act on "my own terminal" need an identity to act as.
    monkeypatch.setenv("CAO_TERMINAL_ID", TERMINAL_ID)

    adapter = _ApiBoundary(TestClient(app, base_url="http://localhost"))
    for verb in ("get", "post", "put", "patch", "delete", "request"):
        monkeypatch.setattr(requests, verb, getattr(adapter, verb))
    monkeypatch.setattr(shim_transport, "urlopen", adapter.urlopen)
    monkeypatch.setattr(app_tools, "_is_enabled", lambda: True)
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
# The surfaces. ``xfail(strict=True)`` marks the ones that are broken TODAY --
# every one of them is a client that attaches no credential against a route that
# requires one. They are #829's fix, not this module's; strict=True means the
# marker must be REMOVED when that lands, so these cannot rot into permanent
# exemptions.
# ---------------------------------------------------------------------------

_BROKEN = pytest.mark.xfail(
    strict=True,
    reason="#829: client attaches no Authorization header to a scope-gated local route",
)

SURFACES = [
    # --- mcp_server tools: the #829 class -------------------------------------
    pytest.param(
        lambda: _call(_tool(mcp_server, "load_skill"), name="cao-memory"),
        id="mcp.load_skill",
        marks=_BROKEN,
    ),
    pytest.param(
        lambda: _call(_tool(mcp_server, "emit_ui"), component="progress", props={"pct": 1}),
        id="mcp.emit_ui",
        marks=_BROKEN,
    ),
    pytest.param(
        lambda: _call(_tool(mcp_server, "answer_user_prompt"), terminal_id=TERMINAL_ID, answer="1"),
        id="mcp.answer_user_prompt",
        marks=_BROKEN,
    ),
    pytest.param(
        lambda: _call(_tool(mcp_server, "list_siblings"), depth=None, cross_session=False),
        id="mcp.list_siblings",
        marks=_BROKEN,
    ),
    pytest.param(
        lambda: _call(_tool(mcp_server, "update_metadata"), metadata={"doing": "gate"}),
        id="mcp.update_metadata",
        marks=_BROKEN,
    ),
    # --- script tier: the shim cannot see a credential at all -----------------
    pytest.param(_shim_run_step, id="script_tier.run_step", marks=_BROKEN),
    # --- surfaces that already attach the bearer ------------------------------
    pytest.param(
        lambda: _call(_tool(mcp_server, "workflow_list")),
        id="mcp.workflow_list",
    ),
    pytest.param(
        lambda: _call(_tool(mcp_server, "workflow_status"), run_id="run-9007"),
        id="mcp.workflow_status",
    ),
    pytest.param(
        lambda: _call(_tool(ops_server, "list_profiles")),
        id="ops_mcp.list_profiles",
    ),
    pytest.param(
        lambda: _call(_tool(ops_server, "list_sessions")),
        id="ops_mcp.list_sessions",
    ),
    pytest.param(
        lambda: app_tools._render_dashboard_impl(),
        id="mcp_apps.render_dashboard",
    ),
    pytest.param(
        lambda: _cli(cli_session.session, ["list"]),
        id="cli.session_list",
    ),
]


@pytest.mark.parametrize("drive", SURFACES)
def test_first_party_client_is_not_refused_at_the_auth_boundary(boundary, drive):
    """The real client reaches the real route with auth on and is not 401/403'd."""
    _drive(drive)

    assert boundary.hops, (
        "the client made no HTTP call, so this case proves nothing -- the entry point "
        "short-circuited before its request (check the fixture's env and arguments)"
    )
    refused = [hop for hop in boundary.hops if hop.status in REFUSED]
    assert not refused, "refused at the auth boundary: " + "; ".join(str(h) for h in refused)


@pytest.mark.parametrize("drive", SURFACES)
def test_first_party_client_sends_this_nodes_bearer(boundary, drive):
    """Stronger form: the credential on the wire is this node's local bearer.

    Separate from the status assertion so a surface that is admitted for the WRONG
    reason -- a route that quietly stopped being gated -- still fails here.
    """
    _drive(drive)

    assert boundary.hops
    bare = [hop for hop in boundary.hops if hop.bearer != f"Bearer {LOCAL_TOKEN}"]
    assert not bare, "sent no / wrong credential: " + "; ".join(str(h) for h in bare)


def test_the_gate_catches_a_removed_bearer_attachment():
    """Proof the gate works: strip the header from a client that HAS it and it goes red.

    ``ops_mcp_server._auth_headers`` is the attachment point for both of that module's
    ``requests.request`` calls. Neutralising it is exactly the regression this module
    exists to catch, so the parametrised assertion above must fail when it is gone.
    """
    with patch.object(ops_server, "_auth_headers", return_value={}):
        adapter = _ApiBoundary(TestClient(app, base_url="http://localhost"))
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
