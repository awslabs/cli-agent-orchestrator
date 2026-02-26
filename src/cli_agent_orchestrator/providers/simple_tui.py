"""Simple provider base for TUI-style CLI tools."""

import logging
import re
import time
from typing import Iterable, Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"


class SimpleTuiProvider(BaseProvider):
    """Provider with generic status detection for interactive CLI tools."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        start_command: str,
        idle_prompt_pattern: str = r"[>❯›]\s",
        idle_prompt_pattern_log: str = r"[>❯›]\s",
        auto_accept_patterns: Optional[Iterable[str]] = None,
        waiting_patterns: Optional[Iterable[str]] = None,
        processing_patterns: Optional[Iterable[str]] = None,
        error_patterns: Optional[Iterable[str]] = None,
        exit_command: str = "C-d",
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._start_command = start_command
        self._idle_prompt_pattern = idle_prompt_pattern
        self._idle_prompt_pattern_log = idle_prompt_pattern_log
        self._auto_accept_patterns = list(
            auto_accept_patterns
            if auto_accept_patterns is not None
            else [
                r"trust this folder",
                r"do you trust",
                r"allow .* action",
            ]
        )
        self._waiting_patterns = list(
            waiting_patterns
            if waiting_patterns is not None
            else [
                r"yes/no",
                r"\[y/n",
                r"waiting for your approval",
                r"allow .* action",
            ]
        )
        self._processing_patterns = list(
            processing_patterns
            if processing_patterns is not None
            else [
                r"thinking",
                r"working",
                r"analyzing",
                r"processing",
                r"esc to interrupt",
            ]
        )
        self._error_patterns = list(
            error_patterns
            if error_patterns is not None
            else [
                r"^error:",
                r"traceback",
                r"failed",
                r"exception",
            ]
        )
        self._exit_command = exit_command
        self._initialized = False
        self._input_received = False

    def _clean_output(self, output: str) -> str:
        return re.sub(ANSI_CODE_PATTERN, "", output)

    def _has_idle_prompt(self, clean_output: str) -> bool:
        lines = clean_output.splitlines()
        for line in lines[-8:]:
            if re.search(self._idle_prompt_pattern, line):
                return True
        return False

    def _matches_any(self, patterns: Iterable[str], text: str) -> bool:
        return any(re.search(pattern, text, re.IGNORECASE | re.MULTILINE) for pattern in patterns)

    def _handle_startup_prompts(self, timeout: float = 20.0) -> None:
        """Auto-accept common workspace trust/allow prompts."""
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = tmux_client.get_history(self.session_name, self.window_name)
            if not output:
                time.sleep(1.0)
                continue

            clean_output = self._clean_output(output)
            if self._has_idle_prompt(clean_output):
                return

            if self._matches_any(self._auto_accept_patterns, clean_output):
                logger.info("Startup trust/permission prompt detected, auto-accepting")
                session = tmux_client.server.sessions.get(session_name=self.session_name)
                if session is None:
                    time.sleep(1.0)
                    continue
                window = session.windows.get(window_name=self.window_name)
                if window is None:
                    time.sleep(1.0)
                    continue
                pane = window.active_pane
                if pane:
                    pane.send_keys("", enter=True)
                time.sleep(1.0)
                continue

            time.sleep(1.0)

    def initialize(self) -> bool:
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        tmux_client.send_keys(self.session_name, self.window_name, self._start_command)
        self._handle_startup_prompts(timeout=20.0)

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=1.0):
            raise TimeoutError("CLI initialization timed out after 60 seconds")

        self._initialized = True
        self._input_received = False
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        if not output:
            return TerminalStatus.ERROR

        clean_output = self._clean_output(output)
        tail_output = "\n".join(clean_output.splitlines()[-40:])

        if self._matches_any(self._waiting_patterns, tail_output):
            return TerminalStatus.WAITING_USER_ANSWER

        if self._matches_any(self._error_patterns, tail_output):
            return TerminalStatus.ERROR

        if self._matches_any(self._processing_patterns, tail_output) and not self._has_idle_prompt(
            clean_output
        ):
            return TerminalStatus.PROCESSING

        if not self._has_idle_prompt(clean_output):
            return TerminalStatus.PROCESSING

        if not self._input_received:
            return TerminalStatus.IDLE

        # With unknown provider-specific transcript formats, consider returning to idle
        # after a user message as task completion for orchestration workflows.
        return TerminalStatus.COMPLETED

    def get_idle_pattern_for_log(self) -> str:
        return self._idle_prompt_pattern_log

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean_output = self._clean_output(script_output)

        # Prefer assistant markers if present.
        assistant_matches = list(
            re.finditer(r"(?m)^\s*(?:assistant:|codex:|agent:|•|⏺)\s*", clean_output)
        )
        if assistant_matches:
            start = assistant_matches[-1].end()
            remaining = clean_output[start:]
            lines = []
            for line in remaining.splitlines():
                if re.search(rf"^\s*{self._idle_prompt_pattern}", line):
                    break
                lines.append(line)
            message = "\n".join(lines).strip()
            if message:
                return message

        # Fallback to text after the last prompt line.
        last_prompt_end = 0
        for match in re.finditer(rf"(?m)^\s*{self._idle_prompt_pattern}.*$", clean_output):
            last_prompt_end = match.end()
        fallback = clean_output[last_prompt_end:].strip()
        if fallback:
            return fallback

        raise ValueError("Unable to extract response from provider output")

    def exit_cli(self) -> str:
        return self._exit_command

    def cleanup(self) -> None:
        self._initialized = False

    def mark_input_received(self) -> None:
        self._input_received = True
