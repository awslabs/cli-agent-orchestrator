"""Install command for CLI Agent Orchestrator."""

import click
from pathlib import Path
from importlib import resources

from cli_agent_orchestrator.constants import AGENT_CONTEXT_DIR, Q_AGENTS_DIR
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.models.q_agent import QAgentConfig


@click.command()
@click.argument('agent_name')
def install(agent_name: str):
    """
    Install an agent from agent_store to Q CLI.
    
    AGENT_NAME should be the name of the agent (e.g., 'developer', 'code_supervisor')
    """
    try:
        # Load agent profile using existing Pydantic parser
        profile = load_agent_profile(agent_name)
        
        # Ensure directories exist
        AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        Q_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        
        # Copy markdown file to agent-context directory
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        source_file = agent_store / f"{agent_name}.md"
        dest_file = AGENT_CONTEXT_DIR / f"{profile.name}.md"
        
        with open(source_file, 'r') as src:
            dest_file.write_text(src.read())
        
        # Build allowedTools default if not specified
        allowed_tools = profile.allowedTools
        if allowed_tools is None:
            # Default: allow all built-in tools and all MCP server tools
            allowed_tools = ["@builtin"]
            if profile.mcpServers:
                for server_name in profile.mcpServers.keys():
                    allowed_tools.append(f"@{server_name}")
        
        # Create Q CLI agent config using Pydantic model
        q_agent_config = QAgentConfig(
            name=profile.name,
            description=profile.description,
            tools=profile.tools if profile.tools is not None else ["*"],
            allowedTools=allowed_tools,
            resources=[f"file://{dest_file.absolute()}"],
            prompt=profile.prompt,
            mcpServers=profile.mcpServers,
            toolAliases=profile.toolAliases,
            toolsSettings=profile.toolsSettings,
            hooks=profile.hooks,
            model=profile.model
        )
        
        # Create path-safe filename (replace / with __)
        safe_filename = profile.name.replace('/', '__')
        
        # Write Q CLI agent JSON using Pydantic's model_dump
        q_agent_file = Q_AGENTS_DIR / f"{safe_filename}.json"
        with open(q_agent_file, 'w') as f:
            f.write(q_agent_config.model_dump_json(indent=2, exclude_none=True))
        
        click.echo(f"✓ Agent '{profile.name}' installed successfully")
        click.echo(f"✓ Context file: {dest_file}")
        click.echo(f"✓ Q CLI agent: {q_agent_file}")
        
    except FileNotFoundError:
        click.echo(f"Error: Agent '{agent_name}' not found in agent_store", err=True)
        return
    except Exception as e:
        click.echo(f"Error: Failed to install agent: {e}", err=True)
        return
