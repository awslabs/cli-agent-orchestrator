"""Provider manager as module singleton with direct terminal_id → provider mapping."""

import logging
from typing import Dict, Optional
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.q_cli import QCliProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.clients.database import get_terminal_metadata

logger = logging.getLogger(__name__)


class ProviderManager:
    """Simplified provider manager with direct mapping."""

    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}

    def create_provider(
        self,
        provider_type: str,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: str = None,
    ) -> BaseProvider:
        """Create and store provider instance."""
        try:
            if provider_type == "q_cli":
                if not agent_profile:
                    raise ValueError("Q CLI provider requires agent_profile parameter")
                provider = QCliProvider(terminal_id, tmux_session, tmux_window, agent_profile)
            elif provider_type == "claude_code":
                provider = ClaudeCodeProvider(terminal_id, tmux_session, tmux_window, agent_profile)
            else:
                raise ValueError(f"Unknown provider type: {provider_type}")

            # Store in direct mapping
            self._providers[terminal_id] = provider
            logger.info(f"Created {provider_type} provider for terminal: {terminal_id}")
            return provider

        except Exception as e:
            logger.error(
                f"Failed to create provider {provider_type} for terminal {terminal_id}: {e}"
            )
            raise

    def get_provider(self, terminal_id: str) -> Optional[BaseProvider]:
        """Get provider instance, creating on-demand if not found.

        Args:
            terminal_id: Terminal ID to get provider for

        Returns:
            Provider instance

        Raises:
            ValueError: If terminal not found in database or provider creation fails
        """
        # Check if already exists
        provider = self._providers.get(terminal_id)
        if provider:
            return provider

        # Try to create on-demand from database metadata
        metadata = get_terminal_metadata(terminal_id)
        if not metadata:
            raise ValueError(f"Terminal {terminal_id} not found in database")

        # Create provider on-demand
        provider = self.create_provider(
            metadata["provider"],
            terminal_id,
            metadata["tmux_session"],
            metadata["tmux_window"],
            metadata["agent_profile"],
        )
        logger.info(f"Created provider on-demand for terminal {terminal_id}")
        return provider

    def cleanup_provider(self, terminal_id: str) -> None:
        """Cleanup provider and remove from map (used when terminal is deleted)."""
        try:
            provider = self._providers.pop(terminal_id, None)
            if provider:
                provider.cleanup()
                logger.info(f"Cleaned up provider for terminal: {terminal_id}")
        except Exception as e:
            logger.error(f"Failed to cleanup provider for terminal {terminal_id}: {e}")

    def list_providers(self) -> Dict[str, str]:
        """List all active providers (for debugging)."""
        return {
            terminal_id: provider.__class__.__name__
            for terminal_id, provider in self._providers.items()
        }


# Module-level singleton
provider_manager = ProviderManager()
