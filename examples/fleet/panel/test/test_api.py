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
