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

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns for Kiro CLI Output Analysis
# =============================================================================

# Green arrow pattern indicates the start of an agent response (escape-stripped)
# Example: "> Here is the code you requested..."
GREEN_ARROW_PATTERN = r"^>\s*"

# SGR (colour) escape codes only. Used by get_status, which strips colour but
# MUST preserve carriage returns and cursor-movement sequences: the permission
# detection counts idle prompts per newline-delimited line, and Kiro renders
# active prompts with \r in-place redraws (same line, no \n). strip_terminal_
# escapes would normalise \r -> \n and split those redraws onto separate lines,
# making an active permission prompt look idle (inbox would then deliver during
# a permission prompt — see test_permission_prompt_detection).
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Additional escape sequences that may appear in terminal output
ESCAPE_SEQUENCE_PATTERN = r"\[[?0-9;]*[a-zA-Z]"

# Control characters to strip from final output
CONTROL_CHAR_PATTERN = r"[\x00-\x1f\x7f-\x9f]"

# Legacy-UI IDLE prompt pattern for log files (with ANSI codes)
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

# Non-SGR CSI sequences (erase-line \x1b[2K, cursor moves, etc.) — everything
# except colour codes ending in 'm'. Compiled once at module scope because
# get_status() is on a hot path (called per output burst); recompiling per call
# was avoidable overhead. Used in Check 6 to strip TUI chrome so the separator
# regex can anchor on the raw ─── characters.
_NON_SGR_CSI = re.compile(r"\x1b\[[0-9;?]*[^m]")

# TUI Credits line: "▸ Credits: N.NN • Time: Ns" marks response completion
TUI_CREDITS_PATTERN = r"▸\s*Credits:\s*[\d.]+"

# TUI processing indicator: ghost text shown while agent is working.
# kiro-cli 2.11+ replaced "Kiro is working" with "Thinking..." (with an
# optional "(esc to cancel)" suffix). Match either variant.
TUI_PROCESSING_PATTERN = r"Kiro is working|Thinking\.\.\."

# TUI initialization indicator: shown during startup before chat is ready.
# Kiro TUI renders the idle prompt placeholder ("Ask a question or describe
# a task") *before* the "● Initializing..." phase completes, which caused a
# premature IDLE verdict. "Initializing..." is cleared by Kiro once startup
# finishes, so its presence unconditionally means PROCESSING (unlike the
# "Kiro is working" ghost text, which can linger as stale after a redraw).
#
# Also covers the MCP-server boot line "M of N mcp servers initialized.
# ctrl-c to start chatting now" — Kiro shows this *before* the idle prompt
# is interactive, so a paste sent during this window is absorbed by the
# pre-prompt boot screen and silently dropped (observed during e2e
# allowed-tools tests).
#
# kiro-cli 2.8.x also shows "Initializing · type to queue a message" during
# boot (different from the "Initializing..." with three dots).
TUI_INITIALIZING_PATTERN = (
    r"Initializing\.\.\."
    r"|\d+ of \d+ mcp servers initialized\.\s*ctrl-c to start chatting now"
    r"|Initializing\s*·\s*type to queue a message"
)


# TUI permission prompt: shown instead of legacy [y/n/t] format.
# Requires all three options together to avoid false positives on "Yes"/"No" in agent output.
# Kiro 2.11 renders the same three-way choice with different wording for
# subagent spawning: "Yes, single permission / Trust, always allow in this
# session / No (Tab to edit)". Both alternatives anchor on the full Yes/Trust/No
# option layout so agent output that merely mentions a permission prompt (or
# quotes "subagent requires approval") can't flip status to WAITING_USER_ANSWER
# — the bare header alternative was dropped for that reason. In practice
# --trust-all-tools suppresses this prompt anyway; detection is a safety net.
TUI_PERMISSION_PATTERN = (
    r"Yes\s+No\s+Always [Aa]llow"
    r"|Yes,\s*single permission[\s\S]{0,200}?Trust,\s*always allow[\s\S]{0,200}?No"
)

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
        self._input_received = False
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

    @property
    def paste_enter_count(self) -> int:
        """Kiro CLI 2.11 needs 2 Enters after bracketed paste to submit.

        The first Enter is consumed by the TUI (finalizes the paste), the
        second submits the message. Older kiro versions only needed 1.
        """
        return 2

    @property
    def paste_submit_delay(self) -> float:
        """Kiro 2.11's TUI needs a longer delay after bracketed paste before
        the Enter key registers as submit rather than being swallowed."""
        return 1.0

    def mark_input_received(self) -> None:
        """Track that input was sent, enabling separator-free completion detection."""
        super().mark_input_received()
        self._input_received = True

    @property
    def extraction_tail_lines(self) -> int:
        """Capture enough scrollback for no-credits extraction.

        The no-credits fallback (_extract_tui_message) needs both the
        start_separator (before the response) and end_separator (TUI frame
        before the idle prompt) in the same capture window. For long agent
        responses the start_separator can be hundreds of lines above the
        idle prompt. 2000 lines covers responses up to ~1800 lines of
        content, which exceeds any realistic single-turn agent response.
        """
        return 2000

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

    async def initialize(self) -> bool:
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
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        # Step 1: Wait for shell prompt to appear in the tmux window
        # This ensures the terminal is ready before we send commands
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Capture the shell process name before launching kiro — used later to detect kiro exit
        self.shell_baseline = get_backend().get_pane_current_command(
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

        # kiro-cli 2.11 introduced a "subagent requires approval" prompt that
        # blocks MCP tool calls that spawn subagents (e.g. cao-mcp-server's
        # assign/handoff). Even for non-yolo profiles, CAO enforces tool
        # scoping at its own layers (profile allowedTools + MCP allowlist),
        # so passing --trust-all-tools is safe: it bypasses kiro's
        # per-invocation UI prompt (there is no human at the terminal in
        # headless orchestration to answer), while CAO still gates what
        # tools can be called. Without this, a supervisor invoking assign()
        # hangs indefinitely on the approval dialog.
        if yolo:
            logger.info(
                "kiro_cli yolo mode: forcing --legacy-ui (kiro-cli 2.0.1 TUI "
                "shows a non-bypassable trust-all-tools consent dialog)"
            )
            base_args = ["kiro-cli", "chat", "--legacy-ui", "--trust-all-tools"]
        else:
            base_args = ["kiro-cli", "chat", "--trust-all-tools"]
        if model:
            base_args.extend(["--model", model])
        base_args.extend(["--agent", self._agent_profile])
        command = shlex.join(base_args)
        # Arm the StatusMonitor stickiness gate before launching the CLI so
        # the IDLE → PROCESSING → IDLE/COMPLETED transition is honored.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Step 3: Wait for Kiro CLI to fully initialize and show the agent prompt.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
        ):
            if yolo:
                # Yolo already launched with --legacy-ui; no further fallback.
                raise TimeoutError("Kiro CLI initialization timed out with --legacy-ui (yolo mode)")
            # Non-yolo TUI mode failed — fall back to --legacy-ui
            logger.warning("Kiro CLI TUI initialization timed out, retrying with --legacy-ui")
            # Exit the current session and start fresh with --legacy-ui
            status_monitor.notify_input_sent(self.terminal_id)
            get_backend().send_keys(self.session_name, self.window_name, "/exit")
            init_timeout = get_server_settings()["provider_init_timeout"]
            if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
                raise TimeoutError(
                    f"Shell recovery timed out after {init_timeout}s (--legacy-ui fallback)"
                )
            # Clear the StatusMonitor buffer so the --legacy-ui attempt is detected
            # against a clean buffer, not one still full of stale TUI marker bytes
            # from the failed first attempt (which would otherwise time out too).
            status_monitor.reset_buffer(self.terminal_id)
            legacy_args = ["kiro-cli", "chat", "--legacy-ui", "--trust-all-tools"]
            if model:
                legacy_args.extend(["--model", model])
            legacy_args.extend(["--agent", self._agent_profile])
            legacy_command = shlex.join(legacy_args)
            status_monitor.notify_input_sent(self.terminal_id)
            get_backend().send_keys(self.session_name, self.window_name, legacy_command)
            if not await wait_until_status(
                self.terminal_id,
                {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
                timeout=float(get_server_settings()["provider_init_timeout"]),
            ):
                raise TimeoutError("Kiro CLI initialization timed out with TUI and `--legacy-ui`")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Get Kiro CLI status by analyzing terminal output.

        Status detection logic (in priority order):
        1. No output → UNKNOWN
        2. No IDLE prompt visible → PROCESSING (agent is generating response)
        3. Error indicators present → ERROR
        4. Permission prompt visible → WAITING_USER_ANSWER
        5. Green arrow + prompt visible → COMPLETED (response ready)
        6. Only prompt visible → IDLE (waiting for input)

        Native (herdr): if the backend can report a native agent_status, trust it
        and skip buffer parsing. On herdr the pipe-pane buffer is never fed, so
        ``output`` is empty and the regex path below can never leave UNKNOWN —
        which is why kiro never reached IDLE and init timed out. The shared
        BaseProvider helper consults the backend and disambiguates herdr's
        ambiguous "idle" via _task_dispatched (set by mark_input_received).
        """
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        # Strip ONLY SGR colour codes for pattern matching. Carriage returns and
        # cursor-movement sequences are intentionally preserved: the permission
        # check below counts idle prompts per "\n"-delimited line and relies on
        # \r in-place redraws staying on the same logical line (see
        # ANSI_CODE_PATTERN). Do not switch this to strip_terminal_escapes.
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # Check 0a: Detect idle prompts early — required for the position-aware
        # processing checks below.
        old_idle_matches = list(re.finditer(self._idle_prompt_pattern, clean_output))
        new_tui_idle_matches = list(re.finditer(NEW_TUI_IDLE_PATTERN, clean_output))
        has_idle_prompt = old_idle_matches[0] if old_idle_matches else None
        has_new_tui_idle = bool(new_tui_idle_matches)

        # Check 0b: TUI startup — Kiro emits "● Initializing..." or
        # "0 of N mcp servers initialized. ctrl-c to start chatting now"
        # before the prompt is interactive; pastes during this window are
        # silently absorbed by the boot screen.
        #
        # The new TUI renders an idle-prompt PLACEHOLDER ("Ask a question
        # or describe a task") even during boot, so NEW_TUI_IDLE_PATTERN
        # matching after the init line does NOT mean init has finished —
        # we must still report PROCESSING.
        #
        # In --legacy-ui (and once the new TUI is interactive), the actual
        # "[agent] N% > " idle prompt only appears AFTER init has completed.
        # The "0 of N mcp servers initialized..." line is drawn once at
        # boot and redrawn over by the TUI; under the event-driven FIFO
        # pipeline that line still sits in the rolling byte stream forever
        # (issue surfaced by yolo --legacy-ui timing out 11/11 e2e tests).
        # Treat the init line as PROCESSING only when no real ``[agent] >``
        # idle prompt appears AFTER the last init match — mirrors the
        # TUI_PROCESSING_PATTERN ghost-text guard below.
        #
        # kiro-cli 2.8.x TUI shows "● Initializing..." (animated spinner)
        # during MCP boot. Once MCP finishes, the TUI redraws completely:
        # the spinner disappears and the idle prompt appears. In the raw
        # FIFO buffer, the idle prompt text lands AFTER the last spinner
        # frame, so checking new_tui_idle_matches after last_init_pos is a
        # reliable post-init signal. During the spinner, only spinner frames
        # are written to the stream; the idle prompt only enters the buffer
        # when the TUI redraws after init completes.
        init_matches = list(re.finditer(TUI_INITIALIZING_PATTERN, clean_output))
        if init_matches:
            last_init_pos = init_matches[-1].end()
            real_idle_after_init = any(m.start() > last_init_pos for m in old_idle_matches)
            new_idle_after_init = any(m.start() > last_init_pos for m in new_tui_idle_matches)
            if not real_idle_after_init and not new_idle_after_init:
                return TerminalStatus.PROCESSING

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
        #
        # Gated on self._initialized: between send_keys("kiro-cli chat ...")
        # and the moment kiro-cli exec's, the pane's current command still
        # matches shell_baseline ("zsh"), and the buffer hasn't shown any
        # idle prompt yet. Without this gate, get_status() returns IDLE
        # immediately after launch, which lets pre-init pastes get absorbed
        # by Kiro's boot screen and silently dropped.
        if not has_idle_prompt and not has_new_tui_idle:
            if self._initialized and self.shell_baseline:
                current_cmd = get_backend().get_pane_current_command(
                    self.session_name, self.window_name
                )
                if current_cmd == self.shell_baseline:
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

        # Check 6: Kiro CLI 2.3.0+ — no Credits marker emitted. Detect completion
        # by finding the bordered "response box" — two separators with ≥2 lines
        # of non-empty content between them — followed by the idle prompt.
        #
        # kiro-cli 2.11+ keeps the "ask a question or describe a task" placeholder
        # visible in the raw buffer at all times (it's part of the TUI chrome),
        # so a bare idle-prompt match does NOT mean the agent finished. Requiring
        # a full response box (two separators + content in between) distinguishes
        # a real completion frame from the pre-response idle state where the
        # placeholder is visible but the agent hasn't produced output yet.
        # A minimal completion is user query + agent response = 2 non-empty lines
        # inside the box.
        if has_new_tui_idle:
            lines = clean_output.split("\n")

            # Strip non-SGR CSI (erase-line \x1b[2K, cursor moves, etc.) so
            # the separator regex can see the raw ─── characters. The top-of-
            # function strip only removes SGR codes ending in 'm' (color) —
            # kiro's TUI prefixes many lines with \x1b[2K which would prevent
            # the separator regex from anchoring at start-of-line.
            # (_NON_SGR_CSI is compiled once at module scope — hot path.)
            def _sep_line(line: str) -> str:
                return _NON_SGR_CSI.sub("", line).strip()

            idle_line_idx = None
            for i in range(len(lines) - 1, -1, -1):
                if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                    idle_line_idx = i
                    break
            if idle_line_idx is not None:
                # Find the last separator BEFORE the idle line.
                last_sep_idx = None
                for i in range(idle_line_idx - 1, -1, -1):
                    if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[i])):
                        last_sep_idx = i
                        break
                if last_sep_idx is not None:
                    # Find the previous separator, and require content between them.
                    for j in range(last_sep_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, _sep_line(lines[j])):
                            content = [l for l in lines[j + 1 : last_sep_idx] if _sep_line(l)]
                            if len(content) >= 2:
                                logger.debug(
                                    "get_status: returning COMPLETED (TUI no-credits fallback)"
                                )
                                return TerminalStatus.COMPLETED
                            break

        # Default: Agent is IDLE, waiting for user input
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent's final response message using green arrow indicator."""
        # Strip ANSI codes for pattern matching
        clean_output = strip_terminal_escapes(script_output)

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
            # Kiro CLI 2.3.0+ may not emit a Credits line. Fall back to
            # extracting content between separators around the response.
            # Only attempt this when we know input was sent — without
            # _input_received the original error contract is preserved.
            if self._input_received:
                idle_idx = None
                for i in range(len(lines) - 1, -1, -1):
                    if re.search(NEW_TUI_IDLE_PATTERN, lines[i]):
                        idle_idx = i
                        break

                if idle_idx is not None:
                    # Find the last separator before idle (TUI frame boundary)
                    end_separator_idx = None
                    for i in range(idle_idx - 1, -1, -1):
                        if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                            end_separator_idx = i
                            break

                    # Find the separator before that (start of response area)
                    start_separator_idx = None
                    if end_separator_idx is not None:
                        for i in range(end_separator_idx - 1, -1, -1):
                            if re.search(TUI_SEPARATOR_PATTERN, lines[i].strip()):
                                start_separator_idx = i
                                break

                    if start_separator_idx is not None and end_separator_idx is not None:
                        content_lines = lines[start_separator_idx + 1 : end_separator_idx]
                        # Skip only the actual TUI header line (agent · model · N%)
                        content_lines = [
                            l
                            for l in content_lines
                            if not re.search(self._new_tui_header_pattern, l)
                        ]
                        # Skip first paragraph (user message echo)
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
                        response_lines = content_lines[agent_start:]
                        final_answer = "\n".join(response_lines).strip()
                        if final_answer:
                            final_answer = re.sub(ESCAPE_SEQUENCE_PATTERN, "", final_answer)
                            final_answer = re.sub(CONTROL_CHAR_PATTERN, "", final_answer)
                            return final_answer.strip()

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
