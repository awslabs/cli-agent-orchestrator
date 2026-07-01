"""Skill loading and validation utilities."""

import fnmatch
import logging
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import frontmatter
from pydantic import ValidationError

from cli_agent_orchestrator.constants import SKILLS_DIR
from cli_agent_orchestrator.models.skill import SkillMetadata

logger = logging.getLogger(__name__)

SKILL_CATALOG_INSTRUCTION = (
    "The following skills are available exclusively in this CAO orchestration context. "
    "To load a skill's full content, use the `mcp__cao-mcp-server__load_skill` tool (NOT the Skill command). "
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


def _resolve_skill(skill_name: str) -> Tuple[SkillMetadata, str]:
    """Load a skill by name from the global store or extra directories.

    Scans the search dirs in resolution order and returns the first
    ``<dir>/<skill_name>`` that loads as a *valid* skill ("first valid match
    wins"), so resolution stays consistent with :func:`list_skills`: an earlier
    folder that contains a ``SKILL.md`` but fails to load no longer shadows a
    later valid folder of the same name. Without this, the injected catalog
    could advertise a skill (from a later dir) that the subsequent ``load_skill``
    call then fails to resolve.

    The matched folder is parsed exactly once. When no candidate loads cleanly,
    the error from the first folder that contains a ``SKILL.md`` is re-raised so
    the underlying validation failure is surfaced; if no folder contains a
    ``SKILL.md`` at all, a ``FileNotFoundError`` referencing the canonical global
    path is raised instead.
    """
    first_error: Optional[Exception] = None
    for directory in _skill_search_dirs():
        candidate = directory / skill_name
        if not (candidate / "SKILL.md").is_file():
            continue
        try:
            return _load_skill_folder(candidate)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error
    return _load_skill_folder(SKILLS_DIR / skill_name)


def load_skill_metadata(name: str) -> SkillMetadata:
    """Load validated metadata for a single installed skill."""
    metadata, _ = _resolve_skill(validate_skill_name(name))
    return metadata


def load_skill_content(name: str) -> str:
    """Load the Markdown body content for a single installed skill."""
    _, content = _resolve_skill(validate_skill_name(name))
    return content


def list_skills() -> List[SkillMetadata]:
    """Return all valid skills from the global store and extra directories.

    Directories are scanned in resolution order (global store first, then
    ``extra_skill_dirs``); the first *valid* occurrence of a skill name wins, so
    a skill in the global store is not shadowed by one in a later extra
    directory. Invalid skill folders are skipped without reserving the name,
    which matches :func:`_resolve_skill` ("first valid match wins"). The
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


def build_skill_catalog(skill_filter: Optional[List[str]] = None) -> str:
    """Build the injected skill catalog block.

    Args:
        skill_filter: Optional allowlist of skill-name patterns — exact names or
            case-sensitive fnmatch globs such as ``"ads-*"``. When ``None`` (the
            default) every installed skill is listed, preserving the original
            behaviour. When a list is given, only skills whose name matches at
            least one pattern are listed; an empty list advertises no skills at
            all. Patterns that match no installed skill are logged (usually a
            typo or a stale skill name).
    """
    skills = list_skills()
    if skill_filter is not None:
        matched_patterns: Set[str] = set()
        selected: List[SkillMetadata] = []
        for skill in skills:
            keep = False
            for pattern in skill_filter:
                if fnmatch.fnmatchcase(skill.name, pattern):
                    matched_patterns.add(pattern)
                    keep = True
            if keep:
                selected.append(skill)
        unmatched = [pattern for pattern in skill_filter if pattern not in matched_patterns]
        if unmatched:
            logger.warning(
                "Skill-catalog filter matched no installed skill for pattern(s): %s",
                ", ".join(repr(pattern) for pattern in unmatched),
            )
        skills = selected
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
