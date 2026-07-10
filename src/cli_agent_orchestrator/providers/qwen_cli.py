"""Qwen Code (``qwen``) provider implementation.

Qwen Code (https://github.com/QwenLM/qwen-code) is Alibaba's terminal-native AI
coding agent — a fork of Google's Gemini CLI. The CLI is invoked via the
``qwen`` binary (``npm install -g @qwen-code/qwen-code``).

Key characteristics (observed on ``qwen`` 0.19.x, full-screen Ink TUI):

- Command: ``qwen --approval-mode yolo`` to auto-approve tool calls so
  orchestrated (handoff / assign) flows do not block on per-tool approval
  prompts, ``--model "<name>"`` to pick a model, ``--append-system-prompt
  "<text>"`` to layer the agent profile's role/instructions on top of qwen's
  built-in agent prompt, and ``--mcp-config <file>`` to point at a per-terminal
  MCP server config.
- Idle prompt: an input box delimited by full-width ``─`` (U+2500) rule lines
  with the placeholder ``Type your message or @path/to/file``, and a footer
  showing the approval mode (``YOLO mode (shift + tab to cycle)``) and the
  model name (``➜ <dir> · <model>``).
- Processing: a spinner line containing ``esc to cancel`` (with an elapsed
  timer, e.g. ``.. Figuring out ... (16.5s · esc to cancel)``) is rendered
  above the input box. The ``esc to cancel`` marker is the reliable,
  render-stable signal (it survives ``strip_terminal_escapes``); the witty
  spinner phrase is not.
- Completed: assistant / tool output is rendered with a filled bullet
  (``● <text>``) between the echoed ``> <query>`` line and the input box, and
  the spinner disappears.
- MCP config: a per-terminal JSON file (``{"mcpServers": {...}}``) written to a
  temp path and passed via ``--mcp-config``. ``CAO_TERMINAL_ID`` is injected
  into each server's ``env`` so cao-mcp-server can resolve the terminal. Using a
  per-instance file (rather than mutating the shared ``~/.qwen/settings.json``)
  avoids cross-terminal write races.
- Auth: user-managed. ``qwen`` reads OpenAI-compatible credentials from the
  ambient environment (``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` /
  ``OPENAI_MODEL``, DashScope-compatible endpoints) or ``qwen-oauth``; the
  provider never handles credentials.
- Exit: ``/quit`` (slash command).

Status detection mirrors the footer-anchored approach of the sibling
Antigravity CLI provider (both are Gemini-CLI-derived Ink TUIs): the presence of
``esc to cancel`` means PROCESSING; a ready input box (approval-mode footer /
placeholder) means IDLE / COMPLETED (split on a turn counter, since the TUI
looks identical in both states). Because the footer redraws in place, the raw
pipe-pane stream keeps a stale ``esc to cancel`` after a turn ends, so the
provider opts in to pyte rendered-screen detection (see
``supports_screen_detection`` / ``get_status_from_screen``).
"""

import json
import logging
import os
import re
import shlex
import shutil
import tempfile
import time
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for Qwen Code provider-specific errors."""

    pass


# =============================================================================
# Regex patterns for Qwen Code (qwen) output analysis
# =============================================================================

# PROCESSING signal. ``qwen`` renders "esc to cancel" on the spinner line every
# frame the agent is working on a turn; it disappears once the turn completes.
# This is the reliable, render-stable processing marker (it survives
# ``strip_terminal_escapes``); the witty spinner phrase before it varies.
PROCESSING_FOOTER_PATTERN = r"esc to cancel"

# IDLE / COMPLETED signal: the ready input box. qwen always renders the
# approval-mode footer ("... (shift + tab to cycle)") and the empty-input
# placeholder ("Type your message or @path/to/file") whenever it is ready for
# input; older/newer builds may also show "? for shortcuts". Any of these means
# the input box is present (PROCESSING is checked first, so a live spinner is
# already excluded by the time this runs).
IDLE_FOOTER_PATTERN = r"(?:shift \+ tab to cycle|Type your message or @|\?\s*for shortcuts)"
# Same hint for log-file pre-checks (no ANSI involved).
IDLE_FOOTER_PATTERN_LOG = IDLE_FOOTER_PATTERN

# Spinner line: a leading glyph (dots / braille) followed by a witty phrase and
# the "(Ns · esc to cancel)" timer. Secondary processing cue; the phrase varies
# so we anchor on the timer + cancel hint.
PROCESSING_SPINNER_PATTERN = r"\(\s*[\d.]+s\s*[·.]\s*esc to cancel\s*\)"

# Echoed user query line: "> <text>" (non-empty after the prompt char).
# Start-of-line anchored so it does not match an empty prompt.
QUERY_PROMPT_PATTERN = r"^\s*>\s+\S"

# Assistant / tool output bullet ("● <text>"). In qwen this marks response
# content (unlike a plain chrome glyph) — the leading bullet is stripped during
# extraction and the text kept.
RESPONSE_BULLET_PATTERN = r"^\s*●\s?"

# Full-width horizontal rule (U+2500) delimiting the input box / transcript
# sections. Anchored to a full line; tolerates surrounding whitespace.
SEPARATOR_PATTERN = r"^\s*─{20,}\s*$"

# Interactive prompts that block on user input (tool-approval dialogs, pickers).
# With --approval-mode yolo these are rare, but we still classify them as
# WAITING_USER_ANSWER so orchestrated input is not mistaken for the answer to
# such a prompt. Phrasing verified against the qwen binary.
WAITING_USER_ANSWER_PATTERN = (
    r"(?:Allow execution)"
    r"|(?:Apply this change)"
    r"|(?:Do you want to proceed)"
    r"|(?:Yes, allow (?:once|always))"
    r"|(?:Waiting for user confirmation)"
    r"|(?:↑/↓\s*(?:to )?[Nn]avigate)"
    r"|(?:\[\s*y\s*/\s*n\s*\])"
)

# First-run dialogs qwen may show before the input box is ready (theme picker,
# folder-trust gate). Dismissed by accepting the pre-selected option with Enter
# in _handle_startup_dialog(). With OpenAI-compatible credentials configured qwen
# skips the auth dialog, so this is typically a no-op.
STARTUP_DIALOG_PATTERN = (
    r"(?:Select Theme|Choose.*theme|How would you like to (?:theme|authenticate))"
    r"|(?:trust (?:this )?folder|Do you trust the files in this folder)"
    r"|(?:Get started|Sign in with)"
)

# Hard-error patterns surfaced by a crashed binary. A transient "✕ [API Error
# ...]" turn is NOT treated as a status error: qwen returns to the ready input
# box (retryable), so it is reported as COMPLETED and the error text is
# extractable as the turn outcome.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|panic:|qwen: .*(?:error|failed)|Traceback \(most recent call last\):)"
)

# Tail window (chars) scanned for the footer markers. The footer + spinner are
# rendered in the last few hundred bytes of every TUI frame; 2KB is well within
# the StatusMonitor's rolling buffer and avoids flipping to IDLE mid-response
# when a long answer scrolls the older spinner out of the window.
FOOTER_TAIL_WINDOW = 2048

# Chrome lines filtered out of the extracted response.
_BANNER_PATTERN = r"(?:Qwen Code \(?v?\d|▀|▄|█|╚|╝|╔|╗|║)"
_TIP_PATTERN = r"^\s*Tips?:"
_FOOTER_LINE_PATTERN = r"(?:\? for shortcuts|esc to cancel|shift \+ tab to cycle|Type your message or @|YOLO mode|Auto mode|plan mode|Accepting edits|➜ )"

# qwen-code ships a native ``send_message`` tool (its team / background-task
# messaging feature — class ``_SendMessageTool``, described as "Send a message
# to a teammate … or to a running background task"). Its *bare* name collides
# with cao-mcp-server's ``send_message``, which qwen exposes under the prefixed
# name ``mcp__cao-mcp-server__send_message``. When a CAO worker is told to
# "send_message" its result back, the model matches the shorter native tool and
# calls it → "No active team and no task_id provided" → the assign/handoff
# callback never routes back to the supervisor. CAO orchestration never uses
# qwen-code's native team messaging (it routes through cao-mcp-server), so we
# drop the native tool via ``--exclude-tools`` and leave the MCP tool as the
# only send-message-shaped tool the model can pick. See issue #376.
QWEN_CONFLICTING_NATIVE_TOOL = "send_message"


class QwenCliProvider(BaseProvider):
    """Provider for Qwen Code (``qwen``).

    Manages the lifecycle of a ``qwen`` REPL session inside a tmux window:
    initialization (with profile system prompt, model, and MCP config), status
    detection, response extraction, and cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance.
        session_name: Name of the tmux session containing this terminal.
        window_name: Name of the tmux window for this terminal.
        _agent_profile: Optional CAO agent profile name to load.
        _model: Optional model override forwarded as ``--model``.
    """

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
        """Initialize the Qwen Code provider.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional CAO agent profile name.
            allowed_tools: Optional list of CAO tool names the agent may use.
                When restricted (not wildcard), the security prompt is appended
                to the injected system prompt for soft enforcement.
            model: Optional model override forwarded as ``--model``. The
                profile's ``model`` field takes precedence when set.
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model
        # Per-terminal temp files (the --mcp-config JSON), removed on cleanup().
        self._tmp_paths: list[Path] = []
        # Turn counter. get_status() returns IDLE while _turns == 0 (fresh
        # spawn / post-init, no task delivered yet) and COMPLETED once at least
        # one turn has been delivered and the agent is back to a ready input box.
        # The ready TUI looks identical in both states, so the counter is the
        # authoritative IDLE-vs-COMPLETED signal. Incremented by
        # mark_input_received(), which the terminal service calls after every
        # send_input(). This keeps the handoff/assign "wait for IDLE before
        # sending the task" contract working right after init.
        self._turns: int = 0

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """qwen's approval dialogs / pickers consume pasted text as the answer.

        Even with ``--approval-mode yolo`` some interactive prompts can surface;
        when one is up, an orchestrated assign/handoff message pasted into the
        input would be read as the prompt's answer. Opting in makes the terminal
        service hold orchestrated delivery until the prompt clears, while still
        allowing explicit user-prompt answers.
        """
        return True

    # ------------------------------------------------------------------ #
    # Launch
    # ------------------------------------------------------------------ #

    def _write_mcp_config(self, mcp_servers: dict) -> Path:
        """Write a per-terminal MCP config file and return its path.

        qwen's ``--mcp-config`` accepts a path to a JSON file of shape
        ``{"mcpServers": {...}}``. We write one file per terminal (tracked in
        ``_tmp_paths`` and removed on cleanup) so concurrent terminals never
        race on a shared config. ``CAO_TERMINAL_ID`` is forwarded into each
        server's ``env`` so cao-mcp-server can resolve the current terminal for
        handoff / assign.
        """
        servers: dict = {}
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                cfg = dict(server_config)
            else:
                cfg = server_config.model_dump(exclude_none=True)
            entry = {
                "command": cfg.get("command", ""),
                "args": cfg.get("args", []),
            }
            env = dict(cfg.get("env", {}))
            env["CAO_TERMINAL_ID"] = self.terminal_id
            entry["env"] = env
            servers[server_name] = entry

        fd, path_str = tempfile.mkstemp(prefix="cao_qwen_mcp_", suffix=".json")
        with os.fdopen(fd, "w") as f:
            json.dump({"mcpServers": servers}, f, indent=2)
        path = Path(path_str)
        self._tmp_paths.append(path)
        return path

    def _build_qwen_command(self) -> str:
        """Build the ``qwen`` launch command.

        Structure::

            qwen --approval-mode yolo [--model "<model>"] \
                 [--append-system-prompt "<system prompt>"] \
                 [--mcp-config "<file>"]

        ``--approval-mode yolo`` auto-approves tool calls (required for
        unattended orchestration). ``--model`` selects the model. The agent
        profile's system prompt (+ skill catalog + security prompt when tool-
        restricted) is layered on top of qwen's built-in agent prompt via
        ``--append-system-prompt``. MCP servers are written to a per-terminal
        file referenced by ``--mcp-config``.

        Returns a shell-escaped command string for ``send_keys``.
        """
        binary = shutil.which("qwen")
        if not binary:
            raise ProviderError(
                "Qwen Code not found: 'qwen' is not on $PATH. "
                "Install via: npm install -g @qwen-code/qwen-code"
            )

        # Drop qwen-code's native ``send_message`` tool so it can't shadow
        # cao-mcp-server's ``send_message`` in orchestration callbacks (#376).
        command_parts = [
            "qwen",
            "--approval-mode",
            "yolo",
            "--exclude-tools",
            QWEN_CONFLICTING_NATIVE_TOOL,
        ]

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as exc:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

        # Model: profile.model wins over the constructor-provided override.
        model = self._model
        if profile is not None and profile.model:
            model = profile.model
        if model:
            command_parts.extend(["--model", model])

        # System prompt injection via --append-system-prompt (layers the CAO
        # role on top of qwen's own agent prompt; interactive-safe).
        if profile is not None:
            system_prompt = profile.system_prompt or ""
            system_prompt = self._apply_skill_prompt(system_prompt)
            # Soft tool restriction: when the profile is not allowed every tool
            # (e.g. the read-only reviewer), append the security prompt. qwen
            # honors a clear instruction not to use disallowed tools.
            if self._allowed_tools and "*" not in self._allowed_tools:
                system_prompt = (
                    f"{system_prompt}\n\n{SECURITY_PROMPT}" if system_prompt else SECURITY_PROMPT
                )
            if system_prompt:
                command_parts.extend(["--append-system-prompt", system_prompt])

            # MCP servers (cao-mcp-server etc.) → per-terminal --mcp-config file.
            if profile.mcpServers:
                mcp_path = self._write_mcp_config(profile.mcpServers)
                command_parts.extend(["--mcp-config", str(mcp_path)])

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Initialize the Qwen Code provider by starting ``qwen``.

        1. Wait for the shell prompt in the tmux window.
        2. Send the ``qwen`` command (model + system prompt + MCP config).
        3. Dismiss any first-run startup dialog (theme / trust).
        4. Wait for the agent to reach IDLE / COMPLETED.

        Raises:
            TimeoutError: If the shell or qwen initialization times out.
        """
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_qwen_command()

        # Arm the StatusMonitor stickiness gate so the launch drives a fresh
        # PROCESSING transition past any stale ready latch. Imported lazily to
        # avoid a circular import (status_monitor imports provider_manager).
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Dismiss the first-run theme / trust dialog if qwen shows one. With
        # OpenAI-compatible credentials in the environment qwen skips the auth
        # dialog and lands directly on the input prompt, but a fresh install may
        # still show a theme picker or a folder-trust gate that blocks IDLE.
        self._handle_startup_dialog()

        # qwen startup + first MCP connection (cao-mcp-server is fetched via uvx
        # from git on first use) can take a while.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=180.0,
        ):
            raise TimeoutError("Qwen Code initialization timed out after 180 seconds")

        self._initialized = True
        return True

    def _handle_startup_dialog(self, timeout: Optional[float] = None) -> None:
        """Dismiss qwen's blocking first-run dialogs (theme / folder-trust).

        Mirrors AntigravityCliProvider._handle_startup_dialog / KimiCliProvider.
        Polls the pane and, if a theme picker or a folder-trust gate is showing,
        accepts the pre-selected option with Enter. Both are one-shot on first
        install; once qwen is at its ready input box the loop returns. When
        credentials are already configured this is typically a no-op.
        """
        if timeout is None:
            timeout = get_server_settings()["startup_prompt_handler_timeout"]
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        start_time = time.time()
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                clean = strip_terminal_escapes(output)
                # Ready input box with no dialog pending → done.
                if re.search(IDLE_FOOTER_PATTERN, clean) and not re.search(
                    STARTUP_DIALOG_PATTERN, clean
                ):
                    return
                if re.search(STARTUP_DIALOG_PATTERN, clean):
                    logger.info("Qwen Code startup dialog detected, accepting default")
                    status_monitor.notify_input_sent(self.terminal_id)
                    get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                    time.sleep(1.0)
                    continue
            time.sleep(1.0)

    # ------------------------------------------------------------------ #
    # Status detection
    # ------------------------------------------------------------------ #

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        """Detect qwen status from the terminal output buffer.

        Priority (matches the checks below in order):
          1. Empty → UNKNOWN
          2. WAITING_USER_ANSWER — an interactive approval / picker prompt
             (takes precedence over the processing spinner)
          3. PROCESSING — "esc to cancel" (or a spinner timer) in the tail
          4. IDLE / COMPLETED — a ready input box (IDLE pre-first-turn,
             COMPLETED after)
          5. ERROR — matched hard-error pattern
          6. UNKNOWN — nothing matched

        NOTE: the raw pipe-pane stream retains a stale "esc to cancel" after a
        turn ends (the footer/spinner redraws in place), so this raw-stream path
        can report a false PROCESSING once a turn is done. The authoritative
        detector is get_status_from_screen() on a pyte-composited viewport; this
        method is the fallback when CAO_PYTE_STATUS is off.
        """
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        clean = strip_terminal_escapes(output)
        tail = clean[-FOOTER_TAIL_WINDOW:]

        # Interactive prompt blocking on user input takes precedence over a
        # plain processing state.
        if re.search(WAITING_USER_ANSWER_PATTERN, tail):
            return TerminalStatus.WAITING_USER_ANSWER

        if re.search(PROCESSING_FOOTER_PATTERN, tail) or any(
            re.search(PROCESSING_SPINNER_PATTERN, line) for line in tail.splitlines()
        ):
            return TerminalStatus.PROCESSING

        # IDLE / COMPLETED: ready input box present. Fresh spawn (no delivered
        # turn) is IDLE; a finished turn is COMPLETED.
        if re.search(IDLE_FOOTER_PATTERN, tail):
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        if re.search(ERROR_PATTERN, clean, re.MULTILINE):
            return TerminalStatus.ERROR

        return TerminalStatus.UNKNOWN

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS).
    # The raw-stream get_status() above is unreliable for qwen: when the spinner
    # ("esc to cancel") is redrawn away at turn end, the append-only pipe-pane
    # log still contains it after strip_terminal_escapes(), so the stale marker
    # pins the terminal to PROCESSING forever. A composited pyte viewport
    # resolves the in-place redraw, leaving only the live frame.
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect qwen status from a pyte-composited viewport (escape-free rows).

        Same precedence as get_status, but anchored on the rendered bottom
        region rather than the raw redraw stream. Because the viewport has every
        in-place redraw already resolved, a live "esc to cancel" appears only
        while the agent is actually working — eliminating the stale-spinner
        false PROCESSING the raw-stream path suffers from.

        The StatusMonitor only invokes this on settled / rising-edge frames, so
        the frame reflects a real end state, not a half-drawn one.
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN

        joined = "\n".join(rows)
        # The input box + footer live on the last rendered rows; the spinner
        # sits a few rows above the input box. A bottom window keeps stale
        # response text from matching while still covering the spinner.
        bottom_rows = rows[-15:]
        bottom = "\n".join(bottom_rows)

        if re.search(WAITING_USER_ANSWER_PATTERN, bottom):
            return TerminalStatus.WAITING_USER_ANSWER

        if re.search(PROCESSING_FOOTER_PATTERN, bottom) or any(
            re.search(PROCESSING_SPINNER_PATTERN, line) for line in bottom_rows
        ):
            return TerminalStatus.PROCESSING

        if re.search(IDLE_FOOTER_PATTERN, bottom):
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        if re.search(ERROR_PATTERN, joined, re.MULTILINE):
            return TerminalStatus.ERROR

        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        """Return the qwen IDLE footer pattern for log-file pre-checks."""
        return IDLE_FOOTER_PATTERN_LOG

    # ------------------------------------------------------------------ #
    # Response extraction
    # ------------------------------------------------------------------ #

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's last response from rendered terminal output.

        Layout of a completed turn (rendered)::

            > <user question>
              ● <assistant response line 1>
                <assistant response continuation>
            ─────────────────────────────  (input box top rule)
            *   Type your message or @path/to/file
            ─────────────────────────────  (input box bottom rule)
              ➜ <dir> · <model>
              YOLO mode (shift + tab to cycle)

        The response is the text between the last echoed ``> <query>`` line and
        the next full-width separator (the top of the input box). The assistant
        bullet (``●``) is stripped and its text kept; TUI chrome (banner,
        separators, footer, tips, spinner) is filtered out.

        Raises:
            ValueError: When no response boundary is detected.
        """
        clean = strip_terminal_escapes(script_output)
        lines = clean.split("\n")

        # Index of the last echoed user query line.
        last_query_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if re.search(QUERY_PROMPT_PATTERN, line):
                last_query_idx = i
        if last_query_idx is None:
            raise ValueError("No Qwen Code user query found - no '> <text>' line detected")

        # Response ends at the first separator after the query (input-box top).
        end_idx = len(lines)
        for i in range(last_query_idx + 1, len(lines)):
            if re.search(SEPARATOR_PATTERN, lines[i]):
                end_idx = i
                break

        def _is_chrome(text_line: str) -> bool:
            """True if the line is recognized TUI chrome (not response content)."""
            stripped_line = text_line.strip()
            return bool(
                re.search(SEPARATOR_PATTERN, text_line)
                or re.search(_FOOTER_LINE_PATTERN, stripped_line)
                or re.search(_TIP_PATTERN, stripped_line)
                or re.search(PROCESSING_SPINNER_PATTERN, stripped_line)
                or re.search(_BANNER_PATTERN, stripped_line)
            )

        body = lines[last_query_idx + 1 : end_idx]
        response_lines: list[str] = []
        for line in body:
            if not line.strip():
                continue
            if _is_chrome(line):
                continue
            # Strip the assistant/tool bullet ("● ") but keep the text.
            text = re.sub(RESPONSE_BULLET_PATTERN, "", line).strip()
            if text:
                response_lines.append(text)

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("Empty Qwen Code response - no content found after query")
        return response

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def exit_cli(self) -> str:
        """Get the command to exit qwen. ``/quit`` is the slash command."""
        return "/quit"

    def cleanup(self) -> None:
        """Remove per-terminal temp files (MCP config) and reset state."""
        for path in self._tmp_paths:
            try:
                path.unlink()
            except OSError:
                pass
        self._tmp_paths = []
        self._initialized = False

    def mark_input_received(self) -> None:
        """Record that a turn was delivered (IDLE → COMPLETED on next status)."""
        super().mark_input_received()
        self._turns += 1
