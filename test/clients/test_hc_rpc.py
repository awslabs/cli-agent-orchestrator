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


CALLER_CORE_ENV = ("HC_TERMINAL_ID", "HC_TERMINAL_SOCKET", "HC_SCOPE", "HC_SOCKET")


@pytest.fixture
def inside_app_terminal(monkeypatch):
    """The variables an app terminal hands its programs, all naming the user's core."""
    monkeypatch.setenv("HC_TERMINAL_ID", "t15")
    monkeypatch.setenv("HC_TERMINAL_SOCKET", "/run/x/default.sock")
    monkeypatch.setenv("HC_SCOPE", "s")
    monkeypatch.setenv("HC_SOCKET", "/run/x/default.sock")
    monkeypatch.setenv("HC_SESSION", "default")


def test_argv_and_environment(fake_hc, inside_app_terminal):
    client = HcRpcClient("cao")
    assert client.call("ping", {"a": 1}) == {"echo": {"a": 1}}
    client.close()
    entry = json.loads(fake_hc.read_text().splitlines()[0])
    assert entry["argv"] == ["connect", "--session", "cao"]
    env = entry["env"]
    assert env["HC_SESSION"] == "cao"
    for key in CALLER_CORE_ENV:
        assert key not in env


def test_server_start_environment(fake_hc, inside_app_terminal):
    HcRpcClient("cao").start_server(drop_env=lambda k: k == "CLAUDECODE")
    entry = json.loads(fake_hc.read_text().splitlines()[0])
    assert entry["argv"] == ["server", "start", "--json"]
    assert entry["env"]["HC_SESSION"] == "cao"
    for key in CALLER_CORE_ENV:
        assert key not in entry["env"]


def test_subscription_closes_its_connection_when_subscribe_fails(fake_hc, monkeypatch):
    monkeypatch.setenv("FAKE_HC_MODE", "fail_subscribe")
    made = []
    real = hc_rpc._Connection

    class Tracked(real):  # type: ignore[misc, valid-type]
        def __init__(self, session):
            super().__init__(session)
            made.append(self)

    monkeypatch.setattr(hc_rpc, "_Connection", Tracked)
    with pytest.raises(HcRpcError):
        HcSubscription("cao")
    assert len(made) == 1 and not made[0].alive()


def test_subscription_closes_its_connection_when_the_core_is_silent(fake_hc, monkeypatch):
    made = []
    real = hc_rpc._Connection

    class Silent(real):  # type: ignore[misc, valid-type]
        def __init__(self, session):
            super().__init__(session)
            made.append(self)

        def get(self, timeout):
            raise TimeoutError("silent")

    monkeypatch.setattr(hc_rpc, "_Connection", Silent)
    with pytest.raises(TimeoutError):
        HcSubscription("cao")
    assert len(made) == 1 and not made[0].alive()


def test_subscribe_sends_no_filter(fake_hc, monkeypatch):
    sent = []
    real = hc_rpc._Connection

    class Spy(real):  # type: ignore[misc, valid-type]
        def write(self, message):
            sent.append(message)
            super().write(message)

    monkeypatch.setattr(hc_rpc, "_Connection", Spy)
    HcSubscription("cao").close()
    assert [(m["method"], m["params"]) for m in sent] == [("events.subscribe", {})]


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


def test_close_does_not_wait_for_a_held_call_and_is_final(fake_hc):
    """close() ends a call the core is holding instead of waiting up to its timeout."""
    import time

    client = HcRpcClient("cao")
    client.call("ping")  # the child is up
    failed = []

    def held():
        try:
            client.call("hold", timeout=30)
        except TerminalBackendError as exc:
            failed.append(exc)

    caller = threading.Thread(target=held)
    caller.start()
    deadline = time.monotonic() + 5
    while not client._lock.locked() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)  # the request is on the wire
    started = time.monotonic()
    client.close()
    caller.join(5)
    assert not caller.is_alive() and time.monotonic() - started < 5
    assert failed and "lost hold" in str(failed[0])
    with pytest.raises(TerminalBackendError, match="closed before ping"):
        client.call("ping")
    assert len(fake_hc.read_text().splitlines()) == 1  # no new `hc connect` after close


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


# reply-delivery-failure, inbox-delivery "Delivery failures are classified":
# HcNotSentError means the request never reached herdr-core, so nothing it
# asked for can have run. A request that was written and then lost is not that.


def test_request_never_written_is_not_sent(fake_hc, monkeypatch):
    """Spec scenario "Send request never written"."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError, _Connection

    def broken(self, message):
        raise BrokenPipeError()

    monkeypatch.setattr(_Connection, "write", broken)
    client = HcRpcClient("cao")
    with pytest.raises(HcNotSentError, match="unreachable for terminal.send") as raised:
        client.call("terminal.send", {})
    assert isinstance(raised.value, TerminalBackendError)
    client.close()


def test_call_on_a_closed_client_is_not_sent(fake_hc):
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    client = HcRpcClient("cao")
    client.close()
    with pytest.raises(HcNotSentError, match="closed before terminal.send"):
        client.call("terminal.send", {})
    assert not fake_hc.exists(), "no `hc connect` is started for a closed client"


def test_request_lost_after_write_is_not_not_sent(fake_hc, monkeypatch):
    """Spec scenario "Send request lost after it was written": the core may have typed."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    monkeypatch.setenv("FAKE_HC_MODE", "eof_after_request")
    client = HcRpcClient("cao")
    with pytest.raises(TerminalBackendError, match="lost terminal.send") as raised:
        client.call("terminal.send", {})
    assert not isinstance(raised.value, HcNotSentError)
    client.close()


def test_request_unanswered_before_the_timeout_is_not_not_sent(fake_hc):
    """herdr-core took the request and sent no answer in time (the incident's terminal.read)."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    client = HcRpcClient("cao")
    with pytest.raises(TerminalBackendError, match="lost hold") as raised:
        client.call("hold", {}, timeout=0.5)
    assert not isinstance(raised.value, HcNotSentError)
    client.close()


def test_error_answer_from_the_core_is_not_not_sent(fake_hc):
    """An explicit error answer is HcRpcError; which kinds typed nothing is the backend's call."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    client = HcRpcClient("cao")
    with pytest.raises(HcRpcError) as raised:
        client.call("fail")
    assert not isinstance(raised.value, HcNotSentError)
    client.close()


def test_connection_that_cannot_start_is_not_sent(fake_hc, monkeypatch):
    """Spec scenario "Send request never written": `hc connect` could not be started."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    def no_process(*_args, **_kwargs):
        raise OSError(24, "Too many open files")

    monkeypatch.setattr(hc_rpc.subprocess, "Popen", no_process)
    client = HcRpcClient("cao")
    with pytest.raises(HcNotSentError, match="terminal.send|hc connect") as raised:
        client.call("terminal.send", {})
    assert isinstance(raised.value, TerminalBackendError)
    assert not fake_hc.exists(), "no `hc connect` ran"
    client.close()


def test_missing_hc_binary_is_not_sent(fake_hc, monkeypatch):
    """Spec scenario "Send request never written": neither `hc` nor `herdr-core` on PATH."""
    from cli_agent_orchestrator.clients.hc_rpc import HcNotSentError

    monkeypatch.setattr(hc_rpc.shutil, "which", lambda _name: None)
    client = HcRpcClient("cao")
    with pytest.raises(HcNotSentError, match="not installed|terminal.send"):
        client.call("terminal.send", {})
    assert not fake_hc.exists(), "no `hc connect` ran"
    client.close()
