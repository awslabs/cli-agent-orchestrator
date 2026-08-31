"""Oh My Pi (``omp``) provider integration.

The provider deliberately augments OMP's normal discovery rather than replacing
its profile, tools, rules, skills, approvals, or MCP configuration. CAO context
is appended as a system-prompt file and CAO MCP servers are supplied through a
per-terminal extension package.
"""

import copy
import json
import logging
import os
import re
import shlex
import shutil
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Captured from OMP 17.2.10 in test/providers/fixtures. The tool-execution
# indicator is a separate live state emitted after the model's Working… phase.
_WORKING_PATTERN = re.compile(r"(?:\bWorking…\s+⟨esc⟩|\bRunning [^\n]*\s+⟨esc⟩)")
_WAITING_PATTERN = re.compile(r"^\s*Allow tool:\s+\S+", re.MULTILINE)
# Kept as a source-compatible ready-frame anchor. OMP's rendered terminal can
# omit this row, but raw/screen buffers from configurations that show it use the
# exact grammar below.
_STATUS_LINE_PATTERN = re.compile(
    r"^\s*in:\s+\d+\s+out:\s+\d+(?:\s+cache\s+\S+)?\s+t:\s+\S+\s+tok/s:\s+\S+",
    re.MULTILINE,
)
# Error rendering is intentionally limited to the captured runtime frame.
# Generic ``Error:`` prose belongs to the assistant response and must never
# change lifecycle state.
_ERROR_PATTERN = re.compile(r"^\s*Error:\s+No model selected\.\s*$", re.MULTILINE)
# The bottom OMP title frame is present in the captured idle/completed viewport.
_READY_FRAME_PATTERN = re.compile(r"^╰─.*─╯\s*$", re.MULTILINE)
_WORKING_LIVE_FRAME_TAIL_PATTERN = re.compile(r"\s*╭── OMP session [^\n]*╮\s*\n╰─[^\n]*─╯\s*\Z")
_ERROR_LIVE_FRAME_TAIL_PATTERN = re.compile(
    r"\s*Use /login,[^\n]*\nThen use /model[^\n]*\n"
    r"\s*╭── OMP session [^\n]*╮\s*\n╰─[^\n]*─╯\s*\Z"
)

# OMP 17.2.10 renders user turns on the terminal's page background and begins
# assistant text by resetting that background. Keep this boundary in the raw
# capture until the final assistant block is isolated; ANSI stripping alone
# would make a canceled user turn indistinguishable from an assistant response.
_ASSISTANT_BLOCK_START_PATTERN = re.compile(r"\x1b\[49m[ \t]*")
_USER_BLOCK_START_PATTERN = re.compile(r"\x1b\[48;2;5;5;5m(?:\r?\n)?\s*")

_TOOL_BORDER_PATTERN = re.compile(r"^[╭╰├│].*[╮╯┤│]$|^─{20,}$")
_OMP_CHROME_PATTERN = re.compile(
    r"^(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+(?:Working…|Running .*⟨esc⟩)|"
    r"Allow tool:.*|Command:.*|up/down navigate.*|❯\s*Approve|Deny|"
    r"Advisor \d+ note|(?:▎\s*)?⟨nit⟩.*|Task already complete\.|"
    r"⟨(?:Wall|Timeout):.*⟩)$"
)


class ProviderError(Exception):
    """Raised for OMP-specific setup failures."""


class OmpProvider(BaseProvider):
    """Launch and monitor an interactive OMP session inside a CAO terminal."""

    supports_screen_detection = True
    supports_direct_status_probe = False

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._agent_profile = agent_profile
        self._model = model
        self._initialized = False
        self._turns = 0
        self._artifact_dir: Optional[Path] = None

    @property
    def paste_enter_count(self) -> int:
        return 1

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        return True

    async def initialize(self) -> bool:
        """Start OMP and wait for its first ready state."""
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self.shell_baseline = get_backend().get_pane_current_command(
            self.session_name, self.window_name
        )

        command = self._build_omp_command()
        get_backend().send_keys(self.session_name, self.window_name, command)
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(init_timeout),
        ):
            raise TimeoutError(f"OMP initialization timed out after {init_timeout}s")

        self._initialized = True
        return True

    def _load_profile(self):
        if self._agent_profile is None:
            return None
        try:
            return load_agent_profile(self._agent_profile)
        except Exception as exc:
            raise ProviderError(
                f"Failed to load agent profile '{self._agent_profile}': {exc}"
            ) from exc

    def _artifact_root(self) -> Path:
        root = CAO_HOME_DIR / "tmp" / "omp" / self.terminal_id
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self._artifact_dir = root
        return root

    @staticmethod
    def _write_private_file(path: Path, content: str) -> None:
        path.write_text(content, encoding="utf-8")
        os.chmod(path, 0o600)

    def _build_omp_command(self) -> str:
        """Build an additive OMP command without narrowing native configuration."""
        if not shutil.which("omp"):
            raise ProviderError(
                "Oh My Pi not found: 'omp' is not on $PATH. Install OMP and authenticate before launch."
            )

        profile = self._load_profile()
        command = ["omp"]
        model = self._model or (profile.model if profile is not None else None)
        if model:
            command.extend(["--model", model])

        context = ""
        if profile is not None:
            context = profile.system_prompt or profile.prompt or ""
        context = self._apply_skill_prompt(context)
        if self._allowed_tools and "*" not in self._allowed_tools:
            tools_list = ", ".join(self._allowed_tools)
            context = (
                SECURITY_PROMPT + f"\nYou only have access to these tools: {tools_list}\n" + context
            )
        if context:
            context_path = self._artifact_root() / "context.md"
            self._write_private_file(context_path, context)
            command.extend(["--append-system-prompt", str(context_path)])

        if profile is not None and profile.mcpServers:
            extension_dir = self._write_extension_root(profile.mcpServers)
            command.extend(["--extension", extension_dir])

        return shlex.join(command)

    def _write_extension_root(self, mcp_servers: dict) -> str:
        """Create OMP's supported, low-priority extension root for CAO MCP servers."""
        extension_dir = self._artifact_root()
        self._write_private_file(extension_dir / "index.js", "export default function () {}")

        servers: dict = {}
        for name, raw_config in mcp_servers.items():
            if isinstance(raw_config, dict):
                config = copy.deepcopy(raw_config)
            else:
                config = raw_config.model_dump(exclude_none=True)
            config = resolve_mcp_server_config(config, persisted=False)
            if "command" in config:
                env = dict(config.get("env") or {})
                if "CAO_TERMINAL_ID" not in env:
                    env["CAO_TERMINAL_ID"] = self.terminal_id
                    config["env"] = env
            servers[name] = config

        self._write_private_file(
            extension_dir / ".mcp.json",
            json.dumps({"mcpServers": servers}, indent=2) + "\n",
        )
        return str(extension_dir)

    def _ready_status(self) -> TerminalStatus:
        return TerminalStatus.COMPLETED if self._turns else TerminalStatus.IDLE

    @staticmethod
    def _has_later_ready_frame(
        clean: str,
        marker_start: int,
        marker_end: int,
        live_frame_tail_pattern: Optional[re.Pattern] = None,
    ) -> bool:
        if any(
            match.start() > marker_start
            for pattern in (_STATUS_LINE_PATTERN,)
            for match in pattern.finditer(clean)
        ):
            return True

        ready_frames = [
            match for match in _READY_FRAME_PATTERN.finditer(clean) if match.start() > marker_start
        ]
        if not ready_frames:
            return False

        # The captured live working/error viewports include one title footer.
        # Preserve a marker only when the entire remaining tail is that known
        # frame; extra output means the append-only raw buffer advanced.
        return not (
            len(ready_frames) == 1
            and live_frame_tail_pattern is not None
            and live_frame_tail_pattern.fullmatch(clean[marker_end:])
        )

    def _get_status_from_clean(self, clean: str) -> TerminalStatus:
        if self._initialized and self.shell_baseline:
            current_cmd = get_backend().get_pane_current_command(
                self.session_name, self.window_name
            )
            if current_cmd == self.shell_baseline:
                return TerminalStatus.ERROR

        if not clean.strip():
            return TerminalStatus.UNKNOWN
        candidates = []
        for pattern, status, live_frame_tail_pattern in (
            (_WAITING_PATTERN, TerminalStatus.WAITING_USER_ANSWER, None),
            (_ERROR_PATTERN, TerminalStatus.ERROR, _ERROR_LIVE_FRAME_TAIL_PATTERN),
            (_WORKING_PATTERN, TerminalStatus.PROCESSING, _WORKING_LIVE_FRAME_TAIL_PATTERN),
        ):
            matches = list(pattern.finditer(clean))
            if not matches:
                continue
            marker = matches[-1]
            if not self._has_later_ready_frame(
                clean, marker.start(), marker.end(), live_frame_tail_pattern
            ):
                candidates.append((marker.start(), status))

        if candidates:
            return max(candidates, key=lambda candidate: candidate[0])[1]

        if _STATUS_LINE_PATTERN.search(clean) or _READY_FRAME_PATTERN.search(clean):
            return self._ready_status()
        return TerminalStatus.UNKNOWN

    def get_status(self, buffer: Optional[str]) -> TerminalStatus:
        """Classify raw OMP terminal output with stale-frame protection."""
        native = self._resolve_native_status(buffer)
        if native is not None:
            return native
        output = self._resolve_buffer(buffer)
        return self._get_status_from_clean(strip_terminal_escapes(output))

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Classify OMP's pyte-composited viewport using the same precedence."""
        clean = "\n".join(line.rstrip() for line in screen_lines if line.strip())
        return self._get_status_from_clean(clean)

    def get_idle_pattern_for_log(self) -> str:
        return _STATUS_LINE_PATTERN.pattern

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the final rendered assistant block without deleting prose by words."""
        user_blocks = list(_USER_BLOCK_START_PATTERN.finditer(script_output))
        if not user_blocks:
            raise ValueError("No rendered OMP user-turn boundary found")
        assistant_block = _ASSISTANT_BLOCK_START_PATTERN.search(
            script_output, user_blocks[-1].end()
        )
        if assistant_block is None:
            raise ValueError("No complete OMP assistant block found")

        clean = strip_terminal_escapes(script_output[assistant_block.start() :])
        lines = clean.splitlines()
        footer_indices = [
            index
            for index, line in enumerate(lines)
            if _STATUS_LINE_PATTERN.search(line) or _READY_FRAME_PATTERN.search(line)
        ]
        if not footer_indices:
            raise ValueError("No complete OMP response frame found")

        response_lines: list[str] = []
        for line in lines[: footer_indices[-1]]:
            if _TOOL_BORDER_PATTERN.search(line) or _OMP_CHROME_PATTERN.search(line.strip()):
                continue
            response_lines.append(line.rstrip())

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("OMP response contained only chrome")
        return response

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        """Remove only this terminal's generated prompt/extension artifacts."""
        if self._artifact_dir is not None:
            try:
                shutil.rmtree(self._artifact_dir)
            except FileNotFoundError:
                pass
            except OSError as exc:
                logger.warning("OMP cleanup failed for %s: %s", self._artifact_dir, exc)
        self._artifact_dir = None
        self._initialized = False
        self._turns = 0

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._turns += 1
