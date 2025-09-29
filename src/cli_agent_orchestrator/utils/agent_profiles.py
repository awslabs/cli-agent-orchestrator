"""Agent profile utilities."""

import frontmatter
from importlib import resources
from cli_agent_orchestrator.models.agent_profile import AgentProfile


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load agent profile from agent_store."""
    try:
        # Use importlib.resources to access agent_store files
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
