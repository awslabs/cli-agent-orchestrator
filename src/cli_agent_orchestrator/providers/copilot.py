"""GitHub Copilot CLI provider implementation."""

from cli_agent_orchestrator.providers.simple_tui import SimpleTuiProvider


class CopilotProvider(SimpleTuiProvider):
    """Provider for GitHub Copilot CLI (`copilot`)."""

    def __init__(self, terminal_id: str, session_name: str, window_name: str):
        super().__init__(
            terminal_id=terminal_id,
            session_name=session_name,
            window_name=window_name,
            start_command="copilot",
            idle_prompt_pattern=r"[>❯›]\s",
            idle_prompt_pattern_log=r"[>❯›]\s",
        )
