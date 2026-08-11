# Provider Test Guide

How to write tests for a new CAO provider. Tests follow a consistent structure across all providers.

## Directory Structure

```
test/providers/
├── test_new_cli_unit.py           # Unit tests (fast, mocked tmux)
├── test_new_cli_coverage.py       # Additional coverage tests (optional)
├── fixtures/
│   ├── new_cli_idle.txt           # Terminal output when CLI is idle
│   ├── new_cli_processing.txt     # Terminal output during processing
│   ├── new_cli_completed.txt      # Terminal output after task completion
│   ├── new_cli_waiting.txt        # Terminal output with permission prompt
│   └── new_cli_response.txt       # Full output with extractable response
└── README.md
```

## Creating Fixtures

Capture real terminal output from the CLI tool. Use tmux's capture-pane:

```bash
# Start the CLI in tmux
tmux new-session -d -s test
tmux send-keys -t test "new-cli" Enter

# After CLI is idle, capture
tmux capture-pane -t test -p -S -200 > test/providers/fixtures/new_cli_idle.txt

# Send a task, capture during processing
tmux send-keys -t test "Hello, what is 2+2?" Enter
# Quickly capture while spinner is showing
tmux capture-pane -t test -p -S -200 > test/providers/fixtures/new_cli_processing.txt

# Wait for completion, capture
tmux capture-pane -t test -p -S -200 > test/providers/fixtures/new_cli_completed.txt
```

Include ANSI codes in fixtures (`-e` flag) for realistic testing:
```bash
tmux capture-pane -t test -e -p -S -200 > test/providers/fixtures/new_cli_with_ansi.txt
```

## Unit Test Structure

```python
"""Unit tests for New CLI provider."""

import re
from unittest.mock import MagicMock, patch

import pytest

from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.new_cli import (
    ANSI_CODE_PATTERN,
    IDLE_PROMPT_PATTERN,
    PROCESSING_PATTERN,
    RESPONSE_PATTERN,
    NewCliProvider,
)


@pytest.fixture
def mock_tmux():
    """Mock tmux_client for unit tests."""
    with patch("cli_agent_orchestrator.providers.new_cli.tmux_client") as mock:
        mock.get_history.return_value = ""
        mock.send_keys.return_value = None
        yield mock


@pytest.fixture
def provider(mock_tmux):
    """Create a provider instance with mocked tmux."""
    return NewCliProvider(
        terminal_id="test-123",
        session_name="test-session",
        window_name="test-window",
        agent_profile="developer",
    )


# --- Initialization Tests ---

class TestInitialization:
    def test_successful_init(self, provider, mock_tmux):
        """Provider initializes and waits for idle prompt."""
        # Set up mock to return idle prompt after command is sent
        mock_tmux.get_history.return_value = "$ \nnew-cli\n> "  # Shell + idle prompt
        with patch(
            "cli_agent_orchestrator.providers.new_cli.wait_for_shell", return_value=True
        ), patch(
            "cli_agent_orchestrator.providers.new_cli.wait_until_status", return_value=True
        ):
            result = provider.initialize()
        assert result is True
        assert provider._initialized is True

    def test_shell_timeout(self, provider, mock_tmux):
        """Provider raises TimeoutError if shell doesn't appear."""
        with patch(
            "cli_agent_orchestrator.providers.new_cli.wait_for_shell", return_value=False
        ):
            with pytest.raises(TimeoutError, match="Shell initialization"):
                provider.initialize()

    def test_cli_timeout(self, provider, mock_tmux):
        """Provider raises TimeoutError if CLI doesn't start."""
        with patch(
            "cli_agent_orchestrator.providers.new_cli.wait_for_shell", return_value=True
        ), patch(
            "cli_agent_orchestrator.providers.new_cli.wait_until_status", return_value=False
        ):
            with pytest.raises(TimeoutError, match="CLI initialization"):
                provider.initialize()


# --- Status Detection Tests ---

class TestStatusDetection:
    def test_idle_status(self, provider, mock_tmux):
        mock_tmux.get_history.return_value = "Welcome to New CLI\n> "
        assert provider.get_status() == TerminalStatus.IDLE

    def test_processing_status(self, provider, mock_tmux):
        mock_tmux.get_history.return_value = "✽ Thinking..."
        assert provider.get_status() == TerminalStatus.PROCESSING

    def test_completed_status(self, provider, mock_tmux):
        """COMPLETED = response marker + idle prompt both present."""
        mock_tmux.get_history.return_value = "⏺ The answer is 4.\n> "
        assert provider.get_status() == TerminalStatus.COMPLETED

    def test_waiting_user_answer(self, provider, mock_tmux):
        mock_tmux.get_history.return_value = "Allow this action? [Y/n]"
        assert provider.get_status() == TerminalStatus.WAITING_USER_ANSWER

    def test_error_on_empty_output(self, provider, mock_tmux):
        mock_tmux.get_history.return_value = ""
        assert provider.get_status() == TerminalStatus.ERROR

    def test_error_on_no_recognizable_state(self, provider, mock_tmux):
        mock_tmux.get_history.return_value = "some random text"
        assert provider.get_status() == TerminalStatus.ERROR

    def test_completed_takes_priority_over_processing(self, provider, mock_tmux):
        """Critical: old spinner in buffer + idle prompt = COMPLETED, not PROCESSING."""
        mock_tmux.get_history.return_value = (
            "✽ Thinking...\n"  # Old spinner from earlier
            "⏺ The answer is 4.\n"  # Response
            "> "  # Idle prompt
        )
        assert provider.get_status() == TerminalStatus.COMPLETED


# --- Message Extraction Tests ---

class TestMessageExtraction:
    def test_successful_extraction(self, provider):
        output = "Some preamble\n⏺ Hello, the answer is 42.\n> "
        result = provider.extract_last_message_from_script(output)
        assert "42" in result

    def test_no_response_marker_raises(self, provider):
        with pytest.raises(ValueError, match="No response found"):
            provider.extract_last_message_from_script("just some text\n> ")

    def test_empty_response_raises(self, provider):
        with pytest.raises(ValueError, match="Empty response"):
            provider.extract_last_message_from_script("⏺ \n> ")

    def test_multiple_responses_uses_last(self, provider):
        output = "⏺ First answer\n> \n⏺ Second answer\n> "
        result = provider.extract_last_message_from_script(output)
        assert "Second" in result

    def test_ansi_codes_stripped(self, provider):
        output = "⏺ \x1b[32mHello\x1b[0m world\n> "
        result = provider.extract_last_message_from_script(output)
        assert "\x1b" not in result
        assert "Hello" in result


# --- Regex Pattern Tests ---

class TestRegexPatterns:
    def test_idle_pattern_matches(self):
        assert re.search(IDLE_PROMPT_PATTERN, "> ")
        assert re.search(IDLE_PROMPT_PATTERN, "some text\n> ")

    def test_processing_pattern_matches(self):
        assert re.search(PROCESSING_PATTERN, "✽ Thinking...")

    def test_ansi_stripping(self):
        text = "\x1b[32mHello\x1b[0m"
        clean = re.sub(ANSI_CODE_PATTERN, "", text)
        assert clean == "Hello"


# --- Edge Cases ---

class TestEdgeCases:
    def test_exit_command(self, provider):
        assert provider.exit_cli() == "/exit"

    def test_idle_pattern_for_log(self, provider):
        pattern = provider.get_idle_pattern_for_log()
        assert isinstance(pattern, str)
        assert len(pattern) > 0

    def test_cleanup(self, provider):
        provider._initialized = True
        provider.cleanup()
        assert provider._initialized is False

    def test_paste_enter_count(self, provider):
        assert provider.paste_enter_count in (1, 2)
```

## E2E Test Classes

Add these test classes to the existing e2e test files. Each follows the same pattern as existing providers.

### test/e2e/conftest.py

```python
@pytest.fixture()
def require_new_cli():
    """Skip test if new-cli is not available."""
    if not _cli_available("new-cli"):
        pytest.skip("new-cli CLI not installed")
```

### test/e2e/test_allowed_tools.py

```python
@pytest.mark.e2e
class TestNewCliAllowedTools:
    def test_restricted_supervisor_cannot_bash(self, require_new_cli):
        _run_restricted_tool_test("new_cli", "supervisor", "@cao-mcp-server,fs_read,fs_list")

    def test_unrestricted_developer_can_bash(self, require_new_cli):
        _run_unrestricted_tool_test("new_cli", "developer", "@builtin,fs_*,execute_bash,@cao-mcp-server")

    def test_allowed_tools_stored_in_metadata(self, require_new_cli):
        _run_allowed_tools_stored_test("new_cli", "developer", "@builtin,fs_*,execute_bash,@cao-mcp-server")
```

### test/e2e/test_assign.py, test_handoff.py, test_send_message.py, test_supervisor_orchestration.py

Follow the same pattern — create a test class per provider, use the `require_new_cli` fixture, and call the shared `_run_*` helper functions with `provider="new_cli"`.

## Running Tests

```bash
# Unit tests only (fast, no CLI needed)
uv run pytest test/providers/test_new_cli_unit.py -v -o "addopts="

# E2E tests (requires running cao-server + CLI installed)
uv run pytest test/e2e/ -v -k "NewCli" -o "addopts="

# Check coverage
uv run pytest test/providers/test_new_cli_unit.py --cov=src/cli_agent_orchestrator/providers/new_cli --cov-report=term-missing -o "addopts="
```

## Coverage Target

Aim for 100% coverage of the provider class. The key areas:
- Every branch in `get_status()` (each TerminalStatus return)
- Every branch in `extract_last_message_from_script()` (success + all error paths)
- `initialize()` (success + both timeout paths)
- Edge cases (empty output, ANSI codes, Unicode)
