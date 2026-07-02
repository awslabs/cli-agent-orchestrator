"""Full tests for terminal service."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.services.terminal_service import (
    OutputMode,
    TerminalInputBlockedError,
    create_terminal,
    delete_terminal,
    get_output,
    get_terminal,
    get_working_directory,
    send_input,
)


class TestCreateTerminal:
    """Tests for create_terminal function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_new_session(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test creating terminal with new session."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        assert result.id == "test1234"
        mock_tmux.create_session.assert_called_once()
        mock_provider.initialize.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.utils.tool_mapping.resolve_allowed_tools")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_persists_resolved_allowed_tools(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        mock_resolve_allowed,
    ):
        """Profile-derived restrictions should be persisted and used at launch."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            allowedTools=["fs_read"],
        )
        mock_resolve_allowed.return_value = ["fs_read"]
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal("kiro_cli", "developer", new_session=True)

        assert result.allowed_tools == ["fs_read"]
        mock_db_create.assert_called_once_with(
            "test1234",
            "cao-session",
            "developer-abcd",
            "kiro_cli",
            "developer",
            ["fs_read"],
            caller_id=None,
        )
        assert mock_provider_manager.create_provider.call_args.args[5] == ["fs_read"]

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_persists_caller_id(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """caller_id reaches the database row and the returned Terminal (issue #284)."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal(
            "kiro_cli", "developer", new_session=True, caller_id="deadbeef"
        )

        assert result.caller_id == "deadbeef"
        assert mock_db_create.call_args.kwargs.get("caller_id") == "deadbeef"

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_existing_session(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test creating terminal in existing session."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "developer-abcd"
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal("kiro_cli", "developer", session_name="cao-existing")

        assert result.id == "test1234"
        mock_tmux.create_window.assert_called_once()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_session_not_found(
        self, mock_load_profile, mock_gen_id, mock_gen_session, mock_gen_window, mock_tmux
    ):
        """Test creating terminal when session not found."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")

        with pytest.raises(ValueError, match="not found"):
            await create_terminal("kiro_cli", "developer", session_name="cao-nonexistent")

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_session_already_exists(
        self, mock_load_profile, mock_gen_id, mock_gen_session, mock_gen_window, mock_tmux
    ):
        """Test creating terminal when session already exists."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = True
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")

        with pytest.raises(ValueError, match="already exists"):
            await create_terminal(
                "kiro_cli", "developer", session_name="cao-existing", new_session=True
            )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_appends_skill_catalog(
        self,
        mock_load_profile,
        mock_build_skill_catalog,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Providers that consume runtime prompts should receive the global skill catalog."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            system_prompt="You are the developer.",
        )
        mock_build_skill_catalog.return_value = (
            "## Available Skills\n\n"
            "The following skills are available exclusively in this CAO orchestration context. "
            "To load a skill's full content, use the `load_skill` MCP tool provided by the "
            "CAO MCP server. These skills are not accessible through provider-native skill "
            "commands or directories.\n\n"
            "- **cao-worker-protocols**: Worker communication\n"
            "- **python-testing**: Pytest conventions"
        )
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_path = MagicMock()
        mock_log_dir.__truediv__.return_value = mock_log_path
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("codex", "developer", new_session=True)

        skill_prompt = mock_provider_manager.create_provider.call_args.kwargs["skill_prompt"]
        assert skill_prompt == (
            "## Available Skills\n\n"
            "The following skills are available exclusively in this CAO orchestration context. "
            "To load a skill's full content, use the `load_skill` MCP tool provided by the "
            "CAO MCP server. These skills are not accessible through provider-native skill "
            "commands or directories.\n\n"
            "- **cao-worker-protocols**: Worker communication\n"
            "- **python-testing**: Pytest conventions"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_without_skills_is_unchanged(
        self,
        mock_load_profile,
        mock_build_skill_catalog,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Providers should receive an empty skill prompt when no skills are installed."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            system_prompt="Base prompt",
        )
        mock_build_skill_catalog.return_value = ""
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_path = MagicMock()
        mock_log_dir.__truediv__.return_value = mock_log_path
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("codex", "developer", new_session=True)

        skill_prompt = mock_provider_manager.create_provider.call_args.kwargs["skill_prompt"]
        assert skill_prompt == ""
        mock_build_skill_catalog.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_name", ["kiro_cli", "copilot_cli"])
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_does_not_pass_skill_prompt_to_non_runtime_provider(
        self,
        mock_load_profile,
        mock_build_skill_catalog,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        provider_name,
    ):
        """Kiro, Q, and Copilot should receive skill_prompt=None."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            system_prompt="Base prompt",
        )
        mock_build_skill_catalog.return_value = (
            "## Available Skills\n\n"
            "The following skills are available exclusively in this CAO orchestration context. "
            "To load a skill's full content, use the `load_skill` MCP tool provided by the "
            "CAO MCP server. These skills are not accessible through provider-native skill "
            "commands or directories.\n\n"
            "- **python-testing**: Pytest conventions"
        )
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_path = MagicMock()
        mock_log_dir.__truediv__.return_value = mock_log_path
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal(provider_name, "developer", new_session=True)

        assert mock_provider_manager.create_provider.call_args.kwargs["skill_prompt"] is None

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_build_skill_catalog_called_for_runtime_prompt_provider(
        self,
        mock_load_profile,
        mock_build_skill_catalog,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """build_skill_catalog() is called exactly once for runtime-prompt providers."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer", description="Developer", system_prompt="You are the developer."
        )
        mock_build_skill_catalog.return_value = "## Available Skills\n\n- skill-a"
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__.return_value = MagicMock()
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("claude_code", "developer", new_session=True)

        mock_build_skill_catalog.assert_called_once_with()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider_name", ["opencode_cli", "kiro_cli", "copilot_cli"])
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.build_skill_catalog")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_build_skill_catalog_not_called_for_native_or_baked_provider(
        self,
        mock_load_profile,
        mock_build_skill_catalog,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
        provider_name,
    ):
        """build_skill_catalog() is never called for providers that deliver skills natively or
        at install time — OpenCode (symlink), Kiro (skill:// resources), Q, Copilot."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer", description="Developer", system_prompt="Base prompt"
        )
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__.return_value = MagicMock()
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal(provider_name, "developer", new_session=True)

        mock_build_skill_catalog.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.TERMINAL_LOG_DIR")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_profile_not_found(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_create,
        mock_provider_manager,
        mock_log_dir,
        mock_fifo_dir,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Terminal creation succeeds when agent profile is not in CAO store (e.g. JSON-only profiles)."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "my-agent-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.side_effect = FileNotFoundError("Agent profile not found: my-agent")
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_path = MagicMock()
        mock_log_dir.__truediv__.return_value = mock_log_path
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal("kiro_cli", "my-agent", new_session=True)

        assert result.id == "test1234"
        mock_provider.initialize.assert_called_once()
        # allowed_tools should be None since profile was not found
        assert mock_provider_manager.create_provider.call_args.kwargs.get("allowed_tools") is None


class TestGetTerminal:
    """Tests for get_terminal function."""

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_success(self, mock_get_metadata, mock_status_monitor):
        """Test getting terminal successfully."""
        mock_get_metadata.return_value = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "kiro_cli",
            "tmux_session": "cao-session",
            "agent_profile": "developer",
            "last_active": datetime.now(),
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        result = get_terminal("test1234")

        assert result["id"] == "test1234"
        assert result["status"] == TerminalStatus.IDLE.value

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_not_found(self, mock_get_metadata):
        """Test getting non-existent terminal."""
        mock_get_metadata.return_value = None

        with pytest.raises(ValueError, match="not found"):
            get_terminal("nonexistent")

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_no_provider(self, mock_get_metadata, mock_status_monitor):
        """Test getting terminal returns status from status_monitor."""
        mock_get_metadata.return_value = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "kiro_cli",
            "tmux_session": "cao-session",
            "agent_profile": "developer",
            "last_active": datetime.now(),
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.UNKNOWN

        result = get_terminal("test1234")

        assert result["status"] == TerminalStatus.UNKNOWN.value


class TestGetWorkingDirectory:
    """Tests for get_working_directory function."""

    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_success(self, mock_get_metadata, mock_tmux):
        """Test getting working directory successfully."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_tmux.get_pane_working_directory.return_value = "/home/user/project"

        result = get_working_directory("test1234")

        assert result == "/home/user/project"

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_working_directory_not_found(self, mock_get_metadata):
        """Test getting working directory for non-existent terminal."""
        mock_get_metadata.return_value = None

        with pytest.raises(ValueError, match="not found"):
            get_working_directory("nonexistent")


class TestSendInput:
    """Tests for send_input function."""

    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_success(self, mock_get_metadata, mock_tmux, mock_pm, mock_update):
        """Test sending input successfully."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.paste_enter_count = 2
        mock_provider.paste_submit_delay = 0.3

        result = send_input("test1234", "test message")

        assert result is True
        mock_tmux.send_keys.assert_called_once_with(
            "cao-session",
            "developer-abcd",
            "test message",
            enter_count=2,
            force_bracketed_paste=True,
            submit_delay=0.3,
        )
        mock_update.assert_called_once_with("test1234")

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_blocks_assign_when_provider_waits_for_user_answer(
        self, mock_get_metadata, mock_tmux, mock_pm, mock_update, mock_status_monitor
    ):
        """Orchestrated task text must not answer an active provider prompt."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER

        with pytest.raises(TerminalInputBlockedError, match="waiting for a user answer"):
            send_input("test1234", "new task", orchestration_type="assign")

        mock_tmux.send_keys.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_blocked_message_uses_enum_value(
        self, mock_get_metadata, mock_tmux, mock_pm, mock_update, mock_status_monitor
    ):
        """Conflict text should say 'assign', not 'OrchestrationType.ASSIGN'."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER

        with pytest.raises(TerminalInputBlockedError) as exc_info:
            send_input("test1234", "new task", orchestration_type=OrchestrationType.ASSIGN)

        assert "sending assign input" in str(exc_info.value)
        assert "OrchestrationType.ASSIGN" not in str(exc_info.value)
        mock_tmux.send_keys.assert_not_called()
        mock_update.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_allows_manual_answer_when_provider_waits_for_user_answer(
        self, mock_get_metadata, mock_tmux, mock_pm, mock_update, mock_status_monitor
    ):
        """Manual input can still answer clarify/approval prompts."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.blocks_orchestrated_input_while_waiting_user_answer = True
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        mock_provider.paste_enter_count = 1
        mock_provider.paste_submit_delay = 0.3

        result = send_input("test1234", "1")

        assert result is True
        mock_tmux.send_keys.assert_called_once_with(
            "cao-session",
            "developer-abcd",
            "1",
            enter_count=1,
            force_bracketed_paste=True,
            submit_delay=0.3,
        )
        mock_update.assert_called_once_with("test1234")

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_not_found(self, mock_get_metadata):
        """Test sending input to non-existent terminal."""
        mock_get_metadata.return_value = None

        with pytest.raises(ValueError, match="not found"):
            send_input("nonexistent", "message")


class TestGetOutput:
    """Tests for get_output function."""

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_full(self, mock_get_metadata, mock_tmux, mock_status_monitor):
        """Test getting full output."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "full terminal output"

        result = get_output("test1234", OutputMode.FULL)

        assert result == "full terminal output"

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last(self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_pm):
        """Test getting last message."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "full terminal output"
        mock_provider = MagicMock()
        mock_provider.extract_last_message_from_script.return_value = "last message"
        mock_pm.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result == "last message"

    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_not_found(self, mock_get_metadata):
        """Test getting output from non-existent terminal."""
        mock_get_metadata.return_value = None

        with pytest.raises(ValueError, match="not found"):
            get_output("nonexistent")

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_no_provider(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_pm
    ):
        """Test getting last message when provider not found."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "full output"
        mock_pm.get_provider.return_value = None

        with pytest.raises(ValueError, match="Provider not found"):
            get_output("test1234", OutputMode.LAST)

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_escalates_and_finds_marker(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_provider_manager
    ):
        """Escalating fetch: marker not found at 200 lines, found at 500."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "buffered output"
        mock_tmux.get_history.return_value = "output"
        mock_provider = MagicMock(
            spec=[
                "extract_last_message_from_script",
                "extraction_retries",
            ]
        )  # no extraction_tail_lines attribute → escalation path
        mock_provider.extract_last_message_from_script.side_effect = [
            ValueError("no marker"),  # 200-line attempt fails
            "found at 500",  # 500-line attempt succeeds
        ]
        mock_provider_manager.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result == "found at 500"
        assert mock_tmux.get_history.call_count == 2

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_escalates_all_steps_then_no_response(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_provider_manager
    ):
        """Escalating fetch: marker never found, sparse buffer — returns NO RESPONSE prefix."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "buffered output"
        # Short output (few lines) — agent never produced text response
        mock_tmux.get_history.return_value = "raw tail content"
        mock_provider = MagicMock(
            spec=[
                "extract_last_message_from_script",
                "extraction_retries",
            ]
        )  # no extraction_tail_lines attribute → escalation path
        mock_provider.extract_last_message_from_script.side_effect = ValueError("no marker")
        mock_provider_manager.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result.startswith("[NO RESPONSE")
        assert "agent completed without producing a text response" in result
        assert "raw tail content" in result
        # 4 escalation steps + 1 full_history attempt = 5 total
        assert mock_tmux.get_history.call_count == 5
        # Last call must use full_history=True
        _, last_kwargs = mock_tmux.get_history.call_args
        assert last_kwargs.get("full_history") is True

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_escalates_all_steps_then_partial_overflow(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_provider_manager
    ):
        """Escalating fetch: marker never found, buffer near-full — returns PARTIAL RESPONSE (overflow)."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "buffered output"
        # Simulate near-full buffer (>= 90% of 5000 = 4500 lines)
        large_output = "\n".join(f"line {i}" for i in range(4800))
        mock_tmux.get_history.return_value = large_output
        mock_provider = MagicMock(
            spec=[
                "extract_last_message_from_script",
                "extraction_retries",
            ]
        )  # no extraction_tail_lines attribute → escalation path
        mock_provider.extract_last_message_from_script.side_effect = ValueError("no marker")
        mock_provider_manager.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result.startswith("[PARTIAL RESPONSE")
        assert "buffer overflow likely" in result
        assert "4800 lines retrieved" in result
        # 4 escalation steps + 1 full_history attempt = 5 total
        assert mock_tmux.get_history.call_count == 5

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_full_history_fallback_finds_marker(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_provider_manager
    ):
        """After all escalation steps fail, full_history=True recovers the marker."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "buffered output"
        mock_provider = MagicMock(
            spec=[
                "extract_last_message_from_script",
                "extraction_retries",
            ]
        )  # no extraction_tail_lines attribute → escalation path

        # Tail-based reads fail (marker too far back), full_history read succeeds
        def history_side_effect(*args, **kwargs):
            if kwargs.get("full_history"):
                return "full scrollback with ⏺ marker"
            return "raw tail content without marker"

        mock_tmux.get_history.side_effect = history_side_effect

        def extract_side_effect(output):
            if "full scrollback" in output:
                return "recovered response"
            raise ValueError("no marker")

        mock_provider.extract_last_message_from_script.side_effect = extract_side_effect
        mock_provider_manager.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result == "recovered response"
        assert mock_tmux.get_history.call_count == 5  # 4 steps + 1 full_history
        _, last_kwargs = mock_tmux.get_history.call_args
        assert last_kwargs.get("full_history") is True

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_output_last_fixed_extraction_tail_lines_skips_escalation(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_provider_manager
    ):
        """Providers that declare extraction_tail_lines bypass escalation entirely."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "buffered output"
        mock_tmux.get_history.return_value = "output"
        mock_provider = MagicMock()
        mock_provider.extraction_tail_lines = 2000  # provider pins depth
        mock_provider.extraction_retries = 0
        mock_provider.extract_last_message_from_script.return_value = "found"
        mock_provider_manager.get_provider.return_value = mock_provider

        result = get_output("test1234", OutputMode.LAST)

        assert result == "found"
        # Only one history call at the fixed depth, no escalation steps
        assert mock_tmux.get_history.call_count == 1
        mock_tmux.get_history.assert_called_once_with(
            "cao-session", "developer-abcd", tail_lines=2000
        )


class TestDeleteTerminal:
    """Tests for delete_terminal function."""

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_success(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test deleting terminal successfully."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_db_delete.return_value = True

        result = delete_terminal("test1234")

        assert result is True
        mock_tmux.stop_pipe_pane.assert_called_once()
        mock_provider_manager.cleanup_provider.assert_called_once_with("test1234")

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_pipe_pane_error(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test deleting terminal when stop_pipe_pane fails."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_tmux.stop_pipe_pane.side_effect = Exception("Pipe error")
        mock_db_delete.return_value = True

        # Should not raise, just warn
        result = delete_terminal("test1234")

        assert result is True

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_no_metadata(
        self,
        mock_get_metadata,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """Test deleting terminal when metadata not found."""
        mock_get_metadata.return_value = None
        mock_db_delete.return_value = True

        result = delete_terminal("test1234")

        assert result is True
