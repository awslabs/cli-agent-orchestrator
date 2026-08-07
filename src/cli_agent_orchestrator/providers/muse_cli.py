"""Muse Code CLI provider — Meta's native terminal agent on Muse Spark 1.2.

Muse Code (https://developer.meta.com/ai/lp/start-building/) is Meta's
terminal coding agent. CAO launches it as a native-CLI worker (the same shape as
``kimi_cli``) so the Muse Spark 1.2 model runs through its own harness rather
than a Claude Code gateway: the Meta Model API rejects several Anthropic-API
fields Claude Code 2.x always sends (``context_management``, ``output_config``),
so a claude_code-on-Muse route cannot work directly.

TUI characteristics (Muse Code 0.1.x):
  - Command: ``muse``
  - Idle prompt: a ``⟩`` input line at the bottom when ready for input
  - Processing: no idle ``⟩`` prompt (turn-in-flight spinner)
  - Response format: assistant reply lines prefixed with ``◆``
  - Auto-approve: ``--yolo`` disables approval and sandbox for a workspace run
  - Model selection: ``--model <id>``; ``--reasoning-effort`` maps CAO effort
  - Exit: ``/exit``

Status detection: the ``⟩`` prompt stays rendered through a whole turn, so the
in-flight signal is the spinner text ("esc to interrupt"); otherwise the bare
``⟩`` prompt means ready (IDLE before a task, COMPLETED after one).

Known first-version limitations (iterate in a live CAO session):
  * The profile's system-prompt body is not yet injected into the worker; the
    worker relies on CAO's initial message + policy guidelines. Muse Code does
    not expose a kimi-style ``--agent-file``; wiring ``--agents <JSON>`` or a
    workspace skill is the follow-up.
  * ``--yolo`` is used for every posture; the reviewer's declared read-only
    tools are enforced by prompt only (no SECURITY_PROMPT injection yet), so
    keep review tasks read-only by instruction.
"""

from __future__ import annotations

import logging
import re
import shlex
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Muse Code 0.1.x TUI markers (empirically verified: the ⟩ prompt stays
# rendered through a whole turn, the spinner alternates ◇/◆ and reads
# "Thinking (Ns · esc to interrupt)", and reply continuations are 2-space
# indented bare lines under a single ◆ lead).
IDLE_PROMPT_PATTERN = r"⟩\s*$"  # input prompt line (rendered all turn)
IDLE_PROMPT_PATTERN_LOG = r"⟩"
SPINNER_PATTERN = r"esc to interrupt"  # in-flight marker (◇/◆ Thinking…)
ERROR_PATTERN = r"(crash report written|Traceback \(most recent call last\))"
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*[A-Za-z]"
# The lead of a real reply bullet: "◆ text", but never a spinner line.
REPLY_LEAD_PATTERN = r"^◆[ \t]+(.+)$"


class MuseCliProvider(BaseProvider):
    """Native Muse Code worker provider (Muse Spark 1.2 standard or Contributor).

    The model is resolved in this order: ``expected_model`` (CAO's managed-launch
    override, set by ``conduct spawn`` from the route model, so a single profile
    can target either ``muse-spark-1.2`` or ``muse-spark-1.2-contributor`` at
    launch), then the profile's ``model`` field, then Muse Code's default.
    """

    BINARY_NAME = "muse"

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        skill_prompt: Optional[str] = None,
        expected_model: Optional[str] = None,
        expected_effort: Optional[str] = None,
    ) -> None:
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._expected_model = expected_model
        self._expected_effort = expected_effort
        self._initialized = False
        self._has_received_input = False
        # Shell process running in the pane before muse launches; used to detect
        # a crashed/exited muse pane that has reverted to a bare shell.
        self.shell_baseline: Optional[str] = None

    @property
    def paste_enter_count(self) -> int:
        return 1

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._has_received_input = True

    def _resolve_model(self) -> Optional[str]:
        if self._expected_model:
            return self._expected_model
        if self._agent_profile:
            try:
                return load_agent_profile(self._agent_profile).model
            except Exception:  # pragma: no cover - profile resolution is best-effort
                return None
        return None

    def _build_command(self) -> str:
        """Build the Muse Code launch command (escaped for tmux send_keys)."""
        parts = [self.BINARY_NAME, "--yolo"]
        model = self._resolve_model()
        if model:
            parts.extend(["--model", model])
        if self._expected_effort:
            parts.extend(["--reasoning-effort", self._expected_effort])
        return shlex.join(parts)

    async def initialize(self) -> bool:
        """Launch ``muse --yolo`` inside the tmux window (cwd = the worktree)."""
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Capture the shell process before launching muse — used later to detect
        # when muse has exited and the pane is back to a bare shell, so a queued
        # message can never be typed into zsh as arbitrary commands.
        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        # Arm the StatusMonitor stickiness gate before the launch keystrokes.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)

        command = self._build_command()
        get_backend().send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=45.0
        ):
            raise TimeoutError("Muse Code initialization timed out after 45 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Detect Muse Code's state from the tmux capture buffer.

        Strategy (mirrors kimi_cli): bottom-anchored. The bare ``⟩`` input line
        means ready; once any ``◆`` response has appeared the turn is COMPLETED
        (latched via ``_has_received_input`` for long responses that scroll the
        marker out of the rolling buffer); no idle prompt means a turn is in
        flight; crash/error markers are ERROR.
        """
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        clean = strip_terminal_escapes(output)

        # A pane that reverted to the shell means muse crashed/exited. Returning
        # ERROR prevents the inbox service from typing a queued message into the
        # shell (which would execute it as arbitrary commands).
        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.ERROR

        if re.search(ERROR_PATTERN, clean):
            return TerminalStatus.ERROR

        # The ⟩ prompt is rendered through the whole turn, so it cannot
        # distinguish idle from in-flight. The spinner text ("esc to
        # interrupt") is the reliable in-flight signal; check it first.
        if re.search(SPINNER_PATTERN, clean):
            return TerminalStatus.PROCESSING

        if re.search(IDLE_PROMPT_PATTERN, clean, re.MULTILINE):
            if self._has_received_input:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Return Muse Code's last reply, skipping the spinner and spanning
        the 2-space-indented continuation lines under the ◆ lead."""
        clean = strip_terminal_escapes(script_output)
        lines = clean.splitlines()
        last_idx = -1
        for i, line in enumerate(lines):
            if re.match(REPLY_LEAD_PATTERN, line) and "esc to interrupt" not in line:
                last_idx = i
        if last_idx == -1:
            raise ValueError("No Muse Code response found in script output")
        parts = [re.sub(r"^◆[ \t]+", "", lines[last_idx]).strip()]
        for line in lines[last_idx + 1 :]:
            if re.match(r"^[ \t]{2}\S", line) and "Voice input" not in line:
                parts.append(line.strip())
            else:
                break
        return "\n".join(parts).strip()

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        return None
