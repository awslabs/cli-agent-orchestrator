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

from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp as _with_plugin_mcp
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Working row, from the 17.2.10 and 18.20 fixtures: 17.x appends the interrupt
# key (``Working… ⟨esc⟩``, ``Running … ⟨esc⟩``); 18.x leads with the Escape
# keycap and the verb or tool intent (``⎋ Working…``, ``⎋ Run requested delay``).
_WORKING_PATTERN = re.compile(
    r"(?:\bWorking…\s+⟨esc⟩|\bRunning [^\n]*\s+⟨esc⟩|^[ \t]*⎋[ \t]+\S[^\n]*$)",
    re.MULTILINE,
)
# OMP 18.x draws the spinner and elapsed time in the status row above the
# composer (``╭── ⠹ 3s > … ╮`` on a box composer, `` ⠹ 3s > …`` on a band) and
# ``π`` there when idle. Only this row is repainted per frame, so its latest
# copy is the freshest screen state.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_WORKING_STATUS_ROW_PATTERN = re.compile(
    rf"^(?:╭──)?[ \t]*[{_SPINNER}][ \t]+\d[^\n>]*>[^\n]*$", re.MULTILINE
)
_READY_STATUS_ROW_PATTERN = re.compile(r"^(?:╭──)?[ \t]*π[ \t]+>[^\n]*$", re.MULTILINE)
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
# The bottom OMP composer frame is present in the captured idle/completed
# viewport. OMP 17.x and OMP 18.x with ``composer.shape: box`` close it as
# ``╰─ … ─╯``; OMP 18.x's default ``band`` composer leaves a bare ``╰─``.
_READY_FRAME_PATTERN = re.compile(r"^╰─(?:.*─╯)?[ \t]*$", re.MULTILINE)
# The status row above it: ``╭── OMP session … ╮`` (17.x), ``╭── π > … ╮``
# (18.x box) or `` π > … `` (18.x band).
_LIVE_STATUS_ROW = r"(?:╭──[^\n]*╮|[ \t]*\S+(?:[ \t]+\d+s)?[ \t]+>[ \t][^\n]*)"
_WORKING_LIVE_FRAME_TAIL_PATTERN = re.compile(rf"\s*{_LIVE_STATUS_ROW}[ \t]*\n\s*╰─[^\n]*\s*\Z")
# After a spinner status row only the composer frame follows; raw redraws can
# leave blank rows in between.
_STATUS_ROW_LIVE_TAIL_PATTERN = re.compile(r"[ \t]*\n\s*╰─[^\n]*\s*\Z")
_ERROR_LIVE_FRAME_TAIL_PATTERN = re.compile(
    r"\s*Use /login,[^\n]*\nThen use /model[^\n]*\n"
    rf"\s*{_LIVE_STATUS_ROW}[ \t]*\n\s*╰─[^\n]*\s*\Z"
)

# OMP 17.2.10 renders user turns on the terminal's page background and begins
# assistant text by resetting that background. Keep this boundary in the raw
# capture until the final assistant block is isolated; ANSI stripping alone
# would make a canceled user turn indistinguishable from an assistant response.
_ASSISTANT_BLOCK_START_PATTERN = re.compile(r"\x1b\[49m[ \t]*")
# The page background is theme-defined (``5;5;5`` on 17.2.10, ``15;18;22``
# under 18.x ``titanium``), so any truecolor background that ends its line opens
# a user turn. An 18.x box status row keeps text after its background.
_USER_BLOCK_START_PATTERN = re.compile(
    r"\x1b\[48;2;5;5;5m(?:\r?\n)?\s*|\x1b\[48;2;\d{1,3};\d{1,3};\d{1,3}m\r?\n\s*"
)

# Drawn only while a turn runs: the 18.x spinner with elapsed time, or the
# 17.x interrupt hint.
_TURN_ACTIVITY_PATTERN = re.compile(rf"[{_SPINNER}][ \t]+\d|⟨esc⟩")
# OSC 7501 Program Status (``ESC ] 7501 ; state=working:app=omp ST``), written
# by OMP 18.8.7+ on each state change while stdout is a TTY: idle, working,
# blocked, done, error or clear. Escape stripping removes it, so it is read from
# raw chunks.
_PROGRAM_STATUS_PATTERN = re.compile(r"\x1b\]7501;state=([a-z]+)[^\x07\x1b]*(?:\x07|\x1b\\)")
_OSC_START = "\x1b]"
_OSC_CARRY_MAX = 4096


_TOOL_BORDER_PATTERN = re.compile(r"^[╭╰├│].*[╮╯┤│]$|^─{20,}$")
_OMP_CHROME_PATTERN = re.compile(
    r"^(?:[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+(?:Working…|Running .*⟨esc⟩)|⎋\s+.*|"
    r"(?:π|[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\d+s)\s+>\s.*|"
    r"Allow tool:.*|Command:.*|up/down navigate.*|❯\s*Approve|Deny|"
    r"Advisor \d+ note|(?:▎\s*)?⟨nit⟩.*|Task already complete\.|"
    r"⟨(?:Wall|Timeout):.*⟩)$"
)


class ProviderError(Exception):
    """Raised for OMP-specific setup failures."""


# OMP 18.3.1+ opens a "Pasted N lines" menu for pastes of
# ``paste.largeMenuThreshold`` (100) lines or more, and CAO's submitting Enter
# would pick its first entry. This ``--config`` overlay turns only that off.
_INPUT_CONFIG_OVERLAY = "paste:\n  largeMenuThreshold: 0\n"


class OmpProvider(BaseProvider):
    """Launch and monitor an interactive OMP session inside a CAO terminal."""

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
        requested_model: Optional[str],
    ) -> bool:
        return True

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
        # Dispatch attribution; see _ready_status_for.
        self._awaiting_turn = False
        self._turn_activity_seen = False
        self._dispatch_recorded = False
        self._program_status: Optional[str] = None
        self._osc_carry = ""

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
            return _with_plugin_mcp(load_agent_profile(self._agent_profile), ProviderType.OMP.value)
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

        overlay_path = self._artifact_root() / "cao-input.yml"
        self._write_private_file(overlay_path, _INPUT_CONFIG_OVERLAY)
        command.extend(["--config", str(overlay_path)])

        context = ""
        if profile is not None:
            context = profile.system_prompt or profile.prompt or ""
        context = self._apply_skill_prompt(context)
        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import (
                tool_constraint_instruction,
            )

            context = (
                SECURITY_PROMPT
                + f"\n{tool_constraint_instruction(self._allowed_tools)}\n"
                + context
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
            config = resolve_mcp_server_config(config, persisted=False, name=name)
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

    def _ready_status_for(self) -> TerminalStatus:
        """Report a ready frame, attributing it to the pending dispatch if any.

        After a recorded dispatch, a ready frame completes the turn only once
        the turn was seen running (a ``working`` report, a spinner row or the
        17.x interrupt hint). Before that it is a redraw of the previous screen:
        the startup paint, the paste not yet submitted, the turn's first frame.
        """
        if not self._awaiting_turn or not self._dispatch_recorded:
            # A bare mark_input_received carries no text to attribute against.
            return self._ready_status()
        if self._turn_activity_seen:
            self._awaiting_turn = False
            return self._ready_status()
        return TerminalStatus.PROCESSING

    @staticmethod
    def _has_later_ready_frame(
        clean: str,
        marker_start: int,
        marker_end: int,
        live_frame_tail_pattern: Optional[re.Pattern] = None,
        retired_by_frame: bool = True,
        turn_ran: bool = False,
    ) -> bool:
        if any(
            match.start() > marker_start
            for pattern in (_STATUS_LINE_PATTERN,)
            for match in pattern.finditer(clean)
        ):
            return True

        # OMP 18 draws ``╰─`` mid-turn too (band frame, tool-box bottoms), so
        # it does not retire a spinner row; only ``π`` or a status line does.
        ready_frames = (
            [
                match
                for match in _READY_FRAME_PATTERN.finditer(clean)
                if match.start() > marker_start
            ]
            if retired_by_frame
            else []
        )
        # ``π`` retires the marker once the turn has run: after a spinner row
        # at or after the marker, or when the dispatched turn was seen running
        # (OMP redraws the working row after the last spinner frame). A ``π``
        # right after ``⎋ Working…`` is only the turn's first frame.
        spinner_rows = [
            match.start()
            for match in _WORKING_STATUS_ROW_PATTERN.finditer(clean)
            if match.start() >= marker_start
        ]
        anchor = spinner_rows[0] if spinner_rows else (marker_start if turn_ran else None)
        if anchor is not None:
            ready_frames += [
                match
                for match in _READY_STATUS_ROW_PATTERN.finditer(clean)
                if match.start() > anchor
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

        screen = self._get_screen_status(clean)
        program = self._program_status_verdict()
        if program is None:
            return screen
        # Before 18.18.9 OMP reported tool approvals as ``working``; the drawn
        # approval dialog stays authoritative.
        if screen == TerminalStatus.WAITING_USER_ANSWER:
            return screen
        return program

    def _program_status_verdict(self) -> Optional[TerminalStatus]:
        """Map the latest OSC 7501 report onto CAO's lifecycle, if OMP sent one."""
        state = self._program_status
        if state is None or state == "clear":
            return None
        if state == "working":
            return TerminalStatus.PROCESSING
        if state == "blocked":
            return TerminalStatus.WAITING_USER_ANSWER
        if state not in ("idle", "done", "error"):
            return None
        if self._awaiting_turn and self._dispatch_recorded and not self._turn_activity_seen:
            # OMP reports ``idle`` once more as it accepts the paste, before
            # ``working``; an earlier turn's ``done``/``error`` is no verdict either.
            return TerminalStatus.PROCESSING
        if state == "error":
            self._awaiting_turn = False
            return TerminalStatus.ERROR
        return self._ready_status_for()

    def _get_screen_status(self, clean: str) -> TerminalStatus:
        """Classify the rendered viewport or stripped raw text by its drawn markers."""
        if not clean.strip():
            return TerminalStatus.UNKNOWN
        candidates = []
        for pattern, status, live_frame_tail_pattern, retired_by_frame in (
            (_WAITING_PATTERN, TerminalStatus.WAITING_USER_ANSWER, None, True),
            (_ERROR_PATTERN, TerminalStatus.ERROR, _ERROR_LIVE_FRAME_TAIL_PATTERN, True),
            (_WORKING_PATTERN, TerminalStatus.PROCESSING, _WORKING_LIVE_FRAME_TAIL_PATTERN, True),
            (
                _WORKING_STATUS_ROW_PATTERN,
                TerminalStatus.PROCESSING,
                _STATUS_ROW_LIVE_TAIL_PATTERN,
                False,
            ),
        ):
            matches = list(pattern.finditer(clean))
            if not matches:
                continue
            marker = matches[-1]
            if not self._has_later_ready_frame(
                clean,
                marker.start(),
                marker.end(),
                live_frame_tail_pattern,
                retired_by_frame,
                self._turn_activity_seen,
            ):
                candidates.append((marker.start(), status))

        if candidates:
            status = max(candidates, key=lambda candidate: candidate[0])[1]
            if status == TerminalStatus.PROCESSING and self._awaiting_turn:
                self._turn_activity_seen = self._turn_activity_seen or bool(
                    _TURN_ACTIVITY_PATTERN.search(clean)
                )
            return status

        if (
            _STATUS_LINE_PATTERN.search(clean)
            or _READY_FRAME_PATTERN.search(clean)
            or _READY_STATUS_ROW_PATTERN.search(clean)
        ):
            return self._ready_status_for()
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
        self._awaiting_turn = False
        self._turn_activity_seen = False
        self._dispatch_recorded = False
        self._program_status = None
        self._osc_carry = ""

    def mark_input_received(self) -> None:
        super().mark_input_received()
        self._turns += 1
        self._awaiting_turn = True
        self._turn_activity_seen = False
        self._dispatch_recorded = False

    def record_status_chunk(self, chunk: str, epoch: int) -> None:
        """Track OSC 7501 reports and turn activity before the monitor crops output.

        Runs under the monitor lock for every chunk, so it only updates flags.
        OMP sends each report once, and a long turn's first frames leave the
        cropped buffer, so both are read here.
        """
        data = self._osc_carry + chunk
        reports = _PROGRAM_STATUS_PATTERN.findall(data)
        if reports:
            self._program_status = reports[-1]
        if self._awaiting_turn and ("working" in reports or _TURN_ACTIVITY_PATTERN.search(data)):
            self._turn_activity_seen = True
        # Keep an OSC sequence split across FIFO reads for the next chunk.
        start = data.rfind(_OSC_START)
        unterminated = start != -1 and not re.search(r"\x07|\x1b\\", data[start + 2 :])
        if unterminated:
            self._osc_carry = data[start:][-_OSC_CARRY_MAX:]
        elif data.endswith("\x1b"):
            # A chunk can end on the ESC that opens the next report, which OMP
            # never repeats.
            self._osc_carry = "\x1b"
        else:
            self._osc_carry = ""

    def record_dispatched_message(self, message: str) -> None:
        super().record_dispatched_message(message)
        self._dispatch_recorded = True
