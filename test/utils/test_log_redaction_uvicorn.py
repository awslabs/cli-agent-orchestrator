"""The WebSocket handshake line a REAL uvicorn writes must not carry the token.

The unit tests in ``test_log_redaction.py`` pin the regex and the logger wiring.
This one pins the assumption underneath the wiring -- that uvicorn emits the
``"WebSocket <path>" [accepted]`` line on ``uvicorn.error`` rather than the
access logger -- by driving a real server on a loopback port. It exists because
the first version of the redaction attached the filter to ``uvicorn.access``
only, every mock-level test passed, and the token still reached stderr.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time

import httpx
import pytest
import uvicorn
import websockets
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route, WebSocketRoute

from cli_agent_orchestrator.utils.logging import REDACTED, install_access_log_redaction


async def _ws(websocket):
    await websocket.accept()
    await websocket.close()


async def _http(request):
    return PlainTextResponse("ok")


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list[tuple[str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append((record.name, record.getMessage()))


@pytest.mark.integration
def test_real_uvicorn_redacts_the_websocket_handshake_and_the_http_request():
    app = Starlette(routes=[WebSocketRoute("/ws", _ws), Route("/http", _http)])
    capture = _Capture()
    loggers = [logging.getLogger(n) for n in ("uvicorn", "uvicorn.error", "uvicorn.access")]
    saved = [(lg.level, lg.propagate, list(lg.handlers)) for lg in loggers]
    for lg in loggers:
        lg.setLevel(logging.INFO)
        lg.propagate = False
        lg.handlers = [capture]
    install_access_log_redaction()

    # ``log_config=None`` keeps uvicorn from replacing the handlers above.
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, log_config=None))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        port = server.servers[0].sockets[0].getsockname()[1]

        async def _drive() -> None:
            async with websockets.connect(f"ws://127.0.0.1:{port}/ws?token=SECRET.WS.JWT"):
                pass
            async with httpx.AsyncClient() as client:
                await client.get(f"http://127.0.0.1:{port}/http?access_token=SECRET.HTTP.JWT")

        asyncio.run(_drive())
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and not any("WebSocket" in m for _, m in capture.lines):
            time.sleep(0.02)
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        for lg, (level, propagate, handlers) in zip(loggers, saved):
            lg.setLevel(level)
            lg.propagate = propagate
            lg.handlers = handlers

    ws_lines = [(n, m) for n, m in capture.lines if "WebSocket" in m]
    http_lines = [(n, m) for n, m in capture.lines if "GET /http" in m]
    assert ws_lines, capture.lines
    assert http_lines, capture.lines
    joined = "\n".join(m for _, m in capture.lines)
    assert "SECRET" not in joined, joined
    assert any(f"token={REDACTED}" in m for _, m in ws_lines)
    assert any(f"access_token={REDACTED}" in m for _, m in http_lines)
