"""Codex CLI provider implementation."""

import logging
import os
import re
import shlex
import subprocess
from pathlib import Path
from typing import Dict, Optional

from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

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
        self._mcp_servers: Dict[str, Dict] = {}
        self._env_exports: Dict[str, str] = {}
        self._profile = None

        if self._agent_profile:
            try:
                self._profile = load_agent_profile(self._agent_profile)
                if self._profile.mcpServers:
                    self._mcp_servers = self._profile.mcpServers
                    for server in self._mcp_servers.values():
                        for key, value in (server.get("env") or {}).items():
                            self._env_exports[key] = value
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Failed to load agent profile '%s': %s", self._agent_profile, exc)

    def initialize(self) -> bool:
        """Launch Codex CLI inside the tmux pane and wait until it's idle."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Ensure MCP configuration is registered ahead of the interactive session.
        if self._mcp_servers:
            self._ensure_mcp_servers_registered()

        # Export server-specific environment variables so Codex inherits them.
        env_exports = dict(self._env_exports)
        env_exports.setdefault("CAO_MCP_HOME", str(Path.home() / ".cache" / "codex-cao"))
        for key, value in env_exports.items():
            quoted = shlex.quote(str(value))
            tmux_client.send_keys(
                self.session_name,
                self.window_name,
                f"export {key}={quoted}"
            )

        command = "codex"
        # Fire up the interactive Codex TUI inside the tmux pane.
        # TODO: map agent_profile to a named Codex workspace once the CLI supports it.
        tmux_client.send_keys(self.session_name, self.window_name, command)

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=45.0):
            raise TimeoutError("Codex CLI initialization timed out after 45 seconds")

        self._initialized = True
        return True

    def _ensure_mcp_servers_registered(self) -> None:
        """Register MCP servers with Codex using the CLI's `mcp add` command."""

        base_env = os.environ.copy()
        base_env.update(self._env_exports)
        base_env.setdefault("CAO_MCP_HOME", str(Path.home() / ".cache" / "codex-cao"))
        Path(base_env["CAO_MCP_HOME"]).mkdir(parents=True, exist_ok=True)

        for name, server in self._mcp_servers.items():
            command = server.get("command")
            if not command:
                logger.warning("Skipping MCP server '%s': missing command", name)
                continue

            args = server.get("args") or []
            server_env = server.get("env") or {}

            cmd = ["codex", "mcp", "add"]
            for key, value in server_env.items():
                cmd.extend(["--env", f"{key}={value}"])
            cmd.extend([command, *args, name])

            try:
                result = subprocess.run(
                    cmd,
                    check=False,
                    capture_output=True,
                    text=True,
                    env=base_env,
                )
                if result.returncode != 0 and "already exists" not in (result.stderr or ""):
                    logger.warning(
                        "Failed to register MCP server '%s': %s", name, result.stderr.strip()
                    )
            except Exception as exc:  # pragma: no cover - defensive
                logger.error("Error registering MCP server '%s': %s", name, exc)

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
