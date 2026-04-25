"""Tests for U9 — Context-Manager Agent.

Covers:
- U9.1: memory_manager.md agent profile exists and has correct structure
- U9.2: --memory flag added to cao launch
- U9.3: get_curated_memory_context() delegates to context-manager or falls back
- U9.4: inject_memory_context() calls get_curated_memory_context()
- U9.5: Heartbeat check — context-manager must be IDLE
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# U9.1 — memory_manager.md agent profile
# ---------------------------------------------------------------------------


class TestMemoryManagerProfile:
    def test_profile_file_exists(self):
        from cli_agent_orchestrator.agent_store import __path__ as store_paths

        store_dir = Path(store_paths[0])
        profile_path = store_dir / "memory_manager.md"
        assert profile_path.exists(), "memory_manager.md not found in agent_store"

    def test_profile_has_required_frontmatter(self):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile = load_agent_profile("memory_manager")
        assert profile.name == "memory_manager"
        assert profile.system_prompt is not None
        assert "Context-Manager" in profile.system_prompt or "memory" in profile.system_prompt.lower()

    def test_profile_has_mcp_server(self):
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile = load_agent_profile("memory_manager")
        assert profile.mcpServers is not None
        assert "cao-mcp-server" in profile.mcpServers


# ---------------------------------------------------------------------------
# U9.2 — --memory flag
# ---------------------------------------------------------------------------


class TestMemoryFlag:
    def test_launch_command_has_memory_option(self):
        from cli_agent_orchestrator.cli.commands.launch import launch

        param_names = [p.name for p in launch.params]
        assert "memory" in param_names

    def test_memory_flag_passes_param_to_api(self):
        """Verify --memory adds memory_manager=true to request params."""
        from click.testing import CliRunner

        from cli_agent_orchestrator.cli.commands.launch import launch

        runner = CliRunner()
        with patch("cli_agent_orchestrator.cli.commands.launch.requests") as mock_requests:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "name": "dev-1234",
                "session_name": "cao-abc",
                "terminal_id": "t1",
            }
            mock_response.raise_for_status = MagicMock()
            mock_requests.post.return_value = mock_response

            result = runner.invoke(
                launch,
                ["--agents", "code_supervisor", "--headless", "--auto-approve", "--memory"],
                catch_exceptions=False,
            )

            # Verify memory_manager param was sent
            call_kwargs = mock_requests.post.call_args
            params = call_kwargs.kwargs.get("params") or call_kwargs[1].get("params", {})
            assert params.get("memory_manager") == "true"


# ---------------------------------------------------------------------------
# U9.3 — get_curated_memory_context() with fallback
# ---------------------------------------------------------------------------


class TestGetCuratedMemoryContext:
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "_find_context_manager_terminal",
        return_value=None,
    )
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "get_memory_context_for_terminal",
        return_value="<cao-memory>\n## Phase 1\n</cao-memory>",
    )
    def test_falls_back_when_no_context_manager(self, mock_phase1, mock_find):
        from cli_agent_orchestrator.services.memory_service import MemoryService

        svc = MemoryService()
        result = svc.get_curated_memory_context("t1", "Fix the bug")

        assert "<cao-memory>" in result
        mock_phase1.assert_called_once_with("t1")

    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "get_memory_context_for_terminal",
        return_value="<cao-memory>\nfallback\n</cao-memory>",
    )
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "_find_context_manager_terminal",
        return_value={"id": "cm-1", "agent_profile": "memory_manager"},
    )
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    def test_falls_back_when_context_manager_busy(self, mock_pm, mock_find, mock_phase1):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.memory_service import MemoryService

        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.PROCESSING
        mock_pm.get_provider.return_value = mock_provider

        svc = MemoryService()
        result = svc.get_curated_memory_context("t1", "Fix the bug")

        assert "fallback" in result
        mock_phase1.assert_called_once_with("t1")

    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "get_memory_context_for_terminal",
        return_value="",
    )
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "_find_context_manager_terminal",
        return_value={"id": "cm-1", "agent_profile": "memory_manager"},
    )
    @patch("cli_agent_orchestrator.services.terminal_service.send_input")
    @patch("cli_agent_orchestrator.services.terminal_service.get_output")
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    def test_returns_curated_response_from_context_manager(
        self, mock_pm, mock_get_output, mock_send_input, mock_find, mock_phase1
    ):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.memory_service import MemoryService

        mock_provider = MagicMock()
        # First call: IDLE (heartbeat check), then COMPLETED after send_input
        mock_provider.get_status.side_effect = [
            TerminalStatus.IDLE,
            TerminalStatus.COMPLETED,
        ]
        mock_pm.get_provider.return_value = mock_provider

        mock_get_output.return_value = "<cao-memory>\n## Curated\n- [global] pref: use pytest\n</cao-memory>"

        svc = MemoryService()
        result = svc.get_curated_memory_context("t1", "Write tests")

        assert "<cao-memory>" in result
        assert "Curated" in result
        mock_send_input.assert_called_once()


# ---------------------------------------------------------------------------
# U9.4 — inject_memory_context uses get_curated_memory_context
# ---------------------------------------------------------------------------


class TestInjectUsesGetCurated:
    def setup_method(self):
        """Clear injection tracking between tests."""
        from cli_agent_orchestrator.services.terminal_service import _memory_injected_terminals

        _memory_injected_terminals.clear()

    @patch(
        "cli_agent_orchestrator.services.terminal_service.MemoryService"
    )
    def test_inject_calls_get_curated(self, MockService):
        from cli_agent_orchestrator.services.terminal_service import inject_memory_context

        mock_svc = MockService.return_value
        mock_svc.get_curated_memory_context.return_value = "<cao-memory>\ncurated\n</cao-memory>"

        result = inject_memory_context("Fix the bug", "t-new")

        mock_svc.get_curated_memory_context.assert_called_once_with(
            "t-new", task_description="Fix the bug"[:200]
        )
        assert "<cao-memory>" in result
        assert "Fix the bug" in result

    @patch(
        "cli_agent_orchestrator.services.terminal_service.MemoryService"
    )
    def test_inject_skips_on_second_call(self, MockService):
        from cli_agent_orchestrator.services.terminal_service import inject_memory_context

        mock_svc = MockService.return_value
        mock_svc.get_curated_memory_context.return_value = "<cao-memory>\ncurated\n</cao-memory>"

        inject_memory_context("First message", "t-repeat")
        result2 = inject_memory_context("Second message", "t-repeat")

        # Second call should return message unchanged (no injection)
        assert result2 == "Second message"
        assert mock_svc.get_curated_memory_context.call_count == 1


# ---------------------------------------------------------------------------
# U9.5 — Heartbeat check
# ---------------------------------------------------------------------------


class TestHeartbeatCheck:
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "get_memory_context_for_terminal",
        return_value="<cao-memory>\nfallback\n</cao-memory>",
    )
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "_find_context_manager_terminal",
        return_value={"id": "cm-1", "agent_profile": "memory_manager"},
    )
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    def test_falls_back_when_provider_not_found(self, mock_pm, mock_find, mock_phase1):
        from cli_agent_orchestrator.services.memory_service import MemoryService

        mock_pm.get_provider.return_value = None

        svc = MemoryService()
        result = svc.get_curated_memory_context("t1")

        mock_phase1.assert_called_once_with("t1")

    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "get_memory_context_for_terminal",
        return_value="<cao-memory>\nfallback\n</cao-memory>",
    )
    @patch.object(
        __import__("cli_agent_orchestrator.services.memory_service", fromlist=["MemoryService"]).MemoryService,
        "_find_context_manager_terminal",
        return_value={"id": "cm-1", "agent_profile": "memory_manager"},
    )
    @patch("cli_agent_orchestrator.providers.manager.provider_manager")
    def test_falls_back_when_error_status(self, mock_pm, mock_find, mock_phase1):
        from cli_agent_orchestrator.models.terminal import TerminalStatus
        from cli_agent_orchestrator.services.memory_service import MemoryService

        mock_provider = MagicMock()
        mock_provider.get_status.return_value = TerminalStatus.ERROR
        mock_pm.get_provider.return_value = mock_provider

        svc = MemoryService()
        result = svc.get_curated_memory_context("t1")

        mock_phase1.assert_called_once_with("t1")

    def test_context_manager_does_not_inject_own_memories(self):
        """Verify the memory_manager profile instructions say no self-injection."""
        from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

        profile = load_agent_profile("memory_manager")
        assert "Do NOT inject your own memories" in profile.system_prompt or \
               "do not receive" in profile.system_prompt.lower()
