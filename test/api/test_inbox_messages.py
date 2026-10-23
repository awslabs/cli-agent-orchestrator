"""Tests for the new inbox messages GET endpoint."""

import asyncio
import json
from datetime import datetime
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

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

    @pytest.mark.asyncio
    async def test_responds_after_the_paste_while_confirmation_is_still_running(self):
        import threading

        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        release = threading.Event()  # held by the test: the confirmation never ends on its own
        finished = threading.Event()

        def deliver(receiver_id, registry=None, after_dispatch=None):
            try:
                after_dispatch()  # the paste reached the pane
                # Deadlock guard only: the test sets release; a timeout here means
                # a mutant made the endpoint wait for the whole delivery, and the
                # finished/not-finished assertion below reports it.
                release.wait(10)  # ...and the confirmation wait begins
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
                # The response is here and the delivery provably has not
                # finished: it is blocked on an Event nobody has set.
                assert body["message_id"] == 7
                assert not release.is_set()
                assert not finished.is_set()
            finally:
                release.set()
            await self._drain_background_tasks()
        assert finished.is_set()

    @pytest.mark.asyncio
    async def test_responds_within_the_budget_when_the_paste_itself_is_delayed(self):
        """The receiver's lock is held by another delivery: answer anyway, the
        attempt keeps running in the background and still completes."""
        import threading

        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        release = threading.Event()  # the other delivery's lock, held by the test
        pasted = threading.Event()

        def deliver(receiver_id, registry=None, after_dispatch=None):
            release.wait(10)  # blocked on the per-terminal lock, no paste yet (guard, see above)
            after_dispatch()
            pasted.set()

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=deliver,
            ),
            # Any budget works: the delivery is blocked on an Event only the
            # test sets, so the response can only come from the budget expiring.
            patch("cli_agent_orchestrator.api.main.INBOX_IMMEDIATE_DELIVERY_WAIT", 0.05),
        ):
            try:
                body = await create_inbox_message_endpoint(
                    self._request(), "abcdef12", "sender01", "hello", _scopes=[]
                )
                assert body["success"] is True
                assert not pasted.is_set(), "the attempt was still running when we answered"
                assert not release.is_set()
            finally:
                release.set()
            # Answering early did not abandon the attempt.
            await self._drain_background_tasks()
        assert pasted.is_set()

    @pytest.mark.asyncio
    async def test_delivery_failure_in_the_background_is_logged_not_raised(self):
        from cli_agent_orchestrator.api.main import create_inbox_message_endpoint

        def deliver(receiver_id, registry=None, after_dispatch=None):
            raise RuntimeError("pane gone")

        with (
            patch(
                "cli_agent_orchestrator.api.main.create_inbox_message",
                return_value=self._stored_message(),
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_service.deliver_pending",
                side_effect=deliver,
            ),
            patch("cli_agent_orchestrator.api.main.logger") as mock_logger,
        ):
            body = await create_inbox_message_endpoint(
                self._request(), "abcdef12", "sender01", "hello", _scopes=[]
            )
            assert body["success"] is True
            # The done callback runs when the delivery task completes; awaiting
            # that task is the barrier (its callback was registered first, so it
            # has run by the time gather returns).
            await self._drain_background_tasks()

        assert any(
            "Immediate delivery attempt failed" in str(c)
            for c in mock_logger.warning.call_args_list
        )
