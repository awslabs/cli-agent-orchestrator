"""Deterministic, NFC-normalized vault note identity helpers.

Derived keys deliberately use the filename stem rather than ADR-006's full
mapping-relative path prefix.  A derived key becomes a graph node label, so
omitting directory names avoids exposing vault structure to graph consumers.
Its digest still covers the full NFC-normalized mapping-relative path.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Optional

_KEY_RE = re.compile(r"^[a-z0-9-]{1,60}$")
_UNSAFE_KEY_CHARS = re.compile(r"[^a-z0-9]+")


def normalize_mapping_relative_path(path: str) -> str:
    """Return the NFC mapping-relative path used for all identity hashing."""
    return unicodedata.normalize("NFC", path.replace("\\", "/"))


def validate_cao_key(key: str) -> str:
    """Reject, rather than sanitize, an authored key outside MemoryKey's charset."""
    if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
        raise ValueError("cao.key must match ^[a-z0-9-]{1,60}$")
    return key


def derive_cao_key(mapping_relative_path: str) -> str:
    """Derive a <=60-character filename-label key with a full-path digest.

    This is a documented divergence from ADR-006's full-path display prefix:
    the readable portion is the filename stem to reduce directory-structure
    disclosure through graph labels.  The stable suffix hashes the complete
    NFC-normalized mapping-relative path, preserving uniqueness and determinism.
    """
    normalized = normalize_mapping_relative_path(mapping_relative_path)
    without_extension = normalized[:-3] if normalized.endswith(".md") else normalized
    filename_stem = without_extension.rsplit("/", 1)[-1]
    stem = _UNSAFE_KEY_CHARS.sub("-", filename_stem.lower()).strip("-") or "note"
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]
    return f"{stem[:51]}-{digest}"


def cao_key(authored_key: Optional[str], mapping_relative_path: str) -> str:
    """Use an authored key when present, otherwise return the derived key."""
    return (
        validate_cao_key(authored_key)
        if authored_key is not None
        else derive_cao_key(mapping_relative_path)
    )


def derive_note_uid(vault_id: str, scope: str, scope_id: Optional[str], key: str) -> str:
    """Derive the stable vault note id from its identity tuple."""
    payload = "\0".join((vault_id, scope, scope_id or "", key))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
