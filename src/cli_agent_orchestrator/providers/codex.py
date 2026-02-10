"""Codex CLI provider implementation."""

import logging
import re
import shlex
import time
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

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
IDLE_PROMPT_PATTERN_LOG = r"\? for shortcuts"
# Match assistant response start: "assistant:/codex:/agent:" (label style from synthetic
# test fixtures) or "•" bullet point (real Codex interactive output format).
ASSISTANT_PREFIX_PATTERN = r"^(?:(?:assistant|codex|agent)\s*:|\s*•)"
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
TUI_FOOTER_PATTERN = r"(?:\?\s+for shortcuts|context left)"
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
    ):
        super().__init__(terminal_id, session_name, window_name)
        self._initialized = False
        self._agent_profile = agent_profile

    def _build_codex_command(self) -> str:
        """Build Codex command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses codex's -c developer_instructions flag to inject agent system prompts.
        """
        command_parts = ["codex", "--no-alt-screen", "--disable", "shell_snapshot"]

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
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

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        return shlex.join(command_parts)

    def _handle_trust_prompt(self, timeout: float = 20.0) -> None:
        """Auto-accept the workspace trust prompt if it appears.

        Codex shows a folder approval dialog when opening a new directory.
        This sends Enter to accept the default option (allow Codex to work).
        CAO assumes the user trusts the working directory since they confirmed
        workspace access during the launch command.
        """
        start_time = time.time()
        while time.time() - start_time < timeout:
            output = tmux_client.get_history(self.session_name, self.window_name)
            if not output:
                time.sleep(1.0)
                continue

            # Clean ANSI codes for reliable text matching
            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                logger.info("Codex workspace trust prompt detected, auto-accepting")
                session = tmux_client.server.sessions.get(session_name=self.session_name)
                window = session.windows.get(window_name=self.window_name)
                pane = window.active_pane
                if pane:
                    pane.send_keys("", enter=True)
                return

            # Check if Codex has fully started (welcome banner visible)
            if re.search(CODEX_WELCOME_PATTERN, clean_output):
                logger.info("Codex started without trust prompt")
                return

            time.sleep(1.0)
        logger.warning("Codex trust prompt handler timed out")

    def initialize(self) -> bool:
        """Initialize Codex provider by starting codex command."""
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Send a warm-up command before launching codex.
        # Codex exits immediately in freshly-created tmux sessions where the shell
        # has not yet processed a full interactive command cycle.
        tmux_client.send_keys(self.session_name, self.window_name, "echo ready")
        time.sleep(2.0)

        # Build command with flags and agent profile (developer_instructions).
        # --no-alt-screen: run in inline mode so output stays in normal scrollback,
        #   making tmux capture-pane reliable.
        # --disable shell_snapshot: avoid TTY input conflicts (SIGTTIN) in tmux
        #   caused by the shell_snapshot subprocess inheriting stdin.
        command = self._build_codex_command()
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Handle workspace trust prompt if it appears (new/untrusted directories)
        self._handle_trust_prompt(timeout=20.0)

        if not wait_until_status(self, TerminalStatus.IDLE, timeout=60.0, polling_interval=1.0):
            raise TimeoutError("Codex initialization timed out after 60 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Codex status by analyzing terminal output."""
        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)
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
        if tui_footer_detected and len(all_lines) > IDLE_PROMPT_TAIL_LINES:
            cutoff_pos = len("\n".join(all_lines[:-IDLE_PROMPT_TAIL_LINES]))
        else:
            cutoff_pos = len(clean_output)

        last_user = None
        for match in re.finditer(USER_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE):
            if match.start() < cutoff_pos:
                last_user = match

        output_after_last_user = clean_output[last_user.start() :] if last_user else clean_output
        assistant_after_last_user = bool(
            last_user
            and re.search(
                ASSISTANT_PREFIX_PATTERN,
                output_after_last_user,
                re.IGNORECASE | re.MULTILINE,
            )
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

            # Consider COMPLETED only if we see an assistant marker after the last user message.
            if last_user is not None:
                if re.search(
                    ASSISTANT_PREFIX_PATTERN,
                    clean_output[last_user.start() :],
                    re.IGNORECASE | re.MULTILINE,
                ):
                    return TerminalStatus.COMPLETED

                return TerminalStatus.IDLE

            return TerminalStatus.IDLE

        # If we're not at an idle prompt and we don't see explicit errors/permission prompts,
        # assume the CLI is still producing output.
        return TerminalStatus.PROCESSING

    def get_idle_pattern_for_log(self) -> str:
        """Return Codex IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

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
        if tui_footer_detected and len(all_lines) > IDLE_PROMPT_TAIL_LINES:
            cutoff_pos = len("\n".join(all_lines[:-IDLE_PROMPT_TAIL_LINES]))
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
            # the user message. This correctly skips multi-line user messages
            # that wrap across several lines in the Codex TUI.
            asst_after_user = re.search(
                ASSISTANT_PREFIX_PATTERN,
                clean_output[last_user.start() :],
                re.IGNORECASE | re.MULTILINE,
            )
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
        matches = list(
            re.finditer(ASSISTANT_PREFIX_PATTERN, clean_output, re.IGNORECASE | re.MULTILINE)
        )

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
