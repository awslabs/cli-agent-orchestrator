"""Token-parse failures must map to clean auth errors, not 500s.

``extract_scopes_from_token`` raises ``jwt.PyJWTError`` subclasses (or JWKS
fetch errors) on invalid/expired/malformed tokens, but the SSE path caught
nothing — so a bad token surfaced as an opaque 500 instead of 401. It failed
closed either way; these tests pin the *clean* failure mode.

Two layers:

* the malformed-token cases run the REAL ``extract_scopes_from_token``
  (a token without three segments fails header parsing before any JWKS
  network I/O, so the test is deterministic and offline), and
* the expired-token case simulates the post-JWKS validation failure by
  raising ``jwt.ExpiredSignatureError`` from the extractor, pinning that ANY
  parse exception maps to 401 (full signature+expiry validation against a
  live JWKS is exercised by the end-to-end auth suite).
"""

from __future__ import annotations

from typing import Any, Dict, List

import jwt
import pytest
from fastapi.testclient import TestClient

import cli_agent_orchestrator.api.main as main
from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")


@pytest.fixture(autouse=True)
def _agui_on_auth_on(monkeypatch):
    monkeypatch.setenv("CAO_AGUI_ENABLED", "true")
    # Enable auth at the source module so the *real* token-validation path
    # runs (main.py re-imports the symbols, so patch both references).
    monkeypatch.setenv("AUTH0_DOMAIN", "unit-test-tenant.invalid")


class TestStreamEndpoint:
    def test_malformed_token_returns_401_not_500(self):
        resp = client.get("/agui/v1/stream", params={"access_token": "not-a-jwt"})
        assert resp.status_code == 401
        assert "invalid" in resp.text.lower() or "expired" in resp.text.lower()

    def test_expired_token_returns_401_not_500(self, monkeypatch):
        def _raise(_tok: str) -> List[str]:
            raise jwt.ExpiredSignatureError("Signature has expired")

        monkeypatch.setattr(main, "extract_scopes_from_token", _raise)
        resp = client.get("/agui/v1/stream", params={"access_token": "e.x.p"})
        assert resp.status_code == 401

    def test_valid_scopes_still_pass_through(self, monkeypatch):
        # Guard against over-catching: a working extractor with read scope
        # must not be converted into a 401 by the new exception handling.
        monkeypatch.setattr(main, "extract_scopes_from_token", lambda tok: ["cao:read"])

        class _EmptyLog:
            def history(self, **kwargs: Any) -> List[Dict]:
                return []

        class _EmptyBus:
            def register(self, overflow_close=False):
                return object()

            def unregister(self, queue):
                pass

            async def drain(self, queue):
                return
                yield  # pragma: no cover

        monkeypatch.setattr(
            "cli_agent_orchestrator.services.event_log_service.get_event_log",
            lambda: _EmptyLog(),
        )
        monkeypatch.setattr(
            "cli_agent_orchestrator.services.sse_bus.get_bus",
            lambda: _EmptyBus(),
        )
        with client.stream("GET", "/agui/v1/stream", params={"access_token": "ok"}) as resp:
            assert resp.status_code == 200
