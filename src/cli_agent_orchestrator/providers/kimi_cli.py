"""Kimi CLI provider implementation.

Kimi CLI (https://kimi.com/code) is Moonshot AI's coding agent CLI tool.
It runs as an interactive TUI using prompt_toolkit in the terminal.

Key characteristics:
- Command: ``kimi`` (installed via ``brew install kimi-cli`` or ``uv tool install kimi-cli``)
- Idle prompt: ``username@dirname💫`` (thinking mode, default) or ``username@dirname✨``
- Processing: No idle prompt visible at bottom while the response is streaming
- Response format: Bullet points prefixed with ``•`` (U+2022)
- Thinking output: Gray italic ``•`` bullets (ANSI color 38;5;244 + italic)
- User input: Displayed in a bordered box using box-drawing characters (╭│╰)
- Auto-approve: ``--yolo`` flag bypasses all tool action confirmations
- Agent profiles: ``--agent-file FILE`` (YAML format, extends built-in 'default' agent)
- MCP config: ``--mcp-config TEXT`` (JSON configuration, repeatable flag)
- Exit commands: ``/exit``, ``exit``, ``quit``, or Ctrl-D
- Status bar: ``HH:MM [yolo] agent (model, thinking) ctrl-x: toggle mode context: X.X%``

Status Detection Strategy:
    Kimi CLI uses a full-screen TUI (prompt_toolkit), so status is detected by
    checking the bottom of tmux capture output:
    - IDLE: Prompt pattern (username@dir💫/✨) visible at bottom, no user input yet
    - PROCESSING: No prompt at bottom (response is streaming)
    - COMPLETED: Prompt at bottom + response content after last user input
    - ERROR: Error message patterns or empty output
"""

import json
import logging
import os
import re
import shlex
import shutil
import tempfile
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for Kimi CLI provider-specific errors."""

    pass


# =============================================================================
# Regex patterns for Kimi CLI output analysis
# =============================================================================

# Strip ANSI escape codes for clean text matching.
# Matches sequences like \x1b[0m, \x1b[38;5;244m, \x1b[1m, etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Kimi idle prompt: ``username@dirname💫`` or ``username@dirname✨``.
# ✨ appears in normal agent mode (--no-thinking).
# 💫 appears when thinking mode is enabled (default behavior).
# Username comes from getpass.getuser(), dirname is the last path component.
# Rendered bold in terminal: \x1b[1m...prompt...\x1b[0m
IDLE_PROMPT_PATTERN = r"\w+@[\w.-]+[✨💫]"

# Number of lines from bottom to scan for the idle prompt.
# Kimi's TUI renders empty padding lines between the prompt and the status bar.
# The padding depends on terminal height: a 46-row terminal has ~32 empty lines
# between the prompt (line ~14 after the welcome banner) and the status bar.
# Must be large enough to cover the tallest expected terminal.
IDLE_PROMPT_TAIL_LINES = 50

# Simplified idle pattern for log file monitoring.
# Just looks for either emoji marker, which is sufficient for quick detection.
IDLE_PROMPT_PATTERN_LOG = r"[✨💫]"

# Kimi welcome banner, shown once during startup inside a bordered box.
# Used to detect successful initialization without needing to wait for prompt.
WELCOME_BANNER_PATTERN = r"Welcome to Kimi Code CLI!"

# User input box boundaries. Kimi displays user messages in a bordered box:
#   ╭──────────────────────────────╮
#   │ user message text             │
#   ╰──────────────────────────────╯
USER_INPUT_BOX_START_PATTERN = r"╭─"
USER_INPUT_BOX_END_PATTERN = r"╰─"

# Response/thinking bullet pattern: ``•`` (U+2022) at the start of a line.
# Both thinking (internal monologue) and response (final answer) use this marker.
# To distinguish them in extraction, check ANSI styling in raw output:
# - Thinking: gray italic (\x1b[38;5;244m• ... \x1b[3m\x1b[38;5;244m)
# - Response: plain ``•`` without ANSI color prefix
RESPONSE_BULLET_PATTERN = r"^•\s"

# Thinking bullet detection in raw (ANSI-preserved) output.
# Thinking lines use gray color (38;5;244) before the bullet character.
# This pattern distinguishes thinking from actual response content
# when extracting messages from terminal output.
THINKING_BULLET_RAW_PATTERN = r"\x1b\[38;5;244m\s*•"

# Kimi TUI status bar at the bottom of the screen.
# Format: "HH:MM  [yolo]  agent (model, thinking)  ctrl-x: toggle mode  context: X.X%"
# Used to identify TUI chrome that should be excluded from content analysis.
STATUS_BAR_PATTERN = r"\d+:\d+\s+.*(?:agent|shell)\s*\("

# Generic error patterns for detecting failure states in terminal output.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|ConnectionError:|APIError:)"
)


class KimiCliProvider(BaseProvider):
    """Provider for Kimi CLI tool integration.

    Manages the lifecycle of a Kimi CLI session in a tmux window,
    including initialization, status detection, response extraction,
    and cleanup. Kimi CLI agent profiles are optional — if not provided,
    Kimi uses its built-in default agent.
    """

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
        # Track temp directory for cleanup (created when agent profile needs temp files)
        self._temp_dir: Optional[str] = None

    def _build_kimi_command(self) -> str:
        """Build Kimi CLI command with agent profile and MCP config if provided.

        Returns properly escaped shell command string for tmux send_keys.
        Uses shlex.join() for safe escaping of all arguments.

        Command structure:
            kimi --yolo [--agent-file FILE] [--mcp-config JSON]

        The --yolo flag auto-approves all tool actions, which is required for
        non-interactive operation in CAO-managed tmux sessions.
        """
        command_parts = ["kimi", "--yolo"]

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Build agent file from profile's system prompt.
                # Kimi uses YAML agent files with a system_prompt_path pointing
                # to a markdown file. We create both in a temp directory.
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                if system_prompt:
                    self._temp_dir = tempfile.mkdtemp(prefix="cao_kimi_")

                    # Write the system prompt as a markdown file
                    prompt_file = os.path.join(self._temp_dir, "system.md")
                    with open(prompt_file, "w") as f:
                        f.write(system_prompt)

                    # Create the agent YAML that extends the default agent
                    # and points to our custom system prompt file.
                    # Written as plain string to avoid adding PyYAML dependency.
                    agent_yaml = (
                        "version: 1\n"
                        "agent:\n"
                        "  extend: default\n"
                        "  system_prompt_path: ./system.md\n"
                    )
                    agent_file = os.path.join(self._temp_dir, "agent.yaml")
                    with open(agent_file, "w") as f:
                        f.write(agent_yaml)

                    command_parts.extend(["--agent-file", agent_file])

                # Add MCP server configuration if present in the agent profile.
                # Kimi accepts --mcp-config as a JSON string (repeatable flag).
                if profile.mcpServers:
                    mcp_config = {}
                    for server_name, server_config in profile.mcpServers.items():
                        if isinstance(server_config, dict):
                            mcp_config[server_name] = dict(server_config)
                        else:
                            mcp_config[server_name] = server_config.model_dump(exclude_none=True)

                        # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                        # can identify the current terminal for handoff/assign operations.
                        # Kimi CLI does not automatically forward parent shell env vars
                        # to MCP subprocesses, so we inject it explicitly via the env field.
                        env = mcp_config[server_name].get("env", {})
                        if "CAO_TERMINAL_ID" not in env:
                            env["CAO_TERMINAL_ID"] = self.terminal_id
                            mcp_config[server_name]["env"] = env

                    command_parts.extend(["--mcp-config", json.dumps(mcp_config)])

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        return shlex.join(command_parts)

    def initialize(self) -> bool:
        """Initialize Kimi CLI provider by starting the kimi command.

        Steps:
        1. Wait for the shell prompt in the tmux window
        2. Build and send the kimi command
        3. Wait for Kimi to reach IDLE state (welcome banner + prompt)

        Returns:
            True if initialization completed successfully

        Raises:
            TimeoutError: If shell or Kimi CLI doesn't start within timeout
        """
        # Wait for shell prompt to appear in the tmux window
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Build properly escaped command string
        command = self._build_kimi_command()

        # Send Kimi command to the tmux window
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Wait for Kimi CLI to reach IDLE state (prompt visible).
        # Kimi takes a few seconds to load and display the welcome banner.
        # Longer timeout than shell (60s) to account for first-run setup.
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=1.0):
            raise TimeoutError("Kimi CLI initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Kimi CLI status by analyzing terminal output.

        Status detection logic:
        1. Capture tmux pane output (full or tail)
        2. Strip ANSI codes for reliable text matching
        3. Check bottom N lines for the idle prompt pattern
        4. If prompt found: distinguish IDLE vs COMPLETED by checking for user input
        5. If no prompt: agent is PROCESSING (streaming response)
        6. Check for ERROR patterns as fallback

        Args:
            tail_lines: Optional number of lines to capture from bottom

        Returns:
            TerminalStatus indicating current state
        """
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # Strip ANSI codes for reliable pattern matching
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check the bottom lines for the idle prompt.
        # Kimi's TUI has padding lines between prompt and status bar.
        # Use end-of-line anchor (\s*$) to distinguish a bare prompt ("user@dir💫")
        # from a prompt with user input after it ("user@dir💫 some text"),
        # which appears when the user has typed a command.
        all_lines = clean_output.strip().splitlines()
        bottom_lines = all_lines[-IDLE_PROMPT_TAIL_LINES:]
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        has_idle_prompt = any(re.search(idle_prompt_eol, line) for line in bottom_lines)

        if has_idle_prompt:
            # Prompt is visible — check if there's a completed response.
            # Look for user input box (╭─...╰─) anywhere in the output,
            # which indicates a task was submitted and processed.
            has_user_input = bool(re.search(USER_INPUT_BOX_START_PATTERN, clean_output))
            has_response = bool(re.search(RESPONSE_BULLET_PATTERN, clean_output, re.MULTILINE))

            if has_user_input and has_response:
                return TerminalStatus.COMPLETED

            return TerminalStatus.IDLE

        # No idle prompt at bottom — check for errors before assuming processing
        if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.ERROR

        # No prompt visible and no error: Kimi is actively processing/streaming
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        """Return Kimi CLI idle prompt pattern for log file monitoring.

        Used by the inbox service for quick IDLE state detection in pipe-pane
        log files before calling the full get_status() method.
        """
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Kimi's final response from terminal output.

        Extraction strategy:
        1. Find the last user input box (╭─...╰─) in clean text
        2. Collect all content between the box end and the next prompt
        3. Filter out thinking bullets (gray ANSI-styled lines)
        4. Return the cleaned response text

        The raw (ANSI-preserved) output is used to distinguish thinking
        from response bullets, since both use the ``•`` prefix character.

        Args:
            script_output: Raw terminal output from tmux capture

        Returns:
            Extracted response text with ANSI codes stripped

        Raises:
            ValueError: If no response content can be extracted
        """
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Work line-by-line for reliable mapping between raw and clean output.
        raw_lines = script_output.split("\n")
        clean_lines = clean_output.split("\n")

        # Find the last user input box end line (╰─)
        box_end_idx = None
        for i, line in enumerate(clean_lines):
            if re.search(USER_INPUT_BOX_END_PATTERN, line):
                box_end_idx = i

        if box_end_idx is None:
            raise ValueError("No Kimi CLI user input found - no input box detected")

        # Find the next idle prompt line after the box end.
        # Use the general pattern (not end-of-line anchored) since
        # the prompt line may have trailing whitespace.
        prompt_idx = len(clean_lines)  # default: end of output
        for i in range(box_end_idx + 1, len(clean_lines)):
            if re.search(IDLE_PROMPT_PATTERN, clean_lines[i]):
                prompt_idx = i
                break

        # Response region: lines between box end and prompt (exclusive)
        response_start = box_end_idx + 1
        response_end = prompt_idx

        # Collect all non-empty lines for the fallback response
        all_response_lines = [
            clean_lines[i].strip()
            for i in range(response_start, response_end)
            if i < len(clean_lines) and clean_lines[i].strip()
        ]

        if not all_response_lines:
            raise ValueError("Empty Kimi CLI response - no content found after input box")

        # Filter out thinking bullets and status bar lines.
        # Thinking bullets have gray ANSI color (38;5;244) in the raw output.
        filtered_lines = []
        for i in range(response_start, response_end):
            raw_line = raw_lines[i] if i < len(raw_lines) else ""
            clean_line = clean_lines[i] if i < len(clean_lines) else ""

            # Skip empty lines
            if not clean_line.strip():
                continue

            # Skip thinking bullets (identified by gray ANSI color in raw output)
            if re.search(THINKING_BULLET_RAW_PATTERN, raw_line):
                continue

            # Skip status bar lines
            if re.search(STATUS_BAR_PATTERN, clean_line):
                continue

            filtered_lines.append(clean_line.strip())

        if not filtered_lines:
            # If all lines were filtered as thinking, fall back to returning
            # all content. This handles edge cases where the response format
            # doesn't match expected patterns.
            return "\n".join(all_response_lines).strip()

        return "\n".join(filtered_lines).strip()

    def exit_cli(self) -> str:
        """Get the command to exit Kimi CLI.

        Kimi CLI supports several exit commands: /exit, exit, quit, or Ctrl-D.
        We use /exit as it's the most reliable and consistent.
        """
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Kimi CLI provider resources.

        Removes any temporary files created for agent profiles
        and resets the initialization state.
        """
        # Remove temp directory if it was created for agent profile
        if self._temp_dir:
            if os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

        self._initialized = False
