"""HerdrAppBackend and HerdrAppInboxService against a throwaway herdr-core.

Every test gets its own core: a fresh ``HC_SESSION`` and XDG runtime, state and
config dirs under a short scratch dir in /tmp (unix socket paths are limited to
108 bytes, so pytest's deep ``tmp_path`` is too long). The operator's own cores,
including ``default`` and the Herdr app's, are never touched.

Run with: uv run pytest -m e2e test/e2e/test_herdr_app_backend_e2e.py -v -s
"""

import asyncio
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.hc_rpc import hc_binary, hc_env
from cli_agent_orchestrator.constants import HC_READ_MAX_LINES
from cli_agent_orchestrator.models.terminal import TerminalStatus

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not (shutil.which("hc") or shutil.which("herdr-core")),
        reason="herdr-core not installed",
    ),
]


@pytest.fixture
def core(monkeypatch):
    scratch = Path(tempfile.mkdtemp(prefix="hce2e", dir="/tmp"))
    for var in ("XDG_RUNTIME_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
        d = scratch / var.split("_")[1].lower()
        d.mkdir(mode=0o700)
        monkeypatch.setenv(var, str(d))
    for var in ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SESSION"):
        monkeypatch.delenv(var, raising=False)
    session = f"cao-e2e-{secrets.token_hex(3)}"
    yield session
    subprocess.run(
        [hc_binary(), "server", "stop"],
        env=hc_env(session),
        timeout=30,
        check=False,
        capture_output=True,
    )
    shutil.rmtree(scratch, ignore_errors=True)


@pytest.fixture
def backend(core):
    from cli_agent_orchestrator.backends.herdr_app_backend import HerdrAppBackend

    b = HerdrAppBackend(herdr_app_session=core)
    yield b
    for session in {t["labels"]["cao.session"] for t in b.list_managed()}:
        b.kill_session(session)


def _wait_for(predicate, timeout=15.0):
    end = time.time() + timeout
    while time.time() < end:
        if predicate():
            return True
        time.sleep(0.25)
    return False


def test_session_lifecycle(backend, tmp_path):
    from cli_agent_orchestrator.backends.herdr_app_backend import space_id_for

    backend.create_session("cao-e2e", "sup", "e2e00001", str(tmp_path))
    backend.create_window("cao-e2e", "w1", "e2e00002", str(tmp_path))
    live = backend.list_managed()
    assert {t["labels"]["herdr.space"] for t in live} == {space_id_for("cao-e2e")}
    assert {t["labels"]["cao.home"] for t in live} == {backend.home_label}
    assert len({t["labels"]["herdr.tab"] for t in live}) == 2
    # The arithmetic only shows up if the shell ran the command, not just echoed it.
    backend.send_keys("cao-e2e", "w1", "echo hi-$((40+2))")
    assert _wait_for(lambda: "hi-42" in backend.get_history("cao-e2e", "w1", tail_lines=50))
    assert backend.kill_session("cao-e2e") is True
    assert backend.session_exists("cao-e2e") is False
    assert backend.list_managed() == []


def test_history_depth(backend, tmp_path):
    backend.create_session("cao-depth", "w", "e2e00003", str(tmp_path))
    # Wait on a marker only the shell can print: "20000" is in the echoed command.
    backend.send_keys("cao-depth", "w", "seq 1 20000; echo seq-done-$((6*7))")
    assert _wait_for(
        lambda: "seq-done-42" in backend.get_history("cao-depth", "w", tail_lines=5), timeout=30
    )
    address = backend.list_managed()[0]["address"]
    text = backend._call("terminal.read", terminal=address, lines=20000)["text"]
    measured = len(text.splitlines())
    print(f"terminal.read returned {measured} lines for lines=20000")
    # HC_READ_MAX_LINES must not ask for more than the core can return.
    assert measured >= HC_READ_MAX_LINES
    out = backend.get_history("cao-depth", "w", full_history=True)
    assert len(out.splitlines()) >= backend.max_history_lines
    backend.kill_session("cao-depth")


def test_inbox_delivers_on_native_idle(backend, tmp_path, monkeypatch):
    """A program's OSC 7501 status reports reach the inbox's delivery callback."""
    from cli_agent_orchestrator.services import herdr_app_inbox_service as mod

    session, tid = "cao-inbox", "e2e00004"
    rows = [{"id": tid, "tmux_session": session}]
    # Seams to CAO's database: this test has no CAO rows, only the one it declares.
    monkeypatch.setattr(mod, "_rows_by_session", lambda: {session: rows})
    monkeypatch.setattr(mod, "_rows_for", lambda s: rows if s == session else [])
    recorded = []
    monkeypatch.setattr(mod, "_record_status", lambda t, st: recorded.append((t, st)))
    monkeypatch.setattr(mod, "_note_activity", lambda t: None)
    torn_down = []
    monkeypatch.setattr(mod, "_teardown", lambda t, reason: torn_down.append((t, reason)))

    delivered = []
    svc = mod.HerdrAppInboxService(backend, delivered.append)
    loop = asyncio.new_event_loop()
    task_box = {}

    def run_loop():
        asyncio.set_event_loop(loop)
        task_box["task"] = loop.create_task(svc.start())
        try:
            loop.run_until_complete(task_box["task"])
        except asyncio.CancelledError:
            pass

    thread = threading.Thread(target=run_loop, daemon=True)
    thread.start()
    try:
        assert _wait_for(lambda: svc._floor_seq >= 0), "inbox never subscribed"
        script = tmp_path / "status.sh"
        script.write_text(
            "sleep 1\n"
            "printf '\\033]7501;state=working\\033\\\\'\n"
            "sleep 1\n"
            "printf '\\033]7501;state=idle\\033\\\\'\n"
            "sleep 60\n"
        )
        backend.create_window(session, "agent", tid, str(tmp_path), window_shell=f"sh {script}")
        assert _wait_for(lambda: tid in delivered, timeout=20), (recorded, svc.mapped())
        assert (tid, TerminalStatus.PROCESSING) in recorded
        assert (tid, TerminalStatus.IDLE) in recorded
        assert backend.get_native_status(session, "agent") == TerminalStatus.IDLE
        assert torn_down == []
    finally:
        loop.call_soon_threadsafe(lambda: task_box["task"].cancel())
        thread.join(timeout=15)
    backend.kill_session(session)
