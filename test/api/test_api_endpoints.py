"""Tests for uncovered API endpoints in main.py.

Covers: health, agents/profiles, agents/providers, sessions CRUD,
terminals CRUD (create in session, list, get, input, output, delete),
flow_daemon, lifespan, and the main() entry point.
"""

import asyncio
from datetime import datetime
from unittest.mock import ANY, AsyncMock, MagicMock, Mock, call, patch

import pytest

from cli_agent_orchestrator.api.main import (
    app,
    flow_daemon,
    inbox_reconciliation_daemon,
    opencode_inbox_delivery_daemon,
)
from cli_agent_orchestrator.models.terminal import Terminal
from cli_agent_orchestrator.services.inbox_service import inbox_service
from cli_agent_orchestrator.utils.skills import SkillNameError

# ── Health endpoint ──────────────────────────────────────────────────


class TestHealthCheck:
    """Tests for GET /health endpoint."""

    def test_health_check_returns_ok(self, client):
        """GET /health returns status ok with component health."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "ok"
        assert data["service"] == "cli-agent-orchestrator"
        components = data["components"]
        assert components["cao"] == "ok"
        assert components["herdr"] in ("ok", "unavailable")
        assert components["claude"] in ("ok", "unavailable")

    def test_health_reports_terminal_backend_tmux(self, client):
        """GET /health reports terminal_backend matching the active backend (tmux)."""
        with patch("cli_agent_orchestrator.api.main.get_backend") as mock_backend:
            mock_backend.return_value = MagicMock(spec=[])  # not HerdrBackend
            response = client.get("/health")
        data = response.json()
        assert data["terminal_backend"] == "tmux"

    def test_health_reports_terminal_backend_herdr(self, client):
        """GET /health reports terminal_backend='herdr' when server uses HerdrBackend."""
        from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

        mock_herdr = MagicMock(spec=HerdrBackend)
        with patch("cli_agent_orchestrator.api.main.get_backend", return_value=mock_herdr):
            response = client.get("/health")
        data = response.json()
        assert data["terminal_backend"] == "herdr"


# ── Agent profiles endpoint ──────────────────────────────────────────


class TestAgentProfiles:
    """Tests for GET /agents/profiles endpoint."""

    def test_list_profiles_success(self, client):
        """GET /agents/profiles returns list of profiles."""
        mock_profiles = [
            {"name": "developer", "path": "/agents/developer"},
            {"name": "reviewer", "path": "/agents/reviewer"},
        ]
        with patch(
            "cli_agent_orchestrator.api.main.list_agent_profiles",
            create=True,
        ) as mock_fn:
            # The endpoint does a lazy import, so we need to patch at the import target
            with patch(
                "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
                return_value=mock_profiles,
            ):
                response = client.get("/agents/profiles")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2
        assert data[0]["name"] == "developer"

    def test_list_profiles_empty(self, client):
        """GET /agents/profiles returns empty list when none exist."""
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            return_value=[],
        ):
            response = client.get("/agents/profiles")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_profiles_server_error(self, client):
        """GET /agents/profiles returns 500 on internal error."""
        with patch(
            "cli_agent_orchestrator.utils.agent_profiles.list_agent_profiles",
            side_effect=Exception("Failed to read profiles"),
        ):
            response = client.get("/agents/profiles")

        assert response.status_code == 500
        assert "Failed to list agent profiles" in response.json()["detail"]


# ── Agent providers endpoint ─────────────────────────────────────────


class TestAgentProviders:
    """Tests for GET /agents/providers endpoint."""

    def test_list_providers_all_installed(self, client):
        """GET /agents/providers returns all providers as installed."""
        with patch("shutil.which", return_value="/usr/bin/dummy"):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 9
        names = [p["name"] for p in data]
        assert "kiro_cli" in names
        assert "claude_code" in names
        assert "codex" in names
        assert "hermes" in names
        assert "kimi_cli" in names
        assert "copilot_cli" in names
        assert "opencode_cli" in names
        assert "cursor_cli" in names
        assert "antigravity_cli" in names
        for p in data:
            assert p["installed"] is True

    def test_list_providers_none_installed(self, client):
        """GET /agents/providers returns all providers as not installed."""
        with patch("shutil.which", return_value=None):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        for p in data:
            assert p["installed"] is False

    def test_list_providers_mixed_installed(self, client):
        """GET /agents/providers returns mixed installation status."""

        def mock_which(binary):
            return "/usr/bin/kiro-cli" if binary == "kiro-cli" else None

        with patch("shutil.which", side_effect=mock_which):
            response = client.get("/agents/providers")

        assert response.status_code == 200
        data = response.json()
        providers_dict = {p["name"]: p for p in data}
        assert providers_dict["kiro_cli"]["installed"] is True
        assert providers_dict["claude_code"]["installed"] is False
        assert providers_dict["codex"]["installed"] is False
        assert providers_dict["kimi_cli"]["installed"] is False
        assert providers_dict["copilot_cli"]["installed"] is False
        assert providers_dict["opencode_cli"]["installed"] is False

    def test_list_providers_has_binary_field(self, client):
        """Each provider entry has correct binary name."""
        with patch("shutil.which", return_value=None):
            response = client.get("/agents/providers")

        data = response.json()
        providers_dict = {p["name"]: p for p in data}
        assert providers_dict["kiro_cli"]["binary"] == "kiro-cli"
        assert providers_dict["claude_code"]["binary"] == "claude"
        assert providers_dict["codex"]["binary"] == "codex"
        assert providers_dict["kimi_cli"]["binary"] == "kimi"
        assert providers_dict["copilot_cli"]["binary"] == "copilot"
        assert providers_dict["opencode_cli"]["binary"] == "opencode"
        assert providers_dict["antigravity_cli"]["binary"] == "agy"


# ── Skills endpoint ──────────────────────────────────────────────────


class TestGetSkillContent:
    """Tests for GET /skills/{name} endpoint."""

    def test_get_skill_returns_content(self, client):
        """GET /skills/{name} returns the skill body on success."""
        with patch(
            "cli_agent_orchestrator.api.main.load_skill_content",
            return_value="# Python Testing\n\nUse pytest.",
        ):
            response = client.get("/skills/python-testing")

        assert response.status_code == 200
        assert response.json() == {
            "name": "python-testing",
            "content": "# Python Testing\n\nUse pytest.",
        }

    def test_get_skill_returns_400_for_invalid_name(self, client):
        """GET /skills/{name} returns 400 for path traversal names."""
        with patch(
            "cli_agent_orchestrator.api.main.load_skill_content",
            side_effect=SkillNameError(
                "Invalid skill name '../secret': must not contain '/', '\\\\', or '..'"
            ),
        ):
            response = client.get("/skills/%2E%2E")

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid skill name: .."

    def test_get_skill_returns_404_for_missing_skill(self, client):
        """GET /skills/{name} returns 404 when the skill does not exist."""
        with patch(
            "cli_agent_orchestrator.api.main.load_skill_content",
            side_effect=FileNotFoundError("Skill folder does not exist"),
        ):
            response = client.get("/skills/missing-skill")

        assert response.status_code == 404
        assert response.json()["detail"] == "Skill not found: missing-skill"

    def test_get_skill_returns_500_for_parse_error(self, client):
        """GET /skills/{name} returns 500 for invalid skill file content."""
        with patch(
            "cli_agent_orchestrator.api.main.load_skill_content",
            side_effect=ValueError("Failed to parse skill file '/tmp/SKILL.md': bad yaml"),
        ):
            response = client.get("/skills/broken-skill")

        assert response.status_code == 500
        assert response.json()["detail"] == (
            "Failed to load skill: Failed to parse skill file '/tmp/SKILL.md': bad yaml"
        )

    def test_get_skill_returns_500_for_filesystem_error(self, client):
        """GET /skills/{name} returns 500 for unexpected filesystem errors."""
        with patch(
            "cli_agent_orchestrator.api.main.load_skill_content",
            side_effect=OSError("Permission denied"),
        ):
            response = client.get("/skills/python-testing")

        assert response.status_code == 500
        assert response.json()["detail"] == "Failed to load skill: Permission denied"


# ── Sessions CRUD ────────────────────────────────────────────────────


class TestCreateSession:
    """Tests for POST /sessions endpoint — success and error cases."""

    def test_create_session_success(self, client):
        """POST /sessions creates a session and returns 201."""
        mock_terminal = Terminal(
            id="abcd1234",
            name="test-window",
            session_name="test-session",
            provider="kiro_cli",
            agent_profile="developer",
        )
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            # The endpoint awaits session_service.create_session, so the patched
            # attribute must be an AsyncMock to return an awaitable.
            mock_svc.create_session = AsyncMock(return_value=mock_terminal)

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "abcd1234"
        assert data["provider"] == "kiro_cli"
        assert data["agent_profile"] == "developer"
        mock_svc.create_session.assert_called_once_with(
            provider="kiro_cli",
            agent_profile="developer",
            session_name=None,
            working_directory=None,
            allowed_tools=None,
            registry=ANY,
            env_vars=None,
        )

    def test_create_session_with_session_name(self, client):
        """POST /sessions with explicit session_name."""
        mock_terminal = Terminal(
            id="abcd1234",
            name="test-window",
            session_name="my-custom-session",
            provider="kiro_cli",
            agent_profile="developer",
        )
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            # The endpoint awaits session_service.create_session, so the patched
            # attribute must be an AsyncMock to return an awaitable.
            mock_svc.create_session = AsyncMock(return_value=mock_terminal)

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "session_name": "my-custom-session",
                },
            )

        assert response.status_code == 201
        call_kwargs = mock_svc.create_session.call_args.kwargs
        assert call_kwargs["session_name"] == "my-custom-session"
        assert call_kwargs["registry"] is not None

    def test_create_session_value_error(self, client):
        """POST /sessions returns 400 on ValueError."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session.side_effect = ValueError("Invalid provider")

            response = client.post(
                "/sessions",
                params={
                    "provider": "bad_provider",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 400
        assert "Invalid provider" in response.json()["detail"]

    def test_create_session_server_error(self, client):
        """POST /sessions returns 500 on unexpected error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.create_session.side_effect = Exception("TMux crashed")

            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 500
        assert "Failed to create session" in response.json()["detail"]

    @pytest.mark.parametrize(
        "bad_name",
        # NB: '-leading' is not in this set — terminal_service prepends the
        # SESSION_PREFIX 'cao-' so the effective name becomes 'cao--leading',
        # which is a valid tmux target (no leading dash). The boundary check
        # validates the prefixed value, so leading-dash inputs are accepted
        # here but rejected on path-param routes that have no prefixing.
        ["evil:name", "evil.name", "with space", "../escape", "name;rm"],
    )
    def test_create_session_rejects_unsafe_name(self, client, bad_name):
        """POST /sessions rejects session names that could break tmux target parsing."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "session_name": bad_name,
                },
            )

        assert response.status_code == 400
        assert "session_name" in response.json()["detail"]
        mock_svc.create_session.assert_not_called()

    def test_create_session_rejects_name_that_overflows_after_prefix(self, client):
        """A 64-char name (max valid) becomes 68 chars after the cao- prefix
        is prepended by terminal_service. The boundary check must catch this
        with a 400 instead of letting it slip through to a sink failure.
        """
        # 64 ascii chars — passes the validator on its own, but cao-prefixed
        # is 68 chars, exceeds the 64-char cap, must be rejected.
        long_name = "a" * 64
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "session_name": long_name,
                },
            )

        assert response.status_code == 400
        assert "session_name" in response.json()["detail"]
        mock_svc.create_session.assert_not_called()

    def test_create_session_accepts_already_prefixed_name(self, client):
        """An already-prefixed valid name should not be double-prefixed in
        the validation check.
        """
        # 60 chars after the cao- prefix → 64 total, exactly at the limit.
        prefixed = "cao-" + "b" * 60
        from cli_agent_orchestrator.models.terminal import Terminal as TerminalModel

        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            # The endpoint awaits session_service.create_session, so the patched
            # attribute must be an AsyncMock to return an awaitable.
            mock_svc.create_session = AsyncMock(
                return_value=TerminalModel(
                    id="abcd1234",
                    name="w",
                    session_name=prefixed,
                    provider="kiro_cli",
                    agent_profile="developer",
                )
            )
            response = client.post(
                "/sessions",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                    "session_name": prefixed,
                },
            )

        assert response.status_code == 201
        mock_svc.create_session.assert_called_once()

    @pytest.mark.parametrize(
        "bad_name",
        ["evil:name", "evil.name", "-leading", "with space"],
    )
    def test_get_session_rejects_unsafe_name(self, client, bad_name):
        """GET /sessions/{name} returns 400 for unsafe names (validation,
        not "not found")."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.get(f"/sessions/{bad_name}")

        assert response.status_code == 400
        assert "session_name" in response.json()["detail"]
        mock_svc.get_session.assert_not_called()

    @pytest.mark.parametrize(
        "bad_name",
        ["evil:name", "evil.name", "-leading", "with space"],
    )
    def test_delete_session_rejects_unsafe_name(self, client, bad_name):
        """DELETE /sessions/{name} returns 400 for unsafe names."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            response = client.delete(f"/sessions/{bad_name}")

        assert response.status_code == 400
        assert "session_name" in response.json()["detail"]
        mock_svc.delete_session.assert_not_called()

    @pytest.mark.parametrize(
        "bad_name",
        ["evil:name", "evil.name", "-leading", "with space"],
    )
    def test_create_terminal_rejects_unsafe_session_name(self, client, bad_name):
        """POST /sessions/{name}/terminals returns 400 for unsafe session names."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.post(
                f"/sessions/{bad_name}/terminals",
                params={"provider": "kiro_cli", "agent_profile": "developer"},
            )

        assert response.status_code == 400
        assert "session_name" in response.json()["detail"]
        mock_svc.create_terminal.assert_not_called()


class TestListSessions:
    """Tests for GET /sessions endpoint."""

    def test_list_sessions_success(self, client):
        """GET /sessions returns list of sessions."""
        mock_sessions = [
            {"id": "cao-session-1", "windows": 2},
            {"id": "cao-session-2", "windows": 1},
        ]
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.return_value = mock_sessions

            response = client.get("/sessions")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_list_sessions_empty(self, client):
        """GET /sessions returns empty list."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.return_value = []

            response = client.get("/sessions")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_sessions_server_error(self, client):
        """GET /sessions returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.list_sessions.side_effect = Exception("TMux not running")

            response = client.get("/sessions")

        assert response.status_code == 500
        assert "Failed to list sessions" in response.json()["detail"]


class TestGetSession:
    """Tests for GET /sessions/{session_name} endpoint."""

    def test_get_session_success(self, client):
        """GET /sessions/{name} returns session details."""
        mock_session = {
            "id": "test-session",
            "windows": [{"name": "window-1", "id": "abcd1234"}],
        }
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.return_value = mock_session

            response = client.get("/sessions/test-session")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "test-session"
        mock_svc.get_session.assert_called_once_with("test-session")

    def test_get_session_not_found(self, client):
        """GET /sessions/{name} returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.get("/sessions/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_get_session_server_error(self, client):
        """GET /sessions/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.get_session.side_effect = Exception("Unexpected error")

            response = client.get("/sessions/test-session")

        assert response.status_code == 500
        assert "Failed to get session" in response.json()["detail"]


class TestDeleteSession:
    """Tests for DELETE /sessions/{session_name} endpoint."""

    def test_delete_session_success(self, client):
        """DELETE /sessions/{name} deletes session and returns success."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.return_value = {
                "deleted": ["test-session"],
                "errors": [],
            }

            response = client.delete("/sessions/test-session")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["deleted"] == ["test-session"]
        mock_svc.delete_session.assert_called_once_with("test-session", registry=ANY)

    def test_delete_session_not_found(self, client):
        """DELETE /sessions/{name} returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.delete("/sessions/nonexistent")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_delete_session_server_error(self, client):
        """DELETE /sessions/{name} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.session_service") as mock_svc:
            mock_svc.delete_session.side_effect = Exception("TMux error")

            response = client.delete("/sessions/test-session")

        assert response.status_code == 500
        assert "Failed to delete session" in response.json()["detail"]


# ── Terminals in sessions ────────────────────────────────────────────


class TestCreateTerminalInSession:
    """Tests for POST /sessions/{session_name}/terminals endpoint."""

    def test_create_terminal_success(self, client):
        """POST /sessions/{name}/terminals creates terminal and returns 201."""
        mock_terminal = Terminal(
            id="abcd5678",
            name="test-window-2",
            session_name="test-session",
            provider="claude_code",
            agent_profile="reviewer",
        )
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            # The endpoint awaits terminal_service.create_terminal, so the
            # patched attribute must be an AsyncMock to return an awaitable.
            mock_svc.create_terminal = AsyncMock(return_value=mock_terminal)

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "claude_code",
                    "agent_profile": "reviewer",
                },
            )

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == "abcd5678"
        assert data["session_name"] == "test-session"
        call_kwargs = mock_svc.create_terminal.call_args.kwargs
        assert call_kwargs["session_name"] == "test-session"
        assert call_kwargs["new_session"] is False

    def test_create_terminal_session_not_found(self, client):
        """POST /sessions/{name}/terminals returns 404 for nonexistent session."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = ValueError("Session 'nonexistent' not found")

            response = client.post(
                "/sessions/nonexistent/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_create_terminal_server_error(self, client):
        """POST /sessions/{name}/terminals returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.create_terminal.side_effect = Exception("TMux error")

            response = client.post(
                "/sessions/test-session/terminals",
                params={
                    "provider": "kiro_cli",
                    "agent_profile": "developer",
                },
            )

        assert response.status_code == 500
        assert "Failed to create terminal" in response.json()["detail"]


class TestListTerminalsInSession:
    """Tests for GET /sessions/{session_name}/terminals endpoint."""

    def test_list_terminals_success(self, client):
        """GET /sessions/{name}/terminals returns terminal list."""
        mock_terminals = [
            {"id": "abcd1234", "tmux_session": "s1", "provider": "kiro_cli"},
            {"id": "abcd5678", "tmux_session": "s1", "provider": "claude_code"},
        ]
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=mock_terminals,
        ):
            response = client.get("/sessions/s1/terminals")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 2

    def test_list_terminals_empty(self, client):
        """GET /sessions/{name}/terminals returns empty list."""
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            return_value=[],
        ):
            response = client.get("/sessions/empty-session/terminals")

        assert response.status_code == 200
        assert response.json() == []

    def test_list_terminals_server_error(self, client):
        """GET /sessions/{name}/terminals returns 500 on error."""
        with patch(
            "cli_agent_orchestrator.clients.database.list_terminals_by_session",
            side_effect=Exception("DB error"),
        ):
            response = client.get("/sessions/s1/terminals")

        assert response.status_code == 500
        assert "Failed to list terminals" in response.json()["detail"]


# ── Individual terminal endpoints ────────────────────────────────────


class TestGetTerminal:
    """Tests for GET /terminals/{terminal_id} endpoint."""

    def test_get_terminal_success(self, client):
        """GET /terminals/{id} returns terminal details."""
        mock_terminal_dict = {
            "id": "abcd1234",
            "name": "test-window",
            "session_name": "test-session",
            "provider": "kiro_cli",
            "agent_profile": "developer",
        }
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.return_value = mock_terminal_dict

            response = client.get("/terminals/abcd1234")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == "abcd1234"
        assert data["provider"] == "kiro_cli"
        mock_svc.get_terminal.assert_called_once_with("abcd1234")

    def test_get_terminal_not_found(self, client):
        """GET /terminals/{id} returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.side_effect = ValueError("Terminal 'deadbeef' not found")

            response = client.get("/terminals/deadbeef")

        assert response.status_code == 404
        assert "not found" in response.json()["detail"]

    def test_get_terminal_server_error(self, client):
        """GET /terminals/{id} returns 500 on internal error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_terminal.side_effect = Exception("DB error")

            response = client.get("/terminals/abcd1234")

        assert response.status_code == 500
        assert "Failed to get terminal" in response.json()["detail"]

    def test_get_terminal_invalid_id_format(self, client):
        """GET /terminals/{id} returns 422 for invalid ID format."""
        response = client.get("/terminals/not-valid-hex")
        assert response.status_code == 422


class TestSendTerminalInput:
    """Tests for POST /terminals/{terminal_id}/input endpoint."""

    def test_send_input_success(self, client):
        """POST /terminals/{id}/input sends message successfully."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.return_value = True

            response = client.post(
                "/terminals/abcd1234/input",
                params={"message": "hello world"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_svc.send_input.assert_called_once_with(
            "abcd1234",
            "hello world",
            registry=ANY,
            sender_id=None,
            orchestration_type=None,
        )

    def test_send_input_with_orchestration_context(self, client):
        """POST /terminals/{id}/input forwards registry and orchestration metadata when provided."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.return_value = True

            response = client.post(
                "/terminals/abcd1234/input",
                params={
                    "message": "hello world",
                    "sender_id": "supervisor-1",
                    "orchestration_type": "assign",
                },
            )

        assert response.status_code == 200
        mock_svc.send_input.assert_called_once_with(
            "abcd1234",
            "hello world",
            registry=ANY,
            sender_id="supervisor-1",
            orchestration_type="assign",
        )

    def test_send_input_terminal_not_found(self, client):
        """POST /terminals/{id}/input returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.side_effect = ValueError("Terminal not found")

            response = client.post(
                "/terminals/deadbeef/input",
                params={"message": "hello"},
            )

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_send_input_blocked_returns_conflict(self, client):
        """POST /terminals/{id}/input returns 409 for protected interactive prompts."""
        from cli_agent_orchestrator.services.terminal_service import TerminalInputBlockedError

        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.TerminalInputBlockedError = TerminalInputBlockedError
            mock_svc.send_input.side_effect = TerminalInputBlockedError(
                "Terminal abcd1234 is waiting for a user answer"
            )

            response = client.post(
                "/terminals/abcd1234/input",
                params={"message": "new task", "orchestration_type": "assign"},
            )

        assert response.status_code == 409
        assert "waiting for a user answer" in response.json()["detail"]

    def test_send_input_server_error(self, client):
        """POST /terminals/{id}/input returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_input.side_effect = Exception("TMux send failed")

            response = client.post(
                "/terminals/abcd1234/input",
                params={"message": "hello"},
            )

        assert response.status_code == 500
        assert "Failed to send input" in response.json()["detail"]


class TestSendTerminalKey:
    """Tests for POST /terminals/{terminal_id}/key endpoint."""

    def test_send_key_success(self, client):
        """POST /terminals/{id}/key sends an allowed tmux key."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_special_key.return_value = True

            response = client.post("/terminals/abcd1234/key", params={"key": "Down"})

        assert response.status_code == 200
        assert response.json()["success"] is True
        mock_svc.send_special_key.assert_called_once_with("abcd1234", "Down")

    @pytest.mark.parametrize("key", ["", "C-C-C", "send-prefix", "C-;"])
    def test_send_key_rejects_malformed_key_name(self, client, key):
        """POST /terminals/{id}/key rejects malformed or unsupported key names."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            response = client.post("/terminals/abcd1234/key", params={"key": key})

        assert response.status_code == 400
        assert "Invalid tmux key name" in response.json()["detail"]
        mock_svc.send_special_key.assert_not_called()

    def test_send_key_terminal_not_found(self, client):
        """POST /terminals/{id}/key returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_special_key.side_effect = ValueError("Terminal not found")

            response = client.post("/terminals/deadbeef/key", params={"key": "Enter"})

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_send_key_server_error(self, client):
        """POST /terminals/{id}/key returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.send_special_key.side_effect = Exception("TMux send failed")

            response = client.post("/terminals/abcd1234/key", params={"key": "Enter"})

        assert response.status_code == 500
        assert "Failed to send key" in response.json()["detail"]


class TestGetTerminalOutput:
    """Tests for GET /terminals/{terminal_id}/output endpoint."""

    def test_get_output_full_mode(self, client):
        """GET /terminals/{id}/output returns full output by default."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.return_value = "Hello from terminal"

            response = client.get("/terminals/abcd1234/output")

        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "Hello from terminal"
        assert data["mode"] == "full"

    def test_get_output_last_mode(self, client):
        """GET /terminals/{id}/output with mode=last returns last response."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.return_value = "Last response"

            response = client.get("/terminals/abcd1234/output?mode=last")

        assert response.status_code == 200
        data = response.json()
        assert data["output"] == "Last response"
        assert data["mode"] == "last"

    def test_get_output_terminal_not_found(self, client):
        """GET /terminals/{id}/output returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.side_effect = ValueError("Terminal not found")

            response = client.get("/terminals/deadbeef/output")

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_get_output_server_error(self, client):
        """GET /terminals/{id}/output returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.get_output.side_effect = Exception("Read failed")

            response = client.get("/terminals/abcd1234/output")

        assert response.status_code == 500
        assert "Failed to get output" in response.json()["detail"]


class TestDeleteTerminal:
    """Tests for DELETE /terminals/{terminal_id} endpoint."""

    def test_delete_terminal_success(self, client):
        """DELETE /terminals/{id} deletes terminal successfully."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.return_value = True

            response = client.delete("/terminals/abcd1234")

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        mock_svc.delete_terminal.assert_called_once_with("abcd1234", registry=ANY)

    def test_delete_terminal_not_found(self, client):
        """DELETE /terminals/{id} returns 404 for nonexistent terminal."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = ValueError("Terminal not found")

            response = client.delete("/terminals/deadbeef")

        assert response.status_code == 404
        assert "Terminal not found" in response.json()["detail"]

    def test_delete_terminal_server_error(self, client):
        """DELETE /terminals/{id} returns 500 on error."""
        with patch("cli_agent_orchestrator.api.main.terminal_service") as mock_svc:
            mock_svc.delete_terminal.side_effect = Exception("Cleanup failed")

            response = client.delete("/terminals/abcd1234")

        assert response.status_code == 500
        assert "Failed to delete terminal" in response.json()["detail"]


# ── flow_daemon ──────────────────────────────────────────────────────


class TestFlowDaemon:
    """Tests for the flow_daemon() background task."""

    @pytest.mark.asyncio
    async def test_flow_daemon_executes_flows(self):
        """flow_daemon fetches and executes due flows."""
        mock_flow = MagicMock()
        mock_flow.name = "test-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.return_value = True

            # Run one iteration then cancel
            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.get_flows_to_run.assert_called_once()
            mock_svc.execute_flow.assert_called_once_with("test-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_handles_execute_error(self):
        """flow_daemon handles errors from execute_flow gracefully."""
        mock_flow = MagicMock()
        mock_flow.name = "fail-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.side_effect = Exception("Execution failed")

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            # Should still have attempted execution
            mock_svc.execute_flow.assert_called_once_with("fail-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_handles_get_flows_error(self):
        """flow_daemon handles errors from get_flows_to_run gracefully."""
        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.side_effect = Exception("DB error")

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.get_flows_to_run.assert_called_once()

    @pytest.mark.asyncio
    async def test_flow_daemon_skipped_flow(self):
        """flow_daemon logs when execute returns False (skipped)."""
        mock_flow = MagicMock()
        mock_flow.name = "skipped-flow"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [mock_flow]
            mock_svc.execute_flow.return_value = False

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            mock_svc.execute_flow.assert_called_once_with("skipped-flow")

    @pytest.mark.asyncio
    async def test_flow_daemon_multiple_flows(self):
        """flow_daemon processes multiple flows in one iteration."""
        flow1 = MagicMock()
        flow1.name = "flow-1"
        flow2 = MagicMock()
        flow2.name = "flow-2"

        with patch("cli_agent_orchestrator.api.main.flow_service") as mock_svc:
            mock_svc.get_flows_to_run.return_value = [flow1, flow2]
            mock_svc.execute_flow.return_value = True

            with patch("asyncio.sleep", side_effect=asyncio.CancelledError):
                with pytest.raises(asyncio.CancelledError):
                    await flow_daemon()

            assert mock_svc.execute_flow.call_count == 2


class TestOpenCodeInboxDeliveryDaemon:
    """Tests for the OpenCode inbox delivery poller task."""

    @pytest.mark.asyncio
    async def test_poller_runs_one_iteration_then_cancels(self):
        """Poller sleeps, runs the sync poll in a thread, then handles cancellation."""
        sleep_calls = 0
        registry = MagicMock()
        mock_to_thread = AsyncMock()

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        with (
            patch("asyncio.sleep", new=fake_sleep),
            patch("asyncio.to_thread", mock_to_thread),
        ):
            with pytest.raises(asyncio.CancelledError):
                await opencode_inbox_delivery_daemon(registry)

        mock_to_thread.assert_awaited_once()
        assert mock_to_thread.await_args.args[1] is registry


class TestInboxReconciliationDaemon:
    """Tests for the provider-agnostic inbox reconciliation sweep (issue #131)."""

    @pytest.mark.asyncio
    async def test_sweep_runs_one_iteration_then_cancels(self):
        """Daemon sleeps, runs the sync sweep in a thread, then handles cancellation."""
        sleep_calls = 0
        registry = MagicMock()
        mock_to_thread = AsyncMock()

        async def fake_sleep(_seconds):
            nonlocal sleep_calls
            sleep_calls += 1
            if sleep_calls > 1:
                raise asyncio.CancelledError

        with (
            patch("asyncio.sleep", new=fake_sleep),
            patch("asyncio.to_thread", mock_to_thread),
        ):
            with pytest.raises(asyncio.CancelledError):
                await inbox_reconciliation_daemon(registry)

        mock_to_thread.assert_awaited_once()
        # The sweep, not some other sync function, must be the dispatched work.
        # reconcile_orphaned_messages is a bound method on the singleton now, so a
        # fresh attribute access is a distinct object — compare by value, not id.
        assert mock_to_thread.await_args.args[0] == inbox_service.reconcile_orphaned_messages
        assert mock_to_thread.await_args.args[1] is registry


# ── lifespan ─────────────────────────────────────────────────────────


class TestLifespan:
    """Tests for the lifespan() context manager."""

    @pytest.mark.asyncio
    async def test_lifespan_startup_and_shutdown(self):
        """lifespan starts the event-bus consumers on entry, cleans up on exit.

        The watchdog PollingObserver inbox watcher was replaced by event-bus
        consumers: startup registers the running loop with the event bus
        (``bus.set_loop``) and spins up StatusMonitor/LogWriter/InboxService as
        background tasks (plus the flow daemon and OpenCode inbox poller). On
        exit the plugin registry is torn down.
        """
        from cli_agent_orchestrator.api import main as main_module
        from cli_agent_orchestrator.api.main import lifespan

        async def fake_daemon():
            await asyncio.sleep(3600)

        async def fake_registry_daemon(_registry):
            await asyncio.sleep(3600)

        async def quick_return():
            return None

        async def never_returns():
            await asyncio.sleep(3600)

        mock_load = AsyncMock()
        mock_teardown = AsyncMock()

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch("cli_agent_orchestrator.api.main.cleanup_expired_memories", quick_return),
            patch("cli_agent_orchestrator.api.main.flow_daemon", fake_daemon),
            patch(
                "cli_agent_orchestrator.api.main.opencode_inbox_delivery_daemon",
                fake_registry_daemon,
            ),
            patch("cli_agent_orchestrator.api.main.bus") as mock_bus,
            patch.object(
                main_module.status_monitor, "run", new=AsyncMock(side_effect=never_returns)
            ),
            patch.object(main_module.log_writer, "run", new=AsyncMock(side_effect=never_returns)),
            patch.object(
                main_module.inbox_service, "run", new=AsyncMock(side_effect=never_returns)
            ),
            patch("cli_agent_orchestrator.plugins.PluginRegistry.load", mock_load),
            patch("cli_agent_orchestrator.plugins.PluginRegistry.teardown", mock_teardown),
        ):
            async with lifespan(app):
                # Inside the lifespan — startup completed.
                # The registry was loaded and stored on app state.
                mock_load.assert_awaited_once()
                assert app.state.plugin_registry is not None
                # The event loop was registered with the event bus so the
                # thread-safe publishers can reach the asyncio consumers.
                mock_bus.set_loop.assert_called_once()
                loop_arg = mock_bus.set_loop.call_args.args[0]
                assert loop_arg is asyncio.get_running_loop()

            # After exit — shutdown tears down the plugin registry.
            mock_teardown.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_lifespan_cancels_inbox_reconciliation_on_shutdown(self):
        """The reconciliation sweep task is cancelled when the server stops (issue #131).

        The watchdog PollingObserver is gone in the event-driven model, so the
        event-bus consumers are stubbed (as in the startup/shutdown test) and the
        reconciliation daemon is replaced with one that records its cancellation.
        """
        from cli_agent_orchestrator.api import main as main_module
        from cli_agent_orchestrator.api.main import lifespan

        reconcile_cancelled = {"value": False}

        async def fake_daemon():
            await asyncio.sleep(3600)

        async def fake_registry_daemon(_registry):
            await asyncio.sleep(3600)

        async def never_returns():
            await asyncio.sleep(3600)

        async def quick_return():
            return None

        async def fake_reconcile(_registry):
            try:
                await asyncio.sleep(3600)
            except asyncio.CancelledError:
                reconcile_cancelled["value"] = True
                raise

        with (
            patch("cli_agent_orchestrator.api.main.setup_logging"),
            patch("cli_agent_orchestrator.api.main.init_db"),
            patch("cli_agent_orchestrator.api.main.cleanup_old_data"),
            patch("cli_agent_orchestrator.api.main.cleanup_expired_memories", quick_return),
            patch("cli_agent_orchestrator.api.main.flow_daemon", fake_daemon),
            patch(
                "cli_agent_orchestrator.api.main.opencode_inbox_delivery_daemon",
                fake_registry_daemon,
            ),
            patch(
                "cli_agent_orchestrator.api.main.inbox_reconciliation_daemon",
                fake_reconcile,
            ),
            patch("cli_agent_orchestrator.api.main.bus"),
            patch.object(
                main_module.status_monitor, "run", new=AsyncMock(side_effect=never_returns)
            ),
            patch.object(main_module.log_writer, "run", new=AsyncMock(side_effect=never_returns)),
            patch.object(
                main_module.inbox_service, "run", new=AsyncMock(side_effect=never_returns)
            ),
            patch("cli_agent_orchestrator.plugins.PluginRegistry.load", new=AsyncMock()),
            patch("cli_agent_orchestrator.plugins.PluginRegistry.teardown", new=AsyncMock()),
        ):
            async with lifespan(app):
                pass

        assert reconcile_cancelled["value"] is True


# ── main() entry point ───────────────────────────────────────────────


class TestMainEntryPoint:
    """Tests for the main() CLI entry point."""

    def test_main_default_args(self):
        """main() runs uvicorn with default host/port."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run") as mock_uvicorn,
        ):
            mock_args.return_value = MagicMock(agents_dir=None, host=None, port=None, terminal=None)

            from cli_agent_orchestrator.api.main import main

            main()

            mock_uvicorn.assert_called_once()
            call_kwargs = mock_uvicorn.call_args
            # Should use SERVER_HOST and SERVER_PORT defaults
            assert call_kwargs[0][0] is app

    def test_main_custom_host_port(self):
        """main() uses custom host and port from args."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run") as mock_uvicorn,
        ):
            mock_args.return_value = MagicMock(
                agents_dir=None, host="0.0.0.0", port=9999, terminal=None
            )

            from cli_agent_orchestrator.api.main import main

            main()

            mock_uvicorn.assert_called_once()
            call_kwargs = mock_uvicorn.call_args.kwargs
            assert mock_uvicorn.call_args.args[0] is app
            assert call_kwargs["host"] == "0.0.0.0"
            assert call_kwargs["port"] == 9999
            assert call_kwargs["proxy_headers"] is True
            assert "forwarded_allow_ips" in call_kwargs

    def test_main_with_agents_dir(self):
        """main() sets KIRO_AGENTS_DIR when --agents-dir is provided."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run"),
            patch("cli_agent_orchestrator.constants.KIRO_AGENTS_DIR") as _,
        ):
            mock_args.return_value = MagicMock(
                agents_dir="/custom/agents", host=None, port=None, terminal=None
            )

            from cli_agent_orchestrator.api.main import main

            main()
            # No assertion needed beyond no exception — the code path is covered

    def test_main_extends_cors_for_custom_host_port(self):
        """``main()`` must call ``add_local_cors_origins`` with the resolved
        host/port *before* uvicorn starts, otherwise the same-host browser
        access promised by issue #151 silently regresses. ``uvicorn.run`` is
        blocking in real execution, so any mutation of ``CORS_ORIGINS`` after
        it would be unreachable on first request — the order assertion below
        guards against an accidental reorder of those two lines."""
        parent = Mock()
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run") as mock_uvicorn,
            patch("cli_agent_orchestrator.api.main.add_local_cors_origins") as mock_add,
        ):
            parent.attach_mock(mock_add, "add_cors")
            parent.attach_mock(mock_uvicorn, "uvicorn_run")
            mock_args.return_value = MagicMock(
                agents_dir=None, host="0.0.0.0", port=9999, terminal=None
            )

            from cli_agent_orchestrator.api.main import main

            main()

            assert parent.mock_calls == [
                call.add_cors("0.0.0.0", 9999),
                call.uvicorn_run(
                    app,
                    host="0.0.0.0",
                    port=9999,
                    proxy_headers=True,
                    forwarded_allow_ips=parent.uvicorn_run.call_args.kwargs["forwarded_allow_ips"],
                ),
            ]

    def test_main_terminal_flag_overrides_backend(self):
        """--terminal sets the backend via the factory before the server starts."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run"),
            patch("cli_agent_orchestrator.backends.factory.BackendFactory.create") as mock_create,
            patch("cli_agent_orchestrator.backends.registry.set_backend") as mock_set,
        ):
            mock_args.return_value = MagicMock(
                agents_dir=None, host=None, port=None, terminal="herdr"
            )

            from cli_agent_orchestrator.api.main import main

            main()

            mock_create.assert_called_once_with(backend_override="herdr")
            mock_set.assert_called_once_with(mock_create.return_value)

    def test_main_no_terminal_flag_leaves_backend_lazy(self):
        """Without --terminal, main() does not eagerly set the backend."""
        with (
            patch("argparse.ArgumentParser.parse_args") as mock_args,
            patch("uvicorn.run"),
            patch("cli_agent_orchestrator.backends.registry.set_backend") as mock_set,
        ):
            mock_args.return_value = MagicMock(agents_dir=None, host=None, port=None, terminal=None)

            from cli_agent_orchestrator.api.main import main

            main()

            mock_set.assert_not_called()
