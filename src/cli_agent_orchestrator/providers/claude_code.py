"""Claude Code provider implementation."""

import json
import logging
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.clients.tmux import tmux_client
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)


# Custom exception for provider errors
class ProviderError(Exception):
    """Exception raised for provider-specific errors."""

    pass


# Regex patterns for Claude Code output analysis
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
RESPONSE_PATTERN = r"⏺(?:\x1b\[[0-9;]*m)*\s+"  # Handle any ANSI codes between marker and text
# Match Claude Code processing spinners:
# - Old format: "✽ Cooking… (esc to interrupt)" / "✶ Thinking… (esc to interrupt)"
# - New format: "✽ Cooking… (6s · ↓ 174 tokens · thinking)"
# - Minimal format: "✻ Orbiting…" (no parenthesized status)
# Common: spinner char + text + ellipsis, optionally followed by parenthesized status
PROCESSING_PATTERN = r"[✶✢✽✻✳·].*\u2026"
# Structural PROCESSING indicator: a spinner line (spinner char + … ) immediately
# before the ──────── separator line that Claude Code draws before the input prompt.
# Requires a known spinner character on the same line as … to avoid false-positives
# from response text or tool outputs that happen to contain … .
# Allows 0–2 blank lines between the spinner and the separator.
# The separator line starts with an ANSI colour code (\x1b[38;5;244m) before the
# box-drawing characters (U+2500), so the pattern skips that prefix explicitly.
#
# Processing: "✻ Skedaddling…\n\n────────\n❯ " → spinner+… before separator → PROCESSING
# Idle/done: "⏺ response text\n────────\n❯ " → no spinner before separator → not PROCESSING
# Stale spinner: "✢ Thinking…" far back in scrollback, current separator has
# no spinner immediately before it → not PROCESSING
THINKING_BEFORE_SEPARATOR_PATTERN = re.compile(
    r"[^\n]*[✶✢✽✻✳·][^\n]*\u2026[^\n]*\n(?:[^\n]*\n){0,2}(?:\x1b\[[0-9;]*m)*\u2500{20,}",
    re.MULTILINE,
)
IDLE_PROMPT_PATTERN = r"[>❯][\s\xa0]"  # Handle both old ">" and new "❯" prompt styles
WAITING_USER_ANSWER_PATTERN = (
    r"↑/↓ to navigate"  # Ink TUI footer shown only while a selection widget is active
)
TRUST_PROMPT_PATTERN = r"Yes, I trust this folder"  # Workspace trust dialog
BYPASS_PROMPT_PATTERN = r"Yes, I accept"  # Bypass permissions confirmation dialog
IDLE_PROMPT_PATTERN_LOG = r"[>❯][\s\xa0]"  # Same pattern for log files


class ClaudeCodeProvider(BaseProvider):
    """Provider for Claude Code CLI tool integration."""

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

    def _build_claude_command(self) -> str:
        """Build Claude Code command with agent profile if provided.

        Returns properly escaped shell command string that can be safely sent via tmux.
        Uses shlex.join() to handle multiline strings and special characters correctly.
        """
        # --dangerously-skip-permissions: bypass the workspace trust dialog and
        # tool permission prompts. CAO already confirms workspace access during
        # `cao launch` (or `--yolo`), so re-prompting each spawned agent
        # (supervisor and worker) is redundant and blocks handoff/assign flows.
        command_parts = ["claude", "--dangerously-skip-permissions"]

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Add system prompt - escape newlines to prevent tmux chunking issues
                system_prompt = profile.system_prompt if profile.system_prompt is not None else ""
                system_prompt = self._apply_skill_prompt(system_prompt)
                if system_prompt:
                    # Replace actual newlines with \n escape sequences
                    # This prevents tmux send_keys chunking from breaking the command
                    escaped_prompt = system_prompt.replace("\\", "\\\\").replace("\n", "\\n")
                    command_parts.extend(["--append-system-prompt", escaped_prompt])

                # Add MCP config if present.
                # Forward CAO_TERMINAL_ID so MCP servers (e.g. cao-mcp-server)
                # can identify the current terminal for handoff/assign operations.
                # Claude Code does not automatically forward parent shell env vars
                # to MCP subprocesses, so we inject it explicitly via the env field.
                if profile.mcpServers:
                    mcp_config = {}
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
                    command_parts.extend(["--mcp-config", mcp_json])

            except Exception as e:
                raise ProviderError(f"Failed to load agent profile '{self._agent_profile}': {e}")

        # Apply tool restrictions via --disallowedTools flags.
        # --dangerously-skip-permissions bypasses prompts but --disallowedTools
        # still prevents the agent from using the blocked tools entirely.
        if self._allowed_tools and "*" not in self._allowed_tools:
            from cli_agent_orchestrator.utils.tool_mapping import get_disallowed_tools

            disallowed = get_disallowed_tools("claude_code", self._allowed_tools)
            for tool in disallowed:
                command_parts.extend(["--disallowedTools", tool])

        # Use shlex.join() for proper shell escaping of all arguments
        # This correctly handles multiline strings, quotes, and special characters
        claude_cmd = shlex.join(command_parts)

        # When cao-server runs inside a Claude Code session, CLAUDE* env vars
        # leak into spawned tmux panes (via the tmux server's global env).
        # Claude Code detects these and refuses to start ("nested session").
        # Unset all matching vars except CLAUDE_CODE_USE_* and
        # CLAUDE_CODE_SKIP_*_AUTH (needed for provider authentication:
        # Bedrock, Vertex AI, Foundry).
        unset_cmd = (
            "unset $(env | sed -n 's/^\\(CLAUDE[A-Z_]*\\)=.*/\\1/p'"
            " | grep -v -E 'CLAUDE_CODE_USE_(BEDROCK|VERTEX|FOUNDRY)"
            "|CLAUDE_CODE_SKIP_(BEDROCK|VERTEX|FOUNDRY)_AUTH'"
            ") 2>/dev/null"
        )
        return f"{unset_cmd}; {claude_cmd}"

    @staticmethod
    def _ensure_skip_bypass_prompt_setting() -> None:
        """Ensure ``skipDangerousModePermissionPrompt`` is set in settings.

        Claude Code (v2.1.41+) shows a bypass permissions confirmation dialog
        on every launch with ``--dangerously-skip-permissions`` unless
        ``skipDangerousModePermissionPrompt: true`` is persisted in
        ``~/.claude/settings.json``.  CAO already uses the flag intentionally,
        so the confirmation is redundant and blocks initialization.
        """
        settings_path = Path.home() / ".claude" / "settings.json"
        settings: dict = {}
        if settings_path.exists():
            try:
                with open(settings_path) as f:
                    settings = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        if settings.get("skipDangerousModePermissionPrompt") is True:
            return

        settings["skipDangerousModePermissionPrompt"] = True
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_path, "w") as f:
            json.dump(settings, f, indent=2)
        logger.info("Set skipDangerousModePermissionPrompt in ~/.claude/settings.json")

    def _handle_startup_prompts(self, timeout: float = 20.0) -> None:
        """Auto-accept startup prompts that may appear before the REPL is ready.

        Claude Code may show up to two prompts during startup:

        1. **Bypass permissions confirmation** (``--dangerously-skip-permissions``)
           – shows "Yes, I accept" as option 2; requires ``Down`` + ``Enter``.
           The settings-based fix (``_ensure_skip_bypass_prompt_setting``) prevents
           this in most cases; this handler is a defensive fallback.
        2. **Workspace trust dialog** – shows "Yes, I trust this folder";
           requires ``Enter``.
        """
        start_time = time.time()
        bypass_accepted = False
        while time.time() - start_time < timeout:
            output = tmux_client.get_history(self.session_name, self.window_name)
            if not output:
                time.sleep(1.0)
                continue

            clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

            # 1) Handle bypass permissions prompt (appears before trust prompt).
            #    Only act once — the text stays in the buffer after dismissal.
            if not bypass_accepted and re.search(BYPASS_PROMPT_PATTERN, clean_output):
                logger.info("Bypass permissions prompt detected, auto-accepting")
                target = f"{self.session_name}:{self.window_name}"
                # Send raw Down arrow escape sequence (-l for literal) to move
                # cursor to "Yes, I accept", then Enter to confirm.
                # tmux send-keys "Down" doesn't work with Claude's Ink TUI.
                subprocess.run(["tmux", "send-keys", "-t", target, "-l", "\x1b[B"], check=False)
                time.sleep(0.5)
                subprocess.run(["tmux", "send-keys", "-t", target, "Enter"], check=False)
                bypass_accepted = True
                time.sleep(1.0)
                continue  # Trust prompt may follow

            # 2) Handle workspace trust prompt
            if re.search(TRUST_PROMPT_PATTERN, clean_output):
                logger.info("Workspace trust prompt detected, auto-accepting")
                session = tmux_client.server.sessions.get(session_name=self.session_name)
                window = session.windows.get(window_name=self.window_name)
                pane = window.active_pane
                if pane:
                    pane.send_keys("", enter=True)
                return

            # 3) Claude Code fully started — no prompts needed
            if re.search(r"Welcome to|Claude Code v\d+", clean_output):
                logger.info("Claude Code started without prompts")
                return
            if re.search(IDLE_PROMPT_PATTERN, clean_output):
                logger.info("Claude Code idle prompt detected, no prompts needed")
                return

            time.sleep(1.0)
        logger.warning("Startup prompt handler timed out")

    def initialize(self) -> bool:
        """Initialize Claude Code provider by starting claude command."""
        # Wait for shell prompt to appear in the tmux window
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        # Prevent bypass permissions dialog from appearing (settings-based fix).
        self._ensure_skip_bypass_prompt_setting()

        # Build properly escaped command string
        command = self._build_claude_command()

        # Send Claude Code command using tmux client
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Handle startup prompts (bypass permissions + workspace trust)
        self._handle_startup_prompts(timeout=20.0)

        # Wait for Claude Code prompt to be ready.
        # Accept both IDLE and COMPLETED — some CLI versions show a startup
        # message that get_status() interprets as a completed response.
        if not wait_until_status(
            self,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=30.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("Claude Code initialization timed out after 30 seconds")

        self._initialized = True
        return True

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Get Claude Code status by analyzing terminal output.

        Uses a structural "thinking-before-separator" check as the primary
        PROCESSING indicator, plus position-based fallbacks for edge cases.

        Bug history:
        1. Stale-spinner bug (#104): Old spinner lines persist in the tmux
           scrollback after the agent returns to idle (Claude Code renders
           inline, not alt-screen, inside a tmux pane). Position comparison
           of last spinner vs last idle prompt catches this.

        2. Mid-tool-execution race (this issue): The ❯ input prompt is ALWAYS
           rendered at the bottom of the tmux pane (last position in the
           scrollback buffer). Position-based comparisons against ❯ are
           therefore unreliable — last_idle.start() is always greater than
           any other marker when anything has been typed/executed.

        V3 fix (structural): Check whether any line containing … (U+2026,
        the ellipsis used in all Claude Code thinking/spinner text) appears
        immediately before the ──────── separator line. Claude Code draws
        this separator between the active-execution area and the input prompt.
        When the agent is thinking/processing:
        "· Swirling… (thinking)\n\n──────────────────────\n❯ "
        When idle or completed:
        "some response text\n──────────────────────\n❯ "
        A stale old spinner line far back in scrollback will NOT be
        immediately before the separator, so the structural check is immune
        to the stale-spinner false-positive.

        See: https://github.com/awslabs/cli-agent-orchestrator/issues/104
        """

        output = tmux_client.get_history(self.session_name, self.window_name, tail_lines=tail_lines)

        if not output:
            return TerminalStatus.ERROR

        # PRIMARY PROCESSING check: structural — thinking line immediately
        # before the ──────── separator. Catches ALL spinner variants (including
        # newer "· Swirling…" format) and is immune to the ❯ position problem.
        if THINKING_BEFORE_SEPARATOR_PATTERN.search(output):
            return TerminalStatus.PROCESSING

        # Find the LAST occurrence of each marker for fallback position checks.
        last_processing = None
        for m in re.finditer(PROCESSING_PATTERN, output):
            last_processing = m

        last_idle = None
        for m in re.finditer(IDLE_PROMPT_PATTERN, output):
            last_idle = m

        last_response = None
        for m in re.finditer(RESPONSE_PATTERN, output):
            last_response = m

        # FALLBACK PROCESSING: spinner visible AND no separator follows it yet
        # (early in execution before the separator appears). Position comparison
        # is used here only when no separator is present (safe case).
        if last_processing and not re.search(r"\u2500{20,}", output):
            if last_idle is None or last_processing.start() > last_idle.start():
                return TerminalStatus.PROCESSING

        # Check for waiting user answer via the active Ink selection footer.
        # Exclude startup prompts (trust + bypass), which also render the footer.
        if (
            re.search(WAITING_USER_ANSWER_PATTERN, output)
            and not re.search(TRUST_PROMPT_PATTERN, output)
            and not re.search(BYPASS_PROMPT_PATTERN, output)
        ):
            return TerminalStatus.WAITING_USER_ANSWER

        # COMPLETED: ⏺ response exists AND ❯ prompt is visible (agent finished).
        if last_response and last_idle:
            return TerminalStatus.COMPLETED

        # IDLE: shell prompt visible but no response yet (e.g. just initialized).
        if last_idle:
            return TerminalStatus.IDLE

        return TerminalStatus.ERROR

    def get_idle_pattern_for_log(self) -> str:
        """Return Claude Code IDLE prompt pattern for log files."""
        return IDLE_PROMPT_PATTERN_LOG

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract Claude's final response message using ⏺ indicator."""
        # Find all matches of response pattern
        matches = list(re.finditer(RESPONSE_PATTERN, script_output))

        if not matches:
            raise ValueError("No Claude Code response found - no ⏺ pattern detected")

        # Get the last match (final answer)
        last_match = matches[-1]
        start_pos = last_match.end()

        # Extract everything after the last ⏺ until next prompt or separator
        remaining_text = script_output[start_pos:]

        # Split by lines and extract response
        lines = remaining_text.split("\n")
        response_lines = []

        for line in lines:
            # Stop at next > prompt or separator line
            if re.match(r">\s", line) or "────────" in line:
                break

            # Clean the line
            clean_line = line.strip()
            response_lines.append(clean_line)

        if not response_lines or not any(line.strip() for line in response_lines):
            raise ValueError("Empty Claude Code response - no content found after ⏺")

        # Join lines and clean up
        final_answer = "\n".join(response_lines).strip()
        # Remove ANSI codes from the final message
        final_answer = re.sub(ANSI_CODE_PATTERN, "", final_answer)
        return final_answer.strip()

    def exit_cli(self) -> str:
        """Get the command to exit Claude Code."""
        return "/exit"

    def cleanup(self) -> None:
        """Clean up Claude Code provider."""
        self._initialized = False
