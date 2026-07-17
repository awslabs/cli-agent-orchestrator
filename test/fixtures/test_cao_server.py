"""Self-tests for the managed cao-server fixtures.

Run with: ``uv run pytest -m e2e test/fixtures/test_cao_server.py -v``.

Each test owns its own lifecycle except where it depends on the session
fixtures (``cao_server``, ``cao_server_with_auth``). The parallelism and
idempotency tests spawn ad-hoc cao-server instances via ``_start_cao_server``
so they don't disturb the session-scoped server.
"""

from __future__ import annotations

import concurrent.futures
import contextlib
import shutil
import time
from pathlib import Path
from test.conftest import mint_test_token
from test.fixtures.cao_server import (
    AuthCaoServer,
    CaoServer,
    _JWKSServer,
    _pick_free_port,
    _session_rsa_keys,
    _start_cao_server,
)

import pytest
import requests

pytestmark = pytest.mark.e2e


# ---------------------------------------------------------------------------
# cao_server (session) — basic shape
# ---------------------------------------------------------------------------


def test_health_reachable(cao_server: CaoServer) -> None:
    resp = requests.get(f"{cao_server.url}/health", timeout=2.0)
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["service"] == "cli-agent-orchestrator"


def test_url_uses_dynamic_port(cao_server: CaoServer) -> None:
    assert cao_server.port != 9889, "fixture must allocate a free port, not the default"
    assert f":{cao_server.port}" in cao_server.url
    assert cao_server.url.startswith("http://127.0.0.1:")


def test_home_isolated(cao_server: CaoServer) -> None:
    """Subprocess writes land under the redirected HOME, not the dev's real one."""
    cao_root = cao_server.home_dir / ".aws" / "cli-agent-orchestrator"
    assert cao_root.exists(), f"subprocess never wrote anything under {cao_root}"
    # The logs subdir is created during lifespan setup_logging().
    assert (cao_root / "logs").exists()


def test_log_file_populated(cao_server: CaoServer) -> None:
    assert cao_server.log_path.exists()
    content = cao_server.log_path.read_text(errors="replace")
    # uvicorn or our logger prints something during startup. Don't pin on a
    # specific banner — just confirm bytes flowed.
    assert content.strip(), "subprocess produced no stdout/stderr output"


# ---------------------------------------------------------------------------
# Ad-hoc server (idempotency, parallelism, failure-diagnostics) — uses
# _start_cao_server directly so we don't kill the session fixture.
# ---------------------------------------------------------------------------


def test_stop_is_idempotent(tmp_path_factory: pytest.TempPathFactory) -> None:
    home = tmp_path_factory.mktemp("cao_idempotent")
    server = _start_cao_server(home, _pick_free_port())
    try:
        server.stop()
        server.stop()  # second call must not raise
    finally:
        with contextlib.suppress(Exception):
            server.stop()


def test_parallel_ports_distinct(tmp_path_factory: pytest.TempPathFactory) -> None:
    """Two concurrent _start_cao_server calls get distinct ports and both work."""
    homes = [
        tmp_path_factory.mktemp("cao_par_a"),
        tmp_path_factory.mktemp("cao_par_b"),
    ]

    def _spawn(home: Path) -> CaoServer:
        return _start_cao_server(home, _pick_free_port())

    servers: list[CaoServer] = []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(_spawn, h) for h in homes]
            servers = [f.result() for f in futures]
        a, b = servers
        assert a.port != b.port, "concurrent allocations collided on the same port"
        for srv in servers:
            r = requests.get(f"{srv.url}/health", timeout=2.0)
            assert r.status_code == 200
    finally:
        for srv in servers:
            with contextlib.suppress(Exception):
                srv.stop()


def test_startup_failure_raises_with_log_tail(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A subprocess that crashes during startup surfaces a diagnostic, not a hang."""
    home = tmp_path_factory.mktemp("cao_crash")
    with pytest.raises(RuntimeError) as excinfo:
        _start_cao_server(
            home,
            _pick_free_port(),
            extra_env={"CAO_API_PORT": "not-an-int"},
            deadline=4.0,
        )
    msg = str(excinfo.value)
    assert "Log tail" in msg
    # The subprocess died (exit code) OR the deadline expired — either is fine.
    assert "cao-server" in msg


# ---------------------------------------------------------------------------
# JWKS server — direct unit
# ---------------------------------------------------------------------------


def test_jwks_server_returns_expected_key() -> None:
    _, public_jwk = _session_rsa_keys()
    server = _JWKSServer(public_jwk)
    server.start()
    try:
        resp = requests.get(server.url, timeout=2.0)
        assert resp.status_code == 200
        body = resp.json()
        assert "keys" in body
        assert len(body["keys"]) == 1
        assert body["keys"][0]["kid"] == "test-kid"
        assert body["keys"][0]["kty"] == "RSA"
    finally:
        server.stop()


def test_jwks_server_stop_is_idempotent() -> None:
    _, public_jwk = _session_rsa_keys()
    server = _JWKSServer(public_jwk)
    server.start()
    server.stop()
    server.stop()  # second call must not raise


def test_jwks_server_404_on_other_paths() -> None:
    _, public_jwk = _session_rsa_keys()
    server = _JWKSServer(public_jwk)
    server.start()
    try:
        base = server.url.rsplit("/", 1)[0]
        resp = requests.get(f"{base}/some-other-path", timeout=2.0)
        assert resp.status_code == 404
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# cao_server_with_auth — subprocess sees real Auth0 enforcement
# ---------------------------------------------------------------------------


def test_auth_rejects_no_token(cao_server_with_auth: AuthCaoServer) -> None:
    """Subprocess with AUTH0_DOMAIN set must reject unauthenticated mutations.

    Targets a write-scoped endpoint (DELETE /sessions/...) which is gated
    on cao:admin. With no Authorization header the dependency chain stops
    at 401 before reaching the not-found check.
    """
    resp = requests.delete(
        f"{cao_server_with_auth.server.url}/sessions/nonexistent-test",
        timeout=2.0,
    )
    assert (
        resp.status_code == 401
    ), f"expected 401 with no Authorization header, got {resp.status_code}: {resp.text}"


def test_auth_accepts_valid_token(cao_server_with_auth: AuthCaoServer) -> None:
    """Subprocess accepts a JWT signed with the matching JWKS keypair.

    With a full-scope token the same DELETE that returned 401 above should
    advance past the auth gate. The session itself doesn't exist, so the
    handler returns 404 / 500 — anything but 401 proves auth passed.
    """
    token = mint_test_token(cao_server_with_auth.private_pem)
    resp = requests.delete(
        f"{cao_server_with_auth.server.url}/sessions/nonexistent-test",
        headers={"Authorization": f"Bearer {token}"},
        timeout=2.0,
    )
    assert (
        resp.status_code != 401
    ), f"expected non-401 with valid Bearer token, got {resp.status_code}: {resp.text}"


# ---------------------------------------------------------------------------
# cao_terminal — gated on a provider CLI being available
# ---------------------------------------------------------------------------


def test_cao_terminal_create_and_get(
    cao_server: CaoServer,
    cao_terminal: str,
) -> None:
    if not shutil.which("kiro-cli"):
        pytest.skip("kiro-cli not available — default cao_terminal provider needs it")
    assert isinstance(cao_terminal, str) and cao_terminal
    # The terminal endpoint should be reachable while the fixture holds the
    # session open. The status field is provider-dependent — we only assert
    # that the resource exists and the API responds.
    resp = requests.get(
        f"{cao_server.url}/terminals/{cao_terminal}",
        timeout=2.0,
    )
    assert resp.status_code == 200, f"GET /terminals/{cao_terminal} returned {resp.status_code}"
