"""Q CLI provider implementation."""

import asyncio
import re
import subprocess
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.adapters.tmux import TmuxAdapter
from cli_agent_orchestrator.utils.terminal import wait_until_status

# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""
    pass

# Regex patterns for Q CLI output analysis
GREEN_ARROW_PATTERN = r'\x1b\[38;5;10m>\s*\x1b\[39m'
ANSI_CODE_PATTERN = r'\x1b\[[0-9;]*m'
ESCAPE_SEQUENCE_PATTERN = r'\[[?0-9;]*[a-zA-Z]'
CONTROL_CHAR_PATTERN = r'[\x00-\x1f\x7f-\x9f]'
BELL_CHAR = '\x07'

# Error indicators
ERROR_INDICATORS = ["Amazon Q is having trouble responding right now"]


class QCliProvider(BaseProvider):
    """Provider for Q CLI tool integration."""
    
    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile
        # Create dynamic prompt pattern based on agent profile - no fallback pattern
        self._q_cli_prompt_pattern = rf'\x1b\[38;5;14m\[{re.escape(self._agent_profile)}\]\s*\x1b\[38;5;13m>\s*\x1b\[39m\s*$'
    
    async def initialize(self) -> bool:
        """Initialize Q CLI provider by starting q chat command."""
        # Send Q CLI start command to tmux
        command = f"q chat --agent {self._agent_profile}"
        subprocess.run([
            "tmux", "send-keys", "-t", f"{self.session_name}:{self.window_name}", 
            command, "Enter"
        ], check=True)
        
        # Wait for Q CLI prompt to be ready using status check
        if not await wait_until_status(self, TerminalStatus.IDLE, timeout=30.0):
            raise TimeoutError("Q CLI initialization timed out after 30 seconds")
        
        self._initialized = True
        return True
    
    async def get_status(self) -> TerminalStatus:
        """Get Q CLI status by analyzing terminal output."""
        
        # Use tmux adapter to get window history
        tmux_adapter = TmuxAdapter()
        output = tmux_adapter.get_window_history(self.session_name, self.window_name)
        
        if not output:
            return TerminalStatus.ERROR
        
        # Check for error indicators first
        if self._detect_error(output):
            return TerminalStatus.ERROR
        
        # Check for waiting user answer (tool permission prompt)
        if self._is_waiting_for_permission(output):
            return TerminalStatus.WAITING_USER_ANSWER
        
        # Check for Q CLI prompt pattern (agent-specific only)
        if re.search(self._q_cli_prompt_pattern, output):
            # Check if there's a response message (completed) vs just ready
            if self._has_response_message(output):
                return TerminalStatus.COMPLETED
            else:
                return TerminalStatus.IDLE  # Ready state
        
        # Check for generic prompt pattern indicating invalid agent
        if re.search(r'\x1b\[38;5;13m>\s*\x1b\[39m\s*$', output):
            raise ProviderError(f"Invalid agent profile '{self._agent_profile}' - Q CLI fell back to generic prompt")
        
        # No prompt detected = processing
        return TerminalStatus.PROCESSING
    
    def _is_waiting_for_permission(self, output: str) -> bool:
        """Check if Q CLI is waiting for permission by examining if permission prompt ends the output."""
        # Look for "Allow this action" followed by permission prompt and clean agent prompt at end of string
        pattern = r'Allow this action\?.*\[.*y.*\/.*n.*\/.*t.*\]:\x1b\[39m\s*' + self._q_cli_prompt_pattern
        return re.search(pattern, output, re.MULTILINE | re.DOTALL) is not None
    
    def _has_response_message(self, output: str) -> bool:
        """Check if output contains a response message (green arrow pattern followed by prompt)."""
        lines = output.split('\n')
        
        # Single pass: find the last prompt and last green arrow
        last_prompt_index = -1
        last_green_arrow_index = -1
        
        for i, line in enumerate(lines):
            if re.search(self._q_cli_prompt_pattern, line):
                last_prompt_index = i
            if re.search(GREEN_ARROW_PATTERN, line):
                last_green_arrow_index = i
        
        # Response message exists if green arrow comes before the last prompt
        return last_green_arrow_index != -1 and last_prompt_index != -1 and last_green_arrow_index < last_prompt_index
    
    def _detect_error(self, output: str) -> bool:
        """Detect if Q CLI output contains error indicators."""
        # Strip ANSI codes for clean text analysis
        clean_output = re.sub(ANSI_CODE_PATTERN, '', output).lower()
        return any(indicator in clean_output for indicator in ERROR_INDICATORS)
    
    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Find all matches of green arrow pattern
        matches = list(re.finditer(GREEN_ARROW_PATTERN, script_output))
        
        if not matches:
            raise ValueError("No Q CLI response found - no green arrow pattern detected")
        
        # Get the last match (final answer)
        last_match = matches[-1]
        start_pos = last_match.end()
        
        # Extract everything after the last green arrow until the end or next prompt
        remaining_text = script_output[start_pos:]
        
        # Split by lines and clean up
        lines = remaining_text.split('\n')
        final_lines = []
        found_final_prompt = False
        
        for line in lines:
            # Check if we found the final Q CLI prompt pattern (agent-specific only)
            if re.search(self._q_cli_prompt_pattern, line):
                found_final_prompt = True
                break
            
            # Clean the line but preserve empty lines for paragraph breaks
            clean_line = line.strip()
            if not clean_line.startswith(BELL_CHAR):  # Skip bell characters
                final_lines.append(clean_line)  # Include empty lines
        
        # Only return extracted message if we found a complete response (with final prompt)
        if not found_final_prompt:
            raise ValueError("Incomplete Q CLI response - no final prompt detected, response may still be processing")
        
        if not final_lines or not any(line.strip() for line in final_lines):
            raise ValueError("Empty Q CLI response - no content found between green arrow and final prompt")
        
        # Join lines and clean up extra whitespace
        final_answer = '\n'.join(final_lines).strip()
        # Remove all ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, '', final_answer)
        # Remove any remaining escape sequences like [?25h[?2004h[K
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, '', final_answer)
        # Remove any remaining control characters
        final_answer = re.sub(CONTROL_CHAR_PATTERN, '', final_answer)
        return final_answer.strip()
    
    def exit_cli(self) -> str:
        """Get the command to exit Q CLI."""
        return "/exit"

    async def cleanup(self) -> None:
        """Clean up Q CLI provider."""
        self._initialized = False
