"""Qwen Code (``qwen``) provider implementation.

Qwen Code (https://github.com/QwenLM/qwen-code) is Alibaba's terminal-native AI
coding agent — a fork of Google's Gemini CLI. The CLI is invoked via the
``qwen`` binary (``npm install -g @qwen-code/qwen-code``).

Key characteristics (Qwen 0.19.x fixtures and a Qwen 0.28 smoke test):

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

import asyncio
import hashlib
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

from cli_agent_orchestrator.agent_plugins.mcp_delivery import with_plugin_mcp as _with_plugin_mcp
from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import CAO_HOME_DIR, SECURITY_PROMPT
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import (
    BaseProvider,
    OutputExtractionError,
    OutputExtractionRejected,
)
from cli_agent_orchestrator.providers.qwen_output import QwenOutput
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# The shell budget and readiness floor match the sibling Antigravity provider.
# Both dialog handling and readiness allow max(180s, provider_init_timeout),
# including profile overrides, for a cold Qwen/MCP start (possibly using uvx).
# The client init allowance already covers this three-phase startup shape.
SHELL_WAIT_TIMEOUT_S = 10.0
READY_TIMEOUT_S = 180.0


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
IDLE_FOOTER_PATTERN = (
    r"(?:shift \+ tab to cycle|Type your (?:message or @|request, or /)|\?\s*for shortcuts)"
)
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
_FINAL_BULLET_PATTERN = r"^\s*◆[\ufe0e\ufe0f]?\s+"
_THOUGHT_PATTERN = r"^\s*∴[\ufe0e\ufe0f]?\s+(?:Thought|Thinking)\b"

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
    r"(?:Select Theme|Choose.*theme|How would you like to theme)"
    r"|(?:trust (?:this )?folder|Do you trust the files in this folder)"
)
AUTH_PICKER_PATTERN = (
    r"(?s)Connect a Provider.*(?:Qwen Account with Subscription|Third-party Providers)"
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
_TOOL_CALL_PATTERN = r"^\s*●\s+\S+\s{2,}(?:[\w_-]+\s*\(|mcp__)"
_PICKER_OPTION_PATTERN = r"^\s*(?:●\s*)?\d+\.\s+(?:Yes|No|Allow|Deny)\b"

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
            model: Optional model override forwarded as ``--model``. An explicit
                launch model takes precedence over the profile default.
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._launch_profile: Optional[AgentProfile] = None
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
        self._output: Optional[QwenOutput] = None
        # Durable terminal identity lets a restarted CAO reconnect to the
        # still-running CLI's journal without restarting the Qwen conversation.
        key = hashlib.sha256(terminal_id.encode("utf-8")).hexdigest()
        self._output_path = CAO_HOME_DIR / "tmp" / "qwen" / key / "events.jsonl"
        if self._output_path.is_file():
            self._output = QwenOutput(self._output_path)

    @property
    def paste_enter_count(self) -> int:
        """One Enter submits Qwen's bracketed-paste input."""
        return 1

    @property
    def allow_raw_transcript_fallback(self) -> bool:
        return False  # A terminal pane can contain private reasoning.

    @classmethod
    def honors_model(
        cls,
        agent_profile: Optional[str],
        profile: Optional[AgentProfile],
        requested_model: Optional[str],
    ) -> bool:
        """Apply the explicit per-launch override before the profile default."""
        return True

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
            entry = {key: value for key, value in cfg.items() if value is not None}
            entry["args"] = cfg.get("args") or []
            env = dict(cfg.get("env") or {})
            env["CAO_TERMINAL_ID"] = self.terminal_id
            entry["env"] = env
            servers[server_name] = entry

        fd, path_str = tempfile.mkstemp(prefix="cao_qwen_mcp_", suffix=".json")
        path = Path(path_str)
        self._tmp_paths.append(path)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"mcpServers": servers}, f, indent=2)
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
        self._output_path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        # Building the command is a fresh CLI launch; restored providers only
        # read the existing journal and do not call this method.
        self._output_path.unlink(missing_ok=True)
        fd = os.open(self._output_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.close(fd)
        self._output = QwenOutput(self._output_path)
        command_parts.extend(["--json-file", str(self._output.path)])

        profile = None
        if self._agent_profile is not None:
            try:
                profile = _with_plugin_mcp(load_agent_profile(self._agent_profile), "qwen_cli")
            except Exception as exc:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

        # Explicit launch override wins over the profile default, as in other providers.
        self._launch_profile = profile
        model = self._model or (profile.model if profile is not None else None)
        if model:
            command_parts.extend(["--model", model])

        # System prompt injection via --append-system-prompt (layers the CAO
        # role on top of qwen's own agent prompt; interactive-safe).
        system_prompt = self._apply_skill_prompt((profile.system_prompt or "") if profile else "")
        # Advisory only: yolo does not enforce a restricted CAO policy.
        if self._allowed_tools is not None and "*" not in self._allowed_tools:
            system_prompt = (
                f"{system_prompt}\n\n{SECURITY_PROMPT}" if system_prompt else SECURITY_PROMPT
            )
            system_prompt += "\nAllowed CAO tools: " + ", ".join(self._allowed_tools)
        if system_prompt:
            command_parts.extend(["--append-system-prompt", system_prompt])

        # MCP servers (cao-mcp-server etc.) → per-terminal --mcp-config file.
        if profile is not None and profile.mcpServers:
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
        if not await wait_for_shell(self.terminal_id, timeout=SHELL_WAIT_TIMEOUT_S):
            raise TimeoutError(
                f"Shell initialization timed out after {SHELL_WAIT_TIMEOUT_S:g} seconds"
            )

        try:
            return await self._initialize_cli()
        except BaseException:
            self.cleanup()
            raise

    async def _initialize_cli(self) -> bool:
        command = await asyncio.to_thread(self._build_qwen_command)

        # Arm the StatusMonitor stickiness gate so the launch drives a fresh
        # PROCESSING transition past any stale ready latch. Imported lazily to
        # avoid a circular import (status_monitor imports provider_manager).
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        await asyncio.to_thread(
            get_backend().send_keys, self.session_name, self.window_name, command
        )

        # Dismiss the first-run theme / trust dialog if qwen shows one. With
        # OpenAI-compatible credentials in the environment qwen skips the auth
        # dialog and lands directly on the input prompt, but a fresh install may
        # still show a theme picker or a folder-trust gate that blocks IDLE.
        ready_timeout = max(READY_TIMEOUT_S, self.get_init_timeout(self._launch_profile))
        await self._handle_startup_dialog(outer_timeout=ready_timeout)

        # qwen startup + first MCP connection (cao-mcp-server is fetched via uvx
        # from git on first use) can take a while.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=ready_timeout,
        ):
            raise TimeoutError(
                f"Qwen Code initialization timed out after {ready_timeout:g} seconds"
            )

        self._initialized = True
        return True

    async def _handle_startup_dialog(
        self, idle_gap: Optional[float] = None, outer_timeout: Optional[float] = None
    ) -> None:
        """Dismiss qwen's blocking first-run dialogs (theme / folder-trust).

        Mirrors AntigravityCliProvider._handle_startup_dialog / KimiCliProvider.
        Polls the pane and, if a theme picker or a folder-trust gate is showing,
        accepts the pre-selected option with Enter. Both are one-shot on first
        install; once qwen is at its ready input box the loop returns. When
        credentials are already configured this is typically a no-op. Before
        the first answered dialog only the outer cap applies; each answered
        dialog resets the idle gap, so late and cascading prompts are handled.
        """
        if idle_gap is None:
            idle_gap = get_server_settings()["startup_prompt_handler_timeout"]
        if outer_timeout is None:
            outer_timeout = self.get_init_timeout(self._launch_profile)
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        deadline = time.monotonic() + outer_timeout
        last_prompt_time: Optional[float] = None
        while True:
            now = time.monotonic()
            if now >= deadline:
                logger.warning("Qwen startup dialog handler reached its initialization outer cap")
                return
            if last_prompt_time is not None and now - last_prompt_time >= idle_gap:
                return
            output = await asyncio.to_thread(
                get_backend().get_history, self.session_name, self.window_name
            )
            if output:
                clean = strip_terminal_escapes(output)
                if re.search(AUTH_PICKER_PATTERN, clean):
                    raise ProviderError(
                        "Qwen authentication needs attention: check the network and native "
                        "login in qwen outside CAO, then retry this launch. "
                        "The provider did not answer the authentication picker."
                    )
                # Ready input box with no dialog pending → done.
                if re.search(IDLE_FOOTER_PATTERN, clean) and not re.search(
                    STARTUP_DIALOG_PATTERN, clean
                ):
                    return
                if re.search(STARTUP_DIALOG_PATTERN, clean):
                    logger.info("Qwen Code startup dialog detected, accepting default")
                    status_monitor.notify_input_sent(self.terminal_id)
                    await asyncio.to_thread(
                        get_backend().send_special_key, self.session_name, self.window_name, "Enter"
                    )
                    last_prompt_time = time.monotonic()
                    await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))
                    continue
            await asyncio.sleep(min(1.0, max(0.0, deadline - time.monotonic())))

    # ------------------------------------------------------------------ #
    # Status detection
    # ------------------------------------------------------------------ #

    def _ready_status(self) -> TerminalStatus:
        if self._output is not None:
            try:
                if not self._output.has_current_user():
                    return TerminalStatus.UNKNOWN
                if self._output.user_turns > 0:
                    return TerminalStatus.COMPLETED
            except OutputExtractionError:
                return TerminalStatus.ERROR
        return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

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
        if not re.search(IDLE_FOOTER_PATTERN, tail) and re.search(AUTH_PICKER_PATTERN, clean):
            return TerminalStatus.WAITING_USER_ANSWER

        # Interactive prompt blocking on user input takes precedence over a
        # plain processing state.
        if not re.search(IDLE_FOOTER_PATTERN, tail) and re.search(
            WAITING_USER_ANSWER_PATTERN, tail
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        if re.search(PROCESSING_FOOTER_PATTERN, tail) or any(
            re.search(PROCESSING_SPINNER_PATTERN, line) for line in tail.splitlines()
        ):
            return TerminalStatus.PROCESSING

        # IDLE / COMPLETED: ready input box present. Fresh spawn (no delivered
        # turn) is IDLE; a finished turn is COMPLETED.
        if re.search(IDLE_FOOTER_PATTERN, tail):
            return self._ready_status()

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
    status_recheck_uses_screen = True

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
        if not re.search(IDLE_FOOTER_PATTERN, bottom) and re.search(AUTH_PICKER_PATTERN, joined):
            return TerminalStatus.WAITING_USER_ANSWER

        if not re.search(IDLE_FOOTER_PATTERN, bottom) and re.search(
            WAITING_USER_ANSWER_PATTERN, bottom
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        if re.search(PROCESSING_FOOTER_PATTERN, bottom) or any(
            re.search(PROCESSING_SPINNER_PATTERN, line) for line in bottom_rows
        ):
            return TerminalStatus.PROCESSING

        if re.search(IDLE_FOOTER_PATTERN, bottom):
            return self._ready_status()

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
        if self._output is not None:
            return self._output.response()
        clean = strip_terminal_escapes(script_output)
        lines = clean.split("\n")

        # Index of the last echoed user query line.
        last_query_idx: Optional[int] = None
        for i, line in enumerate(lines):
            if re.search(QUERY_PROMPT_PATTERN, line):
                last_query_idx = i
        if last_query_idx is None:
            raise OutputExtractionError(
                "No Qwen Code user query found - no '> <text>' line detected"
            )

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
        # Qwen 0.28 distinguishes final prose (◆) from thought panels (∴).
        # Once present, use only the final response region, including its
        # continuation lines. Never return the preceding thought/tool panels.
        finals = [i for i, line in enumerate(body) if re.search(_FINAL_BULLET_PATTERN, line)]
        if finals:
            body = body[finals[-1] :]
        elif any(re.search(_THOUGHT_PATTERN, line) for line in body):
            raise OutputExtractionRejected("Qwen turn contains reasoning but no final answer")
        response_lines: list[str] = []
        in_tool_output = False
        for line in body:
            if not line.strip():
                continue
            if _is_chrome(line):
                continue
            if re.search(_TOOL_CALL_PATTERN, line) or re.search(_PICKER_OPTION_PATTERN, line):
                in_tool_output = True
                continue
            if re.search(RESPONSE_BULLET_PATTERN, line):
                in_tool_output = False
            if in_tool_output:
                continue
            # Strip the assistant/tool bullet ("● ") but keep the text.
            text = re.sub(_FINAL_BULLET_PATTERN, "", line)
            text = re.sub(RESPONSE_BULLET_PATTERN, "", text).strip()
            if text:
                response_lines.append(text)

        response = "\n".join(response_lines).strip()
        if not response:
            raise OutputExtractionRejected(
                "Empty Qwen Code response - no content found after query"
            )
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
        if self._output is not None:
            self._output.path.unlink(missing_ok=True)
            try:
                self._output.path.parent.rmdir()
            except FileNotFoundError:
                pass
            self._output = None
        self._initialized = False

    def mark_input_received(self) -> None:
        """Record that a turn was delivered (IDLE → COMPLETED on next status)."""
        super().mark_input_received()
        if self._output is not None:
            self._output.begin_dispatch()
        self._turns += 1

    def mark_redelivery_received(self) -> None:
        """A retry is a new paste attempt, not a new logical turn."""
        super().mark_input_received()
        if self._output is not None:
            self._output.begin_dispatch()
