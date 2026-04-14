"""Skill loading and validation utilities."""

import logging
from pathlib import Path
from typing import List, Tuple

import frontmatter
from pydantic import ValidationError

from cli_agent_orchestrator.constants import SKILLS_DIR
from cli_agent_orchestrator.models.skill import SkillMetadata

logger = logging.getLogger(__name__)

SKILL_CATALOG_INSTRUCTION = (
    "The following skills are available exclusively in this CAO orchestration context. "
    "To load a skill's full content, use the `load_skill` MCP tool provided by the CAO MCP server. "
    "These skills are not accessible through provider-native skill commands or directories."
)


class SkillNameError(ValueError):
    """Raised when a skill name is empty or unsafe to resolve on disk."""


def validate_skill_name(skill_name: str) -> str:
    """Reject skill names that could cause path traversal."""
    normalized_name = skill_name.strip()
    if not normalized_name:
        raise SkillNameError("Skill name must not be empty")
    if "/" in normalized_name or "\\" in normalized_name or ".." in normalized_name:
        raise SkillNameError(
            f"Invalid skill name '{skill_name}': must not contain '/', '\\', or '..'"
        )
    return normalized_name


def _parse_skill_file(skill_file: Path) -> Tuple[SkillMetadata, str]:
    """Parse a skill file and return validated metadata plus Markdown content."""
    try:
        parsed_skill = frontmatter.loads(skill_file.read_text())
    except Exception as exc:
        raise ValueError(f"Failed to parse skill file '{skill_file}': {exc}") from exc

    try:
        metadata = SkillMetadata(**parsed_skill.metadata)
    except ValidationError as exc:
        raise ValueError(f"Invalid skill metadata in '{skill_file}': {exc}") from exc

    return metadata, parsed_skill.content.strip()


def _load_skill_folder(skill_path: Path) -> Tuple[SkillMetadata, str]:
    """Load and validate a skill folder from the filesystem."""
    if not skill_path.exists():
        raise FileNotFoundError(f"Skill folder does not exist: {skill_path}")
    if not skill_path.is_dir():
        raise ValueError(f"Skill path is not a directory: {skill_path}")

    skill_file = skill_path / "SKILL.md"
    if not skill_file.is_file():
        raise FileNotFoundError(f"Missing SKILL.md in skill folder: {skill_path}")

    metadata, content = _parse_skill_file(skill_file)
    if skill_path.name != metadata.name:
        raise ValueError(
            f"Skill folder name '{skill_path.name}' does not match skill name '{metadata.name}'"
        )

    return metadata, content


def load_skill_metadata(name: str) -> SkillMetadata:
    """Load validated metadata for a single installed skill."""
    skill_name = validate_skill_name(name)
    skill_path = SKILLS_DIR / skill_name
    metadata, _ = _load_skill_folder(skill_path)
    return metadata


def load_skill_content(name: str) -> str:
    """Load the Markdown body content for a single installed skill."""
    skill_name = validate_skill_name(name)
    skill_path = SKILLS_DIR / skill_name
    _, content = _load_skill_folder(skill_path)
    return content


def list_skills() -> List[SkillMetadata]:
    """Return all valid skills from the local skill store sorted by name."""
    if not SKILLS_DIR.exists():
        return []

    skills: List[SkillMetadata] = []
    for item in SKILLS_DIR.iterdir():
        if not item.is_dir():
            continue

        try:
            skills.append(load_skill_metadata(item.name))
        except Exception as exc:
            logger.warning("Skipping invalid skill folder '%s': %s", item, exc)

    return sorted(skills, key=lambda skill: skill.name)


def build_skill_catalog() -> str:
    """Build the injected skill catalog block for all installed skills."""
    skills = list_skills()
    if not skills:
        return ""

    skill_lines = [f"- **{skill.name}**: {skill.description}" for skill in skills]

    return "\n".join(
        [
            "## Available Skills",
            "",
            SKILL_CATALOG_INSTRUCTION,
            "",
            *skill_lines,
        ]
    )


def validate_skill_folder(path: Path) -> SkillMetadata:
    """Validate a skill folder at an arbitrary filesystem path."""
    metadata, _ = _load_skill_folder(path)
    return metadata
