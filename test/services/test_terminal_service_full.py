"""Full tests for terminal service."""

from contextlib import ExitStack
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
from cli_agent_orchestrator.utils.terminal import managed_window_name


class TestCreateTerminal:
    """Tests for create_terminal function."""

    @pytest.fixture(autouse=True)
    def _patch_clear_session_env(self):
        """These tests exercise create_terminal orchestration, not the env
        store; stub the (strict, cond-0050) new-session pre-clear so they do
        not depend on a migrated DB. The store's own behavior is covered in
        test_session_env.py and TestCreateTerminalSessionEnvStore."""
        with patch("cli_agent_orchestrator.services.terminal_service.clear_session_env"):
            yield

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
        assert mock_db_create.call_count == 1
        call = mock_db_create.call_args
        assert call.args == (
            "test1234",
            "cao-session",
            "developer-abcd",
            "kiro_cli",
            "developer",
            ["fs_read"],
        )
        assert call.kwargs["caller_id"] is None
        assert call.kwargs["generation"] is None
        # server-owned immutable pane identity is bound at creation
        assert "pane_id" in call.kwargs
        assert "window_id" in call.kwargs
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
        # No `skills` field on the profile → catalog built with no filter (None).
        mock_build_skill_catalog.assert_called_once_with(None)

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
            name="developer",
            description="Developer",
            system_prompt="You are the developer.",
            skills=["ads-*"],
        )
        mock_build_skill_catalog.return_value = "## Available Skills\n\n- skill-a"
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__.return_value = MagicMock()
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("claude_code", "developer", new_session=True)

        # The profile's `skills` allowlist is threaded into the catalog builder.
        mock_build_skill_catalog.assert_called_once_with(["ads-*"])

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
    async def test_build_skill_catalog_called_with_empty_filter_for_deny_all(
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
        """A `skills: []` deny-all profile threads the empty list through verbatim.
        It must NOT be coerced to None — that would leak the full catalog to an
        agent meant to advertise no skills."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            system_prompt="You are the developer.",
            skills=[],
        )
        mock_build_skill_catalog.return_value = ""
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__.return_value = MagicMock()
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("claude_code", "developer", new_session=True)

        # [] must reach the builder as [] (deny-all), never coerced to None.
        mock_build_skill_catalog.assert_called_once_with([])

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
    async def test_build_skill_catalog_called_with_none_for_missing_profile_runtime_provider(
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
        """A runtime-prompt provider with no profile in the CAO store builds the
        catalog unfiltered (None). The `profile is None` guard must hold — no
        AttributeError on `profile.skills`."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.side_effect = FileNotFoundError("Agent profile not found: developer")
        mock_build_skill_catalog.return_value = "## Available Skills\n\n- skill-a"
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_log_dir.__truediv__.return_value = MagicMock()
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("claude_code", "developer", new_session=True)

        # No profile → no `skills` filter; catalog built with None (full catalog).
        mock_build_skill_catalog.assert_called_once_with(None)

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


class TestCreateTerminalEnvVars:
    """Tests for env_vars handling on both session paths (issues #248/#408).

    #408 regression: the new_session=False branch previously passed only the
    persisted session env to create_window and silently DROPPED the explicit
    env_vars argument, so per-step workflow routing ids
    (CAO_WORKFLOW_RUN_ID/STEP_ID) never reached the terminal.
    """

    @pytest.fixture(autouse=True)
    def _patch_clear_session_env(self):
        """The store functions are mocked per-test here; stub the (strict,
        cond-0050) new-session pre-clear likewise so these orchestration tests
        do not depend on a migrated DB."""
        with patch("cli_agent_orchestrator.services.terminal_service.clear_session_env"):
            yield

    def _wire_happy_mocks(
        self,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_dir,
        *,
        session_exists,
    ):
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = session_exists
        mock_tmux.create_window.return_value = "developer-abcd"
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.get_session_env")
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
    async def test_env_vars_reach_window_in_existing_session(
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
        mock_get_session_env,
    ):
        """#408 happy path: explicit env_vars must reach create_window's
        extra_env on the new_session=False path (merged with session env)."""
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )
        mock_get_session_env.return_value = {"SESSION_VAR": "from-session"}

        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-existing",
            new_session=False,
            env_vars={"CAO_WORKFLOW_RUN_ID": "run-1", "CAO_WORKFLOW_STEP_ID": "s1"},
        )

        extra_env = mock_tmux.create_window.call_args.kwargs["extra_env"]
        # Both the persisted session env AND the per-step vars are present.
        assert extra_env == {
            "SESSION_VAR": "from-session",
            "CAO_WORKFLOW_RUN_ID": "run-1",
            "CAO_WORKFLOW_STEP_ID": "s1",
        }

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.get_session_env")
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
    async def test_per_step_env_var_wins_over_persisted_session_var(
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
        mock_get_session_env,
    ):
        """#408 conflict rule: on a same-named key the explicit per-step value
        wins over the persisted session value."""
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )
        mock_get_session_env.return_value = {"SHARED_KEY": "session-value", "KEEP": "kept"}

        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-existing",
            new_session=False,
            env_vars={"SHARED_KEY": "per-step-value"},
        )

        extra_env = mock_tmux.create_window.call_args.kwargs["extra_env"]
        assert extra_env["SHARED_KEY"] == "per-step-value"  # per-step wins
        assert extra_env["KEEP"] == "kept"  # non-conflicting session var kept

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.get_session_env")
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
    async def test_no_env_vars_existing_session_uses_session_env_only(
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
        mock_get_session_env,
    ):
        """env_vars=None on new_session=False: the window still gets exactly the
        persisted session env (pre-#408 behavior preserved)."""
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )
        mock_get_session_env.return_value = {"SESSION_VAR": "from-session"}

        await create_terminal(
            "kiro_cli", "developer", session_name="cao-existing", new_session=False
        )

        extra_env = mock_tmux.create_window.call_args.kwargs["extra_env"]
        assert extra_env == {"SESSION_VAR": "from-session"}

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.set_session_env")
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
    async def test_new_session_true_path_unchanged(
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
        mock_set_session_env,
    ):
        """new_session=True is untouched by #408: env_vars go verbatim to
        create_session's extra_env and are persisted via set_session_env."""
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=False,
        )

        await create_terminal(
            "kiro_cli",
            "developer",
            new_session=True,
            env_vars={"FOO": "bar"},
        )

        assert mock_tmux.create_session.call_args.kwargs["extra_env"] == {"FOO": "bar"}
        mock_set_session_env.assert_called_once_with("cao-session", {"FOO": "bar"})
        mock_tmux.create_window.assert_not_called()


class TestCreateTerminalSessionEnvStore:
    """create_terminal against the REAL write-through session-env store (no
    get_session_env mock): post-restart durability, merge precedence, and
    fail-closed behavior at the window-creation seam (issue #248)."""

    @pytest.fixture
    def real_store(self, tmp_path, monkeypatch):
        """Point the store at an isolated tmp DB with a cold in-memory cache."""
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.services import session_env

        engine = create_engine(
            f"sqlite:///{tmp_path / 'session-env.db'}",
            connect_args={"check_same_thread": False},
        )
        database.Base.metadata.create_all(bind=engine)
        monkeypatch.setattr(
            database,
            "SessionLocal",
            sessionmaker(autocommit=False, autoflush=False, bind=engine),
        )
        with session_env._lock:
            session_env._session_forwarded_env.clear()
        yield engine
        with session_env._lock:
            session_env._session_forwarded_env.clear()
        engine.dispose()

    def _wire_happy_mocks(
        self,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_dir,
        *,
        session_exists,
    ):
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = session_exists
        mock_tmux.create_window.return_value = "developer-abcd"
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

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
    async def test_post_restart_window_gets_persisted_env_with_precedence(
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
        real_store,
    ):
        """Simulated restart (cold cache, seeded DB row): a window joining the
        session receives the persisted env, and per-step env still wins on
        conflict — {**get_session_env(session), **env_vars} unchanged."""
        import sqlite3

        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )
        with sqlite3.connect(real_store.url.database) as conn:
            conn.execute(
                "INSERT INTO session_env (session_name, env_vars, updated_at) "
                "VALUES ('cao-existing', ?, '2026-07-21T00:00:00Z')",
                ('{"SHARED_KEY": "session-value", "KEEP": "kept"}',),
            )

        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-existing",
            new_session=False,
            env_vars={"SHARED_KEY": "per-step-value"},
        )

        extra_env = mock_tmux.create_window.call_args.kwargs["extra_env"]
        assert extra_env == {"SHARED_KEY": "per-step-value", "KEEP": "kept"}

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
    async def test_missing_row_proceeds_with_per_step_env_only(
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
        real_store,
    ):
        """The legitimate no-forwarded-env case: no row, working DB — window
        creation proceeds and gets exactly the per-step env."""
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )

        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-existing",
            new_session=False,
            env_vars={"CAO_WORKFLOW_RUN_ID": "run-1"},
        )

        extra_env = mock_tmux.create_window.call_args.kwargs["extra_env"]
        assert extra_env == {"CAO_WORKFLOW_RUN_ID": "run-1"}

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
    async def test_corrupt_session_env_aborts_before_any_tmux_launch(
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
        real_store,
    ):
        """Fail closed: corrupt persisted env raises, and NO tmux window,
        provider, FIFO, or DB row is created — zero launch side effects."""
        import sqlite3

        from cli_agent_orchestrator.services.session_env import SessionEnvStoreError

        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=True,
        )
        with sqlite3.connect(real_store.url.database) as conn:
            conn.execute(
                "INSERT INTO session_env (session_name, env_vars, updated_at) "
                "VALUES ('cao-existing', 'not json{', '2026-07-21T00:00:00Z')"
            )

        with pytest.raises(SessionEnvStoreError):
            await create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-existing",
                new_session=False,
            )

        mock_tmux.create_window.assert_not_called()
        mock_tmux.create_session.assert_not_called()
        mock_provider_manager.create_provider.assert_not_called()
        mock_db_create.assert_not_called()
        mock_fifo_manager.create_reader.assert_not_called()

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
    async def test_failed_preclear_aborts_before_any_launch_side_effect(
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
        real_store,
        monkeypatch,
    ):
        """cond-0050: a no-env new session reusing a name whose stale-row
        pre-clear cannot complete durably (real SQLite IMMEDIATE lock) must
        abort BEFORE any tmux/provider/window/terminal side effect. Once the
        lock clears, the retried pre-clear deletes the row durably, so no
        later window of the reused name can receive the prior routing env."""
        import sqlite3

        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker

        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.services import session_env
        from cli_agent_orchestrator.services.session_env import SessionEnvStoreError

        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=False,
        )
        with sqlite3.connect(real_store.url.database) as conn:
            conn.execute(
                "INSERT INTO session_env (session_name, env_vars, updated_at) "
                "VALUES ('cao-reused', ?, '2026-07-21T00:00:00Z')",
                ('{"PATH": "/old/shim", "ZDOTDIR": "/old/zsh"}',),
            )

        # Store operations go through a short-busy-timeout engine so the real
        # lock refuses fast; the lock itself is a genuine second connection.
        fast_engine = create_engine(
            f"sqlite:///{real_store.url.database}",
            connect_args={"check_same_thread": False, "timeout": 0.1},
        )
        monkeypatch.setattr(
            database,
            "SessionLocal",
            sessionmaker(autocommit=False, autoflush=False, bind=fast_engine),
        )
        monkeypatch.setattr(session_env, "_RETRY_DELAY_SECONDS", 0)

        lock_conn = sqlite3.connect(real_store.url.database)
        lock_conn.execute("BEGIN IMMEDIATE")
        try:
            with pytest.raises(SessionEnvStoreError):
                await create_terminal(
                    "kiro_cli",
                    "developer",
                    session_name="cao-reused",
                    new_session=True,
                )
        finally:
            lock_conn.rollback()
            lock_conn.close()

        # Zero launch side effects: no tmux session/window, no provider, no
        # terminal DB row, no FIFO reader.
        mock_tmux.create_session.assert_not_called()
        mock_tmux.create_window.assert_not_called()
        mock_provider_manager.create_provider.assert_not_called()
        mock_db_create.assert_not_called()
        mock_fifo_manager.create_reader.assert_not_called()
        # The stale row survived — nothing claimed it was cleared.
        with sqlite3.connect(real_store.url.database) as conn:
            rows = conn.execute(
                "SELECT env_vars FROM session_env WHERE session_name = 'cao-reused'"
            ).fetchall()
        assert len(rows) == 1

        # Retry with the lock released: the pre-clear now completes durably
        # BEFORE tmux creation, so the reused name starts clean — no later
        # window can inherit the prior routing env.
        await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-reused",
            new_session=True,
        )
        assert mock_tmux.create_session.call_args.kwargs["extra_env"] is None
        with sqlite3.connect(real_store.url.database) as conn:
            rows = conn.execute(
                "SELECT env_vars FROM session_env WHERE session_name = 'cao-reused'"
            ).fetchall()
        assert rows == []
        fast_engine.dispose()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    async def test_failed_preclear_preserves_colliding_live_terminal_state(
        self,
        mock_gen_id,
        mock_tmux,
        mock_db_delete,
        real_store,
        monkeypatch,
        tmp_path,
    ):
        """cond-0067 forced-collision regression: generated terminal IDs carry
        only 32 bits, so force the would-be generated ID to collide with an
        unrelated LIVE terminal, inject durable pre-clear exhaustion, and prove
        the abort touches nothing — the unrelated terminal's DB row, FIFO
        bytes, status entry, and provider state are preserved byte-for-byte,
        every cleanup seam records ZERO calls, the stale session-env row is
        retained, and creation aborts before any terminal-ID-dependent work."""
        import sqlite3

        from cli_agent_orchestrator.clients import database
        from cli_agent_orchestrator.providers.manager import ProviderManager
        from cli_agent_orchestrator.services import fifo_reader, session_env, terminal_service
        from cli_agent_orchestrator.services.fifo_reader import FifoManager
        from cli_agent_orchestrator.services.session_env import SessionEnvStoreError

        collision_id = "deadbeef"
        db_path = real_store.url.database

        # Seed the unrelated live terminal's full state under the exact ID the
        # generator would have been forced to return: durable DB row, on-disk
        # FIFO, status entry, and provider state.
        database.create_terminal(
            collision_id, "cao-unrelated-live", "unrelated-window", "kiro_cli", "developer"
        )

        def terminal_row_state():
            with database.SessionLocal() as db:
                row = db.query(database.TerminalModel).filter_by(id=collision_id).first()
                return (
                    None
                    if row is None
                    else tuple(
                        (column.name, getattr(row, column.name))
                        for column in database.TerminalModel.__table__.columns
                    )
                )

        row_before = terminal_row_state()
        assert row_before is not None

        collateral_dir = tmp_path / "fifos"
        collateral_dir.mkdir()
        collateral_fifo = collateral_dir / f"{collision_id}.fifo"
        sentinel_bytes = b"unrelated live terminal sentinel\x00\xffbytes"
        collateral_fifo.write_bytes(sentinel_bytes)
        monkeypatch.setattr(fifo_reader, "FIFO_DIR", collateral_dir)
        # Real managers wrapped in spies: if any cleanup seam fired, the
        # destruction would be REAL (unlink/row-delete/provider removal) and
        # the spy would record it.
        fifo = FifoManager()
        fifo_stop_spy = MagicMock(wraps=fifo.stop_reader)
        monkeypatch.setattr(fifo, "stop_reader", fifo_stop_spy)
        monkeypatch.setattr(terminal_service, "fifo_manager", fifo)

        provider_cleaned = []

        class ExistingProvider:
            def cleanup(self):
                provider_cleaned.append(collision_id)

        manager = ProviderManager()
        manager._providers[collision_id] = ExistingProvider()
        provider_cleanup_spy = MagicMock(wraps=manager.cleanup_provider)
        monkeypatch.setattr(manager, "cleanup_provider", provider_cleanup_spy)
        monkeypatch.setattr(terminal_service, "provider_manager", manager)

        status_spy = MagicMock()
        monkeypatch.setattr(terminal_service, "status_monitor", status_spy)

        # The stale routing row for the reused name, and injected exhaustion of
        # its durable pre-clear delete.
        session_env.set_session_env("cao-reused", {"PATH": "/old/shim"})
        monkeypatch.setattr(
            session_env,
            "_delete_row",
            lambda _: (_ for _ in ()).throw(sqlite3.OperationalError("injected delete exhaustion")),
        )
        monkeypatch.setattr(session_env, "_RETRY_DELAY_SECONDS", 0)

        mock_gen_id.return_value = collision_id  # the forced 32-bit collision
        mock_tmux.session_exists.return_value = False

        with pytest.raises(SessionEnvStoreError):
            await create_terminal(
                "kiro_cli", "developer", session_name="cao-reused", new_session=True
            )

        # Creation aborted before terminal-ID-dependent work: the ID generator
        # itself was never reached.
        mock_gen_id.assert_not_called()
        mock_tmux.create_session.assert_not_called()
        # Every cleanup seam recorded ZERO calls.
        fifo_stop_spy.assert_not_called()
        status_spy.clear_terminal.assert_not_called()
        provider_cleanup_spy.assert_not_called()
        mock_db_delete.assert_not_called()
        # The unrelated live terminal's state is byte-for-byte preserved.
        assert terminal_row_state() == row_before
        assert collateral_fifo.read_bytes() == sentinel_bytes
        assert collision_id in manager._providers
        assert provider_cleaned == []
        # The stale session-env row is retained — nothing claimed it was cleared.
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT env_vars FROM session_env WHERE session_name = 'cao-reused'"
            ).fetchall()
        assert rows == [('{"PATH": "/old/shim"}',)]

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.clear_session_env")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    async def test_preclear_runs_before_id_generation_outside_cleanup_scope(
        self,
        mock_gen_id,
        mock_tmux,
        mock_provider_manager,
        mock_fifo_manager,
        mock_status_monitor,
        mock_db_delete,
        mock_clear_session_env,
    ):
        """cond-0067 ordering contract: the strict pre-clear is a true
        preflight — it executes BEFORE terminal-ID generation and OUTSIDE the
        resource-owning try/except, so its failure propagates with zero
        cleanup actions for resources this invocation never acquired."""
        import inspect

        from cli_agent_orchestrator.services import terminal_service
        from cli_agent_orchestrator.services.session_env import SessionEnvStoreError

        mock_tmux.session_exists.return_value = False
        mock_clear_session_env.side_effect = SessionEnvStoreError("durable delete refused")

        with pytest.raises(SessionEnvStoreError, match="durable delete refused"):
            await create_terminal(
                "kiro_cli", "developer", session_name="cao-reused", new_session=True
            )

        # Runtime ordering: the pre-clear ran; terminal-ID generation never did.
        mock_clear_session_env.assert_called_once_with("cao-reused")
        mock_gen_id.assert_not_called()
        # Outside the cleanup scope: zero cleanup calls despite the propagating
        # exception, because nothing had been acquired.
        mock_fifo_manager.stop_reader.assert_not_called()
        mock_status_monitor.clear_terminal.assert_not_called()
        mock_provider_manager.cleanup_provider.assert_not_called()
        mock_db_delete.assert_not_called()
        mock_tmux.create_session.assert_not_called()
        mock_tmux.kill_session.assert_not_called()

        # Structural pin: the pre-clear still precedes terminal-ID generation
        # and the resource-owning section. cond-0221 moved it under the
        # new-session admission claim's own try/except (which only releases the
        # claim on failure — no resource cleanup, so cond-0067's zero-cleanup
        # preflight still holds) ahead of the resource try that owns ID gen.
        source, _ = inspect.getsourcelines(terminal_service.create_terminal)
        joined = "".join(source)
        preclear_at = joined.index("clear_session_env(session_name)")
        resource_section_at = joined.index("# Step 1: Generate unique identifiers")
        id_generation_at = joined.index("terminal_id = generate_terminal_id()")
        assert preclear_at < resource_section_at < id_generation_at

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.clear_session_env")
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
    async def test_teardown_clear_failure_preserves_primary_exception(
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
        mock_clear_session_env,
        caplog,
    ):
        """cond-0050's single sanctioned softening: the create-terminal
        exception-teardown path catches and logs a strict-clear failure so
        the earlier, primary exception is preserved for the caller."""
        from cli_agent_orchestrator.services.session_env import SessionEnvStoreError

        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        self._wire_happy_mocks(
            mock_gen_id,
            mock_gen_session,
            mock_gen_window,
            mock_tmux,
            mock_provider_manager,
            mock_fifo_dir,
            session_exists=False,
        )
        # Pre-clear succeeds; the teardown clear fails.
        mock_clear_session_env.side_effect = [None, SessionEnvStoreError("delete failed")]
        mock_provider_manager.create_provider.side_effect = RuntimeError("provider boom")

        with caplog.at_level("WARNING", logger="cli_agent_orchestrator.services.terminal_service"):
            with pytest.raises(RuntimeError, match="provider boom"):
                await create_terminal(
                    "kiro_cli",
                    "developer",
                    session_name="cao-session",
                    new_session=True,
                )

        assert mock_clear_session_env.call_count == 2
        assert "could not clear session env for cao-session" in caplog.text


class TestManagedCreatePreservation:
    @pytest.mark.asyncio
    async def test_persisted_reserved_generation_survives_provider_init_failure(self, tmp_path):
        with ExitStack() as stack:
            clear_env = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.clear_session_env")
            )
            status = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
            )
            fifo = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
            )
            fifo_dir = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.FIFO_DIR")
            )
            manager = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
            )
            db_create = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.db_create_terminal")
            )
            db_delete = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
            )
            get_metadata = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
            )
            backend = stack.enter_context(
                patch("cli_agent_orchestrator.backends.registry._backend")
            )
            gen_window = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
            )
            gen_terminal = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
            )
            load_profile = stack.enter_context(
                patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
            )

            backend.session_exists.return_value = False
            gen_window.return_value = "reviewer-abcd"
            fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
            load_profile.return_value = AgentProfile(
                name="reviewer-sol-max", description="Reviewer"
            )
            provider = AsyncMock()
            provider.initialize.side_effect = RuntimeError("provider startup failed")
            manager.create_provider.return_value = provider
            get_metadata.return_value = {"id": "aabbccdd"}

            with pytest.raises(RuntimeError, match="provider startup failed"):
                await create_terminal(
                    "codex",
                    "reviewer-sol-max",
                    session_name="cao-managed-test",
                    new_session=True,
                    working_directory=str(tmp_path),
                    reserved_terminal_id="aabbccdd",
                    terminal_generation="11111111-1111-4111-8111-111111111111",
                    trusted_project_root=str(tmp_path),
                    preserve_on_init_failure=True,
                )

            gen_terminal.assert_not_called()
            clear_env.assert_called_once_with("cao-managed-test")
            db_create.assert_called_once()
            manager.create_provider.assert_called_once()
            assert manager.create_provider.call_args.kwargs["trusted_project_root"] == str(tmp_path)
            fifo.stop_reader.assert_not_called()
            status.clear_terminal.assert_not_called()
            manager.cleanup_provider.assert_not_called()
            db_delete.assert_not_called()
            backend.kill_session.assert_not_called()


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

    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch(
        "cli_agent_orchestrator.services.terminal_service." "backfill_terminal_identity_if_missing",
        return_value=True,
    )
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_backfills_proven_legacy_identity(
        self,
        mock_get_metadata,
        mock_status_monitor,
        mock_backfill,
        mock_get_backend,
    ):
        legacy = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "codex",
            "tmux_session": "cao-session",
            "agent_profile": "supervisor",
            "pane_id": None,
            "window_id": None,
            "last_active": datetime.now(),
        }
        refreshed = {**legacy, "pane_id": "%9", "window_id": "@7"}
        mock_get_metadata.side_effect = [legacy, refreshed]
        mock_get_backend.return_value.terminal_bound_window_identity.return_value = {
            "pane_id": "%9",
            "window_id": "@7",
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        result = get_terminal("test1234")

        assert result["pane_id"] == "%9"
        assert result["window_id"] == "@7"
        mock_backfill.assert_called_once_with("test1234", "%9", "@7")

    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch(
        "cli_agent_orchestrator.services.terminal_service." "backfill_terminal_identity_if_missing"
    )
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_leaves_unproven_legacy_identity_null(
        self,
        mock_get_metadata,
        mock_status_monitor,
        mock_backfill,
        mock_get_backend,
    ):
        mock_get_metadata.return_value = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "codex",
            "tmux_session": "cao-session",
            "agent_profile": "supervisor",
            "pane_id": None,
            "window_id": None,
            "last_active": datetime.now(),
        }
        mock_get_backend.return_value.terminal_bound_window_identity.return_value = None
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        result = get_terminal("test1234")

        assert result["pane_id"] is None
        assert result["window_id"] is None
        mock_backfill.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch(
        "cli_agent_orchestrator.services.terminal_service." "backfill_terminal_identity_if_missing",
        return_value=False,
    )
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_losing_backfill_race_stays_unbound(
        self,
        mock_get_metadata,
        mock_status_monitor,
        mock_backfill,
        mock_get_backend,
    ):
        mock_get_metadata.return_value = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "codex",
            "tmux_session": "cao-session",
            "agent_profile": "supervisor",
            "pane_id": None,
            "window_id": None,
            "last_active": datetime.now(),
        }
        mock_get_backend.return_value.terminal_bound_window_identity.return_value = {
            "pane_id": "%9",
            "window_id": "@7",
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        result = get_terminal("test1234")

        assert result["pane_id"] is None
        assert result["window_id"] is None
        mock_backfill.assert_called_once_with("test1234", "%9", "@7")
        assert mock_get_metadata.call_count == 1

    @patch("cli_agent_orchestrator.services.terminal_service.get_backend")
    @patch(
        "cli_agent_orchestrator.services.terminal_service." "backfill_terminal_identity_if_missing"
    )
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_get_terminal_never_repairs_partial_identity(
        self,
        mock_get_metadata,
        mock_status_monitor,
        mock_backfill,
        mock_get_backend,
    ):
        mock_get_metadata.return_value = {
            "id": "test1234",
            "tmux_window": "developer-abcd",
            "provider": "codex",
            "tmux_session": "cao-session",
            "agent_profile": "supervisor",
            "pane_id": "%existing",
            "window_id": None,
            "last_active": datetime.now(),
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        result = get_terminal("test1234")

        assert result["pane_id"] == "%existing"
        assert result["window_id"] is None
        mock_get_backend.assert_not_called()
        mock_backfill.assert_not_called()

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
    def test_send_input_clears_rolling_buffer_preserving_arm(
        self, mock_get_metadata, mock_tmux, mock_pm, mock_update, mock_status_monitor
    ):
        """send_input clears the byte buffer AFTER arming the sticky latch.

        Uses clear_rolling_buffer (byte-only) rather than reset_buffer so the
        arm set by notify_input_sent survives. Without this distinction, the
        buffer-clear would also wipe the arm, latch-blocking the subsequent
        IDLE→PROCESSING transition and causing the terminal to read IDLE for
        the entire busy turn (regression seen in test_supervisor_assign_and_
        handoff — supervisor completed real work but wait_until_status timed
        out because status never left IDLE).

        The buffer clear itself is still needed to prevent stale idle
        placeholders from the pre-task buffer combining with input_received=
        True to trigger a false COMPLETED (the handoff-worker-killed-in-8s bug).
        """
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.paste_enter_count = 2
        mock_provider.paste_submit_delay = 1.0
        mock_status_monitor.get_status.return_value = TerminalStatus.IDLE

        send_input("test1234", "hello worker")

        mock_provider.mark_input_received.assert_called_once()
        mock_status_monitor.notify_input_sent.assert_called_once_with("test1234")
        mock_status_monitor.clear_rolling_buffer.assert_called_once_with("test1234")
        # reset_buffer would wipe the arm — must NOT be called on send_input.
        mock_status_monitor.reset_buffer.assert_not_called()

        # Ordering guard: the byte-buffer clear must run BEFORE send_keys, not
        # after. send_keys includes a submit-delay sleep during which the agent
        # can start emitting output; a post-send_keys clear would wipe that
        # newly-emitted first chunk of the turn. Attach both calls to a shared
        # manager so we can assert their relative order.
        manager = MagicMock()
        manager.attach_mock(mock_status_monitor.clear_rolling_buffer, "clear")
        manager.attach_mock(mock_tmux.send_keys, "send_keys")
        # Re-run with the manager wired in to capture ordered calls.
        mock_status_monitor.reset_mock()
        mock_tmux.reset_mock()
        manager.reset_mock()
        manager.attach_mock(mock_status_monitor.clear_rolling_buffer, "clear")
        manager.attach_mock(mock_tmux.send_keys, "send_keys")
        send_input("test1234", "hello again")
        ordered = [c[0] for c in manager.mock_calls]
        assert ordered.index("clear") < ordered.index(
            "send_keys"
        ), f"clear_rolling_buffer must precede send_keys; got order {ordered}"

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

    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_blocks_delivery_into_error_terminal(
        self, mock_get_metadata, mock_tmux, mock_pm, mock_update, mock_status_monitor
    ):
        """Delivery into a terminal in ERROR state must be refused (dead-terminal guard)."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "codex-abcd",
        }
        mock_provider = mock_pm.get_provider.return_value
        mock_provider.blocks_orchestrated_input_while_waiting_user_answer = False
        mock_status_monitor.get_status.return_value = TerminalStatus.ERROR

        with pytest.raises(TerminalInputBlockedError, match="ERROR state"):
            send_input("test1234", "hello worker")

        mock_tmux.send_keys.assert_not_called()
        mock_update.assert_not_called()

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

    def test_managed_delete_refuses_stale_generation_before_external_cleanup(self):
        metadata = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
            "generation": "replacement-generation",
        }
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value=metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal_if_generation",
                return_value=False,
            ),
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager") as provider,
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        ):
            with pytest.raises(ValueError, match="generation mismatch"):
                delete_terminal(
                    "test1234",
                    expected_generation="old-generation",
                    expected_session="cao-session",
                )

        backend.kill_window.assert_not_called()
        provider.cleanup_provider.assert_not_called()

    def test_managed_delete_claims_exact_generation_once(self):
        generation = "11111111-1111-4111-8111-111111111111"
        metadata = {
            "tmux_session": "cao-session",
            "tmux_window": managed_window_name("deadbeef", generation),
            "generation": generation,
        }
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value=metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal_if_generation",
                return_value=True,
            ) as conditional_delete,
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal"
            ) as unconditional_delete,
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        ):
            backend.window_exists.return_value = False
            assert (
                delete_terminal(
                    "deadbeef",
                    expected_generation=generation,
                    expected_session="cao-session",
                )
                is True
            )

        conditional_delete.assert_called_once_with("deadbeef", generation)
        unconditional_delete.assert_not_called()

    def test_managed_delete_preserves_row_when_window_survives(self):
        generation = "11111111-1111-4111-8111-111111111111"
        metadata = {
            "tmux_session": "cao-session",
            "tmux_window": managed_window_name("deadbeef", generation),
            "generation": generation,
        }
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value=metadata,
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal_if_generation"
            ) as conditional_delete,
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        ):
            backend.kill_window.return_value = False
            backend.window_exists.return_value = True
            with pytest.raises(RuntimeError, match="survived cleanup"):
                delete_terminal(
                    "deadbeef",
                    expected_generation=generation,
                    expected_session="cao-session",
                )

        conditional_delete.assert_not_called()

    def test_managed_delete_recovers_when_row_is_already_absent(self):
        generation = "11111111-1111-4111-8111-111111111111"
        with (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                return_value=None,
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal_if_generation"
            ) as conditional_delete,
            patch("cli_agent_orchestrator.backends.registry._backend") as backend,
        ):
            backend.kill_window.return_value = False
            backend.window_exists.return_value = False
            assert (
                delete_terminal(
                    "deadbeef",
                    expected_generation=generation,
                    expected_session="cao-session",
                )
                is True
            )

        backend.kill_window.assert_called_once_with(
            "cao-session", managed_window_name("deadbeef", generation)
        )
        conditional_delete.assert_not_called()


class TestManagedTeardownClaimRecheck:
    """P1-1 (final conformance §20.2f): the generation-owned teardown claim is
    rechecked immediately before EVERY destructive subsystem step. A
    replacement swapped in mid-teardown stops the sequence with zero further
    destructive action; expected_session alone never degrades to ID-only
    destruction."""

    GEN = "11111111-1111-4111-8111-111111111111"
    REPLACEMENT_GEN = "22222222-2222-4222-8222-222222222222"

    def _metadata(self, generation=None):
        generation = generation or self.GEN
        return {
            "tmux_session": "cao-session",
            "tmux_window": managed_window_name("deadbeef", generation),
            "generation": generation,
            "provider": "codex",
        }

    def _swap_side_effect(self, swap_after):
        calls = {"n": 0}

        def side_effect(_tid):
            calls["n"] += 1
            if calls["n"] <= swap_after:
                return self._metadata()
            return self._metadata(self.REPLACEMENT_GEN)

        return side_effect

    def _patches(self, metadata_side_effect):
        return (
            patch(
                "cli_agent_orchestrator.services.terminal_service.get_terminal_metadata",
                side_effect=metadata_side_effect,
            ),
            patch(
                "cli_agent_orchestrator.services.terminal_service.db_delete_terminal_if_generation",
                return_value=True,
            ),
            patch("cli_agent_orchestrator.services.terminal_service.provider_manager"),
            patch("cli_agent_orchestrator.services.terminal_service.fifo_manager"),
            patch("cli_agent_orchestrator.services.terminal_service.status_monitor"),
            patch("cli_agent_orchestrator.services.terminal_service.get_herdr_inbox_service"),
            patch("cli_agent_orchestrator.backends.registry._backend"),
        )

    def test_replacement_swap_before_herdr_step_refuses_all_teardown(self):
        # Swap lands after the entry check: the herdr unregister and every
        # later destructive step must refuse.
        patches = self._patches(self._swap_side_effect(swap_after=1))
        with (
            patches[0],
            patches[1] as conditional,
            patches[2] as provider,
            patches[3] as fifo,
            patches[4] as status_mon,
            patches[5] as herdr,
            patches[6] as backend,
        ):
            with pytest.raises(ValueError, match="changed generation during cleanup"):
                delete_terminal(
                    "deadbeef",
                    expected_generation=self.GEN,
                    expected_session="cao-session",
                )
            self._assert_zero_teardown(conditional, provider, fifo, status_mon, herdr, backend)

    def _assert_zero_teardown(self, conditional, provider, fifo, status_mon, herdr, backend):
        (herdr.return_value.unregister_terminal).assert_not_called()
        fifo.stop_reader.assert_not_called()
        status_mon.clear_terminal.assert_not_called()
        backend.kill_window.assert_not_called()
        backend.stop_pipe_pane.assert_not_called()
        provider.cleanup_provider.assert_not_called()
        conditional.assert_not_called()

    def test_replacement_swap_mid_teardown_refuses_remaining_steps(self):
        # Swap lands after the FIFO step: herdr/snapshot/pipe-pane already
        # ran, but the FIFO stop, status clear, window kill, provider cleanup,
        # and the CAS delete must all refuse.
        patches = self._patches(self._swap_side_effect(swap_after=4))
        with (
            patches[0] as _m,
            patches[1] as conditional,
            patches[2] as provider,
            patches[3] as fifo,
            patches[4] as status_mon,
            patches[5] as herdr,
            patches[6] as backend,
        ):
            with pytest.raises(ValueError, match="changed generation during cleanup"):
                delete_terminal(
                    "deadbeef",
                    expected_generation=self.GEN,
                    expected_session="cao-session",
                )
            (herdr.return_value.unregister_terminal).assert_called_once_with(
                "deadbeef",
                expected_pane_id=None,
            )
            fifo.stop_reader.assert_not_called()
            status_mon.clear_terminal.assert_not_called()
            backend.kill_window.assert_not_called()
            provider.cleanup_provider.assert_not_called()
            conditional.assert_not_called()

    def test_session_without_generation_never_degrades_to_id_only_destruction(self):
        patches = self._patches(self._swap_side_effect(swap_after=0))
        with (
            patches[0],
            patches[1] as conditional,
            patches[2] as provider,
            patches[3] as fifo,
            patches[4] as status_mon,
            patches[5] as herdr,
            patches[6] as backend,
        ):
            with pytest.raises(ValueError, match="without the exact generation"):
                delete_terminal("deadbeef", expected_session="cao-session")
            (herdr.return_value.unregister_terminal).assert_not_called()
            fifo.stop_reader.assert_not_called()
            status_mon.clear_terminal.assert_not_called()
            backend.kill_window.assert_not_called()
            provider.cleanup_provider.assert_not_called()
            conditional.assert_not_called()

    def test_row_appearing_on_row_absent_path_is_preserved_as_replacement(self):
        # Row-absent recovery: the entry window kill is generation-scoped by
        # name, but if a terminal ROW appears mid-teardown it can only be a
        # replacement incarnation — every later step refuses.
        calls = {"n": 0}

        def side_effect(_tid):
            calls["n"] += 1
            if calls["n"] == 1:
                return None  # row absent at entry
            return self._metadata(self.REPLACEMENT_GEN)

        patches = self._patches(side_effect)
        with (
            patches[0],
            patches[1] as conditional,
            patches[2] as provider,
            patches[3] as fifo,
            patches[4] as status_mon,
            patches[5] as herdr,
            patches[6] as backend,
        ):
            backend.window_exists.return_value = False
            with pytest.raises(ValueError, match="row appeared during row-absent"):
                delete_terminal(
                    "deadbeef",
                    expected_generation=self.GEN,
                    expected_session="cao-session",
                )
            (herdr.return_value.unregister_terminal).assert_not_called()
            provider.cleanup_provider.assert_not_called()
            conditional.assert_not_called()


class TestDeferredInitFailureNotification:
    """PR #390 must-fixes #1/#3: a deferred-init failure must be OBSERVABLE to
    the supervisor (assign already returned success=True), teardown must pass
    the registry (post_kill_terminal parity), and TerminalInputBlockedError
    must NOT delete the worker.
    """

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.create_inbox_message")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_notify_enqueues_inbox_to_caller_and_deletes_with_registry(
        self, mock_meta, mock_create_inbox, mock_delete
    ):
        from cli_agent_orchestrator.services.terminal_service import (
            _notify_caller_of_deferred_failure,
        )

        mock_meta.return_value = {"caller_id": "super123"}
        registry = MagicMock()

        _notify_caller_of_deferred_failure(
            "worker99", "init failed: boom", registry, delete_worker=True
        )

        # Caller notified via inbox (sender = the failed worker, receiver = caller)
        mock_create_inbox.assert_called_once()
        _, kwargs = mock_create_inbox.call_args
        assert kwargs["receiver_id"] == "super123"
        assert kwargs["sender_id"] == "worker99"
        assert "init failed: boom" in kwargs["message"]
        # Teardown passes the registry so post_kill_terminal hooks fire.
        mock_delete.assert_called_once_with("worker99", registry=registry)

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.create_inbox_message")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_notify_without_delete_leaves_worker_alive(
        self, mock_meta, mock_create_inbox, mock_delete
    ):
        """delete_worker=False (the WAITING_USER_ANSWER case) must notify but
        NOT tear the worker down."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notify_caller_of_deferred_failure,
        )

        mock_meta.return_value = {"caller_id": "super123"}

        _notify_caller_of_deferred_failure(
            "worker99", "waiting on prompt", None, delete_worker=False
        )

        mock_create_inbox.assert_called_once()
        mock_delete.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.create_inbox_message")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_notify_inbox_failure_does_not_block_teardown(
        self, mock_meta, mock_create_inbox, mock_delete
    ):
        """If the inbox enqueue fails, teardown must still happen (independent
        best-effort steps)."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notify_caller_of_deferred_failure,
        )

        mock_meta.return_value = {"caller_id": "super123"}
        mock_create_inbox.side_effect = Exception("db down")

        _notify_caller_of_deferred_failure("worker99", "boom", None, delete_worker=True)

        mock_delete.assert_called_once()

    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.create_inbox_message")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_notify_no_caller_id_is_log_only(self, mock_meta, mock_create_inbox, mock_delete):
        """No caller_id (e.g. operator-launched) → no inbox attempt, still tears
        down."""
        from cli_agent_orchestrator.services.terminal_service import (
            _notify_caller_of_deferred_failure,
        )

        mock_meta.return_value = {"caller_id": None}

        _notify_caller_of_deferred_failure("worker99", "boom", None, delete_worker=True)

        mock_create_inbox.assert_not_called()
        mock_delete.assert_called_once()


class TestCreateTerminalLifecycleAdmission:
    """The under-claim new-session admission is the zero-effect linearization
    point. ``describe()`` fails open to ``working`` for observational marshal
    callers; creation admission must not, or an unreadable lifecycle store can
    hide a stopped row and allow stale-env deletion / name reuse."""

    @pytest.mark.asyncio
    async def test_an_unreadable_lifecycle_refuses_admission_with_zero_effects(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lifecycle, terminal_service
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        # describe() converts a read failure into {working, unreadable: ...}.
        monkeypatch.setattr(
            session_lifecycle,
            "describe",
            lambda name: {
                "session_name": name,
                "lifecycle": session_lifecycle.WORKING,
                "unreadable": "database is locked",
            },
        )

        effects: list = []
        monkeypatch.setattr(
            terminal_service, "clear_session_env", lambda *a, **k: effects.append("clear_env")
        )
        monkeypatch.setattr(
            terminal_service, "generate_terminal_id", lambda: effects.append("gen_id") or "deadbeef"
        )
        backend = MagicMock()
        backend.session_exists.return_value = False
        backend.create_session.side_effect = lambda *a, **k: effects.append("create_session")
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)

        outcome: dict = {}
        try:
            await create_terminal(
                "mock_cli", "developer", session_name="cao-probe", new_session=True
            )
            outcome["ok"] = True
        except session_lifecycle.SessionLifecycleUnavailable as exc:
            outcome["unavailable"] = exc
        except BaseException as exc:  # noqa: BLE001 - capture the pre-fix fail-open path
            outcome["other"] = exc

        # Admission must treat unreadable as typed lifecycle unavailability and
        # refuse — not fail open into stale-env deletion / creation.
        assert isinstance(
            outcome.get("unavailable"), session_lifecycle.SessionLifecycleUnavailable
        ), outcome
        # Zero effects: nothing past the admission check ran.
        assert effects == [], effects


class TestCreateTerminalAddToExistingAdmission:
    """P1: the add-to-existing (new_session=False) path shares the new-session
    lifecycle admission. A stopped or unreadable row is rejected under the
    physical claim before any window/resource/env/provider/DB effect — even when
    the backend session still exists (partial-stop composition)."""

    @pytest.fixture(autouse=True)
    def _db(self, isolated_memory_db):
        return isolated_memory_db

    def _wire(self, monkeypatch, *, session_exists):
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.services import terminal_service

        effects: list = []
        backend = MagicMock()
        backend.session_exists.return_value = session_exists
        backend.create_window.side_effect = lambda *a, **k: effects.append("create_window") or "win"
        backend.create_session.side_effect = lambda *a, **k: effects.append("create_session")
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
        monkeypatch.setattr(terminal_service, "generate_window_name", lambda *a, **k: "dev-abcd")
        monkeypatch.setattr(terminal_service, "load_agent_profile", lambda *a, **k: None)
        provider = MagicMock()
        provider.initialize = AsyncMock(return_value=True)
        pm = MagicMock()
        pm.create_provider.return_value = provider
        monkeypatch.setattr(terminal_service, "provider_manager", pm)
        monkeypatch.setattr(terminal_service, "db_create_terminal", lambda *a, **k: None)
        monkeypatch.setattr(terminal_service, "fifo_manager", MagicMock())
        monkeypatch.setattr(terminal_service, "status_monitor", MagicMock())
        return effects

    @pytest.mark.asyncio
    async def test_refuses_a_stopped_session_before_any_window_effect(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lifecycle as sl
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        sl.stop("cao-existing", declared_by="boot")
        effects = self._wire(monkeypatch, session_exists=True)

        with pytest.raises(ValueError, match="is stopped"):
            await create_terminal(
                "mock_cli", "developer", session_name="cao-existing", new_session=False
            )
        assert effects == [], effects

    @pytest.mark.asyncio
    async def test_refuses_an_unreadable_lifecycle_before_any_window_effect(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lifecycle as sl
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        monkeypatch.setattr(
            sl,
            "describe",
            lambda name: {
                "session_name": name,
                "lifecycle": sl.WORKING,
                "unreadable": "database is locked",
            },
        )
        effects = self._wire(monkeypatch, session_exists=True)

        with pytest.raises(sl.SessionLifecycleUnavailable):
            await create_terminal(
                "mock_cli", "developer", session_name="cao-existing", new_session=False
            )
        assert effects == [], effects

    @pytest.mark.asyncio
    async def test_admits_an_ordinary_working_session_and_creates_the_window(self, monkeypatch):
        from cli_agent_orchestrator.services import session_lifecycle as sl
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        sl.declare("cao-existing", sl.WORKING, declared_by="boot")
        effects = self._wire(monkeypatch, session_exists=True)

        await create_terminal(
            "mock_cli", "developer", session_name="cao-existing", new_session=False
        )
        assert effects == ["create_window"], effects

    @pytest.mark.asyncio
    async def test_a_partial_stopped_row_refuses_even_when_the_backend_session_survives(
        self, monkeypatch
    ):
        """Composition: a stop that left a durable stopped row plus a surviving
        backend session (partial collection) must still refuse an added terminal
        — backend existence alone no longer admits the create."""
        from cli_agent_orchestrator.services import session_lifecycle as sl
        from cli_agent_orchestrator.services.terminal_service import create_terminal

        sl.stop("cao-existing", declared_by="boot")  # durable stopped row
        effects = self._wire(monkeypatch, session_exists=True)  # session still alive

        with pytest.raises(ValueError, match="is stopped"):
            await create_terminal(
                "mock_cli", "developer", session_name="cao-existing", new_session=False
            )
        assert effects == [], effects


class TestCreateTerminalAsyncClaim:
    """Two asyncio tasks on one event-loop thread cannot overlap the create
    critical section, and the second must not block the loop while the first
    awaits (P1.3). The thread-local reentrancy bypass must not let task B in."""

    @pytest.fixture(autouse=True)
    def _db(self, isolated_memory_db):
        return isolated_memory_db

    def _wire_async(self, monkeypatch, *, a_init):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from cli_agent_orchestrator.services import terminal_service

        effects = {"create_session": 0, "db": 0}

        provider_a = MagicMock()
        provider_a.initialize = a_init
        provider_b = MagicMock()
        provider_b.initialize = AsyncMock(return_value=True)
        pm = MagicMock()
        pm.create_provider.side_effect = [provider_a, provider_b]

        backend = MagicMock()
        created: list = []
        backend.session_exists.side_effect = lambda _name: bool(created)

        def _create_session(*a, **k):
            created.append(True)
            effects["create_session"] += 1
            return "win"

        backend.create_session.side_effect = _create_session
        monkeypatch.setattr(terminal_service, "get_backend", lambda: backend)
        monkeypatch.setattr(terminal_service, "generate_terminal_id", lambda: "deadbeef")
        monkeypatch.setattr(terminal_service, "generate_window_name", lambda *a, **k: "win")
        monkeypatch.setattr(terminal_service, "load_agent_profile", lambda *a, **k: None)
        monkeypatch.setattr(terminal_service, "provider_manager", pm)

        def _db_create(*a, **k):
            effects["db"] += 1

        monkeypatch.setattr(terminal_service, "db_create_terminal", _db_create)
        monkeypatch.setattr(terminal_service, "fifo_manager", MagicMock())
        monkeypatch.setattr(terminal_service, "status_monitor", MagicMock())
        monkeypatch.setattr(terminal_service, "clear_session_env", lambda *a, **k: None)
        return effects

    @pytest.mark.asyncio
    async def test_task_b_reaches_no_effect_until_task_a_releases(self, monkeypatch):
        import asyncio

        from cli_agent_orchestrator.services.terminal_service import create_terminal

        a_in_init = asyncio.Event()
        release_a = asyncio.Event()

        async def a_init():
            a_in_init.set()
            await release_a.wait()

        effects = self._wire_async(monkeypatch, a_init=a_init)

        task_a = asyncio.create_task(
            create_terminal("mock_cli", "developer", session_name="cao-x", new_session=True)
        )
        await a_in_init.wait()  # A holds the claim, paused in provider init
        assert effects["create_session"] == 1, effects  # A created + persisted
        assert effects["db"] == 1, effects

        task_b = asyncio.create_task(
            create_terminal("mock_cli", "developer", session_name="cao-x", new_session=True)
        )
        for _ in range(10):
            await asyncio.sleep(0)  # B runs and must block on the claim, not the loop
        assert not task_b.done(), "task B entered the critical section while A held it"
        assert effects["create_session"] == 1, effects  # B reached no physical effect
        assert effects["db"] == 1, effects  # B reached no DB effect

        release_a.set()
        await task_a  # A completes and releases the claim
        with pytest.raises(ValueError, match="already exists"):
            await task_b  # B now proceeds and refuses the duplicate
        assert effects["create_session"] == 1, effects
        assert effects["db"] == 1, effects

    @pytest.mark.asyncio
    async def test_a_cancelled_in_init_releases_the_claim_for_b(self, monkeypatch):
        import asyncio

        from cli_agent_orchestrator.services.terminal_service import create_terminal

        a_in_init = asyncio.Event()
        a_cancel_gate = asyncio.Event()

        async def a_init():
            a_in_init.set()
            await a_cancel_gate.wait()  # held until cancelled

        effects = self._wire_async(monkeypatch, a_init=a_init)

        task_a = asyncio.create_task(
            create_terminal("mock_cli", "developer", session_name="cao-x", new_session=True)
        )
        await a_in_init.wait()

        task_b = asyncio.create_task(
            create_terminal("mock_cli", "developer", session_name="cao-x", new_session=True)
        )
        for _ in range(10):
            await asyncio.sleep(0)
        assert not task_b.done()

        task_a.cancel()  # cancel A while it holds the claim
        with pytest.raises(asyncio.CancelledError):
            await task_a

        for _ in range(10):
            await asyncio.sleep(0)
        # B must now be able to enter (A released on cancellation).
        a_cancel_gate.set()  # let B's own init proceed if it reaches it
        with pytest.raises(ValueError, match="already exists"):
            await task_b
