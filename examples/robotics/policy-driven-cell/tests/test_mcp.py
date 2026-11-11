from __future__ import annotations

import asyncio
import re
import secrets
import socket
import threading
import time
from pathlib import Path

import demo
import pytest
import uvicorn
from cell import Cell, FakeBackend, Scene
from cell_mcp import controller_client, make_server
from fastmcp.exceptions import ToolError

EXAMPLE = Path(__file__).resolve().parents[1]
ROLES = {
    "supervisor": {"scopes": ["observe"], "role": "supervisor"},
    "checker": {"scopes": ["observe"], "role": "checker"},
    "humanoid": {"scopes": ["observe", "walk"], "role": "humanoid", "robot": "g1"},
    "arm": {"scopes": ["observe", "pick"], "role": "arm", "robot": "arm"},
    "operator": {"scopes": ["observe", "operate"], "role": "operator"},
}


@pytest.fixture
def controller(tmp_path):
    scene = Scene.model_validate_json((EXAMPLE / "cell.json").read_text())
    cell = Cell(scene, FakeBackend(), run_id="test", allow_motion=True)
    secrets_by_role = {role: secrets.token_urlsafe(32) for role in ROLES}
    tokens = {secrets_by_role[role]: {"client_id": role, **meta} for role, meta in ROLES.items()}
    app = make_server(cell, tokens, tmp_path / "last-state.json").http_app(stateless_http=True)
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws="none")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        deadline = time.monotonic() + 10
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "controller did not start"
            time.sleep(0.01)
        try:
            yield f"http://127.0.0.1:{port}/mcp", secrets_by_role
        finally:
            server.should_exit = True
            thread.join(timeout=10)


def call(url: str, token: str, tool: str, **arguments):
    async def run():
        async with controller_client(url, token) as client:
            return await client.call_tool(tool, arguments)

    return asyncio.run(run())


def tools(url: str, token: str) -> set[str]:
    async def run():
        async with controller_client(url, token) as client:
            return {tool.name for tool in await client.list_tools()}

    return asyncio.run(run())


def test_each_role_sees_only_its_tools(controller):
    url, token = controller
    assert tools(url, token["supervisor"]) == {"observe"}
    assert tools(url, token["checker"]) == {"observe"}
    assert tools(url, token["humanoid"]) == {"observe", "walk_to"}
    assert tools(url, token["arm"]) == {"observe", "pick_and_place"}
    assert tools(url, token["operator"]) == {"observe", "stop_cell"}


def test_a_credential_commands_only_its_own_robot(controller):
    url, token = controller
    with pytest.raises(ToolError):
        call(url, token["humanoid"], "walk_to", command_id="w1", robot="g2", location="dock")
    accepted = call(
        url, token["humanoid"], "walk_to", command_id="w1", robot="g1", location="inspection"
    )
    assert accepted.data["status"] in ("accepted", "running", "succeeded")


def test_the_interlock_refusal_reaches_the_agent_as_a_tool_error(controller):
    url, token = controller
    with pytest.raises(ToolError, match="interlock"):
        call(
            url,
            token["arm"],
            "pick_and_place",
            command_id="p1",
            robot="arm",
            part="part_a",
            bin="pass_bin",
        )


def test_profiles_document_exactly_the_tools_their_role_can_call():
    expected = {
        "supervisor": {"observe"},
        "checker": {"observe"},
        "humanoid": {"observe", "walk_to"},
        "arm": {"observe", "pick_and_place"},
    }
    for role, names in expected.items():
        _, body = demo.read_profile(demo.PROFILE_SOURCES[role])
        if "### cell-sim" not in body:
            documented = set(re.findall(r"`(\w+)`", body)) & {
                "observe",
                "walk_to",
                "pick_and_place",
                "stop_cell",
            }
        else:
            section = body.split("### cell-sim", 1)[1].split("\n### ", 1)[0].split("\n## ", 1)[0]
            documented = set(re.findall(r"\*\*(\w+)\*\*\(", section))
        assert documented == names, (role, documented)


def test_prepare_writes_one_credential_and_profile_per_agent(tmp_path):
    manifest = demo.prepare(
        tmp_path / "run", EXAMPLE / "cell.json", port=8767, provider="claude_code"
    )
    assert set(manifest["actors"]) == {
        "supervisor",
        "checker",
        "operator",
        "humanoid_g1",
        "arm_arm",
    }
    for actor in manifest["actors"]:
        credential = tmp_path / "run" / "credentials" / f"{actor}.json"
        assert credential.stat().st_mode & 0o777 == 0o600
    profiles = list((tmp_path / "run" / "profiles").glob("*.md"))
    assert len(profiles) == 4
    for profile in profiles:
        front, _ = demo.read_profile(profile)
        assert front["provider"] == "claude_code"
        assert front["mcpServers"]["cell-sim"]["args"][-1].endswith(".json")
    with pytest.raises(ValueError, match="not supported"):
        demo.prepare(tmp_path / "run2", EXAMPLE / "cell.json", port=8767, provider="hermes")
