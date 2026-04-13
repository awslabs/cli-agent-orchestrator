"""Install command for CLI Agent Orchestrator."""

import re
from importlib import resources
from pathlib import Path

import click
import frontmatter
import requests  # type: ignore[import-untyped]

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    CAO_ENV_FILE,
    COPILOT_AGENTS_DIR,
    DEFAULT_PROVIDER,
    KIRO_AGENTS_DIR,
    LOCAL_AGENT_STORE_DIR,
    PROVIDERS,
    Q_AGENTS_DIR,
    SKILLS_DIR,
)
from cli_agent_orchestrator.models.copilot_agent import CopilotAgentConfig
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.q_agent import QAgentConfig
from cli_agent_orchestrator.utils.agent_profiles import parse_agent_profile_text
from cli_agent_orchestrator.utils.env import resolve_env_vars, set_env_var
from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt


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


def _parse_env_assignment(env_assignment: str) -> tuple[str, str]:
    """Parse a ``KEY=VALUE`` env assignment for install-time injection."""
    if "=" not in env_assignment:
        raise click.BadParameter(
            f"Invalid env var '{env_assignment}'. Expected format KEY=VALUE.", param_hint="--env"
        )

    key, value = env_assignment.split("=", 1)
    if not key:
        raise click.BadParameter(
            f"Invalid env var '{env_assignment}'. Key must not be empty.", param_hint="--env"
        )
    return key, value


@click.command()
@click.argument("agent_source")
@click.option(
    "--provider",
    type=click.Choice(PROVIDERS),
    default=DEFAULT_PROVIDER,
    help=f"Provider to use (default: {DEFAULT_PROVIDER})",
)
@click.option(
    "--env",
    "env_vars",
    multiple=True,
    help=(
        "Set env vars before installing the agent. Values are stored in "
        "~/.aws/cli-agent-orchestrator/.env and can be referenced in profiles as ${VAR}. "
        "Repeatable: --env KEY=VALUE. Example: --env API_TOKEN=my-secret-token."
    ),
)
def install(agent_source: str, provider: str, env_vars: tuple[str, ...]):
    """
    Install an agent from local store, built-in store, URL, or file path.

    AGENT_SOURCE can be:
    - Agent name (e.g., 'developer', 'code_supervisor')
    - File path (e.g., './my-agent.md', '/path/to/agent.md')
    - URL (e.g., 'https://example.com/agent.md')

    Profiles can reference values from ~/.aws/cli-agent-orchestrator/.env using ${VAR}
    placeholders in frontmatter or markdown content. Use `cao env set KEY VALUE` to
    manage those values separately, or pass `--env KEY=VALUE` during install to write
    them before the profile is loaded.

    Example:
    \b
        cao install ./service-agent.md --provider claude_code \
          --env API_TOKEN=my-secret-token \
          --env SERVICE_URL=http://127.0.0.1:27124
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

        for env_assignment in env_vars:
            key, value = _parse_env_assignment(env_assignment)
            set_env_var(key, value)

        # Determine source file for the agent profile
        local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
        if local_profile.exists():
            source_file = local_profile
        else:
            agent_store = resources.files("cli_agent_orchestrator.agent_store")
            source_file = agent_store / f"{agent_name}.md"

        # Read source once; resolve for in-memory profile, keep raw for context file
        raw_content = source_file.read_text()
        resolved_content = resolve_env_vars(raw_content)
        profile = parse_agent_profile_text(resolved_content, agent_name)

        # Warn about unresolved placeholders that will leak into provider configs
        unresolved = set(re.findall(r"\$\{(\w+)\}", resolved_content))
        if unresolved:
            names = ", ".join(sorted(unresolved))
            click.echo(
                f"⚠ Unresolved env var(s) in profile: {names}. "
                f"Set them with `cao env set` or pass --env KEY=VALUE.",
                err=True,
            )

        # Write unresolved source to agent-context (secrets stay in .env)
        AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
        dest_file = AGENT_CONTEXT_DIR / f"{profile.name}.md"
        dest_file.write_text(raw_content)

        # Resolve allowedTools from profile → role defaults → developer defaults
        from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools

        mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)

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
                prompt=compose_agent_prompt(profile),
                mcpServers=profile.mcpServers,
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            safe_filename = profile.name.replace("/", "__")
            agent_file = Q_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                agent_config.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
            )

        elif provider == ProviderType.KIRO_CLI.value:
            KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            # Kiro natively supports skill:// resources with progressive loading
            # (metadata at startup, full content on demand).
            kiro_resources = [
                f"file://{dest_file.absolute()}",
                f"skill://{SKILLS_DIR}/**/SKILL.md",
            ]
            raw_prompt = (
                profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
            )
            agent_config = KiroAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=kiro_resources,
                prompt=raw_prompt,
                mcpServers=profile.mcpServers,
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            safe_filename = profile.name.replace("/", "__")
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                agent_config.model_dump_json(indent=2, exclude_none=True), encoding="utf-8"
            )

        elif provider == ProviderType.COPILOT_CLI.value:
            COPILOT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            system_prompt = profile.system_prompt.strip() if profile.system_prompt else ""
            fallback_prompt = profile.prompt.strip() if profile.prompt else ""
            base_prompt = system_prompt or fallback_prompt
            if not base_prompt:
                raise ValueError(
                    f"Agent '{profile.name}' has no usable prompt content for Copilot "
                    "(both system_prompt and prompt are empty or whitespace)"
                )

            # Bake skill catalog into the agent prompt body (same as Kiro/Q)
            prompt = compose_agent_prompt(profile, base_prompt=base_prompt) or base_prompt

            safe_filename = profile.name.replace("/", "__")
            agent_file = COPILOT_AGENTS_DIR / f"{safe_filename}.agent.md"
            agent_config = CopilotAgentConfig(
                name=profile.name,
                description=profile.description,
                prompt=prompt,
            )
            agent_post = frontmatter.Post(
                prompt.rstrip(),
                name=agent_config.name,
                description=agent_config.description,
            )
            agent_file.write_text(frontmatter.dumps(agent_post), encoding="utf-8")

        click.echo(f"✓ Agent '{profile.name}' installed successfully")
        if env_vars:
            click.echo(f"✓ Set {len(env_vars)} env var(s) in {CAO_ENV_FILE}")
        click.echo(f"✓ Context file: {dest_file}")
        if agent_file:
            click.echo(f"✓ {provider} agent: {agent_file}")

    except click.BadParameter:
        raise
    except FileNotFoundError as e:
        click.echo(f"Error: {e}", err=True)
        return
    except requests.RequestException as e:
        click.echo(f"Error: Failed to download agent: {e}", err=True)
        return
    except Exception as e:
        click.echo(f"Error: Failed to install agent: {e}", err=True)
        return
