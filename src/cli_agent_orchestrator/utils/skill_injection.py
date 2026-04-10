"""Skill catalog injection helpers for installed Kiro and Q agent JSON files."""

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator, List, Optional
from urllib.parse import unquote, urlparse

from cli_agent_orchestrator.constants import AGENT_CONTEXT_DIR, KIRO_AGENTS_DIR, Q_AGENTS_DIR
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.services.terminal_service import build_skill_catalog
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile

logger = logging.getLogger(__name__)


def compose_agent_prompt(profile: AgentProfile) -> Optional[str]:
    """Compose the baked JSON prompt from the profile prompt and global skill catalog."""
    parts: list[str] = []

    if profile.prompt and profile.prompt.strip():
        parts.append(profile.prompt.strip())

    catalog = build_skill_catalog()
    if catalog:
        parts.append(catalog)

    if not parts:
        return None

    return "\n\n".join(parts)


def refresh_agent_json_prompt(json_path: Path, profile: AgentProfile) -> bool:
    """Atomically rewrite the prompt field of one installed Kiro/Q agent JSON."""
    if not json_path.exists():
        return False

    with json_path.open(encoding="utf-8") as source_file:
        loaded_config = json.load(source_file)

    if not isinstance(loaded_config, dict):
        raise ValueError(f"Agent config at '{json_path}' must be a JSON object")

    config: dict[str, Any] = loaded_config
    new_prompt = compose_agent_prompt(profile)
    if new_prompt is None:
        config.pop("prompt", None)
    else:
        config["prompt"] = new_prompt

    temp_path = json_path.with_suffix(json_path.suffix + ".tmp")
    try:
        with temp_path.open("w", encoding="utf-8") as temp_file:
            json.dump(config, temp_file, indent=2)
        os.replace(temp_path, json_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return True


def refresh_installed_agent_for_profile(profile_name: str) -> List[Path]:
    """Refresh the installed Kiro and Q agent JSONs for one source profile."""
    profile = load_agent_profile(profile_name)
    safe_name = profile.name.replace("/", "__")
    refreshed_paths: List[Path] = []

    for json_path in (
        KIRO_AGENTS_DIR / f"{safe_name}.json",
        Q_AGENTS_DIR / f"{safe_name}.json",
    ):
        if refresh_agent_json_prompt(json_path, profile):
            refreshed_paths.append(json_path)

    return refreshed_paths


def refresh_all_cao_managed_agents() -> List[Path]:
    """Refresh every installed Kiro/Q JSON whose resources show CAO management."""
    refreshed_paths: List[Path] = []

    for json_path in _iter_installed_agent_jsons():
        with json_path.open(encoding="utf-8") as source_file:
            loaded_config = json.load(source_file)

        if not isinstance(loaded_config, dict):
            logger.warning("Skipping non-object agent config: %s", json_path)
            continue

        config: dict[str, Any] = loaded_config
        resources = config.get("resources")
        if not _is_cao_managed_resources(resources):
            continue

        profile_name = config.get("name")
        if not isinstance(profile_name, str) or not profile_name:
            logger.warning("Skipping CAO-managed agent with missing name: %s", json_path)
            continue

        try:
            profile = load_agent_profile(profile_name)
        except Exception as exc:
            logger.warning(
                "Skipping CAO-managed agent '%s' at %s: source profile could not be loaded: %s",
                profile_name,
                json_path,
                exc,
            )
            continue

        if refresh_agent_json_prompt(json_path, profile):
            refreshed_paths.append(json_path)

    return refreshed_paths


def _iter_installed_agent_jsons() -> Iterator[Path]:
    """Yield installed agent JSON files from the Kiro and Q agent directories."""
    for agents_dir in (KIRO_AGENTS_DIR, Q_AGENTS_DIR):
        if not agents_dir.exists():
            continue
        yield from sorted(agents_dir.glob("*.json"))


def _is_cao_managed_resources(resources: object) -> bool:
    """Return True when a resources list includes a CAO-managed context file URI."""
    if not isinstance(resources, list):
        return False

    context_dir = AGENT_CONTEXT_DIR.resolve(strict=False)
    for resource in resources:
        if not isinstance(resource, str):
            continue
        if _is_cao_managed_resource_uri(resource, context_dir):
            return True

    return False


def _is_cao_managed_resource_uri(resource: str, context_dir: Path) -> bool:
    """Return True when a file:// URI points at a file within AGENT_CONTEXT_DIR."""
    parsed = urlparse(resource)
    if parsed.scheme != "file":
        return False

    resource_path = Path(unquote(parsed.path)).resolve(strict=False)
    return resource_path.is_relative_to(context_dir)
