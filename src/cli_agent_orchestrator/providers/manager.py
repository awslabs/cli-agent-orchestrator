"""Provider manager as module singleton with direct terminal_id → provider mapping."""

import inspect
import logging
from typing import Dict, List, Optional, Type

from cli_agent_orchestrator.clients.database import get_terminal_metadata
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.providers.antigravity_cli import AntigravityCliProvider
from cli_agent_orchestrator.providers.base import BaseProvider
from cli_agent_orchestrator.providers.claude_code import ClaudeCodeProvider
from cli_agent_orchestrator.providers.codex import CodexProvider
from cli_agent_orchestrator.providers.copilot_cli import CopilotCliProvider
from cli_agent_orchestrator.providers.cursor_cli import CursorCliProvider
from cli_agent_orchestrator.providers.devin_cli import DevinCliProvider
from cli_agent_orchestrator.providers.hermes import HermesProvider
from cli_agent_orchestrator.providers.kimi_cli import KimiCliProvider
from cli_agent_orchestrator.providers.kiro_cli import KiroCliProvider
from cli_agent_orchestrator.providers.mock_cli import MockCliProvider
from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider

logger = logging.getLogger(__name__)


class ProviderManager:
    """Simplified provider manager with direct mapping."""

    _PROVIDER_CLASSES: Dict[str, Type[BaseProvider]] = {
        ProviderType.KIRO_CLI.value: KiroCliProvider,
        ProviderType.CLAUDE_CODE.value: ClaudeCodeProvider,
        ProviderType.CODEX.value: CodexProvider,
        ProviderType.COPILOT_CLI.value: CopilotCliProvider,
        ProviderType.KIMI_CLI.value: KimiCliProvider,
        ProviderType.OPENCODE_CLI.value: OpenCodeCliProvider,
        ProviderType.HERMES.value: HermesProvider,
        ProviderType.CURSOR_CLI.value: CursorCliProvider,
        ProviderType.ANTIGRAVITY_CLI.value: AntigravityCliProvider,
        ProviderType.DEVIN_CLI.value: DevinCliProvider,
        ProviderType.MOCK_CLI.value: MockCliProvider,
    }

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}

    def _get_provider_class(self, provider_type: str) -> Type[BaseProvider]:
        """Get provider class for given type."""
        if provider_type not in self._PROVIDER_CLASSES:
            raise ValueError(f"Unknown provider type: {provider_type}")
        return self._PROVIDER_CLASSES[provider_type]

    @staticmethod
    def _build_provider_kwargs(
        provider_type: str,
        provider_cls: Type[BaseProvider],
        terminal_id: str,
        session_name: str,
        window_name: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        model: Optional[str],
    ) -> dict:
        """Build the keyword arguments a provider constructor actually accepts."""
        params = inspect.signature(provider_cls.__init__).parameters
        kwargs: dict = {
            "terminal_id": terminal_id,
            "session_name": session_name,
            "window_name": window_name,
        }

        if "allowed_tools" in params:
            kwargs["allowed_tools"] = allowed_tools

        if "agent_profile" in params:
            if provider_type == ProviderType.KIRO_CLI.value and not agent_profile:
                raise ValueError("Kiro CLI provider requires agent_profile parameter")
            if agent_profile is not None:
                kwargs["agent_profile"] = agent_profile

        if "model" in params and model is not None:
            kwargs["model"] = model

        if "skill_prompt" in params and skill_prompt is not None:
            kwargs["skill_prompt"] = skill_prompt

        return kwargs

    def create_provider(
        self,
        provider_type: str,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str] = None,
        allowed_tools: Optional[List[str]] = None,
        skill_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> BaseProvider:
        """Create and store provider instance."""
        try:
            provider_cls = self._get_provider_class(provider_type)
            kwargs = self._build_provider_kwargs(
                provider_type,
                provider_cls,
                terminal_id,
                tmux_session,
                tmux_window,
                agent_profile,
                allowed_tools,
                skill_prompt,
                model,
            )
            provider = provider_cls(**kwargs)

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

        # Create provider on-demand, restoring the persisted tool restrictions.
        provider = self.create_provider(
            metadata["provider"],
            terminal_id,
            metadata["tmux_session"],
            metadata["tmux_window"],
            metadata["agent_profile"],
            allowed_tools=metadata.get("allowed_tools"),
        )
        # Restore shell_command baseline from DB so get_status() can detect kiro exit.
        # The terminal already exists in the DB, so its CLI has long since
        # launched — mark the provider as initialized so KiroCliProvider's
        # post-launch checks (Check 3) trust the restored baseline. Without
        # this, a restored terminal that has returned to the shell would be
        # misreported as PROCESSING indefinitely.
        if metadata.get("shell_command"):
            provider.shell_baseline = metadata["shell_command"]
            if hasattr(provider, "_initialized"):
                provider._initialized = True
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
