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


def _read_agent_profile_source(agent_name: str) -> str:
    """Locate an agent profile across configured stores and return the raw text.

    Search order:
    1. Local store: ~/.aws/cli-agent-orchestrator/agent-store/{name}.md
    2. Provider-specific directories (flat {name}.md or {name}/agent.md)
    3. Extra user-added directories (flat {name}.md or {name}/agent.md)
    4. Built-in store (packaged with CAO)

    Shared by ``load_agent_profile`` (which parses the text into an
    ``AgentProfile``) and the install service (which writes the raw text to
    the context file). Centralising the lookup keeps the two callers in sync.
    """
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
        flat = directory / f"{agent_name}.md"
        if flat.exists():
            return flat.read_text(encoding="utf-8")
        nested = directory / agent_name / "agent.md"
        if nested.exists():
            return nested.read_text(encoding="utf-8")

    for extra_dir in get_extra_agent_dirs():
        directory = Path(extra_dir)
        if not directory.exists():
            continue
        flat = directory / f"{agent_name}.md"
        if flat.exists():
            return flat.read_text(encoding="utf-8")
        nested = directory / agent_name / "agent.md"
        if nested.exists():
            return nested.read_text(encoding="utf-8")

    agent_store = resources.files("cli_agent_orchestrator.agent_store")
    built_in = agent_store / f"{agent_name}.md"
    if built_in.is_file():
        return built_in.read_text(encoding="utf-8")

    raise FileNotFoundError(f"Agent profile not found: {agent_name}")


def load_agent_profile(agent_name: str) -> AgentProfile:
    """Load an agent profile from the configured stores."""
    try:
        raw_text = _read_agent_profile_source(agent_name)
        return parse_agent_profile_text(resolve_env_vars(raw_text), agent_name)
    except (FileNotFoundError, ValueError):
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
