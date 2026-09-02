"""Closed vocabulary and supported-boundary data for vault findings."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Literal, Mapping

Severity = Literal["info", "warn", "error"]


class FindingCode(str, Enum):
    HEADING_FRAGMENT_IGNORED = "heading_fragment_ignored"
    BLOCK_REFERENCE_UNSUPPORTED = "block_reference_unsupported"
    EMBED_NOT_INLINED = "embed_not_inlined"
    ATTACHMENT_IGNORED = "attachment_ignored"
    ALIAS_AMBIGUOUS = "alias_ambiguous"
    LINK_AMBIGUOUS = "link_ambiguous"
    KEY_COLLISION = "key_collision"
    LINK_EXCLUDED = "link_excluded"
    LINK_DANGLING = "link_dangling"
    FRONTMATTER_MALFORMED = "frontmatter_malformed"
    FRONTMATTER_UNSAFE = "frontmatter_unsafe"
    FRONTMATTER_TOO_LARGE = "frontmatter_too_large"
    INVALID_CAO_BLOCK = "invalid_cao_block"
    KEY_INVALID = "key_invalid"
    NOTE_TOO_LARGE = "note_too_large"
    PLUGIN_FORMAT_EXCLUDED = "plugin_format_excluded"
    SYMLINK_REFUSED = "symlink_refused"
    SYNC_ARTIFACT_SKIPPED = "sync_artifact_skipped"
    PATH_CASE_COLLISION = "path_case_collision"
    HARDLINK_REFUSED = "hardlink_refused"
    PATH_ESCAPES_ROOT = "path_escapes_root"
    NOTE_NOT_UTF8 = "note_not_utf8"
    SECRET_DETECTED = "secret_detected"
    UNSTABLE_SKIPPED = "unstable_skipped"
    LINK_LIMIT_EXCEEDED = "link_limit_exceeded"
    LINK_TARGET_INVALID = "link_target_invalid"
    BYTE_BUDGET_EXCEEDED = "byte_budget_exceeded"
    NOTE_LIMIT_EXCEEDED = "note_limit_exceeded"
    MAPPING_FOLDER_MISSING = "mapping_folder_missing"
    MAPPING_FOLDER_UNREADABLE = "mapping_folder_unreadable"
    NOTE_CONTAINS_NUL = "note_contains_nul"
    RENAME_WITH_EDIT_UNRESOLVED = "rename_with_edit_unresolved"
    RENAME_AMBIGUOUS = "rename_ambiguous"
    CAO_LINK_CONFLICT = "cao_link_conflict"
    EDGE_LIMIT_EXCEEDED = "edge_limit_exceeded"
    DEINDEXED_RETAINED = "deindexed_retained"


@dataclass(frozen=True)
class BoundaryRule:
    construct: str
    classification: str
    behavior: str
    finding_code: FindingCode | None = None


FINDING_SEVERITIES: Mapping[FindingCode, Severity] = MappingProxyType(
    {
        FindingCode.HEADING_FRAGMENT_IGNORED: "info",
        FindingCode.BLOCK_REFERENCE_UNSUPPORTED: "info",
        FindingCode.EMBED_NOT_INLINED: "info",
        FindingCode.ATTACHMENT_IGNORED: "info",
        FindingCode.ALIAS_AMBIGUOUS: "warn",
        FindingCode.LINK_AMBIGUOUS: "warn",
        FindingCode.KEY_COLLISION: "error",
        FindingCode.LINK_EXCLUDED: "info",
        FindingCode.LINK_DANGLING: "info",
        FindingCode.FRONTMATTER_MALFORMED: "error",
        FindingCode.FRONTMATTER_UNSAFE: "error",
        FindingCode.FRONTMATTER_TOO_LARGE: "error",
        FindingCode.INVALID_CAO_BLOCK: "error",
        FindingCode.KEY_INVALID: "error",
        FindingCode.NOTE_TOO_LARGE: "warn",
        FindingCode.PLUGIN_FORMAT_EXCLUDED: "info",
        FindingCode.SYMLINK_REFUSED: "error",
        FindingCode.SYNC_ARTIFACT_SKIPPED: "info",
        FindingCode.PATH_CASE_COLLISION: "error",
        FindingCode.HARDLINK_REFUSED: "warn",
        FindingCode.PATH_ESCAPES_ROOT: "warn",
        FindingCode.NOTE_NOT_UTF8: "error",
        FindingCode.UNSTABLE_SKIPPED: "warn",
        FindingCode.LINK_LIMIT_EXCEEDED: "warn",
        FindingCode.LINK_TARGET_INVALID: "warn",
        FindingCode.BYTE_BUDGET_EXCEEDED: "warn",
        FindingCode.NOTE_LIMIT_EXCEEDED: "warn",
        FindingCode.MAPPING_FOLDER_MISSING: "warn",
        FindingCode.MAPPING_FOLDER_UNREADABLE: "warn",
        FindingCode.NOTE_CONTAINS_NUL: "error",
        FindingCode.RENAME_WITH_EDIT_UNRESOLVED: "warn",
        FindingCode.RENAME_AMBIGUOUS: "warn",
        FindingCode.CAO_LINK_CONFLICT: "warn",
        FindingCode.EDGE_LIMIT_EXCEEDED: "warn",
        FindingCode.DEINDEXED_RETAINED: "warn",
    }
)


def finding_severity(code: FindingCode, *, secret_gate: str) -> Severity:
    """Return a finding severity, including the mapping-dependent secret rule."""
    if code == FindingCode.SECRET_DETECTED:
        if secret_gate == "reject":
            return "error"
        if secret_gate == "warn":
            return "warn"
        raise ValueError("secret_gate must be 'reject' or 'warn'")
    return FINDING_SEVERITIES[code]


SUPPORTED_BOUNDARY: tuple[BoundaryRule, ...] = (
    BoundaryRule("[[Note]]", "supported", 'relates_to edge, origin = "vault"'),
    BoundaryRule("[[Note|Display]]", "supported", "same edge; display text discarded"),
    BoundaryRule("[[folder/Note]]", "supported", "path-qualified match preferred"),
    BoundaryRule(
        "[[Note#Heading]]",
        "degraded",
        "note-level edge; fragment retained",
        FindingCode.HEADING_FRAGMENT_IGNORED,
    ),
    BoundaryRule(
        "[[Note#^blockid]]", "refused", "no edge", FindingCode.BLOCK_REFERENCE_UNSUPPORTED
    ),
    BoundaryRule(
        "![[Note]] embed", "degraded", "edge only; body not inlined", FindingCode.EMBED_NOT_INLINED
    ),
    BoundaryRule(
        "non-Markdown embed", "refused", "no edge or indexing", FindingCode.ATTACHMENT_IGNORED
    ),
    BoundaryRule("aliases frontmatter", "supported", "resolution input only"),
    BoundaryRule("ambiguous alias", "refused", "no edge", FindingCode.ALIAS_AMBIGUOUS),
    BoundaryRule("duplicate basenames", "refused", "no bare-link edge", FindingCode.LINK_AMBIGUOUS),
    BoundaryRule(
        "duplicate cao.key", "refused", "both notes quarantined", FindingCode.KEY_COLLISION
    ),
    BoundaryRule("link to excluded note", "refused", "no edge", FindingCode.LINK_EXCLUDED),
    BoundaryRule("link to nonexistent note", "refused", "no edge", FindingCode.LINK_DANGLING),
    BoundaryRule("relative Markdown .md link", "supported", "treated as a wikilink"),
    BoundaryRule("http(s) Markdown link", "refused", "no edge or fetch"),
    BoundaryRule(
        "malformed frontmatter", "refused", "note quarantined", FindingCode.FRONTMATTER_MALFORMED
    ),
    BoundaryRule(
        "unsafe YAML anchors or aliases",
        "refused",
        "note quarantined",
        FindingCode.FRONTMATTER_UNSAFE,
    ),
    BoundaryRule(
        "oversize frontmatter", "refused", "note quarantined", FindingCode.FRONTMATTER_TOO_LARGE
    ),
    BoundaryRule("invalid cao block", "refused", "note quarantined", FindingCode.INVALID_CAO_BLOCK),
    BoundaryRule("invalid cao.key", "refused", "note quarantined", FindingCode.KEY_INVALID),
    BoundaryRule("oversize note", "refused", "not indexed", FindingCode.NOTE_TOO_LARGE),
    BoundaryRule("Dataview inline fields", "not interpreted", "preserved as prose"),
    BoundaryRule("Templater, Kanban, similar syntax", "not interpreted", "preserved as prose"),
    BoundaryRule("Excalidraw note", "refused", "not indexed", FindingCode.PLUGIN_FORMAT_EXCLUDED),
    BoundaryRule(".canvas", "refused", "never a candidate"),
    BoundaryRule("always-excluded paths", "refused", "never scanned"),
    BoundaryRule("symlinked path component", "refused", "not indexed", FindingCode.SYMLINK_REFUSED),
    BoundaryRule(
        "sync-conflict filename", "refused", "never a candidate", FindingCode.SYNC_ARTIFACT_SKIPPED
    ),
    BoundaryRule(
        "case-collision path", "refused", "both quarantined", FindingCode.PATH_CASE_COLLISION
    ),
    BoundaryRule("hardlink", "refused unless enabled", "not indexed", FindingCode.HARDLINK_REFUSED),
    BoundaryRule(
        "metadata path escapes root", "refused", "row skipped", FindingCode.PATH_ESCAPES_ROOT
    ),
    BoundaryRule("non-UTF-8 note", "refused", "quarantined", FindingCode.NOTE_NOT_UTF8),
    BoundaryRule(
        "secret-bearing note",
        "configuration-dependent",
        "reported in both modes",
        FindingCode.SECRET_DETECTED,
    ),
    BoundaryRule("derived-key collision", "refused", "both quarantined", FindingCode.KEY_COLLISION),
    BoundaryRule("unstable note", "deferred", "skipped this run", FindingCode.UNSTABLE_SKIPPED),
    BoundaryRule(
        "more than 1000 body wikilinks",
        "degraded",
        "first 1000 retained",
        FindingCode.LINK_LIMIT_EXCEEDED,
    ),
    BoundaryRule(
        "oversize or control-character link target",
        "refused",
        "no edge",
        FindingCode.LINK_TARGET_INVALID,
    ),
    BoundaryRule(
        "aggregate scan byte budget exceeded",
        "deferred",
        "remaining candidates skipped",
        FindingCode.BYTE_BUDGET_EXCEEDED,
    ),
    BoundaryRule(
        "configured note count limit exceeded",
        "deferred",
        "remaining candidates skipped",
        FindingCode.NOTE_LIMIT_EXCEEDED,
    ),
    BoundaryRule(
        "missing mapping folder",
        "deferred",
        "mapping skipped",
        FindingCode.MAPPING_FOLDER_MISSING,
    ),
    BoundaryRule(
        "unreadable mapping folder",
        "deferred",
        "mapping skipped",
        FindingCode.MAPPING_FOLDER_UNREADABLE,
    ),
    BoundaryRule(
        "NUL-containing note", "refused", "note quarantined", FindingCode.NOTE_CONTAINS_NUL
    ),
    BoundaryRule(
        "rename plus edit without cao.key",
        "deferred",
        "delete-plus-create; no identity guess",
        FindingCode.RENAME_WITH_EDIT_UNRESOLVED,
    ),
    BoundaryRule(
        "ambiguous content-hash rename",
        "deferred",
        "treated as new; no identity guess",
        FindingCode.RENAME_AMBIGUOUS,
    ),
    BoundaryRule(
        "conflicting duplicate cao.links entry",
        "refused",
        "no edge for the conflicting target/type tuple",
        FindingCode.CAO_LINK_CONFLICT,
    ),
    BoundaryRule(
        "more than 64 projected edges for one source/type",
        "degraded",
        "canonical entries then sorted body-only entries retained",
        FindingCode.EDGE_LIMIT_EXCEEDED,
    ),
    BoundaryRule(
        "excluded note changes identity at the same path",
        "deferred",
        "new identity remains excluded",
        FindingCode.DEINDEXED_RETAINED,
    ),
)
