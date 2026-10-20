"""Canonical logical-boundary matching for vault-relative paths."""

from __future__ import annotations

import fnmatch
import unicodedata
from collections.abc import Sequence

from cli_agent_orchestrator.services.vault.config import ALWAYS_EXCLUDED_PATTERNS


def normalize_relpath(relpath: str) -> str:
    """Return one NFC POSIX relative path without ambiguous components."""
    normalized = unicodedata.normalize("NFC", relpath.replace("\\", "/"))
    components = normalized.split("/")
    if (
        not normalized
        or normalized.startswith("/")
        or any(component in {"", ".", ".."} for component in components)
    ):
        raise ValueError("path must be a non-empty relative component path")
    return normalized


def is_supported_relpath(relpath: str) -> bool:
    """Return whether ``relpath`` has one unambiguous canonical representation."""
    try:
        normalize_relpath(relpath)
    except ValueError:
        return False
    return True


def relpath_within_folder(relpath: str, folder: str) -> bool:
    """Return whether ``relpath`` is equal to or below ``folder`` by component."""
    path_components = normalize_relpath(relpath).split("/")
    folder_components = normalize_relpath(folder).split("/")
    return path_components[: len(folder_components)] == folder_components


def is_excluded_relpath(relpath: str, exclude: Sequence[str]) -> bool:
    """Apply the scanner's case-insensitive POSIX glob semantics."""
    folded_path = normalize_relpath(relpath).casefold()
    patterns = tuple(
        unicodedata.normalize("NFC", pattern.replace("\\", "/")).casefold()
        for pattern in (*ALWAYS_EXCLUDED_PATTERNS, *exclude)
    )
    return any(_posix_glob_matches(folded_path, pattern) for pattern in patterns)


def _posix_glob_matches(path: str, pattern: str) -> bool:
    if pattern.endswith("/"):
        directory = pattern.rstrip("/")
        return any(component == directory for component in path.split("/"))
    if "/" not in pattern and any(
        fnmatch.fnmatchcase(component, pattern) for component in path.split("/")
    ):
        return True
    return fnmatch.fnmatchcase(path, pattern) or (
        pattern.startswith("**/") and fnmatch.fnmatchcase(path, pattern[3:])
    )
