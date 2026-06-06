"""BackendFactory — constructs the configured TerminalBackend at startup.

Reads `terminal_backend` from ~/.aws/cli-agent-orchestrator/config.json.
Default is "tmux". "herdr" is opt-in and experimental.
"""

import json
import logging
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.backends.base import TerminalBackend

logger = logging.getLogger(__name__)

# Default config path for CAO
_CONFIG_PATH = Path.home() / ".aws" / "cli-agent-orchestrator" / "config.json"


class ConfigurationError(Exception):
    """Raised when the backend configuration is invalid."""

    pass


class BackendFactory:
    """Factory that reads config and returns the appropriate backend instance."""

    @staticmethod
    def create(
        config_path: Optional[Path] = None, backend_override: Optional[str] = None
    ) -> TerminalBackend:
        """Create a TerminalBackend based on configuration.

        Args:
            config_path: Optional override for config file path (for testing)
            backend_override: Explicit backend name that takes precedence over
                the config file (e.g. from ``cao-server --terminal herdr``).
                When provided, ``terminal_backend`` in config.json is ignored,
                though other keys (such as ``herdr_session``) are still read.

        Returns:
            A configured TerminalBackend instance

        Raises:
            ConfigurationError: If terminal_backend value is unrecognized
        """
        path = config_path or _CONFIG_PATH
        backend_name = "tmux"  # default

        config = {}
        if path.exists():
            try:
                with open(path, "r") as f:
                    config = json.load(f)
                backend_name = config.get("terminal_backend", "tmux")
            except (json.JSONDecodeError, OSError) as e:
                logger.warning(f"Failed to read config from {path}: {e}; using default 'tmux'")

        # An explicit override (CLI flag) wins over the config file value.
        if backend_override:
            backend_name = backend_override

        if backend_name == "tmux":
            from cli_agent_orchestrator.backends.tmux_backend import TmuxBackend

            return TmuxBackend()
        elif backend_name == "herdr":
            from cli_agent_orchestrator.backends.herdr_backend import HerdrBackend

            herdr_session = config.get("herdr_session", "cao")
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
