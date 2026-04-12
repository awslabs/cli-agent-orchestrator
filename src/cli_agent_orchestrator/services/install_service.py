"""Service helpers for installing agent profiles."""

import re
from importlib import resources
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import frontmatter
import requests  # type: ignore[import-untyped]
from pydantic import BaseModel

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    COPILOT_AGENTS_DIR,
    KIRO_AGENTS_DIR,
    LOCAL_AGENT_STORE_DIR,
    Q_AGENTS_DIR,
)
from cli_agent_orchestrator.models.copilot_agent import CopilotAgentConfig
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.q_agent import QAgentConfig
from cli_agent_orchestrator.utils.agent_profiles import (
    _validate_agent_name,
    parse_agent_profile_text,
)
from cli_agent_orchestrator.utils.env import resolve_env_vars, set_env_var
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools


class InstallResult(BaseModel):
    """Structured result for agent profile installation."""

    success: bool
    message: str
    agent_name: Optional[str] = None
    context_file: Optional[str] = None
    agent_file: Optional[str] = None
    unresolved_vars: Optional[List[str]] = None


def _download_agent(source: str) -> str:
    """Download or copy an agent profile into the local agent store."""
    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)

    if source.startswith(("http://", "https://")):
        response = requests.get(source)
        response.raise_for_status()

        filename = Path(source).name
        if not filename.endswith(".md"):
            raise ValueError("URL must point to a .md file")

        dest_file = LOCAL_AGENT_STORE_DIR / filename
        dest_file.write_text(response.text, encoding="utf-8")
        return dest_file.stem

    source_path = Path(source)
    if source_path.exists():
        if source_path.suffix != ".md":
            raise ValueError("File must be a .md file")

        dest_file = LOCAL_AGENT_STORE_DIR / source_path.name
        dest_file.write_text(source_path.read_text(encoding="utf-8"), encoding="utf-8")
        return dest_file.stem

    raise FileNotFoundError(f"Source not found: {source}")


def parse_env_assignment(env_assignment: str) -> Tuple[str, str]:
    """Parse a ``KEY=VALUE`` assignment used for install-time env injection."""
    if "=" not in env_assignment:
        raise ValueError(f"Invalid env var '{env_assignment}'. Expected format KEY=VALUE.")

    key, value = env_assignment.split("=", 1)
    if not key:
        raise ValueError(f"Invalid env var '{env_assignment}'. Key must not be empty.")

    return key, value


def _resolve_named_source(agent_name: str) -> str:
    """Locate a named profile and return its raw content."""
    _validate_agent_name(agent_name)

    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_extra_agent_dirs,
    )

    local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
    if local_profile.exists():
        return local_profile.read_text(encoding="utf-8")

    for dir_path in get_agent_dirs().values():
        directory = Path(dir_path)
        if not directory.exists():
            continue

        flat_profile = directory / f"{agent_name}.md"
        if flat_profile.exists():
            return flat_profile.read_text(encoding="utf-8")

        nested_profile = directory / agent_name / "agent.md"
        if nested_profile.exists():
            return nested_profile.read_text(encoding="utf-8")

    for extra_dir in get_extra_agent_dirs():
        directory = Path(extra_dir)
        if not directory.exists():
            continue

        flat_profile = directory / f"{agent_name}.md"
        if flat_profile.exists():
            return flat_profile.read_text(encoding="utf-8")

        nested_profile = directory / agent_name / "agent.md"
        if nested_profile.exists():
            return nested_profile.read_text(encoding="utf-8")

    agent_store = resources.files("cli_agent_orchestrator.agent_store")
    built_in_profile = agent_store / f"{agent_name}.md"
    if built_in_profile.is_file():
        return built_in_profile.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Agent profile not found: {agent_name}")


def _write_context_file(agent_name: str, raw_content: str) -> Path:
    """Write the unresolved profile source to the shared context directory."""
    AGENT_CONTEXT_DIR.mkdir(parents=True, exist_ok=True)
    context_file = AGENT_CONTEXT_DIR / f"{agent_name}.md"
    context_file.write_text(raw_content, encoding="utf-8")
    return context_file


def _build_provider_config(
    profile_name: str,
    resolved_prompt: str,
    description: str,
) -> frontmatter.Post:
    """Create the frontmatter post for a Copilot agent file."""
    return frontmatter.Post(
        resolved_prompt.rstrip(),
        name=profile_name,
        description=description,
    )


def install_agent(
    source: str,
    provider: str,
    env_vars: Optional[Dict[str, str]] = None,
) -> InstallResult:
    """Install an agent profile for the requested provider."""
    try:
        if source.startswith(("http://", "https://")) or Path(source).exists():
            agent_name = _download_agent(source)
        else:
            agent_name = source

        if env_vars:
            for key, value in env_vars.items():
                set_env_var(key, value)

        raw_content = _resolve_named_source(agent_name)
        resolved_content = resolve_env_vars(raw_content)
        profile = parse_agent_profile_text(resolved_content, agent_name)

        unresolved_vars = sorted(set(re.findall(r"\$\{(\w+)\}", resolved_content)))
        context_file = _write_context_file(profile.name, raw_content)

        mcp_server_names = list(profile.mcpServers.keys()) if profile.mcpServers else None
        allowed_tools = resolve_allowed_tools(profile.allowedTools, profile.role, mcp_server_names)

        agent_file: Optional[Path] = None
        safe_filename = profile.name.replace("/", "__")

        if provider == ProviderType.Q_CLI.value:
            Q_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            q_agent_config = QAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=[f"file://{context_file.absolute()}"],
                prompt=profile.prompt,
                mcpServers=profile.mcpServers,
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            agent_file = Q_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                q_agent_config.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )

        elif provider == ProviderType.KIRO_CLI.value:
            KIRO_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            kiro_agent_config = KiroAgentConfig(
                name=profile.name,
                description=profile.description,
                tools=profile.tools if profile.tools is not None else ["*"],
                allowedTools=allowed_tools,
                resources=[f"file://{context_file.absolute()}"],
                prompt=profile.prompt,
                mcpServers=profile.mcpServers,
                toolAliases=profile.toolAliases,
                toolsSettings=profile.toolsSettings,
                hooks=profile.hooks,
                model=profile.model,
            )
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                kiro_agent_config.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )

        elif provider == ProviderType.COPILOT_CLI.value:
            COPILOT_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            system_prompt = profile.system_prompt.strip() if profile.system_prompt else ""
            fallback_prompt = profile.prompt.strip() if profile.prompt else ""
            resolved_prompt = system_prompt or fallback_prompt
            if not resolved_prompt:
                raise ValueError(
                    f"Agent '{profile.name}' has no usable prompt content for Copilot "
                    "(both system_prompt and prompt are empty or whitespace)"
                )

            copilot_agent_config = CopilotAgentConfig(
                name=profile.name,
                description=profile.description,
                prompt=resolved_prompt,
            )
            agent_file = COPILOT_AGENTS_DIR / f"{safe_filename}.agent.md"
            agent_file.write_text(
                frontmatter.dumps(
                    _build_provider_config(
                        profile_name=copilot_agent_config.name,
                        resolved_prompt=copilot_agent_config.prompt,
                        description=copilot_agent_config.description,
                    )
                ),
                encoding="utf-8",
            )

        return InstallResult(
            success=True,
            message=f"Agent '{profile.name}' installed successfully",
            agent_name=profile.name,
            context_file=str(context_file),
            agent_file=str(agent_file) if agent_file else None,
            unresolved_vars=unresolved_vars or None,
        )

    except requests.RequestException as exc:
        return InstallResult(success=False, message=f"Failed to download agent: {exc}")
    except FileNotFoundError as exc:
        return InstallResult(success=False, message=str(exc))
    except (ValueError, OSError) as exc:
        return InstallResult(success=False, message=f"Failed to install agent: {exc}")
