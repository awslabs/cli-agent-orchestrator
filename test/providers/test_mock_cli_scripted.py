"""Unit tests for mock_cli scripted-prompt mode.

Proves:
- When CAO_MOCK_CLI_SCRIPTED_PROMPTS=1 and buffer contains APPROVAL_REQUIRED:
  marker, get_status returns WAITING_USER_ANSWER.
- When answer is delivered (text after marker), status clears back to normal.
- Default behavior unchanged when env var not set.
"""

from __future__ import annotations

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.mock_cli import (
    SCRIPTED_PROMPT_MARKER,
    MockCliProvider,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    """Create a MockCliProvider instance (does not launch binary)."""
    return MockCliProvider(
        terminal_id="t-test",
        session_name="test-session",
        window_name="test-window",
    )


# ---------------------------------------------------------------------------
# Default behavior (env not set)
# ---------------------------------------------------------------------------


class TestDefaultBehavior:
    """Scripted-prompt mode OFF: default behavior unchanged."""

    def test_marker_ignored_when_env_not_set(self, provider, monkeypatch):
        """APPROVAL_REQUIRED: marker is ignored without env var."""
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = f"Some output\n{SCRIPTED_PROMPT_MARKER} Do you approve?\n"
        # Without idle prompt, returns PROCESSING
        status = provider.get_status(buffer)
        assert status == TerminalStatus.PROCESSING

    def test_normal_idle(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = "Welcome to mock_cli\n\u276f "
        status = provider.get_status(buffer)
        assert status == TerminalStatus.IDLE

    def test_normal_completed(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        buffer = "> MOCK: hello\n\u276f "
        status = provider.get_status(buffer)
        assert status == TerminalStatus.COMPLETED

    def test_empty_buffer(self, provider, monkeypatch):
        monkeypatch.delenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", raising=False)
        assert provider.get_status("") == TerminalStatus.UNKNOWN


# ---------------------------------------------------------------------------
# Scripted-prompt mode ON
# ---------------------------------------------------------------------------


class TestScriptedPromptMode:
    """Scripted-prompt mode ON: APPROVAL_REQUIRED: triggers WAITING_USER_ANSWER."""

    def test_waiting_when_marker_present(self, provider, monkeypatch):
        """Buffer with marker and no answer -> WAITING_USER_ANSWER."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"Processing...\n{SCRIPTED_PROMPT_MARKER} Allow file write?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_waiting_with_whitespace_after_marker(self, provider, monkeypatch):
        """Only whitespace after marker -> still WAITING_USER_ANSWER."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"Output\n{SCRIPTED_PROMPT_MARKER}   \n  \n"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_clears_after_answer(self, provider, monkeypatch):
        """Text after marker on subsequent lines -> answer delivered, status clears."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = (
            f"Processing...\n"
            f"{SCRIPTED_PROMPT_MARKER} Allow?\n"
            f"y\n"  # Answer delivered
            f"\u276f "  # Idle prompt
        )
        status = provider.get_status(buffer)
        # Should return to normal (IDLE since it has the prompt)
        assert status == TerminalStatus.IDLE

    def test_clears_to_completed(self, provider, monkeypatch):
        """Answer + response marker -> COMPLETED."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = (
            f"Processing...\n"
            f"{SCRIPTED_PROMPT_MARKER} Allow?\n"
            f"y\n"  # Answer
            f"> MOCK: done\n"
            f"\u276f "
        )
        status = provider.get_status(buffer)
        assert status == TerminalStatus.COMPLETED

    def test_env_true_variant(self, provider, monkeypatch):
        """'true' value also enables scripted mode."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "true")
        buffer = f"{SCRIPTED_PROMPT_MARKER} Approve deletion?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_env_yes_variant(self, provider, monkeypatch):
        """'yes' value also enables scripted mode."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "yes")
        buffer = f"{SCRIPTED_PROMPT_MARKER} Approve deletion?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.WAITING_USER_ANSWER

    def test_error_still_takes_priority(self, provider, monkeypatch):
        """ERROR indicator still takes priority over scripted prompt."""
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")
        buffer = f"ERROR: mock failure injected\n{SCRIPTED_PROMPT_MARKER} Allow?"
        status = provider.get_status(buffer)
        assert status == TerminalStatus.ERROR


# ---------------------------------------------------------------------------
# Round-trip status transition
# ---------------------------------------------------------------------------


class TestStatusTransitionRoundTrip:
    """Full round-trip: IDLE -> WAITING -> answer -> IDLE/COMPLETED."""

    def test_full_round_trip(self, provider, monkeypatch):
        monkeypatch.setenv("CAO_MOCK_CLI_SCRIPTED_PROMPTS", "1")

        # Initial idle
        buffer_idle = "\u276f "
        assert provider.get_status(buffer_idle) == TerminalStatus.IDLE

        # Terminal shows approval prompt
        buffer_waiting = f"\u276f \n{SCRIPTED_PROMPT_MARKER} Execute rm -rf /tmp?"
        assert provider.get_status(buffer_waiting) == TerminalStatus.WAITING_USER_ANSWER

        # User delivers answer
        buffer_answered = (
            f"\u276f \n{SCRIPTED_PROMPT_MARKER} Execute rm -rf /tmp?\n"
            f"y\n"
            f"> MOCK: executed\n"
            f"\u276f "
        )
        assert provider.get_status(buffer_answered) == TerminalStatus.COMPLETED
