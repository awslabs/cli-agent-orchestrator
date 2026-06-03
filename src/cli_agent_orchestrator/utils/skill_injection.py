"""Skill catalog injection helpers for installed Q and Copilot agent files.

Kiro CLI uses native ``skill://`` resources with progressive loading, so it
does not need prompt-based catalog baking or refresh-on-skill-change.
"""

import json
import logging
import os
from pathlib import Path
from typing import Any, Iterator, List, Optional
from urllib.parse import unquote, urlparse

import frontmatter

from cli_agent_orchestrator.constants import (
    AGENT_CONTEXT_DIR,
    COPILOT_AGENTS_DIR,
    Q_AGENTS_DIR,
)
from cli_agent_orchestrator.models.agent_profile import AgentProfile
from cli_agent_orchestrator.utils.agent_profiles import load_agent_profile
from cli_agent_orchestrator.utils.skills import build_skill_catalog

logger = logging.getLogger(__name__)


def compose_agent_prompt(profile: AgentProfile, base_prompt: Optional[str] = None) -> Optional[str]:
    """Compose the baked prompt from profile prompt and global skill catalog.

    When *base_prompt* is provided it is used instead of ``profile.prompt``.
    This is needed for providers like Copilot where the effective prompt is
    resolved from ``system_prompt`` falling back to ``prompt``.
    """
    parts: list[str] = []

    if base_prompt is not None:
        effective = base_prompt.strip()
    else:
        effective = profile.prompt.strip() if profile.prompt else ""

    if effective:
        parts.append(effective)

    catalog = build_skill_catalog()
    if catalog:
        parts.append(catalog)

    if not parts:
        return None

    return "\n\n".join(parts)


def refresh_agent_json_prompt(json_path: Path, profile: AgentProfile) -> bool:
    """Atomically rewrite the prompt field of one installed Q agent JSON."""
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
            json.dump(config, temp_file, indent=2, ensure_ascii=False)
        os.replace(temp_path, json_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return True


def refresh_agent_md_prompt(md_path: Path, profile: AgentProfile) -> bool:
    """Atomically rewrite the body of one installed Copilot ``.agent.md`` file.

    Preserves the YAML frontmatter (name, description) while replacing the
    Markdown body with the composed prompt (profile base prompt + skill catalog).
    """
    if not md_path.exists():
        return False

    post = frontmatter.load(md_path)

    # Copilot prompt resolution: system_prompt takes priority over prompt
    system_prompt = profile.system_prompt.strip() if profile.system_prompt else ""
    base = system_prompt or (profile.prompt.strip() if profile.prompt else "")

    new_body = compose_agent_prompt(profile, base_prompt=base)
    post.content = (new_body or "").rstrip()

    temp_path = md_path.with_suffix(md_path.suffix + ".tmp")
    try:
        temp_path.write_text(frontmatter.dumps(post), encoding="utf-8")
        os.replace(temp_path, md_path)
    finally:
        if temp_path.exists():
            temp_path.unlink()

    return True


def refresh_installed_agent_for_profile(profile_name: str) -> List[Path]:
    """Refresh installed Q and Copilot agents for one source profile."""
    profile = load_agent_profile(profile_name)
    safe_name = profile.name.replace("/", "__")
    refreshed_paths: List[Path] = []

    q_path = Q_AGENTS_DIR / f"{safe_name}.json"
    if refresh_agent_json_prompt(q_path, profile):
        refreshed_paths.append(q_path)

    copilot_path = COPILOT_AGENTS_DIR / f"{safe_name}.agent.md"
    if refresh_agent_md_prompt(copilot_path, profile):
        refreshed_paths.append(copilot_path)

    return refreshed_paths


def refresh_all_cao_managed_agents() -> List[Path]:
    """Refresh every installed Q/Copilot agent managed by CAO."""
    refreshed_paths: List[Path] = []

    # Q JSON agents — identified by resources pointing at AGENT_CONTEXT_DIR
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
            # Bulk refresh should never let one bad installed JSON block the rest.
            logger.warning(
                "Skipping CAO-managed agent '%s' at %s: source profile could not be loaded: %s",
                profile_name,
                json_path,
                exc,
            )
            continue

        if refresh_agent_json_prompt(json_path, profile):
            refreshed_paths.append(json_path)

    # Copilot .agent.md agents — identified by matching context file in AGENT_CONTEXT_DIR
    for md_path in _iter_installed_copilot_agents():
        post = frontmatter.load(md_path)
        profile_name = post.metadata.get("name")
        if not isinstance(profile_name, str) or not profile_name:
            logger.warning("Skipping Copilot agent with missing name: %s", md_path)
            continue

        if not _is_cao_managed_copilot_agent(profile_name):
            continue

        try:
            profile = load_agent_profile(profile_name)
        except Exception as exc:
            logger.warning(
                "Skipping CAO-managed Copilot agent '%s' at %s: "
                "source profile could not be loaded: %s",
                profile_name,
                md_path,
                exc,
            )
            continue

        if refresh_agent_md_prompt(md_path, profile):
            refreshed_paths.append(md_path)

    return refreshed_paths


def _iter_installed_agent_jsons() -> Iterator[Path]:
    """Yield installed Q agent JSON files."""
    if not Q_AGENTS_DIR.exists():
        return
    yield from sorted(Q_AGENTS_DIR.glob("*.json"))


def _iter_installed_copilot_agents() -> Iterator[Path]:
    """Yield installed Copilot ``.agent.md`` files."""
    if not COPILOT_AGENTS_DIR.exists():
        return
    yield from sorted(COPILOT_AGENTS_DIR.glob("*.agent.md"))


def _is_cao_managed_copilot_agent(name: str) -> bool:
    """Return True when a corresponding CAO context file exists for this agent name."""
    context_file = AGENT_CONTEXT_DIR / f"{name}.md"
    return context_file.exists()


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
