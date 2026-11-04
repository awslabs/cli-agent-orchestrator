"""Browser-boundary regression matrix for the HTTP and WebSocket surfaces.

Derived from the ``origin_matrix`` security harness written by Augusto Santos
(@gutosantos82) while reviewing CAO's CI security gates, converted to pytest and
pinned to the fixed behaviour. Each probe names the PR whose fix it guards:

* #124  ``Host`` validation (DNS rebinding) answers 400 before routing;
* #533  a cross-site ``Origin`` on the terminal WebSocket is refused before accept;
* #605  a cross-site state-changing HTTP request is a 403, including the
        "simple request" shapes CORS cannot preflight (``text/plain`` bodies, form
        posts, ``Origin: null``);
* #656  a malformed ``Origin`` is a clean 403 / pre-accept close, never a 500;
* #658  same-origin is scheme-aware: an ``http`` Origin on an ``https`` request is
        not same-origin.

No route handler runs. The HTTP guards are middleware and answer before routing,
so a request that *passes* them on the ``POST /flows`` control shows up as the 422
body-validation status rather than a 400/403. The WebSocket gates run before
``accept()``, so a handshake that passes them completes and is then closed 4004 by
the stubbed terminal lookup; one that fails them never completes and carries the
gate's own close code.
"""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cli_agent_orchestrator.api import main as api_main

SERVER = "localhost:9889"
SAME_ORIGIN_HTTP = f"http://{SERVER}"
SAME_ORIGIN_HTTPS = f"https://{SERVER}"
CROSS_SITE = "http://evil.example"
MALFORMED_ORIGIN = "http://["

POST_PATH = "/flows"
WS_PATH = "/terminals/deadbeef/ws"
TEXT_PLAIN = {"Content-Type": "text/plain"}
FORM_URLENCODED = {"Content-Type": "application/x-www-form-urlencoded"}

CSRF_BLOCKED_BODY = {"detail": "Cross-origin request blocked"}
BODY_VALIDATION = 422
HOST_REJECTED = 400
CSRF_REJECTED = 403
WS_IP_REJECTED = 4003
WS_ORIGIN_REJECTED = 4403
WS_TERMINAL_NOT_FOUND = 4004  # emitted *after* accept by the stubbed lookup

# Sentinel for "the handshake completed": every pre-accept gate passed.
WS_ACCEPTED = "accepted"


@pytest.fixture
def ws_gates_only(monkeypatch):
    """Let the TestClient peer through the IP allowlist and stub the terminal lookup.

    The TestClient peer is ``testclient``, not loopback, so the IP gate would
    short-circuit every row with 4003 and hide the Origin gate behind it. The
    terminal lookup is stubbed so a handshake that passes the gates is closed
    4004 immediately instead of spawning a tmux attach.
    """
    monkeypatch.setattr(api_main, "WS_ALLOWED_CLIENTS", ["*"])
    monkeypatch.setattr(api_main, "get_terminal_metadata", lambda *_a, **_k: None)


def _client(scheme: str = "http") -> TestClient:
    return TestClient(api_main.app, base_url=f"{scheme}://{SERVER}", raise_server_exceptions=False)


def _post(headers: dict, scheme: str = "http", body: str = "x"):
    return _client(scheme).post(POST_PATH, headers=headers, content=body)


def _ws(origin, scheme: str = "ws"):
    """Open the terminal WebSocket; return ``(WS_ACCEPTED, close_code)`` or the pre-accept code."""
    client = _client("https" if scheme == "wss" else "http")
    headers = {} if origin is None else {"origin": origin}
    try:
        with client.websocket_connect(f"{scheme}://{SERVER}{WS_PATH}", headers=headers) as ws:
            try:
                ws.receive_text()
            except WebSocketDisconnect as closed_after_accept:
                return WS_ACCEPTED, closed_after_accept.code
            return WS_ACCEPTED, None
    except WebSocketDisconnect as refused_before_accept:
        return refused_before_accept.code


class TestControls:
    """The legitimate shapes keep working; without these the rejections prove nothing."""

    def test_post_without_origin_reaches_validation(self):
        """Non-browser clients (CLI, curl, MCP) send no Origin and pass the guard."""
        assert _post(TEXT_PLAIN).status_code == BODY_VALIDATION

    def test_same_origin_post_reaches_validation(self):
        assert _post({**TEXT_PLAIN, "Origin": SAME_ORIGIN_HTTP}).status_code == BODY_VALIDATION

    def test_same_origin_websocket_handshake_completes(self, ws_gates_only):
        assert _ws(SAME_ORIGIN_HTTP) == (WS_ACCEPTED, WS_TERMINAL_NOT_FOUND)

    def test_websocket_without_origin_handshake_completes(self, ws_gates_only):
        """Native WebSocket clients send no Origin; the CSWSH threat is browser-only."""
        assert _ws(None) == (WS_ACCEPTED, WS_TERMINAL_NOT_FOUND)

    def test_websocket_ip_gate_still_runs_first(self, monkeypatch):
        """Without the allowlist override the non-loopback TestClient peer is refused 4003."""
        monkeypatch.setattr(api_main, "WS_ALLOWED_CLIENTS", ["127.0.0.1"])
        assert _ws(SAME_ORIGIN_HTTP) == WS_IP_REJECTED


class TestHostValidation:
    """#124 — a forged ``Host`` is refused before routing, for reads and writes alike."""

    def test_get_with_foreign_host_is_rejected(self):
        response = _client().get("/health", headers={"Host": "evil.example"})
        assert response.status_code == HOST_REJECTED

    def test_post_with_foreign_host_is_rejected(self):
        response = _client().post(POST_PATH, headers={"Host": "evil.example:9889"})
        assert response.status_code == HOST_REJECTED


class TestCrossSiteWebSocket:
    """#533 — a cross-site Origin never reaches ``accept()``."""

    def test_cross_site_origin_is_refused_before_accept(self, ws_gates_only):
        assert _ws(CROSS_SITE) == WS_ORIGIN_REJECTED


class TestCsrfSimpleRequests:
    """#605 — the request shapes a browser sends without a CORS preflight are blocked."""

    def test_text_plain_post_from_cross_site_origin(self):
        response = _post({**TEXT_PLAIN, "Origin": CROSS_SITE})
        assert response.status_code == CSRF_REJECTED
        assert response.json() == CSRF_BLOCKED_BODY

    def test_form_post_with_null_origin(self):
        """``Origin: null`` (sandboxed iframe, redirect chain) is not a trusted origin."""
        response = _post({**FORM_URLENCODED, "Origin": "null"}, body="a=b")
        assert response.status_code == CSRF_REJECTED
        assert response.json() == CSRF_BLOCKED_BODY


class TestMalformedOrigin:
    """#656 — an unparseable Origin is a clean refusal, never an unhandled exception."""

    def test_http_post_is_a_403_not_a_500(self):
        response = _post({**TEXT_PLAIN, "Origin": MALFORMED_ORIGIN})
        assert response.status_code == CSRF_REJECTED
        assert response.json() == CSRF_BLOCKED_BODY

    def test_websocket_is_closed_pre_accept_not_crashed(self, ws_gates_only):
        assert _ws(MALFORMED_ORIGIN) == WS_ORIGIN_REJECTED


class TestSchemeAwareSameOrigin:
    """#658 — same authority with a downgraded scheme is a different origin (RFC 6454)."""

    def test_https_post_rejects_http_origin(self):
        response = _post({**TEXT_PLAIN, "Origin": SAME_ORIGIN_HTTP}, scheme="https")
        assert response.status_code == CSRF_REJECTED
        assert response.json() == CSRF_BLOCKED_BODY

    def test_https_post_accepts_https_origin(self):
        response = _post({**TEXT_PLAIN, "Origin": SAME_ORIGIN_HTTPS}, scheme="https")
        assert response.status_code == BODY_VALIDATION

    def test_wss_rejects_http_origin(self, ws_gates_only):
        assert _ws(SAME_ORIGIN_HTTP, scheme="wss") == WS_ORIGIN_REJECTED

    def test_wss_accepts_https_origin(self, ws_gates_only):
        assert _ws(SAME_ORIGIN_HTTPS, scheme="wss") == (WS_ACCEPTED, WS_TERMINAL_NOT_FOUND)
