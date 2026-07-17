"""Both AG-UI enablement paths are contractual — pin them.

``_agui_enabled()`` is true under the dedicated ``CAO_AGUI_ENABLED`` flag OR
the pre-existing ``CAO_MCP_APPS_ENABLED`` flag (the surfaces share one event
source and privacy boundary; see the docstring in api/main.py and
docs/agui.md). This interaction is contractual rather than incidental —
these tests pin it.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app

client = TestClient(app, base_url="http://localhost")

_FLAGS = ("CAO_AGUI_ENABLED", "CAO_MCP_APPS_ENABLED")


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch):
    for flag in _FLAGS:
        monkeypatch.delenv(flag, raising=False)


@pytest.fixture()
def _terminating_stream(monkeypatch):
    """Stub the event source so an opened stream completes instead of running
    forever (the live bus never terminates, which would hang the test client)."""

    class _EmptyLog:
        def history(self, **kwargs):
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


@pytest.mark.parametrize("flag", _FLAGS)
@pytest.mark.parametrize("value", ["true", "1", "yes", "TRUE"])
def test_either_flag_enables_the_agui_stream(monkeypatch, flag, value, _terminating_stream):
    monkeypatch.setenv(flag, value)
    with client.stream("GET", "/agui/v1/stream", params={"since": "2999-01-01T00:00:00Z"}) as resp:
        # 200 (auth off) or 401 (auth on) both mean "surface exists"; the
        # default-off contract is that it must NOT be a 404.
        assert resp.status_code != 404


def test_no_flags_means_no_surface():
    assert client.get("/agui/v1/stream").status_code == 404


@pytest.mark.parametrize("value", ["0", "false", "no", "off", ""])
def test_agui_flag_falsey_values_do_not_enable(monkeypatch, value):
    monkeypatch.setenv("CAO_AGUI_ENABLED", value)
    assert client.get("/agui/v1/stream").status_code == 404
