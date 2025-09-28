"""Claude Code provider implementation."""

import asyncio
import re
import subprocess
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.adapters.tmux import TmuxAdapter

# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""
    pass

# Regex patterns for Claude Code output analysis
ANSI_CODE_PATTERN = r'\x1b\[[0-9;]*m'
RESPONSE_PATTERN = r'⏺(?:\x1b\[[0-9;]*m)*\s+'  # Handle any ANSI codes between marker and text
PROCESSING_PATTERN = r'[✶✢].*….*\(esc to interrupt\)'
IDLE_PROMPT_PATTERN = r'>[\s\xa0]'  # Handle both regular space and non-breaking space


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code CLI tool integration."""
    
    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: str = None):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        # Claude Code doesn't use agent profiles like Q CLI
    
    # TODO: add the ability to launch with --agents <json> and --mcp-config and allowed tools
    async def initialize(self) -> bool:
        """Initialize Claude Code provider by starting claude command."""
        # Send Claude Code start command to tmux
        command = "claude"
        subprocess.run([
            "tmux", "send-keys", "-t", f"{self.session_name}:{self.window_name}", 
            command, "C-m"
        ], check=True)
        
        # Wait for Claude Code prompt to be ready
        max_attempts = 60  # 30 seconds total
        for _ in range(max_attempts):
            await asyncio.sleep(0.5)
            status = await self.get_status()
            if status == TerminalStatus.IDLE:
                self._initialized = True
                return True
        
        raise TimeoutError(f"Claude Code initialization timed out after {max_attempts * 0.5} seconds")
    
    async def get_status(self) -> TerminalStatus:
        """Get Claude Code status by analyzing terminal output."""
        
        # Use tmux adapter to get window history
        tmux_adapter = TmuxAdapter()
        output = tmux_adapter.get_window_history(self.session_name, self.window_name)
        
        if not output:
            return TerminalStatus.ERROR
        
        # Check for processing state first
        if re.search(PROCESSING_PATTERN, output):
            return TerminalStatus.PROCESSING
        
        # Check for completed state (has response + ready prompt)
        if re.search(RESPONSE_PATTERN, output) and re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.COMPLETED
        
        # Check for idle state (just ready prompt, no response)
        if re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.IDLE
        
        # If no recognizable state, return None
        return None
    
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
        lines = remaining_text.split('\n')
        response_lines = []
        
        for line in lines:
            # Stop at next > prompt or separator line
            if re.match(r'>\s', line) or '────────' in line:
                break
            
            # Clean the line
            clean_line = line.strip()
            response_lines.append(clean_line)
        
        if not response_lines or not any(line.strip() for line in response_lines):
            raise ValueError("Empty Claude Code response - no content found after ⏺")
        
        # Join lines and clean up
        final_answer = '\n'.join(response_lines).strip()
        # Remove ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, '', final_answer)
        return final_answer.strip()
    
    def exit_cli(self) -> str:
        """Get the command to exit Claude Code."""
        return "/exit"

    async def cleanup(self) -> None:
        """Clean up Claude Code provider."""
        self._initialized = False
