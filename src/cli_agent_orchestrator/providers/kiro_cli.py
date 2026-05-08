"""Kiro CLI provider implementation.

This module provides the KiroCliProvider class for integrating with Kiro CLI,
an AI-powered coding assistant that operates through a terminal interface.

Kiro CLI Features:
- Agent-based conversations with customizable profiles
- File system access and code manipulation capabilities
- Interactive permission prompts for sensitive operations
- ANSI-colored output with distinctive prompt patterns

The provider detects the following terminal states:
- IDLE: Agent is waiting for user input (shows agent prompt)
- PROCESSING: Agent is generating a response
- COMPLETED: Agent has finished responding (shows green arrow + response)
- WAITING_USER_ANSWER: Agent is waiting for permission confirmation
- ERROR: Agent encountered an error during processing
"""

import logging
import re
import shlex
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns for Kiro CLI Output Analysis
# =============================================================================

# Green arrow pattern indicates the start of an agent response (ANSI-stripped)
# Example: "> Here is the code you requested..."
GREEN_ARROW_PATTERN = r"^>\s*"

# ANSI escape code pattern for stripping terminal colors
# Matches sequences like \x1b[32m (green), \x1b[0m (reset), etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Additional escape sequences that may appear in terminal output
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"

# Control characters to strip from final output
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"

# Bell character (audible alert)
BELL_CHAR = "\x07"
IDLE_PROMPT_PATTERN_LOG = r"\x1b\[38;5;\d+m\[.+?\].*\x1b\[38;5;\d+m>\s*\x1b\[\d*m"

# =============================================================================
# New TUI Patterns (Kiro CLI without --legacy-ui)
# =============================================================================

# New TUI idle prompt: "Ask a question or describe a task ↵"
# Case-insensitive match; comma between "question" and "or" is optional
# (older versions used lowercase with comma, v1.29+ uses capitalized without)
NEW_TUI_IDLE_PATTERN = r"[Aa]sk a question,? or describe a task"

# New TUI IDLE prompt pattern for log files (with ANSI codes)
NEW_TUI_IDLE_PATTERN_LOG = r"[Aa]sk a question,? or describe a task"

# TUI separator line: horizontal bar (────) used to delimit sections.
# Require 20+ chars to avoid matching short markdown separators in agent output.
TUI_SEPARATOR_PATTERN = r"^[─]{20,}$"

# TUI Credits line: "▸ Credits: N.NN • Time: Ns" marks response completion
TUI_CREDITS_PATTERN = r"▸\s*Credits:\s*[\d.]+"

# TUI processing indicator: ghost text shown while agent is working
TUI_PROCESSING_PATTERN = r"Kiro is working"

# TUI initialization indicator: shown during startup before chat is ready.
# Kiro TUI renders the idle prompt placeholder ("Ask a question or describe
# a task") *before* the "● Initializing..." phase completes, which caused a
# premature IDLE verdict. "Initializing..." is cleared by Kiro once startup
# finishes, so its presence unconditionally means PROCESSING (unlike the
# "Kiro is working" ghost text, which can linger as stale after a redraw).
TUI_INITIALIZING_PATTERN = r"Initializing\.\.\."

# TUI permission prompt: shown instead of legacy [y/n/t] format.
# Requires all three options together to avoid false positives on "Yes"/"No" in agent output.
TUI_PERMISSION_PATTERN = r"Yes\s+No\s+Always [Aa]llow"

# =============================================================================
# Error Detection
# =============================================================================

# Strings that indicate the agent encountered an error
ERROR_INDICATORS = ["Kiro is having trouble responding right now"]


class KiroCliProvider(BaseProvider):
    """Provider for Kiro CLI tool integration.

    This provider manages the lifecycle of a Kiro CLI chat session within a tmux window,
    including initialization, status detection, and response extraction.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the Kiro agent profile to use
        _idle_prompt_pattern: Regex pattern for detecting IDLE state
        _permission_prompt_pattern: Regex pattern for detecting permission prompts
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: str,
        allowed_tools: Optional[list] = None,
    ):
        """Initialize Kiro CLI provider with terminal context.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the Kiro agent profile to use (e.g., "developer")
            allowed_tools: Optional list of CAO tool names the agent is allowed to use
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._initialized = False
        self._agent_profile = agent_profile

        # Build dynamic prompt pattern based on agent profile
        # This pattern matches various Kiro prompt formats after ANSI stripping:
        # - [developer] >       (basic prompt)
        # - [developer] !>      (prompt with pending changes)
        # - [developer] 50% >   (prompt with progress indicator)
        # - [developer] λ >     (prompt with lambda symbol)
        # - [developer] 50% λ > (combined progress and lambda)
        self._idle_prompt_pattern = (
            rf"\[{re.escape(self._agent_profile)}\]\s*(?:\d+%\s*)?(?:\u03bb\s*)?!?>\s*"
        )
        self._permission_prompt_pattern = r"Allow this action\?.*?\[.*?y.*?/.*?n.*?/.*?t.*?\]:"

        # New TUI header pattern: "agent_name · model · ◔ N%"
        self._new_tui_header_pattern = rf"{re.escape(self._agent_profile)}\s+·\s+.*·\s+◔\s*\d+%"
        self._shell_command: Optional[str] = None

    @property
    def paste_enter_count(self) -> int:
        """Kiro CLI submits on single Enter after bracketed paste."""
        return 1

    def _get_profile_model(self) -> Optional[str]:
        """Return profile.model if the agent profile can be loaded, else None.

        Best-effort: historically the Kiro CLI provider has not required the
        CAO agent profile to be loadable at runtime (kiro-cli has its own
        agent store). A missing or unparseable profile must not block launch.
        """
        try:
            profile = load_agent_profile(self._agent_profile)
        except (FileNotFoundError, RuntimeError) as exc:
            logger.debug(
                "Profile '%s' not loadable by CAO; skipping --model resolution: %s",
                self._agent_profile,
                exc,
            )
            return None
        return profile.model or None

    def initialize(self) -> bool:
        """Initialize Kiro CLI provider by starting kiro-cli chat command.

        This method:
        1. Waits for the shell to be ready in the tmux window
        2. Sends the kiro-cli chat command with the configured agent profile
        3. Waits for the agent to reach IDLE state (ready for input)

        Returns:
            True if initialization was successful

        Raises:
            TimeoutError: If shell or Kiro CLI initialization times out
        """
        # Step 1: Wait for shell prompt to appear in the tmux window
        # This ensures the terminal is ready before we send commands
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Capture the shell process name before launching kiro — used later to detect kiro exit
        self._shell_command = tmux_client.get_pane_current_command(
            self.session_name, self.window_name
        )

        # Step 2: Start the Kiro CLI chat session.
        #
        # --trust-all-tools: bypass Kiro CLI's permission prompts when CAO
        # launches with --yolo (allowed_tools=['*']). Without this, every
        # tool invocation re-prompts, blocking assign/handoff flows.
        # --model: honor profile.model so workflows can pin a specific model.
        #
        # UI mode selection:
        # - Yolo (--trust-all-tools): kiro-cli 2.0.1 TUI blocks on an
        #   interactive "Yes, I accept" consent dialog before the chat is
        #   ready; only --legacy-ui/--classic/--no-interactive bypass it.
        #   CAO drives kiro-cli headlessly, so we force --legacy-ui for yolo.
        # - Non-yolo: use the default TUI (fall back to --legacy-ui on
        #   timeout, preserving prior behavior for older kiro-cli versions).
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)
        model = self._get_profile_model()

        if yolo:
            logger.info(
                "kiro_cli yolo mode: forcing --legacy-ui (kiro-cli 2.0.1 TUI "
                "shows a non-bypassable trust-all-tools consent dialog)"
            )
            base_args = ["kiro-cli", "chat", "--legacy-ui", "--trust-all-tools"]
        else:
            base_args = ["kiro-cli", "chat"]
        if model:
            base_args.extend(["--model", model])
        base_args.extend(["--agent", self._agent_profile])
        command = shlex.join(base_args)
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Step 3: Wait for Kiro CLI to fully initialize and show the agent prompt.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        if not wait_until_status(
            self, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=30.0
        ):
            if yolo:
                # Yolo already launched with --legacy-ui; no further fallback.
                raise TimeoutError("Kiro CLI initialization timed out with --legacy-ui (yolo mode)")
            # Non-yolo TUI mode failed — fall back to --legacy-ui
            logger.warning("Kiro CLI TUI initialization timed out, retrying with --legacy-ui")
            # Exit the current session and start fresh with --legacy-ui
            tmux_client.send_keys(self.session_name, self.window_name, "/exit")
            if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
                raise TimeoutError("Shell recovery timed out after --legacy-ui fallback")
            legacy_args = ["kiro-cli", "chat", "--legacy-ui"]
            if model:
                legacy_args.extend(["--model", model])
            legacy_args.extend(["--agent", self._agent_profile])
            legacy_command = shlex.join(legacy_args)
            tmux_client.send_keys(self.session_name, self.window_name, legacy_command)
            if not wait_until_status(
                self, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=30.0
            ):
                raise TimeoutError("Kiro CLI initialization timed out with TUI and `--legacy-ui`")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output.

        Status detection logic (in priority order):
        1. No output → ERROR
        2. No IDLE prompt visible → PROCESSING (agent is generating response)
        3. Error indicators present → ERROR
        4. Permission prompt visible → WAITING_USER_ANSWER
        5. Green arrow + prompt visible → COMPLETED (response ready)
        6. Only prompt visible → IDLE (waiting for input)

        Args:
            tail_lines: Number of lines to capture from terminal history.
                        If None, uses default from tmux_client.

        Returns:
            Current TerminalStatus enum value
        """
        logger.debug(f"get_status: tail_lines={tail_lines}")
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        # No output indicates a terminal error
        if not output:
            return TerminalStatus.ERROR

        # Strip ANSI codes once for all pattern matching
        # This simplifies regex patterns and improves reliability
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check 0: TUI startup — the new TUI renders the idle-prompt
        # placeholder ("Ask a question or describe a task") before the
        # "● Initializing..." phase completes, so a naive idle match would
        # declare IDLE ~1s into launch and the first user message would be
        # dropped. "Initializing..." is always cleared once init finishes, so
        # its presence unconditionally means PROCESSING.
        if re.search(TUI_INITIALIZING_PATTERN, clean_output):
            return TerminalStatus.PROCESSING

        # Check 1: Detect idle prompts early — required for the position-aware
        # processing check below.
        old_idle_matches = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idle_matches = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))
        has_idle_prompt = old_idle_matches[0] if old_idle_matches else None
        has_new_tui_idle = bool(new_tui_idle_matches)

        # Check 2: Look for TUI "Kiro is working" ghost text.
        # Kiro TUI redraws the screen in-place, so the buffer can retain a stale
        # "Kiro is working" line from an earlier render even after the agent has
        # finished and the idle prompt has appeared below it.  Only return
        # PROCESSING when no idle prompt appears *after* the last match.
        tui_working_matches = list(re.finditer(TUI_PROCESSING_PATTERN, clean_output))
        if tui_working_matches:
            last_working_pos = tui_working_matches[-1].end()
            idle_after_working = any(
                m.start() > last_working_pos for m in new_tui_idle_matches + old_idle_matches
            )
            if not idle_after_working:
                return TerminalStatus.PROCESSING

        # Check 3: If no idle prompt found, determine if kiro is still running.
        # Compare current pane command against the shell captured before kiro launched.
        # If they match, kiro has exited and the shell is showing again → IDLE.
        if not has_idle_prompt and not has_new_tui_idle:
            if self._shell_command:
                current_cmd = tmux_client.get_pane_current_command(
                    self.session_name, self.window_name
                )
                if current_cmd == self._shell_command:
                    return TerminalStatus.IDLE
            return TerminalStatus.PROCESSING

        # Check 2: Look for known error messages in the output
        if any(indicator.lower() in clean_output.lower() for indicator in ERROR_INDICATORS):
            return TerminalStatus.ERROR

        # Check for permission prompt — legacy [y/n/t] or TUI "Yes, No, Always Allow"
        # Active prompt: 0-1 lines with idle prompt (CLI renders prompt on next line)
        # Stale prompt: 2+ lines with idle prompt (user answered, agent continued)
        # Line-based counting handles \r redraws (same line, no \n) correctly
        perm_matches = list(re.finditer(self._permission_prompt_pattern, clean_output, re.DOTALL))
        tui_perm_matches = list(re.finditer(TUI_PERMISSION_PATTERN, clean_output))
        all_perm_matches = perm_matches + tui_perm_matches
        # Sort by position so we use the last permission prompt regardless of type
        all_perm_matches.sort(key=lambda m: m.start())
        if all_perm_matches:
            after_last_perm = clean_output[all_perm_matches[-1].end() :]
            lines_after = after_last_perm.split("\n")
            idle_lines = sum(
                1
                for line in lines_after
                if re.search(self._idle_prompt_pattern, line)
                or re.search(NEW_TUI_IDLE_PATTERN, line)
            )
            if idle_lines <= 1:
                return TerminalStatus.WAITING_USER_ANSWER

        # Check 4: Look for completed response (green arrow indicates agent output)
        # Must verify that an idle prompt appears AFTER the response
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        if green_arrows:
            # Find if there's an idle prompt after the last green arrow
            last_arrow_pos = green_arrows[-1].end()
            idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))

            for prompt in idle_prompts:
                if prompt.start() > last_arrow_pos:
                    logger.debug(f"get_status: returning COMPLETED")
                    return TerminalStatus.COMPLETED

            # Also check new TUI idle pattern after the last green arrow
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_arrow_pos:
                    logger.debug("get_status: returning COMPLETED (new TUI)")
                    return TerminalStatus.COMPLETED

            # Has green arrow but no prompt after it - still processing
            return TerminalStatus.PROCESSING

        # Check 5: TUI completion — Credits marker + idle prompt after it.
        # In pure TUI mode, there are no green arrows. Completion is indicated
        # by "▸ Credits:" followed by the idle prompt.
        credits_matches = list(re.finditer(TUI_CREDITS_PATTERN, clean_output))
        if credits_matches:
            last_credits_pos = credits_matches[-1].end()
            for prompt in new_tui_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits)")
                    return TerminalStatus.COMPLETED
            for prompt in old_idle_matches:
                if prompt.start() > last_credits_pos:
                    logger.debug("get_status: returning COMPLETED (TUI credits + legacy idle)")
                    return TerminalStatus.COMPLETED
            # Credits marker found but no idle prompt after it — still processing
            return TerminalStatus.PROCESSING

        # Default: Agent is IDLE, waiting for user input
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Strip ANSI codes for pattern matching
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Find patterns in clean output
        green_arrows = list(re.finditer(GREEN_ARROW_PATTERN, clean_output, re.MULTILINE))
        idle_prompts = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idles = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))

        # Slash command fallback: if the most recent interaction (between the
        # last two idle prompts) has no green arrow, it was a CLI-handled
        # command like /context or /compact. Extract that output instead.
        if len(idle_prompts) >= 2:
            last_prompt_pos = idle_prompts[-1].start()
            prev_prompt_pos = idle_prompts[-2].end()
            has_arrow_in_last_interaction = any(
                m.start() > prev_prompt_pos and m.start() < last_prompt_pos for m in green_arrows
            )
            if not has_arrow_in_last_interaction:
                between = clean_output[prev_prompt_pos:last_prompt_pos]
                # First line is the user's command text, skip it
                lines = between.split("\n", 1)
                if lines[0].lstrip().startswith("/"):
                    output = lines[1].strip() if len(lines) > 1 else ""
                    if output:
                        output = re.sub(ESCAPE_SEQUENCE_PATTERN, "", output)
                        output = re.sub(CONTROL_CHAR_PATTERN, "", output)
                        return output.strip()

        if not green_arrows:
            # Fallback: try TUI extraction (separator + Credits pattern)
            return self._extract_tui_message(clean_output)

        if not idle_prompts and not new_tui_idles:
            raise ValueError("Incomplete Kiro CLI response - no final prompt detected")

        # Find the last green arrow (response start)
        last_arrow_pos = green_arrows[-1].end()

        # Find idle prompt that comes AFTER the last green arrow (old or new TUI)
        final_prompt = None
        for prompt in idle_prompts:
            if prompt.start() > last_arrow_pos:
                final_prompt = prompt
                break
        if not final_prompt:
            for prompt in new_tui_idles:
                if prompt.start() > last_arrow_pos:
                    final_prompt = prompt
                    break

        if not final_prompt:
            raise ValueError(
                "Incomplete Kiro CLI response - no final prompt detected after response"
            )

        # Extract directly from clean output
        start_pos = last_arrow_pos
        end_pos = final_prompt.start()

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        # Clean up the message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def _extract_tui_message(self, clean_output: str) -> str:
        """Extract agent response from pure TUI output (no green arrows).

        TUI format:
            ────────────────────────────
              user message here

              Agent's response here.

            ▸ Credits: 0.24 - Time: 3s
            ────────────────────────────
            agent-name - model - N%
             Ask a question or describe a task

        Strategy:
            1. Find the last Credits line (response end marker)
            2. Find the previous Credits line (prior turn boundary) or start of output
            3. Find the first separator after that boundary (outer TUI separator)
               This avoids matching separators inside the agent's response.
            4. Extract text between separator and Credits
            5. Skip the first paragraph (user message) if a blank line separates it
        """
        lines = clean_output.split("\n")

        # Find the last Credits line
        credits_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                credits_idx = i
                break

        if credits_idx is None:
            raise ValueError(
                "No Kiro CLI response found - no Credits marker or green arrow detected"
            )

        # Find the previous Credits line (prior turn's end) to establish search boundary.
        # This ensures we find the outer TUI separator, not one inside the agent's output.
        prev_credits_idx = -1
        for i in range(credits_idx - 1, -1, -1):
            if re.search(TUI_CREDITS_PATTERN, lines[i]):
                prev_credits_idx = i
                break

        # Find the first separator AFTER the previous turn boundary
        separator_idx = None
        for i in range(prev_credits_idx + 1, credits_idx):
            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                separator_idx = i
                break

        # Kiro 2.0: separator is AFTER credits_idx. Scan forward to find it.
        if separator_idx is None:
            next_credits_idx = len(lines)
            for i in range(credits_idx + 1, len(lines)):
                if re.search(TUI_CREDITS_PATTERN, lines[i]):
                    next_credits_idx = i
                    break
            for i in range(credits_idx + 1, next_credits_idx):
                if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                    separator_idx = i
                    break

        if separator_idx is None:
            raise ValueError("No Kiro CLI response found - no separator found near Credits marker")

        # Extract content between separator and Credits
        if separator_idx > credits_idx:
            # Kiro 2.0: separator after Credits. Content precedes credits_idx.
            content_lines = lines[prev_credits_idx + 1 : credits_idx]
        else:
            # Pre-2.0: separator before Credits (existing behavior)
            content_lines = lines[separator_idx + 1 : credits_idx]

        # Skip the first paragraph (user message echo).
        # The user message is the first block of non-empty lines after the separator.
        # After a blank line, the agent response begins.
        agent_start = 0
        found_blank = False
        for i, line in enumerate(content_lines):
            stripped = line.strip()
            if not found_blank and not stripped:
                found_blank = True
                continue
            if found_blank and stripped:
                agent_start = i
                break

        if not found_blank:
            # No blank line found — entire content is the response
            agent_start = 0

        response_lines = content_lines[agent_start:]
        final_answer = "\n".join(response_lines).strip()

        if not final_answer:
            raise ValueError("Empty Kiro CLI response - no content found")

        # Clean up (ANSI codes already stripped from clean_output at caller)
        final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
        final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
        return final_answer.strip()

    def get_idle_pattern_for_log(self) -> str:
        """Return Kiro CLI IDLE prompt pattern for log files.

        Returns a pattern that matches either the legacy UI format
        or the new TUI format.
        """
        return rf"(?:{IDLE_PROMPT_PATTERN_LOG}|{NEW_TUI_IDLE_PATTERN_LOG})"

    def exit_cli(self) -> str:
        """Get the command to exit Kiro CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Kiro CLI provider."""
        self._initialized = False
