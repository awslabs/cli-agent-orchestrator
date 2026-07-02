# Provider Template

Annotated template for creating a new CAO provider. Replace `NewCli` / `new_cli` with your provider name.

```python
"""New CLI provider implementation."""

import logging
import re
import time
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


# ---------------------------------------------------------------------------
# Regex patterns — define at module level for reuse and testability
# ---------------------------------------------------------------------------

# Strip ANSI escape sequences (colors, cursor movement, formatting)
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"

# What the idle prompt looks like when the CLI is waiting for user input.
# Examples from existing providers:
#   Claude Code: r"[>❯][\s\xa0]"
#   Kiro CLI:    r"\[{agent_profile}\]\s*(?:\d+%\s*)?>"
#   Codex:       r"❯\s"
IDLE_PROMPT_PATTERN = r"YOUR_IDLE_PATTERN_HERE"

# Same pattern but for log file monitoring (quick pre-check).
IDLE_PROMPT_PATTERN_LOG = r"YOUR_IDLE_PATTERN_HERE"

# What the CLI shows while processing (spinners, thinking indicators).
# Match on distinctive characters that only appear during processing.
# WARNING: Only match against recent lines, not the full buffer.
# Examples:
#   Claude Code: r"[✶✢✽✻✳].*…"
#   Kiro CLI:    r"Generating\.\.\."
PROCESSING_PATTERN = r"YOUR_PROCESSING_PATTERN_HERE"

# How agent responses start — used to find response boundaries.
# Examples:
#   Claude Code: r"⏺(?:\x1b\[[0-9;]*m)*\s+"
#   Kiro CLI:    r"❯"  (green arrow)
RESPONSE_PATTERN = r"YOUR_RESPONSE_MARKER_HERE"

# Permission or confirmation prompts that need user interaction.
# Return WAITING_USER_ANSWER when detected.
WAITING_USER_ANSWER_PATTERN = r"YOUR_PERMISSION_PATTERN_HERE"


class NewCliProvider(BaseProvider):
    """Provider for New CLI tool integration."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[list] = None,
    ):
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._initialized = False
        self._agent_profile = agent_profile

    # ----- Optional property overrides -----

    @property
    def paste_enter_count(self) -> int:
        """Override if single Enter submits after paste."""
        return 2  # Default: double Enter for multi-line TUIs

    @property
    def extraction_retries(self) -> int:
        """Override if TUI has transient rendering issues."""
        return 0  # Default: no retries

    # ----- Build launch command -----

    def _build_command(self) -> str:
        """Build the CLI launch command with agent profile and MCP config.
        
        Loads the agent profile to get system prompt and MCP server config,
        then constructs the full command string.
        """
        command_parts = ["new-cli"]  # Base command

        if self._agent_profile is not None:
            try:
                profile = load_agent_profile(self._agent_profile)

                # Add system prompt if present
                if profile.system_prompt:
                    command_parts.extend(["--system-prompt", profile.system_prompt])

                # Add MCP config if present
                # Inject CAO_TERMINAL_ID so MCP servers can identify this terminal
                if profile.mcpServers:
                    # Build MCP config dict, injecting CAO_TERMINAL_ID
                    pass  # Implementation depends on CLI's MCP config format

            except Exception as e:
                raise ProviderError(
                    f"Failed to load agent profile '{self._agent_profile}': {e}"
                )

        # Apply tool restrictions if the CLI supports native enforcement
        # See SKILL.md Step 5 for the three enforcement approaches
        if self._allowed_tools and "*" not in self._allowed_tools:
            pass  # Add --disallowedTools flags, or configure agent JSON, etc.

        return " ".join(command_parts)

    # ----- Initialize -----

    def initialize(self) -> bool:
        """Start the CLI tool in the tmux window."""
        # Wait for shell prompt
        if not wait_for_shell(tmux_client, self.session_name, self.window_name, timeout=10.0):
            raise TimeoutError("Shell initialization timed out")

        # Build and send the launch command
        command = self._build_command()
        tmux_client.send_keys(self.session_name, self.window_name, command)

        # Handle any startup prompts (trust dialog, permission bypass, etc.)
        # self._handle_startup_prompts(timeout=20.0)

        # Wait for CLI to be ready
        if not wait_until_status(
            self,
            {TerminalStatus.IDLE, TerminalStatus.COMPLETED},
            timeout=30.0,
            polling_interval=1.0,
        ):
            raise TimeoutError("CLI initialization timed out")

        self._initialized = True
        return True

    # ----- Status detection -----

    def get_status(self, tail_lines: Optional[int] = None) -> TerminalStatus:
        """Detect terminal state by analyzing tmux output.
        
        IMPORTANT: Check COMPLETED before PROCESSING to avoid the stale
        buffer problem. See references/lessons-learnt.md #1.
        """
        output = tmux_client.get_history(
            self.session_name, self.window_name, tail_lines=tail_lines
        )

        if not output:
            return TerminalStatus.ERROR

        # Always strip ANSI codes before pattern matching
        clean_output = re.sub(ANSI_CODE_PATTERN, "", output)

        # 1. Check for permission/confirmation prompts first
        if re.search(WAITING_USER_ANSWER_PATTERN, clean_output):
            return TerminalStatus.WAITING_USER_ANSWER

        # 2. Check for COMPLETED (response marker + idle prompt)
        #    Check this BEFORE processing to avoid stale buffer false positives
        if re.search(RESPONSE_PATTERN, clean_output) and re.search(
            IDLE_PROMPT_PATTERN, clean_output
        ):
            return TerminalStatus.COMPLETED

        # 3. Check for IDLE (just prompt, no response)
        if re.search(IDLE_PROMPT_PATTERN, clean_output):
            return TerminalStatus.IDLE

        # 4. Check for PROCESSING
        if re.search(PROCESSING_PATTERN, clean_output):
            return TerminalStatus.PROCESSING

        return TerminalStatus.ERROR

    # ----- Idle pattern for log monitoring -----

    def get_idle_pattern_for_log(self) -> str:
        """Quick pattern for file watcher pre-check."""
        return IDLE_PROMPT_PATTERN_LOG

    # ----- Message extraction -----

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Extract the agent's last response from terminal output.
        
        Strategy:
        1. Find all response markers in the output
        2. Take the last one (final answer)
        3. Extract text from after the marker until the next prompt
        4. Strip ANSI codes and clean up
        """
        # Find all response markers
        matches = list(re.finditer(RESPONSE_PATTERN, script_output))
        if not matches:
            raise ValueError("No response found in terminal output")

        # Get text after the last response marker
        last_match = matches[-1]
        remaining = script_output[last_match.end():]

        # Extract until next prompt or end of output
        lines = remaining.split("\n")
        response_lines = []
        for line in lines:
            if re.match(IDLE_PROMPT_PATTERN, line):
                break
            response_lines.append(line.strip())

        if not any(line.strip() for line in response_lines):
            raise ValueError("Empty response after marker")

        # Clean up
        result = "\n".join(response_lines).strip()
        result = re.sub(ANSI_CODE_PATTERN, "", result)
        return result.strip()

    # ----- Exit and cleanup -----

    def exit_cli(self) -> str:
        """Command to exit the CLI."""
        return "/exit"  # or "/quit", "exit", etc.

    def cleanup(self) -> None:
        """Clean up provider resources."""
        self._initialized = False
```

## Key Design Decisions

### Why check COMPLETED before PROCESSING?

The tmux buffer retains old output. A spinner line like `✽ Cooking…` from 5 minutes ago is still in the buffer. If you check PROCESSING first, you'll match the old spinner and never see COMPLETED — even though the agent finished and the idle prompt is at the bottom.

By checking COMPLETED first (response marker + idle prompt), you correctly detect that the agent is done regardless of historical spinners.

### Why define patterns at module level?

1. Testability — unit tests can import and test patterns directly
2. Performance — patterns compiled once, not per-call
3. Visibility — easy to see all patterns in one place at the top of the file

### Why inject CAO_TERMINAL_ID into MCP env?

MCP servers (like cao-mcp-server) need to know which terminal they're running in for handoff/assign operations. Some CLIs don't forward parent env vars to MCP subprocesses, so you must inject it explicitly.
