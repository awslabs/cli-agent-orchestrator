"""Skill loading and validation utilities."""

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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


def _skill_search_dirs() -> List[Path]:
    """Return skill store directories in resolution order.

    The global skill store (``SKILLS_DIR``) is searched first, followed by any
    user-added directories from the ``extra_skill_dirs`` setting. This mirrors
    agent-profile resolution (global store first, then extra user directories),
    so a skill in the global store is never shadowed by a later extra directory.
    """
    from cli_agent_orchestrator.services.settings_service import get_extra_skill_dirs

    dirs: List[Path] = [SKILLS_DIR]
    dirs.extend(Path(extra) for extra in get_extra_skill_dirs())
    return dirs


def _resolve_skill_path(skill_name: str) -> Path:
    """Locate a skill folder across the global store and extra directories.

    Returns the first ``<dir>/<skill_name>`` that loads as a *valid* skill, so
    resolution stays consistent with :func:`list_skills` ("first valid match
    wins"): an earlier folder that contains a ``SKILL.md`` but fails to load no
    longer shadows a later valid folder of the same name. Without this, the
    injected catalog could advertise a skill (from a later dir) that the
    subsequent ``load_skill`` call then fails to resolve.

    When no candidate loads cleanly, falls back to the first folder that does
    contain a ``SKILL.md`` so the caller re-raises the underlying validation
    error, or ultimately to ``SKILLS_DIR / skill_name`` so the error references
    the canonical global path.
    """
    first_with_skill_md: Optional[Path] = None
    for directory in _skill_search_dirs():
        candidate = directory / skill_name
        if not (candidate / "SKILL.md").is_file():
            continue
        if first_with_skill_md is None:
            first_with_skill_md = candidate
        try:
            _load_skill_folder(candidate)
        except Exception:
            continue
        return candidate
    return first_with_skill_md if first_with_skill_md is not None else SKILLS_DIR / skill_name


def load_skill_metadata(name: str) -> SkillMetadata:
    """Load validated metadata for a single installed skill."""
    skill_name = validate_skill_name(name)
    metadata, _ = _load_skill_folder(_resolve_skill_path(skill_name))
    return metadata


def load_skill_content(name: str) -> str:
    """Load the Markdown body content for a single installed skill."""
    skill_name = validate_skill_name(name)
    _, content = _load_skill_folder(_resolve_skill_path(skill_name))
    return content


def list_skills() -> List[SkillMetadata]:
    """Return all valid skills from the global store and extra directories.

    Directories are scanned in resolution order (global store first, then
    ``extra_skill_dirs``); the first *valid* occurrence of a skill name wins, so
    a skill in the global store is not shadowed by one in a later extra
    directory. Invalid skill folders are skipped without reserving the name,
    which matches :func:`_resolve_skill_path` ("first valid match wins"). The
    result is sorted by name.
    """
    skills_by_name: Dict[str, SkillMetadata] = {}
    for directory in _skill_search_dirs():
        if not directory.is_dir():
            continue
        for item in directory.iterdir():
            if not item.is_dir() or item.name in skills_by_name:
                continue
            # extra_skill_dirs may point at a broad project root, so only treat a
            # subdirectory as a skill when it actually contains a SKILL.md;
            # unrelated folders are skipped silently. A folder that has a
            # SKILL.md but fails to load is still reported below.
            if not (item / "SKILL.md").is_file():
                continue
            try:
                metadata, _ = _load_skill_folder(item)
                skills_by_name[item.name] = metadata
            except Exception as exc:
                logger.warning("Skipping invalid skill folder '%s': %s", item, exc)

    return sorted(skills_by_name.values(), key=lambda skill: skill.name)


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
