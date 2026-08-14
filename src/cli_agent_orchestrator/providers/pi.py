"""Pi coding-agent provider implementation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import sys
from datetime import datetime
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

_STATE_KEYS = {"status", "lastAssistantText", "error", "updatedAt"}
_STATE_STATUSES = {"idle", "processing", "completed", "error"}
_MAX_STATE_BYTES = 1_048_576
_EDITOR_RULE = re.compile(r"^\s*[─━-]{20,}\s*$")
_PI_BANNER = re.compile(r"^\s*pi v\d+(?:\.\d+){1,3}\s*$", re.IGNORECASE)
_WORKING = re.compile(r"\bWorking\.\.\.(?:\s*\([^\n)]{1,100}\))?", re.IGNORECASE)
_STARTUP_ERROR = re.compile(
    r"(?:command not found:.*\bpi\b|"
    r"No such file or directory:.*\bpi\b|"
    r"Failed to run (?:in Gohan: )?[^\n]*|"
    r"pi MCP proxy configuration error:|"
    r"^(?:Error:|ERROR:|Fatal:|Traceback \(most recent call last\):))",
    re.IGNORECASE | re.MULTILINE,
)
_FOOTER_CONTEXT = re.compile(r"(?:\d+(?:\.\d+)?%|\?)/\d+(?:\.\d+)?[kKmM]?")
_FOOTER_LINE = re.compile(r"^\s*(?:\d+(?:\.\d+)?%|\?)/\d+(?:\.\d+)?[kKmM]?\b")


class ProviderError(RuntimeError):
    """Raised when Pi cannot be configured or launched safely."""


class PiProvider(BaseProvider):
    """Run Pi's regular TUI with CAO-owned lifecycle and MCP sidecars."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        resolved_pi = shutil.which("pi")
        if not resolved_pi:
            raise ProviderError("Pi executable 'pi' was not found on PATH")

        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self.pi_executable = Path(resolved_pi).resolve()
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False

        terminal_key = hashlib.sha256(terminal_id.encode("utf-8")).hexdigest()[:24]
        self.pi_root = CAO_HOME_DIR / "pi"
        self.runtime_dir = self.pi_root / terminal_key
        self.session_dir = self.runtime_dir / "sessions"
        self.prompt_path = self.runtime_dir / "system-prompt.md"
        self.mcp_config_path = self.runtime_dir / "mcp.json"
        self.state_path = self.runtime_dir / "state.json"
        self.extension_path = Path(__file__).with_name("pi_extension.ts").resolve()
        self._dispatch_pending = False
        self._dispatch_state_fingerprint: tuple[str, str, str, str] | None = None
        self._last_state_fingerprint: tuple[str, str, str, str] | None = None
        self._tui_processing_seen = False

    @property
    def paste_enter_count(self) -> int:
        """Pi submits bracketed paste with one Enter."""
        return 1

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)

    @staticmethod
    def _write_private_text(path: Path, content: str) -> Path:
        PiProvider._ensure_private_directory(path.parent)
        flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                fd = -1
                stream.write(content)
        finally:
            if fd >= 0:
                os.close(fd)
        return path

    def _load_profile(self) -> Any | None:
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except Exception as exc:
            raise ProviderError(
                f"Failed to load agent profile '{self._agent_profile}': {exc}"
            ) from exc

    def _write_prompt(self, profile: Any | None = None) -> Path:
        if profile is None and self._agent_profile is not None:
            profile = self._load_profile()
        base_prompt = ""
        if profile is not None:
            base_prompt = (profile.system_prompt or profile.prompt or "").strip()
        prompt = self._apply_skill_prompt(base_prompt)
        return self._write_private_text(self.prompt_path, prompt)

    def _write_mcp_config(self, profile: Any | None = None) -> Path:
        if profile is None and self._agent_profile is not None:
            profile = self._load_profile()
        servers: dict[str, dict[str, Any]] = {}
        if profile is not None and profile.mcpServers:
            for name, server in profile.mcpServers.items():
                if isinstance(server, dict):
                    config = dict(server)
                else:
                    config = server.model_dump(exclude_none=True)
                config = resolve_mcp_server_config(config)
                if "timeout" in config and "requestTimeoutMs" not in config:
                    config["requestTimeoutMs"] = config.pop("timeout")
                servers[name] = config
        payload = json.dumps(
            {"terminalId": self.terminal_id, "servers": servers},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return self._write_private_text(self.mcp_config_path, payload)

    def _build_pi_command(self) -> str:
        """Build Pi's explicit, shell-safe regular-TUI launch command."""
        profile = self._load_profile()
        self._ensure_private_directory(CAO_HOME_DIR)
        self._ensure_private_directory(self.pi_root)
        self._ensure_private_directory(self.runtime_dir)
        self._ensure_private_directory(self.session_dir)
        self._write_prompt(profile)
        self._write_mcp_config(profile)
        self._write_private_text(self.state_path, "{}")

        command_parts = [
            "env",
            f"CAO_PI_STATE_FILE={self.state_path}",
            f"CAO_PI_MCP_CONFIG={self.mcp_config_path}",
            f"CAO_PI_BRIDGE_PYTHON={sys.executable}",
            str(self.pi_executable),
            "--tui-mode",
            "regular",
            "--no-approve",
            "--no-extensions",
            "--extension",
            str(self.extension_path),
            "--no-skills",
            "--no-prompt-templates",
            "--session-id",
            self.terminal_id,
            "--session-dir",
            str(self.session_dir),
            "--append-system-prompt",
            str(self.prompt_path),
        ]

        resolved_model = self._model or (profile.model if profile is not None else None)
        if resolved_model:
            command_parts.extend(["--model", resolved_model])

        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("pi", self._allowed_tools)
            if disallowed:
                command_parts.extend(["--exclude-tools", ",".join(disallowed)])

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Launch Pi after the shell is ready and wait for extension-backed IDLE."""
        profile = self._load_profile()
        init_timeout = self.get_init_timeout(profile)
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        command = await asyncio.to_thread(self._build_pi_command)
        await asyncio.to_thread(
            get_backend().send_keys,
            self.session_name,
            self.window_name,
            command,
        )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + init_timeout
        ready = await wait_until_status(
            self.terminal_id,
            {
                TerminalStatus.IDLE,
                TerminalStatus.PROCESSING,
                TerminalStatus.COMPLETED,
                TerminalStatus.ERROR,
            },
            timeout=init_timeout,
            polling_interval=1.0,
        )
        if not ready:
            state = self._read_state()
            if state is not None:
                status = state["status"]
                if status == "error":
                    detail = state["error"] or "unknown extension error"
                    raise ProviderError(f"Pi extension failed to initialize: {detail}")
                raise ProviderError(f"Pi initialization reached unexpected {status} state")
            raise TimeoutError(f"Pi initialization timed out after {init_timeout}s")

        await self._wait_for_authoritative_idle(deadline, init_timeout)
        self._initialized = True
        return True

    async def _wait_for_authoritative_idle(self, deadline: float, init_timeout: float) -> None:
        """Require the extension sidecar to bind and report its initial idle state."""
        loop = asyncio.get_running_loop()
        while True:
            state = self._read_state()
            if state is not None:
                status = state["status"]
                if status == "idle":
                    self._last_state_fingerprint = self._state_fingerprint(state)
                    return
                if status == "error":
                    detail = state["error"] or "unknown extension error"
                    raise ProviderError(f"Pi extension failed to initialize: {detail}")
                raise ProviderError(f"Pi initialization reached unexpected {status} state")

            remaining = deadline - loop.time()
            if remaining <= 0:
                raise TimeoutError(f"Pi initialization timed out after {init_timeout}s")
            await asyncio.sleep(min(0.1, remaining))

    def _read_state(self) -> dict[str, str] | None:
        """Read a validated state snapshot without following a replaced symlink."""
        flags = os.O_RDONLY
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(self.state_path, flags)
        except OSError:
            return None

        try:
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                return None
            getuid = getattr(os, "getuid", None)
            if getuid is not None and metadata.st_uid != getuid():
                return None
            if stat.S_IMODE(metadata.st_mode) != 0o600:
                return None
            if metadata.st_size > _MAX_STATE_BYTES:
                return None
            payload = bytearray()
            while len(payload) <= _MAX_STATE_BYTES:
                chunk = os.read(fd, _MAX_STATE_BYTES + 1 - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            if len(payload) > _MAX_STATE_BYTES:
                return None
            state = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        finally:
            if fd >= 0:
                os.close(fd)

        if not isinstance(state, dict) or set(state) != _STATE_KEYS:
            return None
        if not all(isinstance(state[key], str) for key in _STATE_KEYS):
            return None
        if state["status"] not in _STATE_STATUSES:
            return None
        try:
            datetime.fromisoformat(state["updatedAt"].replace("Z", "+00:00"))
        except ValueError:
            return None
        return state

    def mark_input_received(self) -> None:
        """Guard against the extension's entire previous snapshot after dispatch."""
        if self._last_state_fingerprint is None:
            state = self._read_state()
            if state is not None:
                self._last_state_fingerprint = self._state_fingerprint(state)
        super().mark_input_received()
        self._dispatch_pending = True
        self._dispatch_state_fingerprint = self._last_state_fingerprint
        self._tui_processing_seen = False

    @staticmethod
    def _state_fingerprint(state: dict[str, str]) -> tuple[str, str, str, str]:
        return (
            state["status"],
            state["lastAssistantText"],
            state["error"],
            state["updatedAt"],
        )

    def _is_pre_dispatch_state(self, state: dict[str, str]) -> bool:
        return (
            self._dispatch_pending
            and self._dispatch_state_fingerprint is not None
            and self._state_fingerprint(state) == self._dispatch_state_fingerprint
        )

    def _status_from_state(self, state: dict[str, str]) -> TerminalStatus:
        fingerprint = self._state_fingerprint(state)
        if self._is_pre_dispatch_state(state):
            return TerminalStatus.PROCESSING
        self._last_state_fingerprint = fingerprint
        status = state["status"]
        if status == "idle":
            if self._dispatch_pending:
                return TerminalStatus.PROCESSING
            return TerminalStatus.IDLE
        if status == "processing":
            self._dispatch_pending = False
            self._dispatch_state_fingerprint = None
            self._tui_processing_seen = True
            return TerminalStatus.PROCESSING
        self._dispatch_pending = False
        self._dispatch_state_fingerprint = None
        if status == "completed":
            return TerminalStatus.COMPLETED
        return TerminalStatus.ERROR

    @staticmethod
    def _latest_tui_frame(clean: str) -> str:
        """Select the newest complete Pi redraw from CAO's rolling pane history."""
        lines = clean.splitlines()
        banner_indices = [index for index, line in enumerate(lines) if _PI_BANNER.fullmatch(line)]
        if len(banner_indices) >= 2:
            return "\n".join(lines[banner_indices[-1] :])

        footer_indices = [index for index, line in enumerate(lines) if _FOOTER_LINE.search(line)]
        if len(footer_indices) >= 2:
            return "\n".join(lines[footer_indices[-2] + 1 :])
        return clean

    def _status_from_tui(self, buffer: str) -> TerminalStatus:
        clean = strip_terminal_escapes(buffer)
        if not clean.strip():
            return TerminalStatus.UNKNOWN
        frame = self._latest_tui_frame(clean)
        if _STARTUP_ERROR.search(frame):
            return TerminalStatus.ERROR
        if _WORKING.search(frame):
            self._dispatch_pending = False
            self._dispatch_state_fingerprint = None
            self._tui_processing_seen = True
            return TerminalStatus.PROCESSING

        lines = frame.splitlines()
        rule_count = sum(bool(_EDITOR_RULE.fullmatch(line)) for line in lines[-20:])
        has_footer = bool(_FOOTER_CONTEXT.search("\n".join(lines[-10:])))
        has_pi_chrome = (
            "pi v" in frame.lower() or "ctrl+c/ctrl+d clear/exit" in frame.lower() or frame != clean
        )
        if rule_count >= 2 and has_footer and has_pi_chrome:
            if self._dispatch_pending:
                return TerminalStatus.PROCESSING
            if self._task_dispatched and self._tui_processing_seen:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        return TerminalStatus.UNKNOWN

    def get_status(self, buffer: str) -> TerminalStatus:
        """Return authoritative sidecar status, with conservative TUI fallback."""
        state = self._read_state()
        if state is not None:
            return self._status_from_state(state)

        native = self._resolve_native_status(buffer)
        if native is not None:
            return native
        return self._status_from_tui(self._resolve_buffer(buffer))

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Return exact completed sidecar text or a narrow regular-TUI fallback."""
        state = self._read_state()
        if state is not None:
            if not self._is_pre_dispatch_state(state) and state["status"] == "completed":
                self._last_state_fingerprint = self._state_fingerprint(state)
                self._dispatch_pending = False
                self._dispatch_state_fingerprint = None
                return state["lastAssistantText"]

        clean = strip_terminal_escapes(script_output)
        frame = self._latest_tui_frame(clean)
        if _WORKING.search(frame):
            raise ValueError("No completed Pi response found while Pi is working")
        lines = frame.splitlines()
        rule_indices = [index for index, line in enumerate(lines) if _EDITOR_RULE.fullmatch(line)]
        if len(rule_indices) < 2:
            raise ValueError("No completed Pi response found in terminal output")

        transcript = lines[: rule_indices[-2]]
        while transcript and not transcript[-1].strip():
            transcript.pop()
        block: list[str] = []
        while transcript and transcript[-1].strip():
            block.append(transcript.pop().strip())
        block.reverse()
        response = "\n".join(block).strip()
        if not response or response.startswith(
            ("Pi can explain", "Press ctrl+o", "escape interrupt", "pi v")
        ):
            raise ValueError("No completed Pi response found in terminal output")
        return response

    def exit_cli(self) -> str:
        """Return the tmux special key that exits Pi."""
        return "C-d"

    def cleanup(self) -> None:
        """Remove only per-provider transient files; retain Pi session data."""
        for path in (self.prompt_path, self.mcp_config_path, self.state_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        self._initialized = False
        self._dispatch_pending = False
        self._dispatch_state_fingerprint = None
        self._last_state_fingerprint = None
        self._tui_processing_seen = False
