"""Cursor CLI provider implementation.

This module provides the CursorCliProvider class for integrating with the
Cursor CLI (https://cursor.com/cli), Anysphere's terminal-native AI coding
assistant. The CLI is invoked via the ``agent`` binary (Cursor's primary
command per https://cursor.com/docs/cli/overview; ``cursor-agent`` is the
historical/backward-compatible alias still shipped on most installations)
and exposes an interactive REPL with the following features exercised by
this provider:

- System prompt injection via a ``--system-prompt`` flag (newlines escaped
  for safe tmux transport, matching the Claude Code pattern).
- Agent profile selection via ``--agent <name>`` when the CAO agent
  profile maps to a Cursor agent.
- Model override via ``--model <id>`` (e.g. ``gpt-5``, ``sonnet-4``).
- Hard tool approval bypass via ``--force`` (a.k.a. ``--yolo``) for
  headless launches.
- MCP server configuration via ``--mcp <json>`` (Cursor 2025+ format).
- Trust prompt bypass via ``--trust``.
- Skill catalog injection appended to the system prompt via the shared
  ``_apply_skill_prompt`` helper from :class:`BaseProvider`.

Status detection is pattern-based. Cursor CLI uses an Ink-style interactive
prompt (``❯``) for IDLE / COMPLETED, spinner text with ellipsis for
PROCESSING, and a structural "thinking-before-separator" check borrowed
from the Claude Code provider to avoid stale-spinner false positives.
"""

import json
import logging
import re
import shlex
import shutil
from typing import Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# =============================================================================
# Regex Patterns for Cursor CLI Output Analysis
# =============================================================================

# ANSI escape code pattern for stripping terminal colors.
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# Cursor CLI uses the same spinner glyph vocabulary as Claude Code while
# generating a response. Match a spinner char + text + ellipsis on a single
# line. Examples: "⠋ Thinking…", "✶ Reasoning… (esc to interrupt)".
PROCESSING_PATTERN = r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳·][^\n]*\u2026"

# Cursor CLI's REPL prompt is a "❯" character (right arrow) with optional
# space / non-breaking space, identical in shape to Claude Code's prompt.
# The pattern is anchored to the start of a line (with optional SGR colour
# codes before the prompt) so it does NOT match the leading "❯ " on echoed
# user input lines (e.g. "❯ Summarize…") or any "> " inside response
# content. This matches the claude_code provider's _SOL_IDLE_RE pattern.
IDLE_PROMPT_PATTERN = r"^\s*(?:\x1b\[[0-9;]*m)*[❯>](?:\x1b\[[0-9;]*m)*[\s\xa0]"

# Same pattern for log files (no ANSI involved). Still start-of-line
# anchored so the log pre-check is consistent with live status detection.
IDLE_PROMPT_PATTERN_LOG = r"^\s*[❯>][\s\xa0]"

# Footer shown while a TUI selection widget is active (mode picker, model
# picker, file completion overlay).
WAITING_USER_ANSWER_PATTERN = r"↑/↓ to navigate"

# Workspace trust dialog (Cursor asks once per directory the first time the
# agent is launched there).
TRUST_PROMPT_PATTERN = r"do you trust (?:the )?files? in this folder|confirm folder trust"

# Permission / approval dialog that appears when the agent wants to run a
# shell command or edit a file without ``--force`` enabled.
PERMISSION_PROMPT_PATTERN = (
    r"(?:do you want to (?:allow|run)|approve this action|\[\s*y\s*/\s*n\s*\])"
)

# Separator regex. Matches a contiguous run of at least 20 box-drawing
# horizontal characters (U+2500), with an optional CSI escape between any
# two consecutive dashes. This is the correct "CSI interleaved with the
# separator" pattern: Cursor's TUI re-renders the separator in place
# with new colour escapes on every prompt, so the byte stream looks
# like:
#   ─\x1b[0m──\x1b[38;5;245m──\x1b[0m──…
# The pattern is anchored to a full line (``^…$``) so we don't match
# a stray dash sequence inside response content.
# Intermediate bytes are restricted to the ECMA-48 param range
# (0x30-0x3F) so a stray ``ESC [`` introducer is not consumed.
SEPARATOR_PATTERN = (
    r"^(?:\x1b\[[\x30-\x3F]*[\x40-\x7E])?(?:\u2500(?:\x1b\[[\x30-\x3F]*[\x40-\x7E])?){20,}$"
)


class CursorCliProvider(BaseProvider):
    """Provider for the Cursor CLI (``agent`` / ``cursor-agent``).

    The provider launches Cursor with the primary ``agent`` command
    (Cursor's documented top-level entrypoint per
    https://cursor.com/docs/cli/overview). The ``cursor-agent`` alias is
    still shipped for backward compatibility and resolves to the same
    binary.

    Manages the lifecycle of a Cursor CLI REPL session inside a tmux
    window: initialization, status detection, response extraction, and
    cleanup.

    Attributes:
        terminal_id: Unique identifier for this terminal instance.
        session_name: Name of the tmux session containing this terminal.
        window_name: Name of the tmux window for this terminal.
        _agent_profile: Optional Cursor agent name (e.g. ``"developer"``).
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
        """Initialize the Cursor CLI provider.

        Args:
            terminal_id: Unique identifier for this terminal.
            session_name: Name of the tmux session.
            window_name: Name of the tmux window.
            agent_profile: Optional Cursor agent name (e.g. ``"developer"``).
            allowed_tools: Optional list of CAO tool names the agent is
                allowed to use. Cursor CLI does not expose a native
                ``--disallowedTools`` flag, so restrictions are enforced
                softly via the ``SECURITY_PROMPT`` (see
                :data:`cli_agent_orchestrator.constants.SECURITY_PROMPT`).
            model: Optional model override (e.g. ``"gpt-5"``, ``"sonnet-4"``).
            skill_prompt: Optional skill catalog text built by the service
                layer. Appended to the system prompt at launch.
        """
        super().__init__(terminal_id, session_name, window_name, allowed_tools, skill_prompt)
        self._initialized = False
        self._agent_profile = agent_profile
        self._model = model

    @property
    def paste_enter_count(self) -> int:
        """Cursor CLI submits on a single Enter after bracketed paste."""
        return 1

    def _build_cursor_command(self) -> str:
        """Build the ``agent`` (Cursor CLI) launch command.

        Cursor's primary command per the official documentation is
        ``agent`` (https://cursor.com/docs/cli/overview). The legacy
        ``cursor-agent`` binary resolves to the same REPL; we prefer
        the primary name so newly-installed machines work out of the
        box.

        Flags used:
        - ``--force`` auto-approves tool calls so the agent does not
          block on per-tool approval prompts during orchestration.
        - ``--trust`` accepts the workspace-trust dialog on first run.
        - ``--approve-mcps`` pre-approves MCP servers declared on the
          command line (Cursor prompts for each MCP server otherwise).
        - ``--system-prompt`` injects the agent's CAO system prompt
          (with the skill catalog appended).
        - ``--agent`` selects a Cursor-side agent (when configured).
        - ``--model`` selects a specific model (when configured).
        - ``--mcp`` injects MCP server configuration.

        Returns a properly escaped shell command string suitable for
        :func:`tmux_client.send_keys`. Uses :func:`shlex.join` to handle
        multiline strings and special characters correctly.
        """
        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as exc:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {exc}")

        # Resolve the binary: prefer Cursor's documented primary
        # command ``agent``; fall back to the legacy ``cursor-agent``
        # alias when only that one is installed (e.g. older macOS
        # installs pinned to the historical name). The e2e skip
        # fixture in test/e2e/conftest.py::require_cursor accepts
        # either name, so the launch should behave consistently.
        if shutil.which("agent"):
            binary = "agent"
        elif shutil.which("cursor-agent"):
            binary = "cursor-agent"
        else:
            raise ProviderError(
                "Cursor CLI not found: neither 'agent' nor 'cursor-agent' is on $PATH. "
                "Install from https://cursor.com/cli"
            )

        command_parts = [binary]

        # Approval + trust flags. We always pass --force when running
        # under CAO so per-tool approval prompts do not block handoff /
        # assign flows. --trust prevents the first-run trust dialog
        # from blocking the REPL.
        command_parts.append("--force")

        # Model override (--model, when explicitly set or supplied by
        # the agent profile's `model` field). Profile.model takes
        # precedence when set, then the constructor-provided
        # ``self._model``.
        model = self._model
        if profile is not None and profile.model:
            model = profile.model
        if model:
            command_parts.extend(["--model", model])

        # Agent selection (Cursor-side agent).
        if self._agent_profile:
            command_parts.extend(["--agent", self._agent_profile])

        # System prompt injection. Escape newlines so tmux send_keys
        # chunking cannot break the command. Prepend SECURITY_PROMPT
        # when tool restrictions are active (Cursor does not yet expose
        # a --disallowedTools equivalent).
        if profile is not None:
            system_prompt = profile.system_prompt or ""
            system_prompt = self._apply_skill_prompt(system_prompt)

            # Soft tool-restriction enforcement: when the operator has
            # set an explicit (non-wildcard) allowlist, prepend the
            # shared SECURITY_PROMPT plus a tool list to the system
            # prompt. See skills/cao-provider/references/lessons-learnt.md
            # #13 for the three enforcement approaches.
            if self._allowed_tools and "*" not in self._allowed_tools:
                from cli_agent_orchestrator.constants import SECURITY_PROMPT

                tools_list = ", ".join(self._allowed_tools)
                tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

            if system_prompt:
                escaped_prompt = system_prompt.replace("\\", "\\\\").replace("\n", "\\n")
                command_parts.extend(["--system-prompt", escaped_prompt])

        # MCP server injection. Forward CAO_TERMINAL_ID so MCP servers
        # (e.g. cao-mcp-server) can identify the current terminal for
        # handoff / assign operations. Cursor's --mcp flag accepts a
        # JSON object of {name: config}.
        if profile is not None and profile.mcpServers:
            mcp_config: dict = {}
            for server_name, server_config in profile.mcpServers.items():
                if isinstance(server_config, dict):
                    mcp_config[server_name] = dict(server_config)
                else:
                    mcp_config[server_name] = server_config.model_dump(exclude_none=True)

                env = mcp_config[server_name].get("env", {})
                if "CAO_TERMINAL_ID" not in env:
                    env["CAO_TERMINAL_ID"] = self.terminal_id
                    mcp_config[server_name]["env"] = env

            mcp_json = json.dumps({"mcpServers": mcp_config})
            command_parts.extend(["--mcp", mcp_json])
            # --approve-mcps is required to skip per-server approval
            # dialogs on first run; otherwise the REPL blocks.
            command_parts.append("--approve-mcps")

        return shlex.join(command_parts)

    async def initialize(self) -> bool:
        """Initialize the Cursor CLI provider by starting ``agent``.

        This method:
        1. Waits for the shell prompt to appear in the tmux window.
        2. Sends the ``agent`` command with the configured agent
           profile, model, system prompt, and MCP config.
        3. Waits for the agent to reach IDLE / COMPLETED state.

        Returns:
            True if initialization was successful.

        Raises:
            TimeoutError: If shell or Cursor CLI initialization times out.
        """
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = self._build_cursor_command()
        # Arm the StatusMonitor stickiness gate so the launching
        # command can drive a fresh PROCESSING transition past any
        # stale ready latch. Without this, a previously-latched
        # IDLE/COMPLETED would suppress the genuine PROCESSING
        # transition that follows once Cursor starts loading.
        # Imported lazily to avoid a circular import: the
        # status_monitor module imports provider_manager, which
        # imports this module.
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Wait for Cursor CLI to fully initialize. Accept both IDLE
        # and COMPLETED — some versions render a startup message that
        # get_status() interprets as a completed response.
        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=30.0,
        ):
            raise TimeoutError("Cursor CLI initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, output: Optional[str]) -> TerminalStatus:
        """Get Cursor CLI status by analyzing terminal output.

        Called by StatusMonitor with the accumulated terminal output
        buffer (the raw pipe-pane byte stream). Status detection checks
        patterns in priority order:

        1. Empty / None output → UNKNOWN
        2. PROCESSING — structural spinner-before-separator check
        3. PROCESSING — fallback spinner visible with no separator
        4. WAITING_USER_ANSWER — TUI selection footer or trust /
           permission prompt
        5. IDLE / COMPLETED — idle prompt present without prior
           processing indicators
        6. UNKNOWN — fallback when no marker matches

        Args:
            output: Raw terminal output (rolling buffer, up to ~8KB).

        Returns:
            Current TerminalStatus.
        """
        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place
        # redraws), not just SGR colour codes — otherwise cursor
        # sequences survive and the structural checks below misfire on
        # the raw stream.
        clean = strip_terminal_escapes(output)

        # PRIMARY PROCESSING check: walk backwards from the *last*
        # separator. If a spinner line appears before another
        # separator, the agent is actively processing. If we hit
        # another separator first, the spinner is from a completed
        # task and should be ignored. Mirrors the Claude Code
        # provider's structural fix for the stale-spinner bug.
        # The separator regex tolerates any CSI sequence interleaved
        # *between* the box-drawing characters — Cursor re-renders
        # the separator with new colour escapes on every prompt, and
        # we must not miss it. Intermediate bytes are restricted to
        # the ECMA-48 param range (0x30-0x3F) so a stray ``ESC [``
        # is not consumed. The pattern is anchored to a full line
        # (``^...$``) so a stray dash sequence inside response
        # content is not matched.
        # The separator regex is anchored to a full line (``^…$``);
        # the ``MULTILINE`` flag is required so ``^`` and ``$``
        # match at every line start/end, not just the buffer
        # start/end.
        _sep_re = re.compile(SEPARATOR_PATTERN, re.MULTILINE)
        _sep_positions = [m.start() for m in _sep_re.finditer(clean)]
        if _sep_positions:
            pre_sep_lines = clean[: _sep_positions[-1]].rstrip("\n").split("\n")
            for line in reversed(pre_sep_lines):
                if re.search(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✶✢✽✻✳·][^\n]*\u2026", line):
                    return TerminalStatus.PROCESSING
                if _sep_re.search(line):
                    break

        # Find the LAST occurrence of each marker for fallback
        # position checks. ``IDLE_PROMPT_PATTERN`` is line-anchored
        # (see its definition) so re.findall across the full buffer
        # only matches true prompt lines, never the leading
        # ``❯ <text>`` of an echoed user input line.
        last_processing = None
        for m in re.finditer(PROCESSING_PATTERN, clean):
            last_processing = m

        last_idle = None
        for m in re.finditer(IDLE_PROMPT_PATTERN, clean, re.MULTILINE):
            last_idle = m

        # FALLBACK PROCESSING: spinner visible AND no separator follows
        # it yet (early in execution before the separator appears).
        if last_processing and not _sep_re.search(clean):
            if last_idle is None or last_processing.start() > last_idle.start():
                return TerminalStatus.PROCESSING

        # Check for active TUI selection widgets (mode picker, model
        # picker, etc.) which show a ↑/↓ navigation footer. Exclude
        # the trust/permission dialogs, which are separate states.
        if (
            re.search(WAITING_USER_ANSWER_PATTERN, clean)
            and not re.search(TRUST_PROMPT_PATTERN, clean, re.IGNORECASE)
            and not re.search(PERMISSION_PROMPT_PATTERN, clean, re.IGNORECASE)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # Trust / permission dialogs are an interactive prompt that
        # blocks the agent until the operator accepts. Treat them as
        # WAITING_USER_ANSWER.
        if re.search(TRUST_PROMPT_PATTERN, clean, re.IGNORECASE) or re.search(
            PERMISSION_PROMPT_PATTERN, clean, re.IGNORECASE
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # IDLE / COMPLETED: an idle prompt at the bottom of the buffer
        # indicates the agent has finished its turn (or is freshly
        # initialized and waiting for input). We do not differentiate
        # IDLE vs COMPLETED on the position of an idle prompt alone —
        # message extraction is the source of truth for the response
        # payload; CAO's status machine only needs to know the agent
        # is no longer working. We report COMPLETED to match the
        # other providers' convention (the supervisor inbox accepts
        # COMPLETED as a "ready" signal).
        if last_idle:
            return TerminalStatus.COMPLETED

        return TerminalStatus.UNKNOWN

    def get_idle_pattern_for_log(self) -> str:
        """Return Cursor CLI IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the last assistant response from the terminal output.

        Cursor CLI does not emit a single canonical response marker
        (unlike Claude Code's ``⏺``), so we use the structural
        separator + trailing prompt pattern. The terminal buffer has
        the shape::

            ──────────────────────────
            ❯ <user question>
            ──────────────────────────
            <assistant response>
            ──────────────────────────
            ❯

        The assistant response is the text between the *second* and
        *third* separators (or equivalently, between the user's
        ``❯ <text>`` question and the next ``❯`` idle prompt).

        The separator regex tolerates SGR colour codes interleaved
        between the ``─`` characters (Cursor's TUI redraws the
        separator in place with new colour escapes on every prompt).
        All remaining terminal escapes (cursor positioning, OSC, \r
        redraws) are stripped from the response region before
        returning so the extracted text is the rendered output, not
        the raw byte stream. We do NOT use
        :func:`cli_agent_orchestrator.utils.text.strip_terminal_escapes`
        because that function normalises ``\\r`` to ``\\n`` and would
        split single-line spinner frames into multiple lines — see
        its docstring. Extraction operates on rendered (capture-pane)
        output, not the raw FIFO stream.

        Raises:
            ValueError: When no response boundary is detected.
        """
        # Match a separator line, with ANY CSI sequence (not just
        # SGR) interleaved between the box-drawing characters.
        # Cursor re-renders the separator with new colour escapes
        # on every prompt, so the byte stream looks like
        # ``\x1b[38;5;245m──\x1b[0m──\x1b[38;5;245m──`` — the
        # pattern must accept CSI inside the dash run, not just
        # before it. Intermediate bytes are restricted to the
        # ECMA-48 param range (0x30-0x3F) so a stray ``ESC [`` is
        # not consumed. The pattern is anchored to a full line
        # (``^...$``) so a stray dash sequence inside response
        # content is not matched.
        # The separator regex is anchored to a full line (``^…$``);
        # the ``MULTILINE`` flag is required so ``^`` and ``$``
        # match at every line start/end, not just the buffer
        # start/end.
        _sep_re = re.compile(SEPARATOR_PATTERN, re.MULTILINE)
        separators = list(_sep_re.finditer(script_output))
        idle_matches = list(re.finditer(IDLE_PROMPT_PATTERN, script_output, re.MULTILINE))

        if not separators or not idle_matches:
            raise ValueError(
                "No Cursor CLI response found - no separator / idle prompt boundary detected"
            )

        # Anchor on the trailing idle prompt (the last ❯ in the
        # buffer). The response ends at this prompt. Walk back to
        # find the separator that immediately precedes the response.
        # The earlier check guarantees that ``separators`` and
        # ``idle_matches`` are both non-empty, so at least one
        # separator is guaranteed to be before the trailing
        # prompt (an idle prompt at position 0 would be paired
        # with no separators, which the earlier check rejects).
        final_prompt = idle_matches[-1]

        # Find the last separator that comes BEFORE the trailing
        # prompt. That separator marks the end of the response.
        end_sep: Optional[re.Match[str]] = None
        for sep in reversed(separators):
            if sep.start() < final_prompt.start():
                end_sep = sep
                break

        assert end_sep is not None  # see comment above

        # The response starts at the separator before end_sep (which
        # marks the start of the response region) — or at the start
        # of the buffer if there is no such separator. This avoids
        # leaking the user's question into the extracted response.
        start_sep: Optional[re.Match[str]] = None
        for sep in reversed(separators):
            if sep.start() < end_sep.start():
                start_sep = sep
                break

        start = start_sep.end() if start_sep is not None else 0
        response = script_output[start : end_sep.start()]

        # Strip ALL terminal escape sequences from the extracted
        # region (not just SGR colours). Cursor CLI re-renders
        # cursor-positioning sequences inside the response area
        # during long generations, and OSC title updates can leak
        # into the captured text. We deliberately do not use
        # ``strip_terminal_escapes`` here because that function
        # normalises ``\r`` → ``\n`` (suitable for status detection
        # but destructive for response extraction).
        #
        # The regex follows the ECMA-48 escape grammar:
        #   CSI:  ESC [  <param-bytes 0x30-0x3F>*  <final-byte 0x40-0x7E>
        #   OSC:  ESC ]  <payload>        BEL  |  ESC \
        #   2-byte ESC: ESC <0x20-0x2F>+  <0x30-0x7E>
        # Intermediate bytes are restricted to 0x20-0x3F so that
        # plain ``ESC [`` (e.g. CSI introducer with no params) is
        # only consumed when followed by a real final byte.
        _full_esc_re = re.compile(
            r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"
            r"|\x1b\[[\x30-\x3F]*[\x40-\x7E]"
            r"|\x1b[\x20-\x2F]+[\x30-\x7E]"
        )
        response = _full_esc_re.sub("", response).strip()

        if not response:
            raise ValueError("Empty Cursor CLI response - no content found between separators")

        return response

    def exit_cli(self) -> str:
        """Get the command to exit Cursor CLI.

        Cursor CLI exits on ``/exit`` (slash command) or Ctrl+D
        (double-press). ``/exit`` is the more reliable programmatic
        path and matches the convention used by the other providers.
        """
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Cursor CLI provider state."""
        self._initialized = False
