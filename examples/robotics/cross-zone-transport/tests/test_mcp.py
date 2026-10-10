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
import httpx
import pytest
import uvicorn
from fastmcp.exceptions import ToolError
from pydantic import SecretStr
from simulation import Scene, World
from transport_mcp import controller_client, make_server, save_snapshot

EXAMPLE = Path(__file__).resolve().parents[1]


@pytest.fixture
def controller(tmp_path):
    scene = Scene.model_validate_json((EXAMPLE / "site.json").read_text())
    world = World(scene, allow_motion=True)
    credentials = {
        name: SecretStr(secrets.token_urlsafe(32))
        for name in ("west", "east", "observer", "operator")
    }
    tokens = {
        credentials[name].get_secret_value(): {
            "client_id": name,
            "scopes": ["observe", "act"],
            "zone": name,
        }
        for name in ("west", "east")
    }
    tokens[credentials["observer"].get_secret_value()] = {
        "client_id": "observer",
        "scopes": ["observe"],
    }
    tokens[credentials["operator"].get_secret_value()] = {
        "client_id": "operator",
        "scopes": ["observe", "operate"],
    }
    snapshot = tmp_path / "last-state.json"
    app = make_server(world, tokens, snapshot).http_app(stateless_http=True)
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
    """Return the transport-sim tools, with parameters, that a committed profile documents."""
    _, body = demo.read_profile(demo.PROFILE_SOURCES[role])
    section = body.split("### transport-sim", 1)[1]
    section = section.split("\n### ", 1)[0].split("\n## ", 1)[0]
    return {
        name: [parameter.strip() for parameter in parameters.split(",") if parameter.strip()]
        for name, parameters in re.findall(r"\*\*(\w+)\*\*\(([^)]*)\)", section)
    }


def test_profiles_document_exactly_the_tools_of_their_credential(controller):
    url, credentials, _, _ = controller

    async def exercise():
        for role, credential in (
            ("supervisor", "observer"),
            ("checker", "observer"),
            ("zone", "west"),
        ):
            async with controller_client(url, credentials[credential].get_secret_value()) as client:
                tools = {
                    tool.name: list(tool.inputSchema.get("properties", {}))
                    for tool in await client.list_tools()
                }
            assert documented_tools(role) == tools, role

    asyncio.run(exercise())


def test_observer_cannot_gain_actions_by_asking_or_naming_a_robot(controller):
    url, credentials, world, _ = controller

    async def exercise():
        async with controller_client(url, credentials["observer"].get_secret_value()) as client:
            assert {tool.name for tool in await client.list_tools()} == {"observe"}
            state = (await client.call_tool("observe")).data
            assert state["run_id"] == world.run_id
            for tool, arguments in (
                (
                    "move",
                    {
                        "command_id": "impersonate",
                        "robot": "cart-west",
                        "payload": "parcel",
                        "destination": "dock",
                    },
                ),
                (
                    "offer_handoff",
                    {"command_id": "offer", "payload": "parcel", "receiver_zone": "east"},
                ),
                (
                    "accept_handoff",
                    {
                        "command_id": "accept",
                        "payload": "parcel",
                        "robot": "cart-east",
                        "offer_id": "imaginary",
                    },
                ),
                ("stop_simulation", {}),
            ):
                with pytest.raises(ToolError):
                    await client.call_tool(tool, arguments)
        assert world.observe()["commands"] == []
        assert not world.stopped

    asyncio.run(exercise())


def test_zone_ownership_is_bound_to_authentication_not_an_argument(controller):
    url, credentials, world, _ = controller

    async def exercise():
        async with controller_client(url, credentials["east"].get_secret_value()) as client:
            assert {tool.name for tool in await client.list_tools()} == {
                "observe",
                "move",
                "offer_handoff",
                "accept_handoff",
            }
            result = await client.call_tool(
                "move",
                {
                    "command_id": "other-zone",
                    "robot": "cart-west",
                    "payload": "parcel",
                    "destination": "dock",
                },
            )
            assert result.data["status"] == "rejected"
            assert result.data["reason"] == "not_robot_owner"
            with pytest.raises(ToolError):
                await client.call_tool("stop_simulation")
        assert world.observe()["payloads"]["parcel"]["at"] == "stock"

    asyncio.run(exercise())


def test_controller_outlives_a_worker_and_a_reconnect_does_not_replay(controller):
    url, credentials, world, _ = controller
    arguments = {
        "command_id": "persistent",
        "robot": "cart-west",
        "payload": "parcel",
        "destination": "dock",
    }

    async def exercise():
        async with controller_client(url, credentials["west"].get_secret_value()) as worker:
            assert (await worker.call_tool("move", arguments)).data["status"] == "accepted"
        async with controller_client(url, credentials["observer"].get_secret_value()) as checker:
            deadline = time.monotonic() + 8
            while True:
                state = (await checker.call_tool("observe")).data
                if state["commands"][0]["status"] == "finished":
                    break
                assert time.monotonic() < deadline, "the actual bounded leg did not finish"
                await asyncio.sleep(0.05)
            assert state["payloads"]["parcel"]["xy"] == pytest.approx([0, 0], abs=0.01)
        async with controller_client(url, credentials["west"].get_secret_value()) as replacement:
            assert (await replacement.call_tool("move", arguments)).data["status"] == "finished"
        assert len(world.observe()["commands"]) == 1

    asyncio.run(exercise())


def test_each_tool_call_is_logged_with_its_calling_agent(controller, caplog):
    # The README guide shows what each agent does from these controller lines.
    url, credentials, _, _ = controller
    arguments = {
        "command_id": "logged-move",
        "robot": "cart-west",
        "payload": "parcel",
        "destination": "dock",
    }

    async def exercise():
        async with controller_client(url, credentials["west"].get_secret_value()) as worker:
            await worker.call_tool("move", arguments)
        async with controller_client(url, credentials["observer"].get_secret_value()) as checker:
            await checker.call_tool("observe")

    with caplog.at_level("INFO", logger="transport"):
        asyncio.run(exercise())
    assert (
        "west zone worker called move(command_id=logged-move, robot=cart-west, "
        "payload=parcel, destination=dock)" in caplog.text
    )
    assert "observer called observe()" in caplog.text


def test_operator_stops_without_a_worker_or_supervisor_inbox(controller):
    url, credentials, world, snapshot = controller

    async def exercise():
        async with controller_client(url, credentials["west"].get_secret_value()) as worker:
            await worker.call_tool(
                "move",
                {
                    "command_id": "interrupt",
                    "robot": "cart-west",
                    "payload": "parcel",
                    "destination": "dock",
                },
            )
        async with controller_client(url, credentials["operator"].get_secret_value()) as operator:
            stopped = (await operator.call_tool("stop_simulation")).data
            assert stopped["stopped"] is True
            assert stopped["commands"][0]["status"] == "interrupted"
            pose = stopped["payloads"]["parcel"]["xy"]
            await asyncio.sleep(0.1)
            assert (await operator.call_tool("observe")).data["payloads"]["parcel"]["xy"] == pose
        assert json.loads(snapshot.read_text())["payloads"]["parcel"]["xy"] == pose
        assert world.observe()["payloads"]["parcel"]["owner"] == "west"

    asyncio.run(exercise())


def test_missing_or_wrong_credential_is_not_read_access(controller):
    url, _, _, _ = controller
    with httpx.Client(trust_env=False) as client:
        request = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
        assert client.post(url, json=request).status_code == 401
        assert (
            client.post(
                url, json=request, headers={"Authorization": "Bearer not-a-valid-credential"}
            ).status_code
            == 401
        )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8766/mcp",
        "http://localhost:8766/mcp",
        "http://192.0.2.1:8766/mcp",
        "http://127.0.0.1:8766/robot",
        "http://127.0.0.1:8766/mcp?next=external",
        "http://name@127.0.0.1:8766/mcp",
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

    async def exercise():
        async with controller_client(url, credentials["observer"].get_secret_value()) as client:
            assert (await client.call_tool("observe")).data["position_units"] == "m"

    asyncio.run(exercise())


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
