"""Official xAI Grok Build CLI provider implementation.

Observed with ``grok 1.0.0 (3cd0d0cbce) [stable]`` in ``--no-alt-screen``
mode.  The empty composer remains visible while a turn is running, so status
detection gives the live ``Waiting for response…`` / ``[stop]`` /
``Esc:cancel`` markers priority.  Completed turns end at ``Worked for <time>``.
Grok reads MCP servers from ``$GROK_HOME/config.toml`` and exits with
``/quit``.  Tool approval pickers expose a ``N/M:select`` footer.

Each CAO terminal receives a private ``GROK_HOME``.  Its MCP configuration is
written directly and atomically (never through ``grok mcp add``, which rewrites
the file as mode 0664), and authentication is reused through a narrow symlink
to the user's existing ``~/.grok/auth.json`` rather than copying credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for Grok CLI provider-specific errors."""


# Render-stable current-turn signals from Grok Build 1.0.0.
PROCESSING_PATTERN = re.compile(
    r"Waiting for response…|\[stop\]|Esc:cancel|"
    r"[\u2800-\u28ff][^\n]*(?:Waiting for response|\u2026)",
    re.IGNORECASE,
)
# Do not bake the footer into this expression.  In the raw pipe-pane stream
# Grok writes a block cursor and cursor-positioning redraws between the
# ``Worked for`` text and ``Ctrl+x:shortcuts``; after escape normalization that
# is no longer whitespace (the proximity check in ``get_status`` handles it).
COMPLETION_PATTERN = re.compile(r"Worked\s+for\s+\d+(?:\.\d+)?[sm]")
# A visible completion marker occupies its own indented status line.  The raw
# pipe-pane equivalent is a cursor-positioned draw (optionally styled dim).
# Do not treat an arbitrary use of "Worked for" in assistant prose as chrome.
RENDERED_COMPLETION_PATTERN = re.compile(r"(?m)^[ \t]{2,}(Worked\s+for\s+\d+(?:\.\d+)?[sm])\s*$")
RAW_COMPLETION_PATTERN = re.compile(
    r"\x1b\[\d+;\d+H(?:\x1b\[[0-9;]*m)*(Worked\s+for\s+\d+(?:\.\d+)?[sm])"
)
QUERY_PATTERN = re.compile(r"^\s*❯\s+\S.*$", re.MULTILINE)
# The rendered pane is a normal ``│ ❯ │`` line, but Grok's raw pipe-pane
# stream positions each cell independently (e.g. CUP row/column sequences).
# ``strip_terminal_escapes`` removes those horizontal cursor moves, producing
# ``│❯│`` glued into a larger logical redraw line. Match that structural box
# anywhere in the recent tail; current processing markers still take priority.
IDLE_COMPOSER_PATTERN = re.compile(r"│\s*❯\s*│")
# Completed raw redraws commonly emit only ``Ctrl+x:shortcuts`` after the
# completion marker; ``Shift+Tab:mode`` may have been overwritten in place.
# Active turns instead show ``Esc:cancel``/``[stop]``, which are checked first.
READY_FOOTER_PATTERN = re.compile(r"Ctrl\+x:shortcuts", re.IGNORECASE)
WAITING_USER_PATTERN = re.compile(
    r"(?:\d+/\d+:select|Tab:next option|Ctrl\+c:cancel|"
    r"Waiting for approval\.\.\.|Approve in your browser to finish signing in|"
    r"Yes, proceed|No, reject \(type to add feedback\))",
    re.IGNORECASE,
)
ERROR_PATTERN = re.compile(
    r"^(?:Error:|ERROR:|panic:|Traceback \(most recent call last\):|"
    r"Authentication failed|Failed to (?:start|connect|load)|"
    r"Unknown model|Model .* (?:not found|unavailable))",
    re.IGNORECASE | re.MULTILINE,
)

_STATUS_TAIL_CHARS = 8192
_COMPLETION_TO_READY_MAX_CHARS = 4096
_TIMESTAMP_SUFFIX = re.compile(r"\s{2,}\d{1,2}:\d{2}\s+(?:AM|PM)\s*$", re.IGNORECASE)
_THOUGHT_LINE = re.compile(r"^\s*◆\s+Thought\b", re.IGNORECASE)
_TOOL_LINE = re.compile(r"^\s*┃")
_TELEMETRY_LINE = re.compile(
    r"Help improve Grok|Off by default\. Opt-in|Read Terms and Privacy Policy",
    re.IGNORECASE,
)
_CHROME_LINE = re.compile(
    r"(?:Shift\+Tab:mode|Ctrl\+x:shortcuts|Esc:cancel|\[stop\]|"
    r"Grok\s+\S+.*always-approve|Clipboard may be unreachable)",
    re.IGNORECASE,
)


def _toml_string(value: Any) -> str:
    """Serialize a scalar as a TOML-compatible basic string."""

    return json.dumps(str(value), ensure_ascii=False)


class GrokCliProvider(BaseProvider):
    """Provider for the official ``grok`` interactive TUI."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
        skill_prompt: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._turns = 0
        self._grok_home: Optional[Path] = None
        self._awaiting_turn_activity = False
        self._last_completion_fingerprint: Optional[str] = None

    @property
    def paste_enter_count(self) -> int:
        """Grok submits bracketed-paste input with one Enter."""

        return 1

    @property
    def paste_submit_delay(self) -> float:
        """Live probing found 0.4s sufficient for Grok's composer."""

        return 0.4

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """Approval and login pickers would consume orchestrated task text."""

        return True

    @property
    def grok_home(self) -> Optional[Path]:
        """The CAO-managed private home, exposed read-only for diagnostics/tests."""

        return self._grok_home

    def _try_load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except Exception:
            return None

    def _load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except FileNotFoundError:
            raise
        except Exception as exc:
            raise ProviderError(
                f"Failed to load agent profile '{self._agent_profile}': {exc}"
            ) from exc

    def _home_path(self) -> Path:
        digest = hashlib.sha256(self.terminal_id.encode("utf-8")).hexdigest()[:12]
        slug = re.sub(r"[^A-Za-z0-9_.-]", "_", self.terminal_id).strip("._") or "terminal"
        return CAO_HOME_DIR / "grok" / "terminals" / f"{slug[:48]}-{digest}"

    @staticmethod
    def _server_dict(server: Any) -> dict[str, Any]:
        if isinstance(server, dict):
            return dict(server)
        if hasattr(server, "model_dump"):
            return dict(server.model_dump(exclude_none=True))
        raise ProviderError(f"Unsupported MCP server configuration: {type(server).__name__}")

    def _render_mcp_config(self, mcp_servers: Optional[dict[str, Any]]) -> str:
        lines = ["# Managed by CLI Agent Orchestrator. Do not edit."]
        for name, raw_server in (mcp_servers or {}).items():
            config = self._server_dict(raw_server)
            if "command" in config:
                config = resolve_mcp_server_config(config)
                env = dict(config.get("env") or {})
                env["CAO_TERMINAL_ID"] = self.terminal_id
                config["env"] = env

            table = f"mcp_servers.{_toml_string(name)}"
            lines.extend(["", f"[{table}]"])
            if config.get("url"):
                lines.append(f"url = {_toml_string(config['url'])}")
            elif config.get("command"):
                lines.append(f"command = {_toml_string(config['command'])}")
                args = config.get("args") or []
                serialized_args = ", ".join(_toml_string(arg) for arg in args)
                lines.append(f"args = [{serialized_args}]")
            else:
                raise ProviderError(f"MCP server '{name}' has neither command nor url")
            lines.append(f"enabled = {'true' if config.get('enabled', True) else 'false'}")
            if config.get("timeout") is not None:
                # CAO's common MCP schema exposes one timeout knob. Grok has
                # separate startup/tool fields; applying the explicit value to
                # both preserves the profile's cap without relying on Grok's
                # provider-specific defaults.
                timeout = int(config["timeout"])
                lines.append(f"startup_timeout_sec = {timeout}")
                lines.append(f"tool_timeout_sec = {timeout}")

            env = config.get("env") or {}
            if env:
                lines.extend(["", f"[{table}.env]"])
                for key, value in env.items():
                    lines.append(f"{_toml_string(key)} = {_toml_string(value)}")

            headers = config.get("headers") or {}
            if headers:
                lines.extend(["", f"[{table}.headers]"])
                for key, value in headers.items():
                    lines.append(f"{_toml_string(key)} = {_toml_string(value)}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _atomic_write_private(path: Path, content: str) -> None:
        """Atomically publish UTF-8 text at mode 0600 in ``path``'s directory."""

        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path.parent, 0o700)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp_path = Path(temp_name)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                fd = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, path)
            os.chmod(path, 0o600)
            if stat.S_IMODE(path.stat().st_mode) != 0o600:
                raise ProviderError(f"Could not secure Grok config at {path}")
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

    def _prepare_grok_home(self, mcp_servers: Optional[dict[str, Any]]) -> Path:
        home = self._home_path()
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(home, 0o700)

        configured_home = os.environ.get("GROK_HOME", "").strip()
        auth_source = (
            Path(configured_home).expanduser() if configured_home else Path.home() / ".grok"
        ) / "auth.json"
        auth_link = home / "auth.json"
        if auth_source.is_file() and not auth_link.exists():
            auth_link.symlink_to(auth_source)

        self._atomic_write_private(home / "config.toml", self._render_mcp_config(mcp_servers))
        self._grok_home = home
        return home

    def _build_grok_command(self) -> str:
        binary = shutil.which("grok")
        if not binary:
            raise ProviderError(
                "Grok Build CLI not found: 'grok' is not on $PATH. "
                "Install the official xAI Grok Build CLI first."
            )

        profile = self._load_profile()
        mcp_servers = profile.mcpServers if profile is not None else None
        home = self._prepare_grok_home(mcp_servers)

        command_parts = [
            "env",
            f"GROK_HOME={home}",
            binary,
            "--no-alt-screen",
            "--always-approve",
            "--no-subagents",
        ]

        # Explicit launch/assign/handoff model wins, then profile model.
        model = self._model or (profile.model if profile is not None else None)
        if model:
            command_parts.extend(["--model", model])

        rules = self._apply_skill_prompt(profile.system_prompt if profile is not None else "")
        if rules:
            command_parts.extend(["--rules", rules])

        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            for tool in get_disallowed_tools("grok_cli", self._allowed_tools):
                command_parts.extend(["--deny", tool])
            if "web_fetch" not in self._allowed_tools:
                command_parts.append("--disable-web-search")

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        profile = self._try_load_profile()
        init_timeout = float(self.get_init_timeout(profile))
        try:
            if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
                raise TimeoutError(f"Shell initialization timed out after {init_timeout:g}s")

            command = await asyncio.to_thread(self._build_grok_command)
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            status_monitor.notify_input_sent(self.terminal_id)
            await asyncio.to_thread(
                get_backend().send_keys, self.session_name, self.window_name, command
            )
            if not await wait_until_status(
                self.terminal_id,
                {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
                timeout=init_timeout,
            ):
                raise TimeoutError(f"Grok CLI initialization timed out after {init_timeout:g}s")

            # Grok preserves an existing config mode. Repair defensively after
            # startup in case a future release rewrites it during migration.
            if self._grok_home is not None:
                config_path = self._grok_home / "config.toml"
                if config_path.exists():
                    await asyncio.to_thread(os.chmod, config_path, 0o600)
            self._initialized = True
            return True
        except Exception:
            await asyncio.to_thread(self.cleanup)
            raise

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        output = self._resolve_buffer(output)
        if not output:
            return TerminalStatus.UNKNOWN

        clean = strip_terminal_escapes(output)
        tail = clean[-_STATUS_TAIL_CHARS:]

        last_waiting = max(
            (match.start() for match in WAITING_USER_PATTERN.finditer(tail)), default=-1
        )
        last_processing = max(
            (match.start() for match in PROCESSING_PATTERN.finditer(tail)), default=-1
        )
        last_ready = max(
            (
                match.start()
                for pattern in (READY_FOOTER_PATTERN, IDLE_COMPOSER_PATTERN)
                for match in pattern.finditer(tail)
            ),
            default=-1,
        )
        last_footer = max(
            (match.start() for match in READY_FOOTER_PATTERN.finditer(tail)), default=-1
        )
        # ``Worked for`` also occurs in normal assistant prose.  It becomes a
        # completion boundary only when Grok's current ready footer follows it
        # closely.  This deliberately accepts raw redraw cells (including the
        # visible block cursor) between the two markers rather than requiring
        # whitespace-only adjacency.
        raw_completion_ordinals: dict[str, set[int]] = {}
        raw_counts: dict[str, int] = {}
        raw_completion_starts = {
            match.start(1) for match in RAW_COMPLETION_PATTERN.finditer(output)
        }
        for match in COMPLETION_PATTERN.finditer(output):
            marker = match.group()
            ordinal = raw_counts.get(marker, 0)
            raw_counts[marker] = ordinal + 1
            if match.start() in raw_completion_starts:
                raw_completion_ordinals.setdefault(marker, set()).add(ordinal)

        rendered_completion_starts = {
            match.start(1) for match in RENDERED_COMPLETION_PATTERN.finditer(tail)
        }
        clean_counts: dict[str, int] = {}
        completion_matches = []
        for match in COMPLETION_PATTERN.finditer(tail):
            marker = match.group()
            ordinal = clean_counts.get(marker, 0)
            clean_counts[marker] = ordinal + 1
            structurally_rendered = match.start() in rendered_completion_starts
            structurally_raw = ordinal in raw_completion_ordinals.get(marker, set())
            has_current_footer = 0 <= last_footer - match.end() <= _COMPLETION_TO_READY_MAX_CHARS
            has_later_query = any(
                query.start() > match.end() and query.start() < last_footer
                for query in QUERY_PATTERN.finditer(tail)
            )
            if (
                (structurally_rendered or structurally_raw)
                and has_current_footer
                and not has_later_query
            ):
                completion_matches.append(match)
        last_completion = completion_matches[-1].start() if completion_matches else -1
        last_error = max((match.start() for match in ERROR_PATTERN.finditer(tail)), default=-1)

        # Pickers/login are bottom-of-screen blocking surfaces. Position guards
        # keep a dismissed prompt retained in scrollback from pinning status.
        if last_waiting > max(last_completion, last_ready):
            return TerminalStatus.WAITING_USER_ANSWER

        if last_processing > last_completion:
            return TerminalStatus.PROCESSING

        if last_error > max(last_completion, last_ready, last_processing):
            return TerminalStatus.ERROR

        if last_ready >= 0:
            if last_completion >= 0 and self._turns > 0:
                completion_match = completion_matches[-1]
                query_matches = list(QUERY_PATTERN.finditer(tail[: completion_match.start()]))
                turn_start = (
                    query_matches[-1].start() if query_matches else completion_match.start()
                )
                fingerprint = hashlib.sha256(
                    tail[turn_start : completion_match.end()].encode("utf-8")
                ).hexdigest()
                if (
                    self._awaiting_turn_activity
                    and self._last_completion_fingerprint == fingerprint
                ):
                    return TerminalStatus.PROCESSING
                self._awaiting_turn_activity = False
                self._last_completion_fingerprint = fingerprint
                return TerminalStatus.COMPLETED
            # After dispatch, do not mistake the previous empty composer for
            # instant completion before Grok has rendered this turn.
            if self._turns > 0:
                return TerminalStatus.PROCESSING
            return TerminalStatus.IDLE

        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        return r"Shift\+Tab:mode[^\n]*Ctrl\+x:shortcuts"

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean = strip_terminal_escapes(script_output)
        completions = list(COMPLETION_PATTERN.finditer(clean))
        if not completions:
            raise ValueError("No Grok CLI completion boundary found")
        completion = completions[-1]

        queries = [match for match in QUERY_PATTERN.finditer(clean[: completion.start()])]
        if not queries:
            raise ValueError("No Grok CLI user query found before completion")
        query = queries[-1]

        body = clean[query.end() : completion.start()].splitlines()
        response_lines: list[str] = []
        for line in body:
            line = _TIMESTAMP_SUFFIX.sub("", line).rstrip()
            if _THOUGHT_LINE.search(line) or _TOOL_LINE.search(line):
                continue
            if _TELEMETRY_LINE.search(line) or _CHROME_LINE.search(line):
                continue
            response_lines.append(line)

        while response_lines and not response_lines[0].strip():
            response_lines.pop(0)
        while response_lines and not response_lines[-1].strip():
            response_lines.pop()

        nonempty_indents = [
            len(line) - len(line.lstrip()) for line in response_lines if line.strip()
        ]
        if nonempty_indents:
            common_indent = min(nonempty_indents)
            if common_indent:
                response_lines = [
                    line[common_indent:] if line.strip() else "" for line in response_lines
                ]

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("Empty Grok CLI response between query and completion")
        return response

    def exit_cli(self) -> str:
        return "/quit"

    def cleanup(self) -> None:
        self._initialized = False
        home = self._grok_home
        if home is None:
            return
        try:
            shutil.rmtree(home)
        except FileNotFoundError:
            self._grok_home = None
        except OSError as exc:
            logger.warning("Failed to remove Grok home %s: %s", home, exc)
        else:
            self._grok_home = None

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._turns += 1
        self._awaiting_turn_activity = True
