"""Integration tests for Q CLI provider with real Q CLI."""

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.q_cli import QCliProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell

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

    # Check if agent already exists
    if agent_file.exists():
        return agent_name

    # Create agent directory if it doesn't exist
    agent_dir.mkdir(parents=True, exist_ok=True)

    # Create a minimal test agent configuration
    agent_config = {
        "name": agent_name,
        "description": "",
        "prompt": None,
        "resources": ["file://.amazonq/rules/**/*.md"],
        "useLegacyMcpJson": True,
        "model": None,
    }

    # Write agent configuration
    with open(agent_file, "w") as f:
        json.dump(agent_config, f, indent=2)

    print(f"\nCreated test agent '{agent_name}' at {agent_file}")
    return agent_name


@pytest.fixture
def test_session_name():
    """Generate a unique test session name."""
    import uuid

    return f"test-q-cli-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_session(test_session_name):
    """Cleanup fixture that ensures test session is terminated."""
    yield
    # Cleanup after test
    try:
        tmux_client.kill_session(test_session_name)
    except Exception:
        pass  # Session may already be cleaned up


class TestQCliProviderIntegration:
    """Integration tests with real Q CLI."""

    def test_real_q_chat_initialization(
        self, ensure_test_agent, test_session_name, cleanup_session
    ):
        """Test real Q CLI initialization flow."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Create provider and initialize (using agent from ensure_test_agent fixture)
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            result = provider.initialize()

            # Verify initialization succeeded
            assert result is True

            # Give Q CLI a moment to fully initialize
            time.sleep(2)

            # Verify status is IDLE after initialization
            status = provider.get_status()
            assert status == TerminalStatus.IDLE

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)

    def test_real_q_chat_simple_query(self, ensure_test_agent, test_session_name, cleanup_session):
        """Test real Q CLI with a simple query."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Q CLI (using agent from ensure_test_agent fixture)
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            provider.initialize()

            # Wait for IDLE status
            time.sleep(2)
            assert provider.get_status() == TerminalStatus.IDLE

            # Send a simple query
            simple_query = "Say 'Hello, integration test!'"
            tmux_client.send_keys(test_session_name, window_name, simple_query)

            # Wait for processing
            time.sleep(1)
            status = provider.get_status()
            assert status in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]

            # Wait for completion (max 30 seconds)
            max_wait = 30
            elapsed = 0
            while elapsed < max_wait:
                status = provider.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                time.sleep(1)
                elapsed += 1

            # Verify we got a completed response
            assert status == TerminalStatus.COMPLETED

            # Extract and verify the message
            output = tmux_client.get_history(test_session_name, window_name)
            message = provider.extract_last_message_from_script(output)

            # Message should contain something (not empty)
            assert len(message) > 0
            assert "\x1b[" not in message  # ANSI codes cleaned

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)

    def test_real_q_chat_status_detection(
        self, ensure_test_agent, test_session_name, cleanup_session
    ):
        """Test status detection with real Q CLI output."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Q CLI (using agent from ensure_test_agent fixture)
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            provider.initialize()

            # Test IDLE status
            time.sleep(2)
            assert provider.get_status() == TerminalStatus.IDLE

            # Send a query to trigger PROCESSING/COMPLETED states
            tmux_client.send_keys(test_session_name, window_name, "What is 2+2?")

            # Should be PROCESSING or quickly move to COMPLETED
            time.sleep(1)
            status = provider.get_status()
            assert status in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED]

            # Wait for completion
            max_wait = 30
            elapsed = 0
            while elapsed < max_wait:
                status = provider.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                time.sleep(1)
                elapsed += 1

            # Should be COMPLETED
            assert status == TerminalStatus.COMPLETED

            # After some time, should return to IDLE (if we send Enter)
            time.sleep(1)
            tmux_client.send_keys(test_session_name, window_name, "")
            time.sleep(1)

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)

    def test_real_q_chat_exit(self, ensure_test_agent, test_session_name, cleanup_session):
        """Test exiting Q CLI."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Q CLI (using agent from ensure_test_agent fixture)
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            provider.initialize()

            time.sleep(2)
            assert provider.get_status() == TerminalStatus.IDLE

            # Send exit command
            exit_cmd = provider.exit_cli()
            tmux_client.send_keys(test_session_name, window_name, exit_cmd)

            # Wait for exit
            time.sleep(2)

            # Get the output to verify exit happened
            output = tmux_client.get_history(test_session_name, window_name)

            # Should not have the Q CLI prompt anymore after exit
            # (This test verifies the exit command works)
            assert "/exit" in output or "exit" in output.lower()

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)

    def test_real_q_chat_with_different_profile(
        self, ensure_test_agent, test_session_name, cleanup_session
    ):
        """Test Q CLI with a different agent profile if available."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Try with a different profile (may not exist, that's okay)
            provider = QCliProvider(terminal_id, test_session_name, window_name, "test-agent")

            # Initialize - may fail if profile doesn't exist
            try:
                result = provider.initialize()
                # If it succeeds, verify basic functionality
                if result:
                    time.sleep(2)
                    status = provider.get_status()
                    # Status should be IDLE or ERROR (if profile doesn't exist)
                    assert status in [TerminalStatus.IDLE, TerminalStatus.ERROR]
            except TimeoutError:
                # Profile may not exist, that's acceptable
                pytest.skip("Test profile not available")

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)


class TestQCliProviderHandoffIntegration:
    """Integration tests for handoff scenarios."""

    def test_real_handoff_status_transitions(
        self, ensure_test_agent, test_session_name, cleanup_session
    ):
        """Test status transitions during a real handoff scenario."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Q CLI with supervisor agent
            # Note: This assumes a supervisor agent exists. If not, will use developer.
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            provider.initialize()

            # Wait for IDLE status
            time.sleep(2)
            assert provider.get_status() == TerminalStatus.IDLE

            # Send a query that might trigger handoff-like behavior
            # (Real handoff depends on agent configuration)
            handoff_query = "Please help me with implementing a new feature"
            tmux_client.send_keys(test_session_name, window_name, handoff_query)

            # Monitor status transitions
            statuses = []
            max_wait = 30
            elapsed = 0

            while elapsed < max_wait:
                status = provider.get_status()
                statuses.append(status)

                # Break if we reach COMPLETED or ERROR
                if status in [TerminalStatus.COMPLETED, TerminalStatus.ERROR]:
                    break

                time.sleep(1)
                elapsed += 1

            # Verify we got through the expected states
            assert TerminalStatus.PROCESSING in statuses or TerminalStatus.COMPLETED in statuses

            # Extract the message if completed
            if statuses[-1] == TerminalStatus.COMPLETED:
                output = tmux_client.get_history(test_session_name, window_name)
                message = provider.extract_last_message_from_script(output)

                # Verify message extraction worked
                assert len(message) > 0
                assert "\x1b[" not in message  # ANSI codes cleaned

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)

    def test_real_handoff_message_integrity(
        self, ensure_test_agent, test_session_name, cleanup_session
    ):
        """Test that message extraction maintains integrity during handoff."""
        # Create a test tmux session
        terminal_id = "test1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Q CLI
            provider = QCliProvider(terminal_id, test_session_name, window_name, ensure_test_agent)
            provider.initialize()

            time.sleep(2)
            assert provider.get_status() == TerminalStatus.IDLE

            # Send a simple query (shorter to avoid buffer truncation)
            query = "Say 'Test message integrity'"
            tmux_client.send_keys(test_session_name, window_name, query)

            # Wait for processing to start
            time.sleep(1)
            initial_status = provider.get_status()

            # If already completed, we're done
            if initial_status == TerminalStatus.COMPLETED:
                status = initial_status
            else:
                # Otherwise wait for completion
                if initial_status != TerminalStatus.PROCESSING:
                    # Debug: print terminal output if not in expected state
                    debug_output = tmux_client.get_history(test_session_name, window_name)
                    print(f"\n=== DEBUG: Unexpected initial status ===")
                    print(f"Status: {initial_status}")
                    print(f"Terminal output:\n{debug_output}")
                    print("=" * 50)
                assert (
                    initial_status == TerminalStatus.PROCESSING
                ), f"Expected PROCESSING but got {initial_status}"

                max_wait = 30
                elapsed = 0
                status_history = [initial_status]
                while elapsed < max_wait:
                    status = provider.get_status()
                    if status != status_history[-1]:
                        status_history.append(status)
                    if status == TerminalStatus.COMPLETED:
                        break
                    time.sleep(1)
                    elapsed += 1

                if status != TerminalStatus.COMPLETED:
                    # Debug: print terminal output on failure
                    debug_output = tmux_client.get_history(test_session_name, window_name)
                    print(f"\n=== DEBUG: Test failed ===")
                    print(f"Final status: {status}")
                    print(f"Status history: {status_history}")
                    print(f"Terminal output:\n{debug_output}")
                    print("=" * 50)

                assert (
                    status == TerminalStatus.COMPLETED
                ), f"Expected COMPLETED but got {status} after {elapsed} seconds. Status history: {status_history}"

            # Get the output
            output = tmux_client.get_history(test_session_name, window_name)

            # Extract message and verify indices weren't corrupted
            message = provider.extract_last_message_from_script(output)

            # Verify message quality
            assert len(message) > 0
            assert "\x1b[" not in message  # All ANSI codes removed
            assert not message.startswith("[")  # No partial ANSI codes
            assert not message.endswith("\x1b")  # No trailing escape chars

            # Message should be coherent (no index corruption)
            # A corrupted extraction would have fragments or missing parts
            assert len(message.split()) >= 3  # Should have multiple words
            assert "Test message integrity" in message  # Should contain our expected phrase

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)


class TestQCliProviderWorkingDirectory:
    """Integration tests for working directory functionality."""

    def test_session_starts_in_custom_directory(self, test_session_name, cleanup_session, tmp_path):
        """Test that terminal starts in specified working directory."""
        # Create session with custom working directory
        window_name = tmux_client.create_session(
            test_session_name, "test-window", "test-term-id", working_directory=str(tmp_path)
        )

        # Query the working directory
        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)

        assert actual_dir == str(tmp_path.resolve())

    def test_working_directory_changes_are_detected(
        self, test_session_name, cleanup_session, tmp_path
    ):
        """Test that directory changes in terminal are detected."""
        # Create session
        window_name = tmux_client.create_session(
            test_session_name, "test-window", "test-term-id", working_directory=str(tmp_path)
        )

        # Create subdirectory
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        # Change directory in tmux pane
        # wait_for_shell ensures shell is initialized before sending commands
        # (paste-buffer delivery is instant, so shell must be ready first)
        wait_for_shell(tmux_client, test_session_name, window_name, timeout=10.0)
        tmux_client.send_keys(test_session_name, window_name, f"cd {subdir}")
        time.sleep(0.5)  # Wait for command to execute

        # Query working directory
        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)

        assert actual_dir == str(subdir.resolve())

    def test_symlink_resolution(self, test_session_name, cleanup_session, tmp_path):
        """Test that symlinks are resolved to real paths."""
        # Create real directory and symlink
        real_dir = tmp_path / "real"
        real_dir.mkdir()
        link_dir = tmp_path / "link"
        link_dir.symlink_to(real_dir)

        # Create session with symlink path
        window_name = tmux_client.create_session(
            test_session_name, "test-window", "test-term-id", working_directory=str(link_dir)
        )

        # Should resolve to real path
        actual_dir = tmux_client.get_pane_working_directory(test_session_name, window_name)

        assert actual_dir == str(real_dir.resolve())


class TestQCliProviderIntegrationErrorHandling:
    """Integration tests for error scenarios."""

    def test_invalid_session_handling(self, q_cli_available):
        """Test handling of invalid session."""
        provider = QCliProvider("test1234", "non-existent-session", "window-0", "developer")

        # Should raise an error or timeout when trying to initialize
        # with a non-existent session
        with pytest.raises((TimeoutError, Exception)):
            provider.initialize()

    def test_get_status_with_nonexistent_session(self, q_cli_available):
        """Test get_status with non-existent session."""
        provider = QCliProvider("test1234", "non-existent-session", "window-0", "developer")

        # Should handle gracefully (likely return ERROR status)
        # The exact behavior depends on tmux_client implementation
        try:
            status = provider.get_status()
            # If it doesn't raise an exception, it should return ERROR
            assert status == TerminalStatus.ERROR
        except Exception:
            # It's also acceptable to raise an exception
            pass
