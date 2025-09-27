"""Provider registry for managing CLI tool providers."""

import logging
from typing import Dict, Optional
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.q_cli import QCliProvider

logger = logging.getLogger(__name__)


class ProviderRegistry:
    """Registry for managing provider instances."""
    
    def __init__(self):
        self._providers: Dict[str, BaseProvider] = {}
    
    def create_provider(self, provider_type: Optional[str], terminal_id: str, 
                       session_name: str, window_name: str, agent_profile: str = None) -> Optional[BaseProvider]:
        """Create and register a provider instance."""
        try:
            if provider_type == "q_cli":
                if not agent_profile:
                    raise ValueError("Q CLI provider requires agent_profile parameter")
                provider = QCliProvider(terminal_id, session_name, window_name, agent_profile)
                self._providers[terminal_id] = provider
                logger.info(f"Created {provider_type} provider for terminal: {terminal_id} with agent: {agent_profile}")
                return provider
            else:
                raise ValueError(f"Unknown provider type: {provider_type}")
                
        except Exception as e:
            logger.error(f"Failed to create provider {provider_type} for terminal {terminal_id}: {e}")
            return None
    
    def get_provider(self, terminal_id: str) -> Optional[BaseProvider]:
        """Get provider instance by terminal ID."""
        return self._providers.get(terminal_id)
    
    async def cleanup_provider(self, terminal_id: str) -> None:
        """Clean up and remove provider instance."""
        try:
            provider = self._providers.get(terminal_id)
            if provider:
                await provider.cleanup()
                del self._providers[terminal_id]
                logger.info(f"Cleaned up provider for terminal: {terminal_id}")
        except Exception as e:
            logger.error(f"Failed to cleanup provider for terminal {terminal_id}: {e}")


# Global registry instance
provider_registry = ProviderRegistry()
