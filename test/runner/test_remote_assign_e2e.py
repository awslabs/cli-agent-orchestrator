"""An agent assigns work to an agent on another runner, and gets the result back (#745).

As deployed: one ``cao-server --terminal remote`` and two ``cao-runner``
processes, each with its own home, its own tmux server and room for one
terminal, so the agent ``assign`` creates cannot share the caller's runner.
Nothing between them is mocked.

The agents are ``mock_cli``, made able to call tools: each runner's
``mock_cli`` is the test fixture with one more input it understands. A line
``__mcp__ {"tool": ..., "args": {...}}`` starts ``cao-mcp-server`` over stdio,
inside the agent's own pane, drives it with ``mcp.ClientSession``, calls the
tool and echoes the result. That is the path a real agent's tool call takes:
the pane's environment (``CAO_TERMINAL_ID``, ``CAO_API_HOST``/``PORT``)
identifies the caller and reaches the central API.

The caller asks for a worker whose task is itself a tool call: reply with
``send_message`` (no receiver: the recorded caller). The test then checks the
artifacts: the worker's row names the other runner, the worker's pane on that
runner ran the call, and the result reached the caller's inbox and its pane.
"""

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional

import pytest
import requests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
]

REPO = Path(__file__).resolve().parents[2]
FIXTURE = REPO / "test" / "providers" / "fixtures" / "bin" / "mock_cli"
BIN = Path(sys.executable).parent
REPLY = "result-from-the-other-runner"

# One more case for the fixture's REPL: run a tool call, echo its result as a
# mock response line so the provider's status detection is unchanged.
_MCP_CASE = """        __mcp__*)
            out="$("{python}" "{driver}" "${{clean_line#__mcp__ }}" 2>>"{log}" | tr '\\n' ' ')"
            echo "> MOCK: mcp $out"
            printf '%s' "$PROMPT"
            ;;
        "")"""

_DRIVER = '''"""Call one cao-mcp-server tool over stdio; print the result on one line."""
import asyncio, json, os, sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(raw):
    # The JSON request may be followed by text the caller appended (assign adds
    # the sender's id and callback instructions after the message).
    request, _ = json.JSONDecoder().raw_decode(raw.strip())
    # The pane's own environment, as an agent CLI passes it (env=None would give
    # the server only the mcp client's small default set, without CAO_*).
    params = StdioServerParameters(command={server!r}, args=[], env=dict(os.environ))
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(request["tool"], request["args"])
    texts = [getattr(c, "text", "") for c in result.content]
    failed = getattr(result, "is_error", getattr(result, "isError", None))
    print(json.dumps({{"tool": request["tool"], "error": failed, "text": texts}}))


asyncio.run(main(sys.argv[1]))
'''


def _two_free_ports():
    """Two distinct free ports, held open together so they cannot be the same.

    They are released before cao-server binds them, so another process can
    still take one in between; ``start`` retries on new ports if it does.
    """
    with socket.socket() as a, socket.socket() as b:
        a.bind(("127.0.0.1", 0))
        b.bind(("127.0.0.1", 0))
        return a.getsockname()[1], b.getsockname()[1]


SERVER_START_ATTEMPTS = 3


def _wait(predicate, timeout: float = 30.0, interval: float = 0.25, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except requests.RequestException as exc:
            last = exc  # not ready yet; an exception must not read as success
        else:
            if last:
                return last
        time.sleep(interval)
    raise AssertionError(f"{what} not met in {timeout}s (last: {last!r})")


class TwoRunners:
    def __init__(self) -> None:
        # Short root: tmux's socket path must fit in sun_path on macOS.
        self.root = Path(tempfile.mkdtemp(prefix="cao-as-", dir="/tmp"))
        self.server: Optional[subprocess.Popen] = None
        self.runners: Dict[str, subprocess.Popen] = {}
        self._logs: List = []
        # Unique per instance: only this stack's own runners can register under
        # these ids (they hold its token), so seeing them proves the server that
        # answers is the one this stack started, not whatever took its port.
        suffix = uuid.uuid4().hex[:8]
        self.runner_ids = (f"runner-a-{suffix}", f"runner-b-{suffix}")
        try:
            self.token_file = self.root / "token"
            self.token_file.write_text("e2e-" + os.urandom(24).hex())
            self.token_file.chmod(0o600)
            self.work = self.root / "work"
            self.work.mkdir()
            self._write_agent_bin()
        except BaseException:
            shutil.rmtree(self.root, ignore_errors=True)
            raise

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.api_port}"

    def _write_agent_bin(self) -> None:
        self.agent_bin = self.root / "agent-bin"
        self.agent_bin.mkdir()
        driver = self.root / "mcp_call.py"
        driver.write_text(_DRIVER.format(server=str(BIN / "cao-mcp-server")))
        script = FIXTURE.read_text()
        assert script.count('        "")') == 1, "the mock_cli fixture changed shape"
        script = script.replace(
            '        "")',
            _MCP_CASE.format(python=sys.executable, driver=driver, log=self.root / "mcp-calls.log"),
        )
        mock = self.agent_bin / "mock_cli"
        mock.write_text(script)
        mock.chmod(0o755)

    def _env(self, home: Path) -> Dict[str, str]:
        env = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("CAO_", "TMUX", "PYTEST", "COV_")) and k != "HOME"
        }
        home.mkdir(exist_ok=True)
        env.update(
            HOME=str(home),
            CAO_HOME_DIR=str(home / ".cao"),
            CAO_AGENTS_DIR=str(home / "kiro-agents"),
            CAO_LOG_LEVEL="INFO",
            # A known shell for the panes: an interactive zsh in a fresh home
            # stops at its first-run menu and swallows the agent's launch.
            SHELL=shutil.which("bash") or "/bin/bash",
        )
        return env

    def _spawn(self, name: str, argv, env) -> subprocess.Popen:
        log = open(self.root / f"{name}.log", "ab")
        self._logs.append(log)
        return subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT)

    def _spawn_server(self) -> None:
        env = self._env(self.root / "server")
        env.update(
            CAO_RUNNER_TOKEN_FILE=str(self.token_file),
            CAO_RUNNER_PORT=str(self.hub_port),
            CAO_RUNNER_PLACEMENT_TIMEOUT="20",
            CAO_RUNNER_AGENT_API_HOST="127.0.0.1",
            CAO_RUNNER_AGENT_API_PORT=str(self.api_port),
        )
        self.server = self._spawn(
            "server",
            [str(BIN / "cao-server"), "--terminal", "remote", "--port", str(self.api_port)],
            env,
        )

    def _spawn_runners(self) -> None:
        for runner_id in self.runner_ids:
            env = self._env(self.root / runner_id)
            (self.root / runner_id / "tmux").mkdir(exist_ok=True)
            env.update(
                PATH=f"{self.agent_bin}{os.pathsep}{BIN}{os.pathsep}{env.get('PATH', '')}",
                TMUX_TMPDIR=str(self.root / runner_id / "tmux"),
                CAO_RUNNER_TOKEN_FILE=str(self.token_file),
                CAO_RUNNER_SERVER_URL=f"ws://127.0.0.1:{self.hub_port}",
                CAO_RUNNER_ID=runner_id,
                CAO_RUNNER_MAX_TERMINALS="1",
                CAO_RUNNER_READY_FILE=str(self.root / runner_id / "ready"),
                # How an agent's in-pane cao-mcp-server reaches the API, as the
                # runner pods in the Kubernetes example set it.
                CAO_API_HOST="127.0.0.1",
                CAO_API_PORT=str(self.api_port),
            )
            self.runners[runner_id] = self._spawn(runner_id, [str(BIN / "cao-runner")], env)

    def start(self) -> None:
        """Start the stack; again on new ports if this attempt's server exits first.

        Ready means: this attempt's server process is alive and lists this
        stack's own runners. A port taken meanwhile by another process makes
        the server exit (or never list them), and the whole attempt is retried.
        """
        for _ in range(SERVER_START_ATTEMPTS):
            self.api_port, self.hub_port = _two_free_ports()
            self._spawn_server()
            self._spawn_runners()
            server = self.server

            def ready():
                if server.poll() is not None:
                    return "exited"
                return set(self.runner_ids) <= self.connected()

            if _wait(ready, 60, what=f"server up with both runners\n{self.logs()}") is True:
                return
            self._stop_processes()
        raise AssertionError(f"cao-server exited on {SERVER_START_ATTEMPTS} starts\n{self.logs()}")

    def connected(self) -> set:
        return {r["runner_id"] for r in requests.get(f"{self.base}/runners", timeout=5).json()}

    def hosted(self) -> Dict[str, List[str]]:
        rows = requests.get(f"{self.base}/runners", timeout=5).json()
        return {r["runner_id"]: r["terminals"] for r in rows}

    def registry(self) -> List[sqlite3.Row]:
        db = self.root / "server" / ".cao" / "db" / "cli-agent-orchestrator.db"
        with sqlite3.connect(str(db)) as conn:
            conn.row_factory = sqlite3.Row
            return conn.execute(
                "SELECT id, tmux_session, tmux_window, runner_id FROM terminals"
            ).fetchall()

    def pane(self, runner_id: str, session: str, window: str) -> str:
        env = {**os.environ, "TMUX_TMPDIR": str(self.root / runner_id / "tmux")}
        env.pop("TMUX", None)
        out = subprocess.run(
            [
                shutil.which("tmux"),
                "capture-pane",
                "-p",
                "-J",
                "-S",
                "-200",
                "-t",
                f"{session}:{window}",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout

    def logs(self) -> str:
        parts = []
        for name in ("server", *self.runner_ids, "mcp-calls"):
            path = self.root / f"{name}.log"
            if path.exists():
                parts.append(f"--- {name}.log (tail)\n" + path.read_text()[-4000:])
        return "\n".join(parts)

    def _stop_processes(self) -> None:
        procs = [*self.runners.values(), self.server]
        for proc in procs:
            if proc is not None and proc.poll() is None:
                proc.terminate()
        for proc in procs:
            if proc is not None:
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
        for runner_id in self.runner_ids:
            env = {**os.environ, "TMUX_TMPDIR": str(self.root / runner_id / "tmux")}
            env.pop("TMUX", None)
            subprocess.run([shutil.which("tmux"), "kill-server"], env=env, capture_output=True)
        self.runners, self.server = {}, None

    def stop(self) -> None:
        self._stop_processes()
        for log in self._logs:
            log.close()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture
def stack():
    s = TwoRunners()
    try:
        s.start()
        yield s
    finally:
        s.stop()


def _row(stack: TwoRunners, terminal_id: str) -> sqlite3.Row:
    return next(r for r in stack.registry() if r["id"] == terminal_id)


def test_an_agent_assigns_to_another_runner_and_gets_the_result_in_its_inbox(stack):
    created = requests.post(
        f"{stack.base}/sessions",
        params={
            "agent_profile": "developer",
            "provider": "mock_cli",
            "working_directory": str(stack.work),
        },
        timeout=60,
    )
    assert created.status_code == 201, created.text
    caller = created.json()["id"]
    caller_row = _row(stack, caller)
    caller_runner = caller_row["runner_id"]
    other_runner = next(r for r in stack.runner_ids if r != caller_runner)

    reply = {"tool": "send_message", "args": {"message": REPLY}}
    assign = {
        "tool": "assign",
        "args": {"agent_profile": "developer", "message": "__mcp__ " + json.dumps(reply)},
    }
    sent = requests.post(
        f"{stack.base}/terminals/{caller}/input",
        params={"message": "__mcp__ " + json.dumps(assign)},
        timeout=30,
    )
    assert sent.ok, sent.text

    # The worker: a new row, placed on the other runner (the caller's is full).
    worker_row = _wait(
        lambda: next((r for r in stack.registry() if r["id"] != caller), None),
        60,
        what=f"a worker row\n{stack.logs()}",
    )
    worker = worker_row["id"]
    assert worker_row["runner_id"] == other_runner
    hosted = stack.hosted()
    assert hosted[caller_runner] == [f"{caller_row['tmux_session']}:{caller_row['tmux_window']}"]
    assert f"{worker_row['tmux_session']}:{worker_row['tmux_window']}" in hosted[other_runner]

    # The caller's assign succeeded, from inside its own pane on its runner.
    caller_pane = _wait(
        lambda: (
            p
            if 'MOCK: mcp {"tool": "assign"'
            in (
                p := stack.pane(
                    caller_runner, caller_row["tmux_session"], caller_row["tmux_window"]
                )
            )
            else None
        ),
        60,
        what=f"the caller's assign result\n{stack.logs()}",
    )
    assert 'MOCK: mcp {"tool": "assign", "error": false' in caller_pane, caller_pane
    assert '\\"success\\":true' in caller_pane, caller_pane

    # The worker ran its own tool call, on the other runner.
    worker_pane = _wait(
        lambda: (
            p
            if 'MOCK: mcp {"tool": "send_message"'
            in (
                p := stack.pane(other_runner, worker_row["tmux_session"], worker_row["tmux_window"])
            )
            else None
        ),
        60,
        what=f"the worker's send_message result\n{stack.logs()}",
    )
    assert 'MOCK: mcp {"tool": "send_message", "error": false' in worker_pane, worker_pane
    assert '\\"success\\":true' in worker_pane, worker_pane

    # The result reached the caller: delivered from its inbox into its pane.
    def delivered():
        rows = requests.get(f"{stack.base}/terminals/{caller}/inbox/messages", timeout=5).json()
        return [m for m in rows if REPLY in m.get("message", "") and m.get("status") == "delivered"]

    (message,) = _wait(delivered, 60, what=f"the reply delivered\n{stack.logs()}")
    assert message["sender_id"] == worker
    _wait(
        lambda: (
            f"MOCK: {REPLY}"
            in stack.pane(caller_runner, caller_row["tmux_session"], caller_row["tmux_window"])
        ),
        30,
        what="the reply in the caller's pane",
    )
