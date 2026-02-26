"""CodeBuddy CLI provider implementation."""

import shlex
from typing import Optional

from cli_agent_orchestrator.providers.simple_tui import SimpleTuiProvider
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


def _build_codebuddy_command(agent_profile: Optional[str]) -> str:
    command_parts = ["codebuddy", "--dangerously-skip-permissions"]
    if agent_profile:
        profile = load_agent_profile(agent_profile)
        if profile.system_prompt:
            command_parts.extend(["--append-system-prompt", profile.system_prompt])
    return shlex.join(command_parts)


class CodeBuddyProvider(SimpleTuiProvider):
    """Provider for CodeBuddy CLI (`codebuddy`)."""

    def __init__(
        self,
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str] = None,
    ):
        super().__init__(
            terminal_id=terminal_id,
            session_name=session_name,
            window_name=window_name,
            start_command=_build_codebuddy_command(agent_profile),
            idle_prompt_pattern=r"[>❯›]\s",
            idle_prompt_pattern_log=r"[>❯›]\s",
        )
