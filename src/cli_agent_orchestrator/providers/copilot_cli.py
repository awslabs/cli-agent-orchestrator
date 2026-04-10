"""Copilot CLI provider implementation."""

from __future__ import annotations

import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from libtmux.exc import LibTmuxException

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b\[[0-?]*[ -/]*[@-~]"
OSC_PATTERN = r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
CONTROL_CHARS_PATTERN = r"[\x00-\x08\x0b-\x1f\x7f]"
IDLE_PROMPT_PATTERN_LOG = r"Type @ to mention files"
USER_PROMPT_LINE_PATTERN = r"^\s*[❯›>]\s+.+$"
IDLE_PROMPT_LINE_PATTERN = r"^\s*(?:[❯›>]|copilot>)(?:\s+.*)?$"
ASSISTANT_PREFIX_PATTERN = r"^assistant\s*:|^[●◐◑◒◓◉].+"
WAITING_PROMPT_PATTERN = (
    r"do you trust all the actions in this folder|"
    r"do you trust the files in this folder|"
    r"do you trust the contents of this directory|"
    r"confirm folder trust|press enter to continue|\[\s*y\s*/\s*n\s*]"
)
ERROR_PATTERN = r"(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"
PROMPT_HELPER_CONTINUATION_PATTERN = r"^(?:shortcuts|for shortcuts)$"
PROCESSING_LINE_PATTERN = r"^(?:[●◐◑◒◓◉◎∙]\s*)?.*\besc to cancel\b.*$"


class CopilotCliProvider(BaseProvider):
    """Provider for GitHub Copilot CLI."""

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
        self._copilot_help_text_cache: Optional[str] = None

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
        try:
            raw = tmux_client.get_history(
                self.session_name, self.window_name, tail_lines=tail_lines
            )
        except (
            ValueError,
            RuntimeError,
            OSError,
            IndexError,
            LibTmuxException,
            subprocess.SubprocessError,
        ) as exc:
            logger.warning(
                "history read failed for %s:%s: %s", self.session_name, self.window_name, exc
            )
            return ""
        return self._clean(raw)

    def _supports_flag(self, flag: str) -> bool:
        if self._copilot_help_text_cache is None:
            try:
                result = subprocess.run(
                    ["copilot", "--help"],
                    check=False,
                    capture_output=True,
                    text=True,
                )
                self._copilot_help_text_cache = result.stdout or ""
            except (OSError, subprocess.SubprocessError):
                self._copilot_help_text_cache = ""
        return flag in self._copilot_help_text_cache

    def _wait_for_shell_ready(self, timeout: float = 30.0, polling_interval: float = 0.5) -> bool:
        """Wait for a stable non-empty shell screen using provider-safe history reads."""
        start_time = time.time()
        previous_output: Optional[str] = None
        stable_reads = 0

        while time.time() - start_time < timeout:
            output = self._history(tail_lines=120)
            if output and output.strip():
                if previous_output is not None and output == previous_output:
                    stable_reads += 1
                    if stable_reads >= 2:
                        return True
                else:
                    stable_reads = 0

            previous_output = output
            time.sleep(polling_interval)

        return False

    def _command(self) -> str:
        config_dir = Path.home() / ".copilot"

        command_parts = ["copilot", "--allow-all"]

        if self._agent_profile:
            command_parts.extend(["--agent", self._agent_profile])

        command_parts.extend(["--config-dir", str(config_dir)])
        try:
            pane_working_dir = tmux_client.get_pane_working_directory(
                self.session_name,
                self.window_name,
            )
        except (LibTmuxException, OSError, RuntimeError, ValueError):
            pane_working_dir = None

        if not isinstance(pane_working_dir, str) or not pane_working_dir.strip():
            pane_working_dir = None

        command_parts.extend(["--add-dir", pane_working_dir or os.getcwd()])

        if self._supports_flag("--additional-mcp-config"):
            runtime_mcp_config = self._build_runtime_mcp_config()
            if runtime_mcp_config:
                command_parts.extend(["--additional-mcp-config", runtime_mcp_config])

        # Apply tool restrictions via --deny-tool flags.
        # --deny-tool takes precedence over --allow-all.
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("copilot_cli", self._allowed_tools)
            for tool in disallowed:
                command_parts.extend(["--deny-tool", tool])

        command_parts.append("--autopilot")

        return shlex.join(command_parts)

    def _build_runtime_mcp_config(self) -> str:
        merged_servers: dict = {}
        mcp_args: list[str]
        venv_script = Path(sys.executable).with_name("cao-mcp-server")
        found_script = shutil.which("cao-mcp-server")
        if venv_script.exists():
            mcp_command = str(venv_script)
            mcp_args = []
        elif found_script:
            mcp_command = found_script
            mcp_args = []
        else:
            mcp_command = sys.executable
            mcp_args = ["-m", "cli_agent_orchestrator.mcp_server.server"]

        merged_servers["cao-mcp-server"] = {
            "command": mcp_command,
            "args": mcp_args,
            "disabled": False,
            "env": {"CAO_TERMINAL_ID": self.terminal_id},
        }
        return json.dumps({"mcpServers": merged_servers}, ensure_ascii=False)

    def _send_enter(self) -> None:
        tmux_client.send_special_key(self.session_name, self.window_name, "Enter")

    def _send_key(self, key: str) -> None:
        tmux_client.send_special_key(self.session_name, self.window_name, key)

    def _accept_trust_prompts(self, timeout: float = 30.0) -> None:
        start = time.time()
        while time.time() - start < timeout:
            raw_content = self._history(tail_lines=120)
            content = raw_content.lower()

            if (
                "confirm folder trust" in content
                and re.search(r"\b1\.\s*yes\b", content)
                and re.search(r"\b2\.\s*yes,\s*and remember", content)
            ):
                # The first option is pre-selected; Enter is the most reliable
                # accept action across Copilot builds.
                self._send_enter()
                time.sleep(1)
                continue

            if (
                "do you trust the files in this folder" in content
                or "do you trust the contents of this directory" in content
            ) and re.search(r"\b1\.\s*yes\b", content):
                # Option 1 is selected by default in the trust dialog; Enter is
                # the most reliable way to accept across Copilot builds.
                self._send_enter()
                time.sleep(1)
                continue

            if "do you trust all the actions in this folder" in content:
                self._send_key("y")
                self._send_enter()
                time.sleep(1)
                continue

            if re.search(r"\[\s*y\s*/\s*n\s*]", content):
                self._send_key("y")
                self._send_enter()
                time.sleep(1)
                continue

            if "confirm folder trust" in content or "press enter to continue" in content:
                self._send_enter()
                time.sleep(1)
                continue

            if re.search(WAITING_PROMPT_PATTERN, content, re.IGNORECASE):
                # Generic waiting prompt fallback: confirm with Enter.
                self._send_enter()
                time.sleep(1)
                continue

            if self._has_idle_prompt_near_end(raw_content.splitlines()):
                return
            time.sleep(1)

        logger.warning(
            "Trust prompt handler timed out for %s:%s",
            self.session_name,
            self.window_name,
        )

    def initialize(self) -> bool:
        try:
            shell_ready = wait_for_shell(
                tmux_client, self.session_name, self.window_name, timeout=30.0
            )
        except Exception as exc:
            logger.warning(
                "wait_for_shell failed for %s:%s, retrying with provider history: %s",
                self.session_name,
                self.window_name,
                exc,
            )
            shell_ready = self._wait_for_shell_ready(timeout=30.0)

        if not shell_ready:
            raise TimeoutError("Shell initialization timed out after 30 seconds")

        tmux_client.send_keys(self.session_name, self.window_name, self._command())

        deadline = time.time() + 60.0
        self._accept_trust_prompts(timeout=10.0)
        while time.time() < deadline:
            status = self.get_status()
            if status == TerminalStatus.WAITING_USER_ANSWER:
                self._accept_trust_prompts(timeout=5.0)
                time.sleep(1.0)
                continue
            if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                # Return on the first ready state like the other providers.
                # The surrounding init loop remains provider-specific because
                # Copilot can surface late trust prompts that need explicit
                # handling before the terminal is actually usable.
                self._initialized = True
                return True
            time.sleep(1.0)

        raise TimeoutError("Copilot initialization timed out after 60 seconds")

    @staticmethod
    def _find_last_user_line(lines: list[str]) -> int:
        last_user = -1
        for idx, line in enumerate(lines):
            if not re.match(USER_PROMPT_LINE_PATTERN, line):
                continue
            stripped = line.strip()
            if stripped in {"❯", ">", "›"}:
                continue
            if "type @ to mention files" in stripped.lower():
                continue
            last_user = idx
        return last_user

    @staticmethod
    def _is_footer_line(line: str) -> bool:
        stripped = line.strip().lower()
        if not stripped:
            return True
        if re.match(r"^[─-]{8,}$", stripped):
            return True
        if re.search(r"\(\d+x\)", stripped):
            return True
        if "shift+tab switch mode" in stripped:
            return True
        if "type @ to mention files" in stripped:
            return True
        # Copilot may wrap the helper hint to a continuation line:
        # "❯ ... or ? for" + "shortcuts"
        if re.match(PROMPT_HELPER_CONTINUATION_PATTERN, stripped):
            return True
        if stripped.startswith("╭") or stripped.startswith("╰") or stripped.startswith("│"):
            return True
        return False

    @staticmethod
    def _is_processing_line(line: str) -> bool:
        return bool(re.match(PROCESSING_LINE_PATTERN, line.strip(), re.IGNORECASE))

    @classmethod
    def _has_idle_prompt_near_end(cls, lines: list[str]) -> bool:
        if not lines:
            return False

        # Strip trailing empty lines — TUI providers (like Copilot) render in
        # a fixed viewport at the top of the pane, leaving the bottom blank.
        stripped = list(lines)
        while stripped and not stripped[-1].strip():
            stripped.pop()
        if not stripped:
            return False

        tail = stripped[-25:]
        last_prompt_idx = -1
        for idx, line in enumerate(tail):
            if re.match(IDLE_PROMPT_LINE_PATTERN, line):
                last_prompt_idx = idx
        if last_prompt_idx < 0:
            return False

        for line in tail[last_prompt_idx + 1 :]:
            if cls._is_footer_line(line):
                continue
            if line.strip():
                return False

        return True

    @classmethod
    def _normalize_post_user_lines(cls, lines: list[str]) -> list[str]:
        normalized = [
            line
            for line in lines
            if line.strip()
            and not cls._is_footer_line(line)
            and not re.match(IDLE_PROMPT_LINE_PATTERN, line)
        ]

        while (
            normalized
            and normalized[0].startswith("  ")
            and not cls._is_processing_line(normalized[0])
        ):
            normalized.pop(0)

        return normalized

    @classmethod
    def _trim_tail_prompts(cls, lines: list[str]) -> list[str]:
        trimmed = list(lines)
        while trimmed:
            tail = trimmed[-1].strip()
            if not tail:
                trimmed.pop()
                continue
            if cls._is_footer_line(tail):
                trimmed.pop()
                continue
            if re.match(r"^[❯›>](?:\s+.*)?$", tail):
                trimmed.pop()
                continue
            break
        return trimmed

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        effective_tail_lines = tail_lines if tail_lines is not None else 220
        output = self._history(tail_lines=effective_tail_lines)
        if not output.strip():
            return TerminalStatus.PROCESSING

        lines = output.splitlines()
        has_idle_prompt_at_end = self._has_idle_prompt_near_end(lines)
        tail_output = "\n".join(lines[-40:])

        waiting_matches = list(re.finditer(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE))
        idle_matches = list(
            re.finditer(IDLE_PROMPT_LINE_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE)
        )
        waiting_now = bool(waiting_matches)
        if waiting_matches and idle_matches:
            waiting_now = waiting_matches[-1].start() > idle_matches[-1].start()
        if waiting_now and not has_idle_prompt_at_end:
            return TerminalStatus.WAITING_USER_ANSWER

        last_user = self._find_last_user_line(lines)

        if not has_idle_prompt_at_end:
            if last_user >= 0:
                post = "\n".join(lines[last_user + 1 :])
                if re.search(ERROR_PATTERN, post, re.IGNORECASE):
                    return TerminalStatus.ERROR
            return TerminalStatus.PROCESSING

        if last_user < 0:
            return TerminalStatus.IDLE

        post_lines = self._trim_tail_prompts(
            self._normalize_post_user_lines(lines[last_user + 1 :])
        )
        if not post_lines:
            return TerminalStatus.IDLE

        if all(self._is_processing_line(line) for line in post_lines):
            return TerminalStatus.PROCESSING

        if self._is_processing_line(post_lines[-1]):
            return TerminalStatus.PROCESSING

        post_text = "\n".join(post_lines)
        if re.search(ERROR_PATTERN, post_text, re.IGNORECASE):
            if re.search(ASSISTANT_PREFIX_PATTERN, post_text, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.COMPLETED
            return TerminalStatus.ERROR

        return TerminalStatus.COMPLETED

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean_output = self._clean(script_output)
        lines = clean_output.splitlines()
        last_user = self._find_last_user_line(lines)

        if last_user >= 0:
            post_lines = self._trim_tail_prompts(
                self._normalize_post_user_lines(lines[last_user + 1 :])
            )
            while post_lines and self._is_processing_line(post_lines[-1]):
                post_lines.pop()
            message = "\n".join(post_lines).strip()
            if message:
                return message

        matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        if matches:
            start_pos = matches[-1].end()
            tail = clean_output[start_pos:].strip()
            if tail:
                return tail

        raise ValueError("No provider response content found in terminal output")

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        self._initialized = False
