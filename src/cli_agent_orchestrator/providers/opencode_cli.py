"""OpenCode CLI provider implementation.

This module provides the OpenCodeCliProvider class for integrating with OpenCode,
a terminal-based AI assistant with a native agent system.

OpenCode Features:
- Agent-based conversations via YAML-frontmatter Markdown agent files
- Per-agent tool/permission configuration via frontmatter and opencode.json
- MCP server support with per-agent tool gating
- 24-bit truecolor alt-screen TUI

The provider detects the following terminal states:
- IDLE: Waiting for user input (``ctrl+p commands`` footer, no ``esc interrupt``)
- PROCESSING: Agent working (``esc interrupt`` footer)
- COMPLETED: Agent finished turn (``▣ <agent> · <model> · Ns`` marker + idle footer)
- WAITING_USER_ANSWER: Permission dialog active (``△ Permission required`` visible)
- ERROR: Fallback when no state marker matches
"""

import logging
import re
import shlex
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns — verified from probe fixtures (§8.1 of design doc)
# =============================================================================

# ANSI escape code pattern — handles 24-bit truecolor sequences (\x1b[38;2;R;G;Bm)
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# User message indent: blue vertical bar + 2 spaces
USER_MESSAGE_PATTERN = r"^┃\s{2}"

# Per-turn completion marker: "▣  <agent>  ·  <model>  ·  <duration>s"
# Two middle-dot separators and a trailing duration are required.
COMPLETION_MARKER_PATTERN = r"▣\s+\S+\s+·\s+.+?\s+·\s+\d+(?:\.\d+)?s"

# Processing footer — keybind hint present while the agent is generating.
PROCESSING_FOOTER_PATTERN = r"\besc interrupt\b"

# Idle footer anchor — present when waiting for the next user message.
IDLE_FOOTER_PATTERN = r"ctrl\+p\s+commands"

# Permission prompt heading — both initial request and "Always allow" sub-confirmation.
PERMISSION_PROMPT_PATTERN = r"△\s+(?:Permission required|Always allow)\b"

# Tool-call in-flight spinner (braille animation): "⠋ Read <path>" etc.
TOOL_CALL_IN_FLIGHT_PATTERN = r"^\s+[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\S+"


class OpenCodeCliProvider(BaseProvider):
    """Provider for OpenCode CLI integration.

    Manages the lifecycle of an OpenCode TUI session inside a tmux window:
    initialization, status detection, response extraction, and cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the installed OpenCode agent to launch
        _model: Optional model override (e.g. ``anthropic/claude-sonnet-4-6``)
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
    ):
        """Initialize OpenCode CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the installed OpenCode agent (e.g. ``"developer"``)
            allowed_tools: Optional CAO tool list (informational; enforcement is via frontmatter)
            model: Optional model override passed via ``--model`` at launch (§3.1 exception)
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._agent_profile = agent_profile or ""
        self._model = model
        self._initialized = False

    @property
    def paste_enter_count(self) -> int:
        """OpenCode TUI submits on a single Enter after bracketed paste."""
        return 1

    def initialize(self) -> bool:
        """Start the OpenCode TUI and wait for the idle splash frame.

        Steps:
        1. Wait for the shell prompt in the tmux window.
        2. Send the inline-env ``opencode --agent <name>`` launch command.
        3. Wait for IDLE or COMPLETED with a 120s timeout to cover the first-run
           ``npm install @opencode-ai/plugin`` blocking period (§8.2, §10.7).

        Returns:
            True if initialization completed successfully.

        Raises:
            TimeoutError: If shell or OpenCode doesn't reach IDLE/COMPLETED in time.
        """
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_launch_command()
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # 120s covers first-run npm install (5–30s) and concurrent multi-agent launches.
        if not wait_until_status(
            self,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
        ):
            raise TimeoutError("OpenCode CLI initialization timed out after 120 seconds")

        self._initialized = True
        return True

    def _build_launch_command(self) -> str:
        """Build the inline-env opencode launch command string (§5 of design doc)."""
        env_pairs = [
            f"OPENCODE_CONFIG={OPENCODE_CONFIG_FILE}",
            f"OPENCODE_CONFIG_DIR={OPENCODE_CONFIG_DIR}",
            "OPENCODE_DISABLE_AUTOUPDATE=1",
            "OPENCODE_DISABLE_MOUSE=1",
            "OPENCODE_DISABLE_TERMINAL_TITLE=1",
            "OPENCODE_CLIENT=cao",
            "TERM=xterm-256color",
        ]
        cmd_parts = ["opencode"]
        if self._agent_profile:
            cmd_parts += ["--agent", self._agent_profile]
        if self._model:
            cmd_parts += ["--model", self._model]
        # env vars are shell words; join cmd parts with shlex for proper quoting
        return " ".join(env_pairs) + " " + shlex.join(cmd_parts)

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Detect current TUI state from the tmux capture buffer.

        Priority order (§8.3 of design doc):
        1. WAITING_USER_ANSWER — permission dialog heading present, no idle footer after it
        2. PROCESSING — ``esc interrupt`` footer; line-level guard prevents stale-buffer
           false positives (lesson #16)
        3. COMPLETED — last full ``▣…·…·…Ns`` marker present, idle footer after it,
           no later ``▣`` token (would indicate a new incomplete turn)
        4. IDLE — idle footer present, no ``esc interrupt`` anywhere
        5. ERROR — fallback

        Args:
            tail_lines: Lines to capture; None uses the tmux_client default.

        Returns:
            Current TerminalStatus.
        """
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        if not output:
            return TerminalStatus.ERROR

        clean = re.sub(ANSI_CODE_PATTERN, "", output)

        # ── 1. WAITING_USER_ANSWER ───────────────────────────────────────────
        perm_matches = list(re.finditer(PERMISSION_PROMPT_PATTERN, clean))
        if perm_matches:
            last_perm_end = perm_matches[-1].end()
            # Permission dialog replaces the normal footer; if idle footer still
            # appears *after* the dialog the user already dismissed it.
            if not re.search(IDLE_FOOTER_PATTERN, clean[last_perm_end:]):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 2. PROCESSING ───────────────────────────────────────────────────
        # Line-level position guard: during normal processing, ``esc interrupt``
        # and ``ctrl+p commands`` share the same footer line.  In the stale case
        # (alt-screen remnant), ``esc interrupt`` is on an earlier line and the
        # new idle footer appears on a *later* line — that later-line presence
        # means processing has ended.
        lines = clean.split("\n")
        last_esc_line = -1
        for i, line in enumerate(lines):
            if re.search(PROCESSING_FOOTER_PATTERN, line):
                last_esc_line = i

        esc_is_stale = False
        if last_esc_line >= 0:
            later = lines[last_esc_line + 1 :]
            has_idle_later = any(re.search(IDLE_FOOTER_PATTERN, l) for l in later)
            has_completion_later = any(re.search(COMPLETION_MARKER_PATTERN, l) for l in later)
            if not has_idle_later and not has_completion_later:
                return TerminalStatus.PROCESSING
            # Guard fired: esc interrupt is a stale alt-screen remnant.
            esc_is_stale = True

        # ── 3. COMPLETED ─────────────────────────────────────────────────────
        # Requires the last full completion marker (with duration) followed by the
        # idle footer and no subsequent ``▣`` token (which would indicate a new
        # incomplete turn visible in the scrollback).
        completion_matches = list(re.finditer(COMPLETION_MARKER_PATTERN, clean))
        if completion_matches:
            last_end = completion_matches[-1].end()
            after = clean[last_end:]
            if re.search(IDLE_FOOTER_PATTERN, after) and not re.search(r"▣", after):
                return TerminalStatus.COMPLETED

        # ── 4. IDLE ──────────────────────────────────────────────────────────
        # Allow IDLE when either no esc interrupt is present OR it was flagged stale
        # (position guard fired in step 2 above).
        if re.search(IDLE_FOOTER_PATTERN, clean) and (
            not re.search(PROCESSING_FOOTER_PATTERN, clean) or esc_is_stale
        ):
            return TerminalStatus.IDLE

        # ── 5. ERROR (fallback) ───────────────────────────────────────────────
        return TerminalStatus.ERROR

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's response from the TUI scrollback.

        Algorithm (§8.4 of design doc):
        1. Strip ANSI codes.
        2. Find the last ``USER_MESSAGE_PATTERN`` (last user turn start).
        3. Find the first full ``COMPLETION_MARKER_PATTERN`` at or after that position.
        4. Extract text between end-of-user-block and start-of-completion-marker.
        5. Strip ``Thinking: …`` preamble lines.
        6. Dedent the 5-space agent indent.
        7. Clean control chars and trailing whitespace.

        Args:
            script_output: Raw terminal output captured from the tmux pane.

        Returns:
            Extracted agent response text.

        Raises:
            ValueError: If no user message or completion marker is found.
        """
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Find the last FULL completion marker to anchor the turn boundary.
        all_completions = list(re.finditer(COMPLETION_MARKER_PATTERN, clean))
        if not all_completions:
            raise ValueError("No completion marker found after last user message")

        last_completion = all_completions[-1]

        # Find the last user-message bar (┃  ) that appears BEFORE the completion marker.
        # TUI lines have leading spaces before ┃; searching unanchored handles that.
        # Searching only in the slice before the completion start avoids matching
        # the bottom input-box header (┃  <agent> · <model>) that follows the turn.
        user_matches = list(re.finditer(r"┃\s{2}", clean[: last_completion.start()]))
        if not user_matches:
            raise ValueError("No user message found in OpenCode output")

        # Both positions are absolute offsets in clean (the slice starts at 0).
        response_start = user_matches[-1].end()  # after the ┃  bar
        response_end = last_completion.start()  # before the ▣ completion marker

        raw_response = clean[response_start:response_end]

        # Skip user-message lines (those still using ┃ indent) to find agent block
        response_lines = raw_response.split("\n")
        agent_lines = []
        past_user_block = False
        for line in response_lines:
            if re.match(r"^\s*┃", line):
                # Still inside the user message block
                past_user_block = False
                continue
            if not past_user_block and not line.strip():
                # Blank line between user block and agent block
                continue
            past_user_block = True
            agent_lines.append(line)

        # Strip Thinking: preamble — remove lines starting with "Thinking:"
        # until the first non-Thinking non-blank line
        stripped_lines = []
        in_thinking = False
        for line in agent_lines:
            if re.match(r"^\s*Thinking:", line):
                in_thinking = True
                continue
            if in_thinking and not line.strip():
                # Blank line after thinking block — skip it
                continue
            in_thinking = False
            stripped_lines.append(line)

        # Dedent 5-space agent indent
        dedented = []
        for line in stripped_lines:
            if line.startswith("     "):
                dedented.append(line[5:])
            else:
                dedented.append(line)

        # Clean control chars and trailing whitespace
        result = "\n".join(dedented).strip()
        result = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", result)
        result = re.sub(r"[ \t]+$", "", result, flags=re.MULTILINE)

        if not result:
            raise ValueError("Empty OpenCode response — no content found after extraction")

        return result

    def get_idle_pattern_for_log(self) -> str:
        """Return pattern matching the idle footer in log file output."""
        return IDLE_FOOTER_PATTERN

    def exit_cli(self) -> str:
        """Return the command to exit the OpenCode TUI (§8.5)."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up OpenCode provider state."""
        self._initialized = False
