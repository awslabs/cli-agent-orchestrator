"""Service helpers for installing agent profiles."""

import os
import re
from pathlib import Path
from typing import Dict, List, Literal, Optional, Tuple
from urllib.parse import urlparse

import frontmatter
import requests  # type: ignore[import-untyped]
from pydantic import BaseModel

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    COPILOT_AGENTS_DIR,
    KIRO_AGENTS_DIR,
    LOCAL_AGENT_STORE_DIR,
    OPENCODE_AGENTS_DIR,
    Q_AGENTS_DIR,
    SKILLS_DIR,
)
from cli_agent_orchestrator.models.copilot_agent import CopilotAgentConfig
from cli_agent_orchestrator.models.kiro_agent import KiroAgentConfig
from cli_agent_orchestrator.models.opencode_agent import OpenCodeAgentConfig
from cli_agent_orchestrator.models.provider import ProviderType
from cli_agent_orchestrator.models.q_agent import QAgentConfig
from cli_agent_orchestrator.utils.agent_profiles import (
    _read_agent_profile_source,
    parse_agent_profile_text,
)
from cli_agent_orchestrator.utils.env import resolve_env_vars, set_env_var
from cli_agent_orchestrator.utils.opencode_config import (
    ensure_skills_symlink,
    remove_agent_tools,
    to_opencode_agent_id,
    translate_mcp_server_config,
    upsert_agent_tools,
    upsert_mcp_server,
)
from cli_agent_orchestrator.utils.opencode_permissions import cao_tools_to_opencode_permission
from cli_agent_orchestrator.utils.skill_injection import compose_agent_prompt
from cli_agent_orchestrator.utils.tool_mapping import resolve_allowed_tools


class InstallResult(BaseModel):
    """Structured result for agent profile installation."""

    success: bool
    message: str
    agent_name: Optional[str] = None
    context_file: Optional[str] = None
    agent_file: Optional[str] = None
    unresolved_vars: Optional[List[str]] = None
    source_kind: Optional[Literal["url", "file", "name"]] = None


_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

_DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
    }
)

_HTTP_TIMEOUT = (5, 30)  # (connect, read) seconds


def _allowed_download_hosts() -> frozenset:
    override = os.environ.get("CAO_PROFILE_ALLOWED_HOSTS")
    if override:
        hosts = {h.strip().lower() for h in override.split(",") if h.strip()}
        if hosts:
            return frozenset(hosts)
    return _DEFAULT_ALLOWED_HOSTS


def _download_agent(source: str) -> str:
    """Download or copy an agent profile into the local agent store."""
    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)

    if source.startswith(("http://", "https://")):
        parsed = urlparse(source)
        if parsed.scheme != "https":
            raise ValueError("Profile URL must use https://")
        host = (parsed.hostname or "").lower()
        if host not in _allowed_download_hosts():
            raise ValueError(
                f"Host '{host}' is not in the allowed downloader hosts. "
                "Set CAO_PROFILE_ALLOWED_HOSTS to extend the allowlist."
            )
        filename = Path(parsed.path).name
        if not filename.endswith(".md"):
            raise ValueError("URL must point to a .md file")
        if not _PROFILE_NAME_RE.fullmatch(Path(filename).stem):
            raise ValueError("URL filename stem must match [A-Za-z0-9_-]{1,64}")

        response = requests.get(source, timeout=_HTTP_TIMEOUT, allow_redirects=False)
        if response.is_redirect:
            raise ValueError("Redirects are not allowed for profile downloads.")
        response.raise_for_status()

        dest_file = LOCAL_AGENT_STORE_DIR / filename
        dest_file.write_text(response.text, encoding="utf-8")
        return dest_file.stem

    source_path = Path(source).resolve()
    if source_path.exists():
        if source_path.suffix != ".md":
            raise ValueError("File must be a .md file")
        if not _PROFILE_NAME_RE.fullmatch(source_path.stem):
            raise ValueError(f"File stem '{source_path.stem}' must match [A-Za-z0-9_-]{{1,64}}")

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
    *,
    allow_file_source: bool = True,
) -> InstallResult:
    """Install an agent profile for the requested provider.

    ``allow_file_source`` controls whether ``source`` may refer to a local
    filesystem path. CLI callers leave this True (the user owns the local
    filesystem); HTTP/MCP callers must pass False so a remote caller cannot
    coerce the server into reading arbitrary files.
    """
    try:
        valid_providers = [provider_type.value for provider_type in ProviderType]
        if provider not in valid_providers:
            return InstallResult(
                success=False,
                message=(
                    f"Invalid provider '{provider}'. "
                    f"Valid providers: {', '.join(valid_providers)}"
                ),
            )

        if source.startswith(("http://", "https://")):
            agent_name = _download_agent(source)
            source_kind: Literal["url", "file", "name"] = "url"
        elif allow_file_source and Path(source).exists():
            agent_name = _download_agent(source)
            source_kind = "file"
        else:
            if not _PROFILE_NAME_RE.fullmatch(source):
                return InstallResult(
                    success=False,
                    message=(
                        f"Invalid profile name '{source}'. "
                        "Expected a name matching [A-Za-z0-9_-]{1,64}, "
                        "an https:// URL, or (CLI only) a local .md file path."
                    ),
                )
            agent_name = source
            source_kind = "name"

        if env_vars:
            for key, value in env_vars.items():
                set_env_var(key, value)

        raw_content = _read_agent_profile_source(agent_name)
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
                prompt=compose_agent_prompt(profile),
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
            # Kiro natively supports skill:// resources with progressive loading
            # (metadata at startup, full content on demand).
            kiro_resources = [
                f"file://{context_file.absolute()}",
                f"skill://{SKILLS_DIR}/**/SKILL.md",
            ]
            raw_prompt = (
                profile.prompt.strip() if profile.prompt and profile.prompt.strip() else None
            )
            kiro_agent_config = KiroAgentConfig(
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
            agent_file = KIRO_AGENTS_DIR / f"{safe_filename}.json"
            agent_file.write_text(
                kiro_agent_config.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
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

            prompt = compose_agent_prompt(profile, base_prompt=base_prompt) or base_prompt
            copilot_agent_config = CopilotAgentConfig(
                name=profile.name,
                description=profile.description,
                prompt=prompt,
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

        elif provider == ProviderType.OPENCODE_CLI.value:
            OPENCODE_AGENTS_DIR.mkdir(parents=True, exist_ok=True)
            ensure_skills_symlink()
            # OpenCode discovers skills natively from OPENCODE_CONFIG_DIR/skills,
            # so the installed system prompt should not embed the CAO skill catalog.
            body = profile.system_prompt or profile.prompt or ""
            opencode_agent_config = OpenCodeAgentConfig(
                description=profile.description,
                mode="all",
                permission=cao_tools_to_opencode_permission(allowed_tools),
            )
            agent_id = to_opencode_agent_id(profile.name)
            agent_file = OPENCODE_AGENTS_DIR / f"{agent_id}.md"
            agent_file.write_text(
                frontmatter.dumps(
                    frontmatter.Post(
                        body.rstrip() if body else "",
                        **opencode_agent_config.model_dump(exclude_none=True),
                    )
                ),
                encoding="utf-8",
            )

            # OpenCode uses a shared opencode.json for MCP declarations. Keep
            # top-level MCP entries default-denied, then re-enable them only
            # for the installed agent. A reinstall without MCP removes stale
            # per-agent grants.
            if profile.mcpServers:
                mcp_names = list(profile.mcpServers.keys())
                for mcp_name, mcp_cfg in profile.mcpServers.items():
                    opencode_mcp_cfg = translate_mcp_server_config(dict(mcp_cfg))
                    upsert_mcp_server(mcp_name, opencode_mcp_cfg)
                upsert_agent_tools(agent_id, mcp_names)
            else:
                remove_agent_tools(agent_id)

        return InstallResult(
            success=True,
            message=f"Agent '{profile.name}' installed successfully",
            agent_name=profile.name,
            context_file=str(context_file),
            agent_file=str(agent_file) if agent_file else None,
            unresolved_vars=unresolved_vars or None,
            source_kind=source_kind,
        )

    except requests.RequestException as exc:
        return InstallResult(success=False, message=f"Failed to download agent: {exc}")
    except FileNotFoundError as exc:
        return InstallResult(success=False, message=str(exc))
    except Exception as exc:
        return InstallResult(success=False, message=f"Failed to install agent: {exc}")
