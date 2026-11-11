"""Routes that call the terminal backend do it off the event loop (#745).

Under the remote backend a backend call is a round trip to a runner, bounded
only by the runner call timeout (30 s by default, after a 10 s send timeout).
Made on the event-loop thread, it would stop every other request for that long,
``/health`` included, and a readiness probe would mark the server not ready.

Each case replaces the route's backend call with a blocking sleep and checks
that ``/health``, sent while the route waits, is answered at once.
"""

import asyncio
import time
from unittest.mock import patch

import httpx
import pytest

from cli_agent_orchestrator.api import main as api_main
from cli_agent_orchestrator.services import session_service

BLOCK = 1.5  # how long the backend call blocks
PROMPT = 0.7  # from the slow request, /health must answer well inside BLOCK


def _blocking(result):
    def call(*_args, **_kwargs):
        time.sleep(BLOCK)
        return result

    return call


CASES = {
    "working-directory": (
        "/terminals/aaaaaaaa/working-directory",
        patch.object(api_main.terminal_service, "get_working_directory", _blocking("/w")),
    ),
    "sessions": (
        "/sessions",
        patch.object(api_main.session_service, "list_sessions", _blocking([])),
    ),
    "session-terminals": (
        "/sessions/cao-s/terminals",
        patch.object(session_service, "list_current_session_terminals", _blocking([])),
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_health_answers_while_a_backend_call_waits(case):
    path, blocking = CASES[case]

    async def run():
        transport = httpx.ASGITransport(app=api_main.app)
        async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
            started = time.monotonic()
            slow = asyncio.create_task(client.get(path))
            await asyncio.sleep(0.1)
            # Timed from when the slow request was sent: a blocked loop delays
            # this coroutine itself, not only the /health handler.
            health = await client.get("/health")
            health_after = time.monotonic() - started
            slow_response = await slow
        return health, health_after, slow_response

    with blocking:
        health, health_after, slow_response = asyncio.run(run())

    assert health.status_code == 200
    assert slow_response.status_code == 200, slow_response.text
    assert health_after < PROMPT, f"/health waited {health_after:.2f}s behind {path}"
