"""Coverage for the emit_ui generative-UI producer MCP tool.

Targets the response-handling branches in ``mcp_server/server.emit_ui``:
success (returns the endpoint JSON), 400 (invalid intent → ValueError), and
404 (AG-UI surface disabled → graceful degrade instead of raising).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.mcp_server.server import emit_ui

# The tool is imported as a plain coroutine function.
_emit = getattr(emit_ui, "fn", emit_ui)


def _resp(status: int, payload: dict) -> MagicMock:
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r


@pytest.mark.asyncio
async def test_emit_ui_success_returns_endpoint_json() -> None:
    with patch(
        "cli_agent_orchestrator.mcp_server.server.requests.post",
        return_value=_resp(200, {"ok": True, "event_id": "e1", "component": "metric"}),
    ):
        out = await _emit(component="metric", props={"label": "tok/s", "value": 42})
    assert out["ok"] is True
    assert out["event_id"] == "e1"


@pytest.mark.asyncio
async def test_emit_ui_400_raises_value_error() -> None:
    resp = _resp(400, {"detail": "off-list component"})
    with patch("cli_agent_orchestrator.mcp_server.server.requests.post", return_value=resp):
        with pytest.raises(ValueError):
            await _emit(component="iframe", props={})


@pytest.mark.asyncio
async def test_emit_ui_404_degrades_gracefully() -> None:
    with patch(
        "cli_agent_orchestrator.mcp_server.server.requests.post",
        return_value=_resp(404, {}),
    ):
        out = await _emit(component="approval_card", props=None)
    assert out["ok"] is False
    assert "CAO_AGUI_ENABLED" in out["reason"]
