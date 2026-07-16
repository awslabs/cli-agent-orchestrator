"""Hermes Agent provider implementation."""

import logging
import os
import re
import shlex
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

ANSI_CODE_PATTERN = r"\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))"
IDLE_PROMPT_PATTERN = os.environ.get(
    "CAO_HERMES_IDLE_PROMPT_REGEX",
    r"^(?!.*(?:msg=interrupt|Ctrl\+C cancel|/queue|/bg|/steer|Tip:|│|─|╭|╰)).{0,80}(?:❯|✦)\s*$",
)
IDLE_PROMPT_PATTERN_LOG = os.environ.get("CAO_HERMES_IDLE_LOG_REGEX", r"⏲")
PROCESSING_PATTERN = os.environ.get(
    "CAO_HERMES_PROCESSING_REGEX",
    r"(?:msg=interrupt|musing\.\.\.|Initializing agent|Ctrl\+C cancel|⏱\s*\d+s)",
)
ACTIVE_PROCESSING_PATTERN = r"(?:msg=interrupt|musing\.\.\.|Ctrl\+C cancel|⏱\s*\d+s)"
WAITING_PROMPT_PATTERN = (
    r"(?:Approve|Allow|Proceed|Confirm)[^\n]*(?:y/n|yes/no|\[y/N\])"
    r"|(?:DANGEROUS COMMAND|危险命令)"
    r"|(?:\[o\](?:nce|仅此一次).*\[s\](?:ession|本次会话).*\[d\](?:eny|拒绝))"
    r"|(?:(?:Choice|选择)\s+\[o/s(?:/a)?/D\]:)"
    r"|(?:Allow once.*Allow always.*Reject)"
    r"|(?:Hermes needs your input|Other \(type (?:your answer|below)\))"
    r"|(?:↑/↓\s*(?:to )?select.*Enter (?:to )?confirm)"
    r"|(?:type your answer and press Enter)"
)
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|hermes .*failed:)"
USER_PREFIX_PATTERN = os.environ.get("CAO_HERMES_USER_PREFIX_REGEX", r"^●\s+")
ASSISTANT_HEADER_PATTERN = os.environ.get(
    "CAO_HERMES_ASSISTANT_HEADER_REGEX",
    r"─\s+.*(?:Hermes|Agent|Assistant).*\s+─",
)
STATUS_IDLE_TIMER_PATTERN = r"⏲\s*([^\s│]+)"
SEPARATOR_PATTERN = r"^[\s─━═-]{10,}$|^[\s─━═-]+\s+.*\s+[\s─━═-]+$"
STATUS_LINE_PATTERN = r"^.*(?:YOLO|ctx|⏲|⏱|msg=interrupt|Ctrl\+C cancel).*$"
MAX_STABLE_IDLE_TIMER_POLLS = int(os.environ.get("CAO_HERMES_MAX_STABLE_IDLE_POLLS", "8"))


class ProviderError(Exception):
    """Exception raised for Hermes provider-specific errors."""

    pass


def _strip_ansi(text: str) -> str:
    return re.sub(ANSI_CODE_PATTERN, "", text)


def _is_idle_line(line: str) -> bool:
    return re.search(IDLE_PROMPT_PATTERN, line) is not None


def _is_chrome_line(line: str) -> bool:
    stripped = line.strip()
    return (
        not stripped
        or _is_idle_line(stripped)
        or re.match(SEPARATOR_PATTERN, stripped) is not None
        or re.match(STATUS_LINE_PATTERN, stripped) is not None
        or re.search(PROCESSING_PATTERN, stripped, re.IGNORECASE) is not None
        or re.search(ASSISTANT_HEADER_PATTERN, stripped) is not None
        or re.search(USER_PREFIX_PATTERN, stripped) is not None
    )


def _last_idle_timer(text: str) -> Optional[str]:
    matches = list(re.finditer(STATUS_IDLE_TIMER_PATTERN, text))
    return matches[-1].group(1) if matches else None


def _has_waiting_prompt(text: str) -> bool:
    """Detect active Hermes approval or clarify prompts near the prompt area."""
    if re.search(WAITING_PROMPT_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        clarify_context = re.search(
            r"(?:Hermes needs your input|Other \(type (?:your answer|below)\)|"
            r"↑/↓\s*(?:to )?select.*Enter (?:to )?confirm|"
            r"type your answer and press Enter)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        approval_context = re.search(
            r"(?:Choice|选择)\s+\[o/s(?:/a)?/D\]:|"
            r"\[o\](?:nce|仅此一次).*\[s\](?:ession|本次会话).*\[d\](?:eny|拒绝)|"
            r"Allow once.*Allow always.*Reject|"
            r"(?:Approve|Allow|Proceed|Confirm)[^\n]*(?:y/n|yes/no|\[y/N\])|"
            r"(?:DANGEROUS COMMAND|危险命令)",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        return bool(clarify_context or approval_context)
    return False


class HermesProvider(BaseProvider):
    """Provider for a CAO-managed Hermes Agent profile.

    The CAO agent profile may set ``hermesProfile`` to a Hermes profile wrapper
    command. When it is omitted, CAO launches the default ``hermes`` command.
    CAO intentionally does not hard-code a concrete Hermes profile name in this
    provider.
    """

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._last_idle_timer: Optional[str] = None
        self._stable_idle_timer_count = 0

    @property
    def paste_enter_count(self) -> int:
        """Hermes submits bracketed paste with a single Enter."""
        return 1

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Hermes approval and clarify pickers consume pasted text as answers."""
        return True

    def _build_hermes_command(self) -> str:
        """Build the Hermes launch command from the CAO agent profile."""
        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        hermes_profile = profile.hermesProfile if profile and profile.hermesProfile else "hermes"

        command_parts = [
            hermes_profile,
            "chat",
            "--yolo",
            "--accept-hooks",
            "--source",
            "cao",
        ]

        if profile and profile.model:
            command_parts.extend(["--model", profile.model])

        if self._skill_prompt:
            logger.warning(
                "Hermes provider does not inject CAO runtime skill catalogs; "
                "configure skills and MCP servers inside the selected Hermes profile"
            )

        if self._allowed_tools and "*" not in self._allowed_tools:
            logger.warning(
                "Hermes provider has no CAO-native tool restriction flag; "
                "restrictions rely on the selected Hermes profile configuration"
            )

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Initialize Hermes by starting the configured profile chat REPL."""
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        command = self._build_hermes_command()
        tmux_client.send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("Hermes initialization timed out after 120 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Get Hermes status by analyzing the terminal output buffer.

        Args:
            output: Terminal output buffer (up to ~8KB rolling buffer) supplied
                by the StatusMonitor via the FIFO reader pipeline.
        """
        # Native status (herdr): trust the backend's agent state when available.
        # Must precede the empty-buffer -> ERROR default below: on herdr the
        # buffer is always empty, so without this every status would be ERROR.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.ERROR

        clean_output = _strip_ansi(output)
        lines = clean_output.splitlines()
        tail_lines_text = lines[-30:]
        tail_output = "\n".join(tail_lines_text)
        bottom_lines_text = [line for line in tail_lines_text if line.strip()][-8:]
        bottom_output = "\n".join(bottom_lines_text)
        has_idle_prompt = any(_is_idle_line(line.strip()) for line in bottom_lines_text)
        has_stable_idle_timer = self._has_stable_idle_timer(tail_output)
        has_user = bool(re.search(USER_PREFIX_PATTERN, clean_output, re.MULTILINE))
        has_response = bool(re.search(ASSISTANT_HEADER_PATTERN, clean_output))
        if not has_response:
            has_response = self._has_extractable_response(clean_output)

        if _has_waiting_prompt(bottom_output):
            return TerminalStatus.WAITING_USER_ANSWER

        if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
            return TerminalStatus.ERROR

        if re.search(ACTIVE_PROCESSING_PATTERN, bottom_output, re.IGNORECASE | re.MULTILINE):
            return TerminalStatus.PROCESSING

        if has_stable_idle_timer or has_idle_prompt:
            if has_user and has_response:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        if re.search(PROCESSING_PATTERN, bottom_output, re.IGNORECASE | re.MULTILINE):
            return TerminalStatus.PROCESSING

        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        """Return Hermes idle prompt pattern for log file monitoring."""
        return IDLE_PROMPT_PATTERN_LOG

    def _has_stable_idle_timer(self, tail_output: str) -> bool:
        """Detect settled Hermes output using the status bar idle timer."""
        current_timer = _last_idle_timer(tail_output)
        if current_timer is None:
            self._last_idle_timer = None
            self._stable_idle_timer_count = 0
            return False

        if current_timer == self._last_idle_timer:
            self._stable_idle_timer_count += 1
        else:
            self._last_idle_timer = current_timer
            self._stable_idle_timer_count = 1

        return 2 <= self._stable_idle_timer_count <= MAX_STABLE_IDLE_TIMER_POLLS

    def _has_extractable_response(self, clean_output: str) -> bool:
        try:
            return bool(self._extract_response(clean_output, require_header=False))
        except ValueError:
            return False

    def _extract_response(self, clean_output: str, require_header: bool = True) -> str:
        matches = list(re.finditer(ASSISTANT_HEADER_PATTERN, clean_output))
        if matches:
            start = clean_output.find("\n", matches[-1].end())
            if start == -1:
                raise ValueError("No Hermes response content found")
            start += 1
            search_region = clean_output[start:]
        elif require_header:
            raise ValueError("No Hermes response found - no assistant header detected")
        else:
            user_matches = list(re.finditer(USER_PREFIX_PATTERN, clean_output, re.MULTILINE))
            if not user_matches:
                raise ValueError("No Hermes response found - no user message detected")
            user_line_end = clean_output.find("\n", user_matches[-1].end())
            if user_line_end == -1:
                user_line_end = user_matches[-1].end()
            search_region = clean_output[user_line_end + 1 :]

        end_match = re.search(IDLE_PROMPT_PATTERN, search_region, re.MULTILINE)
        candidate_text = search_region[: end_match.start()] if end_match else search_region
        candidate_lines = candidate_text.splitlines()

        response_lines: list[str] = []
        for raw_line in reversed(candidate_lines):
            line = raw_line.rstrip()
            stripped = line.strip()
            if _is_chrome_line(stripped):
                if response_lines and stripped:
                    break
                continue
            response_lines.append(stripped)

        response = "\n".join(reversed(response_lines)).strip()
        if not response:
            raise ValueError("Empty Hermes response - no content found")
        return response

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last Hermes response from terminal output."""
        clean_output = _strip_ansi(script_output)
        return self._extract_response(clean_output, require_header=False)

    def exit_cli(self) -> str:
        """Get the command to exit Hermes."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Hermes provider state."""
        self._initialized = False
