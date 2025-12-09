"""Tests for parent terminal tracking and reply functionality."""

import os
from unittest.mock import ANY, MagicMock, patch

import pytest


class TestTmuxParentEnvironment:
    """Test CAO_PARENT_TERMINAL_ID environment variable setting."""

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_session_with_parent_id(self, mock_server_class):
        """Test create_session sets CAO_PARENT_TERMINAL_ID."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_session(
            session_name="test-session",
            window_name="test-window",
            terminal_id="child123",
            parent_id="parent12",
        )

        # Verify environment was passed with both terminal IDs
        call_kwargs = mock_server.new_session.call_args.kwargs
        assert "environment" in call_kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "child123"
        assert call_kwargs["environment"]["CAO_PARENT_TERMINAL_ID"] == "parent12"

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_session_without_parent_id(self, mock_server_class):
        """Test create_session without parent_id doesn't set CAO_PARENT_TERMINAL_ID."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_session(
            session_name="test-session",
            window_name="test-window",
            terminal_id="root1234",
        )

        # Verify CAO_PARENT_TERMINAL_ID is not set
        call_kwargs = mock_server.new_session.call_args.kwargs
        assert "environment" in call_kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "root1234"
        assert "CAO_PARENT_TERMINAL_ID" not in call_kwargs["environment"]

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_window_with_parent_id(self, mock_server_class):
        """Test create_window sets CAO_PARENT_TERMINAL_ID."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.new_window.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_window(
            session_name="test-session",
            window_name="test-window",
            terminal_id="child123",
            parent_id="parent12",
        )

        # Verify environment was passed with both terminal IDs
        call_kwargs = mock_session.new_window.call_args.kwargs
        assert "environment" in call_kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "child123"
        assert call_kwargs["environment"]["CAO_PARENT_TERMINAL_ID"] == "parent12"

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_window_without_parent_id(self, mock_server_class):
        """Test create_window without parent_id doesn't set CAO_PARENT_TERMINAL_ID."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.new_window.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_window(
            session_name="test-session",
            window_name="test-window",
            terminal_id="root1234",
        )

        # Verify CAO_PARENT_TERMINAL_ID is not set
        call_kwargs = mock_session.new_window.call_args.kwargs
        assert "environment" in call_kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "root1234"
        assert "CAO_PARENT_TERMINAL_ID" not in call_kwargs["environment"]

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_session_with_extra_env(self, mock_server_class):
        """Test create_session with extra environment variables."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.windows = [mock_window]
        mock_server.new_session.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_session(
            session_name="test-session",
            window_name="test-window",
            terminal_id="child123",
            parent_id="parent12",
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        # Verify all environment variables are present
        call_kwargs = mock_server.new_session.call_args.kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "child123"
        assert call_kwargs["environment"]["CAO_PARENT_TERMINAL_ID"] == "parent12"
        assert call_kwargs["environment"]["CUSTOM_VAR"] == "custom_value"

    @patch("cli_agent_orchestrator.clients.tmux.libtmux.Server")
    def test_create_window_with_extra_env(self, mock_server_class):
        """Test create_window with extra environment variables."""
        from cli_agent_orchestrator.clients.tmux import TmuxClient

        mock_server = MagicMock()
        mock_session = MagicMock()
        mock_window = MagicMock()
        mock_window.name = "test-window"
        mock_session.new_window.return_value = mock_window
        mock_server.sessions.get.return_value = mock_session
        mock_server_class.return_value = mock_server

        client = TmuxClient()
        client.create_window(
            session_name="test-session",
            window_name="test-window",
            terminal_id="child123",
            parent_id="parent12",
            extra_env={"CUSTOM_VAR": "custom_value"},
        )

        # Verify all environment variables are present
        call_kwargs = mock_session.new_window.call_args.kwargs
        assert call_kwargs["environment"]["CAO_TERMINAL_ID"] == "child123"
        assert call_kwargs["environment"]["CAO_PARENT_TERMINAL_ID"] == "parent12"
        assert call_kwargs["environment"]["CUSTOM_VAR"] == "custom_value"


class TestMcpReplyTool:
    """Test the reply() MCP tool."""

    @pytest.mark.asyncio
    async def test_reply_with_parent_id_set(self):
        """Test reply() succeeds when CAO_PARENT_TERMINAL_ID is set."""
        from cli_agent_orchestrator.mcp_server.server import reply

        # Access the underlying function via .fn
        reply_fn = reply.fn

        with patch.dict(
            os.environ, {"CAO_PARENT_TERMINAL_ID": "parent12", "CAO_TERMINAL_ID": "child123"}
        ):
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_to_inbox"
            ) as mock_send:
                mock_send.return_value = {"success": True, "message_id": 1}

                result = await reply_fn(message="Task completed successfully")

                mock_send.assert_called_once_with("parent12", "Task completed successfully")
                assert result["success"] is True

    @pytest.mark.asyncio
    async def test_reply_without_parent_id(self):
        """Test reply() returns error when CAO_PARENT_TERMINAL_ID is not set."""
        from cli_agent_orchestrator.mcp_server.server import reply

        reply_fn = reply.fn

        # Create a clean env without CAO_PARENT_TERMINAL_ID
        clean_env = {k: v for k, v in os.environ.items() if k != "CAO_PARENT_TERMINAL_ID"}

        with patch.dict(os.environ, clean_env, clear=True):
            result = await reply_fn(message="Task completed")

            assert result["success"] is False
            assert "No parent terminal" in result["error"]

    @pytest.mark.asyncio
    async def test_reply_handles_send_error(self):
        """Test reply() handles errors from _send_to_inbox."""
        from cli_agent_orchestrator.mcp_server.server import reply

        reply_fn = reply.fn

        with patch.dict(
            os.environ, {"CAO_PARENT_TERMINAL_ID": "parent12", "CAO_TERMINAL_ID": "child123"}
        ):
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_to_inbox"
            ) as mock_send:
                mock_send.side_effect = Exception("Network error")

                result = await reply_fn(message="Task completed")

                assert result["success"] is False
                assert "Network error" in result["error"]


class TestMcpAssignWithProvider:
    """Test the assign() MCP tool with provider override."""

    @pytest.mark.asyncio
    async def test_assign_with_provider_override(self):
        """Test assign() with explicit provider override."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_direct_input"
            ) as mock_send:
                mock_create.return_value = ("child123", "q_cli")

                result = await assign_fn(
                    agent_profile="developer",
                    message="Implement feature X",
                    provider="q_cli",
                )

                mock_create.assert_called_once_with("developer", "q_cli", ANY, ANY)
                assert result["success"] is True
                assert result["provider"] == "q_cli"

    @pytest.mark.asyncio
    async def test_assign_inherits_provider(self):
        """Test assign() inherits provider when not specified."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_direct_input"
            ) as mock_send:
                mock_create.return_value = ("child123", "claude_code")

                result = await assign_fn(
                    agent_profile="developer",
                    message="Implement feature X",
                    provider=None,  # Explicitly pass None to test default behavior
                )

                mock_create.assert_called_once_with("developer", None, ANY, ANY)
                assert result["success"] is True

    @pytest.mark.asyncio
    async def test_assign_returns_terminal_id(self):
        """Test assign() returns the created terminal ID."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_direct_input"
            ) as mock_send:
                mock_create.return_value = ("abc12345", "claude_code")

                result = await assign_fn(
                    agent_profile="developer",
                    message="Test task",
                )

                assert result["terminal_id"] == "abc12345"

    @pytest.mark.asyncio
    async def test_assign_handles_creation_error(self):
        """Test assign() handles terminal creation errors."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            mock_create.side_effect = Exception("Session not found")

            result = await assign_fn(
                agent_profile="developer",
                message="Test task",
            )

            assert result["success"] is False
            assert "Session not found" in result["message"]


class TestMcpHandoffWithProvider:
    """Test the handoff() MCP tool with provider override."""

    @pytest.mark.asyncio
    async def test_handoff_with_provider_override(self):
        """Test handoff() with explicit provider override."""
        from cli_agent_orchestrator.mcp_server.server import handoff

        handoff_fn = handoff.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server.wait_until_terminal_status"
            ) as mock_wait:
                with patch(
                    "cli_agent_orchestrator.mcp_server.server._send_direct_input"
                ) as mock_send:
                    with patch(
                        "cli_agent_orchestrator.mcp_server.server.requests"
                    ) as mock_requests:
                        with patch(
                            "cli_agent_orchestrator.mcp_server.server.asyncio.sleep"
                        ) as mock_sleep:
                            mock_create.return_value = ("child123", "q_cli")
                            mock_wait.return_value = True

                            mock_output_response = MagicMock()
                            mock_output_response.json.return_value = {"output": "Task done"}
                            mock_exit_response = MagicMock()

                            mock_requests.get.return_value = mock_output_response
                            mock_requests.post.return_value = mock_exit_response

                            result = await handoff_fn(
                                agent_profile="developer",
                                message="Implement feature X",
                                provider="q_cli",
                            )

                            mock_create.assert_called_once_with("developer", "q_cli", ANY, ANY)
                            assert result.success is True
                            assert "q_cli" in result.message


class TestTerminalModelParentId:
    """Test Terminal model includes parent_id."""

    def test_terminal_model_has_parent_id_field(self):
        """Test Terminal model includes parent_id field."""
        from cli_agent_orchestrator.models.terminal import Terminal

        terminal = Terminal(
            id="child123",
            name="test-window",
            provider="claude_code",
            session_name="test-session",
            agent_profile="developer",
            parent_id="parent12",
        )

        assert terminal.parent_id == "parent12"

    def test_terminal_model_parent_id_optional(self):
        """Test Terminal model allows None for parent_id."""
        from cli_agent_orchestrator.models.terminal import Terminal

        terminal = Terminal(
            id="root1234",
            name="test-window",
            provider="claude_code",
            session_name="test-session",
            agent_profile="developer",
        )

        assert terminal.parent_id is None


class TestCreateTerminalWithParentId:
    """Test _create_terminal passes parent_id to API."""

    def test_create_terminal_passes_parent_id(self):
        """Test _create_terminal includes parent_id in API call."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                # Mock the GET call to get terminal metadata
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "claude_code",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                # Mock the POST call to create terminal
                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                terminal_id, provider = _create_terminal("developer")

                # Verify parent_id was passed in the POST call
                mock_requests.post.assert_called_once()
                call_kwargs = mock_requests.post.call_args.kwargs
                assert call_kwargs["params"]["parent_id"] == "parent12"

    def test_create_terminal_no_parent_for_root(self):
        """Test _create_terminal doesn't set parent_id for root terminal."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        # Clear CAO_TERMINAL_ID to simulate root terminal creation
        clean_env = {k: v for k, v in os.environ.items() if k != "CAO_TERMINAL_ID"}

        with patch.dict(os.environ, clean_env, clear=True):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                # Mock the POST call to create session
                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "root1234"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.post.return_value = mock_post_response

                terminal_id, provider = _create_terminal("developer")

                # Verify parent_id was NOT passed in the POST call
                mock_requests.post.assert_called_once()
                call_kwargs = mock_requests.post.call_args.kwargs
                assert "parent_id" not in call_kwargs["params"]

    def test_create_terminal_with_provider_override(self):
        """Test _create_terminal uses provider override."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                # Mock the GET call
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "claude_code",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                # Mock the POST call
                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                # Call with provider override
                terminal_id, provider = _create_terminal("developer", provider_override="q_cli")

                # Verify provider override was used
                assert provider == "q_cli"
                call_kwargs = mock_requests.post.call_args.kwargs
                assert call_kwargs["params"]["provider"] == "q_cli"

    def test_create_terminal_inherits_provider_without_override(self):
        """Test _create_terminal inherits provider from parent when no override."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                # Mock the GET call - parent uses q_cli
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "q_cli",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                # Mock the POST call
                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                # Call without provider override
                terminal_id, provider = _create_terminal("developer")

                # Verify inherited provider was used
                assert provider == "q_cli"
                call_kwargs = mock_requests.post.call_args.kwargs
                assert call_kwargs["params"]["provider"] == "q_cli"


class TestProviderArgsPassthrough:
    """Test provider_args and no_profile passthrough."""

    def test_create_terminal_with_provider_args(self):
        """Test _create_terminal passes provider_args to API."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "claude_code",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                _create_terminal(
                    "developer",
                    provider_args="--dangerously-skip-permissions --verbose",
                )

                call_kwargs = mock_requests.post.call_args.kwargs
                assert call_kwargs["params"]["provider_args"] == "--dangerously-skip-permissions --verbose"

    def test_create_terminal_with_no_profile(self):
        """Test _create_terminal passes no_profile to API."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "claude_code",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                _create_terminal("developer", no_profile=True)

                call_kwargs = mock_requests.post.call_args.kwargs
                assert call_kwargs["params"]["no_profile"] == "true"

    def test_create_terminal_without_no_profile(self):
        """Test _create_terminal doesn't pass no_profile when False."""
        from cli_agent_orchestrator.mcp_server.server import _create_terminal

        with patch.dict(os.environ, {"CAO_TERMINAL_ID": "parent12"}):
            with patch("cli_agent_orchestrator.mcp_server.server.requests") as mock_requests:
                mock_get_response = MagicMock()
                mock_get_response.json.return_value = {
                    "provider": "claude_code",
                    "session_name": "test-session",
                }
                mock_get_response.raise_for_status.return_value = None

                mock_post_response = MagicMock()
                mock_post_response.json.return_value = {"id": "child123"}
                mock_post_response.raise_for_status.return_value = None

                mock_requests.get.return_value = mock_get_response
                mock_requests.post.return_value = mock_post_response

                _create_terminal("developer", no_profile=False)

                call_kwargs = mock_requests.post.call_args.kwargs
                assert "no_profile" not in call_kwargs["params"]

    @pytest.mark.asyncio
    async def test_assign_with_provider_args(self):
        """Test assign() passes provider_args to _create_terminal."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_direct_input"
            ) as mock_send:
                mock_create.return_value = ("child123", "claude_code")

                await assign_fn(
                    agent_profile="developer",
                    message="Test task",
                    provider_args="--dangerously-skip-permissions",
                )

                # Verify provider_args was passed
                call_args = mock_create.call_args
                assert call_args[0][2] == "--dangerously-skip-permissions"

    @pytest.mark.asyncio
    async def test_assign_with_no_profile(self):
        """Test assign() passes no_profile to _create_terminal."""
        from cli_agent_orchestrator.mcp_server.server import assign

        assign_fn = assign.fn

        with patch(
            "cli_agent_orchestrator.mcp_server.server._create_terminal"
        ) as mock_create:
            with patch(
                "cli_agent_orchestrator.mcp_server.server._send_direct_input"
            ) as mock_send:
                mock_create.return_value = ("child123", "claude_code")

                await assign_fn(
                    agent_profile="developer",
                    message="Test task",
                    no_profile=True,
                )

                # Verify no_profile was passed as True
                call_args = mock_create.call_args
                assert call_args[0][3] is True


class TestProviderBuildCommand:
    """Test provider command building with provider_args."""

    def test_claude_code_build_command_with_provider_args(self):
        """Test ClaudeCodeProvider builds command with provider_args."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        provider = ClaudeCodeProvider(
            terminal_id="test123",
            session_name="test-session",
            window_name="test-window",
            agent_profile="developer",
            provider_args="--dangerously-skip-permissions --verbose",
        )
        command = provider._build_claude_command()

        # Verify provider args are included
        assert "--dangerously-skip-permissions" in command
        assert "--verbose" in command

    def test_claude_code_build_command_with_no_profile(self):
        """Test ClaudeCodeProvider skips profile when CAO_NO_PROFILE=1."""
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

        with patch.dict(os.environ, {"CAO_NO_PROFILE": "1"}):
            with patch(
                "cli_agent_orchestrator.providers.claude_code.load_agent_profile"
            ) as mock_load:
                provider = ClaudeCodeProvider(
                    terminal_id="test123",
                    session_name="test-session",
                    window_name="test-window",
                    agent_profile="developer",
                )
                command = provider._build_claude_command()

                # Verify profile was NOT loaded
                mock_load.assert_not_called()
                # Command should just be ["claude"]
                assert command == ["claude"]

    def test_q_cli_build_command_with_provider_args(self):
        """Test QCliProvider builds command with provider_args from env."""
        from cli_agent_orchestrator.providers.q_cli import QCliProvider

        with patch.dict(os.environ, {"CAO_PROVIDER_ARGS": "--verbose --debug"}):
            provider = QCliProvider(
                terminal_id="test123",
                session_name="test-session",
                window_name="test-window",
                agent_profile="developer",
            )
            command = provider._build_q_command()

            # Verify provider args are included before --agent
            assert "--verbose" in command
            assert "--debug" in command
            # --agent should be at the end
            assert command.endswith("--agent developer")

    def test_kiro_cli_build_command_with_provider_args(self):
        """Test KiroCliProvider builds command with provider_args from env."""
        from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider

        with patch.dict(os.environ, {"CAO_PROVIDER_ARGS": "--verbose --debug"}):
            provider = KiroCliProvider(
                terminal_id="test123",
                session_name="test-session",
                window_name="test-window",
                agent_profile="developer",
            )
            command = provider._build_kiro_command()

            # Verify provider args are included before --agent
            assert "--verbose" in command
            assert "--debug" in command
            # --agent should be at the end
            assert command.endswith("--agent developer")
