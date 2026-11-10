"""Delegation edge hooks in terminal_service.

Arming on confirmed delivery, closing edges on a deferred-init failure notice,
and notifying waiting callers when a worker is deleted.
"""

import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    create_delegation_edge,
    create_terminal,
    get_inbox_messages,
    list_armed_worker_ids,
    list_open_edges_into,
    list_open_edges_out_of,
    mark_assign_edges_delivered,
)
from cli_agent_orchestrator.models.delegation import EdgeSource
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services import terminal_service

SVC = "cli_agent_orchestrator.services.delegation_callback_service"
WORKER = "1e293d67"
CALLER = "a1b2c3d4"
TASK = "do the thing"


@pytest.fixture
def edge_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(bind=engine)
    with patch("cli_agent_orchestrator.clients.database.SessionLocal", sessionmaker(bind=engine)):
        yield


class TestArmingOnConfirmedDelivery:
    @pytest.fixture
    def delivered(self):
        provider = MagicMock()
        provider.initialize = AsyncMock(return_value=True)
        provider.shell_baseline = None
        provider.runtime_variant = None
        provider.requires_execution_evidence = False
        with (
            patch.object(
                terminal_service, "get_terminal_metadata", return_value={"caller_id": CALLER}
            ),
            patch.object(terminal_service, "dispatch_input", return_value=None),
            patch.object(terminal_service, "_persist_initial_delivery", MagicMock()),
            patch.object(terminal_service, "_clear_deferred_init_external_owner", AsyncMock()),
            patch.object(
                terminal_service, "_surface_deferred_init_failure", AsyncMock()
            ) as surface,
            patch.object(terminal_service, "mark_assign_edges_delivered") as mark,
            patch.object(terminal_service, "get_backend") as backend,
            patch.object(terminal_service, "status_monitor"),
        ):
            backend.return_value.supports_event_inbox.return_value = False
            yield MagicMock(provider=provider, mark=mark, surface=surface)

    @pytest.mark.asyncio
    async def test_confirmed_delivery_arms_assign_edges(self, delivered):
        with patch.object(
            terminal_service, "_confirm_worker_started_or_resubmit", AsyncMock(return_value=True)
        ):
            await terminal_service._schedule_deferred_init(
                delivered.provider, WORKER, TASK, None, None, provider="claude_code"
            )

        delivered.mark.assert_called_once_with(WORKER)
        delivered.surface.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_unstarted_delivery_does_not_arm_edges(self, delivered):
        with patch.object(
            terminal_service, "_confirm_worker_started_or_resubmit", AsyncMock(return_value=False)
        ):
            await terminal_service._schedule_deferred_init(
                delivered.provider, WORKER, TASK, None, None, provider="claude_code"
            )

        delivered.mark.assert_not_called()
        delivered.surface.assert_awaited_once()


class TestDeferredFailureClosesEdges:
    def test_failure_notice_closes_edges_without_a_second_notice(self, edge_db):
        create_delegation_edge(CALLER, WORKER, EdgeSource.ASSIGN)
        with (
            patch.object(
                terminal_service,
                "get_terminal_metadata",
                return_value={"caller_id": CALLER, "tmux_session": "cao-s"},
            ),
            patch.object(terminal_service, "create_inbox_message") as create_msg,
            patch.object(terminal_service, "ephemeral_service"),
        ):
            terminal_service._notify_caller_of_deferred_failure(
                WORKER, "failed to start", None, delete_worker=False
            )

        create_msg.assert_called_once()
        assert list_open_edges_into(WORKER) == []


class TestDeleteNotifiesWaitingCaller:
    def test_delete_notifies_caller_before_teardown(self):
        with (
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_worker_ended"
            ) as ended,
            patch.object(terminal_service, "capture_terminal_snapshot", return_value={}),
            patch.object(
                terminal_service, "dismantle_terminal_runtime", return_value=False
            ) as dismantle,
        ):
            result = terminal_service.delete_terminal(WORKER, end_reason="deleted")

        assert result is False  # teardown deferred; the notice still went out
        ended.assert_called_once_with(WORKER, "was deleted", None)
        dismantle.assert_called_once()

    def test_notice_failure_does_not_block_deletion(self):
        with (
            patch(
                "cli_agent_orchestrator.services.delegation_callback_service.handle_worker_ended",
                side_effect=RuntimeError("database unavailable"),
            ),
            patch.object(terminal_service, "capture_terminal_snapshot", return_value={}),
            patch.object(
                terminal_service, "dismantle_terminal_runtime", return_value=False
            ) as dismantle,
        ):
            terminal_service.delete_terminal(WORKER, end_reason="deleted")

        dismantle.assert_called_once()


class TestRowDeletionClosesEdges:
    """Session teardown, retention cleanup and the stale-row purge delete rows
    without ``delete_terminal``; the row deletion itself closes the edges."""

    @pytest.fixture
    def row_deletion(self, edge_db):
        with (
            patch.object(terminal_service, "db_delete_terminal", return_value=True),
            patch.object(terminal_service, "_delete_deferred_failure_fallback"),
            patch.object(terminal_service, "_delete_deferred_init_complete_fallback"),
            patch.object(terminal_service, "ephemeral_service"),
            patch.object(terminal_service, "create_inbox_message") as create_msg,
        ):
            yield create_msg

    def test_row_deletion_closes_every_open_edge_without_a_notice(self, row_deletion):
        create_delegation_edge(CALLER, WORKER, EdgeSource.ASSIGN)
        mark_assign_edges_delivered(WORKER)
        create_delegation_edge(WORKER, "c0ffee00", EdgeSource.ASSIGN)
        assert list_armed_worker_ids() == [WORKER]

        terminal_service.delete_terminal_row(WORKER, None, end_reason="session_teardown")

        assert list_open_edges_into(WORKER) == []
        assert list_open_edges_out_of(WORKER) == []
        assert list_armed_worker_ids() == []
        row_deletion.assert_not_called()
        assert get_inbox_messages(CALLER) == []

    def test_edge_failure_does_not_block_row_deletion(self, row_deletion):
        with patch.object(
            terminal_service,
            "close_open_edges_for_terminal",
            side_effect=RuntimeError("database unavailable"),
        ):
            assert terminal_service.delete_terminal_row(WORKER, None, end_reason="deleted")

    def test_delete_terminal_still_notifies_before_the_row_goes(self, row_deletion):
        create_terminal(CALLER, "cao-s", "sup", "claude_code")
        create_delegation_edge(CALLER, WORKER, EdgeSource.ASSIGN)
        with (
            patch(f"{SVC}.terminal_service") as worker_pane,
            patch(f"{SVC}.inbox_service"),
            patch.object(terminal_service, "capture_terminal_snapshot", return_value={}),
            patch.object(terminal_service, "dismantle_terminal_runtime", return_value=True),
        ):
            worker_pane.get_output.return_value = "half done"
            terminal_service.delete_terminal(WORKER, end_reason="deleted")

        (notice,) = get_inbox_messages(CALLER)
        assert notice.message.startswith(f"[CAO worker ended] Worker {WORKER}")
        assert notice.message.endswith("---\nhalf done")
        assert list_open_edges_into(WORKER) == []


class TestDeleteDoesNotWaitForPickup:
    def test_delete_returns_before_the_caller_confirms_pickup(self, isolated_memory_db):
        """An idle caller that never starts on the notice must not hold the delete.

        Uses the file-backed test database: the notice is delivered on another
        thread, which an in-memory SQLite database would not be shared with.
        """
        from cli_agent_orchestrator.services.inbox_service import InboxService

        create_terminal(CALLER, "cao-s", "sup", "claude_code")
        create_delegation_edge(CALLER, WORKER, EdgeSource.ASSIGN)
        confirming, release, confirmed = threading.Event(), threading.Event(), threading.Event()

        def confirm(*_args, **_kwargs):
            confirming.set()
            release.wait(3)  # an idle caller that does not start
            confirmed.set()
            return False

        service = InboxService()
        with (
            patch(f"{SVC}.inbox_service", service),
            patch(f"{SVC}.terminal_service") as worker_pane,
            patch("cli_agent_orchestrator.services.inbox_service.INBOX_SUBMIT_CONFIRMATION", True),
            patch("cli_agent_orchestrator.services.inbox_service.status_monitor") as monitor,
            patch("cli_agent_orchestrator.services.inbox_service.terminal_service"),
            patch("cli_agent_orchestrator.services.inbox_service.get_backend") as backend,
            patch.object(InboxService, "_confirm_submitted", side_effect=confirm),
            patch.object(terminal_service, "capture_terminal_snapshot", return_value={}),
            patch.object(terminal_service, "dismantle_terminal_runtime", return_value=True),
            patch.object(terminal_service, "db_delete_terminal", return_value=True),
            patch.object(terminal_service, "ephemeral_service"),
        ):
            worker_pane.get_output.return_value = "half done"
            monitor.get_status.return_value = TerminalStatus.IDLE
            backend.return_value.supports_event_inbox.return_value = False
            try:
                assert terminal_service.delete_terminal(WORKER, end_reason="deleted") is True
                assert confirming.wait(5) is True  # the notice was pasted
                assert confirmed.is_set() is False  # and the delete did not wait for pickup
            finally:
                release.set()

        (notice,) = get_inbox_messages(CALLER)
        assert notice.message.startswith(f"[CAO worker ended] Worker {WORKER}")


def test_delete_passes_its_plugin_registry_to_the_notice():
    registry = MagicMock()
    with (
        patch(
            "cli_agent_orchestrator.services.delegation_callback_service.handle_worker_ended"
        ) as ended,
        patch.object(terminal_service, "capture_terminal_snapshot", return_value={}),
        patch.object(terminal_service, "dismantle_terminal_runtime", return_value=False),
    ):
        terminal_service.delete_terminal(WORKER, registry=registry, end_reason="deleted")

    ended.assert_called_once_with(WORKER, "was deleted", registry)
