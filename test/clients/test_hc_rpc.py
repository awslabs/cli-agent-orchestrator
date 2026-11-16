import json
import os
import stat
import sys
import threading
from pathlib import Path

import pytest

from cli_agent_orchestrator.backends.base import TerminalBackendError
from cli_agent_orchestrator.clients import hc_rpc
from cli_agent_orchestrator.clients.hc_rpc import (
    HcProtocolError,
    HcRpcClient,
    HcRpcError,
    HcSubscription,
)

FAKE = Path(__file__).with_name("fake_hc_connect.py")


@pytest.fixture
def fake_hc(tmp_path, monkeypatch):
    """Put an `hc` on PATH that runs fake_hc_connect.py; return the call log path."""
    exe = tmp_path / "hc"
    exe.write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    log = tmp_path / "calls.jsonl"
    monkeypatch.setenv("FAKE_HC_LOG", str(log))
    return log


def test_argv_and_environment(fake_hc, monkeypatch):
    monkeypatch.setenv("HC_TERMINAL_ID", "t15")
    monkeypatch.setenv("HC_TERMINAL_SOCKET", "/run/x/default.sock")
    monkeypatch.setenv("HC_SCOPE", "s")
    client = HcRpcClient("cao")
    assert client.call("ping", {"a": 1}) == {"echo": {"a": 1}}
    client.close()
    entry = json.loads(fake_hc.read_text().splitlines()[0])
    assert entry["argv"] == ["connect", "--session", "cao"]
    env = entry["env"]
    assert env["HC_SESSION"] == "cao"
    for key in ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE"):
        assert key not in env


def test_error_kind_raised(fake_hc):
    client = HcRpcClient("cao")
    with pytest.raises(HcRpcError) as exc:
        client.call("fail")
    assert exc.value.kind == "not_found"
    assert isinstance(exc.value, TerminalBackendError)
    client.close()


def test_concurrent_calls_get_their_own_results(fake_hc):
    client = HcRpcClient("cao")
    results = {}

    def worker(i):
        results[i] = client.call("ping", {"i": i})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert all(results[i] == {"echo": {"i": i}} for i in range(8))
    client.close()


def test_eof_after_request_is_not_resent(fake_hc, monkeypatch):
    monkeypatch.setenv("FAKE_HC_MODE", "eof_after_request")
    client = HcRpcClient("cao")
    with pytest.raises(TerminalBackendError):
        client.call("terminal.run", {})
    assert len(fake_hc.read_text().splitlines()) == 1  # one process, no resend
    client.close()


def test_closed_connection_respawned_before_write(fake_hc):
    client = HcRpcClient("cao")
    client.call("ping")
    client._conn._proc.kill()  # simulate a dead connection between calls
    client._conn._proc.wait()
    assert client.call("ping", {"n": 2}) == {"echo": {"n": 2}}
    assert len(fake_hc.read_text().splitlines()) == 2
    client.close()


def test_hello_checks_generation_and_methods(fake_hc, monkeypatch):
    monkeypatch.setenv("FAKE_HC_METHODS", ",".join(sorted(hc_rpc.REQUIRED_METHODS)))
    HcRpcClient("cao").hello()
    monkeypatch.setenv("FAKE_HC_GEN", "2")
    with pytest.raises(HcProtocolError, match="generation 2"):
        HcRpcClient("cao").hello()
    monkeypatch.setenv("FAKE_HC_GEN", "1")
    monkeypatch.setenv("FAKE_HC_METHODS", "hello")
    with pytest.raises(HcProtocolError, match="terminal.run"):
        HcRpcClient("cao").hello()


def test_subscription_snapshot_then_notifications(fake_hc):
    sub = HcSubscription("cao")
    assert sub.snapshot == {"seq": 0, "terminals": []}
    assert sub.next_notification(5)["method"] == "event"
    assert sub.next_notification(5)["method"] == "heartbeat"
    with pytest.raises(TimeoutError):
        sub.next_notification(0.2)
    sub.close()
