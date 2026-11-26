"""``assign`` publishes its caller's wait: the edge it opens refreshes ``cao.waiting``.

Design D38. The refresh itself is tested in ``test/services/test_delegation_waiting_labels.py``;
here only the wiring in ``POST /sessions/{s}/terminals`` is checked, with the same fixtures as
``test_delegation_edges_api.py``.
"""

import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

from cli_agent_orchestrator.backends import registry
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services import delegation_callback_service

MAIN = "cli_agent_orchestrator.api.main"
CALLER = "a1b2c3d4"
WORKER = "aa11bb22"


def _terminal() -> Terminal:
    return Terminal(id=WORKER, name="developer", provider="claude_code", session_name="cao-s")


def _post(client, **extra):
    return client.post(
        "/sessions/cao-s/terminals",
        params={
            "agent_profile": "developer",
            "provider": "claude_code",
            "caller_id": CALLER,
            **extra,
        },
    )


def test_assign_with_a_callback_refreshes_the_callers_label(client):
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=_terminal())),
        patch(f"{MAIN}.create_delegation_edge"),
        patch(f"{MAIN}.find_any_edge", return_value=None),
        patch(f"{MAIN}.delegation_callback_service.refresh_waiting_labels") as refresh,
    ):
        _post(client, expect_callback="true")

    refresh.assert_called_once_with([CALLER])


def test_assign_without_a_callback_refreshes_nothing(client):
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=_terminal())),
        patch(f"{MAIN}.create_delegation_edge"),
        patch(f"{MAIN}.find_any_edge", return_value=None),
        patch(f"{MAIN}.delegation_callback_service.refresh_waiting_labels") as refresh,
    ):
        _post(client)

    refresh.assert_not_called()


def test_a_replayed_assign_opens_no_edge_and_refreshes_nothing(client):
    with (
        patch(f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=_terminal())),
        patch(f"{MAIN}.create_delegation_edge"),
        patch(f"{MAIN}.find_any_edge", return_value=MagicMock()),
        patch(f"{MAIN}.delegation_callback_service.refresh_waiting_labels") as refresh,
    ):
        _post(client, expect_callback="true")

    refresh.assert_not_called()


def test_assign_does_not_wait_for_a_hung_label_write(client):
    """A herdr-core that never answers must not hold the assign response (review F1)."""
    entered, release = threading.Event(), threading.Event()

    class HungBackend:
        def set_waiting_counts(self, counts, *, complete=False):
            entered.set()
            release.wait(10.0)

    registry.set_backend(HungBackend())  # type: ignore[arg-type]
    try:
        with (
            patch(
                f"{MAIN}.terminal_service.create_terminal", new=AsyncMock(return_value=_terminal())
            ),
            patch(f"{MAIN}.create_delegation_edge"),
            patch(f"{MAIN}.find_any_edge", return_value=None),
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.list_open_edges_out_of",
                return_value=[],
            ),
        ):
            started = time.perf_counter()
            response = _post(client, expect_callback="true")
            elapsed = time.perf_counter() - started

        assert response.status_code < 400
        assert elapsed < 2.0, f"assign waited {elapsed:.1f}s for the label write"
        assert entered.wait(5.0), "the label write never started"
    finally:
        release.set()
        delegation_callback_service.flush_waiting_labels()
