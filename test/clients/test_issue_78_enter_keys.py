"""Tests for issue #78: Prompt not always returned after agents receive messages."""

import subprocess
from unittest.mock import MagicMock, call, patch

import pytest

from cli_agent_orchestrator.clients.tmux import TmuxClient


class TestEnterKeySending:
    """Test that Enter keys are reliably sent after message paste."""

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.time.sleep")
    def test_single_enter_key_sent(self, mock_sleep, mock_run):
        """Test that single Enter key is sent successfully."""
        mock_run.return_value = MagicMock(returncode=0)
        
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "Hello", enter_count=1)
        
        # Verify Enter was sent once
        enter_calls = [
            c for c in mock_run.call_args_list 
            if c[0][0][:2] == ["tmux", "send-keys"] and "Enter" in c[0][0]
        ]
        assert len(enter_calls) == 1

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.time.sleep")
    def test_double_enter_keys_sent(self, mock_sleep, mock_run):
        """Test that two Enter keys are sent for TUI multi-line mode."""
        mock_run.return_value = MagicMock(returncode=0)
        
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "Hello", enter_count=2)
        
        # Verify Enter was sent twice
        enter_calls = [
            c for c in mock_run.call_args_list 
            if c[0][0][:2] == ["tmux", "send-keys"] and "Enter" in c[0][0]
        ]
        assert len(enter_calls) == 2
        
        # Verify delay between Enter keys
        assert mock_sleep.call_count >= 2  # 0.3s initial + 0.5s between Enters

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.time.sleep")
    def test_enter_key_failure_raises_exception(self, mock_sleep, mock_run):
        """Test that Enter key send failure raises exception with details."""
        # Make load-buffer and paste-buffer succeed, but send-keys fail
        def run_side_effect(*args, **kwargs):
            cmd = args[0]
            if "send-keys" in cmd and "Enter" in cmd:
                raise subprocess.CalledProcessError(
                    returncode=1, 
                    cmd=cmd,
                    stderr="no server running on /tmp/tmux-1000/default"
                )
            return MagicMock(returncode=0)
        
        mock_run.side_effect = run_side_effect
        
        client = TmuxClient()
        with pytest.raises(subprocess.CalledProcessError) as exc_info:
            client.send_keys("test-session", "test-window", "Hello", enter_count=1)
        
        assert "no server running" in str(exc_info.value.stderr)

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.time.sleep")
    def test_enter_keys_sent_with_correct_timing(self, mock_sleep, mock_run):
        """Test that timing delays are applied correctly."""
        mock_run.return_value = MagicMock(returncode=0)
        
        client = TmuxClient()
        client.send_keys("test-session", "test-window", "Hello", enter_count=2)
        
        # Verify sleep calls: 0.3s after paste, 0.5s between Enters
        sleep_calls = [c[0][0] for c in mock_sleep.call_args_list]
        assert 0.3 in sleep_calls  # Initial delay after paste
        assert 0.5 in sleep_calls  # Delay between Enter keys

    @patch("cli_agent_orchestrator.clients.tmux.subprocess.run")
    @patch("cli_agent_orchestrator.clients.tmux.time.sleep")
    def test_enter_key_retry_on_failure(self, mock_sleep, mock_run):
        """Test that Enter key sending retries on transient failures."""
        # Make first attempt fail, second succeed
        call_count = 0
        def run_side_effect(*args, **kwargs):
            nonlocal call_count
            cmd = args[0]
            if "send-keys" in cmd and "Enter" in cmd:
                call_count += 1
                if call_count == 1:
                    # First attempt fails
                    raise subprocess.CalledProcessError(
                        returncode=1, cmd=cmd, stderr="session not found"
                    )
                # Second attempt succeeds
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)
        
        mock_run.side_effect = run_side_effect
        
        client = TmuxClient()
        # Should succeed after retry
        client.send_keys("test-session", "test-window", "Hello", enter_count=1)
        
        # Verify retry happened (2 attempts for Enter key)
        enter_calls = [
            c for c in mock_run.call_args_list 
            if c[0][0][:2] == ["tmux", "send-keys"] and "Enter" in c[0][0]
        ]
        assert len(enter_calls) == 2  # First failed, second succeeded
