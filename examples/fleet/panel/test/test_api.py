import httpx
from fastapi.testclient import TestClient
from app import client, main


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
