"""Install command for CLI Agent Orchestrator."""

from importlib import resources
from pathlib import Path

import click
import requests

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    DEFAULT_PROVIDER,
    KIRO_AGENTS_DIR,
    LOCAL_AGENT_STORE_DIR,
    PROVIDERS,
    Q_AGENTS_DIR,
    SERVER_PORT,
)
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.q_agent import QAgentConfig
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile


def _download_agent(source: str) -> str:
    """Download or copy agent file to local store. Returns agent name."""
    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)

    # Handle URL
    if source.startswith("http://") or source.startswith("https://"):
        response = requests.get(source)
        response.raise_for_status()
        content = response.text

        # Extract filename from URL
        filename = Path(source).name
        if not filename.endswith(".md"):
            raise ValueError("URL must point to a .md file")

        dest_file = LOCAL_AGENT_STORE_DIR / filename
        dest_file.write_text(content)

        # Return agent name (filename without .md)
        return dest_file.stem

    # Handle file path
    source_path = Path(source)
    if source_path.exists():
        if not source_path.suffix == ".md":
            raise ValueError("File must be a .md file")

        dest_file = LOCAL_AGENT_STORE_DIR / source_path.name
        dest_file.write_text(source_path.read_text())

        # Return agent name (filename without .md)
        return dest_file.stem

    raise FileNotFoundError(f"Source not found: {source}")


@click.command()
@click.argument("agent_source")
@click.option(
    "--provider",
    type=click.Choice(PROVIDERS),
    default=DEFAULT_PROVIDER,
    help=f"Provider to use (default: {DEFAULT_PROVIDER})",
)
@click.option(
    "--use-hooks",
    is_flag=True,
    default=False,
    help="Enable CAO status hooks for kiro_cli provider (auto-updates terminal status)",
)
def install(agent_source: str, provider: str, use_hooks: bool):
    """
    Install an agent from local store, built-in store, URL, or file path.

    AGENT_SOURCE can be:
    - Agent name (e.g., 'developer', 'code_supervisor')
    - File path (e.g., './my-agent.md', '/path/to/agent.md')
    - URL (e.g., 'https://example.com/agent.md')
    """
    try:
        # Detect source type and handle accordingly
        if agent_source.startswith("http://") or agent_source.startswith("https://"):
            # Download from URL
            agent_name = _download_agent(agent_source)
            click.echo(f"✓ Downloaded agent from URL to local store")
        elif Path(agent_source).exists():
            # Copy from file path
            agent_name = _download_agent(agent_source)
            click.echo(f"✓ Copied agent from file to local store")
        else:
            # Treat as agent name
            agent_name = agent_source

        # Load agent profile using existing Pydantic parser
        profile = load_agent_profile(agent_name)

        # Ensure directories exist
        AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)

        # Determine source for context file
        local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
        if local_profile.exists():
            source_file = local_profile
        else:
            agent_store = resources.files("cli_agent_orchestrator.agent_store")
            source_file = agent_store / f"{agent_name}.md"

        # Copy markdown file to agent-context directory
        dest_file = AGENT_CONTEXT_DIR / f"{profile.name}.md"
        with open(source_file, "r") as src:
            dest_file.write_text(src.read())

        # Build allowedTools default if not specified
        allowed_tools = profile.allowedTools
        if allowed_tools is None:
            # Default: allow all built-in tools and all MCP server tools
            allowed_tools = ["@builtin", "fs_*", "execute_bash"]
            if profile.mcpServers:
                for server_name in profile.mcpServers.keys():
                    allowed_tools.append(f"@{server_name}")

        # Create agent config based on provider
        agent_file = None
        if provider == ProviderType.Q_CLI.value:
            Q_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            agent_config = QAgentConfig(
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
                model=profile.model,
            )
            safe_filename = profile.name.replace("/", "__")
            agent_file = Q_AGENTS_DIR / f"{safe_filename}.json"
            with open(agent_file, "w") as f:
                f.write(agent_config.model_dump_json(indent=2, exclude_none=True))

        elif provider == ProviderType.KIRO_CLI.value:
            KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)

            # Conditionally inject CAO status hooks based on --use-hooks flag
            hooks = profile.hooks or {}
            if use_hooks:
                # Auto-inject CAO status hooks for kiro_cli provider
                # Hook behavior:
                # - If CAO_TERMINAL_ID not set: succeed silently (agent usable outside CAO)
                # - If CAO_TERMINAL_ID is set: fail loudly if curl fails (we expect it to work)
                # - Retries: 3 attempts with exponential backoff for transient failures
                # This ensures hooks work correctly in CAO but don't break standalone usage
                cao_hooks = {
                    "agentSpawn": [
                        {
                            "command": f'[ -z "$CAO_TERMINAL_ID" ] || curl -sf --max-time 2 --retry 3 --retry-delay 1 --retry-max-time 10 -X POST "http://localhost:{SERVER_PORT}/terminals/$CAO_TERMINAL_ID/status?new_status=idle"'
                        }
                    ],
                    "userPromptSubmit": [
                        {
                            "command": f'[ -z "$CAO_TERMINAL_ID" ] || curl -sf --max-time 2 --retry 3 --retry-delay 1 --retry-max-time 10 -X POST "http://localhost:{SERVER_PORT}/terminals/$CAO_TERMINAL_ID/status?new_status=processing"'
                        }
                    ],
                    "stop": [
                        {
                            "command": f'[ -z "$CAO_TERMINAL_ID" ] || curl -sf --max-time 2 --retry 3 --retry-delay 1 --retry-max-time 10 -X POST "http://localhost:{SERVER_PORT}/terminals/$CAO_TERMINAL_ID/status?new_status=idle"'
                        }
                    ],
                }

                # Merge with existing hooks if any
                for event, handlers in cao_hooks.items():
                    if event not in hooks:
                        hooks[event] = handlers
                    else:
                        # Prepend CAO hook to existing handlers
                        hooks[event] = handlers + hooks[event]

            agent_config = KiroAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=[f"file://{dest_file.absolute()}"],
                prompt=profile.prompt,
                mcpServers=profile.mcpServers,
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=hooks,
                model=profile.model,
            )
            safe_filename = profile.name.replace("/", "__")
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            with open(agent_file, "w") as f:
                f.write(agent_config.model_dump_json(indent=2, exclude_none=True))

        click.echo(f"✓ Agent '{profile.name}' installed successfully")
        click.echo(f"✓ Context file: {dest_file}")
        if agent_file:
            click.echo(f"✓ {provider} agent: {agent_file}")

            # Verify hooks were injected if --use-hooks was specified
            if use_hooks and provider == ProviderType.KIRO_CLI.value:
                import json

                if agent_file.exists():
                    config = json.loads(agent_file.read_text())
                    if "hooks" in config and all(
                        event in config["hooks"]
                        for event in ["agentSpawn", "userPromptSubmit", "stop"]
                    ):
                        click.echo(
                            f"✓ CAO status hooks enabled (agentSpawn, userPromptSubmit, stop)"
                        )
                    else:
                        click.echo("⚠ Warning: Failed to inject CAO status hooks", err=True)

    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        return
    except requests.RequestException as e:
        click.echo(f"Error: Failed to download agent: {e}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: Failed to install agent: {e}", err=True)
        return
