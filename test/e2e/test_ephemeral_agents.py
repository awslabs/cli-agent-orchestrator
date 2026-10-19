"""Local-only AC-1.1/AC-1.2 evidence after the isolated sign-in probe passes.

Run only after B4 checks 1-4 pass, under its env -i allowlist, with
CAO_EPHEMERAL_E2E=1 added to the test harness (never the server environment).
No provider state is copied from the developer's HOME.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path
from test.fixtures.cao_server import _pick_free_port, _start_cao_server

import pytest
import requests

from cli_agent_orchestrator.utils import orchestration

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        os.environ.get("CAO_EPHEMERAL_E2E") != "1",
        reason="local evidence only: isolated 60-second sign-in probe must pass first",
    ),
]


@pytest.fixture(scope="session")
def cao_server():
    """Override the general fixture: strict allowlist and no real-HOME seeding."""
    handle = Path(
        subprocess.check_output(
            ["mktemp", "-d", "/private/tmp/ephem-e2e.XXXXXX"], text=True
        ).strip()
    )
    for directory in ["home", "cao-home", "tmux"]:
        (handle / directory).mkdir()
    port = _pick_free_port()
    assert port != 9889
    env = {k: os.environ[k] for k in ["PATH", "LANG", "TERM"] if k in os.environ}
    env.update(
        HOME=str(handle / "home"),
        CAO_HOME_DIR=str(handle / "cao-home"),
        TMUX_TMPDIR=str(handle / "tmux"),
        CAO_API_PORT=str(port),
    )
    for key in ["HOME", "CAO_HOME_DIR", "TMUX_TMPDIR"]:
        assert Path(env[key]).is_relative_to(handle)
    cao_home = handle / "cao-home"
    (cao_home / "settings.json").write_text(json.dumps({"ephemeral": {"enabled": True}}))
    store = cao_home / "agent-store"
    store.mkdir()
    (store / "ephemeral_e2e_creator.md").write_text(
        "---\nname: ephemeral_e2e_creator\nprovider: claude_code\nallowedTools: ['*']\n---\n"
        "Wait for results from workers.\n"
    )
    server = None
    try:
        server = _start_cao_server(handle / "home", port, base_env=env, deadline=20)
        yield server
    finally:
        if server is not None:
            server.stop()
        socket = subprocess.run(
            ["tmux", "display", "-p", "#{socket_path}"], env=env, capture_output=True, text=True
        )
        if socket.returncode == 0:
            path = Path(socket.stdout.strip()).resolve()
            assert path.is_relative_to(handle.resolve())
            subprocess.run(["tmux", "-S", str(path), "kill-server"], env=env, check=True)
        opened = subprocess.run(["lsof", "+D", str(handle)], capture_output=True, text=True)
        assert opened.returncode == 1 and not opened.stdout and not opened.stderr
        shutil.rmtree(handle)


@pytest.fixture
def creator(cao_server, monkeypatch):
    response = requests.post(
        cao_server.url + "/sessions",
        params={
            "provider": "claude_code",
            "agent_profile": "ephemeral_e2e_creator",
            "allowed_tools": "*",
        },
        timeout=90,
    )
    response.raise_for_status()
    handle = cao_server.home_dir.parent
    env = {k: os.environ[k] for k in ["PATH", "LANG", "TERM"] if k in os.environ}
    env.update(
        HOME=str(cao_server.home_dir),
        CAO_HOME_DIR=str(handle / "cao-home"),
        TMUX_TMPDIR=str(handle / "tmux"),
        CAO_API_PORT=str(cao_server.port),
    )
    socket_path = Path(
        subprocess.check_output(
            ["tmux", "display", "-p", "#{socket_path}"], env=env, text=True
        ).strip()
    ).resolve()
    assert socket_path.is_relative_to(handle.resolve())
    body = response.json()
    terminal = body["id"]
    monkeypatch.setenv("CAO_TERMINAL_ID", terminal)
    monkeypatch.setattr(orchestration, "API_BASE_URL", cao_server.url)
    try:
        yield terminal
    finally:
        requests.delete(cao_server.url + "/terminals/" + terminal, timeout=30).raise_for_status()
        requests.delete(
            cao_server.url + "/sessions/" + body["session_name"], timeout=30
        ).raise_for_status()


def create_name(server, creator, brief):
    response = requests.post(
        server.url + "/ephemeral-agents",
        params={"caller_id": creator},
        json={"spec_version": 1, "purpose": "e2e_probe", "brief": brief, "tools": ["fs_read"]},
        timeout=30,
    )
    response.raise_for_status()
    return response.json()["name"]


def assert_collected(server, name):
    with sqlite3.connect(server.db_path) as db:
        assert db.execute(
            "SELECT state FROM ephemeral_agents WHERE name=?", (name,)
        ).fetchone() == ("gc",)
    live = server.db_path.parent.parent / "ephemeral" / "live"
    assert not (live / (name + ".md")).exists()
    assert not (live / (name + ".spec.json")).exists()


def test_assign_round_trip_and_delete(cao_server, creator):
    name = create_name(
        cao_server, creator, "Send your creator the exact message EPHEMERAL_ACK using send_message."
    )
    result = orchestration._assign_impl(name, "Perform the brief, then wait.")
    assert result["success"], result
    try:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            response = requests.get(
                cao_server.url + f"/terminals/{creator}/inbox/messages", timeout=10
            )
            response.raise_for_status()
            if any("EPHEMERAL_ACK" in item["message"] for item in response.json()):
                break
            time.sleep(1)
        else:
            pytest.fail("no send_message round trip within 120 seconds")
    finally:
        assert orchestration._delete_terminal_impl(result["terminal_id"])["success"]
    assert_collected(cao_server, name)


@pytest.mark.asyncio
async def test_handoff_success_releases(cao_server, creator):
    name = create_name(cao_server, creator, "Answer the requested task directly.")
    result = await orchestration._handoff_impl(
        name, "Return exactly EPHEMERAL_HANDOFF_OK.", timeout=120
    )
    assert result.success and "EPHEMERAL_HANDOFF_OK" in result.output
    assert_collected(cao_server, name)
