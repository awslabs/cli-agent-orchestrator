"""Agent profile utilities."""

import logging
from importlib import resources
from pathlib import Path

import frontmatter

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR, PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile

logger = logging.getLogger(__name__)


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load agent profile from local or built-in agent store."""
    try:
        # Check local store first
        local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
        if local_profile.exists():
            profile_data = frontmatter.loads(local_profile.read_text())
            profile_data.metadata["system_prompt"] = profile_data.content.strip()
            return AgentProfile(**profile_data.metadata)

        # Fall back to built-in store
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        profile_file = agent_store / f"{agent_name}.md"

        if not profile_file.is_file():
            raise FileNotFoundError(f"Agent profile not found: {agent_name}")

        # Parse frontmatter
        profile_data = frontmatter.loads(profile_file.read_text())

        # Add system_prompt from markdown content
        profile_data.metadata["system_prompt"] = profile_data.content.strip()

        # Let Pydantic handle the nested object parsing including mcpServers
        return AgentProfile(**profile_data.metadata)

    except Exception as e:
        raise RuntimeError(f"Failed to load agent profile '{agent_name}': {e}")


def resolve_provider(agent_profile_name: str, fallback_provider: str) -> str:
    """Resolve the provider to use for an agent profile.

    Loads the agent profile from the CAO agent store and checks for a
    ``provider`` key.  If present and valid, returns the profile's provider.
    Otherwise returns the fallback provider (typically inherited from the
    calling terminal).

    Args:
        agent_profile_name: Name of the agent profile to look up.
        fallback_provider: Provider to use when the profile does not specify
            one or specifies an invalid value.

    Returns:
        Resolved provider type string.
    """
    try:
        profile = load_agent_profile(agent_profile_name)
    except RuntimeError:
        # Profile not found — provider.initialize() will surface
        # a clear error later.  Fall back for now.
        return fallback_provider

    if profile.provider:
        if profile.provider in PROVIDERS:
            return profile.provider
        else:
            logger.warning(
                "Agent profile '%s' has invalid provider '%s'. "
                "Valid providers: %s. Falling back to '%s'.",
                agent_profile_name,
                profile.provider,
                PROVIDERS,
                fallback_provider,
            )

    return fallback_provider
