"""TerminalFactory — reusable terminal lifecycle helper for e2e tests.

Why this module exists
----------------------
Terminal creation was duplicated in two places:

- ``test/fixtures/cao_server.py`` — the ``cao_terminal`` fixture (no-auth path)
- the authenticated e2e path — terminal creation with an ``Authorization``
  header plus matching teardown

Both patterns share the same shape:
  1. POST /sessions with optional Authorization header
  2. Handle provider-boot failures as pytest.skip (not fixture errors)
  3. Clean up on teardown via POST /exit + DELETE /sessions/<name>

``TerminalFactory`` is the single implementation for both paths. It is
intentionally *not* a pytest fixture itself — it is a helper class that the
``cao_terminal`` and ``cao_terminal_authed`` fixtures below wrap. Direct
instantiation is also fine for tests that need fine-grained control.

Provider skip contract
----------------------
Any 5xx response whose body mentions the provider name, "not installed",
"not found", or "initialization timed out" is treated as a provider-
unavailability skip (not a fixture failure). This matches the behaviour of
the no-auth and authenticated e2e terminal paths, ensuring e2e tests that
require real CLIs degrade gracefully in CI environments where those CLIs
aren't installed.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from dataclasses import dataclass
from test.fixtures.cao_server import CaoServer
from typing import Any, Optional

import pytest
import requests

_DEFAULT_PROVIDER = "mock_cli"
_DEFAULT_PROFILE = "developer"
_SKIP_MARKERS = frozenset(
    ["initialization timed out", "not installed", "not found", "command not found"]
)
_IDLE_POLL_INTERVAL = 0.5
_IDLE_POLL_TIMEOUT = 30.0


@dataclass
class TerminalHandle:
    """Lifecycle handle for a single terminal created by ``TerminalFactory``.

    Fields
    ------
    terminal_id : str
        Opaque terminal ID returned by POST /sessions.
    session_name : str
        The ``cao-`` prefixed session name used for cleanup.
    window_name : str
        tmux window name; used for pane-width introspection in tests.
    server_url : str
        Base HTTP URL of the ``cao_server`` that owns this terminal.
    auth_token : str or None
        The token used to create the terminal (if any). Passed to the cleanup
        calls so auth-gated servers don't reject DELETE /sessions.
    """

    terminal_id: str
    session_name: str
    window_name: str
    server_url: str
    auth_token: Optional[str] = None

    def cleanup(self) -> None:
        """Teardown: POST /exit + DELETE /sessions/<name>."""
        headers: dict[str, str] = {}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
        with contextlib.suppress(Exception):
            requests.post(
                f"{self.server_url}/terminals/{self.terminal_id}/exit",
                headers=headers,
                timeout=5,
            )
        time.sleep(2)
        with contextlib.suppress(Exception):
            requests.delete(
                f"{self.server_url}/sessions/{self.session_name}",
                headers=headers,
                timeout=5,
            )


class TerminalFactory:
    """Creates terminals on a cao-server, with or without auth.

    Usage — no-auth server::

        handle = TerminalFactory.create(server, provider="mock_cli")

    Usage — auth-enabled server (pass the admin token for creation)::

        handle = TerminalFactory.create(server, token=admin_jwt, provider="mock_cli")

    Provider-boot failures are surfaced as ``pytest.skip`` (with a diagnostic
    message) rather than fixture errors, matching the existing contract.

    All created terminals get a unique ``caotest-<hex>`` session name prefix so
    parallel test runs don't collide.
    """

    @staticmethod
    def create(
        server: CaoServer,
        *,
        provider: str = _DEFAULT_PROVIDER,
        agent_profile: str = _DEFAULT_PROFILE,
        token: Optional[str] = None,
        session_prefix: str = "caotest",
    ) -> TerminalHandle:
        """POST /sessions and return a ``TerminalHandle``.

        Args:
            server: The ``CaoServer`` to create the terminal on.
            provider: CLI provider name (default ``mock_cli``).
            agent_profile: Agent profile name (default ``developer``).
            token: If supplied, sent as ``Authorization: Bearer <token>``.
                Required when the server has auth enforcement enabled.
            session_prefix: Prefix for the generated session name (for
                disambiguation in parallel test runs).

        Returns:
            A ``TerminalHandle`` ready for use. Call ``.cleanup()`` on teardown
            or wrap in a pytest fixture.

        Raises:
            pytest.skip.Exception: When the provider can't boot (5xx + known
                marker in the body).
            RuntimeError: For other non-2xx responses.
        """
        session_name = f"{session_prefix}-{uuid.uuid4().hex[:12]}"
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"

        resp = requests.post(
            f"{server.url}/sessions",
            params={
                "provider": provider,
                "agent_profile": agent_profile,
                "session_name": session_name,
            },
            headers=headers,
        )

        if resp.status_code not in (200, 201):
            body = resp.text
            if resp.status_code >= 500:
                body_lower = body.lower()
                if any(m in body_lower for m in _SKIP_MARKERS) or provider.lower() in body_lower:
                    pytest.skip(
                        f"provider {provider!r} not usable on this host "
                        f"(HTTP {resp.status_code}): {body[:200]}"
                    )
            raise RuntimeError(f"POST /sessions failed: {resp.status_code} {resp.text}")

        data = resp.json()
        handle = TerminalHandle(
            terminal_id=data["id"],
            session_name=data["session_name"],
            window_name=data.get("name", "window-0"),
            server_url=server.url,
            auth_token=token,
        )
        TerminalFactory._wait_for_idle(handle, headers)
        return handle

    @staticmethod
    def _wait_for_idle(handle: TerminalHandle, headers: dict[str, str]) -> None:
        """Poll GET /terminals/{id} until status == 'idle' or timeout.

        Prevents races in function-scoped fixtures where the provider CLI
        hasn't finished initializing before the WS test tries to send input.
        """
        deadline = time.monotonic() + _IDLE_POLL_TIMEOUT
        while time.monotonic() < deadline:
            try:
                r = requests.get(
                    f"{handle.server_url}/terminals/{handle.terminal_id}",
                    headers=headers,
                    timeout=5,
                )
                if r.ok and r.json().get("status") == "idle":
                    return
            except Exception:
                pass
            time.sleep(_IDLE_POLL_INTERVAL)


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def cao_terminal_mock(cao_server: CaoServer) -> TerminalHandle:  # type: ignore[return]
    """Function-scoped terminal on ``cao_server`` using the ``mock_cli`` provider.

    Requires: ``cao_server`` session fixture (from ``test.fixtures.cao_server``).
    Provider: ``mock_cli`` (Tier 2 — no external auth needed).

    Cleaner alternative to ``cao_terminal`` when the test only needs the mock
    provider and doesn't need parametrisation.
    """
    handle = TerminalFactory.create(cao_server, provider="mock_cli")
    yield handle
    handle.cleanup()


@pytest.fixture
def cao_terminal_authed(
    cao_server_with_auth: Any,
    jwt_factory: Any,
) -> TerminalHandle:  # type: ignore[return]
    """Function-scoped terminal on ``cao_server_with_auth`` using ``mock_cli``.

    Mints an admin JWT via the session ``jwt_factory`` to satisfy the auth
    gate. The terminal handle carries the same token for cleanup calls.

    Requires:
    - ``cao_server_with_auth`` session fixture (from ``test.fixtures.cao_server``).
    - ``jwt_factory`` session fixture (from ``test.fixtures.jwt_factory``).
    """
    from test.fixtures.cao_server import AuthCaoServer  # noqa: PLC0415

    auth: AuthCaoServer = cao_server_with_auth  # type: ignore[assignment]
    token = jwt_factory.mint(scopes="cao:read cao:write cao:admin")
    handle = TerminalFactory.create(auth.server, token=token, provider="mock_cli")
    yield handle
    handle.cleanup()


__all__ = [
    "TerminalHandle",
    "TerminalFactory",
    "cao_terminal_mock",
    "cao_terminal_authed",
]
