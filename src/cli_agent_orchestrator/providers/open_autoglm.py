"""OpenAutoGLM provider implementation for mobile device automation."""

import re
import shlex
from typing import List, Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_until_status


class ProviderError(Exception):
    """Exception raised for OpenAutoGLM provider-specific errors."""

    pass


# Regex patterns for OpenAutoGLM output analysis
ADB_DEVICE_PATTERN = r"adb.*device.*found"
ADB_ERROR_PATTERN = r"adb.*error|device.*not.*found|no.*devices"
THINKING_PATTERN = r"thinking.*\.\.\.|processing.*\.\.\."
EXECUTING_PATTERN = r"executing.*action|running.*command"
COMPLETION_PATTERN = r"task.*completed|action.*finished|done"
IDLE_PROMPT_PATTERN = r"OpenAutoGLM.*>|\[autoglm\].*>"
ERROR_PATTERN = r"error:|exception:|failed:|traceback"


class OpenAutoGLMProvider(BaseProvider):
    """Provider for OpenAutoGLM mobile device automation integration."""

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
        self._device_id = None
        self._api_endpoint = None

    def _build_autoglm_command(self) -> List[str]:
        """Build OpenAutoGLM command with configuration."""
        command_parts = ["python3", "-m", "open_autoglm.cli"]

        # Add device configuration if available
        if self._device_id:
            command_parts.extend(["--device", self._device_id])

        # Add API endpoint configuration if available
        if self._agent_profile:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Extract OpenAutoGLM specific configuration from profile
                if hasattr(profile, 'open_autoglm_config'):
                    config = profile.open_autoglm_config

                    if config.get('api_endpoint'):
                        command_parts.extend(["--api-endpoint", config['api_endpoint']])

                    if config.get('model_name'):
                        command_parts.extend(["--model", config['model_name']])

                    if config.get('device_id'):
                        command_parts.extend(["--device", config['device_id']])
                        self._device_id = config['device_id']

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        return command_parts

    def initialize(self) -> bool:
        """Initialize OpenAutoGLM provider by starting the CLI."""
        try:
            # Build command with configuration
            command_parts = self._build_autoglm_command()
            command = " ".join(shlex.quote(part) for part in command_parts)

            # Start OpenAutoGLM in interactive mode
            full_command = f"{command} --interactive"

            # Send command using tmux client
            tmux_client.send_keys(self.session_name, self.window_name, full_command)

            # Wait for OpenAutoGLM prompt to be ready
            if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=2.0):
                raise TimeoutError("OpenAutoGLM initialization timed out after 60 seconds")

            self._initialized = True
            return True

        except Exception as e:
            raise ProviderError(f"Failed to initialize OpenAutoGLM: {e}")

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get OpenAutoGLM status by analyzing terminal output."""

        # Use tmux client to get window history
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # Convert to lowercase for case-insensitive matching
        output_lower = output.lower()

        # Check for ADB device errors first
        if re.search(ADB_ERROR_PATTERN, output_lower):
            return TerminalStatus.ERROR

        # Check for processing/thinking state
        if re.search(THINKING_PATTERN, output_lower) or re.search(EXECUTING_PATTERN, output_lower):
            return TerminalStatus.PROCESSING

        # Check for completion state
        if re.search(COMPLETION_PATTERN, output_lower) and re.search(IDLE_PROMPT_PATTERN, output_lower):
            return TerminalStatus.COMPLETED

        # Check for error patterns
        if re.search(ERROR_PATTERN, output_lower):
            return TerminalStatus.ERROR

        # Check for idle state (ready prompt)
        if re.search(IDLE_PROMPT_PATTERN, output_lower):
            return TerminalStatus.IDLE

        # If no recognizable state, return PROCESSING (might be starting up)
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        """Return OpenAutoGLM IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract OpenAutoGLM's final response message."""

        # Split by lines and process
        lines = script_output.split("\n")
        response_lines = []
        found_response = False

        # Look for completion indicators and extract content
        for line in lines:
            line_lower = line.lower()

            # Check for completion/start of response
            if re.search(COMPLETION_PATTERN, line_lower) or "result:" in line_lower:
                found_response = True
                continue

            # If we found response content, collect it
            if found_response:
                # Stop at next prompt
                if re.search(IDLE_PROMPT_PATTERN, line):
                    break

                # Skip empty lines and common debug messages
                if line.strip() and not line.startswith("[DEBUG]") and not line.startswith("[INFO]"):
                    response_lines.append(line.strip())

        # If no completion pattern found, try to extract last meaningful output
        if not response_lines:
            # Look backwards for the last substantial output
            for line in reversed(lines):
                line_stripped = line.strip()
                if (line_stripped and
                    not re.search(IDLE_PROMPT_PATTERN, line) and
                    not line.startswith("[") and
                    not re.match(r"^(thinking|processing|executing)", line_lower)):
                    response_lines.insert(0, line_stripped)
                    break

        if not response_lines:
            raise ValueError("No OpenAutoGLM response found - no recognizable output pattern detected")

        # Join lines and clean up
        final_answer = "\n".join(response_lines).strip()
        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit OpenAutoGLM."""
        return "exit"

    def cleanup(self) -> None:
        """Clean up OpenAutoGLM provider resources."""
        try:
            if self._initialized:
                # Send exit command to gracefully shutdown
                tmux_client.send_keys(self.session_name, self.window_name, self.exit_cli())
                self._initialized = False
        except Exception as e:
            # Log error but don't raise to avoid blocking cleanup
            pass

    def set_device_id(self, device_id: str) -> None:
        """Set the target Android device ID."""
        self._device_id = device_id

    def set_api_endpoint(self, api_endpoint: str) -> None:
        """Set the AutoGLM API endpoint."""
        self._api_endpoint = api_endpoint

    def check_device_connection(self) -> bool:
        """Check if Android device is connected via ADB."""
        try:
            # Send ADB device check command
            tmux_client.send_keys(self.session_name, self.window_name, "adb devices")

            # Wait a moment for output
            import time
            time.sleep(2)

            # Check output for connected device
            output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=10)
            return bool(re.search(ADB_DEVICE_PATTERN, output.lower()))

        except Exception:
            return False