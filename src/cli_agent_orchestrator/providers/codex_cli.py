"""Codex CLI provider implementation."""

import logging
import re
from typing import Optional

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Regular expressions for stripping ANSI/terminal control sequences
CSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # ANSI CSI sequences
OSC_PATTERN = re.compile(r"\x1b\][^\x07]*\x07")       # Operating system commands (OSC)
ST_PATTERN = re.compile(r"\x1b\][^\x1b]*\x1b\\")     # OSC terminated by ST
SINGLE_ESCAPE_PATTERN = re.compile(r"\x1b[@-Z\\-_]")     # Single-character escapes
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Status indicators used by Codex CLI
ESC_TO_INTERRUPT = "esc to interrupt"
WORKING_TOKEN = "working"
APPROVAL_TOKENS = (
    "allow codex to run",
    "requires your approval",
    "press y to allow",
    "enter y to continue",
)
ERROR_TOKENS = (
    "error:",
    "usage limit",
    "failed to",
)


class CodexCliProvider(BaseProvider):
    """Provider for Codex CLI integration."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str, agent_profile: Optional[str] = None):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile

    def initialize(self) -> bool:
        """Launch Codex CLI inside the tmux pane and wait until it's idle."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = "codex"
        # Fire up the interactive Codex TUI inside the tmux pane.
        # TODO: map agent_profile to a named Codex workspace once the CLI supports it.
        tmux_client.send_keys(self.session_name, self.window_name, command)

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=45.0):
            raise TimeoutError("Codex CLI initialization timed out after 45 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: int = None) -> TerminalStatus:
        """Determine Codex CLI status from tmux history."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        if not output:
            return TerminalStatus.ERROR

        clean = self._strip_output(output)
        lower = clean.lower()

        if not clean.strip():
            return TerminalStatus.ERROR

        # Immediate failure modes trump everything else.
        if any(token in lower for token in ERROR_TOKENS):
            return TerminalStatus.ERROR

        # Codex prints human-approval prompts when it needs unblock instructions.
        if any(token in lower for token in APPROVAL_TOKENS):
            return TerminalStatus.WAITING_USER_ANSWER

        # Codex prints responses as bullet lists; the latest bullet carries the active state.
        last_bullet = clean.rfind("•")
        if last_bullet != -1:
            tail = clean[last_bullet:]
            tail_lower = tail.lower()
            if ESC_TO_INTERRUPT in tail_lower:
                return TerminalStatus.PROCESSING
            if WORKING_TOKEN in tail_lower and ESC_TO_INTERRUPT in lower:
                return TerminalStatus.PROCESSING
            # Content after the bullet is the final response
            if tail.strip():
                return TerminalStatus.COMPLETED

        # Default to processing when Codex shows its spinner text.
        if ESC_TO_INTERRUPT in lower or WORKING_TOKEN in lower:
            return TerminalStatus.PROCESSING

        return TerminalStatus.IDLE

    def get_idle_pattern_for_log(self) -> str:
        """Return pattern that indicates Codex is idle in log tails."""
        # The footer with context remaining is re-rendered on each idle transition
        return r"100% context left"

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last Codex response from full tmux history."""
        # Trim escape codes so we can safely parse the Codex text transcript.
        clean = self._strip_output(script_output)
        prompt_index = clean.rfind('\n›')
        if prompt_index == -1:
            prompt_index = clean.rfind('›')
        if prompt_index == -1:
            raise ValueError("Incomplete Codex CLI response - no final prompt detected")

        search_area = clean[:prompt_index]
        # Responses always begin with a bullet followed by optional indentation.
        bullet_index = search_area.rfind('•')
        if bullet_index == -1:
            raise ValueError("No Codex CLI response found - no bullet pattern detected")

        message_block = search_area[bullet_index:]
        if ESC_TO_INTERRUPT in message_block.lower():
            raise ValueError("Codex CLI response still processing")

        lines = message_block.splitlines()
        collected = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped.startswith('•'):
                collected.append(stripped.lstrip('•').strip())
                continue
            if line.startswith('  ') or line.startswith('\t'):
                collected.append(line.strip())
            else:
                break

        if not collected:
            raise ValueError("Empty Codex CLI response - no content found")

        message = '\n'.join(collected).strip()
        return message

    def exit_cli(self) -> str:
        """Return control characters to terminate Codex CLI."""
        # Codex exits on Ctrl+C; sending twice ensures shutdown even during busy states.
        return "\u0003\u0003"

    def cleanup(self) -> None:
        """Reset initialization flag."""
        self._initialized = False

    @staticmethod
    def _strip_output(output: str) -> str:
        """Remove ANSI escape sequences and control characters."""
        text = CSI_PATTERN.sub('', output)
        text = OSC_PATTERN.sub('', text)
        text = ST_PATTERN.sub('', text)
        text = SINGLE_ESCAPE_PATTERN.sub('', text)
        text = CONTROL_CHAR_PATTERN.sub('', text)
        return text.replace('\r', '')
