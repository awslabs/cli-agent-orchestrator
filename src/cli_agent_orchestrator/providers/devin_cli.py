"""Devin CLI provider implementation."""

from __future__ import annotations

import json
import logging
import re
import shlex
import tempfile
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
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

STATUS_BAR_PATTERN = r"Mode:.*Model:"

# Horizontal rule: one or more chars in Unicode box-drawing range U+2500–U+257F
HORIZONTAL_RULE_PATTERN = r"^[\u2500-\u257f]{3,}"

# User input lines are prefixed with "> " (with content after the space).
USER_INPUT_PATTERN = r"^>\s+\S"

# Devin shows a "#" prompt when idle and waiting for input
IDLE_PROMPT_PATTERN = r"^[\s]*#[\s]*$"

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

# Explicit error indicators from Devin CLI or the underlying runtime.
# These are matched only when the TUI prompt is not visible, to avoid
# treating an agent response that mentions an error as a failure.
ERROR_PATTERNS = [
    r"^Error:",
    r"^Traceback \(most recent call last\):",
    r"^panic:",
    r"(?:authentication|login|credentials?|auth).{0,40}(?:failed|invalid|error|denied)",
    r"Devin CLI (?:crashed|exited|failed|error)",
]


class DevinCliProvider(BaseProvider):
    """Provider for Devin CLI (https://cli.devin.ai/)."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider with terminal context."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._temp_prompt_file: Optional[str] = None
        self._temp_config_file: Optional[str] = None

    @property
    def paste_enter_count(self) -> int:
        """Devin CLI needs a single Enter after pasted input."""
        return 1

    @property
    def use_paste_buffer(self) -> bool:
        """Devin CLI doesn't support paste-buffer - use send-keys instead."""
        return False

    @staticmethod
    def _clean(output: str) -> str:
        cleaned = (output or "").replace("\r\n", "\n").replace("\r", "\n")
        # Remove ANSI codes and OSC sequences
        cleaned = re.sub(ANSI_CODE_PATTERN, "", cleaned)
        cleaned = re.sub(OSC_PATTERN, "", cleaned)
        cleaned = re.sub(CONTROL_CHARS_PATTERN, "", cleaned)
        return cleaned

    def _cleanup_temp_files(self) -> None:
        """Clean up any existing temporary files before creating new ones."""
        if self._temp_prompt_file:
            try:
                Path(self._temp_prompt_file).unlink(missing_ok=True)
            except OSError:
                pass
            self._temp_prompt_file = None
        if self._temp_config_file:
            try:
                Path(self._temp_config_file).unlink(missing_ok=True)
            except OSError:
                pass
            self._temp_config_file = None

    def _build_security_constraint(self) -> str:
        """Build security constraint prompt for allowed tools."""
        if self._allowed_tools is None:
            return ""
        tools_list = ", ".join(self._allowed_tools)
        return (
            f"{SECURITY_PROMPT}\n"
            f"## ALLOWED TOOLS\n"
            f"You are restricted to only use the following tools: {tools_list}\n"
        )

    def _write_prompt_file(self, content: str) -> None:
        """Write prompt content to a temporary file and store the path."""
        with tempfile.NamedTemporaryFile(
            mode="w",
            prefix="cao_devin_prompt_",
            suffix=".md",
            delete=False,
            encoding="utf-8",
        ) as f:
            self._temp_prompt_file = f.name
            f.write(content)

    def _load_user_config(self) -> dict:
        """Load the user's existing Devin config or create a minimal one."""
        user_config_path = Path.home() / ".config" / "devin" / "config.json"
        if user_config_path.exists():
            try:
                data = json.loads(user_config_path.read_text())
                if isinstance(data, dict):
                    return data
            except (json.JSONDecodeError, OSError):
                pass
        # Minimal config to skip the first-run wizard
        return {
            "shell": {"setup_complete": True},
            "theme_mode": "dark",
        }

    def _merge_mcp_servers(self, base_config: dict, mcp_servers: dict) -> None:
        """Merge profile MCP servers into existing config."""
        # Ensure mcpServers is a dict in base_config
        if not isinstance(base_config.get("mcpServers"), dict):
            base_config["mcpServers"] = {}

        existing_mcp = base_config.get("mcpServers", {})
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                resolved = resolve_mcp_server_config(dict(server_config))
            else:
                resolved = resolve_mcp_server_config(server_config.model_dump(exclude_none=True))
            existing_mcp[server_name] = resolved
            # Safely handle env dict - ensure it's never None
            env = existing_mcp[server_name].get("env") or {}
            if not isinstance(env, dict):
                env = {}
            if "CAO_TERMINAL_ID" not in env:
                env["CAO_TERMINAL_ID"] = self.terminal_id
                existing_mcp[server_name]["env"] = env
        base_config["mcpServers"] = existing_mcp

    def _build_command(self) -> str:
        """Build Devin CLI command with agent profile if provided.

        Returns properly escaped shell command string for tmux.
        """
        self._cleanup_temp_files()

        command_parts = ["devin"]

        # Only use dangerous permission mode when allowed_tools is unrestricted
        # This follows the pattern of other providers (e.g., kiro_cli.py:250)
        if self._allowed_tools is not None and "*" in self._allowed_tools:
            command_parts.extend(
                [
                    "--permission-mode",
                    "dangerous",
                    "--respect-workspace-trust",
                    "false",
                ]
            )

        # Handle allowed_tools restrictions
        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            security_constraint = self._build_security_constraint()
            self._write_prompt_file(security_constraint)
            assert self._temp_prompt_file is not None
            command_parts.extend(["--prompt-file", self._temp_prompt_file])

        if self._agent_profile is not None:
            from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

            profile = load_agent_profile(self._agent_profile)

            # Devin supports --prompt-file for system prompt injection
            system_prompt = profile.system_prompt if profile.system_prompt else ""
            # Apply skill prompt if provided
            system_prompt = self._apply_skill_prompt(system_prompt)
            if system_prompt:
                # If we already have a prompt-file from allowed_tools, append the system prompt AFTER security constraint
                if self._temp_prompt_file:
                    with open(self._temp_prompt_file, "r", encoding="utf-8") as f:
                        existing_content = f.read()
                    combined_prompt = f"{existing_content}\n\n{system_prompt}"
                    with open(self._temp_prompt_file, "w", encoding="utf-8") as f:
                        f.write(combined_prompt)
                else:
                    self._write_prompt_file(system_prompt)
                    assert self._temp_prompt_file is not None
                    command_parts.extend(["--prompt-file", self._temp_prompt_file])

            # Add MCP config if present
            if profile.mcpServers:
                base_config = self._load_user_config()
                self._merge_mcp_servers(base_config, profile.mcpServers)

                with tempfile.NamedTemporaryFile(
                    mode="w",
                    prefix="cao_devin_config_",
                    suffix=".json",
                    delete=False,
                    encoding="utf-8",
                ) as f:
                    self._temp_config_file = f.name
                    f.write(json.dumps(base_config, indent=2))
                command_parts.extend(["--config", self._temp_config_file])

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Initialize Devin CLI provider."""
        try:
            # Wait for shell prompt to appear in the tmux window
            if not await wait_for_shell(self.terminal_id, timeout=10.0):
                raise TimeoutError("Shell initialization timed out after 10 seconds")

            command = self._build_command()
            from cli_agent_orchestrator.backends.registry import get_backend

            get_backend().send_keys(
                self.session_name,
                self.window_name,
                command,
                use_paste_buffer=True,  # Use paste-buffer for shell commands
            )

            if not await wait_until_status(
                self.terminal_id, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=60.0
            ):
                raise TimeoutError("Devin CLI initialization timed out after 60 seconds")

            self._initialized = True
            return True
        except Exception:
            # Clean up temp files on failure to prevent credential residue
            self.cleanup()
            raise

    @staticmethod
    def _is_processing(lines: list[str]) -> bool:
        """Return True if any processing pattern is visible in the recent output."""
        combined = "\n".join(lines[-50:])
        for pattern in PROCESSING_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE):
                return True
        return False

    @staticmethod
    def _has_input_prompt(lines: list[str]) -> bool:
        """Return True if the `#` input prompt preceded by a horizontal rule is visible.

        The Devin TUI always places a horizontal rule immediately before the `#`
        prompt.  Requiring this context avoids false positives from Markdown
        headings (e.g. ``# Title``) that appear inside agent responses.
        """
        tail = lines[-20:]
        for idx, line in enumerate(tail):
            if not re.match(IDLE_PROMPT_PATTERN, line):
                continue
            # Verify the closest preceding non-empty line is a horizontal rule.
            preceding = [line for line in tail[:idx] if line.strip()]
            if preceding and re.match(HORIZONTAL_RULE_PATTERN, preceding[-1].strip()):
                return True
        return False

    @staticmethod
    def _has_user_input(lines: list[str]) -> bool:
        """Return True if at least one user-input line (`> text`) is visible."""
        for line in lines:
            if re.match(USER_INPUT_PATTERN, line):
                return True
        return False

    @staticmethod
    def _is_error(lines: list[str]) -> bool:
        """Return True if the output contains an explicit error/crash indicator."""
        combined = "\n".join(lines[-50:])
        for pattern in ERROR_PATTERNS:
            if re.search(pattern, combined, re.IGNORECASE | re.MULTILINE):
                return True
        return False

    def get_status(self, buffer: str) -> TerminalStatus:
        """Detect Devin CLI state from terminal output.

        Args:
            buffer: Raw terminal output buffer from pipe-pane

        Returns:
            TerminalStatus based on pattern matching
        """
        if not buffer:
            return TerminalStatus.UNKNOWN

        # Strip ANSI codes for clean matching
        clean_output = self._clean(buffer)

        if not clean_output.strip():
            return TerminalStatus.UNKNOWN

        lines = clean_output.splitlines()

        # 1. Processing spinner patterns take priority
        if self._is_processing(lines):
            return TerminalStatus.PROCESSING

        # 2. Check for the # prompt using horizontal-rule-aware detector
        has_prompt = self._has_input_prompt(lines)

        if has_prompt:
            # Check for user input to distinguish IDLE from COMPLETED.
            # If a task was dispatched and the user-input line has scrolled out
            # of the buffer, the visible prompt means completion.
            if self._has_user_input(lines) or self._task_dispatched:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # 3. Initial Devin CLI welcome screen (before first # prompt)
        # Look for "Ask Devin to build features", "I'm ready to help", or "SWE-1.6"
        if (
            "Ask Devin to build features" in clean_output
            or "I'm ready to help" in clean_output
            or "SWE-1.6" in clean_output
        ):
            return TerminalStatus.IDLE

        # 4. Explicit Devin CLI / runtime crashes are reported as ERROR.
        if self._is_error(lines):
            return TerminalStatus.ERROR

        # 5. Ambiguous output (no prompt, no processing, no error): keep polling.
        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN

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
            raise ValueError("No user input found")

        # Collect lines between the last user input and the next horizontal rule.
        # NOTE: do NOT break on the `#` pattern here — it would incorrectly truncate
        # responses that begin with a Markdown heading (e.g. "# Overview").
        # The horizontal rule (always present before the `#` prompt) is the safe
        # terminator. The status bar is an additional fallback.
        response_lines = []
        for line in lines[last_user_idx + 1 :]:
            if re.match(HORIZONTAL_RULE_PATTERN, line.strip()):
                break
            if re.search(STATUS_BAR_PATTERN, line):
                break
            # Preserve all lines including empty ones for paragraph formatting
            response_lines.append(line)

        if not response_lines:
            raise ValueError("No response found")

        return "\n".join(response_lines).strip()

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        """Clean up temp files."""
        self._cleanup_temp_files()
