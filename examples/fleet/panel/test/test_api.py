import json
import os

import httpx
from fastapi.testclient import TestClient
from app import client, config, main


def _patch_fleet(monkeypatch, online_health, sessions_by_machine):
    async def fake_health(c, base):
        if base in online_health:
            return online_health[base]
        raise httpx.ConnectError("down", request=httpx.Request("GET", base))

    async def fake_list(c, base):
        return sessions_by_machine.get(base, [])

    monkeypatch.setattr(client, "health", fake_health)
    monkeypatch.setattr(client, "list_sessions", fake_list)


def test_fleet_aggregates_and_isolates_offline(monkeypatch):
    from app import config
    node_a = next(m for m in config.load_machines() if m["name"] == "node-a")
    node_a_base = config.base_url(node_a)
    _patch_fleet(
        monkeypatch,
        online_health={node_a_base: {"status": "ok", "components": {"claude": "ok"}}},
        sessions_by_machine={node_a_base: [{"id": "cao-x"}]},
    )
    tc = TestClient(main.app)
    data = tc.get("/api/fleet").json()
    by_name = {m["name"]: m for m in data["machines"]}
    assert by_name["node-a"]["online"] is True
    assert by_name["node-a"]["claude"] == "ok"
    assert by_name["node-a"]["sessions"] == [{"id": "cao-x"}]
    # a node whose health raised is reported offline, not a 500
    assert by_name["node-b"]["online"] is False


def test_unknown_machine_404():
    tc = TestClient(main.app)
    assert tc.post("/api/machines/nope/launch", json={}).status_code == 404


def test_screen_proxy_ok(monkeypatch):
    async def fake_screen(c, base, tid, ansi=True):
        return {"screen": "FRAME", "ansi": True}
    monkeypatch.setattr(client, "get_screen", fake_screen)
    tc = TestClient(main.app)
    r = tc.get("/api/machines/node-a/terminals/abcd1234/screen")
    assert r.status_code == 200
    assert r.json()["screen"] == "FRAME"


def test_screen_proxy_unknown_machine_404():
    tc = TestClient(main.app)
    assert tc.get("/api/machines/nope/terminals/abcd1234/screen").status_code == 404


def test_key_proxy_ok(monkeypatch):
    seen = {}
    async def fake_key(c, base, tid, key):
        seen["key"] = key
        return {"success": True}
    monkeypatch.setattr(client, "send_key", fake_key)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/terminals/abcd1234/key", json={"key": "C-c"})
    assert r.status_code == 200
    assert seen["key"] == "C-c"


def test_key_proxy_rejects_missing_key():
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/terminals/abcd1234/key", json={})
    assert r.status_code == 400


def test_input_proxy_ok(monkeypatch):
    seen = {}
    async def fake_input(c, base, tid, text):
        seen["text"] = text
        return {"success": True}
    monkeypatch.setattr(client, "send_input", fake_input)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/terminals/abcd1234/input", json={"text": "ls"})
    assert r.status_code == 200
    assert seen["text"] == "ls"


def test_screen_proxy_404_fallback(monkeypatch):
    req = httpx.Request("GET", "http://fake/screen")
    async def fake_screen(c, base, tid, ansi=True):
        raise httpx.HTTPStatusError("not found", request=req, response=httpx.Response(404, request=req))
    async def fake_output(c, base, tid, mode):
        return {"output": "TAIL"}
    monkeypatch.setattr(client, "get_screen", fake_screen)
    monkeypatch.setattr(client, "terminal_output", fake_output)
    tc = TestClient(main.app)
    r = tc.get("/api/machines/node-a/terminals/abcd1234/screen")
    assert r.status_code == 200
    data = r.json()
    assert data["screen"] == "TAIL"
    assert data["ansi"] is False
    assert data["fallback"] is True


def test_input_proxy_rejects_missing_text():
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/terminals/abcd1234/input", json={})
    assert r.status_code == 400


def test_providers_proxy_ok(monkeypatch):
    async def fake(c, base):
        return [{"name": "claude_code", "installed": True}, {"name": "codex", "installed": False}]
    monkeypatch.setattr(client, "list_providers", fake)
    tc = TestClient(main.app)
    r = tc.get("/api/machines/node-a/providers")
    assert r.status_code == 200
    assert r.json()[0]["name"] == "claude_code"


def test_profiles_proxy_ok(monkeypatch):
    async def fake(c, base):
        return [{"name": "developer"}, {"name": "reviewer"}]
    monkeypatch.setattr(client, "list_profiles", fake)
    tc = TestClient(main.app)
    r = tc.get("/api/machines/node-a/profiles")
    assert r.status_code == 200
    assert r.json()[1]["name"] == "reviewer"


def test_working_directory_proxy_ok(monkeypatch):
    async def fake(c, base, tid):
        return {"working_directory": "/work/proj"}
    monkeypatch.setattr(client, "working_directory", fake)
    tc = TestClient(main.app)
    r = tc.get("/api/machines/node-a/terminals/abcd1234/working-directory")
    assert r.status_code == 200
    assert r.json()["working_directory"] == "/work/proj"


def test_providers_proxy_unknown_machine_404():
    tc = TestClient(main.app)
    assert tc.get("/api/machines/nope/providers").status_code == 404


# --- launch route ----------------------------------------------------------

def _err(kind, status=None):
    req = httpx.Request("POST", "http://x")
    if kind == "status":
        return httpx.HTTPStatusError("boom", request=req,
                                     response=httpx.Response(status, text="upstream said no", request=req))
    return httpx.ConnectError("down", request=req)


def test_launch_autogenerates_session_name_and_delivers_task(monkeypatch):
    launched = {}
    async def fake_launch(c, base, profile, provider, session_name, wd=None):
        launched.update(profile=profile, provider=provider, session_name=session_name, wd=wd)
        return {"id": "term-9"}
    sent = {}
    async def fake_send(c, base, tid, msg, sender_id="fleet-panel"):
        sent.update(tid=tid, msg=msg)
        return {}
    monkeypatch.setattr(client, "launch", fake_launch)
    monkeypatch.setattr(client, "send_message", fake_send)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/launch", json={"task": "do X", "working_directory": "/w"})
    assert r.status_code == 200
    data = r.json()
    assert data["terminal_id"] == "term-9"
    assert data["task_sent"] is True
    # session name is auto-generated with the renamed prefix (was "cao-panel-")
    assert data["session_name"].startswith("fleet-panel-")
    assert launched["session_name"] == data["session_name"]
    assert launched["profile"] == "developer" and launched["provider"] == "claude_code"
    assert launched["wd"] == "/w"
    assert sent["tid"] == "term-9" and sent["msg"] == "do X"


def test_launch_uses_provided_session_name_and_skips_task(monkeypatch):
    async def fake_launch(c, base, profile, provider, session_name, wd=None):
        return {"id": "term-1"}
    def _boom(*a, **k):
        raise AssertionError("send_message must not be called when no task is given")
    monkeypatch.setattr(client, "launch", fake_launch)
    monkeypatch.setattr(client, "send_message", _boom)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/launch", json={"session_name": "my-sess"})
    assert r.status_code == 200
    body = r.json()
    assert body["session_name"] == "my-sess"
    assert body["task_sent"] is False


def test_launch_maps_upstream_status_error_to_502(monkeypatch):
    async def fake_launch(c, base, *a, **k):
        raise _err("status", 400)
    monkeypatch.setattr(client, "launch", fake_launch)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/launch", json={})
    assert r.status_code == 502
    assert "upstream said no" in r.json()["detail"]


def test_launch_maps_transport_error_to_502(monkeypatch):
    async def fake_launch(c, base, *a, **k):
        raise _err("transport")
    monkeypatch.setattr(client, "launch", fake_launch)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/launch", json={})
    assert r.status_code == 502


def test_launch_survives_task_delivery_failure(monkeypatch):
    async def fake_launch(c, base, *a, **k):
        return {"id": "term-2"}
    async def fake_send(c, base, tid, msg, sender_id="fleet-panel"):
        raise _err("transport")
    monkeypatch.setattr(client, "launch", fake_launch)
    monkeypatch.setattr(client, "send_message", fake_send)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/launch", json={"task": "do X"})
    assert r.status_code == 200            # launch succeeded even though the task didn't land
    assert r.json()["task_sent"] is False


# --- send route ------------------------------------------------------------

def test_send_requires_message():
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/sessions/s1/send", json={})
    assert r.status_code == 400


def test_send_404_when_session_has_no_terminals(monkeypatch):
    async def fake_detail(c, base, name):
        return {"terminals": []}
    monkeypatch.setattr(client, "get_session", fake_detail)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/sessions/s1/send", json={"message": "hi"})
    assert r.status_code == 404


def test_send_happy_path(monkeypatch):
    async def fake_detail(c, base, name):
        return {"terminals": [{"id": "term-7"}]}
    seen = {}
    async def fake_send(c, base, tid, msg, sender_id="fleet-panel"):
        seen.update(tid=tid, msg=msg)
        return {}
    monkeypatch.setattr(client, "get_session", fake_detail)
    monkeypatch.setattr(client, "send_message", fake_send)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/sessions/s1/send", json={"message": "hello"})
    assert r.status_code == 200
    data = r.json()
    assert data["sent"] is True and data["terminal_id"] == "term-7"
    assert seen == {"tid": "term-7", "msg": "hello"}


def test_send_502_when_session_lookup_fails(monkeypatch):
    async def fake_detail(c, base, name):
        raise _err("transport")
    monkeypatch.setattr(client, "get_session", fake_detail)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/sessions/s1/send", json={"message": "hi"})
    assert r.status_code == 502


# --- shutdown route --------------------------------------------------------

def test_shutdown_happy_path(monkeypatch):
    async def fake_shutdown(c, base, name):
        return {"stopped": name}
    monkeypatch.setattr(client, "shutdown", fake_shutdown)
    tc = TestClient(main.app)
    r = tc.post("/api/machines/node-a/sessions/s1/shutdown")
    assert r.status_code == 200
    assert r.json()["stopped"] == "s1"


def test_shutdown_502_on_error(monkeypatch):
    async def fake_shutdown(c, base, name):
        raise _err("transport")
    monkeypatch.setattr(client, "shutdown", fake_shutdown)
    tc = TestClient(main.app)
    assert tc.post("/api/machines/node-a/sessions/s1/shutdown").status_code == 502


# --- path-segment validation ----------------------------------------------

def test_rejects_unsafe_session_name():
    tc = TestClient(main.app)
    # a space is outside [A-Za-z0-9._-]; rejected before proxying upstream
    assert tc.get("/api/machines/node-a/sessions/bad%20name").status_code == 400


def test_rejects_unsafe_terminal_id():
    tc = TestClient(main.app)
    assert tc.get("/api/machines/node-a/terminals/bad;id/screen").status_code == 400


# --- node pass-through -----------------------------------------------------
#
# Upstream is an httpx.MockTransport rather than a fake client, so the assertions
# are made against real httpx.Request objects — the URL, headers and body the
# node would actually receive.

def _stream(*chunks):
    """An async byte stream, which is what an unread streaming response holds.

    A response built with eager `content=`/`json=` is already marked consumed, so
    `aiter_raw()` on it raises StreamConsumed — an artefact of the mock, not of
    the proxy: a real transport hands back an unread body.
    """
    async def gen():
        for chunk in chunks:
            yield chunk
    return gen()


def _upstream_json(status, payload, headers=None):
    return httpx.Response(
        status,
        content=_stream(json.dumps(payload).encode()),
        headers={"content-type": "application/json", **(headers or {})},
    )


def _mock_upstream(monkeypatch, handler):
    """Route every client the app builds to `handler`. Returns the seen requests."""
    seen = []
    real = httpx.AsyncClient

    def transport_handler(request):
        seen.append(request)
        return handler(request)

    def factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(transport_handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(main.httpx, "AsyncClient", factory)
    return seen


def test_proxy_forwards_to_the_named_node(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(200, {"ok": True}))
    tc = TestClient(main.app)
    r = tc.get("/nodes/node-b/sessions?limit=5")
    assert r.status_code == 200
    assert r.json() == {"ok": True}
    # node-b's registry entry, not node-a's, and the query string survives
    assert str(seen[0].url) == "http://100.64.0.12:9889/sessions?limit=5"


def test_proxy_unknown_node_404(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(200, {}))
    tc = TestClient(main.app)
    assert tc.get("/nodes/nope/sessions").status_code == 404
    assert seen == []  # rejected before any request left the panel


def test_proxy_rejects_unlisted_namespace(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(200, {}))
    tc = TestClient(main.app)
    # the agent-facing memory RPC and the AG-UI transport are not browser calls
    for path in ("internal/memory/store", "agui/v1/run", ".well-known/x", "events"):
        assert tc.get(f"/nodes/node-a/{path}").status_code == 404, path
    assert seen == []


def test_proxy_rejects_dot_segments(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(200, {}))
    tc = TestClient(main.app)
    # passes the namespace check, then would climb out of it upstream
    # percent-encoded, so no client on the way in normalises it away
    r = tc.get("/nodes/node-a/sessions/%2e%2e/internal/memory/store")
    assert r.status_code == 400
    assert seen == []


def test_proxy_never_forwards_the_panel_credential(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(200, {}))
    tc = TestClient(main.app)
    tc.get(
        "/nodes/node-a/sessions",
        headers={
            "Authorization": "Bearer panel-token",
            "Cookie": "session=abc",
            "Accept": "application/json",
        },
    )
    # report only WHETHER the header travelled, never its value
    assert "authorization" not in seen[0].headers
    assert "cookie" not in seen[0].headers
    assert seen[0].headers["accept"] == "application/json"
    # Host is the node's, set by httpx from the upstream URL — not the browser's
    assert seen[0].headers["host"] == "100.64.0.11:9889"


def test_proxy_forwards_method_body_and_content_type(monkeypatch):
    seen = _mock_upstream(monkeypatch, lambda r: _upstream_json(201, {"id": "t1"}))
    tc = TestClient(main.app)
    r = tc.post("/nodes/node-a/terminals/t1/input", json={"text": "hi"})
    assert r.status_code == 201
    assert seen[0].method == "POST"
    assert seen[0].headers["content-type"] == "application/json"
    assert json.loads(seen[0].read()) == {"text": "hi"}


def test_proxy_passes_upstream_status_and_detail_through(monkeypatch):
    _mock_upstream(monkeypatch, lambda r: _upstream_json(404, {"detail": "no such terminal"}))
    tc = TestClient(main.app)
    r = tc.get("/nodes/node-a/terminals/gone/output")
    # the node's own answer, not a 502 wrapper — the dashboard branches on this
    assert r.status_code == 404
    assert r.json()["detail"] == "no such terminal"


def test_proxy_drops_hop_by_hop_response_headers(monkeypatch):
    def handler(request):
        return _upstream_json(
            200, {}, headers={"Connection": "keep-alive", "X-Cao-Node": "kept"}
        )
    _mock_upstream(monkeypatch, handler)
    tc = TestClient(main.app)
    r = tc.get("/nodes/node-a/sessions")
    # hop-by-hop describes the panel<->node connection, not this one
    assert "connection" not in {k.lower() for k in r.headers}
    assert r.headers["x-cao-node"] == "kept"
    assert r.headers["content-type"].startswith("application/json")


def test_proxy_streams_an_event_stream(monkeypatch):
    frames = [b"event: step.started\ndata: {\"seq\": 1}\n\n", b"data: {\"seq\": 2}\n\n"]

    def handler(request):
        assert request.headers["accept"] == "text/event-stream"
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream(*frames),
        )

    _mock_upstream(monkeypatch, handler)
    tc = TestClient(main.app)
    with tc.stream(
        "GET",
        "/nodes/node-a/workflows/runs/r1/events",
        headers={"Accept": "text/event-stream"},
    ) as r:
        assert r.status_code == 200
        assert r.headers["content-type"] == "text/event-stream"
        assert b"".join(r.iter_bytes()) == b"".join(frames)


def test_proxy_timeout_matches_the_call():
    # a stream may go quiet for minutes; only a read timeout would kill it
    assert main._proxy_timeout("GET", "workflows/runs/r1/events", "text/event-stream").read is None
    # session launch blocks on the agent CLI reaching a ready prompt
    assert main._proxy_timeout("POST", "sessions", "") is client.LAUNCH_TIMEOUT
    assert main._proxy_timeout("GET", "sessions", "") is client.TIMEOUT


def test_proxy_offline_node_502(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("down", request=request)
    _mock_upstream(monkeypatch, handler)
    tc = TestClient(main.app)
    r = tc.get("/nodes/node-c/sessions")
    assert r.status_code == 502
    assert "node-c" in r.json()["detail"]


# --- front end -------------------------------------------------------------
#
# The panel serves CAO's own dashboard from `web_ui/` when the image has built
# it, and its own `static/` UI otherwise. These tests are written against
# whichever one is on disk, so they pass in a bare checkout (no build) and in
# the image alike.

def test_index_serves_the_resolved_front_end():
    tc = TestClient(main.app)
    r = tc.get("/")
    assert r.status_code == 200
    with open(os.path.join(main._UI_ROOT, "index.html"), "rb") as f:
        assert r.content == f.read()


def test_ui_root_prefers_web_ui_only_when_built():
    # The selection is a file check, not a guess: a checkout with no build must
    # fall back rather than serve 404 for every asset.
    built = os.path.isfile(os.path.join(main._WEB_UI, "index.html"))
    assert main._UI_ROOT == (main._WEB_UI if built else main._STATIC)


def test_root_asset_served_by_name():
    # index.html is present under either front end, so it is the one root-level
    # file this test can name without assuming a build.
    tc = TestClient(main.app)
    assert tc.get("/index.html").status_code == 200


def test_root_asset_rejects_traversal():
    tc = TestClient(main.app)
    # encoded separators, and a name the charset disallows outright
    for path in ("/..%2f..%2fapp%2fmain.py", "/%2e%2e%2fpyproject.toml", "/.env"):
        assert tc.get(path).status_code == 404, path


def test_root_asset_cannot_read_outside_the_ui_root():
    # pyproject.toml sits in the panel root, one level above _UI_ROOT. The name
    # satisfies the charset, so only the containment check keeps it out.
    tc = TestClient(main.app)
    assert tc.get("/pyproject.toml").status_code == 404


def test_root_asset_does_not_shadow_the_api(monkeypatch):
    # The catch-all is the last route registered; an API path still routes to
    # its handler (404 here is "unknown machine", from the API, not the file
    # route — a 200 or a file body would mean the catch-all won).
    _patch_fleet(monkeypatch, online_health={}, sessions_by_machine={})
    tc = TestClient(main.app)
    assert tc.get("/api/fleet").status_code == 200
    assert tc.get("/api/machines/nope/providers").status_code == 404


# --- opt-in shared-token auth ---------------------------------------------

def test_open_when_no_token_configured():
    # default: CAO_PANEL_TOKEN unset → panel is open (fine on loopback)
    tc = TestClient(main.app)
    assert tc.get("/").status_code == 200
    assert tc.post("/api/machines/nope/launch", json={}).status_code == 404


def test_token_required_on_every_route(monkeypatch):
    monkeypatch.setattr(config, "PANEL_TOKEN", "s3cret")
    tc = TestClient(main.app)
    # both the page and the API demand credentials
    for r in (tc.get("/"), tc.post("/api/machines/nope/launch", json={})):
        assert r.status_code == 401
        assert r.headers["WWW-Authenticate"].startswith("Basic")


def test_token_accepts_basic_and_bearer(monkeypatch):
    monkeypatch.setattr(config, "PANEL_TOKEN", "s3cret")
    tc = TestClient(main.app)
    # Basic: any username, password is the token (browser-friendly)
    assert tc.post("/api/machines/nope/launch", json={}, auth=("panel", "s3cret")).status_code == 404
    # Bearer: for scripts
    assert tc.post("/api/machines/nope/launch", json={},
                   headers={"Authorization": "Bearer s3cret"}).status_code == 404


def test_token_rejects_wrong_secret(monkeypatch):
    monkeypatch.setattr(config, "PANEL_TOKEN", "s3cret")
    tc = TestClient(main.app)
    assert tc.post("/api/machines/nope/launch", json={}, auth=("panel", "nope")).status_code == 401
    assert tc.get("/", headers={"Authorization": "Bearer nope"}).status_code == 401
