"""The controller's local bearer only travels to its OWN server (haofeif P1 #802).

``orchestration._auth_headers`` used to attach ``CAO_AUTH_LOCAL_TOKEN`` to every
``requests`` call, including ones whose base URL is a caller-supplied
``target_host``. That leaked the controller's credential to an arbitrary host on
the very first ``/health`` probe. These tests spin a loopback HTTP stub that
records inbound ``Authorization`` headers and drive each target-host path against
it, asserting the stub sees no credential — and a control case pointed at the
process's own server still does.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from cli_agent_orchestrator.utils import orchestration

_LOCAL_TOKEN = "synthetic-local-token-value"


class _RecordingHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # silence stderr access logs
        pass

    def _record_and_reply(self):
        self.server.auth_headers_seen.append(self.headers.get("Authorization"))
        body = json.dumps({"provider": "kiro_cli", "id": "abcd1234"}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = _record_and_reply
    do_POST = _record_and_reply
    do_DELETE = _record_and_reply


class _Stub:
    def __init__(self):
        self.httpd = HTTPServer(("127.0.0.1", 0), _RecordingHandler)
        self.httpd.auth_headers_seen = []
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    @property
    def host_port(self):
        host, port = self.httpd.server_address
        return f"{host}:{port}"

    @property
    def base_url(self):
        return f"http://{self.host_port}"

    @property
    def seen(self):
        return self.httpd.auth_headers_seen

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def stub():
    s = _Stub()
    try:
        yield s
    finally:
        s.stop()


@pytest.fixture
def auth_local_token(monkeypatch):
    """Enable auth and provision the controller's own local token."""
    monkeypatch.setenv("CAO_AUTH_JWKS_URI", "https://idp.test/.well-known/jwks.json")
    monkeypatch.setenv("CAO_AUTH_LOCAL_TOKEN", _LOCAL_TOKEN)


def test_target_host_paths_carry_no_local_bearer(stub, auth_local_token, monkeypatch):
    """Health probe, profile lookup, cleanup, remote delete and remote assign to a
    foreign target_host send no Authorization header."""
    # Own server is explicitly NOT the stub, so the stub is a foreign host.
    monkeypatch.setenv("CAO_API_BASE_URL", "http://127.0.0.1:1")

    orchestration._wait_remote_ready(stub.base_url, timeout=2.0)
    orchestration._resolve_remote_provider(stub.base_url, "some_profile")
    orchestration._cleanup_remote_terminal(stub.base_url, "abcd1234")
    orchestration._delete_terminal_impl("abcd1234", target_host=stub.host_port)
    orchestration._assign_remote(
        agent_profile="some_profile",
        worker_message="hello",
        current_terminal_id="deadbeef",
        target_host=stub.host_port,
        working_directory=None,
        engine=None,
        model=None,
        use_worktree=False,
        ready_wait_seconds=0.0,
    )

    assert stub.seen, "stub recorded no requests — the paths did not run"
    assert all(h is None for h in stub.seen), f"local bearer leaked to foreign host: {stub.seen}"


def test_own_server_still_receives_the_bearer(stub, auth_local_token, monkeypatch):
    """Control: when the destination IS this process's own server, the bearer is sent."""
    # Point this process's own server at the stub, so its origin matches.
    monkeypatch.setenv("CAO_API_BASE_URL", stub.base_url)

    orchestration._wait_remote_ready(stub.base_url, timeout=2.0)

    assert stub.seen, "stub recorded no requests"
    assert all(
        h == f"Bearer {_LOCAL_TOKEN}" for h in stub.seen
    ), f"own-server call did not carry the bearer: {stub.seen}"
