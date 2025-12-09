"""Codex CLI provider implementation."""

import logging
import re
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:^❯\s*$|^›\s*$|^>\s*$|^codex>\s*$|^You>?\s*$)"
IDLE_PROMPT_PATTERN_LOG = r"❯"
ASSISTANT_PREFIX_PATTERN = r"^(?:assistant|codex|agent)\s*:"
PROCESSING_PATTERN = r"(thinking|working|running|executing|processing|analyzing)"
WAITING_PROMPT_PATTERN = r"(approve|allow).*(y/n|yes|no)"
ERROR_INDICATORS = ["error", "failed", "panic"]


class CodexProvider(BaseProvider):
    """Provider for Codex CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile

    def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        tmux_client.send_keys(self.session_name, self.window_name, "codex")

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=1.0):
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Codex status by analyzing terminal output."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
        lowered_output = clean_output.lower()

        if any(indicator in lowered_output for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        if re.search(WAITING_PROMPT_PATTERN, clean_output, re.IGNORECASE | re.DOTALL):
            return TerminalStatus.WAITING_USER_ANSWER

        idle_match = re.search(IDLE_PROMPT_PATTERN, clean_output, re.MULTILINE)
        if idle_match:
            if re.search(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        if re.search(PROCESSING_PATTERN, clean_output, re.IGNORECASE):
            return TerminalStatus.PROCESSING

        return TerminalStatus.ERROR

    def get_idle_pattern_for_log(self) -> str:
        """Return Codex IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response message using assistant label markers."""
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(IDLE_PROMPT_PATTERN, clean_output[start_pos:], re.MULTILINE)
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
