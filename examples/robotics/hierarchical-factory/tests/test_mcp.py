from __future__ import annotations

import asyncio
import json
import re
import secrets
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import demo
import httpx2
import pytest
import uvicorn
from factory_mcp import controller_client, make_server, save_snapshot
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from simulation import Scene, World

EXAMPLE = Path(__file__).resolve().parents[1]
MANAGERS = {"west": "line", "middle": "line", "east": "ship"}
ROLE_OF = {"west": "serial", "middle": "parallel", "east": "formation"}


@pytest.fixture
def controller(tmp_path):
    scene = Scene.model_validate_json((EXAMPLE / "factory.json").read_text())
    world = World(scene, allow_motion=True)
    credentials = {
        name: SecretStr(secrets.token_urlsafe(32))
        for name in ("west", "middle", "east", "supervisor", "operator")
    }
    tokens = {
        credentials[zone].get_secret_value(): {
            "client_id": f"manager_{zone}",
            "scopes": ["observe", "act", scope],
            "zone": zone,
        }
        for zone, scope in MANAGERS.items()
    }
    tokens[credentials["supervisor"].get_secret_value()] = {
        "client_id": "supervisor",
        "scopes": ["observe"],
        "role": "supervisor",
    }
    tokens[credentials["operator"].get_secret_value()] = {
        "client_id": "operator",
        "scopes": ["observe", "operate"],
        "role": "operator",
    }
    snapshot = tmp_path / "last-state.json"
    # As demo.py serve runs it: Host and Origin are checked on every request.
    app = make_server(world, tokens, snapshot).http_app(
        stateless_http=True, host_origin_protection=True
    )
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", ws="none")
        )
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        thread.start()
        try:
            deadline = time.monotonic() + 10
            while not server.started:
                if not thread.is_alive() or time.monotonic() >= deadline:
                    pytest.fail("the test MCP controller did not become ready")
                time.sleep(0.01)
            yield f"http://127.0.0.1:{port}/mcp", credentials, world, snapshot
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive(), "the owned MCP controller did not shut down"
    assert json.loads(snapshot.read_text())["stopped"] is True


def documented_tools(role):
    """Return the factory-sim tools, with parameters, that a committed profile documents."""
    _, body = demo.read_profile(demo.PROFILE_SOURCES[role])
    section = body.split("### factory-sim", 1)[1]
    section = section.split("\n### ", 1)[0].split("\n## ", 1)[0]
    return {
        name: [parameter.strip() for parameter in parameters.split(",") if parameter.strip()]
        for name, parameters in re.findall(r"\*\*(\w+)\*\*\(([^)]*)\)", section)
    }


def call(url, credential, tool, arguments=None):
    async def exercise():
        async with controller_client(url, credential.get_secret_value()) as client:
            return (await client.call_tool(tool, arguments or {})).data

    return asyncio.run(exercise())


def test_profiles_document_exactly_the_tools_of_their_credential(controller):
    url, credentials, _, _ = controller

    async def exercise():
        for credential, role in (("supervisor", "supervisor"), *ROLE_OF.items()):
            async with controller_client(url, credentials[credential].get_secret_value()) as client:
                tools = {
                    tool.name: list(tool.input_schema.get("properties", {}))
                    for tool in await client.list_tools()
                }
            assert documented_tools(role) == tools, role

    asyncio.run(exercise())


def test_the_supervisor_observes_but_cannot_command_a_robot(controller):
    url, credentials, world, _ = controller

    async def exercise():
        async with controller_client(url, credentials["supervisor"].get_secret_value()) as client:
            assert {tool.name for tool in await client.list_tools()} == {"observe"}
            state = (await client.call_tool("observe")).data
            assert state["run_id"] == world.run_id
            for tool, arguments in (
                ("form_team", {"command_id": "impersonate", "formation": "queue"}),
                ("start_line", {"command_id": "line", "parts": 8, "cycle_seconds": 2}),
                ("set_pace", {"command_id": "pace", "cycle_seconds": 4}),
                (
                    "start_shipping",
                    {"command_id": "ship", "units": 8, "batch": 2, "routine": ["run"]},
                ),
                ("wait_team", {}),
                ("stop_simulation", {}),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(tool, arguments)
        assert world.observe()["commands"] == []
        assert not world.stopped

    asyncio.run(exercise())


def test_the_team_of_a_manager_comes_from_its_credential_not_an_argument(controller):
    url, credentials, world, _ = controller

    async def exercise():
        async with controller_client(url, credentials["east"].get_secret_value()) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "observe",
                "form_team",
                "start_shipping",
                "wait_team",
            }
            # Team C asks for the formation of Team A: the controller checks Team C.
            result = await client.call_tool(
                "form_team", {"command_id": "other-team", "formation": "queue"}
            )
            assert result.data["reason"] == "formation_unavailable"
            for tool, arguments in (
                ("start_line", {"command_id": "line", "parts": 8, "cycle_seconds": 2}),
                ("set_pace", {"command_id": "pace", "cycle_seconds": 4}),
                ("stop_simulation", {}),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(tool, arguments)
        assert world.observe()["commands"][0]["actor"] == "east"
        assert world.team_status("west")["status"] == "idle"

    asyncio.run(exercise())


def test_a_line_manager_cannot_ship_and_its_team_is_its_own(controller):
    url, credentials, world, _ = controller

    async def exercise():
        async with controller_client(url, credentials["west"].get_secret_value()) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "observe",
                "form_team",
                "start_line",
                "set_pace",
                "wait_team",
            }
            with pytest.raises(ToolError):
                await client.call_tool(
                    "start_shipping",
                    {"command_id": "ship", "units": 2, "batch": 2, "routine": ["run"]},
                )
            result = await client.call_tool(
                "form_team", {"command_id": "form-1", "formation": "queue"}
            )
            assert result.data["status"] == "accepted"
        assert world.team_status("west")["status"] == "forming"
        assert world.team_status("middle")["status"] == "idle"

    asyncio.run(exercise())


def test_wait_team_reports_formed_blocked_and_timeout(controller):
    url, credentials, world, _ = controller

    async def exercise():
        clients = {
            zone: controller_client(url, credentials[zone].get_secret_value()) for zone in ROLE_OF
        }
        async with clients["west"] as west, clients["middle"] as middle:
            assert (await west.call_tool("wait_team", {"timeout_seconds": 1})).data[
                "event"
            ] == "no_program"
            for client, formation in ((west, "queue"), (middle, "columns")):
                await client.call_tool(
                    "form_team", {"command_id": "form-1", "formation": formation}
                )
            for client in (west, middle):
                event = None
                while event != "formed":
                    event = (await client.call_tool("wait_team", {"timeout_seconds": 10})).data[
                        "event"
                    ]
                    assert event in ("formed", "timeout")
            # Team B never starts, so the buffer fills, and Team A blocks.
            started = await west.call_tool(
                "start_line", {"command_id": "line-1", "parts": 4, "cycle_seconds": 2}
            )
            assert started.data["status"] == "running"
            short = await west.call_tool("wait_team", {"until": "done", "timeout_seconds": 1})
            assert short.data["event"] == "timeout"
            event = None
            while event != "blocked":
                result = await west.call_tool(
                    "wait_team", {"until": "done_or_blocked", "timeout_seconds": 20}
                )
                event = result.data["event"]
                assert event in ("blocked", "timeout")
            team = result.data["team"]
            assert team["status"] == "blocked"
            assert team["program"]["blocked_for_seconds"] >= world.scene.blocked_report_seconds
            paced = await west.call_tool("set_pace", {"command_id": "pace-1", "cycle_seconds": 6})
            assert paced.data["status"] == "finished"
            assert world.team_status("west")["cycle_seconds"] == 6

    asyncio.run(exercise())


def test_each_tool_call_is_logged_with_its_calling_agent(controller, caplog):
    # The README guide shows what each agent does from these controller lines.
    url, credentials, _, _ = controller
    with caplog.at_level("INFO", logger="factory"):
        call(url, credentials["middle"], "form_team", {"command_id": "f-1", "formation": "columns"})
        call(url, credentials["supervisor"], "observe")
    assert "middle team manager called form_team(command_id=f-1, formation=columns)" in caplog.text
    assert "supervisor called observe()" in caplog.text


def test_the_controller_outlives_a_manager_and_a_reconnect_does_not_replay(controller):
    url, credentials, world, _ = controller
    arguments = {"command_id": "persistent", "formation": "diamond"}
    assert call(url, credentials["east"], "form_team", arguments)["status"] == "accepted"
    deadline = time.monotonic() + 15
    while world.command("east", "persistent")["status"] != "finished":
        assert time.monotonic() < deadline, "the formation did not form"
        time.sleep(0.05)
    assert call(url, credentials["east"], "form_team", arguments)["status"] == "finished"
    assert len(world.observe()["commands"]) == 1


def test_operator_stops_without_a_manager_or_supervisor_inbox(controller):
    url, credentials, world, snapshot = controller
    call(url, credentials["west"], "form_team", {"command_id": "interrupt", "formation": "queue"})
    stopped = call(url, credentials["operator"], "stop_simulation")
    assert stopped["stopped"] is True
    assert stopped["commands"][0]["status"] == "interrupted"
    pose = stopped["teams"]["west"]["robots"]["a1"]["xy"]
    time.sleep(0.1)
    after = call(url, credentials["operator"], "observe")
    assert after["teams"]["west"]["robots"]["a1"]["xy"] == pose
    assert json.loads(snapshot.read_text())["teams"]["west"]["robots"]["a1"]["xy"] == pose
    assert call(url, credentials["west"], "wait_team", {"timeout_seconds": 1})["event"] == "stopped"
    assert world.stopped


def test_missing_or_wrong_credential_is_not_read_access(controller):
    url, _, _, _ = controller
    with httpx2.Client(trust_env=False) as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post(url, json=request).status_code == 401
        assert (
            client.post(
                url, json=request, headers={"Authorization": "Bearer not-a-valid-credential"}
            ).status_code
            == 401
        )


def test_the_live_view_serves_only_the_page_and_the_measured_state(controller):
    url, credentials, world, _ = controller
    base = url[: -len("/mcp")]
    call(url, credentials["west"], "form_team", {"command_id": "form-1", "formation": "queue"})
    with httpx2.Client(trust_env=False) as client:
        page = client.get(base + "/")
        assert page.status_code == 200
        assert page.headers["content-type"].startswith("text/html")
        # Page-relative, so the page also works behind the path prefix of a port forward.
        assert 'fetch("state"' in page.text and 'fetch("/state"' not in page.text
        # The robot pictures are inlined: one for each team color, and a blocked one.
        assert "__ROBOT_SPRITES__" not in page.text
        assert page.text.count("data:image/png;base64,") == 5
        # No external assets: the only URL in the page is the SVG namespace.
        assert set(re.findall(r"https?://[^\s\"')]+", page.text)) == {"http://www.w3.org/2000/svg"}
        state = client.get(base + "/state")
        assert state.status_code == 200
        data = state.json()
        assert data["run_id"] == world.run_id
        assert data["commands"][0]["command_id"] == "form-1"
        assert set(data["teams"]) == {"west", "middle", "east"}
        assert all("xy" in part for part in data["parts"].values())
        for credential in credentials.values():
            assert credential.get_secret_value() not in state.text
        for path in ("/observe", "/credentials", "/run.json", "/viewer.html"):
            assert client.get(base + path).status_code == 404
        # The live view is read-only: it accepts no command.
        assert client.post(base + "/state", json={}).status_code == 405


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8767/mcp",
        "http://localhost:8767/mcp",
        "http://192.0.2.1:8767/mcp",
        "http://127.0.0.1:8767/robot",
        "http://127.0.0.1:8767/mcp?next=external",
        "http://name@127.0.0.1:8767/mcp",
    ],
)
def test_relay_cannot_be_redirected_to_hardware_or_another_host(url):
    with pytest.raises(ValueError, match="127.0.0.1"):
        controller_client(url, "unused")


def test_relay_does_not_use_an_environment_proxy(controller, monkeypatch):
    url, credentials, _, _ = controller
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    assert call(url, credentials["supervisor"], "observe")["position_units"] == "m"


def test_concurrent_snapshots_do_not_share_a_temporary_file(tmp_path, monkeypatch):
    snapshot = tmp_path / "last-state.json"
    states = [{"stopped": True, "observed_at": str(index)} for index in range(2)]
    ready = threading.Barrier(2)
    replace = Path.replace

    def simultaneous_replace(source, target):
        ready.wait(timeout=5)
        return replace(source, target)

    monkeypatch.setattr(Path, "replace", simultaneous_replace)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(save_snapshot, snapshot, state) for state in states]
        for future in futures:
            future.result(timeout=10)
    assert json.loads(snapshot.read_text()) in states
    assert list(tmp_path.iterdir()) == [snapshot]


def test_failed_snapshot_preserves_previous_state_and_removes_temporary_file(tmp_path, monkeypatch):
    snapshot = tmp_path / "last-state.json"
    original = {"stopped": True}
    save_snapshot(snapshot, original)

    def fail_replace(source, target):
        raise OSError("snapshot replacement failed")

    monkeypatch.setattr(Path, "replace", fail_replace)
    with pytest.raises(OSError, match="snapshot replacement failed"):
        save_snapshot(snapshot, {"stopped": False})
    assert json.loads(snapshot.read_text()) == original
    assert list(tmp_path.iterdir()) == [snapshot]


@pytest.mark.parametrize("path", ["/", "/state", "/mcp"])
def test_a_rebound_host_name_cannot_reach_the_controller(controller, path):
    # DNS rebinding: a web page resolves its own name to 127.0.0.1. The browser
    # then sends that name in the Host header, and the controller refuses it.
    url, credentials, _, _ = controller
    base = url[: -len("/mcp")]
    headers = {"Host": "evil.example:8767"}
    if path == "/mcp":
        headers["Authorization"] = f"Bearer {credentials['supervisor'].get_secret_value()}"
    with httpx2.Client(trust_env=False) as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        response = (
            client.post(base + path, json=request, headers=headers)
            if path == "/mcp"
            else client.get(base + path, headers=headers)
        )
        assert response.status_code == 421
        port = base.rsplit(":", 1)[1]
        for host in (f"127.0.0.1:{port}", f"localhost:{port}"):
            assert client.get(base + "/state", headers={"Host": host}).status_code == 200


def test_a_cross_site_page_cannot_read_the_live_view(controller):
    url, _, _, _ = controller
    base = url[: -len("/mcp")]
    with httpx2.Client(trust_env=False) as client:
        response = client.get(base + "/state", headers={"Origin": "https://evil.example"})
        assert response.status_code == 403


def test_extra_host_names_come_only_from_the_environment(monkeypatch):
    import factory_mcp

    monkeypatch.delenv("FACTORY_ALLOWED_HOSTS", raising=False)
    assert factory_mcp.allowed_hosts() is None
    monkeypatch.setenv("FACTORY_ALLOWED_HOSTS", " ide.example.com , ,other.example ")
    assert factory_mcp.allowed_hosts() == ["ide.example.com", "other.example"]


def test_a_routine_longer_than_the_scene_limit_fails_the_schema(controller):
    url, credentials, world, _ = controller
    limit = world.scene.teams["east"].max_routine_moves

    async def exercise():
        async with controller_client(url, credentials["east"].get_secret_value()) as client:
            with pytest.raises(ToolError):
                await client.call_tool(
                    "start_shipping",
                    {
                        "command_id": "ship",
                        "units": 2,
                        "batch": 2,
                        "routine": ["spin"] * limit + ["run"],
                    },
                )

    asyncio.run(exercise())


def test_make_server_refuses_a_scene_that_disagrees_with_the_schema_limit(tmp_path):
    data = json.loads((EXAMPLE / "factory.json").read_text())
    data["teams"]["east"]["max_routine_moves"] += 1
    world = World(Scene.model_validate(data), allow_motion=True)
    with pytest.raises(ValueError, match="max_routine_moves"):
        make_server(world, {}, tmp_path / "snapshot.json")
