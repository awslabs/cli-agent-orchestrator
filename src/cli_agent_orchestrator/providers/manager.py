"""Provider manager as module singleton with direct terminal_id → provider mapping."""

import logging
from typing import Dict, List, Optional

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
from cli_agent_orchestrator.providers.opencode_cli import OpenCodeCliProvider

logger = logging.getLogger(__name__)


class ProviderManager:
    """Simplified provider manager with direct mapping."""

    def __init__(self) -> None:
        self._providers: Dict[str, BaseProvider] = {}

    def _get_provider_factory(self, provider_type: str):
        """Get provider factory function for given type."""
        factories = {
            ProviderType.KIRO_CLI.value: self._create_kiro_cli_provider,
            ProviderType.CLAUDE_CODE.value: self._create_claude_code_provider,
            ProviderType.CODEX.value: self._create_codex_provider,
            ProviderType.COPILOT_CLI.value: self._create_copilot_cli_provider,
            ProviderType.KIMI_CLI.value: self._create_kimi_cli_provider,
            ProviderType.OPENCODE_CLI.value: self._create_opencode_cli_provider,
            ProviderType.HERMES.value: self._create_hermes_provider,
            ProviderType.CURSOR_CLI.value: self._create_cursor_cli_provider,
            ProviderType.ANTIGRAVITY_CLI.value: self._create_antigravity_cli_provider,
            ProviderType.DEVIN_CLI.value: self._create_devin_cli_provider,
        }

        if provider_type not in factories:
            raise ValueError(f"Unknown provider type: {provider_type}")

        return factories[provider_type]

    def _create_kiro_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        **kwargs,
    ) -> KiroCliProvider:
        if not agent_profile:
            raise ValueError("Kiro CLI provider requires agent_profile parameter")
        return KiroCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
        )

    def _create_claude_code_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> ClaudeCodeProvider:
        return ClaudeCodeProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
        )

    def _create_codex_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> CodexProvider:
        return CodexProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
        )

    def _create_copilot_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        model: Optional[str],
        **kwargs,
    ) -> CopilotCliProvider:
        return CopilotCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            model=model,
        )

    def _create_kimi_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> KimiCliProvider:
        return KimiCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
        )

    def _create_opencode_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        model: Optional[str],
        **kwargs,
    ) -> OpenCodeCliProvider:
        return OpenCodeCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            model=model,
        )

    def _create_hermes_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> HermesProvider:
        return HermesProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
        )

    def _create_cursor_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        model: Optional[str],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> CursorCliProvider:
        return CursorCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            model=model,
            skill_prompt=skill_prompt,
        )

    def _create_antigravity_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        model: Optional[str],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> AntigravityCliProvider:
        return AntigravityCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            model=model,
            skill_prompt=skill_prompt,
        )

    def _create_devin_cli_provider(
        self,
        terminal_id: str,
        tmux_session: str,
        tmux_window: str,
        agent_profile: Optional[str],
        allowed_tools: Optional[List[str]],
        skill_prompt: Optional[str],
        **kwargs,
    ) -> DevinCliProvider:
        return DevinCliProvider(
            terminal_id,
            tmux_session,
            tmux_window,
            agent_profile,
            allowed_tools,
            skill_prompt=skill_prompt,
        )

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
            factory = self._get_provider_factory(provider_type)
            provider = factory(
                terminal_id=terminal_id,
                tmux_session=tmux_session,
                tmux_window=tmux_window,
                agent_profile=agent_profile,
                allowed_tools=allowed_tools,
                skill_prompt=skill_prompt,
                model=model,
            )

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
