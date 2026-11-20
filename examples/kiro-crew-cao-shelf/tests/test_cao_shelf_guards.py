"""A5: guard tests. What the backend must never do, checked by reading it and by running it."""

from __future__ import annotations

import ast
import builtins
import io
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import cao_shelf_support as sup
import pytest
from cao_shelf_support import SECRET, Stack

BACKEND_FILES = sorted(sup.BACKEND.rglob("*.py"))
FORBIDDEN_IMPORTS = {
    "subprocess",
    "pickle",
    "marshal",
    "ctypes",
    "shelve",
    "pty",
    "importlib",
    "runpy",
    "multiprocessing",
    "ftplib",
    "smtplib",
    "telnetlib",
    "xmlrpc",
    "webbrowser",
    "code",
}
FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__", "breakpoint"}
FORBIDDEN_ATTRS = {
    "system",
    "popen",
    "execv",
    "execve",
    "execl",
    "execlp",
    "execvp",
    "spawnl",
    "spawnv",
    "fork",
    "forkpty",
    "startfile",
}


def tree_of(path: Path) -> ast.AST:
    return ast.parse(path.read_text(), filename=str(path), feature_version=(3, 11))


def test_there_are_backend_files_to_check() -> None:
    names = {p.name for p in BACKEND_FILES}
    assert {"server.py", "http.py", "cao.py", "tiering.py", "actions.py", "poller.py"} <= names


@pytest.mark.parametrize("path", BACKEND_FILES, ids=lambda p: p.name)
def test_no_shell_no_eval_no_dynamic_import(path: Path) -> None:
    for node in ast.walk(tree_of(path)):
        if isinstance(node, ast.Import):
            assert not {a.name.split(".")[0] for a in node.names} & FORBIDDEN_IMPORTS, node.lineno
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] not in FORBIDDEN_IMPORTS, node.lineno
        elif isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name):
                assert fn.id not in FORBIDDEN_CALLS, (path.name, node.lineno, fn.id)
                assert fn.id != "getattr" or not any(
                    isinstance(a, ast.Name) and a.id == "os" for a in node.args
                ), (path.name, node.lineno)
            if isinstance(fn, ast.Attribute):
                assert fn.attr not in FORBIDDEN_ATTRS, (path.name, node.lineno, fn.attr)
            for kw in node.keywords:
                assert not (
                    kw.arg == "shell" and getattr(kw.value, "value", None) is True
                ), node.lineno
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "shell=True" not in node.value


@pytest.mark.parametrize("path", BACKEND_FILES, ids=lambda p: p.name)
def test_python_311_syntax_and_line_length(path: Path) -> None:
    tree_of(path)  # raises if it needs a newer grammar
    for number, line in enumerate(path.read_text().splitlines(), 1):
        assert len(line) <= 100, f"{path.name}:{number}"


def test_python_311_syntax_in_tests_too() -> None:
    for path in sorted((sup.ROOT / "tests").glob("*.py")):
        tree_of(path)


def test_only_the_writers_touch_the_filesystem() -> None:
    """Files are written in config.py (one atomic writer) and the data dir is made in http.py."""
    sinks = {
        "open",
        "write_text",
        "write_bytes",
        "mkdir",
        "unlink",
        "replace",
        "rename",
        "chmod",
        "remove",
        "rmdir",
        "touch",
        "makedirs",
        "fdopen",
    }
    found: Dict[str, Set[str]] = {}
    for path in BACKEND_FILES:
        for node in ast.walk(tree_of(path)):
            if isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
                owner = (
                    fn.value.id
                    if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name)
                    else ""
                )
                ambiguous = name in {"open", "replace"} and isinstance(fn, ast.Attribute)
                if ambiguous and owner not in {"os", "io", "shutil", "builtins"}:
                    continue  # opener.open(...) and str.replace(...) are not file operations
                if name in sinks:
                    found.setdefault(path.name, set()).add(name)
    assert set(found) <= {"config.py", "http.py"}, found
    assert found.get("http.py", set()) <= {"mkdir"}
    # reads of the config and the ledger use read_text, which is not a sink above
    assert "ledger.py" not in found and "poller.py" not in found and "actions.py" not in found


def test_the_client_and_the_readers_name_no_write_verb_only_the_steering_module_does() -> None:
    verbs = {"POST", "DELETE", "PUT", "PATCH"}
    holders = set()
    for path in BACKEND_FILES:
        for node in ast.walk(tree_of(path)):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and node.value in verbs
            ):
                holders.add(path.name)
    assert "actions.py" in holders
    # http.py only routes verbs; none of the modules that talk to CAO for reads mentions one
    assert not holders & {
        "cao.py",
        "poller.py",
        "tiering.py",
        "history.py",
        "ledger.py",
        "prompts.py",
    }


# ------------------------------------------------------------------------------ run it and watch
class Spy:
    """Records every write-mode open and every outbound connection while a scenario runs."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.writes: List[Tuple[str, str]] = []
        self.connections: List[Any] = []
        real_open = builtins.open
        real_os_open = os.open
        real_replace = os.replace
        real_connect = socket.create_connection
        spy = self

        def fake_open(file: Any, mode: str = "r", *a: Any, **k: Any) -> Any:
            if any(c in mode for c in "wax+"):
                spy.writes.append(("open", os.fspath(file)))
            return real_open(file, mode, *a, **k)

        def fake_os_open(path: Any, flags: int, *a: Any, **k: Any) -> Any:
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                spy.writes.append(("os.open", os.fspath(path)))
            return real_os_open(path, flags, *a, **k)

        def fake_replace(src: Any, dst: Any, *a: Any, **k: Any) -> Any:
            spy.writes.append(("replace", os.fspath(dst)))
            return real_replace(src, dst, *a, **k)

        def fake_connect(address: Any, *a: Any, **k: Any) -> Any:
            spy.connections.append(address)
            return real_connect(address, *a, **k)

        monkeypatch.setattr(builtins, "open", fake_open)
        monkeypatch.setattr(io, "open", fake_open)
        monkeypatch.setattr(os, "open", fake_os_open)
        monkeypatch.setattr(os, "replace", fake_replace)
        monkeypatch.setattr(socket, "create_connection", fake_connect)


def scenario(stack: Stack) -> None:
    """Touches every endpoint, with the sentinel string in every place a body could be."""
    for rows in stack.cao.inbox.values():
        for row in rows:
            row["message"] = sup.SENTINEL
    stack.cao.outputs["b94da5c6"] = f"{sup.SENTINEL}\nAPPROVAL_REQUIRED: {sup.SENTINEL}\n"
    stack.warm(5)
    stack.get("/api/shelf")
    stack.get("/api/status")
    stack.get("/api/config")
    stack.call("PUT", "/api/config", {"poll_seconds": 7})
    stack.get("/api/history")
    stack.get("/api/sessions/payments-fix/history")
    closed = stack.get("/api/sessions/payments-fix/history").json["closed"][0]["terminal_id"]
    rows = stack.get(f"/api/sessions/payments-fix/history/{closed}/rows")
    assert sup.SENTINEL in rows.raw.decode()  # bodies do reach the page, on request
    prompt = stack.get("/api/terminals/b94da5c6/prompt")
    assert sup.SENTINEL in prompt.raw.decode()
    stack.get("/api/profiles")
    stack.post(
        "/api/sessions/payments-fix/ask", {"message": sup.SENTINEL, "conductor_id": "b2ce9908"}
    )
    stack.post(
        "/api/terminals/b94da5c6/answer",
        {"answer": sup.SENTINEL, "expect_status": "waiting_user_answer"},
    )
    stack.post(
        "/api/sessions",
        {
            "name": "fresh",
            "agent_profile": "developer",
            "message": sup.SENTINEL,
            "working_directory": f"/work/{sup.SENTINEL}",
        },
    )
    plan = stack.post("/api/stop/plan", {"scope": "named", "sessions": ["ci-flakes"]}).json
    stack.post(
        "/api/stop/execute",
        {
            "plan_id": plan["plan_id"],
            "sessions": [{"name": "ci-flakes", "last_active": plan["sessions"][0]["last_active"]}],
        },
    )
    stack.post("/api/sessions/payments-fix/ask", {"message": sup.SENTINEL})  # an error path
    stack.cao.mode = "down"
    stack.now += 30
    stack.warm(1)
    stack.get("/api/shelf")


def test_the_only_files_opened_for_writing_are_under_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    stack = Stack(data, "busy")
    try:
        spy = Spy(monkeypatch)
        scenario(stack)
    finally:
        monkeypatch.undo()
        stack.close()
    assert spy.writes, "the scenario should have written config and ledger"
    root = str(data)
    for how, path in spy.writes:
        assert os.path.abspath(path).startswith(root + os.sep), (how, path)


def test_only_loopback_connections_are_made(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stack = Stack(tmp_path / "data", "busy")
    try:
        spy = Spy(monkeypatch)
        scenario(stack)
    finally:
        monkeypatch.undo()
        stack.close()
    assert spy.connections
    assert {addr[0] for addr in spy.connections} <= {"127.0.0.1"}


def test_the_server_listens_on_loopback_only(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "data", "busy")
    try:
        assert stack.server.server_address[0] == "127.0.0.1"
    finally:
        stack.close()


def test_logs_and_output_of_a_full_run_hold_no_bodies(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
) -> None:
    import logging

    caplog.set_level(logging.DEBUG)
    stack = Stack(tmp_path / "data", "busy")
    try:
        scenario(stack)
    finally:
        stack.close()
    out, err = capfd.readouterr()
    logged = "\n".join(r.getMessage() + " " + str(r.args) for r in caplog.records)
    for text in (logged, out, err):
        assert sup.SENTINEL not in text
    assert (tmp_path / "data").exists()
    for path in (tmp_path / "data").iterdir():
        assert sup.SENTINEL not in path.read_text(), path.name


def test_error_envelopes_of_a_full_run_hold_no_bodies(tmp_path: Path) -> None:
    stack = Stack(tmp_path / "data", "busy")
    try:
        stack.warm()
        for method, path, body in [
            ("POST", "/api/sessions/payments-fix/ask", {"message": sup.SENTINEL}),
            (
                "POST",
                "/api/terminals/b94da5c6/answer",
                {"answer": sup.SENTINEL, "expect_status": "idle"},
            ),
            (
                "POST",
                "/api/stop/execute",
                {"plan_id": sup.SENTINEL, "sessions": [{"name": "x", "last_active": "y"}]},
            ),
            (
                "POST",
                "/api/sessions",
                {
                    "agent_profile": "d",
                    "working_directory": "/w",
                    "name": "bad name " + sup.SENTINEL,
                },
            ),
        ]:
            r = stack.call(method, path, body)
            assert r.status >= 400 and sup.SENTINEL not in r.raw.decode(), path
    finally:
        stack.close()


# ------------------------------------------------------------------------- the real entry point
def test_server_py_starts_answers_health_and_refuses_unsigned_calls(tmp_path: Path) -> None:
    port = sup.find_port()
    env = {
        "PATH": os.environ.get("PATH", ""),
        "PORT": str(port),
        "KIROCREW_PROXY_SECRET": SECRET,
        "CAO_SHELF_DATA_DIR": str(tmp_path / "data"),
    }
    proc = subprocess.Popen(
        [sys.executable, "-I", str(sup.BACKEND / "server.py")],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as r:
                    assert json.loads(r.read()) == {"status": "ok"}
                    break
            except OSError:
                assert time.monotonic() < deadline and proc.poll() is None, "server did not start"
                time.sleep(0.05)
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/shelf", timeout=2)
            raise AssertionError("unsigned call was accepted")
        except urllib.error.HTTPError as exc:
            assert exc.code == 401
    finally:
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    assert proc.returncode == 0  # SIGTERM shuts down cleanly


def test_server_py_needs_a_port() -> None:
    env = {"PATH": os.environ.get("PATH", "")}
    done = subprocess.run(
        [sys.executable, "-I", str(sup.BACKEND / "server.py")],
        env=env,
        capture_output=True,
        timeout=20,
    )
    assert done.returncode == 2 and b"PORT" in done.stderr
