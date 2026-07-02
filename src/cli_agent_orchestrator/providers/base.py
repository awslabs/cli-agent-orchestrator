"""Base provider interface for CLI tool abstraction.

This module defines the abstract base class that all CLI providers must implement.
A "provider" is an adapter that enables CAO to interact with a specific CLI-based
AI agent (e.g., Kiro CLI, Claude Code, Codex).

Provider Responsibilities:
- Initialize the CLI tool in a tmux window (run startup commands)
- Detect terminal state by parsing terminal output (IDLE, PROCESSING, COMPLETED, etc.)
- Extract agent responses from terminal output
- Provide cleanup logic when terminal is deleted

Implemented Providers:
- KiroCliProvider: For Kiro CLI (kiro-cli chat)
- ClaudeCodeProvider: For Claude Code (claude)
- CodexProvider: For Codex CLI (codex)

Each provider must implement pattern matching for its specific CLI's prompt
and output format to reliably detect status changes.
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus

logger = logging.getLogger(__name__)


class BaseProvider(ABC):
    """Abstract base class for CLI tool providers.

    All CLI providers must inherit from this class and implement the abstract methods.
    The provider abstraction allows CAO to work with different CLI-based AI agents
    through a unified interface.

    Attributes:
        terminal_id: Unique identifier for the terminal this provider manages
        session_name: Name of the tmux session containing the terminal
        window_name: Name of the tmux window containing the terminal
        _status: Internal status cache (use get_status() for current status)
        _allowed_tools: CAO-vocabulary tool names this agent is allowed to use
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        allowed_tools: Optional[List[str]] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider with terminal context.

        Args:
            terminal_id: Unique identifier for this terminal instance
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            allowed_tools: Optional list of CAO tool names the agent is allowed to use
            skill_prompt: Optional skill catalog text built by the service layer.
                Providers append this to the system prompt when building their CLI command.
        """
        self.terminal_id = terminal_id
        self.session_name = session_name
        self.window_name = window_name
        self._status = TerminalStatus.IDLE
        self._allowed_tools: Optional[List[str]] = allowed_tools
        self._skill_prompt: Optional[str] = skill_prompt
        self._shell_baseline: Optional[str] = None
        # Native-status (herdr) dispatch tracking. _task_dispatched disambiguates
        # herdr's ambiguous "idle" (pre-first-turn IDLE vs post-turn COMPLETED);
        # the two *_first_detected stamps drive the post-completion buffer-flush
        # wait in _resolve_native_status(). Set by mark_input_received().
        self._task_dispatched: bool = False
        self._last_dispatch_time: float = 0.0
        self._done_first_detected: float = 0.0
        self._idle_first_detected: float = 0.0

    @property
    def shell_baseline(self) -> Optional[str]:
        """Shell process name captured before the CLI tool launched.

        Used by providers to detect when the CLI tool has exited and the
        shell is showing again (current pane command matches this baseline).
        """
        return self._shell_baseline

    @shell_baseline.setter
    def shell_baseline(self, value: Optional[str]) -> None:
        self._shell_baseline = value

    @property
    def status(self) -> TerminalStatus:
        """Get current provider status."""
        return self._status

    @property
    def paste_enter_count(self) -> int:
        """Number of Enter keys to send after pasting user input.

        After bracketed paste (``paste-buffer -p``), many TUIs (e.g.
        Claude Code) enter multi-line mode. The first Enter adds a
        newline; the second Enter on the empty line triggers submission.

        Default is 2 (double-Enter). Override to 1 for TUIs where single
        Enter submits after bracketed paste.
        """
        return 2

    @abstractmethod
    async def initialize(self) -> bool:
        """Initialize the provider (e.g., start CLI tool, send setup commands).

        Returns:
            bool: True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    def get_status(self, buffer: str) -> TerminalStatus:
        """Detect terminal status from output buffer using provider-specific patterns.

        Called by StatusMonitor with the accumulated terminal output.

        IMPORTANT — input contract: ``buffer`` is the **raw** pipe-pane byte
        stream (cursor-positioning escapes, in-place ``\\r`` redraws, OSC titles),
        NOT a tmux-rendered pane snapshot. Implementations that do structural /
        line-oriented matching MAY run it through
        ``cli_agent_orchestrator.utils.text.strip_terminal_escapes`` first
        (which removes escapes and normalizes cursor moves to newlines).
        Detectors calibrated against rendered snapshots will misfire on the raw
        stream if they skip this step. This is deliberately not a hard
        requirement: some providers depend on the raw escapes (kiro_cli
        preserves ``\\r`` for permission-prompt detection — see the comment in
        its get_status) and must NOT be "fixed" to comply.

        Args:
            buffer: Raw terminal output (up to ~8KB rolling buffer).

        Returns:
            TerminalStatus - always returns a valid status.
            UNKNOWN if no pattern matched, ERROR only for matched error patterns.
        """
        pass

    # Opt-in flag for pyte-rendered status detection. A provider sets this True
    # ONLY when it ships a purpose-built get_status_from_screen() calibrated for
    # a composited fixed-height viewport (not the raw byte stream). When False,
    # the StatusMonitor never routes this provider through the screen path even
    # if CAO_PYTE_STATUS is on — protecting providers (and kiro_cli, which
    # depends on raw \r) whose detectors are tuned for the raw stream.
    supports_screen_detection: bool = False

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-rendered screen (composited viewport).

        ``screen_lines`` is ``pyte.Screen.display``: a fixed-height list of
        viewport rows with all cursor moves and in-place redraws already
        resolved, escape-free, right-padded with spaces. The StatusMonitor
        calls this (instead of get_status) when CAO_PYTE_STATUS is enabled AND
        ``supports_screen_detection`` is True. It is invoked on two edges only —
        the RISING edge (output resumes after a quiet period) and at QUIESCENCE
        (no new output for the debounce window) — never mid-burst, so a frame is
        either freshly-resumed or fully settled, not half-drawn. Detectors must
        not assume every frame is fully settled.

        Default implementation joins the rows into a newline-delimited string
        and delegates to get_status — a safe no-op fallback for providers that
        have not been migrated. Override with a viewport-anchored detector
        (see ClaudeCodeProvider) and set ``supports_screen_detection = True``.
        """
        return self.get_status("\n".join(screen_lines))

    @property
    def paste_submit_delay(self) -> float:
        """Seconds to wait after a bracketed paste before sending the Enter key.

        Some TUIs need time to finish processing the bracketed-paste end marker
        before an Enter registers as "submit" rather than a literal newline.
        Override per-provider when a CLI needs longer than the default (e.g. the
        newest Claude Code, whose Ink renderer swallows an Enter sent too soon).
        """
        return 0.3

    @property
    def accepts_input_while_processing(self) -> bool:
        """Whether this provider buffers pasted input during PROCESSING for next-turn pickup.

        When True AND CAO_EAGER_INBOX_DELIVERY is enabled, the inbox service will
        deliver messages to this terminal even when its status is PROCESSING,
        rather than waiting for IDLE/COMPLETED.

        Override in subclasses for providers whose TUI buffers input at all times
        (e.g., Claude Code's Ink renderer).
        """
        return False

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Whether assign/handoff should pause when the provider is waiting on UI input.

        Some CLIs render interactive pickers or approval prompts where pasted
        task text would be interpreted as the answer to that prompt. Providers
        with those surfaces can opt in so CAO blocks orchestrated task delivery
        while still allowing explicit user-prompt answers.
        """
        return False

    @property
    def extraction_retries(self) -> int:
        """Number of extraction retries for transient TUI rendering issues.

        TUI-based providers (e.g. Antigravity CLI's renderer) may show
        notification spinners that temporarily obscure response text in
        the tmux capture buffer.  Override this to enable automatic retries
        with re-capture between attempts.  Default is 0 (no retries).
        """
        return 0

    @abstractmethod
    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last message from terminal script output.

        Args:
            script_output: Raw terminal output/script content

        Returns:
            str: Extracted last message from the provider
        """
        pass

    @abstractmethod
    def exit_cli(self) -> str:
        """Get the command to exit the provider CLI.

        Returns:
            Command string to send to terminal for exiting
        """
        pass

    @abstractmethod
    def cleanup(self) -> None:
        """Clean up provider resources."""
        pass

    def mark_input_received(self) -> None:
        """Notify the provider that external input was sent to the terminal.

        Called by the terminal service after send_input() delivers a message.
        Records that a task was dispatched so the native-status path can
        distinguish herdr "idle" after task completion (-> COMPLETED) from
        "idle" before any task was dispatched (-> IDLE), and resets the
        post-completion flush-wait timers.

        Providers may override to also update their own buffer-detection flag,
        but should call ``super().mark_input_received()`` to preserve the shared
        native-status tracking.
        """
        self._task_dispatched = True
        self._last_dispatch_time = time.time()
        self._done_first_detected = 0.0
        self._idle_first_detected = 0.0

    def _resolve_native_status(self) -> Optional[TerminalStatus]:
        """Resolve status from the backend's native agent state, if available.

        On the herdr backend, ``pipe_pane`` is a no-op so the StatusMonitor
        buffer is always empty; status must come from herdr's native pane state
        rather than buffer parsing. Every provider calls this at the top of
        ``get_status()``; when it returns non-None the buffer path is skipped.

        The tmux backend returns None from ``get_native_status()``, so this
        returns None and the caller falls through to its buffer analysis
        unchanged.

        The only ambiguous native state is IDLE: herdr reports "idle" both
        before any task has been dispatched AND after a task completed (e.g. the
        user focused the tab, resetting "done" -> "idle"). ``_task_dispatched``
        (set by mark_input_received()) disambiguates. COMPLETED ("done") and
        IDLE-post-dispatch both wait 10s from first detection for the pane buffer
        to flush before reporting COMPLETED, so extract_last_message sees settled
        output; the idle path gives up (reports COMPLETED) 300s after dispatch.
        """
        from cli_agent_orchestrator.backends.registry import get_backend

        native = get_backend().get_native_status(self.session_name, self.window_name)
        logger.debug(
            "[get_status] terminal=%s native=%s",
            self.terminal_id,
            native.value if native is not None else None,
        )
        if native is None:
            return None
        if native == TerminalStatus.PROCESSING:
            # Reset flush-wait timers — herdr is actively working, so any
            # previously stamped idle/done timestamp is from a pre-work gap
            # and must not be counted toward the post-completion flush wait.
            self._done_first_detected = 0.0
            self._idle_first_detected = 0.0
            logger.debug("[get_status] terminal=%s -> PROCESSING (native)", self.terminal_id)
            return TerminalStatus.PROCESSING
        if native == TerminalStatus.COMPLETED and self._task_dispatched:
            # herdr "done": wait 10s from first detection for buffer to flush.
            if self._done_first_detected == 0.0:
                self._done_first_detected = time.time()
            waited = time.time() - self._done_first_detected
            if waited >= 10.0:
                logger.debug(
                    "[get_status] terminal=%s -> COMPLETED (native done, %.1fs flush wait elapsed)",
                    self.terminal_id,
                    waited,
                )
                return TerminalStatus.COMPLETED
            logger.debug(
                "[get_status] terminal=%s -> PROCESSING (native done, flush wait %.1fs/10s)",
                self.terminal_id,
                waited,
            )
            return TerminalStatus.PROCESSING
        if native == TerminalStatus.IDLE and self._task_dispatched:
            # herdr "idle" post-dispatch: wait 10s from first detection for buffer to flush,
            # then report COMPLETED (warn if still idle 5 min after dispatch).
            if self._idle_first_detected == 0.0:
                self._idle_first_detected = time.time()
            waited = time.time() - self._idle_first_detected
            elapsed = time.time() - self._last_dispatch_time
            if waited >= 10.0:
                if elapsed >= 300.0:
                    logger.warning(
                        "[get_status] terminal=%s -> COMPLETED (native idle, %.0fs since dispatch, giving up)",
                        self.terminal_id,
                        elapsed,
                    )
                    return TerminalStatus.COMPLETED
                logger.debug(
                    "[get_status] terminal=%s -> COMPLETED (native idle, %.1fs flush wait elapsed)",
                    self.terminal_id,
                    waited,
                )
                return TerminalStatus.COMPLETED
            logger.debug(
                "[get_status] terminal=%s -> PROCESSING (native idle, flush wait %.1fs/10s)",
                self.terminal_id,
                waited,
            )
            return TerminalStatus.PROCESSING
        if native == TerminalStatus.IDLE:
            logger.debug(
                "[get_status] terminal=%s -> IDLE (native idle, no task dispatched)",
                self.terminal_id,
            )
            return TerminalStatus.IDLE
        # COMPLETED (no task dispatched), WAITING_USER_ANSWER, ERROR -- return directly
        logger.debug("[get_status] terminal=%s -> %s (native)", self.terminal_id, native.value)
        return native

    @staticmethod
    def _extract_questions(user_messages: List[str]) -> List[str]:
        """Extract lines containing '?' from user messages."""
        questions: List[str] = []
        for msg in user_messages:
            for line in msg.splitlines():
                stripped = line.strip()
                if "?" in stripped and len(stripped) > 5:
                    questions.append(stripped)
        return questions[-5:]  # last 5

    @staticmethod
    def _extract_decisions(assistant_text: str) -> List[str]:
        """Extract decision-like sentences from assistant output."""
        decision_indicators = re.compile(
            r"(?:I(?:'ll| will| have| decided| chose| went with|'m going to)|"
            r"(?:The |My |Our )?(?:approach|decision|plan|solution|strategy) (?:is|was|will be)|"
            r"(?:We should|Let's|Going to|Decided to|Chose to))",
            re.IGNORECASE,
        )
        decisions: List[str] = []
        for line in assistant_text.splitlines():
            stripped = line.strip()
            if decision_indicators.search(stripped) and len(stripped) > 10:
                # Trim to first sentence if very long
                if len(stripped) > 200:
                    stripped = stripped[:200] + "..."
                decisions.append(stripped)
        return decisions[-10:]  # last 10

    @staticmethod
    def _extract_file_paths(text: str) -> List[str]:
        """Extract file paths mentioned in terminal output.

        Looks for common patterns: paths with extensions, tool-use file references.
        """
        # Match paths like src/foo/bar.py, ./test.js, /abs/path.ts
        path_pattern = re.compile(
            r"(?:^|[\s\"'`(])(" r"(?:\.{0,2}/)?(?:[\w.-]+/)+[\w.-]+\.\w{1,10}" r")"
        )
        seen: set[str] = set()
        paths: List[str] = []
        for match in path_pattern.finditer(text):
            p = match.group(1)
            if p not in seen and not p.startswith("http"):
                seen.add(p)
                paths.append(p)
        return paths[-20:]  # last 20

    def _build_context_dict(
        self,
        provider_name: str,
        last_task: str,
        key_decisions: List[str],
        open_questions: List[str],
        files_changed: List[str],
    ) -> Dict[str, Any]:
        """Build the standard session context dict."""
        return {
            "provider": provider_name,
            "terminal_id": self.terminal_id,
            "last_task": last_task,
            "key_decisions": key_decisions,
            "open_questions": open_questions,
            "files_changed": files_changed,
        }

    def _apply_skill_prompt(self, system_prompt: str) -> str:
        """Append skill catalog text to a system prompt if available.

        Args:
            system_prompt: The base system prompt string.

        Returns:
            The system prompt with skill catalog appended, or unchanged if
            no skill_prompt was provided.
        """
        if not self._skill_prompt:
            return system_prompt
        if system_prompt:
            return f"{system_prompt}\n\n{self._skill_prompt}"
        return self._skill_prompt

    def _update_status(self, status: TerminalStatus) -> None:
        """Update internal status."""
        self._status = status
