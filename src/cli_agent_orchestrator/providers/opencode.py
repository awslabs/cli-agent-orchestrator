"""OpenCode provider implementation."""

from cli_agent_orchestrator.providers.simple_tui import SimpleTuiProvider


class OpenCodeProvider(SimpleTuiProvider):
    """Provider for OpenCode CLI (`opencode`)."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str):
        super().__init__(
            terminal_id=terminal_id,
            session_name=session_name,
            window_name=window_name,
            start_command="opencode tui",
            idle_prompt_pattern=r"[>❯›]\s",
            idle_prompt_pattern_log=r"[>❯›]\s",
        )
