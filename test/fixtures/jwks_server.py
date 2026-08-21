"""JWKSServer — standalone JWKS HTTP fixture for the CAO test suite.

Why this module exists
----------------------
The JWKS server was previously embedded as a private ``_JWKSServer`` class
inside ``test/fixtures/cao_server.py``. That class was only reachable through
the ``cao_server_with_auth`` fixture, which meant any test that needed to
verify JWT validation logic *without* a full subprocess had to either:

(a) Monkey-patch ``requests.get`` (the ``mock_jwks`` fixture in
    ``test/conftest.py`` — invisible to subprocesses), or
(b) Spin up the entire ``cao-server`` subprocess unnecessarily.

``JWKSServer`` is now a first-class public fixture:

- Usable standalone (``jwks_server`` pytest fixture) — binds a random port,
  serves one JWKS document, tears down on fixture finalisation.
- Still used by ``cao_server_with_auth`` in ``cao_server.py`` (which imports
  this module instead of duplicating the implementation).
- Accepts a ``JWTFactory`` directly, so there is no extra key-generation step.

Wire contract
-------------
``GET /.well-known/jwks.json`` returns::

    {"keys": [<RSA public key as JWK dict>]}

All other paths return 404. The server listens on ``127.0.0.1`` and a
kernel-allocated free port. ``JWKSServer.url`` returns the full URI including
the path component, ready for use as ``CAO_AUTH_JWKS_URI``.
"""

from __future__ import annotations

import contextlib
import http.server
import json
import threading
from typing import Any, Optional

import pytest


class JWKSServer:
    """Tiny stdlib HTTP server that serves one JWKS document.

    Designed to let a real cao-server subprocess verify JWTs minted by
    ``JWTFactory``. The subprocess fetches via ``CAO_AUTH_JWKS_URI``; this
    class binds a daemon thread to a fresh free port and serves the document.

    Usage::

        factory = JWTFactory.generate()
        server = JWKSServer(factory.jwks())
        server.start()
        # Point cao-server subprocess at server.url
        token = factory.mint(scopes="cao:read")
        ...
        server.stop()

    Or use the ``jwks_server`` pytest fixture which handles lifecycle::

        def test_something(jwks_server, jwt_factory):
            # jwks_server.url is already live; pass it as CAO_AUTH_JWKS_URI
            token = jwt_factory.mint(scopes="cao:read")
    """

    _JWKS_PATH = "/.well-known/jwks.json"

    def __init__(
        self,
        jwks_document: dict[str, Any],
        *,
        host: str = "127.0.0.1",
    ) -> None:
        self._host = host
        self._jwks_payload = json.dumps(jwks_document).encode("utf-8")
        self._server: Optional[http.server.ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.port: int = 0

    @classmethod
    def from_factory(cls, factory: Any, *, host: str = "127.0.0.1") -> "JWKSServer":
        """Convenience constructor — build from a ``JWTFactory`` instance."""
        return cls(factory.jwks(), host=host)

    def start(self) -> None:
        """Bind a free port and start serving. Idempotent."""
        if self._server is not None:
            return

        payload = self._jwks_payload
        target_path = self._JWKS_PATH

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 — stdlib API name
                if self.path != target_path:
                    self.send_response(404)
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return  # Suppress default stderr noise.

        self._server = http.server.ThreadingHTTPServer((self._host, 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="JWKSServer",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Shut down the server. Idempotent."""
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
        """Full ``http://host:port/.well-known/jwks.json`` URL.

        Raises ``RuntimeError`` if called before ``start()``.
        """
        if self.port == 0:
            raise RuntimeError("JWKSServer has not been started; call start() first")
        return f"http://{self._host}:{self.port}{self._JWKS_PATH}"

    def __enter__(self) -> "JWKSServer":
        self.start()
        return self

    def __exit__(self, *_: Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def jwks_server(jwt_factory: Any) -> "JWKSServer":  # type: ignore[return]
    """Session-scoped JWKSServer backed by the session ``jwt_factory``.

    Binds a random free port on ``127.0.0.1`` and serves the JWKS document
    for the session-scoped ``JWTFactory``. Use ``jwks_server.url`` as
    ``CAO_AUTH_JWKS_URI`` when pointing a subprocess at the test JWKS.

    Requires the ``jwt_factory`` session fixture (loaded via
    ``pytest_plugins = ("test.fixtures.jwt_factory",)``).
    """
    srv = JWKSServer.from_factory(jwt_factory)
    srv.start()
    yield srv
    srv.stop()


@pytest.fixture
def jwks_server_fn(jwt_factory_fn: Any) -> "JWKSServer":  # type: ignore[return]
    """Function-scoped JWKSServer backed by the function-scoped ``jwt_factory_fn``.

    Use when each test needs a fresh keypair + JWKS combination for complete
    isolation (e.g. testing that tokens from one keypair don't validate against
    a different JWKS).
    """
    srv = JWKSServer.from_factory(jwt_factory_fn)
    srv.start()
    yield srv
    srv.stop()


__all__ = ["JWKSServer", "jwks_server", "jwks_server_fn"]
