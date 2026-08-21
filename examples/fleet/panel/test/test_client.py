import httpx
import pytest
from app import client


def _mock(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://x")


async def test_list_sessions_parses():
    def handler(req):
        assert req.url.path == "/sessions"
        return httpx.Response(200, json=[{"id": "cao-abc"}])
    async with _mock(handler) as c:
        out = await client.list_sessions(c, "http://x")
    assert out == [{"id": "cao-abc"}]


async def test_launch_sends_expected_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(201, json={"id": "term-1", "session_name": "sess-1"})
    async with _mock(handler) as c:
        out = await client.launch(c, "http://x", "developer", "claude_code", "sess-1", "/tmp")
    assert seen["path"] == "/sessions"
    assert seen["params"]["agent_profile"] == "developer"
    assert seen["params"]["provider"] == "claude_code"
    assert seen["params"]["session_name"] == "sess-1"
    assert seen["params"]["working_directory"] == "/tmp"
    assert out["id"] == "term-1"


async def test_launch_omits_optional_params_when_absent():
    seen = {}
    def handler(req):
        seen["params"] = dict(req.url.params)
        return httpx.Response(201, json={"id": "term-1"})
    async with _mock(handler) as c:
        # no provider, no working_directory → those keys must be absent, not empty
        await client.launch(c, "http://x", "developer", None, "sess-1")
    assert seen["params"]["agent_profile"] == "developer"
    assert seen["params"]["session_name"] == "sess-1"
    assert "provider" not in seen["params"]
    assert "working_directory" not in seen["params"]


async def test_send_message_path_and_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={})
    async with _mock(handler) as c:
        await client.send_message(c, "http://x", "term-1", "hello")
    assert seen["path"] == "/terminals/term-1/inbox/messages"
    assert seen["params"]["message"] == "hello"
    assert seen["params"]["sender_id"] == "fleet-panel"


async def test_http_error_raises():
    def handler(req):
        return httpx.Response(404, json={"detail": "nope"})
    async with _mock(handler) as c:
        with pytest.raises(httpx.HTTPStatusError):
            await client.get_session(c, "http://x", "missing")


async def test_get_screen_path_and_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"screen": "frame", "ansi": True})
    async with _mock(handler) as c:
        out = await client.get_screen(c, "http://x", "abcd1234", ansi=True)
    assert seen["path"] == "/terminals/abcd1234/screen"
    assert seen["params"]["ansi"] == "true"
    assert out["screen"] == "frame"


async def test_send_key_path_and_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"success": True})
    async with _mock(handler) as c:
        await client.send_key(c, "http://x", "abcd1234", "C-c")
    assert seen["path"] == "/terminals/abcd1234/key"
    assert seen["params"]["key"] == "C-c"


async def test_send_input_path_and_params():
    seen = {}
    def handler(req):
        seen["path"] = req.url.path
        seen["params"] = dict(req.url.params)
        return httpx.Response(200, json={"success": True})
    async with _mock(handler) as c:
        await client.send_input(c, "http://x", "abcd1234", "ls -la")
    assert seen["path"] == "/terminals/abcd1234/input"
    assert seen["params"]["message"] == "ls -la"
    assert seen["params"]["sender_id"] == "fleet-panel"


async def test_list_providers_path():
    def handler(req):
        assert req.url.path == "/agents/providers"
        return httpx.Response(200, json=[{"name": "claude_code", "installed": True}])
    async with _mock(handler) as c:
        out = await client.list_providers(c, "http://x")
    assert out[0]["name"] == "claude_code"


async def test_list_profiles_path():
    def handler(req):
        assert req.url.path == "/agents/profiles"
        return httpx.Response(200, json=[{"name": "developer"}])
    async with _mock(handler) as c:
        out = await client.list_profiles(c, "http://x")
    assert out[0]["name"] == "developer"


async def test_working_directory_path():
    def handler(req):
        assert req.url.path == "/terminals/abcd1234/working-directory"
        return httpx.Response(200, json={"working_directory": "/work"})
    async with _mock(handler) as c:
        out = await client.working_directory(c, "http://x", "abcd1234")
    assert out["working_directory"] == "/work"
