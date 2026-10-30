"""OpenCode CLI provider implementation.

This module provides the OpenCodeCliProvider class for integrating with OpenCode,
a terminal-based AI assistant with a native agent system.

OpenCode Features:
- Agent-based conversations via YAML-frontmatter Markdown agent files
- Per-agent tool/permission configuration via frontmatter and opencode.json
- MCP server support with per-agent tool gating
- Full-screen v1 TUI and v2 Mini scrollback interface

The provider detects the following terminal states:
- IDLE: Waiting for user input (``ctrl+p commands`` footer, no ``esc interrupt``)
- PROCESSING: Agent working (``esc interrupt`` footer)
- COMPLETED: Agent finished turn (``▣ <agent> · <model> · Ns`` marker + idle footer)
- WAITING_USER_ANSWER: Permission dialog active (``△ Permission required`` visible)
- UNKNOWN: Fallback when no state marker matches (or empty buffer)
- ERROR: CLI exited or startup failed
"""

import asyncio
import json
import logging
import re
import shlex
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import OPENCODE_CONFIG_DIR, OPENCODE_CONFIG_FILE
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import (
    BaseProvider,
    OutputExtractionRejected,
    ProviderExitedError,
)
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.opencode_cli import detect_opencode_major
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# =============================================================================
# Regex Patterns — verified from live OpenCode TUI probe fixtures.
# =============================================================================

# ANSI escape code pattern — handles 24-bit truecolor sequences (\x1b[38;2;R;G;Bm)
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# User message indent: blue vertical bar + 2 spaces
USER_MESSAGE_PATTERN = r"^┃\s{2}"

# Per-turn completion marker: "▣  <agent>  ·  <model>  ·  <duration>"
# Two middle-dot separators and a trailing duration are required.
# OpenCode formats duration as "Ns" for short runs and "Nm Ns" once the turn
# exceeds 60 seconds (e.g. "1m 8s").  Both forms must be matched.
# The agent is shown by its display name, which may contain spaces
# ("Sisyphus - Ultraworker"), so it runs up to the first separator.  It must not
# cross a newline, or a stray "▣" earlier in the response would start the match.
COMPLETION_MARKER_PATTERN = r"▣\s+[^·\n]+·\s+.+?\s+·\s+(?:\d+m\s+)?\d+(?:\.\d+)?s"

# Processing footer — keybind hint present while the agent is generating.
PROCESSING_FOOTER_PATTERN = r"\besc interrupt\b"

# Idle footer anchor — present when waiting for the next user message.
IDLE_FOOTER_PATTERN = r"ctrl\+p\s+commands"

# Permission prompt heading — both initial request and "Always allow" sub-confirmation.
PERMISSION_PROMPT_PATTERN = r"△\s+(?:Permission required|Always allow)\b"

# Tool-call in-flight spinner (braille animation): "⠋ Read <path>" etc.
TOOL_CALL_IN_FLIGHT_PATTERN = r"^\s+[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+\S+"


class OpenCodeCliProvider(BaseProvider):
    """Provider for OpenCode CLI integration.

    Manages the lifecycle of an OpenCode TUI session inside a tmux window:
    initialization, status detection, response extraction, and cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance
        session_name: Name of the tmux session containing this terminal
        window_name: Name of the tmux window for this terminal
        _agent_profile: Name of the installed OpenCode agent to launch
        _model: Optional model override (e.g. ``anthropic/claude-sonnet-4-6``)
    """

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional["AgentProfile"],
        requested_model: Optional[str],
    ) -> bool:
        return True

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        model: Optional[str] = None,
    ):
        """Initialize OpenCode CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal
            session_name: Name of the tmux session
            window_name: Name of the tmux window
            agent_profile: Name of the installed OpenCode agent (e.g. ``"developer"``)
            allowed_tools: Optional CAO tool list (informational; enforcement is via frontmatter)
            model: Optional model override passed via ``--model`` at launch
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._agent_profile = agent_profile or ""
        self._model = model
        self._initialized = False
        self._cli_major = 1
        self._shell_baseline: Optional[str] = None
        self._dispatched_message: Optional[str] = None

    @property
    def runtime_variant(self) -> str:
        """Persist the interface of the running process, not the installed binary."""
        return f"opencode-v{self._cli_major}"

    def restore_runtime_variant(self, value: Optional[str]) -> None:
        """Restore a launch generation; recover pre-persistence rows from live UI only."""
        if value is None:
            clean = strip_terminal_escapes(
                get_backend().get_history(self.session_name, self.window_name, visible_only=True)
            )
            v1 = bool(re.search(IDLE_FOOTER_PATTERN, clean))
            v2 = bool(re.search(r"ctrl\+p\s+menu", clean))
            if v1 != v2:
                self._cli_major = 1 if v1 else 2
                return
            raise RuntimeError(
                "Cannot determine the running OpenCode terminal's CLI generation. "
                "Recreate the terminal; its launch generation was not persisted."
            )
        if value not in ("opencode-v1", "opencode-v2"):
            raise ValueError(f"Unsupported OpenCode runtime variant: {value!r}")
        self._cli_major = 1 if value == "opencode-v1" else 2

    @property
    def paste_enter_count(self) -> int:
        """OpenCode TUI submits on a single Enter after bracketed paste."""
        return 1

    @property
    def paste_submit_delay(self) -> float:
        """OpenCode's TUI can swallow an Enter sent too soon after the bracketed-paste
        end marker. 1.0s (matching kiro_cli) is conservative and avoids the
        deferred-init "never started processing" race (see #479)."""
        return 1.0

    # Opt-in for the deferred-init direct status probe (capture-pane bypass).
    # OpenCode's get_status() detector is line-oriented and works correctly on a
    # rendered capture-pane snapshot. Providers whose get_status() relies on
    # dispatch bookkeeping (e.g. kiro_cli, antigravity_cli, cursor_cli) must NOT
    # set this flag — their COMPLETED/IDLE split is not screen-detectable.
    supports_direct_status_probe = True

    @property
    def extraction_tail_lines(self) -> int:
        """Capture extra scrollback for extraction (belt-and-braces).

        Consumed by ``terminal_service.get_output`` via a ``getattr`` capability
        check: providers that define this attribute opt in to a deeper
        ``capture-pane`` for LAST-mode extraction.

        OpenCode renders in alt-screen mode, so tmux's history_size stays near 2
        and capture-pane returns only the current viewport (~41 lines) regardless
        of the -S prefix. In current opencode releases this request is therefore
        a no-op — extraction relies on the within-viewport fallback at
        extract_last_message_from_script (see the ``first_indent`` branch).

        The value is retained as belt-and-braces in case opencode ever switches
        out of alt-screen mode (at which point the 2000-line capture would start
        providing the extra scrollback the extraction loop originally expected).
        """
        return 2000

    async def initialize(self) -> bool:
        """Start the OpenCode TUI and wait for the idle splash frame.

        Steps:
        1. Wait for the shell prompt in the tmux window.
        2. Probe the CLI and launch its supported interactive interface.
        3. Wait for IDLE or COMPLETED with a 120s timeout to cover the first-run
           ``npm install @opencode-ai/plugin`` blocking period.

        Returns:
            True if initialization completed successfully.

        Raises:
            TimeoutError: If shell or OpenCode doesn't reach IDLE/COMPLETED in time.
        """
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        self._cli_major = await asyncio.to_thread(detect_opencode_major)
        self._shell_baseline = await asyncio.to_thread(
            get_backend().get_pane_current_command, self.session_name, self.window_name
        )
        command = self._build_launch_command()
        get_backend().send_keys(self.session_name, self.window_name, command)

        # 120s covers first-run npm install (5–30s) and concurrent multi-agent launches.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
            on_poll=self._check_startup,
        ):
            raise TimeoutError("OpenCode CLI initialization timed out after 120 seconds")

        await self._check_startup()
        self._initialized = True
        return True

    def _exited_to_shell(self) -> bool:
        return bool(
            self._shell_baseline
            and get_backend().get_pane_current_command(self.session_name, self.window_name)
            == self._shell_baseline
        )

    async def _check_startup(self) -> None:
        output = await asyncio.to_thread(
            get_backend().get_history, self.session_name, self.window_name
        )
        clean = strip_terminal_escapes(output or "")
        if await asyncio.to_thread(self._exited_to_shell) or self._startup_error(clean):
            raise ProviderExitedError(
                "OpenCode failed during initialization. Check the executable on PATH, "
                "agent installation, model credentials and configuration. Pane tail: "
                + " | ".join(clean.splitlines()[-8:])
            )

    @staticmethod
    def _startup_error(output: str) -> bool:
        return bool(
            re.search(
                r"(?im)^\s*(?:ERROR\s*$|Error:|Unrecognized flag:|Unknown (?:argument|option):|"
                r"USAGE\s*$|Usage:\s+opencode|opencode: command not found)",
                output,
            )
        )

    def _build_launch_command(self) -> str:
        """Build the inline-env opencode launch command string."""
        env_pairs = [
            f"OPENCODE_CONFIG={shlex.quote(str(OPENCODE_CONFIG_FILE))}",
            f"OPENCODE_CONFIG_DIR={shlex.quote(str(OPENCODE_CONFIG_DIR))}",
            "OPENCODE_DISABLE_AUTOUPDATE=1",
            "OPENCODE_DISABLE_MOUSE=1",
            "OPENCODE_DISABLE_TERMINAL_TITLE=1",
            "OPENCODE_CLIENT=cao",
            "TERM=xterm-256color",
        ]
        cmd_parts = ["opencode"]
        if self._cli_major == 2:
            # A shared service would inherit another worker's CAO_TERMINAL_ID
            # and configuration. Keep MCP subprocesses local to this worker.
            cmd_parts += ["mini", "--standalone"]
            ui = {
                "mouse": False,
                "mini": {
                    "mono": False,
                    "footer": "show",
                    "turn_summary": "show",
                    "thinking": "hide",
                    "tools": "hide",
                    "shell_output": "hide",
                },
                "keybinds": {"command.palette.show": "ctrl+p", "session.interrupt": "escape"},
            }
            env_pairs.append("OPENCODE_CLI_CONFIG_CONTENT=" + shlex.quote(json.dumps(ui)))
        if self._agent_profile:
            cmd_parts += ["--agent", self._agent_profile]
        if self._model:
            cmd_parts += ["--model", self._model]
        # env vars are shell words; join cmd parts with shlex for proper quoting
        return " ".join(env_pairs) + " " + shlex.join(cmd_parts)

    def get_status(self, output: str) -> TerminalStatus:
        """Detect current TUI state from the StatusMonitor buffer string.

        Priority order:
        1. WAITING_USER_ANSWER — permission dialog heading present, no idle footer after it
        2. PROCESSING — ``esc interrupt`` footer; line-level guard prevents stale-buffer
           false positives (lesson #16)
        3. COMPLETED — last full ``▣…·…·…Ns`` marker present, idle footer after it,
           no later ``▣`` token (would indicate a new incomplete turn)
        4. IDLE — idle footer present, no ``esc interrupt`` anywhere
        5. UNKNOWN — fallback

        Args:
            output: StatusMonitor buffer string for this terminal.

        Returns:
            Current TerminalStatus.
        """
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status(output)
        if native is not None:
            return native

        # herdr never pushes a buffer (pipe_pane is a no-op there); read live
        # pane content instead of falling through to "no output" on every call.
        output = self._resolve_buffer(output)
        if self._initialized and self._exited_to_shell():
            return TerminalStatus.ERROR
        if not output:
            return TerminalStatus.UNKNOWN

        clean = re.sub(ANSI_CODE_PATTERN, "", output)
        if not self._initialized and self._startup_error(clean):
            return TerminalStatus.ERROR
        if self._cli_major == 2:
            return self._mini_status(strip_terminal_escapes(output))

        # ── 1. WAITING_USER_ANSWER ───────────────────────────────────────────
        perm_matches = list(re.finditer(PERMISSION_PROMPT_PATTERN, clean))
        if perm_matches:
            last_perm_end = perm_matches[-1].end()
            # Permission dialog replaces the normal footer; if idle footer still
            # appears *after* the dialog the user already dismissed it.
            if not re.search(IDLE_FOOTER_PATTERN, clean[last_perm_end:]):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 2. PROCESSING ───────────────────────────────────────────────────
        # Line-level position guard: during normal processing, ``esc interrupt``
        # and ``ctrl+p commands`` share the same footer line.  In the stale case
        # (alt-screen remnant), ``esc interrupt`` is on an earlier line and the
        # new idle footer appears on a *later* line — that later-line presence
        # means processing has ended.
        lines = clean.split("\n")
        last_esc_line = -1
        for i, line in enumerate(lines):
            if re.search(PROCESSING_FOOTER_PATTERN, line):
                last_esc_line = i

        esc_is_stale = False
        if last_esc_line >= 0:
            later = lines[last_esc_line + 1 :]
            has_idle_later = any(re.search(IDLE_FOOTER_PATTERN, l) for l in later)
            has_completion_later = any(re.search(COMPLETION_MARKER_PATTERN, l) for l in later)
            if not has_idle_later and not has_completion_later:
                return TerminalStatus.PROCESSING
            # Guard fired: esc interrupt is a stale alt-screen remnant.
            esc_is_stale = True

        # ── 3. COMPLETED ─────────────────────────────────────────────────────
        # Requires the last full completion marker (with duration) followed by the
        # idle footer and no subsequent ``▣`` token (which would indicate a new
        # incomplete turn visible in the scrollback).
        completion_matches = list(re.finditer(COMPLETION_MARKER_PATTERN, clean))
        if completion_matches:
            last_end = completion_matches[-1].end()
            after = clean[last_end:]
            if re.search(IDLE_FOOTER_PATTERN, after) and not re.search(r"▣", after):
                return TerminalStatus.COMPLETED

        # ── 4. IDLE ──────────────────────────────────────────────────────────
        # Allow IDLE when either no esc interrupt is present OR it was flagged stale
        # (position guard fired in step 2 above).
        if re.search(IDLE_FOOTER_PATTERN, clean) and (
            not re.search(PROCESSING_FOOTER_PATTERN, clean) or esc_is_stale
        ):
            return TerminalStatus.IDLE

        # ── 5. UNKNOWN (fallback) ─────────────────────────────────────────────
        return TerminalStatus.UNKNOWN

    # ── Screen-based detection (pyte) ────────────────────────────────────────

    supports_screen_detection = True

    def _mini_summary_pattern(self) -> str:
        # Mini writes the turn summary as a plain line, without v1's ▣.
        agent = re.escape(self._agent_profile) if self._agent_profile else r"[^·\n]+"
        return rf"(?m)^\s*{agent}\s+·\s+[^·\n]+\s+·\s+(?:\d+m\s+)?\d+(?:\.\d+)?s\s*$"

    def record_dispatched_message(self, message: str) -> None:
        """Keep the prompt boundary for Mini's otherwise unlabelled response.

        Mini prefixes only the first user line with ›. A blank line inside a
        multiline task therefore cannot safely delimit its echoed prompt.
        """
        self._dispatched_message = message

    def _mini_status(self, clean: str) -> TerminalStatus:
        if not self._initialized and self._startup_error(clean):
            return TerminalStatus.ERROR
        # Only the most recent footer can describe the current state. Mini
        # keeps completed turns in normal terminal scrollback.
        footers = list(re.finditer(r"(?m)^.*ctrl\+p\s+menu.*$", clean))
        footer = footers[-1] if footers else None
        permissions = list(
            re.finditer(
                r"(?m)^\s*△\s+(?:Permission(?: required)?|Always allow|Reject(?: permission)?)\s*$",
                clean,
            )
        )
        if permissions and (footer is None or permissions[-1].start() > footer.end()):
            return TerminalStatus.WAITING_USER_ANSWER
        forms = list(re.finditer(r"(?m)^\s*◆\s+[^\n]+", clean))
        if forms and (footer is None or forms[-1].start() > footer.end()):
            if re.search(r"\besc dismiss\b", clean[forms[-1].end() :]):
                return TerminalStatus.WAITING_USER_ANSWER
        if footer is None:
            return TerminalStatus.UNKNOWN
        # A growing transcript is not a fresh idle frame until a new footer
        # has been painted after it.
        if clean[footer.end() :].strip():
            return TerminalStatus.UNKNOWN
        if re.search(r"\besc\s+(?:interrupt|stop)\b", footer.group()):
            return TerminalStatus.PROCESSING
        summaries = list(re.finditer(self._mini_summary_pattern(), clean[: footer.start()]))
        if summaries and not re.search(r"(?m)^\s*› ", clean[summaries[-1].end() :]):
            return TerminalStatus.COMPLETED
        return TerminalStatus.IDLE

    def _extract_mini_response(self, clean: str) -> str:
        if not self._dispatched_message:
            # Mini labels only the first user line. Without the dispatched
            # prompt, blank lines cannot distinguish its body from the answer.
            raise ValueError(
                "OpenCode Mini dispatched task is unavailable; cannot validate response"
            )
        summaries = list(re.finditer(self._mini_summary_pattern(), clean))
        if not summaries:
            raise ValueError("No OpenCode Mini completion summary found")
        end = summaries[-1].start()
        previous = summaries[-2].end() if len(summaries) > 1 else 0
        turn = clean[previous:end]
        users = list(re.finditer(r"(?m)^\s*› [^\n]*(?:\n|$)", turn))
        if not users:
            raise ValueError("No user message found before OpenCode Mini summary")
        expected = re.sub(r"\s", "", self._dispatched_message)
        if not expected:
            raise ValueError("Empty OpenCode Mini dispatched task")
        for user in reversed(users):
            start = turn.index("›", user.start()) + 2
            consumed = 0
            for offset, char in enumerate(turn[start:]):
                if char.isspace():
                    continue
                if char != expected[consumed]:
                    break
                consumed += 1
                if consumed == len(expected):
                    tail = turn[start + offset + 1 :]
                    if not re.match(r"[ \t]*\n[ \t]*\n", tail):
                        break  # A prompt prefix is not a validated full echo.
                    response = tail.strip()
                    if response:
                        return response
                    raise ValueError("Empty OpenCode Mini response")
        raise ValueError("OpenCode Mini prompt echo does not match the dispatched task")

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-composited viewport (escape-free rows).

        The pyte screen represents the current terminal viewport without any
        ANSI escape sequences, so the status markers are clean text — no
        buffer-eviction or escape-stripping issues.

        Precedence mirrors ``get_status``:
        1. WAITING_USER_ANSWER — permission dialog heading
        2. PROCESSING — ``esc interrupt`` footer
        3. COMPLETED — completion marker followed by idle footer
        4. IDLE — idle footer present
        5. UNKNOWN — fallback
        """
        if self._initialized and self._exited_to_shell():
            return TerminalStatus.ERROR
        if not self._initialized and self._startup_error("\n".join(screen_lines)):
            return TerminalStatus.ERROR
        if self._cli_major == 2:
            return self._mini_status("\n".join(screen_lines))
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN

        # Join with newlines so multiline patterns work.
        joined = "\n".join(rows)

        # ── 1. WAITING_USER_ANSWER ───────────────────────────────────────
        if re.search(PERMISSION_PROMPT_PATTERN, joined):
            # Permission dialog replaces the normal footer; if idle footer
            # also appears the user already dismissed it.
            if not re.search(IDLE_FOOTER_PATTERN, joined):
                return TerminalStatus.WAITING_USER_ANSWER

        # ── 2. PROCESSING ───────────────────────────────────────────────
        last_esc_line = -1
        for i, row in enumerate(rows):
            if re.search(PROCESSING_FOOTER_PATTERN, row):
                last_esc_line = i

        esc_is_stale = False
        if last_esc_line >= 0:
            later = rows[last_esc_line + 1 :]
            has_idle_later = any(re.search(IDLE_FOOTER_PATTERN, row) for row in later)
            has_completion_later = any(re.search(COMPLETION_MARKER_PATTERN, row) for row in later)
            if not has_idle_later and not has_completion_later:
                return TerminalStatus.PROCESSING
            esc_is_stale = True

        # ── 3. COMPLETED ────────────────────────────────────────────────
        completion_matches = list(re.finditer(COMPLETION_MARKER_PATTERN, joined))
        if completion_matches:
            last_end = completion_matches[-1].end()
            after = joined[last_end:]
            if re.search(IDLE_FOOTER_PATTERN, after) and not re.search(r"▣", after):
                return TerminalStatus.COMPLETED

        # ── 4. IDLE ─────────────────────────────────────────────────────
        if re.search(IDLE_FOOTER_PATTERN, joined) and (
            not re.search(PROCESSING_FOOTER_PATTERN, joined) or esc_is_stale
        ):
            return TerminalStatus.IDLE

        # ── 5. UNKNOWN (fallback) ───────────────────────────────────────
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's response from the TUI scrollback.

        Algorithm:
        1. Strip ANSI codes.
        2. Find the last ``USER_MESSAGE_PATTERN`` (last user turn start).
        3. Find the first full ``COMPLETION_MARKER_PATTERN`` at or after that position.
        4. Extract text between end-of-user-block and start-of-completion-marker.
        5. Strip ``Thinking: …`` preamble lines.
        6. Dedent the 5-space agent indent.
        7. Clean control chars and trailing whitespace.

        Args:
            script_output: Raw terminal output captured from the tmux pane.

        Returns:
            Extracted agent response text.

        Raises:
            ValueError: If no user message or completion marker is found.
        """
        if self._cli_major == 2:
            try:
                return self._extract_mini_response(strip_terminal_escapes(script_output))
            except ValueError as exc:
                # get_output must never publish a Mini transcript as an answer
                # when its prompt/response boundary could not be validated.
                raise OutputExtractionRejected(str(exc)) from exc
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Find the last FULL completion marker to anchor the turn boundary.
        all_completions = list(re.finditer(COMPLETION_MARKER_PATTERN, clean))
        if not all_completions:
            raise ValueError("No completion marker found after last user message")

        last_completion = all_completions[-1]

        # Find the last user-message bar (┃  ) that appears BEFORE the completion marker.
        # NOTE: intentionally NOT using the module-level USER_MESSAGE_PATTERN (r"^┃\s{2}") here.
        # TUI lines carry variable leading spaces (e.g. "  ┃  say hello"), so the ^-anchored
        # form only matches when ┃ is the very first character.  The unanchored r"┃\s{2}" finds
        # the bar regardless of indentation.  USER_MESSAGE_PATTERN is correct for whole-line
        # MULTILINE matches; this unanchored variant is correct for substring position-finding.
        # Searching only in the slice before the completion start avoids matching
        # the bottom input-box header (┃  <agent> · <model>) that follows the turn.
        user_matches = list(re.finditer(r"┃\s{2}", clean[: last_completion.start()]))
        if user_matches:
            # Normal path: anchor on the last visible user message bar.
            # Alt-screen redraws each turn in place; earlier turns are not present in the
            # visible frame, so the last ┃  bar is unambiguously the current turn's.
            response_start = user_matches[-1].end()  # after the ┃  bar
        else:
            # Fallback: user message has scrolled off the top of the 41-line TUI viewport
            # (history_size ≈ 2; alt-screen rendering does not accumulate scrollback).
            # Find the first 5-space-indented agent line as the left boundary instead.
            first_indent = re.search(r"^     \S", clean[: last_completion.start()], re.MULTILINE)
            if not first_indent:
                raise ValueError("No user message found in OpenCode output")
            response_start = first_indent.start()

        response_end = last_completion.start()  # before the ▣ completion marker

        raw_response = clean[response_start:response_end]

        # Skip user-message lines (those still using ┃ indent) to find agent block.
        # When the fallback path was taken there are no ┃ lines; the loop only strips
        # leading blank lines in that case.
        response_lines = raw_response.split("\n")
        agent_lines = []
        past_user_block = False
        for line in response_lines:
            if re.match(r"^\s*┃", line):
                # Still inside the user message block
                past_user_block = False
                continue
            if not past_user_block and not line.strip():
                # Blank line between user block and agent block
                continue
            past_user_block = True
            agent_lines.append(line)

        # Strip Thinking: preamble — remove lines starting with "Thinking:"
        # until the first non-Thinking non-blank line
        stripped_lines = []
        in_thinking = False
        for line in agent_lines:
            if re.match(r"^\s*Thinking:", line):
                in_thinking = True
                continue
            if in_thinking and not line.strip():
                # Blank line after thinking block — skip it
                continue
            in_thinking = False
            stripped_lines.append(line)

        # Dedent 5-space agent indent
        dedented = []
        for line in stripped_lines:
            if line.startswith("     "):
                dedented.append(line[5:])
            else:
                dedented.append(line)

        # Clean control chars and trailing whitespace
        result = "\n".join(dedented).strip()
        result = re.sub(r"[\x00-\x1f\x7f-\x9f]", "", result)
        result = re.sub(r"[ \t]+$", "", result, flags=re.MULTILINE)

        if not result:
            raise ValueError("Empty OpenCode response — no content found after extraction")

        return result

    def get_idle_pattern_for_log(self) -> str:
        """Return pattern matching the idle footer in log file output."""
        return r"ctrl\+p\s+menu" if self._cli_major == 2 else IDLE_FOOTER_PATTERN

    def exit_cli(self) -> str:
        """Return the command to exit the OpenCode TUI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up OpenCode provider state."""
        self._initialized = False
        self._shell_baseline = None
        self._dispatched_message = None
