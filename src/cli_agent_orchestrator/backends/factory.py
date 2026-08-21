"""BackendFactory — constructs the configured TerminalBackend at startup.

Reads `terminal.backend` via ConfigService (CLI flag > CAO_TERMINAL_BACKEND
env var > settings.json > "tmux" default). "herdr" is opt-in and experimental.
"""

import logging
from typing import Optional

from cli_agent_orchestrator.backends.base import TerminalBackend
from cli_agent_orchestrator.services.config_service import ConfigService

logger = logging.getLogger(__name__)


class ConfigurationError(Exception):
    """Raised when the backend configuration is invalid."""

    pass


class BackendFactory:
    """Factory that reads config and returns the appropriate backend instance."""

    @staticmethod
    def create(backend_override: Optional[str] = None) -> TerminalBackend:
        """Create a TerminalBackend based on configuration.

        Args:
            backend_override: Explicit backend name that takes precedence over
                both the ``CAO_TERMINAL_BACKEND`` env var and the config file
                (e.g. from ``cao-server --terminal herdr``). Other keys (such
                as ``herdr_session``) are still resolved normally.

        Returns:
            A configured TerminalBackend instance

        Raises:
            ConfigurationError: If terminal_backend value is unrecognized
        """
        backend_name = ConfigService.get(
            "terminal.backend", default="tmux", override=backend_override
        )

        if backend_name == "tmux":
            from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

            return TmuxBackend()
        elif backend_name == "herdr":
            from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

            herdr_session = ConfigService.get("terminal.herdr_session", default="cao")
            logger.info(
                "[EXPERIMENTAL] terminal_backend='herdr' is experimental. "
                "Report issues at https://github.com/awslabs/cli-agent-orchestrator/issues"
            )
            return HerdrBackend(herdr_session=herdr_session)
        else:
            raise ConfigurationError(
                f"Unknown terminal_backend: '{backend_name}'. "
                f"Valid options are: 'tmux', 'herdr' [EXPERIMENTAL]"
            )
