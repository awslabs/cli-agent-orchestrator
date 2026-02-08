"""Kiro CLI provider implementation."""

import logging
import re
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Regex patterns for Kiro CLI output analysis (module-level constants)
GREEN_ARROW_PATTERN = r"^>\s*"  # Pattern for ANSI-cleaned output (start of line)
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"
BELL_CHAR = "\x07"
IDLE_PROMPT_PATTERN_LOG = r"\x1b\[38;5;\d+m\[.+?\].*\x1b\[38;5;\d+m>\s*\x1b\[\d*m"

# Error indicators
ERROR_INDICATORS = ["Kiro is having trouble responding right now"]


class KiroCliProvider(BaseProvider):
    """Provider for Kiro CLI tool integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile
        # Create dynamic prompt pattern based on agent profile (ANSI-free)
        # Matches: [agent] !> or [agent] > or [agent] X% > or [agent] λ > or [agent] X% λ >
        # after ANSI codes are stripped
        # Also matches with trailing text like "How can I help?"
        self._idle_prompt_pattern = (
            rf"\[{re.escape(self._agent_profile)}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*"
        )
        self._permission_prompt_pattern = (
            r"Allow this action\?.*\[.*y.*\/.*n.*\/.*t.*\]:\s*" + self._idle_prompt_pattern
        )

    def initialize(self) -> bool:
        """Initialize Kiro CLI provider by starting kiro-cli chat command."""
        # Wait for shell to be ready first
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = f"kiro-cli chat --agent {self._agent_profile}"
        tmux_client.send_keys(self.session_name, self.window_name, command)

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=30.0):
            raise TimeoutError("Kiro CLI initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output."""
        logger.debug(f"get_status: tail_lines={tail_lines}")
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # Strip ANSI codes once for all pattern matching
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check if we have the idle prompt (not processing)
        has_idle_prompt = re.search(self._idle_prompt_pattern, clean_output)

        if not has_idle_prompt:
            return TerminalStatus.PROCESSING

        # Check for error indicators
        if any(indicator.lower() in clean_output.lower() for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        # Check for permission prompt
        if re.search(self._permission_prompt_pattern, clean_output, re.MULTILINE | re.DOTALL):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check for completed state (has response + agent prompt AFTER the response)
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

        # Just agent prompt, no response
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
