"""Base provider interface for CLI tool abstraction.

This module defines the abstract base class that all CLI providers must implement.
A "provider" is an adapter that enables CAO to interact with a specific CLI-based
AI agent (e.g., Kiro CLI, Claude Code, Codex, Q CLI).

Provider Responsibilities:
- Initialize the CLI tool in a tmux window (run startup commands)
- Detect terminal state by parsing terminal output (IDLE, PROCESSING, COMPLETED, etc.)
- Extract agent responses from terminal output
- Provide cleanup logic when terminal is deleted

Implemented Providers:
- KiroCliProvider: For Kiro CLI (kiro-cli chat)
- ClaudeCodeProvider: For Claude Code (claude)
- CodexProvider: For Codex CLI (codex)
- QCliProvider: For Amazon Q Developer CLI (q chat)

Each provider must implement pattern matching for its specific CLI's prompt
and output format to reliably detect status changes.
"""

import re
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.models.terminal import TerminalStatus


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
    def initialize(self) -> bool:
        """Initialize the provider (e.g., start CLI tool, send setup commands).

        Returns:
            bool: True if initialization successful, False otherwise
        """
        pass

    @abstractmethod
    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get current provider status by analyzing terminal output.

        Args:
            tail_lines: Number of lines to capture from terminal (default: provider-specific)

        Returns:
            TerminalStatus: Current status of the provider
        """
        pass

    @abstractmethod
    def get_idle_pattern_for_log(self) -> str:
        """Get pattern that indicates IDLE state in log file output.

        Used for quick detection in file watcher before calling full get_status().
        Should return a simple pattern that appears in the IDLE prompt.

        Returns:
            str: Pattern to search for in log file tail
        """
        pass

    @property
    def extraction_retries(self) -> int:
        """Number of extraction retries for transient TUI rendering issues.

        TUI-based providers (e.g. Gemini CLI's Ink renderer) may show
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
        Providers can override this to adjust status detection behavior.
        For example, providers with initial prompts can use this to
        distinguish post-init idle (ready for first input) from
        post-task completed.
        """
        pass

    def register_hooks(
        self,
        working_directory: Optional[str],
        agent_profile: Optional[str],
    ) -> None:
        """Install provider-specific hooks.

        Default is a no-op. After the plugin migration, memory-context
        injection is handled by plugins listening on ``post_create_terminal``
        (see ``plugins/builtin/``). This hook remains on ``BaseProvider`` so
        ``terminal_service.create_terminal`` can still call it uniformly and
        future providers can opt in without a ladder.

        Failures must not be raised past the caller — the service wraps this
        call in a try/except and logs a warning so that hook-registration
        hiccups never block terminal creation.
        """
        return

    @abstractmethod
    async def extract_session_context(self) -> Dict[str, Any]:
        """Extract structured context from this provider's session.

        Parses the terminal output to build a summary of what happened
        in this session.  Used by the ``session_context`` MCP tool and
        the context-manager agent.

        Returns:
            A dict with keys:
            - provider:       provider type string
            - terminal_id:    terminal identifier
            - last_task:      last user message / task description
            - key_decisions:  list of key decisions from assistant responses
            - open_questions: list of open questions from user messages
            - files_changed:  list of file paths modified during session

            Returns empty dict if no session data is found.
        """
        pass

    def get_context_usage_percentage(self) -> Optional[float]:
        """Return the provider's current context window usage as a float 0.0–1.0.

        Providers that expose context usage metrics (e.g. Claude Code via
        its JSONL transcript) should override this method.

        Returns:
            Float between 0.0 and 1.0 representing context usage, or
            ``None`` if this provider does not expose the metric.
        """
        return None

    # ------------------------------------------------------------------
    # Session context extraction helpers
    # ------------------------------------------------------------------

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
