"""Agent profile utilities."""

import logging
from importlib import resources
from pathlib import Path
from typing import Dict, List

import frontmatter

from cli_agent_orchestrator.constants import LOCAL_AGENT_STORE_DIR, PROVIDERS
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.env import resolve_env_vars

logger = logging.getLogger(__name__)


def _validate_agent_name(agent_name: str) -> None:
    """Reject agent names that could cause path traversal."""
    if "/" in agent_name or "\\" in agent_name or ".." in agent_name:
        raise ValueError(f"Invalid agent name '{agent_name}': must not contain '/', '\\', or '..'")


def _scan_directory(directory: Path, source_label: str, profiles: Dict[str, Dict]) -> None:
    """Scan a directory for agent profiles (.md files, .json files, or subdirectories)."""
    if not directory.exists():
        return
    for item in directory.iterdir():
        if item.is_dir():
            profile_name = item.name
            desc = ""
            # Check for agent.md inside directory
            agent_md = item / "agent.md"
            if agent_md.exists():
                try:
                    data = frontmatter.loads(agent_md.read_text())
                    desc = data.metadata.get("description", "")
                except Exception:
                    pass
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "description": desc,
                    "source": source_label,
                }
        elif item.suffix == ".md" and item.is_file():
            profile_name = item.stem
            desc = ""
            try:
                data = frontmatter.loads(item.read_text())
                desc = data.metadata.get("description", "")
            except Exception:
                pass
            if profile_name not in profiles:
                profiles[profile_name] = {
                    "name": profile_name,
                    "description": desc,
                    "source": source_label,
                }


def list_agent_profiles() -> List[Dict]:
    """Discover all available agent profiles from all configured directories.

    Scans built-in store, local store, and all provider agent directories
    (from settings or defaults). Returns deduplicated list sorted by name.
    """
    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_extra_agent_dirs,
    )

    profiles: Dict[str, Dict] = {}

    # 1. Built-in agent store
    try:
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        for item in agent_store.iterdir():
            name = item.name
            if name.endswith(".md"):
                profile_name = name[:-3]
                try:
                    data = frontmatter.loads(item.read_text())
                    profiles[profile_name] = {
                        "name": profile_name,
                        "description": data.metadata.get("description", ""),
                        "source": "built-in",
                    }
                except Exception:
                    profiles[profile_name] = {
                        "name": profile_name,
                        "description": "",
                        "source": "built-in",
                    }
    except Exception as e:
        logger.debug(f"Could not scan built-in agent store: {e}")

    # 2. Local agent store (~/.aws/cli-agent-orchestrator/agent-store/)
    _scan_directory(LOCAL_AGENT_STORE_DIR, "local", profiles)

    # 3. Provider-specific directories (from settings)
    agent_dirs = get_agent_dirs()
    provider_source_labels = {
        "kiro_cli": "kiro",
        "q_cli": "q_cli",
        "claude_code": "claude_code",
        "codex": "codex",
        "cao_installed": "installed",
    }
    for provider, dir_path in agent_dirs.items():
        label = provider_source_labels.get(provider, provider)
        path = Path(dir_path)
        # Skip if it's the same as local store (already scanned)
        if path.resolve() == LOCAL_AGENT_STORE_DIR.resolve():
            continue
        _scan_directory(path, label, profiles)

    # 4. Extra user-added directories
    for extra_dir in get_extra_agent_dirs():
        _scan_directory(Path(extra_dir), "custom", profiles)

    return sorted(profiles.values(), key=lambda p: p["name"])


def parse_agent_profile_text(resolved_text: str, profile_name: str) -> AgentProfile:
    """Parse an AgentProfile from already-resolved markdown text."""
    profile_data = frontmatter.loads(resolved_text)
    meta = profile_data.metadata
    meta["system_prompt"] = profile_data.content.strip()
    # Fill in required fields if missing (Kiro profiles don't have frontmatter)
    if "name" not in meta:
        meta["name"] = profile_name
    if "description" not in meta:
        meta["description"] = ""
    return AgentProfile(**meta)


def _try_load_from_path(profile_path: Path, profile_name: str) -> AgentProfile:
    """Load an AgentProfile from a .md file path."""
    return parse_agent_profile_text(resolve_env_vars(profile_path.read_text()), profile_name)


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load agent profile from local, provider, or built-in agent store.

    Search order:
    1. Local store: ~/.aws/cli-agent-orchestrator/agent-store/{name}.md
    2. Provider-specific directories (e.g. ~/.kiro/agents/{name}/agent.md or {name}.md)
    3. Extra user-added directories
    4. Built-in store (packaged with CAO)
    """
    _validate_agent_name(agent_name)

    from cli_agent_orchestrator.services.settings_service import (
        get_agent_dirs,
        get_extra_agent_dirs,
    )

    try:
        # 1. Check local store first (flat .md files)
        local_profile = LOCAL_AGENT_STORE_DIR / f"{agent_name}.md"
        if local_profile.exists():
            return _try_load_from_path(local_profile, agent_name)

        # 2. Check all provider-specific directories
        for _provider, dir_path in get_agent_dirs().items():
            p = Path(dir_path)
            if not p.exists():
                continue
            # Check flat file: {dir}/{name}.md
            flat = p / f"{agent_name}.md"
            if flat.exists():
                return _try_load_from_path(flat, agent_name)
            # Check directory-style: {dir}/{name}/agent.md
            dir_style = p / agent_name / "agent.md"
            if dir_style.exists():
                return _try_load_from_path(dir_style, agent_name)

        # 3. Check extra user-added directories
        for extra_dir in get_extra_agent_dirs():
            p = Path(extra_dir)
            if not p.exists():
                continue
            flat = p / f"{agent_name}.md"
            if flat.exists():
                return _try_load_from_path(flat, agent_name)
            dir_style = p / agent_name / "agent.md"
            if dir_style.exists():
                return _try_load_from_path(dir_style, agent_name)

        # 4. Fall back to built-in store
        agent_store = resources.files("cli_agent_orchestrator.agent_store")
        profile_file = agent_store / f"{agent_name}.md"

        if not profile_file.is_file():
            raise FileNotFoundError(f"Agent profile not found: {agent_name}")

        return parse_agent_profile_text(resolve_env_vars(profile_file.read_text()), agent_name)

    except FileNotFoundError:
        raise
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
    except (FileNotFoundError, RuntimeError):
        # Profile not found or failed to load — provider.initialize()
        # will surface a clear error later.  Fall back for now.
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
