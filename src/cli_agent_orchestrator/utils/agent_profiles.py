"""Agent profile utilities."""

import frontmatter
from importlib import resources
from pathlib import Path
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load agent profile from local or built-in agent store."""
    try:
        # Check local store first
        local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
        if local_profile.exists():
            profile_data = frontmatter.loads(local_profile.read_text())
            profile_data.metadata['system_prompt'] = profile_data.content.strip()
            return AgentProfile(**profile_data.metadata)
        
        # Fall back to built-in store
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        profile_file = agent_store / f"{agent_name}.md"
        
        if not profile_file.is_file():
            raise FileNotFoundError(f"Agent profile not found: {agent_name}")
        
        # Parse frontmatter
        profile_data = frontmatter.loads(profile_file.read_text())
        
        # Add system_prompt from markdown content
        profile_data.metadata['system_prompt'] = profile_data.content.strip()
        
        # Let Pydantic handle the nested object parsing including mcpServers
        return AgentProfile(**profile_data.metadata)
        
    except Exception as e:
        raise RuntimeError(f"Failed to load agent profile '{agent_name}': {e}")
