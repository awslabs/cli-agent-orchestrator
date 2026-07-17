"""Managed cao-server subprocess fixtures for integration tests.

Contract
--------
- ``cao_server`` (session-scoped): spawns a real ``cao-server`` subprocess on a
  free localhost port, redirects ``$HOME`` to a per-session tmp dir so all
  cao-managed paths (SQLite, logs, agent-card keys) land in isolation,
  disables optional lifespan subsystems (agent-card, A2A, OTel),
  waits for ``/health``, and yields a ``CaoServer`` dataclass with a
  ``stop()`` callable. Idempotent teardown via SIGTERM-to-process-group with
  SIGKILL escalation.

- ``cao_server_with_auth`` (session-scoped): wraps the same machinery with
  Auth0 enforcement enabled. Spawns an in-process stdlib JWKS HTTP server
  serving the public half of a fresh RSA-2048 keypair, points the subprocess
  at it via ``CAO_AUTH_JWKS_URI``, and yields an ``AuthCaoServer`` bundle
  (server + jwks + keys) so tests can mint tokens via ``mint_test_token``.

- ``cao_terminal`` (function-scoped): creates a fresh terminal via
  ``POST /sessions``, yields its ``terminal_id``, and cleans up on teardown.
  Provider/profile are parameterizable via indirect parametrization; default
  is ``("kiro_cli", "developer")``. The test is responsible for skipping if
  the chosen provider CLI is not on ``PATH``.

Design deviations (documented for review)
-------------------------------------------------------------------
- JWKS server uses stdlib ``http.server.ThreadingHTTPServer`` rather than
  ``aiohttp``. ``aiohttp`` is not in ``uv.lock`` (verified) and adding it
  pulls ~60 transitive deps for what is fundamentally a "serve one JSON
  document on one path" requirement.
- Audience is ``cao://test`` rather than ``cao://localhost`` — matches the
  default of ``test.conftest.mint_test_token`` so tests don't need to
  override the kwarg.
- ``cao_server_with_auth`` is session-scoped, not function-scoped. With
  ``CAO_AUTH_JWKS_CACHE_TTL=0`` there is no JWKS state to reset between
  tests; amortizing subprocess startup matters more for the 4-scenario
  matrix.

DO NOT use ``test.conftest.mock_jwks`` with the auth variant. That fixture
patches ``requests.get`` in the *test* process and is invisible to the
subprocess. The in-process ``_JWKSServer`` here is the correct path.
"""

from __future__ import annotations

import atexit
import contextlib
import http.server
import importlib
import json
import os
import pkgutil
import signal
import socket
import socketserver
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Callable, Iterator, Mapping, Optional

import pytest
import requests

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HEALTH_POLL_INTERVAL = 0.05
_HEALTH_TIMEOUT_DEFAULT = 8.0
_STOP_GRACE_SECONDS = 5.0
_LOG_TAIL_LINES = 80

_AUTH_DOMAIN = "test.local"
_AUTH_AUDIENCE = "cao://test"


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaoServer:
    """Handle to a managed cao-server subprocess.

    Fields:
        url: ``http://127.0.0.1:<port>`` — base URL for all HTTP calls.
        port: The TCP port the subprocess is listening on.
        home_dir: The redirected ``$HOME``. All persistent cao paths live
            under ``home_dir / ".aws" / "cli-agent-orchestrator" / ...``.
        db_path: SQLite database (``DATABASE_FILE`` from
            ``cli_agent_orchestrator.constants``).
        log_path: File where the subprocess's merged stdout+stderr is
            appended; tail it for failure diagnostics.
        stop: Idempotent teardown callable.
    """

    url: str
    port: int
    home_dir: Path
    db_path: Path
    log_path: Path
    stop: Callable[[], None]


@dataclass(frozen=True)
class AuthCaoServer:
    """Bundle of an auth-enabled cao-server + the JWKS server + the keypair.

    ``private_pem`` is what tests pass to ``mint_test_token``. ``public_jwk``
    is what the in-process JWKS server is serving.
    """

    server: CaoServer
    jwks: "_JWKSServer"
    private_pem: bytes
    public_jwk: Any  # authlib.jose.JsonWebKey — typed Any to avoid an import here


# ---------------------------------------------------------------------------
# Process-wide registry for atexit cleanup
# ---------------------------------------------------------------------------

_LIVE_SERVERS: "set[CaoServer]" = set()


def _atexit_cleanup() -> None:
    for srv in list(_LIVE_SERVERS):
        with contextlib.suppress(Exception):
            srv.stop()


atexit.register(_atexit_cleanup)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pick_free_port(host: str = "127.0.0.1") -> int:
    """Allocate a free TCP port on ``host`` and return it.

    The kernel does not immediately re-issue the port to another caller, but a
    TOCTOU window exists between close and the subsequent ``bind`` in
    ``Popen``. Under normal load this is microseconds; under heavy parallel
    spawn it can collide. The fixture surfaces a clear log-tail diagnostic if
    that happens.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((host, 0))
        return int(s.getsockname()[1])


def _tail(path: Path, lines: int = _LOG_TAIL_LINES) -> str:
    """Return the last ``lines`` lines of ``path`` (or a placeholder)."""
    try:
        return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])
    except FileNotFoundError:
        return "<log file not created>"
    except OSError as exc:
        return f"<log read failed: {exc}>"


def _subprocess_env(
    home_dir: Path,
    port: int,
    *,
    extra: Optional[Mapping[str, str]] = None,
) -> dict[str, str]:
    """Compose the env passed to the subprocess.

    Starts from a copy of the parent env, deletes any leaked Auth0 vars (so a
    developer's exported ``AUTH0_DOMAIN`` doesn't accidentally enable auth in
    the no-auth variant), then applies the standard test overrides plus any
    ``extra`` knobs.
    """
    env = os.environ.copy()
    for leaked in ("AUTH0_DOMAIN", "AUTH0_AUDIENCE", "CAO_AUTH_JWKS_URI"):
        env.pop(leaked, None)

    env.update(
        {
            "HOME": str(home_dir),
            "CAO_API_HOST": "127.0.0.1",
            "CAO_API_PORT": str(port),
            "CAO_A2A_DISABLED": "true",
            "OTEL_SDK_DISABLED": "true",
            "PYTHONUNBUFFERED": "1",
        }
    )
    if extra:
        env.update(extra)
    return env


def _wait_for_health(
    url: str,
    *,
    deadline: float,
    process: subprocess.Popen,
    log_path: Path,
) -> None:
    """Poll ``GET {url}/health`` until 200 or ``deadline``.

    Raises ``RuntimeError`` with a log tail if the subprocess dies during
    startup or the deadline expires.
    """
    end = time.monotonic() + deadline
    last_error: Optional[str] = None
    while time.monotonic() < end:
        if process.poll() is not None:
            raise RuntimeError(
                f"cao-server exited with code {process.returncode} before "
                f"/health became ready.\nLog tail:\n{_tail(log_path)}"
            )
        try:
            resp = requests.get(f"{url}/health", timeout=0.5)
            if resp.status_code == 200 and resp.json().get("status") == "ok":
                return
            last_error = f"status={resp.status_code} body={resp.text[:200]}"
        except (requests.ConnectionError, requests.Timeout) as exc:
            last_error = type(exc).__name__
        time.sleep(_HEALTH_POLL_INTERVAL)

    raise RuntimeError(
        f"cao-server at {url} did not become healthy within {deadline}s "
        f"(last_error={last_error}).\nLog tail:\n{_tail(log_path)}"
    )


def _terminate(
    process: subprocess.Popen,
    log_handle: Any,
    stopped: list[bool],
) -> None:
    """Idempotent SIGTERM-to-group → wait → SIGKILL escalation."""
    if stopped[0]:
        return
    stopped[0] = True

    pid = process.pid
    if process.poll() is None:
        with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
            os.killpg(pid, signal.SIGTERM)
        try:
            process.wait(timeout=_STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
                os.killpg(pid, signal.SIGKILL)
            with contextlib.suppress(subprocess.TimeoutExpired):
                process.wait(timeout=_STOP_GRACE_SECONDS)

    if log_handle is not None and not log_handle.closed:
        with contextlib.suppress(Exception):
            log_handle.close()


# ---------------------------------------------------------------------------
# JWKS server (in-process, stdlib)
# ---------------------------------------------------------------------------


class _JWKSServer:
    """Tiny stdlib HTTP server that serves one JWKS document.

    Used by ``cao_server_with_auth`` to let the cao-server subprocess verify
    test-minted JWTs against a public key we control. The subprocess fetches
    via ``CAO_AUTH_JWKS_URI``; this class binds a daemon thread to a fresh
    free port and serves ``GET /.well-known/jwks.json``.
    """

    _JWKS_PATH = "/.well-known/jwks.json"

    def __init__(self, public_jwk: Any, host: str = "127.0.0.1") -> None:
        self._host = host
        self._public_jwk = public_jwk
        self._server: Optional[socketserver.TCPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    def start(self) -> None:
        if self._server is not None:
            return

        jwks_payload = json.dumps({"keys": [self._public_jwk.as_dict()]}).encode("utf-8")
        target_path = self._JWKS_PATH

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib API name
                if self.path != target_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(jwks_payload)))
                self.end_headers()
                self.wfile.write(jwks_payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # Silence default stderr noise.

        self._server = http.server.ThreadingHTTPServer((self._host, 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="JWKSServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        with contextlib.suppress(Exception):
            self._server.shutdown()
        with contextlib.suppress(Exception):
            self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._server = None
        self._thread = None

    @property
    def url(self) -> str:
        if self.port == 0:
            raise RuntimeError("JWKS server has not been started")
        return f"http://{self._host}:{self.port}{self._JWKS_PATH}"


# ---------------------------------------------------------------------------
# Core spawn helper (exposed for self-tests)
# ---------------------------------------------------------------------------


def _seed_packaged_skills(home_dir: Path) -> None:
    """Copy package-bundled skills into the redirected HOME.

    With HOME isolated to a tmp dir, ``SKILLS_DIR`` (=``$HOME/.aws/cli-agent-
    orchestrator/skills``) is empty. Older tests assumed the
    developer had run ``cao install <name>`` and that the skill was on disk.
    Seeding the bundled skills here restores that expectation without
    leaking state into the developer's real ``~/.aws/``.
    """
    import shutil

    import cli_agent_orchestrator.skills as _skills_pkg

    src = Path(_skills_pkg.__file__).parent
    dest = home_dir / ".aws" / "cli-agent-orchestrator" / "skills"
    dest.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        if entry.name.startswith("_") or entry.name == "__pycache__":
            continue
        target = dest / entry.name
        if target.exists():
            continue
        if entry.is_dir():
            shutil.copytree(entry, target)
        else:
            shutil.copy2(entry, target)


def _start_cao_server(
    home_dir: Path,
    port: int,
    *,
    extra_env: Optional[Mapping[str, str]] = None,
    deadline: float = _HEALTH_TIMEOUT_DEFAULT,
) -> CaoServer:
    """Spawn a cao-server subprocess and return a managed ``CaoServer``.

    Used directly by both the session fixture and the self-tests that exercise
    parallelism / idempotency. Callers own the lifetime via the returned
    ``stop`` callable.
    """
    home_dir.mkdir(parents=True, exist_ok=True)
    _seed_packaged_skills(home_dir)
    log_path = home_dir / "server.log"
    log_handle = open(log_path, "ab")  # noqa: SIM115 — handle lifetime is in stop()

    env = _subprocess_env(home_dir, port, extra=extra_env)
    url = f"http://127.0.0.1:{port}"

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "cli_agent_orchestrator.api.main",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )

    try:
        _wait_for_health(url, deadline=deadline, process=process, log_path=log_path)
    except BaseException:
        _terminate(process, log_handle, [False])
        raise

    stopped = [False]

    def _stop() -> None:
        _terminate(process, log_handle, stopped)
        _LIVE_SERVERS.discard(server)

    db_path = home_dir / ".aws" / "cli-agent-orchestrator" / "db" / "cli-agent-orchestrator.db"
    server = CaoServer(
        url=url,
        port=port,
        home_dir=home_dir,
        db_path=db_path,
        log_path=log_path,
        stop=_stop,
    )
    _LIVE_SERVERS.add(server)
    return server


# ---------------------------------------------------------------------------
# Session-scoped RSA keypair
# ---------------------------------------------------------------------------


_SESSION_RSA_CACHE: dict[str, tuple[bytes, Any]] = {}


def _session_rsa_keys() -> tuple[bytes, Any]:
    """Session-scoped RSA-2048 keypair (matches ``test.conftest.rsa_keys``).

    ``test.conftest.rsa_keys`` is function-scoped, which is right for unit
    tests but wrong for our session-scoped auth fixture. This helper mirrors
    the keypair-generation logic and caches the result for the session.
    """
    if "keys" not in _SESSION_RSA_CACHE:
        from authlib.jose import JsonWebKey
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        private_pem = private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
        public_pem = private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_jwk = JsonWebKey.import_key(
            public_pem,
            {"kty": "RSA", "use": "sig", "kid": "test-kid"},
        )
        _SESSION_RSA_CACHE["keys"] = (private_pem, public_jwk)
    return _SESSION_RSA_CACHE["keys"]


# ---------------------------------------------------------------------------
# Public fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def cao_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CaoServer]:
    """Spawn a managed cao-server subprocess for the whole session."""
    home = tmp_path_factory.mktemp("cao_home_session")
    port = _pick_free_port()
    server = _start_cao_server(home, port)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def cao_server_with_auth(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[AuthCaoServer]:
    """Spawn a managed cao-server subprocess with Auth0 enforcement enabled.

    The subprocess is pointed at an in-process JWKS HTTP server that serves
    the public half of a fresh RSA-2048 keypair. Tests mint matching tokens
    via ``test.conftest.mint_test_token`` using the bundle's ``private_pem``.
    """
    private_pem, public_jwk = _session_rsa_keys()

    jwks = _JWKSServer(public_jwk)
    jwks.start()

    server: Optional[CaoServer] = None
    try:
        home = tmp_path_factory.mktemp("cao_home_authed_session")
        port = _pick_free_port()
        server = _start_cao_server(
            home,
            port,
            extra_env={
                "AUTH0_DOMAIN": _AUTH_DOMAIN,
                "AUTH0_AUDIENCE": _AUTH_AUDIENCE,
                "CAO_AUTH_JWKS_URI": jwks.url,
                "CAO_AUTH_JWKS_CACHE_TTL": "0",
            },
        )
        yield AuthCaoServer(
            server=server,
            jwks=jwks,
            private_pem=private_pem,
            public_jwk=public_jwk,
        )
    finally:
        if server is not None:
            server.stop()
        jwks.stop()


@pytest.fixture
def cao_terminal(
    cao_server: CaoServer,
    request: pytest.FixtureRequest,
) -> Iterator[str]:
    """Create a terminal on ``cao_server`` and clean it up on teardown.

    Provider/profile are parameterizable via indirect parametrization:

        @pytest.mark.parametrize(
            "cao_terminal",
            [{"provider": "claude_code", "agent_profile": "supervisor"}],
            indirect=True,
        )

    Default is ``("kiro_cli", "developer")``. Tests are responsible for
    skipping if the chosen provider CLI is not available on ``PATH``.
    """
    params = getattr(request, "param", None) or {}
    provider = params.get("provider", "kiro_cli")
    profile = params.get("agent_profile", "developer")
    session_name = f"caotest-{uuid.uuid4().hex[:12]}"

    resp = requests.post(
        f"{cao_server.url}/sessions",
        params={
            "provider": provider,
            "agent_profile": profile,
            "session_name": session_name,
        },
    )
    if resp.status_code not in (200, 201):
        # Provider boot is fragile — CLI may be installed but unauthenticated,
        # rate-limited, or slow to TUI-init. Treat any 5xx that names the
        # provider as a skip, not a fixture-contract failure. The integration
        # tests own provider responsiveness.
        body = resp.text
        if resp.status_code >= 500 and any(
            marker in body.lower()
            for marker in (
                "initialization timed out",
                "not installed",
                "not found",
                "command not found",
                provider.lower(),
            )
        ):
            pytest.skip(
                f"provider {provider!r} not usable on this host "
                f"(HTTP {resp.status_code}): {body[:200]}"
            )
        raise RuntimeError(f"POST /sessions failed: {resp.status_code} {body}")
    data = resp.json()
    terminal_id = data["id"]
    actual_session = data["session_name"]

    try:
        yield terminal_id
    finally:
        with contextlib.suppress(Exception):
            requests.post(f"{cao_server.url}/terminals/{terminal_id}/exit")
        time.sleep(2)
        with contextlib.suppress(Exception):
            requests.delete(f"{cao_server.url}/sessions/{actual_session}")


# ---------------------------------------------------------------------------
# E2E conftest support — module-attribute patch for back-compat with the
# older e2e modules that import ``API_BASE_URL`` from
# ``cli_agent_orchestrator.constants`` at module top.
# ---------------------------------------------------------------------------


def _patch_api_base_url_for_e2e(
    cao_server_obj: CaoServer,
) -> Callable[[], None]:
    """Rewrite ``API_BASE_URL`` everywhere it has been bound by import-time.

    The older e2e modules do ``from cli_agent_orchestrator.constants
    import API_BASE_URL`` at module top, binding their own local copy of the
    string. Pytest collection imports those modules before any fixture runs,
    so simply mutating the constants module after the fact won't reach them.

    This helper:
      1. Snapshots and rewrites ``API_BASE_URL`` / ``SERVER_PORT`` on the
         constants module itself.
      2. Walks ``test.e2e`` via ``pkgutil``, importing each module, and on
         any module that has its own ``API_BASE_URL`` attribute, snapshots
         and overwrites it.

    Returns a ``restore()`` callable that puts every snapshotted attribute
    back to its original value.
    """
    import cli_agent_orchestrator.constants as _constants

    patches: list[tuple[ModuleType, str, Any]] = [
        (_constants, "API_BASE_URL", _constants.API_BASE_URL),
        (_constants, "SERVER_PORT", _constants.SERVER_PORT),
    ]
    _constants.API_BASE_URL = cao_server_obj.url
    _constants.SERVER_PORT = cao_server_obj.port

    try:
        import test.e2e as _e2e_pkg
    except ImportError:
        _e2e_pkg = None  # type: ignore[assignment]

    if _e2e_pkg is not None:
        for info in pkgutil.iter_modules(_e2e_pkg.__path__, prefix="test.e2e."):
            try:
                mod = importlib.import_module(info.name)
            except Exception:
                continue
            if hasattr(mod, "API_BASE_URL"):
                patches.append((mod, "API_BASE_URL", mod.API_BASE_URL))
                mod.API_BASE_URL = cao_server_obj.url

    def restore() -> None:
        for mod, attr, original in patches:
            setattr(mod, attr, original)

    return restore
