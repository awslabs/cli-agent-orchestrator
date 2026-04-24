import re
import time

import pytest

from cli_agent_orchestrator.multiplexers.base import LaunchSpec
from cli_agent_orchestrator.providers.claude_code import (
    IDLE_PROMPT_PATTERN,
    TRUST_PROMPT_PATTERN,
)

pytestmark = pytest.mark.smoke


def test_trust_prompt_acceptance(multiplexer, claude_bin, tmp_path):
    spec = LaunchSpec(argv=(claude_bin,), provider="claude")
    multiplexer.create_session(
        session_name="cao-smoke-claude",
        window_name="claude-0",
        terminal_id="smoke-claude",
        working_directory=str(tmp_path),
        launch_spec=spec,
    )
    try:
        for _ in range(30):
            text = multiplexer.get_history("cao-smoke-claude", "claude-0")
            if re.search(TRUST_PROMPT_PATTERN, text):
                break
            time.sleep(1)
        else:
            pytest.fail("Claude trust prompt not seen in 30s")

        multiplexer.send_special_key("cao-smoke-claude", "claude-0", "Enter")

        for _ in range(30):
            text = multiplexer.get_history("cao-smoke-claude", "claude-0")
            if re.search(IDLE_PROMPT_PATTERN, text):
                return
            time.sleep(1)
        pytest.fail("Claude idle prompt not seen after trust accept")
    finally:
        multiplexer.kill_session("cao-smoke-claude")
