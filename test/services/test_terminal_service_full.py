"""Full tests for terminal service."""

import os
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.inbox import OrchestrationType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import OutputExtractionError
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

pytestmark = pytest.mark.usefixtures("isolated_memory_db")


class TestCreateTerminal:
    """Tests for create_terminal function."""

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
    @patch("cli_agent_orchestrator.services.terminal_service._schedule_deferred_init")
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
    async def test_create_terminal_forwards_deferred_launch_payload(
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
        mock_schedule_deferred_init,
        mock_delete_terminals_by_session,
    ):
        """The real terminal layer sends the model to provider construction and
        the first task to the established deferred-init scheduler."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer",
            description="Developer",
            model="profile-default-model",
        )
        mock_provider = AsyncMock()
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        result = await create_terminal(
            "codex",
            "developer",
            new_session=True,
            defer_init=True,
            initial_message="Review the current change",
            initial_message_orchestration_type=OrchestrationType.SEND_MESSAGE,
            model="gpt-5.1-codex",
        )

        assert result.status == TerminalStatus.UNKNOWN
        assert mock_provider_manager.create_provider.call_args.kwargs["model"] == ("gpt-5.1-codex")
        mock_provider.initialize.assert_not_awaited()
        mock_schedule_deferred_init.assert_called_once_with(
            mock_provider,
            "test1234",
            "Review the current change",
            OrchestrationType.SEND_MESSAGE,
            None,
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
            engine="v2",
            group=None,
            metadata=None,
            working_directory=os.path.realpath(os.getcwd()),
        )
        assert mock_provider_manager.create_provider.call_args.args[5] == ["fs_read"]

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
    async def test_create_terminal_explicit_model_overrides_profile_model(
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
        mock_delete_terminals_by_session,
    ):
        """Regression: PR #501 review -- `model=model or (profile.model if
        profile else None)` in create_terminal is the line the entire
        model-override feature hangs on, and every other test mocks around
        this exact seam (API tests mock terminal_service.create_terminal
        itself, agent_step tests patch the terminal layer, MCP tests mock
        requests, provider tests construct providers directly). This is the
        one test that calls the REAL create_terminal with an explicit
        override AND a profile carrying its own (different) model, so a
        revert to the pre-PR `model=profile.model if profile else None`
        would fail this test even though the rest of the suite stays green."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer", description="Developer", model="profile-default-model"
        )
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal(
            "kiro_cli", "developer", new_session=True, model="explicit-override-model"
        )

        assert mock_provider_manager.create_provider.call_args.kwargs["model"] == (
            "explicit-override-model"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
    async def test_create_terminal_falls_back_to_profile_model_when_no_override(
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
        mock_delete_terminals_by_session,
    ):
        """The other half of the same precedence line: with no explicit
        override, the profile's own model still reaches provider creation
        (unchanged pre-PR behavior)."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = False
        mock_load_profile.return_value = AgentProfile(
            name="developer", description="Developer", model="profile-default-model"
        )
        mock_provider = AsyncMock()
        mock_provider.initialize.return_value = True
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")

        await create_terminal("kiro_cli", "developer", new_session=True)

        assert (
            mock_provider_manager.create_provider.call_args.kwargs["model"]
            == "profile-default-model"
        )

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_session_not_found(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_delete,
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
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    @patch("cli_agent_orchestrator.services.terminal_service.load_agent_profile")
    async def test_create_terminal_session_already_exists(
        self,
        mock_load_profile,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_tmux,
        mock_db_delete,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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


class TestCreateTerminalWorktree:
    """issue #100 Phase 1: use_worktree wiring in create_terminal.

    worktree_service itself is real-git-tested in test_worktree_service.py;
    these tests mock it to verify create_terminal's OWN wiring (call order,
    working_directory override, rollback-on-failure) without needing a real
    git repo.
    """

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
    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    async def test_use_worktree_overrides_working_directory_for_the_new_window(
        self,
        mock_worktree_service,
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
        tmp_path,
    ):
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
        # Both directories are real: create_terminal validates the EFFECTIVE launch
        # cwd (post-worktree-override) before handing it to tmux. Using a real
        # SOURCE dir too is deliberate -- it means the assertions below fail on the
        # override semantics rather than on a synthetic path being rejected first,
        # so this test still catches a regression that resolves the cwd before the
        # worktree block instead of after it.
        source_dir = tmp_path / "some" / "subdir"
        source_dir.mkdir(parents=True)
        worktree_dir = tmp_path / "worktrees" / "test1234"
        worktree_dir.mkdir(parents=True)
        mock_worktree_service.find_repo_root.return_value = "/repo"
        mock_worktree_service.create_worktree.return_value = str(worktree_dir)

        result = await create_terminal(
            "kiro_cli",
            "developer",
            session_name="cao-existing",
            working_directory=str(source_dir),
            use_worktree=True,
        )

        assert result.id == "test1234"
        mock_worktree_service.find_repo_root.assert_called_once_with(str(source_dir))
        mock_worktree_service.create_worktree.assert_called_once_with("/repo", "test1234")
        # The worktree path -- NOT the originally-given working_directory -- is
        # what actually reaches the tmux window (create_window's 4th positional
        # arg, per its own call site in terminal_service.py).
        assert mock_tmux.create_window.call_args.args[3] == os.path.realpath(worktree_dir)
        # ...and is also what gets persisted as the terminal's working_directory,
        # so list_sessions ownership metadata points at the isolated checkout.
        assert mock_db_create.call_args.kwargs["working_directory"] == os.path.realpath(
            worktree_dir
        )

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
    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    async def test_use_worktree_false_never_touches_worktree_service(
        self,
        mock_worktree_service,
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
        """Default False = today's exact behavior, unchanged."""
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

        await create_terminal("kiro_cli", "developer", session_name="cao-existing")

        mock_worktree_service.find_repo_root.assert_not_called()
        mock_worktree_service.create_worktree.assert_not_called()

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_window_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_session_name")
    @patch("cli_agent_orchestrator.services.terminal_service.generate_terminal_id")
    async def test_use_worktree_propagates_a_repo_resolution_failure(
        self,
        mock_gen_id,
        mock_gen_session,
        mock_gen_window,
        mock_worktree_service,
    ):
        """A non-git working_directory must fail fast, before any tmux session/
        window is touched -- not silently fall back to shared-directory
        behavior, which would defeat the isolation use_worktree promises."""
        from cli_agent_orchestrator.services.worktree_service import WorktreeError

        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_worktree_service.WorktreeError = WorktreeError
        mock_worktree_service.find_repo_root.side_effect = WorktreeError("not a git repo")

        with pytest.raises(WorktreeError):
            await create_terminal("kiro_cli", "developer", new_session=True, use_worktree=True)

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
    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    async def test_use_worktree_rolls_back_the_worktree_on_a_later_failure(
        self,
        mock_worktree_service,
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
        tmp_path,
    ):
        """The worktree WAS created before provider.initialize() failed later --
        the failure-cleanup path must roll it back too, or a provider-init
        timeout on a worktree-backed terminal leaves an orphan worktree/branch
        with no CAO-side record pointing at it."""
        mock_gen_id.return_value = "test1234"
        mock_gen_session.return_value = "cao-session"
        mock_gen_window.return_value = "developer-abcd"
        mock_tmux.session_exists.return_value = True
        mock_tmux.create_window.return_value = "developer-abcd"
        mock_load_profile.return_value = AgentProfile(name="developer", description="Developer")
        mock_provider = AsyncMock()
        mock_provider.initialize.side_effect = TimeoutError("provider init timed out")
        mock_provider_manager.create_provider.return_value = mock_provider
        mock_fifo_dir.__truediv__ = MagicMock(return_value="fake.fifo")
        # Real directory so the effective-cwd validation passes and the failure
        # under test is the provider-init timeout this test is actually about.
        worktree_dir = tmp_path / "worktrees" / "test1234"
        worktree_dir.mkdir(parents=True)
        mock_worktree_service.find_repo_root.return_value = "/repo"
        mock_worktree_service.create_worktree.return_value = str(worktree_dir)

        with pytest.raises(TimeoutError):
            await create_terminal(
                "kiro_cli",
                "developer",
                session_name="cao-existing",
                use_worktree=True,
            )

        mock_worktree_service.remove_worktree.assert_called_once_with("/repo", "test1234")


class TestCreateTerminalEnvVars:
    """Tests for env_vars handling on both session paths (issues #248/#408).

    #408 regression: the new_session=False branch previously passed only the
    persisted session env to create_window and silently DROPPED the explicit
    env_vars argument, so per-step workflow routing ids
    (CAO_WORKFLOW_RUN_ID/STEP_ID) never reached the terminal.
    """

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
    @patch("cli_agent_orchestrator.services.terminal_service.delete_terminals_by_session")
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
        mock_delete_terminals_by_session,
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

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_success(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_pm,
        mock_update,
        mock_status_monitor,
        mock_memory_service,
    ):
        """Test sending input successfully."""
        mock_memory_service.return_value.get_curated_memory_context.return_value = ""
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

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_clears_rolling_buffer_preserving_arm(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_pm,
        mock_update,
        mock_status_monitor,
        mock_memory_service,
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
        mock_memory_service.return_value.get_curated_memory_context.return_value = ""
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
        # The active provider receives the same explicit buffer-generation
        # boundary, so stateful detectors never compare post-dispatch output
        # with the discarded rolling buffer.
        mock_status_monitor.clear_rolling_buffer.assert_called_once_with("test1234", mock_provider)
        # reset_buffer would wipe the arm — must NOT be called on send_input.
        mock_status_monitor.reset_buffer.assert_not_called()

        # Ordering guard: clear and the provider turn marker must both run
        # BEFORE send_keys. send_keys includes a submit-delay sleep during
        # which the agent can start emitting output; a post-send_keys clear or
        # marker would parse that first chunk against stale state. Attach all
        # three calls to a shared manager so we can assert their order.
        manager = MagicMock()
        manager.attach_mock(mock_status_monitor.clear_rolling_buffer, "clear")
        manager.attach_mock(mock_provider.mark_input_received, "mark_input")
        manager.attach_mock(mock_tmux.send_keys, "send_keys")
        # Re-run with the manager wired in to capture ordered calls.
        mock_status_monitor.reset_mock()
        mock_provider.reset_mock()
        mock_tmux.reset_mock()
        manager.reset_mock()
        manager.attach_mock(mock_status_monitor.clear_rolling_buffer, "clear")
        manager.attach_mock(mock_provider.mark_input_received, "mark_input")
        manager.attach_mock(mock_tmux.send_keys, "send_keys")
        send_input("test1234", "hello again")
        ordered = [c[0] for c in manager.mock_calls]
        assert (
            ordered.index("clear") < ordered.index("mark_input") < ordered.index("send_keys")
        ), f"clear and mark_input must precede send_keys; got order {ordered}"

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

    @patch("cli_agent_orchestrator.services.terminal_service.MemoryService")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.update_last_active")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_send_input_allows_manual_answer_when_provider_waits_for_user_answer(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_pm,
        mock_update,
        mock_status_monitor,
        mock_memory_service,
    ):
        """Manual input can still answer clarify/approval prompts."""
        mock_memory_service.return_value.get_curated_memory_context.return_value = ""
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
    def test_pinned_depth_extraction_failure_raises_output_extraction_error(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_pm
    ):
        """A missing response marker is not a bad reference (issue #570).

        Providers that pin ``extraction_tail_lines`` (opencode_cli, kiro_cli)
        take the retry path, which re-raises once the attempts are spent. That
        must surface as OutputExtractionError so the API boundary can tell it
        apart from an unknown-terminal ValueError and stop returning 404.
        """
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "full output"
        mock_provider = MagicMock()
        mock_provider.extraction_tail_lines = 200
        mock_provider.extraction_retries = 0
        mock_provider.extract_last_message_from_script.side_effect = ValueError(
            "No completion marker found after last user message"
        )
        mock_pm.get_provider.return_value = mock_provider

        with pytest.raises(OutputExtractionError, match="No completion marker"):
            get_output("test1234", OutputMode.LAST)

    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_extraction_error_is_still_a_value_error(
        self, mock_get_metadata, mock_tmux, mock_status_monitor, mock_pm
    ):
        """Callers that catch ValueError keep working (issue #570)."""
        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_buffer.return_value = "full output"
        mock_provider = MagicMock()
        mock_provider.extraction_tail_lines = 200
        mock_provider.extraction_retries = 0
        mock_provider.extract_last_message_from_script.side_effect = ValueError("no marker")
        mock_pm.get_provider.return_value = mock_provider

        with pytest.raises(ValueError):
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
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_delete_terminal_retains_metadata_when_grok_cleanup_is_deferred(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
    ):
        """A retryable Grok cleanup must not be reported as a successful delete."""

        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_provider_manager.cleanup_provider.return_value = False

        assert delete_terminal("test1234") is False
        mock_db_delete.assert_not_called()
        mock_provider_manager.cleanup_provider.assert_called_once_with("test1234")

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


class TestDeleteTerminalWorktree:
    """issue #100 Phase 1: delete_terminal recognizes and removes a
    worktree-backed terminal's worktree from its own live pane cwd -- there
    is no separate CAO-side record of which terminals are worktree-backed."""

    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_removes_the_worktree_when_the_live_cwd_matches_the_worktree_shape(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
        mock_worktree_service,
    ):
        from cli_agent_orchestrator.services.worktree_service import (
            parse_worktree_path as real_parse_worktree_path,
        )

        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_tmux.get_pane_working_directory.return_value = "/repo/.cao/worktrees/test1234"
        mock_worktree_service.parse_worktree_path.side_effect = real_parse_worktree_path
        mock_db_delete.return_value = True

        result = delete_terminal("test1234")

        assert result is True
        mock_worktree_service.remove_worktree.assert_called_once_with("/repo", "test1234")

    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_does_not_remove_another_terminals_worktree(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
        mock_worktree_service,
    ):
        """Regression: worktree-backed terminal A (cwd
        .../.cao/worktrees/A) spawns non-worktree terminal B with
        working_directory explicitly set to A's cwd -- a common choice
        (handoff/assign both accept an explicit working_directory, and "here"
        is A's own directory). Deleting B must NOT force-remove A's
        still-running worktree just because B's pane cwd happens to
        path-match it; the parsed terminal_id must match the terminal
        actually being deleted (B), not A."""
        from cli_agent_orchestrator.services.worktree_service import (
            parse_worktree_path as real_parse_worktree_path,
        )

        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-bbbb",
        }
        # B's pane cwd is A's worktree root -- NOT B's own terminal_id.
        mock_tmux.get_pane_working_directory.return_value = "/repo/.cao/worktrees/terminalA"
        mock_worktree_service.parse_worktree_path.side_effect = real_parse_worktree_path
        mock_db_delete.return_value = True

        result = delete_terminal("terminalB")

        assert result is True
        mock_worktree_service.remove_worktree.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_does_not_touch_worktree_service_for_an_ordinary_shared_directory(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
        mock_worktree_service,
    ):
        from cli_agent_orchestrator.services.worktree_service import (
            parse_worktree_path as real_parse_worktree_path,
        )

        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_tmux.get_pane_working_directory.return_value = "/home/user/some/ordinary/project"
        mock_worktree_service.parse_worktree_path.side_effect = real_parse_worktree_path
        mock_db_delete.return_value = True

        result = delete_terminal("test1234")

        assert result is True
        mock_worktree_service.remove_worktree.assert_not_called()

    @patch("cli_agent_orchestrator.services.terminal_service.worktree_service")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.fifo_manager")
    @patch("cli_agent_orchestrator.services.terminal_service.db_delete_terminal")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    def test_a_non_string_live_cwd_from_the_backend_does_not_raise(
        self,
        mock_get_metadata,
        mock_tmux,
        mock_provider_manager,
        mock_db_delete,
        mock_fifo_manager,
        mock_status_monitor,
        mock_worktree_service,
    ):
        """Regression: an unconfigured/misbehaving backend call returning
        something other than str | None (e.g. a raw mock/object in a test
        double, or a defensive future change elsewhere) must degrade to
        'not a worktree', not propagate into a real subprocess call two
        steps downstream. This is exactly the shape every OTHER
        TestDeleteTerminal test above relies on implicitly (they never
        configure get_pane_working_directory)."""
        from cli_agent_orchestrator.services.worktree_service import (
            parse_worktree_path as real_parse_worktree_path,
        )

        mock_get_metadata.return_value = {
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        # Deliberately NOT a string -- mock_tmux.get_pane_working_directory()
        # returns a bare MagicMock by default when unconfigured.
        mock_worktree_service.parse_worktree_path.side_effect = real_parse_worktree_path
        mock_db_delete.return_value = True

        result = delete_terminal("test1234")  # must not raise

        assert result is True
        mock_worktree_service.remove_worktree.assert_not_called()


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


class TestDeferredInitWaitingUserAnswerSurvival:
    """PR #539 review (gutosantos82), BLOCKING test gap: "no test stubs
    initialize() to raise it and asserts _schedule_deferred_init leaves the
    worker alive (delete_worker=False)".

    Two scenarios, both ending up in _schedule_deferred_init's own
    ``except TerminalInputBlockedError`` handling — the worker must be left
    ALIVE (delete_worker=False), never torn down, when it lands on a
    recognized interactive prompt instead of genuinely failing.
    """

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service._notify_caller_of_deferred_failure")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    async def test_initialize_raising_it_directly_leaves_worker_alive(self, mock_meta, mock_notify):
        """Baseline: initialize() itself raising TerminalInputBlockedError (the
        outer init-timeout fallback in providers/claude_code.py's initialize())
        must not tear the worker down — it is alive and answerable via
        answer_user_prompt."""
        from cli_agent_orchestrator.services.terminal_service import (
            _deferred_init_tasks,
            _schedule_deferred_init,
        )

        mock_meta.return_value = {"caller_id": "super123"}
        provider_instance = AsyncMock()
        provider_instance.initialize.side_effect = TerminalInputBlockedError(
            "Claude Code initialization timed out after 30s"
        )

        before_tasks = set(_deferred_init_tasks)
        _schedule_deferred_init(
            provider_instance, "worker99", "do the task", OrchestrationType.ASSIGN, None
        )
        (task,) = set(_deferred_init_tasks) - before_tasks
        await task

        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service._notify_caller_of_deferred_failure")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_waiting_user_answer_on_send_input_leaves_worker_alive_undelivered(
        self, mock_tmux, mock_pm, mock_status_monitor, mock_meta, mock_notify
    ):
        """The actual regression PR #539 (round 2) fixes: initialize() SUCCEEDS
        (startup landed on a recognized WAITING_USER_ANSWER choice-prompt, no
        exception), so _schedule_deferred_init proceeds to
        send_input(initial_message) exactly like it would for any other
        successful init. With ClaudeCodeProvider.
        blocks_orchestrated_input_while_waiting_user_answer now True (the
        blocking fix), send_input's own guard raises TerminalInputBlockedError
        instead of pasting the assigned task into the live Ink Select widget and
        auto-confirming whichever option is highlighted -- the exact failure
        mode both reviewers flagged. The worker must be left alive
        (delete_worker=False) with the task undelivered, and nothing must ever
        be pasted into the terminal.

        Without the blocks_orchestrated_input_while_waiting_user_answer
        override (i.e. against the un-patched provider default of False), this
        test fails: send_input's guard never fires, so no
        TerminalInputBlockedError is raised, send_keys IS called (the task gets
        pasted/auto-confirmed), and _notify_caller_of_deferred_failure is never
        invoked at all -- proving this test is a genuine regression check for
        the fix, not just a restatement of already-existing behavior.
        """
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        from cli_agent_orchestrator.services.terminal_service import (
            _deferred_init_tasks,
            _schedule_deferred_init,
        )

        mock_meta.return_value = {
            "caller_id": "super123",
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        # The real provider (not a generic mock) so its actual
        # blocks_orchestrated_input_while_waiting_user_answer property value is
        # what's exercised -- this is the thing under test.
        real_provider = ClaudeCodeProvider("worker99", "cao-session", "developer-abcd")
        mock_pm.get_provider.return_value = real_provider

        provider_instance = AsyncMock()
        provider_instance.initialize.return_value = True  # succeeded: WAITING_USER_ANSWER reached
        provider_instance.shell_baseline = None

        before_tasks = set(_deferred_init_tasks)
        _schedule_deferred_init(
            provider_instance, "worker99", "do the task", OrchestrationType.ASSIGN, None
        )
        (task,) = set(_deferred_init_tasks) - before_tasks
        await task

        # Nothing was ever pasted into the live terminal.
        mock_tmux.send_keys.assert_not_called()
        # The worker is left ALIVE -- never torn down -- with the failure
        # surfaced to the caller.
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False

    @pytest.mark.asyncio
    @patch("cli_agent_orchestrator.services.terminal_service._notify_caller_of_deferred_failure")
    @patch("cli_agent_orchestrator.services.terminal_service.get_terminal_metadata")
    @patch("cli_agent_orchestrator.services.terminal_service.status_monitor")
    @patch("cli_agent_orchestrator.services.terminal_service.provider_manager")
    @patch("cli_agent_orchestrator.backends.registry._backend")
    async def test_no_orchestration_type_still_blocked_post_sessions_bypass(
        self, mock_tmux, mock_pm, mock_status_monitor, mock_meta, mock_notify
    ):
        """Round-3 review fix (call-me-ram): a raw POST /sessions caller supplying
        initial_message with NO initial_message_orchestration_type reaches
        _schedule_deferred_init with orchestration_type=None -- session_service.create_session
        never requires one. Before this fix, send_input's WAITING_USER_ANSWER guard only fires
        for OrchestrationType.ASSIGN/HANDOFF, so a None type sailed straight past it: the initial
        task text would be pasted into a live choice widget same as the round-2 bug, just via a
        different (unauthenticated-orchestration) entry point.

        _schedule_deferred_init now defaults an unstated orchestration_type to ASSIGN for guard
        purposes -- correct because every call reaching this function is by construction an
        unattended initial-task delivery, never an interactive human answer (those go through
        answer_user_prompt's own separate /terminals/{id}/input path, which never routes through
        this function at all, so this default cannot affect it).

        Without the fix (orchestration_type=None passed through unchanged to send_input): the
        guard's `orchestration_value in {ASSIGN, HANDOFF}` check is False for an empty type,
        send_keys IS called, and _notify_caller_of_deferred_failure is never invoked -- same
        failure shape as the round-2 bug this test's sibling proves.
        """
        from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
        from cli_agent_orchestrator.services.terminal_service import (
            _deferred_init_tasks,
            _schedule_deferred_init,
        )

        mock_meta.return_value = {
            "caller_id": None,  # POST /sessions has no supervisor caller
            "tmux_session": "cao-session",
            "tmux_window": "developer-abcd",
        }
        mock_status_monitor.get_status.return_value = TerminalStatus.WAITING_USER_ANSWER
        real_provider = ClaudeCodeProvider("worker99", "cao-session", "developer-abcd")
        mock_pm.get_provider.return_value = real_provider

        provider_instance = AsyncMock()
        provider_instance.initialize.return_value = True
        provider_instance.shell_baseline = None

        before_tasks = set(_deferred_init_tasks)
        # orchestration_type=None -- exactly what session_service.create_session passes when
        # the caller doesn't set initial_message_orchestration_type.
        _schedule_deferred_init(provider_instance, "worker99", "do the task", None, None)
        (task,) = set(_deferred_init_tasks) - before_tasks
        await task

        mock_tmux.send_keys.assert_not_called()
        mock_notify.assert_called_once()
        assert mock_notify.call_args.kwargs["delete_worker"] is False
