"""Kiro CLI provider implementation.

This module provides the KiroCliProvider class for integrating with Kiro CLI,
an AI-powered coding assistant that operates through a terminal interface.

Kiro CLI Features:
- Agent-based conversations with customizable profiles
- File system access and code manipulation capabilities
- Interactive permission prompts for sensitive operations
- ANSI-colored output with distinctive prompt patterns

The provider detects the following terminal states:
- IDLE: Agent is waiting for user input (shows agent prompt)
- PROCESSING: Agent is generating a response
- COMPLETED: Agent has finished responding (shows green arrow + response)
- WAITING_USER_ANSWER: Agent is waiting for permission confirmation
- ERROR: Agent encountered an error during processing
"""

import logging
import re
import shlex
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns for Kiro CLI Output Analysis
# =============================================================================

# Green arrow pattern indicates the start of an agent response (ANSI-stripped)
# Example: "> Here is the code you requested..."
GREEN_ARROW_PATTERN = r"^>\s*"

# ANSI escape code pattern for stripping terminal colors
# Matches sequences like \x1b[32m (green), \x1b[0m (reset), etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Additional escape sequences that may appear in terminal output
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"

# Control characters to strip from final output
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"

# Bell character (audible alert)
BELL_CHAR = "\x07"

# Generic prompt pattern with ANSI codes (purple arrow)
# Used for detecting IDLE state in raw terminal output
GENERIC_PROMPT_PATTERN = r"\x1b\[38;5;13m>\s*\x1b\[39m\s*$"

# IDLE prompt pattern for log file monitoring
# Matches the colored prompt indicator in pipe-pane output (flexible color code)
IDLE_PROMPT_PATTERN_LOG = r"\x1b\[38;5;\d+m\[.+?\].*\x1b\[38;5;\d+m>\s*\x1b\[\d*m"

# =============================================================================
# Error Detection
# =============================================================================

# Strings that indicate the agent encountered an error
ERROR_INDICATORS = ["Kiro is having trouble responding right now"]


class KiroCliProvider(BaseProvider):
    """Provider for Kiro CLI tool integration.

    This provider manages the lifecycle of a Kiro CLI chat session within a tmux window,
    including initialization, status detection, and response extraction.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the Kiro agent profile to use
        _idle_prompt_pattern: Regex pattern for detecting IDLE state
        _permission_prompt_pattern: Regex pattern for detecting permission prompts
    """

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        """Initialize Kiro CLI provider with terminal context.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the Kiro agent profile to use (e.g., "developer")
        """
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile

        # Build dynamic prompt pattern based on agent profile
        # This pattern matches various Kiro prompt formats after ANSI stripping:
        # - [developer] >       (basic prompt)
        # - [developer] !>      (prompt with pending changes)
        # - [developer] 50% >   (prompt with progress indicator)
        # - [developer] λ >     (prompt with lambda symbol)
        # - [developer] 50% λ > (combined progress and lambda)
        self._idle_prompt_pattern = (
            rf"\[{re.escape(self._agent_profile)}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*"
        )

        # Permission prompt pattern for detecting when agent needs user confirmation
        # Example: "Allow this action? [y/n/t]: [developer] >"
        self._permission_prompt_pattern = (
            r"Allow this action\?.*\[.*y.*\/.*n.*\/.*t.*\]:[ \t]*" + self._idle_prompt_pattern
        )

    def initialize(self) -> bool:
        """Initialize Kiro CLI provider by starting kiro-cli chat command.

        This method:
        1. Waits for the shell to be ready in the tmux window
        2. Sends the kiro-cli chat command with the configured agent profile
        3. Waits for the agent to reach IDLE state (ready for input)

        Returns:
            True if initialization was successful

        Raises:
            TimeoutError: If shell or Kiro CLI initialization times out
        """
        # Step 1: Wait for shell prompt to appear in the tmux window
        # This ensures the terminal is ready before we send commands
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Step 2: Start the Kiro CLI chat session with the specified agent profile
        command = shlex.join(["kiro-cli", "chat", "--agent", self._agent_profile])
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Step 3: Wait for Kiro CLI to fully initialize and show the agent prompt
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=30.0):
            raise TimeoutError("Kiro CLI initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output.

        Status detection logic (in priority order):
        1. No output → ERROR
        2. No IDLE prompt visible → PROCESSING (agent is generating response)
        3. Error indicators present → ERROR
        4. Permission prompt visible → WAITING_USER_ANSWER
        5. Green arrow + prompt visible → COMPLETED (response ready)
        6. Only prompt visible → IDLE (waiting for input)

        Args:
            tail_lines: Number of lines to capture from terminal history.
                        If None, uses default from tmux_client.

        Returns:
            Current TerminalStatus enum value
        """
        logger.debug(f"get_status: tail_lines={tail_lines}")
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        # No output indicates a terminal error
        if not output:
            return TerminalStatus.ERROR

        # Strip ANSI codes once for all pattern matching
        # This simplifies regex patterns and improves reliability
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check 1: Look for the agent's IDLE prompt pattern
        # If not found, the agent is still processing a response
        has_idle_prompt = re.search(self._idle_prompt_pattern, clean_output)

        if not has_idle_prompt:
            return TerminalStatus.PROCESSING

        # Check 2: Look for known error messages in the output
        if any(indicator.lower() in clean_output.lower() for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        # Check 3: Look for permission prompts (y/n/t confirmation requests)
        if re.search(self._permission_prompt_pattern, clean_output, re.MULTILINE | re.DOTALL):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check 4: Look for completed response (green arrow indicates agent output)
        # Must verify that an idle prompt appears AFTER the response
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        if green_arrows:
            # Find if there's an idle prompt after the last green arrow
            last_arrow_pos = green_arrows[-1].end()
            idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))

            for prompt in idle_prompts:
                if prompt.start() > last_arrow_pos:
                    logger.debug(f"get_status: returning COMPLETED")
                    return TerminalStatus.COMPLETED

            # Has green arrow but no prompt after it - still processing
            return TerminalStatus.PROCESSING

        # Default: Agent is IDLE, waiting for user input
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Strip ANSI codes for pattern matching
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Find patterns in clean output
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))

        if not green_arrows:
            raise ValueError("No Kiro CLI response found - no green arrow pattern detected")

        if not idle_prompts:
            raise ValueError("Incomplete Kiro CLI response - no final prompt detected")

        # Find the last green arrow (response start)
        last_arrow_pos = green_arrows[-1].end()

        # Find idle prompt that comes AFTER the last green arrow
        final_prompt = None
        for prompt in idle_prompts:
            if prompt.start() > last_arrow_pos:
                final_prompt = prompt
                break

        if not final_prompt:
            raise ValueError(
                "Incomplete Kiro CLI response - no final prompt detected after response"
            )

        # Extract directly from clean output
        start_pos = last_arrow_pos
        end_pos = final_prompt.start()

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        # Clean up the message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return Kiro CLI IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def exit_cli(self) -> str:
        """Get the command to exit Kiro CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Kiro CLI provider."""
        self._initialized = False
