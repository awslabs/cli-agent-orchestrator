"""Tests for terminal-related API endpoints including working directory and exit."""

from typing import Dict
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from cli_agent_orchestrator.api.main import app
from cli_agent_orchestrator.constants import (
    TERMINAL_GROUP_ELEMENT_MAX_LEN,
    TERMINAL_GROUP_MAX_ELEMENTS,
    TERMINAL_METADATA_MAX_BYTES,
)
from cli_agent_orchestrator.models.terminal import Terminal


class TestWorkingDirectoryEndpoint:
    """Test GET /terminals/{terminal_id}/working-directory endpoint."""

    def test_get_working_directory_success(self, client):
        """Test successful retrieval of working directory."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.return_value = "/home/user/project"

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 200
            data = response.json()
            assert data["working_directory"] == "/home/user/project"
            mock_svc.get_working_directory.assert_called_once_with("abcd1234")

    def test_get_working_directory_returns_none(self, client):
        """Test when working directory is unavailable."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.return_value = None

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 200
            assert response.json()["working_directory"] is None

    def test_get_working_directory_terminal_not_found(self, client):
        """Test 404 when terminal doesn't exist."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = ValueError("Terminal 'abcd5678' not found")

            response = client.get("/terminals/abcd5678/working-directory")

            assert response.status_code == 404
            assert "not found" in response.json()["detail"].lower()

    def test_get_working_directory_server_error(self, client):
        """Test 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = Exception("TMux error")

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 500
            assert "Failed to get working directory" in response.json()["detail"]

    def test_get_working_directory_internal_error(self, client):
        """Test 500 when internal error occurs."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_working_directory.side_effect = RuntimeError("Internal service error")

            response = client.get("/terminals/abcd1234/working-directory")

            assert response.status_code == 500
            assert "Failed to get working directory" in response.json()["detail"]


class TestSessionCreationWithWorkingDirectory:
    """Test session creation with working_directory parameter."""

    def test_create_session_passes_working_directory(self, client, tmp_path):
        """Test that working_directory parameter is passed to service."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(
                return_value=Terminal(
                    id="abcd1234",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="developer",
                )
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": str(tmp_path),
                },
            )

            assert response.status_code == 201
            # Verify working_directory was passed
            call_kwargs = mock_svc.create_session.call_args.kwargs
            assert call_kwargs.get("working_directory") == str(tmp_path)
            assert call_kwargs.get("registry") is not None

    def test_create_session_with_working_directory(self, client):
        """Test POST /sessions with working_directory parameter."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(
                return_value=Terminal(
                    id="abcd1234",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="developer",
                )
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "working_directory": "/custom/path",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_session.call_args.kwargs
            assert call_kwargs.get("working_directory") == "/custom/path"


class TestTerminalCreationWithWorkingDirectory:
    """Test terminal creation with working_directory parameter."""

    def test_create_terminal_passes_working_directory(self, client, tmp_path):
        """Test that working_directory parameter is passed to service."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "working_directory": str(tmp_path),
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == str(tmp_path)

    def test_create_terminal_passes_caller_id(self, client):
        """caller_id query param threads through to the service (issue #284)."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                    caller_id="dcba8765",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "caller_id": "dcba8765",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("caller_id") == "dcba8765"
            assert response.json()["caller_id"] == "dcba8765"

    def test_create_terminal_passes_model(self, client):
        """model query param threads through to the service -- explicit
        per-call model override for MCP handoff/assign."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="claude_code",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "analyst",
                    "model": "fable-5",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("model") == "fable-5"

    def test_create_terminal_omitted_model_forwards_none(self, client):
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={"provider": "kiro_cli", "agent_profile": "analyst"},
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("model") is None

    def test_create_terminal_rejects_malformed_model(self, client):
        """PR #501 review: a malformed model (control char/newline/shell
        metacharacter) must 400 at the request boundary rather than either
        reaching terminal_service unvalidated or -- if it later raised
        ValueError there -- being mismapped to a misleading 404 (this
        endpoint's ValueError handler means "session/window not found")."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "model": "fable-5\nrm -rf /",
                },
            )

            assert response.status_code == 400
            mock_svc.create_terminal.assert_not_called()

    def test_create_terminal_kas_refusal_maps_to_400_not_404(self, client):
        """A KAS refusal is a bad request, not a missing resource.

        KiroPhase0KASError subclasses ValueError, and this endpoint's generic
        ValueError arm means "session/window not found" -- so without a narrower
        arm ordered first, an engine rejection reports 404. POST /sessions
        already returns 400 for the identical failure.
        """
        from cli_agent_orchestrator.providers.kiro_capabilities import KiroPhase0KASError

        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                side_effect=KiroPhase0KASError(profile_has_v2_policy=False)
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "engine": "kas",
                },
            )

        assert response.status_code == 400

    def test_create_terminal_rejects_malformed_caller_id(self, client):
        """caller_id is validated against the TerminalId pattern — IDs arrive
        from agent input and must not be persisted unvalidated."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "caller_id": "not-a-terminal-id!",
                },
            )

            assert response.status_code == 422
            mock_svc.create_terminal.assert_not_called()

    def test_create_terminal_in_session_with_working_directory(self, client):
        """Test POST /sessions/{session}/terminals with working_directory."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "working_directory": "/session/path",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("working_directory") == "/session/path"

    def test_create_terminal_in_session_forwards_use_worktree_true(self, client):
        """issue #100 Phase 1: the use_worktree query param reaches
        terminal_service.create_terminal unchanged."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "use_worktree": "true",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("use_worktree") is True

    def test_create_terminal_in_session_defaults_use_worktree_false(self, client):
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={"provider": "kiro_cli", "agent_profile": "analyst"},
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("use_worktree") is False

    def test_create_terminal_in_session_worktree_error_maps_to_400(self, client):
        """A working_directory that isn't a git repo is a client-input
        problem, not a server crash."""
        from cli_agent_orchestrator.services.worktree_service import WorktreeError

        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                side_effect=WorktreeError("'/tmp/x' is not inside a git repository")
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "use_worktree": "true",
                },
            )

            assert response.status_code == 400
            assert "not inside a git repository" in response.json()["detail"]

    def test_create_terminal_in_session_forwards_idempotency_key(self, client):
        """Review on PR #634, issue #616."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="analyst",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "analyst",
                    "idempotency_key": "retry-1",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs.get("idempotency_key") == "retry-1"

    def test_create_terminal_rejects_initial_message_without_defer_init(self, client):
        """initial_message is only delivered on the deferred-init path; sending
        it with defer_init=false must 400 rather than silently drop the payload."""
        with (
            patch(
                "cli_agent_orchestrator.api.main.resolve_provider",
                side_effect=lambda _, fallback_provider: fallback_provider,
            ),
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_svc.create_terminal = AsyncMock()

            response = client.post(
                "/sessions/test-session/terminals",
                params={"provider": "kiro_cli", "agent_profile": "analyst"},
                json={"initial_message": "do work"},
            )

            assert response.status_code == 400
            assert "defer_init=true" in response.json()["detail"]
            # The payload was rejected before reaching the service.
            mock_svc.create_terminal.assert_not_called()


class TestExitTerminalEndpoint:
    """Test POST /terminals/{terminal_id}/exit endpoint.

    The endpoint now delegates to ``terminal_service.exit_terminal_cli`` (the
    shared graceful-shutdown helper); the send_input-vs-send_special_key
    branching is unit-tested in test_terminal_service.py. These tests pin the
    boundary contract: delegation + domain-error -> HTTP-status mapping.
    """

    def test_exit_terminal_delegates_and_returns_success(self, client):
        """A successful exit delegates to exit_terminal_cli and returns 200."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.exit_terminal_cli.assert_called_once_with("abcd1234")

    def test_exit_terminal_value_error_maps_to_404(self, client):
        """A ValueError (e.g. no provider) maps to 404 at the boundary."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.exit_terminal_cli.side_effect = ValueError("Provider not found for terminal x")

            response = client.post("/terminals/deadbeef/exit")

            assert response.status_code == 404
            assert "Provider not found" in response.json()["detail"]

    def test_exit_terminal_server_error_maps_to_500(self, client):
        """An unexpected error maps to 500."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.exit_terminal_cli.side_effect = RuntimeError("TMux error")

            response = client.post("/terminals/abcd1234/exit")

            assert response.status_code == 500
            assert "Failed to exit terminal" in response.json()["detail"]


class TestDeleteTerminalEndpoint:
    """Test DELETE /terminals/{terminal_id} endpoint."""

    def test_delete_terminal_success(self, client):
        """DELETE /terminals/{terminal_id} deletes and returns success."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.return_value = True

            response = client.delete("/terminals/abcd1234")

            assert response.status_code == 200
            assert response.json() == {"success": True}
            mock_svc.delete_terminal.assert_called_once_with("abcd1234", registry=ANY)

    def test_delete_terminal_deferred_cleanup_is_conflict(self, client):
        """A retained Grok home is a failed delete, not HTTP 200."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.return_value = False

            response = client.delete("/terminals/abcd1234")

            assert response.status_code == 409
            assert "cleanup deferred" in response.json()["detail"]

    def test_delete_terminal_not_found(self, client):
        """DELETE /terminals/{terminal_id} returns 404 for missing terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = ValueError("Terminal not found")

            response = client.delete("/terminals/deadbeef")

            assert response.status_code == 404

    def test_delete_terminal_server_error(self, client):
        """DELETE /terminals/{terminal_id} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = Exception("TMux error")

            response = client.delete("/terminals/abcd1234")

            assert response.status_code == 500
            assert "Failed to delete terminal" in response.json()["detail"]


class TestCreateInboxMessageEndpoint:
    """Test POST /terminals/{receiver_id}/inbox/messages endpoint."""

    def test_create_inbox_message_success(self, client):
        """POST creates an inbox message and returns success."""
        mock_msg = MagicMock()
        mock_msg.id = 1
        mock_msg.sender_id = "sender1"
        mock_msg.receiver_id = "abcd1234"
        mock_msg.created_at.isoformat.return_value = "2026-03-13T12:00:00"

        with (
            patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create,
            patch("cli_agent_orchestrator.api.main.inbox_service") as mock_inbox,
        ):
            mock_create.return_value = mock_msg

            response = client.post(
                "/terminals/abcd1234/inbox/messages",
                params={"sender_id": "sender1", "message": "hello"},
            )

            assert response.status_code == 200
            data = response.json()
            assert data["success"] is True
            assert data["message_id"] == 1
            assert data["sender_id"] == "sender1"
            mock_create.assert_called_once_with(
                "sender1",
                "abcd1234",
                "hello",
            )
            mock_inbox.deliver_pending.assert_called_once_with("abcd1234", registry=ANY)

    def test_create_inbox_message_delivery_failure_still_succeeds(self, client):
        """Immediate delivery failure should not fail the API response."""
        mock_msg = MagicMock()
        mock_msg.id = 2
        mock_msg.sender_id = "sender1"
        mock_msg.receiver_id = "abcd1234"
        mock_msg.created_at.isoformat.return_value = "2026-03-13T12:00:00"

        with (
            patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create,
            patch("cli_agent_orchestrator.api.main.inbox_service") as mock_inbox,
        ):
            mock_create.return_value = mock_msg
            mock_inbox.deliver_pending.side_effect = Exception("TMux busy")

            response = client.post(
                "/terminals/abcd1234/inbox/messages",
                params={"sender_id": "sender1", "message": "hello"},
            )

            assert response.status_code == 200
            assert response.json()["success"] is True

    def test_create_inbox_message_not_found(self, client):
        """POST returns 404 when terminal not found."""
        with patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create:
            mock_create.side_effect = ValueError("Terminal not found")

            response = client.post(
                "/terminals/deadbeef/inbox/messages",
                params={"sender_id": "sender1", "message": "hello"},
            )

            assert response.status_code == 404

    def test_create_inbox_message_server_error(self, client):
        """POST returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.create_inbox_message") as mock_create:
            mock_create.side_effect = Exception("DB error")

            response = client.post(
                "/terminals/abcd1234/inbox/messages",
                params={"sender_id": "sender1", "message": "hello"},
            )

            assert response.status_code == 500
            assert "Failed to create inbox message" in response.json()["detail"]


class TestWebSocketLocalhostRestriction:
    """Test that WebSocket endpoint rejects non-loopback clients."""

    def test_websocket_rejects_non_loopback(self, client):
        """WebSocket should close with 4003 for non-localhost clients."""
        # TestClient uses "testclient" as host, which is not in the allowlist
        with pytest.raises(Exception):
            with client.websocket_connect("/terminals/abcd1234/ws"):
                pass

    @pytest.mark.asyncio
    async def test_websocket_endpoint_admits_client_in_allowlist(self):
        """Direct-call unit test: a client whose host is in WS_ALLOWED_CLIENTS
        passes the allowlist gate and reaches the next step (terminal lookup).

        Driving the coroutine directly with a mock ``WebSocket`` lets us
        observe the exact ``close`` calls without fighting the TestClient's
        opaque mapping of post-accept closes to HTTP 400 denials.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="172.17.0.1")  # Docker bridge IP, simulating issue #149
        ws.headers = {}  # non-browser client: no Origin header, passes the Origin gate
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1", "::1", "localhost", "172.17.0.1"],
            ),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value=None,
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        # Allowlist let it through → accept happened.
        ws.accept.assert_awaited_once()
        # Terminal lookup returned None → close with 4004 (not 4003).
        ws.close.assert_awaited_once()
        kwargs = ws.close.call_args.kwargs
        assert kwargs.get("code") == 4004

    @pytest.mark.asyncio
    async def test_websocket_endpoint_rejects_client_outside_allowlist(self):
        """Direct-call unit test: a client whose host is not in the allowlist
        is closed with policy code 4003 before any accept happens."""
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="10.0.0.5")
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with patch(
            "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
            ["127.0.0.1", "::1", "localhost"],
        ):
            await terminal_ws(ws, "abcd1234")

        # Rejected before accept.
        ws.accept.assert_not_called()
        ws.close.assert_awaited_once()
        kwargs = ws.close.call_args.kwargs
        assert kwargs.get("code") == 4003

    @pytest.mark.asyncio
    async def test_websocket_endpoint_rejects_invalid_tmux_metadata(self):
        """Defence-in-depth: if a stored terminal row contains a tmux session
        or window name with delimiter characters, the WS handler must close
        the connection rather than splice the bad name into ``tmux -t``.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1"],
            ),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value={"tmux_session": "evil:name", "tmux_window": "win"},
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        # Allowlist passed → accept happened, but validation failed → 4003.
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once()
        kwargs = ws.close.call_args.kwargs
        assert kwargs.get("code") == 4003
        assert "Invalid tmux target name" in kwargs.get("reason", "")

    @pytest.mark.asyncio
    async def test_websocket_rejects_cross_site_origin(self):
        """CWE-1385: a loopback peer carrying a foreign browser Origin (the
        cross-site WebSocket hijacking scenario) is closed with 4403 before
        any accept — even though its IP passes ``WS_ALLOWED_CLIENTS``.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")  # browser connects from loopback
        # Attacker page: its Origin is its own site, but the socket's Host is
        # the CAO server it targets (browser sets Host, script cannot forge it).
        ws.headers = {"origin": "http://evil.example.com", "host": "localhost:9889"}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1", "::1", "localhost"],
            ),
            patch(
                "cli_agent_orchestrator.constants.CORS_ORIGINS",
                ["http://localhost:9889", "http://127.0.0.1:9889"],
            ),
            patch("cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS", []),
        ):
            await terminal_ws(ws, "abcd1234")

        # Origin authority (evil.example.com) != Host (localhost:9889) and not
        # in any allowlist → rejected before accept, no PTY spun up.
        ws.accept.assert_not_called()
        ws.close.assert_awaited_once()
        kwargs = ws.close.call_args.kwargs
        assert kwargs.get("code") == 4403

    @pytest.mark.asyncio
    async def test_websocket_admits_same_origin_via_host_match(self):
        """The bundled viewer is served by cao-server, so its Origin authority
        equals the request Host. That must pass EVEN WHEN the origin is absent
        from ``CORS_ORIGINS`` — the imported-app deployment
        (``uvicorn ...:app``) never runs ``add_local_cors_origins``, so the
        same-origin match on Host is what keeps its viewer working.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {"origin": "http://localhost:9889", "host": "localhost:9889"}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1", "::1", "localhost"],
            ),
            # Deliberately empty: proves the pass is via Host-match, not CORS.
            patch("cli_agent_orchestrator.constants.CORS_ORIGINS", []),
            patch("cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS", []),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value=None,
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        # Same-origin → accept happened; terminal lookup None → 4004 (not 4403).
        ws.accept.assert_awaited_once()
        ws.close.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 4004

    @pytest.mark.asyncio
    async def test_websocket_admits_proxied_https_same_origin(self):
        """Codespaces / reverse-proxy: the viewer loads over HTTPS at a dynamic
        forwarded hostname and the WSS handshake carries a matching Host. The
        same-origin match must accept it without the operator pre-registering
        the (unpredictable) origin.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        proxied = "myspace-9889.app.github.dev"
        ws = MagicMock()
        ws.client = MagicMock(host="10.0.0.7")  # the forwarding proxy's peer IP
        ws.headers = {"origin": f"https://{proxied}", "host": proxied}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            # Codespaces doc sets CAO_WS_ALLOWED_CLIENTS="*".
            patch("cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS", ["*"]),
            patch("cli_agent_orchestrator.constants.CORS_ORIGINS", []),
            patch("cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS", []),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value=None,
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        ws.accept.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 4004

    @pytest.mark.asyncio
    async def test_websocket_admits_cross_origin_via_allowlist(self):
        """A genuinely cross-origin viewer (Origin authority != Host) still
        works when the operator lists it in ``CAO_WS_ALLOWED_ORIGINS``.
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {"origin": "https://viewer.example.dev", "host": "localhost:9889"}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1", "::1", "localhost"],
            ),
            patch("cli_agent_orchestrator.constants.CORS_ORIGINS", []),
            patch(
                "cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS",
                ["https://viewer.example.dev"],
            ),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value=None,
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        ws.accept.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 4004

    @pytest.mark.asyncio
    async def test_websocket_admits_non_browser_client_without_origin(self):
        """Native (non-browser) clients — CLI, the ``websockets`` lib, tests —
        send no Origin header. The CSRF threat is browser-only, so a missing
        Origin passes the guard (still gated by the loopback IP allowlist).
        """
        from cli_agent_orchestrator.api.main import terminal_ws

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}  # no Origin
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        with (
            patch(
                "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                ["127.0.0.1", "::1", "localhost"],
            ),
            patch("cli_agent_orchestrator.constants.CORS_ORIGINS", []),
            patch("cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS", []),
            patch(
                "cli_agent_orchestrator.api.main.get_terminal_metadata",
                return_value=None,
            ),
        ):
            await terminal_ws(ws, "abcd1234")

        ws.accept.assert_awaited_once()
        assert ws.close.call_args.kwargs.get("code") == 4004


class TestWebSocketOriginIntegration:
    """End-to-end Origin-guard coverage through the real ASGI middleware stack
    (TrustedHostMiddleware + CORSMiddleware + the route), not a mock WebSocket.

    Starlette's ``TestClient.websocket_connect`` performs a genuine ASGI
    handshake: a 4403 policy close surfaces as ``WebSocketDisconnect`` when the
    client tries to receive, while an admitted connection proceeds past accept.
    The TestClient's peer host is ``testclient``, so admit it in
    ``WS_ALLOWED_CLIENTS`` to isolate the Origin behavior under test.
    """

    def _client(self):
        from test.api.conftest import TestClientWithHost

        app.state.plugin_registry = None
        return TestClientWithHost(app)

    def _trust_host(self, host):
        """Add ``host`` to the live ``ALLOWED_HOSTS`` list so a fresh
        ``TrustedHostMiddleware`` (built when the TestClient constructs its
        stack) trusts it — the reverse-proxy case the documented Codespaces
        ``CAO_ALLOWED_HOSTS`` / ``add_local_cors_origins`` flow handles at
        runtime. ``add_middleware`` captured the list by reference, and the
        middleware copies it at build time, so mutating it in place before the
        client builds is what takes effect. Returns a restore callback.
        """
        import cli_agent_orchestrator.api.main as main_mod

        added = host not in main_mod.ALLOWED_HOSTS
        if added:
            main_mod.ALLOWED_HOSTS.append(host)
        # The app is a module singleton whose middleware_stack is built and
        # cached on first request; drop it so the next connect rebuilds the
        # TrustedHostMiddleware from the now-extended ALLOWED_HOSTS list.
        app.middleware_stack = None

        def restore():
            if added and host in main_mod.ALLOWED_HOSTS:
                main_mod.ALLOWED_HOSTS.remove(host)
            app.middleware_stack = None

        return restore

    def _connect(self, origin, host="localhost", cors=None, extra_origins=None, trust_host=False):
        """Open a WS handshake with the given Origin/Host and return the close
        code (or None if the socket was admitted, then closed cleanly)."""
        from starlette.websockets import WebSocketDisconnect

        headers = {"Host": host}
        if origin is not None:
            headers["Origin"] = origin

        restore = self._trust_host(host) if trust_host else (lambda: None)
        try:
            with (
                patch(
                    "cli_agent_orchestrator.api.main.WS_ALLOWED_CLIENTS",
                    ["testclient", "127.0.0.1", "::1", "localhost"],
                ),
                patch("cli_agent_orchestrator.constants.CORS_ORIGINS", cors or []),
                patch(
                    "cli_agent_orchestrator.constants.WS_ALLOWED_ORIGINS",
                    extra_origins or [],
                ),
                # Admitted path stops at terminal lookup so no PTY is spawned.
                patch(
                    "cli_agent_orchestrator.api.main.get_terminal_metadata",
                    return_value=None,
                ),
            ):
                client = self._client()
                try:
                    with client.websocket_connect("/terminals/abcd1234/ws", headers=headers) as ws:
                        # Admitted → accept happened; server then closes with
                        # 4004 (terminal not found). Receiving surfaces that.
                        try:
                            ws.receive_text()
                        except WebSocketDisconnect as exc:
                            return exc.code
                        return None
                except WebSocketDisconnect as exc:
                    # Pre-accept policy close (4403 Origin / 4003 IP).
                    return exc.code
        finally:
            restore()

    def test_bundled_same_origin_viewer_is_admitted(self):
        """The imported-app deployment (``uvicorn ...:app``) never runs
        ``add_local_cors_origins``; with empty CORS the bundled viewer at the
        server's own origin must still be admitted via the Host match."""
        code = self._connect("http://localhost:9889", host="localhost:9889", cors=[])
        assert code == 4004  # admitted, then terminal-not-found

    def test_proxied_https_same_origin_is_admitted(self):
        """Codespaces / reverse proxy: HTTPS viewer at a dynamic forwarded
        hostname, handshake Host matches, admitted with no allowlist entry."""
        proxied = "myspace-9889.app.github.dev"
        code = self._connect(f"https://{proxied}", host=proxied, cors=[], trust_host=True)
        assert code == 4004

    def test_cross_site_origin_is_rejected(self):
        """CWE-1385: a foreign Origin whose authority differs from the Host is
        closed with 4403 before accept, through the real stack."""
        code = self._connect("http://evil.example.com", host="localhost:9889", cors=[])
        assert code == 4403

    def test_no_origin_is_admitted(self):
        """Non-browser client: no Origin header, admitted (IP-gated only)."""
        code = self._connect(None, host="localhost", cors=[])
        assert code == 4004


class TestBuildPtyEnv:
    """Tests for the tmux PTY attach environment builder (issue #150).

    The helper is responsible for ensuring the tmux ``attach-session``
    subprocess sees a usable ``TERM`` value. Container environments
    routinely ship with ``TERM`` unset or set to ``dumb``, which breaks
    xterm.js rendering on the browser side.
    """

    def _build(self, env_overrides):
        """Run ``_build_pty_env`` under a controlled os.environ."""
        from cli_agent_orchestrator.api.main import _build_pty_env

        with patch.dict("os.environ", env_overrides, clear=True):
            return _build_pty_env()

    def test_unset_term_is_defaulted(self):
        env = self._build({"HOME": "/root", "PATH": "/usr/bin"})
        assert env["TERM"] == "xterm-256color"

    def test_empty_string_term_is_defaulted(self):
        env = self._build({"TERM": "", "HOME": "/root"})
        assert env["TERM"] == "xterm-256color"

    def test_dumb_term_is_overridden(self):
        # The whole point of issue #150 — Docker's TERM=dumb is unusable.
        env = self._build({"TERM": "dumb", "HOME": "/root"})
        assert env["TERM"] == "xterm-256color"

    def test_explicit_xterm_term_is_preserved(self):
        env = self._build({"TERM": "xterm-256color", "HOME": "/root"})
        assert env["TERM"] == "xterm-256color"

    def test_custom_term_is_preserved(self):
        # Operators that explicitly pick a different terminfo entry should
        # see their choice respected — only unset/empty/dumb gets overridden.
        env = self._build({"TERM": "screen-256color", "HOME": "/root"})
        assert env["TERM"] == "screen-256color"

    def test_other_env_vars_are_inherited(self):
        env = self._build(
            {
                "HOME": "/home/cao",
                "PATH": "/opt/cao/bin",
                "AWS_REGION": "us-west-2",
                "CAO_TERMINAL_ID": "abcd1234",
            }
        )
        # The whole parent env must reach the subprocess so tmux can find its
        # config and the agent CLIs can locate their credentials.
        assert env["HOME"] == "/home/cao"
        assert env["PATH"] == "/opt/cao/bin"
        assert env["AWS_REGION"] == "us-west-2"
        assert env["CAO_TERMINAL_ID"] == "abcd1234"


class TestWebSocketSubprocessTerm:
    """Wiring guard: ensure ``terminal_ws`` actually hands the PTY env
    (with the corrected ``TERM``) to the tmux attach subprocess. Catches
    a regression where the helper exists but never gets called from the
    endpoint."""

    @pytest.mark.asyncio
    async def test_subprocess_popen_receives_corrected_term(self):
        """When the parent process has TERM=dumb, the tmux attach Popen call
        must receive ``env`` with ``TERM=xterm-256color`` instead of inheriting
        the broken value.

        We let Popen raise immediately after capture so the endpoint stops
        before touching the real PTY/asyncio loop.
        """
        from cli_agent_orchestrator.api import main as main_module

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        captured: Dict[str, object] = {}

        def capture_and_stop(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            raise _StopHere("captured Popen args")

        backend = MagicMock()
        backend.prepare_web_attach.return_value = [
            "tmux",
            "-u",
            "attach-session",
            "-t",
            "cao-s:w",
        ]

        with (
            patch.dict("os.environ", {"TERM": "dumb", "HOME": "/root"}, clear=True),
            patch.object(
                main_module,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-s", "tmux_window": "w"},
            ),
            patch.object(main_module, "get_backend", return_value=backend),
            patch.object(main_module.subprocess, "Popen", side_effect=capture_and_stop),
            patch.object(main_module.pty, "openpty", return_value=(100, 101)),
            patch.object(main_module.fcntl, "ioctl"),
            patch.object(main_module.fcntl, "fcntl"),
            patch.object(main_module.os, "close"),
        ):
            with pytest.raises(_StopHere):
                await main_module.terminal_ws(ws, "abcd1234")

        passed_env = captured["kwargs"].get("env")  # type: ignore[union-attr]
        assert passed_env is not None, (
            "tmux Popen must receive env=; without it the subprocess inherits "
            "the parent's broken TERM (issue #150)"
        )
        assert passed_env["TERM"] == "xterm-256color"

    @pytest.mark.asyncio
    async def test_websocket_uses_configured_backend_attach_command(self):
        """Herdr-backed terminals must not be attached through tmux."""
        from cli_agent_orchestrator.api import main as main_module

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        backend = MagicMock()
        backend.prepare_web_attach.return_value = ["herdr", "--session", "cao"]

        captured: Dict[str, object] = {}

        def capture_and_stop(*args, **kwargs):
            captured["args"] = args
            raise _StopHere("captured Popen args")

        with (
            patch.object(
                main_module,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-s", "tmux_window": "w"},
            ),
            patch.object(main_module, "get_backend", return_value=backend),
            patch.object(main_module.subprocess, "Popen", side_effect=capture_and_stop),
            patch.object(main_module.pty, "openpty", return_value=(100, 101)),
            patch.object(main_module.fcntl, "ioctl"),
            patch.object(main_module.fcntl, "fcntl"),
            patch.object(main_module.os, "close"),
        ):
            with pytest.raises(_StopHere):
                await main_module.terminal_ws(ws, "abcd1234")

        backend.prepare_web_attach.assert_called_once_with("cao-s", "w")
        assert captured["args"][0] == ["herdr", "--session", "cao"]

    @pytest.mark.asyncio
    async def test_websocket_closes_safely_when_backend_attach_fails(self):
        """Backend attach errors close safely before allocating a PTY."""
        from cli_agent_orchestrator.api import main as main_module
        from cli_agent_orchestrator.backends import TerminalBackendError

        ws = MagicMock()
        ws.client = MagicMock(host="127.0.0.1")
        ws.headers = {}
        ws.accept = AsyncMock()
        ws.close = AsyncMock()

        backend = MagicMock()
        backend.prepare_web_attach.side_effect = TerminalBackendError("sensitive backend detail")

        with (
            patch.object(
                main_module,
                "get_terminal_metadata",
                return_value={"tmux_session": "cao-s", "tmux_window": "w"},
            ),
            patch.object(main_module, "get_backend", return_value=backend),
            patch.object(main_module.pty, "openpty") as mock_openpty,
        ):
            await main_module.terminal_ws(ws, "abcd1234")

        ws.close.assert_awaited_once_with(code=4004, reason="Failed to attach terminal")
        mock_openpty.assert_not_called()


class _StopHere(Exception):
    """Sentinel raised by the wiring test once Popen args are captured."""


class TestCrossProviderResolution:
    """Test that create_terminal_in_session resolves provider from agent profile
    while create_session always uses the explicit provider parameter."""

    def test_create_terminal_uses_profile_provider(self, client):
        """create_terminal_in_session should resolve provider from agent profile when omitted."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            mock_resolve.return_value = "claude_code"
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd1234",
                    name="test-window",
                    session_name="test-session",
                    provider="claude_code",
                    agent_profile="developer",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "agent_profile": "developer",
                },
            )

            assert response.status_code == 201
            # Verify resolve_provider was called with the default fallback
            mock_resolve.assert_called_once_with("developer", fallback_provider="kiro_cli")
            # Verify terminal_service got the resolved provider
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs["provider"] == "claude_code"

    def test_create_terminal_falls_back_when_no_profile_provider(self, client):
        """create_terminal_in_session should use fallback when profile has no provider."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc,
        ):
            # resolve_provider returns the fallback (no profile provider key)
            mock_resolve.return_value = "kiro_cli"
            mock_svc.create_terminal = AsyncMock(
                return_value=Terminal(
                    id="abcd5678",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="reviewer",
                )
            )

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "reviewer",
                },
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_terminal.call_args.kwargs
            assert call_kwargs["provider"] == "kiro_cli"

    def test_create_session_does_not_resolve_provider(self, client):
        """create_session should NOT call resolve_provider — CLI flag is the override."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.session_service") as mock_svc,
        ):
            mock_svc.create_session = AsyncMock(
                return_value=Terminal(
                    id="abcd1234",
                    name="test-window",
                    session_name="test-session",
                    provider="kiro_cli",
                    agent_profile="supervisor",
                )
            )

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "supervisor",
                },
            )

            assert response.status_code == 201
            # resolve_provider should NOT have been called
            mock_resolve.assert_not_called()
            # session_service should get the raw provider param
            call_kwargs = mock_svc.create_session.call_args.kwargs
            assert call_kwargs["provider"] == "kiro_cli"

    def test_create_terminal_returns_500_on_resolve_error(self, client):
        """Internal errors during provider resolution should return 500."""
        with (
            patch("cli_agent_orchestrator.api.main.resolve_provider") as mock_resolve,
            patch("cli_agent_orchestrator.api.main.terminal_service"),
        ):
            mock_resolve.side_effect = Exception("Unexpected filesystem error")

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "agent_profile": "developer",
                },
            )

            assert response.status_code == 500
            assert "Failed to create terminal" in response.json()["detail"]


def _terminal_dict(**overrides: Dict) -> Dict:
    base = {
        "id": "abcd1234",
        "name": "test-window",
        "provider": "kiro_cli",
        "session_name": "test-session",
        "agent_profile": "developer",
        "caller_id": None,
        "allowed_tools": None,
        "shell_command": None,
        "group": None,
        "metadata": None,
        "status": "idle",
        "last_active": None,
    }
    base.update(overrides)
    return base


class TestCreateSessionWithGroupAndMetadata:
    """#432: POST /sessions accepts group/metadata in the JSON body."""

    def test_group_and_metadata_forwarded_to_session_service(self, client):
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(
                return_value=Terminal(**_terminal_dict(group=["tenant_1", "project_5"]))
            )

            response = client.post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
                json={"group": ["tenant_1", "project_5"], "metadata": {"task": "bootstrap"}},
            )

            assert response.status_code == 201
            assert response.json()["group"] == ["tenant_1", "project_5"]
            call_kwargs = mock_svc.create_session.call_args.kwargs
            assert call_kwargs["group"] == ["tenant_1", "project_5"]
            assert call_kwargs["metadata"] == {"task": "bootstrap"}

    def test_omitted_group_and_metadata_default_to_none(self, client):
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session = AsyncMock(return_value=Terminal(**_terminal_dict()))

            response = client.post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
            )

            assert response.status_code == 201
            call_kwargs = mock_svc.create_session.call_args.kwargs
            assert call_kwargs["group"] is None
            assert call_kwargs["metadata"] is None


class TestUpdateTerminalGroupEndpoint:
    """#432: PATCH /terminals/{id}/group."""

    def test_update_group_success(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1", "project_9"])

            response = client.patch(
                "/terminals/abcd1234/group", json={"group": ["tenant_1", "project_9"]}
            )

            assert response.status_code == 200
            assert response.json()["group"] == ["tenant_1", "project_9"]
            mock_svc.update_group.assert_called_once_with("abcd1234", ["tenant_1", "project_9"])

    def test_update_group_clears_with_empty_list(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=None)

            response = client.patch("/terminals/abcd1234/group", json={"group": []})

            assert response.status_code == 200
            mock_svc.update_group.assert_called_once_with("abcd1234", [])

    def test_update_group_terminal_not_found(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = False

            response = client.patch("/terminals/deadbeef/group", json={"group": ["tenant_1"]})

            assert response.status_code == 404

    def test_update_group_server_error(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.side_effect = Exception("db exploded")

            response = client.patch("/terminals/abcd1234/group", json={"group": ["tenant_1"]})

            assert response.status_code == 500
            assert "Failed to update terminal group" in response.json()["detail"]

    def test_update_group_omitted_field_rejected_not_treated_as_clear(self, client):
        """Copilot review, PR #433: an omitted ``group`` field must be
        rejected (422) rather than silently treated the same as an explicit
        ``null`` (which clears the group) -- a partial/empty body must never
        accidentally clear data."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.patch("/terminals/abcd1234/group", json={})

            assert response.status_code == 422
            mock_svc.update_group.assert_not_called()

    def test_update_group_explicit_null_still_clears(self, client):
        """The omitted-field fix must not break the pre-existing explicit-null
        clearing path."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=None)

            response = client.patch("/terminals/abcd1234/group", json={"group": None})

            assert response.status_code == 200
            mock_svc.update_group.assert_called_once_with("abcd1234", None)


class TestUpdateTerminalMetadataEndpoint:
    """#432: PATCH /terminals/{id}/metadata (also called by the update_metadata MCP tool)."""

    def test_update_metadata_success(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_metadata.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(metadata={"task": "writing tests"})

            response = client.patch(
                "/terminals/abcd1234/metadata", json={"metadata": {"task": "writing tests"}}
            )

            assert response.status_code == 200
            assert response.json()["metadata"] == {"task": "writing tests"}
            mock_svc.update_metadata.assert_called_once_with("abcd1234", {"task": "writing tests"})

    def test_update_metadata_terminal_not_found(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_metadata.return_value = False

            response = client.patch(
                "/terminals/deadbeef/metadata", json={"metadata": {"task": "x"}}
            )

            assert response.status_code == 404

    def test_update_metadata_omitted_field_rejected_not_treated_as_clear(self, client):
        """Copilot review, PR #433: same omitted-vs-null fix as ``group`` --
        an omitted ``metadata`` field must be rejected (422), not silently
        treated as an explicit clearing ``null``."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.patch("/terminals/abcd1234/metadata", json={})

            assert response.status_code == 422
            mock_svc.update_metadata.assert_not_called()

    def test_update_metadata_explicit_null_still_clears(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_metadata.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(metadata=None)

            response = client.patch("/terminals/abcd1234/metadata", json={"metadata": None})

            assert response.status_code == 200
            mock_svc.update_metadata.assert_called_once_with("abcd1234", None)


class TestListSiblingsEndpoint:
    """#432: GET /terminals/{id}/siblings.

    ``terminal_id`` in the URL is the caller's own resolved identity (the MCP
    ``list_siblings`` tool passes its own CAO_TERMINAL_ID here) -- this
    endpoint only ever compares against that terminal's own persisted group.
    """

    def test_list_siblings_success(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1"])
            mock_svc.list_siblings.return_value = [
                {"id": "sib-1", "group": ["tenant_1"], "metadata": {"task": "x"}}
            ]

            response = client.get("/terminals/abcd1234/siblings")

            assert response.status_code == 200
            assert response.json() == [
                {"id": "sib-1", "group": ["tenant_1"], "metadata": {"task": "x"}}
            ]
            mock_svc.list_siblings.assert_called_once_with(
                "abcd1234", depth=None, cross_session=False
            )

    def test_list_siblings_passes_depth_through(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1", "project_5"])
            mock_svc.list_siblings.return_value = []

            response = client.get("/terminals/abcd1234/siblings", params={"depth": 2})

            assert response.status_code == 200
            mock_svc.list_siblings.assert_called_once_with("abcd1234", depth=2, cross_session=False)

    def test_list_siblings_cross_session_defaults_to_false(self, client):
        """Issue #432 design discussion: session-scoped by default."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1"])
            mock_svc.list_siblings.return_value = []

            client.get("/terminals/abcd1234/siblings")

            mock_svc.list_siblings.assert_called_once_with(
                "abcd1234", depth=None, cross_session=False
            )

    def test_list_siblings_cross_session_true_is_forwarded(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1"])
            mock_svc.list_siblings.return_value = []

            response = client.get("/terminals/abcd1234/siblings", params={"cross_session": "true"})

            assert response.status_code == 200
            mock_svc.list_siblings.assert_called_once_with(
                "abcd1234", depth=None, cross_session=True
            )

    def test_list_siblings_depth_zero_rejected(self, client):
        """#432: depth can never be 0 (an unscoped, all-terminals query) --
        rejected at the API boundary rather than silently reinterpreted."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1"])

            response = client.get("/terminals/abcd1234/siblings", params={"depth": 0})

            assert response.status_code == 422
            mock_svc.list_siblings.assert_not_called()

    def test_list_siblings_negative_depth_rejected(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=["tenant_1"])

            response = client.get("/terminals/abcd1234/siblings", params={"depth": -1})

            assert response.status_code == 422
            mock_svc.list_siblings.assert_not_called()

    def test_list_siblings_terminal_not_found(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.side_effect = ValueError("Terminal 'deadbeef' not found")

            response = client.get("/terminals/deadbeef/siblings")

            assert response.status_code == 404
            mock_svc.list_siblings.assert_not_called()

    def test_list_siblings_no_group_returns_empty_not_error(self, client):
        """A terminal that exists but has no group set finds no siblings --
        this is a 200 with an empty list, not an error (#432)."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = _terminal_dict(group=None)
            mock_svc.list_siblings.return_value = []

            response = client.get("/terminals/abcd1234/siblings")

            assert response.status_code == 200
            assert response.json() == []


class TestGroupSizeCap:
    """call-me-ram, PR #433 review: group elements/count are agent-writable
    (via update_group) and must be capped -- an uncapped array lets a worker
    grow the terminals.group TEXT column arbitrarily, amplified into every
    sibling's list_siblings response."""

    def test_group_at_cap_accepted(self, client):
        group = [f"g{i}" for i in range(TERMINAL_GROUP_MAX_ELEMENTS)]
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=group)

            response = client.patch("/terminals/abcd1234/group", json={"group": group})

            assert response.status_code == 200
            mock_svc.update_group.assert_called_once_with("abcd1234", group)

    def test_group_over_element_count_cap_rejected(self, client):
        group = [f"g{i}" for i in range(TERMINAL_GROUP_MAX_ELEMENTS + 1)]
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.patch("/terminals/abcd1234/group", json={"group": group})

            assert response.status_code == 422
            mock_svc.update_group.assert_not_called()

    def test_group_element_at_length_cap_accepted(self, client):
        element = "x" * TERMINAL_GROUP_ELEMENT_MAX_LEN
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=[element])

            response = client.patch("/terminals/abcd1234/group", json={"group": [element]})

            assert response.status_code == 200

    def test_group_element_over_length_cap_rejected(self, client):
        element = "x" * (TERMINAL_GROUP_ELEMENT_MAX_LEN + 1)
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.patch("/terminals/abcd1234/group", json={"group": [element]})

            assert response.status_code == 422
            mock_svc.update_group.assert_not_called()

    def test_empty_group_not_subject_to_caps(self, client):
        """Clearing the group ([]/null) must never be rejected by the caps
        meant for growth, not clearing."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_group.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(group=None)

            response = client.patch("/terminals/abcd1234/group", json={"group": []})

            assert response.status_code == 200

    def test_create_session_group_over_cap_rejected_with_422(self, client):
        group = [f"g{i}" for i in range(TERMINAL_GROUP_MAX_ELEMENTS + 1)]
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
                json={"group": group},
            )

            assert response.status_code == 422
            mock_svc.create_session.assert_not_called()


class TestMetadataSizeCap:
    """call-me-ram, PR #433 review: metadata is a raw agent-writable
    Dict[str, Any] (via update_metadata) and must be capped by encoded
    bytes, following the WORKFLOW_MAX_SPEC_BYTES precedent."""

    def test_metadata_at_byte_cap_accepted(self, client):
        # Reserve room for the JSON envelope (quotes, braces, key) so the
        # encoded dict lands at (not under) the cap.
        padding = "x" * (TERMINAL_METADATA_MAX_BYTES - len('{"k": ""}'))
        metadata = {"k": padding}
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_metadata.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(metadata=metadata)

            response = client.patch("/terminals/abcd1234/metadata", json={"metadata": metadata})

            assert response.status_code == 200

    def test_metadata_over_byte_cap_rejected(self, client):
        padding = "x" * TERMINAL_METADATA_MAX_BYTES
        metadata = {"k": padding}
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.patch("/terminals/abcd1234/metadata", json={"metadata": metadata})

            assert response.status_code == 422
            mock_svc.update_metadata.assert_not_called()

    def test_empty_metadata_not_subject_to_cap(self, client):
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.update_metadata.return_value = True
            mock_svc.get_terminal.return_value = _terminal_dict(metadata=None)

            response = client.patch("/terminals/abcd1234/metadata", json={"metadata": {}})

            assert response.status_code == 200

    def test_create_session_metadata_over_cap_rejected_with_422(self, client):
        metadata = {"k": "x" * TERMINAL_METADATA_MAX_BYTES}
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.post(
                "/sessions",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
                json={"metadata": metadata},
            )

            assert response.status_code == 422
            mock_svc.create_session.assert_not_called()
