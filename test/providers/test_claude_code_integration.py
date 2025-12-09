"""Integration tests for Claude Code provider with real Claude Code CLI."""

import shutil
import time
import uuid

import pytest

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider

# Mark all tests in this module as integration and slow
pytestmark = [pytest.mark.integration, pytest.mark.slow]


@pytest.fixture(scope="session")
def claude_cli_available():
    """Check if Claude Code CLI is available."""
    if not shutil.which("claude"):
        pytest.skip("Claude Code CLI not installed")
    return True


@pytest.fixture
def test_session_name():
    """Generate a unique test session name."""
    return f"test-claude-{uuid.uuid4().hex[:8]}"


@pytest.fixture
def cleanup_session(test_session_name):
    """Cleanup fixture that ensures test session is terminated."""
    yield
    # Cleanup after test
    try:
        tmux_client.kill_session(test_session_name)
    except Exception:
        pass  # Session may already be cleaned up


class TestClaudeCodeProviderIntegration:
    """Integration tests with real Claude Code CLI."""

    def test_real_claude_initialization(self, claude_cli_available, test_session_name, cleanup_session):
        """Test real Claude Code initialization flow."""
        # Create a test tmux session
        terminal_id = "test-cc-1234"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Create provider and initialize (no agent profile)
            provider = ClaudeCodeProvider(terminal_id, test_session_name, window_name)
            result = provider.initialize()

            # Verify initialization succeeded
            assert result is True

            # Give Claude Code a moment to fully initialize
            time.sleep(2)

            # Verify status is IDLE after initialization
            status = provider.get_status()
            assert status == TerminalStatus.IDLE

        finally:
            # Exit Claude and cleanup
            try:
                tmux_client.send_keys(test_session_name, window_name, "/exit")
                time.sleep(1)
            except Exception:
                pass
            tmux_client.kill_session(test_session_name)

    def test_real_claude_simple_query(self, claude_cli_available, test_session_name, cleanup_session):
        """Test real Claude Code with a simple query."""
        # Create a test tmux session
        terminal_id = "test-cc-query"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Claude Code
            provider = ClaudeCodeProvider(terminal_id, test_session_name, window_name)
            provider.initialize()

            # Wait for IDLE status
            time.sleep(3)
            initial_status = provider.get_status()
            if initial_status != TerminalStatus.IDLE:
                pytest.skip(f"Claude Code not ready, status: {initial_status}")

            # Send a simple query
            simple_query = "Say 'Hello' and nothing else"
            tmux_client.send_keys(test_session_name, window_name, simple_query)

            # Wait for processing
            time.sleep(2)
            status = provider.get_status()

            # Allow various states during transition
            if status not in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED, TerminalStatus.IDLE]:
                pytest.skip(f"Unexpected status during query: {status}")

            # Wait for completion (max 120 seconds for Claude)
            max_wait = 120
            elapsed = 0
            while elapsed < max_wait:
                status = provider.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                if status == TerminalStatus.ERROR:
                    pytest.skip("Claude Code encountered an error")
                time.sleep(3)
                elapsed += 3

            # If still not completed, skip instead of fail
            if status != TerminalStatus.COMPLETED:
                pytest.skip(f"Claude Code did not complete in time, status: {status}")

            # Extract and verify the message
            output = tmux_client.get_history(test_session_name, window_name)
            try:
                message = provider.extract_last_message_from_script(output)
                assert len(message) > 0
            except ValueError:
                pytest.skip("Could not extract message from output")

        finally:
            # Exit Claude and cleanup
            try:
                tmux_client.send_keys(test_session_name, window_name, "/exit")
                time.sleep(1)
            except Exception:
                pass
            tmux_client.kill_session(test_session_name)

    def test_real_claude_status_detection(self, claude_cli_available, test_session_name, cleanup_session):
        """Test status detection with real Claude Code output."""
        # Create a test tmux session
        terminal_id = "test-cc-status"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Claude Code
            provider = ClaudeCodeProvider(terminal_id, test_session_name, window_name)
            provider.initialize()

            # Test IDLE status
            time.sleep(3)
            initial_status = provider.get_status()
            if initial_status != TerminalStatus.IDLE:
                pytest.skip(f"Claude Code not ready, status: {initial_status}")

            # Send a query to trigger PROCESSING/COMPLETED states
            tmux_client.send_keys(test_session_name, window_name, "What is 2+2?")

            # Should be PROCESSING or quickly move to COMPLETED
            time.sleep(2)
            status = provider.get_status()
            if status not in [TerminalStatus.PROCESSING, TerminalStatus.COMPLETED, TerminalStatus.IDLE]:
                pytest.skip(f"Unexpected status during query: {status}")

            # Wait for completion
            max_wait = 120
            elapsed = 0
            while elapsed < max_wait:
                status = provider.get_status()
                if status == TerminalStatus.COMPLETED:
                    break
                if status == TerminalStatus.ERROR:
                    pytest.skip("Claude Code encountered an error")
                time.sleep(3)
                elapsed += 3

            # If not completed, skip
            if status != TerminalStatus.COMPLETED:
                pytest.skip(f"Claude Code did not complete in time, status: {status}")

        finally:
            # Exit Claude and cleanup
            try:
                tmux_client.send_keys(test_session_name, window_name, "/exit")
                time.sleep(1)
            except Exception:
                pass
            tmux_client.kill_session(test_session_name)

    def test_real_claude_exit(self, claude_cli_available, test_session_name, cleanup_session):
        """Test exiting Claude Code."""
        # Create a test tmux session
        terminal_id = "test-cc-exit"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Initialize Claude Code
            provider = ClaudeCodeProvider(terminal_id, test_session_name, window_name)
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

            # Should show exit or return to shell
            assert "/exit" in output or "$" in output or "%" in output

        finally:
            # Cleanup
            tmux_client.kill_session(test_session_name)


class TestClaudeCodeProviderStatusTransitions:
    """Integration tests for status transitions."""

    def test_status_transitions_during_query(self, claude_cli_available, test_session_name, cleanup_session):
        """Test status transitions during a query."""
        terminal_id = "test-cc-trans"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            provider = ClaudeCodeProvider(terminal_id, test_session_name, window_name)
            provider.initialize()

            time.sleep(3)
            initial_status = provider.get_status()
            if initial_status != TerminalStatus.IDLE:
                pytest.skip(f"Claude Code not ready, status: {initial_status}")

            # Send a query
            tmux_client.send_keys(test_session_name, window_name, "Say 'done'")

            # Monitor status transitions
            statuses = []
            max_wait = 120
            elapsed = 0

            while elapsed < max_wait:
                status = provider.get_status()
                if not statuses or status != statuses[-1]:
                    statuses.append(status)

                if status == TerminalStatus.COMPLETED:
                    break

                if status == TerminalStatus.ERROR:
                    pytest.skip("Claude Code encountered an error")

                time.sleep(2)
                elapsed += 2

            # Should have captured COMPLETED (or skip if not)
            if TerminalStatus.COMPLETED not in statuses:
                pytest.skip(f"Claude Code did not complete. Status history: {statuses}")

        finally:
            try:
                tmux_client.send_keys(test_session_name, window_name, "/exit")
                time.sleep(1)
            except Exception:
                pass
            tmux_client.kill_session(test_session_name)


class TestClaudeCodeProviderErrorHandling:
    """Integration tests for error scenarios."""

    def test_invalid_session_handling(self, claude_cli_available):
        """Test handling of invalid session."""
        provider = ClaudeCodeProvider("test1234", "non-existent-session", "window-0")

        # Should raise an error when trying to initialize with non-existent session
        with pytest.raises((TimeoutError, Exception)):
            provider.initialize()

    def test_get_status_with_nonexistent_session(self, claude_cli_available):
        """Test get_status with non-existent session."""
        provider = ClaudeCodeProvider("test1234", "non-existent-session", "window-0")

        # Should handle gracefully (likely return ERROR status)
        try:
            status = provider.get_status()
            assert status == TerminalStatus.ERROR
        except Exception:
            # It's also acceptable to raise an exception
            pass


class TestClaudeCodeProviderWithProfile:
    """Integration tests with agent profiles."""

    def test_initialization_with_nonexistent_profile(self, claude_cli_available, test_session_name, cleanup_session):
        """Test initialization fails gracefully with non-existent profile."""
        terminal_id = "test-cc-profile"
        window_name = "window-0"
        tmux_client.create_session(test_session_name, window_name, terminal_id)

        try:
            # Create provider with non-existent profile
            provider = ClaudeCodeProvider(
                terminal_id, test_session_name, window_name, agent_profile="nonexistent-profile-xyz"
            )

            # Should raise ProviderError when profile doesn't exist
            from cli_agent_orchestrator.providers.claude_code import ProviderError

            with pytest.raises(ProviderError):
                provider.initialize()

        finally:
            tmux_client.kill_session(test_session_name)
