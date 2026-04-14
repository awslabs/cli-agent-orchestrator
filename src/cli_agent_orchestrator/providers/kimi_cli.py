"""Kimi CLI provider implementation.

Kimi CLI (https://kimi.com/code) is Moonshot AI's coding agent CLI tool.
It runs as an interactive TUI using prompt_toolkit in the terminal.

Key characteristics:
- Command: ``kimi`` (installed via ``brew install kimi-cli`` or ``uv tool install kimi-cli``)
- Idle prompt: ``💫`` (thinking mode, default) or ``✨`` (optionally prefixed with ``username@dirname``)
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
from pathlib import Path
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

# Kimi idle prompt: ``💫`` or ``✨`` (optionally prefixed with ``username@dirname``).
# ✨ appears in normal agent mode (--no-thinking).
# 💫 appears when thinking mode is enabled (default behavior).
# Kimi CLI v1.20.0+ renders just the emoji; earlier versions showed ``username@dirname💫``.
# The prefix is made optional to support both formats.
IDLE_PROMPT_PATTERN = r"(?:\w+@[\w.-]+)?[✨💫]"

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

# User input box boundaries (pre-v1.20.0). Kimi displayed user messages in a bordered box:
#   ╭──────────────────────────────╮
#   │ user message text             │
#   ╰──────────────────────────────╯
# In v1.20.0+, user input appears on the prompt line: ``💫 user message``
USER_INPUT_BOX_START_PATTERN = r"╭─"
USER_INPUT_BOX_END_PATTERN = r"╰─"

# Prompt line with user input (v1.20.0+ format).
# Matches ``💫 some text`` or ``✨ some text`` — a prompt emoji followed by non-whitespace
# on the SAME line. Uses [^\S\n]+ (horizontal whitespace only) to avoid matching
# across newlines (a bare ``💫`` followed by blank lines then status bar).
PROMPT_WITH_INPUT_PATTERN = r"(?:\w+@[\w.-]+)?[✨💫][^\S\n]+\S"

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

    # Class-level flag: ensures ~/.kimi/config.toml MCP timeout is set only once,
    # even when multiple KimiCliProvider instances are created in parallel (e.g.,
    # 3 data_analyst workers via assign). Without this, concurrent read/write to
    # the config file causes race conditions and file corruption.
    _mcp_timeout_configured = False

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Track temp directory for cleanup (created when agent profile needs temp files)
        self._temp_dir: Optional[str] = None
        # Latching flag: set True when user input box (╭─) is detected in ANY
        # get_status() call. Persists even after the box scrolls out of the
        # tmux capture window (200 lines). This is needed because:
        # 1. Long responses push the user input box out of capture range
        # 2. Not all responses use • bullets (tables, numbered lists, etc.)
        # Without this, get_status() returns IDLE instead of COMPLETED after
        # the agent finishes processing, causing handoff to time out.
        self._has_received_input = False

    @property
    def paste_enter_count(self) -> int:
        """Kimi CLI's prompt_toolkit submits on single Enter after bracketed paste."""
        return 1

    def _build_kimi_command(self) -> str:
        """Build Kimi CLI command with agent profile and MCP config if provided.

        Returns properly escaped shell command string for tmux send_keys.
        Uses shlex.join() for safe escaping of all arguments.

        Command structure:
            cd <temp_dir> && TERM=xterm-256color kimi --yolo [--agent-file FILE] [--mcp-config JSON]

        The ``cd`` is required because Kimi CLI v1.20.0+ enforces a per-directory
        single-instance lock — only one kimi process can run in a given directory.
        Each provider instance gets its own temp directory to avoid conflicts.

        The ``TERM=xterm-256color`` override is needed because Kimi CLI v1.20.0+
        silently exits when TERM=tmux-256color (the tmux default).

        The --yolo flag auto-approves all tool actions, which is required for
        non-interactive operation in CAO-managed tmux sessions.
        """
        command_parts = ["kimi", "--yolo"]

        # Always create a temp directory for this instance.
        # Kimi CLI v1.20.0+ has a per-directory single-instance lock, so each
        # provider instance needs its own working directory.
        if not self._temp_dir:
            self._temp_dir = tempfile.mkdtemp(prefix="cao_kimi_")

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Build agent file from profile's system prompt.
                # Kimi uses YAML agent files with a system_prompt_path pointing
                # to a markdown file. We create both in the temp directory.
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                system_prompt = self._apply_skill_prompt(system_prompt)

                # Prepend security constraints for soft enforcement (Kimi CLI has no
                # native tool restriction mechanism). Only applied when tool
                # restrictions are active (not unrestricted "*").
                if self._allowed_tools and "*" not in self._allowed_tools:
                    from cli_agent_orchestrator.constants import SECURITY_PROMPT

                    tools_list = ", ".join(self._allowed_tools)
                    tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                    system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

                if system_prompt:
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
                    # Set MCP tool call timeout to 600s by modifying ~/.kimi/config.toml
                    # directly. We cannot use --config flag because it causes Kimi CLI
                    # to bypass its default config file, which breaks OAuth authentication
                    # (shows "model: not set" and /login says "restart without --config").
                    # Class-level guard ensures this runs only once per process.
                    self._ensure_mcp_timeout()

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

        # cd to unique temp dir (per-directory lock) + set TERM for tmux compatibility
        kimi_cmd = shlex.join(command_parts)
        return f"cd {shlex.quote(self._temp_dir)} && TERM=xterm-256color {kimi_cmd}"

    @classmethod
    def _ensure_mcp_timeout(cls) -> None:
        """Ensure MCP tool call timeout is set to 600s in ~/.kimi/config.toml.

        Called once per process (guarded by class-level flag). Kimi CLI defaults
        to tool_call_timeout_ms=60000 (60s) for MCP tool calls, which is too short
        for handoff operations. We modify the config file directly instead of using
        ``--config`` CLI flag, because ``--config`` causes Kimi CLI to bypass the
        default config file and breaks OAuth authentication.

        The timeout is NOT restored on cleanup because:
        1. Multiple Kimi instances may share the config file concurrently
        2. 600s is a strictly better default for anyone using MCP tools
        3. Restoring while other instances are running causes race conditions
        """
        if cls._mcp_timeout_configured:
            return

        config_path = Path.home() / ".kimi" / "config.toml"
        if not config_path.exists():
            logger.warning(f"Kimi config not found at {config_path}, skipping MCP timeout override")
            cls._mcp_timeout_configured = True
            return

        try:
            content = config_path.read_text()

            # Match the existing timeout line under [mcp.client] section
            # Format: tool_call_timeout_ms = 60000
            pattern = r"(tool_call_timeout_ms\s*=\s*)(\d+)"
            match = re.search(pattern, content)
            if match:
                current_value = int(match.group(2))
                if current_value < 600000:
                    new_content = re.sub(pattern, r"\g<1>600000", content)
                    config_path.write_text(new_content)
                    logger.info(
                        f"Set MCP tool_call_timeout_ms to 600000 "
                        f"(was {current_value}) in {config_path}"
                    )
            else:
                logger.warning(
                    f"tool_call_timeout_ms not found in {config_path}, "
                    "MCP tool calls may time out during handoff"
                )
        except Exception as e:
            logger.warning(f"Failed to set MCP timeout in {config_path}: {e}")

        cls._mcp_timeout_configured = True

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

        # Wait for Kimi CLI to reach IDLE or COMPLETED state (prompt visible).
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        # Longer timeout (120s) to account for first-run setup and when
        # multiple Kimi instances are starting concurrently (e.g. assign flow).
        if not wait_until_status(
            self,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("Kimi CLI initialization timed out after 120 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Kimi CLI status by analyzing terminal output.

        Status detection logic:
        1. Capture tmux pane output (full or tail)
        2. Strip ANSI codes for reliable text matching
        3. Latch ``_has_received_input`` when user input box (╭─) is detected
        4. Check bottom N lines for the idle prompt pattern
        5. If prompt found + input was received → COMPLETED
        6. If prompt found + no input yet → IDLE
        7. If no prompt: agent is PROCESSING (streaming response)
        8. Check for ERROR patterns as fallback

        The latching flag approach is necessary because:
        - Long responses (>200 lines) push the user input box out of the
          tmux capture window, so checking for ╭─ on every call is unreliable
        - Not all responses use ``•`` bullets (structured output like tables,
          numbered lists, report templates have no bullet markers at all)
        - The flag is set during the PROCESSING phase when the user input box
          IS still visible in the capture, and persists through completion

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

        # Latch: detect user input to distinguish IDLE from COMPLETED.
        # Supports two formats:
        #
        # Pre-v1.20.0: User input in bordered box (╭─...╰─).
        #   - During PROCESSING (no idle prompt): any ╭─ means user input
        #   - During IDLE/COMPLETED: count ╰─ occurrences (welcome banner = 1, input = 2+)
        #
        # v1.20.0+: User input on prompt line (``💫 message text``).
        #   - Detect prompt emoji followed by non-whitespace text
        if not self._has_received_input:
            # v1.20.0+: prompt line with text after the emoji
            if re.search(PROMPT_WITH_INPUT_PATTERN, clean_output):
                self._has_received_input = True
            # Pre-v1.20.0: input box detection
            elif not has_idle_prompt:
                if re.search(USER_INPUT_BOX_START_PATTERN, clean_output):
                    self._has_received_input = True
            else:
                box_end_count = len(re.findall(USER_INPUT_BOX_END_PATTERN, clean_output))
                if box_end_count >= 2:
                    self._has_received_input = True

        if has_idle_prompt:
            if self._has_received_input:
                # Guard against premature COMPLETED: if processing indicators are
                # visible in the bottom lines, Kimi is still working even though
                # the idle prompt is present. This happens when get_status() is
                # polled in the brief window between task submission and Kimi
                # clearing the prompt to start streaming.
                for line in bottom_lines:
                    stripped = line.strip()
                    # Braille spinner with tool name: "⠼ Using Shell (...)"
                    if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+Using\s", stripped):
                        return TerminalStatus.PROCESSING
                    # Moon phase emoji alone on a line = thinking indicator
                    if stripped in {"🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"}:
                        return TerminalStatus.PROCESSING
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

        Supports two formats:

        Pre-v1.20.0 (input box format):
        1. Find the last user input box (╭─...╰─) in clean text
        2. Collect all content between the box end and the next prompt
        3. Filter out thinking bullets (gray ANSI-styled lines)

        v1.20.0+ (inline prompt format):
        1. Find the last prompt-with-input line (``💫 message text``)
        2. Collect all content between that line and the next bare prompt
        3. Filter out thinking bullets

        Fallback for long responses (markers scrolled out of capture):
        - Extract all content from start of capture up to the idle prompt
        - Filter out thinking/status bar lines

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

        # Strategy 1: Find the last user input box end line (╰─) — pre-v1.20.0
        box_end_idx = None
        # Only consider box-end lines that come AFTER the welcome banner.
        # The welcome banner itself has ╰─, so we skip it by finding the
        # welcome banner line first.
        welcome_idx = 0
        for i, line in enumerate(clean_lines):
            if re.search(WELCOME_BANNER_PATTERN, line):
                welcome_idx = i
        for i in range(welcome_idx + 1, len(clean_lines)):
            if re.search(USER_INPUT_BOX_END_PATTERN, clean_lines[i]):
                box_end_idx = i

        # Strategy 2: Find the last prompt-with-input line — v1.20.0+
        prompt_input_idx = None
        for i, line in enumerate(clean_lines):
            if re.search(PROMPT_WITH_INPUT_PATTERN, line):
                prompt_input_idx = i

        # Choose the best anchor: prefer input box (more precise), fall back to prompt-with-input
        if box_end_idx is not None:
            response_start = box_end_idx + 1
        elif prompt_input_idx is not None:
            response_start = prompt_input_idx + 1
        else:
            # Neither marker found — long response scrolled everything out
            return self._extract_without_input_box(raw_lines, clean_lines)

        # Find the next idle prompt line (bare prompt, no text after it)
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        prompt_idx = len(clean_lines)  # default: end of output
        for i in range(response_start, len(clean_lines)):
            if re.search(idle_prompt_eol, clean_lines[i]):
                prompt_idx = i
                break

        response_end = prompt_idx

        # Collect all non-empty lines for the fallback response
        all_response_lines = [
            clean_lines[i].strip()
            for i in range(response_start, response_end)
            if i < len(clean_lines) and clean_lines[i].strip()
        ]

        if not all_response_lines:
            raise ValueError("Empty Kimi CLI response - no content found after input")

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

    def _extract_without_input_box(self, raw_lines: list, clean_lines: list) -> str:
        """Fallback extraction when user input box has scrolled out of capture.

        For long responses (>200 lines), the user input box (╭─/╰─) and early
        response content are no longer in the tmux capture window. In this case,
        extract all content from the start of capture up to the last idle prompt,
        filtering out status bar and welcome banner lines.

        Args:
            raw_lines: Raw output split by newlines (ANSI preserved)
            clean_lines: ANSI-stripped output split by newlines

        Returns:
            Extracted response text

        Raises:
            ValueError: If no extractable content found
        """
        # Find the last idle prompt line
        prompt_idx = len(clean_lines)
        for i in range(len(clean_lines) - 1, -1, -1):
            if re.search(IDLE_PROMPT_PATTERN, clean_lines[i]):
                prompt_idx = i
                break

        # Collect content from start to prompt, filtering out TUI chrome
        filtered_lines = []
        for i in range(0, prompt_idx):
            raw_line = raw_lines[i] if i < len(raw_lines) else ""
            clean_line = clean_lines[i] if i < len(clean_lines) else ""

            if not clean_line.strip():
                continue

            # Skip thinking bullets
            if re.search(THINKING_BULLET_RAW_PATTERN, raw_line):
                continue

            # Skip status bar
            if re.search(STATUS_BAR_PATTERN, clean_line):
                continue

            # Skip welcome banner lines
            if re.search(WELCOME_BANNER_PATTERN, clean_line):
                continue

            filtered_lines.append(clean_line.strip())

        if not filtered_lines:
            raise ValueError("No extractable content in Kimi CLI output (input box scrolled out)")

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
        and resets the initialization state. MCP timeout is NOT restored
        because multiple Kimi instances may share the config file concurrently.
        """
        # Remove temp directory if it was created for agent profile
        if self._temp_dir:
            if os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

        self._initialized = False
        self._has_received_input = False
