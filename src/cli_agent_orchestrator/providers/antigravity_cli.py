"""Antigravity CLI (``agy``) provider implementation.

Antigravity CLI (https://antigravity.google) is Google's terminal-native AI
coding agent — the successor to the Gemini CLI after Google retired the free
Gemini Code Assist "Login with Google" path for the ``gemini`` binary on
2026-06-18. The CLI is invoked via the ``agy`` binary
(``curl -fsSL https://antigravity.google/cli/install.sh | bash``).

Key characteristics (observed on ``agy`` 1.0.10, full-screen TUI):

- Command: ``agy`` with ``--dangerously-skip-permissions`` to auto-approve tool
  calls so orchestrated (handoff / assign) flows do not block on per-tool
  approval prompts, ``--model "<name>"`` to pick a model (human-readable
  strings such as ``"Gemini 3.1 Pro (High)"`` — see ``agy models``), and
  ``-i "<prompt>"`` (``--prompt-interactive``) to inject the agent profile's
  system prompt as the first message and then continue interactively.
- Idle prompt: a ``>`` input box delimited by full-width ``─`` (U+2500) rule
  lines, with the footer hint ``? for shortcuts`` and the model name in the
  status bar.
- Processing: the footer flips to ``esc to cancel`` and a braille spinner line
  (``⣯ Generating...`` / ``⣽ Working...`` — the word varies) appears. The
  footer marker is the reliable, render-stable signal (it survives
  ``strip_terminal_escapes``); the spinner word is not.
- Completed: the response is rendered (2-space indented) between the echoed
  ``> <query>`` line and the input box, and the footer returns to
  ``? for shortcuts``.
- MCP config: written to ``~/.gemini/config/mcp_config.json`` under the
  top-level ``mcpServers`` key (``agy`` reads MCP servers from this fixed
  path; there is no per-invocation override flag).
- System prompt / role: ``agy`` honors a guarded ``-i`` instruction (verified:
  it adopts the role and waits without exploring when told to). The profile
  body, skill catalog, and (when tool-restricted) the security prompt are
  injected this way.
- Exit: ``/quit`` (slash command) — also exits on Ctrl-D pressed twice.

Status detection mirrors the structural, footer-anchored approach used by the
Cursor CLI provider: the presence of ``esc to cancel`` means PROCESSING; the
presence of ``? for shortcuts`` means IDLE / COMPLETED (split on a turn
counter, since the TUI looks identical in both states).
"""

import json
import logging
import re
import shlex
import shutil
import time
from pathlib import Path
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.constants import SECURITY_PROMPT
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_cao_mcp_command
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for Antigravity CLI provider-specific errors."""

    pass


# =============================================================================
# Regex patterns for Antigravity CLI (agy) output analysis
# =============================================================================

# PROCESSING footer hint. ``agy`` renders "esc to cancel" on the footer line
# every frame the agent is working on a turn; it is replaced by
# "? for shortcuts" once the turn completes. This is the reliable, render-
# stable processing signal (it survives ``strip_terminal_escapes``).
PROCESSING_FOOTER_PATTERN = r"esc to cancel"

# IDLE / COMPLETED footer hint, shown whenever the input box is ready.
IDLE_FOOTER_PATTERN = r"\?\s*for shortcuts"
# Same hint for log-file pre-checks (no ANSI involved).
IDLE_FOOTER_PATTERN_LOG = r"\?\s*for shortcuts"

# Braille spinner + status word (e.g. "⣯ Generating...", "⣽ Working...").
# Secondary processing signal; the word varies so we match the glyph + ellipsis.
PROCESSING_SPINNER_PATTERN = r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏⣾⣽⣻⢿⡿⣟⣯⣷][^\n]*(?:\.\.\.|…)"

# Echoed user query line: "> <text>" (non-empty after the prompt char).
# Start-of-line anchored so it does not match the empty idle prompt ("> ").
QUERY_PROMPT_PATTERN = r"^\s*>\s+\S"

# Empty idle input prompt: a lone "> " on its own line.
IDLE_PROMPT_PATTERN = r"^\s*>\s*$"

# Full-width horizontal rule (U+2500) delimiting the input box / transcript
# sections. Anchored to a full line; tolerates surrounding whitespace.
SEPARATOR_PATTERN = r"^\s*─{20,}\s*$"

# Workspace-trust dialog shown on the FIRST launch in an untrusted directory:
# "Antigravity CLI requires permission to read, edit, and execute files here."
# with a "> Yes, I trust this folder / No, exit" picker (Yes pre-selected).
# --dangerously-skip-permissions covers tool approvals, NOT this workspace-trust
# gate, so CAO (which launches agy in a fresh session cwd) hits it and init
# hangs — the picker matches WAITING_USER_ANSWER and never reads IDLE. Dismissed
# in initialize() by sending Enter (accepts the pre-selected "Yes").
TRUST_PROMPT_PATTERN = r"Yes, I trust this folder|requires permission to read, edit"

# Feedback-survey dialog agy occasionally shows right after startup ("How's
# the CLI experience so far? [1] Good [2] Fine [3] Bad [0] Skip"). Like the
# trust picker it blocks IDLE until answered — dismissed by sending "0"
# (Skip) + Enter. Note the ready footer ("? for shortcuts") stays visible
# UNDER this dialog, so it must be checked before any idle-footer early-exit.
SURVEY_PROMPT_PATTERN = r"How'?s the CLI experience so far"

# Interactive prompts that block on user input (approval dialogs, pickers).
# With --dangerously-skip-permissions these are rare, but we still classify
# them as WAITING_USER_ANSWER so orchestrated input is not mistaken for the
# answer to such a prompt.
WAITING_USER_ANSWER_PATTERN = (
    r"(?:↑/↓\s*(?:to )?[Nn]avigate)"
    r"|(?:\[\s*y\s*/\s*n\s*\])"
    r"|(?:Allow once|Allow always|Do you want to (?:allow|run|proceed))"
    r"|(?:enter Toggle|enter Confirm)"
)

# Error patterns surfaced on the agent's own output / a crashed binary.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|panic:|agy: .*(?:error|failed)|Traceback \(most recent call last\):)"
)

# Tail window (chars) scanned for the footer markers. The footer is rendered
# in the last few hundred bytes of every TUI frame; 2KB is well within the
# StatusMonitor's rolling buffer and avoids flipping to IDLE mid-response when
# a long answer scrolls the older footer out of the window.
FOOTER_TAIL_WINDOW = 2048

# Chrome lines filtered out of the extracted response.
_BANNER_PATTERN = r"(?:Antigravity CLI \d|▀|▄|█)"
_TIP_PATTERN = r"^\s*(?:└\s*)?Tip:"
_FOOTER_LINE_PATTERN = r"(?:\? for shortcuts|esc to cancel)"
# Thought-process summary lines ("▸ Thought for 4s, ...") and tool-call lines
# ("● cao-mcp-server/load_skill(...)", "● Read(...)") are TUI activity chrome,
# not response content. The survey interstitial is filtered too.
_THOUGHT_PATTERN = r"^\s*▸"
_TOOL_CALL_PATTERN = r"^\s*●"
_SURVEY_PATTERN = r"How's the CLI experience|Help us improve|\[\d\]\s*(?:Good|Fine|Bad|Skip)"


class AntigravityCliProvider(BaseProvider):
    """Provider for the Antigravity CLI (``agy``).

    Manages the lifecycle of an ``agy`` REPL session inside a tmux window:
    initialization (with profile system prompt, model, and MCP config),
    status detection, response extraction, and cleanup.

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
        """Initialize the Antigravity CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional CAO agent profile name.
            allowed_tools: Optional list of CAO tool names the agent may use.
                When restricted (not wildcard), the security prompt is appended
                to the injected system prompt for soft enforcement.
            model: Optional model override (e.g. ``"Gemini 3.1 Pro (High)"``).
                The profile's ``model`` field takes precedence when set.
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model
        # MCP server names registered into ~/.gemini/config/mcp_config.json,
        # removed on cleanup().
        self._mcp_server_names: list[str] = []
        # Turn counter. get_status() returns IDLE while _turns == 0 (fresh
        # spawn / post-init, no task delivered yet) and COMPLETED once at least
        # one turn has been delivered and the agent is back to a ready footer.
        # The TUI footer ("? for shortcuts") is identical in both states, so the
        # counter is the authoritative IDLE-vs-COMPLETED signal. Incremented by
        # mark_input_received(), which the terminal service calls after every
        # send_input(). This keeps the handoff/assign "wait for IDLE before
        # sending the task" contract working right after init.
        self._turns: int = 0

    @property
    def blocks_orchestrated_input_while_waiting_user_answer(self) -> bool:
        """agy's approval dialogs / pickers consume pasted text as the answer.

        Even with ``--dangerously-skip-permissions`` some interactive prompts
        can surface; when one is up, an orchestrated assign/handoff message
        pasted into the input would be read as the prompt's answer. Opting in
        makes the terminal service hold orchestrated delivery until the prompt
        clears, while still allowing explicit user-prompt answers.
        """
        return True

    # ------------------------------------------------------------------ #
    # Launch
    # ------------------------------------------------------------------ #

    def _mcp_config_path(self) -> Path:
        """Path to agy's MCP config file (shared ~/.gemini/config/mcp_config.json)."""
        return Path.home() / ".gemini" / "config" / "mcp_config.json"

    def _build_agy_command(self) -> str:
        """Build the ``agy`` launch command.

        Structure::

            agy --dangerously-skip-permissions [--model "<model>"] [-i "<system prompt>"]

        ``--dangerously-skip-permissions`` auto-approves tool calls (required
        for unattended orchestration). ``--model`` selects the model. The agent
        profile's system prompt (+ skill catalog + security prompt when tool-
        restricted) is injected via ``-i`` with an explicit "acknowledge and
        wait" guard so the agent adopts its role without exploring on launch.

        Returns a shell-escaped command string for ``send_keys``.
        """
        binary = shutil.which("agy")
        if not binary:
            raise ProviderError(
                "Antigravity CLI not found: 'agy' is not on $PATH. "
                "Install via: curl -fsSL https://antigravity.google/cli/install.sh | bash"
            )

        command_parts = ["agy", "--dangerously-skip-permissions"]

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

        # System prompt injection via -i.
        if profile is not None:
            system_prompt = profile.system_prompt or ""
            system_prompt = self._apply_skill_prompt(system_prompt)
            # Soft tool restriction: when the profile is not allowed every tool
            # (e.g. the read-only reviewer), append the security prompt. agy
            # honors a clear instruction not to use disallowed tools.
            if self._allowed_tools and "*" not in self._allowed_tools:
                system_prompt = (
                    f"{system_prompt}\n\n{SECURITY_PROMPT}" if system_prompt else SECURITY_PROMPT
                )
            if system_prompt:
                role_name = profile.name or "agent"
                guarded = (
                    f"{system_prompt}\n\n---\n"
                    f"You are the {role_name}. Acknowledge your role in one sentence, "
                    f"then wait for tasks. Do not take any action or use any tools "
                    f"until you receive a specific task."
                )
                command_parts.extend(["-i", guarded])

            # MCP servers (cao-mcp-server etc.) → agy's shared mcp_config.json.
            if profile.mcpServers:
                self._register_mcp_servers(profile.mcpServers)

        return shlex.join(command_parts)

    def _register_mcp_servers(self, mcp_servers: dict) -> None:
        """Register MCP servers into agy's ~/.gemini/config/mcp_config.json.

        agy reads MCP servers from this fixed file under the top-level
        ``mcpServers`` key. We merge our entries in (preserving any existing,
        non-CAO servers), forwarding ``CAO_TERMINAL_ID`` into each server's env
        so cao-mcp-server can resolve the current terminal for handoff / assign.

        Concurrency: the file is shared across terminals, but CAO serializes
        launches (initialize() waits for the agent to become ready before the
        next terminal launches), so each agy process reads the config and spawns
        its MCP subprocess with the correct terminal id before the next write.
        """
        path = self._mcp_config_path()
        try:
            if path.exists() and path.stat().st_size > 0:
                with open(path) as f:
                    config = json.load(f)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                config = {}
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Could not read %s, starting fresh: %s", path, exc)
            config = {}

        # The file is shared with the user's own agy config; tolerate a valid-
        # but-unexpected shape (e.g. a JSON list/string) instead of raising.
        if not isinstance(config, dict):
            logger.warning(
                "MCP config root in %s is %s, not an object; resetting",
                path,
                type(config).__name__,
            )
            config = {}

        servers = config.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            logger.warning(
                "'mcpServers' in %s is %s, not an object; replacing",
                path,
                type(servers).__name__,
            )
            servers = {}
            config["mcpServers"] = servers
        for server_name, server_config in mcp_servers.items():
            if isinstance(server_config, dict):
                cfg = dict(server_config)
            else:
                cfg = server_config.model_dump(exclude_none=True)
            # Resolve the bundled cao-mcp-server console script to a
            # PATH-independent invocation. persisted=True: this command is
            # written to mcp_config.json and read by agy at later launches,
            # so prefer the stable PATH launcher over the versioned venv path.
            command, args = resolve_cao_mcp_command(
                cfg.get("command", ""), cfg.get("args", []) or [], persisted=True
            )
            entry: dict = {
                "command": command,
                "args": args,
            }
            env = dict(cfg.get("env", {}))
            env["CAO_TERMINAL_ID"] = self.terminal_id
            entry["env"] = env
            servers[server_name] = entry
            self._mcp_server_names.append(server_name)

        with open(path, "w") as f:
            json.dump(config, f, indent=2)

    def _unregister_mcp_servers(self) -> None:
        """Remove the MCP servers this provider registered."""
        if not self._mcp_server_names:
            return
        path = self._mcp_config_path()
        if not path.exists():
            self._mcp_server_names = []
            return
        try:
            with open(path) as f:
                config = json.load(f)
            servers = config.get("mcpServers") if isinstance(config, dict) else None
            if isinstance(servers, dict):
                for name in self._mcp_server_names:
                    servers.pop(name, None)
                with open(path, "w") as f:
                    json.dump(config, f, indent=2)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to unregister MCP servers from %s: %s", path, exc)
        finally:
            # Always clear our state so a malformed config can never leave stale
            # names behind and block terminal teardown.
            self._mcp_server_names = []

    def _handle_startup_dialog(self, timeout: Optional[float] = None) -> None:
        """Dismiss agy's blocking startup dialogs (workspace-trust, survey).

        Mirrors ClaudeCodeProvider._handle_startup_prompts / KimiCliProvider.
        Polls the pane and answers, in whatever order they appear:
        - workspace-trust picker → Enter (accepts the pre-selected "Yes")
        - feedback survey → "0" + Enter (Skip)
        Both can appear in ONE startup (trust first, survey after), so a
        dismissal continues the loop rather than returning. Exits once agy is
        at its ready footer with no dialog on screen — the survey renders ON
        TOP of the footer, so the survey check must run before the idle exit.
        """
        if timeout is None:
            timeout = get_server_settings()["startup_prompt_handler_timeout"]
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        start_time = time.time()
        trust_done = survey_done = False
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                clean = strip_terminal_escapes(output)
                if not trust_done and re.search(TRUST_PROMPT_PATTERN, clean):
                    logger.info("Antigravity workspace-trust dialog detected, accepting")
                    status_monitor.notify_input_sent(self.terminal_id)
                    get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                    trust_done = True
                    time.sleep(1.0)
                    continue
                if not survey_done and re.search(SURVEY_PROMPT_PATTERN, clean):
                    logger.info("Antigravity feedback survey detected, skipping")
                    status_monitor.notify_input_sent(self.terminal_id)
                    get_backend().send_keys(self.session_name, self.window_name, "0", enter_count=0)
                    time.sleep(0.5)
                    get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                    survey_done = True
                    time.sleep(1.0)
                    continue
                # At the ready footer with no dialog pending → done.
                if re.search(IDLE_FOOTER_PATTERN, clean):
                    return
            time.sleep(1.0)

    async def initialize(self) -> bool:
        """Initialize the Antigravity CLI provider by starting ``agy``.

        1. Wait for the shell prompt in the tmux window.
        2. Send the ``agy`` command (model + system prompt + MCP config).
        3. Wait for the agent to reach IDLE / COMPLETED.

        Raises:
            TimeoutError: If the shell or agy initialization times out.
        """
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_agy_command()

        # Arm the StatusMonitor stickiness gate so the launch drives a fresh
        # PROCESSING transition past any stale ready latch. Imported lazily to
        # avoid a circular import (status_monitor imports provider_manager).
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Accept the workspace-trust dialog if agy shows one (first launch in an
        # untrusted cwd). Unanswered it blocks init — the picker never reads IDLE.
        self._handle_startup_dialog()

        # agy startup + first MCP connection + the -i acknowledgment can take
        # a while.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=180.0,
        ):
            raise TimeoutError("Antigravity CLI initialization timed out after 180 seconds")

        self._initialized = True
        return True

    # ------------------------------------------------------------------ #
    # Status detection
    # ------------------------------------------------------------------ #

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        """Detect agy status from the terminal output buffer.

        Priority (matches the checks below in order):
          1. Empty → UNKNOWN
          2. WAITING_USER_ANSWER — an interactive approval / picker prompt
             (takes precedence over the processing footer/spinner)
          3. PROCESSING — footer "esc to cancel" (or a spinner line) in the tail
          4. IDLE / COMPLETED — footer "? for shortcuts" (IDLE pre-first-turn,
             COMPLETED after)
          5. ERROR — matched error pattern
          6. UNKNOWN — nothing matched
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

        # PROCESSING: the "esc to cancel" footer is the render-stable signal.
        # The spinner line is a secondary cue. We still let the WAITING check
        # run first below for the rare approval prompt under skip-permissions.
        processing = re.search(PROCESSING_FOOTER_PATTERN, tail) is not None or any(
            re.search(PROCESSING_SPINNER_PATTERN, line) for line in tail.splitlines()
        )

        # Interactive prompt blocking on user input takes precedence over a
        # plain processing state.
        if re.search(WAITING_USER_ANSWER_PATTERN, tail):
            return TerminalStatus.WAITING_USER_ANSWER

        if processing:
            return TerminalStatus.PROCESSING

        # IDLE / COMPLETED: ready footer present. Fresh spawn (no delivered
        # turn) is IDLE; a finished turn is COMPLETED.
        if re.search(IDLE_FOOTER_PATTERN, tail):
            return TerminalStatus.COMPLETED if self._turns > 0 else TerminalStatus.IDLE

        if re.search(ERROR_PATTERN, clean, re.MULTILINE):
            return TerminalStatus.ERROR

        return TerminalStatus.UNKNOWN

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS).
    # The raw-stream get_status() above is unreliable for agy: when the footer
    # flips from "esc to cancel" (PROCESSING) back to "? for shortcuts" (IDLE),
    # agy overwrites it in place with cursor moves. The append-only pipe-pane
    # log keeps BOTH strings after strip_terminal_escapes(), so the stale
    # "esc to cancel" pins the terminal to PROCESSING forever — the session
    # never reaches IDLE and POST /sessions times out. A composited pyte
    # viewport resolves the in-place redraw, leaving only the live footer.
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect agy status from a pyte-composited viewport (escape-free rows).

        Same footer precedence as get_status, but anchored on the rendered
        bottom region rather than the raw redraw stream. Because the viewport
        has every in-place footer rewrite already resolved, exactly one of
        ``esc to cancel`` / ``? for shortcuts`` is present — eliminating the
        stale-footer false PROCESSING the raw-stream path suffers from.

        The StatusMonitor only invokes this on settled / rising-edge frames, so
        the footer reflects a real end state, not a half-drawn one.

        Precedence:
          1. Empty → UNKNOWN
          2. WAITING_USER_ANSWER — interactive approval / picker prompt
          3. PROCESSING — footer "esc to cancel" or a spinner line in the tail
          4. IDLE / COMPLETED — footer "? for shortcuts" (IDLE pre-first-turn,
             COMPLETED after)
          5. ERROR — matched error pattern
          6. UNKNOWN — nothing matched
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN

        joined = "\n".join(rows)
        # The footer lives on the last rendered row; the spinner sits just above
        # it. A small bottom window keeps stale response text from matching.
        bottom_rows = rows[-12:]
        bottom = "\n".join(bottom_rows)

        # Interactive prompt blocking on user input takes precedence over a
        # plain processing state.
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
        """Return the agy IDLE footer pattern for log-file pre-checks."""
        return IDLE_FOOTER_PATTERN_LOG

    # ------------------------------------------------------------------ #
    # Response extraction
    # ------------------------------------------------------------------ #

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's last response from rendered terminal output.

        Layout of a completed turn (rendered)::

            ─────────────────────────────
            > <user question>
              <assistant response line 1>
              <assistant response line 2>
            ───────────────────────────── (input box top rule)
            >
            ───────────────────────────── (input box bottom rule)
            ? for shortcuts            <model>

        The response is the text between the last echoed ``> <query>`` line and
        the next full-width separator (the top of the input box). TUI chrome
        (banner, separators, footer, tips, spinner) is filtered out.

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
            raise ValueError("No Antigravity CLI user query found - no '> <text>' line detected")

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
                or re.search(_THOUGHT_PATTERN, text_line)
                or re.search(_TOOL_CALL_PATTERN, text_line)
                or re.search(_SURVEY_PATTERN, stripped_line)
                or re.search(_BANNER_PATTERN, stripped_line)
            )

        body = lines[last_query_idx + 1 : end_idx]
        response_lines: list[str] = []
        i = 0
        n = len(body)
        while i < n:
            line = body[i]
            stripped = line.strip()
            i += 1
            if not stripped:
                continue
            if re.search(_THOUGHT_PATTERN, line):
                # agy renders a collapsed thought as the "▸ Thought for Xs, N
                # tokens" header immediately followed by one indented auto-
                # generated title line (e.g. "Prioritizing Tool Usage"). The
                # header is chrome; so is that single title line. Skip past any
                # blanks then drop exactly the next non-blank line, but only if
                # it isn't itself recognized chrome (a thought with no title
                # must not consume real response content that follows).
                while i < n and not body[i].strip():
                    i += 1
                if i < n and not _is_chrome(body[i]):
                    i += 1
                continue
            if _is_chrome(line):
                continue
            response_lines.append(stripped)

        response = "\n".join(response_lines).strip()
        if not response:
            raise ValueError("Empty Antigravity CLI response - no content found after query")
        return response

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def exit_cli(self) -> str:
        """Get the command to exit agy. ``/quit`` is the slash command."""
        return "/quit"

    def cleanup(self) -> None:
        """Remove the MCP servers this provider registered and reset state."""
        self._unregister_mcp_servers()
        self._initialized = False

    def mark_input_received(self) -> None:
        """Record that a turn was delivered (IDLE → COMPLETED on next status)."""
        super().mark_input_received()
        self._turns += 1
