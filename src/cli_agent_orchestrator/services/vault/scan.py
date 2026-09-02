"""Bounded, read-only vault traversal with sink-local containment checks."""

from __future__ import annotations

import fnmatch
import hashlib
import os
import stat
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from cli_agent_orchestrator.services.memory_reconciliation import (
    _first_symlink_component,
)
from cli_agent_orchestrator.services.vault.config import (
    ALWAYS_EXCLUDED_PATTERNS,
    FolderMapping,
    VaultSpec,
)
from cli_agent_orchestrator.services.vault.findings import FindingCode, finding_severity
from cli_agent_orchestrator.services.vault.parser import (
    ParseResult,
    classify_secret,
    parse_note,
)

# Bounds the aggregate bytes opened in one scan even when independent config
# ceilings would otherwise permit an impractical product.
MAX_TOTAL_SCAN_BYTES = 64 * 1024 * 1024

SCAN_BYTE_BUDGET_EXCEEDED = FindingCode.BYTE_BUDGET_EXCEEDED
SCAN_NOTE_LIMIT_EXCEEDED = FindingCode.NOTE_LIMIT_EXCEEDED


@dataclass(frozen=True)
class ScanFinding:
    """Content-free outcome for one scanned or refused candidate."""

    code: FindingCode
    detail: str
    severity: str


@dataclass(frozen=True)
class ScanNote:
    """One deterministic scan result; text exists only in memory for reconcile."""

    vault_relpath: str
    scope: str
    scope_id: Optional[str]
    status: str
    text: Optional[str] = None
    parsed: Optional[ParseResult] = None
    content_sha256: Optional[str] = None
    frontmatter_sha256: Optional[str] = None
    size_bytes: Optional[int] = None
    mtime_ns: Optional[int] = None
    findings: tuple[ScanFinding, ...] = ()


@dataclass(frozen=True)
class ScanReport:
    """Frozen, sorted scan output with no database or vault side effects."""

    notes: tuple[ScanNote, ...]
    total_bytes_scanned: int
    max_total_bytes: int


@dataclass(frozen=True)
class _Candidate:
    path: str
    relpath: str
    mapping: FolderMapping
    discovery_stat: os.stat_result


def scan_vault(
    vault: VaultSpec,
    *,
    max_total_bytes: int = MAX_TOTAL_SCAN_BYTES,
) -> ScanReport:
    """Scan mapped Markdown files in deterministic order without writing anything."""
    if not 0 < max_total_bytes <= MAX_TOTAL_SCAN_BYTES:
        raise ValueError(f"max_total_bytes must be between 1 and {MAX_TOTAL_SCAN_BYTES}")

    root = os.path.realpath(vault.root)
    candidates, notes = _discover_candidates(root, vault)
    blocked_by_case = _case_collision_paths(candidates)
    total_bytes = 0

    for index, candidate in enumerate(candidates):
        if index >= vault.max_notes:
            notes.append(
                _refused(
                    candidate,
                    SCAN_NOTE_LIMIT_EXCEEDED,
                    f"max_notes exceeded; {len(candidates) - index} candidates skipped",
                )
            )
            break
        if candidate.relpath in blocked_by_case:
            notes.append(
                _refused(
                    candidate,
                    FindingCode.PATH_CASE_COLLISION,
                    "case-collision path",
                )
            )
            continue

        try:
            before = os.lstat(candidate.path)
        except OSError:
            notes.append(_refused(candidate, FindingCode.UNSTABLE_SKIPPED, "stat failed"))
            continue
        if before.st_size > vault.max_note_bytes:
            notes.append(_refused(candidate, FindingCode.NOTE_TOO_LARGE, "max_note_bytes exceeded"))
            continue
        if total_bytes + before.st_size > max_total_bytes:
            notes.append(
                _refused(
                    candidate,
                    SCAN_BYTE_BUDGET_EXCEEDED,
                    f"total scan byte budget exceeded; {len(candidates) - index} candidates skipped",
                )
            )
            break

        remaining_bytes = max_total_bytes - total_bytes
        read, after, refusal = _read_stable_utf8(
            candidate,
            root,
            vault.max_note_bytes,
            max_read_bytes=min(vault.max_note_bytes, remaining_bytes),
        )
        if refusal is not None:
            notes.append(_refused(candidate, refusal, _refusal_detail(refusal), after))
            continue
        assert read is not None and after is not None
        raw_bytes = read
        total_bytes += len(raw_bytes)
        try:
            text = raw_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            notes.append(
                _refused(
                    candidate,
                    FindingCode.NOTE_NOT_UTF8,
                    "note bytes are not valid UTF-8",
                    after,
                )
            )
            continue

        normalized = _normalize_line_endings(text)
        if "\x00" in normalized:
            notes.append(
                _refused(
                    candidate,
                    FindingCode.NOTE_CONTAINS_NUL,
                    "note contains NUL byte",
                    after,
                )
            )
            continue
        parsed = parse_note(
            normalized,
            max_frontmatter_bytes=vault.max_frontmatter_bytes,
            secret_gate=candidate.mapping.secret_gate,
        )
        findings = _parse_findings(parsed)
        plugin_format = (
            candidate.relpath.lower().endswith(".excalidraw.md")
            or "excalidraw-plugin" in parsed.frontmatter
        )
        if plugin_format:
            findings += (
                ScanFinding(
                    FindingCode.PLUGIN_FORMAT_EXCLUDED,
                    "Excalidraw format is not indexed",
                    finding_severity(FindingCode.PLUGIN_FORMAT_EXCLUDED, secret_gate="reject"),
                ),
            )
        # User frontmatter is preserved verbatim by managed writes. Scan only
        # authored body content; the writer separately rejects secret-shaped
        # generated ``cao`` metadata before publishing it.
        secret = classify_secret(
            parsed.region.body,
            secret_gate=candidate.mapping.secret_gate,
        )
        if secret is not None:
            findings += (ScanFinding(secret[0], secret[2], secret[1]),)

        quarantined = any(finding.severity == "error" for finding in findings)
        notes.append(
            ScanNote(
                vault_relpath=candidate.relpath,
                scope=candidate.mapping.scope,
                scope_id=candidate.mapping.scope_id,
                status=(
                    "unsupported" if plugin_format else "quarantined" if quarantined else "indexed"
                ),
                text=normalized,
                parsed=parsed,
                content_sha256=_sha256(normalized),
                frontmatter_sha256=_sha256(parsed.region.raw) if parsed.region.raw else None,
                size_bytes=after.st_size,
                mtime_ns=after.st_mtime_ns,
                findings=findings,
            )
        )

    return ScanReport(
        notes=tuple(sorted(notes, key=lambda note: note.vault_relpath.encode("utf-8"))),
        total_bytes_scanned=total_bytes,
        max_total_bytes=max_total_bytes,
    )


def _discover_candidates(
    root: str, vault: VaultSpec
) -> tuple[tuple[_Candidate, ...], list[ScanNote]]:
    candidates: list[_Candidate] = []
    notes: list[ScanNote] = []
    root_path = Path(root)
    for mapping in vault.mappings:
        if not mapping.index:
            continue
        mapping_root = os.path.join(root, mapping.folder)
        if not os.path.isdir(mapping_root):
            notes.append(
                ScanNote(
                    mapping.folder,
                    mapping.scope,
                    mapping.scope_id,
                    "skipped",
                    findings=(
                        ScanFinding(
                            FindingCode.MAPPING_FOLDER_MISSING,
                            "mapping folder is missing",
                            finding_severity(
                                FindingCode.MAPPING_FOLDER_MISSING, secret_gate="reject"
                            ),
                        ),
                    ),
                )
            )
            continue
        if _first_symlink_component(Path(mapping_root), root_path) is not None:
            relpath = mapping.folder
            notes.append(
                ScanNote(
                    relpath,
                    mapping.scope,
                    mapping.scope_id,
                    "quarantined",
                    findings=(
                        ScanFinding(
                            FindingCode.SYMLINK_REFUSED,
                            "symlinked mapping root",
                            finding_severity(FindingCode.SYMLINK_REFUSED, secret_gate="reject"),
                        ),
                    ),
                )
            )
            continue

        def onerror(_error: OSError) -> None:
            notes.append(
                ScanNote(
                    _relpath(_error.filename or mapping_root, root),
                    mapping.scope,
                    mapping.scope_id,
                    "skipped",
                    findings=(
                        ScanFinding(
                            FindingCode.MAPPING_FOLDER_UNREADABLE,
                            "mapping folder is unreadable",
                            finding_severity(
                                FindingCode.MAPPING_FOLDER_UNREADABLE,
                                secret_gate="reject",
                            ),
                        ),
                    ),
                )
            )

        for directory, dirnames, filenames in os.walk(
            mapping_root, followlinks=False, onerror=onerror
        ):
            retained_dirs: list[str] = []
            for name in sorted(dirnames, key=lambda item: item.encode("utf-8")):
                path = os.path.join(directory, name)
                relpath = _relpath(path, root)
                if _excluded(relpath, vault):
                    continue
                if _first_symlink_component(Path(path), root_path) is not None:
                    notes.append(
                        ScanNote(
                            relpath,
                            mapping.scope,
                            mapping.scope_id,
                            "quarantined",
                            findings=(
                                ScanFinding(
                                    FindingCode.SYMLINK_REFUSED,
                                    "symlinked path component",
                                    finding_severity(
                                        FindingCode.SYMLINK_REFUSED,
                                        secret_gate="reject",
                                    ),
                                ),
                            ),
                        )
                    )
                    continue
                retained_dirs.append(name)
            dirnames[:] = retained_dirs
            for filename in sorted(filenames, key=lambda name: name.encode("utf-8")):
                path = os.path.join(directory, filename)
                relpath = _relpath(path, root)
                if _excluded(relpath, vault) or not filename.lower().endswith(".md"):
                    continue
                if _first_symlink_component(Path(path), root_path) is not None:
                    notes.append(
                        ScanNote(
                            relpath,
                            mapping.scope,
                            mapping.scope_id,
                            "quarantined",
                            findings=(
                                ScanFinding(
                                    FindingCode.SYMLINK_REFUSED,
                                    "symlinked path component",
                                    finding_severity(
                                        FindingCode.SYMLINK_REFUSED,
                                        secret_gate="reject",
                                    ),
                                ),
                            ),
                        )
                    )
                    continue
                if _sync_conflict_name(filename):
                    notes.append(
                        ScanNote(
                            relpath,
                            mapping.scope,
                            mapping.scope_id,
                            "skipped",
                            findings=(
                                ScanFinding(
                                    FindingCode.SYNC_ARTIFACT_SKIPPED,
                                    "sync-conflict filename",
                                    finding_severity(
                                        FindingCode.SYNC_ARTIFACT_SKIPPED,
                                        secret_gate="reject",
                                    ),
                                ),
                            ),
                        )
                    )
                    continue
                try:
                    metadata = os.lstat(path)
                except OSError:
                    continue
                if (
                    stat.S_ISREG(metadata.st_mode)
                    and metadata.st_nlink > 1
                    and not mapping.allow_hardlinks
                ):
                    notes.append(
                        ScanNote(
                            relpath,
                            mapping.scope,
                            mapping.scope_id,
                            "quarantined",
                            findings=(
                                ScanFinding(
                                    FindingCode.HARDLINK_REFUSED,
                                    "hardlink refused",
                                    finding_severity(
                                        FindingCode.HARDLINK_REFUSED,
                                        secret_gate="reject",
                                    ),
                                ),
                            ),
                        )
                    )
                    continue
                candidates.append(_Candidate(path, relpath, mapping, metadata))
    return tuple(sorted(candidates, key=lambda candidate: candidate.relpath.encode("utf-8"))), notes


def _read_stable_utf8(
    candidate: _Candidate,
    root: str,
    max_note_bytes: int,
    *,
    max_read_bytes: Optional[int] = None,
) -> tuple[Optional[bytes], Optional[os.stat_result], Optional[FindingCode]]:
    """Read a single vetted inode; this is a read-window consistency check, not a lock."""
    read_limit = min(max_note_bytes, max_note_bytes if max_read_bytes is None else max_read_bytes)
    real_path = os.path.realpath(candidate.path)
    # SafeAccessCheck — a bare str and single positive containment guard are
    # colocated with this module's only path-reachable fd-open sink.
    if not real_path.startswith(root + os.sep):
        return None, None, FindingCode.PATH_ESCAPES_ROOT
    try:
        fd = os.open(real_path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None, None, FindingCode.UNSTABLE_SKIPPED
    try:
        before = os.fstat(fd)
        if (before.st_dev, before.st_ino) != (
            candidate.discovery_stat.st_dev,
            candidate.discovery_stat.st_ino,
        ):
            return None, before, FindingCode.PATH_ESCAPES_ROOT
        with os.fdopen(fd, "rb", closefd=False) as handle:
            content = handle.read(read_limit + 1)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(content) > read_limit:
        return (
            None,
            after,
            (
                FindingCode.NOTE_TOO_LARGE
                if read_limit == max_note_bytes
                else FindingCode.BYTE_BUDGET_EXCEEDED
            ),
        )
    if _stat_identity(before) != _stat_identity(after):
        return None, after, FindingCode.UNSTABLE_SKIPPED
    return content, after, None


def _refusal_detail(code: FindingCode) -> str:
    return {
        FindingCode.PATH_ESCAPES_ROOT: "opened file does not match discovered in-root inode",
        FindingCode.NOTE_TOO_LARGE: "max_note_bytes exceeded",
        FindingCode.BYTE_BUDGET_EXCEEDED: "total scan byte budget exceeded",
        FindingCode.UNSTABLE_SKIPPED: "file changed during read",
    }[code]


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    """Signals changed by normal in-place writes, including same-size writes."""
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _case_collision_paths(candidates: tuple[_Candidate, ...]) -> set[str]:
    groups: dict[str, list[str]] = {}
    for candidate in candidates:
        key = unicodedata.normalize("NFC", candidate.relpath).casefold()
        groups.setdefault(key, []).append(candidate.relpath)
    return {path for group in groups.values() if len(group) > 1 for path in group}


def _excluded(relpath: str, vault: VaultSpec) -> bool:
    folded_path = relpath.casefold()
    patterns = tuple(pattern.casefold() for pattern in (*ALWAYS_EXCLUDED_PATTERNS, *vault.exclude))
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


def _sync_conflict_name(filename: str) -> bool:
    lowered = filename.lower()
    return (
        ".sync-conflict-" in lowered
        or "conflicted copy" in lowered
        or lowered.endswith(".icloud")
        or lowered.startswith(".~lock.")
    )


def _parse_findings(parsed: ParseResult) -> tuple[ScanFinding, ...]:
    if parsed.finding_code is None:
        return ()
    # Parser failures are independent of mapping secret_gate except secret_detected,
    # which is added through classify_secret below.
    return (
        ScanFinding(
            parsed.finding_code,
            parsed.finding_detail or parsed.finding_code.value,
            finding_severity(parsed.finding_code, secret_gate="reject"),
        ),
    )


def _refused(
    candidate: _Candidate,
    code: FindingCode,
    detail: str,
    metadata: Optional[os.stat_result] = None,
) -> ScanNote:
    return ScanNote(
        candidate.relpath,
        candidate.mapping.scope,
        candidate.mapping.scope_id,
        "quarantined" if finding_severity(code, secret_gate="reject") == "error" else "skipped",
        size_bytes=metadata.st_size if metadata is not None else None,
        mtime_ns=metadata.st_mtime_ns if metadata is not None else None,
        findings=(ScanFinding(code, detail, finding_severity(code, secret_gate="reject")),),
    )


def _relpath(path: str, root: str) -> str:
    return unicodedata.normalize("NFC", os.path.relpath(path, root).replace(os.sep, "/"))


def _normalize_line_endings(text: str) -> str:
    if text.startswith("\ufeff"):
        text = text[1:]
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
