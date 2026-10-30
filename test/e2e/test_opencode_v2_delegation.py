"""Live Codex -> OpenCode Mini MCP handoff; requires authenticated CLIs."""

import os
import sqlite3
import subprocess
import sys
import time
import uuid
from pathlib import Path
from test.fixtures.cao_server import (
    CaoServer,
    _pick_free_port,
    _subprocess_env,
    _terminate,
    _wait_for_health,
)

import frontmatter
import pytest
import requests

from cli_agent_orchestrator.utils.opencode_cli import detect_opencode_major


@pytest.fixture
def delegation_server(tmp_path):
    """Use the canonical module import so delegation sees the lifespan's app.

    The shared fixture currently uses ``-m api.main``; decision callbacks
    import ``api.main.app`` and would otherwise see a second app instance.
    """
    home = tmp_path / "server-home"
    home.mkdir()
    port = _pick_free_port()
    env = _subprocess_env(home, port)
    log = home / "server.log"
    handle = log.open("wb")
    process = subprocess.Popen(
        [sys.executable, "-c", "from cli_agent_orchestrator.api.main import main; main()"],
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
    )
    stopped = [False]
    server = CaoServer(
        url=f"http://127.0.0.1:{port}",
        port=port,
        home_dir=home,
        db_path=home / ".aws/cli-agent-orchestrator/db/cli-agent-orchestrator.db",
        log_path=log,
        stop=lambda: _terminate(process, handle, stopped),
    )
    try:
        _wait_for_health(server.url, deadline=30, process=process, log_path=log)
        yield server
    finally:
        server.stop()


@pytest.mark.e2e
def test_codex_handoff_to_opencode_v2(delegation_server, require_codex, require_opencode, tmp_path):
    cao_server = delegation_server
    if detect_opencode_major() != 2:
        pytest.skip("This integration test requires OpenCode v2 Mini")

    # Install into the managed server's disposable HOME, using this checkout's
    # interpreter and MCP entry point. Never install into the operator's home.
    env = {
        **os.environ,
        "HOME": str(cao_server.home_dir),
        "CAO_HOME_DIR": str(cao_server.home_dir / ".aws/cli-agent-orchestrator"),
        "CAO_API_PORT": str(cao_server.port),
    }
    auth = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "auth.json"
    if auth.is_file():
        target = cao_server.home_dir / ".codex/auth.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.symlink_to(auth)

    mcp = {
        "cao-mcp-server": {
            "command": sys.executable,
            "args": ["-m", "cli_agent_orchestrator.mcp_server.server"],
            "env": {"CAO_API_PORT": str(cao_server.port)},
        }
    }
    root = Path(__file__).resolve().parents[2]
    developer = frontmatter.load(root / "src/cli_agent_orchestrator/agent_store/developer.md")
    developer["provider"] = "opencode_cli"
    developer["mcpServers"] = mcp
    developer["allowedTools"] = ["@builtin", "fs_*", "execute_bash", "@cao-mcp-server", "discovery"]
    supervisor = frontmatter.Post(
        "Use CAO handoff to delegate to developer when asked. Report the returned result.",
        name="mini-supervisor",
        role="supervisor",
        mcpServers=mcp,
    )
    for post, backend in ((developer, "opencode_cli"), (supervisor, "codex")):
        profile = tmp_path / f"{post['name']}.md"
        profile.write_text(frontmatter.dumps(post))
        installed = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli_agent_orchestrator.cli.main",
                "install",
                str(profile),
                "--provider",
                backend,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert installed.returncode == 0 and "Error:" not in installed.stdout, (
            installed.stdout + installed.stderr
        )

    session_name = "mini-delegation-" + uuid.uuid4().hex[:8]
    session = requests.post(
        cao_server.url + "/sessions",
        params={
            "provider": "codex",
            "agent_profile": "mini-supervisor",
            "session_name": session_name,
            "working_directory": str(tmp_path),
        },
        timeout=180,
    )
    assert session.status_code in (200, 201), session.text
    supervisor_id = session.json()["id"]
    try:
        ready_deadline = time.monotonic() + 120
        while time.monotonic() < ready_deadline:
            state = requests.get(cao_server.url + f"/terminals/{supervisor_id}", timeout=15).json()[
                "status"
            ]
            if state in ("idle", "completed"):
                break
            assert state != "error"
            time.sleep(1)
        else:
            pytest.fail("Codex supervisor did not initialize")
        prompt = (
            "Call cao-mcp-server handoff with agent_profile developer, timeout 120, "
            "and message: 'Use the cao-mcp-server list_siblings tool to inspect your CAO "
            "session and create mini-worker.txt in your working directory containing "
            "MINI_WORKER_OK, then respond with exactly MINI_WORKER_OK.' "
            "After handoff returns, reply SUPERVISOR_RECEIVED followed by the worker result. "
            "Do not perform the worker task yourself."
        )
        result = requests.post(
            cao_server.url + "/terminals/run-step",
            json={
                "provider": "codex",
                "agent": "mini-supervisor",
                "prompt": prompt,
                "reuse_terminal_id": supervisor_id,
                "timeout": 240,
            },
            timeout=300,
        )
        assert result.status_code == 200, result.text
        output = result.json()["last_message"]
        assert "SUPERVISOR_RECEIVED" in output and "MINI_WORKER_OK" in output, output
        # A returned model claim alone cannot prove delegation: require the
        # durable handoff record as evidence that a separate worker ran.
        with sqlite3.connect(cao_server.db_path) as db:
            rows = db.execute(
                "SELECT state, terminal_id, last_message FROM handoff_results"
            ).fetchall()
        assert any(
            state == "completed"
            and terminal_id != supervisor_id
            and message.strip() == "MINI_WORKER_OK"
            for state, terminal_id, message in rows
        ), rows
        assert (tmp_path / "mini-worker.txt").read_text().strip() == "MINI_WORKER_OK"
    finally:
        requests.delete(cao_server.url + f"/sessions/{session_name}", timeout=60)
