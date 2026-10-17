"""Copilot CLI provider implementation."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from libtmux.exc import LibTmuxException

from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp as _with_plugin_mcp
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command
from cli_agent_orchestrator.utils.terminal import wait_for_shell

logger = logging.getLogger(__name__)

#: CAO transport name -> the ``type`` value Copilot CLI's MCP config expects.
#:
#: Copilot's documented vocabulary is ``local``/``stdio`` for a command-based
#: server, ``http`` for Streamable HTTP, and ``sse`` for the legacy transport
#: (https://docs.github.com/en/copilot/how-tos/copilot-cli/customize-copilot/add-mcp-servers).
#: CAO and the Agent Plugins ``mcp.json`` schema use the MCP specification's
#: ``streamable-http``, which Copilot has no case for, so the value has to be
#: translated rather than passed through — the same defect class as
#: ``GROK_URL_TRANSPORTS`` (review 3) and ``KIMI_TRANSPORTS`` (review 5222539218
#: item 5), both on #584. Found by self-audit; Copilot was the site both of those
#: rounds missed.
#:
#: ``stdio`` maps to itself deliberately. The vendor documents ``Local`` and
#: ``STDIO`` as working the same way and recommends ``stdio`` for configurations
#: shared with VS Code, the cloud agent, and other MCP clients, so rewriting it to
#: ``local`` would trade a portable spelling for a Copilot-only one and fix
#: nothing. Only ``streamable-http`` actually changes here.
#:
#: Only these spellings translate. An absent or unrecognised ``type`` is left
#: alone: ``_map_entry`` always emits ``type`` for a plugin server, so a type-less
#: entry came from a hand-written profile — or is CAO's own in-session server,
#: which carries no ``type`` and works because Copilot infers a command-based
#: server from ``command``. Inventing one would be a behaviour change beyond this
#: finding, which is the same boundary ``KIMI_TRANSPORTS`` draws.
COPILOT_TRANSPORTS: dict[str, str] = {
    "stdio": "stdio",
    "streamable-http": "http",
    "http": "http",
    "sse": "sse",
}

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
# Copilot v1.0.31+ renders a status bar below the ❯ prompt:
# " autopilot · / commands    Claude Sonnet 4.6 · (0%)"
# This must be treated as a footer line so idle detection works correctly.
COPILOT_STATUS_BAR_PATTERN = r"^\s*(?:autopilot|plan|interactive)\s*[·•]"
# Copilot v1.0.31+ cwd breadcrumb: " ~/path [⎇ branch*%]"
# Older versions appended " model (0x)" which was caught by \(\d+x\); the
# token/model info moved to the status bar in v1.0.31, leaving only the path.
# Path can be tilde-prefixed (home) or absolute (e.g. /tmp/...), so allow both.
COPILOT_CWD_BREADCRUMB_PATTERN = r"^\s+(?:~|/)[^\[]*\["
COPILOT_HINT_BAR_PATTERN = (
    r"^(?:v\d+\.\d+\.\d+(?:-[0-9a-z.-]+)? downloaded\s*[·•]\s*"
    r"next launch or /restart\s*[·•]\s*)?"
    r"←.*[·•]\s*/ commands\b"
)
COPILOT_AGENT_MODEL_BAR_PATTERN = r"^(?:[\w.-]+\s*[·•]\s*)?github copilot\s*[·•]"
COPILOT_BUSY_ROW_PATTERN = r"\besc (?:to cancel|interrupt)\b"
COPILOT_COLUMN_GAP_PATTERN = r"\S\s{3,}\S"
COPILOT_MODEL_VERSION_PATTERN = r"\d"
PROCESSING_LINE_PATTERN = r"^(?:[●◐◑◒◓◉◎∙]\s*)?.*\besc (?:to cancel|interrupt)\b.*$"


class CopilotCliProvider(BaseProvider):
    """Provider for GitHub Copilot CLI."""

    supports_stale_processing_capture = True
    supports_stale_waiting_capture = True

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
        requested_model: Optional[str],
    ) -> bool:
        return bool(agent_profile)

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model
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
            raw = get_backend().get_history(
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

    async def _wait_for_shell_ready(
        self, timeout: float = 30.0, polling_interval: float = 0.5
    ) -> bool:
        """Wait for a stable non-empty shell screen using provider-safe history reads.

        issue #494: real coroutine, not sync code called from an async
        caller (initialize()) -- the blocking history read is offloaded via
        asyncio.to_thread and the poll delay is asyncio.sleep, so this no
        longer blocks the shared event loop while waiting.
        """
        start_time = time.time()
        previous_output: Optional[str] = None
        stable_reads = 0

        while time.time() - start_time < timeout:
            output = await asyncio.to_thread(self._history, tail_lines=120)
            if output and output.strip():
                if previous_output is not None and output == previous_output:
                    stable_reads += 1
                    if stable_reads >= 2:
                        return True
                else:
                    stable_reads = 0

            previous_output = output
            await asyncio.sleep(polling_interval)

        return False

    def _command(self) -> str:
        config_dir = Path.home() / ".copilot"

        command_parts = ["copilot", "--allow-all"]

        if self._agent_profile:
            command_parts.extend(["--agent", self._agent_profile])
            if self._model:
                command_parts.extend(["--model", self._model])

        command_parts.extend(["--config-dir", str(config_dir)])
        try:
            pane_working_dir = get_backend().get_pane_working_directory(
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
        # A present-but-empty list is deny-all (same as Grok). A falsy check
        # used to skip --deny-tool entirely for allowed_tools=[].
        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("copilot_cli", self._allowed_tools)
            for tool in disallowed:
                command_parts.extend(["--deny-tool", tool])

        command_parts.append("--autopilot")

        return shlex.join(command_parts)

    def _build_runtime_mcp_config(self) -> str:
        # Resolve the bundled cao-mcp-server console script to a PATH-independent
        # invocation via the shared resolver.
        mcp_command, mcp_args = resolve_cao_mcp_command("cao-mcp-server", [])
        merged_servers: dict = {
            "cao-mcp-server": {
                "command": mcp_command,
                "args": mcp_args,
                "disabled": False,
                "env": {"CAO_TERMINAL_ID": self.terminal_id},
            }
        }

        # Agent Plugins: this runtime config is the only MCP configuration Copilot
        # reads, so a plugin server absent here is a plugin server Copilot never
        # sees. Review on #584 found it hardcoded to cao-mcp-server alone, which
        # meant plugin MCP delivery silently did not reach this provider at all.
        if self._agent_profile is not None:
            try:
                profile = _with_plugin_mcp(load_agent_profile(self._agent_profile), "copilot_cli")
            except Exception as exc:
                # Never block a launch on plugin delivery.
                logger.warning(
                    "Could not load profile '%s' for Copilot MCP config: %s",
                    self._agent_profile,
                    exc,
                )
                profile = None

            for name, cfg in ((profile.mcpServers if profile else None) or {}).items():
                if name in merged_servers:
                    # CAO's own in-session server is not replaceable by a plugin.
                    continue
                entry = dict(cfg) if isinstance(cfg, dict) else cfg.model_dump(exclude_none=True)
                declared = entry.get("type")
                translated = COPILOT_TRANSPORTS.get(declared) if isinstance(declared, str) else None
                if translated is not None:
                    entry["type"] = translated
                env = dict(entry.get("env", {}))
                env.setdefault("CAO_TERMINAL_ID", self.terminal_id)
                entry["env"] = env
                entry.setdefault("disabled", False)
                merged_servers[name] = entry

        return json.dumps({"mcpServers": merged_servers}, ensure_ascii=False)

    def _send_enter(self) -> None:
        get_backend().send_special_key(self.session_name, self.window_name, "Enter")

    def _send_key(self, key: str) -> None:
        get_backend().send_special_key(self.session_name, self.window_name, key)

    async def _accept_trust_prompts(self, timeout: float = 30.0) -> None:
        """Poll the pane and auto-accept Copilot's folder-trust dialogs.

        issue #494: real coroutine, not sync code called from an async
        caller. Re-entered from initialize()'s polling loop (see its
        WAITING_USER_ANSWER branch) -- that re-entrant contract is unchanged,
        only the blocking mechanics are: ``_history`` and the ``_send_enter``/
        ``_send_key`` backend calls are offloaded via ``asyncio.to_thread``
        (the two helpers themselves stay sync -- they are also called
        directly from tests and other contexts, so wrapping happens at each
        call site here rather than making them coroutines) and every poll
        delay is ``asyncio.sleep``, so this no longer blocks the shared event
        loop while polling.
        """
        start = time.time()
        while time.time() - start < timeout:
            raw_content = await asyncio.to_thread(self._history, tail_lines=120)
            content = raw_content.lower()

            if (
                "confirm folder trust" in content
                and re.search(r"\b1\.\s*yes\b", content)
                and re.search(r"\b2\.\s*yes,\s*and remember", content)
            ):
                # The first option is pre-selected; Enter is the most reliable
                # accept action across Copilot builds.
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if (
                "do you trust the files in this folder" in content
                or "do you trust the contents of this directory" in content
            ) and re.search(r"\b1\.\s*yes\b", content):
                # Option 1 is selected by default in the trust dialog; Enter is
                # the most reliable way to accept across Copilot builds.
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if "do you trust all the actions in this folder" in content:
                await asyncio.to_thread(self._send_key, "y")
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if re.search(r"\[\s*y\s*/\s*n\s*]", content):
                await asyncio.to_thread(self._send_key, "y")
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if "confirm folder trust" in content or "press enter to continue" in content:
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if re.search(WAITING_PROMPT_PATTERN, content, re.IGNORECASE):
                # Generic waiting prompt fallback: confirm with Enter.
                await asyncio.to_thread(self._send_enter)
                await asyncio.sleep(1)
                continue

            if self._has_idle_prompt_near_end(raw_content.splitlines()):
                return
            await asyncio.sleep(1)

        logger.warning(
            "Trust prompt handler timed out for %s:%s",
            self.session_name,
            self.window_name,
        )

    async def initialize(self) -> bool:
        """Initialize the Copilot CLI provider by starting ``copilot``.

        issue #494: ``self._command()`` does two blocking things --
        ``_supports_flag`` runs a cached blocking ``subprocess.run(["copilot",
        "--help"])`` and ``get_backend().get_pane_working_directory`` is a
        blocking subprocess call -- and ``get_backend().send_keys`` is
        likewise blocking. All three are offloaded via ``asyncio.to_thread``
        so building and sending the launch command can't block the shared
        event loop under concurrent session creation. ``status_monitor.
        get_status`` is offloaded too: it is NOT in-memory only -- for a
        terminal stuck in PROCESSING it can fork a real tmux capture-pane
        subprocess (status_monitor.py's stale-PROCESSING fallback), and this
        provider's own get_status falls back to ``self._history()`` (another
        capture-pane) when the buffer has no visible text. Copilot init is
        exactly the regime that trips the fallback -- cached status PROCESSING
        while the CLI boots, pane quiet during auth/MCP startup -- and this
        loop polls every second for up to 60s, multiplied by concurrent
        session creations.
        """
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        try:
            init_timeout = get_server_settings()["provider_init_timeout"]
            shell_ready = await wait_for_shell(self.terminal_id, timeout=init_timeout)
        except Exception as exc:
            logger.warning(
                "wait_for_shell failed for %s:%s, retrying with provider history: %s",
                self.session_name,
                self.window_name,
                exc,
            )
            init_timeout = get_server_settings()["provider_init_timeout"]
            shell_ready = await self._wait_for_shell_ready(timeout=init_timeout)

        if not shell_ready:
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        command = await asyncio.to_thread(self._command)
        await asyncio.to_thread(
            get_backend().send_keys, self.session_name, self.window_name, command
        )

        deadline = time.time() + 60.0
        await self._accept_trust_prompts(timeout=10.0)
        while time.time() < deadline:
            status = await asyncio.to_thread(status_monitor.get_status, self.terminal_id)
            if status == TerminalStatus.WAITING_USER_ANSWER:
                await self._accept_trust_prompts(timeout=5.0)
                await asyncio.sleep(1.0)
                continue
            if status in (TerminalStatus.IDLE, TerminalStatus.COMPLETED):
                # Return on the first ready state like the other providers.
                # The surrounding init loop remains provider-specific because
                # Copilot can surface late trust prompts that need explicit
                # handling before the terminal is actually usable.
                self._initialized = True
                return True
            await asyncio.sleep(1.0)

        await asyncio.to_thread(self._log_unready_screen)
        raise TimeoutError("Copilot initialization timed out after 60 seconds")

    def _log_unready_screen(self) -> None:
        lines = self._history(tail_lines=60).splitlines()
        unrecognized = self._unrecognized_rows_after_prompt(lines)
        if unrecognized is None:
            logger.warning(
                "Copilot idle prompt not found for %s:%s", self.session_name, self.window_name
            )
        elif unrecognized:
            logger.warning(
                "Copilot idle prompt for %s:%s is followed by unrecognized row %r",
                self.session_name,
                self.window_name,
                unrecognized[0],
            )
        else:
            logger.warning(
                "Copilot idle prompt recognized for %s:%s but status did not settle to idle",
                self.session_name,
                self.window_name,
            )
        screen_tail = [line for line in lines if line.strip()][-12:]
        logger.debug(
            "Copilot screen tail for %s:%s:\n%s",
            self.session_name,
            self.window_name,
            "\n".join(screen_tail),
        )

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
        if stripped.startswith("╭") or stripped.startswith("╰") or line.startswith("│"):
            return True
        # Copilot v1.0.31+ status bar: " autopilot · / commands    Claude Sonnet 4.6 · (0%)"
        if re.match(COPILOT_STATUS_BAR_PATTERN, stripped):
            return True
        # Copilot v1.0.31+ cwd breadcrumb: " ~/path [⎇ branch*%]"
        # (pre-v1.0.31 form included "(0x)" and was caught by the \(\d+x\) check above)
        if re.match(
            COPILOT_CWD_BREADCRUMB_PATTERN, line
        ):  # intentionally use raw line (preserves leading spaces)
            return True
        return False

    @staticmethod
    def _is_waiting_dialog_row(text: str) -> bool:
        stripped = text.strip().lower()
        framed = re.fullmatch(r"│\s*(.*?)\s*│", stripped)
        if framed:
            stripped = framed.group(1).strip()
        if re.match(ASSISTANT_PREFIX_PATTERN, stripped, re.IGNORECASE):
            return False
        return bool(
            re.match(WAITING_PROMPT_PATTERN, stripped)
            or re.search(r"\[\s*y\s*/\s*n\s*\]\s*$", stripped)
        )

    @classmethod
    def _is_busy_or_waiting_row(cls, text: str) -> bool:
        if re.match(ASSISTANT_PREFIX_PATTERN, text.strip(), re.IGNORECASE):
            return False
        return bool(
            re.search(COPILOT_BUSY_ROW_PATTERN, text.strip(), re.IGNORECASE)
            or cls._is_waiting_dialog_row(text)
        )

    @classmethod
    def _active_rows(cls, rows: list[str]) -> list[str]:
        active = []
        continuation_indent = None
        for row in rows:
            stripped = row.strip()
            if re.match(USER_PROMPT_LINE_PATTERN, row):
                continuation_indent = len(row) - len(row.lstrip())
                continue
            if cls._is_processing_line(row):
                active.append(row)
                continuation_indent = None
                continue
            if re.match(ASSISTANT_PREFIX_PATTERN, stripped, re.IGNORECASE):
                continuation_indent = len(row) - len(row.lstrip())
                continue
            if not stripped:
                continue
            if (
                continuation_indent is not None
                and len(row) - len(row.lstrip()) > continuation_indent
                and not re.search(COPILOT_BUSY_ROW_PATTERN, stripped, re.IGNORECASE)
            ):
                continue
            continuation_indent = None
            if cls._is_busy_or_waiting_row(row):
                active.append(row)
        return active

    @staticmethod
    def _is_hint_row(text: str) -> bool:
        stripped = text.strip().lower()
        return bool(
            re.search(COPILOT_HINT_BAR_PATTERN, stripped)
            or re.match(COPILOT_STATUS_BAR_PATTERN, stripped)
        )

    @staticmethod
    def _is_agent_model_row(text: str) -> bool:
        return bool(re.search(COPILOT_AGENT_MODEL_BAR_PATTERN, text.strip().lower()))

    @staticmethod
    def _is_processing_line(line: str) -> bool:
        stripped = line.strip()
        if re.match(ASSISTANT_PREFIX_PATTERN, stripped, re.IGNORECASE) and not re.match(
            r"^[●◐◑◒◓◉]\s*(?:thinking|working)\b", stripped, re.IGNORECASE
        ):
            return False
        return bool(re.match(PROCESSING_LINE_PATTERN, stripped, re.IGNORECASE))

    @classmethod
    def _has_idle_prompt_near_end(cls, lines: list[str]) -> bool:
        return cls._unrecognized_rows_after_prompt(lines) == []

    @classmethod
    def _last_composer_span(
        cls, lines: list[str], *, include_draft: bool = False
    ) -> Optional[tuple[int, int]]:
        """Return the last prompt or boxed composer bounds (end exclusive)."""
        span = None
        for idx, line in enumerate(lines):
            if re.match(IDLE_PROMPT_LINE_PATTERN, line):
                span = (idx, idx + 1)
            if idx + 3 >= len(lines):
                continue
            top = line.strip()
            bottom = lines[idx + 2].strip()
            if (
                re.fullmatch(r"╻▄{8,}", top)
                and (
                    re.fullmatch(r"┃\s*", lines[idx + 1].strip())
                    or (include_draft and re.fullmatch(r"┃\s+\S.*", lines[idx + 1].strip()))
                )
                and bottom == "╹" + "▀" * (len(top) - 1)
                and re.search(COPILOT_HINT_BAR_PATTERN, lines[idx + 3].strip().lower())
            ):
                span = (idx, idx + 3)
        return span

    @classmethod
    def _unrecognized_rows_after_prompt(cls, lines: list[str]) -> Optional[list[str]]:
        # TUI viewports can leave trailing blank rows below the composer.
        stripped = list(lines)
        while stripped and not stripped[-1].strip():
            stripped.pop()
        tail = stripped[-25:]
        span = cls._last_composer_span(tail)
        if span is None:
            return None
        start, end = span
        unrecognized = cls._rows_outside_idle_chrome(tail[end:])
        if end - start > 1:
            # Copilot can keep the composer visible during execution or approval.
            composer_start = len(stripped) - len(tail) + start
            last_user = cls._find_last_user_line(stripped[:composer_start])
            before = stripped[max(last_user, 0) : composer_start]
            unrecognized.extend(cls._active_rows(before))
        return unrecognized

    @classmethod
    def _rows_outside_idle_chrome(cls, rows: list[str]) -> list[str]:
        rows = [row for row in rows if row.strip()]
        unrecognized: list[str] = []
        for idx, row in enumerate(rows):
            if cls._is_busy_or_waiting_row(row):
                unrecognized.append(row)
            elif cls._is_hint_row(row) or cls._is_footer_line(row):
                continue
            elif cls._is_separate_agent_model_row(rows, idx):
                continue
            else:
                unrecognized.append(row)
        return unrecognized

    @classmethod
    def _is_separate_agent_model_row(cls, rows: list[str], idx: int) -> bool:
        if idx == 0 or idx != len(rows) - 1:
            return False
        hint_row = rows[idx - 1]
        return (
            cls._is_hint_row(hint_row)
            and not re.search(COPILOT_COLUMN_GAP_PATTERN, hint_row.strip())
            and not re.search(COPILOT_COLUMN_GAP_PATTERN, rows[idx].strip())
            and (
                cls._is_agent_model_row(rows[idx])
                or bool(re.search(COPILOT_MODEL_VERSION_PATTERN, rows[idx]))
            )
        )

    @classmethod
    def _normalize_post_user_lines(
        cls, lines: list[str], *, after_idle_prompt: bool = False
    ) -> list[str]:
        span = cls._last_composer_span(lines)
        last_prompt = span[0] if span is not None else -1
        composer_end = span[1] if span is not None else 0
        if last_prompt < 0 and after_idle_prompt:
            return cls._rows_outside_idle_chrome(lines)
        body = [
            line
            for line in lines[: last_prompt if last_prompt >= 0 else len(lines)]
            if line.strip()
            and not cls._is_footer_line(line)
            and not re.match(IDLE_PROMPT_LINE_PATTERN, line)
        ]
        trailing = (
            [
                line
                for line in cls._rows_outside_idle_chrome(lines[composer_end:])
                if not cls._is_footer_line(line)
            ]
            if last_prompt >= 0
            else []
        )
        normalized = body + trailing

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

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available,
        # before the tmux capture-pane fallback below (which is a tmux-only path).
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # For TUI apps, the raw FIFO buffer may contain only ANSI escapes.
        # Fall back to tmux capture-pane when the buffer has no visible text.
        cleaned = self._clean(output) if output else ""
        if not cleaned.strip():
            try:
                output = self._history()
            except Exception:
                pass
        if not output or not output.strip():
            return TerminalStatus.UNKNOWN

        output = self._clean(output)
        if not output.strip():
            return TerminalStatus.UNKNOWN

        lines = output.splitlines()
        has_idle_prompt_at_end = self._has_idle_prompt_near_end(lines)
        last_user = self._find_last_user_line(lines)
        recent_rows = lines[last_user if last_user >= 0 else max(0, len(lines) - 40) :]
        waiting_now = any(
            self._is_waiting_dialog_row(row) for row in self._active_rows(recent_rows)
        )
        if waiting_now:
            return TerminalStatus.WAITING_USER_ANSWER

        if not has_idle_prompt_at_end:
            if last_user >= 0:
                post = "\n".join(lines[last_user + 1 :])
                if re.search(ERROR_PATTERN, post, re.IGNORECASE):
                    return TerminalStatus.ERROR
            return TerminalStatus.PROCESSING

        composer_span = self._last_composer_span(lines)
        assert composer_span is not None
        composer_index = composer_span[0]
        before_composer = self._trim_tail_prompts(lines[:composer_index])
        if before_composer and self._is_processing_line(before_composer[-1]):
            return TerminalStatus.PROCESSING

        if last_user < 0:
            return TerminalStatus.IDLE

        post_lines = self._trim_tail_prompts(
            self._normalize_post_user_lines(lines[last_user + 1 :], after_idle_prompt=True)
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

    def probe_stale_processing_capture(self, output: str) -> TerminalStatus:
        cleaned = self._clean(output)
        if not cleaned.strip():
            return TerminalStatus.UNKNOWN
        lines = cleaned.splitlines()
        span = self._last_composer_span(lines)
        last_user = self._find_last_user_line(lines[: span[0]] if span else lines)
        post_user = lines[max(last_user, 0) :]
        # A composer below a dialog does not prove the dialog was dismissed.
        if any(self._is_waiting_dialog_row(row) for row in self._active_rows(post_user)):
            return TerminalStatus.WAITING_USER_ANSWER
        return self.get_status(output)

    def commit_stale_processing_capture(self, output: str, expected: TerminalStatus) -> bool:
        return self.probe_stale_processing_capture(output) == expected

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    @classmethod
    def _without_composer_metadata(cls, lines: list[str]) -> list[str]:
        """Remove anchored directory/session and leading thought metadata."""
        span = cls._last_composer_span(lines)
        if (
            span is None
            or not cls._has_idle_prompt_near_end(lines)
            or not any(cls._is_hint_row(row) for row in lines[span[1] :])
        ):
            return lines
        end = span[0]
        while end and (
            not lines[end - 1].strip() or re.fullmatch(r"[─-]{8,}", lines[end - 1].strip())
        ):
            end -= 1
        if (
            end < 3
            or lines[end - 2].strip()
            or lines[end - 3].strip()
            or not re.fullmatch(
                r"\s+(?:/|~/).+\s{3,}Session:\s+\d+(?:\.\d+)?\s+AIC used\s*",
                lines[end - 1],
            )
        ):
            return lines
        cwd_index = end - 1
        omitted = {cwd_index}
        end = cwd_index
        while end and not lines[end - 1].strip():
            end -= 1
        last_user = cls._find_last_user_line(lines[:end])
        first_reply = next(
            (
                idx
                for idx in range(last_user + 1, end)
                if re.match(ASSISTANT_PREFIX_PATTERN, lines[idx].strip(), re.IGNORECASE)
            ),
            end,
        )
        for idx in range(last_user + 1, first_reply):
            if not re.fullmatch(r"\s*⌄ Thought for \d+(?:\.\d+)?s\s*", lines[idx]):
                continue
            thought_rows = lines[idx + 1 : first_reply]
            if (
                first_reply < end
                and any(row.lstrip().startswith("│") for row in thought_rows)
                and all(not row.strip() or row.lstrip().startswith("│") for row in thought_rows)
            ):
                omitted.update(range(idx, first_reply))
        return [row for idx, row in enumerate(lines) if idx not in omitted]

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean_output = self._clean(script_output)
        lines = clean_output.splitlines()
        span = self._last_composer_span(lines, include_draft=True)
        if span is not None and span[1] - span[0] > 1 and lines[span[0] + 1].strip() != "┃":
            lines = lines[: span[0]]
        lines = self._without_composer_metadata(lines)
        clean_output = "\n".join(lines)
        last_user = self._find_last_user_line(lines)

        if last_user >= 0:
            post_lines = self._trim_tail_prompts(
                self._normalize_post_user_lines(
                    lines[last_user + 1 :],
                    after_idle_prompt=self._has_idle_prompt_near_end(lines),
                )
            )
            while post_lines and self._is_processing_line(post_lines[-1]):
                post_lines.pop()
            message = "\n".join(post_lines).strip()
            if message:
                return message
            raise ValueError("No provider response content found in terminal output")

        span = self._last_composer_span(lines)
        if span is not None and span[1] - span[0] > 1:
            clean_output = "\n".join(lines[: span[0]])
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
