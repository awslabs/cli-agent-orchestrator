"""Delegation edge hooks in terminal_service.

Arming on confirmed delivery, closing edges on a deferred-init failure notice,
and notifying waiting callers when a worker is deleted.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from cli_agent_orchestrator.clients.database import (
    Base,
    create_delegation_edge,
    list_open_edges_into,
)
from cli_agent_orchestrator.models.delegation import EdgeSource
from cli_agent_orchestrator.services import terminal_service

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
        ended.assert_called_once_with(WORKER, "was deleted")
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
