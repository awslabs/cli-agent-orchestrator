"""Tests for the new inbox messages GET endpoint."""

import asyncio
import re
from datetime import datetime
from unittest.mock import Mock, patch

import pytest

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.models.inbox import InboxMessage, MessageStatus


@pytest.fixture
def sample_inbox_messages():
    """Create sample inbox messages for testing."""
    return [
        InboxMessage(
            id=1,
            sender_id="sender1",
            receiver_id="abcdef12",
            message="Hello world",
            status=MessageStatus.PENDING,
            created_at=datetime(2025, 12, 6, 12, 0, 0),
        ),
        InboxMessage(
            id=2,
            sender_id="sender2",
            receiver_id="abcdef12",
            message="Another message",
            status=MessageStatus.DELIVERED,
            created_at=datetime(2025, 12, 6, 12, 5, 0),
        ),
        InboxMessage(
            id=3,
            sender_id="sender3",
            receiver_id="abcdef12",
            message="Failed message",
            status=MessageStatus.FAILED,
            created_at=datetime(2025, 12, 6, 12, 10, 0),
        ),
    ]


class TestGetInboxMessagesEndpoint:
    """Test cases for GET /terminals/{terminal_id}/inbox/messages endpoint."""

    def test_get_all_messages_success(self, client, sample_inbox_messages):
        """Test getting all messages without status filter."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages

            response = client.get("/terminals/abcdef12/inbox/messages")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 3

            # Check response format
            for msg_data in data:
                assert "id" in msg_data
                assert "sender_id" in msg_data
                assert "receiver_id" in msg_data
                assert "message" in msg_data
                assert "status" in msg_data
                assert "created_at" in msg_data

    def test_get_messages_with_status_filter(self, client, sample_inbox_messages):
        """Test getting messages with status filter."""
        pending_messages = [
            msg for msg in sample_inbox_messages if msg.status == MessageStatus.PENDING
        ]

        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = pending_messages

            response = client.get("/terminals/abcdef12/inbox/messages?status=pending")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["status"] == "pending"
            assert data[0]["sender_id"] == "sender1"

    def test_get_messages_with_limit(self, client, sample_inbox_messages):
        """Test getting messages with limit parameter."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages[:2]

            response = client.get("/terminals/abcdef12/inbox/messages?limit=2")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 2
            mock_get.assert_called_once_with(
                "abcdef12", limit=2, status=None, sender_id=None, newest_first=False
            )

    def test_get_messages_with_status_and_limit(self, client, sample_inbox_messages):
        """Test getting messages with both status and limit parameters."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages[:1]

            response = client.get("/terminals/abcdef12/inbox/messages?status=pending&limit=5")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            mock_get.assert_called_once_with(
                "abcdef12",
                limit=5,
                status=MessageStatus.PENDING,
                sender_id=None,
                newest_first=False,
            )

    def test_default_order_is_oldest_first(self, client, sample_inbox_messages):
        """Omitting order keeps the historical oldest-first query."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages

            response = client.get("/terminals/abcdef12/inbox/messages")

            assert response.status_code == 200
            mock_get.assert_called_once_with(
                "abcdef12", limit=10, status=None, sender_id=None, newest_first=False
            )

    def test_order_desc_requests_newest_first(self, client, sample_inbox_messages):
        """order=desc asks the database for newest-first, then applies limit."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = list(reversed(sample_inbox_messages))[:1]

            response = client.get("/terminals/abcdef12/inbox/messages?order=desc&limit=1")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["id"] == 3
            mock_get.assert_called_once_with(
                "abcdef12", limit=1, status=None, sender_id=None, newest_first=True
            )

    def test_order_asc_is_explicit_oldest_first(self, client, sample_inbox_messages):
        """order=asc is the same oldest-first query as omitting order."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages

            response = client.get("/terminals/abcdef12/inbox/messages?order=asc")

            assert response.status_code == 200
            mock_get.assert_called_once_with(
                "abcdef12", limit=10, status=None, sender_id=None, newest_first=False
            )

    def test_sender_id_filter_is_forwarded(self, client, sample_inbox_messages):
        """sender_id is passed through and only that sender's messages are returned."""
        matching = [msg for msg in sample_inbox_messages if msg.sender_id == "sender1"]

        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = matching

            response = client.get("/terminals/abcdef12/inbox/messages?sender_id=sender1")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            assert data[0]["sender_id"] == "sender1"
            mock_get.assert_called_once_with(
                "abcdef12", limit=10, status=None, sender_id="sender1", newest_first=False
            )

    def test_sender_id_combines_with_status_order_and_limit(self, client, sample_inbox_messages):
        """sender_id combines with status, order, and limit."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages[:1]

            response = client.get(
                "/terminals/abcdef12/inbox/messages"
                "?status=pending&order=desc&limit=1&sender_id=sender1"
            )

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1
            mock_get.assert_called_once_with(
                "abcdef12",
                limit=1,
                status=MessageStatus.PENDING,
                sender_id="sender1",
                newest_first=True,
            )

    def test_invalid_order_parameter(self, client):
        """An invalid order value returns 400, the same way an invalid status does."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            response = client.get("/terminals/abcdef12/inbox/messages?order=newest")

            assert response.status_code == 400
            data = response.json()
            assert "detail" in data
            assert "Invalid order" in data["detail"]
            assert "asc, desc" in data["detail"]
            mock_get.assert_not_called()

    def test_invalid_status_parameter(self, client):
        """Test error handling for invalid status parameter."""
        response = client.get("/terminals/abcdef12/inbox/messages?status=invalid_status")

        assert response.status_code == 400
        data = response.json()
        assert "detail" in data
        assert "Invalid status" in data["detail"]
        assert "pending, delivered, failed" in data["detail"]

    def test_limit_exceeds_maximum(self, client):
        """Test that limit parameter is properly validated."""
        # FastAPI Query with le=100 should handle this automatically
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = []

            response = client.get("/terminals/abcdef12/inbox/messages?limit=150")

            # FastAPI should return 422 for query parameter validation error
            assert response.status_code == 422

    def test_database_error_handling(self, client):
        """Test error handling for database errors."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.side_effect = Exception("Database connection failed")

            response = client.get("/terminals/abcdef12/inbox/messages")

            assert response.status_code == 500
            data = response.json()
            assert "detail" in data
            assert "Failed to retrieve inbox messages" in data["detail"]

    def test_terminal_not_found_error(self, client):
        """Test error handling when terminal is not found."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.side_effect = ValueError("Terminal not found")

            response = client.get("/terminals/deadbeef/inbox/messages")

            assert response.status_code == 404
            data = response.json()
            assert "detail" in data
            assert "Terminal not found" in data["detail"]

    def test_empty_message_list(self, client):
        """Test getting messages when no messages exist."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = []

            response = client.get("/terminals/abcdef12/inbox/messages")

            assert response.status_code == 200
            data = response.json()
            assert isinstance(data, list)
            assert len(data) == 0

    def test_message_datetime_formatting(self, client, sample_inbox_messages):
        """Test that datetime is properly formatted in response."""
        with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
            mock_get.return_value = sample_inbox_messages[:1]

            response = client.get("/terminals/abcdef12/inbox/messages")

            assert response.status_code == 200
            data = response.json()
            assert len(data) == 1

            # Check ISO format datetime
            created_at = data[0]["created_at"]
            assert isinstance(created_at, str)
            # Should be able to parse as ISO datetime
            parsed_datetime = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            assert isinstance(parsed_datetime, datetime)

    def test_all_status_values(self, client, sample_inbox_messages):
        """Test filtering by each possible status value."""
        for status_value in ["pending", "delivered", "failed"]:
            filtered_messages = [
                msg for msg in sample_inbox_messages if msg.status.value == status_value
            ]

            with patch("cli_agent_orchestrator.api.main.get_inbox_messages") as mock_get:
                mock_get.return_value = filtered_messages

                response = client.get(f"/terminals/abcdef12/inbox/messages?status={status_value}")

                assert response.status_code == 200
                data = response.json()
                assert len(data) == len(filtered_messages)

                for msg_data in data:
                    assert msg_data["status"] == status_value


class TestDatabaseFunctionCompatibility:
    """Test compatibility with enhanced database functions."""

    def test_get_inbox_messages_function_exists(self):
        """Test that the new get_inbox_messages function is properly imported."""
        from cli_agent_orchestrator.clients.database import get_inbox_messages

        assert callable(get_inbox_messages)

    def test_get_pending_messages_backward_compatibility(self):
        """Test that get_pending_messages still works as before."""
        from cli_agent_orchestrator.clients.database import get_inbox_messages, get_pending_messages

        # Both functions should be callable
        assert callable(get_pending_messages)
        assert callable(get_inbox_messages)

    @patch("cli_agent_orchestrator.clients.database.SessionLocal")
    def test_get_pending_messages_calls_get_inbox_messages(self, mock_session):
        """Test that get_pending_messages properly calls get_inbox_messages with correct parameters."""
        from cli_agent_orchestrator.clients.database import get_pending_messages

        # Mock the database session and query
        mock_db = Mock()
        mock_session.return_value.__enter__.return_value = mock_db
        mock_query = Mock()
        mock_db.query.return_value = mock_query
        mock_query.filter.return_value = mock_query
        mock_query.order_by.return_value = mock_query
        mock_query.limit.return_value = mock_query
        mock_query.all.return_value = []

        # This should work without errors
        result = get_pending_messages("test_terminal", limit=5)
        assert isinstance(result, list)


_UNCERTAIN_PREFIX = "May have been delivered:"
_CHECK_BEFORE_RESENDING = re.compile(
    r"check the receiver'?s terminal before (?:you )?send(?:ing)? it again\.?$", re.IGNORECASE
)


def _assert_uncertain_error(error, cause):
    """Owner request: an uncertain error leads with the warning, then the cause, then the advice."""
    assert error.startswith(_UNCERTAIN_PREFIX), error
    assert cause in error, error
    advice = _CHECK_BEFORE_RESENDING.search(error)
    assert advice is not None, error
    assert len(_UNCERTAIN_PREFIX) <= error.index(cause) < advice.start(), "warning, cause, advice"


class TestCreateInboxMessageResponseTiming:
    """PR #1004 review, P2-2: the POST must not wait out a submit confirmation.

    The message is stored before the immediate delivery attempt starts, so the
    response is already truthful once the paste has reached the pane. Holding
    the response for the confirmation (up to a full confirmation cycle, longer
    when the receiver's lock is already held) pushes the sender past its own
    30s HTTP timeout and tempts it to send the message again.

    No stopwatch anywhere (CI round 2, the #1020 lesson): the fake delivery
    blocks on an Event only the test sets, so "the response arrived while that
    Event is still unset" proves the handler did not wait for the delivery to
    finish, however slow the runner. The endpoint coroutine is called directly
    rather than through ``TestClient``: its per-request portal closes the event
    loop when the handler returns, and that shutdown waits for the default
    executor's threads, so through the client a response can never be observed
    while the delivery thread is still running. The server's loop lives for the
    whole process.
    """

    @staticmethod
    def _stored_message():
        return InboxMessage(
            id=7,
            sender_id="sender01",
            receiver_id="abcdef12",
            message="hello",
            status=MessageStatus.PENDING,
            created_at=datetime(2025, 12, 6, 12, 0, 0),
        )

    @staticmethod
    def _request():
        from starlette.requests import Request

        from cli_agent_orchestrator.plugins import PluginRegistry

        app.state.plugin_registry = PluginRegistry()
        return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "app": app})

    @staticmethod
    async def _drain_background_tasks():
        """Await every task the endpoint left running (its delivery task)."""
        others = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        await asyncio.gather(*others, return_exceptions=True)

    async def _post(self, deliver, **patches):
        """POST message 7 with ``deliver_pending`` replaced by ``deliver``; return the body."""
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=deliver,
            ) as mock_deliver,
            patch("cli_agent_orchestrator.api.main.delegation_callback_service.on_send_message"),
        ):
            body = await create_inbox_message_endpoint(
                self._request(), "abcdef12", "sender01", "hello", _scopes=[]
            )
            await self._drain_background_tasks()
        self.deliver_call = mock_deliver.call_args
        return body

    @staticmethod
    def _outcome():
        from cli_agent_orchestrator.models.inbox import DeliveryOutcome

        return DeliveryOutcome

    @pytest.mark.asyncio
    async def test_responds_delivered_while_confirmation_is_still_running(self):
        """Spec scenario "Immediate delivery succeeds"; the confirmation is not waited for."""
        import threading

        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        release = threading.Event()  # the confirmation; only the test ends it
        finished = threading.Event()
        delivered = self._outcome().DELIVERED

        def deliver(receiver_id, **kwargs):
            try:
                kwargs["on_outcome"](kwargs["report_to_sender"], delivered, None)
                release.wait(10)  # deadlock guard only
            finally:
                finished.set()

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=deliver,
            ),
        ):
            try:
                body = await create_inbox_message_endpoint(
                    self._request(), "abcdef12", "sender01", "hello", _scopes=[]
                )
                assert (body["success"], body["delivery"], body["error"]) == (
                    True,
                    "delivered",
                    None,
                )
                assert not finished.is_set(), "answered before the confirmation ended"
            finally:
                release.set()
            await self._drain_background_tasks()
        assert finished.is_set()

    @pytest.mark.asyncio
    async def test_delivery_is_asked_to_report_this_message(self):
        def deliver(receiver_id, **kwargs):
            return None

        await self._post(deliver)
        assert self.deliver_call.args[0] == "abcdef12"
        assert self.deliver_call.kwargs["report_to_sender"] == 7
        assert callable(self.deliver_call.kwargs["on_outcome"])

    @pytest.mark.asyncio
    async def test_failed_before_typing_returns_the_error(self):
        """Spec scenario "Immediate delivery fails before typing"."""
        failed = self._outcome().FAILED

        def deliver(receiver_id, **kwargs):
            kwargs["on_outcome"](7, failed, "InputNotSentError: lost terminal.read")

        body = await self._post(deliver)
        assert (body["success"], body["delivery"]) == (False, "failed")
        assert body["error"] == "InputNotSentError: lost terminal.read"
        assert body["message_id"] == 7

    @pytest.mark.asyncio
    async def test_uncertain_returns_the_error(self):
        uncertain = self._outcome().UNCERTAIN

        def deliver(receiver_id, **kwargs):
            kwargs["on_outcome"](7, uncertain, "lost terminal.send")

        body = await self._post(deliver)
        assert (body["success"], body["delivery"]) == (False, "uncertain")
        _assert_uncertain_error(body["error"], "lost terminal.send")

    @pytest.mark.asyncio
    async def test_receiver_not_ready_is_queued(self):
        """Spec scenario "Receiver busy": the attempt leaves message 7 alone."""

        def deliver(receiver_id, **kwargs):
            return None

        body = await self._post(deliver)
        assert (body["success"], body["delivery"], body["error"]) == (True, "queued", None)

    @pytest.mark.asyncio
    async def test_older_message_delivered_is_queued_for_this_one(self):
        """Spec scenario "Older messages ahead"."""
        delivered = self._outcome().DELIVERED

        def deliver(receiver_id, **kwargs):
            kwargs["on_outcome"](6, delivered, None)

        body = await self._post(deliver)
        assert (body["success"], body["delivery"]) == (True, "queued")

    @pytest.mark.asyncio
    async def test_first_outcome_for_this_message_decides(self):
        """A failure after the paste landed (plan note 7) does not turn delivered into failed."""
        outcome = self._outcome()

        def deliver(receiver_id, **kwargs):
            kwargs["on_outcome"](7, outcome.DELIVERED, None)
            kwargs["on_outcome"](7, outcome.UNCERTAIN, "confirmation broke")

        body = await self._post(deliver)
        assert (body["success"], body["delivery"], body["error"]) == (True, "delivered", None)

    @pytest.mark.asyncio
    async def test_bound_reached_is_in_progress(self):
        import threading

        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        release = threading.Event()
        reported = threading.Event()
        failed = self._outcome().FAILED

        def deliver(receiver_id, **kwargs):
            release.wait(10)  # blocked on the receiver's lock; guard only
            kwargs["on_outcome"](7, failed, "too late to matter")
            reported.set()

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=deliver,
            ),
            patch("cli_agent_orchestrator.api.main.INBOX_DELIVERY_OUTCOME_WAIT", 0.05),
        ):
            try:
                body = await create_inbox_message_endpoint(
                    self._request(),
                    "abcdef12",
                    "sender01",
                    "hello",
                    wait_for_outcome=True,
                    _scopes=[],
                )
                assert (body["success"], body["delivery"], body["error"]) == (
                    True,
                    "in_progress",
                    None,
                )
                assert not reported.is_set()
            finally:
                release.set()
            await self._drain_background_tasks()
        assert reported.is_set(), "answering early did not abandon the attempt"

    @pytest.mark.asyncio
    async def test_unexpected_delivery_error_is_logged_and_queued(self):
        """Plan note 11."""

        def deliver(receiver_id, **kwargs):
            raise RuntimeError("pane gone")

        with patch("cli_agent_orchestrator.api.main.logger") as mock_logger:
            body = await self._post(deliver)
        assert (body["success"], body["delivery"], body["error"]) == (True, "queued", None)
        assert any(
            "Immediate delivery attempt failed" in str(c)
            for c in mock_logger.warning.call_args_list
        )

    @pytest.mark.asyncio
    async def test_edge_rules_run_before_the_delivery_starts(self):
        """The reply mark must exist when the delivery settles reply edges."""
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        order = []
        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.delegation_callback_service.on_send_message",
                side_effect=lambda **_kw: order.append("edges"),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=lambda *_a, **_kw: order.append("deliver"),
            ),
        ):
            body = await create_inbox_message_endpoint(
                self._request(), "abcdef12", "sender01", "hello", _scopes=[]
            )
            await self._drain_background_tasks()
        assert order == ["edges", "deliver"]
        assert body["delivery"] == "queued"


C, S, W = "cccc0001", "ssss0002", "wwww0003"


@pytest.fixture
def live(isolated_memory_db, monkeypatch):
    """C -> S -> W with real rows, the real inbox POST, delivery and edge rules; pane mocked."""
    from unittest.mock import MagicMock

    from cli_agent_orchestrator.clients.database import create_terminal
    from cli_agent_orchestrator.models.terminal import TerminalStatus
    from cli_agent_orchestrator.services import delegation_callback_service as svc
    from cli_agent_orchestrator.services import inbox_service as inbox_module

    create_terminal(C, "cao-s", "c", "claude_code", "conductor")
    create_terminal(S, "cao-s", "s", "claude_code", "supervisor", caller_id=C)
    create_terminal(W, "cao-s", "w", "claude_code", "forge", caller_id=S)

    monitor = MagicMock()
    monitor.get_status.return_value = TerminalStatus.IDLE
    monitor.started_on_last_input.return_value = True
    monkeypatch.setattr(svc, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "status_monitor", monitor)
    monkeypatch.setattr(inbox_module, "INBOX_SUBMIT_CONFIRMATION", False)
    monkeypatch.setattr(inbox_module, "EAGER_INBOX_DELIVERY", False)
    screen = MagicMock()
    screen.get_output.side_effect = lambda terminal_id, _mode: f"{terminal_id} SCREEN"
    monkeypatch.setattr(svc, "terminal_service", screen)
    paste = MagicMock()
    paste.dispatch_input.return_value = 0
    monkeypatch.setattr(inbox_module, "terminal_service", paste)
    delivered = []
    monkeypatch.setattr(svc, "_deliver", lambda r, s, registry: delivered.append((r, s)))
    return MagicMock(monitor=monitor, paste=paste, delivered=delivered, svc=svc)


def _edge(caller, worker, armed=True):
    from cli_agent_orchestrator.clients.database import (
        create_delegation_edge,
        mark_assign_edges_delivered,
    )
    from cli_agent_orchestrator.models.delegation import EdgeSource

    create_delegation_edge(caller, worker, EdgeSource.ASSIGN)
    if armed:
        mark_assign_edges_delivered(worker)


def _inbox(receiver_id):
    from cli_agent_orchestrator.clients.database import get_inbox_messages

    return get_inbox_messages(receiver_id, limit=50)


def _not_sent(text):
    from cli_agent_orchestrator.backends import base

    return base.InputNotSentError(text)


class TestInboxPostOverRealDelivery:
    """send_message's immediate outcome over a real database and the real deliver_pending."""

    async def _post(self, sender_id, receiver_id, text):
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        body = await create_inbox_message_endpoint(
            TestCreateInboxMessageResponseTiming._request(),
            receiver_id,
            sender_id,
            text,
            _scopes=[],
        )
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()
        return body

    @pytest.mark.asyncio
    async def test_reply_delivered_by_the_post_closes_its_edge_as_replied(self, live):
        from cli_agent_orchestrator.clients.database import find_any_edge
        from cli_agent_orchestrator.models.delegation import EdgeState

        _edge(S, W)
        body = await self._post(W, S, "final reply")
        assert (body["success"], body["delivery"]) == (True, "delivered")
        assert find_any_edge(S, W).state == EdgeState.REPLIED

    @pytest.mark.asyncio
    async def test_busy_receiver_is_queued_without_typing(self, live):
        from cli_agent_orchestrator.models.terminal import TerminalStatus

        live.monitor.get_status.return_value = TerminalStatus.PROCESSING
        body = await self._post(W, C, "status")
        assert (body["success"], body["delivery"], body["error"]) == (True, "queued", None)
        live.paste.dispatch_input.assert_not_called()
        assert [m.status.value for m in _inbox(C)] == ["pending"]

    @pytest.mark.asyncio
    async def test_older_pending_message_goes_first_and_this_one_is_queued(self, live):
        from cli_agent_orchestrator.clients.database import create_inbox_message

        older = create_inbox_message(S, C, "older")
        body = await self._post(W, C, "newer")
        assert (body["success"], body["delivery"]) == (True, "queued")
        statuses = {m.message: m.status.value for m in _inbox(C)}
        assert statuses == {"older": "delivered", "newer": "pending"}
        assert live.paste.dispatch_input.call_args.args[1] == "older"
        assert older.id != body["message_id"]

    @pytest.mark.asyncio
    async def test_failed_message_is_not_retried_and_its_sender_gets_no_notice(self, live):
        """Spec: a message reported failed is marked failed and never retried by the server."""
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        live.paste.dispatch_input.side_effect = [_not_sent("lost terminal.read"), 0, 0]

        body = await self._post(W, C, "status")
        inbox_service.deliver_pending(C)  # what a later sweep or status event does
        inbox_service.reconcile_orphaned_messages()

        assert (body["success"], body["delivery"]) == (False, "failed")
        assert body["error"] == "InputNotSentError: lost terminal.read", "the raw cause"
        assert live.paste.dispatch_input.call_count == 1
        assert [m.status.value for m in _inbox(C)] == ["failed"]
        assert _inbox(W) == [], "the POST carried the error; no notice"

    @pytest.mark.asyncio
    async def test_failure_while_sending_is_uncertain_and_not_retried(self, live):
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        live.paste.dispatch_input.side_effect = [RuntimeError("lost terminal.send"), 0]
        body = await self._post(W, C, "status")
        inbox_service.deliver_pending(C)
        assert (body["success"], body["delivery"]) == (False, "uncertain")
        _assert_uncertain_error(body["error"], "RuntimeError: lost terminal.send")
        assert live.paste.dispatch_input.call_count == 1
        assert _inbox(W) == []


class TestIncident20261117OverThePost:
    """C -> S -> W: W's reply POST to S failed in mark_input_received, before typing."""

    @pytest.mark.asyncio
    async def test_failed_reply_post_returns_the_error_and_keeps_s_waiting(self, live, monkeypatch):
        from cli_agent_orchestrator.clients.database import find_open_edge

        # Arrange
        _edge(C, S)
        _edge(S, W)
        live.paste.dispatch_input.side_effect = _not_sent(
            "TerminalBackendError: herdr-core lost terminal.read: herdr-core sent nothing for 10.0s"
        )

        # Act
        body = await TestInboxPostOverRealDelivery()._post(W, S, "forge's final reply")

        # Assert: W learns it failed, from its own send_message.
        assert (body["success"], body["delivery"]) == (False, "failed")
        assert "lost terminal.read" in body["error"]
        edge = find_open_edge(S, W)
        assert edge is not None and edge.reply_message_id is None
        assert _inbox(W) == []

        # Past the pickup timeout S is still not forwarded to C, and C has nothing.
        svc = live.svc
        live.monitor.started_on_last_input.return_value = False
        live.monitor.last_input_at.return_value = 100.0
        monkeypatch.setattr(
            svc, "_monotonic", lambda: 100.0 + svc.DELEGATION_PICKUP_TIMEOUT_SECONDS + 1
        )
        assert svc.evaluate_completion(S) == 0
        svc.sweep()
        assert _inbox(C) == []
        assert [d for d in live.delivered if d[0] == C] == []


def _stored(receiver_id):
    (message,) = _inbox(receiver_id)
    return message


def _failure_notices(receiver_id):
    return [m for m in _inbox(receiver_id) if m.message.startswith("[CAO delivery failed]")]


class TestInboxPostWaitSelection:
    """The POST keeps the 5 s wait of earlier versions unless the client asks for the outcome.

    Clients of this route time out after 8 to 30 s (the web UI, the shelf and
    fleet examples, the cross-node notice, the EKS broker, older CAO MCP
    clients). Only a client that sets ``wait_for_outcome`` and has an HTTP
    timeout past INBOX_DELIVERY_OUTCOME_WAIT gets the long wait.
    """

    @staticmethod
    def _delivered_at_once(receiver_id, **kwargs):
        from cli_agent_orchestrator.models.inbox import DeliveryOutcome

        kwargs["on_outcome"](kwargs["report_to_sender"], DeliveryOutcome.DELIVERED, None)

    @pytest.mark.parametrize(
        ("query", "expected_wait"),
        [
            ("", 7.0),
            ("&wait_for_outcome=false", 7.0),
            ("&wait_for_outcome=true", 31.0),
        ],
        ids=["absent", "false", "true"],
    )
    def test_the_query_parameter_picks_the_wait(self, client, query, expected_wait):
        seen = []
        real_wait = asyncio.wait

        async def spy(futures, *, timeout=None, **kwargs):
            seen.append(timeout)
            return await real_wait(futures, timeout=timeout, **kwargs)

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=TestCreateInboxMessageResponseTiming._stored_message(),
            ),
            patch("cli_agent_orchestrator.api.main.delegation_callback_service.on_send_message"),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=self._delivered_at_once,
            ),
            patch("cli_agent_orchestrator.api.main.INBOX_IMMEDIATE_DELIVERY_WAIT", 7.0),
            patch("cli_agent_orchestrator.api.main.INBOX_DELIVERY_OUTCOME_WAIT", 31.0),
            patch("cli_agent_orchestrator.api.main.asyncio.wait", spy),
        ):
            response = client.post(
                f"/terminals/abcdef12/inbox/messages?sender_id=sender01&message=hello{query}"
            )

        assert response.status_code == 200
        assert response.json()["delivery"] == "delivered"
        assert seen == [expected_wait]

    @pytest.mark.asyncio
    async def test_without_the_parameter_a_held_receiver_lock_gets_in_progress_at_the_short_wait(
        self, live
    ):
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        with (
            patch("cli_agent_orchestrator.api.main.INBOX_IMMEDIATE_DELIVERY_WAIT", 0.05),
            patch("cli_agent_orchestrator.api.main.INBOX_DELIVERY_OUTCOME_WAIT", 600.0),
            inbox_service._delivery_lock(C),  # another delivery holds the receiver
        ):
            # A wrong choice of the long wait would block here; fail fast instead.
            body = await asyncio.wait_for(
                create_inbox_message_endpoint(
                    TestCreateInboxMessageResponseTiming._request(), C, W, "status", _scopes=[]
                ),
                timeout=20,
            )
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()

        assert (body["success"], body["delivery"], body["error"]) == (True, "in_progress", None)
        assert _stored(C).status.value == "delivered", "the attempt went on once the lock freed"

    @pytest.mark.asyncio
    async def test_with_the_parameter_the_post_waits_past_the_short_wait_for_the_outcome(
        self, live
    ):
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint
        from cli_agent_orchestrator.services.inbox_service import inbox_service

        live.paste.dispatch_input.side_effect = _not_sent("herdr-core lost terminal.read")
        lock = inbox_service._delivery_lock(C)
        lock.acquire()
        asyncio.get_running_loop().call_later(0.3, lock.release)  # well past the short wait
        with (
            patch("cli_agent_orchestrator.api.main.INBOX_IMMEDIATE_DELIVERY_WAIT", 0.01),
            patch("cli_agent_orchestrator.api.main.INBOX_DELIVERY_OUTCOME_WAIT", 600.0),
        ):
            body = await asyncio.wait_for(
                create_inbox_message_endpoint(
                    TestCreateInboxMessageResponseTiming._request(),
                    C,
                    W,
                    "status",
                    wait_for_outcome=True,
                    _scopes=[],
                ),
                timeout=20,
            )
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()

        assert (body["success"], body["delivery"]) == (False, "failed")
        assert "lost terminal.read" in body["error"]
        assert _failure_notices(W) == [], "the POST reported it, so no notice"


class TestPostHandOffAfterTheBound:
    """Ruling on in_progress: exactly one of the POST or the queued path reports a failure.

    If the POST has given up waiting (answered ``in_progress``) before the
    failure is classified, the message is handled as queued: a failure before
    typing is retried with the attempt counted, an uncertain one notifies the
    sender. If the failure was classified first, the POST reports it and no
    notice is sent. Only the POST and deliver_pending's observable effects are
    asserted; how report_to_sender carries the hand-off is forge's choice.

    W -> C opens no edge (C is not waiting on W and was not created by W), so
    any notice is the sender notice.
    """

    BOUND = 0.05

    async def _post_with(self, dispatch):
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        with patch("cli_agent_orchestrator.api.main.INBOX_DELIVERY_OUTCOME_WAIT", self.BOUND):
            body = await create_inbox_message_endpoint(
                TestCreateInboxMessageResponseTiming._request(),
                C,
                W,
                "status",
                wait_for_outcome=True,
                _scopes=[],
            )
        return body

    @staticmethod
    def _failure(kind):
        if kind == "before_typing":
            return _not_sent("herdr-core lost terminal.read")
        return RuntimeError("herdr-core lost terminal.send")

    async def _abandon_then_fail(self, live, kind):
        """The attempt fails only after the POST has answered."""
        import threading

        answered = threading.Event()

        def dispatch(*_args, **_kwargs):
            assert answered.wait(10), "the POST never answered"
            raise self._failure(kind)

        live.paste.dispatch_input.side_effect = dispatch
        try:
            body = await self._post_with(dispatch)
        finally:
            answered.set()
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()
        return body

    @pytest.mark.asyncio
    async def test_failure_before_typing_after_in_progress_is_retried_as_queued(self, live):
        body = await self._abandon_then_fail(live, "before_typing")

        assert (body["success"], body["delivery"]) == (True, "in_progress")
        stored = _stored(C)
        assert stored.status.value == "pending", "handled as queued: back for a retry"
        assert stored.delivery_attempts == 1
        assert "lost terminal.read" in stored.last_error
        assert _failure_notices(W) == [], "no notice while retries remain"

    @pytest.mark.asyncio
    async def test_uncertain_failure_after_in_progress_notifies_the_sender(self, live):
        body = await self._abandon_then_fail(live, "uncertain")

        assert (body["success"], body["delivery"]) == (True, "in_progress")
        assert _stored(C).status.value == "failed"
        (notice,) = _failure_notices(W)
        assert "may have arrived" in notice.message
        assert (W, C) in live.delivered, "the notice delivery was started"

    @pytest.mark.parametrize("kind", ["before_typing", "uncertain"])
    @pytest.mark.asyncio
    async def test_outcome_before_the_bound_is_reported_by_the_post_only(self, live, kind):
        live.paste.dispatch_input.side_effect = self._failure(kind)
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        body = await create_inbox_message_endpoint(
            TestCreateInboxMessageResponseTiming._request(), C, W, "status", _scopes=[]
        )
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()

        expected = "failed" if kind == "before_typing" else "uncertain"
        assert (body["success"], body["delivery"]) == (False, expected)
        assert _stored(C).status.value == "failed", "never retried"
        assert _failure_notices(W) == []

    @pytest.mark.parametrize("kind", ["before_typing", "uncertain"])
    @pytest.mark.parametrize("order", ["abandon_first", "classify_first"])
    @pytest.mark.asyncio
    async def test_abandon_and_classification_race_has_exactly_one_report_path(
        self, live, order, kind
    ):
        """Drive both orders; whichever wins, exactly one of the two paths reports.

        classify_first: the failure is offered to the hand-off before the POST
        gives up. The POST's give-up is held until the attempt has offered its
        outcome, so the order holds however slowly the worker thread gets to
        its dispatch and no timing bound is involved. The POST must still
        report the failure.
        """
        import threading

        from cli_agent_orchestrator.services.inbox_service import DeliveryHandOff

        answered = threading.Event()
        offered = threading.Event()
        real_offer = DeliveryHandOff.offer
        real_abandon = DeliveryHandOff.abandon

        def offer(hand_off, outcome, error):
            try:
                return real_offer(hand_off, outcome, error)
            finally:
                offered.set()

        def abandon(hand_off):
            if order == "classify_first":
                # Runs on the event loop thread; the worker thread that offers
                # does not need the loop, so blocking here cannot deadlock.
                assert offered.wait(10), "the attempt never offered an outcome"
            return real_abandon(hand_off)

        def dispatch(*_args, **_kwargs):
            if order == "abandon_first":
                assert answered.wait(10), "the POST never answered"
            raise self._failure(kind)

        live.paste.dispatch_input.side_effect = dispatch
        try:
            with (
                patch.object(DeliveryHandOff, "offer", offer),
                patch.object(DeliveryHandOff, "abandon", abandon),
            ):
                body = await self._post_with(dispatch)
        finally:
            answered.set()
        await TestCreateInboxMessageResponseTiming._drain_background_tasks()

        stored = _stored(C)
        notices = _failure_notices(W)
        reported_by_post = body["delivery"] in ("failed", "uncertain")
        if kind == "before_typing":
            handled_as_queued = stored.status.value == "pending" and stored.delivery_attempts == 1
        else:
            handled_as_queued = len(notices) == 1
        assert reported_by_post != handled_as_queued, (body, stored, notices)
        if order == "abandon_first":
            assert body["delivery"] == "in_progress" and handled_as_queued
        else:
            assert body["success"] is False and reported_by_post
            assert stored.status.value == "failed" and notices == []
        assert len(notices) <= 1
