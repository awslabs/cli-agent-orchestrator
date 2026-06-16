"""Kimi CLI provider implementation.

Kimi CLI (https://kimi.com/code) is Moonshot AI's coding agent CLI tool.
It runs as an interactive TUI using prompt_toolkit in the terminal.

Key characteristics:
- Command: ``kimi`` (installed via ``brew install kimi-cli`` or ``uv tool install kimi-cli``)
- Idle prompt: ``💫`` (thinking mode, default) or ``✨`` (optionally prefixed with ``username@dirname``)
- Processing: No idle prompt visible at bottom while the response is streaming
- Response format: Bullet points prefixed with ``•`` (U+2022)
- Thinking output: Gray italic ``•`` bullets (ANSI color 38;5;244 + italic)
- User input: Displayed in a bordered box using box-drawing characters (╭│╰)
- Auto-approve: ``--yolo`` flag bypasses all tool action confirmations
- Agent profiles: ``--agent-file FILE`` (YAML format, extends built-in 'default' agent)
- MCP config: ``--mcp-config TEXT`` (JSON configuration, repeatable flag)
- Exit commands: ``/exit``, ``exit``, ``quit``, or Ctrl-D
- Status bar: ``HH:MM [yolo] agent (model, thinking) ctrl-x: toggle mode context: X.X%``

Status Detection Strategy:
    Kimi CLI uses a full-screen TUI (prompt_toolkit), so status is detected by
    checking the bottom of tmux capture output:
    - IDLE: Prompt pattern (username@dir💫/✨) visible at bottom, no user input yet
    - PROCESSING: No prompt at bottom (response is streaming)
    - COMPLETED: Prompt at bottom + response content after last user input
    - ERROR: Error message patterns or empty output
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
from typing import Any, Dict, List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.mcp_resolution import resolve_mcp_server_config
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for Kimi CLI provider-specific errors."""

    pass


# =============================================================================
# Regex patterns for Kimi CLI output analysis
# =============================================================================

# Strip ANSI escape codes for clean text matching.
# Matches sequences like \x1b[0m, \x1b[38;5;244m, \x1b[1m, etc.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Kimi idle prompt: ``💫`` or ``✨`` (optionally prefixed with ``username@dirname``).
# ✨ appears in normal agent mode (--no-thinking).
# 💫 appears when thinking mode is enabled (default behavior).
# Kimi CLI v1.20.0+ renders just the emoji; earlier versions showed ``username@dirname💫``.
# The prefix is made optional to support both formats.
IDLE_PROMPT_PATTERN = r"(?:\w+@[\w.-]+)?[✨💫]"

# Number of lines from bottom to scan for the idle prompt.
# Kimi's TUI renders empty padding lines between the prompt and the status bar.
# The padding depends on terminal height: a 46-row terminal has ~32 empty lines
# between the prompt (line ~14 after the welcome banner) and the status bar.
# Must be large enough to cover the tallest expected terminal.
IDLE_PROMPT_TAIL_LINES = 50

# Simplified idle pattern for log file monitoring.
# Just looks for either emoji marker, which is sufficient for quick detection.
IDLE_PROMPT_PATTERN_LOG = r"[✨💫]"

# Kimi welcome banner, shown once during startup inside a bordered box.
# Used to detect successful initialization without needing to wait for prompt.
WELCOME_BANNER_PATTERN = r"Welcome to Kimi Code CLI!"

# Startup upgrade-reminder dialog. When a newer kimi-cli is available, kimi
# renders an interactive menu ("[Enter] Upgrade now  [q] Not now  [s] Skip
# reminders for version X") BEFORE the REPL and blocks on a keypress. Left
# unanswered, kimi never reaches its ready prompt and init times out (the boot
# gate holds it PROCESSING). We answer 's' to skip reminders for this version
# (persisted, so it does not recur until the next release).
UPGRADE_PROMPT_PATTERN = r"Skip reminders for version|Upgrade now"

# User input box boundaries (pre-v1.20.0). Kimi displayed user messages in a bordered box:
#   ╭──────────────────────────────╮
#   │ user message text             │
#   ╰──────────────────────────────╯
# In v1.20.0+, user input appears on the prompt line: ``💫 user message``
USER_INPUT_BOX_START_PATTERN = r"╭─"
USER_INPUT_BOX_END_PATTERN = r"╰─"

# Prompt line with user input (v1.20.0+ format).
# Matches ``💫 some text`` or ``✨ some text`` — a prompt emoji followed by non-whitespace
# on the SAME line. Uses [^\S\n]+ (horizontal whitespace only) to avoid matching
# across newlines (a bare ``💫`` followed by blank lines then status bar).
PROMPT_WITH_INPUT_PATTERN = r"(?:\w+@[\w.-]+)?[✨💫][^\S\n]+\S"

# Response/thinking bullet pattern: ``•`` (U+2022) at the start of a line.
# Both thinking (internal monologue) and response (final answer) use this marker.
# To distinguish them in extraction, check ANSI styling in raw output:
# - Thinking: gray italic (\x1b[38;5;244m• ... \x1b[3m\x1b[38;5;244m)
# - Response: plain ``•`` without ANSI color prefix
RESPONSE_BULLET_PATTERN = r"^•\s"

# Thinking bullet detection in raw (ANSI-preserved) output.
# Thinking lines use gray color (38;5;244) before the bullet character.
# This pattern distinguishes thinking from actual response content
# when extracting messages from terminal output.
THINKING_BULLET_RAW_PATTERN = r"\x1b\[38;5;244m\s*•"

# Kimi TUI status bar at the bottom of the screen.
# Format: "HH:MM  [yolo]  agent (model, thinking)  ctrl-x: toggle mode  context: X.X%"
# Used to identify TUI chrome that should be excluded from content analysis.
STATUS_BAR_PATTERN = r"\d+:\d+\s+.*(?:agent|shell)\s*\("

# ---------------------------------------------------------------------------
# Newest "Kimi Code" TUI (the redesigned CLI). Older builds rendered an emoji
# prompt (✨/💫) at the input line; the redesign instead shows a boxed input
# area ("── input ──"), a bottom status bar ("yolo  agent (<model> ●) …"), and a
# "context: 12.3% (n/Nk)" usage line — with NO bare emoji prompt. Detection that
# keyed on the emoji therefore never observed IDLE and timed out at init.
# ---------------------------------------------------------------------------
# Either of these confirms the new TUI is up at its prompt: the context-usage
# footer, or the status bar's "agent (<model> ●)" segment (● = U+25CF).
NEW_TUI_STATUS_PATTERN = r"context:\s*\d+(?:\.\d+)?%|agent\s*\([^)]*●"
# Live working indicator: the new TUI animates a braille spinner
# ("⠧ Thinking… 5s · 220 tokens", "⠹ Using handoff({...})") and a moon-phase
# thinking glyph (🌑…🌘) that are cleared when the turn finishes. Any such
# glyph means a turn-in-flight FRAME was rendered; freshness relative to the
# last response bullet decides whether the turn is still going (see
# get_status).
NEW_TUI_SPINNER_PATTERN = r"[⠁-⣿]|[🌑🌒🌓🌔🌕🌖🌗🌘]"
# Boot/MCP chrome also renders braille glyphs while the terminal is genuinely
# idle at the welcome screen ("⠧ MCP Servers: 0/1 connected", "⠦ cao-mcp-server
# (connecting)", "⠋ Resolving dependencies..."). Those must NOT count as a
# live turn-in-flight spinner or a freshly-booted terminal would never read
# IDLE.
NEW_TUI_BOOT_CHROME_PATTERN = re.compile(
    r"MCP Servers|\(connecting\)|Resolving dependencies|connecting to mcp servers"
    r"|Loading configuration|Loading agent|Restoring conversation",
    re.IGNORECASE,
)


def _is_live_turn_spinner_line(line: str) -> bool:
    """True when ``line`` carries a live turn-in-flight spinner glyph."""
    return bool(
        re.search(NEW_TUI_SPINNER_PATTERN, line) and not NEW_TUI_BOOT_CHROME_PATTERN.search(line)
    )


# A response/thinking bullet ("• …") at line start. Its presence means a turn
# has produced output — used to latch "input received" on the new TUI (the
# welcome banner / update nag contain no "•", so this won't false-trigger at
# init).
ANY_BULLET_PATTERN = r"(?m)^\s*•"

# Generic error patterns for detecting failure states in terminal output.
ERROR_PATTERN = (
    r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|ConnectionError:|APIError:)"
)


class KimiCliProvider(BaseProvider):
    """Provider for Kimi CLI tool integration.

    Manages the lifecycle of a Kimi CLI session in a tmux window,
    including initialization, status detection, response extraction,
    and cleanup. Kimi CLI agent profiles are optional — if not provided,
    Kimi uses its built-in default agent.
    """

    # Class-level flag: ensures ~/.kimi/config.toml MCP timeout is set only once,
    # even when multiple KimiCliProvider instances are created in parallel (e.g.,
    # 3 data_analyst workers via assign). Without this, concurrent read/write to
    # the config file causes race conditions and file corruption.
    _mcp_timeout_configured = False

    # Class-level prompt regex shared between status detection
    # and ``extract_session_context``. Bounded quantifiers
    # (no unbounded ``*`` / ``+`` — defeats ReDoS on pathological pane bytes).
    # Matches the v1.20+ idle-line shape ``[user@host]💫 message`` AND the
    # bare-emoji ``💫 message`` form. The optional ``\S`` tail matches "user
    # text follows on the same line" — used to slice the message off after
    # the prompt marker.
    _KIMI_PROMPT_RE = re.compile(r"(?:\w{1,32}@[\w.\-]{1,64})?[✨💫][^\S\n]{1,4}\S")
    # Response/thinking markers used to bound a user message line. Matches
    # the same ``• `` bullet the IDLE/PROCESSING path uses.
    _KIMI_RESPONSE_MARKER_RE = re.compile(r"^•\s")

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
        skill_prompt: Optional[str] = None,
    ):
        """Initialize provider state."""
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        # Track temp directory for cleanup (created when agent profile needs temp files)
        self._temp_dir: Optional[str] = None
        # Latching flag: set True when user input box (╭─) is detected in ANY
        # get_status() call. Persists even after the box scrolls out of the
        # tmux capture window (200 lines). This is needed because:
        # 1. Long responses push the user input box out of capture range
        # 2. Not all responses use • bullets (tables, numbered lists, etc.)
        # Without this, get_status() returns IDLE instead of COMPLETED after
        # the agent finishes processing, causing handoff to time out.
        self._has_received_input = False
        # Wallclock of the last send_input() dispatch (terminal_service calls
        # mark_input_received). Used by the newest-TUI status path: right
        # after a paste, the TUI repaints the ready chrome (status bar) before
        # the spinner's first frame, so a position-based spinner-vs-ready
        # compare reads COMPLETED ~100ms into the new turn. With the
        # StatusMonitor ready-latch, that false COMPLETED is pinned for the
        # whole turn (observed: supervisor-assign e2e extracting mid-flight
        # output). A short dispatch grace bridges the gap until the first
        # spinner frame arrives.
        self._last_dispatch_time = 0.0

    @property
    def paste_enter_count(self) -> int:
        """Kimi CLI's prompt_toolkit submits on single Enter after bracketed paste."""
        return 1

    def mark_input_received(self) -> None:
        """Record a dispatched task (called by terminal_service after send_input).

        Latches ``_has_received_input`` (the buffer-evidence latch can miss it
        when a long paste scrolls the echo out of the rolling window). The
        ``_last_dispatch_time`` stamp (used by the newest-TUI dispatch-grace
        check in get_status()) and the shared native-status tracking come from
        ``super().mark_input_received()``.
        """
        super().mark_input_received()
        self._has_received_input = True

    def _build_kimi_command(self) -> str:
        """Build Kimi CLI command with agent profile and MCP config if provided.

        Returns properly escaped shell command string for tmux send_keys.
        Uses shlex.join() for safe escaping of all arguments.

        Command structure:
            cd <temp_dir> && TERM=xterm-256color kimi --yolo [--agent-file FILE] [--mcp-config JSON]

        The ``cd`` is required because Kimi CLI v1.20.0+ enforces a per-directory
        single-instance lock — only one kimi process can run in a given directory.
        Each provider instance gets its own temp directory to avoid conflicts.

        The ``TERM=xterm-256color`` override is needed because Kimi CLI v1.20.0+
        silently exits when TERM=tmux-256color (the tmux default).

        The --yolo flag auto-approves all tool actions, which is required for
        non-interactive operation in CAO-managed tmux sessions.
        """
        command_parts = ["kimi", "--yolo"]

        # Always create a temp directory for this instance.
        # Kimi CLI v1.20.0+ has a per-directory single-instance lock, so each
        # provider instance needs its own working directory.
        if not self._temp_dir:
            self._temp_dir = tempfile.mkdtemp(prefix="cao_kimi_")

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                if profile.model:
                    command_parts.extend(["--model", profile.model])

                # Build agent file from profile's system prompt.
                # Kimi uses YAML agent files with a system_prompt_path pointing
                # to a markdown file. We create both in the temp directory.
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                system_prompt = self._apply_skill_prompt(system_prompt)

                # Prepend security constraints for soft enforcement (Kimi CLI has no
                # native tool restriction mechanism). Only applied when tool
                # restrictions are active (not unrestricted "*").
                if self._allowed_tools and "*" not in self._allowed_tools:
                    from cli_agent_orchestrator.constants import SECURITY_PROMPT

                    tools_list = ", ".join(self._allowed_tools)
                    tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                    system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

                if system_prompt:
                    # Write the system prompt as a markdown file
                    prompt_file = os.path.join(self._temp_dir, "system.md")
                    with open(prompt_file, "w") as f:
                        f.write(system_prompt)

                    # Create the agent YAML that extends the default agent
                    # and points to our custom system prompt file.
                    # Written as plain string to avoid adding PyYAML dependency.
                    agent_yaml = (
                        "version: 1\n"
                        "agent:\n"
                        "  extend: default\n"
                        "  system_prompt_path: ./system.md\n"
                    )
                    agent_file = os.path.join(self._temp_dir, "agent.yaml")
                    with open(agent_file, "w") as f:
                        f.write(agent_yaml)

                    command_parts.extend(["--agent-file", agent_file])

                # Add MCP server configuration if present in the agent profile.
                # Kimi accepts --mcp-config as a JSON string (repeatable flag).
                if profile.mcpServers:
                    # Set MCP tool call timeout to 600s by modifying ~/.kimi/config.toml
                    # directly. We cannot use --config flag because it causes Kimi CLI
                    # to bypass its default config file, which breaks OAuth authentication
                    # (shows "model: not set" and /login says "restart without --config").
                    # Class-level guard ensures this runs only once per process.
                    self._ensure_mcp_timeout()

                    mcp_config = {}
                    for server_name, server_config in profile.mcpServers.items():
                        if isinstance(server_config, dict):
                            mcp_config[server_name] = dict(server_config)
                        else:
                            mcp_config[server_name] = server_config.model_dump(exclude_none=True)

                        # Resolve the bundled cao-mcp-server console script to a
                        # PATH-independent invocation.
                        mcp_config[server_name] = resolve_mcp_server_config(mcp_config[server_name])

                        # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                        # can identify the current terminal for handoff/assign operations.
                        # Kimi CLI does not automatically forward parent shell env vars
                        # to MCP subprocesses, so we inject it explicitly via the env field.
                        env = mcp_config[server_name].get("env", {})
                        if "CAO_TERMINAL_ID" not in env:
                            env["CAO_TERMINAL_ID"] = self.terminal_id
                            mcp_config[server_name]["env"] = env

                    command_parts.extend(["--mcp-config", json.dumps(mcp_config)])

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        # cd to unique temp dir (per-directory lock) + set TERM for tmux compatibility
        kimi_cmd = shlex.join(command_parts)
        return f"cd {shlex.quote(self._temp_dir)} && TERM=xterm-256color {kimi_cmd}"

    @classmethod
    def _ensure_mcp_timeout(cls) -> None:
        """Ensure MCP tool call timeout is set to 600s in ~/.kimi/config.toml.

        Called once per process (guarded by class-level flag). Kimi CLI defaults
        to tool_call_timeout_ms=60000 (60s) for MCP tool calls, which is too short
        for handoff operations. We modify the config file directly instead of using
        ``--config`` CLI flag, because ``--config`` causes Kimi CLI to bypass the
        default config file and breaks OAuth authentication.

        The timeout is NOT restored on cleanup because:
        1. Multiple Kimi instances may share the config file concurrently
        2. 600s is a strictly better default for anyone using MCP tools
        3. Restoring while other instances are running causes race conditions
        """
        if cls._mcp_timeout_configured:
            return

        config_path = Path.home() / ".kimi" / "config.toml"
        if not config_path.exists():
            logger.warning(f"Kimi config not found at {config_path}, skipping MCP timeout override")
            cls._mcp_timeout_configured = True
            return

        try:
            content = config_path.read_text()

            # Match the existing timeout line under [mcp.client] section
            # Format: tool_call_timeout_ms = 60000
            pattern = r"(tool_call_timeout_ms\s*=\s*)(\d+)"
            match = re.search(pattern, content)
            if match:
                current_value = int(match.group(2))
                if current_value < 600000:
                    new_content = re.sub(pattern, r"\g<1>600000", content)
                    config_path.write_text(new_content)
                    logger.info(
                        f"Set MCP tool_call_timeout_ms to 600000 "
                        f"(was {current_value}) in {config_path}"
                    )
            else:
                logger.warning(
                    f"tool_call_timeout_ms not found in {config_path}, "
                    "MCP tool calls may time out during handoff"
                )
        except Exception as e:
            logger.warning(f"Failed to set MCP timeout in {config_path}: {e}")

        cls._mcp_timeout_configured = True

    def _handle_startup_dialog(self, timeout: Optional[float] = None) -> None:
        """Dismiss kimi's startup upgrade-reminder dialog if it appears.

        Mirrors ClaudeCodeProvider._handle_startup_prompts: polls the pane for
        the interactive "[s] Skip reminders for version X" menu and answers 's'
        so kimi can proceed to its ready prompt. Exits early if kimi is already
        ready (no newer version → no dialog), so a no-update start isn't delayed.
        """
        if timeout is None:
            timeout = get_server_settings()["startup_prompt_handler_timeout"]
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if output:
                clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
                if re.search(UPGRADE_PROMPT_PATTERN, clean_output):
                    from cli_agent_orchestrator.services.status_monitor import status_monitor

                    logger.info("Kimi upgrade-reminder dialog detected, skipping reminders")
                    status_monitor.notify_input_sent(self.terminal_id)
                    # 's' = "Skip reminders for version X"; single-key menu, no Enter.
                    get_backend().send_keys(self.session_name, self.window_name, "s", enter_count=0)
                    time.sleep(1.0)
                    return
                # Already at a ready prompt → no dialog to handle, stop early.
                if self.get_status(output) in (
                    TerminalStatus.IDLE,
                    TerminalStatus.COMPLETED,
                ):
                    return
            time.sleep(1.0)

    async def initialize(self) -> bool:
        """Initialize Kimi CLI provider by starting the kimi command.

        Steps:
        1. Wait for the shell prompt in the tmux window
        2. Build and send the kimi command
        3. Wait for Kimi to reach IDLE state (welcome banner + prompt)

        Returns:
            True if initialization completed successfully

        Raises:
            TimeoutError: If shell or Kimi CLI doesn't start within timeout
        """
        # Wait for shell prompt to appear in the tmux window
        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Build properly escaped command string
        command = self._build_kimi_command()

        # Send Kimi command to the tmux window
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Dismiss the startup upgrade-reminder dialog before waiting for ready:
        # unanswered it blocks kimi from reaching its prompt (init would time out).
        self._handle_startup_dialog()

        # Wait for Kimi CLI to reach IDLE or COMPLETED state (prompt visible).
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        # Longer timeout (120s) to account for first-run setup and when
        # multiple Kimi instances are starting concurrently (e.g. assign flow).
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=120.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("Kimi CLI initialization timed out after 120 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        """Get Kimi CLI status by analyzing terminal output.

        Status detection logic:
        1. Strip ANSI codes for reliable text matching
        2. Latch ``_has_received_input`` when user input box (╭─) is detected
        3. Check bottom N lines for the idle prompt pattern
        4. If prompt found + input was received → COMPLETED
        5. If prompt found + no input yet → IDLE
        6. If no prompt: agent is PROCESSING (streaming response)
        7. Check for ERROR patterns as fallback

        The latching flag approach is necessary because:
        - Long responses (>200 lines) push the user input box out of the
          tmux capture window, so checking for ╭─ on every call is unreliable
        - Not all responses use ``•`` bullets (structured output like tables,
          numbered lists, report templates have no bullet markers at all)
        - The flag is set during the PROCESSING phase when the user input box
          IS still visible in the capture, and persists through completion

        Args:
            output: Terminal output buffer (up to ~8KB rolling buffer)

        Returns:
            TerminalStatus indicating current state
        """
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes, so the bottom-anchored prompt/processing
        # checks see clean, line-oriented text on the raw stream.
        clean_output = strip_terminal_escapes(output)

        # --- Newest "Kimi Code" TUI (redesigned CLI) ---
        # This build has no bare ✨/💫 prompt; readiness is the bottom status bar
        # ("agent (<model> ●)") / "context: N%" footer with the empty "── input ──"
        # box, and a turn-in-flight is a braille spinner ("⠧ Thinking… Ns · N
        # tokens") that is cleared on completion. Gate on the new-TUI markers so
        # legacy (emoji-prompt) builds keep the path below unchanged.
        if re.search(NEW_TUI_STATUS_PATTERN, clean_output):
            # A "•" bullet appears only once a turn produces output (thinking or
            # response); the welcome banner / update nag have none. Latch it so a
            # long response that scrolls the bullets out of the rolling buffer
            # still reads COMPLETED rather than IDLE. Crucially, nothing latches
            # at init, so a freshly-launched terminal reads IDLE (not COMPLETED),
            # avoiding a premature-completion race when the first task is sent.
            if re.search(ANY_BULLET_PATTERN, clean_output):
                self._has_received_input = True

            # PROCESSING vs ready. A spinner-vs-status-bar position compare is
            # unreliable here: the TUI renders the live spinner BETWEEN the
            # "── input ──" rule and the status bar and repaints the status
            # bar with every frame, so the ready chrome is the freshest
            # content even mid-turn (observed: a supervisor turn flapping
            # completed↔processing 29 times in one 57KB stream, which the
            # StatusMonitor ready-latch then pinned at a false COMPLETED).
            # Two in-flight signals, validated by replaying captured live
            # streams; either one means a turn is running:
            # - spinner glyph (braille/moon, incl. tool-call lines like
            #   "⠹ Using handoff({…})") within the freshest tail lines —
            #   frames land every ~100ms while the agent works, and the
            #   turn-finished repaint (input rule + ~12 blank box lines +
            #   separator + status bar + context footer) pushes stale frames
            #   beyond this window;
            # - the last spinner glyph rendered AFTER the last "•" bullet —
            #   catches chunk boundaries mid-repaint where streamed thinking
            #   text has temporarily pushed the spinner out of the tail
            #   window (a finished turn always ends with bullets as the
            #   freshest non-chrome content).
            lines = clean_output.splitlines()
            last_spinner = max(
                (i for i, line in enumerate(lines) if _is_live_turn_spinner_line(line)),
                default=-1,
            )
            last_bullet = max(
                (i for i, line in enumerate(lines) if re.match(r"\s*•", line)),
                default=-1,
            )
            spinner_in_tail = last_spinner >= 0 and last_spinner >= len(lines) - 15
            if spinner_in_tail or last_spinner > last_bullet:
                return TerminalStatus.PROCESSING

            # Dispatch grace: for a few seconds after send_input(), trust the
            # dispatch over the chrome. The paste repaints the status bar
            # (ready chrome lands LAST in the stream) before the turn's first
            # spinner frame, so the checks above briefly read "ready" ~100ms
            # into a new turn — and the StatusMonitor ready-latch would pin
            # that false COMPLETED until the next input.
            if self._last_dispatch_time and time.time() - self._last_dispatch_time < 5.0:
                return TerminalStatus.PROCESSING

            # The stream looks ready — confirm against the RENDERED pane.
            # A ready-looking chunk boundary is byte-identical mid-turn vs at
            # real completion (measured on captured streams: stale spinner
            # ~21 lines back, bullets 2-3 from the end in BOTH), so the raw
            # stream alone cannot split them. The rendered pane can: tmux's
            # compositor has resolved every in-place redraw, so a spinner
            # glyph visible in the pane tail is live, not stale. Gated to
            # post-dispatch only (boot screens legitimately show braille
            # like '⠧ MCP Servers: 0/1' while idle at the welcome screen,
            # and init readiness is already handled by the stream path).
            if self._last_dispatch_time:
                try:
                    pane_tail = get_backend().get_history(
                        self.session_name,
                        self.window_name,
                        tail_lines=25,
                        strip_escapes=True,
                    )
                    if any(_is_live_turn_spinner_line(line) for line in pane_tail.splitlines()):
                        return TerminalStatus.PROCESSING
                except Exception:
                    # Pane unavailable (deleted window, backend hiccup) —
                    # fall through to the stream-derived ready status.
                    pass

            if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
                return TerminalStatus.ERROR

            return TerminalStatus.COMPLETED if self._has_received_input else TerminalStatus.IDLE

        # --- Legacy emoji-prompt TUI ---
        # Check the bottom lines for the idle prompt.
        # Kimi's TUI has padding lines between prompt and status bar.
        # Use end-of-line anchor (\s*$) to distinguish a bare prompt ("user@dir💫")
        # from a prompt with user input after it ("user@dir💫 some text"),
        # which appears when the user has typed a command.
        all_lines = clean_output.strip().splitlines()
        bottom_lines = all_lines[-IDLE_PROMPT_TAIL_LINES:]
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        has_idle_prompt = any(re.search(idle_prompt_eol, line) for line in bottom_lines)

        # Latch: detect user input to distinguish IDLE from COMPLETED.
        # Supports two formats:
        #
        # Pre-v1.20.0: User input in bordered box (╭─...╰─).
        #   - During PROCESSING (no idle prompt): any ╭─ means user input
        #   - During IDLE/COMPLETED: count ╰─ occurrences (welcome banner = 1, input = 2+)
        #
        # v1.20.0+: User input on prompt line (``💫 message text``).
        #   - Detect prompt emoji followed by non-whitespace text
        if not self._has_received_input:
            # v1.20.0+: prompt line with text after the emoji
            if re.search(PROMPT_WITH_INPUT_PATTERN, clean_output):
                self._has_received_input = True
            # Pre-v1.20.0: input box detection
            elif not has_idle_prompt:
                if re.search(USER_INPUT_BOX_START_PATTERN, clean_output):
                    self._has_received_input = True
            else:
                box_end_count = len(re.findall(USER_INPUT_BOX_END_PATTERN, clean_output))
                if box_end_count >= 2:
                    self._has_received_input = True

        if has_idle_prompt:
            if self._has_received_input:
                # Guard against premature COMPLETED: if processing indicators are
                # visible in the bottom lines, Kimi is still working even though
                # the idle prompt is present. This happens when get_status() is
                # polled in the brief window between task submission and Kimi
                # clearing the prompt to start streaming.
                for line in bottom_lines:
                    stripped = line.strip()
                    # Braille spinner with tool name: "⠼ Using Shell (...)"
                    if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏]\s+Using\s", stripped):
                        return TerminalStatus.PROCESSING
                    # Moon phase emoji alone on a line = thinking indicator
                    if stripped in {"🌑", "🌒", "🌓", "🌔", "🌕", "🌖", "🌗", "🌘"}:
                        return TerminalStatus.PROCESSING
                return TerminalStatus.COMPLETED

            return TerminalStatus.IDLE

        # No idle prompt at bottom — check for errors before assuming processing
        if re.search(ERROR_PATTERN, clean_output, re.MULTILINE):
            return TerminalStatus.ERROR

        # No prompt visible and no error: Kimi is actively processing/streaming
        return TerminalStatus.PROCESSING

    # Opt in to pyte rendered-screen detection (gated by CAO_PYTE_STATUS).
    supports_screen_detection = True

    def get_status_from_screen(self, screen_lines: List[str]) -> TerminalStatus:
        """Detect status from a pyte-composited viewport (escape-free rows).

        The composited screen removes the need for the raw-stream hacks the
        buffer path carries (the get_history re-capture and the dispatch-grace
        window): a spinner visible in the rendered pane tail is unambiguously
        live, and the response bullets are present without eviction. Called by
        the StatusMonitor only on settled / rising-edge frames.
        """
        rows = [ln.rstrip() for ln in screen_lines if ln.strip()]
        if not rows:
            return TerminalStatus.UNKNOWN
        joined = "\n".join(rows)
        tail = rows[-18:]

        # Boot gate: Kimi draws its status bar BEFORE it can accept input —
        # while MCP servers are still connecting it shows "connecting to mcp
        # servers" / "cao-mcp-server (connecting)". Reporting IDLE here is
        # premature: a message delivered in this window is pasted into the boot
        # screen and silently absorbed (observed live — an inbox message
        # delivered 1.3s after a premature IDLE left the receiver stuck). Treat
        # the connecting state as PROCESSING so init waits for a real ready
        # prompt.
        #
        # Scan only NON-bullet lines. This boot chrome renders in the status-bar
        # / spinner region (braille-prefixed status lines, the "connecting to mcp
        # servers" progress line), never as a "•" response bullet. Searching the
        # whole composited screen would re-strand a genuinely COMPLETED turn as
        # PROCESSING whenever its response text merely MENTIONS "(connecting)" /
        # "connecting to mcp servers" — plausible in an MCP orchestrator — and
        # since the boot gate precedes the ready check and re-fires on every
        # settled frame, the inbox (delivers only on IDLE/COMPLETED) would then
        # never deliver to that terminal.
        if any(
            re.search(r"connecting to mcp servers|\(connecting\)", ln, re.IGNORECASE)
            for ln in rows
            if not re.match(r"\s*•", ln)
        ):
            return TerminalStatus.PROCESSING

        # Newest "Kimi Code" TUI: readiness is the status bar / context footer.
        if re.search(NEW_TUI_STATUS_PATTERN, joined):
            if any(_is_live_turn_spinner_line(ln) for ln in tail):
                return TerminalStatus.PROCESSING
            if re.search(ERROR_PATTERN, joined, re.MULTILINE):
                return TerminalStatus.ERROR
            return (
                TerminalStatus.COMPLETED
                if re.search(ANY_BULLET_PATTERN, joined)
                else TerminalStatus.IDLE
            )

        # Legacy emoji-prompt TUI: bare ✨/💫 prompt visible at the bottom.
        if any(re.search(IDLE_PROMPT_PATTERN, ln) for ln in tail):
            return (
                TerminalStatus.COMPLETED
                if re.search(ANY_BULLET_PATTERN, joined)
                else TerminalStatus.IDLE
            )

        if re.search(ERROR_PATTERN, joined, re.MULTILINE):
            return TerminalStatus.ERROR
        # No Kimi TUI chrome on the composited screen at all (boot screen, or a
        # torn-down pane back at the shell). On the RAW path "no prompt = still
        # streaming" is a safe default, but on a fully rendered screen the
        # absence of all TUI chrome means we are NOT looking at an active Kimi
        # turn — so report UNKNOWN rather than a false PROCESSING.
        return TerminalStatus.UNKNOWN

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Kimi's final response from terminal output.

        Supports two formats:

        Pre-v1.20.0 (input box format):
        1. Find the last user input box (╭─...╰─) in clean text
        2. Collect all content between the box end and the next prompt
        3. Filter out thinking bullets (gray ANSI-styled lines)

        v1.20.0+ (inline prompt format):
        1. Find the last prompt-with-input line (``💫 message text``)
        2. Collect all content between that line and the next bare prompt
        3. Filter out thinking bullets

        Fallback for long responses (markers scrolled out of capture):
        - Extract all content from start of capture up to the idle prompt
        - Filter out thinking/status bar lines

        Args:
            script_output: Raw terminal output from tmux capture

        Returns:
            Extracted response text with ANSI codes stripped

        Raises:
            ValueError: If no response content can be extracted
        """
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Work line-by-line for reliable mapping between raw and clean output.
        raw_lines = script_output.split("\n")
        clean_lines = clean_output.split("\n")

        # Strategy 1: Find the last user input box end line (╰─) — pre-v1.20.0
        box_end_idx = None
        # Only consider box-end lines that come AFTER the welcome banner.
        # The welcome banner itself has ╰─, so we skip it by finding the
        # welcome banner line first.
        welcome_idx = 0
        for i, line in enumerate(clean_lines):
            if re.search(WELCOME_BANNER_PATTERN, line):
                welcome_idx = i
        for i in range(welcome_idx + 1, len(clean_lines)):
            if re.search(USER_INPUT_BOX_END_PATTERN, clean_lines[i]):
                box_end_idx = i

        # Strategy 2: Find the last prompt-with-input line — v1.20.0+
        prompt_input_idx = None
        for i, line in enumerate(clean_lines):
            if re.search(PROMPT_WITH_INPUT_PATTERN, line):
                prompt_input_idx = i

        # Choose the best anchor: the LATEST marker wins. The newest "Kimi
        # Code" TUI draws decorative ╰─ boxes during boot (its own welcome box
        # and MCP-server banners like FastMCP's) but renders user messages as
        # ✨-prefixed prompt lines — so a box-end can match boot chrome ABOVE
        # the real message and box-first priority would slice the response
        # from the boot screen. The response always follows the LAST user
        # input, whichever marker style rendered it.
        if box_end_idx is not None and prompt_input_idx is not None:
            response_start = max(box_end_idx, prompt_input_idx) + 1
        elif box_end_idx is not None:
            response_start = box_end_idx + 1
        elif prompt_input_idx is not None:
            response_start = prompt_input_idx + 1
        else:
            # Neither marker found — long response scrolled everything out
            return self._extract_without_input_box(raw_lines, clean_lines)

        # Find where the response ends: the next bare idle prompt
        # (legacy/v1.20 TUIs), or the newest-TUI footer chrome — the
        # "── input ──" box rule or the status bar / context footer
        # (NEW_TUI_STATUS_PATTERN). Without the footer stops, a newest-TUI
        # response would run to end-of-capture and drag the empty input box
        # and status bar into the extracted message.
        idle_prompt_eol = IDLE_PROMPT_PATTERN + r"\s*$"
        new_tui_input_rule = r"^\s*─{2,}\s*input\s*─{2,}"
        prompt_idx = len(clean_lines)  # default: end of output
        for i in range(response_start, len(clean_lines)):
            line = clean_lines[i]
            if (
                re.search(idle_prompt_eol, line)
                or re.match(new_tui_input_rule, line)
                or re.search(NEW_TUI_STATUS_PATTERN, line)
            ):
                prompt_idx = i
                break

        response_end = prompt_idx

        # Collect all non-empty lines for the fallback response
        all_response_lines = [
            clean_lines[i].strip()
            for i in range(response_start, response_end)
            if i < len(clean_lines) and clean_lines[i].strip()
        ]

        if not all_response_lines:
            raise ValueError("Empty Kimi CLI response - no content found after input")

        # Filter out thinking bullets and status bar lines.
        # Thinking bullets have gray ANSI color (38;5;244) in the raw output.
        filtered_lines = []
        for i in range(response_start, response_end):
            raw_line = raw_lines[i] if i < len(raw_lines) else ""
            clean_line = clean_lines[i] if i < len(clean_lines) else ""

            # Skip empty lines
            if not clean_line.strip():
                continue

            # Skip thinking bullets (identified by gray ANSI color in raw output)
            if re.search(THINKING_BULLET_RAW_PATTERN, raw_line):
                continue

            # Skip status bar lines
            if re.search(STATUS_BAR_PATTERN, clean_line):
                continue

            filtered_lines.append(clean_line.strip())

        if not filtered_lines:
            # If all lines were filtered as thinking, fall back to returning
            # all content. This handles edge cases where the response format
            # doesn't match expected patterns.
            return "\n".join(all_response_lines).strip()

        return "\n".join(filtered_lines).strip()

    def _extract_without_input_box(self, raw_lines: list, clean_lines: list) -> str:
        """Fallback extraction when user input box has scrolled out of capture.

        For long responses (>200 lines), the user input box (╭─/╰─) and early
        response content are no longer in the tmux capture window. In this case,
        extract all content from the start of capture up to the last idle prompt,
        filtering out status bar and welcome banner lines.

        Args:
            raw_lines: Raw output split by newlines (ANSI preserved)
            clean_lines: ANSI-stripped output split by newlines

        Returns:
            Extracted response text

        Raises:
            ValueError: If no extractable content found
        """
        # Find the last idle prompt line
        prompt_idx = len(clean_lines)
        for i in range(len(clean_lines) - 1, -1, -1):
            if re.search(IDLE_PROMPT_PATTERN, clean_lines[i]):
                prompt_idx = i
                break

        # Collect content from start to prompt, filtering out TUI chrome
        filtered_lines = []
        for i in range(0, prompt_idx):
            raw_line = raw_lines[i] if i < len(raw_lines) else ""
            clean_line = clean_lines[i] if i < len(clean_lines) else ""

            if not clean_line.strip():
                continue

            # Skip thinking bullets
            if re.search(THINKING_BULLET_RAW_PATTERN, raw_line):
                continue

            # Skip status bar
            if re.search(STATUS_BAR_PATTERN, clean_line):
                continue

            # Skip welcome banner lines
            if re.search(WELCOME_BANNER_PATTERN, clean_line):
                continue

            filtered_lines.append(clean_line.strip())

        if not filtered_lines:
            raise ValueError("No extractable content in Kimi CLI output (input box scrolled out)")

        return "\n".join(filtered_lines).strip()

    def exit_cli(self) -> str:
        """Get the command to exit Kimi CLI.

        Kimi CLI supports several exit commands: /exit, exit, quit, or Ctrl-D.
        We use /exit as it's the most reliable and consistent.
        """
        return "/exit"

    async def extract_session_context(self) -> Dict[str, Any]:
        """Tmux-primary session extraction for Kimi.

        Mirrors the universal pattern used by the other providers
        (Claude Code / Codex / Kiro / Copilot). Returns the locked
        6-field shape from ``_build_context_dict``. Empty tmux history
        returns the LITERAL empty dict ``{}``. All
        emitted strings flow through ``_sanitize_for_log`` at this
        producer layer (sanitised at both produce and consume). Never raises
        out — top-level ``except Exception`` returns ``{}`` with a
        sanitised WARNING. ``KeyboardInterrupt`` and ``SystemExit``
        propagate.
        """
        from cli_agent_orchestrator.services.wiki_compiler import _sanitize_for_log

        try:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                return {}  # literal empty dict, not a populated-empty one

            clean = re.sub(ANSI_CODE_PATTERN, "", output)

            user_messages: list = []
            lines = clean.splitlines()
            i = 0
            while i < len(lines):
                m = self._KIMI_PROMPT_RE.search(lines[i])
                if not m:
                    i += 1
                    continue
                msg_lines: list = []
                # Text after the prompt emoji on the same line.
                after = lines[i][m.end() - 1 :].strip()
                if after:
                    msg_lines.append(after)
                i += 1
                while i < len(lines):
                    if self._KIMI_PROMPT_RE.search(lines[i]) or self._KIMI_RESPONSE_MARKER_RE.match(
                        lines[i]
                    ):
                        break
                    if lines[i].strip():
                        msg_lines.append(lines[i].strip())
                    i += 1
                if msg_lines:
                    user_messages.append(" ".join(msg_lines))

            last_response = ""
            try:
                last_response = self.extract_last_message_from_script(output)
            except ValueError:
                pass

            return self._build_context_dict(
                provider_name="kimi_cli",
                last_task=_sanitize_for_log(user_messages[-1] if user_messages else ""),
                key_decisions=[
                    _sanitize_for_log(s) for s in self._extract_decisions(last_response)
                ],
                open_questions=[
                    _sanitize_for_log(s) for s in self._extract_questions(user_messages)
                ],
                files_changed=[_sanitize_for_log(s) for s in self._extract_file_paths(clean)],
            )
        except (KeyboardInterrupt, SystemExit):
            # Control flow MUST propagate.
            raise
        except Exception as e:
            logger.warning(
                "kimi_extract_session_context_failed reason=%s",
                _sanitize_for_log(str(e))[:200],
            )
            return {}

    def cleanup(self) -> None:
        """Clean up Kimi CLI provider resources.

        Removes any temporary files created for agent profiles
        and resets the initialization state. MCP timeout is NOT restored
        because multiple Kimi instances may share the config file concurrently.
        """
        # Remove temp directory if it was created for agent profile
        if self._temp_dir:
            if os.path.exists(self._temp_dir):
                shutil.rmtree(self._temp_dir, ignore_errors=True)
            self._temp_dir = None

        self._initialized = False
        self._has_received_input = False
