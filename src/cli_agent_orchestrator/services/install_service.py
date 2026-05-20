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


# Profile names are used as filesystem path segments under LOCAL_AGENT_STORE_DIR
# and provider agent dirs. Restricting to [A-Za-z0-9_-] with a 64-char cap blocks
# traversal ("../etc/passwd"), separators, and absolute paths at the boundary.
# CodeQL also recognises this regex as a path-injection sanitiser.
_PROFILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

# URL path component for allowlisted hosts. Each segment must start with an
# alphanumeric, which forbids "..", "." and hidden segments — and by extension
# any traversal sequence. Used to rebuild a safe URL from validated parts,
# which is the CodeQL-recognised SSRF sanitisation pattern.
_SAFE_URL_PATH_RE = re.compile(r"^(/[A-Za-z0-9_][A-Za-z0-9_.-]*)+\.md$")

# SSRF guard: only fetch profiles from hosts we explicitly trust. Operators can
# extend via CAO_PROFILE_ALLOWED_HOSTS (e.g. an internal profile mirror).
_DEFAULT_ALLOWED_HOSTS = frozenset(
    {
        "github.com",
        "raw.githubusercontent.com",
    }
)

# (connect, read) seconds. Tighter than a single-number timeout: 5s connect fails
# fast on a dead/hostile IP; 30s read leaves room for flaky residential networks
# without letting a slow-loris peer tie up a cao-server worker indefinitely.
_HTTP_TIMEOUT = (5, 30)


def _allowed_download_hosts() -> frozenset:
    override = os.environ.get("CAO_PROFILE_ALLOWED_HOSTS")
    if override:
        hosts = {h.strip().lower() for h in override.split(",") if h.strip()}
        if hosts:
            return frozenset(hosts)
    return _DEFAULT_ALLOWED_HOSTS


def _download_agent(source: str) -> str:
    """Download an agent profile from an https:// URL into the local store.

    File-path handling deliberately does NOT live in this module: only the CLI
    has legitimate filesystem trust, and keeping Path(user_input) out of the
    HTTP-reachable layer closes an entire class of py/path-injection alerts
    (CodeQL #49/#61 kept reopening while this lived here). The CLI entry point
    copies local files into LOCAL_AGENT_STORE_DIR itself and then calls
    install_agent() with the bare stem, which flows through the "name" branch.
    """
    LOCAL_AGENT_STORE_DIR.mkdir(parents=True, exist_ok=True)

    # SSRF hardening: narrow what a caller-provided URL can reach before any
    # network I/O happens. https-only rules out http://169.254.169.254/...;
    # the host allowlist rules out arbitrary internal services; the path
    # regex rules out crafted paths that would write outside the store.
    parsed = urlparse(source)
    if parsed.scheme != "https":
        raise ValueError("Profile URL must use https://")
    host = (parsed.hostname or "").lower()
    allowed_hosts = _allowed_download_hosts()
    if host not in allowed_hosts:
        raise ValueError(
            f"Host '{host}' is not in the allowed downloader hosts. "
            "Set CAO_PROFILE_ALLOWED_HOSTS to extend the allowlist."
        )
    # Reject any URL that carries a query string, fragment, or userinfo —
    # none of them are meaningful for a static .md fetch and each is an
    # SSRF foothold (credentials encoded in @, redirect targets in ?next=).
    if parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError("Profile URL must not include query, fragment, or userinfo.")
    if not _SAFE_URL_PATH_RE.fullmatch(parsed.path):
        raise ValueError("URL path must match /segment/.../file.md with no traversal segments.")
    filename = parsed.path.rsplit("/", 1)[-1]
    if not _PROFILE_NAME_RE.fullmatch(filename[: -len(".md")]):
        raise ValueError("URL filename stem must match [A-Za-z0-9_-]{1,64}")

    # Look up the canonical host from the allowlist instead of passing the
    # parsed host back through. Belt-and-braces: even if a caller smuggled
    # an odd Unicode codepoint that normalised into a known host name,
    # `safe_host` is guaranteed to be a literal from our trust root.
    safe_host = next(h for h in allowed_hosts if h == host)
    safe_url = f"https://{safe_host}{parsed.path}"

    # allow_redirects=False + explicit is_redirect check: an allowlisted
    # host could otherwise 302 us to an internal target (IMDS, admin panel)
    # and the allowlist would never see the hop.
    response = requests.get(safe_url, timeout=_HTTP_TIMEOUT, allow_redirects=False)
    if response.is_redirect:
        raise ValueError("Redirects are not allowed for profile downloads.")
    response.raise_for_status()

    dest_file = LOCAL_AGENT_STORE_DIR / filename
    dest_file.write_text(response.text, encoding="utf-8")
    return dest_file.stem


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
) -> InstallResult:
    """Install an agent profile for the requested provider.

    ``source`` must be either an https:// URL on the allowlist or a bare
    profile name matching ``_PROFILE_NAME_RE``. Local ``.md`` file paths
    are deliberately NOT accepted here — the CLI copies user files into
    the local store itself and then calls this function with the resulting
    bare stem. This split is what lets the HTTP/MCP surface share this
    function safely: every caller reaches the same two sanitised shapes,
    and no call site constructs ``Path(user_input)`` through this module.
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
            source_kind: Literal["url", "name"] = "url"
        else:
            # `source` is treated as a bare profile name and feeds
            # _read_agent_profile_source() which builds Path objects from it.
            # Enforce the sanitiser at the boundary so every downstream sink
            # (agent_profiles.py and the provider-dir loop) sees safe input.
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
