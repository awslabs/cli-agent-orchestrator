"""Fail-closed configuration schema for the Obsidian vault source.

This validates configuration only. Filesystem consumers must re-assert
realpath containment immediately beside each filesystem sink.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from pathlib import Path
from typing import Literal, Optional

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    StrictBool,
    StrictInt,
    field_validator,
    model_validator,
)

from cli_agent_orchestrator.constants import (
    CAO_HOME_DIR,
    MEMORY_BASE_DIR,
    MEMORY_SCOPE_BUDGET_CHARS,
    graph_export_root,
)
from cli_agent_orchestrator.utils.path_validation import (
    resolve_and_validate_path,
    validate_path_component,
)

logger = logging.getLogger(__name__)

DEFAULT_MAX_NOTE_BYTES = 262144
DEFAULT_MAX_NOTES = 20000
DEFAULT_MAX_FRONTMATTER_BYTES = 16384
DEFAULT_MAX_RECALL_BODY_CHARS = 4096

# Finite ceilings preserve the denial-of-service boundary. ADR-007 specifies
# defaults and requires caps, but does not prescribe their exact values.
MAX_NOTE_BYTES_LIMIT = 1048576
MAX_NOTES_LIMIT = 100000
MAX_FRONTMATTER_BYTES_LIMIT = 65536
MAX_RECALL_BODY_CHARS_LIMIT = 65536

ALWAYS_EXCLUDED_PATTERNS = (".obsidian/", ".trash/", ".git/", "_cao-*")
_VAULT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SCOPE_ID_RE = re.compile(r"^[a-zA-Z0-9._-]{1,128}$")


def _validate_relative_posix_path(value: str, *, key: str, charset: bool) -> str:
    """Reject malformed configured paths without changing user input."""
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty relative POSIX path")
    if "\x00" in value:
        raise ValueError(f"{key} must not contain a NUL byte")
    if "\\" in value or value.startswith("/") or value.endswith("/"):
        raise ValueError(f"{key} must be a relative POSIX path without a trailing separator")

    segments = value.split("/")
    if any(not segment for segment in segments):
        raise ValueError(f"{key} must not contain empty path segments")
    if any(segment in (".", "..") for segment in segments):
        raise ValueError(f"{key} must not contain '.' or '..' path segments")
    if charset:
        for segment in segments:
            validate_path_component(segment, description=key)
    return value


def _comparison_key(path: str) -> str:
    """Return a conservative textual fallback for paths without usable stats."""
    return unicodedata.normalize("NFC", os.path.normcase(os.path.normpath(path))).casefold()


def _same_path(left: str, right: str) -> bool:
    """Compare existing paths by identity, else use a normalized fallback."""
    try:
        left_stat = os.stat(left)
        right_stat = os.stat(right)
    except OSError:
        return _comparison_key(left) == _comparison_key(right)
    return (left_stat.st_dev, left_stat.st_ino) == (right_stat.st_dev, right_stat.st_ino)


def _is_path_prefix(prefix: str, candidate: str) -> bool:
    prefix_normalized = os.path.normpath(prefix)
    candidate_normalized = os.path.normpath(candidate)
    prefix_key = _comparison_key(prefix_normalized)
    candidate_key = _comparison_key(candidate_normalized)
    return prefix_key == candidate_key or candidate_key.startswith(prefix_key + os.sep)


def _is_path_contained_by(candidate: str, base: str) -> bool:
    current = os.path.normpath(candidate)
    while True:
        if _same_path(current, base):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _paths_overlap(left: str, right: str) -> bool:
    return _is_path_contained_by(left, right) or _is_path_contained_by(right, left)


class FolderMapping(BaseModel):
    """A vault-relative read folder and the memory scope it supplies."""

    model_config = ConfigDict(extra="forbid")

    folder: str
    scope: Literal["global", "project", "agent"]
    scope_id: Optional[str] = None
    index: StrictBool = True
    inject: StrictBool = False
    writable: StrictBool = False
    secret_gate: Literal["reject", "warn"] = "reject"
    allow_hardlinks: StrictBool = False

    @field_validator("folder")
    @classmethod
    def validate_folder(cls, value: str) -> str:
        return _validate_relative_posix_path(value, key="folder", charset=False)

    @field_validator("scope_id")
    @classmethod
    def validate_scope_id(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not _SCOPE_ID_RE.fullmatch(value) or set(value) == {"."}:
            raise ValueError("scope_id must match ^[a-zA-Z0-9._-]{1,128}$ and not be only dots")
        return value

    @model_validator(mode="after")
    def validate_cross_field_rules(self) -> "FolderMapping":
        if self.scope == "global" and self.scope_id is not None:
            raise ValueError("scope_id is forbidden for global scope")
        if self.scope in ("project", "agent") and self.scope_id is None:
            raise ValueError(f"scope_id is required for {self.scope} scope")
        if self.inject and not self.index:
            raise ValueError("inject=true requires index=true")
        return self


class VaultSpec(BaseModel):
    """The release-one single-vault configuration."""

    model_config = ConfigDict(extra="forbid")

    id: str
    root: str
    managed_folder: str
    exclude: list[str] = Field(default_factory=list)
    max_note_bytes: StrictInt = DEFAULT_MAX_NOTE_BYTES
    max_notes: StrictInt = DEFAULT_MAX_NOTES
    max_frontmatter_bytes: StrictInt = DEFAULT_MAX_FRONTMATTER_BYTES
    mappings: list[FolderMapping] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not _VAULT_ID_RE.fullmatch(value):
            raise ValueError("id must match ^[a-z0-9][a-z0-9-]{0,31}$")
        return value

    @field_validator("root")
    @classmethod
    def validate_root(cls, value: str) -> str:
        return resolve_and_validate_path(value, allow_create=False, description="root")

    @field_validator("managed_folder")
    @classmethod
    def validate_managed_folder(cls, value: str) -> str:
        return _validate_relative_posix_path(value, key="managed_folder", charset=True)

    @field_validator("exclude")
    @classmethod
    def validate_exclude(cls, value: list[str]) -> list[str]:
        for pattern in value:
            if not isinstance(pattern, str) or not pattern:
                raise ValueError("exclude patterns must be non-empty strings")
            if pattern.startswith("/") or "\\" in pattern or ".." in pattern.split("/"):
                raise ValueError("exclude patterns must be relative and not contain '..'")
        return value

    @field_validator("max_note_bytes")
    @classmethod
    def validate_max_note_bytes(cls, value: int) -> int:
        return _validate_bounded_int(value, "max_note_bytes", MAX_NOTE_BYTES_LIMIT)

    @field_validator("max_notes")
    @classmethod
    def validate_max_notes(cls, value: int) -> int:
        return _validate_bounded_int(value, "max_notes", MAX_NOTES_LIMIT)

    @field_validator("max_frontmatter_bytes")
    @classmethod
    def validate_max_frontmatter_bytes(cls, value: int) -> int:
        return _validate_bounded_int(value, "max_frontmatter_bytes", MAX_FRONTMATTER_BYTES_LIMIT)

    @model_validator(mode="after")
    def validate_vault_rules(self) -> "VaultSpec":
        root = self.root
        if root == str(Path.home().resolve()):
            raise ValueError("root must not be the user's home directory")
        for name, protected in (
            ("MEMORY_BASE_DIR", str(MEMORY_BASE_DIR.resolve())),
            ("CAO_HOME_DIR", str(CAO_HOME_DIR.resolve())),
            ("graph export root", str(graph_export_root().expanduser().resolve())),
        ):
            if _paths_overlap(root, protected):
                raise ValueError(f"root must not overlap {name}")

        folders = [mapping.folder for mapping in self.mappings]
        for index, folder in enumerate(folders):
            for other in folders[index + 1 :]:
                if _is_path_prefix(folder, other) or _is_path_prefix(other, folder):
                    raise ValueError("mappings[].folder values must not overlap")

        containing = [
            mapping
            for mapping in self.mappings
            if _is_path_prefix(mapping.folder, self.managed_folder)
        ]
        writable = [mapping for mapping in self.mappings if mapping.writable]
        if len(containing) != 1 or not containing[0].writable:
            raise ValueError("managed_folder must lie inside exactly one writable mapping")
        if len(writable) != 1:
            raise ValueError("only the managed_folder mapping may be writable")

        bindings = [(mapping.scope, mapping.scope_id) for mapping in self.mappings]
        if len(bindings) != len(set(bindings)):
            raise ValueError("mappings must not resolve to the same (scope, scope_id)")
        return self


def _validate_bounded_int(value: int, key: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= maximum:
        raise ValueError(f"{key} must be a positive integer no greater than {maximum}")
    return value


class VaultConfig(BaseModel):
    """Top-level ``memory.vault`` object."""

    model_config = ConfigDict(extra="forbid")

    enabled: StrictBool = False
    max_recall_body_chars: StrictInt = DEFAULT_MAX_RECALL_BODY_CHARS
    vaults: list[VaultSpec] = Field(default_factory=list)
    _warnings: tuple[str, ...] = PrivateAttr(default=())

    @field_validator("max_recall_body_chars")
    @classmethod
    def validate_max_recall_body_chars(cls, value: int) -> int:
        return _validate_bounded_int(value, "max_recall_body_chars", MAX_RECALL_BODY_CHARS_LIMIT)

    @model_validator(mode="after")
    def validate_config_rules(self) -> "VaultConfig":
        if self.enabled and not self.vaults:
            raise ValueError("enabled=true requires at least one vault")
        if len(self.vaults) > 1:
            raise ValueError("vaults supports only one vault in release one")

        warnings = tuple(
            f"mapping {mapping.folder!r} has secret_gate='warn' with inject=true"
            for vault in self.vaults
            for mapping in vault.mappings
            if mapping.secret_gate == "warn" and mapping.inject
        )
        self._warnings = warnings
        for message in warnings:
            logger.warning(message)
        if self.max_recall_body_chars > MEMORY_SCOPE_BUDGET_CHARS:
            logger.warning(
                "max_recall_body_chars=%s exceeds the injection scope budget %s; "
                "the injection renderer requires its own lower cap",
                self.max_recall_body_chars,
                MEMORY_SCOPE_BUDGET_CHARS,
            )
        return self

    @property
    def warnings(self) -> tuple[str, ...]:
        """Configuration warnings that status must keep visible."""
        return self._warnings
