"""Claude Code provider implementation."""

import re
import shlex
from typing import List, Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_until_status


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# Regex patterns for Claude Code output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
RESPONSE_PATTERN = r"⏺(?:\x1b\[[0-9;]*m)*\s+"  # Handle any ANSI codes between marker and text
PROCESSING_PATTERN = r"[✶✢✽✻·✳].*….*\(esc to interrupt.*\)"
IDLE_PROMPT_PATTERN = r">[\s\xa0]"  # Handle both regular space and non-breaking space
WAITING_USER_ANSWER_PATTERN = (
    r"❯.*\d+\."  # Pattern for Claude showing selection options with arrow cursor
)
IDLE_PROMPT_PATTERN_LOG = r">[\s\xa0]"  # Same pattern for log files


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code CLI tool integration."""

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

    def _build_claude_command(self) -> List[str]:
        """Build Claude Code command with agent profile if provided."""
        command_parts = ["claude"]

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Add system prompt with proper escaping
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                command_parts.extend(["--append-system-prompt", shlex.quote(system_prompt)])

                # Add MCP config if present
                if profile.mcpServers:
                    mcp_json = profile.model_dump_json(include={"mcpServers"})
                    command_parts.extend(["--mcp-config", shlex.quote(mcp_json)])

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        return command_parts

    def initialize(self) -> bool:
        """Initialize Claude Code provider by starting claude command."""
        # Build command with agent profile support
        command_parts = self._build_claude_command()
        command = " ".join(command_parts)

        # Send Claude Code command using tmux client
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Wait for Claude Code prompt to be ready
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=30.0, polling_interval=1.0):
            raise TimeoutError("Claude Code initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Claude Code status by analyzing terminal output."""

        # Use tmux client singleton to get window history
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # Check for processing state first
        if re.search(PROCESSING_PATTERN, output):
            return TerminalStatus.PROCESSING

        # Check for waiting user answer (Claude asking for user selection)
        if re.search(WAITING_USER_ANSWER_PATTERN, output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check for completed state (has response + ready prompt)
        if re.search(RESPONSE_PATTERN, output) and re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.COMPLETED

        # Check for idle state (just ready prompt, no response)
        if re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.IDLE

        # If no recognizable state, return ERROR
        return TerminalStatus.ERROR

    def get_idle_pattern_for_log(self) -> str:
        """Return Claude Code IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Claude's final response message using ⏺ indicator."""
        # Find all matches of response pattern
        matches = list(re.finditer(RESPONSE_PATTERN, script_output))

        if not matches:
            raise ValueError("No Claude Code response found - no ⏺ pattern detected")

        # Get the last match (final answer)
        last_match = matches[-1]
        start_pos = last_match.end()

        # Extract everything after the last ⏺ until next prompt or separator
        remaining_text = script_output[start_pos:]

        # Split by lines and extract response
        lines = remaining_text.split("\n")
        response_lines = []

        for line in lines:
            # Stop at next > prompt or separator line
            if re.match(r">\s", line) or "────────" in line:
                break

            # Clean the line
            clean_line = line.strip()
            response_lines.append(clean_line)

        if not response_lines or not any(line.strip() for line in response_lines):
            raise ValueError("Empty Claude Code response - no content found after ⏺")

        # Join lines and clean up
        final_answer = "\n".join(response_lines).strip()
        # Remove ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        return final_answer.strip()

    def exit_cli(self) -> str:
        """Get the command to exit Claude Code."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Claude Code provider."""
        self._initialized = False
