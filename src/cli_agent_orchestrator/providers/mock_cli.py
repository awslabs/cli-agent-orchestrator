"""Mock CLI provider — deterministic stand-in for credential-free CI tests.

This provider exists to exercise CAO's orchestration logic (handoff,
assign, send_message, inbox watchdog, multi-provider sessions) in
CI without requiring any real coding-CLI binary, network call, or
credentials.

It wraps a tiny ``mock_cli`` shell binary shipped at
``test/providers/fixtures/bin/mock_cli``. The binary is a deterministic
REPL: it prints a prompt, reads stdin, sleeps a configurable delay, and
echoes the input prefixed with ``> MOCK:``.

Production code paths never see this provider — the binary is not on
PATH outside pytest. The conftest-level PATH-prepend in
``test/conftest.py`` makes it discoverable for the duration of the test
session.

See ``docs/mock-cli-provider.md`` for the design and motivation.
"""

import logging
import re
import shlex
from typing import List, Optional

from cli_agent_orchestrator.backends.registry import get_backend
from cli_agent_orchestrator.models.terminal import TerminalStatus
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.utils.terminal import wait_for_shell, wait_until_status

logger = logging.getLogger(__name__)

# Idle prompt emitted by the mock_cli binary at the end of every turn.
IDLE_PROMPT_PATTERN = r"❯\s*$"
IDLE_PROMPT_PATTERN_LOG = r"❯\s"
# Response indicator emitted by the binary before each reply line.
RESPONSE_INDICATOR_PATTERN = r"^>\s*MOCK:"
ANSI_CODE_PATTERN = r"\x1b\[[0-9;]*m"
ERROR_INDICATOR = "ERROR: mock failure injected"


class MockCliProvider(BaseProvider):
    """Deterministic mock provider for orchestration-layer CI tests.

    Not for production use. The companion binary lives at
    ``test/providers/fixtures/bin/mock_cli`` and must be on PATH (the
    repo's ``test/conftest.py`` prepends it for the pytest session).
    """

    BINARY_NAME = "mock_cli"

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        allowed_tools: Optional[List[str]] = None,
        delay_ms: int = 50,
    ) -> None:
        super().__init__(terminal_id, session_name, window_name, allowed_tools)
        self._delay_ms = delay_ms

    async def initialize(self) -> bool:
        """Launch the ``mock_cli`` binary inside the tmux window."""
        if not await wait_for_shell(self.terminal_id, timeout=10.0):
            raise TimeoutError("Shell initialization timed out after 10 seconds")

        command = shlex.join([self.BINARY_NAME, "--delay-ms", str(self._delay_ms)])
        get_backend().send_keys(self.session_name, self.window_name, command)

        if not await wait_until_status(
            self.terminal_id, {TerminalStatus.IDLE, TerminalStatus.COMPLETED}, timeout=15.0
        ):
            raise TimeoutError("mock_cli initialization timed out after 15 seconds")

        self._initialized = True
        return True

    def get_status(self, buffer: str) -> TerminalStatus:
        """Pattern-match the binary's output buffer to determine current state."""
        if not buffer:
            return TerminalStatus.UNKNOWN

        clean = re.sub(ANSI_CODE_PATTERN, "", buffer)

        if ERROR_INDICATOR in clean:
            return TerminalStatus.ERROR

        has_idle = re.search(IDLE_PROMPT_PATTERN, clean, re.MULTILINE)
        if not has_idle:
            return TerminalStatus.PROCESSING

        responses = list(re.finditer(RESPONSE_INDICATOR_PATTERN, clean, re.MULTILINE))
        if responses:
            return TerminalStatus.COMPLETED
        return TerminalStatus.IDLE

    def extract_last_message_from_script(self, script_output: str) -> str:
        """Return the payload of the last ``> MOCK: ...`` line."""
        clean = re.sub(ANSI_CODE_PATTERN, "", script_output)
        matches = list(re.finditer(r"^>\s*MOCK:\s*(.*)$", clean, re.MULTILINE))
        if not matches:
            raise ValueError("No mock_cli response found in script output")
        return matches[-1].group(1).strip()

    def get_idle_pattern_for_log(self) -> str:
        return IDLE_PROMPT_PATTERN_LOG

    def exit_cli(self) -> str:
        return "/exit"

    def cleanup(self) -> None:
        return None
