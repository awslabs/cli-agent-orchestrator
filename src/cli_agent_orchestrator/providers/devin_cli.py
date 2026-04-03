"""Devin CLI provider implementation."""

from __future__ import annotations

import json
import logging
import re
import shlex
import sys
import tempfile
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]"
OSC_PATTERN = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
CONTROL_CHARS_PATTERN = r"[\x00-\x08\x0b-\x1f\x7f]"

# Devin TUI layout:
#   > user message        <- user input prefix
#   Response text         <- agent reply
#   ────────────────────  <- horizontal rule (U+2500–U+257F)
#   #                     <- input prompt (fixed chrome — NEVER disappears)
#   ────────────────────  <- horizontal rule
#   Mode: ... Model: ...  <- status bar

# The `#` prompt is fixed TUI chrome and never disappears during processing.
# Use a relaxed pattern that also accepts ghost/autocomplete text after `#`.
INPUT_PROMPT_PATTERN = r"^\s*#"
STATUS_BAR_PATTERN = r"Mode:.*Model:"

# Horizontal rule: one or more chars in Unicode box-drawing range U+2500–U+257F
HORIZONTAL_RULE_PATTERN = r"^[\u2500-\u257f]{3,}"

# User input lines are prefixed with "> " (with content after the space).
USER_INPUT_PATTERN = r"^>\s+\S"

# Processing state indicators (take priority over the fixed `#` prompt)
PROCESSING_PATTERNS = [
    r"Running tools",
    r"esc to interrupt",
    r"Running:",
    r"Executing:",
    r"Reading file",
    r"Writing to",
    r"Editing file",
]

IDLE_PROMPT_PATTERN_LOG = r"Mode:.*Model:"


class DevinCliProvider(BaseProvider):
    """Provider for Devin CLI (https://cli.devin.ai/)."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._initialized = False
        self._agent_profile = agent_profile
        self._temp_prompt_file: Optional[str] = None
        self._temp_config_file: Optional[str] = None

    @property
    def paste_enter_count(self) -> int:
        return 1

    @staticmethod
    def _clean(output: str) -> str:
        cleaned = (output or "").replace("\r\n", "\n").replace("\r", "\n")
        cleaned = re.sub(OSC_PATTERN, "", cleaned)
        cleaned = re.sub(ANSI_CODE_PATTERN, "", cleaned)
        return re.sub(CONTROL_CHARS_PATTERN, "", cleaned)

    def _history(self, tail_lines: Optional[int] = None) -> str:
        raw = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)
        return self._clean(raw)

    def _build_command(self) -> str:
        """Build the devin CLI command."""
        command_parts = [
            "devin",
            "--permission-mode",
            "dangerous",
            "--respect-workspace-trust",
            "false",
        ]

        if self._agent_profile:
            # Write agent profile content to a temp file
            try:
                from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

                profile = load_agent_profile(self._agent_profile)
                if profile.system_prompt:
                    tmp = tempfile.NamedTemporaryFile(
                        mode="w",
                        suffix=".md",
                        delete=False,
                        prefix="devin_profile_",
                    )
                    tmp.write(profile.system_prompt)
                    tmp.flush()
                    tmp.close()
                    self._temp_prompt_file = tmp.name
                    command_parts.extend(["--prompt-file", tmp.name])
            except (FileNotFoundError, RuntimeError, OSError):
                logger.debug("Could not load agent profile '%s' for Devin CLI", self._agent_profile)

        # Build MCP config
        mcp_config = self._build_mcp_config()
        if mcp_config:
            tmp_cfg = tempfile.NamedTemporaryFile(
                mode="w",
                suffix=".json",
                delete=False,
                prefix="devin_config_",
            )
            json.dump(mcp_config, tmp_cfg, ensure_ascii=False)
            tmp_cfg.flush()
            tmp_cfg.close()
            self._temp_config_file = tmp_cfg.name
            command_parts.extend(["--config", tmp_cfg.name])

        return shlex.join(command_parts)

    def _build_mcp_config(self) -> Optional[dict]:
        """Build the MCP server config dict for --config."""
        import shutil

        venv_script = Path(sys.executable).with_name("cao-mcp-server")
        found_script = shutil.which("cao-mcp-server")
        if venv_script.exists():
            mcp_command = str(venv_script)
            mcp_args: list = []
        elif found_script:
            mcp_command = found_script
            mcp_args = []
        else:
            mcp_command = sys.executable
            mcp_args = ["-m", "cli_agent_orchestrator.mcp_server.server"]

        return {
            "mcpServers": {
                "cao-mcp-server": {
                    "command": mcp_command,
                    "args": mcp_args,
                    "env": {"CAO_TERMINAL_ID": self.terminal_id},
                }
            }
        }

    def initialize(self) -> bool:
        """Initialize Devin CLI provider."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_command()
        tmux_client.send_keys(self.session_name, self.window_name, command)

        if not wait_until_status(
            self, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=60.0
        ):
            raise TimeoutError("Devin CLI initialization timed out after 60 seconds")

        self._initialized = True
        return True

    @staticmethod
    def _is_processing(lines: list[str]) -> bool:
        """Return True if any processing pattern is visible in the recent output."""
        combined = "\n".join(lines[-50:])
        for pattern in PROCESSING_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _has_status_bar(lines: list[str]) -> bool:
        """Return True if the Devin status bar (Mode: ... Model:) is visible."""
        for line in reversed(lines[-20:]):
            if re.search(STATUS_BAR_PATTERN, line):
                return True
        return False

    @staticmethod
    def _has_input_prompt(lines: list[str]) -> bool:
        """Return True if the `#` input prompt is visible near the bottom."""
        for line in reversed(lines[-20:]):
            if re.match(INPUT_PROMPT_PATTERN, line):
                return True
        return False

    @staticmethod
    def _has_user_input(lines: list[str]) -> bool:
        """Return True if at least one user-input line (`> text`) is visible."""
        for line in lines:
            if re.match(USER_INPUT_PATTERN, line):
                return True
        return False

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Detect Devin CLI state from terminal output.

        Decision tree:
        1. Processing patterns (Running tools, esc to interrupt, …) → PROCESSING
        2. `#` prompt visible AND status bar visible:
           a. `> user_input` line exists → check for response → COMPLETED or PROCESSING
           b. No user input line → IDLE
        3. Neither prompt nor status bar → PROCESSING (still starting up)
        """
        effective_tail = tail_lines if tail_lines is not None else 220
        output = self._history(tail_lines=effective_tail)
        if not output.strip():
            return TerminalStatus.PROCESSING

        lines = output.splitlines()

        # 1. Processing spinner patterns take priority over the fixed `#` prompt.
        if self._is_processing(lines):
            return TerminalStatus.PROCESSING

        # 2. Require both the input prompt and status bar to consider terminal ready.
        has_prompt = self._has_input_prompt(lines)
        has_status = self._has_status_bar(lines)

        if not (has_prompt and has_status):
            return TerminalStatus.PROCESSING

        # 3. Distinguish IDLE from COMPLETED based on user-input lines.
        if not self._has_user_input(lines):
            return TerminalStatus.IDLE

        # There is at least one `> text` user input.  Check whether there is
        # response content between the last user input and the horizontal rule.
        last_user_idx = -1
        for idx, line in enumerate(lines):
            if re.match(USER_INPUT_PATTERN, line):
                last_user_idx = idx

        response_lines = []
        for line in lines[last_user_idx + 1 :]:
            if re.match(HORIZONTAL_RULE_PATTERN, line.strip()):
                break
            if re.match(INPUT_PROMPT_PATTERN, line):
                break
            if line.strip():
                response_lines.append(line)

        if response_lines:
            return TerminalStatus.COMPLETED

        # User input present but no response yet — still processing.
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract agent response between last user-input line and horizontal rule."""
        clean_output = self._clean(script_output)
        lines = clean_output.splitlines()

        # Find the last user-input line ("> text")
        last_user_idx = -1
        for idx, line in enumerate(lines):
            if re.match(USER_INPUT_PATTERN, line):
                last_user_idx = idx

        if last_user_idx < 0:
            raise ValueError("No Devin CLI user input found — cannot locate response")

        # Collect lines between the last user input and the next horizontal rule or `#` prompt.
        response_lines = []
        for line in lines[last_user_idx + 1 :]:
            if re.match(HORIZONTAL_RULE_PATTERN, line.strip()):
                break
            if re.match(INPUT_PROMPT_PATTERN, line):
                break
            response_lines.append(line)

        # Strip blank lines from head and tail
        while response_lines and not response_lines[0].strip():
            response_lines.pop(0)
        while response_lines and not response_lines[-1].strip():
            response_lines.pop()

        message = "\n".join(response_lines).strip()
        if not message:
            raise ValueError("Empty Devin CLI response — no content found after user input")

        return message

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        """Clean up temporary files and provider state."""
        self._initialized = False
        for tmp_path in (self._temp_prompt_file, self._temp_config_file):
            if tmp_path:
                try:
                    Path(tmp_path).unlink(missing_ok=True)
                except OSError as exc:
                    logger.debug("Failed to remove temp file '%s': %s", tmp_path, exc)
        self._temp_prompt_file = None
        self._temp_config_file = None
