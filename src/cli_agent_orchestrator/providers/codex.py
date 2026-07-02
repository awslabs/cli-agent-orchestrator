"""Codex CLI provider implementation."""

import asyncio
import logging
import re
import shlex
import time
from typing import Any, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.services.settings_service import get_server_settings
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status
from cli_agent_orchestrator.utils.text import strip_terminal_escapes

logger = logging.getLogger(__name__)

# Regex patterns for Codex output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
IDLE_PROMPT_PATTERN = r"(?:❯|›|codex>)"
# Number of lines from the bottom of capture to check for the idle prompt.
# With --no-alt-screen, codex output is inline (scrollback contains history),
# so we can't anchor to \Z. Instead, check the last few lines where the prompt
# and status bar appear.
IDLE_PROMPT_TAIL_LINES = 5
# The idle prompt character ❯ (U+276F) is rendered on-screen by capture-pane
# but is NOT written to the raw output stream captured by pipe-pane.  Instead,
# the TUI footer text "? for shortcuts" is reliably present whenever the TUI
# is active.  This is intentionally permissive — _has_idle_pattern() is a
# lightweight pre-check; the real status decision is made by get_status()
# which uses capture-pane (rendered screen).
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
# [^\S\n]* matches horizontal whitespace only (not newlines) so the match anchors
# on the actual bullet line — using \s* would let the match start on a blank
# line above the bullet, breaking per-line tool-call filtering downstream.
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|[^\S\n]*•)"
# MCP tool call marker emitted by Codex when invoking a tool, e.g.
# "• Called cao-mcp-server.load_skill({...})". The body that follows
# (└ ... lines) is the tool's return value, not the model's reply.
# Used to skip these markers when locating the actual response start.
# The "<server>.<tool>(" shape (identifier.identifier followed by an open
# paren) is required so legitimate model bullets like "• Called attention
# to the bug" don't get filtered as tool calls.
MCP_TOOL_CALL_PATTERN = r"^[^\S\n]*•\s+Called\s+[\w-]+\.[\w-]+\("
# Match user input: "You ..." (label style) or "› text" (Codex interactive prompt).
# The "›[^\S\n]*\S" alternative requires a non-whitespace character on the same line
# to distinguish user input ("› what is your role?") from the empty idle prompt ("› ").
# [^\S\n] matches horizontal whitespace only (spaces/tabs), preventing the pattern
# from crossing newline boundaries into subsequent lines.
USER_PREFIX_PATTERN = r"^(?:You\b|›[^\S\n]*\S)"
# Strict idle prompt pattern for extraction: matches empty prompt lines only.
# Distinguishes "› " (idle) from "› user message" (user input with text).
IDLE_PROMPT_STRICT_PATTERN = r"^\s*(?:❯|›|codex>)\s*$"

PROCESSING_PATTERN = r"\b(thinking|working|running|executing|processing|analyzing)\b"
WAITING_PROMPT_PATTERN = r"^(?:Approve|Allow)\b.*\b(?:y/n|yes/no|yes|no)\b"
ERROR_PATTERN = r"^(?:Error:|ERROR:|Traceback \(most recent call last\):|panic:)"

# Codex TUI footer indicators (status bar below the idle prompt).
# Used to detect when the bottom lines contain TUI chrome rather than user input.
# v0.110 and earlier: "? for shortcuts" and "N% context left"
# v0.111+: "model · N% left · path" (PR #13202 restored draft footer hints)
# v0.136+: "model · path" (the "N% left" segment was removed)
# The "·\s+[~/]" alternative anchors on the path component of the footer,
# which is shared across v0.111 and v0.136 status bars.
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left|\d+%\s+left|·\s+[~/])"
# Codex TUI progress spinner: "• Working (0s • esc to interrupt)",
# "• Thinking (2s ...)", "• Starting script creation (10s • esc to interrupt)".
# The prefix text varies but the "(Ns • esc to interrupt)" format is consistent.
# Appears inline with --no-alt-screen when the agent is actively processing.
# Must be checked before COMPLETED to avoid false positives (the • matches
# ASSISTANT_PREFIX_PATTERN and the TUI footer › matches idle prompt).
TUI_PROGRESS_PATTERN = r"•.*\(\d+s\s*•\s*esc to interrupt\)"

# Workspace trust/approval prompt shown when Codex opens a new directory
TRUST_PROMPT_PATTERN = r"allow Codex to work in this folder"
# Codex welcome banner indicating normal startup (no trust prompt)
CODEX_WELCOME_PATTERN = r"OpenAI Codex"


def _compute_tui_footer_cutoff(all_lines: list) -> int:
    """Compute the character position where the TUI footer area starts.

    Scans backward from the last line to find the TUI footer status bar
    (matches TUI_FOOTER_PATTERN), then continues upward to include any
    blank lines and the suggestion hint line (› with text) that appear
    above the status bar as part of the footer area.

    Returns the character position in the joined text (``'\\n'.join(all_lines)``)
    where the footer starts. Returns ``len('\\n'.join(all_lines))`` if no
    footer is found.
    """
    n = len(all_lines)
    footer_start_idx = n

    # Find the status bar line (last TUI_FOOTER_PATTERN match in the bottom area)
    for i in range(n - 1, max(n - IDLE_PROMPT_TAIL_LINES - 1, -1), -1):
        if re.search(TUI_FOOTER_PATTERN, all_lines[i]):
            footer_start_idx = i
            break

    if footer_start_idx == n:
        return len("\n".join(all_lines))

    # Scan upward from the status bar to include blank lines and the
    # suggestion hint (› with text) that are part of the TUI footer chrome.
    for j in range(footer_start_idx - 1, max(footer_start_idx - 4, -1), -1):
        line = all_lines[j]
        if not line.strip():
            footer_start_idx = j
        elif re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line):
            footer_start_idx = j
            break
        else:
            break

    return len("\n".join(all_lines[:footer_start_idx]))


def _toml_scalar(value: Any) -> str:
    """Serialize a Python scalar to a TOML literal for a ``-c key=<value>`` override.

    Strings become quoted TOML basic strings (backslash, quote, tab, CR, and newline escaped so
    tmux ``send_keys`` keeps the launch command on one line); bools become
    ``true``/``false``; ints and floats are emitted bare. Non-scalar values (dict/list/None) raise ``TypeError`` so a misconfigured profile fails fast. ``bool`` is checked
    before ``int`` because ``bool`` is a subclass of ``int`` in Python, so the
    order here is load-bearing — a flipped order would render ``True`` as ``1``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if not isinstance(value, str):
        raise TypeError(
            "codexConfig values must be scalars (str, bool, int, or float); "
            f"got {type(value).__name__}"
        )
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )
    return f'"{escaped}"'


_CODEX_CONFIG_KEY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


def _toml_override(key: str, value: Any) -> str:
    """Build one ``key=<toml-scalar>`` Codex ``-c`` override, validating the key.

    Keys must be non-empty dotted config paths over ``[A-Za-z0-9_.-]`` (e.g.
    ``features.fast_mode``); spaces, ``=``, quotes, or control characters are
    rejected so a misconfigured profile fails fast instead of silently emitting
    a malformed ``-c`` override. Value-serialization failures from
    :func:`_toml_scalar` are re-raised with the offending key for context.
    """
    if not isinstance(key, str) or not _CODEX_CONFIG_KEY_PATTERN.match(key):
        raise ValueError(
            f"Invalid codexConfig key {key!r}: must be a dotted config path over "
            "[A-Za-z0-9_.-] (e.g. 'features.fast_mode')"
        )
    try:
        return f"{key}={_toml_scalar(value)}"
    except TypeError as exc:
        raise TypeError(f"codexConfig key '{key}': {exc}") from exc


def _find_assistant_marker(text: str) -> Optional[re.Match[str]]:
    """Find the first ASSISTANT_PREFIX_PATTERN match in ``text`` whose line
    is not an MCP tool-call marker.

    Codex emits ``• Called <server>.<tool>(...)`` when invoking an MCP tool;
    that bullet matches ASSISTANT_PREFIX_PATTERN but is followed by tool
    output, not the model's reply. Anchoring on it would conflate tool
    output with the model response (status: false COMPLETED;
    extraction: skill-body leak).
    """
    for m in re.finditer(ASSISTANT_PREFIX_PATTERN, text, re.IGNORECASE | re.MULTILINE):
        line_end = text.find("\n", m.start())
        if line_end == -1:
            line_end = len(text)
        line = text[m.start() : line_end]
        if re.match(MCP_TOOL_CALL_PATTERN, line):
            continue
        return m
    return None


class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


class CodexProvider(BaseProvider):
    """Provider for Codex CLI tool integration."""

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

    def _build_codex_command(self) -> str:
        """Build Codex command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses codex's -c developer_instructions flag to inject agent system prompts.
        """
        # --yolo (alias for --dangerously-bypass-approvals-and-sandbox)
        # is the default because CAO runs codex non-interactively in tmux
        # where approval prompts would block handoff/assign. Profiles can
        # opt out via `codexProfile` (names a [profiles.<name>] block in
        # ~/.codex/config.toml), unless unrestricted allowed tools are enabled.
        # In practice, allowed_tools containing "*" is treated as yolo mode
        # and overrides codexProfile in the same way as an explicit yolo launch.
        yolo = bool(self._allowed_tools and "*" in self._allowed_tools)

        profile = None
        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)
            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        if profile and profile.codexProfile and not yolo:
            command_parts = ["codex", "--profile", profile.codexProfile]
        else:
            command_parts = ["codex", "--yolo"]
        command_parts.extend(["--no-alt-screen", "--disable", "shell_snapshot"])

        if profile is not None:
            if profile.model:
                command_parts.extend(["--model", profile.model])

            system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
            system_prompt = self._apply_skill_prompt(system_prompt)

            # Prepend security constraints for soft enforcement (Codex has no
            # native tool restriction mechanism). Only applied when tool
            # restrictions are active (not unrestricted "*").
            if self._allowed_tools and "*" not in self._allowed_tools:
                from cli_agent_orchestrator.constants import SECURITY_PROMPT

                tools_list = ", ".join(self._allowed_tools)
                tool_constraint = f"\nYou only have access to these tools: {tools_list}\n"
                system_prompt = SECURITY_PROMPT + tool_constraint + system_prompt

            if system_prompt:
                # Codex accepts developer_instructions via -c config override.
                # This is injected as a developer role message before AGENTS.md content.
                # Escape backslashes, double quotes, and newlines for TOML basic string.
                # Newlines must become literal \n to prevent tmux send_keys from
                # splitting the command across multiple lines.
                escaped_prompt = (
                    system_prompt.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
                )
                command_parts.extend(["-c", f'developer_instructions="{escaped_prompt}"'])

            # Add MCP servers via -c config overrides (per-session, no global config changes).
            # Each server field is set via dotted path: mcp_servers.<name>.<field>=<value>
            if profile.mcpServers:
                for server_name, server_config in profile.mcpServers.items():
                    prefix = f"mcp_servers.{server_name}"
                    if isinstance(server_config, dict):
                        cfg = server_config
                    else:
                        cfg = server_config.model_dump(exclude_none=True)
                    if "command" in cfg:
                        command_parts.extend(["-c", f'{prefix}.command="{cfg["command"]}"'])
                    if "args" in cfg:
                        args_toml = "[" + ", ".join(f'"{a}"' for a in cfg["args"]) + "]"
                        command_parts.extend(["-c", f"{prefix}.args={args_toml}"])
                    if "env" in cfg and cfg["env"]:
                        for env_key, env_val in cfg["env"].items():
                            command_parts.extend(["-c", f'{prefix}.env.{env_key}="{env_val}"'])
                    # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                    # can identify the current session for handoff/assign operations.
                    # Codex does not forward env vars to MCP subprocesses by default;
                    # env_vars lists names to inherit from the parent shell environment.
                    env_vars = cfg.get("env_vars", [])
                    if "CAO_TERMINAL_ID" not in env_vars:
                        env_vars = list(env_vars) + ["CAO_TERMINAL_ID"]
                    env_vars_toml = "[" + ", ".join(f'"{v}"' for v in env_vars) + "]"
                    command_parts.extend(["-c", f"{prefix}.env_vars={env_vars_toml}"])
                    # Set a generous tool timeout for MCP calls like handoff, which
                    # create a new terminal, initialize the provider, send a message,
                    # wait for the agent to complete, and extract the output.
                    # Codex defaults to 60s which is too short for multi-step operations.
                    # Value MUST be a TOML float (600.0, not 600) because Codex
                    # deserializes tool_timeout_sec via Option<f64>; a TOML integer
                    # is silently rejected and falls back to the 60s default.
                    if "tool_timeout_sec" not in cfg:
                        command_parts.extend(["-c", f"{prefix}.tool_timeout_sec=600.0"])

            # Inline Codex config overrides (-c key=value). Lets a profile set
            # per-agent Codex knobs — reasoning effort, service tier, fast mode,
            # etc. — without editing the global ~/.codex/config.toml or
            # maintaining named profile files. Keys may be dotted config paths
            # (e.g. "features.fast_mode"); values are serialized to TOML
            # scalars. Emitted last so they take precedence over CAO's own
            # overrides and the profile/config defaults on key conflicts.
            if profile.codexConfig:
                for key, value in profile.codexConfig.items():
                    command_parts.extend(["-c", _toml_override(key, value)])

        return shlex.join(command_parts)

    async def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Auto-accept the workspace trust prompt if it appears.

        Codex shows a folder approval dialog when opening a new directory.
        This sends Enter to accept the default option (allow Codex to work).
        CAO assumes the user trusts the working directory since they confirmed
        workspace access during the launch command.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = get_backend().get_history(self.session_name, self.window_name)
            if not output:
                await asyncio.sleep(1.0)
                continue

            # Clean ANSI codes for reliable text matching
            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                from cli_agent_orchestrator.services.status_monitor import status_monitor

                logger.info("Codex workspace trust prompt detected, auto-accepting")
                status_monitor.notify_input_sent(self.terminal_id)
                get_backend().send_special_key(self.session_name, self.window_name, "Enter")
                return

            # Check if Codex has fully started (welcome banner visible)
            if re.search(CODEX_WELCOME_PATTERN, clean_output):
                logger.info("Codex started without trust prompt")
                return

            await asyncio.sleep(1.0)
        logger.warning("Codex trust prompt handler timed out")

    async def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        from cli_agent_orchestrator.services.status_monitor import status_monitor

        init_timeout = get_server_settings()["provider_init_timeout"]
        if not await wait_for_shell(self.terminal_id, timeout=init_timeout):
            raise TimeoutError(f"Shell initialization timed out after {init_timeout}s")

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        # Arm the StatusMonitor stickiness gate: each send_keys here represents
        # external input that must be allowed to drive PROCESSING transitions
        # past any previously-latched ready state.
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, "echo ready")
        await asyncio.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        status_monitor.notify_input_sent(self.terminal_id)
        get_backend().send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        await self._handle_trust_prompt(timeout=20.0)

        if not await wait_until_status(
            self.terminal_id,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=float(get_server_settings()["provider_init_timeout"]),
            polling_interval=1.0,
        ):
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, output: str) -> TerminalStatus:
        # Native status (herdr): trust the backend's agent state when available;
        # on herdr the buffer is never fed, so buffer parsing can't leave UNKNOWN.
        native = self._resolve_native_status()
        if native is not None:
            return native

        if not output:
            return TerminalStatus.UNKNOWN

        # Strip the RAW pipe-pane escapes (cursor positioning, in-place redraws),
        # not just SGR colour codes — otherwise cursor sequences survive and the
        # idle ``›`` prompt / structural checks below misfire on the raw stream.
        clean_output = strip_terminal_escapes(output)
        tail_output = "\n".join(clean_output.splitlines()[-25:])

        # Search for user messages, excluding the Codex TUI footer when present.
        # The TUI footer (idle prompt hint like "› Summarize recent commits" +
        # status bar "? for shortcuts / context left") can contain › followed by
        # suggestion text, which USER_PREFIX_PATTERN would incorrectly match as
        # user input, preventing COMPLETED detection.
        # Only apply the cutoff when TUI footer indicators are actually present
        # to avoid over-excluding in short outputs or test fixtures.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        # Skip MCP tool-call markers — those mark "model invoked a tool", not
        # "model has replied", and shouldn't gate WAITING/ERROR detection.
        assistant_after_last_user = bool(
            last_user and _find_assistant_marker(output_after_last_user) is not None
        )

        # Check trust prompt early — the trust menu uses › which matches the idle prompt
        # pattern, and PROCESSING_PATTERN matches "running" in "You are running Codex in..."
        if re.search(TRUST_PROMPT_PATTERN, clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # Check bottom of captured output for idle prompt.
        # With --no-alt-screen, scrollback contains history so we can't anchor
        # to end-of-string. Instead, check only the last few lines.
        bottom_lines = clean_output.strip().splitlines()[-IDLE_PROMPT_TAIL_LINES:]
        has_idle_prompt_at_end = any(
            re.match(rf"\s*{IDLE_PROMPT_PATTERN}", line, re.IGNORECASE) for line in bottom_lines
        )

        # Only treat ERROR/WAITING prompts as actionable if they appear after the last user message
        # and are not part of an assistant response.
        if last_user is not None:
            if not assistant_after_last_user:
                if re.search(
                    WAITING_PROMPT_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.WAITING_USER_ANSWER
                if re.search(
                    ERROR_PATTERN,
                    output_after_last_user,
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.ERROR
        else:
            if re.search(WAITING_PROMPT_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.WAITING_USER_ANSWER
            if re.search(ERROR_PATTERN, tail_output, re.IGNORECASE | re.MULTILINE):
                return TerminalStatus.ERROR
        if has_idle_prompt_at_end:
            # Check for TUI progress indicator ("• Working (0s • esc to interrupt)").
            # With --no-alt-screen, the TUI footer (› hint + status bar) is always
            # rendered at the bottom, even during processing. The • in the progress
            # spinner matches ASSISTANT_PREFIX_PATTERN, causing a false COMPLETED.
            # Detect the spinner and return PROCESSING before checking for COMPLETED.
            if re.search(TUI_PROGRESS_PATTERN, tail_output, re.MULTILINE):
                return TerminalStatus.PROCESSING

            # Consider COMPLETED only if we see an assistant marker (skipping
            # MCP tool-call markers) after the last user message. Without the
            # tool-call filter, "• Called <server>.<tool>(...)" emitted before
            # the model has actually replied would trip COMPLETED prematurely.
            if last_user is not None:
                if _find_assistant_marker(clean_output[last_user.start() :]) is not None:
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            # No user-message marker in the cleaned buffer. Two cases:
            # - Fresh init: no assistant content either → IDLE.
            # - Long-running response: the › user marker has been evicted from
            #   the 8KB rolling buffer by the time the response settles, but an
            #   assistant bullet is still visible. Without this branch we'd
            #   return IDLE forever and ``wait_for_status(completed)`` in the
            #   e2e tests would time out.
            # Search above the TUI footer cutoff so the › suggestion-hint and
            # status-bar lines aren't confused with a model reply.
            if _find_assistant_marker(clean_output[:cutoff_pos]) is not None:
                return TerminalStatus.COMPLETED
            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Codex's final response from terminal output.

        Supports two output formats:
        - Label style: "You ...\\nassistant: response\\n❯" (synthetic/test format)
        - Bullet style: "› user message\\n• response\\n›" (real Codex interactive mode)

        Primary approach: find the last user message and extract everything between
        the end of that line and the next empty idle prompt.
        Fallback: use assistant marker based extraction when no user message is found.
        """
        clean_output = re.sub(ANSI_CODE_PATTERN, "", script_output)

        # Primary: find last user message, extract response between it and idle prompt.
        # Exclude the Codex TUI footer from user-message matching when detected.
        all_lines = clean_output.splitlines()
        tui_footer_detected = any(
            re.search(TUI_FOOTER_PATTERN, line) for line in all_lines[-IDLE_PROMPT_TAIL_LINES:]
        )
        if tui_footer_detected:
            cutoff_pos = _compute_tui_footer_cutoff(all_lines)
        else:
            cutoff_pos = len(clean_output)

        user_matches = [
            m
            for m in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
            if m.start() < cutoff_pos
        ]

        if user_matches:
            last_user = user_matches[-1]

            # Find the first assistant response marker (• or assistant:) after
            # the user message, skipping "• Called <server>.<tool>(...)" MCP
            # tool call markers — those are followed by tool output, not the
            # model's reply. Anchoring on a tool call marker would pull tool
            # output (e.g. skill body text) into the extracted response.
            asst_after_user = _find_assistant_marker(clean_output[last_user.start() :])

            if asst_after_user:
                response_start = last_user.start() + asst_after_user.start()
            else:
                # No assistant marker found; fall back to skipping one line
                user_line_end = clean_output.find("\n", last_user.start())
                if user_line_end == -1:
                    user_line_end = len(clean_output)
                response_start = user_line_end + 1

            # Find extraction boundary: empty idle prompt or TUI footer area.
            # With --no-alt-screen, the TUI footer (› hint + status bar) has no
            # empty idle prompt. Use cutoff_pos as the boundary when TUI is present.
            idle_after = re.search(
                IDLE_PROMPT_STRICT_PATTERN,
                clean_output[response_start:],
                re.MULTILINE,
            )
            if idle_after:
                end_pos = response_start + idle_after.start()
            elif tui_footer_detected:
                end_pos = cutoff_pos
            else:
                end_pos = len(clean_output)

            response_text = clean_output[response_start:end_pos].strip()

            if response_text:
                # Strip "assistant:" prefix if present (label format)
                response_text = re.sub(
                    r"^(?:assistant|codex|agent)\s*:\s*",
                    "",
                    response_text,
                    count=1,
                    flags=re.IGNORECASE,
                )
                return response_text.strip()

        # Fallback: assistant marker based extraction (no user message found).
        # Filter out "• Called <tool>(...)" MCP tool call markers so we anchor
        # on the model's actual reply, not tool output.
        all_matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )
        matches = []
        for m in all_matches:
            line_end = clean_output.find("\n", m.start())
            if line_end == -1:
                line_end = len(clean_output)
            line = clean_output[m.start() : line_end]
            if re.match(MCP_TOOL_CALL_PATTERN, line):
                continue
            matches.append(m)

        if not matches:
            raise ValueError("No Codex response found - no assistant marker detected")

        last_match = matches[-1]
        start_pos = last_match.end()

        idle_after = re.search(
            IDLE_PROMPT_STRICT_PATTERN,
            clean_output[start_pos:],
            re.MULTILINE,
        )
        end_pos = start_pos + idle_after.start() if idle_after else len(clean_output)

        final_answer = clean_output[start_pos:end_pos].strip()

        if not final_answer:
            raise ValueError("Empty Codex response - no content found")

        return final_answer

    def exit_cli(self) -> str:
        """Get the command to exit Codex CLI."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Codex CLI provider."""
        self._initialized = False
