"""Gemini CLI provider implementation.

Gemini CLI (https://github.com/google-gemini/gemini-cli) is Google's coding agent CLI tool.
It runs as an interactive TUI using Ink (React-based terminal UI) in the terminal.

Key characteristics:
- Command: ``gemini`` (installed via ``npm install -g @google/gemini-cli``)
- Idle prompt: ``*`` asterisk with placeholder text "Type your message" in bottom input box
- Processing: User query displayed in input box with ``*`` prefix and thinking text below
- Response format: Lines prefixed with ``✦`` (U+2726, four-pointed star)
- User query display: Lines prefixed with ``>`` (greater-than) inside bordered input box
- Input box borders: ``▀`` (U+2580 top border), ``▄`` (U+2584 bottom border)
- Tool call results: Bordered box using ``╭╰╮╯`` with ``✓`` checkmark
- Auto-approve: ``--yolo`` / ``-y`` flag bypasses all tool action confirmations
- MCP config: ``gemini mcp add <name> <command> [args...]`` (pre-launch setup, not inline flag)
- Exit commands: Ctrl+D to exit; Ctrl+C cancels current query
- Status bar: ``~/dir (branch*)  sandbox  Auto (Model) /model |XX.X MB``
- YOLO indicator: ``YOLO mode (ctrl + y to toggle)`` above bottom input box

Status Detection Strategy:
    Gemini CLI uses an Ink-based full-screen TUI (not alternate screen), so status
    is detected by checking the bottom of tmux capture output:
    - IDLE: ``*`` placeholder text ("Type your message") visible in bottom input box
    - PROCESSING: ``*`` prefix with user query text (not placeholder) in bottom input box,
      or ``Responding with`` model indicator visible without response completion
    - COMPLETED: ``✦`` response text + ``*`` idle placeholder in bottom input box
    - ERROR: Error message patterns or empty output
"""

import logging
import re
import shlex
import time
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for Gemini CLI provider-specific errors."""

    pass


# =============================================================================
# Regex patterns for Gemini CLI output analysis
# =============================================================================

# Strip ANSI escape codes for clean text matching.
# Matches sequences like \x1b[0m, \x1b[38;2;203;166;247m, \x1b[1m, etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Gemini idle prompt: asterisk (*) followed by placeholder text "Type your message".
# The idle input box at the bottom always contains this placeholder when Gemini
# is ready for input. The * is rendered in pink (ANSI 38;2;243;139;168).
IDLE_PROMPT_PATTERN = r"\*\s+Type your message"

# Number of lines from bottom to scan for the idle prompt.
# Gemini's Ink TUI renders the input box, status bar, and possible empty lines
# at the bottom. The idle prompt is typically within the last 10 lines, but
# use 50 to account for tall terminals and additional TUI padding.
IDLE_PROMPT_TAIL_LINES = 50

# Simplified idle pattern for log file monitoring.
# Just looks for the asterisk + "Type your message" text for quick detection.
IDLE_PROMPT_PATTERN_LOG = r"\*.*Type your message"

# Gemini welcome banner, shown once during startup as ASCII art.
# The banner includes the word "GEMINI" in block characters using █ and ░.
# Used to detect successful initialization.
WELCOME_BANNER_PATTERN = r"█████████.*██████████"

# Query input box: user queries are displayed between ▀ (top) and ▄ (bottom) borders
# with a > prefix. Submitted queries show "> query text".
QUERY_BOX_PREFIX_PATTERN = r"^\s*>\s+\S"

# Response prefix: ✦ (U+2726, four-pointed star) at the start of response lines.
# All Gemini response text lines are prefixed with this character.
RESPONSE_PREFIX_PATTERN = r"✦\s"

# Model indicator line: appears between query box and response.
# Format: "Responding with <model-name>"
MODEL_INDICATOR_PATTERN = r"Responding with\s+\S+"

# Tool call result box: bordered box with ✓ checkmark for YOLO auto-approved actions.
# Used to detect tool invocations in the response area.
TOOL_CALL_BOX_PATTERN = r"[╭╰]─"

# Input box border patterns: ▀ (U+2580) for top, ▄ (U+2584) for bottom.
# Full-width lines of these characters delimit the input box.
INPUT_BOX_TOP_PATTERN = r"▀{10,}"
INPUT_BOX_BOTTOM_PATTERN = r"▄{10,}"

# Gemini status bar at the bottom of the screen.
# Format: "~/dir (branch*)  sandbox  Auto (Model) /model |XX.X MB"
# Used to identify TUI chrome that should be excluded from content analysis.
STATUS_BAR_PATTERN = r"(?:sandbox|no sandbox).*(?:Auto|/mod(?:e|el))"

# YOLO mode indicator text above the bottom input box.
YOLO_INDICATOR_PATTERN = r"YOLO mode"

# Generic error patterns for detecting failure states in terminal output.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|ConnectionError:|APIError:)"
)


class GeminiCliProvider(BaseProvider):
    """Provider for Gemini CLI tool integration.

    Manages the lifecycle of a Gemini CLI session in a tmux window,
    including initialization, status detection, response extraction,
    and cleanup. Gemini CLI does not support inline agent profiles —
    if provided, the system prompt is passed via --prompt-interactive flag.
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
        # Track MCP servers that were configured via `gemini mcp add`
        # so they can be removed during cleanup.
        self._mcp_server_names: list[str] = []

    def _build_gemini_command(self) -> str:
        """Build Gemini CLI command with appropriate flags.

        Returns properly escaped shell command string for tmux send_keys.
        Uses shlex.join() for safe escaping of all arguments.

        Command structure:
            gemini --yolo [--sandbox false]

        The --yolo flag auto-approves all tool actions, which is required for
        non-interactive operation in CAO-managed tmux sessions.

        Note: Gemini CLI does not support inline agent profiles or system prompts
        via CLI flags in the same way as Kimi or Claude Code. Agent profile system
        prompts are handled by creating a GEMINI.md file in the working directory.
        MCP servers must be configured beforehand via `gemini mcp add`.
        """
        command_parts = ["gemini", "--yolo", "--sandbox", "false"]

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Gemini CLI reads system instructions from GEMINI.md files.
                # We don't create these automatically since it would modify the
                # user's working directory. Instead, the system prompt from the
                # agent profile is not applied for Gemini CLI.
                # Future: consider --prompt-interactive for initial instruction.

                # Configure MCP servers via `gemini mcp add` if present.
                # This must happen BEFORE launching gemini, so we return
                # a compound command that sets up MCP servers first.
                if profile.mcpServers:
                    setup_commands = []
                    for server_name, server_config in profile.mcpServers.items():
                        if isinstance(server_config, dict):
                            cfg = server_config
                        else:
                            cfg = server_config.model_dump(exclude_none=True)

                        command = cfg.get("command", "")
                        args = cfg.get("args", [])

                        # Build `gemini mcp add <name> --scope user [-e KEY=VALUE] <command> [args...]`
                        # Note: Do NOT use `--` separator — yargs in gemini-cli treats
                        # it as end-of-options and fails with "not enough positional args".
                        # Use -e flag for env vars (native gemini mcp add support).
                        # Use --scope user to avoid "Please use --scope user to edit
                        # settings in the home directory" error when working_directory
                        # is the user's home directory.
                        mcp_parts = [
                            "gemini",
                            "mcp",
                            "add",
                            server_name,
                            "--scope",
                            "user",
                            "-e",
                            f"CAO_TERMINAL_ID={self.terminal_id}",
                            command,
                        ]
                        mcp_parts.extend(args)
                        setup_commands.append(shlex.join(mcp_parts))
                        self._mcp_server_names.append(server_name)

                    # Chain: add MCP servers → launch gemini
                    all_commands = setup_commands + [shlex.join(command_parts)]
                    return " && ".join(all_commands)

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        return shlex.join(command_parts)

    def initialize(self) -> bool:
        """Initialize Gemini CLI provider by starting the gemini command.

        Steps:
        1. Wait for the shell prompt in the tmux window
        2. Build and send the gemini command (may include MCP setup)
        3. Wait for Gemini to reach IDLE state (welcome banner + input box)

        Returns:
            True if initialization completed successfully

        Raises:
            TimeoutError: If shell or Gemini CLI doesn't start within timeout
        """
        # Wait for shell prompt to appear in the tmux window
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Send a warm-up command before launching Gemini.
        # Gemini's Ink TUI exits silently in freshly-created tmux sessions where
        # the shell environment (PATH, node, nvm, homebrew) is not fully loaded.
        # wait_for_shell() returns when the prompt text stabilizes, but slow
        # shell init scripts (.zshrc, brew shellenv) may still be running.
        # An echo round-trip with output verification ensures the shell has
        # fully processed its init before we launch gemini.
        warmup_marker = "CAO_SHELL_READY"
        tmux_client.send_keys(self.session_name, self.window_name, f"echo {warmup_marker}")
        warmup_start = time.time()
        warmup_timeout = 15.0
        while time.time() - warmup_start < warmup_timeout:
            output = tmux_client.get_history(self.session_name, self.window_name)
            if output and warmup_marker in output:
                break
            time.sleep(0.5)
        else:
            logger.warning("Shell warm-up marker not detected within timeout, proceeding anyway")

        # Build properly escaped command string
        command = self._build_gemini_command()

        # Send Gemini command to the tmux window
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Wait for Gemini CLI to reach IDLE state (input box visible).
        # Gemini takes 10-15+ seconds to load due to Node.js/Ink startup.
        # Longer timeout than shell (60s) to account for first-run setup.
        if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=1.0):
            raise TimeoutError("Gemini CLI initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Gemini CLI status by analyzing terminal output.

        Status detection logic:
        1. Capture tmux pane output (full or tail)
        2. Strip ANSI codes for reliable text matching
        3. Check bottom N lines for the idle prompt pattern (* + placeholder text)
        4. If idle prompt found: distinguish IDLE vs COMPLETED by checking for ✦ response
        5. If no idle prompt: check for processing indicators or errors
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
        # Gemini's Ink TUI places the input box near the bottom with status bar below.
        all_lines = clean_output.strip().splitlines()
        bottom_lines = all_lines[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt = any(re.search(IDLE_PROMPT_PATTERN, line) for line in bottom_lines)

        if has_idle_prompt:
            # Idle prompt is visible — check if there's a completed response.
            # Look for ✦ response prefix anywhere in the output,
            # which indicates Gemini produced a response.
            has_response = bool(re.search(RESPONSE_PREFIX_PATTERN, clean_output))
            # Also check for submitted query (> prefix inside input box)
            has_query = bool(re.search(QUERY_BOX_PREFIX_PATTERN, clean_output, re.MULTILINE))

            if has_query and has_response:
                return TerminalStatus.COMPLETED

            return TerminalStatus.IDLE

        # No idle prompt at bottom — check for errors before assuming processing
        if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.ERROR

        # No idle prompt visible and no error: Gemini is actively processing
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        """Return Gemini CLI idle prompt pattern for log file monitoring.

        Used by the inbox service for quick IDLE state detection in pipe-pane
        log files before calling the full get_status() method.
        """
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Gemini's final response from terminal output.

        Extraction strategy:
        1. Find the last query input box (> prefix between ▀/▄ borders)
        2. Collect all ✦-prefixed response lines after the query
        3. Strip the ✦ prefix and response formatting
        4. Filter out status bar, YOLO indicator, and input box chrome

        Args:
            script_output: Raw terminal output from tmux capture

        Returns:
            Extracted response text with ANSI codes stripped

        Raises:
            ValueError: If no response content can be extracted
        """
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)
        clean_lines = clean_output.split("\n")

        # Find the last query box: line matching "> query text" pattern
        last_query_idx = None
        for i, line in enumerate(clean_lines):
            if re.search(QUERY_BOX_PREFIX_PATTERN, line):
                last_query_idx = i

        if last_query_idx is None:
            raise ValueError("No Gemini CLI user query found - no > prefix detected")

        # Find the end boundary: the idle prompt (* + Type your message) or end of output
        prompt_idx = len(clean_lines)
        for i in range(last_query_idx + 1, len(clean_lines)):
            if re.search(IDLE_PROMPT_PATTERN, clean_lines[i]):
                prompt_idx = i
                break

        # Collect response content between query and prompt.
        # Response lines are prefixed with ✦, tool boxes use ╭╰╮╯ borders,
        # and there may be model indicator lines and other content.
        response_lines = []
        for i in range(last_query_idx + 1, prompt_idx):
            line = clean_lines[i].strip()

            # Skip empty lines
            if not line:
                continue

            # Skip input box borders (▀▀▀ or ▄▄▄)
            if re.search(INPUT_BOX_TOP_PATTERN, line) or re.search(INPUT_BOX_BOTTOM_PATTERN, line):
                continue

            # Skip status bar
            if re.search(STATUS_BAR_PATTERN, line):
                continue

            # Skip YOLO indicator
            if re.search(YOLO_INDICATOR_PATTERN, line):
                continue

            # Skip model indicator ("Responding with ...")
            if re.search(MODEL_INDICATOR_PATTERN, line):
                continue

            response_lines.append(line)

        if not response_lines:
            raise ValueError("Empty Gemini CLI response - no content found after query")

        return "\n".join(response_lines).strip()

    def exit_cli(self) -> str:
        """Get the command to exit Gemini CLI.

        Gemini CLI exits via Ctrl+D (EOF). It does not have /quit or /exit commands.
        We send C-d via tmux, which is the standard EOF signal.
        """
        return "C-d"

    def cleanup(self) -> None:
        """Clean up Gemini CLI provider resources.

        Removes any MCP servers that were configured via `gemini mcp add`
        and resets the initialization state.
        """
        # Remove MCP servers that were added during initialization
        for server_name in self._mcp_server_names:
            try:
                tmux_client.send_keys(
                    self.session_name,
                    self.window_name,
                    f"gemini mcp remove --scope user {shlex.quote(server_name)}",
                )
            except Exception as e:
                logger.warning(f"Failed to remove MCP server '{server_name}': {e}")
        self._mcp_server_names = []

        self._initialized = False
