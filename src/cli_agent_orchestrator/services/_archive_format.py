"""Phase 4 U2 — shared archive format constants + helpers.

Used by both ``memory_export`` and ``memory_import``. Single source of
truth for caps, regex shapes, schema invariants, and the canonical
content-hash rule (devsecops T7).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Optional

# Format version — bumped only with a fresh threat-model pass.
SUPPORTED_FORMAT_VERSION: int = 1

# Caps (devsecops T1, all BLESSED).
GZIP_RATIO_CAP: int = 200
GZIP_RATIO_FLOOR_BYTES: int = 64 * 1024
DECOMPRESSED_TOTAL_CAP_BYTES: int = 256 * 1024 * 1024  # 256 MiB
TAR_MEMBER_COUNT_CAP: int = 12_000
PER_WIKI_FILE_CAP_BYTES: int = 1 * 1024 * 1024  # 1 MiB
SQLITE_DUMP_CAP_BYTES: int = 16 * 1024 * 1024  # 16 MiB
MANIFEST_CAP_BYTES: int = 64 * 1024  # 64 KiB
INDEX_MD_CAP_BYTES: int = 1 * 1024 * 1024  # 1 MiB
MEMBER_NAME_LENGTH_CAP: int = 256
REJECTION_LIST_CAP: int = 1000  # T16.d
TMP_DIR_PREFIX: str = "cao-import-"
TMP_DIR_AGE_THRESHOLD_HOURS: int = 24
PROJECT_MARKER_STRONG_MODE_DEFAULT: bool = False

# Regex shapes (closed contracts).
KEY_REGEX = re.compile(r"^[a-z0-9-]{1,60}$")
EXPORTED_BY_RE = re.compile(r"^[A-Za-z0-9 ._:/-]{1,64}$")
CONTENT_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
PROJECT_ID_RE = re.compile(r"^[a-zA-Z0-9._\-]{1,128}$")
ISO8601_Z_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

# Member names allowed inside the archive (devsecops T2.c whitelist).
ALLOWED_MEMBER_NAMES_FIXED = frozenset({"manifest.json", "sqlite-dump.json", "index.md"})
WIKI_MEMBER_RE = re.compile(r"^wiki/([a-z0-9-]{1,60})\.md$")

# Manifest closed-key set (devsecops T4).
MANIFEST_REQUIRED_KEYS: frozenset = frozenset(
    {
        "format_version",
        "project_id",
        # Provenance of ``project_id``: ``override`` | ``git_remote`` | ``cwd_hash``.
        # Import uses this to decide portability per issue #316 (a cwd_hash id
        # is host-local and must not be trusted as a stable cross-host key).
        "id_kind",
        "created_at",
        "exported_by",
        "scope_set",
        "n_wiki_files",
        "n_metadata_rows",
        "content_hash",
    }
)

# sqlite-dump.json row schema — closed-key set. Mirrors ``MemoryMetadataModel``
# columns at export time PLUS the ``imported_from`` provenance column. Order
# is irrelevant; ``set(...) == DUMP_ROW_REQUIRED_KEYS`` is the contract.
DUMP_ROW_REQUIRED_KEYS: frozenset = frozenset(
    {
        "id",
        "key",
        "memory_type",
        "scope",
        "scope_id",
        "file_path",
        "tags",
        "source_provider",
        "source_terminal_id",
        "token_estimate",
        "created_at",
        "updated_at",
        "last_compiled_at",
        "access_count",
        "last_accessed_at",
        "related_keys",
        "imported_from",
    }
)

# ``federated`` is a first-class scope value in the shipped federation design
# (PR #314): there is no separate directory and no per-row federation flag.
VALID_SCOPES: frozenset = frozenset({"session", "project", "global", "agent", "federated"})
# ``agent`` is BANNED for U2 import (devsecops T11). ``federated`` imports as
# ``federated`` — no demotion. Secret screening for any imported row is handled
# by ``secret_gate.scan_for_secrets`` at import time.
IMPORTABLE_SCOPES: frozenset = frozenset({"session", "project", "global", "federated"})


# Sentinel content_hash placeholder used during canonical hashing.
CONTENT_HASH_PLACEHOLDER = "sha256:" + ("0" * 64)


# Closed-vocab rejection reasons (mirrors ``ImportReport`` enum below).
REJECTION_REASONS: frozenset = frozenset(
    {
        "path_absolute",
        "path_traversal",
        "symlink",
        "device_or_pipe",
        "size_exceeds_cap",
        "ratio_exceeds_cap",
        "manifest_invalid",
        "format_version_unsupported",
        "content_hash_mismatch",
        "scope_invalid",
        "scope_elevation",
        "key_invalid",
        "encoding_invalid",
        "dump_row_invalid",
        "unknown_member",
        # Reject an imported row whose content trips ``secret_gate``. Reports the
        # matched pattern NAME only — never the offending content bytes.
        "federation_secret_in_import",
    }
)


@dataclass(frozen=True)
class ArchiveRejection:
    member: str
    reason: str
    detail: Optional[str] = None


def canonical_dump_bytes(rows: list) -> bytes:
    """Canonical bytes for ``sqlite-dump.json`` content (devsecops T7).

    Sorted keys, compact separators, ASCII-only — deterministic across
    Python implementations.
    """
    return json.dumps(
        rows,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def canonical_manifest_bytes_with_placeholder(manifest: dict) -> bytes:
    """Canonical manifest bytes with the placeholder hash field.

    Used at export to compute the real hash, AND at import to recompute
    and compare. The substitution rule is symmetric: replace whatever
    ``content_hash`` is present with the all-zero placeholder before
    rendering.
    """
    placeholder = dict(manifest)
    placeholder["content_hash"] = CONTENT_HASH_PLACEHOLDER
    return json.dumps(
        placeholder,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")


def compute_content_hash(
    *,
    manifest: dict,
    dump_rows: list,
    index_md: bytes,
    wiki_files: dict,
) -> str:
    """Compute the canonical sha256 over the inner-archive bytes.

    ``wiki_files`` maps ``key -> bytes``. Files are emitted in lex-sorted
    order to keep the hash deterministic.
    """
    h = hashlib.sha256()
    h.update(canonical_manifest_bytes_with_placeholder(manifest))
    h.update(canonical_dump_bytes(dump_rows))
    h.update(index_md or b"")
    for key in sorted(wiki_files.keys()):
        h.update(wiki_files[key] or b"")
    return "sha256:" + h.hexdigest()


def is_allowed_member_name(name: str) -> bool:
    """T2.c whitelist check. Accepts only the 3 fixed names + wiki/<key>.md."""
    if name in ALLOWED_MEMBER_NAMES_FIXED:
        return True
    return bool(WIKI_MEMBER_RE.match(name))


def member_size_cap(name: str) -> int:
    """Per-member cap (T1)."""
    if name == "manifest.json":
        return MANIFEST_CAP_BYTES
    if name == "sqlite-dump.json":
        return SQLITE_DUMP_CAP_BYTES
    if name == "index.md":
        return INDEX_MD_CAP_BYTES
    if WIKI_MEMBER_RE.match(name):
        return PER_WIKI_FILE_CAP_BYTES
    return 0  # unknown → forces rejection upstream
