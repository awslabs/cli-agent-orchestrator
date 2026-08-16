"""MiniMax Code CLI provider implementation."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

_PLUGIN_NAME = "cao-orchestrator"
_PLUGIN_SERVER_NAME = re.compile(r"^[a-z][a-z0-9]*(?:[._-][a-z0-9]+)*$")
_ICON_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class ProviderError(Exception):
    """Raised when MiniMax Code cannot be configured safely."""


class MiniMaxCodeProvider(BaseProvider):
    """Provider for the interactive ``mcode`` terminal UI."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> None:
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._data_dir: Optional[Path] = None
        self._has_received_input = False
        self._awaiting_turn = False
        self._turn_activity_seen = False
        self._last_completion_identity: Optional[str] = None

    supports_screen_detection = True
    supports_direct_status_probe = True

    @property
    def paste_enter_count(self) -> int:
        return 1

    @property
    def accepts_input_while_processing(self) -> bool:
        return True

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        return True

    @property
    def assume_processing_on_dispatch(self) -> bool:
        return True

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._has_received_input = True
        self._awaiting_turn = True
        self._turn_activity_seen = False

    @staticmethod
    def _is_substantive_user_line(line: str) -> bool:
        match = re.match(r"^\s*›[^\S\n]+(.*)$", line)
        return bool(match and any(character.isalnum() for character in match.group(1)))

    @classmethod
    def _latest_completion_identity(cls, clean: str) -> tuple[Optional[str], bool]:
        lines = clean.splitlines()
        completion_indices = [
            index
            for index, line in enumerate(lines)
            if re.match(r"^\s*└\s+Completed in\s+\d", line, re.IGNORECASE)
        ]
        if not completion_indices:
            return None, False
        completion = completion_indices[-1]
        user = max(
            (
                index
                for index, line in enumerate(lines[:completion])
                if cls._is_substantive_user_line(line)
            ),
            default=-1,
        )
        assistants = [
            index
            for index in range(user + 1, completion)
            if re.match(r"^\s*●\s+\S", lines[index])
            and not re.match(r"^\s*●\s+Ready\s*$", lines[index])
        ]
        anchored = user >= 0 or bool(assistants)
        if user >= 0:
            start = user
        elif assistants:
            start = assistants[-1]
        else:
            # A long final answer can push both its opening ``●`` and the user
            # row outside the fixed-height viewport while the completion note
            # remains visible. Hash the settled tail so that case still has a
            # completion identity; callers require observed turn activity
            # before accepting this unanchored fallback.
            start = max(0, completion - 30)
        material = "\n".join(lines[start : completion + 1])
        return hashlib.sha256(material.encode("utf-8")).hexdigest(), anchored

    @staticmethod
    def _source_data_dir() -> Path:
        configured = os.environ.get("MINIMAX_DATA_DIR", "").strip()
        return Path(configured).expanduser() if configured else Path.home() / ".minimax"

    def _data_dir_path(self) -> Path:
        digest = hashlib.sha256(self.terminal_id.encode("utf-8")).hexdigest()
        return CAO_HOME_DIR / "providers" / "minimax_code" / digest

    @staticmethod
    def _secure_tree(path: Path) -> None:
        os.chmod(path, 0o700)
        for child in path.rglob("*"):
            if child.is_symlink():
                child.unlink()
            elif child.is_dir():
                os.chmod(child, 0o700)
            elif child.is_file():
                os.chmod(child, 0o600)

    @classmethod
    def _copy_auth_material(cls, source: Path, destination: Path) -> None:
        for name in ("config.yaml", "local-runtime.auth.json"):
            source_file = source / name
            if source_file.is_file():
                shutil.copyfile(source_file, destination / name)
                os.chmod(destination / name, 0o600)

        source_auth = source / "cli-auth"
        destination_auth = destination / "cli-auth"
        if source_auth.is_dir():
            shutil.copytree(
                source_auth,
                destination_auth,
                symlinks=False,
                ignore_dangling_symlinks=True,
            )
            cls._secure_tree(destination_auth)

    @staticmethod
    def _serialize_server(name: str, raw_config: Any) -> dict[str, Any]:
        if not _PLUGIN_SERVER_NAME.fullmatch(name):
            raise ProviderError(f"Invalid MiniMax Code MCP server name: {name!r}")
        config = (
            dict(raw_config)
            if isinstance(raw_config, dict)
            else raw_config.model_dump(exclude_none=True)
        )
        config = resolve_mcp_server_config(config)
        command = config.get("command")
        if not isinstance(command, str) or not command:
            raise ProviderError(f"MCP server {name!r} must define a stdio command")

        env = {str(key): str(value) for key, value in (config.get("env") or {}).items()}
        command_path = Path(command)
        if command_path.is_absolute():
            command = command_path.name
            existing_path = env.get("PATH", os.environ.get("PATH", ""))
            env["PATH"] = os.pathsep.join(
                part for part in (str(command_path.parent), existing_path) if part
            )
        elif "/" in command or "\\" in command:
            raise ProviderError(
                f"MCP server {name!r} command must be a bare executable or absolute path"
            )

        return {
            "type": "stdio",
            "command": command,
            "args": [str(arg) for arg in (config.get("args") or [])],
            "env": env,
            "description": f"CAO-managed MCP server {name}",
            "timeout": 600_000,
        }

    def _write_plugin(self, data_dir: Path, mcp_servers: dict[str, Any]) -> None:
        plugin_dir = data_dir / "plugins" / _PLUGIN_NAME
        manifest_dir = plugin_dir / ".minimax-plugin"
        manifest_dir.mkdir(parents=True, mode=0o700)
        servers = {
            name: self._serialize_server(name, raw_config)
            for name, raw_config in mcp_servers.items()
        }
        for server in servers.values():
            server["env"]["CAO_TERMINAL_ID"] = self.terminal_id

        manifest = {
            "schemaVersion": 1,
            "name": _PLUGIN_NAME,
            "displayName": "CAO Orchestrator",
            "version": "1.0.0",
            "description": "Exposes CLI Agent Orchestrator tools to this terminal.",
            "author": "CLI Agent Orchestrator",
            "icon": "icon.png",
            "category": "Code",
            "exampleQueries": ["Use CAO Orchestrator to collaborate with another agent."],
            "apps": [],
            "mcpServers": ["servers.mcp.json"],
            "skills": [],
        }
        (manifest_dir / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        (plugin_dir / "servers.mcp.json").write_text(
            json.dumps({"schemaVersion": 1, "mcpServers": servers}), encoding="utf-8"
        )
        (plugin_dir / "icon.png").write_bytes(_ICON_PNG)
        self._secure_tree(plugin_dir)

    def _prepare_runtime(self) -> tuple[Path, str]:
        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as exc:
                raise ProviderError(
                    f"Failed to load agent profile {self._agent_profile!r}: {exc}"
                ) from exc

        if self._model or (profile is not None and profile.model):
            raise ProviderError(
                "MiniMax Code's interactive CLI does not expose a per-session model flag"
            )

        data_dir = self._data_dir_path()
        if data_dir.exists():
            shutil.rmtree(data_dir)
        data_dir.mkdir(parents=True, mode=0o700)
        os.chmod(data_dir, 0o700)
        self._copy_auth_material(self._source_data_dir(), data_dir)
        if profile is not None and profile.mcpServers:
            self._write_plugin(data_dir, profile.mcpServers)
        self._data_dir = data_dir

        system_prompt = (profile.system_prompt or "") if profile is not None else ""
        return data_dir, self._apply_skill_prompt(system_prompt)

    def _build_command(self) -> str:
        binary = shutil.which("mcode")
        if not binary:
            raise ProviderError(
                "MiniMax Code CLI not found: install it with "
                "'npm install -g @minimax-ai/code' and run 'mcode login'."
            )

        data_dir, bootstrap = self._prepare_runtime()
        if self._allowed_tools and "*" not in self._allowed_tools:
            tools = ", ".join(self._allowed_tools)
            bootstrap = (
                f"{SECURITY_PROMPT}\nYou only have access to these tools: {tools}\n\n"
                f"{bootstrap}"
            )
        bootstrap = (
            f"{bootstrap}\n\n"
            "You are starting inside a CLI Agent Orchestrator terminal. "
            "Apply the instructions above for all later turns in this session. "
            "Do not begin any task yet. Reply exactly CAO_MCODE_READY."
        ).strip()
        return shlex.join(
            [
                "env",
                f"MINIMAX_DATA_DIR={data_dir}",
                "TERM=xterm-256color",
                binary,
                bootstrap,
            ]
        )

    def _try_load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except Exception:
            return None

    async def _wait_for_bootstrap_ready(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            output = await asyncio.to_thread(
                get_backend().get_history,
                self.session_name,
                self.window_name,
                strip_escapes=True,
            )
            has_bootstrap_response = (
                re.search(r"(?m)^\s*●\s+CAO_MCODE_READY\s*$", output) is not None
            )
            if has_bootstrap_response and self.get_status(output) in {
                TerminalStatus.IDLE,
                TerminalStatus.COMPLETED,
            }:
                return True
            await asyncio.sleep(0.5)
        return False

    async def initialize(self) -> bool:
        init_timeout = float(self.get_init_timeout(self._try_load_profile()))
        ready_timeout = max(120.0, init_timeout)
        try:
            if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
                raise TimeoutError(f"Shell initialization timed out after {init_timeout:g}s")

            command = await asyncio.to_thread(self._build_command)
            from cli_agent_orchestrator.services.status_monitor import status_monitor

            status_monitor.notify_input_sent(self.terminal_id)
            await asyncio.to_thread(
                get_backend().send_keys,
                self.session_name,
                self.window_name,
                command,
            )
            if not await self._wait_for_bootstrap_ready(ready_timeout):
                raise TimeoutError(
                    f"MiniMax Code initialization timed out after {ready_timeout:g}s"
                )
            self._initialized = True
            return True
        except Exception:
            await asyncio.to_thread(self.cleanup)
            raise

    def get_status(self, buffer: str) -> TerminalStatus:
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native
        buffer = self._resolve_buffer(buffer)
        if not buffer:
            return TerminalStatus.UNKNOWN

        clean = strip_terminal_escapes(buffer)

        def last(pattern: str) -> int:
            return max(
                (match.start() for match in re.finditer(pattern, clean, re.IGNORECASE)),
                default=-1,
            )

        last_waiting = last(
            r"Approval needed|Run this command\?|Allow for this conversation|"
            r"Always allow this action|User prompt request"
        )
        last_processing = last(
            r"[⠁-⣿◇◆]\s+(?:Loading|Running)(?:\s+\d+s)?[^\n]*(?:Enter queue|Esc stop)"
        )
        last_completion = last(r"(?:^|\n)\s*└\s+Completed in\s+\d")
        last_ready = max(last(r"●\s+Ready"), last(r"Message\s+·\s+Enter send"))
        last_error = last(
            r"Sign in required|Authentication failed|Not authenticated|"
            r"(?:^|\n)\s*(?:Fatal|Error):"
        )
        completion_identity, completion_anchored = self._latest_completion_identity(clean)

        if last_waiting > max(last_processing, last_completion, last_ready):
            return TerminalStatus.WAITING_USER_ANSWER
        if last_processing > last_completion:
            if self._awaiting_turn:
                self._turn_activity_seen = True
            return TerminalStatus.PROCESSING
        if last_error > max(last_processing, last_completion, last_ready):
            return TerminalStatus.ERROR
        if last_ready >= 0:
            if self._awaiting_turn and completion_identity == self._last_completion_identity:
                if not self._turn_activity_seen:
                    return TerminalStatus.PROCESSING
            if self._awaiting_turn and completion_identity is None:
                return TerminalStatus.PROCESSING
            if self._awaiting_turn and not completion_anchored and not self._turn_activity_seen:
                return TerminalStatus.PROCESSING
            if completion_identity is not None and self._has_received_input:
                self._last_completion_identity = completion_identity
                self._awaiting_turn = False
                self._turn_activity_seen = False
                return TerminalStatus.COMPLETED
            if completion_identity is not None:
                self._last_completion_identity = completion_identity
            return TerminalStatus.IDLE
        if completion_identity is not None:
            if self._awaiting_turn and completion_identity == self._last_completion_identity:
                if not self._turn_activity_seen:
                    return TerminalStatus.PROCESSING
            if self._awaiting_turn and not completion_anchored and not self._turn_activity_seen:
                return TerminalStatus.PROCESSING
            self._last_completion_identity = completion_identity
            if self._has_received_input:
                self._awaiting_turn = False
                self._turn_activity_seen = False
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE
        if self._awaiting_turn:
            return TerminalStatus.PROCESSING
        return TerminalStatus.UNKNOWN

    def get_status_from_screen(self, screen_lines: list[str]) -> TerminalStatus:
        return self.get_status("\n".join(screen_lines))

    def extract_last_message_from_script(self, script_output: str) -> str:
        clean = strip_terminal_escapes(script_output)
        lines = clean.splitlines()

        last_user = max(
            (index for index, line in enumerate(lines) if self._is_substantive_user_line(line)),
            default=-1,
        )
        completion = max(
            (
                index
                for index, line in enumerate(lines)
                if re.match(r"^\s*└\s+Completed in\s+\d", line, re.IGNORECASE)
            ),
            default=len(lines),
        )
        assistant_starts = [
            index
            for index in range(last_user + 1, completion)
            if re.match(r"^\s*●\s+\S", lines[index])
            and not re.match(r"^\s*●\s+Ready\s*$", lines[index])
        ]
        if not assistant_starts:
            raise ValueError("No MiniMax Code final response found")

        start = assistant_starts[-1]
        response = [re.sub(r"^\s*●\s+", "", lines[start]).strip()]
        for line in lines[start + 1 : completion]:
            stripped = line.strip()
            if not stripped:
                continue
            if re.match(r"^(?:├|└|Called\s|Message\s+·|/[^ ]+\s+\|)", stripped):
                continue
            if re.match(r"^[╭│╰]", stripped):
                continue
            response.append(stripped)

        answer = "\n".join(response).strip()
        if not answer:
            raise ValueError("MiniMax Code final response was empty")
        return answer

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        data_dir = self._data_dir or self._data_dir_path()
        if data_dir.exists():
            shutil.rmtree(data_dir)
        self._data_dir = None
        self._initialized = False
        self._has_received_input = False
        self._awaiting_turn = False
        self._turn_activity_seen = False
        self._last_completion_identity = None
