"""Q CLI provider implementation."""

import re
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.utils.terminal import wait_until_status, wait_for_shell

# Regex patterns for Q CLI output analysis (module-level constants)
GREEN_ARROW_PATTERN = r'\x1b\[38;5;10m>\s*\x1b\[39m'
ANSI_CODE_PATTERN = r'\x1b\[[0-9;]*m'
ESCAPE_SEQUENCE_PATTERN = r'\[[?0-9;]*[a-zA-Z]'
CONTROL_CHAR_PATTERN = r'[\x00-\x1f\x7f-\x9f]'
BELL_CHAR = '\x07'
GENERIC_PROMPT_PATTERN = r'\x1b\[38;5;13m>\s*\x1b\[39m\s*$'

# Error indicators
ERROR_INDICATORS = ["Amazon Q is having trouble responding right now"]


class QCliProvider(BaseProvider):
    """Provider for Q CLI tool integration."""
    
    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str):
        super().__init__(terminal_id, session_name, window_name)
        # TODO: remove the ._initialized if it's not referenced anywhere
        self._initialized = False
        self._agent_profile = agent_profile
        # Create dynamic prompt pattern based on agent profile
        self._idle_prompt_pattern = rf'\x1b\[38;5;14m\[{re.escape(self._agent_profile)}\]\s*\x1b\[38;5;13m>\s*\x1b\[39m\s*$'
        self._permission_prompt_pattern = r'Allow this action\?.*\[.*y.*\/.*n.*\/.*t.*\]:\x1b\[39m\s*' + self._idle_prompt_pattern
    
    def initialize(self) -> bool:
        """Initialize Q CLI provider by starting q chat command."""
        # Wait for shell to be ready first
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")
        
        command = f"q chat --agent {self._agent_profile}"
        tmux_client.send_keys(self.session_name, self.window_name, command)
        
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=30.0):
            raise TimeoutError("Q CLI initialization timed out after 30 seconds")
        
        self._initialized = True
        return True
    
    def get_status(self) -> TerminalStatus:
        """Get Q CLI status by analyzing terminal output."""
        output = tmux_client.get_history(self.session_name, self.window_name)
        
        if not output:
            return TerminalStatus.ERROR
        
        # Check for error indicators
        clean_output = re.sub(ANSI_CODE_PATTERN, '', output).lower()
        if any(indicator.lower() in clean_output for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR
        
        # Check for permission prompt
        if re.search(self._permission_prompt_pattern, output, re.MULTILINE | re.DOTALL):
            return TerminalStatus.WAITING_USER_ANSWER
        
        # Check for agent-specific prompt
        if re.search(self._idle_prompt_pattern, output):
            # Has response message = completed, no response = idle
            lines = output.split('\n')
            last_prompt_idx = -1
            last_arrow_idx = -1
            
            for i, line in enumerate(lines):
                if re.search(self._idle_prompt_pattern, line):
                    last_prompt_idx = i
                if re.search(GREEN_ARROW_PATTERN, line):
                    last_arrow_idx = i
            
            if last_arrow_idx != -1 and last_prompt_idx != -1 and last_arrow_idx < last_prompt_idx:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        
        # Check for generic prompt (invalid agent)
        if re.search(GENERIC_PROMPT_PATTERN, output):
            raise ValueError(f"Invalid agent profile '{self._agent_profile}' - Q CLI fell back to generic prompt")
        
        # No prompt = processing
        return TerminalStatus.PROCESSING
    
    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        matches = list(re.finditer(GREEN_ARROW_PATTERN, script_output))
        
        if not matches:
            raise ValueError("No Q CLI response found - no green arrow pattern detected")
        
        # Extract text after last green arrow until final prompt
        last_match = matches[-1]
        remaining_text = script_output[last_match.end():]
        lines = remaining_text.split('\n')
        final_lines = []
        found_final_prompt = False
        
        for line in lines:
            if re.search(self._idle_prompt_pattern, line):
                found_final_prompt = True
                break
            
            clean_line = line.strip()
            if not clean_line.startswith(BELL_CHAR):
                final_lines.append(clean_line)
        
        if not found_final_prompt:
            raise ValueError("Incomplete Q CLI response - no final prompt detected")
        
        if not final_lines or not any(line.strip() for line in final_lines):
            raise ValueError("Empty Q CLI response - no content found")
        
        # Clean up the message
        final_answer = '\n'.join(final_lines).strip()
        final_answer = re.sub(ANSI_CODE_PATTERN, '', final_answer)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, '', final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, '', final_answer)
        return final_answer.strip()
    
    # TODO: exit_cli should run the tmux.send_keys directly with /exit or ctrl-c twice
    def exit_cli(self) -> str:
        """Get the command to exit Q CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Q CLI provider."""
        # TODO: remove this cleanup method
        self._initialized = False
