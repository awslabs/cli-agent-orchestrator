"""OpenAutoGLM provider implementation."""

import json
import os
import re
from typing import List, Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_until_status


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""
    pass


# Regex patterns for OpenAutoGLM output analysis
THINKING_PATTERN = r"💭\s+(?:思考过程|Thinking):"  # Match both Chinese and English
ACTION_PATTERN = r"🎯\s+(?:执行动作|Action):"
RESULT_PATTERN = r"(?:最终结果|Final Result|任务结果|Task Result):"
ERROR_PATTERN = r"(?:错误|Error|失败|Failed|连接失败|Connection Failed)"
TASK_COMPLETED_PATTERN = r"(?:任务完成|Task Completed|完成|Done)"
IDLE_PROMPT_PATTERN = r"Enter your task:|Type 'quit' to exit|Goodbye!"
INTERACTIVE_MODE_PATTERN = r"Entering interactive mode|Type 'quit' to exit"


class OpenAutoGLMProvider(BaseProvider):
    """Provider for OpenAutoGLM CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        openautoglm_path: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile
        self._openautoglm_path = openautoglm_path or os.path.expanduser(
            "~/Workspace/work-assistant/projects/Open-AutoGLM/main.py"
        )

    def _build_openautoglm_command(self) -> str:
        """Build OpenAutoGLM command with environment configuration."""
        # Use python to run main.py from the OpenAutoGLM project
        command = f"cd {os.path.dirname(self._openautoglm_path)} && python main.py"

        # Check if we should run in interactive mode or with a task
        # For CAO integration, we'll use interactive mode
        return command

    def initialize(self) -> bool:
        """Initialize OpenAutoGLM provider by starting the CLI."""
        try:
            # Build command to start OpenAutoGLM
            command = self._build_openautoglm_command()

            # Send OpenAutoGLM command using tmux client
            tmux_client.send_keys(self.session_name, self.window_name, command)

            # Wait for interactive mode prompt to be ready
            # Give it more time as ADB setup might take longer
            if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=2.0):
                # Check if there's an error message
                output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=20)
                if re.search(ERROR_PATTERN, output, re.IGNORECASE):
                    raise ProviderError(f"OpenAutoGLM initialization failed with error: {output}")
                raise TimeoutError("OpenAutoGLM initialization timed out after 60 seconds")

            self._initialized = True
            return True

        except Exception as e:
            raise ProviderError(f"Failed to initialize OpenAutoGLM provider: {e}")

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenAutoGLM status by analyzing terminal output."""
        # Use tmux client to get window history
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # Check for error state first
        if re.search(ERROR_PATTERN, output, re.IGNORECASE):
            return TerminalStatus.ERROR

        # Check for processing state (thinking or executing actions)
        if re.search(THINKING_PATTERN, output):
            return TerminalStatus.PROCESSING

        if re.search(ACTION_PATTERN, output):
            return TerminalStatus.PROCESSING

        # Check for completed state (has result and ready for next input)
        if re.search(RESULT_PATTERN, output) or re.search(TASK_COMPLETED_PATTERN, output):
            # Check if we're back to the prompt
            if re.search(IDLE_PROMPT_PATTERN, output):
                return TerminalStatus.COMPLETED

        # Check for idle state (interactive mode ready for input)
        if re.search(INTERACTIVE_MODE_PATTERN, output) or re.search(IDLE_PROMPT_PATTERN, output):
            return TerminalStatus.IDLE

        # If no recognizable state, return ERROR
        return TerminalStatus.ERROR

    def get_idle_pattern_for_log(self) -> str:
        """Return OpenAutoGLM IDLE prompt pattern for log files."""
        return "Enter your task:"

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract OpenAutoGLM's final result message."""
        # Look for the last result message
        result_match = None
        for pattern in [RESULT_PATTERN, TASK_COMPLETED_PATTERN]:
            matches = list(re.finditer(pattern, script_output, re.IGNORECASE))
            if matches:
                result_match = matches[-1]
                break

        if not result_match:
            # If no explicit result marker, try to find the last meaningful output
            # Look for content after the last action
            action_matches = list(re.finditer(ACTION_PATTERN, script_output))
            if action_matches:
                last_action = action_matches[-1]
                # Extract everything after the action block
                start_pos = last_action.end()
                # Skip the JSON action block
                remaining = script_output[start_pos:]
                # Find the next content after the JSON
                json_match = re.search(r'\n\s*}\s*\n', remaining)
                if json_match:
                    start_pos = start_pos + json_match.end()
                    remaining_text = script_output[start_pos:]
                    # Extract meaningful content
                    lines = remaining_text.split('\n')
                    result_lines = []
                    for line in lines:
                        line = line.strip()
                        if line and not line.startswith('=') and not line.startswith('-'):
                            result_lines.append(line)
                        elif result_lines:  # Stop at first separator after content
                            break

                    if result_lines:
                        return '\n'.join(result_lines).strip()

            raise ValueError("No OpenAutoGLM result found in output")

        # Extract content after the result marker
        start_pos = result_match.end()
        remaining_text = script_output[start_pos:]

        # Clean up and return the result
        lines = remaining_text.split('\n')
        result_lines = []

        for line in lines:
            line = line.strip()
            # Skip empty lines and separators
            if not line or line.startswith('=') or line.startswith('-'):
                if result_lines:  # Stop if we already have content
                    break
                continue
            result_lines.append(line)

        if not result_lines:
            raise ValueError("Empty OpenAutoGLM result")

        return '\n'.join(result_lines).strip()

    def exit_cli(self) -> str:
        """Get the command to exit OpenAutoGLM."""
        return "quit"

    def cleanup(self) -> None:
        """Clean up OpenAutoGLM provider."""
        self._initialized = False