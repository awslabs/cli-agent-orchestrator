"""Integration tests for Q CLI provider with real Q CLI.

Uses the real create_terminal() flow with FIFO pipeline and mocked DB.
"""

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.manager import provider_manager
from cli_agent_orchestrator.services.status_monitor import status_monitor
from cli_agent_orchestrator.services.terminal_service import (
    create_terminal,
    delete_terminal,
    send_input,
)

# Mark all tests in this module as integration and slow
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="session")
def q_cli_available():
    """Check if Q CLI is available and configured."""
    if not shutil.which("q"):
        pytest.skip("Q CLI not installed")
    return True


@pytest.fixture(scope="session")
def ensure_test_agent(q_cli_available):
    """Ensure a test agent exists for integration tests."""
    agent_name = "agent-q-cli-integration-test"
    agent_dir = Path.home() / ".aws" / "amazonq" / "cli-agents"
    agent_file = agent_dir / f"{agent_name}.json"

    if agent_file.exists():
        return agent_name

    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_config = {
        "name": agent_name,
        "description": "",
        "prompt": None,
        "resources": ["file://.amazonq/rules/**/*.md"],
        "useLegacyMcpJson": True,
        "model": None,
    }
    with open(agent_file, "w") as f:
        json.dump(agent_config, f, indent=2)

    return agent_name


@pytest_asyncio.fixture
async def terminal(event_pipeline, mock_db, ensure_test_agent):
    """Create a real terminal via create_terminal() with full FIFO pipeline."""
    t = await create_terminal(
        provider="q_cli",
        agent_profile=ensure_test_agent,
        new_session=True,
    )
    yield t
    try:
        delete_terminal(t.id)
    except Exception:
        pass
    try:
        tmux_client.kill_session(t.session_name)
    except Exception:
        pass


# --- Helpers ---


def _wait_for_status(terminal_id, target, timeout=30):
    elapsed = 0
    while elapsed < timeout:
        s = status_monitor.get_status(terminal_id)
        if s == target:
            return s
        time.sleep(1)
        elapsed += 1
    return status_monitor.get_status(terminal_id)


def _send(terminal_id, text):
    send_input(terminal_id, text)


class TestQCliProviderIntegration:
    """Integration tests with real Q CLI."""

    @pytest.mark.asyncio
    async def test_real_q_chat_initialization(self, terminal):
        """Test real Q CLI initialization flow."""
        status = status_monitor.get_status(terminal.id)
        assert status in {TerminalStatus.IDLE, TerminalStatus.COMPLETED}

    @pytest.mark.asyncio
    async def test_real_q_chat_simple_query(self, terminal):
        """Test real Q CLI with a simple query."""
        _send(terminal.id, "Say 'Hello, integration test!'")

        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED)
        assert status == TerminalStatus.COMPLETED

        provider = provider_manager.get_provider(terminal.id)
        output = status_monitor.get_buffer(terminal.id)
        message = provider.extract_last_message_from_script(output)

        assert len(message) > 0
        assert "\x1b[" not in message

    @pytest.mark.asyncio
    async def test_real_q_chat_status_detection(self, terminal):
        """Test status detection with real Q CLI output."""
        _send(terminal.id, "What is 2+2?")

        time.sleep(1)
        status = status_monitor.get_status(terminal.id)
        assert status in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]

        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED)
        assert status == TerminalStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_real_q_chat_exit(self, terminal):
        """Test exiting Q CLI."""
        provider = provider_manager.get_provider(terminal.id)
        exit_cmd = provider.exit_cli()
        _send(terminal.id, exit_cmd)

        time.sleep(2)
        output = status_monitor.get_buffer(terminal.id)
        assert "/exit" in output or "exit" in output.lower()

    @pytest.mark.asyncio
    async def test_real_q_chat_with_different_profile(
        self, event_pipeline, mock_db, q_cli_available
    ):
        """Test Q CLI with a different agent profile if available."""
        try:
            t = await create_terminal(
                provider="q_cli",
                agent_profile="test-agent",
                new_session=True,
            )
            status = status_monitor.get_status(t.id)
            assert status in [TerminalStatus.IDLE, TerminalStatus.ERROR]
            delete_terminal(t.id)
            tmux_client.kill_session(t.session_name)
        except TimeoutError:
            pytest.skip("Test profile not available")


class TestQCliProviderHandoffIntegration:
    """Integration tests for handoff scenarios."""

    @pytest.mark.asyncio
    async def test_real_handoff_status_transitions(self, terminal):
        """Test status transitions during a real handoff scenario."""
        _send(terminal.id, "Please help me with implementing a new feature")

        statuses = []
        max_wait = 30
        elapsed = 0
        while elapsed < max_wait:
            status = status_monitor.get_status(terminal.id)
            statuses.append(status)
            if status in [TerminalStatus.COMPLETED, TerminalStatus.ERROR]:
                break
            time.sleep(1)
            elapsed += 1

        assert TerminalStatus.PROCESSING in statuses or TerminalStatus.COMPLETED in statuses

        if statuses[-1] == TerminalStatus.COMPLETED:
            provider = provider_manager.get_provider(terminal.id)
            output = status_monitor.get_buffer(terminal.id)
            message = provider.extract_last_message_from_script(output)
            assert len(message) > 0
            assert "\x1b[" not in message

    @pytest.mark.asyncio
    async def test_real_handoff_message_integrity(self, terminal):
        """Test that message extraction maintains integrity during handoff."""
        _send(terminal.id, "Say 'Test message integrity'")

        status = _wait_for_status(terminal.id, TerminalStatus.COMPLETED)
        assert status == TerminalStatus.COMPLETED, f"Expected COMPLETED but got {status}"

        provider = provider_manager.get_provider(terminal.id)
        output = status_monitor.get_buffer(terminal.id)
        message = provider.extract_last_message_from_script(output)

        assert len(message) > 0
        assert "\x1b[" not in message
        assert not message.startswith("[")
        assert not message.endswith("\x1b")
        assert len(message.split()) >= 3


class TestQCliProviderWorkingDirectory:
    """Integration tests for working directory functionality.

    These tests don't need Q CLI — just tmux.
    """

    @pytest.fixture
    def home_tmp_path(self):
        path = Path.home() / f".cao_test_tmp_{uuid.uuid4().hex[:8]}"
        path.mkdir(parents=True, exist_ok=True)
        yield path
        shutil.rmtree(path, ignore_errors=True)

    @pytest.fixture
    def test_session_name(self):
        return f"test-q-cli-{uuid.uuid4().hex[:8]}"

    @pytest.fixture
    def cleanup_session(self, test_session_name):
        yield
        try:
            tmux_client.kill_session(test_session_name)
        except Exception:
            pass

    def test_session_starts_in_custom_directory(
        self, test_session_name, cleanup_session, home_tmp_path
    ):
        """Test that terminal starts in specified working directory."""
        window_name = tmux_client.create_session(
            test_session_name,
            "test-window",
            "test-term-id",
            working_directory=str(home_tmp_path),
        )
        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)
        assert actual_dir == str(home_tmp_path.resolve())

    def test_working_directory_changes_are_detected(
        self, test_session_name, cleanup_session, home_tmp_path
    ):
        """Test that directory changes in terminal are detected."""
        window_name = tmux_client.create_session(
            test_session_name,
            "test-window",
            "test-term-id",
            working_directory=str(home_tmp_path),
        )
        subdir = home_tmp_path / "subdir"
        subdir.mkdir()

        time.sleep(3)
        tmux_client.send_keys(test_session_name, window_name, f"cd {subdir}")
        time.sleep(2)

        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)
        assert actual_dir == str(subdir.resolve())

    def test_symlink_resolution(self, test_session_name, cleanup_session, home_tmp_path):
        """Test that symlinks are resolved to real paths."""
        real_dir = home_tmp_path / "real"
        real_dir.mkdir()
        link_dir = home_tmp_path / "link"
        link_dir.symlink_to(real_dir)

        window_name = tmux_client.create_session(
            test_session_name,
            "test-window",
            "test-term-id",
            working_directory=str(link_dir),
        )
        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)
        assert actual_dir == str(real_dir.resolve())


class TestQCliProviderIntegrationErrorHandling:
    """Integration tests for error scenarios."""

    @pytest.mark.asyncio
    async def test_invalid_session_handling(self, event_pipeline, mock_db, q_cli_available):
        """Test handling of invalid agent profile."""
        with pytest.raises((TimeoutError, Exception)):
            await create_terminal(
                provider="q_cli",
                agent_profile="non-existent-agent-profile-xyz",
                new_session=True,
            )

    def test_get_status_with_empty_output(self, q_cli_available):
        """Test get_status with empty output."""
        from cli_agent_orchestrator.providers.q_cli import QCliProvider

        provider = QCliProvider("test1234", "non-existent", "window-0", "developer")
        status = provider.get_status("")
        assert status == TerminalStatus.UNKNOWN
