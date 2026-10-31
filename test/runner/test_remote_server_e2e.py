"""A real cao-server on the remote backend, a real cao-runner, exercised over HTTP (#745).

Two processes, as deployed: ``cao-server --terminal remote`` (uvicorn, the API
routes, the runner hub on its own port) and ``cao-runner`` driving a private
tmux server that the mock_cli agent runs in. Nothing in between is mocked. The
assertions read artifacts — the runner's tmux windows and their identity
options, the server's registry row, the pane's own output — not only the
statuses the API reports.

Failure semantics are driven by real faults: a runner that is not there (503),
a runner frozen mid-call with SIGSTOP (504), a runner whose tmux fails (502),
and a killed runner (status ``unknown``), restored when a new runner process
re-adopts the surviving tmux window.
"""

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Optional

import pytest
import requests

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux not installed"),
]

REPO = Path(__file__).resolve().parents[2]
MOCK_BIN = REPO / "test" / "providers" / "fixtures" / "bin"
BIN = Path(sys.executable).parent
RUNNER_ID = "e2e-runner"
CALL_TIMEOUT = 3


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(predicate, timeout: float = 30.0, interval: float = 0.25, what: str = "condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
        except requests.RequestException as exc:
            last = exc
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{what} not met in {timeout}s (last: {last!r})")


class Stack:
    """One cao-server and its cao-runner, each in its own private home."""

    def __init__(self) -> None:
        # Short root: tmux's socket path must fit in sun_path on macOS.
        self.root = Path(tempfile.mkdtemp(prefix="cao-e2e-", dir="/tmp"))
        self.api_port, self.hub_port = _free_port(), _free_port()
        self.base = f"http://127.0.0.1:{self.api_port}"
        self.token_file = self.root / "token"
        self.token_file.write_text("e2e-" + os.urandom(24).hex())
        self.token_file.chmod(0o600)
        self.tmux_dir = self.root / "tmux"
        self.tmux_dir.mkdir()
        self.ready_file = self.root / "runner-ready"
        self.tmux_fail_flag = self.root / "tmux-fail"
        self._shim_dir = self.root / "shim"
        self._write_tmux_shim()
        self.work = self.root / "work"
        self.work.mkdir()
        self.server: Optional[subprocess.Popen] = None
        self.runner: Optional[subprocess.Popen] = None
        self._logs = []

    def _write_tmux_shim(self) -> None:
        """A ``tmux`` whose listings are garbage while the flag file exists (runner only)."""
        self._shim_dir.mkdir()
        real = shutil.which("tmux")
        shim = self._shim_dir / "tmux"
        # An unparsable listing: TmuxClient reports it as TmuxLookupError
        # ("cannot tell"), never as "not found" — a runner-side failure.
        shim.write_text(
            "#!/bin/sh\n"
            f'if [ -e "{self.tmux_fail_flag}" ]; then\n'
            '  case "$*" in *list-sessions*|*list-windows*) '
            'echo "injected-unparsable-listing"; exit 0;; esac\n'
            "fi\n"
            f'exec "{real}" "$@"\n'
        )
        shim.chmod(0o755)

    def _base_env(self, home: Path) -> Dict[str, str]:
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
        )
        return env

    def _spawn(self, name: str, argv, env) -> subprocess.Popen:
        log = open(self.root / f"{name}.log", "ab")
        self._logs.append(log)
        return subprocess.Popen(argv, env=env, stdout=log, stderr=subprocess.STDOUT)

    def start_server(self) -> None:
        env = self._base_env(self.root / "server")
        env.update(
            CAO_RUNNER_TOKEN_FILE=str(self.token_file),
            CAO_RUNNER_PORT=str(self.hub_port),
            CAO_RUNNER_CALL_TIMEOUT=str(CALL_TIMEOUT),
            CAO_RUNNER_PLACEMENT_TIMEOUT="3",
            CAO_RUNNER_AGENT_API_HOST="127.0.0.1",
            CAO_RUNNER_AGENT_API_PORT=str(self.api_port),
        )
        self.server = self._spawn(
            "server",
            [str(BIN / "cao-server"), "--terminal", "remote", "--port", str(self.api_port)],
            env,
        )
        _wait(lambda: requests.get(f"{self.base}/health", timeout=2).ok, 60, what="server health")

    def start_runner(self) -> None:
        env = self._base_env(self.root / "runner")
        env.update(
            PATH=f"{self._shim_dir}{os.pathsep}{MOCK_BIN}{os.pathsep}{env.get('PATH', '')}",
            TMUX_TMPDIR=str(self.tmux_dir),
            CAO_RUNNER_TOKEN_FILE=str(self.token_file),
            CAO_RUNNER_SERVER_URL=f"ws://127.0.0.1:{self.hub_port}",
            CAO_RUNNER_ID=RUNNER_ID,
            CAO_RUNNER_MAX_TERMINALS="4",
            CAO_RUNNER_READY_FILE=str(self.ready_file),
        )
        self.runner = self._spawn("runner", [str(BIN / "cao-runner")], env)
        _wait(self.ready_file.exists, 30, what="runner ready file")
        _wait(lambda: self.runner_connected(), 30, what="runner connected")

    def runner_connected(self) -> bool:
        rows = requests.get(f"{self.base}/runners", timeout=5).json()
        return any(r["runner_id"] == RUNNER_ID and r.get("connected", True) for r in rows)

    def kill_runner(self) -> None:
        if self.runner is not None and self.runner.poll() is None:
            self.runner.kill()
            self.runner.wait(timeout=10)

    def tmux(self, *args) -> subprocess.CompletedProcess:
        env = {**os.environ, "TMUX_TMPDIR": str(self.tmux_dir)}
        env.pop("TMUX", None)
        return subprocess.run(
            [shutil.which("tmux"), *args], env=env, capture_output=True, text=True, timeout=10
        )

    def runner_windows(self) -> Dict[str, str]:
        """terminal id -> session:window, read from the runner's own tmux."""
        out = self.tmux(
            "list-windows", "-a", "-F", "#{session_name}:#{window_name}\t#{@cao_terminal_id}"
        )
        if out.returncode != 0:
            return {}
        return {
            tid: name
            for name, _, tid in (line.partition("\t") for line in out.stdout.splitlines())
            if tid
        }

    def registry_runner(self, terminal_id: str) -> Optional[str]:
        db = self.root / "server" / ".cao" / "db" / "cli-agent-orchestrator.db"
        with sqlite3.connect(str(db)) as conn:
            row = conn.execute(
                "SELECT runner_id FROM terminals WHERE id = ?", (terminal_id,)
            ).fetchone()
        return row[0] if row else None

    def launch(self) -> str:
        response = requests.post(
            f"{self.base}/sessions",
            params={
                "provider": "mock_cli",
                "agent_profile": "developer",
                "working_directory": str(self.work),
            },
            timeout=120,
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    def streamed(self, terminal_id: str) -> str:
        """What reached the SERVER through the runner's output stream.

        The server-side terminal log is written only from the FIFO the hub
        feeds; ``/output`` can instead read the pane over the channel, so it
        cannot tell a live stream from a dead one.
        """
        log = self.root / "server" / ".cao" / "logs" / "terminal" / f"{terminal_id}.log"
        return log.read_text(errors="replace") if log.exists() else ""

    def status(self, terminal_id: str) -> str:
        return requests.get(f"{self.base}/terminals/{terminal_id}", timeout=30).json()["status"]

    def output(self, terminal_id: str) -> str:
        r = requests.get(
            f"{self.base}/terminals/{terminal_id}/output", params={"mode": "full"}, timeout=30
        )
        assert r.ok, r.text
        return r.json()["output"]

    def logs(self) -> str:
        parts = []
        for name in ("server", "runner"):
            p = self.root / f"{name}.log"
            if p.exists():
                parts.append(f"--- {name}.log (tail)\n" + p.read_text(errors="replace")[-4000:])
        return "\n".join(parts)

    def stop(self) -> None:
        for proc in (self.runner, self.server):
            if proc is not None and proc.poll() is None:
                try:
                    proc.send_signal(signal.SIGCONT)
                except ProcessLookupError:
                    pass
                proc.terminate()
                try:
                    proc.wait(timeout=15)
                except subprocess.TimeoutExpired:
                    proc.kill()
        self.tmux("kill-server")
        for log in self._logs:
            log.close()
        shutil.rmtree(self.root, ignore_errors=True)


@pytest.fixture(scope="module")
def stack():
    s = Stack()
    try:
        s.start_server()
        s.start_runner()
        yield s
    except BaseException:
        print(s.logs())
        raise
    finally:
        s.stop()


@pytest.fixture
def terminal(stack):
    terminal_id = stack.launch()
    _wait(lambda: stack.status(terminal_id) == "idle", 60, what=f"{terminal_id} idle")
    yield terminal_id
    requests.delete(f"{stack.base}/terminals/{terminal_id}", timeout=60)


def test_launch_input_output_status_delete_run_on_the_runner(stack):
    terminal_id = stack.launch()
    try:
        # The pane exists on the RUNNER's tmux, tagged with this id and runner.
        windows = _wait(lambda: stack.runner_windows(), 30, what="runner window")
        assert terminal_id in windows
        name = windows[terminal_id]
        owner = stack.tmux(
            "show-options", "-w", "-v", "-t", f"={name.replace(':', ':=')}", "@cao_runner_id"
        )
        assert owner.stdout.strip() == RUNNER_ID
        # The registry row records where it runs (A1).
        assert stack.registry_runner(terminal_id) == RUNNER_ID
        # The server detects status from the bytes the runner streams back.
        _wait(lambda: stack.status(terminal_id) == "idle", 60, what="idle")

        r = requests.post(
            f"{stack.base}/terminals/{terminal_id}/input",
            params={"message": "hello-from-e2e"},
            timeout=60,
        )
        assert r.ok, r.text
        _wait(lambda: "MOCK: hello-from-e2e" in stack.output(terminal_id), 30, what="echo")
        _wait(lambda: "MOCK: hello-from-e2e" in stack.streamed(terminal_id), 30, what="stream")
        # Not just the API's word: the pane on the runner shows the reply.
        pane = stack.tmux("capture-pane", "-p", "-t", f"={name.replace(':', ':=')}")
        assert "MOCK: hello-from-e2e" in pane.stdout
        _wait(lambda: stack.status(terminal_id) in ("idle", "completed"), 30, what="settled")
    finally:
        r = requests.delete(f"{stack.base}/terminals/{terminal_id}", timeout=60)
    assert r.ok, r.text
    _wait(lambda: terminal_id not in stack.runner_windows(), 30, what="pane killed on runner")
    assert requests.get(f"{stack.base}/terminals/{terminal_id}", timeout=10).status_code == 404


def test_a_runner_reported_failure_is_502(stack, terminal):
    stack.tmux_fail_flag.touch()
    try:
        r = requests.post(
            f"{stack.base}/terminals/{terminal}/key", params={"key": "Enter"}, timeout=60
        )
    finally:
        stack.tmux_fail_flag.unlink()
    assert r.status_code == 502, r.text
    assert RUNNER_ID in r.json()["detail"]
    # Only that call failed; the terminal is still served.
    assert requests.post(
        f"{stack.base}/terminals/{terminal}/key", params={"key": "Enter"}, timeout=60
    ).ok


def test_a_frozen_runner_is_504(stack, terminal):
    # Connected but not answering: the call was sent, its outcome is unknown.
    os.kill(stack.runner.pid, signal.SIGSTOP)
    try:
        r = requests.post(
            f"{stack.base}/terminals/{terminal}/key", params={"key": "Enter"}, timeout=60
        )
    finally:
        os.kill(stack.runner.pid, signal.SIGCONT)
    assert r.status_code == 504, r.text
    assert "outcome unknown" in r.json()["detail"]
    _wait(stack.runner_connected, 30, what="runner still connected after SIGCONT")


def test_a_killed_runner_reads_unknown_then_503_and_recovers(stack, terminal):
    stack.kill_runner()
    # Never the stale idle: out of reach is UNKNOWN (A7).
    _wait(lambda: stack.status(terminal) == "unknown", 30, what="unknown after runner loss")
    r = requests.post(f"{stack.base}/terminals/{terminal}/key", params={"key": "Enter"}, timeout=60)
    assert r.status_code == 503, r.text
    # Nowhere to place a new terminal either.
    r = requests.post(
        f"{stack.base}/sessions",
        params={
            "provider": "mock_cli",
            "agent_profile": "developer",
            "working_directory": str(stack.work),
        },
        timeout=60,
    )
    assert r.status_code == 503, r.text
    # The tmux server outlived the runner; a new runner process re-adopts the
    # pane by its tags and the terminal is back in reach.
    assert terminal in stack.runner_windows()
    stack.ready_file.unlink(missing_ok=True)
    stack.start_runner()
    _wait(lambda: stack.status(terminal) != "unknown", 30, what="status restored")
    # And the output stream is re-armed, not frozen at the last parsed state:
    # a new turn's reply reaches the server and is detected.
    r = requests.post(
        f"{stack.base}/terminals/{terminal}/input",
        params={"message": "after-recovery"},
        timeout=60,
    )
    assert r.ok, r.text
    _wait(lambda: "MOCK: after-recovery" in stack.streamed(terminal), 30, what="re-armed stream")
    _wait(lambda: stack.status(terminal) in ("idle", "completed"), 30, what="settled again")


def test_the_runner_token_never_reaches_the_agent(stack, terminal):
    name = stack.runner_windows()[terminal]
    env = stack.tmux("show-environment", "-t", f"={name.split(':', 1)[0]}")
    assert "CAO_RUNNER_TOKEN" not in env.stdout
    token = stack.token_file.read_text()
    pane = stack.tmux("capture-pane", "-p", "-S", "-", "-t", f"={name.replace(':', ':=')}")
    assert token not in pane.stdout
    assert json.dumps(token) not in (stack.root / "runner.log").read_text(errors="replace")
